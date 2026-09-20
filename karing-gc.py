#!/usr/bin/env python3
"""Recycle stale Karing connections and keep force-direct domains off the proxy.

History / rationale
-------------------
An earlier version closed *every* connection pinned to the previously selected
node whenever a URLTest group switched.  The node pool flaps a lot (the same
Hong-Kong nodes trade places within the 120 ms tolerance), so that policy tore
down ~19 live connections on average per switch -- which the user experiences as
"the network keeps dropping".

sing-box itself runs these groups with ``interrupt_exist_connections = false``,
i.e. it deliberately keeps existing sessions alive across a switch.  So the
aggressive close was fighting the core and causing the outages.

Current policy
--------------
* A URLTest switch is only *logged*, never acted upon.
* A connection is closed only when the node it is pinned to has an explicit
  test *error* (not merely a slow/zero delay), is no longer selected, and has
  been alive long enough to be worth recycling.
* Force-direct domains/IPs that somehow egress through the proxy are still
  torn down immediately (that is the protection the helper exists for).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_rules

KARING_DIR = Path.home() / ".local/share/com.nebula.karing"
SERVICE_JSON = KARING_DIR / "service.json"
LOG_DIR = Path.home() / ".local/share/karing-net"
STATE_PATH = LOG_DIR / "gc-state.json"
POLL_SECONDS = 8
MIN_AGE_SECONDS = 3
# A node must be failing for this long before its pinned connections are recycled.
DEAD_NODE_GRACE_SECONDS = 20
MISROUTE_AGE_SECONDS = 1
API_TIMEOUT = 3
# FakeIP CLOSE-WAIT reaping is cheap insurance but must not run on every tick.
FAKEIP_CLEAN_EVERY_TICKS = 12
# Re-assert the URLTest anti-flapping values every ~5 minutes.
TUNING_EVERY_TICKS = 38
# The URLTest group list comes from the full ``/proxies`` map (11733 bytes, 65
# entries).  Rediscovering it every ~5 minutes, instead of on every tick, is what
# lets the per-tick reads be just the groups plus the nodes a connection uses.
GROUP_DISCOVERY_TICKS = 38
FAKEIP_NETS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("198.20.0.0/15"))

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("karing-gc")

_tick_count = 0
# Cached URLTest group names, refreshed every GROUP_DISCOVERY_TICKS ticks.
_group_names: list[str] = []
_group_names_at = -10**9


def load_api():
    cfg = json.loads(SERVICE_JSON.read_text())
    port = cfg.get("control_port") or 3057
    secret = cfg.get("secret") or ""
    return f"http://127.0.0.1:{port}", secret


def api(base: str, secret: str, method: str, path: str, timeout: int = API_TIMEOUT):
    req = urllib.request.Request(
        base + path,
        method=method,
        headers={"Authorization": f"Bearer {secret}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        if not body:
            return None
        return json.loads(body.decode())


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"now": {}, "closed": 0, "misrouted": 0, "recycled": 0}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False))


def migrate_counters(state: dict) -> bool:
    """Make ``closed`` add up, once, and retire the misnamed ``deferred``.

    ``deferred`` never meant "postponed": it was incremented by the dead-node
    recycles (``dead = n - mis``), which is the opposite of what the name says.

    On this machine the three counters did not reconcile either -- ``closed`` was
    3052 while ``misrouted`` was 0 and ``deferred`` 6 -- because ``closed`` had
    been accumulating since the aggressive-close policy that was later removed,
    while the other two only started after it.  Reading 3052 suggests today's
    policy recycled thousands of connections.  The part that cannot be attributed
    is kept aside as ``closed_legacy`` so nothing is silently discarded, and from
    here on ``closed == misrouted + recycled`` holds on every read.
    """
    if "deferred" in state:
        state["recycled"] = state.pop("deferred")
    if "counters_migrated" in state:
        return False
    closed = int(state.get("closed") or 0)
    mis = int(state.get("misrouted") or 0)
    rec = int(state.get("recycled") or 0)
    if closed != mis + rec:
        state["closed_legacy"] = max(0, closed - (mis + rec))
        state["closed"] = mis + rec
    state["counters_migrated"] = datetime.now().isoformat(timespec="seconds")
    return True


def conn_age_seconds(conn: dict) -> float:
    start = conn.get("start") or ""
    if not start:
        return 0
    try:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        return (datetime.now(dt.tzinfo) - dt).total_seconds()
    except Exception:
        return 0


def node_is_dead(proxy: dict | None) -> bool:
    """True only on an explicit probe error.

    A zero delay without an error is treated as 'unknown', not 'dead': sing-box
    reports 0 while a check is still pending, and acting on that was a source of
    spurious teardowns.
    """
    hist = (proxy or {}).get("history") or []
    if not hist:
        return False
    return bool((hist[-1] or {}).get("err"))


def host_matches(host: str, suffixes: list[str]) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    for suffix in suffixes:
        s = suffix.lower().lstrip(".")
        if h == s or h.endswith("." + s):
            return True
    return False


def conn_hosts(conn: dict) -> list[str]:
    meta = conn.get("metadata") or {}
    out = []
    for key in ("host", "sniffHost", "sniff_host", "destinationName"):
        value = meta.get(key)
        if value:
            out.append(str(value))
    return out


def dest_ip(conn: dict) -> str:
    meta = conn.get("metadata") or {}
    return str(meta.get("destinationIP") or meta.get("destination_ip") or "")


def ip_in_cidrs(ip: str, cidrs: list[str]) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def is_fake_ip(ip: str) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in FAKEIP_NETS)


def is_proxied(conn: dict) -> bool:
    chains = conn.get("chains") or []
    if not chains:
        return False
    if "direct_out" in chains or "block_out" in chains:
        return False
    return True


class ProxyView:
    """The proxy entries this tick needs, fetched by name instead of in bulk.

    ``collect_close_ids`` reads two things out of ``/proxies``: the URLTest groups
    (their ``type`` and ``now``) and the health ``history`` of the nodes that
    appear in a connection chain.  The full map answers both, but it is 11733
    bytes of 65 entries and pulling it every 8 s measured 284 MB/day and 75 s/day
    of latency.  ``/proxies/<name>`` carries the same fields -- verified on this
    machine that a node's ``history`` from the single-name endpoint compares equal
    to the full map's -- so entries are fetched by name and cached for the tick.
    ``items()`` yields the groups, which the caller fetches eagerly so that being
    unable to read them can still abort the tick the way it always has.
    """

    def __init__(self, base: str, secret: str, groups: dict) -> None:
        self._base = base
        self._secret = secret
        self._groups = groups
        self._cache: dict[str, dict | None] = {}

    def items(self):
        return self._groups.items()

    def get(self, name: str) -> dict | None:
        """One proxy by name, or None.  None is safe: a node whose health cannot
        be read is not treated as dead, so nothing is recycled on a bad read."""
        if name in self._cache:
            return self._cache[name]
        try:
            item = api(self._base, self._secret, "GET", f"/proxies/{urllib.parse.quote(name, safe='')}")
        except Exception as exc:
            log.debug("cannot read /proxies/%s: %s", name, exc)
            item = None
        self._cache[name] = item
        return item


def fetch_groups(base: str, secret: str, tick_no: int) -> tuple[dict, bool]:
    """The URLTest groups, rediscovering the name list only every ~5 minutes.

    Returns ``(groups, ok)``.  ``ok`` is False when the selection could not be
    read at all, which is the caller's cue to skip the tick rather than act while
    blind to which node is selected: ``selected`` is what protects the live node's
    sessions from being recycled.
    """
    global _group_names, _group_names_at
    if not _group_names or tick_no - _group_names_at >= GROUP_DISCOVERY_TICKS:
        try:
            proxies = (api(base, secret, "GET", "/proxies") or {}).get("proxies") or {}
        except Exception as exc:
            log.debug("cannot read /proxies: %s", exc)
            if not _group_names:
                return {}, False
        else:
            _group_names = sorted(
                name for name, p in proxies.items()
                if (p.get("type") or "").lower() in {"urltest", "url-test"}
            )
            _group_names_at = tick_no
    groups = {}
    for name in _group_names:
        try:
            item = api(base, secret, "GET", f"/proxies/{urllib.parse.quote(name, safe='')}")
        except Exception as exc:
            log.debug("cannot read /proxies/%s: %s", name, exc)
            continue
        if item:
            groups[name] = item
    if _group_names and not groups:
        log.debug("no URLTest group could be read; leaving connections alone this tick")
        return {}, False
    return groups, True


def collect_close_ids(proxies: dict, conns: list, state: dict, direct: dict) -> tuple[list[str], dict[str, str]]:
    groups = {
        name: p
        for name, p in proxies.items()
        if (p.get("type") or "").lower() in {"urltest", "url-test"}
    }
    selected = {p.get("now") for p in groups.values() if p.get("now")}
    to_close: list[str] = []
    reasons: dict[str, str] = {}

    # --- 1. URLTest flapping: observe only. -------------------------------
    # Existing sessions are intentionally left alone; the core is configured
    # with interrupt_exist_connections=false, so a switch does not require us to
    # drop anything and dropping is what the user perceives as a disconnect.
    for name, p in groups.items():
        now = p.get("now") or ""
        old = (state.get("now") or {}).get(name) or ""
        if old and now and old != now:
            log.info("urltest %s switched %s -> %s (existing connections kept)", name, old, now)
        if now:
            state.setdefault("now", {})[name] = now

    # --- 2. Recycle sessions pinned to a node that is actively erroring. ---
    for c in conns:
        chains = c.get("chains") or []
        if not chains:
            continue
        node = chains[0]
        if node in selected or node in ("direct_out", "block_out", "urltest_out"):
            continue
        if not node_is_dead(proxies.get(node)):
            continue
        if conn_age_seconds(c) < DEAD_NODE_GRACE_SECONDS:
            continue
        cid = c.get("id")
        if cid:
            to_close.append(cid)
            reasons[cid] = f"dead:{node}"

    # --- 3. Force-direct traffic that leaked into the proxy. --------------
    suffixes = direct["domain_suffix"]
    cidrs = direct["ip_cidr"]
    for c in conns:
        if not is_proxied(c) or conn_age_seconds(c) < MISROUTE_AGE_SECONDS:
            continue
        cid = c.get("id")
        if not cid or cid in reasons:
            continue
        hosts = conn_hosts(c)
        ip = dest_ip(c)
        matched = next((h for h in hosts if host_matches(h, suffixes)), "")
        if matched:
            to_close.append(cid)
            reasons[cid] = f"misroute:{matched}"
            continue
        if ip_in_cidrs(ip, cidrs):
            to_close.append(cid)
            reasons[cid] = f"misroute-ip:{ip}"
            continue
        if is_fake_ip(ip) and any(host_matches(h, suffixes) for h in hosts):
            to_close.append(cid)
            reasons[cid] = f"fakeip:{ip}"

    seen = set()
    out = []
    for cid in to_close:
        if cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out, reasons


def close_connections(base: str, secret: str, ids: list[str], reasons: dict[str, str]) -> list[str]:
    """Close the given connections and return the ones that actually closed.

    Returning the ids rather than a count is what keeps the totals honest: a
    DELETE that fails must not be counted as either a dead-node recycle or a
    misroute close, or ``closed == misrouted + recycled`` stops holding.
    """
    done = []
    for cid in ids:
        try:
            api(base, secret, "DELETE", f"/connections/{cid}")
            done.append(cid)
            reason = reasons.get(cid) or ""
            if reason.startswith("misroute") or reason.startswith("fakeip"):
                log.info("closed %s %s", cid[:8], reason)
        except Exception as exc:
            log.debug("delete %s failed: %s", cid, exc)
    return done


def fakeip_close_wait_present() -> bool:
    """Read-only probe: are there CLOSE-WAIT sockets towards the FakeIP pools?"""
    try:
        out = subprocess.run(
            ["/usr/bin/ss", "-tan", "state", "close-wait"],
            check=False, capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return False
    prefixes = ("198.18.", "198.19.", "198.20.", "198.21.")
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        peer = parts[4]
        if peer.startswith(prefixes):
            return True
    return False


def kill_fakeip_close_wait() -> None:
    if not fakeip_close_wait_present():
        return
    cmds = [
        ["sudo", "-n", "/usr/bin/ss", "-K", "state", "close-wait", "dst", "198.18.0.0/15"],
        ["sudo", "-n", "/usr/bin/ss", "-K", "state", "close-wait", "dst", "198.20.0.0/15"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def ensure_rules() -> None:
    try:
        result = sync_rules.apply(include_core=True)
        if any(result.values()):
            log.info("synced force-direct rules %s", result)
    except Exception:
        log.exception("sync force-direct rules failed")


def tick(state: dict, direct: dict) -> None:
    global _tick_count
    if not SERVICE_JSON.exists():
        return
    base, secret = load_api()
    try:
        groups, groups_ok = fetch_groups(base, secret, _tick_count)
        if not groups_ok:
            return
        payload = api(base, secret, "GET", "/connections") or {}
        conns = payload.get("connections") or []
    except urllib.error.URLError:
        return
    except Exception as exc:
        log.debug("api unavailable: %s", exc)
        return

    # collect_close_ids also records the URLTest selection in state, so "the state
    # changed" is not the same question as "we closed something".
    before = json.dumps(state, ensure_ascii=False, sort_keys=True)
    ids, reasons = collect_close_ids(ProxyView(base, secret, groups), conns, state, direct)
    done = close_connections(base, secret, ids, reasons) if ids else []
    if done:
        n = len(done)
        mis = sum(1 for cid in done if (reasons.get(cid) or "").startswith(("misroute", "fakeip")))
        dead = n - mis
        state["closed"] = int(state.get("closed") or 0) + n
        state["misrouted"] = int(state.get("misrouted") or 0) + mis
        state["recycled"] = int(state.get("recycled") or 0) + dead
        log.info(
            "recycled %s connections (%s dead-node, %s misrouted; totals %s/%s)",
            n, dead, mis, state["recycled"], state["misrouted"],
        )
    # Write only when something actually changed.  This used to be an
    # unconditional save every 8 s -- 10800 writes a day -- even when the URLTest
    # selection and all three counters were identical.
    if json.dumps(state, ensure_ascii=False, sort_keys=True) != before:
        save_state(state)

    _tick_count += 1
    if _tick_count % FAKEIP_CLEAN_EVERY_TICKS == 0:
        kill_fakeip_close_wait()
    if _tick_count % TUNING_EVERY_TICKS == 0:
        # Karing rewrites karing_setting.json from its own state, so re-assert the
        # flapping-reduction values; they take effect on the next core restart.
        try:
            changed = sync_rules.ensure_tuning()
            if changed:
                log.info("re-applied urltest tuning %s", changed)
        except Exception:
            log.exception("ensure_tuning failed")


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.info("karing-gc started (non-disruptive profile)")
    ensure_rules()
    direct = sync_rules.load_direct()
    state = load_state()
    if migrate_counters(state):
        save_state(state)
    while True:
        try:
            tick(state, direct)
        except Exception:
            log.exception("tick failed")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
