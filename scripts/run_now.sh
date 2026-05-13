#!/usr/bin/env bash
set -euo pipefail

DRONE_APP_URL="${DRONE_APP_URL:-}"
DRONE_APP_TEMPLATE_URL="${DRONE_APP_TEMPLATE_URL:-}"
DRONE_APP_API_ROUTES_URL="${DRONE_APP_API_ROUTES_URL:-}"
DRONE_APP_UI_ROUTES_URL="${DRONE_APP_UI_ROUTES_URL:-}"
DRONE_APP_ROUTE_CONFIG_URL="${DRONE_APP_ROUTE_CONFIG_URL:-}"
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
SCRIPT_PATH="${0:-}"
mkdir -p "$WORK_DIR"
APP_DIR="$WORK_DIR/app"
APP_PATH="$APP_DIR/rom_api.py"
MAIN_PATH="$APP_DIR/main.py"
INIT_PATH="$APP_DIR/__init__.py"
TEMPLATES_DIR="$APP_DIR/templates"
TEMPLATE_PATH="$TEMPLATES_DIR/index.html"
API_ROUTES_PATH="$APP_DIR/api_routes.py"
UI_ROUTES_PATH="$APP_DIR/ui_routes.py"
ROUTE_CONFIG_PATH="$APP_DIR/route_config.py"

if [[ -n "$DRONE_APP_BASE_URL" ]]; then
  DRONE_APP_BASE_URL="${DRONE_APP_BASE_URL%/}"
  DRONE_APP_URL="${DRONE_APP_URL:-$DRONE_APP_BASE_URL/app/rom_api.py}"
  DRONE_APP_API_ROUTES_URL="${DRONE_APP_API_ROUTES_URL:-$DRONE_APP_BASE_URL/app/api_routes.py}"
  DRONE_APP_UI_ROUTES_URL="${DRONE_APP_UI_ROUTES_URL:-$DRONE_APP_BASE_URL/app/ui_routes.py}"
  DRONE_APP_ROUTE_CONFIG_URL="${DRONE_APP_ROUTE_CONFIG_URL:-$DRONE_APP_BASE_URL/app/route_config.py}"
  DRONE_APP_TEMPLATE_URL="${DRONE_APP_TEMPLATE_URL:-$DRONE_APP_BASE_URL/app/templates/index.html}"
fi

if [[ -z "$DRONE_APP_URL" || -z "$DRONE_APP_API_ROUTES_URL" || -z "$DRONE_APP_UI_ROUTES_URL" || -z "$DRONE_APP_ROUTE_CONFIG_URL" ]]; then
  echo "Missing required app file URL(s)."
  echo "Provide DRONE_APP_BASE_URL or set DRONE_APP_URL, DRONE_APP_API_ROUTES_URL, DRONE_APP_UI_ROUTES_URL, and DRONE_APP_ROUTE_CONFIG_URL."
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

mkdir -p "$APP_DIR"
download_file "$DRONE_APP_URL" "$APP_PATH"
download_file "$DRONE_APP_API_ROUTES_URL" "$API_ROUTES_PATH"
download_file "$DRONE_APP_UI_ROUTES_URL" "$UI_ROUTES_PATH"
download_file "$DRONE_APP_ROUTE_CONFIG_URL" "$ROUTE_CONFIG_PATH"
mkdir -p "$TEMPLATES_DIR"
cat > "$INIT_PATH" <<'EOF'
# package marker
EOF
cat > "$MAIN_PATH" <<'EOF'
from app.rom_api import main

if __name__ == "__main__":
    main()
EOF
if ! download_file "$DRONE_APP_TEMPLATE_URL" "$TEMPLATE_PATH"; then
  cat > "$TEMPLATE_PATH" <<'EOF'
<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Drone App</title></head>
  <body><h1>Drone App Running</h1></body>
</html>
EOF
fi

echo "Downloaded Drone App to $WORK_DIR"

cleanup() {
  rm -rf "$WORK_DIR" 2>/dev/null || true
  if [[ -n "$SCRIPT_PATH" && -f "$SCRIPT_PATH" ]]; then
    rm -f "$SCRIPT_PATH" 2>/dev/null || true
  fi
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
  IMAGE_CACHE_TTL_SECONDS="${IMAGE_CACHE_TTL_SECONDS:-3600}" \
  IMAGE_MISS_CACHE_TTL_SECONDS="${IMAGE_MISS_CACHE_TTL_SECONDS:-300}" \
  IMAGE_CACHE_MAX_ITEMS="${IMAGE_CACHE_MAX_ITEMS:-1000}" \
  IMAGE_CACHE_MAX_BYTES="${IMAGE_CACHE_MAX_BYTES:-268435456}" \
  JSON_CACHE_TTL_SECONDS="${JSON_CACHE_TTL_SECONDS:-3600}" \
  JSON_CACHE_MAX_ITEMS="${JSON_CACHE_MAX_ITEMS:-2000}" \
  JSON_CACHE_MAX_BYTES="${JSON_CACHE_MAX_BYTES:-67108864}" \
  python3 -m app.main
