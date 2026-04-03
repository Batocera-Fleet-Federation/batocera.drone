import os
import json
import base64
import hashlib
import time
import ssl
import subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import OrderedDict
from threading import Lock
from urllib.parse import unquote

ROMS_ROOT = Path("/userdata/roms")
CERT_DIR = Path("/userdata/system/certs")
CERT_FILE = CERT_DIR / "server.crt"
KEY_FILE = CERT_DIR / "server.key"

USERNAME = os.environ.get("USERNAME")
PASSWORD = os.environ.get("PASSWORD")

if not USERNAME or not PASSWORD:
    raise RuntimeError("USERNAME and PASSWORD environment variables must be set")

HTTPS_PORT = int(os.environ.get("HTTPS_PORT", "8443"))

IMAGE_CACHE_TTL_SECONDS = int(os.environ.get("IMAGE_CACHE_TTL_SECONDS", "3600"))
IMAGE_MISS_CACHE_TTL_SECONDS = int(os.environ.get("IMAGE_MISS_CACHE_TTL_SECONDS", "300"))
IMAGE_CACHE_MAX_ITEMS = int(os.environ.get("IMAGE_CACHE_MAX_ITEMS", "1000"))
IMAGE_CACHE_MAX_BYTES = int(os.environ.get("IMAGE_CACHE_MAX_BYTES", str(256 * 1024 * 1024)))

JSON_CACHE_TTL_SECONDS = int(os.environ.get("JSON_CACHE_TTL_SECONDS", "3600"))
JSON_CACHE_MAX_ITEMS = int(os.environ.get("JSON_CACHE_MAX_ITEMS", "2000"))
JSON_CACHE_MAX_BYTES = int(os.environ.get("JSON_CACHE_MAX_BYTES", str(64 * 1024 * 1024)))

IMAGE_CACHE = OrderedDict()
IMAGE_MISS_CACHE = {}
IMAGE_CACHE_TOTAL_BYTES = 0
IMAGE_CACHE_LOCK = Lock()

JSON_CACHE = OrderedDict()
JSON_CACHE_TOTAL_BYTES = 0
JSON_CACHE_LOCK = Lock()


def json_bytes(obj):
    return json.dumps(obj, indent=2).encode("utf-8")


def html_bytes(text: str):
    return text.encode("utf-8")


def valid_segment(value: str) -> str:
    if not value or value in (".", "..") or "/" in value or "\x00" in value:
        raise ValueError("invalid path segment")
    return value


def check_auth(header_value: str) -> bool:
    if not header_value or not header_value.startswith("Basic "):
        return False
    try:
        encoded = header_value.split(" ", 1)[1].strip()
        decoded = base64.b64decode(encoded).decode("utf-8")
        user, pw = decoded.split(":", 1)
        return user == USERNAME and pw == PASSWORD
    except Exception:
        return False


def build_unique_id(path: Path) -> str:
    resolved = path.resolve()
    stat = resolved.stat()
    raw = f"{resolved}|{stat.st_size}|{int(stat.st_mtime)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def should_include_system(name: str) -> bool:
    if "." in name:
        return False
    if name.lower() == "steam":
        return False
    return True


def iter_files(path: Path):
    if not path.exists() or not path.is_dir():
        return
    for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_file():
            yield entry


def prune_expired_image_cache():
    global IMAGE_CACHE_TOTAL_BYTES
    now = time.time()

    with IMAGE_CACHE_LOCK:
        expired_keys = [key for key, value in IMAGE_CACHE.items() if value["expires_at"] <= now]
        for key in expired_keys:
            IMAGE_CACHE_TOTAL_BYTES -= IMAGE_CACHE[key]["size"]
            del IMAGE_CACHE[key]

        expired_misses = [key for key, expires_at in IMAGE_MISS_CACHE.items() if expires_at <= now]
        for key in expired_misses:
            del IMAGE_MISS_CACHE[key]


def cache_miss(path: Path):
    with IMAGE_CACHE_LOCK:
        IMAGE_MISS_CACHE[str(path)] = time.time() + IMAGE_MISS_CACHE_TTL_SECONDS


def is_miss_cached(path: Path) -> bool:
    now = time.time()
    with IMAGE_CACHE_LOCK:
        expires_at = IMAGE_MISS_CACHE.get(str(path))
        if not expires_at:
            return False
        if expires_at <= now:
            del IMAGE_MISS_CACHE[str(path)]
            return False
        return True


def get_cached_image(path: Path):
    now = time.time()
    key = str(path)

    with IMAGE_CACHE_LOCK:
        entry = IMAGE_CACHE.get(key)
        if not entry:
            return None

        if entry["expires_at"] <= now:
            global IMAGE_CACHE_TOTAL_BYTES
            IMAGE_CACHE_TOTAL_BYTES -= entry["size"]
            del IMAGE_CACHE[key]
            return None

        IMAGE_CACHE.move_to_end(key)
        return entry


def put_cached_image(path: Path, content_type: str, data: bytes):
    global IMAGE_CACHE_TOTAL_BYTES

    key = str(path)
    size = len(data)

    if size > IMAGE_CACHE_MAX_BYTES:
        return

    entry = {
        "content_type": content_type,
        "data": data,
        "size": size,
        "expires_at": time.time() + IMAGE_CACHE_TTL_SECONDS,
    }

    with IMAGE_CACHE_LOCK:
        old = IMAGE_CACHE.pop(key, None)
        if old:
            IMAGE_CACHE_TOTAL_BYTES -= old["size"]

        IMAGE_CACHE[key] = entry
        IMAGE_CACHE.move_to_end(key)
        IMAGE_CACHE_TOTAL_BYTES += size

        while len(IMAGE_CACHE) > IMAGE_CACHE_MAX_ITEMS or IMAGE_CACHE_TOTAL_BYTES > IMAGE_CACHE_MAX_BYTES:
            _, oldest_entry = IMAGE_CACHE.popitem(last=False)
            IMAGE_CACHE_TOTAL_BYTES -= oldest_entry["size"]


def prune_expired_json_cache():
    global JSON_CACHE_TOTAL_BYTES
    now = time.time()

    with JSON_CACHE_LOCK:
        expired_keys = [key for key, value in JSON_CACHE.items() if value["expires_at"] <= now]
        for key in expired_keys:
            JSON_CACHE_TOTAL_BYTES -= JSON_CACHE[key]["size"]
            del JSON_CACHE[key]


def get_cached_json(cache_key: str):
    now = time.time()

    with JSON_CACHE_LOCK:
        entry = JSON_CACHE.get(cache_key)
        if not entry:
            return None

        if entry["expires_at"] <= now:
            global JSON_CACHE_TOTAL_BYTES
            JSON_CACHE_TOTAL_BYTES -= entry["size"]
            del JSON_CACHE[cache_key]
            return None

        JSON_CACHE.move_to_end(cache_key)
        return entry["data"]


def put_cached_json(cache_key: str, data: bytes):
    global JSON_CACHE_TOTAL_BYTES

    size = len(data)
    if size > JSON_CACHE_MAX_BYTES:
        return

    entry = {
        "data": data,
        "size": size,
        "expires_at": time.time() + JSON_CACHE_TTL_SECONDS,
    }

    with JSON_CACHE_LOCK:
        old = JSON_CACHE.pop(cache_key, None)
        if old:
            JSON_CACHE_TOTAL_BYTES -= old["size"]

        JSON_CACHE[cache_key] = entry
        JSON_CACHE.move_to_end(cache_key)
        JSON_CACHE_TOTAL_BYTES += size

        while len(JSON_CACHE) > JSON_CACHE_MAX_ITEMS or JSON_CACHE_TOTAL_BYTES > JSON_CACHE_MAX_BYTES:
            _, oldest_entry = JSON_CACHE.popitem(last=False)
            JSON_CACHE_TOTAL_BYTES -= oldest_entry["size"]


def ensure_self_signed_cert():
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    if CERT_FILE.exists() and KEY_FILE.exists():
        return

    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(KEY_FILE),
            "-out", str(CERT_FILE),
            "-days", "3650",
            "-nodes",
            "-subj", "/CN=batocera.local"
        ],
        check=True
    )


class RomHandler(BaseHTTPRequestHandler):
    server_version = "RomAPI/5.0"

    def unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ROM API"')
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json_bytes({"error": "unauthorized"}))

    def send_json(self, status_code: int, payload: dict, cache_key: str = None):
        if status_code == 200 and cache_key:
            prune_expired_json_cache()
            cached = get_cached_json(cache_key)
            if cached is not None:
                body = cached
            else:
                body = json_bytes(payload)
                put_cached_json(cache_key, body)
        else:
            body = json_bytes(payload)

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if status_code == 200 and cache_key:
            self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, status_code: int, html: str):
        body = html_bytes(html)
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def guess_content_type(self, path: Path) -> str:
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

    def stream_file(self, path: Path, content_type: str, as_attachment: bool = False):
        file_size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_size))
        if as_attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()

        with path.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def stream_cached_image(self, path: Path):
        prune_expired_image_cache()

        if is_miss_cached(path):
            raise FileNotFoundError()

        cached = get_cached_image(path)
        if cached:
            data = cached["data"]
            self.send_response(200)
            self.send_header("Content-Type", cached["content_type"])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return

        if not path.exists():
            cache_miss(path)
            raise FileNotFoundError()

        if not path.is_file():
            raise ValueError("not a file")

        data = path.read_bytes()
        content_type = self.guess_content_type(path)
        put_cached_image(path, content_type, data)

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def get_system_dir(self, system: str) -> Path:
        system = valid_segment(system)
        system_link = ROMS_ROOT / system

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

    def list_asset_files(self, system: str, asset_type: str):
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
            for entry in iter_files(asset_dir):
                stat = entry.stat()
                items.append({
                    "unique_id": build_unique_id(entry),
                    "name": entry.name,
                    "byte_count": stat.st_size
                })

        return asset_dir, items

    def handle_systems(self):
        if not ROMS_ROOT.exists():
            raise FileNotFoundError()

        systems = []
        for entry in sorted(ROMS_ROOT.iterdir(), key=lambda p: p.name.lower()):
            if not (entry.is_dir() or entry.is_symlink()):
                continue
            if not should_include_system(entry.name):
                continue

            target_dir = entry.resolve()
            if not target_dir.exists() or not target_dir.is_dir():
                continue

            rom_count = len(list(iter_files(target_dir)))
            if rom_count < 2:
                continue

            systems.append({
                "name": entry.name,
                "rom_count": rom_count
            })

        self.send_json(200, {"systems": systems}, cache_key="json:/systems")

    def handle_rom_list(self, system: str):
        _, roms = self.list_asset_files(system, "roms")
        self.send_json(200, {"system": system, "roms": roms}, cache_key=f"json:/systems/{system}")

    def handle_images_list(self, system: str):
        _, images = self.list_asset_files(system, "images")
        self.send_json(200, {"system": system, "images": images}, cache_key=f"json:/systems/{system}/images")

    def handle_videos_list(self, system: str):
        _, videos = self.list_asset_files(system, "videos")
        self.send_json(200, {"system": system, "videos": videos}, cache_key=f"json:/systems/{system}/videos")

    def handle_public_image(self, system: str, image_file: str):
        system_dir = self.get_system_dir(system)
        image_file = valid_segment(unquote(image_file))
        image_path = (system_dir / "images" / image_file).resolve()
        self.stream_cached_image(image_path)

    def handle_image_file_or_download(self, system: str, image_ref: str):
        system_dir = self.get_system_dir(system)
        image_ref = valid_segment(unquote(image_ref))

        image_path = (system_dir / "images" / image_ref).resolve()
        if image_path.exists():
            if not image_path.is_file():
                raise ValueError("not a file")
            self.stream_cached_image(image_path)
            return

        _, roms = self.list_asset_files(system, "roms")
        for rom in roms:
            if rom["unique_id"] == image_ref:
                rom_stem = Path(rom["name"]).stem
                mapped_image_path = (system_dir / "images" / f"{rom_stem}-image.png").resolve()
                self.stream_cached_image(mapped_image_path)
                return

        self.handle_download(system, "images", image_ref)

    def handle_rom_download_by_id(self, system: str, unique_id: str):
        unique_id = valid_segment(unquote(unique_id))
        asset_dir, items = self.list_asset_files(system, "roms")

        target_path = None
        for item in items:
            if item["unique_id"] == unique_id:
                target_path = (asset_dir / item["name"]).resolve()
                break

        if not target_path or not target_path.exists():
            raise FileNotFoundError()

        if not target_path.is_file():
            raise ValueError("not a file")

        self.stream_file(target_path, "application/octet-stream", as_attachment=True)

    def handle_download(self, system: str, asset_type: str, unique_id: str):
        unique_id = valid_segment(unquote(unique_id))
        asset_dir, items = self.list_asset_files(system, asset_type)

        target_path = None
        for item in items:
            if item["unique_id"] == unique_id:
                target_path = (asset_dir / item["name"]).resolve()
                break

        if not target_path or not target_path.exists():
            raise FileNotFoundError()

        if not target_path.is_file():
            raise ValueError("not a file")

        self.stream_file(target_path, "application/octet-stream", as_attachment=True)

    def do_GET(self):
        try:
            raw_path = self.path.split("?", 1)[0]
            parts = [unquote(p) for p in raw_path.split("/") if p]

            # Public image route over HTTPS without auth
            if len(parts) == 5 and parts[0] == "public" and parts[1] == "systems" and parts[3] == "images":
                self.handle_public_image(parts[2], parts[4])
                return

            # Everything else requires auth
            if not check_auth(self.headers.get("Authorization")):
                self.unauthorized()
                return

            if raw_path == "/":
                self.handle_root_html()
                return

            if raw_path == "/systems":
                self.handle_systems()
                return

            if len(parts) == 2 and parts[0] == "systems":
                self.handle_rom_list(parts[1])
                return

            if len(parts) == 3 and parts[0] == "systems" and parts[2] == "images":
                self.handle_images_list(parts[1])
                return

            if len(parts) == 3 and parts[0] == "systems" and parts[2] == "videos":
                self.handle_videos_list(parts[1])
                return

            if len(parts) == 3 and parts[0] == "systems":
                self.handle_rom_download_by_id(parts[1], parts[2])
                return

            if len(parts) == 4 and parts[0] == "systems" and parts[2] == "roms":
                self.handle_download(parts[1], "roms", parts[3])
                return

            if len(parts) == 4 and parts[0] == "systems" and parts[2] == "images":
                self.handle_image_file_or_download(parts[1], parts[3])
                return

            if len(parts) == 4 and parts[0] == "systems" and parts[2] == "videos":
                self.handle_download(parts[1], "videos", parts[3])
                return

            self.send_json(404, {"error": "not found"})
        except ValueError as e:
            self.send_json(400, {"error": str(e)})
        except FileNotFoundError:
            self.send_json(404, {"error": "not found"})
        except ConnectionResetError:
            pass
        except BrokenPipeError:
            pass
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def handle_root_html(self):
        self.send_html(200, """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Batocera ROM Browser</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body { background: #f8f9fa; }
    .tile { height: 100%; }
    .pointer { cursor: pointer; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .truncate-2 {
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
      min-height: 3em;
    }
  </style>
</head>
<body>
<div class="container py-4">
  <div class="d-flex justify-content-between align-items-center mb-4">
    <div>
      <h1 class="h3 mb-1">Batocera Library</h1>
      <div class="text-muted">Systems and ROMs</div>
    </div>
    <button id="backBtn" class="btn btn-outline-secondary d-none">Back</button>
  </div>

  <div class="alert alert-warning">
    Self-signed certificate in use.
  </div>

  <div id="alerts"></div>
  <div id="content"></div>
</div>

<script>
const content = document.getElementById("content");
const alerts = document.getElementById("alerts");
const backBtn = document.getElementById("backBtn");

function showError(message) {
  alerts.innerHTML = `<div class="alert alert-danger">${message}</div>`;
}
function clearError() {
  alerts.innerHTML = "";
}
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
async function api(url) {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) {
    let msg = `Request failed: ${res.status}`;
    try {
      const data = await res.json();
      if (data.error) msg = data.error;
    } catch (_) {}
    throw new Error(msg);
  }
  return await res.json();
}
function setHash(hash) {
  window.location.hash = hash;
}
function romDownloadUrl(system, uniqueId) {
  return `/systems/${encodeURIComponent(system)}/${encodeURIComponent(uniqueId)}`;
}
function publicRomImageUrl(system, romName) {
  const lastDot = romName.lastIndexOf(".");
  const stem = lastDot >= 0 ? romName.substring(0, lastDot) : romName;
  const imageFile = `${stem}-image.png`;
  return `/public/systems/${encodeURIComponent(system)}/images/${encodeURIComponent(imageFile)}`;
}
function renderSystems(data) {
  backBtn.classList.add("d-none");
  const systems = data.systems || [];
  content.innerHTML = `
    <div class="row g-3">
      ${systems.map(system => `
        <div class="col-12 col-sm-6 col-lg-4 col-xl-3">
          <div class="card shadow-sm tile pointer" onclick="setHash('#system/${encodeURIComponent(system.name)}')">
            <div class="card-body">
              <h2 class="h5 card-title mb-2">${escapeHtml(system.name)}</h2>
              <div class="text-muted">ROMs: ${system.rom_count}</div>
            </div>
          </div>
        </div>
      `).join("")}
    </div>
  `;
}
function renderRomGrid(system, items) {
  return `
    <div class="mb-4">
      <h3 class="h5 mb-3">ROMs <span class="text-muted">(${items.length})</span></h3>
      <div class="row g-3">
        ${items.map(item => `
          <div class="col-12 col-md-6 col-xl-4">
            <div class="card shadow-sm tile h-100">
              <img
                src="${publicRomImageUrl(system, item.name)}"
                class="card-img-top"
                alt="${escapeHtml(item.name)}"
                style="height: 220px; object-fit: contain; background: #111;"
                onerror="this.style.display='none';"
              >
              <div class="card-body d-flex flex-column">
                <div class="fw-semibold truncate-2 mb-2">${escapeHtml(item.name)}</div>
                ${item.byte_count !== undefined ? `<div class="text-muted small mono mb-3">${item.byte_count} bytes</div>` : ""}
                <div class="mt-auto">
                  <a class="btn btn-primary btn-sm" href="${romDownloadUrl(system, item.unique_id)}">Download</a>
                </div>
              </div>
            </div>
          </div>
        `).join("") || `<div class="col-12"><div class="text-muted">No roms found.</div></div>`}
      </div>
    </div>
  `;
}
async function renderSystem(system) {
  backBtn.classList.remove("d-none");
  const romsData = await api(`/systems/${encodeURIComponent(system)}`);

  content.innerHTML = `
    <div class="mb-4">
      <h2 class="h4 mb-1">${escapeHtml(system)}</h2>
      <div class="text-muted">
        ROMs: ${(romsData.roms || []).length}
      </div>
    </div>

    ${renderRomGrid(system, romsData.roms || [])}
  `;
}
async function router() {
  clearError();
  try {
    const hash = window.location.hash || "";
    if (hash.startsWith("#system/")) {
      const system = decodeURIComponent(hash.substring("#system/".length));
      await renderSystem(system);
    } else {
      const data = await api("/systems");
      renderSystems(data);
    }
  } catch (err) {
    showError(err.message || "Unexpected error");
  }
}
backBtn.addEventListener("click", () => setHash(""));
window.addEventListener("hashchange", router);
router();
</script>
</body>
</html>""")


def run_https_server():
    ensure_self_signed_cert()

    httpsd = ThreadingHTTPServer(("0.0.0.0", HTTPS_PORT), RomHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(CERT_FILE), keyfile=str(KEY_FILE))
    httpsd.socket = context.wrap_socket(httpsd.socket, server_side=True)

    print(f"Serving HTTPS UI/API/images on port {HTTPS_PORT}")
    print(f"Certificate: {CERT_FILE}")
    print(f"Private key: {KEY_FILE}")
    httpsd.serve_forever()


if __name__ == "__main__":
    run_https_server()