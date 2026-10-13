"""Issue #160: cloneable identifiers must never decide hardware identity.

A restored disk image copies hostname, IP, NIC MAC, disk/filesystem UUID,
/etc/machine-id, and the installation UUID stored on that disk. Requirement 3
forbids using any of those as hardware evidence. Scenario A still has to treat
the original machine as the same machine when only those values change.
Scenario B still has to treat different DMI/board identifiers as a clone even
when every cloneable identifier is identical.
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
from app.common.hardware_identity import (
    ALL_KINDS,
    DIFFERENT,
    SAME,
    HardwareIdentityProvider,
    compare_hardware,
)
from app.common.settings import Settings


UUID_A = "6c1f9a52-83d4-4e0b-9a7e-5b2d1c3f4a60"
UUID_B = "0b7e2d91-5c3a-47f8-8d1e-9a4c6b2f7e13"
BOARD_A = "BOARD-SOURCE-1234"
BOARD_B = "BOARD-CLONE-5678"

CLONEABLE = {
    "etc/hostname": "batocera",
    "etc/machine-id": "0123456789abcdef0123456789abcdef",
    "sys/class/net/eth0/address": "aa:bb:cc:dd:ee:ff",
    "sys/class/net/wlan0/address": "de:ad:be:ef:00:01",
    "etc/fstab": "UUID=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee / userdata ext4 defaults 0 1\n",
    "proc/sys/kernel/random/boot_id": "ffffffff-1111-2222-3333-444444444444\n",
    "dev/disk/by-uuid/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee": "rootfs\n",
}


def write_file(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def plant_cloneable(root: Path, **overrides: str) -> None:
    for relative, value in {**CLONEABLE, **overrides}.items():
        write_file(root, relative, value)
    (root / "sys/class/net/eth0/device").mkdir(parents=True, exist_ok=True)


def hardware(root: Path, uuid: str, serial: str, **cloneable: str) -> HardwareIdentityProvider:
    write_file(root, "sys/class/dmi/id/product_uuid", uuid + "\n")
    write_file(root, "sys/class/dmi/id/board_serial", serial + "\n")
    write_file(root, "sys/class/dmi/id/sys_vendor", "Example Hardware Inc.\n")
    write_file(root, "sys/class/dmi/id/product_name", "Drone Test Board\n")
    plant_cloneable(root, **cloneable)
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
class ForbiddenCloneableIdentifierTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-160-forbidden-ids-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_cloneable_identifiers_are_not_hardware_evidence(self) -> None:
        first = hardware(self.tmp / "same-dmi-a", UUID_A, BOARD_A)
        second = hardware(
            self.tmp / "same-dmi-b",
            UUID_A,
            BOARD_A,
            **{
                "etc/hostname": "other-host",
                "etc/machine-id": "fedcba9876543210fedcba9876543210",
                "sys/class/net/eth0/address": "11:22:33:44:55:66",
                "sys/class/net/wlan0/address": "77:88:99:aa:bb:cc",
                "etc/fstab": "UUID=99999999-8888-7777-6666-555555555555 / userdata ext4 defaults 0 1\n",
                "proc/sys/kernel/random/boot_id": "00000000-aaaa-bbbb-cccc-dddddddddddd\n",
                "dev/disk/by-uuid/99999999-8888-7777-6666-555555555555": "rootfs\n",
            },
        )
        one, two = first.collect(), second.collect()
        self.assertEqual(set(one.components), {"dmi_product_uuid", "dmi_board_serial"})
        self.assertTrue(set(one.components).issubset(ALL_KINDS))
        self.assertEqual(one.fingerprint, two.fingerprint)
        self.assertEqual(compare_hardware(one, two), SAME)

        rendered = json.dumps(one.to_dict()) + json.dumps(one.describe())
        for leaked in (
            "batocera",
            "0123456789abcdef0123456789abcdef",
            "aa:bb:cc:dd:ee:ff",
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            UUID_A,
            BOARD_A.lower(),
        ):
            self.assertNotIn(leaked, rendered.lower())

    def test_copied_cloneable_identifiers_do_not_hide_a_hardware_mismatch(self) -> None:
        """The disk-image case: MAC/machine-id/disk UUID/hostname all match; DMI does not."""
        source_hw = hardware(self.tmp / "hw-source", UUID_A, BOARD_A)
        clone_hw = hardware(self.tmp / "hw-clone", UUID_B, BOARD_B)
        self.assertEqual(compare_hardware(source_hw.collect(), clone_hw.collect()), DIFFERENT)

        original = settings_for(self.tmp / "original")
        self.assertEqual(identity.ensure_identity(original, source_hw).state, identity.State.READY)
        source_fp = fingerprint(original)
        source_id = original.device_id
        installation = json.loads(identity._binding_path(original).read_text(encoding="utf-8"))["installation_id"]

        target = self.tmp / "clone"
        shutil.copytree(original.userdata_root, target, symlinks=True)
        identity.reset_runtime_state_for_tests()
        result = identity.ensure_identity(settings_for(target), clone_hw)

        self.assertEqual(result.status, identity.STATUS_CLONE)
        self.assertNotEqual(fingerprint(settings_for(target)), source_fp)
        self.assertNotEqual(result.settings.device_id, source_id)
        clone_binding = json.loads(identity._binding_path(settings_for(target)).read_text(encoding="utf-8"))
        self.assertNotEqual(clone_binding["installation_id"], installation)
        self.assertEqual(fingerprint(original), source_fp)

    def test_same_machine_keeps_identity_when_only_cloneable_identifiers_change(self) -> None:
        source_hw = hardware(self.tmp / "hw-a", UUID_A, BOARD_A)
        original = settings_for(self.tmp / "original")
        self.assertEqual(identity.ensure_identity(original, source_hw).state, identity.State.READY)
        cert = original.drone_cert_file.read_bytes()
        key = original.drone_key_file.read_bytes()
        device_id = original.device_id
        binding = identity._binding_path(original).read_bytes()

        renamed = hardware(
            self.tmp / "hw-a-renamed",
            UUID_A,
            BOARD_A,
            **{
                "etc/hostname": "renamed-batocera",
                "etc/machine-id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "sys/class/net/eth0/address": "00:11:22:33:44:55",
            },
        )
        identity.reset_runtime_state_for_tests()
        again = identity.ensure_identity(settings_for(original.userdata_root), renamed)
        self.assertEqual(again.status, identity.STATUS_VERIFIED)
        self.assertEqual(original.drone_cert_file.read_bytes(), cert)
        self.assertEqual(original.drone_key_file.read_bytes(), key)
        self.assertEqual(again.settings.device_id, device_id)
        self.assertEqual(identity._binding_path(original).read_bytes(), binding)


if __name__ == "__main__":
    unittest.main()
