"""Issue #726 UAT: Switch save discovery/import (Super Smash Bros Ultimate) with
symlinked layout roots and large unrelated Switch save trees.

Issue & trusted amendment quotes:
- "This should work for all systems and roms."
- "For example, Super Smash Bros Ultimate on Switch: I will be testing that I can
  download from remote machine, have it applied, then run with all unlocked characters."
- "However, it is online and I can search assets from that machine (so i know it is
  online).  This needs to be fixed."
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common.settings import Settings
from app.transfer import game_saves as saves
from app.web.handlers_game_saves import HandlersGameSavesMixin

SMASH_TITLE_ID = "01006A800016E000"
OTHER_TITLE_ID = "0100F2C0115B6000"


class _BytesResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SwitchAndSymlinkDiscoveryUat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(saves._CHOICES.clear)

        env = {
            "USERDATA_ROOT": str(self.root),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.db"),
            "DRONE_DEVICE_ID": "local-drone",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.settings = Settings.from_env()
        self.item = {
            "system": "switch",
            "rom": f"Super Smash Bros Ultimate [{SMASH_TITLE_ID}].nsp",
            "stem": f"Super Smash Bros Ultimate [{SMASH_TITLE_ID}]",
            "filename": f"Super Smash Bros Ultimate [{SMASH_TITLE_ID}].nsp",
            "ids": [SMASH_TITLE_ID],
            "fingerprint": "fp-smash",
        }

    def test_symlinked_switch_layout_does_not_abort_discovery_or_apply(self):
        """A symlinked emulator layout (e.g. saves/switch/yuzu/nand -> configs/yuzu/nand)
        must not be followed, and must not abort scanning/applying valid layouts."""
        eden_root = saves.roots(self.settings, self.item)["eden-config"]
        remote_profile = "A" * 32
        local_profile = "B" * 32
        save_rel = f"user/save/0000000000000000/{remote_profile}/{SMASH_TITLE_ID}/save_data.bin"
        save_file = eden_root / save_rel
        save_file.parent.mkdir(parents=True)
        save_file.write_bytes(b"all-characters-unlocked")

        # Sibling emulator layout is a symlink (common Batocera Switch layout).
        yuzu_saves = saves.roots(self.settings, self.item)["yuzu-saves"]
        yuzu_saves.parent.mkdir(parents=True)
        real_yuzu = self.root / "system" / "configs" / "yuzu" / "nand"
        real_yuzu.mkdir(parents=True)
        (real_yuzu / "user" / "save" / "0000000000000000" / ("C" * 32) / SMASH_TITLE_ID).mkdir(parents=True)
        (real_yuzu / "user" / "save" / "0000000000000000" / ("C" * 32) / SMASH_TITLE_ID / "s.bin").write_bytes(b"sym")
        yuzu_saves.symlink_to(real_yuzu)

        # Remote peer export handler must succeed and only expose the real (non-symlink) layout.
        handler = HandlersGameSavesMixin()
        handler.settings = self.settings
        handler.repository = mock.Mock()
        handler._peer_request_authorized = mock.Mock(return_value=True)
        sent = {}
        handler._send_json = lambda code, body: sent.update(code=code, body=body)
        with mock.patch.object(saves, "source_game", return_value=self.item):
            handler._handle_peer_game_saves({"system": ["switch"], "rom": [self.item["rom"]]})
        self.assertEqual(sent["code"], 200, sent)
        self.assertEqual([g["layout"] for g in sent["body"]["saves"]], ["eden-config"])

        # Local import into a second profile directory with yuzu-saves symlinked must also apply cleanly.
        save_file.unlink()
        save_file.parent.rmdir()
        save_file.parent.parent.rmdir()
        local_save = eden_root / "user" / "save" / "0000000000000000" / local_profile / SMASH_TITLE_ID / "save_data.bin"
        local_save.parent.mkdir(parents=True)
        local_save.write_bytes(b"fresh-save")

        peer = {"drone_id": "batocera", "name": "BATOCERA", "certificate_fingerprint": "a" * 64}
        group = sent["body"]["saves"][0]
        with mock.patch.object(saves, "tls_peer", return_value=peer), mock.patch.object(
            saves, "_open_peer", return_value=_BytesResponse(json.dumps({"saves": [group]}).encode("utf-8"))
        ):
            token = saves.discover(self.settings, self.item, "batocera")["saves"][0]["choice"]
        with mock.patch.object(saves, "tls_peer", return_value=peer), mock.patch.object(
            saves, "_open_peer", return_value=_BytesResponse(b"all-characters-unlocked")
        ), mock.patch.object(saves.local_network, "record_activity"):
            result = saves.import_choice(self.settings, self.item, token, overwrite=False)
        self.assertTrue(result["applied"])
        self.assertEqual(local_save.read_bytes(), b"all-characters-unlocked")

    def test_unrelated_switch_games_and_ryujinx_slots_do_not_exhaust_scan_limit(self):
        """Large save directories belonging to *other* Switch titles or Ryujinx slots
        must not prevent discovering Smash Ultimate's save."""
        eden_root = saves.roots(self.settings, self.item)["eden-saves"]
        smash_file = eden_root / "user" / "save" / "0000000000000000" / ("A" * 32) / SMASH_TITLE_ID / "progress.bin"
        smash_file.parent.mkdir(parents=True)
        smash_file.write_bytes(b"smash-unlocked")

        other_title_dir = eden_root / "user" / "save" / "0000000000000000" / ("A" * 32) / OTHER_TITLE_ID
        other_title_dir.mkdir(parents=True)
        for index in range(50):
            (other_title_dir / f"entry_{index}.dat").write_bytes(b"x")

        ryu_root = saves.roots(self.settings, self.item)["ryujinx-config"]
        smash_slot = ryu_root / "0000000000000001"
        (smash_slot / "0").mkdir(parents=True)
        (smash_slot / "ExtraData").write_bytes(int(SMASH_TITLE_ID, 16).to_bytes(8, "little") + bytes(504))
        (smash_slot / "0" / "smash.sav").write_bytes(b"ryu-unlocked")

        other_slot = ryu_root / "0000000000000002" / "0"
        other_slot.mkdir(parents=True)
        (ryu_root / "0000000000000002" / "ExtraData").write_bytes(int(OTHER_TITLE_ID, 16).to_bytes(8, "little") + bytes(504))
        for index in range(50):
            (other_slot / f"blob_{index}.dat").write_bytes(b"y")

        with mock.patch.object(saves, "MAX_SCAN", 35):
            groups = saves.inventory(self.settings, self.item)
        self.assertEqual({g["layout"] for g in groups}, {"eden-saves", "ryujinx-config"})


if __name__ == "__main__":
    unittest.main()
