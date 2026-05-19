import base64
import html
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import uuid
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


FAKE_OVERMIND_EMAIL = "demo@example.com"
FAKE_OVERMIND_PASSWORD = "DemoPass123"
FAKE_OVERMIND_TOKEN = "demo-local-drone-token"
_OVERMIND_POLLER_STARTED = False
LAUNCHBOX_API_BASE = "https://api.gamesdb.launchbox-app.com/api"
LAUNCHBOX_IMAGE_BASE = "https://images.launchbox-app.com"
SCRAPER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ARTWORK_FIELDS = ("image", "thumbnail", "marquee", "fanart", "boxart")
ARTWORK_DUPLICATE_FILTER = "duplicate_artwork"
OVERMIND_EVENT_TYPES = {
    "gameplay": "gameplay_activity",
    "rom_update": "rom_update",
    "filesystem": "filesystem_event",
    "speed": "speed_sample",
    "peer": "peer_health",
}
PEER_CHECK_TIMEOUT_SECONDS = float(os.environ.get("DRONE_PEER_CHECK_TIMEOUT_SECONDS", "3"))
PEER_CHECK_INTERVAL_SECONDS = int(os.environ.get("DRONE_PEER_CHECK_INTERVAL_SECONDS", "300"))
OVERMIND_SPEED_SAMPLE_SECONDS = int(os.environ.get("OVERMIND_SPEED_SAMPLE_SECONDS", "300"))
OVERMIND_HEARTBEAT_SECONDS = int(os.environ.get("OVERMIND_POLL_SECONDS", "60"))
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


def _machine_id() -> str:
    node = uuid.getnode()
    return ":".join(f"{(node >> shift) & 0xff:02x}" for shift in range(40, -1, -8))


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
    username: Optional[str]
    password: Optional[str]
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
    overmind_auth_token: Optional[str]
    overmind_token: Optional[str]
    overmind_device_id: str
    overmind_poll_seconds: int
    hostname_override: Optional[str]
    drone_cert_file: Path
    drone_key_file: Path
    drone_cert_days: int
    drone_mtls_enabled: bool
    drone_mtls_ca_file: Optional[Path]

    @classmethod
    def from_env(cls) -> "Settings":
        https_port_value = os.environ.get("HTTPS_PORT", os.environ.get("PORT", "8443"))
        cert_value = os.environ.get("TLS_CERT_FILE")
        key_value = os.environ.get("TLS_KEY_FILE")
        use_fake_data = _env_bool(False, "USE_FAKE_DATA")
        userdata_root = Path(os.environ.get("USERDATA_ROOT", "/userdata"))
        default_drone_cert = userdata_root / "system" / "drone-app" / "certs" / "drone.crt"
        default_drone_key = userdata_root / "system" / "drone-app" / "certs" / "drone.key"

        return cls(
            userdata_root=userdata_root,
            roms_root=Path(os.environ.get("ROMS_ROOT", "/userdata/roms")),
            bios_root=Path(os.environ.get("BIOS_ROOT", "/userdata/bios")),
            username=os.environ.get("DRONE_APP_USERNAME") or None,
            password=os.environ.get("DRONE_APP_PASSWORD") or None,
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
            http_only=_env_bool(False, "HTTP_ONLY", "DRONE_APP_HTTP_ONLY"),
            use_fake_data=use_fake_data,
            fake_image_base_url=os.environ.get("FAKE_IMAGE_BASE_URL"),
            overmind_url=os.environ.get("OVERMIND_URL"),
            overmind_email=os.environ.get("OVERMIND_EMAIL"),
            overmind_password=os.environ.get("OVERMIND_PASSWORD"),
            overmind_auth_token=os.environ.get("OVERMIND_AUTH_TOKEN") or os.environ.get("OVERMIND_AUTHORIZATION_TOKEN"),
            overmind_token=os.environ.get("OVERMIND_DRONE_TOKEN"),
            overmind_device_id=os.environ.get("OVERMIND_DEVICE_ID") or os.environ.get("DRONE_DEVICE_ID") or (_fake_machine_id() if use_fake_data else _machine_id()),
            overmind_poll_seconds=OVERMIND_HEARTBEAT_SECONDS,
            hostname_override=(os.environ.get("HOSTNAME_OVERRIDE") or "").strip() or None,
            drone_cert_file=Path(os.environ.get("DRONE_CERT_FILE", os.environ.get("TLS_CERT_FILE", str(default_drone_cert)))),
            drone_key_file=Path(os.environ.get("DRONE_KEY_FILE", os.environ.get("TLS_KEY_FILE", str(default_drone_key)))),
            drone_cert_days=int(os.environ.get("DRONE_CERT_DAYS", "825")),
            drone_mtls_enabled=_env_bool(False, "DRONE_MTLS_ENABLED", "DRONE_TO_DRONE_MTLS_ENABLED"),
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
    def __init__(self, username: Optional[str], password: Optional[str]):
        self.username = username
        self.password = password

    def check(self, header_value: Optional[str]) -> bool:
        if not self.username or not self.password:
            return True
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
        return not str(name or "").strip().lower().endswith(".old")

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

    def _attach_gamelist_to_rom_items(self, system_dir: Path, items: List[dict]) -> List[dict]:
        try:
            _, root = self._read_gamelist(system_dir)
        except Exception:
            root = ET.Element("gameList")
        for item in items:
            rom_file = str(item.get("rom_file") or item.get("name") or "")
            display_name = str(item.get("image_stem") or item.get("name") or "")
            game = self._find_gamelist_entry(root, rom_file, display_name)
            item["rom_path"] = _normalize_gamelist_rom_path(_text_or_empty(game, "path")) if game is not None else rom_file
            item["title"] = _text_or_empty(game, "name") if game is not None else str(item.get("name") or display_name)
            item["existing"] = {field: _text_or_empty(game, field) if game is not None else "" for field in ARTWORK_FIELDS}
            item["gamelist"] = _gamelist_details(game)
            item["has_gamelist_entry"] = game is not None
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
            raise FileNotFoundError(str(self.roms_root))

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
        "title": "Drone App",
        "version": "4.0",
        "description": "Browse and download ROM, image, video, and BIOS assets. Peer API routes can require mTLS. For manual testing use a client certificate/key with curl, for example: curl --cert client.crt --key client.key -k https://drone-host:8443/v1/api/peer/health. The admin API page exposes certificate metadata and the public certificate only; private key material must stay on the Drone.",
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
    },
}

class RomRequestHandler(ApiRoutesMixin, UiRoutesMixin, BaseHTTPRequestHandler):
    server_version = "DroneApp/4.0"
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
        self.send_header("WWW-Authenticate", 'Basic realm="Drone App"')
        self.send_header("Content-Type", "application/json")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(json_bytes({"error": "unauthorized"}))

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

    def _rom_md5_cache_path(self) -> Path:
        return (self.settings.userdata_root / "system" / "drone-app" / "rom_md5_cache.json").resolve()

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
            merged["overmind_token"] = FAKE_OVERMIND_TOKEN
        else:
            _strip_fake_overmind_values(merged)
        return merged

    def _save_overmind_config(self, payload: dict) -> None:
        path = self._overmind_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _load_json_file(self, path: Path, fallback):
        try:
            if path.exists() and path.is_file():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                return loaded if loaded is not None else fallback
        except Exception:
            pass
        return fallback

    def _overmind_public_payload(self, config: dict) -> dict:
        password = str(config.get("overmind_password") or "")
        auth_token = str(config.get("overmind_auth_token") or "")
        token = str(config.get("overmind_token") or "")
        email = str(config.get("overmind_email") or "")
        status = {
            "configured": bool(config.get("overmind_url")) and bool(token or auth_token),
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
        path = self._overmind_actions_path()
        if not path.exists() or not path.is_file():
            return []
        try:
            loaded = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except Exception:
            return []
        return loaded if isinstance(loaded, list) else []

    def _handle_admin_overmind_actions(self) -> None:
        self._send_json(200, {"actions": list(reversed(self._load_processed_overmind_actions()))})

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
                    "curl": "curl --cert /path/to/client.crt --key /path/to/client.key -k https://drone-host:8443/v1/api/peer/health",
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

    def _handle_rom_md5(self, system: str, unique_id: str) -> None:
        system_dir = self.repository.get_system_dir(system)
        rom = self.repository.find_rom_by_unique_id(system, unique_id)
        rom_file = str(rom.get("rom_file") or rom.get("name") or "")
        target = (system_dir / rom_file).resolve()
        if not target.exists() or not target.is_file() or (target != system_dir and system_dir not in target.parents):
            raise FileNotFoundError()
        stat = target.stat()
        cache_path = self._rom_md5_cache_path()
        cache = self._load_json_file(cache_path, {})
        key = f"{system}:{unique_id}:{stat.st_size}:{int(stat.st_mtime)}"
        md5_value = cache.get(key) if isinstance(cache, dict) else None
        if not md5_value:
            md5_value = self.repository.build_md5(target)
            cache = {key: md5_value}
            _write_json_file(cache_path, cache)
        self._send_json(200, {"system": system, "unique_id": unique_id, "md5": md5_value, "cached": bool(cache.get(key))})

    def _handle_peer_health(self) -> None:
        if self.settings.drone_mtls_enabled:
            cert = self.connection.getpeercert() if hasattr(self.connection, "getpeercert") else None
            if not cert:
                self._send_json(403, {"error": "client certificate required"})
                return
        self._send_json(
            200,
            {
                "status": "ok",
                "drone_id": self.settings.overmind_device_id,
                "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "mtls": bool(self.settings.drone_mtls_enabled),
            },
        )

    def _handle_peer_rom_download(self, system: str, relative_path: str) -> None:
        if self.settings.drone_mtls_enabled:
            cert = self.connection.getpeercert() if hasattr(self.connection, "getpeercert") else None
            if not cert:
                self._send_json(403, {"error": "client certificate required"})
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

    def _handle_admin_system_info(self) -> None:
        import subprocess

        if self.settings.use_fake_data:
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
                "battery": "N/A",
                "machine_id": self.settings.overmind_device_id,
                "overmind_integrated": "yes" if self._load_overmind_config().get("integration_enabled") else "no",
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
                elif key_lower == "battery":
                    fields["battery"] = value

            overmind_integrated = "yes" if self._load_overmind_config().get("integration_enabled") else "no"
            entries.insert(0, {"key": "Integrated with Overmind", "value": overmind_integrated})
            entries.insert(0, {"key": "Machine ID", "value": self.settings.overmind_device_id})
            fields["machine_id"] = self.settings.overmind_device_id
            fields["overmind_integrated"] = overmind_integrated

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
            overmind_integrated = "yes" if self._load_overmind_config().get("integration_enabled") else "no"
            entries = [
                {"key": "Machine ID", "value": self.settings.overmind_device_id},
                {"key": "Integrated with Overmind", "value": overmind_integrated},
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
                    },
                    "warning": f"Failed to run batocera-info: {str(error)}",
                },
            )

    def _handle_admin_overmind_status(self) -> None:
        config = self._load_overmind_config()
        self._send_json(200, self._overmind_public_payload(config))

    def _handle_admin_overmind_config(self, payload: dict) -> None:
        raw_url = str(payload.get("overmind_url") or "").strip()
        raw_email = str(payload.get("overmind_email") or "").strip()
        raw_password = payload.get("overmind_password")
        raw_auth_token = payload.get("overmind_auth_token")
        raw_token = payload.get("overmind_token")

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
        if raw_auth_token is not None:
            auth_token_value = str(raw_auth_token)
            if not auth_token_value:
                raise ValueError("overmind_auth_token cannot be empty when provided")
            new_config["overmind_auth_token"] = auth_token_value
        if raw_token is not None:
            token_value = str(raw_token)
            if not token_value:
                raise ValueError("overmind_token cannot be empty when provided")
            new_config["overmind_token"] = token_value
        new_config["requested_at"] = self._now_iso()
        new_config["integration_state"] = "configured"
        new_config["last_error"] = None
        new_config["notes"] = "Configuration saved. Drone will report alive and collect Overmind actions on its polling interval."
        if new_config.get("overmind_auth_token"):
            base_url = str(new_config.get("overmind_url") or "").strip().rstrip("/")
            new_config["integration_enabled"] = True
            token = _register_or_claim_overmind_token(self.settings, self.repository, new_config, base_url)
            if token:
                new_config = self._load_overmind_config()
            else:
                new_config["integration_enabled"] = False
        self._save_overmind_config(new_config)
        self._send_json(200, self._overmind_public_payload(new_config))

    def _handle_admin_overmind_start(self, payload: dict) -> None:
        config = self._load_overmind_config()
        email = str(config.get("overmind_email") or "").strip()
        password = str(config.get("overmind_password") or "")
        auth_token = str(config.get("overmind_auth_token") or "")
        token = str(config.get("overmind_token") or "")
        if not str(config.get("overmind_url") or "").strip():
            raise ValueError("overmind_url is not configured")
        if not email:
            raise ValueError("overmind_email is not configured")
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
        config["last_started_at"] = self._now_iso()
        config["last_error"] = None
        config["notes"] = (
            "Integration active. Drone periodically calls Overmind, claims actions, performs local collection, "
            "and posts completion results back to the Overmind API."
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
        if self.settings.hostname_override:
            alt_names.append(f"DNS:{self.settings.hostname_override}")
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


def _write_json_file(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


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
        "overmind_password": fake_password if settings.use_fake_data else settings.overmind_password or "",
        "overmind_auth_token": "" if settings.use_fake_data else settings.overmind_auth_token or "",
        "overmind_token": fake_token if settings.use_fake_data else settings.overmind_token or "",
        "integration_enabled": bool(settings.overmind_url and (settings.overmind_token or settings.overmind_auth_token or fake_token)),
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


def _drone_scheme(settings: Settings) -> str:
    return "http" if settings.http_only else "https"


def _drone_report_host(settings: Settings, network: Optional[dict] = None) -> str:
    if settings.hostname_override:
        return settings.hostname_override
    network = network if isinstance(network, dict) else _get_local_ip_addresses()
    ipv4 = network.get("ipv4") if isinstance(network.get("ipv4"), list) else []
    ipv6 = network.get("ipv6") if isinstance(network.get("ipv6"), list) else []
    if ipv4:
        return str(ipv4[0])
    if ipv6:
        return str(ipv6[0])
    return "127.0.0.1"


def _drone_reachable_url(settings: Settings, network: Optional[dict] = None) -> str:
    host = _drone_report_host(settings, network)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{_drone_scheme(settings)}://{host}:{settings.https_port}"


def _drone_network_payload(settings: Settings) -> dict:
    network = _get_local_ip_addresses()
    network["hostname_override"] = settings.hostname_override or None
    network["reachable_url"] = _drone_reachable_url(settings, network)
    return network


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
    if not (has_known_fake or _mock_userdata_marker(userdata_root).exists()):
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
    path = _overmind_actions_path_for_settings(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    max_bytes = int(os.environ.get("OVERMIND_ACTION_LOG_MAX_BYTES", str(settings.log_max_bytes)))
    if path.exists() and max_bytes > 0 and path.stat().st_size > max_bytes:
        backup = path.with_name(f"{path.name}.1")
        try:
            if backup.exists():
                backup.unlink()
            path.replace(backup)
        except OSError:
            path.unlink(missing_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _peer_cert_cache_dir(settings: Settings) -> Path:
    return (settings.userdata_root / "system" / "drone-app" / "peer-certs").resolve()


def _peer_cert_cache_path(settings: Settings, peer_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", peer_id)
    return _peer_cert_cache_dir(settings) / f"{safe}.crt"


def _peer_cert_meta_path(settings: Settings, peer_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", peer_id)
    return _peer_cert_cache_dir(settings) / f"{safe}.json"


def _drone_client_ssl_context(settings: Settings, url: str, verify: bool = False, cafile: Optional[Path] = None) -> Optional[ssl.SSLContext]:
    if not url.startswith("https://"):
        return None
    context = ssl.create_default_context(cafile=str(cafile) if cafile else None) if verify else ssl._create_unverified_context()
    if verify and cafile:
        context.check_hostname = False
    if settings.drone_mtls_enabled and settings.drone_cert_file.exists() and settings.drone_key_file.exists():
        context.load_cert_chain(certfile=str(settings.drone_cert_file), keyfile=str(settings.drone_key_file))
    return context


def _overmind_post_json(url: str, payload: dict, token: Optional[str] = None, settings: Optional[Settings] = None) -> dict:
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


def _overmind_get_json(url: str, token: Optional[str] = None, settings: Optional[Settings] = None) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    context = _drone_client_ssl_context(settings, url) if settings else (ssl._create_unverified_context() if url.startswith("https://") else None)
    with urlopen(request, timeout=10, context=context) as response:
        raw = response.read()
    parsed = json.loads(raw.decode("utf-8")) if raw else {}
    return parsed if isinstance(parsed, dict) else {}


def _overmind_raw_request(
    url: str,
    token: Optional[str] = None,
    settings: Optional[Settings] = None,
    data: Optional[bytes] = None,
    content_type: str = "application/octet-stream",
) -> bytes:
    headers = {"Accept": "application/octet-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = content_type
    request = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    context = _drone_client_ssl_context(settings, url) if settings else (ssl._create_unverified_context() if url.startswith("https://") else None)
    with urlopen(request, timeout=10, context=context) as response:
        return response.read()


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


def _save_overmind_runtime_config(settings: Settings, config: dict) -> None:
    _write_json_file(_overmind_config_path_for_settings(settings), config)


def _register_or_claim_overmind_token(settings: Settings, repository: "RomRepository", config: dict, base_url: str) -> Optional[str]:
    auth_token = str(config.get("overmind_auth_token") or "").strip()
    email = str(config.get("overmind_email") or "").strip()
    network = _drone_network_payload(settings)
    reachable_url = _drone_reachable_url(settings, network)
    payload = {
        "device_id": settings.overmind_device_id,
        "device_name": socket.gethostname(),
        "api_port": settings.https_port,
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
            "api_port": settings.https_port,
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
    try:
        response = _overmind_post_json(f"{base_url}/api/devices/register", payload, token=auth_token or None, settings=settings)
    except Exception as error:
        config["integration_state"] = "pending_failed"
        config["last_error"] = _format_overmind_error(error)
        _save_overmind_runtime_config(settings, config)
        print(f"Overmind onboarding request failed for {settings.overmind_device_id}: {config['last_error']}", file=sys.stderr, flush=True)
        return None
    if response.get("drone_token"):
        config["overmind_token"] = str(response["drone_token"])
        config["integration_enabled"] = True
        config["integration_state"] = "polling"
        config["last_error"] = None
        config["notes"] = "Drone approved by Overmind and polling is active."
        _save_overmind_runtime_config(settings, config)
        print(f"Overmind onboarding approved for {settings.overmind_device_id}", file=sys.stdout, flush=True)
        return config["overmind_token"]
    config["integration_state"] = "pending_approval"
    config["notes"] = response.get("message") or "Psionic connection detected. Awaiting Overlord approval."
    config["last_error"] = None
    _save_overmind_runtime_config(settings, config)
    return None


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
        _peer_cert_meta_path(settings, peer_id).write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Fetched peer certificate for {peer_id}", file=sys.stdout, flush=True)
        return cert_path
    except Exception as error:
        print(f"Failed to fetch peer certificate for {peer_id}: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
        return None


def _peer_get_json(url: str, settings: Settings, peer_id: Optional[str] = None, config: Optional[dict] = None, refresh_cert: bool = False) -> dict:
    cafile = None
    if peer_id:
        if refresh_cert and config:
            cafile = _fetch_peer_certificate(settings, config, peer_id)
        else:
            cached = _peer_cert_cache_path(settings, peer_id)
            cafile = cached if cached.exists() else (_fetch_peer_certificate(settings, config or {}, peer_id) if config else None)
    if url.startswith("https://") and peer_id and not cafile:
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "batocera-drone-peer/1.0"})
    with urlopen(request, timeout=PEER_CHECK_TIMEOUT_SECONDS, context=_drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)) as response:
        raw = response.read()
    parsed = json.loads(raw.decode("utf-8")) if raw else {}
    return parsed if isinstance(parsed, dict) else {}


def _peer_address(peer: dict) -> Optional[str]:
    reachable_url = str(peer.get("reachable_url") or "").strip().rstrip("/")
    if reachable_url:
        return reachable_url
    scheme = str(peer.get("scheme") or peer.get("protocol") or "https").strip() or "https"
    port = peer.get("api_port") or peer.get("port") or 8443
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
    for key in ("local_ip", "private_ip", "public_ip"):
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
        _peer_get_json(f"{address}/v1/api/peer/health", settings, peer_id=peer_id, config=config)
        result["status"] = "pass"
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
    except ssl.SSLError as error:
        message = str(error)
        if config and any(term in message.lower() for term in ("unknown ca", "certificate", "cert")):
            try:
                _peer_get_json(f"{address}/v1/api/peer/health", settings, peer_id=peer_id, config=config, refresh_cert=True)
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


def _get_local_ip_addresses() -> dict:
    """Resolve local IPv4/IPv6 addresses for Overmind alive pings."""
    ipv4: List[str] = []
    ipv6: List[str] = []

    def add(value: str) -> None:
        value = str(value or "").split("%", 1)[0].strip()
        if not value:
            return
        target = ipv6 if ":" in value else ipv4
        if value not in target:
            target.append(value)

    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            add(info[4][0])
    except OSError as error:
        print(f"Overmind network resolution failed for hostname: {error}", file=sys.stderr, flush=True)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            add(probe.getsockname()[0])
    except OSError as error:
        print(f"Overmind IPv4 route resolution failed: {error}", file=sys.stderr, flush=True)

    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as probe6:
            probe6.connect(("2001:4860:4860::8888", 80))
            add(probe6.getsockname()[0])
    except OSError as error:
        if os.environ.get("DRONE_DEBUG_NETWORK", "").strip().lower() in {"1", "true", "yes", "on"}:
            print(f"Overmind IPv6 route unavailable; skipping IPv6 detection: {error}", file=sys.stderr, flush=True)

    if "127.0.0.1" not in ipv4:
        ipv4.append("127.0.0.1")
    gateway_ip = None
    try:
        result = subprocess.run(["sh", "-c", "ip route show default 2>/dev/null | awk '{print $3; exit}'"], capture_output=True, text=True, timeout=2)
        gateway_ip = (result.stdout or "").strip() or None
    except Exception:
        gateway_ip = None
    public_ip = None
    try:
        with urlopen(Request("https://api.ipify.org", headers={"User-Agent": "batocera-drone-app/4.0"}), timeout=3) as response:
            public_ip = response.read().decode("utf-8", errors="replace").strip() or None
    except Exception:
        public_ip = None
    print(f"Overmind network resolved ipv4={ipv4} ipv6={ipv6} gateway={gateway_ip} public={public_ip}", file=sys.stdout, flush=True)
    return {"ipv4": ipv4, "ipv6": ipv6, "gateway_ip": gateway_ip, "public_ip": public_ip}


def _get_local_certificate_ips() -> List[str]:
    ips = ["127.0.0.1"]
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            value = str(info[4][0] or "").split("%", 1)[0].strip()
            if value and ":" not in value and value not in ips:
                ips.append(value)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            value = probe.getsockname()[0]
            if value and value not in ips:
                ips.append(value)
    except OSError:
        pass
    return ips


def _sample_speed(settings: Settings, base_url: str, token: str) -> dict:
    """Measure lightweight Drone <-> Overmind throughput."""
    sampled_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    size = max(1024, min(int(os.environ.get("OVERMIND_SPEED_SAMPLE_BYTES", "262144")), 2 * 1024 * 1024))
    device_id = quote(settings.overmind_device_id, safe="")
    sample = {
        "upload_mbps": 0,
        "download_mbps": 0,
        "latency_ms": 0,
        "source": "overmind-probe",
        "sampled_at": sampled_at,
        "bytes": size,
    }
    try:
        download_url = f"{base_url}/api/devices/{device_id}/speed/download?bytes={size}"
        started = time.monotonic()
        downloaded = _overmind_raw_request(download_url, token=token, settings=settings)
        elapsed = max(time.monotonic() - started, 0.001)
        sample["download_mbps"] = round((len(downloaded) * 8) / elapsed / 1_000_000, 3)
        sample["latency_ms"] = int(elapsed * 1000)

        upload_url = f"{base_url}/api/devices/{device_id}/speed/upload"
        payload = b"1" * size
        started = time.monotonic()
        _overmind_raw_request(upload_url, token=token, settings=settings, data=payload)
        elapsed = max(time.monotonic() - started, 0.001)
        sample["upload_mbps"] = round((len(payload) * 8) / elapsed / 1_000_000, 3)
    except Exception as error:
        sample["source"] = "overmind-probe-failed"
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
    return {
        "hostname": hostname,
        "device_name": hostname,
        "platform": sys.platform,
        "os": os.uname().sysname if hasattr(os, "uname") else sys.platform,
        "os_release": os.uname().release if hasattr(os, "uname") else "",
        "batocera_version": batocera_version,
        "drone_app_version": OPENAPI_SPEC.get("info", {}).get("version"),
        "architecture": os.uname().machine if hasattr(os, "uname") else "",
        "cpu": {"model": os.environ.get("DRONE_CPU_MODEL", ""), "count": os.cpu_count()},
        "memory": memory,
        "disk": disk,
        "gpu": _collect_gpu_info(),
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
    try:
        systems = repository.list_systems()
    except FileNotFoundError:
        systems = []
    roms = []
    gamelists = []
    for system in systems:
        system_name = str(system.get("name") or "").strip()
        if not system_name:
            continue
        try:
            _, system_roms = repository.list_assets(system_name, "roms")
        except Exception as error:
            roms.append({"system": system_name, "error": str(error)})
            continue
        for rom in system_roms:
            item = dict(rom)
            item["system"] = system_name
            roms.append(item)
        gamelist_path = settings.roms_root / system_name / "gamelist.xml"
        if gamelist_path.exists() and gamelist_path.is_file():
            gamelist = _read_text_file(gamelist_path, max_bytes=524288)
            gamelist["system"] = system_name
            gamelists.append(gamelist)
    result = {
        "type": "rom_metadata",
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "roms_root": str(settings.roms_root),
        "systems": systems,
        "roms": roms,
        "gamelists": gamelists,
    }
    print(
        f"ROM metadata scan root={settings.roms_root} systems={len(systems)} roms={len(roms)} gamelists={len(gamelists)}",
        file=sys.stdout,
        flush=True,
    )
    return result


def _collect_log_sources(settings: Settings) -> dict:
    candidates = {
        "es_launch_stdout": ["/userdata/system/logs/es_launch_stdout.log"],
        "es_launch_stderr": ["/userdata/system/logs/es_launch_stderr.log"],
        "drone_stdout": [str((settings.log_dir / settings.stdout_log_file).resolve())],
        "drone_stderr": [str((settings.log_dir / settings.stderr_log_file).resolve())],
    }
    logs = []
    for source, paths in candidates.items():
        entry = {"source": source, "files": []}
        for raw_path in paths:
            path = _resolve_userdata_path(settings, raw_path)
            if path.exists() and path.is_file():
                entry["files"].append(_read_text_file(path, max_bytes=262144))
        logs.append(entry)
    return {
        "type": "log_sources",
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "logs": logs,
    }


def _filesystem_watch_roots(settings: Settings) -> List[Path]:
    return [
        settings.roms_root,
        settings.userdata_root / "system" / "configs",
        settings.userdata_root / "system" / "logs",
        settings.log_dir,
    ]


def _filesystem_snapshot(settings: Settings, max_files: int = 5000) -> Dict[str, dict]:
    snapshot: Dict[str, dict] = {}
    checked = 0
    for root in _filesystem_watch_roots(settings):
        if not root.exists():
            continue
        try:
            for path in root.rglob("*"):
                checked += 1
                if checked > max_files:
                    return snapshot
                if not path.is_file():
                    continue
                stat = path.stat()
                snapshot[str(path.resolve())] = {"size": stat.st_size, "mtime": int(stat.st_mtime)}
        except Exception:
            continue
    return snapshot


def _filesystem_events(settings: Settings, previous: Dict[str, dict], current: Dict[str, dict]) -> List[dict]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    events = []
    for path, meta in current.items():
        old = previous.get(path)
        if not old:
            action = "create"
        elif old != meta:
            action = "update"
        else:
            continue
        events.append({
            "drone_id": settings.overmind_device_id,
            "event_type": OVERMIND_EVENT_TYPES["filesystem"],
            "timestamp": now,
            "path": path,
            "metadata": {"action": action, **meta, "old": old},
        })
    for path, old in previous.items():
        if path not in current:
            events.append({
                "drone_id": settings.overmind_device_id,
                "event_type": OVERMIND_EVENT_TYPES["filesystem"],
                "timestamp": now,
                "path": path,
                "metadata": {"action": "delete", "old": old},
            })
    return events[:100]


def _collect_game_logs(settings: Settings) -> dict:
    log_data = _collect_log_sources(settings)
    sessions = []
    for source in log_data.get("logs", []):
        if source.get("source") != "es_launch_stdout":
            continue
        for file_info in source.get("files", []):
            current = {}
            for line in str(file_info.get("content") or "").splitlines():
                lowered = line.lower()
                if "emulator=" in lowered:
                    current["raw_emulator_line"] = line
                    match = re.search(r"emulator=([^\\s]+)", line, re.IGNORECASE)
                    if match:
                        current["system_name"] = match.group(1)
                if "rom=" in lowered:
                    current["raw_rom_line"] = line
                    match = re.search(r"rom=(.+)$", line, re.IGNORECASE)
                    if match:
                        current["game_name"] = match.group(1).strip()
                    if current:
                        sessions.append(dict(current))
                        current = {}
    return {
        "type": "game_logs",
        "collected_at": log_data["collected_at"],
        "sessions": sessions,
        "logs": log_data.get("logs", []),
    }


def _collect_emulator_configs(settings: Settings) -> dict:
    roots = [
        settings.userdata_root / "system" / "configs",
        settings.userdata_root / "system" / ".config",
    ]
    allowed_suffixes = {".cfg", ".conf", ".ini", ".json", ".toml", ".xml", ".yml", ".yaml", ".bml", ".reg"}
    configs = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if len(configs) >= 250:
                break
            if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
                continue
            item = _read_text_file(path, max_bytes=131072)
            try:
                item["relative_path"] = str(path.relative_to(root))
            except Exception:
                item["relative_path"] = path.name
            item["root"] = str(root)
            configs.append(item)
    return {
        "type": "emulator_configs",
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "configs": configs,
    }


def _safe_rom_relative_path(value: str) -> str:
    rel = str(value or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in Path(rel).parts:
        raise ValueError("invalid rom path")
    return rel


def _rom_exists(repository: "RomRepository", system: str, relative_path: str) -> bool:
    try:
        system_dir = repository.get_system_dir(system).resolve()
        target = (system_dir / _safe_rom_relative_path(relative_path)).resolve()
        return target.exists() and target.is_file() and (target == system_dir or system_dir in target.parents)
    except Exception:
        return False


def _best_peer_for_rom(settings: Settings, repository: "RomRepository", config: dict, system: str, relative_path: str) -> Optional[dict]:
    swarm = _read_json_file(_overmind_swarm_path_for_settings(settings), [])
    peer_checks = _read_json_file(_overmind_peer_results_path_for_settings(settings), [])
    checks = {str(row.get("target_drone_id") or ""): row for row in peer_checks if isinstance(row, dict)}
    candidates = []
    for peer in swarm if isinstance(swarm, list) else []:
        peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
        if not peer_id or peer_id == settings.overmind_device_id or not peer.get("online", True):
            continue
        systems = peer.get("rom_systems") or peer.get("systems") or []
        system_names = {str(item.get("name") if isinstance(item, dict) else item).lower() for item in systems}
        if system_names and system.lower() not in system_names:
            continue
        check = checks.get(peer_id) or {}
        if check.get("status") == "fail":
            continue
        score = 0
        sample = peer.get("last_speed_sample") or {}
        try:
            score += float(sample.get("upload_mbps") or 0)
        except Exception:
            pass
        if check.get("status") == "pass":
            score += 1000
        candidates.append((score, peer_id, peer))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2] if candidates else None


def _download_rom_from_peer(settings: Settings, config: dict, peer: dict, system: str, relative_path: str, expected_size=None) -> dict:
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
    address = _peer_address(peer)
    if not address:
        raise RuntimeError("selected peer has no address")
    rel = _safe_rom_relative_path(relative_path)
    url = f"{address}/v1/api/peer/roms/{quote(system, safe='')}/{quote(rel, safe='/')}"
    target = (settings.roms_root / system / rel).resolve()
    system_dir = (settings.roms_root / system).resolve()
    if target.exists():
        raise FileExistsError("ROM already exists locally")
    if system_dir not in target.parents:
        raise ValueError("invalid target path")
    target.parent.mkdir(parents=True, exist_ok=True)
    cafile = _peer_cert_cache_path(settings, peer_id)
    if not cafile.exists():
        _fetch_peer_certificate(settings, config, peer_id)
    if address.startswith("https://") and not cafile.exists():
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    context = _drone_client_ssl_context(settings, url, verify=cafile.exists(), cafile=cafile if cafile.exists() else None)
    started = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    bytes_written = 0
    request = Request(url, headers={"User-Agent": "batocera-drone-rom-sync/1.0"})
    try:
        with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, target.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                bytes_written += len(chunk)
    except ssl.SSLError:
        _fetch_peer_certificate(settings, config, peer_id)
        if target.exists():
            target.unlink()
        cafile = _peer_cert_cache_path(settings, peer_id)
        context = _drone_client_ssl_context(settings, url, verify=cafile.exists(), cafile=cafile if cafile.exists() else None)
        bytes_written = 0
        with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, target.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                bytes_written += len(chunk)
    if expected_size not in (None, ""):
        try:
            if int(expected_size) != bytes_written:
                raise RuntimeError(f"size mismatch expected={expected_size} actual={bytes_written}")
        except ValueError:
            pass
    return {
        "source_drone_id": peer_id,
        "target_drone_id": settings.overmind_device_id,
        "system": system,
        "rom_name": rel,
        "action": "download",
        "status": "completed",
        "bytes_transferred": bytes_written,
        "file_size": expected_size or bytes_written,
        "started_at": started,
        "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "selected_peer_reason": "healthy peer with requested system and best sampled score",
    }


def _summarize_overmind_result(result: Optional[dict]) -> str:
    if not isinstance(result, dict):
        return ""
    if result.get("type") == "rom_metadata":
        return f"{len(result.get('systems') or [])} systems, {len(result.get('roms') or [])} ROMs, {len(result.get('gamelists') or [])} gamelists"
    if result.get("type") == "game_logs":
        return f"{len(result.get('sessions') or [])} parsed sessions, {len(result.get('logs') or [])} logs"
    if result.get("type") == "emulator_configs":
        return f"{len(result.get('configs') or [])} config files"
    if result.get("type") == "log_sources":
        return f"{len(result.get('logs') or [])} log sources"
    return "data returned"


def _execute_overmind_action(settings: Settings, repository: "RomRepository", action: dict) -> Tuple[str, str, Optional[dict]]:
    action_name = str(action.get("action") or "").strip().lower()

    if action_name == "collect_rom_metadata":
        result = _collect_rom_metadata(settings, repository)
        return "completed", f"Collected {_summarize_overmind_result(result)}.", result

    if action_name == "collect_game_logs":
        result = _collect_game_logs(settings)
        return "completed", f"Collected {_summarize_overmind_result(result)}.", result

    if action_name == "collect_emulator_configs":
        result = _collect_emulator_configs(settings)
        return "completed", f"Collected {_summarize_overmind_result(result)}.", result

    if action_name == "collect_log_sources":
        result = _collect_log_sources(settings)
        return "completed", f"Collected {_summarize_overmind_result(result)}.", result

    if action_name in {"sync_rom", "sync_system"}:
        config = _load_overmind_config_for_settings(settings)
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
            if not system or not rel:
                continue
            sync_id = str(uuid.uuid4())
            if _rom_exists(repository, system, rel):
                activities.append({"sync_id": sync_id, "target_drone_id": settings.overmind_device_id, "system": system, "rom_name": rel, "action": "download", "status": "skipped", "failure_reason": "ROM already exists locally"})
                continue
            peer = _best_peer_for_rom(settings, repository, config, system, rel)
            if not peer:
                failures += 1
                activities.append({"sync_id": sync_id, "target_drone_id": settings.overmind_device_id, "system": system, "rom_name": rel, "action": "download", "status": "failed", "failure_reason": "No healthy source peer found"})
                continue
            try:
                activity = _download_rom_from_peer(settings, config, peer, system, rel, expected_size=item.get("file_size"))
                activity["sync_id"] = sync_id
                activity["rom_md5"] = item.get("rom_md5")
                activities.append(activity)
            except Exception as error:
                failures += 1
                activities.append({
                    "sync_id": sync_id,
                    "source_drone_id": str(peer.get("drone_id") or peer.get("device_id") or ""),
                    "target_drone_id": settings.overmind_device_id,
                    "system": system,
                    "rom_name": rel,
                    "action": "download",
                    "status": "failed",
                    "failure_reason": str(error),
                })
        result = {"type": "rom_sync", "activity": activities}
        if failures and failures == len(activities):
            return "failed", f"ROM sync failed for {failures} item(s).", result
        return "completed", f"ROM sync processed {len(activities)} item(s) with {failures} failure(s).", result

    if action_name == "shutdown":
        if settings.use_fake_data:
            return "completed", "Simulated shutdown action because USE_FAKE_DATA is enabled.", None
        if not shutil.which("shutdown"):
            return "failed", "shutdown command was not found", None
        subprocess.Popen(["shutdown", "-h", "now"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "completed", "Shutdown command issued.", None

    if action_name == "restart":
        if settings.use_fake_data:
            return "completed", "Simulated restart action because USE_FAKE_DATA is enabled.", None
        if shutil.which("reboot"):
            subprocess.Popen(["reboot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "completed", "Reboot command issued.", None
        if shutil.which("shutdown"):
            subprocess.Popen(["shutdown", "-r", "now"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "completed", "Restart command issued.", None
        return "failed", "reboot/shutdown command was not found", None

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
    system_info_refresh_seconds = max(300, int(os.environ.get("DRONE_SYSTEM_INFO_REFRESH_SECONDS", "3600")))
    last_speed_sample_at = -float(speed_sample_seconds)
    last_peer_check_at = -float(PEER_CHECK_INTERVAL_SECONDS)
    last_system_info_at = -float(system_info_refresh_seconds)
    last_missing_roms_warning_at = -float(3600)
    system_info_payload: dict = {}
    rom_metadata_payload: dict = {}
    fs_snapshot = _filesystem_snapshot(settings)

    def loop() -> None:
        nonlocal last_speed_sample_at, last_peer_check_at, last_system_info_at, last_missing_roms_warning_at, system_info_payload, rom_metadata_payload, fs_snapshot
        while True:
            try:
                config = _load_overmind_config_for_settings(settings)
                base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
                token = str(config.get("overmind_token") or "").strip()
                if not base_url:
                    time.sleep(poll_seconds)
                    continue
                if not token:
                    token = _register_or_claim_overmind_token(settings, repository, config, base_url) or ""
                    if not token:
                        time.sleep(poll_seconds)
                        continue

                device_id = quote(settings.overmind_device_id, safe="")
                now = time.monotonic()
                try:
                    rom_metadata_payload = _collect_rom_metadata(settings, repository)
                    rom_systems = rom_metadata_payload.get("systems") or []
                except FileNotFoundError as error:
                    rom_metadata_payload = {
                        "type": "rom_metadata",
                        "roms_root": str(settings.roms_root),
                        "systems": [],
                        "roms": [],
                        "gamelists": [],
                        "error": str(error),
                    }
                    rom_systems = []
                    if now - last_missing_roms_warning_at >= 3600:
                        print(
                            f"Overmind action poll warning: local ROM root unavailable; reporting no ROM systems ({_format_overmind_error(error)})",
                            file=sys.stderr,
                            flush=True,
                        )
                        last_missing_roms_warning_at = now
                if not system_info_payload or now - last_system_info_at >= system_info_refresh_seconds:
                    system_info_payload = _collect_system_info_payload(settings)
                    last_system_info_at = now
                network_payload = _drone_network_payload(settings)
                alive_payload = {
                    "device_id": settings.overmind_device_id,
                    "network": network_payload,
                    "rom_systems": rom_systems,
                    "api_port": settings.https_port,
                    "scheme": _drone_scheme(settings),
                    "reachable_url": _drone_reachable_url(settings, network_payload),
                    "certificate": DroneCertificateManager(settings).metadata(),
                    "system_info": system_info_payload,
                    "rom_metadata": rom_metadata_payload,
                }
                alive_url = f"{base_url}/api/devices/{device_id}/alive"
                response = _overmind_post_json(alive_url, alive_payload, token=token, settings=settings)
                print(
                    f"ROM metadata sent to Overmind for {settings.overmind_device_id}: root={rom_metadata_payload.get('roms_root')} systems={len(rom_metadata_payload.get('systems') or [])} roms={len(rom_metadata_payload.get('roms') or [])}",
                    file=sys.stdout,
                    flush=True,
                )
                swarm = response.get("swarm") if isinstance(response.get("swarm"), list) else []
                _write_json_file(_overmind_swarm_path_for_settings(settings), swarm)

                if speed_sample_seconds > 0 and now - last_speed_sample_at >= speed_sample_seconds:
                    speed_url = f"{base_url}/api/devices/{device_id}/speed"
                    speed_sample = _sample_speed(settings, base_url, token)
                    try:
                        _overmind_post_json(speed_url, speed_sample, token=token, settings=settings)
                        print(f"Speed sample sent to Overmind for {settings.overmind_device_id}", file=sys.stdout, flush=True)
                    except Exception as error:
                        print(f"Speed sample failed for {settings.overmind_device_id}: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
                        raise
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
                    last_speed_sample_at = now

                if swarm and now - last_peer_check_at >= PEER_CHECK_INTERVAL_SECONDS:
                    peer_results = []
                    for peer in swarm:
                        peer_id = str(peer.get("drone_id") or peer.get("device_id") or peer.get("id") or "")
                        if not peer_id or peer_id == settings.overmind_device_id:
                            continue
                        peer_results.append(_check_peer(settings, peer, config=config))
                    if peer_results:
                        _write_json_file(_overmind_peer_results_path_for_settings(settings), peer_results)
                        _overmind_post_json(
                            f"{base_url}/api/devices/{device_id}/peer-checks",
                            {"results": peer_results},
                            token=token,
                            settings=settings,
                        )
                    last_peer_check_at = now

                next_fs_snapshot = _filesystem_snapshot(settings)
                for event in _filesystem_events(settings, fs_snapshot, next_fs_snapshot):
                    print(f"Filesystem event: {event.get('metadata', {}).get('action')} {event.get('path')}", file=sys.stdout, flush=True)
                    _overmind_post_json(f"{base_url}/api/devices/{device_id}/events", event, token=token, settings=settings)
                fs_snapshot = next_fs_snapshot

                action = response.get("action")
                if not isinstance(action, dict):
                    time.sleep(poll_seconds)
                    continue

                status_value, message, result = _execute_overmind_action(settings, repository, action)
                _record_processed_overmind_action(settings, action, status_value, message, result)
                print(
                    f"Processed Overmind action {action.get('action')} ({action.get('id')}): {status_value} - {message}",
                    file=sys.stdout,
                    flush=True,
                )
                action_id = quote(str(action.get("id") or ""), safe="")
                if action_id:
                    complete_url = f"{base_url}/api/devices/{device_id}/actions/{action_id}/complete"
                    completion_payload = {"status": status_value, "message": message}
                    if result is not None:
                        completion_payload["result"] = result
                    _overmind_post_json(complete_url, completion_payload, token=token, settings=settings)
            except (HTTPError, URLError) as error:
                print(f"Overmind action poll failed: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
            except (TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
                print(f"Overmind action poll failed: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
            except Exception as error:
                print(f"Overmind action poll unexpected error: {_format_overmind_error(error)}", file=sys.stderr, flush=True)
            time.sleep(poll_seconds)

    thread = Thread(target=loop, name="overmind-action-poller", daemon=True)
    thread.start()


def create_server(settings: Settings) -> ThreadingHTTPServer:
    global _OVERMIND_POLLER_STARTED
    roms_root, bios_root = _real_data_roots(settings)
    repository = RomRepository(
        roms_root,
        bios_root,
        rom_search_cache_ttl_seconds=settings.rom_search_cache_ttl_seconds,
    )
    auth = BasicAuth(settings.username, settings.password)
    cert_state = DroneCertificateManager(settings).ensure_certificate()
    if cert_state.get("error"):
        print(f"Drone certificate setup: {cert_state.get('error')}", file=sys.stderr, flush=True)

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
        if settings.drone_cert_file.exists() and settings.drone_key_file.exists():
            cert_file, key_file = settings.drone_cert_file, settings.drone_key_file
        else:
            cert_file, key_file = _resolve_tls_material(settings)
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
        if settings.drone_mtls_enabled:
            ssl_context.verify_mode = ssl.CERT_OPTIONAL
            if settings.drone_mtls_ca_file and settings.drone_mtls_ca_file.exists():
                ssl_context.load_verify_locations(cafile=str(settings.drone_mtls_ca_file))
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)

    if not _OVERMIND_POLLER_STARTED:
        _start_overmind_action_poller(settings, repository)
        _OVERMIND_POLLER_STARTED = True

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
    print(f"Serving Drone App on {scheme}://0.0.0.0:{settings.https_port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
