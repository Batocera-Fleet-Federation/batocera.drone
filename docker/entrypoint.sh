#!/usr/bin/env bash
set -euo pipefail

USERDATA_ROOT="${USERDATA_ROOT:-/userdata}"
ROMS_ROOT="${ROMS_ROOT:-${USERDATA_ROOT}/roms}"
BIOS_ROOT="${BIOS_ROOT:-${USERDATA_ROOT}/bios}"
ROM_SOURCE_ROOT="${ROM_SOURCE_ROOT:-/rom-source}"
DRONE_DEVICE_ID="${DRONE_DEVICE_ID:-${OVERMIND_DEVICE_ID:-drone-container}}"
DRONE_ROM_MIN="${DRONE_ROM_MIN:-6}"
DRONE_ROM_MAX="${DRONE_ROM_MAX:-18}"

mkdir -p \
  "$ROMS_ROOT" \
  "$BIOS_ROOT" \
  "$USERDATA_ROOT/system/configs/emulationstation" \
  "$USERDATA_ROOT/system/logs" \
  "$USERDATA_ROOT/system/drone-app/logs" \
  "$USERDATA_ROOT/themes/default"

cat > "$USERDATA_ROOT/system/batocera.conf" <<EOF
global.theme=default
system.hostname=${DRONE_DEVICE_ID}
EOF

cat > "$USERDATA_ROOT/system/configs/emulationstation/es_settings.cfg" <<EOF
<?xml version="1.0"?>
<config>
  <string name="ThemeSet" value="default" />
</config>
EOF

touch "$USERDATA_ROOT/system/logs/es_launch_stdout.log" "$USERDATA_ROOT/system/logs/es_launch_stderr.log"
echo "launch emulator container=${DRONE_DEVICE_ID}" >> "$USERDATA_ROOT/system/logs/es_launch_stdout.log"

if [[ ! -d "$ROM_SOURCE_ROOT" ]] || ! find "$ROM_SOURCE_ROOT" -mindepth 2 -type f | grep -q .; then
  cat >&2 <<EOF
ERROR: No ROM files found at ${ROM_SOURCE_ROOT}.
Mount the shared ROM source at ${ROM_SOURCE_ROOT}.
Expected layout: .github/data/roms/<system>/<files>
Run .github/scripts/import-roms-remotely.sh first if the folder is empty.
EOF
  exit 2
fi

python - <<'PY'
import hashlib
import os
import random
import shutil
from pathlib import Path

source = Path(os.environ.get("ROM_SOURCE_ROOT", "/rom-source"))
dest_root = Path(os.environ.get("ROMS_ROOT", "/userdata/roms"))
device_id = os.environ.get("DRONE_DEVICE_ID") or os.environ.get("OVERMIND_DEVICE_ID") or "drone"
minimum = max(1, int(os.environ.get("DRONE_ROM_MIN", "6")))
maximum = max(minimum, int(os.environ.get("DRONE_ROM_MAX", "18")))

files = [p for p in source.rglob("*") if p.is_file()]
if not files:
    raise SystemExit("No ROM files found in source pool")

seed = int(hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:12], 16)
rng = random.Random(seed)
count = min(len(files), rng.randint(minimum, maximum))
selected = rng.sample(files, count)

systems = set()
for src in selected:
    rel = src.relative_to(source)
    if len(rel.parts) < 2:
        continue
    system = rel.parts[0]
    target = dest_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    systems.add(system)

for system in sorted(systems):
    system_dir = dest_root / system
    games = []
    for rom in sorted(p for p in system_dir.rglob("*") if p.is_file() and p.name != "gamelist.xml"):
        rel = rom.relative_to(system_dir).as_posix()
        games.append(f"  <game><path>./{rel}</path><name>{rom.stem}</name></game>")
    if games:
        (system_dir / "gamelist.xml").write_text("<gameList>\n" + "\n".join(games) + "\n</gameList>\n", encoding="utf-8")

print(f"Seeded {len(selected)} ROM files for {device_id}: {', '.join(sorted(systems))}")
PY

export DRONE_APP_USERNAME="${DRONE_APP_USERNAME:-${ROM_API_USERNAME:-admin}}"
export DRONE_APP_PASSWORD="${DRONE_APP_PASSWORD:-${ROM_API_PASSWORD:-changeme}}"
export BATOCERA_CONF_FILE="${BATOCERA_CONF_FILE:-$USERDATA_ROOT/system/batocera.conf}"
export ES_SETTINGS_FILE="${ES_SETTINGS_FILE:-$USERDATA_ROOT/system/configs/emulationstation/es_settings.cfg}"
export OVERMIND_DEVICE_ID="${OVERMIND_DEVICE_ID:-$DRONE_DEVICE_ID}"
export RUNNING_IN_DOCKER=1

exec "$@"
