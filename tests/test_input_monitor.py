import fcntl
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import input_activity_monitor as iam
import app.device.automation as automation
from app.device.automation import (
    _run_idle_volume_automation_once,
    _save_automation_config,
)
from app.common.settings import Settings


def _event(ev_type: int, code: int, value: int, when: float = 0.0) -> bytes:
    sec = int(when)
    usec = int(round((when - sec) * 1_000_000))
    return struct.pack(iam.EVENT_FORMAT, sec, usec, ev_type, code, value)


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "input-monitor-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


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

    def _open_pipe(self):
        read_fd, write_fd = os.pipe()
        flags = fcntl.fcntl(read_fd, fcntl.F_GETFL)
        fcntl.fcntl(read_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        return read_fd, write_fd

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

    def test_analog_sweep_counts_as_activity(self) -> None:
        # Three poles past the deadzone is a real stick sweep, not a square wave.
        payload = b"".join(_event(iam.EV_ABS, 2, v) for v in (127, 180, 255))
        read_fd = self._feed(payload)
        try:
            self.assertTrue(iam._read_deliberate_input(read_fd, {}, {}, {}, now=1_000.0))
        finally:
            os.close(read_fd)

    def test_large_abs_movement_is_pending_until_held(self) -> None:
        payload = _event(iam.EV_ABS, 2, 127) + _event(iam.EV_ABS, 2, 255)
        read_fd = self._feed(payload)
        trackers: dict = {}
        try:
            self.assertFalse(
                iam._read_deliberate_input(read_fd, {}, {}, trackers, now=1_000.0)
            )
        finally:
            os.close(read_fd)
        self.assertFalse(iam._confirm_pending_axes(trackers, 1_001.0))
        self.assertTrue(
            iam._confirm_pending_axes(
                trackers, 1_000.0 + iam.ABS_PENDING_CONFIRM_SECONDS
            )
        )

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

    def test_periodic_square_wave_is_not_activity(self) -> None:
        read_fd, write_fd = self._open_pipe()
        abs_state: dict = {}
        deadzones: dict = {}
        trackers: dict = {}
        now = 1_700_000_000.0
        try:
            for index in range(12):
                value = 0 if index % 2 == 0 else 127
                os.write(write_fd, _event(iam.EV_ABS, 5, value, when=now))
                activity = iam._read_deliberate_input(
                    read_fd, abs_state, deadzones, trackers, now=now
                )
                activity = activity or iam._confirm_pending_axes(trackers, now)
                self.assertFalse(activity, f"square-wave edge {index} counted as input")
                now += 3.0
            self.assertTrue(any(tracker.get("periodic") for tracker in trackers.values()))
            self.assertFalse(iam._confirm_pending_axes(trackers, now + 30.0))
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_isolated_stick_flick_confirms_after_silence(self) -> None:
        read_fd, write_fd = self._open_pipe()
        abs_state: dict = {}
        deadzones: dict = {}
        trackers: dict = {}
        now = 2_000.0
        try:
            os.write(write_fd, _event(iam.EV_ABS, 2, 127, when=now))
            self.assertFalse(
                iam._read_deliberate_input(read_fd, abs_state, deadzones, trackers, now=now)
            )
            now += 0.2
            os.write(write_fd, _event(iam.EV_ABS, 2, 255, when=now))
            self.assertFalse(
                iam._read_deliberate_input(read_fd, abs_state, deadzones, trackers, now=now)
            )
            now += 0.2
            os.write(write_fd, _event(iam.EV_ABS, 2, 127, when=now))
            self.assertFalse(
                iam._read_deliberate_input(read_fd, abs_state, deadzones, trackers, now=now)
            )
            self.assertFalse(iam._confirm_pending_axes(trackers, now + 1.0))
            self.assertTrue(
                iam._confirm_pending_axes(
                    trackers, now + iam.ABS_PENDING_CONFIRM_SECONDS
                )
            )
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_third_pole_breaks_periodic_noise(self) -> None:
        read_fd, write_fd = self._open_pipe()
        abs_state: dict = {}
        deadzones: dict = {}
        trackers: dict = {}
        now = 3_000.0
        try:
            for index in range(8):
                value = 0 if index % 2 == 0 else 127
                os.write(write_fd, _event(iam.EV_ABS, 5, value, when=now))
                iam._read_deliberate_input(
                    read_fd, abs_state, deadzones, trackers, now=now
                )
                now += 3.0
            os.write(write_fd, _event(iam.EV_ABS, 5, 255, when=now))
            self.assertTrue(
                iam._read_deliberate_input(
                    read_fd, abs_state, deadzones, trackers, now=now
                )
            )
            self.assertFalse(any(tracker.get("periodic") for tracker in trackers.values()))
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_key_still_counts_while_axis_is_periodic(self) -> None:
        read_fd, write_fd = self._open_pipe()
        abs_state: dict = {}
        deadzones: dict = {}
        trackers: dict = {}
        now = 4_000.0
        try:
            for index in range(8):
                value = 0 if index % 2 == 0 else 127
                os.write(write_fd, _event(iam.EV_ABS, 5, value, when=now))
                iam._read_deliberate_input(
                    read_fd, abs_state, deadzones, trackers, now=now
                )
                now += 3.0
            os.write(write_fd, _event(iam.EV_KEY, 304, 1, when=now))
            self.assertTrue(
                iam._read_deliberate_input(
                    read_fd, abs_state, deadzones, trackers, now=now
                )
            )
        finally:
            os.close(read_fd)
            os.close(write_fd)


class InputMonitorFileIntegrationTests(unittest.TestCase):
    """Drive the monitor helpers the way the main loop does and check the
    last-input-activity file."""

    def _open_pipe(self):
        read_fd, write_fd = os.pipe()
        flags = fcntl.fcntl(read_fd, fcntl.F_GETFL)
        fcntl.fcntl(read_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        return read_fd, write_fd

    def _tick(self, read_fd, write_fd, payload, abs_state, deadzones, trackers, now):
        os.write(write_fd, payload)
        activity = iam._read_deliberate_input(
            read_fd, abs_state, deadzones, trackers, now=now
        )
        return activity or iam._confirm_pending_axes(trackers, now)

    def test_periodic_square_wave_does_not_refresh_activity_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "last-input-activity"
            seeded = 1_700_000_000.0
            iam._write_activity(str(output), seeded)
            original = output.read_text(encoding="utf-8")
            read_fd, write_fd = self._open_pipe()
            abs_state: dict = {}
            deadzones: dict = {}
            trackers: dict = {}
            now = seeded
            last_written = seeded
            try:
                for index in range(16):
                    value = 0 if index % 2 == 0 else 127
                    activity = self._tick(
                        read_fd,
                        write_fd,
                        _event(iam.EV_ABS, 5, value, when=now),
                        abs_state,
                        deadzones,
                        trackers,
                        now,
                    )
                    if activity and now - last_written >= iam.WRITE_THROTTLE_SECONDS:
                        iam._write_activity(str(output), now)
                        last_written = now
                    now += 3.0
            finally:
                os.close(read_fd)
                os.close(write_fd)
            self.assertEqual(output.read_text(encoding="utf-8"), original)

    def test_key_press_refreshes_activity_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "last-input-activity"
            seeded = 1_700_000_000.0
            iam._write_activity(str(output), seeded)
            read_fd, write_fd = self._open_pipe()
            now = seeded + 5.0
            try:
                activity = self._tick(
                    read_fd,
                    write_fd,
                    _event(iam.EV_KEY, 304, 1, when=now),
                    {},
                    {},
                    {},
                    now,
                )
            finally:
                os.close(read_fd)
                os.close(write_fd)
            self.assertTrue(activity)
            iam._write_activity(str(output), now)
            self.assertEqual(output.read_text(encoding="utf-8").strip(), f"{now:.0f}")

    def test_held_stick_refreshes_activity_file_after_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "last-input-activity"
            seeded = 1_700_000_000.0
            iam._write_activity(str(output), seeded)
            original = output.read_text(encoding="utf-8")
            read_fd, write_fd = self._open_pipe()
            abs_state: dict = {}
            deadzones: dict = {}
            trackers: dict = {}
            now = seeded + 1.0
            try:
                activity = self._tick(
                    read_fd,
                    write_fd,
                    _event(iam.EV_ABS, 2, 127, when=now) + _event(iam.EV_ABS, 2, 255, when=now),
                    abs_state,
                    deadzones,
                    trackers,
                    now,
                )
                self.assertFalse(activity)
                self.assertEqual(output.read_text(encoding="utf-8"), original)
                later = now + iam.ABS_PENDING_CONFIRM_SECONDS
                self.assertTrue(iam._confirm_pending_axes(trackers, later))
                iam._write_activity(str(output), later)
            finally:
                os.close(read_fd)
                os.close(write_fd)
            self.assertEqual(output.read_text(encoding="utf-8").strip(), f"{later:.0f}")


class IdleAutomationInputMonitorUATTests(unittest.TestCase):
    """UAT: a DragonRise-style periodic axis must not block idle volume."""

    def setUp(self) -> None:
        automation._reset_idle_volume_armed_state()

    def _open_pipe(self):
        read_fd, write_fd = os.pipe()
        flags = fcntl.fcntl(read_fd, fcntl.F_GETFL)
        fcntl.fcntl(read_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        return read_fd, write_fd

    def test_idle_volume_fires_while_periodic_axis_square_wave_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            activity_path = root / "last-input-activity"
            idle_started = 1_700_000_000.0
            iam._write_activity(str(activity_path), idle_started)
            _save_automation_config(
                settings,
                {"idle_volume": {"enabled": True, "idle_minutes": 1, "target_volume": 25}},
            )
            read_fd, write_fd = self._open_pipe()
            abs_state: dict = {}
            deadzones: dict = {}
            trackers: dict = {}
            now = idle_started
            try:
                for index in range(25):
                    value = 0 if index % 2 == 0 else 127
                    os.write(write_fd, _event(iam.EV_ABS, 5, value, when=now))
                    activity = iam._read_deliberate_input(
                        read_fd, abs_state, deadzones, trackers, now=now
                    )
                    activity = activity or iam._confirm_pending_axes(trackers, now)
                    if activity:
                        iam._write_activity(str(activity_path), now)
                    now += 3.0
            finally:
                os.close(read_fd)
                os.close(write_fd)

            self.assertEqual(
                activity_path.read_text(encoding="utf-8").strip(),
                f"{idle_started:.0f}",
            )
            with mock.patch.dict(
                "os.environ",
                {"DRONE_INPUT_ACTIVITY_FILE": str(activity_path)},
                clear=False,
            ), mock.patch.object(
                automation, "_get_audio_volume", return_value=80
            ), mock.patch.object(
                automation, "_apply_audio_volume"
            ) as apply_mock, mock.patch.object(
                automation, "_find_running_emulatorlauncher", return_value=None
            ), mock.patch.object(
                automation.time, "time", return_value=idle_started + 90
            ):
                _run_idle_volume_automation_once(settings)
            apply_mock.assert_called_once_with(settings, 25)

    def test_real_button_input_keeps_idle_volume_from_firing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _build_settings(root)
            activity_path = root / "last-input-activity"
            started = 1_700_000_000.0
            iam._write_activity(str(activity_path), started)
            _save_automation_config(
                settings,
                {"idle_volume": {"enabled": True, "idle_minutes": 1, "target_volume": 25}},
            )
            read_fd, write_fd = self._open_pipe()
            now = started + 30.0
            try:
                os.write(write_fd, _event(iam.EV_KEY, 304, 1, when=now))
                activity = iam._read_deliberate_input(read_fd, {}, {}, {}, now=now)
            finally:
                os.close(read_fd)
                os.close(write_fd)
            self.assertTrue(activity)
            iam._write_activity(str(activity_path), now)
            with mock.patch.dict(
                "os.environ",
                {"DRONE_INPUT_ACTIVITY_FILE": str(activity_path)},
                clear=False,
            ), mock.patch.object(
                automation, "_apply_audio_volume"
            ) as apply_mock, mock.patch.object(
                automation, "_find_running_emulatorlauncher", return_value=None
            ), mock.patch.object(
                automation.time, "time", return_value=now + 30
            ):
                _run_idle_volume_automation_once(settings)
            apply_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
