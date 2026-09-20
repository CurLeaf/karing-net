#!/usr/bin/env python3
"""The timeline sink: one handle, cheap writes, and never a silent blackout.

A debug window delivers hundreds of lines a second, so emit() no longer opens and
closes the log per line, and no longer calls stat() per line either -- it counts
the bytes it wrote and rolls over from that.  Both changes are easy to get subtly
wrong, and getting them wrong loses the timeline exactly when it matters, so they
are measured here.

The failure path matters just as much: emit() must not raise (it is called from
inside the poll loop and from the core-log thread), but a timeline that has gone
quiet must not look like a quiet tunnel.  A failed write has to say so somewhere,
which it does on stderr, once a minute.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Checks, load  # noqa: E402

tw = load("tunnel-watch.py")
checks = Checks()

tmp = Path(tempfile.mkdtemp(prefix="karing-log-sink-"))
tw.LOG_PATH = tmp / "sink.log"

# --------------------------------------------------------------------------
# one handle, reused, with nothing flushed away
# --------------------------------------------------------------------------
tw.emit("INFO", "first")
handle = tw._LOG_FH
checks.check(handle is not None and not handle.closed, "a handle is kept open")
for i in range(50):
    tw.emit("INFO", f"line {i}")
checks.check(tw._LOG_FH is handle, "the same handle is reused, not reopened per line")
body = tw.LOG_PATH.read_text().splitlines()
checks.check(len(body) == 51 and body[0].endswith("first") and body[-1].endswith("line 49"),
             "every line landed, in order", f"{len(body)} lines")
# Nothing is buffered in Python: the timeline is read with tail -f.
on_disk = len(tw.LOG_PATH.read_bytes())
checks.check(on_disk == tw._LOG_BYTES,
             "the byte count matches what is on disk (so every line was flushed)",
             f"{on_disk} vs {tw._LOG_BYTES}")

# --------------------------------------------------------------------------
# no stat() per line
# --------------------------------------------------------------------------
real_stat = os.stat
seen: list[str] = []


def counting_stat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
    seen.append(str(path))
    return real_stat(path, *args, **kwargs)


os.stat = counting_stat  # type: ignore[assignment]
try:
    for i in range(200):
        tw.emit("INFO", f"hot {i}")
finally:
    os.stat = real_stat  # type: ignore[assignment]
checks.check(not [p for p in seen if p.endswith("sink.log")],
             "200 lines cost no stat() on the log at all", f"{len(seen)} stat calls, 0 on the log")

# --------------------------------------------------------------------------
# roll-over, driven by the counter
# --------------------------------------------------------------------------
tw.MAX_LOG_BYTES = 128
tw._LOG_BYTES = tw.MAX_LOG_BYTES + 1
tw.emit("INFO", "after the roll")
rolled = tw.LOG_PATH.with_suffix(".log.1")
checks.check(rolled.exists() and tw.LOG_PATH.exists(),
             "the full log is moved aside and a fresh one is opened")
checks.check(tw._LOG_BYTES == len(tw.LOG_PATH.read_bytes()) < tw.MAX_LOG_BYTES,
             "the counter restarts from the new file's size", f"{tw._LOG_BYTES}")

# --------------------------------------------------------------------------
# a failed write complains once, then heals
# --------------------------------------------------------------------------
blocked = tmp / "blocked"
blocked.mkdir()
tw.LOG_PATH = blocked            # open(<dir>, "a") raises IsADirectoryError
tw._log_close()                  # drop the handle for the old path first
tw._emit_failed_at = 0.0
captured = io.StringIO()
with contextlib.redirect_stderr(captured):
    tw.emit("INFO", "into a directory")
    first = captured.getvalue()
    tw.emit("INFO", "and again")
    second = captured.getvalue()
checks.check(tw._LOG_FH is None, "a failed write leaves no half-open handle")
checks.check("cannot write" in first and not first.startswith("Traceback"),
             "the failure is reported on stderr, not raised", f"{first.strip()[:90]}")
checks.check(second == first, "the report is rate limited, not one line per emit")

# The next successful open must clear it.
tw.LOG_PATH = tmp / "recovered.log"
tw.emit("INFO", "back in business")
checks.check(tw._LOG_FH is not None and "back in business" in tw.LOG_PATH.read_text(),
             "writing resumes once the path is usable again")

sys.exit(checks.finish())
