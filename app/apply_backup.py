#!/usr/bin/env python3
"""Restore a backup onto this machine as root (privileged service worker helper).

The Drone app reuses ``run()`` in-process when it already runs as root; otherwise the
service worker in ``service_bootstrap.sh`` runs ``python3 app/apply_backup.py
<request.json>`` when the app drops ``apply-backup.request`` in the control directory.
Both paths share this sequence so it can never drift:

1. exit any running game (``batocera-es-swissknife --emukill``) so no emulator holds a
   save or config open, and stop EmulationStation so it cannot rewrite gamelists or
   settings on exit;
2. ``backups.restore.apply`` -- verified, symlink-safe, selected categories only;
3. ``batocera-save-overlay`` (best effort) and restart EmulationStation, retrying and
   finally falling back to ``batocera-es-swissknife --restart``.

The request JSON carries the archive path, the manifest (re-validated here), the
selected categories, mode and roots. The detailed result (or ``{"error": ...}``) is
written to ``result_path``; the exit status tells the worker whether it succeeded.
Stdlib only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from .backups import archive as _archive
    from .backups import categories as _categories
    from .backups import restore as _restore
except ImportError:  # run as a script or in flat mode
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from backups import archive as _archive  # type: ignore
    from backups import categories as _categories  # type: ignore
    from backups import restore as _restore  # type: ignore

EMULATIONSTATION_SERVICE = "/etc/init.d/S31emulationstation"
EMULATIONSTATION_START_TIMEOUT_SECONDS = 20


def _log(message: str) -> None:
    print(f"[apply_backup] {message}", flush=True)


def _run_step(command: list, *, timeout: int = 120, capture_output: bool = True) -> bool:
    """Best-effort step; never raises. ``start`` must not capture output: the init
    script backgrounds ``startx &`` with inherited stdout, so a pipe would block."""
    label = " ".join(command)
    try:
        if capture_output:
            proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
            _log(f"{label} -> rc={proc.returncode} {(proc.stdout or '').strip()}".rstrip())
        else:
            proc = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
            _log(f"{label} -> rc={proc.returncode}")
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as error:
        _log(f"{label} -> error: {error}")
        return False


def _emulationstation_running() -> bool:
    try:
        return subprocess.run(["pidof", "emulationstation"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _wait_for_emulationstation(timeout: int = EMULATIONSTATION_START_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + max(1, timeout)
    while time.monotonic() < deadline:
        if _emulationstation_running():
            return True
        time.sleep(0.5)
    return False


def _stop_games_and_emulationstation() -> None:
    swissknife = shutil.which("batocera-es-swissknife")
    if swissknife:
        _run_step([swissknife, "--emukill"], timeout=30)
    else:
        _log("batocera-es-swissknife not found; skipping emulator exit")
    _run_step([EMULATIONSTATION_SERVICE, "stop"], timeout=60)
    time.sleep(3)


def _start_emulationstation() -> bool:
    _run_step(["batocera-save-overlay"], timeout=30)
    for _attempt in range(2):
        if _run_step([EMULATIONSTATION_SERVICE, "start"], timeout=60, capture_output=False) and _wait_for_emulationstation():
            return True
        time.sleep(3)
    swissknife = shutil.which("batocera-es-swissknife")
    if swissknife and _run_step([swissknife, "--restart"], timeout=60, capture_output=False):
        return _wait_for_emulationstation()
    return False


def run(request: dict, *, manage_emulationstation: bool = True) -> dict:
    manifest = _archive.validate_manifest(request.get("manifest"))
    manifest = _restore.with_extra_categories(manifest, request.get("extra_categories") or [])
    roots = _categories.Roots(Path(request["userdata_root"]), Path(request["roms_root"]), Path(request["saves_root"]))
    archive_path = Path(request["archive_path"])
    if not archive_path.is_file():
        raise _restore.RestoreError("The backup archive is missing.", 404)
    _log(f"restoring {manifest['backup_id']} categories={request.get('categories')} mode={request.get('mode')}")
    if manage_emulationstation:
        _stop_games_and_emulationstation()
    try:
        result = _restore.apply(roots, archive_path, manifest, request.get("categories") or [],
                                str(request.get("mode") or "overlay"), bool(request.get("include_machine_keys")),
                                Path(request["staging_dir"]), request.get("paths"))
    finally:
        started = _start_emulationstation() if manage_emulationstation else None
    result["emulationstation_restarted"] = started
    if started is False:
        result["warning"] = "EmulationStation did not come back on its own. Restart the machine to finish."
    _log(f"result: {json.dumps(result)}")
    return result


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: apply_backup.py <request.json>", file=sys.stderr)
        return 2
    try:
        request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"Unable to read request JSON: {error}", file=sys.stderr)
        return 2
    result_path = Path(request["result_path"]) if request.get("result_path") else None
    try:
        result = run(request)
        status = 0
    except Exception as error:  # noqa: BLE001 - reported to the Drone app
        result = {"error": str(error) or error.__class__.__name__}
        _log(f"failed: {result['error']}")
        status = 1
    if result_path is not None:
        try:
            result_path.write_text(json.dumps(result), encoding="utf-8")
        except OSError as error:
            _log(f"could not write result: {error}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
