"""Adversarial UAT for #592 interval configuration resilience.

An operator supplied interval must not break app import or kill the VPN
revocation poller before a check. Numeric intervals preserve ordinary values,
clamp to the 30 second floor, and reject values that are non-finite or too
large for a portable sleep interval.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.common.poller_interval import POLLER_INTERVAL_MAX_SECONDS, parse_poller_interval
from app.drone_api import Settings

ROOT = Path(__file__).resolve().parents[2]


class Issue592IntervalEnvResilienceUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with mock.patch.dict(os.environ, {
            "USERDATA_ROOT": str(self.root),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-592-interval",
        }, clear=True):
            self.settings = Settings.from_env()

    def test_parser_rejects_malformed_nonfinite_and_unrepresentable_values(self) -> None:
        for raw in ("abc", "5m", "0x1.0p+4", "--1", "inf", "-inf", "nan", "1e400", "1e10",
                    str(POLLER_INTERVAL_MAX_SECONDS + 1)):
            with self.subTest(raw=raw):
                self.assertEqual(parse_poller_interval(raw, 300.0), 300.0)
        for raw in (None, "", "  "):
            with self.subTest(raw=raw):
                self.assertEqual(parse_poller_interval(raw, 300.0), 300.0)

    def test_numeric_boundaries_keep_value_and_enforce_floor(self) -> None:
        self.assertEqual(parse_poller_interval("30", 300.0), 30.0)
        self.assertEqual(parse_poller_interval("45.5", 300.0), 45.5)
        self.assertEqual(parse_poller_interval("0", 300.0), 30.0)
        self.assertEqual(parse_poller_interval("-10", 300.0), 30.0)
        self.assertEqual(parse_poller_interval(str(POLLER_INTERVAL_MAX_SECONDS), 300.0), POLLER_INTERVAL_MAX_SECONDS)
        self.assertTrue(math.isfinite(parse_poller_interval("1", 300.0)))

    def test_vpn_poller_gets_a_tick_with_unusable_interval_values(self) -> None:
        for raw in ("abc", "inf", "1e10", "1e400"):
            with self.subTest(raw=raw):
                checks = mock.Mock(return_value=False)
                sleeps: list[float] = []

                def sleep(seconds: float) -> None:
                    sleeps.append(seconds)

                def retired(_generation: int) -> bool:
                    return len(sleeps) > 1

                with mock.patch.dict(os.environ, {"DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS": raw}), \
                     mock.patch.object(vpn_manager, "poller_generation", return_value=17), \
                     mock.patch.object(vpn_manager, "poller_retired", side_effect=retired), \
                     mock.patch.object(vpn_manager.time, "sleep", side_effect=sleep), \
                     mock.patch.object(vpn_manager, "check_sharing_revocation", checks):
                    vpn_manager.run_sharing_revocation_poller(self.settings)
                self.assertEqual(checks.call_count, 1)
                self.assertEqual(sleeps, [300.0, 300.0])

    def _import_app(self, variable: str, value: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "USERDATA_ROOT": str(self.root),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-592-import",
            variable: value,
        }
        return subprocess.run([sys.executable, "-c", "import app.drone_api"], cwd=ROOT,
                              env=env, capture_output=True, text=True, timeout=90)

    def test_app_import_survives_bad_smtp_and_tailnet_values(self) -> None:
        for variable, value in (
            ("DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS", "not-a-number"),
            ("DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS", "nan"),
            ("DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS", "1e400"),
            ("DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS", "inf"),
        ):
            with self.subTest(variable=variable, value=value):
                result = self._import_app(variable, value)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
