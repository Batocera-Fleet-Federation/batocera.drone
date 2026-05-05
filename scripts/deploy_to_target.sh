#!/usr/bin/env bash
set -euo pipefail

# Configurable connection settings
TARGET_IP="${TARGET_IP:-192.168.0.206}"
TARGET_USER="${TARGET_USER:-root}"
TARGET_PASSWORD="${TARGET_PASSWORD:-}"
TARGET_DIR="${TARGET_DIR:-/userdata/system/apps/roms-api}"
SSH_PORT="${SSH_PORT:-22}"

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

Positional args (override env vars):
  1: TARGET_IP
  2: TARGET_USER
  3: TARGET_PASSWORD
  4: TARGET_DIR

Examples:
  TARGET_PASSWORD='secret' ./deploy_to_target.sh
  ./deploy_to_target.sh 192.168.0.206 root 'secret'
  ./deploy_to_target.sh 192.168.0.206 root 'secret' /userdata/system/apps/roms-api
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
  -o NumberOfPasswordPrompts=1
  -p "$SSH_PORT"
)
SCP_OPTS=(
  -o StrictHostKeyChecking=no
  -o PubkeyAuthentication=no
  -o PreferredAuthentications=password,keyboard-interactive
  -o NumberOfPasswordPrompts=1
  -P "$SSH_PORT"
)

sshpass -p "$TARGET_PASSWORD" ssh "${SSH_OPTS[@]}" "${TARGET_USER}@${TARGET_IP}" \
  "mkdir -p '$TARGET_DIR/app/templates' '$TARGET_DIR/scripts'"

echo "Uploading core app files..."
sshpass -p "$TARGET_PASSWORD" scp "${SCP_OPTS[@]}" \
  "$ROOT_DIR/README.md" \
  "$ROOT_DIR/.gitignore" \
  "${TARGET_USER}@${TARGET_IP}:${TARGET_DIR}/"

echo "Uploading app directory..."
sshpass -p "$TARGET_PASSWORD" scp "${SCP_OPTS[@]}" -r \
  "$ROOT_DIR/app/." \
  "${TARGET_USER}@${TARGET_IP}:${TARGET_DIR}/app/"

echo "Uploading scripts directory..."
sshpass -p "$TARGET_PASSWORD" scp "${SCP_OPTS[@]}" -r \
  "$ROOT_DIR/scripts/." \
  "${TARGET_USER}@${TARGET_IP}:${TARGET_DIR}/scripts/"

echo "Deploy complete."
echo "Remote path: ${TARGET_DIR}"
