"""Issue #173: a failed clone reset cannot leave copied admin access usable.

The source installation and a same-hardware failure retain their own access.
Fixtures image userdata after checkpointing SQLite; no listener or device is used.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity, mcp_auth
from app.common.auth import DroneCredentialStore, SessionStore
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.storage.state_store import database_path, load_payload, open_database, save_payload
from app.transfer import swarm_reset
from tests.test_hardware_identity import UUID_B, x86_tree


def settings_for(root):
    with mock.patch.dict(os.environ, {
        "USERDATA_ROOT": str(root),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
    }, clear=True):
        return Settings.from_env()


def provider_for(root):
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


@unittest.skipUnless(shutil.which("openssl"), "openssl is required for identity provisioning")
class HeldCloneAuthAcceptance(unittest.TestCase):
    def setUp(self):
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        tmp = tempfile.TemporaryDirectory(prefix="issue-173-held-clone-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.hardware_a = x86_tree(self.root / "hardware-a")
        self.hardware_b = x86_tree(self.root / "hardware-b", uuid=UUID_B, board="OTHERBOARD99")
        self.source = settings_for(self.root / "source")
        installed = identity.ensure_identity(self.source, provider_for(self.hardware_a))
        self.assertEqual(installed.state, identity.State.READY)
        self.source = installed.settings
        source_db = database_path(self.source.userdata_root)
        self.cookie = SessionStore(source_db).create("arcade-admin")
        self.bearer = mcp_auth.generate_token(self.source, "source client")["token"]
        self.login = DroneCredentialStore(
            self.source.credentials_file,
            state_database_file=source_db,
        )
        self.login.update("arcade-admin", "configured-secret")
        save_payload(source_db, "local_paired_peers", {"source-peer": {"paired": True}})
        save_payload(source_db, "automation_config.json", {"enabled": True})

    def clone(self):
        source_db = database_path(self.source.userdata_root)
        with open_database(source_db) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target = self.root / "clone"
        shutil.copytree(self.source.userdata_root, target, symlinks=True)
        return settings_for(target)

    def assert_source_still_authorized(self):
        db = database_path(self.source.userdata_root)
        self.assertEqual(SessionStore(db).validate(self.cookie), "arcade-admin")
        self.assertTrue(mcp_auth.verify_token(self.source, self.bearer))

    def test_reset_fails_before_sqlite_cleanup_then_retry(self):
        clone = self.clone()
        clone_db = database_path(clone.userdata_root)
        self.assertEqual(SessionStore(clone_db).validate(self.cookie), "arcade-admin")
        self.assertTrue(mcp_auth.verify_token(clone, self.bearer))

        with mock.patch.object(swarm_reset, "reset_swarm_identity_state", side_effect=OSError("reset unavailable")):
            held = identity.ensure_identity(clone, provider_for(self.hardware_b))

        self.assertEqual(held.state, identity.State.RECOVERY_REQUIRED)
        self.assertEqual(held.reason, "provisioning_failed")
        self.assertFalse(held.swarm_networking_enabled)
        self.assertIsNone(SessionStore(clone_db).validate(self.cookie))
        self.assertFalse(mcp_auth.verify_token(clone, self.bearer))
        self.assertFalse(mcp_auth.token_status(clone)["configured"])
        with open_database(clone_db) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM app_state WHERE namespace='mcp'").fetchone()[0], 0)
        # The focused failure cleanup must leave credentials and other local state intact.
        self.assertTrue(DroneCredentialStore(clone.credentials_file, state_database_file=clone_db).check(
            "arcade-admin", "configured-secret"
        ))
        self.assertEqual(load_payload(clone_db, "local_paired_peers", {}), {"source-peer": {"paired": True}})
        self.assertEqual(load_payload(clone_db, "automation_config.json", {}), {"enabled": True})
        self.assert_source_still_authorized()

        identity.reset_runtime_state_for_tests()
        retried = identity.ensure_identity(settings_for(clone.userdata_root), provider_for(self.hardware_b))
        self.assertEqual(retried.state, identity.State.READY)
        self.assertIsNone(SessionStore(clone_db).validate(self.cookie))
        self.assertFalse(mcp_auth.verify_token(clone, self.bearer))
        self.assert_source_still_authorized()

    def test_original_with_pre_verdict_error_keeps_its_own_access(self):
        # A transient validation failure on the source is a hold, not evidence of a clone.
        with mock.patch.object(identity, "_ensure_locked", side_effect=OSError("hardware read failed")):
            held = identity.ensure_identity(settings_for(self.source.userdata_root), provider_for(self.hardware_a))

        self.assertEqual(held.state, identity.State.RECOVERY_REQUIRED)
        self.assertEqual(held.reason, "provisioning_failed")
        self.assertFalse(held.swarm_networking_enabled)
        self.assert_source_still_authorized()

    def test_failed_revocation_does_not_expose_copied_access(self):
        clone = self.clone()
        clone_db = database_path(clone.userdata_root)
        # Simulate an SQLite cleanup failure that still permits normal auth reads.
        with open_database(clone_db) as connection:
            connection.execute(
                "CREATE TRIGGER block_mcp_delete BEFORE DELETE ON app_state "
                "WHEN OLD.namespace = 'mcp' BEGIN SELECT RAISE(ABORT, 'disk write failed'); END"
            )
            connection.commit()

        with mock.patch.object(swarm_reset, "reset_swarm_identity_state", side_effect=OSError("reset unavailable")):
            held = identity.ensure_identity(clone, provider_for(self.hardware_b))

        self.assertEqual(held.state, identity.State.RECOVERY_REQUIRED)
        self.assertFalse(held.swarm_networking_enabled)
        self.assertIsNone(SessionStore(clone_db).validate(self.cookie))
        self.assertFalse(mcp_auth.verify_token(clone, self.bearer))
        self.assert_source_still_authorized()


if __name__ == "__main__":
    unittest.main()
