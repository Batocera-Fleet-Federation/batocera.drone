"""Adversarial acceptance for issue #169, in-scope boundary: spec=Path mocks via Path(...) wrappers.

Issue #169: ``MagicMock(spec=Path)`` passes ``isinstance(value, Path)`` and used to bypass
the mock guard in ``database_path``. The reported symptom is that the state DB still opens
under ``<cwd>/system/drone-app``.

Expected behavior, derived from the issue before reading the patch:

- No production entry point may open ``rom_metadata_cache.sqlite3`` under the working
  directory when the root it is handed is a spec'd mock. Wrapping the mock in ``Path(...)``
  before ``database_path`` turns it into ``Path('.')`` and is the same hole, so it is
  in scope for #169.
- Real roots (``Path``, ``str``) must still persist. The fix must not over-reject.
- Each store entry point must be a positive control: with a real temporary root the same
  call creates the DB under ``<root>/system/drone-app``. Without that control, a call that
  raises early would pass the "nothing under cwd" assertion vacuously.

Every spec'd subtest runs from its own empty temporary cwd so pollution from one case
cannot leak into the next. Stdout is captured because the identity module logs to stdout.
"""

from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path, PurePath
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning
from app.storage import audit_store, mail_queue_store, music_scrape_job_items, saves_store, update_history_store
from app.storage.state_store import DATABASE_FILENAME


MOCK_REJECTION = "userdata_root must be a filesystem path"

SPEC_ROOT_FACTORIES = {
    "MagicMock(spec=Path)": lambda: mock.MagicMock(spec=Path),
    "Mock(spec=Path)": lambda: mock.Mock(spec=Path),
    "MagicMock(spec_set=Path)": lambda: mock.MagicMock(spec_set=Path),
    "MagicMock(spec=PurePath)": lambda: mock.MagicMock(spec=PurePath),
    "create_autospec(Path, instance=True)": lambda: mock.create_autospec(Path, instance=True),
}


class _CwdSandbox(unittest.TestCase):
    """Record every sqlite3.connect target and keep the environment free of DB overrides."""

    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()

    @contextlib.contextmanager
    def _fresh_cwd(self):
        """Run the body from a new empty directory; yield its resolved path."""
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            os.chdir(cwd)
            try:
                yield cwd
            finally:
                os.chdir(previous)

    @contextlib.contextmanager
    def _spy_sqlite_connect(self):
        real_connect = sqlite3.connect
        targets: list[str] = []

        def spy(database, *args, **kwargs):
            targets.append(str(database))
            return real_connect(database, *args, **kwargs)

        with mock.patch("app.storage.state_store.sqlite3.connect", side_effect=spy):
            yield targets

    def _assert_nothing_under(self, cwd: Path, targets: list[str]) -> None:
        for target in targets:
            self.assertNotIn(
                cwd,
                Path(target).resolve().parents,
                msg=f"sqlite3.connect opened {target!r} under the working directory",
            )
        self.assertFalse((cwd / "system").exists(), msg="cwd gained a system/ tree")
        self.assertEqual(sorted(p.name for p in cwd.iterdir()), [])

    def _assert_db_under_root(self, root: Path) -> None:
        db = root / "system" / "drone-app" / DATABASE_FILENAME
        self.assertTrue(db.exists(), msg=f"expected state DB at {db}")


class RecordCollisionSpecRootTests(_CwdSandbox):
    """identity_provisioning.record_collision wraps the root in Path(...) before database_path."""

    def test_spec_path_roots_never_open_state_db_under_cwd(self) -> None:
        for label, factory in SPEC_ROOT_FACTORIES.items():
            with self.subTest(root=label), self._fresh_cwd() as cwd:
                settings = SimpleNamespace(userdata_root=factory(), device_id="aa:bb:cc:dd:ee:ff")
                with self._spy_sqlite_connect() as targets:
                    with contextlib.redirect_stdout(io.StringIO()):
                        identity_provisioning.record_collision(settings, "peer-x", "detail")
                self._assert_nothing_under(cwd, targets)

    def test_real_path_and_str_roots_still_record_collision_event(self) -> None:
        for label in ("Path", "str"):
            with self.subTest(root=label), tempfile.TemporaryDirectory() as root_dir:
                root = Path(root_dir).resolve()
                userdata_root = root if label == "Path" else str(root)
                settings = SimpleNamespace(userdata_root=userdata_root, device_id="aa:bb:cc:dd:ee:ff")
                with contextlib.redirect_stdout(io.StringIO()):
                    identity_provisioning.record_collision(settings, f"peer-{label}", "detail")
                self._assert_db_under_root(root)
                with contextlib.closing(sqlite3.connect(root / "system" / "drone-app" / DATABASE_FILENAME)) as connection:
                    rows = connection.execute(
                        "SELECT COUNT(*) FROM app_events WHERE namespace = ?",
                        (identity_provisioning._COLLISION_EVENT_NAMESPACE,),
                    ).fetchone()[0]
                self.assertGreaterEqual(rows, 1)


class StoreEntryPointSpecRootTests(_CwdSandbox):
    """Each store entry point gets a spec'd root (nothing under cwd) and a real root (DB created)."""

    # Each entry point takes the userdata_root-like value directly, so spec'd mocks reach
    # the production code unchanged (no test-side Path arithmetic on the mock).
    ENTRY_POINTS = {
        "update_history.list_updates": lambda root: update_history_store.list_updates(
            SimpleNamespace(userdata_root=root)
        ),
        "update_history.record_update": lambda root: update_history_store.record_update(
            SimpleNamespace(userdata_root=root), version="4.0.0"
        ),
        "mail_queue.ready": lambda root: mail_queue_store.ready(SimpleNamespace(userdata_root=root)),
        "music_scrape_job_items.list_by_status": lambda root: music_scrape_job_items.list_by_status(
            SimpleNamespace(userdata_root=root), "pending"
        ),
        "audit_store.list_unsent_events": lambda root: audit_store.list_unsent_events(
            SimpleNamespace(userdata_root=root), ["vpn_connected"]
        ),
    }

    def test_spec_path_root_never_opens_state_db_under_cwd(self) -> None:
        for name, call in self.ENTRY_POINTS.items():
            for label, factory in SPEC_ROOT_FACTORIES.items():
                with self.subTest(entry_point=name, root=label), self._fresh_cwd() as cwd:
                    with self._spy_sqlite_connect() as targets:
                        try:
                            call(factory())
                        except TypeError as error:
                            # Signature drift must not pass vacuously as "rejected".
                            self.assertIn(MOCK_REJECTION, str(error))
                        except (OSError, sqlite3.Error):
                            pass
                    self._assert_nothing_under(cwd, targets)

    def test_saves_store_spec_path_root_never_opens_state_db_under_cwd(self) -> None:
        # saves_store takes the saves directory; its DB lives at the parent (userdata) root.
        for label, factory in SPEC_ROOT_FACTORIES.items():
            with self.subTest(root=label), self._fresh_cwd() as cwd:
                with self._spy_sqlite_connect() as targets:
                    try:
                        saves_store.list_saves(factory())
                    except TypeError as error:
                        self.assertIn(MOCK_REJECTION, str(error))
                    except (OSError, sqlite3.Error):
                        pass
                self._assert_nothing_under(cwd, targets)

    def test_real_root_positive_control_creates_db_under_root(self) -> None:
        for name, call in self.ENTRY_POINTS.items():
            with self.subTest(entry_point=name), tempfile.TemporaryDirectory() as root_dir:
                root = Path(root_dir).resolve()
                with self._spy_sqlite_connect() as targets:
                    call(root)
                self._assert_db_under_root(root)
                self.assertTrue(
                    any(Path(target).resolve().parent == root / "system" / "drone-app" for target in targets),
                    msg=f"{name} did not open the state DB under the real root",
                )

    def test_saves_store_real_root_positive_control_creates_db_under_root(self) -> None:
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir).resolve()
            (root / "saves").mkdir()
            with self._spy_sqlite_connect() as targets:
                saves_store.list_saves(root / "saves")
            self._assert_db_under_root(root)
            self.assertTrue(
                any(Path(target).resolve().parent == root / "system" / "drone-app" for target in targets),
            )


if __name__ == "__main__":
    unittest.main()
