"""Issue #163: cloned disks must drop inherited admin sessions and MCP tokens.

Derived from the issue, not from the current implementation:

* A disk image is a byte copy of userdata, including SQLite. After clone
  provisioning the copy is a new Drone.
* Configured login credentials stay, so an administrator can sign in again.
* Browser session cookies and the MCP bearer token are live admin credentials
  of the source installation. They must not authorize the clone.
* The source Drone is untouched: its sessions and MCP token keep working.
* Same-hardware reboot, insufficient/contradictory evidence, and an authorized
  motherboard swap keep this identity, so they must not force a logout.
* Administrator ``new`` identity replacement uses the same destructive cleanup
  as clone provisioning, so inherited sessions and MCP tokens are invalid there
  too.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common import mcp_auth
from app.common.auth import DroneCredentialStore, SessionStore
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.identity_provisioning import State
from app.common.settings import Settings
from app.storage.state_store import (
    create_session,
    database_path,
    load_payload,
    open_database,
    save_payload,
)
from app.transfer import swarm_reset
from app.transfer.drone_tls import DroneCertificateManager
from tests.test_hardware_identity import UUID_A, UUID_B, write, x86_tree


OPENSSL_AVAILABLE = shutil.which("openssl") is not None
STORED_USERNAME = "arcade-admin"
STORED_PASSWORD = "stored-secret-99"
GAMEPLAY_KEY = "issue-163-gameplay-keep"


def settings_for(root: Path, **extra: str) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
        **extra,
    }
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings.from_env()


def provider_for(root: Path) -> HardwareIdentityProvider:
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def image_disk(source: Path, target: Path) -> None:
    with open_database(database_path(source)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copytree(source, target, symlinks=True)


def iso_from_now(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def seed_gameplay_history(db_path: Path) -> None:
    now = iso_from_now(0)
    with open_database(db_path) as connection:
        connection.execute(
            "INSERT INTO gameplay_history "
            "(session_key, played_at, system_name, game_name, rom_path, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                GAMEPLAY_KEY,
                now,
                "snes",
                "Kept Game",
                "/roms/snes/kept.sfc",
                json.dumps({"session_key": GAMEPLAY_KEY}),
                now,
            ),
        )


def gameplay_present(db_path: Path) -> bool:
    with open_database(db_path) as connection:
        row = connection.execute(
            "SELECT session_key FROM gameplay_history WHERE session_key = ?",
            (GAMEPLAY_KEY,),
        ).fetchone()
    return row is not None


def mcp_namespace_keys(db_path: Path) -> list[str]:
    with open_database(db_path) as connection:
        rows = connection.execute(
            "SELECT state_key FROM app_state WHERE namespace = ? ORDER BY state_key",
            ("mcp",),
        ).fetchall()
    return [row[0] for row in rows]


def session_count(db_path: Path) -> int:
    with open_database(db_path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()
    return int(row[0])


@unittest.skipUnless(OPENSSL_AVAILABLE, "openssl is required for identity provisioning")
class CloneSessionMcpBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-163-auth-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hw_a = x86_tree(self.tmp / "hw-a")
        self.hw_b = x86_tree(self.tmp / "hw-b", uuid=UUID_B, board="BOARDSERIAL02")
        self.root = self.tmp / "disk-a"
        self.root.mkdir()

    def install(self) -> Settings:
        settings = settings_for(self.root)
        result = identity.ensure_identity(settings, provider_for(self.hw_a))
        self.assertEqual(result.state, State.READY)
        return result.settings

    def store_login(self, settings: Settings) -> DroneCredentialStore:
        store = DroneCredentialStore(
            settings.credentials_file,
            env_username="env-user",
            env_password="env-password-fallback",
            state_database_file=database_path(settings.userdata_root),
        )
        store.update(STORED_USERNAME, STORED_PASSWORD)
        return store

    def seed_admin_secrets(self, settings: Settings) -> dict:
        db = database_path(settings.userdata_root)
        sessions = SessionStore(db)
        admin = sessions.create("admin")
        operator = sessions.create("operator")
        expired = "expired-source-session"
        create_session(db, expired, "admin", iso_from_now(-120))
        mcp = mcp_auth.generate_token(settings, "source laptop")
        save_payload(db, "mcp", load_payload(db, "mcp", {}), state_key="shadow")
        seed_gameplay_history(db)
        credentials = self.store_login(settings)
        self.assertEqual(sessions.validate(admin), "admin")
        self.assertEqual(sessions.validate(operator), "operator")
        self.assertTrue(mcp_auth.verify_token(settings, mcp["token"]))
        return {
            "admin": admin,
            "operator": operator,
            "expired": expired,
            "mcp": mcp["token"],
            "credentials": credentials,
        }

    def clone_boot(self, hardware: Path, name: str = "disk-b"):
        target = self.tmp / name
        image_disk(self.root, target)
        identity.reset_runtime_state_for_tests()
        settings = settings_for(target)
        return settings, identity.ensure_identity(settings, provider_for(hardware)), target

    def test_clone_rejects_copied_sessions_and_mcp_token_but_keeps_login(self) -> None:
        original = self.install()
        secrets = self.seed_admin_secrets(original)
        original_db = database_path(original.userdata_root)

        clone, result, _target = self.clone_boot(self.hw_b)
        self.assertEqual(result.status, identity.STATUS_CLONE)
        clone_db = database_path(clone.userdata_root)

        clone_sessions = SessionStore(clone_db)
        self.assertIsNone(clone_sessions.validate(secrets["admin"]))
        self.assertIsNone(clone_sessions.validate(secrets["operator"]))
        self.assertIsNone(clone_sessions.validate(secrets["expired"]))
        self.assertEqual(session_count(clone_db), 0)
        self.assertFalse(mcp_auth.verify_token(clone, secrets["mcp"]))
        self.assertFalse(mcp_auth.verify_token(clone, "  " + secrets["mcp"] + "  "))
        self.assertFalse(mcp_auth.verify_token(clone, secrets["mcp"][:-1]))
        self.assertFalse(mcp_auth.verify_token(clone, secrets["mcp"] + "x"))
        self.assertFalse(mcp_auth.verify_token(clone, None))
        self.assertFalse(mcp_auth.verify_token(clone, ""))
        self.assertFalse(mcp_auth.token_status(clone)["configured"])
        self.assertEqual(mcp_namespace_keys(clone_db), [])

        clone_login = DroneCredentialStore(
            clone.credentials_file,
            env_username="env-user",
            env_password="env-password-fallback",
            state_database_file=clone_db,
        )
        self.assertTrue(clone_login.check(STORED_USERNAME, STORED_PASSWORD))
        self.assertFalse(clone_login.check("env-user", "env-password-fallback"))
        self.assertEqual(load_payload(clone_db, "credentials", {}).get("username"), STORED_USERNAME)
        self.assertTrue(gameplay_present(clone_db))

        self.assertEqual(SessionStore(original_db).validate(secrets["admin"]), "admin")
        self.assertEqual(SessionStore(original_db).validate(secrets["operator"]), "operator")
        self.assertTrue(mcp_auth.verify_token(original, secrets["mcp"]))
        self.assertTrue(secrets["credentials"].check(STORED_USERNAME, STORED_PASSWORD))

        fresh_session = clone_sessions.create(STORED_USERNAME)
        self.assertEqual(clone_sessions.validate(fresh_session), STORED_USERNAME)
        replacement = mcp_auth.generate_token(clone, "clone laptop")["token"]
        self.assertTrue(mcp_auth.verify_token(clone, replacement))
        self.assertFalse(mcp_auth.verify_token(clone, secrets["mcp"]))
        self.assertFalse(mcp_auth.verify_token(original, replacement))

    def test_normal_reboot_and_held_evidence_keep_admin_secrets(self) -> None:
        original = self.install()
        secrets = self.seed_admin_secrets(original)
        db = database_path(original.userdata_root)

        identity.reset_runtime_state_for_tests()
        reboot = identity.ensure_identity(settings_for(self.root), provider_for(self.hw_a))
        self.assertEqual(reboot.status, identity.STATUS_VERIFIED)
        self.assertEqual(SessionStore(db).validate(secrets["admin"]), "admin")
        self.assertTrue(mcp_auth.verify_token(original, secrets["mcp"]))

        blind = self.tmp / "hw-blind"
        blind.mkdir()
        identity.reset_runtime_state_for_tests()
        held = identity.ensure_identity(settings_for(self.root), provider_for(blind))
        self.assertEqual(held.state, State.RECOVERY_REQUIRED)
        self.assertEqual(held.reason, "insufficient_evidence")
        self.assertEqual(SessionStore(db).validate(secrets["admin"]), "admin")
        self.assertTrue(mcp_auth.verify_token(original, secrets["mcp"]))

        odd = x86_tree(self.tmp / "hw-odd", uuid=UUID_A, board="REPLACEDBOARD")
        identity.reset_runtime_state_for_tests()
        contradictory = identity.ensure_identity(settings_for(self.root), provider_for(odd))
        self.assertEqual(contradictory.reason, "contradictory_evidence")
        self.assertEqual(SessionStore(db).validate(secrets["operator"]), "operator")
        self.assertTrue(mcp_auth.verify_token(original, secrets["mcp"]))
        self.assertTrue(gameplay_present(db))

    def test_authorized_hardware_change_keeps_sessions_and_mcp_token(self) -> None:
        original = self.install()
        secrets = self.seed_admin_secrets(original)
        identity.authorize_hardware_change(original, hours=24, actor="admin")

        identity.reset_runtime_state_for_tests()
        rebound = identity.ensure_identity(settings_for(self.root), provider_for(self.hw_b))
        self.assertEqual(rebound.state, State.READY)
        self.assertNotEqual(rebound.status, identity.STATUS_CLONE)
        db = database_path(original.userdata_root)
        self.assertEqual(SessionStore(db).validate(secrets["admin"]), "admin")
        self.assertTrue(mcp_auth.verify_token(original, secrets["mcp"]))
        self.assertTrue(DroneCredentialStore(
            original.credentials_file,
            state_database_file=db,
        ).check(STORED_USERNAME, STORED_PASSWORD))

    def test_operator_new_identity_invalidates_sessions_and_mcp_on_that_machine(self) -> None:
        original = self.install()
        secrets = self.seed_admin_secrets(original)
        identity.reset_runtime_state_for_tests()
        outcome = identity.resolve_recovery(settings_for(self.root), "new", provider_for(self.hw_a), actor="admin")
        self.assertEqual(outcome["action"], "new")
        db = database_path(original.userdata_root)
        self.assertIsNone(SessionStore(db).validate(secrets["admin"]))
        self.assertFalse(mcp_auth.verify_token(original, secrets["mcp"]))
        self.assertFalse(mcp_auth.token_status(original)["configured"])
        self.assertTrue(DroneCredentialStore(
            original.credentials_file,
            state_database_file=db,
        ).check(STORED_USERNAME, STORED_PASSWORD))
        self.assertTrue(gameplay_present(db))

    def test_empty_and_malformed_mcp_state_does_not_block_clone(self) -> None:
        original = self.install()
        db = database_path(original.userdata_root)
        save_payload(db, "mcp", ["not", "a", "record"])
        save_payload(db, "mcp", {"token_sha256": "0" * 64, "hint": "dmcp_xxxx"}, state_key="alt")
        create_session(db, "   ", "admin", iso_from_now(3600))

        clone, result, _target = self.clone_boot(self.hw_b)
        self.assertEqual(result.status, identity.STATUS_CLONE)
        clone_db = database_path(clone.userdata_root)
        self.assertFalse(mcp_auth.verify_token(clone, "dmcp_" + "a" * 43))
        self.assertFalse(mcp_auth.token_status(clone)["configured"])
        self.assertEqual(mcp_namespace_keys(clone_db), [])
        self.assertEqual(session_count(clone_db), 0)

    def test_clone_with_no_sessions_or_mcp_token_still_provisions(self) -> None:
        self.install()
        clone, result, _target = self.clone_boot(self.hw_b)
        self.assertEqual(result.status, identity.STATUS_CLONE)
        self.assertFalse(mcp_auth.token_status(clone)["configured"])
        self.assertEqual(session_count(database_path(clone.userdata_root)), 0)

    def test_failed_clone_after_swarm_reset_does_not_restore_copied_secrets(self) -> None:
        original = self.install()
        secrets = self.seed_admin_secrets(original)
        target = self.tmp / "disk-fail"
        image_disk(self.root, target)
        identity.reset_runtime_state_for_tests()
        clone = settings_for(target)
        with mock.patch.object(
            DroneCertificateManager,
            "_generate_local_certificate",
            side_effect=RuntimeError("openssl failed"),
        ):
            held = identity.ensure_identity(clone, provider_for(self.hw_b))
        self.assertEqual(held.state, State.RECOVERY_REQUIRED)
        clone_db = database_path(clone.userdata_root)
        self.assertIsNone(SessionStore(clone_db).validate(secrets["admin"]))
        self.assertFalse(mcp_auth.verify_token(clone, secrets["mcp"]))
        self.assertEqual(SessionStore(database_path(original.userdata_root)).validate(secrets["admin"]), "admin")
        self.assertTrue(mcp_auth.verify_token(original, secrets["mcp"]))

        identity.reset_runtime_state_for_tests()
        resumed = identity.ensure_identity(settings_for(target), provider_for(self.hw_b))
        self.assertEqual(resumed.status, identity.STATUS_CLONE)
        self.assertIsNone(SessionStore(clone_db).validate(secrets["admin"]))
        self.assertFalse(mcp_auth.verify_token(clone, secrets["mcp"]))

    def test_session_wipe_failure_rolls_back_mcp_namespace(self) -> None:
        original = self.install()
        secrets = self.seed_admin_secrets(original)
        target = self.tmp / "disk-atomic"
        image_disk(self.root, target)
        clone = settings_for(target)
        real_open = swarm_reset.open_database

        def wrapped(path):
            connection = real_open(path)

            class ConnectionProxy:
                def execute(self, sql, *args, **kwargs):
                    if isinstance(sql, str) and "DELETE FROM sessions" in sql:
                        raise sqlite3.OperationalError("simulated sessions wipe failure")
                    return connection.execute(sql, *args, **kwargs)

                def __enter__(self):
                    connection.__enter__()
                    return self

                def __exit__(self, *exc):
                    return connection.__exit__(*exc)

                def __getattr__(self, name):
                    return getattr(connection, name)

            return ConnectionProxy()

        with mock.patch.object(swarm_reset, "open_database", side_effect=wrapped), mock.patch.object(
            swarm_reset, "_revoke_shared_credentials", return_value=[]
        ), mock.patch("app.device.nfs_export_manager.revoke_exports_for_identity_change"):
            with self.assertRaises(sqlite3.OperationalError):
                swarm_reset.reset_swarm_identity_state(clone)

        clone_db = database_path(clone.userdata_root)
        self.assertEqual(SessionStore(clone_db).validate(secrets["admin"]), "admin")
        self.assertTrue(mcp_auth.verify_token(clone, secrets["mcp"]))
        self.assertTrue(mcp_auth.token_status(clone)["configured"])


if __name__ == "__main__":
    unittest.main()
