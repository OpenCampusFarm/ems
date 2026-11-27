# portal

Flask and HTMX console for monitoring and controlling the Campus Farm EMS.

## Controls

- **Fan** selects Off, On, or Auto. In Auto, the badge shows the current relay state.
- **EMS** Off pauses CoolBot setpoint changes and returns charging control to OpenEVSE.
- **Charging policy** selects Binary or Staged. **CoolBot setpoints** lets you edit both temperature targets. [Core decision logic](../core/README.md#decision-logic)

Settings live in `system.db`. The defaults are Fan Auto, EMS On, and Staged charging. Charts read up to 3,650 days of history from `telemetry.db`.

The CO₂ chart shows two estimates. Ventilation compares four fans in Auto with AC providing the same cooling. Set `CARBON_FAN_POWER_W` in `core/.env` to replace the assumed 124 W total. EV charging compares the estimated solar share of measured charging with the same energy from the grid. Both use WattTime.

## Setup

From `portal/`, create the operator configuration.

```bash
test -f .env || cp .env.example .env
chmod 600 .env
openssl rand -hex 32
```

Set `PORTAL_USERNAME`, `PORTAL_PASSWORD`, and `PORTAL_SECRET_KEY` in `.env`. Use the generated hex string as the signing key.

## Preview on macOS

```bash
cd portal
uv run python app.py --demo
```

Open <http://127.0.0.1:8080>. With no configured password, use `operator` / `farm-preview`. Demo mode uses synthetic data and cannot control farm equipment.

## Install on the Raspberry Pi

Configure the three `.env` files, then install the Python environments and the shared [systemd service](../ems.service).

```bash
cd /home/pi/ems
(cd core && uv sync)
(cd fan && uv sync)
(cd portal && uv sync)

sudo install -m 0644 ems.service /etc/systemd/system/ems.service
sudo systemctl daemon-reload
sudo systemctl restart ems.service
```

## Cloudflare Tunnel

Install `cloudflared` at `/usr/local/bin/cloudflared`. Save the tunnel token to `/home/pi/.config/campus-ems/cloudflared-token` with owner `pi` and permissions `0600`. Point the published hostname to `http://127.0.0.1:8080` and open the published URL over HTTPS. Supervisord starts the portal and the tunnel. [Shared supervisord config](../supervisord.conf)

## Logs

```bash
tail -f /tmp/ems_portal.log /tmp/ems_cloudflared.log
```
