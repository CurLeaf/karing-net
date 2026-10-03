# Karing 网络监控系统 - 知识沉淀

**适用平台**: macOS, Linux  
**适用场景**: Clash/Karing 等基于 Clash API 的代理工具监控

---

## 核心知识点

### 1. Clash API 代理组类型限制

#### 关键发现

**Selector vs URLTest 的根本区别：**

| 类型 | 特性 | API 切换支持 | 使用场景 |
|------|------|-------------|---------|
| **Selector** | 手动选择 | ✅ 支持 `PUT /proxies/{group}` | 需要用户控制选择 |
| **URLTest** | 自动选择（延迟最低） | ❌ 返回 HTTP 400 | 完全自动化场景 |
| **Fallback** | 故障转移 | ❌ 不支持 | 高可用场景 |
| **LoadBalance** | 负载均衡 | ❌ 不支持 | 分散流量 |

**API 错误示例：**
```json
// 尝试切换 URLTest 组时
PUT /proxies/urltest_out
{"name": "香港HK-HY2"}

// 返回
HTTP 400
{"message":"Must be a Selector"}
```

**设计含义：**
- URLTest 由 Clash 内核自动管理，基于延迟测试选择
- 外部干预会破坏自动选择逻辑
- 如需手动控制，必须在配置中使用 Selector 类型

---

### 2. 监控策略：通知 vs 自动切换

#### 架构决策

**场景 A：可以自动切换（有 Selector 组）**
```python
def auto_switch_strategy():
    if detect_slow():
        candidate = find_best_node()
        api.select_proxy(group, candidate)  # ✅ 可行
        notify_user("已切换到 " + candidate)
```

**场景 B：不能自动切换（只有 URLTest）**
```python
def monitor_and_notify_strategy():
    if detect_slow(threshold=3):  # 连续 3 次
        recommendations = get_top_nodes(limit=3)
        notify_user("建议切换", recommendations)  # ✅ 替代方案
```

**权衡：**
- 自动切换：便捷，但可能与用户意图冲突
- 监控通知：需要手动操作，但保持用户控制权

---

### 3. 节点性能评估模型

#### 多维度评分

```python
class NodeScoring:
    """
    节点评分算法
    
    延迟权重 30%：影响交互体验
    吞吐权重 50%：影响下载速度
    成功率权重 20%：影响稳定性
    """
    
    def calculate_score(self, node_stats):
        # 延迟评分：< 100ms = 100 分，每增加 100ms 减 10 分
        latency_score = max(0, 100 - (node_stats.avg_latency - 0.1) * 100)
        
        # 吞吐量评分：> 2MB/s = 100 分，线性映射
        throughput_mbps = node_stats.avg_throughput / (1024 * 1024)
        throughput_score = min(100, (throughput_mbps / 2.0) * 100)
        
        # 成功率评分：直接映射
        success_score = node_stats.success_rate * 100
        
        # 加权计算
        return (
            0.3 * latency_score +
            0.5 * throughput_score +
            0.2 * success_score
        )
```

**参数调优指南：**
- **视频流媒体场景**：吞吐权重调至 60-70%
- **游戏场景**：延迟权重调至 50-60%
- **稳定性优先**：成功率权重调至 40%

---

### 4. 数据库设计

#### 时间序列数据管理

```python
class PerformanceDatabase:
    """
    节点性能时间序列数据库
    
    设计原则：
    1. 只保留最近 N 个样本（避免无限增长）
    2. 定期清理过期节点（> 30 天未见）
    3. 统计基于滑动窗口（最近 20 个样本）
    """
    
    SAMPLE_RETENTION = 100      # 每节点最多 100 样本
    NODE_EXPIRY_DAYS = 30       # 30 天未见则清理
    STATS_WINDOW = 20           # 统计窗口：最近 20 次
    
    def record_sample(self, node, latency, throughput, success):
        # 添加样本
        self.nodes[node]['samples'].append({
            'timestamp': time.time(),
            'latency': latency,
            'throughput': throughput,
            'success': success
        })
        
        # 滑动窗口：只保留最近 N 个
        if len(self.nodes[node]['samples']) > self.SAMPLE_RETENTION:
            self.nodes[node]['samples'] = \
                self.nodes[node]['samples'][-self.SAMPLE_RETENTION:]
```

**存储成本估算：**
- 每样本：~100 字节
- 100 样本/节点 × 50 节点 = ~5 MB
- 可接受范围

---

### 5. 通知策略

#### 防止通知疲劳

```python
class NotificationStrategy:
    """
    智能通知策略
    
    目标：及时但不骚扰
    """
    
    SLOW_THRESHOLD = 3          # 连续 3 次慢速才触发
    COOLDOWN_SECONDS = 900      # 15 分钟冷却期
    
    def should_notify(self, current_streak, last_notify_time):
        # 条件 1：达到阈值
        if current_streak < self.SLOW_THRESHOLD:
            return False
        
        # 条件 2：冷却期已过
        if time.time() - last_notify_time < self.COOLDOWN_SECONDS:
            return False
        
        return True
```

**参数建议：**
- **高响应场景**（交易、游戏）：阈值 = 2，冷却 = 5 分钟
- **普通场景**：阈值 = 3，冷却 = 15 分钟
- **低干扰场景**：阈值 = 5，冷却 = 30 分钟

---

### 6. 跨平台适配要点

#### macOS 特定实现

```python
# 通知
def notify_macos(title, body):
    script = '''
    on run argv
        display notification (item 2 of argv) with title (item 1 of argv)
    end run
    '''
    subprocess.run(['/usr/bin/osascript', '-e', script, title, body])

# 代理端口检测
def get_proxy_port_macos():
    # 读取 Clash 配置
    config = read_json('~/Library/Application Support/karing/config.json')
    return config['mixed-port']
```

#### Linux 适配

```python
# 通知
def notify_linux(title, body):
    subprocess.run(['notify-send', title, body])

# 代理端口检测
def get_proxy_port_linux():
    # 环境变量
    if 'CLASH_API_PORT' in os.environ:
        return int(os.environ['CLASH_API_PORT'])
    
    # 配置文件位置不同
    config = read_yaml('~/.config/clash/config.yaml')
    return config.get('mixed-port', 7890)
```

#### Windows 适配提示

```python
# 通知（需要 win10toast）
from win10toast import ToastNotifier
toaster = ToastNotifier()
toaster.show_toast(title, body, duration=10)

# 配置路径
config_path = os.path.expanduser('~\\AppData\\Roaming\\clash\\config.yaml')
```

---

### 7. 健康监控模式

#### 三层健康检查

```python
class HealthMonitor:
    """
    分层健康检查
    
    Layer 1: API 可用性
    Layer 2: 连通性（代理/直连）
    Layer 3: 性能（吞吐量）
    """
    
    def check_api_health(self):
        """Layer 1: Clash API 是否响应"""
        try:
            requests.get('http://127.0.0.1:9090/proxies', timeout=3)
            return True
        except:
            return False
    
    def check_connectivity(self, proxy_port):
        """Layer 2: 能否访问外网"""
        try:
            # 主探测
            probe_http('https://www.gstatic.com/generate_204', proxy_port)
            return True
        except:
            # 备用探测
            return probe_http('https://cp.cloudflare.com/generate_204', proxy_port)
    
    def check_throughput(self, proxy_port):
        """Layer 3: 吞吐量是否达标"""
        result = download_test('https://speed.cloudflare.com/__down?bytes=262144', 
                               proxy_port)
        return result['bytes_per_second'] >= 256 * 1024
```

**探测目标选择：**
- **主探测**：Google Gstatic（全球可达，稳定）
- **备用探测**：Cloudflare（CDN，快速）
- **国内直连**：Baidu 或 Apple（避免 GFW）

---

### 8. 配置模式识别

#### 自动检测代理组类型

```python
def detect_proxy_group_capabilities(api_base_url):
    """
    检测代理组是否支持手动切换
    
    返回：
    {
        'group_name': {
            'type': 'selector' | 'urltest' | 'fallback',
            'switchable': True | False,
            'members': ['node1', 'node2', ...]
        }
    }
    """
    groups = requests.get(f'{api_base_url}/proxies').json()
    
    result = {}
    for name, group in groups['proxies'].items():
        group_type = group.get('type', '').lower()
        result[name] = {
            'type': group_type,
            'switchable': group_type == 'selector',
            'members': group.get('all', [])
        }
    
    return result
```

**使用场景：**
- 启动时自动检测，决定使用哪种策略
- 避免盲目尝试切换导致错误

---

### 9. 测试与验证

#### 单元测试示例

```python
import unittest
from node_tracker import NodePerformanceTracker

class TestNodeTracker(unittest.TestCase):
    def test_scoring_algorithm(self):
        tracker = NodePerformanceTracker()
        
        # 模拟数据
        for _ in range(10):
            tracker.record_sample('test_node', 
                                 latency=0.1, 
                                 throughput=2*1024*1024, 
                                 success=True)
        
        stats = tracker.get_node_stats('test_node')
        
        # 验证：低延迟 + 高吞吐 = 高分
        self.assertGreater(stats['score'], 90)
    
    def test_recommendation_order(self):
        tracker = NodePerformanceTracker()
        
        # 快节点
        for _ in range(5):
            tracker.record_sample('fast', 0.05, 5*1024*1024, True)
        
        # 慢节点
        for _ in range(5):
            tracker.record_sample('slow', 0.5, 500*1024, True)
        
        recommendations = tracker.get_recommendations()
        
        # 快节点应该排在前面
        self.assertEqual(recommendations[0][0], 'fast')
```

---

### 10. 故障排查指南

#### 常见问题诊断

**问题 1：通知不显示**
```bash
# macOS: 检查通知权限
# 系统设置 → 通知 → 终端/Python → 允许通知

# 测试
osascript -e 'display notification "测试" with title "标题"'
```

**问题 2：API 连接失败**
```bash
# 检查 Clash 是否运行
ps aux | grep clash

# 检查 API 端口
lsof -i :9090

# 测试 API
curl http://127.0.0.1:9090/proxies
```

**问题 3：性能数据不更新**
```bash
# 检查监控进程
ps aux | grep tunnel_watch

# 查看日志
tail -f ~/Library/Application\ Support/karing-net/tunnel-watch.log

# 检查数据库
ls -lh ~/Library/Application\ Support/karing-net/node_performance.json
```

---

## 架构总览

```
┌─────────────────────────────────────────────────────────┐
│                    监控主循环                             │
│  (tunnel_watch.py)                                      │
│                                                         │
│  每 10s:  状态更新                                       │
│  每 30s:  HTTP 探测 → 记录延迟                           │
│  每 120s: 吞吐量测试 → 记录性能 → 检测慢速                │
└────────────────┬────────────────────────────────────────┘
                 │
                 ├──────→ node_tracker.py
                 │        (性能数据库)
                 │        ├─ 记录样本
                 │        ├─ 计算评分
                 │        └─ 推荐节点
                 │
                 ├──────→ 通知系统
                 │        (macOS/Linux/Windows)
                 │        └─ 防骚扰策略
                 │
                 └──────→ node_stats.py
                          (命令行工具)
                          └─ 可视化查看
```

---

## 配置参考

### 推荐配置（可调整）

```python
# tunnel_watch.py 顶部配置
INTERVAL = 10                 # 主循环间隔（秒）
PROBE_INTERVAL = 30           # HTTP 探测间隔（秒）
THROUGHPUT_INTERVAL = 120     # 吞吐量测试间隔（秒）
MIN_THROUGHPUT_BPS = 256*1024 # 最低吞吐量阈值（256 KB/s）
SLOW_NODE_THRESHOLD = 3       # 慢速检测阈值（次数）
NOTIFY_COOLDOWN = 900         # 通知冷却期（秒）

# node_tracker.py 配置
SAMPLE_RETENTION = 100        # 样本保留数量
NODE_EXPIRY_DAYS = 30         # 节点过期天数
STATS_WINDOW = 20             # 统计窗口大小

# 评分权重
LATENCY_WEIGHT = 0.3          # 延迟权重
THROUGHPUT_WEIGHT = 0.5       # 吞吐量权重
SUCCESS_RATE_WEIGHT = 0.2     # 成功率权重
```

---

## 复用清单

### 必需文件

1. **node_tracker.py** - 性能数据库（通用，无平台依赖）
2. **监控主循环** - 需适配平台（通知、配置路径）
3. **node_stats.py** - 查看工具（通用）

### 可选组件

- **Dashboard** - Web 可视化界面
- **告警集成** - Slack/Telegram/Email
- **Prometheus 导出器** - 监控系统集成

---

## 参考资料

- [Clash API 文档](https://clash.gitbook.io/doc/restful-api)
- [Clash 配置文件规范](https://github.com/Dreamacro/clash/wiki/configuration)
- Karing 项目地址（如有）

---

**知识版本**: v1.0  
**最后更新**: 2026-10-04  
**维护者**: karing-net 项目
