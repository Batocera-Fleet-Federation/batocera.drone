"""Issue #160: sequential filler values are generic placeholders, not unique hardware identifiers.

Requirement 3: "Reject empty, generic, placeholder, or known non-unique identifiers."
Firmware vendors routinely ship counting sequences (123456, 9876543210, 00112233-4455-...)
as stand-ins. Treating one as evidence makes every unit with that filler look like the same
machine (a clone goes undetected) and falsely reports the install as hardware-verified.
Genuine values that merely *contain* a sequence must still be accepted.
"""

import tempfile
import unittest
from pathlib import Path

from app.common.hardware_identity import HardwareIdentityProvider, normalize_identifier

SEQUENTIAL_SERIALS = ("123456", "1234567", "234567", "01234567", "654321", "76543210", "987654321", "9876543210")
SEQUENTIAL_UUIDS = (
    "00112233-4455-6677-8899-aabbccddeeff",
    "01234567-89ab-cdef-fedc-ba9876543210",
    "fedcba98-7654-3210-fedc-ba9876543210",
    "12345678-9abc-def0-1234-56789abcdef0",
)
GENUINE_SERIALS = ("BSN12345678", "L1HF25401A3", "PF2K9X7Q", "NB-4821-7730", "9S2K7Q31")
GENUINE_UUIDS = ("6c1f9a52-83d4-4e0b-9a7e-5b2d1c3f4a60", "0b7e2d91-5c3a-47f8-8d1e-9a4c6b2f7e13")


def machine(root: Path, uuid: str = "", serial: str = "") -> HardwareIdentityProvider:
    directory = root / "sys" / "class" / "dmi" / "id"
    directory.mkdir(parents=True, exist_ok=True)
    if uuid:
        (directory / "product_uuid").write_text(uuid + "\n", encoding="utf-8")
    if serial:
        (directory / "board_serial").write_text(serial + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


class SequentialPlaceholderTests(unittest.TestCase):
    def test_sequential_serials_are_rejected(self) -> None:
        accepted = [value for value in SEQUENTIAL_SERIALS if normalize_identifier("dmi_board_serial", value)]
        self.assertEqual(accepted, [], msg="sequential filler accepted as a unique serial")

    def test_sequential_uuids_are_rejected(self) -> None:
        accepted = [value for value in SEQUENTIAL_UUIDS if normalize_identifier("dmi_product_uuid", value)]
        self.assertEqual(accepted, [], msg="sequential filler accepted as a unique product UUID")

    def test_genuine_values_that_contain_a_sequence_are_still_accepted(self) -> None:
        self.assertEqual([v for v in GENUINE_SERIALS if not normalize_identifier("dmi_board_serial", v)], [])
        self.assertEqual([v for v in GENUINE_UUIDS if not normalize_identifier("dmi_product_uuid", v)], [])

    def test_provider_reports_no_evidence_for_a_board_that_only_carries_filler(self) -> None:
        for uuid, serial in zip(SEQUENTIAL_UUIDS, SEQUENTIAL_SERIALS):
            with tempfile.TemporaryDirectory() as tmp:
                snapshot = machine(Path(tmp), uuid, serial).collect()
            self.assertEqual(snapshot.components, {}, msg=f"{uuid} / {serial}")
            self.assertEqual(snapshot.metadata.get("unavailable_reason"), "no_unique_hardware_identifier")


if __name__ == "__main__":
    unittest.main()
