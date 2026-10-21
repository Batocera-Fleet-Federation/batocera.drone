"""_handle_admin_leave_swarm: forget every paired peer, one at a time.

Deliberately a loop over the same per-peer path a single Forget click uses
(NFS revoke, peer notify via swarm_membership.publish_forget, cert cache
cleanup) rather than a separate bulk primitive, so it can never drift from
what Forget already does for one peer.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.drone_api import Settings
from app.web import handlers_network


def _settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "leave-swarm-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class LeaveSwarmHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = _settings(Path(self._tmp.name))
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = self.settings
        self.handler._send_json = mock.Mock()

    def _peers(self, *ids):
        return [{"drone_id": peer_id} for peer_id in ids]

    def test_forgets_every_paired_peer_and_reports_the_count(self) -> None:
        with mock.patch.object(handlers_network._local_network, "paired_peers", return_value=self._peers("peer-a", "peer-b")), \
                mock.patch.object(handlers_network._nfs_exports, "revoke_peer") as revoke, \
                mock.patch("app.transfer.swarm_membership.publish_forget", return_value=True) as publish, \
                mock.patch.object(handlers_network, "_local_peer_cert_cache_path", return_value=Path(self._tmp.name) / "nope.crt"):
            self.handler._handle_admin_leave_swarm()
        self.assertEqual(revoke.call_count, 2)
        self.assertEqual(publish.call_count, 2)
        publish.assert_any_call(self.settings, "peer-a")
        publish.assert_any_call(self.settings, "peer-b")
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "left", "peers_forgotten": 2})

    def test_no_paired_peers_is_still_a_200_with_zero_forgotten(self) -> None:
        with mock.patch.object(handlers_network._local_network, "paired_peers", return_value=[]):
            self.handler._handle_admin_leave_swarm()
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "left", "peers_forgotten": 0})

    def test_a_peer_publish_forget_refuses_is_not_counted(self) -> None:
        # publish_forget returning False (e.g. already gone) must not be
        # counted as a successful removal.
        with mock.patch.object(handlers_network._local_network, "paired_peers", return_value=self._peers("peer-a")), \
                mock.patch.object(handlers_network._nfs_exports, "revoke_peer"), \
                mock.patch("app.transfer.swarm_membership.publish_forget", return_value=False), \
                mock.patch.object(handlers_network, "_local_peer_cert_cache_path", return_value=Path(self._tmp.name) / "nope.crt"):
            self.handler._handle_admin_leave_swarm()
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(payload["peers_forgotten"], 0)

    def test_this_drones_own_identity_is_never_touched(self) -> None:
        # No certificate/identity call of any kind -- leaving the swarm is
        # purely forgetting peers, not resetting who this Drone is.
        with mock.patch.object(handlers_network._local_network, "paired_peers", return_value=self._peers("peer-a")), \
                mock.patch.object(handlers_network._nfs_exports, "revoke_peer"), \
                mock.patch("app.transfer.swarm_membership.publish_forget", return_value=True), \
                mock.patch.object(handlers_network, "_local_peer_cert_cache_path", return_value=Path(self._tmp.name) / "nope.crt"), \
                mock.patch.object(handlers_network, "DroneCertificateManager") as cert_manager:
            self.handler._handle_admin_leave_swarm()
        cert_manager.assert_not_called()


if __name__ == "__main__":
    unittest.main()
