# 升级验证清单

**升级日期**: 2026-10-01  
**升级版本**: v1.1.0 → v1.2.0

---

## ✅ 立即验证项

### 1. 代码部署
- [x] `tunnel_watch.py` 已更新（添加 `is_valid_proxy_node()`）
- [x] 服务已重启（新 PID: 76446）
- [x] 单元测试通过（36/36）

### 2. 工具脚本
- [x] `karing.py` - 维护工具
- [x] `optimize.py` - 全量诊断
- [x] `fix_karing.py` - 配置修复
- [x] `optimize_auto_switch.py` - 节点测试

### 3. 文档完善
- [x] `README.md` - 项目概览
- [x] `ARCHITECTURE.md` - 技术架构 (22 KB)
- [x] `QUICK_START.md` - 快速入门 (7.6 KB)
- [x] `OPTIMIZATION_REPORT.md` - 优化报告
- [x] `UPGRADE_CHECKLIST.md` - 本清单

### 4. 配置文件
- [x] `config/direct.json` - 已存在，包含搜索引擎域名
- [x] `config/karing_routing_group.apple.json` - 路由模板
- [x] `config/karing_subscribe_use.apple.json` - 订阅模板

---

## ⏳ 24 小时内验证项

### 1. 节点自动切换
**目标**: 不再出现 HTTP 400 错误

**验证方法**:
```bash
# 检查最近 100 行日志中是否有 HTTP 400
grep "HTTP 400" ~/Library/Application\ Support/karing-net/tunnel-watch.log | tail -20
```

**预期结果**: 无新的 HTTP 400 错误（旧日志可能还有）

**检查时间**:
- [ ] 12 小时后 (2026-10-02 00:00)
- [ ] 24 小时后 (2026-10-02 12:00)

---

### 2. Chrome 检索功能
**目标**: Google、Bing 搜索正常

**前提**: 需要重新连接 Karing

**验证步骤**:
1. [ ] 断开 Karing 连接
2. [ ] 重新连接 Karing
3. [ ] 打开 Chrome
4. [ ] 访问 google.com - 应立即加载
5. [ ] 访问 bing.com - 应立即加载
6. [ ] 搜索测试 - 无 `ERR_CONNECTION_CLOSED`

**如果失败**:
```bash
# 检查直连配置
cat config/direct.json

# 应用配置（需权限）
python3 karing.py apply

# 或使用 sudo
sudo python3 karing.py apply

# 再次重连 Karing
```

---

### 3. 连接回收稳定性
**目标**: 回收成功率 > 99%

**验证方法**:
```bash
python3 -c "
import json
from pathlib import Path
data = json.loads(Path.home().joinpath('Library/Application Support/karing-net/connection-gc.json').read_text())
totals = data['totals']
success = totals['delete_accepted']
failed = totals['delete_failed']
total = success + failed
rate = success / total * 100 if total > 0 else 0
print(f'回收成功: {success}')
print(f'回收失败: {failed}')
print(f'成功率: {rate:.2f}%')
"
```

**预期结果**: 成功率 > 99%

**检查时间**:
- [ ] 12 小时后
- [ ] 24 小时后

---

### 4. 服务进程稳定性
**目标**: 无异常重启

**验证方法**:
```bash
# 检查进程 PID 是否变化
python3 -c "
import json
from pathlib import Path
gc = json.loads(Path.home().joinpath('Library/Application Support/karing-net/connection-gc.json').read_text())
watch = json.loads(Path.home().joinpath('Library/Application Support/karing-net/tunnel-watch.json').read_text())
print(f'connection-gc PID: {gc[\"pid\"]} (初始: 38609)')
print(f'tunnel-watch PID: {watch[\"pid\"]} (初始: 76446)')
"
```

**预期结果**: PID 不变（除非手动重启）

**检查时间**:
- [ ] 12 小时后
- [ ] 24 小时后

---

## ⏳ 1 周内验证项

### 1. 内存占用趋势
**目标**: 稳定在 25 MiB 左右

**验证方法**:
```bash
ps aux | grep -E "connection_gc|tunnel_watch" | grep -v grep
```

**预期结果**: VSZ 列 < 50 MB，RSS 列 < 30 MB

**检查时间**:
- [ ] 3 天后 (2026-10-04)
- [ ] 7 天后 (2026-10-08)

---

### 2. 日志文件大小
**目标**: 日志轮转正常，单文件 < 2 MiB

**验证方法**:
```bash
ls -lh ~/Library/Application\ Support/karing-net/*.log
```

**预期结果**: 每个 .log 文件 < 2 MiB

**检查时间**:
- [ ] 3 天后
- [ ] 7 天后

---

### 3. API 错误率
**目标**: < 5%

**验证方法**:
```bash
python3 -c "
import json
from pathlib import Path
data = json.loads(Path.home().joinpath('Library/Application Support/karing-net/connection-gc.json').read_text())
ticks = data['ticks']
errors = data['totals']['api_errors']
rate = errors / ticks * 100 if ticks > 0 else 0
print(f'总周期: {ticks}')
print(f'API错误: {errors}')
print(f'错误率: {rate:.2f}%')
"
```

**预期结果**: 错误率 < 5%

**检查时间**:
- [ ] 3 天后
- [ ] 7 天后

---

## 🔧 手动操作项

### 必须执行（影响功能）

#### 1. 重新连接 Karing
**目的**: 应用直连配置，修复 Chrome 检索

**步骤**:
- [ ] 打开 Karing App
- [ ] 点击断开连接
- [ ] 等待 3 秒
- [ ] 点击重新连接
- [ ] 测试 Chrome 访问 Google

**时间要求**: 升级后首次使用前

---

### 可选执行（消除警告）

#### 2. 添加终端磁盘访问权限
**目的**: 消除配置漂移警告

**步骤**:
- [ ] 打开系统设置
- [ ] 隐私与安全性
- [ ] 完全磁盘访问权限
- [ ] 点击 + 添加 "Terminal"
- [ ] 重启终端
- [ ] 运行: `python3 karing.py apply`

**时间要求**: 无，可选

**替代方案**: 使用 `sudo python3 karing.py apply`

---

## 📊 监控命令

### 实时状态
```bash
# 每 5 分钟运行一次
python3 karing.py status
```

### 实时日志
```bash
# 保持窗口打开
python3 karing.py logs-live
```

### 健康检查
```bash
# 每天运行一次
python3 karing.py test-network
```

---

## ❌ 回滚方案

如果升级后出现严重问题：

### 1. 停止服务
```bash
python3 karing.py stop
```

### 2. 回滚代码
```bash
git checkout HEAD~1 tunnel_watch.py
```

### 3. 重启服务
```bash
python3 karing.py restart
```

### 4. 报告问题
```bash
# 收集日志
python3 karing.py logs > upgrade_issue.log

# 收集状态
python3 karing.py status > upgrade_status.txt
```

---

## 📈 成功指标

### 短期（24 小时）
- [x] 服务稳定运行
- [ ] 无 HTTP 400 错误
- [ ] Chrome 检索正常
- [ ] 回收成功率 > 99%

### 中期（1 周）
- [ ] 无异常重启
- [ ] 内存占用稳定
- [ ] API 错误率 < 5%
- [ ] 日志轮转正常

### 长期（1 月）
- [ ] 自动切换成功率 > 95%
- [ ] 系统 24/7 稳定运行
- [ ] 用户无手动干预

---

## 📝 备注

### 已知问题
1. **配置漂移警告**: 非关键，需要磁盘访问权限才能消除
2. **偶尔探测超时**: 正常现象，系统会自动重试

### 改进建议
1. 监控前 3 天的日志，确认优化效果
2. 如果 Chrome 检索仍有问题，检查 `config/direct.json` 是否包含你使用的搜索引擎域名
3. 考虑添加更多内部域名到直连列表

---

**检查人**: _____________  
**最后检查**: _____________  
**状态**: 🟡 待验证 → 🟢 已验证 / 🔴 有问题
