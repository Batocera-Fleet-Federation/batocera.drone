"""Issue #173: inherited access is unusable throughout a held clone boot.

A clone's copied admin credentials must not become an escape hatch when the
reset, the targeted revocation, or the provisioning journal cannot be written.
The source disk remains independently authorized.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity, mcp_auth
from app.common.auth import SessionStore
from app.storage.state_store import database_path, open_database
from app.transfer import swarm_reset
from tests.adversarial.test_issue_173_held_clone_auth_uat import provider_for, settings_for
from tests.test_hardware_identity import UUID_B, x86_tree


@unittest.skipUnless(shutil.which("openssl"), "openssl is required for identity provisioning")
class HeldCloneRevocationFailureBoundaries(unittest.TestCase):
    def setUp(self):
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        tmp = tempfile.TemporaryDirectory(prefix="issue-173-revoke-boundary-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.hardware_a = x86_tree(self.root / "hardware-a")
        self.hardware_b = x86_tree(self.root / "hardware-b", uuid=UUID_B, board="OTHERBOARD99")
        installed = identity.ensure_identity(settings_for(self.root / "source"), provider_for(self.hardware_a))
        self.assertEqual(installed.state, identity.State.READY)
        self.source = installed.settings
        self.cookie = SessionStore(database_path(self.source.userdata_root)).create("admin")
        self.bearer = mcp_auth.generate_token(self.source)["token"]

    def clone(self):
        with open_database(database_path(self.source.userdata_root)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        root = self.root / "clone"
        shutil.copytree(self.source.userdata_root, root, symlinks=True)
        return settings_for(root)

    def assert_source_authorized(self):
        self.assertEqual(SessionStore(database_path(self.source.userdata_root)).validate(self.cookie), "admin")
        self.assertTrue(mcp_auth.verify_token(self.source, self.bearer))

    def assert_clone_held_without_copied_access(self, clone, held):
        self.assertEqual(held.state, identity.State.RECOVERY_REQUIRED)
        self.assertEqual(held.reason, "provisioning_failed")
        self.assertFalse(held.swarm_networking_enabled)
        self.assertIsNone(SessionStore(database_path(clone.userdata_root)).validate(self.cookie))
        self.assertFalse(mcp_auth.verify_token(clone, self.bearer))
        self.assert_source_authorized()

    def test_sqlite_refuses_delete_and_update_of_both_copied_secrets(self):
        clone = self.clone()
        db = database_path(clone.userdata_root)
        # The DB remains readable and other writes work; both revocation forms fail.
        with open_database(db) as connection:
            for table, predicate in (("sessions", "1"), ("app_state", "OLD.namespace = 'mcp'")):
                for verb in ("DELETE", "UPDATE"):
                    connection.execute(
                        f"CREATE TRIGGER block_{table}_{verb.lower()} BEFORE {verb} ON {table} "
                        f"WHEN {predicate} BEGIN SELECT RAISE(ABORT, 'revocation unavailable'); END"
                    )
            connection.commit()
        with mock.patch.object(swarm_reset, "reset_swarm_identity_state", side_effect=OSError("reset unavailable")):
            held = identity.ensure_identity(clone, provider_for(self.hardware_b))
        self.assert_clone_held_without_copied_access(clone, held)

    def test_clone_detected_but_journal_write_fails_before_reset(self):
        clone = self.clone()
        with mock.patch.object(identity, "_record_journal", side_effect=OSError("journal unavailable")):
            held = identity.ensure_identity(clone, provider_for(self.hardware_b))
        self.assert_clone_held_without_copied_access(clone, held)


if __name__ == "__main__":
    unittest.main()
