"""External metadata scraper clients (LaunchBox, TheGamesDB, MobyGames).

Extracted from ``drone_api.py``. These are read-only HTTP clients the Drone uses
to look up box art / metadata for ROMs, plus the small title/platform
normalization helpers used to match a local ROM to a remote entry. Self-contained
(stdlib + these helpers). ``drone_api`` re-exports them; ``_clean_rom_title`` is
also used by the ROM scanner, so it is re-exported too.
"""

import html
import json
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

try:
    from .gamelist import _first_metadata_value, _looks_like_placeholder_image
except ImportError:  # pragma: no cover - supports direct module execution in legacy paths
    from gamelist import _first_metadata_value, _looks_like_placeholder_image  # type: ignore


LAUNCHBOX_API_BASE = "https://gamesdb-api.launchbox-app.com/api"
LAUNCHBOX_API_BASE_FALLBACKS = (
    "https://gamesdb-api.launchbox-app.com/api",
    "https://api.gamesdb.launchbox-app.com/api",
)
LAUNCHBOX_IMAGE_BASE = "https://images.launchbox-app.com"
SCRAPER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


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


class ScraperUnavailableError(RuntimeError):
    """Raised when an optional external scraper cannot be reached."""


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


class LaunchBoxClient:
    def __init__(self, timeout_seconds: int = 15):
        self.timeout_seconds = timeout_seconds
        configured = os.environ.get("LAUNCHBOX_API_BASE") or os.environ.get("DRONE_LAUNCHBOX_API_BASE")
        bases = [configured] if configured else []
        bases.extend(LAUNCHBOX_API_BASE_FALLBACKS)
        self.api_bases = []
        for base in bases:
            normalized = str(base or "").strip().rstrip("/")
            if normalized and normalized not in self.api_bases:
                self.api_bases.append(normalized)

    def _get_json(self, url: str) -> dict:
        request = Request(url, headers={"User-Agent": SCRAPER_USER_AGENT, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise ScraperUnavailableError(f"LaunchBox returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise ScraperUnavailableError("LaunchBox could not be reached from this Drone") from error

    def _get_json_from_bases(self, path: str, query: Optional[str] = None) -> dict:
        last_error: Optional[ScraperUnavailableError] = None
        suffix = f"{path}{query or ''}"
        for base in self.api_bases:
            try:
                return self._get_json(f"{base}{suffix}")
            except ScraperUnavailableError as error:
                last_error = error
        if last_error:
            raise last_error
        raise ScraperUnavailableError("LaunchBox could not be reached from this Drone")

    def search(self, query: str, system: Optional[str] = None, limit: int = 20) -> List[dict]:
        normalized_query = (query or "").strip()
        if not normalized_query:
            return []
        expected_platform = _launchbox_platform_for_system(system)

        def _search_payload(platform: Optional[str]) -> dict:
            query_string = ""
            if platform:
                query_string = f"?platform={quote(platform, safe='')}"
            return self._get_json_from_bases(f"/search/{quote(normalized_query, safe='')}", query_string)

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
        payload = self._get_json_from_bases(f"/games/details/{safe_key}")
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
