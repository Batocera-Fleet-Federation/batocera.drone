"""Regression tests for startup TLS leaf reconciliation."""

import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer.drone_tls import DroneCertificateManager, certificate_alt_names
from app.web.server_tls import _resolve_tls_material


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class ServerTlsTests(unittest.TestCase):
    def _settings(self, root: Path) -> Settings:
        with mock.patch.dict(
            "os.environ",
            {
                "USERDATA_ROOT": str(root),
                "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
                "DRONE_DEVICE_ID": "58:47:ca:7e:38:57",
            },
            clear=True,
        ):
            return Settings.from_env()

    def test_alt_names_include_mdns_hostname_and_current_addresses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            with mock.patch("app.transfer.drone_tls.socket.gethostname", return_value="batocera"), \
                    mock.patch("app.transfer.drone_tls.socket.getfqdn", return_value="batocera.local"), \
                    mock.patch(
                        "app.transfer.drone_tls._get_local_certificate_ips",
                        return_value=["127.0.0.1", "192.168.0.206", "100.64.0.8"],
                    ):
                names = certificate_alt_names(settings)

            self.assertIn("DNS:batocera.local", names)
            self.assertIn("IP:192.168.0.206", names)
            self.assertIn("IP:100.64.0.8", names)

    def test_startup_repairs_leaf_without_rotating_swarm_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            identity_manager = DroneCertificateManager(settings)
            with mock.patch("app.transfer.drone_tls.socket.gethostname", return_value="batocera"), \
                    mock.patch("app.transfer.drone_tls.socket.getfqdn", return_value="batocera.local"), \
                    mock.patch(
                        "app.transfer.drone_tls._get_local_certificate_ips",
                        return_value=["127.0.0.1", "192.168.0.206"],
                    ):
                identity = identity_manager.ensure_certificate()
                identity_bytes = settings.drone_cert_file.read_bytes()
                identity_fingerprint = identity["fingerprint"]

                cert_file, key_file = _resolve_tls_material(settings)
                first_leaf = cert_file.read_bytes()
                first_key = key_file.read_bytes()
                # A second startup with unchanged names reuses the valid leaf.
                self.assertEqual((cert_file, key_file), _resolve_tls_material(settings))
                self.assertEqual(first_leaf, cert_file.read_bytes())
                self.assertEqual(first_key, key_file.read_bytes())

                # A damaged/mismatched server key is repaired at startup too.
                key_file.write_text("not a private key\n", encoding="utf-8")
                _resolve_tls_material(settings)
                self.assertNotEqual(first_leaf, cert_file.read_bytes())
                first_leaf = cert_file.read_bytes()

            decoded = ssl._ssl._test_decode_cert(str(cert_file))  # type: ignore[attr-defined]
            sans = set(decoded.get("subjectAltName", ()))
            self.assertIn(("DNS", "batocera.local"), sans)
            self.assertIn(("IP Address", "192.168.0.206"), sans)
            subprocess.run(
                ["openssl", "verify", "-CAfile", str(settings.drone_cert_file), str(cert_file)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._assert_hostname_and_mutual_tls(
                "batocera.local",
                cert_file,
                key_file,
                settings.drone_cert_file,
                settings.drone_key_file,
            )

            # DHCP/Tailscale address changes replace only the server leaf. The
            # certificate peers pin and the private swarm identity stay stable.
            with mock.patch("app.transfer.drone_tls.socket.gethostname", return_value="batocera"), \
                    mock.patch("app.transfer.drone_tls.socket.getfqdn", return_value="batocera.local"), \
                    mock.patch(
                        "app.transfer.drone_tls._get_local_certificate_ips",
                        return_value=["127.0.0.1", "192.168.0.207", "100.64.0.9"],
                    ):
                _resolve_tls_material(settings)

            self.assertNotEqual(first_leaf, cert_file.read_bytes())
            self.assertEqual(identity_bytes, settings.drone_cert_file.read_bytes())
            self.assertEqual(identity_fingerprint, identity_manager.metadata()["fingerprint"])

    def _assert_hostname_and_mutual_tls(
        self,
        hostname: str,
        server_cert: Path,
        server_key: Path,
        identity_cert: Path,
        identity_key: Path,
    ) -> None:
        """Exercise the same server-leaf/client-identity handshake peers use."""
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(server_cert), str(server_key))
        server_context.load_verify_locations(cafile=str(identity_cert))
        server_context.verify_mode = ssl.CERT_REQUIRED

        client_context = ssl.create_default_context(cafile=str(identity_cert))
        client_context.load_cert_chain(str(identity_cert), str(identity_key))

        errors = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)

            def serve() -> None:
                try:
                    raw, _ = listener.accept()
                    with raw, server_context.wrap_socket(raw, server_side=True) as connection:
                        if connection.recv(1) != b"x":
                            raise RuntimeError("unexpected TLS test payload")
                        connection.sendall(b"y")
                except BaseException as error:  # surfaced on the test thread below
                    errors.append(error)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            with socket.create_connection(listener.getsockname(), timeout=3) as raw:
                with client_context.wrap_socket(raw, server_hostname=hostname) as connection:
                    connection.sendall(b"x")
                    self.assertEqual(b"y", connection.recv(1))
            thread.join(timeout=3)

        self.assertFalse(thread.is_alive(), "TLS test server did not finish")
        if errors:
            raise errors[0]


if __name__ == "__main__":
    unittest.main()
