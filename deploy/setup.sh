#!/usr/bin/env bash
# One-shot server setup for the Polymarket bot on Ubuntu 22.04/24.04.
#
#   curl -fsSL <raw-url>/deploy/setup.sh | sudo bash
# or, from a clone:
#   sudo bash deploy/setup.sh
#
# Safe to re-run: every step checks before it acts, so this doubles as the
# repair path if something got half-installed.
#
# What it does NOT do is write your .env. Secrets are typed on the server by
# you, once, and never travel through a script, a shell history, or a repo.
set -euo pipefail

APP_USER=polymarket
APP_DIR=/opt/polymarket
REPO=${REPO:-https://github.com/labilab1/claude-ai-poly.git}
BRANCH=${BRANCH:-claude/new-session-er72vo}

log()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mXX\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo."

# --- python ---------------------------------------------------------------
# polymarket-client needs 3.11+. Ubuntu 24.04 ships 3.12, 22.04 ships 3.10 and
# needs the deadsnakes PPA. Checked rather than assumed, because installing
# against 3.10 fails later with a confusing resolver error.
log "Checking Python"
PY=""
for candidate in python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1; then PY=$candidate; break; fi
done
if [[ -z "$PY" ]]; then
    warn "No Python 3.11+ found. Installing 3.12."
    apt-get update -qq
    apt-get install -y software-properties-common >/dev/null
    add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1 || true
    apt-get update -qq
    apt-get install -y python3.12 python3.12-venv >/dev/null
    PY=python3.12
fi
log "Using $PY ($($PY -V))"

log "Installing packages"
apt-get update -qq
apt-get install -y git >/dev/null
# The venv module ships separately on Debian/Ubuntu. Missing it fails later
# with "ensurepip is not available", which reads like a Python bug and is not.
if ! "$PY" -c "import venv, ensurepip" >/dev/null 2>&1; then
    log "Installing ${PY}-venv"
    apt-get install -y "${PY}-venv" >/dev/null \
        || die "Could not install ${PY}-venv. Install it manually and re-run."
fi

# --- user -----------------------------------------------------------------
# A dedicated, non-login account. If the bot is ever compromised, it gets a
# shell-less user that owns nothing but its own directory.
if ! id "$APP_USER" >/dev/null 2>&1; then
    log "Creating user $APP_USER"
    useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
else
    log "User $APP_USER exists"
fi

# --- code -----------------------------------------------------------------
# `git clone` refuses a non-empty directory, and useradd --create-home has
# already put .bashrc and friends in here - so the repo is fetched into the
# existing directory rather than cloned over it.
#
# Nothing in this block deletes anything. An earlier version wiped the
# directory before cloning, which would have taken .env (your private key) and
# data/ (your exit rules) with it on any re-run.
if [[ -d "$APP_DIR/.git" ]]; then
    log "Updating existing checkout"
else
    log "Initialising $APP_DIR from $REPO"
    sudo -u "$APP_USER" git -C "$APP_DIR" init --quiet
    sudo -u "$APP_USER" git -C "$APP_DIR" remote add origin "$REPO" 2>/dev/null \
        || sudo -u "$APP_USER" git -C "$APP_DIR" remote set-url origin "$REPO"
fi
sudo -u "$APP_USER" git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
sudo -u "$APP_USER" git -C "$APP_DIR" checkout --quiet -B "$BRANCH" "origin/$BRANCH"

# --- venv -----------------------------------------------------------------
log "Building virtualenv"
sudo -u "$APP_USER" "$PY" -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# --- runtime state --------------------------------------------------------
install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$APP_DIR/data"

# --- env ------------------------------------------------------------------
# Created empty and locked down. You fill it in; the script never sees a secret.
if [[ ! -f "$APP_DIR/.env" ]]; then
    log "Creating .env from the example"
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
fi
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

# --- services -------------------------------------------------------------
log "Installing systemd units"
cp "$APP_DIR/deploy/polymarket-bot.service" /etc/systemd/system/
cp "$APP_DIR/deploy/polymarket-monitor.service" /etc/systemd/system/
systemctl daemon-reload

# --- verify ---------------------------------------------------------------
log "Verifying the install"
if sudo -u "$APP_USER" "$APP_DIR/venv/bin/python" -c "import polymarket, requests, eth_account" 2>/dev/null; then
    echo "  dependencies import cleanly"
else
    die "Dependencies did not import - check the pip output above."
fi

cat <<EOF

$(log "Installed. Two things left, both yours:")

  1. Put your credentials in the env file:

       sudo -u $APP_USER nano $APP_DIR/.env

     You need POLYMARKET_PRIVATE_KEY, POLYMARKET_WALLET and
     TELEGRAM_BOT_TOKEN. TELEGRAM_CHAT_ID can stay blank for now - the bot
     will tell you yours the first time you message it.

  2. Start them:

       sudo systemctl enable --now polymarket-bot
       sudo systemctl enable --now polymarket-monitor

Then watch it come up:

       journalctl -u polymarket-bot -f

The monitor starts in DRY RUN and will not sell anything. See the note at the
top of /etc/systemd/system/polymarket-monitor.service before changing that.

EOF
