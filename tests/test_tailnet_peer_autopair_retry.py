"""The periodic tailnet-peer discover+auto-pair retry worker.

Before this, a Drone that successfully enrolled in a tailnet (on its own or
via the bootstrap-retry worker) only ever ran the discover-and-auto-pair pass
against its tailnet peers when a human opened the Swarm page and clicked
Discover/Refresh -- enrollment converged on its own, pairing did not. This
mirrors ``TailnetBootstrapRetryTests`` in tests/test_issue_139_swarm_tailnet_uat.py.
"""

import unittest
from unittest import mock

from app.transfer import peer_workers
from app.web.handlers_network import HandlersNetworkMixin


class TailnetPeerAutopairRetryTests(unittest.TestCase):
    def _retry_target(self):
        targets = {}

        def thread(*args, **kwargs):
            targets[kwargs["name"]] = kwargs["target"]
            return mock.Mock()

        with mock.patch.object(peer_workers, "Thread", side_effect=thread), \
                mock.patch.object(peer_workers._local_network, "start_discovery_worker", side_effect=RuntimeError("stop setup")):
            with self.assertRaisesRegex(RuntimeError, "stop setup"):
                peer_workers._start_local_network_workers(mock.Mock())
        return targets["drone-tailnet-peer-autopair"]

    def _run_one_pass(self, status, sync=None):
        sync = sync or mock.Mock(return_value=[])
        with mock.patch.object(peer_workers, "_wait_for_generation_stop", side_effect=[False, RuntimeError("stop loop")]), \
                mock.patch.object(peer_workers, "tailnet_status", return_value=status), \
                mock.patch.object(HandlersNetworkMixin, "_sync_tailnet_peers", sync):
            with self.assertRaisesRegex(RuntimeError, "stop loop"):
                self._retry_target()()
        return sync

    def test_enrolled_drone_runs_the_discover_and_autopair_pass(self):
        sync = self._run_one_pass({"installed": True, "running": True, "enrolled": True})
        sync.assert_called_once()

    def test_not_enrolled_drone_does_not_probe_peers(self):
        self._run_one_pass({"installed": True, "running": False, "enrolled": False}).assert_not_called()

    def test_sync_failure_does_not_kill_the_retry_loop(self):
        sync = mock.Mock(side_effect=OSError("peer exploded"))
        with mock.patch.object(peer_workers, "_wait_for_generation_stop", side_effect=[False, False, RuntimeError("stop loop")]), \
                mock.patch.object(peer_workers, "tailnet_status", return_value={"enrolled": True}), \
                mock.patch.object(HandlersNetworkMixin, "_sync_tailnet_peers", sync):
            with self.assertRaisesRegex(RuntimeError, "stop loop"):
                self._retry_target()()
        self.assertEqual(sync.call_count, 2)

    def test_interval_is_configurable_with_a_floor(self):
        waits = []
        with mock.patch.dict("os.environ", {"DRONE_TAILNET_PEER_SYNC_INTERVAL_SECONDS": "5"}), \
                mock.patch.object(peer_workers, "_wait_for_generation_stop", side_effect=lambda stop, interval: (waits.append(interval), (_ for _ in ()).throw(RuntimeError("stop")))[0]):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                self._retry_target()()
        self.assertEqual(waits, [60])

    def test_helper_has_no_server_attribute_so_cert_activation_falls_back(self):
        # The worker builds a bare HandlersNetworkMixin with only ``.settings``
        # set -- no in-flight HTTP request, so no ``.server``. This is the
        # precondition _activate_local_peer_certificate's fallback depends on;
        # covered end-to-end in tests/test_handlers_network_cert_activation.py.
        targets = {}

        def thread(*args, **kwargs):
            targets[kwargs["name"]] = kwargs["target"]
            return mock.Mock()

        with mock.patch.object(peer_workers, "Thread", side_effect=thread), \
                mock.patch.object(peer_workers._local_network, "start_discovery_worker", side_effect=RuntimeError("stop setup")):
            with self.assertRaisesRegex(RuntimeError, "stop setup"):
                peer_workers._start_local_network_workers(mock.Mock())

        seen = {}

        def capture_self(self, status):
            seen["has_server"] = hasattr(self, "server")
            return []

        with mock.patch.object(peer_workers, "_wait_for_generation_stop", side_effect=[False, RuntimeError("stop loop")]), \
                mock.patch.object(peer_workers, "tailnet_status", return_value={"enrolled": True}), \
                mock.patch.object(HandlersNetworkMixin, "_sync_tailnet_peers", capture_self):
            with self.assertRaisesRegex(RuntimeError, "stop loop"):
                targets["drone-tailnet-peer-autopair"]()
        self.assertFalse(seen["has_server"])


if __name__ == "__main__":
    unittest.main()
