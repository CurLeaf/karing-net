#!/usr/bin/env python3
"""Find domestically hosted sites that Karing is routing through the proxy.

Why this exists
---------------
Karing decides the outbound for a *domain* using its rule-sets.  A site that is
hosted in mainland China but whose domain is absent from every CN list
(``geosite:cn``, ``acl:ChinaDomain``, ...) falls through to ``route.final`` and
is therefore proxied.  Because the destination is then reached by an overseas
node, domestic-only services either fail or crawl -- this is the whole reason the
force-direct group in ``direct.json`` exists.

Detection
---------
For each candidate domain we replay the *actual* route rules from
``service_core.json`` (so this never drifts from the live config) to learn
whether it would be proxied.  If it would, we resolve the real address through
the local proxy (the system resolver is FakeIP-hijacked) and test it against the
CN IP rule-sets.  Proxied + CN address == a domain that belongs in ``direct.json``.

Usage
-----
    python3 scan_cn.py                # scan Chrome history
    python3 scan_cn.py --live         # scan the core's current connections
    python3 scan_cn.py --stdin < domains.txt
    python3 scan_cn.py --suggest      # emit a direct.json fragment
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

KARING_DIR = Path.home() / ".local/share/com.nebula.karing"
KARING_DATA = Path("/opt/karing/data")
KARING_CORE = Path("/opt/karing/karingService")
SERVICE_JSON = KARING_DIR / "service.json"
CORE_JSON = KARING_DIR / "service_core.json"
CACHE_DIR = Path.home() / ".local/share/karing-net/rulesets"
DOH = "https://1.1.1.1/dns-query?name={}&type=A"

CN_TAGS = {
    "geosite:cn", "geoip:cn", "acl:ChinaIp", "acl:ChinaDomain",
    "acl:ChinaCompanyIp", "acl:UnBan", "acl:SteamCN", "acl:Download",
    "acl:ChinaMedia",
}
PROXY_TAGS = {"geosite:geolocation-!cn", "acl:ProxyGFWlist", "acl:ProxyMedia"}

# Constraints this scanner does not model; a rule carrying one cannot fire for a
# plain domain lookup, so it is treated as non-matching.
UNMODELLED = ("inbound", "process_name", "process_path", "package_name",
              "protocol", "source_ip_cidr", "port", "network_type", "query_type")


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(x) for x in value]


# --------------------------------------------------------------------------
# rule-set loading
# --------------------------------------------------------------------------
class RuleSet:
    def __init__(self, path: Path):
        data = json.loads(path.read_text())
        self.exact: set[str] = set()
        self.suffix: set[str] = set()
        self.keyword: list[str] = []
        self.regex: list[re.Pattern] = []
        self.cidrs: list = []
        for rule in data.get("rules") or []:
            self.exact.update(x.lower() for x in _as_list(rule.get("domain")))
            self.suffix.update(
                x.lower().lstrip("*").lstrip(".") for x in _as_list(rule.get("domain_suffix"))
            )
            self.keyword.extend(x.lower() for x in _as_list(rule.get("domain_keyword")))
            for x in _as_list(rule.get("domain_regex")):
                try:
                    self.regex.append(re.compile(x))
                except re.error:
                    pass
            for c in _as_list(rule.get("ip_cidr")):
                try:
                    self.cidrs.append(ipaddress.ip_network(c, strict=False))
                except ValueError:
                    pass

    def match_domain(self, domain: str) -> bool:
        d = (domain or "").strip().lower().rstrip(".")
        if not d:
            return False
        if d in self.exact:
            return True
        parts = d.split(".")
        for i in range(len(parts)):
            if ".".join(parts[i:]) in self.suffix:
                return True
        if any(kw in d for kw in self.keyword):
            return True
        return any(r.search(d) for r in self.regex)

    def match_ip(self, addr: str) -> bool:
        try:
            a = ipaddress.ip_address(addr)
        except ValueError:
            return False
        return any(a in n for n in self.cidrs)


class RuleBook:
    """Loads rule-sets on demand, decompiling them once into CACHE_DIR."""

    def __init__(self) -> None:
        self.core = json.loads(CORE_JSON.read_text())
        self.paths: dict[str, Path] = {}
        for rs in (self.core.get("route") or {}).get("rule_set") or []:
            tag, rel = rs.get("tag"), rs.get("path")
            if rs.get("type") == "local" and rel:
                self.paths[tag] = KARING_DATA / rel
        self._cache: dict[str, RuleSet] = {}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def get(self, tag: str) -> RuleSet:
        if tag not in self._cache:
            src = self.paths.get(tag)
            if src is None:
                self._cache[tag] = RuleSet.__new__(RuleSet)
                self._cache[tag].exact = set()
                self._cache[tag].suffix = set()
                self._cache[tag].keyword = []
                self._cache[tag].regex = []
                self._cache[tag].cidrs = []
                return self._cache[tag]
            out = CACHE_DIR / (tag.replace(":", "_").replace("/", "_") + ".json")
            if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
                subprocess.run(
                    [str(KARING_CORE), "rule-set", "decompile", str(src),
                     "-s", str(SERVICE_JSON), "-o", str(out)],
                    check=True, capture_output=True, timeout=120,
                )
            self._cache[tag] = RuleSet(out)
        return self._cache[tag]


# --------------------------------------------------------------------------
# route evaluation, replayed from the live config
# --------------------------------------------------------------------------
class Router:
    def __init__(self, book: RuleBook) -> None:
        self.book = book
        self.rules = (book.core.get("route") or {}).get("rules") or []
        self.final = (book.core.get("route") or {}).get("final") or ""

    def _leaf(self, rule: dict, domain: str, ip: str) -> bool:
        if any(k in rule for k in UNMODELLED):
            return False
        hit = False
        if domain:
            d = domain.lower().rstrip(".")
            for s in _as_list(rule.get("domain_suffix")):
                s = s.lower().lstrip("*").lstrip(".")
                if d == s or d.endswith("." + s):
                    hit = True
            for x in _as_list(rule.get("domain")):
                if d == x.lower():
                    hit = True
            for kw in _as_list(rule.get("domain_keyword")):
                if kw.lower() in d:
                    hit = True
            for rx in _as_list(rule.get("domain_regex")):
                try:
                    if re.search(rx, d):
                        hit = True
                except re.error:
                    pass
            for tag in _as_list(rule.get("rule_set")):
                if self.book.get(tag).match_domain(d):
                    hit = True
        if ip:
            for c in _as_list(rule.get("ip_cidr")):
                try:
                    if ipaddress.ip_address(ip) in ipaddress.ip_network(c, strict=False):
                        hit = True
                except ValueError:
                    pass
            for tag in _as_list(rule.get("rule_set")):
                if self.book.get(tag).match_ip(ip):
                    hit = True
            if rule.get("ip_is_private"):
                try:
                    if ipaddress.ip_address(ip).is_private:
                        hit = True
                except ValueError:
                    pass
        return hit

    def _matches(self, rule: dict, domain: str, ip: str) -> bool:
        if rule.get("type") == "logical":
            subs = rule.get("rules") or []
            if rule.get("mode") == "and":
                return bool(subs) and all(self._matches(s, domain, ip) for s in subs)
            return any(self._matches(s, domain, ip) for s in subs)
        return self._leaf(rule, domain, ip)

    def outbound_for(self, domain: str = "", ip: str = "") -> tuple[str, str]:
        for rule in self.rules:
            if not rule.get("outbound"):
                continue
            if self._matches(rule, domain, ip):
                return rule["outbound"], str(rule.get("name") or "")
        return self.final, "final"

    def is_cn_ip(self, addr: str) -> str:
        for tag in CN_TAGS:
            if tag in self.book.paths and self.book.get(tag).match_ip(addr):
                return tag
        return ""


# --------------------------------------------------------------------------
# candidate collection + resolution
# --------------------------------------------------------------------------
def chrome_hosts() -> list[str]:
    hist = Path.home() / ".config/google-chrome/Default/History"
    if not hist.exists():
        return []
    tmp = Path("/tmp/karing-scan-History.db")
    tmp.write_bytes(hist.read_bytes())
    con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
    try:
        rows = con.execute("select url from urls").fetchall()
    finally:
        con.close()
    return sorted({urllib.parse.urlparse(r[0]).hostname or "" for r in rows} - {""})


def live_hosts() -> list[str]:
    cfg = json.loads(SERVICE_JSON.read_text())
    base = f"http://127.0.0.1:{cfg.get('control_port') or 3057}"
    req = urllib.request.Request(
        base + "/connections", headers={"Authorization": f"Bearer {cfg.get('secret')}"}
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        payload = json.loads(r.read().decode())
    out = set()
    for c in payload.get("connections") or []:
        meta = c.get("metadata") or {}
        for k in ("host", "sniffHost", "sniff_host"):
            if meta.get(k):
                out.add(str(meta[k]))
    return sorted(out)


def proxy_port() -> int:
    for ib in (json.loads(CORE_JSON.read_text()).get("inbounds") or []):
        if ib.get("tag") == "mixed_in_rule":
            return int(ib.get("listen_port") or 3067)
    return 3067


def resolve(host: str, port: int) -> list[str]:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({
            "http": f"http://127.0.0.1:{port}",
            "https": f"http://127.0.0.1:{port}",
        })
    )
    req = urllib.request.Request(DOH.format(host), headers={"accept": "application/dns-json"})
    try:
        with opener.open(req, timeout=12) as r:
            data = json.loads(r.read().decode())
        return [a["data"] for a in data.get("Answer") or [] if a.get("type") == 1]
    except Exception:
        return []


SECOND_LEVEL = {"com.cn", "net.cn", "org.cn", "gov.cn", "co.uk", "com.hk", "com.tw", "co.jp"}
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def base_domain(host: str) -> str:
    parts = host.lower().split(".")
    if len(parts) <= 2:
        return host.lower()
    if ".".join(parts[-2:]) in SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="scan current connections")
    ap.add_argument("--stdin", action="store_true", help="read hosts from stdin")
    ap.add_argument("--suggest", action="store_true", help="print direct.json fragment")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if args.stdin:
        hosts = [l.strip() for l in sys.stdin if l.strip()]
    elif args.live:
        hosts = live_hosts()
    else:
        hosts = chrome_hosts()

    hosts = [h for h in hosts if h and not IP_RE.match(h)
             and h not in ("localhost",) and not h.endswith((".local", ".localhost"))]

    router = Router(RuleBook())
    port = proxy_port()

    candidates = []
    for h in hosts:
        ob, rule = router.outbound_for(domain=h)
        if ob.startswith("urltest") or ob == "block_out":
            candidates.append((h, ob, rule))

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        ips = list(ex.map(lambda h: resolve(h, port), [c[0] for c in candidates]))

    findings = []
    for (h, ob, rule), ipl in zip(candidates, ips):
        tags = sorted({t for ip in ipl if (t := router.is_cn_ip(ip))})
        if tags:
            findings.append((h, ",".join(ipl), ",".join(tags), ob, rule))

    print(f"扫描主机 {len(hosts)} 个，其中走代理 {len(candidates)} 个，"
          f"判定为国内却被代理 {len(findings)} 个\n")
    for h, ip, tag, ob, rule in sorted(findings):
        print(f"  {h:40s} {ip:34s} [{tag}]  <- {rule}")
    if findings:
        print("\n可加入 direct.json 的域名后缀（已按主域名归并）：")
        for d in sorted({base_domain(h) for h, *_ in findings}):
            print(f"    {d}")
        if args.suggest:
            print("\n=== direct.json 片段 ===")
            print(json.dumps(sorted({base_domain(h) for h, *_ in findings}),
                             ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
