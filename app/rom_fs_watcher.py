"""Real-time ROM filesystem watcher using raw Linux inotify (no third-party deps).

The asset-metadata poller normally re-scans the ROM tree on a fixed interval.
This watcher adds near-real-time detection: when files are created, deleted,
moved, or finished being written under the ROM tree, it wakes the poller
(debounced) so additions and deletions sync promptly instead of waiting for the
next interval.

It is best-effort and self-contained:

* Implemented with ``ctypes`` against ``libc`` inotify, so it needs no extra
  Python packages (the Drone app is otherwise stdlib-only).
* On non-Linux platforms, when inotify is unavailable, or when the kernel watch
  limit is exhausted, ``start()`` returns ``False`` and the caller simply keeps
  relying on the periodic poll. It never raises into the caller.
* It only *wakes* the poller; the poller remains the single source of truth that
  scans the disk, reconciles new/changed/deleted ROMs, and reuses cached md5.
"""

from __future__ import annotations

import ctypes
import os
import select
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

# inotify event mask bits (from <sys/inotify.h>).
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_ISDIR = 0x40000000

# Watch finished writes, plus creation/removal/move of files and directories.
# IN_MODIFY is intentionally excluded: it is noisy during long copies, and a
# finished file fires IN_CLOSE_WRITE which is what the poller actually cares
# about.
_WATCH_MASK = (
    IN_CLOSE_WRITE
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
)

_O_CLOEXEC = 0o2000000

# struct inotify_event { int wd; uint32_t mask, cookie, len; } then name[len].
_EVENT_HEADER = struct.Struct("iIII")


def _log(message: str) -> None:
    print(message, file=sys.stdout, flush=True)


class RomFilesystemWatcher:
    """Watch a ROM tree with inotify and invoke ``on_change`` (debounced)."""

    def __init__(
        self,
        roms_root: Path,
        on_change: Callable[[], None],
        *,
        debounce_seconds: float = 10.0,
        max_delay_seconds: float = 60.0,
    ) -> None:
        self.roms_root = Path(roms_root)
        self.on_change = on_change
        self.debounce_seconds = max(0.5, float(debounce_seconds))
        # Never wait longer than this to fire, even if changes keep arriving
        # (e.g. a long bulk copy), so a sync still happens within the window.
        self.max_delay_seconds = max(self.debounce_seconds, float(max_delay_seconds))
        self._libc: Optional[ctypes.CDLL] = None
        self._fd = -1
        self._stop_r = -1
        self._stop_w = -1
        self._wd_to_path: Dict[int, str] = {}
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        """Begin watching in a daemon thread. Returns False if unavailable."""
        if not sys.platform.startswith("linux"):
            _log(f"ROM filesystem watcher disabled: unsupported platform {sys.platform}")
            return False
        if not self.roms_root.exists():
            _log(f"ROM filesystem watcher disabled: roms_root missing {self.roms_root}")
            return False
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.inotify_init1.argtypes = [ctypes.c_int]
            libc.inotify_init1.restype = ctypes.c_int
            libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            libc.inotify_add_watch.restype = ctypes.c_int
            libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
            libc.inotify_rm_watch.restype = ctypes.c_int
            fd = libc.inotify_init1(_O_CLOEXEC)
            if fd < 0:
                raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        except Exception as error:  # noqa: BLE001 - degrade to periodic poll
            _log(f"ROM filesystem watcher unavailable, relying on periodic poll: {error}")
            return False
        self._libc = libc
        self._fd = fd
        self._stop_r, self._stop_w = os.pipe()
        watches = self._add_watch_tree(self.roms_root)
        if watches == 0:
            _log("ROM filesystem watcher could not register any watches, relying on periodic poll")
            self._cleanup()
            return False
        _log(
            f"ROM filesystem watcher started: roms_root={self.roms_root} watches={watches} "
            f"debounce_s={self.debounce_seconds:.0f} max_delay_s={self.max_delay_seconds:.0f}"
        )
        self._thread = threading.Thread(target=self._loop, name="rom-fs-watcher", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Signal the watcher thread to exit (best-effort)."""
        if self._stop_w >= 0:
            try:
                os.write(self._stop_w, b"x")
            except OSError:
                pass

    def _add_watch(self, path: Path) -> bool:
        if self._libc is None or self._fd < 0:
            return False
        try:
            wd = self._libc.inotify_add_watch(self._fd, str(path).encode("utf-8"), _WATCH_MASK | IN_ONLYDIR)
        except Exception:  # noqa: BLE001
            return False
        if wd < 0:
            return False
        self._wd_to_path[wd] = str(path)
        return True

    def _add_watch_tree(self, root: Path) -> int:
        count = 1 if self._add_watch(root) else 0
        try:
            for dirpath, dirnames, _filenames in os.walk(root):
                for dirname in dirnames:
                    if self._add_watch(Path(dirpath) / dirname):
                        count += 1
        except OSError:
            pass
        return count

    def _loop(self) -> None:
        pending = False
        first_event = 0.0
        last_event = 0.0
        while True:
            if pending:
                now = time.monotonic()
                deadline = min(last_event + self.debounce_seconds, first_event + self.max_delay_seconds)
                timeout: Optional[float] = max(0.0, deadline - now)
            else:
                timeout = None
            try:
                readable, _, _ = select.select([self._fd, self._stop_r], [], [], timeout)
            except (OSError, ValueError):
                break
            if self._stop_r in readable:
                break
            if self._fd in readable and self._read_and_process_events():
                now = time.monotonic()
                if not pending:
                    first_event = now
                    pending = True
                last_event = now
            if pending:
                now = time.monotonic()
                if now >= min(last_event + self.debounce_seconds, first_event + self.max_delay_seconds):
                    pending = False
                    try:
                        self.on_change()
                    except Exception as error:  # noqa: BLE001
                        _log(f"ROM filesystem watcher on_change failed: {error}")
        self._cleanup()

    def _read_and_process_events(self) -> bool:
        try:
            data = os.read(self._fd, 64 * 1024)
        except OSError:
            return False
        relevant = False
        offset = 0
        size = len(data)
        while offset + _EVENT_HEADER.size <= size:
            wd, mask, _cookie, length = _EVENT_HEADER.unpack_from(data, offset)
            offset += _EVENT_HEADER.size
            name = b""
            if length:
                name = data[offset:offset + length].split(b"\x00", 1)[0]
                offset += length
            if mask & IN_Q_OVERFLOW:
                # Kernel dropped events; force a reconcile to recover truth.
                relevant = True
                continue
            if mask & (IN_IGNORED | IN_DELETE_SELF | IN_MOVE_SELF):
                self._wd_to_path.pop(wd, None)
                relevant = True
                continue
            base = self._wd_to_path.get(wd)
            # A new subdirectory appeared: start watching it (and its subtree, in
            # case a whole directory was moved in — those children get no events).
            if base and name and (mask & IN_ISDIR) and (mask & (IN_CREATE | IN_MOVED_TO)):
                self._add_watch_tree(Path(base) / name.decode("utf-8", "replace"))
            relevant = True
        return relevant

    def _cleanup(self) -> None:
        for fd in (self._fd, self._stop_r, self._stop_w):
            if fd is not None and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._fd = self._stop_r = self._stop_w = -1
        self._wd_to_path.clear()
