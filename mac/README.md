# Karing macOS 配置与优化系统

完整的 Karing VPN 客户端配置管理、连接优化和自动化监控方案。

---

## 🎯 核心功能

- ✅ **自动连接回收** - 节点切换后 24 秒自动清理旧连接
- ✅ **智能线路监控** - 实时健康检查，故障自动切换节点
- ✅ **配置规则同步** - 防止 Karing App 覆盖自定义配置
- ✅ **Chrome 检索优化** - 强制直连规则，避免搜索引擎被拦截
- ✅ **节点智能过滤** - 排除 "剩余流量" 等特殊节点，防止切换失败

---

## 📦 快速开始

### 1. 查看系统状态
```bash
python3 karing.py status
```

### 2. 运行测试验证
```bash
python3 karing.py test
```

### 3. 重启服务应用优化
```bash
python3 karing.py restart
```

### 4. 查看实时日志
```bash
python3 karing.py logs-live
```

---

## 📚 文档索引

### 快速参考
- **[karing.py](karing.py)** - 一键维护命令工具
- **[QUICK_START.md](QUICK_START.md)** - 10 分钟快速入门指南

### 深入阅读
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - 完整技术架构 (22 KB)
- **[OPTIMIZATION_REPORT.md](OPTIMIZATION_REPORT.md)** - 最新优化报告

### 工具脚本
- **[optimize.py](optimize.py)** - 全量优化与诊断工具
- **[fix_karing.py](fix_karing.py)** - 配置修复工具
- **[sync_rules.py](sync_rules.py)** - 规则同步脚本
- **[connection_gc.py](connection_gc.py)** - 连接垃圾回收
- **[tunnel_watch.py](tunnel_watch.py)** - 线路监控与自动切换
- **[manage_services.py](manage_services.py)** - 后台服务管理

---

## 🔧 核心组件

### 后台服务 (LaunchAgent)

#### 连接回收服务
```
服务名: com.karing.net.gc
作用: 节点切换 24 秒后自动清理旧连接
间隔: 每 2 分钟检查一次
```

#### 线路监控服务
```
服务名: com.karing.net.watch
作用: 实时健康检查，故障自动切换节点
间隔: 每 10 秒探测一次
```

### 配置文件

```
config/
├── direct.json                      # 强制直连域名 (Google, Bing 等)
├── karing_routing_group.apple.json  # 路由规则模板
└── karing_subscribe_use.apple.json  # 订阅配置模板
```

---

## 📊 系统状态

### 最近优化 (2026-10-01)

✅ **已修复**:
1. 自动切换 HTTP 400 错误 - 节点名称过滤不当
2. Chrome 检索受限 - 缺少直连配置
3. 节点选择逻辑 - 添加 `is_valid_proxy_node()` 过滤函数

✅ **已优化**:
1. 创建 `karing.py` 维护工具
2. 完善技术文档 (ARCHITECTURE.md, QUICK_START.md)
3. 添加优化报告 (OPTIMIZATION_REPORT.md)

### 当前运行状态

```bash
# 查看实时状态
python3 karing.py status

# 输出示例:
# ✅ 连接回收: 运行中 (累计回收 584 个连接)
# ✅ 线路监控: 运行中 (API: up, 代理: up, 直连: up)
# ✅ 自动切换: 已启用
```

---

## 🎯 常见任务

### 检查配置是否同步
```bash
python3 karing.py check
```

### 应用配置修复
```bash
python3 karing.py apply
# 如遇权限错误:
sudo python3 karing.py apply
```

### 重启后台服务
```bash
python3 karing.py restart
```

### 停止后台服务
```bash
python3 karing.py stop
```

### 查看日志
```bash
# 最近 50 行
python3 karing.py logs

# 实时监控
python3 karing.py logs-live
```

### 网络验收测试
```bash
python3 karing.py test-network
```

---

## 🔍 故障排查

### Chrome 仍然无法检索

1. **检查配置**:
```bash
python3 karing.py check
```

2. **应用修复**:
```bash
python3 karing.py apply
```

3. **重新连接 Karing**:
在 Karing App 中断开并重新连接

4. **验证直连规则**:
```bash
cat config/direct.json
```

### 自动切换失败 (HTTP 400)

**原因**: 尝试切换到 "剩余流量" 等特殊节点

**修复**: 已在 `tunnel_watch.py` 中添加节点过滤

**验证**:
```bash
python3 optimize_auto_switch.py
```

### 配置漂移警告

**原因**: macOS 沙箱阻止访问 Karing 数据目录

**解决方案**:

1. **添加终端权限** (推荐):
   - 系统设置 → 隐私与安全性 → 完全磁盘访问权限
   - 添加 "Terminal"
   - 重启终端

2. **使用 sudo** (临时):
```bash
sudo python3 karing.py apply
```

### 查看详细服务状态
```bash
python3 karing.py service-status
```

---

## 📈 性能指标

### 资源占用
- **内存**: ~25 MiB per process
- **CPU**: 空闲时 0.0%
- **采样延迟**: ~19 ms

### 回收效率
- **回收成功**: 584 次
- **回收失败**: 0 次
- **API 错误**: 58 次 (1% 错误率)

### 健康状态
- **API**: ✅ up
- **代理路径**: ✅ up
- **直连路径**: ✅ up

---

## 🛠️ 开发与测试

### 运行单元测试
```bash
python3 karing.py test
```

### 测试覆盖
- ✅ 36 项单元测试
- ✅ 连接回收逻辑
- ✅ 规则同步机制
- ✅ 健康检查状态机
- ✅ 节点选择算法

### 代码质量
```bash
# 运行所有测试
python3 -m unittest discover -s tests -v

# 特定模块测试
python3 -m unittest tests.test_karing_mac
python3 -m unittest tests.test_sync_rules
python3 -m unittest tests.test_connection_gc
```

---

## 📖 技术架构

### 核心模块

```
karing_mac.py          核心库 (374 行)
  ├── GroupReader      读取 Karing 节点选择
  ├── select_proxy()   切换代理节点
  ├── api_call()       Clash API 封装
  └── atomic_json()    原子写入 JSON

sync_rules.py          规则同步 (219 行)
  ├── check()          检查配置漂移
  ├── apply()          应用配置修复
  └── report()         生成同步报告

connection_gc.py       连接回收 (295 行)
  ├── gc_cycle()       回收循环逻辑
  ├── should_gc()      回收判定算法
  └── delete_conn()    删除连接

tunnel_watch.py        线路监控 (447 行)
  ├── Health           健康状态机
  ├── probe_*()        探测函数
  ├── is_valid_proxy_node()  节点过滤 [新增]
  └── auto_switch()    自动切换逻辑

manage_services.py     服务管理 (216 行)
  ├── install()        安装 LaunchAgent
  ├── uninstall()      卸载服务
  └── status()         服务状态
```

### 数据流

```
Karing App
  ↓ (Clash API :9097)
karing_mac.py
  ↓
connection_gc.py + tunnel_watch.py
  ↓
LaunchAgent (后台服务)
  ↓
日志 + 状态文件
```

---

## 🔐 安全性

### 权限范围
- ✅ 仅读取 Karing 公开 API (HTTP 本地端口)
- ✅ 仅写入自己的状态目录
- ✅ 不修改系统配置
- ✅ 不访问敏感数据

### 沙箱限制
- LaunchAgent 在受限环境运行
- 无 sudo 权限
- 无网络服务器
- 日志文件有大小限制 (2 MiB × 4)

---

## 📝 更新日志

### v1.2.0 (2026-10-01)
- ✅ 添加节点智能过滤 (`is_valid_proxy_node`)
- ✅ 修复自动切换 HTTP 400 错误
- ✅ 创建 `karing.py` 维护工具
- ✅ 完善文档 (ARCHITECTURE, OPTIMIZATION_REPORT)
- ✅ 添加 `optimize.py` 全量诊断工具

### v1.1.0 (2026-09-30)
- ✅ 功能验收完成
- ✅ 短时验收通过
- ✅ 36 项单元测试

### v1.0.0 (2026-09-25)
- ✅ 初始版本
- ✅ 连接回收机制
- ✅ 线路监控与自动切换
- ✅ 配置规则同步

---

## 🤝 贡献指南

### 报告问题
1. 运行诊断: `python3 optimize.py`
2. 收集日志: `python3 karing.py logs`
3. 附上系统状态截图

### 提交改进
1. 确保测试通过: `python3 karing.py test`
2. 更新文档
3. 遵循现有代码风格

---

## 📄 许可证

本项目代码遵循项目根目录 LICENSE 文件。

---

## 🙏 致谢

感谢 Karing 项目提供的 VPN 客户端和 Clash API。

---

## 📞 支持

### 文档
- [快速入门](QUICK_START.md)
- [技术架构](ARCHITECTURE.md)
- [优化报告](OPTIMIZATION_REPORT.md)

### 命令帮助
```bash
python3 karing.py help
```

### 系统状态
```bash
python3 karing.py status
```

---

**最后更新**: 2026-10-01  
**版本**: v1.2.0  
**状态**: ✅ 生产就绪
