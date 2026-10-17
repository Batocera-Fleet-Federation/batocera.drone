"""Acceptance boundaries for #344's peer-health shutdown write barrier."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from app.transfer import local_network, peer_workers, swarm_membership


class PeerHealthShutdownBarrierUAT(unittest.TestCase):
    """Use controlled in-flight health work; never rely on scheduler timing."""

    def setUp(self) -> None:
        peer_workers.stop_local_network_workers(timeout=1)
        self.entered = threading.Event()
        self.release = threading.Event()
        self._first_sleep = True
        self.discovery_threads: list[threading.Thread] = []
        self.patchers = [
            mock.patch.object(peer_workers, "ensure_tailnet_networking"),
            mock.patch.object(peer_workers, "bootstrap_tailnet_from_swarm"),
            mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True}),
            mock.patch.object(peer_workers, "_repair_tailnet_data_path"),
            mock.patch.object(local_network, "start_discovery_worker", side_effect=self._discovery),
            mock.patch.object(local_network, "is_local_mode", return_value=True),
            mock.patch.object(peer_workers, "_sleep_unless_stopped", side_effect=self._health_wait),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        # Release any deliberately in-flight write before retiring this fixture.
        self.release.set()
        peer_workers.stop_local_network_workers(timeout=3)
        for thread in self.discovery_threads:
            thread.join(1)
        for patcher in reversed(self.patchers):
            patcher.stop()

    def _discovery(self, _settings, _fingerprint, stop_event):
        worker = threading.Thread(target=stop_event.wait, daemon=True)
        worker.start()
        self.discovery_threads.append(worker)
        return worker

    def _health_wait(self, stop, _interval, **_kwargs):
        if self._first_sleep:
            self._first_sleep = False
            return False
        return stop.wait(30)

    def _stop_in_thread(self, errors, returned):
        try:
            peer_workers.stop_local_network_workers(timeout=2)
        except BaseException as error:  # surface a shutdown failure in the test thread
            errors.append(error)
        finally:
            returned.set()

    def test_successful_stop_waits_for_inflight_state_write_and_prevents_later_gossip(self) -> None:
        """A successful return is only permitted after an in-flight DB write exits."""
        writes: list[str] = []

        def blocked_save(_settings, _checks):
            self.entered.set()
            self.assertTrue(self.release.wait(2), "test did not release the state write")
            writes.append("peer-checks")

        with mock.patch.object(local_network, "paired_peers", return_value=[]), \
                mock.patch.object(local_network, "save_peer_checks", side_effect=blocked_save), \
                mock.patch.object(swarm_membership, "propagate_membership") as gossip:
            peer_workers._start_local_network_workers(mock.Mock(device_id="uat-344"))
            self.assertTrue(self.entered.wait(2), "health worker never entered save_peer_checks")
            health = peer_workers._LOCAL_NETWORK_HEALTH
            errors: list[BaseException] = []
            returned = threading.Event()
            stopper = threading.Thread(target=self._stop_in_thread, args=(errors, returned))
            stopper.start()

            self.assertFalse(returned.wait(0.1), "stop returned while health could still write state")
            self.release.set()
            stopper.join(3)

            self.assertTrue(returned.is_set())
            self.assertEqual(errors, [])
            self.assertEqual(writes, ["peer-checks"])
            gossip.assert_not_called()
            self.assertFalse(health.is_alive())
            self.assertIsNone(peer_workers._LOCAL_NETWORK_HEALTH)

    def test_cancelled_inflight_probe_cannot_publish_checks_after_stop(self) -> None:
        """Cancellation during a peer request suppresses both subsequent state writers."""
        peer = {"drone_id": "peer-344", "address": "https://192.0.2.44"}

        def blocked_probe(*_args, **_kwargs):
            self.entered.set()
            self.assertTrue(self.release.wait(2), "test did not release the peer probe")
            return {}, peer["address"]

        with mock.patch.object(local_network, "paired_peers", return_value=[peer]), \
                mock.patch.object(peer_workers, "_preferred_peer_address", return_value=peer["address"]), \
                mock.patch.object(peer_workers, "_peer_get_json_for_peer", side_effect=blocked_probe), \
                mock.patch.object(local_network, "save_peer_checks") as save, \
                mock.patch.object(swarm_membership, "propagate_membership") as gossip:
            peer_workers._start_local_network_workers(mock.Mock(device_id="uat-344"))
            self.assertTrue(self.entered.wait(2), "health worker never entered peer probe")
            errors: list[BaseException] = []
            returned = threading.Event()
            stopper = threading.Thread(target=self._stop_in_thread, args=(errors, returned))
            stopper.start()

            self.assertFalse(returned.wait(0.1), "stop returned while health probe was in flight")
            self.release.set()
            stopper.join(3)

            self.assertTrue(returned.is_set())
            self.assertEqual(errors, [])
            save.assert_not_called()
            gossip.assert_not_called()

    def test_zero_timeout_is_an_explicit_failure_that_retains_the_generation_for_retry(self) -> None:
        """A timed-out stop must not discard a live handle and permit a replacement."""
        def blocked_peers(_settings):
            self.entered.set()
            self.assertTrue(self.release.wait(2), "test did not release paired_peers")
            return []

        with mock.patch.object(local_network, "paired_peers", side_effect=blocked_peers):
            peer_workers._start_local_network_workers(mock.Mock(device_id="uat-344"))
            self.assertTrue(self.entered.wait(2), "health worker never read paired peers")
            stop = peer_workers._LOCAL_NETWORK_STOP
            health = peer_workers._LOCAL_NETWORK_HEALTH

            with self.assertRaises(TimeoutError):
                peer_workers.stop_local_network_workers(timeout=0)

            self.assertTrue(stop.is_set())
            self.assertIs(peer_workers._LOCAL_NETWORK_STOP, stop)
            self.assertIs(peer_workers._LOCAL_NETWORK_HEALTH, health)
            self.release.set()
            peer_workers.stop_local_network_workers(timeout=2)
            self.assertFalse(health.is_alive())
            self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)

    def test_finite_timeout_does_not_report_a_successful_barrier_before_inflight_work_exits(self) -> None:
        """The historical blocked-peer ordering must time out, then allow retry."""
        def blocked_peers(_settings):
            self.entered.set()
            self.assertTrue(self.release.wait(2), "test did not release paired_peers")
            return []

        with mock.patch.object(local_network, "paired_peers", side_effect=blocked_peers), \
                mock.patch.object(local_network, "save_peer_checks") as save, \
                mock.patch.object(swarm_membership, "propagate_membership") as gossip:
            peer_workers._start_local_network_workers(mock.Mock(device_id="uat-344"))
            self.assertTrue(self.entered.wait(2), "health worker never read paired peers")
            stop = peer_workers._LOCAL_NETWORK_STOP
            health = peer_workers._LOCAL_NETWORK_HEALTH

            with self.assertRaises(TimeoutError):
                peer_workers.stop_local_network_workers(timeout=0.05)

            self.assertTrue(stop.is_set())
            self.assertIs(peer_workers._LOCAL_NETWORK_STOP, stop)
            self.assertIs(peer_workers._LOCAL_NETWORK_HEALTH, health)
            self.assertTrue(health.is_alive(), "a timeout must not pretend the worker retired")
            # Releasing after the failed stop must not allow the cancelled iteration
            # to write peer state; a second successful stop establishes the barrier.
            self.release.set()
            peer_workers.stop_local_network_workers(timeout=2)
            self.assertFalse(health.is_alive())
            save.assert_not_called()
            gossip.assert_not_called()


if __name__ == "__main__":
    unittest.main()
