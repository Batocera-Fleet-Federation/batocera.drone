"""Redirects this process's stdout/stderr into the *same* log files the
Drone service itself writes to (``app/common/logging_setup.py``'s
``stdout.log``/``stderr.log``, default ``/userdata/system/logs/drone-app``),
so debugging a ports-client problem doesn't require a separate log to go
looking for -- it's right there interleaved with the Drone's own narration
of the same request. Every line is prefixed ``[ports-client]`` so it's
unambiguous which process wrote it.

Deliberately a small, independent append-mode tee, not a reuse of
``app.common.logging_setup``'s ``_TeeRotatingStream``/rotation: ports-client
never imports the Drone package at all (see ``client/config.py``'s "talks to
Drone over HTTP, never via a Python import" rule) and is a separate,
short-lived process, so it has no business owning that file's rotation --
the Drone process's own rotation (based on ITS total output) eventually
rolls this content away too, exactly like any of its own lines.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

_PREFIX = "[ports-client]"


class _AppendOnlyTee:
    """Mirrors ``_TeeRotatingStream``'s partial-line timestamp handling
    (buffer until a real newline, so a multi-write single line doesn't get
    two timestamps) without owning rotation -- just opens the target file
    in append mode and leaves size/rollover to the Drone process."""

    def __init__(self, original_stream, log_path: Path):
        self._original_stream = original_stream
        self._file = log_path.open("a", encoding="utf-8")
        self._lock = Lock()
        self._partial = ""

    def write(self, data: str) -> int:
        if not isinstance(data, str):
            data = str(data)
        with self._lock:
            if data:
                self._partial += data
                lines = self._partial.split("\n")
                complete, self._partial = lines[:-1], lines[-1]
                for line in complete:
                    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
                    self._file.write(f"[{ts}] {_PREFIX} {line}\n")
                if complete:
                    self._file.flush()
            if self._original_stream is not None:
                self._original_stream.write(data)
            return len(data)

    def flush(self) -> None:
        with self._lock:
            self._file.flush()
            if self._original_stream is not None:
                self._original_stream.flush()

    def isatty(self) -> bool:
        return self._original_stream.isatty() if self._original_stream is not None else False


def configure(*, userdata_root: Optional[Path] = None) -> None:
    """Swap sys.stdout/sys.stderr for tees into the Drone's own log
    directory. Best-effort: if the directory can't be created or the files
    can't be opened (read-only filesystem, permissions), this silently
    leaves stdout/stderr untouched rather than crashing the app over a
    logging nicety."""
    root = userdata_root or Path(os.environ.get("USERDATA_ROOT", "/userdata"))
    log_dir = Path(os.environ.get("LOG_DIR", str(root / "system" / "logs" / "drone-app")))
    stdout_name = os.environ.get("STDOUT_LOG_FILE", "stdout.log")
    stderr_name = os.environ.get("STDERR_LOG_FILE", "stderr.log")

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        sys.stdout = _AppendOnlyTee(sys.stdout, log_dir / stdout_name)
        sys.stderr = _AppendOnlyTee(sys.stderr, log_dir / stderr_name)
    except OSError:
        pass
