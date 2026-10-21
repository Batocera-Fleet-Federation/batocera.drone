"""_activate_local_peer_certificate falls back to the live-TLS-servers registry.

A real HTTP handler always has ``.server`` and keeps using
``load_peer_cert_everywhere(self.server, ...)`` unchanged. The periodic
background tailnet-peer auto-pair worker (tests/test_tailnet_peer_autopair_retry.py)
builds a bare HandlersNetworkMixin with only ``.settings`` set -- no in-flight
HTTP request, so no ``.server`` -- and needs the same cert to be trusted on
every live listener anyway, so it falls back to ``activate_peer_certificate``,
the same registry-based helper swarm-gossip cert activation already uses.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.web import handlers_network
from app.web.handlers_network import HandlersNetworkMixin


class ActivateLocalPeerCertificateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cert_path = Path(self._tmp.name) / "peer.crt"
        self.cert_path.write_text("not a real cert, just needs to exist")

    def test_with_a_server_uses_load_peer_cert_everywhere(self):
        handler = HandlersNetworkMixin()
        handler.server = mock.Mock()
        with mock.patch.object(handlers_network, "load_peer_cert_everywhere") as load_everywhere, \
                mock.patch.object(handlers_network, "activate_peer_certificate") as registry_fallback:
            handler._activate_local_peer_certificate({"certificate_path": str(self.cert_path)})
        load_everywhere.assert_called_once_with(handler.server, self.cert_path)
        registry_fallback.assert_not_called()

    def test_without_a_server_falls_back_to_the_registry(self):
        handler = HandlersNetworkMixin()  # no .server -- background-worker shape
        with mock.patch.object(handlers_network, "load_peer_cert_everywhere") as load_everywhere, \
                mock.patch.object(handlers_network, "activate_peer_certificate") as registry_fallback:
            handler._activate_local_peer_certificate({"certificate_path": str(self.cert_path)})
        registry_fallback.assert_called_once_with(self.cert_path)
        load_everywhere.assert_not_called()

    def test_missing_certificate_path_does_nothing(self):
        handler = HandlersNetworkMixin()
        with mock.patch.object(handlers_network, "load_peer_cert_everywhere") as load_everywhere, \
                mock.patch.object(handlers_network, "activate_peer_certificate") as registry_fallback:
            handler._activate_local_peer_certificate({})
        load_everywhere.assert_not_called()
        registry_fallback.assert_not_called()

    def test_nonexistent_certificate_file_does_nothing(self):
        handler = HandlersNetworkMixin()
        with mock.patch.object(handlers_network, "load_peer_cert_everywhere") as load_everywhere, \
                mock.patch.object(handlers_network, "activate_peer_certificate") as registry_fallback:
            handler._activate_local_peer_certificate({"certificate_path": str(Path(self._tmp.name) / "missing.crt")})
        load_everywhere.assert_not_called()
        registry_fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
