"""Issue #160: a boot with no hardware evidence must not weaken clone protection.

"Do not treat a missing identifier as evidence of new hardware." The same rule has to hold
for an administrator's one-shot hardware-change pre-authorization: it exists for a real
hardware swap, so a boot that merely cannot read the identifiers (non-root run, sysfs not
mounted yet) must not spend it, and must not erase the hardware evidence the clone check
depends on. Otherwise an image taken afterwards carries an unbound identity and every
machine booted from it silently keeps the original's certificate.
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


def provider(root: Path, uuid: str = "", serial: str = "") -> HardwareIdentityProvider:
    directory = root / "sys" / "class" / "dmi" / "id"
    directory.mkdir(parents=True, exist_ok=True)
    if uuid:
        (directory / "product_uuid").write_text(uuid + "\n", encoding="utf-8")
    if serial:
        (directory / "board_serial").write_text(serial + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def settings(root: Path) -> Settings:
    with mock.patch.dict(os.environ, {
        "USERDATA_ROOT": str(root),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
    }, clear=True):
        return Settings.from_env()


def fingerprint(item: Settings) -> str:
    pem = item.drone_cert_file.read_text(encoding="utf-8")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


@unittest.skipUnless(shutil.which("openssl"), "openssl is required")
class HardwareEvidenceDowngradeUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-160-downgrade-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hw_a = provider(self.tmp / "hw-a", "3f713284-3270-4c5f-990c-17231045b68e", "BOARD-ORIG-8941")
        self.hw_c = provider(self.tmp / "hw-c", "b81d7e20-4c11-4f6a-9a53-6e0c2d98a417", "BOARD-CLONE-0077")
        (self.tmp / "blind").mkdir()
        self.blind = HardwareIdentityProvider(self.tmp / "blind", container_probe=lambda: False, machine="x86_64")
        self.root = self.tmp / "disk-a"
        self.root.mkdir()
        self.original = settings(self.root)
        self.assertEqual(identity.ensure_identity(self.original, self.hw_a).state, identity.State.READY)
        self.bound_fingerprint = json.loads(identity._binding_path(self.original).read_text())["hardware_fingerprint"]
        self.assertTrue(self.bound_fingerprint)

    def test_blind_boot_with_pending_authorization_keeps_the_hardware_binding(self) -> None:
        identity.authorize_hardware_change(self.original, 2, actor="admin")

        identity.reset_runtime_state_for_tests()
        identity.ensure_identity(settings(self.root), self.blind)

        binding = json.loads(identity._binding_path(self.original).read_text())
        self.assertEqual(
            binding["hardware_fingerprint"], self.bound_fingerprint,
            msg="a boot without hardware evidence erased the recorded hardware fingerprint",
        )

    def test_image_taken_after_a_blind_authorized_boot_is_still_recognised_as_a_clone(self) -> None:
        identity.authorize_hardware_change(self.original, 2, actor="admin")
        identity.reset_runtime_state_for_tests()
        identity.ensure_identity(settings(self.root), self.blind)
        # The administrator notices and withdraws the authorization before imaging the disk.
        identity.cancel_hardware_change(self.original)
        target = self.tmp / "disk-c"
        shutil.copytree(self.root, target, symlinks=True)

        identity.reset_runtime_state_for_tests()
        clone = settings(target)
        result = identity.ensure_identity(clone, self.hw_c)

        self.assertNotEqual(
            fingerprint(clone), fingerprint(self.original),
            msg="the clone kept the original's identity certificate",
        )
        self.assertEqual(result.status, identity.STATUS_CLONE)


if __name__ == "__main__":
    unittest.main()
