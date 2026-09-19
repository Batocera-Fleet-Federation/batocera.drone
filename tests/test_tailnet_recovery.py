import json
import unittest
from unittest import mock

from app.device import tailnet_service as service


class TailnetRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.payload = {"BackendState": "Running", "TailscaleIPs": ["100.64.0.1"],
                        "Peer": {"peer": {"TailscaleIPs": ["100.64.0.2"]}}}
        self.link = [{"flags": ["UP"], "addr_info": [{"local": "100.64.0.1"}]}]
        for patch in (mock.patch.object(service.Path, "read_text", return_value="123"),
                      mock.patch.object(service.Path, "read_bytes", return_value=b"tailscaled\0--tun=tailscale0\0")):
            patch.start()
            self.addCleanup(patch.stop)

    def result(self, data, code=0):
        return mock.Mock(returncode=code, stdout=json.dumps(data))

    def test_daemon_running_but_interface_down(self):
        with mock.patch.object(service.subprocess, "run", return_value=self.result([{"flags": []}])):
            self.assertEqual(service._kernel_tailnet_failure(self.payload), "tailscale0 is down")

    def test_missing_address_and_missing_route(self):
        with mock.patch.object(service.subprocess, "run", return_value=self.result([{"flags": ["UP"]}])):
            self.assertIn("address", service._kernel_tailnet_failure(self.payload))
        with mock.patch.object(service.subprocess, "run", side_effect=[self.result(self.link), self.result([{"dev": "wlan0"}])]):
            self.assertIn("route", service._kernel_tailnet_failure(self.payload))

    def test_healthy_interface_and_route(self):
        with mock.patch.object(service.subprocess, "run", side_effect=[self.result(self.link), self.result([{"dev": "tailscale0"}])]):
            self.assertIsNone(service._kernel_tailnet_failure(self.payload))

    def test_logged_out_and_userspace_nodes_are_not_restarted(self):
        with mock.patch.object(service.subprocess, "run") as run:
            self.assertIsNone(service._kernel_tailnet_failure({"BackendState": "Stopped"}))
            with mock.patch.object(service.Path, "read_bytes", return_value=b"tailscaled\0--tun=userspace-networking\0"):
                self.assertIsNone(service._kernel_tailnet_failure(self.payload))
            run.assert_not_called()

    def test_recovery_restarts_once_and_verifies_then_cools_down(self):
        with mock.patch.object(service.sys, "platform", "linux"), \
             mock.patch.object(service.Path, "is_file", return_value=True), \
             mock.patch.object(service, "_NETWORK_REPAIR_LAST", float("-inf")), \
             mock.patch.object(service, "_run_cli", return_value=self.result(self.payload)), \
             mock.patch.object(service, "_kernel_tailnet_failure", side_effect=["tailscale0 is down", None]) as check, \
             mock.patch.object(service.subprocess, "run", return_value=self.result({})) as run:
            service._repair_tailnet_data_path()
            service._repair_tailnet_data_path()
            run.assert_called_once_with(["sh", str(service.TAILNET_SERVICE), "restart"], capture_output=True, text=True, timeout=45)
            self.assertEqual(check.call_count, 2)

    def test_invalid_status_does_not_restart(self):
        with mock.patch.object(service.sys, "platform", "linux"), \
             mock.patch.object(service.Path, "is_file", return_value=True), \
             mock.patch.object(service, "_NETWORK_REPAIR_LAST", float("-inf")), \
             mock.patch.object(service, "_run_cli", return_value=mock.Mock(returncode=0, stdout="invalid")), \
             mock.patch.object(service.subprocess, "run") as run:
            service._repair_tailnet_data_path()
            run.assert_not_called()

    def test_watchdog_checks_data_path_even_when_daemon_is_running(self):
        from app.transfer import peer_workers
        targets = {}
        def thread(*args, **kwargs):
            targets[kwargs["name"]] = kwargs["target"]
            return mock.Mock()
        with mock.patch.object(peer_workers, "Thread", side_effect=thread), \
             mock.patch.object(peer_workers._local_network, "start_discovery_worker", side_effect=RuntimeError("stop setup")):
            with self.assertRaisesRegex(RuntimeError, "stop setup"):
                peer_workers._start_local_network_workers(mock.Mock())
        with mock.patch.object(peer_workers.time, "sleep", side_effect=[None, RuntimeError("stop loop")]), \
             mock.patch.object(peer_workers, "tailnet_status", return_value={"running": True}), \
             mock.patch.object(peer_workers, "_repair_tailnet_data_path") as repair:
            with self.assertRaisesRegex(RuntimeError, "stop loop"):
                targets["drone-tailnet-watchdog"]()
            repair.assert_called_once_with()
