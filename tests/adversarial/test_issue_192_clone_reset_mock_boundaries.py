"""Issue #192 integration: clone reset must not report success on a mock userdata_root.

The helper-level short-circuits in #192 also sat on the clone-provisioning path:
reset_swarm_identity_state logs pinned_certificates_removed and the journal treats
that step as complete. Returning 0 from a mock root would mark pins cleared when
nothing was touched.

Expected, from the clone invariants (pins cleared before complete; fresh id; a
failed cleanup must not look like success) before relying on the patch:

- reset_swarm_identity_state with a mock userdata_root raises TypeError and does
  not return pinned_certificates_removed=0.
- An explicit DRONE_STATE_DATABASE_FILE does not skip the pin-directory guard:
  the SQLite reset may use the override, but pin clearing still needs a real
  userdata_root.
- Flat-mode imports (python app/main.py) reject a mock root the same way; a
  relative-import fallback must not resurrect the old ``if _is_mock_path: return 0``.
- A real userdata tree still clears pins and reports the count (positive control).

NFS export revocation is stubbed so a missing NFS state file cannot mask the
identity-path assertion.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common.device_identity import _require_real_path
from app.storage.state_store import DATABASE_FILENAME, open_database
from app.transfer import swarm_reset


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
REJECTION = "userdata_root must be a filesystem path"

FLAT_MOCK_PROBE = r"""
import sys
import types
from unittest import mock

sys.path.insert(0, sys.argv[1])

from common import identity_provisioning
from transfer import swarm_reset

settings = types.SimpleNamespace(userdata_root=mock.MagicMock(), device_id="AA:BB:CC:DD:EE:FF")
for helper in (
    lambda: identity_provisioning._choose_device_id(settings, {}),
    lambda: identity_provisioning._retire_device_id(settings),
    lambda: swarm_reset._clear_pinned_peer_certificates(settings),
):
    try:
        result = helper()
    except TypeError as error:
        if "userdata_root must be a filesystem path" not in str(error):
            raise
    else:
        raise SystemExit(f"helper returned {result!r} instead of raising TypeError")
print("flat-mock-root-rejected")
"""


def _settings(userdata_root) -> SimpleNamespace:
    return SimpleNamespace(userdata_root=userdata_root, device_id=CONFIGURED_ID)


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


class CloneResetMockRootTests(_CwdSandbox):
    def test_reset_swarm_identity_state_does_not_report_zero_pins_cleared(self) -> None:
        settings = _settings(mock.MagicMock())
        with mock.patch("app.device.nfs_export_manager.revoke_exports_for_identity_change"):
            with self.assertRaisesRegex(TypeError, REJECTION):
                swarm_reset.reset_swarm_identity_state(settings)
        self.assertFalse((self.cwd / "system").exists())
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])

    def test_database_file_override_still_refuses_to_clear_pins_for_a_mock_root(self) -> None:
        db = self.cwd / "override.sqlite3"
        with open_database(db) as connection:
            connection.execute(
                "INSERT INTO app_state (namespace, state_key, payload, updated_at) VALUES (?,?,?,?)",
                ("local_paired_peers", "payload", "{}", "2020-01-01T00:00:00+00:00"),
            )
            connection.commit()
        settings = _settings(mock.MagicMock())
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": str(db)}, clear=True):
            with mock.patch("app.device.nfs_export_manager.revoke_exports_for_identity_change"):
                with self.assertRaisesRegex(TypeError, REJECTION):
                    swarm_reset.reset_swarm_identity_state(settings)
        self.assertTrue(db.exists())
        self.assertFalse((self.cwd / "system" / "drone-app" / "local-peer-certs").exists())

    def test_reset_on_a_real_root_clears_pins_and_reports_the_count(self) -> None:
        root = self.cwd / "userdata"
        pin_dir = root / "system" / "drone-app" / "local-peer-certs"
        pin_dir.mkdir(parents=True)
        (pin_dir / "peer.crt").write_text("inherited", encoding="utf-8")
        db = root / "system" / "drone-app" / DATABASE_FILENAME
        with open_database(db) as connection:
            connection.execute(
                "INSERT INTO app_state (namespace, state_key, payload, updated_at) VALUES (?,?,?,?)",
                ("local_paired_peers", "payload", "{}", "2020-01-01T00:00:00+00:00"),
            )
            connection.commit()
        settings = _settings(root)
        with mock.patch("app.device.nfs_export_manager.revoke_exports_for_identity_change"):
            with mock.patch(
                "app.device.vpn_manager._load_state",
                return_value={"source_peer_id": "", "sharing_enabled": False},
            ):
                with mock.patch(
                    "app.device.smtp_manager._load_state",
                    return_value={"source_peer_id": "", "sharing_enabled": False},
                ):
                    with mock.patch("app.device.tailnet_service._revoke_local_sharing"):
                        result = swarm_reset.reset_swarm_identity_state(settings)
        self.assertEqual(result["pinned_certificates_removed"], 1)
        self.assertEqual(list(pin_dir.iterdir()), [])


class FlatModeMockRootTests(unittest.TestCase):
    def test_flat_mode_helpers_reject_mock_root_and_leave_cwd_empty(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            cwd_path = Path(cwd).resolve()
            result = subprocess.run(
                [sys.executable, "-I", "-c", FLAT_MOCK_PROBE, str(REPO_ROOT / "app")],
                cwd=str(cwd_path),
                capture_output=True,
                text=True,
                timeout=120,
            )
            names = sorted(p.name for p in cwd_path.iterdir())
        self.assertEqual(
            result.returncode,
            0,
            msg=f"flat-mode mock-root rejection failed:\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("flat-mock-root-rejected", result.stdout)
        self.assertEqual(names, [])


class RequireRealPathContractTests(unittest.TestCase):
    """The shared guard is the mechanism; a mock must not be classified as a real path."""

    def test_require_real_path_rejects_magicmock_before_pathlib(self) -> None:
        with self.assertRaisesRegex(TypeError, REJECTION):
            _require_real_path(mock.MagicMock(), "userdata_root")

    def test_require_real_path_rejects_spec_path_masquerade(self) -> None:
        with self.assertRaisesRegex(TypeError, REJECTION):
            _require_real_path(mock.MagicMock(spec=Path), "userdata_root")

    def test_require_real_path_accepts_str_and_path(self) -> None:
        _require_real_path("/userdata", "userdata_root")
        _require_real_path(Path("/userdata"), "userdata_root")


if __name__ == "__main__":
    unittest.main()
