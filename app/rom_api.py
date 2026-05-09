import base64
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlparse

try:
    from .api_routes import ApiRoutesMixin
    from .route_config import API_PREFIX, api_url
    from .ui_routes import UiRoutesMixin
except ImportError:
    from api_routes import ApiRoutesMixin  # type: ignore
    from route_config import API_PREFIX, api_url  # type: ignore
    from ui_routes import UiRoutesMixin  # type: ignore


FAKE_OVERMIND_EMAIL = "arcade@example.com"
FAKE_OVERMIND_PASSWORD = "ArcadePass123"
FAKE_OVERMIND_DEVICE_ID = "arcade-cabinet-002"
LAUNCHBOX_API_BASE = "https://api.gamesdb.launchbox-app.com/api"
LAUNCHBOX_IMAGE_BASE = "https://images.launchbox-app.com"
ARTWORK_FIELDS = ("image", "thumbnail", "marquee", "fanart", "boxart")
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
    "image": ("Box - Front",),
    "thumbnail": ("Box - Front", "Screenshot - Game Title", "Screenshot - Gameplay"),
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


def _clean_rom_title(value: str) -> str:
    name = Path(value or "").stem
    name = re.sub(r"\[[^\]]*\]", " ", name)
    name = re.sub(r"\([^)]*\)", " ", name)
    name = re.sub(r"\s+", " ", name.replace("_", " ")).strip()
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


def _set_child_text(parent: ET.Element, tag: str, value: str) -> None:
    child = parent.find(tag)
    if child is None:
        child = ET.SubElement(parent, tag)
    child.text = value


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


class LaunchBoxClient:
    def __init__(self, timeout_seconds: int = 15):
        self.timeout_seconds = timeout_seconds

    def _get_json(self, url: str) -> dict:
        request = Request(url, headers={"User-Agent": "batocera-drone-rom-api/4.0"})
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
            "images": images,
        }

    def choose_image_for_field(self, details: dict, field: str) -> Optional[dict]:
        wanted = LAUNCHBOX_FIELD_TYPES.get(field, ())
        images = details.get("images") or []
        for image_type in wanted:
            for image in images:
                if str(image.get("type") or "").lower() == image_type.lower():
                    return image
        return images[0] if field in ("image", "thumbnail", "boxart") and images else None

    def download_image(self, url: str) -> Tuple[bytes, str]:
        request = Request(url, headers={"User-Agent": "batocera-drone-rom-api/4.0"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            content_type = response.headers.get_content_type()
            return response.read(), content_type


@dataclass(frozen=True)
class Settings:
    userdata_root: Path
    roms_root: Path
    bios_root: Path
    username: str
    password: str
    https_port: int

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
    overmind_device_id: str
    overmind_poll_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        https_port_value = os.environ.get("HTTPS_PORT", os.environ.get("PORT", "8443"))
        cert_value = os.environ.get("TLS_CERT_FILE")
        key_value = os.environ.get("TLS_KEY_FILE")
        use_fake_data = _env_bool(False, "USE_FAKE_DATA")

        return cls(
            userdata_root=Path(os.environ.get("USERDATA_ROOT", "/userdata")),
            roms_root=Path(os.environ.get("ROMS_ROOT", "/userdata/roms")),
            bios_root=Path(os.environ.get("BIOS_ROOT", "/userdata/bios")),
            username=_require_any_env("ROM_API_USERNAME", "USERNAME"),
            password=_require_any_env("ROM_API_PASSWORD", "PASSWORD"),
            https_port=int(https_port_value),
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
            http_only=_env_bool(False, "HTTP_ONLY", "ROM_API_HTTP_ONLY"),
            use_fake_data=use_fake_data,
            fake_image_base_url=os.environ.get("FAKE_IMAGE_BASE_URL"),
            overmind_url=os.environ.get("OVERMIND_URL"),
            overmind_email=os.environ.get("OVERMIND_EMAIL"),
            overmind_password=os.environ.get("OVERMIND_PASSWORD"),
            overmind_device_id=os.environ.get(
                "OVERMIND_DEVICE_ID",
                os.environ.get("DEVICE_ID", FAKE_OVERMIND_DEVICE_ID if use_fake_data else "batocera-drone"),
            ),
            overmind_poll_seconds=int(os.environ.get("OVERMIND_POLL_SECONDS", "5")),
        )


class _TeeRotatingStream:
    def __init__(self, original_stream, log_path: Path, max_bytes: int, backup_count: int):
        self._original_stream = original_stream
        self._log_path = log_path
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._file = self._log_path.open("a", encoding="utf-8")
        self._lock = Lock()

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

    def write(self, data: str) -> int:
        if not isinstance(data, str):
            data = str(data)
        with self._lock:
            if data:
                self._file.write(data)
                self._rollover_if_needed()
            self._original_stream.write(data)
            return len(data)

    def flush(self) -> None:
        with self._lock:
            self._original_stream.flush()
            self._file.flush()

    def isatty(self) -> bool:
        return self._original_stream.isatty()


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


class BasicAuth:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

    def check(self, header_value: Optional[str]) -> bool:
        if not header_value or not header_value.startswith("Basic "):
            return False

        try:
            encoded = header_value.split(" ", 1)[1].strip()
            decoded = base64.b64decode(encoded).decode("utf-8")
            user, pw = decoded.split(":", 1)
            return user == self.username and pw == self.password
        except Exception:
            return False


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
    def __init__(self, roms_root: Path, bios_root: Path, rom_search_cache_ttl_seconds: int = 300):
        self.roms_root = roms_root
        self.bios_root = bios_root
        self.rom_search_cache_ttl_seconds = rom_search_cache_ttl_seconds
        self._search_cache_lock = Lock()
        self._search_index: List[dict] = []
        self._search_index_expires_at = 0.0
        self._missing_artwork_cache_lock = Lock()
        self._missing_artwork_cache: Dict[str, dict] = {}

    @staticmethod
    def should_include_system(name: str) -> bool:
        return True

    @staticmethod
    def build_unique_id(path: Path) -> str:
        resolved = path.resolve()
        stat = resolved.stat()
        raw = f"{resolved}|{stat.st_size}|{int(stat.st_mtime)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def build_md5(path: Path) -> str:
        digest = hashlib.md5()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def should_ignore_rom_file(file_name: str, system: Optional[str] = None) -> bool:
        lower = str(file_name or "").strip().lower()
        if lower in {"_info.txt", "gamelist.xml", ".keep"}:
            return True
        if lower.endswith(".sh.keys"):
            return True
        return False

    @staticmethod
    def iter_files(path: Path) -> Iterable[Path]:
        if not path.exists() or not path.is_dir():
            return []
        return [entry for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()) if entry.is_file()]

    def _list_rom_items(self, system: str, asset_dir: Path) -> List[dict]:
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
                    stat = entry.stat()
                    display_name = entry.name[:-4]
                    items.append(
                        {
                            "unique_id": self.build_unique_id(entry),
                            "name": display_name,
                            "rom_file": entry.name,
                            "byte_count": stat.st_size,
                            "entry_type": "folder",
                            "is_downloadable": False,
                            "source_folder": entry.name,
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

                stat = entry.stat()
                display_name = ps4_name_file.stem
                items.append(
                    {
                            "unique_id": self.build_unique_id(entry),
                            "name": display_name,
                            "rom_file": entry.name,
                        "byte_count": stat.st_size,
                        "entry_type": "folder",
                        "is_downloadable": False,
                        "source_folder": entry.name,
                        "image_stem": display_name,
                    }
                )
            return items

        for entry in self.iter_files(asset_dir):
            if self.should_ignore_rom_file(entry.name, system=system):
                continue
            display_name = Path(entry.name).stem
            stat = entry.stat()
            items.append(
                {
                    "unique_id": self.build_unique_id(entry),
                    "name": display_name,
                    "rom_file": entry.name,
                    "byte_count": stat.st_size,
                    "entry_type": "file",
                    "is_downloadable": (system_lower != "steam"),
                    "image_stem": display_name,
                }
            )
        return items

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

    def list_systems(self) -> List[dict]:
        if not self.roms_root.exists():
            raise FileNotFoundError()

        systems = []
        for entry in sorted(self.roms_root.iterdir(), key=lambda p: p.name.lower()):
            if not (entry.is_dir() or entry.is_symlink()):
                continue
            if not self.should_include_system(entry.name):
                continue

            target_dir = entry.resolve()
            if not target_dir.exists() or not target_dir.is_dir():
                continue

            rom_count = len(self._list_rom_items(entry.name, target_dir))
            if rom_count < 2:
                continue

            systems.append({"name": entry.name, "rom_count": rom_count})

        return systems

    def _build_search_index(self) -> List[dict]:
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
                    }
                )
        return index

    def search_roms(self, query: str, limit: Optional[int] = None, system_filter: Optional[str] = None) -> List[dict]:
        normalized = query.strip().lower()
        if not normalized:
            return []
        normalized_system_filter = system_filter.strip().lower() if system_filter else None

        with self._search_cache_lock:
            now = time.time()
            if now >= self._search_index_expires_at:
                self._search_index = self._build_search_index()
                self._search_index_expires_at = now + self.rom_search_cache_ttl_seconds
            source = list(self._search_index)

        results = []
        for item in source:
            if normalized not in item["name"].lower():
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
        return missing

    def _list_missing_artwork_from_gamelists(self) -> List[dict]:
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
                if not missing:
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
                        "has_gamelist_entry": True,
                    }
                )
        missing_items.sort(key=lambda item: (str(item["system"]).lower(), str(item["name"]).lower()))
        return missing_items

    def _list_missing_artwork_from_filesystem(self) -> List[dict]:
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
                if not missing:
                    continue
                missing_items.append(
                    {
                        "system": system,
                        "name": rom.get("name"),
                        "rom_name": rom_file,
                        "rom_path": rom_file,
                        "title": _text_or_empty(game, "name") if game is not None else str(rom.get("image_stem") or rom.get("name") or ""),
                        "search_title": _clean_rom_title(str(rom.get("image_stem") or rom.get("name") or "")),
                        "unique_id": rom.get("unique_id"),
                        "missing": missing,
                        "existing": {field: _text_or_empty(game, field) if game is not None else "" for field in ARTWORK_FIELDS},
                        "has_gamelist_entry": game is not None,
                    }
                )
        missing_items.sort(key=lambda item: (str(item["system"]).lower(), str(item["name"]).lower()))
        return missing_items

    def list_missing_artwork(self, include_filesystem: bool = False, force_refresh: bool = False) -> List[dict]:
        cache_key = "filesystem" if include_filesystem else "gamelist"
        now = time.time()
        with self._missing_artwork_cache_lock:
            cached = self._missing_artwork_cache.get(cache_key)
            if cached and not force_refresh and cached.get("expires_at", 0) > now:
                return [dict(item) for item in cached.get("items", [])]

        items = self._list_missing_artwork_from_filesystem() if include_filesystem else self._list_missing_artwork_from_gamelists()
        with self._missing_artwork_cache_lock:
            self._missing_artwork_cache[cache_key] = {
                "items": [dict(item) for item in items],
                "expires_at": time.time() + 120,
            }
        return items

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

    def apply_launchbox_artwork(
        self,
        system: str,
        unique_id: str,
        game_key: str,
        client: LaunchBoxClient,
        rom_path: Optional[str] = None,
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

        missing = self._entry_missing_artwork(game)
        if not missing:
            return {"system": system, "unique_id": unique_id, "updated": [], "skipped": list(ARTWORK_FIELDS)}

        details = client.details(game_key)
        images_dir = system_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(display_name).stem).strip("-") or "rom"
        updated = []
        skipped = []
        for field in missing:
            selected = client.choose_image_for_field(details, field)
            if not selected:
                skipped.append(field)
                continue
            source_url = str(selected.get("url") or "")
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
        }

    def list_assets(self, system: str, asset_type: str) -> Tuple[Path, List[dict]]:
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
        if asset_dir.exists() and asset_dir.is_dir():
            if asset_type == "roms":
                items = self._list_rom_items(system, asset_dir)
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
            entries.append(
                {
                    "entry_type": "file",
                    "name": file_path.name,
                    "path": relative_path,
                    "unique_id": self.build_unique_id(file_path),
                    "byte_count": size,
                    "md5": self.build_md5(file_path),
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
        "title": "ROM API",
        "version": "4.0",
        "description": "Browse and download ROM, image, video, and BIOS assets.",
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
        "/admin/artwork/missing": {
            "get": {
                "summary": "List ROMs whose gamelist.xml entries are missing artwork fields",
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
    },
}

class RomRequestHandler(ApiRoutesMixin, UiRoutesMixin, BaseHTTPRequestHandler):
    server_version = "RomAPI/4.0"
    openapi_spec = OPENAPI_SPEC

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
        self.log_error(
            '401 unauthorized "%s" auth_header_present=%s',
            self.path.split("?", 1)[0],
            "yes" if self.headers.get("Authorization") else "no",
        )
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ROM API"')
        self.send_header("Content-Type", "application/json")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(json_bytes({"error": "unauthorized"}))

    def _send_security_headers(self) -> None:
        image_sources = ["'self'", "data:", "https://images.launchbox-app.com"]
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
            f"default-src 'self'; img-src {' '.join(image_sources)}; style-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
            "font-src 'self' data: https://cdn.jsdelivr.net; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
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

    def _send_html(self, status_code: int, html: str) -> None:
        body = html_bytes(html)
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

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
        return (self.settings.userdata_root / "system" / "rom-api" / "overmind_integration.json").resolve()

    def _overmind_actions_path(self) -> Path:
        return (self.settings.userdata_root / "system" / "rom-api" / "overmind_processed_actions.json").resolve()

    def _mask_secret(self, value: str) -> str:
        if not value:
            return ""
        if len(value) <= 4:
            return "*" * len(value)
        return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"

    def _load_overmind_config(self) -> dict:
        fake_email = FAKE_OVERMIND_EMAIL if self.settings.use_fake_data else ""
        fake_password = FAKE_OVERMIND_PASSWORD if self.settings.use_fake_data else ""
        default = {
            "overmind_url": (self.settings.overmind_url or "").strip(),
            "overmind_email": (fake_email if self.settings.use_fake_data else self.settings.overmind_email or "").strip(),
            "integration_enabled": False,
            "integration_state": "not_started",
            "requested_at": None,
            "last_started_at": None,
            "last_error": None,
            "notes": "Stub integration until batocera.overmind app is available.",
        }

        if self.settings.overmind_password or fake_password:
            default["overmind_password"] = fake_password if self.settings.use_fake_data else self.settings.overmind_password

        path = self._overmind_config_path()
        if not path.exists() or not path.is_file():
            return default
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return default
        except Exception:
            return default

        merged = dict(default)
        merged.update(loaded)
        if self.settings.use_fake_data:
            merged["overmind_email"] = FAKE_OVERMIND_EMAIL
            merged["overmind_password"] = FAKE_OVERMIND_PASSWORD
        return merged

    def _save_overmind_config(self, payload: dict) -> None:
        path = self._overmind_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _overmind_public_payload(self, config: dict) -> dict:
        password = str(config.get("overmind_password") or "")
        email = str(config.get("overmind_email") or "")
        status = {
            "configured": bool(config.get("overmind_url")) and bool(email) and bool(password),
            "integration_enabled": bool(config.get("integration_enabled")),
            "integration_state": str(config.get("integration_state") or "not_started"),
            "requested_at": config.get("requested_at"),
            "last_started_at": config.get("last_started_at"),
            "last_error": config.get("last_error"),
            "notes": config.get("notes") or "Stub integration until batocera.overmind app is available.",
        }
        return {
            "overmind_url": config.get("overmind_url") or "",
            "overmind_email": email,
            "password_configured": bool(password),
            "password_masked": self._mask_secret(password) if password else "",
            "status": status,
        }

    def _load_processed_overmind_actions(self) -> List[dict]:
        path = self._overmind_actions_path()
        if not path.exists() or not path.is_file():
            return []
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        return loaded if isinstance(loaded, list) else []

    def _handle_admin_overmind_actions(self) -> None:
        self._send_json(200, {"actions": list(reversed(self._load_processed_overmind_actions()))})

    def _stream_file(self, path: Path, content_type: str, as_attachment: bool = False) -> None:
        file_size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_size))
        self._send_security_headers()
        if as_attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
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
        if cached:
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
        self.image_cache.put(key, data, meta={"content_type": content_type})

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

        if selected_systems:
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
        _, roms = self.repository.list_assets(system, "roms")
        if not self.settings.downloads_enabled:
            for item in roms:
                item["is_downloadable"] = False
        self._send_json(200, {"system": system, "roms": roms}, cache_key=f"json:/systems/{system}")

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
        entries = self.repository.list_bios_entries()
        query_value = (query or "").strip().lower()
        selected_systems = set((s or "").strip().lower() for s in (system_filters or []) if (s or "").strip())

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
                    or query_value in (item.get("md5") or "").lower()
                    or query_value in _entry_system(item)
                )
            ]
        if selected_systems:
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

    def _handle_admin_artwork_missing(self, include_filesystem: bool = False, refresh: bool = False) -> None:
        started_at = time.time()
        items = self.repository.list_missing_artwork(include_filesystem=include_filesystem, force_refresh=refresh)
        systems = sorted({str(item.get("system") or "") for item in items if item.get("system")})
        field_counts = {field: 0 for field in ARTWORK_FIELDS}
        for item in items:
            for field in item.get("missing") or []:
                if field in field_counts:
                    field_counts[field] += 1
        self._send_json(
            200,
            {
                "roms": items,
                "count": len(items),
                "systems": systems,
                "fields": list(ARTWORK_FIELDS),
                "field_counts": field_counts,
                "mode": "filesystem" if include_filesystem else "gamelist",
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
        if not system:
            raise ValueError("system is required")
        if not rom_id and not rom_path:
            raise ValueError("rom_id or rom_path is required")
        if not game_key:
            raise ValueError("game_key is required")
        client = LaunchBoxClient()
        result = self.repository.apply_launchbox_artwork(system, rom_id, game_key, client, rom_path=rom_path or None)
        with self.repository._search_cache_lock:
            self.repository._search_index_expires_at = 0
        with self.repository._missing_artwork_cache_lock:
            self.repository._missing_artwork_cache.clear()
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
                target_path = (asset_dir / item["name"]).resolve()
                is_downloadable = item.get("is_downloadable", True)
                break

        if not target_path or not target_path.exists():
            raise FileNotFoundError()
        if not is_downloadable:
            raise ValueError("asset is not downloadable")
        if not target_path.is_file():
            raise ValueError("not a file")

        self._stream_file(target_path, "application/octet-stream", as_attachment=True)

    def _handle_image_file_or_download(self, system: str, image_ref: str) -> None:
        if self.settings.use_fake_data:
            self._redirect_to_fake_image(seed=f"{system}-{image_ref}", width=640, height=360)
            return
        system_dir = self.repository.get_system_dir(system)
        image_ref = valid_segment(image_ref)
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
            # Use tail to get the last N lines
            result = subprocess.run(
                ["tail", "-n", str(safe_lines), str(log_path)],
                capture_output=True,
                text=True,
                check=True
            )
            log_content = result.stdout.strip()
            self._send_json(200, {
                "source": normalized_source,
                "path": str(log_path),
                "lines": safe_lines,
                "content": log_content.split('\n') if log_content else []
            })
        except subprocess.CalledProcessError as e:
            self._send_json(500, {"error": f"Failed to read log: {str(e)}"})
        except Exception as e:
            self._send_json(500, {"error": f"Internal error: {str(e)}"})

    def _handle_admin_system_info(self) -> None:
        import subprocess

        if self.settings.use_fake_data:
            entries = [
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
                {"key": "Battery", "value": "N/A"},
            ]
            fields = {
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
                "battery": "N/A",
            }
            raw = "\n".join(f"{item['key']}: {item['value']}" for item in entries)
            self._send_json(
                200,
                {
                    "raw": raw,
                    "lines": raw.splitlines(),
                    "entries": entries,
                    "fields": fields,
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
                if key_lower == "model":
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
                elif key_lower == "battery":
                    fields["battery"] = value

            self._send_json(
                200,
                {
                    "raw": raw,
                    "lines": lines,
                    "entries": entries,
                    "fields": fields,
                },
            )
        except Exception as error:
            self._send_json(500, {"error": f"Failed to run batocera-info: {str(error)}"})

    def _handle_admin_overmind_status(self) -> None:
        config = self._load_overmind_config()
        self._send_json(200, self._overmind_public_payload(config))

    def _handle_admin_overmind_config(self, payload: dict) -> None:
        raw_url = str(payload.get("overmind_url") or "").strip()
        raw_email = str(payload.get("overmind_email") or "").strip()
        raw_password = payload.get("overmind_password")

        if not raw_url:
            raise ValueError("overmind_url is required")
        parsed = urlparse(raw_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("overmind_url must be a valid http/https URL")
        if not raw_email:
            raise ValueError("overmind_email is required")
        if "@" not in raw_email or raw_email.startswith("@") or raw_email.endswith("@"):
            raise ValueError("overmind_email must be a valid email address")

        existing = self._load_overmind_config()
        new_config = dict(existing)
        new_config["overmind_url"] = raw_url.rstrip("/")
        new_config["overmind_email"] = raw_email
        if raw_password is not None:
            password_value = str(raw_password)
            if not password_value:
                raise ValueError("overmind_password cannot be empty when provided")
            new_config["overmind_password"] = password_value
        new_config["requested_at"] = self._now_iso()
        new_config["integration_state"] = "configured"
        new_config["last_error"] = None
        new_config["notes"] = "Stub configuration saved. Start Integration to simulate handshake."
        self._save_overmind_config(new_config)
        self._send_json(200, self._overmind_public_payload(new_config))

    def _handle_admin_overmind_start(self, payload: dict) -> None:
        config = self._load_overmind_config()
        email = str(config.get("overmind_email") or "").strip()
        password = str(config.get("overmind_password") or "")
        if not str(config.get("overmind_url") or "").strip():
            raise ValueError("overmind_url is not configured")
        if not email:
            raise ValueError("overmind_email is not configured")
        if not password:
            raise ValueError("overmind_password is not configured")

        if "overmind_password" in payload:
            supplied = str(payload.get("overmind_password") or "")
            if not supplied:
                raise ValueError("overmind_password cannot be empty")
            config["overmind_password"] = supplied
            password = supplied

        config["integration_enabled"] = True
        config["integration_state"] = "stub_connected"
        config["last_started_at"] = self._now_iso()
        config["last_error"] = None
        config["notes"] = (
            "Stub integration active. This simulates a successful handshake with batocera.overmind. "
            "Replace with real client calls when overmind API is available."
        )
        self._save_overmind_config(config)
        self._send_json(200, self._overmind_public_payload(config))

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
            "mame": ["/userdata/system/configs/mame/mame.ini"],
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
                    raw_text = source_path.read_text(encoding="utf-8", errors="replace")
                except Exception as error:
                    self._send_json(500, {"error": f"Failed to read config: {str(error)}"})
                    return
                lines = raw_text.splitlines()
                self._send_json(
                    200,
                    {
                        "source": normalized_source,
                        "path": str(source_path),
                        "type": "xml",
                        "format": "xml",
                        "max_bytes": safe_max_bytes,
                        "truncated": False,
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

            raw = selected_path.read_bytes()
            truncated = False
            if len(raw) > safe_max_bytes:
                raw = raw[-safe_max_bytes:]
                truncated = True
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


def _overmind_config_path_for_settings(settings: Settings) -> Path:
    return (settings.userdata_root / "system" / "rom-api" / "overmind_integration.json").resolve()


def _overmind_actions_path_for_settings(settings: Settings) -> Path:
    return (settings.userdata_root / "system" / "rom-api" / "overmind_processed_actions.json").resolve()


def _load_overmind_config_for_settings(settings: Settings) -> dict:
    fake_email = FAKE_OVERMIND_EMAIL if settings.use_fake_data else ""
    fake_password = FAKE_OVERMIND_PASSWORD if settings.use_fake_data else ""
    default = {
        "overmind_url": (settings.overmind_url or "").strip(),
        "overmind_email": (fake_email if settings.use_fake_data else settings.overmind_email or "").strip(),
        "overmind_password": fake_password if settings.use_fake_data else settings.overmind_password or "",
        "integration_enabled": bool(settings.overmind_url and (settings.overmind_email or fake_email) and (settings.overmind_password or fake_password)),
    }
    path = _overmind_config_path_for_settings(settings)
    if not path.exists() or not path.is_file():
        return default
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    if not isinstance(loaded, dict):
        return default
    merged = dict(default)
    merged.update(loaded)
    if settings.use_fake_data:
        merged["overmind_email"] = FAKE_OVERMIND_EMAIL
        merged["overmind_password"] = FAKE_OVERMIND_PASSWORD
    return merged


def _record_processed_overmind_action(settings: Settings, action: dict, status_value: str, message: str) -> None:
    path = _overmind_actions_path_for_settings(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except Exception:
        loaded = []
    actions = loaded if isinstance(loaded, list) else []
    actions.append(
        {
            "id": action.get("id"),
            "device_id": settings.overmind_device_id,
            "action": action.get("action"),
            "status": status_value,
            "message": message,
            "processed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "fake_data": settings.use_fake_data,
        }
    )
    path.write_text(json.dumps(actions[-200:], indent=2, sort_keys=True), encoding="utf-8")


def _overmind_post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    context = ssl._create_unverified_context() if url.startswith("https://") else None
    with urlopen(request, timeout=10, context=context) as response:
        raw = response.read()
    if not raw:
        return {}
    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _execute_overmind_action(settings: Settings, action: dict) -> Tuple[str, str]:
    action_name = str(action.get("action") or "").strip().lower()
    if settings.use_fake_data:
        return "completed", f"Simulated {action_name} action because USE_FAKE_DATA is enabled."

    if action_name == "shutdown":
        if not shutil.which("shutdown"):
            return "failed", "shutdown command was not found"
        subprocess.Popen(["shutdown", "-h", "now"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "completed", "Shutdown command issued."

    if action_name == "restart":
        if shutil.which("reboot"):
            subprocess.Popen(["reboot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "completed", "Reboot command issued."
        if shutil.which("shutdown"):
            subprocess.Popen(["shutdown", "-r", "now"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "completed", "Restart command issued."
        return "failed", "reboot/shutdown command was not found"

    if action_name == "update":
        updater = shutil.which("batocera-upgrade")
        if not updater:
            return "failed", "batocera-upgrade command was not found"
        subprocess.Popen([updater], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "completed", "Batocera update command issued."

    return "failed", f"Unsupported action: {action_name}"


def _start_overmind_action_poller(settings: Settings) -> None:
    poll_seconds = max(5, int(settings.overmind_poll_seconds or 15))

    def loop() -> None:
        while True:
            try:
                config = _load_overmind_config_for_settings(settings)
                base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
                email = str(config.get("overmind_email") or "").strip()
                password = str(config.get("overmind_password") or "")
                if not base_url or not email or not password:
                    time.sleep(poll_seconds)
                    continue

                device_id = quote(settings.overmind_device_id, safe="")
                credentials = {"email": email, "password": password}
                claim_url = f"{base_url}/api/devices/{device_id}/actions/claim"
                response = _overmind_post_json(claim_url, credentials)
                action = response.get("action")
                if not isinstance(action, dict):
                    time.sleep(poll_seconds)
                    continue

                status_value, message = _execute_overmind_action(settings, action)
                _record_processed_overmind_action(settings, action, status_value, message)
                print(
                    f"Processed Overmind action {action.get('action')} ({action.get('id')}): {status_value} - {message}",
                    file=sys.stdout,
                    flush=True,
                )
                action_id = quote(str(action.get("id") or ""), safe="")
                if action_id:
                    complete_url = f"{base_url}/api/devices/{device_id}/actions/{action_id}/complete"
                    _overmind_post_json(
                        complete_url,
                        {**credentials, "status": status_value, "message": message},
                    )
            except HTTPError as error:
                print(f"Overmind action poll failed: {error}", file=sys.stderr, flush=True)
            except URLError:
                pass
            except (TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
                print(f"Overmind action poll failed: {error}", file=sys.stderr, flush=True)
            except Exception as error:
                print(f"Overmind action poll unexpected error: {error}", file=sys.stderr, flush=True)
            time.sleep(poll_seconds)

    thread = Thread(target=loop, name="overmind-action-poller", daemon=True)
    thread.start()


def create_server(settings: Settings) -> ThreadingHTTPServer:
    repository = RomRepository(
        settings.roms_root,
        settings.bios_root,
        rom_search_cache_ttl_seconds=settings.rom_search_cache_ttl_seconds,
    )
    auth = BasicAuth(settings.username, settings.password)

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

    handler_factory = _build_handler(
        settings=settings,
        auth=auth,
        repository=repository,
        image_cache=image_cache,
        image_miss_cache=image_miss_cache,
        json_cache=json_cache,
    )

    server = ThreadingHTTPServer(("0.0.0.0", settings.https_port), handler_factory)

    if not settings.http_only:
        cert_file, key_file = _resolve_tls_material(settings)
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)

    return server


def main() -> None:
    settings = Settings.from_env()
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
    print(f"Auth username: {settings.username}")
    scheme = "http" if settings.http_only else "https"
    print(f"Serving ROM API on {scheme}://0.0.0.0:{settings.https_port}")
    _start_overmind_action_poller(settings)
    server.serve_forever()


if __name__ == "__main__":
    main()
