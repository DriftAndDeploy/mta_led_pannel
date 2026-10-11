#!/usr/bin/env bash
# Pull the latest code from GitHub and restart the server.
#
#   ~/code/mta_led_pannel/deploy/update.sh
#   ssh janniel-pi@raspberrypi.local '~/code/mta_led_pannel/deploy/update.sh'
#
# If server/firmware/firmware.bin changed, panels start updating within ~15 s.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

before="$(git rev-parse HEAD)"
git pull --ff-only
after="$(git rev-parse HEAD)"

if [ "$before" = "$after" ]; then
  echo "Already up to date ($(git log -1 --format='%h %s'))"
else
  git --no-pager log --oneline "$before..$after"
fi

if ! git diff --quiet "$before" "$after" -- server/requirements.txt || [ ! -f server/venv/.installed ]; then
  echo "==> Installing requirements"
  [ -x server/venv/bin/python ] || python3 -m venv server/venv
  server/venv/bin/pip install -q -r server/requirements.txt
  touch server/venv/.installed
fi

sudo systemctl restart mta-proxy
for i in 1 2 3 4 5; do
  if out="$(curl -fsS http://localhost:5000/healthz 2>/dev/null)"; then
    echo "Server up: $out"
    exit 0
  fi
  sleep 2
done
echo "Server did not come back — check: sudo journalctl -u mta-proxy -n 50 --no-pager"
exit 1
