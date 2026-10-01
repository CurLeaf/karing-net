# Karing macOS 优化快速入门

**适用场景**: 你已经安装 Karing，想解决以下问题：
- Chrome 访问 Google 等网站受限
- 节点切换后连接数堆积
- 节点故障时希望自动切换

**前置条件**: macOS, Python 3.12+, Karing 已运行

---

## 一、10 分钟快速部署

### 1.1 创建强制直连配置

这一步解决 **Chrome 检索受限**问题：

```bash
cd /Users/curleaf/Project/karing-net/mac

# 创建你的强制直连域名列表
cat > config/direct.json << 'EOF'
{
  "domain_suffix": [
    "google.com",
    "google.com.hk",
    "google.co.jp",
    "googleapis.com",
    "googleusercontent.com",
    "gstatic.com",
    "bing.com",
    "baidu.com"
  ]
}
EOF
```

💡 **根据实际需要添加你的内部域名**

### 1.2 应用分流规则

```bash
# 设置 Python 路径
PY=.vfox/sdks/python/bin/python3

# 应用规则
$PY sync_rules.py apply

# 检查是否成功
$PY sync_rules.py check
```

**预期输出**:
```json
{
  "source_ok": true,
  "source_drift": [],
  "generated_ok": true,
  "generated_drift": [],
  "live_reload_verified": false
}
```

### 1.3 重新连接 Karing

⚠️ **重要**: 必须重新连接才能生效

1. 打开 Karing
2. 断开当前连接
3. 重新连接

### 1.4 验证配置

```bash
# 快速验证（约 5 秒）
$PY selfcheck.py

# 完整网络验证（约 70 秒）
$PY selfcheck.py --network
```

**看到 `PASS` 就成功了！**

---

## 二、安装后台服务（可选但推荐）

后台服务提供：
- ✅ 自动回收旧节点连接
- ✅ 线路健康监控
- ✅ 节点故障自动切换

### 2.1 安装服务

```bash
$PY manage_services.py install
```

**预期输出**:
```
gc installed
watch installed
```

### 2.2 确认运行

```bash
$PY manage_services.py status
```

**看到 `state = running` 就成功了！**

### 2.3 查看实时日志

```bash
# 线路监控日志
tail -f ~/Library/Application\ Support/karing-net/tunnel-watch.log

# 连接回收日志
tail -f ~/Library/Application\ Support/karing-net/connection-gc.log
```

---

## 三、验证效果

### 3.1 Chrome 检索

打开 Chrome，访问 https://www.google.com

✅ **成功**: 正常加载搜索页面  
❌ **失败**: `ERR_CONNECTION_CLOSED` → 检查 `config/direct.json` 是否包含 `google.com`

### 3.2 连接自动回收

```bash
# 切换到另一个节点（在 Karing App 中）
# 等待 30 秒后查看连接数

$PY selfcheck.py | grep connections
```

连接数应该在 **10-20** 左右，而不是 60-90

### 3.3 节点自动切换

```bash
# 查看最近的自动切换记录
jq '.auto_switch.last' ~/Library/Application\ Support/karing-net/tunnel-watch.json
```

如果当前节点故障，应该会在 **90 秒**内自动切换

---

## 四、常用命令速查

### 修改强制直连域名

```bash
# 编辑 config/direct.json
vim config/direct.json

# 应用
$PY sync_rules.py apply

# 重新连接 Karing
```

### 查看系统状态

```bash
# 快速检查
$PY selfcheck.py

# 查看后台服务
$PY manage_services.py status

# 查看连接回收统计
jq '.totals' ~/Library/Application\ Support/karing-net/connection-gc.json

# 查看线路健康
jq '.health' ~/Library/Application\ Support/karing-net/tunnel-watch.json
```

### 停止后台服务

```bash
$PY manage_services.py stop
```

### 重启后台服务

```bash
$PY manage_services.py install  # install 会自动重启
```

---

## 五、常见问题

### Q1: Chrome 还是访问不了 Google

**检查清单**:
1. `config/direct.json` 是否包含 `google.com`
2. `sync_rules.py check` 是否返回 `source_ok: true`
3. 是否重新连接了 Karing
4. Karing 本身的连接是否正常

**深入排查**:
```bash
# 查看 Google 的实际路由
$PY selfcheck.py --network | grep google

# 应该看到 chains 包含 direct_out
```

### Q2: 连接数还在增长

**检查 connection_gc 服务**:
```bash
$PY manage_services.py status | grep gc

# 如果不是 running，重新安装
$PY manage_services.py install
```

**查看回收日志**:
```bash
tail -f ~/Library/Application\ Support/karing-net/connection-gc.log

# 应该看到 "tick=N connections=N candidates=N deleted=N"
```

### Q3: 节点故障没有自动切换

**检查自动切换是否启用**:
```bash
jq '.auto_switch.enabled' ~/Library/Application\ Support/karing-net/tunnel-watch.json

# 应该返回 true
```

**手动禁用/启用**:
```bash
# 禁用
export KARING_AUTO_SWITCH=0
$PY manage_services.py install

# 启用
unset KARING_AUTO_SWITCH
$PY manage_services.py install
```

### Q4: 修改配置后没生效

**原因**: 忘记重新连接 Karing

**解决**: 
1. 断开 Karing 连接
2. 重新连接
3. 运行 `$PY selfcheck.py` 验证

### Q5: Python 版本不对

**检查版本**:
```bash
.vfox/sdks/python/bin/python3 --version

# 应该是 3.12.14 或更高
```

**如果没有 vfox**:
```bash
# 使用系统 Python (需要 >= 3.12)
python3 --version

# 修改所有命令中的 PY 变量
PY=python3
```

---

## 六、进阶配置

### 6.1 调整回收时机

编辑 `connection_gc.py`:
```python
QUIET_SECONDS = 24.0      # 空闲多久回收 (默认 24 秒)
CLOSE_BUDGET = 24         # 每轮最多回收数量 (默认 24)
```

**修改后重启服务**:
```bash
$PY manage_services.py install
```

### 6.2 调整监控频率

编辑 `tunnel_watch.py`:
```python
INTERVAL = 10             # 基础采样间隔 (默认 10 秒)
PROBE_INTERVAL = 30       # HTTP 探测间隔 (默认 30 秒)
THROUGHPUT_INTERVAL = 120 # 吞吐测试间隔 (默认 120 秒)
```

### 6.3 自定义探测目标

编辑 `tunnel_watch.py` 中的 `step()` 方法:
```python
a = pool.submit(http_probe, 'https://你的探测目标.com', ports['mixed_in_rule'])
```

---

## 七、卸载

### 7.1 停止后台服务

```bash
cd /Users/curleaf/Project/karing-net/mac
.vfox/sdks/python/bin/python3 manage_services.py stop
```

### 7.2 删除 LaunchAgent

```bash
rm ~/Library/LaunchAgents/com.karing.net.*.plist
```

### 7.3 清理状态文件（可选）

```bash
rm -rf ~/Library/Application\ Support/karing-net
```

### 7.4 恢复 Karing 配置（可选）

如果想恢复原始配置：
```bash
cd /Users/curleaf/Project/karing-net/mac
ls -l ~/Library/Application\ Support/karing-net/backups/

# 选择一个备份恢复
.vfox/sdks/python/bin/python3 sync_rules.py restore --backup ~/Library/Application\ Support/karing-net/backups/20260925-104352

# 重新连接 Karing
```

---

## 八、获取帮助

### 查看完整文档

```bash
cat ARCHITECTURE.md      # 完整架构文档
cat README.md            # 部署说明
cat ACCEPTANCE.md        # 验收报告
```

### 查看日志

```bash
# 最近的事件
grep -E "EVENT|DOWN|UP|switch|ERROR" ~/Library/Application\ Support/karing-net/*.log | tail -50

# 实时监控
tail -f ~/Library/Application\ Support/karing-net/tunnel-watch.log
```

### 运行测试

```bash
cd /Users/curleaf/Project/karing-net/mac
.vfox/sdks/python/bin/python3 -m unittest discover -s tests -v

# 应该看到 36 项测试全部通过
```

---

## 九、最佳实践

### ✅ 推荐做法

1. **定期检查**: 每周运行一次 `selfcheck.py --network --services`
2. **监控日志**: 遇到网络问题时先查日志
3. **及时更新**: 代码更新后运行 `manage_services.py install`
4. **备份配置**: 重要修改前先备份 `config/direct.json`

### ❌ 不推荐做法

1. **不要直接修改 `service_core.json`**: 会被 Karing App 覆盖
2. **不要跳过重新连接**: 修改配置后必须重连才生效
3. **不要禁用后台服务**: 除非你有特殊需求
4. **不要频繁切换节点**: 等回收完成再切（约 30 秒）

---

**部署完成！🎉**

如果遇到问题，请查看 [ARCHITECTURE.md](ARCHITECTURE.md) 了解详细机制。
