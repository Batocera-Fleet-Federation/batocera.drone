"""Out-of-scope evidence found while testing #586. Not a blocking gate for #586.

#586 covers run_sharing_revocation_poller in app/device/vpn_manager.py. The SMTP
and tailnet sharing-revocation pollers are the same forever-loop shape with no
tick-level guard: one exception from check_sharing_revocation (or the tailnet
equivalent) ends the thread, so later revocation checks never run. Recorded
separately so the VPN fix stays scoped to #586.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.smtp_manager as smtp_manager
import app.device.tailnet_service as tailnet_service
from app.drone_api import Settings


class SiblingSharingPollerExceptionOutOfScopeUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-586-sibling-sharing-pollers",
        }, clear=True):
            self.settings = Settings.from_env()

    def _run_poller(self, module, poller_name: str, check_name: str, check: mock.Mock, live_ticks: int):
        """Run the poller for ``live_ticks`` live wake-ups; return any exception that escaped it."""
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(_generation: int) -> bool:
            return len(sleeps) > live_ticks

        escaped = None
        with mock.patch.object(module, "poller_generation", return_value=31), \
                mock.patch.object(module, "poller_retired", new=retired), \
                mock.patch.object(module.time, "sleep", new=fake_sleep), \
                mock.patch.object(module, check_name, check), \
                mock.patch.object(module.sys, "stderr", io.StringIO()):
            try:
                getattr(module, poller_name)(self.settings)
            except RuntimeError as error:
                escaped = error
        return escaped

    def test_smtp_sharing_poller_survives_one_unexpected_check_exception(self) -> None:
        check = mock.Mock(side_effect=[RuntimeError("unexpected"), False])

        escaped = self._run_poller(
            smtp_manager, "run_sharing_revocation_poller", "check_sharing_revocation", check, live_ticks=2,
        )

        self.assertIsNone(escaped, f"SMTP sharing revocation poller exited on: {escaped!r}")
        self.assertEqual(check.call_count, 2)

    def test_tailnet_sharing_poller_survives_one_unexpected_check_exception(self) -> None:
        check = mock.Mock(side_effect=[RuntimeError("unexpected"), False])

        escaped = self._run_poller(
            tailnet_service,
            "run_tailnet_sharing_revocation_poller",
            "check_tailnet_sharing_revocation",
            check,
            live_ticks=2,
        )

        self.assertIsNone(escaped, f"tailnet sharing revocation poller exited on: {escaped!r}")
        self.assertEqual(check.call_count, 2)


if __name__ == "__main__":
    unittest.main()
