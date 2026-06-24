import fcntl
import os
import struct
import unittest

from app import input_activity_monitor as iam


def _event(ev_type: int, code: int, value: int) -> bytes:
    return struct.pack(iam.EVENT_FORMAT, 0, 0, ev_type, code, value)


class DeliberateInputDetectionTests(unittest.TestCase):
    """_read_deliberate_input must ignore analog-axis jitter and sync noise but
    register real key/button presses and meaningful axis movement."""

    def _feed(self, payload: bytes):
        read_fd, write_fd = os.pipe()
        flags = fcntl.fcntl(read_fd, fcntl.F_GETFL)
        fcntl.fcntl(read_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        os.write(write_fd, payload)
        os.close(write_fd)
        return read_fd

    def test_abs_jitter_around_center_is_ignored(self) -> None:
        # DragonRise-style ABS_Z jitter: ~127 +/- 9 (range 0..255 -> deadzone 38).
        payload = b"".join(
            _event(iam.EV_ABS, 2, v) + _event(iam.EV_SYN, 0, 0)
            for v in (127, 130, 124, 133, 121, 129)
        )
        read_fd = self._feed(payload)
        try:
            self.assertFalse(iam._read_deliberate_input(read_fd, {}, {}))
        finally:
            os.close(read_fd)

    def test_key_event_counts_as_activity(self) -> None:
        read_fd = self._feed(_event(iam.EV_KEY, 304, 1) + _event(iam.EV_SYN, 0, 0))
        try:
            self.assertTrue(iam._read_deliberate_input(read_fd, {}, {}))
        finally:
            os.close(read_fd)

    def test_large_abs_movement_counts_as_activity(self) -> None:
        # Baseline at center, then a full deflection well past the deadzone.
        payload = _event(iam.EV_ABS, 2, 127) + _event(iam.EV_ABS, 2, 255)
        read_fd = self._feed(payload)
        try:
            self.assertTrue(iam._read_deliberate_input(read_fd, {}, {}))
        finally:
            os.close(read_fd)

    def test_relative_movement_counts_as_activity(self) -> None:
        read_fd = self._feed(_event(iam.EV_REL, 0, 5))
        try:
            self.assertTrue(iam._read_deliberate_input(read_fd, {}, {}))
        finally:
            os.close(read_fd)

    def test_sync_only_is_not_activity(self) -> None:
        read_fd = self._feed(_event(iam.EV_SYN, 0, 0) * 5)
        try:
            self.assertFalse(iam._read_deliberate_input(read_fd, {}, {}))
        finally:
            os.close(read_fd)

    def test_default_deadzone_when_absinfo_unavailable(self) -> None:
        # A pipe fd has no absinfo ioctl; the fallback range (0..255) -> deadzone 38.
        read_fd = self._feed(b"")
        try:
            self.assertEqual(iam._axis_deadzone(read_fd, 2), 38)
        finally:
            os.close(read_fd)


if __name__ == "__main__":
    unittest.main()
