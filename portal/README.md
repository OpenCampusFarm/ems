# Farm Energy Portal

Python, Flask and HTMX operator console for the Campus Farm Raspberry Pi.

- **Fan** selects Off, On, or Auto. Auto runs the existing CoolBot and outdoor-temperature rules. Manual modes set the relay without waiting for a network connection.
- **EMS** selects On or Off. Off stops EMS automation and lets its shutdown routine release its OpenEVSE claim. OpenEVSE may charge under its own settings. The portal does not set charger current or change the charging policy.
- **Monitoring** shows the supervisord process states and EMS measurements at selectable time scales. Solar comes from Sol-Ark, grid power comes from eGauge, and The first card shows the latest OpenEVSE charge current in amps. The chart shows charge power in watts alongside solar and grid power. Negative grid power means export. A running service alone does not confirm hardware state.

The shared `../system.db` has one table, `system`, and one row. With no file, the fan controller starts in Auto and the EMS starts On. The portal creates the file on the first submitted mode. A changed mode is saved before supervisord applies it to the corresponding process. A failed process change leaves the requested mode saved and shows a Retry button. Both controllers read the database at startup. The separate `../telemetry.db` holds EMS measurements for 3,650 days. At a regular five-minute cadence this is about 1.05 million samples. The EMS prunes expired rows as it writes new readings. The first write imports the existing `../ems-telemetry.json` once, without removing the file. The portal can display the legacy snapshot before that import. Readings become stale after 12 minutes and gaps are visible in the chart.

Power flow defaults to the last 24 hours. Minutes shows actual readings from the last hour, Hours shows hourly means over 12 hours, and Week shows hourly means over seven days. Month and 3 months show daily means over 30 and 90 days. Year shows weekly means over 365 days. Daily and weekly buckets follow the farm's Eastern time, with weeks starting Monday. Averages use the measurements available in each bucket and omit missing readings.

The purple line uses the right-hand axis in kg CO₂/hour. It shows the modeled grid emissions without EMS minus the measured grid emissions. Both use the same WattTime marginal emissions rate, and exported solar receives no emissions credit. Negative values remain visible. Longer views weight the estimate by the duration of the available readings.

Carbon tracking is enabled by default. The dashboard publisher saves estimates in `telemetry.db` without requiring Drupal credentials. Set `PUBLISH_CARBON=0` in `core/.env` to disable tracking. The line starts once fresh model inputs are available. Older power-only records and intervals without fresh WattTime or outdoor temperature readings leave gaps.

## Preview on macOS

Run these from the EMS checkout:

```bash
cd portal
uv run python app.py --demo
```

Open <http://127.0.0.1:8080> and sign in with the account in `portal/.env`. Without a configured account, the preview accepts `operator` with `farm-preview`. Use Ctrl+C in the terminal to stop. On the first run, uv creates the virtual environment and installs dependencies from `pyproject.toml`.

Demo mode uses a temporary SQLite database, synthetic telemetry, and a separate signing key. It runs independently of the farm services and never contacts supervisord. Logs appear in the terminal.

## Install on the Raspberry Pi

The existing `ems.service` starts Go supervisord, which manages `core`, `fan`, `portal`, and the automatic Git updater. The portal applies a mode to one program over a local Unix socket at `/run/campus-ems/supervisor.sock`. EMS Off stops the core program, and On starts it. With Off saved, core also exits at boot before starting automation. The fan process stays running in all three modes.

The service assumes the checkout is `/home/pi/ems`, the `pi` account has a primary group named `pi`, and the controller credentials are already configured.

As `pi`:

```bash
cd /home/pi/ems
(cd core && uv sync)
(cd fan && uv sync)
(cd portal && uv sync)
```

Create the operator account configuration in `portal/.env`, using the same setup as `core` and `fan`:

```bash
cd /home/pi/ems/portal
test -f .env || cp .env.example .env
chmod 600 .env
openssl rand -hex 32
```

Edit `.env` to set `PORTAL_USERNAME`, a strong `PORTAL_PASSWORD`, and `PORTAL_SECRET_KEY` using the generated value. The portal reads this file from its own directory at startup. A login lasts 60 days from sign-in. Keep the signing key stable across service restarts to preserve existing sessions.

Install the updated unit and restart it. On startup the unit enables the Portal supervisord configuration and creates the control socket directory:

```bash
cd /home/pi/ems
sudo install -m 0644 ems.service /etc/systemd/system/ems.service
sudo systemctl daemon-reload
sudo systemctl restart ems.service
systemctl status ems.service
```

The portal listens on `127.0.0.1:8080`. Point the HTTPS reverse proxy on the Pi (such as the planned Cloudflare Tunnel) at `http://127.0.0.1:8080`. The production login cookie requires HTTPS. Verify both controls against the hardware during an on-site test.

For future changes to `ems.service` itself, reinstall the unit and run `daemon-reload`. The Git updater continues to pull application changes and restart the stack.

## Cloudflare Tunnel

On the Pi, install the official Linux release binary matching the Pi's architecture at `/usr/local/bin/cloudflared`. Put the remotely managed Tunnel token in `/home/pi/.config/campus-ems/cloudflared-token` with permissions `0600`, owned by `pi`. The existing supervisord starts the connector on boot. The connector logs to `/tmp/ems_cloudflared.log`.

## Logs

Portal uses the same timestamp and log levels as core and fan. Supervisord captures its output on the Pi in `/tmp/ems_portal.log`. Use `tail -f /tmp/ems_portal.log` to follow startup, login, mode changes, and errors.
