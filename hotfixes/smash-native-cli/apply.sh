#!/bin/bash
# Install the Smash native-CLI launch bridge into an RGS yuzu generator.
HERE="$(cd "$(dirname "$0")" && pwd)"
GEN_DIR="${RGS_GEN_DIR:-/userdata/system/rgs/generators/yuzu}"
GEN="$GEN_DIR/yuzuMainlineGenerator.py"
WRAP="$GEN_DIR/smash-native-launch.sh"
GUI="$GEN_DIR/gui-autoload.sh"
GUI_SOURCE="$HERE/../smash-gui-autoload/gui-autoload.sh"
BK="${HOTFIX_BACKUP_DIR:-/userdata/system/backups/hotfixes/smash-native-cli}"

[ -f "$GEN" ] || { echo "apply: generator not found: $GEN" >&2; exit 2; }
[ -f "$GUI_SOURCE" ] || { echo "apply: preserved GUI wrapper not found: $GUI_SOURCE" >&2; exit 2; }
command -v python3 >/dev/null || { echo "apply: python3 missing" >&2; exit 2; }
mkdir -p "$BK"

install_if_changed() {
  local source="$1" target="$2"
  if cmp -s "$source" "$target" 2>/dev/null && [ -x "$target" ]; then
    echo "apply: wrapper already current: $target"
  else
    cp "$source" "$target.new" && chmod 755 "$target.new" && mv "$target.new" "$target"
    echo "apply: wrapper installed/updated: $target"
  fi
}

install_if_changed "$HERE/smash-native-launch.sh" "$WRAP"
install_if_changed "$GUI_SOURCE" "$GUI"

[ -f "$BK/pre-native-$(sha256sum "$GEN" | cut -c1-12).py" ] || \
  cp -p "$GEN" "$BK/pre-native-$(sha256sum "$GEN" | cut -c1-12).py"

python3 - "$GEN" <<'PY' || { echo "apply: generator layout changed upstream - hotfix NOT applied" >&2; exit 3; }
import os
import py_compile
import re
import shutil
import sys
import tempfile

gen = sys.argv[1]
with open(gen, encoding="utf-8") as stream:
    source = stream.read()

# Idempotence, plus migration from the earlier GUI-autoload patch (both the
# bundled marked form and the original hand-applied form used on the test box).
source = re.sub(
    r'^[ \t]*# >>> smash-native-cli hotfix.*?^[ \t]*# <<< smash-native-cli hotfix[ \t]*\n',
    '', source, flags=re.S | re.M)
source = re.sub(
    r'^[ \t]*# >>> gui-autoload hotfix.*?^[ \t]*# <<< gui-autoload hotfix[ \t]*\n',
    '', source, flags=re.S | re.M)
source = re.sub(
    r'(?:^[ \t]*#[^\n]*\n){1,5}^[ \t]*if emulator (?:==|in) [^\n]*01006A800016E000[^\n]*:\n'
    r'^[ \t]*commandArray = \[[^\n]*gui-autoload\.sh[^\n]*\]\n\n?',
    '', source, flags=re.M)

pattern = re.compile(
    r'^(?P<i>[ \t]*)commandArray = \["\./"\+emulator\+"\.AppImage", "-f",\s+"-g", rom \][ \t]*\n',
    re.M)
matches = list(pattern.finditer(source))
if len(matches) != 1:
    sys.exit(3)

match = matches[0]
indent = match.group('i')
block = (
    indent + '# >>> smash-native-cli hotfix: avoid the Qt content-provider race for Smash\n'
    + indent + "if emulator in ('eden', 'citron') and '01006A800016E000' in str(rom).upper():\n"
    + indent + '    commandArray = ["/bin/bash", "/userdata/system/rgs/generators/yuzu/smash-native-launch.sh", emulator, str(rom)]\n'
    + indent + '# <<< smash-native-cli hotfix\n'
)
patched = source[:match.end()] + block + source[match.end():]

directory = os.path.dirname(gen)
fd, temporary = tempfile.mkstemp(suffix='.py', dir=directory)
os.close(fd)
with open(temporary, 'w', encoding='utf-8') as stream:
    stream.write(patched)
try:
    py_compile.compile(temporary, doraise=True)
except Exception:
    os.unlink(temporary)
    raise
shutil.copymode(gen, temporary)
os.replace(temporary, gen)
print('apply: generator patched for native CLI launch')
PY

python3 -m py_compile "$GEN" || exit 4
"$HERE/check.sh"
