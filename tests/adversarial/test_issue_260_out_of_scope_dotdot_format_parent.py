"""Out-of-scope evidence for issue #260: a format-only directory component created before a ``..`` name.

#260 covers a format-only *final* name. ``open_database('sub/<U+200B>/..')`` has a final name
of ``..``, which is a directory reference, not a format-only name. The guard passes it, so
``open_database`` calls ``mkdir`` on the parent ``sub/<U+200B>`` (an invisible directory) and
then ``sqlite3.connect`` fails on the directory. The directory-path contract is owned by
#220 (directory database paths), not by #260, so this suite is retained for scheduled runs
and must not block #260 delivery.

The expected behavior below is the #260 invariant applied to every format-only path component: no
invisible artifact may be created by ``open_database`` for it. A failing run leaves no files
behind because the work happens in a temporary directory.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.storage import state_store


ZWSP = "​"


class DotDotFormatParentOutOfScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.original_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.original_cwd)
        os.chdir(self.root)
        env = mock.patch.dict(os.environ, {"DRONE_STATE_DATABASE_FILE": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_format_only_parent_component_before_dotdot_is_not_created(self):
        value = "sub/" + ZWSP + "/.."
        with mock.patch.object(state_store.sqlite3, "connect") as connect:
            try:
                state_store.open_database(value)
            except Exception:
                pass
        connect.assert_not_called()
        self.assertEqual(sorted(os.listdir(self.root)), [])


if __name__ == "__main__":
    unittest.main()
