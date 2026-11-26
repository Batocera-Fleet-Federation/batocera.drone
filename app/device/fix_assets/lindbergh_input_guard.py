#!/usr/bin/env python3
"""Batocera game hook that keeps every game within a safe joystick budget.

Emulators such as RetroArch and LinuxLoader can crash ("stack smashing
detected") when too many joystick devices exist at launch.  At gameStart this
hook counts them and, only when over the limit, first stops the Wii U GameCube
adapter daemon (its four virtual pads have no USB parent) and then unbinds
multi-port USB controller adapters.  Everything is restored at gameStop.

Installed and removed by Batocera Drone's Admin Fixes page.  Batocera invokes
executable files in /userdata/system/scripts for gameStart and gameStop events.
"""

import fcntl
import json
import os
import re
import signal
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
USB_DEVICES_ROOT = Path(os.environ.get("DRONE_INPUT_GUARD_USB_DEVICES", "/sys/bus/usb/devices"))
PROC_ROOT = Path(os.environ.get("DRONE_INPUT_GUARD_PROC", "/proc"))
SWITCH2_GUARD_CONFIG = USERDATA_ROOT / "system" / "switch2-gamecube-guard" / "config.json"
SWITCH2_GUARD_STATE = RUNTIME_ROOT / "drone-switch2-gamecube-guard.json"
STOP_TIMEOUT_SECONDS = 3.0
UDEV_RESTART_GRACE_SECONDS = 4.0


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
        # "*" guards every system; list system names to restrict it.
        "systems": ["*"],
        # Systems that legitimately use the GameCube adapter keep it.
        "adapter_exempt_systems": ["gamecube", "wii", "wiiu", "switch"],
        "adapter_usb_ids": ["057e:0337"],
        "adapter_daemon_names": ["wii-u-gc-adapter", "wii-u-gc-adapter-daemon"],
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
    for key in ("protected_name_patterns", "candidate_name_patterns", "systems", "adapter_exempt_systems",
                "adapter_usb_ids", "adapter_daemon_names"):
        if isinstance(raw.get(key), list):
            values = [str(value).strip() for value in raw[key] if str(value).strip()]
            defaults[key] = [value.lower() for value in values] if key != "protected_name_patterns" and key != "candidate_name_patterns" else values
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


def joystick_count():
    return len(list(SYS_INPUT_ROOT.glob("js*")))


def find_adapters(config):
    """Sysfs names (such as 5-1.4) of connected GameCube adapters."""
    wanted = set(config["adapter_usb_ids"])
    found = []
    try:
        entries = sorted(USB_DEVICES_ROOT.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return found
    for entry in entries:
        if ":" in entry.name:
            continue
        if f"{read_text(entry / 'idVendor').lower()}:{read_text(entry / 'idProduct').lower()}" in wanted:
            found.append(entry.name)
    return found


def adapter_bound(usb_id):
    return (USB_DEVICES_ROOT / usb_id / "driver").exists()


def process_argv(pid):
    try:
        raw = (PROC_ROOT / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def parent_pid(pid):
    try:
        return int((PROC_ROOT / str(pid) / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def find_daemons(config):
    names = set(config["adapter_daemon_names"])
    own = os.getpid()
    found = []
    try:
        entries = list(PROC_ROOT.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == own:
            continue
        argv = process_argv(entry.name)
        if any(os.path.basename(arg).lower() in names for arg in argv[:2]):
            found.append((int(entry.name), argv))
    return sorted(found)


def restart_commands(daemons):
    """A wrapper and the daemon it runs both match; restart only the outermost."""
    matched = {pid for pid, _ in daemons}
    return [argv for pid, argv in daemons if parent_pid(pid) not in matched]


def pid_alive(pid):
    return (PROC_ROOT / str(pid)).exists()


def stop_daemon(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def start_daemon(argv):
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True)
        log(f"restarted adapter daemon: {' '.join(argv)}")
    except OSError as error:
        log(f"could not restart adapter daemon {' '.join(argv)}: {error}")


def switch2_guard_owns(system):
    """The dedicated Switch 2 guard manages the adapter for its own systems."""
    if SWITCH2_GUARD_STATE.exists():
        return True
    systems = ["switch2"]
    try:
        raw = json.loads(SWITCH2_GUARD_CONFIG.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("systems"), list):
            systems = [str(value).strip().lower() for value in raw["systems"]]
    except (OSError, ValueError, TypeError):
        pass
    return SWITCH2_GUARD_CONFIG.exists() and system in systems or system == "switch2"


def write_driver_action(group, action):
    control = USB_DRIVERS_ROOT / group.get("driver", "usb") / action
    with control.open("w", encoding="utf-8") as handle:
        handle.write(group["usb_id"])


def save_state(groups, launcher_pid=None, launcher_start=None, adapters=(), daemons=()):
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(f".tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(
            {
                "version": 1,
                "detached": groups,
                "adapters": list(adapters),
                "daemons": list(daemons),
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
    adapters = [str(v) for v in state.get("adapters") or [] if isinstance(v, str)]
    daemons = [list(map(str, a)) for a in state.get("daemons") or [] if isinstance(a, list) and a]
    if not detached and not adapters and not daemons:
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
    for usb_id in adapters:
        if adapter_bound(usb_id) or not (USB_DEVICES_ROOT / usb_id).exists():
            continue
        try:
            write_driver_action({"usb_id": usb_id, "driver": "usb"}, "bind")
            log(f"restored GameCube adapter {usb_id} ({reason})")
        except OSError as error:
            failures.append(f"{usb_id}: {error}")
    if daemons:
        config = load_config()
        deadline = time.monotonic() + (UDEV_RESTART_GRACE_SECONDS if adapters else 0)
        while not find_daemons(config) and time.monotonic() < deadline:
            time.sleep(0.25)
        if find_daemons(config):
            log(f"adapter daemon is running again ({reason})")
        else:
            for argv in daemons:
                start_daemon(argv)
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


def on_game_start(launcher_pid, system="lindbergh"):
    config = load_config()
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        restore_locked("stale-state recovery")
        maximum = config["max_joysticks"]
        # Count every joystick node, including Bluetooth/platform devices that
        # cannot be detached through a USB parent.
        count = joystick_count()
        if count <= maximum:
            log(f"{system} launch: {count} joystick devices; no guard action needed")
            return
        start_time = proc_start_time(launcher_pid)
        adapters, daemons = [], []
        detached = []
        try:
            # Step 1: the GameCube adapter's virtual pads (no USB parent).
            if system not in config["adapter_exempt_systems"] and not switch2_guard_owns(system):
                adapters = [u for u in find_adapters(config) if adapter_bound(u)]
                found = find_daemons(config)
                if adapters or found:
                    daemons = restart_commands(found)
                    save_state(detached, launcher_pid, start_time, adapters, daemons)
                    matched = {pid for pid, _ in found}
                    for pid, argv in sorted(found, key=lambda item: parent_pid(item[0]) in matched):
                        stop_daemon(pid)
                        log(f"stopped adapter daemon {pid} ({' '.join(argv)})")
                    for usb_id in adapters:
                        write_driver_action({"usb_id": usb_id, "driver": "usb"}, "unbind")
                        log(f"temporarily detached GameCube adapter {usb_id}")
                    count = joystick_count()
            # Step 2: multi-port USB adapters, only if still over the limit.
            if count > maximum:
                selected = select_groups_to_detach(joystick_groups(), count - maximum, config)
                if not selected and not adapters and not daemons:
                    log(f"{system} launch: {count} joysticks exceed {maximum}, but no safe adapter was found")
                    return
                for group in selected:
                    save_state(detached + [group], launcher_pid, start_time, adapters, daemons)
                    write_driver_action(group, "unbind")
                    detached.append(group)
                    log(f"temporarily detached USB device {group['usb_id']} ({', '.join(group['names'])})")
            save_state(detached, launcher_pid, start_time, adapters, daemons)
            log(f"{system} launch: joystick devices -> {joystick_count()} (limit {maximum})")
        except OSError as error:
            log(f"failed to apply input guard: {error}")
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
    systems = load_config()["systems"]
    if "*" not in systems and system not in [value.lower() for value in systems]:
        return 0
    if event == "gameStart":
        on_game_start(os.getppid(), system)
    elif event == "gameStop":
        on_restore("gameStop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
