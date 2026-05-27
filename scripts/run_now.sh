#!/usr/bin/env bash
set -euo pipefail

DRONE_APP_URL="${DRONE_APP_URL:-}"
DRONE_APP_TEMPLATE_URL="${DRONE_APP_TEMPLATE_URL:-}"
DRONE_APP_API_ROUTES_URL="${DRONE_APP_API_ROUTES_URL:-}"
DRONE_APP_UI_ROUTES_URL="${DRONE_APP_UI_ROUTES_URL:-}"
DRONE_APP_ROUTE_CONFIG_URL="${DRONE_APP_ROUTE_CONFIG_URL:-}"
DRONE_APP_CSS_URL="${DRONE_APP_CSS_URL:-}"
DRONE_APP_JS_URL="${DRONE_APP_JS_URL:-}"
DRONE_APP_CONTENT_URL="${DRONE_APP_CONTENT_URL:-}"
DRONE_APP_ARCHIVE_URL="${DRONE_APP_ARCHIVE_URL:-}"
DRONE_APP_BASE_URL="${DRONE_APP_BASE_URL:-${1:-}}"

if [[ -z "$DRONE_APP_URL" && -z "$DRONE_APP_BASE_URL" ]]; then
  echo "Usage:"
  echo "  DRONE_APP_BASE_URL=<raw-base-url> ./run_now.sh"
  echo "  ./run_now.sh <raw-base-url>"
  echo "  or set all required file URLs directly"
  exit 1
fi

DOWNLOAD_TOOL=""
if command -v curl >/dev/null 2>&1; then
  DOWNLOAD_TOOL="curl"
elif command -v wget >/dev/null 2>&1; then
  DOWNLOAD_TOOL="wget"
else
  echo "curl or wget is required"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required"
  exit 1
fi

WORK_DIR="${DRONE_APP_WORK_DIR:-$HOME/.drone-app}"
mkdir -p "$WORK_DIR"
APP_DIR="$WORK_DIR/app"
APP_PATH="$APP_DIR/drone_api.py"
MAIN_PATH="$APP_DIR/main.py"
INIT_PATH="$APP_DIR/__init__.py"
TEMPLATES_DIR="$APP_DIR/templates"
TEMPLATE_PATH="$TEMPLATES_DIR/index.html"
STATIC_DIR="$APP_DIR/static"
CSS_PATH="$STATIC_DIR/css/drone.css"
JS_PATH="$STATIC_DIR/js/drone.js"
CONTENT_DIR="$WORK_DIR/content"
API_ROUTES_PATH="$APP_DIR/api_routes.py"
UI_ROUTES_PATH="$APP_DIR/ui_routes.py"
ROUTE_CONFIG_PATH="$APP_DIR/route_config.py"

if [[ -n "$DRONE_APP_BASE_URL" ]]; then
  DRONE_APP_BASE_URL="${DRONE_APP_BASE_URL%/}"
  DRONE_APP_URL="${DRONE_APP_URL:-$DRONE_APP_BASE_URL/app/drone_api.py}"
  DRONE_APP_API_ROUTES_URL="${DRONE_APP_API_ROUTES_URL:-$DRONE_APP_BASE_URL/app/api_routes.py}"
  DRONE_APP_UI_ROUTES_URL="${DRONE_APP_UI_ROUTES_URL:-$DRONE_APP_BASE_URL/app/ui_routes.py}"
  DRONE_APP_ROUTE_CONFIG_URL="${DRONE_APP_ROUTE_CONFIG_URL:-$DRONE_APP_BASE_URL/app/route_config.py}"
  DRONE_APP_TEMPLATE_URL="${DRONE_APP_TEMPLATE_URL:-$DRONE_APP_BASE_URL/app/templates/index.html}"
  DRONE_APP_CSS_URL="${DRONE_APP_CSS_URL:-$DRONE_APP_BASE_URL/app/static/css/drone.css}"
  DRONE_APP_JS_URL="${DRONE_APP_JS_URL:-$DRONE_APP_BASE_URL/app/static/js/drone.js}"
  DRONE_APP_CONTENT_URL="${DRONE_APP_CONTENT_URL:-$DRONE_APP_BASE_URL/content}"

  if [[ -z "$DRONE_APP_ARCHIVE_URL" && "$DRONE_APP_BASE_URL" == https://raw.githubusercontent.com/* ]]; then
    raw_path="${DRONE_APP_BASE_URL#https://raw.githubusercontent.com/}"
    owner="${raw_path%%/*}"
    raw_path="${raw_path#*/}"
    repo="${raw_path%%/*}"
    raw_path="${raw_path#*/}"
    ref="${raw_path%%/*}"
    if [[ -n "$owner" && -n "$repo" && -n "$ref" ]]; then
      DRONE_APP_ARCHIVE_URL="https://codeload.github.com/$owner/$repo/tar.gz/$ref"
    fi
  fi
fi

if [[ -z "$DRONE_APP_URL" || -z "$DRONE_APP_API_ROUTES_URL" || -z "$DRONE_APP_UI_ROUTES_URL" || -z "$DRONE_APP_ROUTE_CONFIG_URL" || -z "$DRONE_APP_CSS_URL" || -z "$DRONE_APP_JS_URL" ]]; then
  echo "Missing required app file URL(s)."
  echo "Provide DRONE_APP_BASE_URL or set DRONE_APP_URL, DRONE_APP_API_ROUTES_URL, DRONE_APP_UI_ROUTES_URL, DRONE_APP_ROUTE_CONFIG_URL, DRONE_APP_CSS_URL, and DRONE_APP_JS_URL."
  exit 1
fi

download_file() {
  local src="$1"
  local dst="$2"
  if [[ "$DOWNLOAD_TOOL" == "curl" ]]; then
    curl -fsSL "$src" -o "$dst"
  else
    wget -qO "$dst" "$src"
  fi
}

download_archive_dirs() {
  local archive_path="$WORK_DIR/source.tar.gz"
  download_file "$DRONE_APP_ARCHIVE_URL" "$archive_path"
  python3 - "$archive_path" "$WORK_DIR" <<'PY'
import shutil
import sys
import tarfile
from pathlib import Path

archive_path = Path(sys.argv[1])
work_dir = Path(sys.argv[2]).resolve()
wanted_roots = ("app/", "content/")

with tarfile.open(archive_path, "r:gz") as archive:
    for member in archive.getmembers():
        parts = member.name.split("/", 1)
        if len(parts) != 2:
            continue
        relative = parts[1]
        if not relative.startswith(wanted_roots):
            continue
        relative_path = Path(relative)
        if "__pycache__" in relative_path.parts:
            continue
        target = (work_dir / relative_path).resolve()
        if work_dir not in target.parents and target != work_dir:
            raise RuntimeError(f"archive member escapes work dir: {member.name}")
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        source = archive.extractfile(member)
        if source is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
PY
  rm -f "$archive_path"
}

copy_local_dirs() {
  local base_path="${DRONE_APP_BASE_URL#file://}"
  python3 - "$base_path" "$WORK_DIR" <<'PY'
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote

source_root = Path(unquote(sys.argv[1])).resolve()
work_dir = Path(sys.argv[2]).resolve()

for name in ("app", "content"):
    source = source_root / name
    target = work_dir / name
    if not source.exists() or not source.is_dir():
        raise RuntimeError(f"missing required source directory: {source}")
    if target.exists():
        shutil.rmtree(target)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(source, target, ignore=ignore)
PY
}

if [[ -n "$DRONE_APP_BASE_URL" ]]; then
  if [[ -n "$DRONE_APP_ARCHIVE_URL" ]] && download_archive_dirs; then
    :
  elif [[ "$DRONE_APP_BASE_URL" == file://* ]]; then
    copy_local_dirs
  else
    echo "DRONE_APP_BASE_URL must be a GitHub raw URL, a file:// URL, or be paired with DRONE_APP_ARCHIVE_URL so app/ and content/ can be staged completely."
    exit 1
  fi
else
  mkdir -p "$APP_DIR"
  download_file "$DRONE_APP_URL" "$APP_PATH"
  download_file "$DRONE_APP_API_ROUTES_URL" "$API_ROUTES_PATH"
  download_file "$DRONE_APP_UI_ROUTES_URL" "$UI_ROUTES_PATH"
  download_file "$DRONE_APP_ROUTE_CONFIG_URL" "$ROUTE_CONFIG_PATH"
  mkdir -p "$TEMPLATES_DIR"
  mkdir -p "$(dirname "$CSS_PATH")" "$(dirname "$JS_PATH")"
  cat > "$INIT_PATH" <<'EOF'
# package marker
EOF
  cat > "$MAIN_PATH" <<'EOF'
from app.drone_api import main

if __name__ == "__main__":
    main()
EOF
fi

if [[ -z "$DRONE_APP_BASE_URL" && ! -f "$TEMPLATE_PATH" ]] && ! download_file "$DRONE_APP_TEMPLATE_URL" "$TEMPLATE_PATH"; then
  mkdir -p "$(dirname "$TEMPLATE_PATH")"
  cat > "$TEMPLATE_PATH" <<'EOF'
<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Drone App</title></head>
  <body><h1>Drone App Running</h1></body>
</html>
EOF
fi

if [[ -z "$DRONE_APP_BASE_URL" && ! -f "$CSS_PATH" ]]; then
  mkdir -p "$(dirname "$CSS_PATH")"
  download_file "$DRONE_APP_CSS_URL" "$CSS_PATH"
fi

if [[ -z "$DRONE_APP_BASE_URL" && ! -f "$JS_PATH" ]]; then
  mkdir -p "$(dirname "$JS_PATH")"
  download_file "$DRONE_APP_JS_URL" "$JS_PATH"
fi

if [[ -z "$DRONE_APP_BASE_URL" && -n "$DRONE_APP_CONTENT_URL" && ! -f "$CONTENT_DIR/batocera-swarm-mascot.jpg" ]]; then
  mkdir -p "$CONTENT_DIR"
  download_file "$DRONE_APP_CONTENT_URL/batocera-swarm-mascot.jpg" "$CONTENT_DIR/batocera-swarm-mascot.jpg"
fi

if [[ ! -f "$APP_PATH" || ! -d "$STATIC_DIR" || ! -d "$CONTENT_DIR" ]]; then
  echo "Downloaded Drone App is incomplete. Expected app/, app/static/, and content/ under $WORK_DIR."
  exit 1
fi

echo "Downloaded Drone App to $WORK_DIR"

cleanup() {
  rm -rf "$WORK_DIR" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

DRONE_APP_USERNAME="${DRONE_APP_USERNAME:-}"
DRONE_APP_PASSWORD="${DRONE_APP_PASSWORD:-}"

env \
  PYTHONPATH="$WORK_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  DRONE_APP_USERNAME="$DRONE_APP_USERNAME" \
  DRONE_APP_PASSWORD="$DRONE_APP_PASSWORD" \
  HTTPS_PORT="${HTTPS_PORT:-8443}" \
  ROMS_ROOT="${ROMS_ROOT:-/userdata/roms}" \
  BIOS_ROOT="${BIOS_ROOT:-/userdata/bios}" \
  TLS_SELF_SIGNED_DIR="${TLS_SELF_SIGNED_DIR:-/userdata/system/certs}" \
  LOG_DIR="${LOG_DIR:-/userdata/system/logs/drone-app}" \
  LOG_MAX_BYTES="${LOG_MAX_BYTES:-5242880}" \
  LOG_BACKUP_COUNT="${LOG_BACKUP_COUNT:-5}" \
  ROM_METADATA_POLL_SECONDS="${ROM_METADATA_POLL_SECONDS:-900}" \
  ROM_METADATA_INITIAL_DELAY_SECONDS="${ROM_METADATA_INITIAL_DELAY_SECONDS:-60}" \
  ROM_METADATA_PROGRESS_SECONDS="${ROM_METADATA_PROGRESS_SECONDS:-30}" \
  ROM_METADATA_PROGRESS_FILES="${ROM_METADATA_PROGRESS_FILES:-250}" \
  ROM_METADATA_HASH_IO_YIELD_SECONDS="${ROM_METADATA_HASH_IO_YIELD_SECONDS:-0.05}" \
  IMAGE_CACHE_TTL_SECONDS="${IMAGE_CACHE_TTL_SECONDS:-3600}" \
  IMAGE_MISS_CACHE_TTL_SECONDS="${IMAGE_MISS_CACHE_TTL_SECONDS:-300}" \
  IMAGE_CACHE_MAX_ITEMS="${IMAGE_CACHE_MAX_ITEMS:-1000}" \
  IMAGE_CACHE_MAX_BYTES="${IMAGE_CACHE_MAX_BYTES:-268435456}" \
  JSON_CACHE_TTL_SECONDS="${JSON_CACHE_TTL_SECONDS:-3600}" \
  JSON_CACHE_MAX_ITEMS="${JSON_CACHE_MAX_ITEMS:-2000}" \
  JSON_CACHE_MAX_BYTES="${JSON_CACHE_MAX_BYTES:-67108864}" \
  OVERMIND_DRONE_TOKEN="${OVERMIND_DRONE_TOKEN:-}" \
  OVERMIND_POLL_SECONDS="${OVERMIND_POLL_SECONDS:-60}" \
  OVERMIND_SPEED_SAMPLE_SECONDS="${OVERMIND_SPEED_SAMPLE_SECONDS:-600}" \
  python3 -m app.main
