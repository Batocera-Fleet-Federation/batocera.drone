#!/bin/bash
# Remove the native bridge selection from the generator. Installed wrappers are
# retained so the diagnosis and GUI workaround remain available.
GEN_DIR="${RGS_GEN_DIR:-/userdata/system/rgs/generators/yuzu}"
GEN="$GEN_DIR/yuzuMainlineGenerator.py"

python3 - "$GEN" <<'PY' || exit $?
import os
import py_compile
import re
import shutil
import sys
import tempfile

gen = sys.argv[1]
with open(gen, encoding='utf-8') as stream:
    source = stream.read()
result, count = re.subn(
    r'^[ \t]*# >>> smash-native-cli hotfix.*?^[ \t]*# <<< smash-native-cli hotfix[ \t]*\n',
    '', source, flags=re.S | re.M)
if count == 0:
    print('revert: native hotfix block not present')
    sys.exit(0)
directory = os.path.dirname(gen)
fd, temporary = tempfile.mkstemp(suffix='.py', dir=directory)
os.close(fd)
with open(temporary, 'w', encoding='utf-8') as stream:
    stream.write(result)
try:
    py_compile.compile(temporary, doraise=True)
except Exception:
    os.unlink(temporary)
    raise
shutil.copymode(gen, temporary)
os.replace(temporary, gen)
print('revert: native hotfix block removed; standard -f -g restored')
PY

python3 -m py_compile "$GEN"
