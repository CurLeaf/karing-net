#!/usr/bin/env python3
"""One-shot connectivity self-check for the Karing tunnel setup (read-only).

Answers "is the network actually healthy right now?" by exercising every path a
request can take on this machine, because the three paths fail very differently:

* ``direct`` -- curl bound to the LAN address.  The policy rules
  (``ip rule`` 9001/9003 -> table 2022) send everything from a normal process
  into ``tun0``, so binding to ``enp3s0``'s own IP is the only way to find out
  whether the physical uplink itself still works.
* ``tunnel`` -- the default path, i.e. whatever any local program does
  (``tun0`` -> Karing rules -> direct_out or urltest_out).
* ``proxy``  -- ``127.0.0.1:3067``, which is what Chrome uses via the GNOME
  system proxy setting.

It also samples DNS (system resolver vs a public resolver), checks the
force-direct domain, and reads the core's own state.  Nothing is written.
"""
from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

LAN_IF = "enp3s0"
TUN = "tun0"
PROXY = "127.0.0.1:3067"
KARING_DIR = Path.home() / ".local/share/com.nebula.karing"
SERVICE_JSON = KARING_DIR / "service.json"
NET_DIR = Path.home() / ".local/share/karing-net"
WATCH_LOG = NET_DIR / "tunnel-watch.log"
RECONCILE_STATE = NET_DIR / "reconcile-state.json"
GC_STATE = NET_DIR / "gc-state.json"
FAKEIP_NETS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("198.20.0.0/15"))
DIRECT_JSON = Path(__file__).resolve().parent / "direct.json"

RESULTS: list[tuple[str, str, bool, str]] = []
WARNINGS: list[tuple[str, str, str]] = []


def direct_domains() -> list[str]:
    """The force-direct domains from ``direct.json``, in configured order.

    Read from the config rather than written out here: they are this machine's own
    company domains and do not belong in a shared checkout (see
    ``direct.example.json``), and reading them means these checks follow whatever
    is actually configured rather than a stale copy.
    """
    try:
        return list(json.loads(DIRECT_JSON.read_text()).get("domain_suffix") or [])
    except Exception:
        return []


def run(cmd: list[str], timeout: float = 10.0) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (proc.stdout or "").strip() or (proc.stderr or "").strip()
    except Exception as exc:
        return f"<{exc!r}>"


def lan_ip() -> str:
    out = run(["ip", "-4", "-o", "addr", "show", LAN_IF], timeout=4.0)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


def record(section: str, name: str, ok: bool, detail: str) -> None:
    RESULTS.append((section, name, ok, detail))


def warn(section: str, name: str, detail: str) -> None:
    WARNINGS.append((section, name, detail))


def http(url: str, mode: str, lan: str) -> tuple[str, str]:
    """mode: tunnel | direct | proxy.  Returns (http_code, time_total|error)."""
    common = ["/usr/bin/curl", "-sS", "-o", "/dev/null", "-m", "20",
              "-w", "%{http_code} %{time_total}"]
    if mode == "proxy":
        cmd = common + ["-x", f"http://{PROXY}", url]
    elif mode == "direct":
        # Binding the source address defeats the "from 0.0.0.0 iif lo" policy
        # rule, so this leaves through the physical uplink instead of tun0.
        cmd = common + ["--noproxy", "*", "--interface", lan, url]
    else:
        cmd = common + ["--noproxy", "*", url]
    out = run(cmd, timeout=25.0)
    parts = out.split()
    if len(parts) >= 2 and parts[0].isdigit():
        return parts[0], f"{float(parts[1]):.2f}s"
    return "000", out[:60] or "<no output>"


def expect(code: str, spec: str) -> bool:
    if code == "000":
        return False
    if spec == "any":
        return int(code) < 500
    return code in spec.split(",")


def check_http(section: str, url: str, mode: str, spec: str, lan: str, label: str = "",
               optional: bool = False, slow_after: float = 5.0) -> None:
    code, extra = http(url, mode, lan)
    ok = expect(code, spec)
    try:
        seconds = float(extra.rstrip("s"))
    except ValueError:
        seconds = -1.0
    text = f"{code:<4} {extra}"
    if not ok and optional:
        warn(section, label or url, text)
        print(f"  WARN {mode:<6} {(label or url):<40} {text}   (可选站点)")
        return
    record(section, label or url, ok, text)
    if not ok:
        print(f"  FAIL {mode:<6} {(label or url):<40} {text}")
        return
    if seconds >= slow_after:
        # Reachable but slow: this is the difference between "broken" and "很费劲".
        warn(section, label or url, f"{extra} 通但慢")
        print(f"  OK   {mode:<6} {(label or url):<40} {text}   慢")
    else:
        print(f"  OK   {mode:<6} {(label or url):<40} {text}")


def dig(name: str, server: str | None = None, rtype: str = "A") -> tuple[list[str], str]:
    """Returns (answers, raw_status).  This dig build rejects "+type=", so use -t."""
    cmd = ["dig", "+short", "+time=3", "+tries=1", "-t", rtype]
    if server:
        cmd.append(f"@{server}")
    cmd.append(name)
    out = run(cmd, timeout=10.0)
    answers = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith(";")]
    return answers, out


def is_fakeip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in FAKEIP_NETS)


def check_dns(name: str, want: str) -> None:
    system, _ = dig(name)
    system = [a for a in system if re.fullmatch(r"[0-9.]+", a)]
    public, _ = dig(name, "223.5.5.5")
    public = [a for a in public if re.fullmatch(r"[0-9.]+", a)]
    fake = any(is_fakeip(a) for a in system)
    kind = "fakeip" if fake else ("real" if system else "empty")
    ok = (kind == want) if want in ("real", "fakeip") else bool(system)
    detail = f"system={kind} {','.join(system[:2]) or '-'} public={','.join(public[:2]) or '-'}"
    record("DNS", name, ok, detail)
    print(f"  {'OK ' if ok else 'FAIL'} {name:<40} system={kind:<7} "
          f"{','.join(system[:2]) or '-':<32} public={','.join(public[:2]) or '-'}")


def check_github_api() -> None:
    """api.github.com is unauthenticated here, and the shared node IP is a
    limited resource: 60 requests/hour.  A 403 therefore means *rate limited*,
    not unreachable, and it is worth showing the reset time."""
    body = run(["/usr/bin/curl", "-sS", "-m", "8", "--noproxy", "*",
                "https://api.github.com/rate_limit"], timeout=12.0)
    try:
        core = (json.loads(body) or {}).get("resources", {}).get("core", {})
    except Exception:
        core = {}
    if not core:
        record("站点", "api.github.com", False, body[:60])
        print(f"  FAIL tunnel api.github.com                          {body[:60]}")
        return
    remaining, limit = core.get("remaining"), core.get("limit")
    if remaining:
        record("站点", "api.github.com", True, f"remaining={remaining}/{limit}")
        print(f"  OK   tunnel api.github.com                          remaining={remaining}/{limit}")
        return
    reset = datetime.fromtimestamp(core.get("reset", 0)).strftime("%H:%M:%S")
    detail = f"限流已用完 0/{limit}，{reset} 重置（共享出口 IP，不是不通）"
    warn("站点", "api.github.com", detail)
    print(f"  WARN tunnel api.github.com                          403 {detail}")


def core_state() -> None:
    cfg = json.loads(SERVICE_JSON.read_text())
    port, secret = str(cfg.get("control_port")), str(cfg.get("secret"))
    base = f"http://127.0.0.1:{port}"

    def api(path: str):
        req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {secret}"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        ver = api("/version")
        record("Karing", "version", True, str(ver.get("version")))
        print(f"  OK   version {ver.get('version')} premium={ver.get('premium')}")
    except Exception as exc:
        record("Karing", "version", False, repr(exc))
        print(f"  FAIL version {exc!r}")
        return

    proxies = (api("/proxies") or {}).get("proxies") or {}
    for name, item in proxies.items():
        if str(item.get("type") or "").lower() not in {"urltest", "url-test", "selector"}:
            continue
        node = proxies.get(str(item.get("now"))) or {}
        hist = (node.get("history") or [{}])[-1]
        delay = hist.get("delay")
        err = hist.get("err") or ""
        ok = bool(item.get("now")) and (delay or 0) > 0 and not err
        detail = f"now={item.get('now')} delay={delay} err={err!r} members={len(item.get('all') or [])}"
        record("Karing", f"group {name}", ok, detail)
        print(f"  {'OK ' if ok else 'FAIL'} group {name:<26} {detail}")

    try:
        conns = api("/connections") or {}
        n = len(conns.get("connections") or [])
        record("Karing", "connections", n > 0, str(n))
        print(f"  {'OK ' if n else 'FAIL'} active connections {n}")
    except Exception as exc:
        record("Karing", "connections", False, repr(exc))
        print(f"  FAIL active connections {exc!r}")


def local_state() -> None:
    ports = sorted({int(m) for m in re.findall(r":(\d+)\s", run(["ss", "-ltn"], timeout=6.0))})
    want = {3057, 3065, 3066, 3067}
    missing = sorted(want - set(ports))
    ok = not missing
    record("本机", "proxy ports", ok, f"missing={missing} listening={sorted(want & set(ports))}")
    print(f"  {'OK ' if ok else 'FAIL'} proxy ports {sorted(want & set(ports))} missing={missing or 'none'}")

    # interface_name is empty, so the kernel assigns the next free tunN.
    # Match Karing by the tun inbound address, not the name tun0.
    ip_out = run(["ip", "-o", "-4", "addr", "show"], timeout=4.0)
    match = re.search(r"\d+:\s+(tun\d+)\s+inet\s+10\.20\.0\.1/", ip_out)
    if match:
        name = match.group(1)
        ifindex = run(["cat", f"/sys/class/net/{name}/ifindex"], timeout=4.0).strip()
        record("本机", "karing tun", True, f"{name} ifindex={ifindex}")
        print(f"  OK  karing tun {name} ifindex={ifindex}")
    else:
        ifindex = run(["cat", f"/sys/class/net/{TUN}/ifindex"], timeout=4.0).strip()
        ok = bool(re.fullmatch(r"\d+", ifindex))
        record("本机", f"{TUN}", ok, f"ifindex={ifindex or 'absent'}")
        print(f"  {'OK ' if ok else 'FAIL'} {TUN} ifindex={ifindex or 'absent'}")

    # `-x` matches the process name exactly.  `pgrep -af karingService` matched any
    # command line that merely mentioned the string -- a shell, a grep, an editor
    # -- which is the same mistake the watcher used to make, and it made this check
    # pass while the core was actually gone.
    pids = run(["pgrep", "-x", "karingService"], timeout=4.0).split()
    core = " ".join(pids) or "absent"
    record("本机", "core process", bool(pids), f"pids={core}")
    print(f"  {'OK ' if pids else 'FAIL'} core process pids={core}")

    gui = run(["pgrep", "-x", "karing"], timeout=4.0).split()
    if gui:
        record("本机", "gui process", True, f"pids={' '.join(gui)}")
        print(f"  OK  gui process pids={' '.join(gui)}")
    else:
        # The core outliving the window is normal, so a missing GUI is a note.
        warn("本机", "gui process", "absent")
        print("  WARN gui process absent")


def watch_counters() -> None:
    if not WATCH_LOG.exists():
        return
    tail = WATCH_LOG.read_text(errors="replace").splitlines()[-400:]
    beats = [l for l in tail if "[HEARTBEAT]" in l]
    if not beats:
        return
    last = beats[-1]
    print(f"  last heartbeat: {last.split('] ', 1)[-1][:150]}")


def reconcile_state() -> None:
    """What the reconciler last did: owed reloads, the retry budget, the race count.

    Read-only, and the closest thing to a health check for the fast path: a reload
    that is still owed, or a type-65 probe that came back empty, means the running
    core does not have the corrected config whatever the file on disk says.
    """
    if not RECONCILE_STATE.exists():
        warn("回收", "reconcile state", f"{RECONCILE_STATE} 不存在")
        print(f"  WARN 状态文件不存在 {RECONCILE_STATE}")
        return
    try:
        st = json.loads(RECONCILE_STATE.read_text())
    except Exception as exc:
        record("回收", "reconcile state", False, repr(exc))
        print(f"  FAIL 状态文件读不出来: {exc!r}")
        return

    race = st.get("race") or {}
    probe = st.get("last_probe") or {}
    pending = bool(st.get("pending_reload"))
    print(f"  ok   reconciles={st.get('reconciles', 0)} reloads={st.get('reloads', 0)} "
          f"failures={st.get('failures', 0)} race={race.get('won', 0)}won/{race.get('lost', 0)}lost "
          f"pending_reload={pending} reload_attempts={st.get('reload_attempts', 0)}")
    print(f"       last_event={st.get('last_event') or '-'} "
          f"last_probe={probe.get('ok')} {probe.get('at') or '-'} {probe.get('detail') or ''}")

    if probe:
        record("回收", "type65 探针", bool(probe.get("ok")), f"{probe.get('at')} {probe.get('detail')}")
    if pending:
        warn("回收", "pending_reload", "还欠一次重载；只在重连的宽限期内属正常，长期为 True 说明内核没收下补写")
    if st.get("failures"):
        warn("回收", "failures", f"累计 {st['failures']} 次（补写无效、内核拒绝或循环异常）")
    if race.get("lost"):
        warn("回收", "race.lost", f"{race['lost']} 次没抢在 App 的 reload 前面，每次多一次 reload 和 tun0 重建")


def gc_state() -> None:
    """karing-gc counters: closed == misrouted + recycled, recycled == dead + stale."""
    if not GC_STATE.exists():
        warn("连接回收", "gc state", f"{GC_STATE} 不存在")
        print(f"  WARN 状态文件不存在 {GC_STATE}")
        return
    try:
        st = json.loads(GC_STATE.read_text())
    except Exception as exc:
        record("连接回收", "gc state", False, repr(exc))
        print(f"  FAIL 状态文件读不出来: {exc!r}")
        return
    closed = int(st.get("closed") or 0)
    mis = int(st.get("misrouted") or 0)
    rec = int(st.get("recycled") or 0)
    dead = int(st.get("recycled_dead") or 0)
    stale = int(st.get("recycled_stale") or 0)
    now = st.get("now") or {}
    ok = closed == mis + rec and rec == dead + stale
    detail = (f"closed={closed} misrouted={mis} recycled={rec} "
              f"dead={dead} stale={stale} now={now}")
    record("连接回收", "counters", ok, detail)
    print(f"  {'OK ' if ok else 'FAIL'} {detail}")
    if st.get("closed_legacy"):
        print(f"       closed_legacy={st['closed_legacy']} (pre-policy, not in closed)")


def main() -> int:
    lan = lan_ip()
    print(f"=== 线路自检 {subprocess.run(['date', '+%F %T'], capture_output=True, text=True).stdout.strip()} "
          f"(LAN {lan or '?'} / {LAN_IF}) ===")

    print("\n--- 1. 局域网与真实出口 ---")
    gw = run(["ping", "-c", "2", "-W", "2", "192.168.1.1"], timeout=8.0)
    ok = " 0% packet loss" in gw or "2 received" in gw or "1 received" in gw
    record("出口", "gateway ping", ok, gw.splitlines()[-2] if gw else "")
    print(f"  {'OK ' if ok else 'FAIL'} gateway 192.168.1.1          {gw.splitlines()[-2] if gw else 'no reply'}")
    check_http("出口", "http://www.baidu.com", "direct", "200", lan, "www.baidu.com (direct)")
    check_http("出口", "https://223.5.5.5/", "direct", "any", lan, "223.5.5.5:443 (direct)")

    print("\n--- 2. DNS ---")
    print("     (system = 系统解析器 127.0.0.53；public = 直接问 223.5.5.5，同样会被 tun0 劫持)")
    for name, want in (("www.baidu.com", "real"), ("codeup.aliyun.com", "real"),
                       ("github.com", "fakeip"), ("www.google.com", "fakeip")):
        check_dns(name, want)
    forced = direct_domains()
    if forced:
        check_dns(forced[0], "real")
    else:
        warn("DNS", "force-direct domains", "direct.json 里没有可检查的域名")
    answers, raw = dig("www.google.com", rtype="HTTPS")
    if answers:
        record("DNS", "HTTPS RR (type65)", True, ",".join(answers[:2]))
        print(f"  OK   HTTPS 类型查询 (type65)              {answers[:2]}")
    else:
        warn("DNS", "HTTPS RR (type65)", raw.splitlines()[0] if raw else "no answer")
        print("  WARN HTTPS 类型查询 (type65)              无应答（fakeip 只支持 A/AAAA）")

    print("\n--- 3. 常用站点（默认路径 = 走隧道）---")
    for url, spec, label, optional in (
        ("https://www.gstatic.com/generate_204", "204", "gstatic 204 (探针)", False),
        ("https://www.google.com", "200,302", "www.google.com", False),
        ("https://github.com", "200", "github.com", False),
        ("https://raw.githubusercontent.com", "any", "raw.githubusercontent.com", False),
        ("https://api.openai.com/v1/models", "401", "api.openai.com", False),
        ("https://api.anthropic.com/v1/messages", "any", "api.anthropic.com", False),
        ("https://api2.cursor.sh/", "any", "api2.cursor.sh (Cursor)", False),
        ("https://claude.ai", "any", "claude.ai", False),
        ("https://gemini.google.com", "any", "gemini.google.com", False),
        ("https://www.youtube.com", "any", "youtube.com", True),
        ("https://x.com", "any", "x.com", True),
        ("https://www.beeapi.ai/", "any", "beeapi.ai", False),
        ("https://registry.npmjs.org", "any", "registry.npmjs.org", False),
        ("https://pypi.org", "any", "pypi.org", False),
        ("https://cdn.jsdelivr.net", "any", "cdn.jsdelivr.net", True),
        ("https://codeup.aliyun.com", "any", "codeup.aliyun.com (国内)", False),
        ("https://docs.qq.com", "any", "docs.qq.com (国内)", False),
        ("https://registry.npmmirror.com", "any", "registry.npmmirror.com (国内)", False),
    ):
        check_http("站点", url, "tunnel", spec, lan, label, optional=optional)
    check_github_api()

    print("\n--- 4. Chrome 走的代理端口 3067 ---")
    for url, spec, label in (
        ("https://www.gstatic.com/generate_204", "204", "gstatic 204 (via proxy)"),
        ("https://github.com", "200", "github.com (via proxy)"),
    ):
        check_http("代理端口", url, "proxy", spec, lan, label)
    if forced:
        check_http("代理端口", f"https://{forced[0]}", "proxy", "200,301,302", lan,
                   f"{forced[0]} (强制直连域名，经代理端口)")

    print("\n--- 5. Karing 内核状态 ---")
    core_state()

    print("\n--- 6. 本机 ---")
    local_state()

    print("\n--- 7. 监视器最近一次心跳 ---")
    watch_counters()

    print("\n--- 8. 回收器最近一次运行 ---")
    reconcile_state()

    print("\n--- 9. 连接回收计数 ---")
    gc_state()

    total = len(RESULTS)
    passed = sum(1 for _, _, ok, _ in RESULTS if ok)
    print(f"\n=== 汇总: {passed}/{total} 通过 ===")
    bad = [(s, n, d) for s, n, ok, d in RESULTS if not ok]
    if bad:
        print("未通过:")
        for section, name, detail in bad:
            print(f"  - [{section}] {name}: {detail}")
    if WARNINGS:
        print("已知问题 / 可选站点:")
        for section, name, detail in WARNINGS:
            print(f"  - [{section}] {name}: {detail}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
