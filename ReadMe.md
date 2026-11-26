# EMS

Energy Management System with `core`, `fan`, and the [operator portal](portal/README.md). Use the portal setup to configure the operator account and its Python environment.

## Project Structure

```
ems/
├── core/           # Core subproject
├── fan/            # Fan subproject
├── portal/         # Operator web console
├── system_state.py # Shared controller settings
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
git clone https://github.com/OpenCampusFarm/ems /home/pi/ems
```

### 2. Install the Python environments and systemd service

Follow the [Raspberry Pi setup](portal/README.md#install-on-the-raspberry-pi). On a new installation, also enable startup at boot with `sudo systemctl enable ems.service`.

This starts supervisord, which manages the following processes:

| Program   | Description                                          |
| --------- | ---------------------------------------------------- |
| `core`    | Runs `core/main.py` from the core virtual environment                         |
| `fan`     | Runs `fan/main.py` from the fan virtual environment                          |
| `dashboard_publisher` | Publishes power and carbon readings to the farm website |
| `portal`  | Runs the operator web console                          |
| `updater` | Polls GitHub every 2 min and restarts on new commits |

## Auto-update

`update.sh` runs in a loop, pulling from GitHub every 2 minutes. If new commits are detected it runs `sudo systemctl restart ems` to restart the whole stack. No change means no restart.

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
tail -f /tmp/ems_portal.log
```
