"""Issue #255 adversarial: a non-object journal must not change the hardware verdict.

Domain invariant (docs/drone-identity.md, verdict table): when the bound hardware is
confidently different, the outcome is automatic clone provisioning. An unreadable
journal is only a reason to re-derive the decision from the durable binding and the
present hardware; it never replaces that verdict.

The #255 patch made a valid non-object journal (array, null, bool, number, string)
fail closed as RECOVERY_REQUIRED on every path, including a clone. Before the patch the
same clone was reprovisioned (fresh logical id, new certificate, inherited swarm trust
cleared, networking on). The result is that a clone with a junk journal keeps the
original's logical id and certificate and never gets its own identity.

The torn-JSON case ("{trunc") is the positive control: it still reprovisions, so a
failure here isolates the non-object branch rather than the clone harness.

This suite is expected to FAIL on the current patch. It is a blocking #255 finding.
"""

from __future__ import annotations

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


ORIGINAL_UUID = "3f713284-3270-4c5f-990c-17231045b68e"
CLONE_UUID = "11111111-2222-3333-4444-555555555555"

# Valid JSON that decodes to something other than an object.
NON_OBJECT_JOURNALS = ("[]", "null", "true", "false", "0", "7", '""', '"02:11:22:33:44:55"')
TORN_CONTROL = "{truncated"


def hardware(root: Path, uuid: str, serial: str) -> HardwareIdentityProvider:
    for name, value in (("product_uuid", uuid), ("board_serial", serial)):
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
class NonObjectJournalCloneReprovisionTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-255-clone-journal-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original_hw = hardware(self.root / "hw-original", ORIGINAL_UUID, "BOARD-ORIGINAL-255")
        self.clone_hw = hardware(self.root / "hw-clone", CLONE_UUID, "BOARD-CLONE-255")

    def _bound_original(self) -> Settings:
        original = settings(self.root / "original")
        first = identity.ensure_identity(original, self.original_hw)
        self.assertEqual(first.state, identity.State.READY)
        peer = {"peer-1": {"drone_id": "peer-1", "certificate_fingerprint": "a" * 64}}
        save_payload(database_path(original.userdata_root), "local_paired_peers", peer)
        return original

    def _clone_with_journal(self, original: Settings, name: str, payload: str) -> Settings:
        clone_root = self.root / name
        shutil.copytree(original.userdata_root, clone_root)
        clone = settings(clone_root)
        journal = identity._journal_path(clone)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(payload, encoding="utf-8")
        return clone

    def _assert_reprovisioned(self, original: Settings, clone: Settings, result, label: str) -> None:
        self.assertEqual(result.state, identity.State.READY, msg=label)
        self.assertEqual(result.reason, "clone", msg=label)
        self.assertTrue(identity.swarm_networking_allowed(), msg=label)
        self.assertNotEqual(result.settings.device_id, original.device_id, msg=label)
        self.assertNotEqual(
            clone.drone_cert_file.read_bytes(),
            original.drone_cert_file.read_bytes(),
            msg=label,
        )
        self.assertEqual(
            load_payload(database_path(clone.userdata_root), "local_paired_peers", {}),
            {},
            msg=label,
        )

    def test_torn_json_clone_reprovisions_positive_control(self) -> None:
        original = self._bound_original()
        clone = self._clone_with_journal(original, "clone-torn", TORN_CONTROL)
        identity.reset_runtime_state_for_tests()
        result = identity.ensure_identity(clone, self.clone_hw)
        self._assert_reprovisioned(original, clone, result, "torn journal control")

    def test_non_object_journal_clone_reprovisions_like_torn_json(self) -> None:
        original = self._bound_original()
        for index, payload in enumerate(NON_OBJECT_JOURNALS):
            with self.subTest(journal=payload):
                clone = self._clone_with_journal(original, f"clone-non-object-{index}", payload)
                identity.reset_runtime_state_for_tests()
                result = identity.ensure_identity(clone, self.clone_hw)
                self._assert_reprovisioned(original, clone, result, payload)


if __name__ == "__main__":
    unittest.main()
