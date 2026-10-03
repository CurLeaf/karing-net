# karing-net

Karing 网络监控系统，跨平台支持 macOS 和 Linux。

## 项目结构

按平台独立组织，两个目录互不依赖：

- **[linux/](linux/)** - Linux 平台完整实现
  - 配置修正（karing-reconcile）
  - 连接回收（karing-gc）
  - 网络监控（tunnel-watch）
  - 完善的测试套件

- **[mac/](mac/)** - macOS 平台完整实现
  - 节点性能监控（tunnel_watch）
  - 性能数据库（node_tracker）
  - 命令行工具（node_stats）
  - launchd 服务集成

## 核心功能

### 共同特性
- ✅ Clash/Karing API 集成
- ✅ 节点性能追踪
- ✅ 智能通知系统
- ✅ 数据持久化

### Linux 特有
- ✅ 配置自动修正（SVCB 规则）
- ✅ 连接智能回收
- ✅ systemd 服务集成

### macOS 特有
- ✅ 节点性能评分算法
- ✅ 多维度推荐系统
- ✅ launchd 服务集成

## 快速开始

### macOS
```bash
cd mac
python3 node_stats.py           # 查看节点性能
tail -f ~/Library/Application\ Support/karing-net/tunnel-watch.log
```

### Linux
```bash
cd linux
python3 selfcheck.py            # 完整自检
systemctl --user status tunnel-watch.service
```

## 技术文档

- **[KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md)** - 跨平台技术知识沉淀
  - Clash API 代理组类型限制
  - 节点性能评估模型
  - 监控策略设计
  - 跨平台适配指南
  - 完整的测试与验证示例

- **[linux/README.md](linux/README.md)** - Linux 完整文档
- **[mac/README.md](mac/README.md)** - macOS 使用指南

## 架构设计

### 监控策略

**URLTest vs Selector 限制**：
- URLTest 代理组不支持 API 手动切换（返回 HTTP 400）
- 解决方案：监控 + 通知 + 推荐，保持用户控制权

### 节点性能评估

多维度评分（可配置权重）：
- 延迟（30%）- 影响交互体验
- 吞吐量（50%）- 影响下载速度  
- 成功率（20%）- 影响稳定性

### 通知策略

防止通知疲劳：
- 连续 3 次慢速才触发
- 15 分钟冷却期
- 提供推荐节点列表

## 系统要求

### macOS
- macOS 10.14+
- Python 3.8+
- Karing 或 Clash 客户端

### Linux  
- Python 3.8+
- systemd（服务管理）
- notify-send（通知）
- Karing 或 Clash 客户端

## 贡献

欢迎提交 Issue 和 Pull Request。

## License

MIT
