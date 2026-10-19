"""Issue #369 UAT: an unreadable journal on a confident hardware-mismatch clone reprovisions.

The #369 report treated the clone outcome for an unreadable journal (torn JSON, or valid
non-object JSON such as ``null`` or ``[]``) as a bug. It is the documented policy
(docs/drone-identity.md, "Journal type contract"): the hardware verdict is re-derived from
the durable binding, and a confident mismatch is a clone. A clone must not keep the
original's logical id, certificate or pinned swarm trust, because that is the #160 hazard.
Holding the clone in RECOVERY_REQUIRED would leave it trusting as the original.

What this suite pins:

- the reported reproduction (``null`` and ``[]`` journals, different DMI evidence) ends
  ``READY`` with ``clone_automatically_provisioned``, a new certificate, a new logical id,
  and inherited peer trust cleared;
- the clone never reuses an identifier found in the journal bytes (the journal is never
  resumed as a device id);
- an unreadable journal on the *original* hardware is discarded and the original keeps its
  identity (the same run, unchanged hardware, is not a clone).
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
CLONE_UUID = "22222222-3333-4444-5555-666666666666"

UNREADABLE_JOURNALS = ("{truncated", "null", "[]")
LEAKED_ID_MARKER = "drone-leaked-id-369"


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
class UnreadableJournalCloneUatTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-369-journal-policy-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original_hw = hardware(self.root / "hw-original", ORIGINAL_UUID, "BOARD-ORIGINAL-369")
        self.clone_hw = hardware(self.root / "hw-clone", CLONE_UUID, "BOARD-CLONE-369")

    def _bound_original(self) -> Settings:
        original = settings(self.root / "original")
        first = identity.ensure_identity(original, self.original_hw)
        self.assertEqual(first.state, identity.State.READY)
        peer = {"peer-1": {"drone_id": "peer-1", "certificate_fingerprint": "b" * 64}}
        save_payload(database_path(original.userdata_root), "local_paired_peers", peer)
        return original

    def _copy_with_journal(self, original: Settings, name: str, payload: str) -> Settings:
        clone_root = self.root / name
        shutil.copytree(original.userdata_root, clone_root)
        clone = settings(clone_root)
        journal = identity._journal_path(clone)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(payload, encoding="utf-8")
        return clone

    def test_reported_reproduction_clone_reprovisions_with_unreadable_journal(self) -> None:
        original = self._bound_original()
        original_cert = original.drone_cert_file.read_bytes()
        for index, payload in enumerate(UNREADABLE_JOURNALS):
            with self.subTest(journal=payload):
                clone = self._copy_with_journal(original, f"clone-{index}", payload)
                identity.reset_runtime_state_for_tests()
                result = identity.ensure_identity(clone, self.clone_hw)
                self.assertEqual(result.state, identity.State.READY)
                self.assertEqual(result.status, identity.STATUS_CLONE)
                self.assertEqual(result.reason, "clone")
                self.assertTrue(identity.swarm_networking_allowed())
                self.assertNotEqual(result.settings.device_id, original.device_id)
                self.assertNotEqual(clone.drone_cert_file.read_bytes(), original_cert)
                self.assertEqual(
                    load_payload(database_path(clone.userdata_root), "local_paired_peers", {}),
                    {},
                )

    def test_clone_never_reuses_an_identifier_from_the_journal(self) -> None:
        original = self._bound_original()
        # Valid JSON whose only string is an id-shaped value: it is not an object, so the
        # journal must never be resumed, and the fresh id must not be that value.
        payload = f'"{LEAKED_ID_MARKER}"'
        clone = self._copy_with_journal(original, "clone-leak", payload)
        identity.reset_runtime_state_for_tests()
        result = identity.ensure_identity(clone, self.clone_hw)
        self.assertEqual(result.state, identity.State.READY)
        self.assertNotIn(LEAKED_ID_MARKER, result.settings.device_id)
        self.assertNotEqual(result.settings.device_id, original.device_id)

    def test_unreadable_journal_on_original_hardware_keeps_identity(self) -> None:
        original = self._bound_original()
        original_id = original.device_id
        for index, payload in enumerate(UNREADABLE_JOURNALS):
            with self.subTest(journal=payload):
                same = self._copy_with_journal(original, f"same-{index}", payload)
                identity.reset_runtime_state_for_tests()
                result = identity.ensure_identity(same, self.original_hw)
                self.assertEqual(result.state, identity.State.READY)
                self.assertNotEqual(result.status, identity.STATUS_CLONE)
                self.assertEqual(result.settings.device_id, original_id)
                self.assertFalse(identity._journal_path(same).exists())


if __name__ == "__main__":
    unittest.main()
