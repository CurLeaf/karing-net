#!/usr/bin/env python3
"""The reload budget: spent by a failing core, refilled by the next correction.

Two behaviours are load-bearing and were both wrong before:

* a correction has to refill ``reload_attempts``, otherwise one bad burst stops
  this process from ever reloading again (the counter used to be cleared only by a
  successful reload) while still logging "the next drift retries";
* ``reload_budget_spent()`` has to be true at the cap, so the caller can stop
  probing a reload that is not going to be attempted -- every extra probe counted
  another ``race.lost`` and logged another "reloading" line for nothing.

``sync_rules.reconcile`` and ``drift`` are stubbed, so nothing here touches Karing's
config or the reconciler's state file.
"""
from __future__ import annotations

import logging
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Checks, load  # noqa: E402

kr = load("karing-reconcile.py")
# reconcile_once warns about the drift it found; the test is not the place to print it.
kr.log.addHandler(logging.NullHandler())
kr.log.propagate = False
checks = Checks()

problems: dict = {}


def fake_reconcile() -> dict:
    """Stand in for the real writer: the drift is gone once it has run."""
    problems.clear()
    return {"svcb": {"inserted_at": 2}}


kr.drift = lambda: dict(problems)
kr.sync_rules = types.SimpleNamespace(reconcile=fake_reconcile, changed=lambda result: True)

checks.check(kr.reload_budget_spent({}) is False, "no counter at all: budget not spent")
for n in range(kr.MAX_RELOAD_ATTEMPTS):
    checks.check(kr.reload_budget_spent({"reload_attempts": n}) is False, f"{n} attempts: budget not spent")
checks.check(
    kr.reload_budget_spent({"reload_attempts": kr.MAX_RELOAD_ATTEMPTS}) is True,
    f"{kr.MAX_RELOAD_ATTEMPTS} attempts: budget spent",
)
checks.check(kr.reload_budget_spent({"reload_attempts": 99}) is True, "past the cap: budget spent")

# A correction refills it, and owes a reload.
problems.update({"svcb": "missing"})
state = {"reload_attempts": kr.MAX_RELOAD_ATTEMPTS, "reconciles": 0, "pending_reload": True}
owed = kr.reconcile_once(state, dry_run=False)
checks.check(owed is True, "a correction still owes a reload")
checks.check(state["reload_attempts"] == 0, "the correction refilled the reload budget", f"now {state['reload_attempts']}")
checks.check(state["reconciles"] == 1, "the correction was counted", f"reconciles={state['reconciles']}")
checks.check(
    float(state.get("verify_at") or 0) > time.time(),
    "a race deadline was set (the app's own reload gets its grace period)",
)

# Nothing wrong and nothing pending: no correction, so no refill either.
problems.clear()
kept = {"reload_attempts": kr.MAX_RELOAD_ATTEMPTS, "pending_reload": False}
checks.check(kr.reconcile_once(kept, dry_run=False) is False, "nothing to do: no reload owed")
checks.check(
    kept["reload_attempts"] == kr.MAX_RELOAD_ATTEMPTS,
    "an idle pass does not refill the budget",
    f"still {kept['reload_attempts']}",
)

# A dry run reports and changes nothing, budget included.
problems.update({"svcb": "missing"})
dry = {"reload_attempts": kr.MAX_RELOAD_ATTEMPTS, "pending_reload": False}
checks.check(kr.reconcile_once(dry, dry_run=True) is False, "dry run: never owes a reload")
checks.check(dry["reload_attempts"] == kr.MAX_RELOAD_ATTEMPTS, "dry run: budget untouched")
checks.check(bool(problems), "dry run: the drift is still there afterwards")

sys.exit(checks.finish())
