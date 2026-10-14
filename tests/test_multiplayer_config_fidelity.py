"""Lossless key ownership with concurrent writes and persisted recovery journals."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.multiplayer import models as m
from app.multiplayer.config_guard import ConfigGuard
from test_multiplayer_core import make_settings


class ConfigFidelityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = make_settings(temporary.name)
        self.path = Path(self.settings.batocera_conf_file)
        self.guard = ConfigGuard(self.settings)
        self.sid = m.new_session_id()

    def test_restore_keeps_concurrent_bytes_and_exact_owned_line(self):
        original = b'# note\xff\r\n  snes.core  =  bsnes \r\nglobal.volume=50\r\n'
        self.path.write_bytes(original)
        self.guard.apply(self.sid, self.path, "kv", [{"key": "snes.core", "value": "snes9x"}])
        self.path.write_bytes(self.path.read_bytes().replace(b'volume=50', b'volume=75'))
        reborn = ConfigGuard(self.settings)
        self.assertEqual(reborn.restore(self.sid)["errors"], [])
        self.assertEqual(self.path.read_bytes(), original.replace(b'volume=50', b'volume=75'))

    def test_new_key_after_unterminated_tail_keeps_later_user_append(self):
        self.path.write_bytes(b'global.note=Caf\xc3\xa9\xe2\x80\xa8Quest')
        self.guard.apply(self.sid, self.path, "kv", [{"key": "snes.core", "value": "snes9x"}])
        with self.path.open("ab") as handle:
            handle.write(b'global.audio=1\n')
        self.guard.restore(self.sid)
        self.assertEqual(self.path.read_bytes(), b'global.note=Caf\xc3\xa9\xe2\x80\xa8Quest\nglobal.audio=1\n')

    def test_repeated_apply_restores_first_line_and_missing_final_newline(self):
        original = b'  snes.core = bsnes'
        self.path.write_bytes(original)
        for value in ("first", "second"):
            self.guard.apply(self.sid, self.path, "kv", [{"key": "snes.core", "value": value},
                                                        {"key": "snes.netplay.mode", "value": "host"}])
        self.guard.restore(self.sid)
        self.assertEqual(self.path.read_bytes(), original)

    def test_ini_recovery_after_write_failure_preserves_mixed_endings(self):
        original = b'[Network]\r\nEnableWlan=False\nProAdhocServer = socom.cc'
        self.path.write_bytes(original)
        self.guard.apply(self.sid, self.path, "ini", [{"section": "Network", "key": "EnableWlan", "value": "True"},
                                                     {"section": "Network", "key": "EnableAdhocServer", "value": "True"}])
        with mock.patch.object(self.guard, "_atomic_write", side_effect=OSError("busy")):
            self.assertTrue(self.guard.restore(self.sid)["errors"])
        self.assertIn(self.sid, self.guard.pending())
        self.assertEqual(ConfigGuard(self.settings).restore(self.sid)["errors"], [])
        self.assertEqual(self.path.read_bytes(), original)

    def test_user_change_to_owned_key_is_not_reverted(self):
        self.path.write_bytes(b'snes.core=bsnes\r\n')
        self.guard.apply(self.sid, self.path, "kv", [{"key": "snes.core", "value": "snes9x"}])
        self.path.write_bytes(b'snes.core=mesen-s\r\n')
        self.assertEqual(self.guard.restore(self.sid)["left_alone"], 1)
        self.assertEqual(self.path.read_bytes(), b'snes.core=mesen-s\r\n')

    def test_later_apply_appends_after_a_previously_owned_unterminated_line(self):
        original = b'  snes.core = bsnes'
        self.path.write_bytes(original)
        self.guard.apply(self.sid, self.path, "kv", [{"key": "snes.core", "value": "snes9x"}])
        self.guard.apply(self.sid, self.path, "kv", [{"key": "snes.netplay.mode", "value": "host"}])
        self.guard.restore(self.sid)
        self.assertEqual(self.path.read_bytes(), original)
