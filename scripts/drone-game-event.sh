#!/bin/sh
# Batocera EmulationStation event hook for the Drone fleet agent.
#
# Installed to /userdata/system/scripts/ by batocera_install.sh. Batocera's
# EmulationStation executes every script in that directory on each UI event,
# passing the event name as $1 followed by event-specific parameters (the ROM
# path is among them for game launches). We only react to game start/stop and
# record one small JSON file per event into a spool directory that the Drone app
# drains on its next heartbeat and forwards to Overmind.
#
# Design notes:
#  - Runs as whatever user EmulationStation runs as (root on Batocera). The spool
#    dir is setgid + group-writable (group drone-app) so the Drone app user can
#    delete files it has processed.
#  - One file per event => no log rotation, no read cursor, no double-sends.
#  - Must never block or fail the launch: every path exits 0 quickly.

EVENT="$1"
SPOOL_DIR="/userdata/system/drone-app/game-events"

# Map Batocera event-name variants (they have shifted across versions).
case "$EVENT" in
  game-start|gameStart|game-launch) KIND="start" ;;
  game-end|gameStop|game-stop)      KIND="end" ;;
  *) exit 0 ;;
esac

# Locate the ROM path among the arguments. Argument order varies by Batocera
# version, so prefer an explicit /userdata/roms path, then any existing file.
ROM=""
for arg in "$@"; do
  case "$arg" in
    /userdata/roms/*) ROM="$arg"; break ;;
  esac
done
if [ -z "$ROM" ]; then
  for arg in "$@"; do
    if [ -f "$arg" ]; then ROM="$arg"; break; fi
  done
fi
[ -z "$ROM" ] && exit 0

NOW="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"

# Minimal JSON string escaping (backslash and double-quote).
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
ROM_ESC="$(esc "$ROM")"

mkdir -p "$SPOOL_DIR" 2>/dev/null || exit 0

# Write to a temp name then rename so the Drone never reads a half-written file.
TMP="$SPOOL_DIR/.$$.$(date +%s%N 2>/dev/null || date +%s).tmp"
FINAL="$SPOOL_DIR/$(date +%s%N 2>/dev/null || date +%s)-$$-${KIND}.json"
printf '{"event":"%s","played_at":"%s","rom_path":"%s"}\n' "$KIND" "$NOW" "$ROM_ESC" > "$TMP" 2>/dev/null || exit 0
mv "$TMP" "$FINAL" 2>/dev/null || rm -f "$TMP" 2>/dev/null

exit 0
