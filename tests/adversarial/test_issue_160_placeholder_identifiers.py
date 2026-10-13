"""Issue #160: vendor placeholder strings are not hardware identifiers.

Two physically different boards that both report "Not Settable" (or any other shared
filler) must not be judged the same machine. The requirement: reject empty, generic,
placeholder, or known non-unique identifiers before fingerprinting.
"""

import tempfile
import unittest
from pathlib import Path

from app.common.hardware_identity import (
    HardwareIdentityProvider,
    SAME,
    compare_hardware,
    normalize_identifier,
)

# Fillers that firmware vendors ship in place of a real serial number.
PLACEHOLDER_SERIALS = (
    "Not Settable",
    "NotSettable",
    "Not Defined",
    "MB-1234567890",
)


def snapshot(root: Path, board_serial: str):
    directory = root / "sys" / "class" / "dmi" / "id"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "board_serial").write_text(board_serial + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64").collect()


class PlaceholderIdentifierTests(unittest.TestCase):
    def test_placeholder_serials_are_rejected_by_the_normalizer(self) -> None:
        for value in PLACEHOLDER_SERIALS:
            for kind in ("dmi_board_serial", "dmi_product_serial", "dmi_chassis_serial"):
                self.assertIsNone(normalize_identifier(kind, value), msg=f"{kind}={value!r}")

    def test_machines_sharing_a_placeholder_serial_produce_no_hardware_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for value in PLACEHOLDER_SERIALS:
                one = snapshot(Path(directory) / "one", value)
                two = snapshot(Path(directory) / "two", value)
                self.assertEqual(one.components, {}, msg=value)
                self.assertNotEqual(compare_hardware(one, two), SAME, msg=value)


if __name__ == "__main__":
    unittest.main()
