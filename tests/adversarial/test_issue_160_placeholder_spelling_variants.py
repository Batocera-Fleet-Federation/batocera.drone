"""Issue #160: placeholder rejection must not depend on one exact spelling.

Firmware vendors ship the same filler in many spellings ("Chassis Serial Number",
"ChassisSerialNumber", "Not Specified.", "(null)"). A machine whose only evidence is such
a filler would be judged the *same machine* as any other with the same filler, so a clone
would never be detected. The requirement is to reject generic/placeholder identifiers,
not just the literal strings that were seen first.
"""

import tempfile
import unittest
from pathlib import Path

from app.common.hardware_identity import (
    DIFFERENT,
    HardwareIdentityProvider,
    SAME,
    compare_hardware,
    normalize_identifier,
)

SERIAL_KINDS = ("dmi_board_serial", "dmi_product_serial", "dmi_chassis_serial")

# Spacing / punctuation / case variants of fillers the implementation already treats as placeholders.
SPELLING_VARIANTS = (
    "ChassisSerialNumber",
    "BoardSerialNumber",
    "BaseBoardSerialNumber",
    "SystemSerialNumber",
    "SERIALNUMBER",
    "NotApplicable",
    "Not Specified.",
    "Not-Available",
    "(null)",
    "<null>",
)

# Other generic fillers seen in the field.
GENERIC_FILLERS = ("Not Supplied", "Unspecified")

REAL_SERIALS = ("BSN12345678", "PF2K4X9M", "M80-1B2C3D4E", "1A2B3C4D5E6F")


def collect(root: Path, name: str, value: str):
    directory = root / "sys" / "class" / "dmi" / "id"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(value + "\n", encoding="utf-8")
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64").collect()


class PlaceholderSpellingVariants(unittest.TestCase):
    def test_spelling_variants_of_known_fillers_are_rejected(self) -> None:
        accepted = [
            (kind, value) for value in SPELLING_VARIANTS for kind in SERIAL_KINDS if normalize_identifier(kind, value) is not None
        ]
        self.assertEqual(accepted, [], msg="treated as unique hardware serials: %r" % accepted)

    def test_generic_filler_words_are_rejected(self) -> None:
        accepted = [value for value in GENERIC_FILLERS if normalize_identifier("dmi_board_serial", value) is not None]
        self.assertEqual(accepted, [])

    def test_two_different_machines_with_a_variant_filler_are_never_the_same_machine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for value in SPELLING_VARIANTS + GENERIC_FILLERS:
                one = collect(Path(directory) / "one", "chassis_serial", value)
                two = collect(Path(directory) / "two", "chassis_serial", value)
                self.assertEqual(one.components, {}, msg=value)
                self.assertNotEqual(compare_hardware(one, two), SAME, msg=value)

    def test_genuine_serials_are_still_accepted_and_distinguish_machines(self) -> None:
        for value in REAL_SERIALS:
            self.assertIsNotNone(normalize_identifier("dmi_board_serial", value), msg=value)
        with tempfile.TemporaryDirectory() as directory:
            one = collect(Path(directory) / "one", "board_serial", "BSN12345678")
            two = collect(Path(directory) / "two", "board_serial", "PF2K4X9M")
        self.assertEqual(compare_hardware(one, two), DIFFERENT)


if __name__ == "__main__":
    unittest.main()
