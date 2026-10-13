"""Issue #160: damaged recovery metadata cannot make the original a clone."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.storage.state_store import database_path, load_payload, save_payload


def hardware(root: Path) -> HardwareIdentityProvider:
    values = {
        "product_uuid": "3f713284-3270-4c5f-990c-17231045b68e",
        "board_serial": "BOARD-ORIGINAL-8941",
    }
    for name, value in values.items():
        path = root / "sys/class/dmi/id" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def settings(root: Path) -> Settings:
    with mock.patch.dict(os.environ, {
        "USERDATA_ROOT": str(root),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
    }, clear=True):
        return Settings.from_env()


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class CorruptJournalOriginalUAT(unittest.TestCase):
    def test_unreadable_journal_on_original_does_not_destroy_bound_identity(self) -> None:
        """Only a proven hardware mismatch can initiate automatic clone cleanup."""
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        temporary = tempfile.TemporaryDirectory(prefix="issue-160-original-journal-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        provider = hardware(root / "hardware")
        original = settings(root / "userdata")
        self.assertEqual(identity.ensure_identity(original, provider).state, identity.State.READY)
        db = database_path(original.userdata_root)
        peer = {"peer-1": {"drone_id": "peer-1", "certificate_fingerprint": "a" * 64}}
        save_payload(db, "local_paired_peers", peer)
        certificate = original.drone_cert_file.read_bytes()
        private_key = original.drone_key_file.read_bytes()
        binding = identity._binding_path(original).read_bytes()
        identity._journal_path(original).write_text("{truncated", encoding="utf-8")

        result = identity.ensure_identity(settings(original.userdata_root), provider)

        self.assertEqual(original.drone_cert_file.read_bytes(), certificate)
        self.assertEqual(original.drone_key_file.read_bytes(), private_key)
        self.assertEqual(identity._binding_path(original).read_bytes(), binding)
        self.assertEqual(load_payload(db, "local_paired_peers", {}), peer)
        self.assertEqual(result.settings.device_id, original.device_id)
        self.assertNotEqual(result.status, identity.STATUS_CLONE)
        self.assertIn(result.state, (identity.State.READY, identity.State.RECOVERY_REQUIRED))
        if result.state is identity.State.RECOVERY_REQUIRED:
            self.assertFalse(identity.swarm_networking_allowed())


if __name__ == "__main__":
    unittest.main()
