import base64
import hashlib
import json
import os
import ssl
import subprocess
import time
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Dict, Iterable, List, Optional, Tuple


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable must be set")
    return value


@dataclass(frozen=True)
class Settings:
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

    @classmethod
    def from_env(cls) -> "Settings":
        https_port_value = os.environ.get("HTTPS_PORT", os.environ.get("PORT", "8443"))
        cert_value = os.environ.get("TLS_CERT_FILE")
        key_value = os.environ.get("TLS_KEY_FILE")

        return cls(
            roms_root=Path(os.environ.get("ROMS_ROOT", "/userdata/roms")),
            bios_root=Path(os.environ.get("BIOS_ROOT", "/userdata/bios")),
            username=_require_env("USERNAME"),
            password=_require_env("PASSWORD"),
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


class RomRepository:
    def __init__(self, roms_root: Path, bios_root: Path):
        self.roms_root = roms_root
        self.bios_root = bios_root

    @staticmethod
    def should_include_system(name: str) -> bool:
        if "." in name:
            return False
        if name.lower() == "steam":
            return False
        return True

    @staticmethod
    def build_unique_id(path: Path) -> str:
        resolved = path.resolve()
        stat = resolved.stat()
        raw = f"{resolved}|{stat.st_size}|{int(stat.st_mtime)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def iter_files(path: Path) -> Iterable[Path]:
        if not path.exists() or not path.is_dir():
            return []
        return [entry for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()) if entry.is_file()]

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

            rom_count = len(list(self.iter_files(target_dir)))
            if rom_count < 2:
                continue

            systems.append({"name": entry.name, "rom_count": rom_count})

        return systems

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
            for entry in self.iter_files(asset_dir):
                stat = entry.stat()
                items.append(
                    {
                        "unique_id": self.build_unique_id(entry),
                        "name": entry.name,
                        "byte_count": stat.st_size,
                    }
                )

        return asset_dir, items

    def get_bios_root(self) -> Path:
        if not self.bios_root.exists() or not self.bios_root.is_dir():
            raise FileNotFoundError()
        return self.bios_root.resolve()

    def _build_bios_folder_id(self, path: Path, total_bytes: int) -> str:
        resolved = path.resolve()
        stat = resolved.stat()
        raw = f"{resolved}|{total_bytes}|{int(stat.st_mtime)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def list_bios_entries(self) -> List[dict]:
        bios_root = self.get_bios_root()
        folder_totals: Dict[Path, int] = {}
        folders: set[Path] = set()
        files: List[Tuple[Path, int]] = []

        for current_root, dirs, file_names in os.walk(bios_root):
            root_path = Path(current_root)

            for dir_name in dirs:
                folders.add((root_path / dir_name).resolve())

            for file_name in file_names:
                file_path = (root_path / file_name).resolve()
                if not file_path.is_file():
                    continue

                size = file_path.stat().st_size
                files.append((file_path, size))

                parent = file_path.parent
                while bios_root in parent.parents or parent == bios_root:
                    if parent == bios_root:
                        break
                    folder_totals[parent] = folder_totals.get(parent, 0) + size
                    parent = parent.parent

        entries: List[dict] = []

        for folder_path in sorted(folders, key=lambda p: str(p.relative_to(bios_root)).lower()):
            relative_path = folder_path.relative_to(bios_root).as_posix()
            total_bytes = folder_totals.get(folder_path, 0)
            entries.append(
                {
                    "entry_type": "folder",
                    "name": folder_path.name,
                    "path": relative_path,
                    "unique_id": self._build_bios_folder_id(folder_path, total_bytes),
                    "byte_count": total_bytes,
                }
            )

        for file_path, size in sorted(files, key=lambda item: str(item[0].relative_to(bios_root)).lower()):
            relative_path = file_path.relative_to(bios_root).as_posix()
            entries.append(
                {
                    "entry_type": "file",
                    "name": file_path.name,
                    "path": relative_path,
                    "unique_id": self.build_unique_id(file_path),
                    "byte_count": size,
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



TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def load_template(name: str) -> str:
    path = TEMPLATES_DIR / name
    return path.read_text(encoding="utf-8")


UI_HTML = load_template("index.html")
SWAGGER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ROM API Swagger</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.addEventListener("load", function () {
      SwaggerUIBundle({
        url: "/openapi.json",
        dom_id: "#swagger-ui",
        deepLinking: true,
        presets: [SwaggerUIBundle.presets.apis]
      });
    });
  </script>
</body>
</html>
"""

OPENAPI_SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "ROM API",
        "version": "4.0",
        "description": "Browse and download ROM, image, video, and BIOS assets.",
    },
    "servers": [{"url": "/"}],
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
        "/bios": {"get": {"summary": "List BIOS entries", "responses": {"200": {"description": "BIOS list"}}}},
        "/bios/{unique_id}": {
            "get": {
                "summary": "Download BIOS file by unique ID",
                "parameters": [{"name": "unique_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"description": "BIOS file stream"}},
            }
        },
        "/openapi.json": {"get": {"summary": "OpenAPI spec", "responses": {"200": {"description": "OpenAPI JSON"}}}},
        "/swagger": {"get": {"summary": "Swagger UI", "responses": {"200": {"description": "Swagger HTML"}}}},
    },
}

class RomRequestHandler(BaseHTTPRequestHandler):
    server_version = "RomAPI/4.0"

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

    def _guess_content_type(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".png":
            return "image/png"
        if suffix in (".jpg", ".jpeg"):
            return "image/jpeg"
        if suffix == ".webp":
            return "image/webp"
        if suffix == ".gif":
            return "image/gif"
        if suffix == ".mp4":
            return "video/mp4"
        return "application/octet-stream"

    def _send_unauthorized(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ROM API"')
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json_bytes({"error": "unauthorized"}))

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
        if status_code == 200 and cache_key:
            self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status_code: int, html: str) -> None:
        body = html_bytes(html)
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_file(self, path: Path, content_type: str, as_attachment: bool = False) -> None:
        file_size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_size))
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
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _handle_root_html(self) -> None:
        self._send_html(200, UI_HTML)

    def _handle_swagger_html(self) -> None:
        self._send_html(200, SWAGGER_HTML)

    def _handle_openapi_json(self) -> None:
        self._send_json(200, OPENAPI_SPEC)

    def _handle_systems(self) -> None:
        systems = self.repository.list_systems()
        self._send_json(200, {"systems": systems}, cache_key="json:/systems")

    def _handle_rom_list(self, system: str) -> None:
        _, roms = self.repository.list_assets(system, "roms")
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

    def _handle_bios_list(self) -> None:
        entries = self.repository.list_bios_entries()
        self._send_json(200, {"bios": entries}, cache_key="json:/bios")

    def _handle_bios_download(self, unique_id: str) -> None:
        target_path = self.repository.find_bios_file_by_unique_id(unique_id)
        self._stream_file(target_path, "application/octet-stream", as_attachment=True)

    def _handle_public_image(self, system: str, image_file: str) -> None:
        system_dir = self.repository.get_system_dir(system)
        image_file = valid_segment(image_file)
        image_path = (system_dir / "images" / image_file).resolve()
        self._stream_cached_image(image_path)

    def _handle_download(self, system: str, asset_type: str, unique_id: str) -> None:
        unique_id = valid_segment(unique_id)
        asset_dir, items = self.repository.list_assets(system, asset_type)

        target_path = None
        for item in items:
            if item["unique_id"] == unique_id:
                target_path = (asset_dir / item["name"]).resolve()
                break

        if not target_path or not target_path.exists():
            raise FileNotFoundError()
        if not target_path.is_file():
            raise ValueError("not a file")

        self._stream_file(target_path, "application/octet-stream", as_attachment=True)

    def _handle_image_file_or_download(self, system: str, image_ref: str) -> None:
        system_dir = self.repository.get_system_dir(system)
        image_ref = valid_segment(image_ref)

        image_path = (system_dir / "images" / image_ref).resolve()
        if image_path.exists():
            if not image_path.is_file():
                raise ValueError("not a file")
            self._stream_cached_image(image_path)
            return

        _, roms = self.repository.list_assets(system, "roms")
        for rom in roms:
            if rom["unique_id"] == image_ref:
                rom_stem = Path(rom["name"]).stem
                mapped_image_path = (system_dir / "images" / f"{rom_stem}-image.png").resolve()
                self._stream_cached_image(mapped_image_path)
                return

        self._handle_download(system, "images", image_ref)

    def do_GET(self) -> None:
        try:
            raw_path = self.path.split("?", 1)[0]
            parts = [part for part in raw_path.split("/") if part]

            if len(parts) == 5 and parts[0] == "public" and parts[1] == "systems" and parts[3] == "images":
                self._handle_public_image(parts[2], parts[4])
                return

            if not self.auth.check(self.headers.get("Authorization")):
                self._send_unauthorized()
                return

            if raw_path == "/":
                self._handle_root_html()
                return

            if raw_path == "/swagger":
                self._handle_swagger_html()
                return

            if raw_path == "/openapi.json":
                self._handle_openapi_json()
                return

            if raw_path == "/systems":
                self._handle_systems()
                return

            if raw_path == "/bios":
                self._handle_bios_list()
                return

            if len(parts) == 2 and parts[0] == "systems":
                self._handle_rom_list(parts[1])
                return

            if len(parts) == 2 and parts[0] == "bios":
                self._handle_bios_download(parts[1])
                return

            if len(parts) == 3 and parts[0] == "systems" and parts[2] == "images":
                self._handle_images_list(parts[1])
                return

            if len(parts) == 3 and parts[0] == "systems" and parts[2] == "videos":
                self._handle_videos_list(parts[1])
                return

            if len(parts) == 3 and parts[0] == "systems":
                self._handle_download(parts[1], "roms", parts[2])
                return

            if len(parts) == 4 and parts[0] == "systems" and parts[2] == "roms":
                self._handle_download(parts[1], "roms", parts[3])
                return

            if len(parts) == 4 and parts[0] == "systems" and parts[2] == "images":
                self._handle_image_file_or_download(parts[1], parts[3])
                return

            if len(parts) == 4 and parts[0] == "systems" and parts[2] == "videos":
                self._handle_download(parts[1], "videos", parts[3])
                return

            self._send_json(404, {"error": "not found"})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except FileNotFoundError:
            self._send_json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self._send_json(500, {"error": str(error)})


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


def create_server(settings: Settings) -> ThreadingHTTPServer:
    repository = RomRepository(settings.roms_root, settings.bios_root)
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

    cert_file, key_file = _resolve_tls_material(settings)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    server.socket = ssl_context.wrap_socket(server.socket, server_side=True)

    return server


def main() -> None:
    settings = Settings.from_env()
    server = create_server(settings)
    print(f"Serving ROM API on https://0.0.0.0:{settings.https_port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
