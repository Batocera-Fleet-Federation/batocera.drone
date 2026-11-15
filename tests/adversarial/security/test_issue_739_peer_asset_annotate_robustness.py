"""Issue #739: peer-supplied asset paths must never break local existence annotation.

Criterion: "When you search assets on a drone in the swarm (not your own) and
the asset is on the machine then it should indicate that the asset is already
on machine."
A paired peer controls the inventory rows this Drone annotates, so hostile
file_path values (embedded null bytes, absolute paths, traversal, overlong
names) must degrade to exists_locally=False instead of raising out of the
admin peer-assets endpoint.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from app.common.settings import Settings
from app.web.handlers_network import HandlersNetworkMixin


def _harness(tmp: str) -> HandlersNetworkMixin:
    bios_root = os.path.join(tmp, "bios")
    os.makedirs(bios_root, exist_ok=True)
    env = {
        "USERDATA_ROOT": tmp,
        "ROMS_ROOT": os.path.join(tmp, "roms"),
        "BIOS_ROOT": bios_root,
        "SAVES_ROOT": os.path.join(tmp, "saves"),
        "MOVIES_ROOT": os.path.join(tmp, "movies"),
        "SHOWS_ROOT": os.path.join(tmp, "shows"),
        "MUSIC_ROOT": os.path.join(tmp, "music"),
        "DRONE_DEVICE_ID": "aa000000-0000-4000-8000-000000000001",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        settings = Settings.from_env()
    handler = HandlersNetworkMixin.__new__(HandlersNetworkMixin)
    handler.settings = settings
    return handler


class PeerAssetAnnotateRobustnessTests(unittest.TestCase):
    def test_hostile_peer_paths_degrade_to_not_on_machine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = _harness(tmp)
            good = os.path.join(tmp, "bios", "good.bin")
            with open(good, "wb") as fh:
                fh.write(b"x")
            items = [
                {"file_path": "good.bin"},
                {"file_path": "a\x00b"},
                {"file_path": "../../etc/passwd"},
                {"file_path": "/etc/passwd"},
                {"file_path": ""},
                {"file_path": "x" * 5000},
            ]
            # Must not raise: hostile peer inventory must not 500 the endpoint.
            handler._annotate_assets_exist_locally("bios", items)
            self.assertTrue(items[0]["exists_locally"] is True)
            for row in items[1:]:
                self.assertTrue(
                    row["exists_locally"] is False,
                    "hostile peer path must degrade to exists_locally=False, got %r" % (row,),
                )


if __name__ == "__main__":
    unittest.main()
