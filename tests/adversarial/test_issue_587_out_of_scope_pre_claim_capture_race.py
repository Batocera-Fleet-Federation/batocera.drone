"""Out-of-scope probes next to #587: retirement can still miss the VPN poller.

Not blocking #587 assertions. #587 is the per-tick Exception guard so one
unexpected check cannot permanently stop revocation. These cases are a
separate lifecycle hole already tracked as #615: a poller whose thread was
started before retirement can still capture a *new* generation and keep
checking.

Evidence added here, beyond the original #586 generation-read race:

* ``claim_poller()`` only helps once it has returned. Retirement that lands
  after ``Thread.start()`` but before ``claim_poller()`` registers the claim
  still lets the thread take a fresh claim and the post-retirement generation.
* ``create_server()`` still starts the VPN sharing poller without binding the
  pre-start generation, so that window exists in production.
* SMTP and tailnet sharing-revocation pollers never claim at all, so they keep
  the original capture-before-generation-read race.
"""

from __future__ import annotations

import inspect
import threading
import unittest
from unittest import mock

import app.device.smtp_manager as smtp_manager
import app.device.tailnet_service as tailnet_service
import app.device.vpn_manager as vpn_manager
import app.drone_api as drone_api
from app.common import runtime_state


class OutOfScopePreClaimCaptureRaceUAT(unittest.TestCase):
    def setUp(self) -> None:
        runtime_state.retire_pollers()
        self.addCleanup(runtime_state.retire_pollers)

    def test_retirement_before_claim_returns_still_lets_the_vpn_poller_check(self) -> None:
        # Thread has entered the poller and is blocked inside claim_poller. Shutdown
        # then retires. When the claim proceeds it is a *new* claim under the new
        # generation, so poller_claim_retired is false and checks still run.
        real_claim = vpn_manager.claim_poller
        entered = threading.Event()
        gate = threading.Event()

        def gated_claim() -> int:
            entered.set()
            if not gate.wait(10):
                raise AssertionError("claim gate timed out")
            return real_claim()

        check = mock.Mock(return_value=False)
        sleeps: list[float] = []

        def fake_sleep(_seconds: float) -> None:
            sleeps.append(1)
            if len(sleeps) == 3:
                runtime_state.retire_pollers()

        with mock.patch.object(vpn_manager, "claim_poller", gated_claim), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", check):
            thread = threading.Thread(
                target=vpn_manager.run_sharing_revocation_poller,
                args=(None,),
                name="issue-587-pre-claim-capture-race",
                daemon=True,
            )
            thread.start()
            self.assertTrue(entered.wait(10), "poller never reached claim_poller")
            runtime_state.retire_pollers()
            gate.set()
            thread.join(10)

        self.assertFalse(thread.is_alive(), "poller thread did not exit")
        self.assertEqual(
            check.call_count,
            0,
            "a poller started before retirement ran checks after retirement "
            "because claim_poller completed after retire_pollers",
        )

    def test_create_server_starts_vpn_sharing_poller_without_binding_generation(self) -> None:
        source = inspect.getsource(drone_api.create_server)
        start = source.find("target=_vpn_manager.run_sharing_revocation_poller")
        self.assertNotEqual(start, -1, "create_server must start the VPN sharing poller")
        window = source[start:start + 220]
        self.assertIn(
            "generation=",
            window,
            "create_server starts the VPN sharing poller without binding the "
            "pre-start generation, so a retirement before the thread is scheduled "
            "still lets it capture the new generation (#615)",
        )

    def test_smtp_sharing_poller_keeps_generation_capture_race(self) -> None:
        real_generation = smtp_manager.poller_generation
        gate = threading.Event()

        def gated_generation() -> int:
            gate.wait(10)
            return real_generation()

        check = mock.Mock(return_value=False)
        sleeps: list[float] = []

        def fake_sleep(_seconds: float) -> None:
            sleeps.append(1)
            if len(sleeps) == 3:
                runtime_state.retire_pollers()

        with mock.patch.object(smtp_manager, "poller_generation", gated_generation), \
                mock.patch.object(smtp_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(smtp_manager, "check_sharing_revocation", check):
            thread = threading.Thread(
                target=smtp_manager.run_sharing_revocation_poller,
                args=(None,),
                name="issue-587-smtp-capture-race",
                daemon=True,
            )
            thread.start()
            runtime_state.retire_pollers()
            gate.set()
            thread.join(10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(
            check.call_count,
            0,
            "SMTP sharing-revocation poller ran checks after a retirement that "
            "landed before it captured poller_generation",
        )

    def test_tailnet_sharing_poller_keeps_generation_capture_race(self) -> None:
        real_generation = tailnet_service.poller_generation
        gate = threading.Event()

        def gated_generation() -> int:
            gate.wait(10)
            return real_generation()

        check = mock.Mock(return_value=False)
        sleeps: list[float] = []

        def fake_sleep(_seconds: float) -> None:
            sleeps.append(1)
            if len(sleeps) == 3:
                runtime_state.retire_pollers()

        with mock.patch.object(tailnet_service, "poller_generation", gated_generation), \
                mock.patch.object(tailnet_service.time, "sleep", new=fake_sleep), \
                mock.patch.object(tailnet_service, "check_tailnet_sharing_revocation", check):
            thread = threading.Thread(
                target=tailnet_service.run_tailnet_sharing_revocation_poller,
                args=(None,),
                name="issue-587-tailnet-capture-race",
                daemon=True,
            )
            thread.start()
            runtime_state.retire_pollers()
            gate.set()
            thread.join(10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(
            check.call_count,
            0,
            "tailnet sharing-revocation poller ran checks after a retirement that "
            "landed before it captured poller_generation",
        )


if __name__ == "__main__":
    unittest.main()
