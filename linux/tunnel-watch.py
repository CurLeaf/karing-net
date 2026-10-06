#!/usr/bin/env python3
"""
Karing 网络监控 Linux 版 v2.0 - 主动优化版

核心功能：
- 🔍 监控代理和直连线路健康状态
- 🚀 检测慢速节点并主动触发 URLTest
- ✂️ 清理慢速节点的闲置连接
- 📊 记录节点性能数据到数据库
- 🔔 桌面通知状态变化

工作原理：
1. 每 30 秒探测代理和直连线路
2. 每 120 秒测试当前节点吞吐量
3. 检测到慢速节点（< 256 KB/s × 3 次）时：
   - 断开该节点的闲置连接
   - 触发 URLTest 立即测试
   - 等待 Karing 自动切换
   - 通知用户结果
"""

import argparse
import json
import logging
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# 尝试导入性能追踪器
try:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from node_tracker import NodePerformanceTracker
    PERF_TRACKING = True
except ImportError:
    PERF_TRACKING = False

# 配置
SERVICE_JSON = Path.home() / ".local/share/com.nebula.karing/service.json"
LOG_FILE = Path.home() / ".local/share/karing-net/tunnel-watch.log"
CONTROL_PORT = 6677

INTERVAL = 10  # 主循环间隔
PROBE_INTERVAL = 30  # HTTP 探测间隔
THROUGHPUT_INTERVAL = 120  # 吞吐量测试间隔
THROUGHPUT_URL = 'https://speed.cloudflare.com/__down?bytes=262144'
THROUGHPUT_BYTES = 262144
MIN_THROUGHPUT_BPS = 256 * 1024  # 256 KB/s
MONITOR_GROUP = 'urltest_out'
SLOW_NODE_THRESHOLD = 3  # 连续 3 次慢速触发切换
NOTIFY_COOLDOWN = 15 * 60  # 通知冷却 15 分钟

# 健康检查阈值
FAIL_THRESHOLD = 3  # 连续失败 3 次判定 down
RECOVER_THRESHOLD = 2  # 连续成功 2 次判定恢复


class Health:
    """健康状态跟踪器"""

    def __init__(self, name: str):
        self.name = name
        self.state = 'unknown'
        self.failures = 0
        self.successes = 0

    def note(self, ok: bool) -> str | None:
        """记录一次检查结果，返回状态变化事件"""
        before = self.state

        if ok:
            self.successes += 1
            self.failures = 0
            if self.successes >= RECOVER_THRESHOLD:
                self.state = 'up'
        else:
            self.failures += 1
            self.successes = 0
            if self.failures >= FAIL_THRESHOLD:
                self.state = 'down'

        # 返回状态变化事件
        if self.state != before and (self.state == 'down' or before == 'down'):
            return 'down' if self.state == 'down' else 'recovered'
        return None


def setup_logging():
    """配置日志"""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger('tunnel-watch')


def api_request(path: str, method: str = 'GET', timeout: float = 6.0) -> dict[str, Any]:
    """调用 Karing Clash API"""
    if not SERVICE_JSON.exists():
        raise FileNotFoundError(f'{SERVICE_JSON} not found')

    cfg = json.loads(SERVICE_JSON.read_text())
    port = int(cfg.get('control_port') or CONTROL_PORT)
    secret = str(cfg.get('secret') or '')

    url = f'http://127.0.0.1:{port}{path}'
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {secret}'}, method=method)

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def http_probe(url: str, proxy_port: int | None = None, expected=(200, 204, 301, 302)) -> dict:
    """HTTP 探测"""
    args = ['/usr/bin/curl', '--silent', '--show-error', '--output', '/dev/null',
            '--connect-timeout', '4', '--max-time', '8',
            '--write-out', '%{http_code} %{time_total}']

    if proxy_port:
        args.extend(['--proxy', f'http://127.0.0.1:{proxy_port}', '--noproxy', ''])
    else:
        args.extend(['--noproxy', '*'])

    args.append(url)

    start = time.monotonic()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
        parts = proc.stdout.strip().split()
        code = int(parts[0]) if len(parts) > 0 else 0
        elapsed = float(parts[1]) if len(parts) > 1 else 0

        ok = proc.returncode == 0 and code in expected
        error = '' if ok else f'curl={proc.returncode} HTTP={code} {proc.stderr.strip()[:120]}'

        return {'ok': ok, 'http': code, 'seconds': round(elapsed, 3), 'error': error}
    except Exception as exc:
        return {'ok': False, 'http': 0, 'seconds': round(time.monotonic() - start, 3),
                'error': str(exc)[:160]}


def throughput_probe(url: str = THROUGHPUT_URL, proxy_port: int | None = None) -> dict:
    """吞吐量测试"""
    args = ['/usr/bin/curl', '--silent', '--show-error', '--output', '/dev/null',
            '--connect-timeout', '4', '--max-time', '15',
            '--write-out', '%{http_code} %{size_download} %{time_total}']

    if proxy_port:
        args.extend(['--proxy', f'http://127.0.0.1:{proxy_port}', '--noproxy', ''])
    else:
        args.extend(['--noproxy', '*'])

    args.append(url)

    start = time.monotonic()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=18)
        parts = proc.stdout.strip().split()
        code = int(parts[0]) if len(parts) > 0 else 0
        size = float(parts[1]) if len(parts) > 1 else 0
        elapsed = float(parts[2]) if len(parts) > 2 else 0

        bps = size / elapsed if elapsed > 0 else 0
        ok = proc.returncode == 0 and code in (200, 206) and size >= THROUGHPUT_BYTES * 0.9

        if ok and bps < MIN_THROUGHPUT_BPS:
            ok = False

        error = '' if ok else f'curl={proc.returncode} HTTP={code} bytes={size:.0f} bps={bps:.0f}'

        return {'ok': ok, 'http': code, 'bytes': round(size), 'seconds': round(elapsed, 3),
                'bytes_per_second': round(bps), 'error': error}
    except Exception as exc:
        return {'ok': False, 'http': 0, 'bytes': 0, 'seconds': round(time.monotonic() - start, 3),
                'bytes_per_second': 0, 'error': str(exc)[:160]}


def notify(title: str, body: str, urgent: bool = False):
    """桌面通知"""
    try:
        args = ['notify-send']
        if urgent:
            args.extend(['-u', 'critical'])
        args.extend([title, body])
        subprocess.run(args, timeout=5, capture_output=True)
    except Exception:
        pass  # 忽略通知失败


def get_proxy_port() -> int:
    """获取代理端口（从 service_core.json 读取）"""
    try:
        core_json = Path.home() / '.local/share/com.nebula.karing/service_core.json'
        if core_json.exists():
            cfg = json.loads(core_json.read_text())

            # 查找 mixed_in_rule 端口（规则代理）
            for inbound in cfg.get('inbounds', []):
                if inbound.get('tag') == 'mixed_in_rule' and inbound.get('type') == 'mixed':
                    port = inbound.get('listen_port')
                    if isinstance(port, int) and 1 <= port <= 65535:
                        return port

            # 备选：mixed_in_proxy
            for inbound in cfg.get('inbounds', []):
                if inbound.get('tag') == 'mixed_in_proxy' and inbound.get('type') == 'mixed':
                    port = inbound.get('listen_port')
                    if isinstance(port, int) and 1 <= port <= 65535:
                        return port
    except Exception:
        pass

    return 7890  # 默认端口


def get_current_node(group: str = MONITOR_GROUP) -> str | None:
    """获取当前使用的节点"""
    try:
        data = api_request(f'/proxies/{urllib.parse.quote(group, safe="")}')
        return data.get('now')
    except Exception:
        return None


def is_valid_proxy_node(name: str) -> bool:
    """检查是否为有效的代理节点"""
    if not isinstance(name, str) or not name:
        return False
    exclude = ['剩余流量', '到期时间', '套餐', '更新时间', '官网地址',
               'DIRECT', 'REJECT', 'PASS', 'GLOBAL']
    name_lower = name.lower()
    return not any(pat.lower() in name_lower for pat in exclude)


def close_idle_connections(node_name: str, log) -> int:
    """断开指定节点的闲置连接（流量 < 1 KB/s）"""
    try:
        data = api_request('/connections')
        connections = data.get('connections', [])

        closed = 0
        for conn in connections:
            chains = conn.get('chains', [])
            if node_name in chains:
                upload = conn.get('upload', 0)
                download = conn.get('download', 0)

                if upload < 1024 and download < 1024:
                    try:
                        api_request(f'/connections/{conn.get("id")}', method='DELETE')
                        closed += 1
                    except Exception:
                        pass  # 忽略单个连接失败

        return closed
    except Exception as e:
        log.warning(f'Failed to close idle connections: {e}')
        return 0


def trigger_urltest(group: str = MONITOR_GROUP, log=None):
    """触发 URLTest 立即进行延迟测试"""
    try:
        api_request(
            f'/group/{urllib.parse.quote(group, safe="")}/delay'
            f'?timeout=5000&url=https://www.gstatic.com/generate_204',
            timeout=10.0
        )
        if log:
            log.info('🔄 Triggered URLTest immediate health check')
    except Exception as e:
        if log:
            log.info(f'URLTest trigger response: {str(e)[:100]} (expected)')


class Watcher:
    """网络监控主类"""

    def __init__(self, notifications=True):
        self.log = setup_logging()
        self.notifications = notifications

        # 健康状态跟踪
        self.health = {
            'api': Health('API'),
            'proxy': Health('代理'),
            'direct': Health('直连')
        }

        # 时间戳
        self.last_probe = -float('inf')
        self.last_throughput = -float('inf')
        self.last_slow_notify = -float('inf')

        # 状态
        self.current_node = None
        self.proxy_port = get_proxy_port()
        self.slow_node_streak = 0
        self.probes = {}
        self.throughput = None

        # 性能追踪
        if PERF_TRACKING:
            self.tracker = NodePerformanceTracker()
            self.log.info('Performance tracking enabled')
        else:
            self.tracker = None
            self.log.info('Performance tracking disabled (node_tracker not available)')

    def transition(self, name: str, ok: bool, detail: str):
        """处理健康状态转换"""
        event = self.health[name].note(ok)
        if event:
            self.log.warning(f'{name} {event}: {detail}')

            if self.notifications:
                title = 'Karing 线路状态'
                body = f'{name}: {"恢复" if event == "recovered" else "持续异常"}；{detail}'
                notify(title, body, urgent=(event == 'down'))

    def auto_switch_slow_node(self, node_name: str):
        """自动切换慢速节点"""
        self.log.warning(f'🚨 Auto-switch triggered: node={node_name}, streak={self.slow_node_streak}')

        try:
            # 步骤 1：断开慢速节点的闲置连接
            closed = close_idle_connections(node_name, self.log)
            self.log.info(f'✂️ Closed {closed} idle connections on slow node')

            # 步骤 2：触发 URLTest 立即测试
            trigger_urltest(MONITOR_GROUP, self.log)

            # 步骤 3：等待 URLTest 切换（30 秒）
            time.sleep(30)

            # 步骤 4：验证切换结果
            new_node = get_current_node(MONITOR_GROUP)

            if new_node and new_node != node_name:
                # 切换成功
                self.log.info(f'✅ Auto-switch successful: {node_name} → {new_node}')
                msg = f'已自动切换到 {new_node}\n（从慢速节点 {node_name}）'
                if self.notifications:
                    notify('Karing 自动优化', msg)
                self.slow_node_streak = 0  # 重置计数器
            else:
                # 未能切换 - 静默重试
                self.log.warning(f'⚠️ Auto-switch attempt failed: still on {node_name}, will retry')
                # 不重置 streak，下次继续尝试

        except Exception as e:
            self.log.error(f'Auto-switch error: {e}')

    def step(self):
        """执行一次监控循环"""
        now = time.monotonic()

        # API 健康检查
        try:
            self.current_node = get_current_node(MONITOR_GROUP)
            self.transition('api', True, 'Clash API 可用')
        except Exception as e:
            self.transition('api', False, str(e))
            return  # API 不可用，跳过本轮

        # HTTP 探测（每 30 秒）
        if now - self.last_probe >= PROBE_INTERVAL:
            self.last_probe = now

            try:
                self.probes = {
                    'proxy': http_probe('https://www.gstatic.com/generate_204',
                                       self.proxy_port, (204,)),
                    'direct': http_probe('https://www.baidu.com/', None)
                }

                for name, result in self.probes.items():
                    detail = result['error'] or f'HTTP 探测成功 ({result["seconds"]}s)'
                    self.transition(name, result['ok'], detail)

            except Exception as e:
                self.log.error(f'Probe error: {e}')

        # 吞吐量测试（每 120 秒）
        if (now - self.last_throughput >= THROUGHPUT_INTERVAL and
            self.current_node and is_valid_proxy_node(self.current_node) and
            self.health['proxy'].state == 'up'):

            self.last_throughput = now

            try:
                self.throughput = throughput_probe(THROUGHPUT_URL, self.proxy_port)
                bps = self.throughput.get('bytes_per_second', 0)

                if self.throughput['ok']:
                    self.log.info(f'✅ Throughput test passed: {bps / 1024:.1f} KB/s')
                    self.slow_node_streak = 0
                else:
                    self.log.warning(f'⚠️ Throughput test failed: {bps / 1024:.1f} KB/s '
                                    f'(threshold: {MIN_THROUGHPUT_BPS / 1024:.1f} KB/s)')
                    self.slow_node_streak += 1

                # 记录性能数据
                if self.tracker and self.probes.get('proxy'):
                    probe = self.probes['proxy']
                    self.tracker.record_sample(
                        self.current_node,
                        probe.get('seconds', 0),
                        bps,
                        self.throughput['ok']
                    )

                # 检测慢速节点 → 自动切换
                if (self.slow_node_streak >= SLOW_NODE_THRESHOLD and
                    now - self.last_slow_notify >= NOTIFY_COOLDOWN):
                    self.auto_switch_slow_node(self.current_node)
                    self.last_slow_notify = now

            except Exception as e:
                self.log.error(f'Throughput test error: {e}')

    def run(self):
        """主循环"""
        self.log.info('Karing tunnel watcher started (Linux v2.0)')
        self.log.info(f'Monitor group: {MONITOR_GROUP}')
        self.log.info(f'Proxy port: {self.proxy_port}')

        while True:
            try:
                self.step()
                time.sleep(INTERVAL)
            except KeyboardInterrupt:
                self.log.info('Stopped by user')
                break
            except Exception as e:
                self.log.error(f'Unexpected error: {e}', exc_info=True)
                time.sleep(INTERVAL)


def main():
    parser = argparse.ArgumentParser(description='Karing 网络监控 Linux 版')
    parser.add_argument('--no-notify', action='store_true', help='禁用桌面通知')
    args = parser.parse_args()

    watcher = Watcher(notifications=not args.no_notify)
    watcher.run()


if __name__ == '__main__':
    main()
