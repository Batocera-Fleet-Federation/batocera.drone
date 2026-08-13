"""ClientConfig.from_env()'s cert-file resolution.

This exact logic was wrong for months without a single test catching it:
an earlier version only ever checked the self-signed server.crt fallback
path, which no real device actually uses in practice (DroneCertificateManager
provisions drone_cert_file at startup, and the server's own listener-cert
selection in drone_api.py picks that whenever it exists, before ever
falling back to a self-signed cert). The bug was invisible in local dev
against the mock server (which runs HTTP_ONLY, so ca_cert_file is never
even read) and only surfaced live against a real HTTPS device -- see the
drone-live-debugging skill and ports-client/README.md's "Vendoring" note
for how it was found. These tests exist so that never happens silently
again: they exercise from_env()'s actual priority order, not just
construct a ClientConfig directly with an already-correct path.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from client.config import ClientConfig


class ClientConfigCertResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _env(self, **overrides):
        env = {"USERDATA_ROOT": str(self.root)}
        env.update(overrides)
        return env

    def test_prefers_the_device_identity_drone_cert_when_it_exists(self):
        # Mirrors drone_api.py's own listener setup: `if
        # settings.drone_cert_file.exists(): use it` wins over everything
        # else -- the common case, since DroneCertificateManager provisions
        # this cert at every startup. No TLS_CERT_FILE/DRONE_CERT_FILE
        # override here -- this is the plain-default, no-config-needed path.
        drone_cert = self.root / "system" / "drone-app" / "certs" / "drone.crt"
        drone_cert.parent.mkdir(parents=True)
        drone_cert.write_text("drone cert")

        with mock.patch.dict("os.environ", self._env(), clear=True):
            config = ClientConfig.from_env()
        self.assertEqual(config.ca_cert_file, drone_cert)

    def test_drone_cert_file_env_override_is_honored(self):
        custom_drone_cert = self.root / "custom-drone.crt"
        custom_drone_cert.write_text("custom drone cert")

        with mock.patch.dict("os.environ", self._env(DRONE_CERT_FILE=str(custom_drone_cert)), clear=True):
            config = ClientConfig.from_env()
        self.assertEqual(config.ca_cert_file, custom_drone_cert)

    def test_tls_cert_file_alone_also_feeds_the_drone_cert_default(self):
        # settings.py derives drone_cert_file's own default from
        # DRONE_CERT_FILE, falling back to TLS_CERT_FILE, falling back to
        # the real default -- so TLS_CERT_FILE alone (no DRONE_CERT_FILE)
        # is *also* what drone_cert_file resolves to, not a separate value.
        # This mirrors that exactly rather than treating TLS_CERT_FILE as
        # only relevant to the self-signed fallback.
        cert = self.root / "provided-server.crt"
        cert.write_text("provided cert")

        with mock.patch.dict("os.environ", self._env(TLS_CERT_FILE=str(cert)), clear=True):
            config = ClientConfig.from_env()
        self.assertEqual(config.ca_cert_file, cert)

    def test_falls_back_to_explicit_tls_cert_file_when_drone_cert_override_is_missing(self):
        # The one scenario where DRONE_CERT_FILE and TLS_CERT_FILE actually
        # diverge: DRONE_CERT_FILE explicitly overridden to something that
        # doesn't exist, while TLS_CERT_FILE separately points at a real
        # file.
        missing_drone_cert = self.root / "does-not-exist" / "drone.crt"
        explicit_cert = self.root / "explicit-server.crt"
        explicit_cert.write_text("explicit cert")

        with mock.patch.dict(
            "os.environ",
            self._env(DRONE_CERT_FILE=str(missing_drone_cert), TLS_CERT_FILE=str(explicit_cert)),
            clear=True,
        ):
            config = ClientConfig.from_env()
        self.assertEqual(config.ca_cert_file, explicit_cert)

    def test_falls_back_to_self_signed_server_crt_when_nothing_else_exists(self):
        # This is the path a device that has *never* successfully started
        # Drone would hit -- the least common real-world case, not the
        # default one, even though an earlier version of this code treated
        # it as the only case. Note: settings.py's own tls_self_signed_dir
        # default is a *literal* /userdata/system/certs, not derived from
        # USERDATA_ROOT -- mirrored here bug-for-bug on purpose, since the
        # whole point is matching what the real server actually does.
        with mock.patch.dict("os.environ", self._env(), clear=True):
            config = ClientConfig.from_env()
        self.assertEqual(config.ca_cert_file, Path("/userdata/system/certs/server.crt"))

    def test_self_signed_dir_env_override_is_honored_in_the_fallback_path(self):
        custom_dir = self.root / "custom-certs-dir"
        with mock.patch.dict("os.environ", self._env(TLS_SELF_SIGNED_DIR=str(custom_dir)), clear=True):
            config = ClientConfig.from_env()
        self.assertEqual(config.ca_cert_file, custom_dir / "server.crt")

    def test_other_fields_resolve_independently_of_cert_logic(self):
        with mock.patch.dict(
            "os.environ",
            self._env(HTTPS_PORT="8443", HTTP_ONLY="1", DRONE_PORTS_CLIENT_HOST="10.0.0.5"),
            clear=True,
        ):
            config = ClientConfig.from_env()
        self.assertEqual(config.https_port, 8443)
        self.assertTrue(config.http_only)
        self.assertEqual(config.host, "10.0.0.5")
        self.assertEqual(
            config.session_cookie_path, self.root / "system" / "drone-app" / "ports-client-session.json"
        )


if __name__ == "__main__":
    unittest.main()
