"""Issue #173: copied access stays revoked across a held clone process restart."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import identity_provisioning as identity, mcp_auth
from app.common.auth import SessionStore
from app.storage.state_store import database_path, open_database
from app.transfer import swarm_reset
from tests.adversarial.test_issue_173_held_clone_auth_uat import provider_for, settings_for
from tests.test_hardware_identity import UUID_B, x86_tree


REPO_ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("openssl"), "openssl is required for identity provisioning")
class HeldCloneRestartAcceptance(unittest.TestCase):
    def test_readable_but_unmodifiable_copied_rows_stay_denied_after_restart(self):
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        with tempfile.TemporaryDirectory(prefix="issue-173-restart-") as temp:
            root = Path(temp)
            hardware_a = x86_tree(root / "hardware-a")
            hardware_b = x86_tree(root / "hardware-b", uuid=UUID_B, board="OTHERBOARD99")
            installed = identity.ensure_identity(settings_for(root / "source"), provider_for(hardware_a))
            self.assertEqual(installed.state, identity.State.READY)
            source = installed.settings
            source_db = database_path(source.userdata_root)
            inherited_cookie = SessionStore(source_db).create("admin")
            inherited_bearer = mcp_auth.generate_token(source)["token"]
            with open_database(source_db) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            clone_root = root / "clone"
            shutil.copytree(source.userdata_root, clone_root, symlinks=True)
            clone = settings_for(clone_root)
            clone_db = database_path(clone.userdata_root)

            # A readable database may reject both deletion and row neutralization.
            with open_database(clone_db) as connection:
                for table, predicate in (("sessions", "1"), ("app_state", "OLD.namespace = 'mcp'")):
                    for verb in ("DELETE", "UPDATE"):
                        connection.execute(
                            f"CREATE TRIGGER block_{table}_{verb.lower()} BEFORE {verb} ON {table} "
                            f"WHEN {predicate} BEGIN SELECT RAISE(ABORT, 'revocation unavailable'); END"
                        )
                connection.commit()
            with mock.patch.object(swarm_reset, "reset_swarm_identity_state", side_effect=OSError("reset unavailable")):
                held = identity.ensure_identity(clone, provider_for(hardware_b))
            self.assertEqual(held.state, identity.State.RECOVERY_REQUIRED)
            self.assertEqual(held.reason, "provisioning_failed")
            self.assertFalse(held.swarm_networking_enabled)
            with open_database(clone_db) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM app_state WHERE namespace='mcp'").fetchone()[0], 1)

            # A child interpreter has no in-memory deny list from this process.
            probe = """
import json, sys
from pathlib import Path
from app.common.auth import SessionStore
from app.common import mcp_auth
from app.storage.state_store import database_path
from tests.adversarial.test_issue_173_held_clone_auth_uat import settings_for
request = json.load(sys.stdin)
settings = settings_for(Path(request['root']))
print(json.dumps({
    'session': SessionStore(database_path(settings.userdata_root)).validate(request['cookie']),
    'mcp': mcp_auth.verify_token(settings, request['bearer']),
}))
"""
            def fresh_process(check_root):
                result = subprocess.run(
                    [sys.executable, "-c", probe],
                    input=json.dumps({"root": str(check_root), "cookie": inherited_cookie, "bearer": inherited_bearer}),
                    text=True, capture_output=True, cwd=REPO_ROOT, timeout=30,
                    env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}, check=True,
                )
                return json.loads(result.stdout)

            self.assertEqual(fresh_process(clone_root), {"session": None, "mcp": False})
            self.assertEqual(fresh_process(source.userdata_root), {"session": "admin", "mcp": True})

            # The held clone remains locally administrable after a new login.
            new_cookie = SessionStore(clone_db).create("admin")
            self.assertEqual(SessionStore(clone_db).validate(new_cookie), "admin")
            self.assertNotEqual(new_cookie, inherited_cookie)


if __name__ == "__main__":
    unittest.main()
