#!/bin/sh
set -e

DRONE_USER="drone-app"
DRONE_GROUP="drone-app"
DRONE_UID="999"
DRONE_GID="999"
WORK_DIR="/userdata/system/drone-app"

BATOCERA_VERSION=""
if command -v batocera-version >/dev/null 2>&1; then
  BATOCERA_VERSION="$(batocera-version 2>/dev/null | head -1 | tr -d '[:space:]')"
fi

if [ -z "$BATOCERA_VERSION" ]; then
  if [ -f /usr/share/batocera/batocera.version ]; then
    BATOCERA_VERSION="$(cat /usr/share/batocera/batocera.version | head -1 | tr -d '[:space:]')"
  elif [ -f /etc/batocera-release ]; then
    BATOCERA_VERSION="$(cat /etc/batocera-release | head -1 | tr -d '[:space:]')"
  fi
fi

MAJOR_VERSION="$(echo "$BATOCERA_VERSION" | sed 's/^\([0-9]*\).*/\1/')"

USE_LEGACY_METHOD=false
if [ -n "$MAJOR_VERSION" ] && [ "$MAJOR_VERSION" -lt 43 ] 2>/dev/null; then
  USE_LEGACY_METHOD=true
fi

echo "============================================"
echo " Batocera Drone Installer"
echo "============================================"
echo "Detected Batocera version: ${BATOCERA_VERSION:-unknown}"
echo ""

mkdir -p "$WORK_DIR"

echo ""
echo "Permissions are applied at service startup via"
echo "the ensure_permissions() function in DRONE_SERVER."

if [ "$USE_LEGACY_METHOD" = false ]; then
  echo ""
  echo "Installing for Batocera v43+ ..."

  SERVICES_DIR="/userdata/system/services"
  SERVICE_FILE="${SERVICES_DIR}/DRONE_SERVER"

  mkdir -p "$SERVICES_DIR"

  cat > "$SERVICE_FILE" << 'SERVICEBLOCK'
#!/bin/sh

DRONE_USER="drone-app"
DRONE_GROUP="drone-app"
DRONE_UID="999"
DRONE_GID="999"
WORK_DIR="/userdata/system/drone-app"
ACTION="$1"
PID_FILE="/tmp/drone-server.pid"
CONTROL_PID_FILE="/tmp/drone-server-control.pid"
CONTROL_DIR="/userdata/system/drone-app/control"
STARTUP_LOG="/userdata/system/logs/drone-app/startup.log"

ensure_drone_user() {
  echo "[drone-service] Ensuring ${DRONE_USER} user/group exists..."

  if grep -q "^${DRONE_GROUP}:" /etc/group 2>/dev/null; then
    sed -i "s#^${DRONE_GROUP}:.*#${DRONE_GROUP}:x:${DRONE_GID}:#" /etc/group
  else
    echo "${DRONE_GROUP}:x:${DRONE_GID}:" >> /etc/group
  fi

  if grep -q "^${DRONE_USER}:" /etc/passwd 2>/dev/null; then
    sed -i "s#^${DRONE_USER}:.*#${DRONE_USER}:x:${DRONE_UID}:${DRONE_GID}:drone-app:${WORK_DIR}:/bin/sh#" /etc/passwd
  else
    echo "${DRONE_USER}:x:${DRONE_UID}:${DRONE_GID}:drone-app:${WORK_DIR}:/bin/sh" >> /etc/passwd
  fi

  if [ -f /etc/shadow ]; then
    if grep -q "^${DRONE_USER}:" /etc/shadow 2>/dev/null; then
      sed -i "s#^${DRONE_USER}:.*#${DRONE_USER}:*:19000:0:99999:7:::#" /etc/shadow
    else
      echo "${DRONE_USER}:*:19000:0:99999:7:::" >> /etc/shadow
    fi
  fi

  echo "[drone-service] ✓ User ${DRONE_USER} ready"
}

ensure_permissions() {
  echo "[drone-service] Applying filesystem permissions..."

  mkdir -p \
    /userdata/system/drone-app \
    /userdata/system/drone-app/app \
    /userdata/system/drone-app/content \
    "$CONTROL_DIR" \
    /userdata/system/drone-app/certs \
    /userdata/system/certs \
    /userdata/system/logs/drone-app

  chown -R root:"$DRONE_GROUP" \
    /userdata/system/drone-app \
    /userdata/system/certs \
    /userdata/system/logs/drone-app 2>/dev/null || true

  chmod -R 775 \
    /userdata/system/drone-app \
    "$CONTROL_DIR" \
    /userdata/system/certs \
    /userdata/system/logs/drone-app 2>/dev/null || true

  chown "$DRONE_USER":"$DRONE_GROUP" /userdata/system/drone-app/rom_metadata_cache.sqlite3* 2>/dev/null || true
  chmod 600 /userdata/system/drone-app/rom_metadata_cache.sqlite3* 2>/dev/null || true

  chmod o+rx /userdata/system 2>/dev/null || true
  chmod o+rx /userdata/system/configs 2>/dev/null || true

  if [ -d /userdata/system/configs/PCSX2 ]; then
    chmod -R o+rX /userdata/system/configs/PCSX2 2>/dev/null || true
  fi

  # Gameplay event spool. Drone's procfs monitor writes one file per game
  # start/stop here and drains it after successful Overmind delivery.
  mkdir -p /userdata/system/drone-app/game-events 2>/dev/null || true
  chown root:"$DRONE_GROUP" /userdata/system/drone-app/game-events 2>/dev/null || true
  chmod 2775 /userdata/system/drone-app/game-events 2>/dev/null || true

  # Remove the legacy EmulationStation hook. Gameplay detection now watches
  # emulatorlauncher directly through /proc from the Drone process.
  rm -f /userdata/system/scripts/drone-game-event.sh 2>/dev/null || true

  repair_rom_content_permissions() {
    romdir="$1"
    [ -d "$romdir" ] || return 0
    system_name="$(basename "$romdir")"

    chown root:"$DRONE_GROUP" "$romdir" 2>/dev/null || true
    chmod 2775 "$romdir" 2>/dev/null || true

    for subdir in images videos manuals downloaded_images covers media; do
      target="${romdir}/${subdir}"
      mkdir -p "$target"
      chown root:"$DRONE_GROUP" "$target" 2>/dev/null || true
      chmod 2775 "$target" 2>/dev/null || true
      find "$target" -type d -exec chown root:"$DRONE_GROUP" {} \; -exec chmod 2775 {} \; 2>/dev/null || true
      find "$target" -type f -exec chown root:"$DRONE_GROUP" {} \; -exec chmod 664 {} \; 2>/dev/null || true
    done

    gamelist="${romdir}/gamelist.xml"
    if [ -f "$gamelist" ]; then
      chown root:"$DRONE_GROUP" "$gamelist" 2>/dev/null || true
      chmod 664 "$gamelist" 2>/dev/null || true
    fi

    if [ -d "$romdir" ] && [ ! -f "$gamelist" ]; then
      touch "$gamelist" 2>/dev/null || true
      if [ -f "$gamelist" ] && [ ! -s "$gamelist" ]; then
        printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' '<gameList />' > "$gamelist" 2>/dev/null || true
      fi
      chown root:"$DRONE_GROUP" "$gamelist" 2>/dev/null || true
      chmod 664 "$gamelist" 2>/dev/null || true
    fi
  }

  if [ "${DRONE_REPAIR_ROM_PERMISSIONS:-0}" = "1" ]; then
    find /userdata/roms -mindepth 1 -maxdepth 1 -type d 2>/dev/null | while read romdir; do
      repair_rom_content_permissions "$romdir"
    done
  else
    find /userdata/roms -mindepth 1 -maxdepth 1 -type d -exec chown root:"$DRONE_GROUP" {} \; -exec chmod 2775 {} \; 2>/dev/null || true
    echo "[drone-service] Skipped recursive ROM permission repair; set DRONE_REPAIR_ROM_PERMISSIONS=1 to run it."
  fi

  chown root:"$DRONE_GROUP" /userdata/system/batocera.conf 2>/dev/null || true
  chmod 664 /userdata/system/batocera.conf 2>/dev/null || true

  echo "[drone-service] ✓ Permissions applied"
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

ensure_low_port_binding() {
  primary_port="${HTTPS_PORT:-443}"
  compat_ports="${DRONE_COMPAT_HTTPS_PORTS:-8443}"
  case " ${primary_port} ${compat_ports} " in
    *" 443 "*)
      if [ -w /proc/sys/net/ipv4/ip_unprivileged_port_start ]; then
        current_start="$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || echo 1024)"
        if [ "${current_start:-1024}" -gt 0 ] 2>/dev/null; then
          echo 0 > /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || true
          echo "[drone-service] Enabled unprivileged binding for HTTPS port 443"
        fi
      else
        echo "[drone-service] Cannot adjust unprivileged port binding; HTTPS port 443 may fail for ${DRONE_USER}"
      fi
      ;;
  esac
}

run_as_drone() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$DRONE_USER" -- "$@"
  elif command -v chpst >/dev/null 2>&1; then
    chpst -u "$DRONE_USER" "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$DRONE_USER" "$@"
  else
    su -s /bin/sh -c "$*" "$DRONE_USER"
  fi
}

run_as_drone_shell() {
  command="$1"
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$DRONE_USER" -- sh -c "$command"
  elif command -v chpst >/dev/null 2>&1; then
    chpst -u "$DRONE_USER" sh -c "$command"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$DRONE_USER" sh -c "$command"
  else
    su -s /bin/sh -c "$command" "$DRONE_USER"
  fi
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
    "$WORK_DIR/app/api_routes.py" \
    "$WORK_DIR/app/ui_routes.py" \
    "$WORK_DIR/app/route_config.py"; do
    if [ ! -s "$required_file" ]; then
      echo "[drone-service] Local Drone app validation failed: missing or empty ${required_file}"
      return 1
    fi
  done

  PYTHONPATH="$WORK_DIR" python3 - <<'PY'
import importlib

required = {
    "app.api_routes": "ApiRoutesMixin",
    "app.ui_routes": "UiRoutesMixin",
}

for module_name, symbol in required.items():
    module = importlib.import_module(module_name)
    if not hasattr(module, symbol):
        raise ImportError(f"{module_name} does not export {symbol}")

importlib.import_module("app.drone_api")
PY
}

launch_drone() {
  if [ -f "$WORK_DIR/app/main.py" ] && [ -f "$WORK_DIR/app/drone_api.py" ]; then
    if validate_local_app; then
      echo "[drone-service] Launching local Drone app from ${WORK_DIR}..."
      run_as_drone_shell "cd '$WORK_DIR' && env PYTHONPATH='$WORK_DIR' HTTPS_PORT='${HTTPS_PORT:-443}' DRONE_COMPAT_HTTPS_PORTS='${DRONE_COMPAT_HTTPS_PORTS:-8443}' ROMS_ROOT='${ROMS_ROOT:-/userdata/roms}' BIOS_ROOT='${BIOS_ROOT:-/userdata/bios}' TLS_SELF_SIGNED_DIR='${TLS_SELF_SIGNED_DIR:-/userdata/system/certs}' LOG_DIR='${LOG_DIR:-/userdata/system/logs/drone-app}' LOG_MAX_BYTES='${LOG_MAX_BYTES:-5242880}' LOG_BACKUP_COUNT='${LOG_BACKUP_COUNT:-5}' DRONE_LOG_UNAUTHORIZED_REQUESTS='${DRONE_LOG_UNAUTHORIZED_REQUESTS:-0}' DRONE_UNAUTH_RATE_LIMIT_ENABLED='${DRONE_UNAUTH_RATE_LIMIT_ENABLED:-1}' DRONE_UNAUTH_RATE_LIMIT_REQUESTS='${DRONE_UNAUTH_RATE_LIMIT_REQUESTS:-60}' DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS='${DRONE_UNAUTH_RATE_LIMIT_WINDOW_SECONDS:-60}' ROM_METADATA_POLL_SECONDS='${ROM_METADATA_POLL_SECONDS:-900}' ROM_METADATA_INITIAL_DELAY_SECONDS='${ROM_METADATA_INITIAL_DELAY_SECONDS:-60}' ROM_METADATA_PROGRESS_SECONDS='${ROM_METADATA_PROGRESS_SECONDS:-30}' ROM_METADATA_PROGRESS_FILES='${ROM_METADATA_PROGRESS_FILES:-250}' ROM_METADATA_UPLOAD_CHUNK_SIZE='${ROM_METADATA_UPLOAD_CHUNK_SIZE:-250}' ROM_METADATA_HASH_IO_YIELD_SECONDS='${ROM_METADATA_HASH_IO_YIELD_SECONDS:-0.05}' ROM_METADATA_HASH_ROMS_ENABLED='${ROM_METADATA_HASH_ROMS_ENABLED:-1}' IMAGE_CACHE_TTL_SECONDS='${IMAGE_CACHE_TTL_SECONDS:-3600}' IMAGE_MISS_CACHE_TTL_SECONDS='${IMAGE_MISS_CACHE_TTL_SECONDS:-300}' IMAGE_CACHE_MAX_ITEMS='${IMAGE_CACHE_MAX_ITEMS:-1000}' IMAGE_CACHE_MAX_BYTES='${IMAGE_CACHE_MAX_BYTES:-134217728}' JSON_CACHE_TTL_SECONDS='${JSON_CACHE_TTL_SECONDS:-3600}' JSON_CACHE_MAX_ITEMS='${JSON_CACHE_MAX_ITEMS:-1000}' JSON_CACHE_MAX_BYTES='${JSON_CACHE_MAX_BYTES:-33554432}' OVERMIND_DRONE_TOKEN='${OVERMIND_DRONE_TOKEN:-}' OVERMIND_POLL_SECONDS='${OVERMIND_POLL_SECONDS:-60}' OVERMIND_SPEED_SAMPLE_SECONDS='${OVERMIND_SPEED_SAMPLE_SECONDS:-600}' python3 -m app.main"
      return "$?"
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
  run_as_drone bash "$runner"
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

service_control_worker() {
  while true; do
    if [ -f "$CONTROL_DIR/restart-emulationstation.request" ]; then
      rm -f "$CONTROL_DIR/restart-emulationstation.request"
      echo "[drone-service] EmulationStation restart requested by Drone app."
      restart_emulationstation_as_root
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
    ensure_drone_user
    ensure_permissions
    ensure_dns_fallback
    ensure_low_port_binding
    start_control_worker

    supervise_drone
  ) >> "$STARTUP_LOG" 2>&1 &

  echo $! > "$PID_FILE"
  echo "Web Server running on https://$(hostname).local (compatibility: https://$(hostname).local:8443)"
  echo "Startup log: $STARTUP_LOG"
}

stop_app() {
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
  *)
    echo "Usage: $0 {start|stop|restart}"
    exit 1
    ;;
esac
SERVICEBLOCK

  chmod +x "$SERVICE_FILE"

  echo "✓ Created service: $SERVICE_FILE"

  echo ""
  echo "Downloading latest Drone app bundle..."
  RUNNER="/tmp/drone-run-now-install.$$"
  if curl -fsSL --connect-timeout 10 --max-time 120 -o "$RUNNER" https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/run_now.sh; then
    chmod 755 "$RUNNER" 2>/dev/null || true
    APP_WAS_RUNNING=false
    if [ -f /tmp/drone-server.pid ]; then
      EXISTING_PID="$(cat /tmp/drone-server.pid 2>/dev/null || true)"
      if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
        APP_WAS_RUNNING=true
      fi
    fi
    if lsof -i:"${HTTPS_PORT:-443}" >/dev/null 2>&1; then
      APP_WAS_RUNNING=true
    fi
    "$SERVICE_FILE" stop >/dev/null 2>&1 || true
    DRONE_APP_STAGE_ONLY=1 \
      DRONE_APP_WORK_DIR="$WORK_DIR" \
      DRONE_APP_ARCHIVE_URL="${DRONE_APP_ARCHIVE_URL:-https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/drone-app.tar.gz}" \
      bash "$RUNNER"
    rm -f "$RUNNER"
    chown -R root:"$DRONE_GROUP" "$WORK_DIR" 2>/dev/null || true
    chmod -R 775 "$WORK_DIR" 2>/dev/null || true
    echo "✓ Updated Drone app bundle in $WORK_DIR"
    if [ "$APP_WAS_RUNNING" = true ]; then
      echo "Restarting Drone service with updated app bundle..."
      "$SERVICE_FILE" start
    fi
  else
    rm -f "$RUNNER"
    echo "Could not download latest Drone app bundle now; service will download it on next startup if the local app is invalid."
  fi

  echo "Start now with:"
  echo "  $SERVICE_FILE start"

else
  echo ""
  echo "Installing for Batocera < v43 ..."

  CUSTOM_SH="/userdata/system/custom.sh"

  if [ ! -f "$CUSTOM_SH" ]; then
    echo '#!/bin/sh' > "$CUSTOM_SH"
    chmod +x "$CUSTOM_SH"
  elif [ ! -x "$CUSTOM_SH" ]; then
    chmod +x "$CUSTOM_SH"
  fi

  if grep -q "Batocera-Fleet-Federation/batocera.drone" "$CUSTOM_SH" 2>/dev/null; then
    echo "Drone startup block already exists in $CUSTOM_SH. Skipping."
  else
    cat >> "$CUSTOM_SH" << 'SERVICEBLOCK'

# Run Drone Web Server: https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone
(
  max_attempts="${DRONE_NETWORK_WAIT_ATTEMPTS:-12}"
  attempt=1
  while [ "$attempt" -le "$max_attempts" ]; do
    if curl -fsI --connect-timeout 5 --max-time 10 https://github.com >/dev/null 2>&1; then
      break
    fi
    if ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then
      break
    fi
    attempt=$((attempt + 1))
    sleep 5
  done

  curl -fsSL --connect-timeout 10 --max-time 120 https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/run_now.sh | su -s /bin/sh -c "bash" drone-app
) &

SERVICEBLOCK

    echo "✓ Appended startup block to $CUSTOM_SH"
  fi
fi

echo ""
echo "Installation complete!"
echo "Web Server URL: https://$(hostname)"
echo ""
echo "drone-app can read:"
echo "  /userdata/system/configs/PCSX2/**"
echo ""
echo "drone-app can write to:"
echo "  /userdata/roms/*/images/"
echo "  /userdata/roms/*/videos/"
echo "  /userdata/roms/*/manuals/"
echo "  /userdata/roms/*/{downloaded_images,covers,media}/"
echo "  /userdata/roms/*/gamelist.xml"
echo "  /userdata/system/drone-app/"
echo "  /userdata/system/drone-app/certs/"
echo "  /userdata/system/certs/"
echo "  /userdata/system/logs/drone-app/"
echo ""
echo "ROM files and Batocera system config remain read-only to drone-app."
