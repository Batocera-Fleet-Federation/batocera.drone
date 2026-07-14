#!/bin/sh
set -e

DRONE_USER="drone-app"
DRONE_GROUP="drone-app"
WORK_DIR="/userdata/system/drone-app"
LOG_DIR="/userdata/system/logs/drone-app"
SERVICE_SERVER="/userdata/system/services/DRONE_SERVER"
SERVICE_APP="/userdata/system/services/DRONE_APP"
SERVICE_FILES="${SERVICE_SERVER} ${SERVICE_APP}"
TS_DIR="/userdata/system/tailscale"
TS_SERVICE="/userdata/system/services/DRONE_TAILNET"
TS_SOCKET="/var/run/tailscale/tailscaled.sock"
CUSTOM_SH="/userdata/system/custom.sh"
GAME_EVENT_HOOK="/userdata/system/scripts/drone-game-event.sh"
PID_FILE="/tmp/drone-server.pid"
CONTROL_PID_FILE="/tmp/drone-server-control.pid"

echo "============================================"
echo " Batocera Drone Uninstaller"
echo "============================================"
echo ""

stop_drone() {
  stopped=false

  for service_file in $SERVICE_FILES; do
    if [ -x "$service_file" ]; then
      echo "Stopping Drone service: $service_file"
      "$service_file" stop >/dev/null 2>&1 || true
      stopped=true
    fi
  done

  if command -v pkill >/dev/null 2>&1; then
    pkill -u "$DRONE_USER" 2>/dev/null || true
  fi

  rm -f "$CONTROL_PID_FILE" "$PID_FILE" /tmp/drone-run-now.* /tmp/drone-run-now-install.* 2>/dev/null || true

  if [ "$stopped" = true ]; then
    echo "✓ Stopped Drone service"
  else
    echo "No v43+ Drone service file was found."
  fi
}

remove_service_files() {
  removed=false
  for service_file in $SERVICE_FILES; do
    if [ -e "$service_file" ]; then
      rm -f "$service_file"
      echo "✓ Removed service file: $service_file"
      removed=true
    fi
  done

  if [ "$removed" = false ]; then
    echo "No v43+ service file needed removal."
  fi
}

remove_legacy_custom_sh_block() {
  if [ ! -f "$CUSTOM_SH" ]; then
    echo "No legacy custom.sh file was found."
    return 0
  fi

  if ! grep -q "Batocera-Fleet-Federation/batocera.drone" "$CUSTOM_SH" 2>/dev/null; then
    echo "No legacy Drone startup block was found in $CUSTOM_SH."
    return 0
  fi

  tmp_file="${CUSTOM_SH}.drone-uninstall.$$"
  awk '
    /# Run Drone Web Server: https:\/\/raw\.githubusercontent\.com\/Batocera-Fleet-Federation\/batocera\.drone/ {
      skip=1
      next
    }
    skip && /^\) &$/ {
      skip=0
      next
    }
    !skip {
      print
    }
  ' "$CUSTOM_SH" > "$tmp_file"

  cat "$tmp_file" > "$CUSTOM_SH"
  rm -f "$tmp_file"
  chmod +x "$CUSTOM_SH" 2>/dev/null || true
  echo "✓ Removed legacy Drone startup block from $CUSTOM_SH"
}

remove_drone_files() {
  if [ -d "$WORK_DIR" ]; then
    rm -rf "$WORK_DIR"
    echo "✓ Removed Drone app directory: $WORK_DIR"
  else
    echo "Drone app directory was not present."
  fi

  if [ -d "$LOG_DIR" ]; then
    rm -rf "$LOG_DIR"
    echo "✓ Removed Drone logs: $LOG_DIR"
  else
    echo "Drone log directory was not present."
  fi

  if [ -f "$GAME_EVENT_HOOK" ]; then
    rm -f "$GAME_EVENT_HOOK"
    echo "✓ Removed EmulationStation game-event hook: $GAME_EVENT_HOOK"
  fi
}

remove_drone_account() {
  if command -v userdel >/dev/null 2>&1; then
    userdel "$DRONE_USER" 2>/dev/null || true
  fi

  if grep -q "^${DRONE_USER}:" /etc/passwd 2>/dev/null; then
    sed -i "/^${DRONE_USER}:/d" /etc/passwd 2>/dev/null || true
  fi

  if grep -q "^${DRONE_GROUP}:" /etc/group 2>/dev/null; then
    sed -i "/^${DRONE_GROUP}:/d" /etc/group 2>/dev/null || true
  fi

  if [ -f /etc/shadow ] && grep -q "^${DRONE_USER}:" /etc/shadow 2>/dev/null; then
    sed -i "/^${DRONE_USER}:/d" /etc/shadow 2>/dev/null || true
  fi

  echo "✓ Removed ${DRONE_USER} account/group entries when present"
}

detect_install_method() {
  detected="none"
  for service_file in $SERVICE_FILES; do
    if [ -e "$service_file" ]; then
      detected="v43+ service"
    fi
  done

  if [ -f "$CUSTOM_SH" ] && grep -q "Batocera-Fleet-Federation/batocera.drone" "$CUSTOM_SH" 2>/dev/null; then
    if [ "$detected" = "none" ]; then
      detected="legacy custom.sh"
    else
      detected="${detected} and legacy custom.sh"
    fi
  fi

  echo "Detected install method: $detected"
  echo ""
}

remove_tailscale_mesh() {
  if [ "${DRONE_KEEP_TAILSCALE:-0}" = "1" ]; then
    echo "Keeping the Tailscale mesh install (DRONE_KEEP_TAILSCALE=1)."
    return 0
  fi
  if [ ! -e "$TS_SERVICE" ] && [ ! -d "$TS_DIR" ]; then
    echo "No Drone tailnet install was found."
    return 0
  fi
  # Best-effort logout releases this node's key so it does not linger as a
  # dead device in the tailnet admin console.
  if [ -x "$TS_DIR/bin/tailscale" ]; then
    "$TS_DIR/bin/tailscale" --socket="$TS_SOCKET" logout 2>/dev/null || true
  fi
  if [ -x "$TS_SERVICE" ]; then
    "$TS_SERVICE" stop >/dev/null 2>&1 || true
  fi
  if command -v batocera-services >/dev/null 2>&1; then
    batocera-services disable DRONE_TAILNET >/dev/null 2>&1 || true
  fi
  rm -f "$TS_SERVICE"
  rm -rf "$TS_DIR"
  if [ -f "$CUSTOM_SH" ] && grep -q "Start Drone tailnet mesh" "$CUSTOM_SH" 2>/dev/null; then
    tmp_file="${CUSTOM_SH}.drone-tailnet-uninstall.$$"
    awk '
      /# Start Drone tailnet mesh: Batocera-Fleet-Federation\/batocera\.drone/ {
        skip=1
        next
      }
      skip && /^\) &$/ {
        skip=0
        next
      }
      !skip {
        print
      }
    ' "$CUSTOM_SH" > "$tmp_file"
    cat "$tmp_file" > "$CUSTOM_SH"
    rm -f "$tmp_file"
    echo "✓ Removed tailnet startup block from $CUSTOM_SH"
  fi
  echo "✓ Removed Drone tailnet install (service, binaries, and state)"
}

detect_install_method
stop_drone
remove_service_files
remove_legacy_custom_sh_block
remove_tailscale_mesh
remove_drone_files
remove_drone_account

echo ""
echo "Uninstall complete."
echo "ROM files, artwork folders, gamelist.xml files, and their current permissions were not changed."
