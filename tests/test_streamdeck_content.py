"""Stream Deck content and web-boundary tests (no hardware required).

Covers custom scripts (CRUD, execution, timeout, output capture, path
traversal), the shared process runner, uploaded-image validation, key
rendering (fill/fit/stretch, generated art, game artwork, per-device sizes),
the runtime compiler, the game picker over the *real* RomRepository, the
Admin -> Integrations handler security gates, and the routes end to end
through a real Drone HTTP server.
"""

import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zlib
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.integrations.streamdeck.actions import BatoceraControl, BuiltInActionRegistry
from app.integrations.streamdeck.compiler import RuntimeCompiler
from app.integrations.streamdeck.config import default_config
from app.integrations.streamdeck.devices import LibraryStreamDeckDevice
from app.integrations.streamdeck.games import GameLibrary
from app.integrations.streamdeck.images import ImageStore, inspect_image
from app.integrations.streamdeck.manager import StreamDeckIntegration
from app.integrations.streamdeck.paths import OwnershipError, StreamDeckPaths
from app.integrations.streamdeck.process import ProcessRunner
from app.integrations.streamdeck.scripts import ScriptInUseError, ScriptStore

try:
    import PIL  # noqa: F401
    from PIL import Image
    HAVE_PIL = True
except ImportError:  # pragma: no cover
    HAVE_PIL = False

POSIX = os.name == "posix"


# --------------------------------------------------------------- fixtures
def png_bytes(width: int, height: int, rgb=(255, 0, 0)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def jpeg_bytes(width: int, height: int) -> bytes:
    """Structurally valid JPEG headers (APP0 + SOF0) -- enough for the stdlib check."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def webp_bytes(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + bits.to_bytes(4, "little") + b"\x00" * 8
    body = b"WEBP" + b"VP8L" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", len(body)) + body


def pil_bytes(fmt: str, size=(64, 32), color=(0, 128, 255)) -> bytes:
    from io import BytesIO
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


class FakeRepository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.rows = {"snes": [{"unique_id": "g1", "title": "Super Game", "rom_path": "Super Game.sfc",
                               "existing": {"image": "./images/super.png"}}]}
        (root / "snes" / "images").mkdir(parents=True)
        (root / "snes" / "Super Game.sfc").write_bytes(b"rom")

    def list_assets(self, system, asset_type, include_fingerprint=False):
        return self.root / system, list(self.rows.get(system, []))

    def search_roms(self, query, limit=30, **_kwargs):
        return [{"system": s, "unique_id": r["unique_id"], "name": r["title"]}
                for s, rows in self.rows.items() for r in rows if query.lower() in r["title"].lower()][:limit]

    def list_systems(self):
        return [{"name": s, "rom_count": len(r)} for s, r in self.rows.items()]


class Temp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.paths = StreamDeckPaths(self.root / "integrations" / "streamdeck")
        self.paths.ensure_layout()


# ------------------------------------------------------------------ scripts
@unittest.skipUnless(POSIX, "scripts are POSIX executables")
class ScriptTests(Temp):
    def setUp(self) -> None:
        super().setUp()
        self.store = ScriptStore(self.paths)

    def test_create_edit_duplicate_list_and_delete(self):
        created = self.store.create({"name": "Hello", "description": "greets", "code": "#!/bin/sh\necho hi"})
        self.assertRegex(created["id"], r"^[a-f0-9]{32}$")
        script_file = self.paths.scripts_dir / f"{created['id']}.sh"
        self.assertTrue(script_file.is_file())
        self.assertEqual(oct(script_file.stat().st_mode & 0o777), "0o700")
        self.assertTrue(created["code"].endswith("\n"))
        updated = self.store.update(created["id"], {"code": "#!/bin/sh\necho changed\n"})
        self.assertEqual(updated["name"], "Hello")
        self.assertIn("changed", self.store.get(created["id"])["code"])
        copy = self.store.duplicate(created["id"])
        self.assertNotEqual(copy["id"], created["id"])
        self.assertEqual(copy["name"], "Copy of Hello")
        self.assertEqual([row["name"] for row in self.store.list()], ["Copy of Hello", "Hello"])
        self.assertNotIn("code", self.store.list()[0])
        with self.assertRaises(ScriptInUseError):
            self.store.delete(created["id"], [{"profile_id": "default", "key": 0}])
        self.store.delete(created["id"])
        with self.assertRaises(KeyError):
            self.store.get(created["id"])

    def test_validation(self):
        for payload in ({"name": "", "code": "#!/bin/sh\n"}, {"name": "x", "code": "echo no shebang"},
                        {"name": "x", "code": "#!/bin/sh\n\x00"}, {"name": "x", "code": "#!/bin/sh\n" + "a" * 70000},
                        {"name": "x", "code": 42}):
            with self.subTest(payload=str(payload)[:40]), self.assertRaises(ValueError):
                self.store.create(payload)

    def test_path_traversal_and_symlinks_are_refused(self):
        for script_id in ("../../etc/passwd", "..", "/etc/passwd", "a" * 31, "a" * 32 + "/x", "a" * 32 + ".sh"):
            with self.subTest(script_id=script_id):
                with self.assertRaises(ValueError):
                    self.store.get(script_id)
                with self.assertRaises(ValueError):
                    self.store.run(script_id)
        outside = self.root / "outside.sh"
        outside.write_text("#!/bin/sh\necho owned\n")
        script_id = "b" * 32
        (self.paths.scripts_dir / f"{script_id}.sh").symlink_to(outside)
        (self.paths.scripts_dir / f"{script_id}.json").write_text(json.dumps({"name": "evil"}))
        with self.assertRaises(OwnershipError):
            self.store.run(script_id)
        self.assertEqual(self.store.list(), [])

    def test_execution_captures_stdout_stderr_exit_code_and_context(self):
        script = self.store.create({"name": "Env", "code": (
            "#!/bin/sh\necho \"out:$DRONE_STREAMDECK_KEY:$DRONE_STREAMDECK_TRIGGER\"\n"
            "echo \"secret:${DRONE_SECRET_TOKEN:-none}\"\necho oops >&2\nexit 3\n")})
        with mock.patch.dict(os.environ, {"DRONE_SECRET_TOKEN": "leak"}):
            result = self.store.run(script["id"], context={"DRONE_STREAMDECK_KEY": "4", "DRONE_STREAMDECK_TRIGGER": "test",
                                                           "PATH": "/evil"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("out:4:test", result["stdout"])
        self.assertIn("secret:none", result["stdout"])  # Drone's environment is not inherited
        self.assertIn("oops", result["stderr"])
        self.assertGreaterEqual(result["duration_seconds"], 0)
        ok = self.store.create({"name": "Ok", "code": "#!/bin/sh\necho done\n"})
        self.assertEqual(self.store.run(ok["id"])["status"], "completed")

    def test_timeout_and_cancellation_stop_the_script(self):
        script = self.store.create({"name": "Slow", "code": "#!/bin/sh\nsleep 30\n"})
        started = time.monotonic()
        result = self.store.run(script["id"], timeout=1)
        self.assertEqual(result["status"], "timed-out")
        self.assertLess(time.monotonic() - started, 10)
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        self.assertEqual(self.store.run(script["id"], timeout=30, cancel_event=cancel)["status"], "cancelled")

    def test_missing_script_fails_without_running(self):
        result = self.store.run("c" * 32)
        self.assertEqual(result["status"], "failed")
        self.assertIn("not found", result["error"])


@unittest.skipUnless(POSIX, "POSIX processes")
class ProcessRunnerTests(unittest.TestCase):
    def test_arguments_are_never_shell_interpreted(self):
        result = ProcessRunner().run("/bin/echo", ["hi; touch /tmp/should-not-exist-$$", "$(id)"])
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout.strip(), "hi; touch /tmp/should-not-exist-$$ $(id)")

    def test_output_is_bounded(self):
        result = ProcessRunner().run(sys.executable, ["-c", "print('x' * 100000)"], output_limit=1000)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.stdout), 1000)

    def test_backgrounded_grandchild_does_not_turn_success_into_timeout(self):
        started = time.monotonic()
        result = ProcessRunner().run("/bin/sh", ["-c", "sleep 20 & echo started"], timeout=10)
        self.assertTrue(result.ok)
        self.assertLess(time.monotonic() - started, 5)

    def test_missing_executable_is_a_structured_error(self):
        result = ProcessRunner().run("/nonexistent/tool", [])
        self.assertFalse(result.ok)
        self.assertIn("could not start", result.start_error)


# ------------------------------------------------------------------- images
class ImageUploadTests(Temp):
    def setUp(self) -> None:
        super().setUp()
        self.store = ImageStore(self.paths)

    def test_valid_png_jpeg_and_webp_uploads(self):
        for name, data, kind, mime in (("a.png", png_bytes(10, 20), "png", "image/png"),
                                       ("b.JPG", jpeg_bytes(30, 40), "jpeg", "image/jpeg"),
                                       ("c.jpeg", jpeg_bytes(5, 5), "jpeg", "image/jpeg"),
                                       ("d.webp", webp_bytes(64, 48), "webp", "image/webp")):
            with self.subTest(name=name):
                meta = self.store.save_upload(name, data, mime)
                self.assertEqual(meta["type"], kind)
                path, stored = self.store.get(meta["id"])
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(path.parent, self.paths.images_dir)
                self.assertEqual(stored["content_type"], mime)
        self.assertEqual(inspect_image(webp_bytes(64, 48)), ("webp", 64, 48))
        self.assertEqual(inspect_image(jpeg_bytes(30, 40)), ("jpeg", 30, 40))

    @unittest.skipUnless(HAVE_PIL, "needs Pillow to produce real encoder output")
    def test_real_encoder_output_is_accepted(self):
        for fmt, ext in (("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp")):
            with self.subTest(fmt=fmt):
                self.assertEqual(self.store.save_upload(f"x.{ext}", pil_bytes(fmt), "")["width"], 64)

    def test_invalid_uploads_are_rejected(self):
        gif = b"GIF89a" + b"\x00" * 40
        cases = (
            ("evil.sh", png_bytes(2, 2), "image/png"),          # extension
            ("x.gif", gif, "image/gif"),                         # type
            ("x.png", b"#!/bin/sh\nrm -rf /\n", "image/png"),   # not an image
            ("x.png", jpeg_bytes(4, 4), "image/png"),            # extension/content mismatch
            ("x.png", png_bytes(2, 2), "image/jpeg"),            # MIME/content mismatch
            ("x.png", png_bytes(2, 2)[:-6], "image/png"),        # truncated
            ("x.png", png_bytes(5000, 1), "image/png"),          # dimensions
            ("x.png", b"", "image/png"),                         # empty
            ("x.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * (6 * 1024 * 1024), "image/png"),  # size
        )
        for name, data, mime in cases:
            with self.subTest(name=name, mime=mime, size=len(data)), self.assertRaises(ValueError):
                self.store.save_upload(name, data, mime)
        corrupt = bytearray(png_bytes(3, 3))
        corrupt[-20] ^= 0xFF
        with self.assertRaises(ValueError):
            self.store.save_upload("x.png", bytes(corrupt), "image/png")
        self.assertEqual(list(self.paths.images_dir.iterdir()), [])

    def test_decoder_failure_removes_the_stored_file(self):
        store = ImageStore(self.paths, verifier=mock.Mock(side_effect=RuntimeError("cannot identify image")))
        with self.assertRaises(ValueError):
            store.save_upload("x.png", png_bytes(2, 2), "image/png")
        self.assertEqual(list(self.paths.images_dir.iterdir()), [])

    @unittest.skipUnless(HAVE_PIL and POSIX, "needs Pillow")
    def test_manager_verifier_decodes_with_isolated_pillow(self):
        lib = self.paths.lib_dir
        lib.mkdir()
        (lib / "PIL").symlink_to(Path(PIL.__file__).parent, target_is_directory=True)
        manager = StreamDeckIntegration(SimpleNamespace(roms_root=self.root / "roms"), None, paths=self.paths,
                                        usb_detector=lambda: [])
        with mock.patch.object(manager.dependencies, "installed", return_value=True):
            manager.images.save_upload("ok.png", pil_bytes("PNG"), "image/png")
            from io import BytesIO
            buffer = BytesIO()
            Image.effect_noise((256, 256), 64).convert("RGB").save(buffer, format="JPEG")
            truncated = buffer.getvalue()[: len(buffer.getvalue()) // 2]
            self.assertEqual(inspect_image(truncated)[0], "jpeg")  # headers alone look fine...
            with self.assertRaisesRegex(ValueError, "could not be decoded"):  # ...the real decode does not
                manager.images.save_upload("bad.jpg", truncated, "image/jpeg")
        with mock.patch.object(manager.dependencies, "installed", return_value=False):
            with self.assertRaisesRegex(ValueError, "Enable Stream Deck first"):
                manager.images.save_upload("ok.png", pil_bytes("PNG"), "image/png")

    def test_prune_keeps_referenced_and_recent_uploads(self):
        keep = self.store.save_upload("a.png", png_bytes(2, 2), "")
        drop = self.store.save_upload("b.png", png_bytes(2, 2), "")
        recent = self.store.save_upload("c.png", png_bytes(2, 2), "")
        old = time.time() - 7200
        for image_id in (keep["id"], drop["id"]):
            os.utime(self.paths.images_dir / f"{image_id}.json", (old, old))
        self.assertEqual(self.store.prune({keep["id"]}), [drop["id"]])
        self.assertIsNotNone(self.store.path(keep["id"]))
        self.assertIsNotNone(self.store.path(recent["id"]))
        self.assertIsNone(self.store.path(drop["id"]))


@unittest.skipUnless(HAVE_PIL, "needs Pillow")
class RendererTests(Temp):
    def setUp(self) -> None:
        super().setUp()
        from app.integrations.streamdeck.render import KeyRenderer
        self.art_root = self.root / "roms"
        self.art_root.mkdir()
        self.renderer = KeyRenderer(self.paths.rendered_dir, allowed_roots=(self.art_root, self.paths.images_dir))
        # A wide image: left half red, right half blue.
        wide = Image.new("RGB", (200, 100), (255, 0, 0))
        wide.paste(Image.new("RGB", (100, 100), (0, 0, 255)), (100, 0))
        self.wide = self.art_root / "wide.png"
        wide.save(self.wide)

    def spec(self, fit, **extra):
        return {"kind": "image", "source": str(self.wide), "fit": fit, "background": "#00ff00",
                "fallback": {"kind": "generated", "text": "FALLBACK"}, **extra}

    def test_fill_crops_center_and_covers_the_key(self):
        image = self.renderer.render(self.spec("fill"), (72, 72))
        self.assertEqual(image.size, (72, 72))
        self.assertEqual(image.getpixel((2, 36))[:3], (255, 0, 0))
        self.assertEqual(image.getpixel((70, 36))[:3], (0, 0, 255))
        self.assertNotEqual(image.getpixel((36, 1))[:3], (0, 255, 0))  # no border

    def test_fit_letterboxes_with_background(self):
        image = self.renderer.render(self.spec("fit"), (72, 72))
        self.assertEqual(image.size, (72, 72))
        self.assertEqual(image.getpixel((36, 2))[:3], (0, 255, 0))
        self.assertEqual(image.getpixel((5, 36))[:3], (255, 0, 0))

    def test_stretch_ignores_aspect_ratio(self):
        image = self.renderer.render(self.spec("stretch"), (80, 80))
        self.assertEqual(image.size, (80, 80))
        # Whole key covered (no letterbox rows) and both halves kept (no crop).
        for y in (0, 79):
            self.assertEqual(image.getpixel((2, y))[:3], (255, 0, 0))
            self.assertEqual(image.getpixel((77, y))[:3], (0, 0, 255))

    def test_generated_art_and_symbols_at_device_size(self):
        for size in ((72, 72), (80, 80), (96, 96), (120, 120)):
            with self.subTest(size=size):
                image = self.renderer.render({"kind": "generated", "text": "EXIT", "symbol": "exit",
                                              "background": "#b91c1c", "text_color": "#ffffff"}, size)
                self.assertEqual(image.size, size)
                self.assertEqual(image.getpixel((0, 0))[:3], (0xB9, 0x1C, 0x1C))
                colors = {pixel[:3] for pixel in image.getdata()}
                self.assertIn((255, 255, 255), colors)

    def test_disallowed_or_missing_source_falls_back_to_generated(self):
        outside = self.root / "secret.png"
        Image.new("RGB", (10, 10), (1, 2, 3)).save(outside)
        image = self.renderer.render({"kind": "image", "source": str(outside), "fit": "fill",
                                      "fallback": {"kind": "generated", "text": "X", "background": "#123456"}}, (72, 72))
        self.assertEqual(image.getpixel((0, 0))[:3], (0x12, 0x34, 0x56))
        missing = self.renderer.render(self.spec("fill", source=str(self.art_root / "gone.png"),
                                                 fallback={"kind": "generated", "background": "#654321"}), (72, 72))
        self.assertEqual(missing.getpixel((0, 0))[:3], (0x65, 0x43, 0x21))

    def test_rendered_images_are_cached_per_spec_and_size(self):
        self.renderer.render(self.spec("fill"), (72, 72))
        self.renderer.render(self.spec("fill"), (72, 72))
        self.renderer.render(self.spec("fill"), (96, 96))
        self.assertEqual(len(list(self.paths.rendered_dir.glob("*.png"))), 2)
        with mock.patch.object(self.renderer, "_image", side_effect=AssertionError("re-rendered")):
            self.assertEqual(self.renderer.render(self.spec("fill"), (72, 72)).size, (72, 72))

    def test_library_adapter_converts_with_model_aware_pilhelper(self):
        deck = mock.Mock()
        deck.id.return_value = "/dev/hidraw0"
        deck.key_image_format.return_value = {"size": (72, 72)}
        helper = SimpleNamespace(to_native_key_format=mock.Mock(return_value=b"native"))
        device = LibraryStreamDeckDevice(deck, helper)
        image = self.renderer.render({"kind": "generated", "text": "A"}, device.key_image_size)
        self.assertEqual(device.to_native(image), b"native")
        helper.to_native_key_format.assert_called_once_with(deck, image)


# ----------------------------------------------------------------- compiler
class CompilerTests(Temp):
    def setUp(self) -> None:
        super().setUp()
        self.roms = self.root / "roms"
        self.repository = FakeRepository(self.roms)
        if HAVE_PIL:
            Image.new("RGB", (10, 10)).save(self.roms / "snes" / "images" / "super.png")
        else:
            (self.roms / "snes" / "images" / "super.png").write_bytes(png_bytes(4, 4))
        self.games = GameLibrary(self.repository, self.roms)
        self.images = ImageStore(self.paths)
        self.scripts = ScriptStore(self.paths)
        actions = BuiltInActionRegistry(BatoceraControl(which=lambda _name: None),
                                        game_runtime=SimpleNamespace(get_active_game=lambda: None, is_game_running=lambda: False))
        self.compiler = RuntimeCompiler(self.games, self.images, self.scripts, actions)

    def config(self, buttons):
        config = default_config()
        config["profiles"][0]["buttons"] = buttons
        return config

    def test_builtin_default_art_and_structured_entry(self):
        document = self.compiler.compile(self.config([{"key": 0, "action_type": "builtin", "action_id": "exit-game",
                                                       "label": "", "image": {"type": "default"}}]))
        button = document["profiles"][0]["buttons"][0]
        self.assertEqual(button["render"]["text"], "EXIT")
        self.assertEqual(button["render"]["symbol"], "exit")
        self.assertNotIn("command", json.dumps(document))

    def test_game_artwork_and_missing_game(self):
        game = {"id": "g1", "name": "Super Game", "system": "snes", "rom_path": "Super Game.sfc"}
        config = self.config([
            {"key": 0, "action_type": "game", "game": game, "label": "", "image": {"type": "game-artwork", "artwork_field": "auto"}},
            {"key": 1, "action_type": "game", "game": {**game, "id": "gone", "rom_path": "Gone.sfc"}, "label": "",
             "image": {"type": "default"}},
        ])
        worker = self.compiler.compile(config)["profiles"][0]["buttons"]
        self.assertEqual(worker[0]["render"]["kind"], "image")
        self.assertEqual(Path(worker[0]["render"]["source"]), (self.roms / "snes/images/super.png").resolve())
        self.assertEqual(worker[0]["game"]["installed"], True)
        self.assertEqual(worker[1]["problem"], "Game not found. Relink this button.")
        self.assertEqual(worker[1]["render"]["kind"], "generated")
        ui = self.compiler.profile_view(config)[0]["buttons"][0]
        self.assertNotIn("source", ui["render"])
        self.assertTrue(ui["render"]["source_url"].startswith("/v1/api/admin/integrations/streamdeck/games/artwork?"))

    def test_uploaded_and_missing_references(self):
        upload = self.images.save_upload("a.png", png_bytes(4, 4), "image/png")
        config = self.config([
            {"key": 0, "action_type": "none", "label": "", "image": {"type": "uploaded", "image_id": upload["id"], "fit": "fit"}},
            {"key": 1, "action_type": "none", "label": "", "image": {"type": "uploaded", "image_id": "d" * 32}},
            {"key": 2, "action_type": "script", "script_id": "e" * 32, "label": "", "image": {"type": "default"}},
            {"key": 3, "action_type": "profile", "operation": "go-to", "profile_id": "nowhere", "label": "",
             "image": {"type": "default"}},
        ])
        buttons = self.compiler.compile(config)["profiles"][0]["buttons"]
        self.assertEqual((buttons[0]["render"]["kind"], buttons[0]["render"]["fit"]), ("image", "fit"))
        self.assertEqual(buttons[1]["problem"], "Uploaded image is missing.")
        self.assertIn("Script not found", buttons[2]["problem"])
        self.assertIn("Target profile", buttons[3]["problem"])


class RealRepositoryGameTests(Temp):
    """The picker over the real RomRepository (filesystem path, no fakes)."""

    def test_search_browse_resolve_and_artwork(self):
        from app.drone_api import RomRepository
        roms = self.root / "userdata" / "roms"
        system = roms / "snes"
        (system / "images").mkdir(parents=True)
        (self.root / "userdata" / "bios").mkdir()
        (system / "Super Smash.sfc").write_bytes(b"rom")
        (system / "Other.sfc").write_bytes(b"rom")
        (system / "images" / "smash-image.png").write_bytes(png_bytes(4, 4))
        (system / "gamelist.xml").write_text(
            "<gameList><game><path>./Super Smash.sfc</path><name>Super Smash Bros</name>"
            "<image>./images/smash-image.png</image><favorite>true</favorite></game></gameList>")
        library = GameLibrary(RomRepository(roms, self.root / "userdata" / "bios"), roms)
        browse = library.search(system="snes")
        self.assertEqual([item["name"] for item in browse["items"]], ["Super Smash Bros", "Other"])
        smash = browse["items"][0]
        self.assertTrue(smash["favorite"])
        self.assertTrue(smash["has_artwork"])
        self.assertTrue(smash["id"])
        self.assertEqual(library.search("smash")["items"][0]["id"], smash["id"])
        self.assertEqual(library.search("smash", system="snes")["items"][0]["rom_path"], "Super Smash.sfc")
        self.assertEqual(library.resolve(smash)["resolution"], "id")
        self.assertEqual(library.artwork_path(smash).name, "smash-image.png")
        (system / "Super Smash.sfc").unlink()
        library.invalidate()
        self.assertFalse(library.resolve(smash)["installed"])


# --------------------------------------------------------------- handlers
class _Headers(dict):
    def get(self, key, default=None):
        for name, value in self.items():
            if name.lower() == key.lower():
                return value
        return default


class HandlerGateTests(Temp):
    def handler(self, *, session=True, admin=True, headers=None, body=b"{}"):
        from app.web.handlers_integrations import HandlersIntegrationsMixin
        manager = StreamDeckIntegration(SimpleNamespace(roms_root=self.root / "roms"), None, paths=self.paths,
                                        usb_detector=lambda: [])

        class Handler(HandlersIntegrationsMixin):
            pass

        handler = Handler()
        handler.settings = SimpleNamespace(admin_enabled=admin, roms_root=self.root / "roms")
        handler.repository = None
        handler.client_address = ("127.0.0.1", 5555)
        handler.headers = _Headers({"Host": "drone.local:8080", "Content-Type": "application/json",
                                    "Content-Length": str(len(body)), **(headers or {})})
        handler.auth = mock.Mock()
        handler.auth.authenticate_request.side_effect = (
            lambda _headers, client_ip=None: {"token": "t", "username": "admin"} if session else None)
        handler.responses = []
        handler._send_json = lambda status, payload, **_kw: handler.responses.append((status, payload))
        handler._send_unauthorized = lambda: handler.responses.append((401, {}))
        handler._read_json_body = lambda: json.loads(body or b"{}")
        handler._streamdeck = lambda: manager
        handler.manager = manager
        return handler

    def test_session_cookie_is_required_even_for_loopback(self):
        handler = self.handler(session=False)
        handler._handle_admin_integrations_get(["streamdeck", "status"], {})
        handler._handle_admin_integrations_post(["streamdeck", "scripts"])
        self.assertEqual([status for status, _ in handler.responses], [401, 401])
        # Only the cookie is consulted: the loopback address is not passed to the gate.
        for call in handler.auth.authenticate_request.call_args_list:
            self.assertEqual(call.args[1:], ())
            self.assertNotIn("client_ip", call.kwargs)

    def test_admin_disabled_cross_origin_and_non_json_are_refused(self):
        handler = self.handler(admin=False)
        handler._handle_admin_integrations_post(["streamdeck", "scripts"])
        self.assertEqual(handler.responses[-1][0], 403)
        handler = self.handler(headers={"Origin": "https://evil.example"})
        handler._handle_admin_integrations_post(["streamdeck", "scripts"])
        self.assertEqual(handler.responses[-1][0], 403)
        handler = self.handler(headers={"Content-Type": "text/plain"})
        handler._handle_admin_integrations_post(["streamdeck", "scripts"])
        self.assertEqual(handler.responses[-1][0], 415)
        self.assertEqual(handler.manager.scripts.list(), [])

    def test_same_origin_post_creates_script_and_bad_ids_map_to_400(self):
        body = json.dumps({"name": "Hi", "code": "#!/bin/sh\necho hi\n"}).encode()
        handler = self.handler(headers={"Origin": "http://drone.local:8080"}, body=body)
        handler._handle_admin_integrations_post(["streamdeck", "scripts"])
        self.assertEqual(handler.responses[-1][0], 201)
        handler._handle_admin_integrations_get(["streamdeck", "scripts", "..%2F..%2Fetc%2Fpasswd"], {})
        self.assertEqual(handler.responses[-1][0], 400)
        handler._handle_admin_integrations_get(["streamdeck", "scripts", "f" * 32], {})
        self.assertEqual(handler.responses[-1][0], 404)
        handler._handle_admin_integrations_get(["streamdeck", "logs"], {"source": ["../../etc/passwd"]})
        self.assertEqual(handler.responses[-1][0], 400)
        handler._handle_admin_integrations_get(["other"], {})
        self.assertEqual(handler.responses[-1][0], 404)

    def test_buttons_reject_shell_commands_and_tests_require_confirmation(self):
        body = json.dumps({"action_type": "builtin", "action_id": "exit-game", "command": "rm -rf /"}).encode()
        handler = self.handler(body=body)
        handler._handle_admin_integrations_post(["streamdeck", "profiles", "default", "buttons", "0"])
        self.assertEqual(handler.responses[-1][0], 400)
        handler = self.handler(body=json.dumps({"action_type": "builtin", "action_id": "reboot-system"}).encode())
        handler._handle_admin_integrations_post(["streamdeck", "actions", "test"])
        self.assertEqual(handler.responses[-1][0], 400)
        self.assertIn("confirmed", handler.responses[-1][1]["error"])


# ------------------------------------------------------- real HTTP server
class HttpRoutesTests(unittest.TestCase):
    """Every layer: session gate, api_routes dispatch, handlers, manager, disk."""

    def setUp(self) -> None:
        from app.drone_api import Settings, create_server
        from app.mock_data import seed_mock_userdata

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        root = base / "userdata"
        seed_mock_userdata(root)
        self.install_root = base / "drone-app"
        patcher = mock.patch("app.integrations.streamdeck.paths.drone_install_root", return_value=self.install_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"), "THEMES_ROOT": str(root / "themes"),
            "BATOCERA_CONF_FILE": str(root / "system" / "batocera.conf"),
            "ES_SETTINGS_FILE": str(root / "system" / "configs" / "emulationstation" / "es_settings.cfg"),
            "DRONE_APP_USERNAME": "admin", "DRONE_APP_PASSWORD": "changeme", "HTTPS_PORT": "0", "HTTP_ONLY": "1",
            "DRONE_LOCAL_ALLOW_INSECURE_HTTP": "1", "LOG_DIR": str(base / "logs"), "ROM_METADATA_POLL_SECONDS": "0",
            "DRONE_STATE_DATABASE_FILE": str(base / "state.sqlite3"), "DRONE_DEVICE_ID": "streamdeck-http-test",
        }
        env_patch = mock.patch.dict(os.environ, env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        try:
            self.server = create_server(Settings.from_env())
        except PermissionError as error:
            self.skipTest(f"Socket bind is not allowed in this environment: {error}")
        self.port = int(self.server.server_address[1])
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=3)
        self.addCleanup(stop)
        self.cookie = self._login()

    def _login(self) -> str:
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/api/auth/login", method="POST",
                                         data=json.dumps({"username": "admin", "password": "changeme"}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            jar = SimpleCookie()
            jar.load(response.headers.get("Set-Cookie"))
        morsel = next(iter(jar.values()))
        return f"{morsel.key}={morsel.value}"

    def call(self, path, payload=None, *, cookie=True, headers=None, data=None):
        url = f"http://127.0.0.1:{self.port}/v1/api/admin/integrations{path}"
        all_headers = {"Cookie": self.cookie} if cookie else {}
        if payload is not None:
            data = json.dumps(payload).encode()
            all_headers["Content-Type"] = "application/json"
        all_headers.update(headers or {})
        request = urllib.request.Request(url, data=data, headers=all_headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                body = response.read()
                status = response.status
                kind = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            body, status, kind = error.read(), error.code, error.headers.get("Content-Type", "")
        return status, (json.loads(body) if "json" in kind else body)

    def test_integration_routes_end_to_end(self):
        status, cards = self.call("")
        self.assertEqual(status, 200)
        self.assertEqual([card["id"] for card in cards["integrations"]], ["streamdeck"])
        self.assertEqual(cards["scope"], "local-only")
        self.assertFalse(cards["integrations"][0]["enabled"])
        self.assertEqual(self.call("/streamdeck/status", cookie=False)[0], 401)  # loopback is not enough
        status, payload = self.call("/streamdeck/status")
        self.assertEqual((status, payload["enabled"], payload["scope"]), (200, False, "local-only"))
        self.assertEqual(len(self.call("/streamdeck/actions")[1]["actions"]), 10)

        # Games come from the Drone's own library; assign one to a key and apply (saved while disabled).
        systems = self.call("/streamdeck/games/systems")[1]["systems"]
        self.assertTrue(systems)
        status, results = self.call(f"/streamdeck/games?system={systems[0]['name']}")
        self.assertEqual(status, 200)
        game = results["items"][0]
        status, assigned = self.call("/streamdeck/profiles/default/buttons/1",
                                     {"action_type": "game", "game": game, "image": {"type": "game-artwork"}})
        self.assertEqual(status, 200, assigned)
        self.assertEqual(assigned["button"]["game"]["id"], game["id"])
        status, applied = self.call("/streamdeck/apply", {})
        self.assertEqual((status, applied["status"]), (200, "saved"))

        # Scripts: create, test as a job, poll it.
        status, script = self.call("/streamdeck/scripts", {"name": "Hello", "code": "#!/bin/sh\necho hello-from-test\n"})
        self.assertEqual(status, 201)
        if POSIX:
            status, job = self.call(f"/streamdeck/scripts/{script['id']}/test", {})
            self.assertEqual(status, 202)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                job = self.call(f"/streamdeck/jobs/{job['id']}")[1]
                if job.get("finished_at"):
                    break
                time.sleep(0.1)
            self.assertEqual(job["status"], "completed")
            self.assertIn("hello-from-test", job["result"]["stdout"])

        # Upload an image (multipart) and fetch it back.
        image = png_bytes(8, 8)
        body = (b"--B\r\nContent-Disposition: form-data; name=\"image\"; filename=\"key.png\"\r\n"
                b"Content-Type: image/png\r\n\r\n" + image + b"\r\n--B--\r\n")
        status, refused = self.call("/streamdeck/images/upload", data=body,
                                    headers={"Content-Type": "multipart/form-data; boundary=B"})
        self.assertEqual(status, 400)  # no isolated Pillow yet -> cannot fully decode -> refused
        self.assertIn("Enable Stream Deck first", refused["error"])
        from app.integrations.streamdeck.manager import _MANAGERS
        live = next(m for m in _MANAGERS.values() if str(m.paths.root).startswith(str(self.install_root)))
        with mock.patch.object(live.images, "verifier", None):  # as if the isolated decoder had passed it
            status, uploaded = self.call("/streamdeck/images/upload", data=body,
                                         headers={"Content-Type": "multipart/form-data; boundary=B"})
        self.assertEqual(status, 201, uploaded)
        self.assertEqual(self.call(f"/streamdeck/images/{uploaded['id']}")[1], image)

        # Security gates through the real router.
        self.assertEqual(self.call("/streamdeck/scripts", data=b"{}", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.call("/streamdeck/scripts", {"name": "x", "code": "#!/bin/sh\n"},
                                   headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.call("/streamdeck/scripts/..%2f..%2fetc%2fpasswd")[0], 400)
        files = {path.relative_to(self.install_root).parts[:2] for path in self.install_root.rglob("*") if path.is_file()}
        self.assertEqual(files, {("integrations", "streamdeck")})


if __name__ == "__main__":
    unittest.main()
