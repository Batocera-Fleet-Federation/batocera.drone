"""Issue #176 boundaries: first-boot logical ids must not be the host NIC.

Expected, derived from the issue and docs/drone-identity.md before relying on the
patch:

* A fresh ``Settings`` / ``ensure_identity`` install without ``DRONE_DEVICE_ID``
  mints a random, locally administered, MAC-shaped id, persists it, and never
  adopts the NIC MAC (or ``uuid.getnode()``) when that persist succeeds.
* Two userdata trees on one host therefore get distinct ids even when they share
  hardware evidence and the same NIC candidate list.
* An installation that already persisted an id — including a legacy NIC MAC —
  keeps it; ``DRONE_DEVICE_ID`` / ``OVERMIND_DEVICE_ID`` still pin.
* If the id cannot be persisted, the NIC-derived fallback is used so the value
  does not change every start.
* Malformed persisted files and invalid env pins are not device ids; they fall
  through to first-boot generation.
* Losing the device-id file on an already-bound install restores the binding's
  id rather than minting a new one or falling back to the NIC.
* A clone still gets a new logical id even when the copied file is the NIC MAC.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.device_identity import (
    _device_id_path,
    _machine_id,
    _normalize_device_id,
    _read_persisted_machine_id,
    _write_persisted_machine_id,
    random_device_id,
)
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.identity_provisioning import State
from app.common.settings import Settings


HOST_MAC = "5e:98:ab:b9:e3:8b"
RUNTIME_MAC = "2c:cf:67:97:8c:8f"
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"
OPENSSL_AVAILABLE = shutil.which("openssl") is not None


def write_hardware(root: Path, product_uuid: str, serial: str) -> Path:
    directory = root / "sys" / "class" / "dmi" / "id"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "product_uuid").write_text(product_uuid + "\n", encoding="utf-8")
    (directory / "board_serial").write_text(serial + "\n", encoding="utf-8")
    return root


def settings_for(root: Path, **extra: str) -> Settings:
    env = {"USERDATA_ROOT": str(root), "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"), **extra}
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def provider_for(root: Path) -> HardwareIdentityProvider:
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def read_binding(settings: Settings) -> dict:
    return json.loads(identity._binding_path(settings).read_text(encoding="utf-8"))


def persist_path(root: Path) -> Path:
    return root / "system" / "drone-app" / "device-id"


class RandomDeviceIdTests(unittest.TestCase):
    def test_ids_are_locally_administered_unicast_and_not_the_nic(self) -> None:
        seen = set()
        for _ in range(64):
            value = random_device_id()
            self.assertRegex(value, MAC_SHAPED)
            self.assertEqual(int(value.split(":")[0], 16) & 0x03, 0x02)
            self.assertNotEqual(value, HOST_MAC)
            self.assertNotEqual(value, RUNTIME_MAC)
            seen.add(value)
        self.assertEqual(len(seen), 64)

    def test_normalize_rejects_malformed_values(self) -> None:
        self.assertIsNone(_normalize_device_id(""))
        self.assertIsNone(_normalize_device_id("   "))
        self.assertIsNone(_normalize_device_id("ab"))
        self.assertIsNone(_normalize_device_id("aa:bb:cc:dd:ee:ff\nsecond-line"))
        self.assertIsNone(_normalize_device_id("aa:bb:cc:dd:ee:ff\x00"))
        self.assertIsNone(_normalize_device_id("aa:bb:cc:dd:ee:ff\u200b"))
        self.assertIsNone(_normalize_device_id("x" * 129))
        self.assertEqual(_normalize_device_id("  cabinet-1  "), "cabinet-1")
        self.assertEqual(_normalize_device_id(f"  {HOST_MAC} \n"), HOST_MAC)


class FirstBootMachineIdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-176-id-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _settings(self, root: Path, **extra: str) -> Settings:
        with mock.patch("app.common.device_identity._physical_mac_candidates", return_value=[HOST_MAC]), mock.patch(
            "app.common.device_identity._runtime_machine_id",
            return_value=RUNTIME_MAC,
        ):
            return settings_for(root, **extra)

    def assert_fresh_logical_id(self, value: str) -> None:
        self.assertRegex(value, MAC_SHAPED)
        self.assertEqual(int(value.split(":")[0], 16) & 0x03, 0x02)
        self.assertNotEqual(value, HOST_MAC)
        self.assertNotEqual(value, RUNTIME_MAC)

    def test_two_userdata_roots_on_one_nic_do_not_share_an_id(self) -> None:
        first_root, second_root = self.tmp / "a", self.tmp / "b"
        first_root.mkdir()
        second_root.mkdir()
        first = self._settings(first_root)
        second = self._settings(second_root)
        self.assert_fresh_logical_id(first.device_id)
        self.assert_fresh_logical_id(second.device_id)
        self.assertNotEqual(first.device_id, second.device_id)
        self.assertEqual(persist_path(first_root).read_text(encoding="utf-8").strip(), first.device_id)
        self.assertEqual(persist_path(second_root).read_text(encoding="utf-8").strip(), second.device_id)

    def test_legacy_persisted_nic_mac_is_kept(self) -> None:
        root = self.tmp / "legacy"
        path = persist_path(root)
        path.parent.mkdir(parents=True)
        path.write_text(HOST_MAC + "\n", encoding="utf-8")
        settings = self._settings(root)
        self.assertEqual(settings.device_id, HOST_MAC)
        self.assertEqual(path.read_text(encoding="utf-8").strip(), HOST_MAC)

    def test_crlf_persisted_id_is_kept(self) -> None:
        root = self.tmp / "crlf"
        path = persist_path(root)
        path.parent.mkdir(parents=True)
        path.write_text("cabinet-east-1\r\n", encoding="utf-8")
        self.assertEqual(self._settings(root).device_id, "cabinet-east-1")

    def test_malformed_persisted_files_are_not_adopted(self) -> None:
        cases = {
            "empty": "",
            "newline": "\n",
            "short": "ab",
            "two-lines": f"{HOST_MAC}\nextra",
            "null": f"{HOST_MAC}\x00",
            "spaces": "   ",
            "too-long": "x" * 129,
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                root = self.tmp / name
                path = persist_path(root)
                path.parent.mkdir(parents=True)
                path.write_bytes(body.encode("utf-8") if isinstance(body, str) else body)
                settings = self._settings(root)
                self.assert_fresh_logical_id(settings.device_id)
                self.assertEqual(path.read_text(encoding="utf-8").strip(), settings.device_id)

    def test_device_id_path_that_is_a_directory_falls_back_to_the_nic(self) -> None:
        root = self.tmp / "dir-collision"
        path = persist_path(root)
        path.mkdir(parents=True)
        with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True), mock.patch(
            "app.common.device_identity._physical_mac_candidates",
            return_value=[HOST_MAC],
        ), mock.patch("app.common.device_identity._runtime_machine_id", return_value=RUNTIME_MAC):
            value = _machine_id(root)
        self.assertEqual(value, HOST_MAC)

    def test_unwritable_parent_falls_back_to_the_nic(self) -> None:
        root = self.tmp / "readonly"
        parent = persist_path(root).parent
        parent.mkdir(parents=True)
        parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(lambda: parent.chmod(stat.S_IRWXU))
        with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True), mock.patch(
            "app.common.device_identity._physical_mac_candidates",
            return_value=[HOST_MAC],
        ), mock.patch("app.common.device_identity._runtime_machine_id", return_value=RUNTIME_MAC):
            try:
                value = _machine_id(root)
            except OSError:
                self.skipTest("host allowed writes to a user-readonly directory")
                return
        if os.access(parent, os.W_OK):
            self.skipTest("host allowed writes to a user-readonly directory")
        self.assertEqual(value, HOST_MAC)

    def test_write_helper_refuses_missing_parent_and_invalid_values(self) -> None:
        root = self.tmp / "missing-parent"
        root.mkdir()
        self.assertFalse(_write_persisted_machine_id(root, HOST_MAC))
        self.assertFalse(_write_persisted_machine_id(root, "ab"))
        persist_path(root).parent.mkdir(parents=True)
        self.assertTrue(_write_persisted_machine_id(root, HOST_MAC))
        self.assertEqual(_read_persisted_machine_id(root), HOST_MAC)

    def test_empty_nic_list_falls_back_to_runtime_id_when_persist_fails(self) -> None:
        root = self.tmp / "no-nic"
        root.mkdir()
        with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True), mock.patch(
            "app.common.device_identity._physical_mac_candidates",
            return_value=[],
        ), mock.patch("app.common.device_identity._write_persisted_machine_id", return_value=False), mock.patch(
            "app.common.device_identity._runtime_machine_id",
            return_value=RUNTIME_MAC,
        ):
            self.assertEqual(_machine_id(root), RUNTIME_MAC)

    def test_invalid_env_pin_falls_through_to_a_fresh_logical_id(self) -> None:
        for index, pin in enumerate(("", "ab", "id with spaces", "x" * 129)):
            with self.subTest(pin=pin):
                root = self.tmp / f"bad-pin-{index}"
                root.mkdir()
                settings = self._settings(root, DRONE_DEVICE_ID=pin)
                self.assert_fresh_logical_id(settings.device_id)

    def test_overmind_device_id_still_pins(self) -> None:
        root = self.tmp / "legacy-pin"
        root.mkdir()
        settings = self._settings(root, OVERMIND_DEVICE_ID="legacy-cabinet")
        self.assertEqual(settings.device_id, "legacy-cabinet")
        self.assertFalse(persist_path(root).exists())

    def test_drone_device_id_wins_over_a_persisted_file(self) -> None:
        root = self.tmp / "pinned"
        path = persist_path(root)
        path.parent.mkdir(parents=True)
        path.write_text(HOST_MAC + "\n", encoding="utf-8")
        settings = self._settings(root, DRONE_DEVICE_ID="pinned-drone-176")
        self.assertEqual(settings.device_id, "pinned-drone-176")
        self.assertEqual(path.read_text(encoding="utf-8").strip(), HOST_MAC)


@unittest.skipUnless(OPENSSL_AVAILABLE, "openssl is required for identity provisioning")
class FirstInstallAndCloneIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-176-prov-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hw_a = write_hardware(self.tmp / "hw-a", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1", "BOARD-A-176")
        self.hw_b = write_hardware(self.tmp / "hw-b", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2", "BOARD-B-176")

    def _settings(self, root: Path, **extra: str) -> Settings:
        with mock.patch("app.common.device_identity._physical_mac_candidates", return_value=[HOST_MAC]), mock.patch(
            "app.common.device_identity._runtime_machine_id",
            return_value=RUNTIME_MAC,
        ):
            return settings_for(root, **extra)

    def test_initial_provision_records_the_minted_id_not_the_nic(self) -> None:
        first_root, second_root = self.tmp / "install-1", self.tmp / "install-2"
        first_root.mkdir()
        second_root.mkdir()
        first = self._settings(first_root)
        second = self._settings(second_root)
        first_result = identity.ensure_identity(first, provider_for(self.hw_a))
        second_result = identity.ensure_identity(second, provider_for(self.hw_a))
        self.assertEqual(first_result.state, State.READY)
        self.assertEqual(second_result.state, State.READY)
        self.assertNotEqual(first.device_id, HOST_MAC)
        self.assertNotEqual(second.device_id, HOST_MAC)
        self.assertNotEqual(first.device_id, second.device_id)
        self.assertEqual(read_binding(first)["device_id"], first.device_id)
        self.assertEqual(read_binding(second)["device_id"], second.device_id)
        self.assertEqual(_device_id_path(first_root).read_text(encoding="utf-8").strip(), first.device_id)

    def test_lost_device_id_file_is_restored_from_the_binding(self) -> None:
        root = self.tmp / "bound"
        root.mkdir()
        settings = self._settings(root)
        result = identity.ensure_identity(settings, provider_for(self.hw_a))
        self.assertEqual(result.state, State.READY)
        original = settings.device_id
        persist_path(root).unlink()
        again = self._settings(root)
        self.assertNotEqual(again.device_id, original)
        restored = identity.ensure_identity(again, provider_for(self.hw_a))
        self.assertEqual(restored.settings.device_id, original)
        self.assertEqual(persist_path(root).read_text(encoding="utf-8").strip(), original)
        self.assertEqual(read_binding(settings)["device_id"], original)

    def test_clone_of_a_nic_mac_install_gets_a_new_logical_id(self) -> None:
        source_root = self.tmp / "source"
        source_root.mkdir()
        path = persist_path(source_root)
        path.parent.mkdir(parents=True)
        path.write_text(HOST_MAC + "\n", encoding="utf-8")
        source = self._settings(source_root)
        self.assertEqual(source.device_id, HOST_MAC)
        installed = identity.ensure_identity(source, provider_for(self.hw_a))
        self.assertEqual(installed.state, State.READY)
        self.assertEqual(read_binding(source)["device_id"], HOST_MAC)

        clone_root = self.tmp / "clone"
        shutil.copytree(source_root, clone_root)
        clone = self._settings(clone_root)
        result = identity.ensure_identity(clone, provider_for(self.hw_b))
        self.assertEqual(result.status, identity.STATUS_CLONE)
        self.assertNotEqual(result.settings.device_id, HOST_MAC)
        self.assertNotEqual(result.settings.device_id, RUNTIME_MAC)
        self.assertRegex(result.settings.device_id, MAC_SHAPED)
        self.assertEqual(int(result.settings.device_id.split(":")[0], 16) & 0x03, 0x02)
        self.assertEqual(read_binding(result.settings)["device_id"], result.settings.device_id)
        self.assertEqual(persist_path(clone_root).read_text(encoding="utf-8").strip(), result.settings.device_id)
        self.assertEqual(read_binding(source)["device_id"], HOST_MAC)


if __name__ == "__main__":
    unittest.main()
