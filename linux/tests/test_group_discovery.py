#!/usr/bin/env python3
"""Group discovery can fail while Karing is starting or resuming."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Checks, load  # noqa: E402

tw = load("tunnel-watch.py")
checks = Checks()
watcher = tw.Watcher()
messages: list[tuple[str, str]] = []
tw.emit = lambda level, message: messages.append((level, message))

attempts = iter((ConnectionError("API not ready"), ["urltest_out"],
                ConnectionError("API restarting")))


def discover():
    result = next(attempts)
    if isinstance(result, Exception):
        raise result
    return result


tw.group_names = discover
watcher.refresh_groups()
checks.check(watcher.groups == [],
             "unavailable API leaves startup with an empty group list")
checks.check(messages[-1][0] == "WARN" and "will retry" in messages[-1][1],
             "temporary discovery failure is logged as recoverable")

watcher.refresh_groups()
checks.check(watcher.groups == ["urltest_out"],
             "next discovery succeeds after the API becomes ready",
             str(watcher.groups))
checks.check(messages[-1] == ("INFO", "groups [] -> ['urltest_out']"),
             "recovered group list is logged")

watcher.refresh_groups()
checks.check(watcher.groups == ["urltest_out"],
             "temporary API loss preserves the last known groups",
             str(watcher.groups))

source = inspect.getsource(tw.Watcher.main)
checks.check("self.refresh_groups()" in source,
             "startup and periodic discovery share the recovery handler")
checks.check("self.groups = group_names()" not in source,
             "main never lets startup API errors escape")

print()
sys.exit(checks.finish())
