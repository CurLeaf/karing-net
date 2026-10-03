# Karing 网络监控系统

**智能节点性能追踪与通知系统**

---

## 快速开始

### 查看节点推荐
```bash
cd /Users/curleaf/Project/karing-net/mac
.vfox/sdks/python/bin/python3 node_stats.py
```

### 查看监控状态
```bash
.vfox/sdks/python/bin/python3 node_stats.py --summary
```

### 查看所有节点统计
```bash
.vfox/sdks/python/bin/python3 node_stats.py --all
```

---

## 系统概述

本系统监控 Karing 代理节点性能，在检测到慢速节点时主动通知用户，并提供基于历史数据的智能推荐。

### 核心功能

- ✅ **持续监控**：每 2 分钟测试节点延迟和吞吐量
- ✅ **性能数据库**：记录并分析长期性能数据
- ✅ **智能推荐**：基于延迟、吞吐量、成功率的综合评分
- ✅ **主动通知**：连续慢速时推送通知（15 分钟冷却）
- ✅ **命令行工具**：随时查看节点性能统计

---

## 架构

```
监控主循环 (tunnel_watch.py)
    ├─ 每 10s:  状态更新
    ├─ 每 30s:  HTTP 探测 → 记录延迟
    └─ 每 120s: 吞吐量测试 → 性能数据库 → 慢速检测
               ↓
        节点性能数据库 (node_tracker.py)
               ├─ 记录样本（保留最近 100 个）
               ├─ 计算评分（延迟 30% + 吞吐 50% + 成功率 20%）
               └─ 推荐节点
               ↓
        通知系统 (macOS/Linux)
               └─ 防骚扰策略（连续 3 次慢速，15 分钟冷却）
```

---

## 重要说明

### 为什么不自动切换？

Karing 使用 **URLTest 类型代理组**，由 Clash 内核根据延迟自动选择节点。这种类型的代理组**不支持通过 API 手动切换**，任何切换请求都会返回 HTTP 400 错误：

```
{"message":"Must be a Selector"}
```

因此本系统采用**监控 + 通知**策略：
- 持续监控性能
- 检测到问题时通知用户
- 用户在 Karing 应用中手动选择推荐节点

---

## 文件说明

### 核心文件

| 文件 | 说明 |
|------|------|
| `tunnel_watch.py` | 监控主服务 |
| `node_tracker.py` | 性能数据库 |
| `node_stats.py` | 命令行工具 |
| `karing_mac.py` | Karing API 封装 |

### 配置与数据

| 路径 | 说明 |
|------|------|
| `~/Library/Application Support/karing-net/tunnel-watch.json` | 监控状态 |
| `~/Library/Application Support/karing-net/node_performance.json` | 性能数据库 |
| `~/Library/Application Support/karing-net/tunnel-watch.log` | 运行日志 |

### 文档

| 文件 | 说明 |
|------|------|
| `README.md` | 本文档 |
| `KNOWLEDGE_BASE.md` | 技术知识沉淀（跨平台复用） |
| `docs/` | 历史诊断和完成报告 |

---

## 服务管理

### 查看服务状态
```bash
ps aux | grep tunnel_watch
```

### 重启服务
```bash
launchctl stop gui/501/com.karing.net.watch
launchctl start gui/501/com.karing.net.watch
```

### 查看实时日志
```bash
tail -f ~/Library/Application\ Support/karing-net/tunnel-watch.log
```

---

## 配置调优

编辑 `tunnel_watch.py` 顶部常量：

```python
INTERVAL = 10                 # 主循环间隔（秒）
PROBE_INTERVAL = 30           # HTTP 探测间隔（秒）
THROUGHPUT_INTERVAL = 120     # 吞吐量测试间隔（秒）
MIN_THROUGHPUT_BPS = 256*1024 # 最低吞吐量（256 KB/s）
SLOW_NODE_THRESHOLD = 3       # 慢速阈值（连续次数）
NOTIFY_COOLDOWN = 900         # 通知冷却期（15 分钟）
```

**场景建议：**
- **高响应场景**（游戏）：`SLOW_NODE_THRESHOLD = 2`, `NOTIFY_COOLDOWN = 300`
- **普通场景**（默认）：保持当前配置
- **低干扰场景**：`SLOW_NODE_THRESHOLD = 5`, `NOTIFY_COOLDOWN = 1800`

---

## 故障排查

### 通知不显示

检查 macOS 通知权限：
```
系统设置 → 通知 → 终端 → 允许通知
```

测试：
```bash
osascript -e 'display notification "测试" with title "Karing"'
```

### 性能数据不更新

检查监控进程：
```bash
ps aux | grep tunnel_watch | grep -v grep
```

如果没有运行：
```bash
launchctl start gui/501/com.karing.net.watch
```

### API 连接失败

检查 Karing 是否运行：
```bash
curl http://127.0.0.1:9090/proxies
```

---

## 跨平台支持

本系统设计为跨平台，核心逻辑（`node_tracker.py`）无平台依赖。

### Linux 适配

需要修改：
1. **通知**：使用 `notify-send` 替代 `osascript`
2. **配置路径**：`~/.config/karing/` 或 `~/.config/clash/`
3. **日志路径**：`~/.local/share/karing-net/`

### Windows 适配

需要修改：
1. **通知**：使用 `win10toast` 库
2. **配置路径**：`%APPDATA%\karing\`
3. **服务管理**：使用 Windows Task Scheduler

详见 [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md) 第 6 节。

---

## 贡献与反馈

### 报告问题

请包含：
1. 操作系统版本
2. Karing 版本
3. 日志片段（最后 50 行）
4. `node_stats.py --summary` 输出

### 功能建议

欢迎提交：
- 新的评分算法
- Web Dashboard 实现
- 其他平台适配

---

## 许可证

本项目为 karing-net 的一部分。

---

**最后更新**: 2026-10-04  
**版本**: v2.0
