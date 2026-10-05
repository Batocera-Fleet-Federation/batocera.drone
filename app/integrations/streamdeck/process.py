"""The single subprocess boundary for Stream Deck built-ins, launching, and scripts.

Always ``executable + argument list`` with ``shell=False``: nothing is ever
interpolated into shell text. Each child gets its own session/process group so
a timeout or cancellation can stop everything it spawned. Output is captured
incrementally (no pipe deadlocks) and bounded.
"""

import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence


DEFAULT_OUTPUT_LIMIT = 64 * 1024
_EXIT_DRAIN_SECONDS = 0.5


@dataclass
class ProcessResult:
    exit_code: Optional[int]
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    start_error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not (self.timed_out or self.cancelled or self.start_error)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["duration_seconds"] = round(self.duration_seconds, 3)
        return payload

    def summary(self) -> str:
        if self.start_error:
            return self.start_error
        if self.timed_out:
            return "timed out"
        if self.cancelled:
            return "cancelled"
        detail = (self.stderr or self.stdout).strip().splitlines()
        tail = detail[-1] if detail else ""
        return f"exit code {self.exit_code}" + (f": {tail[:200]}" if tail else "")


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


class ProcessRunner:
    """Run an executable with a timeout, bounded capture, and optional cancellation."""

    def run(
        self,
        executable: str,
        arguments: Sequence[str] = (),
        *,
        timeout: float = 30.0,
        cwd: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
        cancel_event: Optional[threading.Event] = None,
        output_limit: int = DEFAULT_OUTPUT_LIMIT,
    ) -> ProcessResult:
        started = time.monotonic()
        timeout = max(0.1, float(timeout))
        try:
            process = subprocess.Popen(
                [str(executable), *[str(argument) for argument in arguments]],
                cwd=str(cwd) if cwd else None,
                env=dict(env) if env is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as error:
            return ProcessResult(None, duration_seconds=time.monotonic() - started,
                                 start_error=f"could not start {Path(str(executable)).name}: {error.strerror or error}")

        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = False
        timed_out = cancelled = False
        exited_at: Optional[float] = None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        try:
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if now - started >= timeout:
                    timed_out = True
                    break
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if exited_at is None and process.poll() is not None:
                    exited_at = now
                # A child that backgrounded a grandchild keeping our pipes open
                # must not turn a finished script into a timeout.
                if exited_at is not None and now - exited_at >= _EXIT_DRAIN_SECONDS:
                    break
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffer = buffers[key.data]
                    room = output_limit - len(buffer)
                    if room > 0:
                        buffer.extend(chunk[:room])
                    if len(chunk) > max(room, 0):
                        truncated = True
            if timed_out or cancelled:
                _signal_group(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                # Grandchildren may still be alive even when the parent
                # accepted TERM. Clean up the whole owned process group.
                _signal_group(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=max(0.1, timeout - (time.monotonic() - started)) if not (timed_out or cancelled) else 5)
            except subprocess.TimeoutExpired:
                timed_out = True
                _signal_group(process.pid, signal.SIGKILL)
                process.wait()
        finally:
            selector.close()
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        return ProcessResult(
            process.returncode,
            buffers["stdout"].decode("utf-8", "replace"),
            buffers["stderr"].decode("utf-8", "replace"),
            time.monotonic() - started,
            timed_out=timed_out,
            cancelled=cancelled,
            truncated=truncated,
        )
