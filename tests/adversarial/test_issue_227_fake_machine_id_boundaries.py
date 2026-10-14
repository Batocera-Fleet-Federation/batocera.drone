"""Boundary, malformed, and error-path checks for issue #227.

Expected, from the unscoped-helper contract (not from the current branch):

* Any value that is not a filesystem path is the same class of bug as
  ``userdata_root=None``: a future caller omitted a real root. ``_fake_machine_id``
  must raise ``TypeError`` and must not return ``uuid.getnode()``.
* Unittest mocks, including ``spec=Path`` masquerades, are not roots.
* Falsy non-paths (``False``, ``0``) must not sneak through to
  ``_machine_id(None)``.
* Extra positional or keyword arguments fail at the call binding and still
  must not adopt getnode.
* A blank ``str`` is not a userdata tree; it must not return getnode.
* After a rejected call, a real ``Path`` still mints and persists a logical id
  under that tree, including when the persist directory does not exist yet.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity
from app.common.device_identity import _fake_machine_id, _require_real_path


GETNODE = 0x0242AC110002
RUNTIME_MAC = "02:42:ac:11:00:02"
HOST_MAC = "5e:98:ab:b9:e3:8b"
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"

NON_PATH_ROOTS = (
    ("None", None),
    ("False", False),
    ("0", 0),
    ("object", object()),
    ("bytes", b"/userdata"),
    ("list", ["/userdata"]),
    ("dict", {"userdata_root": "/userdata"}),
)


def persist_path(root: Path) -> Path:
    return root / "system" / "drone-app" / "device-id"


class FakeMachineIdBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="issue-227-bound-")
        self.work = Path(self._tmpdir.name).resolve()
        self.cwd = self.work / "cwd"
        self.cwd.mkdir()
        os.chdir(self.cwd)
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._env.stop()
        self._tmpdir.cleanup()

    def _assert_rejects_without_host_id(self, root) -> None:
        with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE) as getnode:
            with mock.patch.object(device_identity, "_runtime_machine_id", wraps=device_identity._runtime_machine_id) as runtime:
                with self.assertRaises(TypeError):
                    _fake_machine_id(root)
        getnode.assert_not_called()
        runtime.assert_not_called()
        self.assertFalse(persist_path(self.cwd).exists())
        self.assertEqual(sorted(path.name for path in self.cwd.iterdir()), [])

    def test_non_path_roots_are_typeerror_not_getnode(self) -> None:
        for label, value in NON_PATH_ROOTS:
            with self.subTest(root=label):
                self._assert_rejects_without_host_id(value)

    def test_unittest_mocks_are_rejected_before_pathlib(self) -> None:
        for label, value in (
            ("MagicMock", mock.MagicMock()),
            ("spec Path", mock.MagicMock(spec=Path)),
            ("autospec Path", mock.create_autospec(Path, instance=True)),
        ):
            with self.subTest(root=label):
                self._assert_rejects_without_host_id(value)

    def test_blank_strings_do_not_return_getnode(self) -> None:
        for blank in ("", " ", "\t", "\n"):
            with self.subTest(root=repr(blank)):
                with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE) as getnode:
                    with self.assertRaises(TypeError):
                        _fake_machine_id(blank)
                getnode.assert_not_called()
                self.assertFalse(persist_path(self.cwd).exists())

    def test_extra_arguments_fail_at_binding_not_via_getnode(self) -> None:
        root = self.work / "extra"
        root.mkdir()
        with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE) as getnode:
            with self.assertRaises(TypeError):
                _fake_machine_id(root, None)  # type: ignore[misc]
            with self.assertRaises(TypeError):
                _fake_machine_id(root, userdata_root=root)  # type: ignore[misc]
            with self.assertRaises(TypeError):
                _fake_machine_id(unexpected=root)  # type: ignore[call-arg]
        getnode.assert_not_called()
        self.assertFalse(persist_path(root).exists())

    def test_require_real_path_rejects_none_with_the_filesystem_message(self) -> None:
        with self.assertRaisesRegex(TypeError, "userdata_root must be a filesystem path"):
            _require_real_path(None, "userdata_root")
        with self.assertRaisesRegex(TypeError, "userdata_root must be a filesystem path"):
            _fake_machine_id(None)

    def test_missing_parent_tree_is_created_and_is_not_getnode(self) -> None:
        root = self.work / "fresh" / "nested"
        self.assertFalse(root.exists())
        with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE), mock.patch(
            "app.common.device_identity._physical_mac_candidates",
            return_value=[HOST_MAC],
        ):
            value = _fake_machine_id(root)
        self.assertRegex(value, MAC_SHAPED)
        self.assertEqual(int(value.split(":")[0], 16) & 0x03, 0x02)
        self.assertNotEqual(value, RUNTIME_MAC)
        self.assertNotEqual(value, HOST_MAC)
        self.assertEqual(persist_path(root).read_text(encoding="utf-8").strip(), value)

    def test_existing_persisted_id_is_kept_even_when_getnode_is_the_docker_mac(self) -> None:
        root = self.work / "legacy"
        path = persist_path(root)
        path.parent.mkdir(parents=True)
        path.write_text("cabinet-east-227\n", encoding="utf-8")
        with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE):
            self.assertEqual(_fake_machine_id(root), "cabinet-east-227")
        self.assertEqual(path.read_text(encoding="utf-8").strip(), "cabinet-east-227")

    def test_malformed_persisted_file_is_replaced_with_a_logical_id(self) -> None:
        root = self.work / "malformed"
        path = persist_path(root)
        path.parent.mkdir(parents=True)
        path.write_text("\n", encoding="utf-8")
        with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE), mock.patch(
            "app.common.device_identity._physical_mac_candidates",
            return_value=[HOST_MAC],
        ):
            value = _fake_machine_id(root)
        self.assertNotEqual(value, RUNTIME_MAC)
        self.assertNotEqual(value, HOST_MAC)
        self.assertEqual(path.read_text(encoding="utf-8").strip(), value)


if __name__ == "__main__":
    unittest.main()
