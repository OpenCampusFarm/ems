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
- [supervisord](https://github.com/ochinchina/supervisord) — process manager (ochinchina's Go port)

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

| Program   | Description                                      |
|-----------|--------------------------------------------------|
| `core`    | Runs `core/main.py` via `uv`                     |
| `fan`     | Runs `fan/main.py` via `uv`                      |
| `updater` | Polls GitHub every 60s and reloads on new commits |

## Auto-update

`update.sh` runs in a loop, pulling from GitHub and calling `supervisorctl reload` only when new commits are detected. The poll interval defaults to 60 seconds and can be overridden via the `UPDATE_INTERVAL` environment variable in `supervisord.conf`.

## Useful Commands

```bash
# Check service status
sudo systemctl status ems

# View logs
tail -f /tmp/ems_core.log
tail -f /tmp/ems_fan.log
tail -f /tmp/ems_updater.log

# Manually reload all programs
supervisorctl reload
```
