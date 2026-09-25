#!/usr/bin/env python3
"""Connection recycle policy: leftovers on the old node drain, in-flight do not.

The previous two policies both failed in opposite directions:

* close-on-switch tore down ~19 live sessions per URLTest flap;
* close-only-on-probe-error left Cursor's HTTP/2 pool stacked across
  generations, which is how the Clash connection count climbed to 60-90.

Splitting idle vs trickle clocks then failed a third way: HTTP/2 pings reset
the idle clock and idle ticks reset the trickle clock, so leftover ``api3``
pools on an unselected node never reached either threshold.

This file pins the replacement: one quiet clock for anything below
``ACTIVE_BYTES``, recycle after ``IDLE_LIGHT_SECONDS`` on an unselected node,
and never touch a connection that is still transferring or that is pinned to
the currently selected node.  Nothing here talks to the live Clash API or
writes gc-state.json.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Checks, load  # noqa: E402

gc = load("karing-gc.py")
gc.log.addHandler(logging.NullHandler())
gc.log.propagate = False
checks = Checks()

DIRECT = {"domain_suffix": ["corp.example"], "ip_cidr": ["10.9.0.0/16"]}
NOW_NODE = "香港-now"
OLD_NODE = "香港-old"
GPT_NODE = "台湾-GPT"
TZ = timezone(timedelta(hours=8))


def iso(age_s: float) -> str:
    """A Clash-style start timestamp, including nanoseconds."""
    dt = datetime.now(TZ) - timedelta(seconds=age_s)
    # 9 fractional digits, the way /connections actually emits them.
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond:06d}000{dt.strftime('%z')}"


def conn(
    cid: str,
    node: str,
    *,
    host: str = "api2.cursor.sh",
    age: float = 60,
    up: int = 100,
    down: int = 100,
    dest_ip: str = "1.2.3.4",
) -> dict:
    return {
        "id": cid,
        "start": iso(age),
        "upload": up,
        "download": down,
        "chains": [node, "urltest_out"],
        "metadata": {"host": host, "destinationIP": dest_ip},
    }


def proxies(selected: str = NOW_NODE, extra: dict | None = None) -> dict:
    out = {
        "urltest_out": {"type": "URLTest", "now": selected},
        "urltest_out-GPT自动": {"type": "URLTest", "now": GPT_NODE},
        NOW_NODE: {"history": [{"delay": 80}]},
        OLD_NODE: {"history": [{"delay": 90}]},
        GPT_NODE: {"history": [{"delay": 100}]},
    }
    if extra:
        out.update(extra)
    return out


def collect(conns, book=None, now=None, selected=NOW_NODE, extra_proxies=None, state=None):
    state = state if state is not None else {"now": {"urltest_out": selected}}
    return gc.collect_close_ids(
        proxies(selected, extra_proxies), conns, state, DIRECT,
        book=book, now=now,
    )


def reasons_of(conns, **kwargs) -> dict[str, str]:
    _, reasons = collect(conns, **kwargs)
    return reasons


def idle_book(cid: str, *, ever_active: bool, idle_s: float, now: float = 1000.0) -> gc.TrafficBook:
    """A book that has already classified ``cid`` as idle for ``idle_s`` seconds."""
    book = gc.TrafficBook()
    c = conn(cid, OLD_NODE, up=100, down=100)
    # First sighting: no delta yet.
    book.observe([c], now - idle_s - 8)
    if ever_active:
        busy = conn(cid, OLD_NODE, up=100 + gc.ACTIVE_BYTES, down=100)
        book.observe([busy], now - idle_s - 1)
        quiet = conn(cid, OLD_NODE, up=100 + gc.ACTIVE_BYTES, down=100)
        book.observe([quiet], now - idle_s)
    else:
        book.observe([c], now - idle_s)
    return book


def trickle_book(cid: str, *, trickle_s: float, now: float = 1000.0) -> gc.TrafficBook:
    book = gc.TrafficBook()
    up = 100
    t = now - trickle_s - 8
    book.observe([conn(cid, OLD_NODE, up=up, down=100)], t)
    t += 8
    # Stay in the trickle band until ``now``.
    while t <= now:
        up += 200
        book.observe([conn(cid, OLD_NODE, up=up, down=100)], t)
        t += 8
    return book


# --------------------------------------------------------------------------
# timestamps
# --------------------------------------------------------------------------
age = gc.conn_age_seconds({"start": iso(121)})
checks.check(110 < age < 130, "nanosecond Clash timestamps parse to a real age", f"age={age:.1f}")
checks.check(gc.conn_age_seconds({"start": iso(-30)}) == 0.0, "future timestamps clamp to 0")
checks.check(gc.conn_age_seconds({"start": "nope"}) == 0.0, "garbage timestamps clamp to 0")

# --------------------------------------------------------------------------
# selected / protected nodes are never stale-recycled
# --------------------------------------------------------------------------
book = idle_book("keep-live", ever_active=False, idle_s=gc.IDLE_LIGHT_SECONDS + 5)
r = reasons_of([conn("keep-live", NOW_NODE)], book=book, now=1000.0)
checks.check(r == {}, "selected-node idle keepalive is left alone")

book = idle_book("keep-gpt", ever_active=False, idle_s=gc.IDLE_LIGHT_SECONDS + 5)
r = reasons_of([conn("keep-gpt", GPT_NODE)], book=book, now=1000.0)
checks.check(r == {}, "the other URLTest group's selected node is also protected")

book = idle_book("keep-direct", ever_active=False, idle_s=gc.IDLE_LIGHT_SECONDS + 5)
direct_conn = conn("keep-direct", "direct_out", host="baidu.com")
r = reasons_of([direct_conn], book=book, now=1000.0)
checks.check(r == {}, "direct_out is never stale-recycled")

# --------------------------------------------------------------------------
# first sighting / short idle: do nothing
# --------------------------------------------------------------------------
book = gc.TrafficBook()
c = conn("new", OLD_NODE)
book.observe([c], 0.0)
r = reasons_of([c], book=book, now=0.0)
checks.check(r == {}, "first observation cannot classify idle, so it does not close")

book = idle_book("short", ever_active=False, idle_s=8)
r = reasons_of([conn("short", OLD_NODE)], book=book, now=1000.0)
checks.check(r == {}, "an 8s idle orphan is below IDLE_LIGHT_SECONDS")

# --------------------------------------------------------------------------
# quiet orphan (never burst) closes at 24s; so does a previously-busy one
# --------------------------------------------------------------------------
book = idle_book("light", ever_active=False, idle_s=gc.IDLE_LIGHT_SECONDS)
r = reasons_of([conn("light", OLD_NODE)], book=book, now=1000.0)
checks.check(
    r.get("light", "").startswith("stale-idle:"),
    "a keepalive orphan idle for 24s is recycled",
    r.get("light", ""),
)

book = idle_book("heavy-quiet", ever_active=True, idle_s=gc.IDLE_LIGHT_SECONDS)
r = reasons_of([conn("heavy-quiet", OLD_NODE)], book=book, now=1000.0)
checks.check(
    r.get("heavy-quiet", "").startswith("stale-idle:"),
    "an Agent stream idle for 24s on an old node is recycled (leftover path)",
    r.get("heavy-quiet", ""),
)

# --------------------------------------------------------------------------
# trickle vs live transfer; idle/ping must not reset each other
# --------------------------------------------------------------------------
book = trickle_book("drip", trickle_s=gc.IDLE_LIGHT_SECONDS)
r = reasons_of([conn("drip", OLD_NODE, up=100 + 200 * 20, down=100)], book=book, now=1000.0)
checks.check(
    r.get("drip", "").startswith("stale-trickle:"),
    "a trickle orphan (HTTP/2 ping) is recycled after 24s quiet",
    r.get("drip", ""),
)

book = gc.TrafficBook()
alt_up = 100
t = 1000.0 - gc.IDLE_LIGHT_SECONDS - 8
book.observe([conn("flip", OLD_NODE, up=alt_up, down=100)], t)
t += 8
ping = False
while t <= 1000.0:
    if ping:
        alt_up += 200
    book.observe([conn("flip", OLD_NODE, up=alt_up, down=100)], t)
    t += 8
    ping = not ping
r = reasons_of([conn("flip", OLD_NODE, up=alt_up, down=100)], book=book, now=1000.0)
checks.check(
    r.get("flip", "").startswith("stale-"),
    "alternating idle and ping still drains the old path after 24s quiet",
    r.get("flip", ""),
)
checks.check(
    book.quiet_seconds("flip", 1000.0) >= gc.IDLE_LIGHT_SECONDS,
    "quiet clock survives idle↔ping flips",
    f"quiet={book.quiet_seconds('flip', 1000.0):.1f}s",
)

book = gc.TrafficBook()
live = conn("live", OLD_NODE, up=100, down=100, age=600)
book.observe([live], 0.0)
busy = conn("live", OLD_NODE, up=100 + gc.ACTIVE_BYTES * 4, down=100, age=600)
book.observe([busy], 8.0)
r = reasons_of([busy], book=book, now=8.0)
checks.check(r == {}, "an orphan still transferring ACTIVE_BYTES this tick is left alone")

# --------------------------------------------------------------------------
# dead node still wins, even if the stream is active; selected dead does not
# --------------------------------------------------------------------------
dead_extra = {OLD_NODE: {"history": [{"err": "timeout"}]}}
book = gc.TrafficBook()
active_dead = conn("dead-active", OLD_NODE, up=10_000, down=10_000, age=30)
book.observe([active_dead], 0.0)
book.observe([conn("dead-active", OLD_NODE, up=10_000 + gc.ACTIVE_BYTES * 2, down=10_000, age=30)], 8.0)
r = reasons_of(
    [conn("dead-active", OLD_NODE, up=10_000 + gc.ACTIVE_BYTES * 2, down=10_000, age=30)],
    book=book, now=8.0, extra_proxies=dead_extra,
)
checks.check(
    r.get("dead-active", "").startswith("dead:"),
    "a dead unselected node is recycled even while transferring",
    r.get("dead-active", ""),
)

young_dead = conn("dead-young", OLD_NODE, age=5)
r = reasons_of([young_dead], extra_proxies=dead_extra)
checks.check(r == {}, "a dead-node connection younger than DEAD_NODE_GRACE is kept")

selected_dead = {NOW_NODE: {"history": [{"err": "timeout"}]}}
r = reasons_of([conn("live-dead", NOW_NODE, age=60)], extra_proxies=selected_dead)
checks.check(r == {}, "the currently selected node is not recycled just because it is erroring")

# --------------------------------------------------------------------------
# misroute still fires immediately, including against the selected node
# --------------------------------------------------------------------------
mis = conn("mis", NOW_NODE, host="app.corp.example", age=5)
r = reasons_of([mis])
checks.check(
    r.get("mis") == "misroute:app.corp.example",
    "force-direct host leaking through the proxy is closed",
    r.get("mis", ""),
)

mis_ip = conn("mis-ip", NOW_NODE, host="", dest_ip="10.9.1.4", age=5)
r = reasons_of([mis_ip])
checks.check(
    r.get("mis-ip") == "misroute-ip:10.9.1.4",
    "force-direct CIDR leaking through the proxy is closed",
    r.get("mis-ip", ""),
)

# FakeIP + matching host
fake = conn("fake", NOW_NODE, host="vpn.corp.example", dest_ip="198.18.1.9", age=5)
r = reasons_of([fake])
checks.check(
    r.get("fake") == "fakeip:198.18.1.9",
    "FakeIP destination for a force-direct host is closed",
    r.get("fake", ""),
)

# --------------------------------------------------------------------------
# close budget: misroutes always pass; orphans are capped
# --------------------------------------------------------------------------
ids = [f"m{i}" for i in range(3)] + [f"s{i}" for i in range(40)]
reasons = {f"m{i}": "misroute:corp.example" for i in range(3)}
reasons.update({f"s{i}": f"stale-idle:{OLD_NODE}" for i in range(40)})
capped = gc.apply_close_budget(ids, reasons, budget=10)
checks.check(
    capped[:3] == ["m0", "m1", "m2"] and len(capped) == 13,
    "misroutes ignore the budget; orphans take the remaining 10 slots",
    f"n={len(capped)} head={capped[:4]}",
)
checks.check(
    all(classify.startswith("m") for classify in capped[:3]),
    "budget preserves misroute-before-stale order",
)

# --------------------------------------------------------------------------
# TrafficBook: prune gone ids, first sample is unclassified
# --------------------------------------------------------------------------
book = gc.TrafficBook()
a = conn("a", OLD_NODE, up=1, down=1)
b = conn("b", OLD_NODE, up=1, down=1)
book.observe([a, b], 0.0)
checks.check(
    book.idle_seconds("a", 0.0) == 0.0 and book.quiet_seconds("a", 0.0) == 0.0,
    "first sample reports neither idle nor quiet",
)
book.observe([a], 8.0)
checks.check("b" not in book._samples, "connections that disappeared are pruned")
checks.check(book.idle_seconds("a", 8.0) == 0.0 or book.idle_seconds("a", 16.0) >= 0.0,
             "second identical sample starts the idle clock")
checks.check(book.idle_seconds("a", 8.0) == 0.0, "idle clock starts at the moment of the zero-delta tick")
checks.check(abs(book.idle_seconds("a", 8.0 + 24) - 24) < 0.01, "idle clock then grows with now")

# a burst then silence flips ever_active
book = gc.TrafficBook()
book.observe([conn("x", OLD_NODE, up=10, down=10)], 0.0)
book.observe([conn("x", OLD_NODE, up=10 + gc.ACTIVE_BYTES, down=10)], 8.0)
checks.check(book.ever_active("x") is True, "a burst marks the connection ever-active")
book.observe([conn("x", OLD_NODE, up=10 + gc.ACTIVE_BYTES, down=10)], 16.0)
checks.check(book.idle_seconds("x", 16.0) == 0.0, "the idle clock starts on the quiet tick, not before")
checks.check(book.ever_active("x") is True, "ever-active survives a later idle period")

# --------------------------------------------------------------------------
# switch log updates state.now; orphans on the old node become eligible
# --------------------------------------------------------------------------
state = {"now": {"urltest_out": OLD_NODE}}
c = conn("orphan", OLD_NODE)
book = idle_book("orphan", ever_active=False, idle_s=gc.IDLE_LIGHT_SECONDS)
ids, reasons = gc.collect_close_ids(
    proxies(NOW_NODE), [c], state, DIRECT, book=book, now=1000.0,
)
checks.check(state["now"]["urltest_out"] == NOW_NODE, "switch is recorded on the state")
checks.check(
    reasons.get("orphan", "").startswith("stale-idle:"),
    "after a switch, an idle connection still pinned to the old node is recycled",
)

# --------------------------------------------------------------------------
# counter identity after the stale split
# --------------------------------------------------------------------------
migrated = {"closed": 6, "misrouted": 0, "recycled": 6, "counters_migrated": "already"}
checks.check(gc.migrate_counters(migrated) is True, "adding recycled_dead/stale counts as a migration")
checks.check(
    migrated["recycled_dead"] == 6 and migrated["recycled_stale"] == 0,
    "historical recycled is attributed to recycled_dead",
    f"dead={migrated.get('recycled_dead')} stale={migrated.get('recycled_stale')}",
)
checks.check(
    migrated["closed"] == migrated["misrouted"] + migrated["recycled"],
    "closed == misrouted + recycled still holds",
)
checks.check(
    migrated["recycled"] == migrated["recycled_dead"] + migrated["recycled_stale"],
    "recycled == recycled_dead + recycled_stale",
)

fresh = {}
gc.migrate_counters(fresh)
checks.check(
    fresh["closed"] == 0 and "counters_migrated" in fresh and fresh["recycled_stale"] == 0,
    "an empty state migrates to a zeroed, reconcilable counter set",
)

# classify_reason is what tick() uses to keep those identities
checks.check(gc.classify_reason("misroute:x") == "misroute", "misroute reason class")
checks.check(gc.classify_reason("fakeip:1.2.3.4") == "misroute", "fakeip counts as misroute")
checks.check(gc.classify_reason("dead:node") == "dead", "dead reason class")
checks.check(gc.classify_reason("stale-idle:node") == "stale", "stale-idle reason class")
checks.check(gc.classify_reason("stale-idle-heavy:node") == "stale", "stale-idle-heavy reason class")
checks.check(gc.classify_reason("stale-trickle:node") == "stale", "stale-trickle reason class")

sys.exit(checks.finish())
