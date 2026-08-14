#!/usr/bin/env bash
# Assembles the on-device directory layout the Ports launcher expects
# (see launcher/batocera-drone-client.sh) into a tarball for one arch:
#
#   batocera-drone-client.sh                 <- top-level launcher ES execs
#   images/batocera-drone_marquee.png        <- ES gamelist marquee
#   .data/batocera-drone-client/
#     main.py, client/, ui/                  <- app code
#     vendor/<arch>/                         <- imgui_bundle, from vendor_deps.sh
#
# Run scripts/vendor_deps.sh for this arch first. This only assembles a
# local tarball -- publishing it (e.g. attaching to a GitHub Release that
# batocera_install.sh's install_ports_client() downloads) is a follow-up
# once there's an actual release to point at; see README.md.
#
# Usage: scripts/build_release_bundle.sh <arch> [output_dir]
set -euo pipefail

ARCH="${1:?cpu arch, e.g. x86_64 or aarch64 (matching the vendor_deps.sh run)}"
OUTPUT_DIR="${2:-dist}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR_ARCH_DIR="$ROOT/vendor/$ARCH"
VENDOR_COMMON_DIR="$ROOT/vendor/common"

if [ ! -d "$VENDOR_ARCH_DIR" ] || [ ! -d "$VENDOR_COMMON_DIR" ]; then
  echo "No vendored deps at $VENDOR_ARCH_DIR / $VENDOR_COMMON_DIR -- run scripts/vendor_deps.sh <py_tag> $ARCH <platform_tag> first." >&2
  exit 1
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

APP_DIR="$STAGE/.data/batocera-drone-client"
mkdir -p "$APP_DIR/vendor"

cp "$ROOT/main.py" "$APP_DIR/"
cp -R "$ROOT/client" "$APP_DIR/client"
cp -R "$ROOT/ui" "$APP_DIR/ui"
cp -R "$VENDOR_ARCH_DIR" "$APP_DIR/vendor/$ARCH"
cp -R "$VENDOR_COMMON_DIR" "$APP_DIR/vendor/common"
find "$APP_DIR" -name "__pycache__" -type d -prune -exec rm -rf {} +
cp "$ROOT/launcher/batocera-drone-client.sh" "$STAGE/batocera-drone-client.sh"
chmod +x "$STAGE/batocera-drone-client.sh"
mkdir -p "$STAGE/images"
cp "$ROOT/launcher/images/batocera-drone_marquee.png" "$STAGE/images/"

# Fail the release build instead of publishing a code-only/incomplete Ports
# client. The API update worker treats this bundle as required on supported
# Batocera architectures, including the image assets used by the actual UI.
for required_file in \
  "$STAGE/batocera-drone-client.sh" \
  "$APP_DIR/main.py" \
  "$APP_DIR/client/endpoints.py" \
  "$APP_DIR/client/gamelist_integration.py" \
  "$APP_DIR/ui/app.py" \
  "$APP_DIR/ui/assets/logo.png" \
  "$STAGE/images/batocera-drone_marquee.png"; do
  if [ ! -s "$required_file" ]; then
    echo "Incomplete Ports client bundle: missing or empty $required_file" >&2
    exit 1
  fi
done

mkdir -p "$OUTPUT_DIR"
TARBALL="$OUTPUT_DIR/batocera-drone-client-$ARCH.tar.gz"
tar -C "$STAGE" -czf "$TARBALL" .

echo "Built $TARBALL"
