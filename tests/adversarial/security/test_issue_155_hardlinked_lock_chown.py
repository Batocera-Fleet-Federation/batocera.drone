"""Issue #155 security: the root dispatcher must not chown a file the Drone user planted a link to.

``state/`` is writable by the unprivileged Drone user. If that user hard-links
``state/hold.lock`` to a root-owned file on the same filesystem, the root
dispatcher opens it and (with the #155 hand-over) ``fchown``s it to the Drone
user, which hands that user ownership of an arbitrary root-owned file. The
hand-over must only apply to files the dispatcher created itself.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.integrations.media_keys.dispatch import HoldGate
from app.integrations.media_keys.paths import MediaKeysPaths


class HardlinkedLockChownTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.paths = MediaKeysPaths(base / "integrations" / "media-keys")
        self.paths.ensure_layout()
        self.victim = base / "root-owned-victim"
        self.victim.write_text("#!/bin/sh\n")

    def test_preexisting_hardlink_is_not_handed_to_service_user(self):
        os.link(self.victim, self.paths.state_dir / "hold.lock")
        victim_inode = os.stat(self.victim).st_ino
        chowned_inodes = []

        def record_fchown(fd, uid, gid):
            chowned_inodes.append(os.fstat(fd).st_ino)

        owner = (os.getuid() + 1, os.getgid())
        with mock.patch("app.integrations.media_keys.dispatch.script_owner", return_value=owner), \
                mock.patch("os.fchown", side_effect=record_fchown), \
                mock.patch("os.lchown"):
            HoldGate(self.paths, clock=lambda: 1000.0).check("reboot", 2)

        self.assertNotIn(victim_inode, chowned_inodes,
                         "dispatcher fchown'd a pre-existing multi-link file planted in state/")


if __name__ == "__main__":
    unittest.main()
