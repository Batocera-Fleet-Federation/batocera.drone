"""Issue #255 adversarial: journal *shape* boundaries that the non-object suites do not reach.

Expected behaviour is derived from the #255 contract (docs/drone-identity.md, "Journal type
contract"), not from the implementation. That contract says a journal that is not a usable
JSON object is "treated exactly like a torn or unreadable journal", and that on a bound
original which still matches its hardware "the journal is discarded and the identity stays
READY". So:

- a deeply nested array (a non-object that json.loads cannot decode, raising RecursionError
  rather than ValueError) must reach the same outcome as a torn journal, not escape
  ensure_identity or be recorded as a generic ``provisioning_failed``;
- a directory at the journal path (an unreadable journal) must not turn an intact bound
  original into ``provisioning_failed``, which would forbid ``keep`` and push the operator
  toward replacing a healthy identity;
- neither shape may replace the certificate or mint a logical id.

These are guard tests for the current patch. A failure is a real finding in journal handling.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings


DEEP_NESTED_ARRAY = b"[" * 200_000 + b"]" * 200_000


def hardware(root: Path) -> HardwareIdentityProvider:
    for name, value in (("product_uuid", "5a8d0c1e-2b3f-4e6a-9c7d-0f1e2d3c4b5a"),
                        ("board_serial", "BOARD-255-SHAPE-BOUNDARY")):
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


class JournalShapeBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = tempfile.TemporaryDirectory(prefix="issue-255-shape-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.provider = hardware(self.root / "hardware")
        self.drone = settings(self.root)

    def _recovery_reason(self, drone: Settings):
        marker = identity._read_json(identity._recovery_path(drone))
        return marker.get("reason") if isinstance(marker, dict) else None

    def _first_boot_outcome(self, name: str, raw: bytes | None = None, directory: bool = False):
        """Run a first boot with one damaged journal shape in its own userdata tree."""
        root = self.root / name
        provider = hardware(root / "hardware")
        drone = settings(root)
        path = identity._journal_path(drone)
        path.parent.mkdir(parents=True, exist_ok=True)
        if directory:
            path.mkdir()
        else:
            path.write_bytes(raw)
        identity.reset_runtime_state_for_tests()
        try:
            result = identity.ensure_identity(drone, provider)
        except Exception as error:  # noqa: BLE001 - the contract is "no escape", whatever the type
            self.fail(f"{name} journal escaped ensure_identity as {type(error).__name__}: {error}")
        self.assertFalse(drone.drone_cert_file.exists(), f"{name}: a certificate was minted")
        self.assertFalse(drone.drone_key_file.exists(), f"{name}: a key was minted")
        self.assertFalse(identity.swarm_networking_allowed(), f"{name}: swarm networking was not held")
        return result.state, self._recovery_reason(drone)

    def test_first_boot_deep_nested_array_matches_torn_journal_outcome(self) -> None:
        # Undecodable nesting is "treated exactly like a torn journal": same state, same reason.
        torn = self._first_boot_outcome("torn", raw=b'{"reason": "clo')
        deep = self._first_boot_outcome("deep", raw=DEEP_NESTED_ARRAY)
        self.assertEqual(deep[0], identity.State.RECOVERY_REQUIRED)
        self.assertEqual(deep, torn)

    def test_first_boot_directory_journal_matches_torn_journal_outcome(self) -> None:
        torn = self._first_boot_outcome("torn-dir-ref", raw=b'{"reason": "clo')
        directory = self._first_boot_outcome("directory", directory=True)
        self.assertEqual(directory, torn)

    def _bound_original(self, name: str):
        root = self.root / name
        provider = hardware(root / "hardware")
        drone = settings(root)
        first = identity.ensure_identity(drone, provider)
        self.assertEqual(first.state, identity.State.READY)
        return provider, drone, first

    def _assert_bound_original_kept(self, drone: Settings, result, original_id: str, cert: bytes) -> None:
        self.assertEqual(result.state, identity.State.READY,
                         f"bound original must stay READY, got {result.state} ({result.status})")
        self.assertEqual(result.settings.device_id, original_id)
        self.assertEqual(drone.drone_cert_file.read_bytes(), cert)
        self.assertNotIn(self._recovery_reason(drone), {"provisioning_failed"},
                         "a damaged journal on an intact original must not record provisioning_failed")

    def test_bound_original_with_deep_nested_array_stays_ready(self) -> None:
        provider, drone, first = self._bound_original("deep-bound")
        original_id = first.settings.device_id
        cert = drone.drone_cert_file.read_bytes()
        path = identity._journal_path(drone)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(DEEP_NESTED_ARRAY)
        try:
            result = identity.ensure_identity(drone, provider)
        except Exception as error:  # noqa: BLE001
            self.fail(f"bound original with deep nested journal escaped as {type(error).__name__}: {error}")
        self._assert_bound_original_kept(drone, result, original_id, cert)
        self.assertFalse(path.exists(), "the unusable journal must be discarded after verification")

    def test_bound_original_with_directory_journal_stays_ready(self) -> None:
        provider, drone, first = self._bound_original("directory-bound")
        original_id = first.settings.device_id
        cert = drone.drone_cert_file.read_bytes()
        path = identity._journal_path(drone)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()
        try:
            result = identity.ensure_identity(drone, provider)
        except Exception as error:  # noqa: BLE001
            self.fail(f"bound original with a directory journal escaped as {type(error).__name__}: {error}")
        self._assert_bound_original_kept(drone, result, original_id, cert)


if __name__ == "__main__":
    unittest.main()
