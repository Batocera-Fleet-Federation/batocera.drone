"""Security regression for credentials copied into a disk image clone."""

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import mcp_auth
from app.common.auth import SessionStore
from app.storage.state_store import database_path, load_payload, open_database, save_payload
from app.transfer import swarm_reset


class CloneAuthRevocationTests(unittest.TestCase):
    def test_reset_revokes_copied_session_and_mcp_token_only_on_clone(self):
        with tempfile.TemporaryDirectory(prefix="issue-163-security-") as temp:
            root = Path(temp)
            source_root = root / "source"
            clone_root = root / "clone"
            source_root.mkdir()
            source = SimpleNamespace(userdata_root=source_root)
            clone = SimpleNamespace(userdata_root=clone_root)
            source_db = database_path(source_root)
            source_sessions = SessionStore(source_db)
            session_token = source_sessions.create("admin")
            mcp_token = mcp_auth.generate_token(source)["token"]
            save_payload(source_db, "mcp", {"token_sha256": "stale-extra"}, state_key="extra")

            # A disk image duplicates the SQLite database, but not subsequent writes.
            clone_root.mkdir()
            with open_database(source_db) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            shutil.copytree(source_root, clone_root, dirs_exist_ok=True)

            clone_db = database_path(clone_root)
            clone_sessions = SessionStore(clone_db)
            self.assertEqual(clone_sessions.validate(session_token), "admin")
            self.assertTrue(mcp_auth.verify_token(clone, mcp_token))
            with mock.patch("app.device.nfs_export_manager.revoke_exports_for_identity_change"), \
                    mock.patch("app.transfer.swarm_reset._revoke_shared_credentials", return_value=[]):
                swarm_reset.reset_swarm_identity_state(clone)

            self.assertIsNone(clone_sessions.validate(session_token))
            self.assertFalse(mcp_auth.verify_token(clone, mcp_token))
            self.assertEqual(load_payload(clone_db, "mcp", "missing"), "missing")
            self.assertEqual(source_sessions.validate(session_token), "admin")
            self.assertTrue(mcp_auth.verify_token(source, mcp_token))


if __name__ == "__main__":
    unittest.main()
