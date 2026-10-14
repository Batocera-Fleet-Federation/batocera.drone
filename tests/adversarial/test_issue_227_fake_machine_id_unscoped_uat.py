"""Issue #227 UAT: unscoped ``_fake_machine_id`` must not return uuid.getnode().

Expected, from the issue reproduction and the fake-data identity invariant
(not from the patch):

* ``_fake_machine_id`` exists so fake-data processes get a persisted logical id
  scoped to a userdata tree. ``_machine_id(None)`` is the NIC /
  ``uuid.getnode()`` fallback; this helper must never take that branch.
* Calling it with no argument, with positional ``None``, or with
  ``userdata_root=None`` is an unscoped call. It must fail closed (TypeError)
  and must not return ``02:42:ac:11:00:02`` when ``uuid.getnode`` is
  ``0x0242AC110002``.
* An unscoped call must not mint ``system/drone-app/device-id`` under
  ``USERDATA_ROOT``, under the process cwd, or via a ``USERDATA_ROOT`` /
  ``DRONE_DEVICE_ID_FILE`` environment variable that was never passed in.
* The ``app.drone_api`` re-export has the same contract. A later call that
  *does* pass a real root still persists a logical id that is not getnode.
"""

from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import device_identity
from app.common.device_identity import _fake_machine_id
from app.drone_api import _fake_machine_id as drone_api_fake_machine_id


GETNODE = 0x0242AC110002
RUNTIME_MAC = "02:42:ac:11:00:02"
HOST_MAC = "5e:98:ab:b9:e3:8b"
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"


def persist_path(root: Path) -> Path:
    return root / "system" / "drone-app" / "device-id"


class UnscopedFakeMachineIdUatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="issue-227-uat-")
        self.work = Path(self._tmpdir.name).resolve()
        self.cwd = self.work / "cwd"
        self.userdata = self.work / "userdata"
        self.cwd.mkdir()
        self.userdata.mkdir()
        os.chdir(self.cwd)
        self.custom_id_file = self.userdata / "custom-device-id"
        self._env = mock.patch.dict(
            "os.environ",
            {
                "USERDATA_ROOT": str(self.userdata),
                "DRONE_DEVICE_ID_FILE": str(self.custom_id_file),
            },
            clear=True,
        )
        self._env.start()

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._env.stop()
        self._tmpdir.cleanup()

    def _assert_unscoped_failure(self, call) -> None:
        with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE) as getnode:
            with mock.patch.object(device_identity, "_runtime_machine_id", wraps=device_identity._runtime_machine_id) as runtime:
                with mock.patch.object(device_identity, "_machine_id", wraps=device_identity._machine_id) as machine:
                    with self.assertRaises(TypeError) as raised:
                        call()
        self.assertNotEqual(str(raised.exception), RUNTIME_MAC)
        getnode.assert_not_called()
        runtime.assert_not_called()
        machine.assert_not_called()
        self.assertFalse(persist_path(self.userdata).exists())
        self.assertFalse(persist_path(self.cwd).exists())
        self.assertFalse(self.custom_id_file.exists())
        self.assertEqual(sorted(path.name for path in self.cwd.iterdir()), [])

    def test_omitted_root_fails_closed_and_does_not_adopt_getnode(self) -> None:
        self._assert_unscoped_failure(_fake_machine_id)

    def test_positional_none_fails_closed_and_does_not_adopt_getnode(self) -> None:
        self._assert_unscoped_failure(lambda: _fake_machine_id(None))

    def test_keyword_none_fails_closed_and_does_not_adopt_getnode(self) -> None:
        self._assert_unscoped_failure(lambda: _fake_machine_id(userdata_root=None))

    def test_drone_api_reexport_omitted_and_none_match_the_helper(self) -> None:
        self.assertIs(drone_api_fake_machine_id, _fake_machine_id)
        self._assert_unscoped_failure(drone_api_fake_machine_id)
        self._assert_unscoped_failure(lambda: drone_api_fake_machine_id(None))

    def test_signature_has_no_userdata_root_default(self) -> None:
        parameter = inspect.signature(_fake_machine_id).parameters["userdata_root"]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        self.assertIs(
            inspect.signature(drone_api_fake_machine_id).parameters["userdata_root"].default,
            inspect.Parameter.empty,
        )

    def test_scoped_call_after_unscoped_rejection_persists_a_logical_id(self) -> None:
        self._assert_unscoped_failure(_fake_machine_id)
        root = self.work / "scoped"
        root.mkdir()
        env = {"USERDATA_ROOT": str(root)}
        with mock.patch.dict("os.environ", env, clear=True), mock.patch.object(
            device_identity.uuid, "getnode", return_value=GETNODE
        ), mock.patch(
            "app.common.device_identity._physical_mac_candidates",
            return_value=[HOST_MAC],
        ):
            first = _fake_machine_id(root)
            second = _fake_machine_id(userdata_root=root)
        self.assertRegex(first, MAC_SHAPED)
        self.assertEqual(int(first.split(":")[0], 16) & 0x03, 0x02)
        self.assertNotEqual(first, RUNTIME_MAC)
        self.assertNotEqual(first, HOST_MAC)
        self.assertEqual(first, second)
        self.assertEqual(persist_path(root).read_text(encoding="utf-8").strip(), first)
        self.assertFalse(persist_path(self.cwd).exists())
        self.assertFalse(persist_path(self.userdata).exists())
        self.assertFalse(self.custom_id_file.exists())

    def test_two_scoped_roots_do_not_share_getnode(self) -> None:
        ids = []
        with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE), mock.patch(
            "app.common.device_identity._physical_mac_candidates",
            return_value=[HOST_MAC],
        ):
            for name in ("one", "two"):
                root = self.work / name
                root.mkdir()
                with mock.patch.dict("os.environ", {"USERDATA_ROOT": str(root)}, clear=True):
                    ids.append(_fake_machine_id(root))
                self.assertEqual(persist_path(root).read_text(encoding="utf-8").strip(), ids[-1])
        self.assertNotEqual(ids[0], ids[1])
        self.assertNotIn(RUNTIME_MAC, ids)
        self.assertNotIn(HOST_MAC, ids)


if __name__ == "__main__":
    unittest.main()
