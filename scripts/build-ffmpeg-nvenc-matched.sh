#!/usr/bin/env bash
# build-ffmpeg-nvenc-matched.sh — build an ffmpeg that matches your NVIDIA
# driver's NVENC API version, installed to ~/.local (shadows the distro
# ffmpeg via ~/.local/bin precedence in PATH).
#
# WHY THIS EXISTS
# ---------------
# ffmpeg builds are compiled against a specific NVENC API (nv-codec-headers
# "gen") and the installed NVIDIA driver must be at least that new. If your
# card is on a legacy driver branch (e.g. 580 is the LAST branch supporting
# Pascal cards like the GTX 10xx), you cannot upgrade the driver to what a
# bleeding-edge ffmpeg wants — e.g.:
#
#   [hevc_nvenc @ ...] Driver does not support the required nvenc API
#   version. Required: 13.1 Found: 13.0
#
# The fix is the inverse: build ffmpeg against the API your driver DOES
# provide (Found: X.Y → nv-codec-headers nX.Y.*). OpenTranscode's probe
# picks ~/.local/bin/ffmpeg up automatically on the next launch (PATH
# order) and the Auto engine will start using the GPU.
#
# USAGE
# -----
#   ./build-ffmpeg-nvenc-matched.sh            # auto-detect driver API
#   ./build-ffmpeg-nvenc-matched.sh 13.0       # explicit gen
#
# Requires: git, make, gcc, nasm, pkg-config, clang (for --enable-cuda-llvm;
# the script drops that flag automatically if clang is absent).
set -euo pipefail

FF_VER="${FF_VER:-n9.0.2}"
PREFIX="${PREFIX:-$HOME/.local}"
BUILD_DIR="${BUILD_DIR:-/tmp/ffmpeg-nvenc-build}"
BASE_CONF="${BASE_CONF:-}"   # optional: a configure line to copy (e.g. distro ffmpeg)

if [[ $# -ge 1 ]]; then
    GEN="$1"
else
    # First two components of the driver's NVENC API, from the error ffmpeg
    # prints (or run `nvidia-smi` — 580/550/etc. drivers ↔ gen 13.0/12.2).
    read -rp "NVENC API gen to build against (e.g. 13.0): " GEN
fi
HEADERS_TAG="n${GEN}.19.1"   # any n<GEN>.* tag provides that gen
mkdir -p "$BUILD_DIR"

echo "==> nv-codec-headers $HEADERS_TAG → $PREFIX"
if [[ ! -d "$BUILD_DIR/nv-codec-headers" ]]; then
    git clone --quiet https://github.com/FFmpeg/nv-codec-headers.git "$BUILD_DIR/nv-codec-headers"
fi
git -C "$BUILD_DIR/nv-codec-headers" fetch --quiet --depth 1 origin tag "$HEADERS_TAG" || true
git -C "$BUILD_DIR/nv-codec-headers" checkout --quiet "$HEADERS_TAG"
make -C "$BUILD_DIR/nv-codec-headers" install "PREFIX=$PREFIX" >/dev/null

echo "==> ffmpeg $FF_VER source → $BUILD_DIR/src"
if [[ ! -d "$BUILD_DIR/src" ]]; then
    git clone --quiet --depth 1 --branch "$FF_VER" \
        https://github.com/FFmpeg/ffmpeg.git "$BUILD_DIR/src"
fi

CONF_ARGS="${BASE_CONF:-}"
if [[ -z "$CONF_ARGS" && -x /usr/bin/ffmpeg ]]; then
    # Copy the distro build's feature set so the result is a drop-in.
    CONF_ARGS=$(/usr/bin/ffmpeg -version 2>/dev/null | sed -n 3p | sed 's/^configuration: //')
fi
if [[ -z "$CONF_ARGS" ]]; then
    CONF_ARGS="--enable-gpl --enable-libx264 --enable-libx265 --enable-libvpx --enable-libsvtav1 --enable-libopus --enable-libvorbis --enable-libdav1d"
fi
CONF_ARGS=${CONF_ARGS/--prefix=*\/usr /}          # strip distro prefix
CONF_ARGS=$(echo "$CONF_ARGS" | sed 's/--prefix=[^ ]*//')
# --enable-rpath is CRITICAL: without it the installed binary resolves its
# SONAME libs (libavcodec.so etc.) from /usr/lib — the DISTRO build — and
# silently keeps demanding the newer NVENC API. rpath pins it to
# $PREFIX/lib where our NVENC-13.0-matched libs live.
CONF_ARGS="--prefix=$PREFIX $CONF_ARGS --enable-nvenc --enable-nvdec --enable-rpath"
command -v clang >/dev/null || CONF_ARGS=$(echo "$CONF_ARGS" | sed 's/--enable-cuda-llvm //')

echo "==> configure"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
cd "$BUILD_DIR/src"
# shellcheck disable=SC2086
./configure $CONF_ARGS

echo "==> make ($(nproc) jobs) — this takes a few minutes"
make -j"$(nproc)"
make install

echo
echo "==> done. Verify (all three must succeed):"
echo "   ldd $PREFIX/bin/ffmpeg | grep avcodec   # MUST print $PREFIX/lib/..."
echo "   hash -r; which ffmpeg                   # should print $PREFIX/bin/ffmpeg"
echo "   $PREFIX/bin/ffmpeg -hide_banner -loglevel error -f lavfi -i 'color=c=black:s=256x256:d=0.2' -c:v hevc_nvenc -f null -"
