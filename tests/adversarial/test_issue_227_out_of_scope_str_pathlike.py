"""Out-of-scope evidence for #227: str / PathLike pass the guard then fail on ``/``.

``_fake_machine_id`` now calls ``_require_real_path``, which accepts ``str`` and
``os.PathLike``. ``_device_id_path`` then does ``userdata_root / "system"``,
which is a Path operation, so a ``str`` root raises
``TypeError: unsupported operand type(s) for /: 'str' and 'str'`` and a custom
PathLike raises the same against its type. Neither call returns getnode.

Why outside scope: #227 is the missing-root getnode leak. ``Settings.from_env``
always passes a ``Path``. ``_machine_id`` already required Path operations
before this change; the new guard documents a broader contract than the
join can honour. These assertions pin that mismatch as evidence.
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

    def test_require_real_path_accepts_str_and_pathlike_but_helper_cannot_join_them(self) -> None:
        _require_real_path(str(self.root), "userdata_root")
        _require_real_path(_FsPath(str(self.root)), "userdata_root")
        with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE) as getnode:
            with self.assertRaisesRegex(TypeError, "unsupported operand type"):
                _fake_machine_id(str(self.root))
            with self.assertRaisesRegex(TypeError, "unsupported operand type"):
                _fake_machine_id(_FsPath(str(self.root)))
        getnode.assert_not_called()
        self.assertFalse((self.root / "system" / "drone-app" / "device-id").exists())
        # The TypeError is a join failure, not the filesystem-path guard, and not getnode.
        with mock.patch.object(device_identity.uuid, "getnode", return_value=GETNODE):
            try:
                value = _fake_machine_id(str(self.root))
            except TypeError as exc:
                self.assertNotEqual(str(exc), RUNTIME_MAC)
                self.assertIn("unsupported operand type", str(exc))
            else:
                self.fail(f"str root returned {value!r} instead of failing the Path join")


if __name__ == "__main__":
    unittest.main()
