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
DRONE_APP_FALLBACK_ARCHIVE_URL="${DRONE_APP_FALLBACK_ARCHIVE_URL:-}"
DRONE_APP_STAGE_ONLY="${DRONE_APP_STAGE_ONLY:-0}"
DRONE_APP_DEVELOPMENT="${DRONE_APP_DEVELOPMENT:-0}"
DRONE_APP_BASE_URL="${DRONE_APP_BASE_URL:-}"

usage() {
  echo "Usage:"
  echo "  DRONE_APP_BASE_URL=<raw-base-url> ./run_web_now.sh"
  echo "  ./run_web_now.sh <raw-base-url>"
  echo "  ./run_web_now.sh --dev [<raw-base-url>]"
  echo "  or set all required file URLs directly"
  echo ""
  echo "Normal installs use a published drone-app.tar.gz release. Source/codeload"
  echo "archives require an explicit development option: --dev or DRONE_APP_DEVELOPMENT=1."
}

for arg in "$@"; do
  case "$arg" in
    --dev|--development)
      DRONE_APP_DEVELOPMENT=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      echo "Unknown option: $arg"
      usage
      exit 1
      ;;
    *)
      if [[ -z "$DRONE_APP_BASE_URL" ]]; then
        DRONE_APP_BASE_URL="$arg"
      fi
      ;;
  esac
done

if [[ -z "$DRONE_APP_URL" && -z "$DRONE_APP_BASE_URL" ]]; then
  DRONE_APP_BASE_URL="https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main"
fi

development_mode_enabled() {
  case "${DRONE_APP_DEVELOPMENT:-0}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

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

WORK_DIR="${DRONE_APP_WORK_DIR:-/userdata/system/drone-app}"
mkdir -p "$WORK_DIR"
STAGE_DIR="$WORK_DIR/.incoming.$$"
mkdir -p "$STAGE_DIR"

cleanup_stage() {
  if [[ -n "${STAGE_DIR:-}" && -d "$STAGE_DIR" ]]; then
    rm -rf "$STAGE_DIR"
  fi
}
trap cleanup_stage EXIT

using_stage=0
if [[ -n "$DRONE_APP_BASE_URL" ]]; then
  using_stage=1
  APP_ROOT="$STAGE_DIR"
else
  APP_ROOT="$WORK_DIR"
fi

APP_DIR="$APP_ROOT/app"
APP_PATH="$APP_DIR/drone_api.py"
MAIN_PATH="$APP_DIR/main.py"
INIT_PATH="$APP_DIR/__init__.py"
TEMPLATES_DIR="$APP_DIR/web/templates"
TEMPLATE_PATH="$TEMPLATES_DIR/index.html"
STATIC_DIR="$APP_DIR/web/static"
CSS_PATH="$STATIC_DIR/css/drone.css"
JS_PATH="$STATIC_DIR/js/drone.js"
INTEGRATIONS_JS_PATH="$STATIC_DIR/js/integrations.js"
INTEGRATIONS_HANDLER_PATH="$APP_DIR/web/handlers_integrations.py"
INTEGRATIONS_INIT_PATH="$APP_DIR/integrations/__init__.py"
INTEGRATIONS_REGISTRY_PATH="$APP_DIR/integrations/registry.py"
STREAMDECK_MANAGER_PATH="$APP_DIR/integrations/streamdeck/manager.py"
CONTENT_DIR="$APP_ROOT/content"
API_ROUTES_PATH="$APP_DIR/web/api_routes.py"
UI_ROUTES_PATH="$APP_DIR/web/ui_routes.py"
ROUTE_CONFIG_PATH="$APP_DIR/web/route_config.py"
VERSION_PATH="$APP_DIR/VERSION"

if [[ -n "$DRONE_APP_BASE_URL" ]]; then
  DRONE_APP_BASE_URL="${DRONE_APP_BASE_URL%/}"
  DRONE_APP_URL="${DRONE_APP_URL:-$DRONE_APP_BASE_URL/app/drone_api.py}"
  DRONE_APP_API_ROUTES_URL="${DRONE_APP_API_ROUTES_URL:-$DRONE_APP_BASE_URL/app/web/api_routes.py}"
  DRONE_APP_UI_ROUTES_URL="${DRONE_APP_UI_ROUTES_URL:-$DRONE_APP_BASE_URL/app/web/ui_routes.py}"
  DRONE_APP_ROUTE_CONFIG_URL="${DRONE_APP_ROUTE_CONFIG_URL:-$DRONE_APP_BASE_URL/app/web/route_config.py}"
  DRONE_APP_TEMPLATE_URL="${DRONE_APP_TEMPLATE_URL:-$DRONE_APP_BASE_URL/app/web/templates/index.html}"
  DRONE_APP_CSS_URL="${DRONE_APP_CSS_URL:-$DRONE_APP_BASE_URL/app/web/static/css/drone.css}"
  DRONE_APP_JS_URL="${DRONE_APP_JS_URL:-$DRONE_APP_BASE_URL/app/web/static/js/drone.js}"
  DRONE_APP_CONTENT_URL="${DRONE_APP_CONTENT_URL:-$DRONE_APP_BASE_URL/content}"

  if [[ -z "$DRONE_APP_ARCHIVE_URL" && "$DRONE_APP_BASE_URL" == https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/* ]]; then
    DRONE_APP_ARCHIVE_URL="https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/drone-app.tar.gz"
  fi

  if development_mode_enabled && [[ -z "$DRONE_APP_FALLBACK_ARCHIVE_URL" && "$DRONE_APP_BASE_URL" == https://raw.githubusercontent.com/* ]]; then
    raw_path="${DRONE_APP_BASE_URL#https://raw.githubusercontent.com/}"
    owner="${raw_path%%/*}"
    raw_path="${raw_path#*/}"
    repo="${raw_path%%/*}"
    raw_path="${raw_path#*/}"
    ref="${raw_path%%/*}"
    if [[ -n "$owner" && -n "$repo" && -n "$ref" ]]; then
      DRONE_APP_FALLBACK_ARCHIVE_URL="https://codeload.github.com/$owner/$repo/tar.gz/$ref"
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
    curl -fsSL --connect-timeout 10 --max-time 120 "$src" -o "$dst"
  else
    wget -T 120 -qO "$dst" "$src"
  fi
}

download_archive_dirs() {
  local archive_url="$1"
  local archive_path="$STAGE_DIR/source.tar.gz"
  rm -rf "$STAGE_DIR/app" "$STAGE_DIR/content"
  rm -f "$archive_path"
  if ! download_file "$archive_url" "$archive_path"; then
    echo "Failed to download archive from $archive_url"
    rm -f "$archive_path"
    return 1
  fi
  if [ ! -f "$archive_path" ]; then
    echo "Archive download produced no file at $archive_path"
    return 1
  fi
  python3 - "$archive_path" "$STAGE_DIR" <<'PY'
import sys
import tarfile
import shutil
from pathlib import Path

archive_path = Path(sys.argv[1])
work_dir = Path(sys.argv[2]).resolve()
wanted_roots = ("app/", "content/")

with tarfile.open(archive_path, "r:gz") as archive:
    for member in archive.getmembers():
        relative = member.name.lstrip("/")
        if not relative.startswith(wanted_roots):
            parts = relative.split("/", 1)
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
  python3 - "$base_path" "$STAGE_DIR" <<'PY'
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

download_any_archive() {
  if [[ -n "$DRONE_APP_ARCHIVE_URL" ]]; then
    if download_archive_dirs "$DRONE_APP_ARCHIVE_URL"; then
      return 0
    fi
    echo "Failed to download or extract the published Drone release from $DRONE_APP_ARCHIVE_URL"
  fi
  if development_mode_enabled && [[ -n "$DRONE_APP_FALLBACK_ARCHIVE_URL" ]]; then
    echo "Development mode enabled; trying source archive $DRONE_APP_FALLBACK_ARCHIVE_URL"
    if download_archive_dirs "$DRONE_APP_FALLBACK_ARCHIVE_URL"; then
      return 0
    fi
    echo "Failed to download development source archive from $DRONE_APP_FALLBACK_ARCHIVE_URL"
  elif [[ -n "$DRONE_APP_FALLBACK_ARCHIVE_URL" ]]; then
    echo "Ignoring source/codeload fallback because development mode is not enabled."
  fi
  return 1
}

overlay_staged_tree() {
  python3 - "$STAGE_DIR" "$WORK_DIR" <<'PY'
import shutil
import sys
from pathlib import Path

stage_dir = Path(sys.argv[1]).resolve()
work_dir = Path(sys.argv[2]).resolve()
for name in ("app", "content"):
    source = stage_dir / name
    target = work_dir / name
    if not source.exists():
        continue
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
PY
}

read_staged_version() {
  if [[ ! -s "$VERSION_PATH" ]]; then
    echo ""
    return 0
  fi
  head -n 1 "$VERSION_PATH" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

validate_staged_payload() {
  if [[ ! -f "$APP_PATH" || ! -d "$STATIC_DIR" || ! -d "$CONTENT_DIR" ]]; then
    echo "Downloaded Drone App is incomplete. Expected app/, app/web/static/, and content/ under the staging directory."
    return 1
  fi

  local required_file
  for required_file in \
    "$MAIN_PATH" \
    "$APP_PATH" \
    "$VERSION_PATH" \
    "$API_ROUTES_PATH" \
    "$UI_ROUTES_PATH" \
    "$ROUTE_CONFIG_PATH" \
    "$TEMPLATE_PATH" \
    "$CSS_PATH" \
    "$JS_PATH" \
    "$INTEGRATIONS_JS_PATH" \
    "$INTEGRATIONS_HANDLER_PATH" \
    "$INTEGRATIONS_INIT_PATH" \
    "$INTEGRATIONS_REGISTRY_PATH" \
    "$STREAMDECK_MANAGER_PATH" \
    "$CONTENT_DIR/batocera-swarm-mascot.jpg" \
    "$CONTENT_DIR/drone.png"; do
    if [[ ! -s "$required_file" ]]; then
      echo "Downloaded Drone App is incomplete. Missing or empty required file: $required_file"
      return 1
    fi
  done

  local version
  version="$(read_staged_version)"
  if [[ -z "$version" ]]; then
    echo "Downloaded Drone App is unversioned. Missing or empty app/VERSION."
    return 1
  fi
  if development_mode_enabled; then
    :
  elif ! printf '%s\n' "$version" | grep -qE '^v?[0-9]+\.[0-9]+\.[0-9]+([-+].*)?$'; then
    echo "Rejected unversioned or development payload (VERSION=${version}). Published installs require a semantic release. Use --dev or DRONE_APP_DEVELOPMENT=1 for source archives."
    return 1
  fi

  if ! PYTHONPATH="$APP_ROOT" python3 - <<'PY'
import importlib

required = {
    "app.web.api_routes": "ApiRoutesMixin",
    "app.web.ui_routes": "UiRoutesMixin",
}

for module_name, symbol in required.items():
    module = importlib.import_module(module_name)
    if not hasattr(module, symbol):
        raise ImportError(f"{module_name} does not export {symbol}")

importlib.import_module("app.drone_api")
PY
  then
    echo "Downloaded Drone App failed import validation. Refusing to launch incomplete app bundle."
    return 1
  fi

  INSTALLED_VERSION="$version"
}

abort_without_installing() {
  echo "Refusing to change the installed Drone App. Existing files under $WORK_DIR were left unchanged."
  echo "A published, versioned drone-app.tar.gz is required unless you pass --dev or set DRONE_APP_DEVELOPMENT=1."
  exit 1
}

if [[ -n "$DRONE_APP_BASE_URL" ]]; then
  if download_any_archive; then
    :
  elif [[ "$DRONE_APP_BASE_URL" == file://* ]]; then
    copy_local_dirs
  else
    echo "Failed to download a published Drone release. Source/codeload fallback is disabled unless development mode is enabled."
    echo "DRONE_APP_BASE_URL must be a GitHub raw URL paired with a release archive, a file:// URL, or DRONE_APP_ARCHIVE_URL."
    abort_without_installing
  fi
  if ! validate_staged_payload; then
    abort_without_installing
  fi
  overlay_staged_tree
  rm -rf "$STAGE_DIR"
  STAGE_DIR=""
else
  mkdir -p "$APP_DIR"
  download_file "$DRONE_APP_URL" "$APP_PATH"
  mkdir -p "$APP_DIR/web"
  download_file "$DRONE_APP_API_ROUTES_URL" "$API_ROUTES_PATH"
  download_file "$DRONE_APP_UI_ROUTES_URL" "$UI_ROUTES_PATH"
  download_file "$DRONE_APP_ROUTE_CONFIG_URL" "$ROUTE_CONFIG_PATH"
  mkdir -p "$TEMPLATES_DIR"
  mkdir -p "$(dirname "$CSS_PATH")" "$(dirname "$JS_PATH")"
  cat > "$INIT_PATH" <<'EOF'
# package marker
EOF
  cat > "$APP_DIR/web/__init__.py" <<'EOF'
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

if [[ "$using_stage" != "1" ]]; then
  if [[ ! -f "$APP_PATH" || ! -d "$STATIC_DIR" || ! -d "$CONTENT_DIR" ]]; then
    echo "Downloaded Drone App is incomplete. Expected app/, app/static/, and content/ under $WORK_DIR."
    exit 1
  fi

  for required_file in \
    "$MAIN_PATH" \
    "$APP_PATH" \
    "$API_ROUTES_PATH" \
    "$UI_ROUTES_PATH" \
    "$ROUTE_CONFIG_PATH" \
    "$INTEGRATIONS_JS_PATH" \
    "$INTEGRATIONS_HANDLER_PATH" \
    "$INTEGRATIONS_INIT_PATH" \
    "$INTEGRATIONS_REGISTRY_PATH" \
    "$STREAMDECK_MANAGER_PATH"; do
    if [[ ! -s "$required_file" ]]; then
      echo "Downloaded Drone App is incomplete. Missing or empty required file: $required_file"
      exit 1
    fi
  done

  if ! PYTHONPATH="$WORK_DIR" python3 - <<'PY'
import importlib

required = {
    "app.web.api_routes": "ApiRoutesMixin",
    "app.web.ui_routes": "UiRoutesMixin",
}

for module_name, symbol in required.items():
    module = importlib.import_module(module_name)
    if not hasattr(module, symbol):
        raise ImportError(f"{module_name} does not export {symbol}")

importlib.import_module("app.drone_api")
PY
  then
    echo "Downloaded Drone App failed import validation. Refusing to launch incomplete app bundle."
    exit 1
  fi
  INSTALLED_VERSION="$(read_staged_version)"
fi

LIVE_VERSION_PATH="$WORK_DIR/app/VERSION"
if [[ -z "${INSTALLED_VERSION:-}" && -s "$LIVE_VERSION_PATH" ]]; then
  INSTALLED_VERSION="$(head -n 1 "$LIVE_VERSION_PATH" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
fi
INSTALLED_VERSION="${INSTALLED_VERSION:-unknown}"

echo "Downloaded Drone App to $WORK_DIR"
echo "Installed Drone App version ${INSTALLED_VERSION}"

if [[ "$DRONE_APP_STAGE_ONLY" == "1" || "$DRONE_APP_STAGE_ONLY" == "true" || "$DRONE_APP_STAGE_ONLY" == "yes" ]]; then
  echo "Drone App staged successfully; launch skipped because DRONE_APP_STAGE_ONLY=${DRONE_APP_STAGE_ONLY}."
  exit 0
fi

HTTPS_PORT="${HTTPS_PORT:-443}"

# Prevent multiple instances - bail if the configured port is already in use.
if lsof -i :"$HTTPS_PORT" >/dev/null 2>&1; then
  echo "Port ${HTTPS_PORT} is already in use - Drone App may already be running. Exiting."
  exit 0
fi

# Ensure the Drone certificate directory exists so openssl can write to it
DRONE_CERT_FILE="${DRONE_CERT_FILE:-}"
DRONE_CERT_DIR="${DRONE_CERT_FILE%/*}"
if [ -z "$DRONE_CERT_DIR" ]; then
  DRONE_CERT_DIR="$WORK_DIR/certs"
fi
mkdir -p "$DRONE_CERT_DIR" 2>/dev/null || true

# Do NOT clean up on exit — the app files must persist for use

DRONE_APP_USERNAME="${DRONE_APP_USERNAME:-}"
DRONE_APP_PASSWORD="${DRONE_APP_PASSWORD:-}"

env \
  PYTHONPATH="$WORK_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  DRONE_APP_USERNAME="$DRONE_APP_USERNAME" \
  DRONE_APP_PASSWORD="$DRONE_APP_PASSWORD" \
  HTTPS_PORT="$HTTPS_PORT" \
  DRONE_COMPAT_HTTPS_PORTS="${DRONE_COMPAT_HTTPS_PORTS:-8443}" \
  ROMS_ROOT="${ROMS_ROOT:-/userdata/roms}" \
  BIOS_ROOT="${BIOS_ROOT:-/userdata/bios}" \
  TLS_SELF_SIGNED_DIR="${TLS_SELF_SIGNED_DIR:-/userdata/system/certs}" \
  LOG_DIR="${LOG_DIR:-/userdata/system/logs/drone-app}" \
  LOG_MAX_BYTES="${LOG_MAX_BYTES:-5242880}" \
  LOG_BACKUP_COUNT="${LOG_BACKUP_COUNT:-5}" \
  DRONE_LOG_UNAUTHORIZED_REQUESTS="${DRONE_LOG_UNAUTHORIZED_REQUESTS:-0}" \
  DRONE_UNAUTH_RATE_LIMIT_ENABLED="${DRONE_UNAUTH_RATE_LIMIT_ENABLED:-1}" \
  DRONE_UNAUTH_RATE_LIMIT_REQUESTS="${DRONE_UNAUTH_RATE_LIMIT_REQUESTS:-60}" \
  DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS="${DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS:-60}" \
  ROM_METADATA_POLL_SECONDS="${ROM_METADATA_POLL_SECONDS:-900}" \
  ROM_METADATA_INITIAL_DELAY_SECONDS="${ROM_METADATA_INITIAL_DELAY_SECONDS:-60}" \
  ROM_METADATA_PROGRESS_SECONDS="${ROM_METADATA_PROGRESS_SECONDS:-30}" \
  ROM_METADATA_PROGRESS_FILES="${ROM_METADATA_PROGRESS_FILES:-250}" \
  ROM_METADATA_UPLOAD_CHUNK_SIZE="${ROM_METADATA_UPLOAD_CHUNK_SIZE:-250}" \
  ROM_METADATA_HASH_IO_YIELD_SECONDS="${ROM_METADATA_HASH_IO_YIELD_SECONDS:-0.05}" \
  ROM_METADATA_HASH_ROMS_ENABLED="${ROM_METADATA_HASH_ROMS_ENABLED:-1}" \
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
