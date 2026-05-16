# Sector Bot — Installation Guide

Sector Bot is a Discord bot that monitors VATSIM controller activity, assigns an “Online ATC” role, sends alerts to Discord and Telegram, and provides slash commands for METAR, TAF, and status. This project has coded on Ubuntu 24.04 LTS Noble and not tested on another enviroment.

This guide is for **anyone cloning the repository from GitHub**. The repo contains source code and configuration templates only. You create your own Python virtual environment, secrets file, and logs on your machine—they are **not** part of the repository.

---

## What is in the repository

| File | Purpose |
|------|---------|
| `vattur.py` | Main bot |
| `requirements.txt` | Python dependencies |
| `callsigns.txt` | VATSIM callsigns to monitor (one per line)—edit for your division |
| `vattur.env.example` | Template for secrets—copy to `vattur.env` locally |
| `INSTALL.md` | This guide |

**Not in the repo (you create these locally):**

- `.venv/` — Python virtual environment (`bin`, `lib`, `include`, etc.)
- `vattur.env` — API tokens and IDs (never commit)
- `vattur.log` — runtime logs

---

## Requirements

- **OS:** Linux recommended for 24/7 hosting (Ubuntu/Debian examples below). macOS works for testing; Windows is untested.
- **Python:** 3.11 or newer
- **Network:** Outbound HTTPS to Discord, Telegram, VATSIM, CheckWX, and VATEUD
- **API keys and bots:** See [Configuration](#configuration)

### Discord (one-time)

1. [Discord Developer Portal](https://discord.com/developers/applications) → create an application → **Bot** → copy token.
2. Enable **Message Content Intent** and **Server Members Intent**.
3. Invite the bot to your server with **Manage Roles**, **Send Messages**, and **Use Application Commands** (or use Administrator).
4. Place the bot’s role **above** the role it should assign (see [Controller role](#controller-role-id) below).

---

## 1. Clone the repository

```bash
git clone https://github.com/mrkazaam/SectorBot.git
cd SectorBot
```

---

## 2. Install system dependencies

**Ubuntu / Debian:**

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv build-essential git
```

If `python3.11` is unavailable, install Python 3.11+ (e.g. [deadsnakes PPA](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa) on Ubuntu) or use `python3` if it is already 3.11+.

Verify:

```bash
python3.11 --version   # or: python3 --version
```

---

## 3. Create a virtual environment

From the project directory, create a local venv named `.venv` (this folder stays on your machine and is ignored by git):

```bash
cd "SectorBot"
python3.11 -m venv .venv
```

Activate it (optional for interactive use):

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

---

## 4. Configuration

The bot reads settings from **environment variables** only (`Config` class in `vattur.py`).

### Create your secrets file

```bash
cp vattur.env.example vattur.env
chmod 600 vattur.env
```

Edit `vattur.env` and fill in every value:

| Variable | Description |
|----------|-------------|
| `DISCORD_BOT_TOKEN` | Bot token from Discord Developer Portal |
| `DISCORD_GUILD_ID` | Your Discord server ID (Developer Mode → right‑click server → Copy ID) |
| `DISCORD_CHANNEL_ID` | Channel ID for VATSIM / rogue alerts |
| `DISCORD_OWNER_ID` | Your Discord user ID (for `/shutdown`) |
| `TELEGRAM_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHANNEL_ID` | Telegram channel/chat ID (often `-100…`) |
| `CHECKWX_API_KEY` | From [checkwx.com](https://www.checkwx.com/) |
| `VATEUD_API_KEY` | VATEUD API key (roster / rogue-controller checks) |

**Never commit `vattur.env`.** It is listed in `.gitignore`.

### Callsigns

Edit `callsigns.txt`: one VATSIM callsign per line (e.g. `LTAC_TWR`). Use the list appropriate for **your** airspace, not necessarily the sample in the repo.

### Controller role ID (for ex. "Online ATC")

In `vattur.py`, set `CONTROLLER_ROLE_ID` to your Discord **“Online ATC”** (or equivalent) role ID:

```python
self.CONTROLLER_ROLE_ID = YOUR_ROLE_ID_HERE
```

Enable Developer Mode → **Server Settings → Roles** → right‑click the role → Copy ID. The bot’s highest role must be **above** this role in the role list.

---

## 5. Run the bot (manual test)

Load secrets and start the bot:

```bash
cd "SectorBot"
set -a && source vattur.env && set +a
.venv/bin/python vattur.py
```

You should see log lines such as `Bot logged in as …`. Stop with `Ctrl+C`.

If you see `GUILD_ID environment variable is not set`, check `DISCORD_GUILD_ID` in `vattur.env`.

---

## 6. Run on boot with systemd (Linux server)

For production, run the bot as a dedicated user with a service.

### Create a system user (optional but recommended)

```bash
sudo useradd -r -m -d /opt/vattur -s /usr/sbin/nologin vattur
sudo mkdir -p /opt/vattur
sudo chown vattur:vattur /opt/vattur
```

### Deploy the code

As root or with `sudo`:

```bash
sudo -u vattur git clone https://github.com/mrkazaam/SectorBot.git /opt/vattur/app
cd /opt/vattur/app
sudo -u vattur python3.11 -m venv .venv
sudo -u vattur .venv/bin/pip install -r requirements.txt
sudo -u vattur cp vattur.env.example vattur.env
sudo -u vattur nano vattur.env   # fill in real values
sudo chmod 600 /opt/vattur/app/vattur.env
```

Edit `callsigns.txt` and `CONTROLLER_ROLE_ID` in `vattur.py` before starting the service.

### Service unit

Create `/etc/systemd/system/vattur.service`:

```ini
[Unit]
Description=SectorBot Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=vattur
Group=vattur
WorkingDirectory=/opt/vattur/app
EnvironmentFile=/opt/vattur/app/vattur.env
ExecStart=/opt/vattur/app/.venv/bin/python /opt/vattur/app/vattur.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Adjust `WorkingDirectory`, `User`, and paths if you installed elsewhere (e.g. `~/vattur`).

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable vattur
sudo systemctl start vattur
sudo systemctl status vattur
```

Logs:

```bash
journalctl -u vattur -f
tail -f /opt/vattur/app/vattur.log
```

---

## 7. Verify everything works

1. Bot shows **online** in Discord.
2. Slash commands `/metar`, `/taf`, `/status` appear in your server (may take up to a minute after first start).
3. When a listed callsign connects on VATSIM, messages appear in your Discord channel and Telegram.
4. Members with a CID in their nickname get the controller role while online on a tracked callsign (see `extract_cid` in `vattur.py` for nickname formats).

---

## 8. Updating from GitHub

```bash
sudo systemctl stop vattur
cd /opt/vattur/app
sudo -u vattur git pull
sudo -u vattur .venv/bin/pip install -r requirements.txt
# Re-apply any local edits (callsigns.txt, CONTROLLER_ROLE_ID) if you had conflicts
sudo systemctl start vattur
```

---

## Troubleshooting

| Symptom | What to check |
|---------|----------------|
| `GUILD_ID environment variable is not set` | `DISCORD_GUILD_ID` in `vattur.env` |
| `ModuleNotFoundError` | Virtualenv not used; run with `.venv/bin/python` or reinstall requirements |
| No slash commands | Wrong guild ID; invite URL must include `applications.commands` |
| Roles not added/removed | Bot role position; **Manage Roles** permission |
| No Telegram messages | Bot added to channel; correct `TELEGRAM_CHANNEL_ID` |
| VATEUD 403 / “Just a moment” | Cloudflare; upgrade `curl_cffi` or contact VATEUD about your server IP |
| METAR/TAF errors | Airport code or `CHECKWX_API_KEY` |

---

## Security

- Keep `vattur.env` out of git and restrict permissions (`chmod 600`).
- Rotate tokens if they were ever committed or shared.
- Do not paste tokens into issues or pull requests.

---

## Quick reference

```bash
# One-time setup after clone
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp vattur.env.example vattur.env   # then edit

# Run manually
set -a && source vattur.env && set +a && .venv/bin/python vattur.py
```
