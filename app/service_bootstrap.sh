#!/bin/sh
# Versioned Drone service bootstrap.
#
# This script lives in the auto-updating app bundle (app/) and contains ALL of
# the service-side logic: runtime directory setup, DNS preparation, privileged
# controls (Kiosk/volume/restart), and the Drone app supervisor. The installed
# /userdata/system/services/DRONE_SERVER is a thin shim that only ensures the
# bundle is present and then delegates here, so new Drone releases apply their
# service-side changes automatically on the next service restart -- no need to
# re-run batocera_install.sh on every machine.

WORK_DIR="/userdata/system/drone-app"
ACTION="$1"
PID_FILE="/tmp/drone-server.pid"
CONTROL_PID_FILE="/tmp/drone-server-control.pid"
INPUT_MONITOR_PID_FILE="/tmp/drone-input-activity-monitor.pid"
CONTROL_DIR="/userdata/system/drone-app/control"
INPUT_ACTIVITY_FILE="/userdata/system/drone-app/control/last-input-activity"
STARTUP_LOG="/userdata/system/logs/drone-app/startup.log"
STARTUP_UPDATE_ATTEMPTED=0

ensure_rom_write_access_all() {
  echo "[drone-service] Drone runs as root; ROM permission repair is not required."
  return 0
}

ensure_permissions() {
  echo "[drone-service] Ensuring Drone runtime directories..."

  mkdir -p \
    /userdata/system/drone-app \
    /userdata/system/drone-app/app \
    /userdata/system/drone-app/content \
    "$CONTROL_DIR" \
    /userdata/system/drone-app/certs \
    /userdata/system/certs \
    /userdata/system/logs/drone-app

  # Gameplay event spool. Drone's procfs monitor writes one file per game
  # start/stop here and drains it after successful Overmind delivery.
  mkdir -p /userdata/system/drone-app/game-events 2>/dev/null || true

  # Remove the legacy EmulationStation hook. Gameplay detection now watches
  # emulatorlauncher directly through /proc from the Drone process.
  rm -f /userdata/system/scripts/drone-game-event.sh 2>/dev/null || true

  echo "[drone-service] ✓ Runtime directories ready"
}

ensure_dns_fallback() {
  if [ -w /etc/resolv.conf ]; then
    if ! grep -q "^nameserver 1\\.1\\.1\\.1$" /etc/resolv.conf 2>/dev/null; then
      echo "nameserver 1.1.1.1" >> /etc/resolv.conf
    fi
    if ! grep -q "^nameserver 8\\.8\\.8\\.8$" /etc/resolv.conf 2>/dev/null; then
      echo "nameserver 8.8.8.8" >> /etc/resolv.conf
    fi
  fi
}

run_as_root_shell() {
  command="$1"
  sh -c "$command"
}

wait_for_network() {
  max_attempts="${DRONE_NETWORK_WAIT_ATTEMPTS:-12}"
  attempt=1
  echo "[drone-service] Waiting for network connectivity..."
  while [ "$attempt" -le "$max_attempts" ]; do
    if curl -fsI --connect-timeout 5 --max-time 10 https://github.com >/dev/null 2>&1; then
      echo "[drone-service] ✓ Network ready"
      return 0
    fi
    if ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then
      echo "[drone-service] ✓ Network ready"
      return 0
    fi
    echo "[drone-service] Network not ready (${attempt}/${max_attempts}); retrying..."
    attempt=$((attempt + 1))
    sleep 5
  done
  echo "[drone-service] Network check timed out; attempting startup anyway"
  return 0
}

validate_local_app() {
  for required_file in \
    "$WORK_DIR/app/main.py" \
    "$WORK_DIR/app/drone_api.py" \
    "$WORK_DIR/app/web/api_routes.py" \
    "$WORK_DIR/app/web/ui_routes.py" \
    "$WORK_DIR/app/web/route_config.py"; do
    if [ ! -s "$required_file" ]; then
      echo "[drone-service] Local Drone app validation failed: missing or empty ${required_file}"
      return 1
    fi
  done

  PYTHONPATH="$WORK_DIR" python3 - <<'PY'
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
}

stage_latest_app_once() {
  if [ "${DRONE_UPDATE_ON_STARTUP:-1}" != "1" ]; then
    return 0
  fi
  runner="/tmp/drone-startup-update.$$"
  echo "[drone-service] Checking for the latest Drone app bundle..."
  wait_for_network
  if ! curl -fsSL --connect-timeout 10 --max-time 45 -o "$runner" https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/run_now.sh; then
    rm -f "$runner"
    echo "[drone-service] Latest bundle check failed; continuing with the validated local app."
    return 0
  fi
  chmod 755 "$runner" 2>/dev/null || true
  if DRONE_APP_STAGE_ONLY=1 \
      DRONE_APP_WORK_DIR="$WORK_DIR" \
      DRONE_APP_ARCHIVE_URL="${DRONE_APP_ARCHIVE_URL:-https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/drone-app.tar.gz}" \
      bash "$runner"; then
    echo "[drone-service] Latest Drone app bundle staged."
  else
    echo "[drone-service] Latest bundle staging failed; validating the local app before launch."
  fi
  rm -f "$runner"
}

launch_drone() {
  if [ -f "$WORK_DIR/app/main.py" ] && [ -f "$WORK_DIR/app/drone_api.py" ]; then
    if validate_local_app; then
      if [ "$STARTUP_UPDATE_ATTEMPTED" -eq 0 ]; then
        STARTUP_UPDATE_ATTEMPTED=1
        stage_latest_app_once
      fi
      if ! validate_local_app; then
        echo "[drone-service] Staged Drone app failed validation; downloading a clean bundle."
      else
        echo "[drone-service] Launching local Drone app from ${WORK_DIR}..."
        run_as_root_shell "cd '$WORK_DIR' && env PYTHONPATH='$WORK_DIR' HTTPS_PORT='${HTTPS_PORT:-443}' DRONE_COMPAT_HTTPS_PORTS='${DRONE_COMPAT_HTTPS_PORTS:-8443}' ROMS_ROOT='${ROMS_ROOT:-/userdata/roms}' BIOS_ROOT='${BIOS_ROOT:-/userdata/bios}' TLS_SELF_SIGNED_DIR='${TLS_SELF_SIGNED_DIR:-/userdata/system/certs}' LOG_DIR='${LOG_DIR:-/userdata/system/logs/drone-app}' LOG_MAX_BYTES='${LOG_MAX_BYTES:-5242880}' LOG_BACKUP_COUNT='${LOG_BACKUP_COUNT:-5}' DRONE_LOG_UNAUTHORIZED_REQUESTS='${DRONE_LOG_UNAUTHORIZED_REQUESTS:-0}' DRONE_UNAUTH_RATE_LIMIT_ENABLED='${DRONE_UNAUTH_RATE_LIMIT_ENABLED:-1}' DRONE_UNAUTH_RATE_LIMIT_REQUESTS='${DRONE_UNAUTH_RATE_LIMIT_REQUESTS:-60}' DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS='${DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS:-60}' ROM_METADATA_POLL_SECONDS='${ROM_METADATA_POLL_SECONDS:-900}' ROM_METADATA_INITIAL_DELAY_SECONDS='${ROM_METADATA_INITIAL_DELAY_SECONDS:-60}' ROM_METADATA_PROGRESS_SECONDS='${ROM_METADATA_PROGRESS_SECONDS:-30}' ROM_METADATA_PROGRESS_FILES='${ROM_METADATA_PROGRESS_FILES:-250}' ROM_METADATA_UPLOAD_CHUNK_SIZE='${ROM_METADATA_UPLOAD_CHUNK_SIZE:-250}' ROM_METADATA_HASH_IO_YIELD_SECONDS='${ROM_METADATA_HASH_IO_YIELD_SECONDS:-0.05}' ROM_METADATA_HASH_ROMS_ENABLED='${ROM_METADATA_HASH_ROMS_ENABLED:-1}' IMAGE_CACHE_TTL_SECONDS='${IMAGE_CACHE_TTL_SECONDS:-3600}' IMAGE_MISS_CACHE_TTL_SECONDS='${IMAGE_MISS_CACHE_TTL_SECONDS:-300}' IMAGE_CACHE_MAX_ITEMS='${IMAGE_CACHE_MAX_ITEMS:-1000}' IMAGE_CACHE_MAX_BYTES='${IMAGE_CACHE_MAX_BYTES:-134217728}' JSON_CACHE_TTL_SECONDS='${JSON_CACHE_TTL_SECONDS:-3600}' JSON_CACHE_MAX_ITEMS='${JSON_CACHE_MAX_ITEMS:-1000}' JSON_CACHE_MAX_BYTES='${JSON_CACHE_MAX_BYTES:-33554432}' OVERMIND_DRONE_TOKEN='${OVERMIND_DRONE_TOKEN:-}' OVERMIND_POLL_SECONDS='${OVERMIND_POLL_SECONDS:-60}' OVERMIND_SPEED_SAMPLE_SECONDS='${OVERMIND_SPEED_SAMPLE_SECONDS:-600}' python3 -m app.main"
        return "$?"
      fi
    fi
    echo "[drone-service] Local Drone app import check failed; downloading a fresh app bundle."
  fi

  runner="/tmp/drone-run-now.$$"
  echo "[drone-service] Downloading and launching Drone app..."
  wait_for_network
  if ! curl -fsSL --connect-timeout 10 --max-time 120 -o "$runner" https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/run_now.sh; then
    rm -f "$runner"
    echo "[drone-service] Failed to download Drone launcher"
    return 1
  fi
  chmod 755 "$runner" 2>/dev/null || true
  export DRONE_APP_ARCHIVE_URL="${DRONE_APP_ARCHIVE_URL:-https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/drone-app.tar.gz}"
  bash "$runner"
  exit_code="$?"
  rm -f "$runner"
  return "$exit_code"
}

request_host_reboot() {
  echo "[drone-service] Remote reboot requested by Drone app."
  if [ -x /sbin/reboot ]; then
    /sbin/reboot
    return
  fi
  if [ -x /usr/sbin/reboot ]; then
    /usr/sbin/reboot
    return
  fi
  if command -v reboot >/dev/null 2>&1; then
    reboot
    return
  fi
  if command -v shutdown >/dev/null 2>&1; then
    shutdown -r now
    return
  fi
  echo "[drone-service] Unable to reboot: reboot/shutdown command was not found."
}

restart_emulationstation_as_root() {
  if [ -x /etc/init.d/S31emulationstation ]; then
    /etc/init.d/S31emulationstation restart
    return
  fi
  if command -v batocera-es-swissknife >/dev/null 2>&1; then
    batocera-es-swissknife --restart
    return
  fi
  echo "[drone-service] Unable to restart EmulationStation: restart command was not found."
}

kill_emulator_as_root() {
  if command -v batocera-es-swissknife >/dev/null 2>&1; then
    batocera-es-swissknife --emukill
    return "$?"
  fi
  echo "[drone-service] Unable to exit the running game: batocera-es-swissknife command was not found."
  return 1
}

set_screen_mode_as_root() {
  mode="$1"
  helper="$WORK_DIR/app/set_screen_mode.py"
  if [ ! -f "$helper" ]; then
    echo "[drone-service] Unable to set screen mode: helper was not found."
    return 1
  fi
  python3 "$helper" "$mode"
}

set_volume_as_root() {
  level="$1"
  helper="$WORK_DIR/app/set_volume.py"
  if [ ! -f "$helper" ]; then
    echo "[drone-service] Unable to set volume: helper was not found."
    return 1
  fi
  python3 "$helper" "$level"
}

# Signature of the on-disk bootstrap script. The Drone app self-updates by
# re-exec'ing its own app process in place, which leaves this service layer
# running the OLD code (old ensure_permissions + control worker) until the next
# full service restart. We use this signature so the control worker can detect a
# shipped service-layer update and adopt it on its own.
_script_signature() {
  stat -c '%Y %s' "$WORK_DIR/app/service_bootstrap.sh" 2>/dev/null \
    || md5sum "$WORK_DIR/app/service_bootstrap.sh" 2>/dev/null | awk '{print $1}' \
    || echo "unknown"
}

# Run the input-activity monitor as root so it can read /dev/input/event* and
# record the last input time for idle automations (e.g. lowering the volume).
# Idempotent: does nothing if the monitor is already running.
start_input_activity_monitor() {
  helper="$WORK_DIR/app/input_activity_monitor.py"
  [ -f "$helper" ] || return 0
  if [ -f "$INPUT_MONITOR_PID_FILE" ]; then
    existing_pid="$(cat "$INPUT_MONITOR_PID_FILE" 2>/dev/null || true)"
    if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
      return 0
    fi
  fi
  python3 "$helper" "$INPUT_ACTIVITY_FILE" >/dev/null 2>&1 &
  echo $! > "$INPUT_MONITOR_PID_FILE"
  echo "[drone-service] Input activity monitor started (pid $(cat "$INPUT_MONITOR_PID_FILE" 2>/dev/null))."
}

service_control_worker() {
  worker_script_sig="$(_script_signature)"
  start_input_activity_monitor
  while true; do
    # Keep the input-activity monitor alive (it may exit if all input devices
    # disappear, or after a bundle update ships a new helper).
    start_input_activity_monitor
    # If the bundle's service_bootstrap.sh changed under us (an app self-update
    # shipped a new service layer), re-exec into it so new root-side logic and
    # control commands take effect without waiting for a full DRONE_SERVER restart.
    if [ "$(_script_signature)" != "$worker_script_sig" ]; then
      echo "[drone-service] Service bundle changed; re-execing control worker to adopt the update."
      # Stop the monitor so the re-exec'd worker starts the (possibly updated) helper.
      if [ -f "$INPUT_MONITOR_PID_FILE" ]; then
        kill "$(cat "$INPUT_MONITOR_PID_FILE" 2>/dev/null)" 2>/dev/null || true
        rm -f "$INPUT_MONITOR_PID_FILE"
      fi
      exec sh "$WORK_DIR/app/service_bootstrap.sh" control-worker
    fi
    if [ -f "$CONTROL_DIR/restart-emulationstation.request" ]; then
      rm -f "$CONTROL_DIR/restart-emulationstation.request"
      echo "[drone-service] EmulationStation restart requested by Drone app."
      restart_emulationstation_as_root
    fi
    if [ -f "$CONTROL_DIR/kill-emulator.request" ]; then
      kill_result="$CONTROL_DIR/kill-emulator.result"
      rm -f "$CONTROL_DIR/kill-emulator.request" "$kill_result"
      echo "[drone-service] Emulator exit requested by Drone app (idle game-exit automation)."
      if kill_output="$(kill_emulator_as_root 2>&1)"; then
        printf '%s\n' "ok" > "$kill_result"
      else
        printf 'Privileged emulator exit failed: %s\n' "$(printf '%s' "$kill_output" | tr '\n' ' ' | cut -c1-300)" > "$kill_result"
      fi
      echo "[drone-service] Emulator exit result: ${kill_output}"
    fi
    for mode in full kiosk kid; do
      request="$CONTROL_DIR/set-screen-mode-${mode}.request"
      result="$CONTROL_DIR/set-screen-mode-${mode}.result"
      if [ -f "$request" ]; then
        rm -f "$request" "$result"
        echo "[drone-service] Screen mode ${mode} requested by Drone app."
        # Capture the helper's combined output so a failure reports the real reason
        # back to the Drone app (and on to the Overmind action result) instead of a
        # generic message.
        if helper_output="$(set_screen_mode_as_root "$mode" 2>&1)"; then
          printf '%s\n' "ok" > "$result"
        else
          printf 'Privileged screen mode %s operation failed: %s\n' "$mode" "$(printf '%s' "$helper_output" | tr '\n' ' ' | cut -c1-300)" > "$result"
        fi
        echo "[drone-service] Screen mode ${mode} result: ${helper_output}"
      fi
    done
    perm_request="$CONTROL_DIR/repair-rom-permissions.request"
    perm_result="$CONTROL_DIR/repair-rom-permissions.result"
    if [ -f "$perm_request" ]; then
      # First line (optional) names a single system; empty means all systems.
      perm_system="$(head -n 1 "$perm_request" 2>/dev/null | tr -d '\r\n' | tr -cd 'A-Za-z0-9._-')"
      rm -f "$perm_request" "$perm_result"
      echo "[drone-service] ROM permission request ignored because Drone runs as root (system='${perm_system:-all}')."
      if perm_output="$(ensure_rom_write_access_all "$perm_system" 2>&1)"; then
        printf '%s\n' "ok" > "$perm_result"
      else
        printf 'ROM permission check failed: %s\n' "$(printf '%s' "$perm_output" | tr '\n' ' ' | cut -c1-300)" > "$perm_result"
      fi
    fi
    volume_request="$CONTROL_DIR/set-volume.request"
    volume_result="$CONTROL_DIR/set-volume.result"
    if [ -f "$volume_request" ]; then
      level="$(head -n 1 "$volume_request" 2>/dev/null | tr -cd '0-9')"
      rm -f "$volume_request" "$volume_result"
      echo "[drone-service] Volume change to ${level:-?} requested by Drone app."
      if [ -n "$level" ] && volume_output="$(set_volume_as_root "$level" 2>&1)"; then
        printf '%s\n' "ok" > "$volume_result"
      else
        printf 'Privileged volume operation failed: %s\n' "$(printf '%s' "${volume_output:-no level provided}" | tr '\n' ' ' | cut -c1-300)" > "$volume_result"
      fi
      echo "[drone-service] Volume change result: ${volume_output:-no level provided}"
    fi
    sleep 1
  done
}

start_control_worker() {
  service_control_worker &
  echo $! > "$CONTROL_PID_FILE"
}

supervise_drone() {
  restart_delay="${DRONE_RESTART_DELAY_SECONDS:-10}"
  restart_enabled="${DRONE_SERVICE_RESTART:-1}"
  remote_reboot_exit_code="${DRONE_REMOTE_REBOOT_EXIT_CODE:-76}"

  while true; do
    launch_started="$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)"
    echo "[drone-service] Launch attempt started at ${launch_started}"
    launch_drone
    exit_code="$?"
    launch_ended="$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date)"
    echo "[drone-service] Drone app process exited at ${launch_ended} with code ${exit_code}"

    if [ "$exit_code" -eq 0 ]; then
      exit 0
    fi

    if [ "$exit_code" -eq "$remote_reboot_exit_code" ]; then
      request_host_reboot
      exit "$exit_code"
    fi

    if [ "$restart_enabled" != "1" ]; then
      exit "$exit_code"
    fi

    echo "[drone-service] Restarting Drone app in ${restart_delay}s..."
    sleep "$restart_delay"
  done
}

start_app() {
  mkdir -p "$(dirname "$STARTUP_LOG")"
  if [ -f "$PID_FILE" ]; then
    existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
      echo "Drone service supervisor already running: pid=${existing_pid}"
      echo "Startup log: $STARTUP_LOG"
      exit 0
    fi
  fi
  HTTPS_PORT="${HTTPS_PORT:-443}"
  if lsof -i:"$HTTPS_PORT" >/dev/null 2>&1; then
    echo "Drone App already appears to be listening on port ${HTTPS_PORT}"
    echo "Startup log: $STARTUP_LOG"
    exit 0
  fi
  (
    ensure_permissions
    ensure_dns_fallback
    start_control_worker

    supervise_drone
  ) >> "$STARTUP_LOG" 2>&1 &

  echo $! > "$PID_FILE"
  echo "Web Server running on https://$(hostname).local (compatibility: https://$(hostname).local:8443)"
  echo "Startup log: $STARTUP_LOG"
}

stop_app() {
  if [ -f "$INPUT_MONITOR_PID_FILE" ]; then
    kill "$(cat "$INPUT_MONITOR_PID_FILE" 2>/dev/null)" 2>/dev/null || true
    rm -f "$INPUT_MONITOR_PID_FILE"
  fi
  if [ -f "$CONTROL_PID_FILE" ]; then
    kill "$(cat "$CONTROL_PID_FILE" 2>/dev/null)" 2>/dev/null || true
  fi
  if [ -f "$PID_FILE" ]; then
    kill "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null || true
  fi
  HTTPS_PORT="${HTTPS_PORT:-443}"
  kill -9 $(lsof -t -i:"$HTTPS_PORT") 2>/dev/null || true
  compat_ports="$(printf '%s' "${DRONE_COMPAT_HTTPS_PORTS:-8443}" | tr ',;' '  ')"
  for compat_port in $compat_ports; do
    kill -9 $(lsof -t -i:"$compat_port") 2>/dev/null || true
  done
  rm -f "$PID_FILE"
  rm -f "$CONTROL_PID_FILE"
}

case "$ACTION" in
  start)
    start_app
    ;;
  stop)
    stop_app
    ;;
  restart)
    stop_app
    start_app
    ;;
  control-worker)
    # Re-entry point used by the control worker's own self-re-exec after a bundle
    # update. Resume processing privileged control requests with the new code.
    # Runs in the foreground (it has replaced the
    # previous worker process via exec, so CONTROL_PID_FILE still points at it).
    service_control_worker
    ;;
  *)
    echo "Usage: $0 {start|stop|restart}"
    exit 1
    ;;
esac
