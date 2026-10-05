"""Integration and UAT coverage for issue #96: release staging and opt-in source fallback.

Normal ``scripts/run_web_now.sh`` installs must use a published, versioned
``drone-app.tar.gz``. A failed or incomplete release is rejected in a staging
directory without mutating the live tree and without fetching GitHub
codeload. Source archives require ``--dev`` or ``DRONE_APP_DEVELOPMENT=1``.
An existing ``VERSION=dev`` install still converges through the self-updater
to a semantic release.
"""
from __future__ import annotations

import io
import os
import socket
import subprocess
import tarfile
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.common import self_update


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_web_now.sh"


def _targz(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                payload = content if isinstance(content, bytes) else content.encode("utf-8")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _complete_install_members(members=(), *, version="v0.1.245"):
    required = {
        "app/__init__.py": b"# package\n",
        "app/main.py": b"# staged main\n",
        "app/drone_api.py": b"# staged drone_api\n",
        "app/VERSION": f"{version}\n".encode("utf-8"),
        "app/web/__init__.py": b"# web package\n",
        "app/web/api_routes.py": b"class ApiRoutesMixin:\n    pass\n",
        "app/web/ui_routes.py": b"class UiRoutesMixin:\n    pass\n",
        "app/web/route_config.py": b"ROUTES = {}\n",
        "app/web/templates/index.html": b"<html>drone</html>\n",
        "app/web/static/js/drone.js": b"function drone() {}\n",
        "app/web/static/js/integrations.js": b"function renderIntegrationsPage() {}\n",
        "app/web/static/css/drone.css": b".drone {}\n",
        "app/web/handlers_integrations.py": b"# handlers\n",
        "app/integrations/__init__.py": b"# integrations\n",
        "app/integrations/registry.py": b"class IntegrationRegistry:\n    pass\n",
        "app/integrations/streamdeck/manager.py": b"class StreamDeckManager:\n    pass\n",
        "content/batocera-swarm-mascot.jpg": b"jpg-bytes",
        "content/drone.png": b"png-bytes",
    }
    required.update(dict(members))
    return list(required.items())


class _ArchiveHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A003
        return

    def do_GET(self):  # noqa: N802
        self.server.requests.append(self.path.split("?", 1)[0])
        body = self.server.files.get(self.path.split("?", 1)[0])
        if body is None:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ArchiveServer:
    def __init__(self, files):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ArchiveHandler)
        self._httpd.files = dict(files)
        self._httpd.requests = []
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def origin(self):
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def requests(self):
        return list(self._httpd.requests)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


class RunWebNowStagingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name) / "drone-app"
        self.work_dir.mkdir()
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.stop()
        self._tmp.cleanup()

    def _seed_installed(self, *, version="v0.1.244"):
        app = self.work_dir / "app"
        content = self.work_dir / "content"
        (app / "web" / "static" / "js").mkdir(parents=True)
        content.mkdir(parents=True)
        (app / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        (app / "main.py").write_text("original-main\n", encoding="utf-8")
        (app / "web" / "static" / "js" / "integrations.js").write_text("original-integrations\n", encoding="utf-8")
        (content / "keep-me.txt").write_text("keep\n", encoding="utf-8")
        (content / "drone.png").write_text("original-png\n", encoding="utf-8")

    def _run(self, extra_env, *args):
        env = os.environ.copy()
        env.update({
            "DRONE_APP_WORK_DIR": str(self.work_dir),
            "DRONE_APP_STAGE_ONLY": "1",
            "DRONE_APP_DEVELOPMENT": "0",
            "DRONE_APP_BASE_URL": extra_env.get("DRONE_APP_BASE_URL", "http://127.0.0.1/unused"),
        })
        env.update(extra_env)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def _assert_existing_install_intact(self, *, version="v0.1.244"):
        self.assertEqual((self.work_dir / "app" / "VERSION").read_text(encoding="utf-8").strip(), version)
        self.assertEqual((self.work_dir / "app" / "main.py").read_text(encoding="utf-8"), "original-main\n")
        self.assertEqual(
            (self.work_dir / "app" / "web" / "static" / "js" / "integrations.js").read_text(encoding="utf-8"),
            "original-integrations\n",
        )
        self.assertEqual((self.work_dir / "content" / "keep-me.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertFalse(any(self.work_dir.glob(".incoming*")))

    def test_release_download_failure_does_not_fetch_or_install_source(self):
        source = _targz(_complete_install_members(version="dev"))
        self.server = _ArchiveServer({"/source.tar.gz": source}).start()
        self._seed_installed()
        result = self._run({
            "DRONE_APP_ARCHIVE_URL": f"{self.server.origin}/missing-release.tar.gz",
            "DRONE_APP_FALLBACK_ARCHIVE_URL": f"{self.server.origin}/source.tar.gz",
            "DRONE_APP_BASE_URL": self.server.origin,
        })
        self.assertNotEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Failed to download a published Drone release", result.stdout + result.stderr)
        self.assertIn("Ignoring source/codeload fallback", result.stdout + result.stderr)
        self.assertIn("/missing-release.tar.gz", self.server.requests)
        self.assertNotIn("/source.tar.gz", self.server.requests)
        self._assert_existing_install_intact()

    def test_incomplete_release_is_rejected_without_changing_install(self):
        incomplete = _targz(_complete_install_members([
            ("app/web/static/js/integrations.js", b""),
        ]))
        self.server = _ArchiveServer({"/drone-app.tar.gz": incomplete}).start()
        self._seed_installed()
        result = self._run({
            "DRONE_APP_ARCHIVE_URL": f"{self.server.origin}/drone-app.tar.gz",
            "DRONE_APP_FALLBACK_ARCHIVE_URL": f"{self.server.origin}/source.tar.gz",
            "DRONE_APP_BASE_URL": self.server.origin,
        })
        self.assertNotEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Missing or empty required file", result.stdout + result.stderr)
        self.assertNotIn("/source.tar.gz", self.server.requests)
        self._assert_existing_install_intact()

    def test_unversioned_release_is_rejected_without_changing_install(self):
        unversioned = _targz(_complete_install_members(version="dev"))
        self.server = _ArchiveServer({"/drone-app.tar.gz": unversioned}).start()
        self._seed_installed()
        result = self._run({
            "DRONE_APP_ARCHIVE_URL": f"{self.server.origin}/drone-app.tar.gz",
            "DRONE_APP_FALLBACK_ARCHIVE_URL": f"{self.server.origin}/source.tar.gz",
            "DRONE_APP_BASE_URL": self.server.origin,
        })
        self.assertNotEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Rejected unversioned or development payload", result.stdout + result.stderr)
        self.assertNotIn("/source.tar.gz", self.server.requests)
        self._assert_existing_install_intact()

    def test_fresh_install_with_failed_release_does_not_launch_unversioned_app(self):
        source = _targz(_complete_install_members(version="dev"))
        self.server = _ArchiveServer({"/source.tar.gz": source}).start()
        result = self._run({
            "DRONE_APP_ARCHIVE_URL": f"{self.server.origin}/missing-release.tar.gz",
            "DRONE_APP_FALLBACK_ARCHIVE_URL": f"{self.server.origin}/source.tar.gz",
            "DRONE_APP_BASE_URL": self.server.origin,
            "DRONE_APP_STAGE_ONLY": "0",
            "HTTPS_PORT": str(_unused_port()),
        })
        self.assertNotEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse((self.work_dir / "app" / "VERSION").exists())
        self.assertFalse((self.work_dir / "app" / "main.py").exists())
        self.assertNotIn("/source.tar.gz", self.server.requests)
        self.assertNotIn("python3 -m app.main", result.stdout)
        self.assertIn("Refusing to change the installed Drone App", result.stdout + result.stderr)

    def test_explicit_development_mode_installs_source_and_reports_dev(self):
        source = _targz(_complete_install_members(version="dev"))
        self.server = _ArchiveServer({"/source.tar.gz": source}).start()
        result = self._run({
            "DRONE_APP_ARCHIVE_URL": f"{self.server.origin}/missing-release.tar.gz",
            "DRONE_APP_FALLBACK_ARCHIVE_URL": f"{self.server.origin}/source.tar.gz",
            "DRONE_APP_BASE_URL": self.server.origin,
            "DRONE_APP_DEVELOPMENT": "1",
        })
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("/source.tar.gz", self.server.requests)
        self.assertEqual((self.work_dir / "app" / "VERSION").read_text(encoding="utf-8").strip(), "dev")
        self.assertIn("Installed Drone App version dev", result.stdout)
        self.assertTrue((self.work_dir / "app" / "web" / "static" / "js" / "integrations.js").is_file())
        self.assertTrue((self.work_dir / "content" / "drone.png").is_file())

    def test_dev_flag_installs_source_archive(self):
        source = _targz(_complete_install_members(version="dev"))
        self.server = _ArchiveServer({"/source.tar.gz": source}).start()
        result = self._run(
            {
                "DRONE_APP_ARCHIVE_URL": f"{self.server.origin}/missing-release.tar.gz",
                "DRONE_APP_FALLBACK_ARCHIVE_URL": f"{self.server.origin}/source.tar.gz",
                "DRONE_APP_BASE_URL": self.server.origin,
            },
            "--dev",
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((self.work_dir / "app" / "VERSION").read_text(encoding="utf-8").strip(), "dev")
        self.assertIn("Installed Drone App version dev", result.stdout)

    def test_successful_normal_install_reports_release_version_and_assets(self):
        release = _targz(_complete_install_members(version="v0.1.245"))
        self.server = _ArchiveServer({"/drone-app.tar.gz": release}).start()
        result = self._run({
            "DRONE_APP_ARCHIVE_URL": f"{self.server.origin}/drone-app.tar.gz",
            "DRONE_APP_FALLBACK_ARCHIVE_URL": f"{self.server.origin}/source.tar.gz",
            "DRONE_APP_BASE_URL": self.server.origin,
        })
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("/source.tar.gz", self.server.requests)
        self.assertIn("Installed Drone App version v0.1.245", result.stdout)
        self.assertEqual((self.work_dir / "app" / "VERSION").read_text(encoding="utf-8").strip(), "v0.1.245")
        self.assertIn("renderIntegrationsPage", (self.work_dir / "app" / "web" / "static" / "js" / "integrations.js").read_text(encoding="utf-8"))
        self.assertTrue((self.work_dir / "app" / "web" / "handlers_integrations.py").is_file())
        self.assertTrue((self.work_dir / "app" / "integrations" / "registry.py").is_file())
        self.assertTrue((self.work_dir / "app" / "integrations" / "streamdeck" / "manager.py").is_file())
        self.assertTrue((self.work_dir / "app" / "web" / "api_routes.py").is_file())
        self.assertTrue((self.work_dir / "content" / "drone.png").is_file())
        self.assertTrue((self.work_dir / "content" / "batocera-swarm-mascot.jpg").is_file())
        self.assertFalse(any(self.work_dir.glob(".incoming*")))


class ExistingDevInstallUpdateUATTests(unittest.TestCase):
    """UAT: a live ``dev`` tree still auto-upgrades to a published semantic release."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.work_dir = self.root / "drone-app"
        (self.work_dir / "app").mkdir(parents=True)
        (self.work_dir / "app" / "VERSION").write_text("dev\n", encoding="utf-8")
        (self.work_dir / "app" / "main.py").write_text("dev-main\n", encoding="utf-8")
        self.settings = types.SimpleNamespace(userdata_root=self.root / "userdata")
        self.env = mock.patch.dict("os.environ", {
            "DRONE_APP_WORK_DIR": str(self.work_dir),
            "DRONE_APP_ARCHIVE_URL": "http://test.invalid/drone-app.tar.gz",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def test_existing_dev_installation_overlays_published_semantic_release(self):
        archive = _targz(_complete_install_members([
            ("app/main.py", b"release-main\n"),
            ("app/web/static/js/integrations.js", b"release-integrations\n"),
        ], version="v0.1.245"))
        with mock.patch.object(self_update, "_latest_drone_release_version", return_value="v0.1.245"), \
             mock.patch.object(self_update, "urlopen", lambda request, timeout=None: io.BytesIO(archive)), \
             mock.patch.object(self_update, "_download_latest_ports_client", return_value={"status": "test-noop"}), \
             mock.patch.object(self_update, "_restart_drone_process_soon") as restart:
            result = self_update._run_drone_auto_update_check_once(self.settings)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["current_version"], "dev")
        self.assertEqual(result["latest_version"], "v0.1.245")
        restart.assert_called_once_with()
        self.assertEqual((self.work_dir / "app" / "VERSION").read_text(encoding="utf-8").strip(), "v0.1.245")
        self.assertEqual((self.work_dir / "app" / "main.py").read_text(encoding="utf-8"), "release-main\n")
        self.assertEqual(
            (self.work_dir / "app" / "web" / "static" / "js" / "integrations.js").read_text(encoding="utf-8"),
            "release-integrations\n",
        )


def _unused_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    unittest.main()
