#!/usr/bin/env bash
set -euo pipefail

# Configurable connection settings
TARGET_IP="${TARGET_IP:-192.168.0.206}"
TARGET_USER="${TARGET_USER:-root}"
TARGET_PASSWORD="${TARGET_PASSWORD:-}"
TARGET_DIR="${TARGET_DIR:-/userdata/system/apps/roms-api}"
SSH_PORT="${SSH_PORT:-22}"
DEPLOY_RETRIES="${DEPLOY_RETRIES:-5}"
DEPLOY_RETRY_DELAY_SECONDS="${DEPLOY_RETRY_DELAY_SECONDS:-2}"

usage() {
  cat <<'EOF'
Upload ROM API files to a remote machine via SCP.

Required:
  Set TARGET_PASSWORD (env var) or pass as 3rd positional arg.

Optional env vars:
  TARGET_IP        Default: 192.168.0.206
  TARGET_USER      Default: root
  TARGET_DIR       Default: /userdata/system/apps/roms-api
  SSH_PORT         Default: 22
  DEPLOY_RETRIES   Default: 5
  DEPLOY_RETRY_DELAY_SECONDS Default: 2

Positional args (override env vars):
  1: TARGET_IP
  2: TARGET_USER
  3: TARGET_PASSWORD
  4: TARGET_DIR

Examples:
  TARGET_IP='192.168.0.206' TARGET_USER='root' TARGET_PASSWORD='secret' TARGET_DIR='/userdata/system/apps/roms-api' ./deploy_to_target.sh
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

TARGET_IP="${1:-$TARGET_IP}"
TARGET_USER="${2:-$TARGET_USER}"
TARGET_PASSWORD="${3:-$TARGET_PASSWORD}"
TARGET_DIR="${4:-$TARGET_DIR}"

if [[ -z "$TARGET_PASSWORD" ]]; then
  echo "TARGET_PASSWORD is required." >&2
  usage
  exit 1
fi

if ! command -v sshpass >/dev/null 2>&1; then
  echo "sshpass is required for password-based SCP/SSH. Install it and retry." >&2
  exit 1
fi

if ! command -v scp >/dev/null 2>&1; then
  echo "scp is required." >&2
  exit 1
fi

if ! command -v ssh >/dev/null 2>&1; then
  echo "ssh is required." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "Ensuring remote directory exists: ${TARGET_USER}@${TARGET_IP}:${TARGET_DIR}"
SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o PubkeyAuthentication=no
  -o PreferredAuthentications=password,keyboard-interactive
  -o NumberOfPasswordPrompts=3
  -o ConnectTimeout=10
  -o ConnectionAttempts=1
  -p "$SSH_PORT"
)
SCP_OPTS=(
  -o StrictHostKeyChecking=no
  -o PubkeyAuthentication=no
  -o PreferredAuthentications=password,keyboard-interactive
  -o NumberOfPasswordPrompts=3
  -o ConnectTimeout=10
  -o ConnectionAttempts=1
  -P "$SSH_PORT"
)

retry_cmd() {
  local description="$1"
  shift
  local attempt=1
  local max_attempts="$DEPLOY_RETRIES"
  local delay="$DEPLOY_RETRY_DELAY_SECONDS"

  while true; do
    if "$@"; then
      return 0
    fi

    local exit_code=$?
    if (( attempt >= max_attempts )); then
      echo "Failed after ${attempt} attempts: ${description}" >&2
      return "$exit_code"
    fi

    echo "Attempt ${attempt}/${max_attempts} failed for ${description} (exit ${exit_code}). Retrying in ${delay}s..." >&2
    sleep "$delay"
    attempt=$((attempt + 1))
  done
}

retry_cmd "create remote directories" \
  sshpass -p "$TARGET_PASSWORD" ssh "${SSH_OPTS[@]}" "${TARGET_USER}@${TARGET_IP}" \
  "mkdir -p '$TARGET_DIR/app/templates' '$TARGET_DIR/scripts'"

echo "Uploading core app files..."
retry_cmd "upload core app files" \
  sshpass -p "$TARGET_PASSWORD" scp "${SCP_OPTS[@]}" \
  "$ROOT_DIR/README.md" \
  "$ROOT_DIR/.gitignore" \
  "${TARGET_USER}@${TARGET_IP}:${TARGET_DIR}/"

echo "Uploading app directory..."
retry_cmd "upload app directory" \
  sshpass -p "$TARGET_PASSWORD" scp "${SCP_OPTS[@]}" -r \
  "$ROOT_DIR/app/." \
  "${TARGET_USER}@${TARGET_IP}:${TARGET_DIR}/app/"

echo "Uploading scripts directory..."
retry_cmd "upload scripts directory" \
  sshpass -p "$TARGET_PASSWORD" scp "${SCP_OPTS[@]}" -r \
  "$ROOT_DIR/scripts/." \
  "${TARGET_USER}@${TARGET_IP}:${TARGET_DIR}/scripts/"

echo "Deploy complete."
echo "Remote path: ${TARGET_DIR}"
