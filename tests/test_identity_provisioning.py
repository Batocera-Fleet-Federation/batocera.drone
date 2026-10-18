"""Clone detection, automatic identity provisioning, recovery, and swarm isolation."""

import collections
import hashlib
import json
import ast
import shutil
import ssl
import subprocess
import types
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common import mcp_auth
from app.common.auth import SessionStore
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.identity_provisioning import State
from app.common.settings import Settings
from app.storage.state_store import (
    append_event,
    database_path,
    load_events,
    load_payload,
    open_database,
    save_payload,
    save_peer_route,
)
from app.transfer import local_network, swarm_membership, swarm_reset
from app.transfer.drone_tls import DroneCertificateManager
from tests.test_hardware_identity import UUID_A, UUID_B, pi_tree, write, x86_tree

REPO_ROOT = Path(__file__).resolve().parent.parent
needs_openssl = unittest.skipUnless(shutil.which("openssl"), "openssl is required")


class HeldIdentityStartupGateTests(unittest.TestCase):
    def test_vpn_auto_connect_and_self_heal_start_only_when_swarm_is_allowed(self) -> None:
        source = (REPO_ROOT / "app" / "drone_api.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        create_server = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "create_server"
        )
        gates = {}
        for node in ast.walk(create_server):
            if not isinstance(node, ast.If):
                continue
            test_text = ast.unparse(node.test)
            if "_VPN_AUTO_CONNECT_ATTEMPTED" in test_text:
                gates["maybe_auto_connect"] = test_text
            if "_VPN_SELF_HEAL_POLLER_STARTED" in test_text:
                gates["run_self_heal_poller"] = test_text
        self.assertIn("swarm_enabled", gates.get("maybe_auto_connect", ""))
        self.assertIn("swarm_enabled", gates.get("run_self_heal_poller", ""))


def settings_for(root: Path, **extra: str) -> Settings:
    env = {"USERDATA_ROOT": str(root), "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"), **extra}
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def provider_for(root: Path, machine: str = "x86_64") -> HardwareIdentityProvider:
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine=machine)


def fingerprint_of(settings: Settings) -> str:
    pem = settings.drone_cert_file.read_text(encoding="utf-8")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def read_binding(settings: Settings) -> dict:
    return json.loads(identity._binding_path(settings).read_text(encoding="utf-8"))


def seed_swarm(settings: Settings, peer_id: str = "11:22:33:44:55:66") -> None:
    db = database_path(settings.userdata_root)
    peer = {"drone_id": peer_id, "device_id": peer_id, "name": "peer", "certificate_fingerprint": "ab" * 32, "paired": True}
    save_payload(db, "local_paired_peers", {peer_id: peer})
    save_payload(db, "local_discovered_peers", {peer_id: peer})
    save_payload(db, "local_pairing_code", {"code": "12345678", "expires_at": "2999-01-01T00:00:00+00:00"})
    save_payload(db, "local_peer_checks", [{"peer": peer_id}])
    save_payload(db, "tailnet_forgotten_peers", [peer_id])
    save_payload(db, "network_share_manager.json", {"peers": {peer_id: {"mounted": True}}})
    save_payload(db, "nfs_export_manager.json", {"peers": {peer_id: {}}})
    save_payload(
        db,
        "swarm_membership",
        {"clock": 4, "records": {peer_id: {"op": "add", "epoch": 2, "origin": peer_id, "card": {}}}, "departed": False},
    )
    save_payload(
        db,
        "download_manager.json",
        {"version": 1, "paused": True, "jobs": [{"job_id": "a", "source_drone_id": peer_id}, {"job_id": "b", "source_drone_id": ""}]},
    )
    append_event(db, "local_sync_activity", {"peer": peer_id})
    save_peer_route(db, peer_id, "10.0.0.9", "ip")
    pins = settings.userdata_root / "system" / "drone-app" / "local-peer-certs"
    pins.mkdir(parents=True, exist_ok=True)
    (pins / "peer.crt").write_text("-----BEGIN CERTIFICATE-----\npinned\n", encoding="utf-8")
    # Unrelated, legitimate configuration that must survive reprovisioning.
    save_payload(db, "automation_config.json", {"wifi_recovery": {"enabled": True}})
    save_payload(db, "smtp_manager.json", {"has_config": True, "host": "smtp.example.test", "recipient_email": "me@example.test"})
    settings.credentials_file.parent.mkdir(parents=True, exist_ok=True)
    settings.credentials_file.write_text(json.dumps({"username": "admin", "password_hash": "x"}), encoding="utf-8")


def image_disk(source: Path, target: Path) -> None:
    """Byte-for-byte disk image of a Drone's userdata (what Clonezilla/dd would produce)."""
    with open_database(database_path(source)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copytree(source, target, symlinks=True)


@needs_openssl
class ProvisioningTestBase(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.tmp = Path(tempfile.mkdtemp(prefix="drone-identity-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.hw_a = x86_tree(self.tmp / "hw-a")
        self.hw_b = x86_tree(self.tmp / "hw-b", uuid=UUID_B, board="BOARDSERIAL02")
        self.root = self.tmp / "disk-a"
        self.root.mkdir()

    def install(self, hardware: Path = None):
        settings = settings_for(self.root)
        result = identity.ensure_identity(settings, provider_for(hardware or self.hw_a))
        return result.settings, result

    def clone_boot(self, hardware: Path, name: str = "disk-b"):
        target = self.tmp / name
        image_disk(self.root, target)
        settings = settings_for(target)
        return settings, identity.ensure_identity(settings, provider_for(hardware))


class FirstInstallAndRebootTests(ProvisioningTestBase):
    def test_first_installation_creates_unique_identity_and_binding(self) -> None:
        settings, result = self.install()
        self.assertEqual(result.state, State.READY)
        self.assertEqual(result.status, identity.STATUS_NEW_INSTALLATION)
        binding = read_binding(settings)
        self.assertEqual(binding["schema_version"], 1)
        self.assertEqual(binding["identity_certificate_fingerprint"], fingerprint_of(settings))
        self.assertEqual(binding["device_id"], settings.device_id)
        self.assertEqual(binding["hardware_fingerprint_version"], 1)
        self.assertEqual(len(binding["hardware_fingerprint"]), 64)
        self.assertEqual(len(binding["installation_id"]), 32)
        self.assertEqual(identity.identity_dir(settings).stat().st_mode & 0o777, 0o700)
        self.assertEqual(identity._binding_path(settings).stat().st_mode & 0o777, 0o600)
        self.assertEqual(settings.drone_key_file.stat().st_mode & 0o777, 0o600)
        self.assertFalse(identity._journal_path(settings).exists())
        self.assertTrue(identity.swarm_networking_allowed())

    def test_binding_never_contains_raw_hardware_values(self) -> None:
        settings, _ = self.install()
        text = identity._binding_path(settings).read_text(encoding="utf-8").lower()
        self.assertNotIn(UUID_A, text)
        self.assertNotIn("bsn12345678", text)

    def test_normal_reboot_changes_nothing(self) -> None:
        settings, _ = self.install()
        seed_swarm(settings)
        before = {
            "cert": settings.drone_cert_file.read_bytes(),
            "key": settings.drone_key_file.read_bytes(),
            "binding": identity._binding_path(settings).read_bytes(),
            "binding_mtime": identity._binding_path(settings).stat().st_mtime_ns,
            "device_id": settings.device_id,
            "peers": load_payload(database_path(self.root), "local_paired_peers", {}),
        }
        for _ in range(3):
            identity.reset_runtime_state_for_tests()
            again = settings_for(self.root)
            result = identity.ensure_identity(again, provider_for(self.hw_a))
            self.assertEqual(result.status, identity.STATUS_VERIFIED)
            self.assertEqual(again.device_id, before["device_id"])
        self.assertEqual(settings.drone_cert_file.read_bytes(), before["cert"])
        self.assertEqual(settings.drone_key_file.read_bytes(), before["key"])
        self.assertEqual(identity._binding_path(settings).read_bytes(), before["binding"])
        self.assertEqual(identity._binding_path(settings).stat().st_mtime_ns, before["binding_mtime"])
        self.assertEqual(load_payload(database_path(self.root), "local_paired_peers", {}), before["peers"])

    def test_reboot_does_not_touch_sqlite(self) -> None:
        settings, _ = self.install()
        seed_swarm(settings)
        with mock.patch.object(swarm_reset, "reset_swarm_identity_state") as reset, \
                mock.patch("app.storage.state_store.open_database") as opened:
            identity.ensure_identity(settings, provider_for(self.hw_a))
        reset.assert_not_called()
        opened.assert_not_called()

    def test_normal_reboot_preserves_admin_session_and_mcp_token(self) -> None:
        original, _ = self.install()
        sessions = SessionStore(database_path(original.userdata_root))
        session_token = sessions.create("admin")
        mcp_token = mcp_auth.generate_token(original)["token"]

        identity.reset_runtime_state_for_tests()
        result = identity.ensure_identity(settings_for(self.root), provider_for(self.hw_a))

        self.assertEqual(result.status, identity.STATUS_VERIFIED)
        self.assertEqual(sessions.validate(session_token), "admin")
        self.assertTrue(mcp_auth.verify_token(original, mcp_token))

    def test_application_upgrade_preserves_identity(self) -> None:
        settings, _ = self.install()
        fingerprint = fingerprint_of(settings)
        # An upgrade swaps app files but never the userdata identity; a newer binding schema is tolerated.
        document = read_binding(settings)
        document["schema_version"] = 2
        document["future_field"] = {"x": 1}
        identity._binding_path(settings).write_text(json.dumps(document), encoding="utf-8")
        result = identity.ensure_identity(settings_for(self.root), provider_for(self.hw_a))
        self.assertEqual(result.status, identity.STATUS_VERIFIED)
        self.assertEqual(fingerprint_of(settings), fingerprint)

    def test_repeated_initialization_is_idempotent(self) -> None:
        first, _ = self.install()
        binding = identity._binding_path(first).read_bytes()
        for _ in range(3):
            self.install()
        self.assertEqual(identity._binding_path(first).read_bytes(), binding)

    def test_fresh_installs_on_one_host_do_not_share_the_nic_derived_device_id(self) -> None:
        host_mac = "5e:98:ab:b9:e3:8b"
        first_root, second_root = self.tmp / "install-1", self.tmp / "install-2"
        first_root.mkdir()
        second_root.mkdir()
        with mock.patch("app.common.device_identity._physical_mac_candidates", return_value=[host_mac]):
            first = settings_for(first_root)
            second = settings_for(second_root)
        self.assertNotEqual(first.device_id, host_mac)
        self.assertNotEqual(second.device_id, host_mac)
        self.assertNotEqual(first.device_id, second.device_id)
        identity.ensure_identity(first, provider_for(self.hw_a))
        identity.ensure_identity(second, provider_for(self.hw_a))
        self.assertEqual(read_binding(first)["device_id"], first.device_id)
        self.assertEqual(read_binding(second)["device_id"], second.device_id)
        self.assertNotEqual(read_binding(first)["device_id"], read_binding(second)["device_id"])

    def test_pinned_device_id_survives_first_installation(self) -> None:
        settings = settings_for(self.root, DRONE_DEVICE_ID="pinned-drone-9")
        with mock.patch.dict("os.environ", {"DRONE_DEVICE_ID": "pinned-drone-9"}):
            result = identity.ensure_identity(settings, provider_for(self.hw_a))
        self.assertEqual(result.settings.device_id, "pinned-drone-9")
        self.assertEqual(read_binding(settings)["device_id"], "pinned-drone-9")

    def test_generic_hardware_ids_are_not_treated_as_unique(self) -> None:
        generic = x86_tree(self.tmp / "hw-generic", uuid="03000200-0400-0500-0006-000700080009", board="Default string")
        settings, result = self.install(generic)
        self.assertEqual(result.state, State.READY)
        self.assertIsNone(read_binding(settings)["hardware_fingerprint"])
        again = identity.ensure_identity(settings_for(self.root), provider_for(self.hw_b))
        # Nothing was ever proven about the old machine, so a later machine adopts a baseline and is not a "clone".
        self.assertEqual(again.state, State.READY)
        self.assertEqual(fingerprint_of(settings), read_binding(settings)["identity_certificate_fingerprint"])


class CloneProvisioningTests(ProvisioningTestBase):
    def test_clone_revokes_copied_admin_sessions_and_mcp_token(self) -> None:
        original, _ = self.install()
        original_db = database_path(original.userdata_root)
        original_sessions = SessionStore(original_db)
        session_token = original_sessions.create("admin")
        mcp_token = mcp_auth.generate_token(original)["token"]

        self.assertEqual(original_sessions.validate(session_token), "admin")
        self.assertTrue(mcp_auth.verify_token(original, mcp_token))
        clone, result = self.clone_boot(self.hw_b)
        self.assertEqual(result.status, identity.STATUS_CLONE)
        clone_sessions = SessionStore(database_path(clone.userdata_root))
        self.assertIsNone(clone_sessions.validate(session_token))
        self.assertFalse(mcp_auth.verify_token(clone, mcp_token))
        self.assertFalse(mcp_auth.token_status(clone)["configured"])
        self.assertEqual(load_payload(database_path(clone.userdata_root), "mcp", "missing"), "missing")

        # Revocation is confined to the clone; both credentials still work on the source.
        self.assertEqual(original_sessions.validate(session_token), "admin")
        self.assertTrue(mcp_auth.verify_token(original, mcp_token))
        self.assertEqual(clone_sessions.validate(clone_sessions.create("admin")), "admin")
        self.assertTrue(mcp_auth.verify_token(clone, mcp_auth.generate_token(clone)["token"]))

    def test_clone_on_different_hardware_gets_a_new_identity_and_no_inherited_swarm(self) -> None:
        original, _ = self.install()
        seed_swarm(original)
        original_fp = fingerprint_of(original)
        original_binding = read_binding(original)
        old_cert = original.drone_cert_file.read_text(encoding="utf-8")

        clone, result = self.clone_boot(self.hw_b)

        self.assertEqual(result.state, State.READY)
        self.assertEqual(result.status, identity.STATUS_CLONE)
        self.assertTrue(identity.swarm_networking_allowed())
        self.assertNotEqual(fingerprint_of(clone), original_fp)
        self.assertNotEqual(result.settings.device_id, original.device_id)
        binding = read_binding(clone)
        self.assertNotEqual(binding["installation_id"], original_binding["installation_id"])
        self.assertNotEqual(binding["hardware_fingerprint"], original_binding["hardware_fingerprint"])
        self.assertEqual(binding["origin"], "clone")
        self.assertEqual(binding["identity_certificate_fingerprint"], fingerprint_of(clone))
        self.assertEqual(binding["device_id"], result.settings.device_id)
        self.assertEqual(clone.drone_key_file.stat().st_mode & 0o777, 0o600)
        # The inherited private key is gone: the old certificate cannot be used with the new key.
        old_cert_file = self.tmp / "old.crt"
        old_cert_file.write_text(old_cert, encoding="utf-8")
        with self.assertRaises(ssl.SSLError):
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(str(old_cert_file), str(clone.drone_key_file))
        # The key material for the device-id file follows the new identity.
        self.assertIn(result.settings.device_id, clone.userdata_root.joinpath("system/drone-app/device-id").read_text())

        db = database_path(clone.userdata_root)
        for namespace in swarm_reset.IDENTITY_BOUND_NAMESPACES:
            self.assertEqual(load_payload(db, namespace, "missing"), "missing", msg=namespace)
        self.assertEqual(local_network.paired_peers(result.settings), [])
        self.assertEqual(load_events(db, "local_sync_activity"), [])
        self.assertIsNone(__import__("app.storage.state_store", fromlist=["x"]).load_peer_route(db, "11:22:33:44:55:66"))
        self.assertEqual(list((clone.userdata_root / "system" / "drone-app" / "local-peer-certs").glob("*")), [])
        queue = load_payload(db, "download_manager.json", {})
        self.assertEqual([job["job_id"] for job in queue["jobs"]], ["b"])
        self.assertTrue(queue["paused"])

    def test_unrelated_settings_and_content_survive_reprovisioning(self) -> None:
        original, _ = self.install()
        seed_swarm(original)
        (self.root / "roms" / "snes").mkdir(parents=True)
        (self.root / "roms" / "snes" / "game.sfc").write_bytes(b"rom")
        clone, _ = self.clone_boot(self.hw_b)
        db = database_path(clone.userdata_root)
        self.assertEqual(load_payload(db, "automation_config.json", {}), {"wifi_recovery": {"enabled": True}})
        self.assertEqual(load_payload(db, "smtp_manager.json", {})["host"], "smtp.example.test")
        self.assertEqual(json.loads(clone.credentials_file.read_text())["username"], "admin")
        self.assertEqual((clone.userdata_root / "roms" / "snes" / "game.sfc").read_bytes(), b"rom")
        with open_database(db) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_original_keeps_identity_and_swarm_and_nothing_is_announced(self) -> None:
        original, _ = self.install()
        seed_swarm(original)
        original_cert = original.drone_cert_file.read_bytes()
        with mock.patch.object(local_network, "forget_peer") as forget, \
                mock.patch.object(swarm_membership, "publish_forget") as publish, \
                mock.patch.object(swarm_membership, "record_local_removal") as tombstone, \
                mock.patch.object(swarm_membership, "propagate_membership") as propagate, \
                mock.patch.object(swarm_membership, "_post_to_peer") as post, \
                mock.patch("urllib.request.urlopen") as urlopen:
            self.clone_boot(self.hw_b)
        for call in (forget, publish, tombstone, propagate, post, urlopen):
            call.assert_not_called()
        # The original continues on its own hardware with everything intact.
        identity.reset_runtime_state_for_tests()
        result = identity.ensure_identity(settings_for(self.root), provider_for(self.hw_a))
        self.assertEqual(result.status, identity.STATUS_VERIFIED)
        self.assertEqual(original.drone_cert_file.read_bytes(), original_cert)
        self.assertEqual(len(local_network.paired_peers(original)), 1)
        self.assertIsNotNone(load_payload(database_path(self.root), "swarm_membership", None))

    def test_clone_does_not_log_secrets_or_raw_hardware_values(self) -> None:
        original, _ = self.install()
        output = []
        with mock.patch("builtins.print", side_effect=lambda *args, **kwargs: output.append(" ".join(map(str, args)))):
            self.clone_boot(self.hw_b)
        text = "\n".join(output)
        for expected in ("[Identity] High-confidence hardware mismatch detected", "[Identity] Clone reprovisioning initiated",
                         "[Identity] New cryptographic identity created", "[Identity] Inherited swarm trust cleared",
                         "[Identity] Clone provisioning completed", "[Identity] Swarm networking enabled"):
            self.assertIn(expected, text)
        for secret in ("PRIVATE KEY", UUID_A, UUID_B, "BOARDSERIAL02", original.drone_key_file.read_text()[:40]):
            self.assertNotIn(secret, text)

    def test_unpaired_image_still_gets_a_unique_identity(self) -> None:
        original, _ = self.install()
        clone, result = self.clone_boot(self.hw_b)
        self.assertEqual(result.status, identity.STATUS_CLONE)
        self.assertNotEqual(fingerprint_of(clone), fingerprint_of(original))

    def test_many_clones_each_provision_a_distinct_identity(self) -> None:
        original, _ = self.install()
        seen = {fingerprint_of(original)}
        device_ids = {original.device_id}
        installations = {read_binding(original)["installation_id"]}
        for index in range(6):
            hardware = x86_tree(self.tmp / f"hw-{index}", uuid=f"{index:08x}-1111-4222-8333-{index:012x}", board=f"SERIAL-UNIT-{index}")
            clone, result = self.clone_boot(hardware, name=f"disk-{index}")
            identity.reset_runtime_state_for_tests()
            seen.add(fingerprint_of(clone))
            device_ids.add(result.settings.device_id)
            installations.add(read_binding(clone)["installation_id"])
        self.assertEqual((len(seen), len(device_ids), len(installations)), (7, 7, 7))

    def test_clones_with_identical_virtual_hardware_still_get_distinct_device_ids(self) -> None:
        original, _ = self.install()
        ids = set()
        for index in range(3):
            clone, result = self.clone_boot(self.hw_b, name=f"vm-{index}")
            ids.add(result.settings.device_id)
        self.assertEqual(len(ids), 3)
        self.assertNotIn(original.device_id, ids)

    def test_clones_can_pair_with_the_original_and_each_other(self) -> None:
        original, _ = self.install()
        clone, result = self.clone_boot(self.hw_b)
        clone_settings = result.settings
        for local, remote in ((clone_settings, original), (original, clone_settings)):
            with mock.patch("app.transfer.local_network._notifications.record_event"):
                saved = local_network.save_paired_peer(
                    local, {"drone_id": remote.device_id, "name": "other", "certificate_fingerprint": fingerprint_of(remote)}
                )
            self.assertEqual(saved["drone_id"], remote.device_id)

    def test_copied_pinned_device_id_is_replaced_on_a_clone(self) -> None:
        settings = settings_for(self.root, DRONE_DEVICE_ID="pinned-drone-1")
        identity.ensure_identity(settings, provider_for(self.hw_a))
        target = self.tmp / "pinned-clone"
        image_disk(self.root, target)
        clone = settings_for(target, DRONE_DEVICE_ID="pinned-drone-1")
        with mock.patch.dict("os.environ", {"DRONE_DEVICE_ID": "pinned-drone-1"}):
            result = identity.ensure_identity(clone, provider_for(self.hw_b))
            self.assertEqual(result.status, identity.STATUS_CLONE)
            self.assertNotEqual(result.settings.device_id, "pinned-drone-1")
            self.assertNotEqual(fingerprint_of(clone), fingerprint_of(settings))
            # The copied environment pin keeps naming the retired id; it must not revive it.
            identity.reset_runtime_state_for_tests()
            again = identity.ensure_identity(settings_for(target, DRONE_DEVICE_ID="pinned-drone-1"), provider_for(self.hw_b))
        self.assertEqual(again.settings.device_id, result.settings.device_id)
        self.assertEqual(read_binding(clone)["device_id"], result.settings.device_id)

    def test_erased_hardware_evidence_in_binding_holds_swarm(self) -> None:
        original, _ = self.install()
        document = read_binding(original)
        document["hardware"] = {"version": 1, "components": {}, "metadata": {}}
        document["hardware_fingerprint"] = None
        identity._binding_path(original).write_text(json.dumps(document), encoding="utf-8")
        result = identity.ensure_identity(settings_for(self.root), provider_for(self.hw_b))
        self.assertEqual(result.state, State.RECOVERY_REQUIRED)
        self.assertEqual(result.reason, "binding_hardware_evidence_missing")

    def test_raspberry_pi_clone(self) -> None:
        pi_a = pi_tree(self.tmp / "pi-a")
        pi_b = pi_tree(self.tmp / "pi-b", serial="10000000ffee1122", dt_serial="10000000ffee1122\x00")
        original, _ = self.install(pi_a)
        self.assertEqual(set(read_binding(original)["hardware"]["components"]), {"platform_serial"})
        identity.ensure_identity(settings_for(self.root), provider_for(pi_a, "aarch64"))
        target = self.tmp / "pi-disk-b"
        image_disk(self.root, target)
        clone = settings_for(target)
        result = identity.ensure_identity(clone, provider_for(pi_b, "aarch64"))
        self.assertEqual(result.status, identity.STATUS_CLONE)
        self.assertNotEqual(fingerprint_of(clone), fingerprint_of(original))


class InsufficientEvidenceTests(ProvisioningTestBase):
    def test_missing_hardware_data_never_rotates_identity(self) -> None:
        original, _ = self.install()
        seed_swarm(original)
        cert = original.drone_cert_file.read_bytes()
        blind = self.tmp / "hw-blind"
        blind.mkdir()
        for _ in range(3):
            identity.reset_runtime_state_for_tests()
            result = identity.ensure_identity(settings_for(self.root), provider_for(blind))
            self.assertEqual(result.state, State.RECOVERY_REQUIRED)
            self.assertEqual(result.reason, "insufficient_evidence")
            self.assertFalse(identity.swarm_networking_allowed())
        self.assertEqual(original.drone_cert_file.read_bytes(), cert)
        self.assertEqual(len(local_network.paired_peers(original)), 1)
        payload = identity.status_payload(original, provider_for(blind))
        self.assertEqual(payload["status"], identity.STATUS_RECOVERY)
        self.assertEqual(payload["recovery"]["actions"], ["keep", "new"])

    def test_contradictory_evidence_holds_swarm_without_destroying_state(self) -> None:
        original, _ = self.install()
        # Same unit UUID, different board serial: odd enough that a human must decide.
        odd = x86_tree(self.tmp / "hw-odd", uuid=UUID_A, board="REPLACEDBOARD")
        result = identity.ensure_identity(settings_for(self.root), provider_for(odd))
        self.assertEqual(result.reason, "contradictory_evidence")
        self.assertFalse(result.swarm_networking_enabled)
        self.assertTrue(original.drone_key_file.exists())

    def test_ambiguous_platform_serials_are_contradictory(self) -> None:
        pi_a = pi_tree(self.tmp / "pi-a")
        self.install(pi_a)
        ambiguous = pi_tree(self.tmp / "pi-x", dt_serial="20000000ffeeddcc\x00")
        result = identity.ensure_identity(settings_for(self.root), provider_for(ambiguous, "aarch64"))
        self.assertEqual(result.reason, "contradictory_evidence")

    def test_unbound_installation_without_identifiers_continues(self) -> None:
        blind = self.tmp / "hw-blind"
        blind.mkdir()
        settings, result = self.install(blind)
        self.assertEqual(result.state, State.READY)
        again = identity.ensure_identity(settings_for(self.root), provider_for(blind))
        self.assertEqual(again.status, identity.STATUS_UNAVAILABLE)
        self.assertTrue(again.swarm_networking_enabled)
        self.assertEqual(identity.status_payload(settings, provider_for(blind))["hardware_verification"], "unavailable")

    def test_corrupt_binding_requires_recovery_and_keeps_keys(self) -> None:
        original, _ = self.install()
        identity._binding_path(original).write_text("{not json", encoding="utf-8")
        cert = original.drone_cert_file.read_bytes()
        result = identity.ensure_identity(settings_for(self.root), provider_for(self.hw_a))
        self.assertEqual(result.state, State.RECOVERY_REQUIRED)
        self.assertEqual(result.reason, "binding_unreadable")
        self.assertEqual(original.drone_cert_file.read_bytes(), cert)


class MigrationTests(ProvisioningTestBase):
    def test_existing_installation_without_binding_is_adopted_not_reset(self) -> None:
        settings = settings_for(self.root)
        DroneCertificateManager(settings).ensure_certificate()
        seed_swarm(settings)
        cert = settings.drone_cert_file.read_bytes()
        result = identity.ensure_identity(settings, provider_for(self.hw_a))
        self.assertEqual(result.origin, "migrated")
        self.assertEqual(result.status, identity.STATUS_VERIFIED)
        self.assertEqual(settings.drone_cert_file.read_bytes(), cert)
        self.assertEqual(len(local_network.paired_peers(settings)), 1)
        binding = read_binding(settings)
        self.assertEqual(binding["history"][0]["event"], "binding_migrated")
        self.assertEqual(binding["device_id"], settings.device_id)

    def test_migration_without_identifiers_records_unavailable(self) -> None:
        settings = settings_for(self.root)
        DroneCertificateManager(settings).ensure_certificate()
        blind = self.tmp / "hw-blind"
        blind.mkdir()
        result = identity.ensure_identity(settings, provider_for(blind))
        self.assertEqual(result.status, identity.STATUS_UNAVAILABLE)

    def test_already_cloned_unbound_install_cannot_be_detected(self) -> None:
        """Documented one-time limitation: with no prior binding a clone looks like the original."""
        settings = settings_for(self.root)
        DroneCertificateManager(settings).ensure_certificate()
        target = self.tmp / "old-clone"
        image_disk(self.root, target)
        clone = settings_for(target)
        result = identity.ensure_identity(clone, provider_for(self.hw_b))
        self.assertEqual(result.origin, "migrated")
        self.assertEqual(fingerprint_of(clone), fingerprint_of(settings))


class RecoveryAndInterruptionTests(ProvisioningTestBase):
    def test_invalid_certificate_on_same_hardware_is_reissued_securely(self) -> None:
        original, _ = self.install()
        seed_swarm(original)
        old = fingerprint_of(original)
        original.drone_cert_file.write_text("garbage", encoding="utf-8")
        result = identity.ensure_identity(settings_for(self.root), provider_for(self.hw_a))
        self.assertEqual(result.state, State.READY)
        self.assertNotEqual(fingerprint_of(original), old)
        binding = read_binding(original)
        self.assertEqual(binding["identity_certificate_fingerprint"], fingerprint_of(original))
        self.assertEqual(binding["history"][-1]["event"], "certificate_reissued")
        self.assertEqual(original.drone_key_file.stat().st_mode & 0o777, 0o600)

    def test_recorded_certificate_rotation_keeps_binding_current(self) -> None:
        original, _ = self.install()
        DroneCertificateManager(original).regenerate_self_signed_certificate()
        identity.record_certificate_rotation(original)
        self.assertEqual(read_binding(original)["identity_certificate_fingerprint"], fingerprint_of(original))
        result = identity.ensure_identity(settings_for(self.root), provider_for(self.hw_a))
        self.assertEqual(result.status, identity.STATUS_VERIFIED)

    def test_failed_certificate_generation_does_not_resurrect_the_inherited_identity(self) -> None:
        original, _ = self.install()
        seed_swarm(original)
        old = fingerprint_of(original)
        target = self.tmp / "disk-b"
        image_disk(self.root, target)
        clone = settings_for(target)
        with mock.patch.object(DroneCertificateManager, "_generate_local_certificate", side_effect=RuntimeError("disk full")):
            result = identity.ensure_identity(clone, provider_for(self.hw_b))
        self.assertEqual(result.state, State.RECOVERY_REQUIRED)
        self.assertEqual(result.reason, "provisioning_failed")
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertFalse(clone.drone_key_file.exists())
        self.assertFalse(clone.drone_cert_file.exists())
        self.assertTrue(identity._journal_path(clone).exists())
        self.assertEqual(local_network.paired_peers(clone), [])
        # Retry on the next start finishes the job.
        identity.reset_runtime_state_for_tests()
        retry = identity.ensure_identity(settings_for(target), provider_for(self.hw_b))
        self.assertEqual(retry.state, State.READY)
        self.assertNotEqual(fingerprint_of(clone), old)
        self.assertFalse(identity._journal_path(clone).exists())
        self.assertFalse(identity._recovery_path(clone).exists())
        self.assertTrue(identity.swarm_networking_allowed())

    def test_held_clone_revokes_inherited_session_and_mcp_token_when_reset_fails(self) -> None:
        original, _ = self.install()
        session_token = SessionStore(database_path(original.userdata_root)).create("admin")
        mcp_token = mcp_auth.generate_token(original)["token"]
        target = self.tmp / "disk-b"
        image_disk(self.root, target)
        clone = settings_for(target)
        with mock.patch.object(swarm_reset, "reset_swarm_identity_state", side_effect=OSError("io error")):
            result = identity.ensure_identity(clone, provider_for(self.hw_b))
        self.assertEqual(result.reason, "provisioning_failed")
        self.assertFalse(result.swarm_networking_enabled)
        self.assertIsNone(SessionStore(database_path(clone.userdata_root)).validate(session_token))
        self.assertFalse(mcp_auth.verify_token(clone, mcp_token))
        # The source installation keeps its own credentials.
        self.assertEqual(SessionStore(database_path(original.userdata_root)).validate(session_token), "admin")
        self.assertTrue(mcp_auth.verify_token(original, mcp_token))

    def test_failure_during_swarm_cleanup_resumes(self) -> None:
        original, _ = self.install()
        seed_swarm(original)
        target = self.tmp / "disk-b"
        image_disk(self.root, target)
        clone = settings_for(target)
        with mock.patch.object(swarm_reset, "reset_swarm_identity_state", side_effect=OSError("io error")):
            result = identity.ensure_identity(clone, provider_for(self.hw_b))
        self.assertEqual(result.reason, "provisioning_failed")
        self.assertFalse(clone.drone_key_file.exists())  # inherited key already destroyed
        retry = identity.ensure_identity(settings_for(target), provider_for(self.hw_b))
        self.assertEqual(retry.status, identity.STATUS_CLONE)
        self.assertEqual(local_network.paired_peers(retry.settings), [])

    def test_crash_after_new_certificate_reuses_it_on_resume(self) -> None:
        original, _ = self.install()
        target = self.tmp / "disk-b"
        image_disk(self.root, target)
        clone = settings_for(target)
        with mock.patch.object(identity, "_save_binding", side_effect=OSError("power loss")):
            first = identity.ensure_identity(clone, provider_for(self.hw_b))
        self.assertEqual(first.reason, "provisioning_failed")
        generated = clone.drone_cert_file.read_bytes()
        self.assertNotEqual(fingerprint_of(clone), fingerprint_of(original))
        retry = identity.ensure_identity(settings_for(target), provider_for(self.hw_b))
        self.assertEqual(retry.state, State.READY)
        self.assertEqual(clone.drone_cert_file.read_bytes(), generated)
        self.assertEqual(retry.settings.device_id, read_binding(clone)["device_id"])

    def test_corrupt_journal_is_treated_as_interrupted_clone_provisioning(self) -> None:
        original, _ = self.install()
        target = self.tmp / "disk-b"
        image_disk(self.root, target)
        clone = settings_for(target)
        identity._journal_path(clone).write_text("{trunc", encoding="utf-8")
        result = identity.ensure_identity(clone, provider_for(self.hw_b))
        self.assertEqual(result.state, State.READY)
        self.assertNotEqual(fingerprint_of(clone), fingerprint_of(original))

    def test_concurrent_initialization_creates_a_single_identity(self) -> None:
        results = []

        def run() -> None:
            settings = settings_for(self.root)
            results.append(identity.ensure_identity(settings, provider_for(self.hw_a)))

        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 8)
        self.assertTrue(all(result.state == State.READY for result in results))
        settings = settings_for(self.root)
        self.assertEqual(read_binding(settings)["identity_certificate_fingerprint"], fingerprint_of(settings))
        self.assertEqual(sum(1 for result in results if result.status == identity.STATUS_NEW_INSTALLATION), 1)

    def test_concurrent_processes_create_a_single_identity(self) -> None:
        hw = self.hw_a
        script = (
            "import sys; from pathlib import Path; from unittest import mock\n"
            "from app.common.settings import Settings; from app.common import identity_provisioning as i\n"
            "from app.common.hardware_identity import HardwareIdentityProvider as P\n"
            "root=sys.argv[1]\n"
            "with mock.patch.dict('os.environ',{'USERDATA_ROOT':root,'TLS_SELF_SIGNED_DIR':root+'/system/certs'},clear=True):\n"
            "    s=Settings.from_env()\n"
            "r=i.ensure_identity(s,P(Path(sys.argv[2]),container_probe=lambda:False,machine='x86_64'))\n"
            "print(r.status)\n"
        )
        processes = [
            subprocess.Popen([sys.executable, "-c", script, str(self.root), str(hw)], cwd=str(REPO_ROOT),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(4)
        ]
        outputs = [process.communicate(timeout=100) for process in processes]
        statuses = [out[0].strip().splitlines()[-1] for out in outputs if out[0].strip()]
        self.assertEqual(len(statuses), 4, msg=str(outputs))
        self.assertEqual(statuses.count(identity.STATUS_NEW_INSTALLATION), 1, msg=str(statuses))
        settings = settings_for(self.root)
        self.assertEqual(read_binding(settings)["identity_certificate_fingerprint"], fingerprint_of(settings))

    def test_externally_managed_identity_is_not_touched(self) -> None:
        for env in ({"DRONE_MTLS_MODE": "managed"}, {"TLS_CERT_FILE": "/x/c.pem", "TLS_KEY_FILE": "/x/k.pem"}):
            settings = settings_for(self.tmp / "managed", **env)
            result = identity.ensure_identity(settings, provider_for(self.hw_a))
            self.assertEqual(result.status, identity.STATUS_MANAGED)
            self.assertFalse(identity._binding_path(settings).exists())


class HardwareReplacementTests(ProvisioningTestBase):
    def test_preauthorized_hardware_change_keeps_identity(self) -> None:
        original, _ = self.install()
        seed_swarm(original)
        fingerprint = fingerprint_of(original)
        identity.authorize_hardware_change(original, 2, actor="admin")
        self.assertIsNotNone(identity.hardware_change_authorization(original))
        result = identity.ensure_identity(settings_for(self.root), provider_for(self.hw_b))
        self.assertEqual(result.status, identity.STATUS_VERIFIED)
        self.assertEqual(fingerprint_of(original), fingerprint)
        self.assertEqual(len(local_network.paired_peers(original)), 1)
        binding = read_binding(original)
        self.assertEqual(binding["history"][-1]["event"], "hardware_rebound")
        self.assertIsNone(identity.hardware_change_authorization(original))
        # The authorization is single-use: the next different machine is a clone.
        identity.reset_runtime_state_for_tests()
        third = x86_tree(self.tmp / "hw-c", uuid="aaaaaaaa-1111-4222-8333-bbbbbbbbbbbb", board="THIRDBOARD3")
        self.assertEqual(identity.ensure_identity(settings_for(self.root), provider_for(third)).status, identity.STATUS_CLONE)

    def test_expired_or_cancelled_authorization_does_not_apply(self) -> None:
        original, _ = self.install()
        identity.authorize_hardware_change(original, 1)
        self.assertTrue(identity.cancel_hardware_change(original))
        self.assertFalse(identity.cancel_hardware_change(original))
        expired = {"authorized_at": "2020-01-01T00:00:00+00:00", "expires_at": "2020-01-02T00:00:00+00:00"}
        identity._authorization_path(original).write_text(json.dumps(expired), encoding="utf-8")
        self.assertIsNone(identity.hardware_change_authorization(original))
        result = identity.ensure_identity(settings_for(self.root), provider_for(self.hw_b))
        self.assertEqual(result.status, identity.STATUS_CLONE)

    def test_authorization_window_is_bounded(self) -> None:
        original, _ = self.install()
        for bad in (0, -1, identity.MAX_AUTHORIZATION_HOURS + 1, "abc", None.__class__):
            with self.assertRaises(ValueError):
                identity.authorize_hardware_change(original, bad)

    def test_administrator_can_keep_identity_after_recovery_hold(self) -> None:
        original, _ = self.install()
        seed_swarm(original)
        fingerprint = fingerprint_of(original)
        odd = x86_tree(self.tmp / "hw-odd", uuid=UUID_A, board="REPLACEDBOARD")
        held = identity.ensure_identity(settings_for(self.root), provider_for(odd))
        self.assertEqual(held.state, State.RECOVERY_REQUIRED)
        with mock.patch.object(identity, "_tailnet_module") as tailnet:
            outcome = identity.resolve_recovery(original, "keep", provider_for(odd), actor="admin")
        self.assertEqual(outcome["action"], "keep")
        self.assertTrue(identity.swarm_networking_allowed())
        self.assertEqual(fingerprint_of(original), fingerprint)
        self.assertFalse(identity._recovery_path(original).exists())
        tailnet.return_value.release_local_node.assert_not_called()  # it was never held (no tailscale here)
        identity.reset_runtime_state_for_tests()
        self.assertEqual(identity.ensure_identity(settings_for(self.root), provider_for(odd)).status, identity.STATUS_VERIFIED)

    def test_keep_during_missing_hardware_evidence_preserves_the_baseline(self) -> None:
        original, _ = self.install()
        baseline = read_binding(original)["hardware_fingerprint"]
        blind = self.tmp / "hw-blind"
        blind.mkdir()
        self.assertEqual(identity.ensure_identity(settings_for(self.root), provider_for(blind)).state, State.RECOVERY_REQUIRED)
        identity.resolve_recovery(original, "keep", provider_for(blind), actor="admin")
        self.assertEqual(read_binding(original)["hardware_fingerprint"], baseline)
        identity.reset_runtime_state_for_tests()
        self.assertEqual(identity.ensure_identity(settings_for(self.root), provider_for(self.hw_b)).status, identity.STATUS_CLONE)

    def test_administrator_can_provision_a_new_identity_after_recovery_hold(self) -> None:
        original, _ = self.install()
        seed_swarm(original)
        fingerprint = fingerprint_of(original)
        blind = self.tmp / "hw-blind"
        blind.mkdir()
        identity.ensure_identity(settings_for(self.root), provider_for(blind))
        outcome = identity.resolve_recovery(settings_for(self.root), "new", provider_for(blind))
        self.assertEqual(outcome["action"], "new")
        self.assertNotEqual(fingerprint_of(original), fingerprint)
        self.assertEqual(local_network.paired_peers(original), [])
        self.assertTrue(identity.swarm_networking_allowed())

    def test_keep_is_rejected_when_nothing_is_pending(self) -> None:
        original, _ = self.install()
        with self.assertRaises(ValueError):
            identity.resolve_recovery(original, "keep", provider_for(self.hw_a))
        with self.assertRaises(ValueError):
            identity.resolve_recovery(original, "bogus", provider_for(self.hw_a))

    def test_administrator_can_replace_the_identity_of_an_already_cloned_install(self) -> None:
        settings = settings_for(self.root)
        DroneCertificateManager(settings).ensure_certificate()
        seed_swarm(settings)
        identity.ensure_identity(settings, provider_for(self.hw_a))  # migrated baseline, nothing pending
        before = fingerprint_of(settings)
        outcome = identity.resolve_recovery(settings, "new", provider_for(self.hw_a), actor="admin")
        self.assertEqual(outcome["action"], "new")
        self.assertNotEqual(fingerprint_of(settings), before)
        self.assertEqual(local_network.paired_peers(settings), [])
        self.assertEqual(read_binding(settings)["origin"], "operator")

    def test_unfinished_provisioning_cannot_be_resolved_by_keeping(self) -> None:
        original, _ = self.install()
        target = self.tmp / "disk-b"
        image_disk(self.root, target)
        clone = settings_for(target)
        with mock.patch.object(DroneCertificateManager, "_generate_local_certificate", side_effect=RuntimeError("x")):
            identity.ensure_identity(clone, provider_for(self.hw_b))
        with self.assertRaises(ValueError):
            identity.resolve_recovery(clone, "keep", provider_for(self.hw_b))
        identity.resolve_recovery(clone, "new", provider_for(self.hw_b))
        self.assertTrue(identity.swarm_networking_allowed())


class ContainerTests(ProvisioningTestBase):
    def test_container_instances_get_independent_identities(self) -> None:
        fingerprints, ids = set(), set()
        host_hw = HardwareIdentityProvider(self.hw_a, container_probe=lambda: True, machine="x86_64")
        for index in range(3):
            root = self.tmp / f"container-{index}"
            root.mkdir()
            settings = settings_for(root, DRONE_DEVICE_ID=f"drone-container-{index}")
            result = identity.ensure_identity(settings, host_hw)
            self.assertEqual(result.state, State.READY)
            self.assertIsNone(read_binding(settings)["hardware_fingerprint"])
            fingerprints.add(fingerprint_of(settings))
            ids.add(settings.device_id)
        self.assertEqual((len(fingerprints), len(ids)), (3, 3))


class SwarmIsolationTests(ProvisioningTestBase):
    def test_outbound_peer_context_refused_while_identity_unresolved(self) -> None:
        from app.transfer.peer_connectivity import _drone_client_ssl_context

        settings, _ = self.install()
        identity._publish(State.RECOVERY_REQUIRED, identity.STATUS_RECOVERY, "x", False)
        with self.assertRaises(PermissionError):
            _drone_client_ssl_context(settings, "https://10.0.0.5:8543")
        self.assertIsNone(_drone_client_ssl_context(settings, "http://10.0.0.5"))

    def test_peer_routes_are_closed_while_identity_unresolved(self) -> None:
        from app.web.api_routes import ApiRoutesMixin

        sent = []

        class Handler(ApiRoutesMixin):
            server = object()

            def _send_json(self, status, payload, **_kw):
                sent.append(status)

        identity._publish(State.RECOVERY_REQUIRED, identity.STATUS_RECOVERY, "x", False)
        self.assertTrue(Handler()._reject_if_wrong_listener_surface(["peer", "info"], "/peer/info"))
        self.assertEqual(sent, [503])
        identity._publish(State.READY, identity.STATUS_VERIFIED, "", True)
        self.assertFalse(Handler()._reject_if_wrong_listener_surface(["peer", "info"], "/peer/info"))

    def test_gossip_cannot_replace_a_pinned_fingerprint(self) -> None:
        settings, _ = self.install()
        pem_one, pem_two = self.make_pem("one"), self.make_pem("two")
        from app.transfer.drone_network import _certificate_pem_fingerprint

        with mock.patch("app.transfer.local_network._notifications.record_event"):
            local_network.save_paired_peer(
                settings, {"drone_id": "aa:bb:cc:dd:ee:01", "name": "p", "certificate_fingerprint": _certificate_pem_fingerprint(pem_one)}
            )
        record = {"peer_id": "aa:bb:cc:dd:ee:01", "op": "add", "epoch": 9, "origin": "x",
                  "card": {"certificate_pem": pem_two, "reachable_url": "https://10.0.0.2", "scheme": "https"}}
        self.assertFalse(swarm_membership._apply_add(settings, {"clock": 0, "records": {}}, record))
        pinned = local_network.get_paired_peer(settings, "aa:bb:cc:dd:ee:01")
        self.assertEqual(pinned["certificate_fingerprint"], _certificate_pem_fingerprint(pem_one))

    def test_duplicate_identity_is_isolated_not_revoked(self) -> None:
        settings, _ = self.install()
        seed_swarm(settings)
        own_pem = settings.drone_cert_file.read_text(encoding="utf-8")
        record = {"peer_id": "aa:bb:cc:dd:ee:02", "op": "add", "epoch": 3, "origin": "x",
                  "card": {"certificate_pem": own_pem, "reachable_url": "https://10.0.0.3", "scheme": "https"}}
        before = load_payload(database_path(self.root), "local_paired_peers", {})
        self.assertFalse(swarm_membership._apply_add(settings, {"clock": 0, "records": {}}, record))
        self.assertEqual(load_payload(database_path(self.root), "local_paired_peers", {}), before)
        events = load_events(database_path(self.root), "identity_collisions")
        self.assertEqual(events[0]["source"], "membership-own-certificate")
        self.assertTrue(settings.drone_cert_file.exists())

    def test_discovery_announcing_our_certificate_is_quarantined(self) -> None:
        settings, _ = self.install()
        payload = {
            "service": local_network.DISCOVERY_SERVICE, "drone_id": "ff:ee:dd:cc:bb:aa", "name": "dup",
            "certificate_fingerprint": fingerprint_of(settings), "scheme": "https", "api_port": 443,
        }
        row = local_network.record_discovered_peer(settings, payload, source_ip="10.0.0.77")
        self.assertTrue(row["identity_conflict"])
        self.assertFalse(row["paired"])
        self.assertTrue(row["drone_id"].startswith("identity-conflict:"))
        self.assertEqual(load_events(database_path(self.root), "identity_collisions")[0]["source"], "discovery-duplicate-identity")

    def make_pem(self, name: str) -> str:
        directory = Path(tempfile.mkdtemp(dir=self.tmp))
        subprocess.run(
            ["openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048", "-keyout", str(directory / "k"), "-out", str(directory / "c"),
             "-days", "30", "-subj", f"/CN={name}"],
            check=True, capture_output=True,
        )
        return (directory / "c").read_text(encoding="utf-8")


class TailnetCloneTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.device import tailnet_service

        self.ts = tailnet_service
        self.tmp = Path(tempfile.mkdtemp(prefix="drone-ts-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "tailscale").write_text("#!/bin/sh\n")
        (self.tmp / "state").mkdir()
        (self.tmp / "state" / "tailscaled.state").write_text("node-key-material")
        (self.tmp / "state" / "certs").mkdir()
        (self.tmp / "state" / "certs" / "x.key").write_text("k")

    def patches(self):
        return (
            mock.patch.object(self.ts, "TAILSCALE_DIR", self.tmp),
            mock.patch.object(self.ts, "TAILSCALE_CLI", self.tmp / "bin" / "tailscale"),
        )

    def test_clone_removes_local_node_state_without_logging_out(self) -> None:
        calls = []
        first, second = self.patches()
        with first, second, \
                mock.patch.object(self.ts, "_stop_tailscaled", return_value=True), \
                mock.patch.object(self.ts, "_start_daemon_if_needed", return_value=None), \
                mock.patch.object(self.ts, "_run_cli", side_effect=lambda args, timeout: calls.append(args)):
            result = self.ts.reset_local_node_identity()
        self.assertEqual(result["status"], "reset")
        self.assertTrue(result["enrollment_required"])
        self.assertEqual(list((self.tmp / "state").rglob("*.state")), [])
        self.assertEqual(list((self.tmp / "state").rglob("*.key")), [])
        self.assertNotIn(["logout"], calls)  # would deregister the original Drone's shared node key

    def test_unstoppable_daemon_is_taken_down_not_wiped(self) -> None:
        calls = []
        first, second = self.patches()
        with first, second, mock.patch.object(self.ts, "_stop_tailscaled", return_value=False), \
                mock.patch.object(self.ts, "_run_cli", side_effect=lambda args, timeout: calls.append(args) or mock.Mock(returncode=0)):
            result = self.ts.reset_local_node_identity()
        self.assertEqual(result["status"], "held")
        self.assertIn(["down"], calls)
        self.assertTrue((self.tmp / "state" / "tailscaled.state").exists())

    def test_failed_down_is_an_error_not_a_hold(self) -> None:
        first, second = self.patches()
        with first, second, mock.patch.object(self.ts, "_stop_tailscaled", return_value=False), \
                mock.patch.object(self.ts, "hold_local_node", return_value=False):
            self.assertEqual(self.ts.reset_local_node_identity()["status"], "error")
        self.assertTrue((self.tmp / "state" / "tailscaled.state").exists())

    def test_not_installed(self) -> None:
        with mock.patch.object(self.ts, "TAILSCALE_CLI", self.tmp / "missing"):
            self.assertEqual(self.ts.reset_local_node_identity(), {"status": "not_installed"})

    def _clone_with_tailnet_failure(self, hold_result: bool):
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        hw_a = x86_tree(self.tmp / "hw-a")
        hw_b = x86_tree(self.tmp / "hw-b", uuid=UUID_B, board="BOARDSERIAL02")
        root = self.tmp / "disk"
        root.mkdir()
        identity.ensure_identity(settings_for(root), provider_for(hw_a))
        save_payload(database_path(root), "tailnet_sharing.json", {"auth_key": "tskey-secret", "sharing_enabled": True})
        target = self.tmp / "clone"
        image_disk(root, target)
        with mock.patch.object(self.ts, "reset_local_node_identity", side_effect=RuntimeError("boom")), \
                mock.patch.object(self.ts, "hold_local_node", return_value=hold_result):
            result = identity.ensure_identity(settings_for(target), provider_for(hw_b))
        return target, result

    def test_unclearable_tailnet_node_that_can_be_held_lets_the_clone_continue_offline(self) -> None:
        target, result = self._clone_with_tailnet_failure(hold_result=True)
        self.assertEqual(result.state, State.READY)
        self.assertTrue(read_binding(settings_for(target))["tailnet_reset_pending"])
        sharing = load_payload(database_path(target), "tailnet_sharing.json", {})
        self.assertEqual(sharing["auth_key"], "")
        self.assertFalse(sharing["sharing_enabled"])

    def test_unclearable_and_unholdable_tailnet_node_holds_the_swarm(self) -> None:
        target, result = self._clone_with_tailnet_failure(hold_result=False)
        self.assertEqual(result.state, State.RECOVERY_REQUIRED)
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertTrue(identity._journal_path(settings_for(target)).exists())


class FailureBoundaryTests(ProvisioningTestBase):
    def test_unremovable_peer_pin_blocks_ready_and_is_retried(self) -> None:
        original, _ = self.install()
        seed_swarm(original)
        target = self.tmp / "disk-b"
        image_disk(self.root, target)
        clone = settings_for(target)
        stuck = target / "system" / "drone-app" / "local-peer-certs" / "peer.crt"
        real_unlink = Path.unlink

        def flaky(path, *args, **kwargs):
            if path == stuck:
                raise PermissionError("read-only")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(swarm_reset.Path, "unlink", flaky):
            failed = identity.ensure_identity(clone, provider_for(self.hw_b))
        self.assertEqual(failed.state, State.RECOVERY_REQUIRED)
        self.assertTrue(stuck.exists())
        identity.reset_runtime_state_for_tests()
        retried = identity.ensure_identity(settings_for(target), provider_for(self.hw_b))
        self.assertEqual(retried.state, State.READY)
        self.assertFalse(stuck.exists())

    def test_failed_credential_revocation_blocks_ready_and_is_retried(self) -> None:
        from app.device import tailnet_service

        original, _ = self.install()
        save_payload(database_path(self.root), "tailnet_sharing.json", {"auth_key": "tskey-copied", "sharing_enabled": True})
        target = self.tmp / "disk-b"
        image_disk(self.root, target)
        with mock.patch.object(tailnet_service, "_revoke_local_sharing", side_effect=OSError("read-only")):
            failed = identity.ensure_identity(settings_for(target), provider_for(self.hw_b))
        self.assertEqual(failed.state, State.RECOVERY_REQUIRED)
        self.assertEqual(load_payload(database_path(target), "tailnet_sharing.json", {})["auth_key"], "tskey-copied")
        identity.reset_runtime_state_for_tests()
        retried = identity.ensure_identity(settings_for(target), provider_for(self.hw_b))
        self.assertEqual(retried.state, State.READY)
        self.assertEqual(load_payload(database_path(target), "tailnet_sharing.json", {})["auth_key"], "")

    def test_valid_foreign_certificate_does_not_silently_replace_the_bound_identity(self) -> None:
        original, _ = self.install()
        other_root = self.tmp / "other"
        other_root.mkdir()
        other = settings_for(other_root)
        identity.ensure_identity(other, provider_for(self.hw_a))
        bound = identity._binding_path(original).read_bytes()
        original.drone_cert_file.write_bytes(other.drone_cert_file.read_bytes())
        original.drone_key_file.write_bytes(other.drone_key_file.read_bytes())
        identity.reset_runtime_state_for_tests()
        result = identity.ensure_identity(settings_for(self.root), provider_for(self.hw_a))
        self.assertEqual(result.reason, "certificate_mismatch")
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertEqual(identity._binding_path(original).read_bytes(), bound)
        # An administrator can explicitly keep it.
        identity.resolve_recovery(original, "keep", provider_for(self.hw_a))
        self.assertEqual(read_binding(original)["identity_certificate_fingerprint"], fingerprint_of(original))

    def test_automatic_certificate_renewal_keeps_the_binding_current(self) -> None:
        original, _ = self.install()
        original.drone_cert_file.write_text("garbage", encoding="utf-8")
        DroneCertificateManager(original).ensure_certificate()
        self.assertEqual(read_binding(original)["identity_certificate_fingerprint"], fingerprint_of(original))
        identity.reset_runtime_state_for_tests()
        self.assertEqual(identity.ensure_identity(settings_for(self.root), provider_for(self.hw_a)).status, identity.STATUS_VERIFIED)


class IdentityHandlerTests(ProvisioningTestBase):
    def setUp(self) -> None:
        super().setUp()
        from app.web import handlers_identity

        handlers_identity.reset_failed_passwords_for_tests()
        self.addCleanup(handlers_identity.reset_failed_passwords_for_tests)

    def test_repeated_wrong_passwords_are_throttled(self) -> None:
        identity.ensure_identity(settings_for(self.root), provider_for(self.hw_a))
        statuses = []
        for _ in range(7):
            handler, sent, _settings = self.handler(body={"password": "wrong", "action": "new"})
            handler._handle_admin_identity_post(["resolve"])
            statuses.append(sent[-1][0])
        self.assertEqual(statuses, [403] * 5 + [429] * 2)
        handler, sent, _settings = self.handler(body={"password": "correct horse", "action": "new"})
        handler._handle_admin_identity_post(["resolve"])
        self.assertEqual(sent[-1][0], 429)  # even the right password waits out the lockout

    def handler(self, *, session=True, password_ok=True, origin=None, content_type="application/json", body=None):
        from app.web.handlers_identity import HandlersIdentityMixin

        outer = self
        settings = settings_for(self.root)
        sent = []

        class Auth:
            class credential_store:  # noqa: N801
                @staticmethod
                def check(username, password):
                    return password_ok and password == "correct horse"

            @staticmethod
            def authenticate_request(headers, client_ip=None):
                return {"token": "t", "username": "admin"} if session else None

        class Handler(HandlersIdentityMixin):
            auth = Auth
            client_address = ("192.168.1.9", 1234)

            def __init__(self):
                self.settings = settings
                self.headers = {"Content-Type": content_type, "Host": "drone.local", **({"Origin": origin} if origin else {})}

            def _send_json(self, status, payload, **_kw):
                sent.append((status, payload))

            def _send_unauthorized(self):
                sent.append((401, {}))

            def _read_json_body(self):
                return body or {}

        return Handler(), sent, settings

    def test_status_requires_a_session_and_hides_raw_identifiers(self) -> None:
        handler, sent, settings = self.handler(session=False)
        handler._handle_admin_identity_get(["status"])
        self.assertEqual(sent[0][0], 401)
        handler, sent, settings = self.handler()
        identity.ensure_identity(settings, provider_for(self.hw_a))
        handler._handle_admin_identity_get(["status"])
        status, payload = sent[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], identity.STATUS_NEW_INSTALLATION)
        self.assertNotIn(UUID_A, json.dumps(payload))

    def test_authorizing_hardware_change_needs_password_session_and_same_origin(self) -> None:
        identity.ensure_identity(settings_for(self.root), provider_for(self.hw_a))
        for kwargs, expected in (
            ({"session": False, "body": {"password": "correct horse"}}, 401),
            ({"body": {"password": "wrong"}}, 403),
            ({"body": {}}, 403),
            ({"body": {"password": "correct horse"}, "origin": "https://evil.example"}, 403),
            ({"body": {"password": "correct horse"}, "content_type": "text/plain"}, 415),
        ):
            handler, sent, settings = self.handler(**kwargs)
            handler._handle_admin_identity_post(["authorize-hardware-change"])
            self.assertEqual(sent[-1][0], expected, msg=str(kwargs))
            self.assertIsNone(identity.hardware_change_authorization(settings))
        handler, sent, settings = self.handler(body={"password": "correct horse", "hours": 3}, origin="https://drone.local")
        handler._handle_admin_identity_post(["authorize-hardware-change"])
        self.assertEqual(sent[-1][0], 200)
        self.assertIsNotNone(identity.hardware_change_authorization(settings))
        handler, sent, settings = self.handler(body={"password": "correct horse"})
        handler._handle_admin_identity_post(["cancel-hardware-change"])
        self.assertTrue(sent[-1][1]["cancelled"])

    def test_resolving_recovery_restarts_the_drone(self) -> None:
        settings = settings_for(self.root)
        identity.ensure_identity(settings, provider_for(self.hw_a))
        blind = self.tmp / "hw-blind"
        blind.mkdir()
        identity.ensure_identity(settings, provider_for(blind))
        handler, sent, _ = self.handler(body={"password": "wrong", "action": "keep"})
        handler._handle_admin_identity_post(["resolve"])
        self.assertEqual(sent[-1][0], 403)
        self.assertTrue(identity._recovery_path(settings).exists())
        handler, sent, _ = self.handler(body={"password": "correct horse", "action": "keep"})
        with mock.patch("app.web.handlers_identity._restart_drone_process_soon") as restart, \
                mock.patch.object(identity, "HardwareIdentityProvider", return_value=provider_for(blind)):
            handler._handle_admin_identity_post(["resolve"])
        self.assertEqual(sent[-1][0], 200)
        restart.assert_called_once()
        handler, sent, _ = self.handler(body={"password": "correct horse", "action": "keep"})
        handler._handle_admin_identity_post(["resolve"])
        self.assertEqual(sent[-1][0], 409)  # nothing pending any more


if __name__ == "__main__":
    unittest.main()


class MockUserdataRootRejectionTests(unittest.TestCase):
    """A mocked userdata_root must raise, never silently keep the old id or report success."""

    def _mock_settings(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(userdata_root=mock.MagicMock(), device_id="AA:BB:CC:DD:EE:FF")

    def test_choose_device_id_rejects_mock_root_instead_of_reusing_configured_id(self) -> None:
        with self.assertRaises(TypeError):
            identity._choose_device_id(self._mock_settings(), {})

    def test_choose_device_id_rejects_mock_journal_instead_of_stringifying_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = types.SimpleNamespace(userdata_root=tmp, device_id="AA:BB:CC:DD:EE:FF")
            for journal in (mock.MagicMock(), mock.MagicMock(spec=dict)):
                with self.subTest(journal=type(journal)):
                    with self.assertRaises(identity.IdentityError):
                        identity._choose_device_id(settings, journal)

    def test_choose_device_id_refuses_non_mapping_json_journal_without_minting_an_id(self) -> None:
        # Decoded JSON that is not an object must fail closed: raise (so ensure_identity holds
        # networking in RECOVERY_REQUIRED) and never mint or persist a fresh device id.
        with tempfile.TemporaryDirectory() as tmp:
            settings = types.SimpleNamespace(userdata_root=tmp, device_id="AA:BB:CC:DD:EE:FF")
            for journal in ([], "02:11:22:33:44:55", 7, None, True):
                with self.subTest(journal=journal):
                    with self.assertRaisesRegex(identity.IdentityError, "must be a mapping"):
                        identity._choose_device_id(settings, journal)
            self.assertFalse(identity._device_id_path(Path(tmp)).exists())

    def test_choose_device_id_accepts_a_real_userdict_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = types.SimpleNamespace(userdata_root=tmp, device_id="AA:BB:CC:DD:EE:FF")
            journal = collections.UserDict({"new_device_id": "02:11:22:33:44:55"})
            self.assertEqual(
                identity._choose_device_id(settings, journal), "02:11:22:33:44:55"
            )

    def test_record_journal_serializes_mapping_implementations_as_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = types.SimpleNamespace(userdata_root=tmp)
            expected = {"state": "PROVISIONING", "reason": "clone"}
            for journal in (
                collections.UserDict(expected),
                collections.ChainMap({"state": "PROVISIONING"}, {"reason": "clone"}),
                types.MappingProxyType(expected),
            ):
                with self.subTest(journal=type(journal).__name__):
                    identity._record_journal(settings, journal)
                    self.assertEqual(
                        json.loads(identity._journal_path(settings).read_text(encoding="utf-8")),
                        expected,
                    )

    def test_choose_device_id_rejects_mapping_with_inconsistent_key_lookup(self) -> None:
        class InconsistentMapping(collections.abc.Mapping):
            def __contains__(self, key: object) -> bool:
                return key == "new_device_id"

            def __getitem__(self, key: str) -> object:
                raise KeyError(key)

            def __iter__(self):
                return iter(())

            def __len__(self) -> int:
                return 0

        with tempfile.TemporaryDirectory() as tmp:
            settings = types.SimpleNamespace(userdata_root=tmp, device_id="AA:BB:CC:DD:EE:FF")
            with self.assertRaisesRegex(identity.IdentityError, "journaled device id"):
                identity._choose_device_id(settings, InconsistentMapping())
            self.assertFalse(identity._device_id_path(Path(tmp)).exists())

    def test_choose_device_id_rejects_non_string_resumed_id_instead_of_stringifying_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = types.SimpleNamespace(userdata_root=tmp, device_id="AA:BB:CC:DD:EE:FF")
            for value in (mock.MagicMock(), 0, False, [], {}, 12345, ["02:aa:bb:cc:dd:ee"], "bad id with spaces!"):
                with self.subTest(value=type(value)):
                    with self.assertRaises(identity.IdentityError):
                        identity._choose_device_id(settings, {"new_device_id": value})
                    self.assertFalse(identity._device_id_path(Path(tmp)).exists())

    def test_choose_device_id_treats_none_as_no_resumed_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = types.SimpleNamespace(userdata_root=tmp, device_id="AA:BB:CC:DD:EE:FF")
            chosen = identity._choose_device_id(settings, {"new_device_id": None})
            self.assertEqual(identity._device_id_path(Path(tmp)).read_text(encoding="utf-8").strip(), chosen)

    def test_choose_device_id_rejects_empty_string_as_invalid_resumed_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = types.SimpleNamespace(userdata_root=tmp, device_id="AA:BB:CC:DD:EE:FF")
            with self.assertRaises(identity.IdentityError):
                identity._choose_device_id(settings, {"new_device_id": ""})
            self.assertFalse(identity._device_id_path(Path(tmp)).exists())

    def test_retire_device_id_rejects_mock_root(self) -> None:
        with self.assertRaises(TypeError):
            identity._retire_device_id(self._mock_settings())

    def test_clear_pinned_peer_certificates_rejects_mock_root_instead_of_reporting_zero(self) -> None:
        from app.transfer import swarm_reset

        with self.assertRaises(TypeError):
            swarm_reset._clear_pinned_peer_certificates(self._mock_settings())
