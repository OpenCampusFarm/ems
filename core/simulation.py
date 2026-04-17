"""
Campus Farm EMS — one-day simulation.

Runs 1 440 minutes (one full day) through a physics-based model:
  - PV    : real-world CSV data if available, otherwise sine-wave approximation
  - Cooler: first-order RC thermal model with bang-bang thermostat
  - EV    : simple SoC integrator
  - Grid  : synthetic CO2 signal that mimics a duck-curve daily pattern

EMS decision each minute:
  - "Clean" if PV >= PV_MIN_PRODUCING kW  OR  synthetic MOER < CO2_THRESHOLD
  - Clean  → CoolBot setpoint = SETPOINT_COOLTH (35 °F), charge EV
  - Dirty  → CoolBot setpoint = SETPOINT_ECON   (48 °F), don't charge EV
  - Safety: TMIN/TMAX overrides applied before the normal clean/dirty choice

Usage:
    python normal.py                          # sine-wave PV
    python normal.py --csv PVdata.csv         # real PV data
"""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

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

    def update(self) -> None:
        self._thermostat()
        self._thermal_step()

    def change_setpoint(self, sp: float) -> None:
        self.setpoint = sp

    @property
    def instant_power_kw(self) -> float:
        return self.power_kw if self._on else 0.0


# ── EV model ──────────────────────────────────────────────────────────────────

class EV:
    def __init__(
        self,
        soc_init: float       = EV_SOC_INIT,
        capacity_kwh: float   = EV_CAPACITY,
        charge_rate_kw: float = EV_CHARGE_RATE,
        charge_eff: float     = EV_CHARGE_EFF,
        soc_target: float     = EV_SOC_TARGET,
    ):
        self.soc      = soc_init
        self.capacity = capacity_kwh
        self.rate     = charge_rate_kw
        self.eff      = charge_eff
        self.target   = soc_target
        self.charging = False

    def charge(self, dt_hours: float = 1.0 / 60.0) -> None:
        if self.soc < self.target:
            self.soc = min(
                self.soc + (self.rate * self.eff * dt_hours) / self.capacity,
                1.0,
            )
            self.charging = True
        else:
            self.charging = False

    def idle(self, dt_hours: float = 1.0 / 60.0) -> None:
        self.soc = max(0.0, self.soc - (0.02 / (30 * 24)) * dt_hours * 60)
        self.charging = False


# ── EMS decision ──────────────────────────────────────────────────────────────

def ems_setpoint(pv_kw: float, moer: float, cooler_temp: float) -> int:
    if cooler_temp < TMIN:
        return SETPOINT_ECON
    if cooler_temp > TMAX:
        return SETPOINT_COOLTH
    energy_clean = pv_kw >= PV_MIN_PRODUCING or moer < CO2_THRESHOLD
    return SETPOINT_COOLTH if energy_clean else SETPOINT_ECON


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate(csv_path: str | None = None) -> None:
    pv     = PV(csv_path=csv_path)
    cooler = Cooler()
    ev     = EV()

    setpoints, cooler_temps, ev_socs, pv_powers, cooler_loads, moers = (
        [] for _ in range(6)
    )

    for minute in range(1440):
        pv_kw = pv.update(minute)
        moer  = synthetic_moer(minute)
        cooler.update()

        sp = ems_setpoint(pv_kw, moer, cooler.temp)
        cooler.change_setpoint(sp)

        energy_clean = pv_kw >= PV_MIN_PRODUCING or moer < CO2_THRESHOLD
        ev.charge() if energy_clean else ev.idle()

        setpoints.append(sp)
        cooler_temps.append(cooler.temp)
        ev_socs.append(ev.soc * 100.0)
        pv_powers.append(pv_kw)
        cooler_loads.append(cooler.instant_power_kw)
        moers.append(moer)

    print(f"Final EV SoC:          {ev.soc * 100:.1f}%")
    print(f"Cooler temp range:     {min(cooler_temps):.1f}–{max(cooler_temps):.1f} °F")
    print(f"Total PV energy:       {sum(pv_powers) / 60:.2f} kWh")
    print(f"Total cooler energy:   {sum(cooler_loads) / 60:.2f} kWh")

    _plot(setpoints, cooler_temps, ev_socs, pv_powers, cooler_loads, moers)


def _plot(setpoints, cooler_temps, ev_socs, pv_powers, cooler_loads, moers):
    time_h = [m / 60.0 for m in range(1440)]

    fig, axes = plt.subplots(4, 1, figsize=(13, 11), sharex=True)
    fig.suptitle("Campus Farm EMS — 1-day simulation", fontsize=13)

    ax = axes[0]
    ax.plot(time_h, pv_powers, color="orange", label="PV output (kW)")
    ax.plot(time_h, [m / 1000.0 for m in moers], color="gray", linestyle="--",
            alpha=0.6, label="Grid MOER (×10³ lbs/MWh)")
    ax.axhline(CO2_THRESHOLD / 1000.0, color="gray", linestyle=":", linewidth=0.8,
               label=f"CO₂ threshold ({CO2_THRESHOLD} lbs/MWh)")
    ax.set_ylabel("kW / (×10³ lbs/MWh)")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.3)

    ax = axes[1]
    ax.plot(time_h, setpoints,    color="steelblue", label="Setpoint (°F)", linewidth=1.5)
    ax.plot(time_h, cooler_temps, color="crimson",   label="Actual temp (°F)", alpha=0.8)
    ax.axhline(TMIN, color="blue", linestyle="--", linewidth=0.8, label=f"TMIN={TMIN}°F")
    ax.axhline(TMAX, color="red",  linestyle="--", linewidth=0.8, label=f"TMAX={TMAX}°F")
    ax.fill_between(time_h, TMIN, TMAX, color="green", alpha=0.05, label="Safe zone")
    ax.set_ylabel("°F")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.3)

    ax = axes[2]
    ax.plot(time_h, ev_socs, color="green", label="EV SoC (%)")
    ax.axhline(EV_SOC_TARGET * 100, color="gray", linestyle="--", linewidth=0.8,
               label=f"Target {EV_SOC_TARGET * 100:.0f}%")
    ax.set_ylim(0, 105)
    ax.set_ylabel("%")
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(True, alpha=0.3)

    ax = axes[3]
    ax.plot(time_h, cooler_loads, color="purple", label="Cooler load (kW)")
    ax.set_xlabel("Hour of day")
    ax.set_ylabel("kW")
    ax.set_xticks(range(0, 25, 2))
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Campus Farm EMS — 1-day simulation")
    parser.add_argument("--csv", metavar="PATH", help="CSV with 'Minute' and 'Power' columns")
    args = parser.parse_args()
    simulate(csv_path=args.csv)
