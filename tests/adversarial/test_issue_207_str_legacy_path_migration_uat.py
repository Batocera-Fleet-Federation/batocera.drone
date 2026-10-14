"""Adversarial UAT for issue #207: str/PathLike legacy JSON must migrate.

Issue #207: ``load_payload(..., legacy_path=str(legacy.json))`` returned the
fallback and left the JSON file in place. The same call with ``pathlib.Path``
imported the document and unlinked the file. ``_read_legacy_json`` invoked
``path.exists()`` / ``read_text()`` inside a bare ``except Exception``, so a
``str`` (no those attributes) was swallowed as "no legacy file".

Expected behavior, derived from the issue and the one-time JSON import
invariant, before treating the delivery tests as ground truth:

- A plain ``str`` legacy path is a filesystem path. ``load_payload`` must
  return the JSON document, persist it in the state DB, and unlink the file.
- A non-``Path`` ``os.PathLike`` whose ``__fspath__`` returns ``str`` must
  behave the same. ``pathlib.Path`` is insufficient coverage: it already has
  ``exists`` / ``read_text`` / ``unlink``, so it never reproduced the bug.
- ``pathlib.PurePosixPath`` is PathLike and has none of those methods.
- After a successful import, a later load with no legacy path still returns
  the migrated document (SQLite is the system of record).
- ``load_events`` has the same str/PathLike hole for JSONL and must import
  rather than skip.
- Production ``DroneCredentialStore.load`` forwards ``self.path`` as
  ``legacy_path``; a str credentials file must still import.

Cases run from an empty temporary cwd with a cleared environment.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from app.common.auth import DroneCredentialStore
from app.storage.state_store import load_events, load_payload


class _FsPath:
    """os.PathLike that is not pathlib.Path and has no exists/read_text/unlink."""

    def __init__(self, value: str) -> None:
        self._value = value

    def __fspath__(self) -> str:
        return self._value


class _StrPath(str):
    """str subclass; isinstance(..., str) is True."""


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

    def _write_json(self, name: str, payload) -> Path:
        path = self.cwd / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


class Issue207StrReproductionTests(_CwdSandbox):
    def test_load_payload_migrates_str_legacy_path_and_unlinks(self) -> None:
        # Shape of the reported repro: load_payload(str(db), 'ns', fallback,
        # legacy_path=str(legacy.json)).
        legacy = self._write_json("legacy.json", {"migrated": True})
        database = str(self.cwd / "state.sqlite3")
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(result, {"migrated": True})
        self.assertFalse(legacy.exists())
        self.assertTrue(Path(database).is_file())

    def test_migrated_str_legacy_survives_a_second_load_without_the_file(self) -> None:
        legacy = self._write_json("keep.json", {"migrated": True})
        database = str(self.cwd / "persist.sqlite3")
        first = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(first, {"migrated": True})
        self.assertFalse(legacy.exists())
        second = load_payload(database, "ns", {"fallback": True})
        self.assertEqual(second, {"migrated": True})

    def test_path_legacy_path_still_migrates(self) -> None:
        legacy = self._write_json("path.json", {"migrated": True})
        result = load_payload(
            self.cwd / "path.sqlite3",
            "ns",
            {"fallback": True},
            legacy_path=legacy,
        )
        self.assertEqual(result, {"migrated": True})
        self.assertFalse(legacy.exists())

    def test_relative_str_legacy_path_imports_from_cwd(self) -> None:
        self._write_json("rel.json", {"via": "relative"})
        result = load_payload(
            "rel.sqlite3", "ns", {"fallback": True}, legacy_path="rel.json"
        )
        self.assertEqual(result, {"via": "relative"})
        self.assertFalse((self.cwd / "rel.json").exists())
        self.assertTrue((self.cwd / "rel.sqlite3").is_file())


class Issue207NonPathPathLikeTests(_CwdSandbox):
    def test_custom_pathlike_without_exists_or_read_text_migrates(self) -> None:
        legacy = self._write_json("like.json", {"migrated": True})
        value = _FsPath(str(legacy))
        self.assertFalse(hasattr(value, "exists"))
        self.assertFalse(hasattr(value, "read_text"))
        self.assertFalse(hasattr(value, "unlink"))
        result = load_payload(
            self.cwd / "like.sqlite3",
            "ns",
            {"fallback": True},
            legacy_path=value,
        )
        self.assertEqual(result, {"migrated": True})
        self.assertFalse(legacy.exists())

    def test_pure_posix_path_migrates_without_path_methods(self) -> None:
        legacy = self._write_json("pure.json", {"migrated": True})
        value = PurePosixPath(legacy)
        self.assertFalse(hasattr(value, "exists"))
        self.assertFalse(hasattr(value, "read_text"))
        self.assertFalse(hasattr(value, "unlink"))
        result = load_payload(
            self.cwd / "pure.sqlite3",
            "ns",
            {"fallback": True},
            legacy_path=value,
        )
        self.assertEqual(result, {"migrated": True})
        self.assertFalse(legacy.exists())

    def test_str_subclass_legacy_path_migrates(self) -> None:
        legacy = self._write_json("sub.json", {"migrated": True})
        result = load_payload(
            self.cwd / "sub.sqlite3",
            "ns",
            {"fallback": True},
            legacy_path=_StrPath(str(legacy)),
        )
        self.assertEqual(result, {"migrated": True})
        self.assertFalse(legacy.exists())


class Issue207LoadEventsMigrationTests(_CwdSandbox):
    def test_load_events_migrates_str_jsonl_and_unlinks(self) -> None:
        legacy = self.cwd / "events.jsonl"
        legacy.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
        events = load_events(
            str(self.cwd / "events.sqlite3"), "ns", legacy_path=str(legacy)
        )
        self.assertEqual(events, [{"b": 2}, {"a": 1}])
        self.assertFalse(legacy.exists())
        again = load_events(str(self.cwd / "events.sqlite3"), "ns")
        self.assertEqual(again, [{"b": 2}, {"a": 1}])

    def test_load_events_migrates_custom_pathlike_jsonl(self) -> None:
        legacy = self.cwd / "like-events.jsonl"
        legacy.write_text('{"a": 1}\n', encoding="utf-8")
        value = _FsPath(str(legacy))
        self.assertFalse(hasattr(value, "exists"))
        events = load_events(
            self.cwd / "like-events.sqlite3", "ns", legacy_path=value
        )
        self.assertEqual(events, [{"a": 1}])
        self.assertFalse(legacy.exists())


class Issue207CredentialStoreUat(_CwdSandbox):
    def test_credential_store_imports_str_credentials_json(self) -> None:
        credentials = self._write_json(
            "system/drone-app/credentials.json",
            {
                "username": "migrated-admin",
                "password_hash": "pbkdf2_sha256$240000$aa$" + ("ab" * 32),
            },
        )
        database = str(self.cwd / "system" / "drone-app" / "rom_metadata_cache.sqlite3")
        store = DroneCredentialStore(str(credentials), state_database_file=database)
        loaded = store.load()
        self.assertEqual(loaded.get("username"), "migrated-admin")
        self.assertIn("password_hash", loaded)
        self.assertFalse(credentials.exists())
        # SQLite is now the source of truth; a later load without the file
        # still authenticates the migrated username.
        again = DroneCredentialStore(
            str(credentials), state_database_file=database
        ).load()
        self.assertEqual(again.get("username"), "migrated-admin")

    def test_custom_state_key_migrates_str_legacy_path(self) -> None:
        legacy = self._write_json("keyed.json", {"migrated": True})
        database = str(self.cwd / "keyed.sqlite3")
        result = load_payload(
            database,
            "ns",
            {"fallback": True},
            legacy_path=str(legacy),
            state_key="alt",
        )
        self.assertEqual(result, {"migrated": True})
        self.assertFalse(legacy.exists())
        self.assertEqual(
            load_payload(database, "ns", {"fallback": True}, state_key="alt"),
            {"migrated": True},
        )
        # Default key is a different row and must not inherit the migration.
        self.assertEqual(
            load_payload(database, "ns", {"fallback": True}),
            {"fallback": True},
        )


class Issue207UnicodeAndSymlinkTests(_CwdSandbox):
    def test_unicode_json_content_roundtrips_through_str_legacy_path(self) -> None:
        legacy = self._write_json("ünicode.json", {"ok": "café"})
        database = str(self.cwd / "ünicode.sqlite3")
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(result, {"ok": "café"})
        self.assertFalse(legacy.exists())
        self.assertEqual(load_payload(database, "ns", {}), {"ok": "café"})

    def test_symlink_str_legacy_path_imports_and_unlinks_the_given_path(self) -> None:
        target = self._write_json("target.json", {"migrated": True})
        link = self.cwd / "link.json"
        os.symlink(target, link)
        result = load_payload(
            str(self.cwd / "link.sqlite3"),
            "ns",
            {"fallback": True},
            legacy_path=str(link),
        )
        self.assertEqual(result, {"migrated": True})
        self.assertFalse(link.exists())
        # The named legacy path is the one-time import source; the target
        # remaining is acceptable as long as the given path is consumed.
        self.assertEqual(
            load_payload(str(self.cwd / "link.sqlite3"), "ns", {"fallback": True}),
            {"migrated": True},
        )


if __name__ == "__main__":
    unittest.main()
