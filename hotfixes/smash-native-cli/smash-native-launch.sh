#!/bin/bash
# Start Smash through the native SDL/command-line frontend bundled in the Eden
# and Citron AppImages. This avoids the Qt game-list/content-provider race.
#
# usage: smash-native-launch.sh <eden|citron> <rom>

EMU="${1:-}"
ROM="${2:-}"
GEN_DIR="$(cd "$(dirname "$0")" && pwd)"
EMU_DIR="/userdata/system/rgs/emulators/switch"
QT_CONFIG="/userdata/system/configs/yuzu/qt-config.ini"
MODE="native"
MODE_CONFIG="$GEN_DIR/smash-native-launch.conf"

[ -f "$MODE_CONFIG" ] && . "$MODE_CONFIG"

case "$EMU" in
  eden|citron) ;;
  *) echo "smash-native-launch: unsupported emulator: $EMU" >&2; exit 2 ;;
esac
[ -f "$ROM" ] || { echo "smash-native-launch: ROM not found: $ROM" >&2; exit 2; }
[ -r "$QT_CONFIG" ] || { echo "smash-native-launch: config not readable: $QT_CONFIG" >&2; exit 2; }
[ -x "$EMU_DIR/$EMU.AppImage" ] || { echo "smash-native-launch: AppImage not executable: $EMU_DIR/$EMU.AppImage" >&2; exit 2; }

gui_fallback() {
  local gui="$GEN_DIR/gui-autoload.sh"
  [ -x "$gui" ] || { echo "smash-native-launch: GUI fallback is unavailable: $gui" >&2; exit 3; }
  echo "smash-native-launch: using preserved GUI-autoload workaround" >&2
  exec /bin/bash "$gui" "$EMU" "$ROM"
}

[ "$MODE" = "gui" ] && gui_fallback
[ "$MODE" = "native" ] || { echo "smash-native-launch: MODE must be native or gui" >&2; exit 2; }

RUNROOT="$(mktemp -d /tmp/smash-native-cli.XXXXXX)" || gui_fallback
CHILD=""

cleanup() {
  case "$RUNROOT" in
    /tmp/smash-native-cli.*) rm -rf -- "$RUNROOT" ;;
  esac
}

forward_signal() {
  # AppImage mount helpers and the emulator may be separate processes. They
  # share the session/process group created below, so terminate the group.
  [ -n "$CHILD" ] && kill -TERM -- "-$CHILD" 2>/dev/null
  [ -n "$CHILD" ] && wait "$CHILD" 2>/dev/null
  # Keep the disposable config alive while AppImage helpers finish shutting
  # down. Normally this is well under a second; cap it so ES cannot hang.
  if [ -n "$CHILD" ]; then
    for _ in $(seq 1 50); do
      kill -0 -- "-$CHILD" 2>/dev/null || break
      sleep 0.1
    done
  fi
  exit 0
}

trap cleanup EXIT
trap forward_signal TERM INT HUP

cd "$EMU_DIR" || exit 2

if [ "$EMU" = "citron" ]; then
  # citron-cmd honors -c, but writes the selected file on exit. Give it a
  # disposable copy so the RGS-owned Qt configuration remains untouched.
  cp "$QT_CONFIG" "$RUNROOT/qt-config.ini" || gui_fallback
  echo "smash-native-launch: starting Citron native frontend" >&2
  setsid "./citron.AppImage" citron-cmd -c "$RUNROOT/qt-config.ini" -f -g "$ROM" &
else
  # Eden v0.2.1 crashes when eden-cli receives -c. Its default SDL config path
  # works and accepts the RGS Qt settings, including multiplayer mappings.
  mkdir -p "$RUNROOT/eden" || gui_fallback
  cp "$QT_CONFIG" "$RUNROOT/eden/sdl2-config.ini" || gui_fallback
  echo "smash-native-launch: starting Eden native frontend" >&2
  setsid env XDG_CONFIG_HOME="$RUNROOT" "./eden.AppImage" eden-cli -f -g "$ROM" &
fi

CHILD=$!
wait "$CHILD"
STATUS=$?
CHILD=""
exit "$STATUS"
