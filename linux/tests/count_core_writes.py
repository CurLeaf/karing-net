#!/usr/bin/env python3
"""How many times does one reconcile() write service_core.json, and in what order?

The question matters because every rewrite is followed by a reload decision, and a
reload costs one tun0 rebuild.  sync_rules.dump_json is spied on to count the
writes, so a single correction that writes the file twice is visible here.

The run happens against a copy of the Karing data directory under a throwaway
HOME: sync_rules resolves every path from Path.home() at import time, so the copy
is what keeps the real config untouched.  The project-wide lock file is the only
real path this touches, and only for the moment the copy is being rewritten.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Checks, ROOT  # noqa: E402

FLAG = "KARING_WRITES_ISOLATED_HOME"
REAL_DIR = Path(os.path.expanduser("~")) / ".local/share/com.nebula.karing"


def reexec_under_a_copy() -> int:
    """First pass: copy the data dir into a temp HOME and run this again inside it."""
    if not REAL_DIR.exists():
        print(f"{REAL_DIR} does not exist; nothing to copy")
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="karing-writes-"))
    (tmp / ".local/share").mkdir(parents=True)
    shutil.copytree(REAL_DIR, tmp / ".local/share/com.nebula.karing")
    env = dict(os.environ, HOME=str(tmp), **{FLAG: "1"})
    print(f"isolated HOME: {tmp}", flush=True)
    try:
        return subprocess.call([sys.executable, str(Path(__file__).resolve())], env=env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    sys.path.insert(0, str(ROOT))
    if importlib.util.find_spec("sync_rules") is None:
        print(f"cannot import sync_rules from {ROOT}")
        return 1
    import sync_rules

    checks = Checks()
    core = sync_rules.CORE_PATH
    if not core.exists():
        print(f"{core} does not exist in the isolated copy")
        return 1

    writes: list[dict] = []
    real_dump = sync_rules.dump_json

    def spy(path, data) -> None:
        if Path(path) == core and isinstance(data, dict):
            writes.append({
                "at": time.monotonic(),
                "svcb": any(
                    sorted(str(t).upper() for t in (r.get("query_type") or [])) == ["HTTPS", "SVCB"]
                    for r in ((data.get("dns") or {}).get("rules") or [])
                ),
                "tolerance": next(
                    (ob.get("tolerance") for ob in data.get("outbounds") or []
                     if ob.get("tag") == "urltest_out-GPT自动"),
                    None,
                ),
            })
        real_dump(path, data)

    sync_rules.dump_json = spy

    drifted = json.loads(core.read_text())
    drifted["dns"]["rules"] = [
        r for r in drifted["dns"]["rules"]
        if sorted(str(t).upper() for t in (r.get("query_type") or [])) != ["HTTPS", "SVCB"]
    ]
    for ob in drifted["outbounds"]:
        if ob.get("tag") == "urltest_out-GPT自动":
            ob["tolerance"] = 120
    core.write_text(json.dumps(drifted, ensure_ascii=False, indent=2) + "\n")

    result = sync_rules.reconcile()
    print("changed parts:", {k: bool(v) for k, v in result.items() if v not in (False, {}, None, [])})
    print(f"service_core.json written {len(writes)} time(s):")
    for i, w in enumerate(writes, 1):
        gap = "" if i == 1 else f", {(w['at'] - writes[i - 2]['at']) * 1000:.1f} ms after the previous one"
        print(f"  write {i}: svcb rule present = {w['svcb']}, urltest tolerance = {w['tolerance']}{gap}")

    # A correction that leaves the rule out of one of its writes hands the app's own
    # reload a config without it, and that reload is what the fast path is racing.
    checks.check(len(writes) >= 1, "the correction wrote the file at least once")
    checks.check(
        all(w["svcb"] for w in writes),
        "every write carries the type-65 rule",
        "no intermediate state without it",
    )
    checks.check(
        writes[-1]["tolerance"] == sync_rules.TUNING["tolerance"],
        "the last write carries the wanted tolerance",
        f"{writes[-1]['tolerance']}",
    )
    if len(writes) > 1:
        span = (writes[-1]["at"] - writes[0]["at"]) * 1000
        print(f"note: {len(writes)} separate writes, {span:.1f} ms apart -- "
              f"a reload landing in between would read a half-corrected file")
        checks.check(span < 50, "the writes land well inside the app's ~100 ms window", f"{span:.1f} ms")
    return checks.finish()


if __name__ == "__main__":
    if os.environ.get(FLAG) != "1":
        raise SystemExit(reexec_under_a_copy())
    raise SystemExit(main())
