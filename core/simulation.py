"""
Campus Farm EMS — one-day simulation.

Runs 1 440 minutes (one full day) through a physics-based model:
  - PV     : Sol-Ark export (with WattTime MOER) if given, else CSV, else sine wave
  - Cooler : first-order RC thermal model with bang-bang thermostat
  - EV     : SoC integrator from ev_real_data_simulation
  - Battery: stationary Sol-Ark battery (Pytes V5), dispatched on PV surplus/deficit
  - Grid   : WattTime MOER with a Sol-Ark export, otherwise a synthetic duck curve

EMS decision each minute:
  - "Clean" if PV >= PV_MIN_PRODUCING kW  OR  synthetic MOER < CO2_THRESHOLD
  - Clean  → CoolBot setpoint = SETPOINT_COOLTH (35 °F), charge EV
  - Dirty  → CoolBot setpoint = SETPOINT_ECON   (48 °F), don't charge EV
  - Safety: TMIN/TMAX overrides applied before the normal clean/dirty choice

Usage:
    python core/simulation.py                          # sine-wave PV
    python core/simulation.py --csv PVdata.csv         # real PV data
    python core/simulation.py --pv-xlsx solark.xlsx    # Sol-Ark PV + WattTime MOER
"""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ev_real_data_simulation import (
    CHARGER_POWER_LEVELS_KW,
    EV,
    SAMPLE_DAY_LABELS,
    SCENARIO_EXPECTED_DATES,
    get_watttime_token,
    load_solark_pv,
    real_inputs,
)

# ── Constants ─────────────────────────────────────────────────────────────────
SETPOINT_COOLTH  = 35     # °F — low setpoint (clean energy available)
SETPOINT_ECON    = 48     # °F — high setpoint (dirty / no renewable energy)
EV_CAPACITY      = 131.0  # kWh  (F-150 Lightning extended range)
EV_CHARGE_RATE   = 11.5   # kW
EV_CHARGE_EFF    = 0.90
EV_MAX_MILES     = 240    # miles (used for display only)
EV_SOC_INIT      = 0.20   # starting state of charge
EV_SOC_TARGET    = 0.95

PV_MAX_POWER     = 13.2   # kW
PV_MIN_PRODUCING = 0.5    # kW — threshold to count PV as "producing"
CO2_THRESHOLD    = 1400   # lbs CO2/MWh — synthetic grid cleanliness threshold

AMBIENT_TEMP     = 70.0   # °F — outside air temperature assumed constant
TMIN             = 34.0   # °F — safety minimum (freeze prevention)
TMAX             = 55.0   # °F — safety maximum (spoilage prevention)

# Pytes V5: 100Ah, 51.2V, 50A charge, 100A discharge (180A peak, unused)
# SOC limits match the current Sol-Ark inverter settings; soc_init is a placeholder for the start of the day
# TODO(team): seed soc_init from the previous day's ending SOC (night->day->night convergence)
BATT_CAPACITY_AH               = 100
BATT_NOMINAL_VOLTAGE           = 51.2   # V
BATT_CHARGE_CURRENT_MAX        = 50     # A
BATT_DISCHARGE_CURRENT_NOMINAL = 100    # A
BATT_DISCHARGE_CURRENT_PEAK    = 180    # A
BATT_SOC_MIN                   = 15     # %
BATT_SOC_MAX                   = 100    # %
BATT_SOC_INIT                  = 100    # %

# Sol-Ark Limiter Param > Time of Use, as set on the inverter.
# Each slot runs until the next start: (start hour, max discharge kW, Batt % floor/target, grid Charge)
# Batt is the SOC the battery won't discharge below in that slot; with Charge checked the
# inverter also charges from the grid up to it. Sell is unchecked in every slot.
BATT_TOU_SCHEDULE = (
    (0,  2.0, 15,  True),
    (7,  2.0, 15,  True),
    (9,  2.0, 100, True),
    (13, 2.0, 100, True),
    (17, 2.0, 100, True),
    (19, 2.0, 15,  True),
)
# TODO(team): grid charge rate is set in the Sol-Ark battery settings, not this screen;
# the charge current limit (50 A) is assumed

# Week-long runs: the EV is plugged in all week except the Wednesday dining hall trip.
# It starts the week full and comes back from the trip at EV_TRIP_RETURN_SOC.
EV_WEEK_SOC_INIT   = EV_SOC_TARGET
EV_TRIP_WEEKDAY    = 2      # Wednesday (Monday = 0)
EV_TRIP_START_HOUR = 9      # unplugged 9:00 AM
EV_TRIP_END_HOUR   = 19     # plugged back in 7:00 PM
EV_TRIP_RETURN_SOC = 0.20


# ── Synthetic grid CO2 signal ─────────────────────────────────────────────────

def synthetic_moer(minute: int) -> float:
    hour = (minute // 60) % 24
    peak = 800.0 * np.sin(np.pi * max(0.0, hour - 7) / 14.0) ** 2
    return 800.0 + peak


# ── PV model ──────────────────────────────────────────────────────────────────

class PV:
    def __init__(
        self,
        inv_eff: float = 0.96,
        max_power: float = PV_MAX_POWER,
        csv_path: str | None = None,
    ):
        self.inv_eff   = inv_eff
        self.max_power = max_power
        self.power_kw  = 0.0
        self._data: pd.DataFrame | None = None

        if csv_path and Path(csv_path).exists():
            self._data = pd.read_csv(csv_path, usecols=["Minute", "Power"])
            print(f"[PV] Using CSV data from {csv_path}")
        else:
            print("[PV] Using sine-wave approximation (07:00–18:30 daylight window)")

    def update(self, minute: int) -> float:
        if self._data is not None:
            idx = min(minute, len(self._data) - 1)
            self.power_kw = float(self._data.at[idx, "Power"])
        else:
            daylight_start = 7 * 60
            daylight_end   = 18 * 60 + 30
            duration       = daylight_end - daylight_start
            t = minute - daylight_start
            if 0 <= t <= duration:
                self.power_kw = (self.inv_eff * self.max_power / 2.0) * (
                    np.sin(np.pi * t / duration) + 1.0
                )
            else:
                self.power_kw = 0.0
        return self.power_kw


# ── Cooler model ──────────────────────────────────────────────────────────────


class Cooler:
    def __init__(
        self,
        ambient_f: float  = AMBIENT_TEMP,
        setpoint_f: float = SETPOINT_ECON,
        power_kw: float   = 3.67,
        cop: float        = 2.0,
        ri: float         = 10.0,
        ci: float         = 0.2,
    ):
        self.ambient  = ambient_f
        self.setpoint = setpoint_f
        self.power_kw = power_kw
        self.cop      = cop
        self.ri       = ri
        self.ci       = ci
        self.dt       = 1.0 / 60.0
        self.temp     = setpoint_f + 2.0
        self._on      = False

    @property
    def _band_high(self) -> float: return self.setpoint + 2.0
    @property
    def _band_low(self)  -> float: return self.setpoint - 2.0

    def _thermostat(self) -> None:
        if self.temp > self._band_high:
            self._on = True
        elif self.temp < self._band_low:
            self._on = False

    def _thermal_step(self) -> None:
        alpha    = np.exp(-self.dt / (self.ci * self.ri))
        q_remove = self.ri * self.power_kw * self.cop if self._on else 0.0
        self.temp = alpha * self.temp + (1.0 - alpha) * (self.ambient - q_remove)

    def update(self, outdoor_f: float | None = None) -> None:
        if outdoor_f is not None:
            self.ambient = float(outdoor_f)

        self._thermostat()
        self._thermal_step()

    def change_setpoint(self, sp: float) -> None:
        self.setpoint = sp

    @property
    def instant_power_kw(self) -> float:
        return self.power_kw if self._on else 0.0


# ── EV model ──────────────────────────────────────────────────────────────────

class SimEV(EV):
    """ev_real_data_simulation's EV, billing only the energy that actually goes in.

    EV.charge reports the full commanded power even on a minute where the SoC is clamped
    at the target (e.g. topping up after a sliver of standby loss), which overstates
    charger and grid energy.
    """

    def charge(self, power_kw: float | None = None, dt_hours: float = 1.0 / 60.0) -> None:
        soc_before = self.soc
        super().charge(power_kw, dt_hours)
        if self.charging:
            stored_kwh = (self.soc - soc_before) * self.capacity
            self.input_power_kw = stored_kwh / self.eff / dt_hours


# ── Stationary battery model ──────────────────────────────────────────────────

class Battery:
    # Stationary battery on the Sol-Ark inverter (Pytes V5), not the EV battery
    def __init__(
        self,
        capacity_ah: float               = BATT_CAPACITY_AH,
        nominal_voltage: float           = BATT_NOMINAL_VOLTAGE,
        charge_current_max: float        = BATT_CHARGE_CURRENT_MAX,
        discharge_current_nominal: float = BATT_DISCHARGE_CURRENT_NOMINAL,
        discharge_current_peak: float    = BATT_DISCHARGE_CURRENT_PEAK,
        soc_min: float                   = BATT_SOC_MIN,
        soc_max: float                   = BATT_SOC_MAX,
        soc_init: float                  = BATT_SOC_INIT,
    ):
        self.capacity_ah     = capacity_ah
        self.nominal_voltage = nominal_voltage
        self.max_charge_power     = charge_current_max * nominal_voltage / 1000         # kW
        self.max_discharge_power  = discharge_current_nominal * nominal_voltage / 1000  # kW
        self.peak_discharge_power = discharge_current_peak * nominal_voltage / 1000     # kW, not modeled yet
        self.soc_min = soc_min  # %
        self.soc_max = soc_max  # %
        self.soc     = soc_init  # %

    # TODO(team): no round-trip efficiency yet (100% assumed)
    def charge(self, power_kw: float, dt_hours: float = 1.0 / 60.0) -> None:
        delta_ah = (power_kw * 1000 / self.nominal_voltage) * dt_hours
        self.soc += delta_ah / self.capacity_ah * 100

    def discharge(self, power_kw: float, dt_hours: float = 1.0 / 60.0) -> None:
        delta_ah = (power_kw * 1000 / self.nominal_voltage) * dt_hours
        self.soc -= delta_ah / self.capacity_ah * 100

    def _soc_to_kw(self, soc_delta: float, dt_hours: float) -> float:
        # power that moves the SOC by soc_delta (%) over one step
        return soc_delta / 100 * self.capacity_ah * self.nominal_voltage / 1000 / dt_hours

    @staticmethod
    def tou_slot(hour: int) -> tuple[float, float, bool]:
        """(max discharge kW, Batt %, grid Charge) for the Time of Use slot containing hour."""
        slot = BATT_TOU_SCHEDULE[0]
        for row in BATT_TOU_SCHEDULE:
            if hour >= row[0]:
                slot = row
        return slot[1], slot[2], slot[3]

    def dispatch(
        self,
        pv_kw: float,
        cooler_kw: float,
        ev_kw: float,
        hour: int,
        dt_hours: float = 1.0 / 60.0,
    ) -> tuple[float, float, float, float]:
        """Sol-Ark order: PV -> loads -> battery -> grid sell; deficit: battery -> grid.

        PV serves the cooler before the EV. The battery only ever covers the cooler
        (never the EV), and only within the Time of Use slot's discharge limit and Batt %.
        SOC stays within [soc_min, soc_max].
        Returns battery kW (charge + / discharge -), grid sell kW, grid buy kW,
        and the part of the battery charge that came from the grid (kW).
        """
        max_discharge_kw, slot_soc, grid_charge = self.tou_slot(hour)
        surplus_kw = pv_kw - cooler_kw - ev_kw

        pv_charge = 0.0
        grid_sell = max(0.0, surplus_kw)
        if surplus_kw > 0:
            headroom_kw = max(0.0, self._soc_to_kw(self.soc_max - self.soc, dt_hours))
            pv_charge = min(surplus_kw, self.max_charge_power, headroom_kw)
            grid_sell = surplus_kw - pv_charge

        # Charge checked: grid tops the battery up to the slot's Batt %
        grid_charge_kw = 0.0
        if grid_charge:
            target_kw = max(0.0, self._soc_to_kw(slot_soc - self.soc, dt_hours) - pv_charge)
            grid_charge_kw = min(self.max_charge_power - pv_charge, target_kw)
        self.charge(pv_charge + grid_charge_kw, dt_hours)

        # battery only covers the part of the cooler that PV doesn't
        grid_buy = max(0.0, -surplus_kw) + grid_charge_kw
        discharge_kw = 0.0
        if surplus_kw < 0 and grid_charge_kw == 0.0:
            cooler_deficit = max(0.0, cooler_kw - pv_kw)
            floor = max(self.soc_min, slot_soc)
            available_kw = max(0.0, self._soc_to_kw(self.soc - floor, dt_hours))
            discharge_kw = min(cooler_deficit, max_discharge_kw, self.max_discharge_power, available_kw)
            self.discharge(discharge_kw, dt_hours)
            grid_buy -= discharge_kw

        batt_power = pv_charge + grid_charge_kw - discharge_kw
        return batt_power, grid_sell, grid_buy, grid_charge_kw


# ── EMS decision ──────────────────────────────────────────────────────────────

def ems_setpoint(pv_kw: float, moer: float, cooler_temp: float) -> int:
    if cooler_temp < TMIN:
        return SETPOINT_ECON
    if cooler_temp > TMAX:
        return SETPOINT_COOLTH
    energy_clean = pv_kw >= PV_MIN_PRODUCING or moer < CO2_THRESHOLD
    return SETPOINT_COOLTH if energy_clean else SETPOINT_ECON

def _load_outdoor_temperatures(
    outdoor_csv_path: str | Path,
    simulation_date: str | pd.Timestamp | None = None,
) -> pd.Series:
    """
    Read hourly outdoor temperatures and interpolate them to one-minute values.

    The returned Series:
      - has timestamps as its index;
      - has outdoor temperatures in °F as its values;
      - uses the America/Detroit time zone.
    """
    outdoor = pd.read_csv(
        outdoor_csv_path,
        usecols=["timestamp", "temperature_f"],
    )

    outdoor["temperature_f"] = pd.to_numeric(
        outdoor["temperature_f"],
        errors="coerce",
    )

    outdoor["timestamp"] = pd.to_datetime(
        outdoor["timestamp"],
        errors="coerce",
    )

    if outdoor["timestamp"].dt.tz is None:
        outdoor["timestamp"] = outdoor["timestamp"].dt.tz_localize(
            "America/Detroit",
            ambiguous="infer",
            nonexistent="shift_forward",
        )
    else:
        outdoor["timestamp"] = outdoor["timestamp"].dt.tz_convert(
            "America/Detroit"
        )

    outdoor = (
        outdoor
        .dropna(subset=["timestamp", "temperature_f"])
        .sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="last")
    )

    if outdoor.empty:
        raise ValueError(
            f"No valid outdoor-temperature data found in {outdoor_csv_path}"
        )

    outdoor_by_minute = (
        outdoor
        .set_index("timestamp")["temperature_f"]
        .resample("1min")
        .interpolate(method="time")
    )

    if simulation_date is None:
        start = outdoor_by_minute.index.min().normalize()
    else:
        start = pd.Timestamp(simulation_date)

        if start.tzinfo is None:
            start = start.tz_localize("America/Detroit")
        else:
            start = start.tz_convert("America/Detroit")

        start = start.normalize()

    end = start + pd.DateOffset(days=1)

    outdoor_day = outdoor_by_minute.loc[
        (outdoor_by_minute.index >= start)
        & (outdoor_by_minute.index < end)
    ]

    if outdoor_day.empty:
        raise ValueError(
            f"No outdoor-temperature data found for {start.date()}"
        )

    return outdoor_day


# ── Scenario definitions ──────────────────────────────────────────────────────

# No-EMS cooler: the real CoolBot holds a fixed 38 °F setpoint (core/Data/room_temperature_30d.csv)
SETPOINT_NO_EMS = 38

CONTROLLERS = ("without-ems", "with-ems", "staged-ems")
CONTROLLER_LABELS = {
    "without-ems": "No EMS",
    "with-ems": "Binary EMS",
    "staged-ems": "Staged EMS",
}
CONTROLLER_COLORS = {
    "without-ems": "dimgray",
    "with-ems": "steelblue",
    "staged-ems": "darkorange",
}

# Same sample days and scenario layout as ev_real_data_simulation.py
DAY_KEYS = {"High PV": "high-pv", "Typical Day": "typical-day", "Low PV": "low-pv"}
SCENARIOS = {
    "scenario-1-baseline": (("Typical Day",), ("without-ems",)),
    "scenario-2-binary": (SAMPLE_DAY_LABELS, ("without-ems", "with-ems")),
    "scenario-3-staged": (("Typical Day",), CONTROLLERS),
}


def staged_charger_kw(surplus_kw: float, moer_condition: bool) -> float:
    """Highest charger step the PV surplus fully covers (rounded down).

    Below the lowest step, trickle at 25% only when the grid is clean.
    """
    steps = [level for level in CHARGER_POWER_LEVELS_KW if level > 0.0]
    covered = [level for level in steps if level <= surplus_kw]
    if covered:
        return max(covered)
    return steps[0] if moer_condition else 0.0


# ── Simulation ────────────────────────────────────────────────────────────────

def day_inputs(
    outdoor_csv_path: str | Path = "core/Data/outdoor_temperatures.csv",
    simulation_date: str | pd.Timestamp | None = None,
    pv_csv_path: str | None = None,
    pv_xlsx_path: str | Path | None = None,
    moer_source: str = "historical",
    watttime_region: str = "MISO_DETROIT",
    watttime_cache_dir: str | Path = "results/ev_simulation/cache",
) -> pd.DataFrame:
    """One row per minute: pv_kw, moer_lb_per_mwh, outdoor_f.

    With a Sol-Ark workbook, PV and WattTime MOER are real and the day follows the
    workbook; otherwise PV comes from the CSV or sine wave and MOER is synthetic.
    """
    real_data = None
    if pv_xlsx_path is not None:
        real_data = _load_real_inputs(
            Path(pv_xlsx_path),
            moer_source,
            watttime_region,
            Path(watttime_cache_dir),
        )
        simulation_date = real_data.index[0]

    outdoor_day = _load_outdoor_temperatures(
        outdoor_csv_path,
        simulation_date=simulation_date,
    )
    simulation_start = outdoor_day.index.min().normalize()
    simulation_times = pd.date_range(
        start=simulation_start,
        periods=1440,
        freq="1min",
    )
    outdoor_temps = outdoor_day.reindex(
        simulation_times,
        method="nearest",
        tolerance=pd.Timedelta("1 hour"),
    )
    if outdoor_temps.isna().any():
        missing = int(outdoor_temps.isna().sum())
        raise ValueError(
            f"Missing outdoor temperature for {missing} simulation minutes"
        )

    if real_data is not None:
        pv_kw = real_data["pv_kw"].to_numpy()[:1440]
        moer = real_data["moer_lb_per_mwh"].to_numpy()[:1440]
    else:
        pv = PV(csv_path=pv_csv_path)
        minutes = [t.hour * 60 + t.minute for t in simulation_times]
        pv_kw = [pv.update(minute) for minute in minutes]
        moer = [synthetic_moer(minute) for minute in minutes]

    return pd.DataFrame(
        {
            "pv_kw": pv_kw,
            "moer_lb_per_mwh": moer,
            "outdoor_f": outdoor_temps.to_numpy(),
        },
        index=simulation_times,
    )


def run_day(
    inputs: pd.DataFrame,
    controller: str = "with-ems",
    ev_soc_init: float = EV_SOC_INIT,
    battery_soc_init: float = BATT_SOC_INIT,
) -> pd.DataFrame:
    """Run cooler, EV and battery through the inputs (one day or longer) under one controller.

    without-ems: cooler at the fixed CoolBot setpoint, EV charges whenever below target
    with-ems   : 35/48 °F clean/dirty setpoint, EV at full power only when clean
    staged-ems : same cooler rule, EV at the charger step covered by PV after the cooler
    The battery follows the Sol-Ark Time of Use schedule and never powers the EV.

    If inputs has an ev_trip_fraction column, the EV is unplugged wherever it is set
    (0 → 1 over the trip) and drains linearly to EV_TRIP_RETURN_SOC by the time it returns.
    """
    if controller not in CONTROLLERS:
        raise ValueError(f"controller must be one of {CONTROLLERS}")

    cooler = Cooler()
    ev = SimEV(soc_init=ev_soc_init)
    battery = Battery(soc_init=battery_soc_init)
    cooler.ambient = float(inputs["outdoor_f"].iloc[0])
    trip_fraction = (
        inputs["ev_trip_fraction"] if "ev_trip_fraction" in inputs
        else pd.Series(np.nan, index=inputs.index)
    )
    departure_soc = None
    records: list[dict] = []

    for timestamp, row in inputs.iterrows():
        pv_kw = float(row["pv_kw"])
        moer = float(row["moer_lb_per_mwh"])
        moer_condition = moer < CO2_THRESHOLD
        energy_clean = pv_kw >= PV_MIN_PRODUCING or moer_condition

        if controller == "without-ems":
            sp = SETPOINT_NO_EMS
        else:
            sp = ems_setpoint(pv_kw, moer, cooler.temp)
        cooler.change_setpoint(sp)
        cooler.update(outdoor_f=float(row["outdoor_f"]))

        # TODO(team): the old simulator counted battery discharge as a supply when PV is low
        # (COMBO instead of GRID_SUPPORT). energy_clean only looks at PV and MOER; decide
        # whether battery energy should count as clean for the cooler setpoint and EV charging
        trip = trip_fraction.loc[timestamp]
        plugged = pd.isna(trip)
        if not plugged:
            # away from the charger: drain linearly toward the return SOC
            if departure_soc is None:
                departure_soc = ev.soc
            ev.soc = departure_soc - (departure_soc - EV_TRIP_RETURN_SOC) * float(trip)
            ev.charging = False
            ev.input_power_kw = 0.0
        else:
            departure_soc = None
            if controller == "without-ems":
                ev_kw = ev.rate
            elif controller == "with-ems":
                ev_kw = ev.rate if energy_clean else 0.0
            else:
                ev_kw = staged_charger_kw(pv_kw - cooler.instant_power_kw, moer_condition)
            ev.charge(ev_kw) if ev_kw > 0.0 else ev.idle()

        # battery dispatch on this minute's loads; the battery never powers the EV
        # TODO(team): confirm the cooler is on the inverter LOAD port ("Limited power to Load")
        batt_power, grid_sell, grid_buy, batt_grid_charge = battery.dispatch(
            pv_kw, cooler.instant_power_kw, ev.input_power_kw, timestamp.hour
        )

        records.append(
            {
                "timestamp": timestamp,
                "pv_kw": pv_kw,
                "moer_lb_per_mwh": moer,
                "outdoor_f": float(row["outdoor_f"]),
                "setpoint_f": sp,
                "cooler_temp_f": cooler.temp,
                "cooler_kw": cooler.instant_power_kw,
                "ev_kw": ev.input_power_kw,
                "ev_plugged": plugged,
                "ev_soc_percent": ev.soc * 100.0,
                "battery_kw": batt_power,
                "battery_grid_charge_kw": batt_grid_charge,
                "battery_soc_percent": battery.soc,
                "grid_sell_kw": grid_sell,
                "grid_buy_kw": grid_buy,
            }
        )

    return pd.DataFrame.from_records(records).set_index("timestamp")


def summarize(results: pd.DataFrame, day_label: str, controller: str) -> dict:
    kwh = lambda column: float(results[column].sum() / 60.0)
    target = results[results["ev_soc_percent"] >= EV_SOC_TARGET * 100.0 - 1e-9]
    charging = results[results["ev_kw"] > 0]
    ev_kwh = kwh("ev_kw")
    return {
        "day": day_label,
        "date": results.index[0].date().isoformat(),
        "controller": controller,
        "pv_kwh": kwh("pv_kw"),
        "cooler_kwh": kwh("cooler_kw"),
        "cooler_min_f": float(results["cooler_temp_f"].min()),
        "cooler_max_f": float(results["cooler_temp_f"].max()),
        "cooler_unsafe_minutes": int(
            ((results["cooler_temp_f"] < TMIN) | (results["cooler_temp_f"] > TMAX)).sum()
        ),
        "ev_kwh": ev_kwh,
        "ev_final_soc_percent": float(results["ev_soc_percent"].iloc[-1]),
        "ev_target_reached_at": (
            None if target.empty else target.index[0].strftime("%H:%M")
        ),
        "ev_avg_charging_moer": (
            float((charging["ev_kw"] * charging["moer_lb_per_mwh"]).sum() / charging["ev_kw"].sum())
            if ev_kwh > 0 else None
        ),
        "battery_charged_kwh": float(results["battery_kw"].clip(lower=0).sum() / 60.0),
        "battery_discharged_kwh": float(-results["battery_kw"].clip(upper=0).sum() / 60.0),
        "battery_grid_charged_kwh": kwh("battery_grid_charge_kw"),
        "battery_final_soc_percent": float(results["battery_soc_percent"].iloc[-1]),
        "grid_buy_kwh": kwh("grid_buy_kw"),
        "grid_sell_kwh": kwh("grid_sell_kw"),
        # whole-farm grid import weighted by MOER; exports get no credit
        "grid_emissions_lb": float(
            (results["grid_buy_kw"] * results["moer_lb_per_mwh"]).sum() / 60.0 / 1000.0
        ),
    }


def simulate(
    pv_csv_path: str | None = None,
    cooler_csv_path: str | None = None,
    outdoor_csv_path: str = "core/Data/outdoor_temperatures.csv",
    simulation_date: str | pd.Timestamp | None = None,
    pv_xlsx_path: str | Path | None = None,
    moer_source: str = "historical",
    watttime_region: str = "MISO_DETROIT",
    watttime_cache_dir: str | Path = "results/ev_simulation/cache",
    controller: str = "with-ems",
) -> None:
    inputs = day_inputs(
        outdoor_csv_path,
        simulation_date,
        pv_csv_path,
        pv_xlsx_path,
        moer_source,
        watttime_region,
        watttime_cache_dir,
    )
    results = run_day(inputs, controller)
    summary = summarize(results, "Single day", controller)

    print(f"Final EV SoC:          {summary['ev_final_soc_percent']:.1f}%")
    print(f"Final battery SoC:     {summary['battery_final_soc_percent']:.1f}%")
    print(f"Cooler temp range:     {summary['cooler_min_f']:.1f}–{summary['cooler_max_f']:.1f} °F")
    print(f"Total PV energy:       {summary['pv_kwh']:.2f} kWh")
    print(f"Total cooler energy:   {summary['cooler_kwh']:.2f} kWh")
    print(f"Total EV energy:       {summary['ev_kwh']:.2f} kWh")
    print(f"Battery charged:       {summary['battery_charged_kwh']:.2f} kWh "
          f"({summary['battery_grid_charged_kwh']:.2f} kWh from grid)")
    print(f"Battery discharged:    {summary['battery_discharged_kwh']:.2f} kWh")
    print(f"Grid sell:             {summary['grid_sell_kwh']:.2f} kWh")
    print(f"Grid buy:              {summary['grid_buy_kwh']:.2f} kWh")

    _plot(results)


def run_scenario(
    scenario: str,
    workbooks: dict[str, Path | None],
    outdoor_csv_path: str | Path,
    output_dir: Path,
    synthetic: bool = False,
    moer_source: str = "historical",
    watttime_region: str = "MISO_DETROIT",
    watttime_cache_dir: str | Path = "results/ev_simulation/cache",
    show_plot: bool = False,
) -> pd.DataFrame:
    """Run one scenario over its sample days and controllers; save CSVs and a plot."""
    day_labels, controllers = SCENARIOS[scenario]
    scenario_dir = output_dir / scenario
    scenario_dir.mkdir(parents=True, exist_ok=True)

    results_by_day: dict[str, dict[str, pd.DataFrame]] = {}
    summaries: list[dict] = []
    for label in day_labels:
        if synthetic:
            inputs = day_inputs(
                outdoor_csv_path,
                simulation_date=SCENARIO_EXPECTED_DATES[DAY_KEYS[label]].isoformat(),
            )
        else:
            inputs = day_inputs(
                outdoor_csv_path,
                pv_xlsx_path=workbooks[label],
                moer_source=moer_source,
                watttime_region=watttime_region,
                watttime_cache_dir=watttime_cache_dir,
            )
        results_by_day[label] = {}
        for controller in controllers:
            results = run_day(inputs, controller)
            results_by_day[label][controller] = results
            summaries.append(summarize(results, label, controller))
            results.to_csv(scenario_dir / f"{DAY_KEYS[label]}_{controller}_minutes.csv")

    summary = pd.DataFrame(summaries)
    summary.to_csv(scenario_dir / "summary.csv", index=False)
    _plot_scenario(scenario, results_by_day, scenario_dir / "comparison.png", show_plot)
    _print_scenario(scenario, summary, synthetic)
    print(f"Outputs: {scenario_dir.resolve()}\n")
    return summary


def _load_real_inputs(
    pv_xlsx: Path,
    moer_source: str,
    region: str,
    cache_dir: Path,
) -> pd.DataFrame:
    """Sol-Ark PV and WattTime MOER, one row per minute, via ev_real_data_simulation."""
    day = load_solark_pv(pv_xlsx, "America/Detroit").index[0].date()
    cache_path = cache_dir / f"{moer_source}_{region}_{day.isoformat()}.json"
    token = "cache-only" if cache_path.exists() else get_watttime_token()
    return real_inputs(
        pv_xlsx,
        "America/Detroit",
        region,
        token,
        cache_path,
        moer_source,
    )


def _print_scenario(scenario: str, summary: pd.DataFrame, synthetic: bool) -> None:
    source = "sine-wave PV + synthetic MOER" if synthetic else "Sol-Ark PV + WattTime MOER"
    print(f"{scenario} ({source})")
    header = (
        f"{'Day':<13}{'Controller':<12}{'EV 95% at':>10}{'EV kWh':>9}"
        f"{'Grid buy':>10}{'Grid sell':>11}{'Grid CO2 lb':>13}{'Batt end':>10}{'Cooler °F':>13}"
    )
    print(header)
    print("-" * len(header))
    for row in summary.itertuples():
        print(
            f"{row.day:<13}{CONTROLLER_LABELS[row.controller]:<12}"
            f"{row.ev_target_reached_at or 'N/R':>10}{row.ev_kwh:>9.1f}"
            f"{row.grid_buy_kwh:>10.1f}{row.grid_sell_kwh:>11.1f}{row.grid_emissions_lb:>13.1f}"
            f"{row.battery_final_soc_percent:>9.0f}%"
            f"{row.cooler_min_f:>7.1f}–{row.cooler_max_f:.1f}"
        )


def _plot_scenario(
    scenario: str,
    results_by_day: dict[str, dict[str, pd.DataFrame]],
    output_path: Path,
    show_plot: bool,
) -> None:
    rows = (
        ("ev_soc_percent", "EV SoC (%)"),
        ("cooler_temp_f", "Cooler (°F)"),
        ("battery_soc_percent", "Battery SoC (%)"),
        ("grid_buy_kw", "Grid buy (kW)"),
    )
    labels = list(results_by_day)
    fig, axes = plt.subplots(
        len(rows) + 1, len(labels),
        figsize=(6 * len(labels), 14), sharex=True, squeeze=False,
    )
    fig.suptitle(f"Campus Farm EMS — {scenario}", fontsize=13)
    time_h = [m / 60.0 for m in range(1440)]

    for col, label in enumerate(labels):
        runs = results_by_day[label]
        inputs = next(iter(runs.values()))

        ax = axes[0][col]
        ax.plot(time_h, inputs["pv_kw"], color="orange", label="PV (kW)")
        ax.plot(time_h, inputs["moer_lb_per_mwh"] / 1000.0, color="gray", linestyle="--",
                alpha=0.6, label="MOER (×10³ lbs/MWh)")
        ax.axhline(CO2_THRESHOLD / 1000.0, color="gray", linestyle=":", linewidth=0.8)
        ax.set_title(f"{label} ({inputs.index[0].date()})")
        ax.legend(fontsize=8, loc="upper right")
        ax.grid(True, alpha=0.3)

        for row, (column, ylabel) in enumerate(rows, start=1):
            ax = axes[row][col]
            for controller, results in runs.items():
                ax.plot(time_h, results[column], color=CONTROLLER_COLORS[controller],
                        label=CONTROLLER_LABELS[controller], alpha=0.85)
            if column == "cooler_temp_f":
                ax.fill_between(time_h, TMIN, TMAX, color="green", alpha=0.05)
            if col == 0:
                ax.set_ylabel(ylabel)
            ax.legend(fontsize=8, loc="upper right")
            ax.grid(True, alpha=0.3)

        axes[-1][col].set_xlabel("Hour of day")
        axes[-1][col].set_xticks(range(0, 25, 2))

    plt.tight_layout()
    fig.savefig(output_path, dpi=120)
    if show_plot:
        plt.show()
    plt.close(fig)


def _plot(results: pd.DataFrame) -> None:
    time_h = [m / 60.0 for m in range(1440)]

    fig, axes = plt.subplots(6, 1, figsize=(13, 16), sharex=True)
    fig.suptitle("Campus Farm EMS — 1-day simulation", fontsize=13)

    ax = axes[0]
    ax.plot(time_h, results["pv_kw"], color="orange", label="PV output (kW)")
    ax.plot(time_h, results["moer_lb_per_mwh"] / 1000.0, color="gray", linestyle="--",
            alpha=0.6, label="Grid MOER (×10³ lbs/MWh)")
    ax.axhline(CO2_THRESHOLD / 1000.0, color="gray", linestyle=":", linewidth=0.8,
               label=f"CO₂ threshold ({CO2_THRESHOLD} lbs/MWh)")
    ax.set_ylabel("kW / (×10³ lbs/MWh)")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.plot(time_h, results["setpoint_f"],    color="steelblue", label="Setpoint (°F)", linewidth=1.5)
    ax.plot(time_h, results["cooler_temp_f"], color="crimson",   label="Actual temp (°F)", alpha=0.8)
    ax.axhline(TMIN, color="blue", linestyle="--", linewidth=0.8, label=f"TMIN={TMIN}°F")
    ax.axhline(TMAX, color="red",  linestyle="--", linewidth=0.8, label=f"TMAX={TMAX}°F")
    ax.fill_between(time_h, TMIN, TMAX, color="green", alpha=0.05, label="Safe zone")
    ax.set_ylabel("°F")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    ax.plot(time_h, results["ev_soc_percent"], color="green", label="EV SoC (%)")
    ax.axhline(EV_SOC_TARGET * 100, color="gray", linestyle="--", linewidth=0.8,
               label=f"Target {EV_SOC_TARGET * 100:.0f}%")
    ax.set_ylim(0, 105)
    ax.set_ylabel("%")
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(True, alpha=0.3)

    ax = axes[3]
    ax.plot(time_h, results["cooler_kw"], color="purple", label="Cooler load (kW)")
    ax.plot(time_h, results["ev_kw"], color="green", label="EV charger (kW)")
    ax.set_ylabel("kW")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.3)

    ax = axes[4]
    ax.plot(time_h, results["battery_soc_percent"], color="teal", label="Battery SoC (%)")
    ax.axhline(BATT_SOC_MIN, color="gray", linestyle="--", linewidth=0.8,
               label=f"SoC min {BATT_SOC_MIN}%")
    ax.set_ylim(0, 105)
    ax.set_ylabel("%")
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(True, alpha=0.3)

    ax = axes[5]
    ax.plot(time_h, results["battery_kw"], color="teal", label="Battery (charge +, discharge −)")
    ax.plot(time_h, results["grid_sell_kw"], color="goldenrod", label="Grid sell (kW)")
    ax.plot(time_h, results["grid_buy_kw"], color="dimgray", label="Grid buy (kW)")
    ax.set_xlabel("Hour of day")
    ax.set_ylabel("kW")
    ax.set_xticks(range(0, 25, 2))
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.show()


# ── Week-long simulation ──────────────────────────────────────────────────────

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
    """Hourly outdoor temperatures interpolated to the simulation minutes."""
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
    """Fraction of the Wednesday trip elapsed (0 → 1) while away; NaN while plugged in."""
    fraction = pd.Series(np.nan, index=minutes)
    trip_minutes = (EV_TRIP_END_HOUR - EV_TRIP_START_HOUR) * 60
    for day in pd.unique(minutes.normalize()):
        if day.weekday() != EV_TRIP_WEEKDAY:
            continue
        start = day + pd.Timedelta(hours=EV_TRIP_START_HOUR)
        away = (minutes >= start) & (minutes < day + pd.Timedelta(hours=EV_TRIP_END_HOUR))
        elapsed = (minutes[away] - start).total_seconds() / 60.0 + 1
        fraction[away] = elapsed / trip_minutes
    return fraction


def week_inputs(pv_xlsx: Path, moer_csv: Path, outdoor_csv: Path) -> pd.DataFrame:
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


def run_week(inputs: pd.DataFrame, controller: str) -> pd.DataFrame:
    """Run the week continuously.

    The battery's starting SOC is taken from the end of a first pass, so the week
    starts the way it ends (no assumption about Sunday midnight).
    """
    first = run_day(inputs, controller, ev_soc_init=EV_WEEK_SOC_INIT, battery_soc_init=BATT_SOC_MIN)
    start_soc = float(first["battery_soc_percent"].iloc[-1])
    return run_day(inputs, controller, ev_soc_init=EV_WEEK_SOC_INIT, battery_soc_init=start_soc)


def summarize_week(results: pd.DataFrame, controller: str) -> dict:
    kwh = lambda column: float(results[column].sum() / 60.0)
    returned = results.index[~results["ev_plugged"]]
    after_trip = results.loc[returned[-1]:] if len(returned) else results
    recharged = after_trip[after_trip["ev_soc_percent"] >= EV_SOC_TARGET * 100.0 - 1e-6]
    pv_kwh, sell_kwh = kwh("pv_kw"), kwh("grid_sell_kw")
    load_kwh = kwh("cooler_kw") + kwh("ev_kw")
    return {
        "controller": controller,
        "start": results.index[0].date().isoformat(),
        "end": results.index[-1].date().isoformat(),
        "pv_kwh": pv_kwh,
        "pv_used_on_site_kwh": pv_kwh - sell_kwh,
        "cooler_kwh": kwh("cooler_kw"),
        "cooler_unsafe_minutes": int(
            ((results["cooler_temp_f"] < TMIN) | (results["cooler_temp_f"] > TMAX)).sum()
        ),
        "ev_kwh": kwh("ev_kw"),
        "ev_back_to_target_at": (
            None if recharged.empty else recharged.index[0].strftime("%a %m/%d %H:%M")
        ),
        "ev_final_soc_percent": float(results["ev_soc_percent"].iloc[-1]),
        "battery_start_soc_percent": float(results["battery_soc_percent"].iloc[0]),
        "battery_final_soc_percent": float(results["battery_soc_percent"].iloc[-1]),
        "battery_charged_kwh": float(results["battery_kw"].clip(lower=0).sum() / 60.0),
        "battery_grid_charged_kwh": kwh("battery_grid_charge_kw"),
        "battery_discharged_kwh": float(-results["battery_kw"].clip(upper=0).sum() / 60.0),
        "grid_buy_kwh": kwh("grid_buy_kw"),
        "grid_sell_kwh": sell_kwh,
        "self_sufficiency_percent": 100.0 * (1 - kwh("grid_buy_kw") / load_kwh) if load_kwh else None,
        "grid_emissions_lb": float(
            (results["grid_buy_kw"] * results["moer_lb_per_mwh"]).sum() / 60.0 / 1000.0
        ),
    }


def daily_totals(results: pd.DataFrame, controller: str) -> pd.DataFrame:
    per_minute = results.assign(
        grid_emissions_lb=results["grid_buy_kw"] * results["moer_lb_per_mwh"] / 1000.0,
        pv_used_kw=results["pv_kw"] - results["grid_sell_kw"],
        battery_charge_kw=results["battery_kw"].clip(lower=0),
        battery_discharge_kw=-results["battery_kw"].clip(upper=0),
    )
    columns = [
        "pv_kw", "pv_used_kw", "cooler_kw", "ev_kw", "battery_charge_kw",
        "battery_grid_charge_kw", "battery_discharge_kw", "grid_buy_kw",
        "grid_sell_kw", "grid_emissions_lb",
    ]
    daily = per_minute[columns].resample("D").sum() / 60.0
    daily.columns = [c.replace("_kw", "_kwh") for c in columns]
    daily.insert(0, "controller", controller)
    daily.index = daily.index.strftime("%a %m/%d")
    return daily


def run_week_scenario(
    pv_xlsx: Path,
    moer_csv: Path,
    outdoor_csv: Path,
    output_dir: Path,
    show_plot: bool = False,
) -> pd.DataFrame:
    inputs = week_inputs(pv_xlsx, moer_csv, outdoor_csv)
    label = f"week-{inputs.index[0].date().isoformat()}"
    week_dir = output_dir / label
    week_dir.mkdir(parents=True, exist_ok=True)

    results_by_controller: dict[str, pd.DataFrame] = {}
    summaries, dailies = [], []
    for controller in CONTROLLERS:
        results = run_week(inputs, controller)
        results_by_controller[controller] = results
        summaries.append(summarize_week(results, controller))
        dailies.append(daily_totals(results, controller))
        results.to_csv(week_dir / f"{controller}_minutes.csv")

    summary = pd.DataFrame(summaries)
    summary.to_csv(week_dir / "summary.csv", index=False)
    daily = pd.concat(dailies)
    daily.to_csv(week_dir / "daily.csv")

    filled_hours = inputs["moer_filled"].sum() / 60.0
    _plot_week_timeline(label, inputs, results_by_controller, week_dir / "timeline.png", show_plot)
    _plot_week_daily(label, daily, week_dir / "daily.png", show_plot)
    _print_week(label, summary, filled_hours)
    print(f"Outputs: {week_dir.resolve()}\n")
    return summary


def _print_week(label: str, summary: pd.DataFrame, filled_hours: float) -> None:
    print(f"{label} (Sol-Ark PV + WattTime MOER, EV trip Wed 9:00–19:00)")
    if filled_hours:
        print(f"  note: {filled_hours:.1f} h of MOER filled from the week before")
    header = (
        f"{'Controller':<12}{'Grid buy':>10}{'Grid sell':>11}{'Grid CO2 lb':>13}"
        f"{'Self-suff.':>12}{'EV kWh':>9}{'EV back to 95%':>17}{'Batt in/out kWh':>17}{'Unsafe min':>12}"
    )
    print(header)
    print("-" * len(header))
    for row in summary.itertuples():
        print(
            f"{CONTROLLER_LABELS[row.controller]:<12}{row.grid_buy_kwh:>10.1f}{row.grid_sell_kwh:>11.1f}"
            f"{row.grid_emissions_lb:>13.1f}{row.self_sufficiency_percent:>11.0f}%{row.ev_kwh:>9.1f}"
            f"{row.ev_back_to_target_at or 'not reached':>17}"
            f"{row.battery_charged_kwh:>9.1f}/{row.battery_discharged_kwh:<7.1f}"
            f"{row.cooler_unsafe_minutes:>12}"
        )


def _day_axis(ax, index: pd.DatetimeIndex) -> None:
    days = pd.date_range(index[0].normalize(), index[-1].normalize(), freq="D")
    ax.set_xticks(days + pd.Timedelta(hours=12), [d.strftime("%a %m/%d") for d in days])
    for day in days[1:]:
        ax.axvline(day, color="gray", linewidth=0.6, alpha=0.5)


def _shade(ax, index: pd.DatetimeIndex, mask: pd.Series, color: str, label: str) -> None:
    runs = (mask != mask.shift()).cumsum()[mask]
    for i, (_, run) in enumerate(runs.groupby(runs)):
        ax.axvspan(run.index[0], run.index[-1], color=color, alpha=0.15,
                   label=label if i == 0 else None)


def _plot_week_timeline(
    label: str,
    inputs: pd.DataFrame,
    results_by_controller: dict[str, pd.DataFrame],
    output_path: Path,
    show_plot: bool,
) -> None:
    rows = (
        ("ev_soc_percent", "EV SoC (%)"),
        ("cooler_temp_f", "Cooler (°F)"),
        ("battery_soc_percent", "Battery SoC (%)"),
        ("grid_buy_kw", "Grid buy (kW)"),
    )
    fig, axes = plt.subplots(len(rows) + 1, 1, figsize=(18, 16), sharex=True)
    fig.suptitle(f"Campus Farm EMS — {label}", fontsize=13)
    away = ~next(iter(results_by_controller.values()))["ev_plugged"].astype(bool)

    ax = axes[0]
    ax.plot(inputs.index, inputs["pv_kw"], color="orange", linewidth=0.8, label="PV (kW)")
    ax.set_ylabel("PV (kW)")
    moer_ax = ax.twinx()
    moer_ax.plot(inputs.index, inputs["moer_lb_per_mwh"], color="gray", linewidth=0.6,
                 alpha=0.7, label="MOER (lb/MWh)")
    moer_ax.axhline(CO2_THRESHOLD, color="gray", linestyle=":", linewidth=0.8)
    moer_ax.set_ylabel("MOER (lb/MWh)")
    if inputs["moer_filled"].any():
        _shade(ax, inputs.index, inputs["moer_filled"], "red", "MOER filled from week before")
    handles = ax.get_legend_handles_labels()[0] + moer_ax.get_legend_handles_labels()[0]
    ax.legend(handles=handles, fontsize=8, loc="upper left")
    ax.grid(True, alpha=0.3)

    for row, (column, ylabel) in enumerate(rows, start=1):
        ax = axes[row]
        for controller, results in results_by_controller.items():
            ax.plot(results.index, results[column], color=CONTROLLER_COLORS[controller],
                    label=CONTROLLER_LABELS[controller], linewidth=0.8, alpha=0.85)
        _shade(ax, inputs.index, away, "purple", "EV away (Wed trip)")
        if column == "cooler_temp_f":
            ax.axhspan(TMIN, TMAX, color="green", alpha=0.05)
        ax.set_ylabel(ylabel)
        ax.legend(fontsize=8, loc="upper right")
        ax.grid(True, alpha=0.3)

    _day_axis(axes[-1], inputs.index)
    plt.tight_layout()
    fig.savefig(output_path, dpi=120)
    if show_plot:
        plt.show()
    plt.close(fig)


def _plot_week_daily(label: str, daily: pd.DataFrame, output_path: Path, show_plot: bool) -> None:
    panels = (
        ("grid_buy_kwh", "Grid buy (kWh)"),
        ("grid_emissions_lb", "Grid CO₂ (lb)"),
        ("pv_used_kwh", "Solar used on site (kWh)"),
        ("battery_discharge_kwh", "Battery discharged (kWh)"),
    )
    days = list(dict.fromkeys(daily.index))
    controllers = list(dict.fromkeys(daily["controller"]))
    width = 0.8 / len(controllers)
    x = np.arange(len(days))

    fig, axes = plt.subplots(len(panels), 1, figsize=(14, 13), sharex=True)
    fig.suptitle(f"Campus Farm EMS — {label}, daily totals", fontsize=13)
    for ax, (column, ylabel) in zip(axes, panels):
        for i, controller in enumerate(controllers):
            values = daily[daily["controller"] == controller][column].reindex(days)
            ax.bar(x + (i - (len(controllers) - 1) / 2) * width, values, width,
                   color=CONTROLLER_COLORS[controller], label=CONTROLLER_LABELS[controller])
        ax.set_ylabel(ylabel)
        ax.grid(True, axis="y", alpha=0.3)
        ax.legend(fontsize=8, loc="upper right")
    axes[-1].set_xticks(x, days)
    plt.tight_layout()
    fig.savefig(output_path, dpi=120)
    if show_plot:
        plt.show()
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Campus Farm EMS — 1-day simulation")
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"),
                        help="Run a sample-day scenario instead of a single day")
    parser.add_argument("--week-pv-xlsx", type=Path,
                        help="Week run: Sol-Ark Operation Data export covering whole days (Sun–Sat)")
    parser.add_argument("--moer-csv", type=Path,
                        help="Week run: WattTime CSV with point_time (UTC) and value columns")
    parser.add_argument("--high-pv-xlsx", type=Path, help="Sol-Ark export for the High PV day")
    parser.add_argument("--typical-pv-xlsx", type=Path, help="Sol-Ark export for the Typical Day")
    parser.add_argument("--low-pv-xlsx", type=Path, help="Sol-Ark export for the Low PV day")
    parser.add_argument("--synthetic", action="store_true",
                        help="Scenarios: use sine-wave PV and synthetic MOER on the sample dates")
    parser.add_argument("--output-dir", type=Path, default=Path("results/integrated_simulation"))
    parser.add_argument("--show-plot", action="store_true")
    parser.add_argument("--controller", choices=CONTROLLERS, default="with-ems",
                        help="Single-day run: which controller to use")
    parser.add_argument("--csv", metavar="PATH", help="CSV with 'Minute' and 'Power' columns")
    parser.add_argument("--pv-xlsx", metavar="PATH",
                        help="Sol-Ark one-day Excel export; uses WattTime MOER for that day")
    parser.add_argument("--moer-source", choices=("historical", "forecast-historical"),
                        default="historical")
    parser.add_argument("--watttime-cache-dir", type=Path,
                        default=Path("results/ev_simulation/cache"),
                        help="Saved WattTime responses, named <moer-source>_<region>_<date>.json")
    parser.add_argument("--outdoor-csv", metavar="PATH",
                        default="core/Data/outdoor_temperatures.csv")
    parser.add_argument("--date", help="Simulation date (YYYY-MM-DD) when not using --pv-xlsx")
    args = parser.parse_args()

    if args.week_pv_xlsx:
        if args.moer_csv is None:
            parser.error("--week-pv-xlsx requires --moer-csv")
        run_week_scenario(
            args.week_pv_xlsx,
            args.moer_csv,
            Path(args.outdoor_csv),
            args.output_dir,
            show_plot=args.show_plot,
        )
    elif args.scenario:
        workbooks = {
            "High PV": args.high_pv_xlsx,
            "Typical Day": args.typical_pv_xlsx,
            "Low PV": args.low_pv_xlsx,
        }
        scenarios = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
        needed = {label for name in scenarios for label in SCENARIOS[name][0]}
        missing = sorted(label for label in needed if workbooks[label] is None)
        if missing and not args.synthetic:
            parser.error(
                f"Missing Sol-Ark workbooks for {', '.join(missing)} "
                "(pass --high-pv-xlsx/--typical-pv-xlsx/--low-pv-xlsx, or --synthetic)"
            )
        for name in scenarios:
            run_scenario(
                name,
                workbooks,
                args.outdoor_csv,
                args.output_dir,
                synthetic=args.synthetic,
                moer_source=args.moer_source,
                watttime_cache_dir=args.watttime_cache_dir,
                show_plot=args.show_plot,
            )
    else:
        simulate(
            pv_csv_path=args.csv,
            outdoor_csv_path=args.outdoor_csv,
            simulation_date=args.date,
            pv_xlsx_path=args.pv_xlsx,
            moer_source=args.moer_source,
            watttime_cache_dir=args.watttime_cache_dir,
            controller=args.controller,
        )
