"""Device automations and input-activity tracking.

Extracted from ``drone_api.py``. Polls the last-input-activity timestamp (written by
the privileged input monitor) and, after an idle threshold, sets the volume to the
configured target (raising or lowering it, whichever the target requires); also
reports the idle-volume config to Overmind. Config persists in the state DB.
"""

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from threading import Thread
from typing import Any, Optional
from urllib.parse import quote

try:
    from ..common.settings import Settings
    from ..common.logging_setup import _overmind_log
    from ..overmind.overmind_client import _format_overmind_error, _overmind_post_json
    from ..overmind.overmind_config import _load_overmind_config_for_settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..transfer import local_network as _local_network
    from .device_control import _apply_audio_volume, _get_audio_volume, _kill_running_emulator
    from ..overmind.overmind_game_logs import find_running_emulatorlauncher as _find_running_emulatorlauncher
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from common.logging_setup import _overmind_log  # type: ignore
    from overmind.overmind_client import _format_overmind_error, _overmind_post_json  # type: ignore
    from overmind.overmind_config import _load_overmind_config_for_settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from device.device_control import _apply_audio_volume, _get_audio_volume, _kill_running_emulator  # type: ignore
    from overmind.overmind_game_logs import find_running_emulatorlauncher as _find_running_emulatorlauncher  # type: ignore


AUTOMATION_STATE_NAMESPACE = "automation_config.json"
AUTOMATION_POLL_SECONDS = 15
DEFAULT_IDLE_VOLUME_MINUTES = 5
DEFAULT_IDLE_VOLUME_TARGET = 25
DEFAULT_IDLE_GAME_EXIT_MINUTES = 15
WIFI_RECOVERY_CHECK_SECONDS = 60
WIFI_RECOVERY_RESET_SECONDS = 3
INPUT_ACTIVITY_FILENAME = "last-input-activity"


# Last input-activity timestamp idle-volume/idle-game-exit were armed against (module
# state; drone_api clears these on config changes via the matching _reset_*_armed_state()).
_IDLE_VOLUME_LAST_ARMED_ACTIVITY: Optional[float] = None
_IDLE_GAME_EXIT_LAST_ARMED_ACTIVITY: Optional[float] = None
_WIFI_RECOVERY_LAST_CHECK_MONOTONIC: Optional[float] = None
_WIFI_RECOVERY_RUNTIME = {
    "last_check_epoch": None,
    "last_recovery_epoch": None,
    "wifi_enabled": None,
    "wifi_connected": None,
    "wireless_interfaces": [],
    "last_error": None,
}


def _reset_idle_volume_armed_state() -> None:
    global _IDLE_VOLUME_LAST_ARMED_ACTIVITY
    _IDLE_VOLUME_LAST_ARMED_ACTIVITY = None


def _reset_idle_game_exit_armed_state() -> None:
    global _IDLE_GAME_EXIT_LAST_ARMED_ACTIVITY
    _IDLE_GAME_EXIT_LAST_ARMED_ACTIVITY = None


def _reset_wifi_recovery_check_state() -> None:
    global _WIFI_RECOVERY_LAST_CHECK_MONOTONIC
    _WIFI_RECOVERY_LAST_CHECK_MONOTONIC = None


def _input_activity_file_path() -> Path:
    override = os.environ.get("DRONE_INPUT_ACTIVITY_FILE")
    if override:
        return Path(override)
    control_dir = Path(os.environ.get("DRONE_SERVICE_CONTROL_DIR", "/userdata/system/drone-app/control"))
    return control_dir / INPUT_ACTIVITY_FILENAME


def _read_last_input_activity() -> Optional[float]:
    """Epoch seconds of the most recent input the privileged monitor saw, or None."""
    try:
        text = _input_activity_file_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        return None


def _normalize_idle_volume_config(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    try:
        idle_minutes = int(raw.get("idle_minutes", DEFAULT_IDLE_VOLUME_MINUTES))
    except (TypeError, ValueError):
        idle_minutes = DEFAULT_IDLE_VOLUME_MINUTES
    try:
        target_volume = int(raw.get("target_volume", DEFAULT_IDLE_VOLUME_TARGET))
    except (TypeError, ValueError):
        target_volume = DEFAULT_IDLE_VOLUME_TARGET
    return {
        "enabled": bool(raw.get("enabled", False)),
        "idle_minutes": max(1, min(1440, idle_minutes)),
        "target_volume": max(0, min(100, target_volume)),
    }


def _normalize_idle_game_exit_config(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    try:
        idle_minutes = int(raw.get("idle_minutes", DEFAULT_IDLE_GAME_EXIT_MINUTES))
    except (TypeError, ValueError):
        idle_minutes = DEFAULT_IDLE_GAME_EXIT_MINUTES
    return {
        "enabled": bool(raw.get("enabled", False)),
        "idle_minutes": max(1, min(1440, idle_minutes)),
    }


def _normalize_wifi_recovery_config(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {"enabled": bool(raw.get("enabled", False))}


def _load_automation_config(settings: Settings) -> dict:
    stored = _load_state_payload(
        _state_database_path(settings.userdata_root),
        AUTOMATION_STATE_NAMESPACE,
        {},
    )
    stored = stored if isinstance(stored, dict) else {}
    return {
        "idle_volume": _normalize_idle_volume_config(stored.get("idle_volume")),
        "idle_game_exit": _normalize_idle_game_exit_config(stored.get("idle_game_exit")),
        "wifi_recovery": _normalize_wifi_recovery_config(stored.get("wifi_recovery")),
    }


def _save_automation_config(settings: Settings, config: dict) -> dict:
    """Normalize + persist automation config. Callers may pass just the section they
    changed (e.g. only "idle_volume") — the other section is preserved from what's
    already stored rather than being reset to defaults."""
    config = config if isinstance(config, dict) else {}
    existing = _load_automation_config(settings)
    normalized = {
        "idle_volume": _normalize_idle_volume_config(
            config["idle_volume"] if "idle_volume" in config else existing["idle_volume"]
        ),
        "idle_game_exit": _normalize_idle_game_exit_config(
            config["idle_game_exit"] if "idle_game_exit" in config else existing["idle_game_exit"]
        ),
        "wifi_recovery": _normalize_wifi_recovery_config(
            config["wifi_recovery"] if "wifi_recovery" in config else existing["wifi_recovery"]
        ),
    }
    _save_state_payload(
        _state_database_path(settings.userdata_root),
        AUTOMATION_STATE_NAMESPACE,
        normalized,
    )
    return normalized


def _read_batocera_config_value(path: Path, key: str) -> Optional[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    value = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        candidate, candidate_value = line.split("=", 1)
        if candidate.strip() == key:
            value = candidate_value.strip().strip('"').strip("'")
    return value


def _wifi_is_enabled(settings: Settings) -> Optional[bool]:
    value = _read_batocera_config_value(settings.batocera_conf_file, "wifi.enabled")
    if value is None:
        getter = shutil.which("batocera-settings-get")
        if getter:
            try:
                result = subprocess.run(
                    [getter, "wifi.enabled"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    value = (result.stdout or "").strip()
            except (OSError, subprocess.SubprocessError):
                pass
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


def _wireless_interfaces() -> list[str]:
    network_root = Path(os.environ.get("DRONE_SYS_CLASS_NET", "/sys/class/net"))
    try:
        return sorted(entry.name for entry in network_root.iterdir() if (entry / "wireless").exists())
    except OSError:
        return []


def _wireless_default_route_connected(interfaces: list[str]) -> bool:
    if not interfaces:
        return False
    route_path = Path(os.environ.get("DRONE_PROC_NET_ROUTE", "/proc/net/route"))
    try:
        rows = route_path.read_text(encoding="utf-8", errors="ignore").splitlines()[1:]
    except OSError:
        return False
    interface_names = set(interfaces)
    for row in rows:
        fields = row.split()
        if len(fields) < 4 or fields[0] not in interface_names or fields[1] != "00000000":
            continue
        try:
            if int(fields[3], 16) & 0x1:
                return True
        except ValueError:
            continue
    return False


def _wifi_health(settings: Settings) -> dict:
    interfaces = _wireless_interfaces()
    connected = _wireless_default_route_connected(interfaces)
    enabled = _wifi_is_enabled(settings)
    if enabled is None and connected:
        enabled = True
    return {
        "wifi_enabled": enabled,
        "wifi_connected": connected,
        "wireless_interfaces": interfaces,
    }


def _recover_wifi() -> None:
    tool = shutil.which("batocera-wifi")
    if not tool:
        raise OSError("batocera-wifi command was not found")
    subprocess.run([tool, "disable"], check=True, timeout=30)
    time.sleep(WIFI_RECOVERY_RESET_SECONDS)
    subprocess.run([tool, "enable"], check=True, timeout=30)


def _wifi_recovery_status(settings: Settings) -> dict:
    status = dict(_WIFI_RECOVERY_RUNTIME)
    status.update(_wifi_health(settings))
    return status


def _run_wifi_recovery_automation_once(
    settings: Settings,
    *,
    now_monotonic: Optional[float] = None,
) -> None:
    """Every minute, reset Wi-Fi when it is disabled or lacks a wireless route."""
    global _WIFI_RECOVERY_LAST_CHECK_MONOTONIC
    if settings.use_fake_data:
        return
    config = _load_automation_config(settings)["wifi_recovery"]
    if not config.get("enabled"):
        _WIFI_RECOVERY_LAST_CHECK_MONOTONIC = None
        return
    current_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    if (
        _WIFI_RECOVERY_LAST_CHECK_MONOTONIC is not None
        and current_monotonic - _WIFI_RECOVERY_LAST_CHECK_MONOTONIC < WIFI_RECOVERY_CHECK_SECONDS
    ):
        return
    _WIFI_RECOVERY_LAST_CHECK_MONOTONIC = current_monotonic
    health = _wifi_health(settings)
    _WIFI_RECOVERY_RUNTIME.update(health)
    _WIFI_RECOVERY_RUNTIME["last_check_epoch"] = time.time()
    _WIFI_RECOVERY_RUNTIME["last_error"] = None
    if health["wifi_enabled"] is True and health["wifi_connected"]:
        return
    try:
        _recover_wifi()
    except (OSError, subprocess.SubprocessError) as error:
        _WIFI_RECOVERY_RUNTIME["last_error"] = str(error)
        print(f"Wi-Fi recovery automation failed: {error}", file=sys.stderr, flush=True)
        return
    _WIFI_RECOVERY_RUNTIME["last_recovery_epoch"] = time.time()
    print(
        "Wi-Fi recovery automation reset the wireless connection",
        file=sys.stdout,
        flush=True,
    )


def _run_idle_volume_automation_once(settings: Settings) -> None:
    """Set the volume to the configured target once the device has been idle past
    the threshold. Applies regardless of direction -- raises the volume just as
    readily as it lowers it -- since the target is whatever the user configured,
    not necessarily a quieter level than the current one."""
    global _IDLE_VOLUME_LAST_ARMED_ACTIVITY
    if settings.use_fake_data:
        return
    config = _load_automation_config(settings)["idle_volume"]
    if not config.get("enabled"):
        _IDLE_VOLUME_LAST_ARMED_ACTIVITY = None
        return
    try:
        if _find_running_emulatorlauncher():
            _IDLE_VOLUME_LAST_ARMED_ACTIVITY = None
            return
    except Exception:
        pass
    last_activity = _read_last_input_activity()
    if last_activity is None:
        # No monitor data yet; never touch volume on a machine we cannot confirm is idle.
        return
    # Any input since we last applied re-arms the automation for the next idle period.
    if _IDLE_VOLUME_LAST_ARMED_ACTIVITY is not None and last_activity != _IDLE_VOLUME_LAST_ARMED_ACTIVITY:
        _IDLE_VOLUME_LAST_ARMED_ACTIVITY = None
    if _IDLE_VOLUME_LAST_ARMED_ACTIVITY is not None:
        return  # already applied for this idle period
    idle_seconds = time.time() - last_activity
    if idle_seconds < config["idle_minutes"] * 60:
        return
    target = config["target_volume"]
    current = _get_audio_volume(settings)
    if current is not None and current == target:
        # Already at target; mark armed so we don't re-check every tick.
        _IDLE_VOLUME_LAST_ARMED_ACTIVITY = last_activity
        return
    try:
        applied = _apply_audio_volume(settings, target)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"Idle-volume automation could not set volume: {error}", file=sys.stderr, flush=True)
        return
    _IDLE_VOLUME_LAST_ARMED_ACTIVITY = last_activity
    print(
        f"Idle-volume automation set volume to {applied}% after {int(idle_seconds)}s idle",
        file=sys.stdout,
        flush=True,
    )


def _run_idle_game_exit_automation_once(settings: Settings) -> None:
    """Exit the running game once it has been idle past the configured threshold.

    Unlike idle-volume (which backs off while a game is active), this automation only
    fires while a game *is* running — it is the mechanism that ends that idle period,
    via ``batocera-es-swissknife --emukill``.
    """
    global _IDLE_GAME_EXIT_LAST_ARMED_ACTIVITY
    if settings.use_fake_data:
        return
    config = _load_automation_config(settings)["idle_game_exit"]
    if not config.get("enabled"):
        _IDLE_GAME_EXIT_LAST_ARMED_ACTIVITY = None
        return
    try:
        running = _find_running_emulatorlauncher()
    except Exception:
        running = None
    if not running:
        # No game running; re-arm so the next idle period (in a future game) is fresh.
        _IDLE_GAME_EXIT_LAST_ARMED_ACTIVITY = None
        return
    last_activity = _read_last_input_activity()
    if last_activity is None:
        # No monitor data yet; never exit a game we cannot confirm is idle.
        return
    # Any input since we last exited re-arms the automation for the next idle period.
    if _IDLE_GAME_EXIT_LAST_ARMED_ACTIVITY is not None and last_activity != _IDLE_GAME_EXIT_LAST_ARMED_ACTIVITY:
        _IDLE_GAME_EXIT_LAST_ARMED_ACTIVITY = None
    if _IDLE_GAME_EXIT_LAST_ARMED_ACTIVITY is not None:
        return  # already exited for this idle period
    idle_seconds = time.time() - last_activity
    if idle_seconds < config["idle_minutes"] * 60:
        return
    try:
        killed = _kill_running_emulator()
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Idle-game-exit automation could not exit the game: {error}", file=sys.stderr, flush=True)
        return
    if not killed:
        print(
            "Idle-game-exit automation could not exit the game: no supported emulator exit command is available",
            file=sys.stderr,
            flush=True,
        )
        return
    _IDLE_GAME_EXIT_LAST_ARMED_ACTIVITY = last_activity
    print(
        f"Idle-game-exit automation exited the running game after {int(idle_seconds)}s idle",
        file=sys.stdout,
        flush=True,
    )


def _start_automation_poller(settings: Settings) -> None:
    poll_seconds = max(5, int(os.environ.get("AUTOMATION_POLL_SECONDS", str(AUTOMATION_POLL_SECONDS))))

    def loop() -> None:
        while True:
            try:
                _run_idle_volume_automation_once(settings)
            except Exception as error:
                print(f"Automation poll failed: {error}", file=sys.stderr, flush=True)
            try:
                _run_idle_game_exit_automation_once(settings)
            except Exception as error:
                print(f"Automation poll failed: {error}", file=sys.stderr, flush=True)
            try:
                _run_wifi_recovery_automation_once(settings)
            except Exception as error:
                print(f"Automation poll failed: {error}", file=sys.stderr, flush=True)
            time.sleep(poll_seconds)

    thread = Thread(target=loop, name="drone-automation-poller", daemon=True)
    thread.start()
    print(f"Automation poller thread started: poll_seconds={poll_seconds}", file=sys.stdout, flush=True)


def _push_automation_config_to_overmind(settings: Settings) -> bool:
    # _collect_system_info_payload aggregates status from drone_api; lazy-import to avoid a cycle.
    try:
        from .system_info import _collect_system_info_payload
    except ImportError:  # pragma: no cover - flat execution
        from device.system_info import _collect_system_info_payload  # type: ignore
    """Best-effort immediate push of the automation config
    to Overmind.

    Heartbeats only rebuild the full system_info hourly, so a local change made on
    the Drone would otherwise take up to an hour to appear in Overmind. This sends a
    heartbeat now carrying a full system_info snapshot (which includes the automation
    config). A full snapshot — rather than a partial one — avoids clobbering other
    system_info columns on Overmind's full-state mirror path. Returns True only on a
    successful post. Never raises.
    """
    if settings.use_fake_data or not _local_network.is_overmind_mode(settings):
        return False
    try:
        config = _load_overmind_config_for_settings(settings)
        base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
        token = str(config.get("overmind_token") or "").strip() or str(config.get("overmind_auth_token") or "").strip()
        if not base_url or not token:
            return False
        device_id = quote(settings.overmind_device_id, safe="")
        url = f"{base_url}/api/devices/{device_id}/heartbeat"
        payload = {
            "device_id": settings.overmind_device_id,
            "device_name": str(config.get("drone_name") or "").strip() or socket.gethostname(),
            "system_info": _collect_system_info_payload(settings),
        }
        _overmind_post_json(url, payload, token=token, settings=settings)
        _overmind_log(f"Automation config pushed to Overmind for {settings.overmind_device_id}")
        return True
    except Exception as error:
        _overmind_log(f"Automation Overmind push failed; heartbeat will reconcile: {_format_overmind_error(error)}")
        return False


# Device-control helpers (ES restart / screen mode / es_systems / theme group 2)
# moved to device_control.py (re-exported above).
