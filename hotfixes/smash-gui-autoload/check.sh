#!/bin/bash
# Exit 0 if the hotfix is fully in place, 1 otherwise. Prints what is missing.
HERE="$(cd "$(dirname "$0")" && pwd)"
GEN_DIR="${RGS_GEN_DIR:-/userdata/system/rgs/generators/yuzu}"
ok=0
grep -q "gui-autoload.sh" "$GEN_DIR/yuzuMainlineGenerator.py" 2>/dev/null && gen=yes || { gen=NO; ok=1; }
cmp -s "$HERE/gui-autoload.sh" "$GEN_DIR/gui-autoload.sh" 2>/dev/null && wrap=yes || { wrap="NO (missing or different)"; ok=1; }
[ -x "$GEN_DIR/gui-autoload.sh" ] && ex=yes || { ex=NO; ok=1; }
echo "check: generator patched: $gen | wrapper current: $wrap | wrapper executable: $ex -> $([ $ok -eq 0 ] && echo 'HOTFIX IN PLACE' || echo 'HOTFIX MISSING - run apply.sh')"
exit $ok
