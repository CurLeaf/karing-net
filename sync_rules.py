#!/usr/bin/env python3
"""Persist force-direct domains into Karing's own config files."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIRECT_PATH = ROOT / "direct.json"
KARING_DIR = Path.home() / ".local/share/com.nebula.karing"
ROUTING_PATH = KARING_DIR / "karing_routing_group.json"
USE_PATH = KARING_DIR / "karing_subscribe_use.json"
SETTING_PATH = KARING_DIR / "karing_setting.json"
CORE_PATH = KARING_DIR / "service_core.json"
SUBSCRIBE_PATH = KARING_DIR / "karing_subscribe.json"

GROUP_ID = "custom"
SERVER_GROUP = "direct"
SERVER_NAME = "direct_out"

# ---- the "GPT自动" server group (serves OpenAI / Gemini / Claude) ----------
# Karing probes this group against https://www.gstatic.com/generate_204 only, so
# it selects whatever node is closest to gstatic -- which is not the node that is
# fast for the sites the group actually carries.  Measured with the core's own
# delay API on 2026-09-20 (three samples per node, `GET /proxies/<tag>/delay`):
#
#   node                gstatic   claude.ai          gemini
#   台湾-优化2-GPT        175 ms    357 / 402 / 416ms  310 ms
#   美国LA-优化2-GPT      251 ms    593 / 604 / 618ms  691 ms
#   美国LA-优化3-GPT      263 ms   1115 ms             581 ms
#   新加坡-优化2-GPT      147 ms   1910 / 1944 / 6852   420 ms   <- was selected
#   新加坡-优化-GPT       fail      fail               fail
#   美国LA-优化-GPT       fail      fail               fail
#   英国-优化-GPT         fail      fail               fail
#
# So the winning gstatic node was the worst one for claude.ai (5-17x slower than
# the best member) and three members never answered at all.  The member list is
# therefore pinned to the nodes that do answer.  regexs is emptied on purpose:
# it was the "GPT" regex that pulled the failing nodes in, and it would pull them
# straight back in on the next subscription update.
GPT_GROUP = "GPT自动"
GPT_KEEP_TAGS = ("台湾-优化2-GPT", "美国LA-优化2-GPT", "美国LA-优化3-GPT")
# The urltest outbound Karing builds from that group inside the generated config.
GPT_OUTBOUND = "urltest_out-GPT自动"

# ---- SVCB / HTTPS (type 65) DNS queries ------------------------------------
# fakeip can only answer A/AAAA, but the per-group DNS rules match on domain
# alone, so for e.g. geosite:google they swallow SVCB/HTTPS queries too.  The
# core then logs "exchange failed ... only IP queries are supported by fakeip",
# retries the same query every 5 s for two minutes, and the client gets no answer
# at all (verified with `dig -t HTTPS www.google.com`, which times out).
# This rule sends those two query types to the resolver dns.final already uses
# for every other non-A/AAAA query, and must sit before the domain rules.
SVCB_RULE_NAME = "dns_proxy_out[svcb-自定义][dns]"
SVCB_QUERY_TYPES = ["HTTPS", "SVCB"]
SVCB_SERVER = "dns_proxy_out"
# Query types are matched order- and case-insensitively; the reject rule also
# mixes the numeric type 0 with names, so everything is normalised to text.
SVCB_QUERY_TYPES_KEY = sorted(t.upper() for t in SVCB_QUERY_TYPES)


def _query_types(rule: dict) -> list[str]:
    return sorted(str(t).upper() for t in (rule.get("query_type") or []))


# Karing derives the URLTest groups in service_core.json from these two values on
# every connect, and it keeps them in sync with this file (karing-gc re-asserts
# them because the app also writes the file back from memory).  The app's stock
# 120 ms tolerance makes a pool of 39 near-equivalent Hong-Kong/Singapore nodes
# trade places every few minutes, which churns the selected path far more than
# necessary, so tolerance is raised to 300 ms.
#
# ``interval`` is deliberately left at the app's stock 300 s.  It is NOT a free
# knob: the app derives the core's urltest ``interval`` from it, but
# ``idle_timeout`` is not part of the app's model at all (see
# SettingConfigItemAutoSelect in lib/app/modules/setting_manager.dart -- it has
# ``interval`` and ``tolerance`` only), so the two can end up inconsistent.
# Measured 2026-09-20 with interval 600: the app emitted ``interval: 10m`` next
# to a leftover ``idle_timeout: 5m``, the core refused the whole config --
# "start outbound/urltest[urltest_out-GPT自动]: interval must be less or equal
# than idle_timeout" (kept in the app's errors.json) -- and exited, taking the
# tunnel down with it.  At 300 s the app pairs 5m/5m by itself, which is valid.
# ensure_derived_tuning() can only repair a config already on disk; it cannot
# stop the app from generating a bad one at start-up, so the setting itself has
# to stay inside what the app pairs safely.
TUNING = {
    "tolerance": 300,
    "interval": 300,
}
# Anything above this has been observed to make Karing emit interval > idle_timeout.
TUNING_INTERVAL_MAX = 300
if TUNING["interval"] > TUNING_INTERVAL_MAX:
    raise SystemExit(
        f"refusing to run: auto_select.interval {TUNING['interval']}s would make Karing "
        f"generate a urltest outbound with interval > idle_timeout, which the core rejects "
        f"by exiting (max {TUNING_INTERVAL_MAX}s)."
    )


def load_direct() -> dict:
    data = json.loads(DIRECT_PATH.read_text())
    suffixes = sorted({s.strip().lstrip(".").lower() for s in data.get("domain_suffix") or [] if s.strip()})
    cidrs = sorted({s.strip() for s in data.get("ip_cidr") or [] if s.strip()})
    bypass = sorted({s.strip() for s in data.get("proxy_bypass") or [] if s.strip()})
    name = (data.get("group_name") or "🏠 强制直连").strip()
    if not suffixes:
        raise SystemExit("direct.json: domain_suffix is empty")
    return {"group_name": name, "domain_suffix": suffixes, "ip_cidr": cidrs, "proxy_bypass": bypass}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


LOCK_PATH = ROOT / ".sync_rules.lock"
_lock_handle = None
_lock_depth = 0


@contextmanager
def lock():
    """Serialize writers across processes, and allow nesting inside one process.

    karing-gc and karing-reconcile both rewrite these files; a second flock from
    the same process (apply -> ensure_*) would deadlock, so the lock is counted.
    """
    global _lock_handle, _lock_depth
    if _lock_handle is None:
        _lock_handle = LOCK_PATH.open("w")
        fcntl.flock(_lock_handle, fcntl.LOCK_EX)
    _lock_depth += 1
    try:
        yield
    finally:
        _lock_depth -= 1
        if _lock_depth == 0:
            fcntl.flock(_lock_handle, fcntl.LOCK_UN)
            _lock_handle.close()
            _lock_handle = None


def dump_json(path: Path, data: dict) -> None:
    """Write atomically: Karing and the core may read the file at any moment."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def merge_unique(old, new) -> list:
    seen = []
    for item in list(old or []) + list(new or []):
        if item in seen:
            continue
        seen.append(item)
    return seen


def upsert_routing(direct: dict) -> bool:
    data = load_json(ROUTING_PATH)
    items = data.setdefault("items", [])
    if not items:
        items.append({"groupid": GROUP_ID, "urlOrPath": "", "remark": "自定义", "editAble": True, "groups": []})
    groups = items[0].setdefault("groups", [])
    name = direct["group_name"]
    current = next((g for g in groups if g.get("name") == name), None)
    wanted = {
        "groupid": GROUP_ID,
        "name": name,
        "type": "",
        "or": True,
        "domain_suffix": list(direct["domain_suffix"]),
    }
    changed = False
    if current is None:
        groups.insert(0, wanted)
        dump_json(ROUTING_PATH, data)
        return True
    idx = groups.index(current)
    merged = merge_unique(current.get("domain_suffix"), direct["domain_suffix"])
    if idx != 0:
        groups.pop(idx)
        current = dict(current)
        current.update(wanted)
        current["domain_suffix"] = merged
        groups.insert(0, current)
        changed = True
    elif merged != list(current.get("domain_suffix") or []):
        current["domain_suffix"] = merged
        current["or"] = True
        current["groupid"] = GROUP_ID
        changed = True
    if changed:
        dump_json(ROUTING_PATH, data)
    return changed


def upsert_use(direct: dict) -> bool:
    data = load_json(USE_PATH)
    rows = data.setdefault("diversion_group", [])
    name = direct["group_name"]
    wanted = {
        "diversion_groupid": GROUP_ID,
        "diversion_name": name,
        "server_groupid": SERVER_GROUP,
        "server_name": SERVER_NAME,
        "dns_servers": [],
    }
    current = next((r for r in rows if r.get("diversion_name") == name), None)
    changed = False
    if current is None:
        current = wanted
        rows.insert(0, current)
        changed = True
    else:
        if current.get("server_groupid") != SERVER_GROUP or current.get("server_name") != SERVER_NAME:
            current["server_groupid"] = SERVER_GROUP
            current["server_name"] = SERVER_NAME
            changed = True
    finals = [r for r in rows if r.get("diversion_groupid") == "final"]
    others = [r for r in rows if r.get("diversion_groupid") != "final"]
    others = [current] + [r for r in others if r is not current]
    ordered = others + finals
    if ordered != rows:
        data["diversion_group"] = ordered
        changed = True
    if changed:
        dump_json(USE_PATH, data)
    return changed


def upsert_bypass(direct: dict) -> bool:
    data = load_json(SETTING_PATH)
    changed = False
    for key in ("system_proxy_bypass_domain",):
        proxy = data.setdefault("proxy", {})
        cur = proxy.get(key) or []
        merged = merge_unique(cur, direct["proxy_bypass"])
        if merged != cur:
            proxy[key] = merged
            changed = True
    tun = data.setdefault("tun", {})
    cur = tun.get("allow_bypass_httpproxy_domains") or []
    merged = merge_unique(cur, direct["proxy_bypass"])
    if merged != cur:
        tun["allow_bypass_httpproxy_domains"] = merged
        changed = True
    if changed:
        dump_json(SETTING_PATH, data)
    return changed


def _collect_suffixes(rule) -> set[str]:
    found: set[str] = set()
    if isinstance(rule, dict):
        for item in rule.get("domain_suffix") or []:
            found.add(str(item).lower())
        for item in rule.get("rules") or []:
            found.update(_collect_suffixes(item))
    elif isinstance(rule, list):
        for item in rule:
            found.update(_collect_suffixes(item))
    return found


def _rule_covers(rule: dict, suffixes: list[str], outbound_key: str, outbound_value: str) -> bool:
    if not rule:
        return False
    if rule.get(outbound_key) != outbound_value:
        return False
    have = _collect_suffixes(rule)
    return set(suffixes).issubset(have)


def _dns_rule(direct: dict) -> dict:
    return {
        "domain_suffix": list(direct["domain_suffix"]),
        "server": "dns_direct_out",
        "client_subnet": "110.81.122.154",
        "name": f"dns_direct_out[{direct['group_name']}[自定义]][dns]",
        "rewrite_ttl": 43200,
    }


def _route_rule(direct: dict) -> dict:
    rules = [{"domain_suffix": list(direct["domain_suffix"])}]
    if direct["ip_cidr"]:
        rules.append({"ip_cidr": list(direct["ip_cidr"])})
    return {
        "rules": rules,
        "outbound": SERVER_NAME,
        "action": None,
        "name": f"{direct['group_name']}[自定义]",
        "type": "logical",
        "mode": "or",
    }


def upsert_core(direct: dict) -> bool:
    if not CORE_PATH.exists():
        return False
    data = load_json(CORE_PATH)
    changed = False
    dns_name = f"dns_direct_out[{direct['group_name']}[自定义]][dns]"
    route_name = f"{direct['group_name']}[自定义]"
    dns_rules = data.setdefault("dns", {}).setdefault("rules", [])
    route_rules = data.setdefault("route", {}).setdefault("rules", [])

    dns_rule = _dns_rule(direct)
    existing_dns = next((r for r in dns_rules if r.get("name") == dns_name), None)
    if existing_dns is None:
        insert_at = 0
        for i, rule in enumerate(dns_rules):
            name = str(rule.get("name") or "")
            if "国外穿墙" in name or name.endswith("[dns-fakeip]"):
                insert_at = i
                break
            insert_at = i + 1
        dns_rules.insert(insert_at, dns_rule)
        changed = True
    elif not _rule_covers(existing_dns, direct["domain_suffix"], "server", "dns_direct_out"):
        existing_dns.clear()
        existing_dns.update(dns_rule)
        changed = True

    route_rule = _route_rule(direct)
    existing_route = next((r for r in route_rules if r.get("name") == route_name), None)
    if existing_route is None:
        insert_at = 0
        for i, rule in enumerate(route_rules):
            name = str(rule.get("name") or "")
            if "国外穿墙" in name:
                insert_at = i
                break
            insert_at = i + 1
        route_rules.insert(insert_at, route_rule)
        changed = True
    else:
        if not _rule_covers(existing_route, direct["domain_suffix"], "outbound", SERVER_NAME):
            existing_route.clear()
            existing_route.update(route_rule)
            changed = True
        idx = route_rules.index(existing_route)
        wall = next((i for i, r in enumerate(route_rules) if "国外穿墙" in str(r.get("name") or "")), None)
        if wall is not None and idx > wall:
            route_rules.pop(idx)
            route_rules.insert(wall, existing_route)
            changed = True

    if changed:
        dump_json(CORE_PATH, data)
    return changed


def ensure_tuning() -> dict:
    """Keep Karing's auto-select settings from causing constant node flapping."""
    if not SETTING_PATH.exists():
        return {}
    with lock():
        data = load_json(SETTING_PATH)
        auto = data.setdefault("auto_select", {})
        changed: dict[str, tuple] = {}
        for key, want in TUNING.items():
            if auto.get(key) != want:
                changed[key] = (auto.get(key), want)
                auto[key] = want
        if changed:
            dump_json(SETTING_PATH, data)
        return changed


def tune_gpt_group() -> dict:
    """Pin the GPT group to the nodes that actually answer its sites."""
    if not SUBSCRIBE_PATH.exists():
        return {}
    data = load_json(SUBSCRIBE_PATH)
    changed: dict[str, tuple] = {}
    for item in data.get("items") or []:
        for group in item.get("urltests") or []:
            if group.get("remark") != GPT_GROUP:
                continue
            wanted = list(GPT_KEEP_TAGS)
            if list(group.get("tags") or []) != wanted:
                changed["tags"] = (group.get("tags"), wanted)
                group["tags"] = wanted
            if group.get("regexs"):
                changed["regexs"] = (group.get("regexs"), [])
                group["regexs"] = []
    if changed:
        dump_json(SUBSCRIBE_PATH, data)
    return changed


def ensure_svcb_rule() -> dict:
    """Give SVCB/HTTPS (type 65) queries a resolver that can answer them."""
    if not CORE_PATH.exists():
        return {}
    data = load_json(CORE_PATH)
    dns = data.setdefault("dns", {})
    rules = dns.setdefault("rules", [])
    if any(_query_types(r) == SVCB_QUERY_TYPES_KEY for r in rules):
        return {}
    tags = {s.get("tag") for s in dns.get("servers") or []}
    server = SVCB_SERVER if SVCB_SERVER in tags else str(dns.get("final") or "local")
    insert_at = 0
    for i, rule in enumerate(rules):
        if str(rule.get("name") or "").startswith("route-options"):
            insert_at = i + 1
    rules.insert(insert_at, {
        "query_type": list(SVCB_QUERY_TYPES),
        "action": "route",
        "server": server,
        "name": SVCB_RULE_NAME,
    })
    dump_json(CORE_PATH, data)
    return {"inserted_at": insert_at, "server": server}


def _duration_seconds(value) -> float | None:
    """sing-box takes "5m"/"600s" strings and bare nanosecond numbers."""
    if isinstance(value, (int, float)):
        return float(value) / 1e9
    text = str(value or "").strip()
    if not text:
        return None
    unit = text[-1]
    try:
        number = float(text[:-1]) if unit.isalpha() else float(text)
    except ValueError:
        return None
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit.lower())
    return number * factor if factor else None


def urltest_rules_are_sane(data: dict) -> tuple[bool, str]:
    """interval > idle_timeout makes the core reject the *whole* config and exit.

    A rejected reload is survivable; a rejected config is not (the core dies and
    the tunnel with it).  So the config is checked before the core is asked to
    re-read anything.
    """
    for ob in data.get("outbounds") or []:
        if ob.get("type") != "urltest":
            continue
        interval = _duration_seconds(ob.get("interval"))
        idle = _duration_seconds(ob.get("idle_timeout"))
        if interval and idle and interval > idle:
            return False, f"{ob.get('tag')}: interval {ob.get('interval')} > idle_timeout {ob.get('idle_timeout')}"
    return True, ""


def svcb_rule_problem(data: dict) -> str:
    """'' when the type-65 rule is present *and* ahead of the domain rules."""
    rules = (data.get("dns") or {}).get("rules") or []
    at = next((i for i, r in enumerate(rules) if _query_types(r) == SVCB_QUERY_TYPES_KEY), None)
    if at is None:
        return "missing"
    first_domain = next((i for i, r in enumerate(rules)
                         if any(r.get(key) for key in ("domain_suffix", "domain", "domain_keyword",
                                                       "domain_regex", "rule_set", "rules"))), None)
    if first_domain is not None and at > first_domain:
        return f"behind the domain rules (index {at} > {first_domain})"
    return ""


def ensure_gpt_members() -> dict:
    """Pin the GPT group to the nodes that actually answer its sites.

    Karing rebuilds this list from the subscription's regex on every reconnect, so
    it drifts back to the full pool (7 nodes, four of which never answered a
    probe) even though karing_subscribe.json is already pinned.
    """
    if not CORE_PATH.exists():
        return {}
    data = load_json(CORE_PATH)
    wanted = list(GPT_KEEP_TAGS)
    for ob in data.get("outbounds") or []:
        if ob.get("tag") != GPT_OUTBOUND:
            continue
        current = list(ob.get("outbounds") or [])
        if current == wanted:
            return {}
        missing = [tag for tag in wanted if tag not in current]
        if missing:
            return {"skipped": f"node definitions missing: {missing}"}
        ob["outbounds"] = wanted
        dump_json(CORE_PATH, data)
        return {"before": current, "after": wanted}
    return {}


def ensure_derived_tuning() -> dict:
    """Re-assert the URLTest anti-flapping values in the generated config.

    ``tolerance`` is straightened out, and so is the invariant that made the core
    die today: ``interval`` must be <= ``idle_timeout``, or the core rejects the
    whole config and exits (taking the tunnel with it).  Karing derives both from
    ``auto_select``, so a tuning change can hand us a config the core refuses.
    The relation is repaired by *raising* idle_timeout, never by lowering the
    re-test interval, which would make the node pool churn more.
    """
    if not CORE_PATH.exists():
        return {}
    data = load_json(CORE_PATH)
    changed = {}
    for ob in data.get("outbounds") or []:
        if ob.get("type") != "urltest":
            continue
        if ob.get("tolerance") != TUNING["tolerance"]:
            changed.setdefault("tolerance", {})[ob["tag"]] = (ob.get("tolerance"), TUNING["tolerance"])
            ob["tolerance"] = TUNING["tolerance"]
        interval = _duration_seconds(ob.get("interval"))
        idle = _duration_seconds(ob.get("idle_timeout"))
        if interval and idle and interval > idle:
            changed.setdefault("idle_timeout", {})[ob["tag"]] = (ob.get("idle_timeout"), ob.get("interval"))
            ob["idle_timeout"] = ob.get("interval")
    if changed:
        dump_json(CORE_PATH, data)
    return changed


def changed(result: dict) -> bool:
    """True when any part of an apply/reconcile result actually wrote something."""
    def truthy(value):
        if isinstance(value, dict):
            if "skipped" in value:
                return False
            return any(truthy(v) for v in value.values())
        if isinstance(value, list):
            return bool(value)
        return bool(value)
    return any(truthy(v) for v in result.values())


def reconcile() -> dict:
    """Bring Karing's input files *and* its generated core config back in line."""
    result = apply()
    with lock():
        result["gpt_members"] = ensure_gpt_members()
        result["derived_tuning"] = ensure_derived_tuning()
    return result


def apply(include_core: bool = True) -> dict:
    with lock():
        return _apply(include_core)


def _apply(include_core: bool = True) -> dict:
    direct = load_direct()
    result = {
        "routing": upsert_routing(direct),
        "use": upsert_use(direct),
        "bypass": upsert_bypass(direct),
        "tuning": bool(ensure_tuning()),
        "gpt_group": tune_gpt_group(),
    }
    if include_core:
        result["core"] = upsert_core(direct)
        result["svcb"] = ensure_svcb_rule()
    return result


def check() -> int:
    direct = load_direct()
    routing = load_json(ROUTING_PATH)
    groups = (((routing.get("items") or [{}])[0]).get("groups")) or []
    group = next((g for g in groups if g.get("name") == direct["group_name"]), None)
    use = load_json(USE_PATH)
    mapped = next((r for r in use.get("diversion_group") or [] if r.get("diversion_name") == direct["group_name"]), None)

    core_ok = svcb_ok = gpt_ok = tuning_ok = sane = True
    detail = {}
    if CORE_PATH.exists():
        core = load_json(CORE_PATH)
        names = [r.get("name") for r in (core.get("route") or {}).get("rules") or []]
        core_ok = f"{direct['group_name']}[自定义]" in names
        detail["svcb"] = svcb_rule_problem(core) or "ok"
        svcb_ok = detail["svcb"] == "ok"
        for ob in core.get("outbounds") or []:
            if ob.get("tag") == GPT_OUTBOUND:
                members = list(ob.get("outbounds") or [])
                gpt_ok = members == list(GPT_KEEP_TAGS)
                detail["gpt_members"] = "ok" if gpt_ok else f"drifted: {members}"
            if ob.get("type") == "urltest" and ob.get("tolerance") != TUNING["tolerance"]:
                tuning_ok = False
                detail.setdefault("tolerance", []).append(f"{ob.get('tag')}={ob.get('tolerance')}")
        detail.setdefault("tolerance", "ok")
        sane, why = urltest_rules_are_sane(core)
        if not sane:
            detail["config"] = why

    ok = (bool(group) and bool(mapped) and mapped.get("server_name") == SERVER_NAME
          and core_ok and svcb_ok and gpt_ok and tuning_ok and sane)
    print("group", "ok" if group else "missing")
    print("mapping", "ok" if mapped else "missing")
    print("core", "ok" if core_ok else "missing")
    print(f"{GPT_GROUP} members", detail.get("gpt_members", "ok" if gpt_ok else "missing"))
    print("tuning", "ok" if tuning_ok else detail.get("tolerance"))
    print("svcb rule", detail.get("svcb", "ok"))
    if not sane:
        print("config", detail["config"])
    if group:
        print("domains", ",".join(group.get("domain_suffix") or []))
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync force-direct domains into Karing")
    parser.add_argument("command", nargs="?", default="apply", choices=["apply", "check", "reconcile"])
    parser.add_argument("--skip-core", action="store_true")
    args = parser.parse_args()
    if args.command == "check":
        return check()
    if args.command == "reconcile":
        result = reconcile()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    result = apply(include_core=not args.skip_core)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
