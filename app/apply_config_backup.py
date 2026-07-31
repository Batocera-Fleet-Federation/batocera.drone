#!/usr/bin/env python3
"""Restore a config-backup tarball onto this machine, as the privileged service worker.

Canonical implementation of "extract a config-backup archive back over the real
Batocera paths it was built from, restarting EmulationStation so the restored
gamelists/settings are picked up", mirroring set_screen_mode.py / set_es_collections.py's
proven stop -> write -> overlay-save -> start sequence (same rationale: the
``stop``/overlay-save steps are best-effort and headless-safe, and every step is
logged to stdout for the service worker to capture). The privileged (root) Drone
service worker invokes it as ``python3 app/apply_config_backup.py <path-to-request-json>``;
the Drone app itself reuses ``restore_config_backup`` in-process when it happens to run
as root, so the sequence can never drift between the two entry points.

Archive members use the same three logical roots ``device/config_backup.py``'s
``collect_sources()`` builds them from -- ``system/...`` (-> ``<userdata_root>/system/...``),
``roms/<system>/gamelist.xml`` (-> ``<roms_root>/<system>/gamelist.xml``), and
``saves/...`` (-> ``<saves_root>/...``); ``MANIFEST.txt`` is metadata about the backup
itself, not a real config file, and is skipped. Each computed destination is resolved
and checked against its expected root before anything is written (the same tar-slip
barrier ``common/self_update.py`` uses for the Drone's own self-update archive) --
defensive even though these archives are either built by this Drone or pulled from an
already-trusted paired peer.

A running game is exited first (best-effort) so its emulator isn't holding a save file
open while the restore overwrites it, then EmulationStation itself is stopped so it
can't clobber a restored gamelist.xml on its own exit later and so startup-only config
changes actually take effect once it restarts.

Deliberately self-contained (stdlib only, no imports from the rest of the ``app``
package) like the other privileged helper scripts: the caller (Drone app, which has
full package access) resolves ``userdata_root``/``roms_root``/``saves_root`` from
``Settings`` -- this script just extracts.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

EMULATIONSTATION_SERVICE = "/etc/init.d/S31emulationstation"
EMULATIONSTATION_START_TIMEOUT_SECONDS = 20


def _run_step(command: list, *, timeout: int = 120, capture_output: bool = True) -> bool:
    """Run a best-effort step, logging its combined output. Never raises.

    See set_screen_mode.py's ``_run_step`` for why "start" must not capture via a pipe:
    the init script backgrounds ``startx &`` without redirecting its own stdout/stderr,
    so ``subprocess.run(stdout=PIPE)`` would block until EmulationStation exits.
    """
    label = " ".join(command)
    try:
        if capture_output:
            proc = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout
            )
            output = (proc.stdout or "").strip()
            print(f"[apply_config_backup] {label} -> rc={proc.returncode} {output}".rstrip())
        else:
            proc = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
            print(f"[apply_config_backup] {label} -> rc={proc.returncode}")
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as error:
        print(f"[apply_config_backup] {label} -> error: {error}")
        return False


def _wait_for_emulationstation(timeout: int = EMULATIONSTATION_START_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + max(1, timeout)
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["pidof", "emulationstation"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(0.5)
    return False


def _kill_running_emulator() -> None:
    """Exit any currently-running game (best-effort) before the restore touches
    saves/gamelists -- mirrors device_control.py's idle-game-exit automation path."""
    tool = shutil.which("batocera-es-swissknife")
    if not tool:
        print("[apply_config_backup] batocera-es-swissknife not found; skipping emulator-exit step")
        return
    _run_step([tool, "--emukill"], timeout=30)


def _resolve_restore_destination(arcname: str, userdata_root: Path, roms_root: Path, saves_root: Path) -> Optional[Path]:
    """Map one archive member back to its real on-disk path, the reverse of
    ``collect_sources()``'s arcname construction. Returns ``None`` for
    ``MANIFEST.txt``, an unrecognized top-level segment, or a path that would
    resolve outside its expected root (tar-slip safety)."""
    normalized = str(arcname or "").replace("\\", "/").lstrip("/")
    if not normalized or normalized == "MANIFEST.txt":
        return None
    if normalized.startswith("system/"):
        root = (userdata_root / "system").resolve()
        rel = normalized[len("system/"):]
    elif normalized.startswith("roms/"):
        root = roms_root.resolve()
        rel = normalized[len("roms/"):]
    elif normalized.startswith("saves/"):
        root = saves_root.resolve()
        rel = normalized[len("saves/"):]
    else:
        return None
    if not rel:
        return None
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        return None
    return target


def _extract_all(archive_path: Path, userdata_root: Path, roms_root: Path, saves_root: Path) -> Tuple[int, List[dict]]:
    restored = 0
    skipped: List[dict] = []
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            destination = _resolve_restore_destination(member.name, userdata_root, roms_root, saves_root)
            if destination is None:
                if member.name != "MANIFEST.txt":
                    skipped.append({"path": member.name, "reason": "unrecognized or unsafe path"})
                continue
            source = tar.extractfile(member)
            if source is None:
                skipped.append({"path": member.name, "reason": "unreadable archive member"})
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
                restored += 1
            except OSError as error:
                skipped.append({"path": member.name, "reason": f"write failed: {error}"})
    return restored, skipped


def restore_config_backup(archive_path: Path, userdata_root: Path, roms_root: Path, saves_root: Path) -> dict:
    """Extract ``archive_path`` back onto the real Batocera paths, stopping
    EmulationStation (and any running game) first and restarting it afterward.
    Always attempts the restore even if stopping ES failed (best-effort, same
    headless-tolerant philosophy as set_screen_mode.py); raises only if
    EmulationStation fails to come back up at the end, even though the files were
    already restored by that point."""
    print(f"[apply_config_backup] restoring {archive_path}")
    _kill_running_emulator()
    _run_step([EMULATIONSTATION_SERVICE, "stop"], timeout=60)
    time.sleep(3)
    restored, skipped = _extract_all(archive_path, userdata_root, roms_root, saves_root)
    print(f"[apply_config_backup] restored {restored} file(s), skipped {len(skipped)}")
    # Persist to the overlay so the change survives a reboot, but never let a slow or
    # failing overlay save block the EmulationStation restart below.
    _run_step(["batocera-save-overlay"], timeout=30)
    print("[apply_config_backup] starting EmulationStation")
    started = _run_step([EMULATIONSTATION_SERVICE, "start"], timeout=60, capture_output=False)
    if started:
        started = _wait_for_emulationstation()
    if not started:
        print("[apply_config_backup] EmulationStation process did not return; retrying start")
        time.sleep(3)
        started = _run_step([EMULATIONSTATION_SERVICE, "start"], timeout=60, capture_output=False)
        if started:
            started = _wait_for_emulationstation()
    if not started:
        restart_tool = shutil.which("batocera-es-swissknife")
        if restart_tool:
            started = _run_step([restart_tool, "--restart"], timeout=60, capture_output=False)
            if started:
                started = _wait_for_emulationstation()
    if not started:
        raise RuntimeError(
            f"EmulationStation did not restart after the config backup restore "
            f"({restored} file(s) were already restored)"
        )
    print("[apply_config_backup] EmulationStation start completed")
    return {
        "restored_file_count": restored,
        "skipped_file_count": len(skipped),
        "skipped": skipped,
        "restarted_emulationstation": True,
    }


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: apply_config_backup.py <path-to-request-json>", file=sys.stderr)
        return 2
    try:
        request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Unable to read request JSON: {error}", file=sys.stderr)
        return 2
    result = restore_config_backup(
        Path(request["archive_path"]),
        Path(request["userdata_root"]),
        Path(request["roms_root"]),
        Path(request["saves_root"]),
    )
    print(f"[apply_config_backup] result: {json.dumps(result)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
