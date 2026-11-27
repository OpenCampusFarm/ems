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
3. Use the CoolBot clean-energy target (default 45°F) if PV is at least 500 W or MOER is below 1400 lbs CO₂/MWh. Otherwise use the other target (default 50°F). The portal saves both values in `system.db` and core reads them each cycle.
4. Estimate surplus solar by subtracting the non-EV load from SolArk's PV output. Apply the saved EV charging policy.

The portal records eGauge circuits, line voltages, frequency and meter environment along with Sol-Ark inverter and battery readings each cycle. It prefers the eGauge grid reading and uses signed Sol-Ark grid power when that reading is missing. These source choices apply to monitoring. EV decisions use the Sol-Ark PV and load readings and the OpenEVSE charging power.

- **Staged (default)** requests the highest of 6, 20, 34, or 48 A that estimated surplus solar covers at 240 V. It pauses below 1.44 kW of estimated surplus.
- **Binary** requests 12 A when estimated surplus is at least 2.88 kW and pauses below that threshold.

Both policies use an OpenEVSE claim (`POST /claims/<id>`). The charger applies its configured limits and the vehicle can draw less than the requested current. Actual grid imports can vary with meter timing and changes in load between polls. The charger’s manual override can take precedence. The car ends the charge itself when full.

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
