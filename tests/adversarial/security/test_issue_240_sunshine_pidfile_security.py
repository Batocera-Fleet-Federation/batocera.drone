"""The root-owned Sunshine service must not trust attacker-controlled /tmp state."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.multiplayer.dependencies import SunshineInstaller


class _Probe:
    pass


class SunshinePidfileSecurityTests(unittest.TestCase):
    def test_service_uses_a_private_pidfile_not_a_predictable_tmp_path(self):
        """A local user can pre-create /tmp/drone-sunshine.pid as a symlink.

        The service is executed by Batocera as root.  A shell redirection to that
        symlink would therefore overwrite the selected target with a PID; its
        contents are also later passed to ``kill``.  Keep the assertion on the
        generated service itself so this remains a safe, local-only regression.
        """
        installer = SunshineInstaller(SimpleNamespace(userdata_root="/userdata"), _Probe())
        script = installer._service_text()

        self.assertNotIn("PID_FILE=/tmp/drone-sunshine.pid", script,
                         "root service must not write its PID through a predictable /tmp pathname")


if __name__ == "__main__":
    unittest.main()
