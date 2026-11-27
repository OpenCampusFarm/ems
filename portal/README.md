# Farm Energy Portal

Python, Flask and HTMX operator console for the Campus Farm Raspberry Pi.

- **Fan** selects Off, On, or Auto. Auto runs the existing CoolBot and outdoor-temperature rules. The Auto badge reports the fan relay's On or Off setting every five seconds. Manual modes set the relay without waiting for a network connection.
- **EMS** selects On or Off. Off stops automatic CoolBot setpoint changes and releases the EMS claim on OpenEVSE. CoolBot continues using its last setpoint. OpenEVSE may charge under its own settings.
- **EV charging policy** selects Binary or Staged. Binary requests a fixed 12 A when enough surplus solar is available. Staged requests the highest solar-covered step from 6, 20, 34, and 48 A. A policy change restarts core if EMS is on. With EMS off, the portal saves the policy for the next start.
- **Outdoor temperature** shows the Pi's DS18B20 reading in the top status row. If the sensor is unavailable, the portal labels its Open-Meteo fallback as an estimate.
- **CoolBot setpoints** saves two whole-degree Fahrenheit targets (34–65°F) to `system.db`. EMS selects the clean-energy target (default 45°F) when solar is at least 500 W or WattTime MOER is below 1400 lb CO₂/MWh, and otherwise selects the other target (default 50°F). Core reads both on its next cycle, normally within five minutes. With EMS off, the new targets are saved for the next start.
- **WattTime MOER** shows the marginal grid emissions rate from the latest EMS sample in lb CO₂/MWh, with the sample time in Eastern time. The card flags stale telemetry and shows a dash when that sample has no WattTime reading.
- **Cool room temperature** and **CoolBot setpoint** come from a read-only CoolBot WebSocket poll every 30 seconds. The portal uses the CoolBot credentials from `core/.env` and reads independently of EMS or fan mode. If a reading is over a minute old, both cards show when the portal last read it.
- **Farm overview** pairs the six headline cards with a solar, grid, and EV power chart. The grid uses eGauge first and falls back to Sol-Ark. EV circuit power uses eGauge first and falls back to OpenEVSE. The charge-current card uses the reading from OpenEVSE.
- **Sol-Ark** shows solar, the inverter's own grid reading, load, battery power and charge, generator power, and microinverter power. The chart switches between watts, battery charge, and individual numeric inverter registers. **All inverter readings** displays the latest diagnostic record with its timestamp, API units, and status codes. Diagnostic history starts with the first successful record saved by core. Set `SOLARK_DEVICE_SN` in `core/.env` for another inverter.
- **eGauge** shows watts for the grid legs, cooler circuit, and EV circuit. Its chart also shows eGauge total grid power and omits readings from the overview's backup sources. **All eGauge readings** shows the latest meter registers and live channel rates with their original units and EMS sample times.

The shared `../system.db` has one table, `system`, and one row. With no file, the fan starts in Auto, EMS starts On, and the charging policy defaults to Staged. Existing databases retain both controller modes and default to Staged until a policy is saved. The portal creates or updates the schema on the first submitted mode. A failed process change leaves the requested mode saved and shows a Retry button. Both controllers read the database at startup. The separate `../telemetry.db` holds EMS measurements for 3,650 days. At a regular five-minute cadence this is about 1.05 million samples. The EMS prunes expired rows as it writes new readings. The first write imports the existing `../ems-telemetry.json` once, without removing the file. The portal can display the legacy snapshot before that import. Readings become stale after 12 minutes and gaps are visible in the chart.

Each chart has its own time range, which defaults to the last 24 hours. Negative grid power means export, and negative stationary battery power means charging. Minutes shows actual readings from the last hour, Hours shows hourly means over 12 hours, and Week shows hourly means over seven days. Month and 3 months show daily means over 30 and 90 days. Year shows weekly means over 365 days. Daily and weekly buckets follow the farm's Eastern time, with weeks starting Monday. Averages use the measurements available in each bucket and omit missing readings.

The bottom chart shows estimated CO₂ avoided in kg/hour. It shows the modeled grid emissions without EMS minus the measured grid emissions. Both use the same WattTime marginal emissions rate, and exported solar receives no emissions credit. Positive values mean lower measured emissions than the baseline, zero means equal, and negative values mean higher measured emissions. Hover, tap, or use the arrow keys on a chart to inspect values and their measurement times or averaging windows. Longer views weight the carbon estimate by the duration of the available readings.

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

Install the updated unit and restart it. It creates the control socket directory, then starts the root `supervisord.conf`, which manages the entire stack:

```bash
cd /home/pi/ems
sudo install -m 0644 ems.service /etc/systemd/system/ems.service
sudo systemctl daemon-reload
sudo systemctl restart ems.service
systemctl status ems.service
```

The portal listens on `127.0.0.1:8080`. Point the HTTPS reverse proxy on the Pi (such as the planned Cloudflare Tunnel) at `http://127.0.0.1:8080`. The production login cookie requires HTTPS. Verify both controls against the hardware during an on-site test.

For future changes to `ems.service` itself, reinstall the unit and run `daemon-reload`. The Git updater pulls application changes and restarts the stack. Older installed units still copy the comment-only `portal/supervisord.conf` during startup. Keep that placeholder until those units have been updated.

## Cloudflare Tunnel

On the Pi, install the official Linux release binary matching the Pi's architecture at `/usr/local/bin/cloudflared`. Put the remotely managed Tunnel token in `/home/pi/.config/campus-ems/cloudflared-token` with permissions `0600`, owned by `pi`. The existing supervisord starts the connector on boot. The connector logs to `/tmp/ems_cloudflared.log`.

## Logs

Portal uses the same timestamp and log levels as core and fan. Supervisord captures its output on the Pi in `/tmp/ems_portal.log`. Use `tail -f /tmp/ems_portal.log` to follow startup, login, mode changes, and errors.
