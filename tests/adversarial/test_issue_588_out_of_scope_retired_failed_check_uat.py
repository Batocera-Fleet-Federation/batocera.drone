"""Out-of-scope evidence for #588: a retired poller still reconciles RGS after a failed check.

Already tracked as #606 ("Retired VPN self-heal poller still reconciles RGS after
a failed check"). This suite supplies the executable reproduction for that entry
and is deliberately kept separate from the in-scope #588 suites. It is evidence
for the worker to file, not a blocking acceptance test for #588.

The gate was introduced by the #588 change to run_self_heal_poller:
``if not check_failed and poller_retired(generation)`` skips the retirement gate
after any raised check. The shutdown rule in run_self_heal_poller (a retired
generation must not perform further RGS actions) is unchanged from #582.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class OutOfScopeRetiredFailedCheckUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-588-out-of-scope-retired",
        }, clear=True):
            self.settings = Settings.from_env()

    def test_raised_self_heal_then_retirement_performs_no_rgs_action(self) -> None:
        retired_now = {"value": False}
        reconcile_calls: list[object] = []
        sleep_calls: list[float] = []

        def check_retires_then_raises(_settings: object) -> None:
            # Shutdown retires the generation while this tick's check is still in flight.
            retired_now["value"] = True
            raise RuntimeError("openvpn log: bad \udcff path")

        def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        def retired(_generation: int) -> bool:
            return retired_now["value"]

        with mock.patch.object(vpn_manager, "poller_generation", return_value=588), \
                mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_and_self_heal", check_retires_then_raises), \
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel",
                                  side_effect=lambda _s: reconcile_calls.append(_s)):
            vpn_manager.run_self_heal_poller(self.settings)

        self.assertEqual(
            reconcile_calls, [],
            "a generation retired during a raised self-heal check must not reconcile RGS (#606)",
        )
        self.assertEqual(len(sleep_calls), 1, "the retired poller must return without another tick")


if __name__ == "__main__":
    unittest.main()
