"""Issue #267 adversarial UAT: observe the hold from the *peer's* side, and in flat import mode.

Expected, derived from the issue and CLAUDE.md ("outbound peer TLS all gated; the pair-by-address
peer-info fetch raises PeerIdentityHold, 503 from the handlers") before reading the patch:

* While swarm networking is held, a real TCP listener standing in for the peer must see ZERO
  connection attempts from pair-by-address (any scheme, with or without a pairing code or a
  confirmed fingerprint) and from the pairing TLS probe. Patching Python-level primitives
  cannot catch a dial that goes through a helper nobody patched; the listener can.
* The same call against the same listener, once the hold is lifted, does connect (control: it
  proves the listener is reachable and the zero above is caused by the gate, not by the setup).
* The deployed entrypoint is the flat layout (``PYTHONPATH=app``). The 503 mapping relies on
  ``except PeerIdentityHold`` matching the class raised in ``transfer.peer_connectivity``; a
  duplicated module (``app.transfer...`` vs ``transfer...``) would silently turn the hold into a
  502 "could not reach". A fresh interpreter in flat mode must give the same 503 and no dial.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning
from app.transfer import peer_connectivity
from app.web import handlers_network

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"
FINGERPRINT = "ab" * 32
HOLD_MESSAGE = "peer pairing is held pending Drone identity verification"


def _hold() -> None:
    identity_provisioning._publish(
        identity_provisioning.State.RECOVERY_REQUIRED, "held", "adversarial hold", False
    )


class _Listener:
    """A plain TCP server that counts accepted connections and drops them."""

    def __init__(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self._server.settimeout(0.05)
        self.port = self._server.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except (socket.timeout, OSError):
                continue
            self.accepted += 1
            connection.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._server.close()


class RealListenerSeesNothingWhileHeldTests(unittest.TestCase):
    def setUp(self) -> None:
        identity_provisioning.reset_runtime_state_for_tests()
        self.addCleanup(identity_provisioning.reset_runtime_state_for_tests)
        self.listener = _Listener()
        self.addCleanup(self.listener.close)
        patcher = mock.patch.object(handlers_network._local_network, "is_local_mode", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        saver = mock.patch.object(handlers_network._local_network, "save_paired_peer")
        self.save = saver.start()
        self.addCleanup(saver.stop)
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.MagicMock(use_fake_data=False)
        self.handler.settings.device_id = "drone-self"
        self.handler._send_json = mock.Mock()

    def _addresses(self) -> tuple[str, ...]:
        port = self.listener.port
        return (
            f"https://127.0.0.1:{port}",
            f"http://127.0.0.1:{port}",
            f"127.0.0.1:{port}",
            f"localhost:{port}",
        )

    def test_held_pair_by_address_never_reaches_the_peer(self) -> None:
        _hold()
        payloads = (
            {},
            {"pairing_code": "secret-code"},
            {"pairing_code": "secret-code", "certificate_fingerprint": FINGERPRINT},
            {"certificate_fingerprint": FINGERPRINT},
        )
        for address in self._addresses():
            for extra in payloads:
                with self.subTest(address=address, extra=sorted(extra)):
                    self.handler._send_json.reset_mock()
                    self.handler._handle_admin_local_peer_pair_by_address({"address": address, **extra})
                    self.assertEqual(self.handler._send_json.call_args.args, (503, {"error": HOLD_MESSAGE}))
        self.assertEqual(self.listener.accepted, 0)
        self.save.assert_not_called()

    def test_held_pairing_tls_probe_never_reaches_the_peer(self) -> None:
        _hold()
        with mock.patch.object(peer_connectivity.subprocess, "run", side_effect=AssertionError("openssl")):
            with self.assertRaises(peer_connectivity.PeerIdentityHold):
                peer_connectivity._pairing_tls_context(f"https://127.0.0.1:{self.listener.port}", FINGERPRINT)
        self.assertEqual(self.listener.accepted, 0)

    def test_held_tailnet_style_probe_never_reaches_the_peer(self) -> None:
        _hold()
        for target in (f"127.0.0.1:{self.listener.port}", f"http://127.0.0.1:{self.listener.port}"):
            with self.subTest(target=target):
                _, info, error = handlers_network.HandlersNetworkMixin._probe_tailnet_device({"tailnet_ip": target})
                self.assertIsNone(info)
                self.assertEqual(error, HOLD_MESSAGE)
        self.assertEqual(self.listener.accepted, 0)

    def test_control_the_same_call_connects_once_the_hold_is_lifted(self) -> None:
        port = self.listener.port
        for address in (f"http://127.0.0.1:{port}", f"https://127.0.0.1:{port}"):
            with self.subTest(address=address):
                before = self.listener.accepted
                self.handler._send_json.reset_mock()
                self.handler._handle_admin_local_peer_pair_by_address({"address": address})
                status, payload = self.handler._send_json.call_args.args
                self.assertEqual(status, 502)
                self.assertIn("could not reach a Drone at", payload["error"])
                self.assertGreater(self.listener.accepted, before)


_FLAT_CHILD = r"""
import json
from unittest import mock

from common import identity_provisioning
from transfer import peer_connectivity
from web import handlers_network

assert handlers_network.PeerIdentityHold is peer_connectivity.PeerIdentityHold, "duplicated module"
identity_provisioning._publish(identity_provisioning.State.RECOVERY_REQUIRED, "held", "flat hold", False)
handler = handlers_network.HandlersNetworkMixin()
handler.settings = mock.MagicMock(use_fake_data=False)
handler.settings.device_id = "drone-self"
handler._send_json = mock.Mock()
results = {}
with mock.patch.object(handlers_network._local_network, "is_local_mode", return_value=True), \
        mock.patch.object(peer_connectivity, "urlopen", side_effect=AssertionError("dialed")) as urlopen_mock, \
        mock.patch.object(peer_connectivity.socket, "create_connection", side_effect=AssertionError("connected")):
    for address in ("https://peer.invalid:8443", "http://peer.invalid:8080", "peer.invalid"):
        handler._send_json.reset_mock()
        handler._handle_admin_local_peer_pair_by_address({"address": address, "pairing_code": "x", "certificate_fingerprint": "ab" * 32})
        results[address] = list(handler._send_json.call_args.args)
    try:
        peer_connectivity._pairing_tls_context("https://peer.invalid:8443", "ab" * 32)
        results["probe"] = "no-error"
    except peer_connectivity.PeerIdentityHold as error:
        results["probe"] = str(error)
    results["urlopen_calls"] = urlopen_mock.call_count
print("RESULT:" + json.dumps(results))
"""


class FlatImportModeTests(unittest.TestCase):
    def test_flat_layout_maps_the_hold_to_503_and_dials_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as empty_cwd:
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            env["PYTHONPATH"] = str(APP_DIR)
            completed = subprocess.run(
                [sys.executable, "-c", _FLAT_CHILD],
                cwd=empty_cwd, env=env, capture_output=True, text=True, timeout=120, check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        line = next(l for l in completed.stdout.splitlines() if l.startswith("RESULT:"))
        results = json.loads(line[len("RESULT:"):])
        for address in ("https://peer.invalid:8443", "http://peer.invalid:8080", "peer.invalid"):
            self.assertEqual(results[address], [503, {"error": HOLD_MESSAGE}], address)
        self.assertEqual(results["probe"], HOLD_MESSAGE)
        self.assertEqual(results["urlopen_calls"], 0)


if __name__ == "__main__":
    unittest.main()
