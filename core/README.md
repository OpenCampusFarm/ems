# core

Campus Farm EMS real-time control and offline simulation.

The real-time energy management system (EMS) reads solar/grid power and carbon intensity every 5 minutes, then adjusts the CoolBot setpoint and OpenEVSE charging.

The offline simulation compares **No EMS**, **Binary EMS**, and **Staged EMS** using measured solar production, grid emissions, outdoor temperatures, and models of the cooler, truck, fans, and stationary battery.

## Project Structure

```text
core/
├── main.py                       # Real-time entry point
├── real_time_ems.py               # Main EMS control loop
├── solArk_inverter.py             # SolArk inverter API client
├── egauge_client.py               # eGauge power meter client
├── egauge_client_test.py          # Manual eGauge connectivity test
├── simulation_ems.py              # Offline models, inputs, and simulation engine
├── run_simulation_ems.py          # Simulation CLI, summaries, and plotting 
├── ev_real_data_simulation.py     # Base EV model used by the simulation
├── ev_demo_outputs.py             # EV-specific simulation outputs
├── integrated_demo_outputs.py     # Integrated simulation reports and plots
├── binary_charge_speed.py         # Sweep Binary charging currents and solar charging rules
├── cooler_analysis.py             # Plot cooler energy, temperature ranges, and violations
├── cooler_validation.py           # Compare measured and simulated cooler temperatures
└── validate_model.py              # Compare original/tuned models with SolArk inverter data
├── Data/                          # Solar, emissions, and outdoor temperature inputs
└── Loads/
    ├── coolbot.py                 # CoolBot AC controller (Blynk WebSocket)
    └── openevse.py                # OpenEVSE charger (HTTP API, EVSE claim)
```

## Real-Time Decision Logic

Every `POLL_INTERVAL` seconds (default 300 seconds):

1. Read the SolArk inverter: PV watts and grid watts.
2. Read WattTime: marginal operating emissions rate (MOER), in lb CO₂/MWh.
3. Determine whether energy is **clean**:
   - PV ≥ 500 W, **or**
   - MOER < 1,400 lb CO₂/MWh.
4. Apply the CoolBot setpoint:

| Condition | CoolBot setpoint |
| --- | --- |
| Clean | 45 °F |
| Dirty | 50 °F |

Each cycle also logs eGauge grid, CoolBot, and EV power for monitoring.

5. Select EV charging from excess PV: PV minus site loads, excluding the EV.

Charging mode is set with `EV_MODE`:

- **`binary` (default):** charge at 12 A while excess PV is at least 2,880 W; otherwise block charging.
- **`staged`:** charge at the highest of 6, 20, 34, or 48 A that excess PV fully covers; otherwise block charging.


EV charging is switched with an **EVSE claim** (`POST /claims/<id>`, state `active`/`disabled`, API priority 500). The charger’s LCD/manual override (priority 1000) can still take control.

Vehicle state of charge (SOC) is not used by the real-time controller; the vehicle ends charging when it reaches its configured limit.

## Setup

Copy `.env.example` to `.env` and fill in credentials:

```bash
cp .env.example .env
```

Key variables:

| Variable | Description |
| --- | --- |
| `WT_USERNAME` / `WT_PASSWORD` | WattTime API credentials |
| `SIT_EMAIL` / `SIT_PASSWORD` | CoolBot (Store It Cold) credentials |
| `SOLARK_USERNAME` / `SOLARK_PASSWORD` | SolArk cloud credentials |
| `OPENEVSE_URI` / `OPENEVSE_USER` / `OPENEVSE_PASSWORD` | OpenEVSE charger; default URI is `http://openevse.local`, authentication optional |
| `OPENEVSE_CLAIM_ID` | Numeric EVSE claim client ID; default 20 |
| `EGAUGE_METER_NAME` / `EGAUGE_USER` / `EGAUGE_PASSWORD` | eGauge meter configuration |

## Running the Real-Time EMS

From `core/`:

```bash
uv run python main.py
```

## Offline Simulation

The simulation does not require hardware connections or real-time API credentials. It uses local input files.

### Simulation Files

**`simulation_ems.py`** contains:

- Equipment parameters.
- Cooler, fan, truck, and stationary battery models.
- EMS control rules.
- Input loading and alignment.
- Delivery scheduling.
- Minute-by-minute execution.

**`run_simulation_ems.py`** contains:

- Command-line options.
- Execution of all three controller modes.
- Summary calculations.
- CSV output and terminal reporting.
- Plotting helpers and calls to the report-generation modules.

### Requirements

Use Python 3.10 or later.

The simulation directly uses:

- `numpy`
- `pandas`
- `matplotlib`
- `openpyxl`

These packages must be available in the project environment. 

### Running

Run simulation commands from the **repository root**, because default input paths begin with `core/Data/`.

```bash
uv run python core/run_simulation_ems.py
```

Custom input files:

```bash
uv run python core/run_simulation_ems.py \
  --week-pv-xlsx path/to/solar.xlsx \
  --moer-csv path/to/moer.csv \
  --outdoor-csv path/to/temperatures.csv \
  --output-dir results/integrated_simulation
```

Display plots:

```bash
uv run python core/run_simulation_ems.py --show-plot
```

Additional scenario options:

```bash
uv run python core/run_simulation_ems.py --battery-first
uv run python core/run_simulation_ems.py --compare-fan-power
```


### Command-Line Options

| Option | Description |
| --- | --- |
| `--week-pv-xlsx` | SolArk Operation Data Excel export |
| `--moer-csv` | WattTime emissions CSV |
| `--outdoor-csv` | Outdoor temperature CSV |
| `--output-dir` | Output directory; default `results/integrated_simulation` |
| `--compare-fan-power` | Request additional Binary/Staged runs with fans disabled |
| `--show-plot` | Display generated figures |

### Default Inputs

```text
core/Data/PV_2026-09-20_to_2026-09-27.xlsx
core/Data/watttime_MISO_DETROIT_2026-09-13_to_09-26_utc.csv
core/Data/outdoor_temperatures_2026-09-12_to_09-27.csv
```
Simulation timestamps use **America/Detroit** by default.

### Input Formats

#### Solar: SolArk Excel export

- First worksheet.
- Headers on Excel row 6 (`header=5`).
- `Time` column containing local timestamps.
- Exactly four columns whose names begin with `Ppv`.
- PV values in watts.

The loader sums the PV channels, converts to kW, averages duplicate minute timestamps, and aligns the result to whole days at one-minute resolution.

Missing values are forward-filled and then backward-filled without a maximum gap limit. Negative PV values raise an error.

#### Grid emissions: WattTime CSV

Required columns:

- `point_time`: UTC timestamp.
- `value`: MOER in lb CO₂/MWh.

Observations are forward-filled to minute resolution within the source data’s span. Missing simulation minutes use the corresponding timestamp seven days earlier.

Unresolved missing values raise an error. The input table records previous-week replacements in `moer_filled`.

#### Outdoor temperatures: CSV

Required columns:

- `timestamp`: local, timezone-naive timestamp.
- `temperature_f`: outdoor temperature in °F.

Hourly data are interpolated to minute resolution.

### Simulated Controllers

| Mode | Cooler setpoint | Truck charging | Fans |
| --- | --- | --- | --- |
| No EMS (`without-ems`) | Fixed 38 °F | Maximum modeled power when charging is requested | Disabled |
| Binary EMS (`with-ems`) | 45 °F clean / 50 °F dirty | 12 A / 2.88 kW when surplus covers it | Enabled by default |
| Staged EMS (`staged-ems`) | 45 °F clean / 50 °F dirty | Highest covered level: 6/20/34/48 A | Enabled by default |


#### Cooler and fans

- First-order resistance-capacitance thermal model.
- Thermostat switching band: ±1 °F.
- Total installed fan electrical draw: 62 W.
- Fans run when the cooler is above 34 °F and outdoor air is colder than the cooler.
- Fans take priority over compressor operation.

#### Delivery truck

- Battery capacity: 131 kWh.
- Charging efficiency: 90%.
- Maximum charging power: 11.28 kW.
- Initial and target SOC: 80%.
- Charging resumes at 79% SOC or below.
- Deliveries: Tuesday and Thursday, 9:00 a.m.–4:00 p.m., America/Detroit.
- Each delivery consumes 10 percentage points of SOC.


#### Stationary battery

The model has:

- Nominal capacity: 5.12 kWh.
- Maximum charging power: 2.56 kW.
- SOC range: 15–100%.
- Charging efficiency: 97.2%.

Solar supplies loads first, then charges the stationary battery, then exports remaining power.

The battery charges only from excess solar and can discharge for eligible cooler and other farm loads, not the truck.

The time-of-use schedule limits discharge to 2 kW and prevents discharge from 9:00 a.m. to 7:00 p.m. through a 100% SOC floor.

#### Battery warm-up

`run_week()` runs two passes:

1. A first pass starting the stationary battery at 15% SOC.
2. A reported pass starting at the first pass’s ending battery SOC.

### Outputs

```text
results/integrated_simulation/
└── week-<start-date>/
    ├── summary.csv
    ├── data/
    │   ├── without-ems_minutes.csv
    │   ├── with-ems_minutes.csv
    │   └── staged-ems_minutes.csv
    └── additional reports and charts
```

Battery-first scenarios use a `_battery-first` directory suffix.

Additional reports and charts are generated by `ev_demo_outputs.py` and `integrated_demo_outputs.py`.

### Summary Metrics

`summary.csv` includes:

- Grid purchases and exports in kWh.
- Estimated emissions from grid purchases in lb CO₂.
- Reported self-sufficiency percentage.
- Cooler, fan, compressor, and EV energy.
- Truck charging hours and charger starts.
- Reserve charging events and energy.
- Minimum, final, and departure truck SOC.
- Departures below 30% SOC.
- Cooler temperature extrema and out-of-range minutes.


# Analysis and Validation Tools

These tools supplement the integrated simulation with charging-current experiments, cooler analysis, and comparisons against measured farm data.

Run commands from the **repository root** so paths beginning with `core/Data/` resolve correctly.

### Binary Charging Current Sweep

**File:** `binary_charge_speed.py`

Tests which fixed Binary charging currents can keep up with the truck’s modeled delivery use while limiting additional grid purchases.

#### Scenarios

The default analysis uses two September weeks of measured inputs and compares:

- Original September PV production.
- PV scaled to 40% as a rough low-solar stress test—not measured late-fall data.

Charging currents tested:

```text
6, 8, 10, 12, 16, 20, 24, 32, 40, and 47 A
```

Charging rules tested:

- PV above a fixed threshold: 0.5, 1.5, 2, 3, 4, 5, or 6 kW.
- Solar surplus after site loads of at least 3 kW.
- Solar surplus after site loads sufficient to cover charger power.
- Solar surplus after site loads and reserved battery charging capacity sufficient to cover charger power.

Charging schedules tested:

- Every day.
- Friday through Monday only.

Fixed PV-threshold rules do not necessarily ensure solar covers charging power. They can therefore cause grid purchases even though charging is triggered by solar production.


#### Running

Default September and reduced-PV cases:

```bash
uv run python -m core.binary_charge_speed
```

Custom Sunday-to-Saturday week:

```bash
uv run python -m core.binary_charge_speed \
  --pv-xlsx path/to/solar-week.xlsx \
  --outdoor-csv path/to/temperatures.csv \
  --name october
```

Repeated-weather tests:

```bash
uv run python -m core.binary_charge_speed --weather-test
```

#### Outputs

Default output directory:

```text
results/integrated_simulation/binary_charge_speed/
├── all_results.csv
└── charging_speed.png
```

Custom-week results:

```text
results/integrated_simulation/binary_charge_speed/<name>/
└── all_results.csv
```

Weather-test results:

```text
results/integrated_simulation/binary_charge_speed/weather_test/
├── results.csv
└── weather_test.png
```

Reported metrics include:

- Tuesday departure SOC and whether charging keeps up.
- Weekly EV charging energy, hours, and starts.
- Weekly grid purchases, exports, and emissions.
- Added grid purchases and emissions relative to a no-truck-charging baseline.

The reported `ev_grid_percent` is an incremental comparison against that baseline, not a direct measurement of the truck’s energy source.

### Cooler Summary Analysis

**File:** `cooler_analysis.py`

Creates a three-panel cooler report using an integrated simulation’s `summary.csv` and controller minute files:

1. Cooler electricity consumption.
2. Minimum-to-maximum cooler temperature.
3. Minutes below and above the evaluation thresholds.

An optional measured-energy reference adds simulated-versus-measured differences.

#### Running

```bash
uv run python core/cooler_analysis.py \
  results/integrated_simulation/week-2026-09-20/summary.csv
```

Compare against measured cooler energy for the same period:

```bash
uv run python core/cooler_analysis.py \
  results/integrated_simulation/week-2026-09-20/summary.csv \
  --measured-cooler-kwh 42
```

Choose an output path:

```bash
uv run python core/cooler_analysis.py \
  results/integrated_simulation/week-2026-09-20/summary.csv \
  --output results/cooler_summary.png
```

#### Inputs and Validation

Required summary columns:

- `controller`
- `cooler_kwh`
- `cooler_unsafe_minutes`

Minute files are expected under the week directory’s `data/` folder:

```text
data/without-ems_minutes.csv
data/with-ems_minutes.csv
data/staged-ems_minutes.csv
```

Each minute file must contain:

- `timestamp`
- `cooler_temp_f`


#### Output

Default output:

```text
<week-directory>/cooler_summary.png
```

### Cooler Temperature Validation

**File:** `cooler_validation.py`

Compares measured cooler temperatures with a 30-day cooler simulation using:

- Measured outdoor temperatures.
- Synthetic daytime PV.
- Synthetic grid MOER.
- The EMS cooler setpoint rule.

#### Required Inputs

```text
core/Data/room_temperature_30d.csv
core/Data/outdoor_temperatures.csv
```

Required room-temperature columns:

- `timestamp_local`
- `room_temp_f`
- `set_temp_f`

Required outdoor-temperature columns:

- `timestamp`
- `temperature_f`

#### Running

```bash
uv run python -m core.cooler_validation
```

The script displays:

1. Measured cooler temperature, setpoint, and outdoor temperature.
2. Simulated cooler temperature and setpoint at matching measurement times.
3. Measured versus simulated cooler temperature.

### SolArk Model Validation

**File:** `validate_model.py`

Compares real SolArk inverter records with:

- An original, untuned model.
- A tuned model using the configured inverter losses, battery charging efficiency, EV draw, and temperature-dependent cooler electricity.

The plotted comparison covers **September 20–26, 2026**.

#### Running

```bash
uv run python -m core.validate_model
```

Include historical battery-to-EV discharge and charging taper:

```bash
uv run python -m core.validate_model --battery-powers-ev
```

The optional case allows the tuned model’s battery to cover EV demand when discharge is permitted, including after 7:00 p.m., and adds reduced EV charging power near full SOC.

#### Required Inputs

In addition to the standard MOER and outdoor-temperature CSVs, this tool reads both September SolArk workbooks.

The exports must include the daily energy counters and power/SOC registers referenced in `load_real()`, including grid purchases, exports, load energy, PV energy, battery charge/discharge energy, and battery SOC.

#### Outputs

```text
results/integrated_simulation/validation/
├── daily_comparison.csv
├── V1_daily_real_vs_model.png
├── V2_hourly_profile.png
└── V3_battery_soc_real_vs_model.png
```

The optional battery-to-EV case writes the same output names under:

```text
results/integrated_simulation/validation/battery_powers_ev/
```

Terminal reports include:

- Weekly energy totals and model differences.
- Mean absolute daily errors on non-EV days.
- Times at which the battery reaches its minimum SOC.








