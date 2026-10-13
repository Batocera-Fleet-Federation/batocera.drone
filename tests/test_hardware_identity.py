"""Hardware fingerprinting + verdicts used for clone detection."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.hardware_identity import (
    BASELINE,
    CONTRADICTORY,
    DIFFERENT,
    INSUFFICIENT,
    SAME,
    HardwareIdentityProvider,
    HardwareSnapshot,
    compare_hardware,
    normalize_identifier,
)

UUID_A = "6c1f9a52-83d4-4e0b-9a7e-5b2d1c3f4a60"
UUID_B = "0b7e2d91-5c3a-47f8-8d1e-9a4c6b2f7e13"


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def x86_tree(root: Path, uuid=UUID_A, board="BSN12345678", vendor="ASUSTeK", product="PRIME B450") -> Path:
    write(root, "sys/class/dmi/id/product_uuid", f"{uuid}\n" if uuid is not None else "")
    write(root, "sys/class/dmi/id/board_serial", f"{board}\n" if board is not None else "")
    write(root, "sys/class/dmi/id/sys_vendor", f"{vendor}\n")
    write(root, "sys/class/dmi/id/product_name", f"{product}\n")
    return root


def pi_tree(root: Path, serial="10000000a1b2c3d4", dt_serial="10000000a1b2c3d4\x00") -> Path:
    write(root, "proc/cpuinfo", f"processor\t: 0\nHardware\t: BCM2711\nSerial\t\t: {serial}\nModel\t\t: Raspberry Pi 4\n")
    if dt_serial is not None:
        write(root, "sys/firmware/devicetree/base/serial-number", dt_serial)
    return root


def provider(root: Path, machine="x86_64") -> HardwareIdentityProvider:
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine=machine)


class NormalizationTests(unittest.TestCase):
    def test_placeholders_and_generic_values_are_rejected(self) -> None:
        for value in (
            "", "   ", "None", "To Be Filled By O.E.M.", "Default string", "Unknown", "N/A", "0", "123456789",
            "System Serial Number", "00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "03000200-0400-0500-0006-000700080009", "0000000000000000", "FFFFFFFFFFFFFFFF", "abab", "x" * 400, None,
        ):
            kind = "dmi_product_uuid" if "-" in str(value) else "dmi_board_serial"
            self.assertIsNone(normalize_identifier(kind, value), msg=repr(value))

    def test_uuid_must_be_well_formed(self) -> None:
        self.assertIsNone(normalize_identifier("dmi_product_uuid", "not-a-uuid"))
        self.assertEqual(normalize_identifier("dmi_product_uuid", UUID_A.upper()), UUID_A)

    def test_real_serial_is_normalized(self) -> None:
        self.assertEqual(normalize_identifier("platform_serial", " 10000000A1B2C3D4 \n"), "10000000a1b2c3d4")


class ProviderTests(unittest.TestCase):
    def test_x86_snapshot_uses_hashes_not_raw_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = provider(x86_tree(Path(tmp))).collect()
        self.assertEqual(set(snapshot.components), {"dmi_product_uuid", "dmi_board_serial"})
        self.assertTrue(snapshot.usable)
        rendered = str(snapshot.to_dict()) + str(snapshot.describe())
        for raw in (UUID_A, "bsn12345678", "BSN12345678"):
            self.assertNotIn(raw, rendered)
        self.assertEqual(len(snapshot.fingerprint), 64)
        self.assertFalse(snapshot.describe()["virtualized"])

    def test_snapshot_is_deterministic_and_machine_specific(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = provider(x86_tree(Path(a))).collect()
            again = provider(Path(a)).collect()
            other = provider(x86_tree(Path(b), uuid=UUID_B, board="OTHERBOARD99")).collect()
        self.assertEqual(first.fingerprint, again.fingerprint)
        self.assertNotEqual(first.fingerprint, other.fingerprint)

    def test_raspberry_pi_serial_from_cpuinfo_and_devicetree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = provider(pi_tree(Path(tmp)), machine="aarch64").collect()
        self.assertEqual(set(snapshot.components), {"platform_serial"})
        self.assertEqual(snapshot.describe()["architecture"], "aarch64")

    def test_conflicting_platform_serials_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = provider(pi_tree(Path(tmp), dt_serial="20000000ffeeddcc\x00")).collect()
        self.assertTrue(snapshot.ambiguous)
        self.assertFalse(snapshot.usable)

    def test_qemu_zero_serial_is_not_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = provider(pi_tree(Path(tmp), serial="0000000000000000", dt_serial=None), machine="aarch64").collect()
        self.assertEqual(snapshot.components, {})
        self.assertEqual(snapshot.metadata["unavailable_reason"], "no_unique_hardware_identifier")

    def test_missing_or_placeholder_dmi_yields_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = provider(Path(tmp)).collect()
            generic = provider(x86_tree(Path(tmp), uuid="03000200-0400-0500-0006-000700080009", board="Default string")).collect()
        self.assertEqual(empty.components, {})
        self.assertEqual(generic.components, {})

    def test_containers_never_report_host_hardware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            container = HardwareIdentityProvider(x86_tree(Path(tmp)), container_probe=lambda: True)
            snapshot = container.collect()
        self.assertEqual(snapshot.components, {})
        self.assertTrue(snapshot.describe()["container"])

    def test_virtual_machines_are_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = provider(x86_tree(Path(tmp), vendor="QEMU", product="Standard PC")).collect()
        self.assertTrue(snapshot.describe()["virtualized"])

    def test_disable_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"DRONE_HARDWARE_IDENTITY_DISABLED": "1"}):
            self.assertEqual(provider(x86_tree(Path(tmp))).collect().components, {})

    def test_fixture_root_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            x86_tree(Path(tmp))
            with mock.patch.dict("os.environ", {"DRONE_HARDWARE_ID_ROOT": tmp}):
                self.assertIn("dmi_product_uuid", HardwareIdentityProvider(machine="x86_64").collect().components)

    def test_round_trip_through_storage_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = provider(x86_tree(Path(tmp))).collect()
        restored = HardwareSnapshot.from_dict(snapshot.to_dict())
        self.assertEqual(restored.fingerprint, snapshot.fingerprint)
        self.assertIsNone(HardwareSnapshot.from_dict("garbage"))


class VerdictTests(unittest.TestCase):
    def test_same_machine(self) -> None:
        a = HardwareSnapshot(components={"dmi_product_uuid": "1" * 64, "dmi_board_serial": "2" * 64})
        self.assertEqual(compare_hardware(a, HardwareSnapshot(components=dict(a.components))), SAME)

    def test_different_machine_requires_every_comparable_identifier_to_differ(self) -> None:
        a = HardwareSnapshot(components={"dmi_product_uuid": "1" * 64, "dmi_board_serial": "2" * 64})
        b = HardwareSnapshot(components={"dmi_product_uuid": "3" * 64, "dmi_board_serial": "4" * 64})
        self.assertEqual(compare_hardware(a, b), DIFFERENT)

    def test_primary_identifier_decides_over_shared_vendor_serial(self) -> None:
        a = HardwareSnapshot(components={"dmi_product_uuid": "1" * 64, "dmi_board_serial": "2" * 64})
        # Same-batch boards often share a vendor-wide serial; the unit UUID still proves a different machine.
        b = HardwareSnapshot(components={"dmi_product_uuid": "3" * 64, "dmi_board_serial": "2" * 64})
        self.assertEqual(compare_hardware(a, b), DIFFERENT)

    def test_same_unit_identifier_with_changed_board_serial_is_contradictory(self) -> None:
        a = HardwareSnapshot(components={"dmi_product_uuid": "1" * 64, "dmi_board_serial": "2" * 64})
        b = HardwareSnapshot(components={"dmi_product_uuid": "1" * 64, "dmi_board_serial": "4" * 64})
        self.assertEqual(compare_hardware(a, b), CONTRADICTORY)

    def test_secondary_serials_decide_only_without_a_primary(self) -> None:
        a = HardwareSnapshot(components={"dmi_board_serial": "2" * 64, "dmi_product_serial": "5" * 64})
        self.assertEqual(compare_hardware(a, HardwareSnapshot(components={"dmi_board_serial": "4" * 64})), DIFFERENT)
        self.assertEqual(compare_hardware(a, HardwareSnapshot(components={"dmi_board_serial": "2" * 64})), SAME)
        mixed = HardwareSnapshot(components={"dmi_board_serial": "2" * 64, "dmi_product_serial": "6" * 64})
        self.assertEqual(compare_hardware(a, mixed), CONTRADICTORY)

    def test_pi_serial_change_is_different(self) -> None:
        self.assertEqual(
            compare_hardware(HardwareSnapshot(components={"platform_serial": "a" * 64}), HardwareSnapshot(components={"platform_serial": "b" * 64})),
            DIFFERENT,
        )

    def test_missing_identifier_is_never_a_mismatch(self) -> None:
        a = HardwareSnapshot(components={"dmi_product_uuid": "1" * 64})
        self.assertEqual(compare_hardware(a, HardwareSnapshot()), INSUFFICIENT)
        self.assertEqual(compare_hardware(a, HardwareSnapshot(components={"platform_serial": "b" * 64})), INSUFFICIENT)

    def test_ambiguous_current_snapshot_is_contradictory(self) -> None:
        a = HardwareSnapshot(components={"dmi_product_uuid": "1" * 64})
        self.assertEqual(compare_hardware(a, HardwareSnapshot(components={"dmi_product_uuid": "1" * 64}, ambiguous=True)), CONTRADICTORY)

    def test_nothing_stored(self) -> None:
        current = HardwareSnapshot(components={"dmi_product_uuid": "1" * 64})
        self.assertEqual(compare_hardware(None, current), BASELINE)
        self.assertEqual(compare_hardware(HardwareSnapshot(), current), BASELINE)
        self.assertEqual(compare_hardware(None, HardwareSnapshot()), INSUFFICIENT)

    def test_fingerprint_version_mismatch_is_insufficient(self) -> None:
        a = HardwareSnapshot(components={"dmi_product_uuid": "1" * 64}, version=2)
        self.assertEqual(compare_hardware(a, HardwareSnapshot(components={"dmi_product_uuid": "1" * 64})), INSUFFICIENT)


if __name__ == "__main__":
    unittest.main()
