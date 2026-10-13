"""Adversarial boundary checks for issue #170: mock masquerades beyond ``spec=Path``.

Issue #170: ``open_database`` and ``database_path_for_legacy_file`` accepted a unittest
mock as a SQLite path. The sibling suites cover ``MagicMock()`` and ``spec=Path``
flavours; this suite targets masquerades the guard must also catch:

- ``spec=str`` mocks. ``isinstance(mock, str)`` is True, so a guard built on
  ``isinstance(value, str)`` would let them through and ``str(mock)`` would become a
  ``<MagicMock ...>`` filename.
- Non-path ``unittest.mock`` singletons (``sentinel.*``, ``mock.ANY``). They are not paths,
  so they must raise ``TypeError`` before SQLite is touched.
- A rejected mock must not poison later real calls in the same process.

Expected behavior, derived from the issue and the db-management path rule before reading
the patch: any mock is not a filesystem path, so ``open_database`` and the legacy helper
raise ``TypeError`` with no ``sqlite3.connect`` call and no mock-named file in the cwd.
A real ``Path`` in the same process still opens the database beside it.

Every rejection case runs from an empty temporary cwd.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage.state_store import (
    append_event,
    database_path_for_legacy_file,
    load_payload,
    open_database,
    save_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
REJECTION = "must be a filesystem path"


def _mock_named_files(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    names: list[str] = []
    for path in directory.iterdir():
        name = path.name
        if "MagicMock" in name or "NonCallable" in name or name.startswith("<Mock") or name.startswith("<MagicMock"):
            names.append(name)
    return names


MASQUERADE_FLAVOURS = {
    "MagicMock(spec=str)": lambda: mock.MagicMock(spec=str),
    "Mock(spec=str)": lambda: mock.Mock(spec=str),
    "NonCallableMagicMock(spec=str)": lambda: mock.NonCallableMagicMock(spec=str),
    "NonCallableMock(spec=str)": lambda: mock.NonCallableMock(spec=str),
    "create_autospec(str, instance=True)": lambda: mock.create_autospec(str, instance=True),
    "sentinel.path": lambda: mock.sentinel.path,
    "mock.ANY": lambda: mock.ANY,
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

    def _assert_clean(self, targets: list[str]) -> None:
        self.assertEqual(targets, [], msg=f"sqlite3.connect was called with {targets!r}")
        self.assertEqual(_mock_named_files(self.cwd), [])
        self.assertEqual(_mock_named_files(REPO_ROOT), [])
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])


class MasqueradeRejectionTests(_CwdSandbox):
    def test_open_database_rejects_every_masquerade_before_sqlite(self) -> None:
        for label, factory in MASQUERADE_FLAVOURS.items():
            with self.subTest(path=label):
                with self._spy_sqlite_connect() as targets:
                    with self.assertRaisesRegex(TypeError, REJECTION):
                        open_database(factory())
                self._assert_clean(targets)

    def test_legacy_helper_rejects_every_masquerade_and_returns_nothing(self) -> None:
        for label, factory in MASQUERADE_FLAVOURS.items():
            with self.subTest(path=label):
                with self._spy_sqlite_connect() as targets:
                    with self.assertRaisesRegex(TypeError, REJECTION):
                        database_path_for_legacy_file(factory())
                self._assert_clean(targets)

    def test_legacy_composition_with_open_database_never_reaches_sqlite(self) -> None:
        for label, factory in MASQUERADE_FLAVOURS.items():
            with self.subTest(path=label):
                with self._spy_sqlite_connect() as targets:
                    with self.assertRaises(TypeError):
                        open_database(database_path_for_legacy_file(factory())).close()
                self._assert_clean(targets)

    def test_payload_wrappers_reject_spec_str_mock(self) -> None:
        spec_str = mock.MagicMock(spec=str)
        with self._spy_sqlite_connect() as targets:
            with self.assertRaisesRegex(TypeError, REJECTION):
                save_payload(spec_str, "ns", {"k": 1})
            with self.assertRaisesRegex(TypeError, REJECTION):
                load_payload(spec_str, "ns", {})
            with self.assertRaisesRegex(TypeError, REJECTION):
                append_event(spec_str, "ns", {"event": 1})
        self._assert_clean(targets)

    def test_rejected_mock_does_not_poison_a_later_real_open(self) -> None:
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(TypeError):
                open_database(mock.MagicMock(spec=str))
            path = self.cwd / "after-rejection" / "state.sqlite3"
            save_payload(path, "issue_170", {"after": True})
            self.assertEqual(load_payload(path, "issue_170", {}), {"after": True})
        self.assertTrue(path.exists())
        self.assertTrue(any(Path(target).resolve() == path.resolve() for target in targets))
        self.assertEqual(_mock_named_files(self.cwd), [])
        self.assertEqual(_mock_named_files(REPO_ROOT), [])


class ConfiguredOverrideMasqueradeTests(_CwdSandbox):
    def test_configured_override_still_rejects_spec_str_at_open_database(self) -> None:
        configured = self.cwd / "configured.sqlite3"
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": str(configured)}, clear=True):
            with self._spy_sqlite_connect() as targets:
                with self.assertRaisesRegex(TypeError, REJECTION):
                    open_database(mock.MagicMock(spec=str))
        self.assertFalse(configured.exists())
        self._assert_clean(targets)

    def test_configured_override_resolves_spec_str_legacy_argument_to_real_file(self) -> None:
        configured = self.cwd / "configured.sqlite3"
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": str(configured)}, clear=True):
            resolved = database_path_for_legacy_file(mock.MagicMock(spec=str))
        self.assertEqual(resolved, configured.resolve())
        self.assertIsInstance(resolved, Path)
        self.assertEqual(resolved.name, "configured.sqlite3")
        self.assertNotIn("Mock", str(resolved))


if __name__ == "__main__":
    unittest.main()
