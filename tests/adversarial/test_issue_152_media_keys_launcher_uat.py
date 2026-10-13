"""Issue #152 UAT: the generated ``drone-media-key`` launcher really reaches the dispatcher.

hotkeygen runs only the launcher the Drone writes. This copies the Drone package
into an isolated install root, lets the integration generate its launcher and
override, then runs the launcher exactly as hotkeygen would (a subprocess with a
minimal environment). Unit tests inject the dispatcher directly and would miss a
launcher that cannot import the dispatcher, resolves the wrong install root, or
hangs the key. No Batocera tools exist on the PATH used here, so built-ins end in
"unavailable"; nothing reboots and nothing outside the temp directory is touched.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _copy_package(destination: Path) -> None:
    app = destination / "app"
    app.mkdir(parents=True)
    shutil.copy2(REPO / "app" / "__init__.py", app / "__init__.py")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in ("common", "integrations"):
        shutil.copytree(REPO / "app" / name, app / name, ignore=ignore)


class LauncherUatTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.install = self.base / "drone-app"
        _copy_package(self.install)
        self.user_dir = self.base / "hotkeygen"
        self.root = self.install / "integrations" / "media-keys"

    def _run_in_package(self, code: str) -> str:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.base), "PYTHONDONTWRITEBYTECODE": "1"}
        done = subprocess.run(["python3", "-I", "-c", code], env=env, capture_output=True, text=True, timeout=60,
                              cwd=str(self.base))
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def _enable(self) -> None:
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from app.integrations.media_keys.manager import MediaKeysIntegration\n"
            "from app.integrations.media_keys.hotkeygen import HotkeygenOverrides\n"
            "from app.integrations.media_keys.paths import MediaKeysPaths\n"
            "paths = MediaKeysPaths.default()\n"
            "assert paths.root == Path(%r), paths.root\n"
            "m = MediaKeysIntegration(None, None, paths=paths, overrides=HotkeygenOverrides(paths, user_dir=Path(%r)))\n"
            "print(m.enable()['status'])\n" % (str(self.install), str(self.root), str(self.user_dir))
        )
        self.assertEqual(self._run_in_package(code).strip().splitlines()[-1], "ok")

    def _press(self, binding_id: str) -> subprocess.CompletedProcess:
        launcher = self.root / "bin" / "drone-media-key"
        self.assertTrue(os.access(launcher, os.X_OK), "launcher must be executable")
        # hotkeygen gives a bare environment: no PYTHONPATH, no Batocera tools on PATH.
        python_dir = str(Path(shutil.which("python3")).parent)
        env = {"PATH": f"{python_dir}:/usr/bin:/bin", "HOME": str(self.base)}
        started = time.monotonic()
        done = subprocess.run(["sh", str(launcher), binding_id], env=env, capture_output=True, text=True,
                              timeout=15, cwd="/")
        self.elapsed = time.monotonic() - started
        return done

    def _wait_for_press(self, binding_id: str, timeout: float = 20.0) -> dict:
        press_file = self.root / "state" / "last-press.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = json.loads(press_file.read_text())
                if binding_id in data:
                    return data[binding_id]
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        self.fail(f"dispatcher never recorded a result for {binding_id}")

    def test_launcher_returns_immediately_and_dispatcher_records_the_press(self):
        self._enable()
        done = self._press("volume-up")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertLess(self.elapsed, 5.0, "the launcher must detach so a slow action never blocks hotkeygen")
        record = self._wait_for_press("volume-up")
        # No batocera-audio/amixer on this PATH: the reviewed built-in reports it, rather than the
        # dispatcher crashing on import or silently doing nothing.
        self.assertEqual(record["status"], "unavailable", record)

    def test_launcher_with_integration_disabled_does_nothing(self):
        self._enable()
        config_path = self.root / "config" / "media-keys.json"
        config = json.loads(config_path.read_text())
        config["enabled"] = False
        config_path.write_text(json.dumps(config))
        done = self._press("volume-up")
        self.assertEqual(done.returncode, 0)
        time.sleep(3.0)
        self.assertFalse((self.root / "state" / "last-press.json").exists(),
                         "a disabled integration must not act on a key press")

    def test_launcher_single_reboot_press_does_nothing(self):
        self._enable()
        self._press("reboot")
        time.sleep(3.0)
        press_file = self.root / "state" / "last-press.json"
        recorded = json.loads(press_file.read_text()) if press_file.exists() else {}
        self.assertNotIn("reboot", recorded, "a single press of the reboot key must not run the action")

    def test_launcher_rejects_unknown_binding_without_error_exit(self):
        self._enable()
        done = self._press("../../etc/passwd")
        self.assertEqual(done.returncode, 0, "the launcher must never wedge hotkeygen")
        time.sleep(2.0)
        self.assertFalse((self.root / "state" / "last-press.json").exists())


if __name__ == "__main__":
    unittest.main()
