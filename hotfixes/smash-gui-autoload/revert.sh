#!/bin/bash
# Remove the hotfix: restores the generator's original launch line and (optionally) removes the wrapper.  ./revert.sh [--remove-wrapper]
GEN_DIR="${RGS_GEN_DIR:-/userdata/system/rgs/generators/yuzu}"; GEN="$GEN_DIR/yuzuMainlineGenerator.py"
python3 - "$GEN" <<'PY' || exit $?
import re, sys, py_compile, os, shutil, tempfile
gen = sys.argv[1]; s = open(gen, encoding="utf-8").read()
# new marked form (4 lines, exact mirror of apply.sh)
new, n1 = re.subn(r'^[ \t]*# >>> gui-autoload hotfix.*?^[ \t]*# <<< gui-autoload hotfix[ \t]*\n', '', s, flags=re.S | re.M)
# legacy form (hand-applied before the bundle existed): up to 4 comment lines + if + commandArray line + one blank line
new, n2 = re.subn(r'(?:^[ \t]*#[^\n]*\n){1,4}^[ \t]*if emulator (?:==|in) [^\n]*01006A800016E000[^\n]*:\n^[ \t]*commandArray = \[[^\n]*gui-autoload\.sh[^\n]*\]\n\n', '', new, flags=re.M)
if n1 + n2 == 0: print("revert: generator has no hotfix block (nothing to do)"); sys.exit(0)
d = os.path.dirname(gen); fd, tmp = tempfile.mkstemp(suffix=".py", dir=d); os.close(fd); open(tmp, "w", encoding="utf-8").write(new)
try: py_compile.compile(tmp, doraise=True)
except Exception as e: os.unlink(tmp); print("revert: result would not compile:", e, file=sys.stderr); sys.exit(4)
shutil.copymode(gen, tmp); os.replace(tmp, gen); print("revert: hotfix block removed from generator")
PY
[ "${1:-}" = "--remove-wrapper" ] && rm -f "$GEN_DIR/gui-autoload.sh" && echo "revert: wrapper removed"
python3 -m py_compile "$GEN" && echo "revert: generator compiles OK"
