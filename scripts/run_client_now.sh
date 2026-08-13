#!/usr/bin/env bash
# Packages (sets up a local Python venv with the app's real runtime deps)
# and runs the Ports client (ports-client/) locally for manual testing.
#
# This is NOT the on-device deploy path -- see
# ports-client/scripts/vendor_deps.sh + build_release_bundle.sh for the
# per-arch bundle that actually ships to a Batocera device. This script is
# for iterating on a dev machine, the same way run_mock_server.py is a
# dev-only stand-in for a real Drone install.
#
# Pairs naturally with scripts/run_mock_server.py: a full Drone instance,
# HTTP-only on :8080, seeded with fake data -- no root, no real ROMs
# needed. If nothing is already listening at the (default) target and
# you haven't pointed this at a specific host/port yourself, this script
# starts run_mock_server.py for you and stops it again on exit -- no
# second terminal required. The defaults below target exactly that mock
# server. Override
# PORTS_CLIENT_TARGET_PORT / PORTS_CLIENT_TARGET_HTTP_ONLY / DRONE_PORTS_CLIENT_HOST
# to point at a real Drone instead (e.g. one started with run_web_now.sh).
# Deliberately NOT named HTTPS_PORT/HTTP_ONLY: those are common, generic
# names other projects also export, and `${HTTPS_PORT:-8080}` only falls
# back to 8080 when the var is truly unset -- an unrelated HTTPS_PORT
# already sitting in your shell would silently win and point this script
# at nothing listening there ("connection refused"). These target_* names
# can't collide with that.
set -euo pipefail

# Captured before any default is applied below, so we can tell "using our
# own default" apart from "the caller pointed this somewhere specific" --
# only the former is safe to auto-start a mock server for.
_TARGET_PORT_WAS_SET="${PORTS_CLIENT_TARGET_PORT+set}"
_TARGET_HOST_WAS_SET="${DRONE_PORTS_CLIENT_HOST+set}"
_TARGET_HTTP_ONLY_WAS_SET="${PORTS_CLIENT_TARGET_HTTP_ONLY+set}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLIENT_DIR="$ROOT/ports-client"
VENV_DIR="${PORTS_CLIENT_DEV_VENV_DIR:-$CLIENT_DIR/.dev-venv}"

PYTHON_BIN="${PORTS_CLIENT_DEV_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  # imgui_bundle requires Python >= 3.10; prefer a newer interpreter over
  # a possibly-older default `python3` if one is available (found the hard
  # way: this repo's own dev box defaults to 3.9, which imgui_bundle rejects).
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "No python3 interpreter found." >&2
  exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
  echo "Creating local dev venv at $VENV_DIR (using $("$PYTHON_BIN" --version 2>&1)) ..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

echo "Installing/updating ports-client dev dependencies ..."
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q -r "$CLIENT_DIR/requirements-vendor.txt"
# Dev-only convenience: bundled SDL2 binaries for a machine with no system
# SDL2 -- never vendored on-device, where Batocera's own libSDL2.so (the
# same one EmulationStation links against) is used instead. Best-effort:
# some platforms (Linux dev boxes, which usually already have system SDL2)
# may have no matching wheel at all, and that's fine.
"$VENV_DIR/bin/pip" install -q pysdl2-dll >/dev/null 2>&1 || true

# The client's session-cookie path defaults to /userdata/system/drone-app/...
# (a real Batocera path that doesn't exist here) -- point USERDATA_ROOT at a
# local, writable directory instead, mirroring run_mock_server.py's own
# local-data/ convention, so login doesn't crash trying to persist a cookie.
DEV_USERDATA_ROOT="${USERDATA_ROOT:-$ROOT/local-data/ports-client-userdata}"
mkdir -p "$DEV_USERDATA_ROOT"

TARGET_PORT="${PORTS_CLIENT_TARGET_PORT:-8080}"
TARGET_HTTP_ONLY="${PORTS_CLIENT_TARGET_HTTP_ONLY:-1}"
TARGET_HOST="${DRONE_PORTS_CLIENT_HOST:-127.0.0.1}"
TARGET_SCHEME="http"
[[ "$TARGET_HTTP_ONLY" == "1" ]] || TARGET_SCHEME="https"
USING_DEFAULT_TARGET=1
[[ -z "$_TARGET_PORT_WAS_SET" && -z "$_TARGET_HOST_WAS_SET" && -z "$_TARGET_HTTP_ONLY_WAS_SET" ]] || USING_DEFAULT_TARGET=0

_port_is_open() {
  (exec 3<>"/dev/tcp/$1/$2") >/dev/null 2>&1
}

MOCK_SERVER_PID=""
cleanup() {
  if [[ -n "$MOCK_SERVER_PID" ]]; then
    echo "Stopping the mock server (pid $MOCK_SERVER_PID) ..."
    kill "$MOCK_SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if ! _port_is_open "$TARGET_HOST" "$TARGET_PORT"; then
  if [[ "$USING_DEFAULT_TARGET" == "1" ]]; then
    echo "Nothing is listening at ${TARGET_SCHEME}://${TARGET_HOST}:${TARGET_PORT} -- starting scripts/run_mock_server.py for you ..."
    MOCK_LOG="$ROOT/local-data/run_client_now-mock-server.log"
    mkdir -p "$(dirname "$MOCK_LOG")"
    HTTPS_PORT="$TARGET_PORT" python3 "$ROOT/scripts/run_mock_server.py" > "$MOCK_LOG" 2>&1 &
    MOCK_SERVER_PID=$!
    for _ in $(seq 1 50); do
      _port_is_open "$TARGET_HOST" "$TARGET_PORT" && break
      sleep 0.2
    done
    if ! _port_is_open "$TARGET_HOST" "$TARGET_PORT"; then
      echo "Mock server did not come up in time; see $MOCK_LOG" >&2
      exit 1
    fi
    echo "Mock server ready (pid $MOCK_SERVER_PID, log at $MOCK_LOG)."
  else
    echo "Nothing is listening at ${TARGET_SCHEME}://${TARGET_HOST}:${TARGET_PORT}." >&2
    echo "Start a Drone there first (e.g. scripts/run_web_now.sh, or a real device), or unset" >&2
    echo "PORTS_CLIENT_TARGET_PORT/PORTS_CLIENT_TARGET_HTTP_ONLY/DRONE_PORTS_CLIENT_HOST to use the default mock server instead." >&2
    exit 1
  fi
fi

echo "Running the Ports client locally against ${TARGET_SCHEME}://${TARGET_HOST}:${TARGET_PORT} (Ctrl+C to quit) ..."
echo "(override with PORTS_CLIENT_TARGET_PORT / PORTS_CLIENT_TARGET_HTTP_ONLY / DRONE_PORTS_CLIENT_HOST)"
cd "$CLIENT_DIR"
env \
  PYTHONPATH="$CLIENT_DIR" \
  USERDATA_ROOT="$DEV_USERDATA_ROOT" \
  DRONE_PORTS_CLIENT_HOST="$TARGET_HOST" \
  HTTPS_PORT="$TARGET_PORT" \
  HTTP_ONLY="$TARGET_HTTP_ONLY" \
  PORTS_CLIENT_DEV_WINDOWED="${PORTS_CLIENT_DEV_WINDOWED:-1}" \
  PORTS_CLIENT_DEV_BACKEND="${PORTS_CLIENT_DEV_BACKEND:-}" \
  "$VENV_DIR/bin/python3" main.py
# Not exec'd above: this script must keep running after the client exits
# so the `cleanup` trap can stop the mock server it started, if any.
