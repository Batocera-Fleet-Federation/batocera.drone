"""Issue #349 UAT: a live LAN generation is retired when a real HTTP shutdown raises.

The protected #152 harness callbacks call server.shutdown() and then
stop_local_network_workers(). When the HTTP shutdown raises, the production
server wrapper must already have stopped the independent LAN generation.

This suite uses a real socketserver loop on loopback. Only the LAN discovery and
peer-health handles are simulated (threads that wait on the generation event), so
no multicast socket, tailnet process, or userdata is touched.
"""

import socketserver
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from unittest import mock

from app import drone_api
from app.transfer import peer_workers


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


_REAL_BASE_SHUTDOWN = socketserver.BaseServer.shutdown


def _failing_shutdown_after_real_stop(self):
    # Stop the real serve loop first, then fail: the HTTP side is genuinely down
    # while the caller still receives an error from shutdown.
    _REAL_BASE_SHUTDOWN(self)
    raise RuntimeError("injected shutdown failure after the serve loop stopped")


class LiveGenerationRetirementTests(unittest.TestCase):
    def setUp(self):
        self.generation_stop = threading.Event()
        self.addCleanup(self.generation_stop.set)
        self.discovery = threading.Thread(
            target=self.generation_stop.wait, args=(10,), daemon=True, name="drone-local-discovery")
        self.health = threading.Thread(
            target=self.generation_stop.wait, args=(10,), daemon=True, name="drone-local-peer-health")
        for name, value in (("_LOCAL_NETWORK_STOP", self.generation_stop),
                            ("_LOCAL_NETWORK_DISCOVERY", self.discovery),
                            ("_LOCAL_NETWORK_HEALTH", self.health)):
            patcher = mock.patch.object(peer_workers, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.discovery.start()
        self.health.start()

        try:
            self.server = drone_api.DroneThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
        except PermissionError as error:
            self.skipTest(f"Socket bind is not allowed in this environment: {error}")
        self.addCleanup(self.server.server_close)
        self.serve_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.serve_thread.start()

    def test_live_generation_is_retired_when_real_shutdown_raises(self):
        self.assertTrue(peer_workers.local_network_workers_running(),
                        "precondition: the simulated generation must be live")

        with mock.patch.object(socketserver.BaseServer, "shutdown", _failing_shutdown_after_real_stop):
            with self.assertRaisesRegex(RuntimeError, "injected shutdown failure"):
                self.server.shutdown()

        self.serve_thread.join(timeout=5)
        self.assertFalse(self.serve_thread.is_alive(), "the HTTP serve loop must have stopped")
        self.assertFalse(peer_workers.local_network_workers_running())
        self.discovery.join(timeout=5)
        self.health.join(timeout=5)
        self.assertFalse(self.discovery.is_alive(), "discovery worker must be retired")
        self.assertFalse(self.health.is_alive(), "peer-health worker must be retired")

    def test_harness_follow_up_stop_after_failed_shutdown_is_safe(self):
        with mock.patch.object(socketserver.BaseServer, "shutdown", _failing_shutdown_after_real_stop):
            with self.assertRaises(RuntimeError):
                self.server.shutdown()

        # The protected harness finally-block calls stop again. It must be a no-op now.
        peer_workers.stop_local_network_workers()
        self.assertFalse(peer_workers.local_network_workers_running())

    def test_successful_shutdown_leaves_the_generation_to_its_owner(self):
        self.server.shutdown()
        self.server.server_close()
        self.serve_thread.join(timeout=5)

        self.assertTrue(peer_workers.local_network_workers_running(),
                        "a clean HTTP shutdown must not silently retire the LAN generation")
        self.assertTrue(self.discovery.is_alive())
        peer_workers.stop_local_network_workers()
        self.discovery.join(timeout=5)
        self.assertFalse(self.discovery.is_alive())


if __name__ == "__main__":
    unittest.main()
