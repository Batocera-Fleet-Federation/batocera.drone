"""A malformed LAN announcement must not terminate the discovery listener."""

import json
import socket
import threading
import unittest
from unittest import mock

from app.transfer import local_network


class DiscoveryPacketTypeSecurityTests(unittest.TestCase):
    def test_json_array_does_not_kill_discovery_listener(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        ready = threading.Event()
        stopped = threading.Event()
        stop = threading.Event()
        prior_hook = threading.excepthook
        failures = []

        def exception_hook(args):
            failures.append(args.exc_value)
            stopped.set()

        threading.excepthook = exception_hook
        try:
            with mock.patch.object(local_network, "DISCOVERY_PORT", port), \
                    mock.patch.object(local_network, "_join_multicast_group", side_effect=lambda sock: ready.set()), \
                    mock.patch.object(local_network, "announce", return_value=True):
                worker = local_network.start_discovery_worker(mock.Mock(), lambda: "", stop_event=stop)
                self.assertTrue(ready.wait(3), "listener did not bind")
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                    sender.sendto(json.dumps([{"service": local_network.DISCOVERY_SERVICE}]).encode(),
                                  ("127.0.0.1", port))
                stopped.wait(1)
                self.assertTrue(worker.is_alive(), "unauthenticated JSON array killed discovery")
                self.assertEqual(failures, [])
        finally:
            stop.set()
            if "worker" in locals():
                worker.join(3)
            threading.excepthook = prior_hook


if __name__ == "__main__":
    unittest.main()
