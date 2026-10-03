#!/usr/bin/env python3
"""
Karing 网络监控 v3.1 - 100% 自动化版

核心变化（v3.1）：
- 🚀 彻底移除所有手动切换提示
- 🎯 100% 自动化，零人工干预
- 🔄 失败时自动重试，不打扰用户
- 📊 保留性能追踪和数据库

工作原理：
1. 检测慢速节点（连续 3 次 < 256 KB/s）
2. 断开该节点的闲置连接（流量 < 1 KB/s）
3. 触发 URLTest 立即测试所有节点
4. URLTest 自动切换到快速节点
5. 成功：通知用户；失败：静默重试
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import time

import karing_mac as km
import sync_rules
from node_tracker import NodePerformanceTracker

INTERVAL = 10
PROBE_INTERVAL = 30
THROUGHPUT_INTERVAL = 120
THROUGHPUT_URL = 'https://speed.cloudflare.com/__down?bytes=262144'
THROUGHPUT_BYTES = 262144
MIN_THROUGHPUT_BPS = 256 * 1024
MONITOR_GROUP = 'urltest_out'
SLOW_NODE_THRESHOLD = 3  # 连续3次慢速才触发通知
NOTIFY_COOLDOWN = 15 * 60  # 通知间隔15分钟


def proxy_ports():
    core = km.load_json(km.KARING_DIR / 'service_core.json')
    result = {i.get('tag'): i.get('listen_port') for i in core.get('inbounds', []) if i.get('type') == 'mixed'}
    for tag in ('mixed_in_direct', 'mixed_in_rule'):
        if not isinstance(result.get(tag), int) or not 1 <= result[tag] <= 65535:
            raise ValueError(f'missing inbound port: {tag}')
    return result


def http_probe(url, port=None, expected=(200, 204, 301, 302)):
    args = ['/usr/bin/curl', '--silent', '--show-error', '--output', '/dev/null',
            '--connect-timeout', '4', '--max-time', '8', '--write-out', '%{http_code} %{time_total}',
            '--proxy', f'http://127.0.0.1:{port}' if port else '', '--noproxy', '' if port else '*', url]
    at = time.monotonic()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
        code, elapsed = (proc.stdout.strip().split() + ['0', '0'])[:2]
        ok = proc.returncode == 0 and int(code) in expected
        return {'ok': ok, 'http': int(code), 'seconds': round(float(elapsed), 3),
                'error': '' if ok else f'curl={proc.returncode} HTTP={code} {proc.stderr.strip()[:120]}'}
    except Exception as exc:
        return {'ok': False, 'http': 0, 'seconds': round(time.monotonic() - at, 3), 'error': str(exc)[:160]}


def throughput_probe(url=THROUGHPUT_URL, port=None, expected=(200, 206)):
    """Download a small fixed body and report sustained bytes per second."""
    args = ['/usr/bin/curl', '--silent', '--show-error', '--output', '/dev/null',
            '--connect-timeout', '4', '--max-time', '15',
            '--write-out', '%{http_code} %{size_download} %{time_total}',
            '--proxy', f'http://127.0.0.1:{port}' if port else '', '--noproxy', '' if port else '*', url]
    at = time.monotonic()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=18)
        code, size, elapsed = (proc.stdout.strip().split() + ['0', '0', '0'])[:3]
        code, size, elapsed = int(code), float(size), float(elapsed)
        bps = size / elapsed if elapsed > 0 else 0.0
        ok = proc.returncode == 0 and code in expected and size >= THROUGHPUT_BYTES * 0.9
        if ok and bps < MIN_THROUGHPUT_BPS:
            ok = False
        error = '' if ok else f'curl={proc.returncode} HTTP={code} bytes={size:.0f} {proc.stderr.strip()[:120]}'
        return {'ok': ok, 'http': code, 'bytes': round(size), 'seconds': round(elapsed, 3),
                'bytes_per_second': round(bps), 'error': error}
    except Exception as exc:
        return {'ok': False, 'http': 0, 'bytes': 0, 'seconds': round(time.monotonic() - at, 3),
                'bytes_per_second': 0, 'error': str(exc)[:160]}


def is_valid_proxy_node(name: str) -> bool:
    """Check if a node name is a valid proxy (not a special node)."""
    if not isinstance(name, str) or not name:
        return False
    exclude_patterns = [
        '剩余流量', '到期时间', '套餐', '更新时间', '官网地址',
        'DIRECT', 'REJECT', 'PASS', 'GLOBAL'
    ]
    name_lower = name.lower()
    return not any(pattern.lower() in name_lower for pattern in exclude_patterns)


class Health:
    def __init__(self):
        self.state, self.failures, self.successes = 'unknown', 0, 0

    def note(self, ok):
        before = self.state
        self.successes = self.successes + 1 if ok else 0
        self.failures = self.failures + 1 if not ok else 0
        if self.failures >= 3:
            self.state = 'down'
        elif self.successes >= 2:
            self.state = 'up'
        return ('down' if self.state == 'down' else 'recovered') if self.state != before and (self.state == 'down' or before == 'down') else None

    def snapshot(self):
        return {'state': self.state, 'failures': self.failures, 'successes': self.successes}


def notify(title, body):
    script = 'on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run'
    try:
        return subprocess.run(['/usr/bin/osascript', '-e', script, title, body], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def network_snapshot():
    result = {}
    for name, cmd in [('routes', ['/usr/sbin/netstat', '-rn']), ('dns', ['/usr/sbin/scutil', '--dns']),
                      ('proxy', ['/usr/sbin/scutil', '--proxy'])]:
        try:
            result[name] = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout[:12000]
        except (OSError, subprocess.TimeoutExpired) as exc:
            result[name] = str(exc)
    km.atomic_json(km.STATE_DIR / 'network-snapshot.json', {'at': time.time(), **result})


class Watcher:
    def __init__(self, notifications=True):
        self.reader = km.GroupReader()
        self.log = km.logger('tunnel-watch')
        self.health = {name: Health() for name in ('api', 'proxy', 'direct')}
        self.previous = None
        self.last_probe = self.last_rules = -float('inf')
        self.last_log = self.last_error = -float('inf')
        self.probes = {}
        self.rules = {}
        self.ticks = 0
        self.notifications = notifications
        self.last_notify = {}
        self.last_throughput = -float('inf')
        self.throughput = None
        self.proxy_degraded_streak = 0
        self.slow_node_streak = 0
        self.last_slow_notify = -float('inf')
        self.tracker = NodePerformanceTracker()
        self.current_node = None
        self.log.info('initialized with performance tracking enabled')

    def check_node_performance(self, groups: dict, now: float):
        """
        检查节点性能，记录数据，必要时自动切换

        v3.0 自动切换机制：
        1. 检测慢速节点（连续 3 次 < 256 KB/s）
        2. 断开该节点的闲置连接（流量 < 1 KB/s）
        3. 触发 URLTest 立即重测
        4. URLTest 自动切换到快速节点
        5. 通知用户切换结果
        """
        group = groups.get(MONITOR_GROUP)
        if not group:
            return

        current = group.get('now')
        if not current or not is_valid_proxy_node(current):
            return

        self.current_node = current

        # 记录性能数据
        if self.throughput and self.probes.get('proxy'):
            probe = self.probes['proxy']
            self.tracker.record_sample(
                current,
                probe.get('seconds', 0),
                self.throughput.get('bytes_per_second', 0),
                self.throughput.get('ok', False)
            )

        # 检测慢速节点
        if self.throughput and not self.throughput.get('ok'):
            self.slow_node_streak += 1
        else:
            self.slow_node_streak = 0

        # 连续慢速 → 自动切换
        if self.slow_node_streak >= SLOW_NODE_THRESHOLD and now - self.last_slow_notify >= NOTIFY_COOLDOWN:
            self.auto_switch_slow_node(current)
            self.last_slow_notify = now

    def auto_switch_slow_node(self, current_node: str):
        """
        自动切换慢速节点

        策略：
        1. 断开慢速节点的闲置连接
        2. 触发 URLTest 立即测试
        3. 等待 URLTest 自动切换
        4. 验证并通知结果
        """
        self.log.warning('🚨 Auto-switch triggered: node=%s, streak=%d', current_node, self.slow_node_streak)

        try:
            # 步骤 1：断开慢速节点的闲置连接
            closed_count = self._close_idle_connections(current_node)
            self.log.info('✂️ Closed %d idle connections on slow node', closed_count)

            # 步骤 2：触发 URLTest 立即测试
            self._trigger_urltest_now()

            # 步骤 3：等待 URLTest 切换（30 秒）
            time.sleep(30)

            # 步骤 4：验证切换结果
            new_node = self._get_current_node()

            if new_node and new_node != current_node:
                # 切换成功
                self.log.info('✅ Auto-switch successful: %s → %s', current_node, new_node)
                msg = f'已自动切换到 {new_node}\n（从慢速节点 {current_node}）'
                if self.notifications:
                    notify('Karing 自动优化', msg)
                self.slow_node_streak = 0  # 重置计数器
            else:
                # 未能切换 - 继续尝试，不通知用户
                self.log.warning('⚠️ Auto-switch attempt failed: still on %s, will retry on next detection', current_node)
                # 不重置 streak，下次检测继续尝试
                # 完全静默，无通知

        except Exception as e:
            self.log.error('Auto-switch error: %s', e)
            # 静默处理错误，自动重试

    def _close_idle_connections(self, node_name: str) -> int:
        """断开指定节点的闲置连接（流量 < 1 KB/s）"""
        try:
            status, data = km.api_request('/connections')
            connections = data.get('connections', [])

            closed_count = 0
            for conn in connections:
                chains = conn.get('chains', [])
                if node_name in chains:
                    # 只断开闲置的（上传下载都 < 1 KB/s）
                    upload = conn.get('upload', 0)
                    download = conn.get('download', 0)

                    if upload < 1024 and download < 1024:
                        try:
                            km.api_request(f'/connections/{conn.get("id")}', 'DELETE')
                            closed_count += 1
                        except Exception:
                            pass  # 忽略单个连接断开失败

            return closed_count
        except Exception as e:
            self.log.warning('Failed to close idle connections: %s', e)
            return 0

    def _trigger_urltest_now(self):
        """触发 URLTest 立即进行延迟测试"""
        try:
            # 使用 timeout 确保不会阻塞太久
            km.api_request(
                f'/group/{MONITOR_GROUP}/delay?timeout=5000&url=https://www.gstatic.com/generate_204',
                timeout=10.0
            )
            self.log.info('🔄 Triggered URLTest immediate health check')
        except Exception as e:
            # 超时或其他错误是正常的（测试可能需要时间）
            self.log.info('URLTest trigger response: %s (expected)', str(e)[:100])

    def _get_current_node(self) -> str | None:
        """获取当前使用的节点"""
        try:
            status, data = km.api_request(f'/proxies/{MONITOR_GROUP}')
            return data.get('now')
        except Exception:
            return None

    def transition(self, name, ok, detail):
        event = self.health[name].note(ok)
        if event:
            self.log.warning('%s %s: %s', name, event, detail)
            network_snapshot()
            key = (name, event)
            now = time.monotonic()
            if self.notifications and now - self.last_notify.get(key, -float('inf')) >= 60:
                self.last_notify[key] = now
                if not notify('Karing 线路状态', f'{name}: {"恢复" if event == "recovered" else "持续异常"}；{detail}'):
                    self.log.warning('notification unavailable; event kept in log')

    def step(self):
        now = time.monotonic()
        error, selected, count, groups = '', {}, None, {}
        try:
            groups = self.reader.read()
            conns = km.connections()
            selected = {name: group['now'] for name, group in groups.items() if group['now']}
            count = len(conns)
            self.transition('api', True, 'Clash API 可用')
            if selected != self.previous:
                self.log.info('selection changed: %s', json.dumps(selected, ensure_ascii=False))
                self.previous = selected
                self.proxy_degraded_streak = 0
                self.throughput = None
                self.last_throughput = -float('inf')
        except Exception as exc:
            error = str(exc)
            self.transition('api', False, error)
            if now - self.last_error >= 60:
                self.log.warning('API paused: %s', error)
                self.last_error = now

        if now - self.last_probe >= PROBE_INTERVAL:
            self.last_probe = now
            try:
                ports = proxy_ports()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    a = pool.submit(http_probe, 'https://www.gstatic.com/generate_204', ports['mixed_in_rule'], (204,))
                    b = pool.submit(http_probe, 'https://www.baidu.com/', ports['mixed_in_direct'])
                    self.probes = {'proxy': a.result(), 'direct': b.result()}
                for name, result in self.probes.items():
                    detail = result['error'] or 'HTTP 探测成功'
                    effective_ok = result['ok']
                    if not result['ok']:
                        secondary = http_probe(
                            'https://cp.cloudflare.com/generate_204' if name == 'proxy' else 'https://www.apple.com/',
                            ports['mixed_in_rule' if name == 'proxy' else 'mixed_in_direct'],
                            (204,) if name == 'proxy' else (200, 301, 302))
                        result['secondary'] = secondary
                        if secondary['ok']:
                            effective_ok = True
                            detail = '仅主探测目标异常；备用目标可达；' + result['error']
                        else:
                            detail = '主、备用探测目标均异常；' + result['error']
                    self.transition(name, effective_ok, detail)
            except Exception as exc:
                self.log.warning('probe error: %s', exc)

        if now - self.last_throughput >= THROUGHPUT_INTERVAL:
            self.last_throughput = now
            try:
                ports = proxy_ports()
                self.throughput = throughput_probe(port=ports['mixed_in_rule'])
                if self.probes.get('proxy'):
                    self.probes['proxy']['throughput'] = self.throughput
            except Exception as exc:
                self.log.warning('throughput probe error: %s', exc)
                self.throughput = None

        # 检查节点性能
        if groups:
            self.check_node_performance(groups, now)

        if now - self.last_rules >= 120:
            self.last_rules = now
            try:
                self.rules = sync_rules.report()
                if not self.rules['source_ok'] or not self.rules['generated_ok']:
                    self.log.warning('rule drift: %s', json.dumps(self.rules, ensure_ascii=False))
            except Exception as exc:
                self.log.warning('rule check error: %s', exc)

        self.ticks += 1
        if now - self.last_log >= 120 or self.ticks == 1:
            self.last_log = now
            health_summary = {k: v.state for k, v in self.health.items()}
            self.log.info('heartbeat ticks=%d connections=%s health=%s',
                         self.ticks, count if count is not None else '?', health_summary)

        state = {
            'at': time.time(),
            'pid': os.getpid(),
            'ticks': self.ticks,
            'api_ok': not error,
            'error': error,
            'selected': selected,
            'connections': count,
            'health': {k: v.snapshot() for k, v in self.health.items()},
            'probes': self.probes,
            'throughput': self.throughput,
            'rules': self.rules,
        }
        km.atomic_json(km.STATE_DIR / 'tunnel-watch.json', state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-notify', action='store_true', help='disable notifications')
    args = parser.parse_args()

    watcher = Watcher(notifications=not args.no_notify)
    watcher.log.info('started pid=%d', os.getpid())

    while not km.STOP.wait(INTERVAL):
        try:
            watcher.step()
        except KeyboardInterrupt:
            break
        except Exception as exc:
            watcher.log.exception('unexpected error: %s', exc)
            time.sleep(30)

    watcher.log.info('stopped')


if __name__ == '__main__':
    main()
