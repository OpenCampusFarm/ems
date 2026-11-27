# fan

Controls a ventilation fan on a Raspberry Pi based on outdoor temperature and CoolBot AC state.

## Hardware

- DS18B20 1-Wire temperature sensor (outdoor, connected to the Pi)
- Relay on GPIO23 (pin 16) controlling the fan
- CoolBot AC controller communicating via Blynk WebSocket

## Fan logic

Auto checks temperatures every 30 seconds. The fan can cool the room before CoolBot calls for cooling at its own setpoint.

- **On** when the room reaches 0.5°F below the setpoint and outdoor air is at least 3°F cooler than the room.
- **Off** when the room reaches 1°F below the setpoint, the outdoor air is at most 1°F cooler than the room, or CoolBot is off, offline, or missing data.
- **Hold** the current fan state between thresholds.

At a 45°F CoolBot setpoint, the fan can start at 44.5°F and stops at 44°F. The thresholds follow the active setpoint.

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
