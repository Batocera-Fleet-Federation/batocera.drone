#!/usr/bin/env bash
# Populate app vendor/ with the FastAPI stack for on-device use (the Batocera device has no pip).
#
# The pure-Python deps go in vendor/common; the compiled pydantic_core goes in vendor/<arch>,
# selected at runtime by platform.machine() (see app/api_bridge.py:_vendor_dirs).
#
# You MUST target the device's exact Python + arch + libc. Determine them on a Batocera box:
#   python3 -c "import sys,platform; print(sys.version_info, platform.machine())"; ldd --version | head -1
#
# Usage:
#   scripts/vendor_deps.sh <py_tag> <arch> <platform_tag>
# Examples (run on a dev/CI machine, once per target arch):
#   scripts/vendor_deps.sh 311 x86_64  manylinux2014_x86_64
#   scripts/vendor_deps.sh 311 aarch64 manylinux2014_aarch64
set -euo pipefail

PY_TAG="${1:?python tag, e.g. 311}"
ARCH="${2:?cpu arch, e.g. x86_64 or aarch64}"
PLATFORM_TAG="${3:?pip platform tag, e.g. manylinux2014_x86_64}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$ROOT/vendor"
COMMON="$VENDOR/common"
ARCH_DIR="$VENDOR/$ARCH"
PKGS=(fastapi uvicorn starlette pydantic pydantic-core anyio sniffio typing_extensions annotated-types idna h11 click)

rm -rf "$COMMON" "$ARCH_DIR"
mkdir -p "$COMMON" "$ARCH_DIR"

# Pure-Python deps: install into vendor/common (arch-independent).
python3 -m pip install --no-compile --target "$COMMON" \
  fastapi uvicorn starlette anyio sniffio typing_extensions annotated-types idna h11 click pydantic

# pydantic_core is a compiled wheel — fetch the wheel matching the *device* tag and unpack the
# arch-specific binary into vendor/<arch> (it shadows any copy under common).
TMP="$(mktemp -d)"
python3 -m pip download --only-binary=:all: \
  --platform "$PLATFORM_TAG" --python-version "$PY_TAG" --implementation cp --abi "cp${PY_TAG}" \
  -d "$TMP" pydantic-core
WHEEL="$(ls "$TMP"/pydantic_core-*.whl | head -1)"
python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$WHEEL" "$ARCH_DIR"
rm -rf "$TMP"

echo "Vendored ${PKGS[*]}"
echo "  pure-Python -> $COMMON"
echo "  pydantic_core ($ARCH/$PLATFORM_TAG/cp$PY_TAG) -> $ARCH_DIR"
echo "Confirm on-device: PYTHONPATH includes vendor/common + vendor/<arch>; python3 -c 'import fastapi, pydantic_core'"
