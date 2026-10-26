"""Out-of-scope evidence found while testing #586. Not a blocking gate for #586.

Extends #592 (malformed sharing-check interval env var kills the VPN poller before
its first tick). #592's suite uses a non-numeric value ("abc"). This adds a value
class that float() accepts but time.sleep() cannot represent: DRONE_VPN_SHARING_
CHECK_INTERVAL_SECONDS=inf, 1e10 or 1e400. max(30.0, float(...)) keeps those values,
and the first time.sleep(interval) raises OverflowError inside the loop but outside
the tick guard #586 added. The VPN sharing poller thread dies before its first check.

The emulated sleep below mirrors CPython's _PyTime_t limit (int64 nanoseconds);
test_real_time_sleep_rejects_the_same_values checks the premise against the real
time.sleep, which raises at once for these inputs, so no test blocks on a real sleep.
"""

from __future__ import annotations

import math
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


OVERFLOW_VALUES = ("inf", "1e10", "1e400")
_PYTIME_T_NANOSECONDS_LIMIT = 2 ** 63


def _sleep_like_cpython(seconds: float) -> None:
    """Accept what CPython's time.sleep accepts; raise the same OverflowError for the rest."""
    if not math.isfinite(seconds) or seconds * 1e9 >= _PYTIME_T_NANOSECONDS_LIMIT:
        raise OverflowError("timestamp too large to convert to C _PyTime_t")


class IntervalOverflowStartupOutOfScopeUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install-root")
        self.addCleanup(patcher.stop)
        patcher.start()
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-586-interval-overflow",
        }, clear=True):
            self.settings = Settings.from_env()

    def test_real_time_sleep_rejects_the_same_values(self) -> None:
        # Premise: these values parse as floats and the real time.sleep refuses them.
        for value in OVERFLOW_VALUES:
            with self.subTest(value=value):
                seconds = max(30.0, float(value))
                with self.assertRaises(OverflowError):
                    time.sleep(seconds)

    def test_vpn_poller_reaches_first_check_with_parseable_overflow_interval(self) -> None:
        # Expected behavior: a parseable but unusable interval must not end the
        # poller before its first check. Fails today: OverflowError escapes the loop.
        for value in OVERFLOW_VALUES:
            with self.subTest(value=value):
                check = mock.Mock(return_value=False)
                sleeps: list[float] = []

                def fake_sleep(seconds: float) -> None:
                    sleeps.append(seconds)
                    _sleep_like_cpython(seconds)

                def retired(_generation: int) -> bool:
                    return len(sleeps) > 1

                escaped = None
                with mock.patch.dict(os.environ, {"DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS": value}), \
                        mock.patch.object(vpn_manager, "poller_generation", return_value=41), \
                        mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                        mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                        mock.patch.object(vpn_manager, "check_sharing_revocation", check):
                    try:
                        vpn_manager.run_sharing_revocation_poller(self.settings)
                    except OverflowError as error:
                        escaped = error

                self.assertIsNone(escaped, f"interval {value!r} ended the VPN sharing poller: {escaped!r}")
                self.assertEqual(check.call_count, 1, f"interval {value!r} never reached the first check")


if __name__ == "__main__":
    unittest.main()
