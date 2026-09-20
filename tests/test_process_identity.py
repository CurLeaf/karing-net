#!/usr/bin/env python3
"""Process identity and the debug window's budget.

Both of these are about *not* believing the first thing you see:

* the watcher used to search each process's whole command line for the string
  ``karingService``, so any process that merely mentioned the name -- Cursor's
  sandbox wrapper bash carries the entire command text in its argv, and a grep or
  an editor opening service.json does the same -- was recorded as a core start.
  Those processes come and go inside one sweep, so the pid set flipped on every
  sweep and every flip bought another debug window.  That is what made the core
  look permanently stuck at ``debug``.
* the debug window used to be extended on every trigger without saying so, so a
  fault that repeated for an hour bought an hour of ``debug``.

Nothing here writes Karing's configuration.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Checks, load  # noqa: E402

tw = load("tunnel-watch.py")
checks = Checks()

# --------------------------------------------------------------------------
# karing_kind: what decides, and what only vetoes
# --------------------------------------------------------------------------
CASES = [
    # (argv0, exe link, expected)
    # The real pair on this machine.  The core runs as root, so its exe link is
    # NOT readable (verified: PermissionError) -- the verdict has to survive that.
    ("/opt/karing/karingService", "", "core"),
    ("/opt/karing/karing", "/opt/karing/karing", "gui"),
    ("karingService", "", "core"),
    # Upgraded in place: the link carries the kernel's " (deleted)" suffix.
    ("/opt/karing/karingService", "/opt/karing/karingService (deleted)", "core"),
    # The regression: Cursor's sandbox wrapper bash, whose argv holds the text of
    # a command that named the binary.  argv[0] is the shell, so it never gets in.
    ("/bin/bash", "/usr/bin/bash", ""),
    ("/usr/bin/python3", "/usr/bin/python3", ""),
    ("/usr/bin/grep", "/usr/bin/grep", ""),
    # argv[0] rewritten to look like the core, but the kernel knows what is
    # actually running.  The exe link is a veto, never a way in.
    ("karingService", "/usr/bin/sleep", ""),
    ("/opt/karing/karing", "/usr/bin/bash", ""),
    # A substring of the name is not the name.
    ("karingService-helper", "", ""),
    ("/opt/karing/karingService.bak", "", ""),
    ("", "", ""),
]
for argv0, exe, want in CASES:
    got = tw.karing_kind(argv0, exe)
    checks.check(got == want, f"karing_kind({argv0!r}, {exe!r}) == {want!r}", f"got {got!r}")

# --------------------------------------------------------------------------
# karing_processes: the live machine, plus a live impostor
# --------------------------------------------------------------------------
# A process whose command line names the binary must not qualify.  Every case
# below is checked to be *alive and actually carrying the string* before the
# verdict is read: a process that died, or one bash replaced with `exec`, would
# make the check pass for the wrong reason (bash rewrites its own argv to `sleep`
# when the script is a single command, and `sleep` rejects a stray operand).
# A real executable whose *file name* is the core's name.  This is the case the
# exact-basename rule alone cannot reject: argv[0] is a truthful path and the exe
# link agrees with it.  Measured on the live service 2026-09-20 21:36, a copy
# under ~/.cache/karing-fakecore/ was taken for the core and bought a 90 s debug
# window plus a route-table dump -- the noise class this file exists to remove,
# through a door the name check left open.  core_proof() is what closes it, so the
# test below asserts that the name layer *does* accept this shape.
#
# Two properties of the host have to be worked around to build it: /tmp is a
# noexec tmpfs, so the binary lives under ~/.cache; and /bin/sleep here is a
# coreutils multi-call that refuses to run under an unknown name ("unknown program
# 'karingService'"), so a copy of the interpreter is used instead -- it ignores
# argv[0] and was verified to run renamed.
fake_dir = Path(tempfile.mkdtemp(prefix="karing-impostor-", dir=Path.home() / ".cache"))
fake_core = fake_dir / "karingService"
shutil.copy2(Path("/usr/bin/python3").resolve(), fake_core)

IMPOSTORS = [
    ("the Cursor shape: a shell whose argv holds the text",
     ["/bin/bash", "-c", "sleep 30; true # karingService"], None),
    ("an interpreter with the name in a later argv entry",
     ["/usr/bin/python3", "-c", "import time; time.sleep(30)", "karingService"], None),
    ("argv[0] rewritten to the core's name, exe left as sleep",
     ["karingService", "30"], "/bin/sleep"),
    ("a real file whose name is exactly the core's",
     [str(fake_core), "-c", "import time; time.sleep(30)"], None),
]
procs: list[tuple[str, subprocess.Popen]] = []
for label, args, exe in IMPOSTORS:
    procs.append((label, subprocess.Popen(
        args, executable=exe, start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)))
# karing_processes() reports a refused impostor through emit(), which writes to
# the *live* watcher log.  Capture it instead, so running the tests cannot put
# lines into the timeline the service is judged by.
captured: list[str] = []
tw.emit = lambda kind, msg: captured.append(f"[{kind}] {msg}")
try:
    time.sleep(0.5)
    for label, proc in procs:
        checks.check(proc.poll() is None, f"impostor is running: {label}", f"pid={proc.pid}")
        if proc.poll() is not None:
            # Do not read /proc for a process that already exited: that turns a
            # clear "this impostor did not start" into a traceback.
            continue
        raw = Path(f"/proc/{proc.pid}/cmdline").read_bytes()
        cmd = b" ".join(x for x in raw.split(b"\0") if x).decode("utf-8", "replace")
        checks.check("karingService" in cmd,
                     f"impostor really carries the name: {label}", f"{cmd[:60]!r}")
    # The name layer alone accepts the fake binary: argv[0] is a truthful path and
    # its exe link agrees, so nothing there separates it from the core.  The
    # rejection below therefore has to come from core_proof(), and asserting that
    # the name layer accepts it keeps this test honest about which half does what.
    checks.check(tw.karing_kind(str(fake_core), str(fake_core)) == "core",
                 "the name layer alone accepts a file called karingService",
                 f"{tw.karing_kind(str(fake_core), str(fake_core))!r}")
    found = tw.karing_processes()
    fake_proc = next(proc for label, proc in procs
                     if label == "a real file whose name is exactly the core's")
    ok, why = tw.core_proof(fake_proc.pid, set())
    checks.check(not ok, "core_proof refuses it: not setuid, not the GUI's child, "
                         "not under the install dir", why)
    for label, proc in procs:
        checks.check(proc.pid not in found,
                     f"not mistaken for Karing: {label}", f"pid={proc.pid}")
    checks.check(not any(proc.pid in found for _, proc in procs),
                 f"all {len(procs)} impostors rejected, though every one of them "
                 "would pass the old whole-command-line search")
    checks.check(all(v.startswith(("core:", "gui:")) for v in found.values()),
                 "every entry is labelled core/gui", f"{sorted(found.values())}")
    checks.check(all(Path(f"/proc/{pid}").exists() for pid in found),
                 "reported pids exist", f"{sorted(found)}")
    checks.check(any("was rejected" in line for line in captured),
                 "the refusal is written once to the timeline, with its reason",
                 f"{captured[:1]}")
finally:
    for _, proc in procs:
        proc.kill()
        proc.wait()
    shutil.rmtree(fake_dir, ignore_errors=True)

# --------------------------------------------------------------------------
# core_proof: the real core has to keep getting in
# --------------------------------------------------------------------------
# This direction matters more than the regression above.  The core's exe link is
# unreadable because it is setuid, so a proof that *required* a readable exe would
# lose the core entirely, while trusting the name alone is what let the fake in.
# These two checks pin down both ends.
live = tw.karing_processes()
gui_pids = {pid for pid, label in live.items() if label.startswith("gui")}
cores = sorted(pid for pid, label in live.items() if label.startswith("core"))
if cores:
    ok, why = tw.core_proof(cores[0], gui_pids)
    checks.check(ok, "the live core is accepted by core_proof", f"pid={cores[0]}: {why}")
    checks.check(tw.proc_exe(cores[0]) == "",
                 "…and its exe link really is unreadable, so the proof cannot rely on it",
                 f"exe={tw.proc_exe(cores[0])!r}")
else:
    checks.check(True, "no live core running; the positive path was skipped")

# --------------------------------------------------------------------------
# ProcessSet: confirmation, and the escape hatch for a set that never settles
# --------------------------------------------------------------------------
tracker = tw.ProcessSet()
a = {1: "core: a"}
ab = {1: "core: a", 2: "gui: b"}
checks.check(tracker.note(a, 0.0) is None, "a changed set is held back on its first sweep")
first = tracker.note(a, 10.0)
checks.check(first is not None and first.core_changed,
             "the second identical sweep confirms it, as a core change",
             f"{first.events() if first else None}")
checks.check(tracker.note(a, 20.0) is None, "an unchanged set produces nothing")
checks.check(tracker.note(ab, 30.0) is None, "a changed set is held back again")
change = tracker.note(ab, 40.0)
checks.check(change is not None and change.core_changed is False,
             "a gui arriving is confirmed on the second sweep and is not a core change")
checks.check(change is not None and len(change.events()) == 1
             and "pid=2" in change.events()[0],
             "the event names the pid that arrived",
             f"{change.events() if change else None}")

# The same shape as the bug: something that appears and disappears between sweeps
# must produce nothing at all, no matter how often it happens.
flap = tw.ProcessSet()
flap.current = a
seen = [flap.note(a if i % 2 else ab, float(i * 10)) for i in range(8)]
checks.check(all(x is None for x in seen),
             "a set that keeps alternating under the confirm window claims nothing")

# ...and it must not be able to hide a real change forever either.
forced = tw.ProcessSet()
forced.current = a
forced.note(ab, 0.0)
late = forced.note(ab, tw.PROC_CONFIRM_MAX_SECONDS + 1.0)
checks.check(late is not None and late.forced,
             "a set that never settles is accepted after "
             f"{tw.PROC_CONFIRM_MAX_SECONDS:.0f}s")

# --------------------------------------------------------------------------
# CoreLogStream: the window is bought, not extended
# --------------------------------------------------------------------------
logged: list[str] = []
tw.emit = lambda kind, msg: logged.append(f"[{kind}] {msg}")   # type: ignore[assignment]

core = tw.CoreLogStream()
checks.check(core.desired_level() == tw.LOG_LEVEL_BASE, "starts at the base level")
checks.check(abs(core.debug_left() - tw.LOG_DEBUG_BURST) < 1.0,
             "the pool starts full", f"{core.debug_left():.1f}s")

core.escalate("probe failed (tcp_timeout)")
first_until = core._until
checks.check(core.desired_level() == tw.LOG_LEVEL_ANOMALY, "a failure opens a window")
checks.check(abs((first_until - time.monotonic()) - tw.LOG_ESCALATE_SECONDS) < 1.0,
             f"the window is {tw.LOG_ESCALATE_SECONDS:.0f}s",
             f"{first_until - time.monotonic():.1f}s")
checks.check(abs(core.debug_left() - (tw.LOG_DEBUG_BURST - tw.LOG_ESCALATE_SECONDS)) < 1.0,
             "the window was paid for out of the pool", f"{core.debug_left():.1f}s")

# The silent extension: the same fault, still inside its window, buys nothing and
# says nothing.  This is what used to push the deadline out invisibly.
before = len(logged)
core.escalate("probe failed (tcp_timeout)")
checks.check(core._until == first_until, "a repeated fault does not extend the window")
checks.check(len(logged) == before, "a repeated fault does not claim to have extended it")

# A different fault is a real new event and is announced with the seconds it bought.
core.escalate("tun0 rebuilt")
checks.check(core._until > first_until, "a new fault extends the window")
checks.check(any("window +" in line for line in logged[before:]),
             "the extension is logged with its seconds", f"{logged[before:]}")

# Drain it: escalate() must stop buying, and the level must fall back by itself.
drained = tw.CoreLogStream()
drained._tokens, drained._tokens_at = 0.0, time.monotonic()
logged.clear()
drained.escalate("probe failed (tcp_timeout)")
checks.check(drained.desired_level() == tw.LOG_LEVEL_BASE,
             "an empty pool leaves the level at the base")
checks.check(any("debug pool empty" in line for line in logged),
             "the refusal is reported once", f"{logged}")
logged.clear()
drained.escalate("probe failed (tcp_timeout)")
checks.check(not logged, "the refusal is rate limited, not repeated every probe")

# A full pool can only buy the pool, however many faults arrive.
spree = tw.CoreLogStream()
bought = 0.0
for i in range(20):
    before_tokens = spree.debug_left()
    spree.escalate(f"fault {i}")
    bought += max(0.0, before_tokens - spree.debug_left())
checks.check(bought <= tw.LOG_DEBUG_BURST + 1.0,
             f"20 faults buy at most the {tw.LOG_DEBUG_BURST:.0f}s pool",
             f"bought {bought:.0f}s")
checks.check(spree.desired_level() == tw.LOG_LEVEL_ANOMALY
             and spree._until - time.monotonic() <= tw.LOG_ESCALATE_SECONDS + 1.0,
             "what is left is at most the last window bought, never more",
             f"{spree._until - time.monotonic():.1f}s left")
checks.check(abs(spree.debug_left()) < 1.0, "the pool is exhausted", f"{spree.debug_left():.1f}s")

# The pool refills, but slowly: a quiet ten minutes is worth a fresh burst.
refilled = tw.CoreLogStream()
refilled._tokens, refilled._tokens_at = 0.0, time.monotonic() - 10_000.0
checks.check(abs(refilled.debug_left() - tw.LOG_DEBUG_BURST) < 1.0,
             "the pool refills, capped at the burst", f"{refilled.debug_left():.1f}s")
checks.check(tw.LOG_DEBUG_REFILL * 60 < tw.LOG_ESCALATE_SEVERE_SECONDS,
             "refill is slower than one severe window, so it cannot stay on",
             f"{tw.LOG_DEBUG_REFILL * 60:.1f}s of debug per minute")

# Severe asks for more than ordinary, and still comes out of the same pool.
severe = tw.CoreLogStream()
severe.escalate("tun0 destroyed", severe=True)
checks.check(abs((severe._until - time.monotonic()) - tw.LOG_ESCALATE_SEVERE_SECONDS) < 1.0,
             "a severe fault buys the longer window",
             f"{severe._until - time.monotonic():.1f}s")

sys.exit(checks.finish())
