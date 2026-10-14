#!/usr/bin/env bash
# Runs every 5 minutes via mta-autoupdate.timer (as root).
#
# If GitHub has new commits: pull them, reinstall requirements/units if they
# changed, restart the server and check it. If the new version fails its
# health check, roll back to the previous commit and skip that commit until
# a newer one is pushed.
#
# Logs:  journalctl -u mta-autoupdate -n 50 --no-pager
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OWNER="$(stat -c %U "$REPO")"
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
HEALTH_URL="${HEALTH_URL:-http://localhost:5000/healthz}"
BAD_REV_FILE="${BAD_REV_FILE:-/var/lib/mta-autoupdate/bad_rev}"

# git and pip run as the repo owner so files stay theirs and the GitHub
# deploy key in their ~/.ssh is used.
as_owner() {
  if [ "$(id -un)" = "$OWNER" ]; then "$@"
  else runuser -u "$OWNER" -- env HOME="$OWNER_HOME" "$@"; fi
}

healthy() {
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -fsS --max-time 3 "$HEALTH_URL" > /dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

cd "$REPO"
as_owner git config core.fileMode false   # local chmod +x must not block pulls
as_owner git fetch -q origin 2>/dev/null || { echo "fetch failed (offline?) — will retry"; exit 0; }

before="$(as_owner git rev-parse HEAD)"
target="$(as_owner git rev-parse '@{u}')"
[ "$before" = "$target" ] && exit 0

if [ -f "$BAD_REV_FILE" ] && [ "$(cat "$BAD_REV_FILE")" = "$target" ]; then
  exit 0   # this commit already failed once; wait for a newer push
fi

echo "Updating $(as_owner git log -1 --format='%h' "$before") -> $(as_owner git log -1 --format='%h %s' "$target")"
if ! as_owner git merge -q --ff-only "$target"; then
  echo "Cannot fast-forward (local changes on the Pi?) — skipping"
  exit 1
fi

if ! as_owner git diff --quiet "$before" HEAD -- server/requirements.txt; then
  echo "Installing requirements"
  as_owner server/venv/bin/pip install -q -r server/requirements.txt
fi
if ! as_owner git diff --quiet "$before" HEAD -- deploy/; then
  echo "Refreshing systemd units"
  bash deploy/install_units.sh
fi

"$SYSTEMCTL" restart mta-proxy
if healthy; then
  echo "Server healthy on $(as_owner git log -1 --format='%h')"
  exit 0
fi

echo "New version failed its health check — rolling back to $(as_owner git log -1 --format='%h' "$before")"
mkdir -p "$(dirname "$BAD_REV_FILE")"
echo "$target" > "$BAD_REV_FILE"
as_owner git reset -q --hard "$before"
"$SYSTEMCTL" restart mta-proxy
healthy && echo "Rolled back OK" || echo "Rollback also unhealthy — check: journalctl -u mta-proxy -n 50"
exit 1
