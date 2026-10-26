"""Out-of-scope evidence for issue #581 (fresh tester). Not a blocking gate.

The RGS reconcile now rides on the VPN self-heal poller. The poller loop has no
exception guard around check_and_self_heal, so if that call ever raises, the
whole thread dies and RGS is never reconciled again. check_and_self_heal's
docstring promises it never raises, so this is a hardening issue in the
existing poller, not a defect of the #581 change itself.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class PollerExceptionOutOfScopeUAT(unittest.TestCase):
    def test_self_heal_exception_does_not_stop_rgs_reconcile_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict("os.environ", {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "adversarial-581-poller-exception",
            }, clear=True):
                settings = Settings.from_env()
            # Two ticks; the first self-heal call raises unexpectedly.
            retired = [False, False, True]
            with mock.patch.object(vpn_manager, "poller_generation", return_value=1), \
                    mock.patch.object(vpn_manager, "poller_retired", side_effect=retired), \
                    mock.patch.object(vpn_manager.time, "sleep"), \
                    mock.patch.object(vpn_manager, "check_and_self_heal",
                                      side_effect=[RuntimeError("unexpected"), None]) as check, \
                    mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel") as reconcile:
                try:
                    vpn_manager.run_self_heal_poller(settings)
                except RuntimeError:
                    pass
            self.assertEqual(reconcile.call_count, 1,
                             "RGS reconcile stopped running after one self-heal exception")


if __name__ == "__main__":
    unittest.main()
