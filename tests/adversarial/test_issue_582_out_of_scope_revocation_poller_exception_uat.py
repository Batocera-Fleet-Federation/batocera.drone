"""Out-of-scope evidence found while testing #582. Not a blocking gate for #582.

run_sharing_revocation_poller has the same unguarded shape #582 is fixing on
run_self_heal_poller: it calls check_sharing_revocation with no tick-level
except. check_sharing_revocation's docstring promises it never raises, so this
is the same class of poller-hardening issue on a different forever-loop.
Outside the #582 scope (VPN self-heal + RGS reconcile cadence).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class SharingRevocationPollerExceptionOutOfScopeUAT(unittest.TestCase):
    def test_revocation_check_exception_does_not_stop_later_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict("os.environ", {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "adversarial-582-oos-revocation-poller",
            }, clear=True):
                settings = Settings.from_env()
            retired = [False, False, True]
            with mock.patch.object(vpn_manager, "poller_generation", return_value=1), \
                    mock.patch.object(vpn_manager, "poller_retired", side_effect=retired), \
                    mock.patch.object(vpn_manager.time, "sleep"), \
                    mock.patch.object(
                        vpn_manager,
                        "check_sharing_revocation",
                        side_effect=[RuntimeError("unexpected"), None],
                    ) as check:
                try:
                    vpn_manager.run_sharing_revocation_poller(settings)
                except RuntimeError:
                    pass
            self.assertEqual(
                check.call_count,
                2,
                "sharing revocation poller stopped after one unexpected exception",
            )


if __name__ == "__main__":
    unittest.main()
