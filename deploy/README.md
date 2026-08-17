# Running the bot on a server

Once this is done the bot runs whether or not your computer is on — which is
the point, because **stop-losses only exist while the monitor is running**.
Polymarket has no native stop orders; a stop-loss lives inside that process and
nowhere else. A laptop that went to sleep is a position with no protection.

Target here is **Oracle Cloud Always Free** (genuinely free, no time limit) on
**Ubuntu 24.04**. Any Ubuntu box works the same way.

---

## Before you start: what goes on that machine

`POLYMARKET_PRIVATE_KEY` has to live on the server, because signing orders is
the whole job. Anyone who reads that file can move the funds in that wallet.

You chose to keep using the current wallet (~$21). That is a reasonable
trade for a small, dedicated trading balance. It stops being reasonable the
moment the balance becomes an amount you would mind losing — at that point,
create a separate wallet for the bot and fund only what you are trading.

Two properties of this setup that limit the damage:

* **No inbound ports.** The bot long-polls Telegram over outbound HTTPS. The
  server never accepts a connection except your SSH, so there is no service to
  attack from the internet.
* **The bot runs as a shell-less system user** with a hardened systemd unit —
  read-only filesystem except its own `data/`, no new privileges, no device
  access.

---

## 1. Create the server

1. Sign up at <https://cloud.oracle.com> → *Always Free* eligible.
2. **Compute → Instances → Create Instance.**
3. Image: **Ubuntu 24.04**.
4. Shape: **Ampere A1 (ARM)**, 1 OCPU / 6 GB is plenty.
   *If you get "out of capacity", try a different Availability Domain, or pick
   `VM.Standard.E2.1.Micro` (AMD) instead — also Always Free, and enough for
   this.*
5. **Add SSH keys** → *Generate a key pair for me* → download the private key.
6. Create. Note the **public IP**.

> Oracle's free tier reclaims instances that stay idle for long periods. This
> bot polls constantly, so it will not look idle.

## 2. Connect

```bash
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@YOUR_SERVER_IP
```

On Windows, use the same command in PowerShell or Git Bash.

## 3. Install

```bash
sudo apt update && sudo apt install -y git
git clone -b claude/new-session-er72vo https://github.com/labilab1/claude-ai-poly.git /tmp/pm
sudo bash /tmp/pm/deploy/setup.sh
```

The script installs Python 3.11+, creates the `polymarket` user, clones into
`/opt/polymarket`, builds a virtualenv, and installs the systemd units. It is
safe to re-run.

## 4. Put your credentials on the server

```bash
sudo -u polymarket nano /opt/polymarket/.env
```

Fill in:

| Variable | Where it comes from |
|---|---|
| `POLYMARKET_PRIVATE_KEY` | Your signing wallet's private key |
| `POLYMARKET_WALLET` | Your Polymarket **deposit/proxy** address — *not* your MetaMask address |
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `TELEGRAM_CHAT_ID` | Leave blank; the bot tells you yours on first message |

Save with `Ctrl+O`, `Enter`, `Ctrl+X`.

**Type them here rather than copying the file up.** A secret that never leaves
your hands cannot be left behind in a shell history or a synced folder.

The file is already `chmod 600` and owned by `polymarket`.

## 5. Start it

```bash
sudo systemctl enable --now polymarket-bot
journalctl -u polymarket-bot -f
```

You should see `Telegram bot online as @yourbot`. Message it `/start` — if
`TELEGRAM_CHAT_ID` is blank it replies with your chat id. Put that in `.env`,
then:

```bash
sudo systemctl restart polymarket-bot
```

## 6. Start the monitor — the part that protects positions

```bash
sudo systemctl enable --now polymarket-monitor
journalctl -u polymarket-monitor -f
```

**It starts in DRY RUN.** It watches every rule and reports what it *would*
have done, and sells nothing. Leave it there for a few days and read the logs.

When you want it to actually exit positions:

```bash
sudo systemctl edit --full polymarket-monitor
```

Add `--execute --yes` to the `ExecStart` line, then:

```bash
sudo systemctl daemon-reload && sudo systemctl restart polymarket-monitor
```

`--yes` is needed because there is no terminal to type a confirmation into.
What bounds it after that are the caps in `.env`: per-order, per-market, and
the rolling 24-hour loss limit.

---

## Running it

```bash
# state
sudo systemctl status polymarket-bot polymarket-monitor

# logs, live
journalctl -u polymarket-bot -f
journalctl -u polymarket-monitor -f

# logs, since yesterday
journalctl -u polymarket-monitor --since yesterday

# restart / stop
sudo systemctl restart polymarket-bot
sudo systemctl stop polymarket-monitor
```

Both restart automatically on crash and start on boot.

## Updating

```bash
sudo bash /opt/polymarket/deploy/update.sh
```

Stops the services, pulls, reinstalls dependencies, **runs the test suite**,
and only then starts them again. If the tests fail it leaves everything
stopped and says so — restarting into broken code is worse than being down,
because this process holds a key.

## Backup

The only irreplaceable things are `.env` and `data/` (your exit rules,
watchlist, and the rolling loss ledger):

```bash
sudo tar czf ~/pm-backup-$(date +%F).tar.gz \
  -C /opt/polymarket .env data
```

Copy it somewhere private. It contains your private key.

---

## Troubleshooting

**`Another Polymarket Telegram bot is already running`**
Exactly what it says — one poller per token, or Telegram hands your taps to
whichever asked first. Check for a stray process:

```bash
sudo systemctl stop polymarket-bot
pgrep -af telegram_bot           # should print nothing
sudo systemctl start polymarket-bot
```

If a crash left the lock behind and nothing is running:
`sudo rm /opt/polymarket/data/telegram_bot.lock`

**Bot starts then dies**
`journalctl -u polymarket-bot -n 50` — almost always a missing or wrong value
in `.env`.

**Reports $0 with no error**
`POLYMARKET_WALLET` is the MetaMask address instead of the Polymarket
deposit/proxy address. Run:
`sudo -u polymarket /opt/polymarket/venv/bin/python -m polymarket_bot.scripts.wallet_status`
It prints the signer and the configured wallet side by side.

**Rules never fire**
The monitor is in dry run (the default). See step 6.
