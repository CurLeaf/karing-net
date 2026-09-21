#!/usr/bin/env python3
"""Tunnel presence: advance the pointer, and identify Karing by address.

Two mistakes produced the 2026-09-21 18:10 popup storm:

* the poll compared ``tun != prev_tun`` but never assigned ``prev_tun = tun``,
  so a single tun0 teardown was DESTROYED every 2 seconds and notified every
  60 (the per-category cooldown).  The process set already requires two
  identical scans before it counts; the tun pointer just forgot to move.
* ``interface_name`` in the core config is empty, so the kernel hands out the
  next free tunN.  Another tun already on the machine (tun1) made the
  reconnect land on tun2 (10.20.0.1/30).  Watching only tun0 then reported a
  permanent teardown of a tunnel that was up.

Nothing here writes Karing's configuration or the live watcher's log.
"""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Checks, load  # noqa: E402

tw = load("tunnel-watch.py")
checks = Checks()


def T(present: bool, ifindex: int = 0, operstate: str = "", name: str = "") -> tw.TunStatus:
    return tw.TunStatus(present, ifindex, operstate, name)


# --------------------------------------------------------------------------
# tun_transition: one change, one event; advancing prev is the whole point
# --------------------------------------------------------------------------
gone = T(False)
alive_tun0 = T(True, 5, "unknown", "tun0")
alive_tun0_new = T(True, 6, "unknown", "tun0")
alive_tun2 = T(True, 10, "unknown", "tun2")

checks.check(tw.tun_transition(alive_tun0, alive_tun0) == (),
             "identical polls produce no event")
checks.check(tw.tun_transition(alive_tun0, gone) == ("destroyed",),
             "present -> absent is destroyed once",
             str(tw.tun_transition(alive_tun0, gone)))
checks.check(tw.tun_transition(gone, alive_tun2) == ("rebuilt",),
             "absent -> present is rebuilt")
checks.check(tw.tun_transition(alive_tun0, alive_tun0_new) == ("rebuilt",),
             "same name, new ifindex is rebuilt (the reload case)")
checks.check(tw.tun_transition(alive_tun0, alive_tun2) == ("rebuilt",),
             "tun0 -> tun2 with the same address is rebuilt, not destroyed",
             str(tw.tun_transition(alive_tun0, alive_tun2)))

# The live bug: prev stuck at the 09:42 baseline (True, 5, tun0).
stuck = []
for _ in range(5):
    stuck.extend(tw.tun_transition(alive_tun0, gone))
checks.check(stuck == ["destroyed"] * 5,
             "forgetting to advance prev retriggers destroyed every poll",
             f"{len(stuck)} events")

advanced = []
state = alive_tun0
for _ in range(5):
    advanced.extend(tw.tun_transition(state, gone))
    state = gone
checks.check(advanced == ["destroyed"],
             "advancing prev fires destroyed once, then silence",
             str(advanced))

source = inspect.getsource(tw.Watcher.main)
checks.check("prev_tun = tun" in source,
             "Watcher.main assigns prev_tun = tun each poll")
checks.check("prev_ports = ports" in source,
             "Watcher.main assigns prev_ports = ports each poll")
notify_src = inspect.getsource(tw.notify)
checks.check('"-u", "critical"' not in notify_src,
             "notify-send is never urgency=critical (GNOME would keep it in the tray)")
checks.check("boolean:transient:1" in notify_src,
             "toasts are transient so they leave the notification list")
checks.check("retract_notify" in source,
             "startup retracts a leftover tunnel toast")


# --------------------------------------------------------------------------
# tun_state: pick the iface that holds Karing's address, ignore the other tun
# --------------------------------------------------------------------------
tmp_net = Path(tempfile.mkdtemp(prefix="karing-tun-net-"))
tmp_karing = Path(tempfile.mkdtemp(prefix="karing-tun-cfg-"))
for name, ifindex in (("tun1", "7"), ("tun2", "10")):
    d = tmp_net / name
    d.mkdir()
    (d / "ifindex").write_text(ifindex + "\n")
    (d / "operstate").write_text("unknown\n")
(tmp_karing / "service_core.json").write_text(json.dumps({
    "inbounds": [{"type": "tun", "interface_name": "", "address": ["10.20.0.1/30"]}],
}))

orig_net = tw.SYS_NET
orig_dir = tw.KARING_DIR
orig_ip = tw.iface_ipv4
orig_cache = tw._tun_addrs_cache
tw.SYS_NET = tmp_net
tw.KARING_DIR = tmp_karing
tw._tun_addrs_cache = None


def fake_ip(name: str) -> str:
    return {"tun1": "10.126.126.2", "tun2": "10.20.0.1"}.get(name, "")


tw.iface_ipv4 = fake_ip  # type: ignore[assignment]
try:
    st = tw.tun_state()
    checks.check(st.present and st.name == "tun2" and st.ifindex == 10,
                 "selects tun2 by 10.20.0.1, not the other tun1",
                 st.render())

    # A foreign tun alone is not Karing's tunnel.
    (tmp_net / "tun2").rename(tmp_net / "tun2.bak")
    tw._tun_addrs_cache = None
    st = tw.tun_state()
    checks.check(not st.present,
                 "tun1 with a different address is not treated as Karing",
                 st.render())
    (tmp_net / "tun2.bak").rename(tmp_net / "tun2")

    # Fall back to tun0 when it exists but has no address yet (just created).
    d0 = tmp_net / "tun0"
    d0.mkdir()
    (d0 / "ifindex").write_text("5\n")
    (d0 / "operstate").write_text("unknown\n")
    (tmp_net / "tun2").rename(tmp_net / "tun2.bak")
    tw.iface_ipv4 = lambda name: ""  # type: ignore[assignment]
    tw._tun_addrs_cache = None
    st = tw.tun_state()
    checks.check(st.present and st.name == "tun0" and st.ifindex == 5,
                 "falls back to tun0 when no address matches yet",
                 st.render())
finally:
    tw.SYS_NET = orig_net
    tw.KARING_DIR = orig_dir
    tw.iface_ipv4 = orig_ip
    tw._tun_addrs_cache = orig_cache


print()
sys.exit(checks.finish())
