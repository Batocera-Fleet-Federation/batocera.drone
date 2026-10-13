"""Boundary, malformed, and error-path checks for issue #193.

The constructor default is ``None``, not truthiness. Every non-``None`` injection
must survive construction as the same object; SQLite methods then use that object.

Cases the ``or`` default would have swallowed:

- Every falsy unittest mock flavour (spec=str/bytes, spec_set, autospec, AsyncMock,
  NonCallableMagicMock).
- Programmer-error scalars: ``False``, ``0``, ``b""``.
- Empty string (falsy ``str``): must not be rewritten to the derived path.
- Positional fourth argument, not only the keyword.

Omitted + ``DRONE_STATE_DATABASE_FILE`` still resolves through the legacy helper
(positive control that ``None`` still means "derive").
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

FALSY_MOCK_FLAVOURS = {
    "MagicMock(spec=str)": lambda: mock.MagicMock(spec=str),
    "MagicMock(spec_set=str)": lambda: mock.MagicMock(spec_set=str),
    "MagicMock(spec=bytes)": lambda: mock.MagicMock(spec=bytes),
    "NonCallableMagicMock(spec=str)": lambda: mock.NonCallableMagicMock(spec=str),
    "create_autospec(str)": lambda: mock.create_autospec(str),
    "create_autospec(str, instance=True)": lambda: mock.create_autospec(str, instance=True),
    "AsyncMock(spec=str)": lambda: mock.AsyncMock(spec=str),
}

FALSY_NON_PATH_SCALARS = {
    "False": False,
    "0": 0,
    "empty-bytes": b"",
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

    def _credentials(self) -> Path:
        return self.cwd / "credentials.json"


class FalsyMockFlavourBoundaryTests(_CwdSandbox):
    def test_every_falsy_mock_flavour_is_kept_and_rejected_before_sqlite(self) -> None:
        credentials = self._credentials()
        derived = database_path_for_legacy_file(credentials)
        for label, factory in FALSY_MOCK_FLAVOURS.items():
            with self.subTest(injected=label):
                injected = factory()
                self.assertFalse(injected, msg=f"{label} is no longer falsy; the or-default would not swallow it")
                with self._spy_sqlite_connect() as targets:
                    store = DroneCredentialStore(credentials, state_database_file=injected)
                    self.assertIs(store.state_database_file, injected)
                    with self.assertRaisesRegex(TypeError, PATH_REJECTION):
                        store.load()
                    with self.assertRaisesRegex(TypeError, PATH_REJECTION):
                        store.check("admin", "correct-horse")
                    with self.assertRaisesRegex(TypeError, PATH_REJECTION):
                        store.update("admin", "correct-horse")
                self.assertEqual(targets, [])
                self.assertFalse(derived.exists())
                self.assertEqual(_mock_named(self.cwd), [])
                self.assertEqual(_mock_named(REPO_ROOT), [])

    def test_positional_fourth_argument_is_not_keyword_only(self) -> None:
        injected = mock.MagicMock(spec=str)
        store = DroneCredentialStore(self._credentials(), None, None, injected)
        self.assertIs(store.state_database_file, injected)
        with self.assertRaisesRegex(TypeError, PATH_REJECTION):
            store.load()


class FalsyScalarBoundaryTests(_CwdSandbox):
    def test_false_zero_and_empty_bytes_are_not_replaced_and_fail_the_path_guard(self) -> None:
        credentials = self._credentials()
        derived = database_path_for_legacy_file(credentials)
        for label, injected in FALSY_NON_PATH_SCALARS.items():
            with self.subTest(injected=label):
                self.assertFalse(injected)
                with self._spy_sqlite_connect() as targets:
                    store = DroneCredentialStore(credentials, state_database_file=injected)
                    self.assertIs(store.state_database_file, injected)
                    with self.assertRaisesRegex(TypeError, PATH_REJECTION):
                        store.load()
                self.assertEqual(targets, [])
                self.assertFalse(derived.exists())

    def test_empty_string_is_not_rewritten_to_the_derived_legacy_path(self) -> None:
        """Empty str is a falsy injected value. #193 forbids silent replacement.

        The path guard currently accepts ``str``, so the failure after construction
        is whatever SQLite does with ``Path('')`` (``.``). This assertion is only
        that the derived credentials-adjacent database is not used instead.
        """
        credentials = self._credentials()
        derived = database_path_for_legacy_file(credentials)
        store = DroneCredentialStore(credentials, state_database_file="")
        self.assertEqual(store.state_database_file, "")
        with self._spy_sqlite_connect() as targets:
            with self.assertRaises(Exception):
                store.update("arcade-admin", "correct-horse")
        self.assertFalse(derived.exists())
        derived_as_str = str(derived)
        self.assertTrue(
            all(derived_as_str not in target and Path(target).name != DATABASE_FILENAME for target in targets),
            msg=f"empty-string injection still opened the derived database: {targets!r}",
        )


class OmittedNoneStillDerivesTests(_CwdSandbox):
    def test_omitted_honours_drone_state_database_file_env(self) -> None:
        credentials = self._credentials()
        env_path = self.cwd / "from-env" / "state.sqlite3"
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": str(env_path)}, clear=True):
            store = DroneCredentialStore(credentials)
            self.assertEqual(store.state_database_file, env_path.resolve())
            store.update("arcade-admin", "correct-horse")
            self.assertTrue(store.check("arcade-admin", "correct-horse"))
        self.assertTrue(env_path.exists())
        self.assertFalse((self.cwd / DATABASE_FILENAME).exists())

    def test_constructing_with_a_falsy_mock_does_not_open_sqlite(self) -> None:
        with self._spy_sqlite_connect() as targets:
            DroneCredentialStore(self._credentials(), state_database_file=mock.MagicMock(spec=str))
        self.assertEqual(targets, [])
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
