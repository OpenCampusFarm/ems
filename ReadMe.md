# EMS

Energy Management System consisting of two subprojects: `core` and `fan`.

## Project Structure

```
ems/
├── core/           # Core subproject
├── fan/            # Fan subproject
├── supervisord.conf
├── update.sh
└── ems.service
```

## Prerequisites

- [uv](https://github.com/astral-sh/uv) — Python package manager
- [supervisord](https://github.com/ochinchina/supervisord) — process manager (ochinchina's Go port, single `supervisord` binary)

## Setup

### 1. Clone the repo

```bash
git clone <repo-url> /home/pi/ems
```

### 2. Install systemd service

```bash
sudo cp ems.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ems
```

This starts supervisord which manages three processes:

| Program   | Description                                       |
|-----------|---------------------------------------------------|
| `core`    | Runs `core/main.py` via `uv`                      |
| `fan`     | Runs `fan/main.py` via `uv`                       |
| `updater` | Polls GitHub every 5 min and restarts on new commits |

## Auto-update

`update.sh` runs in a loop, pulling from GitHub every 5 minutes. If new commits are detected it runs `sudo systemctl restart ems` to restart the whole stack. No change means no restart.

To allow the `pi` user to restart the service without a password prompt, add to `/etc/sudoers`:

```
pi ALL=(ALL) NOPASSWD: /bin/systemctl restart ems
```

## Useful Commands

```bash
# Check service status
sudo systemctl status ems

# Restart everything
sudo systemctl restart ems

# Stop / start
sudo systemctl stop ems
sudo systemctl start ems

# View logs
tail -f /tmp/ems_core.log
tail -f /tmp/ems_fan.log
tail -f /tmp/ems_updater.log
```
