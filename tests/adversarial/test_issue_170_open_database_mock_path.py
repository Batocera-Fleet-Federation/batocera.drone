"""Adversarial acceptance for issue #170: ``open_database`` must not take a mock path.

Issue #170: ``open_database(MagicMock())`` and ``save_payload(MagicMock(), 'ns', {})``
used to call ``sqlite3.connect(str(mock))``, creating ``<MagicMock id='...'>`` plus
``-shm``/``-wal`` in the working directory.

Expected behavior, from the issue and the db-management path rule, derived before
the patch:

- A unittest mock is not a filesystem path. ``open_database`` must raise ``TypeError``
  before SQLite sees it, including ``spec=Path`` and ``create_autospec(Path)`` mocks
  that satisfy ``isinstance(value, Path)`` / ``os.PathLike``.
- Wrappers that pass their first argument straight to ``open_database`` (``save_payload``,
  ``load_payload``, ``append_event``) inherit that rejection.
- ``DRONE_STATE_DATABASE_FILE`` is an override for path *helpers*, not a license for
  ``open_database`` to treat a mock as a filename.
- A real ``pathlib.Path`` still opens SQLite at that path (positive control so a silent
  early return cannot pass).

Every rejection case runs from its own empty temporary cwd so stray connect targets
and mock-named files are observable.
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

from app.storage.state_store import (
    DATABASE_FILENAME,
    append_event,
    load_payload,
    open_database,
    save_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
OPEN_REJECTION = "database path must be a filesystem path"


class _SpecSubclass(mock.MagicMock):
    """User-defined MagicMock subclass; its type lives in this test module."""


def _mock_named_files(directory: Path) -> list[str]:
    names: list[str] = []
    if not directory.exists():
        return names
    for path in directory.iterdir():
        name = path.name
        if "MagicMock" in name or name.startswith("<Mock") or name.startswith("<MagicMock"):
            names.append(name)
    return names


MOCK_PATH_FLAVOURS = {
    "MagicMock()": lambda: mock.MagicMock(),
    "Mock()": lambda: mock.Mock(),
    "NonCallableMagicMock()": lambda: mock.NonCallableMagicMock(),
    "MagicMock(spec=Path)": lambda: mock.MagicMock(spec=Path),
    "Mock(spec=Path)": lambda: mock.Mock(spec=Path),
    "NonCallableMock(spec=Path)": lambda: mock.NonCallableMock(spec=Path),
    "AsyncMock(spec=Path)": lambda: mock.AsyncMock(spec=Path),
    "MagicMock(spec_set=Path)": lambda: mock.MagicMock(spec_set=Path),
    "create_autospec(Path, instance=True)": lambda: mock.create_autospec(Path, instance=True),
    "create_autospec(Path)": lambda: mock.create_autospec(Path),
    "MagicMock subclass with spec=Path": lambda: _SpecSubclass(spec=Path),
}


class _CwdSandbox(unittest.TestCase):
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

    def _assert_no_sqlite_pollution(self, targets: list[str]) -> None:
        self.assertEqual(targets, [], msg=f"sqlite3.connect was called with {targets!r}")
        self.assertEqual(_mock_named_files(self.cwd), [])
        self.assertEqual(_mock_named_files(REPO_ROOT), [])
        self.assertFalse((self.cwd / DATABASE_FILENAME).exists())
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])


class OpenDatabaseMockRejectionTests(_CwdSandbox):
    def test_every_mock_flavour_raises_before_sqlite_and_leaves_cwd_empty(self) -> None:
        for label, factory in MOCK_PATH_FLAVOURS.items():
            with self.subTest(path=label):
                with self._spy_sqlite_connect() as targets:
                    with self.assertRaisesRegex(TypeError, OPEN_REJECTION):
                        open_database(factory())
                self._assert_no_sqlite_pollution(targets)

    def test_spec_path_mock_is_a_path_instance_and_still_rejected(self) -> None:
        # Documents the impersonation that made the original connect(str(mock)) path possible.
        spec_mock = mock.MagicMock(spec=Path)
        self.assertIsInstance(spec_mock, Path)
        self.assertIsInstance(spec_mock, os.PathLike)
        with self._spy_sqlite_connect() as targets:
            with self.assertRaisesRegex(TypeError, OPEN_REJECTION):
                open_database(spec_mock)
        self._assert_no_sqlite_pollution(targets)

    def test_plain_magicmock_is_pathlike_via_fspath_and_still_rejected(self) -> None:
        # Bare MagicMock implements __fspath__, so a PathLike-only guard is not enough.
        value = mock.MagicMock()
        self.assertIsInstance(value, os.PathLike)
        with self._spy_sqlite_connect() as targets:
            with self.assertRaisesRegex(TypeError, OPEN_REJECTION):
                open_database(value)
        self._assert_no_sqlite_pollution(targets)

    def test_magicmock_with_fspath_returning_a_real_file_is_still_rejected(self) -> None:
        real = self.cwd / "via-fspath.sqlite3"
        value = mock.MagicMock()
        value.__fspath__ = mock.Mock(return_value=str(real))
        with self._spy_sqlite_connect() as targets:
            with self.assertRaisesRegex(TypeError, OPEN_REJECTION):
                open_database(value)
        self.assertFalse(real.exists())
        self._assert_no_sqlite_pollution(targets)

    def test_configured_drone_state_database_file_does_not_let_a_mock_through(self) -> None:
        configured = self.cwd / "configured-state.sqlite3"
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": str(configured)}, clear=True):
            with self._spy_sqlite_connect() as targets:
                with self.assertRaisesRegex(TypeError, OPEN_REJECTION):
                    open_database(mock.MagicMock())
        self.assertFalse(configured.exists())
        self._assert_no_sqlite_pollution(targets)

    def test_empty_drone_state_database_file_still_rejects_mocks(self) -> None:
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": ""}, clear=True):
            with self._spy_sqlite_connect() as targets:
                with self.assertRaisesRegex(TypeError, OPEN_REJECTION):
                    open_database(mock.MagicMock(spec=Path))
        self._assert_no_sqlite_pollution(targets)


class SaveAndLoadPayloadMockTests(_CwdSandbox):
    def test_save_payload_rejects_magicmock_before_sqlite(self) -> None:
        # Reproduction from the #162 finding that became #170.
        with self._spy_sqlite_connect() as targets:
            with self.assertRaisesRegex(TypeError, OPEN_REJECTION):
                save_payload(mock.MagicMock(), "ns", {})
        self._assert_no_sqlite_pollution(targets)

    def test_save_payload_rejects_spec_path_mock(self) -> None:
        with self._spy_sqlite_connect() as targets:
            with self.assertRaisesRegex(TypeError, OPEN_REJECTION):
                save_payload(mock.MagicMock(spec=Path), "ns", {"k": 1})
        self._assert_no_sqlite_pollution(targets)

    def test_load_payload_rejects_magicmock_before_sqlite(self) -> None:
        with self._spy_sqlite_connect() as targets:
            with self.assertRaisesRegex(TypeError, OPEN_REJECTION):
                load_payload(mock.MagicMock(), "ns", {})
        self._assert_no_sqlite_pollution(targets)

    def test_append_event_rejects_magicmock_before_sqlite(self) -> None:
        with self._spy_sqlite_connect() as targets:
            with self.assertRaisesRegex(TypeError, OPEN_REJECTION):
                append_event(mock.MagicMock(), "ns", {"event": 1})
        self._assert_no_sqlite_pollution(targets)


class MalformedOpenDatabaseInputTests(_CwdSandbox):
    def test_non_path_values_raise_typeerror_and_do_not_open_sqlite(self) -> None:
        for label, value in (
            ("None", None),
            ("int", 123),
            ("list", []),
            ("dict", {}),
            ("object", object()),
            ("bytes", b"/tmp/drone.sqlite3"),
            ("True", True),
        ):
            with self.subTest(value=label):
                with self._spy_sqlite_connect() as targets:
                    with self.assertRaises(TypeError):
                        open_database(value)  # type: ignore[arg-type]
                self._assert_no_sqlite_pollution(targets)


class RealPathPositiveControlTests(_CwdSandbox):
    def test_real_path_opens_sqlite_at_that_path_and_roundtrips_payload(self) -> None:
        path = self.cwd / "nested" / "state.sqlite3"
        with self._spy_sqlite_connect() as targets:
            save_payload(path, "issue_170", {"ok": True})
            self.assertEqual(load_payload(path, "issue_170", {}), {"ok": True})
        self.assertTrue(path.exists())
        self.assertTrue(any(Path(target).resolve() == path.resolve() for target in targets))
        self.assertEqual(_mock_named_files(self.cwd), [])
        self.assertEqual(_mock_named_files(REPO_ROOT), [])

    def test_path_subclass_is_accepted(self) -> None:
        class _TaggedPath(type(Path())):
            pass

        path = _TaggedPath(self.cwd / "tagged.sqlite3")
        open_database(path).close()
        self.assertTrue(Path(path).exists())
        self.assertEqual(_mock_named_files(self.cwd), [])


class UnittestAbsentOpenDatabaseTests(_CwdSandbox):
    def test_real_path_still_opens_when_unittest_is_unimportable(self) -> None:
        path = self.cwd / "no-unittest.sqlite3"
        with mock.patch.dict(sys.modules, {"unittest": None, "unittest.mock": None}):
            open_database(path).close()
        self.assertTrue(path.exists())

    def test_prebuilt_magicmock_is_still_rejected_when_unittest_is_unimportable(self) -> None:
        value = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"unittest": None, "unittest.mock": None}):
            with self._spy_sqlite_connect() as targets:
                with self.assertRaisesRegex(TypeError, OPEN_REJECTION):
                    open_database(value)
        self._assert_no_sqlite_pollution(targets)


if __name__ == "__main__":
    unittest.main()
