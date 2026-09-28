#!/usr/bin/env python3
"""Reload the running core so the 囡囡喵面板 route is live, then time the site.

The reload can take tens of seconds and drops tun0 once.  This script is the
whole step: find the service port, GET /reload, wait until the proxy answers,
then print three timings and the connection chain.  Stdout is the result.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROXY = "http://127.0.0.1:3067"
SITE = "https://xn--i2r10aa.com/"
HOST = "xn--i2r10aa.com"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def curl_once() -> str:
    proc = subprocess.run(
        ["/usr/bin/curl", "-sS", "-o", "/dev/null", "-m", "20", "-x", PROXY,
         "-w", "code=%{http_code} tls=%{time_appconnect} ttfb=%{time_starttransfer} total=%{time_total}",
         SITE],
        capture_output=True, text=True, timeout=25,
    )
    line = (proc.stdout or proc.stderr or "").strip()
    return line or f"exit {proc.returncode}"


def main() -> int:
    log_path = Path.home() / ".local/share/karing-net/activate-panel.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = log_path.open("a")

    class Tee:
        def write(self, text: str) -> int:
            sys.__stdout__.write(text)
            log_fp.write(text)
            log_fp.flush()
            return len(text)

        def flush(self) -> None:
            sys.__stdout__.flush()
            log_fp.flush()

    sys.stdout = Tee()
    print(f"\n--- {time.strftime('%F %T')} ---", flush=True)
    reconcile = load("karing_reconcile", ROOT / "karing-reconcile.py")
    sync = load("sync_rules", ROOT / "sync_rules.py")
    core = reconcile.core_process()
    if not core or not core[1]:
        print(f"no core port: {core}")
        return 1
    pid, port = core
    print(f"core pid={pid} port={port}", flush=True)
    ok, detail = reconcile.reload_core(port)
    print(f"reload ok={ok} {detail}", flush=True)
    if not ok:
        return 1

    deadline = time.time() + 40
    while time.time() < deadline:
        probe = subprocess.run(
            ["/usr/bin/curl", "-sS", "-o", "/dev/null", "-m", "8", "-x", PROXY,
             "-w", "%{http_code}", "https://www.gstatic.com/generate_204"],
            capture_output=True, text=True, timeout=12,
        )
        code = (probe.stdout or "").strip()
        if code == "204":
            print(f"proxy back after {40 - (deadline - time.time()):.1f}s", flush=True)
            break
        time.sleep(1)
    else:
        print("proxy did not return 204 within 40s")
        return 1

    for i in range(1, 4):
        print(f"site #{i} {curl_once()}", flush=True)

    cfg = json.loads(sync.SERVICE_JSON.read_text()) if hasattr(sync, "SERVICE_JSON") else json.loads(
        Path.home().joinpath(".local/share/com.nebula.karing/service.json").read_text())
    secret = str(cfg.get("secret") or "")
    control = int(cfg.get("control_port") or 9090)
    req = urllib.request.Request(
        f"http://127.0.0.1:{control}/connections",
        headers={"Authorization": f"Bearer {secret}"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        conns = json.loads(resp.read().decode()).get("connections") or []
    hits = []
    for c in conns:
        host = (c.get("metadata") or {}).get("host") or ""
        if HOST in host:
            hits.append(" -> ".join(c.get("chains") or []))
    print("chains", hits or "none yet", flush=True)

    q = urllib.parse.quote(sync.PANEL_OUTBOUND, safe="")
    req = urllib.request.Request(
        f"http://127.0.0.1:{control}/proxies/{q}",
        headers={"Authorization": f"Bearer {secret}"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        group = json.loads(resp.read().decode())
    print(f"now {group.get('now')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
