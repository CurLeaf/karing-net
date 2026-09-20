#!/usr/bin/env python3
"""The reload decision, without a live tunnel.

Inside the grace period the app's own reload is given its chance and no probe is
spent.  After it, a type-65 answer means the app carried the rewrite (race won, no
reload of ours); no answer means we reload (race lost).  The real state file is
redirected to a temporary path.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Checks, load  # noqa: E402

kr = load("karing-reconcile.py")
checks = Checks()

tmp = Path(tempfile.mkdtemp(prefix="karing-resolve-"))
kr.STATE_PATH = tmp / "reconcile-state.json"

calls: list[str] = []


def answered(name: str = "www.google.com", timeout: float = 8.0):
    calls.append(name)
    return True, '1 . alpn="h2"'


def unanswered(name: str = "www.google.com", timeout: float = 8.0):
    calls.append(name)
    return False, "no answer"


kr.svcb_probe = answered
state = {"verify_at": time.time() + 10, "pending_reload": True}
checks.check(kr.resolve_reload(state) is False, "inside the grace: no reload", f"probes so far {len(calls)}")
checks.check(len(calls) == 0, "inside the grace: no probe spent", f"probes {len(calls)}")

state["verify_at"] = time.time() - 1
reload_now = kr.resolve_reload(state)
checks.check(reload_now is False, "answered after the grace: no reload of ours")
checks.check(state["pending_reload"] is False, "answered after the grace: nothing stays pending")
checks.check(state["race"] == {"won": 1, "lost": 0}, "counted as a race won", json.dumps(state["race"]))
checks.check(len(calls) == 1, "exactly one probe spent", f"probes {len(calls)}")
checks.check(kr.STATE_PATH.exists(), "state written to the redirected path", str(kr.STATE_PATH))

kr.svcb_probe = unanswered
state2 = {"verify_at": time.time() - 1, "pending_reload": True}
checks.check(kr.resolve_reload(state2) is True, "unanswered after the grace: reload owed")
checks.check(state2["race"] == {"won": 0, "lost": 1}, "counted as a race lost", json.dumps(state2["race"]))
checks.check(state2["verify_at"] == 0, "the deadline is cleared once decided")
checks.check(
    kr.resolve_reload({"pending_reload": True, "race": dict(state2["race"])}) is True,
    "no deadline at all: decides immediately (fresh core, corrected file)",
)

# --------------------------------------------------------------------------
# verify_deadline: the wall clock is allowed to move under a persisted deadline
# --------------------------------------------------------------------------
checks.check(kr.VERIFY_GRACE_MAX_SECONDS > kr.VERIFY_GRACE_SECONDS,
             "the clamp is looser than a real grace period, so it cannot cut one short",
             f"{kr.VERIFY_GRACE_SECONDS}s grace vs {kr.VERIFY_GRACE_MAX_SECONDS}s clamp")

within = time.time() + kr.VERIFY_GRACE_SECONDS
checks.check(abs(kr.verify_deadline({"verify_at": within}) - within) < 0.01,
             "a deadline inside the grace is honoured")

# The shape a backwards NTP step leaves behind: a deadline an hour ahead.  It has
# to read as "no grace left" so the probe runs now, instead of a correction whose
# app-reload did not take it sitting unverified until the clock comes back.
jumped = time.time() + 3600
checks.check(kr.verify_deadline({"verify_at": jumped}) == 0.0,
             "a deadline an hour out (clock jumped back) is not honoured",
             f"verify_at=+3600s -> {kr.verify_deadline({'verify_at': jumped})}")

checks.check(kr.verify_deadline({}) == 0.0, "no deadline reads as no grace")
checks.check(kr.verify_deadline({"verify_at": 0}) == 0.0, "a cleared deadline reads as no grace")

# The effect that matters: a jumped clock must not be able to stop the probe.
kr.svcb_probe = unanswered
calls.clear()
state3 = {"verify_at": jumped, "pending_reload": True}
checks.check(kr.resolve_reload(state3) is True,
             "a jumped clock does not postpone the probe: the reload still happens")
checks.check(len(calls) == 1, "…and the probe was really spent", f"probes {len(calls)}")

sys.exit(checks.finish())
