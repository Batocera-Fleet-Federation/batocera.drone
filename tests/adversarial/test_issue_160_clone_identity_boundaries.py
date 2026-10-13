"""Adversarial acceptance tests for Issue #160 clone identity boundaries.

These tests derive their expectations from the clone workflow: a persisted hardware
binding is the only evidence that distinguishes a restored image from its source;
damaged evidence cannot silently bless inherited credentials, and a clone is a new
logical Drone even when its copied configuration includes a fixed device id.
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
from app.common.identity_provisioning import State
from app.common.settings import Settings


OPENSSL_AVAILABLE = shutil.which("openssl") is not None


def write_hardware(root: Path, uuid: str, serial: str) -> Path:
    files = {
        "sys/class/dmi/id/product_uuid": uuid,
        "sys/class/dmi/id/board_serial": serial,
        "sys/class/dmi/id/sys_vendor": "Example Hardware Inc.",
        "sys/class/dmi/id/product_name": "Drone Test Board",
    }
    for relative, value in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n", encoding="utf-8")
    return root


def settings_for(root: Path, **extra: str) -> Settings:
    values = {
        "USERDATA_ROOT": str(root),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
        **extra,
    }
    with mock.patch.dict(os.environ, values, clear=True):
        return Settings.from_env()


def provider_for(root: Path) -> HardwareIdentityProvider:
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def certificate_fingerprint(settings: Settings) -> str:
    pem = settings.drone_cert_file.read_text(encoding="utf-8")
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


@unittest.skipUnless(OPENSSL_AVAILABLE, "openssl is required for identity provisioning")
class CloneIdentityBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.temp = tempfile.TemporaryDirectory(prefix="issue-160-adversarial-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_userdata = self.root / "source"
        self.clone_userdata = self.root / "clone"
        self.source_userdata.mkdir()
        self.hw_source = write_hardware(
            self.root / "hardware-source",
            "6c1f9a52-83d4-4e0b-9a7e-5b2d1c3f4a60",
            "BOARD-SOURCE-1234",
        )
        self.hw_clone = write_hardware(
            self.root / "hardware-clone",
            "0b7e2d91-5c3a-47f8-8d1e-9a4c6b2f7e13",
            "BOARD-CLONE-5678",
        )
        self.addCleanup(identity.reset_runtime_state_for_tests)

    def _install_source(self, **environment: str) -> Settings:
        settings = settings_for(self.source_userdata, **environment)
        with mock.patch.dict(os.environ, environment, clear=False):
            result = identity.ensure_identity(settings, provider_for(self.hw_source))
        self.assertEqual(result.state, State.READY)
        return result.settings

    def _copy_userdata(self) -> None:
        shutil.copytree(self.source_userdata, self.clone_userdata, symlinks=True)

    def test_clone_gets_a_new_logical_id_even_when_a_fixed_id_is_copied(self) -> None:
        """The issue requires a fresh logical Drone identifier on every clone."""
        pinned_id = "02:11:22:33:44:55"
        source = self._install_source(DRONE_DEVICE_ID=pinned_id)
        source_fingerprint = certificate_fingerprint(source)
        self._copy_userdata()

        clone = settings_for(self.clone_userdata, DRONE_DEVICE_ID=pinned_id)
        with mock.patch.dict(os.environ, {"DRONE_DEVICE_ID": pinned_id}, clear=False):
            result = identity.ensure_identity(clone, provider_for(self.hw_clone))

        self.assertEqual(result.status, identity.STATUS_CLONE)
        self.assertNotEqual(result.settings.device_id, source.device_id)
        self.assertNotEqual(certificate_fingerprint(result.settings), source_fingerprint)

    def test_damaged_hardware_binding_holds_inherited_identity_for_recovery(self) -> None:
        """Corrupt/erased evidence on an established install is not a new baseline."""
        source = self._install_source()
        inherited_certificate = source.drone_cert_file.read_bytes()
        binding_path = identity._binding_path(source)
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
        binding["hardware"] = {"version": 1, "components": {}, "metadata": {}}
        binding["hardware_fingerprint"] = None
        binding_path.write_text(json.dumps(binding), encoding="utf-8")
        damaged_binding = binding_path.read_bytes()

        result = identity.ensure_identity(settings_for(self.source_userdata), provider_for(self.hw_clone))

        self.assertEqual(result.state, State.RECOVERY_REQUIRED)
        self.assertFalse(result.swarm_networking_enabled)
        self.assertFalse(identity.swarm_networking_allowed())
        self.assertEqual(source.drone_cert_file.read_bytes(), inherited_certificate)
        self.assertEqual(binding_path.read_bytes(), damaged_binding)


if __name__ == "__main__":
    unittest.main()
