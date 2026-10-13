"""Adversarial UAT for issue #193: DroneCredentialStore must not replace a falsy injected DB path.

Issue #193: ``state_database_file or <derived path>`` treated a falsy injected value
(``MagicMock(spec=str)`` is ``bool`` False) as omitted and substituted the derived
legacy path, so the injected object never reached the state_store path guard.

Expected behavior, derived from the issue before reading the patch:

- Omitted or explicit ``None`` is the only absent sentinel. The store then uses
  ``database_path_for_legacy_file(credentials_path)``.
- Any other injected value is stored as the same object and is the path later
  SQLite calls use — including values that are falsy (a PathLike with ``__bool__``
  False, ``MagicMock(spec=str)``).
- Construction does not open SQLite. A mock injected path raises ``TypeError``
  (filesystem-path guard) on load/check/update, creates no mock-named files, and
  does not write the derived legacy database.
- A real injected path still persists credentials (positive control).
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
from app.storage.state_store import DATABASE_FILENAME, database_path_for_legacy_file


REPO_ROOT = Path(__file__).resolve().parents[2]
PATH_REJECTION = "database path must be a filesystem path"


class _FalsyPathLike:
    """A real filesystem path that is falsy. The ``or`` default would drop it."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __bool__(self) -> bool:
        return False

    def __fspath__(self) -> str:
        return str(self._path)


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


class CredentialStoreDefaultAndInjectionUatTests(_CwdSandbox):
    def test_omitted_state_database_file_uses_derived_legacy_path(self) -> None:
        credentials = self.cwd / "auth" / "credentials.json"
        credentials.parent.mkdir()
        store = DroneCredentialStore(credentials)
        expected = database_path_for_legacy_file(credentials)
        self.assertEqual(store.state_database_file, expected)
        self.assertEqual(expected, credentials.parent / DATABASE_FILENAME)

        payload = store.load()
        self.assertEqual(payload["username"], DroneCredentialStore.DEFAULT_USERNAME)
        self.assertEqual(payload["source"], "default")
        store.update("arcade-admin", "correct-horse")
        self.assertTrue(store.check("arcade-admin", "correct-horse"))
        self.assertTrue(expected.exists())
        self.assertEqual(_mock_named(self.cwd), [])

    def test_explicit_none_is_the_same_absent_sentinel_as_omitted(self) -> None:
        credentials = self.cwd / "credentials.json"
        omitted = DroneCredentialStore(credentials)
        explicit = DroneCredentialStore(credentials, state_database_file=None)
        self.assertEqual(omitted.state_database_file, explicit.state_database_file)
        self.assertEqual(explicit.state_database_file, database_path_for_legacy_file(credentials))

    def test_falsy_pathlike_injection_persists_to_the_injected_file_not_the_derived_path(self) -> None:
        credentials = self.cwd / "credentials.json"
        injected_path = self.cwd / "injected" / "state.sqlite3"
        injected = _FalsyPathLike(injected_path)
        self.assertFalse(injected)

        derived = database_path_for_legacy_file(credentials)
        store = DroneCredentialStore(credentials, state_database_file=injected)
        self.assertIs(store.state_database_file, injected)

        store.update("arcade-admin", "correct-horse")
        self.assertTrue(store.check("arcade-admin", "correct-horse"))
        self.assertTrue(injected_path.exists())
        self.assertFalse(derived.exists())
        self.assertEqual(_mock_named(self.cwd), [])

    def test_spec_str_mock_is_kept_and_rejected_before_sqlite(self) -> None:
        credentials = self.cwd / "credentials.json"
        injected = mock.MagicMock(spec=str)
        self.assertFalse(injected)
        derived = database_path_for_legacy_file(credentials)

        with self._spy_sqlite_connect() as targets:
            store = DroneCredentialStore(credentials, state_database_file=injected)
            self.assertIs(store.state_database_file, injected)
            self.assertEqual(targets, [])

            for operation in (
                store.load,
                lambda: store.check("arcade-admin", "correct-horse"),
                lambda: store.update("arcade-admin", "correct-horse"),
            ):
                with self.assertRaisesRegex(TypeError, PATH_REJECTION):
                    operation()

        self.assertEqual(targets, [])
        self.assertFalse(derived.exists())
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])
        self.assertEqual(_mock_named(REPO_ROOT), [])

    def test_real_injected_path_is_used_even_when_env_names_another_database(self) -> None:
        credentials = self.cwd / "credentials.json"
        injected = self.cwd / "injected.sqlite3"
        env_path = self.cwd / "from-env.sqlite3"
        derived = self.cwd / DATABASE_FILENAME
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": str(env_path)}, clear=True):
            store = DroneCredentialStore(credentials, state_database_file=injected)
            self.assertEqual(store.state_database_file, injected)
            store.update("arcade-admin", "correct-horse")
            self.assertTrue(store.check("arcade-admin", "correct-horse"))
        self.assertTrue(injected.exists())
        self.assertFalse(env_path.exists())
        self.assertFalse(derived.exists())


if __name__ == "__main__":
    unittest.main()
