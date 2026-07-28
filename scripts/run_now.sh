#!/usr/bin/env bash
set -euo pipefail

# Standalone, signed Drone bootstrap. This file may be downloaded by an
# installer, but it never trusts the application archive until a manifest
# signature made by the pinned offline release key has been verified.

WORK_DIR="${DRONE_APP_WORK_DIR:-/userdata/system/drone-app}"
STAGE_ONLY="${DRONE_APP_STAGE_ONLY:-0}"
MANIFEST_URL="${DRONE_UPDATE_MANIFEST_URL:-https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/release-manifest.json}"
SIGNATURE_URL="${DRONE_UPDATE_MANIFEST_SIGNATURE_URL:-https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/release-manifest.sig}"
RELEASE_ROOT="${DRONE_RELEASE_DOWNLOAD_ROOT:-https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/download}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required to verify Drone releases" >&2
  exit 1
fi

DOWNLOAD_TOOL=""
if command -v curl >/dev/null 2>&1; then
  DOWNLOAD_TOOL="curl"
elif command -v wget >/dev/null 2>&1; then
  DOWNLOAD_TOOL="wget"
else
  echo "curl or wget is required" >&2
  exit 1
fi

download_file() {
  local source_url="$1"
  local destination="$2"
  if [[ "$DOWNLOAD_TOOL" == "curl" ]]; then
    curl -fsSL --connect-timeout 10 --max-time 120 "$source_url" -o "$destination"
  else
    wget -T 120 -qO "$destination" "$source_url"
  fi
}

mkdir -p "$WORK_DIR"
TEMP_DIR="$(mktemp -d "$WORK_DIR/.verified-bootstrap.XXXXXX")"
cleanup() {
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

PUBLIC_KEY="$TEMP_DIR/update-signing-public.pem"
cat > "$PUBLIC_KEY" <<'PUBLICKEY'
-----BEGIN PUBLIC KEY-----
MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEA6v5FRoVdVNL8KBxoGliK
bqWgUgkUD04qpYwjss1QWCJlSE/XSySYFfPRmc1CLzQZDbjQO/Wv3xF2HGMxXj1t
u5Iq0af7Ab8FuarWG5u1fUeoyq4+3muvk1HZlG6EGEYt5pkBZqpLb5pJtd5UkVdx
IugagWdrGCbxE5InSV8+Ni8E1S64z/oKTibNJD/7rBB2AJyw28x2PvcSRGMlXLxK
/g+g5dIHu7AmjT2gweNdtZy7LApsFsR/Y2xZbsaS208ITV/UbuhBv0nqpEAAdbSW
s2VFr41LyiyE1AMBQPoDqxor/AP7YKyWcMIt35VPlLfVI48/iQQHFJbNNibzHC+8
5GRjjbeOaTFN9Oz15G0D2Pwby9f2PMAvR1SBRyibe//XHgvBI0L8GKuAbDtrmAE7
P24hCiKk0huhBUpWwjbTLe8lG6wSc4smGwX8aqqmjU09o9whkC4vFfQhfHHF2b2i
rOWdZE5kb/1z4siM82ilZ8SQwYnm8bhtg4r1dAr6YOjlAgMBAAE=
-----END PUBLIC KEY-----
PUBLICKEY

MANIFEST="$TEMP_DIR/release-manifest.json"
SIGNATURE="$TEMP_DIR/release-manifest.sig"
ARCHIVE="$TEMP_DIR/drone-app.tar.gz"
download_file "$MANIFEST_URL" "$MANIFEST"
download_file "$SIGNATURE_URL" "$SIGNATURE"
if ! openssl dgst -sha256 -verify "$PUBLIC_KEY" -signature "$SIGNATURE" "$MANIFEST" >/dev/null; then
  echo "Drone release manifest signature verification failed" >&2
  exit 1
fi

RELEASE_VERSION="$(python3 - "$MANIFEST" <<'PY'
import json
import re
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
version = str(payload.get("version") or "")
asset = (payload.get("assets") or {}).get("drone-app.tar.gz")
if payload.get("schema") != 1 or not re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", version):
    raise SystemExit("invalid signed release manifest")
if not isinstance(asset, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(asset.get("sha256") or "")):
    raise SystemExit("signed manifest does not describe drone-app.tar.gz")
if not isinstance(asset.get("size"), int) or asset["size"] <= 0:
    raise SystemExit("signed archive size is invalid")
print(version)
PY
)"

ARCHIVE_URL="${DRONE_APP_ARCHIVE_URL:-$RELEASE_ROOT/$RELEASE_VERSION/drone-app.tar.gz}"
download_file "$ARCHIVE_URL" "$ARCHIVE"

python3 - "$MANIFEST" "$ARCHIVE" "$WORK_DIR" <<'PY'
import hashlib
import json
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path

manifest_path, archive_path, work_dir = map(Path, sys.argv[1:])
work_dir = work_dir.resolve()
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
version = manifest["version"]
metadata = manifest["assets"]["drone-app.tar.gz"]
if archive_path.stat().st_size != metadata["size"]:
    raise SystemExit("Drone archive size does not match its signed manifest")
digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
if digest != metadata["sha256"]:
    raise SystemExit("Drone archive digest does not match its signed manifest")

stage = work_dir / f".release-stage-{os.getpid()}"
stage.mkdir(parents=True, exist_ok=False)
wanted = {"app", "content"}
seen = set()
try:
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            relative = member.name.lstrip("/")
            parts = relative.split("/", 1)
            if parts[0] not in wanted and len(parts) == 2:
                relative = parts[1]
                parts = relative.split("/", 1)
            if not parts or parts[0] not in wanted:
                continue
            relative_path = Path(relative)
            if "__pycache__" in relative_path.parts or member.name.endswith(".pyc"):
                continue
            if member.issym() or member.islnk() or member.isdev():
                raise RuntimeError(f"archive contains special entry: {member.name}")
            target = (stage / relative_path).resolve()
            if stage not in target.parents and target != stage:
                raise RuntimeError(f"archive member escapes staging: {member.name}")
            seen.add(parts[0])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"cannot read archive member: {member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    if seen != wanted:
        raise RuntimeError("archive is missing app/ or content/")
    for required in ("main.py", "drone_api.py", "service_bootstrap.sh", "VERSION"):
        if not (stage / "app" / required).is_file():
            raise RuntimeError(f"archive is missing app/{required}")
    archive_version = (stage / "app" / "VERSION").read_text(encoding="utf-8").splitlines()[0].strip()
    if archive_version != version:
        raise RuntimeError("archive version does not match signed manifest")

    releases = work_dir / ".releases"
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / f"{version.lstrip('v')}-{metadata['sha256'][:12]}"
    if release.exists():
        shutil.rmtree(stage)
    else:
        stage.replace(release)

    current = work_dir / "current"
    next_current = work_dir / ".current.new"
    next_current.unlink(missing_ok=True)
    next_current.symlink_to(Path(".releases") / release.name, target_is_directory=True)
    app = work_dir / "app"
    content = work_dir / "content"
    if app.is_symlink() and content.is_symlink():
        os.replace(next_current, current)
    else:
        legacy = releases / f"pre-signed-update-{int(time.time())}"
        legacy.mkdir()
        next_app = work_dir / ".app.new"
        next_content = work_dir / ".content.new"
        next_app.unlink(missing_ok=True)
        next_content.unlink(missing_ok=True)
        next_app.symlink_to(Path("current") / "app", target_is_directory=True)
        next_content.symlink_to(Path("current") / "content", target_is_directory=True)
        for name, path in (("app", app), ("content", content)):
            if path.exists() or path.is_symlink():
                path.replace(legacy / name)
        os.replace(next_current, current)
        os.replace(next_app, app)
        os.replace(next_content, content)
finally:
    if stage.exists():
        shutil.rmtree(stage)
PY

echo "Verified and activated Drone $RELEASE_VERSION in $WORK_DIR"
case "$STAGE_ONLY" in
  1|true|TRUE|yes|YES|on|ON) exit 0 ;;
esac

HTTPS_PORT="${HTTPS_PORT:-443}"
if command -v lsof >/dev/null 2>&1 && lsof -i :"$HTTPS_PORT" >/dev/null 2>&1; then
  echo "Port ${HTTPS_PORT} is already in use; Drone may already be running."
  exit 0
fi

exec env \
  PYTHONPATH="$WORK_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  HTTPS_PORT="$HTTPS_PORT" \
  DRONE_COMPAT_HTTPS_PORTS="${DRONE_COMPAT_HTTPS_PORTS:-8443}" \
  ROMS_ROOT="${ROMS_ROOT:-/userdata/roms}" \
  BIOS_ROOT="${BIOS_ROOT:-/userdata/bios}" \
  TLS_SELF_SIGNED_DIR="${TLS_SELF_SIGNED_DIR:-/userdata/system/certs}" \
  LOG_DIR="${LOG_DIR:-/userdata/system/logs/drone-app}" \
  python3 -m app.main
