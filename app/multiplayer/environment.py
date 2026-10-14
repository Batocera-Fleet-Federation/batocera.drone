"""Read-only probes of the local Batocera machine, behind one injectable seam.

Adapters never touch the filesystem, ``PATH`` or subprocesses directly: they
ask a ``SystemProbe``. That keeps detection honest (it reports what is really
installed, never what is assumed) and lets tests supply a fake machine.
Subprocesses are fixed ``argv`` lists, ``shell=False``, short timeouts.
"""

import glob
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_VERSION = re.compile(r"\d+(?:\.\d+){1,3}[A-Za-z0-9._+-]*")
_CONF_LINE = re.compile(r'^\s*([^#;=\s][^=]*?)\s*=\s*(.*?)\s*$')
_JOYSTICK = re.compile(r"\bjs\d+\b")
MAX_READ_BYTES = 512 * 1024


def extract_version(text: str, pattern: Optional["re.Pattern"] = None) -> str:
    match = (pattern or _VERSION).search(str(text or ""))
    return match.group(0) if match else ""


class SystemProbe:
    def __init__(self, settings: Any, *, which=shutil.which, proc_root: Path = Path("/proc"),
                 input_devices: Path = Path("/proc/bus/input/devices"), dri_dir: Path = Path("/dev/dri")) -> None:
        self.settings = settings
        self._which = which
        self.proc_root = Path(proc_root)
        self.input_devices = Path(input_devices)
        self.dri_dir = Path(dri_dir)
        self._conf_cache: Optional[tuple] = None

    # --------------------------------------------------------------- binaries
    def find_binary(self, names: Iterable[str], extra: Iterable[str] = ()) -> Optional[str]:
        for name in names:
            found = self._which(name)
            if found:
                return found
        for candidate in extra:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def version_of(self, binary: Optional[str], arguments: Iterable[str] = ("--version",), timeout: float = 4.0,
                   pattern: Optional["re.Pattern"] = None) -> str:
        if not binary:
            return ""
        try:
            completed = subprocess.run([binary, *arguments], capture_output=True, text=True, timeout=timeout,
                                       shell=False, stdin=subprocess.DEVNULL, env={"PATH": os.environ.get("PATH", ""), "LANG": "C"})
        except (OSError, subprocess.SubprocessError):
            return ""
        return extract_version((completed.stdout or "") + "\n" + (completed.stderr or ""), pattern)

    def process_running(self, names: Iterable[str]) -> bool:
        wanted = {name.lower() for name in names}
        try:
            entries = [entry for entry in self.proc_root.iterdir() if entry.name.isdigit()]
        except OSError:
            return False
        for entry in entries:
            try:
                if (entry / "comm").read_text(errors="ignore").strip().lower() in wanted:
                    return True
            except OSError:
                continue
        return False

    def tcp_open(self, host: str, port: int, timeout: float = 1.5) -> bool:
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except (OSError, ValueError):
            return False

    # ------------------------------------------------------------------ files
    def exists(self, path: Any) -> bool:
        return Path(path).exists()

    def read_text(self, path: Any, limit: int = MAX_READ_BYTES) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read(limit)
        except OSError:
            return ""

    def glob(self, pattern: str, limit: int = 400) -> List[str]:
        return sorted(glob.glob(pattern))[:limit]

    def file_contains_all(self, paths: Iterable[str], tokens: Iterable[str]) -> Dict[str, bool]:
        """Which of ``tokens`` appear anywhere in the given source files (feature introspection)."""
        remaining = {token: False for token in tokens}
        for path in paths:
            text = self.read_text(path, 2 * 1024 * 1024)
            for token in remaining:
                if not remaining[token] and token in text:
                    remaining[token] = True
            if all(remaining.values()):
                break
        return remaining

    # ----------------------------------------------------------- batocera.conf
    def conf_value(self, system: str, rom_name: str, key: str) -> str:
        """Effective ``batocera.conf`` value: per-game, then per-system, then global."""
        values = self._conf_values()
        for name in (f'{system}["{rom_name}"].{key}', f"{system}.{key}", f"global.{key}"):
            if values.get(name):
                return values[name]
        return ""

    def _conf_values(self) -> Dict[str, str]:
        """Parsed batocera.conf, re-read only when the file changes."""
        path = Path(self.settings.batocera_conf_file)
        try:
            info = path.stat()
            stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            return {}
        if self._conf_cache and self._conf_cache[0] == stamp:
            return self._conf_cache[1]
        values: Dict[str, str] = {}
        for line in self.read_text(path, 4 * 1024 * 1024).splitlines():
            match = _CONF_LINE.match(line)
            if match:
                values[match.group(1)] = match.group(2)
        self._conf_cache = (stamp, values)
        return values

    # ------------------------------------------------------------ controllers
    def controllers(self) -> List[dict]:
        """Joystick-class devices from the kernel's input list (read only; no USB enumeration)."""
        text = self.read_text(self.input_devices)
        devices = []
        for block in re.split(r"\n\s*\n", text):
            name = re.search(r'^N: Name="(.*)"$', block, re.M)
            handlers = re.search(r"^H: Handlers=(.*)$", block, re.M)
            if name and handlers and _JOYSTICK.search(handlers.group(1)):
                devices.append({"name": name.group(1)[:80], "handler": _JOYSTICK.search(handlers.group(1)).group(0)})
        return devices

    def hardware(self) -> dict:
        render_nodes = []
        try:
            render_nodes = sorted(entry.name for entry in self.dri_dir.iterdir() if entry.name.startswith("renderD"))
        except OSError:
            pass
        return {
            "cpu_count": os.cpu_count() or 1,
            "vaapi": bool(render_nodes),
            "nvenc": bool(self._which("nvidia-smi")),
        }
