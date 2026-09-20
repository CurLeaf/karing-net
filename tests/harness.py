"""Shared helpers for the karing-net self-tests.

Every test under this directory must be safe to run next to the live services:
none of them may write Karing's configuration or the reconciler's state file.  The
tests that would have to write those (the end-to-end fast-path check) are
deliberately not here -- run those by hand, with the tunnel down for a moment.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> ModuleType:
    """Import one of the project's scripts by path (they are not a package)."""
    path = ROOT / name
    spec = importlib.util.spec_from_file_location(f"{path.stem}_under_test", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Checks:
    """Tiny pass/fail recorder so each test can report and exit properly."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        print(f"{'ok  ' if ok else 'BAD '} {label}{f'  {detail}' if detail else ''}")
        if not ok:
            self.failures.append(label)
        return ok

    def finish(self) -> int:
        if self.failures:
            print(f"\n{len(self.failures)} check(s) failed: {', '.join(self.failures)}")
            return 1
        print("\nall checks passed")
        return 0
