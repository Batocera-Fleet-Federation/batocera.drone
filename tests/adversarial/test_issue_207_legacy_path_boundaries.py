"""Adversarial malformed-input and error-path checks for issue #207.

Issue #207 is a swallowed AttributeError on str ``legacy_path``. Migration is a
one-time import: when the state row is empty, a readable JSON file is copied
into SQLite and then unlinked. Anything that is not a readable JSON file must
not destroy the source and must not raise ``AttributeError``.

Expected behavior, derived from that invariant:

- Missing files, directories, and unreadable JSON yield the fallback and leave
  the path in place so an operator can fix the file and retry.
- A populated state row wins over a still-present legacy file; the file is not
  unlinked (import already happened, or the DB is the source of truth).
- JSON values that parse as ``False``, ``0``, ``""``, or ``[]`` are payloads,
  not misses. The miss sentinel is Python ``None`` from "no readable document".
- Non-path values (None, int, bytes, mocks) skip import without AttributeError
  and without writing mock-named files into cwd.
- A later valid rewrite of a previously invalid file must still import.
- Unlink failure after a successful copy must not lose the copied payload.
- ``load_events`` fail-closes on a JSONL parse error: no unlink, no partial
  import of a truncated file.

Cases run from an empty temporary cwd so stray files are visible.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage.state_store import append_event, load_events, load_payload, save_payload


REPO_ROOT = Path(__file__).resolve().parents[2]


def _mock_named(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return [
        path.name
        for path in directory.iterdir()
        if "Mock" in path.name or path.name.startswith("<")
    ]


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


class Issue207MalformedLegacyFileTests(_CwdSandbox):
    def test_missing_str_legacy_path_returns_fallback(self) -> None:
        missing = str(self.cwd / "absent.json")
        result = load_payload(
            str(self.cwd / "absent.sqlite3"),
            "ns",
            {"fallback": True},
            legacy_path=missing,
        )
        self.assertEqual(result, {"fallback": True})
        self.assertFalse(Path(missing).exists())

    def test_directory_str_legacy_path_returns_fallback_and_keeps_directory(self) -> None:
        directory = self.cwd / "legacy-dir"
        directory.mkdir()
        (directory / "nested.txt").write_text("keep", encoding="utf-8")
        result = load_payload(
            str(self.cwd / "dir.sqlite3"),
            "ns",
            {"fallback": True},
            legacy_path=str(directory),
        )
        self.assertEqual(result, {"fallback": True})
        self.assertTrue(directory.is_dir())
        self.assertEqual((directory / "nested.txt").read_text(encoding="utf-8"), "keep")

    def test_invalid_json_returns_fallback_and_keeps_file_for_retry(self) -> None:
        legacy = self.cwd / "bad.json"
        legacy.write_text("{not json", encoding="utf-8")
        database = str(self.cwd / "bad.sqlite3")
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(result, {"fallback": True})
        self.assertTrue(legacy.is_file())
        self.assertEqual(legacy.read_text(encoding="utf-8"), "{not json")

        legacy.write_text('{"migrated": true}', encoding="utf-8")
        retried = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(retried, {"migrated": True})
        self.assertFalse(legacy.exists())

    def test_utf8_bom_json_does_not_destroy_the_file_when_unreadable(self) -> None:
        legacy = self.cwd / "bom.json"
        legacy.write_bytes(b"\xef\xbb\xbf{\"migrated\": true}")
        result = load_payload(
            str(self.cwd / "bom.sqlite3"),
            "ns",
            {"fallback": True},
            legacy_path=str(legacy),
        )
        # json.loads rejects a leading BOM. Fail closed: keep the file.
        if result == {"migrated": True}:
            self.assertFalse(legacy.exists())
        else:
            self.assertEqual(result, {"fallback": True})
            self.assertTrue(legacy.is_file())

    def test_empty_legacy_file_returns_fallback_and_keeps_file(self) -> None:
        legacy = self.cwd / "empty.json"
        legacy.write_text("", encoding="utf-8")
        result = load_payload(
            str(self.cwd / "empty.sqlite3"),
            "ns",
            {"fallback": True},
            legacy_path=str(legacy),
        )
        self.assertEqual(result, {"fallback": True})
        self.assertTrue(legacy.is_file())


class Issue207PayloadShapeTests(_CwdSandbox):
    def test_falsy_json_values_are_imported_not_treated_as_missing(self) -> None:
        cases = {
            "false.json": ("false", False),
            "zero.json": ("0", 0),
            "empty-str.json": ('""', ""),
            "empty-list.json": ("[]", []),
            "empty-obj.json": ("{}", {}),
        }
        for name, (text, expected) in cases.items():
            with self.subTest(file=name):
                legacy = self.cwd / name
                legacy.write_text(text, encoding="utf-8")
                database = str(self.cwd / (name + ".sqlite3"))
                result = load_payload(
                    database, "ns", {"fallback": True}, legacy_path=str(legacy)
                )
                self.assertEqual(result, expected)
                self.assertFalse(legacy.exists())
                self.assertEqual(
                    load_payload(database, "ns", {"fallback": True}), expected
                )

    def test_populated_db_wins_and_str_legacy_file_is_left_in_place(self) -> None:
        legacy = self.cwd / "stale.json"
        legacy.write_text('{"migrated": true}', encoding="utf-8")
        database = str(self.cwd / "stale.sqlite3")
        save_payload(database, "ns", {"existing": True})
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(result, {"existing": True})
        self.assertTrue(legacy.is_file())
        self.assertEqual(legacy.read_text(encoding="utf-8"), '{"migrated": true}')

    def test_other_namespace_does_not_consume_the_legacy_file(self) -> None:
        legacy = self.cwd / "shared.json"
        legacy.write_text('{"migrated": true}', encoding="utf-8")
        database = str(self.cwd / "shared.sqlite3")
        save_payload(database, "other", {"other": True})
        result = load_payload(
            database, "ns", {"fallback": True}, legacy_path=str(legacy)
        )
        self.assertEqual(result, {"migrated": True})
        self.assertFalse(legacy.exists())
        self.assertEqual(load_payload(database, "other", {}), {"other": True})


class Issue207NonPathSkipTests(_CwdSandbox):
    def test_non_path_legacy_values_return_fallback_without_attributeerror(self) -> None:
        values = {
            "None": None,
            "empty-str": "",
            "int": 12345,
            "float": 1.5,
            "bool": True,
            "list": [],
            "dict": {},
            "object": object(),
            "bytes": b"legacy.json",
            "bytearray": bytearray(b"legacy.json"),
        }
        for label, value in values.items():
            with self.subTest(value=label):
                result = load_payload(
                    str(self.cwd / f"{label}.sqlite3"),
                    "ns",
                    {"fallback": True},
                    legacy_path=value,  # type: ignore[arg-type]
                )
                self.assertEqual(result, {"fallback": True})
        self.assertEqual(_mock_named(self.cwd), [])
        self.assertEqual(_mock_named(REPO_ROOT), [])

    def test_magicmock_legacy_path_skips_and_does_not_write_mock_named_files(self) -> None:
        flavours = [
            mock.MagicMock(),
            mock.Mock(),
            mock.MagicMock(spec=Path),
            mock.MagicMock(spec=str),
            mock.create_autospec(Path, instance=True),
        ]
        for index, value in enumerate(flavours):
            with self.subTest(index=index):
                result = load_payload(
                    str(self.cwd / f"mock-{index}.sqlite3"),
                    "ns",
                    {"fallback": True},
                    legacy_path=value,
                )
                self.assertEqual(result, {"fallback": True})
        self.assertEqual(_mock_named(self.cwd), [])
        self.assertEqual(_mock_named(REPO_ROOT), [])

    def test_empty_str_legacy_path_does_not_import_cwd_json_named_empty(self) -> None:
        # ``if not value`` must not wander into cwd looking for a default file.
        stray = self.cwd / "legacy.json"
        stray.write_text('{"migrated": true}', encoding="utf-8")
        result = load_payload(
            str(self.cwd / "empty-arg.sqlite3"),
            "ns",
            {"fallback": True},
            legacy_path="",
        )
        self.assertEqual(result, {"fallback": True})
        self.assertTrue(stray.is_file())


class Issue207UnlinkFailureTests(_CwdSandbox):
    def test_unlink_oserror_still_returns_migrated_payload_from_db(self) -> None:
        db_dir = self.cwd / "db"
        legacy_dir = self.cwd / "legacy-parent"
        db_dir.mkdir()
        legacy_dir.mkdir()
        legacy = legacy_dir / "legacy.json"
        legacy.write_text('{"migrated": true}', encoding="utf-8")
        database = str(db_dir / "state.sqlite3")
        os.chmod(legacy_dir, 0o555)
        try:
            result = load_payload(
                database, "ns", {"fallback": True}, legacy_path=str(legacy)
            )
            self.assertEqual(result, {"migrated": True})
            # Parent is not writable, so the source file can remain. The DB copy
            # is still the system of record on the next load.
            self.assertEqual(
                load_payload(
                    database, "ns", {"fallback": True}, legacy_path=str(legacy)
                ),
                {"migrated": True},
            )
        finally:
            os.chmod(legacy_dir, 0o755)


class Issue207LoadEventsErrorPathTests(_CwdSandbox):
    def test_invalid_jsonl_is_not_unlinked(self) -> None:
        legacy = self.cwd / "bad.jsonl"
        legacy.write_text('{"a": 1}\nnot-json\n{"b": 2}\n', encoding="utf-8")
        events = load_events(
            str(self.cwd / "bad-events.sqlite3"), "ns", legacy_path=str(legacy)
        )
        self.assertEqual(events, [])
        self.assertTrue(legacy.is_file())
        self.assertIn("not-json", legacy.read_text(encoding="utf-8"))

    def test_populated_events_namespace_does_not_import_or_unlink_str_jsonl(self) -> None:
        legacy = self.cwd / "later.jsonl"
        legacy.write_text('{"imported": true}\n', encoding="utf-8")
        database = str(self.cwd / "later.sqlite3")
        append_event(database, "ns", {"existing": True})
        events = load_events(database, "ns", legacy_path=str(legacy))
        self.assertEqual(events, [{"existing": True}])
        self.assertTrue(legacy.is_file())

    def test_missing_str_jsonl_returns_empty_list(self) -> None:
        events = load_events(
            str(self.cwd / "no-events.sqlite3"),
            "ns",
            legacy_path=str(self.cwd / "missing.jsonl"),
        )
        self.assertEqual(events, [])

    def test_events_directory_legacy_path_is_left_in_place(self) -> None:
        directory = self.cwd / "events-dir"
        directory.mkdir()
        events = load_events(
            str(self.cwd / "dir-events.sqlite3"), "ns", legacy_path=str(directory)
        )
        self.assertEqual(events, [])
        self.assertTrue(directory.is_dir())

    def test_load_events_skips_mock_legacy_path_without_mock_named_files(self) -> None:
        events = load_events(
            str(self.cwd / "mock-events.sqlite3"),
            "ns",
            legacy_path=mock.MagicMock(spec=Path),
        )
        self.assertEqual(events, [])
        self.assertEqual(_mock_named(self.cwd), [])
        self.assertEqual(_mock_named(REPO_ROOT), [])


if __name__ == "__main__":
    unittest.main()
