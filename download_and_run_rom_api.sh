#!/usr/bin/env bash
set -euo pipefail

ROM_API_URL="${ROM_API_URL:-${1:-}}"
if [[ -z "$ROM_API_URL" ]]; then
  echo "Usage: ROM_API_URL=<raw rom_api.py URL> ./download_and_run_rom_api.sh"
  echo "   or: ./download_and_run_rom_api.sh <raw rom_api.py URL>"
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

curl -fsSL "$ROM_API_URL" -o "$APP_PATH"

echo "Downloaded rom_api.py to $APP_PATH"

if [[ -z "${USERNAME:-}" ]]; then
  read -r -p "USERNAME: " USERNAME
fi

if [[ -z "${PASSWORD:-}" ]]; then
  read -r -s -p "PASSWORD: " PASSWORD
  echo
fi

exec env \
  USERNAME="$USERNAME" \
  PASSWORD="$PASSWORD" \
  HTTPS_PORT="${HTTPS_PORT:-8443}" \
  IMAGE_CACHE_TTL_SECONDS="${IMAGE_CACHE_TTL_SECONDS:-3600}" \
  IMAGE_MISS_CACHE_TTL_SECONDS="${IMAGE_MISS_CACHE_TTL_SECONDS:-300}" \
  IMAGE_CACHE_MAX_ITEMS="${IMAGE_CACHE_MAX_ITEMS:-1000}" \
  IMAGE_CACHE_MAX_BYTES="${IMAGE_CACHE_MAX_BYTES:-268435456}" \
  JSON_CACHE_TTL_SECONDS="${JSON_CACHE_TTL_SECONDS:-3600}" \
  JSON_CACHE_MAX_ITEMS="${JSON_CACHE_MAX_ITEMS:-2000}" \
  JSON_CACHE_MAX_BYTES="${JSON_CACHE_MAX_BYTES:-67108864}" \
  python3 "$APP_PATH"
