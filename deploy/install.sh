#!/usr/bin/env bash
# One-time setup on the Raspberry Pi. Safe to re-run.
#
#   cd ~/code/mta_led_pannel && bash deploy/install.sh
#
# - creates server/venv and installs requirements
# - copies panel configs from the old ~/code/mta_panel_server folder (if found)
# - installs/updates the mta-proxy service and the auto-update timer
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OLD_DIR="${OLD_DIR:-$HOME/code/mta_panel_server}"
cd "$REPO"
git config core.fileMode false   # chmod +x on the Pi must not block git pull

echo "==> Python environment"
[ -x server/venv/bin/python ] || python3 -m venv server/venv
server/venv/bin/pip install -q --upgrade pip
server/venv/bin/pip install -q -r server/requirements.txt

echo "==> Panel configs"
mkdir -p server/data
if compgen -G "$OLD_DIR/config_*.json" > /dev/null; then
  cp -n "$OLD_DIR"/config_*.json server/data/
  echo "    copied $(ls "$OLD_DIR"/config_*.json | wc -l) config(s) from $OLD_DIR (existing ones kept)"
else
  echo "    no old configs found in $OLD_DIR — panels start with defaults"
fi

echo "==> systemd services (server + 5-minute auto-update)"
bash deploy/install_units.sh
sudo systemctl restart mta-proxy
chmod +x deploy/*.sh

echo "==> Health check"
for i in 1 2 3 4 5; do
  if curl -fsS http://localhost:5000/healthz; then echo; echo "Done."; exit 0; fi
  sleep 2
done
echo "Server did not answer — check: sudo journalctl -u mta-proxy -n 50 --no-pager"
exit 1
