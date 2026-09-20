#!/usr/bin/env python3
"""Keep Karing's generated core config in the shape this project needs.

Why this exists
---------------
``service_core.json`` is an *output*.  The Karing desktop app rebuilds the whole
file from its own in-memory model every time it connects, so anything written
into it survives only until the next reconnect.  Measured on 2026-09-20: the app
restored its own 21 DNS rules, dropped the hand-written SVCB rule, and put the
7-node GPT pool back -- while the rules that *are* derivable from Karing's own
data (``karing_routing_group.json`` + ``karing_subscribe_use.json``) were
regenerated correctly.

The SVCB/HTTPS (type 65) rule is the one thing it cannot regenerate: query_type
exists nowhere in Karing's data files (grep: only in service_core.json itself),
so the app has no model to reproduce it from.  The rule is therefore re-asserted
here, after every regeneration, and the core is told to re-read the file.

Mechanism (measured, not assumed)
---------------------------------
* ``GET http://127.0.0.1:<service-http-port>/reload`` makes the running core
  re-read service_core.json: with the rule removed, ``dig -t HTTPS www.apple.com``
  times out; with it restored, a fresh name answers again.
* The Clash API on the control port does *not*: ``PUT /configs?force=true``
  returns 204 but leaves the running DNS rules untouched.  Do not use it.
* The service port changes on every connect (36767 -> 36283 came and went today),
  so it is read from the karingService command line each time.
* A reload **does** tear the tunnel down and rebuild it.  Every one of the nine
  reloads this file logged on 2026-09-20 (19:20:22, 19:20:46, 19:21:21,
  19:22:27, 19:22:47, 19:38:28, 19:39:34, 19:40:57, 19:52:53) was followed by a
  fresh tun0 within 38 ms -- 2 s: NetworkManager announces a new Tun device and
  the ifindex walks upwards (69, 70, 71, 72, 73, 74).  An earlier note here
  ("an 8 MB transfer completed across one, and tun0 stayed up") was wrong: it
  read ``ip link`` state, which does survive, and missed the device being
  recreated underneath.  So a reload costs one blip, and a *second* reload inside
  one connect sequence costs a second one -- which is what the fast path avoids.
* A reload is only attempted when the config on disk is known-good; the core
  *exits* on a config it rejects (interval > idle_timeout did exactly that).

Racing the app's own reload
---------------------------
The app rewrites service_core.json in place (open O_TRUNC, write, close) and asks
the core to reload about a tenth of a second later.  Rewriting the file the moment
that close() lands -- rather than on the next 5 s poll -- means the app's reload
usually reads the corrected file, so no second reload and no second tun0 rebuild
is needed at all.  A reload is now taken only when a real ``dig -t HTTPS`` query
still gets no answer after the grace period, i.e. when the race was lost.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import logging
import os
import select
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_rules

KARING_DIR = Path.home() / ".local/share/com.nebula.karing"
CORE_PATH = sync_rules.CORE_PATH
LOG_DIR = Path.home() / ".local/share/karing-net"
LOG_PATH = LOG_DIR / "reconcile.log"
STATE_PATH = LOG_DIR / "reconcile-state.json"
PID_PATH = LOG_DIR / "reconcile.pid"
POLL_SECONDS = 5
# Give a freshly started core a moment before asking it to re-read its config.
CORE_SETTLE_SECONDS = 5
# Reload requests that keep failing are retried, but not forever in one burst.
MAX_RELOAD_ATTEMPTS = 5
# How long the app's own reload is given to pick up a rewrite before we conclude
# the race was lost and reload ourselves.  The observed gap between the app's
# write and its reload is ~0.1 s; the rest is slack for a slow connect.
VERIFY_GRACE_SECONDS = 2.0
# The grace deadline is persisted, so it is wall-clock time and the clock can move
# under it.  A deadline further ahead than this is not a grace period any more, so
# it is treated as stale instead of believed.
VERIFY_GRACE_MAX_SECONDS = 30.0
# Our own rewrite ends in a rename, which is itself a write event on the watched
# file.  Hits this soon after our own write are ours, not the app's.
IGNORE_OWN_WRITE_SECONDS = 1.0

# inotify masks: the app closes the file it truncated; our writer renames a .tmp
# over it, and a future version of the app might too, so watch both.
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
WATCHED_EVENTS = IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE
INOTIFY_EVENT = "iIII"

# Held for the lifetime of the process; the lock is released when it exits.
_instance_lock = None


class ConfigWatcher:
    """Wake the loop the moment the app's rewrite of service_core.json lands.

    The app opens the file with O_TRUNC and writes it in place, so its close() is
    the first moment the content is complete -- and the event raced against the
    app's own reload request.  The containing directory is watched rather than
    the file itself so that replacement-by-rename is seen as well; an inode watch
    would keep following the file that was replaced.
    """

    def __init__(self, path: Path) -> None:
        self.name = path.name.encode()
        self.fd: int | None = None
        self.last_own_write = 0.0
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                raise OSError(ctypes.get_errno(), "inotify_init1")
            wd = libc.inotify_add_watch(
                ctypes.c_int(fd), str(path.parent).encode(), ctypes.c_uint32(WATCHED_EVENTS)
            )
            if wd < 0:
                raise OSError(ctypes.get_errno(), "inotify_add_watch")
            self.fd = fd
        except Exception as exc:
            # Not fatal: without inotify this is simply the 5 s poller again.
            log.warning("inotify unavailable (%s); falling back to the %ss poll", exc, POLL_SECONDS)

    def note_own_write(self) -> None:
        self.last_own_write = time.monotonic()

    def wait(self, timeout: float) -> bool:
        """Block up to ``timeout`` seconds; True when the config was just written."""
        if self.fd is None:
            time.sleep(timeout)
            return False
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)
            if not ready:
                return False
            data = os.read(self.fd, 64 * 1024)
        except OSError:
            # A watch can be dropped (directory replaced); sleeping keeps the
            # poller's cadence instead of spinning.
            time.sleep(timeout)
            return False
        hit = False
        offset = 0
        while offset + 16 <= len(data):
            _wd, mask, _cookie, nlen = struct.unpack_from(INOTIFY_EVENT, data, offset)
            name = data[offset + 16:offset + 16 + nlen].split(b"\0", 1)[0]
            offset += 16 + nlen
            if name != self.name or not mask & WATCHED_EVENTS:
                continue
            if time.monotonic() - self.last_own_write < IGNORE_OWN_WRITE_SECONDS:
                continue
            hit = True
        return hit


def single_instance() -> bool:
    """Only one reconciler may poll: two of them fight over the same file.

    Both would see the same drift, both would rewrite it and both would ask the
    core to reload, doubling the reloads (and the connection blips) for no gain.
    A stray manual ``--once``/``-v`` run next to the systemd service was observed
    doing exactly that on 2026-09-20.
    """
    global _instance_lock
    # "a+" and not "w": opening with "w" would truncate the holder's pid before we
    # even try to take the lock, so the "already running" message could not say who.
    _instance_lock = PID_PATH.open("a+")
    try:
        fcntl.flock(_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _instance_lock.seek(0)
        pid = _instance_lock.read().strip()
        print(f"another karing-reconcile is already running (pid {pid or '?'})", file=sys.stderr)
        return False
    _instance_lock.seek(0)
    _instance_lock.truncate()
    _instance_lock.write(f"{os.getpid()}\n")
    _instance_lock.flush()
    return True

log = logging.getLogger("karing-reconcile")


def setup_logging(verbose: bool = False, to_file: bool = True) -> None:
    """Log to stderr always, and to reconcile.log unless this is a read-only run.

    A ``--dry-run`` is how the service's view of the world is inspected by hand; it
    must not leave "karing-reconcile started" lines in the log the service is judged
    by.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    handlers: list[logging.Handler] = [stream]
    if to_file:
        file_handler = logging.FileHandler(LOG_PATH)
        file_handler.setFormatter(fmt)
        handlers.append(file_handler)
    log.handlers[:] = handlers
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def service_port_from_argv(argv: list[str]) -> int:
    """The ``--service-http-port`` in a karingService command line, 0 if absent.

    0 means "the service has not published the port yet", not "no core": see
    ``core_process``, where that distinction is what keeps a reconnect's pending
    verification alive.
    """
    port = None
    for i, arg in enumerate(argv):
        if arg.startswith("--service-http-port="):
            port = arg.split("=", 1)[1]
        elif arg == "--service-http-port" and i + 1 < len(argv):
            port = argv[i + 1]
    return int(port) if port and port.isdigit() else 0


def core_process(report: bool = False) -> tuple[int, int] | None:
    """The running karingService as (pid, service http port), from /proc.

    The port comes back as 0 when the process is there but its command line does
    not carry a readable ``--service-http-port`` yet: the app's service publishes
    that argument a moment *after* it starts.  Measured 2026-09-20 20:08 -- a
    rewrite done 0.77 s after the core appeared read no port, while the same core
    carried a port by 1.9 s.  Treating "no port yet" as "no core" is what made a
    reconnect drop its pending verification instead of waiting for it.

    pgrep is not used on purpose: a shell command line that merely mentions
    ``--service-http-port=`` (this project's own tooling does) would match.
    """
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = [a.decode(errors="replace") for a in (entry / "cmdline").read_bytes().split(b"\0") if a]
        except Exception:
            continue
        if not argv:
            continue
        if not argv[0].endswith("karingService"):
            if report and Path(argv[0]).name.startswith("karing"):
                log.info("pid %s looks like a core but argv[0] is %r", entry.name, argv[0])
            continue
        port = service_port_from_argv(argv)
        if report and not port:
            log.info("pid %s is karingService but has no usable --service-http-port yet: %s", entry.name, argv[1:])
        return int(entry.name), port
    return None


def process_age(pid: int) -> float:
    """Seconds this process has been alive, from /proc/<pid>/stat field 22.

    starttime counts clock ticks since boot, so it has to be subtracted from the
    system uptime.  Returning ``time.time() - ticks/HZ`` (what this did before)
    yields an epoch-sized number, which compares as "old" for every process and
    silently disabled the settle guard it feeds.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        start_ticks = float(stat.rsplit(")", 1)[1].split()[19])
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        return uptime - start_ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        return 0.0


def drift() -> dict:
    """What is wrong with the generated config right now, as a reportable dict."""
    data = sync_rules.load_json(CORE_PATH)
    problems: dict = {}
    svcb = sync_rules.svcb_rule_problem(data)
    if svcb:
        problems["svcb"] = svcb
    for ob in data.get("outbounds") or []:
        if ob.get("tag") == sync_rules.GPT_OUTBOUND:
            members = list(ob.get("outbounds") or [])
            if members != list(sync_rules.GPT_KEEP_TAGS):
                problems["gpt_members"] = {"now": members, "wanted": list(sync_rules.GPT_KEEP_TAGS)}
        if ob.get("type") == "urltest" and ob.get("tolerance") != sync_rules.TUNING["tolerance"]:
            problems.setdefault("tolerance", {})[ob["tag"]] = ob.get("tolerance")
    sane, why = sync_rules.urltest_rules_are_sane(data)
    if not sane:
        problems["unsafe"] = why
    return problems


def reload_core(port: int) -> tuple[bool, str]:
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}/reload"
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            body = resp.read().decode(errors="replace").strip()
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}
        if payload.get("err"):
            return False, str(payload["err"])
        return True, body or f"http {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} {exc.read()[:120]!r}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"reconciles": 0, "reloads": 0, "failures": 0, "last_drift": {}, "last_event": ""}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def reconcile_once(
    state: dict,
    dry_run: bool = False,
    watcher: "ConfigWatcher | None" = None,
) -> bool:
    """Returns True when a reload may be owed to the running core.

    With ``watcher``, a rewrite is followed by a grace period instead of an
    immediate reload: the app reloads the core itself right after it writes, and
    if that reload reads the corrected file there is nothing left to do.
    """
    try:
        problems = drift()
    except FileNotFoundError:
        return False
    except Exception as exc:
        # A half-written file (the app writing non-atomically) is expected now and
        # then; it is not worth an error report.
        log.debug("cannot read %s yet: %s", CORE_PATH.name, exc)
        return False

    if not problems:
        return bool(state.get("pending_reload"))

    log.warning("drift detected: %s", json.dumps(problems, ensure_ascii=False, default=str))
    if dry_run:
        return False

    try:
        result = sync_rules.reconcile()
    except Exception:
        log.exception("reconcile failed")
        state["failures"] = int(state.get("failures") or 0) + 1
        return False
    if watcher:
        # Our rewrite lands as a rename, i.e. as another write event on the very
        # file we watch.  Mark it so the next wait() does not mistake it for the
        # app writing again.
        watcher.note_own_write()

    if not sync_rules.changed(result):
        # Nothing to write, yet the config is still wrong: it is coming from
        # somewhere we cannot reproduce, or a node definition is missing.
        log.error("drift could not be corrected: %s", json.dumps(problems, ensure_ascii=False, default=str))
        state["failures"] = int(state.get("failures") or 0) + 1
        return False

    state["reconciles"] = int(state.get("reconciles") or 0) + 1
    state["last_drift"] = json.loads(json.dumps(problems, default=str))
    state["last_event"] = datetime.now().isoformat(timespec="seconds")
    # A fresh correction earns a fresh reload budget.  Without this the give-up in
    # do_reload() is permanent -- that counter is only cleared by a successful
    # reload, so one bad burst would stop this process reloading for the rest of
    # its life, while still logging that "the next drift retries".
    state["reload_attempts"] = 0
    log.info("rewrote %s", json.dumps(result, ensure_ascii=False, default=str))

    # Never hand the core a config it will reject: a rejected config is not a
    # failed reload, it is the core exiting and the tunnel going down with it.
    still = drift()
    if still.get("unsafe"):
        log.error("core would reject this config, not reloading: %s", still["unsafe"])
        state["failures"] = int(state.get("failures") or 0) + 1
        return False
    if still:
        log.error("config still wrong after the rewrite: %s", json.dumps(still, ensure_ascii=False, default=str))
        state["failures"] = int(state.get("failures") or 0) + 1
        return False

    # The file on disk is right; whether the *running* core has it is a separate
    # question.  The app asks for its own reload right after writing, so give it
    # the grace period before spending a reload of our own (and with it a tun0
    # rebuild) on a config it may already have picked up.
    state["verify_at"] = time.time() + VERIFY_GRACE_SECONDS
    return True


def verify_deadline(state: dict) -> float:
    """The grace deadline, clamped so a wall-clock jump cannot postpone the probe.

    ``verify_at`` is stored as wall-clock time because it has to survive a service
    restart, which means the clock can move under it.  Should it jump backwards,
    the stored deadline stays in the future for as long as the size of the jump --
    and ``resolve_reload`` refuses to probe until it passes, so a correction whose
    app-reload did not take it would sit unverified for that whole time.

    Anything further ahead than ``VERIFY_GRACE_MAX_SECONDS`` is therefore reported
    as 0, meaning "no grace left": the probe runs now instead of the loop waiting
    for the clock to catch up.  Zero is also what an absent deadline returns, and
    both callers already treat that as "probe immediately".
    """
    verify_at = float(state.get("verify_at") or 0)
    if not verify_at:
        return 0.0
    if verify_at - time.time() > VERIFY_GRACE_MAX_SECONDS:
        return 0.0
    return verify_at


def resolve_reload(state: dict) -> bool:
    """Decide whether the corrected config still needs a reload of our own.

    Only a type-65 query that gets no answer after the grace period proves the
    core is running without the rule; a query that answers proves the app's own
    reload took the rewrite, and the reload is skipped.
    """
    verify_at = verify_deadline(state)
    if verify_at and time.time() < verify_at:
        return False
    state["verify_at"] = 0
    good, why = svcb_probe()
    state["last_probe"] = {
        "ok": good,
        "detail": why,
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    race = state.setdefault("race", {"won": 0, "lost": 0})
    if good:
        race["won"] = int(race.get("won") or 0) + 1
        state["pending_reload"] = False
        log.info("type 65 answered before we reloaded (%s); the app's reload carried the rewrite", why)
        save_state(state)
        return False
    race["lost"] = int(race.get("lost") or 0) + 1
    log.info("type 65 still unanswered (%s); reloading", why)
    save_state(state)
    return True


def svcb_probe(name: str = "www.google.com", timeout: float = 8.0) -> tuple[bool, str]:
    """Ask the live resolver for a type-65 record: the only real test of the rule.

    The rule exists for the core, not for the app, so "the JSON has the rule" is
    not proof that it is in effect.  A reload that silently does nothing would
    otherwise look like a success.
    """
    try:
        proc = subprocess.run(
            ["dig", "+short", "+time=3", "+tries=1", "-t", "HTTPS", name],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    answers = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if answers:
        return True, answers[0]
    return False, "no answer"


def reload_budget_spent(state: dict) -> bool:
    """True once this correction has spent its reload attempts and got nowhere.

    The caller clears ``pending_reload`` when this trips, so a reload that will not
    be attempted again stops being probed -- and ``race.lost`` stops being counted
    -- on every poll.  The budget is refilled by the next correction, which is what
    makes "the next drift retries" true.
    """
    return int(state.get("reload_attempts") or 0) >= MAX_RELOAD_ATTEMPTS


def do_reload(state: dict, core: tuple[int, int]) -> None:
    pid, port = core
    if not port:
        # Nothing to call: the service has not published its http port yet.
        log.info("core pid %s has no reload port yet; the reload waits", pid)
        return
    attempts = int(state.get("reload_attempts") or 0)
    if reload_budget_spent(state):
        return
    ok, detail = reload_core(port)
    state["reload_attempts"] = attempts + 1 if not ok else 0
    if ok:
        state["reloads"] = int(state.get("reloads") or 0) + 1
        state["pending_reload"] = False
        log.info("core (pid %s) reloaded via port %s: %s", pid, port, detail)
        good, why = svcb_probe()
        state["last_probe"] = {
            "ok": good,
            "detail": why,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        if good:
            log.info("type 65 probe answered: %s", why)
        else:
            log.warning("type 65 probe got no answer after the reload (%s)", why)
    else:
        state["failures"] = int(state.get("failures") or 0) + 1
        state["pending_reload"] = True
        log.error("reload on port %s failed (%s/%s): %s", port, attempts + 1, MAX_RELOAD_ATTEMPTS, detail)
    save_state(state)


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-assert Karing's generated core config")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="report drift, change nothing")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose, to_file=not args.dry_run)

    # A dry run only reads; it is how the service's view of the world is checked
    # by hand, so it does not need the lock.
    if not args.dry_run and not single_instance():
        return 1

    state = load_state()
    watcher = ConfigWatcher(CORE_PATH)
    log.info("karing-reconcile started (poll %ss)", POLL_SECONDS)
    while True:
        # A correction that has just gone in is checked against the live resolver
        # sooner than the poll interval; everything else waits for the next tick.
        # --once must not sit in select() first.
        timeout = 0.0 if args.once else POLL_SECONDS
        verify_at = verify_deadline(state)
        if verify_at and not args.once:
            # Only a deadline still in the future may shorten the wait.  A stale one
            # must not pin the loop to its 0.2 s floor: that is what logged the same
            # "waiting" line five times a second on 2026-09-20 20:04.  And one left
            # far in the future by a wall-clock jump is not honoured at all -- see
            # verify_deadline().
            remaining = verify_at - time.time()
            if remaining > 0:
                timeout = max(0.2, min(POLL_SECONDS, remaining))
        hot = watcher.wait(timeout)
        if hot:
            log.debug("the app rewrote %s; correcting it inside its own write window", CORE_PATH.name)
        core = core_process()
        try:
            owed = reconcile_once(state, args.dry_run, watcher=watcher)
            if owed and core:
                state["pending_reload"] = True
                save_state(state)
            elif owed:
                # No core at all to reload: the next one reads the corrected file
                # when it starts, so nothing is owed.  Clearing it here also stops
                # the loop from repeating the same line every cycle while the
                # tunnel is down.  Report what the scan saw, because "no core"
                # right after a connect is the one case that has surprised us.
                log.info("config corrected with no core running; the next one reads the file")
                core_process(report=True)
                state["pending_reload"] = False
                state["verify_at"] = 0
                save_state(state)
            if state.get("pending_reload"):
                if not core:
                    log.debug("core exited before the reload; the next one reads the file")
                elif not core[1]:
                    # The core is up but has not published its http port, so there is
                    # nothing to call yet.  Drop the race deadline with it: the app's
                    # own reload has already gone out either way, and the 5 s poll is
                    # enough for the port to show up.  Keeping verify_at would hold the
                    # loop at its 0.2 s floor and repeat this line 5x a second.
                    log.info("core pid %s has not published its http port yet; waiting", core[0])
                    state["verify_at"] = 0
                    save_state(state)
                elif process_age(core[0]) < CORE_SETTLE_SECONDS:
                    log.debug("core pid %s still settling (%.1fs)", core[0], process_age(core[0]))
                elif reload_budget_spent(state):
                    # The file is right and the core will not take it.  Stop asking:
                    # leaving pending_reload set would re-probe (and count another
                    # race lost) every poll for a reload that is not coming.  The next
                    # correction refills the budget, so the drift is retried then.
                    log.error(
                        "core pid %s has not taken the corrected config after %s reload attempts; "
                        "waiting for the next drift", core[0], state.get("reload_attempts"),
                    )
                    state["pending_reload"] = False
                    state["verify_at"] = 0
                    save_state(state)
                elif resolve_reload(state):
                    do_reload(state, core)
        except Exception:
            log.exception("cycle failed")
            state["failures"] = int(state.get("failures") or 0) + 1
            save_state(state)
        if args.once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
