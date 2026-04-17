# core

Campus Farm EMS real-time control loop. Reads solar/grid power and carbon intensity every 5 minutes, then adjusts the CoolBot setpoint and Ford EV charging accordingly.

## Project Structure

```
core/
├── main.py                 # Entry point
├── real_time_ems.py        # Main EMS control loop
├── solArk_inverter.py      # SolArk solar inverter API client
├── egauge_client.py        # eGauge power meter client
├── egauge_client_test.py   # Manual eGauge connectivity test
├── simulation.py           # 1-day physics simulation (offline)
└── Loads/
    ├── coolbot.py          # CoolBot AC controller (Blynk WebSocket)
    └── ev_battery.py       # Ford EV battery via Home Assistant
```

## Decision Logic

Every `POLL_INTERVAL` seconds (default 300s):

1. Read SolArk inverter — PV watts, grid watts
2. Read WattTime — grid carbon intensity (MOER, lbs CO₂/MWh)
3. Determine if energy is **clean**: PV ≥ 500W **or** MOER < 1400 lbs CO₂/MWh
4. Apply decision:

| Condition | CoolBot setpoint | EV charging          |
| --------- | ---------------- | -------------------- |
| Clean     | 45°F             | ON (if SOC < target) |
| Dirty     | 50°F             | OFF                  |

## Setup

Copy `.env.example` to `.env` and fill in credentials:

```bash
cp .env.example .env
```

Key variables:

| Variable                                                | Description                         |
| ------------------------------------------------------- | ----------------------------------- |
| `WT_USERNAME` / `WT_PASSWORD`                           | WattTime API credentials            |
| `SIT_EMAIL` / `SIT_PASSWORD`                            | CoolBot (Store It Cold) credentials |
| `SOLARK_USERNAME` / `SOLARK_PASSWORD`                   | SolArk cloud credentials            |
| `HA_URI` / `HA_TOKEN` / `HA_VIN`                        | Home Assistant for Ford EV          |
| `EGAUGE_METER_NAME` / `EGAUGE_USER` / `EGAUGE_PASSWORD` | eGauge meter                        |

## Running

```bash
uv run python main.py
```

### Simulation (no hardware needed)

```bash
uv run python simulation.py                   # sine-wave PV approximation
uv run python simulation.py --csv PVdata.csv  # real PV data from CSV
```

Managed by supervisord as part of the `ems` project — see the root `supervisord.conf`.
