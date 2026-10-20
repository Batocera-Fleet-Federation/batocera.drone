"""Acceptance boundaries for #350's resumed identity-id validation.

A provisioning journal is durable recovery input.  A field explicitly present in
that journal is an attempted identity resume, not an optional settings value:
only JSON ``null`` and an omitted field may produce a new logical device id.
Malformed present values must fail closed before changing the persisted id.
"""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity


class ResumedDeviceIdUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-350-uat-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "userdata"
        self.settings = types.SimpleNamespace(
            userdata_root=str(self.root), device_id="AA:BB:CC:DD:EE:FF"
        )
        self.device_id_file = identity._device_id_path(self.root)

    def _seed_prior_id(self) -> None:
        self.device_id_file.parent.mkdir(parents=True, exist_ok=True)
        self.device_id_file.write_text("02:10:20:30:40:50\n", encoding="utf-8")

    def test_present_invalid_values_fail_closed_without_replacing_prior_identity(self) -> None:
        """Falsy JSON types must not be mistaken for a missing resume value."""
        invalid_values = (False, 0, 0.0, "", " \t", [], {}, ["02:aa:bb:cc:dd:ee"])

        for value in invalid_values:
            with self.subTest(value=repr(value)):
                self._seed_prior_id()
                with mock.patch.object(identity, "random_device_id") as mint:
                    with self.assertRaisesRegex(identity.IdentityError, "journaled device id"):
                        identity._choose_device_id(self.settings, {"new_device_id": value})
                mint.assert_not_called()
                self.assertEqual(
                    self.device_id_file.read_text(encoding="utf-8"),
                    "02:10:20:30:40:50\n",
                    "a rejected resume must not overwrite the existing logical identity",
                )

    def test_valid_resumed_id_is_normalized_and_never_mints_or_writes(self) -> None:
        """A valid resume is returned, rather than treated as a request to rotate."""
        self._seed_prior_id()

        with mock.patch.object(identity, "random_device_id") as mint:
            chosen = identity._choose_device_id(
                self.settings, {"new_device_id": " 02:aa:bb:cc:dd:ee "}
            )

        self.assertEqual(chosen, "02:aa:bb:cc:dd:ee")
        mint.assert_not_called()
        self.assertEqual(self.device_id_file.read_text(encoding="utf-8"), "02:10:20:30:40:50\n")

    def test_only_absent_or_json_null_take_the_fresh_id_path(self) -> None:
        """The two intentionally empty resume states retain the legacy mint behavior."""
        for journal, candidate in (({}, "02:aa:bb:cc:dd:01"), ({"new_device_id": None}, "02:aa:bb:cc:dd:02")):
            with self.subTest(journal=journal):
                self._seed_prior_id()
                with mock.patch.object(identity, "random_device_id", return_value=candidate) as mint:
                    chosen = identity._choose_device_id(self.settings, journal)
                self.assertEqual(chosen, candidate)
                mint.assert_called_once_with()
                self.assertEqual(self.device_id_file.read_text(encoding="utf-8").strip(), candidate)


if __name__ == "__main__":
    unittest.main()
