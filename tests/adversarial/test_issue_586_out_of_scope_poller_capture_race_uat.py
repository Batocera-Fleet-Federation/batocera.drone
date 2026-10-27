"""Out-of-scope evidence found while testing #594 (#586 follow-up). Not a blocking gate for #594.

run_sharing_revocation_poller reads its generation on the thread it runs on, not in the caller that
starts the thread. create_server() starts the thread and returns. If retire_background_pollers() runs
before the thread reaches that read (for example, a create_server() failure after the VPN thread
starts, followed by main()'s finally block), the thread captures the new generation, never sees
itself as retired, and keeps checking after shutdown. Production windows are narrow because
serve_forever() normally runs long after the thread has captured, but the ordering is reachable.

The tick guard added for #586 is not involved; this is the same capture-point shape every
forever-loop poller uses. The gate below forces the ordering deterministically.

Expected behavior: a poller whose thread was started before retirement returns before any check.
This suite fails today: the thread runs checks after the retirement.
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.common import runtime_state


class SharingPollerCaptureRaceOutOfScopeUAT(unittest.TestCase):
    def test_retirement_before_thread_capture_stops_the_poller_before_any_check(self) -> None:
        real_generation = vpn_manager.poller_generation
        gate = threading.Event()

        def gated_generation() -> int:
            # The thread has started; hold its capture until shutdown has retired the generation.
            gate.wait(10)
            return real_generation()

        check = mock.Mock(return_value=False)
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 3:
                # Bound the defective run: a fixed poller exits on the first wake after retirement.
                runtime_state.retire_pollers()

        with mock.patch.object(vpn_manager, "poller_generation", gated_generation), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", check):
            thread = threading.Thread(
                target=vpn_manager.run_sharing_revocation_poller,
                args=(None,),
                name="drone-vpn-sharing-revocation-capture-race",
                daemon=True,
            )
            thread.start()
            runtime_state.retire_pollers()
            gate.set()
            thread.join(10)

        self.assertFalse(thread.is_alive(), "poller thread did not exit after retirement")
        self.assertEqual(
            check.call_count,
            0,
            "a poller started before retirement ran checks after retirement",
        )


if __name__ == "__main__":
    unittest.main()
