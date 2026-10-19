"""Additional UAT coverage for Issue #359's resumed-journal value boundary.

The resume journal is durable state.  Its actual string payload, including the
normalizer's ordinary whitespace handling, must determine the resumed identity.
A str subclass is permitted at this helper boundary, but none of its overridable
hooks may participate in that decision.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity


RAW_ID = "  02:11:22:33:44:55  "
CANONICAL_ID = "02:11:22:33:44:55"


class _HostileResumedId(str):
    """Every normal string hook involved in the old resume path is untrusted."""

    def __bool__(self):
        raise AssertionError("resume must not ask the subclass whether it is truthy")

    def __str__(self):
        raise AssertionError("resume must not stringify through the subclass override")

    def strip(self, *args, **kwargs):
        raise AssertionError("resume must not normalize through the subclass override")


class HostileResumedStringHooksUAT(unittest.TestCase):
    def test_journal_admission_uses_the_same_backing_text_as_resume(self) -> None:
        """A journal accepted for resumption must not invoke its ID subclass hooks."""
        journal = {
            "reason": "clone",
            "state": identity.State.CLONE_DETECTED.value,
            "steps": {},
            "started_at": "2026-10-18T00:00:00+00:00",
            "previous_device_id": "AA:BB:CC:DD:EE:FF",
            "previous_installation_id": "previous-installation",
            "previous_certificate_fingerprint": "",
            "hardware": identity.HardwareSnapshot(
                components={"dmi_product_uuid": "a" * 64}
            ).to_dict(),
            "new_device_id": _HostileResumedId(RAW_ID),
        }

        self.assertTrue(identity._valid_journal(journal))

    def test_resumed_id_uses_backing_text_without_calling_subclass_hooks(self) -> None:
        """A hostile subclass neither changes nor blocks the payload's canonical ID."""
        with tempfile.TemporaryDirectory(prefix="issue-359-hostile-hooks-") as tmp:
            userdata = Path(tmp) / "userdata"
            settings = SimpleNamespace(
                userdata_root=userdata, device_id="AA:BB:CC:DD:EE:FF"
            )
            with mock.patch.object(identity, "random_device_id") as mint:
                chosen = identity._choose_device_id(
                    settings, {"new_device_id": _HostileResumedId(RAW_ID)}
                )

            self.assertEqual(chosen, CANONICAL_ID)
            mint.assert_not_called()
            self.assertFalse(
                (userdata / "system" / "drone-app" / "device-id").exists(),
                "resuming a valid journaled id must not persist a newly minted replacement",
            )


if __name__ == "__main__":
    unittest.main()
