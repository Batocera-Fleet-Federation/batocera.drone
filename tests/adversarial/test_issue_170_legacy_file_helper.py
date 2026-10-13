"""Adversarial acceptance for issue #170: ``database_path_for_legacy_file`` mock paths.

Issue #170: ``database_path_for_legacy_file(MagicMock())`` returned another MagicMock
(``mock.resolve().parent.__truediv__()``) with no type check. Feeding that result to
``open_database`` called ``sqlite3.connect(str(mock))`` and left mock-named SQLite
files (plus ``-shm``/``-wal``) in the working directory.

Expected behavior, from the issue, derived before the patch:

- The helper must reject a unittest mock with ``TypeError`` and must not return a mock.
- Composition ``open_database(database_path_for_legacy_file(mock))`` must not reach SQLite.
- An empty ``DRONE_STATE_DATABASE_FILE`` is falsy and must not bypass the guard.
- A non-empty ``DRONE_STATE_DATABASE_FILE`` is an explicit operator/test override and
  still resolves to that real file even if the legacy-file argument is a mock.
- A real credentials-file ``Path`` still resolves to ``<parent>/<DATABASE_FILENAME>``.
- ``DroneCredentialStore`` (the production caller) must not open SQLite when constructed
  with a mock path.

Rejection cases run from an empty temporary cwd.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.auth import DroneCredentialStore
from app.storage.state_store import (
    DATABASE_FILENAME,
    database_path_for_legacy_file,
    load_payload,
    open_database,
    save_payload,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_REJECTION = "path must be a filesystem path"
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
    "create_autospec(Path, instance=True)": lambda: mock.create_autospec(Path, instance=True),
    "create_autospec(Path)": lambda: mock.create_autospec(Path),
    "MagicMock(spec_set=Path)": lambda: mock.MagicMock(spec_set=Path),
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


class LegacyHelperMockRejectionTests(_CwdSandbox):
    def test_every_mock_flavour_raises_and_does_not_return_a_mock(self) -> None:
        for label, factory in MOCK_PATH_FLAVOURS.items():
            with self.subTest(path=label):
                with self.assertRaisesRegex(TypeError, LEGACY_REJECTION):
                    database_path_for_legacy_file(factory())
                self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])

    def test_helper_does_not_return_truediv_magicmock(self) -> None:
        # The pre-fix return was mock.resolve().parent.__truediv__() — another MagicMock.
        value = mock.MagicMock()
        with self.assertRaisesRegex(TypeError, LEGACY_REJECTION):
            database_path_for_legacy_file(value)

    def test_composition_with_open_database_never_reaches_sqlite(self) -> None:
        for label, factory in MOCK_PATH_FLAVOURS.items():
            with self.subTest(path=label):
                with self._spy_sqlite_connect() as targets:
                    with self.assertRaises(TypeError):
                        open_database(database_path_for_legacy_file(factory())).close()
                self._assert_no_sqlite_pollution(targets)

    def test_empty_configured_database_file_does_not_bypass_the_guard(self) -> None:
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": ""}, clear=True):
            with self._spy_sqlite_connect() as targets:
                with self.assertRaisesRegex(TypeError, LEGACY_REJECTION):
                    database_path_for_legacy_file(mock.MagicMock(spec=Path))
        self._assert_no_sqlite_pollution(targets)

    def test_configured_database_file_overrides_a_mock_legacy_path(self) -> None:
        configured = self.cwd / "configured-state.sqlite3"
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": str(configured)}, clear=True):
            resolved = database_path_for_legacy_file(mock.MagicMock(spec=Path))
        self.assertEqual(resolved, configured.resolve())
        self.assertIsInstance(resolved, Path)
        self.assertNotIsInstance(resolved, mock.Mock)

    def test_malformed_non_path_values_raise_typeerror(self) -> None:
        for label, value in (
            ("None", None),
            ("int", 123),
            ("list", []),
            ("object", object()),
            ("bytes", b"/tmp/credentials.json"),
        ):
            with self.subTest(value=label):
                with self.assertRaises(TypeError):
                    database_path_for_legacy_file(value)  # type: ignore[arg-type]
                self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])


class CredentialStoreMockPathTests(_CwdSandbox):
    def test_credential_store_rejects_magicmock_path_before_sqlite(self) -> None:
        with self._spy_sqlite_connect() as targets:
            with self.assertRaisesRegex(TypeError, LEGACY_REJECTION):
                DroneCredentialStore(mock.MagicMock())
        self._assert_no_sqlite_pollution(targets)

    def test_credential_store_rejects_spec_path_mock(self) -> None:
        with self._spy_sqlite_connect() as targets:
            with self.assertRaisesRegex(TypeError, LEGACY_REJECTION):
                DroneCredentialStore(mock.MagicMock(spec=Path))
        self._assert_no_sqlite_pollution(targets)

    def test_credential_store_load_with_mock_path_does_not_pollute_cwd(self) -> None:
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(TypeError):
                DroneCredentialStore(mock.create_autospec(Path, instance=True)).load()
        self._assert_no_sqlite_pollution(targets)


class RealLegacyPathPositiveControlTests(_CwdSandbox):
    def test_real_credentials_path_resolves_beside_the_file(self) -> None:
        credentials = self.cwd / "auth" / "credentials.json"
        credentials.parent.mkdir(parents=True)
        expected = (credentials.parent / DATABASE_FILENAME).resolve()
        resolved = database_path_for_legacy_file(credentials)
        self.assertEqual(resolved, expected)
        save_payload(resolved, "credentials", {"stored": True})
        self.assertEqual(load_payload(resolved, "credentials", {}), {"stored": True})
        self.assertTrue(expected.exists())
        self.assertEqual(_mock_named_files(self.cwd), [])
        self.assertFalse((self.cwd / DATABASE_FILENAME).exists())

    def test_credential_store_with_real_path_opens_db_beside_credentials(self) -> None:
        credentials = self.cwd / "credentials.json"
        with self._spy_sqlite_connect() as targets:
            store = DroneCredentialStore(credentials)
            payload = store.load()
        self.assertEqual(payload["username"], DroneCredentialStore.DEFAULT_USERNAME)
        expected = (credentials.parent / DATABASE_FILENAME).resolve()
        self.assertEqual(store.state_database_file, expected)
        self.assertTrue(any(Path(target).resolve() == expected for target in targets))
        self.assertEqual(_mock_named_files(self.cwd), [])
        self.assertEqual(_mock_named_files(REPO_ROOT), [])


if __name__ == "__main__":
    unittest.main()
