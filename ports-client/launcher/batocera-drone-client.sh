#!/bin/sh
# What EmulationStation actually execs from the Ports menu. Per Batocera's
# Ports convention (wiki.batocera.org/systems:ports) this script lives at
# /userdata/roms/ports/batocera-drone-client.sh; the app code + vendored
# imgui_bundle live alongside it in the hidden .data/ subfolder so ES's
# gamelist scan doesn't pick them up as separate "games".
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SCRIPT_DIR/.data/batocera-drone-client"
VENDOR_ARCH_DIR="$APP_DIR/vendor/$(uname -m)"
VENDOR_COMMON_DIR="$APP_DIR/vendor/common"

export PYTHONPATH="$VENDOR_ARCH_DIR:$VENDOR_COMMON_DIR:$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"

exec python3 "$APP_DIR/main.py"
