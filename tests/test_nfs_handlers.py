"""Peer mTLS endpoints that negotiate source-side NFS exports."""

import unittest
from unittest import mock

from app.web import handlers_peer


class _Handler(handlers_peer.HandlersPeerMixin):
    def __init__(self, *, authorized: bool = True, peer_id: str = "peer-1") -> None:
        self.settings = mock.Mock()
        self.client_address = ("192.168.0.178", 43123)
        self.authorized = authorized
        self.peer_id = peer_id
        self.responses = []

    def _peer_request_authorized(self) -> bool:
        return self.authorized

    def _peer_requester_device_id(self):
        return self.peer_id

    def _send_json(self, status_code: int, payload: dict) -> None:
        self.responses.append((status_code, payload))


class NfsPeerHandlerTests(unittest.TestCase):
    def test_authorize_requires_peer_mtls_before_touching_exports(self) -> None:
        handler = _Handler(authorized=False)
        with mock.patch.object(handlers_peer._nfs_exports, "authorize_peer") as authorize:
            handler._handle_peer_nfs_authorize({})

        authorize.assert_not_called()
        self.assertEqual(handler.responses, [])

    def test_authorize_binds_mtls_identity_to_observed_client_address(self) -> None:
        handler = _Handler()
        contract = {
            "available": True,
            "protocol": "nfs",
            "versions": ["4.2"],
            "preferred_version": "4.2",
            "port": 2049,
            "export_path": "/",
            "authorized_addresses": ["192.168.0.178"],
            "peer_id": "peer-1",
        }
        with mock.patch.object(handlers_peer._nfs_exports, "authorize_peer", return_value=contract) as authorize:
            handler._handle_peer_nfs_authorize({"ignored_client_address": "8.8.8.8"})

        authorize.assert_called_once_with(handler.settings, "peer-1", "192.168.0.178")
        self.assertEqual(handler.responses, [(200, contract)])

    def test_authorize_reports_source_capability_failure_as_service_unavailable(self) -> None:
        handler = _Handler()
        with mock.patch.object(
            handlers_peer._nfs_exports,
            "authorize_peer",
            side_effect=RuntimeError("NFSv4 server is not active"),
        ):
            handler._handle_peer_nfs_authorize({})

        self.assertEqual(handler.responses, [(503, {"error": "NFSv4 server is not active"})])

    def test_revoke_maps_cleanup_failure_to_server_error(self) -> None:
        handler = _Handler()
        result = {"status": "error", "peer_id": "peer-1", "status_detail": "exportfs failed"}
        with mock.patch.object(handlers_peer._nfs_exports, "revoke_peer", return_value=result):
            handler._handle_peer_nfs_revoke({})

        self.assertEqual(handler.responses, [(500, result)])


if __name__ == "__main__":
    unittest.main()
