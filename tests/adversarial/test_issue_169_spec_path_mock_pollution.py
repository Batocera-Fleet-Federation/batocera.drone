"""Adversarial acceptance for issue #169: spec=Path mocks must not open the state DB in cwd.

Issue #169 (found while testing #162): ``database_path()`` only rejected a mock when it
failed ``isinstance(value, (str, Path))``. ``MagicMock(spec=Path)`` and
``create_autospec(Path)`` pass that check, so the guard was skipped and SQLite opened
``<cwd>/system/drone-app/rom_metadata_cache.sqlite3``.

Expected behavior, derived from the issue before reading the patch:

- A spec'd or autospec'd mock is still a mock, not a filesystem path. Calling
  ``database_path(mock)`` must raise ``TypeError`` before SQLite is touched.
- Production callers that wrap ``settings.userdata_root`` in ``Path(...)`` first turn a
  spec mock into ``Path('.')`` (the issue's own evidence). The state DB must still not
  open under the working directory when those callers run.
- ``notifications.record_event`` with mocked settings must not write anything and must
  return ``None`` (its documented never-raise contract).
- Regressions: real ``Path``, ``str`` and real ``Path`` subclass roots still resolve to
  ``<root>/system/drone-app/rom_metadata_cache.sqlite3`` and persist. With
  ``DRONE_STATE_DATABASE_FILE`` set, the configured file is used; the issue scopes its
  bug to the "unset" case, so this pins that the fix does not over-reject.

Each test runs in a fresh temporary working directory so any pollution is observable.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path, PurePath
from types import SimpleNamespace
from unittest import mock

from app.device import notifications
from app.storage import rom_metadata_store
from app.storage.state_store import DATABASE_FILENAME, database_path
from app.transfer import local_network, swarm_reset


MOCK_REJECTION = "userdata_root must be a filesystem path"

SPEC_MOCK_FACTORIES = {
    "MagicMock(spec=Path)": lambda: mock.MagicMock(spec=Path),
    "Mock(spec=Path)": lambda: mock.Mock(spec=Path),
    "NonCallableMagicMock(spec=Path)": lambda: mock.NonCallableMagicMock(spec=Path),
    "MagicMock(spec_set=Path)": lambda: mock.MagicMock(spec_set=Path),
    "MagicMock(spec=PurePath)": lambda: mock.MagicMock(spec=PurePath),
    "create_autospec(Path, instance=True)": lambda: mock.create_autospec(Path, instance=True),
}


class _CwdSandbox(unittest.TestCase):
    """Run each test from an empty temporary cwd; any stray write is observable."""

    def setUp(self) -> None:
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmpdir.name).resolve()
        os.chdir(self.cwd)

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._tmpdir.cleanup()

    @contextlib.contextmanager
    def _spy_sqlite_connect(self):
        """Record every sqlite3.connect target; the real connect still runs."""
        real_connect = sqlite3.connect
        targets: list[str] = []

        def spy(database, *args, **kwargs):
            targets.append(str(database))
            return real_connect(database, *args, **kwargs)

        with mock.patch("app.storage.state_store.sqlite3.connect", side_effect=spy):
            yield targets

    def _assert_nothing_opened_under_cwd(self, targets: list[str]) -> None:
        for target in targets:
            resolved = Path(target).resolve()
            self.assertNotIn(
                self.cwd,
                resolved.parents,
                msg=f"sqlite3.connect opened {target!r} under the working directory",
            )
        self.assertFalse((self.cwd / "system" / "drone-app").exists())
        self.assertFalse((self.cwd / DATABASE_FILENAME).exists())
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])


class SpecPathMockDatabasePathTests(_CwdSandbox):
    def test_spec_and_autospec_path_mocks_are_rejected_before_sqlite_connect(self) -> None:
        for label, factory in SPEC_MOCK_FACTORIES.items():
            with self.subTest(mock=label):
                with mock.patch.dict("os.environ", {}, clear=True):
                    with self._spy_sqlite_connect() as targets:
                        with self.assertRaisesRegex(TypeError, MOCK_REJECTION):
                            database_path(factory())
                self.assertEqual(targets, [])
                self._assert_nothing_opened_under_cwd(targets)

    def test_spec_mock_premise_is_isinstance_path(self) -> None:
        # The bug class only exists because a spec mock passes isinstance(..., Path).
        # If this stops holding, the rejection tests above stop proving the fix.
        self.assertIsInstance(mock.MagicMock(spec=Path), Path)
        self.assertIsInstance(mock.create_autospec(Path, instance=True), Path)


class PathWrappedCallerTests(_CwdSandbox):
    """Callers that wrap userdata_root in Path(...) before database_path turn a spec mock
    into Path('.'), which is why a check inside database_path alone is not enough."""

    def _settings_with_spec_root(self) -> mock.MagicMock:
        settings = mock.MagicMock()
        settings.userdata_root = mock.MagicMock(spec=Path)
        return settings

    def test_local_network_helpers_do_not_open_state_db_in_cwd(self) -> None:
        settings = self._settings_with_spec_root()
        with mock.patch.dict("os.environ", {}, clear=True):
            with self._spy_sqlite_connect() as targets:
                try:
                    local_network.get_mode(settings)
                    local_network.paired_peers(settings)
                except (TypeError, OSError, sqlite3.Error):
                    pass
        self._assert_nothing_opened_under_cwd(targets)

    def test_swarm_reset_database_does_not_open_state_db_in_cwd(self) -> None:
        settings = self._settings_with_spec_root()
        with mock.patch.dict("os.environ", {}, clear=True):
            with self._spy_sqlite_connect() as targets:
                try:
                    swarm_reset._reset_database(settings)
                except (TypeError, OSError, sqlite3.Error):
                    pass
        self._assert_nothing_opened_under_cwd(targets)

    def test_cached_asset_counts_do_not_open_state_db_in_cwd(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            with self._spy_sqlite_connect() as targets:
                rom_metadata_store.read_cached_asset_counts(mock.MagicMock(spec=Path))
        self._assert_nothing_opened_under_cwd(targets)


class RecordEventSpecMockTests(_CwdSandbox):
    def test_record_event_with_spec_path_userdata_root_writes_nothing(self) -> None:
        settings = mock.MagicMock()
        settings.userdata_root = mock.MagicMock(spec=Path)
        with mock.patch.dict("os.environ", {}, clear=True):
            with self._spy_sqlite_connect() as targets:
                result = notifications.record_event(
                    settings, "vpn_connected", "VPN connected", "from spec-mocked settings"
                )
        self.assertIsNone(result)
        self._assert_nothing_opened_under_cwd(targets)


class _TaggedPath(type(Path())):
    """A genuine (non-mock) Path subclass; must keep working as a real root."""


class RealRootRegressionTests(_CwdSandbox):
    def test_real_path_root_resolves_and_record_event_persists(self) -> None:
        root = self.cwd / "userdata"
        expected = (root / "system" / "drone-app" / DATABASE_FILENAME).resolve()
        settings = SimpleNamespace(userdata_root=root)
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(database_path(root), expected)
            result = notifications.record_event(settings, "vpn_connected", "VPN connected", "ok")
        self.assertIsNotNone(result)
        self.assertEqual(result["event_type"], "vpn_connected")
        self.assertTrue(expected.exists())
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), ["userdata"])

    def test_path_subclass_root_is_accepted(self) -> None:
        root = self.cwd / "tagged"
        expected = (root / "system" / "drone-app" / DATABASE_FILENAME).resolve()
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(database_path(_TaggedPath(root)), expected)

    def test_configured_state_database_file_is_used_for_spec_mock(self) -> None:
        # Boundary, outside the issue's "unset" scope: a configured file is an explicit
        # operator choice, so the guard must not reject it.
        configured = self.cwd / "configured-state.sqlite3"
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": str(configured)}, clear=True):
            self.assertEqual(database_path(mock.MagicMock(spec=Path)), configured.resolve())


if __name__ == "__main__":
    unittest.main()
