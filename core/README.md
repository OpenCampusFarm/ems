# core

Campus Farm EMS real-time control loop. Reads solar/grid power and carbon intensity every 5 minutes, then adjusts the CoolBot setpoint and OpenEVSE EV charging accordingly.

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
    └── openevse.py         # OpenEVSE charger (HTTP API, EVSE claim)
```

## Decision Logic

Every `POLL_INTERVAL` seconds (default 300s):

1. Read SolArk inverter — PV watts, grid watts
2. Read WattTime — grid carbon intensity (MOER, lbs CO₂/MWh)
3. Determine if energy is **clean**: PV ≥ 500W **or** MOER < 1400 lbs CO₂/MWh
4. Apply decision to the CoolBot:

(Each cycle also logs eGauge grid / CoolBot / EV power for monitoring; it does not affect the decision.)

| Condition | CoolBot setpoint |
| --------- | ---------------- |
| Clean     | 45°F             |
| Dirty     | 50°F             |

5. EV charging from excess PV (PV minus site loads, EV excluded), set with `EV_MODE`. No grid or night charging.
   - `binary` (default): charge at 12 A while excess PV is at least 2880 W, otherwise block
   - `staged`: charge at the highest of 6, 20, 34 or 48 A that excess PV fully covers, otherwise block

EV charging is switched with an **EVSE claim** (`POST /claims/<id>`, state `active`/`disabled`, API priority 500), not the manual override, so the charger's LCD / manual override (priority 1000) can still take control. Vehicle SOC is not used; the car ends the charge itself when full.

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
| `OPENEVSE_URI` / `OPENEVSE_USER` / `OPENEVSE_PASSWORD`  | OpenEVSE charger (default `http://openevse.local`, auth optional) |
| `OPENEVSE_CLAIM_ID`                                     | Numeric EVSE claim client id (default 20) |
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
