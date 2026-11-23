"""
Live carbon tracking for the Campus Farm dashboard: three lines of lb CO2 from grid electricity.

  baseline    : the farm before solar, battery and the EV truck. Cooler at the CoolBot's fixed
                38 °F plus other loads, all bought from the grid, plus a gas truck doing the
                deliveries.
  without-ems : the farm's PV, battery and EV truck with no EMS (simulation_ems "without-ems"),
                run as a shadow model on live PV, outdoor temperature and MOER.
  with-ems    : measured. Sol-Ark grid buy × live MOER.

CO2 = grid bought × MOER, and selling earns no credit (same rule as run_simulation_ems.py).
Only farm loads on the inverter count: cooler, EV truck, other loads (wifi/electronics room)
and the inverter's own use. Baseline has no inverter, so no inverter use.

Live use: one CarbonTracker per process, call update() every EMS cycle (POLL_INTERVAL) with that
cycle's readings, and keep its state across restarts with save_state() / load_state().

Usage (from the repo folder):
    python core/carbon_live.py              # replay the default week and check against the simulation
    python core/carbon_live.py --week-pv-xlsx <Sol-Ark week export> --outdoor-csv <temps>
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from simulation_ems import (
    BATT_SOC_MIN,
    DEFAULT_MOER_CSV,
    DEFAULT_OUTDOOR_CSV,
    DEFAULT_WEEK_PV,
    EV_CAPACITY,
    EV_CHARGE_EFF,
    EV_CHARGER_KW,
    EV_RESUME_MARGIN,
    EV_SOC_TARGET,
    EV_TRIP_SOC_USED,
    INVERTER_PV_LOSS,
    INVERTER_STANDBY_DAY_KW,
    INVERTER_STANDBY_KW,
    OTHER_LOAD_KW,
    PV_STANDBY_THRESHOLD_KW,
    SETPOINT_ECON,
    SETPOINT_NO_EMS,
    SimBattery,
    SimCooler,
    SimEV,
    run_minutes,
    week_inputs,
)


# ── Constants ─────────────────────────────────────────────────────────────────
# Gas truck that the EV truck replaced (baseline only)
EV_KWH_PER_MILE           = 0.48   # kWh/mile, F-150 Lightning extended range, EPA ~48 kWh/100 mi (assumed, check)
GAS_TRUCK_CO2_LB_PER_MILE = 1.59   # lb CO2/mile, previous team's figure for the gas delivery truck
EV_MIN_TRIP_MINUTES       = 60     # unplugged shorter than this = not a delivery (no SOC used, no gas miles)

STATE_FILE = Path(__file__).parent / "carbon_live_state.json"   # *.json is git-ignored


def grid_kw_from_flow(flow_data: dict) -> tuple[float, float]:
    """Sol-Ark flow API "data" → (grid_buy_kw, grid_sell_kw).

    gridOrMeterPower is always positive (W); the direction is in the flags:
    gridTo = buying from the grid, toGrid = selling to the grid.
    (The Sol-Ark week export's AcTotalPower is signed instead: + buy, − sell.)
    """
    grid_kw = abs(float(flow_data.get("gridOrMeterPower", 0))) / 1000.0
    if flow_data.get("gridTo"):
        return grid_kw, 0.0
    if flow_data.get("toGrid"):
        return 0.0, grid_kw
    return 0.0, 0.0


def co2_lb(kw: float, moer_lb_per_mwh: float, minutes: float = 1.0) -> float:
    """kW for some minutes × lb CO2/MWh → lb CO2."""
    return kw * minutes / 60.0 / 1000.0 * moer_lb_per_mwh


# ── Shadow model ──────────────────────────────────────────────────────────────

class CarbonTracker:
    """Runs the without-ems farm alongside the real one and turns all three lines into lb CO2.

    The baseline cooler is the same 38 °F, no-fan cooler as without-ems, so one SimCooler serves both.
    """

    def __init__(self, outdoor_f: float, battery_soc_percent: float = BATT_SOC_MIN,
                 ev_soc: float = EV_SOC_TARGET, cooler_start_setpoint_f: float = SETPOINT_NO_EMS):
        # Live: the cooler is already holding 38 °F. (The week simulation starts it at its default
        # 50 °F setpoint, so replay_week passes that to match it.)
        self.cooler = SimCooler(outdoor_f=outdoor_f, setpoint_f=cooler_start_setpoint_f)
        self.ev = SimEV(soc_init=ev_soc, soc_target=EV_SOC_TARGET, charge_rate_kw=EV_CHARGER_KW,
                        capacity_kwh=EV_CAPACITY, charge_eff=EV_CHARGE_EFF)
        self.battery = SimBattery(soc_init=battery_soc_percent)
        self.wants_charge = self.ev.soc < self.ev.target   # truck below its limit?
        self.ev_away_minutes = 0                           # minutes unplugged so far this trip

    # one minute of the without-ems farm, same steps as simulation_ems.run_minutes
    def step_minute(self, timestamp: pd.Timestamp, pv_kw: float, moer_lb_per_mwh: float,
                    outdoor_f: float, ev_plugged: bool) -> dict:
        pv_ac_kw = pv_kw * (1.0 - INVERTER_PV_LOSS)
        other_kw = OTHER_LOAD_KW
        standby_kw = INVERTER_STANDBY_DAY_KW if pv_kw > PV_STANDBY_THRESHOLD_KW else INVERTER_STANDBY_KW

        # cooler: fixed 38 °F, no fan (the farm today)
        self.cooler.change_setpoint(SETPOINT_NO_EMS)
        self.cooler.update(outdoor_f, False)

        # truck: full power whenever below the limit; a delivery uses EV_TRIP_SOC_USED
        gas_truck_co2_lb = 0.0
        if not ev_plugged:
            self.ev_away_minutes += 1
            self.ev.charging = False
            self.ev.input_power_kw = 0.0
        else:
            if self.ev_away_minutes >= EV_MIN_TRIP_MINUTES:   # back from a delivery
                self.ev.soc -= EV_TRIP_SOC_USED
                trip_miles = EV_TRIP_SOC_USED * EV_CAPACITY / EV_KWH_PER_MILE
                gas_truck_co2_lb = trip_miles * GAS_TRUCK_CO2_LB_PER_MILE
            self.ev_away_minutes = 0
            if self.ev.soc <= self.ev.target - EV_RESUME_MARGIN:
                self.wants_charge = True
            elif self.ev.soc >= self.ev.target - 1e-9:
                self.wants_charge = False
            ev_kw = self.ev.rate if self.wants_charge else 0.0
            self.ev.charge(ev_kw) if ev_kw > 0.0 else self.ev.idle()

        # battery and grid (Load First)
        _, grid_sell_kw, grid_buy_kw = self.battery.dispatch(
            pv_ac_kw, self.cooler.instant_power_kw, self.ev.input_power_kw, timestamp.hour,
            other_kw=other_kw + standby_kw,
        )

        baseline_grid_buy_kw = self.cooler.instant_power_kw + other_kw   # no solar, no inverter
        return {
            "cooler_kw": self.cooler.instant_power_kw,
            "ev_kw": self.ev.input_power_kw,
            "without_ems_grid_buy_kw": grid_buy_kw,
            "without_ems_grid_sell_kw": grid_sell_kw,
            "baseline_grid_buy_kw": baseline_grid_buy_kw,
            "without_ems_co2_lb": co2_lb(grid_buy_kw, moer_lb_per_mwh),
            "baseline_co2_lb": co2_lb(baseline_grid_buy_kw, moer_lb_per_mwh) + gas_truck_co2_lb,
            "gas_truck_co2_lb": gas_truck_co2_lb,
        }

    def update(self, timestamp: pd.Timestamp, interval_minutes: int, pv_kw: float,
               moer_lb_per_mwh: float, outdoor_f: float, ev_plugged: bool,
               grid_buy_kw: float) -> dict:
        """One EMS cycle: readings held for interval_minutes, ending at timestamp.

        grid_buy_kw is the measured Sol-Ark grid buy (see grid_kw_from_flow).
        Returns this interval's lb CO2 for each line, ready to POST to /api/cf/carbon.
        """
        start = timestamp - pd.Timedelta(minutes=interval_minutes)
        totals = {"baseline_co2_lb": 0.0, "without_ems_co2_lb": 0.0, "gas_truck_co2_lb": 0.0}
        for i in range(interval_minutes):
            minute = self.step_minute(start + pd.Timedelta(minutes=i + 1), pv_kw,
                                      moer_lb_per_mwh, outdoor_f, ev_plugged)
            for key in totals:
                totals[key] += minute[key]
        return {
            "timestamp": timestamp.isoformat(),
            "interval_minutes": interval_minutes,
            "moer_lb_per_mwh": moer_lb_per_mwh,
            "grid_buy_kw": grid_buy_kw,
            "with_ems_co2_lb": co2_lb(grid_buy_kw, moer_lb_per_mwh, interval_minutes),
            **totals,
        }

    # keep the shadow farm's state across restarts
    def to_state(self) -> dict:
        return {
            "cooler_temp_f": self.cooler.temp,
            "cooler_compressor_demand": self.cooler._compressor_demand,
            "cooler_outdoor_f": self.cooler.outdoor_temp,
            "ev_soc": self.ev.soc,
            "ev_wants_charge": self.wants_charge,
            "ev_away_minutes": self.ev_away_minutes,
            "battery_soc_percent": self.battery.soc,
        }

    @classmethod
    def from_state(cls, state: dict) -> "CarbonTracker":
        tracker = cls(outdoor_f=state["cooler_outdoor_f"],
                      battery_soc_percent=state["battery_soc_percent"], ev_soc=state["ev_soc"])
        tracker.cooler.temp = state["cooler_temp_f"]
        tracker.cooler._compressor_demand = state["cooler_compressor_demand"]
        tracker.wants_charge = state["ev_wants_charge"]
        tracker.ev_away_minutes = state["ev_away_minutes"]
        return tracker

    def save_state(self, path: Path = STATE_FILE) -> None:
        path.write_text(json.dumps(self.to_state(), indent=2))

    @classmethod
    def load_state(cls, outdoor_f: float, path: Path = STATE_FILE) -> "CarbonTracker":
        """Saved state if there is one, otherwise a fresh shadow farm."""
        if path.exists():
            return cls.from_state(json.loads(path.read_text()))
        return cls(outdoor_f=outdoor_f)


# ── Replay check ──────────────────────────────────────────────────────────────

def replay_week(inputs: pd.DataFrame, battery_soc_percent: float = BATT_SOC_MIN) -> pd.DataFrame:
    """Feed a past week through CarbonTracker one minute at a time, like the live loop would."""
    tracker = CarbonTracker(outdoor_f=float(inputs["outdoor_f"].iloc[0]),
                            battery_soc_percent=battery_soc_percent,
                            cooler_start_setpoint_f=SETPOINT_ECON)
    records = []
    for timestamp, row in inputs.iterrows():
        minute = tracker.step_minute(timestamp, float(row["pv_kw"]), float(row["moer_lb_per_mwh"]),
                                     float(row["outdoor_f"]), pd.isna(row["ev_trip_fraction"]))
        records.append({"timestamp": timestamp, **minute})
    return pd.DataFrame.from_records(records).set_index("timestamp")


def check_against_simulation(pv_xlsx: Path, moer_csv: Path, outdoor_csv: Path) -> bool:
    """Replay the week and compare minute by minute with simulation_ems "without-ems"."""
    inputs = week_inputs(pv_xlsx, moer_csv, outdoor_csv)
    sim = run_minutes(inputs, "without-ems", battery_soc_init=BATT_SOC_MIN)
    with_ems = run_minutes(inputs, "with-ems", battery_soc_init=BATT_SOC_MIN)
    live = replay_week(inputs)

    ok = True
    for live_col, sim_col in [("without_ems_grid_buy_kw", "grid_buy_kw"),
                              ("without_ems_grid_sell_kw", "grid_sell_kw"),
                              ("cooler_kw", "cooler_kw"), ("ev_kw", "ev_kw")]:
        diff = float(np.abs(live[live_col].to_numpy() - sim[sim_col].to_numpy()).max())
        match = diff < 1e-9
        ok &= match
        print(f"  {live_col:<26} max difference {diff:.2e} kW  {'OK' if match else 'MISMATCH'}")

    sim_co2_lb = float((sim["grid_buy_kw"] * sim["moer_lb_per_mwh"]).sum() / 60.0 / 1000.0)
    with_ems_co2_lb = float((with_ems["grid_buy_kw"] * with_ems["moer_lb_per_mwh"]).sum() / 60.0 / 1000.0)
    gas_lb = float(live["gas_truck_co2_lb"].sum())
    baseline_lb = float(live["baseline_co2_lb"].sum())
    print(f"\nWeek of {inputs.index[0]:%Y-%m-%d} (lb CO2)")
    print(f"  baseline     {baseline_lb:8.1f}   (grid {baseline_lb - gas_lb:.1f} + gas truck {gas_lb:.1f})")
    print(f"  without-ems  {live['without_ems_co2_lb'].sum():8.1f}   (simulation: {sim_co2_lb:.1f})")
    print(f"  with-ems     {with_ems_co2_lb:8.1f}   (simulated binary, standing in for the measured grid buy)")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--week-pv-xlsx", type=Path, default=DEFAULT_WEEK_PV)
    parser.add_argument("--moer-csv", type=Path, default=DEFAULT_MOER_CSV)
    parser.add_argument("--outdoor-csv", type=Path, default=DEFAULT_OUTDOOR_CSV)
    args = parser.parse_args()
    ok = check_against_simulation(args.week_pv_xlsx, args.moer_csv, args.outdoor_csv)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
