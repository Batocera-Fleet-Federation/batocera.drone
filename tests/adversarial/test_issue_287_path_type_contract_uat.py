"""Issue #287 adversarial UAT: boundaries of the str / PathLike root contract.

Expected behavior comes from the issue and the identity invariants, not from the implementation:
every root type ``_require_real_path`` accepts must either persist identity under its own absolute
root with ``uuid.getnode()`` untouched, or fail closed with ``TypeError`` before any write. A root
that is not really a filesystem path (non-str ``__fspath__`` result, whitespace-prefixed text) must
never reach a join, a NIC fallback, or the cwd.
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.common import device_identity  # noqa: E402

HOST_NIC = 0x0242AC110002
HOST_NIC_TEXT = "02:42:ac:11:00:02"
MAC_SHAPE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class _FsPath:
    def __init__(self, value):
        self._value = value

    def __fspath__(self):
        return self._value


def _snapshot(root: Path):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


class PathTypeContractUatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="issue-287-contract-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.cwd = self.base / "cwd"
        self.cwd.mkdir()
        previous = Path.cwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DRONE_DEVICE_ID_FILE", None)
        os.environ.pop("DRONE_DEVICE_ID", None)

    def _root(self, name):
        root = self.base / name
        (root / "system" / "drone-app").mkdir(parents=True)
        return root

    def test_pure_posix_path_root_is_a_real_path_and_persists_under_itself(self):
        root = self._root("pure-posix")
        with mock.patch.object(device_identity.uuid, "getnode", return_value=HOST_NIC) as getnode:
            value = device_identity._fake_machine_id(PurePosixPath(root))
        getnode.assert_not_called()
        self.assertRegex(value, MAC_SHAPE)
        self.assertNotEqual(value, HOST_NIC_TEXT)
        persisted = root / "system" / "drone-app" / "device-id"
        self.assertEqual(persisted.read_text(encoding="utf-8").strip(), value)
        self.assertEqual(_snapshot(self.cwd), [])

    def test_non_str_fspath_result_fails_closed_before_identity_or_writes(self):
        root = self._root("int-fspath")
        before = _snapshot(self.base)
        with mock.patch.object(device_identity.uuid, "getnode", return_value=HOST_NIC) as getnode:
            with self.assertRaises(TypeError):
                device_identity._fake_machine_id(_FsPath(7))
        getnode.assert_not_called()
        self.assertEqual(_snapshot(self.base), before)
        self.assertFalse((root / "system" / "drone-app" / "device-id").exists())

    def test_leading_space_absolute_text_is_not_an_absolute_root(self):
        root = self._root("spaced")
        before = _snapshot(self.base)
        with mock.patch.object(device_identity.uuid, "getnode", return_value=HOST_NIC) as getnode:
            for hostile in (f" {root}", f"\t{root}"):
                with self.subTest(root=hostile):
                    with self.assertRaises(TypeError):
                        device_identity._fake_machine_id(hostile)
        getnode.assert_not_called()
        self.assertEqual(_snapshot(self.base), before)

    def test_str_root_identity_is_stable_across_fresh_processes(self):
        root = self._root("fresh-process")
        script = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from app.common import device_identity; "
            "print(device_identity._fake_machine_id(sys.argv[2]))"
        )
        ids = []
        for _ in range(2):
            completed = subprocess.run(
                [sys.executable, "-c", script, str(REPO), str(root)],
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            ids.append(completed.stdout.strip())
        self.assertRegex(ids[0], MAC_SHAPE)
        self.assertNotEqual(ids[0], HOST_NIC_TEXT)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(
            (root / "system" / "drone-app" / "device-id").read_text(encoding="utf-8").strip(),
            ids[0],
        )
        self.assertEqual(_snapshot(self.cwd), [])


if __name__ == "__main__":
    unittest.main()
