#!/bin/bash
# Select the default native path or the preserved GUI-autoload workaround.
GEN_DIR="${RGS_GEN_DIR:-/userdata/system/rgs/generators/yuzu}"
CONFIG="$GEN_DIR/smash-native-launch.conf"

case "${1:-}" in
  native)
    printf '%s\n' 'MODE="native"' > "$CONFIG.new" && mv "$CONFIG.new" "$CONFIG"
    echo "mode: native CLI"
    ;;
  gui)
    printf '%s\n' 'MODE="gui"' > "$CONFIG.new" && mv "$CONFIG.new" "$CONFIG"
    echo "mode: preserved GUI-autoload workaround"
    ;;
  *)
    echo "usage: $0 native|gui" >&2
    exit 2
    ;;
esac
