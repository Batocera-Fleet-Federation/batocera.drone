"""Peer-state teardown must wait for every in-flight health iteration (#344)."""

import threading
import unittest
from unittest import mock

from app.transfer import local_network, peer_workers, swarm_membership


class PeerHealthWorkerShutdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.threads = []
        for patcher in (
            mock.patch.object(peer_workers, "ensure_tailnet_networking"),
            mock.patch.object(local_network, "start_discovery_worker", side_effect=self.discovery),
            mock.patch.object(local_network, "is_local_mode", return_value=True),
            mock.patch.object(local_network, "paired_peers", return_value=[]),
            mock.patch.object(local_network, "save_peer_checks"),
            mock.patch.object(swarm_membership, "propagate_membership"),
            # Enter one iteration immediately, then wait for generation shutdown.
            mock.patch.object(peer_workers, "_sleep_unless_stopped", side_effect=self.wait),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.cleanup_workers)
        self.first_wait = True

    def cleanup_workers(self) -> None:
        self.release.set()
        peer_workers.stop_local_network_workers(timeout=5)
        for thread in self.threads:
            thread.join(5)

    def wait(self, stop, interval, **kwargs):
        if self.first_wait:
            self.first_wait = False
            return stop.is_set()
        return stop.wait(5)

    def discovery(self, settings, fingerprint, stop_event):
        thread = threading.Thread(target=stop_event.wait, daemon=True)
        thread.start()
        self.threads.append(thread)
        return thread

    def block(self, *args, **kwargs):
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("test did not release in-flight worker")
        return []

    def test_success_waits_for_inflight_state_work(self) -> None:
        # Cover cancellation before writes, during a DB write, and during gossip.
        for stage in ("paired_peers", "save_peer_checks", "propagate_membership"):
            with self.subTest(stage=stage):
                self.entered.clear()
                self.release.clear()
                self.first_wait = True
                owner = swarm_membership if stage == "propagate_membership" else local_network
                with mock.patch.object(owner, stage, side_effect=self.block):
                    peer_workers._start_local_network_workers(mock.Mock())
                    health = peer_workers._LOCAL_NETWORK_HEALTH
                    self.assertTrue(self.entered.wait(2))
                    stop = peer_workers._LOCAL_NETWORK_STOP
                    finished = threading.Event()
                    errors = []

                    def shutdown():
                        try:
                            peer_workers.stop_local_network_workers(timeout=3)
                        except Exception as error:
                            errors.append(error)
                        finally:
                            finished.set()

                    stopper = threading.Thread(target=shutdown)
                    stopper.start()
                    self.threads.append(stopper)
                    self.assertTrue(stop.wait(2))
                    self.assertFalse(finished.wait(0.05))
                    self.release.set()
                    stopper.join(4)
                    self.assertTrue(finished.is_set())
                    self.assertEqual(errors, [])
                    self.assertFalse(health.is_alive())
                    self.assertIsNone(peer_workers._LOCAL_NETWORK_HEALTH)
                    self.assertIsNone(peer_workers._LOCAL_NETWORK_STOP)
                    if stage == "paired_peers":
                        local_network.save_peer_checks.assert_not_called()
                        swarm_membership.propagate_membership.assert_not_called()
                    if stage == "save_peer_checks":
                        swarm_membership.propagate_membership.assert_not_called()
                local_network.save_peer_checks.reset_mock()
                swarm_membership.propagate_membership.reset_mock()

    def test_health_timeout_retains_generation_and_blocks_restart_until_retry(self) -> None:
        with mock.patch.object(local_network, "paired_peers", side_effect=self.block):
            peer_workers._start_local_network_workers(mock.Mock())
            self.assertTrue(self.entered.wait(2))
            health = peer_workers._LOCAL_NETWORK_HEALTH
            stop = peer_workers._LOCAL_NETWORK_STOP
            discovery = peer_workers._LOCAL_NETWORK_DISCOVERY
            with self.assertRaises(TimeoutError):
                peer_workers.stop_local_network_workers(timeout=0)
            self.assertTrue(stop.is_set())
            self.assertIs(peer_workers._LOCAL_NETWORK_STOP, stop)
            self.assertIs(peer_workers._LOCAL_NETWORK_HEALTH, health)
            self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, discovery)
            self.assertFalse(peer_workers.local_network_workers_running())
            with self.assertRaises(TimeoutError):
                peer_workers._start_local_network_workers(mock.Mock())
            self.assertIs(peer_workers._LOCAL_NETWORK_STOP, stop)
            self.release.set()
            peer_workers.stop_local_network_workers(timeout=3)
            self.assertFalse(health.is_alive())
            self.assertIsNone(peer_workers._LOCAL_NETWORK_HEALTH)
            peer_workers.stop_local_network_workers(timeout=0)
            peer_workers._start_local_network_workers(mock.Mock())
            self.assertIsNot(peer_workers._LOCAL_NETWORK_STOP, stop)
            peer_workers.stop_local_network_workers(timeout=3)

    def test_discovery_timeout_also_retains_generation_for_retry(self) -> None:
        worker = threading.Thread(target=self.block, daemon=True)
        worker.start()
        self.threads.append(worker)
        with mock.patch.object(local_network, "start_discovery_worker", return_value=worker):
            peer_workers._start_local_network_workers(mock.Mock())
            stop = peer_workers._LOCAL_NETWORK_STOP
            with self.assertRaises(TimeoutError):
                peer_workers.stop_local_network_workers(timeout=0)
            self.assertIs(peer_workers._LOCAL_NETWORK_STOP, stop)
            self.assertIs(peer_workers._LOCAL_NETWORK_DISCOVERY, worker)
            self.release.set()
            peer_workers.stop_local_network_workers(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertIsNone(peer_workers._LOCAL_NETWORK_DISCOVERY)

    def test_cancelled_peer_probe_does_not_publish_checks_or_membership(self) -> None:
        peer = {"drone_id": "peer", "address": "https://192.0.2.1"}
        with mock.patch.object(local_network, "paired_peers", return_value=[peer]), \
                mock.patch.object(peer_workers, "_preferred_peer_address", return_value=peer["address"]), \
                mock.patch.object(peer_workers, "_peer_get_json_for_peer", side_effect=self.block):
            peer_workers._start_local_network_workers(mock.Mock())
            self.assertTrue(self.entered.wait(2))
            health = peer_workers._LOCAL_NETWORK_HEALTH
            with self.assertRaises(TimeoutError):
                peer_workers.stop_local_network_workers(timeout=0)
            self.release.set()
            peer_workers.stop_local_network_workers(timeout=3)
            self.assertFalse(health.is_alive())
            local_network.save_peer_checks.assert_not_called()
            swarm_membership.propagate_membership.assert_not_called()


if __name__ == "__main__":
    unittest.main()
