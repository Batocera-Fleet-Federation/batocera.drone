"""Adversarial boundaries for issue #169: the spec'd-mock guard in ``state_store.database_path``.

Issue #169: ``MagicMock(spec=Path)`` passes ``isinstance(value, Path)``, so the old guard was
skipped and SQLite opened ``<cwd>/system/drone-app/rom_metadata_cache.sqlite3``.

Expected behavior, derived from the issue before reading the patch:

- Every mock flavour that can impersonate ``Path`` (spec, spec_set, autospec, AsyncMock,
  NonCallableMock, a MagicMock subclass) is rejected with ``TypeError`` before SQLite is
  touched, and the working directory stays empty.
- An empty ``DRONE_STATE_DATABASE_FILE`` is falsy, so it must not bypass the guard.
- Production must not need ``unittest``: the locked-down Batocera Python may not ship it, so a
  hidden ``unittest`` module must not break real ``Path``, ``str`` or ``os.PathLike`` roots.
- The guard must not over-reject. Real roots still resolve to
  ``<root>/system/drone-app/rom_metadata_cache.sqlite3``, and the read-only cache-count reader
  fails soft to zero for a spec'd root without opening a DB under cwd.

Every rejection case runs from its own empty temporary cwd. A positive control with a real root
accompanies each store-level assertion so a silent early return cannot pass vacuously.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage import rom_metadata_store
from app.storage.state_store import DATABASE_FILENAME, database_path


MOCK_REJECTION = "userdata_root must be a filesystem path"


class _SpecSubclass(mock.MagicMock):
    """A user-defined MagicMock subclass; its type lives in this test module, not unittest.mock."""


class _PathLikeRoot:
    """A non-mock os.PathLike; it is a legitimate root and must not be over-rejected."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __fspath__(self) -> str:
        return str(self._path)


SPEC_MOCK_FLAVOURS = {
    "MagicMock(spec=Path)": lambda: mock.MagicMock(spec=Path),
    "Mock(spec=Path)": lambda: mock.Mock(spec=Path),
    "NonCallableMock(spec=Path)": lambda: mock.NonCallableMock(spec=Path),
    "NonCallableMagicMock(spec=Path)": lambda: mock.NonCallableMagicMock(spec=Path),
    "AsyncMock(spec=Path)": lambda: mock.AsyncMock(spec=Path),
    "MagicMock(spec_set=Path)": lambda: mock.MagicMock(spec_set=Path),
    "create_autospec(Path, instance=True)": lambda: mock.create_autospec(Path, instance=True),
    "create_autospec(Path) class-level": lambda: mock.create_autospec(Path),
    "MagicMock subclass with spec=Path": lambda: _SpecSubclass(spec=Path),
}


class _CwdSandbox(unittest.TestCase):
    """Run each test from an empty temporary cwd with no DB overrides; stray writes are observable."""

    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmpdir.name).resolve()
        os.chdir(self.cwd)

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._tmpdir.cleanup()
        self._env.stop()

    @contextlib.contextmanager
    def _spy_sqlite_connect(self):
        real_connect = sqlite3.connect
        targets: list[str] = []

        def spy(database, *args, **kwargs):
            targets.append(str(database))
            return real_connect(database, *args, **kwargs)

        with mock.patch("app.storage.state_store.sqlite3.connect", side_effect=spy):
            yield targets

    def _assert_cwd_untouched(self, targets: list[str]) -> None:
        for target in targets:
            self.assertNotIn(
                self.cwd,
                Path(target).resolve().parents,
                msg=f"sqlite3.connect opened {target!r} under the working directory",
            )
        self.assertFalse((self.cwd / "system").exists(), msg="cwd gained a system/ tree")
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])


class SpecMockFlavourRejectionTests(_CwdSandbox):
    def test_every_mock_flavour_is_rejected_before_sqlite_and_leaves_cwd_empty(self) -> None:
        for label, factory in SPEC_MOCK_FLAVOURS.items():
            with self.subTest(mock=label):
                with self._spy_sqlite_connect() as targets:
                    with self.assertRaisesRegex(TypeError, MOCK_REJECTION):
                        database_path(factory())
                self.assertEqual(targets, [])
                self._assert_cwd_untouched(targets)

    def test_empty_configured_database_file_does_not_bypass_the_guard(self) -> None:
        # "" is falsy: only a non-empty DRONE_STATE_DATABASE_FILE is an explicit operator choice.
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": ""}, clear=True):
            with self._spy_sqlite_connect() as targets:
                with self.assertRaisesRegex(TypeError, MOCK_REJECTION):
                    database_path(mock.MagicMock(spec=Path))
        self._assert_cwd_untouched(targets)

    def test_cache_counts_fail_soft_for_spec_root_without_opening_db_under_cwd(self) -> None:
        with self._spy_sqlite_connect() as targets:
            counts = rom_metadata_store.read_cached_asset_counts(mock.MagicMock(spec=Path))
        self.assertEqual(counts, {"roms": 0, "bios": 0, "artwork": 0})
        self._assert_cwd_untouched(targets)

    def test_real_root_positive_control_creates_db_under_root(self) -> None:
        # Same store-level call as above with a real root, so the spec'd case is not vacuous.
        root = self.cwd / "userdata"
        root.mkdir()
        with self._spy_sqlite_connect() as targets:
            rom_metadata_store.read_cached_asset_counts(root)
        expected = root / "system" / "drone-app" / DATABASE_FILENAME
        self.assertTrue(expected.exists(), msg=f"expected state DB at {expected}")
        self.assertTrue(any(Path(target).resolve() == expected.resolve() for target in targets))


class UnittestAbsentBoundaryTests(_CwdSandbox):
    """Stdlib-only deployment: the guard must degrade cleanly when unittest cannot be imported."""

    def test_real_roots_resolve_when_unittest_is_unimportable(self) -> None:
        root = self.cwd / "userdata"
        expected = (root / "system" / "drone-app" / DATABASE_FILENAME).resolve()
        with mock.patch.dict(sys.modules, {"unittest": None, "unittest.mock": None}):
            self.assertEqual(database_path(root), expected)
            self.assertEqual(database_path(str(root)), expected)
            self.assertEqual(database_path(_PathLikeRoot(root)), expected)

    def test_path_like_root_is_not_over_rejected(self) -> None:
        root = self.cwd / "pathlike"
        expected = (root / "system" / "drone-app" / DATABASE_FILENAME).resolve()
        self.assertEqual(database_path(_PathLikeRoot(root)), expected)


if __name__ == "__main__":
    unittest.main()
