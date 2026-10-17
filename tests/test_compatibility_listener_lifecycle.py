"""Compatibility HTTPS listeners follow the returned server lifecycle (#340)."""

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler

from app.drone_api import DroneThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class CompatibilityListenerLifecycleTests(unittest.TestCase):
    def test_shutdown_and_close_release_owned_compatibility_listener(self):
        main = DroneThreadingHTTPServer(("127.0.0.1", _free_port()), _Handler)
        compat_port = _free_port()
        compat = DroneThreadingHTTPServer(("127.0.0.1", compat_port), _Handler)
        main._owned_compatibility_servers = [compat]
        main_thread = threading.Thread(target=main.serve_forever, daemon=True)
        compat_thread = threading.Thread(target=compat.serve_forever, daemon=True)
        main.thread = main_thread
        compat.thread = compat_thread
        main_thread.start()
        compat_thread.start()
        main.shutdown()
        main.server_close()
        main_thread.join(timeout=2)
        compat_thread.join(timeout=2)
        self.assertFalse(main_thread.is_alive())
        self.assertFalse(compat_thread.is_alive())

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", compat_port))


if __name__ == "__main__":
    unittest.main()
