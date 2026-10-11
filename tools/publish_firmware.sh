#!/usr/bin/env bash
# Publish the firmware you just exported from Arduino IDE (WSL / Git Bash).
#
#   1. Bump FW_BUILD in firmware/subway_display/subway_display.ino
#   2. Arduino IDE: Sketch > Export Compiled Binary
#   3. ./tools/publish_firmware.sh
#   4. git add/commit/push, then run deploy/update.sh on the Pi
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$REPO/firmware/subway_display/build"
DEST="$REPO/server/firmware/firmware.bin"

build_number() { grep -aoE '##MTAPANEL_BUILD=[0-9]+##' "$1" | head -1 | grep -oE '[0-9]+' || true; }

BIN="$(find "$BUILD_DIR" -name 'subway_display.ino.bin' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-)"
[ -n "$BIN" ] || { echo "No exported binary in $BUILD_DIR. In Arduino IDE use Sketch > Export Compiled Binary."; exit 1; }

NEW="$(build_number "$BIN")"
[ -n "$NEW" ] || { echo "Build tag not found in $BIN — is this the panel sketch?"; exit 1; }

if [ -f "$DEST" ]; then
  OLD="$(build_number "$DEST")"
  if [ -n "$OLD" ] && [ "$NEW" -le "$OLD" ]; then
    echo "Build $NEW is not newer than the published build $OLD. Bump FW_BUILD and export again."
    exit 1
  fi
fi

cp "$BIN" "$DEST"
echo "Published build $NEW ($(( $(stat -c %s "$DEST") / 1024 )) KB) -> server/firmware/firmware.bin"
echo
echo "Next:"
echo "  git add server/firmware/firmware.bin firmware/ && git commit -m \"Firmware build $NEW\" && git push"
echo "  ssh janniel-pi@raspberrypi.local '~/code/mta_led_pannel/deploy/update.sh'"
