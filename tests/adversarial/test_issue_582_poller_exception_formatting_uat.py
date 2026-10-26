"""A malformed exception formatter must not turn recovery into poller death."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class _UnprintableError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("exception formatting failed")


class Issue582ExceptionFormattingUAT(unittest.TestCase):
    def test_bad_exception_string_does_not_kill_polling_or_starve_rgs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict("os.environ", {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "adversarial-582-bad-error-format",
            }, clear=True):
                settings = Settings.from_env()

            ticks = {"sleep": 0}

            def sleep(_seconds: float) -> None:
                ticks["sleep"] += 1

            def retired(_generation: int) -> bool:
                return ticks["sleep"] > 2

            with mock.patch.object(vpn_manager, "poller_generation", return_value=82), \
                    mock.patch.object(vpn_manager, "poller_retired", side_effect=retired), \
                    mock.patch.object(vpn_manager.time, "sleep", side_effect=sleep), \
                    mock.patch.object(vpn_manager, "check_and_self_heal",
                                      side_effect=[_UnprintableError(), None]) as check, \
                    mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel") as reconcile:
                vpn_manager.run_self_heal_poller(settings)

            self.assertEqual(check.call_count, 2)
            self.assertEqual(reconcile.call_count, 2)
            self.assertEqual(ticks["sleep"], 3)


if __name__ == "__main__":
    unittest.main()
