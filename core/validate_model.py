"""Compare the simulation with the real Sol-Ark inverter data, week of 9/20–9/26.

The simulation is set up the way the farm ran: No EMS (cooler fixed at 38 °F, EV charges as
soon as it is plugged in), one battery, Batt First solar priority, no grid charging of the
battery. The Wednesday EV charge starts when the inverter shows it starting and takes the
energy the inverter recorded.

Two versions of the model are compared with the real data:
  original  the model as used in the option comparisons
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
  V3_battery_soc_real_vs_model.png  battery charge Mon–Wed 9/21–9/23, same window as compare chart 4

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

import compare_options as co
import simulation as sim

warnings.filterwarnings("ignore", category=UserWarning)

OUT = Path("results/integrated_simulation/validation")
WEEK = "9/20"
# EV charging seen on the inverter: start hour, kWh (load above the hour before it, summed)
REAL_TRIPS = {"9/13": (15 + 20 / 60, 80.2), "9/20": (16.0, 43.6)}
REAL_COLOR, ORIGINAL_COLOR, TUNED_COLOR = co.TEXT_2, co.NEUTRAL, co.SERIES[0]
SOURCES = {"real": ("Real (inverter)", REAL_COLOR),
           "original": ("Original model", ORIGINAL_COLOR),
           "tuned": ("Tuned model", TUNED_COLOR)}

# Only with --battery-powers-ev: the car slows down near full, so it is still charging after
# 7 PM when the battery may discharge. Measured on 9/16 and 9/23: ~5.7 kWh over ~75–80 min.
EV_TAPER_KWH = 5.7
DEFAULTS = {name: getattr(sim, name) for name in ["BATT_POWERS_EV", "EV_TAPER_KWH"]}


def configure_farm(tuned: bool, battery_powers_ev: bool = False) -> None:
    sim.CHARGER = "openevse-5-6a"   # No EMS always charges at full power, so the levels don't matter
    sim.BATT_MODULES = 1
    sim.BATT_GRID_CHARGE = False
    sim.BATTERY_FIRST = False
    sim.SOLAR_PRIORITY = "batt-first"
    for name, value in DEFAULTS.items():
        setattr(sim, name, value)
    sim.use_tuned_model(tuned)   # values and where they come from: simulation.TUNED_SETTINGS
    if tuned:
        sim.BATT_POWERS_EV = battery_powers_ev
        if battery_powers_ev:
            sim.EV_TAPER_KWH = EV_TAPER_KWH


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
    saved = sim.EV_TRIP_END_HOUR, sim.EV_TRIP_RETURN_SOC
    end_hour, kwh = REAL_TRIPS[week]
    sim.EV_TRIP_END_HOUR = end_hour
    sim.EV_TRIP_RETURN_SOC = sim.EV_SOC_TARGET - kwh * sim.EV_CHARGE_EFF / sim.EV_CAPACITY
    try:
        inputs = sim.week_inputs(co.WEEKS[week], co.MOER, co.OUTDOOR)
        return sim.run_week(inputs, "without-ems")
    finally:
        sim.EV_TRIP_END_HOUR, sim.EV_TRIP_RETURN_SOC = saved


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
    real = {week: load_real(path) for week, path in co.WEEKS.items()}

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
    fig = co.new_figure(16, 12.5, f"Model vs real inverter data, day by day (week of {WEEK})",
                        "Farm settings: No EMS, 1 battery, Batt First, battery charged from solar only · "
                        "real = Sol-Ark daily counters · EV charging matched to the real Wednesday")
    x = np.arange(len(daily))
    labels = [d.strftime("%a\n%-m/%-d") for d in daily.index]
    width = 0.27
    for i, (metric, title) in enumerate(panels):
        ax = fig.add_axes([0.05, 0.75 - i * 0.172, 0.93, 0.125])
        co.style(ax)
        for j, (src, (label, color)) in enumerate(SOURCES.items()):
            ax.bar(x + (j - 1) * width, daily[(src, metric)], width * 0.92, color=color, label=label)
        ax.set_title(title + " (kWh/day)", fontsize=11.5, color=co.TEXT, loc="left")
        ax.set_xticks(x, labels if i == len(panels) - 1 else [""] * len(x), fontsize=9)
        if metric in ("grid buy", "load"):
            ax.set_yscale("symlog", linthresh=10)
            ax.set_yticks([0, 2, 5, 10, 50], ["0", "2", "5", "10", "50"])
        if i == 0:
            ax.legend(loc="upper right", frameon=False, fontsize=10, labelcolor=co.TEXT, ncol=3)
    fig.text(0.05, 0.012, "Load and grid buy are on a stretched scale so the normal days stay visible next to the "
             "EV-charging Wednesday (≈40–60 kWh). Load = cooler + wifi/electronics + EV, without inverter losses.",
             fontsize=9, color=co.TEXT_2)
    fig.savefig(OUT / "V1_daily_real_vs_model.png", dpi=160, facecolor=co.SURFACE)
    plt.close(fig)

    # ── V2: average hourly profile on normal days ──
    hourly = {src: normal_days(p).groupby(normal_days(p).index.hour).mean() for src, p in powers.items()}
    rows = [("load", "Load"), ("grid import", "Grid import"), ("grid export", "Grid export"),
            ("battery charging", "Battery charging"), ("battery discharging", "Battery discharging")]
    fig = co.new_figure(16, 11, "Model vs real: an average normal day, hour by hour",
                        f"Average power (kW) for each hour, week of {WEEK} without the EV day "
                        "(Wed/Thu excluded) · Farm settings: No EMS, 1 battery, Batt First")
    for i, (metric, title) in enumerate(rows):
        ax = fig.add_axes([0.05 + (i % 2) * 0.48, 0.62 - (i // 2) * 0.29, 0.44, 0.22])
        co.style(ax)
        for src, (label, color) in SOURCES.items():
            ax.plot(hourly[src].index, hourly[src][metric], color=color, linewidth=2.5 if src == "real" else 2,
                    marker="o", markersize=4, label=label)
        ax.set_title(f"{title} (kW)", fontsize=11.5, color=co.TEXT, loc="left")
        ax.set_xticks(range(0, 24, 3), [pd.Timestamp(2026, 1, 1, h).strftime("%-I %p") for h in range(0, 24, 3)])
        ax.set_ylim(0, None)
        if i == 0:
            ax.legend(loc="lower center", frameon=False, fontsize=10, labelcolor=co.TEXT, ncol=3)
    fig.savefig(OUT / "V2_hourly_profile.png", dpi=160, facecolor=co.SURFACE)
    plt.close(fig)

    # ── V3: battery charge, same window as compare_options chart 4 ──
    # Mon–Wed like compare chart 4; with --battery-powers-ev through Thu morning to show the EV evening
    start = pd.Timestamp("2026-09-21 00:00")
    end = pd.Timestamp("2026-09-24 09:00" if args.battery_powers_ev else "2026-09-23 09:00")
    fig = co.new_figure(13, 5.4, f"Battery charge Mon–{end:%a}: model vs real inverter data",
                        "Farm settings: No EMS, 1 battery, Batt First · shaded = 9 AM–7 PM (battery held, no discharge)")
    ax = fig.add_axes([0.06, 0.12, 0.92, 0.68])
    co.style(ax)
    ax.grid(True, axis="x", color=co.GRID, linewidth=0.5)
    ax.set_xlim(start, end)
    for day in pd.date_range(start.normalize(), end, freq="D"):
        ax.axvspan(day + pd.Timedelta(hours=9), day + pd.Timedelta(hours=19), color=co.GRID, alpha=0.45, linewidth=0)
    ax.axhline(sim.BATT_SOC_MIN, color=co.NEUTRAL, linewidth=1, linestyle="--")
    ax.text(start + pd.Timedelta(hours=1), sim.BATT_SOC_MIN + 2, f"{sim.BATT_SOC_MIN}% minimum", fontsize=9, color=co.TEXT_2)
    for src, (label, color) in SOURCES.items():
        ax.plot(powers[src].index, powers[src]["battery soc"], color=color, linewidth=2.5 if src == "real" else 2,
                label=label)
    ax.set_ylabel("Battery charge (%)")
    ax.set_ylim(0, 105)
    ticks = pd.date_range(start, end, freq="6h")
    ax.set_xticks(ticks, [t.strftime("%a %-I %p") for t in ticks], fontsize=9)
    ax.legend(loc="lower center", ncol=3, frameon=True, facecolor=co.SURFACE, edgecolor=co.GRID,
              fontsize=9.5, labelcolor=co.TEXT)
    fig.savefig(OUT / "V3_battery_soc_real_vs_model.png", dpi=160, facecolor=co.SURFACE)
    plt.close(fig)
    print(f"\nSaved to {OUT.resolve()}")


if __name__ == "__main__":
    main()
