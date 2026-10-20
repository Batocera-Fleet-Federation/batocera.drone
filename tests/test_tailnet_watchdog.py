import unittest
from unittest import mock

from app.transfer import local_network, peer_workers


class TailnetWatchdogTests(unittest.TestCase):
    def test_watchdog_survives_a_failed_pass_and_repairs_on_the_next_tick(self) -> None:
        # One raising status call must not end the watchdog thread; the next
        # tick should still run the repair path.
        status_calls = []

        def flaky_status():
            status_calls.append(1)
            if len(status_calls) == 1:
                raise RuntimeError("tailscale status blew up")
            return {"running": True}

        # Two ticks run, then the generation is stopped.
        waits = iter([False, False, True])

        threads = []

        def capture_thread(*args, **kwargs):
            threads.append(kwargs)
            return mock.Mock()

        with mock.patch.object(peer_workers, "Thread", side_effect=capture_thread), \
                mock.patch.object(peer_workers, "_wait_for_generation_stop", side_effect=lambda stop, interval: next(waits)), \
                mock.patch.object(peer_workers, "tailnet_status", side_effect=flaky_status), \
                mock.patch.object(peer_workers, "ensure_tailnet_networking") as ensure, \
                mock.patch.object(peer_workers, "_repair_tailnet_data_path") as repair, \
                mock.patch.object(local_network, "start_discovery_worker", return_value=mock.Mock()), \
                mock.patch.object(peer_workers, "DroneCertificateManager"):
            peer_workers._start_local_network_workers(mock.Mock())
            watchdog = next(kwargs["target"] for kwargs in threads if kwargs.get("name") == "drone-tailnet-watchdog")
            with mock.patch("sys.stderr"):
                watchdog()

        self.assertEqual(len(status_calls), 2)
        ensure.assert_not_called()
        repair.assert_called_once()
        peer_workers.stop_local_network_workers(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
