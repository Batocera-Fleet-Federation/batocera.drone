#!/usr/bin/env python3
"""Apply a session-only Sinden recoil preset from a Batocera game hook."""

from contextlib import contextmanager
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


SYSTEM_ROOT = Path(__file__).resolve().parent.parent
CONFIG = SYSTEM_ROOT / "sinden-recoil" / "config.json"
RUN = Path("/var/run")
DRIVER_ROOT = RUN / "sinden"
RUNTIME = RUN / "drone-sinden-recoil"
PROC = Path("/proc")
DRIVER_LOG_ROOT = Path("/var/log")
MONO = "/usr/bin/mono"
DRIVER_SERVICE = "/usr/lib/mono/4.5/mono-service.exe"
PRESETS = {
    "single": ("Single Shot", {"EnableRecoil": "1", "TriggerRecoilNormalOrRepeat": "0"}),
    "machinegun": ("Machine Gun", {
        "EnableRecoil": "1", "TriggerRecoilNormalOrRepeat": "1",
        "AutoRecoilStrength": "100", "AutoRecoilStartDelay": "1", "AutoRecoilDelayBetweenPulses": "10",
    }),
    "machinegun-quiet": ("Quiet Machine Gun", {
        "EnableRecoil": "1", "TriggerRecoilNormalOrRepeat": "1",
        "AutoRecoilStrength": "40", "AutoRecoilStartDelay": "1", "AutoRecoilDelayBetweenPulses": "10",
    }),
    "gun": ("Gun (Delayed Repeat)", {
        "EnableRecoil": "1", "TriggerRecoilNormalOrRepeat": "1",
        "AutoRecoilStrength": "100", "AutoRecoilStartDelay": "150", "AutoRecoilDelayBetweenPulses": "10",
    }),
    "gun-quiet": ("Quiet Gun (Delayed Repeat)", {
        "EnableRecoil": "1", "TriggerRecoilNormalOrRepeat": "1",
        "AutoRecoilStrength": "40", "AutoRecoilStartDelay": "150", "AutoRecoilDelayBetweenPulses": "10",
    }),
    "off": ("No Recoil", {"EnableRecoil": "0"}),
}
LOG = logging.getLogger("drone-sinden-recoil")


def game_key(system, rom):
    if not isinstance(system, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", system):
        raise ValueError("Invalid Batocera system")
    if not isinstance(rom, str) or not rom or len(rom) > 4096 or re.search(r"[\x00-\x1f\x7f]", rom):
        raise ValueError("Invalid ROM path")
    path = PurePosixPath(rom)
    if path.is_absolute() or ".." in path.parts or not path.parts or str(path) != rom:
        raise ValueError("Use a ROM path relative to its system folder")
    return f"{system}/{rom}"


def read_json(path, default):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return default


def atomic_write(path, content):
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def process_identity(pid):
    try:
        fields = (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] not in {"Z", "X"}:
            return [pid, fields[19]]
    except (OSError, IndexError):
        pass
    return None


def process_args(pid):
    try:
        return [os.fsdecode(part) for part in (PROC / str(pid) / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")]
    except OSError:
        return []


def pid_from_file(path):
    try:
        pid = int(path.read_text().strip())
        return pid if pid > 1 else None
    except (OSError, ValueError):
        return None


class Gun:
    def __init__(self, device_hash):
        if not re.fullmatch(r"[0-9a-f]{32}", device_hash):
            raise ValueError("Invalid Sinden device ID")
        self.device_hash = device_hash
        self.directory = DRIVER_ROOT / f"p{device_hash}"
        self.exe = f"LightgunMono-{device_hash}.exe"
        self.config = self.directory / f"{self.exe}.config"
        self.lockfile = self.directory / "lockfile"
        self.mapper = RUN / f"virtual-sindenlightgun-devices.{device_hash}.pid"

    def mapper_identity(self):
        pid = pid_from_file(self.mapper)
        args = process_args(pid) if pid else []
        if args and Path(args[0]).name == "evsieve" and "name=Sinden lightgun" in args:
            return process_identity(pid)
        return None

    @property
    def command(self):
        return [MONO, DRIVER_SERVICE, f"-l:{self.lockfile}", f"-d:{self.directory}", f"./{self.exe}"]

    def driver_process(self, pid):
        args = process_args(pid) if pid else []
        if args == self.command:
            return process_identity(pid)
        return None

    def driver_identity(self):
        return self.driver_process(pid_from_file(self.lockfile))

    def config_tree(self):
        tree = ET.parse(self.config, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
        return tree, {entry.attrib["key"]: entry for entry in tree.findall("./appSettings/add")}

    def config_values(self, keys):
        _, entries = self.config_tree()
        return {key: entries[key].attrib["value"] for key in keys}

    def restart(self, values, mapper_identity):
        if self.mapper_identity() != mapper_identity:
            return False
        driver = self.driver_identity()
        if not driver:
            # Recovery can run after a new Mono process has been spawned and
            # before it has published its PID in the lock file.
            for path in PROC.iterdir():
                if path.name.isdecimal() and (driver := self.driver_process(int(path.name))):
                    break
        if driver:
            self.stop_driver(driver)
        if self.mapper_identity() != mapper_identity:
            return False
        tree, entries = self.config_tree()
        for key, value in values.items():
            entries[key].set("value", value)
        atomic_write(self.config, ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True))
        if self.mapper_identity() != mapper_identity:
            return False
        self.lockfile.unlink(missing_ok=True)
        serial = entries["SerialPortWrite"].attrib["value"]
        log_path = DRIVER_LOG_ROOT / f"virtual-sindenlightgun-devices.{self.device_hash}.log2"
        with log_path.open("ab") as output:
            child = subprocess.Popen(
                self.command, cwd=self.directory, env={"PATH": "/bin:/sbin:/usr/bin:/usr/sbin"},
                stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True,
            )
        try:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and child.poll() is None:
                if self.mapper_identity() != mapper_identity:
                    stop_child(child)
                    return False
                identity = self.driver_identity()
                if identity and identity[0] == child.pid and driver_has_devices(child.pid, serial):
                    return True
                time.sleep(0.05)
            raise OSError(f"Sinden driver for {self.device_hash} did not reopen its devices")
        except BaseException:
            stop_child(child)
            raise

    def stop_driver(self, identity):
        pid = identity[0]
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise OSError("Sinden recoil needs Linux process descriptors")
        try:
            pidfd = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            if self.driver_process(pid) == identity:
                signal.pidfd_send_signal(pidfd, signal.SIGTERM)
        except ProcessLookupError:
            return
        finally:
            os.close(pidfd)
        deadline = time.monotonic() + 3
        while process_identity(pid) == identity:
            if time.monotonic() >= deadline:
                raise OSError(f"Sinden driver for {self.device_hash} did not stop")
            time.sleep(0.05)


def driver_has_devices(pid, serial):
    devices = set()
    try:
        for fd in (PROC / str(pid) / "fd").iterdir():
            try:
                devices.add(os.readlink(fd))
            except FileNotFoundError:
                pass
    except OSError:
        return False
    return serial in devices and any(re.fullmatch(r"/dev/video[0-9]+", value) for value in devices)


def stop_child(child):
    child.terminate()
    try:
        child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=1)


@contextmanager
def session_lock():
    import fcntl

    with (RUNTIME / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def load_session():
    return read_json(RUNTIME / "session.json", None)


def save_session(session):
    atomic_write(RUNTIME / "session.json", json.dumps(session).encode())


def restore(session):
    if not session:
        return True
    failures = []
    for entry in session["guns"]:
        gun = Gun(entry["device"])
        try:
            gun.restart(entry["values"], entry["mapper"])
        except (OSError, ValueError, KeyError, ET.ParseError, subprocess.SubprocessError) as error:
            failures.append(entry)
            LOG.warning("Restore for Sinden %s is pending: %s", entry["device"], error)
    if failures:
        session["guns"] = failures
        save_session(session)
        return False
    (RUNTIME / "session.json").unlink(missing_ok=True)
    LOG.info("Restored Batocera recoil settings for %r", session["game"])
    return True


def launch_game_key(system, rom, launcher_pid, root):
    # Some launchers pass a temporary mount path to hooks for writable ROMs.
    # The original --rom/-rom argument matches the library in both cases.
    args = process_args(launcher_pid)
    for index, arg in enumerate(args[:-1]):
        if arg in {"-rom", "--rom"}:
            rom = args[index + 1]
            break
    base = Path(root) / system
    path = Path(rom)
    try:
        return game_key(system, path.relative_to(base).as_posix())
    except ValueError:
        return None


def start(system, rom, launcher_pid, owner):
    previous = load_session()
    if previous and process_identity(previous["owner"][0]) == previous["owner"]:
        return
    if not restore(previous):
        return
    config = read_json(CONFIG, {})
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return
    game = launch_game_key(system, rom, launcher_pid, config["roms_root"])
    preset = config["profiles"].get(game)
    if not isinstance(preset, str) or preset not in PRESETS:
        return
    values = PRESETS[preset][1]
    candidates = []
    for directory in sorted(DRIVER_ROOT.glob("p*")):
        if not directory.is_dir() or not re.fullmatch(r"p[0-9a-f]{32}", directory.name):
            continue
        gun = Gun(directory.name[1:])
        mapper = gun.mapper_identity()
        if mapper and gun.driver_identity():
            original = gun.config_values(values)
            if original != values:
                candidates.append((gun, {"device": gun.device_hash, "values": original, "mapper": mapper}))
    if not candidates:
        LOG.info("No running Sinden driver needed a recoil change for %r", game)
        return
    session = {"owner": owner, "game": game, "guns": []}
    save_session(session)
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "watch", str(owner[0]), owner[1]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for gun, entry in candidates:
            session["guns"].append(entry)
            save_session(session)
            if not gun.restart(values, entry["mapper"]):
                raise OSError(f"Sinden {gun.device_hash} disconnected during the switch")
        LOG.info("Applied %s recoil to %r", preset, game)
    except (OSError, ValueError, KeyError, ET.ParseError, subprocess.SubprocessError):
        LOG.exception("Could not apply Sinden recoil")
        restore(session)


def watch(owner):
    while process_identity(owner[0]) == owner:
        session = load_session()
        if not session or session["owner"] != owner:
            return
        time.sleep(0.5)
    for _ in range(3):
        with session_lock():
            session = load_session()
            if not session or session["owner"] != owner or restore(session):
                return
        time.sleep(1)


def main(argv):
    if not argv or argv[0] not in {"gameStart", "gameStop", "watch", "restore"}:
        return 0
    event = argv[0]
    if event in {"gameStart", "gameStop"} and len(argv) != 5:
        return 0
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    log_path = SYSTEM_ROOT / "logs" / "sinden-recoil.log"
    log_path.parent.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=256 * 1024, backupCount=1)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    try:
        if event == "watch":
            watch([int(argv[1]), argv[2]])
            return 0
        with session_lock():
            if event == "restore":
                return 0 if restore(load_session()) else 1
            pid = os.getppid()
            owner = process_identity(pid)
            if owner and event == "gameStart":
                start(argv[1], argv[4], pid, owner)
            elif owner:
                session = load_session()
                if session and session["owner"] == owner:
                    restore(session)
    except (OSError, ValueError, KeyError, ET.ParseError, subprocess.SubprocessError):
        LOG.exception("Sinden recoil hook failed during %s", event)
        return 1 if event == "restore" else 0
    finally:
        LOG.removeHandler(handler)
        handler.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
