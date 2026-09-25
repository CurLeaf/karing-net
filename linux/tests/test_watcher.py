#!/usr/bin/env python3
"""ConfigWatcher: it must see the app's in-place write, and not our own rename.

The fast path is worth nothing if the wake-up is late, so the first case measures
the latency to a close() of a file opened with O_TRUNC (the app's pattern).  The
second case is the false positive that would make the loop rewrite the file it
just wrote.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Checks, load  # noqa: E402

kr = load("karing-reconcile.py")
checks = Checks()

tmpdir = Path(tempfile.mkdtemp(prefix="karing-watcher-"))
target = tmpdir / "service_core.json"
target.write_text('{"a": 1}\n')

watcher = kr.ConfigWatcher(target)
checks.check(bool(watcher.fd and watcher.fd > 0), "inotify available", f"fd={watcher.fd}")


def app_write(delay: float) -> None:
    time.sleep(delay)
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC)
    os.write(fd, b'{"a": 111}\n')
    os.close(fd)


def rename_write(delay: float, mine: bool) -> None:
    """A write done by .tmp + rename, as sync_rules.dump_json does it."""
    time.sleep(delay)
    tmp = tmpdir / "service_core.json.tmp"
    tmp.write_text('{"a": 222}\n')
    if mine:
        watcher.note_own_write()
    os.replace(tmp, target)


threading.Thread(target=app_write, args=(0.3,), daemon=True).start()
t0 = time.monotonic()
hot = watcher.wait(5)
latency = time.monotonic() - t0
checks.check(hot and latency < 0.5, "app O_TRUNC write wakes the watcher", f"after {latency:.4f}s (write at 0.30)")

threading.Thread(target=rename_write, args=(0.2, True), daemon=True).start()
t0 = time.monotonic()
hot = watcher.wait(1.0)
checks.check(not hot, "our own rename is not reported as an app write", f"after {time.monotonic() - t0:.4f}s")

# Outside the ignore window somebody else's rename has to be seen again.  No
# latency assertion here: an event left over in the buffer makes this fire at once,
# which is harmless for the loop (it only triggers another drift check).
time.sleep(kr.IGNORE_OWN_WRITE_SECONDS + 0.1)
threading.Thread(target=rename_write, args=(0.2, False), daemon=True).start()
hot = watcher.wait(2.0)
checks.check(hot, "a rename outside the window is seen")

os.close(watcher.fd)
sys.exit(checks.finish())
