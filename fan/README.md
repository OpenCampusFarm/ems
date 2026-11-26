# fan

Controls a ventilation fan on a Raspberry Pi based on outdoor temperature and CoolBot AC state.

## Hardware

- DS18B20 1-Wire temperature sensor (outdoor, connected to the Pi)
- Relay on GPIO23 (pin 16) controlling the fan
- CoolBot AC controller communicating via Blynk WebSocket

## Fan logic

Every 30 seconds:

1. Connect to the CoolBot and read its state
2. If CoolBot is **offline or off** → fan OFF
3. If CoolBot is **on**, read outdoor temp (`outdoor`) and compare with room temp (`room`) and CoolBot setpoint (`setpoint`):
   - Fan turns **ON** when: `outdoor + 2 < setpoint` AND `room > setpoint + 2`
   - Fan turns **OFF** when: `outdoor >= setpoint` OR `room <= setpoint`
   - Otherwise fan state is **unchanged** (hysteresis deadband prevents rapid on/off cycling)

The 2°F hysteresis (`HYSTERESIS` in `main.py`) ensures the fan doesn't oscillate when temperatures are close to the setpoint.

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
