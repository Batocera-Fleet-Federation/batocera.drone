"""Issue #160: an imaged in-flight journal without unique hardware must stay held.

Requirement 3: missing identifiers are not new hardware, and an ambiguous
result must never let an unverified inherited identity join the swarm.
Requirement 9: a partially provisioned identity must not enter the swarm;
unfinished provisioning resumes only when the hardware is the one the
journal was written for. A disk image taken mid-provisioning can land on a
board with no DMI/serials; that clone must not become READY with the
in-flight certificate.
"""

import hashlib
import json
import os
import shutil
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.storage.state_store import database_path, load_payload, save_payload
from app.transfer.peer_connectivity import _drone_client_ssl_context, _local_pair_peer


PEER = {"drone_id": "aa:bb:cc:dd:ee:01", "name": "other", "reachable_url": "https://10.0.0.5", "api_port": 443}


def hardware(root: Path, uuid: str = "", serial: str = "") -> HardwareIdentityProvider:
    directory = root / "sys/class/dmi/id"
    directory.mkdir(parents=True, exist_ok=True)
    if uuid:
        (directory / "product_uuid").write_text(uuid + "\n", encoding="utf-8")
    if serial:
        (directory / "board_serial").write_text(serial + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def settings_for(root: Path) -> Settings:
    with mock.patch.dict(os.environ, {
        "USERDATA_ROOT": str(root),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
    }, clear=True):
        return Settings.from_env()


def fingerprint(item: Settings) -> str:
    pem = item.drone_cert_file.read_text(encoding="utf-8")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class ImagedJournalInsufficientEvidenceUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-160-journal-blind-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hw_a = hardware(self.tmp / "hw-a", "c25ddfd4-6e36-48cd-b489-1843d4da74ad", "BOARD-ORIGINAL-8291")
        self.hw_b = hardware(self.tmp / "hw-b", "20b5f8b7-5962-4b8e-985a-bc3c6ace160d", "BOARD-CLONE-8172")
        self.blind = hardware(self.tmp / "hw-blind")
        self.original = settings_for(self.tmp / "original")
        self.assertEqual(identity.ensure_identity(self.original, self.hw_a).state, identity.State.READY)
        save_payload(
            database_path(self.original.userdata_root),
            "local_paired_peers",
            {PEER["drone_id"]: {"drone_id": PEER["drone_id"], "certificate_fingerprint": "a" * 64, "paired": True}},
        )

    def _interrupt_clone_after_certificate(self) -> Settings:
        middle = self.tmp / "middle"
        shutil.copytree(self.original.userdata_root, middle, symlinks=True)
        clone = settings_for(middle)
        with mock.patch.object(identity, "_save_binding", side_effect=OSError("interrupted write")):
            failed = identity.ensure_identity(clone, self.hw_b)
        self.assertEqual(failed.state, identity.State.RECOVERY_REQUIRED)
        journal = json.loads(identity._journal_path(clone).read_text(encoding="utf-8"))
        self.assertEqual(journal["reason"], "clone")
        self.assertTrue(journal["steps"].get("certificate"))
        return clone

    def test_image_of_in_flight_clone_journal_on_identifierless_hardware_stays_held(self) -> None:
        middle = self._interrupt_clone_after_certificate()
        in_flight_fp = fingerprint(middle)
        in_flight_id = json.loads(identity._journal_path(middle).read_text(encoding="utf-8")).get("new_device_id")
        third = self.tmp / "third"
        shutil.copytree(middle.userdata_root, third, symlinks=True)

        identity.reset_runtime_state_for_tests()
        result = identity.ensure_identity(settings_for(third), self.blind)
        self.assertEqual(result.state, identity.State.RECOVERY_REQUIRED)
        self.assertFalse(result.swarm_networking_enabled)
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertEqual(fingerprint(settings_for(third)), in_flight_fp)
        payload = identity.status_payload(settings_for(third), self.blind)
        self.assertNotIn("keep", (payload.get("recovery") or {}).get("actions", []))

        sent = []

        def capture(request, timeout=None, context=None):
            sent.append(request)
            raise OSError("peer unreachable")

        with mock.patch("app.transfer.peer_connectivity.urlopen", capture):
            with self.assertRaises(PermissionError):
                _local_pair_peer(settings_for(third), dict(PEER), "12345678")
            with self.assertRaises(PermissionError):
                _drone_client_ssl_context(settings_for(third), "https://10.0.0.5:8543")
        self.assertEqual(sent, [])

        with self.assertRaises(ValueError):
            identity.resolve_recovery(settings_for(third), "keep", self.blind, actor="admin")
        identity.reset_runtime_state_for_tests()
        replaced = identity.resolve_recovery(settings_for(third), "new", self.blind, actor="admin")
        self.assertEqual(replaced["action"], "new")
        self.assertNotEqual(fingerprint(settings_for(third)), in_flight_fp)
        self.assertNotEqual(replaced["device_id"], in_flight_id)
        self.assertEqual(load_payload(database_path(third), "local_paired_peers", {}), {})
        self.assertTrue(identity.swarm_networking_allowed())

        identity.reset_runtime_state_for_tests()
        middle_result = identity.ensure_identity(settings_for(middle.userdata_root), self.hw_b)
        self.assertEqual(middle_result.state, identity.State.READY)
        self.assertEqual(fingerprint(settings_for(middle.userdata_root)), in_flight_fp)
        self.assertEqual(fingerprint(self.original), json.loads(identity._binding_path(self.original).read_text())["identity_certificate_fingerprint"])


if __name__ == "__main__":
    unittest.main()
