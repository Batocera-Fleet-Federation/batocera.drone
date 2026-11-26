#!/usr/bin/env python3
"""Batocera user service that keeps the Wii U GameCube adapter driver healthy.

Installed and removed by Batocera Drone's Admin Fixes page as
/userdata/system/services/DRONE_GC_ADAPTER and started by Batocera's
S99userservices at boot ('start' / 'stop' / 'status').

Batocera drives a Wii U GameCube adapter (057e:0337) with the userspace
wii-u-gc-adapter daemon, started by a udev rule with no ACTION filter that
matches both the USB device and its interface.  One plug-in (or boot)
therefore runs the rule about six times, and each run kills the previous
daemon and starts another.  The survivor can open the adapter while an
instance being killed still has its interface claimed; its setup then fails
part-way, it never retries, and it sits idle holding the device open: the
adapter shows in lsusb but no "Wii U GameCube Adapter Port N" joystick ever
appears, and replugging just reruns the same race.

A healthy daemon runs one reader thread per adapter on top of its main and
libusb event threads.  Once the adapter and daemon have been unchanged for a
few seconds (udev's burst is over), this watcher checks for exactly one
daemon with that reader thread.  Otherwise it stops every instance and
starts exactly one, at most a few times per plug-in.  If that still fails it
shows an EmulationStation message.  It never touches USB binding, never acts
while the adapter is unbound (the Switch 2 GameCube adapter guard takes it
offline on purpose), and never stops a daemon it is not about to replace.
"""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
USERDATA_ROOT = Path(os.environ.get("DRONE_GC_FIX_USERDATA", str(SCRIPT_PATH.parent.parent.parent)))
CONFIG_PATH = USERDATA_ROOT / "system" / "gamecube-adapter-fix" / "config.json"
LOG_PATH = USERDATA_ROOT / "system" / "logs" / "gamecube-adapter-fix.log"
RUNTIME_ROOT = Path(os.environ.get("DRONE_GC_FIX_RUNTIME_DIR", "/var/run"))
PID_PATH = RUNTIME_ROOT / "drone-gamecube-adapter-fix.pid"
LOCK_PATH = RUNTIME_ROOT / "drone-gamecube-adapter-fix.lock"
# The Switch 2 GameCube adapter guard's files: while its state exists it has
# taken the adapter offline on purpose, and its lock serialises restarts.
GUARD_STATE_PATH = RUNTIME_ROOT / "drone-switch2-gamecube-guard.json"
GUARD_LOCK_PATH = RUNTIME_ROOT / "drone-switch2-gamecube-guard.lock"
USB_DEVICES_ROOT = Path(os.environ.get("DRONE_GC_FIX_USB_DEVICES", "/sys/bus/usb/devices"))
PROC_ROOT = Path(os.environ.get("DRONE_GC_FIX_PROC", "/proc"))
NOTIFY_URL = os.environ.get("DRONE_GC_FIX_NOTIFY_URL", "http://127.0.0.1:1234/notify")

DEFAULT_CONFIG = {
    "usb_ids": ["057e:0337"],
    "daemon_path": "/usr/bin/wii-u-gc-adapter",
    "wrapper_names": ["wii-u-gc-adapter-daemon"],
    "interval_seconds": 2.0,
    "settle_seconds": 5.0,
    "max_repairs": 3,
}
STOP_TIMEOUT_SECONDS = 3.0
# EmulationStation may still be starting when a boot-time repair gives up.
TOAST_RETRY_SECONDS = 180.0
TOAST_MAX_CHARS = 80
FAILURE_TOAST = "GameCube adapter driver failed to start. Unplug the adapter and plug it back in."
MISSING_DAEMON_TOAST = "GameCube adapter: Batocera's wii-u-gc-adapter driver is missing."


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
    config = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    for key in ("usb_ids", "wrapper_names"):
        values = raw.get(key)
        if isinstance(values, list):
            cleaned = [str(value).strip().lower() for value in values if str(value).strip()]
            if cleaned:
                config[key] = cleaned
    if isinstance(raw.get("daemon_path"), str) and raw["daemon_path"].startswith("/"):
        config["daemon_path"] = raw["daemon_path"]
    for key, low, high in (("interval_seconds", 0.5, 60.0), ("settle_seconds", 1.0, 120.0)):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            config[key] = min(max(float(value), low), high)
    value = raw.get("max_repairs")
    if isinstance(value, int) and not isinstance(value, bool):
        config["max_repairs"] = min(max(value, 1), 10)
    return config


def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def find_adapters(config):
    """Bound adapters as stable keys '<sysfs name>@<busnum>.<devnum>'.

    devnum changes on every replug, so a new key means a new plug-in.  An
    unbound adapter has been taken offline on purpose and is left out.
    """
    wanted = set(config["usb_ids"])
    found = []
    try:
        entries = sorted(USB_DEVICES_ROOT.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return found
    for entry in entries:
        if ":" in entry.name:  # interfaces carry no idVendor
            continue
        usb_id = f"{read_text(entry / 'idVendor').lower()}:{read_text(entry / 'idProduct').lower()}"
        if usb_id in wanted and (entry / "driver").exists():
            found.append(f"{entry.name}@{read_text(entry / 'busnum')}.{read_text(entry / 'devnum')}")
    return found


def process_argv(pid):
    try:
        raw = (PROC_ROOT / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def scan_processes(config):
    """(daemon pids, wrapper running?).  The daemon is matched on argv[0]; the
    udev wrapper script is a shell running it, so argv[1] is checked too."""
    daemon_name = os.path.basename(config["daemon_path"]).lower()
    wrappers = set(config["wrapper_names"])
    own = os.getpid()
    daemons, wrapper = [], False
    try:
        entries = list(PROC_ROOT.iterdir())
    except OSError:
        return daemons, wrapper
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) == own:
            continue
        argv = process_argv(entry.name)
        names = [os.path.basename(arg).lower() for arg in argv[:2]]
        if names and names[0] == daemon_name:
            daemons.append(int(entry.name))
        elif any(name in wrappers for name in names):
            wrapper = True
    return sorted(daemons), wrapper


def thread_count(pid):
    try:
        return len(list((PROC_ROOT / str(pid) / "task").iterdir()))
    except OSError:
        return 0


def assess(adapters, daemons):
    """None when healthy, else a short reason."""
    if not daemons:
        return "the adapter driver is not running"
    if len(daemons) > 1:
        return f"{len(daemons)} adapter drivers are running"
    threads = thread_count(daemons[0])
    if threads < 2 + len(adapters):
        return f"the adapter driver is stuck ({threads} threads, no adapter reader)"
    return None


def pid_alive(pid):
    return (PROC_ROOT / str(pid)).exists()


def stop_process(pid):
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


def start_daemon(config):
    subprocess.Popen(
        [config["daemon_path"]],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def repair(config, daemons):
    """Stop every daemon instance and start exactly one.  True when started."""
    if not Path(config["daemon_path"]).is_file():
        log(f"cannot repair: {config['daemon_path']} is missing")
        return False
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    with GUARD_LOCK_PATH.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("Switch 2 GameCube adapter guard is busy; repair postponed")
            return False
        if GUARD_STATE_PATH.exists():
            return False
        for pid in daemons:
            stop_process(pid)
        time.sleep(1.0)
        try:
            start_daemon(config)
        except OSError as error:
            log(f"could not start {config['daemon_path']}: {error}")
            return False
    log(f"restarted {config['daemon_path']} (stopped {len(daemons)} instance(s): {daemons})")
    return True


def toast_text(text):
    text = " ".join(str(text).split())
    return text if len(text) <= TOAST_MAX_CHARS else text[:TOAST_MAX_CHARS - 3].rstrip() + "..."


def post_toast(message):
    try:
        request = urllib.request.Request(NOTIFY_URL, data=toast_text(message).encode("utf-8"), method="POST")
        with urllib.request.urlopen(request, timeout=3) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


class Watcher:
    """One tick at a time, so tests can drive it with a fake clock."""

    def __init__(self, config, clock=time.monotonic, repairer=repair, notifier=post_toast):
        self.config = config
        self.clock = clock
        self.repairer = repairer
        self.notifier = notifier
        self.signature = None
        self.changed_at = 0.0
        self.plug_key = None
        self.repairs = 0
        self.gave_up = False
        self.pending_toast = None
        self.toast_deadline = 0.0

    def _reset_plug(self, key):
        self.plug_key = key
        self.repairs = 0
        self.gave_up = False
        self.pending_toast = None

    def _give_up(self, reason, message):
        self.gave_up = True
        log(f"giving up: {reason}")
        self.pending_toast = message
        self.toast_deadline = self.clock() + TOAST_RETRY_SECONDS
        self._flush_toast()
        return "gave_up"

    def _flush_toast(self):
        if self.pending_toast is None:
            return
        if self.notifier(self.pending_toast):
            log(f"EmulationStation message shown: {self.pending_toast}")
            self.pending_toast = None
        elif self.clock() > self.toast_deadline:
            log("EmulationStation did not accept the message; giving up on it")
            self.pending_toast = None

    def tick(self):
        """Returns what happened: idle, settling, healthy, repaired, failed, gave_up."""
        self._flush_toast()
        now = self.clock()
        if GUARD_STATE_PATH.exists():
            self.signature = None
            return "idle"
        adapters = find_adapters(self.config)
        if not adapters:
            if self.plug_key is not None:
                self._reset_plug(None)
            self.signature = None
            return "idle"
        key = tuple(adapters)
        if key != self.plug_key:
            self._reset_plug(key)
        daemons, wrapper = scan_processes(self.config)
        signature = (key, tuple(daemons), wrapper)
        if signature != self.signature:
            self.signature = signature
            self.changed_at = now
        if wrapper or now - self.changed_at < self.config["settle_seconds"]:
            return "settling"
        problem = assess(adapters, daemons)
        if problem is None:
            if self.repairs:
                log(f"adapter driver healthy after {self.repairs} repair(s)")
                self.repairs = 0
            return "healthy"
        if self.gave_up:
            return "gave_up"
        if not Path(self.config["daemon_path"]).is_file():
            return self._give_up(f"{self.config['daemon_path']} is missing", MISSING_DAEMON_TOAST)
        if self.repairs >= self.config["max_repairs"]:
            return self._give_up(f"{problem} after {self.repairs} repair(s)", FAILURE_TOAST)
        log(f"adapter {', '.join(adapters)}: {problem}")
        self.repairs += 1
        started = self.repairer(self.config, daemons)
        # Wait a full settle period before judging the new instance.
        self.signature = None
        return "repaired" if started else "failed"


def run():
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    lock = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # another watcher already runs
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    config = load_config()
    log("watcher started")
    watcher = Watcher(config)
    while not stopping:
        try:
            watcher.tick()
        except Exception as error:  # one bad tick must not end the watcher
            log(f"unexpected error: {error!r}")
        time.sleep(config["interval_seconds"])
    log("watcher stopped")
    try:
        PID_PATH.unlink()
    except FileNotFoundError:
        pass
    return 0


def running_pid():
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    argv = process_argv(pid)
    return pid if any(SCRIPT_PATH.name in arg for arg in argv) else None


def start():
    if running_pid():
        return 0
    subprocess.Popen(
        [sys.executable, str(SCRIPT_PATH), "run"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    return 0


def stop():
    pid = running_pid()
    if pid:
        stop_process(pid)
    return 0


def main(argv):
    action = argv[0] if argv else ""
    if action == "run":
        return run()
    if action == "start":
        return start()
    if action == "stop":
        return stop()
    if action == "restart":
        stop()
        return start()
    if action == "status":
        pid = running_pid()
        print(f"running (pid {pid})" if pid else "stopped")
        return 0
    if action == "check":  # one pass now, for diagnostics
        watcher = Watcher(load_config())
        watcher.config["settle_seconds"] = 0
        print(watcher.tick())
        return 0
    print(f"Usage: {SCRIPT_PATH.name} start|stop|restart|status|check")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
