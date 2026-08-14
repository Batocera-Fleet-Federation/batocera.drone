"""Tests for drone self-update archive extraction (``common/self_update.py``).

Self-update is the most sensitive path on the device: an admin-triggered action makes
the drone download ``drone-app.tar.gz`` and overlay it onto the *running* app tree, then
re-exec. ``_download_latest_drone_app`` therefore hand-rolls a tar-slip barrier —
each member is resolved and rejected if it escapes the staging dir — plus a
leading-release-dir re-home and an ``{app, content}`` root allow-list. None of it was
tested. These lock it: a crafted/compromised archive with ``..`` members must raise
and never touch the work tree; legitimate archives overlay ``app``/``content`` while
skipping ``__pycache__``/``.pyc`` and unrelated roots. See ``drone-p2p-transfer-security``.
"""
import io
import json
import tarfile
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from app.common import self_update
from app.common.settings import Settings
from app.storage import audit_store
from app.storage import update_history_store


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


def _complete_drone_members(members=(), *, prefix=""):
    required = {
        "app/main.py": b"main",
        "app/drone_api.py": b"api",
        "app/VERSION": b"v1.0.0\n",
        "app/web/templates/index.html": b"html",
        "app/web/static/js/drone.js": b"js",
        "app/web/static/css/drone.css": b"css",
        "content/batocera-swarm-mascot.jpg": b"jpg",
        "content/drone.png": b"png",
    }
    required.update(dict(members))
    return [(f"{prefix}{name}", content) for name, content in required.items()]


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
        # Ports client update is covered separately in
        # DownloadLatestPortsClientTests -- stubbed here so these
        # drone-app-archive-extraction tests aren't coupled to it (and don't
        # share the mocked urlopen above with a second, unrelated download).
        with mock.patch.dict("os.environ", env), \
             mock.patch.object(self_update, "urlopen", lambda request, timeout=None: io.BytesIO(archive_bytes)), \
             mock.patch.object(self_update, "_download_latest_ports_client", return_value={"status": "test-noop"}):
            return self_update._download_latest_drone_app(self.settings)

    # --- happy path ------------------------------------------------------
    def test_extracts_app_and_content_skipping_pycache_and_other_roots(self):
        archive = _targz(_complete_drone_members([
            ("app/main.py", b"m"),
            ("app/pkg/mod.py", b"p"),
            ("content/theme.css", b"c"),
            ("app/__pycache__/main.cpython-39.pyc", b"junk"),  # skipped
            ("docs/readme.md", b"d"),                          # outside {app,content}
        ]))
        result = self._run(archive)
        self.assertEqual(result["status"], "downloaded")
        self.assertTrue(result["restart_required"])
        self.assertEqual(result["copied_files"], 10)
        self.assertEqual((self.work_dir / "app" / "main.py").read_bytes(), b"m")
        self.assertEqual((self.work_dir / "app" / "pkg" / "mod.py").read_bytes(), b"p")
        self.assertEqual((self.work_dir / "content" / "theme.css").read_bytes(), b"c")
        self.assertFalse((self.work_dir / "app" / "__pycache__").exists())
        self.assertFalse((self.work_dir / "docs").exists())

    def test_rehomes_leading_release_directory(self):
        # GitHub release tarballs wrap everything in a top-level dir; it is stripped.
        archive = _targz(_complete_drone_members(
            [("app/main.py", b"m"), ("content/x.css", b"c")],
            prefix="batocera.drone/",
        ))
        result = self._run(archive)
        self.assertEqual(result["copied_files"], 9)
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

    def test_rejects_incomplete_web_api_or_image_payload(self):
        archive = _targz([
            ("app/main.py", b"m"),
            ("app/drone_api.py", b"api"),
            ("content/placeholder.txt", b"not-the-required-image"),
        ])
        with self.assertRaises(ValueError) as ctx:
            self._run(archive)
        self.assertIn("incomplete", str(ctx.exception))
        self.assertIn("app/web/templates/index.html", str(ctx.exception))
        self.assertIn("content/drone.png", str(ctx.exception))
        self.assertFalse((self.work_dir / "app").exists())

    def test_rejects_archive_whose_version_does_not_match_the_checked_release(self):
        archive = _targz(_complete_drone_members([("app/VERSION", b"v1.2.2\n")]))
        env = {
            "DRONE_APP_WORK_DIR": str(self.work_dir),
            "DRONE_APP_ARCHIVE_URL": "http://test.invalid/drone-app.tar.gz",
        }
        with mock.patch.dict("os.environ", env), \
             mock.patch.object(self_update, "urlopen", lambda request, timeout=None: io.BytesIO(archive)):
            with self.assertRaises(ValueError) as ctx:
                self_update._download_latest_drone_app_unlocked(self.settings, release_version="v1.2.3")
        self.assertIn("version mismatch", str(ctx.exception))
        self.assertFalse((self.work_dir / "app").exists())

    def test_empty_download_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._run(b"")
        self.assertIn("empty", str(ctx.exception))


class DownloadLatestDroneAppNotificationTests(unittest.TestCase):
    """The single choke point behind both the manual "Update Drone" button and
    the auto-update poller must record a drone_updated notification on a real
    success, and must not record one when the download fails."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.work_dir = self.root / "work"
        env = {
            "USERDATA_ROOT": str(self.root / "userdata"),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(self.root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "self-update-notify-test",
            "DRONE_APP_WORK_DIR": str(self.work_dir),
            "DRONE_APP_ARCHIVE_URL": "http://test.invalid/drone-app.tar.gz",
        }
        self._env_patch = mock.patch.dict("os.environ", env, clear=True)
        self._env_patch.start()
        self.settings = Settings.from_env()
        # Ports client update is covered separately in
        # DownloadLatestPortsClientTests -- stubbed here (this class uses a
        # real Settings with a real roms_root, so without this it would
        # actually attempt a second download through the same mocked
        # urlopen each test patches in below, extracting the wrong fixture
        # bytes into roms_root/ports).
        self._ports_client_patch = mock.patch.object(
            self_update, "_download_latest_ports_client", return_value={"status": "test-noop"}
        )
        self._ports_client_patch.start()

    def tearDown(self):
        self._ports_client_patch.stop()
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_successful_update_records_a_drone_updated_notification(self):
        (self.work_dir / "app").mkdir(parents=True)
        (self.work_dir / "app" / "VERSION").write_text("v1.0.0\n", encoding="utf-8")
        archive = _targz(_complete_drone_members([
            ("app/main.py", b"m"),
            ("app/VERSION", b"v1.0.1\n"),
            ("content/theme.css", b"c"),
        ]))
        with mock.patch.object(self_update, "urlopen", lambda request, timeout=None: io.BytesIO(archive)):
            self_update._download_latest_drone_app(self.settings)

        events = audit_store.list_unsent_events(self.settings, event_types=["drone_updated"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "Drone app updated")
        self.assertIn("v1.0.0", events[0]["message"])
        self.assertIn("v1.0.1", events[0]["message"])

    def test_failed_update_does_not_record_a_notification(self):
        with mock.patch.object(self_update, "urlopen", lambda request, timeout=None: io.BytesIO(b"")):
            with self.assertRaises(ValueError):
                self_update._download_latest_drone_app(self.settings)

        events = audit_store.list_unsent_events(self.settings, event_types=["drone_updated"])
        self.assertEqual(events, [])

    def test_successful_update_records_history_and_includes_notes_in_notification(self):
        (self.work_dir / "app").mkdir(parents=True)
        (self.work_dir / "app" / "VERSION").write_text("v1.0.0\n", encoding="utf-8")
        archive = _targz(_complete_drone_members([
            ("app/main.py", b"m"),
            ("app/VERSION", b"v1.0.1\n"),
            ("content/theme.css", b"c"),
        ]))
        with mock.patch.object(self_update, "urlopen", lambda request, timeout=None: io.BytesIO(archive)), \
             mock.patch.object(self_update, "_fetch_commit_notes", return_value="- did a thing (abc1234)"):
            self_update._download_latest_drone_app(self.settings)

        events = audit_store.list_unsent_events(self.settings, event_types=["drone_updated"])
        self.assertIn("did a thing", events[0]["message"])

        history = update_history_store.list_updates(self.settings)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["version"], "v1.0.1")
        self.assertEqual(history[0]["previous_version"], "v1.0.0")
        self.assertEqual(history[0]["release_notes"], "- did a thing (abc1234)")
        self.assertEqual(
            history[0]["release_url"],
            "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/tag/v1.0.1",
        )

    def test_notification_message_has_no_notes_block_when_notes_are_unavailable(self):
        (self.work_dir / "app").mkdir(parents=True)
        (self.work_dir / "app" / "VERSION").write_text("v1.0.0\n", encoding="utf-8")
        archive = _targz(_complete_drone_members([
            ("app/main.py", b"m"),
            ("app/VERSION", b"v1.0.1\n"),
            ("content/theme.css", b"c"),
        ]))
        with mock.patch.object(self_update, "urlopen", lambda request, timeout=None: io.BytesIO(archive)), \
             mock.patch.object(self_update, "_fetch_commit_notes", return_value=""):
            self_update._download_latest_drone_app(self.settings)

        events = audit_store.list_unsent_events(self.settings, event_types=["drone_updated"])
        self.assertEqual(events[0]["message"], "v1.0.0 -> v1.0.1; restarting to apply.")
        history = update_history_store.list_updates(self.settings)
        self.assertEqual(history[0]["release_notes"], "")


class DownloadLatestPortsClientTests(unittest.TestCase):
    """The API worker installs the Ports bundle as a required part of the
    same release as the web/API app. Both manual and periodic UI actions use
    that worker; neither UI owns this download or extraction path.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = types.SimpleNamespace(roms_root=self.root / "roms")

    def tearDown(self):
        self._tmp.cleanup()

    def _bundle(self, members):
        # ports-client/scripts/build_release_bundle.sh's own tar invocation
        # (tar -C "$STAGE" -czf "$TARBALL" .) prefixes every member with
        # "./" -- real bundles look like this, not bare paths.
        complete = {
            "batocera-drone-client.sh": b"#!/bin/sh\n",
            ".data/batocera-drone-client/main.py": b"main",
            ".data/batocera-drone-client/client/endpoints.py": b"endpoints",
            ".data/batocera-drone-client/ui/app.py": b"app",
            ".data/batocera-drone-client/ui/assets/logo.png": b"png",
        }
        complete.update(dict(members))
        return _targz([(f"./{name}", content) for name, content in complete.items()])

    def _run_unlocked(self, archive_bytes, *, arch="x86_64"):
        # Exercises _download_latest_ports_client_unlocked directly (not the
        # try/except-wrapped _download_latest_ports_client below) so a bad
        # archive's ValueError is actually observable in these tests --
        # the wrapper deliberately swallows it (see
        # test_wrapper_never_raises_and_reports_the_error).
        with mock.patch.object(self_update.platform, "machine", return_value=arch), \
             mock.patch.object(self_update, "urlopen", lambda request, timeout=None: io.BytesIO(archive_bytes)):
            return self_update._download_latest_ports_client_unlocked(self.settings)

    def test_extracts_launcher_and_app_tree_and_restores_launcher_executable_bit(self):
        archive = self._bundle([
            ("batocera-drone-client.sh", b"#!/bin/sh\nexec python3 ...\n"),
            (".data/batocera-drone-client/main.py", b"m"),
            (".data/batocera-drone-client/vendor/x86_64/imgui_bundle/__init__.py", b"v"),
        ])
        result = self._run_unlocked(archive)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["arch"], "x86_64")
        self.assertEqual(result["copied_files"], 6)

        ports_dir = self.root / "roms" / "ports"
        launcher = ports_dir / "batocera-drone-client.sh"
        self.assertEqual(launcher.read_bytes(), b"#!/bin/sh\nexec python3 ...\n")
        self.assertTrue(launcher.stat().st_mode & 0o111, "launcher must stay executable")
        self.assertEqual((ports_dir / ".data" / "batocera-drone-client" / "main.py").read_bytes(), b"m")
        self.assertEqual(
            (ports_dir / ".data" / "batocera-drone-client" / "vendor" / "x86_64" / "imgui_bundle" / "__init__.py").read_bytes(),
            b"v",
        )
        self.assertEqual(
            (ports_dir / ".data" / "batocera-drone-client" / "ui" / "assets" / "logo.png").read_bytes(),
            b"png",
        )

    def test_uses_the_per_arch_release_asset_url(self):
        archive = self._bundle([("batocera-drone-client.sh", b"x")])
        result = self._run_unlocked(archive, arch="aarch64")
        self.assertEqual(
            result["archive_url"],
            "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/"
            "download/batocera-drone-client-aarch64.tar.gz",
        )

    def test_pins_ports_bundle_to_the_release_selected_by_the_worker(self):
        archive = self._bundle([])
        with mock.patch.object(self_update.platform, "machine", return_value="x86_64"), \
             mock.patch.object(self_update, "urlopen", lambda request, timeout=None: io.BytesIO(archive)):
            result = self_update._download_latest_ports_client_unlocked(
                self.settings,
                release_version="v2.3.4",
            )
        self.assertEqual(
            result["archive_url"],
            "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/download/"
            "v2.3.4/batocera-drone-client-x86_64.tar.gz",
        )

    def test_unsupported_arch_skips_without_touching_the_network(self):
        with mock.patch.object(self_update.platform, "machine", return_value="riscv64"), \
             mock.patch.object(self_update, "urlopen") as urlopen_mock:
            result = self_update._download_latest_ports_client(self.settings)
        self.assertEqual(result, {"status": "unsupported_arch", "arch": "riscv64"})
        urlopen_mock.assert_not_called()

    def test_rejects_parent_traversal_member(self):
        archive = self._bundle([
            ("batocera-drone-client.sh", b"x"),
            ("../../pwned.txt", b"evil"),
        ])
        with self.assertRaises(ValueError) as ctx:
            self._run_unlocked(archive)
        self.assertIn("escapes", str(ctx.exception))
        self.assertFalse((self.root / "pwned.txt").exists())

    def test_empty_download_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self._run_unlocked(b"")
        self.assertIn("empty", str(ctx.exception))

    def test_rejects_bundle_missing_runtime_code_or_logo(self):
        archive = _targz([
            ("./batocera-drone-client.sh", b"#!/bin/sh\n"),
            ("./.data/batocera-drone-client/main.py", b"main"),
        ])
        with self.assertRaises(ValueError) as ctx:
            self._run_unlocked(archive)
        self.assertIn("Ports client archive is incomplete", str(ctx.exception))
        self.assertIn("ui/assets/logo.png", str(ctx.exception))
        self.assertFalse((self.root / "roms" / "ports" / "batocera-drone-client.sh").exists())

    # --- the wrapper the actual update flow calls -------------------------
    # (_download_latest_ports_client, not _download_latest_ports_client_unlocked)

    def test_wrapper_never_raises_and_reports_the_error(self):
        with mock.patch.object(self_update.platform, "machine", return_value="x86_64"), \
             mock.patch.object(self_update, "urlopen", side_effect=OSError("no network")):
            result = self_update._download_latest_ports_client(self.settings)
        self.assertEqual(result["status"], "error")
        self.assertIn("no network", result["error"])

    def test_a_ports_client_failure_keeps_the_app_version_retryable(self):
        # A complete release includes both UIs. If the Ports asset is missing,
        # abort before app/VERSION advances so the next worker poll retries.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_dir = root / "work"
            env = {
                "USERDATA_ROOT": str(root / "userdata"),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "ports-client-failure-test",
                "DRONE_APP_WORK_DIR": str(work_dir),
                "DRONE_APP_ARCHIVE_URL": "http://test.invalid/drone-app.tar.gz",
            }
            app_archive = _targz([("app/main.py", b"m"), ("app/VERSION", b"v1.0.1\n"), ("content/x.css", b"c")])

            def fake_urlopen(request, timeout=None):
                url = request.full_url if hasattr(request, "full_url") else str(request)
                if "drone-app.tar.gz" in url:
                    return io.BytesIO(app_archive)
                raise OSError("Ports client release asset unreachable")

            # _drone_work_dir/_download_latest_drone_app_unlocked (and, for
            # the events readback below, audit_store's own DB path lookup)
            # all read env vars directly at call time, not from the settings
            # object -- so the env patch has to stay active for everything
            # below, not just for building settings.
            with mock.patch.dict("os.environ", env, clear=True):
                settings = Settings.from_env()
                with mock.patch.object(self_update, "urlopen", side_effect=fake_urlopen), \
                     mock.patch.object(self_update.platform, "machine", return_value="x86_64"):
                    with self.assertRaises(RuntimeError) as error:
                        self_update._download_latest_drone_app(settings)

                self.assertIn("Ports client bundle is required", str(error.exception))
                self.assertFalse((work_dir / "app" / "VERSION").exists())
                events = audit_store.list_unsent_events(settings, event_types=["drone_updated"])
                self.assertEqual(events, [])


class FetchCommitNotesTests(unittest.TestCase):
    def test_returns_empty_when_no_previous_version(self) -> None:
        self.assertEqual(self_update._fetch_commit_notes("", "v1.0.1"), "")

    def test_returns_empty_when_versions_are_identical(self) -> None:
        self.assertEqual(self_update._fetch_commit_notes("v1.0.0", "v1.0.0"), "")

    def test_builds_bullet_list_from_compare_api_commits(self) -> None:
        payload = json.dumps({
            "commits": [
                {"sha": "abc1234567890", "commit": {"message": "Fix a bug\n\nlonger body text"}},
                {"sha": "def4567890123", "commit": {"message": "Add a feature"}},
            ]
        }).encode("utf-8")
        with mock.patch.object(self_update, "urlopen", lambda request, timeout=None: io.BytesIO(payload)):
            notes = self_update._fetch_commit_notes("v1.0.0", "v1.0.1")
        self.assertEqual(notes, "- Fix a bug (abc1234)\n- Add a feature (def4567)")

    def test_caps_at_max_commits_with_a_remaining_count(self) -> None:
        commits = [
            {"sha": f"{i:07d}", "commit": {"message": f"change {i}"}}
            for i in range(self_update.RELEASE_NOTES_MAX_COMMITS + 5)
        ]
        payload = json.dumps({"commits": commits}).encode("utf-8")
        with mock.patch.object(self_update, "urlopen", lambda request, timeout=None: io.BytesIO(payload)):
            notes = self_update._fetch_commit_notes("v1.0.0", "v1.0.1")
        lines = notes.splitlines()
        self.assertEqual(len(lines), self_update.RELEASE_NOTES_MAX_COMMITS + 1)
        self.assertEqual(lines[-1], "... and 5 more commit(s)")

    def test_returns_empty_on_network_failure_without_raising(self) -> None:
        def raise_error(request, timeout=None):
            raise URLError("no network")

        with mock.patch.object(self_update, "urlopen", raise_error):
            notes = self_update._fetch_commit_notes("v1.0.0", "v1.0.1")
        self.assertEqual(notes, "")

    def test_returns_empty_on_malformed_json_without_raising(self) -> None:
        with mock.patch.object(self_update, "urlopen", lambda request, timeout=None: io.BytesIO(b"not json")):
            notes = self_update._fetch_commit_notes("v1.0.0", "v1.0.1")
        self.assertEqual(notes, "")


class DroneUpdateHistoryAdminHandlerTests(unittest.TestCase):
    class _FakeHandler:
        def __init__(self, settings) -> None:
            self.settings = settings
            self.response = None

        def _send_json(self, status_code: int, payload: dict) -> None:
            self.response = (status_code, payload)

    def _handler(self, settings):
        from app.web import handlers_system

        class FakeHandler(handlers_system.HandlersSystemMixin, self._FakeHandler):
            pass

        return FakeHandler(settings)

    def _build_settings(self, root: Path) -> Settings:
        env = {
            "USERDATA_ROOT": str(root / "userdata"),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "update-history-handler-test",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            return Settings.from_env()

    def test_returns_recorded_updates_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._build_settings(Path(tmp))
            update_history_store.record_update(settings, version="v1.0.0")
            update_history_store.record_update(
                settings, version="v1.0.1", previous_version="v1.0.0", release_notes="- did a thing"
            )
            handler = self._handler(settings)
            handler._handle_admin_drone_update_history()
            status_code, payload = handler.response
            self.assertEqual(status_code, 200)
            self.assertEqual([entry["version"] for entry in payload["updates"]], ["v1.0.1", "v1.0.0"])

    def test_returns_empty_list_when_never_updated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._build_settings(Path(tmp))
            handler = self._handler(settings)
            handler._handle_admin_drone_update_history()
            self.assertEqual(handler.response, (200, {"updates": []}))


class DroneManualUpdateAdminHandlerTests(unittest.TestCase):
    """The manual button submits work; it never downloads in the HTTP thread."""

    class _FakeHandler:
        def __init__(self, settings) -> None:
            self.settings = settings
            self.response = None

        def _send_json(self, status_code: int, payload: dict) -> None:
            self.response = (status_code, payload)

    def _handler(self, settings):
        from app.web import handlers_system

        class FakeHandler(handlers_system.HandlersSystemMixin, self._FakeHandler):
            pass

        return FakeHandler(settings)

    def test_submits_to_the_api_worker_and_returns_immediately(self) -> None:
        from app.web import handlers_system

        settings = types.SimpleNamespace()
        handler = self._handler(settings)
        queued = {"status": "queued", "owner": "api_worker", "accepted": True}
        with mock.patch.object(handlers_system, "request_drone_update_check", return_value=queued) as request:
            handler._handle_admin_drone_update()

        request.assert_called_once_with(settings, source="manual")
        status_code, payload = handler.response
        self.assertEqual(status_code, 202)
        self.assertEqual(payload, queued)

    def test_status_route_only_reads_worker_state(self) -> None:
        from app.web import handlers_system

        settings = types.SimpleNamespace()
        handler = self._handler(settings)
        state = {"status": "downloading", "owner": "api_worker"}
        with mock.patch.object(handlers_system, "get_drone_update_status", return_value=state) as get_status:
            handler._handle_admin_drone_update_status()

        get_status.assert_called_once_with(settings)
        self.assertEqual(handler.response, (200, state))


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
        download.assert_called_once_with(self.settings, release_version="v1.2.4")
        restart.assert_called_once_with()

    def test_poller_runs_on_daemon_thread_without_blocking_caller(self):
        stopped = threading.Event()
        checked = threading.Event()

        def check_once(settings, **kwargs):
            self.assertTrue(kwargs["respect_auto_update_setting"])
            checked.set()
            stopped.set()
            return {"status": "current"}

        with mock.patch.object(self_update, "_run_drone_auto_update_check_once", side_effect=check_once):
            thread = self_update._start_drone_auto_update_poller(self.settings, poll_seconds=0.01, stop_event=stopped)
            self.assertIsNotNone(thread)
            self.assertTrue(thread.daemon)
            self.assertTrue(checked.wait(1))
            thread.join(timeout=1)
            worker = self_update._DRONE_UPDATE_WORKERS.get(str(self.work_dir.resolve()))
            self.assertIsNotNone(worker)
            worker.join(timeout=1)

    def test_manual_request_runs_check_on_named_api_worker_and_persists_status(self):
        completed = threading.Event()

        def check_once(settings, **kwargs):
            self.assertFalse(kwargs["respect_auto_update_setting"])
            kwargs["progress_callback"]("checking", "checking now")
            completed.set()
            return {"status": "current", "current_version": "v1.2.3", "latest_version": "v1.2.3"}

        with mock.patch.object(self_update, "_run_drone_auto_update_check_once", side_effect=check_once):
            queued = self_update.request_drone_update_check(self.settings, source="manual")
            self.assertTrue(completed.wait(1))
            for thread in list(self_update._DRONE_UPDATE_WORKERS.values()):
                thread.join(timeout=1)

        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["owner"], "api_worker")
        status = self_update.get_drone_update_status(self.settings)
        self.assertEqual(status["status"], "current")
        self.assertEqual(status["owner"], "api_worker")


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


class UpdateHistoryUiContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.js = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")

    def test_system_info_page_fetches_and_renders_update_history(self) -> None:
        self.assertIn('api("/admin/system/update-history")', self.js)
        self.assertIn("function renderUpdateHistorySection(updates)", self.js)
        self.assertIn("function renderUpdateHistoryEntry(entry)", self.js)
        self.assertIn("Update History", self.js)
        page_start = self.js.index("async function renderAdminSystemInfoPage()")
        page_end = self.js.index("\nasync function renderAdminControlsPage()")
        self.assertIn("renderUpdateHistorySection(updateHistory)", self.js[page_start:page_end])

    def test_update_drone_button_only_submits_and_monitors_the_backend_worker(self) -> None:
        page_start = self.js.index("async function updateDroneApp()")
        page_end = self.js.index("\nasync function setDroneAutoUpdate(")
        section = self.js[page_start:page_end]
        self.assertIn('apiPost("/admin/system/update-drone", {})', section)
        self.assertIn("API worker owns the download and install", section)
        self.assertIn("void monitorDroneUpdateWorker()", section)
        monitor = self.js[
            self.js.index("async function monitorDroneUpdateWorker()"):
            page_start
        ]
        self.assertIn('api("/admin/system/update-drone")', monitor)
        self.assertIn("Web/API code, images, and the Ports client are installed", monitor)


if __name__ == "__main__":
    unittest.main()
