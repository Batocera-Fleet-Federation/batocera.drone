#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Download all ROMs from the ROM API into per-system folders.

Usage:
  ./download_all_roms.sh --base-url URL --username USER --password PASS [options]

Required:
  --base-url URL       Example: https://72.176.228.250
  --username USER      HTTP Basic Auth username
  --password PASS      HTTP Basic Auth password

Options:
  --output-dir DIR     Root output directory (default: ./downloads)
  --overwrite          Overwrite existing files instead of skipping
  --verify-tls         Verify TLS certificate (default is off for self-signed certs)
  -h, --help           Show this help
EOF
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

BASE_URL=""
USERNAME=""
PASSWORD=""
OUTPUT_DIR="./downloads"
OVERWRITE=0
VERIFY_TLS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url)
      BASE_URL="${2:-}"
      shift 2
      ;;
    --username)
      USERNAME="${2:-}"
      shift 2
      ;;
    --password)
      PASSWORD="${2:-}"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="${2:-}"
      shift 2
      ;;
    --overwrite)
      OVERWRITE=1
      shift
      ;;
    --verify-tls)
      VERIFY_TLS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$BASE_URL" || -z "$USERNAME" || -z "$PASSWORD" ]]; then
  echo "Missing required arguments." >&2
  usage
  exit 1
fi

require_cmd curl
require_cmd jq

BASE_URL="${BASE_URL%/}"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR_ABS="$(cd "$OUTPUT_DIR" && pwd)"

CURL_TLS_FLAG="-k"
if [[ "$VERIFY_TLS" -eq 1 ]]; then
  CURL_TLS_FLAG=""
fi

curl_json() {
  local path="$1"
  local url="${BASE_URL}${path}"

  if [[ -n "$CURL_TLS_FLAG" ]]; then
    curl -fsS "$CURL_TLS_FLAG" -u "${USERNAME}:${PASSWORD}" "$url"
  else
    curl -fsS -u "${USERNAME}:${PASSWORD}" "$url"
  fi
}

download_file() {
  local path="$1"
  local destination="$2"
  local tmp_path="${destination}.part"
  local url="${BASE_URL}${path}"

  mkdir -p "$(dirname "$destination")"
  if [[ -n "$CURL_TLS_FLAG" ]]; then
    curl -fsS "$CURL_TLS_FLAG" -u "${USERNAME}:${PASSWORD}" "$url" -o "$tmp_path"
  else
    curl -fsS -u "${USERNAME}:${PASSWORD}" "$url" -o "$tmp_path"
  fi
  mv -f "$tmp_path" "$destination"
}

systems_json="$(curl_json "/systems")"
system_count="$(jq '.systems | length' <<<"$systems_json")"
if [[ "$system_count" -eq 0 ]]; then
  echo "No systems returned by API."
  exit 0
fi

downloaded_count=0
skipped_count=0

while IFS=$'\t' read -r system_name system_name_enc; do
  [[ -z "$system_name" ]] && continue
  system_dir="${OUTPUT_DIR_ABS}/${system_name}"
  mkdir -p "$system_dir"
  echo "System: $system_name"

  roms_json="$(curl_json "/systems/${system_name_enc}")"
  rom_count="$(jq '.roms | length' <<<"$roms_json")"
  if [[ "$rom_count" -eq 0 ]]; then
    echo "  No ROMs found."
    continue
  fi

  while IFS=$'\t' read -r rom_name unique_id unique_id_enc; do
    [[ -z "$rom_name" || -z "$unique_id" ]] && continue
    output_path="${system_dir}/${rom_name}"

    if [[ -f "$output_path" && "$OVERWRITE" -eq 0 ]]; then
      echo "  Skip: $rom_name (exists)"
      skipped_count=$((skipped_count + 1))
      continue
    fi

    echo "  Download: $rom_name"
    if download_file "/systems/${system_name_enc}/${unique_id_enc}" "$output_path"; then
      downloaded_count=$((downloaded_count + 1))
    else
      echo "    Failed download: $rom_name" >&2
      rm -f "${output_path}.part"
    fi
  done < <(
    jq -r '.roms[] | [.name, .unique_id, (.unique_id|@uri)] | @tsv' <<<"$roms_json"
  )
done < <(
  jq -r '.systems[] | [.name, (.name|@uri)] | @tsv' <<<"$systems_json"
)

echo "Done. Downloaded: ${downloaded_count}, Skipped: ${skipped_count}, Output: ${OUTPUT_DIR_ABS}"
