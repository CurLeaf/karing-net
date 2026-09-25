# Chrome / Docker 登录故障排查

日期：2026-09-25。环境：macOS 26.6、Chrome 154.0.8037.58、Karing TUN。

## 当前结论

本次网页不加载现象已在完整重启 Chrome 后恢复，并在原个人资料中完成浏览器验收。证据指向 Chrome 当次运行状态中的页面导航异常；尚未定位到具体 Chromium 模块，也不能据此保证永不复发。

本轮没有修改 Karing 分流、DNS、节点或 Chrome 安全设置，没有删除个人资料、Cookie 或扩展。

## 故障时的证据

- Chrome 普通窗口访问 Docker 卡住，无痕窗口访问 Docker 和 Apple 同样卡住；地址栏显示目标地址，页面仍停留在原来的新标签页。Chrome 内部页面可打开。
- Safari 能打开同一 Docker 授权链接并显示 Google 账号选择页。
- 命令行沿 Docker / Google 授权跳转取得 302、302、200；规则代理入口访问 Docker Hub 也返回 200。强制直连 Google / Hub 存在 TLS 超时，不适合把这些域名全部改为直连。
- Chrome NetLog 对 `login.docker.com` 记录了预连接：DNS 约 7 ms、TCP 约 3 ms、TLS 约 620 ms；TLS 1.3 和 HTTP/2 协商成功。该目标没有实际的 `URL_REQUEST_START_JOB` 页面请求。
- HTTPS / SVCB（TYPE65）单独查询存在超时，但该次 Chrome DNS 已及时返回 A 地址并完成 TLS，不能认定 TYPE65 是此次卡页的原因。
- Chrome 没有已设置的企业策略，也没有额外命令行实验开关。
- 重启前保存了 3 秒进程采样。主线程大部分采样处于事件循环等待，缺少完整 Chromium 符号，无法据此断言具体死锁。

## 处理和实机验收

通过 Chrome 地址栏执行 `chrome://restart` 完整重启并恢复会话。主进程 PID 从 5980 变为 9726，确认发生了进程替换，而非仅关闭窗口。

| 验收项 | 结果 |
| --- | --- |
| 原个人资料打开 Docker Hub | 正常显示首页、搜索入口及镜像列表 |
| 重新打开原 Docker Desktop 授权链接 | 正常显示 Google「选择账号 / 继续前往 docker.com」 |
| 后续只读观察 | 浏览器已到 Docker「You're almost done!」桌面回跳页面；未把它等同于桌面客户端已登录 |
| 此前卡住的 Apple 官网 | 正常显示导航和产品内容 |
| Karing 辅助服务 | gc / watch 均为 running，PID 8536 / 8547，排查期间持续运行 |

访客模式测试因窗口状态变化未完成，不能当作已通过的隔离测试。原资料重启后恢复，因此本轮没有继续重置配置。

## 如果再次发生

1. 先比较 Safari 和 Chrome，再比较 Chrome 普通网页与内部设置页；记录时间及受影响站点。
2. 如再次表现为「其他浏览器正常、Chrome 多站点停在旧页面」，先从 `chrome://net-export/` 保存默认去隐私模式的本地日志，然后执行 `chrome://restart`。
3. 若重启无效，再做访客模式隔离和 Chrome 更新检查；不要仅凭加载转圈就修改分流或关闭安全防护。
4. Docker OAuth 链接含一次性会话参数。若网页已恢复但桌面回跳失败，从 Docker Desktop 重新点 Sign in 生成新链接。

本机原始证据位于 `~/Downloads/karing-docker-chrome-netlog.json` 和 `~/Downloads/karing-chrome-process-sample.txt`，没有上传，也不纳入 Git。NetLog 即使去除 Cookie 等字段仍可能包含访问 URL，分享前需进一步脱敏。
