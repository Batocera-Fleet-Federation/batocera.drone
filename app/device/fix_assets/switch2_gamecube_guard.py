#!/usr/bin/env python3
"""Batocera game hook that takes the Wii U GameCube adapter offline for Switch 2 games.

Installed and removed by Batocera Drone's Admin Fixes page.  Batocera invokes
executable files in /userdata/system/scripts for gameStart and gameStop events.

The Switch 2 emulators (eglantine, orchid, lichii) abort with "stack smashing
detected" while initializing input when a GameCube adapter is connected.
Batocera drives the adapter with the userspace wii-u-gc-adapter daemon, which
creates four virtual (uinput) joysticks with no USB parent, so they cannot be
removed by unbinding a multi-port USB group the way the Lindbergh guard does.
At gameStart this hook stops that daemon (its virtual joysticks disappear with
it) and unbinds the adapter's USB device so the emulator's own libusb GameCube
driver cannot open it either -- the same state as physically unplugging it.
gameStop, a launcher watchdog, or the next launch binds the adapter again and
restarts the daemon with the command line it had.
"""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
USERDATA_ROOT = SCRIPT_PATH.parent.parent.parent
CONFIG_PATH = USERDATA_ROOT / "system" / "switch2-gamecube-guard" / "config.json"
LOG_PATH = USERDATA_ROOT / "system" / "logs" / "switch2-gamecube-guard.log"
RUNTIME_ROOT = Path(os.environ.get("DRONE_GC_GUARD_RUNTIME_DIR", "/var/run"))
STATE_PATH = RUNTIME_ROOT / "drone-switch2-gamecube-guard.json"
LOCK_PATH = RUNTIME_ROOT / "drone-switch2-gamecube-guard.lock"
USB_DEVICES_ROOT = Path(os.environ.get("DRONE_GC_GUARD_USB_DEVICES", "/sys/bus/usb/devices"))
USB_DRIVERS_ROOT = Path(os.environ.get("DRONE_GC_GUARD_USB_DRIVERS", "/sys/bus/usb/drivers"))
SYS_INPUT_ROOT = Path(os.environ.get("DRONE_GC_GUARD_SYS_INPUT", "/sys/class/input"))
PROC_ROOT = Path(os.environ.get("DRONE_GC_GUARD_PROC", "/proc"))

DEFAULT_CONFIG = {
    "systems": ["switch2"],
    "usb_ids": ["057e:0337"],
    "daemon_names": ["wii-u-gc-adapter", "wii-u-gc-adapter-daemon"],
}
# How long to wait for the daemon to exit after SIGTERM, and for udev to start
# it again after the adapter is bound, before acting ourselves.
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
    config = {key: list(value) for key, value in DEFAULT_CONFIG.items()}
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    for key in config:
        values = raw.get(key)
        if isinstance(values, list):
            cleaned = [str(value).strip().lower() for value in values if str(value).strip()]
            if cleaned:
                config[key] = cleaned
    return config


def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def find_adapters(config):
    """USB device ids (sysfs names such as 5-1.4) of connected adapters."""
    wanted = set(config["usb_ids"])
    found = []
    try:
        entries = sorted(USB_DEVICES_ROOT.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return found
    for entry in entries:
        # Interfaces (5-1.4:1.0) carry no idVendor; root hubs are usbN.
        if ":" in entry.name:
            continue
        usb_id = f"{read_text(entry / 'idVendor').lower()}:{read_text(entry / 'idProduct').lower()}"
        if usb_id in wanted:
            found.append(entry.name)
    return found


def is_bound(usb_id):
    return (USB_DEVICES_ROOT / usb_id / "driver").exists()


def process_argv(pid):
    try:
        raw = (PROC_ROOT / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def find_daemons(config):
    """(pid, argv) of running adapter daemons, matched on the program name."""
    names = set(config["daemon_names"])
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
        # Match the program itself, or a shell/interpreter running it.
        if any(os.path.basename(arg).lower() in names for arg in argv[:2]):
            found.append((int(entry.name), argv))
    return sorted(found)


def parent_pid(pid):
    try:
        # Field 4 is the parent pid; split after the final ')' as for starttime.
        return int((PROC_ROOT / str(pid) / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def restart_commands(daemons):
    """Command lines to start again: a wrapper and the daemon it runs both
    match, but only the outermost one may be restarted or two would compete."""
    matched = {pid for pid, _ in daemons}
    return [argv for pid, argv in daemons if parent_pid(pid) not in matched]


def pid_alive(pid):
    return (PROC_ROOT / str(pid)).exists()


def joystick_count():
    return len(list(SYS_INPUT_ROOT.glob("js*")))


def write_driver_action(usb_id, action):
    with (USB_DRIVERS_ROOT / "usb" / action).open("w", encoding="utf-8") as handle:
        handle.write(usb_id)


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
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        log(f"restarted adapter daemon: {' '.join(argv)}")
    except OSError as error:
        log(f"could not restart adapter daemon {' '.join(argv)}: {error}")


def save_state(adapters, daemons, launcher_pid=None, launcher_start=None):
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(f".tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(
            {
                "version": 1,
                "adapters": adapters,
                "daemons": daemons,
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


def clear_state():
    try:
        STATE_PATH.unlink()
    except FileNotFoundError:
        pass


def restore_locked(reason, config):
    state = load_state()
    adapters = [str(value) for value in state.get("adapters") or [] if isinstance(value, str)]
    daemons = [list(map(str, argv)) for argv in state.get("daemons") or [] if isinstance(argv, list) and argv]
    if not adapters and not daemons:
        clear_state()
        return True
    failures = []
    for usb_id in adapters:
        if is_bound(usb_id) or not (USB_DEVICES_ROOT / usb_id).exists():
            continue  # already back, or unplugged while the game ran
        try:
            write_driver_action(usb_id, "bind")
            log(f"restored GameCube adapter {usb_id} ({reason})")
        except OSError as error:
            failures.append(f"{usb_id}: {error}")
    if daemons:
        # Binding the adapter can make udev start the daemon itself; only start
        # it ourselves when that has not happened, so two never fight over it.
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
    clear_state()
    return True


def proc_start_time(pid):
    try:
        # Field 22 is starttime; the process name in parentheses can contain
        # spaces, so split only after the final ')'.
        tail = (PROC_ROOT / str(pid) / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
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


def on_game_start(launcher_pid, system):
    config = load_config()
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        restore_locked("stale-state recovery", config)
        adapters = [usb_id for usb_id in find_adapters(config) if is_bound(usb_id)]
        daemons = find_daemons(config)
        if not adapters and not daemons:
            log(f"{system} launch: no GameCube adapter connected; nothing to do")
            return
        before = joystick_count()
        start_time = proc_start_time(launcher_pid)
        # Persist before acting so an abrupt launcher failure can never leave
        # the adapter detached or its daemon stopped without a record.
        save_state(adapters, restart_commands(daemons), launcher_pid, start_time)
        try:
            # Outermost first, so a wrapper cannot respawn the daemon it runs.
            matched = {pid for pid, _ in daemons}
            for pid, argv in sorted(daemons, key=lambda item: parent_pid(item[0]) in matched):
                stop_daemon(pid)
                log(f"stopped adapter daemon {pid} ({' '.join(argv)})")
            for usb_id in adapters:
                write_driver_action(usb_id, "unbind")
                log(f"temporarily detached GameCube adapter {usb_id}")
        except OSError as error:
            log(f"failed to take the GameCube adapter offline: {error}")
            restore_locked("start failure", config)
            return
        log(f"{system} launch: joystick devices {before} -> {joystick_count()}")
    spawn_watchdog(launcher_pid, start_time)


def on_restore(reason):
    config = load_config()
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        restore_locked(reason, config)


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
    if system not in load_config()["systems"]:
        return 0
    if event == "gameStart":
        on_game_start(os.getppid(), system)
    elif event == "gameStop":
        on_restore("gameStop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
