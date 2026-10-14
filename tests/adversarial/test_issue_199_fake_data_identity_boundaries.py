"""Issue #199 boundaries: USE_FAKE_DATA identity must not be uuid.getnode().

Expected, derived from #199 and docs/drone-identity.md before relying on the
patch:

* ``Settings.from_env()`` with ``USE_FAKE_DATA`` set and no ``DRONE_DEVICE_ID``
  mints a random, locally administered, MAC-shaped logical id and persists it
  under that process's ``USERDATA_ROOT`` (default
  ``system/drone-app/device-id``). It must not adopt the host NIC /
  ``uuid.getnode()`` when persist succeeds.
* Two fake-data userdata trees on one host therefore get distinct ids even
  when they share the same NIC candidate list. The mock-server spelling
  ``USE_FAKE_DATA=true`` and a missing ``DRONE_DEVICE_ID_FILE`` env (the
  ``scripts/run_mock_server.py`` path) are in scope.
* An already-persisted id — including a legacy NIC MAC — is kept.
  ``DRONE_DEVICE_ID`` / ``OVERMIND_DEVICE_ID`` still pin. Invalid env pins and
  malformed persisted files fall through to a fresh logical id, not getnode.
* If the id cannot be persisted, the NIC-derived fallback is used so the
  value does not change every start (same invariant as a real install).
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity
from app.common.device_identity import (
    _fake_machine_id,
    _machine_id,
    _runtime_machine_id,
)
from app.common.settings import Settings


HOST_MAC = "5e:98:ab:b9:e3:8b"
RUNTIME_MAC = "02:42:ac:11:00:02"
GETNODE = 0x0242AC110002
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"


def persist_path(root: Path) -> Path:
    return root / "system" / "drone-app" / "device-id"


def fake_env(root: Path, **extra: str) -> dict:
    env = {
        "USE_FAKE_DATA": "true",
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.db"),
    }
    env.update(extra)
    return env


def settings_for(root: Path, **extra: str) -> Settings:
    with mock.patch.dict("os.environ", fake_env(root, **extra), clear=True), mock.patch(
        "app.common.device_identity._physical_mac_candidates",
        return_value=[HOST_MAC],
    ), mock.patch(
        "app.common.device_identity._runtime_machine_id",
        return_value=RUNTIME_MAC,
    ), mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE):
        return Settings.from_env()


class FakeDataIdentityBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-199-fake-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def assert_fresh_logical_id(self, value: str, label: str = "device_id") -> None:
        self.assertRegex(value, MAC_SHAPED, f"{label} {value!r} is not MAC-shaped")
        self.assertEqual(
            int(value.split(":")[0], 16) & 0x03,
            0x02,
            f"{label} {value!r} is not locally administered unicast",
        )
        self.assertNotEqual(value, HOST_MAC, f"{label} adopted the NIC MAC")
        self.assertNotEqual(value, RUNTIME_MAC, f"{label} adopted uuid.getnode()")

    def test_mock_server_env_mints_persisted_id_without_device_id_file_env(self) -> None:
        """run_mock_server.py sets USE_FAKE_DATA=true and never sets DRONE_DEVICE_ID_FILE."""
        root = self.tmp / "mock-server"
        root.mkdir()
        settings = settings_for(root)
        self.assertTrue(settings.use_fake_data)
        self.assert_fresh_logical_id(settings.device_id)
        path = persist_path(root)
        self.assertTrue(path.is_file(), f"expected persist at default path {path}")
        self.assertEqual(path.read_text(encoding="utf-8").strip(), settings.device_id)

    def test_two_fake_roots_on_one_host_do_not_share_getnode(self) -> None:
        first_root, second_root = self.tmp / "one", self.tmp / "two"
        first_root.mkdir()
        second_root.mkdir()
        first = settings_for(first_root)
        second = settings_for(second_root)
        self.assert_fresh_logical_id(first.device_id, "first")
        self.assert_fresh_logical_id(second.device_id, "second")
        self.assertNotEqual(first.device_id, second.device_id)
        self.assertEqual(persist_path(first_root).read_text(encoding="utf-8").strip(), first.device_id)
        self.assertEqual(persist_path(second_root).read_text(encoding="utf-8").strip(), second.device_id)

    def test_same_root_is_stable_across_from_env_restarts(self) -> None:
        root = self.tmp / "stable"
        root.mkdir()
        first = settings_for(root).device_id
        second = settings_for(root).device_id
        self.assertEqual(first, second)
        self.assertEqual(persist_path(root).read_text(encoding="utf-8").strip(), first)

    def test_use_fake_data_truthy_spellings_all_persist_a_logical_id(self) -> None:
        for index, flag in enumerate(("1", "true", "TRUE", "yes", "on", " True ")):
            with self.subTest(flag=flag):
                root = self.tmp / f"flag-{index}"
                root.mkdir()
                settings = settings_for(root, USE_FAKE_DATA=flag)
                self.assertTrue(settings.use_fake_data, flag)
                self.assert_fresh_logical_id(settings.device_id, flag)

    def test_legacy_persisted_nic_mac_is_kept_in_fake_data(self) -> None:
        root = self.tmp / "legacy"
        path = persist_path(root)
        path.parent.mkdir(parents=True)
        path.write_text(HOST_MAC + "\n", encoding="utf-8")
        settings = settings_for(root)
        self.assertEqual(settings.device_id, HOST_MAC)
        self.assertEqual(path.read_text(encoding="utf-8").strip(), HOST_MAC)

    def test_crlf_and_padded_persisted_id_is_kept(self) -> None:
        root = self.tmp / "crlf"
        path = persist_path(root)
        path.parent.mkdir(parents=True)
        path.write_text("cabinet-east-199\r\n", encoding="utf-8")
        self.assertEqual(settings_for(root).device_id, "cabinet-east-199")

    def test_malformed_persisted_files_are_replaced_with_a_logical_id(self) -> None:
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
                path.write_bytes(body.encode("utf-8"))
                settings = settings_for(root)
                self.assert_fresh_logical_id(settings.device_id, name)
                self.assertEqual(path.read_text(encoding="utf-8").strip(), settings.device_id)

    def test_invalid_env_pin_falls_through_to_a_fresh_logical_id(self) -> None:
        for index, pin in enumerate(("", "  ", "ab", "id with spaces", "x" * 129)):
            with self.subTest(pin=pin):
                root = self.tmp / f"bad-pin-{index}"
                root.mkdir()
                settings = settings_for(root, DRONE_DEVICE_ID=pin)
                self.assert_fresh_logical_id(settings.device_id)

    def test_drone_device_id_wins_over_fake_machine_id_and_does_not_rewrite_file(self) -> None:
        root = self.tmp / "pinned"
        path = persist_path(root)
        path.parent.mkdir(parents=True)
        path.write_text(HOST_MAC + "\n", encoding="utf-8")
        settings = settings_for(root, DRONE_DEVICE_ID="pinned-fake-199")
        self.assertEqual(settings.device_id, "pinned-fake-199")
        self.assertEqual(path.read_text(encoding="utf-8").strip(), HOST_MAC)

    def test_overmind_device_id_still_pins_in_fake_data(self) -> None:
        root = self.tmp / "legacy-pin"
        root.mkdir()
        settings = settings_for(root, OVERMIND_DEVICE_ID="legacy-cabinet-199")
        self.assertEqual(settings.device_id, "legacy-cabinet-199")
        self.assertFalse(persist_path(root).exists())

    def test_drone_device_id_file_override_persists_there_not_the_default_path(self) -> None:
        root = self.tmp / "override"
        root.mkdir()
        custom = root / "alt" / "device-id"
        custom.parent.mkdir()
        settings = settings_for(root, DRONE_DEVICE_ID_FILE=str(custom))
        self.assert_fresh_logical_id(settings.device_id)
        self.assertTrue(custom.is_file())
        self.assertEqual(custom.read_text(encoding="utf-8").strip(), settings.device_id)
        self.assertFalse(persist_path(root).exists())

    def test_device_id_path_that_is_a_directory_falls_back_to_the_nic(self) -> None:
        root = self.tmp / "dir-collision"
        path = persist_path(root)
        path.mkdir(parents=True)
        with mock.patch.dict("os.environ", fake_env(root), clear=True), mock.patch(
            "app.common.device_identity._physical_mac_candidates",
            return_value=[HOST_MAC],
        ), mock.patch("app.common.device_identity._runtime_machine_id", return_value=RUNTIME_MAC):
            value = _fake_machine_id(root)
        self.assertEqual(value, HOST_MAC)

    def test_unwritable_parent_falls_back_to_the_nic(self) -> None:
        root = self.tmp / "readonly"
        parent = persist_path(root).parent
        parent.mkdir(parents=True)
        parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(lambda: parent.chmod(stat.S_IRWXU))
        with mock.patch.dict("os.environ", fake_env(root), clear=True), mock.patch(
            "app.common.device_identity._physical_mac_candidates",
            return_value=[HOST_MAC],
        ), mock.patch("app.common.device_identity._runtime_machine_id", return_value=RUNTIME_MAC):
            try:
                value = _fake_machine_id(root)
            except OSError:
                self.skipTest("host allowed writes to a user-readonly directory")
                return
        if os.access(parent, os.W_OK):
            self.skipTest("host allowed writes to a user-readonly directory")
        self.assertEqual(value, HOST_MAC)

    def test_empty_nic_list_falls_back_to_runtime_id_when_persist_fails(self) -> None:
        root = self.tmp / "no-nic"
        root.mkdir()
        with mock.patch.dict("os.environ", fake_env(root), clear=True), mock.patch(
            "app.common.device_identity._physical_mac_candidates",
            return_value=[],
        ), mock.patch(
            "app.common.device_identity._write_persisted_machine_id",
            return_value=False,
        ), mock.patch("app.common.device_identity._runtime_machine_id", return_value=RUNTIME_MAC):
            self.assertEqual(_fake_machine_id(root), RUNTIME_MAC)

    def test_fake_machine_id_without_userdata_root_is_not_the_settings_path(self) -> None:
        """Settings always passes USERDATA_ROOT; the no-root helper must not be what from_env uses."""
        root = self.tmp / "settings-passes-root"
        root.mkdir()
        with mock.patch(
            "app.common.settings._fake_machine_id",
            wraps=_fake_machine_id,
        ) as wrapped:
            settings = settings_for(root)
        wrapped.assert_called_once()
        args, kwargs = wrapped.call_args
        passed = args[0] if args else kwargs.get("userdata_root")
        self.assertEqual(Path(passed), root)
        self.assert_fresh_logical_id(settings.device_id)

    def test_real_mode_still_persists_when_use_fake_data_is_off(self) -> None:
        root = self.tmp / "real"
        root.mkdir()
        settings = settings_for(root, USE_FAKE_DATA="0")
        self.assertFalse(settings.use_fake_data)
        self.assert_fresh_logical_id(settings.device_id)
        self.assertEqual(persist_path(root).read_text(encoding="utf-8").strip(), settings.device_id)

    def test_machine_id_and_fake_machine_id_agree_for_the_same_root(self) -> None:
        root = self.tmp / "agree"
        root.mkdir()
        with mock.patch.dict("os.environ", fake_env(root), clear=True), mock.patch(
            "app.common.device_identity._physical_mac_candidates",
            return_value=[HOST_MAC],
        ), mock.patch("app.common.device_identity._runtime_machine_id", return_value=RUNTIME_MAC):
            minted = _fake_machine_id(root)
            again = _machine_id(root)
        self.assertEqual(minted, again)
        self.assert_fresh_logical_id(minted)


class HostDerivedIdInventoryTests(unittest.TestCase):
    def test_runtime_machine_id_matches_uuid_getnode_format(self) -> None:
        with mock.patch("app.common.device_identity.uuid.getnode", return_value=GETNODE):
            self.assertEqual(_runtime_machine_id(), RUNTIME_MAC)


if __name__ == "__main__":
    unittest.main()
