#!/bin/sh
set -e

BATOCERA_VERSION=""
if command -v batocera-version >/dev/null 2>&1; then
  BATOCERA_VERSION=$(batocera-version 2>/dev/null | head -1 | tr -d '[:space:]')
fi

if [ -z "$BATOCERA_VERSION" ]; then
  if [ -f /usr/share/batocera/batocera.version ]; then
    BATOCERA_VERSION=$(cat /usr/share/batocera/batocera.version | head -1 | tr -d '[:space:]')
  elif [ -f /etc/batocera-release ]; then
    BATOCERA_VERSION=$(cat /etc/batocera-release | head -1 | tr -d '[:space:]')
  fi
fi

MAJOR_VERSION=$(echo "$BATOCERA_VERSION" | sed 's/^\([0-9]*\).*/\1/')

USE_LEGACY_METHOD=false
if [ -n "$MAJOR_VERSION" ] && [ "$MAJOR_VERSION" -lt 43 ] 2>/dev/null; then
  USE_LEGACY_METHOD=true
fi

echo "============================================"
echo " Batocera Drone Installer"
echo "============================================"
echo "Detected Batocera version: ${BATOCERA_VERSION:-unknown}"
echo ""

DRONE_USER="drone-app"
DRONE_GROUP="drone-app"
DRONE_UID="999"
DRONE_GID="999"

echo "---------------------------------------------"
echo " Creating dedicated ${DRONE_USER} user/group"
echo "---------------------------------------------"

if ! grep -q "^${DRONE_GROUP}:" /etc/group 2>/dev/null; then
  echo "${DRONE_GROUP}:x:${DRONE_GID}:" >> /etc/group
fi

if ! id -u "$DRONE_USER" >/dev/null 2>&1; then
  if command -v adduser >/dev/null 2>&1; then
    adduser -S -H -s /bin/sh "$DRONE_USER" 2>/dev/null || true
  fi

  if ! id -u "$DRONE_USER" >/dev/null 2>&1; then
    echo "${DRONE_USER}:x:${DRONE_UID}:${DRONE_GID}:drone-app:/userdata/system/.drone-app:/bin/sh" >> /etc/passwd
    echo "${DRONE_USER}:!:19000:0:99999:7:::" >> /etc/shadow 2>/dev/null || true
  fi
fi

if ! id -u "$DRONE_USER" >/dev/null 2>&1; then
  echo "FATAL: Could not create ${DRONE_USER}"
  exit 1
fi

echo "✓ User ready: ${DRONE_USER}"

echo ""
echo "---------------------------------------------"
echo " Applying filesystem permissions"
echo "---------------------------------------------"

mkdir -p \
  /userdata/system/.drone-app \
  /userdata/system/certs \
  /userdata/system/logs/drone-app

chown -R root:"$DRONE_GROUP" \
  /userdata/system/.drone-app \
  /userdata/system/certs \
  /userdata/system/logs/drone-app 2>/dev/null || true

chmod -R 775 \
  /userdata/system/.drone-app \
  /userdata/system/certs \
  /userdata/system/logs/drone-app 2>/dev/null || true

# ROM tree default: readable, not writable by drone-app
chown -R root:root /userdata/roms 2>/dev/null || true
find /userdata/roms -type d -exec chmod 755 {} \; 2>/dev/null || true
find /userdata/roms -type f -exec chmod 644 {} \; 2>/dev/null || true

# Writable areas for drone-app
find /userdata/roms -mindepth 1 -maxdepth 1 -type d 2>/dev/null | while read romdir; do
  for subdir in images videos manuals; do
    target="${romdir}/${subdir}"
    mkdir -p "$target"
    chown -R root:"$DRONE_GROUP" "$target" 2>/dev/null || true
    chmod -R 775 "$target" 2>/dev/null || true
  done

  gamelist="${romdir}/gamelist.xml"
  if [ -f "$gamelist" ]; then
    chown root:"$DRONE_GROUP" "$gamelist" 2>/dev/null || true
    chmod 664 "$gamelist" 2>/dev/null || true
  fi
done

# Batocera config read-only to drone-app
chown root:root /userdata/batocera.conf 2>/dev/null || true
chmod 644 /userdata/batocera.conf 2>/dev/null || true

echo "✓ Permissions applied"

if [ "$USE_LEGACY_METHOD" = false ]; then
  echo ""
  echo "Installing for Batocera v43+ ..."

  SERVICES_DIR="/userdata/system/services"
  SERVICE_FILE="${SERVICES_DIR}/DRONE_SERVER"

  mkdir -p "$SERVICES_DIR"

  cat > "$SERVICE_FILE" << 'SERVICEBLOCK'
#!/bin/sh

DRONE_USER="drone-app"
ACTION="$1"
PID_FILE="/tmp/drone-server.pid"

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

start_app() {
  (
    while ! ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; do
      sleep 5
    done

    curl -fsSL "https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main/scripts/run_now.sh" -o /tmp/run_now.sh && \
    chmod +x /tmp/run_now.sh && \
    DRONE_APP_BASE_URL="https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main" \
    run_as_drone /tmp/run_now.sh
  ) &

  echo $! > "$PID_FILE"
  echo "Web Server running on https://$(hostname).local:8443"
}

stop_app() {
  if command -v lsof >/dev/null 2>&1; then
    PIDS="$(lsof -t -i:8443 2>/dev/null || true)"
    if [ -n "$PIDS" ]; then
      kill $PIDS 2>/dev/null || true
      sleep 2
      kill -9 $PIDS 2>/dev/null || true
    fi
  fi

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
  echo ""
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
  while ! ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; do
    sleep 5
  done

  curl -fsSL "https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main/scripts/run_now.sh" -o /tmp/run_now.sh && \
  chmod +x /tmp/run_now.sh && \
  DRONE_APP_BASE_URL="https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main" \
  su -s /bin/sh -c "/tmp/run_now.sh" drone-app
) &

echo "Web Server running on https://$(hostname).local:8443"
SERVICEBLOCK

    echo "✓ Appended startup block to $CUSTOM_SH"
  fi
fi

echo ""
echo "Installation complete!"
echo "Web Server URL: https://$(hostname):8443"
echo ""
echo "drone-app can write to:"
echo "  /userdata/roms/*/images/"
echo "  /userdata/roms/*/videos/"
echo "  /userdata/roms/*/manuals/"
echo "  /userdata/roms/*/gamelist.xml"
echo "  /userdata/system/.drone-app/"
echo "  /userdata/system/certs/"
echo "  /userdata/system/logs/drone-app/"
echo ""
echo "ROM files and Batocera system config remain read-only to drone-app."