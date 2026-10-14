#!/usr/bin/env bash
# Installs / refreshes the systemd units:
#   mta-proxy.service        the server
#   mta-autoupdate.timer     pulls from GitHub every 5 minutes
# Called by install.sh, update.sh and auto_update.sh. Does not restart the server.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="$(stat -c %U "$REPO")"
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"

fill() { sed "s#__USER__#$OWNER#g; s#__REPO__#$REPO#g" "$1"; }

fill "$REPO/deploy/mta-proxy.service"       | $SUDO tee /etc/systemd/system/mta-proxy.service > /dev/null
fill "$REPO/deploy/mta-autoupdate.service"  | $SUDO tee /etc/systemd/system/mta-autoupdate.service > /dev/null
$SUDO cp "$REPO/deploy/mta-autoupdate.timer" /etc/systemd/system/mta-autoupdate.timer

$SUDO systemctl daemon-reload
$SUDO systemctl enable -q mta-proxy
$SUDO systemctl enable -q --now mta-autoupdate.timer
