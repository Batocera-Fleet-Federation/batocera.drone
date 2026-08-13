#!/usr/bin/env bash
# Populate ports-client/vendor/{common,<arch>} for on-device use (the
# Batocera device has no pip) -- mirrors the main app's scripts/vendor_deps.sh
# common/<arch> split: PySDL2 + PyOpenGL are pure-Python/ctypes (py3-none-any
# wheels, arch-independent) so they go in vendor/common once; imgui_bundle and
# numpy are compiled per-arch+per-Python-version, so they go in vendor/<arch>,
# run once per target arch (see requirements-vendor.txt for why each is here
# -- notably numpy, despite being an "extra" in imgui_bundle's own metadata,
# is a verified hard runtime requirement for its texture-upload path).
#
# You MUST target the device's exact Python + arch + libc -- get this
# wrong and the compiled imgui_bundle extension's ABI-tagged filename
# won't match what the device's CPython import machinery looks for, which
# fails as ModuleNotFoundError, not a normal ImportError (confirmed live
# 2026-08-13: a 311 build was silently unloadable on a real Python-3.12.8
# device -- see the drone-live-debugging skill for how that was found, and
# ports-client/README.md's "Vendoring" section for the incident). Determine
# them on a Batocera box, don't assume:
#   python3 -c "import sys,platform; print(sys.version_info, platform.machine())"; ldd --version | head -1
#
# Usage:
#   scripts/vendor_deps.sh <py_tag> <arch> <platform_tag>
# Examples (run on a dev/CI machine, once per target arch -- 312 is what
# release.yml currently uses, verified against real hardware; re-verify
# before changing it):
#   scripts/vendor_deps.sh 312 x86_64  manylinux_2_28_x86_64
#   scripts/vendor_deps.sh 312 aarch64 manylinux_2_28_aarch64
set -euo pipefail

PY_TAG="${1:?python tag, e.g. 311}"
ARCH="${2:?cpu arch, e.g. x86_64 or aarch64}"
PLATFORM_TAG="${3:?pip platform tag, e.g. manylinux_2_28_x86_64}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENDOR="$ROOT/vendor"
COMMON="$VENDOR/common"
ARCH_DIR="$VENDOR/$ARCH"

rm -rf "$COMMON" "$ARCH_DIR"
mkdir -p "$COMMON" "$ARCH_DIR"

python3 -m pip install --no-compile --only-binary=:all: --target "$COMMON" \
  PySDL2 PyOpenGL

# --ignore-requires-python: without it, pip gates on *this* interpreter's own
# version even though --python-version/--abi/--platform already say who the
# real target is -- confirmed necessary against the real imgui_bundle wheel,
# which declares requires-python >=3.10 and would otherwise be rejected when
# vendoring from a dev machine running an older/newer Python than the device.
python3 -m pip install --no-compile --only-binary=:all: --ignore-requires-python --target "$ARCH_DIR" \
  --platform "$PLATFORM_TAG" --python-version "$PY_TAG" --implementation cp --abi "cp${PY_TAG}" \
  imgui_bundle numpy

echo "Vendored PySDL2 + PyOpenGL -> $COMMON"
echo "Vendored imgui_bundle + numpy ($ARCH/$PLATFORM_TAG/cp$PY_TAG) -> $ARCH_DIR"
echo "Confirm on-device: PYTHONPATH includes vendor/common + vendor/<arch>; python3 -c 'import sdl2, OpenGL.GL, imgui_bundle, numpy'"
