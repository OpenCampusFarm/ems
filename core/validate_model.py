"""Compare the simulation with the real Sol-Ark inverter data, 9/13–9/26.

The simulation is set up the way the farm ran: No EMS (cooler fixed at 38 °F, EV charges as
soon as it is plugged in), one battery, Batt First solar priority, no grid charging of the
battery. The Wednesday trip is run two ways: as the model assumes (back at 4 PM at 20%) and
matched to what the inverter recorded (plug-in time and energy).

Saves to results/integrated_simulation/validation/:
  daily_comparison.csv          real vs simulated daily totals (inverter counters)
  V1_daily_real_vs_model.png    daily grid buy, grid sell, battery charge / discharge
  V2_hourly_profile.png         average day, hour by hour (EV days excluded)

Usage:
    python core/validate_model.py
"""

import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import compare_options as co
import simulation as sim

warnings.filterwarnings("ignore", category=UserWarning)

OUT = Path("results/integrated_simulation/validation")
REAL_TRIPS = {"9/13": (15 + 20 / 60, 88.6), "9/20": (16.0, 49.6)}  # plug-in hour, kWh charged
REAL_COLOR, MODEL_COLOR, MATCHED_COLOR = co.TEXT_2, co.SERIES[0], co.SERIES[1]


def configure_farm() -> None:
    sim.CHARGER = "openevse-5-6a"   # No EMS always charges at full power, so the levels don't matter
    sim.BATT_MODULES = 1
    sim.BATT_GRID_CHARGE = False
    sim.BATTERY_FIRST = False
    sim.SOLAR_PRIORITY = "batt-first"


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
    })
    return daily, power


def model_daily(r: pd.DataFrame) -> pd.DataFrame:
    load = r["cooler_kw"] + r["other_load_kw"] + r["ev_kw"]
    per_minute = pd.DataFrame({
        "grid buy": r["grid_buy_kw"], "grid sell": r["grid_sell_kw"], "load": load,
        "battery charge": r["battery_kw"].clip(lower=0), "battery discharge": (-r["battery_kw"]).clip(lower=0),
        "PV": r["pv_kw"],
    })
    daily = per_minute.groupby(r.index.date).sum() / 60
    return daily


def model_power(r: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "grid import": r["grid_buy_kw"], "grid export": r["grid_sell_kw"],
        "battery charging": r["battery_kw"].clip(lower=0),
        "battery discharging": (-r["battery_kw"]).clip(lower=0),
        "load": r["cooler_kw"] + r["other_load_kw"] + r["ev_kw"],
    }).set_axis(r.index.tz_localize(None))


def run_model(week: str, matched: bool) -> pd.DataFrame:
    saved = sim.EV_TRIP_END_HOUR, sim.EV_TRIP_RETURN_SOC
    if matched:
        end_hour, kwh = REAL_TRIPS[week]
        sim.EV_TRIP_END_HOUR = end_hour
        sim.EV_TRIP_RETURN_SOC = sim.EV_SOC_TARGET - kwh * sim.EV_CHARGE_EFF / sim.EV_CAPACITY
    try:
        inputs = sim.week_inputs(co.WEEKS[week], co.MOER, co.OUTDOOR)
        return sim.run_week(inputs, "without-ems")
    finally:
        sim.EV_TRIP_END_HOUR, sim.EV_TRIP_RETURN_SOC = saved


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    configure_farm()
    tables, real_power, model_powers = [], [], []
    for week, path in co.WEEKS.items():
        real_daily, power = load_real(path)
        real_power.append(power)
        assumed = model_daily(run_model(week, matched=False))
        matched_run = run_model(week, matched=True)
        matched = model_daily(matched_run)
        model_powers.append(model_power(matched_run))
        table = pd.concat({"real": real_daily, "model (EV as assumed)": assumed,
                           "model (EV matched)": matched}, axis=1)
        table.insert(0, ("", "week"), week)
        tables.append(table)
    daily = pd.concat(tables)
    daily.index = pd.to_datetime(daily.index)
    daily.round(2).to_csv(OUT / "daily_comparison.csv")

    # ── numbers ──
    metrics = ["grid buy", "grid sell", "load", "battery charge", "battery discharge", "PV"]
    print("Weekly totals (kWh): real | model, EV as assumed | model, EV matched")
    for week in co.WEEKS:
        w = daily[daily[("", "week")] == week]
        print(f"  week {week}")
        for m in metrics:
            real, a, b = (w[(src, m)].sum() for src in ("real", "model (EV as assumed)", "model (EV matched)"))
            print(f"    {m:<18}{real:8.1f}{a:10.1f}{b:10.1f}   ({(b - real) / real * 100:+.0f}% matched)")
    normal = daily[~daily.index.weekday.isin([2, 3])]  # no EV trip / recharge
    print("\nNormal days (no EV): mean absolute daily error, model (EV matched) vs real")
    for m in metrics:
        err = (normal[("model (EV matched)", m)] - normal[("real", m)]).abs().mean()
        print(f"    {m:<18}{err:5.2f} kWh/day  (real average {normal[('real', m)].mean():5.2f})")

    # ── V1: daily bars ──
    panels = [("grid buy", "Grid buy"), ("grid sell", "Grid sell"),
              ("battery charge", "Battery charged"), ("battery discharge", "Battery discharged")]
    fig = co.new_figure(16, 10.5, "Model vs real inverter data, day by day (9/13–9/26)",
                        "Farm settings: No EMS, 1 battery, Batt First, battery charged from solar only · "
                        "real = Sol-Ark daily counters · EV trip matched to the real Wednesdays")
    x = np.arange(len(daily))
    labels = [d.strftime("%a\n%-m/%-d") for d in daily.index]
    for i, (metric, title) in enumerate(panels):
        ax = fig.add_axes([0.05, 0.7 - i * 0.205, 0.93, 0.15])
        co.style(ax)
        ax.bar(x - 0.2, daily[("real", metric)], 0.38, color=REAL_COLOR, label="Real (inverter)")
        ax.bar(x + 0.2, daily[("model (EV matched)", metric)], 0.38, color=MODEL_COLOR, label="Model")
        ax.set_title(title + " (kWh/day)", fontsize=11.5, color=co.TEXT, loc="left")
        ax.set_xticks(x, labels if i == len(panels) - 1 else [""] * len(x), fontsize=9)
        if metric == "grid buy":
            ax.set_yscale("symlog", linthresh=10)
            ax.set_yticks([0, 2, 5, 10, 50, 100], ["0", "2", "5", "10", "50", "100"])
        if i == 0:
            ax.legend(loc="upper right", frameon=False, fontsize=10, labelcolor=co.TEXT, ncol=2)
    fig.text(0.05, 0.015, "Grid buy is on a stretched scale so the normal days (≈1–6 kWh) stay visible next to the "
             "EV-charging Wednesdays (≈40–90 kWh).", fontsize=9, color=co.TEXT_2)
    fig.savefig(OUT / "V1_daily_real_vs_model.png", dpi=160, facecolor=co.SURFACE)
    plt.close(fig)

    # ── V2: average hourly profile on normal days ──
    real_p = pd.concat(real_power)
    model_p = pd.concat(model_powers)
    keep = lambda df: df[(~df.index.weekday.isin([2, 3])) & (df["load"] < 3)]
    real_h = keep(real_p).groupby(keep(real_p).index.hour).mean()
    model_h = keep(model_p).groupby(keep(model_p).index.hour).mean()
    rows = [("grid import", "Grid import"), ("grid export", "Grid export"),
            ("battery charging", "Battery charging"), ("battery discharging", "Battery discharging")]
    fig = co.new_figure(16, 8, "Model vs real: an average normal day, hour by hour",
                        "Average power (kW) for each hour over the 10 days without the EV trip or recharge "
                        "(Wed/Thu excluded) · Farm settings: No EMS, 1 battery, Batt First")
    for i, (metric, title) in enumerate(rows):
        ax = fig.add_axes([0.05 + (i % 2) * 0.48, 0.47 - (i // 2) * 0.4, 0.44, 0.3])
        co.style(ax)
        ax.plot(real_h.index, real_h[metric], color=REAL_COLOR, linewidth=2.5, marker="o", markersize=4,
                label="Real (inverter)")
        ax.plot(model_h.index, model_h[metric], color=MODEL_COLOR, linewidth=2, marker="o", markersize=4,
                label="Model")
        ax.set_title(f"{title} (kW)", fontsize=11.5, color=co.TEXT, loc="left")
        ax.set_xticks(range(0, 24, 3), [pd.Timestamp(2026, 1, 1, h).strftime("%-I %p") for h in range(0, 24, 3)])
        ax.set_ylim(0, None)
        if i == 0:
            ax.legend(loc="upper right", frameon=False, fontsize=10, labelcolor=co.TEXT)
    fig.savefig(OUT / "V2_hourly_profile.png", dpi=160, facecolor=co.SURFACE)
    plt.close(fig)
    print(f"\nSaved to {OUT.resolve()}")


if __name__ == "__main__":
    main()
