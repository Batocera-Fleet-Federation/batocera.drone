"""Tests for the pure-mock userdata safety guard (``common/mock_userdata.py``).

``_looks_like_pure_mock_userdata`` decides whether a userdata tree is the disposable
``USE_FAKE_DATA`` demo set or a real device. ``_real_data_roots`` uses it to redirect
the served ROM/BIOS roots, so a **false positive** (real data judged "mock") would
mis-serve a user's real library. The safety net is exclusivity: the tree qualifies as
mock only if it contains a recognised fake ROM (or the seeded flag) AND *every* ROM
file is a known fake with matching bytes — any unknown or tampered file disqualifies
it. These lock that. State lookups are patched out so the tests isolate the file-tree
logic from the state store.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common import mock_userdata
from app.common.mock_userdata import _looks_like_pure_mock_userdata

KNOWN_FAKES = {
    "snes/Chrono Trigger (USA).zip": b"FAKE-SNES-ROM-1",
    "snes/Super Mario World (USA).zip": b"FAKE-SNES-ROM-2",
    "snes/The Legend of Zelda - A Link to the Past (USA).zip": b"FAKE-SNES-ROM-3",
    "gba/Metroid Fusion (USA).zip": b"FAKE-GBA-ROM-1",
    "gba/Mario Kart Super Circuit (USA).zip": b"FAKE-GBA-ROM-2",
    "psx/Castlevania - Symphony of the Night (USA).chd": b"FAKE-PSX-ROM-1",
}


def _write(roms_root: Path, relpath: str, content: bytes):
    path = roms_root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class PureMockDetectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.userdata = Path(self._tmp.name)
        self.roms = self.userdata / "roms"
        # Isolate from the state store: no seed flag unless a test opts in.
        patcher = mock.patch.object(mock_userdata, "_load_state_payload", return_value=None)
        self.load_state = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_all_fakes(self):
        for rel, content in KNOWN_FAKES.items():
            _write(self.roms, rel, content)

    def test_missing_roms_root_is_not_mock(self):
        self.assertFalse(_looks_like_pure_mock_userdata(self.userdata))

    def test_full_known_fake_set_is_mock(self):
        self._seed_all_fakes()
        self.assertTrue(_looks_like_pure_mock_userdata(self.userdata))

    def test_subset_of_known_fakes_is_mock(self):
        _write(self.roms, "snes/Chrono Trigger (USA).zip", b"FAKE-SNES-ROM-1")
        _write(self.roms, "gba/Metroid Fusion (USA).zip", b"FAKE-GBA-ROM-1")
        self.assertTrue(_looks_like_pure_mock_userdata(self.userdata))

    def test_unknown_real_rom_present_disqualifies(self):
        self._seed_all_fakes()
        _write(self.roms, "nes/Real Homebrew (USA).zip", b"actual user rom")
        self.assertFalse(_looks_like_pure_mock_userdata(self.userdata))

    def test_known_fake_path_with_tampered_bytes_disqualifies(self):
        # One genuine fake gets us past the gate; a fake-named file with real bytes
        # must still be rejected by the per-file content check.
        _write(self.roms, "snes/Super Mario World (USA).zip", b"FAKE-SNES-ROM-2")
        _write(self.roms, "snes/Chrono Trigger (USA).zip", b"REAL SAVE DATA, NOT A FAKE")
        self.assertFalse(_looks_like_pure_mock_userdata(self.userdata))

    def test_allowed_gamelist_and_media_do_not_disqualify(self):
        self._seed_all_fakes()
        _write(self.roms, "snes/gamelist.xml", b"<gameList/>")
        _write(self.roms, "snes/images/box.png", b"\x89PNG fake")
        _write(self.roms, "snes/videos/clip.mp4", b"fake video")
        self.assertTrue(_looks_like_pure_mock_userdata(self.userdata))

    def test_no_fakes_and_not_seeded_is_not_mock(self):
        _write(self.roms, "snes/gamelist.xml", b"<gameList/>")  # only allowed files, no fake ROM
        self.assertFalse(_looks_like_pure_mock_userdata(self.userdata))

    def test_seeded_flag_alone_qualifies_empty_tree(self):
        self.roms.mkdir(parents=True)  # exists but no ROM files
        self.load_state.return_value = {"seeded": True}
        self.assertTrue(_looks_like_pure_mock_userdata(self.userdata))

    def test_seeded_flag_does_not_override_a_real_rom(self):
        # Even a seeded tree is NOT pure-mock if a real ROM slipped in — exclusivity
        # is the ultimate safety net, independent of the seed flag.
        self.load_state.return_value = {"seeded": True}
        _write(self.roms, "nes/Real Game (USA).zip", b"real user rom")
        self.assertFalse(_looks_like_pure_mock_userdata(self.userdata))


if __name__ == "__main__":
    unittest.main()
