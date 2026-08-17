#!/usr/bin/env bash
# Pull the latest code and restart. Run on the server:
#
#   sudo bash /opt/polymarket/deploy/update.sh
#
# Stops the services first rather than restarting into a half-updated tree,
# and runs the test suite before bringing them back - a server that restarts
# into broken code holds a key that can spend money.
set -euo pipefail

APP_USER=polymarket
APP_DIR=/opt/polymarket

log()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mXX\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo."

log "Stopping services"
systemctl stop polymarket-bot polymarket-monitor 2>/dev/null || true

log "Pulling"
sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only

log "Updating dependencies"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

log "Running tests"
if ! sudo -u "$APP_USER" "$APP_DIR/venv/bin/python" -m pytest -q "$APP_DIR/tests"; then
    die "Tests failed. Services are STOPPED. Fix before starting them again."
fi

log "Refreshing units"
cp "$APP_DIR/deploy/polymarket-bot.service" /etc/systemd/system/
cp "$APP_DIR/deploy/polymarket-monitor.service" /etc/systemd/system/
systemctl daemon-reload

log "Starting services"
systemctl start polymarket-bot polymarket-monitor
sleep 3
systemctl --no-pager --lines=5 status polymarket-bot polymarket-monitor || true

log "Done. Follow the logs with: journalctl -u polymarket-bot -f"
