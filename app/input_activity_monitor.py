#!/usr/bin/env python3
"""Record the time of the most recent physical input as the privileged worker.

Batocera runs the Drone app as an unprivileged user that cannot read the kernel
input devices, so the root service control worker runs this monitor. It watches
every ``/dev/input/event*`` device and writes the wall-clock epoch of the latest
controller/keyboard/mouse event to a small file the Drone app polls to drive
idle automations (e.g. lowering the volume after a period of no input).

Usage: ``python3 app/input_activity_monitor.py [output_file]``. The output file
defaults to ``DRONE_INPUT_ACTIVITY_FILE`` or the control directory's
``last-input-activity``. The file is written world-readable so the unprivileged
Drone app can read it.
"""

from __future__ import annotations

import errno
import glob
import os
import select
import sys
import time


DEFAULT_OUTPUT = "/userdata/system/drone-app/control/last-input-activity"
WRITE_THROTTLE_SECONDS = 2.0  # avoid rewriting the file on every event
RESCAN_SECONDS = 30.0  # re-open devices to pick up hot-plugged controllers
SELECT_TIMEOUT_SECONDS = 5.0


def _open_devices() -> dict:
    """Open every readable /dev/input/event* device, keyed by file descriptor."""
    devices: dict = {}
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        devices[fd] = path
    return devices


def _close_devices(devices: dict) -> None:
    for fd in list(devices):
        try:
            os.close(fd)
        except OSError:
            pass
    devices.clear()


def _write_activity(output_path: str, epoch: float) -> None:
    tmp_path = f"{output_path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(f"{epoch:.0f}\n")
        os.replace(tmp_path, output_path)
        try:
            os.chmod(output_path, 0o664)
        except OSError:
            pass
    except OSError:
        # Best-effort; a failed write just means the automation waits another tick.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _drain(fd: int) -> bool:
    """Read all currently-available bytes from fd. Returns True if any were read.

    Raises OSError (other than EAGAIN/EWOULDBLOCK) if the device went away.
    """
    read_any = False
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return read_any
            raise
        if not chunk:
            return read_any
        read_any = True
        if len(chunk) < 4096:
            return read_any


def main() -> int:
    if len(sys.argv) > 1:
        output_path = sys.argv[1]
    else:
        output_path = os.environ.get("DRONE_INPUT_ACTIVITY_FILE", DEFAULT_OUTPUT)

    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    # Seed so a fresh boot is not treated as already-idle.
    now = time.time()
    _write_activity(output_path, now)
    last_written = now

    devices = _open_devices()
    last_scan = time.time()

    while True:
        if time.time() - last_scan >= RESCAN_SECONDS:
            _close_devices(devices)
            devices = _open_devices()
            last_scan = time.time()

        if not devices:
            time.sleep(2.0)
            continue

        try:
            readable, _, _ = select.select(list(devices), [], [], SELECT_TIMEOUT_SECONDS)
        except OSError as error:
            if error.errno == errno.EINTR:
                continue
            _close_devices(devices)
            devices = _open_devices()
            last_scan = time.time()
            continue

        activity = False
        for fd in readable:
            try:
                if _drain(fd):
                    activity = True
            except OSError:
                # Device unplugged; drop it and rescan on the next loop.
                try:
                    os.close(fd)
                except OSError:
                    pass
                devices.pop(fd, None)

        if activity:
            now = time.time()
            if now - last_written >= WRITE_THROTTLE_SECONDS:
                _write_activity(output_path, now)
                last_written = now


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
