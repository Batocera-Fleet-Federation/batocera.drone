"""Cross-process UAT for issue #233: a legacy JSON ``null`` file imports once.

The reported repro is a sequence of calls against real files. These checks run
each step in a fresh interpreter, so a JSON null is proven to survive in SQLite
rather than in the memory of the process that migrated it.

Expected, from the issue (null is a document, not a miss):

- The first process imports the legacy ``null``, returns ``None``, and unlinks
  the legacy file.
- A second process with no legacy file returns ``None`` (the stored document),
  not the fallback.
- A process that never sees a legacy file and has no stored row returns the
  fallback, so a genuine miss still falls back.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Paths arrive as argv so no quoting is involved. The fallback is a dict so a
# wrongly substituted fallback is visible in the printed JSON.
_HELPER = (
    "import json, sys\n"
    "from app.storage.state_store import load_payload\n"
    "result = load_payload(sys.argv[1], 'ns', {'fallback': True}, legacy_path=sys.argv[2])\n"
    "print(json.dumps(result))\n"
)


def _run_load(db: Path, legacy: Path) -> object:
    completed = subprocess.run(
        [sys.executable, "-c", _HELPER, str(db), str(legacy)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=dict(os.environ),
    )
    if completed.returncode != 0:
        raise AssertionError(f"helper exited {completed.returncode}: {completed.stderr}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


class NullLegacyAcrossProcessesUAT(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name).resolve()
        self.db = self.root / "state.sqlite3"
        self.legacy = self.root / "legacy-null.json"

    def test_null_import_in_one_process_reads_back_as_none_in_another(self) -> None:
        self.legacy.write_text("null\n", encoding="utf-8")

        migrated = _run_load(self.db, self.legacy)
        self.assertIsNone(migrated)
        self.assertFalse(self.legacy.exists(), "migrated legacy null must be unlinked")

        # A fresh process with no legacy file must see the persisted document,
        # not the fallback.
        reread = _run_load(self.db, self.root / "absent.json")
        self.assertIsNone(reread)

    def test_stored_row_holds_json_null_text_for_the_namespace(self) -> None:
        self.legacy.write_text("null", encoding="utf-8")
        _run_load(self.db, self.legacy)

        with sqlite3.connect(str(self.db)) as connection:
            rows = connection.execute(
                "SELECT payload FROM app_state WHERE namespace = ? AND state_key = ?",
                ("ns", "payload"),
            ).fetchall()
        self.assertEqual(rows, [("null",)])

    def test_genuine_miss_in_fresh_process_returns_fallback_and_writes_nothing(self) -> None:
        self.assertEqual(_run_load(self.db, self.root / "absent.json"), {"fallback": True})
        self.assertEqual(_run_load(self.db, self.root / "absent.json"), {"fallback": True})

        if self.db.exists():
            with sqlite3.connect(str(self.db)) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "app_state" in tables:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM app_state WHERE namespace = ?", ("ns",)
                    ).fetchone()[0]
                    self.assertEqual(count, 0)

    def test_unreadable_legacy_in_fresh_process_keeps_file_and_falls_back(self) -> None:
        self.legacy.write_text("{not json", encoding="utf-8")

        self.assertEqual(_run_load(self.db, self.legacy), {"fallback": True})
        self.assertEqual(self.legacy.read_text(encoding="utf-8"), "{not json")


if __name__ == "__main__":
    unittest.main()
