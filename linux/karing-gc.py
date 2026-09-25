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
i.e. it deliberately keeps existing sessions alive across a switch.  The
aggressive close was fighting the core.

The opposite policy -- only recycle a node that has an explicit probe *error* --
left healthy orphans sitting for hours.  Cursor's HTTP/2 pool (api2 / api3 /
agentn / api2direct) opens a new generation on the new node and never closes the
old one, so the Clash connection count climbed from an idle ~15 to 60-90 during
an afternoon of Agent use.

Current policy
--------------
* A URLTest switch is logged; the core is not asked to interrupt in-flight
  streams (``ACTIVE_BYTES`` this tick).
* Force-direct domains/IPs that leaked into the proxy are torn down immediately.
* A node with an explicit probe error, that is no longer selected, loses its
  pinned connections after ``DEAD_NODE_GRACE_SECONDS``.
* Orphans on a *healthy* but no-longer-selected node share one quiet clock:
  any tick below ``ACTIVE_BYTES`` (idle *or* HTTP/2 ping) continues it, and a
  burst resets it.  After ``IDLE_LIGHT_SECONDS`` of quiet the leftover
  generation is closed.  Cursor already opened a new generation on the new
  node; keeping the old one was how ``api3`` pools sat on 香港 ❀原生❀… for
  18 minutes, because idle ticks cleared the trickle clock and ping ticks
  cleared the idle clock, so neither ``IDLE_HEAVY_SECONDS`` nor
  ``TRICKLE_SECONDS`` ever elapsed.
* Misroutes always close this tick.  Dead/orphan closes are capped at
  ``CLOSE_BUDGET`` so a switch does not force the client to reconnect everything
  at once.
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
from dataclasses import dataclass
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
# Clash has no last-active field.  A tick-to-tick jump of this many bytes is
# treated as real traffic rather than a keepalive / HTTP/2 ping.
ACTIVE_BYTES = 2048
# Quiet (idle or HTTP/2 ping) on an unselected node this long -> close.
# In-flight bursts are the only thing that keeps an old-path stream alive;
# Cursor has already opened the next generation on the new node.
IDLE_LIGHT_SECONDS = 24
# Dead + orphan closes per tick.  Misroutes are not counted against this.
CLOSE_BUDGET = 24
API_TIMEOUT = 3
# FakeIP CLOSE-WAIT reaping is cheap insurance but must not run on every tick.
FAKEIP_CLEAN_EVERY_TICKS = 4
# Re-assert the URLTest anti-flapping values every ~5 minutes.
TUNING_EVERY_TICKS = 38
# The URLTest group list comes from the full ``/proxies`` map (11733 bytes, 65
# entries).  Rediscovering it every ~5 minutes, instead of on every tick, is what
# lets the per-tick reads be just the groups plus the nodes a connection uses.
GROUP_DISCOVERY_TICKS = 38
FAKEIP_NETS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("198.20.0.0/15"))
PROTECTED_OUTBOUNDS = frozenset({
    "direct_out", "block_out", "urltest_out",
    "dns_direct_out", "dns_proxy_out",
})

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
    return {"now": {}, "closed": 0, "misrouted": 0, "recycled": 0,
            "recycled_dead": 0, "recycled_stale": 0}


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

    ``recycled`` later split into ``recycled_dead`` + ``recycled_stale``.  The
    historical value was entirely dead-node closes, so the remainder goes there.
    """
    changed = False
    if "deferred" in state:
        state["recycled"] = state.pop("deferred")
        changed = True
    if "counters_migrated" not in state:
        closed = int(state.get("closed") or 0)
        mis = int(state.get("misrouted") or 0)
        rec = int(state.get("recycled") or 0)
        if closed != mis + rec:
            state["closed_legacy"] = max(0, closed - (mis + rec))
            state["closed"] = mis + rec
        state["counters_migrated"] = datetime.now().isoformat(timespec="seconds")
        changed = True
    if "recycled_stale" not in state or "recycled_dead" not in state:
        rec = int(state.get("recycled") or 0)
        stale = int(state.get("recycled_stale") or 0)
        state["recycled_stale"] = stale
        state["recycled_dead"] = rec - stale
        if state["recycled_dead"] < 0:
            state["recycled_dead"] = rec
            state["recycled_stale"] = 0
        changed = True
    for key in ("closed", "misrouted", "recycled", "recycled_dead", "recycled_stale"):
        if key not in state:
            state[key] = 0
            changed = True
    return changed


def conn_age_seconds(conn: dict) -> float:
    start = conn.get("start") or ""
    if not start:
        return 0.0
    try:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        age = (datetime.now(dt.tzinfo) - dt).total_seconds()
        return age if age > 0 else 0.0
    except Exception:
        return 0.0


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


def conn_node(conn: dict) -> str:
    chains = conn.get("chains") or []
    return str(chains[0]) if chains else ""


def classify_reason(reason: str) -> str:
    if reason.startswith(("misroute", "fakeip")):
        return "misroute"
    if reason.startswith("dead:"):
        return "dead"
    return "stale"


def apply_close_budget(
    ids: list[str],
    reasons: dict[str, str],
    budget: int = CLOSE_BUDGET,
) -> list[str]:
    """Misroutes always close; dead/orphan closes are capped per tick.

    ``ids`` is assumed to already be in priority order (misroute, dead, stale).
    """
    must, optional = [], []
    for cid in ids:
        if classify_reason(reasons.get(cid) or "") == "misroute":
            must.append(cid)
        else:
            optional.append(cid)
    return must + optional[:budget]


@dataclass
class ConnSample:
    total: int
    last_delta: int | None
    quiet_since: float | None
    ever_active: bool


class TrafficBook:
    """Byte-delta tracker so keepalives and live streams can be told apart.

    Clash ``/connections`` only exposes cumulative upload+download, not
    last-active.  One sample cannot classify a connection; two ticks can.

    Idle (delta 0) and HTTP/2 pings (delta below ``ACTIVE_BYTES``) used to
    reset each other's clocks, which is how leftover ``api3`` pools on an old
    node survived 18 minutes: neither consecutive-idle nor consecutive-trickle
    ever reached its threshold.  They now share one quiet clock.  Only a burst
    of ``ACTIVE_BYTES`` or more clears it.
    """

    def __init__(self) -> None:
        self._samples: dict[str, ConnSample] = {}

    def observe(self, conns: list, now: float) -> None:
        live: set[str] = set()
        for c in conns:
            cid = c.get("id")
            if not cid:
                continue
            live.add(cid)
            total = int(c.get("upload") or 0) + int(c.get("download") or 0)
            prev = self._samples.get(cid)
            if prev is None:
                self._samples[cid] = ConnSample(total, None, None, False)
                continue
            delta = total - prev.total
            if delta < 0:
                delta = 0
            ever = prev.ever_active or delta >= ACTIVE_BYTES
            if delta >= ACTIVE_BYTES:
                quiet_since = None
            else:
                quiet_since = prev.quiet_since if prev.quiet_since is not None else now
            self._samples[cid] = ConnSample(total, delta, quiet_since, ever)
        for cid in list(self._samples):
            if cid not in live:
                del self._samples[cid]

    def last_delta(self, cid: str) -> int | None:
        sample = self._samples.get(cid)
        return None if sample is None else sample.last_delta

    def quiet_seconds(self, cid: str, now: float) -> float:
        sample = self._samples.get(cid)
        if not sample or sample.quiet_since is None:
            return 0.0
        return max(0.0, now - sample.quiet_since)

    def idle_seconds(self, cid: str, now: float) -> float:
        sample = self._samples.get(cid)
        if not sample or sample.quiet_since is None or sample.last_delta != 0:
            return 0.0
        return max(0.0, now - sample.quiet_since)

    def trickle_seconds(self, cid: str, now: float) -> float:
        sample = self._samples.get(cid)
        if (not sample or sample.quiet_since is None
                or sample.last_delta is None
                or sample.last_delta == 0
                or sample.last_delta >= ACTIVE_BYTES):
            return 0.0
        return max(0.0, now - sample.quiet_since)

    def ever_active(self, cid: str) -> bool:
        sample = self._samples.get(cid)
        return bool(sample and sample.ever_active)


_traffic = TrafficBook()


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


def collect_close_ids(
    proxies,
    conns: list,
    state: dict,
    direct: dict,
    book: TrafficBook | None = None,
    now: float | None = None,
) -> tuple[list[str], dict[str, str]]:
    groups = {
        name: p
        for name, p in proxies.items()
        if (p.get("type") or "").lower() in {"urltest", "url-test"}
    }
    selected = {p.get("now") for p in groups.values() if p.get("now")}
    to_close: list[str] = []
    reasons: dict[str, str] = {}

    # --- 1. URLTest flapping: log, then let the orphan rules drain it. ----
    for name, p in groups.items():
        current = p.get("now") or ""
        old = (state.get("now") or {}).get(name) or ""
        if old and current and old != current:
            n_orphans = sum(1 for c in conns if conn_node(c) == old)
            log.info(
                "urltest %s switched %s -> %s (%s orphans; quiet-recycle %ss, in-flight kept)",
                name, old, current, n_orphans, IDLE_LIGHT_SECONDS,
            )
        if current:
            state.setdefault("now", {})[name] = current

    # --- 2. Force-direct traffic that leaked into the proxy. --------------
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
        # FakeIP + a force-direct host is the more specific diagnosis; it must
        # be checked before the generic host match, or it is unreachable.
        if is_fake_ip(ip) and any(host_matches(h, suffixes) for h in hosts):
            to_close.append(cid)
            reasons[cid] = f"fakeip:{ip}"
            continue
        matched = next((h for h in hosts if host_matches(h, suffixes)), "")
        if matched:
            to_close.append(cid)
            reasons[cid] = f"misroute:{matched}"
            continue
        if ip_in_cidrs(ip, cidrs):
            to_close.append(cid)
            reasons[cid] = f"misroute-ip:{ip}"
            continue

    # --- 3. Recycle sessions pinned to a node that is actively erroring. ---
    for c in conns:
        cid = c.get("id")
        if not cid or cid in reasons:
            continue
        node = conn_node(c)
        if not node or node in selected or node in PROTECTED_OUTBOUNDS:
            continue
        if not node_is_dead(proxies.get(node)):
            continue
        if conn_age_seconds(c) < DEAD_NODE_GRACE_SECONDS:
            continue
        to_close.append(cid)
        reasons[cid] = f"dead:{node}"

    # --- 4. Healthy orphans on a node that is no longer selected. ---------
    # Quiet keepalives (idle *or* HTTP/2 ping) share one clock.  A still-
    # transferring stream is left alone -- that is the only "don't drop the
    # in-flight Agent response" rule.  Cursor already opened the next
    # generation on the new node, so the leftover path drains after
    # IDLE_LIGHT_SECONDS of no burst.
    if book is not None and now is not None:
        for c in conns:
            cid = c.get("id")
            if not cid or cid in reasons:
                continue
            node = conn_node(c)
            if not node or node in selected or node in PROTECTED_OUTBOUNDS:
                continue
            if conn_age_seconds(c) < MIN_AGE_SECONDS:
                continue
            delta = book.last_delta(cid)
            if delta is None or delta >= ACTIVE_BYTES:
                continue
            if book.quiet_seconds(cid, now) < IDLE_LIGHT_SECONDS:
                continue
            tag = "stale-trickle" if delta > 0 else "stale-idle"
            to_close.append(cid)
            reasons[cid] = f"{tag}:{node}"

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
            if classify_reason(reason) == "misroute":
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


def tick(state: dict, direct: dict, book: TrafficBook | None = None) -> None:
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

    tracker = book if book is not None else _traffic
    now = time.monotonic()
    tracker.observe(conns, now)

    # collect_close_ids also records the URLTest selection in state, so "the state
    # changed" is not the same question as "we closed something".
    before = json.dumps(state, ensure_ascii=False, sort_keys=True)
    ids, reasons = collect_close_ids(
        ProxyView(base, secret, groups), conns, state, direct,
        book=tracker, now=now,
    )
    ids = apply_close_budget(ids, reasons)
    done = close_connections(base, secret, ids, reasons) if ids else []
    if done:
        kinds = [classify_reason(reasons.get(cid) or "") for cid in done]
        n = len(done)
        n_mis = kinds.count("misroute")
        n_dead = kinds.count("dead")
        n_stale = kinds.count("stale")
        state["closed"] = int(state.get("closed") or 0) + n
        state["misrouted"] = int(state.get("misrouted") or 0) + n_mis
        state["recycled"] = int(state.get("recycled") or 0) + n_dead + n_stale
        state["recycled_dead"] = int(state.get("recycled_dead") or 0) + n_dead
        state["recycled_stale"] = int(state.get("recycled_stale") or 0) + n_stale
        log.info(
            "recycled %s connections (%s stale, %s dead-node, %s misrouted; "
            "totals stale=%s dead=%s mis=%s)",
            n, n_stale, n_dead, n_mis,
            state["recycled_stale"], state["recycled_dead"], state["misrouted"],
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
    log.info(
        "karing-gc started (orphan quiet-recycle: %ss without a burst, budget %s/tick)",
        IDLE_LIGHT_SECONDS, CLOSE_BUDGET,
    )
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
