#!/usr/bin/env bash
set -euo pipefail

USERDATA_ROOT="${USERDATA_ROOT:-/userdata}"
ROMS_ROOT="${ROMS_ROOT:-${USERDATA_ROOT}/roms}"
BIOS_ROOT="${BIOS_ROOT:-${USERDATA_ROOT}/bios}"
ROM_SOURCE_ROOT="${ROM_SOURCE_ROOT:-/rom-source}"
BATOCERA_TEST_DATA_ROOT="${BATOCERA_TEST_DATA_ROOT:-}"
DRONE_DEVICE_ID="${DRONE_DEVICE_ID:-${OVERMIND_DEVICE_ID:-drone-container}}"
DRONE_ROM_MIN="${DRONE_ROM_MIN:-6}"
DRONE_ROM_MAX="${DRONE_ROM_MAX:-18}"
DRONE_CERT_FILE="${DRONE_CERT_FILE:-$USERDATA_ROOT/system/drone-app/certs/drone.crt}"
DRONE_KEY_FILE="${DRONE_KEY_FILE:-$USERDATA_ROOT/system/drone-app/certs/drone.key}"

mkdir -p \
  "$ROMS_ROOT" \
  "$BIOS_ROOT" \
  "$USERDATA_ROOT/system/configs/emulationstation" \
  "$USERDATA_ROOT/system/logs" \
  "$USERDATA_ROOT/system/drone-app/logs" \
  "$USERDATA_ROOT/themes/default"

if [[ -n "$BATOCERA_TEST_DATA_ROOT" && -d "$BATOCERA_TEST_DATA_ROOT" ]]; then
  for name in bios themes; do
    if [[ -d "$BATOCERA_TEST_DATA_ROOT/$name" ]]; then
      cp -Rn "$BATOCERA_TEST_DATA_ROOT/$name/." "$USERDATA_ROOT/$name/" 2>/dev/null || true
    fi
  done
  if [[ -d "$BATOCERA_TEST_DATA_ROOT/system" ]]; then
    cp -Rn "$BATOCERA_TEST_DATA_ROOT/system/." "$USERDATA_ROOT/system/" 2>/dev/null || true
  fi
fi

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

if [[ ! -d "$ROM_SOURCE_ROOT" ]] || [[ -z "$(find "$ROM_SOURCE_ROOT" -mindepth 2 -type f -print -quit)" ]]; then
  cat >&2 <<EOF
ERROR: No ROM files found at ${ROM_SOURCE_ROOT}.
Mount the shared ROM source at ${ROM_SOURCE_ROOT}.
Expected layout: .github/data/roms/<system>/<files>
Run .github/scripts/import-batocera-test-data.sh --generate-only first if the folder is empty.
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

if [[ -n "${DRONE_MTLS_CA_FILE:-}" ]]; then
  export DRONE_MTLS_CA_KEY_FILE="${DRONE_MTLS_CA_KEY_FILE:-$(dirname "$DRONE_MTLS_CA_FILE")/ca.key}"
  export DRONE_CERT_FILE
  export DRONE_KEY_FILE
  python - <<'PY'
import ipaddress
import hashlib
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

ca_file = Path(os.environ["DRONE_MTLS_CA_FILE"])
ca_key_file = Path(os.environ["DRONE_MTLS_CA_KEY_FILE"])
cert_file = Path(os.environ["DRONE_CERT_FILE"])
key_file = Path(os.environ["DRONE_KEY_FILE"])
device_id = os.environ.get("DRONE_DEVICE_ID") or os.environ.get("OVERMIND_DEVICE_ID") or "drone-container"
days = max(1, int(os.environ.get("DRONE_CERT_DAYS", "825")))
identity = re.sub(r"[^A-Za-z0-9_.:-]+", "-", device_id).strip("-") or "drone"
common_name = f"batocera-drone-{identity}"

def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip("[]"))
        return True
    except ValueError:
        return False

def add_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)

def host_tokens(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;\\s]+", value or "") if item.strip()]

def local_ips() -> list[str]:
    values: list[str] = []
    try:
        output = subprocess.check_output(["hostname", "-I"], text=True, stderr=subprocess.DEVNULL)
        for item in output.split():
            if is_ip(item):
                add_unique(values, item)
    except Exception:
        pass
    try:
        for item in socket.gethostbyname_ex(socket.gethostname())[2]:
            if is_ip(item):
                add_unique(values, item)
    except Exception:
        pass
    return values

lock_dir = ca_file.parent / ".ca.lock"
ca_file.parent.mkdir(parents=True, exist_ok=True)
for _ in range(100):
    try:
        lock_dir.mkdir()
        break
    except FileExistsError:
        time.sleep(0.1)
else:
    raise SystemExit(f"Timed out waiting for CA lock: {lock_dir}")

try:
    if not ca_file.exists() or not ca_key_file.exists():
        subprocess.run(
            [
                "openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
                "-keyout", str(ca_key_file), "-out", str(ca_file), "-days", "3650",
                "-subj", "/CN=Batocera Fleet Federation Drone mTLS CA",
                "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
finally:
    try:
        lock_dir.rmdir()
    except OSError:
        pass

dns_names: list[str] = []
ip_names: list[str] = []
add_unique(dns_names, common_name)
add_unique(dns_names, "localhost")
add_unique(dns_names, device_id)
try:
    add_unique(dns_names, socket.gethostname())
except Exception:
    pass
for item in host_tokens(os.environ.get("HOSTNAME_OVERRIDE", "")):
    if is_ip(item):
        add_unique(ip_names, item.strip("[]"))
    else:
        add_unique(dns_names, item)
add_unique(ip_names, "127.0.0.1")
for item in local_ips():
    add_unique(ip_names, item.strip("[]"))

alt_lines = []
for idx, value in enumerate(dns_names, 1):
    alt_lines.append(f"DNS.{idx} = {value}")
for idx, value in enumerate(ip_names, 1):
    alt_lines.append(f"IP.{idx} = {value}")

cert_file.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as tmp:
    tmpdir = Path(tmp)
    config_file = tmpdir / "openssl.cnf"
    csr_file = tmpdir / "drone.csr"
    config_file.write_text(
        "\n".join(
            [
                "[req]",
                "distinguished_name = dn",
                "req_extensions = ext",
                "prompt = no",
                "[dn]",
                f"CN = {common_name}",
                "[ext]",
                "basicConstraints = CA:FALSE",
                "keyUsage = critical, digitalSignature, keyEncipherment",
                "extendedKeyUsage = serverAuth, clientAuth",
                "subjectAltName = @alt_names",
                "[alt_names]",
                *alt_lines,
                "",
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "openssl", "req", "-new", "-nodes", "-newkey", "rsa:2048",
            "-keyout", str(key_file), "-out", str(csr_file),
            "-subj", f"/CN={common_name}", "-config", str(config_file),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    command = [
        "openssl", "x509", "-req", "-in", str(csr_file),
        "-CA", str(ca_file), "-CAkey", str(ca_key_file),
        "-out", str(cert_file), "-days", str(days), "-sha256",
        "-extfile", str(config_file), "-extensions", "ext",
        "-set_serial", str(int(hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:30], 16)),
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.chown(key_file, user=0, group=0)
    key_file.chmod(0o600)

print(f"Using shared Drone mTLS CA: {ca_file}")
PY
fi

export DRONE_APP_USERNAME="${DRONE_APP_USERNAME:-${ROM_API_USERNAME:-admin}}"
export DRONE_APP_PASSWORD="${DRONE_APP_PASSWORD:-${ROM_API_PASSWORD:-changeme}}"
export BATOCERA_CONF_FILE="${BATOCERA_CONF_FILE:-$USERDATA_ROOT/system/batocera.conf}"
export ES_SETTINGS_FILE="${ES_SETTINGS_FILE:-$USERDATA_ROOT/system/configs/emulationstation/es_settings.cfg}"
export OVERMIND_DEVICE_ID="${OVERMIND_DEVICE_ID:-$DRONE_DEVICE_ID}"
export DRONE_CERT_FILE
export DRONE_KEY_FILE
export TLS_CERT_FILE="${TLS_CERT_FILE:-$DRONE_CERT_FILE}"
export TLS_KEY_FILE="${TLS_KEY_FILE:-$DRONE_KEY_FILE}"
export RUNNING_IN_DOCKER=1

exec "$@"
