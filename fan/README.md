# fan

Controls a ventilation fan on a Raspberry Pi based on outdoor temperature and CoolBot AC state.

## Hardware

- DS18B20 1-Wire temperature sensor (outdoor, connected to the Pi)
- Relay on GPIO23 (pin 16) controlling the fan
- CoolBot AC controller communicating via Blynk WebSocket

## Fan logic

Auto checks temperatures every 30 seconds. The fan can cool the room before CoolBot calls for cooling at its own setpoint.

- **On** when the room reaches 1°F below the active CoolBot setpoint and outdoor air is at least 3°F cooler than the room. While EMS automation is on and CoolBot uses the Otherwise setpoint, the Clean energy target can start the fan sooner.
- **Off** when the room cools to 0.5°F below the start temperature, the outdoor air is at most 1°F cooler than the room, or CoolBot is off, offline, or missing data.
- **Hold** the current fan state between thresholds.

With a 45°F Clean energy target, the fan can start at 45°F and stop at 44.5°F when CoolBot is set to 50°F. When CoolBot is set to 45°F, those thresholds are 44°F and 43.5°F.

## Raspberry Pi Access

```bash
ssh pi@raspberrypi.local
```

Default password: `raspberry`

## Setup

Copy `.env.example` to `.env` and fill in credentials:

```bash
cp .env.example .env
```

```ini
SIT_EMAIL=your@email.com
SIT_PASSWORD=yourpassword
```

## Running

```bash
uv run python main.py
```

Managed by supervisord (Go implementation) as part of the `ems` project — see the root `supervisord.conf` and `ems.service`.

The service runs supervisord as root so the fan program can access GPIO. Core and updater programs run as `pi`.

## Fan mode override

The fan reads the shared `../system.db` at startup and defaults to Auto if the file is absent. Use the [operator portal](../portal/README.md) to change its mode and restart the fan process through supervisord.

| Value | Behavior |
|-------|----------|
| Auto | Apply the CoolBot and outdoor-temperature rules |
| On | Turn on the relay without waiting for network readings |
| Off | Turn off the relay without waiting for network readings |
