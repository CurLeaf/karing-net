#!/usr/bin/env python3
"""Read-only recorder that explains Karing tunnel drops.

Why this exists
---------------
The tunnel interface (tun0) is periodically torn down and rebuilt, and every
rebuild resets the default route, the DNS wiring and every live connection --
which the user experiences as "the connection dropped again".  Karing leaves no
usable trace of *why*: the GUI writes one line when it launches, and the core's
file log (service_core.log) keeps ERROR lines only.

This watcher builds one timeline so the next drop can be attributed:

* Karing GUI and karingService process start / exit (with pid and wall time)
* listeners on the proxy ports (3057 control, 3065/3066/3067 mixed)
* tun create / destroy (tracked by ifindex and by Karing's tun address, so a
  rebuild onto tun1/tun2 is visible and a foreign tun is not mistaken for ours)
* selected node per group, plus that node's delay
* live connection count
* reachability through the proxy and direct, sampled phase by phase
* the core's own log, streamed from the Clash API /logs endpoint, kept in a
  rolling buffer and dumped on every transition
* the system journal around the transition (NetworkManager / DHCP / resolved),
  which is where a drop caused outside Karing shows up
* a desktop notification when a path is declared unusable and when it recovers

It never writes to Karing's configuration: it only reads /proc, the Clash API
and the network state, and appends to its own log file.

The probe, and why it looks the way it does
-------------------------------------------
The previous version shelled out to ``curl -m 6`` every six seconds and treated
any non-204 as "the proxy failed".  That produced false alarms, because
"returned 000 after 6.00s" is not the same claim as "the proxy is down":

* The 6s ceiling sat exactly where healthy samples live.  Measured over one
  evening: 72 successful samples, 18 of them slower than 4s, the closest at
  5.993s -- i.e. 0.009s of jitter decided between "slow" and "failed".
* One sample decided the verdict, so a node switch (the pool has 39 members and
  reassigns constantly) paged the user for a tunnel that was up.
* ``curl``'s exit code was discarded, so a refused connection, a stalled
  handshake and a bad status all arrived as the same string ``000``.  The three
  genuine outages in the log (instant ``000 0.0001xx``) were indistinguishable
  from the sixteen stalls (``000 6.00``).

So the sample is now taken in-process and timed phase by phase -- ``tcp`` (the
local proxy port), ``connect`` (the proxy's CONNECT), ``tls``, ``http`` -- which
says *where* a path is slow instead of only that it is.  Failures carry a kind
(``port_closed``, ``connect_timeout``, ``tls_cert``, ...) that survives into the
log and the notification.

The verdict is a hysteresis state machine: ``FAIL_THRESHOLD`` consecutive
failures declare a path down, ``RECOVER_THRESHOLD`` consecutive successes
declare it up.  At 10s per sample that means a core restart finishing in five
seconds no longer pages anyone, while a real outage is still reported within
~30s.  A single failure is logged and escalates the core log level so the
context is on disk, but it does not notify.

Cost
----
Measured on this machine before the rewrite: 0.87% of a core for the watcher,
plus a larger indirect cost on the core itself, which was being asked to stream
``level=debug`` around the clock (11.4 KB / 10 s idle, ~70k filtered lines in
2.5h).  After the rewrite:

* no subprocess for probing at all (was 20 ``curl`` forks a minute)
* the core log stream idles at ``warning``, which measured *zero bytes* over 30
  idle seconds, and is promoted to ``debug`` only out of a token bucket, so the
  total debug time the core is ever asked to produce is bounded -- the core's
  reaction to a problem is what the file log never keeps
* /proc is swept every 10s instead of 2s; tun0 and the listening ports still
  every 2s, because those are two small file reads
* per-group Clash queries are used instead of the full /proxies dump (246-1301
  bytes instead of 11647), and only when needed
* the timeline is written through one long-lived handle, and the roll-over check
  counts the bytes this process wrote instead of calling stat() per line
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# tunables
# --------------------------------------------------------------------------
KARING_DIR = Path.home() / ".local/share/com.nebula.karing"
SERVICE_JSON = KARING_DIR / "service.json"
LOG_DIR = Path.home() / ".local/share/karing-net"
LOG_PATH = LOG_DIR / "tunnel-watch.log"
MAX_LOG_BYTES = 20 * 1024 * 1024

CONTROL_PORT = 3057
PROXY_PORT = 3067
PROXY_ENDPOINT = ("127.0.0.1", PROXY_PORT)
WATCHED_PORTS = (3057, 3065, 3066, 3067)
NOTIFY_BIN = Path("/usr/bin/notify-send")
TUN = "tun0"
SYS_NET = Path("/sys/class/net")
# Karing's tun inbound leaves interface_name empty, so the kernel hands out the
# next free tunN.  The stable identity is the address in service_core.json
# (10.20.0.1/30 here), not the name tun0.
DEFAULT_TUN_ADDRS = frozenset({"10.20.0.1"})
_tun_addrs_cache: tuple[float, frozenset[str]] | None = None
SIOCGIFADDR = 0x8915

POLL_SECONDS = 2.0            # tun0 + listening ports: two small file reads
PROC_SCAN_SECONDS = 10.0      # /proc sweep, to catch a core restart
PROC_CONFIRM_SCANS = 2        # a changed pid set must repeat before it counts
PROC_CONFIRM_MAX_SECONDS = 90.0   # ...but flapping must not hide it forever
PROBE_SECONDS = 10.0
GROUP_SECONDS = 30.0
GROUP_REDISCOVER_SECONDS = 300.0
HEARTBEAT_SECONDS = 120.0

# A path is "down" only after this many consecutive failures, and back "up"
# after this many consecutive successes.  With PROBE_SECONDS=10 that means a
# five-second core restart never reaches the threshold, while a real outage is
# reported within ~30s.
FAIL_THRESHOLD = 3
RECOVER_THRESHOLD = 2

SLOW_SECONDS = 3.0            # successful but slow -> WARN, rate limited
SLOW_LOG_SECONDS = 60.0

# Per-phase ceilings.  The old single 6s budget is what made jitter look like an
# outage; phases are given room of their own instead.
PHASE_TIMEOUT = {"tcp": 5.0, "connect": 10.0, "tls": 10.0, "http": 10.0}
# The scope confirmation runs inside the main loop, so it is given a budget that
# cannot hide a tun0 change for long.
CONFIRM_TIMEOUT = {"tcp": 3.0, "connect": 5.0, "tls": 5.0, "http": 5.0}

LOG_LEVEL_BASE = "warning"    # measured: zero bytes while healthy
LOG_LEVEL_ANOMALY = "debug"
LOG_ESCALATE_SECONDS = 45.0   # debug bought by an ordinary anomaly
LOG_ESCALATE_SEVERE_SECONDS = 90.0   # ...and by a teardown-level one
LOG_ESCALATE_MIN_SECONDS = 10.0      # shorter than this is not worth a reopen
# Debug time is spent out of a token bucket instead of being extended on every
# trigger.  Without it a fault that repeats for an hour bought an hour of debug,
# because every escalate() pushed the deadline further out.
LOG_DEBUG_BURST = 180.0       # debug-seconds available at once
LOG_DEBUG_REFILL = LOG_DEBUG_BURST / 900.0   # debug-seconds earned per second
LOG_POLL_SECONDS = 2.0        # how often an idle stream re-checks the level
CORE_BUFFER = 1200            # ring buffer of core log lines
CORE_WINDOW_BUDGET = 400      # max non-error lines written per escalation window
CONTEXT_DELAY_SECONDS = 20.0  # when to capture the core's reaction to a down

# The binaries Karing runs, mapped to the label used in the timeline.  Only the
# basename is ever compared: see karing_kind() for why the *whole* command line
# must never be searched for these names.  A name match is only half of it though
# -- see core_proof() for why a *file* called karingService is not the core.
KARING_EXE_NAMES = {"karingService": "core", "karing": "gui"}
# Where Karing is installed; an exe link under here is one of the proofs.
KARING_INSTALL_DIR = Path("/opt/karing")

NOTIFY_COOLDOWN = 60.0        # per category, and failure/recovery do not share
NOTIFY_EXPIRE_MS = 8000       # GNOME ignores this for urgency=critical, so we don't use that


GROUP_TYPES = {"urltest", "url-test", "selector"}

CORE_LINES: deque[str] = deque(maxlen=CORE_BUFFER)
COUNTERS = {"chatter": 0, "budget_skipped": 0}
_lock = threading.Lock()

# A drop is explained by lifecycle / health lines, not by per-connection routing.
# Kept as a pre-filter so a busy debug window does not cost a regex per line.
CHATTER = re.compile(
    r"inbound connection|inbound packet connection"
    r"|matchRule|match\[\d+\]|found process path|listener_tcp\.go"
    r"|dns: strategy rejected"
    # Direct egress to a bare address names no node and no domain, so it cannot
    # explain a drop -- it only eats the debug window's line budget.
    r"|outbound/direct\[direct_out\]: outbound connection to \d+\.\d+\.\d+\.\d+:",
    re.I,
)
EXPLAIN = re.compile(
    r"sing-box|start(ed|ing)?|stopped|shutdown|reload|restart|fatal|panic"
    r"|url_?test|urltest|health ?check|selected|switch|sniff"
    r"|outbound|exchange failed|lookup failed|rejected|deadline exceeded|timeout|timed out"
    r"|no route|unreachable|reset by peer|network is down|broken pipe|EOF"
    r"|failed to|\berror\b|\berrors\b|\bwarn(ing)?\b|config|rule-set|rule_set|interface|tun|listen"
    r"|dns: strategy|dns cache|expire",
    re.I,
)

KIND_TEXT = {
    "port_closed": "本机代理端口没有监听（内核未运行或刚重启）",
    "refused": "连接被拒绝",
    "reset": "连接被重置",
    "tcp_timeout": "连接本机代理端口超时",
    "connect_rejected": "代理拒绝建立隧道（CONNECT 被驳回）",
    "connect_timeout": "建立隧道超时（节点握手一直没有回来）",
    "tls_error": "TLS 握手失败",
    "tls_cert": "TLS 证书校验失败（可能有中间人拦截）",
    "tls_timeout": "TLS 握手超时",
    "http_timeout": "请求已发出但没有收到响应",
    "http_status": "返回了非预期状态码",
    "empty_reply": "对端没有返回状态行",
    "dns_error": "域名解析失败",
    "os_error": "系统调用失败",
    "unknown": "未知错误",
}


def kind_text(kind: str) -> str:
    return KIND_TEXT.get(kind, kind or "未知错误")


def duration_text(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f} 秒"
    minutes = seconds / 60.0
    if minutes < 90:
        return f"{minutes:.1f} 分钟"
    return f"{minutes / 60.0:.1f} 小时"


def percentile(values, pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int(round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[min(max(index, 0), len(ordered) - 1)]


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------
_LOG_FH = None
_LOG_BYTES = 0
_emit_failed_at = 0.0


def _log_close() -> None:
    global _LOG_FH
    if _LOG_FH is not None:
        try:
            _LOG_FH.close()
        except Exception:
            pass
    _LOG_FH = None


def _log_open() -> None:
    """One long-lived handle instead of an open/write/close per line.

    A debug window delivers hundreds of lines a second, and the old emit() paid
    a stat(), an open(), a write() and a close() for each of them.
    """
    global _LOG_FH, _LOG_BYTES
    _log_close()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_FH = LOG_PATH.open("a", encoding="utf-8")
    try:
        _LOG_BYTES = LOG_PATH.stat().st_size
    except OSError:
        _LOG_BYTES = 0


def rotate() -> None:
    """Roll the log over its ceiling, using the bytes we counted rather than a
    stat() per line."""
    global _LOG_BYTES
    if _LOG_BYTES <= MAX_LOG_BYTES:
        return
    _log_close()
    try:
        LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
    except Exception:
        pass
    _LOG_BYTES = 0


def emit(kind: str, message: str) -> None:
    global _LOG_BYTES, _emit_failed_at
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"{stamp} [{kind}] {message}\n"
    blob = line.encode("utf-8", "replace")
    with _lock:
        rotate()
        failure = ""
        for _ in (0, 1):
            try:
                if _LOG_FH is None:
                    _log_open()
                _LOG_FH.write(line)
                _LOG_FH.flush()      # the timeline is read with tail -f
                _LOG_BYTES += len(blob)
                return
            except Exception as exc:
                _log_close()
                failure = repr(exc)
        # Both attempts failed.  Losing a line must never take the watcher down,
        # but a timeline that has gone quiet must not look like a quiet tunnel
        # either, so say it once a minute on stderr -- systemd puts that in the
        # journal, which is where somebody would look after noticing the gap.
        if time.monotonic() - _emit_failed_at >= SLOW_LOG_SECONDS:
            _emit_failed_at = time.monotonic()
            print(f"tunnel-watch: cannot write {LOG_PATH}: {failure}",
                  file=sys.stderr, flush=True)


def emit_block(kind: str, title: str, body: list[str]) -> None:
    emit(kind, f"=== {title} ===")
    for line in body:
        emit(kind, "    " + line.rstrip())
    emit(kind, f"=== end {title} ===")


def dump_core_log(tag: str, limit: int = 120) -> None:
    with _lock:
        lines = list(CORE_LINES)[-limit:]
    if lines:
        emit_block("CORE-DUMP", tag, lines)
    else:
        emit("CORE-DUMP", f"=== {tag}: buffer empty (stream level was '{LOG_LEVEL_BASE}') ===")


def _http_status(head: bytes) -> str:
    """Status code out of a raw HTTP response head, or '' if it is not HTTP."""
    line = head.split(b"\n", 1)[0].decode("latin-1", "replace").strip()
    bits = line.split()
    if len(bits) >= 2 and bits[1].isdigit():
        return bits[1]
    return ""


# --------------------------------------------------------------------------
# core log stream: level follows the current mood
# --------------------------------------------------------------------------
class CoreLogStream:
    """Streams the core's log, promoting itself to ``debug`` after an anomaly.

    ``level`` is per subscription (verified: a debug and a warning stream opened
    at the same time returned 18 KB and 0 bytes respectively), so changing it
    here does not touch the core's own configuration or its file log.

    The promotion is paid for out of a token bucket rather than extended on every
    trigger; see :meth:`escalate`.  That is what makes "the level always falls
    back to ``warning``" a property of the code instead of a hope: the core's
    ``debug`` output measured ~13576 bytes per idle 30s, roughly ten times what
    ``info`` costs, and it is the core that spends the CPU to format it.
    """

    def __init__(self) -> None:
        self.base_level = LOG_LEVEL_BASE
        self.anomaly_level = LOG_LEVEL_ANOMALY
        self._until = 0.0
        self._reason = ""
        self._tokens = LOG_DEBUG_BURST
        self._tokens_at = time.monotonic()
        self._refused_at = 0.0
        self._window_written = 0
        self._announced = ""
        self._state_lock = threading.Lock()

    # -- level control ----------------------------------------------------
    def desired_level(self) -> str:
        with self._state_lock:
            if time.monotonic() < self._until:
                return self.anomaly_level
        return self.base_level

    def debug_left(self) -> float:
        """Debug seconds still in the pool.  Reported in the heartbeat, so a
        window that keeps being paid for is visible instead of inferred."""
        with self._state_lock:
            self._refill(time.monotonic())
            return self._tokens

    def _refill(self, now: float) -> None:
        """Caller holds ``_state_lock``."""
        earned = (now - self._tokens_at) * LOG_DEBUG_REFILL
        if earned > 0.0:
            self._tokens = min(LOG_DEBUG_BURST, self._tokens + earned)
        self._tokens_at = now

    def escalate(self, reason: str, severe: bool = False) -> None:
        """Buy a window of ``debug`` out of the pool.

        ``severe`` asks for the longer window; it is for what is a teardown by
        definition (tun0 gone, the core pid changed).  Three things the previous
        version got wrong are fixed here:

        * the deadline was simply pushed out on every trigger, so a fault that
          repeated for an hour bought an hour of debug -- now every grant is paid
          for out of a pool that only refills at ``LOG_DEBUG_REFILL``, which caps
          the core's debug output at ``LOG_DEBUG_BURST`` seconds per burst;
        * the extension was silent, so "still at debug" could not be attributed
          from the log -- now it is logged with the seconds it bought;
        * the same fault retriggering every probe cycle re-bought the window --
          now a repeat of the reason that is already covered costs nothing.
        """
        now = time.monotonic()
        want = LOG_ESCALATE_SEVERE_SECONDS if severe else LOG_ESCALATE_SECONDS
        with self._state_lock:
            self._refill(now)
            open_window = now < self._until
            repeat = open_window and reason == self._reason
            granted = 0.0 if repeat else min(want, self._tokens)
            starved = granted < LOG_ESCALATE_MIN_SECONDS
            if starved:
                quiet = now - self._refused_at < SLOW_LOG_SECONDS
                self._refused_at = now
            else:
                self._tokens -= granted
                self._until = max(self._until, now + granted)
                self._reason = reason
                quiet = False
            until, pool = self._until, self._tokens

        if not starved:
            if open_window:
                emit("INFO", f"core log window +{granted:.0f}s ({reason}; "
                             f"{until - now:.0f}s left, pool {pool:.0f}s)")
            else:
                emit("INFO", f"core log level -> {self.anomaly_level} for "
                             f"{granted:.0f}s ({reason}; pool {pool:.0f}s)")
        elif not repeat and not open_window and not quiet:
            emit("INFO", f"core log stays at {self.base_level}: debug pool empty "
                         f"({reason}; refills {LOG_DEBUG_REFILL * 60:.0f}s of debug "
                         f"per minute)")

    def note_window_reset(self) -> None:
        with self._state_lock:
            self._window_written = 0

    def budget_left(self) -> bool:
        with self._state_lock:
            if self._window_written >= CORE_WINDOW_BUDGET:
                return False
            self._window_written += 1
            return True

    # -- delivery ---------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            level = self.desired_level()
            self.note_window_reset()
            try:
                self._stream(level, stop)
            except Exception as exc:
                emit("WARN", f"core log stream unavailable: {exc!r}")
            stop.wait(2.0)

    def _stream(self, level: str, stop: threading.Event) -> None:
        """Read ``/logs`` off a bare socket, because the endpoint is not a normal
        HTTP response.

        Verified behaviour: at ``level=warning`` with nothing to report the core
        flushes *no response headers at all* -- ``curl -i`` prints nothing until a
        line exists.  ``http.client.getresponse()`` therefore blocks forever, and
        a level change made while blocked would never take effect.  Reading the
        socket directly keeps two things true at once: the connection is opened
        once (a TCP connect that also proves the API answers), and an idle stream
        re-checks the requested level every ``LOG_POLL_SECONDS``.

        The body is ``Transfer-Encoding: chunked``, so the chunk-size framing is
        simply skipped: only lines that parse as JSON are fed to the filter.
        """
        port, secret = api_base()
        sock = socket.create_connection(("127.0.0.1", port), 5.0)
        try:
            sock.sendall(
                f"GET /logs?level={level} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {secret}\r\n"
                f"Accept: */*\r\n"
                f"Connection: keep-alive\r\n\r\n".encode()
            )
            if self._announced != level:
                self._announced = level
                emit("INFO", f"core log stream opened at level={level}")

            buffer = b""
            headers_done = False
            delivering = False
            while not stop.is_set() and self.desired_level() == level:
                sock.settimeout(LOG_POLL_SECONDS)
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue          # idle: loop, so a level change is noticed
                if not chunk:
                    break
                buffer += chunk
                if not headers_done:
                    if b"\r\n\r\n" not in buffer:
                        continue
                    head, _, buffer = buffer.partition(b"\r\n\r\n")
                    status = _http_status(head)
                    if status != "200":
                        raise RuntimeError(f"logs endpoint returned {status or 'non-HTTP'}")
                    headers_done = True
                if not delivering:
                    delivering = True
                    emit("INFO", f"core log stream delivering at level={level}")
                while b"\n" in buffer:
                    raw, _, buffer = buffer.partition(b"\n")
                    self._line(raw)
            if not stop.is_set() and self.desired_level() != level:
                emit("INFO", f"core log level {level} -> {self.desired_level()}")
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _line(self, raw: bytes) -> None:
        raw = raw.strip()
        # Chunk-size lines, keepalive blank lines, and the terminating "0".
        if not raw or not raw.startswith(b"{"):
            return
        try:
            item = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return
        if isinstance(item, dict):
            self._ingest(str(item.get("type") or "?"), str(item.get("payload") or ""))

    def _ingest(self, kind: str, payload: str) -> None:
        with _lock:
            CORE_LINES.append(f"[{kind}] {payload}")
        if kind in ("error", "warning", "warn", "fatal", "panic"):
            emit("CORE-" + kind.upper(), payload)
            return
        if CHATTER.search(payload):
            with _lock:
                COUNTERS["chatter"] += 1
            return
        if EXPLAIN.search(payload):
            if self.budget_left():
                emit("CORE", payload)
            else:
                with _lock:
                    COUNTERS["budget_skipped"] += 1


# --------------------------------------------------------------------------
# read-only state probes
# --------------------------------------------------------------------------
def process_start_epoch(pid: int) -> float:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rindex(")") + 2:].split()
        starttime = int(fields[19])
        btime = 0
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        return btime + starttime / os.sysconf("SC_CLK_TCK")
    except Exception:
        return 0.0


def karing_kind(argv0: str, exe: str) -> str:
    """``"core"``, ``"gui"`` or ``""`` for one process's argv[0] and exe link.

    ``argv[0]`` decides, and it is compared as an **exact basename**.  Searching
    the whole command line for the name -- what this used to do -- classified any
    process that merely mentioned it as the core: a shell wrapping a command that
    named the binary (Cursor's sandbox wrapper bash carries the entire command
    text in its argv), a ``grep``, an editor opening service.json.  Each one
    appeared and vanished within a sweep, and every flip bought another debug
    window, which is how the core ended up looking permanently stuck at ``debug``.

    ``/proc/<pid>/exe`` is used to *refuse* a match, never to make one, and that
    direction matters: the core runs as root, so its exe link is not readable for
    us (PermissionError, verified on this machine) and requiring it would mean
    never seeing a core restart at all.  When the link *is* readable and names
    something else, argv[0] was rewritten and the process is not Karing.
    """
    want = KARING_EXE_NAMES.get(os.path.basename(argv0))
    if want is None:
        return ""
    name = os.path.basename(exe)
    if name.endswith(" (deleted)"):
        name = name[: -len(" (deleted)")]
    if name and name not in KARING_EXE_NAMES:
        return ""
    return want


def proc_exe(pid: int) -> str:
    """The exe link, or '' when the kernel will not show it (other user's pid)."""
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except Exception:
        return ""


def parent_pid(pid: int) -> int:
    """The parent pid from /proc/<pid>/stat, 0 when it cannot be read.

    Field 4, but the comm field is parenthesised and may itself contain spaces or
    parentheses, so the field split has to start after the *last* ')'.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat[stat.rindex(")") + 2:].split()[1])
    except Exception:
        return 0


def effective_uid(pid: int) -> int | None:
    """The effective uid from /proc/<pid>/status, None when it cannot be read.

    ``status`` is world readable while ``exe`` is not -- measured on this machine
    for the core, which is setuid (``Uid: 1000 0 0 0``).  That is exactly why this
    is the field worth reading.
    """
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[2])
    except Exception:
        return None
    return None


def under_install_dir(exe: str) -> bool:
    name = exe[: -len(" (deleted)")] if exe.endswith(" (deleted)") else exe
    try:
        return KARING_INSTALL_DIR in Path(name).parents
    except Exception:
        return False


def core_proof(pid: int, gui_pids: set[int]) -> tuple[bool, str]:
    """Why this pid is believed to be the Karing core and not merely named like it.

    ``karing_kind`` only says the *name* matches, and every file called
    ``karingService`` anywhere on disk matches.  Measured 2026-09-20 21:36: a copy
    under ``~/.cache/karing-fakecore/`` was taken for the core on the name alone,
    which bought a 90 s debug window and a full route-table dump -- the same class
    of noise this file exists to get rid of, through a door the name check left
    open.

    So a name match now has to be backed by one of:

    * ``euid == 0`` -- the real core is setuid (``Uid: 1000 0 0 0``, measured) and
      ``status`` stays readable to us, unlike its ``exe`` link.
    * the parent is the Karing GUI (measured ppid 233217 == ``karing``).
    * the ``exe`` link resolves under the install directory.

    When none of those can even be read (``/proc`` hidden by ``hidepid``) the name
    is accepted anyway, because the two failures are not symmetrical: accepting a
    fake costs one debug window, while refusing a real core would quietly drop it
    from the timeline this file exists to produce.
    """
    uid = effective_uid(pid)
    if uid == 0:
        return True, "euid=0"
    parent = parent_pid(pid)
    if parent and parent in gui_pids:
        return True, f"started by the Karing GUI (pid {parent})"
    exe = proc_exe(pid)
    if exe and under_install_dir(exe):
        return True, f"exe under {KARING_INSTALL_DIR}"
    if uid is None and not exe and not parent:
        return True, "unverifiable (/proc not readable); accepted on the name"
    return False, f"euid={uid if uid is not None else '?'} parent={parent or '?'} exe={exe or 'unreadable'}"


_impostors_reported: set[int] = set()


def report_impostor(pid: int, why: str, cmd: str) -> None:
    """Say once, per pid, why something named like the core was not counted."""
    if pid in _impostors_reported:
        return
    if len(_impostors_reported) > 256:      # pids recycle; do not grow forever
        _impostors_reported.clear()
    _impostors_reported.add(pid)
    emit("INFO", f"pid {pid} is named like the Karing core but was rejected: {why}; cmd={cmd[:120]}")


def karing_processes() -> dict[int, str]:
    """The Karing pids as ``{pid: "core: <cmd>"}``.

    The GUI has to be found before the core is judged, because "the parent is the
    GUI" is one of the proofs a core is real: hence the two passes.
    """
    candidates: dict[int, tuple[str, str]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except Exception:
            continue
        argv = [p for p in raw.split(b"\0") if p]
        if not argv:
            continue
        # Cheap reject first: one file read settles almost every pid, and only a
        # handful ever reach the readlink below.
        argv0 = argv[0].decode("utf-8", "replace")
        if os.path.basename(argv0) not in KARING_EXE_NAMES:
            continue
        kind = karing_kind(argv0, proc_exe(pid))
        if kind:
            cmd = b" ".join(argv).decode("utf-8", "replace")[:120]
            candidates[pid] = (kind, cmd)

    gui_pids = {pid for pid, (kind, _) in candidates.items() if kind == "gui"}
    found: dict[int, str] = {}
    for pid, (kind, cmd) in candidates.items():
        if kind == "core":
            ok, why = core_proof(pid, gui_pids)
            if not ok:
                report_impostor(pid, why, cmd)
                continue
        found[pid] = f"{kind}: {cmd}"
    return found


class ProcessSet:
    """The set of Karing pids, with change confirmation on top.

    A /proc sweep can see a pid before it has exec'd -- a fresh core shows up with
    an empty or transient argv[0] and only looks like itself one sweep later -- so
    a real restart is allowed to flicker.  Nothing is claimed until the same set
    has been seen ``PROC_CONFIRM_SCANS`` times running, which also means a process
    that lives for less than one sweep is never reported at all.  A set that never
    settles (alternating between two shapes) is accepted after
    ``PROC_CONFIRM_MAX_SECONDS`` so that confirmation cannot hide a flapping core
    forever.

    Start/exit lines are written only once a change is confirmed.  They carry the
    process's own start time, so confirming late costs no accuracy.
    """

    def __init__(self) -> None:
        self.current: dict[int, str] = {}
        self._pending: dict[int, str] | None = None
        self._scans = 0
        self._since = 0.0

    def note(self, observed: dict[int, str], now: float) -> ProcessChange | None:
        """Feed one sweep; return the change once it is confirmed."""
        if set(observed) == set(self.current):
            self._pending, self._scans, self._since = None, 0, 0.0
            return None
        if self._pending is None or set(self._pending) != set(observed):
            self._pending, self._scans, self._since = observed, 1, now
        else:
            self._scans += 1
        forced = now - self._since >= PROC_CONFIRM_MAX_SECONDS
        if self._scans < PROC_CONFIRM_SCANS and not forced:
            return None
        previous, self.current = self.current, observed
        self._pending, self._scans, self._since = None, 0, 0.0
        return ProcessChange(previous=previous, current=observed, forced=forced)


@dataclass
class ProcessChange:
    """A confirmed change to the set of Karing pids."""

    previous: dict[int, str] = field(default_factory=dict)
    current: dict[int, str] = field(default_factory=dict)
    forced: bool = False

    def events(self) -> list[str]:
        lines: list[str] = []
        for pid, cmd in self.current.items():
            if pid in self.previous:
                continue
            started = datetime.fromtimestamp(process_start_epoch(pid)).strftime("%H:%M:%S")
            lines.append(f"process START pid={pid} at={started} {cmd}")
        for pid, cmd in self.previous.items():
            if pid not in self.current:
                lines.append(f"process EXIT pid={pid} {cmd}")
        return lines

    @property
    def core_changed(self) -> bool:
        """A core pid change is a core restart, i.e. a teardown in its own right.

        The GUI coming and going does not take the tunnel down with it, so the two
        are kept apart and only this one buys the long debug window.
        """
        return self._cores(self.previous) != self._cores(self.current)

    @staticmethod
    def _cores(procs: dict[int, str]) -> set[int]:
        return {pid for pid, label in procs.items() if label.startswith("core")}


def listening_ports() -> set[int]:
    live: set[int] = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(path).read_text().splitlines()[1:]
        except Exception:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            try:
                port = int(fields[1].split(":")[1], 16)
            except Exception:
                continue
            if port in WATCHED_PORTS:
                live.add(port)
    return live


@dataclass(frozen=True)
class TunStatus:
    """One poll of Karing's tunnel interface, whichever tunN it occupies."""

    present: bool
    ifindex: int = 0
    operstate: str = ""
    name: str = ""

    def render(self) -> str:
        if not self.present:
            return "none"
        return f"{self.name}/{self.ifindex}/{self.operstate}"


ABSENT_TUN = TunStatus(present=False)


def iface_ipv4(name: str) -> str:
    """IPv4 address of a local interface, or '' if it has none yet."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = struct.pack("256s", name.encode("ascii", "replace")[:15])
        info = fcntl.ioctl(sock, SIOCGIFADDR, packed)
        return socket.inet_ntoa(info[20:24])
    except OSError:
        return ""
    finally:
        sock.close()


def karing_tun_addrs() -> frozenset[str]:
    """IPv4 addresses configured on the tun inbound.

    Cached by service_core.json mtime so the 2s poll does not parse the file.
    """
    global _tun_addrs_cache
    path = KARING_DIR / "service_core.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return DEFAULT_TUN_ADDRS
    cached = _tun_addrs_cache
    if cached is not None and cached[0] == mtime:
        return cached[1]
    addrs: set[str] = set()
    try:
        data = json.loads(path.read_text())
    except Exception:
        result = DEFAULT_TUN_ADDRS
        _tun_addrs_cache = (mtime, result)
        return result
    for ib in data.get("inbounds") or []:
        if not isinstance(ib, dict) or ib.get("type") != "tun":
            continue
        for key in ("address", "inet4_address"):
            for item in ib.get(key) or []:
                host = str(item).split("/", 1)[0].strip()
                if host:
                    addrs.add(host)
    result = frozenset(addrs) if addrs else DEFAULT_TUN_ADDRS
    _tun_addrs_cache = (mtime, result)
    return result


def _sys_attr(name: str, attr: str) -> str:
    try:
        return (SYS_NET / name / attr).read_text().strip()
    except Exception:
        return ""


def tun_status_of(name: str) -> TunStatus:
    if not (SYS_NET / name).is_dir():
        return ABSENT_TUN
    try:
        ifindex = int(_sys_attr(name, "ifindex") or "0")
    except ValueError:
        ifindex = 0
    return TunStatus(True, ifindex, _sys_attr(name, "operstate"), name)


def tun_state() -> TunStatus:
    """Presence of Karing's TUN, whichever tunN currently holds its address.

    Watching only tun0 reports a teardown whenever another tun is already on
    the machine (tun1 at 17:30 forced the 18:10 reconnect onto tun2).
    """
    try:
        names = sorted(p.name for p in SYS_NET.iterdir() if p.name.startswith("tun"))
    except OSError:
        return ABSENT_TUN
    if not names:
        return ABSENT_TUN
    addrs = karing_tun_addrs()
    for name in names:
        if iface_ipv4(name) in addrs:
            return tun_status_of(name)
    if TUN in names:
        return tun_status_of(TUN)
    return ABSENT_TUN


def tun_transition(prev: TunStatus, curr: TunStatus) -> tuple[str, ...]:
    """Classify one poll.  Empty unless the tunnel actually changed.

    The caller MUST then take ``curr`` as the next ``prev``.  Forgetting that
    is how 2026-09-21 18:10 turned one tun0 teardown into a DESTROYED event
    every 2 seconds and a desktop popup every 60.
    """
    if prev == curr:
        return ()
    events: list[str] = []
    if prev.present and not curr.present:
        events.append("destroyed")
    if curr.present and (
            not prev.present
            or prev.ifindex != curr.ifindex
            or prev.name != curr.name):
        events.append("rebuilt")
    return tuple(events)


def run(cmd: list[str], timeout: float = 6.0) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (proc.stdout or proc.stderr or "").strip()
    except Exception as exc:
        return f"<{exc!r}>"


def api_base() -> tuple[int, str]:
    cfg = json.loads(SERVICE_JSON.read_text())
    return int(cfg.get("control_port") or CONTROL_PORT), str(cfg.get("secret") or "")


def api_json(path: str, timeout: float = 6.0):
    port, secret = api_base()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Authorization": f"Bearer {secret}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def group_names() -> list[str]:
    """Discover the URLTest / Selector groups once, then poll them one by one.

    ``/proxies`` is 11647 bytes of node detail we do not need every cycle;
    ``/proxies/<name>`` is 246-1301 bytes and carries the selected node and its
    delay, which is what the timeline actually records.
    """
    proxies = (api_json("/proxies") or {}).get("proxies") or {}
    return sorted(
        name for name, item in proxies.items()
        if str(item.get("type") or "").lower() in GROUP_TYPES
    )


def group_state(names: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in names:
        try:
            item = api_json(f"/proxies/{urllib.parse.quote(name, safe='')}") or {}
        except Exception:
            continue
        history = item.get("history") or []
        last = history[-1] if history else {}
        out[name] = {
            "now": str(item.get("now") or ""),
            "delay": last.get("delay"),
            "size": len(item.get("all") or []),
        }
    return out


def node_errors(names: list[str]) -> dict[str, str]:
    """Node-level probe errors, from the full dump -- only asked for on a down."""
    out: dict[str, str] = {}
    try:
        proxies = (api_json("/proxies") or {}).get("proxies") or {}
    except Exception:
        return out
    for name in names:
        node = (proxies.get(name) or {}).get("now") or ""
        history = ((proxies.get(node) or {}).get("history") or [])
        last = history[-1] if history else {}
        if last.get("err"):
            out[name] = f"{node}: {last['err']}"
    return out


def connection_count() -> int:
    payload = api_json("/connections") or {}
    return len(payload.get("connections") or [])


def system_proxy() -> str:
    return run(["gsettings", "get", "org.gnome.system.proxy", "mode"], timeout=4.0)


def dns_snapshot() -> list[str]:
    lines = run(["resolvectl", "status"], timeout=6.0).splitlines()
    keep = [l for l in lines if re.search(r"tun\d+|enp3s0|DNS Server|Current DNS|Fallback", l)]
    return keep[:24]


def route_snapshot() -> list[str]:
    lines = run(["ip", "-o", "route", "show", "table", "all"], timeout=6.0).splitlines()
    keep = [l for l in lines if re.search(r"\btun\d+\b|\bdefault\b", l)]
    return keep[:24]


def rule_snapshot() -> list[str]:
    return run(["ip", "rule", "show"], timeout=6.0).splitlines()[:24]


def journal_snapshot(seconds: int = 180, limit: int = 30) -> list[str]:
    """Recent system events, so a drop caused *outside* Karing is still visible."""
    since = run(["date", "-d", f"-{seconds} seconds", "+%Y-%m-%d %H:%M:%S"], timeout=4.0)
    if not since:
        return []
    lines = run(["journalctl", "--no-pager", "-o", "short-iso", "--since", since],
                timeout=8.0).splitlines()
    keep = [l for l in lines
            if re.search(r"tun\d+|NetworkManager|karing|dhcp|carrier|resolved", l, re.I)]
    return keep[-limit:]


def full_snapshot(tag: str) -> None:
    emit_block("STATE", f"{tag}: routes", route_snapshot())
    emit_block("STATE", f"{tag}: dns", dns_snapshot())
    emit_block("STATE", f"{tag}: journal", journal_snapshot())


# --------------------------------------------------------------------------
# the probe
# --------------------------------------------------------------------------
class ProbeError(Exception):
    """A failure with a machine-readable kind, raised from inside a phase."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class Target:
    name: str
    host: str
    path: str = "/generate_204"
    port: int = 443
    expect: tuple[str, ...] = ("204",)


PROXY_TARGET = Target("gstatic", "www.gstatic.com", "/generate_204")
# Used only to confirm a down: if this one still works, the outage is specific to
# the primary target's route rather than the proxy as a whole, and the
# notification says so instead of claiming the proxy is dead.
CONFIRM_TARGET = Target("cloudflare", "cp.cloudflare.com", "/generate_204")
DIRECT_TARGET = Target("baidu", "www.baidu.com", "/", expect=("200", "301", "302"))


@dataclass
class ProbeResult:
    target: str
    direction: str
    ok: bool
    code: str = ""
    kind: str = ""
    detail: str = ""
    stage: str = ""
    phases: dict[str, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return sum(self.phases.values())

    def render(self) -> str:
        timings = " ".join(f"{k}={v:.3f}" for k, v in self.phases.items())
        if self.ok:
            return f"code={self.code} total={self.total:.3f}s {timings}"
        return (f"kind={self.kind} stage={self.stage or '-'} code={self.code or '-'} "
                f"total={self.total:.3f}s {timings} detail={self.detail!r}")


_SSL_CONTEXT: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    """One context per process: building it parses the whole CA bundle."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        _SSL_CONTEXT = ctx
    return _SSL_CONTEXT


def _status_code(raw: bytes) -> str:
    if not raw:
        return ""
    line = raw.split(b"\n", 1)[0].decode("latin-1", "replace").strip()
    bits = line.split()
    if len(bits) >= 2 and bits[1].isdigit():
        return bits[1]
    return ""


def _read_headers(sock: socket.socket, timeout: float, kind: str) -> bytes:
    """Read just far enough to see the status line and the end of the headers."""
    deadline = time.monotonic() + timeout
    buf = b""
    while b"\r\n\r\n" not in buf and b"\n\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            raise ProbeError(kind, f"{timeout:.0f}s 内没有收到完整响应头")
        sock.settimeout(left)
        try:
            chunk = sock.recv(2048)
        except socket.timeout:
            raise ProbeError(kind, f"{timeout:.0f}s 内没有收到完整响应头") from None
        if not chunk:
            break
        buf += chunk
        if len(buf) > 16384:
            break
    return buf


def _classify(exc: BaseException, stage: str, direction: str,
              budget: dict[str, float]) -> tuple[str, str]:
    if isinstance(exc, ProbeError):
        return exc.kind, exc.detail
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls_cert", str(getattr(exc, "verify_message", "") or exc)
    if isinstance(exc, ssl.SSLError):
        return "tls_error", str(exc)
    if isinstance(exc, socket.gaierror):
        return "dns_error", str(exc)
    if isinstance(exc, ConnectionRefusedError):
        if stage == "tcp" and direction == "proxy":
            return "port_closed", f"{PROXY_ENDPOINT[0]}:{PROXY_ENDPOINT[1]} 没有监听"
        return "refused", str(exc)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return f"{stage}_timeout", f"{stage} 阶段超过 {budget.get(stage, 0):.0f}s"
    if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
        return "reset", str(exc)
    if isinstance(exc, OSError):
        return "os_error", f"{exc.__class__.__name__}: {exc}"
    return "unknown", repr(exc)


@contextlib.contextmanager
def _phase(phases: dict[str, float], stage: str):
    """Time one phase, recording it even when the phase is what failed.

    Without this, a refused connection and a stalled handshake both came out as
    ``total=0.000s`` -- the one distinction the old ``000 0.000127`` vs
    ``000 6.002913`` pair carried by accident.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        phases.setdefault(stage, time.monotonic() - started)


def probe(target: Target, direction: str,
          proxy: tuple[str, int] | None = None,
          context: ssl.SSLContext | None = None,
          timeouts: dict[str, float] | None = None) -> ProbeResult:
    """One end-to-end sample, timed phase by phase.

    No subprocess: the old curl probe forked twice every six seconds and threw
    curl's exit code away, so a refused connection, a stalled handshake and a bad
    status all arrived as the same "000".

    ``context`` and ``timeouts`` exist for the self-tests and for the scope
    confirmation, which must not stall the 2s tunnel/port watch behind it.
    """
    budget = timeouts or PHASE_TIMEOUT
    phases: dict[str, float] = {}
    stage = "tcp"
    code = ""
    sock = None
    try:
        host, port = proxy if proxy else (target.host, target.port)

        with _phase(phases, "tcp"):
            sock = socket.create_connection((host, port), budget["tcp"])

        if proxy is not None:
            stage = "connect"
            with _phase(phases, "connect"):
                sock.sendall(
                    f"CONNECT {target.host}:{target.port} HTTP/1.1\r\n"
                    f"Host: {target.host}:{target.port}\r\n"
                    f"Proxy-Connection: keep-alive\r\n\r\n".encode()
                )
                raw = _read_headers(sock, budget["connect"], "connect_timeout")
                status = _status_code(raw)
                if status != "200":
                    raise ProbeError("connect_rejected",
                                     f"CONNECT 返回 {status or '空响应'}")

        stage = "tls"
        with _phase(phases, "tls"):
            sock.settimeout(budget["tls"])
            sock = (context or ssl_context()).wrap_socket(
                sock, server_hostname=target.host)

        stage = "http"
        with _phase(phases, "http"):
            sock.sendall(
                f"GET {target.path} HTTP/1.1\r\n"
                f"Host: {target.host}\r\n"
                f"User-Agent: tunnel-watch\r\n"
                f"Accept: */*\r\n"
                f"Connection: close\r\n\r\n".encode()
            )
            raw = _read_headers(sock, budget["http"], "http_timeout")
            code = _status_code(raw)
            if not code:
                raise ProbeError("empty_reply", "对端关闭连接且没有状态行")
            if code not in target.expect:
                raise ProbeError("http_status",
                                 f"期望 {'/'.join(target.expect)}，得到 {code}")
        return ProbeResult(target.name, direction, True, code=code, phases=phases)
    except Exception as exc:
        kind, detail = _classify(exc, stage, direction, budget)
        return ProbeResult(target.name, direction, False, code=code, kind=kind,
                           detail=detail, stage=stage, phases=phases)
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# verdict: hysteresis, so one slow sample never pages anyone
# --------------------------------------------------------------------------
class Direction:
    """One probed path: an availability verdict plus rolling statistics."""

    def __init__(self, name: str, label: str) -> None:
        self.name = name
        self.label = label
        self.state = "unknown"          # unknown | up | down
        self.fail_streak = 0
        self.ok_streak = 0
        self.down_since = 0.0
        self.down_samples = 0
        self.notified_down = False
        self.last_slow_log = 0.0
        self.latency: deque[float] = deque(maxlen=180)
        self.phases: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=180))
        self.counts: Counter = Counter()
        self.outage_kinds: Counter = Counter()
        self.last: ProbeResult | None = None

    # -- sampling ---------------------------------------------------------
    def feed(self, result: ProbeResult) -> str | None:
        """Record one sample.  Returns 'down'/'up' only on a state transition."""
        self.last = result
        self.counts[result.kind or "ok"] += 1

        if result.ok:
            self.latency.append(result.total)
            for key, secs in result.phases.items():
                self.phases[key].append(secs)
            self.ok_streak += 1
            self.fail_streak = 0
            if self.state == "down" and self.ok_streak >= RECOVER_THRESHOLD:
                self.state = "up"
                return "up"
            if self.state == "unknown":
                self.state = "up"
            return None

        # A failure during an outage is not news; only the first one is.
        if self.state == "down":
            self.down_samples += 1
            self.outage_kinds[result.kind] += 1
            return None

        self.fail_streak += 1
        self.ok_streak = 0
        self.outage_kinds[result.kind] += 1
        if self.fail_streak >= FAIL_THRESHOLD:
            self.state = "down"
            self.down_samples = self.fail_streak
            # The streak started (FAIL_THRESHOLD - 1) samples ago.
            self.down_since = time.time() - (self.fail_streak - 1) * PROBE_SECONDS
            return "down"
        return None

    # -- reporting --------------------------------------------------------
    def short_state(self) -> str:
        if self.state == "down":
            return f"down({duration_text(time.time() - self.down_since)})"
        return self.state

    def stats_line(self) -> str:
        total = sum(self.counts.values())
        good = self.counts["ok"]
        rate = f"{good}/{total}" if total else "n/a"
        parts = [f"{self.name}[{self.short_state()} ok={rate}"]
        p50 = percentile(self.latency, 50)
        p95 = percentile(self.latency, 95)
        if p50 is not None:
            parts.append(f"p50={p50:.2f} p95={p95:.2f}")
        for stage in ("tcp", "connect", "tls", "http"):
            stage95 = percentile(self.phases.get(stage) or [], 95)
            if stage95 is not None and stage95 >= 0.3:
                parts.append(f"{stage}95={stage95:.2f}")
        bad = {k: v for k, v in self.counts.items() if k != "ok"}
        if bad:
            parts.append("bad={" + " ".join(f"{k}:{v}" for k, v in sorted(bad.items())) + "}")
        return " ".join(parts) + "]"


# --------------------------------------------------------------------------
# notifications: paired, per category, and never silent about suppression
# --------------------------------------------------------------------------
_last_notify: dict[str, float] = {}


def notify(category: str, summary: str, body: str, urgent: bool = False,
           *, expire_ms: int | None = None, cooldown: bool = True) -> bool:
    """Desktop popup for a real state change.

    Purely informational: it never toggles Karing.  The cooldown is per category
    and failure/recovery no longer share one budget, so a recovery popup cannot
    swallow the next genuine failure.  Every attempt and every suppression is
    logged, because a silent drop is how a popup storm gets misread.

    Urgency is always ``normal`` (never ``critical``): GNOME keeps critical
    toasts in the tray until the user clicks them, so a 60-second cooldown
    storm looks like it is still happening minutes later.  ``transient`` stops
    the same leftover from accumulating in the notification list.
    """
    if not NOTIFY_BIN.exists():
        return False
    now = time.monotonic()
    if cooldown and now - _last_notify.get(category, 0.0) < NOTIFY_COOLDOWN:
        emit("INFO", f"notify suppressed [{category}]: {summary}")
        return False
    if cooldown:
        _last_notify[category] = now
    expire = NOTIFY_EXPIRE_MS if expire_ms is None else expire_ms
    # Never urgency=critical: GNOME keeps those in the tray until clicked, so a
    # cooldown-spaced storm looks like it is still firing minutes later.
    try:
        proc = subprocess.run(
            [str(NOTIFY_BIN),
             "-u", "normal",
             "-t", str(max(1, expire)),
             "-a", "tunnel-watch",
             "-h", f"string:x-canonical-private-synchronous:tunnel-watch-{category}",
             "-h", "boolean:transient:1",
             summary, body],
            timeout=5, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            emit("WARN", f"notify-send failed ({proc.returncode}): "
                         f"{(proc.stderr or '').strip()[:120]}")
            return False
    except Exception as exc:
        emit("WARN", f"notify-send raised: {exc!r}")
        return False
    emit("INFO", f"notified [{category}]: {summary} | {body}")
    return True


_NOTIFY_ID_RE = re.compile(r"uint32\s+(\d+)")


def retract_notify(category: str) -> None:
    """Take down a leftover popup under this category's replace key.

    After the 2026-09-21 tun0 false-alarm storm the last critical toast sat in
    the tray and looked like a new disconnect every time the user looked up.
    A 1 ms transient replacement under the same synchronous key, immediately
    closed, clears it without asking anyone to click X.
    """
    hint = f"tunnel-watch-{category}"
    dict_arg = (
        "{'urgency': <byte 0>, 'transient': <true>, "
        f"'x-canonical-private-synchronous': <'{hint}'>"
        "}"
    )
    try:
        proc = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.freedesktop.Notifications",
             "--object-path", "/org/freedesktop/Notifications",
             "--method", "org.freedesktop.Notifications.Notify",
             "tunnel-watch", "0", "", "Karing", "",
             "[]", dict_arg, "1"],
            timeout=5, capture_output=True, text=True)
        match = _NOTIFY_ID_RE.search(proc.stdout or "")
        if match:
            subprocess.run(
                ["gdbus", "call", "--session",
                 "--dest", "org.freedesktop.Notifications",
                 "--object-path", "/org/freedesktop/Notifications",
                 "--method", "org.freedesktop.Notifications.CloseNotification",
                 match.group(1)],
                timeout=5, capture_output=True, text=True)
        elif proc.returncode != 0:
            emit("WARN", f"retract notify failed: {(proc.stderr or proc.stdout or '')[:160]}")
            return
    except Exception as exc:
        emit("WARN", f"retract notify raised: {exc!r}")
        return
    emit("INFO", f"notify retracted [{category}]")


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------
class Watcher:
    def __init__(self) -> None:
        self.proxy = Direction("proxy", "代理方向")
        self.direct = Direction("direct", "直连方向")
        self.core = CoreLogStream()
        self.groups: list[str] = []
        self.group_view: dict[str, dict] = {}
        self.pending: list[tuple[float, str]] = []
        self.divergence = ""

    # -- node attribution -------------------------------------------------
    def node_summary(self) -> str:
        if not self.group_view:
            return "未知"
        return " ".join(
            f"{name}={state['now']}({state['delay']})"
            for name, state in sorted(self.group_view.items())
        )

    # -- sample handling --------------------------------------------------
    def handle(self, direction: Direction, result: ProbeResult,
               confirm: Target | None) -> None:
        transition = direction.feed(result)

        if result.ok:
            if result.total >= SLOW_SECONDS:
                now = time.monotonic()
                if now - direction.last_slow_log >= SLOW_LOG_SECONDS:
                    direction.last_slow_log = now
                    emit("WARN", f"probe {direction.name} slow but ok: {result.render()}")
            if transition == "up":
                self.on_up(direction, result)
            return

        emit("EVENT", f"probe {direction.name} FAIL "
                      f"streak={direction.fail_streak}/{FAIL_THRESHOLD} {result.render()}")
        self.core.escalate(f"{direction.name} probe failed ({result.kind})")
        if transition == "down":
            self.on_down(direction, result, confirm)

    def on_down(self, direction: Direction, result: ProbeResult,
                confirm: Target | None) -> None:
        emit("EVENT", f"{direction.name} DOWN after {direction.fail_streak} consecutive "
                      f"failures (since {datetime.fromtimestamp(direction.down_since):%H:%M:%S})")
        self.group_view = group_state(self.groups) if self.groups else {}
        errors = node_errors(self.groups) if self.groups else {}
        if errors:
            emit_block("STATE", f"{direction.name} down: node errors",
                       [f"{k}: {v}" for k, v in sorted(errors.items())])
        full_snapshot(f"{direction.name} down")

        scope = f"{direction.label}不可用"
        extra = ""
        if confirm is not None and result.kind == "port_closed":
            # No point asking about scope: the port this would be probed through
            # is the one that is gone.
            extra = "本机代理端口没有监听，通常是内核未运行或刚重启。"
        elif confirm is not None:
            check = probe(confirm, "confirm", PROXY_ENDPOINT, None, CONFIRM_TIMEOUT)
            if check.ok:
                scope = f"{direction.label}上的 {result.target} 线路异常"
                extra = (f"同一代理访问 {confirm.name} 正常（{check.total:.2f}s），"
                         f"只有 {result.target} 走不通。")
            else:
                extra = (f"第二目标 {confirm.name} 同样不通（{kind_text(check.kind)}），"
                         f"不是单条线路的问题。")
            emit("EVENT", f"confirm via {confirm.name}: {check.render()}")
            self.core.escalate(f"confirm probe: {check.kind or 'ok'}")

        kinds = ", ".join(f"{kind_text(k)}×{v}"
                          for k, v in direction.outage_kinds.most_common())
        body = (f"{result.target}：{kind_text(result.kind)}（{result.stage} 阶段）；"
                f"连续 {direction.fail_streak} 次失败；节点 {self.node_summary()}。"
                f"{extra} 明细见 tunnel-watch.log。")
        direction.notified_down = notify(direction.name, f"Karing {scope}", body)
        # Capture the core's reaction once the debug window has produced something.
        self.pending.append((time.monotonic() + CONTEXT_DELAY_SECONDS,
                             f"{direction.name} down: core reaction"))
        emit("EVENT", f"{direction.name} outage kinds: {kinds}")

    def on_up(self, direction: Direction, result: ProbeResult) -> None:
        outage = time.time() - direction.down_since if direction.down_since else 0.0
        emit("EVENT", f"{direction.name} UP after {duration_text(outage)} "
                      f"({direction.down_samples} failed samples, "
                      f"recovered in {result.total:.2f}s)")
        full_snapshot(f"{direction.name} up")
        dump_core_log(f"{direction.name} recovered")
        if direction.notified_down:
            notify(direction.name, f"Karing {direction.label}已恢复",
                   f"中断 {duration_text(outage)}，{direction.down_samples} 次失败；"
                   f"恢复样本 {result.total:.2f}s。", urgent=False)
        direction.notified_down = False
        direction.down_since = 0.0
        direction.down_samples = 0
        direction.ok_streak = 0

    def check_divergence(self) -> None:
        """The two directions disagreeing is the single most useful signal."""
        both_known = self.proxy.state != "unknown" and self.direct.state != "unknown"
        current = ""
        if both_known:
            if self.proxy.state == "down" and self.direct.state == "up":
                current = f"代理方向不可用而直连正常 -> 问题在代理侧（{self.node_summary()}）"
            elif self.direct.state == "down" and self.proxy.state == "up":
                current = "直连不可用而代理方向正常 -> 问题在本地上行或 DNS"
            elif self.proxy.state == "down" and self.direct.state == "down":
                current = "两个方向同时不可用 -> 问题盖过 Karing（本地上行/内核重启）"
        if current != self.divergence:
            self.divergence = current
            if current:
                emit("EVENT", f"DIVERGENCE: {current}")

    def run_probe_cycle(self) -> None:
        self.handle(self.proxy,
                    probe(PROXY_TARGET, "proxy", PROXY_ENDPOINT),
                    CONFIRM_TARGET)
        self.handle(self.direct, probe(DIRECT_TARGET, "direct", None), None)
        self.check_divergence()

    def heartbeat(self, procs, ports, tun, conns) -> None:
        with _lock:
            counters = dict(COUNTERS)
        node_bit = " ".join(
            f"{name}={state['now']}({state['delay']})"
            for name, state in sorted(self.group_view.items())
        ) or "n/a"
        emit("HEARTBEAT",
             f"procs={sorted(procs)} ports={sorted(ports)} tun={tun.render()} conns={conns} "
             f"core_level={self.core.desired_level()} "
             f"core_debug_pool={self.core.debug_left():.0f}s "
             f"core_noise={counters['chatter']} budget_skipped={counters['budget_skipped']} "
             f"{self.proxy.stats_line()} {self.direct.stats_line()} nodes={node_bit}")

    # -- main -------------------------------------------------------------
    def main(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        emit("INFO", f"tunnel-watch started (pid {os.getpid()})")

        stop = threading.Event()
        threading.Thread(target=self.core.run, args=(stop,), daemon=True).start()

        processes = ProcessSet()
        processes.current = karing_processes()
        procs = processes.current
        prev_ports = listening_ports()
        prev_tun = tun_state()
        ports, tun = prev_ports, prev_tun
        prev_conns = -1
        prev_sysproxy = system_proxy()
        last_proc = last_probe = last_group = last_heartbeat = 0.0
        last_discover = time.monotonic()
        self.groups = group_names()
        emit("INFO", f"baseline procs={sorted(procs)} ports={sorted(prev_ports)} "
                     f"tun={prev_tun.render()} groups={self.groups} "
                     f"fail_threshold={FAIL_THRESHOLD} recover_threshold={RECOVER_THRESHOLD} "
                     f"debug_pool={LOG_DEBUG_BURST:.0f}s")
        # A previous run may have left a critical toast in the tray.
        retract_notify("tunnel")

        while True:
            now = time.monotonic()
            try:
                # -- fast cadence: two small file reads ------------------
                tun = tun_state()
                ports = listening_ports()

                if ports != prev_ports:
                    appeared = sorted(ports - prev_ports)
                    gone = sorted(prev_ports - ports)
                    if appeared:
                        emit("EVENT", f"ports UP {appeared} (now {sorted(ports)})")
                    if gone:
                        emit("EVENT", f"ports DOWN {gone} (now {sorted(ports)})")
                    self.core.escalate("proxy ports changed", severe=bool(gone))
                prev_ports = ports

                events = tun_transition(prev_tun, tun)
                if tun != prev_tun:
                    label = tun.name or prev_tun.name or TUN
                    emit("EVENT", f"{label} state {prev_tun.render()} -> {tun.render()}")
                    if "destroyed" in events:
                        gone_name = prev_tun.name or TUN
                        emit("EVENT", f"{gone_name} DESTROYED (tunnel torn down)")
                        self.core.escalate(f"{gone_name} destroyed", severe=True)
                        full_snapshot(f"{gone_name} gone")
                        self.pending.append((time.monotonic() + CONTEXT_DELAY_SECONDS,
                                             f"{gone_name} gone: core reaction"))
                        notify("tunnel", "Karing 隧道已断开",
                               f"{gone_name} 被销毁，路由和 DNS 已被重置。")
                    if "rebuilt" in events:
                        emit("EVENT", f"{tun.name} REBUILT ifindex={tun.ifindex} "
                                     f"operstate={tun.operstate}")
                        self.core.escalate(f"{tun.name} rebuilt")
                prev_tun = tun

                # -- medium cadence: /proc sweep -------------------------
                if now - last_proc >= PROC_SCAN_SECONDS:
                    last_proc = now
                    change = processes.note(karing_processes(), now)
                    procs = processes.current
                    if change is not None:
                        for line in change.events():
                            emit("EVENT", line)
                        if change.forced:
                            emit("WARN", "karing process set never settled; "
                                         f"accepting it after "
                                         f"{PROC_CONFIRM_MAX_SECONDS:.0f}s")
                        self.core.escalate(
                            "karing core process changed" if change.core_changed
                            else "karing gui process changed",
                            severe=change.core_changed)
                        emit_block("STATE", "after process change",
                                   route_snapshot() + rule_snapshot())

                # -- 30s cadence: groups, connection count, system proxy ----
                # Kept off the 10s probe cycle so the core is not asked to
                # serialise 13 KB of connections and this process is not forking
                # `gsettings` six times a minute.
                if now - last_group >= GROUP_SECONDS:
                    last_group = now
                    if not self.groups or now - last_discover >= GROUP_REDISCOVER_SECONDS:
                        last_discover = now
                        discovered = group_names()
                        if discovered != self.groups:
                            emit("INFO", f"groups {self.groups} -> {discovered}")
                            self.groups = discovered
                    if self.groups:
                        state = group_state(self.groups)
                        for name, new in state.items():
                            old = self.group_view.get(name)
                            if old is None:
                                emit("INFO", f"group {name}: {new['now']} "
                                             f"(delay={new['delay']}, {new['size']} members)")
                            elif old != new:
                                emit("EVENT", f"group {name}: {old['now']} -> {new['now']} "
                                              f"(delay={new['delay']}); was delay={old['delay']}")
                        # Merge rather than replace: a group that failed to answer
                        # this round must not look like a brand new one next round.
                        merged = dict(self.group_view)
                        merged.update(state)
                        self.group_view = merged

                    conns = connection_count()
                    if prev_conns >= 0 and abs(conns - prev_conns) >= 5:
                        emit("INFO", f"connections {prev_conns} -> {conns}")
                    prev_conns = conns

                    sysproxy = system_proxy()
                    if sysproxy != prev_sysproxy:
                        emit("EVENT", f"system proxy mode {prev_sysproxy} -> {sysproxy}")
                        prev_sysproxy = sysproxy

                # -- probes ---------------------------------------------
                if now - last_probe >= PROBE_SECONDS:
                    last_probe = now
                    self.run_probe_cycle()

                # -- deferred context --------------------------------
                due = [item for item in self.pending if item[0] <= now]
                self.pending = [item for item in self.pending if item[0] > now]
                for _, tag in due:
                    dump_core_log(tag)
                    if self.groups:
                        self.group_view = group_state(self.groups)

                # -- heartbeat ---------------------------------------
                if now - last_heartbeat >= HEARTBEAT_SECONDS:
                    last_heartbeat = now
                    self.heartbeat(procs, ports, tun, prev_conns)
            except Exception as exc:
                emit("ERROR", f"poll failed: {exc!r}")
            time.sleep(POLL_SECONDS)


def main() -> None:
    Watcher().main()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
