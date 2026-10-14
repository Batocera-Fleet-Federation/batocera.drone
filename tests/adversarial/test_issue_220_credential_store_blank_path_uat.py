"""Adversarial UAT for issue #220: a blank state database path never reaches SQLite.

Issue #220 (found while testing #193): ``DroneCredentialStore(path, state_database_file='')``
stored the empty string as-is. ``state_store._require_filesystem_path`` accepted it because
it is a ``str``, and SQLite then failed on ``Path('') == Path('.')`` with ``OperationalError``.

Expected behavior, derived from the issue and the credential-store contract before reading
the patch:

- An explicit blank database path is a ``ValueError`` from the guard, not a silent
  substitution of the derived legacy database and not an ``OperationalError`` from
  ``sqlite3.connect('.')``.
- ``update()`` writes nothing and must not unlink the legacy credentials JSON. The unlink
  only happens after a successful database save.
- ``check()`` fails closed: it may raise ``ValueError`` or return ``False``, but it must
  never accept the default ``batocera``/``linux`` credentials or a legacy user.
- ``state_database_file=None`` still means "derive the legacy database" (#193 sentinel),
  and a later valid path still works after a rejected blank path.
- A mock injected as the database path stays a ``TypeError``, not a ``ValueError``.

Every case runs from an empty temporary cwd with ``DRONE_STATE_DATABASE_FILE`` unset, so a
stray database file is observable as a changed directory tree.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.auth import DroneCredentialStore
from app.storage.state_store import DATABASE_FILENAME


BLANK_DATABASE_PATHS = ("", Path(""), Path("."), ".", "./")
LEGACY_JSON = '{"username": "arcade-admin"}'


class _BlankPathLike:
    """An ``os.PathLike`` that resolves to an empty string, as a wrapper could produce."""

    def __fspath__(self) -> str:
        return ""


class BlankDatabasePathCredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        env = mock.patch.dict("os.environ")
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DRONE_STATE_DATABASE_FILE", None)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.work = self.root / "cwd"
        self.work.mkdir()
        self._cwd = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, self._cwd)

        self.legacy = self.root / "creds" / "credentials.json"
        self.legacy.parent.mkdir()
        self.legacy.write_text(LEGACY_JSON, encoding="utf-8")

    def _tree(self) -> set[str]:
        return {str(path.relative_to(self.root)) for path in self.root.rglob("*")}

    @contextlib.contextmanager
    def _sqlite_connect_spy(self):
        with mock.patch("sqlite3.connect") as connect:
            yield connect

    def test_blank_database_path_is_a_value_error_before_sqlite(self) -> None:
        for blank in BLANK_DATABASE_PATHS:
            with self.subTest(blank=blank):
                store = DroneCredentialStore(self.legacy, state_database_file=blank)
                before = self._tree()
                with self._sqlite_connect_spy() as connect:
                    with self.assertRaises(ValueError):
                        store.update("arcade-admin", "correct-horse")
                connect.assert_not_called()
                self.assertEqual(self._tree(), before)

    def test_blank_database_path_leaves_the_legacy_credentials_file_intact(self) -> None:
        store = DroneCredentialStore(self.legacy, state_database_file="")
        with self.assertRaises(ValueError):
            store.update("arcade-admin", "correct-horse")
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), LEGACY_JSON)

    def test_blank_database_path_is_not_read_as_the_derived_legacy_database(self) -> None:
        store = DroneCredentialStore(self.legacy, state_database_file="")
        with self.assertRaises(ValueError):
            store.load()
        self.assertFalse((self.legacy.parent / DATABASE_FILENAME).exists())
        self.assertFalse((self.work / DATABASE_FILENAME).exists())

    def test_blank_database_path_fails_closed_for_authentication(self) -> None:
        for blank in BLANK_DATABASE_PATHS:
            with self.subTest(blank=blank):
                store = DroneCredentialStore(self.legacy, state_database_file=blank)
                for username, password in (("batocera", "linux"), ("arcade-admin", "correct-horse")):
                    try:
                        accepted = store.check(username, password)
                    except ValueError:
                        accepted = False
                    self.assertFalse(accepted, f"{username!r} accepted with blank database path {blank!r}")

    def test_pathlike_resolving_to_blank_is_a_value_error_before_sqlite(self) -> None:
        store = DroneCredentialStore(self.legacy, state_database_file=_BlankPathLike())
        before = self._tree()
        with self._sqlite_connect_spy() as connect:
            with self.assertRaises(ValueError):
                store.update("arcade-admin", "correct-horse")
        connect.assert_not_called()
        self.assertEqual(self._tree(), before)

    def test_injected_mock_database_path_is_still_a_type_error(self) -> None:
        # #193 sentinel: an injected mock must reach the guard, not be swapped for a default.
        store = DroneCredentialStore(self.legacy, state_database_file=mock.MagicMock(spec=str))
        with self._sqlite_connect_spy() as connect:
            with self.assertRaises(TypeError):
                store.update("arcade-admin", "correct-horse")
        connect.assert_not_called()

    def test_none_still_derives_the_legacy_database_and_round_trips(self) -> None:
        store = DroneCredentialStore(self.legacy)
        store.update("arcade-admin", "correct-horse")
        self.assertTrue((self.legacy.parent / DATABASE_FILENAME).is_file())
        self.assertTrue(store.check("arcade-admin", "correct-horse"))
        self.assertFalse(self.legacy.exists())

    def test_valid_database_path_works_after_blank_rejections(self) -> None:
        for blank in ("", Path(".")):
            with self.subTest(blank=blank):
                with self.assertRaises(ValueError):
                    DroneCredentialStore(self.legacy, state_database_file=blank).update(
                        "arcade-admin", "correct-horse"
                    )
        database = self.root / "state" / "state.sqlite3"
        store = DroneCredentialStore(self.legacy, state_database_file=database)
        store.update("arcade-admin", "correct-horse")
        self.assertTrue(database.is_file())
        self.assertTrue(store.check("arcade-admin", "correct-horse"))


if __name__ == "__main__":
    unittest.main()
