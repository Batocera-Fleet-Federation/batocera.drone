"""Process logging setup for the Drone: timestamped, size-rotating tee streams.

Extracted from ``drone_api.py``. ``_configure_rotating_logs`` swaps ``sys.stdout``
/ ``sys.stderr`` for ``_TeeRotatingStream`` wrappers that echo to the console and
also append timestamped, rotating log files, and stands up a dedicated file-only
stream for detailed narration. ``_drone_log`` routes those events to that
``drone.log`` (surfaced as the ``drone_activity`` Log Source), optionally also
echoing a high-level line to stdout.

Pure stdlib; takes a ``Settings``-like object (only attribute access) so it does
not import ``drone_api`` and create a cycle.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

# Dedicated, file-only stream for detailed narration (pairing/tailnet, automation,
# peer health, ROM-metadata sync). Configured by _configure_rotating_logs; None
# until then (and in unit tests).
_DRONE_ACTIVITY_LOG_STREAM = None


class _TimestampFormatter:
    """Thread-safe ISO-8601 timestamp provider."""
    _lock = Lock()

    @classmethod
    def now(cls) -> str:
        with cls._lock:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


class _TeeRotatingStream:
    def __init__(self, original_stream, log_path: Path, max_bytes: int, backup_count: int):
        self._original_stream = original_stream
        self._log_path = log_path
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._file = self._log_path.open("a", encoding="utf-8")
        self._lock = Lock()
        self._partial = ""  # buffer for partial-line writes

    def _rollover_if_needed(self) -> None:
        if self._max_bytes <= 0:
            return
        self._file.flush()
        if self._log_path.stat().st_size < self._max_bytes:
            return

        self._file.close()
        if self._backup_count > 0:
            for index in range(self._backup_count - 1, 0, -1):
                src = self._log_path.with_name(f"{self._log_path.name}.{index}")
                dst = self._log_path.with_name(f"{self._log_path.name}.{index + 1}")
                if src.exists():
                    if dst.exists():
                        dst.unlink()
                    src.rename(dst)

            first_backup = self._log_path.with_name(f"{self._log_path.name}.1")
            if first_backup.exists():
                first_backup.unlink()
            if self._log_path.exists():
                self._log_path.rename(first_backup)
        else:
            if self._log_path.exists():
                self._log_path.unlink()

        self._file = self._log_path.open("a", encoding="utf-8")

    def _timestamped_line(self, line: str) -> str:
        ts = _TimestampFormatter.now()
        return f"[{ts}] {line}"

    def write(self, data: str) -> int:
        if not isinstance(data, str):
            data = str(data)
        with self._lock:
            if data:
                # Prepend timestamp to each complete line in the data
                self._partial += data
                lines = self._partial.split("\n")
                # All complete lines (except possibly the last partial)
                complete = lines[:-1]
                self._partial = lines[-1]
                for line in complete:
                    ts_line = self._timestamped_line(line + "\n")
                    self._file.write(ts_line)
                    self._file.flush()
                self._rollover_if_needed()
            # original_stream is None for file-only streams (e.g. the activity log),
            # which must NOT also echo to the console/stdout.
            if self._original_stream is not None:
                self._original_stream.write(data)
            return len(data)

    def flush(self) -> None:
        with self._lock:
            if self._original_stream is not None:
                self._original_stream.flush()
            self._file.flush()

    def isatty(self) -> bool:
        return self._original_stream.isatty() if self._original_stream is not None else False


def _configure_rotating_logs(settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = settings.log_dir / settings.stdout_log_file
    stderr_path = settings.log_dir / settings.stderr_log_file

    sys.stdout = _TeeRotatingStream(
        original_stream=sys.stdout,
        log_path=stdout_path,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )
    sys.stderr = _TeeRotatingStream(
        original_stream=sys.stderr,
        log_path=stderr_path,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )
    # Dedicated, file-only stream for detailed narration (pairing/tailnet, automation,
    # peer health, ROM-metadata sync). Keeps stdout to high-level lifecycle events.
    # Surfaced in Log Sources as "drone_activity".
    global _DRONE_ACTIVITY_LOG_STREAM
    _DRONE_ACTIVITY_LOG_STREAM = _TeeRotatingStream(
        original_stream=None,
        log_path=settings.log_dir / settings.activity_log_file,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )


def _drone_log(message: str, *, also_stdout: bool = False) -> None:
    """Record a detailed narration event to the dedicated drone.log.

    Detailed events (pairing/tailnet, automation, peer health, ROM-metadata sync)
    go to drone.log only. High-level lifecycle events pass ``also_stdout=True`` so a
    concise summary still appears in stdout.log. If the dedicated stream is not configured
    yet (e.g. unit tests, early startup), fall back to stdout so nothing is lost.
    """
    line = message if message.endswith("\n") else message + "\n"
    stream = _DRONE_ACTIVITY_LOG_STREAM
    if stream is None:
        sys.stdout.write(line)
        sys.stdout.flush()
        return
    stream.write(line)
    if also_stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
