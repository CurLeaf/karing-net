#!/usr/bin/env python3
"""The argv parsing and the process-age maths, both changed for the port-0 case.

``core_process`` returns port 0 for a core whose command line does not carry
``--service-http-port`` yet; that is "the port is not published", not "no core".
``process_age`` has to be a real age in seconds, not an epoch-sized number.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Checks, load  # noqa: E402

kr = load("karing-reconcile.py")
checks = Checks()

cases = [
    (["/opt/karing/karingService", "run2", "--service-config=/x", "--service-http-port=35463"], 35463),
    (["/opt/karing/karingService", "run2", "--service-config=/x", "--service-http-port", "35463"], 35463),
    (["/opt/karing/karingService", "run2", "--service-config=/x"], 0),
    (["/opt/karing/karingService", "run2", "--service-http-port="], 0),
    (["/opt/karing/karingService", "run2", "--service-http-port=abc"], 0),
]
# The parser is a plain scan of the arguments (these are karingService's own argv);
# the guard against unrelated command lines that merely mention the flag lives in
# core_process(), which checks argv[0] before it looks at anything else.
for argv, want in cases:
    got = kr.service_port_from_argv(argv)
    checks.check(got == want, f"argv {want} <- {' '.join(argv[2:]) or '(bare)'}", f"=> {got}")

core = kr.core_process(report=False)
print(f"core_process() now: {core}")
if core:
    age = kr.process_age(core[0])
    checks.check(0 <= age < 100000, f"process_age(pid {core[0]}) = {age:.1f} s", "expect a plain uptime")
else:
    print("no core running; skipping the live process_age check")

sys.exit(checks.finish())
