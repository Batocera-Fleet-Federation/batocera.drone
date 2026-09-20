#!/bin/bash
# Exit 0 only when the native bridge and preserved GUI fallback are installed.
HERE="$(cd "$(dirname "$0")" && pwd)"
GEN_DIR="${RGS_GEN_DIR:-/userdata/system/rgs/generators/yuzu}"
ok=0

grep -q "smash-native-launch.sh" "$GEN_DIR/yuzuMainlineGenerator.py" 2>/dev/null && gen=yes || { gen=NO; ok=1; }
cmp -s "$HERE/smash-native-launch.sh" "$GEN_DIR/smash-native-launch.sh" 2>/dev/null && native=yes || { native=NO; ok=1; }
cmp -s "$HERE/../smash-gui-autoload/gui-autoload.sh" "$GEN_DIR/gui-autoload.sh" 2>/dev/null && gui=yes || { gui=NO; ok=1; }
[ -x "$GEN_DIR/smash-native-launch.sh" ] && native_ex=yes || { native_ex=NO; ok=1; }
[ -x "$GEN_DIR/gui-autoload.sh" ] && gui_ex=yes || { gui_ex=NO; ok=1; }

echo "check: generator=$gen native=$native/$native_ex gui-fallback=$gui/$gui_ex -> $([ "$ok" -eq 0 ] && echo READY || echo MISSING)"
exit "$ok"
