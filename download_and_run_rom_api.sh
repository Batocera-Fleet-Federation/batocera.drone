#!/usr/bin/env bash
set -euo pipefail

ROM_API_URL="${ROM_API_URL:-}"
ROM_API_TEMPLATE_URL="${ROM_API_TEMPLATE_URL:-}"
ROM_API_BASE_URL="${ROM_API_BASE_URL:-${1:-}}"

if [[ -z "$ROM_API_URL" && -z "$ROM_API_BASE_URL" ]]; then
  echo "Usage:"
  echo "  ROM_API_BASE_URL=<raw-base-url> ./download_and_run_rom_api.sh"
  echo "  ./download_and_run_rom_api.sh <raw-base-url>"
  echo "  or set ROM_API_URL/ROM_API_TEMPLATE_URL directly"
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required"
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required"
  exit 1
fi

WORK_DIR="${ROM_API_WORK_DIR:-$HOME/.rom-api}"
mkdir -p "$WORK_DIR"
APP_PATH="$WORK_DIR/rom_api.py"
TEMPLATES_DIR="$WORK_DIR/templates"
TEMPLATE_PATH="$TEMPLATES_DIR/index.html"

if [[ -n "$ROM_API_BASE_URL" ]]; then
  ROM_API_BASE_URL="${ROM_API_BASE_URL%/}"
  ROM_API_URL="${ROM_API_URL:-$ROM_API_BASE_URL/rom_api.py}"
  ROM_API_TEMPLATE_URL="${ROM_API_TEMPLATE_URL:-$ROM_API_BASE_URL/templates/index.html}"
fi

curl -fsSL "$ROM_API_URL" -o "$APP_PATH"
mkdir -p "$TEMPLATES_DIR"
if ! curl -fsSL "$ROM_API_TEMPLATE_URL" -o "$TEMPLATE_PATH"; then
  cat > "$TEMPLATE_PATH" <<'EOF'
<!doctype html>
<html>
  <head><meta charset="utf-8"><title>ROM API</title></head>
  <body><h1>ROM API Running</h1></body>
</html>
EOF
fi

echo "Downloaded ROM API to $WORK_DIR"

if [[ -z "${ROM_API_USERNAME:-}" ]]; then
  read -r -p "ROM_API_USERNAME: " ROM_API_USERNAME
fi

if [[ -z "${ROM_API_PASSWORD:-}" ]]; then
  read -r -s -p "ROM_API_PASSWORD: " ROM_API_PASSWORD
  echo
fi

exec env \
  ROM_API_USERNAME="$ROM_API_USERNAME" \
  ROM_API_PASSWORD="$ROM_API_PASSWORD" \
  HTTPS_PORT="${HTTPS_PORT:-8443}" \
  ROMS_ROOT="${ROMS_ROOT:-$WORK_DIR/local-data/roms}" \
  BIOS_ROOT="${BIOS_ROOT:-$WORK_DIR/local-data/bios}" \
  TLS_SELF_SIGNED_DIR="${TLS_SELF_SIGNED_DIR:-$WORK_DIR/local-data/certs}" \
  IMAGE_CACHE_TTL_SECONDS="${IMAGE_CACHE_TTL_SECONDS:-3600}" \
  IMAGE_MISS_CACHE_TTL_SECONDS="${IMAGE_MISS_CACHE_TTL_SECONDS:-300}" \
  IMAGE_CACHE_MAX_ITEMS="${IMAGE_CACHE_MAX_ITEMS:-1000}" \
  IMAGE_CACHE_MAX_BYTES="${IMAGE_CACHE_MAX_BYTES:-268435456}" \
  JSON_CACHE_TTL_SECONDS="${JSON_CACHE_TTL_SECONDS:-3600}" \
  JSON_CACHE_MAX_ITEMS="${JSON_CACHE_MAX_ITEMS:-2000}" \
  JSON_CACHE_MAX_BYTES="${JSON_CACHE_MAX_BYTES:-67108864}" \
  python3 "$APP_PATH"
