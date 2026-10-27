"""Malformed LAN packets must not kill discovery."""

import socket
import threading
import unittest
from unittest import mock

from app.transfer import local_network


class DiscoveryInfinitePortSecurityTests(unittest.TestCase):
    def _assert_packet_does_not_terminate_listener(self, packet: bytes):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        ready = threading.Event()
        failed = threading.Event()
        stop = threading.Event()
        errors = []
        previous_hook = threading.excepthook

        def exception_hook(args):
            errors.append(args.exc_value)
            failed.set()

        settings = mock.Mock(device_id="local-device")
        threading.excepthook = exception_hook
        try:
            with mock.patch.object(local_network, "DISCOVERY_PORT", port), \
                    mock.patch.object(local_network, "_join_multicast_group", side_effect=lambda sock: ready.set()), \
                    mock.patch.object(local_network, "announce", return_value=True), \
                    mock.patch.object(local_network._identity, "own_certificate_fingerprint", return_value=""), \
                    mock.patch.object(local_network, "_load_peer_map", return_value={}):
                worker = local_network.start_discovery_worker(settings, lambda: "", stop_event=stop)
                self.assertTrue(ready.wait(3), "listener did not bind")
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                    sender.sendto(packet, ("127.0.0.1", port))
                failed.wait(1)
                self.assertEqual(errors, [])
                self.assertTrue(worker.is_alive(), "unauthenticated packet killed discovery")
        finally:
            stop.set()
            if "worker" in locals():
                worker.join(3)
            threading.excepthook = previous_hook

    def test_overflowing_json_port_does_not_terminate_listener(self):
        self._assert_packet_does_not_terminate_listener(
            b'{"service":"batocera-drone-local-v1",'
            b'"drone_id":"remote-device","api_port":1e999}'
        )

    def test_deeply_nested_json_does_not_terminate_listener(self):
        self._assert_packet_does_not_terminate_listener(
            b"[" * 1500 + b"0" + b"]" * 1500
        )


if __name__ == "__main__":
    unittest.main()
