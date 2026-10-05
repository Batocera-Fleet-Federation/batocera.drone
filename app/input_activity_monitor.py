#!/usr/bin/env python3
"""Record the time of the most recent physical input as the privileged worker.

Batocera runs the Drone app as an unprivileged user that cannot read the kernel
input devices, so the root service control worker runs this monitor. It watches
every ``/dev/input/event*`` device and writes the wall-clock epoch of the latest
*deliberate* controller/keyboard/mouse event to a small file the Drone app polls
to drive idle automations (e.g. lowering the volume after a period of no input).

The "deliberate" qualifier matters: many gamepads/arcade encoders (e.g. DragonRise
USB joysticks) stream analog-axis (``EV_ABS``) noise continuously even when nobody
is touching them. Counting that noise as input means the device never looks idle, so
this monitor parses each event and ignores sync events, in-deadzone jitter, and
periodic full-range axis square waves (a resting DragonRise axis 5 has been seen
flipping ``0 <-> 127`` about every 3s, which clears the deadzone). It treats as
input: any key/button event (``EV_KEY``), relative movement (``EV_REL``), analog
sweeps through three or more distinct values, and an absolute-axis change that is
held (or isolated, not repeating on a regular cadence) past the pending-confirm
window.

Usage: ``python3 app/input_activity_monitor.py [output_file]``. The output file
defaults to ``DRONE_INPUT_ACTIVITY_FILE`` or the control directory's
``last-input-activity``. The file is written world-readable so the unprivileged
Drone app can read it.
"""

from __future__ import annotations

import errno
import fcntl
import glob
import os
import select
import struct
import sys
import time
from collections import deque


DEFAULT_OUTPUT = "/userdata/system/drone-app/control/last-input-activity"
WRITE_THROTTLE_SECONDS = 2.0  # avoid rewriting the file on every event
RESCAN_SECONDS = 30.0  # re-open devices to pick up hot-plugged controllers
SELECT_TIMEOUT_SECONDS = 5.0

# struct input_event { struct timeval time; __u16 type; __u16 code; __s32 value; }
# timeval is two C longs; using native sizes matches the running kernel's word size.
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

# Event types we care about (linux/input-event-codes.h).
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03

# Fraction of an absolute axis's full range that a change must exceed to count as
# deliberate movement. Cheap encoders jitter a few percent around center at rest;
# a real stick/trigger push swings far past this. Digital d-pads (HAT axes) have a
# tiny range, so even a single step easily clears the fraction.
ABS_DEADZONE_FRACTION = 0.15
ABS_DEADZONE_FLOOR = 4  # smallest meaningful change, for very small-range axes

# Periodic two-value ABS oscillation (the DragonRise 0<->127 ~3s square wave) is
# not a held stick: each level sits still for seconds, then snaps back. Count an
# absolute axis as input only once it looks like real motion or a non-repeating
# hold. Cadence bounds cover the observed ~3s wave without swallowing a stick
# that is genuinely held longer than the idle automations' minute-scale timer.
ABS_PERIODIC_MIN_INTERVAL = 0.4
ABS_PERIODIC_MAX_INTERVAL = 10.0
ABS_PERIODIC_MIN_INTERVALS = 3  # four edges of a square wave
ABS_PERIODIC_INTERVAL_SLOP = 0.4  # relative deviation from the mean interval
ABS_PENDING_CONFIRM_SECONDS = 10.0  # silence after a 2-value swing before counting it
ABS_MOTION_DISTINCT_POLES = 3
ABS_TRANSITION_HISTORY = 12

# EVIOCGABS(code): _IOR('E', 0x40 + code, struct input_absinfo) -> 6 * __s32.
_ABSINFO_FORMAT = "6i"
_ABSINFO_SIZE = struct.calcsize(_ABSINFO_FORMAT)
_IOC_READ = 2


def _eviocgabs(code: int) -> int:
    return (_IOC_READ << 30) | (_ABSINFO_SIZE << 16) | (ord("E") << 8) | (0x40 + code)


def _axis_deadzone(fd: int, code: int) -> int:
    """Deadzone (in raw axis units) for one absolute axis, from its reported range."""
    try:
        raw = fcntl.ioctl(fd, _eviocgabs(code), bytes(_ABSINFO_SIZE))
        _value, minimum, maximum, _fuzz, flat, _res = struct.unpack(_ABSINFO_FORMAT, raw)
    except OSError:
        minimum, maximum, flat = 0, 255, 0
    axis_range = abs(maximum - minimum)
    return max(ABS_DEADZONE_FLOOR, int(axis_range * ABS_DEADZONE_FRACTION), int(flat))


def _event_epoch(sec: int, usec: int, fallback: float) -> float:
    """Kernel timeval if present, otherwise the caller's clock (tests use 0,0)."""
    if sec or usec:
        return float(sec) + float(usec) / 1_000_000.0
    return fallback


def _distinct_poles(values, deadzone: int):
    """Collapse raw axis values that sit within one deadzone of each other."""
    poles = []
    for value in values:
        if not any(abs(value - pole) <= deadzone for pole in poles):
            poles.append(value)
    return poles


def _intervals_are_regular(intervals) -> bool:
    if len(intervals) < ABS_PERIODIC_MIN_INTERVALS:
        return False
    mean = sum(intervals) / len(intervals)
    if mean < ABS_PERIODIC_MIN_INTERVAL or mean > ABS_PERIODIC_MAX_INTERVAL:
        return False
    slop = mean * ABS_PERIODIC_INTERVAL_SLOP
    return all(abs(interval - mean) <= slop for interval in intervals)


def _new_axis_tracker() -> dict:
    return {
        "transitions": deque(maxlen=ABS_TRANSITION_HISTORY),
        "periodic": False,
        "last_time": None,
        "rest_value": None,
    }


def _note_abs_transition(
    tracker: dict, now: float, previous: int, value: int, deadzone: int
) -> bool:
    """Record a deadzone-clearing ABS change. True if it is clearly real motion.

    Two-value swings stay pending until ``_confirm_pending_axes`` sees silence or
    until enough regular intervals classify the axis as periodic noise. The rest
    position is a pole (so 127 -> 180 -> 255 is three poles) but is not itself a
    timed edge, so it cannot fabricate a zero-length interval.
    """
    if tracker.get("rest_value") is None:
        tracker["rest_value"] = previous
    transitions = tracker["transitions"]
    transitions.append((now, value))
    tracker["last_time"] = now
    poles = _distinct_poles(
        [tracker["rest_value"]] + [item[1] for item in transitions], deadzone
    )
    if len(poles) >= ABS_MOTION_DISTINCT_POLES:
        tracker["periodic"] = False
        return True
    times = [item[0] for item in transitions]
    intervals = [times[index] - times[index - 1] for index in range(1, len(times))]
    recent = intervals[-ABS_PERIODIC_MIN_INTERVALS:]
    if len(poles) <= 2 and _intervals_are_regular(recent):
        tracker["periodic"] = True
        return False
    if tracker.get("periodic"):
        if _intervals_are_regular(recent):
            return False
        tracker["periodic"] = False
        return True
    return False


def _confirm_pending_axes(trackers: dict, now: float) -> bool:
    """True when a non-periodic large ABS change has been held/isolated long enough."""
    activity = False
    for tracker in trackers.values():
        if tracker.get("periodic"):
            continue
        last_time = tracker.get("last_time")
        if last_time is None or not tracker["transitions"]:
            continue
        if now - last_time < ABS_PENDING_CONFIRM_SECONDS:
            continue
        tracker["rest_value"] = tracker["transitions"][-1][1]
        tracker["transitions"].clear()
        tracker["last_time"] = None
        activity = True
    return activity


def _drop_fd_state(fd: int, *stores: dict) -> None:
    for store in stores:
        for key in [item for item in store if item[0] == fd]:
            store.pop(key, None)


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


def _read_deliberate_input(
    fd: int,
    abs_state: dict,
    deadzones: dict,
    trackers=None,
    now=None,
) -> bool:
    """Read pending events from fd; return True if any was a deliberate user input.

    ``abs_state`` maps (fd, code) -> last seen absolute value so we can ignore the
    continuous jitter that idle analog axes emit. ``trackers`` holds per-axis
    transition history used to ignore periodic two-value square waves. Raises
    OSError (other than EAGAIN/EWOULDBLOCK) if the device disappears so the
    caller can drop it.
    """
    if trackers is None:
        trackers = {}
    if now is None:
        now = time.time()
    activity = False
    while True:
        try:
            data = os.read(fd, EVENT_SIZE * 64)
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return activity
            raise
        if not data:
            return activity
        count = len(data) // EVENT_SIZE
        for index in range(count):
            offset = index * EVENT_SIZE
            sec, usec, ev_type, code, value = struct.unpack(
                EVENT_FORMAT, data[offset : offset + EVENT_SIZE]
            )
            if ev_type == EV_KEY:
                activity = True
            elif ev_type == EV_REL:
                if value != 0:
                    activity = True
            elif ev_type == EV_ABS:
                key = (fd, code)
                previous = abs_state.get(key)
                abs_state[key] = value
                if previous is None:
                    continue  # establish a baseline; first sample is not movement
                deadzone = deadzones.get(key)
                if deadzone is None:
                    deadzone = _axis_deadzone(fd, code)
                    deadzones[key] = deadzone
                if abs(value - previous) > deadzone:
                    tracker = trackers.get(key)
                    if tracker is None:
                        tracker = _new_axis_tracker()
                        trackers[key] = tracker
                    event_now = _event_epoch(sec, usec, now)
                    if _note_abs_transition(tracker, event_now, previous, value, deadzone):
                        activity = True
            # EV_SYN and everything else are ignored.
        if count < 64:
            return activity


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
    abs_state: dict = {}
    deadzones: dict = {}
    axis_trackers: dict = {}
    last_scan = time.time()

    while True:
        if time.time() - last_scan >= RESCAN_SECONDS:
            _close_devices(devices)
            devices = _open_devices()
            abs_state.clear()
            deadzones.clear()
            axis_trackers.clear()
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
            abs_state.clear()
            deadzones.clear()
            axis_trackers.clear()
            last_scan = time.time()
            continue

        now = time.time()
        activity = False
        for fd in readable:
            try:
                if _read_deliberate_input(fd, abs_state, deadzones, axis_trackers, now=now):
                    activity = True
            except OSError:
                # Device unplugged; drop it and rescan on the next loop.
                try:
                    os.close(fd)
                except OSError:
                    pass
                devices.pop(fd, None)
                _drop_fd_state(fd, abs_state, deadzones, axis_trackers)

        if _confirm_pending_axes(axis_trackers, now):
            activity = True

        if activity:
            if now - last_written >= WRITE_THROTTLE_SECONDS:
                _write_activity(output_path, now)
                last_written = now


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
