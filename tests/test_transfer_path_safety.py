"""Adversarial tests for the P2P asset-transfer path-safety guards.

``transfer/transfer_files.py`` gates the mTLS ``GET /peer/{roms,bios,saves}/...`` serving
and the peer-download write path, so ``safe_rom_relative_path`` / ``collision_safe_target``
are the traversal barrier between a peer-supplied string and the local filesystem. These
tests lock the barrier: traversal (``..``), absolute paths, backslash tricks, and symlink
escape must be rejected; legitimate nested paths must pass; and de-collision must never
escape the system directory. See the ``drone-p2p-transfer-security`` skill.
"""
import os
import tempfile
import unittest
from pathlib import Path

from app.transfer.transfer_files import collision_safe_target, safe_rom_relative_path


class SafeRomRelativePathTests(unittest.TestCase):
    def test_accepts_legitimate_relative_paths(self):
        for value, expected in [
            ("Game (USA).zip", "Game (USA).zip"),
            ("snes/Super Mario World.sfc", "snes/Super Mario World.sfc"),
            ("ps3/Game.ps3/PS3_GAME/USRDIR/EBOOT.BIN", "ps3/Game.ps3/PS3_GAME/USRDIR/EBOOT.BIN"),
            ("a\\b\\c.rom", "a/b/c.rom"),          # windows separators normalized
            ("/leading/slash.zip", "leading/slash.zip"),  # absolute -> relative
            ("///many///slashes.zip", "many///slashes.zip"),
            ("weird..name.zip", "weird..name.zip"),        # ".." only rejected as a whole segment
            ("dir/..extension", "dir/..extension"),
        ]:
            with self.subTest(value=value):
                self.assertEqual(safe_rom_relative_path(value), expected)

    def test_rejects_parent_traversal(self):
        for value in [
            "..",
            "../etc/passwd",
            "a/../../b",
            "foo/../../../etc/shadow",
            "..\\..\\windows",          # backslash traversal (normalized to ../..)
            "/../escape",
            "sub/dir/../../../../root",
            "./../x",
        ]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    safe_rom_relative_path(value)

    def test_rejects_empty_or_root_only(self):
        for value in ["", None, "/", "///", "\\", "  "[:0]]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    safe_rom_relative_path(value)


class CollisionSafeTargetTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.system_dir = Path(self._tmp.name) / "roms" / "snes"
        self.system_dir.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_requested_path_when_free(self):
        target = collision_safe_target(self.system_dir, "Game.sfc")
        self.assertEqual(target, (self.system_dir / "Game.sfc").resolve())
        self.assertIn(self.system_dir.resolve(), target.parents)

    def test_allows_nested_subdirectories(self):
        target = collision_safe_target(self.system_dir, "Game.ps3/PS3_GAME/PARAM.SFO")
        self.assertTrue(str(target).endswith("Game.ps3/PS3_GAME/PARAM.SFO"))
        self.assertIn(self.system_dir.resolve(), target.parents)

    def test_de_collides_without_overwriting(self):
        (self.system_dir / "Game.sfc").write_bytes(b"existing")
        (self.system_dir / "Game (2).sfc").write_bytes(b"also existing")
        target = collision_safe_target(self.system_dir, "Game.sfc")
        self.assertEqual(target.name, "Game (3).sfc")
        self.assertFalse(target.exists())

    def test_rejects_traversal_out_of_system_dir(self):
        for value in ["../snes-evil/x.sfc", "../../etc/passwd", "..\\..\\x", "a/../../b.sfc"]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    collision_safe_target(self.system_dir, value)

    def test_rejects_targeting_the_system_dir_itself(self):
        # a path that resolves back to system_dir must be refused (would clobber the dir)
        with self.assertRaises(ValueError):
            collision_safe_target(self.system_dir, ".")

    @unittest.skipUnless(hasattr(os, "symlink"), "requires symlink support")
    def test_rejects_symlink_escape_after_resolve(self):
        # A symlink *inside* system_dir pointing outside must not let writes escape:
        # collision_safe_target re-checks containment AFTER resolve().
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        link = self.system_dir / "escape"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink not permitted in this environment")
        with self.assertRaises(ValueError):
            collision_safe_target(self.system_dir, "escape/pwned.sfc")


if __name__ == "__main__":
    unittest.main()
