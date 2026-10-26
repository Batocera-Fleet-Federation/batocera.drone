"""Out-of-scope evidence found while testing #586. Not a blocking gate for #586.

The sharing-revocation poller intervals are read from operator env vars with a bare
float(): DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS inside run_sharing_revocation_poller
(before its first tick), and DRONE_SMTP_/DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS at
module import time. A malformed value is therefore not a tick exception, so #586's
tick guard never sees it: the VPN poller thread dies before its first check, and the
SMTP/tailnet modules fail to import, which stops `import app.drone_api` (the whole
server). Recorded separately from #586's scope.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


REPO_ROOT = Path(__file__).resolve().parents[2]


class IntervalEnvStartupOutOfScopeUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _env(self, **extra: str) -> dict:
        return {
            "PATH": os.environ.get("PATH", ""),
            "USERDATA_ROOT": str(self.root),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-586-interval-env-startup",
            **extra,
        }

    def test_vpn_poller_reaches_first_check_with_malformed_interval(self) -> None:
        env = self._env(DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS="abc")
        check = mock.Mock(return_value=False)
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(_generation: int) -> bool:
            return len(sleeps) > 1

        escaped = None
        # The interval is read inside the poller, so the malformed env must be active during the call.
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(vpn_manager, "poller_generation", return_value=41), \
                mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", check), \
                mock.patch.object(vpn_manager.sys, "stderr", io.StringIO()):
            try:
                vpn_manager.run_sharing_revocation_poller(Settings.from_env())
            except ValueError as error:
                escaped = error

        self.assertIsNone(escaped, f"VPN sharing revocation poller died before its first tick: {escaped!r}")
        self.assertEqual(check.call_count, 1)

    def _import_app_in_subprocess(self, variable: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", "import app.drone_api"],
            cwd=REPO_ROOT,
            env=self._env(**{variable: "abc"}),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_app_imports_with_malformed_smtp_interval_env(self) -> None:
        result = self._import_app_in_subprocess("DRONE_SMTP_SHARING_CHECK_INTERVAL_SECONDS")

        self.assertEqual(
            result.returncode,
            0,
            f"import app.drone_api failed with a malformed SMTP interval: {result.stderr.strip().splitlines()[-1:]}",
        )

    def test_app_imports_with_malformed_tailnet_interval_env(self) -> None:
        result = self._import_app_in_subprocess("DRONE_TAILNET_SHARING_CHECK_INTERVAL_SECONDS")

        self.assertEqual(
            result.returncode,
            0,
            f"import app.drone_api failed with a malformed tailnet interval: {result.stderr.strip().splitlines()[-1:]}",
        )


if __name__ == "__main__":
    unittest.main()
