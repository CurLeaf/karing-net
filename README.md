# karing-net

本机 Karing 分流补强：境外自动选择、公司域名强制直连、节点切换后回收旧连接。

## 做什么

- ChatGPT / Gemini / Claude 走自定义自动组 `GPT自动`
- 其它境外走默认自动选择
- `direct.json` 里的域名写入 Karing 最高优先级自定义组 `🏠 强制直连`，DNS 走真实解析，出站走 `direct_out`
- 系统代理绕过同一组域名，避免 Chrome 再把它们送进 `127.0.0.1:3067` 后落到默认节点
- `karing-gc` 在自动切换节点后关掉旧连接；只回收 Fake-IP 的 CLOSE-WAIT；发现强制直连域名误走代理时立刻拆掉

这些域名不在 `geosite:cn` 里。Chrome 走系统代理时只有域名、没有 IP，会掉进默认 `urltest_out`，再被日本/新加坡节点拒绝，表现为 `ERR_CONNECTION_CLOSED`。

`direct.json` 是本机自己的内部域名清单，**不纳入版本控制**（在 `.gitignore` 里）；仓库里放的是 `direct.example.json`，改成实际域名即可，格式一致。

## 为什么 `service_core.json` 里的规则会被改回去

`service_core.json` 是 Karing 的**产物**，不是它的数据源。桌面端连着的时候会按自己模型里的东西把整份文件重写一遍：DNS 规则按分流组重新生成，outbound 按设置重新补参数。手写进去、模型里没有的东西会在下一次重写时消失。2026-09-20 实测：重写后 App 自己的 21 条 DNS 规则回来了、手写的 SVCB 规则没了、`GPT自动` 变回 7 个节点。

边界很清楚：**有来源的规则能活下来，没来源的活不下来。**

- `🏠 强制直连` 的 DNS / 路由规则活下来，因为 `karing_routing_group.json` 里有这个组、`karing_subscribe_use.json` 里有它到 `direct_out` 的映射，每次重写 App 都能把它重新生成出来。
- SVCB/HTTPS（type 65）规则没有来源。Karing 的模型里没有"按查询类型匹配"这个概念：`lib/app/modules/setting_manager.dart` 的 `SettingConfigItemDNS` 只有 `ttl / enableRule / testDomain / proxyResolveMode(fakeip|proxy|direct) / resolver|outbound|direct|proxy 四组服务器地址 / staticIPs / list`，而 `list` 只是自定义 DNS 服务器的 `{isp,url}` 清单；分流组的编辑界面（`diversion_group_custom_edit_screen.dart`）只有 `domain / domainSuffix / domainKeyword / domainRegex / ruleSet / ruleSetBuildIn / package / networkType`。`query_type` 这个键在 Karing 全部自有数据里只出现在 `service_core.json` 里。

所以这条规则只能由 `karing-reconcile` 在每次被覆盖后写回去，再让运行中的内核重读配置。

重写的时机是 App 重新下发配置（重连、它自己重启内核），**不是**我们调 `/reload`：实测 `/reload` 只让内核以只读方式重读文件，不会引起重写（bpftrace 全程记录，170 秒内只有 python 的读、内核的一次 O_RDONLY）。

### `/reload` 会拆掉并重建 `tun0`，所以要抢在 App 自己那次 reload 之前写回去

2026-09-20 20:00 复核：`/reload` 不是"重读配置"这么温和。日志里 9 次 reload（19:20:22、19:20:46、19:21:21、19:22:27、19:22:47、19:38:28、19:39:34、19:40:57、19:52:53）每一次都在 38 毫秒到 2 秒内跟出一次新的 `tun0`：NetworkManager 每次都报一个新的 Tun 设备，ifindex 一路往上走（69 → 70 → 71 → 72 → 73 → 74）。之前这里记的"reload 不拆隧道、8 MB 传输跨过一次 reload"是错的——当时看的是 `ip link` 的 up 状态，它确实还在，但设备已经在底下被重建了，接口上的连接会断。

于是产生了"每次重连多一次闪断"：App 一连上就重写 `service_core.json`，我们补写规则再 reload，这次 reload 又把 `tun0` 拆了重建一遍。

现在的做法是抢在 App 自己那次 reload 前面：

1. `inotify` 盯着 `~/.local/share/com.nebula.karing` 目录里 `service_core.json` 的写入。App 的写法是 `O_TRUNC` 原地重写（flags `0x80242`），所以它的 `close()` 就是内容完整的第一个时刻，也就是要抢的那一毫秒。
2. 抢到就立刻补写规则。实测这一步（读 + 改 + rename + 复检）**5.1 毫秒**，而 App 从写完到发出 reload 大约 100 毫秒，窗口够用。
3. 等 2 秒宽限，然后做一次真实的 type 65 查询（`dig +short -t HTTPS www.google.com`）。有答案说明 App 那次 reload 已经把修正读进去了，我们**不再 reload**；没答案才自己 reload 一次。
4. 每 5 秒的轮询保留着，兜住 inotify 漏掉的写入（比如别的路径改名替换）。

结果计数在 `~/.local/share/karing-net/reconcile-state.json` 的 `race.won` / `race.lost` 里：`won` 是"App 自己的 reload 带上了修正、我们省掉一次 reload"，`lost` 是"宽限后 type 65 还是没答案、我们自己 reload"。

刚启动的内核有个坑，已经补上：它的命令行里**还没有** `--service-http-port`。实测 2026-09-20 20:08 —— 内核 20:08:31.86 起来，20:08:32.63 我们回写配置时读不到端口，同一条命令行的端口要到 1.9 秒后才出现。以前"读不到端口"被当成"没有内核"，于是把待验证状态清掉了，抢写窗口的成果白费。现在读不到端口就返回 `0`，含义是"内核在跑、端口未知"：待验证状态保留，下一轮再看。

同一次还修掉了 `process_start_time` 的算法错误：它算出来的是 epoch 量级的数（`time.time() - starttime/Hz`，漏了减去系统 uptime），所以"内核刚起来 5 秒内先别 reload"这条保护从来没生效过。现在改成 `uptime - starttime/Hz`，返回真正的存活秒数。

### 为什么不是"干脆别用 fakeip"

把 `dns.proxy_resolve_mode` 从 `fakeip` 改成 `proxy`，分流组的 DNS 规则就会指向能回答 type 65 的 `dns_proxy_out`，这条规则就不需要了——代价是丢掉 fakeip。fakeip 在这里承担的是"让核心看得见域名"：域名规则（`国外穿墙`、`强制直连`、geosite 系列）都靠域名匹配，没有 fakeip 就只能靠 TLS/QUIC 嗅探，纯 TCP、明文之外的协议和已经缓存了真实 IP 的客户端会掉出规则匹配。所以维持 fakeip + 补一条 type 65 规则是代价最小的做法。

## 不要动 `auto_select.interval`

`ensure_tuning()` 会把 `tolerance` 提到 300ms 来抑制节点抖动，但 `interval` 固定在 App 自带的 300s。

内核里 urltest 的 `interval` 由这个值推导，而 `idle_timeout` 完全不在 App 的模型里（`SettingConfigItemAutoSelect` 只有 `interval` / `tolerance` 这些）。两者一旦打架，内核会**拒绝整份配置并退出**——不是重载失败，是整个隧道消失。2026-09-20 就是这么断的：设成 600 之后 App 写出 `interval: 10m` 配一个残留的 `idle_timeout: 5m`，内核报 `start outbound/urltest[urltest_out-GPT自动]: interval must be less or equal than idle_timeout` 然后退出（原文留在 App 的 `errors.json` 里）。300s 时 App 自己配对成 `5m/5m`，合法。

`sync_rules.py` 对此加了硬断言：`TUNING["interval"] > 300` 直接拒绝启动，避免再被调回去。

## 改名单

编辑 `direct.json` 后执行：

```bash
python3 /home/yxw/Projects/karing-net/sync_rules.py apply
python3 /home/yxw/Projects/karing-net/sync_rules.py check
```

然后重开一次 Karing 连接，让核心按新规则重生。`karing-gc` 启动时也会补写缺失规则。

## 服务

- `karing-reconcile.service`：发现内核配置被改回就补写 SVCB 规则 / `GPT自动` 成员 / urltest 参数，然后通过 `GET http://127.0.0.1:<service-http-port>/reload` 让内核重读。状态 `~/.local/share/karing-net/reconcile-state.json`，日志 `reconcile.log`。每次 reload 后会做一次真实的 type 65 查询（`dig -t HTTPS`）确认规则真的生效，结果记在 `last_probe` 里。
  - **先抢 App 的写窗口**：`inotify` 一看到 App 写完 `service_core.json` 就补写（5 毫秒级），2 秒后查一次 type 65；有答案就不 reload。省掉的那次 reload 记在 `race.won`，没抢到、自己 reload 的记在 `race.lost`。因为 reload 会拆掉重建 `tun0`（见上文），`lost` 就意味着这一轮多一次闪断；正常重连应该是 `won`，计数不动才说明 inotify 没起作用。
  - **单实例**：靠 `~/.local/share/karing-net/reconcile.pid` 上的 flock 保证。手动再起一个会被拒绝（`--dry-run` 例外，它只读）。两个实例同时轮询会让 reload 次数翻倍，每次 reload 都会让在途连接报一次 `missing supported outbound`，并且各拆一次 `tun0`。
  - **重载预算**：一次补写给 5 次 reload 机会（`MAX_RELOAD_ATTEMPTS`），全失败就记一条 error、清掉 `pending_reload`，不再每轮去探针——否则 `race.lost` 会每 5 秒涨一次、日志每 5 秒说一次"还在 reloading"。**下一次补写会把预算填满**（`reload_attempts` 在补写成功时清零），所以是"下一个 drift 再试"，不是永久放弃。
  - **端口没公布时不抢**：刚起来的内核命令行里还没有 `--service-http-port`，这时 `core_process` 返回端口 0，本轮不 reload 也不做探针，宽限期的截止时间一起清掉，退回 5 秒轮询等端口出现。不这样的话 `verify_at` 会一直停在过去，循环被钉在 0.2 秒下限上打日志。
  - **宽限期有上限，时钟回拨不能把它变成"无限等"。** `verify_at` 必须能跨服务重启，所以只能存墙钟时间，而时钟可能在它下面移动。若 NTP 把时钟往回调，这个截止时间会在"未来"停留与回拨时长相同的时间，而 `resolve_reload` 在它到期前**拒绝做探针**——于是一份"App 那次 reload 没带上修正"的配置会长时间没人验证。现在 `verify_deadline()` 只认 `VERIFY_GRACE_MAX_SECONDS`（30 秒）以内的截止时间，更远的按"没有宽限"处理、立刻探针。（循环间隔本来就被 `min(POLL_SECONDS, remaining)` 夹住，所以回拨不会像 20:04 那样把循环钉在 0.2 秒下限刷日志；那次是截止时间停在**过去**造成的，是另一个已修的问题。）
  - **一次补写会写两遍 `service_core.json`**（实测间隔 1.7 ms）：`ensure_svcb_rule` 先落规则、`ensure_derived_tuning` 再落 tuning。type 65 规则在第一遍就在，所以中间态是可用的；但它不是原子的，App 的 reload 若正好落在两遍中间，读到的是"有规则、tuning 还是旧值"的版本，下一轮会再补一次。`tests/count_core_writes.py` 把这个间隔量出来并盯着别涨。
- `karing-gc.service`：连接回收 + urltest 防抖，启动命令 `karing-gc.py`。状态 `~/.local/share/karing-net/gc-state.json`。
  - **按需读单个代理，不拉全量 `/proxies`。** 它只需要两类信息：URLTest 组的 `type`/`now`，以及当前连接 `chains[0]` 里那几个节点的 `history`。旧版每 8 秒拉一次全量 `/proxies`（本机实测 11733 字节 / 65 项），换成 `/proxies/<name>` 后代理部分降到 2,694 字节（约 −84%，折算每日约 182 MB → 29 MB）。组名仍取自全量 `/proxies`，但只在每 `GROUP_DISCOVERY_TICKS`（38 个 tick，约 5 分钟）重新发现一次。剩下的 `/connections`（实测 18,185 字节）是每轮都必须看的大头，降不掉。读不到任何组时这一轮**直接跳过**，而不是在"不知道当前选中哪个节点"的情况下动手——`selected` 正是保护在线节点会话的东西。
  - **状态只在真的变了才落盘。** 旧版两个分支都写 `gc-state.json`，8 秒一次、每天 10800 次，即使选中节点和三个计数器都没变。现在比较序列化结果，相同就不写。注意"状态变了"和"关过连接"不是同一个问题：`collect_close_ids` 也会把 URLTest 选中项记进 state。
  - **计数器可核对。** `deferred` 这个名字是错的——它存的是死节点回收数（`dead = n - mis`），而不是"被推迟"的连接。而且在本机三者也确实对不上：`closed` 是 3052，而 `misrouted` 是 0、`deferred` 是 6，因为 `closed` 从"一切换就清连接"的旧策略时代就开始累积，另两个是那之后才有的。读到 3052 只会以为现行策略回收过三千多条连接。现在改名为 `recycled`，无法归因的那部分挪进 `closed_legacy`（本机 3046，不静默丢弃），并记一个 `counters_migrated` 时间戳；从这次起 `closed == misrouted + recycled` 恒成立。`close_connections()` 也从"返回条数"改成"返回真正关掉的 id 列表"，于是一个失败的 DELETE 不会被算进任何一类。
- `tunnel-watch.service`：只读掉线记录器，启动命令 `tunnel-watch.py`。它只读 `/proc`、Clash API 和网络状态，不改 Karing 任何配置，日志写在 `~/.local/share/karing-net/tunnel-watch.log`。每次 `tun0` 销毁重建、进程重启、端口变化、节点切换、方向不可用都会连当时的路由、DNS、系统日志和内核日志一起落盘，用于回答"这次到底为什么断"；真实中断和恢复各弹一次桌面通知。
  - **探针在进程内做，分阶段计时。** 早期版本是 `curl -m 6`，每 6 秒 fork 两次，并且把 curl 的退出码丢掉——于是"端口没人监听"、"握手卡死"、"状态码不对"全都变成同一个字符串 `000`。现在是纯 Python：连本机代理端口（`tcp`）、代理的 `CONNECT`（`connect`）、TLS 握手（`tls`）、请求到状态行（`http`），每段各自计时，失败带 `kind`（`port_closed` / `connect_timeout` / `tls_cert` / `http_status` …）和阶段名，日志和通知都用它。**卡住的阶段也会记耗时**，所以"瞬时被拒"和"卡满 10 秒"能分辨。
  - **判定用迟滞，不靠单次采样。** 连续 `FAIL_THRESHOLD`（3）次失败才判定不可用，连续 `RECOVER_THRESHOLD`（2）次成功才判定恢复。10 秒一次采样意味着：内核重启 5 秒就回来的一次都不弹窗；真断线约 30 秒内报出来。单次失败只写日志、并从一个有额度的池子里买一段 `debug` 窗口抓现场，不打扰人。之前的 6 秒硬阈值正好压在健康样本的分布上——实测 72 次成功里有 18 次慢于 4 秒，最近的一条是 5.993s，也就是说 0.009 秒的抖动决定了"慢"还是"失败"。
  - **判定不可用时先确认范围。** 触发 down 时会用第二个目标（`cp.cloudflare.com`）复测一次：如果它通，通知就说"只有 `<目标>` 这条线路异常"，而不是笼统地说代理挂了；报文中还会带上当前选中节点、失败原因和阶段。
  - **失败和恢复的通知是配对的，且各自限流。** 旧版本共用一个 60 秒额度，恢复通知会吃掉下一次失败通知；现在按方向（`proxy` / `direct` / `tunnel`）各自计时，被限流时会写一条 `notify suppressed` 到日志，不再静默吞掉。通知带 `x-canonical-private-synchronous` 提示，同类消息互相替换而不是堆成一列。
  - **两个方向不一致是最有用的信号。** 代理方向和直连方向跑同一套判定，结论分歧时记一条 `DIVERGENCE`（"代理不可用而直连正常 -> 问题在代理侧"）。实测有段时间直连百度连续超时十几次而代理侧全是 204，这种反向不一致旧版只当成 INFO 埋掉了。
  - **开销。** 探针不再有子进程（原来每分钟 20 个 `curl`）；`/proc` 扫描 2 秒降到 10 秒（`tun0` 和监听端口仍 2 秒，都是两个小文件读）；分组查询改用 `/proxies/<name>`（246–1301 字节）而不是全量 `/proxies`（11647 字节）。
  - **内核日志流平时几乎不花钱，出事才升到 `debug`。** 基线 `warning`，实测空闲 30 秒输出 **0 字节**；异常后升到 `debug` 抓一个窗口再看内核对这个问题的反应，然后回落。`debug` 的产出约为 `info` 的 10 倍以上（实测空闲 30 秒 13576 字节），旧版本 24 小时挂着，2.5 小时里过滤掉了约 7 万行。级别是**按订阅生效**的（同时开 debug 和 warning 两条流，分别收到 18 KB 和 0 字节），所以改它不碰内核自己的配置和文件日志。
    - **窗口是买来的，不是每次触发都往后推。** 升级花的是令牌池：池子容量 `LOG_DEBUG_BURST`（180 秒），按 `LOG_DEBUG_REFILL`（每分钟 12 秒）回充。普通异常买 `LOG_ESCALATE_SECONDS`（45）秒，`tun0` 被销毁、内核 pid 变化这类"按定义就是拆链路"的事件买 `LOG_ESCALATE_SEVERE_SECONDS`（90）秒。窗口开着时同一个原因重复触发**不再付费**；池子见底就停在 `warning`，无论触发多频繁。旧版是每次触发都 `_until = now + 60`，而且窗口内重复触发**不打日志**，所以一个反复出现的小毛病就能把内核按在 `debug` 上很久，日志里还看不出是谁在续。实测 21:22:40 那次窗口实际持续 93 秒，日志里只有一条"for 60s"。现在每次续期都会写明它买了多少秒、池子里还剩多少（`core log window +45s (…; 45s left, pool 90s)`），池子空了也会说一次（60 秒限流）。心跳里有 `core_debug_pool=` 可以直接看剩余额度。
    - 注意 `/logs` 在 `warning` 级别下**空闲时不发响应头**（`curl -i` 什么都不打印），所以这里用裸 socket 读、按 `LOG_POLL_SECONDS`（2 秒）超时轮询，级别变化 2 秒内生效；`http.client.getresponse()` 会永久阻塞，升级就永远不生效。响应体是 chunked，分块长度行靠"解析不出 JSON 就跳过"滤掉。
  - **认进程看的是"它到底是什么"，不是命令行里有没有那个词。** 判定只看 `argv[0]` 的**精确 basename**（`karingService` / `karing`），可读时再用 `/proc/<pid>/exe` 反查一道**否决**。早前是 `"karingService" in cmd` 整条命令行搜子串，于是任何"提到"这个名字的进程都被算成内核：Cursor 的沙箱包装 bash 会把整条命令文本放进自己的 argv，`grep`、编辑器打开 `service.json` 同理。它们随起随灭，进程集合每次扫描都翻转一次，每翻一次就买一个 `debug` 窗口——这才是"内核日志卡在 debug 不回落"的真凶。`exe` 只用来否决、不用来认定，因为内核 `karingService` 是 **setuid** 的（本机实测 `Uid: 1000 0 0 0`，即 real uid 1000 + effective uid 0，不是完全以 root 身份存在），`/proc/<pid>/exe` 对我们因此是 `PermissionError`，要求它可读就等于永远看不见内核重启。`selfcheck.py` 里同样的 `pgrep -af karingService` 也改成了 `pgrep -x`。
  - **光有名字还不够：`core_proof()` 要一道正向证据。** 上面那条只解决"命令行里提到名字"，没解决"有个文件就叫这个名字"。2026-09-20 21:36 实测：`~/.cache/karing-fakecore/karingService` 这样一份副本，`argv[0]` 是真实路径、`exe` 软链也指回自己，名字这一层完全无法把它和内核分开，于是被线上 watcher 收下，买走 90 秒 `debug` 并 dump 了一整份路由表——正是本节要消灭的那类噪声，换了个门口进来。现在名字命中之后还必须满足下面**任意一条**：`euid == 0`（真内核 setuid，而 `status` 对我们可读，`exe` 不可读）；父进程是 Karing GUI（实测 ppid 233217 == `karing`）；`exe` 软链落在 `/opt/karing` 安装目录下。三条都读不到时（`hidepid` 把 `/proc` 藏了）仍按名字采信，因为两种错法并不对称：放过一个假的只花一个 debug 窗口，错杀真内核会让时间线悄悄少掉它本该记录的重启。被拒的进程会**每个 pid 只记一次** `pid N is named like the Karing core but was rejected: <原因>`，所以自测里那份假内核现在是安全的——实测跑完 `test_process_identity.py`，线上日志一行没涨、debug 池停在 180 秒、`tun0 REBUILT` 不涨。
  - **进程集合变化要连续两次扫描一致才认。** 进程 fork 出来到 `exec` 之间，`/proc` 扫到的可能是个还没长成自己的 pid，所以真重启允许抖一次；连续 `PROC_CONFIRM_SCANS`（2）次看到同一个集合才确认，代价是 START/EXIT 事件晚一个扫描周期（10 秒），但事件里写的是进程真实的启动时刻，不影响时间线。一直定不下来的集合在 `PROC_CONFIRM_MAX_SECONDS`（90 秒）后强制采信，免得确认机制把真在抖的内核藏起来。比一次扫描还短的进程（就是上面那类 shell）现在完全不会被记。
  - **日志是常驻句柄，不是每行开关一次文件。** 一个 `debug` 窗口每秒几百行，旧版每行都要 `stat()` + `open()` + `write()` + `close()`。现在写之前不再 `stat`（用自己累计的字节数判断该不该轮转），句柄一直开着、每行 `flush`（时间线是 `tail -f` 看的）。

```bash
systemctl --user status tunnel-watch.service
tail -f ~/.local/share/karing-net/tunnel-watch.log       # 日常盯
grep -E "EVENT|DESTROYED|DIVERGENCE|DOWN|UP after" ~/.local/share/karing-net/tunnel-watch.log | tail -30
grep -E "probe (proxy|direct) FAIL" ~/.local/share/karing-net/tunnel-watch.log | tail -20   # 注意有 streak=n/3，不满 3 次就不是故障
grep HEARTBEAT ~/.local/share/karing-net/tunnel-watch.log | tail -1                          # 成功率 / 分位 / 各阶段耗时 / 当前节点
systemctl --user disable --now tunnel-watch.service      # 不想留就停掉
```

## 排查

```bash
python3 /home/yxw/Projects/karing-net/selfcheck.py           # 全量：线路 + DNS + 内核 + 回收器状态（只读，约 70 秒）
python3 /home/yxw/Projects/karing-net/sync_rules.py check    # 规则是否都在
python3 /home/yxw/Projects/karing-net/karing-reconcile.py --once --dry-run
dig +short -t HTTPS www.google.com                          # type 65 是否真的通
tail -f ~/.local/share/karing-net/reconcile.log
grep -c 'tun0 REBUILT' ~/.local/share/karing-net/tunnel-watch.log        # 一次重连本该只加一次（甚至零次）
```

`selfcheck.py` 的第 8 段直接读 `reconcile-state.json`：`reconciles` / `reloads` / `failures` / `race` 胜负 / `pending_reload` / `reload_attempts` / `last_probe`。`last_probe.ok` 为 false、或 `pending_reload` 长期是 true，都说明**运行中的内核没有拿到修正**，与磁盘上的文件无关。

`scan_cn.py` 是从 Chrome 历史里找域名、给 `direct.json` 提候选的辅助脚本，用法见它自己的 docstring（`--live` 看内核当前连接，`--suggest` 直接吐一个 `direct.json` 片段）。

所有写入者（`karing-gc`、`karing-reconcile`、手动 `apply`）在项目目录下的 `.sync_rules.lock` 上排队；这个文件是空的锁文件，不是残留。

## 自测

```bash
bash /home/yxw/Projects/karing-net/tests/run_tests.sh
```

`tests/` 下这几个都不会动 Karing 的配置，也不碰 `reconcile-state.json`，可以随时跟线上服务并行跑：

- `test_port_parse.py`：`--service-http-port` 的解析（含"还没公布"要返回 0）和 `process_age` 必须是真实存活秒数。
- `test_reload_budget.py`：重载预算在满额时为 spent、补写后清零、空跑和 `--dry-run` 不填预算。
- `test_resolve.py`：宽限期内不花探针、宽限后按探针结果记 `won` / `lost`；以及时钟回拨把截止时间推到很远时不会推迟探针（`verify_deadline()` 的上限）。
- `test_watcher.py`：App 式 `O_TRUNC` 写在 0.3 秒内被唤醒、我们自己的 rename 不被误判。
- `test_probe.py`：探针的失败分类和判定迟滞。只在临时端口的本地监听上跑（含自签证书的 TLS 监听和一个可指定故障模式的假 CONNECT 代理），所以可以挨着线上服务跑。它验两件事：端口未监听 / CONNECT 被驳回 / 握手停滞 / 证书不可信 / 状态码非预期必须给出**五种不同的 kind**（这是旧版 `000` 吞掉的区别），以及失败不满 3 次、恢复不满 2 次都不能改变判定。
- `count_core_writes.py`：在临时 HOME 里复制一份数据目录，数一次补写写了 `service_core.json` 几遍、间隔多少毫秒。
- `test_process_identity.py`：进程身份的两道关，以及 `debug` 窗口的额度池。四种伪装进程——Cursor 那种把命令文本塞进 argv 的 shell、名字只出现在后续 argv 项里的解释器、`argv[0]` 被改写成内核名的、以及**真有一个文件叫 `karingService`**——都必须被拒。最后一种要在 `~/.cache` 下构造（`/tmp` 是 noexec）且二进制不能按 `argv[0]` 分发（本机 `/bin/sleep` 是 coreutils 多合一，改名后直接报 `unknown program`），所以用解释器的副本；用例会先断言名字这一层**确实接受**它，以此证明拦住它的是 `core_proof()`，并再断言真内核仍被接受、且它的 `exe` 确实不可读。它会临时改写 `emit`，免得把"拒绝了谁"写进线上日志。

会写真实 `service_core.json` 的端到端抢写测试故意不放在这里：它可能让这一轮多一次 reload、把 `tun0` 拆了重建，所以放在 `tools/` 下并加了 `--yes` 确认：

```bash
python3 ~/Projects/karing-net/tools/simulate_app_write.py --yes
```

它按 App 的写法（原地 `O_TRUNC`）把 type-65 规则抹掉，量补写恢复的毫秒数，跑完对照 `race` 计数和 `grep -c 'tun0 REBUILT'`：正常的重连和这个模拟都应该是 `won` 涨 1、`lost` 不动、`tun0 REBUILT` 不涨。

## 抓写入者

要确认到底是谁在写 `service_core.json`（App、内核、还是我们自己的工具）：

```bash
sudo bpftrace ~/Projects/karing-net/tools/watch_config.bt \
    $(pgrep -f '/opt/karing/karing$') | tee -a ~/.local/share/karing-net/config-writes.log
```

每行带 `秒.毫秒`（相对 `[start]` 那行的墙钟时间），能分辨 `[WRITE-OPEN]`（带写标志的 openat）、`[OPEN]`（任何 open，包括内核的只读重读）、`[RENAME]`（我们自己 `dump_json` 的 rename）、`[APP-WRITE]`（App 针对其它文件的写，需传 App pid）。脚本不常驻，查完 Ctrl-C；传 pid 是可选的，不传只是 `[APP-WRITE]` 那两条不触发。

