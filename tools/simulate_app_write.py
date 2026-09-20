#!/usr/bin/env python3
"""Simulate the app's rewrite of service_core.json and measure the fast path.

    !!! This writes Karing's live core config.  Read this first.  !!!

It does what the app does on a reconnect: truncates ``service_core.json`` in place
(stripping the type-65 rule and resetting the urltest tolerance), then watches how
long the running karing-reconcile takes to put the rule back.  The reconciler will
rewrite the file and, if it does not win the race against the app's own reload, ask
the core to reload -- and a reload tears down and rebuilds ``tun0`` (see the README),
so this can cost one network blip for everything going through the tunnel.

Run it only when a blip is acceptable, and always with ``--yes``:

    python3 ~/Projects/karing-net/tools/simulate_app_write.py --yes

What to look at afterwards:

* the latency it prints (the observed fast path is ~5 ms; the app's own reload
  follows about 100 ms after the write, so anything under that is a win);
* ``race`` in ``~/.local/share/karing-net/reconcile-state.json``: ``won`` should go
  up by one and ``lost`` should not;
* ``grep -c 'tun0 REBUILT' ~/.local/share/karing-net/tunnel-watch.log`` should not
  move if the race was won.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

CORE = Path.home() / ".local/share/com.nebula.karing/service_core.json"
STATE = Path.home() / ".local/share/karing-net/reconcile-state.json"
TUN_LOG = Path.home() / ".local/share/karing-net/tunnel-watch.log"
DEADLINE_SECONDS = 6.0


def svcb_rules(data: dict) -> list[dict]:
    return [
        r for r in ((data.get("dns") or {}).get("rules") or [])
        if sorted(str(t).upper() for t in (r.get("query_type") or [])) == ["HTTPS", "SVCB"]
    ]


def race_counts() -> dict:
    try:
        return json.loads(STATE.read_text()).get("race") or {}
    except Exception:
        return {}


def rebuilt_count() -> int:
    try:
        return sum(1 for line in TUN_LOG.read_text(errors="replace").splitlines() if "tun0 REBUILT" in line)
    except Exception:
        return -1


def main() -> int:
    if "--yes" not in sys.argv:
        print(__doc__)
        print("refusing to run without --yes")
        return 2
    if not CORE.exists():
        print(f"{CORE} does not exist; is Karing connected?")
        return 1

    before_race, before_rebuilt = race_counts(), rebuilt_count()
    want = json.loads(CORE.read_text())
    if not svcb_rules(want):
        print("the live config already has no type-65 rule; nothing to simulate against")
        return 1

    drifted = json.loads(CORE.read_text())
    drifted["dns"]["rules"] = [r for r in drifted["dns"]["rules"] if r not in svcb_rules(drifted)]
    for ob in drifted.get("outbounds") or []:
        if ob.get("tag") == "urltest_out-GPT自动":
            ob["tolerance"] = 120
    payload = (json.dumps(drifted, ensure_ascii=False, indent=2) + "\n").encode()
    print(f"{datetime.now():%F %T} simulating the app: "
          f"{len(want['dns']['rules'])} -> {len(drifted['dns']['rules'])} DNS rules, {len(payload)} bytes")

    # The app's own pattern: O_TRUNC in place.  close() is the moment the race starts.
    fd = os.open(CORE, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC)
    os.write(fd, payload)
    os.close(fd)
    t0 = time.monotonic()

    restored_after = None
    while time.monotonic() - t0 < DEADLINE_SECONDS:
        try:
            now = json.loads(CORE.read_text())
        except Exception:
            time.sleep(0.002)
            continue
        if svcb_rules(now):
            restored_after = (time.monotonic() - t0) * 1000
            break
        time.sleep(0.002)

    if restored_after is None:
        print(f"NOT restored within {DEADLINE_SECONDS:.0f} s -- the fast path did not fire")
    else:
        print(f"rule restored after {restored_after:.0f} ms")

    final = json.loads(CORE.read_text())
    print("tolerance now:", [ob.get("tolerance") for ob in final["outbounds"]
                             if ob.get("tag") == "urltest_out-GPT自动"])
    print("identical to the pre-test config:", json.dumps(final, sort_keys=True) == json.dumps(want, sort_keys=True))

    time.sleep(3.0)  # let the grace period and any reload finish
    after_race, after_rebuilt = race_counts(), rebuilt_count()
    print(f"race: {before_race} -> {after_race}")
    print(f"tun0 REBUILT count: {before_rebuilt} -> {after_rebuilt} "
          f"(unchanged means the app's own reload carried the correction)")
    return 0 if restored_after is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
