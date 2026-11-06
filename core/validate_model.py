"""Compare the simulation with the real Sol-Ark inverter data, week of 9/20–9/26.

The simulation is set up the way the farm ran: No EMS (cooler fixed at 38 °F, EV charges as
soon as it is plugged in), one battery, Batt First solar priority, no grid charging of the
battery. The Wednesday EV charge starts when the inverter shows it starting and takes the
energy the inverter recorded.

Two versions of the model are compared with the real data:
  original  the model before tuning
  tuned     plus the "match the real farm" adjustments fitted to the inverter data
            (inverter losses, battery charging efficiency, measured EV draw, cooler power
            rising with outdoor temperature). The loads are still calculated by the model
            (cooler from outdoor temperature + flat wifi/electronics load), not taken from the data.

The week of 9/13 is not plotted: the battery stopped at ~30% Sun–Wed that week (different
inverter setting). It still goes into the inverter loss and battery efficiency fit.

Saves to results/integrated_simulation/validation/:
  daily_comparison.csv              real vs original vs tuned daily totals
  V1_daily_real_vs_model.png        daily grid buy, grid sell, battery charge / discharge
  V2_hourly_profile.png             average day, hour by hour (EV day excluded)
  V3_battery_soc_real_vs_model.png  battery charge Mon–Wed 9/21–9/23

With --battery-powers-ev the tuned model also lets the battery cover the EV after 7 PM, like
the real inverter did on the EV days (the team treats that as a mistake to fix at the farm),
and the car slows its charging down near full, as measured on 9/16 and 9/23.
Those charts go to results/integrated_simulation/validation/battery_powers_ev/.

Usage:
    python core/validate_model.py [--battery-powers-ev]
"""

import argparse

import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import simulation as sim

warnings.filterwarnings("ignore", category=UserWarning)

DATA = Path("core/Data")
MOER = DATA / "watttime_MISO_DETROIT_2026-09-13_to_09-26_utc.csv"
OUTDOOR = DATA / "outdoor_temperatures_2026-09-12_to_09-27.csv"
WEEKS = {
    "9/20": DATA / "PV_2026-09-20_to_2026-09-27.xlsx",
    "9/13": DATA / "PV_2026-09-13_to_2026-09-20.xlsx",
}
OUT = Path("results/integrated_simulation/validation")

# Reference palette (dataviz skill), light mode
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e4e3df"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
NEUTRAL = "#a8a7a2"
WEEK = "9/20"
# EV charging seen on the inverter: start hour, kWh (load above the hour before it, summed)
REAL_TRIPS = {"9/13": (15 + 20 / 60, 80.2), "9/20": (16.0, 43.6)}
REAL_COLOR, ORIGINAL_COLOR, TUNED_COLOR = TEXT_2, NEUTRAL, SERIES[0]
SOURCES = {"real": ("Real (inverter)", REAL_COLOR),
           "original": ("Original model", ORIGINAL_COLOR),
           "tuned": ("Tuned model", TUNED_COLOR)}

# Only with --battery-powers-ev: the car slows down near full, so it is still charging after
# 7 PM when the battery may discharge. Measured on 9/16 and 9/23: ~5.7 kWh over ~75–80 min.
EV_TAPER_KWH = 5.7
EV_TAPER_END_KW = 1.7

# The model before tuning, for the "original" bars. The tuned values are simulation.py's own.
ORIGINAL = {
    "INVERTER_STANDBY_KW": 0.0,
    "INVERTER_STANDBY_DAY_KW": 0.0,
    "INVERTER_PV_LOSS": 0.0,
    "BATT_CHARGE_EFF": 1.0,
    "BATT_DISCHARGE_EFF": 1.0,
    "EV_CHARGER_KW": sim.OPENEVSE_MAX_A * sim.OPENEVSE_VOLTS / 1000,   # charger maximum, 11.52 kW
    "COOLER_ELECTRIC_KW": 0.24,
    "COOLER_ELECTRIC_KW_PER_F": 0.0,
}
TUNED = {name: getattr(sim, name) for name in ORIGINAL}


def style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT_2, labelsize=10)
    ax.yaxis.label.set_color(TEXT_2)
    ax.xaxis.label.set_color(TEXT_2)
    ax.grid(True, axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def new_figure(width: float, height: float, title: str, subtitle: str):
    fig = plt.figure(figsize=(width, height), facecolor=SURFACE)
    fig.text(0.01, 0.97, title, fontsize=15, fontweight="bold", color=TEXT, va="top")
    fig.text(0.01, 0.915, subtitle, fontsize=10.5, color=TEXT_2, va="top")
    return fig


# ── How the farm ran in September (simulation.py only models the farm as it runs now) ──

class SeptemberBattery(sim.Battery):
    """Batt First: solar charges the battery before the loads. With powers_ev, the battery
    also covers the truck after 7 PM, like the real inverter did."""
    powers_ev = False

    def dispatch(self, pv_kw, cooler_kw, ev_kw, hour, dt_hours=1.0 / 60.0, other_kw=0.0):
        max_discharge_kw, slot_soc = self.tou_slot(hour)
        site_kw = cooler_kw + other_kw
        site_surplus = pv_kw - site_kw
        floor = max(self.soc_min, slot_soc)
        can_discharge = max_discharge_kw > 0 and self.soc > floor + 1e-9
        battery_loads_kw = site_kw + (ev_kw if self.powers_ev else 0.0)
        if not (can_discharge and pv_kw < battery_loads_kw):
            # battery before every load, except when it may discharge and PV can't cover the
            # loads (e.g. dusk): then PV goes to the loads and the battery covers the rest
            pv_for_battery = max(0.0, pv_kw)
        else:
            pv_for_battery = max(0.0, site_surplus - ev_kw)
        pv_charge = min(pv_for_battery, self.charge_acceptance_kw(dt_hours))
        self.charge(pv_charge, dt_hours)
        surplus_kw = site_surplus - ev_kw - pv_charge
        grid_sell = max(0.0, surplus_kw)
        grid_buy = max(0.0, -surplus_kw)
        discharge_kw = 0.0
        if surplus_kw < 0 and pv_charge == 0.0:
            site_deficit = max(0.0, battery_loads_kw - pv_kw)
            available_kw = max(0.0, self._soc_to_kw(self.soc - floor, dt_hours) * sim.BATT_DISCHARGE_EFF)
            discharge_kw = min(site_deficit, max_discharge_kw, self.max_discharge_power, available_kw)
            self.discharge(discharge_kw, dt_hours)
            grid_buy -= discharge_kw
        return pv_charge - discharge_kw, grid_sell, grid_buy


class TaperEV(sim.SimEV):
    """Truck that slows down near full: over the last taper_kwh before the limit, power falls
    with the energy still needed, down to EV_TAPER_END_KW. 0 = no taper."""
    taper_kwh = 0.0

    def charge(self, power_kw=None, dt_hours=1.0 / 60.0):
        if self.taper_kwh > 0.0:
            remaining_kwh = max(0.0, self.target - self.soc) * self.capacity / self.eff
            if remaining_kwh < self.taper_kwh:
                taper_kw = max(EV_TAPER_END_KW, self.rate * remaining_kwh / self.taper_kwh)
                power_kw = min(self.rate if power_kw is None else power_kw, taper_kw)
        super().charge(power_kw, dt_hours)


def configure_farm(tuned: bool, battery_powers_ev: bool = False) -> None:
    # Batt First, the truck charged to 95% and made one Wednesday trip
    # (run_model sets the trip's energy from the measured charge)
    sim.Battery, sim.SimEV = SeptemberBattery, TaperEV
    sim.EV_SOC_TARGET = 0.95
    sim.EV_TRIP_WEEKDAYS = (2,)
    for name, value in (TUNED if tuned else ORIGINAL).items():
        setattr(sim, name, value)
    SeptemberBattery.powers_ev = tuned and battery_powers_ev
    TaperEV.taper_kwh = EV_TAPER_KWH if tuned and battery_powers_ev else 0.0


def load_real(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = pd.read_excel(path, header=5, engine="openpyxl")
    d["Time"] = pd.to_datetime(d["Time"])
    d = d.set_index("Time").sort_index()
    num = lambda col: pd.to_numeric(d[col], errors="coerce")
    last = lambda col: num(col).groupby(d.index.date).last()
    daily = pd.DataFrame({
        "grid buy": last("GridBuyToday(kWh)/76"),
        "grid sell": last("GridSellToday(kWh)/77"),
        "load": last("DailyUsed(kWh)/84"),
        "battery charge": last("BatChargeToday(kWh)/70"),
        "battery discharge": last("BatDischargeToday(kWh)/71"),
        "PV": last("Pvetoday(kWh)/108"),
    })
    battery = num("BatteryPower(W)/190")  # + discharging, − charging
    grid = num("AcTotalPower(W)/169")     # + buying, − selling
    power = pd.DataFrame({
        "grid import": grid.clip(lower=0) / 1000,
        "grid export": (-grid).clip(lower=0) / 1000,
        "battery charging": (-battery).clip(lower=0) / 1000,
        "battery discharging": battery.clip(lower=0) / 1000,
        "load": num("LoadTotalPower(W)/178") / 1000,
        "battery soc": num("BatteryEnergy(A·H)/184"),  # register 184 is the battery charge in %
    })
    return daily, power


def normal_days(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the EV charging day (Wed) and the day after, and any EV charging minutes."""
    return df[(~df.index.weekday.isin([2, 3])) & (df["load"] < 3)]


def model_daily(r: pd.DataFrame) -> pd.DataFrame:
    load = r["cooler_kw"] + r["other_load_kw"] + r["ev_kw"]
    per_minute = pd.DataFrame({
        "grid buy": r["grid_buy_kw"], "grid sell": r["grid_sell_kw"], "load": load,
        "battery charge": r["battery_kw"].clip(lower=0), "battery discharge": (-r["battery_kw"]).clip(lower=0),
        "PV": r["pv_kw"],
    })
    return per_minute.groupby(r.index.date).sum() / 60


def model_power(r: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "grid import": r["grid_buy_kw"], "grid export": r["grid_sell_kw"],
        "battery charging": r["battery_kw"].clip(lower=0),
        "battery discharging": (-r["battery_kw"]).clip(lower=0),
        "load": r["cooler_kw"] + r["other_load_kw"] + r["ev_kw"],
        "battery soc": r["battery_soc_percent"],
    }).set_axis(r.index.tz_localize(None))


def run_model(week: str) -> pd.DataFrame:
    """No EMS week with the EV plug-in time and energy matched to the inverter data."""
    end_hour, kwh = REAL_TRIPS[week]
    sim.EV_TRIP_END_HOUR = end_hour
    sim.EV_TRIP_SOC_USED = kwh * sim.EV_CHARGE_EFF / sim.EV_CAPACITY   # trip uses what was recharged
    inputs = sim.week_inputs(WEEKS[week], MOER, OUTDOOR)
    return sim.run_week(inputs, "without-ems")


def main() -> None:
    global OUT
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--battery-powers-ev", action="store_true",
                        help="Tuned model: battery covers the EV after 7 PM, like the real inverter")
    args = parser.parse_args()
    if args.battery_powers_ev:
        OUT = OUT / "battery_powers_ev"
        SOURCES["tuned"] = ("Tuned model, battery powers EV + EV taper", TUNED_COLOR)
    OUT.mkdir(parents=True, exist_ok=True)
    real = {week: load_real(path) for week, path in WEEKS.items()}

    runs = {}
    for name in ("original", "tuned"):
        configure_farm(tuned=name == "tuned", battery_powers_ev=args.battery_powers_ev)
        runs[name] = run_model(WEEK)
    configure_farm(tuned=False)

    real_daily, real_power = real[WEEK]
    daily = pd.concat({"real": real_daily, **{k: model_daily(r) for k, r in runs.items()}}, axis=1)
    daily.index = pd.to_datetime(daily.index)
    daily = daily.sort_index()
    daily.round(2).to_csv(OUT / "daily_comparison.csv")
    powers = {"real": real_power.sort_index(), **{k: model_power(r) for k, r in runs.items()}}

    # ── numbers ──
    metrics = ["grid buy", "grid sell", "load", "battery charge", "battery discharge", "PV"]
    print(f"Week of {WEEK}, weekly totals (kWh):   real  original     tuned"
          + ("  (tuned: battery powers EV)" if args.battery_powers_ev else ""))
    for m in metrics:
        r, a, b = (daily[(src, m)].sum() for src in SOURCES)
        print(f"    {m:<18}{r:12.1f}{a:10.1f} ({(a - r) / r * 100:+3.0f}%){b:8.1f} ({(b - r) / r * 100:+3.0f}%)")
    normal = daily[~daily.index.weekday.isin([2, 3])]
    print("\nNormal days (no EV): mean absolute daily error (kWh/day)   original   tuned   real average")
    for m in metrics:
        err = lambda src: (normal[(src, m)] - normal[("real", m)]).abs().mean()
        print(f"    {m:<18}{err('original'):39.2f}{err('tuned'):8.2f}{normal[('real', m)].mean():10.2f}")
    print("\nBattery reaches 15% each night (first time):")
    for day in pd.date_range(daily.index[0], daily.index[-1] - pd.Timedelta(days=1)):
        times = []
        for src in SOURCES:
            soc = powers[src]["battery soc"][day + pd.Timedelta(hours=19):day + pd.Timedelta(hours=33)]
            hit = soc[soc <= sim.BATT_SOC_MIN + 0.5]
            times.append(hit.index[0].strftime("%a %-I:%M %p") if len(hit) else "—")
        print(f"    night of {day:%a %-m/%-d}:  real {times[0]:<14} original {times[1]:<14} tuned {times[2]}")

    # ── V1: daily bars ──
    panels = [("load", "Load"), ("grid buy", "Grid buy"), ("grid sell", "Grid sell"),
              ("battery charge", "Battery charged"), ("battery discharge", "Battery discharged")]
    if args.battery_powers_ev:
        # battery charged doesn't depend on the battery powering the EV; dates under every panel
        panels.remove(("battery charge", "Battery charged"))
    fig = new_figure(16, 12.5, f"Model vs real inverter data, day by day (week of {WEEK})",
                        "Farm settings: No EMS, 1 battery, Batt First, battery charged from solar only · "
                        "real = Sol-Ark daily counters · EV charging matched to the real Wednesday")
    x = np.arange(len(daily))
    labels = [d.strftime("%a\n%-m/%-d") for d in daily.index]
    width = 0.27
    for i, (metric, title) in enumerate(panels):
        if args.battery_powers_ev:
            ax = fig.add_axes([0.05, 0.7 - i * 0.205, 0.93, 0.125])
        else:
            ax = fig.add_axes([0.05, 0.725 - i * 0.167, 0.93, 0.12])
        style(ax)
        for j, (src, (label, color)) in enumerate(SOURCES.items()):
            ax.bar(x + (j - 1) * width, daily[(src, metric)], width * 0.92, color=color, label=label)
        ax.set_title(title + " (kWh/day)", fontsize=11.5, color=TEXT, loc="left")
        show_dates = args.battery_powers_ev or i == len(panels) - 1
        ax.set_xticks(x, labels if show_dates else [""] * len(x), fontsize=9)
        if metric in ("grid buy", "load"):
            ax.set_yscale("symlog", linthresh=10)
            ax.set_yticks([0, 2, 5, 10, 50], ["0", "2", "5", "10", "50"])
        if i == 0:
            ax.legend(loc="lower right", bbox_to_anchor=(1.0, 1.02), frameon=False, fontsize=10,
                      labelcolor=TEXT, ncol=3)
    fig.text(0.05, 0.012, "Load and grid buy are on a stretched scale so the normal days stay visible next to the "
             "EV-charging Wednesday (≈40–60 kWh). Load = cooler + wifi/electronics + EV, without inverter losses.",
             fontsize=9, color=TEXT_2)
    fig.savefig(OUT / "V1_daily_real_vs_model.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)

    # ── V2: average hourly profile on normal days ──
    hourly = {src: normal_days(p).groupby(normal_days(p).index.hour).mean() for src, p in powers.items()}
    rows = [("load", "Load"), ("grid import", "Grid import"), ("grid export", "Grid export"),
            ("battery charging", "Battery charging"), ("battery discharging", "Battery discharging")]
    fig = new_figure(16, 11, "Model vs real: an average normal day, hour by hour",
                        f"Average power (kW) for each hour, week of {WEEK} without the EV day "
                        "(Wed/Thu excluded) · Farm settings: No EMS, 1 battery, Batt First")
    for i, (metric, title) in enumerate(rows):
        ax = fig.add_axes([0.05 + (i % 2) * 0.48, 0.62 - (i // 2) * 0.29, 0.44, 0.22])
        style(ax)
        for src, (label, color) in SOURCES.items():
            ax.plot(hourly[src].index, hourly[src][metric], color=color, linewidth=2.5 if src == "real" else 2,
                    marker="o", markersize=4, label=label)
        ax.set_title(f"{title} (kW)", fontsize=11.5, color=TEXT, loc="left")
        ax.set_xticks(range(0, 24, 3), [pd.Timestamp(2026, 1, 1, h).strftime("%-I %p") for h in range(0, 24, 3)])
        ax.set_ylim(0, None)
        if i == 0:
            ax.legend(loc="lower center", frameon=False, fontsize=10, labelcolor=TEXT, ncol=3)
    fig.savefig(OUT / "V2_hourly_profile.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)

    # ── V3: battery charge ──
    # Mon–Wed; with --battery-powers-ev through Thu morning to show the EV evening
    start = pd.Timestamp("2026-09-21 00:00")
    end = pd.Timestamp("2026-09-24 09:00" if args.battery_powers_ev else "2026-09-23 09:00")
    fig = new_figure(13, 5.4, f"Battery charge Mon–{end:%a}: model vs real inverter data",
                        "Farm settings: No EMS, 1 battery, Batt First · shaded = 9 AM–7 PM (battery held, no discharge)")
    ax = fig.add_axes([0.06, 0.12, 0.92, 0.68])
    style(ax)
    ax.grid(True, axis="x", color=GRID, linewidth=0.5)
    ax.set_xlim(start, end)
    for day in pd.date_range(start.normalize(), end, freq="D"):
        ax.axvspan(day + pd.Timedelta(hours=9), day + pd.Timedelta(hours=19), color=GRID, alpha=0.45, linewidth=0)
    ax.axhline(sim.BATT_SOC_MIN, color=NEUTRAL, linewidth=1, linestyle="--")
    ax.text(start + pd.Timedelta(hours=1), sim.BATT_SOC_MIN + 2, f"{sim.BATT_SOC_MIN}% minimum", fontsize=9, color=TEXT_2)
    for src, (label, color) in SOURCES.items():
        ax.plot(powers[src].index, powers[src]["battery soc"], color=color, linewidth=2.5 if src == "real" else 2,
                label=label)
    ax.set_ylabel("Battery charge (%)")
    ax.set_ylim(0, 105)
    ticks = pd.date_range(start, end, freq="6h")
    ax.set_xticks(ticks, [t.strftime("%a %-I %p") for t in ticks], fontsize=9)
    ax.legend(loc="lower center", ncol=3, frameon=True, facecolor=SURFACE, edgecolor=GRID,
              fontsize=9.5, labelcolor=TEXT)
    fig.savefig(OUT / "V3_battery_soc_real_vs_model.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)
    print(f"\nSaved to {OUT.resolve()}")


if __name__ == "__main__":
    main()
