"""Adversarial acceptance for issue #170: mock database paths injected into stores.

Issue #170: ``open_database`` and ``database_path_for_legacy_file`` accepted a MagicMock as a
SQLite path, so ``sqlite3.connect(str(mock))`` created mock-named files in the cwd.

Expected behavior, derived from the issue before reading the patch:

- A store handed a mock database file must fail when it touches SQLite. It must not use
  the mock's repr as a filename and must not create files in the cwd.
- Construction alone does not touch SQLite, so it must not raise or create files.
- Real paths through the same public store methods keep working (positive control).

The existing #170 suites exercise ``open_database``, the legacy helper and the credential
store's *derived* path. These tests cover the injection points the issue's callers use:
``SessionStore(state_database_file)`` and ``DroneCredentialStore(state_database_file=...)``.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.auth import DroneCredentialStore, SessionStore


REPO_ROOT = Path(__file__).resolve().parents[2]
PATH_REJECTION = "database path must be a filesystem path"

INJECTED_MOCK_FLAVOURS = {
    "MagicMock()": lambda: mock.MagicMock(),
    "Mock()": lambda: mock.Mock(),
    "MagicMock(spec=str)": lambda: mock.MagicMock(spec=str),
    "create_autospec(Path, instance=True)": lambda: mock.create_autospec(Path, instance=True),
}


def _mock_named(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return [p.name for p in directory.iterdir() if "Mock" in p.name or p.name.startswith("<")]


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

    def _assert_no_pollution(self, targets: list[str]) -> None:
        self.assertEqual(targets, [], msg=f"sqlite3.connect was called with {targets!r}")
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])
        self.assertEqual(_mock_named(REPO_ROOT), [])


class InjectedSessionStoreMockTests(_CwdSandbox):
    def test_every_session_operation_rejects_injected_mock_before_sqlite(self) -> None:
        for label, factory in INJECTED_MOCK_FLAVOURS.items():
            with self.subTest(database=label):
                store = SessionStore(factory())
                with self._spy_sqlite_connect() as targets:
                    operations = [
                        lambda: store.create("admin"),
                        lambda: store.validate("token"),
                        lambda: store.revoke("token"),
                        lambda: store.revoke_all(),
                    ]
                    for operation in operations:
                        with self.assertRaisesRegex(TypeError, PATH_REJECTION):
                            operation()
                self._assert_no_pollution(targets)

    def test_constructing_session_store_with_mock_does_not_touch_sqlite(self) -> None:
        with self._spy_sqlite_connect() as targets:
            SessionStore(mock.MagicMock())
        self._assert_no_pollution(targets)


class InjectedCredentialStoreMockTests(_CwdSandbox):
    def test_credential_store_rejects_injected_mock_state_db_on_every_sqlite_method(self) -> None:
        # MagicMock(spec=str) is excluded: it is falsy, so DroneCredentialStore's
        # ``state_database_file or <derived path>`` replaces it before any guard runs.
        # test_falsy_spec_str_mock_never_reaches_sqlite covers that flavour.
        for label, factory in INJECTED_MOCK_FLAVOURS.items():
            if label == "MagicMock(spec=str)":
                continue
            with self.subTest(database=label):
                store = DroneCredentialStore(self.cwd / "credentials.json", state_database_file=factory())
                with self._spy_sqlite_connect() as targets:
                    operations = [
                        lambda: store.load(),
                        lambda: store.check("admin", "correct-horse"),
                        lambda: store.update("admin", "correct-horse"),
                    ]
                    for operation in operations:
                        with self.assertRaisesRegex(TypeError, PATH_REJECTION):
                            operation()
                self._assert_no_pollution(targets)

    def test_falsy_spec_str_mock_never_reaches_sqlite(self) -> None:
        store = DroneCredentialStore(self.cwd / "credentials.json", state_database_file=mock.MagicMock(spec=str))
        with self._spy_sqlite_connect() as targets:
            store.update("admin", "correct-horse")
            self.assertTrue(store.check("admin", "correct-horse"))
        self.assertTrue(targets)
        self.assertTrue(all("Mock" not in target and "<" not in target for target in targets), msg=repr(targets))
        self.assertEqual(_mock_named(self.cwd), [])
        self.assertEqual(_mock_named(REPO_ROOT), [])


class InjectedRealPathPositiveControlTests(_CwdSandbox):
    def test_session_store_round_trips_on_a_real_database_path(self) -> None:
        database = self.cwd / "sessions" / "state.sqlite3"
        store = SessionStore(database)
        token = store.create("admin")
        self.assertEqual(store.validate(token), "admin")
        self.assertTrue(database.exists())
        self.assertEqual(_mock_named(self.cwd), [])
        self.assertEqual(_mock_named(REPO_ROOT), [])

    def test_credential_store_persists_credentials_on_a_real_database_path(self) -> None:
        database = self.cwd / "state.sqlite3"
        store = DroneCredentialStore(self.cwd / "credentials.json", state_database_file=database)
        store.update("admin", "correct-horse")
        self.assertTrue(store.check("admin", "correct-horse"))
        self.assertFalse(store.check("admin", "wrong"))
        self.assertTrue(database.exists())
        self.assertEqual(_mock_named(self.cwd), [])


if __name__ == "__main__":
    unittest.main()
