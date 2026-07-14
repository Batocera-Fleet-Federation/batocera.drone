"""Tests for drone self-update archive extraction (``common/self_update.py``).

Self-update is the most sensitive path on the device: an Overmind action makes the
drone download ``drone-app.tar.gz`` and overlay it onto the *running* app tree, then
re-exec. ``_download_latest_drone_app`` therefore hand-rolls a tar-slip barrier —
each member is resolved and rejected if it escapes the staging dir — plus a
leading-release-dir re-home and an ``{app, content}`` root allow-list. None of it was
tested. These lock it: a crafted/compromised archive with ``..`` members must raise
and never touch the work tree; legitimate archives overlay ``app``/``content`` while
skipping ``__pycache__``/``.pyc`` and unrelated roots. See ``drone-p2p-transfer-security``.
"""
import io
import tarfile
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from app.common import self_update


def _targz(members):
    """Build in-memory .tar.gz bytes. members: list of (name, content|None-for-dir)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class DownloadLatestDroneAppTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.work_dir = self.root / "work"
        self.settings = types.SimpleNamespace(userdata_root=self.root / "userdata")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, archive_bytes):
        env = {"DRONE_APP_WORK_DIR": str(self.work_dir),
               "DRONE_APP_ARCHIVE_URL": "http://test.invalid/drone-app.tar.gz"}
        with mock.patch.dict("os.environ", env), \
             mock.patch.object(self_update, "urlopen", lambda request, timeout=None: io.BytesIO(archive_bytes)):
            return self_update._download_latest_drone_app(self.settings)

    # --- happy path ------------------------------------------------------
    def test_extracts_app_and_content_skipping_pycache_and_other_roots(self):
        archive = _targz([
            ("app/main.py", b"m"),
            ("app/pkg/mod.py", b"p"),
            ("content/theme.css", b"c"),
            ("app/__pycache__/main.cpython-39.pyc", b"junk"),  # skipped
            ("docs/readme.md", b"d"),                          # outside {app,content}
        ])
        result = self._run(archive)
        self.assertEqual(result["status"], "downloaded")
        self.assertTrue(result["restart_required"])
        self.assertEqual(result["copied_files"], 3)
        self.assertEqual((self.work_dir / "app" / "main.py").read_bytes(), b"m")
        self.assertEqual((self.work_dir / "app" / "pkg" / "mod.py").read_bytes(), b"p")
        self.assertEqual((self.work_dir / "content" / "theme.css").read_bytes(), b"c")
        self.assertFalse((self.work_dir / "app" / "__pycache__").exists())
        self.assertFalse((self.work_dir / "docs").exists())

    def test_rehomes_leading_release_directory(self):
        # GitHub release tarballs wrap everything in a top-level dir; it is stripped.
        archive = _targz([
            ("batocera.drone/app/main.py", b"m"),
            ("batocera.drone/content/x.css", b"c"),
        ])
        result = self._run(archive)
        self.assertEqual(result["copied_files"], 2)
        self.assertEqual((self.work_dir / "app" / "main.py").read_bytes(), b"m")
        self.assertEqual((self.work_dir / "content" / "x.css").read_bytes(), b"c")

    # --- tar-slip barrier ------------------------------------------------
    def test_rejects_parent_traversal_member(self):
        archive = _targz([
            ("app/main.py", b"m"),
            ("app/../../pwned.txt", b"evil"),  # escapes the stage dir
        ])
        with self.assertRaises(ValueError) as ctx:
            self._run(archive)
        self.assertIn("escapes", str(ctx.exception))
        # the overlay onto the real work tree never ran
        self.assertFalse((self.work_dir / "app").exists())
        self.assertFalse((self.root / "pwned.txt").exists())

    def test_rejects_traversal_hidden_behind_rehomed_root(self):
        # A stray top dir is stripped first; the ".." underneath must still be caught.
        archive = _targz([
            ("app/main.py", b"m"),
            ("wrapper/app/../../pwned", b"evil"),
        ])
        with self.assertRaises(ValueError) as ctx:
            self._run(archive)
        self.assertIn("escapes", str(ctx.exception))

    # --- integrity checks ------------------------------------------------
    def test_missing_required_root_raises(self):
        archive = _targz([("app/main.py", b"m")])  # no content/
        with self.assertRaises(ValueError) as ctx:
            self._run(archive)
        self.assertIn("missing required directories", str(ctx.exception))

    def test_empty_download_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(b"")
        self.assertIn("empty", str(ctx.exception))


class DroneAutoUpdateSettingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.work_dir = self.root / "drone-app"
        self.settings = types.SimpleNamespace(userdata_root=self.root / "userdata")

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults_to_enabled_when_setting_has_not_been_saved(self):
        with mock.patch.dict("os.environ", {"DRONE_APP_WORK_DIR": str(self.work_dir)}):
            self.assertTrue(self_update.is_drone_auto_update_enabled(self.settings))

    def test_persists_disabled_and_enabled_choices(self):
        with mock.patch.dict("os.environ", {"DRONE_APP_WORK_DIR": str(self.work_dir)}):
            self.assertFalse(self_update.set_drone_auto_update_enabled(self.settings, False))
            self.assertFalse(self_update.is_drone_auto_update_enabled(self.settings))
            self.assertEqual((self.work_dir / self_update.DRONE_AUTO_UPDATE_FILE).read_text(), "0\n")

            self.assertTrue(self_update.set_drone_auto_update_enabled(self.settings, True))
            self.assertTrue(self_update.is_drone_auto_update_enabled(self.settings))
            self.assertEqual((self.work_dir / self_update.DRONE_AUTO_UPDATE_FILE).read_text(), "1\n")


class DroneAutoUpdatePollerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.work_dir = self.root / "drone-app"
        (self.work_dir / "app").mkdir(parents=True)
        self.settings = types.SimpleNamespace(userdata_root=self.root / "userdata")
        self.env = mock.patch.dict("os.environ", {"DRONE_APP_WORK_DIR": str(self.work_dir)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def _set_version(self, version):
        (self.work_dir / "app" / "VERSION").write_text(f"{version}\n", encoding="utf-8")

    def test_semantic_version_comparison(self):
        self.assertEqual(self_update._semantic_version("v1.4.12"), (1, 4, 12))
        self.assertEqual(self_update._semantic_version("1.4.12"), (1, 4, 12))
        self.assertIsNone(self_update._semantic_version("dev"))

    def test_release_version_is_read_from_latest_redirect(self):
        location = "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/tag/v1.2.3"
        self.assertEqual(self_update._release_version_from_redirect(location), "v1.2.3")

    def test_latest_release_check_uses_head_without_downloading_a_body(self):
        class Response:
            headers = {"Location": "https://github.com/example/drone/releases/tag/v2.0.1"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def geturl(self):
                return ""

        opener = mock.Mock()
        opener.open.return_value = Response()
        with mock.patch.object(self_update, "build_opener", return_value=opener):
            version = self_update._latest_drone_release_version()

        self.assertEqual(version, "v2.0.1")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10.0)

    def test_disabled_check_does_not_touch_network(self):
        self._set_version("v1.0.0")
        self_update.set_drone_auto_update_enabled(self.settings, False)
        with mock.patch.object(self_update, "_latest_drone_release_version") as latest:
            result = self_update._run_drone_auto_update_check_once(self.settings)
        self.assertEqual(result["status"], "disabled")
        latest.assert_not_called()

    def test_current_version_does_not_download(self):
        self._set_version("v1.2.3")
        with mock.patch.object(self_update, "_latest_drone_release_version", return_value="v1.2.3"), \
             mock.patch.object(self_update, "_download_latest_drone_app") as download:
            result = self_update._run_drone_auto_update_check_once(self.settings)
        self.assertEqual(result["status"], "current")
        download.assert_not_called()

    def test_newer_version_downloads_and_schedules_restart(self):
        self._set_version("v1.2.3")
        with mock.patch.object(self_update, "_latest_drone_release_version", return_value="v1.2.4"), \
             mock.patch.object(self_update, "_download_latest_drone_app", return_value={"copied_files": 10}) as download, \
             mock.patch.object(self_update, "_restart_drone_process_soon") as restart:
            result = self_update._run_drone_auto_update_check_once(self.settings)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["latest_version"], "v1.2.4")
        download.assert_called_once_with(self.settings)
        restart.assert_called_once_with()

    def test_poller_runs_on_daemon_thread_without_blocking_caller(self):
        stopped = threading.Event()
        checked = threading.Event()

        def check_once(settings):
            checked.set()
            stopped.set()
            return {"status": "current"}

        with mock.patch.object(self_update, "_run_drone_auto_update_check_once", side_effect=check_once):
            thread = self_update._start_drone_auto_update_poller(self.settings, poll_seconds=0.01, stop_event=stopped)
            self.assertIsNotNone(thread)
            self.assertTrue(thread.daemon)
            self.assertTrue(checked.wait(1))
            thread.join(timeout=1)


class OverlayReleaseTreeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_copies_files_and_skips_pycache(self):
        source = self.root / "src"
        (source / "pkg" / "__pycache__").mkdir(parents=True)
        (source / "pkg" / "mod.py").write_bytes(b"code")
        (source / "top.py").write_bytes(b"top")
        (source / "pkg" / "__pycache__" / "mod.pyc").write_bytes(b"junk")
        (source / "stray.pyc").write_bytes(b"junk")
        target = self.root / "dst"
        copied = self_update._overlay_drone_release_tree(source, target)
        self.assertEqual(copied, 2)  # mod.py + top.py; both .pyc/pycache skipped
        self.assertEqual((target / "pkg" / "mod.py").read_bytes(), b"code")
        self.assertEqual((target / "top.py").read_bytes(), b"top")
        self.assertFalse((target / "pkg" / "__pycache__").exists())
        self.assertFalse((target / "stray.pyc").exists())

    def test_missing_source_raises(self):
        with self.assertRaises(ValueError):
            self_update._overlay_drone_release_tree(self.root / "nope", self.root / "dst")


if __name__ == "__main__":
    unittest.main()
