"""Security boundaries when a Drone identity is replaced on a live machine."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common.auth import SessionStore
from app.storage.state_store import create_session, database_path, get_session, load_payload, save_payload
from app.transfer import swarm_reset


class ReprovisioningBoundaries(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="drone-security-160-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = SimpleNamespace(userdata_root=self.root)

    def test_live_nfs_export_is_revoked_before_ownership_record_is_deleted(self):
        """A paired client's kernel export must not outlive its pairing record."""
        db = database_path(self.root)
        address = "192.168.44.10"
        save_payload(db, "nfs_export_manager.json", {"peers": {"old-peer": {"addresses": [address], "status": "active"}}})
        observed = []

        def unexport(_settings, client_address):
            observed.append((client_address, load_payload(db, "nfs_export_manager.json", {})))
            return None

        with mock.patch("app.device.nfs_export_manager._unexport_client", side_effect=unexport), \
                mock.patch("app.device.nfs_export_manager._cleanup_bind_mounts"), \
                mock.patch("app.transfer.swarm_reset._revoke_shared_credentials", return_value=[]):
            swarm_reset.reset_swarm_identity_state(self.settings)

        self.assertEqual([item[0] for item in observed], [address])
        self.assertIn("old-peer", observed[0][1]["peers"])
        self.assertIsNone(load_payload(db, "nfs_export_manager.json", None))

    def test_sessions_copied_from_source_are_invalid_after_clone_reset(self):
        """A browser session minted for the source must not authorize the new Drone."""
        db = database_path(self.root)
        token = "source-device-session-token"
        expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        create_session(db, token, "admin", expiry)
        self.assertIsNotNone(get_session(db, token))
        self.assertEqual(SessionStore(db).validate(token), "admin")

        with mock.patch("app.transfer.swarm_reset._revoke_shared_credentials", return_value=[]):
            swarm_reset.reset_swarm_identity_state(self.settings)

        self.assertIsNone(SessionStore(db).validate(token))


if __name__ == "__main__":
    unittest.main()
