"""Issue #615 capture-race evidence, originally filed while testing #594.

The production defect: create_server() starts the VPN sharing poller and returns, so
retire_background_pollers() (main()'s finally, including a create_server() failure after
the thread starts) can run before the worker body runs. A poller must be bound to the
generation current in the caller before Thread.start(), so that retirement still yields
zero checks.

Adjudication of the round-1 dispute: the first version of this test started a raw thread
with args=(settings,) only, which is the generation=None test-hook fallback, not the
create_server() starter. Issue #615 is the create_server() / shutdown path. This suite
now binds args through drone_api._poller_start_args (the helper create_server() uses)
and keeps the original ordering: start the thread, retire, then allow any on-thread
poller_generation() read. A worker that still recaptures on-thread after start() will
adopt the post-retirement generation and fail the zero-check assertion.
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

import app.device.vpn_manager as vpn_manager
import app.drone_api as drone_api
from app.common import runtime_state


class SharingPollerCaptureRaceOutOfScopeUAT(unittest.TestCase):
    def test_retirement_before_thread_capture_stops_the_poller_before_any_check(self) -> None:
        real_generation = vpn_manager.poller_generation
        generation_gate = threading.Event()
        sleep_gate = threading.Event()
        self.addCleanup(generation_gate.set)
        self.addCleanup(sleep_gate.set)

        def gated_generation() -> int:
            # Stall only the on-thread fallback. create_server() / _poller_start_args
            # read runtime_state.poller_generation in the caller and are not gated.
            generation_gate.wait(10)
            return real_generation()

        check = mock.Mock(return_value=False)
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) > 6:
                raise RuntimeError("poller kept sleeping after retirement")
            sleep_gate.wait(10)

        args = drone_api._poller_start_args(vpn_manager.run_sharing_revocation_poller, None)
        self.assertEqual(len(args), 2, "create_server helper must pass the caller-captured generation")
        self.assertIsInstance(args[1], int)

        with mock.patch.object(vpn_manager, "poller_generation", gated_generation), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", check):
            thread = threading.Thread(
                target=vpn_manager.run_sharing_revocation_poller,
                args=args,
                name="drone-vpn-sharing-revocation-capture-race",
                daemon=True,
            )
            thread.start()
            runtime_state.retire_pollers()
            generation_gate.set()
            sleep_gate.set()
            thread.join(10)

        self.assertFalse(thread.is_alive(), "poller thread did not exit after retirement")
        self.assertGreaterEqual(len(sleeps), 1, "poller never entered its loop; start args may have crashed it")
        self.assertEqual(
            check.call_count,
            0,
            "a poller started before retirement ran checks after retirement",
        )


if __name__ == "__main__":
    unittest.main()
