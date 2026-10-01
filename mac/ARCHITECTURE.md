# Karing macOS 配置与优化架构文档

**版本**: 2026-10-01  
**环境**: macOS, Python 3.12.14, Karing/sing-box 1.13.19.2802  
**代码行数**: 约 1,551 行 Python (不含测试)

---

## 一、系统概述

这是一套针对 macOS 平台的 Karing VPN 优化工具集，通过 Clash API 实现自动化分流管理、连接回收和线路监控，解决以下核心问题：

### 核心问题
1. **Chrome 系统代理检索受限**：未配置强制直连的域名（如 Google）会被错误路由到海外节点并被拒绝
2. **节点切换后连接堆积**：旧节点上的空闲连接不会自动清理，导致连接数从 10+ 堆到 60-90
3. **配置被 App 覆盖**：手工修改 `service_core.json` 会被 Karing App 重写回去
4. **节点故障无感知**：代理节点失效时没有自动切换机制

### 解决方案架构
```
┌─────────────────────────────────────────────────────────────┐
│                      Karing Desktop App                      │
│  ┌────────────────┐  ┌─────────────┐  ┌─────────────────┐  │
│  │  订阅/分流配置  │  │ sing-box核心 │  │ Clash HTTP API  │  │
│  └────────┬───────┘  └──────┬──────┘  └────────┬────────┘  │
└───────────┼──────────────────┼──────────────────┼───────────┘
            │                  │                  │
            │ 1.读取/合并      │                  │ 2.读取状态
            │                  │                  │ 3.控制操作
            ▼                  │                  ▼
  ┌──────────────────┐         │      ┌────────────────────────┐
  │  sync_rules.py   │         │      │   后台服务 (launchd)    │
  │  分流规则同步     │         │      │  ┌──────────────────┐  │
  │  - Apple 直连    │         │      │  │ connection_gc.py │  │
  │  - 强制直连       │         │      │  │  连接回收 (8秒)   │  │
  │  - AI 分组       │         │      │  └──────────────────┘  │
  └──────────────────┘         │      │  ┌──────────────────┐  │
                               │      │  │ tunnel_watch.py  │  │
  ┌──────────────────┐         │      │  │  线路监控 (10秒) │  │
  │   selfcheck.py   │         │      │  │  故障切换 (可选) │  │
  │  验收与健康检查   │◄────────┘      │  └──────────────────┘  │
  └──────────────────┘                └────────────────────────┘
            │                                      │
            ▼                                      ▼
  ┌──────────────────────────────────────────────────┐
  │          状态文件 (~/Library/...)                 │
  │  - connection-gc.json   (回收状态)               │
  │  - tunnel-watch.json    (线路健康)               │
  │  - network-snapshot.json (故障快照)              │
  └──────────────────────────────────────────────────┘
```

---

## 二、核心模块

### 2.1 karing_mac.py - 运行时基础库

**职责**: 提供所有脚本共用的底层能力

**关键功能**:
- **Clash API 通信**: 直连 `127.0.0.1:<port>` 绕过系统代理，带认证 Token
- **数据安全**: 原子写入 JSON (rename)，权限 0600，认证密钥不写日志
- **单实例锁**: 基于 `flock` 防止重复运行，避免冲突和资源浪费
- **日志轮转**: 每文件 2 MiB，保留 3 个备份
- **优雅停机**: 信号处理 (SIGTERM/SIGINT) + 退避重试

**核心 API**:
```python
load_json(path)              # 读取并验证 JSON
atomic_json(path, data)      # 原子写入，rename 保证一致性
api_request(path, method)    # Clash API 请求，带认证
connections()                # 获取所有连接，验证完整性
GroupReader().read()         # 读取代理组信息，缓存 5 分钟
selected_nodes(groups)       # 解析嵌套 selector 的最终节点
InstanceLock(name)           # 单实例锁，context manager
```

**配置路径**:
- `KARING_DIR`: Karing 数据目录 (默认 `~/Library/Group Containers/group.com.nebula.karing/`)
- `STATE_DIR`: 状态/日志目录 (默认 `~/Library/Application Support/karing-net/`)

---

### 2.2 sync_rules.py - 分流规则同步

**职责**: 合并托管规则，不破坏 Karing 其他配置

**管理的规则组** (优先级顺序):
1. **🍎 苹果服务**: Apple 官方域名 → `direct_out` 直连
2. **🏠 强制直连**: 用户自定义域名 (如 Google 搜索) → `direct_out`
3. **💬 OpenAI**: ChatGPT/API → `GPT自动` 组
4. **♊️ Google Gemini**: Gemini → `GPT自动` 组
5. **💬 Claude**: Anthropic → `GPT自动` 组

**配置源**:
- `config/karing_routing_group.apple.json`: Apple 域名模板
- `config/karing_subscribe_use.apple.json`: Apple 出站映射
- `config/direct.json`: **用户自定义强制直连域名** (Git 忽略)

**关键机制**:
```python
# 1. 读取 Karing 当前配置
routing = load_json(KARING_DIR / 'karing_routing_group.json')
use = load_json(KARING_DIR / 'karing_subscribe_use.json')

# 2. 合并 Apple + 强制直连域名
prepare(routing, use, direct_config())

# 3. 并发检测：写入前后验证未被 App 修改
if paths[i].read_bytes() != original[i]:
    raise RuntimeError('concurrent Karing write; sync aborted')
```

**命令**:
```bash
python3 sync_rules.py check      # 检查源文件和生成文件是否漂移
python3 sync_rules.py apply      # 应用规则（原子写入 + 完整备份）
python3 sync_rules.py restore --backup <目录>  # 恢复历史备份
```

**注意事项**:
- **不写 `service_core.json`**: 这是 App 的产物，直接修改会被覆盖
- **不调用 `/reload`**: macOS 版没有暴露可靠的 reload 接口
- **需要重连 Karing**: 修改源文件后让 App 重新生成配置

---

### 2.3 connection_gc.py - 连接垃圾回收

**职责**: 节点切换后回收旧节点上的空闲连接，保护活跃流量

**回收策略**:

| 连接类型 | 条件 | 回收时机 | 受预算限制 |
|---------|------|---------|-----------|
| **强制直连误路由** | 配置为直连但走了代理 | 立即 (1秒后) | ❌ 否 |
| **旧节点空闲** | 未选中节点 + 安静 24 秒 | 24 秒 | ✅ 是 (24条/轮) |
| **活跃流量** | 2KB/tick 以上传输 | 不回收 | - |
| **当前节点** | 任一组正在使用 | 不回收 | - |

**保护机制**:
```python
# 1. 协议保护: UDP、SSH(22/2222)、RDP(3389)
# 2. 入口保护: mixed_in_proxy、mixed_in_direct
# 3. 出站保护: direct_out、block_out、dns_*
# 4. 进程保护: ssh、mosh-client

def protected(c: dict) -> bool:
    meta = c.get('metadata') or {}
    return (meta.get('network') != 'tcp'
            or str(meta.get('destinationPort')) in {'22', '2222', '3389'}
            or 'mixed_in_proxy' in meta.get('type', '')
            or meta.get('protocol') == 'ssh')
```

**空闲判定** (解决心跳与真空闲的误判):
- 采样间隔 8 秒，连续 3 个采样 (24 秒) 低于 2KB/tick
- 上传/下载增量独立计算，HTTP/2 心跳也算在内
- 只在**同一节点选择**下连续计时，切换重置

**删除复查**:
```python
# 采样后重新读取，验证：
# 1. 连接仍存在且 start/chains 未变
# 2. 分类结果仍相同 (节点未重新选中)
# 3. 流量计数未增长 (没有突发传输)
if still_eligible(original, fresh.get(cid), reason, fresh_groups, suffixes):
    api_request('/connections/' + quote(cid), 'DELETE')
```

**状态文件** (`connection-gc.json`):
```json
{
  "at": 1727856234.5,
  "ticks": 142,
  "selected": {"urltest_out": "香港-优化2", "GPT自动": "日本-GPT"},
  "connections": 18,
  "candidates": [{"id": "abc123", "reason": "quiet-old-node"}],
  "accepted_this_tick": ["abc123"],
  "totals": {
    "delete_accepted": 6,
    "confirmed_absent": 6,
    "delete_failed": 0,
    "api_errors": 0
  }
}
```

**命令**:
```bash
# 单次采样（不回收）
python3 connection_gc.py --dry-run

# 持续运行（真实回收）
python3 connection_gc.py --apply --loop

# 测试 40 秒后停止
python3 connection_gc.py --dry-run --loop --duration 40
```

---

### 2.4 tunnel_watch.py - 线路监控与自动切换

**职责**: 持续监控 API、代理、直连三条路径的健康状态，故障时自动切换节点

**监控周期**:
- **基础采样**: 10 秒 (API 可用性、节点选择)
- **HTTP 探测**: 30 秒 (代理和直连入口)
- **吞吐测试**: 120 秒 (256 KiB 下载，检测限速)
- **规则审计**: 120 秒 (检测配置漂移)

**探测目标**:

| 路径 | 主目标 | 备用目标 | 期望状态 |
|-----|-------|---------|---------|
| **代理** | gstatic.com | cloudflare.com | 204 |
| **直连** | baidu.com | apple.com | 200/301/302 |

**健康判定** (迟滞逻辑，避免抖动):
- **Down**: 连续 **3 次**失败
- **Up**: 连续 **2 次**成功
- 单次失败只记日志，不触发通知

**自动切换** (可通过 `KARING_AUTO_SWITCH=0` 关闭):
```python
# 触发条件：连续 3 个周期
# 1. 吞吐不足 (<256 KB/s)
# 2. 主、备用探测均失败

if proxy_degraded_streak >= 3:
    candidate = next_proxy_candidate(group, failed_nodes, now)
    select_proxy('urltest_out', candidate)
    failed_nodes[current] = now  # 30分钟内不再尝试
```

**保护机制**:
- **切换冷却**: 15 分钟
- **失败节点冷却**: 30 分钟
- **节点更换后重置**: 吞吐样本、故障计数清零

**macOS 通知**:
```python
notify('Karing 自动切换节点', f'{current} -> {candidate}；吞吐不足')
notify('Karing 线路状态', f'proxy: 持续异常；主、备用探测目标均异常')
```

**网络快照** (故障时自动保存):
```bash
netstat -rn       # 路由表
scutil --dns      # DNS 配置
scutil --proxy    # 系统代理配置
```

**状态文件** (`tunnel-watch.json`):
```json
{
  "at": 1727856234.5,
  "ticks": 425,
  "selected": {"urltest_out": "香港-优化2"},
  "health": {
    "api": {"state": "up", "failures": 0, "successes": 10},
    "proxy": {"state": "up", "failures": 0, "successes": 5},
    "direct": {"state": "up", "failures": 0, "successes": 5}
  },
  "probes": {
    "proxy": {"ok": true, "http": 204, "seconds": 0.152},
    "direct": {"ok": true, "http": 200, "seconds": 0.083}
  },
  "auto_switch": {
    "enabled": true,
    "last": {"from": "日本-1", "to": "香港-优化2", "reason": "吞吐不足"},
    "streak": 0
  }
}
```

---

### 2.5 selfcheck.py - 健康检查与验收

**职责**: 只读检查，用于部署验收和日常诊断

**检查维度**:

1. **配置一致性**:
   - 源文件 (`karing_routing_group.json`, `karing_subscribe_use.json`)
   - 生成文件 (`service_core.json` 中的路由规则)
   - 优先级顺序 (Apple 必须在国外穿墙之前)

2. **Clash API**:
   - `/version` 可访问
   - `/connections` 可读取
   - 活跃组有选中节点 (闲置组标记 WARN)

3. **DNS 解析** (`--network`):
   - Apple 域名返回真实 IP (非 Fake-IP 198.18.x.x)
   - 4 个域名 × 20 次 = 80 次采样

4. **受控分流** (`--network`):
   - 创建 CONNECT 连接，通过 `sourcePort` 关联 API 记录
   - 验证 `chains` 路径符合规则

5. **后台服务** (`--services`):
   - launchd 状态
   - PID、运行时长、最近心跳

**命令**:
```bash
python3 selfcheck.py                     # 基础检查
python3 selfcheck.py --network           # 含网络探测 (~70秒)
python3 selfcheck.py --services          # 含后台服务状态
python3 selfcheck.py --network --services --repetitions 3  # 重复探测
```

**输出示例**:
```
PASS source and generated rules: {"source_ok": true, ...}
PASS Clash API: {"version": "sing-box 1.13.19", "connections": 18}
PASS active group selection: {"missing": []}
WARN unused group selection: {"not_verified": ["备用组"]}
PASS Apple DNS: {"apps.apple.com": ["17.253.x.x"], ...}
PASS route live api.openai.com: {"ok": true, "chains": ["mixed_in_rule", "GPT自动", ...]}
```

---

### 2.6 manage_services.py - LaunchAgent 管理

**职责**: 安装/停止/查询两个后台服务

**服务定义**:

| 名称 | Label | 命令 | 说明 |
|-----|-------|------|------|
| **gc** | com.karing.net.gc | `connection_gc.py --apply --loop` | 连接回收 |
| **watch** | com.karing.net.watch | `tunnel_watch.py` | 线路监控 |

**plist 生成** (动态，无需手工编辑):
```python
{
  'Label': 'com.karing.net.gc',
  'ProgramArguments': [python, script, *args],
  'WorkingDirectory': ROOT,
  'RunAtLoad': True,          # 登录后启动
  'KeepAlive': True,          # 崩溃自动拉起
  'ThrottleInterval': 15,     # 15秒启动节流
  'ExitTimeOut': 15,          # 15秒优雅停机
  'StandardOutPath': '/dev/null',  # 日志由脚本轮转
}
```

**命令**:
```bash
python3 manage_services.py install   # 安装/更新两个服务
python3 manage_services.py status    # 查询运行状态
python3 manage_services.py stop      # 停止两个服务
```

**安装流程**:
1. 验证 Python >= 3.12
2. 停止旧服务 (bootout + 20秒等待)
3. 生成 plist 并验证 (`plutil -lint`)
4. 启用 (`launchctl enable`)
5. 加载 (`launchctl bootstrap`，最多重试 8 次)

---

## 三、配置文件

### 3.1 目录结构

```
mac/
├── config/
│   ├── karing_routing_group.apple.json   # Apple 域名模板
│   ├── karing_subscribe_use.apple.json   # Apple 出站映射
│   ├── direct.example.json               # 强制直连示例
│   └── direct.json                       # 实际配置 (Git 忽略)
├── *.py                                  # 核心脚本
├── tests/                                # 单元测试
└── launchd/
    └── README.md

~/Library/Group Containers/group.com.nebula.karing/
├── karing_routing_group.json             # Karing 源配置
├── karing_subscribe_use.json             # Karing 源配置
├── service_core.json                     # sing-box 生成配置
└── service.json                          # API 端口和密钥

~/Library/Application Support/karing-net/
├── backups/                              # 配置备份 (最多 20 份)
├── connection-gc.log                     # 回收日志
├── tunnel-watch.log                      # 监控日志
├── connection-gc.json                    # 回收状态
├── tunnel-watch.json                     # 监控状态
└── network-snapshot.json                 # 故障快照
```

### 3.2 关键配置

**`config/direct.json`** (必须手工创建):
```json
{
  "domain_suffix": [
    "google.com",
    "google.com.hk",
    "googleapis.com",
    "googleusercontent.com",
    "gstatic.com",
    "bing.com",
    "microsoft.com",
    "你的内部域名.com"
  ]
}
```

**用途**: 解决 Chrome 检索受限问题，强制这些域名走 `direct_out` 直连。

---

## 四、工作流程

### 4.1 首次部署

```bash
cd /Users/curleaf/Project/karing-net/mac

# 1. 创建强制直连配置
cat > config/direct.json << 'EOF'
{
  "domain_suffix": [
    "google.com",
    "googleapis.com",
    "gstatic.com",
    "内部域名.com"
  ]
}
EOF

# 2. 应用分流规则
.vfox/sdks/python/bin/python3 sync_rules.py apply
.vfox/sdks/python/bin/python3 sync_rules.py check

# 3. 重新连接 Karing (让配置生效)

# 4. 验收
.vfox/sdks/python/bin/python3 selfcheck.py --network --services

# 5. 安装后台服务
.vfox/sdks/python/bin/python3 manage_services.py install

# 6. 确认服务运行
.vfox/sdks/python/bin/python3 manage_services.py status
```

### 4.2 日常维护

**查看状态**:
```bash
tail -f ~/Library/Application\ Support/karing-net/tunnel-watch.log
tail -f ~/Library/Application\ Support/karing-net/connection-gc.log

# 快速检查
python3 selfcheck.py --services
```

**修改强制直连域名**:
```bash
# 编辑 config/direct.json
python3 sync_rules.py apply
# 重新连接 Karing
```

**更新脚本**:
```bash
git pull
python3 manage_services.py install  # 自动重启服务
```

**故障排查**:
```bash
# 查看最近事件
grep -E "EVENT|DOWN|UP|switch" ~/Library/Application\ Support/karing-net/tunnel-watch.log | tail -30

# 查看回收统计
jq '.totals' ~/Library/Application\ Support/karing-net/connection-gc.json

# 查看网络快照 (故障时自动保存)
cat ~/Library/Application\ Support/karing-net/network-snapshot.json
```

### 4.3 测试

```bash
cd /Users/curleaf/Project/karing-net/mac

# 运行所有单元测试 (36 项)
python3 -m unittest discover -s tests -v

# 单次采样观察 (不回收)
python3 connection_gc.py --dry-run --loop --duration 40

# 监控一次性检查
python3 tunnel_watch.py --once --no-notify
```

---

## 五、关键设计决策

### 5.1 为什么不直接修改 `service_core.json`

**问题**: `service_core.json` 是 Karing App 的**产物**，不是数据源。

App 会在以下时机重写整个文件：
- 桌面端连接/重连
- 订阅更新
- 设置修改

手写的规则会在下次重写时消失。

**解决方案**: 只修改**源文件** (`karing_routing_group.json`, `karing_subscribe_use.json`)，让 App 重新生成配置。

### 5.2 为什么需要 24 秒空闲判定

**问题**: 
- HTTP/2 心跳 (几百字节/分钟) 与真空闲难以区分
- 旧版按累计字节判定，导致传输过 1MB 的连接永远不回收

**解决方案**:
- 采样**增量** (当前字节 - 上次字节) / 时间间隔
- 连续 3 个采样 (24秒) 低于 2KB/tick 才回收
- 切换节点后重新计时

### 5.3 为什么用删除复查

**问题**: 
- 采样到删除之间有 8+ 秒延迟
- 节点可能重新选中
- 连接可能开始传输

**解决方案**:
重新读取 `/connections`，验证：
1. 连接仍存在且 `start`/`chains` 未变
2. 分类结果仍相同
3. 流量计数未增长

### 5.4 为什么不用 Linux 的 `reconcile` 机制

**差异**:
- Linux: 有 `inotify`、`ss -K` 和已知的 `/reload` 接口
- macOS: 没有可靠的文件监控和 reload 接口

**macOS 策略**:
- 只修改源文件
- 不尝试 reload
- 需要手动重连 Karing

### 5.5 自动切换的权衡

**默认启用** (`KARING_AUTO_SWITCH=1`):
- 适合：办公、浏览、下载等场景
- 风险：切换可能中断长连接 (如 SSH)

**手动禁用** (`export KARING_AUTO_SWITCH=0`):
- 适合：需要稳定连接的场景
- 代价：节点故障时需要手动切换

---

## 六、性能与资源

**资源占用** (2026-09-25 验收数据):
- **内存**: 每进程 ~25 MiB RSS
- **CPU**: 采样时 0.0% (8秒/10秒周期)
- **采样延迟**: ~19 ms (connection_gc)
- **日志**: 2 MiB × 4 个文件 (含备份)

**API 开销**:
- `/connections`: ~18 KB (每 8/10 秒)
- `/proxies/<name>`: ~246-1301 字节 (按需)
- `/proxies`: ~11 KB (每 5 分钟重新发现)

**磁盘写入**:
- 状态文件: 仅当内容变化时写入 (序列化比较)
- 日志: 每行 flush，按 2 MiB 轮转

---

## 七、已知限制

1. **macOS 特定**:
   - 无 `inotify`，无法监听配置变更
   - 无可靠的 `/reload` 接口
   - 需要手动重连 Karing

2. **流量判定**:
   - 低于 256 B/s 的持续小流量可能被视为心跳
   - 无法保证所有第三方长轮询永不重连

3. **用户会话**:
   - LaunchAgent 仅在登录后运行
   - 退出登录后服务停止

4. **删除时机**:
   - 强制直连误路由: 立即回收 (可能中断该连接)
   - 其他连接: 24 秒后回收 (期间连接仍占用资源)

5. **验收边界**:
   - 已完成功能和短时验收
   - **尚未完成 24 小时连续运行验收**
   - 长期负载、整段 SSH/下载/AI 流连续性待实机验证

---

## 八、故障排查

### 问题: Chrome 搜索受限

**症状**: `ERR_CONNECTION_CLOSED`，Safari 正常

**原因**: 缺少 `config/direct.json`，Google 域名走了代理节点被拒

**解决**:
```bash
cat > config/direct.json << 'EOF'
{"domain_suffix": ["google.com", "googleapis.com", "gstatic.com"]}
EOF
python3 sync_rules.py apply
# 重新连接 Karing
```

### 问题: 连接数持续增长

**症状**: `/connections` 从 10+ 涨到 60-90

**原因**: `connection_gc` 服务未运行或采样失败

**排查**:
```bash
python3 manage_services.py status
tail -f ~/Library/Application\ Support/karing-net/connection-gc.log
jq '.totals' ~/Library/Application\ Support/karing-net/connection-gc.json
```

### 问题: 节点故障未自动切换

**检查**:
```bash
# 1. 自动切换是否启用
echo $KARING_AUTO_SWITCH  # 应该是 1 或空

# 2. 查看监控状态
jq '.auto_switch' ~/Library/Application\ Support/karing-net/tunnel-watch.json

# 3. 查看日志
grep "auto switch" ~/Library/Application\ Support/karing-net/tunnel-watch.log | tail -5
```

### 问题: 规则被覆盖

**检查**:
```bash
python3 sync_rules.py check
# 如果返回 source_drift 或 generated_drift，说明被覆盖了

# 重新应用
python3 sync_rules.py apply
# 重新连接 Karing
```

---

## 九、后续增强方向

1. **24 小时验收**: 长期运行稳定性
2. **配置热重载**: 探索 macOS 的 reload 接口
3. **流量分类优化**: 区分心跳、长轮询、下载
4. **通知优化**: 区分不同严重程度
5. **远程监控**: 暴露健康检查接口

---

## 十、参考

- **验收报告**: [ACCEPTANCE.md](ACCEPTANCE.md)
- **Chrome 故障案例**: [DOCKER_CHROME_DIAGNOSIS.md](DOCKER_CHROME_DIAGNOSIS.md)
- **部署说明**: [README.md](README.md)
- **LaunchAgent**: [launchd/README.md](launchd/README.md)

---

**最后更新**: 2026-10-01  
**维护者**: 基于代码和文档自动生成
