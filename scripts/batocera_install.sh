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

# ── No legacy cleanup ───────────────────────────────────────────────
# If upgrading from a previous version that used chattr +i, remove
# those flags manually before running this installer:
#   chattr -R -i /userdata/system 2>/dev/null || true
#   chattr -i /userdata/batocera.conf 2>/dev/null || true
#   find /userdata/roms -type f -exec chattr -i {} \; 2>/dev/null || true

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

# ── Create dedicated drone user (MUST succeed) ──────────────────────
# The drone-app user is required — the app will only run under this user,
# never as root. If this fails, installation aborts.
DRONE_USER="drone-app"
if ! id -u "$DRONE_USER" >/dev/null 2>&1; then
    echo "---------------------------------------------"
    echo " Creating dedicated '${DRONE_USER}' user"
    echo "---------------------------------------------"
    echo ""
    CREATED=false
    # BusyBox (Batocera) adduser: -S = system account, -H = no home dir
    # Use /bin/sh as shell so privilege-drop tools (su, runuser, etc.)
    # can execute commands without PAM authentication errors.
    if command -v adduser >/dev/null 2>&1; then
        adduser -S -H -s /bin/sh "$DRONE_USER" && CREATED=true
    fi
    if [ "$CREATED" = false ] && command -v useradd >/dev/null 2>&1; then
        useradd -r -s /bin/sh "$DRONE_USER" && CREATED=true
    fi
    if [ "$CREATED" = false ]; then
        # Last resort: manual /etc/passwd entry
        if ! grep -q "^${DRONE_USER}:" /etc/passwd 2>/dev/null; then
            echo "${DRONE_USER}:x:999:999:drone-app:/:/bin/false" >> /etc/passwd
            CREATED=true
        fi
    fi
    if [ "$CREATED" = false ]; then
        echo ""
        echo "FATAL: Could not create user '${DRONE_USER}'."
        echo "The drone-app user is required for process isolation."
        echo "Installation aborted."
        exit 1
    fi
    echo "✓ Created system user: $DRONE_USER"
fi

# ── Set up process-level permissions ──────────────────────────────────
# The Python app runs exclusively as the 'drone-app' user via su.
# Standard Unix file ownership and permissions limit what that user
# can write to. Root retains full access to everything.
echo ""
echo "---------------------------------------------"
echo " Process-Level Permissions"
echo "---------------------------------------------"
echo ""

# Grant the drone-app user write access to ROM asset directories
echo "  Granting write access to ROM assets for ${DRONE_USER}..."
find /userdata/roms -mindepth 1 -maxdepth 1 -type d 2>/dev/null | while read romdir; do
    for subdir in images videos manuals; do
        target="${romdir}/${subdir}"
        if [ -d "$target" ]; then
            chown -R "$DRONE_USER:$DRONE_USER" "$target" 2>/dev/null || true
            chmod -R u+rwX,go+rX "$target" 2>/dev/null || true
        fi
    done
    gamelist="${romdir}/gamelist.xml"
    if [ -f "$gamelist" ]; then
        chown "$DRONE_USER:$DRONE_USER" "$gamelist" 2>/dev/null || true
        chmod u+rw,go+r "$gamelist" 2>/dev/null || true
    fi
done

# Create and set ownership for app working directory
WORK_DIR="${DRONE_APP_WORK_DIR:-/userdata/system/.drone-app}"
mkdir -p "$WORK_DIR"
chown -R "$DRONE_USER:$DRONE_USER" "$WORK_DIR" 2>/dev/null || true

# Create and set ownership for certs and logs directories
mkdir -p /userdata/system/certs /userdata/system/logs/drone-app
chown -R "$DRONE_USER:$DRONE_USER" /userdata/system/certs /userdata/system/logs/drone-app 2>/dev/null || true

echo ""
echo "✓ Permissions applied"
echo "  The Python app runs as user '${DRONE_USER}' with write access only to:"
echo "    /userdata/roms/*/{images,videos,manuals}/"
echo "    /userdata/roms/*/gamelist.xml"
echo "    /userdata/system/.drone-app/"
echo "    /userdata/system/certs/"
echo "    /userdata/system/logs/drone-app/"
echo "  Root retains full access to ALL files and directories."

# ── Install startup method ───────────────────────────────────────────
if [ "$USE_LEGACY_METHOD" = false ]; then
  # ── Batocera v43+ method ──────────────────────────────────────────
  echo ""
  echo "Installing for Batocera v43+ ..."
  SERVICES_DIR="/userdata/system/services"
  SERVICE_FILE="${SERVICES_DIR}/DRONE_SERVER"

  # Create services directory if it doesn't exist
  mkdir -p "$SERVICES_DIR"

  # Write the service file
  # Note: 'SERVICEBLOCK' is single-quoted to prevent variable expansion
  # at install time — the service file uses its own variables at runtime.
  cat > "$SERVICE_FILE" << 'SERVICEBLOCK'
#!/bin/sh

DRONE_USER="drone-app"
ACTION="$1"

run_as_drone() {
  # Find a working privilege-drop command on this system
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$DRONE_USER" -- "$@"
  elif command -v chpst >/dev/null 2>&1; then
    chpst -u "$DRONE_USER" "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$DRONE_USER" "$@"
  else
    # Last resort: BusyBox su with non-PAM flags
    su -s /bin/sh -p -c "$*" "$DRONE_USER"
  fi
}

start_app() {
  (
    while ! ping -c 1 -W 2 8.8.8.8 > /dev/null 2>&1; do
      sleep 5
    done

    curl -fsSL "https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main/scripts/run_now.sh" -o /tmp/run_now.sh && \
    chmod +x /tmp/run_now.sh && \
    DRONE_APP_BASE_URL="https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main" \
    run_as_drone /tmp/run_now.sh
  ) &

  echo "Web Server running on https://$(hostname).local:8443"
}

stop_app() {
  kill -9 $(lsof -t -i:8443)
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
  echo ""
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

    # Find a working privilege-drop command on this system
    DRONE_USER="drone-app"
    DROP_CMD=""
    if command -v runuser >/dev/null 2>&1; then
      DROP_CMD="runuser -u $DRONE_USER -- /tmp/run_now.sh"
    elif command -v chpst >/dev/null 2>&1; then
      DROP_CMD="chpst -u $DRONE_USER /tmp/run_now.sh"
    elif command -v sudo >/dev/null 2>&1; then
      DROP_CMD="sudo -u $DRONE_USER /tmp/run_now.sh"
    else
      DROP_CMD="su -s /bin/sh -p -c '/tmp/run_now.sh' $DRONE_USER"
    fi
    curl -fsSL "https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main/scripts/run_now.sh" -o /tmp/run_now.sh && \
    chmod +x /tmp/run_now.sh && \
    DRONE_APP_BASE_URL="https://raw.githubusercontent.com/Batocera-Fleet-Federation/batocera.drone/main" \
    eval "$DROP_CMD"
  ) &

  echo "Web Server running on https://$(hostname).local:8443"
SERVICEBLOCK

    echo ""
    echo "✓ Appended startup block to $CUSTOM_SH"
    echo ""
    echo "The drone web server will start automatically on boot (via custom.sh)."
    echo ""
  fi
fi

# ── Done ────────────────────────────────────────────────────────────
echo ""
echo "Installation complete!"
echo ""
echo "Web Server URL: https://$(hostname):8443"
echo ""
echo "To uninstall:"
echo "  userdel ${DRONE_USER} 2>/dev/null || true"
if [ "$USE_LEGACY_METHOD" = false ]; then
  echo "  rm -f ${SERVICES_DIR}/DRONE_SERVER"
else
  echo "  Edit $CUSTOM_SH and remove the Drone Web Server block"
fi
echo ""
echo "  Note: Permissions on /userdata/roms/*/{images,videos,manuals}/"
echo "  and gamelist.xml will persist until manually reverted with chown."