# macOS 验收记录

执行日期：2026-09-25。Python 3.12.14，Karing 内核 `sing-box 1.13.19.2802`。

## 已完成并通过

| 验收项目 | 证据与结果 |
| --- | --- |
| 离线策略/故障/同步测试 | `python -m unittest discover -s tests -v`，36 项通过 |
| 累计流量误判修复 | 历史传输 1 MB 后静默仍可回收；连续采样 24 秒前不回收；切换时重新计时 |
| 活跃流量与当前节点保护 | 活跃突发重置时钟、当前节点、其他组选中节点、SSH/UDP、显式强制代理均有测试 |
| API 错误保护 | 部分响应、重复 ID、计数回退、休眠间隔、API 失败均拒绝危险回收或重置采样 |
| 删除复查与计数 | 节点重新选中、新增流量、连接 ID 复用不删除；失败不计成功；204 与后续确认消失分别计数 |
| 配置幂等及回滚 | 合并额外 Apple 域名/自定义字段；不修改无关项；第二次无写入/备份；备份恢复与写入失败回滚通过 |
| 本机配置 | 源规则/生成规则均一致；实际执行 `apply` 返回 `changed: []`，无需重连 |
| Apple DNS | App Store、iTunes、iCloud、系统更新 4 个域名各 20 次，共 80 次返回真实 IP，无 Fake-IP |
| HTTP | Apple 官网、App Store、iTunes、更新服务器、gstatic 代理、百度直连各 5 次，共 30 次符合预期 |
| 运行中的分流 | 创建 10 条受控 CONNECT 连接，用 host + sourcePort 对应 API 连接：4 个 Apple 域名及百度走 direct_out；OpenAI/Gemini/Claude 走 GPT 组；Google/GitHub 走默认组 |
| 只观察运行 | 连续 5 个采样周期识别 2 条旧节点空闲连接，未调用 DELETE |
| 真实自动回收 | 首轮上述 2 条旧节点连接回收通过；最终检查累计 6 条 DELETE 被接受并确认消失，删除失败 0、API 错误 0 |
| DELETE 接口验收 | 另建立一条测试专用 Apple CONNECT 连接，只删除该连接，返回 204 并确认消失 |
| 单实例 | 后台服务运行时再次手工启动 `--apply --loop` 返回 `connection-gc already running` |
| launchd 自动恢复 | 只向两个辅助服务发 SIGTERM，launchd 自动拉起新 PID，两者重新生成健康状态；Karing 本体未重启 |
| 服务更新 | 安装器处理 bootout 的卸载延迟，有限重试；重新安装后两个任务均为 running |
| 日志轮转 | 小容量测试触发轮转，最多 1 个活动文件和 3 个备份 |
| 当前资源快照 | 两个 Python 进程各约 25–26 MiB RSS；采样时 CPU 均显示 0.0%；回收周期约 19 ms。这是短时快照，不是长期平均值 |

网络验收原始 JSON、服务检查、重启验收分别保存在本机状态目录的 `acceptance-network.json`、`acceptance-services.json`、`acceptance-restart.json`。不把含节点信息的原始运行数据放入 Git。

## 验收边界

- 一个没有活动连接的闲置 URLTest 组当前 `now` 为空，明确记为 WARN/未验证，不伪装成选中节点正常。回收器不会处理该组的连接。
- DNS 连续采样可能命中缓存，不能声称覆盖了 80 次不同的上游解析。此次 HTTP 测试均校验 TLS；更新服务器根 URL 的 404 仅证明 HTTP 可达，不代表完整系统更新功能。
- 源文件、生成文件、受控连接分流分开检查。本轮源配置没有变化，所以没有强行断开重连 Karing；“重连/订阅更新后仍然正确”尚待实机验证。
- 36 项测试覆盖模拟节点切换、API 离线恢复、活跃连接保护；没有主动切换用户正在使用的 Karing 节点或断开 Wi-Fi。真实切换/睡眠唤醒后长期负载、整段 SSH/下载/AI 流连续性仍待实机验证。
- launchd 是已登录用户服务，退出登录后不运行。系统通知是否可见未进行人为故障弹窗验证。
- 本次完成的是功能和短时运行验收，**尚未完成 24 小时连续运行验收，也没有完成先前建议的 30 分钟只观察期**。实际删除前做了跨越 24 秒阈值的连续采样、完整离线故障测试和受控 DELETE 验证。

## 后续验收命令

```bash
cd /Users/curleaf/Project/karing-net/mac
PY=.vfox/sdks/python/bin/python3
$PY selfcheck.py --network --services
$PY manage_services.py status
$PY -m unittest discover -s tests -v
```

24 小时后检查两个状态文件的更新时间、累计错误和回收计数，并查日志中的 paused/异常；结合实际下载、SSH、AI 流体验确认有无误回收。低速流量（低于 256 B/s）和长时间无字节的应用暂停可能被当作静默，不能仅靠 Clash 累计字节证明应用层已完成。
