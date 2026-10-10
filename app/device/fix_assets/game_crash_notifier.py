#!/usr/bin/env python3
"""Batocera game hook that shows an EmulationStation toast when a game crashes.

Installed and removed by Batocera Drone's Admin Fixes page.  Batocera invokes
executable files in /userdata/system/scripts for gameStart and gameStop events.
At gameStart this hook starts a detached watcher; when the launcher exits it
scores the launch log, session length and kernel log, and posts a toast to
EmulationStation's loopback API if the game appears to have crashed.
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
USERDATA_ROOT = SCRIPT_PATH.parent.parent.parent
CONFIG_PATH = USERDATA_ROOT / "system" / "game-crash-notifier" / "config.json"
LOG_PATH = USERDATA_ROOT / "system" / "logs" / "game-crash-notifier.log"
HISTORY_PATH = USERDATA_ROOT / "system" / "game-crash-notifier" / "history.jsonl"
HISTORY_LIMIT = 50
BATOCERA_VERSION_PATH = Path(os.environ.get("DRONE_CRASH_NOTIFIER_VERSION_FILE", "/usr/share/batocera/batocera.version"))
MEMINFO_PATH = Path("/proc/meminfo")
LAUNCH_STDERR = USERDATA_ROOT / "system" / "logs" / "es_launch_stderr.log"
LAUNCH_STDOUT = USERDATA_ROOT / "system" / "logs" / "es_launch_stdout.log"
SYS_INPUT_ROOT = Path(os.environ.get("DRONE_CRASH_NOTIFIER_SYS_INPUT", "/sys/class/input"))
NOTIFY_URL = os.environ.get("DRONE_CRASH_NOTIFIER_URL", "http://127.0.0.1:1234/notify")

# The launcher logs the emulator's whole stderr as ERROR even on a clean exit,
# so only genuine crash text is matched, never the log level.
UNPLUG_ACTION = "Try unplugging USB controllers or adapters, then relaunch."
CRASH_SIGNATURES = (
    (r"stack smashing detected", "the emulator aborted with a memory-corruption error", UNPLUG_ACTION),
    (r"segmentation fault|SIGSEGV", "the emulator hit a segmentation fault", UNPLUG_ACTION),
    (r"core dumped", "the emulator crashed and dumped core", UNPLUG_ACTION),
    (r"\bAborted\b|SIGABRT", "the emulator aborted unexpectedly", UNPLUG_ACTION),
    (r"illegal instruction|SIGILL", "the emulator hit an illegal CPU instruction", "Try a different core or emulator for this system."),
    (r"bus error|SIGBUS", "the emulator hit a bus error", "Check the ROM file for corruption, then relaunch."),
    (r"Failed to load content|Failed to open content", "the game file could not be loaded", "Check the ROM file and any required BIOS."),
)
LAUNCHER_ACTION = "Check this system's emulator and core settings."
EXIT_STATUS_ACTION = "Check the ROM, any required BIOS and this system's emulator settings, then relaunch."
# The launcher runs the emulator directly, so a crash that prints nothing (a
# plain SIGSEGV, or an emulator that just exits with an error code) never
# reaches the stderr signatures above.  The launcher does log both outcomes to
# its own stdout log, so those lines are the fallback evidence.
SIGNAL_NOTICE = re.compile(r"Emulator terminated by signal \([^)]*?(\d+)\)")
EXIT_STATUS_NOTICE = re.compile(r"Exiting configgen with status (-?\d+)")
# Only signals that mean the emulator itself faulted.  SIGTERM/SIGINT/SIGHUP/
# SIGKILL are how Batocera closes a game normally, so they are never a crash.
CRASH_SIGNALS = {
    4: ("SIGILL", "the emulator hit an illegal CPU instruction", "Try a different core or emulator for this system."),
    6: ("SIGABRT", "the emulator aborted unexpectedly", UNPLUG_ACTION),
    7: ("SIGBUS", "the emulator hit a bus error", "Check the ROM file for corruption, then relaunch."),
    8: ("SIGFPE", "the emulator hit an arithmetic error", "Try a different core or emulator for this system."),
    11: ("SIGSEGV", "the emulator hit a segmentation fault", UNPLUG_ACTION),
    31: ("SIGSYS", "the emulator made an invalid system call", "Try a different core or emulator for this system."),
}
KERNEL_ACTION = "Close other apps or try a lighter core, then relaunch."
KERNEL_SIGNATURES = re.compile(r"segfault|general protection|out of memory|killed process|invoked oom-killer", re.IGNORECASE)
MEMORY_CORRUPTION = re.compile(r"stack smashing|segmentation fault|SIGSEGV|core dumped|SIGABRT|\bAborted\b", re.IGNORECASE)


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
    defaults = {"short_session_seconds": 15, "joystick_hint_threshold": 8}
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = {}
    if isinstance(raw, dict):
        for key in defaults:
            try:
                defaults[key] = max(1, int(raw.get(key, defaults[key])))
            except (TypeError, ValueError):
                pass
    return defaults


def proc_start_time(pid):
    try:
        # Field 22 is starttime; split after the final ')' because the process
        # name can contain spaces.
        tail = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return tail[19]
    except (OSError, IndexError):
        return None


def launcher_is_alive(pid, expected_start):
    current = proc_start_time(pid)
    return current is not None and (expected_start is None or current == expected_start)


def uptime_seconds():
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_new_text(path, offset):
    """Return text appended since ``offset``; re-read from 0 if the log was truncated."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(offset if 0 <= offset <= size else 0)
            return handle.read(512 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return ""


def read_new_tail(path, offset, limit=512 * 1024):
    """Like ``read_new_text`` but keeps the end, where the launcher logs how the session ended."""
    try:
        size = path.stat().st_size
        start = offset if 0 <= offset <= size else 0
        with path.open("rb") as handle:
            handle.seek(max(start, size - limit))
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def launcher_traceback(text):
    """True for a Python traceback raised by the launcher or a generator.

    Batocera helpers such as hotkeygen print a harmless traceback at the start
    of ordinary launches, so only blocks that touch launcher code count.  A
    block is just the indented frame lines under the header: the launcher's
    next log line can follow the exception with no blank line between them.
    """
    frames = []
    in_block = False
    for line in text.splitlines() + [""]:
        if line.startswith("Traceback (most recent call last):"):
            frames, in_block = [], True
        elif in_block and line[:1] in (" ", "\t"):
            frames.append(line)
        elif in_block:
            if re.search(r"emulatorlauncher|configgen|generators", "\n".join(frames)):
                return True
            in_block = False
    return False


def match_signature(text):
    for pattern, reason, action in CRASH_SIGNATURES:
        if re.search(pattern, text, re.IGNORECASE):
            return pattern, reason, action
    if launcher_traceback(text):
        return "launcher-traceback", "the Batocera launcher raised an error", LAUNCHER_ACTION
    return None


def launcher_outcome(launcher_text):
    """Return ``(fatal_signal, exit_status)`` the launcher logged for the session.

    Either value is ``None`` when the launcher did not log it.  The last match
    wins so a log containing several launches never reports an earlier one.
    """
    signals = SIGNAL_NOTICE.findall(launcher_text)
    statuses = EXIT_STATUS_NOTICE.findall(launcher_text)
    return (int(signals[-1]) if signals else None, int(statuses[-1]) if statuses else None)


def kernel_evidence(dmesg_text, start_uptime, end_uptime):
    """Return the first kernel crash line stamped inside [start, end] uptime seconds."""
    for line in dmesg_text.splitlines():
        match = re.match(r"\s*\[\s*(\d+(?:\.\d+)?)\]\s*(.*)", line)
        if not match or not KERNEL_SIGNATURES.search(match.group(2)):
            continue
        stamp = float(match.group(1))
        if start_uptime is None or end_uptime is None or start_uptime - 1 <= stamp <= end_uptime + 5:
            return match.group(2).strip()
    return ""


def read_dmesg():
    try:
        return subprocess.run(["dmesg"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def joystick_names():
    """Return one 'Name (xN)' label per distinct connected joystick name."""
    counts = {}
    try:
        for js_path in sorted(SYS_INPUT_ROOT.glob("js*")):
            try:
                name = (js_path / "device" / "name").read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                name = ""
            name = re.sub(r"\s+", " ", name) or js_path.name
            counts[name] = counts.get(name, 0) + 1
    except OSError:
        return []
    return [f"{name} (x{count})" if count > 1 else name for name, count in counts.items()]


def joystick_count():
    try:
        return len(list(SYS_INPUT_ROOT.glob("js*")))
    except OSError:
        return 0


def log_excerpt(text, pattern, max_lines=60, max_chars=8000):
    """Return the lines around the first crash signature (or the log tail)."""
    lines = text.splitlines()
    start = max(0, len(lines) - max_lines)
    if pattern:
        for index, line in enumerate(lines):
            if re.search(pattern, line, re.IGNORECASE):
                start = max(0, index - 15)
                break
    excerpt = "\n".join(lines[start:start + max_lines])
    return excerpt[-max_chars:]


def memory_available_mb():
    try:
        for line in MEMINFO_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def batocera_version():
    try:
        return BATOCERA_VERSION_PATH.read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError):
        return ""


def write_history(record):
    """Append one crash record, keeping only the newest HISTORY_LIMIT entries."""
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        if HISTORY_PATH.exists():
            lines = [line for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
        lines.append(json.dumps(record, sort_keys=True))
        HISTORY_PATH.write_text("\n".join(lines[-HISTORY_LIMIT:]) + "\n", encoding="utf-8")
    except OSError as error:
        log(f"could not write crash history: {error}")


def build_record(state, verdict, duration, log_text, evidence, messages, joysticks, device_names, launcher_text=""):
    rom = str(state.get("rom") or "")
    rom_size = None
    try:
        rom_size = Path(rom).stat().st_size if rom else None
    except OSError:
        pass
    if verdict.get("source") == "launcher":
        # The evidence is in the launcher's own log; stderr is just noise here.
        excerpt = log_excerpt(launcher_text, "")
    else:
        excerpt = log_excerpt(log_text, verdict["signature"] if verdict["signature"] not in ("kernel", "launcher-traceback") else "")
    return {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "epoch": int(time.time()),
        "game": Path(rom).stem if rom else "",
        "rom_path": rom,
        "rom_exists": rom_size is not None,
        "rom_size_bytes": rom_size,
        "system": state.get("system", ""),
        "emulator": state.get("emulator", ""),
        "core": state.get("core", ""),
        "duration_seconds": round(duration),
        "reason": verdict["reason"],
        "action": verdict["action"],
        "signature": verdict["signature"],
        "short_session": bool(verdict["short"]),
        "kernel_evidence": evidence,
        "joystick_count": joysticks,
        "joysticks": device_names,
        "memory_available_mb": memory_available_mb(),
        "batocera_version": batocera_version(),
        "hostname": socket.gethostname(),
        "toasts": messages,
        "log_excerpt": excerpt,
    }


def assess_launch(log_text, duration, dmesg_evidence, config, launcher_text=""):
    """Decide whether a session that just ended was a crash.

    A crash signature is enough on its own.  A short session is only suspicious
    when the kernel also recorded a fault or the emulator exited with an error
    status, so a player who quits right away is never flagged.  A fatal signal
    the launcher logged (the emulator faulted without printing anything) is a
    crash whatever the session length.
    """
    signature = match_signature(log_text)
    short = duration is not None and duration < config["short_session_seconds"]
    if signature:
        return {"crashed": True, "reason": signature[1], "signature": signature[0], "action": signature[2], "short": short}
    fatal_signal, exit_status = launcher_outcome(launcher_text)
    if fatal_signal in CRASH_SIGNALS:
        name, reason, action = CRASH_SIGNALS[fatal_signal]
        return {"crashed": True, "reason": reason, "signature": f"signal-{name}", "action": action, "short": short, "source": "launcher"}
    if short and dmesg_evidence:
        return {"crashed": True, "reason": "the emulator was killed by the system", "signature": "kernel", "action": KERNEL_ACTION, "short": True}
    if short and exit_status:
        return {
            "crashed": True,
            "reason": f"the emulator exited with an error (status {exit_status})",
            "signature": f"exit-status-{exit_status}",
            "action": EXIT_STATUS_ACTION,
            "short": True,
            "source": "launcher",
        }
    return {"crashed": False, "reason": "", "signature": "", "action": "", "short": short}


def build_messages(system, rom, verdict, joysticks, device_names, config, hostname):
    """Return the toasts to show in order: what happened, then what to do."""
    game = Path(str(rom)).stem if rom else "The game"
    # Keep each message glanceable: EmulationStation toasts disappear quickly.
    first = f"{game} ({system}) crashed: {verdict['reason']}."
    action = verdict["action"]
    if joysticks > config["joystick_hint_threshold"] and MEMORY_CORRUPTION.search(verdict["signature"] + " " + verdict["reason"]):
        action = f"{joysticks} controllers connected ({', '.join(device_names)}). {UNPLUG_ACTION}"
    return [first, f"{action} Details: Admin > Debug > Game Crashes on {hostname}."]


def post_toast(message):
    try:
        request = urllib.request.Request(NOTIFY_URL, data=message.encode("utf-8"), method="POST")
        with urllib.request.urlopen(request, timeout=3) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def send_notifications(messages, wait_seconds=30, gap_seconds=6):
    """Post toasts in order once EmulationStation is back; return how many were accepted."""
    deadline = time.time() + wait_seconds
    time.sleep(2)
    while not post_toast(messages[0]):
        if time.time() >= deadline:
            return 0
        time.sleep(2)
    delivered = 1
    for message in messages[1:]:
        # Let the previous toast finish so the second one is not swallowed.
        time.sleep(gap_seconds)
        delivered += 1 if post_toast(message) else 0
    return delivered


def spawn_watcher(state):
    try:
        subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH), "watch", json.dumps(state)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as error:
        log(f"could not start crash watcher: {error}")


def on_game_start(argv, launcher_pid):
    offsets = []
    for path in (LAUNCH_STDERR, LAUNCH_STDOUT):
        try:
            offsets.append(path.stat().st_size)
        except OSError:
            offsets.append(0)
    spawn_watcher(
        {
            "pid": launcher_pid,
            "start_time": proc_start_time(launcher_pid),
            "system": argv[1] if len(argv) > 1 else "",
            "rom": argv[4] if len(argv) > 4 else "",
            "emulator": argv[2] if len(argv) > 2 else "",
            "core": argv[3] if len(argv) > 3 else "",
            "started": time.time(),
            "start_uptime": uptime_seconds(),
            "stderr_offset": offsets[0],
            "stdout_offset": offsets[1],
        }
    )


def watch(state):
    config = load_config()
    while launcher_is_alive(state["pid"], state.get("start_time")):
        time.sleep(1)
    duration = time.time() - state["started"]
    log_text = read_new_text(LAUNCH_STDERR, int(state.get("stderr_offset") or 0))
    # Older watchers carry no stdout offset; skip the launcher log rather than
    # re-reading a whole log that may describe earlier launches.
    launcher_text = read_new_tail(LAUNCH_STDOUT, int(state["stdout_offset"])) if state.get("stdout_offset") is not None else ""
    evidence = kernel_evidence(read_dmesg(), state.get("start_uptime"), uptime_seconds())
    verdict = assess_launch(log_text, duration, evidence, config, launcher_text)
    if not verdict["crashed"]:
        log(f"{state['system']}: clean exit after {duration:.0f}s")
        return
    joysticks, names = joystick_count(), joystick_names()
    messages = build_messages(state["system"], state["rom"], verdict, joysticks, names, config, socket.gethostname())
    # Persist the evidence before the toast wait so a killed watcher cannot lose it.
    write_history(build_record(state, verdict, duration, log_text, evidence, messages, joysticks, names, launcher_text))
    delivered = send_notifications(messages)
    log(f"{state['system']}: crash after {duration:.0f}s ({verdict['signature']}); {delivered}/{len(messages)} toasts delivered: {' | '.join(messages)}")


def main(argv):
    if not argv:
        return 0
    event = argv[0]
    if event == "watch" and len(argv) >= 2:
        watch(json.loads(argv[1]))
    elif event == "gameStart":
        # Batocera hook arguments: event, system, emulator, core, rom.  The
        # hook's parent is emulatorlauncher, whose exit marks the session end.
        on_game_start(argv, os.getppid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
