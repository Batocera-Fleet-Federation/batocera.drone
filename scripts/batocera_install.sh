#!/bin/sh
# -------------------------------------------------------------------
# batocera_install.sh – Batocera Drone auto-start installer
#
# This script is downloaded from Batocera and executed to install
# the drone web server as a service on Batocera.
#
# Usage (on Batocera):
#   curl -fsSL "https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main/scripts/batocera_install.sh" -o /tmp/batocera_install.sh && \
#   chmod +x /tmp/batocera_install.sh && /tmp/batocera_install.sh
#
# For v43+ Batocera:
#   Creates /userdata/system/services/DRONE_SERVER
# For < v43 Batocera:
#   Appends startup block to /userdata/system/custom.sh
# -------------------------------------------------------------------
set -e

# ── Detect Batocera version ─────────────────────────────────────────
BATOCERA_VERSION=""
# Try the batocera-version command first (most reliable)
if command -v batocera-version >/dev/null 2>&1; then
  BATOCERA_VERSION=$(batocera-version 2>/dev/null | head -1 | tr -d '[:space:]')
fi
# Fall back to version files if command not available
if [ -z "$BATOCERA_VERSION" ]; then
  if [ -f /usr/share/batocera/batocera.version ]; then
    BATOCERA_VERSION=$(cat /usr/share/batocera/batocera.version | head -1 | tr -d '[:space:]')
  elif [ -f /etc/batocera-release ]; then
    BATOCERA_VERSION=$(cat /etc/batocera-release | head -1 | tr -d '[:space:]')
  fi
fi

# Extract leading numeric major version (handles "43av", "43.1", "2024.43", etc.)
MAJOR_VERSION=$(echo "$BATOCERA_VERSION" | sed 's/^\([0-9]*\).*/\1/')

# Default to legacy path if we can't determine version
USE_LEGACY_METHOD=false
if [ -n "$MAJOR_VERSION" ] && [ "$MAJOR_VERSION" -lt 43 ] 2>/dev/null; then
  USE_LEGACY_METHOD=true
elif [ -z "$MAJOR_VERSION" ]; then
  # Cannot determine version; assume v43+ (safer default for modern Batocera)
  USE_LEGACY_METHOD=false
fi

echo "============================================"
echo " Batocera Drone Installer"
echo "============================================"
echo ""
echo "Detected Batocera version: ${BATOCERA_VERSION:-unknown}"
echo ""

if [ "$USE_LEGACY_METHOD" = false ]; then
  # ── Batocera v43+ method ──────────────────────────────────────────
  echo "Installing for Batocera v43+ ..."
  SERVICES_DIR="/userdata/system/services"
  SERVICE_FILE="${SERVICES_DIR}/DRONE_SERVER"

  # Create services directory if it doesn't exist
  mkdir -p "$SERVICES_DIR"

  # Write the service file
  cat > "$SERVICE_FILE" << 'SERVICEBLOCK'
#!/bin/sh

ACTION="$1"

start_app() {
  (
    while ! ping -c 1 -W 2 8.8.8.8 > /dev/null 2>&1; do
      sleep 5
    done

    curl -fsSL "https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main/scripts/run_now.sh" -o /tmp/run_now.sh && \
    chmod +x /tmp/run_now.sh && \
    ROM_API_BASE_URL="https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main" \
    /tmp/run_now.sh
  ) &

  echo "Web Server running on https://$(hostname):8443"
}

stop_app() {
  # TODO: Fix this so it doesn't blow all python apps away
  pkill python3
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

  echo ""
  echo "✓ Created service: $SERVICE_FILE"
  echo ""
  echo "The drone web server will start automatically on boot."
  echo "Restart Batocera or run the following to start now:"
  echo ""
  echo "    $SERVICE_FILE start"
  echo ""

else
  # ── Legacy Batocera (< v43) method ──────────────────────────────
  echo "Installing for Batocera < v43 ..."
  CUSTOM_SH="/userdata/system/custom.sh"

  # Ensure custom.sh exists and is executable
  if [ ! -f "$CUSTOM_SH" ]; then
    echo '#!/bin/sh' > "$CUSTOM_SH"
    chmod +x "$CUSTOM_SH"
    echo "✓ Created $CUSTOM_SH"
  elif [ ! -x "$CUSTOM_SH" ]; then
    chmod +x "$CUSTOM_SH"
    echo "✓ Made $CUSTOM_SH executable"
  fi

  # Check if already installed to avoid duplicates
  if grep -q "Batocera-Fleet-Federation/batocera.drone" "$CUSTOM_SH" 2>/dev/null; then
    echo ""
    echo "⚠ Drone web server entry already found in $CUSTOM_SH. Skipping."
    echo "  Remove the existing block and re-run to reinstall."
    echo ""
  else
    # Append the startup block
    cat >> "$CUSTOM_SH" << 'SERVICEBLOCK'

# Run Drone Web Server: https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone
  (
    while ! ping -c 1 -W 2 8.8.8.8 > /dev/null 2>&1; do
      sleep 5
    done

    curl -fsSL "https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main/scripts/run_now.sh" -o /tmp/run_now.sh && \
    chmod +x /tmp/run_now.sh && \
    ROM_API_BASE_URL="https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main" \
    /tmp/run_now.sh
  ) &

  echo "Web Server running on https://$(hostname):8443"
SERVICEBLOCK

    echo ""
    echo "✓ Appended startup block to $CUSTOM_SH"
    echo ""
    echo "The drone web server will start automatically on boot (via custom.sh)."
    echo ""
  fi
fi

echo "Installation complete!"
echo ""
echo "Web Server URL: https://$(hostname):8443"
echo ""
echo "To uninstall:"
if [ "$USE_LEGACY_METHOD" = false ]; then
  echo "  rm -f ${SERVICES_DIR}/DRONE_SERVER"
else
  echo "  Edit $CUSTOM_SH and remove the Drone Web Server block"
fi