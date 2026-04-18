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
   - `outdoor < setpoint < room` → fan **ON** (outside is cooler than setpoint; free cooling)
   - anything else → fan **OFF**

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

## Testing

To force the fan on regardless of conditions, set `TEST_FAN_ON = True` in `main.py`. Remember to set it back to `False` for normal operation.
