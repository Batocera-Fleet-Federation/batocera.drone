"""Issue #160: inherited credentials must stay gated through uncertain startup.

These are disk-image acceptance cases. Each hardware fixture has independent DMI
values, and each Drone has isolated userdata; no real network service is started.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.storage.state_store import database_path, save_payload
from app.transfer import swarm_reset


def hardware(root: Path, uuid: str, serial: str) -> HardwareIdentityProvider:
    for name, value in (("product_uuid", uuid), ("board_serial", serial)):
        path = root / "sys" / "class" / "dmi" / "id" / name
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
class ProvisioningFailureBoundaries(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        temp = tempfile.TemporaryDirectory(prefix="issue-160-failures-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.first_hardware = hardware(
            self.root / "hardware-a",
            "c47d159a-99f8-4c1c-8c7a-3f69f2916012",
            "BOARD-A-954720",
        )
        self.second_hardware = hardware(
            self.root / "hardware-b",
            "b0626e1f-f32a-44a2-bb7e-11b7f911c51b",
            "BOARD-B-185932",
        )
        self.original = settings(self.root / "original")
        first = identity.ensure_identity(self.original, self.first_hardware)
        self.assertEqual(first.state, identity.State.READY)

    def clone(self) -> Settings:
        target = self.root / "clone"
        shutil.copytree(self.original.userdata_root, target)
        return settings(target)

    def test_valid_certificate_from_another_installation_cannot_silently_replace_bound_identity(self) -> None:
        """A binding/cert mismatch needs an explicit authorized rotation or recovery."""
        other = settings(self.root / "other")
        self.assertEqual(identity.ensure_identity(other, self.first_hardware).state, identity.State.READY)
        binding_path = identity._binding_path(self.original)
        bound = json.loads(binding_path.read_text(encoding="utf-8"))
        replacement_cert = other.drone_cert_file.read_bytes()
        replacement_key = other.drone_key_file.read_bytes()
        self.original.drone_cert_file.write_bytes(replacement_cert)
        self.original.drone_key_file.write_bytes(replacement_key)

        result = identity.ensure_identity(settings(self.original.userdata_root), self.first_hardware)

        self.assertEqual(result.state, identity.State.RECOVERY_REQUIRED)
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertEqual(json.loads(binding_path.read_text(encoding="utf-8")), bound)
        self.assertEqual(self.original.drone_cert_file.read_bytes(), replacement_cert)

    def test_unremovable_peer_pin_cannot_leave_clone_ready_or_survive_retry(self) -> None:
        """A failed pin deletion must prevent a partially trusted clone from starting."""
        pin_dir = self.original.userdata_root / "system" / "drone-app" / "local-peer-certs"
        pin_dir.mkdir(parents=True, exist_ok=True)
        (pin_dir / "old-peer.crt").write_text("inherited peer pin", encoding="utf-8")
        save_payload(database_path(self.original.userdata_root), "local_paired_peers", {
            "old-peer": {"drone_id": "old-peer", "certificate_fingerprint": "a" * 64}
        })
        clone = self.clone()
        stuck_pin = clone.userdata_root / "system" / "drone-app" / "local-peer-certs" / "old-peer.crt"
        original_unlink = Path.unlink

        def fail_one_unlink(path, *args, **kwargs):
            if path == stuck_pin:
                raise PermissionError("simulated read-only pinned certificate")
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(swarm_reset.Path, "unlink", fail_one_unlink):
            failed = identity.ensure_identity(clone, self.second_hardware)

        self.assertEqual(failed.state, identity.State.RECOVERY_REQUIRED)
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertTrue(stuck_pin.exists())

        identity.reset_runtime_state_for_tests()
        retried = identity.ensure_identity(settings(clone.userdata_root), self.second_hardware)
        self.assertEqual(retried.state, identity.State.READY)
        self.assertFalse(stuck_pin.exists())

    def test_tailscale_reset_exception_cannot_enable_a_copied_node_identity(self) -> None:
        """If local Tailscale state cannot be cleared, the clone needs a safe hold."""
        clone = self.clone()
        with mock.patch.object(identity._tailnet_module(), "reset_local_node_identity", side_effect=OSError("daemon unavailable")), \
                mock.patch.object(identity._tailnet_module(), "hold_local_node", return_value=False):
            result = identity.ensure_identity(clone, self.second_hardware)

        self.assertEqual(result.state, identity.State.RECOVERY_REQUIRED)
        self.assertFalse(identity.swarm_networking_allowed())


if __name__ == "__main__":
    unittest.main()
