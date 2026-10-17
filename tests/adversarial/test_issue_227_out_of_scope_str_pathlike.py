"""Evidence for #227's str / PathLike guard, adjudicated against #287.

``_fake_machine_id`` calls ``_require_real_path``, which accepts ``str`` and
``os.PathLike``. The original revision of this suite pinned the follow-on defect:
``_device_id_path`` did ``userdata_root / "system"``, so a ``str`` or custom PathLike
root raised ``TypeError: unsupported operand type(s) for /``.

Adjudication (round 2, #287 dispute): that join failure is the defect #287 fixes, not
a contract to preserve. The guard acceptance assertion stays. The join-failure pin is
replaced with the #287 contract: the root persists under its own directory and
``uuid.getnode()`` is never returned. The fuller #287 coverage lives in
``test_issue_287_fake_identity_path_types.py``.
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


class _FsPath:
    def __init__(self, value: str) -> None:
        self._value = value

    def __fspath__(self) -> str:
        return self._value


class StrPathLikeEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="issue-227-str-")
        self.root = Path(self._tmpdir.name).resolve() / "userdata"
        self.root.mkdir()
        os.chdir(self.root.parent)
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._env.stop()
        self._tmpdir.cleanup()

    def test_require_real_path_accepts_str_and_pathlike_and_helper_persists_under_each(self) -> None:
        _require_real_path(str(self.root), "userdata_root")
        _require_real_path(_FsPath(str(self.root)), "userdata_root")
        persisted = self.root / "system" / "drone-app" / "device-id"
        with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE) as getnode:
            for supplied in (str(self.root), _FsPath(str(self.root))):
                with self.subTest(root_type=type(supplied).__name__):
                    value = _fake_machine_id(supplied)
                    self.assertNotEqual(value, RUNTIME_MAC)
                    self.assertEqual(persisted.read_text(encoding="utf-8").strip(), value)
        getnode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
