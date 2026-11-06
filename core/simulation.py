"""
Campus Farm EMS simulation: No EMS vs Binary EMS vs Staged EMS, one week.

Farm setup (decided): inverter on Load First, one battery charged from excess solar only,
OpenEVSE charger. Values marked "tuned" were fitted to the Sol-Ark data 9/13–9/26.

  - PV     : Sol-Ark export, one minute at a time
  - Grid   : WattTime MOER (grid CO2)
  - Cooler : first-order RC thermal model with bang-bang thermostat
  - EV     : the farm truck
  - Battery: stationary Sol-Ark battery (Pytes V5)

Truck: charge limit EV_SOC_TARGET (80%), deliveries Tuesday and Thursday 9 AM–4 PM using
10% each, plugged in the rest of the week.

Truck charging, decided every DECISION_MINUTES like the real EMS, from excess solar only:
  - No EMS : full power whenever below the limit
  - Binary : BINARY_AMPS (12 A = 2.88 kW) while excess solar covers it
  - Staged : highest of STAGED_AMPS (6/20/34/48 A) that excess solar covers

Usage (from the repo folder):
    python core/simulation.py                              # week of 9/20
    python core/simulation.py --week-pv-xlsx <Sol-Ark week export> --outdoor-csv <temps>
    python core/simulation.py --battery-first              # battery fills before the truck
Outputs: results/integrated_simulation/week-<start date>/
"""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ev_real_data_simulation import EV

# ── Constants ─────────────────────────────────────────────────────────────────
# SOC = state of charge (how full a battery is). MOER = grid CO2 per MWh right now (WattTime).
SETPOINT_COOLTH  = 45     # °F — low setpoint (clean energy available), real_time_ems.py
SETPOINT_ECON    = 50     # °F — high setpoint (dirty / no renewable energy), real_time_ems.py
SETPOINT_NO_EMS  = 38     # °F — No EMS: the real CoolBot's fixed setpoint
EV_CAPACITY      = 131.0  # kWh  (F-150 Lightning extended range)
EV_CHARGE_EFF    = 0.90   # 90% of wall energy ends up in the truck battery
EV_SOC_TARGET    = 0.80   # truck charge limit
EV_RESUME_MARGIN = 0.01   # once at the limit, charging restarts only this far below it
DECISION_MINUTES = 5      # the EMS decides truck charging this often (real_time_ems.py POLL_INTERVAL)

PV_MIN_PRODUCING = 0.5    # kW — threshold to count PV as "producing"
CO2_THRESHOLD    = 1400   # lbs CO2/MWh — grid cleanliness threshold

OUTDOOR_TEMP     = 70.0   # °F — starting outdoor temp; real hourly data replaces it
TMIN             = 40.0   # °F — safety minimum
TMAX             = 55.0   # °F — safety maximum (spoilage prevention)

# Cooler electricity while on = COOLER_ELECTRIC_KW + COOLER_ELECTRIC_KW_PER_F × (outdoor − 60 °F).
COOLER_ELECTRIC_KW       = 0.207    # kW at 60 °F outdoors (tuned)
COOLER_ELECTRIC_KW_PER_F = 0.0165   # kW more per °F warmer outside (tuned)
COOLER_ELECTRIC_REF_F    = 60.0     # °F, reference outdoor temp for the line above
OTHER_LOAD_KW      = 0.25   # kW, wifi/electronics room and controllers on the load port, always on (lights off)

# Fan (placeholder until the fan team adds its temperature equation, see Cooler._fan_cooling_kw)
FAN_INSTALLED   = False   # True = Binary/Staged use the fan rules in Cooler.update
FAN_ELECTRIC_KW = 0.0     # TODO(team): fan power draw

# Inverter losses (tuned from the Sol-Ark daily counters)
INVERTER_STANDBY_KW     = 0.070   # kW, inverter self-use at night (tuned)
INVERTER_STANDBY_DAY_KW = 0.149   # kW, inverter self-use while PV is producing (tuned)
INVERTER_PV_LOSS        = 0.0148  # fraction of PV lost in conversion (tuned)
PV_STANDBY_THRESHOLD_KW = 0.05  # kW, above this PV counts as "daytime" for inverter self-use
EV_CHARGER_KW        = 11.28  # kW, truck draw at full power (tuned: inverter and eGauge agree)

# ── Stationary battery (Pytes V5) ─────────────────────────────────────────────
# 100 Ah × 51.2 V = 5.12 kWh. Max charge 50 A ≈ 2.56 kW. 100A discharge
BATT_CAPACITY_AH               = 100
BATT_NOMINAL_VOLTAGE           = 51.2   # V
BATT_CHARGE_CURRENT_MAX        = 50     # A
BATT_DISCHARGE_CURRENT_NOMINAL = 100    # A
BATT_SOC_MIN                   = 15     # %
BATT_SOC_MAX                   = 100    # %
BATT_CHARGE_EFF                = 0.972  # tuned: 52.7 Wh in per 1% SOC vs 51.2 Wh out
BATT_DISCHARGE_EFF             = 1.0

# Each slot runs until the next start: (start hour, max discharge kW, Batt % floor)
# Batt is the SOC the battery won't discharge below in that slot. Sell is unchecked in every
# slot. The battery is only charged from excess PV, never from the grid (Charge unchecked).
# PLEASE CHECK IF THIS IS MOST UPDATED TOU SETTING
BATT_TOU_SCHEDULE = (
    (0,  2.0, 15),
    (7,  2.0, 15),
    (9,  2.0, 100),
    (13, 2.0, 100),
    (17, 2.0, 100),
    (19, 2.0, 15),
)

# ── Charger ───────────────────────────────────────────────────────────────────
# OpenEVSE Advanced Series 48A (J1772)
OPENEVSE_VOLTS = 240
OPENEVSE_MIN_A = 6
OPENEVSE_MAX_A = 48
# Binary: one current, on or off. 12 A (2.88 kW) was the slowest current that kept the
# truck. Same as real_time_ems.py EV_CHARGE_AMPS.
BINARY_AMPS = 12
# Staged: off and 4 levels evenly spaced from 6 A to 48 A (14 A apart) = 6, 20, 34, 48 A.
# Same as real_time_ems.py EV_STAGED_AMPS.
STAGED_AMPS = tuple(
    OPENEVSE_MIN_A + i * (OPENEVSE_MAX_A - OPENEVSE_MIN_A) // 3 for i in range(4)
)
# amps × volts → kW: 12 A = 2.88 kW; staged = 1.44 / 4.8 / 8.16 / 11.52 kW
BINARY_KW = BINARY_AMPS * OPENEVSE_VOLTS / 1000
STAGED_KW = tuple(amps * OPENEVSE_VOLTS / 1000 for amps in STAGED_AMPS)


# Battery charged before truck false for now, setting it true only contributes 1lb improvement but less charging for truck
BATTERY_BEFORE_TRUCK = False


# The truck is plugged in all week except deliveries. It starts the week at its charge limit,
# and each delivery uses EV_TRIP_SOC_USED, drained evenly while it is away.
EV_TRIP_WEEKDAYS   = (1, 3) # Tuesday and Thursday (Monday = 0)
EV_TRIP_START_HOUR = 9      # unplugged 9:00 AM (assumed)
EV_TRIP_END_HOUR   = 16     # plugged back in 4:00 PM (assumed)
EV_TRIP_SOC_USED   = 0.10


# Default week: Sun 9/20 – Sat 9/26
DEFAULT_WEEK_PV     = Path("core/Data/PV_2026-09-20_to_2026-09-27.xlsx")
DEFAULT_MOER_CSV    = Path("core/Data/watttime_MISO_DETROIT_2026-09-13_to_09-26_utc.csv")
DEFAULT_OUTDOOR_CSV = Path("core/Data/outdoor_temperatures_2026-09-12_to_09-27.csv")

CONTROLLERS = ("without-ems", "with-ems", "staged-ems")   # "with-ems" = Binary
CONTROLLER_LABELS = {"without-ems": "No EMS", "with-ems": "Binary EMS", "staged-ems": "Staged EMS"}


# ── Cooler model ──────────────────────────────────────────────────────────────
class Cooler:
    """Walk-in cooler (CoolBot + AC), Rachel's model.

    temp = indoor (cooler room) °F, outdoor_temp = outside air °F.
    RC model: room drifts toward outdoor temp, AC pulls it down. Thermostat ±1 °F around setpoint.
    """
    def __init__(
        self,
        outdoor_f: float  = OUTDOOR_TEMP,
        setpoint_f: float = SETPOINT_ECON,
        power_kw: float   = 3.67,   # AC cooling power for the temperature model
        cop: float        = 2.0,    # cooling out per unit of electricity
        ci: float         = 0.2,    # thermal capacitance (how slowly the room changes)
        cold_capacity_factor: float = 0.75,
        cold_outdoor_f: float = 60.0,
        warm_outdoor_f: float = 70.0,
    ):
        self.outdoor_temp = outdoor_f
        self.setpoint = setpoint_f
        self.power_kw = power_kw
        self.cop      = cop
        self.ci       = ci
        self.cold_capacity_factor = cold_capacity_factor
        self.cold_outdoor_f = cold_outdoor_f
        self.warm_outdoor_f = warm_outdoor_f
        self.dt       = 1.0 / 60.0         # one-minute step, in hours
        self.temp     = setpoint_f + 1.0   # indoor temperature, °F
        self._on      = False              # AC running?
        self.fan_on   = False

    def _thermostat(self) -> None:
        # ±1 °F band so the AC doesn't flip on/off every minute
        if self.temp > self.setpoint + 1.0:
            self._on = True
        elif self.temp < self.setpoint - 1.0:
            self._on = False

    def _effective_ri(self) -> float:
        # heat leaks in faster (lower resistance) when it is cold outside: 1 at 50 °F → 3 at 70 °F
        fraction = np.clip((self.outdoor_temp - 50.0) / (70.0 - 50.0), 0.0, 1.0)
        return 1.0 + fraction * (3.0 - 1.0)

    def _capacity_factor(self) -> float:
        # AC cools less well in cold weather: 75% at 60 °F → 100% at 70 °F
        fraction = np.clip(
            (self.outdoor_temp - self.cold_outdoor_f) / (self.warm_outdoor_f - self.cold_outdoor_f),
            0.0, 1.0,
        )
        return self.cold_capacity_factor + fraction * (1.0 - self.cold_capacity_factor)

    def _fan_cooling_kw(self) -> float:
        # TODO(team): fan cooling effect (kW of heat removed) from the fan team's equation.
        # 0 for now: with the fan "on" and the AC off, the room just drifts toward the outdoor temp.
        return 0.0

    def _thermal_step(self) -> None:
        # one minute of the RC model: temp moves toward (outdoor − cooling effect)
        alpha = np.exp(-self.dt / (self.ci * self._effective_ri()))
        cooling_kw = 0.0
        if self._on:
            cooling_kw += self.power_kw * self.cop * self._capacity_factor()
        if self.fan_on:
            cooling_kw += self._fan_cooling_kw()
        self.temp = (
            alpha * self.temp
            + (1.0 - alpha) * (self.outdoor_temp - self._effective_ri() * cooling_kw)
        )

    def update(self, outdoor_f: float, fan_installed: bool = False) -> None:
        self.outdoor_temp = float(outdoor_f)
        if fan_installed and self.temp < TMIN:
            self.fan_on, self._on = False, False     # cold enough: everything off
        elif fan_installed and self.outdoor_temp < self.temp:
            self.fan_on, self._on = True, False      # outside is colder: fan instead of AC
        else:
            self.fan_on = False
            self._thermostat()                       # AC follows the setpoint
        self._thermal_step()

    def change_setpoint(self, sp: float) -> None:
        self.setpoint = sp

    @property
    def instant_power_kw(self) -> float:
        # electricity this minute: AC (more the warmer it is outside) + fan
        ac_kw = 0.0
        if self._on:
            ac_kw = max(0.0, COOLER_ELECTRIC_KW
                        + COOLER_ELECTRIC_KW_PER_F * (self.outdoor_temp - COOLER_ELECTRIC_REF_F))
        return ac_kw + (FAN_ELECTRIC_KW if self.fan_on else 0.0)


# ── EV model ──────────────────────────────────────────────────────────────────

class SimEV(EV):
    """The farm truck (KeynesZ's EV class), billing only the energy that actually goes in.

    soc = how full (0–1), target = charge limit (80%), rate = max charger kW.
    charge(kW) adds one minute of charging; idle() applies a tiny standby loss.
    """

    def charge(self, power_kw: float | None = None, dt_hours: float = 1.0 / 60.0) -> None:
        soc_before = self.soc
        super().charge(power_kw, dt_hours)
        # record the wall power from what was actually stored
        if self.charging:
            stored_kwh = (self.soc - soc_before) * self.capacity
            self.input_power_kw = stored_kwh / self.eff / dt_hours


# ── Stationary battery model ──────────────────────────────────────────────────

class Battery:
    # Stationary battery on the Sol-Ark inverter (Pytes V5), not the EV battery
    def __init__(self, soc_init: float):
        self.capacity_ah     = BATT_CAPACITY_AH
        self.nominal_voltage = BATT_NOMINAL_VOLTAGE
        self.max_charge_power    = BATT_CHARGE_CURRENT_MAX * BATT_NOMINAL_VOLTAGE / 1000         # kW
        self.max_discharge_power = BATT_DISCHARGE_CURRENT_NOMINAL * BATT_NOMINAL_VOLTAGE / 1000  # kW
        self.soc_min = BATT_SOC_MIN  # %
        self.soc_max = BATT_SOC_MAX  # %
        self.soc     = soc_init      # %

    # kW for one minute → amp-hours → % SOC
    def charge(self, power_kw: float, dt_hours: float = 1.0 / 60.0) -> None:
        delta_ah = (power_kw * BATT_CHARGE_EFF * 1000 / self.nominal_voltage) * dt_hours
        self.soc += delta_ah / self.capacity_ah * 100

    def discharge(self, power_kw: float, dt_hours: float = 1.0 / 60.0) -> None:
        delta_ah = (power_kw / BATT_DISCHARGE_EFF * 1000 / self.nominal_voltage) * dt_hours
        self.soc -= delta_ah / self.capacity_ah * 100

    def _soc_to_kw(self, soc_delta: float, dt_hours: float) -> float:
        # power that moves the SOC by soc_delta (%) over one step
        return soc_delta / 100 * self.capacity_ah * self.nominal_voltage / 1000 / dt_hours

    def charge_acceptance_kw(self, dt_hours: float = 1.0 / 60.0) -> float:
        """How much charging power the battery can take this step (current limit, room to 100%)."""
        headroom_kw = max(0.0, self._soc_to_kw(self.soc_max - self.soc, dt_hours) / BATT_CHARGE_EFF)
        return min(self.max_charge_power, headroom_kw)

    # Look up this hour's row of the inverter Time of Use table
    @staticmethod
    def tou_slot(hour: int) -> tuple[float, float]:
        """(max discharge kW, Batt %) for the Time of Use slot containing hour."""
        slot = BATT_TOU_SCHEDULE[0]
        for row in BATT_TOU_SCHEDULE:
            if hour >= row[0]:
                slot = row
        return slot[1], slot[2]

    def dispatch(
        self,
        pv_kw: float,
        cooler_kw: float,
        ev_kw: float,
        hour: int,
        dt_hours: float = 1.0 / 60.0,
        other_kw: float = 0.0,
    ) -> tuple[float, float, float]:
        """Load First: PV -> loads (truck included) -> battery -> grid sell; shortfall: battery -> grid.

        The battery charges from PV only and covers the cooler and other loads, never the truck,
        within the Time of Use slot's discharge limit and Batt %.
        Returns battery kW (charge + / discharge -), grid sell kW, grid buy kW.
        """
        max_discharge_kw, slot_soc = self.tou_slot(hour)
        site_kw = cooler_kw + other_kw          # farm loads except the truck
        site_surplus = pv_kw - site_kw          # solar left after those loads (negative = short)
        floor = max(self.soc_min, slot_soc)     # lowest SOC allowed right now (15%, or 100% = no discharge 9–19)
        pv_for_battery = max(0.0, site_surplus - ev_kw)   # battery gets what's left after all loads
        pv_charge = min(pv_for_battery, self.charge_acceptance_kw(dt_hours))   # capped by battery limit
        self.charge(pv_charge, dt_hours)
        surplus_kw = site_surplus - ev_kw - pv_charge   # + = left over → sold, − = short → battery/grid
        grid_sell = max(0.0, surplus_kw)

        # battery only covers the part of the cooler and other loads that PV doesn't
        grid_buy = max(0.0, -surplus_kw)
        discharge_kw = 0.0
        if surplus_kw < 0 and pv_charge == 0.0:
            site_deficit = max(0.0, site_kw - pv_kw)
            available_kw = max(0.0, self._soc_to_kw(self.soc - floor, dt_hours) * BATT_DISCHARGE_EFF)
            discharge_kw = min(site_deficit, max_discharge_kw, self.max_discharge_power, available_kw)
            self.discharge(discharge_kw, dt_hours)
            grid_buy -= discharge_kw

        return pv_charge - discharge_kw, grid_sell, grid_buy


# ── EMS decisions ─────────────────────────────────────────────────────────────
# Cooler rule (Binary and Staged): colder (45 °F) when energy is clean, warmer (50 °F) when not.
# Only matters while the AC is the one cooling (not when the fan rules take over).
def ems_setpoint(pv_kw: float, moer: float | None) -> float:
    pv_producing = pv_kw >= PV_MIN_PRODUCING
    grid_clean = moer is not None and moer < CO2_THRESHOLD
    return SETPOINT_COOLTH if pv_producing or grid_clean else SETPOINT_ECON


# Truck rules. surplus_kw = excess solar (PV left after the cooler, other loads and inverter losses)
def binary_charger_kw(surplus_kw: float) -> float:
    """BINARY_KW while excess solar covers it, otherwise off."""
    return BINARY_KW if surplus_kw >= BINARY_KW else 0.0


def staged_charger_kw(surplus_kw: float) -> float:
    """Highest staged level the excess solar fully covers (rounded down), otherwise off."""
    covered = [level for level in STAGED_KW if level <= surplus_kw]   # levels solar can pay for
    return max(covered) if covered else 0.0                           # e.g. 6 kW excess → 4.8 kW (20 A)


# ── Week inputs ───────────────────────────────────────────────────────────────

def load_solark_pv_range(xlsx_path: Path, timezone: str = "America/Detroit") -> pd.Series:
    """Sol-Ark Operation Data export covering one or more whole days, as one-minute PV kW."""
    frame = pd.read_excel(xlsx_path, sheet_name=0, header=5, engine="openpyxl")
    columns = [c for c in frame.columns if str(c).startswith("Ppv")]
    if len(columns) != 4:
        raise ValueError(f"Expected Ppv1-Ppv4 columns in {xlsx_path}, found {columns}")
    timestamps = pd.to_datetime(frame["Time"], errors="raise").dt.tz_localize(
        timezone, ambiguous="raise", nonexistent="shift_forward"
    )
    pv_kw = frame[columns].apply(pd.to_numeric, errors="raise").sum(axis=1) / 1000.0
    pv = pd.Series(pv_kw.to_numpy(), index=pd.DatetimeIndex(timestamps).floor("min"))
    pv = pv.groupby(level=0).mean().sort_index()

    start = pv.index[0].normalize()
    end = pv.index[-1].normalize() + pd.Timedelta(days=1)
    minutes = pd.date_range(start, end, freq="1min", inclusive="left")
    aligned = pv.reindex(minutes).ffill().bfill()
    if (aligned < 0).any():
        raise ValueError("Sol-Ark PV power must be nonnegative")
    return aligned


def load_moer_csv(csv_path: Path, minutes: pd.DatetimeIndex) -> tuple[pd.Series, pd.Series]:
    """WattTime CSV (point_time in UTC, value in lb/MWh) aligned to the simulation minutes.

    Minutes the CSV doesn't cover are filled with the same time one week earlier.
    Returns (MOER, filled flag).
    """
    raw = pd.read_csv(csv_path)
    times = pd.to_datetime(raw["point_time"], utc=True).dt.tz_convert(minutes.tz)
    moer = pd.Series(raw["value"].astype(float).to_numpy(), index=times).sort_index()
    moer = moer[~moer.index.duplicated(keep="last")]

    # forward-fill 5-minute points only within the data's own span
    full = pd.date_range(moer.index[0], moer.index[-1], freq="1min")
    by_minute = moer.reindex(full).ffill()
    aligned = by_minute.reindex(minutes)
    filled = aligned.isna()
    if filled.any():
        week_before = by_minute.reindex(minutes[filled] - pd.Timedelta(days=7))
        aligned.loc[filled] = week_before.to_numpy()
    if aligned.isna().any():
        raise ValueError(
            f"MOER missing for {int(aligned.isna().sum())} minutes, even after "
            "filling from the week before"
        )
    return aligned, filled


def load_outdoor_range(csv_path: Path, minutes: pd.DatetimeIndex) -> pd.Series:
    """Hourly outdoor temperatures (Open-Meteo CSV) interpolated to the simulation minutes."""
    outdoor = pd.read_csv(csv_path, usecols=["timestamp", "temperature_f"])
    times = pd.to_datetime(outdoor["timestamp"]).dt.tz_localize(
        minutes.tz, ambiguous="infer", nonexistent="shift_forward"
    )
    series = pd.Series(outdoor["temperature_f"].astype(float).to_numpy(), index=times)
    series = series[~series.index.duplicated()].sort_index()
    by_minute = series.resample("1min").interpolate(method="time")
    aligned = by_minute.reindex(minutes, method="nearest", tolerance=pd.Timedelta("1 hour"))
    if aligned.isna().any():
        raise ValueError(
            f"Outdoor temperature missing for {int(aligned.isna().sum())} minutes; "
            f"{csv_path} must cover {minutes[0].date()} to {minutes[-1].date()}"
        )
    return aligned


def ev_trip_fraction(minutes: pd.DatetimeIndex) -> pd.Series:
    """Fraction of each delivery trip elapsed (0 → 1) while away; NaN while plugged in."""
    fraction = pd.Series(np.nan, index=minutes)
    trip_minutes = (EV_TRIP_END_HOUR - EV_TRIP_START_HOUR) * 60   # 7 h = 420 min
    for day in pd.unique(minutes.normalize()):
        if day.weekday() not in EV_TRIP_WEEKDAYS:   # only Tuesdays and Thursdays
            continue
        start = day + pd.Timedelta(hours=EV_TRIP_START_HOUR)
        away = (minutes >= start) & (minutes < day + pd.Timedelta(hours=EV_TRIP_END_HOUR))
        elapsed = (minutes[away] - start).total_seconds() / 60.0 + 1
        fraction[away] = elapsed / trip_minutes
    return fraction


def week_inputs(pv_xlsx: Path, moer_csv: Path, outdoor_csv: Path) -> pd.DataFrame:
    """The week's inputs, one row per minute: real solar, grid CO2, outdoor temp, deliveries."""
    pv = load_solark_pv_range(pv_xlsx)
    minutes = pv.index
    moer, moer_filled = load_moer_csv(moer_csv, minutes)
    return pd.DataFrame(
        {
            "pv_kw": pv.to_numpy(),
            "moer_lb_per_mwh": moer.to_numpy(),
            "moer_filled": moer_filled.to_numpy(),
            "outdoor_f": load_outdoor_range(outdoor_csv, minutes).to_numpy(),
            "ev_trip_fraction": ev_trip_fraction(minutes).to_numpy(),
        },
        index=minutes,
    )


# ── Simulation ────────────────────────────────────────────────────────────────

def run_minutes(inputs: pd.DataFrame, controller: str, battery_soc_init: float) -> pd.DataFrame:
    """Run cooler, truck and battery through every minute of the inputs under one mode."""
    if controller not in CONTROLLERS:
        raise ValueError(f"controller must be one of {CONTROLLERS}")

    # ── set up the three devices ──
    cooler = Cooler(outdoor_f=float(inputs["outdoor_f"].iloc[0]))
    ev = SimEV(soc_init=EV_SOC_TARGET, soc_target=EV_SOC_TARGET, charge_rate_kw=EV_CHARGER_KW,
               capacity_kwh=EV_CAPACITY, charge_eff=EV_CHARGE_EFF)
    battery = Battery(soc_init=battery_soc_init)
    fan_installed = FAN_INSTALLED and controller != "without-ems"   # No EMS = the farm today, no fan
    departure_soc = None                # truck SOC when the current delivery left
    wants_charge = ev.soc < ev.target   # truck below its limit?
    ems_ev_kw = 0.0                     # last EMS charging decision, held between decisions
    records: list[dict] = []            # one row of results per minute

    # ── one loop pass = one minute ──
    for step, (timestamp, row) in enumerate(inputs.iterrows()):
        # 1. this minute's inputs
        pv_kw = float(row["pv_kw"])
        pv_ac_kw = pv_kw * (1.0 - INVERTER_PV_LOSS)   # after conversion loss
        other_kw = OTHER_LOAD_KW                       # wifi/electronics, always on
        # inverter's own power use (higher in the day)
        standby_kw = INVERTER_STANDBY_DAY_KW if pv_kw > PV_STANDBY_THRESHOLD_KW else INVERTER_STANDBY_KW
        moer = float(row["moer_lb_per_mwh"])

        # 2. cooler: pick the setpoint, then run one minute of the fan/thermostat and temperature
        if controller == "without-ems":
            sp = SETPOINT_NO_EMS               # fixed 38 °F, like the CoolBot today
        else:
            sp = ems_setpoint(pv_kw, moer)     # 45 °F clean / 50 °F dirty
        cooler.change_setpoint(sp)
        cooler.update(float(row["outdoor_f"]), fan_installed)

        # 3. truck
        trip = row["ev_trip_fraction"]
        plugged = pd.isna(trip)
        if not plugged:
            # away on a delivery: drain EV_TRIP_SOC_USED evenly over the trip
            if departure_soc is None:
                departure_soc = ev.soc
            ev.soc = departure_soc - EV_TRIP_SOC_USED * float(trip)
            ev.charging = False
            ev.input_power_kw = 0.0
        else:
            departure_soc = None
            # does the truck need charge? starts below 79%, stops at 80%
            if ev.soc <= ev.target - EV_RESUME_MARGIN:
                wants_charge = True
            elif ev.soc >= ev.target - 1e-9:
                wants_charge = False

            if controller == "without-ems":
                ev_kw = ev.rate                    # No EMS: full power, any time
            else:
                if step % DECISION_MINUTES == 0:   # EMS decides every 5 minutes
                    # excess solar = PV − cooler − other loads − inverter use
                    surplus_kw = pv_ac_kw - cooler.instant_power_kw - other_kw - standby_kw
                    if BATTERY_BEFORE_TRUCK:
                        surplus_kw -= battery.charge_acceptance_kw()   # leave the battery its share
                    ems_ev_kw = (binary_charger_kw(surplus_kw) if controller == "with-ems"
                                 else staged_charger_kw(surplus_kw))
                ev_kw = ems_ev_kw                  # between decisions, keep the last one
            ev_kw = min(ev_kw, ev.rate) if wants_charge else 0.0
            ev.charge(ev_kw) if ev_kw > 0.0 else ev.idle()   # one minute of charging (or standing)

        # 4. battery and grid: share out this minute's solar (the battery never powers the truck)
        batt_power, grid_sell, grid_buy = battery.dispatch(
            pv_ac_kw, cooler.instant_power_kw, ev.input_power_kw, timestamp.hour,
            other_kw=other_kw + standby_kw,
        )

        # 5. save this minute
        records.append(
            {
                "timestamp": timestamp,
                "pv_kw": pv_kw,
                "moer_lb_per_mwh": moer,
                "outdoor_f": cooler.outdoor_temp,
                "setpoint_f": sp,
                "cooler_temp_f": cooler.temp,
                "fan_on": cooler.fan_on,
                "cooler_kw": cooler.instant_power_kw,
                "other_load_kw": other_kw,
                "inverter_loss_kw": standby_kw + pv_kw - pv_ac_kw,
                "ev_kw": ev.input_power_kw,
                "ev_plugged": plugged,
                "ev_soc_percent": ev.soc * 100.0,
                "battery_kw": batt_power,
                "battery_soc_percent": battery.soc,
                "grid_sell_kw": grid_sell,
                "grid_buy_kw": grid_buy,
            }
        )

    return pd.DataFrame.from_records(records).set_index("timestamp")


def run_week(inputs: pd.DataFrame, controller: str) -> pd.DataFrame:
    """Run the week continuously.

    The battery's starting SOC is taken from the end of a first pass, so the week
    starts the way it ends (no assumption about Sunday midnight).
    """
    # pass 1 only finds where the battery ends up Saturday night; pass 2 is the real result
    first = run_minutes(inputs, controller, battery_soc_init=BATT_SOC_MIN)
    start_soc = float(first["battery_soc_percent"].iloc[-1])
    return run_minutes(inputs, controller, battery_soc_init=start_soc)


# ── Results ───────────────────────────────────────────────────────────────────

def summarize_week(results: pd.DataFrame, controller: str) -> dict:
    """Week totals for one mode: the numbers in the terminal table and summary.csv."""
    kwh = lambda column: float(results[column].sum() / 60.0)   # kW each minute → kWh
    # SOC as each delivery leaves (the minute before it unplugs)
    plugged = results["ev_plugged"].astype(bool).to_numpy()
    departures = results["ev_soc_percent"].iloc[np.flatnonzero(plugged[:-1] & ~plugged[1:])]
    charging = (results["ev_kw"] > 0).to_numpy()   # for charger starts (off → on only, not level changes)
    load_kwh = kwh("cooler_kw") + kwh("other_load_kw") + kwh("ev_kw")
    return {
        "controller": controller,
        "grid_buy_kwh": kwh("grid_buy_kw"),
        "grid_sell_kwh": kwh("grid_sell_kw"),
        # CO2 = grid bought each minute × that minute's grid CO2 rate; selling earns no credit
        "grid_emissions_lb": float(
            (results["grid_buy_kw"] * results["moer_lb_per_mwh"]).sum() / 60.0 / 1000.0
        ),
        # share of farm load NOT bought from the grid
        "self_sufficiency_percent": 100.0 * (1 - kwh("grid_buy_kw") / load_kwh),
        "cooler_kwh": kwh("cooler_kw"),
        "ev_kwh": kwh("ev_kw"),
        "ev_charger_starts": int((charging[1:] & ~charging[:-1]).sum() + charging[0]),
        "ev_soc_at_departures": " / ".join(f"{t:%a} {soc:.1f}%" for t, soc in departures.items()),
        "ev_lowest_departure_soc_percent": float(departures.min()),
        "ev_final_soc_percent": float(results["ev_soc_percent"].iloc[-1]),
        "cooler_unsafe_minutes": int(
            ((results["cooler_temp_f"] < TMIN) | (results["cooler_temp_f"] > TMAX)).sum()
        ),
    }


def print_week(label: str, summary: pd.DataFrame, filled_hours: float) -> None:
    """Results table in the terminal."""
    print(
        f"{label}: truck limit {EV_SOC_TARGET:.0%}, deliveries Tue/Thu using {EV_TRIP_SOC_USED:.0%} each, "
        f"Binary {BINARY_AMPS} A, Staged {'/'.join(map(str, STAGED_AMPS))} A"
        + (", battery before truck" if BATTERY_BEFORE_TRUCK else "")
    )
    if filled_hours:
        print(f"  note: {filled_hours:.1f} h of MOER filled from the week before")
    header = (
        f"{'Mode':<12}{'Grid buy':>10}{'Grid sell':>11}{'CO2 lb':>8}{'Self-suff.':>12}"
        f"{'EV kWh':>9}{'EV starts':>11}{'Lowest SOC leaving':>20}{'Unsafe min':>12}"
    )
    print(header)
    print("-" * len(header))
    for row in summary.itertuples():
        print(
            f"{CONTROLLER_LABELS[row.controller]:<12}{row.grid_buy_kwh:>10.1f}{row.grid_sell_kwh:>11.1f}"
            f"{row.grid_emissions_lb:>8.1f}{row.self_sufficiency_percent:>11.0f}%{row.ev_kwh:>9.1f}"
            f"{row.ev_charger_starts:>11}{row.ev_lowest_departure_soc_percent:>19.1f}%"
            f"{row.cooler_unsafe_minutes:>12}"
        )


def plot_mode(label: str, controller: str, results: pd.DataFrame, output_path: Path):
    """One window per mode: truck SOC (top) and grid buy (bottom) over the week."""
    fig, (soc_ax, grid_ax) = plt.subplots(2, 1, figsize=(14, 7), sharex=True)
    fig.suptitle(f"{CONTROLLER_LABELS[controller]} — {label}")

    # shade deliveries on both charts
    away = ~results["ev_plugged"].astype(bool)
    runs = (away != away.shift()).cumsum()[away]
    for ax in (soc_ax, grid_ax):
        for i, (_, run) in enumerate(runs.groupby(runs)):
            ax.axvspan(run.index[0], run.index[-1], color="purple", alpha=0.12,
                       label="Delivery (unplugged)" if i == 0 else None)
        ax.grid(True, alpha=0.3)

    soc_ax.plot(results.index, results["ev_soc_percent"], color="green", label="Truck SOC")
    soc_ax.axhline(EV_SOC_TARGET * 100, color="gray", linestyle="--", linewidth=0.8,
                   label=f"Limit {EV_SOC_TARGET:.0%}")
    soc_ax.set_ylabel("Truck SOC (%)")
    soc_ax.legend(loc="lower left", fontsize=8)

    grid_ax.plot(results.index, results["grid_buy_kw"], color="dimgray", linewidth=0.8, label="Grid buy")
    grid_ax.set_ylabel("Grid buy (kW)")
    grid_ax.legend(loc="upper left", fontsize=8)

    days = pd.date_range(results.index[0].normalize(), results.index[-1].normalize(), freq="D")
    grid_ax.set_xticks(days + pd.Timedelta(hours=12), [d.strftime("%a %m/%d") for d in days])
    fig.tight_layout()
    fig.savefig(output_path, dpi=120)
    return fig


def run_week_scenario(pv_xlsx: Path, moer_csv: Path, outdoor_csv: Path,
                      output_dir: Path, show_plot: bool = False) -> pd.DataFrame:
    """Run all three modes on one week; save CSVs and charts to output_dir/week-<date>/."""
    inputs = week_inputs(pv_xlsx, moer_csv, outdoor_csv)
    label = f"week-{inputs.index[0].date().isoformat()}" + ("_battery-first" if BATTERY_BEFORE_TRUCK else "")
    week_dir = output_dir / label
    week_dir.mkdir(parents=True, exist_ok=True)

    summaries = []
    for controller in CONTROLLERS:
        results = run_week(inputs, controller)
        summaries.append(summarize_week(results, controller))
        results.to_csv(week_dir / f"{controller}_minutes.csv")
        fig = plot_mode(label, controller, results, week_dir / f"{controller}.png")
        if not show_plot:
            plt.close(fig)

    summary = pd.DataFrame(summaries)
    summary.to_csv(week_dir / "summary.csv", index=False)
    print_week(label, summary, inputs["moer_filled"].sum() / 60.0)
    print(f"Outputs: {week_dir.resolve()}\n")
    if show_plot:
        plt.show()   # one window per mode
    return summary


# ── Command line ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Campus Farm EMS week simulation (No EMS / Binary / Staged)")
    parser.add_argument("--week-pv-xlsx", type=Path, default=DEFAULT_WEEK_PV,
                        help="Sol-Ark Operation Data export covering a whole week, Sun–Sat (default: %(default)s)")
    parser.add_argument("--moer-csv", type=Path, default=DEFAULT_MOER_CSV,
                        help="WattTime CSV with point_time (UTC) and value columns (default: %(default)s)")
    parser.add_argument("--outdoor-csv", type=Path, default=DEFAULT_OUTDOOR_CSV,
                        help="Hourly outdoor temperatures (default: %(default)s)")
    parser.add_argument("--battery-first", action="store_true",
                        help="Binary/Staged: excess solar fills the battery before the truck")
    parser.add_argument("--output-dir", type=Path, default=Path("results/integrated_simulation"))
    parser.add_argument("--show-plot", action="store_true")
    args = parser.parse_args()
    BATTERY_BEFORE_TRUCK = args.battery_first

    run_week_scenario(args.week_pv_xlsx, args.moer_csv, args.outdoor_csv,
                      args.output_dir, show_plot=args.show_plot)
