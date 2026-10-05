"""Isolated Stream Deck tooling (python-elgato-streamdeck + Pillow).

Nothing is installed into, or removed from, Batocera's system Python. Packages
always land in the integration-owned ``lib/`` (``pip install --target``) and are
only importable by the worker, which prepends that one directory. pip itself is
obtained, in order of preference:

1. ``venv`` -- a private virtual environment in ``python/`` created with the
   system interpreter (its own ensurepip bootstraps pip *inside the venv*);
2. ``bundled-pip`` -- the pip wheel bundled with the stdlib's ``ensurepip``,
   run straight from the wheel exactly the way ensurepip itself runs it,
   without installing pip anywhere.

Installs go to ``lib.staging`` and are swapped in only after an import check
succeeds, so a failed or interrupted install leaves the previous tooling
intact (safe to retry). ``state/dependencies.json`` records versions plus the
interpreter they were built for; startup only checks that marker -- it never
reinstalls on every boot -- and a Python upgrade is detected as "needs repair".
"""

import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

from .paths import StreamDeckPaths, atomic_write_json, read_json
from .process import ProcessRunner


STREAMDECK_REQUIREMENT = "streamdeck==0.10.0"
STREAMDECK_VERSION = "0.10.0"
PILLOW_REQUIREMENT = "Pillow>=9.1"
TOOLING_SCHEMA = 1
PIP_TIMEOUT_SECONDS = 900

_VERIFY_SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import PIL
from PIL import Image
import StreamDeck
from pathlib import Path
root = Path(sys.argv[1]).resolve()
for module in (PIL, StreamDeck):
    Path(module.__file__).resolve().relative_to(root)
from StreamDeck.ImageHelpers import PILHelper
from StreamDeck.DeviceManager import DeviceManager
try:
    from importlib.metadata import version, PackageNotFoundError
    from importlib import metadata
    dists = {d.metadata["Name"].lower(): d.version for d in metadata.distributions(path=[sys.argv[1]])}
except Exception:
    dists = {}
transport = "ok"
try:
    DeviceManager()
except Exception as error:
    transport = "unavailable: %s" % (error,)
print(json.dumps({"streamdeck": dists.get("streamdeck", ""), "pillow": getattr(PIL, "__version__", ""),
                  "python": "%d.%d.%d" % sys.version_info[:3], "hid_transport": transport}))
"""

_BUNDLED_PIP_SHIM = (
    "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); "
    "runpy.run_module('pip', run_name='__main__', alter_sys=True)"
)

Progress = Callable[[str], None]


class DependencyError(RuntimeError):
    pass


def interpreter_tag() -> str:
    return f"{sys.implementation.name}-{sys.version_info[0]}.{sys.version_info[1]}-{platform.machine()}"


class DependencyManager:
    def __init__(self, paths: StreamDeckPaths, runner: Optional[ProcessRunner] = None,
                 python: Optional[str] = None) -> None:
        self.paths = paths
        self.runner = runner or ProcessRunner()
        self.python = python or sys.executable

    # -- status -------------------------------------------------------------------
    def marker(self) -> dict:
        payload = read_json(self.paths.dependency_marker, {})
        return payload if isinstance(payload, dict) else {}

    def status(self) -> dict:
        marker = self.marker()
        reason = ""
        if not marker:
            reason = "Stream Deck tooling is not installed."
        elif marker.get("schema") != TOOLING_SCHEMA or marker.get("streamdeck") != STREAMDECK_VERSION:
            reason = "Installed tooling is from a different version; repair to update it."
        elif marker.get("interpreter") != interpreter_tag():
            reason = "Batocera's Python changed since the tooling was installed; repair to rebuild it."
        elif not (self.paths.lib_dir / "StreamDeck" / "__init__.py").is_file() or not (self.paths.lib_dir / "PIL" / "__init__.py").is_file():
            reason = "Installed tooling files are missing; repair to reinstall them."
        return {
            "installed": not reason,
            "reason": reason,
            "versions": {key: marker.get(key, "") for key in ("streamdeck", "pillow", "python")},
            "method": marker.get("method", ""),
            "hid_transport": marker.get("hid_transport", ""),
            "installed_at": marker.get("installed_at", ""),
        }

    def installed(self) -> bool:
        return self.status()["installed"]

    # -- install --------------------------------------------------------------------
    def _log(self, text: str) -> None:
        try:
            self.paths.ensure(self.paths.logs_dir)
            with open(self.paths.assert_owned(self.paths.install_log), "a", encoding="utf-8") as handle:
                handle.write(time.strftime("[%Y-%m-%dT%H:%M:%SZ] ", time.gmtime()) + text.rstrip() + "\n")
        except OSError:
            pass

    def _python_available(self) -> None:
        result = self.runner.run(self.python, ["-c", "import sys; print(sys.version)"], timeout=30)
        if not result.ok:
            raise DependencyError(f"Python 3 is not usable on this system ({result.summary()}).")

    def _venv_pip(self, progress: Optional[Progress]) -> Optional[List[str]]:
        self.paths.assert_owned(self.paths.python_dir)
        venv_python = self.paths.python_dir / "bin" / "python3"
        if venv_python.is_file():
            probe = self.runner.run(str(venv_python), ["-m", "pip", "--version"], timeout=60)
            if probe.ok:
                return [str(venv_python), "-m", "pip"]
        if progress:
            progress("Preparing runtime...")
        self.paths.ensure(self.paths.state_dir)
        created = self.runner.run(self.python, ["-m", "venv", "--clear", str(self.paths.python_dir)], timeout=300)
        self._log(f"venv: exit={created.exit_code} {created.summary()}")
        if created.ok and venv_python.is_file():
            probe = self.runner.run(str(venv_python), ["-m", "pip", "--version"], timeout=60)
            if probe.ok:
                return [str(venv_python), "-m", "pip"]
        if self.paths.python_dir.exists():
            self.paths.remove_owned_tree(self.paths.python_dir)
        return None

    def _bundled_pip(self) -> Optional[List[str]]:
        probe = self.runner.run(
            self.python,
            ["-c", "import ensurepip, pathlib; d = pathlib.Path(ensurepip.__file__).parent / '_bundled'; "
                   "print('\\n'.join(sorted(str(p) for p in d.glob('pip-*.whl'))))"],
            timeout=30,
        )
        wheels = [line.strip() for line in probe.stdout.splitlines() if line.strip()] if probe.ok else []
        if not wheels:
            return None
        return [self.python, "-c", _BUNDLED_PIP_SHIM, wheels[-1]]

    def _pip(self, progress: Optional[Progress]) -> tuple:
        pip = self._venv_pip(progress)
        if pip:
            return pip, "venv"
        pip = self._bundled_pip()
        if pip:
            return pip, "bundled-pip"
        raise DependencyError("No isolated pip is available: Python's venv/ensurepip modules are missing.")

    def ensure(self, progress: Optional[Progress] = None, *, force: bool = False) -> dict:
        """Idempotently install/verify tooling. Returns the status dict."""
        if not force and self.installed():
            return self.status()
        if progress:
            progress("Checking environment...")
        self.paths.ensure(self.paths.state_dir, self.paths.logs_dir)
        self.paths.assert_owned(self.paths.install_log)
        try:
            self.paths.install_log.write_text("", encoding="utf-8")
        except OSError:
            pass
        self._log(f"install start: python={self.python} interpreter={interpreter_tag()} force={force}")
        self._python_available()
        pip, method = self._pip(progress)
        self._log(f"pip method: {method}")
        staging = self.paths.owned("lib.staging")
        if staging.exists():
            self.paths.remove_owned_tree(staging)
        staging.mkdir(mode=0o700)
        env = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": str(self.paths.state_dir),
            "PYTHONNOUSERSITE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        }
        for name in ("http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            if os.environ.get(name):
                env[name] = os.environ[name]
        base =[*pip[1:], "install", "--no-cache-dir", "--prefer-binary", "--no-warn-script-location",
                "--upgrade", "--target", str(staging)]
        for message, requirement in (("Installing Stream Deck support...", STREAMDECK_REQUIREMENT),
                                     ("Installing image support...", PILLOW_REQUIREMENT)):
            if progress:
                progress(message)
            result = self.runner.run(pip[0], [*base, requirement], timeout=PIP_TIMEOUT_SECONDS, env=env)
            self._log(f"pip install {requirement}: exit={result.exit_code}\n{result.stdout[-4000:]}\n{result.stderr[-4000:]}")
            if not result.ok:
                self.paths.remove_owned_tree(staging)
                raise DependencyError(f"Installing {requirement} failed: {result.summary()}")
        if progress:
            progress("Verifying installation...")
        versions = self.verify(staging)
        self._swap_in(staging)
        marker = {
            "schema": TOOLING_SCHEMA, "streamdeck": STREAMDECK_VERSION, "pillow": versions.get("pillow", ""),
            "python": versions.get("python", ""), "interpreter": interpreter_tag(), "method": method,
            "hid_transport": versions.get("hid_transport", ""),
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        atomic_write_json(self.paths.dependency_marker, marker)
        self._log(f"install complete: {json.dumps(marker)}")
        return self.status()

    def verify(self, lib_dir: Optional[Path] = None) -> dict:
        lib_dir = lib_dir or self.paths.lib_dir
        result = self.runner.run(self.python, ["-c", _VERIFY_SCRIPT, str(lib_dir)], timeout=60,
                                 env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONNOUSERSITE": "1"})
        if not result.ok:
            self._log(f"verify failed: {result.stderr[-4000:]}")
            raise DependencyError(f"Installed Stream Deck libraries cannot be imported: {result.summary()}")
        try:
            versions = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as error:
            raise DependencyError("Could not read the installed library versions.") from error
        if versions.get("streamdeck") != STREAMDECK_VERSION or not versions.get("pillow"):
            raise DependencyError("The isolated StreamDeck/Pillow version check failed.")
        return versions

    def _swap_in(self, staging: Path) -> None:
        live = self.paths.owned("lib")
        retired = self.paths.owned("lib.old")
        if retired.exists():
            self.paths.remove_owned_tree(retired)
        if live.exists():
            live.rename(retired)
        try:
            staging.rename(live)
        except OSError:
            if retired.exists() and not live.exists():
                retired.rename(live)
            raise
        if retired.exists():
            self.paths.remove_owned_tree(retired)

    def remove(self) -> List[str]:
        """Remove only integration-owned tooling. Never touches system pip/setuptools."""
        removed = []
        for name in ("lib", "lib.staging", "lib.old", "python"):
            target = self.paths.owned(name)
            if target.exists() or target.is_symlink():
                self.paths.remove_owned_tree(target)
                removed.append(name)
        self.paths.dependency_marker.unlink(missing_ok=True)
        return removed
