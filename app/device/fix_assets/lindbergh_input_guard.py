#!/usr/bin/env python3
"""Batocera game hook that keeps Lindbergh within its safe joystick budget.

Installed and removed by Batocera Drone's Admin Fixes page.  Batocera invokes
executable files in /userdata/system/scripts for gameStart and gameStop events.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
USERDATA_ROOT = SCRIPT_PATH.parent.parent.parent
CONFIG_PATH = USERDATA_ROOT / "system" / "input-device-guard" / "config.json"
LOG_PATH = USERDATA_ROOT / "system" / "logs" / "input-device-guard.log"
RUNTIME_ROOT = Path(os.environ.get("DRONE_INPUT_GUARD_RUNTIME_DIR", "/var/run"))
STATE_PATH = RUNTIME_ROOT / "drone-lindbergh-input-guard.json"
LOCK_PATH = RUNTIME_ROOT / "drone-lindbergh-input-guard.lock"
SYS_INPUT_ROOT = Path(os.environ.get("DRONE_INPUT_GUARD_SYS_INPUT", "/sys/class/input"))
USB_DRIVERS_ROOT = Path(os.environ.get("DRONE_INPUT_GUARD_USB_DRIVERS", "/sys/bus/usb/drivers"))


def log(message):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 512 * 1024:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def load_config():
    defaults = {
        "max_joysticks": 8,
        "protected_name_patterns": ["sinden", "light[ -]?gun"],
        "candidate_name_patterns": ["gamecube", "wup-028", "mayflash", "nintendo.*adapter"],
    }
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        defaults["max_joysticks"] = max(1, min(32, int(raw.get("max_joysticks", 8))))
    except (TypeError, ValueError):
        pass
    for key in ("protected_name_patterns", "candidate_name_patterns"):
        if isinstance(raw.get(key), list):
            defaults[key] = [str(value) for value in raw[key] if str(value).strip()]
    return defaults


def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def usb_parent_for(js_path):
    try:
        current = (js_path / "device").resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if (candidate / "idVendor").is_file() and (candidate / "idProduct").is_file():
            return candidate
    return None


def joystick_groups():
    groups = {}
    for js_path in sorted(SYS_INPUT_ROOT.glob("js*")):
        parent = usb_parent_for(js_path)
        if parent is None:
            continue
        usb_id = parent.name
        group = groups.setdefault(
            usb_id,
            {
                "usb_id": usb_id,
                "driver": "usb",
                "vendor": read_text(parent / "idVendor"),
                "product": read_text(parent / "idProduct"),
                "names": [],
                "joysticks": [],
            },
        )
        name = read_text(js_path / "device" / "name") or js_path.name
        group["names"].append(name)
        group["joysticks"].append(js_path.name)
    return list(groups.values())


def pattern_matches(patterns, text):
    for pattern in patterns:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error:
            if pattern.lower() in text.lower():
                return True
    return False


def select_groups_to_detach(groups, overflow, config):
    candidates = []
    for group in groups:
        combined_name = " ".join(group["names"])
        count = len(group["joysticks"])
        # A single-controller device is never detached automatically.  This
        # keeps the generic heuristic focused on multi-port adapters rather
        # than choosing among a cabinet's ordinary controls.
        if count < 2 or pattern_matches(config["protected_name_patterns"], combined_name):
            continue
        preferred = pattern_matches(config["candidate_name_patterns"], combined_name)
        candidates.append((0 if preferred else 1, -count, group["usb_id"], group))
    candidates.sort(key=lambda item: item[:3])
    selected = []
    removed = 0
    for _, neg_count, _, group in candidates:
        selected.append(group)
        removed += -neg_count
        if removed >= overflow:
            break
    return selected if removed >= overflow else []


def write_driver_action(group, action):
    control = USB_DRIVERS_ROOT / group.get("driver", "usb") / action
    with control.open("w", encoding="utf-8") as handle:
        handle.write(group["usb_id"])


def save_state(groups, launcher_pid=None, launcher_start=None):
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(f".tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(
            {
                "version": 1,
                "detached": groups,
                "launcher_pid": launcher_pid,
                "launcher_start": launcher_start,
                "created_at": time.time(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, STATE_PATH)


def load_state():
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return state if isinstance(state, dict) else {}


def restore_locked(reason):
    state = load_state()
    detached = state.get("detached") if isinstance(state.get("detached"), list) else []
    if not detached:
        try:
            STATE_PATH.unlink()
        except FileNotFoundError:
            pass
        return True
    failures = []
    for group in detached:
        try:
            write_driver_action(group, "bind")
            log(f"restored USB device {group.get('usb_id')} ({reason})")
        except OSError as error:
            failures.append(f"{group.get('usb_id')}: {error}")
    if failures:
        log("restore incomplete: " + "; ".join(failures))
        return False
    try:
        STATE_PATH.unlink()
    except FileNotFoundError:
        pass
    return True


def proc_start_time(pid):
    try:
        # Field 22 is starttime; the process name in parentheses can contain
        # spaces, so split only after the final ')'.
        tail = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return tail[19]
    except (OSError, IndexError):
        return None


def launcher_is_alive(pid, expected_start):
    current = proc_start_time(pid)
    return current is not None and (expected_start is None or current == expected_start)


def spawn_watchdog(pid, start_time):
    try:
        subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH), "watch", str(pid), str(start_time or "")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as error:
        log(f"could not start restore watchdog: {error}")


def on_game_start(launcher_pid):
    config = load_config()
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        restore_locked("stale-state recovery")
        groups = joystick_groups()
        # Count every joystick node, including Bluetooth/platform devices that
        # cannot be detached through a USB parent. Only USB groups are eligible
        # candidates below.
        count = len(list(SYS_INPUT_ROOT.glob("js*")))
        maximum = config["max_joysticks"]
        if count <= maximum:
            log(f"Lindbergh launch: {count} joystick devices; no guard action needed")
            return
        selected = select_groups_to_detach(groups, count - maximum, config)
        if not selected:
            log(f"Lindbergh launch blocked from guard action: {count} joysticks exceed {maximum}, but no safe multi-port adapter was found")
            return
        start_time = proc_start_time(launcher_pid)
        detached = []
        try:
            for group in selected:
                # Persist before unbinding so an abrupt launcher failure can
                # never leave an untracked device detached.
                save_state(detached + [group], launcher_pid, start_time)
                write_driver_action(group, "unbind")
                detached.append(group)
                log(f"temporarily detached USB device {group['usb_id']} ({', '.join(group['names'])})")
            save_state(detached, launcher_pid, start_time)
        except OSError as error:
            log(f"failed to apply Lindbergh input guard: {error}")
            restore_locked("start failure")
            return
    spawn_watchdog(launcher_pid, start_time)


def on_restore(reason):
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        restore_locked(reason)


def watch(pid, expected_start):
    while STATE_PATH.exists() and launcher_is_alive(pid, expected_start or None):
        time.sleep(2)
    if STATE_PATH.exists():
        on_restore("launcher watchdog")


def main(argv):
    if not argv:
        return 0
    event = argv[0]
    if event == "watch" and len(argv) >= 2:
        watch(int(argv[1]), argv[2] if len(argv) >= 3 else "")
        return 0
    if event == "restore":
        on_restore("manual disable")
        return 0
    # Batocera hook arguments: event, system, emulator, core, rom.
    system = argv[1].lower() if len(argv) >= 2 else ""
    if system != "lindbergh":
        return 0
    if event == "gameStart":
        on_game_start(os.getppid())
    elif event == "gameStop":
        on_restore("gameStop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
