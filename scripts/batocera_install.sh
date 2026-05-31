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
    /userdata/system/drone-app/certs \
    /userdata/system/certs \
    /userdata/system/logs/drone-app

  chown -R root:"$DRONE_GROUP" \
    /userdata/system/drone-app \
    /userdata/system/certs \
    /userdata/system/logs/drone-app 2>/dev/null || true

  chmod -R 775 \
    /userdata/system/drone-app \
    /userdata/system/certs \
    /userdata/system/logs/drone-app 2>/dev/null || true

  chmod o+rx /userdata/system 2>/dev/null || true
  chmod o+rx /userdata/system/configs 2>/dev/null || true

  if [ -d /userdata/system/configs/PCSX2 ]; then
    chmod -R o+rX /userdata/system/configs/PCSX2 2>/dev/null || true
  fi

  find /userdata/roms -mindepth 1 -maxdepth 1 -type d 2>/dev/null | while read romdir; do
    system_name="$(basename "$romdir")"

    for subdir in images videos manuals; do
      target="${romdir}/${subdir}"
      mkdir -p "$target"
      chown root:"$DRONE_GROUP" "$target" 2>/dev/null || true
      chmod 775 "$target" 2>/dev/null || true
    done

    gamelist="${romdir}/gamelist.xml"
    if [ -f "$gamelist" ]; then
      chown root:"$DRONE_GROUP" "$gamelist" 2>/dev/null || true
      chmod 664 "$gamelist" 2>/dev/null || true
    fi
  done

  chown root:root /userdata/batocera.conf 2>/dev/null || true
  chmod 644 /userdata/batocera.conf 2>/dev/null || true

  echo "[drone-service] ✓ Permissions applied"
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

launch_drone() {
  runner="/tmp/drone-run-now.$$"
  echo "[drone-service] Downloading and launching Drone app..."
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

supervise_drone() {
  restart_delay="${DRONE_RESTART_DELAY_SECONDS:-10}"
  restart_enabled="${DRONE_SERVICE_RESTART:-1}"

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
  if lsof -i:8443 >/dev/null 2>&1; then
    echo "Drone App already appears to be listening on port 8443"
    echo "Startup log: $STARTUP_LOG"
    exit 0
  fi
  (
    ensure_drone_user
    ensure_permissions
    wait_for_network

    supervise_drone
  ) >> "$STARTUP_LOG" 2>&1 &

  echo $! > "$PID_FILE"
  echo "Web Server running on https://$(hostname).local:8443"
  echo "Startup log: $STARTUP_LOG"
}

stop_app() {
  if [ -f "$PID_FILE" ]; then
    kill "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null || true
  fi
  kill -9 $(lsof -t -i:8443) 2>/dev/null || true
  rm -f "$PID_FILE"
}

case "$ACTION" in
  start)
    start_app
    ;;
  stop)
    stop_app
    ;;
  *)
    echo "Usage: $0 {start|stop}"
    exit 1
    ;;
esac
SERVICEBLOCK

  chmod +x "$SERVICE_FILE"

  echo "✓ Created service: $SERVICE_FILE"
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
echo "Web Server URL: https://$(hostname):8443"
echo ""
echo "drone-app can read:"
echo "  /userdata/system/configs/PCSX2/**"
echo ""
echo "drone-app can write to:"
echo "  /userdata/roms/*/images/"
echo "  /userdata/roms/*/videos/"
echo "  /userdata/roms/*/manuals/"
echo "  /userdata/roms/*/gamelist.xml"
echo "  /userdata/system/drone-app/"
echo "  /userdata/system/drone-app/certs/"
echo "  /userdata/system/certs/"
echo "  /userdata/system/logs/drone-app/"
echo ""
echo "ROM files and Batocera system config remain read-only to drone-app."
