import base64
import hmac
import html
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, RLock, Thread
from threading import Event
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlparse

DRONE_LATEST_ARCHIVE_URL = "https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/drone-app.tar.gz"
DRONE_SELF_UPDATE_EXIT_CODE = 75
DRONE_REMOTE_REBOOT_EXIT_CODE = 76
APP_DIR = Path(__file__).resolve().parent

try:
    from .app_version import drone_app_version as _drone_app_version
    from .api_routes import ApiRoutesMixin
    from .set_screen_mode import set_screen_mode as _set_screen_mode_helper
    from .set_volume import set_audio_volume as _set_audio_volume_helper
    from .network_identity import (
        drone_network_payload as _build_drone_network_payload,
        drone_reachable_url as _build_drone_reachable_url,
        drone_report_host as _build_drone_report_host,
        drone_scheme as _drone_scheme,
        get_local_certificate_ips as _build_local_certificate_ips,
        get_local_ip_addresses as _build_local_ip_addresses,
        get_router_ip_address as _build_router_ip_address,
        hostname_override_values as _hostname_override_values,
        is_ip_literal as _is_ip_literal,
    )
    from . import local_network as _local_network
    from .overmind_filesystem import (
        filesystem_events as _build_filesystem_events,
        filesystem_snapshot as _filesystem_snapshot,
    )
    from .overmind_game_logs import commit_game_log_cursors as _commit_game_log_cursors
    from .overmind_game_logs import collect_game_logs as _build_game_log_payload
    from .overmind_game_logs import collect_game_event_sessions as _collect_game_event_sessions
    from .overmind_game_logs import delete_game_event_spool as _delete_game_event_spool
    from .overmind_game_logs import GameProcessMonitor
    from .overmind_game_logs import load_gameplay_history as _load_gameplay_history
    from .overmind_game_logs import pending_game_event_count as _pending_game_event_count
    from .overmind_reporting import (
        collect_emulator_configs as _collect_emulator_configs,
        collect_log_sources as _collect_log_sources,
        commit_emulator_config_fingerprints as _commit_emulator_config_fingerprints,
        commit_log_cursors as _commit_log_cursors,
        list_emulator_config_files as _list_emulator_config_files,
        read_emulator_config_file as _read_emulator_config_file,
    )
    from .peer_selection import select_best_peer as _select_best_peer
    from .route_config import API_PREFIX, api_url
    from .rom_metadata_store import (
        ROM_METADATA_CACHE_VERSION,
        ArtworkCacheRow,
        search_rom_entries,
        rom_cache_has_entries,
        rom_cache_ready,
        list_rom_rows_by_system,
        _empty_rom_metadata_cache,
        _clear_pending_rom_metadata_changes,
        _clear_sqlite_asset_metadata_cache,
        _purge_asset_cache_keep_fingerprint,
        _read_preserved_asset_fingerprint,
        _load_rom_metadata_cache,
        _persist_rom_metadata_cache,
        _read_pending_rom_metadata_changes,
        _read_rom_metadata_cache_state,
        _read_sqlite_asset_systems,
        _rom_metadata_cache_path,
        _update_rom_metadata_cache_state,
    )
    from .rom_fs_watcher import RomFilesystemWatcher
    from . import saves_store as _saves_store
    from .state_store import (
        append_event as _append_state_event,
        database_path as _state_database_path,
        database_path_for_legacy_file as _state_database_path_for_legacy_file,
        load_events as _load_state_events,
        load_payload as _load_state_payload,
        save_payload as _save_state_payload,
    )
    from .transfer_files import (
        bios_md5_exists as _bios_md5_exists,
        collision_safe_target as _collision_safe_target,
        rom_exists as _rom_exists,
        rom_fingerprint_exists as _rom_fingerprint_exists,
        safe_rom_relative_path as _safe_rom_relative_path,
    )
    from .ui_routes import UiRoutesMixin
except ImportError:
    if __package__ not in (None, ""):
        raise
    from app_version import drone_app_version as _drone_app_version  # type: ignore
    from api_routes import ApiRoutesMixin  # type: ignore
    from set_screen_mode import set_screen_mode as _set_screen_mode_helper  # type: ignore
    from set_volume import set_audio_volume as _set_audio_volume_helper  # type: ignore
    from network_identity import (  # type: ignore
        drone_network_payload as _build_drone_network_payload,
        drone_reachable_url as _build_drone_reachable_url,
        drone_report_host as _build_drone_report_host,
        drone_scheme as _drone_scheme,
        get_local_certificate_ips as _build_local_certificate_ips,
        get_local_ip_addresses as _build_local_ip_addresses,
        get_router_ip_address as _build_router_ip_address,
        hostname_override_values as _hostname_override_values,
        is_ip_literal as _is_ip_literal,
    )
    import local_network as _local_network  # type: ignore
    from overmind_filesystem import (  # type: ignore
        filesystem_events as _build_filesystem_events,
        filesystem_snapshot as _filesystem_snapshot,
    )
    from overmind_game_logs import commit_game_log_cursors as _commit_game_log_cursors  # type: ignore
    from overmind_game_logs import collect_game_logs as _build_game_log_payload  # type: ignore
    from overmind_game_logs import collect_game_event_sessions as _collect_game_event_sessions  # type: ignore
    from overmind_game_logs import delete_game_event_spool as _delete_game_event_spool  # type: ignore
    from overmind_game_logs import GameProcessMonitor  # type: ignore
    from overmind_game_logs import load_gameplay_history as _load_gameplay_history  # type: ignore
    from overmind_game_logs import pending_game_event_count as _pending_game_event_count  # type: ignore
    from overmind_reporting import (  # type: ignore
        collect_emulator_configs as _collect_emulator_configs,
        collect_log_sources as _collect_log_sources,
        commit_emulator_config_fingerprints as _commit_emulator_config_fingerprints,
        commit_log_cursors as _commit_log_cursors,
        list_emulator_config_files as _list_emulator_config_files,
        read_emulator_config_file as _read_emulator_config_file,
    )
    from peer_selection import select_best_peer as _select_best_peer  # type: ignore
    from route_config import API_PREFIX, api_url  # type: ignore
    from rom_metadata_store import (  # type: ignore
        ROM_METADATA_CACHE_VERSION,
        ArtworkCacheRow,
        search_rom_entries,
        rom_cache_has_entries,
        rom_cache_ready,
        list_rom_rows_by_system,
        _empty_rom_metadata_cache,
        _clear_pending_rom_metadata_changes,
        _clear_sqlite_asset_metadata_cache,
        _purge_asset_cache_keep_fingerprint,
        _read_preserved_asset_fingerprint,
        _load_rom_metadata_cache,
        _persist_rom_metadata_cache,
        _read_pending_rom_metadata_changes,
        _read_rom_metadata_cache_state,
        _read_sqlite_asset_systems,
        _rom_metadata_cache_path,
        _update_rom_metadata_cache_state,
    )
    from rom_fs_watcher import RomFilesystemWatcher  # type: ignore
    import saves_store as _saves_store  # type: ignore
    from state_store import (  # type: ignore
        append_event as _append_state_event,
        database_path as _state_database_path,
        database_path_for_legacy_file as _state_database_path_for_legacy_file,
        load_events as _load_state_events,
        load_payload as _load_state_payload,
        save_payload as _save_state_payload,
    )
    from transfer_files import (  # type: ignore
        bios_md5_exists as _bios_md5_exists,
        collision_safe_target as _collision_safe_target,
        rom_exists as _rom_exists,
        rom_fingerprint_exists as _rom_fingerprint_exists,
        safe_rom_relative_path as _safe_rom_relative_path,
    )
    from ui_routes import UiRoutesMixin  # type: ignore


FAKE_OVERMIND_EMAIL = "demo@example.com"
FAKE_OVERMIND_PASSWORD = "DemoPass123"
FAKE_OVERMIND_TOKEN = "demo-local-drone-token"
_OVERMIND_POLLER_STARTED = False
_ROM_METADATA_POLLER_STARTED = False
_ROM_METADATA_WATCHER_STARTED = False
_ROM_METADATA_WATCHER = None
_SAVES_METADATA_WATCHER = None
# File-only rotating stream for Overmind-related logs; configured in _configure_rotating_logs.
_OVERMIND_LOG_STREAM = None
_PEER_HEALTH_CHECK_THREAD_STARTED = False
_LOCAL_NETWORK_WORKERS_STARTED = False
_GAME_PROCESS_MONITOR_STARTED = False
_GAME_PROCESS_MONITOR = None
_ROM_METADATA_ACTIVE = Event()
_ROM_METADATA_WAKE = Event()
# Set when an Overmind heartbeat reports asset thumbprints that differ from what the
# Drone holds locally, so the next metadata poll pushes a full inventory to resync.
_ASSET_PUSH_REQUESTED = Event()
# Same idea for game saves, tracked independently so a saves drift does not force a
# (much larger) ROM/BIOS inventory push and vice versa.
_SAVES_PUSH_REQUESTED = Event()
_ROM_METADATA_LOCK = RLock()
_DOWNLOAD_MANAGER = None
_PERFORMANCE_METRICS_LAST_SAMPLE: Optional[dict] = None
LAUNCHBOX_API_BASE = "https://api.gamesdb.launchbox-app.com/api"
LAUNCHBOX_IMAGE_BASE = "https://images.launchbox-app.com"
SCRAPER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ARTWORK_FIELDS = ("image", "thumbnail", "marquee", "fanart", "boxart", "video", "wheel", "manual")

# Serializes gamelist.xml read-modify-write so concurrent artwork downloads (the
# download pool runs several at once) can't clobber each other's <image>/<video>
# entries on the same system. Writes are sub-millisecond, so a single global lock
# costs nothing relative to the transfers themselves.
_GAMELIST_WRITE_LOCK = Lock()
ARTWORK_DUPLICATE_FILTER = "duplicate_artwork"
OVERMIND_EVENT_TYPES = {
    "gameplay": "gameplay_activity",
    "rom_update": "rom_update",
    "filesystem": "filesystem_event",
    "speed": "speed_sample",
    "peer": "peer_health",
}
DOWNLOAD_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "skipped"}
PERSISTENT_OVERMIND_LOG_SOURCES = ("drone_stderr", "es_launch_stdout", "es_launch_stderr")
DOWNLOAD_PROGRESS_PUSH_SECONDS = float(os.environ.get("DOWNLOAD_PROGRESS_PUSH_SECONDS", "5"))
PEER_CHECK_TIMEOUT_SECONDS = float(os.environ.get("DRONE_PEER_CHECK_TIMEOUT_SECONDS", "3"))
PEER_CHECK_INTERVAL_SECONDS = int(os.environ.get("DRONE_PEER_CHECK_INTERVAL_SECONDS", "300"))
OVERMIND_SPEED_SAMPLE_SECONDS = int(os.environ.get("OVERMIND_SPEED_SAMPLE_SECONDS", "600"))
SPEED_TEST_DEFAULT_BASE_URL = "https://speed.cloudflare.com"
OVERMIND_HEARTBEAT_SECONDS = int(os.environ.get("OVERMIND_POLL_SECONDS", "30"))
OVERMIND_HEARTBEAT_TIMEOUT_SECONDS = max(10, int(os.environ.get("OVERMIND_HEARTBEAT_TIMEOUT_SECONDS", "20")))
OVERMIND_CONFIG_REPORT_SECONDS = int(os.environ.get("OVERMIND_CONFIG_REPORT_SECONDS", "300"))
ROM_METADATA_POLL_SECONDS = int(os.environ.get("ROM_METADATA_POLL_SECONDS", "300"))
ROM_METADATA_INITIAL_DELAY_SECONDS = int(os.environ.get("ROM_METADATA_INITIAL_DELAY_SECONDS", "60"))
ROM_METADATA_PROGRESS_SECONDS = float(os.environ.get("ROM_METADATA_PROGRESS_SECONDS", "30"))
ROM_METADATA_PROGRESS_FILES = int(os.environ.get("ROM_METADATA_PROGRESS_FILES", "250"))
ROM_METADATA_FINGERPRINT_BATCH_SIZE = max(1, int(os.environ.get("ROM_METADATA_FINGERPRINT_BATCH_SIZE", "250")))
ROM_METADATA_UPLOAD_CHUNK_SIZE = max(1, int(os.environ.get("ROM_METADATA_UPLOAD_CHUNK_SIZE", "250")))
# Cross-drone file fingerprint (sample-fp-v1). A sampled hash (size + fixed
# head/middle/tail windows) replaces full-file fingerprint so disc images are not read end
# to end. Constant cost per file; see RomRepository.build_fingerprint.
FINGERPRINT_ALGORITHM = "sample-fp-v1"
FINGERPRINT_SAMPLE_BYTES = max(4096, int(os.environ.get("ROM_METADATA_FINGERPRINT_SAMPLE_BYTES", str(64 * 1024))))
# Files at or below this size are fingerprinted whole (exact); larger files use the
# three sample windows. Keep >= 3x the sample size so the windows never overlap.
FINGERPRINT_SMALL_FILE_BYTES = max(3 * FINGERPRINT_SAMPLE_BYTES, int(os.environ.get("ROM_METADATA_FINGERPRINT_SMALL_FILE_BYTES", str(3 * FINGERPRINT_SAMPLE_BYTES))))
# Wall-clock budget for fingerprinting within a single poll. Fingerprinting is cheap
# (constant I/O per file) and resumable, so this is a safety guard that rarely trips.
ROM_METADATA_HASH_BUDGET_SECONDS = max(0.0, float(os.environ.get("ROM_METADATA_HASH_BUDGET_SECONDS", "120")))
ROM_METADATA_HASH_ROMS_ENABLED = os.environ.get("ROM_METADATA_HASH_ROMS_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
# Real-time inotify watcher that wakes the metadata poller when ROM files change.
ROM_METADATA_WATCH_ENABLED = os.environ.get("ROM_METADATA_WATCH_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
# Coalesce a burst of filesystem events: wait for this much quiet before waking
# the poller, but never delay longer than the max even during a long bulk copy.
ROM_METADATA_WATCH_DEBOUNCE_SECONDS = max(0.5, float(os.environ.get("ROM_METADATA_WATCH_DEBOUNCE_SECONDS", "10")))
ROM_METADATA_WATCH_MAX_DELAY_SECONDS = max(
    ROM_METADATA_WATCH_DEBOUNCE_SECONDS,
    float(os.environ.get("ROM_METADATA_WATCH_MAX_DELAY_SECONDS", "60")),
)
DRONE_LOG_UNAUTHORIZED_REQUESTS = os.environ.get("DRONE_LOG_UNAUTHORIZED_REQUESTS", "0").strip().lower() in {"1", "true", "yes", "on"}
DRONE_UNAUTH_RATE_LIMIT_ENABLED = os.environ.get("DRONE_UNAUTH_RATE_LIMIT_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
DRONE_UNAUTH_RATE_LIMIT_REQUESTS = max(1, int(os.environ.get("DRONE_UNAUTH_RATE_LIMIT_REQUESTS", "60")))
DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS = max(1.0, float(os.environ.get("DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS", "60")))

# Brute-force protection: temporarily block an IP that produces too many 401
# (unauthorized) responses in a short window. Defaults: 5 failures / 60s -> 5 min block.
DRONE_AUTH_BLOCK_ENABLED = os.environ.get("DRONE_AUTH_BLOCK_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
DRONE_AUTH_BLOCK_THRESHOLD = max(1, int(os.environ.get("DRONE_AUTH_BLOCK_THRESHOLD", "5")))
DRONE_AUTH_BLOCK_WINDOW_SECONDS = max(1.0, float(os.environ.get("DRONE_AUTH_BLOCK_WINDOW_SECONDS", "60")))
DRONE_AUTH_BLOCK_DURATION_SECONDS = max(1.0, float(os.environ.get("DRONE_AUTH_BLOCK_DURATION_SECONDS", "300")))
OVERMIND_UPLOAD_TIMEOUT_SECONDS = max(10, int(os.environ.get("OVERMIND_UPLOAD_TIMEOUT_SECONDS", "60")))
LAUNCHBOX_PLATFORM_ALIASES = {
    "3do": "3DO Interactive Multiplayer",
    "adam": "Coleco ADAM",
    "amiga": "Commodore Amiga",
    "amigacd32": "Commodore Amiga CD32",
    "amstradcpc": "Amstrad CPC",
    "apple2": "Apple II",
    "arcade": "Arcade",
    "atari2600": "Atari 2600",
    "atari5200": "Atari 5200",
    "atari7800": "Atari 7800",
    "atari800": "Atari 8-bit",
    "atarijaguar": "Atari Jaguar",
    "atarijaguarcd": "Atari Jaguar CD",
    "atarilynx": "Atari Lynx",
    "atarist": "Atari ST",
    "atomiswave": "Sammy Atomiswave",
    "c128": "Commodore 128",
    "c20": "Commodore VIC-20",
    "c64": "Commodore 64",
    "cavestory": "Cave Story",
    "cdimono1": "Philips CD-i",
    "chailove": "ChaiLove",
    "channel_f": "Fairchild Channel F",
    "colecovision": "ColecoVision",
    "cps1": "Capcom CPS-1",
    "cps2": "Capcom CPS-2",
    "cps3": "Capcom CPS-3",
    "daphne": "Daphne",
    "dos": "MS-DOS",
    "nes": "Nintendo Entertainment System",
    "snes": "Super Nintendo Entertainment System",
    "n64": "Nintendo 64",
    "gba": "Nintendo Game Boy Advance",
    "gb": "Nintendo Game Boy",
    "gbc": "Nintendo Game Boy Color",
    "nds": "Nintendo DS",
    "3ds": "Nintendo 3DS",
    "gamecube": "Nintendo GameCube",
    "wii": "Nintendo Wii",
    "wiiu": "Nintendo Wii U",
    "switch": "Nintendo Switch",
    "famicom": "Nintendo Famicom",
    "fds": "Nintendo Famicom Disk System",
    "genesis": "Sega Genesis",
    "megadrive": "Sega Genesis",
    "megadrive-japan": "Sega Mega Drive",
    "sega32x": "Sega 32X",
    "segacd": "Sega CD",
    "mastersystem": "Sega Master System",
    "sg1000": "Sega SG-1000",
    "gamegear": "Sega Game Gear",
    "dreamcast": "Sega Dreamcast",
    "saturn": "Sega Saturn",
    "psx": "Sony Playstation",
    "ps1": "Sony Playstation",
    "ps2": "Sony Playstation 2",
    "ps3": "Sony Playstation 3",
    "ps4": "Sony Playstation 4",
    "ps5": "Sony Playstation 5",
    "psp": "Sony PSP",
    "psvita": "Sony Playstation Vita",
    "mame": "Arcade",
    "fbneo": "Arcade",
    "neogeo": "SNK Neo Geo MVS",
    "neogeocd": "SNK Neo Geo CD",
    "ngp": "SNK Neo Geo Pocket",
    "ngpc": "SNK Neo Geo Pocket Color",
    "odyssey2": "Magnavox Odyssey 2",
    "openbor": "OpenBOR",
    "pc": "Windows",
    "pcengine": "NEC TurboGrafx-16",
    "pcenginecd": "NEC TurboGrafx-CD",
    "pcfx": "NEC PC-FX",
    "ports": "Ports",
    "satellaview": "Nintendo Satellaview",
    "scummvm": "ScummVM",
    "steam": "Windows",
    "supergrafx": "NEC SuperGrafx",
    "tic80": "TIC-80",
    "triforce": "Namco Sega Nintendo Triforce",
    "vectrex": "GCE Vectrex",
    "virtualboy": "Nintendo Virtual Boy",
    "windows": "Windows",
    "wswan": "Bandai WonderSwan",
    "wswanc": "Bandai WonderSwan Color",
    "xbox": "Microsoft Xbox",
    "xbox360": "Microsoft Xbox 360",
    "xboxone": "Microsoft Xbox One",
    "zxspectrum": "Sinclair ZX Spectrum",
}
LAUNCHBOX_FIELD_TYPES = {
    "image": ("Screenshot - Gameplay", "Screenshot - Game Title"),
    "thumbnail": ("Screenshot - Game Title", "Screenshot - Gameplay", "Box - Front"),
    "marquee": ("Clear Logo",),
    "fanart": ("Fanart - Background",),
    "boxart": ("Box - Front",),
}


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable must be set")
    return value


def _require_any_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    joined = " or ".join(names)
    raise RuntimeError(f"{joined} environment variable must be set")


def _env_bool(default: bool, *names: str) -> bool:
    for name in names:
        value = os.environ.get(name)
        if value is None:
            continue
        return value.strip().lower() not in ("0", "false", "no", "off")
    return default


def _parse_port_list(value: Optional[str]) -> Tuple[int, ...]:
    ports = []
    for raw in re.split(r"[,;\s]+", str(value or "")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            port = int(raw)
        except ValueError:
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return tuple(ports)


_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{3,128}$")
_VIRTUAL_INTERFACE_PREFIXES = (
    "br-",
    "docker",
    "dummy",
    "ip6tnl",
    "sit",
    "tap",
    "tun",
    "veth",
    "virbr",
    "wg",
    "zt",
)
_VIRTUAL_INTERFACE_NAMES = {"lo", "bonding_masters"}
_PHYSICAL_INTERFACE_PRIORITIES = (
    "eth",
    "en",
    "wlan",
    "wl",
)


def _normalize_device_id(value: Optional[str]) -> Optional[str]:
    normalized = (value or "").strip()
    if not normalized or not _DEVICE_ID_PATTERN.match(normalized):
        return None
    return normalized


def _device_id_path(userdata_root: Path) -> Path:
    return Path(os.environ.get("DRONE_DEVICE_ID_FILE", str(userdata_root / "system" / "drone-app" / "device-id")))


def _read_persisted_machine_id(userdata_root: Path) -> Optional[str]:
    try:
        return _normalize_device_id(_device_id_path(userdata_root).read_text(encoding="utf-8"))
    except OSError:
        return None


def _write_persisted_machine_id(userdata_root: Path, value: str) -> None:
    normalized = _normalize_device_id(value)
    if not normalized:
        return
    try:
        path = _device_id_path(userdata_root)
        if not path.parent.exists():
            return
        path.write_text(f"{normalized}\n", encoding="utf-8")
    except OSError:
        return


def _interface_priority(name: str, has_device: bool, mac: str) -> Tuple[int, int, str]:
    prefix_index = next((index for index, prefix in enumerate(_PHYSICAL_INTERFACE_PRIORITIES) if name.startswith(prefix)), len(_PHYSICAL_INTERFACE_PRIORITIES))
    first_octet = int(mac.split(":", 1)[0], 16)
    locally_administered = 1 if first_octet & 0x02 else 0
    return (0 if has_device else 1, prefix_index + locally_administered, name)


def _physical_mac_candidates(sys_class_net: Path = Path("/sys/class/net")) -> List[str]:
    candidates: List[Tuple[Tuple[int, int, str], str]] = []
    try:
        interfaces = list(sys_class_net.iterdir())
    except OSError:
        return []
    for interface in interfaces:
        name = interface.name
        if name in _VIRTUAL_INTERFACE_NAMES or name.startswith(_VIRTUAL_INTERFACE_PREFIXES):
            continue
        try:
            mac = (interface / "address").read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if not re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", mac) or mac == "00:00:00:00:00:00":
            continue
        candidates.append((_interface_priority(name, (interface / "device").exists(), mac), mac))
    candidates.sort(key=lambda row: row[0])
    return [mac for _, mac in candidates]


def _runtime_machine_id() -> str:
    node = uuid.getnode()
    return ":".join(f"{(node >> shift) & 0xff:02x}" for shift in range(40, -1, -8))


def _machine_id(userdata_root: Optional[Path] = None) -> str:
    if userdata_root is None:
        return _runtime_machine_id()
    persisted = _read_persisted_machine_id(userdata_root)
    if persisted:
        return persisted
    generated = next(iter(_physical_mac_candidates()), None) or _runtime_machine_id()
    _write_persisted_machine_id(userdata_root, generated)
    return generated


def _fake_machine_id() -> str:
    return _machine_id()


def _clean_rom_title(value: str) -> str:
    name = Path(value or "").stem
    name = re.sub(r"[:,\-;\[\]\(\)<>_]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or Path(value or "").stem or value


def _normalize_platform_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _launchbox_platform_for_system(system: Optional[str]) -> Optional[str]:
    key = _normalize_platform_key(system or "")
    if not key:
        return None
    return LAUNCHBOX_PLATFORM_ALIASES.get(key)


def _text_or_empty(parent: ET.Element, tag: str) -> str:
    child = parent.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _gamelist_details(game: Optional[ET.Element]) -> dict:
    if game is None:
        return {}
    details = {}
    for child in list(game):
        tag = child.tag
        value = (child.text or "").strip()
        if child.attrib:
            value = {"text": value, "attributes": dict(child.attrib)}
        if tag in details:
            existing = details[tag]
            if isinstance(existing, list):
                existing.append(value)
            else:
                details[tag] = [existing, value]
        else:
            details[tag] = value
    return details


def _gamelist_game_id(game: Optional[ET.Element], relative_path: str) -> str:
    if game is None:
        return relative_path
    return str(game.get("id") or _text_or_empty(game, "id") or relative_path).strip() or relative_path


def _find_gamelist_entry_by_game_id(root: ET.Element, game_id: str) -> Optional[ET.Element]:
    wanted = str(game_id or "").strip()
    if not wanted:
        return None
    wanted_path = _normalize_gamelist_rom_path(wanted).lower()
    for game in root.findall("game"):
        candidates = [
            str(game.get("id") or "").strip(),
            _text_or_empty(game, "id"),
            _normalize_gamelist_rom_path(_text_or_empty(game, "path")),
        ]
        if any(candidate and candidate == wanted for candidate in candidates):
            return game
        if wanted_path and any(_normalize_gamelist_rom_path(candidate).lower() == wanted_path for candidate in candidates):
            return game
    return None


def _gamelist_metadata_for_reference(gamelist_path: str, game_id: str) -> dict:
    path = Path(str(gamelist_path or ""))
    if not path.exists() or not path.is_file():
        return {}
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return {}
    if root.tag != "gameList":
        return {}
    game = _find_gamelist_entry_by_game_id(root, game_id)
    return _gamelist_details(game)


def _database_rom_metadata_fields(rom: dict, system_name: str, file_path: str, absolute: Path, stat_size: int, stat_mtime: int) -> dict:
    display_name = Path(file_path).stem
    return {
        **{k: v for k, v in rom.items() if k not in {"fingerprint", "rom_fingerprint", "gamelist", "existing", "name", "title", "rom_name"}},
        "system": system_name,
        "system_name": system_name,
        "rom_name": display_name,
        "name": display_name,
        "title": display_name,
        "rom_file": Path(file_path).name,
        "filename": Path(file_path).name,
        "file_path": file_path,
        "relative_path": file_path,
        "rom_path": file_path,
        "file_size": stat_size,
        "byte_count": stat_size,
        "size": stat_size,
        "modified_time": stat_mtime,
        "mtime": stat_mtime,
        "absolute_path": str(absolute),
        "source": "gamelist.xml" if rom.get("gamelist_path") else "filesystem",
        "metadata_source": "gamelist.xml" if rom.get("gamelist_path") else "filesystem",
        "has_gamelist_entry": bool(rom.get("gamelist_path")),
        "image_stem": display_name,
    }


def _set_child_text(parent: ET.Element, tag: str, value: str) -> None:
    child = parent.find(tag)
    if child is None:
        child = ET.SubElement(parent, tag)
    child.text = value


def _first_metadata_value(*values) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            nested = _first_metadata_value(
                value.get("name"),
                value.get("title"),
                value.get("value"),
                value.get("displayName"),
            )
            if nested:
                return nested
            continue
        if isinstance(value, (list, tuple, set)):
            parts = [_first_metadata_value(item) for item in value]
            joined = ", ".join(part for part in parts if part)
            if joined:
                return joined
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _looks_like_placeholder_image(data: bytes) -> bool:
    """Catch common tiny/blank scraper placeholders before assigning them to ROM art."""
    if not data or len(data) < 128:
        return True
    sample = data[: min(len(data), 8192)]
    if sample and len(set(sample)) <= 3:
        return True
    digest = hashlib.sha256(data).hexdigest()
    known_bad = {
        # LaunchBox and CDN placeholders can shift, so keep this list small and
        # combine it with the tiny/flat image checks above.
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    }
    return digest in known_bad


def _artwork_identity(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/").lower()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.netloc}{parsed.path}".rstrip("/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw.lstrip("/")


def _remove_child(parent: ET.Element, tag: str) -> None:
    child = parent.find(tag)
    if child is not None:
        parent.remove(child)


def _relative_artwork_path(system_dir: Path, path: Path) -> str:
    try:
        return f"./{path.resolve().relative_to(system_dir.resolve()).as_posix()}"
    except Exception:
        return str(path)


def _normalize_gamelist_rom_path(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw.lstrip("/")


def _read_file_tail(path: Path, max_bytes: int) -> Tuple[bytes, bool]:
    safe_max = max(1, int(max_bytes))
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    fd = os.open(str(path), flags)
    try:
        stat_result = os.fstat(fd)
        size = int(stat_result.st_size)
        start = max(0, size - safe_max)
        if start:
            os.lseek(fd, start, os.SEEK_SET)
        chunks = []
        remaining = min(size, safe_max)
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), size > safe_max
    finally:
        os.close(fd)


def _tail_lines(path: Path, line_count: int, max_bytes: int = 1024 * 1024) -> List[str]:
    raw, truncated = _read_file_tail(path, max_bytes)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    output = lines[-max(1, int(line_count)) :]
    if truncated and output:
        output.insert(0, f"[truncated] showing last {max_bytes} bytes of file")
    return output


class LaunchBoxClient:
    def __init__(self, timeout_seconds: int = 15):
        self.timeout_seconds = timeout_seconds

    def _get_json(self, url: str) -> dict:
        request = Request(url, headers={"User-Agent": SCRAPER_USER_AGENT, "Accept": "application/json"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def search(self, query: str, system: Optional[str] = None, limit: int = 20) -> List[dict]:
        normalized_query = (query or "").strip()
        if not normalized_query:
            return []
        expected_platform = _launchbox_platform_for_system(system)

        def _search_payload(platform: Optional[str]) -> dict:
            url = f"{LAUNCHBOX_API_BASE}/search/{quote(normalized_query, safe='')}"
            if platform:
                url = f"{url}?platform={quote(platform, safe='')}"
            return self._get_json(url)

        payload = _search_payload(expected_platform)
        results = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(results, list):
            return []
        if expected_platform and not results:
            payload = _search_payload(None)
            results = payload.get("data") if isinstance(payload, dict) else []
            if not isinstance(results, list):
                return []

        output = []
        for item in results:
            if not isinstance(item, dict):
                continue
            platform = str(item.get("platformName") or "")
            score = 0
            if expected_platform and platform.lower() == expected_platform.lower():
                score -= 20
            name = str(item.get("name") or "")
            if name.lower() == normalized_query.lower():
                score -= 10
            thumb = str(item.get("thumbName") or "")
            output.append(
                {
                    "game_key": item.get("gameKey"),
                    "name": name,
                    "platform": platform,
                    "platform_filter": expected_platform,
                    "thumbnail_url": f"{LAUNCHBOX_IMAGE_BASE}/{quote(thumb, safe='')}" if thumb else None,
                    "details_url": f"https://gamesdb.launchbox-app.com/games/details/{item.get('gameKey')}",
                    "_score": score,
                }
            )
        output.sort(key=lambda item: (item["_score"], item["platform"].lower(), item["name"].lower()))
        for item in output:
            item.pop("_score", None)
        return output[: max(1, min(limit, 50))]

    def details(self, game_key: str) -> dict:
        safe_key = re.sub(r"[^0-9]", "", str(game_key or ""))
        if not safe_key:
            raise ValueError("game_key is required")
        payload = self._get_json(f"{LAUNCHBOX_API_BASE}/games/details/{safe_key}")
        images = []
        for item in payload.get("gameImages") or []:
            if not isinstance(item, dict):
                continue
            file_name = item.get("fullGameImageFileName") or item.get("imageFileName")
            if not file_name:
                continue
            images.append(
                {
                    "file_name": str(file_name),
                    "type": str(item.get("imageTypeName") or "").replace(" Thumb", ""),
                    "region": item.get("regionName"),
                    "width": item.get("fullGameImageWidth") or item.get("width"),
                    "height": item.get("fullGameImageHeight") or item.get("height"),
                    "url": f"{LAUNCHBOX_IMAGE_BASE}/{quote(str(file_name), safe='')}",
                }
            )
        return {
            "game_key": payload.get("gameKey"),
            "name": payload.get("name"),
            "platform": (payload.get("platform") or {}).get("name") if isinstance(payload.get("platform"), dict) else None,
            "release_date": payload.get("releaseDate"),
            "overview": payload.get("overview"),
            "genre": _first_metadata_value(payload.get("genres"), payload.get("genre"), payload.get("genreName")),
            "developer": _first_metadata_value(payload.get("developers"), payload.get("developer"), payload.get("developerName")),
            "publisher": _first_metadata_value(payload.get("publishers"), payload.get("publisher"), payload.get("publisherName")),
            "players": _first_metadata_value(payload.get("players"), payload.get("maxPlayers"), payload.get("numberOfPlayers")),
            "rating": _first_metadata_value(payload.get("communityStarRating"), payload.get("esrb"), payload.get("rating")),
            "images": images,
        }

    def choose_image_for_field(self, details: dict, field: str) -> Optional[dict]:
        wanted = LAUNCHBOX_FIELD_TYPES.get(field, ())
        images = details.get("images") or []
        for image_type in wanted:
            for image in images:
                candidate_url = str(image.get("url") or image.get("file_name") or "").lower()
                if any(marker in candidate_url for marker in ("placeholder", "no-image", "no_image", "default-image", "missing")):
                    continue
                if str(image.get("type") or "").lower() == image_type.lower():
                    return image
        return None

    def download_image(self, url: str) -> Tuple[bytes, str]:
        request = Request(url, headers={"User-Agent": SCRAPER_USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            content_type = response.headers.get_content_type()
            data = response.read(20 * 1024 * 1024 + 1)
            if len(data) > 20 * 1024 * 1024:
                raise ValueError("image is too large")
            if not str(content_type or "").startswith("image/"):
                raise ValueError("image_url did not return an image")
            if _looks_like_placeholder_image(data):
                raise ValueError("LaunchBox returned a placeholder image")
            return data, content_type


class TheGamesDBScraper:
    BASE_URL = "https://thegamesdb.net"
    CDN_HOST = "cdn.thegamesdb.net"

    def __init__(self, timeout_seconds: int = 15):
        self.timeout_seconds = timeout_seconds

    def _get_text(self, url: str) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": SCRAPER_USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return response.read().decode("utf-8", errors="replace")

    def _strip_tags(self, value: str) -> str:
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<.*?>", " ", value or ""))).strip()

    def search(self, title: str, system: str = "", limit: int = 10) -> List[dict]:
        normalized_title = _clean_rom_title(title or "")
        if not normalized_title:
            return []
        search_url = f"{self.BASE_URL}/search.php?name={quote(normalized_title, safe='')}"
        text = self._get_text(search_url)
        cards = []
        for match in re.finditer(r'<a\s+href="\./game\.php\?id=(\d+)">(.*?)(?=<div class="col-6 col-md-2">|</div>\s*</div>\s*</div>\s*</div>)', text, flags=re.DOTALL):
            game_id = match.group(1)
            card_html = match.group(2)
            title_match = re.search(r'<div class="card-footer.*?</div>', card_html, flags=re.DOTALL)
            footer = title_match.group(0) if title_match else card_html
            paragraphs = [
                re.sub(r"\s+", " ", html.unescape(re.sub(r"<.*?>", " ", item))).strip()
                for item in re.findall(r"<p[^>]*>(.*?)</p>", footer, flags=re.DOTALL)
            ]
            game_title = paragraphs[0] if paragraphs else title
            platform = paragraphs[-1] if paragraphs else ""
            score = 0
            expected_platform = _launchbox_platform_for_system(system) or system
            if expected_platform and platform and expected_platform.lower() in platform.lower():
                score -= 20
            if game_title.lower() == normalized_title.lower():
                score -= 10
            thumb_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', card_html, flags=re.DOTALL)
            thumbnail_url = html.unescape(thumb_match.group(1)) if thumb_match else ""
            if thumbnail_url.startswith("./"):
                thumbnail_url = f"{self.BASE_URL}/{thumbnail_url[2:]}"
            cards.append(
                {
                    "game_id": game_id,
                    "name": game_title,
                    "title": game_title,
                    "platform": platform,
                    "thumbnail_url": thumbnail_url,
                    "details_url": f"{self.BASE_URL}/game.php?id={game_id}",
                    "_score": score,
                }
            )
        cards.sort(key=lambda item: (item["_score"], item["title"].lower(), item["platform"].lower()))
        return [{key: value for key, value in item.items() if key != "_score"} for item in cards[: max(1, min(limit, 10))]]

    def details(self, game_id: str) -> dict:
        normalized_id = str(game_id or "").strip()
        if not normalized_id.isdigit():
            raise ValueError("game_id is required")
        text = self._get_text(f"{self.BASE_URL}/game.php?id={quote(normalized_id, safe='')}")
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", text, flags=re.DOTALL)
        overview_match = re.search(r'<p[^>]+class=["\'][^"\']*game-overview[^"\']*["\'][^>]*>(.*?)</p>', text, flags=re.DOTALL)
        metadata = {
            "game_id": normalized_id,
            "name": self._strip_tags(title_match.group(1)) if title_match else "",
            "overview": self._strip_tags(overview_match.group(1)) if overview_match else "",
        }
        meta_patterns = {
            "developer": r"Developers?\(s\):\s*(?:</strong>)?\s*(.*?)(?:</p>|<br\s*/?>)",
            "publisher": r"Publishers?\(s\):\s*(?:</strong>)?\s*(.*?)(?:</p>|<br\s*/?>)",
            "release_date": r"Release\s*Date:\s*(?:</strong>)?\s*(.*?)(?:</p>|<br\s*/?>)",
            "players": r"Players:\s*(?:</strong>)?\s*(.*?)(?:</p>|<br\s*/?>)",
            "rating": r"ESRB Rating:\s*(?:</strong>)?\s*(.*?)(?:</p>|<br\s*/?>)",
            "genre": r"Genre\(s\):\s*(?:</strong>)?\s*(.*?)(?:</p>|<br\s*/?>)",
        }
        for key, pattern in meta_patterns.items():
            match = re.search(pattern, text, flags=re.DOTALL | re.IGNORECASE)
            if match:
                metadata[key] = self._strip_tags(match.group(1))
        output = []
        seen = set()
        for image_url in re.findall(r'href=["\'](https://cdn\.thegamesdb\.net/images/original/[^"\']+)["\']', text):
            image_url = html.unescape(image_url)
            if image_url in seen:
                continue
            seen.add(image_url)
            image_type = "artwork"
            path = urlparse(image_url).path.lower()
            if "/boxart/front/" in path:
                image_type = "boxart front"
            elif "/boxart/back/" in path:
                image_type = "boxart back"
            elif "/fanart/" in path:
                image_type = "fanart"
            elif "/clearlogo/" in path:
                image_type = "clearlogo"
            elif "/graphical/" in path or "/banner/" in path or "/banners/" in path:
                image_type = "banner"
            elif "/screenshot/" in path or "/screenshots/" in path:
                image_type = "screenshot"
            elif "/titlescreen/" in path:
                image_type = "titlescreen"
            thumbnail_url = image_url.replace("/images/original/", "/images/thumb/")
            if any(part in path for part in ("/fanart/", "/screenshot/", "/screenshots/", "/titlescreen/", "/graphical/", "/banner/", "/banners/")):
                thumbnail_url = image_url.replace("/images/original/", "/images/cropped_center_thumb/")
            output.append(
                {
                    "url": image_url,
                    "image_url": image_url,
                    "thumbnail_url": thumbnail_url,
                    "type": image_type,
                    "file_name": Path(urlparse(image_url).path).name,
                }
            )
        metadata["images"] = output
        return metadata

    def choose_image_for_field(self, details: dict, field: str) -> Optional[dict]:
        images = details.get("images") if isinstance(details, dict) else []
        if not isinstance(images, list):
            return None
        preferred = {
            "boxart": ("boxart front",),
            "fanart": ("fanart",),
            "marquee": ("clearlogo", "banner"),
            "image": ("screenshot", "titlescreen", "fanart", "boxart front"),
            "thumbnail": ("screenshot", "titlescreen", "boxart front", "fanart"),
        }.get(field, ())
        for image_type in preferred:
            for image in images:
                if str(image.get("type") or "").lower() == image_type:
                    return image
        return None

    def download_image(self, url: str) -> Tuple[bytes, str]:
        parsed = urlparse(str(url or ""))
        if parsed.scheme != "https" or parsed.netloc != self.CDN_HOST:
            raise ValueError("image_url must be a TheGamesDB CDN URL")
        request = Request(
            url,
            headers={"User-Agent": SCRAPER_USER_AGENT},
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            content_type = response.headers.get_content_type()
            if not str(content_type or "").startswith("image/"):
                raise ValueError("image_url did not return an image")
            max_bytes = 20 * 1024 * 1024
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("image is too large")
            return data, content_type


MOBYGAMES_PLATFORM_ALIASES = {
    "3do": ("3DO",),
    "amiga": ("Amiga",),
    "amstradcpc": ("Amstrad CPC",),
    "arcade": ("Arcade",),
    "atari2600": ("Atari 2600",),
    "atari5200": ("Atari 5200",),
    "atari7800": ("Atari 7800",),
    "atarijaguar": ("Jaguar",),
    "atarilynx": ("Lynx",),
    "atarist": ("Atari ST",),
    "c64": ("Commodore 64",),
    "dos": ("DOS",),
    "dreamcast": ("Dreamcast",),
    "gamecube": ("GameCube",),
    "gb": ("Game Boy",),
    "gba": ("Game Boy Advance",),
    "gbc": ("Game Boy Color",),
    "genesis": ("Genesis",),
    "megadrive": ("Genesis", "SEGA Mega Drive"),
    "n64": ("Nintendo 64",),
    "nds": ("Nintendo DS",),
    "nes": ("NES", "Nintendo Entertainment System"),
    "ps1": ("PlayStation",),
    "ps2": ("PlayStation 2",),
    "ps3": ("PlayStation 3",),
    "ps4": ("PlayStation 4",),
    "psp": ("PSP",),
    "psvita": ("PS Vita",),
    "saturn": ("SEGA Saturn", "Saturn"),
    "segacd": ("SEGA CD",),
    "snes": ("SNES", "SNES (Super Famicom)", "Super Nintendo Entertainment System"),
    "switch": ("Nintendo Switch",),
    "wii": ("Wii",),
    "wiiu": ("Wii U",),
    "windows": ("Windows",),
    "xbox": ("Xbox",),
    "xbox360": ("Xbox 360",),
    "zxspectrum": ("ZX Spectrum",),
}


class MobyGamesClient:
    WEB_BASE = "https://www.mobygames.com"

    def __init__(self, timeout_seconds: int = 15):
        self.timeout_seconds = timeout_seconds

    def platform_name_for_system(self, system: Optional[str]) -> Optional[str]:
        aliases = MOBYGAMES_PLATFORM_ALIASES.get(_normalize_platform_key(system or ""), ())
        return aliases[0] if aliases else None

    def _get_text(self, url: str) -> Tuple[str, str]:
        request = Request(
            url,
            headers={
                "User-Agent": SCRAPER_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            final_url = response.geturl()
            text = response.read().decode("utf-8", errors="replace")
        if "Just a moment..." in text or "__cf_chl_" in text or "Enable JavaScript and cookies to continue" in text:
            raise ValueError("MobyGames blocked the request with a browser challenge.")
        return text, final_url

    def _strip_tags(self, value: str) -> str:
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<.*?>", " ", value or ""))).strip()

    def _absolute_url(self, value: str) -> str:
        raw = html.unescape(str(value or "")).strip()
        if raw.startswith("//"):
            return f"https:{raw}"
        if raw.startswith("/"):
            return f"{self.WEB_BASE}{raw}"
        return raw

    def _game_match_from_page(self, text: str, final_url: str) -> Optional[dict]:
        url_match = re.search(r"/game/(\d+)/([^/?#]+)", final_url)
        id_match = re.search(r"Moby ID:\s*(\d+)", text, flags=re.IGNORECASE)
        game_id = (url_match.group(1) if url_match else None) or (id_match.group(1) if id_match else None)
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", text, flags=re.DOTALL | re.IGNORECASE)
        if not game_id or not title_match:
            return None
        return {
            "game_id": game_id,
            "name": self._strip_tags(title_match.group(1)),
            "title": self._strip_tags(title_match.group(1)),
            "platform": "MobyGames page",
            "thumbnail_url": self._first_image_url(text),
            "details_url": f"{self.WEB_BASE}/game/{game_id}/{url_match.group(2) if url_match else ''}".rstrip("/"),
        }

    def _first_image_url(self, text: str) -> Optional[str]:
        for pattern in (
            r'(https?://[^"\']*mobygames\.com/images/(?:covers|shots)/[^"\']+)',
            r'["\'](/images/(?:covers|shots)/[^"\']+)["\']',
        ):
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return self._absolute_url(match.group(1))
        return None

    def _collect_image_links(self, text: str, image_type: str) -> List[dict]:
        output = []
        seen = set()
        patterns = [
            r'(https?://[^"\']*mobygames\.com/images/(?:covers|shots)/[^"\']+)',
            r'["\'](/images/(?:covers|shots)/[^"\']+)["\']',
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                url = self._absolute_url(match.group(1))
                clean_url = url.split("?", 1)[0]
                if clean_url in seen:
                    continue
                seen.add(clean_url)
                lower = clean_url.lower()
                detected_type = image_type
                if "/covers/" in lower:
                    detected_type = "front cover" if any(part in lower for part in ("front", "cover")) else "cover"
                if "/shots/" in lower:
                    detected_type = "screenshot"
                output.append(
                    {
                        "url": clean_url,
                        "image_url": clean_url,
                        "thumbnail_url": clean_url,
                        "type": detected_type,
                        "file_name": Path(urlparse(clean_url).path).name,
                    }
                )
        return output

    def search(self, title: str, system: str = "", limit: int = 5) -> List[dict]:
        normalized_title = _clean_rom_title(title or "")
        if not normalized_title:
            return []
        text, final_url = self._get_text(f"{self.WEB_BASE}/search/?q={quote(normalized_title, safe='')}")
        direct_match = self._game_match_from_page(text, final_url)
        expected_aliases = [alias.lower() for alias in MOBYGAMES_PLATFORM_ALIASES.get(_normalize_platform_key(system or ""), ())]
        if direct_match:
            direct_match["platform"] = self.platform_name_for_system(system) or "MobyGames page"
            return [direct_match]
        output = []
        seen = set()
        for match in re.finditer(r'<a[^>]+href=["\'](/game/(\d+)/[^"\']+)["\'][^>]*>(.*?)</a>', text, flags=re.DOTALL | re.IGNORECASE):
            href, game_id, label_html = match.group(1), match.group(2), match.group(3)
            if game_id in seen:
                continue
            seen.add(game_id)
            label = self._strip_tags(label_html)
            surrounding = self._strip_tags(text[max(0, match.start() - 300):match.end() + 300])
            score = 0
            if label.lower() == normalized_title.lower():
                score -= 10
            if expected_aliases and any(alias in surrounding.lower() for alias in expected_aliases):
                score -= 20
            output.append(
                {
                    "game_id": game_id,
                    "name": label,
                    "title": label,
                    "platform": self.platform_name_for_system(system) or "MobyGames",
                    "thumbnail_url": None,
                    "details_url": self._absolute_url(href),
                    "_score": score,
                }
            )
        output.sort(key=lambda item: (item["_score"], item["name"].lower()))
        for item in output:
            item.pop("_score", None)
        return output[: max(1, min(int(limit), 5))]

    def details(self, game_id: str, system: str = "") -> dict:
        safe_id = re.sub(r"[^0-9]", "", str(game_id or ""))
        if not safe_id:
            raise ValueError("game_id is required")
        text, final_url = self._get_text(f"{self.WEB_BASE}/game/{safe_id}/")
        title_match = re.search(r"<h1[^>]*>(.*?)</h1>", text, flags=re.DOTALL | re.IGNORECASE)
        title = self._strip_tags(title_match.group(1)) if title_match else ""
        description = ""
        desc_match = re.search(r"<h2[^>]*>\s*Description.*?</h2>(.*?)(?:<h2|<h3|$)", text, flags=re.DOTALL | re.IGNORECASE)
        if desc_match:
            description = self._strip_tags(desc_match.group(1))
        genre = ""
        genre_match = re.search(r"Genre\s*</[^>]+>\s*<[^>]+>(.*?)</", text, flags=re.DOTALL | re.IGNORECASE)
        if genre_match:
            genre = self._strip_tags(genre_match.group(1))
        release_date = ""
        release_match = re.search(r"Released\s*</[^>]+>\s*<[^>]+>(.*?)</", text, flags=re.DOTALL | re.IGNORECASE)
        if release_match:
            release_date = self._strip_tags(release_match.group(1))
        images = self._collect_image_links(text, "artwork")
        page_urls = [f"{self.WEB_BASE}/game/{safe_id}/covers/", f"{self.WEB_BASE}/game/{safe_id}/screenshots/"]
        for href in re.findall(r'href=["\'](/game/%s/[^"\']*(?:covers|screenshots)[^"\']*)["\']' % re.escape(safe_id), text, flags=re.IGNORECASE):
            page_urls.append(self._absolute_url(href))
        seen_pages = set()
        for page_url in page_urls:
            if page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            try:
                page_text, _ = self._get_text(page_url)
            except Exception:
                continue
            page_type = "screenshot" if "screenshots" in page_url else "cover"
            images.extend(self._collect_image_links(page_text, page_type))
        deduped = []
        seen_images = set()
        for image in images:
            url = image.get("url")
            if not url or url in seen_images:
                continue
            seen_images.add(url)
            deduped.append(image)
        return {
            "game_id": safe_id,
            "name": title,
            "overview": description,
            "release_date": release_date,
            "genre": genre,
            "developer": None,
            "publisher": None,
            "images": deduped,
        }

    def choose_image_for_field(self, details: dict, field: str) -> Optional[dict]:
        images = details.get("images") if isinstance(details, dict) else []
        if not isinstance(images, list):
            return None
        preferred = {
            "boxart": ("front cover", "cover"),
            "thumbnail": ("screenshot", "front cover", "cover"),
            "image": ("screenshot", "front cover", "cover"),
            "fanart": ("screenshot",),
            "marquee": (),
        }.get(field, ())
        for image_type in preferred:
            for image in images:
                if image_type in str(image.get("type") or "").lower():
                    return image
        return None

    def download_image(self, url: str) -> Tuple[bytes, str]:
        parsed = urlparse(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc.endswith("mobygames.com"):
            raise ValueError("image_url must be a MobyGames image URL")
        request = Request(url, headers={"User-Agent": SCRAPER_USER_AGENT})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            content_type = response.headers.get_content_type()
            if not str(content_type or "").startswith("image/"):
                raise ValueError("image_url did not return an image")
            max_bytes = 20 * 1024 * 1024
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("image is too large")
            return data, content_type


@dataclass(frozen=True)
class Settings:
    userdata_root: Path
    roms_root: Path
    bios_root: Path
    saves_root: Path
    username: Optional[str]
    password: Optional[str]
    credentials_file: Path
    https_port: int
    compatibility_https_ports: Tuple[int, ...]
    advertised_api_port: int

    image_cache_ttl_seconds: int
    image_miss_cache_ttl_seconds: int
    image_cache_max_items: int
    image_cache_max_bytes: int

    json_cache_ttl_seconds: int
    json_cache_max_items: int
    json_cache_max_bytes: int

    tls_cert_file: Optional[Path]
    tls_key_file: Optional[Path]
    tls_self_signed: bool
    tls_self_signed_dir: Path
    log_dir: Path
    stdout_log_file: str
    stderr_log_file: str
    overmind_log_file: str
    log_max_bytes: int
    log_backup_count: int
    rom_search_cache_ttl_seconds: int
    downloads_enabled: bool
    admin_enabled: bool
    themes_root: Path
    batocera_conf_file: Path
    es_settings_file: Path
    es_systems_file: Path
    batocera_theme_name: Optional[str]
    http_only: bool
    use_fake_data: bool
    fake_image_base_url: Optional[str]
    overmind_url: Optional[str]
    overmind_email: Optional[str]
    overmind_password: Optional[str]
    overmind_auth_token: Optional[str]
    overmind_token: Optional[str]
    overmind_device_id: str
    overmind_poll_seconds: int
    rom_metadata_poll_seconds: int
    hostname_override: Optional[str]
    public_ip_override: Optional[str]
    drone_cert_file: Path
    drone_key_file: Path
    drone_cert_days: int
    drone_mtls_enabled: bool
    drone_mtls_mode: str
    drone_mtls_ca_file: Optional[Path]

    @classmethod
    def from_env(cls) -> "Settings":
        https_port_value = os.environ.get("HTTPS_PORT", os.environ.get("PORT", "443"))
        advertised_api_port_value = (
            os.environ.get("DRONE_ADVERTISED_API_PORT")
            or os.environ.get("DRONE_PUBLIC_API_PORT")
            or https_port_value
        )
        compatibility_https_ports = _parse_port_list(os.environ.get("DRONE_COMPAT_HTTPS_PORTS", "8443"))
        cert_value = os.environ.get("TLS_CERT_FILE")
        key_value = os.environ.get("TLS_KEY_FILE")
        use_fake_data = _env_bool(False, "USE_FAKE_DATA")
        userdata_root = Path(os.environ.get("USERDATA_ROOT", "/userdata"))
        default_drone_cert = userdata_root / "system" / "drone-app" / "certs" / "drone.crt"
        default_drone_key = userdata_root / "system" / "drone-app" / "certs" / "drone.key"

        configured_overmind_device_id = _normalize_device_id(
            os.environ.get("OVERMIND_DEVICE_ID") or os.environ.get("DRONE_DEVICE_ID")
        )

        return cls(
            userdata_root=userdata_root,
            roms_root=Path(os.environ.get("ROMS_ROOT", "/userdata/roms")),
            bios_root=Path(os.environ.get("BIOS_ROOT", "/userdata/bios")),
            saves_root=Path(os.environ.get("SAVES_ROOT", "/userdata/saves")),
            username=os.environ.get("DRONE_APP_USERNAME") or None,
            password=os.environ.get("DRONE_APP_PASSWORD") or None,
            credentials_file=Path(os.environ.get("DRONE_CREDENTIALS_FILE", str(userdata_root / "system" / "drone-app" / "credentials.json"))),
            https_port=int(https_port_value),
            compatibility_https_ports=tuple(port for port in compatibility_https_ports if port != int(https_port_value)),
            advertised_api_port=int(advertised_api_port_value),
            image_cache_ttl_seconds=int(os.environ.get("IMAGE_CACHE_TTL_SECONDS", "3600")),
            image_miss_cache_ttl_seconds=int(os.environ.get("IMAGE_MISS_CACHE_TTL_SECONDS", "300")),
            image_cache_max_items=int(os.environ.get("IMAGE_CACHE_MAX_ITEMS", "1000")),
            image_cache_max_bytes=int(os.environ.get("IMAGE_CACHE_MAX_BYTES", str(256 * 1024 * 1024))),
            json_cache_ttl_seconds=int(os.environ.get("JSON_CACHE_TTL_SECONDS", "3600")),
            json_cache_max_items=int(os.environ.get("JSON_CACHE_MAX_ITEMS", "2000")),
            json_cache_max_bytes=int(os.environ.get("JSON_CACHE_MAX_BYTES", str(64 * 1024 * 1024))),
            tls_cert_file=Path(cert_value) if cert_value else None,
            tls_key_file=Path(key_value) if key_value else None,
            tls_self_signed=os.environ.get("TLS_SELF_SIGNED", "1") not in ("0", "false", "False"),
            tls_self_signed_dir=Path(os.environ.get("TLS_SELF_SIGNED_DIR", "/userdata/system/certs")),
            log_dir=Path(os.environ.get("LOG_DIR", "./logs")),
            stdout_log_file=os.environ.get("STDOUT_LOG_FILE", "stdout.log"),
            stderr_log_file=os.environ.get("STDERR_LOG_FILE", "stderr.log"),
            overmind_log_file=os.environ.get("OVERMIND_LOG_FILE", "overmind.log"),
            log_max_bytes=int(os.environ.get("LOG_MAX_BYTES", str(5 * 1024 * 1024))),
            log_backup_count=int(os.environ.get("LOG_BACKUP_COUNT", "5")),
            rom_search_cache_ttl_seconds=int(os.environ.get("ROM_SEARCH_CACHE_TTL_SECONDS", "300")),
            downloads_enabled=_env_bool(True, "ALLOW_CONTENT_DOWNLOAD", "DOWNLOAD", "DOWNLOADS_ENABLED"),
            admin_enabled=_env_bool(True, "ALLOW_ADMIN"),
            themes_root=Path(os.environ.get("THEMES_ROOT", "/userdata/themes")),
            batocera_conf_file=Path(os.environ.get("BATOCERA_CONF_FILE", "/userdata/system/batocera.conf")),
            es_settings_file=Path(
                os.environ.get("ES_SETTINGS_FILE", "/userdata/system/configs/emulationstation/es_settings.cfg")
            ),
            es_systems_file=Path(
                os.environ.get("ES_SYSTEMS_FILE", "/usr/share/emulationstation/es_systems.cfg")
            ),
            batocera_theme_name=os.environ.get("BATOCERA_THEME_NAME"),
            http_only=_env_bool(False, "HTTP_ONLY", "DRONE_APP_HTTP_ONLY"),
            use_fake_data=use_fake_data,
            fake_image_base_url=os.environ.get("FAKE_IMAGE_BASE_URL"),
            overmind_url=os.environ.get("OVERMIND_URL", "https://www.batocera-swarm.com"),
            overmind_email=os.environ.get("OVERMIND_EMAIL"),
            overmind_password=os.environ.get("OVERMIND_PASSWORD"),
            overmind_auth_token=os.environ.get("OVERMIND_AUTH_TOKEN") or os.environ.get("OVERMIND_AUTHORIZATION_TOKEN"),
            overmind_token=os.environ.get("OVERMIND_DRONE_TOKEN"),
            overmind_device_id=configured_overmind_device_id or (_fake_machine_id() if use_fake_data else _machine_id(userdata_root)),
            overmind_poll_seconds=OVERMIND_HEARTBEAT_SECONDS,
            rom_metadata_poll_seconds=max(0, int(os.environ.get("ROM_METADATA_POLL_SECONDS", str(ROM_METADATA_POLL_SECONDS)))),
            hostname_override=(os.environ.get("HOSTNAME_OVERRIDE") or "").strip() or None,
            public_ip_override=(os.environ.get("DRONE_PUBLIC_IP_OVERRIDE") or "").strip() or None,
            drone_cert_file=Path(os.environ.get("DRONE_CERT_FILE", os.environ.get("TLS_CERT_FILE", str(default_drone_cert)))),
            drone_key_file=Path(os.environ.get("DRONE_KEY_FILE", os.environ.get("TLS_KEY_FILE", str(default_drone_key)))),
            drone_cert_days=int(os.environ.get("DRONE_CERT_DAYS", "825")),
            drone_mtls_enabled=_env_bool(False, "DRONE_MTLS_ENABLED", "DRONE_TO_DRONE_MTLS_ENABLED"),
            drone_mtls_mode=(os.environ.get("DRONE_MTLS_MODE") or "self-signed").strip().lower(),
            drone_mtls_ca_file=Path(os.environ["DRONE_MTLS_CA_FILE"]) if os.environ.get("DRONE_MTLS_CA_FILE") else None,
        )


class _TimestampFormatter:
    """Thread-safe ISO-8601 timestamp provider."""
    _lock = Lock()

    @classmethod
    def now(cls) -> str:
        with cls._lock:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


class _TeeRotatingStream:
    def __init__(self, original_stream, log_path: Path, max_bytes: int, backup_count: int):
        self._original_stream = original_stream
        self._log_path = log_path
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._file = self._log_path.open("a", encoding="utf-8")
        self._lock = Lock()
        self._partial = ""  # buffer for partial-line writes

    def _rollover_if_needed(self) -> None:
        if self._max_bytes <= 0:
            return
        self._file.flush()
        if self._log_path.stat().st_size < self._max_bytes:
            return

        self._file.close()
        if self._backup_count > 0:
            for index in range(self._backup_count - 1, 0, -1):
                src = self._log_path.with_name(f"{self._log_path.name}.{index}")
                dst = self._log_path.with_name(f"{self._log_path.name}.{index + 1}")
                if src.exists():
                    if dst.exists():
                        dst.unlink()
                    src.rename(dst)

            first_backup = self._log_path.with_name(f"{self._log_path.name}.1")
            if first_backup.exists():
                first_backup.unlink()
            if self._log_path.exists():
                self._log_path.rename(first_backup)
        else:
            if self._log_path.exists():
                self._log_path.unlink()

        self._file = self._log_path.open("a", encoding="utf-8")

    def _timestamped_line(self, line: str) -> str:
        ts = _TimestampFormatter.now()
        return f"[{ts}] {line}"

    def write(self, data: str) -> int:
        if not isinstance(data, str):
            data = str(data)
        with self._lock:
            if data:
                # Prepend timestamp to each complete line in the data
                self._partial += data
                lines = self._partial.split("\n")
                # All complete lines (except possibly the last partial)
                complete = lines[:-1]
                self._partial = lines[-1]
                for line in complete:
                    ts_line = self._timestamped_line(line + "\n")
                    self._file.write(ts_line)
                    self._file.flush()
                self._rollover_if_needed()
            # original_stream is None for file-only streams (e.g. the Overmind log),
            # which must NOT also echo to the console/stdout.
            if self._original_stream is not None:
                self._original_stream.write(data)
            return len(data)

    def flush(self) -> None:
        with self._lock:
            if self._original_stream is not None:
                self._original_stream.flush()
            self._file.flush()

    def isatty(self) -> bool:
        return self._original_stream.isatty() if self._original_stream is not None else False


def _configure_rotating_logs(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = settings.log_dir / settings.stdout_log_file
    stderr_path = settings.log_dir / settings.stderr_log_file

    sys.stdout = _TeeRotatingStream(
        original_stream=sys.stdout,
        log_path=stdout_path,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )
    sys.stderr = _TeeRotatingStream(
        original_stream=sys.stderr,
        log_path=stderr_path,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )
    # Dedicated, file-only stream for Overmind-related chatter (heartbeat, asset/saves
    # sync details, speed samples, peer checks, token reclaim). Keeps stdout to high-level
    # lifecycle events. Surfaced in Log Sources as "drone_overmind".
    global _OVERMIND_LOG_STREAM
    _OVERMIND_LOG_STREAM = _TeeRotatingStream(
        original_stream=None,
        log_path=settings.log_dir / settings.overmind_log_file,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
    )


def _overmind_log(message: str, *, also_stdout: bool = False) -> None:
    """Record an Overmind-related event to the dedicated overmind.log.

    Detailed events (heartbeat, per-chunk uploads, sync triggers, speed/peer telemetry)
    go to overmind.log only. High-level lifecycle events pass ``also_stdout=True`` so a
    concise summary still appears in stdout.log. If the dedicated stream is not configured
    yet (e.g. unit tests, early startup), fall back to stdout so nothing is lost.
    """
    line = message if message.endswith("\n") else message + "\n"
    stream = _OVERMIND_LOG_STREAM
    if stream is None:
        sys.stdout.write(line)
        sys.stdout.flush()
        return
    stream.write(line)
    if also_stdout:
        sys.stdout.write(line)
        sys.stdout.flush()


class DroneCredentialStore:
    DEFAULT_USERNAME = "batocera"
    DEFAULT_PASSWORD = "linux"
    STATE_NAMESPACE = "credentials"

    def __init__(
        self,
        path: Path,
        env_username: Optional[str] = None,
        env_password: Optional[str] = None,
        state_database_file: Optional[Path] = None,
    ):
        self.path = path
        self.env_username = env_username
        self.env_password = env_password
        self.state_database_file = state_database_file or _state_database_path_for_legacy_file(path)
        self._lock = Lock()

    def _hash_password(self, password: str, salt: Optional[str] = None) -> str:
        salt_value = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_value.encode("ascii"), 240000)
        return f"pbkdf2_sha256$240000${salt_value}${digest.hex()}"

    def _verify_hash(self, password: str, stored: str) -> bool:
        try:
            scheme, rounds, salt, digest = stored.split("$", 3)
            if scheme != "pbkdf2_sha256":
                return False
            candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), int(rounds))
            return hmac.compare_digest(candidate.hex(), digest)
        except Exception:
            return False

    def load(self) -> dict:
        data = _load_state_payload(
            self.state_database_file,
            self.STATE_NAMESPACE,
            {},
            legacy_path=self.path,
        )
        if isinstance(data, dict) and data.get("username") and data.get("password_hash"):
            return data
        username = self.env_username or self.DEFAULT_USERNAME
        password = self.env_password or self.DEFAULT_PASSWORD
        return {"username": username, "password_plain_fallback": password, "source": "default"}

    def check(self, username: str, password: str) -> bool:
        data = self.load()
        if not hmac.compare_digest(username, str(data.get("username") or "")):
            return False
        password_hash = data.get("password_hash")
        if password_hash:
            return self._verify_hash(password, str(password_hash))
        return hmac.compare_digest(password, str(data.get("password_plain_fallback") or ""))

    def update(self, username: str, password: str) -> dict:
        username = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9._@-]{3,64}", username):
            raise ValueError("username must be 3-64 characters using letters, numbers, dot, dash, underscore, or @")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        with self._lock:
            data = {
                "username": username,
                "password_hash": self._hash_password(password),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_state_payload(
                self.state_database_file,
                self.STATE_NAMESPACE,
                data,
            )
            self.path.unlink(missing_ok=True)
            return {"username": username, "updated_at": data["updated_at"], "stored": True}


class BasicAuth:
    def __init__(self, username: Optional[str], password: Optional[str], credential_store: Optional[DroneCredentialStore] = None):
        self.username = username
        self.password = password
        self.credential_store = credential_store

    def check(self, header_value: Optional[str]) -> bool:
        if not header_value or not header_value.startswith("Basic "):
            return False

        try:
            encoded = header_value.split(" ", 1)[1].strip()
            decoded = base64.b64decode(encoded).decode("utf-8")
            user, pw = decoded.split(":", 1)
            if self.credential_store:
                return self.credential_store.check(user, pw)
            if not self.username or not self.password:
                return True
            return user == self.username and pw == self.password
        except Exception:
            return False


_UNAUTH_RATE_LIMIT_BUCKETS: "defaultdict[str, deque]" = defaultdict(deque)
_UNAUTH_RATE_LIMIT_LOCK = Lock()

# Brute-force auth blocker state. ``_AUTH_401_BUCKETS`` holds recent 401 timestamps
# per client IP (monotonic clock); ``_AUTH_BLOCKED_IPS`` maps a blocked IP to the
# monotonic time it should be unblocked. Both guarded by ``_AUTH_BLOCK_LOCK``.
_AUTH_401_BUCKETS: "defaultdict[str, deque]" = defaultdict(deque)
_AUTH_BLOCKED_IPS: "dict[str, float]" = {}
_AUTH_BLOCK_LOCK = Lock()


def _auth_block_exempt_ip(client_ip: str) -> bool:
    """Never block loopback so the local UI / on-device tooling can't lock itself out."""
    try:
        address = ipaddress.ip_address(str(client_ip or "").split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def record_unauthorized_response(client_ip: str, now: Optional[float] = None) -> bool:
    """Record a 401 for ``client_ip``; block the IP if it crosses the threshold.

    Returns True when this 401 triggered a new block. Blocking is logged to stdout
    (visible in the Drone container/service logs). Self-traffic (loopback) is exempt.
    """
    if not DRONE_AUTH_BLOCK_ENABLED:
        return False
    ip = str(client_ip or "-")
    if ip == "-" or _auth_block_exempt_ip(ip):
        return False
    timestamp = time.monotonic() if now is None else float(now)
    cutoff = timestamp - DRONE_AUTH_BLOCK_WINDOW_SECONDS
    with _AUTH_BLOCK_LOCK:
        if ip in _AUTH_BLOCKED_IPS:
            return False  # already blocked; nothing more to count
        bucket = _AUTH_401_BUCKETS[ip]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        bucket.append(timestamp)
        if len(bucket) < DRONE_AUTH_BLOCK_THRESHOLD:
            return False
        _AUTH_BLOCKED_IPS[ip] = timestamp + DRONE_AUTH_BLOCK_DURATION_SECONDS
        _AUTH_401_BUCKETS.pop(ip, None)
    print(
        f"Auth block: ip={ip} blocked after {DRONE_AUTH_BLOCK_THRESHOLD} unauthorized "
        f"requests within {int(DRONE_AUTH_BLOCK_WINDOW_SECONDS)}s; "
        f"blocked for {int(DRONE_AUTH_BLOCK_DURATION_SECONDS)}s",
        file=sys.stdout,
        flush=True,
    )
    return True


def is_ip_blocked(client_ip: str, now: Optional[float] = None) -> bool:
    """Return True if ``client_ip`` is currently blocked, expiring stale blocks lazily."""
    if not DRONE_AUTH_BLOCK_ENABLED:
        return False
    ip = str(client_ip or "-")
    if ip == "-" or _auth_block_exempt_ip(ip):
        return False
    timestamp = time.monotonic() if now is None else float(now)
    with _AUTH_BLOCK_LOCK:
        blocked_until = _AUTH_BLOCKED_IPS.get(ip)
        if blocked_until is None:
            return False
        if timestamp >= blocked_until:
            # 5-minute (configurable) block elapsed: unblock and start fresh.
            _AUTH_BLOCKED_IPS.pop(ip, None)
            _AUTH_401_BUCKETS.pop(ip, None)
            return False
        return True


def _is_external_client_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value or "").split("%", 1)[0])
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


def _unauthenticated_request_allowed(client_ip: str, now: Optional[float] = None) -> bool:
    if not DRONE_UNAUTH_RATE_LIMIT_ENABLED:
        return True
    if not _is_external_client_ip(client_ip):
        return True
    timestamp = time.monotonic() if now is None else float(now)
    cutoff = timestamp - DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS
    with _UNAUTH_RATE_LIMIT_LOCK:
        bucket = _UNAUTH_RATE_LIMIT_BUCKETS[str(client_ip or "-")]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= DRONE_UNAUTH_RATE_LIMIT_REQUESTS:
            return False
        bucket.append(timestamp)
        return True


class ExpiringLRUCache:
    def __init__(self, ttl_seconds: int, max_items: int, max_bytes: int):
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.total_bytes = 0
        self._items: "OrderedDict[str, dict]" = OrderedDict()
        self._lock = Lock()

    def _prune_expired_unlocked(self) -> None:
        now = time.time()
        expired_keys = [key for key, value in self._items.items() if value["expires_at"] <= now]
        for key in expired_keys:
            self.total_bytes -= self._items[key]["size"]
            del self._items[key]

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            self._prune_expired_unlocked()
            value = self._items.get(key)
            if value is None:
                return None
            self._items.move_to_end(key)
            return value

    def put(self, key: str, data: bytes, meta: Optional[dict] = None) -> None:
        size = len(data)
        if size > self.max_bytes:
            return

        entry = {
            "data": data,
            "size": size,
            "meta": meta or {},
            "expires_at": time.time() + self.ttl_seconds,
        }

        with self._lock:
            old = self._items.pop(key, None)
            if old:
                self.total_bytes -= old["size"]

            self._items[key] = entry
            self._items.move_to_end(key)
            self.total_bytes += size

            while len(self._items) > self.max_items or self.total_bytes > self.max_bytes:
                _, oldest = self._items.popitem(last=False)
                self.total_bytes -= oldest["size"]


class ExpiringKeyCache:
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._items: Dict[str, float] = {}
        self._lock = Lock()

    def has(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            expires_at = self._items.get(key)
            if not expires_at:
                return False
            if expires_at <= now:
                del self._items[key]
                return False
            return True

    def put(self, key: str) -> None:
        with self._lock:
            self._items[key] = time.time() + self.ttl_seconds


def json_bytes(obj: dict) -> bytes:
    return json.dumps(obj, indent=2).encode("utf-8")


def html_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def valid_segment(value: str) -> str:
    if not value or value in (".", "..") or "/" in value or "\x00" in value:
        raise ValueError("invalid path segment")
    return value


def _parse_batocera_theme_name(conf_path: Path) -> Optional[str]:
    if not conf_path.exists() or not conf_path.is_file():
        return None
    try:
        for raw_line in conf_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in ("global.theme", "system.theme"):
                candidate = value.strip().strip('"').strip("'")
                if candidate:
                    return candidate
    except Exception:
        return None
    return None


def _parse_es_theme_name(es_settings_path: Path) -> Optional[str]:
    if not es_settings_path.exists() or not es_settings_path.is_file():
        return None
    try:
        text = es_settings_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    # EmulationStation usually stores this as:
    # <string name="ThemeSet" value="carbon"/>
    match = re.search(r'<string\s+name="ThemeSet"\s+value="([^"]+)"', text, flags=re.IGNORECASE)
    if not match:
        return None
    theme = match.group(1).strip()
    return theme or None


def _set_screen_mode(settings: Settings, mode: str) -> Path:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"full", "kiosk", "kid"}:
        raise ValueError("Screen mode must be one of: full, kiosk, kid")
    path = settings.es_settings_file
    target = normalized_mode.title()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tree = ET.parse(path) if path.exists() else ET.ElementTree(ET.Element("map"))
    except ET.ParseError:
        tree = ET.ElementTree(ET.Element("map"))
    root = tree.getroot()
    node = root.find(".//string[@name='UIMode']")
    if node is None:
        node = ET.SubElement(root, "string")
        node.set("name", "UIMode")
    node.set("value", target)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def _get_screen_mode(settings: Settings) -> Optional[str]:
    try:
        root = ET.parse(settings.es_settings_file).getroot()
    except Exception:
        return None
    node = root.find(".//string[@name='UIMode']")
    if node is None:
        return None
    mode = str(node.get("value") or "").strip().lower()
    return mode if mode in {"full", "kiosk", "kid"} else None


def _get_audio_volume(settings: Settings) -> Optional[int]:
    """Best-effort read of the current output volume (0-100). Read-only, no root."""
    getter = shutil.which("batocera-settings-get")
    if getter:
        try:
            result = subprocess.run(
                [getter, "audio.volume"], capture_output=True, text=True, timeout=5
            )
            value = (result.stdout or "").strip()
            if value.isdigit():
                return max(0, min(100, int(value)))
        except (OSError, subprocess.SubprocessError):
            pass
    audio = shutil.which("batocera-audio")
    if audio:
        try:
            result = subprocess.run(
                [audio, "getSystemVolume"], capture_output=True, text=True, timeout=5
            )
            match = re.search(r"\d{1,3}", result.stdout or "")
            if match:
                return max(0, min(100, int(match.group())))
        except (OSError, subprocess.SubprocessError):
            pass
    amixer = shutil.which("amixer")
    if amixer:
        try:
            result = subprocess.run(
                [amixer, "sget", "Master"], capture_output=True, text=True, timeout=5
            )
            match = re.search(r"\[(\d{1,3})%\]", result.stdout or "")
            if match:
                return max(0, min(100, int(match.group(1))))
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _request_service_control(command: str, body: Optional[str] = None) -> bool:
    if command not in {
        "restart-emulationstation",
        "set-screen-mode-full",
        "set-screen-mode-kiosk",
        "set-screen-mode-kid",
        "repair-rom-permissions",
    }:
        return False
    control_dir = Path(os.environ.get("DRONE_SERVICE_CONTROL_DIR", "/userdata/system/drone-app/control"))
    request_path = control_dir / f"{command}.request"
    try:
        control_dir.mkdir(parents=True, exist_ok=True)
        # The privileged worker reads the first line: for parametrized commands
        # that is the argument (e.g. a system name); otherwise a timestamp marker.
        content = body if body is not None else datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        request_path.write_text(content + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def _request_rom_permission_repair(system: str = "") -> bool:
    """Best-effort: ask the privileged service worker to make a ROM system's
    images/ dir and gamelist.xml group-writable so the unprivileged Drone can
    place artwork and update the gamelist. No-op outside the on-device service."""
    try:
        return _request_service_control("repair-rom-permissions", body=str(system or "").strip())
    except Exception:
        return False


def _ensure_rom_write_access(settings: Settings, system: str = "", timeout_seconds: float = 5.0) -> bool:
    """Synchronously ask the privileged service worker to make a ROM system's media
    dirs + gamelist.xml writable by the Drone, waiting briefly for confirmation so a
    following write actually succeeds. Best-effort: returns True only on a confirmed
    "ok"; returns False on failure/timeout and never raises (e.g. off-device, or when
    the privileged worker is unavailable)."""
    control_dir = Path(os.environ.get("DRONE_SERVICE_CONTROL_DIR", "/userdata/system/drone-app/control"))
    result_path = control_dir / "repair-rom-permissions.result"
    try:
        result_path.unlink(missing_ok=True)
    except OSError:
        pass
    if not _request_rom_permission_repair(system):
        return False
    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    while time.monotonic() < deadline:
        try:
            if result_path.exists():
                result = result_path.read_text(encoding="utf-8", errors="ignore").strip()
                result_path.unlink(missing_ok=True)
                return result == "ok"
        except OSError:
            return False
        time.sleep(0.2)
    return False


def _request_screen_mode_service_control(mode: str) -> bool:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"full", "kiosk", "kid"}:
        raise ValueError("Screen mode must be one of: full, kiosk, kid")
    command = f"set-screen-mode-{normalized_mode}"
    control_dir = Path(os.environ.get("DRONE_SERVICE_CONTROL_DIR", "/userdata/system/drone-app/control"))
    request_path = control_dir / f"{command}.request"
    result_path = control_dir / f"{command}.result"
    try:
        result_path.unlink(missing_ok=True)
    except OSError:
        pass
    if not _request_service_control(command):
        return False
    deadline = time.monotonic() + max(3.0, float(os.environ.get("DRONE_SERVICE_CONTROL_TIMEOUT_SECONDS", "120")))
    while time.monotonic() < deadline:
        try:
            if result_path.exists():
                result = result_path.read_text(encoding="utf-8", errors="ignore").strip()
                result_path.unlink(missing_ok=True)
                if result == "ok":
                    return True
                raise OSError(result or "Privileged screen mode operation failed")
        except OSError:
            raise
        time.sleep(0.25)
    try:
        request_path.unlink(missing_ok=True)
    except OSError:
        pass
    raise OSError("Timed out waiting for the privileged screen mode service operation")


def _request_volume_service_control(level: int) -> bool:
    level = max(0, min(100, int(level)))
    control_dir = Path(os.environ.get("DRONE_SERVICE_CONTROL_DIR", "/userdata/system/drone-app/control"))
    request_path = control_dir / "set-volume.request"
    result_path = control_dir / "set-volume.result"
    try:
        result_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        control_dir.mkdir(parents=True, exist_ok=True)
        request_path.write_text(f"{level}\n", encoding="utf-8")
    except OSError:
        return False
    deadline = time.monotonic() + max(3.0, float(os.environ.get("DRONE_SERVICE_CONTROL_TIMEOUT_SECONDS", "120")))
    while time.monotonic() < deadline:
        try:
            if result_path.exists():
                result = result_path.read_text(encoding="utf-8", errors="ignore").strip()
                result_path.unlink(missing_ok=True)
                if result == "ok":
                    return True
                raise OSError(result or "Privileged volume operation failed")
        except OSError:
            raise
        time.sleep(0.25)
    try:
        request_path.unlink(missing_ok=True)
    except OSError:
        pass
    raise OSError("Timed out waiting for the privileged volume service operation")


def _apply_audio_volume(settings: Settings, level: int) -> int:
    """Apply the output volume (0-100), returning the clamped level that was set."""
    level = max(0, min(100, int(level)))
    if settings.use_fake_data:
        return level
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        _set_audio_volume_helper(level)
        return level
    if not _request_volume_service_control(level):
        raise OSError(
            "Unable to dispatch the privileged volume request; the Drone service "
            "control worker may not be running."
        )
    return level


def _emulationstation_restart_command() -> Optional[List[str]]:
    init_script = Path("/etc/init.d/S31emulationstation")
    if init_script.exists():
        return [str(init_script), "restart"]
    restart_tool = shutil.which("batocera-es-swissknife")
    if restart_tool:
        return [restart_tool, "--restart"]
    return None


def _restart_emulationstation() -> bool:
    if _request_service_control("restart-emulationstation"):
        return True
    command = _emulationstation_restart_command()
    if not command:
        return False
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def _apply_screen_mode(settings: Settings, mode: str) -> tuple[Path, bool]:
    if settings.use_fake_data:
        return _set_screen_mode(settings, mode), False
    # When the Drone app already runs as root, apply the change directly using the
    # proven stop -> write -> overlay -> start sequence shared with set_screen_mode.py.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        _set_screen_mode_helper(mode, config=settings.es_settings_file)
        return settings.es_settings_file, True
    # Non-root (production): the privileged service worker runs set_screen_mode.py for us.
    # _request_screen_mode_service_control returns True only after the worker reports "ok"
    # and raises on failure/timeout, so the result we report back to Overmind reflects
    # what actually happened instead of falsely claiming an EmulationStation restart.
    if not _request_screen_mode_service_control(mode):
        raise OSError(
            "Unable to dispatch the privileged screen mode request; the Drone service "
            "control worker may not be running."
        )
    return settings.es_settings_file, True


def _resolve_es_settings_file(settings: Settings) -> Optional[Path]:
    candidates = [
        settings.es_settings_file,
        Path("/userdata/system/configs/emulationstation/es_settings.cfg"),
        Path("/userdata/system/.emulationstation/es_settings.cfg"),
    ]
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return candidate
        except Exception:
            continue
    return None


def _parse_es_systems_cfg(path: Path) -> List[dict]:
    if not path.exists() or not path.is_file():
        return []
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except Exception:
        return []

    systems: List[dict] = []
    for system_node in root.findall(".//system"):
        data: dict = {}
        for tag in ("name", "fullname", "path", "extension", "command", "platform", "theme"):
            node = system_node.find(tag)
            if node is not None and node.text is not None:
                data[tag] = node.text.strip()
        hidden_attr = system_node.attrib.get("hidden")
        hidden_node = system_node.find("hidden")
        hidden_text = hidden_node.text.strip() if hidden_node is not None and hidden_node.text else ""
        hidden_value = (hidden_attr or hidden_text or "").strip().lower()
        data["hidden"] = hidden_value in ("1", "true", "yes", "on")
        if data.get("name"):
            systems.append(data)
    return systems


def _resolve_es_systems_effective(settings: Settings) -> Tuple[Optional[Path], List[dict]]:
    userdata_root = settings.userdata_root.resolve()
    override = (userdata_root / "system" / "configs" / "emulationstation" / "es_systems.cfg").resolve()
    base = settings.es_systems_file

    source_path = override if override.exists() and override.is_file() else base
    systems = _parse_es_systems_cfg(source_path)

    # Apply overlays (es_systems_<name>.cfg) by replacing/adding per <name>.
    overlay_dir = (userdata_root / "system" / "configs" / "emulationstation").resolve()
    overlays: List[Path] = []
    if overlay_dir.exists() and overlay_dir.is_dir():
        overlays = sorted(
            [p for p in overlay_dir.glob("es_systems_*.cfg") if p.is_file()],
            key=lambda p: p.name.lower(),
        )

    by_name = {item.get("name"): item for item in systems if item.get("name")}
    for overlay in overlays:
        for item in _parse_es_systems_cfg(overlay):
            name = item.get("name")
            if not name:
                continue
            by_name[name] = item

    merged = list(by_name.values())
    merged.sort(key=lambda item: str(item.get("name", "")).lower())
    return source_path if source_path.exists() else None, merged


def _resolve_theme_dir(settings: Settings) -> Optional[Path]:
    es_settings_file = _resolve_es_settings_file(settings)
    from_es_settings = _parse_es_theme_name(es_settings_file) if es_settings_file else None
    theme_name = (
        settings.batocera_theme_name
        or _parse_batocera_theme_name(settings.batocera_conf_file)
        or from_es_settings
    )
    if theme_name:
        theme_dir = (settings.themes_root / theme_name).resolve()
        if theme_dir.exists() and theme_dir.is_dir():
            return theme_dir

    # Batocera installs can omit explicit theme settings.
    # If exactly one theme directory exists, use it automatically.
    try:
        candidates = sorted(
            [entry.resolve() for entry in settings.themes_root.iterdir() if entry.is_dir()],
            key=lambda p: p.name.lower(),
        )
    except Exception:
        return None

    if len(candidates) == 1:
        return candidates[0]
    return None


class RomRepository:
    def __init__(self, roms_root: Path, bios_root: Path, rom_search_cache_ttl_seconds: int = 300, settings=None):
        self.roms_root = roms_root
        self.bios_root = bios_root
        # Settings are required to read the relational SQLite cache. When absent
        # (e.g. unit tests constructing a bare repository) the cache-backed paths
        # transparently fall back to scanning the filesystem.
        self.settings = settings
        self.rom_search_cache_ttl_seconds = rom_search_cache_ttl_seconds
        self._search_cache_lock = Lock()
        self._search_index: List[dict] = []
        self._search_index_expires_at = 0.0
        self._missing_artwork_cache_lock = Lock()
        self._missing_artwork_cache: Dict[str, dict] = {}

    @staticmethod
    def should_include_system(name: str) -> bool:
        return not str(name or "").strip().lower().endswith(".old")

    @staticmethod
    def build_unique_id(path: Path) -> str:
        resolved = path.resolve()
        stat = resolved.stat()
        raw = f"{resolved}|{stat.st_size}|{int(stat.st_mtime)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def build_fingerprint(path: Path) -> str:
        """Content fingerprint for cross-drone file identity (``sample-fp-v1``).

        Hashes the file size plus up to three fixed 64 KB windows (head, middle,
        tail). Files at or below the small-file threshold are hashed whole, so
        small files are exact. Cost is constant regardless of file size, which is
        what lets us fingerprint multi-GB disc images without reading them end to
        end. Folding the size into the digest means two files of different size can
        never collide. This is the imohash approach used by file-sync tools: it is
        not a cryptographic hash, but for "is this the same file on another drone?"
        the collision probability is negligible. Deterministic across drones."""
        size = int(path.stat().st_size)
        digest = hashlib.md5()
        digest.update(size.to_bytes(8, "little"))
        with path.open("rb") as handle:
            if size <= FINGERPRINT_SMALL_FILE_BYTES:
                digest.update(handle.read())
            else:
                digest.update(handle.read(FINGERPRINT_SAMPLE_BYTES))
                handle.seek(max(0, size // 2 - FINGERPRINT_SAMPLE_BYTES // 2))
                digest.update(handle.read(FINGERPRINT_SAMPLE_BYTES))
                handle.seek(size - FINGERPRINT_SAMPLE_BYTES)
                digest.update(handle.read(FINGERPRINT_SAMPLE_BYTES))
        return digest.hexdigest()

    @staticmethod
    def build_md5(path: Path) -> str:
        """Full-file MD5 — used for BIOS identity.

        BIOS files must be matched exactly against known-good dumps (No-Intro / MAME
        BIOS sets), so unlike ROMs they use a true content MD5 rather than the sampled
        fingerprint. BIOS files are small, so reading them whole is cheap."""
        digest = hashlib.md5()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def build_directory_stats(path: Path) -> Tuple[int, int]:
        total_size = 0
        latest_mtime = int(path.stat().st_mtime)
        for child in path.rglob("*"):
            if not child.is_file():
                continue
            stat = child.stat()
            total_size += int(stat.st_size)
            latest_mtime = max(latest_mtime, int(stat.st_mtime))
        return total_size, latest_mtime

    @staticmethod
    def should_ignore_rom_file(file_name: str, system: Optional[str] = None) -> bool:
        lower = str(file_name or "").strip().lower()
        if lower.startswith(".") or lower in {"_info.txt", "gamelist.xml", ".keep", ".gitkeep", "readme.md"}:
            return True
        if lower.endswith(".sh.keys"):
            return True
        ignored_extensions = {
            ".xml", ".txt", ".md", ".nfo", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
            ".mp4", ".mkv", ".avi", ".mov", ".pdf", ".cue", ".m3u", ".json", ".db",
        }
        if lower.endswith(tuple(ignored_extensions)):
            return True
        return False

    @staticmethod
    def should_ignore_rom_path(path: Path) -> bool:
        ignored_dirs = {
            "images", "videos", "manuals", "media", "downloaded_images", "covers",
            "boxart", "fanart", "marquee", "thumbs", "screenshots",
        }
        if any(part.startswith(".") or part.lower() in ignored_dirs for part in path.parts):
            return True
        return RomRepository.should_ignore_rom_file(path.name)

    @staticmethod
    def iter_files(path: Path) -> Iterable[Path]:
        if not path.exists() or not path.is_dir():
            return []
        return [entry for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()) if entry.is_file()]

    def _list_rom_items(self, system: str, asset_dir: Path, include_fingerprint: bool = True) -> List[dict]:
        items: List[dict] = []
        system_lower = system.lower()

        if not asset_dir.exists() or not asset_dir.is_dir():
            return items

        if system_lower in ("ps3", "ps4"):
            for entry in sorted(asset_dir.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_file():
                    if system_lower == "steam" and entry.suffix.lower() == ".sh":
                        continue
                    if self.should_ignore_rom_file(entry.name, system=system):
                        continue
                    stat = entry.stat()
                    items.append(
                        {
                            "unique_id": self.build_unique_id(entry),
                            "name": entry.name,
                            "rom_file": entry.name,
                            "byte_count": stat.st_size,
                            "entry_type": "file",
                            "is_downloadable": True,
                            "image_stem": Path(entry.name).stem,
                        }
                    )
                    continue

                if not entry.is_dir():
                    continue

                if system_lower == "ps3":
                    if not entry.name.lower().endswith(".ps3"):
                        continue
                    size, mtime = self.build_directory_stats(entry)
                    display_name = entry.name[:-4]
                    items.append(
                        {
                            "unique_id": self.build_unique_id(entry),
                            "name": display_name,
                            "rom_file": entry.name,
                            "filename": entry.name,
                            "relative_path": entry.name,
                            "absolute_path": str(entry.resolve()),
                            "rom_path": entry.name,
                            "file_path": entry.name,
                            "byte_count": size,
                            "size": size,
                            "file_size": size,
                            "modified_time": mtime,
                            "mtime": mtime,
                            "entry_type": "folder",
                            "is_downloadable": False,
                            "source_folder": entry.name,
                            "source": "disk",
                            "metadata_source": None,
                            "image_stem": display_name,
                        }
                    )
                    continue

                # PS4: each game is a folder and includes a ".ps4" marker file for game name.
                ps4_name_file = None
                for child in sorted(entry.iterdir(), key=lambda p: p.name.lower()):
                    if child.is_file() and child.name.lower().endswith(".ps4"):
                        ps4_name_file = child
                        break
                if not ps4_name_file:
                    continue

                size, mtime = self.build_directory_stats(entry)
                display_name = ps4_name_file.stem
                items.append(
                    {
                        "unique_id": self.build_unique_id(entry),
                        "name": display_name,
                        "rom_file": entry.name,
                        "filename": entry.name,
                        "relative_path": entry.name,
                        "absolute_path": str(entry.resolve()),
                        "rom_path": entry.name,
                        "file_path": entry.name,
                        "byte_count": size,
                        "size": size,
                        "file_size": size,
                        "modified_time": mtime,
                        "mtime": mtime,
                        "entry_type": "folder",
                        "is_downloadable": False,
                        "source_folder": entry.name,
                        "source": "disk",
                        "metadata_source": None,
                        "image_stem": display_name,
                    }
                )
            return items

        for entry in sorted(asset_dir.rglob("*"), key=lambda p: p.relative_to(asset_dir).as_posix().lower()):
            if not entry.is_file():
                continue
            relative_path = entry.relative_to(asset_dir).as_posix()
            if self.should_ignore_rom_path(Path(relative_path)):
                continue
            display_name = Path(entry.name).stem
            stat = entry.stat()
            item = {
                "unique_id": self.build_unique_id(entry),
                "name": display_name,
                "rom_file": entry.name,
                "filename": entry.name,
                "relative_path": relative_path,
                "absolute_path": str(entry.resolve()),
                "rom_path": relative_path,
                "file_path": relative_path,
                "byte_count": stat.st_size,
                "size": stat.st_size,
                "file_size": stat.st_size,
                "modified_time": int(stat.st_mtime),
                "mtime": int(stat.st_mtime),
                "source": "disk",
                "metadata_source": None,
                "entry_type": "file",
                "is_downloadable": (system_lower != "steam"),
                "image_stem": display_name,
            }
            if include_fingerprint:
                fingerprint_value = self.build_fingerprint(entry)
                item["fingerprint"] = fingerprint_value
                item["rom_fingerprint"] = fingerprint_value
            items.append(item)
        return items

    def _count_rom_items(self, system: str, asset_dir: Path) -> int:
        system_lower = system.lower()
        if not asset_dir.exists() or not asset_dir.is_dir():
            return 0

        if system_lower in ("ps3", "ps4"):
            count = 0
            for entry in sorted(asset_dir.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_file():
                    if system_lower == "steam" and entry.suffix.lower() == ".sh":
                        continue
                    if self.should_ignore_rom_file(entry.name, system=system):
                        continue
                    count += 1
                    continue

                if not entry.is_dir():
                    continue

                if system_lower == "ps3":
                    if entry.name.lower().endswith(".ps3"):
                        count += 1
                    continue

                for child in sorted(entry.iterdir(), key=lambda p: p.name.lower()):
                    if child.is_file() and child.name.lower().endswith(".ps4"):
                        count += 1
                        break
            return count

        count = 0
        for entry in sorted(asset_dir.rglob("*"), key=lambda p: p.relative_to(asset_dir).as_posix().lower()):
            if not entry.is_file():
                continue
            relative_path = entry.relative_to(asset_dir).as_posix()
            if self.should_ignore_rom_path(Path(relative_path)):
                continue
            count += 1
        return count

    def _attach_gamelist_to_rom_items(self, system_dir: Path, items: List[dict]) -> List[dict]:
        try:
            _, root = self._read_gamelist(system_dir)
        except Exception:
            root = ET.Element("gameList")
        exact_paths = {}
        path_names = {}
        path_stems = {}
        display_names = {}
        for index, game in enumerate(root.findall("game")):
            path_value = _normalize_gamelist_rom_path(_text_or_empty(game, "path")).lower()
            if path_value:
                exact_paths.setdefault(path_value, game)
                path_name = Path(path_value).name.lower()
                path_names.setdefault(path_name, (index, game))
                path_stems.setdefault(Path(path_name).stem.lower(), (index, game))
            display_name = _text_or_empty(game, "name").lower()
            if display_name:
                display_names.setdefault(display_name, (index, game))
        for item in items:
            rom_file = str(item.get("rom_file") or item.get("name") or "")
            display_name = str(item.get("image_stem") or item.get("name") or "")
            relative_path = str(item.get("relative_path") or item.get("rom_path") or rom_file)
            normalized_path = _normalize_gamelist_rom_path(relative_path).lower()
            game = exact_paths.get(normalized_path)
            if game is None:
                normalized_file = rom_file.lower()
                normalized_file_stem = Path(rom_file).stem.lower()
                normalized_display = display_name.lower()
                candidates = [
                    path_names.get(normalized_file),
                    path_stems.get(normalized_file_stem),
                    path_stems.get(normalized_display),
                    display_names.get(normalized_display),
                    display_names.get(normalized_file),
                    display_names.get(normalized_file_stem),
                ]
                matches = [candidate for candidate in candidates if candidate is not None]
                if matches:
                    game = min(matches, key=lambda candidate: candidate[0])[1]
            item["rom_path"] = relative_path
            item["title"] = _text_or_empty(game, "name") if game is not None else str(item.get("name") or display_name)
            item["existing"] = {field: _text_or_empty(game, field) if game is not None else "" for field in ARTWORK_FIELDS}
            item["gamelist"] = _gamelist_details(game)
            item["has_gamelist_entry"] = game is not None
            item["metadata_source"] = "gamelist.xml" if game is not None else item.get("metadata_source")
        return items

    def list_gamelist_rom_metadata(self, system: str, system_dir: Optional[Path] = None) -> Tuple[dict, List[dict]]:
        """Build ROM metadata from gamelist.xml, statting only referenced ROM paths."""
        system = valid_segment(system)
        system_dir = (system_dir or self.get_system_dir(system)).resolve()
        gamelist_path = system_dir / "gamelist.xml"
        tree, root = self._read_gamelist(system_dir)
        del tree
        gamelist_stat = gamelist_path.stat() if gamelist_path.exists() and gamelist_path.is_file() else None
        items: List[dict] = []
        seen_paths = set()
        system_lower = system.lower()
        for game in root.findall("game"):
            relative_path = _normalize_gamelist_rom_path(_text_or_empty(game, "path"))
            if not relative_path:
                continue
            normalized_key = relative_path.lower()
            if normalized_key in seen_paths:
                continue
            seen_paths.add(normalized_key)
            rom_path = (system_dir / relative_path).resolve()
            try:
                rom_path.relative_to(system_dir)
            except ValueError:
                continue
            if not rom_path.exists():
                continue
            if rom_path.is_dir():
                size, mtime = self.build_directory_stats(rom_path)
                entry_type = "folder"
                is_downloadable = False
            elif rom_path.is_file():
                stat = rom_path.stat()
                size = int(stat.st_size)
                mtime = int(stat.st_mtime)
                entry_type = "file"
                is_downloadable = system_lower != "steam"
            else:
                continue
            display_name = Path(relative_path).stem
            title = _text_or_empty(game, "name") or display_name
            item = {
                "unique_id": self.build_unique_id(rom_path),
                "name": title,
                "rom_name": title,
                "title": title,
                "rom_file": Path(relative_path).name,
                "filename": Path(relative_path).name,
                "relative_path": relative_path,
                "absolute_path": str(rom_path),
                "rom_path": relative_path,
                "file_path": relative_path,
                "byte_count": size,
                "size": size,
                "file_size": size,
                "modified_time": mtime,
                "mtime": mtime,
                "source": "gamelist.xml",
                "metadata_source": "gamelist.xml",
                "entry_type": entry_type,
                "is_downloadable": is_downloadable,
                "image_stem": display_name,
                "existing": {field: _text_or_empty(game, field) for field in ARTWORK_FIELDS},
                "gamelist": _gamelist_details(game),
                "gamelist_path": str(gamelist_path),
                "gamelist_game_id": _gamelist_game_id(game, relative_path),
                "has_gamelist_entry": True,
            }
            items.append(item)
        try:
            disk_items = self._list_rom_items(system, system_dir, include_fingerprint=False)
        except Exception as error:
            print(f"ROM metadata disk supplement skipped: system={system} error={_format_overmind_error(error)}", file=sys.stderr, flush=True)
            disk_items = []
        for disk_item in disk_items:
            relative_path = _normalize_gamelist_rom_path(
                disk_item.get("file_path") or disk_item.get("relative_path") or disk_item.get("rom_path") or ""
            )
            if not relative_path:
                continue
            normalized_key = relative_path.lower()
            if normalized_key in seen_paths:
                continue
            seen_paths.add(normalized_key)
            disk_item = dict(disk_item)
            disk_item["name"] = disk_item.get("name") or Path(relative_path).stem
            disk_item["rom_name"] = disk_item.get("rom_name") or disk_item.get("name") or Path(relative_path).stem
            disk_item["title"] = disk_item.get("title") or disk_item.get("rom_name")
            disk_item["relative_path"] = relative_path
            disk_item["rom_path"] = relative_path
            disk_item["file_path"] = relative_path
            disk_item["source"] = "filesystem"
            disk_item["metadata_source"] = "filesystem"
            disk_item["gamelist"] = {}
            disk_item["gamelist_path"] = ""
            disk_item["gamelist_game_id"] = relative_path
            disk_item["has_gamelist_entry"] = False
            disk_item.setdefault("existing", {})
            items.append(disk_item)
        items.sort(key=lambda item: str(item.get("relative_path") or "").lower())
        gamelist = {
            "system": system,
            "system_name": system,
            "path": str(gamelist_path),
            "file_path": str(gamelist_path),
            "exists": bool(gamelist_stat),
            "rom_count": len(items),
        }
        if gamelist_stat:
            gamelist["file_size"] = int(gamelist_stat.st_size)
            gamelist["modified_time"] = int(gamelist_stat.st_mtime)
        return gamelist, items

    def get_system_dir(self, system: str) -> Path:
        system = valid_segment(system)
        system_link = self.roms_root / system

        if not system_link.exists():
            raise FileNotFoundError()
        if not (system_link.is_dir() or system_link.is_symlink()):
            raise ValueError("system is not a directory")

        system_dir = system_link.resolve()
        if not system_dir.exists():
            raise FileNotFoundError()
        if not system_dir.is_dir():
            raise ValueError("system target is not a directory")

        return system_dir

    def list_system_names(self) -> List[str]:
        """List usable ROM system directories without walking their content."""
        if not self.roms_root.exists():
            raise FileNotFoundError(str(self.roms_root))
        names = []
        for entry in sorted(self.roms_root.iterdir(), key=lambda p: p.name.lower()):
            if not (entry.is_dir() or entry.is_symlink()) or not self.should_include_system(entry.name):
                continue
            target_dir = entry.resolve()
            if target_dir.exists() and target_dir.is_dir():
                names.append(entry.name)
        return names

    def list_systems(self) -> List[dict]:
        cached_systems = _read_sqlite_asset_systems(self.roms_root.parent)
        if cached_systems:
            return [
                row for row in cached_systems
                if self.should_include_system(str(row.get("name") or ""))
            ]
        cached = self._cached_asset_snapshot()
        if cached:
            counts: Dict[str, int] = {}
            for row in cached.get("roms") or []:
                system = str(row.get("system") or row.get("system_name") or "").strip()
                if system:
                    counts[system] = counts.get(system, 0) + 1
            if counts:
                return [{"name": name, "rom_count": counts[name]} for name in sorted(counts, key=str.lower)]
        systems = []
        for system_name in self.list_system_names():
            target_dir = self.get_system_dir(system_name)
            rom_count = self._count_rom_items(system_name, target_dir)
            if rom_count < 1:
                continue

            systems.append({"name": system_name, "rom_count": rom_count})

        return systems

    def _build_search_index(self) -> List[dict]:
        cached = self._cached_asset_snapshot()
        if cached:
            index = []
            for rom in cached.get("roms") or []:
                system_name = str(rom.get("system") or rom.get("system_name") or "").strip()
                if not system_name or not self.should_include_system(system_name):
                    continue
                index.append(
                    {
                        "system": system_name,
                        "name": rom.get("rom_name") or rom.get("name") or rom.get("file_path") or "",
                        "unique_id": rom.get("unique_id", ""),
                        "is_downloadable": rom.get("is_downloadable", True),
                        "image_stem": rom.get("image_stem"),
                        "fingerprint": rom.get("fingerprint") or rom.get("rom_fingerprint"),
                    }
                )
            return index
        if not self.roms_root.exists():
            return []
        index: List[dict] = []
        for entry in sorted(self.roms_root.iterdir(), key=lambda p: p.name.lower()):
            if not (entry.is_dir() or entry.is_symlink()):
                continue
            if not self.should_include_system(entry.name):
                continue
            system_name = entry.name
            try:
                _, roms = self.list_assets(system_name, "roms")
            except Exception:
                continue
            for rom in roms:
                index.append(
                    {
                        "system": system_name,
                        "name": rom.get("name", ""),
                        "unique_id": rom.get("unique_id", ""),
                        "is_downloadable": rom.get("is_downloadable", True),
                        "image_stem": rom.get("image_stem"),
                        "fingerprint": rom.get("fingerprint") or rom.get("rom_fingerprint"),
                    }
                )
        return index

    def search_roms(self, query: str, limit: Optional[int] = None, system_filter: Optional[str] = None) -> List[dict]:
        normalized = query.strip()
        if not normalized:
            return []

        # Fast path: search SQLite directly (FTS5 trigram, or indexed LIKE fallback).
        # No full-cache materialization or per-query linear scan. Used whenever the
        # relational cache is populated; otherwise we fall back to the legacy index.
        if self.settings is not None and rom_cache_has_entries(self.settings):
            rows = search_rom_entries(self.settings, normalized, system_filter=system_filter)
            results = [item for item in rows if self.should_include_system(item["system"])]
            if limit is not None and limit > 0:
                return results[:limit]
            return results

        # Legacy fallback: in-memory index built from the snapshot/filesystem.
        normalized_lower = normalized.lower()
        normalized_system_filter = system_filter.strip().lower() if system_filter else None
        with self._search_cache_lock:
            now = time.time()
            if now >= self._search_index_expires_at:
                self._search_index = self._build_search_index()
                self._search_index_expires_at = now + self.rom_search_cache_ttl_seconds
            source = list(self._search_index)

        results = []
        for item in source:
            if normalized_lower not in item["name"].lower():
                continue
            if normalized_system_filter and item["system"].lower() != normalized_system_filter:
                continue
            results.append(item)
        results.sort(key=lambda item: (item["system"].lower(), item["name"].lower()))
        if limit is not None and limit > 0:
            return results[:limit]
        return results

    def _read_gamelist(self, system_dir: Path) -> Tuple[ET.ElementTree, ET.Element]:
        gamelist_path = system_dir / "gamelist.xml"
        if gamelist_path.exists() and gamelist_path.is_file():
            try:
                tree = ET.parse(gamelist_path)
                root = tree.getroot()
                if root.tag != "gameList":
                    raise ValueError("gamelist root is not gameList")
                return tree, root
            except ET.ParseError as error:
                raise ValueError(f"invalid gamelist.xml: {error}") from error
        root = ET.Element("gameList")
        return ET.ElementTree(root), root

    def _find_gamelist_entry(self, root: ET.Element, rom_name: str, rom_display_name: str) -> Optional[ET.Element]:
        normalized_file = rom_name.lower()
        normalized_file_stem = Path(rom_name).stem.lower()
        normalized_display = rom_display_name.lower()
        for game in root.findall("game"):
            path_value = _text_or_empty(game, "path")
            if path_value:
                path_name = Path(path_value.replace("\\", "/")).name.lower()
                if path_name == normalized_file or Path(path_name).stem.lower() in (normalized_file_stem, normalized_display):
                    return game
            name_value = _text_or_empty(game, "name").lower()
            if name_value and name_value in (normalized_display, normalized_file, normalized_file_stem):
                return game
        return None

    def _find_gamelist_entry_by_path(self, root: ET.Element, rom_path: str) -> Optional[ET.Element]:
        normalized = _normalize_gamelist_rom_path(rom_path).lower()
        if not normalized:
            return None
        for game in root.findall("game"):
            path_value = _normalize_gamelist_rom_path(_text_or_empty(game, "path")).lower()
            if path_value == normalized:
                return game
        return None

    def _entry_missing_artwork(self, game: Optional[ET.Element]) -> List[str]:
        missing = []
        for field in ARTWORK_FIELDS:
            if game is None or not _text_or_empty(game, field):
                missing.append(field)
        if self._entry_has_duplicate_artwork(game):
            missing.append(ARTWORK_DUPLICATE_FILTER)
        return missing

    def _entry_has_duplicate_artwork(self, game: Optional[ET.Element]) -> bool:
        if game is None:
            return False
        seen = {}
        for field in ARTWORK_FIELDS:
            identity = _artwork_identity(_text_or_empty(game, field))
            if not identity:
                continue
            if identity in seen:
                return True
            seen[identity] = field
        return False

    def list_artwork_metadata(self) -> List[dict]:
        if not self.roms_root.exists():
            return []
        rows: List[dict] = []
        for entry in sorted(self.roms_root.iterdir(), key=lambda p: p.name.lower()):
            if not (entry.is_dir() or entry.is_symlink()):
                continue
            if not self.should_include_system(entry.name):
                continue
            system = entry.name
            system_dir = entry.resolve()
            try:
                _, root = self._read_gamelist(system_dir)
            except Exception:
                continue
            for game in root.findall("game"):
                rom_path = _normalize_gamelist_rom_path(_text_or_empty(game, "path"))
                if not rom_path:
                    continue
                artwork_types = [
                    field for field in ARTWORK_FIELDS
                    if _text_or_empty(game, field)
                ]
                if not artwork_types:
                    continue
                rows.append({
                    "asset_type": "artwork",
                    "system": system,
                    "system_name": system,
                    "rom_path": rom_path,
                    "file_path": rom_path,
                    "rom_name": Path(rom_path).name,
                    "title": _text_or_empty(game, "name") or Path(rom_path).stem,
                    "artwork_types": artwork_types,
                    "metadata_source": "gamelist.xml",
                })
        rows.sort(key=lambda item: (str(item.get("system") or "").lower(), str(item.get("rom_path") or "").lower()))
        return rows

    def resolve_artwork_file(self, system: str, rom_path: str, artwork_type: str) -> Tuple[Path, str, str]:
        system_dir = self.get_system_dir(system).resolve()
        field = str(artwork_type or "").strip()
        if field not in ARTWORK_FIELDS:
            raise ValueError("invalid artwork type")
        tree, root = self._read_gamelist(system_dir)
        game = self._find_gamelist_entry_by_path(root, rom_path)
        if game is None:
            raise FileNotFoundError()
        artwork_ref = _normalize_gamelist_rom_path(_text_or_empty(game, field))
        if not artwork_ref:
            raise FileNotFoundError()
        target = (system_dir / artwork_ref).resolve()
        if not target.exists() or not target.is_file() or (target != system_dir and system_dir not in target.parents):
            raise FileNotFoundError()
        return target, target.relative_to(system_dir).as_posix(), artwork_ref

    def update_gamelist_artwork_reference(self, system: str, rom_path: str, artwork_type: str, artwork_relative_path: str) -> dict:
        system_dir = self.get_system_dir(system).resolve()
        field = str(artwork_type or "").strip()
        if field not in ARTWORK_FIELDS:
            raise ValueError("invalid artwork type")
        normalized_rom_path = _normalize_gamelist_rom_path(rom_path)
        normalized_artwork_path = _normalize_gamelist_rom_path(artwork_relative_path)
        if not normalized_rom_path or not normalized_artwork_path:
            raise ValueError("rom_path and artwork path are required")
        gamelist_path = system_dir / "gamelist.xml"
        # Hold the lock across the whole read-modify-write so parallel artwork
        # downloads for this system serialize instead of overwriting each other.
        with _GAMELIST_WRITE_LOCK:
            tree, root = self._read_gamelist(system_dir)
            game = self._find_gamelist_entry_by_path(root, normalized_rom_path)
            if game is None:
                game = ET.SubElement(root, "game")
                _set_child_text(game, "path", f"./{normalized_rom_path}")
                _set_child_text(game, "name", Path(normalized_rom_path).stem)
            _set_child_text(game, field, f"./{normalized_artwork_path}")
            try:
                ET.indent(tree, space="  ")
            except Exception:
                pass
            tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
            with gamelist_path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
        return {"system": system, "rom_path": normalized_rom_path, "artwork_type": field, "artwork_path": normalized_artwork_path}

    def _rom_path_exists(self, system: str, rom_path: str) -> bool:
        try:
            system_dir = self.get_system_dir(system)
            normalized = _normalize_gamelist_rom_path(rom_path)
            if not normalized or "\x00" in normalized:
                return False
            target_path = (system_dir / normalized).resolve()
            if target_path != system_dir and system_dir not in target_path.parents:
                return False
            return target_path.exists()
        except Exception:
            return False

    def _list_missing_artwork_from_gamelists(self, include_complete: bool = False) -> List[dict]:
        if not self.roms_root.exists():
            return []
        missing_items: List[dict] = []
        for entry in sorted(self.roms_root.iterdir(), key=lambda p: p.name.lower()):
            if not (entry.is_dir() or entry.is_symlink()):
                continue
            if not self.should_include_system(entry.name):
                continue
            system = entry.name
            system_dir = entry.resolve()
            if not system_dir.exists() or not system_dir.is_dir():
                continue
            try:
                _, root = self._read_gamelist(system_dir)
            except Exception:
                continue
            for game in root.findall("game"):
                missing = self._entry_missing_artwork(game)
                if not missing and not include_complete:
                    continue
                rom_path = _normalize_gamelist_rom_path(_text_or_empty(game, "path"))
                if not rom_path:
                    continue
                rom_name = Path(rom_path).name
                title = _text_or_empty(game, "name") or Path(rom_name).stem
                missing_items.append(
                    {
                        "system": system,
                        "name": title,
                        "rom_name": rom_name,
                        "rom_path": rom_path,
                        "title": title,
                        "search_title": _clean_rom_title(title or rom_name),
                        "unique_id": "",
                        "missing": missing,
                        "existing": {field: _text_or_empty(game, field) for field in ARTWORK_FIELDS},
                        "gamelist": _gamelist_details(game),
                        "has_gamelist_entry": True,
                    }
                )
        missing_items.sort(key=lambda item: (str(item["system"]).lower(), str(item["name"]).lower()))
        return missing_items

    def _list_missing_artwork_from_filesystem(self, include_complete: bool = False) -> List[dict]:
        if not self.roms_root.exists():
            return []
        missing_items: List[dict] = []
        for entry in sorted(self.roms_root.iterdir(), key=lambda p: p.name.lower()):
            if not (entry.is_dir() or entry.is_symlink()):
                continue
            if not self.should_include_system(entry.name):
                continue
            system = entry.name
            system_dir = entry.resolve()
            if not system_dir.exists() or not system_dir.is_dir():
                continue
            try:
                _, roms = self.list_assets(system, "roms")
                _, root = self._read_gamelist(system_dir)
            except Exception:
                continue
            for rom in roms:
                rom_file = str(rom.get("rom_file") or rom.get("name") or "")
                game = self._find_gamelist_entry(root, rom_file, str(rom.get("image_stem") or rom.get("name") or ""))
                missing = self._entry_missing_artwork(game)
                if not missing and not include_complete:
                    continue
                missing_items.append(
                    {
                        "system": system,
                        "name": rom.get("name"),
                        "rom_name": rom_file,
                        "rom_path": rom_file,
                        "title": _text_or_empty(game, "name") if game is not None else str(rom.get("image_stem") or rom.get("name") or ""),
                        "search_title": _clean_rom_title(_text_or_empty(game, "name") if game is not None else str(rom.get("image_stem") or rom.get("name") or "")),
                        "unique_id": rom.get("unique_id"),
                        "missing": missing,
                        "existing": {field: _text_or_empty(game, field) if game is not None else "" for field in ARTWORK_FIELDS},
                        "gamelist": _gamelist_details(game),
                        "has_gamelist_entry": game is not None,
                    }
                )
        missing_items.sort(key=lambda item: (str(item["system"]).lower(), str(item["name"]).lower()))
        return missing_items

    def list_missing_artwork(self, include_filesystem: bool = False, force_refresh: bool = False, include_complete: bool = False) -> List[dict]:
        cache_key = f"{'filesystem' if include_filesystem else 'gamelist'}:{'all' if include_complete else 'missing'}"
        now = time.time()
        with self._missing_artwork_cache_lock:
            cached = self._missing_artwork_cache.get(cache_key)
            if cached and not force_refresh and cached.get("expires_at", 0) > now:
                return [dict(item) for item in cached.get("items", [])]

        items = (
            self._list_missing_artwork_from_filesystem(include_complete=include_complete)
            if include_filesystem
            else self._list_missing_artwork_from_gamelists(include_complete=include_complete)
        )
        with self._missing_artwork_cache_lock:
            self._missing_artwork_cache[cache_key] = {
                "items": [dict(item) for item in items],
                "expires_at": time.time() + 120,
            }
        return items

    def remove_gamelist_entry(self, system: str, rom_path: str) -> dict:
        system_dir = self.get_system_dir(system)
        normalized_rom_path = _normalize_gamelist_rom_path(rom_path)
        if not normalized_rom_path:
            raise ValueError("rom_path is required")
        tree, root = self._read_gamelist(system_dir)
        game = self._find_gamelist_entry_by_path(root, normalized_rom_path)
        if game is None:
            raise FileNotFoundError()
        root.remove(game)
        gamelist_path = system_dir / "gamelist.xml"
        try:
            ET.indent(tree, space="  ")
        except Exception:
            pass
        tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
        with gamelist_path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        with self._missing_artwork_cache_lock:
            self._missing_artwork_cache.clear()
        return {"system": system, "rom_path": normalized_rom_path, "removed": True}

    def remove_gamelist_entries(self, entries: List[dict]) -> dict:
        grouped: Dict[str, List[str]] = {}
        for entry in entries:
            system = str(entry.get("system") or "").strip()
            rom_path = _normalize_gamelist_rom_path(str(entry.get("rom_path") or ""))
            if not system or not rom_path:
                continue
            grouped.setdefault(system, []).append(rom_path)

        removed = []
        not_found = []
        failed = []
        for system, paths in grouped.items():
            try:
                system_dir = self.get_system_dir(system)
                tree, root = self._read_gamelist(system_dir)
            except Exception as error:
                for rom_path in paths:
                    failed.append({"system": system, "rom_path": rom_path, "error": str(error)})
                continue
            changed = False
            pending_removed = []
            for rom_path in paths:
                game = self._find_gamelist_entry_by_path(root, rom_path)
                if game is None:
                    not_found.append({"system": system, "rom_path": rom_path})
                    continue
                root.remove(game)
                pending_removed.append({"system": system, "rom_path": rom_path})
                changed = True
            if changed:
                gamelist_path = system_dir / "gamelist.xml"
                try:
                    ET.indent(tree, space="  ")
                except Exception:
                    pass
                try:
                    tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
                    with gamelist_path.open("a", encoding="utf-8") as handle:
                        handle.write("\n")
                    removed.extend(pending_removed)
                except Exception as error:
                    for item in pending_removed:
                        failed.append({**item, "path": str(gamelist_path), "error": str(error)})

        if removed:
            with self._missing_artwork_cache_lock:
                self._missing_artwork_cache.clear()
        return {"removed": removed, "removed_count": len(removed), "not_found": not_found, "failed": failed, "failed_count": len(failed)}

    def update_gamelist_entry(self, system: str, rom_path: str, fields: dict) -> dict:
        system_dir = self.get_system_dir(system)
        normalized_rom_path = _normalize_gamelist_rom_path(rom_path)
        if not normalized_rom_path:
            raise ValueError("rom_path is required")
        if not isinstance(fields, dict):
            raise ValueError("fields must be an object")
        tree, root = self._read_gamelist(system_dir)
        game = self._find_gamelist_entry_by_path(root, normalized_rom_path)
        created = False
        if game is None:
            game = ET.SubElement(root, "game")
            _set_child_text(game, "path", f"./{normalized_rom_path}")
            created = True

        updated = {}
        removed = []
        for raw_tag, raw_value in fields.items():
            tag = str(raw_tag or "").strip()
            if not tag or tag == "path":
                continue
            if not re.match(r"^[A-Za-z0-9_.-]+$", tag):
                raise ValueError(f"invalid gamelist field: {tag}")
            value = str(raw_value if raw_value is not None else "").strip()
            if value:
                _set_child_text(game, tag, value)
                updated[tag] = value
            else:
                _remove_child(game, tag)
                removed.append(tag)

        gamelist_path = system_dir / "gamelist.xml"
        try:
            ET.indent(tree, space="  ")
        except Exception:
            pass
        tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
        with gamelist_path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        with self._missing_artwork_cache_lock:
            self._missing_artwork_cache.clear()

        return {
            "system": system,
            "rom_path": normalized_rom_path,
            "created": created,
            "updated": updated,
            "removed": removed,
            "title": _text_or_empty(game, "name") or Path(normalized_rom_path).stem,
            "search_title": _clean_rom_title(_text_or_empty(game, "name") or Path(normalized_rom_path).stem),
            "missing": self._entry_missing_artwork(game),
            "existing": {field: _text_or_empty(game, field) for field in ARTWORK_FIELDS},
            "gamelist": _gamelist_details(game),
            "has_gamelist_entry": True,
        }

    def find_rom_by_unique_id(self, system: str, unique_id: str) -> dict:
        _, roms = self.list_assets(system, "roms")
        for rom in roms:
            if str(rom.get("unique_id")) == str(unique_id):
                return rom
        raise FileNotFoundError()

    def find_rom_by_path(self, system: str, rom_path: str) -> dict:
        system_dir = self.get_system_dir(system)
        normalized_path = _normalize_gamelist_rom_path(rom_path)
        if not normalized_path or "\x00" in normalized_path:
            raise ValueError("invalid rom_path")
        target_path = (system_dir / normalized_path).resolve()
        if target_path != system_dir and system_dir not in target_path.parents:
            raise ValueError("rom_path is outside system directory")
        if not target_path.exists():
            raise FileNotFoundError()
        name = target_path.stem if target_path.is_file() else target_path.name
        return {
            "unique_id": "",
            "name": name,
            "rom_file": target_path.name,
            "rom_path": normalized_path,
            "image_stem": name,
            "entry_type": "folder" if target_path.is_dir() else "file",
            "is_downloadable": target_path.is_file(),
        }

    def apply_remote_artwork(
        self,
        system: str,
        unique_id: str,
        rom_path: Optional[str],
        field: str,
        image_data: bytes,
        content_type: str,
        source_url: str,
        source_label: str = "remote",
    ) -> dict:
        if field not in ARTWORK_FIELDS:
            raise ValueError("invalid artwork field")
        system_dir = self.get_system_dir(system)
        rom = self.find_rom_by_path(system, rom_path) if rom_path else self.find_rom_by_unique_id(system, unique_id)
        tree, root = self._read_gamelist(system_dir)
        rom_name = str(rom.get("rom_file") or rom.get("rom_name") or rom.get("name") or "")
        normalized_rom_path = str(rom.get("rom_path") or rom_path or rom_name)
        display_name = str(rom.get("image_stem") or rom.get("name") or Path(normalized_rom_path).stem)
        game = self._find_gamelist_entry_by_path(root, normalized_rom_path) or self._find_gamelist_entry(root, rom_name, display_name)
        if game is None:
            game = ET.SubElement(root, "game")
            _set_child_text(game, "path", f"./{normalized_rom_path}")
            _set_child_text(game, "name", _clean_rom_title(display_name))

        parsed_suffix = Path(urlparse(source_url).path).suffix.lower()
        if parsed_suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            parsed_suffix = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/webp": ".webp",
                "image/gif": ".gif",
            }.get(content_type, ".jpg")
        images_dir = system_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(display_name).stem).strip("-") or "rom"
        safe_label = re.sub(r"[^a-zA-Z0-9._-]+", "-", source_label).strip("-") or "remote"
        target_path = images_dir / f"{safe_stem}-{safe_label}-{field}{parsed_suffix}"
        target_path.write_bytes(image_data)
        relative_path = _relative_artwork_path(system_dir, target_path)
        _set_child_text(game, field, relative_path)

        gamelist_path = system_dir / "gamelist.xml"
        try:
            ET.indent(tree, space="  ")
        except Exception:
            pass
        tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
        with gamelist_path.open("a", encoding="utf-8") as handle:
            handle.write("\n")
        with self._missing_artwork_cache_lock:
            self._missing_artwork_cache.clear()

        return {
            "system": system,
            "unique_id": unique_id,
            "rom_name": rom_name or display_name,
            "rom_path": normalized_rom_path,
            "updated": [{"field": field, "path": relative_path, "source_url": source_url}],
            "missing": self._entry_missing_artwork(game),
            "existing": {item: _text_or_empty(game, item) for item in ARTWORK_FIELDS},
            "gamelist": _gamelist_details(game),
            "has_gamelist_entry": True,
        }

    def apply_launchbox_artwork(
        self,
        system: str,
        unique_id: str,
        game_key: str,
        client: LaunchBoxClient,
        rom_path: Optional[str] = None,
        override_existing: bool = False,
        import_metadata: bool = False,
    ) -> dict:
        system_dir = self.get_system_dir(system)
        rom = self.find_rom_by_path(system, rom_path) if rom_path else self.find_rom_by_unique_id(system, unique_id)
        tree, root = self._read_gamelist(system_dir)
        rom_name = str(rom.get("rom_file") or rom.get("rom_name") or rom.get("name") or "")
        normalized_rom_path = str(rom.get("rom_path") or rom_name)
        display_name = str(rom.get("image_stem") or rom.get("name") or "")
        game = self._find_gamelist_entry_by_path(root, normalized_rom_path) or self._find_gamelist_entry(root, rom_name, display_name)
        if game is None:
            game = ET.SubElement(root, "game")
            _set_child_text(game, "path", f"./{normalized_rom_path}")
            _set_child_text(game, "name", _clean_rom_title(display_name))

        if override_existing:
            fields_to_fetch = list(ARTWORK_FIELDS)
        else:
            fields_to_fetch = self._entry_missing_artwork(game)
        if not fields_to_fetch and not import_metadata:
            return {"system": system, "unique_id": unique_id, "updated": [], "skipped": list(ARTWORK_FIELDS)}

        details = client.details(game_key)
        images_dir = system_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(display_name).stem).strip("-") or "rom"
        updated = []
        skipped = []
        used_source_urls = {
            _artwork_identity(_text_or_empty(game, field))
            for field in ARTWORK_FIELDS
            if _text_or_empty(game, field)
        }

        # Import metadata if requested
        if import_metadata and details:
            meta_fields_map = {
                "name": details.get("name"),
                "desc": details.get("overview"),
                "releasedate": details.get("release_date"),
                "genre": details.get("genre"),
                "developer": details.get("developer"),
                "publisher": details.get("publisher"),
                "players": details.get("players"),
                "rating": details.get("rating"),
            }
            for mfield, mvalue in meta_fields_map.items():
                if mvalue and (override_existing or not _text_or_empty(game, mfield)):
                    _set_child_text(game, mfield, str(mvalue))
                    updated.append({"field": mfield, "value": str(mvalue), "source": "launchbox_metadata"})

        for field in fields_to_fetch:
            selected = client.choose_image_for_field(details, field)
            if not selected:
                skipped.append(field)
                continue
            source_url = str(selected.get("url") or "")
            source_identity = _artwork_identity(source_url)
            if source_identity and source_identity in used_source_urls:
                skipped.append(field)
                continue
            try:
                data, content_type = client.download_image(source_url)
            except Exception:
                skipped.append(field)
                continue
            source_suffix = Path(str(selected.get("file_name") or "")).suffix.lower()
            if source_suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                source_suffix = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                }.get(content_type, ".jpg")
            target_path = images_dir / f"{safe_stem}-launchbox-{field}{source_suffix}"
            target_path.write_bytes(data)
            relative_path = _relative_artwork_path(system_dir, target_path)
            _set_child_text(game, field, relative_path)
            used_source_urls.update({source_identity, _artwork_identity(relative_path)})
            updated.append(
                {
                    "field": field,
                    "path": relative_path,
                    "source_url": source_url,
                    "type": selected.get("type"),
                    "region": selected.get("region"),
                }
            )

        if updated:
            gamelist_path = system_dir / "gamelist.xml"
            try:
                ET.indent(tree, space="  ")
            except Exception:
                pass
            tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
            with gamelist_path.open("a", encoding="utf-8") as handle:
                handle.write("\n")

        return {
            "system": system,
            "unique_id": unique_id,
            "rom_name": rom_name,
            "rom_path": normalized_rom_path,
            "launchbox": {
                "game_key": details.get("game_key"),
                "name": details.get("name"),
                "platform": details.get("platform"),
            },
            "updated": updated,
            "skipped": skipped,
            "missing": self._entry_missing_artwork(game),
            "existing": {item: _text_or_empty(game, item) for item in ARTWORK_FIELDS},
            "gamelist": _gamelist_details(game),
            "has_gamelist_entry": True,
        }

    def apply_thegamesdb_artwork(
        self,
        system: str,
        unique_id: str,
        game_id: str,
        scraper: TheGamesDBScraper,
        rom_path: Optional[str] = None,
        override_existing: bool = False,
        import_metadata: bool = True,
    ) -> dict:
        system_dir = self.get_system_dir(system)
        rom = self.find_rom_by_path(system, rom_path) if rom_path else self.find_rom_by_unique_id(system, unique_id)
        tree, root = self._read_gamelist(system_dir)
        rom_name = str(rom.get("rom_file") or rom.get("rom_name") or rom.get("name") or "")
        normalized_rom_path = str(rom.get("rom_path") or rom_path or rom_name)
        display_name = str(rom.get("image_stem") or rom.get("name") or Path(normalized_rom_path).stem)
        game = self._find_gamelist_entry_by_path(root, normalized_rom_path) or self._find_gamelist_entry(root, rom_name, display_name)
        if game is None:
            game = ET.SubElement(root, "game")
            _set_child_text(game, "path", f"./{normalized_rom_path}")
            _set_child_text(game, "name", _clean_rom_title(display_name))

        fields_to_fetch = list(ARTWORK_FIELDS) if override_existing else self._entry_missing_artwork(game)
        details = scraper.details(game_id)
        images_dir = system_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(display_name).stem).strip("-") or "rom"
        updated = []
        skipped = []

        if import_metadata and details:
            meta_fields_map = {
                "name": details.get("name"),
                "desc": details.get("overview"),
                "releasedate": details.get("release_date"),
                "genre": details.get("genre"),
                "developer": details.get("developer"),
                "publisher": details.get("publisher"),
                "players": details.get("players"),
                "rating": details.get("rating"),
            }
            for mfield, mvalue in meta_fields_map.items():
                if not mvalue:
                    continue
                existing_value = _text_or_empty(game, mfield)
                if override_existing or not existing_value:
                    _set_child_text(game, mfield, str(mvalue))
                    updated.append({"field": mfield, "value": str(mvalue), "source": "thegamesdb_metadata"})

        for field in fields_to_fetch:
            selected = scraper.choose_image_for_field(details, field)
            if not selected:
                skipped.append(field)
                continue
            source_url = str(selected.get("url") or selected.get("image_url") or "")
            try:
                data, content_type = scraper.download_image(source_url)
            except Exception:
                skipped.append(field)
                continue
            source_suffix = Path(str(selected.get("file_name") or urlparse(source_url).path)).suffix.lower()
            if source_suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                source_suffix = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                }.get(content_type, ".jpg")
            target_path = images_dir / f"{safe_stem}-thegamesdb-{field}{source_suffix}"
            target_path.write_bytes(data)
            relative_path = _relative_artwork_path(system_dir, target_path)
            _set_child_text(game, field, relative_path)
            updated.append(
                {
                    "field": field,
                    "path": relative_path,
                    "source_url": source_url,
                    "type": selected.get("type"),
                }
            )

        if updated:
            gamelist_path = system_dir / "gamelist.xml"
            try:
                ET.indent(tree, space="  ")
            except Exception:
                pass
            tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
            with gamelist_path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
            with self._missing_artwork_cache_lock:
                self._missing_artwork_cache.clear()

        return {
            "system": system,
            "unique_id": unique_id,
            "rom_name": rom_name or display_name,
            "rom_path": normalized_rom_path,
            "thegamesdb": {
                "game_id": details.get("game_id"),
                "name": details.get("name"),
            },
            "updated": updated,
            "skipped": skipped,
            "missing": self._entry_missing_artwork(game),
            "existing": {item: _text_or_empty(game, item) for item in ARTWORK_FIELDS},
            "gamelist": _gamelist_details(game),
            "has_gamelist_entry": True,
        }

    def apply_mobygames_artwork(
        self,
        system: str,
        unique_id: str,
        game_id: str,
        client: MobyGamesClient,
        rom_path: Optional[str] = None,
        override_existing: bool = False,
        import_metadata: bool = True,
    ) -> dict:
        system_dir = self.get_system_dir(system)
        rom = self.find_rom_by_path(system, rom_path) if rom_path else self.find_rom_by_unique_id(system, unique_id)
        tree, root = self._read_gamelist(system_dir)
        rom_name = str(rom.get("rom_file") or rom.get("rom_name") or rom.get("name") or "")
        normalized_rom_path = str(rom.get("rom_path") or rom_path or rom_name)
        display_name = str(rom.get("image_stem") or rom.get("name") or Path(normalized_rom_path).stem)
        game = self._find_gamelist_entry_by_path(root, normalized_rom_path) or self._find_gamelist_entry(root, rom_name, display_name)
        if game is None:
            game = ET.SubElement(root, "game")
            _set_child_text(game, "path", f"./{normalized_rom_path}")
            _set_child_text(game, "name", _clean_rom_title(display_name))

        fields_to_fetch = list(ARTWORK_FIELDS) if override_existing else self._entry_missing_artwork(game)
        details = client.details(game_id, system=system)
        images_dir = system_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(display_name).stem).strip("-") or "rom"
        updated = []
        skipped = []

        if import_metadata and details:
            meta_fields_map = {
                "name": details.get("name"),
                "desc": details.get("overview"),
                "releasedate": details.get("release_date"),
                "genre": details.get("genre"),
                "developer": details.get("developer"),
                "publisher": details.get("publisher"),
            }
            for mfield, mvalue in meta_fields_map.items():
                if not mvalue:
                    continue
                existing_value = _text_or_empty(game, mfield)
                if override_existing or not existing_value:
                    _set_child_text(game, mfield, str(mvalue))
                    updated.append({"field": mfield, "value": str(mvalue), "source": "mobygames_metadata"})

        for field in fields_to_fetch:
            selected = client.choose_image_for_field(details, field)
            if not selected:
                skipped.append(field)
                continue
            source_url = str(selected.get("url") or selected.get("image_url") or "")
            try:
                data, content_type = client.download_image(source_url)
            except Exception:
                skipped.append(field)
                continue
            source_suffix = Path(str(selected.get("file_name") or urlparse(source_url).path)).suffix.lower()
            if source_suffix not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                source_suffix = {
                    "image/png": ".png",
                    "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                    "image/gif": ".gif",
                }.get(content_type, ".jpg")
            target_path = images_dir / f"{safe_stem}-mobygames-{field}{source_suffix}"
            target_path.write_bytes(data)
            relative_path = _relative_artwork_path(system_dir, target_path)
            _set_child_text(game, field, relative_path)
            updated.append(
                {
                    "field": field,
                    "path": relative_path,
                    "source_url": source_url,
                    "type": selected.get("type"),
                }
            )

        if updated:
            gamelist_path = system_dir / "gamelist.xml"
            try:
                ET.indent(tree, space="  ")
            except Exception:
                pass
            tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
            with gamelist_path.open("a", encoding="utf-8") as handle:
                handle.write("\n")
            with self._missing_artwork_cache_lock:
                self._missing_artwork_cache.clear()

        return {
            "system": system,
            "unique_id": unique_id,
            "rom_name": rom_name or display_name,
            "rom_path": normalized_rom_path,
            "mobygames": {
                "game_id": details.get("game_id"),
                "name": details.get("name"),
            },
            "updated": updated,
            "skipped": skipped,
            "missing": self._entry_missing_artwork(game),
            "existing": {item: _text_or_empty(game, item) for item in ARTWORK_FIELDS},
            "gamelist": _gamelist_details(game),
            "has_gamelist_entry": True,
        }

    def list_assets(self, system: str, asset_type: str, include_fingerprint: bool = True) -> Tuple[Path, List[dict]]:
        system_dir = self.get_system_dir(system)

        if asset_type == "roms":
            asset_dir = system_dir
        elif asset_type == "images":
            asset_dir = system_dir / "images"
        elif asset_type == "videos":
            asset_dir = system_dir / "videos"
        else:
            raise ValueError("invalid asset type")

        items = []
        # Fast path: query just this system's rows from SQLite (indexed by system),
        # instead of materializing the entire library snapshot in memory. Only used
        # once the cache is authoritative; otherwise we fall through to the filesystem.
        if asset_type == "roms" and self.settings is not None and rom_cache_ready(self.settings):
            rows = list_rom_rows_by_system(self.settings, system, include_fingerprint=include_fingerprint)
            if rows is not None:
                items = []
                for rom in rows:
                    relative_path = str(rom.get("file_path") or rom.get("rom_name") or "")
                    row = {
                        "unique_id": rom.get("unique_id") or hashlib.sha256(f"{system}:{relative_path}".encode("utf-8")).hexdigest()[:16],
                        "name": rom.get("rom_name") or Path(relative_path).name,
                        "rom_file": Path(relative_path).name,
                        "filename": Path(relative_path).name,
                        "relative_path": relative_path,
                        "rom_path": relative_path,
                        "file_path": relative_path,
                        "byte_count": rom.get("file_size"),
                        "entry_type": rom.get("entry_type") or "file",
                        "is_downloadable": rom.get("is_downloadable", True),
                        "image_stem": rom.get("image_stem") or Path(relative_path).stem,
                    }
                    if include_fingerprint:
                        row["fingerprint"] = rom.get("fingerprint")
                        row["rom_fingerprint"] = row["fingerprint"]
                    items.append(row)
                return system_dir, self._attach_gamelist_to_rom_items(system_dir, items)
        if asset_dir.exists() and asset_dir.is_dir():
            if asset_type == "roms":
                items = self._list_rom_items(system, asset_dir, include_fingerprint=include_fingerprint)
                items = self._attach_gamelist_to_rom_items(system_dir, items)
            else:
                for entry in self.iter_files(asset_dir):
                    stat = entry.stat()
                    items.append(
                        {
                            "unique_id": self.build_unique_id(entry),
                            "name": entry.name,
                            "byte_count": stat.st_size,
                            "entry_type": "file",
                            "is_downloadable": True,
                        }
                    )

        return asset_dir, items

    def _cached_asset_snapshot(self) -> Optional[dict]:
        try:
            cache, rebuilt = _load_rom_metadata_cache(self.settings)
        except Exception:
            return None
        if rebuilt or not cache.get("last_full_scan_at") or cache.get("scan_in_progress"):
            return None
        if not isinstance(cache.get("systems"), list):
            return None
        return _build_rom_metadata_snapshot_from_cache(self.settings, cache)

    def get_bios_root(self) -> Path:
        if not self.bios_root.exists() or not self.bios_root.is_dir():
            raise FileNotFoundError()
        return self.bios_root.resolve()

    def list_bios_entries(self) -> List[dict]:
        bios_root = self.get_bios_root()
        files: List[Tuple[Path, int]] = []
        allowed_extensions = {
            ".bin",
            ".rom",
            ".zip",
            ".img",
            ".keys",
            ".pup",
            ".gg",
            ".sms",
            ".pce",
            ".col",
            ".min",
            ".qcow2",
            ".nand",
            ".dat",
            ".iso",
            ".chd",
            ".7z",
        }

        for current_root, dirs, file_names in os.walk(bios_root):
            root_path = Path(current_root)

            for file_name in file_names:
                file_path = (root_path / file_name).resolve()
                if not file_path.is_file():
                    continue
                if not (file_path == bios_root or bios_root in file_path.parents):
                    continue
                if file_path.suffix.lower() not in allowed_extensions:
                    continue

                size = file_path.stat().st_size
                files.append((file_path, size))

        entries: List[dict] = []

        for file_path, size in sorted(files, key=lambda item: str(item[0].relative_to(bios_root)).lower()):
            relative_path = file_path.relative_to(bios_root).as_posix()
            # BIOS uses a full-file MD5 (exact emulator identity), not the sampled fingerprint.
            bios_md5 = self.build_md5(file_path)
            entries.append(
                {
                    "entry_type": "file",
                    "name": file_path.name,
                    "path": relative_path,
                    "unique_id": self.build_unique_id(file_path),
                    "byte_count": size,
                    "md5": bios_md5,
                    "bios_md5": bios_md5,
                }
            )

        return entries

    def find_bios_file_by_unique_id(self, unique_id: str) -> Path:
        unique_id = valid_segment(unique_id)
        bios_root = self.get_bios_root()

        for current_root, _, file_names in os.walk(bios_root):
            root_path = Path(current_root)
            for file_name in file_names:
                file_path = (root_path / file_name).resolve()
                if not file_path.is_file():
                    continue
                if self.build_unique_id(file_path) == unique_id:
                    return file_path

        raise FileNotFoundError()

OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "Drone App",
        "version": _drone_app_version(),
        "description": "Browse and download ROM, image, video, and BIOS assets. Peer API routes can require mTLS. For manual health testing use a client certificate/key with curl, for example: curl --cert client.crt --key client.key -k https://drone-host/health. The admin API page exposes certificate metadata and the public certificate only; private key material must stay on the Drone.",
    },
    "servers": [{"url": API_PREFIX}],
    "components": {
        "securitySchemes": {
            "basicAuth": {
                "type": "http",
                "scheme": "basic",
            }
        }
    },
    "security": [{"basicAuth": []}],
    "paths": {
        "/": {"get": {"summary": "Root UI", "responses": {"200": {"description": "HTML UI"}}}},
        "/systems": {
            "get": {
                "summary": "List systems",
                "responses": {"200": {"description": "Systems list"}},
            }
        },
        "/systems/{system}": {
            "get": {
                "summary": "List ROMs for a system",
                "parameters": [{"name": "system", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ROM list"}},
            }
        },
        "/systems/{system}/roms/{unique_id}": {
            "get": {
                "summary": "Download ROM by unique ID",
                "parameters": [
                    {"name": "system", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "unique_id", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "ROM file stream"}},
            }
        },
        "/systems/{system}/{unique_id}": {
            "get": {
                "summary": "Download ROM by unique ID (legacy route)",
                "parameters": [
                    {"name": "system", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "unique_id", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "ROM file stream"}},
            }
        },
        "/systems/{system}/images": {
            "get": {
                "summary": "List images for a system",
                "parameters": [{"name": "system", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "Image list"}},
            }
        },
        "/systems/{system}/images/{image_ref}": {
            "get": {
                "summary": "Get image or download image asset by reference",
                "parameters": [
                    {"name": "system", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "image_ref", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "Image bytes or attachment"}},
            }
        },
        "/public/systems/{system}/images/{image_file}": {
            "get": {
                "summary": "Public image endpoint (no auth)",
                "security": [],
                "parameters": [
                    {"name": "system", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "image_file", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "Image bytes"}},
            }
        },
        "/systems/{system}/videos": {
            "get": {
                "summary": "List videos for a system",
                "parameters": [{"name": "system", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "Video list"}},
            }
        },
        "/systems/{system}/videos/{unique_id}": {
            "get": {
                "summary": "Download video by unique ID",
                "parameters": [
                    {"name": "system", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "unique_id", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "Video file stream"}},
            }
        },
        "/bios": {
            "get": {
                "summary": "List BIOS entries (paged + searchable)",
                "parameters": [
                    {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "default": 100}},
                    {"name": "offset", "in": "query", "required": False, "schema": {"type": "integer", "default": 0}},
                    {"name": "q", "in": "query", "required": False, "schema": {"type": "string"}},
                    {
                        "name": "systems",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": "Comma-separated list of system filter values (example: snes,ps2,_root)",
                    },
                ],
                "responses": {"200": {"description": "Paged BIOS list"}},
            }
        },
        "/bios/{unique_id}": {
            "get": {
                "summary": "Download BIOS file by unique ID",
                "parameters": [{"name": "unique_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "BIOS file stream"}},
            }
        },
        "/openapi.json": {"get": {"summary": "OpenAPI spec", "responses": {"200": {"description": "OpenAPI JSON"}}}},
        "/swagger": {"get": {"summary": "Swagger UI", "responses": {"200": {"description": "Swagger HTML"}}}},
        "/downloads": {
            "get": {
                "summary": "HTML sitemap of downloadable ROM links grouped by system",
                "responses": {"200": {"description": "Download sitemap HTML"}},
            }
        },
        "/search": {
            "get": {
                "summary": "Search ROMs across all systems",
                "parameters": [
                    {"name": "q", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "system", "in": "query", "required": False, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "Search results"}},
            }
        },
        "/theme/meta": {
            "get": {
                "summary": "Detected Batocera theme metadata and resolved asset URLs",
                "responses": {"200": {"description": "Theme metadata"}},
            }
        },
        "/theme/assets/{path}": {
            "get": {
                "summary": "Serve asset from detected Batocera theme directory",
                "parameters": [{"name": "path", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "Theme asset bytes"}},
            }
        },
        "/theme/system/{system}": {
            "get": {
                "summary": "Resolved theme metadata for a specific system folder",
                "parameters": [{"name": "system", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "System theme metadata"}},
            }
        },
        "/theme/backgrounds": {
            "get": {
                "summary": "List candidate background images from active Batocera theme",
                "responses": {"200": {"description": "Theme background candidates"}},
            }
        },
        "/theme/logos": {
            "get": {
                "summary": "List candidate logo images from active Batocera theme",
                "responses": {"200": {"description": "Theme logo candidates"}},
            }
        },
        "/theme/images": {
            "get": {
                "summary": "List all image assets from active Batocera theme",
                "parameters": [
                    {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "default": 100}},
                    {"name": "offset", "in": "query", "required": False, "schema": {"type": "integer", "default": 0}},
                    {"name": "q", "in": "query", "required": False, "schema": {"type": "string"}},
                    {"name": "system", "in": "query", "required": False, "schema": {"type": "string"}},
                    {
                        "name": "systems",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": "Comma-separated list of system filter values (example: snes,ps2,_root)",
                    },
                ],
                "responses": {"200": {"description": "Paged theme image catalog"}},
            }
        },
        "/admin/logs/{source}": {
            "get": {
                "summary": "Get logs from Batocera system or emulators",
                "parameters": [
                    {
                        "name": "source",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "enum": ["es_launch_stdout", "es_launch_stderr"]},
                        "description": "Log source (case-insensitive): es_launch_stdout, es_launch_stderr",
                    },
                    {"name": "lines", "in": "query", "required": False, "schema": {"type": "integer", "default": 200, "minimum": 1, "maximum": 5000}, "description": "Number of lines to return from the end of the log"},
                ],
                "responses": {
                    "200": {"description": "Log content"},
                    "404": {"description": "Log source not found or log file doesn't exist"}
                },
            }
        },
        "/admin/system-info": {
            "get": {
                "summary": "Get Batocera system information via batocera-info",
                "responses": {
                    "200": {"description": "Structured system information"},
                    "500": {"description": "Failed to execute batocera-info"},
                },
            }
        },
        "/admin/asset-cache": {
            "get": {
                "summary": "Get ROM, BIOS, and artwork asset cache progress",
                "responses": {"200": {"description": "Asset cache status and pending upload counts"}},
            }
        },
        "/admin/api/status": {
            "get": {
                "summary": "API access, Swagger, and mTLS certificate guidance",
                "responses": {"200": {"description": "API admin status and certificate metadata"}},
            }
        },
        "/admin/api/certificate": {
            "get": {
                "summary": "Download Drone public certificate",
                "description": "Downloads the public certificate only. Private key material is not exposed.",
                "responses": {"200": {"description": "Public certificate PEM"}},
            }
        },
        "/admin/artwork/missing": {
            "get": {
                "summary": "List ROMs for the artwork and metadata hub",
                "responses": {"200": {"description": "Missing artwork report"}},
            }
        },
        "/admin/artwork/launchbox/search": {
            "get": {
                "summary": "Search LaunchBox Games Database for artwork candidates",
                "parameters": [
                    {"name": "system", "in": "query", "required": False, "schema": {"type": "string"}},
                    {"name": "rom_id", "in": "query", "required": False, "schema": {"type": "string"}},
                    {"name": "q", "in": "query", "required": False, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "LaunchBox search matches"}},
            }
        },
        "/admin/artwork/launchbox/apply": {
            "post": {
                "summary": "Download selected LaunchBox artwork and update only missing gamelist.xml fields",
                "responses": {"200": {"description": "Artwork update result"}},
            }
        },
        "/admin/configs/{source}": {
            "get": {
                "summary": "Get important configuration file content for debugging",
                "parameters": [
                    {"name": "source", "in": "path", "required": True, "schema": {"type": "string"}, "description": "Config source key (batocera, emulationstation, retroarch, ... )"},
                    {"name": "max_bytes", "in": "query", "required": False, "schema": {"type": "integer", "default": 131072, "minimum": 1024, "maximum": 1048576}, "description": "Maximum bytes returned from end of file"},
                    {"name": "format", "in": "query", "required": False, "schema": {"type": "string", "enum": ["json", "xml"], "default": "json"}, "description": "Only used for source=es_systems. json returns parsed merged systems; xml returns on-disk XML content."},
                ],
                "responses": {
                    "200": {"description": "Config file content"},
                    "404": {"description": "Config source/path not found"},
                },
            }
        },
        "/admin/configs/sources": {
            "get": {
                "summary": "List config source keys available on this host",
                "responses": {
                    "200": {"description": "Detected config sources"},
                },
            }
        },
        "/admin/emulators": {
            "get": {
                "summary": "List emulator config files selected for Overmind reporting",
                "responses": {
                    "200": {"description": "Emulator config files and content"},
                },
            }
        },
        "/admin/emulators/file": {
            "get": {
                "summary": "Read one emulator config file selected for Overmind reporting",
                "parameters": [
                    {"name": "root", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "relative_path", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "max_bytes", "in": "query", "required": False, "schema": {"type": "integer", "default": 131072, "minimum": 1024, "maximum": 1048576}},
                ],
                "responses": {
                    "200": {"description": "Emulator config file content"},
                    "404": {"description": "Config file not found"},
                },
            }
        },
    },
}

class RomRequestHandler(ApiRoutesMixin, UiRoutesMixin, BaseHTTPRequestHandler):
    server_version = "DroneApp/4.0"
    openapi_spec = OPENAPI_SPEC
    # Per-connection idle timeout (applied to the socket in BaseHTTPRequestHandler.setup).
    # The TLS handshake is now deferred to this worker thread (do_handshake_on_connect=False),
    # so this bounds both the handshake and per-request reads/writes: a stalled or silent
    # client is dropped instead of holding a thread forever. It is a per-operation idle
    # timeout, not a total-transfer cap, so large peer ROM transfers with flowing data are
    # unaffected. Overridable via env for slow networks.
    timeout = max(15, int(os.environ.get("DRONE_REQUEST_TIMEOUT_SECONDS", "120")))

    def __init__(
        self,
        *args,
        settings: Settings,
        auth: BasicAuth,
        repository: RomRepository,
        image_cache: ExpiringLRUCache,
        image_miss_cache: ExpiringKeyCache,
        json_cache: ExpiringLRUCache,
        **kwargs,
    ):
        self.settings = settings
        self.auth = auth
        self.repository = repository
        self.image_cache = image_cache
        self.image_miss_cache = image_miss_cache
        self.json_cache = json_cache
        super().__init__(*args, **kwargs)

    def log_request(self, code="-", size="-") -> None:
        client_ip = self.client_address[0] if self.client_address else "-"
        message = f'{client_ip} - "{self.requestline}" {code} {size}'
        print(message, file=sys.stdout, flush=True)

    def log_error(self, format: str, *args) -> None:
        message = format % args if args else format
        client_ip = self.client_address[0] if self.client_address else "-"
        print(f"{client_ip} - {message}", file=sys.stderr, flush=True)

    def _guess_content_type(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".js":
            return "application/javascript"
        if suffix == ".css":
            return "text/css"
        if suffix == ".svg":
            return "image/svg+xml"
        if suffix == ".png":
            return "image/png"
        if suffix in (".jpg", ".jpeg"):
            return "image/jpeg"
        if suffix == ".webp":
            return "image/webp"
        if suffix == ".gif":
            return "image/gif"
        if suffix == ".woff":
            return "font/woff"
        if suffix == ".woff2":
            return "font/woff2"
        if suffix == ".ttf":
            return "font/ttf"
        if suffix == ".otf":
            return "font/otf"
        if suffix == ".mp4":
            return "video/mp4"
        return "application/octet-stream"

    def _send_unauthorized(self) -> None:
        has_auth_header = bool(self.headers.get("Authorization"))
        if DRONE_LOG_UNAUTHORIZED_REQUESTS or has_auth_header:
            self.log_error(
                '401 unauthorized "%s" auth_header_present=%s',
                self.path.split("?", 1)[0],
                "yes" if has_auth_header else "no",
            )
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Drone App"')
        self.send_header("Content-Type", "application/json")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(json_bytes({"error": "unauthorized"}))
        client_ip = self.client_address[0] if self.client_address else "-"
        record_unauthorized_response(client_ip)

    def _reject_if_ip_blocked(self) -> bool:
        """Reject (403) and log every request from an IP blocked for 401 brute force."""
        client_ip = self.client_address[0] if self.client_address else "-"
        if not is_ip_blocked(client_ip):
            return False
        print(
            f"Blocked request: ip={client_ip} {self.command} {self.path.split('?', 1)[0]}",
            file=sys.stdout,
            flush=True,
        )
        try:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", str(int(DRONE_AUTH_BLOCK_DURATION_SECONDS)))
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(json_bytes({"error": "blocked"}))
        except Exception:
            pass
        return True

    def _send_rate_limited(self) -> None:
        self.log_error('429 rate limited "%s"', self.path.split("?", 1)[0])
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", str(int(DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(json_bytes({"error": "rate_limited"}))

    def _rate_limit_unauthenticated_external_request(self) -> bool:
        if self.auth.check(self.headers.get("Authorization")):
            return False
        try:
            cert = self.connection.getpeercert() if hasattr(self.connection, "getpeercert") else None
        except Exception:
            cert = None
        if cert:
            return False
        client_ip = self.client_address[0] if self.client_address else "-"
        if _unauthenticated_request_allowed(client_ip):
            return False
        self._send_rate_limited()
        return True

    def _send_security_headers(self) -> None:
        image_sources = ["'self'", "data:", "https:"]
        if self.settings.use_fake_data:
            image_sources.append("https:")
            fake_base = (self.settings.fake_image_base_url or "").strip()
            if fake_base:
                parsed = urlparse(fake_base)
                if parsed.scheme and parsed.netloc:
                    image_sources.append(f"{parsed.scheme}://{parsed.netloc}")
                elif fake_base.startswith("https://") or fake_base.startswith("http://"):
                    image_sources.append(fake_base.rstrip("/"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        self.send_header("Cache-Control", "no-store")
        # CSP keeps UI/resource loading strict while still allowing bundled Swagger assets.
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'self'; img-src {' '.join(image_sources)}; style-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
            "font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com; connect-src 'self' https://unpkg.com https://cdn.jsdelivr.net; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def _build_fake_image_url(self, seed: str, width: int = 640, height: int = 360) -> str:
        template = (self.settings.fake_image_base_url or "https://picsum.photos/seed/{seed}/{width}/{height}").strip()
        safe_seed = re.sub(r"[^a-zA-Z0-9._-]+", "-", seed).strip("-") or "image"
        if "{" in template and "}" in template:
            return template.format(seed=quote(safe_seed, safe=""), width=width, height=height)
        base = template.rstrip("/")
        return f"{base}/{quote(safe_seed, safe='')}/{width}/{height}"

    def _redirect_to_fake_image(self, seed: str, width: int = 640, height: int = 360) -> None:
        location = self._build_fake_image_url(seed=seed, width=width, height=height)
        self.send_response(302)
        self.send_header("Location", location)
        self._send_security_headers()
        self.end_headers()

    def _fake_theme_asset_url(self, relative_path: str) -> str:
        lowered = relative_path.lower()
        if lowered.endswith(".svg"):
            return self._build_fake_image_url(seed=f"theme-{relative_path}", width=800, height=450)
        if lowered.endswith(".png"):
            return self._build_fake_image_url(seed=f"theme-{relative_path}", width=800, height=450)
        if lowered.endswith(".jpg") or lowered.endswith(".jpeg") or lowered.endswith(".webp") or lowered.endswith(".gif"):
            return self._build_fake_image_url(seed=f"theme-{relative_path}", width=800, height=450)
        return api_url(f"/theme/assets/{quote(relative_path, safe='/')}")

    def _send_json(self, status_code: int, payload: dict, cache_key: Optional[str] = None) -> None:
        if status_code == 200 and cache_key:
            cached = self.json_cache.get(cache_key)
            if cached is None:
                body = json_bytes(payload)
                self.json_cache.put(cache_key, body)
            else:
                body = cached["data"]
        else:
            body = json_bytes(payload)

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        if status_code == 200 and cache_key:
            self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _handle_admin_downloads(self) -> None:
        manager = _get_download_manager()
        if manager is None:
            self._send_json(200, {"target_drone_id": self.settings.overmind_device_id, "downloads": [], "active": [], "queued": [], "recent": []})
            return
        self._send_json(200, manager.snapshot())

    def _handle_admin_asset_cache(self) -> None:
        self._send_json(200, _rom_metadata_cache_status(self.settings))

    def _handle_admin_asset_cache_purge(self) -> None:
        """Purge cached asset metadata while keeping fingerprint, forcing a clean resync."""
        result = _purge_asset_cache_keep_fingerprint(self.settings)
        _ROM_METADATA_WAKE.set()
        cleared = result.get("cleared") or {}
        roms = int(cleared.get("roms") or 0)
        kept = int(cleared.get("preserved_fingerprint") or 0)
        self._send_json(200, {
            "status": result.get("status", "queued"),
            "kept_fingerprint": True,
            "cleared": cleared,
            "requested_at": result.get("requested_at"),
            "message": (
                f"Asset cache cleared ({roms} ROMs, {int(cleared.get('bios') or 0)} BIOS, "
                f"{int(cleared.get('artwork') or 0)} artwork). Kept {kept} fingerprint hashes — "
                "rebuilding now without re-hashing, then uploading a full inventory."
            ),
        })

    def _handle_admin_asset_cache_clear_pending(self) -> None:
        """Discard pending asset metadata upload changes without clearing cached assets."""
        before = _rom_metadata_cache_status(self.settings).get("pending_changes") or {}
        cleared_total = int(before.get("total") or 0)
        _clear_pending_rom_metadata_changes(self.settings)
        _update_rom_metadata_cache_state(self.settings, dirty=False, full_refresh_pending=False)
        after = _rom_metadata_cache_status(self.settings)
        self._send_json(200, {
            "status": "cleared",
            "cleared": before,
            "pending_changes": after.get("pending_changes") or {},
            "message": f"Cleared {cleared_total:,} pending asset change{'s' if cleared_total != 1 else ''}.",
        })

    def _handle_admin_download_cancel(self, job_id: str) -> None:
        manager = _get_download_manager()
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        result = manager.cancel(job_id, "cancelled from Drone admin")
        status_code = 404 if result.get("status") == "not_found" else 200
        self._send_json(status_code, result)

    def _handle_admin_download_retry(self, job_id: str) -> None:
        manager = _get_download_manager()
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        result = manager.retry(job_id)
        status_code = 404 if result.get("status") == "not_found" else 409 if result.get("status") == "not_retryable" else 200
        self._send_json(status_code, result)

    def _handle_admin_downloads_pause(self) -> None:
        manager = _get_download_manager()
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        self._send_json(200, manager.pause())

    def _handle_admin_downloads_resume(self) -> None:
        manager = _get_download_manager()
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        self._send_json(200, manager.resume())

    def _handle_admin_downloads_clear(self) -> None:
        manager = _get_download_manager()
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        self._send_json(200, manager.clear_queue())

    def _send_html(self, status_code: int, html: str) -> None:
        body = html_bytes(html)
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status_code: int) -> None:
        self.send_response(status_code)
        self.send_header("Content-Length", "0")
        self._send_security_headers()
        self.end_headers()

    def _handle_content_file(self, relative_path: str) -> None:
        content_root = Path(__file__).resolve().parent.parent / "content"
        rel = str(relative_path or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            raise FileNotFoundError()
        target = (content_root / rel).resolve()
        if content_root.resolve() not in target.parents or not target.exists() or not target.is_file():
            raise FileNotFoundError()
        self._stream_file(target, self._guess_content_type(target))

    def _read_json_body(self) -> dict:
        length_value = self.headers.get("Content-Length", "0").strip()
        try:
            length = int(length_value or "0")
        except Exception:
            raise ValueError("invalid content length")
        if length < 0 or length > (256 * 1024):
            raise ValueError("request body too large")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("invalid JSON body")
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _overmind_config_path(self) -> Path:
        return (self.settings.userdata_root / "system" / "drone-app" / "overmind_integration.json").resolve()

    def _overmind_actions_path(self) -> Path:
        return Path(os.environ.get(
            "OVERMIND_ACTION_LOG_FILE",
            str(self.settings.userdata_root / "system" / "drone-app" / "overmind_actions.log"),
        )).resolve()

    def _overmind_swarm_path(self) -> Path:
        return (self.settings.userdata_root / "system" / "drone-app" / "overmind_swarm.json").resolve()

    def _overmind_peer_results_path(self) -> Path:
        return (self.settings.userdata_root / "system" / "drone-app" / "peer_checks.json").resolve()

    def _rom_fingerprint_cache_path(self) -> Path:
        return (self.settings.userdata_root / "system" / "drone-app" / "rom_fingerprint_cache.json").resolve()

    def _mask_secret(self, value: str) -> str:
        if not value:
            return ""
        if len(value) <= 4:
            return "*" * len(value)
        return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"

    def _load_overmind_config(self) -> dict:
        fake_email = FAKE_OVERMIND_EMAIL if self.settings.use_fake_data else ""
        fake_password = FAKE_OVERMIND_PASSWORD if self.settings.use_fake_data else ""
        fake_token = FAKE_OVERMIND_TOKEN if self.settings.use_fake_data else ""
        auth_token = self.settings.overmind_auth_token or ""
        default = {
            "overmind_url": (self.settings.overmind_url or "").strip(),
            "overmind_email": (fake_email if self.settings.use_fake_data else self.settings.overmind_email or "").strip(),
            "drone_name": socket.gethostname(),
            "integration_enabled": False,
            "integration_state": "not_started",
            "requested_at": None,
            "last_started_at": None,
            "last_error": None,
            "notes": "Stub integration until batocera.overmind app is available.",
        }

        if self.settings.overmind_password or fake_password:
            default["overmind_password"] = fake_password if self.settings.use_fake_data else self.settings.overmind_password
        if self.settings.overmind_token or fake_token:
            default["overmind_token"] = fake_token if self.settings.use_fake_data else self.settings.overmind_token
        if auth_token:
            default["overmind_auth_token"] = auth_token

        loaded = self._load_json_file(self._overmind_config_path(), {})
        if not isinstance(loaded, dict) or not loaded:
            return default

        merged = dict(default)
        merged.update(loaded)
        if self.settings.use_fake_data:
            merged["overmind_email"] = FAKE_OVERMIND_EMAIL
            merged["overmind_password"] = FAKE_OVERMIND_PASSWORD
            merged["overmind_token"] = FAKE_OVERMIND_TOKEN
        else:
            _strip_fake_overmind_values(merged)
        return merged

    def _save_overmind_config(self, payload: dict) -> None:
        _save_state_payload(
            _state_database_path(self.settings.userdata_root),
            self._overmind_config_path().name,
            payload,
        )
        self._overmind_config_path().unlink(missing_ok=True)

    def _load_json_file(self, path: Path, fallback):
        return _load_state_payload(
            _state_database_path(self.settings.userdata_root),
            path.name,
            fallback,
            legacy_path=path,
        )

    def _save_json_state(self, path: Path, payload) -> None:
        _save_state_payload(
            _state_database_path(self.settings.userdata_root),
            path.name,
            payload,
        )
        path.unlink(missing_ok=True)

    def _overmind_public_payload(self, config: dict) -> dict:
        config = dict(config)
        _normalize_overmind_link_state(config)
        password = str(config.get("overmind_password") or "")
        auth_token = str(config.get("overmind_auth_token") or "")
        token = str(config.get("overmind_token") or "")
        email = str(config.get("overmind_email") or "")
        state = str(config.get("integration_state") or "not_started")
        connected = bool(token) and bool(config.get("integration_enabled")) and state not in {"pending_failed", "not_started"}
        swarm_status = str(config.get("swarm_connection_status") or "")
        if state == "pending_failed" or not config.get("integration_enabled"):
            swarm_status = "disconnected"
        elif not swarm_status:
            swarm_status = "connected" if connected else ("pending approval" if state == "pending_approval" else "disconnected")
        status = {
            "configured": connected,
            "integration_enabled": bool(config.get("integration_enabled")),
            "integration_state": state,
            "swarm_connection_status": swarm_status,
            "requested_at": config.get("requested_at"),
            "last_started_at": config.get("last_started_at"),
            "last_error": config.get("last_error"),
            "last_onboarding_attempt": config.get("last_onboarding_attempt") if isinstance(config.get("last_onboarding_attempt"), dict) else None,
            "notes": config.get("notes") or "Stub integration until batocera.overmind app is available.",
        }
        return {
            "overmind_url": config.get("overmind_url") or "",
            "overmind_email": email,
            "drone_name": config.get("drone_name") or socket.gethostname(),
            "machine_id": self.settings.overmind_device_id,
            "password_configured": bool(password),
            "password_masked": self._mask_secret(password) if password else "",
            "auth_token_configured": bool(auth_token),
            "auth_token_masked": self._mask_secret(auth_token) if auth_token else "",
            "token_configured": bool(token),
            "token_masked": self._mask_secret(token) if token else "",
            "status": status,
            "swarm": self._load_json_file(self._overmind_swarm_path(), []),
            "peer_checks": self._load_json_file(self._overmind_peer_results_path(), []),
            "certificate": DroneCertificateManager(self.settings).metadata(),
        }

    def _load_processed_overmind_actions(self) -> List[dict]:
        return _load_state_events(
            _state_database_path(self.settings.userdata_root),
            "overmind_actions",
            legacy_path=self._overmind_actions_path(),
        )

    def _handle_admin_overmind_actions(self) -> None:
        self._send_json(200, {"actions": self._load_processed_overmind_actions()})

    def _handle_admin_drone_update(self) -> None:
        result = _download_latest_drone_app(self.settings)
        result["restart"] = {
            "scheduled": True,
            "exit_code": DRONE_SELF_UPDATE_EXIT_CODE,
            "note": "The Drone app process will restart so the downloaded version is loaded. Batocera itself is not restarted.",
        }
        self._send_json(200, result)
        try:
            self.wfile.flush()
        except Exception:
            pass
        _restart_drone_process_soon()

    def _handle_admin_api_status(self) -> None:
        metadata = DroneCertificateManager(self.settings).ensure_certificate()
        self._send_json(
            200,
            {
                "swagger_url": api_url("/swagger"),
                "openapi_url": api_url("/openapi.json"),
                "certificate_download_url": api_url("/admin/api/certificate"),
                "mtls_enabled": self.settings.drone_mtls_enabled,
                "certificate": metadata,
                "guidance": {
                    "curl": "curl --cert /path/to/client.crt --key /path/to/client.key -k https://drone-host/health",
                    "warning": "Do not share Drone private key material. The download endpoint provides the public certificate only.",
                    "lifecycle": f"Drone creates or reuses a local certificate on startup. Default lifetime is {self.settings.drone_cert_days} days; expired certificates are recreated on restart.",
                },
            },
        )

    def _handle_admin_api_certificate(self) -> None:
        metadata = DroneCertificateManager(self.settings).ensure_certificate()
        cert_file = self.settings.drone_cert_file
        if metadata.get("status") != "loaded" or not cert_file.exists():
            raise FileNotFoundError()
        self._stream_file(cert_file, "application/x-pem-file", as_attachment=True)

    def _handle_public_health(self) -> None:
        self._send_json(
            200,
            {
                "status": "ok",
                "drone_id": self.settings.overmind_device_id,
                "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            },
        )

    def _handle_rom_fingerprint(self, system: str, unique_id: str) -> None:
        system_dir = self.repository.get_system_dir(system)
        rom = self.repository.find_rom_by_unique_id(system, unique_id)
        rom_path = str(rom.get("relative_path") or rom.get("rom_path") or rom.get("rom_file") or rom.get("name") or "")
        target = (system_dir / rom_path).resolve()
        if not target.exists() or not target.is_file() or (target != system_dir and system_dir not in target.parents):
            raise FileNotFoundError()
        stat = target.stat()
        cache_path = self._rom_fingerprint_cache_path()
        cache = self._load_json_file(cache_path, {})
        key = f"{system}:{unique_id}:{stat.st_size}:{int(stat.st_mtime)}"
        fingerprint_value = cache.get(key) if isinstance(cache, dict) else None
        if not fingerprint_value:
            fingerprint_value = self.repository.build_fingerprint(target)
            cache = {key: fingerprint_value}
        self._save_json_state(cache_path, cache)
        self._send_json(200, {"system": system, "unique_id": unique_id, "fingerprint": fingerprint_value, "cached": bool(cache.get(key))})

    def _peer_request_authorized(self) -> bool:
        if _local_network.is_local_mode(self.settings):
            if self.settings.http_only:
                if _env_bool(False, "DRONE_LOCAL_ALLOW_INSECURE_HTTP"):
                    return True
                self._send_json(403, {"error": "local-network peer API requires HTTPS and a paired client certificate"})
                return False
            try:
                der = self.connection.getpeercert(binary_form=True) if hasattr(self.connection, "getpeercert") else None
            except Exception:
                der = None
            fingerprint = hashlib.sha256(der).hexdigest() if der else ""
            trusted = {
                str(peer.get("certificate_fingerprint") or "").strip().lower()
                for peer in _local_network.paired_peers(self.settings)
            }
            if fingerprint and fingerprint.lower() in trusted:
                return True
            if not _local_network.is_overmind_mode(self.settings):
                self._send_json(403, {"error": "paired client certificate required"})
                return False
        if self.settings.drone_mtls_enabled:
            cert = self.connection.getpeercert() if hasattr(self.connection, "getpeercert") else None
            if not cert:
                self._send_json(403, {"error": "client certificate required"})
                return False
            try:
                der = self.connection.getpeercert(binary_form=True)
            except Exception:
                der = None
            fingerprint = hashlib.sha256(der).hexdigest().lower() if der else ""
            local_match = next(
                (
                    peer
                    for peer in _local_network.paired_peers(self.settings)
                    if str(peer.get("certificate_fingerprint") or "").strip().lower() == fingerprint
                ),
                None,
            )
            if local_match:
                peer_id = str(local_match.get("drone_id") or "")
                approved_path = _peer_cert_cache_path(self.settings, peer_id)
                try:
                    independently_approved = (
                        approved_path.exists()
                        and _certificate_pem_fingerprint(approved_path.read_text(encoding="utf-8", errors="ignore")).lower() == fingerprint
                    )
                except Exception:
                    independently_approved = False
                if not independently_approved:
                    self._send_json(403, {"error": "Local Network pairing trust is inactive in Overmind mode"})
                    return False
        return True

    def _handle_peer_pair(self, payload: dict) -> None:
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Drone is not in local network mode"})
            return
        if not _local_network.validate_pairing_code(self.settings, str(payload.get("pairing_code") or "")):
            client_ip = self.client_address[0] if self.client_address else "-"
            record_unauthorized_response(client_ip)
            self._send_json(403, {"error": "invalid or expired pairing code"})
            return
        peer_id = str(payload.get("drone_id") or "").strip()
        certificate_pem = str(payload.get("certificate_pem") or "")
        if not peer_id or peer_id == self.settings.overmind_device_id:
            raise ValueError("invalid peer id")
        cert_path, fingerprint = _save_local_peer_certificate(self.settings, peer_id, certificate_pem)
        expected = str(payload.get("certificate_fingerprint") or "").strip().lower()
        if expected and expected != fingerprint.lower():
            cert_path.unlink(missing_ok=True)
            raise ValueError("peer certificate fingerprint mismatch")
        source_ip = self.client_address[0] if self.client_address else ""
        scheme = str(payload.get("scheme") or ("http" if self.settings.http_only else "https"))
        port = int(payload.get("api_port") or 443)
        advertised_reachable_url = str(payload.get("reachable_url") or "").strip()
        reachable_url = advertised_reachable_url
        if source_ip:
            suffix = "" if scheme == "https" and port == 443 else f":{port}"
            reachable_url = f"{scheme}://{source_ip}{suffix}"
        peer = _local_network.save_paired_peer(
            self.settings,
            {
                "drone_id": peer_id,
                "name": str(payload.get("name") or peer_id),
                "hostname": str(payload.get("hostname") or ""),
                "reachable_url": reachable_url,
                "advertised_reachable_url": advertised_reachable_url,
                "scheme": scheme,
                "api_port": port,
                "certificate_fingerprint": fingerprint,
                "certificate_path": str(cert_path),
                "source_ip": source_ip,
            },
        )
        ssl_context = getattr(self.server, "ssl_context", None)
        if ssl_context is not None:
            try:
                ssl_context.load_verify_locations(cafile=str(cert_path))
            except ssl.SSLError:
                pass
        _local_network.pairing_code(self.settings, rotate=True)
        own_certificate = DroneCertificateManager(self.settings).ensure_certificate()
        own_discovery = _local_network.discovery_payload(
            self.settings,
            str(own_certificate.get("fingerprint") or ""),
        )
        self._send_json(
            200,
            {
                "status": "paired",
                "peer": _public_local_peer(peer),
                "drone_id": self.settings.overmind_device_id,
                "name": socket.gethostname(),
                "scheme": _drone_scheme(self.settings),
                "api_port": _drone_advertised_api_port(self.settings),
                "reachable_url": own_discovery.get("reachable_url"),
                "certificate_pem": str(own_certificate.get("public_certificate") or ""),
                "certificate_fingerprint": str(own_certificate.get("fingerprint") or ""),
            },
        )

    def _handle_peer_health(self) -> None:
        if not self._peer_request_authorized():
            return
        self._send_json(
            200,
            {
                "status": "ok",
                "drone_id": self.settings.overmind_device_id,
                "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "mtls": bool(self.settings.drone_mtls_enabled or _local_network.is_local_mode(self.settings)),
                "network_mode": _network_mode(self.settings),
            },
        )

    def _handle_peer_inventory(self, asset_type: str, query_params: dict, require_authorization: bool = True) -> None:
        if require_authorization and not self._peer_request_authorized():
            return
        self._send_json(200, self._collect_peer_inventory(asset_type, query_params))

    def _collect_peer_inventory(self, asset_type: str, query_params: dict) -> dict:
        normalized = str(asset_type or "").strip().lower()
        try:
            limit = max(1, min(int((query_params.get("limit") or ["500"])[0]), 2000))
            offset = max(0, int((query_params.get("offset") or ["0"])[0]))
        except (TypeError, ValueError):
            raise ValueError("limit and offset must be integers")
        query = str((query_params.get("q") or [""])[0]).strip().lower()
        system = str((query_params.get("system") or [""])[0]).strip()
        systems = {
            value.strip().lower()
            for value in str((query_params.get("systems") or [""])[0]).split(",")
            if value.strip()
        }
        if normalized == "summary":
            cache_status = _rom_metadata_cache_status(self.settings)
            return {
                "drone_id": self.settings.overmind_device_id,
                "name": socket.gethostname(),
                "systems": self.repository.list_system_names(),
                "counts": cache_status.get("counts") or {},
                "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
        if normalized == "roms":
            # Scan only the requested systems. Scanning the WHOLE library and then
            # filtering (the old plural-`systems` path) is dramatically slower on a
            # large library and could blow past the requester's peer-fetch timeout,
            # surfacing as a silent "Failed to fetch". An empty target list means
            # "no filter" -> the whole library.
            if system:
                target_systems = [system]
            elif systems:
                target_systems = [name for name in self.repository.list_system_names() if name.strip().lower() in systems]
            else:
                target_systems = list(self.repository.list_system_names())
            per_system_rows = []
            for system_name in target_systems:
                try:
                    _, system_rows = self.repository.list_assets(system_name, "roms")
                except Exception:
                    continue
                # Stamp the system on every row so the requester (and the bulk copy
                # path) always knows where each ROM belongs, even when the SQLite
                # fast path omits it.
                for row in system_rows:
                    if isinstance(row, dict):
                        row["system"] = system_name
                per_system_rows.append(system_rows)
            if len(per_system_rows) <= 1:
                rows = per_system_rows[0] if per_system_rows else []
            else:
                # Round-robin interleave so every requested system is visible from
                # the first page (and downloads in a balanced order) instead of all
                # of one system before the next -- which made multi-system requests
                # look like only one system was returned.
                rows = []
                longest = max(len(system_rows) for system_rows in per_system_rows)
                for index in range(longest):
                    for system_rows in per_system_rows:
                        if index < len(system_rows):
                            rows.append(system_rows[index])
        elif normalized == "bios":
            rows = self.repository.list_bios_entries()
        elif normalized == "artwork":
            rows = self.repository.list_artwork_metadata()
            if system:
                rows = [row for row in rows if str(row.get("system") or "").lower() == system.lower()]
        elif normalized == "saves":
            if self.settings.use_fake_data:
                _saves_store.sync_saves_cache(self.settings.saves_root)
            rows = _saves_store.list_saves(self.settings.saves_root, system=system or None)
        elif normalized == "emulator_configs":
            configs = _list_emulator_config_files(self.settings, max_configs=2000)
            rows = [
                {
                    "name": Path(str(row.get("relative_path") or "")).name,
                    "root_name": row.get("root_name"),
                    "relative_path": row.get("relative_path"),
                    "size": row.get("size"),
                    "modified_at": row.get("modified_at"),
                    "error": row.get("error"),
                    "is_downloadable": False,
                }
                for row in configs.get("configs") or []
                if isinstance(row, dict)
            ]
        elif normalized == "gameplay":
            rows = sorted(
                [dict(row, is_downloadable=False) for row in _load_gameplay_history(self.settings)],
                key=lambda row: str(row.get("played_at") or row.get("started_at") or ""),
                reverse=True,
            )
        else:
            raise ValueError("asset type must be summary, roms, bios, artwork, saves, emulator_configs, or gameplay")
        rows = [
            {key: value for key, value in row.items() if key not in {"absolute_path"}}
            for row in rows
            if isinstance(row, dict)
        ]
        if systems:
            rows = [
                row for row in rows
                if str(row.get("system") or row.get("root_name") or "").strip().lower() in systems
            ]
        if query:
            rows = [row for row in rows if query in json.dumps(row, sort_keys=True).lower()]
        total = len(rows)
        page = rows[offset:offset + limit]
        return {
            "drone_id": self.settings.overmind_device_id,
            "asset_type": normalized,
            "system": system or None,
            "systems": sorted(systems),
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": page,
        }

    def _handle_peer_rom_download(self, system: str, relative_path: str) -> None:
        if not self._peer_request_authorized():
            return
        system_dir = self.repository.get_system_dir(system).resolve()
        rel = unquote(relative_path or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            self._send_json(400, {"error": "invalid rom path"})
            return
        target = (system_dir / rel).resolve()
        if not target.exists() or not target.is_file() or (target != system_dir and system_dir not in target.parents):
            self.log_error("peer rom download failed system=%s rom=%s resolved=%s reason=not_found", system, rel, str(target))
            self._send_json(404, {"error": "not found"})
            return
        self.log_message("peer rom download system=%s rom=%s bytes=%s", system, rel, target.stat().st_size)
        self._stream_file(target, "application/octet-stream", as_attachment=True)

    def _handle_peer_rom_manifest(self, system: str, relative_path: str) -> None:
        if not self._peer_request_authorized():
            return
        system_dir = self.repository.get_system_dir(system).resolve()
        rel = unquote(relative_path or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            self._send_json(400, {"error": "invalid rom path"})
            return
        target = (system_dir / rel).resolve()
        if not target.exists() or not target.is_dir() or (target != system_dir and system_dir not in target.parents):
            self.log_error("peer rom manifest failed system=%s rom=%s resolved=%s reason=not_found", system, rel, str(target))
            self._send_json(404, {"error": "not found"})
            return
        files = []
        directories = []
        total_size = 0
        latest_mtime = int(target.stat().st_mtime)
        for child in sorted(target.rglob("*"), key=lambda p: p.relative_to(target).as_posix().lower()):
            child_rel = child.relative_to(target).as_posix()
            if child.is_dir():
                directories.append(child_rel)
                continue
            if not child.is_file():
                continue
            stat = child.stat()
            total_size += int(stat.st_size)
            latest_mtime = max(latest_mtime, int(stat.st_mtime))
            files.append({"relative_path": child_rel, "file_size": int(stat.st_size), "modified_time": int(stat.st_mtime)})
        self._send_json(
            200,
            {
                "system": system,
                "relative_path": rel,
                "entry_type": "folder",
                "file_count": len(files),
                "file_size": total_size,
                "modified_time": latest_mtime,
                "directories": directories,
                "files": files,
            },
        )

    def _handle_peer_bios_download(self, relative_path: str) -> None:
        if not self._peer_request_authorized():
            return
        try:
            bios_root = self.repository.get_bios_root().resolve()
        except FileNotFoundError:
            self._send_json(404, {"error": "not found"})
            return
        rel = unquote(relative_path or "").replace("\\", "/").lstrip("/")
        if not rel or ".." in Path(rel).parts:
            self._send_json(400, {"error": "invalid bios path"})
            return
        target = (bios_root / rel).resolve()
        if not target.exists() or not target.is_file() or (target != bios_root and bios_root not in target.parents):
            self.log_error("peer bios download failed bios=%s resolved=%s reason=not_found", rel, str(target))
            self._send_json(404, {"error": "not found"})
            return
        self.log_message("peer bios download bios=%s bytes=%s", rel, target.stat().st_size)
        self._stream_file(target, "application/octet-stream", as_attachment=True)

    def _handle_peer_save_download(self, system: str, relative_path: str) -> None:
        """Serve a single game-save file to an authenticated peer (mTLS when enabled)."""
        if not self._peer_request_authorized():
            return
        saves_root = Path(self.settings.saves_root).resolve()
        system_clean = unquote(system or "").replace("\\", "/").strip("/")
        rel = unquote(relative_path or "").replace("\\", "/").lstrip("/")
        if not system_clean or ".." in Path(system_clean).parts or not rel or ".." in Path(rel).parts:
            self._send_json(400, {"error": "invalid save path"})
            return
        target = (saves_root / system_clean / rel).resolve()
        if not target.exists() or not target.is_file() or saves_root not in target.parents:
            self.log_error("peer save download failed system=%s save=%s resolved=%s reason=not_found", system_clean, rel, str(target))
            self._send_json(404, {"error": "not found"})
            return
        self.log_message("peer save download system=%s save=%s bytes=%s", system_clean, rel, target.stat().st_size)
        self._stream_file(target, "application/octet-stream", as_attachment=True)

    def _handle_peer_artwork_download(self, system: str, artwork_type: str, rom_path: str) -> None:
        if not self._peer_request_authorized():
            return
        try:
            target, relative_path, gamelist_ref = self.repository.resolve_artwork_file(system, unquote(rom_path or ""), unquote(artwork_type or ""))
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        except Exception:
            self._send_json(404, {"error": "not found"})
            return
        self.log_message("peer artwork download system=%s type=%s rom=%s artwork=%s bytes=%s", system, artwork_type, rom_path, relative_path, target.stat().st_size)
        self._stream_file(
            target,
            "application/octet-stream",
            as_attachment=True,
            extra_headers={"X-Asset-Relative-Path": relative_path, "X-Gamelist-Reference": gamelist_ref},
        )

    def _stream_file(self, path: Path, content_type: str, as_attachment: bool = False, extra_headers: Optional[dict] = None) -> None:
        file_size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_size))
        self._send_security_headers()
        if as_attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        for key, value in (extra_headers or {}).items():
            self.send_header(str(key), str(value))
        self.end_headers()

        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _stream_cached_image(self, path: Path) -> None:
        key = str(path)

        if self.image_miss_cache.has(key):
            raise FileNotFoundError()

        cached = self.image_cache.get(key)
        current_mtime = path.stat().st_mtime if path.exists() else None
        if cached and cached["meta"].get("mtime") == current_mtime:
            data = cached["data"]
            content_type = cached["meta"]["content_type"]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self._send_security_headers()
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return

        if not path.exists():
            self.image_miss_cache.put(key)
            raise FileNotFoundError()

        if not path.is_file():
            raise ValueError("not a file")

        data = path.read_bytes()
        content_type = self._guess_content_type(path)
        self.image_cache.put(key, data, meta={"content_type": content_type, "mtime": path.stat().st_mtime})

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._send_security_headers()
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)


    def _handle_search(self, query: str, system: Optional[str] = None) -> None:
        query = query.strip()
        if not query:
            self._send_json(400, {"error": "missing query parameter q"})
            return
        system_filter = system.strip() if system else None
        if system_filter:
            system_filter = valid_segment(system_filter)
        results = self.repository.search_roms(query, system_filter=system_filter)
        if not self.settings.downloads_enabled:
            for item in results:
                item["is_downloadable"] = False
        cache_key = f"json:/search?q={query.lower()}&system={(system_filter or '').lower()}"
        self._send_json(200, {"query": query, "system": system_filter, "results": results}, cache_key=cache_key)

    def _build_theme_meta(self) -> dict:
        explicit = self.settings.batocera_theme_name
        from_batocera_conf = _parse_batocera_theme_name(self.settings.batocera_conf_file)
        resolved_es_settings_file = _resolve_es_settings_file(self.settings)
        from_es_settings = _parse_es_theme_name(resolved_es_settings_file) if resolved_es_settings_file else None
        selected = explicit or from_batocera_conf or from_es_settings
        theme_dir = _resolve_theme_dir(self.settings)
        if not theme_dir:
            return {
                "enabled": False,
                "selected_theme_name": selected,
                "theme_sources": {
                    "env": explicit,
                    "batocera_conf": from_batocera_conf,
                    "es_settings": from_es_settings,
                },
                "themes_root": str(self.settings.themes_root),
                "es_settings_file": str(resolved_es_settings_file) if resolved_es_settings_file else None,
            }

        css_candidates = ["theme.css", "style.css", "theme/theme.css", "theme/style.css", "_inc/theme.css", "_inc/style.css"]
        bg_name_candidates = ["background", "fond", "bg", "backdrop", "wallpaper"]
        logo_name_candidates = ["logo", "brand", "title", "system-logo"]

        def first_existing(candidates: List[str]) -> Optional[str]:
            for rel in candidates:
                target = (theme_dir / rel).resolve()
                if target.exists() and target.is_file() and theme_dir in target.parents:
                    return rel
            return None

        def first_match_recursive(name_fragments: List[str], allowed_suffixes: Tuple[str, ...]) -> Optional[str]:
            # Keep this bounded for large theme trees.
            checked = 0
            for path in theme_dir.rglob("*"):
                if checked > 5000:
                    break
                checked += 1
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix not in allowed_suffixes:
                    continue
                name_lower = path.stem.lower()
                if any(fragment in name_lower for fragment in name_fragments):
                    try:
                        return path.relative_to(theme_dir).as_posix()
                    except Exception:
                        continue
            return None

        css_file = first_existing(css_candidates)
        if not css_file:
            css_file = first_match_recursive(["theme", "style"], (".css",))

        bg_file = first_existing(
            [
                "art/background.png",
                "art/background.jpg",
                "art/fond.png",
                "art/fond.jpg",
                "background.png",
                "background.jpg",
            ]
        )
        if not bg_file:
            bg_file = first_match_recursive(bg_name_candidates, (".png", ".jpg", ".jpeg", ".webp"))

        logo_file = first_existing(["art/logo.png", "art/logo.svg", "logo.png", "logo.svg"])
        if not logo_file:
            logo_file = first_match_recursive(logo_name_candidates, (".png", ".jpg", ".jpeg", ".webp", ".svg"))

        css_url = api_url(f"/theme/assets/{css_file}") if css_file else None
        if self.settings.use_fake_data and css_url:
            css_url = None
        background_url = self._fake_theme_asset_url(bg_file) if (self.settings.use_fake_data and bg_file) else (api_url(f"/theme/assets/{bg_file}") if bg_file else None)
        logo_url = self._fake_theme_asset_url(logo_file) if (self.settings.use_fake_data and logo_file) else (api_url(f"/theme/assets/{logo_file}") if logo_file else None)

        return {
            "enabled": True,
            "theme_name": theme_dir.name,
            "theme_dir": str(theme_dir),
            "selected_theme_name": selected,
            "theme_sources": {
                "env": explicit,
                "batocera_conf": from_batocera_conf,
                "es_settings": from_es_settings,
            },
            "themes_root": str(self.settings.themes_root),
            "es_settings_file": str(resolved_es_settings_file) if resolved_es_settings_file else None,
            "api": {
                "theme_assets_base": api_url("/theme/assets/"),
                "system_theme_meta": api_url("/theme/system/{system}"),
            },
            "ui": {
                "css_url": css_url,
                "background_url": background_url,
                "logo_url": logo_url,
            },
            "css_url": css_url,
            "background_url": background_url,
            "logo_url": logo_url,
            "resolved_files": {
                "css": css_file,
                "background": bg_file,
                "logo": logo_file,
            },
        }

    def _handle_theme_meta(self) -> None:
        self._send_json(200, self._build_theme_meta(), cache_key="json:/theme/meta")

    def _build_system_theme_meta(self, system: str) -> dict:
        theme_dir = _resolve_theme_dir(self.settings)
        if not theme_dir:
            return {"enabled": False, "system": system, "reason": "no active theme"}

        candidate_dirs = [
            theme_dir / system,
            theme_dir / system.lower(),
            theme_dir / system.upper(),
            theme_dir / "default",
            theme_dir / "_inc",
        ]

        system_dir: Optional[Path] = None
        for candidate in candidate_dirs:
            if candidate.exists() and candidate.is_dir():
                system_dir = candidate.resolve()
                break

        if not system_dir:
            return {"enabled": False, "system": system, "reason": "system theme folder not found"}

        def first_match_recursive(base: Path, name_fragments: List[str], allowed_suffixes: Tuple[str, ...]) -> Optional[str]:
            checked = 0
            for path in base.rglob("*"):
                if checked > 5000:
                    break
                checked += 1
                if not path.is_file():
                    continue
                if path.suffix.lower() not in allowed_suffixes:
                    continue
                stem = path.stem.lower()
                if any(fragment in stem for fragment in name_fragments):
                    try:
                        return path.relative_to(theme_dir).as_posix()
                    except Exception:
                        continue
            return None

        theme_xml = first_match_recursive(system_dir, ["theme"], (".xml",))
        css_file = first_match_recursive(system_dir, ["style", "theme"], (".css",))
        bg_file = first_match_recursive(system_dir, ["background", "bg", "fond"], (".png", ".jpg", ".jpeg", ".webp"))
        logo_file = first_match_recursive(system_dir, ["logo", "title", "brand"], (".png", ".jpg", ".jpeg", ".webp", ".svg"))

        theme_xml_url = api_url(f"/theme/assets/{theme_xml}") if theme_xml else None
        css_url = api_url(f"/theme/assets/{css_file}") if css_file else None
        if self.settings.use_fake_data and css_url:
            css_url = None
        background_url = self._fake_theme_asset_url(bg_file) if (self.settings.use_fake_data and bg_file) else (api_url(f"/theme/assets/{bg_file}") if bg_file else None)
        logo_url = self._fake_theme_asset_url(logo_file) if (self.settings.use_fake_data and logo_file) else (api_url(f"/theme/assets/{logo_file}") if logo_file else None)

        return {
            "enabled": True,
            "system": system,
            "theme_name": theme_dir.name,
            "system_theme_dir": system_dir.relative_to(theme_dir).as_posix(),
            "theme_xml_url": theme_xml_url,
            "css_url": css_url,
            "background_url": background_url,
            "logo_url": logo_url,
            "resolved_files": {
                "theme_xml": theme_xml,
                "css": css_file,
                "background": bg_file,
                "logo": logo_file,
            },
        }

    def _handle_system_theme_meta(self, system: str) -> None:
        system = valid_segment(system)
        self._send_json(200, self._build_system_theme_meta(system), cache_key=f"json:/theme/system/{system}")

    def _build_theme_background_candidates(self) -> dict:
        theme_dir = _resolve_theme_dir(self.settings)
        if not theme_dir:
            return {
                "enabled": False,
                "theme_name": None,
                "count": 0,
                "backgrounds": [],
                "cache_seconds": 60,
            }

        allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
        # Mirrors requested shell filter semantics.
        path_pattern = re.compile(
            r"((_inc|assets|images|art|common).*(background|wallpaper|wall|back|bg))|"
            r"(/(background|wallpaper|wall|back|bg)[^/]*\.(png|jpg|jpeg|webp)$)",
            flags=re.IGNORECASE,
        )

        candidates: List[str] = []
        for path in theme_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in allowed_suffixes:
                continue
            rel = path.relative_to(theme_dir).as_posix()
            rel_with_slash = f"/{rel}"
            if path_pattern.search(rel_with_slash):
                candidates.append(rel)

        candidates = sorted(set(candidates), key=str.lower)
        if self.settings.use_fake_data:
            urls = [self._fake_theme_asset_url(rel) for rel in candidates]
        else:
            urls = [api_url(f"/theme/assets/{quote(rel, safe='/')}") for rel in candidates]
        return {
            "enabled": True,
            "theme_name": theme_dir.name,
            "count": len(urls),
            "backgrounds": urls,
            "cache_seconds": 60,
        }

    def _handle_theme_backgrounds(self) -> None:
        self._send_json(200, self._build_theme_background_candidates(), cache_key="json:/theme/backgrounds")

    def _build_theme_logo_candidates(self) -> dict:
        theme_dir = _resolve_theme_dir(self.settings)
        if not theme_dir:
            return {
                "enabled": False,
                "theme_name": None,
                "count": 0,
                "logos": [],
                "cache_seconds": 60,
            }

        allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
        name_pattern = re.compile(r"(logo|logos|system|wheel|marquee|banner)", flags=re.IGNORECASE)

        candidates: List[str] = []
        for path in theme_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in allowed_suffixes:
                continue
            rel = path.relative_to(theme_dir).as_posix()
            if name_pattern.search(rel):
                candidates.append(rel)

        candidates = sorted(set(candidates), key=str.lower)
        if self.settings.use_fake_data:
            urls = [self._fake_theme_asset_url(rel) for rel in candidates]
        else:
            urls = [api_url(f"/theme/assets/{quote(rel, safe='/')}") for rel in candidates]
        return {
            "enabled": True,
            "theme_name": theme_dir.name,
            "count": len(urls),
            "logos": urls[:200],
            "cache_seconds": 60,
        }

    def _handle_theme_logos(self) -> None:
        self._send_json(200, self._build_theme_logo_candidates(), cache_key="json:/theme/logos")

    def _build_theme_image_catalog(
        self,
        limit: int = 500,
        offset: int = 0,
        query: Optional[str] = None,
        system_filter: Optional[str] = None,
        system_filters: Optional[List[str]] = None,
    ) -> dict:
        theme_dir = _resolve_theme_dir(self.settings)
        if not theme_dir:
            return {"enabled": False, "theme_name": None, "count": 0, "images": []}

        allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}
        images_all: List[dict] = []
        checked = 0
        for path in theme_dir.rglob("*"):
            checked += 1
            if checked > 200000:
                break
            if not path.is_file():
                continue
            if path.suffix.lower() not in allowed_suffixes:
                continue
            rel = path.relative_to(theme_dir).as_posix()
            folder = Path(rel).parent.as_posix()
            image_url = self._fake_theme_asset_url(rel) if self.settings.use_fake_data else api_url(f"/theme/assets/{quote(rel, safe='/')}")
            images_all.append(
                {
                    "path": rel,
                    "folder": "." if folder == "." else folder,
                    "name": Path(rel).name,
                    "url": image_url,
                }
            )

        images_all.sort(key=lambda item: item["path"].lower())
        systems_all = sorted(
            {
                (item["folder"].split("/")[0] if item["folder"] != "." else "_root").lower()
                for item in images_all
            }
        )
        if query:
            q = query.strip().lower()
            images_all = [item for item in images_all if q in item["path"].lower()]
        selected_systems: List[str] = []
        if system_filters:
            selected_systems = [s.strip().lower() for s in system_filters if s and s.strip()]
        elif system_filter:
            selected_systems = [system_filter.strip().lower()]

        if "__none__" in selected_systems:
            images_all = []
        elif selected_systems:
            selected_set = set(selected_systems)
            images_all = [
                item
                for item in images_all
                if ((item["folder"].split("/")[0] if item["folder"] != "." else "_root").lower() in selected_set)
            ]

        total = len(images_all)
        offset = max(0, offset)
        limit = max(1, min(limit, 5000))
        images = images_all[offset : offset + limit]
        return {
            "enabled": True,
            "theme_name": theme_dir.name,
            "systems": systems_all,
            "count": total,
            "offset": offset,
            "limit": limit,
            "returned": len(images),
            "has_more": (offset + len(images)) < total,
            "images": images,
        }

    def _handle_theme_images(
        self,
        limit: int,
        offset: int,
        query: Optional[str],
        system_filter: Optional[str],
        system_filters: Optional[List[str]] = None,
    ) -> None:
        payload = self._build_theme_image_catalog(
            limit=limit,
            offset=offset,
            query=query,
            system_filter=system_filter,
            system_filters=system_filters,
        )
        systems_key = ",".join(sorted([s.lower() for s in (system_filters or [])]))
        cache_key = (
            f"json:/theme/images?limit={limit}&offset={offset}&q={(query or '').lower()}"
            f"&system={(system_filter or '').lower()}&systems={systems_key}"
        )
        self._send_json(200, payload, cache_key=cache_key)

    def _handle_theme_asset(self, relative_path: str) -> None:
        if self.settings.use_fake_data:
            lowered = relative_path.lower()
            if lowered.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")):
                self._redirect_to_fake_image(seed=f"theme-asset-{relative_path}", width=800, height=450)
                return
        theme_dir = _resolve_theme_dir(self.settings)
        if not theme_dir:
            raise FileNotFoundError()
        requested = unquote(relative_path.lstrip("/"))
        if not requested or "\x00" in requested:
            raise ValueError("invalid theme asset path")
        asset_path = (theme_dir / requested).resolve()
        if theme_dir not in asset_path.parents or not asset_path.exists() or not asset_path.is_file():
            raise FileNotFoundError()
        self._stream_file(asset_path, self._guess_content_type(asset_path))

    def _handle_systems(self) -> None:
        systems = self.repository.list_systems()
        _, es_systems = _resolve_es_systems_effective(self.settings)
        if es_systems:
            visible = {
                str(item.get("name", "")).strip().lower()
                for item in es_systems
                if item.get("name") and not bool(item.get("hidden"))
            }
            if visible:
                systems = [item for item in systems if str(item.get("name", "")).lower() in visible]
        self._send_json(200, {"systems": systems}, cache_key="json:/systems")

    def _handle_rom_list(self, system: str) -> None:
        _, roms = self.repository.list_assets(system, "roms", include_fingerprint=False)
        if not self.settings.downloads_enabled:
            for item in roms:
                item["is_downloadable"] = False
        self._send_json(200, {"system": system, "roms": roms}, cache_key=f"json:/systems/{system}?fingerprint=0")

    def _handle_images_list(self, system: str) -> None:
        _, images = self.repository.list_assets(system, "images")
        self._send_json(
            200,
            {"system": system, "images": images},
            cache_key=f"json:/systems/{system}/images",
        )

    def _handle_videos_list(self, system: str) -> None:
        _, videos = self.repository.list_assets(system, "videos")
        self._send_json(
            200,
            {"system": system, "videos": videos},
            cache_key=f"json:/systems/{system}/videos",
        )

    def _handle_bios_list(
        self,
        limit: int = 100,
        offset: int = 0,
        query: Optional[str] = None,
        system_filters: Optional[List[str]] = None,
    ) -> None:
        cache, _ = _load_rom_metadata_cache(self.settings)
        cached_bios = cache.get("bios_entries") if isinstance(cache.get("bios_entries"), dict) else {}
        entries = []
        for row in cached_bios.values():
            if not isinstance(row, dict):
                continue
            path = str(row.get("file_path") or row.get("relative_path") or row.get("path") or "").strip()
            if not path:
                continue
            md5_value = str(row.get("bios_md5") or row.get("md5") or "").strip()
            entries.append({
                **row,
                "name": row.get("name") or Path(path).name,
                "path": path,
                "byte_count": row.get("byte_count") if row.get("byte_count") is not None else row.get("file_size"),
                "fingerprint": md5_value,
                "md5": md5_value,
                "bios_md5": md5_value,
            })
        if not entries:
            entries = self.repository.list_bios_entries()
        query_value = (query or "").strip().lower()
        selected_systems = set((s or "").strip().lower() for s in (system_filters or []) if (s or "").strip())
        none_selected = "__none__" in selected_systems
        selected_systems.discard("__none__")

        def _entry_system(item: dict) -> str:
            path = item.get("path") or item.get("name") or ""
            return (path.split("/")[0] if "/" in path else "_root").lower()

        systems_all = sorted({_entry_system(item) for item in entries})
        filtered = entries
        if query_value:
            filtered = [
                item
                for item in filtered
                if (
                    query_value in (item.get("path") or "").lower()
                    or query_value in (item.get("name") or "").lower()
                    or query_value in (item.get("fingerprint") or "").lower()
                    or query_value in _entry_system(item)
                )
            ]
        if none_selected:
            filtered = []
        elif selected_systems:
            filtered = [item for item in filtered if _entry_system(item) in selected_systems]

        total = len(filtered)
        offset = max(0, offset)
        limit = max(1, min(limit, 5000))
        page_entries = filtered[offset : offset + limit]

        if not self.settings.downloads_enabled:
            for item in page_entries:
                item["is_downloadable"] = False
        else:
            for item in page_entries:
                item["is_downloadable"] = True
        systems_filtered = sorted({_entry_system(item) for item in filtered})
        cache_key = (
            f"json:/bios?limit={limit}&offset={offset}&q={query_value}"
            f"&systems={','.join(sorted(selected_systems))}"
        )
        self._send_json(
            200,
            {
                "bios": page_entries,
                "count": total,
                "offset": offset,
                "limit": limit,
                "returned": len(page_entries),
                "has_more": (offset + len(page_entries)) < total,
                "systems": systems_all,
                "systems_filtered": systems_filtered,
            },
            cache_key=cache_key,
        )

    def _handle_bios_download(self, unique_id: str) -> None:
        if not self.settings.downloads_enabled:
            raise ValueError("downloads are disabled")
        target_path = self.repository.find_bios_file_by_unique_id(unique_id)
        self._stream_file(target_path, "application/octet-stream", as_attachment=True)

    def _handle_admin_artwork_missing(
        self,
        include_filesystem: bool = False,
        refresh: bool = False,
        limit: int = 200,
        offset: int = 0,
        art_fields: Optional[List[str]] = None,
        system_filters: Optional[List[str]] = None,
        query: Optional[str] = None,
        rom_status: Optional[str] = None,
    ) -> None:
        started_at = time.time()
        normalized_art_fields = {
            str(field or "").strip().lower()
            for field in (art_fields or [])
            if str(field or "").strip()
        }
        include_complete = "show_all" in normalized_art_fields
        items = self.repository.list_missing_artwork(
            include_filesystem=include_filesystem,
            force_refresh=refresh,
            include_complete=include_complete,
        )
        systems_all = sorted({str(item.get("system") or "") for item in items if item.get("system")})
        if "any" in normalized_art_fields or include_complete:
            normalized_art_fields = set()
        valid_art_filters = set(ARTWORK_FIELDS) | {ARTWORK_DUPLICATE_FILTER}
        normalized_art_fields = {field for field in normalized_art_fields if field in valid_art_filters}
        normalized_systems = {
            str(system or "").strip().lower()
            for system in (system_filters or [])
            if str(system or "").strip()
        }
        normalized_query = str(query or "").strip().lower()
        normalized_rom_status = str(rom_status or "any").strip().lower()
        if normalized_rom_status not in ("any", "exists", "missing"):
            normalized_rom_status = "any"

        filtered_items = items
        items_with_status = []
        for item in filtered_items:
            next_item = dict(item)
            next_item["rom_exists"] = self.repository._rom_path_exists(
                str(next_item.get("system") or ""),
                str(next_item.get("rom_path") or next_item.get("rom_name") or ""),
            )
            items_with_status.append(next_item)
        filtered_items = items_with_status
        if normalized_art_fields:
            filtered_items = [
                item
                for item in filtered_items
                if normalized_art_fields.intersection({str(field).lower() for field in (item.get("missing") or [])})
            ]
        if normalized_systems:
            filtered_items = [
                item
                for item in filtered_items
                if str(item.get("system") or "").strip().lower() in normalized_systems
            ]
        if normalized_query:
            filtered_items = [
                item
                for item in filtered_items
                if normalized_query
                in " ".join(
                    [
                        str(item.get("system") or ""),
                        str(item.get("name") or ""),
                        str(item.get("title") or ""),
                        str(item.get("rom_name") or ""),
                        str(item.get("rom_path") or ""),
                        " ".join(str(field) for field in (item.get("missing") or [])),
                    ]
                ).lower()
            ]
        if normalized_rom_status == "exists":
            filtered_items = [item for item in filtered_items if bool(item.get("rom_exists"))]
        elif normalized_rom_status == "missing":
            filtered_items = [item for item in filtered_items if not bool(item.get("rom_exists"))]

        total = len(filtered_items)
        safe_limit = max(1, min(int(limit), 500))
        safe_offset = max(0, int(offset))
        page_items = [dict(item) for item in filtered_items[safe_offset : safe_offset + safe_limit]]
        systems_filtered = sorted({str(item.get("system") or "") for item in filtered_items if item.get("system")})
        field_counts = {field: 0 for field in (*ARTWORK_FIELDS, ARTWORK_DUPLICATE_FILTER)}
        for item in filtered_items:
            for field in item.get("missing") or []:
                if field in field_counts:
                    field_counts[field] += 1
        self._send_json(
            200,
            {
                "roms": page_items,
                "count": total,
                "returned": len(page_items),
                "limit": safe_limit,
                "offset": safe_offset,
                "has_more": (safe_offset + len(page_items)) < total,
                "systems": systems_all,
                "systems_filtered": systems_filtered,
                "fields": list(ARTWORK_FIELDS) + [ARTWORK_DUPLICATE_FILTER],
                "field_counts": field_counts,
                "selected_fields": ["show_all"] if include_complete else (sorted(normalized_art_fields) if normalized_art_fields else ["any"]),
                "selected_systems": sorted(normalized_systems),
                "rom_status": normalized_rom_status,
                "query": normalized_query,
                "mode": "filesystem" if include_filesystem else "gamelist",
                "show_all": include_complete,
                "cached": not refresh,
                "elapsed_ms": int((time.time() - started_at) * 1000),
            },
        )

    def _handle_admin_launchbox_search(self, system: str, rom_id: str, rom_path: str, query: str) -> None:
        system_value = (system or "").strip()
        rom_id_value = (rom_id or "").strip()
        rom_path_value = _normalize_gamelist_rom_path(rom_path)
        query_value = (query or "").strip()
        if system_value and not query_value and (rom_path_value or rom_id_value):
            rom = self.repository.find_rom_by_path(system_value, rom_path_value) if rom_path_value else self.repository.find_rom_by_unique_id(system_value, rom_id_value)
            query_value = _clean_rom_title(str(rom.get("image_stem") or rom.get("name") or ""))
        elif query_value:
            query_value = _clean_rom_title(query_value)
        if not query_value:
            raise ValueError("q or system+rom_id/rom_path is required")
        client = LaunchBoxClient()
        matches = client.search(query_value, system=system_value or None)
        self._send_json(
            200,
            {
                "query": query_value,
                "system": system_value,
                "launchbox_platform": _launchbox_platform_for_system(system_value),
                "rom_id": rom_id_value,
                "rom_path": rom_path_value,
                "matches": matches,
            },
        )

    def _handle_admin_launchbox_apply(self, payload: dict) -> None:
        system = str(payload.get("system") or "").strip()
        rom_id = str(payload.get("rom_id") or payload.get("unique_id") or "").strip()
        rom_path = _normalize_gamelist_rom_path(str(payload.get("rom_path") or ""))
        game_key = str(payload.get("game_key") or "").strip()
        override_existing = bool(payload.get("override_existing", False))
        import_metadata = bool(payload.get("import_metadata", False))
        if not system:
            raise ValueError("system is required")
        if not rom_id and not rom_path:
            raise ValueError("rom_id or rom_path is required")
        if not game_key:
            raise ValueError("game_key is required")
        client = LaunchBoxClient()
        result = self.repository.apply_launchbox_artwork(
            system, rom_id, game_key, client, rom_path=rom_path or None,
            override_existing=override_existing, import_metadata=import_metadata
        )
        with self.repository._search_cache_lock:
            self.repository._search_index_expires_at = 0
        with self.repository._missing_artwork_cache_lock:
            self.repository._missing_artwork_cache.clear()
        result["override_existing"] = override_existing
        result["metadata_imported"] = len([item for item in result.get("updated", []) if str(item.get("source") or "") == "launchbox_metadata"])
        self._send_json(200, result)

    def _handle_admin_thegamesdb_artwork_search(self, system: str, rom_id: str, rom_path: str, query: str) -> None:
        system_value = (system or "").strip()
        rom_id_value = (rom_id or "").strip()
        rom_path_value = _normalize_gamelist_rom_path(rom_path)
        query_value = (query or "").strip()
        title_value = query_value
        if system_value and not title_value and (rom_path_value or rom_id_value):
            rom = self.repository.find_rom_by_path(system_value, rom_path_value) if rom_path_value else self.repository.find_rom_by_unique_id(system_value, rom_id_value)
            title_value = str(rom.get("image_stem") or rom.get("name") or "")
        title_value = _clean_rom_title(title_value)
        if not title_value:
            raise ValueError("q or system+rom_id/rom_path is required")
        scraper = TheGamesDBScraper()
        matches = scraper.search(title_value, system=system_value, limit=5)
        self._send_json(
            200,
            {
                "query": title_value,
                "system": system_value,
                "rom_id": rom_id_value,
                "rom_path": rom_path_value,
                "matches": matches,
                "fields": list(ARTWORK_FIELDS),
            },
        )

    def _handle_admin_thegamesdb_artwork_apply(self, payload: dict) -> None:
        system = str(payload.get("system") or "").strip()
        rom_id = str(payload.get("rom_id") or payload.get("unique_id") or "").strip()
        rom_path = _normalize_gamelist_rom_path(str(payload.get("rom_path") or ""))
        game_id = str(payload.get("game_id") or "").strip()
        override_existing = bool(payload.get("override_existing", False))
        import_metadata = bool(payload.get("import_metadata", True))
        if not system:
            raise ValueError("system is required")
        if not rom_id and not rom_path:
            raise ValueError("rom_id or rom_path is required")
        if not game_id:
            raise ValueError("game_id is required")
        scraper = TheGamesDBScraper()
        result = self.repository.apply_thegamesdb_artwork(
            system,
            rom_id,
            game_id,
            scraper,
            rom_path=rom_path or None,
            override_existing=override_existing,
            import_metadata=import_metadata,
        )
        with self.repository._search_cache_lock:
            self.repository._search_index_expires_at = 0
        result["source"] = "thegamesdb"
        result["override_existing"] = override_existing
        result["metadata_imported"] = len([item for item in result.get("updated", []) if str(item.get("source") or "") == "thegamesdb_metadata"])
        self._send_json(200, result)

    def _handle_admin_mobygames_artwork_search(self, system: str, rom_id: str, rom_path: str, query: str) -> None:
        system_value = (system or "").strip()
        rom_id_value = (rom_id or "").strip()
        rom_path_value = _normalize_gamelist_rom_path(rom_path)
        query_value = (query or "").strip()
        title_value = query_value
        if system_value and not title_value and (rom_path_value or rom_id_value):
            rom = self.repository.find_rom_by_path(system_value, rom_path_value) if rom_path_value else self.repository.find_rom_by_unique_id(system_value, rom_id_value)
            title_value = str(rom.get("image_stem") or rom.get("name") or "")
        title_value = _clean_rom_title(title_value)
        if not title_value:
            raise ValueError("q or system+rom_id/rom_path is required")
        self._send_json(
            200,
            {
                "query": title_value,
                "system": system_value,
                "mobygames_platform": MobyGamesClient().platform_name_for_system(system_value),
                "rom_id": rom_id_value,
                "rom_path": rom_path_value,
                "matches": [],
                "configured": False,
                "message": "MobyGames scraping is disabled because the site often requires a browser challenge. Use the MobyGames link to search manually.",
                "fields": list(ARTWORK_FIELDS),
            },
        )

    def _handle_admin_mobygames_artwork_apply(self, payload: dict) -> None:
        system = str(payload.get("system") or "").strip()
        rom_id = str(payload.get("rom_id") or payload.get("unique_id") or "").strip()
        rom_path = _normalize_gamelist_rom_path(str(payload.get("rom_path") or ""))
        game_id = str(payload.get("game_id") or "").strip()
        if not system:
            raise ValueError("system is required")
        if not rom_id and not rom_path:
            raise ValueError("rom_id or rom_path is required")
        if not game_id:
            raise ValueError("game_id is required")
        raise ValueError("MobyGames scraping is disabled because the site often requires a browser challenge. Use the MobyGames link to search manually.")

    def _handle_admin_artwork_upload(self) -> None:
        import urllib.parse
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("multipart/form-data expected")
        # Read raw multipart body
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0 or content_length > 50 * 1024 * 1024:
            raise ValueError("invalid content size")
        raw_body = self.rfile.read(content_length)
        # Parse using simple field extraction for file upload
        boundary = content_type.split("boundary=")[1].strip() if "boundary=" in content_type else None
        if not boundary:
            raise ValueError("boundary not found in content-type")
        boundary = boundary.strip('"').strip("'")
        # Simple multipart parser for file + fields
        parts = raw_body.split(f"--{boundary}".encode())
        field_name = None
        system = None
        rom_id = None
        rom_path = None
        file_data = None
        filename = None
        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            lines = part.split(b"\r\n")
            disposition = b""
            for line in lines:
                if b"Content-Disposition" in line:
                    disposition = line
                    break
            disp_str = disposition.decode("utf-8", errors="replace")
            # Determine field name
            fname = None
            if ' name="' in disp_str:
                fname = disp_str.split(' name="')[1].split('"')[0]
            # Check for filename
            has_file = ' filename="' in disp_str
            fn = None
            if has_file:
                fn = disp_str.split(' filename="')[1].split('"')[0]
            # Find payload (after headers)
            header_end = part.find(b"\r\n\r\n")
            payload_start = header_end + 4 if header_end >= 0 else 0
            payload = lines[-1:] if len(lines) == 1 else raw_body  # simplified
            # Re-extract payload properly
            payload = part[part.find(b"\r\n\r\n")+4:] if b"\r\n\r\n" in part else b""
            payload = payload.rstrip(b"\r\n").rstrip(b"--")
            if has_file and fn:
                file_data = payload
                filename = fn
            elif fname:
                value = payload.decode("utf-8", errors="replace").strip()
                if fname == "field":
                    field_name = value
                elif fname == "system":
                    system = value
                elif fname == "rom_id":
                    rom_id = value
                elif fname == "rom_path":
                    rom_path = _normalize_gamelist_rom_path(value)
        if not file_data or not field_name or not system or (not rom_id and not rom_path):
            raise ValueError("file, field, system, and rom_id or rom_path are required")
        if field_name not in ARTWORK_FIELDS:
            raise ValueError("invalid artwork field")
        filename = filename or f"{field_name}.png"
        # Find the ROM to update its gamelist and images
        system_dir = self.repository.get_system_dir(system)
        # Try to find the ROM by unique_id first, then by path
        try:
            rom = self.repository.find_rom_by_unique_id(system, rom_id) if rom_id else self.repository.find_rom_by_path(system, rom_path or "")
        except FileNotFoundError:
            try:
                rom = self.repository.find_rom_by_path(system, rom_path or rom_id)
            except FileNotFoundError:
                # Just use rom_id as a name stem if not found
                fallback = rom_path or rom_id
                rom = {"name": Path(fallback).stem or fallback, "image_stem": Path(fallback).stem or fallback, "rom_path": rom_path or fallback}
        images_dir = (system_dir / "images").resolve()
        images_dir.mkdir(parents=True, exist_ok=True)
        display_name = str(rom.get("image_stem") or rom.get("name") or rom_id)
        safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(display_name).stem).strip("-") or "rom"
        dest_filename = f"{safe_stem}-{field_name}{Path(filename).suffix}"
        dest_path = images_dir / dest_filename
        with open(dest_path, "wb") as f:
            f.write(file_data)
        relative_path = _relative_artwork_path(system_dir, dest_path)
        normalized_rom_path = _normalize_gamelist_rom_path(rom_path or str(rom.get("rom_path") or ""))
        game = None
        gamelist_details = {}
        existing = {field: "" for field in ARTWORK_FIELDS}
        missing = list(ARTWORK_FIELDS)
        has_gamelist_entry = False
        # Update gamelist if possible
        try:
            tree, root = self.repository._read_gamelist(system_dir)
            game = self.repository._find_gamelist_entry_by_path(root, normalized_rom_path)
            if game is None:
                rom_name = str(rom.get("rom_file") or Path(normalized_rom_path or rom_id).name)
                display_name = str(rom.get("image_stem") or rom.get("name") or Path(rom_name).stem)
                game = self.repository._find_gamelist_entry(root, rom_name, display_name)
            if game is None and normalized_rom_path:
                display_name = str(rom.get("image_stem") or rom.get("name") or Path(normalized_rom_path).stem)
                game = ET.SubElement(root, "game")
                _set_child_text(game, "path", f"./{normalized_rom_path}")
                _set_child_text(game, "name", _clean_rom_title(display_name))
            if game is not None:
                _set_child_text(game, field_name, relative_path)
                gamelist_path = system_dir / "gamelist.xml"
                try:
                    ET.indent(tree, space="  ")
                except Exception:
                    pass
                tree.write(gamelist_path, encoding="utf-8", xml_declaration=True)
                with gamelist_path.open("a", encoding="utf-8") as handle:
                    handle.write("\n")
                gamelist_details = _gamelist_details(game)
                existing = {field: _text_or_empty(game, field) for field in ARTWORK_FIELDS}
                missing = self.repository._entry_missing_artwork(game)
                has_gamelist_entry = True
        except Exception:
            pass  # Gamelist write is best-effort for manual uploads
        # Invalidate caches
        with self.repository._missing_artwork_cache_lock:
            self.repository._missing_artwork_cache.clear()
        rom_name = str(rom.get("name") or Path(rom_path or rom_id).stem or rom_path or rom_id)
        self._send_json(200, {
            "rom_name": rom_name,
            "field": field_name,
            "path": str(dest_path),
            "relative_path": relative_path,
            "url": api_url(f"/public/systems/{quote(system, safe='')}/images/{quote(dest_filename, safe='')}"),
            "existing": existing,
            "missing": missing,
            "gamelist": gamelist_details,
            "has_gamelist_entry": has_gamelist_entry,
        })

    def _handle_admin_gamelist_remove(self, payload: dict) -> None:
        system = str(payload.get("system") or "").strip()
        rom_path = _normalize_gamelist_rom_path(str(payload.get("rom_path") or ""))
        if not system:
            raise ValueError("system is required")
        if not rom_path:
            raise ValueError("rom_path is required")
        result = self.repository.remove_gamelist_entry(system, rom_path)
        self._send_json(200, result)

    def _handle_admin_gamelist_update(self, payload: dict) -> None:
        system = str(payload.get("system") or "").strip()
        rom_path = _normalize_gamelist_rom_path(str(payload.get("rom_path") or ""))
        fields = payload.get("fields")
        if not system:
            raise ValueError("system is required")
        if not rom_path:
            raise ValueError("rom_path is required")
        if not isinstance(fields, dict):
            raise ValueError("fields must be an object")
        result = self.repository.update_gamelist_entry(system, rom_path, fields)
        self._send_json(200, result)

    def _handle_admin_gamelist_remove_missing(self, payload: dict) -> None:
        confirm = str(payload.get("confirm") or "").strip()
        if confirm != "DELETE_MISSING_GAMELIST_ENTRIES":
            raise ValueError("confirm must be DELETE_MISSING_GAMELIST_ENTRIES")
        include_filesystem = bool(payload.get("include_filesystem"))
        art_fields = payload.get("fields") if isinstance(payload.get("fields"), list) else []
        system_filters = payload.get("systems") if isinstance(payload.get("systems"), list) else []
        query = str(payload.get("q") or "")

        items = self.repository.list_missing_artwork(include_filesystem=include_filesystem, force_refresh=False)
        normalized_art_fields = {str(field or "").strip().lower() for field in art_fields if str(field or "").strip()}
        if "any" in normalized_art_fields:
            normalized_art_fields = set()
        normalized_art_fields = {field for field in normalized_art_fields if field in ARTWORK_FIELDS}
        normalized_systems = {str(system or "").strip().lower() for system in system_filters if str(system or "").strip()}
        normalized_query = query.strip().lower()

        filtered = []
        for item in items:
            candidate = dict(item)
            candidate["rom_exists"] = self.repository._rom_path_exists(
                str(candidate.get("system") or ""),
                str(candidate.get("rom_path") or candidate.get("rom_name") or ""),
            )
            if candidate["rom_exists"]:
                continue
            if normalized_art_fields and not normalized_art_fields.intersection({str(field).lower() for field in (candidate.get("missing") or [])}):
                continue
            if normalized_systems and str(candidate.get("system") or "").strip().lower() not in normalized_systems:
                continue
            if normalized_query:
                haystack = " ".join(
                    [
                        str(candidate.get("system") or ""),
                        str(candidate.get("name") or ""),
                        str(candidate.get("title") or ""),
                        str(candidate.get("rom_name") or ""),
                        str(candidate.get("rom_path") or ""),
                        " ".join(str(field) for field in (candidate.get("missing") or [])),
                    ]
                ).lower()
                if normalized_query not in haystack:
                    continue
            filtered.append(candidate)

        result = self.repository.remove_gamelist_entries(filtered)
        result["matched_count"] = len(filtered)
        self._send_json(200, result)

    def _handle_public_image(self, system: str, image_file: str) -> None:
        if self.settings.use_fake_data:
            self._redirect_to_fake_image(seed=f"{system}-{image_file}", width=640, height=360)
            return
        system = valid_segment(unquote(system))
        system_dir = self.repository.get_system_dir(system)
        image_file = valid_segment(unquote(image_file))
        images_dir = (system_dir / "images").resolve()
        image_path = (images_dir / image_file).resolve()

        # Fast path: exact filename match.
        if image_path.exists() and image_path.is_file():
            self._stream_cached_image(image_path)
            return

        # Fallback 1: case-insensitive filename match in images root.
        if images_dir.exists() and images_dir.is_dir():
            requested_lower = image_file.lower()
            for candidate in images_dir.iterdir():
                if candidate.is_file() and candidate.name.lower() == requested_lower:
                    self._stream_cached_image(candidate.resolve())
                    return

        # Fallback 2: recursive stem-based lookup to handle theme/artwork packs
        # that use different extensions, case, or nested folders.
        requested_stem = Path(image_file).stem.lower()
        allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        preferred_stems = {
            requested_stem,
            requested_stem.replace("-image", ""),
            f"{requested_stem}-image",
        }
        checked = 0
        if images_dir.exists() and images_dir.is_dir():
            for candidate in images_dir.rglob("*"):
                checked += 1
                if checked > 30000:
                    break
                if not candidate.is_file():
                    continue
                if candidate.suffix.lower() not in allowed_suffixes:
                    continue
                if candidate.stem.lower() in preferred_stems:
                    self._stream_cached_image(candidate.resolve())
                    return

        # Some gamelist.xml files store artwork in nested media folders instead
        # of the standard images directory. Keep the lookup scoped to the system.
        checked = 0
        if system_dir.exists() and system_dir.is_dir():
            requested_lower = image_file.lower()
            for candidate in system_dir.rglob("*"):
                checked += 1
                if checked > 30000:
                    break
                if not candidate.is_file() or candidate.suffix.lower() not in allowed_suffixes:
                    continue
                if candidate.name.lower() == requested_lower:
                    self._stream_cached_image(candidate.resolve())
                    return

        raise FileNotFoundError()

    def _handle_download(self, system: str, asset_type: str, unique_id: str) -> None:
        if not self.settings.downloads_enabled:
            raise ValueError("downloads are disabled")
        if asset_type == "roms" and str(system).strip().lower() == "steam":
            raise ValueError("steam rom downloads are disabled")
        unique_id = valid_segment(unique_id)
        asset_dir, items = self.repository.list_assets(system, asset_type)

        target_path = None
        is_downloadable = True
        for item in items:
            if item["unique_id"] == unique_id:
                file_name = str(item.get("rom_file") or item.get("name") or "")
                target_path = (asset_dir / file_name).resolve()
                is_downloadable = item.get("is_downloadable", True)
                break

        if not target_path or not target_path.exists():
            self.log_error(
                "download lookup failed system=%s asset_type=%s requested=%s resolved=%s reason=not_found",
                system,
                asset_type,
                unique_id,
                str(target_path) if target_path else "",
            )
            raise FileNotFoundError()
        if not is_downloadable:
            self.log_error("download lookup failed system=%s asset_type=%s requested=%s resolved=%s reason=not_downloadable", system, asset_type, unique_id, str(target_path))
            raise ValueError("asset is not downloadable")
        if not target_path.is_file():
            self.log_error("download lookup failed system=%s asset_type=%s requested=%s resolved=%s reason=not_file", system, asset_type, unique_id, str(target_path))
            raise ValueError("not a file")

        self._stream_file(target_path, "application/octet-stream", as_attachment=True)

    def _handle_image_file_or_download(self, system: str, image_ref: str) -> None:
        if self.settings.use_fake_data:
            self._redirect_to_fake_image(seed=f"{system}-{image_ref}", width=640, height=360)
            return
        system = valid_segment(unquote(system))
        system_dir = self.repository.get_system_dir(system)
        image_ref = valid_segment(unquote(image_ref))
        images_dir = (system_dir / "images").resolve()

        image_path = (images_dir / image_ref).resolve()
        if image_path.exists():
            if not image_path.is_file():
                raise ValueError("not a file")
            self._stream_cached_image(image_path)
            return

        _, roms = self.repository.list_assets(system, "roms")
        for rom in roms:
            if rom["unique_id"] == image_ref:
                stems: List[str] = []
                image_stem = rom.get("image_stem")
                if isinstance(image_stem, str) and image_stem:
                    stems.append(image_stem)
                name_stem = Path(rom["name"]).stem
                if name_stem not in stems:
                    stems.append(name_stem)
                source_folder = rom.get("source_folder")
                if isinstance(source_folder, str) and source_folder:
                    folder_stem = Path(source_folder).stem
                    if folder_stem not in stems:
                        stems.append(folder_stem)

                suffixes = [".png", ".jpg", ".jpeg", ".webp", ".gif"]
                name_patterns = ["{stem}-image{suffix}", "{stem}{suffix}"]
                for stem in stems:
                    for pattern in name_patterns:
                        for suffix in suffixes:
                            candidate_name = pattern.format(stem=stem, suffix=suffix)
                            mapped_image_path = (images_dir / candidate_name).resolve()
                            try:
                                self._stream_cached_image(mapped_image_path)
                                return
                            except FileNotFoundError:
                                continue

                # Fallback: recursive + case-insensitive match by stem for theme/artwork packs
                # that store images in subfolders or mixed-case extensions.
                allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
                normalized_stems = {s.lower() for s in stems}
                normalized_stems_with_suffix = {f"{s.lower()}-image" for s in stems}
                checked = 0
                if images_dir.exists() and images_dir.is_dir():
                    for candidate in images_dir.rglob("*"):
                        checked += 1
                        if checked > 30000:
                            break
                        if not candidate.is_file():
                            continue
                        if candidate.suffix.lower() not in allowed_suffixes:
                            continue
                        candidate_stem = candidate.stem.lower()
                        if candidate_stem in normalized_stems or candidate_stem in normalized_stems_with_suffix:
                            self._stream_cached_image(candidate.resolve())
                            return
                raise FileNotFoundError()

        self._handle_download(system, "images", image_ref)

    def _handle_admin_logs(self, log_source: str, lines: int) -> None:
        import subprocess
        from pathlib import Path

        requested_source = (log_source or "").strip()
        normalized_source = requested_source.lower()
        safe_lines = max(1, min(int(lines), 5000))

        # For now, only expose EmulationStation launch stdout/stderr logs.
        log_path_candidates = {
            "es_launch_stdout": ["/userdata/system/logs/es_launch_stdout.log"],
            "es_launch_stderr": ["/userdata/system/logs/es_launch_stderr.log"],
            "drone_stdout": [str((self.settings.log_dir / self.settings.stdout_log_file).resolve())],
            "drone_stderr": [str((self.settings.log_dir / self.settings.stderr_log_file).resolve())],
            "drone_overmind": [str((self.settings.log_dir / self.settings.overmind_log_file).resolve())],
        }

        def _resolve_userdata_path(candidate: str) -> str:
            if candidate.startswith("/userdata/"):
                suffix = candidate[len("/userdata/") :]
                return str((self.settings.userdata_root / suffix).resolve())
            if candidate == "/userdata":
                return str(self.settings.userdata_root.resolve())
            return candidate

        if normalized_source not in log_path_candidates:
            self._send_json(404, {"error": f"Unknown log source: {requested_source}"})
            return

        def _dedupe(values):
            seen = set()
            result = []
            for value in values:
                item = str(value)
                if item in seen:
                    continue
                seen.add(item)
                result.append(item)
            return result

        # Build a list of fallback file-name patterns we can search for in common roots.
        names = [normalized_source]
        filename_candidates = []
        for name in names:
            filename_candidates.extend([f"{name}.log", f"{name}.txt", f"{name}_log.txt"])

        candidate_paths = [_resolve_userdata_path(path) for path in log_path_candidates[normalized_source]]
        common_roots = [
            _resolve_userdata_path("/userdata/system/logs"),
            _resolve_userdata_path("/userdata/system/configs"),
            _resolve_userdata_path("/userdata/system/.config"),
            _resolve_userdata_path("/userdata/system"),
        ]
        for root in common_roots:
            for filename in filename_candidates:
                candidate_paths.append(f"{root}/{filename}")

        candidate_paths = _dedupe(candidate_paths)

        log_path = None
        for candidate in candidate_paths:
            path = Path(candidate)
            if path.exists() and path.is_file():
                log_path = path
                break

        # Final fallback: bounded recursive search for matching filenames.
        searched_roots = []
        if log_path is None:
            max_dirs_per_root = 1500
            for root in common_roots:
                root_path = Path(root)
                if not root_path.exists() or not root_path.is_dir():
                    continue
                searched_roots.append(root)
                try:
                    checked = 0
                    for path in root_path.rglob("*"):
                        checked += 1
                        if checked > max_dirs_per_root:
                            break
                        if not path.is_file():
                            continue
                        path_name = path.name.lower()
                        if path_name in {name.lower() for name in filename_candidates}:
                            log_path = path
                            break
                    if log_path is not None:
                        break
                except Exception:
                    # Ignore unreadable trees and continue search.
                    continue

        if log_path is None:
            attempted = candidate_paths[:12]
            self._send_json(404, {
                "error": f"Log file not found for source: {requested_source}",
                "attempted_paths": attempted,
                "searched_roots": searched_roots,
            })
            return

        try:
            log_content = _tail_lines(log_path, safe_lines)
            self._send_json(200, {
                "source": normalized_source,
                "path": str(log_path),
                "lines": safe_lines,
                "content": log_content,
            })
        except Exception as e:
            self._send_json(500, {"error": f"Internal error: {str(e)}"})

    def _handle_admin_gameplay_logs(self) -> None:
        try:
            sessions = _load_gameplay_history(self.settings)
            sessions.sort(key=lambda row: str(row.get("played_at") or ""), reverse=True)
            self._send_json(
                200,
                {
                    "type": "game_logs",
                    "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "sessions": sessions,
                    "logs": [],
                    "pending_spool_events": _pending_game_event_count(self.settings),
                },
            )
        except Exception as error:
            self._send_json(500, {"error": _format_overmind_error(error)})

    def _handle_admin_system_info(self, include_speed: bool = False) -> None:
        router_ip_address = _get_router_ip_address() or "Unavailable"
        runtime_metrics = _collect_performance_metrics(self.settings.userdata_root)
        speed_sample = _sample_speed() if include_speed else {
            "upload_mbps": None,
            "download_mbps": None,
            "latency_ms": None,
            "source": "not_sampled",
            "sampled_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        if self.settings.use_fake_data:
            fake_router_ip_address = router_ip_address if router_ip_address != "Unavailable" else "192.168.1.1"
            entries = [
                {"key": "Machine ID", "value": self.settings.overmind_device_id},
                {"key": "Integrated with Overmind", "value": "yes" if self._load_overmind_config().get("integration_enabled") else "no"},
                {"key": "Batocera Version", "value": "v43-dev (Fake)"},
                {"key": "Model", "value": "Batocera DevBox (Fake)"},
                {"key": "System", "value": "Linux 6.6.0-fake"},
                {"key": "Architecture", "value": "x86_64"},
                {"key": "CPU model", "value": "AMD Ryzen 7 7800X3D (Fake)"},
                {"key": "CPU cores / threads", "value": "8 / 16"},
                {"key": "CPU max frequency", "value": "5.00 GHz"},
                {"key": "Temperature", "value": "51 C"},
                {"key": "Available memory", "value": "25.4 GiB / 32 GiB"},
                {"key": "Display resolution", "value": "1920x1080"},
                {"key": "Display refresh rate", "value": "60 Hz"},
                {"key": "Data partition available space", "value": "812 GiB"},
                {"key": "Network IP address", "value": "192.168.1.123"},
                {"key": "Router IP Address", "value": fake_router_ip_address},
                {"key": "Battery", "value": "N/A"},
            ]
            fields = {
                "batocera_version": "v43-dev (Fake)",
                "model": "Batocera DevBox (Fake)",
                "system": "Linux 6.6.0-fake",
                "architecture": "x86_64",
                "cpu_model": "AMD Ryzen 7 7800X3D (Fake)",
                "cpu_topology": "8 / 16",
                "cpu_max_frequency": "5.00 GHz",
                "temperature": "51 C",
                "available_memory": "25.4 GiB / 32 GiB",
                "display_resolution": "1920x1080",
                "display_refresh_rate": "60 Hz",
                "data_partition_available_space": "812 GiB",
                "network_ip_address": "192.168.1.123",
                "router_ip_address": fake_router_ip_address,
                "battery": "N/A",
                "machine_id": self.settings.overmind_device_id,
                "overmind_integrated": "yes" if self._load_overmind_config().get("integration_enabled") else "no",
                "drone_app_version": _drone_app_version(),
            }
            raw = "\n".join(f"{item['key']}: {item['value']}" for item in entries)
            self._send_json(
                200,
                {
                    "raw": raw,
                    "lines": raw.splitlines(),
                    "entries": entries,
                    "fields": fields,
                    "drone_app_version": _drone_app_version(),
                    "runtime_metrics": runtime_metrics,
                    "speed_sample": speed_sample,
                },
            )
            return

        try:
            result = subprocess.run(
                ["batocera-info"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            raw = (result.stdout or "").strip()
            lines = raw.splitlines() if raw else []

            entries = []
            for line in lines:
                text = str(line or "").strip()
                if not text:
                    continue
                if ":" in text:
                    key, value = text.split(":", 1)
                    entries.append({"key": key.strip(), "value": value.strip()})
                else:
                    entries.append({"key": text, "value": ""})

            # Canonical fields for common UI needs.
            fields = {}
            for entry in entries:
                key_lower = entry["key"].lower()
                value = entry["value"]
                if key_lower in ("version", "batocera version"):
                    fields["batocera_version"] = value
                elif key_lower == "model":
                    fields["model"] = value
                elif key_lower == "system":
                    fields["system"] = value
                elif key_lower == "architecture":
                    fields["architecture"] = value
                elif key_lower == "cpu model":
                    fields["cpu_model"] = value
                elif key_lower.startswith("cpu cores"):
                    fields["cpu_topology"] = value
                elif key_lower == "cpu max frequency":
                    fields["cpu_max_frequency"] = value
                elif key_lower == "temperature":
                    fields["temperature"] = value
                elif key_lower == "available memory":
                    fields["available_memory"] = value
                elif key_lower == "display resolution":
                    fields["display_resolution"] = value
                elif key_lower == "display refresh rate":
                    fields["display_refresh_rate"] = value
                elif key_lower == "data partition available space":
                    fields["data_partition_available_space"] = value
                elif key_lower == "network ip address":
                    fields["network_ip_address"] = value
                elif key_lower == "router ip address":
                    fields["router_ip_address"] = value
                elif key_lower == "battery":
                    fields["battery"] = value

            overmind_integrated = "yes" if self._load_overmind_config().get("integration_enabled") else "no"
            entries.insert(0, {"key": "Integrated with Overmind", "value": overmind_integrated})
            entries.insert(0, {"key": "Machine ID", "value": self.settings.overmind_device_id})
            if not fields.get("router_ip_address"):
                router_entry = {"key": "Router IP Address", "value": router_ip_address}
                network_index = next(
                    (
                        index
                        for index, entry in enumerate(entries)
                        if str(entry.get("key", "")).lower() == "network ip address"
                    ),
                    None,
                )
                if network_index is None:
                    entries.insert(2, router_entry)
                else:
                    entries.insert(network_index + 1, router_entry)
                fields["router_ip_address"] = router_ip_address
            fields["machine_id"] = self.settings.overmind_device_id
            fields["overmind_integrated"] = overmind_integrated
            fields["drone_app_version"] = _drone_app_version()

            self._send_json(
                200,
                {
                    "raw": raw,
                    "lines": lines,
                    "entries": entries,
                    "fields": fields,
                    "drone_app_version": _drone_app_version(),
                    "runtime_metrics": runtime_metrics,
                    "speed_sample": speed_sample,
                },
            )
        except Exception as error:
            overmind_integrated = "yes" if self._load_overmind_config().get("integration_enabled") else "no"
            entries = [
                {"key": "Machine ID", "value": self.settings.overmind_device_id},
                {"key": "Integrated with Overmind", "value": overmind_integrated},
                {"key": "Router IP Address", "value": router_ip_address},
                {"key": "System Info", "value": f"batocera-info unavailable: {str(error)}"},
            ]
            raw = "\n".join(f"{item['key']}: {item['value']}" for item in entries)
            self._send_json(
                200,
                {
                    "raw": raw,
                    "lines": raw.splitlines(),
                    "entries": entries,
                    "fields": {
                        "machine_id": self.settings.overmind_device_id,
                        "overmind_integrated": overmind_integrated,
                        "router_ip_address": router_ip_address,
                        "drone_app_version": _drone_app_version(),
                    },
                    "drone_app_version": _drone_app_version(),
                    "runtime_metrics": runtime_metrics,
                    "speed_sample": speed_sample,
                    "warning": f"Failed to run batocera-info: {str(error)}",
                },
            )

    def _handle_admin_overmind_status(self) -> None:
        config = self._load_overmind_config()
        if _normalize_overmind_link_state(config):
            self._save_overmind_config(config)
        payload = self._overmind_public_payload(config)
        payload["network_mode"] = _network_mode(self.settings)
        payload["overmind_active"] = _local_network.is_overmind_mode(self.settings)
        if not payload["overmind_active"]:
            payload["status"]["integration_enabled"] = False
            payload["status"]["integration_state"] = "disabled"
            payload["status"]["swarm_connection_status"] = "disconnected"
        self._send_json(200, payload)

    def _require_overmind_mode(self) -> bool:
        if _local_network.is_overmind_mode(self.settings):
            return True
        self._send_json(409, {"error": "Overmind integration is disabled"})
        return False

    def _handle_admin_network_mode(self) -> None:
        mode = _network_mode(self.settings)
        integrations = _local_network.get_integrations(self.settings)
        self._send_json(
            200,
            {
                "mode": mode,
                "overmind_active": integrations["overmind_enabled"],
                "local_network_active": integrations["local_network_enabled"],
                "overmind_enabled": integrations["overmind_enabled"],
                "local_network_enabled": integrations["local_network_enabled"],
                "modes": [
                    _local_network.MODE_OVERMIND,
                    _local_network.MODE_LOCAL_NETWORK,
                    _local_network.MODE_BOTH,
                    _local_network.MODE_DISABLED,
                ],
            },
        )

    def _handle_admin_network_mode_update(self, payload: dict) -> None:
        current = _local_network.get_integrations(self.settings)
        if "overmind_enabled" in payload or "local_network_enabled" in payload:
            result = _local_network.set_integrations(
                self.settings,
                overmind_enabled=bool(payload.get("overmind_enabled", current["overmind_enabled"])),
                local_network_enabled=bool(payload.get("local_network_enabled", current["local_network_enabled"])),
            )
        else:
            result = _local_network.set_mode(self.settings, str(payload.get("mode") or ""))
        if result["local_network_enabled"]:
            ssl_context = getattr(self.server, "ssl_context", None)
            if ssl_context is not None:
                ssl_context.verify_mode = ssl.CERT_OPTIONAL
                for peer in _local_network.paired_peers(self.settings):
                    cert_path = Path(str(peer.get("certificate_path") or ""))
                    if cert_path.exists():
                        try:
                            ssl_context.load_verify_locations(cafile=str(cert_path))
                        except ssl.SSLError:
                            continue
            _local_network.announce(self.settings, str(DroneCertificateManager(self.settings).metadata().get("fingerprint") or ""))
        self._handle_admin_network_mode()

    def _local_network_status_payload(self) -> dict:
        hide_seeded_demo = False
        if self.settings.use_fake_data and _local_network.is_local_mode(self.settings):
            discovered_peers = _local_network.discovered_peers(self.settings, include_stale=True)
            visible_peer = next((peer for peer in discovered_peers if not peer.get("fake_data")), None)
            paired_peers = _local_network.paired_peers(self.settings)
            if visible_peer and not any(not peer.get("fake_data") for peer in paired_peers):
                _local_network.forget_peer(self.settings, "fake-local-peer-01")
                _local_network.save_paired_peer(self.settings, {**visible_peer, "fake_data": True})
            hide_seeded_demo = visible_peer is not None
        paired = {str(peer.get("drone_id") or ""): peer for peer in _local_network.paired_peers(self.settings)}
        checks = {
            str(check.get("target_drone_id") or ""): check
            for check in _local_network.load_peer_checks(self.settings)
            if isinstance(check, dict)
        }
        discovered = []
        seen = set()
        for peer in _local_network.discovered_peers(self.settings, include_stale=True):
            peer_id = str(peer.get("drone_id") or "")
            if hide_seeded_demo and peer_id == "fake-local-peer-01":
                continue
            discovered.append(_public_local_peer({**peer, **paired.get(peer_id, {}), "health": checks.get(peer_id)}))
            seen.add(peer_id)
        for peer_id, peer in paired.items():
            if peer_id not in seen:
                discovered.append(_public_local_peer({**peer, "health": checks.get(peer_id)}))
        return {
            "mode": _network_mode(self.settings),
            "active": _local_network.is_local_mode(self.settings),
            "pairing": _local_network.pairing_code(self.settings),
            "peers": discovered,
            "paired_count": len(paired),
            "discovered_count": len(discovered),
            "downloads": _get_download_manager().snapshot() if _get_download_manager() else {},
            "activity": _local_network.load_activity(self.settings),
        }

    def _handle_admin_local_network_status(self) -> None:
        self._send_json(200, self._local_network_status_payload())

    def _handle_admin_local_network_discover(self) -> None:
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Enable Local Network mode before discovering peers"})
            return
        sent = _local_network.announce(
            self.settings,
            str(DroneCertificateManager(self.settings).metadata().get("fingerprint") or ""),
        )
        payload = self._local_network_status_payload()
        payload["announcement_sent"] = sent
        self._send_json(200, payload)

    def _handle_admin_local_pairing_code_rotate(self) -> None:
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Enable Local Network mode before pairing"})
            return
        self._send_json(200, {"pairing": _local_network.pairing_code(self.settings, rotate=True)})

    def _handle_admin_local_peer_pair(self, peer_id: str, payload: dict) -> None:
        peer_id = unquote(peer_id)
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Enable Local Network mode before pairing"})
            return
        peer = next(
            (row for row in _local_network.discovered_peers(self.settings, include_stale=True) if str(row.get("drone_id") or "") == peer_id),
            None,
        )
        if not peer:
            self._send_json(404, {"error": "discovered peer not found"})
            return
        paired = _local_pair_peer(self.settings, peer, str(payload.get("pairing_code") or ""))
        ssl_context = getattr(self.server, "ssl_context", None)
        cert_path = Path(str(paired.get("certificate_path") or ""))
        if ssl_context is not None and cert_path.exists():
            try:
                ssl_context.load_verify_locations(cafile=str(cert_path))
            except ssl.SSLError:
                pass
        self._send_json(200, {"status": "paired", "peer": _public_local_peer(paired)})

    def _handle_admin_local_peer_forget(self, peer_id: str) -> None:
        peer_id = unquote(peer_id)
        removed = _local_network.forget_peer(self.settings, peer_id)
        _local_peer_cert_cache_path(self.settings, peer_id).unlink(missing_ok=True)
        self._send_json(200, {"status": "forgotten" if removed else "not_found", "peer_id": peer_id})

    def _handle_admin_local_peer_assets(self, peer_id: str, query_params: dict) -> None:
        peer_id = unquote(peer_id)
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Enable Local Network mode before browsing peers"})
            return
        peer = _local_network.get_paired_peer(self.settings, peer_id)
        if not peer:
            self._send_json(404, {"error": "paired peer not found"})
            return
        asset_type = str((query_params.get("type") or ["summary"])[0]).strip().lower()
        if self.settings.use_fake_data and peer.get("fake_data"):
            result = self._collect_peer_inventory(asset_type, query_params)
        else:
            params = []
            for key in ("system", "systems", "q", "limit", "offset"):
                value = str((query_params.get(key) or [""])[0]).strip()
                if value:
                    params.append(f"{quote(key, safe='')}={quote(value, safe='')}")
            address = _peer_address(peer)
            if not address:
                raise ValueError("paired peer has no reachable address")
            suffix = f"?{'&'.join(params)}" if params else ""
            result = _peer_get_json(
                f"{address}/v1/api/peer/inventory/{quote(asset_type, safe='')}{suffix}",
                self.settings,
                peer_id=peer_id,
                config={"network_mode": "local_network"},
            )
        if asset_type == "roms" and isinstance(result, dict):
            self._annotate_roms_exist_locally(result.get("items") or [])
        self._send_json(200, result)

    def _annotate_roms_exist_locally(self, items: List[dict]) -> None:
        """Flag each peer ROM row with whether it already exists on this machine
        (by content thumbprint) so the UI can show it and skip re-downloading."""
        cache: dict = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            system = str(item.get("system") or "").strip()
            if not system:
                item["exists_locally"] = False
                continue
            index = self._local_rom_index(system, cache=cache)
            item["exists_locally"] = self._match_local_rom(index, item) is not None

    def _local_rom_index(self, system: str, cache: Optional[dict] = None) -> dict:
        """Build a lookup of this machine's ROMs for a system: content
        fingerprint -> relative path, and normalized path -> relative path. Used
        to decide whether a peer ROM already exists locally. Optionally memoized
        in `cache` (system -> index) for bulk operations."""
        if cache is not None and system in cache:
            return cache[system]
        fingerprints: Dict[str, str] = {}
        paths: Dict[str, str] = {}
        names_by_size: Dict[tuple, str] = {}
        try:
            _, rows = self.repository.list_assets(system, "roms")
        except Exception:
            rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            rel = str(row.get("relative_path") or row.get("rom_path") or row.get("file_path") or "")
            rel_norm = rel.replace("\\", "/").lstrip("./").lower()
            fp = str(row.get("fingerprint") or row.get("rom_fingerprint") or "").strip().lower()
            if rel_norm:
                paths.setdefault(rel_norm, rel)
                size = row.get("byte_count") or row.get("file_size") or row.get("size")
                try:
                    size_int = int(size)
                except (TypeError, ValueError):
                    size_int = -1
                names_by_size.setdefault((Path(rel_norm).name, size_int), rel)
            if fp:
                fingerprints.setdefault(fp, rel)
        system_dir = (self.settings.roms_root if getattr(self, "settings", None) else self.repository.roms_root) / system
        if system_dir.exists() and system_dir.is_dir():
            for entry in sorted(system_dir.rglob("*"), key=lambda path: path.relative_to(system_dir).as_posix().lower()):
                if not entry.is_file():
                    continue
                rel = entry.relative_to(system_dir).as_posix()
                if self.repository.should_ignore_rom_path(Path(rel)):
                    continue
                rel_norm = rel.replace("\\", "/").lstrip("./").lower()
                try:
                    size_int = int(entry.stat().st_size)
                except OSError:
                    continue
                paths.setdefault(rel_norm, rel)
                names_by_size.setdefault((Path(rel_norm).name, size_int), rel)
                try:
                    fingerprints.setdefault(self.repository.build_fingerprint(entry).lower(), rel)
                except Exception:
                    continue
        index = {"fingerprints": fingerprints, "paths": paths, "names_by_size": names_by_size}
        if cache is not None:
            cache[system] = index
        return index

    def _ensure_system_writable_once(self, system: str) -> None:
        """Ensure this ROM system's media dirs + gamelist are writable by the Drone,
        at most once per request (a bulk copy touches one system many times)."""
        system = str(system or "").strip()
        if not system:
            return
        done = getattr(self, "_perm_repaired_systems", None)
        if done is None:
            done = set()
            self._perm_repaired_systems = done
        if system in done:
            return
        done.add(system)
        try:
            _ensure_rom_write_access(self.settings, system)
        except Exception:
            pass

    def _match_local_rom(self, index: dict, item: dict) -> Optional[str]:
        """Return the local relative path of a ROM matching this peer item, or
        None. Prefers a content-fingerprint (thumbprint) match; falls back to a
        path match only when no fingerprint is available to compare."""
        peer_fp = str(item.get("rom_fingerprint") or item.get("fingerprint") or "").strip().lower()
        if peer_fp:
            match = index.get("fingerprints", {}).get(peer_fp)
            if match:
                return match
        rel = str(item.get("relative_path") or item.get("rom_path") or item.get("file_path") or "")
        rel_norm = rel.replace("\\", "/").lstrip("./").lower()
        match = index.get("paths", {}).get(rel_norm)
        if match:
            return match
        size = item.get("byte_count") or item.get("file_size") or item.get("size")
        try:
            size_int = int(size)
        except (TypeError, ValueError):
            size_int = -1
        if size_int >= 0 and rel_norm:
            return index.get("names_by_size", {}).get((Path(rel_norm).name, size_int))
        return None

    def _enqueue_local_asset(
        self,
        manager: "DownloadManager",
        config: dict,
        peer: dict,
        asset_type: str,
        item: dict,
        default_system: str = "",
        include_artwork: bool = True,
        local_index_cache: Optional[dict] = None,
    ) -> List[dict]:
        """Enqueue a single peer asset (and, for ROMs, its artwork when present).

        ROMs already present on this machine (matched by content fingerprint, or
        path when no fingerprint is available) are NOT re-downloaded; their artwork
        is still copied (overwriting any same-named file) and linked into the local
        gamelist. Returns the list of jobs created. Shared by the single-item sync
        and the bulk "copy all" handlers. `local_index_cache` (system -> index) lets
        the bulk path reuse the local ROM lookup across many items."""
        jobs: List[dict] = []
        if asset_type == "roms":
            system = str(item.get("system") or default_system or "").strip()
            relative_path = str(item.get("relative_path") or item.get("rom_path") or item.get("file_path") or "").strip()
            if not relative_path:
                return jobs
            index = self._local_rom_index(system, cache=local_index_cache)
            local_match = self._match_local_rom(index, item)
            if local_match is None:
                pending_match = (
                    manager.find_pending_rom(system, relative_path, item.get("rom_fingerprint") or item.get("fingerprint"))
                    if hasattr(manager, "find_pending_rom")
                    else None
                )
            else:
                pending_match = None
            if local_match is None and pending_match is None:
                # Not on this machine yet -> download it.
                jobs.append(manager.enqueue_rom(
                    config,
                    peer,
                    system,
                    relative_path,
                    expected_size=item.get("byte_count") or item.get("file_size"),
                    expected_fingerprint=item.get("rom_fingerprint") or item.get("fingerprint"),
                    entry_type=str(item.get("entry_type") or "file"),
                ))
                art_local_path = relative_path
            else:
                # Already present (thumbprint match) -> skip the ROM, attach artwork
                # to the existing local ROM so it shows after a gamelist refresh.
                art_local_path = local_match or relative_path
            if include_artwork:
                gamelist = item.get("gamelist") if isinstance(item.get("gamelist"), dict) else {}
                fields = [field for field in ARTWORK_FIELDS if gamelist.get(field)]
                if fields:
                    # Make the target system's media dirs + gamelist.xml writable
                    # for the unprivileged Drone before the artwork jobs run. Done
                    # once per system, synchronously, so the download worker doesn't
                    # race ahead of the privileged permission repair.
                    self._ensure_system_writable_once(system)
                for field in fields:
                    try:
                        jobs.append(manager.enqueue_artwork(
                            config, peer, system, relative_path, field,
                            overwrite=True, local_rom_path=art_local_path,
                        ))
                    except Exception:
                        continue
        elif asset_type == "bios":
            relative_path = str(item.get("path") or item.get("relative_path") or item.get("file_path") or "").strip()
            if not relative_path:
                return jobs
            jobs.append(manager.enqueue_bios(
                config,
                peer,
                relative_path,
                expected_size=item.get("byte_count") or item.get("file_size"),
                expected_md5=item.get("bios_md5") or item.get("md5"),
            ))
        elif asset_type == "artwork":
            artwork_types = item.get("artwork_types")
            if isinstance(artwork_types, list):
                default_artwork_type = str(artwork_types[0] if artwork_types else "image")
            else:
                default_artwork_type = str(artwork_types or "image")
            system = str(item.get("system") or default_system or "")
            self._ensure_system_writable_once(system)
            jobs.append(manager.enqueue_artwork(
                config,
                peer,
                system,
                str(item.get("rom_path") or item.get("file_path") or ""),
                str(item.get("artwork_type") or default_artwork_type),
                overwrite=True,
            ))
        elif asset_type == "saves":
            relative_path = str(item.get("file_path") or item.get("relative_path") or "").strip()
            if not relative_path:
                return jobs
            jobs.append(manager.enqueue_save(
                config,
                peer,
                str(item.get("system") or default_system or ""),
                relative_path,
                expected_size=item.get("file_size"),
                expected_fingerprint=item.get("saves_fingerprint") or item.get("fingerprint"),
            ))
        else:
            raise ValueError("asset_type must be roms, bios, artwork, or saves")
        return jobs

    def _handle_admin_local_sync(self, payload: dict) -> None:
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Enable Local Network mode before syncing assets"})
            return
        peer_id = str(payload.get("peer_id") or "").strip()
        asset_type = str(payload.get("asset_type") or "").strip().lower()
        item = payload.get("item") if isinstance(payload.get("item"), dict) else payload
        include_artwork = bool(payload.get("include_artwork", True))
        peer = _local_network.get_paired_peer(self.settings, peer_id)
        manager = _get_download_manager()
        if not peer:
            self._send_json(404, {"error": "paired peer not found"})
            return
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        config = {"network_mode": "local_network"}
        jobs = self._enqueue_local_asset(
            manager,
            config,
            peer,
            asset_type,
            item,
            default_system=str(payload.get("system") or ""),
            include_artwork=include_artwork,
        )
        rom_skipped = asset_type == "roms" and not any(job.get("file_type") == "ROM" for job in jobs)
        self._send_json(202, {
            "status": "queued",
            "job": jobs[0] if jobs else None,
            "jobs": jobs,
            "rom_skipped": rom_skipped,
        })

    def _handle_admin_local_sync_bulk(self, payload: dict) -> None:
        """Copy every item of an asset type from a paired peer.

        Pages through the peer's inventory server-side (so it works regardless of
        the UI's current page) and enqueues each transferable item. Optionally
        scoped to a single system and/or a search query. For ROMs, artwork is
        enqueued alongside each ROM when include_artwork is set."""
        if not _local_network.is_local_mode(self.settings):
            self._send_json(409, {"error": "Enable Local Network mode before syncing assets"})
            return
        peer_id = str(payload.get("peer_id") or "").strip()
        asset_type = str(payload.get("asset_type") or "").strip().lower()
        if asset_type not in {"roms", "bios", "artwork", "saves"}:
            self._send_json(400, {"error": "Bulk copy supports roms, bios, artwork, or saves"})
            return
        system = str(payload.get("system") or "").strip()
        systems = [
            str(value).strip()
            for value in (payload.get("systems") or [])
            if str(value).strip()
        ]
        query = str(payload.get("q") or "").strip()
        include_artwork = bool(payload.get("include_artwork", True))
        peer = _local_network.get_paired_peer(self.settings, peer_id)
        manager = _get_download_manager()
        if not peer:
            self._send_json(404, {"error": "paired peer not found"})
            return
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        config = {"network_mode": "local_network"}
        page_size = 500
        offset = 0
        queued_assets = 0
        queued_artwork = 0
        skipped_existing = 0
        total = None
        local_index_cache: dict = {}
        while True:
            params = [f"limit={page_size}", f"offset={offset}"]
            if system:
                params.append(f"system={quote(system, safe='')}")
            if systems:
                params.append(f"systems={quote(','.join(systems), safe='')}")
            if query:
                params.append(f"q={quote(query, safe='')}")
            inventory = self._fetch_peer_inventory(peer, peer_id, asset_type, params)
            items = inventory.get("items") or []
            if total is None:
                total = inventory.get("total")
            if not items:
                break
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                jobs = self._enqueue_local_asset(
                    manager,
                    config,
                    peer,
                    asset_type,
                    entry,
                    default_system=system,
                    include_artwork=include_artwork,
                    local_index_cache=local_index_cache,
                )
                asset_jobs = [job for job in jobs if job.get("file_type") != "ARTWORK"]
                queued_assets += len(asset_jobs)
                queued_artwork += len(jobs) - len(asset_jobs)
                if asset_type == "roms" and not asset_jobs:
                    skipped_existing += 1
            offset += len(items)
            if total is not None and offset >= int(total):
                break
            if len(items) < page_size:
                break
        self._send_json(202, {
            "status": "queued",
            "asset_type": asset_type,
            "system": system or None,
            "systems": systems,
            "queued_assets": queued_assets,
            "queued_artwork": queued_artwork,
            "skipped_existing": skipped_existing,
            "total_available": total,
        })

    def _fetch_peer_inventory(self, peer: dict, peer_id: str, asset_type: str, params: List[str]) -> dict:
        """Fetch a page of a peer's inventory, transparently handling the
        fake-data local peer (used in tests/dev) the same way browsing does."""
        if self.settings.use_fake_data and peer.get("fake_data"):
            query_params: dict = {}
            for raw in params:
                if "=" in raw:
                    key, value = raw.split("=", 1)
                    query_params[unquote(key)] = [unquote(value)]
            return self._collect_peer_inventory(asset_type, query_params)
        address = _peer_address(peer)
        if not address:
            raise ValueError("paired peer has no reachable address")
        suffix = f"?{'&'.join(params)}" if params else ""
        return _peer_get_json(
            f"{address}/v1/api/peer/inventory/{quote(asset_type, safe='')}{suffix}",
            self.settings,
            peer_id=peer_id,
            config={"network_mode": "local_network"},
        )

    def _handle_admin_credentials_update(self, payload: dict) -> None:
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if not getattr(self.auth, "credential_store", None):
            raise ValueError("credential storage is not available")
        result = self.auth.credential_store.update(username, password)
        self._send_json(200, {"credentials": result, "message": "Drone credentials updated."})

    def _handle_admin_overmind_config(self, payload: dict) -> None:
        raw_url = str(payload.get("overmind_url") or "").strip()
        raw_email = str(payload.get("overmind_email") or "").strip()
        raw_drone_name = str(payload.get("drone_name") or "").strip()
        raw_password = payload.get("overmind_password")
        raw_auth_token = payload.get("overmind_auth_token")
        raw_token = payload.get("overmind_token")

        if not raw_url:
            raise ValueError("overmind_url is required")
        parsed = urlparse(raw_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("overmind_url must be a valid http/https URL")
        if raw_email and ("@" not in raw_email or raw_email.startswith("@") or raw_email.endswith("@")):
            raise ValueError("overmind_email must be a valid email address")

        existing = self._load_overmind_config()
        new_config = dict(existing)
        new_config["overmind_url"] = raw_url.rstrip("/")
        new_config["overmind_email"] = raw_email
        new_config["drone_name"] = raw_drone_name or socket.gethostname()
        claim_password = str(raw_password or "") if raw_password is not None else ""
        if raw_password is not None and not claim_password:
            raise ValueError("overmind_password cannot be empty when provided")
        if raw_auth_token is not None:
            auth_token_value = str(raw_auth_token)
            if not auth_token_value:
                raise ValueError("overmind_auth_token cannot be empty when provided")
            new_config["overmind_auth_token"] = auth_token_value
            new_config.pop("overmind_token", None)
            new_config["swarm_connection_status"] = "disconnected"
            self._save_json_state(self._overmind_swarm_path(), [])
            self._save_json_state(self._overmind_peer_results_path(), [])
        if raw_token is not None:
            token_value = str(raw_token)
            if not token_value:
                raise ValueError("overmind_token cannot be empty when provided")
            new_config["overmind_token"] = token_value
        if not str(new_config.get("overmind_auth_token") or "").strip() and not str(new_config.get("overmind_token") or "").strip():
            raise ValueError("authorization token is required to connect this Drone to Overmind")
        new_config["requested_at"] = self._now_iso()
        new_config["integration_state"] = "configured"
        new_config["last_error"] = None
        new_config["notes"] = "Configuration saved. Drone will report heartbeat and collect Overmind actions on its polling interval."
        overmind_active = _local_network.is_overmind_mode(self.settings)
        if new_config.get("overmind_auth_token") and overmind_active:
            base_url = str(new_config.get("overmind_url") or "").strip().rstrip("/")
            new_config["integration_enabled"] = True
            token = _register_or_claim_overmind_token(self.settings, self.repository, new_config, base_url)
            if token:
                new_config = self._load_overmind_config()
            else:
                refreshed = self._load_overmind_config()
                if refreshed.get("integration_state") == "pending_approval":
                    new_config = refreshed
                    new_config["integration_enabled"] = True
                else:
                    new_config["integration_enabled"] = False
        elif new_config.get("overmind_auth_token"):
            new_config["integration_enabled"] = False
            new_config["integration_state"] = "disabled"
            new_config["notes"] = "Configuration saved. Enable Overmind integration to connect this Drone."
        if raw_password is not None:
            if not overmind_active:
                raise ValueError("enable Overmind integration before claiming ownership")
            if parsed.scheme != "https":
                raise ValueError("claim ownership requires an https Overmind URL")
            if not raw_email:
                raise ValueError("overmind_email is required to claim ownership")
            if not str(new_config.get("overmind_token") or "").strip():
                raise ValueError("authorization token is required before claiming ownership")
            base_url = raw_url.rstrip("/")
            network_payload = _drone_network_payload(self.settings)
            claim_payload = {
                "device_id": self.settings.overmind_device_id,
                "device_name": new_config["drone_name"],
                "email": raw_email,
                "password": claim_password,
                "network": network_payload,
                "api_port": _drone_advertised_api_port(self.settings),
                "scheme": _drone_scheme(self.settings),
                "reachable_url": _drone_reachable_url(self.settings, network_payload),
                "certificate": DroneCertificateManager(self.settings).metadata(),
                "system_info": _collect_system_info_payload(self.settings),
            }
            print(
                f"Overmind ownership claim requested for {self.settings.overmind_device_id}: endpoint={base_url}/api/drones/claim-ownership",
                file=sys.stdout,
                flush=True,
            )
            try:
                status_code, response = _overmind_post_json_with_status(
                    f"{base_url}/api/drones/claim-ownership",
                    claim_payload,
                    settings=self.settings,
                )
            except HTTPError as error:
                print(
                    f"Overmind ownership claim failed for {self.settings.overmind_device_id}: status={error.code}",
                    file=sys.stderr,
                    flush=True,
                )
                self._send_json(error.code if 400 <= error.code < 600 else 502, {"error": "ownership claim failed"})
                return
            except Exception as error:
                print(
                    f"Overmind ownership claim failed for {self.settings.overmind_device_id}: {_format_overmind_error(error)}",
                    file=sys.stderr,
                    flush=True,
                )
                self._send_json(502, {"error": "ownership claim failed"})
                return
            if status_code >= 400:
                self._send_json(status_code, {"error": "ownership claim failed"})
                return
            new_config["claimed_at"] = self._now_iso()
            new_config["ownership_claim_status"] = response.get("status") or "claimed"
            new_config["notes"] = "Configuration saved. Ownership claim recorded in Overmind; authorization token remains the Drone connection credential."
            new_config.pop("overmind_password", None)
        self._save_overmind_config(new_config)
        self._send_json(200, self._overmind_public_payload(new_config))

    def _handle_admin_overmind_start(self, payload: dict) -> None:
        if not self._require_overmind_mode():
            return
        config = self._load_overmind_config()
        password = str(config.get("overmind_password") or "")
        auth_token = str(config.get("overmind_auth_token") or "")
        token = str(config.get("overmind_token") or "")
        if not str(config.get("overmind_url") or "").strip():
            raise ValueError("overmind_url is not configured")
        if not token and not auth_token:
            raise ValueError("overmind authorization token is not configured")

        if "overmind_password" in payload:
            supplied = str(payload.get("overmind_password") or "")
            if not supplied:
                raise ValueError("overmind_password cannot be empty")
            config["overmind_password"] = supplied
            password = supplied
        if "overmind_token" in payload:
            supplied_token = str(payload.get("overmind_token") or "")
            if not supplied_token:
                raise ValueError("overmind_token cannot be empty")
            config["overmind_token"] = supplied_token
        if "overmind_auth_token" in payload:
            supplied_auth = str(payload.get("overmind_auth_token") or "")
            if not supplied_auth:
                raise ValueError("overmind_auth_token cannot be empty")
            config["overmind_auth_token"] = supplied_auth

        config["integration_enabled"] = True
        config["integration_state"] = "polling"
        config["swarm_connection_status"] = "connected"
        config["last_started_at"] = self._now_iso()
        config["last_error"] = None
        config["notes"] = (
            "Integration active. Drone periodically calls Overmind, claims actions, performs local collection, "
            "and posts completion results back to the Overmind API."
        )
        self._save_overmind_config(config)
        self._send_json(200, self._overmind_public_payload(config))

    def _handle_admin_overmind_claim_ownership(self, payload: dict) -> None:
        if not self._require_overmind_mode():
            return
        raw_url = str(payload.get("overmind_url") or "").strip()
        email = str(payload.get("email") or "").strip()
        password = str(payload.get("password") or "")
        drone_name = str(payload.get("drone_name") or "").strip() or socket.gethostname()
        if not raw_url:
            raise ValueError("overmind_url is required")
        parsed = urlparse(raw_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("claim ownership requires an https Overmind URL")
        if not email or "@" not in email or email.startswith("@") or email.endswith("@"):
            raise ValueError("email must be a valid email address")
        if not password:
            raise ValueError("password is required")

        base_url = raw_url.rstrip("/")
        network_payload = _drone_network_payload(self.settings)
        claim_payload = {
            "device_id": self.settings.overmind_device_id,
            "device_name": drone_name,
            "email": email,
            "password": password,
            "network": network_payload,
            "api_port": _drone_advertised_api_port(self.settings),
            "scheme": _drone_scheme(self.settings),
            "reachable_url": _drone_reachable_url(self.settings, network_payload),
            "certificate": DroneCertificateManager(self.settings).metadata(),
            "system_info": _collect_system_info_payload(self.settings),
        }
        print(
            f"Overmind ownership claim requested for {self.settings.overmind_device_id}: endpoint={base_url}/api/drones/claim-ownership",
            file=sys.stdout,
            flush=True,
        )
        try:
            status_code, response = _overmind_post_json_with_status(
                f"{base_url}/api/drones/claim-ownership",
                claim_payload,
                settings=self.settings,
            )
        except HTTPError as error:
            print(
                f"Overmind ownership claim failed for {self.settings.overmind_device_id}: status={error.code}",
                file=sys.stderr,
                flush=True,
            )
            self._send_json(error.code if 400 <= error.code < 600 else 502, {"error": "ownership claim failed"})
            return
        except Exception as error:
            print(
                f"Overmind ownership claim failed for {self.settings.overmind_device_id}: {_format_overmind_error(error)}",
                file=sys.stderr,
                flush=True,
            )
            self._send_json(502, {"error": "ownership claim failed"})
            return
        token = str(response.get("drone_token") or "").strip()
        if status_code >= 400 or not token:
            self._send_json(status_code if status_code >= 400 else 502, {"error": "ownership claim failed"})
            return

        config = self._load_overmind_config()
        config.update({
            "overmind_url": base_url,
            "overmind_email": email,
            "drone_name": drone_name,
            "overmind_token": token,
            "integration_enabled": True,
            "integration_state": "polling",
            "swarm_connection_status": "connected",
            "claimed_at": self._now_iso(),
            "last_error": None,
            "notes": "Ownership claimed through Overmind credentials. Drone heartbeat and ROM metadata polling are active.",
        })
        config.pop("overmind_password", None)
        self._save_overmind_config(config)
        print(f"Overmind ownership claim succeeded for {self.settings.overmind_device_id}", file=sys.stdout, flush=True)
        self._send_json(200, self._overmind_public_payload(config))

    def _handle_admin_overmind_swarm_connect(self) -> None:
        if not self._require_overmind_mode():
            return
        config = self._load_overmind_config()
        base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
        if not base_url:
            raise ValueError("overmind_url is not configured")
        if not str(config.get("overmind_auth_token") or "").strip():
            raise ValueError("overmind authorization token is not configured")
        config["integration_enabled"] = True
        config["requested_at"] = self._now_iso()
        config["integration_state"] = "approval_requested"
        config["swarm_connection_status"] = "approval requested"
        token = _register_or_claim_overmind_token(self.settings, self.repository, config, base_url)
        refreshed = self._load_overmind_config()
        self._send_json(200, self._overmind_public_payload(refreshed if token or refreshed else config))

    def _handle_admin_overmind_swarm_disconnect(self) -> None:
        if not self._require_overmind_mode():
            return
        config = self._load_overmind_config()
        base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
        token = str(config.get("overmind_token") or "").strip()
        if base_url and token:
            try:
                _overmind_post_json(f"{base_url}/api/devices/{quote(self.settings.overmind_device_id, safe='')}/disconnect", {}, token=token, settings=self.settings)
            except Exception as error:
                config["integration_state"] = "disconnect_failed"
                config["swarm_connection_status"] = "disconnect failed"
                config["last_error"] = _format_overmind_error(error)
                self._save_overmind_config(config)
                self._send_json(502, self._overmind_public_payload(config))
                return
        config["integration_enabled"] = False
        config["integration_state"] = "disconnected"
        config["swarm_connection_status"] = "disconnected"
        config["notes"] = "Drone disconnected from its Overmind swarm. Its retained recovery credential is used only for lightweight heartbeats."
        self._save_overmind_config(config)
        self._save_json_state(self._overmind_swarm_path(), [])
        self._send_json(200, self._overmind_public_payload(config))

    def _handle_admin_api_certificate_rotate(self) -> None:
        config = self._load_overmind_config()
        base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
        token = str(config.get("overmind_token") or "").strip()
        if not base_url or not token:
            raise ValueError("approved Overmind connection is required before certificate rotation")
        manager = DroneCertificateManager(self.settings)
        csr = manager.generate_rotation_csr()
        try:
            signed = _overmind_post_json(
                f"{base_url}/api/devices/{quote(self.settings.overmind_device_id, safe='')}/certificate/sign",
                {"csr_pem": csr["csr_pem"], "days": max(1, int(self.settings.drone_cert_days))},
                token=token,
                settings=self.settings,
            )
            metadata = manager.install_signed_certificate(
                str(signed.get("certificate_pem") or ""),
                csr["pending_key"],
                str(signed.get("ca_certificate_pem") or "") or None,
            )
            self._send_json(200, {"status": "rotated", "certificate": metadata})
        except Exception as error:
            try:
                csr["pending_key"].unlink(missing_ok=True)
                csr["pending_csr"].unlink(missing_ok=True)
            except Exception:
                pass
            self._send_json(502, {"status": "failed", "error": _format_overmind_error(error), "certificate": manager.metadata()})

    def _handle_admin_config(self, config_source: str, max_bytes: int, output_format: str = "json") -> None:
        from pathlib import Path

        requested_source = (config_source or "").strip()
        normalized_source = requested_source.lower()
        safe_max_bytes = max(1024, min(int(max_bytes), 1048576))
        normalized_format = (output_format or "json").strip().lower()

        # Curated set of meaningful configs for Batocera/ES/emulators.
        config_path_candidates = {
            "batocera": ["/userdata/system/batocera.conf"],
            "es_systems": [
                "/userdata/system/configs/emulationstation/es_systems.cfg",
                "/usr/share/emulationstation/es_systems.cfg",
            ],
            "emulationstation": [
                "/userdata/system/.emulationstation/es_settings.cfg",
                "/userdata/system/configs/emulationstation/es_settings.cfg",
            ],
            "es_input": [
                "/userdata/system/.emulationstation/es_input.cfg",
                "/userdata/system/configs/emulationstation/es_input.cfg",
            ],
            "es_gamelists": [
                "/userdata/roms",
                "/userdata/system/.emulationstation/gamelists",
                "/userdata/system/configs/emulationstation/gamelists",
            ],
            "retroarch": [
                "/userdata/system/configs/retroarch/retroarch.cfg",
                "/userdata/system/.config/retroarch/retroarch.cfg",
                "/userdata/system/configs/retroarch/retroarchcustom.cfg",
                "/userdata/system/configs/all/retroarch.cfg",
                "/userdata/system/.emulationstation/es_settings.cfg",
            ],
            "mame": [
                "/userdata/system/configs/mame/mame.ini",
                "/userdata/system/configs/mame/default.cfg",
                "/userdata/system/configs/mame",
            ],
            "dolphin": ["/userdata/system/configs/dolphin-emu/Dolphin.ini"],
            "psx2": [
                "/userdata/system/configs/PCSX2/inis/PCSX2_ui.ini",
                "/userdata/system/configs/PCSX2/inis/PCSX2.ini",
                "/userdata/system/configs/pcsx2/inis/PCSX2_ui.ini",
                "/userdata/system/configs/pcsx2/inis/PCSX2.ini",
            ],
            "pcsx2": [
                "/userdata/system/configs/PCSX2/inis/PCSX2_ui.ini",
                "/userdata/system/configs/PCSX2/inis/PCSX2.ini",
                "/userdata/system/configs/pcsx2/inis/PCSX2_ui.ini",
                "/userdata/system/configs/pcsx2/inis/PCSX2.ini",
            ],
            "rpcs3": ["/userdata/system/configs/rpcs3/config.yml"],
            "ppsspp": ["/userdata/system/configs/ppsspp/PSP/SYSTEM/ppsspp.ini"],
            "duckstation": [
                "/userdata/system/configs/duckstation/settings.ini",
                "/userdata/system/configs/duckstation/duckstation.ini",
                "/userdata/system/configs/duckstation/config/settings.ini",
            ],
            "citra": [
                "/userdata/system/configs/citra-emu/qt-config.ini",
                "/userdata/system/configs/citra-emu/config/qt-config.ini",
                "/userdata/system/configs/citra/config/qt-config.ini",
            ],
            "yuzu": [
                "/userdata/system/configs/yuzu/qt-config.ini",
                "/userdata/system/configs/yuzu/config/qt-config.ini",
            ],
            "ryujinx": [
                "/userdata/system/configs/Ryujinx/Config.json",
                "/userdata/system/configs/ryujinx/Config.json",
                "/userdata/system/configs/Ryujinx/config.json",
                "/userdata/system/configs/ryujinx/config.json",
            ],
            "cemu": ["/userdata/system/configs/cemu/settings.xml"],
            "xemu": ["/userdata/system/configs/xemu/xemu.toml"],
            "xenia": [
                "/userdata/system/configs/xenia/xenia.config.toml",
                "/userdata/system/configs/xenia/xenia-canary.config.toml",
            ],
            "flycast": ["/userdata/system/configs/flycast/emu.cfg"],
            "dosbox": [
                "/userdata/system/configs/dosbox/dosboxx.conf",
                "/userdata/system/configs/dosbox/dosbox.conf",
                "/userdata/system/configs/dosbox/dosbox-0.74.conf",
            ],
            "scummvm": [
                "/userdata/system/configs/scummvm/scummvm.ini",
                "/userdata/system/configs/scummvm/scummvmrc",
                "/userdata/system/.scummvmrc",
            ],
            "snes9x": [
                "/userdata/system/configs/snes9x/snes9x.conf",
                "/userdata/system/configs/snes9x/snes9x-gtk.conf",
            ],
            "bsnes": [
                "/userdata/system/configs/bsnes/settings.bml",
                "/userdata/system/configs/bsnes/bsnes.cfg",
                "/userdata/system/configs/bsnes/config.bml",
            ],
            "fceux": [
                "/userdata/system/configs/fceux/fceux.cfg",
                "/userdata/system/configs/fceux/fceux.conf",
            ],
            "mednafen": [
                "/userdata/system/configs/mednafen/mednafen.cfg",
                "/userdata/system/.mednafen/mednafen.cfg",
            ],
            "mgba": [
                "/userdata/system/configs/mgba/config.ini",
                "/userdata/system/configs/mgba/qt.ini",
            ],
            "wine": [
                "/userdata/system/configs/wine/user.reg",
                "/userdata/system/configs/wine/system.reg",
                "/userdata/system/wine-bottles/system.reg",
                "/userdata/system/wine-bottles/user.reg",
            ],
            "shadps4": [
                "/userdata/system/configs/shadps4/user/config.toml",
                "/userdata/system/configs/shadPS4/user/config.toml",
                "/userdata/system/configs/shadps4/config.toml",
                "/userdata/system/configs/shadPS4/config.toml",
                "/userdata/system/configs/shadps4/shadps4.toml",
                "/userdata/system/configs/shadPS4/shadps4.toml",
            ],
            "themes": ["/userdata/themes"],
            "controllers": ["/userdata/system/configs/emulationstation/es_input.cfg"],
        }
        def _resolve_userdata_path(candidate: str) -> str:
            if candidate == "/userdata":
                return str(self.settings.userdata_root.resolve())
            if candidate.startswith("/userdata/"):
                suffix = candidate[len("/userdata/") :]
                return str((self.settings.userdata_root / suffix).resolve())
            return candidate

        if normalized_source not in config_path_candidates:
            self._send_json(404, {"error": f"Unknown config source: {requested_source}"})
            return

        resolved_candidates = [_resolve_userdata_path(path) for path in config_path_candidates[normalized_source]]

        if normalized_source == "es_systems":
            source_path, systems = _resolve_es_systems_effective(self.settings)
            if source_path is None:
                self._send_json(404, {
                    "error": f"Config path not found for source: {requested_source}",
                    "attempted_paths": resolved_candidates,
                })
                return
            if normalized_format == "xml":
                try:
                    raw_bytes, truncated = _read_file_tail(source_path, safe_max_bytes)
                    raw_text = raw_bytes.decode("utf-8", errors="replace")
                except Exception as error:
                    self._send_json(500, {"error": f"Failed to read config: {str(error)}"})
                    return
                lines = raw_text.splitlines()
                if truncated:
                    lines.insert(0, f"[truncated] showing last {safe_max_bytes} bytes of file")
                self._send_json(
                    200,
                    {
                        "source": normalized_source,
                        "path": str(source_path),
                        "type": "xml",
                        "format": "xml",
                        "max_bytes": safe_max_bytes,
                        "truncated": truncated,
                        "content": lines,
                    },
                )
                return
            parsed_json = {
                "source_file": str(source_path),
                "systems": systems,
                "count": len(systems),
            }
            rendered = json.dumps(parsed_json, indent=2)
            self._send_json(
                200,
                {
                    "source": normalized_source,
                    "path": str(source_path),
                    "type": "json",
                    "format": "json",
                    "max_bytes": safe_max_bytes,
                    "truncated": False,
                    "parsed": parsed_json,
                    "content": rendered.splitlines(),
                },
            )
            return

        selected_path = None
        selected_is_dir = False
        for candidate in resolved_candidates:
            path = Path(candidate)
            if path.exists():
                selected_path = path
                selected_is_dir = path.is_dir()
                break

        def _find_first_file(candidates):
            for candidate in candidates:
                path = Path(candidate)
                if path.exists() and path.is_file():
                    return path
            return None

        # Fallback discovery for sources with diverse Batocera layouts.
        if selected_path is None and normalized_source == "retroarch":
            search_roots = [
                Path(_resolve_userdata_path("/userdata/system/configs")),
                Path(_resolve_userdata_path("/userdata/system/.config")),
                Path(_resolve_userdata_path("/userdata/system")),
            ]
            target_names = {"retroarch.cfg", "retroarchcustom.cfg"}
            for root in search_roots:
                if not root.exists() or not root.is_dir():
                    continue
                checked = 0
                try:
                    for path in root.rglob("*"):
                        checked += 1
                        if checked > 4000:
                            break
                        if path.is_file() and path.name.lower() in target_names:
                            selected_path = path
                            selected_is_dir = False
                            break
                    if selected_path is not None:
                        break
                except Exception:
                    continue

        # Generic fallback discovery for known emulator config formats.
        if selected_path is None:
            discovery_filenames = {
                "psx2": {"pcsx2_ui.ini", "pcsx2.ini"},
                "pcsx2": {"pcsx2_ui.ini", "pcsx2.ini"},
                "duckstation": {"settings.ini", "duckstation.ini"},
                "citra": {"qt-config.ini"},
                "yuzu": {"qt-config.ini"},
                "ryujinx": {"config.json"},
                "xenia": {"xenia.config.toml", "xenia-canary.config.toml"},
                "dosbox": {"dosboxx.conf", "dosbox.conf", "dosbox-0.74.conf"},
                "scummvm": {"scummvm.ini", "scummvmrc"},
                "snes9x": {"snes9x.conf", "snes9x-gtk.conf"},
                "bsnes": {"settings.bml", "config.bml", "bsnes.cfg"},
                "fceux": {"fceux.cfg", "fceux.conf"},
                "mednafen": {"mednafen.cfg"},
                "mgba": {"config.ini", "qt.ini"},
                "wine": {"user.reg", "system.reg"},
                "shadps4": {"config.toml", "shadps4.toml"},
            }
            root_hints = {
                "psx2": {"pcsx2"},
                "pcsx2": {"pcsx2"},
                "duckstation": {"duckstation"},
                "citra": {"citra"},
                "yuzu": {"yuzu"},
                "ryujinx": {"ryujinx"},
                "xenia": {"xenia"},
                "dosbox": {"dosbox"},
                "scummvm": {"scummvm"},
                "snes9x": {"snes9x"},
                "bsnes": {"bsnes"},
                "fceux": {"fceux"},
                "mednafen": {"mednafen"},
                "mgba": {"mgba"},
                "wine": {"wine", "wine-bottles"},
                "shadps4": {"shadps4"},
            }
            if normalized_source in discovery_filenames:
                targets = discovery_filenames[normalized_source]
                hints = root_hints.get(normalized_source, set())
                search_roots = [
                    Path(_resolve_userdata_path("/userdata/system/configs")),
                    Path(_resolve_userdata_path("/userdata/system/.config")),
                    Path(_resolve_userdata_path("/userdata/system")),
                    Path(_resolve_userdata_path("/userdata")),
                ]
                best_match = None
                for root in search_roots:
                    if not root.exists() or not root.is_dir():
                        continue
                    checked = 0
                    try:
                        for path in root.rglob("*"):
                            checked += 1
                            if checked > 10000:
                                break
                            if not path.is_file():
                                continue
                            file_name = path.name.lower()
                            if file_name not in targets:
                                continue
                            full = str(path).lower()
                            if hints and not any(h in full for h in hints):
                                continue
                            if best_match is None or len(str(path)) < len(str(best_match)):
                                best_match = path
                    except Exception:
                        continue
                if best_match is not None:
                    selected_path = best_match
                    selected_is_dir = False

        if selected_path is None and normalized_source == "es_gamelists":
            # Prefer actual gamelist XML files from /userdata/roms trees.
            roms_root = Path(_resolve_userdata_path("/userdata/roms"))
            if roms_root.exists() and roms_root.is_dir():
                checked = 0
                found = []
                try:
                    for path in roms_root.rglob("gamelist.xml"):
                        checked += 1
                        if checked > 2000:
                            break
                        if path.is_file():
                            found.append(path)
                            if len(found) >= 100:
                                break
                except Exception:
                    found = []
                if found:
                    selected_path = roms_root
                    selected_is_dir = True

        # Last chance for controller config alias.
        if selected_path is None and normalized_source == "controllers":
            selected_path = _find_first_file([
                _resolve_userdata_path("/userdata/system/configs/emulationstation/es_input.cfg"),
                _resolve_userdata_path("/userdata/system/.emulationstation/es_input.cfg"),
            ])
            selected_is_dir = bool(selected_path and selected_path.is_dir())

        if selected_path is None:
            self._send_json(404, {
                "error": f"Config path not found for source: {requested_source}",
                "attempted_paths": resolved_candidates,
            })
            return

        try:
            if selected_is_dir:
                entries = []
                if normalized_source == "es_gamelists" and selected_path == Path(_resolve_userdata_path("/userdata/roms")):
                    checked = 0
                    for gamelist in sorted(selected_path.rglob("gamelist.xml")):
                        checked += 1
                        if checked > 500:
                            entries.append("... (truncated gamelist.xml results)")
                            break
                        rel = gamelist.relative_to(selected_path)
                        entries.append(f"[file] {rel}")
                else:
                    for child in sorted(selected_path.iterdir(), key=lambda p: p.name.lower()):
                        kind = "dir" if child.is_dir() else "file"
                        entries.append(f"[{kind}] {child.name}")
                        if len(entries) >= 500:
                            entries.append("... (truncated directory listing)")
                            break
                self._send_json(200, {
                    "source": normalized_source,
                    "path": str(selected_path),
                    "type": "directory",
                    "max_bytes": safe_max_bytes,
                    "truncated": len(entries) > 500,
                    "content": entries,
                })
                return

            raw, truncated = _read_file_tail(selected_path, safe_max_bytes)
            text = raw.decode("utf-8", errors="replace")
            lines = text.splitlines()
            if truncated:
                lines.insert(0, f"[truncated] showing last {safe_max_bytes} bytes of file")

            self._send_json(200, {
                "source": normalized_source,
                "path": str(selected_path),
                "type": "file",
                "max_bytes": safe_max_bytes,
                "truncated": truncated,
                "content": lines,
            })
        except Exception as error:
            self._send_json(500, {"error": f"Failed to read config: {str(error)}"})

    def _detect_emulator_version(self, source: str) -> Optional[str]:
        if self.settings.use_fake_data and source not in {"batocera", "es_systems", "emulationstation", "es_input", "themes", "controllers"}:
            return "Mock 1.0"

        command_candidates = {
            "retroarch": [["retroarch", "--version"]],
            "mame": [["mame", "-help"]],
            "dolphin": [["dolphin-emu", "--version"], ["dolphin", "--version"]],
            "pcsx2": [["pcsx2", "--version"], ["PCSX2", "--version"]],
            "rpcs3": [["rpcs3", "--version"]],
            "ppsspp": [["PPSSPPSDL", "--version"], ["ppsspp", "--version"]],
            "duckstation": [["duckstation-qt", "--version"], ["duckstation", "--version"]],
            "citra": [["citra", "--version"]],
            "yuzu": [["yuzu", "--version"]],
            "ryujinx": [["Ryujinx", "--version"], ["ryujinx", "--version"]],
            "cemu": [["cemu", "--version"]],
            "xemu": [["xemu", "--version"]],
            "xenia": [["xenia", "--version"]],
            "flycast": [["flycast", "--version"]],
            "dosbox": [["dosbox", "--version"], ["dosbox-x", "--version"]],
            "scummvm": [["scummvm", "--version"]],
            "snes9x": [["snes9x", "--version"]],
            "bsnes": [["bsnes", "--version"]],
            "fceux": [["fceux", "--version"]],
            "mednafen": [["mednafen", "-help"]],
            "mgba": [["mgba-qt", "--version"], ["mgba", "--version"]],
            "wine": [["wine", "--version"]],
            "shadps4": [["shadps4", "--version"], ["shadPS4", "--version"]],
        }
        for command in command_candidates.get(source, []):
            executable = shutil.which(command[0])
            if not executable:
                continue
            try:
                result = subprocess.run(
                    [executable, *command[1:]],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
            except Exception:
                continue
            output = (result.stdout or result.stderr or "").strip().splitlines()
            if output:
                return output[0][:120]
        return None

    def _handle_admin_config_sources(self) -> None:
        from pathlib import Path

        def _resolve_userdata_path(candidate: str) -> str:
            if candidate == "/userdata":
                return str(self.settings.userdata_root.resolve())
            if candidate.startswith("/userdata/"):
                suffix = candidate[len("/userdata/") :]
                return str((self.settings.userdata_root / suffix).resolve())
            return candidate

        # Always keep these top-level debugging sources available.
        base_sources = [
            "batocera",
            "es_systems",
            "emulationstation",
            "es_input",
            "themes",
            "controllers",
        ]
        # Emulator sources should appear only when a matching folder or file exists
        # under /userdata/system/configs (strict detection, no fuzzy substring scan).
        emulator_presence_rules = {
            "retroarch": [
                ("retroarch", "dir"),
            ],
            "mame": [
                ("mame", "dir"),
            ],
            "dolphin": [
                ("dolphin-emu", "dir"),
                ("dolphin", "dir"),
            ],
            "pcsx2": [
                ("PCSX2", "dir"),
                ("pcsx2", "dir"),
            ],
            "rpcs3": [
                ("rpcs3", "dir"),
            ],
            "ppsspp": [
                ("ppsspp", "dir"),
            ],
            "duckstation": [
                ("duckstation", "dir"),
            ],
            "citra": [
                ("citra-emu", "dir"),
                ("citra", "dir"),
            ],
            "yuzu": [
                ("yuzu", "dir"),
            ],
            "ryujinx": [
                ("Ryujinx/Config.json", "file"),
                ("ryujinx/Config.json", "file"),
                ("Ryujinx/config.json", "file"),
                ("ryujinx/config.json", "file"),
            ],
            "cemu": [
                ("cemu", "dir"),
            ],
            "xemu": [
                ("xemu", "dir"),
            ],
            "xenia": [
                ("xenia/xenia.config.toml", "file"),
                ("xenia/xenia-canary.config.toml", "file"),
            ],
            "flycast": [
                ("flycast", "dir"),
            ],
            "dosbox": [
                ("dosbox/dosboxx.conf", "file"),
                ("dosbox/dosbox.conf", "file"),
                ("dosbox/dosbox-0.74.conf", "file"),
            ],
            "scummvm": [
                ("scummvm/scummvm.ini", "file"),
                ("scummvm/scummvmrc", "file"),
            ],
            "snes9x": [
                ("snes9x/snes9x.conf", "file"),
                ("snes9x/snes9x-gtk.conf", "file"),
            ],
            "bsnes": [
                ("bsnes/settings.bml", "file"),
                ("bsnes/config.bml", "file"),
                ("bsnes/bsnes.cfg", "file"),
            ],
            "fceux": [
                ("fceux/fceux.cfg", "file"),
                ("fceux/fceux.conf", "file"),
            ],
            "mednafen": [
                ("mednafen/mednafen.cfg", "file"),
            ],
            "mgba": [
                ("mgba/config.ini", "file"),
                ("mgba/qt.ini", "file"),
            ],
            "wine": [
                ("wine/user.reg", "file"),
                ("wine/system.reg", "file"),
            ],
            "shadps4": [
                ("shadps4/user/config.toml", "file"),
                ("shadPS4/user/config.toml", "file"),
                ("shadps4/config.toml", "file"),
                ("shadPS4/config.toml", "file"),
                ("shadps4/shadps4.toml", "file"),
                ("shadPS4/shadps4.toml", "file"),
            ],
        }

        configs_root = Path(_resolve_userdata_path("/userdata/system/configs"))
        discovered = set(base_sources)
        if configs_root.exists() and configs_root.is_dir():
            for source, checks in emulator_presence_rules.items():
                for rel_path, required_kind in checks:
                    path = configs_root / rel_path
                    if required_kind == "dir" and path.exists() and path.is_dir():
                        discovered.add(source)
                        break
                    if required_kind == "file" and path.exists() and path.is_file():
                        discovered.add(source)
                        break

        ordered_sources = base_sources + [source for source in emulator_presence_rules.keys() if source in discovered]
        versions = {source: self._detect_emulator_version(source) for source in ordered_sources}
        self._send_json(
            200,
            {
                "sources": ordered_sources,
                "versions": versions,
                "scan_root": str(configs_root),
            },
        )

    def _handle_admin_emulators(self) -> None:
        self._send_json(200, _list_emulator_config_files(self.settings, max_configs=250))

    def _handle_admin_emulator_file(self, root_name: str, relative_path: str, max_bytes: int) -> None:
        try:
            self._send_json(200, _read_emulator_config_file(self.settings, root_name, relative_path, max_bytes=max_bytes))
        except FileNotFoundError as error:
            self._send_json(404, {"error": str(error)})


def _build_handler(
    settings: Settings,
    auth: BasicAuth,
    repository: RomRepository,
    image_cache: ExpiringLRUCache,
    image_miss_cache: ExpiringKeyCache,
    json_cache: ExpiringLRUCache,
):
    def factory(*args, **kwargs):
        return RomRequestHandler(
            *args,
            settings=settings,
            auth=auth,
            repository=repository,
            image_cache=image_cache,
            image_miss_cache=image_miss_cache,
            json_cache=json_cache,
            **kwargs,
        )

    return factory


def _generate_self_signed_cert(cert_file: Path, key_file: Path) -> None:
    cert_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "openssl",
        "req",
        "-x509",
        "-nodes",
        "-newkey",
        "rsa:2048",
        "-keyout",
        str(key_file),
        "-out",
        str(cert_file),
        "-days",
        "3650",
        "-subj",
        "/CN=localhost",
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _resolve_tls_material(settings: Settings) -> Tuple[Path, Path]:
    cert_file = settings.tls_cert_file
    key_file = settings.tls_key_file

    if cert_file and key_file:
        return cert_file, key_file

    if not settings.tls_self_signed:
        raise RuntimeError("TLS_CERT_FILE and TLS_KEY_FILE are required when TLS_SELF_SIGNED is disabled")

    cert_file = settings.tls_self_signed_dir / "server.crt"
    key_file = settings.tls_self_signed_dir / "server.key"

    if not cert_file.exists() or not key_file.exists():
        _generate_self_signed_cert(cert_file, key_file)

    return cert_file, key_file


class DroneCertificateManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def ensure_certificate(self) -> dict:
        cert_file = self.settings.drone_cert_file
        key_file = self.settings.drone_key_file
        if cert_file.exists() and key_file.exists():
            metadata = self.metadata()
            if metadata.get("status") == "loaded" and metadata.get("renewal_status") != "expired":
                return metadata
        if self.settings.drone_mtls_mode == "managed":
            return {
                "status": "invalid",
                "error": "managed Drone mTLS mode requires pre-provisioned, unexpired certificate and key files",
                "cert_file": str(cert_file),
                "key_file": str(key_file),
            }
        self._generate_local_certificate(cert_file, key_file)
        return self.metadata()

    def _generate_local_certificate(self, cert_file: Path, key_file: Path) -> None:
        cert_file.parent.mkdir(parents=True, exist_ok=True)
        identity = re.sub(r"[^A-Za-z0-9_.:-]+", "-", self.settings.overmind_device_id).strip("-") or "drone"
        common_name = f"batocera-drone-{identity}"
        alt_names = [
            f"DNS:{common_name}",
            "DNS:localhost",
            "IP:127.0.0.1",
        ]
        for override in _hostname_override_values(self.settings):
            if _is_ip_literal(override):
                alt_names.append(f"IP:{override.strip('[]')}")
            else:
                alt_names.append(f"DNS:{override}")
        for ip in _get_local_certificate_ips():
            alt_names.append(f"IP:{ip}")
        san = ",".join(dict.fromkeys(alt_names))
        command = [
            "openssl",
            "req",
            "-x509",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key_file),
            "-out",
            str(cert_file),
            "-days",
            str(max(1, int(self.settings.drone_cert_days))),
            "-subj",
            f"/CN={common_name}",
            "-addext",
            f"subjectAltName={san}",
            "-addext",
            "extendedKeyUsage=serverAuth,clientAuth",
        ]
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            raise RuntimeError(f"failed to generate Drone certificate with openssl: {error}") from error

    def generate_rotation_csr(self) -> dict:
        cert_file = self.settings.drone_cert_file
        key_file = self.settings.drone_key_file
        pending_key = key_file.with_suffix(key_file.suffix + ".pending")
        pending_csr = cert_file.with_suffix(cert_file.suffix + ".csr")
        cert_file.parent.mkdir(parents=True, exist_ok=True)
        identity = re.sub(r"[^A-Za-z0-9_.:-]+", "-", self.settings.overmind_device_id).strip("-") or "drone"
        common_name = f"batocera-drone-{identity}"
        alt_names = ["DNS:localhost", "IP:127.0.0.1"]
        for override in _hostname_override_values(self.settings):
            alt_names.append(f"IP:{override.strip('[]')}" if _is_ip_literal(override) else f"DNS:{override}")
        for ip in _get_local_certificate_ips():
            alt_names.append(f"IP:{ip}")
        command = [
            "openssl", "req", "-nodes", "-newkey", "rsa:2048",
            "-keyout", str(pending_key), "-out", str(pending_csr),
            "-subj", f"/CN={common_name}",
            "-addext", f"subjectAltName={','.join(dict.fromkeys(alt_names))}",
            "-addext", "extendedKeyUsage=serverAuth,clientAuth",
        ]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(pending_key, 0o600)
        return {"csr_pem": pending_csr.read_text(encoding="utf-8"), "pending_key": pending_key, "pending_csr": pending_csr}

    def install_signed_certificate(self, certificate_pem: str, pending_key: Path, ca_certificate_pem: Optional[str] = None) -> dict:
        cert_file = self.settings.drone_cert_file
        key_file = self.settings.drone_key_file
        pending_cert = cert_file.with_suffix(cert_file.suffix + ".signed")
        pending_cert.write_text(certificate_pem, encoding="utf-8")
        ssl.PEM_cert_to_DER_cert(certificate_pem)
        if not pending_key.exists():
            raise RuntimeError("pending private key is missing")
        pending_key.replace(key_file)
        pending_cert.replace(cert_file)
        os.chmod(key_file, 0o600)
        os.chmod(cert_file, 0o644)
        if ca_certificate_pem:
            ca_file = cert_file.with_name("overmind-ca.crt")
            ca_file.write_text(ca_certificate_pem, encoding="utf-8")
            os.chmod(ca_file, 0o644)
        return self.metadata()

    def metadata(self) -> dict:
        cert_file = self.settings.drone_cert_file
        if not cert_file.exists():
            return {"status": "missing", "cert_file": str(cert_file)}
        try:
            pem = cert_file.read_text(encoding="utf-8", errors="ignore")
            der = ssl.PEM_cert_to_DER_cert(pem)
            decoded = ssl._ssl._test_decode_cert(str(cert_file))  # type: ignore[attr-defined]
        except Exception as error:
            return {"status": "invalid", "error": str(error), "cert_file": str(cert_file)}

        def _name(items) -> str:
            parts = []
            for group in items or []:
                for key, value in group:
                    parts.append(f"{key}={value}")
            return ", ".join(parts)

        san = []
        for kind, value in decoded.get("subjectAltName", ()):
            if kind.lower() == "dns":
                san.append(value)
        not_after = decoded.get("notAfter")
        renewal_status = "unknown"
        try:
            expires_at = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            days_left = (expires_at - datetime.now(timezone.utc)).days
            renewal_status = "expired" if days_left < 0 else ("renew_soon" if days_left <= 30 else "valid")
        except Exception:
            days_left = None
        return {
            "status": "loaded",
            "source": "local_self_signed",
            "fingerprint": hashlib.sha256(der).hexdigest(),
            "public_certificate": pem,
            "subject": _name(decoded.get("subject")),
            "issuer": _name(decoded.get("issuer")),
            "serial_number": decoded.get("serialNumber"),
            "san": san,
            "valid_from": decoded.get("notBefore"),
            "valid_until": not_after,
            "days_until_expiry": days_left,
            "registered_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "last_seen": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "renewal_status": renewal_status,
            "identity": self.settings.overmind_device_id,
            "mtls_enabled": self.settings.drone_mtls_enabled,
            "mtls_mode": self.settings.drone_mtls_mode,
        }


def _overmind_config_path_for_settings(settings: Settings) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / "overmind_integration.json").resolve()


def _overmind_actions_path_for_settings(settings: Settings) -> Path:
    return Path(os.environ.get(
        "OVERMIND_ACTION_LOG_FILE",
        str(settings.userdata_root / "system" / "drone-app" / "overmind_actions.log"),
    )).resolve()


def _overmind_swarm_path_for_settings(settings: Settings) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / "overmind_swarm.json").resolve()


def _overmind_peer_results_path_for_settings(settings: Settings) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / "peer_checks.json").resolve()


def _read_json_file(path: Path, fallback):
    try:
        if path.exists() and path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return fallback


def _load_overmind_config_for_settings(settings: Settings) -> dict:
    fake_email = FAKE_OVERMIND_EMAIL if settings.use_fake_data else ""
    fake_password = FAKE_OVERMIND_PASSWORD if settings.use_fake_data else ""
    fake_token = FAKE_OVERMIND_TOKEN if settings.use_fake_data else ""
    default = {
        "overmind_url": (settings.overmind_url or "").strip(),
        "overmind_email": (fake_email if settings.use_fake_data else settings.overmind_email or "").strip(),
        "drone_name": socket.gethostname(),
        "overmind_password": fake_password if settings.use_fake_data else settings.overmind_password or "",
        "overmind_auth_token": "" if settings.use_fake_data else settings.overmind_auth_token or "",
        "overmind_token": fake_token if settings.use_fake_data else settings.overmind_token or "",
        "integration_enabled": bool(settings.overmind_url and (settings.overmind_token or settings.overmind_auth_token or fake_token)),
    }
    path = _overmind_config_path_for_settings(settings)
    loaded = _load_state_payload(
        _state_database_path(settings.userdata_root),
        path.name,
        {},
        legacy_path=path,
    )
    if not isinstance(loaded, dict) or not loaded:
        return default
    merged = dict(default)
    merged.update(loaded)
    if settings.use_fake_data:
        merged["overmind_email"] = FAKE_OVERMIND_EMAIL
        merged["overmind_password"] = FAKE_OVERMIND_PASSWORD
        merged["overmind_token"] = FAKE_OVERMIND_TOKEN
    else:
        _strip_fake_overmind_values(merged)
    return merged


def _strip_fake_overmind_values(config: dict) -> None:
    """Keep previously seeded demo credentials out of real Drone state."""
    if config.get("overmind_email") == FAKE_OVERMIND_EMAIL:
        config["overmind_email"] = ""
    if config.get("overmind_password") == FAKE_OVERMIND_PASSWORD:
        config.pop("overmind_password", None)
    if config.get("overmind_token") == FAKE_OVERMIND_TOKEN:
        config.pop("overmind_token", None)
    if config.get("integration_enabled") and not (config.get("overmind_token") or config.get("overmind_auth_token")):
        config["integration_enabled"] = False
        config["integration_state"] = "not_started"


def _normalize_overmind_link_state(config: dict) -> bool:
    """Reconcile stale onboarding status once an approved Drone token exists."""
    token = str(config.get("overmind_token") or "").strip()
    enabled = bool(config.get("integration_enabled"))
    if not token or not enabled:
        return False

    state = str(config.get("integration_state") or "not_started")
    if state in {"pending_failed", "not_started", "disconnected", "disconnect_failed"}:
        return False

    changed = False
    if state in {"configured", "approval_requested", "pending_approval"}:
        config["integration_state"] = "polling"
        changed = True

    swarm_status = str(config.get("swarm_connection_status") or "")
    if swarm_status != "connected":
        config["swarm_connection_status"] = "connected"
        changed = True

    notes = str(config.get("notes") or "")
    if "Awaiting Overlord approval" in notes:
        config["notes"] = "Drone approved by Overmind and polling is active."
        changed = True

    return changed


def _mark_overmind_auth_failed(settings: Settings, config: dict, error: BaseException) -> None:
    config.pop("overmind_token", None)
    config["integration_enabled"] = False
    config["integration_state"] = "pending_failed"
    config["swarm_connection_status"] = "disconnected"
    config["last_error"] = _format_overmind_error(error)
    config["notes"] = "Overmind authorization token was rejected. Generate a new authorization token and try again."
    _save_overmind_runtime_config(settings, config)
    _save_state_payload(_state_database_path(settings.userdata_root), "overmind_swarm.json", [])
    _save_state_payload(_state_database_path(settings.userdata_root), "peer_checks.json", [])
    _overmind_swarm_path_for_settings(settings).unlink(missing_ok=True)
    _overmind_peer_results_path_for_settings(settings).unlink(missing_ok=True)


def _safe_token_fingerprint(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return "none"
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:12]


def _overmind_onboarding_context(settings: Settings, config: dict, base_url: str, payload: Optional[dict] = None) -> dict:
    email = str(config.get("overmind_email") or "").strip()
    auth_token = str(config.get("overmind_auth_token") or "").strip()
    drone_token = str(config.get("overmind_token") or "").strip()
    request_payload = payload if isinstance(payload, dict) else {}
    certificate = ((request_payload.get("batocera_info") or {}).get("certificate") or {}) if isinstance(request_payload.get("batocera_info"), dict) else {}
    return {
        "endpoint": f"{base_url.rstrip('/')}/api/devices/register" if base_url else "/api/devices/register",
        "device_id": settings.overmind_device_id,
        "drone_name": str(config.get("drone_name") or "").strip() or socket.gethostname(),
        "email_hint_present": bool(email),
        "email_hint_domain": email.split("@", 1)[1].lower() if "@" in email else "",
        "auth_token_present": bool(auth_token),
        "auth_token_fingerprint": _safe_token_fingerprint(auth_token),
        "stored_drone_token_present": bool(drone_token),
        "payload_authorization_token_present": bool(request_payload.get("authorization_token")),
        "header_authorization_token_present": bool(auth_token),
        "certificate_fingerprint_present": bool(certificate.get("fingerprint") or certificate.get("sha256_fingerprint")),
    }


def _log_overmind_onboarding(message: str, context: dict, *, error: Optional[BaseException] = None) -> None:
    safe_context = json.dumps(context, sort_keys=True)
    suffix = f" error={_format_overmind_error(error)}" if error else ""
    print(f"{message}: {safe_context}{suffix}", file=sys.stderr if error else sys.stdout, flush=True)


def _get_router_ip_address() -> Optional[str]:
    return _build_router_ip_address(run_command=subprocess.run)


def _get_local_ip_addresses() -> dict:
    return _build_local_ip_addresses(
        socket_module=socket,
        gateway_loader=_get_router_ip_address,
        open_url=urlopen,
        request_factory=Request,
    )


def _get_local_certificate_ips() -> List[str]:
    return _build_local_certificate_ips(socket_module=socket)


def _drone_report_host(settings: Settings, network: Optional[dict] = None) -> str:
    return _build_drone_report_host(settings, network, network_loader=_get_local_ip_addresses)


def _drone_reachable_url(settings: Settings, network: Optional[dict] = None) -> str:
    return _build_drone_reachable_url(settings, network, report_host=_drone_report_host)


def _drone_network_payload(settings: Settings) -> dict:
    return _build_drone_network_payload(settings, network_loader=_get_local_ip_addresses)


def _drone_advertised_api_port(settings: Settings) -> int:
    return int(settings.advertised_api_port or settings.https_port)


def _mock_userdata_marker(userdata_root: Path) -> Path:
    return userdata_root / "system" / "drone-app" / "mock_userdata_seeded.json"


def _looks_like_pure_mock_userdata(userdata_root: Path) -> bool:
    roms_root = userdata_root / "roms"
    if not roms_root.exists():
        return False
    known_fake_files = {
        roms_root / "snes" / "Chrono Trigger (USA).zip": b"FAKE-SNES-ROM-1",
        roms_root / "snes" / "Super Mario World (USA).zip": b"FAKE-SNES-ROM-2",
        roms_root / "snes" / "The Legend of Zelda - A Link to the Past (USA).zip": b"FAKE-SNES-ROM-3",
        roms_root / "gba" / "Metroid Fusion (USA).zip": b"FAKE-GBA-ROM-1",
        roms_root / "gba" / "Mario Kart Super Circuit (USA).zip": b"FAKE-GBA-ROM-2",
        roms_root / "psx" / "Castlevania - Symphony of the Night (USA).chd": b"FAKE-PSX-ROM-1",
    }
    has_known_fake = False
    for path, expected in known_fake_files.items():
        try:
            if path.exists() and path.read_bytes() == expected:
                has_known_fake = True
        except OSError:
            continue
    seeded = _load_state_payload(
        _state_database_path(userdata_root),
        "mock_userdata_seeded",
        None,
        legacy_path=_mock_userdata_marker(userdata_root),
    )
    if not (has_known_fake or seeded):
        return False

    for path in roms_root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.lower() == "gamelist.xml" or "/images/" in path.as_posix() or "/videos/" in path.as_posix():
            continue
        expected = known_fake_files.get(path)
        if expected is None:
            return False
        try:
            if path.read_bytes() != expected:
                return False
        except OSError:
            return False
    return True


def _real_data_roots(settings: Settings) -> Tuple[Path, Path]:
    if os.environ.get("ROMS_ROOT") or os.environ.get("BIOS_ROOT"):
        return settings.roms_root, settings.bios_root
    if settings.use_fake_data or not _looks_like_pure_mock_userdata(settings.userdata_root):
        return settings.roms_root, settings.bios_root
    empty_root = settings.userdata_root / "system" / "drone-app" / "real-data-empty"
    return empty_root / "roms", empty_root / "bios"


def _record_processed_overmind_action(
    settings: Settings,
    action: dict,
    status_value: str,
    message: str,
    result: Optional[dict] = None,
) -> None:
    entry = {
            "id": action.get("id"),
            "device_id": settings.overmind_device_id,
            "action": action.get("action"),
            "status": status_value,
            "message": message,
            "result_summary": _summarize_overmind_result(result),
            "processed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "fake_data": settings.use_fake_data,
    }
    legacy_path = _overmind_actions_path_for_settings(settings)
    _load_state_events(
        _state_database_path(settings.userdata_root),
        "overmind_actions",
        legacy_path=legacy_path,
    )
    _append_state_event(
        _state_database_path(settings.userdata_root),
        "overmind_actions",
        entry,
        max_events=500,
    )


def _network_mode(settings: Settings) -> str:
    return _local_network.get_mode(settings)


def _certificate_pem_fingerprint(pem: str) -> str:
    der = ssl.PEM_cert_to_DER_cert(str(pem or ""))
    return hashlib.sha256(der).hexdigest()


def _public_local_peer(peer: dict) -> dict:
    return {key: value for key, value in dict(peer or {}).items() if key not in {"certificate_path"}}


def _save_local_peer_certificate(settings: Settings, peer_id: str, certificate_pem: str) -> Tuple[Path, str]:
    if "BEGIN CERTIFICATE" not in str(certificate_pem or ""):
        raise ValueError("peer certificate is required")
    fingerprint = _certificate_pem_fingerprint(certificate_pem)
    cert_path = _local_peer_cert_cache_path(settings, peer_id)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_text(certificate_pem, encoding="utf-8")
    try:
        cert_path.chmod(0o600)
    except OSError:
        pass
    return cert_path, fingerprint


def _peer_cert_cache_dir(settings: Settings) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / "peer-certs").resolve()


def _local_peer_cert_cache_path(settings: Settings, peer_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", peer_id)
    return (settings.userdata_root / "system" / "drone-app" / "local-peer-certs" / f"{safe}.crt").resolve()


def _peer_cert_cache_path(settings: Settings, peer_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", peer_id)
    return _peer_cert_cache_dir(settings) / f"{safe}.crt"


def _peer_cert_meta_path(settings: Settings, peer_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", peer_id)
    return _peer_cert_cache_dir(settings) / f"{safe}.json"


def _peer_trust_cafile(
    settings: Settings,
    peer_id: Optional[str] = None,
    config: Optional[dict] = None,
    refresh_cert: bool = False,
) -> Optional[Path]:
    if (
        _local_network.is_local_mode(settings)
        and (
            str((config or {}).get("network_mode") or "") == "local_network"
            or not _local_network.is_overmind_mode(settings)
        )
    ):
        if not peer_id:
            return None
        local_cached = _local_peer_cert_cache_path(settings, peer_id)
        return local_cached if local_cached.exists() else None
    if settings.drone_mtls_ca_file and settings.drone_mtls_ca_file.exists():
        if peer_id and refresh_cert and config:
            _fetch_peer_certificate(settings, config, peer_id)
        return settings.drone_mtls_ca_file
    if not peer_id:
        return None
    if refresh_cert and config:
        return _fetch_peer_certificate(settings, config, peer_id)
    cached = _peer_cert_cache_path(settings, peer_id)
    if cached.exists():
        return cached
    return _fetch_peer_certificate(settings, config or {}, peer_id) if config else None


def _peer_ssl_diagnostic(url: str, cafile: Optional[Path], error: BaseException) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    ca_configured = bool(cafile)
    ca_path = str(cafile) if cafile else "none"
    reason = str(error).strip() or error.__class__.__name__
    hint = "certificate validation failed"
    lower = reason.lower()
    if "hostname" in lower or "not valid for" in lower or "ip address mismatch" in lower:
        hint = "hostname/SAN mismatch"
    elif "self-signed" in lower or "unknown ca" in lower or "unable to get local issuer" in lower:
        hint = "missing or incorrect trusted CA bundle"
    elif "expired" in lower or "not yet valid" in lower:
        hint = "expired or not-yet-valid certificate"
    return f"{hint}: peer_url={url} hostname={host or 'unknown'} ca_configured={str(ca_configured).lower()} cafile={ca_path} error={reason}"


def _is_ssl_url_error(error: URLError) -> bool:
    reason = getattr(error, "reason", None)
    return isinstance(reason, ssl.SSLError)


def _drone_client_ssl_context(settings: Settings, url: str, verify: bool = False, cafile: Optional[Path] = None) -> Optional[ssl.SSLContext]:
    if not url.startswith("https://"):
        return None
    context = ssl.create_default_context(cafile=str(cafile) if cafile else None) if verify else ssl._create_unverified_context()
    configured_ca = settings.drone_mtls_ca_file
    uses_configured_ca = bool(configured_ca and configured_ca.exists() and cafile and cafile.resolve() == configured_ca.resolve())
    uses_peer_pin = bool(verify and cafile and not uses_configured_ca)
    if uses_peer_pin:
        # The pinned peer certificate came from Overmind; its routed NAT address need not appear in the SAN.
        context.check_hostname = False
    if (settings.drone_mtls_enabled or _local_network.is_local_mode(settings)) and settings.drone_cert_file.exists() and settings.drone_key_file.exists():
        context.load_cert_chain(certfile=str(settings.drone_cert_file), keyfile=str(settings.drone_key_file))
    return context


def _overmind_post_json(url: str, payload: dict, token: Optional[str] = None, settings: Optional[Settings] = None) -> dict:
    if settings is not None and not _local_network.is_overmind_mode(settings):
        raise RuntimeError("Overmind integration is disabled")
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    context = _drone_client_ssl_context(settings, url) if settings else (ssl._create_unverified_context() if url.startswith("https://") else None)
    with urlopen(request, timeout=10, context=context) as response:
        raw = response.read()
    if not raw:
        return {}
    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _overmind_post_json_with_status(
    url: str,
    payload: dict,
    token: Optional[str] = None,
    settings: Optional[Settings] = None,
    timeout_seconds: int = 10,
) -> Tuple[int, dict]:
    if settings is not None and not _local_network.is_overmind_mode(settings):
        raise RuntimeError("Overmind integration is disabled")
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=body, headers=headers, method="POST")
    context = _drone_client_ssl_context(settings, url) if settings else (ssl._create_unverified_context() if url.startswith("https://") else None)
    with urlopen(request, timeout=timeout_seconds, context=context) as response:
        status_code = int(getattr(response, "status", 200) or 200)
        raw = response.read()
    parsed = json.loads(raw.decode("utf-8")) if raw else {}
    return status_code, parsed if isinstance(parsed, dict) else {}


def _overmind_get_json(url: str, token: Optional[str] = None, settings: Optional[Settings] = None) -> dict:
    if settings is not None and not _local_network.is_overmind_mode(settings):
        raise RuntimeError("Overmind integration is disabled")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    context = _drone_client_ssl_context(settings, url) if settings else (ssl._create_unverified_context() if url.startswith("https://") else None)
    with urlopen(request, timeout=10, context=context) as response:
        raw = response.read()
    parsed = json.loads(raw.decode("utf-8")) if raw else {}
    return parsed if isinstance(parsed, dict) else {}


def _overmind_delete_json(url: str, token: Optional[str] = None, settings: Optional[Settings] = None) -> dict:
    if settings is not None and not _local_network.is_overmind_mode(settings):
        raise RuntimeError("Overmind integration is disabled")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="DELETE")
    context = _drone_client_ssl_context(settings, url) if settings else (ssl._create_unverified_context() if url.startswith("https://") else None)
    with urlopen(request, timeout=10, context=context) as response:
        raw = response.read()
    parsed = json.loads(raw.decode("utf-8")) if raw else {}
    return parsed if isinstance(parsed, dict) else {}


def _format_overmind_error(error: BaseException) -> str:
    if isinstance(error, HTTPError):
        detail = ""
        try:
            raw = error.read()
            detail = raw.decode("utf-8", errors="replace").strip() if raw else ""
        except Exception:
            detail = ""
        if len(detail) > 500:
            detail = detail[:500] + "..."
        suffix = f" body={detail}" if detail else ""
        return f"HTTPError status={error.code} reason={error.reason or error.msg or 'unknown'} url={error.geturl()}{suffix}"
    if isinstance(error, URLError):
        reason = getattr(error, "reason", None)
        return f"URLError reason={reason!r}" if reason else f"URLError {error!r}"
    message = str(error).strip()
    if message:
        return f"{error.__class__.__name__}: {message}"
    return repr(error)


def _drone_work_dir(settings: Settings) -> Path:
    return Path(os.environ.get("DRONE_APP_WORK_DIR", str(settings.userdata_root / "system" / "drone-app"))).resolve()


def _overlay_drone_release_tree(source: Path, target: Path) -> int:
    copied = 0
    if not source.exists() or not source.is_dir():
        raise ValueError(f"release source directory is missing: {source}")
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if "__pycache__" in relative.parts or item.name.endswith(".pyc"):
            continue
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, destination)
        try:
            destination.chmod(0o664)
        except OSError:
            pass
        copied += 1
    return copied


def _download_latest_drone_app(settings: Settings) -> dict:
    archive_url = os.environ.get("DRONE_APP_ARCHIVE_URL", DRONE_LATEST_ARCHIVE_URL)
    work_dir = _drone_work_dir(settings)
    work_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="drone-update-", dir=str(work_dir)) as temp_dir_name:
        temp_dir = Path(temp_dir_name).resolve()
        archive_path = temp_dir / "drone-app.tar.gz"
        request = Request(archive_url, headers={"User-Agent": "batocera-drone-self-update"})
        with urlopen(request, timeout=120) as response:
            with archive_path.open("wb") as output:
                shutil.copyfileobj(response, output)
        if not archive_path.exists() or archive_path.stat().st_size <= 0:
            raise ValueError("downloaded Drone archive was empty")
        stage_dir = temp_dir / "stage"
        stage_dir.mkdir()
        wanted_roots = {"app", "content"}
        extracted_roots = set()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                relative = member.name.lstrip("/")
                parts = relative.split("/", 1)
                if parts and parts[0] not in wanted_roots and len(parts) == 2:
                    relative = parts[1]
                    parts = relative.split("/", 1)
                if not parts or parts[0] not in wanted_roots:
                    continue
                relative_path = Path(relative)
                if "__pycache__" in relative_path.parts:
                    continue
                target = (stage_dir / relative_path).resolve()
                if stage_dir not in target.parents and target != stage_dir:
                    raise ValueError(f"archive member escapes stage directory: {member.name}")
                extracted_roots.add(parts[0])
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        missing = wanted_roots - extracted_roots
        if missing:
            raise ValueError(f"Drone archive is missing required directories: {', '.join(sorted(missing))}")
        copied_files = 0
        for name in sorted(wanted_roots):
            source = stage_dir / name
            target = work_dir / name
            copied_files += _overlay_drone_release_tree(source, target)
    return {
        "status": "downloaded",
        "archive_url": archive_url,
        "work_dir": str(work_dir),
        "copied_files": copied_files,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "restart_required": True,
    }


def _restart_drone_process_soon(delay_seconds: float = 1.0) -> None:
    def restart() -> None:
        time.sleep(max(0.1, delay_seconds))
        print(
            "Drone self-update restart requested: re-executing app process",
            file=sys.stderr,
            flush=True,
        )
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except Exception as exc:
            print(
                f"Drone self-update re-exec failed: {exc!r}; exiting with code {DRONE_SELF_UPDATE_EXIT_CODE}",
                file=sys.stderr,
                flush=True,
            )
            os._exit(DRONE_SELF_UPDATE_EXIT_CODE)

    Thread(target=restart, name="drone-self-update-restart", daemon=True).start()


def _save_overmind_runtime_config(settings: Settings, config: dict) -> None:
    path = _overmind_config_path_for_settings(settings)
    _save_state_payload(_state_database_path(settings.userdata_root), path.name, config)
    path.unlink(missing_ok=True)


def _register_or_claim_overmind_token(settings: Settings, repository: "RomRepository", config: dict, base_url: str) -> Optional[str]:
    auth_token = str(config.get("overmind_auth_token") or "").strip()
    email = str(config.get("overmind_email") or "").strip()
    drone_name = str(config.get("drone_name") or "").strip() or socket.gethostname()
    network = _drone_network_payload(settings)
    reachable_url = _drone_reachable_url(settings, network)
    payload = {
        "device_id": settings.overmind_device_id,
        "device_name": drone_name,
        "api_port": _drone_advertised_api_port(settings),
        "scheme": _drone_scheme(settings),
        "reachable_url": reachable_url,
        "batocera_info": {
            "model": "Batocera Drone",
            "system": sys.platform,
            "architecture": os.uname().machine if hasattr(os, "uname") else "",
            "cpu_model": os.environ.get("DRONE_CPU_MODEL", "unknown"),
            "cpu_cores": os.cpu_count() or 1,
            "cpu_threads": os.cpu_count() or 1,
            "cpu_max_frequency": "unknown",
            "memory_available": "unknown",
            "memory_total": "unknown",
            "ip_address": _drone_report_host(settings, network),
            "network": network,
            "api_port": _drone_advertised_api_port(settings),
            "scheme": _drone_scheme(settings),
            "reachable_url": reachable_url,
            "system_info": _collect_system_info_payload(settings),
            "certificate": DroneCertificateManager(settings).metadata(),
        },
    }
    if email:
        payload["email"] = email
    if auth_token:
        payload["authorization_token"] = auth_token
    context = _overmind_onboarding_context(settings, config, base_url, payload)
    config["last_onboarding_attempt"] = context
    _log_overmind_onboarding("Overmind onboarding request prepared", context)
    try:
        response = _overmind_post_json(f"{base_url}/api/devices/register", payload, token=auth_token or None, settings=settings)
    except Exception as error:
        _mark_overmind_auth_failed(settings, config, error)
        config["last_onboarding_attempt"] = context
        _save_overmind_runtime_config(settings, config)
        _log_overmind_onboarding("Overmind onboarding request failed", context, error=error)
        return None
    config["last_onboarding_attempt"] = context
    if response.get("drone_token"):
        config["overmind_token"] = str(response["drone_token"])
        config["integration_enabled"] = True
        config["integration_state"] = "polling"
        config["swarm_connection_status"] = "connected"
        config["last_error"] = None
        config["notes"] = "Drone approved by Overmind and polling is active."
        _save_overmind_runtime_config(settings, config)
        print(f"Overmind onboarding approved for {settings.overmind_device_id}", file=sys.stdout, flush=True)
        return config["overmind_token"]
    config["integration_state"] = "pending_approval"
    config["swarm_connection_status"] = "pending approval"
    config["integration_enabled"] = True
    config["notes"] = response.get("message") or "Psionic connection detected. Awaiting Overlord approval."
    config["last_error"] = None
    _save_overmind_runtime_config(settings, config)
    _log_overmind_onboarding("Overmind onboarding request pending approval", context)
    return None


def _reclaim_overmind_token_after_unauthorized(settings: Settings, repository: "RomRepository", config: dict, base_url: str, error: HTTPError) -> Optional[str]:
    auth_token = str(config.get("overmind_auth_token") or "").strip()
    if not auth_token:
        return None
    config.pop("overmind_token", None)
    config["integration_state"] = "credential_reclaim"
    config["last_error"] = _format_overmind_error(error)
    config["notes"] = "Stored Drone bearer token was rejected; reclaiming with bound authorization token."
    _save_overmind_runtime_config(settings, config)
    _overmind_log(
        f"Overmind bearer token rejected for {settings.overmind_device_id}; reclaiming with bound authorization token."
    )
    return _register_or_claim_overmind_token(settings, repository, config, base_url)


def _report_overmind_action_completion(
    settings: "Settings",
    repository: "RomRepository",
    config: dict,
    base_url: str,
    token: str,
    device_id: str,
    action: dict,
    status_value: str,
    message: str,
    result: Optional[dict],
    integration_enabled: bool,
) -> str:
    """Report an action's completion to Overmind, returning the (possibly reclaimed) token.

    If the stored bearer token was rotated out from under us, the completion POST gets a
    401. Without recovery the action would stay 'in_progress' in Overmind forever (the
    Drone already executed and dropped it). So on 401 we reclaim the token with the bound
    authorization token and retry the completion once, mirroring the heartbeat path.
    """
    if not _local_network.is_overmind_mode(settings):
        return token
    action_id = quote(str(action.get("id") or ""), safe="")
    action_label = str(action.get("action") or "?")
    action_id_log = str(action.get("id") or "?")
    if not action_id:
        return token
    complete_url = f"{base_url}/api/devices/{device_id}/actions/{action_id}/complete"
    completion_payload: dict = {"status": status_value, "message": message}
    if result is not None:
        completion_payload["result"] = result
    try:
        try:
            _overmind_post_json(complete_url, completion_payload, token=token, settings=settings)
        except HTTPError as error:
            if error.code == 401 and integration_enabled:
                replacement_token = _reclaim_overmind_token_after_unauthorized(settings, repository, config, base_url, error)
                if not replacement_token:
                    raise
                token = replacement_token
                _overmind_post_json(complete_url, completion_payload, token=token, settings=settings)
            else:
                raise
        _overmind_log(
            f"Reported Overmind action completion {action_label} ({action_id_log}): {status_value}"
        )
    except Exception as error:
        _overmind_log(
            f"Failed to report Overmind action completion {action_id_log}: {_format_overmind_error(error)}",
            also_stdout=True,
        )
    return token


def _fetch_peer_certificate(settings: Settings, config: dict, peer_id: str) -> Optional[Path]:
    base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
    token = str(config.get("overmind_token") or "").strip()
    if not base_url or not token or not peer_id:
        return None
    try:
        payload = _overmind_get_json(
            f"{base_url}/api/devices/{quote(settings.overmind_device_id, safe='')}/peer-certificate/{quote(peer_id, safe='')}",
            token=token,
            settings=settings,
        )
        pem = str(payload.get("certificate_pem") or "")
        if "BEGIN CERTIFICATE" not in pem:
            return None
        cert_path = _peer_cert_cache_path(settings, peer_id)
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        cert_path.write_text(pem, encoding="utf-8")
        meta = dict(payload.get("metadata") or {})
        meta["peer_drone_id"] = peer_id
        meta["fetched_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        meta["source_overmind_url"] = base_url
        _save_state_payload(
            _state_database_path(settings.userdata_root),
            "peer_certificate_metadata",
            meta,
            state_key=peer_id,
        )
        _peer_cert_meta_path(settings, peer_id).unlink(missing_ok=True)
        print(f"Fetched peer certificate for {peer_id}", file=sys.stdout, flush=True)
        return cert_path
    except Exception as error:
        print(f"Failed to fetch peer certificate for {peer_id}: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
        return None


def _peer_get_json(url: str, settings: Settings, peer_id: Optional[str] = None, config: Optional[dict] = None, refresh_cert: bool = False) -> dict:
    cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config, refresh_cert=refresh_cert)
    if url.startswith("https://") and peer_id and not cafile:
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "batocera-drone-peer/1.0"})
    try:
        with urlopen(request, timeout=PEER_CHECK_TIMEOUT_SECONDS, context=_drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)) as response:
            raw = response.read()
    except ssl.SSLError as error:
        raise ssl.SSLError(_peer_ssl_diagnostic(url, cafile, error)) from error
    except URLError as error:
        reason = getattr(error, "reason", None)
        if isinstance(reason, ssl.SSLError):
            raise URLError(_peer_ssl_diagnostic(url, cafile, reason)) from error
        raise
    parsed = json.loads(raw.decode("utf-8")) if raw else {}
    return parsed if isinstance(parsed, dict) else {}


def _local_pair_peer(settings: Settings, peer: dict, pairing_code: str) -> dict:
    if not _local_network.is_local_mode(settings):
        raise ValueError("Drone is not in local network mode")
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "").strip()
    address = _peer_address(peer)
    if not peer_id or not address:
        raise ValueError("discovered peer has no reachable address")
    certificate = DroneCertificateManager(settings).ensure_certificate()
    certificate_pem = str(certificate.get("public_certificate") or "")
    if not certificate_pem:
        raise RuntimeError("local Drone certificate is unavailable")
    own_discovery = _local_network.discovery_payload(settings, str(certificate.get("fingerprint") or ""))
    payload = {
        "pairing_code": str(pairing_code or "").strip(),
        "drone_id": settings.overmind_device_id,
        "name": socket.gethostname(),
        "hostname": socket.gethostname(),
        "scheme": _drone_scheme(settings),
        "api_port": _drone_advertised_api_port(settings),
        "reachable_url": own_discovery.get("reachable_url"),
        "certificate_pem": certificate_pem,
        "certificate_fingerprint": str(certificate.get("fingerprint") or ""),
    }
    request = Request(
        f"{address.rstrip('/')}/v1/api/peer/pair",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "batocera-drone-local-pair/1.0"},
        method="POST",
    )
    context = ssl._create_unverified_context() if address.startswith("https://") else None
    with urlopen(request, timeout=10, context=context) as response:
        raw = response.read()
    result = json.loads(raw.decode("utf-8")) if raw else {}
    if not isinstance(result, dict) or str(result.get("status") or "") != "paired":
        raise RuntimeError("peer did not accept pairing request")
    remote_id = str(result.get("drone_id") or "").strip()
    if remote_id != peer_id:
        raise RuntimeError("paired peer identity did not match discovered peer")
    remote_pem = str(result.get("certificate_pem") or "")
    cert_path, fingerprint = _save_local_peer_certificate(settings, peer_id, remote_pem)
    expected = str(peer.get("certificate_fingerprint") or "").strip().lower()
    returned = str(result.get("certificate_fingerprint") or "").strip().lower()
    if (expected and expected != fingerprint.lower()) or (returned and returned != fingerprint.lower()):
        cert_path.unlink(missing_ok=True)
        raise RuntimeError("paired peer certificate fingerprint did not match discovery")
    stored = _local_network.save_paired_peer(
        settings,
        {
            **peer,
            "name": str(result.get("name") or peer.get("name") or peer_id),
            "reachable_url": address,
            "advertised_reachable_url": str(result.get("reachable_url") or peer.get("advertised_reachable_url") or ""),
            "scheme": str(result.get("scheme") or peer.get("scheme") or "https"),
            "api_port": int(result.get("api_port") or peer.get("api_port") or 443),
            "certificate_fingerprint": fingerprint,
            "certificate_path": str(cert_path),
        },
    )
    return stored


def _peer_health_url(address: str) -> str:
    return f"{str(address or '').strip().rstrip('/')}/health"


def _peer_api_port(peer: dict) -> int:
    try:
        return int(peer.get("api_port") or peer.get("port") or 443)
    except (TypeError, ValueError):
        return 443


def _peer_address(peer: dict) -> Optional[str]:
    public_reachable_url = str(peer.get("public_reachable_url") or "").strip().rstrip("/")
    if public_reachable_url:
        return public_reachable_url
    scheme = str(peer.get("scheme") or peer.get("protocol") or "https").strip() or "https"
    port = _peer_api_port(peer)
    reachable_url = str(peer.get("reachable_url") or "").strip().rstrip("/")
    if reachable_url:
        return reachable_url
    public_ip = str(peer.get("public_ip") or "").strip()
    if public_ip and peer.get("public_resolvable") is True:
        if ":" in public_ip and not public_ip.startswith("["):
            public_ip = f"[{public_ip}]"
        port_suffix = "" if port == 443 and scheme == "https" else f":{port}"
        return f"{scheme}://{public_ip}{port_suffix}"
    resolved = peer.get("resolved_network") if isinstance(peer.get("resolved_network"), dict) else {}
    for value in resolved.get("ipv4") or []:
        host = str(value or "").strip()
        if host:
            return f"{scheme}://{host}:{port}"
    for value in resolved.get("ipv6") or []:
        host = str(value or "").strip()
        if host:
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            return f"{scheme}://{host}:{port}"
    for key in ("local_ip", "private_ip"):
        value = peer.get(key)
        if isinstance(value, list):
            value = next((item for item in value if item), None)
        if value:
            host = str(value).strip()
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            return f"{scheme}://{host}:{port}"
    return None


def _check_peer(settings: Settings, peer: dict, config: Optional[dict] = None) -> dict:
    target_id = str(peer.get("drone_id") or peer.get("device_id") or peer.get("id") or "")
    peer_id = target_id
    address = _peer_address(peer)
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    result = {
        "source_drone_id": settings.overmind_device_id,
        "target_drone_id": target_id,
        "target_address": address,
        "status": "fail",
        "latency_ms": None,
        "failure_reason": None,
        "checked_at": checked_at,
    }
    if not address:
        result["failure_reason"] = "no peer address available"
        return result
    started = time.monotonic()
    try:
        _peer_get_json(_peer_health_url(address), settings, peer_id=peer_id, config=config)
        result["status"] = "pass"
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
    except ssl.SSLError as error:
        message = str(error)
        if config and any(term in message.lower() for term in ("unknown ca", "certificate", "cert")):
            try:
                _peer_get_json(_peer_health_url(address), settings, peer_id=peer_id, config=config, refresh_cert=True)
                result["status"] = "pass"
                result["latency_ms"] = int((time.monotonic() - started) * 1000)
                return result
            except Exception as retry_error:
                result["failure_reason"] = f"{message}; retry after cert refresh failed: {retry_error}"
                return result
        result["failure_reason"] = message
    except Exception as error:
        result["failure_reason"] = str(error)
    return result


def _speed_test_raw_request(url: str, data: Optional[bytes] = None) -> bytes:
    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": "batocera-drone-speed-test/1.0",
    }
    if data is not None:
        headers["Content-Type"] = "application/octet-stream"
    request = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    timeout = max(1, int(os.environ.get("DRONE_SPEED_TEST_TIMEOUT_SECONDS", "15")))
    with urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
        return response.read()


def _sample_speed() -> dict:
    """Measure Internet throughput against Cloudflare's public speed-test edge."""
    sampled_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    base_url = (
        os.environ.get("DRONE_SPEED_TEST_BASE_URL", SPEED_TEST_DEFAULT_BASE_URL).strip().rstrip("/")
        or SPEED_TEST_DEFAULT_BASE_URL
    )
    source = "cloudflare-speed-test" if base_url == SPEED_TEST_DEFAULT_BASE_URL else "external-speed-test"
    size = max(1024, min(int(os.environ.get("DRONE_SPEED_TEST_BYTES", "1000000")), 25 * 1000 * 1000))
    sample = {
        "upload_mbps": 0,
        "download_mbps": 0,
        "latency_ms": 0,
        "source": source,
        "sampled_at": sampled_at,
        "bytes": size,
    }
    try:
        latency_url = f"{base_url}/__down?bytes=0"
        started = time.monotonic()
        _speed_test_raw_request(latency_url)
        sample["latency_ms"] = int(max(time.monotonic() - started, 0.001) * 1000)

        download_url = f"{base_url}/__down?bytes={size}"
        started = time.monotonic()
        downloaded = _speed_test_raw_request(download_url)
        elapsed = max(time.monotonic() - started, 0.001)
        sample["download_mbps"] = round((len(downloaded) * 8) / elapsed / 1_000_000, 3)

        upload_url = f"{base_url}/__up"
        payload = b"1" * size
        started = time.monotonic()
        _speed_test_raw_request(upload_url, data=payload)
        elapsed = max(time.monotonic() - started, 0.001)
        sample["upload_mbps"] = round((len(payload) * 8) / elapsed / 1_000_000, 3)
    except Exception as error:
        sample["source"] = f"{source}-failed"
        sample["error"] = _format_overmind_error(error)
    print(f"Speed sample created: source={sample['source']} down={sample['download_mbps']} up={sample['upload_mbps']}", file=sys.stdout, flush=True)
    return sample


def _collect_gpu_info() -> dict:
    info = {
        "vendor": None,
        "model": None,
        "driver": None,
        "renderer": None,
        "pci_devices": [],
    }
    try:
        result = subprocess.run(["lspci", "-nnk"], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            current = None
            for line in (result.stdout or "").splitlines():
                lower = line.lower()
                if " vga compatible controller" in lower or " 3d controller" in lower or " display controller" in lower:
                    current = {"description": line.strip(), "driver": None}
                    parts = line.split(":", 2)
                    description = parts[-1].strip() if parts else line.strip()
                    if not info["model"]:
                        info["model"] = description
                    if " nvidia " in f" {lower} ":
                        info["vendor"] = info["vendor"] or "NVIDIA"
                    elif " amd " in f" {lower} " or " advanced micro devices" in lower or " ati " in f" {lower} ":
                        info["vendor"] = info["vendor"] or "AMD"
                    elif " intel " in f" {lower} ":
                        info["vendor"] = info["vendor"] or "Intel"
                    info["pci_devices"].append(current)
                    continue
                if current and "kernel driver in use:" in lower:
                    driver = line.split(":", 1)[1].strip()
                    current["driver"] = driver
                    info["driver"] = info["driver"] or driver
    except Exception:
        pass

    for card in sorted(Path("/sys/class/drm").glob("card*/device")):
        try:
            vendor_id = (card / "vendor").read_text(encoding="utf-8", errors="ignore").strip()
            device_id = (card / "device").read_text(encoding="utf-8", errors="ignore").strip()
            driver = card.resolve().parts[-2] if card.exists() else None
            entry = {"path": str(card), "vendor_id": vendor_id, "device_id": device_id}
            if driver:
                entry["driver"] = driver
            info["pci_devices"].append(entry)
        except Exception:
            continue

    try:
        result = subprocess.run(["sh", "-c", "glxinfo -B 2>/dev/null"], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            for line in (result.stdout or "").splitlines():
                if ":" not in line:
                    continue
                key, value = [part.strip() for part in line.split(":", 1)]
                lower = key.lower()
                if lower == "opengl vendor string":
                    info["vendor"] = info["vendor"] or value
                elif lower == "opengl renderer string":
                    info["renderer"] = value
                    info["model"] = info["model"] or value
        elif not info["renderer"]:
            info["renderer"] = None
    except Exception:
        pass

    return info


def _collect_performance_metrics(root: Path) -> dict:
    global _PERFORMANCE_METRICS_LAST_SAMPLE
    now = time.monotonic()
    previous = _PERFORMANCE_METRICS_LAST_SAMPLE
    elapsed = max(0.001, now - float(previous.get("monotonic") or now)) if previous else None

    process_seconds = float(os.times().user + os.times().system)
    total_jiffies = None
    idle_jiffies = None
    try:
        values = [int(part) for part in Path("/proc/stat").read_text(encoding="utf-8", errors="ignore").splitlines()[0].split()[1:]]
        total_jiffies = sum(values)
        idle_jiffies = values[3] + (values[4] if len(values) > 4 else 0)
    except Exception:
        pass

    memory = {}
    try:
        parsed = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            parsed[key] = int(raw.strip().split()[0]) * 1024
        total = int(parsed.get("MemTotal") or 0)
        available = int(parsed.get("MemAvailable") or 0)
        used = max(0, total - available) if total else 0
        memory = {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": used,
            "used_percent": round((used / total) * 100, 2) if total else None,
        }
    except Exception:
        memory = {}

    process_memory = {}
    try:
        values = {}
        for line in Path("/proc/self/status").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith(("VmRSS:", "VmSize:")):
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().split()[0]) * 1024
        process_memory = {"rss_bytes": values.get("VmRSS"), "vms_bytes": values.get("VmSize")}
    except Exception:
        process_memory = {}

    diskstats = {}
    try:
        totals = {"read_bytes": 0, "write_bytes": 0, "weighted_io_ms": 0}
        for line in Path("/proc/diskstats").read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) < 14 or parts[2].startswith(("loop", "ram", "fd")):
                continue
            totals["read_bytes"] += int(parts[5]) * 512
            totals["write_bytes"] += int(parts[9]) * 512
            totals["weighted_io_ms"] += int(parts[13])
        diskstats = totals
    except Exception:
        diskstats = {}

    process_cpu_percent = None
    host_cpu_percent = None
    disk_rates = {}
    if previous and elapsed:
        process_delta = process_seconds - float(previous["cpu"].get("process_seconds") or 0)
        process_cpu_percent = round(max(0.0, process_delta / elapsed * 100 / max(1, os.cpu_count() or 1)), 2)
        if total_jiffies is not None and previous["cpu"].get("total_jiffies") is not None:
            total_delta = int(total_jiffies) - int(previous["cpu"]["total_jiffies"])
            idle_delta = int(idle_jiffies or 0) - int(previous["cpu"]["idle_jiffies"] or 0)
            if total_delta > 0:
                host_cpu_percent = round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 2)
        if diskstats and previous.get("diskstats"):
            prev_disk = previous["diskstats"]
            read_delta = max(0, diskstats.get("read_bytes", 0) - prev_disk.get("read_bytes", 0))
            write_delta = max(0, diskstats.get("write_bytes", 0) - prev_disk.get("write_bytes", 0))
            weighted_delta = max(0, diskstats.get("weighted_io_ms", 0) - prev_disk.get("weighted_io_ms", 0))
            disk_rates = {
                "read_bytes_per_second": round(read_delta / elapsed, 2),
                "write_bytes_per_second": round(write_delta / elapsed, 2),
                "contention_percent": round(max(0.0, min(100.0, weighted_delta / (elapsed * 1000) * 100)), 2),
            }

    disks = _collect_mounted_disk_metrics(root)
    disk = dict(disks[0]) if disks else {}
    disk.update(disk_rates)

    sample = {
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "cpu": {
            "process_percent": process_cpu_percent,
            "host_percent": host_cpu_percent,
            "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            "cpu_count": os.cpu_count(),
            "process_seconds": process_seconds,
            "total_jiffies": total_jiffies,
            "idle_jiffies": idle_jiffies,
        },
        "memory": memory,
        "process": process_memory,
        "disk": disk,
        "disks": disks,
        "diskstats": diskstats,
        "monotonic": now,
    }
    _PERFORMANCE_METRICS_LAST_SAMPLE = sample
    public_cpu = {key: value for key, value in sample["cpu"].items() if key not in {"process_seconds", "total_jiffies", "idle_jiffies"}}
    return {
        "collected_at": sample["collected_at"],
        "cpu": public_cpu,
        "memory": memory,
        "process": process_memory,
        "disk": disk,
        "disks": disks,
    }


def _decode_mountinfo_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _collect_mounted_disk_metrics(root: Path, mountinfo_path: Path = Path("/proc/self/mountinfo")) -> List[dict]:
    """Collect capacity metrics for the main data filesystem and mounted physical drives."""
    root = root.resolve()
    try:
        main_device = root.stat().st_dev
    except OSError:
        main_device = None

    candidates = []
    try:
        for line in mountinfo_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            before, after = line.split(" - ", 1)
            fields = before.split()
            trailing = after.split()
            if len(fields) < 5 or len(trailing) < 2:
                continue
            mount_path = Path(_decode_mountinfo_path(fields[4]))
            source = _decode_mountinfo_path(trailing[1])
            if not source.startswith("/dev/"):
                continue
            candidates.append((mount_path, trailing[0], source))
    except (OSError, ValueError):
        candidates = []

    # Always include the configured userdata filesystem even when procfs is unavailable
    # or Batocera exposes it through a non-/dev mount source.
    candidates.insert(0, (root, "", ""))
    rows = []
    by_device = {}
    for mount_path, filesystem, source in candidates:
        try:
            stat = mount_path.stat()
            usage = shutil.disk_usage(mount_path)
        except OSError:
            continue
        device_id = stat.st_dev
        existing_index = by_device.get(device_id)
        if existing_index is not None:
            existing = rows[existing_index]
            if source and not existing.get("source"):
                existing["source"] = source
            if filesystem and not existing.get("filesystem"):
                existing["filesystem"] = filesystem
            continue
        by_device[device_id] = len(rows)
        is_main = main_device is not None and device_id == main_device
        label = "Main drive" if is_main else (mount_path.name or source or str(mount_path))
        rows.append({
            "label": label,
            "path": str(mount_path),
            "source": source or None,
            "filesystem": filesystem or None,
            "is_main": is_main,
            "is_external": not is_main,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": round((usage.used / usage.total) * 100, 2) if usage.total else None,
        })
    rows.sort(key=lambda row: (not row["is_main"], str(row["label"]).lower(), str(row["path"]).lower()))
    return rows


def _collect_system_info_payload(settings: Settings) -> dict:
    hostname = socket.gethostname()
    network = _get_local_ip_addresses()
    memory = {}
    try:
        raw = Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines()
        parsed = {}
        for line in raw:
            if ":" in line:
                key, value = line.split(":", 1)
                parsed[key.strip()] = value.strip()
        memory = {"total": parsed.get("MemTotal"), "available": parsed.get("MemAvailable")}
    except Exception:
        memory = {}
    disk = {}
    try:
        usage = shutil.disk_usage(settings.userdata_root)
        disk = {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}
    except Exception:
        disk = {}
    uptime = None
    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except Exception:
        uptime = None
    batocera_version = None
    for candidate in (settings.userdata_root / "system" / "batocera.version", Path("/usr/share/batocera/batocera.version")):
        try:
            if candidate.exists():
                batocera_version = candidate.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
                break
        except Exception:
            continue
    asset_cache = {}
    try:
        cache_status = _rom_metadata_cache_status(settings)
        counts = cache_status.get("counts") if isinstance(cache_status.get("counts"), dict) else {}
        total_assets = int(counts.get("total") or 0)
        complete = bool(cache_status.get("complete"))
        uploaded = bool(cache_status.get("uploaded"))
        pending_total = int((cache_status.get("pending_changes") or {}).get("total") or 0)
        cached_percent = 100.0 if total_assets and complete else (0.0 if not total_assets else 50.0)
        upload_percent = 100.0 if total_assets and uploaded and not pending_total else (0.0 if total_assets else None)
        health = "green" if complete and (uploaded or not pending_total) else "yellow"
        if cache_status.get("rebuilt") or cache_status.get("scan_in_progress"):
            health = "yellow"
        asset_cache = {
            "health": health,
            "cached_percent": cached_percent,
            "uploaded_percent": upload_percent,
            "counts": counts,
            "pending_changes": cache_status.get("pending_changes") or {},
            "last_full_scan_at": cache_status.get("last_full_scan_at"),
            "last_successful_upload_at": cache_status.get("last_successful_upload_at"),
            "scan_in_progress": bool(cache_status.get("scan_in_progress")),
            "needs_upload": bool(cache_status.get("needs_upload")),
        }
    except Exception as error:
        asset_cache = {"health": "red", "error": _format_overmind_error(error)}
    return {
        "hostname": hostname,
        "device_name": hostname,
        "platform": sys.platform,
        "os": os.uname().sysname if hasattr(os, "uname") else sys.platform,
        "os_release": os.uname().release if hasattr(os, "uname") else "",
        "batocera_version": batocera_version,
        "drone_app_version": _drone_app_version(),
        "architecture": os.uname().machine if hasattr(os, "uname") else "",
        "cpu": {"model": os.environ.get("DRONE_CPU_MODEL", ""), "count": os.cpu_count()},
        "memory": memory,
        "disk": disk,
        "gpu": _collect_gpu_info(),
        "performance": _collect_performance_metrics(settings.userdata_root),
        "asset_cache": asset_cache,
        "screen_mode": _get_screen_mode(settings),
        "audio_volume": _get_audio_volume(settings),
        "network": network,
        "uptime_seconds": uptime,
        "container": Path("/.dockerenv").exists() or os.environ.get("RUNNING_IN_DOCKER") == "1",
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def _read_text_file(path: Path, max_bytes: int = 262144) -> dict:
    try:
        raw = path.read_bytes()[:max_bytes + 1]
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        return {
            "path": str(path),
            "size": path.stat().st_size,
            "truncated": truncated,
            "content": raw.decode("utf-8", errors="replace"),
        }
    except Exception as error:
        return {"path": str(path), "error": str(error)}


def _resolve_userdata_path(settings: Settings, candidate: str) -> Path:
    if candidate == "/userdata":
        return settings.userdata_root.resolve()
    if candidate.startswith("/userdata/"):
        return (settings.userdata_root / candidate[len("/userdata/") :]).resolve()
    return Path(candidate).resolve()


def _collect_rom_metadata(settings: Settings, repository: "RomRepository") -> dict:
    cache, _ = _load_rom_metadata_cache(settings)
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    bios_entries = cache.get("bios_entries") if isinstance(cache.get("bios_entries"), dict) else {}
    artwork_entries = cache.get("artwork_entries") if isinstance(cache.get("artwork_entries"), dict) else {}
    if entries or bios_entries or artwork_entries:
        result = _build_rom_metadata_snapshot_from_cache(settings, cache, rehydrate_gamelist=True)
        print(
            f"Asset metadata collected from local database: systems={len(result.get('systems') or [])} roms={len(result.get('roms') or [])} bios={len(result.get('bios') or [])} artwork={len(result.get('artwork') or [])}",
            file=sys.stdout,
            flush=True,
        )
        return result

    try:
        system_names = repository.list_system_names()
    except FileNotFoundError:
        system_names = []
    try:
        bios = repository.list_bios_entries()
    except FileNotFoundError:
        bios = []
    try:
        artwork = repository.list_artwork_metadata()
    except Exception:
        artwork = []
    roms = []
    systems = []
    gamelists = []
    for system_name in system_names:
        system_name = str(system_name or "").strip()
        if not system_name:
            continue
        try:
            system_dir = repository.get_system_dir(system_name)
            gamelist, system_roms = repository.list_gamelist_rom_metadata(system_name, system_dir)
        except Exception as error:
            roms.append({"system": system_name, "error": str(error)})
            continue
        gamelists.append(gamelist)
        if system_roms:
            systems.append({"name": system_name, "rom_count": len(system_roms)})
        for rom in system_roms:
            item = dict(rom)
            item["system"] = system_name
            item["system_name"] = system_name
            roms.append(item)
    result = {
        "type": "asset_metadata",
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "roms_root": str(settings.roms_root),
        "bios_root": str(settings.bios_root),
        "systems": systems,
        "roms": roms,
        "bios": bios,
        "artwork": artwork,
        "gamelists": gamelists,
    }
    print(
        f"Asset metadata scan root={settings.roms_root} systems={len(systems)} roms={len(roms)} bios={len(bios)} artwork={len(artwork)} source=database_or_filesystem",
        file=sys.stdout,
        flush=True,
    )
    return result


def _rom_cache_entry_key(system: str, relative_path: str) -> str:
    normalized_path = _normalize_gamelist_rom_path(str(relative_path or ""))
    return f"{system.strip().lower()}:{normalized_path}"


def _bios_cache_entry_key(relative_path: str) -> str:
    return _normalize_gamelist_rom_path(str(relative_path or "")).lower()


def _artwork_cache_entry_key(system: str, rom_path: str) -> str:
    return f"{str(system or '').strip().lower()}:{_normalize_gamelist_rom_path(str(rom_path or '')).lower()}"


ROM_INVENTORY_FINGERPRINT_ALGORITHM = "rom-inventory-sha256-v1"


def _normalize_rom_inventory_path(value: object) -> str:
    return str(value or "").replace("\\", "/").strip().lstrip("./").lower()


def _rom_inventory_fingerprint(roms: Iterable[dict]) -> str:
    rows = []
    for row in roms or []:
        if not isinstance(row, dict):
            continue
        system = str(row.get("system") or row.get("system_name") or "").strip().lower()
        path = _normalize_rom_inventory_path(
            row.get("file_path")
            or row.get("relative_path")
            or row.get("rom_path")
            or row.get("rom_file")
            or row.get("rom_name")
            or row.get("name")
        )
        if not system or not path:
            continue
        entry_type = str(row.get("entry_type") or "file").strip().lower()
        fingerprint_value = str(row.get("rom_fingerprint") or row.get("fingerprint") or row.get("hash") or "").strip().lower()
        file_size = row.get("file_size") if row.get("file_size") is not None else row.get("byte_count")
        size_value = str(int(file_size)) if isinstance(file_size, (int, float)) else str(file_size or "").strip()
        rows.append("\t".join((system, path, entry_type, fingerprint_value, size_value)))
    digest = hashlib.sha256()
    for value in sorted(rows):
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _rom_inventory_fingerprint_from_cache_state(settings: Settings) -> Optional[str]:
    try:
        state = _read_rom_metadata_cache_state(settings, "rom_inventory_fingerprint")
    except Exception:
        return None
    value = str(state.get("rom_inventory_fingerprint") or "").strip()
    return value or None


# Wholistic per-asset-class "thumbprints" round-tripped with Overmind so the Drone
# (not Overmind) decides when a re-sync is needed: Overmind echoes the thumbprints it
# last stored, the Drone compares them against what it currently holds on disk, and only
# pushes when they differ. The romset thumbprint reuses the ROM inventory fingerprint;
# BIOS gets its own so the two asset classes can drift independently.
BIOS_INVENTORY_FINGERPRINT_ALGORITHM = "bios-inventory-sha256-v1"


def _bios_inventory_fingerprint(bios: Iterable[dict]) -> str:
    rows = []
    for row in bios or []:
        if not isinstance(row, dict):
            continue
        path = _normalize_rom_inventory_path(
            row.get("relative_path")
            or row.get("file_path")
            or row.get("path")
            or row.get("name")
            or row.get("bios_name")
        )
        if not path:
            continue
        md5_value = str(row.get("bios_md5") or row.get("md5") or row.get("fingerprint") or "").strip().lower()
        file_size = row.get("file_size") if row.get("file_size") is not None else row.get("byte_count")
        size_value = str(int(file_size)) if isinstance(file_size, (int, float)) else str(file_size or "").strip()
        rows.append("\t".join((path, md5_value, size_value)))
    digest = hashlib.sha256()
    for value in sorted(rows):
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _local_asset_thumbprints(settings: Settings) -> Tuple[str, str]:
    """Return the (romset, bios) thumbprints the Drone last persisted for its on-disk assets."""
    try:
        state = _read_rom_metadata_cache_state(
            settings,
            "romset_files_thumbprint",
            "bios_files_thumbprint",
            "rom_inventory_fingerprint",
        )
    except Exception:
        return "", ""
    romset = str(state.get("romset_files_thumbprint") or state.get("rom_inventory_fingerprint") or "").strip()
    bios = str(state.get("bios_files_thumbprint") or "").strip()
    return romset, bios


def _snapshot_asset_thumbprints(snapshot: dict) -> Tuple[str, str]:
    romset = str(snapshot.get("romset_files_thumbprint") or snapshot.get("rom_inventory_fingerprint") or "").strip()
    bios = str(snapshot.get("bios_files_thumbprint") or "").strip()
    return romset, bios


def _maybe_request_asset_push_from_heartbeat(settings: Settings, response: dict) -> None:
    """Compare Overmind-echoed asset thumbprints with the Drone's local thumbprints.

    When Overmind reports a thumbprint that differs from what the Drone last synced,
    Overmind's stored asset set has drifted (or it never received ours). Flag a push so
    the next metadata poll uploads a full inventory and resyncs, and wake the poller so it
    happens promptly instead of waiting out the full poll interval. Only fires when the
    Drone actually has local assets, so a fresh Drone's initial upload still flows through
    the normal poller path.
    """
    if not isinstance(response, dict):
        return
    overmind_romset = str(response.get("romset_files_thumbprint") or "").strip()
    overmind_bios = str(response.get("bios_files_thumbprint") or "").strip()
    if not overmind_romset and not overmind_bios:
        return
    local_romset, local_bios = _local_asset_thumbprints(settings)
    if not local_romset:
        return
    romset_mismatch = overmind_romset != local_romset
    # Only treat BIOS as drifted when Overmind actually reported a BIOS thumbprint;
    # an older Overmind that never sends one should not trigger endless pushes.
    bios_mismatch = bool(overmind_bios) and overmind_bios != local_bios
    if not romset_mismatch and not bios_mismatch:
        return
    if _ASSET_PUSH_REQUESTED.is_set():
        return
    _ASSET_PUSH_REQUESTED.set()
    _ROM_METADATA_WAKE.set()
    print(
        "Asset thumbprint mismatch from heartbeat; queued resync push: "
        f"romset_mismatch={romset_mismatch} bios_mismatch={bios_mismatch} "
        f"overmind_romset={overmind_romset[:12]} local_romset={local_romset[:12]} "
        f"overmind_bios={overmind_bios[:12]} local_bios={local_bios[:12]}",
        file=sys.stdout,
        flush=True,
    )


def _local_saves_thumbprint(settings: Settings) -> str:
    """Return the saves thumbprint the Drone last persisted for its on-disk saves."""
    try:
        state = _read_rom_metadata_cache_state(settings, "saves_files_thumbprint")
    except Exception:
        return ""
    return str(state.get("saves_files_thumbprint") or "").strip()


def _maybe_request_saves_push_from_heartbeat(settings: Settings, response: dict) -> None:
    """Queue a saves resync when Overmind's echoed saves thumbprint drifts from ours."""
    if not isinstance(response, dict):
        return
    overmind_saves = str(response.get("saves_files_thumbprint") or "").strip()
    local_saves = _local_saves_thumbprint(settings)
    # Only act once the Drone has stored a saves thumbprint AND Overmind actually echoed one
    # that differs. Treating an empty/absent Overmind thumbprint as drift (an Overmind that
    # doesn't yet report saves) would re-push the full saves set on every heartbeat — mirror
    # the bios guard (bool(overmind_bios) and ...) in _maybe_request_asset_push_from_heartbeat.
    if not local_saves or not overmind_saves or overmind_saves == local_saves:
        return
    if _SAVES_PUSH_REQUESTED.is_set():
        return
    _SAVES_PUSH_REQUESTED.set()
    _ROM_METADATA_WAKE.set()
    print(
        "Saves thumbprint mismatch from heartbeat; queued resync push: "
        f"overmind_saves={overmind_saves[:12]} local_saves={local_saves[:12]}",
        file=sys.stdout,
        flush=True,
    )


def _build_rom_metadata_snapshot_from_cache(settings: Settings, cache: dict, rehydrate_gamelist: bool = False) -> dict:
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    roms = []
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        row = {k: v for k, v in entry.items() if k != "absolute_path"}
        gamelist_details = (
            _gamelist_metadata_for_reference(
                str(row.get("gamelist_path") or ""),
                str(row.get("gamelist_game_id") or row.get("file_path") or row.get("rom_path") or ""),
            )
            if rehydrate_gamelist
            else {}
        )
        row["gamelist"] = gamelist_details
        row["existing"] = {field: str(gamelist_details.get(field) or "") for field in ARTWORK_FIELDS}
        row["has_gamelist_entry"] = bool(row.get("gamelist_path"))
        row["metadata_source"] = "gamelist.xml" if row.get("gamelist_path") else row.get("metadata_source") or "filesystem"
        title = str(gamelist_details.get("name") or "").strip()
        if title:
            row["name"] = title
            row["rom_name"] = title
            row["title"] = title
        roms.append(row)
    roms.sort(key=lambda row: (str(row.get("system") or ""), str(row.get("file_path") or "")))
    bios_entries = cache.get("bios_entries") if isinstance(cache.get("bios_entries"), dict) else {}
    bios = []
    for entry in bios_entries.values():
        if not isinstance(entry, dict):
            continue
        bios.append({k: v for k, v in entry.items() if k != "absolute_path"})
    bios.sort(key=lambda row: str(row.get("file_path") or row.get("path") or ""))
    artwork_entries = cache.get("artwork_entries") if isinstance(cache.get("artwork_entries"), dict) else {}
    artwork = []
    for entry in artwork_entries.values():
        if not isinstance(entry, dict):
            continue
        artwork.append(dict(entry))
    artwork.sort(key=lambda row: (str(row.get("system") or ""), str(row.get("rom_path") or "")))
    fingerprint = _rom_inventory_fingerprint(roms)
    bios_thumbprint = _bios_inventory_fingerprint(bios)
    return {
        "type": "asset_metadata",
        "collected_at": cache.get("last_full_scan_at") or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "roms_root": str(settings.roms_root),
        "bios_root": str(settings.bios_root),
        "rom_inventory_fingerprint": fingerprint,
        "rom_inventory_fingerprint_algorithm": ROM_INVENTORY_FINGERPRINT_ALGORITHM,
        "romset_files_thumbprint": fingerprint,
        "bios_files_thumbprint": bios_thumbprint,
        "bios_inventory_fingerprint_algorithm": BIOS_INVENTORY_FINGERPRINT_ALGORITHM,
        "systems": cache.get("systems") if isinstance(cache.get("systems"), list) else [],
        "roms": roms,
        "bios": bios,
        "artwork": artwork,
        "gamelists": cache.get("gamelists") if isinstance(cache.get("gamelists"), list) else [],
        "cache": {"schema_version": ROM_METADATA_CACHE_VERSION},
    }


def _rom_metadata_inventory_id(settings: Settings, snapshot: dict) -> str:
    counts = (
        len(snapshot.get("roms") if isinstance(snapshot.get("roms"), list) else []),
        len(snapshot.get("bios") if isinstance(snapshot.get("bios"), list) else []),
        len(snapshot.get("artwork") if isinstance(snapshot.get("artwork"), list) else []),
    )
    return f"{settings.overmind_device_id}:{snapshot.get('collected_at') or ''}:{counts[0]}:{counts[1]}:{counts[2]}"


def _chunk_rom_metadata_inventory(
    settings: Settings,
    snapshot: dict,
    chunk_size: Optional[int] = None,
    *,
    replace_all: bool = False,
) -> List[dict]:
    chunk_size = max(1, int(chunk_size or ROM_METADATA_UPLOAD_CHUNK_SIZE))
    roms = _wire_asset_rows(snapshot.get("roms") if isinstance(snapshot.get("roms"), list) else [])
    bios = _wire_asset_rows(snapshot.get("bios") if isinstance(snapshot.get("bios"), list) else [])
    artwork = _wire_asset_rows(snapshot.get("artwork") if isinstance(snapshot.get("artwork"), list) else [])
    rows = [("roms", row) for row in roms] + [("bios", row) for row in bios] + [("artwork", row) for row in artwork]
    base = {
        "device_id": settings.overmind_device_id,
        "type": snapshot.get("type") or "asset_metadata",
        "collected_at": snapshot.get("collected_at"),
        "roms_root": snapshot.get("roms_root"),
        "bios_root": snapshot.get("bios_root"),
        "rom_inventory_fingerprint": snapshot.get("rom_inventory_fingerprint") or _rom_inventory_fingerprint(roms),
        "rom_inventory_fingerprint_algorithm": snapshot.get("rom_inventory_fingerprint_algorithm") or ROM_INVENTORY_FINGERPRINT_ALGORITHM,
        "romset_files_thumbprint": snapshot.get("romset_files_thumbprint") or snapshot.get("rom_inventory_fingerprint") or _rom_inventory_fingerprint(roms),
        "bios_files_thumbprint": snapshot.get("bios_files_thumbprint") or _bios_inventory_fingerprint(bios),
        "systems": snapshot.get("systems") if isinstance(snapshot.get("systems"), list) else [],
        "gamelists": snapshot.get("gamelists") if isinstance(snapshot.get("gamelists"), list) else [],
        "cache": snapshot.get("cache") if isinstance(snapshot.get("cache"), dict) else {},
        "replace_all": bool(replace_all),
    }
    if len(rows) <= chunk_size:
        return [{**base, "update_mode": "inventory", "roms": roms, "bios": bios, "artwork": artwork}]

    chunks = []
    total = (len(rows) + chunk_size - 1) // chunk_size
    inventory_id = _rom_metadata_inventory_id(settings, snapshot)
    counts = {"roms": len(roms), "bios": len(bios), "artwork": len(artwork)}
    for index in range(total):
        chunk_rows = rows[index * chunk_size:(index + 1) * chunk_size]
        payload = {
            **base,
            "update_mode": "inventory_chunk",
            "inventory_id": inventory_id,
            "chunk_index": index,
            "chunk_total": total,
            "inventory_complete": index == total - 1,
            "inventory_counts": counts,
            "roms": [],
            "bios": [],
            "artwork": [],
        }
        for asset_type, row in chunk_rows:
            payload[asset_type].append(row)
        chunks.append(payload)
    return chunks


def _wire_asset_rows(rows: list) -> list:
    return [
        {key: value for key, value in row.items() if key != "absolute_path"}
        for row in rows
        if isinstance(row, dict)
    ]


def _chunk_rom_metadata_delta(settings: Settings, snapshot: dict, changes: dict, chunk_size: Optional[int] = None) -> List[dict]:
    chunk_size = max(1, int(chunk_size or ROM_METADATA_UPLOAD_CHUNK_SIZE))
    deleted = changes.get("deleted") if isinstance(changes.get("deleted"), dict) else {}
    rows = (
        [("roms", "upsert", row) for row in _wire_asset_rows(changes.get("roms") or [])]
        + [("bios", "upsert", row) for row in _wire_asset_rows(changes.get("bios") or [])]
        + [("artwork", "upsert", row) for row in _wire_asset_rows(changes.get("artwork") or [])]
        + [("roms", "delete", row) for row in _wire_asset_rows(deleted.get("roms") or [])]
        + [("bios", "delete", row) for row in _wire_asset_rows(deleted.get("bios") or [])]
        + [("artwork", "delete", row) for row in _wire_asset_rows(deleted.get("artwork") or [])]
    )
    if not rows:
        return []
    base = {
        "device_id": settings.overmind_device_id,
        "type": snapshot.get("type") or "asset_metadata",
        "update_mode": "inventory_delta",
        "collected_at": snapshot.get("collected_at"),
        "rom_inventory_fingerprint": snapshot.get("rom_inventory_fingerprint") or _rom_inventory_fingerprint(snapshot.get("roms") if isinstance(snapshot.get("roms"), list) else []),
        "rom_inventory_fingerprint_algorithm": snapshot.get("rom_inventory_fingerprint_algorithm") or ROM_INVENTORY_FINGERPRINT_ALGORITHM,
        "romset_files_thumbprint": snapshot.get("romset_files_thumbprint") or snapshot.get("rom_inventory_fingerprint") or _rom_inventory_fingerprint(snapshot.get("roms") if isinstance(snapshot.get("roms"), list) else []),
        "bios_files_thumbprint": snapshot.get("bios_files_thumbprint") or _bios_inventory_fingerprint(snapshot.get("bios") if isinstance(snapshot.get("bios"), list) else []),
        "systems": snapshot.get("systems") if isinstance(snapshot.get("systems"), list) else [],
    }
    payloads = []
    total = (len(rows) + chunk_size - 1) // chunk_size
    for index, start in enumerate(range(0, len(rows), chunk_size)):
        payload = {
            **base,
            "delta_index": index,
            "delta_total": total,
            "inventory_complete": index == total - 1,
            "roms": [],
            "bios": [],
            "artwork": [],
            "deleted": {"roms": [], "bios": [], "artwork": []},
        }
        for asset_type, operation, row in rows[start:start + chunk_size]:
            if operation == "delete":
                payload["deleted"][asset_type].append(row)
            else:
                payload[asset_type].append(row)
        payloads.append(payload)
    return payloads


def _json_payload_size_bytes(payload: dict) -> int:
    try:
        return len(json.dumps(payload).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _mark_rom_metadata_upload_clean(
    settings: Settings,
    fingerprint: Optional[str] = None,
    bios_thumbprint: Optional[str] = None,
) -> None:
    state = {
        "last_successful_upload_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "dirty": False,
        "full_refresh_pending": False,
    }
    if fingerprint:
        state["rom_inventory_fingerprint"] = fingerprint
        state["rom_inventory_fingerprint_algorithm"] = ROM_INVENTORY_FINGERPRINT_ALGORITHM
        state["romset_files_thumbprint"] = fingerprint
    if bios_thumbprint is not None:
        state["bios_files_thumbprint"] = bios_thumbprint
        state["bios_inventory_fingerprint_algorithm"] = BIOS_INVENTORY_FINGERPRINT_ALGORITHM
    _clear_pending_rom_metadata_changes(settings)
    _update_rom_metadata_cache_state(settings, **state)
    # The Drone has now told Overmind its current thumbprints; any pending
    # heartbeat-driven resync request is satisfied.
    _ASSET_PUSH_REQUESTED.clear()


def _rom_metadata_cache_status(settings: Settings) -> dict:
    cache, rebuilt = _load_rom_metadata_cache(settings)
    changes = _read_pending_rom_metadata_changes(settings)
    roms = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    bios = cache.get("bios_entries") if isinstance(cache.get("bios_entries"), dict) else {}
    artwork = cache.get("artwork_entries") if isinstance(cache.get("artwork_entries"), dict) else {}
    deleted = changes.get("deleted") if isinstance(changes.get("deleted"), dict) else {}
    pending = {
        "roms": len(changes.get("roms") if isinstance(changes.get("roms"), list) else []),
        "bios": len(changes.get("bios") if isinstance(changes.get("bios"), list) else []),
        "artwork": len(changes.get("artwork") if isinstance(changes.get("artwork"), list) else []),
        "deleted_roms": len(deleted.get("roms") if isinstance(deleted.get("roms"), list) else []),
        "deleted_bios": len(deleted.get("bios") if isinstance(deleted.get("bios"), list) else []),
        "deleted_artwork": len(deleted.get("artwork") if isinstance(deleted.get("artwork"), list) else []),
    }
    pending["total"] = sum(pending.values())
    complete = bool(cache.get("last_full_scan_at")) and not bool(cache.get("scan_in_progress"))
    uploaded = bool(cache.get("last_successful_upload_at"))
    cached_assets = len(roms) + len(bios) + len(artwork)
    return {
        "path": str(_rom_metadata_cache_path(settings)),
        "schema_version": cache.get("schema_version"),
        "rebuilt": rebuilt,
        "active": _ROM_METADATA_ACTIVE.is_set(),
        "poller_enabled": settings.rom_metadata_poll_seconds != 0,
        "poll_seconds": settings.rom_metadata_poll_seconds,
        "watch_enabled": ROM_METADATA_WATCH_ENABLED,
        "watch_active": _ROM_METADATA_WATCHER is not None,
        "rom_hashing_enabled": ROM_METADATA_HASH_ROMS_ENABLED,
        "initial_delay_seconds": ROM_METADATA_INITIAL_DELAY_SECONDS,
        "complete": complete,
        "uploaded": uploaded,
        "needs_upload": bool(cached_assets and (cache.get("dirty") or cache.get("full_refresh_pending") or pending["total"])),
        "dirty": bool(cache.get("dirty")),
        "full_refresh_pending": bool(cache.get("full_refresh_pending")),
        "scan_in_progress": bool(cache.get("scan_in_progress")),
        "last_full_scan_at": cache.get("last_full_scan_at"),
        "last_successful_upload_at": cache.get("last_successful_upload_at"),
        "scan_checkpoint_at": cache.get("scan_checkpoint_at"),
        "counts": {
            "systems": len(cache.get("systems") if isinstance(cache.get("systems"), list) else []),
            "roms": len(roms),
            "bios": len(bios),
            "artwork": len(artwork),
            "total": cached_assets,
        },
        "pending_changes": pending,
    }


def _begin_rom_metadata_activity(reason: str) -> bool:
    if not _ROM_METADATA_LOCK.acquire(blocking=False):
        _overmind_log(f"Asset metadata {reason} skipped: metadata work already running")
        return False
    _ROM_METADATA_ACTIVE.set()
    return True


def _end_rom_metadata_activity() -> None:
    _ROM_METADATA_ACTIVE.clear()
    _ROM_METADATA_LOCK.release()


def _poll_rom_metadata_cache(settings: Settings, repository: "RomRepository") -> Tuple[dict, bool, dict]:
    started = time.monotonic()
    _overmind_log("Asset metadata poll started: phase=cache_load")
    cache_load_started = time.monotonic()
    cache, rebuilt = _load_rom_metadata_cache(settings)
    was_dirty = bool(cache.get("dirty"))
    resuming_scan = bool(cache.get("scan_in_progress"))
    existing_entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    existing_bios_entries = cache.get("bios_entries") if isinstance(cache.get("bios_entries"), dict) else {}
    existing_artwork_entries = cache.get("artwork_entries") if isinstance(cache.get("artwork_entries"), dict) else {}
    # fingerprint snapshot kept by a cache purge so a clean rebuild does not re-hash files.
    preserved_fingerprint = _read_preserved_asset_fingerprint(settings)
    preserved_rom_fingerprint = preserved_fingerprint.get("rom") or {}
    preserved_bios_md5 = preserved_fingerprint.get("bios") or {}
    print(
        f"Asset metadata cache load completed: entries={len(existing_entries)} bios_entries={len(existing_bios_entries)} artwork_entries={len(existing_artwork_entries)} duration_ms={int((time.monotonic() - cache_load_started) * 1000)}",
        file=sys.stdout,
        flush=True,
    )
    previous_keys = set(existing_entries.keys())
    previous_bios_keys = set(existing_bios_entries.keys())
    previous_artwork_keys = set(existing_artwork_entries.keys())
    next_entries: Dict[str, dict] = {}
    next_bios_entries: Dict[str, dict] = {}
    next_artwork_entries: Dict[str, dict] = {}
    persisted_entries = dict(existing_entries)
    persisted_bios_entries = dict(existing_bios_entries)
    new_or_changed: List[Tuple[str, Path, dict]] = []
    bios_new_or_changed: List[Tuple[str, Path, dict]] = []
    systems_scanned = 0
    discovered = 0
    bios_discovered = 0
    artwork_discovered = 0
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    last_checkpoint = started

    def checkpoint_scan(phase: str, *, force: bool = False) -> None:
        nonlocal last_checkpoint
        has_new_work = bool(new_or_changed) or bool(bios_new_or_changed)
        if not (rebuilt or resuming_scan or has_new_work):
            return
        processed = discovered + bios_discovered
        now = time.monotonic()
        if (
            not force
            and processed % max(1, ROM_METADATA_PROGRESS_FILES) != 0
            and now - last_checkpoint < ROM_METADATA_PROGRESS_SECONDS
        ):
            return
        cache["entries"] = {**existing_entries, **next_entries}
        cache["bios_entries"] = {**existing_bios_entries, **next_bios_entries}
        cache["systems"] = systems
        cache["gamelists"] = gamelists
        cache["dirty"] = True
        cache["scan_in_progress"] = True
        cache["scan_checkpoint_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        rom_updates = {
            key: value for key, value in next_entries.items()
            if persisted_entries.get(key) != value
        }
        bios_updates = {
            key: value for key, value in next_bios_entries.items()
            if persisted_bios_entries.get(key) != value
        }
        _persist_rom_metadata_cache(
            settings,
            cache,
            rom_updates=rom_updates,
            bios_updates=bios_updates,
        )
        persisted_entries.update(rom_updates)
        persisted_bios_entries.update(bios_updates)
        print(
            f"Asset metadata checkpoint saved: phase={phase} roms={len(next_entries)} bios={len(next_bios_entries)}",
            file=sys.stdout,
            flush=True,
        )
        last_checkpoint = now

    _overmind_log("Asset metadata poll phase=scan")
    try:
        system_names = repository.list_system_names()
    except FileNotFoundError:
        system_names = []
    systems = []
    gamelists = []
    for system_name in system_names:
        system_name = str(system_name or "").strip()
        if not system_name:
            continue
        systems_scanned += 1
        try:
            system_dir = repository.get_system_dir(system_name)
            gamelist, roms = repository.list_gamelist_rom_metadata(system_name, system_dir)
            gamelists.append(gamelist)
        except Exception as error:
            print(f"ROM metadata scan warning: system={system_name} error={_format_overmind_error(error)}", file=sys.stderr, flush=True)
            continue
        system_discovered = 0
        for rom in roms:
            file_path = str(rom.get("file_path") or rom.get("relative_path") or rom.get("rom_path") or rom.get("rom_file") or "").strip()
            absolute_path = str(rom.get("absolute_path") or "").strip()
            if not file_path or not absolute_path:
                continue
            absolute = Path(absolute_path)
            entry_type = str(rom.get("entry_type") or "file").strip().lower()
            discovered += 1
            system_discovered += 1
            key = _rom_cache_entry_key(system_name, file_path)
            stat_size = int(rom.get("file_size") or rom.get("byte_count") or absolute.stat().st_size)
            stat_mtime = int(rom.get("modified_time") or rom.get("mtime") or absolute.stat().st_mtime)
            previous = existing_entries.get(key) if isinstance(existing_entries.get(key), dict) else {}
            base_entry = _database_rom_metadata_fields(rom, system_name, file_path, absolute, stat_size, stat_mtime)
            previous_fingerprint = (previous.get("rom_fingerprint") or previous.get("fingerprint")) if previous else None
            reuse_fingerprint = None
            if previous and previous.get("file_size") == stat_size and previous_fingerprint:
                reuse_fingerprint = previous_fingerprint
            else:
                # After a purge the entry rows are gone; reuse fingerprint from the
                # snapshot for files whose size and mtime are unchanged.
                kept = preserved_rom_fingerprint.get(key)
                if kept and kept.get("fingerprint") and kept.get("file_size") == stat_size and kept.get("modified_time") == stat_mtime:
                    reuse_fingerprint = kept["fingerprint"]
            if reuse_fingerprint:
                next_entries[key] = dict(base_entry)
                next_entries[key].update({"fingerprint": reuse_fingerprint, "rom_fingerprint": reuse_fingerprint})
            else:
                # No reusable fingerprint (new/changed file, or a value cleared by the
                # md5->fingerprint migration) -> queue it for hashing. Folders are never
                # hashed, so they just carry forward without a fingerprint.
                next_entries[key] = base_entry
                if entry_type != "folder":
                    new_or_changed.append((key, absolute, base_entry))
            checkpoint_scan("rom_scan")
        if system_discovered:
            systems.append({"name": system_name, "rom_count": system_discovered})

    checkpoint_scan("rom_scan_complete", force=bool(discovered))
    deleted = previous_keys - set(next_entries.keys())
    try:
        bios_root = repository.get_bios_root()
        for bios_path in sorted(bios_root.rglob("*"), key=lambda item: str(item.relative_to(bios_root)).lower()):
            if not bios_path.is_file():
                continue
            relative_path = bios_path.relative_to(bios_root).as_posix()
            bios_discovered += 1
            key = _bios_cache_entry_key(relative_path)
            stat = bios_path.stat()
            stat_size = int(stat.st_size)
            stat_mtime = int(stat.st_mtime)
            previous = existing_bios_entries.get(key) if isinstance(existing_bios_entries.get(key), dict) else {}
            base_entry = {
                "entry_type": "file",
                "name": bios_path.name,
                "path": relative_path,
                "file_path": relative_path,
                "relative_path": relative_path,
                "unique_id": repository.build_unique_id(bios_path),
                "file_size": stat_size,
                "byte_count": stat_size,
                "size": stat_size,
                "modified_time": stat_mtime,
                "mtime": stat_mtime,
                "absolute_path": str(bios_path),
            }
            reuse_bios_md5 = None
            if previous and previous.get("file_size") == stat_size and previous.get("modified_time") == stat_mtime and previous.get("md5"):
                reuse_bios_md5 = previous.get("md5")
            else:
                kept = preserved_bios_md5.get(key)
                if kept and kept.get("md5") and kept.get("file_size") == stat_size and kept.get("modified_time") == stat_mtime:
                    reuse_bios_md5 = kept["md5"]
            if reuse_bios_md5:
                next_bios_entries[key] = {**base_entry, "md5": reuse_bios_md5, "bios_md5": (previous.get("bios_md5") if previous else None) or reuse_bios_md5}
            else:
                next_bios_entries[key] = base_entry
                bios_new_or_changed.append((key, bios_path, base_entry))
            checkpoint_scan("bios_scan")
    except FileNotFoundError:
        pass
    except Exception as error:
        print(f"BIOS metadata scan warning: error={_format_overmind_error(error)}", file=sys.stderr, flush=True)
    checkpoint_scan("bios_scan_complete", force=bool(bios_discovered))
    bios_deleted = previous_bios_keys - set(next_bios_entries.keys()) - {key for key, _, _ in bios_new_or_changed}
    try:
        for artwork in repository.list_artwork_metadata():
            key = _artwork_cache_entry_key(str(artwork.get("system") or ""), str(artwork.get("rom_path") or artwork.get("file_path") or ""))
            if not key.split(":", 1)[-1]:
                continue
            artwork_discovered += 1
            # Normalize the scanned artwork into the same canonical payload shape the cache
            # round-trips (ArtworkCacheRow.to_payload). Without this, the raw scan dict never
            # equals the cached entry, so every artwork is re-queued as "changed" on every
            # poll and the whole artwork set is re-uploaded forever (a CPU-pinning resync loop).
            clean = ArtworkCacheRow.from_payload(key, {**artwork, "asset_type": "artwork"}).to_payload()
            next_artwork_entries[key] = clean
    except Exception as error:
        print(f"Artwork metadata scan warning: error={_format_overmind_error(error)}", file=sys.stderr, flush=True)
    artwork_deleted = previous_artwork_keys - set(next_artwork_entries.keys())
    artwork_changed = next_artwork_entries != existing_artwork_entries
    print(
        f"Asset metadata poll scan complete: systems={systems_scanned} roms={discovered} bios={bios_discovered} artwork={artwork_discovered} new_or_changed={len(new_or_changed)} bios_new_or_changed={len(bios_new_or_changed)} deleted={len(deleted)} bios_deleted={len(bios_deleted)} artwork_deleted={len(artwork_deleted)}",
        file=sys.stdout,
        flush=True,
    )

    if bios_new_or_changed:
        hash_started = time.monotonic()
        last_log = hash_started
        print(f"BIOS metadata poll phase=md5_hashing count={len(bios_new_or_changed)}", file=sys.stdout, flush=True)
        for bios_index, (key, absolute, entry) in enumerate(bios_new_or_changed, start=1):
            # BIOS uses a full-file MD5 (exact emulator identity), not the sampled ROM fingerprint.
            md5_value = RomRepository.build_md5(absolute)
            next_bios_entries[key] = {**entry, "md5": md5_value, "bios_md5": md5_value}
            now = time.monotonic()
            if bios_index == len(bios_new_or_changed) or bios_index % max(1, ROM_METADATA_PROGRESS_FILES) == 0 or now - last_log >= ROM_METADATA_PROGRESS_SECONDS:
                checkpoint_scan("bios_md5", force=True)
                print(f"BIOS metadata md5 progress: {bios_index}/{len(bios_new_or_changed)} files", file=sys.stdout, flush=True)
                last_log = now
        print(
            f"BIOS metadata md5 hashing completed: count={len(bios_new_or_changed)} duration_ms={int((time.monotonic() - hash_started) * 1000)}",
            file=sys.stdout,
            flush=True,
        )

    rom_metadata_changed = next_entries != existing_entries
    gamelists_changed = gamelists != (cache.get("gamelists") if isinstance(cache.get("gamelists"), list) else [])
    systems_changed = systems != (cache.get("systems") if isinstance(cache.get("systems"), list) else [])
    changed = (
        rebuilt
        or bool(new_or_changed)
        or bool(deleted)
        or rom_metadata_changed
        or systems_changed
        or gamelists_changed
        or bool(bios_new_or_changed)
        or bool(bios_deleted)
        or artwork_changed
        or was_dirty
    )
    cache["entries"] = next_entries
    cache["bios_entries"] = next_bios_entries
    cache["artwork_entries"] = next_artwork_entries
    cache["systems"] = systems
    cache["gamelists"] = gamelists
    cache["last_full_scan_at"] = now_iso
    cache["dirty"] = changed
    cache["scan_in_progress"] = False
    _overmind_log("Asset metadata poll phase=cache_write")
    cache_write_started = time.monotonic()
    rom_updates = {
        key: value for key, value in next_entries.items()
        if persisted_entries.get(key) != value
    }
    bios_updates = {
        key: value for key, value in next_bios_entries.items()
        if persisted_bios_entries.get(key) != value
    }
    artwork_updates = {
        key: value for key, value in next_artwork_entries.items()
        if existing_artwork_entries.get(key) != value
    }
    _persist_rom_metadata_cache(
        settings,
        cache,
        rom_updates=rom_updates,
        bios_updates=bios_updates,
        artwork_updates=artwork_updates,
        rom_deletes=set(persisted_entries) - set(next_entries),
        bios_deletes=set(persisted_bios_entries) - set(next_bios_entries),
        artwork_deletes=artwork_deleted,
        rom_deleted_rows={key: existing_entries[key] for key in deleted if key in existing_entries},
        bios_deleted_rows={key: existing_bios_entries[key] for key in bios_deleted if key in existing_bios_entries},
        artwork_deleted_rows={key: existing_artwork_entries[key] for key in artwork_deleted if key in existing_artwork_entries},
    )
    print(
        f"Asset metadata cache write completed: entries={len(next_entries)} bios_entries={len(next_bios_entries)} artwork_entries={len(next_artwork_entries)} changed={changed} write_duration_ms={int((time.monotonic() - cache_write_started) * 1000)} total_poll_duration_ms={int((time.monotonic() - started) * 1000)}",
        file=sys.stdout,
        flush=True,
    )
    stats = {
        "systems_scanned": systems_scanned,
        "roms_discovered": discovered,
        "bios_discovered": bios_discovered,
        "artwork_discovered": artwork_discovered,
        "new_or_changed": len(new_or_changed),
        "roms_pending_fingerprint": len(new_or_changed),
        "bios_new_or_changed": len(bios_new_or_changed),
        "deleted": len(deleted),
        "bios_deleted": len(bios_deleted),
        "artwork_deleted": len(artwork_deleted),
        "artwork_changed": artwork_changed,
        "rebuilt": rebuilt,
        "had_cached_assets": bool(existing_entries or existing_bios_entries or existing_artwork_entries),
        "had_successful_upload": bool(cache.get("last_successful_upload_at")),
        "full_refresh_pending": bool(cache.get("full_refresh_pending")),
    }
    return _build_rom_metadata_snapshot_from_cache(settings, cache), changed, stats


def _hash_rom_metadata_batches(settings: Settings, repository: "RomRepository", batch_size: int = ROM_METADATA_FINGERPRINT_BATCH_SIZE):
    """Yield bounded hash patches for ROM entries missing a current fingerprint."""
    if not ROM_METADATA_HASH_ROMS_ENABLED:
        return
    cache, _ = _load_rom_metadata_cache(settings)
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    pending = [
        (key, entry)
        for key, entry in entries.items()
        if isinstance(entry, dict) and not entry.get("rom_fingerprint") and entry.get("absolute_path")
    ]
    total = len(pending)
    if not total:
        return
    batch_size = max(1, int(batch_size))
    started = time.monotonic()
    last_checkpoint = started
    budget_seconds = ROM_METADATA_HASH_BUDGET_SECONDS
    print(f"ROM metadata phase=fingerprint_hashing count={total} batch_size={batch_size} budget_seconds={budget_seconds}", file=sys.stdout, flush=True)
    patch = []
    pending_updates = {}
    budget_exhausted = False
    hashed = 0
    for processed, (key, entry) in enumerate(pending, start=1):
        if budget_seconds and (time.monotonic() - started) >= budget_seconds:
            # Stop starting new files once the per-poll budget is spent; flush any
            # accumulated patch/updates below, then resume on the next poll.
            budget_exhausted = True
            break
        absolute = Path(str(entry.get("absolute_path") or ""))
        if absolute.exists() and absolute.is_file():
            fingerprint_value = repository.build_fingerprint(absolute)
            updated = {**entry, "fingerprint": fingerprint_value, "rom_fingerprint": fingerprint_value}
            entries[key] = updated
            pending_updates[key] = updated
            patch.append({k: v for k, v in updated.items() if k != "absolute_path"})
            hashed += 1
        now = time.monotonic()
        checkpoint_due = (
            bool(patch)
            and (
                processed == total
                or processed % max(1, ROM_METADATA_PROGRESS_FILES) == 0
                or now - last_checkpoint >= ROM_METADATA_PROGRESS_SECONDS
            )
        )
        if checkpoint_due:
            cache["entries"] = entries
            cache["dirty"] = True
            _persist_rom_metadata_cache(settings, cache, rom_updates=pending_updates)
            pending_updates = {}
            print(f"ROM metadata fingerprint checkpoint: {processed}/{total} files hashed", file=sys.stdout, flush=True)
            last_checkpoint = now
        if not patch or (len(patch) < batch_size and processed != total):
            continue
        if not checkpoint_due:
            cache["entries"] = entries
            cache["dirty"] = True
            _persist_rom_metadata_cache(settings, cache, rom_updates=pending_updates)
            pending_updates = {}
        print(f"ROM metadata fingerprint progress: {processed}/{total} files", file=sys.stdout, flush=True)
        yield {
            "type": "asset_metadata",
            "update_mode": "rom_hash_patch",
            "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "roms": patch,
            "hash_progress": {"processed": processed, "total": total, "complete": processed == total},
        }
        patch = []
    # Flush any work accumulated before an early budget break (the in-loop yield only
    # fires on a full batch or on the final file, neither of which is reached on break).
    if pending_updates:
        cache["entries"] = entries
        cache["dirty"] = True
        _persist_rom_metadata_cache(settings, cache, rom_updates=pending_updates)
        pending_updates = {}
    if patch:
        yield {
            "type": "asset_metadata",
            "update_mode": "rom_hash_patch",
            "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "roms": patch,
            # complete only when we exhausted the pending list, not on a budget break,
            # so Overmind does not mark the inventory fingerprint clean prematurely.
            "hash_progress": {"processed": hashed, "total": total, "complete": not budget_exhausted},
        }
        patch = []
    if budget_exhausted:
        print(
            f"ROM metadata fingerprint hashing paused (budget {budget_seconds}s reached): "
            f"hashed={hashed} remaining≈{total - hashed}/{total} resume on next poll "
            f"duration_ms={int((time.monotonic() - started) * 1000)}",
            file=sys.stdout,
            flush=True,
        )
    else:
        print(
            f"ROM metadata fingerprint hashing completed: count={total} duration_ms={int((time.monotonic() - started) * 1000)}",
            file=sys.stdout,
            flush=True,
        )


def _filesystem_events(settings: Settings, previous: Dict[str, dict], current: Dict[str, dict]) -> List[dict]:
    return _build_filesystem_events(
        settings,
        previous,
        current,
        event_type=OVERMIND_EVENT_TYPES["filesystem"],
    )


def _collect_game_logs(settings: Settings, repository: Optional["RomRepository"] = None, log_data: Optional[dict] = None) -> dict:
    return _build_game_log_payload(
        settings,
        repository,
        log_data,
        collect_log_sources=_collect_log_sources,
        format_error=_format_overmind_error,
    )


class DownloadCancelled(RuntimeError):
    pass


def _kick_asset_metadata_sync_after_download(settings: Settings, repository: "RomRepository", config: dict, reason: str) -> None:
    if not _local_network.is_overmind_mode(settings):
        _ROM_METADATA_WAKE.set()
        return
    base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
    token = str(config.get("overmind_token") or "").strip()
    if not base_url or not token:
        return

    def run() -> None:
        try:
            result = _sync_rom_metadata_to_overmind(settings, repository, config, base_url, token)
            _overmind_log(
                f"Asset metadata follow-up sync completed: reason={reason} status={result.get('status')} changed={result.get('changed')}"
            )
        except Exception as error:
            _overmind_log(
                f"Asset metadata follow-up sync failed: reason={reason} error={_format_overmind_error(error)}"
            )

    Thread(target=run, name="asset-metadata-follow-up-sync", daemon=True).start()


class DownloadManager:
    """Per-target Drone download queue with a small pool of worker threads.

    Running a few transfers concurrently markedly improves aggregate throughput
    over Wi-Fi (where a single TCP stream rarely fills the link) and hides the
    per-file TLS-handshake latency when copying many small files (artwork). The
    pool size is ``DRONE_DOWNLOAD_CONCURRENCY`` (default 3, clamped 1-8)."""

    def __init__(self, settings: Settings, repository: "RomRepository") -> None:
        self.settings = settings
        self.repository = repository
        self._lock = Lock()
        self._jobs: OrderedDict[str, dict] = OrderedDict()
        self._cancel_events: Dict[str, Event] = {}
        self._wake = Event()
        self._paused = False
        self._last_download_state_push_at = 0.0
        self._concurrency = self._resolve_concurrency()
        # Job selection + the queued->downloading transition happen atomically under
        # self._lock, so multiple workers never claim the same job. Per-job state is
        # likewise mutated under the lock, so running _run_job concurrently is safe.
        self._threads = []
        for index in range(self._concurrency):
            thread = Thread(target=self._worker, name=f"drone-download-worker-{index + 1}", daemon=True)
            thread.start()
            self._threads.append(thread)

    @staticmethod
    def _resolve_concurrency() -> int:
        try:
            value = int(os.environ.get("DRONE_DOWNLOAD_CONCURRENCY", "3"))
        except (TypeError, ValueError):
            value = 3
        return max(1, min(value, 8))

    def enqueue_rom(self, config: dict, peer: dict, system: str, relative_path: str, expected_size=None, expected_fingerprint=None, source_action_id: Optional[str] = None, entry_type: str = "file", sync_id: Optional[str] = None) -> dict:
        job_id = str(uuid.uuid4())
        peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        job = {
            "id": job_id,
            "job_id": job_id,
            "sync_id": sync_id or job_id,
            "source_action_id": source_action_id,
            "source_drone_id": peer_id,
            "target_drone_id": self.settings.overmind_device_id,
            "file_path": relative_path,
            "file_name": Path(relative_path).name,
            "file_type": "ROM",
            "entry_type": entry_type,
            "system": system,
            "rom_name": relative_path,
            "relative_path": relative_path,
            "total_bytes": expected_size,
            "file_size": expected_size,
            "downloaded_bytes": 0,
            "bytes_transferred": 0,
            "percentage": 0,
            "transfer_speed_bps": 0,
            "status": "queued",
            "queue_position": None,
            "started_at": None,
            "download_started_at": None,
            "completed_at": None,
            "download_completed_at": None,
            "error_message": None,
            "failure_reason": None,
            "cancellation_requested": False,
            "created_at": now,
            "_config": config,
            "_peer": peer,
            "_expected_fingerprint": expected_fingerprint,
            "_entry_type": entry_type,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = Event()
            self._update_queue_positions_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return snapshot

    def enqueue_bios(self, config: dict, peer: dict, relative_path: str, expected_size=None, expected_md5=None, source_action_id: Optional[str] = None) -> dict:
        job_id = str(uuid.uuid4())
        peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        job = {
            "id": job_id,
            "job_id": job_id,
            "sync_id": job_id,
            "source_action_id": source_action_id,
            "source_drone_id": peer_id,
            "target_drone_id": self.settings.overmind_device_id,
            "asset_type": "bios",
            "file_path": relative_path,
            "file_name": Path(relative_path).name,
            "file_type": "BIOS",
            "system": "bios",
            "bios_name": relative_path,
            "rom_name": relative_path,
            "relative_path": relative_path,
            "total_bytes": expected_size,
            "file_size": expected_size,
            "downloaded_bytes": 0,
            "bytes_transferred": 0,
            "percentage": 0,
            "transfer_speed_bps": 0,
            "status": "queued",
            "queue_position": None,
            "started_at": None,
            "download_started_at": None,
            "completed_at": None,
            "download_completed_at": None,
            "error_message": None,
            "failure_reason": None,
            "cancellation_requested": False,
            "created_at": now,
            "bios_md5": expected_md5,
            "_asset_type": "bios",
            "_config": config,
            "_peer": peer,
            "_expected_fingerprint": expected_md5,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = Event()
            self._update_queue_positions_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return snapshot

    def enqueue_artwork(self, config: dict, peer: dict, system: str, rom_path: str, artwork_type: str, source_action_id: Optional[str] = None, overwrite: bool = False, local_rom_path: Optional[str] = None) -> dict:
        job_id = str(uuid.uuid4())
        peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        label = f"{rom_path}:{artwork_type}"
        job = {
            "id": job_id,
            "job_id": job_id,
            "sync_id": job_id,
            "source_action_id": source_action_id,
            "source_drone_id": peer_id,
            "target_drone_id": self.settings.overmind_device_id,
            "asset_type": "artwork",
            "file_path": label,
            "file_name": Path(rom_path).name,
            "file_type": "ARTWORK",
            "system": system,
            "rom_name": rom_path,
            "rom_path": rom_path,
            "artwork_type": artwork_type,
            "relative_path": label,
            "total_bytes": None,
            "file_size": None,
            "downloaded_bytes": 0,
            "bytes_transferred": 0,
            "percentage": 0,
            "transfer_speed_bps": 0,
            "status": "queued",
            "queue_position": None,
            "started_at": None,
            "download_started_at": None,
            "completed_at": None,
            "download_completed_at": None,
            "error_message": None,
            "failure_reason": None,
            "cancellation_requested": False,
            "created_at": now,
            "_asset_type": "artwork",
            "_config": config,
            "_peer": peer,
            "_overwrite": bool(overwrite),
            "_local_rom_path": local_rom_path,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = Event()
            self._update_queue_positions_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return snapshot

    def enqueue_save(self, config: dict, peer: dict, system: str, relative_path: str, expected_size=None, expected_fingerprint=None) -> dict:
        job_id = str(uuid.uuid4())
        peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        job = {
            "id": job_id,
            "job_id": job_id,
            "sync_id": job_id,
            "source_drone_id": peer_id,
            "target_drone_id": self.settings.overmind_device_id,
            "asset_type": "saves",
            "file_path": relative_path,
            "file_name": Path(relative_path).name,
            "file_type": "Save",
            "system": system,
            "relative_path": relative_path,
            "total_bytes": expected_size,
            "file_size": expected_size,
            "downloaded_bytes": 0,
            "bytes_transferred": 0,
            "percentage": 0,
            "transfer_speed_bps": 0,
            "status": "queued",
            "queue_position": None,
            "started_at": None,
            "download_started_at": None,
            "completed_at": None,
            "download_completed_at": None,
            "error_message": None,
            "failure_reason": None,
            "cancellation_requested": False,
            "created_at": now,
            "_asset_type": "saves",
            "_config": config,
            "_peer": peer,
            "_expected_fingerprint": expected_fingerprint,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = Event()
            self._update_queue_positions_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return snapshot

    def cancel(self, job_id: str, reason: str = "cancelled by user") -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"status": "not_found", "job_id": job_id}
            if job.get("status") in DOWNLOAD_TERMINAL_STATUSES:
                return {"status": job.get("status"), "job": self._public_job_locked(job)}
            job["cancellation_requested"] = True
            job["cancel_reason"] = reason
            event = self._cancel_events.get(job_id)
            if event:
                event.set()
            if job.get("status") == "queued":
                job["status"] = "cancelled"
                job["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                job["download_completed_at"] = job["completed_at"]
                job["failure_reason"] = reason
                job["error_message"] = reason
                self._update_queue_positions_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return {"status": snapshot.get("status"), "job": snapshot}

    def retry(self, job_id: str) -> dict:
        with self._lock:
            original = self._jobs.get(job_id)
            if not original:
                return {"status": "not_found", "job_id": job_id}
            if original.get("status") not in {"failed", "cancelled"}:
                return {"status": "not_retryable", "job_id": job_id, "job": self._public_job_locked(original)}
            retry_job = {key: value for key, value in original.items() if key not in {"id", "job_id", "sync_id"}}
            retry_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            retry_job.update({
                "id": retry_id,
                "job_id": retry_id,
                "sync_id": retry_id,
                "status": "queued",
                "queue_position": None,
                "downloaded_bytes": 0,
                "bytes_transferred": 0,
                "percentage": 0,
                "transfer_speed_bps": 0,
                "started_at": None,
                "download_started_at": None,
                "completed_at": None,
                "download_completed_at": None,
                "error_message": None,
                "failure_reason": None,
                "cancellation_requested": False,
                "cancel_reason": None,
                "retried_from_job_id": job_id,
                "created_at": now,
            })
            retry_job.pop("_started_mono", None)
            self._jobs[retry_id] = retry_job
            self._cancel_events[retry_id] = Event()
            self._update_queue_positions_locked()
            snapshot = self._public_job_locked(retry_job)
        self._wake.set()
        return {"status": "queued", "job": snapshot, "retried_from_job_id": job_id}

    def find_pending_rom(self, system: str, relative_path: str, expected_fingerprint: Optional[str] = None) -> Optional[dict]:
        """Return an active/queued ROM job for the same local target or fingerprint."""
        system_norm = str(system or "").strip().lower()
        path_norm = str(relative_path or "").replace("\\", "/").strip().lstrip("./").lower()
        fp_norm = str(expected_fingerprint or "").strip().lower()
        with self._lock:
            for job in self._jobs.values():
                if job.get("status") not in {"queued", "downloading"}:
                    continue
                if str(job.get("file_type") or "").upper() != "ROM":
                    continue
                if str(job.get("system") or "").strip().lower() != system_norm:
                    continue
                job_path = str(job.get("relative_path") or job.get("file_path") or "").replace("\\", "/").strip().lstrip("./").lower()
                job_fp = str(job.get("_expected_fingerprint") or "").strip().lower()
                if path_norm and job_path == path_norm:
                    return self._public_job_locked(job)
                if fp_norm and job_fp == fp_norm:
                    return self._public_job_locked(job)
        return None

    def snapshot(self) -> dict:
        with self._lock:
            jobs = [self._public_job_locked(job) for job in self._jobs.values()]
            paused = self._paused
        active = [job for job in jobs if job.get("status") == "downloading"]
        queued = [job for job in jobs if job.get("status") == "queued"]
        recent = [job for job in jobs if job.get("status") in DOWNLOAD_TERMINAL_STATUSES][-25:]
        estimate = self._queue_estimate(active, queued, recent, paused, self._concurrency)
        return {
            "target_drone_id": self.settings.overmind_device_id,
            "concurrency": {"scope": "target_drone", "active_limit": self._concurrency},
            "paused": paused,
            "active": active,
            "queued": queued,
            "recent": list(reversed(recent)),
            "downloads": active + queued + list(reversed(recent)),
            **estimate,
        }

    @staticmethod
    def _queue_estimate(active: List[dict], queued: List[dict], recent: List[dict], paused: bool, concurrency: int = 1) -> dict:
        def safe_int(value: object) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        pending = active + queued
        known_remaining = 0
        unknown_size_count = 0
        known_sizes = []
        for job in pending:
            total = safe_int(job.get("total_bytes") or job.get("file_size"))
            downloaded = safe_int(job.get("downloaded_bytes") or job.get("bytes_transferred"))
            if total > 0:
                known_sizes.append(total)
                known_remaining += max(0, total - downloaded)
            else:
                unknown_size_count += 1

        if not known_sizes:
            known_sizes = [
                safe_int(job.get("total_bytes") or job.get("file_size"))
                for job in recent
                if safe_int(job.get("total_bytes") or job.get("file_size")) > 0
            ]
        average_size = int(sum(known_sizes) / len(known_sizes)) if known_sizes else 0
        estimated_unknown_bytes = average_size * unknown_size_count
        size_estimate_available = unknown_size_count == 0 or average_size > 0
        remaining_bytes = known_remaining + estimated_unknown_bytes if size_estimate_available else None

        parallel = max(1, int(concurrency or 1))
        speeds = [safe_int(job.get("transfer_speed_bps")) for job in active if safe_int(job.get("transfer_speed_bps")) > 0]
        speed_source = "active"
        if speeds:
            # Aggregate throughput across the concurrently-running streams is what
            # actually drains the queue.
            speed_bps = sum(speeds)
        else:
            recent_speeds = [
                safe_int(job.get("transfer_speed_bps"))
                for job in recent
                if job.get("status") == "completed" and safe_int(job.get("transfer_speed_bps")) > 0
            ]
            speed_source = "recent"
            # Project a single stream's typical speed across however many transfers
            # will run in parallel (bounded by the pool size and the work pending).
            per_stream = int(sum(recent_speeds) / len(recent_speeds)) if recent_speeds else 0
            speed_bps = per_stream * (min(parallel, len(pending)) if pending else 1)
        eta_seconds = int(remaining_bytes / speed_bps) if remaining_bytes is not None and remaining_bytes > 0 and speed_bps > 0 else None
        return {
            "queue_eta_seconds": eta_seconds,
            "queue_remaining_bytes": remaining_bytes,
            "queue_known_remaining_bytes": known_remaining,
            "queue_estimated_unknown_bytes": estimated_unknown_bytes,
            "queue_unknown_size_count": unknown_size_count,
            "queue_size_estimate_available": size_estimate_available,
            "queue_estimate_speed_bps": speed_bps,
            "queue_estimate_speed_source": speed_source if speed_bps else None,
            "queue_eta_state": "paused" if paused and pending else ("calculating" if pending and eta_seconds is None else "ready"),
        }

    def pause(self) -> dict:
        """Stop the worker from starting any further queued downloads. A job that
        is already downloading runs to completion (cancel it individually to stop
        it sooner)."""
        with self._lock:
            self._paused = True
        return self.snapshot()

    def resume(self) -> dict:
        with self._lock:
            self._paused = False
        self._wake.set()
        return self.snapshot()

    def clear_queue(self) -> dict:
        """Cancel every still-queued job so nothing further downloads. The active
        job (if any) is left running."""
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        cleared = 0
        with self._lock:
            for job in self._jobs.values():
                if job.get("status") == "queued":
                    job["status"] = "cancelled"
                    job["failure_reason"] = "queue cleared by user"
                    job["error_message"] = job["failure_reason"]
                    job["cancellation_requested"] = True
                    job["completed_at"] = now
                    job["download_completed_at"] = now
                    event = self._cancel_events.get(job.get("job_id"))
                    if event:
                        event.set()
                    cleared += 1
            self._update_queue_positions_locked()
        result = self.snapshot()
        result["cleared"] = cleared
        return result

    def _public_job_locked(self, job: dict) -> dict:
        public = {key: value for key, value in job.items() if not key.startswith("_")}
        downloaded = int(public.get("downloaded_bytes") or 0)
        total = public.get("total_bytes") or public.get("file_size")
        try:
            total_int = int(total)
        except Exception:
            total_int = 0
        public["total_bytes"] = total_int or None
        public["file_size"] = total_int or public.get("file_size")
        public["downloaded_bytes"] = downloaded
        public["bytes_transferred"] = downloaded
        public["percentage"] = round((downloaded / total_int) * 100, 1) if total_int else 0
        return public

    def _update_queue_positions_locked(self) -> None:
        position = 1
        for job in self._jobs.values():
            if job.get("status") == "queued":
                job["queue_position"] = position
                position += 1
            else:
                job["queue_position"] = None

    def _worker(self) -> None:
        while True:
            job_id = None
            with self._lock:
                if not self._paused:
                    for candidate_id, candidate in self._jobs.items():
                        if candidate.get("status") == "queued":
                            job_id = candidate_id
                            break
                if job_id:
                    job = self._jobs[job_id]
                    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                    job["status"] = "downloading"
                    job["started_at"] = now
                    job["download_started_at"] = now
                    job["_started_mono"] = time.monotonic()
                    self._update_queue_positions_locked()
            if not job_id:
                self._wake.wait(1)
                self._wake.clear()
                continue
            self._run_job(job_id)

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            config = job.get("_config") or {}
            peer = job.get("_peer") or {}
            system = str(job.get("system") or "")
            rel = str(job.get("relative_path") or job.get("file_path") or "")
            rom_path = str(job.get("rom_path") or job.get("rom_name") or rel)
            artwork_type = str(job.get("artwork_type") or "")
            artwork_overwrite = bool(job.get("_overwrite"))
            artwork_local_rom_path = job.get("_local_rom_path")
            expected_size = job.get("file_size") or job.get("total_bytes")
            expected_fingerprint = job.get("_expected_fingerprint")
            entry_type = str(job.get("_entry_type") or job.get("entry_type") or "file").lower()
            cancel_event = self._cancel_events.get(job_id) or Event()
            asset_type = str(job.get("_asset_type") or "rom").lower()
        self._push_download_state(config, "started", force=True)

        def progress(downloaded: int, total: Optional[int]) -> None:
            should_push = False
            with self._lock:
                current = self._jobs.get(job_id)
                if not current:
                    return
                current["downloaded_bytes"] = downloaded
                current["bytes_transferred"] = downloaded
                if total:
                    current["total_bytes"] = total
                    current["file_size"] = total
                started = float(current.get("_started_mono") or time.monotonic())
                elapsed = max(0.001, time.monotonic() - started)
                current["transfer_speed_bps"] = int(downloaded / elapsed)
                now = time.monotonic()
                if now - self._last_download_state_push_at >= DOWNLOAD_PROGRESS_PUSH_SECONDS:
                    self._last_download_state_push_at = now
                    should_push = True
            if should_push:
                self._push_download_state(config, "progress", force=True)

        try:
            if asset_type == "artwork":
                activity = _download_artwork_from_peer(
                    self.settings,
                    self.repository,
                    config,
                    peer,
                    system,
                    rom_path,
                    artwork_type,
                    progress_callback=progress,
                    cancellation_event=cancel_event,
                    overwrite=artwork_overwrite,
                    local_rom_path=artwork_local_rom_path,
                )
            elif asset_type == "saves":
                activity = _download_save_from_peer(
                    self.settings,
                    config,
                    peer,
                    system,
                    rel,
                    expected_size=expected_size,
                    expected_fingerprint=expected_fingerprint,
                    cancellation_event=cancel_event,
                )
            elif asset_type == "bios":
                activity = _download_bios_from_peer(
                    self.settings,
                    config,
                    peer,
                    rel,
                    expected_size=expected_size,
                    expected_md5=expected_fingerprint,
                    progress_callback=progress,
                    cancellation_event=cancel_event,
                )
            else:
                if entry_type == "folder":
                    activity = _download_rom_folder_from_peer(
                        self.settings,
                        config,
                        peer,
                        system,
                        rel,
                        expected_size=expected_size,
                        progress_callback=progress,
                        cancellation_event=cancel_event,
                    )
                else:
                    activity = _download_rom_from_peer(
                        self.settings,
                        config,
                        peer,
                        system,
                        rel,
                        expected_size=expected_size,
                        expected_fingerprint=expected_fingerprint,
                        progress_callback=progress,
                        cancellation_event=cancel_event,
                    )
            refresh_started = time.monotonic()
            try:
                refreshed = (
                    self.repository.list_artwork_metadata()
                    if asset_type == "artwork"
                    else (
                        self.repository.list_bios_entries()
                        if asset_type == "bios"
                        else (_saves_store.list_saves(self.settings.saves_root, system=system or None) if asset_type == "saves" else self.repository.list_assets(system, "roms")[1])
                    )
                )
                activity["inventory_refresh_status"] = "succeeded"
                activity["inventory_refresh_count"] = len(refreshed)
            except Exception as refresh_error:
                activity["inventory_refresh_status"] = "failed"
                activity["inventory_refresh_error"] = str(refresh_error)
            activity["inventory_refresh_duration_ms"] = int((time.monotonic() - refresh_started) * 1000)
            with self._lock:
                current = self._jobs.get(job_id)
                if current:
                    current.update(activity)
                    current["id"] = job_id
                    current["job_id"] = job_id
                    current["sync_id"] = job_id
        except DownloadCancelled as error:
            with self._lock:
                current = self._jobs.get(job_id)
                if current:
                    current["status"] = "cancelled"
                    current["failure_reason"] = str(error) or current.get("cancel_reason") or "cancelled"
                    current["error_message"] = current["failure_reason"]
                    current["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                    current["download_completed_at"] = current["completed_at"]
        except Exception as error:
            with self._lock:
                current = self._jobs.get(job_id)
                if current:
                    current["status"] = "failed"
                    current["failure_reason"] = str(error)
                    current["error_message"] = str(error)
                    current["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                    current["download_completed_at"] = current["completed_at"]
        finally:
            terminal_activity = None
            with self._lock:
                current = self._jobs.get(job_id)
                if current and current.get("status") in DOWNLOAD_TERMINAL_STATUSES:
                    terminal_activity = self._public_job_locked(current)
                self._update_queue_positions_locked()
            self._push_download_state(config, "completed", force=True)
            if terminal_activity:
                if _local_network.is_local_mode(self.settings):
                    _local_network.record_activity(self.settings, terminal_activity)
                _post_rom_sync_activity(self.settings, config, terminal_activity)
                if asset_type == "rom" and terminal_activity.get("status") == "completed":
                    _kick_asset_metadata_sync_after_download(self.settings, self.repository, config, "rom_download_completed")

    def _push_download_state(self, config: dict, reason: str, force: bool = False) -> None:
        if not force:
            now = time.monotonic()
            with self._lock:
                if now - self._last_download_state_push_at < DOWNLOAD_PROGRESS_PUSH_SECONDS:
                    return
                self._last_download_state_push_at = now
        _post_download_state(self.settings, config, self.snapshot(), reason=reason)


def _get_download_manager() -> Optional[DownloadManager]:
    return _DOWNLOAD_MANAGER


def _cached_rom_fingerprint_exists(settings: Settings, expected_fingerprint: Optional[str]) -> bool:
    expected = str(expected_fingerprint or "").strip().lower()
    if not expected:
        return False
    try:
        cache, _ = _load_rom_metadata_cache(settings)
    except Exception:
        return False
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        fingerprint_value = str(entry.get("rom_fingerprint") or entry.get("fingerprint") or "").strip().lower()
        if fingerprint_value == expected:
            return True
    return False


def _post_download_state(settings: Settings, config: dict, snapshot: dict, reason: str = "progress") -> None:
    if not _local_network.is_overmind_mode(settings):
        return
    base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
    token = str(config.get("overmind_token") or "").strip()
    if not base_url or not token:
        return
    device_id = quote(settings.overmind_device_id, safe="")
    endpoint = f"{base_url}/api/devices/{device_id}/downloads"
    try:
        active_count = len(snapshot.get("active") or [])
        queued_count = len(snapshot.get("queued") or [])
        recent_count = len(snapshot.get("recent") or [])
        print(
            f"Download state push started: endpoint={endpoint} reason={reason} active={active_count} queued={queued_count} recent={recent_count}",
            file=sys.stdout,
            flush=True,
        )
        _overmind_post_json(endpoint, snapshot, token=token, settings=settings)
        print(
            f"Download state push succeeded: reason={reason} active={active_count} queued={queued_count} recent={recent_count}",
            file=sys.stdout,
            flush=True,
        )
    except Exception as error:
        print(
            f"Download state push failed: reason={reason} error={_format_overmind_error(error)}",
            file=sys.stderr,
            flush=True,
        )


def _post_rom_sync_activity(settings: Settings, config: dict, activity: dict) -> None:
    if not _local_network.is_overmind_mode(settings):
        return
    base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
    token = str(config.get("overmind_token") or "").strip()
    if not base_url or not token:
        print(
            f"ROM sync activity push skipped: overmind not configured status={activity.get('status')} rom={activity.get('system')}/{activity.get('relative_path') or activity.get('rom_name')}",
            file=sys.stdout,
            flush=True,
        )
        return
    device_id = quote(settings.overmind_device_id, safe="")
    endpoint = f"{base_url}/api/devices/{device_id}/sync-activity"
    try:
        print(
            f"ROM sync activity push started: endpoint={endpoint} status={activity.get('status')} rom={activity.get('system')}/{activity.get('relative_path') or activity.get('rom_name')}",
            file=sys.stdout,
            flush=True,
        )
        _overmind_post_json(endpoint, activity, token=token, settings=settings)
        print(
            f"ROM sync activity push succeeded: status={activity.get('status')} bytes={activity.get('bytes_transferred')} rom={activity.get('system')}/{activity.get('relative_path') or activity.get('rom_name')}",
            file=sys.stdout,
            flush=True,
        )
    except Exception as error:
        print(
            f"ROM sync activity push failed: status={activity.get('status')} error={_format_overmind_error(error)} rom={activity.get('system')}/{activity.get('relative_path') or activity.get('rom_name')}",
            file=sys.stderr,
            flush=True,
        )


def _best_peer_for_rom(
    settings: Settings,
    repository: "RomRepository",
    config: dict,
    system: str,
    relative_path: str,
    source_device_ids: Optional[set] = None,
) -> Optional[dict]:
    swarm = _load_state_payload(
        _state_database_path(settings.userdata_root),
        "overmind_swarm.json",
        [],
        legacy_path=_overmind_swarm_path_for_settings(settings),
    )
    peer_checks = _load_state_payload(
        _state_database_path(settings.userdata_root),
        "peer_checks.json",
        [],
        legacy_path=_overmind_peer_results_path_for_settings(settings),
    )
    return _select_best_peer(
        swarm,
        peer_checks,
        settings.overmind_device_id,
        source_device_ids=source_device_ids,
        required_system=system,
    )


def _best_peer_for_bios(
    settings: Settings,
    config: dict,
    relative_path: str,
    source_device_ids: Optional[set] = None,
) -> Optional[dict]:
    swarm = _load_state_payload(
        _state_database_path(settings.userdata_root),
        "overmind_swarm.json",
        [],
        legacy_path=_overmind_swarm_path_for_settings(settings),
    )
    peer_checks = _load_state_payload(
        _state_database_path(settings.userdata_root),
        "peer_checks.json",
        [],
        legacy_path=_overmind_peer_results_path_for_settings(settings),
    )
    return _select_best_peer(swarm, peer_checks, settings.overmind_device_id, source_device_ids=source_device_ids)


def _download_rom_folder_from_peer(
    settings: Settings,
    config: dict,
    peer: dict,
    system: str,
    relative_path: str,
    expected_size=None,
    progress_callback=None,
    cancellation_event: Optional[Event] = None,
) -> dict:
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
    address = _peer_address(peer)
    if not address:
        raise RuntimeError("selected peer has no address")
    rel = _safe_rom_relative_path(relative_path)
    manifest_url = f"{address}/v1/api/peer/rom-manifest/{quote(system, safe='')}/{quote(rel, safe='/')}"
    system_dir = (settings.roms_root / system).resolve()
    target_dir = (system_dir / rel).resolve()
    if target_dir == system_dir or system_dir not in target_dir.parents:
        raise ValueError("invalid target path")
    if target_dir.exists() and not target_dir.is_dir():
        raise ValueError("target path exists and is not a directory")
    cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config)
    if address.startswith("https://") and not cafile:
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    manifest = _peer_get_json(manifest_url, settings, peer_id=peer_id, config=config)
    files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    directories = manifest.get("directories") if isinstance(manifest.get("directories"), list) else []
    if not files and not directories:
        raise RuntimeError("folder manifest is empty")

    started_dt = datetime.now(timezone.utc).replace(microsecond=0)
    started = started_dt.isoformat()
    started_mono = time.monotonic()
    bytes_written = 0
    total_bytes = None
    try:
        total_bytes = int(manifest.get("file_size") or expected_size or 0) or None
    except Exception:
        total_bytes = None

    def ensure_not_cancelled(partial: Optional[Path] = None) -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            if partial and partial.exists():
                partial.unlink()
            raise DownloadCancelled("download cancelled")

    target_dir.mkdir(parents=True, exist_ok=True)
    for directory in directories:
        child_dir = (target_dir / _safe_rom_relative_path(str(directory or ""))).resolve()
        if child_dir == target_dir or target_dir not in child_dir.parents:
            raise ValueError("invalid manifest directory path")
        child_dir.mkdir(parents=True, exist_ok=True)

    for item in files:
        if not isinstance(item, dict):
            continue
        child_rel = _safe_rom_relative_path(str(item.get("relative_path") or ""))
        target = (target_dir / child_rel).resolve()
        if target == target_dir or target_dir not in target.parents:
            raise ValueError("invalid manifest file path")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial_target = target.with_name(f"{target.name}.part")
        file_url = f"{address}/v1/api/peer/roms/{quote(system, safe='')}/{quote(rel + '/' + child_rel, safe='/')}"
        request = Request(file_url, headers={"User-Agent": "batocera-drone-rom-folder-sync/1.0"})
        context = _drone_client_ssl_context(settings, file_url, verify=bool(cafile), cafile=cafile)
        expected_file_size = item.get("file_size")
        file_bytes = 0
        try:
            with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, partial_target.open("wb") as handle:
                while True:
                    ensure_not_cancelled(partial_target)
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    file_bytes += len(chunk)
                    bytes_written += len(chunk)
                    if progress_callback:
                        progress_callback(bytes_written, total_bytes)
        except DownloadCancelled:
            if partial_target.exists():
                partial_target.unlink()
            raise
        except Exception:
            if partial_target.exists():
                partial_target.unlink()
            raise
        if expected_file_size not in (None, ""):
            try:
                if int(expected_file_size) != file_bytes:
                    if partial_target.exists():
                        partial_target.unlink()
                    raise RuntimeError(f"size mismatch for {child_rel} expected={expected_file_size} actual={file_bytes}")
            except ValueError:
                pass
        partial_target.replace(target)
    if expected_size not in (None, ""):
        try:
            if int(expected_size) != bytes_written:
                raise RuntimeError(f"size mismatch expected={expected_size} actual={bytes_written}")
        except ValueError:
            pass
    completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    return {
        "entry_type": "folder",
        "source_drone_id": peer_id,
        "target_drone_id": settings.overmind_device_id,
        "system": system,
        "rom_name": rel,
        "relative_path": target_dir.relative_to(system_dir).as_posix(),
        "action": "download",
        "status": "completed",
        "bytes_transferred": bytes_written,
        "file_size": expected_size or total_bytes or bytes_written,
        "download_started_at": started,
        "download_completed_at": completed_dt.isoformat(),
        "started_at": started,
        "completed_at": completed_dt.isoformat(),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
        "selected_peer_reason": "healthy peer with requested directory ROM and best sampled score",
    }


def _download_rom_from_peer(
    settings: Settings,
    config: dict,
    peer: dict,
    system: str,
    relative_path: str,
    expected_size=None,
    expected_fingerprint=None,
    progress_callback=None,
    cancellation_event: Optional[Event] = None,
) -> dict:
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
    address = _peer_address(peer)
    if not address:
        raise RuntimeError("selected peer has no address")
    rel = _safe_rom_relative_path(relative_path)
    url = f"{address}/v1/api/peer/roms/{quote(system, safe='')}/{quote(rel, safe='/')}"
    system_dir = (settings.roms_root / system).resolve()
    target = (system_dir / rel).resolve()
    if target == system_dir or system_dir not in target.parents:
        raise ValueError("invalid target path")
    partial_target = target.with_name(f"{target.name}.part")
    started_dt = datetime.now(timezone.utc).replace(microsecond=0)
    started = started_dt.isoformat()
    started_mono = time.monotonic()
    expected_fingerprint_clean = str(expected_fingerprint or "").strip().lower()

    def skipped_activity(existing: Path, reason: str) -> dict:
        completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
        duration_ms = int((time.monotonic() - started_mono) * 1000)
        try:
            size = int(existing.stat().st_size)
        except OSError:
            try:
                size = int(expected_size or 0) if expected_size not in (None, "") else 0
            except (TypeError, ValueError):
                size = 0
        try:
            fingerprint = RomRepository.build_fingerprint(existing) if existing.is_file() else expected_fingerprint_clean
        except Exception:
            fingerprint = expected_fingerprint_clean
        return {
            "source_drone_id": peer_id,
            "target_drone_id": settings.overmind_device_id,
            "system": system,
            "rom_name": rel,
            "relative_path": existing.relative_to(system_dir).as_posix(),
            "action": "download",
            "status": "skipped",
            "skip_reason": reason,
            "failure_reason": reason,
            "bytes_transferred": 0,
            "file_size": size or expected_size,
            "fingerprint": fingerprint,
            "rom_fingerprint": expected_fingerprint_clean or fingerprint,
            "download_started_at": started,
            "download_completed_at": completed_dt.isoformat(),
            "started_at": started,
            "completed_at": completed_dt.isoformat(),
            "duration_ms": duration_ms,
            "duration_seconds": round(duration_ms / 1000, 3),
            "selected_peer_reason": "local ROM already exists",
        }

    if target.exists():
        return skipped_activity(target, "target path already exists")
    if expected_fingerprint_clean and system_dir.exists() and system_dir.is_dir():
        for candidate in sorted(system_dir.rglob("*"), key=lambda path: path.relative_to(system_dir).as_posix().lower()):
            if not candidate.is_file():
                continue
            rel_candidate = candidate.relative_to(system_dir).as_posix()
            if RomRepository.should_ignore_rom_path(Path(rel_candidate)):
                continue
            try:
                if RomRepository.build_fingerprint(candidate).lower() == expected_fingerprint_clean:
                    return skipped_activity(candidate, "matching ROM already exists")
            except Exception:
                continue

    target.parent.mkdir(parents=True, exist_ok=True)
    cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config)
    if address.startswith("https://") and not cafile:
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    context = _drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)
    bytes_written = 0
    request = Request(url, headers={"User-Agent": "batocera-drone-rom-sync/1.0"})
    def ensure_not_cancelled() -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            if partial_target.exists():
                partial_target.unlink()
            raise DownloadCancelled("download cancelled")

    def response_total(response) -> Optional[int]:
        if expected_size not in (None, ""):
            try:
                return int(expected_size)
            except Exception:
                pass
        try:
            return int(response.headers.get("Content-Length") or 0) or None
        except Exception:
            return None

    try:
        with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, partial_target.open("wb") as handle:
            total_bytes = response_total(response)
            while True:
                ensure_not_cancelled()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                bytes_written += len(chunk)
                if progress_callback:
                    progress_callback(bytes_written, total_bytes)
    except (ssl.SSLError, URLError) as error:
        if isinstance(error, URLError) and not _is_ssl_url_error(error):
            raise
        ssl_error = getattr(error, "reason", error)
        print(f"ROM sync SSL validation failed: {_peer_ssl_diagnostic(url, cafile, ssl_error)}", file=sys.stderr, flush=True)
        if partial_target.exists():
            partial_target.unlink()
        cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config, refresh_cert=True)
        if address.startswith("https://") and not cafile:
            raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}") from error
        context = _drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)
        bytes_written = 0
        try:
            with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, partial_target.open("wb") as handle:
                total_bytes = response_total(response)
                while True:
                    ensure_not_cancelled()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    bytes_written += len(chunk)
                    if progress_callback:
                        progress_callback(bytes_written, total_bytes)
        except (ssl.SSLError, URLError) as retry_error:
            if isinstance(retry_error, URLError) and not _is_ssl_url_error(retry_error):
                raise
            retry_ssl_error = getattr(retry_error, "reason", retry_error)
            print(f"ROM sync SSL validation retry failed: {_peer_ssl_diagnostic(url, cafile, retry_ssl_error)}", file=sys.stderr, flush=True)
            if partial_target.exists():
                partial_target.unlink()
            raise ssl.SSLError(_peer_ssl_diagnostic(url, cafile, retry_ssl_error)) from retry_error
    except DownloadCancelled:
        if partial_target.exists():
            partial_target.unlink()
        raise
    except Exception:
        if partial_target.exists():
            partial_target.unlink()
        raise
    if expected_size not in (None, ""):
        try:
            if int(expected_size) != bytes_written:
                raise RuntimeError(f"size mismatch expected={expected_size} actual={bytes_written}")
        except ValueError:
            pass
    actual_fingerprint = RomRepository.build_fingerprint(partial_target)
    if expected_fingerprint_clean and actual_fingerprint.lower() != expected_fingerprint_clean:
        if partial_target.exists():
            partial_target.unlink()
        raise RuntimeError(f"fingerprint mismatch expected={expected_fingerprint_clean} actual={actual_fingerprint}")
    partial_target.replace(target)
    completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    return {
        "source_drone_id": peer_id,
        "target_drone_id": settings.overmind_device_id,
        "system": system,
        "rom_name": rel,
        "relative_path": target.relative_to(system_dir).as_posix(),
        "action": "download",
        "status": "completed",
        "bytes_transferred": bytes_written,
        "file_size": expected_size or bytes_written,
        "fingerprint": actual_fingerprint,
        "rom_fingerprint": expected_fingerprint_clean or actual_fingerprint,
        "download_started_at": started,
        "download_completed_at": completed_dt.isoformat(),
        "started_at": started,
        "completed_at": completed_dt.isoformat(),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
        "selected_peer_reason": "healthy peer with requested system and best sampled score",
    }


def _download_bios_from_peer(
    settings: Settings,
    config: dict,
    peer: dict,
    relative_path: str,
    expected_size=None,
    expected_md5=None,
    progress_callback=None,
    cancellation_event: Optional[Event] = None,
) -> dict:
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
    address = _peer_address(peer)
    if not address:
        raise RuntimeError("selected peer has no address")
    rel = _safe_rom_relative_path(relative_path)
    url = f"{address}/v1/api/peer/bios/{quote(rel, safe='/')}"
    bios_root = settings.bios_root.resolve()
    target = _collision_safe_target(bios_root, rel)
    partial_target = target.with_name(f"{target.name}.part")
    target.parent.mkdir(parents=True, exist_ok=True)
    cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config)
    if address.startswith("https://") and not cafile:
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    context = _drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)
    started_dt = datetime.now(timezone.utc).replace(microsecond=0)
    started = started_dt.isoformat()
    started_mono = time.monotonic()
    bytes_written = 0
    request = Request(url, headers={"User-Agent": "batocera-drone-bios-sync/1.0"})

    def ensure_not_cancelled() -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            if partial_target.exists():
                partial_target.unlink()
            raise DownloadCancelled("download cancelled")

    def response_total(response) -> Optional[int]:
        if expected_size not in (None, ""):
            try:
                return int(expected_size)
            except Exception:
                pass
        try:
            return int(response.headers.get("Content-Length") or 0) or None
        except Exception:
            return None

    try:
        with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, partial_target.open("wb") as handle:
            total_bytes = response_total(response)
            while True:
                ensure_not_cancelled()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                bytes_written += len(chunk)
                if progress_callback:
                    progress_callback(bytes_written, total_bytes)
    except (ssl.SSLError, URLError) as error:
        if isinstance(error, URLError) and not _is_ssl_url_error(error):
            raise
        ssl_error = getattr(error, "reason", error)
        print(f"BIOS sync SSL validation failed: {_peer_ssl_diagnostic(url, cafile, ssl_error)}", file=sys.stderr, flush=True)
        if partial_target.exists():
            partial_target.unlink()
        cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config, refresh_cert=True)
        if address.startswith("https://") and not cafile:
            raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}") from error
        context = _drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)
        bytes_written = 0
        try:
            with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, partial_target.open("wb") as handle:
                total_bytes = response_total(response)
                while True:
                    ensure_not_cancelled()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    bytes_written += len(chunk)
                    if progress_callback:
                        progress_callback(bytes_written, total_bytes)
        except (ssl.SSLError, URLError) as retry_error:
            if isinstance(retry_error, URLError) and not _is_ssl_url_error(retry_error):
                raise
            retry_ssl_error = getattr(retry_error, "reason", retry_error)
            print(f"BIOS sync SSL validation retry failed: {_peer_ssl_diagnostic(url, cafile, retry_ssl_error)}", file=sys.stderr, flush=True)
            if partial_target.exists():
                partial_target.unlink()
            raise ssl.SSLError(_peer_ssl_diagnostic(url, cafile, retry_ssl_error)) from retry_error
    except DownloadCancelled:
        if partial_target.exists():
            partial_target.unlink()
        raise
    except Exception:
        if partial_target.exists():
            partial_target.unlink()
        raise
    if expected_size not in (None, ""):
        try:
            if int(expected_size) != bytes_written:
                raise RuntimeError(f"size mismatch expected={expected_size} actual={bytes_written}")
        except ValueError:
            pass
    # BIOS verifies against a full-file MD5 (exact emulator identity), not the sampled fingerprint.
    actual_md5 = RomRepository.build_md5(partial_target)
    expected_md5_clean = str(expected_md5 or "").strip().lower()
    if expected_md5_clean and actual_md5.lower() != expected_md5_clean:
        if partial_target.exists():
            partial_target.unlink()
        raise RuntimeError(f"md5 mismatch expected={expected_md5_clean} actual={actual_md5}")
    partial_target.replace(target)
    completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    return {
        "asset_type": "bios",
        "file_type": "BIOS",
        "source_drone_id": peer_id,
        "target_drone_id": settings.overmind_device_id,
        "system": "bios",
        "bios_name": rel,
        "rom_name": rel,
        "relative_path": target.relative_to(bios_root).as_posix(),
        "action": "download",
        "status": "completed",
        "bytes_transferred": bytes_written,
        "file_size": expected_size or bytes_written,
        "md5": actual_md5,
        "bios_md5": expected_md5_clean or actual_md5,
        "download_started_at": started,
        "download_completed_at": completed_dt.isoformat(),
        "started_at": started,
        "completed_at": completed_dt.isoformat(),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
        "selected_peer_reason": "healthy peer from Overmind BIOS source list with best sampled score",
    }


def _download_save_from_peer(
    settings: Settings,
    config: dict,
    peer: dict,
    system: str,
    relative_path: str,
    expected_size=None,
    expected_fingerprint=None,
    cancellation_event: Optional[Event] = None,
) -> dict:
    """Fetch a single game-save file from a peer and write it under saves_root.

    Unlike ROMs/BIOS (which never overwrite an existing file), saves resolve
    newest-modified-wins, so the fetched copy replaces the local one at the exact
    path. The sampled fingerprint is verified when the caller supplies the expected
    value. ``relative_path`` is the path WITHIN the system directory.
    """
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
    address = _peer_address(peer)
    if not address:
        raise RuntimeError("selected peer has no address")
    system_clean = _safe_rom_relative_path(system).strip("/")
    rel = _safe_rom_relative_path(relative_path)
    url = f"{address}/v1/api/peer/saves/{quote(system_clean, safe='/')}/{quote(rel, safe='/')}"
    saves_root = Path(settings.saves_root).resolve()
    target = (saves_root / system_clean / rel).resolve()
    if saves_root not in target.parents:
        raise ValueError("invalid save target path")
    partial_target = target.with_name(f"{target.name}.part")
    target.parent.mkdir(parents=True, exist_ok=True)
    cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config)
    if address.startswith("https://") and not cafile:
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    context = _drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)
    started_mono = time.monotonic()
    bytes_written = 0
    request = Request(url, headers={"User-Agent": "batocera-drone-saves-sync/1.0"})

    def ensure_not_cancelled() -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            if partial_target.exists():
                partial_target.unlink()
            raise DownloadCancelled("download cancelled")

    try:
        with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, partial_target.open("wb") as handle:
            while True:
                ensure_not_cancelled()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                bytes_written += len(chunk)
    except DownloadCancelled:
        raise
    except Exception:
        if partial_target.exists():
            partial_target.unlink()
        raise
    expected_fp = str(expected_fingerprint or "").strip().lower()
    if expected_fp:
        actual_fp = RomRepository.build_fingerprint(partial_target).lower()
        if actual_fp != expected_fp:
            if partial_target.exists():
                partial_target.unlink()
            raise RuntimeError(f"fingerprint mismatch expected={expected_fp} actual={actual_fp}")
    partial_target.replace(target)  # newest-wins: overwrite any existing local save
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    return {
        "asset_type": "saves",
        "file_type": "Save",
        "source_drone_id": peer_id,
        "target_drone_id": settings.overmind_device_id,
        "system": system_clean,
        "save_name": rel,
        "relative_path": f"{system_clean}/{rel}",
        "action": "download",
        "status": "completed",
        "bytes_transferred": bytes_written,
        "file_size": expected_size or bytes_written,
        "fingerprint": expected_fp or RomRepository.build_fingerprint(target),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
    }


def _download_artwork_from_peer(
    settings: Settings,
    repository: "RomRepository",
    config: dict,
    peer: dict,
    system: str,
    rom_path: str,
    artwork_type: str,
    progress_callback=None,
    cancellation_event: Optional[Event] = None,
    overwrite: bool = False,
    local_rom_path: Optional[str] = None,
) -> dict:
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
    address = _peer_address(peer)
    if not address:
        raise RuntimeError("selected peer has no address")
    system = valid_segment(system)
    field = str(artwork_type or "").strip()
    if field not in ARTWORK_FIELDS:
        raise ValueError("invalid artwork type")
    # The peer resolves its artwork from its own gamelist using the *peer's* ROM
    # path; the file is written locally and linked in the local gamelist against
    # the *local* ROM path (which differs only when the same ROM is present under
    # a different filename).
    rom_rel = _safe_rom_relative_path(rom_path)
    local_rom_rel = _safe_rom_relative_path(local_rom_path) if local_rom_path else rom_rel
    url = f"{address}/v1/api/peer/artwork/{quote(system, safe='')}/{quote(field, safe='')}/{quote(rom_rel, safe='/')}"
    system_dir = (settings.roms_root / system).resolve()
    cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config)
    if address.startswith("https://") and not cafile:
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    context = _drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)
    started_dt = datetime.now(timezone.utc).replace(microsecond=0)
    started = started_dt.isoformat()
    started_mono = time.monotonic()
    bytes_written = 0
    request = Request(url, headers={"User-Agent": "batocera-drone-artwork-sync/1.0"})

    def ensure_not_cancelled() -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            raise DownloadCancelled("download cancelled")

    partial_target = None
    target = None
    artwork_relative_path = ""
    try:
        with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response:
            header_name = response.headers.get("X-Asset-Relative-Path") or ""
            ext = Path(header_name).suffix or Path(urlparse(response.geturl()).path).suffix or ".bin"
            if overwrite:
                # Deterministic name keyed to the *local* ROM so a re-copy overwrites
                # the same file instead of accumulating "-1" duplicates. Keep the
                # peer's media subdir (videos/ for video, manuals/ for manual, etc.)
                # so the file lands where EmulationStation expects it; default to
                # images/ when the peer did not provide one.
                media_subdir = Path(header_name).parent.as_posix() if header_name else ""
                if not media_subdir or media_subdir in (".", "/"):
                    media_subdir = "images"
                artwork_relative_path = _safe_rom_relative_path(f"{media_subdir}/{Path(local_rom_rel).stem}-{field}{ext}")
                target = (system_dir / artwork_relative_path).resolve()
                if target != system_dir and system_dir not in target.parents:
                    raise ValueError("unsafe artwork target path")
            else:
                artwork_relative_path = _safe_rom_relative_path(header_name or f"images/{Path(local_rom_rel).stem}-{field}{ext}")
                target = _collision_safe_target(system_dir, artwork_relative_path)
            partial_target = target.with_name(f"{target.name}.part")

            def _open_partial():
                target.parent.mkdir(parents=True, exist_ok=True)
                return partial_target.open("wb")

            try:
                handle = _open_partial()
            except PermissionError:
                # The media dir isn't yet writable by the unprivileged Drone (a
                # freshly-scraped, root-owned images/ or videos/). Ask the privileged
                # worker to fix perms, then retry once before giving up.
                _ensure_rom_write_access(settings, system)
                handle = _open_partial()
            try:
                total_bytes = int(response.headers.get("Content-Length") or 0) or None
            except Exception:
                total_bytes = None
            with handle:
                while True:
                    ensure_not_cancelled()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    bytes_written += len(chunk)
                    if progress_callback:
                        progress_callback(bytes_written, total_bytes)
    except DownloadCancelled:
        if partial_target and partial_target.exists():
            partial_target.unlink()
        raise
    except Exception:
        if partial_target and partial_target.exists():
            partial_target.unlink()
        raise
    if not partial_target or not target:
        raise RuntimeError("artwork download failed")
    actual_fingerprint = RomRepository.build_fingerprint(partial_target)
    partial_target.replace(target)
    gamelist_update = None
    gamelist_update_status = "succeeded"
    artwork_rel = target.relative_to(system_dir).as_posix()
    try:
        try:
            gamelist_update = repository.update_gamelist_artwork_reference(system, local_rom_rel, field, artwork_rel)
        except PermissionError:
            # gamelist.xml is root-owned / not yet writable by the Drone; fix perms
            # via the privileged worker and retry once before reporting failure.
            _ensure_rom_write_access(settings, system)
            gamelist_update = repository.update_gamelist_artwork_reference(system, local_rom_rel, field, artwork_rel)
    except Exception as error:
        gamelist_update_status = "failed"
        gamelist_update = {"error": str(error), "path": str(system_dir / "gamelist.xml")}
        print(
            f"Artwork download completed but gamelist update failed: system={system} rom={local_rom_rel} artwork_type={field} error={error}",
            file=sys.stderr,
            flush=True,
        )
    completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    return {
        "asset_type": "artwork",
        "file_type": "ARTWORK",
        "source_drone_id": peer_id,
        "target_drone_id": settings.overmind_device_id,
        "system": system,
        "rom_name": local_rom_rel,
        "rom_path": local_rom_rel,
        "artwork_type": field,
        "relative_path": target.relative_to(system_dir).as_posix(),
        "action": "download",
        "status": "completed",
        "bytes_transferred": bytes_written,
        "file_size": bytes_written,
        "fingerprint": actual_fingerprint,
        "download_started_at": started,
        "download_completed_at": completed_dt.isoformat(),
        "started_at": started,
        "completed_at": completed_dt.isoformat(),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
        "selected_peer_reason": "healthy peer from Overmind artwork source list with best sampled score",
        "gamelist_update_status": gamelist_update_status,
        "gamelist_update": gamelist_update,
    }


def _summarize_overmind_result(result: Optional[dict]) -> str:
    if not isinstance(result, dict):
        return ""
    if result.get("type") in {"rom_metadata", "asset_metadata"}:
        return (
            f"{len(result.get('systems') or [])} systems, {len(result.get('roms') or [])} ROMs, "
            f"{len(result.get('bios') or [])} BIOS files, {len(result.get('artwork') or [])} artwork rows, "
            f"{len(result.get('gamelists') or [])} gamelists"
        )
    if result.get("type") == "game_logs":
        return f"{len(result.get('sessions') or [])} parsed sessions, {len(result.get('logs') or [])} logs"
    if result.get("type") == "emulator_configs":
        return f"{len(result.get('configs') or [])} config files"
    if result.get("type") == "log_sources":
        return f"{len(result.get('logs') or [])} log sources"
    return "data returned"


def _execute_overmind_action(
    settings: Settings,
    repository: "RomRepository",
    action: dict,
    config: Optional[dict] = None,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
) -> Tuple[str, str, Optional[dict]]:
    action_name = str(action.get("action") or "").strip().lower()

    if action_name == "collect_rom_metadata":
        result = _collect_rom_metadata(settings, repository)
        return "completed", f"Collected {_summarize_overmind_result(result)}.", result

    if action_name == "rebuild_asset_metadata":
        _clear_sqlite_asset_metadata_cache(settings)
        cache = _empty_rom_metadata_cache()
        cache["dirty"] = True
        cache["full_refresh_pending"] = True
        cache["rebuild_requested_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        _persist_rom_metadata_cache(settings, cache)
        _ROM_METADATA_WAKE.set()
        return "completed", "Queued full asset metadata rebuild; local asset cache was cleared and the metadata poller will upload a fresh snapshot.", {
            "type": "asset_metadata_rebuild",
            "status": "queued",
            "reason": "local_asset_cache_cleared",
            "poller_wake_requested": True,
        }

    if action_name == "purge_asset_cache":
        result = _purge_asset_cache_keep_fingerprint(settings)
        _ROM_METADATA_WAKE.set()
        return "completed", "Queued asset cache purge; cached fingerprint values were kept, and the metadata poller will re-scan and upload a fresh full inventory.", {
            "type": "asset_cache_purge",
            "status": result.get("status", "queued"),
            "reason": "full_refresh_kept_fingerprint",
            "poller_wake_requested": True,
        }

    if action_name == "collect_game_logs":
        sessions = _load_gameplay_history(settings)
        result = {"type": "game_logs", "sessions": sessions}
        return "completed", f"Collected {_summarize_overmind_result(result)}.", result

    if action_name == "collect_emulator_configs":
        result = _collect_emulator_configs(settings, include_unchanged=True)
        result.pop("_fingerprints", None)
        return "completed", f"Collected {_summarize_overmind_result(result)}.", result

    if action_name == "collect_log_sources":
        return "skipped", "Raw log streaming is only active while the Overmind logs UI is open.", {
            "type": "log_sources",
            "logs": [],
            "streaming_required": True,
        }

    if action_name == "sync_bios":
        config = _load_overmind_config_for_settings(settings)
        manager = _get_download_manager()
        if manager is None:
            return "failed", "Download manager is not available.", None
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        rel = str(payload.get("file_path") or payload.get("relative_path") or payload.get("bios_name") or payload.get("name") or "").strip()
        expected_md5 = payload.get("bios_md5") or payload.get("fingerprint")
        source_device_ids = {
            str(device.get("device_id") or device.get("drone_id") or "")
            for device in payload.get("devices", [])
            if isinstance(device, dict)
        }
        if not rel:
            return "failed", "BIOS path is required.", None
        sync_id = str(uuid.uuid4())
        started_wall = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        started_mono = time.monotonic()
        if expected_md5 and _bios_md5_exists(repository, expected_md5):
            result = {
                "type": "bios_sync",
                "activity": [{
                    "asset_type": "bios",
                    "sync_id": sync_id,
                    "target_drone_id": settings.overmind_device_id,
                    "system": "bios",
                    "bios_name": rel,
                    "relative_path": rel,
                    "action": "download",
                    "status": "skipped",
                    "failure_reason": "BIOS fingerprint already exists locally",
                    "bios_md5": expected_md5,
                    "download_started_at": started_wall,
                    "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "duration_ms": int((time.monotonic() - started_mono) * 1000),
                }],
            }
            return "completed", "BIOS sync skipped because matching file already exists.", result
        peer = _best_peer_for_bios(settings, config, rel, source_device_ids=source_device_ids)
        if not peer:
            result = {
                "type": "bios_sync",
                "activity": [{
                    "asset_type": "bios",
                    "sync_id": sync_id,
                    "target_drone_id": settings.overmind_device_id,
                    "system": "bios",
                    "bios_name": rel,
                    "relative_path": rel,
                    "action": "download",
                    "status": "failed",
                    "failure_reason": "No healthy source peer with requested BIOS found",
                    "bios_md5": expected_md5,
                    "download_started_at": started_wall,
                    "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "duration_ms": int((time.monotonic() - started_mono) * 1000),
                }],
            }
            return "failed", "BIOS sync failed: no healthy source peer.", result
        try:
            activity = manager.enqueue_bios(
                config,
                peer,
                rel,
                expected_size=payload.get("file_size"),
                expected_md5=expected_md5,
                source_action_id=str(action.get("id") or ""),
            )
            activity["sync_id"] = activity.get("job_id") or sync_id
            activity["bios_md5"] = activity.get("bios_md5") or expected_md5
            return "completed", "BIOS sync queued 1 item.", {"type": "bios_sync", "activity": [activity]}
        except Exception as error:
            result = {
                "type": "bios_sync",
                "activity": [{
                    "asset_type": "bios",
                    "sync_id": sync_id,
                    "source_drone_id": str(peer.get("drone_id") or peer.get("device_id") or ""),
                    "target_drone_id": settings.overmind_device_id,
                    "system": "bios",
                    "bios_name": rel,
                    "relative_path": rel,
                    "action": "download",
                    "status": "failed",
                    "failure_reason": str(error),
                    "bios_md5": expected_md5,
                    "download_started_at": started_wall,
                    "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "duration_ms": int((time.monotonic() - started_mono) * 1000),
                }],
            }
            return "failed", "BIOS sync failed for 1 item.", result

    if action_name == "sync_artwork":
        config = _load_overmind_config_for_settings(settings)
        manager = _get_download_manager()
        if manager is None:
            return "failed", "Download manager is not available.", None
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        system = str(payload.get("system_name") or payload.get("system") or "").strip()
        rom_path = str(payload.get("rom_path") or payload.get("file_path") or payload.get("rom_name") or "").strip()
        artwork_type = str(payload.get("artwork_type") or "").strip()
        source_device_ids = {
            str(device.get("device_id") or device.get("drone_id") or "")
            for device in payload.get("devices", [])
            if isinstance(device, dict)
        }
        if not system or not rom_path or artwork_type not in ARTWORK_FIELDS:
            return "failed", "system, rom_path, and artwork_type are required.", None
        sync_id = str(uuid.uuid4())
        started_wall = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        peer = _best_peer_for_bios(settings, config, rom_path, source_device_ids=source_device_ids)
        if not peer:
            result = {"type": "artwork_sync", "activity": [{
                "asset_type": "artwork",
                "sync_id": sync_id,
                "target_drone_id": settings.overmind_device_id,
                "system": system,
                "rom_name": rom_path,
                "rom_path": rom_path,
                "artwork_type": artwork_type,
                "relative_path": rom_path,
                "action": "download",
                "status": "failed",
                "failure_reason": "No healthy source peer with requested artwork found",
                "download_started_at": started_wall,
                "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }]}
            return "failed", "Artwork sync failed: no healthy source peer.", result
        activity = manager.enqueue_artwork(
            config,
            peer,
            system,
            rom_path,
            artwork_type,
            source_action_id=str(action.get("id") or ""),
        )
        activity["sync_id"] = activity.get("job_id") or sync_id
        return "completed", "Artwork sync queued 1 item.", {"type": "artwork_sync", "activity": [activity]}

    if action_name in {"sync_rom", "sync_system"}:
        config = _load_overmind_config_for_settings(settings)
        manager = _get_download_manager()
        if manager is None:
            return "failed", "Download manager is not available.", None
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        requested = []
        if action_name == "sync_rom":
            requested = [payload]
        else:
            requested = payload.get("roms") if isinstance(payload.get("roms"), list) else []
        activities = []
        failures = 0
        for item in requested:
            system = str(item.get("system_name") or item.get("system") or payload.get("system_name") or "").strip()
            rel = str(item.get("file_path") or item.get("rom_name") or "").strip()
            expected_fingerprint = item.get("rom_fingerprint") or item.get("fingerprint")
            entry_type = str(item.get("entry_type") or "file").strip().lower()
            sync_id = str(item.get("sync_id") or payload.get("sync_id") or uuid.uuid4())
            source_device_ids = {
                str(device.get("device_id") or device.get("drone_id") or "")
                for device in item.get("devices", [])
                if isinstance(device, dict)
            }
            if not system or not rel:
                continue
            started_wall = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            started_mono = time.monotonic()
            if expected_fingerprint and _cached_rom_fingerprint_exists(settings, expected_fingerprint):
                activity = {
                    "sync_id": sync_id,
                    "target_drone_id": settings.overmind_device_id,
                    "system": system,
                    "rom_name": rel,
                    "relative_path": rel,
                    "entry_type": entry_type,
                    "action": "download",
                    "status": "skipped",
                    "failure_reason": "ROM fingerprint already exists locally",
                    "rom_fingerprint": expected_fingerprint,
                    "download_started_at": started_wall,
                    "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "duration_ms": int((time.monotonic() - started_mono) * 1000),
                }
                _post_rom_sync_activity(settings, config, activity)
                activities.append(activity)
                continue
            peer = _best_peer_for_rom(settings, repository, config, system, rel, source_device_ids=source_device_ids)
            if not peer:
                failures += 1
                activity = {
                    "sync_id": sync_id,
                    "target_drone_id": settings.overmind_device_id,
                    "system": system,
                    "rom_name": rel,
                    "relative_path": rel,
                    "entry_type": entry_type,
                    "action": "download",
                    "status": "failed",
                    "failure_reason": "No healthy source peer with requested ROM found",
                    "rom_fingerprint": expected_fingerprint,
                    "download_started_at": started_wall,
                    "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "duration_ms": int((time.monotonic() - started_mono) * 1000),
                }
                _post_rom_sync_activity(settings, config, activity)
                activities.append(activity)
                continue
            try:
                activity = manager.enqueue_rom(
                    config,
                    peer,
                    system,
                    rel,
                    expected_size=item.get("file_size"),
                    expected_fingerprint=expected_fingerprint,
                    source_action_id=str(action.get("id") or ""),
                    entry_type=entry_type,
                    sync_id=sync_id,
                )
                activity["sync_id"] = sync_id
                activity["rom_fingerprint"] = activity.get("rom_fingerprint") or expected_fingerprint
                activity["entry_type"] = activity.get("entry_type") or entry_type
                activities.append(activity)
            except Exception as error:
                failures += 1
                activity = {
                    "sync_id": sync_id,
                    "source_drone_id": str(peer.get("drone_id") or peer.get("device_id") or ""),
                    "target_drone_id": settings.overmind_device_id,
                    "system": system,
                    "rom_name": rel,
                    "relative_path": rel,
                    "entry_type": entry_type,
                    "action": "download",
                    "status": "failed",
                    "failure_reason": str(error),
                    "rom_fingerprint": expected_fingerprint,
                    "download_started_at": started_wall,
                    "download_completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "duration_ms": int((time.monotonic() - started_mono) * 1000),
                }
                _post_rom_sync_activity(settings, config, activity)
                activities.append(activity)
        result = {"type": "rom_sync", "activity": activities}
        if failures and failures == len(activities):
            return "failed", f"ROM sync failed for {failures} item(s).", result
        return "completed", f"ROM sync queued {len(activities)} item(s) with {failures} failure(s).", result

    if action_name == "cancel_download":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        job_id = str(payload.get("job_id") or payload.get("download_id") or "").strip()
        if not job_id:
            return "failed", "job_id is required.", None
        manager = _get_download_manager()
        if manager is None:
            return "failed", "Download manager is not available.", None
        result = manager.cancel(job_id, "cancelled from Overmind")
        status_value = "completed" if result.get("status") != "not_found" else "failed"
        return status_value, f"Cancel request for download {job_id}: {result.get('status')}.", {"type": "download_cancel", **result}

    if action_name == "shutdown":
        return "failed", "Unsupported action: shutdown is disabled by Overmind safety policy.", None

    if action_name == "restart":
        if settings.use_fake_data:
            return "completed", "Simulated restart action because USE_FAKE_DATA is enabled.", None
        return "completed", "Host reboot requested; Drone service supervisor will reboot after action completion is reported.", {
            "type": "system_restart",
            "reboot_requested": True,
            "exit_code": DRONE_REMOTE_REBOOT_EXIT_CODE,
        }

    if action_name == "refresh_emulator_list":
        if settings.use_fake_data:
            return "completed", "Simulated emulator list refresh because USE_FAKE_DATA is enabled.", {
                "type": "emulator_list_refresh",
                "emulationstation_restarted": False,
                "simulated": True,
            }
        if not _restart_emulationstation():
            return "failed", "Unable to refresh emulator list: EmulationStation restart command was not found.", {
                "type": "emulator_list_refresh",
                "emulationstation_restarted": False,
            }
        return "completed", "Emulator list refresh issued through an EmulationStation restart.", {
            "type": "emulator_list_refresh",
            "emulationstation_restarted": True,
        }

    if action_name == "set_screen_mode":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        mode = str(payload.get("mode") or "").strip().lower()
        if mode not in {"full", "kiosk", "kid"}:
            return "failed", "A screen mode of full, kiosk, or kid is required.", None
        try:
            settings_path, restarted = _apply_screen_mode(settings, mode)
        except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError) as error:
            return "failed", f"Unable to update screen mode settings: {error}", None
        suffix = " EmulationStation restart issued." if restarted else " Applies on the next EmulationStation restart."
        return "completed", f"Screen mode set to {mode}.{suffix}", {
            "type": "screen_mode",
            "mode": mode,
            "settings_file": str(settings_path),
            "emulationstation_restarted": restarted,
        }

    if action_name == "set_volume":
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        raw_level = payload.get("level")
        if raw_level is None:
            raw_level = payload.get("volume")
        try:
            level = int(raw_level)
        except (TypeError, ValueError):
            return "failed", "A numeric volume level (0-100) is required.", None
        try:
            applied = _apply_audio_volume(settings, level)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            return "failed", f"Unable to set volume: {error}", None
        label = "muted" if applied <= 0 else f"set to {applied}%"
        return "completed", f"Volume {label}.", {
            "type": "audio_volume",
            "level": applied,
            "muted": applied <= 0,
        }

    if action_name == "update":
        if settings.use_fake_data:
            return "completed", "Simulated update action because USE_FAKE_DATA is enabled.", None
        updater = shutil.which("batocera-upgrade")
        if not updater:
            return "failed", "batocera-upgrade command was not found", None
        subprocess.Popen([updater], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "completed", "Batocera update command issued.", None

    return "failed", f"Unsupported action: {action_name}", None


def _start_overmind_action_poller(settings: Settings, repository: "RomRepository") -> None:
    poll_seconds = max(5, int(settings.overmind_poll_seconds or OVERMIND_HEARTBEAT_SECONDS))
    speed_sample_seconds = OVERMIND_SPEED_SAMPLE_SECONDS
    config_report_seconds = max(0, int(OVERMIND_CONFIG_REPORT_SECONDS))
    system_info_refresh_seconds = max(300, int(os.environ.get("DRONE_SYSTEM_INFO_REFRESH_SECONDS", "3600")))
    last_speed_sample_at: Optional[float] = None
    last_config_report_at = -float(config_report_seconds or 0)
    last_system_info_at = -float(system_info_refresh_seconds)
    system_info_payload: dict = {}
    fs_snapshot = _filesystem_snapshot(settings)

    def loop() -> None:
        nonlocal last_speed_sample_at, last_config_report_at, last_system_info_at, system_info_payload, fs_snapshot
        while True:
            if not _local_network.is_overmind_mode(settings):
                time.sleep(poll_seconds)
                continue
            try:
                config = _load_overmind_config_for_settings(settings)
                base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
                token = str(config.get("overmind_token") or "").strip()
                integration_enabled = bool(config.get("integration_enabled"))
                if not base_url:
                    time.sleep(poll_seconds)
                    continue
                if not token:
                    auth_token = str(config.get("overmind_auth_token") or "").strip()
                    if auth_token and integration_enabled:
                        token = _register_or_claim_overmind_token(settings, repository, config, base_url) or ""
                        if not token:
                            # Pending onboarding still emits a lightweight heartbeat. Overmind
                            # records it as an installed, recoverable Drone until approval.
                            token = auth_token
                    if not token:
                        time.sleep(poll_seconds)
                        continue

                device_id = quote(settings.overmind_device_id, safe="")
                now = time.monotonic()
                if not system_info_payload or now - last_system_info_at >= system_info_refresh_seconds:
                    system_info_payload = _collect_system_info_payload(settings)
                    last_system_info_at = now
                else:
                    system_info_payload["performance"] = _collect_performance_metrics(settings.userdata_root)
                    system_info_payload["updated_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                    system_info_payload["screen_mode"] = _get_screen_mode(settings)
                    system_info_payload["audio_volume"] = _get_audio_volume(settings)
                network_payload = _drone_network_payload(settings)
                heartbeat_payload = {
                    "device_id": settings.overmind_device_id,
                    "device_name": str(config.get("drone_name") or "").strip() or socket.gethostname(),
                    "network": network_payload,
                    "api_port": _drone_advertised_api_port(settings),
                    "scheme": _drone_scheme(settings),
                    "reachable_url": _drone_reachable_url(settings, network_payload),
                    "certificate": DroneCertificateManager(settings).metadata(),
                    "system_info": system_info_payload,
                    "downloads": _get_download_manager().snapshot() if _get_download_manager() else {},
                }
                rom_fingerprint = _rom_inventory_fingerprint_from_cache_state(settings)
                if rom_fingerprint:
                    heartbeat_payload["rom_inventory_fingerprint"] = rom_fingerprint
                    heartbeat_payload["rom_inventory_fingerprint_algorithm"] = ROM_INVENTORY_FINGERPRINT_ALGORITHM
                local_romset_thumbprint, local_bios_thumbprint = _local_asset_thumbprints(settings)
                if local_romset_thumbprint:
                    heartbeat_payload["romset_files_thumbprint"] = local_romset_thumbprint
                if local_bios_thumbprint:
                    heartbeat_payload["bios_files_thumbprint"] = local_bios_thumbprint
                local_saves_thumbprint = _local_saves_thumbprint(settings)
                if local_saves_thumbprint:
                    heartbeat_payload["saves_files_thumbprint"] = local_saves_thumbprint
                heartbeat_url = f"{base_url}/api/devices/{device_id}/heartbeat"
                heartbeat_started = time.monotonic()
                _overmind_log(
                    f"Heartbeat send started: endpoint={heartbeat_url} device_id={settings.overmind_device_id}"
                )
                try:
                    try:
                        status_code, response = _overmind_post_json_with_status(
                            heartbeat_url,
                            heartbeat_payload,
                            token=token,
                            settings=settings,
                            timeout_seconds=OVERMIND_HEARTBEAT_TIMEOUT_SECONDS,
                        )
                    except HTTPError as error:
                        if error.code == 401 and integration_enabled:
                            replacement_token = _reclaim_overmind_token_after_unauthorized(settings, repository, config, base_url, error)
                            if replacement_token:
                                token = replacement_token
                                status_code, response = _overmind_post_json_with_status(
                                    heartbeat_url,
                                    heartbeat_payload,
                                    token=token,
                                    settings=settings,
                                    timeout_seconds=OVERMIND_HEARTBEAT_TIMEOUT_SECONDS,
                                )
                            else:
                                raise
                        else:
                            raise
                except Exception as error:
                    status_part = f" status={error.code}" if isinstance(error, HTTPError) else ""
                    _overmind_log(
                        f"Heartbeat send failed: endpoint={heartbeat_url}{status_part} error={_format_overmind_error(error)} duration_ms={int((time.monotonic() - heartbeat_started) * 1000)}"
                    )
                    raise
                _overmind_log(
                    f"Heartbeat send succeeded: endpoint={heartbeat_url} status={status_code} duration_ms={int((time.monotonic() - heartbeat_started) * 1000)}"
                )
                if not integration_enabled:
                    time.sleep(poll_seconds)
                    continue
                swarm = response.get("swarm") if isinstance(response.get("swarm"), list) else []
                _save_state_payload(_state_database_path(settings.userdata_root), "overmind_swarm.json", swarm)
                _overmind_swarm_path_for_settings(settings).unlink(missing_ok=True)

                # Overmind echoes the asset thumbprints it last stored for this Drone.
                # If they differ from what the Drone currently holds, Overmind's copy has
                # drifted (or is missing) — wake the metadata poller to push a fresh
                # inventory and resync. The Drone, not Overmind, decides to resend.
                _maybe_request_asset_push_from_heartbeat(settings, response)
                _maybe_request_saves_push_from_heartbeat(settings, response)

                # Telemetry steps below are best-effort and independent: a failure in
                # one (e.g. a flaky speed test) must not abort the heartbeat iteration
                # before later steps such as the game-log upload get a chance to run.
                if speed_sample_seconds > 0 and (
                    last_speed_sample_at is None or now - last_speed_sample_at >= speed_sample_seconds
                ):
                    speed_url = f"{base_url}/api/devices/{device_id}/speed"
                    try:
                        speed_sample = _sample_speed()
                        _overmind_post_json(speed_url, speed_sample, token=token, settings=settings)
                        _overmind_post_json(
                            f"{base_url}/api/devices/{device_id}/events",
                            {
                                "drone_id": settings.overmind_device_id,
                                "event_type": OVERMIND_EVENT_TYPES["speed"],
                                "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                                "metadata": {"speed_result": speed_sample},
                            },
                            token=token,
                            settings=settings,
                        )
                        _overmind_log(f"Speed sample sent to Overmind for {settings.overmind_device_id}")
                        last_speed_sample_at = now
                    except Exception as error:
                        _overmind_log(f"Speed sample failed for {settings.overmind_device_id}; continuing: {_format_overmind_error(error)}")

                if not _ROM_METADATA_ACTIVE.is_set():
                    try:
                        next_fs_snapshot = _filesystem_snapshot(settings)
                        for event in _filesystem_events(settings, fs_snapshot, next_fs_snapshot):
                            print(f"Filesystem event: {event.get('metadata', {}).get('action')} {event.get('path')}", file=sys.stdout, flush=True)
                            _overmind_post_json(f"{base_url}/api/devices/{device_id}/events", event, token=token, settings=settings)
                        fs_snapshot = next_fs_snapshot
                    except Exception as error:
                        print(f"Filesystem event upload failed; continuing: {_format_overmind_error(error)}", file=sys.stderr, flush=True)

                # The procfs game monitor writes durable start/stop events to the spool.
                # Best-effort so transient failures leave those events queued for retry.
                try:
                    event_sessions, spool_files = _collect_game_event_sessions(settings, repository)
                    if event_sessions:
                        game_logs = {"type": "game_logs", "sessions": event_sessions}
                        _overmind_post_json(f"{base_url}/api/devices/{device_id}/game-logs", game_logs, token=token, settings=settings)
                        _delete_game_event_spool(spool_files)
                        _overmind_log(
                            f"Sent {len(game_logs.get('sessions') or [])} game log session(s) to Overmind"
                        )
                    else:
                        _delete_game_event_spool(spool_files)
                except Exception as error:
                    # Leave spool files in place so the next heartbeat retries them.
                    _overmind_log(f"Game log upload failed; will retry next heartbeat: {_format_overmind_error(error)}")

                persistent_logs = _collect_log_sources(settings, sources=PERSISTENT_OVERMIND_LOG_SOURCES)
                persistent_log_cursors = persistent_logs.pop("_cursors", {})
                if persistent_logs.get("logs"):
                    try:
                        _overmind_post_json(f"{base_url}/api/devices/{device_id}/log-sources", persistent_logs, token=token, settings=settings)
                    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
                        _overmind_log(
                            f"Persistent log upload failed; heartbeat/action poll will continue: {_format_overmind_error(error)}"
                        )
                    else:
                        _commit_log_cursors(settings, persistent_log_cursors)
                        _overmind_log(
                            f"Sent {len(persistent_logs.get('logs') or [])} persistent log source(s) to Overmind"
                        )

                if bool(response.get("log_stream_requested")):
                    log_sources = _collect_log_sources(settings)
                    log_cursors = log_sources.pop("_cursors", {})
                    if log_sources.get("logs"):
                        try:
                            _overmind_post_json(f"{base_url}/api/devices/{device_id}/log-sources", log_sources, token=token, settings=settings)
                        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
                            _overmind_log(
                                f"Log stream upload failed; heartbeat/action poll will continue: {_format_overmind_error(error)}"
                            )
                        else:
                            _commit_log_cursors(settings, log_cursors)
                            _overmind_log(
                                f"Streamed {len(log_sources.get('logs') or [])} log source(s) to Overmind"
                            )

                if (
                    config_report_seconds > 0
                    and now - last_config_report_at >= config_report_seconds
                    and not _ROM_METADATA_ACTIVE.is_set()
                ):
                    emulator_configs = _collect_emulator_configs(settings)
                    emulator_config_fingerprints = emulator_configs.pop("_fingerprints", {})
                    if emulator_configs.get("configs"):
                        _overmind_post_json(f"{base_url}/api/devices/{device_id}/emulator-configs", emulator_configs, token=token, settings=settings)
                        _commit_emulator_config_fingerprints(settings, emulator_config_fingerprints)
                        _overmind_log(
                            f"Sent {len(emulator_configs.get('configs') or [])} changed emulator config(s) to Overmind"
                        )
                    last_config_report_at = now

                actions = response.get("actions") if isinstance(response.get("actions"), list) else None
                if actions is None:
                    legacy_action = response.get("action")
                    actions = [legacy_action] if isinstance(legacy_action, dict) else []
                actions = [action for action in actions if isinstance(action, dict)]
                if not actions:
                    time.sleep(poll_seconds)
                    continue

                claimed_names = ", ".join(str(action.get("action") or "?") for action in actions)
                _overmind_log(
                    f"Claimed {len(actions)} Overmind action(s) for {settings.overmind_device_id}: {claimed_names}",
                    also_stdout=True,
                )
                for action in actions:
                    action_name_log = str(action.get("action") or "?")
                    action_id_log = str(action.get("id") or "?")
                    payload_log = action.get("payload") if isinstance(action.get("payload"), dict) else {}
                    _overmind_log(
                        f"Executing Overmind action {action_name_log} ({action_id_log}) payload={payload_log}",
                        also_stdout=True,
                    )
                    status_value, message, result = _execute_overmind_action(settings, repository, action, config, base_url, token)
                    reboot_requested = (
                        str(action.get("action") or "").strip().lower() == "restart"
                        and status_value == "completed"
                        and not settings.use_fake_data
                    )
                    _record_processed_overmind_action(settings, action, status_value, message, result)
                    _overmind_log(
                        f"Processed Overmind action {action_name_log} ({action_id_log}): {status_value} - {message}",
                        also_stdout=True,
                    )
                    token = _report_overmind_action_completion(
                        settings,
                        repository,
                        config,
                        base_url,
                        token,
                        device_id,
                        action,
                        status_value,
                        message,
                        result,
                        integration_enabled,
                    )
                    if reboot_requested:
                        print(
                            f"Remote restart action acknowledged; exiting with code {DRONE_REMOTE_REBOOT_EXIT_CODE} for service supervisor reboot.",
                            file=sys.stdout,
                            flush=True,
                        )
                        os._exit(DRONE_REMOTE_REBOOT_EXIT_CODE)
            except (HTTPError, URLError) as error:
                print(f"Overmind action poll failed: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
            except (TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
                print(f"Overmind action poll failed: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
            except Exception as error:
                print(f"Overmind action poll unexpected error: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
            time.sleep(poll_seconds)

    thread = Thread(target=loop, name="overmind-action-poller", daemon=True)
    thread.start()


def _probe_peer_public_ip(settings: Settings, peer: dict, config: Optional[dict] = None) -> dict:
    """Health-check a peer using its public endpoint via mTLS https://<public_ip>[:port]/health."""
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or peer.get("id") or "")
    public_ip = str(peer.get("public_ip") or "").strip()
    api_port = _peer_api_port(peer)
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    result: dict = {
        "source_drone_id": settings.overmind_device_id,
        "target_drone_id": peer_id,
        "target_address": None,
        "public_ip": public_ip or None,
        "api_port": api_port,
        "status": "fail",
        "latency_ms": None,
        "failure_reason": None,
        "checked_at": checked_at,
    }
    if not public_ip:
        result["failure_reason"] = "no public IP available"
        return result
    host = public_ip
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port_suffix = "" if api_port == 443 else f":{api_port}"
    address = f"https://{host}{port_suffix}"
    result["target_address"] = address
    started = time.monotonic()
    try:
        _peer_get_json(_peer_health_url(address), settings, peer_id=peer_id, config=config)
        result["status"] = "pass"
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
    except ssl.SSLError as error:
        message = str(error)
        if config and any(term in message.lower() for term in ("unknown ca", "certificate", "cert")):
            try:
                _peer_get_json(_peer_health_url(address), settings, peer_id=peer_id, config=config, refresh_cert=True)
                result["status"] = "pass"
                result["latency_ms"] = int((time.monotonic() - started) * 1000)
                return result
            except Exception as retry_error:
                result["failure_reason"] = f"{message}; retry after cert refresh: {retry_error}"
                return result
        result["failure_reason"] = message
    except Exception as error:
        result["failure_reason"] = str(error)
    return result


def _start_peer_health_check_thread(settings: Settings) -> None:
    """Start a background thread that periodically health-checks swarm peers via their public IP."""
    interval = max(30, PEER_CHECK_INTERVAL_SECONDS)

    def loop() -> None:
        while True:
            time.sleep(interval)
            if not _local_network.is_overmind_mode(settings):
                continue
            try:
                config = _load_overmind_config_for_settings(settings)
                base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
                token = str(config.get("overmind_token") or "").strip()
                if not base_url or not token:
                    continue
                swarm = _load_state_payload(
                    _state_database_path(settings.userdata_root),
                    "overmind_swarm.json",
                    [],
                    legacy_path=_overmind_swarm_path_for_settings(settings),
                )
                if not swarm:
                    continue
                peer_results = []
                for peer in swarm:
                    peer_id = str(peer.get("drone_id") or peer.get("device_id") or peer.get("id") or "")
                    if not peer_id or peer_id == settings.overmind_device_id:
                        continue
                    if not str(peer.get("public_ip") or "").strip():
                        continue
                    result = _probe_peer_public_ip(settings, peer, config=config)
                    peer_results.append(result)
                    _overmind_log(
                        f"Peer health check: source={settings.overmind_device_id} target={peer_id} "
                        f"status={result['status']} address={result.get('target_address')} "
                        f"latency={result.get('latency_ms')}ms"
                    )
                if peer_results:
                    _save_state_payload(
                        _state_database_path(settings.userdata_root),
                        "peer_checks.json",
                        peer_results,
                    )
                    _overmind_peer_results_path_for_settings(settings).unlink(missing_ok=True)
                    device_id = quote(settings.overmind_device_id, safe="")
                    try:
                        _overmind_post_json(
                            f"{base_url}/api/devices/{device_id}/peer-checks",
                            {"results": peer_results},
                            token=token,
                            settings=settings,
                        )
                        _overmind_log(
                            f"Peer health checks reported to Overmind: {len(peer_results)} result(s)"
                        )
                    except Exception as report_error:
                        _overmind_log(
                            f"Failed to report peer health checks to Overmind: {_format_overmind_error(report_error)}"
                        )
            except Exception as error:
                _overmind_log(f"Peer health check thread error: {_format_overmind_error(error)}")

    thread = Thread(target=loop, name="peer-health-checker", daemon=True)
    thread.start()


def _start_local_network_workers(settings: Settings) -> None:
    def fingerprint() -> str:
        return str(DroneCertificateManager(settings).metadata().get("fingerprint") or "")

    _local_network.start_discovery_worker(settings, fingerprint)
    interval = max(10, int(os.environ.get("DRONE_LOCAL_HEALTH_INTERVAL_SECONDS", "30")))

    def health_loop() -> None:
        while True:
            time.sleep(interval)
            if not _local_network.is_local_mode(settings):
                continue
            checks = []
            for peer in _local_network.paired_peers(settings):
                peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
                address = _peer_address(peer)
                checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                result = {
                    "source_drone_id": settings.overmind_device_id,
                    "target_drone_id": peer_id,
                    "target_address": address,
                    "status": "fail",
                    "latency_ms": None,
                    "failure_reason": None,
                    "checked_at": checked_at,
                }
                if not address:
                    result["failure_reason"] = "no peer address available"
                    checks.append(result)
                    continue
                started = time.monotonic()
                try:
                    _peer_get_json(
                        f"{address.rstrip('/')}/v1/api/peer/health",
                        settings,
                        peer_id=peer_id,
                        config={"network_mode": "local_network"},
                    )
                    result["status"] = "pass"
                    result["latency_ms"] = int((time.monotonic() - started) * 1000)
                except Exception as error:
                    result["failure_reason"] = str(error)
                checks.append(result)
            _local_network.save_peer_checks(settings, checks)

    Thread(target=health_loop, name="drone-local-peer-health", daemon=True).start()


def _sync_rom_metadata_to_overmind(
    settings: Settings,
    repository: "RomRepository",
    config: dict,
    base_url: str,
    token: str,
    prepared_poll: Optional[Tuple[dict, bool, dict]] = None,
    *,
    force_upload: bool = False,
) -> dict:
    if not _local_network.is_overmind_mode(settings):
        return {"status": "skipped", "reason": "local_network_mode", "changed": False, "uploads": []}
    if not _begin_rom_metadata_activity("sync"):
        cache, _ = _load_rom_metadata_cache(settings)
        snapshot = _build_rom_metadata_snapshot_from_cache(settings, cache)
        return {
            "status": "skipped",
            "reason": "metadata_already_running",
            "changed": False,
            "rom_count": len(snapshot.get("roms") or []),
            "bios_count": len(snapshot.get("bios") or []),
            "artwork_count": len(snapshot.get("artwork") or []),
            "uploads": [],
            "stats": {"metadata_already_running": True},
        }
    try:
        return _sync_rom_metadata_to_overmind_locked(
            settings,
            repository,
            config,
            base_url,
            token,
            prepared_poll=prepared_poll,
            force_upload=force_upload,
        )
    finally:
        _end_rom_metadata_activity()


def _sync_rom_metadata_to_overmind_locked(
    settings: Settings,
    repository: "RomRepository",
    config: dict,
    base_url: str,
    token: str,
    prepared_poll: Optional[Tuple[dict, bool, dict]] = None,
    *,
    force_upload: bool = False,
) -> dict:
    poll_started = time.monotonic()
    snapshot, changed, stats = prepared_poll or _poll_rom_metadata_cache(settings, repository)
    rom_count = len(snapshot.get("roms") or [])
    bios_count = len(snapshot.get("bios") or [])
    artwork_count = len(snapshot.get("artwork") or [])
    device_id = quote(settings.overmind_device_id, safe="")
    upload_url = f"{base_url}/api/devices/{device_id}/rom-metadata"
    uploads = []

    def upload(payload: dict, phase: str) -> dict:
        nonlocal token
        update_mode = str(payload.get("update_mode") or phase)
        payload_bytes = _json_payload_size_bytes(payload)
        chunk_label = ""
        if update_mode == "inventory_chunk":
            chunk_label = f" chunk={int(payload.get('chunk_index') or 0) + 1}/{payload.get('chunk_total')}"
        try:
            status_code, response = _overmind_post_json_with_status(
                upload_url,
                payload,
                token=token,
                settings=settings,
                timeout_seconds=OVERMIND_UPLOAD_TIMEOUT_SECONDS,
            )
        except HTTPError as error:
            if error.code != 401:
                print(
                    f"Asset metadata upload failed: phase={phase} mode={update_mode}{chunk_label} payload_bytes={payload_bytes} error={_format_overmind_error(error)}",
                    file=sys.stderr,
                    flush=True,
                )
                raise
            replacement_token = _reclaim_overmind_token_after_unauthorized(settings, repository, config, base_url, error)
            if not replacement_token:
                print(
                    f"Asset metadata upload failed: phase={phase} mode={update_mode}{chunk_label} payload_bytes={payload_bytes} error={_format_overmind_error(error)}",
                    file=sys.stderr,
                    flush=True,
                )
                raise
            token = replacement_token
            try:
                status_code, response = _overmind_post_json_with_status(
                    upload_url,
                    payload,
                    token=token,
                    settings=settings,
                    timeout_seconds=OVERMIND_UPLOAD_TIMEOUT_SECONDS,
                )
            except Exception as retry_error:
                print(
                    f"Asset metadata upload failed after token refresh: phase={phase} mode={update_mode}{chunk_label} payload_bytes={payload_bytes} error={_format_overmind_error(retry_error)}",
                    file=sys.stderr,
                    flush=True,
                )
                raise
        except Exception as error:
            print(
                f"Asset metadata upload failed: phase={phase} mode={update_mode}{chunk_label} payload_bytes={payload_bytes} error={_format_overmind_error(error)}",
                file=sys.stderr,
                flush=True,
            )
            raise
        uploads.append({"phase": phase, "status_code": status_code, "payload_bytes": payload_bytes, "response": response})
        return response

    pending_changes = _read_pending_rom_metadata_changes(settings)
    has_cached_assets = bool(rom_count or bios_count or artwork_count)
    # A heartbeat that reported a thumbprint differing from ours means Overmind's
    # stored asset set drifted from the Drone's; resync by pushing a full inventory.
    push_requested = _ASSET_PUSH_REQUESTED.is_set()
    full_refresh = bool(
        force_upload
        or push_requested
        or stats.get("full_refresh_pending")
        or (has_cached_assets and not stats.get("had_successful_upload"))
    )
    payloads = _chunk_rom_metadata_inventory(settings, snapshot, replace_all=True) if full_refresh else _chunk_rom_metadata_delta(settings, snapshot, pending_changes)
    should_upload_inventory = bool(payloads)

    # Explain exactly why this sync did or did not fire. The reasons map 1:1 to the
    # full_refresh / delta decision inputs above, so "syncing when nothing changed" can be
    # traced to its trigger (e.g. heartbeat_thumbprint_mismatch sets _ASSET_PUSH_REQUESTED,
    # or a non-empty pending change queue from the scan).
    _pending_deleted = pending_changes.get("deleted") if isinstance(pending_changes.get("deleted"), dict) else {}
    pending_roms = len(pending_changes.get("roms") or [])
    pending_bios = len(pending_changes.get("bios") or [])
    pending_artwork = len(pending_changes.get("artwork") or [])
    pending_deleted = len(_pending_deleted.get("roms") or []) + len(_pending_deleted.get("bios") or []) + len(_pending_deleted.get("artwork") or [])
    first_upload_after_cache = bool(has_cached_assets and not stats.get("had_successful_upload"))
    sync_reasons = []
    if force_upload:
        sync_reasons.append("force_upload")
    if push_requested:
        sync_reasons.append("heartbeat_thumbprint_mismatch")
    if stats.get("full_refresh_pending"):
        sync_reasons.append("full_refresh_pending")
    if first_upload_after_cache:
        sync_reasons.append("first_upload_after_cache_load")
    if not full_refresh and should_upload_inventory:
        if pending_roms:
            sync_reasons.append(f"delta_roms={pending_roms}")
        if pending_bios:
            sync_reasons.append(f"delta_bios={pending_bios}")
        if pending_artwork:
            sync_reasons.append(f"delta_artwork={pending_artwork}")
        if pending_deleted:
            sync_reasons.append(f"delta_deletes={pending_deleted}")
    if not should_upload_inventory:
        sync_reasons.append("no_changes")
    decision = "full_refresh" if full_refresh else ("delta" if should_upload_inventory else "skip")
    _overmind_log(
        f"Asset metadata sync trigger: decision={decision} will_upload={should_upload_inventory} "
        f"reasons={','.join(sync_reasons) or 'none'} pending_roms={pending_roms} pending_bios={pending_bios} "
        f"pending_artwork={pending_artwork} pending_deletes={pending_deleted} chunks={len(payloads)} "
        f"force_upload={force_upload} push_requested={push_requested} "
        f"full_refresh_pending={bool(stats.get('full_refresh_pending'))} first_upload={first_upload_after_cache}"
    )

    if should_upload_inventory:
        upload_kind = "full refresh" if full_refresh else "delta"
        payload_sizes = [_json_payload_size_bytes(payload) for payload in payloads]
        total_payload_bytes = sum(payload_sizes)
        max_payload_bytes = max(payload_sizes) if payload_sizes else 0
        # High-level lifecycle event -> stdout (also recorded in overmind.log).
        _overmind_log(
            f"Asset metadata {upload_kind} sync started: roms={rom_count} bios={bios_count} artwork={artwork_count} chunks={len(payloads)} total_payload_bytes={total_payload_bytes}",
            also_stdout=True,
        )
        # Per-chunk detail -> overmind.log only.
        _overmind_log(
            f"Asset metadata {upload_kind} sync detail: endpoint={upload_url} max_payload_bytes={max_payload_bytes} timeout_seconds={OVERMIND_UPLOAD_TIMEOUT_SECONDS} force={force_upload}"
        )
        accepted_roms = 0
        accepted_bios = 0
        accepted_artwork = 0
        for index, payload in enumerate(payloads, start=1):
            payload_bytes = _json_payload_size_bytes(payload)
            _overmind_log(
                f"Asset metadata inventory chunk upload started: chunk={index}/{len(payloads)} payload_bytes={payload_bytes} roms={len(payload.get('roms') or [])} bios={len(payload.get('bios') or [])} artwork={len(payload.get('artwork') or [])}"
            )
            response = upload(payload, "inventory")
            accepted_roms += int(response.get("rom_count") or 0)
            accepted_bios += int(response.get("bios_count") or 0)
            accepted_artwork += int(response.get("artwork_count") or 0)
            _overmind_log(
                f"Asset metadata inventory chunk upload succeeded: chunk={index}/{len(payloads)} payload_bytes={payload_bytes} accepted_roms={response.get('rom_count')} accepted_bios={response.get('bios_count')} accepted_artwork={response.get('artwork_count')}"
            )
        _mark_rom_metadata_upload_clean(
            settings,
            snapshot.get("romset_files_thumbprint") or snapshot.get("rom_inventory_fingerprint"),
            snapshot.get("bios_files_thumbprint"),
        )
        _overmind_log(
            f"Asset metadata {upload_kind} sync succeeded: accepted_roms={accepted_roms} accepted_bios={accepted_bios} accepted_artwork={accepted_artwork}"
        )
        # Flush the pending-change queue now that this snapshot's inventory was accepted.
        # Without this the same pending rows re-upload on every poll forever (observed as
        # a steady "decision=delta delta_roms=N" loop in overmind.log).
        _clear_pending_rom_metadata_changes(settings)

    hash_batches = 0
    hashed_roms = 0
    hash_patch_failed = False
    for patch in _hash_rom_metadata_batches(settings, repository, batch_size=ROM_METADATA_FINGERPRINT_BATCH_SIZE):
        patch_roms = patch.get("roms") if isinstance(patch.get("roms"), list) else []
        payload = {"device_id": settings.overmind_device_id, **patch}
        progress = patch.get("hash_progress") or {}
        _overmind_log(
            f"Asset metadata hash patch sync started: endpoint={upload_url} payload_bytes={_json_payload_size_bytes(payload)} batch_roms={len(patch_roms)} processed={progress.get('processed')}/{progress.get('total')}"
        )
        try:
            upload(payload, "rom_hash_patch")
        except Exception:
            # fingerprint is already persisted to the local cache before the patch is sent,
            # so without this the next poll sees nothing pending to hash and never
            # resends — leaving Overmind with fingerprint-less rows forever. Flag a full
            # refresh so the next poll re-uploads the inventory (now carrying fingerprint)
            # and Overmind converges.
            hash_patch_failed = True
            _update_rom_metadata_cache_state(settings, dirty=True, full_refresh_pending=True)
            _overmind_log(
                "Asset metadata hash patch upload failed; flagged full refresh so fingerprint values resync on the next poll"
            )
            break
        hash_batches += 1
        hashed_roms += len(patch_roms)
    if not hash_patch_failed and (should_upload_inventory or hash_batches):
        if hash_batches:
            cache, _ = _load_rom_metadata_cache(settings)
            snapshot = _build_rom_metadata_snapshot_from_cache(settings, cache)
        _mark_rom_metadata_upload_clean(
            settings,
            snapshot.get("romset_files_thumbprint") or snapshot.get("rom_inventory_fingerprint"),
            snapshot.get("bios_files_thumbprint"),
        )

    if not should_upload_inventory and not hash_batches:
        # Advertise the fingerprint of what the drone *actually* holds (fingerprint
        # included), even when there is nothing to upload. If a past hash patch
        # never reached Overmind, its stored fingerprint still reflects the
        # fingerprint-less inventory; reporting the true data fingerprint in the next
        # heartbeat lets Overmind detect the drift and queue a resync on its own,
        # so already-stuck drones recover without a manual force.
        current_fingerprint = snapshot.get("rom_inventory_fingerprint")
        current_bios_thumbprint = str(snapshot.get("bios_files_thumbprint") or "")
        stored_romset, stored_bios = _local_asset_thumbprints(settings)
        stored_legacy = _rom_inventory_fingerprint_from_cache_state(settings) or ""
        if current_fingerprint and (
            current_fingerprint != stored_romset
            or current_fingerprint != stored_legacy
            or current_bios_thumbprint != stored_bios
        ):
            _update_rom_metadata_cache_state(
                settings,
                rom_inventory_fingerprint=current_fingerprint,
                rom_inventory_fingerprint_algorithm=ROM_INVENTORY_FINGERPRINT_ALGORITHM,
                romset_files_thumbprint=current_fingerprint,
                bios_files_thumbprint=current_bios_thumbprint,
                bios_inventory_fingerprint_algorithm=BIOS_INVENTORY_FINGERPRINT_ALGORITHM,
            )
        _overmind_log(
            f"Asset metadata sync skipped: no changes detected systems={stats.get('systems_scanned')} roms={stats.get('roms_discovered')} bios={stats.get('bios_discovered')} artwork={stats.get('artwork_discovered')} duration_ms={int((time.monotonic() - poll_started) * 1000)}"
        )
        return {
            "status": "skipped",
            "reason": "no_changes",
            "rom_count": rom_count,
            "bios_count": bios_count,
            "artwork_count": artwork_count,
            "changed": changed,
            "stats": stats,
        }
    _overmind_log(
        f"Asset metadata sync finished: inventory_uploaded={should_upload_inventory} hash_batches={hash_batches} hashed_roms={hashed_roms} duration_ms={int((time.monotonic() - poll_started) * 1000)}",
        also_stdout=bool(should_upload_inventory or hash_batches),
    )
    return {
        "status": "uploaded",
        "uploads": uploads,
        "rom_count": rom_count,
        "bios_count": bios_count,
        "artwork_count": artwork_count,
        "hash_batches": hash_batches,
        "hashed_roms": hashed_roms,
        "changed": changed,
        "forced": force_upload,
        "stats": stats,
    }


def _complete_local_rom_metadata_cache(settings: Settings, repository: "RomRepository", reason: str) -> dict:
    hash_batches = 0
    hashed_roms = 0
    for patch in _hash_rom_metadata_batches(settings, repository, batch_size=ROM_METADATA_FINGERPRINT_BATCH_SIZE):
        hash_batches += 1
        hashed_roms += len(patch.get("roms") or [])
    print(
        f"Asset metadata cached locally: upload_deferred={reason} hash_batches={hash_batches} hashed_roms={hashed_roms}",
        file=sys.stdout,
        flush=True,
    )
    return {
        "status": "cached",
        "reason": reason,
        "hash_batches": hash_batches,
        "hashed_roms": hashed_roms,
    }


def _defer_rom_metadata_upload(settings: Settings, reason: str) -> dict:
    cache, _ = _load_rom_metadata_cache(settings)
    cache["dirty"] = True
    _persist_rom_metadata_cache(settings, cache)
    print(
        f"Asset metadata upload deferred: reason={reason}",
        file=sys.stderr,
        flush=True,
    )
    return {
        "status": "deferred",
        "reason": reason,
        "changed": False,
    }


def _sync_saves_to_overmind(settings: Settings, base_url: str, token: str) -> dict:
    """Scan game saves and push created/updated/deleted entries to Overmind.

    Runs on the ROM-metadata poll cadence but with its own small payload so a saves
    change never forces a (much larger) ROM/BIOS re-upload. Uses a single-shot
    ``inventory_delta`` so it upserts changed saves and removes deleted ones WITHOUT
    touching ROM/BIOS rows (``replace_all`` would clear those). The on-disk saves
    thumbprint is persisted every scan so heartbeats can report and compare it; the
    upload only fires when something actually changed or a heartbeat flagged drift.
    """
    if not _local_network.is_overmind_mode(settings):
        return {"status": "skipped", "reason": "local_network_mode"}
    try:
        summary = _saves_store.sync_saves_cache(settings.saves_root)
    except Exception as error:
        _overmind_log(f"Saves scan failed: error={_format_overmind_error(error)}")
        return {"status": "scan_failed"}
    current = str(summary.get("thumbprint") or "").strip()
    _update_rom_metadata_cache_state(settings, saves_files_thumbprint=current)
    pending = _saves_store.read_pending_changes(settings.saves_root)
    has_changes = bool(pending.get("saves") or pending.get("deleted"))
    push_requested = _SAVES_PUSH_REQUESTED.is_set()
    try:
        uploaded_state = _read_rom_metadata_cache_state(settings, "saves_files_thumbprint_uploaded")
    except Exception:
        uploaded_state = {}
    last_uploaded = str(uploaded_state.get("saves_files_thumbprint_uploaded") or "").strip()
    # Explain why a saves sync did or did not fire (mirrors the asset sync trigger log).
    saves_reasons = []
    if has_changes:
        saves_reasons.append(f"changed_saves={len(pending.get('saves') or [])}")
        if pending.get("deleted"):
            saves_reasons.append(f"deleted_saves={len(pending.get('deleted') or [])}")
    if push_requested:
        saves_reasons.append("heartbeat_thumbprint_mismatch")
    if not has_changes and not push_requested and current != last_uploaded:
        saves_reasons.append("thumbprint_changed_since_upload")
    will_upload = bool(has_changes or push_requested or current != last_uploaded)
    _overmind_log(
        f"Saves sync trigger: will_upload={will_upload} reasons={','.join(saves_reasons) or 'none'} "
        f"thumbprint={current[:12]} last_uploaded={last_uploaded[:12]} "
        f"has_changes={has_changes} push_requested={push_requested}"
    )
    if not has_changes and not push_requested and current == last_uploaded:
        return {"status": "unchanged", "thumbprint": current}
    if has_changes:
        upsert_rows = pending.get("saves") or []
        deleted_rows = pending.get("deleted") or []
    else:
        # Drift resync / first push with an empty change queue: re-assert the full set.
        upsert_rows = _saves_store.list_saves(settings.saves_root)
        deleted_rows = []
    payload = {
        "device_id": settings.overmind_device_id,
        "type": "asset_metadata",
        "update_mode": "inventory_delta",
        "saves": upsert_rows,
        "deleted": {"saves": deleted_rows},
        "saves_files_thumbprint": current,
        "saves_root": str(settings.saves_root),
        "inventory_complete": True,
    }
    device_id = quote(settings.overmind_device_id, safe="")
    upload_url = f"{base_url}/api/devices/{device_id}/rom-metadata"
    try:
        status_code, _ = _overmind_post_json_with_status(
            upload_url,
            payload,
            token=token,
            settings=settings,
            timeout_seconds=OVERMIND_UPLOAD_TIMEOUT_SECONDS,
        )
    except Exception as error:
        _overmind_log(f"Saves upload failed: error={_format_overmind_error(error)}")
        return {"status": "upload_failed"}
    _saves_store.clear_pending_changes(settings.saves_root)
    _update_rom_metadata_cache_state(settings, saves_files_thumbprint_uploaded=current)
    _SAVES_PUSH_REQUESTED.clear()
    # High-level lifecycle event -> stdout (also recorded in overmind.log).
    _overmind_log(
        f"Saves sync uploaded: upserts={len(upsert_rows)} deletes={len(deleted_rows)} "
        f"thumbprint={current[:12]} status={status_code}",
        also_stdout=True,
    )
    return {"status": "ok", "upserts": len(upsert_rows), "deletes": len(deleted_rows), "thumbprint": current}


def _poll_rom_metadata_once(settings: Settings, repository: "RomRepository") -> dict:
    if not _begin_rom_metadata_activity("poll"):
        return {"status": "skipped", "reason": "metadata_already_running", "changed": False}
    try:
        prepared_poll = _poll_rom_metadata_cache(settings, repository)
        if _local_network.is_local_mode(settings) and not _local_network.is_overmind_mode(settings):
            try:
                _saves_store.sync_saves_cache(settings.saves_root)
            except Exception as error:
                print(f"Local saves cache scan failed: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
            return _complete_local_rom_metadata_cache(settings, repository, "overmind_disabled")
        config = _load_overmind_config_for_settings(settings)
        base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
        token = str(config.get("overmind_token") or "").strip()
        if not base_url:
            return _complete_local_rom_metadata_cache(settings, repository, "overmind_not_configured")
        if not token:
            auth_token = str(config.get("overmind_auth_token") or "").strip()
            if auth_token:
                token = _register_or_claim_overmind_token(settings, repository, config, base_url) or ""
            if not token:
                return _complete_local_rom_metadata_cache(settings, repository, "overmind_not_connected")
        try:
            result = _sync_rom_metadata_to_overmind_locked(settings, repository, config, base_url, token, prepared_poll=prepared_poll)
        except Exception:
            _defer_rom_metadata_upload(settings, "overmind_upload_failed")
            raise
        # Best-effort: a saves sync failure must not affect ROM metadata results.
        try:
            _sync_saves_to_overmind(settings, base_url, token)
        except Exception as error:
            _overmind_log(f"Saves sync failed: error={_format_overmind_error(error)}")
        return result
    finally:
        _end_rom_metadata_activity()


def _start_rom_metadata_poller(settings: Settings, repository: "RomRepository") -> None:
    poll_seconds = max(30, int(settings.rom_metadata_poll_seconds or ROM_METADATA_POLL_SECONDS))
    initial_delay_seconds = max(
        0,
        int(os.environ.get("ROM_METADATA_INITIAL_DELAY_SECONDS", str(ROM_METADATA_INITIAL_DELAY_SECONDS))),
    )
    print(
        f"Asset metadata poller starting: poll_seconds={poll_seconds} initial_delay_seconds={initial_delay_seconds}",
        file=sys.stdout,
        flush=True,
    )

    def loop() -> None:
        if initial_delay_seconds:
            print(
                f"Asset metadata poll delayed at startup: seconds={initial_delay_seconds}",
                file=sys.stdout,
                flush=True,
            )
            if _ROM_METADATA_WAKE.wait(initial_delay_seconds):
                _ROM_METADATA_WAKE.clear()
        while True:
            poll_started = time.monotonic()
            try:
                _poll_rom_metadata_once(settings, repository)
            except (HTTPError, URLError) as error:
                status_part = f" status={error.code}" if isinstance(error, HTTPError) else ""
                print(
                    f"ROM metadata sync failed:{status_part} error={_format_overmind_error(error)} duration_ms={int((time.monotonic() - poll_started) * 1000)}",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception as error:
                print(
                    f"ROM metadata sync failed: error={_format_overmind_error(error)} duration_ms={int((time.monotonic() - poll_started) * 1000)}",
                    file=sys.stderr,
                    flush=True,
                )
            if _ROM_METADATA_WAKE.wait(poll_seconds):
                _ROM_METADATA_WAKE.clear()

    thread = Thread(target=loop, name="rom-metadata-poller", daemon=True)
    thread.start()
    print("Asset metadata poller thread started", file=sys.stdout, flush=True)


def _start_rom_metadata_watcher(settings: Settings) -> None:
    """Wake the metadata poller in near real time when ROM files change.

    Best-effort: if inotify is unavailable the periodic poll still covers
    changes, so a failure here is logged and otherwise ignored.
    """
    global _ROM_METADATA_WATCHER, _SAVES_METADATA_WATCHER
    watcher = RomFilesystemWatcher(
        settings.roms_root,
        _ROM_METADATA_WAKE.set,
        debounce_seconds=ROM_METADATA_WATCH_DEBOUNCE_SECONDS,
        max_delay_seconds=ROM_METADATA_WATCH_MAX_DELAY_SECONDS,
    )
    if watcher.start():
        _ROM_METADATA_WATCHER = watcher
    # Watch the saves tree too so a created/updated/deleted save wakes the poller in
    # near real time; the periodic poll still covers it if inotify is unavailable.
    saves_watcher = RomFilesystemWatcher(
        settings.saves_root,
        _ROM_METADATA_WAKE.set,
        debounce_seconds=ROM_METADATA_WATCH_DEBOUNCE_SECONDS,
        max_delay_seconds=ROM_METADATA_WATCH_MAX_DELAY_SECONDS,
    )
    if saves_watcher.start():
        _SAVES_METADATA_WATCHER = saves_watcher


def _ensure_game_event_spool(settings: Settings) -> None:
    """Prepare the durable process-monitor event spool and remove the legacy hook."""
    target = (settings.userdata_root / "system" / "scripts" / "drone-game-event.sh").resolve()
    spool = (settings.userdata_root / "system" / "drone-app" / "game-events").resolve()
    try:
        spool.mkdir(parents=True, exist_ok=True)
        try:
            spool.chmod(0o2775)
        except OSError:
            pass
        if target.exists():
            target.unlink()
            print(f"Legacy gameplay event hook removed: {target}", file=sys.stdout, flush=True)
    except OSError as error:
        print(f"Gameplay event spool setup skipped: {_format_overmind_error(error)}", file=sys.stderr, flush=True)


class DroneThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    # Per-IP throttle so a chatty unpaired peer (or scanner) can't flood the log
    # with one identical line per connection attempt.
    _drop_log_lock = Lock()
    _drop_log_last: Dict[str, float] = {}
    _DROP_LOG_INTERVAL_SECONDS = 60.0

    def handle_error(self, request, client_address):
        # The public HTTPS port is constantly probed by internet scanners sending
        # non-TLS or malformed payloads, which surface as SSL/connection errors during
        # request handling. socketserver's default dumps a full traceback for each,
        # spamming stderr. Log a single concise line for these benign cases instead;
        # fall back to the noisy traceback only for genuinely unexpected errors.
        error = sys.exc_info()[1]
        if isinstance(error, (ssl.SSLError, ConnectionError, BrokenPipeError, TimeoutError, OSError)):
            ip = client_address[0] if isinstance(client_address, (tuple, list)) and client_address else client_address
            now = time.monotonic()
            cls = DroneThreadingHTTPServer
            with cls._drop_log_lock:
                last = cls._drop_log_last.get(str(ip))
                if last is not None and now - last < cls._DROP_LOG_INTERVAL_SECONDS:
                    return
                cls._drop_log_last[str(ip)] = now
            hint = ""
            reason = str(error).lower()
            if "certificate" in reason and not _is_external_client_ip(str(ip)):
                # On a LAN this is almost always another Drone that this one has not
                # paired with (or that is not running HTTPS) trying to transfer.
                hint = " — this looks like a Drone on your network that is not paired with this one (or is not running HTTPS). Pair it under Admin > Integration > Local Network. (repeats from this IP are suppressed for 60s)"
            print(
                f"Dropped untrusted/insecure connection from {ip}: {error.__class__.__name__}: {error}{hint}",
                file=sys.stderr,
                flush=True,
            )
            return
        super().handle_error(request, client_address)


def _apply_server_tls(settings: Settings, server: ThreadingHTTPServer) -> None:
    if settings.http_only:
        return
    if settings.drone_mtls_mode == "managed" and not (settings.drone_cert_file.exists() and settings.drone_key_file.exists()):
        raise RuntimeError("managed Drone mTLS mode requires DRONE_CERT_FILE and DRONE_KEY_FILE")
    if settings.drone_cert_file.exists() and settings.drone_key_file.exists():
        cert_file, key_file = settings.drone_cert_file, settings.drone_key_file
    else:
        cert_file, key_file = _resolve_tls_material(settings)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    if settings.drone_mtls_enabled or _local_network.is_local_mode(settings):
        ssl_context.verify_mode = ssl.CERT_OPTIONAL
        if settings.drone_mtls_ca_file and settings.drone_mtls_ca_file.exists():
            ssl_context.load_verify_locations(cafile=str(settings.drone_mtls_ca_file))
        for peer in _local_network.paired_peers(settings):
            cert_path = Path(str(peer.get("certificate_path") or ""))
            if cert_path.exists():
                try:
                    ssl_context.load_verify_locations(cafile=str(cert_path))
                except ssl.SSLError:
                    continue
        # Belt-and-suspenders: also trust every cert in the local-peer-certs store
        # so a paired peer stays trusted across restarts even if its record's
        # certificate_path drifts or is missing. Pairing also injects new certs
        # into this live context (see _handle_peer_pair), so post-startup pairings
        # work without a restart too.
        local_certs_dir = _local_peer_cert_cache_path(settings, "x").parent
        if local_certs_dir.exists():
            for cert_file_path in sorted(local_certs_dir.glob("*.crt")):
                try:
                    ssl_context.load_verify_locations(cafile=str(cert_file_path))
                except ssl.SSLError:
                    continue
    server.ssl_context = ssl_context  # type: ignore[attr-defined]
    # do_handshake_on_connect=False is critical: wrapping the LISTENING socket otherwise
    # makes accept() perform the TLS handshake on the single serve_forever thread, so one
    # silent client (e.g. an internet scanner that opens 443 and never speaks) blocks
    # accept() forever and wedges the whole server. Deferring the handshake lets accept()
    # return immediately; the handshake then runs in the per-request worker thread under
    # RomRequestHandler.timeout, where a stall costs only that one thread.
    server.socket = ssl_context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)


def create_server(settings: Settings) -> ThreadingHTTPServer:
    global _OVERMIND_POLLER_STARTED, _ROM_METADATA_POLLER_STARTED, _ROM_METADATA_WATCHER_STARTED, _PEER_HEALTH_CHECK_THREAD_STARTED, _LOCAL_NETWORK_WORKERS_STARTED, _GAME_PROCESS_MONITOR_STARTED, _GAME_PROCESS_MONITOR, _DOWNLOAD_MANAGER
    roms_root, bios_root = _real_data_roots(settings)
    repository = RomRepository(
        roms_root,
        bios_root,
        rom_search_cache_ttl_seconds=settings.rom_search_cache_ttl_seconds,
        settings=settings,
    )
    credential_store = DroneCredentialStore(
        settings.credentials_file,
        settings.username,
        settings.password,
        state_database_file=_state_database_path(settings.userdata_root),
    )
    auth = BasicAuth(settings.username, settings.password, credential_store=credential_store)
    cert_state = DroneCertificateManager(settings).ensure_certificate()
    if cert_state.get("error"):
        message = f"Drone certificate setup: {cert_state.get('error')}"
        if settings.drone_mtls_mode == "managed":
            raise RuntimeError(message)
        print(message, file=sys.stderr, flush=True)

    image_cache = ExpiringLRUCache(
        ttl_seconds=settings.image_cache_ttl_seconds,
        max_items=settings.image_cache_max_items,
        max_bytes=settings.image_cache_max_bytes,
    )
    image_miss_cache = ExpiringKeyCache(settings.image_miss_cache_ttl_seconds)
    json_cache = ExpiringLRUCache(
        ttl_seconds=settings.json_cache_ttl_seconds,
        max_items=settings.json_cache_max_items,
        max_bytes=settings.json_cache_max_bytes,
    )
    if _DOWNLOAD_MANAGER is None:
        _DOWNLOAD_MANAGER = DownloadManager(settings, repository)
    _ensure_game_event_spool(settings)
    if not _GAME_PROCESS_MONITOR_STARTED:
        poll_seconds = max(0.25, float(os.environ.get("GAME_PROCESS_POLL_SECONDS", "2")))
        _GAME_PROCESS_MONITOR = GameProcessMonitor(settings, poll_seconds=poll_seconds)
        _GAME_PROCESS_MONITOR.start()
        _GAME_PROCESS_MONITOR_STARTED = True

    handler_factory = _build_handler(
        settings=settings,
        auth=auth,
        repository=repository,
        image_cache=image_cache,
        image_miss_cache=image_miss_cache,
        json_cache=json_cache,
    )

    server = DroneThreadingHTTPServer(("0.0.0.0", settings.https_port), handler_factory)
    server.auth = auth  # type: ignore[attr-defined]
    _apply_server_tls(settings, server)

    compatibility_servers = []
    for compatibility_port in settings.compatibility_https_ports:
        try:
            compatibility_server = DroneThreadingHTTPServer(("0.0.0.0", compatibility_port), handler_factory)
            compatibility_server.auth = auth  # type: ignore[attr-defined]
            _apply_server_tls(settings, compatibility_server)
        except OSError as error:
            print(
                f"Drone compatibility listener skipped on port {compatibility_port}: {error}",
                file=sys.stderr,
                flush=True,
            )
            continue
        compatibility_thread = Thread(
            target=compatibility_server.serve_forever,
            name=f"drone-compat-listener-{compatibility_port}",
            daemon=True,
        )
        compatibility_thread.start()
        compatibility_server.thread = compatibility_thread  # type: ignore[attr-defined]
        compatibility_servers.append(compatibility_server)
        scheme = "http" if settings.http_only else "https"
        print(f"Serving Drone compatibility listener on {scheme}://0.0.0.0:{compatibility_port}", flush=True)
    server.compatibility_servers = compatibility_servers  # type: ignore[attr-defined]

    if not _OVERMIND_POLLER_STARTED:
        _start_overmind_action_poller(settings, repository)
        _OVERMIND_POLLER_STARTED = True
    if not _PEER_HEALTH_CHECK_THREAD_STARTED:
        _start_peer_health_check_thread(settings)
        _PEER_HEALTH_CHECK_THREAD_STARTED = True
    if not _LOCAL_NETWORK_WORKERS_STARTED:
        _start_local_network_workers(settings)
        _LOCAL_NETWORK_WORKERS_STARTED = True
    if settings.rom_metadata_poll_seconds == 0:
        print("Asset metadata poller disabled: ROM_METADATA_POLL_SECONDS=0", file=sys.stdout, flush=True)
    elif not _ROM_METADATA_POLLER_STARTED:
        _start_rom_metadata_poller(settings, repository)
        _ROM_METADATA_POLLER_STARTED = True
    else:
        print("Asset metadata poller already started", file=sys.stdout, flush=True)

    # Near-real-time ROM change detection wakes the poller above; only useful
    # when the poller is running.
    if settings.rom_metadata_poll_seconds == 0 or not ROM_METADATA_WATCH_ENABLED:
        if not ROM_METADATA_WATCH_ENABLED:
            print("ROM filesystem watcher disabled: ROM_METADATA_WATCH_ENABLED=0", file=sys.stdout, flush=True)
    elif not _ROM_METADATA_WATCHER_STARTED:
        _start_rom_metadata_watcher(settings)
        _ROM_METADATA_WATCHER_STARTED = True

    return server


def main() -> None:
    settings = Settings.from_env()
    try:
        if settings.use_fake_data:
            try:
                from .mock_data import seed_mock_userdata
            except ImportError:
                from mock_data import seed_mock_userdata  # type: ignore

            seed_mock_userdata(settings.userdata_root)
            print(f"USE_FAKE_DATA enabled: seeded fake dataset at {settings.userdata_root}")
        _configure_rotating_logs(settings)
        server = create_server(settings)
        print(f"Log files: {settings.log_dir / settings.stdout_log_file}, {settings.log_dir / settings.stderr_log_file}")
        server_auth = getattr(server, "auth", None)
        credential_store = getattr(server_auth, "credential_store", None)
        safe_username = credential_store.load().get("username") if credential_store else settings.username
        print(f"Auth username: {safe_username}")
        scheme = "http" if settings.http_only else "https"
        print(f"Serving Drone App on {scheme}://0.0.0.0:{settings.https_port}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        print("Drone App shutdown requested", file=sys.stderr, flush=True)
        raise
    except BaseException:
        print("Drone App fatal error:", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
