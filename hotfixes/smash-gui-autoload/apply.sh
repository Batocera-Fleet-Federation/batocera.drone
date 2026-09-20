#!/bin/bash
# Idempotent: install/repair the Smash "GUI autoload" hotfix in the RGS yuzu generator. Safe to run any number of times.
#   ./apply.sh            apply (or repair) the hotfix
#   RGS_GEN_DIR=/some/dir ./apply.sh   operate on a copy (testing)
# Never leaves the generator half-patched: the patched file is compiled first and only then moved into place.
HERE="$(cd "$(dirname "$0")" && pwd)"
GEN_DIR="${RGS_GEN_DIR:-/userdata/system/rgs/generators/yuzu}"
GEN="$GEN_DIR/yuzuMainlineGenerator.py"
WRAP="$GEN_DIR/gui-autoload.sh"
BK="${HOTFIX_BACKUP_DIR:-/userdata/system/backups/hotfixes/smash-gui-autoload}"
[ -f "$GEN" ] || { echo "apply: generator not found: $GEN" >&2; exit 2; }
command -v python3 >/dev/null || { echo "apply: python3 missing" >&2; exit 2; }
mkdir -p "$BK"

# 1) the wrapper script
if ! cmp -s "$HERE/gui-autoload.sh" "$WRAP" 2>/dev/null; then
  cp "$HERE/gui-autoload.sh" "$WRAP.new" && chmod 755 "$WRAP.new" && mv "$WRAP.new" "$WRAP" && echo "apply: wrapper installed/updated -> $WRAP"
else echo "apply: wrapper already current"; fi

# 2) the generator patch
if grep -q "gui-autoload.sh" "$GEN"; then
  echo "apply: generator already patched (nothing to do)"
else
  [ -f "$BK/pristine-$(sha256sum "$GEN" | cut -c1-12).py" ] || cp -p "$GEN" "$BK/pristine-$(sha256sum "$GEN" | cut -c1-12).py"
  python3 - "$GEN" <<'PY' || { echo "apply: generator layout changed upstream - hotfix NOT applied (generator untouched)" >&2; exit 3; }
import re, sys, py_compile, os, shutil, tempfile
gen = sys.argv[1]
s = open(gen, encoding="utf-8").read()
pat = re.compile(r'^(?P<i>[ \t]*)commandArray = \["\./"\+emulator\+"\.AppImage", "-f",\s+"-g", rom \][ \t]*\n', re.M)
if len(pat.findall(s)) != 1:
    sys.exit(3)
m = pat.search(s); i = m.group("i")
block = (i + "# >>> gui-autoload hotfix (smash): autoboot (-g) starts before update/DLC layers are indexed; load via the emulator GUI instead\n"
         + i + "if emulator in ('eden', 'citron') and '01006A800016E000' in str(rom).upper():\n"
         + i + "    commandArray = [\"/bin/bash\", \"/userdata/system/rgs/generators/yuzu/gui-autoload.sh\", emulator, str(rom)]\n"
         + i + "# <<< gui-autoload hotfix\n")
new = s[:m.end()] + block + s[m.end():]
d = os.path.dirname(gen); fd, tmp = tempfile.mkstemp(suffix=".py", dir=d); os.close(fd)
open(tmp, "w", encoding="utf-8").write(new)
try: py_compile.compile(tmp, doraise=True)
except Exception as e:
    os.unlink(tmp); print("patched file failed to compile:", e, file=sys.stderr); sys.exit(4)
shutil.copymode(gen, tmp); os.replace(tmp, gen)
print("apply: generator patched")
PY
  rc=$?; [ $rc -eq 0 ] || exit $rc
fi
python3 -m py_compile "$GEN" && echo "apply: generator compiles OK"
"$HERE/check.sh"
