"""Issue #312 signal-driven shutdown of a real ``drone_api.main`` process.

Expected behavior, derived from the issue before reading the delivery:

* Shutting the Drone down must retire the local-network generation.
* The device stops the service with a plain ``kill`` (SIGTERM) in
  ``app/service_bootstrap.sh`` ``stop_app``, and Ctrl-C delivers SIGINT. Both
  are process-exit paths, so both must set the generation's stop event and
  close and join LAN discovery.

Each scenario starts ``drone_api.main`` in a child interpreter with only the
network, identity and bootstrap edges replaced. The discovery double writes
marker files on close and join, so the parent can observe what the child did.
SIGKILL is not tested: no process can run cleanup under it.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[2]

# Child entrypoint. Only the outer boundaries are replaced; ``main`` and the
# real generation bookkeeping in ``peer_workers`` run unchanged.
CHILD_SCRIPT = r"""
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app import drone_api
from app.transfer import local_network, peer_workers
from app.web import api_bridge

marker = Path(os.environ["SHUTDOWN_MARKER_DIR"])


class Discovery:
    def close_discovery_socket(self):
        (marker / "discovery-closed").write_text("1")

    def join(self, timeout=None):
        (marker / "discovery-joined").write_text("1")


def fake_start_discovery(settings, fingerprint, on_discovery=None, stop_event=None):
    return Discovery()


def serve_forever():
    (marker / "ready").write_text("1")
    while True:
        time.sleep(0.05)


settings = SimpleNamespace(
    use_fake_data=False,
    https_port=8443,
    http_only=True,
    log_dir=marker,
    stdout_log_file="drone.stdout.log",
    stderr_log_file="drone.stderr.log",
    username="admin",
)
server = SimpleNamespace(auth=None, serve_forever=serve_forever)


def create_server(received_settings):
    peer_workers._start_local_network_workers(received_settings)
    return server


patches = [
    mock.patch.object(drone_api.Settings, "from_env", return_value=settings),
    mock.patch.object(drone_api, "_configure_rotating_logs"),
    mock.patch.object(drone_api._identity, "ensure_identity", return_value=SimpleNamespace(settings=settings)),
    mock.patch.object(drone_api, "create_server", side_effect=create_server),
    mock.patch.object(drone_api, "_start_drone_auto_update_poller"),
    mock.patch.object(api_bridge, "maybe_start"),
    mock.patch.object(peer_workers, "ensure_tailnet_networking"),
    mock.patch.object(local_network, "start_discovery_worker", side_effect=fake_start_discovery),
]
for patch in patches:
    patch.start()

drone_api.main()
"""


@unittest.skipUnless(os.name == "posix", "POSIX signal delivery")
class SignalShutdownTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.marker = Path(tmp.name)
        self.process: subprocess.Popen | None = None
        self.addCleanup(self._kill_child)

    def _kill_child(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)

    def _start_child(self) -> subprocess.Popen:
        env = dict(os.environ, PYTHONPATH=str(REPO), SHUTDOWN_MARKER_DIR=str(self.marker))
        stderr_file = open(self.marker / "stderr.log", "w")
        self.addCleanup(stderr_file.close)
        self.process = subprocess.Popen(
            [sys.executable, "-c", CHILD_SCRIPT],
            cwd=str(REPO),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
        )
        return self.process

    def _wait_until_serving(self) -> None:
        ready = self.marker / "ready"
        deadline = time.monotonic() + 60
        while not ready.exists():
            if self.process.poll() is not None:
                self.fail(f"child exited before serving: {self._stderr()}")
            if time.monotonic() > deadline:
                self.fail(f"child never reached serve_forever: {self._stderr()}")
            time.sleep(0.05)

    def _stderr(self) -> str:
        return (self.marker / "stderr.log").read_text(errors="replace")[-2000:]

    def _shutdown_with(self, signum: int) -> None:
        self._start_child()
        self._wait_until_serving()
        self.process.send_signal(signum)
        try:
            self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.fail("child did not exit after signal")

    def test_sigint_shutdown_retires_generation(self) -> None:
        # Ctrl-C raises KeyboardInterrupt inside serve_forever, which main's
        # finally must turn into a retired generation before the process exits.
        self._shutdown_with(signal.SIGINT)

        self.assertTrue((self.marker / "discovery-closed").exists(), self._stderr())
        self.assertTrue((self.marker / "discovery-joined").exists(), self._stderr())

    def test_sigterm_shutdown_retires_generation(self) -> None:
        # service_bootstrap.sh stop_app sends SIGTERM. The Drone has no handler
        # for it, so Python ends the process without unwinding main's finally,
        # and the generation's discovery is never closed or joined by the app.
        self._shutdown_with(signal.SIGTERM)

        self.assertTrue((self.marker / "discovery-closed").exists(), self._stderr())
        self.assertTrue((self.marker / "discovery-joined").exists(), self._stderr())


if __name__ == "__main__":
    unittest.main()
