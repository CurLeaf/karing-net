# macOS Karing 优化

本目录独立运行，不依赖 `../linux/`。使用 vfox Python 3.12.14，全部为标准库，无 pip 依赖。

## 当前运行机制

- `sync_rules.py`：合并 Apple 域名、保持 Apple/强制直连/AI 规则顺序；AI 映射到现有 `GPT自动`，直连映射到 `direct_out`。保留额外字段及其他规则，不硬编码订阅节点。重复同步没有变更时不写文件、不生成备份。
- `connection_gc.py --apply --loop`：每 8 秒采样；节点切换后旧节点 TCP 连接连续安静 24 秒才回收。以相邻样本的上传/下载增量计时（阈值 256 B/s），删除前复查节点和流量；单轮最多 24 条。当前任何组选中的节点、直连、UDP、SSH/22/2222、RDP/3389、显式强制代理入口受到保护。已经在源配置和生成配置中确认的强制直连域名误走代理时回收。
- `tunnel_watch.py`：每 10 秒检查 API/节点选择，每 30 秒检查规则代理和强制直连入口，每 120 秒检查规则漂移。连续失败 3 次后报告异常，连续成功 2 次报告恢复；故障时用第二目标确认范围，记录路由/DNS/系统代理快照。通知由 macOS `osascript` 发出，是否展示取决于系统通知权限。
- 两个用户级 launchd 服务：登录后启动、退出后自动拉起；单实例锁防止手工命令和后台服务重叠。API 故障时暂停回收，8/16/32/60 秒退避；恢复或休眠后重新采样，不把停顿当成空闲。

控制 API 直接访问 `127.0.0.1`，每次读取当前端口和认证；不会把本机 API 请求送进系统代理。日志每文件 2 MiB，最多保留 3 个轮转文件。状态及备份权限为 0600，认证密钥不写日志。

## 常用命令

先进入本目录，使用项目 Python：

```bash
cd /Users/curleaf/Project/karing-net/mac
PY=.vfox/sdks/python/bin/python3
$PY sync_rules.py check
$PY sync_rules.py apply
$PY selfcheck.py --network --services
$PY connection_gc.py --dry-run --loop --duration 40
$PY manage_services.py status
$PY manage_services.py install  # 安装或更新并重启两个服务
$PY manage_services.py stop     # 停止两个服务
$PY -m unittest discover -s tests -v
```

`connection_gc.py` 单次运行只建立样本，不会宣称已观察到 24 秒空闲；`--loop` 才连续采样。dry-run 使用独立锁、日志和状态，不覆盖生产服务统计。生产服务已运行时手工 `--apply` 会被拒绝。

## 分流配置

Karing 数据目录默认 `~/Library/Group Containers/group.com.nebula.karing/`，可通过 `KARING_DATA_DIR` 覆盖。

Apple 配置源为 `config/karing_routing_group.apple.json` 和 `config/karing_subscribe_use.apple.json`。仅包含片段，不能覆盖整个 Karing 配置。公司域名可将 `config/direct.example.json` 复制为 `config/direct.json` 后填写；该文件被 Git 忽略。未提供此文件时保留当前公司的域名列表。

修改前完整备份两个源文件及 `service_core.json`，放到 `~/Library/Application Support/karing-net/backups/`，最多保留最近 20 份。恢复命令：

```bash
$PY sync_rules.py restore --backup "$HOME/Library/Application Support/karing-net/backups/<时间戳>"
```

恢复会先备份当前源文件。同步和恢复不写 `service_core.json`、不调用 Mac 扩展的 `/reload`；有源文件变更时需重新连接 Karing，再用 `selfcheck.py --network` 验证。App 可能持有内存配置，因此磁盘检查通过不能代替重连后的验收。监控器只报告漂移，不周期性强行覆盖 App 配置。

## 验收与限制

详见 [验收报告](ACCEPTANCE.md)。`selfcheck.py` 分别检查源文件、生成文件、Clash API、真实连接分流和网络；闲置组没有当前节点时标 WARN，不为特定组硬编码放行。

Chrome 页面一直转圈、Docker 登录不加载的实际排查与恢复过程见 [Chrome / Docker 故障记录](DOCKER_CHROME_DIAGNOSIS.md)。此次完整重启 Chrome 后恢复，浏览器实测通过；网络探针通过不能代替页面验收。

日志和状态默认位于 `~/Library/Application Support/karing-net/`，可通过 `KARING_STATE_DIR` 覆盖。敏感配置不进入仓库。原先 `mac/backup/` 中的本机备份继续保留且被忽略。

这版明确不提供 Linux 的 `ss -K`、inotify 和未知的 Mac `/reload` 接口；也未移植“报错节点 20 秒回收”分支，统一用已验证的 24 秒安静策略。低于 256 B/s 的持续小流量可能被视为心跳；无法保证所有第三方长轮询或暂停中的 AI 流永不重连。强制直连误路由回收是例外，可能中断该连接以便重新路由。

这里的 direct 探针通过 Karing 的 `mixed_in_direct` 入口明确选择 `direct_out`，可以判断直连出口可达，但不能证明 Karing 整体失效时物理网络仍正常。系统更新服务器根路径的 404 仅验证 DNS/TLS/HTTP 可达，不代表完整系统更新下载成功。
