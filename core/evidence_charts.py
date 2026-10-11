"""Evidence charts for the final-simulation notes.

Saves to results/integrated_simulation/evidence/:

  A_real_grid_charging_by_hour.png   real inverter data: battery charging from PV vs from the
                                     grid, by hour of day (9/13–9/26)
  B_real_morning_example.png         real inverter data: one morning, minute by minute
  C_levels_where_energy_goes.png     simulation: where solar and grid energy go with
                                     5 / 9 / every-amp charger levels, no priority vs battery first
  D_levels_one_day.png               simulation: Thursday after the trip, EV vs battery charging

Usage:
    python core/evidence_charts.py
"""

import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import compare_options as co
import simulation as sim

warnings.filterwarnings("ignore", category=UserWarning)

OUT = Path("results/integrated_simulation/evidence")
SURFACE, TEXT, TEXT_2, GRID = co.SURFACE, co.TEXT, co.TEXT_2, co.GRID
BLUE, ORANGE, AQUA, YELLOW = co.SERIES
NEUTRAL_DOT = co.NEUTRAL
REAL_FILES = [co.WEEKS["9/13"], co.WEEKS["9/20"]]
EV_THRESHOLD_W = 3000  # readings above this are EV charging


# ── Real inverter data ────────────────────────────────────────────────────────

def load_real() -> pd.DataFrame:
    frames = [pd.read_excel(path, header=5, engine="openpyxl") for path in REAL_FILES]
    d = pd.concat(frames)
    d["Time"] = pd.to_datetime(d["Time"])
    d = d.set_index("Time").sort_index()
    num = lambda col: pd.to_numeric(d[col], errors="coerce")
    pv = sum(num(f"Ppv{i}(W)/{185 + i}") for i in range(1, 5))
    load = num("LoadTotalPower(W)/178")
    charge = (-num("BatteryPower(W)/190")).clip(lower=0)  # negative BatteryPower = charging
    surplus = (pv - load).clip(lower=0)
    from_grid = (charge - surplus).clip(lower=0)
    from_grid[load > EV_THRESHOLD_W] = 0  # leave EV-charging readings out
    return pd.DataFrame({
        "pv_w": pv, "load_w": load, "charge_w": charge,
        "from_pv_w": charge - from_grid, "from_grid_w": from_grid,
        "grid_w": num("AcTotalPower(W)/169").clip(lower=0),  # exports shown as 0
    })


def chart_real_by_hour(real: pd.DataFrame) -> None:
    kwh = real[["from_pv_w", "from_grid_w"]] * 5 / 60 / 1000  # 5-minute readings
    by_hour = kwh.groupby(kwh.index.hour).sum().reindex(range(24), fill_value=0)
    total_grid = kwh["from_grid_w"].sum()
    total = kwh.sum().sum()
    morning = kwh.loc[(kwh.index.hour >= 7) & (kwh.index.hour < 13), "from_grid_w"].sum()

    fig = co.new_figure(12, 5.4, "Real data: battery charging by hour — blue = charged while the loads ran on the grid",
                        f"Sol-Ark inverter, 9/13–9/26 (2 weeks) · the grid never charged the battery directly; "
                        f"{total_grid:.1f} of {total:.1f} kWh of charging happened while solar didn't cover the loads, "
                        f"{morning / total_grid * 100:.0f}% of it 7 AM–1 PM")
    ax = fig.add_axes([0.07, 0.13, 0.9, 0.67])
    co.style(ax)
    for start, end, label in ((7, 9, "Batt 15%"), (9, 19, "Batt 100%, Charge ✓")):
        ax.axvspan(start - 0.5, end - 0.5, color=GRID, alpha=0.35 if start == 9 else 0.15, linewidth=0)
        ax.text((start + end) / 2 - 0.5, by_hour.sum(axis=1).max() * 1.08, label, ha="center",
                fontsize=9, color=TEXT_2)
    ax.bar(by_hour.index, by_hour["from_pv_w"], width=0.75, color=ORANGE, edgecolor=SURFACE,
           linewidth=1.5, label="Charged from excess PV")
    ax.bar(by_hour.index, by_hour["from_grid_w"], bottom=by_hour["from_pv_w"], width=0.75,
           color=BLUE, edgecolor=SURFACE, linewidth=1.5, label="Charged while the loads drew from the grid")
    for hour, row in by_hour.iterrows():
        if row["from_grid_w"] >= 0.5:
            ax.text(hour, row.sum() + 0.15, f"{row['from_grid_w']:.1f}", ha="center", fontsize=9,
                    color=BLUE, fontweight="bold")
    ax.set_xticks(range(0, 24, 2), [pd.Timestamp(2026, 1, 1, h).strftime("%-I %p") for h in range(0, 24, 2)])
    ax.set_xlim(-0.6, 23.6)
    ax.set_ylim(0, by_hour.sum(axis=1).max() * 1.18)
    ax.set_ylabel("Battery charging, 2 weeks (kWh)")
    ax.legend(loc="upper right", frameon=False, fontsize=10, labelcolor=TEXT)
    ax.text(0.01, -0.13, "Blue numbers = kWh of charging in that hour beyond the solar left after the loads "
            "(the loads took that much from the grid instead). 5-minute readings; EV charging excluded.",
            transform=ax.transAxes, fontsize=8.5, color=TEXT_2)
    fig.savefig(OUT / "A_real_grid_charging_by_hour.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


def chart_real_morning(real: pd.DataFrame) -> None:
    # the morning with the most grid charging
    mornings = real.between_time("06:00", "14:00")
    day = (mornings["from_grid_w"] * 5 / 60 / 1000).groupby(mornings.index.date).sum().idxmax()
    m = mornings.loc[str(day)] / 1000
    fig = co.new_figure(12, 5.4, f"Real data, {pd.Timestamp(day):%a %-m/%-d} morning: solar goes into the battery, the loads run on the grid",
                        "Sol-Ark inverter, 5-minute readings · battery charging tracks the solar; grid import "
                        "tracks the load · shaded = charging beyond the solar left after the loads")
    ax = fig.add_axes([0.07, 0.12, 0.9, 0.68])
    co.style(ax)
    ax.fill_between(m.index, 0, m["from_grid_w"], color=BLUE, alpha=0.18, linewidth=0, step="mid",
                    label="Charging beyond solar left after loads")
    lines = [("pv_w", "PV", ORANGE), ("load_w", "Load (cooler + base)", YELLOW),
             ("charge_w", "Battery charging", AQUA), ("grid_w", "Grid import (0 when exporting)", BLUE)]
    for column, label, color in lines:
        ax.plot(m.index, m[column], color=color, linewidth=2, label=label, drawstyle="steps-mid")
    ax.axvline(pd.Timestamp(day) + pd.Timedelta(hours=9), color=TEXT_2, linewidth=0.8, linestyle=":")
    ax.text(pd.Timestamp(day) + pd.Timedelta(hours=9, minutes=5), ax.get_ylim()[1] * 0.95,
            "9 AM: Batt target → 100%", fontsize=9, color=TEXT_2)
    ax.set_ylabel("Power (kW)")
    ax.set_ylim(0, None)
    ticks = pd.date_range(m.index[0].floor("h"), m.index[-1].ceil("h"), freq="1h")
    ax.set_xticks(ticks, [t.strftime("%-I %p") for t in ticks])
    ax.legend(loc="upper left", frameon=False, fontsize=9.5, labelcolor=TEXT)
    fig.savefig(OUT / "B_real_morning_example.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


def chart_batt_first(real: pd.DataFrame) -> None:
    """Real data vs the two Sol-Ark solar priority settings (manual p.33)."""
    loss = 120  # typical inverter loss, W (median of PV + grid + battery − load)
    fig = co.new_figure(16, 6.4, "Real data behaves like “Batt First”: solar charges the battery before covering the loads",
                        "Sol-Ark inverter, 9/13–9/26 · Batt First (manual default, p.33): solar → battery first · "
                        "Load First: solar → loads first, battery gets only what's left · setting itself not yet read off the inverter")
    # left: one real morning
    day = "2026-09-17"
    m = real.loc[f"{day} 07:30":f"{day} 13:00"] / 1000
    ax = fig.add_axes([0.05, 0.13, 0.42, 0.62])
    co.style(ax)
    for column, label, color, width in (("pv_w", "Solar", ORANGE, 2.5), ("charge_w", "Battery charging", AQUA, 1.5),
                                        ("load_w", "Load (cooler + other)", YELLOW, 2.5), ("grid_w", "Grid import", BLUE, 1.5)):
        ax.plot(m.index, m[column], color=color, linewidth=width, label=label, drawstyle="steps-mid")
    ax.set_title("Thu 9/17 morning: battery charging ≈ solar, grid import ≈ load", fontsize=11.5,
                 color=TEXT, loc="left")
    ax.set_ylabel("Power (kW)")
    ax.set_ylim(0, None)
    ticks = pd.date_range(m.index[0].ceil("h"), m.index[-1], freq="1h")
    ax.set_xticks(ticks, [t.strftime("%-I %p") for t in ticks])
    ax.legend(loc="upper left", frameon=False, fontsize=9.5, labelcolor=TEXT)
    # right: every charging reading vs the two predictions
    c = real[(real.load_w < EV_THRESHOLD_W) & (real.charge_w > 50) & (real.pv_w > 100)]
    ax = fig.add_axes([0.55, 0.13, 0.42, 0.62])
    co.style(ax)
    ax.grid(True, axis="x", color=GRID, linewidth=0.8)
    below = c.pv_w < c.load_w
    ax.scatter(c.pv_w[~below] / 1000, c.charge_w[~below] / 1000, s=12, color=NEUTRAL_DOT, alpha=0.6,
               linewidth=0, label="Battery charging, solar above the load")
    ax.scatter(c.pv_w[below] / 1000, c.charge_w[below] / 1000, s=16, color=BLUE, alpha=0.8,
               linewidth=0, label=f"Battery charging, solar BELOW the load ({below.sum()} readings, 13 of 14 days)")
    x = np.linspace(0, 3, 50)
    load = c.load_w.median() / 1000
    ax.plot(x, np.clip(x - loss / 1000, 0, None), color=ORANGE, linewidth=2.2, label="Batt First prediction (solar − losses)")
    ax.plot(x, np.clip(x - load - loss / 1000, 0, None), color=TEXT_2, linewidth=2, linestyle="--",
            label=f"Load First prediction (solar − load ≈ {load:.2f} kW − losses)")
    ax.set_xlim(0, 3)
    ax.set_ylim(0, 3)
    ax.set_xlabel("Solar (kW)")
    ax.set_ylabel("Battery charging (kW)")
    err_bf = (c.charge_w - (c.pv_w - loss).clip(lower=0)).abs().median()
    err_lf = (c.charge_w - (c.pv_w - c.load_w - loss).clip(lower=0)).abs().median()
    ax.set_title(f"All {len(c)} charging readings: typical error {err_bf:.0f} W (Batt First) vs "
                 f"{err_lf:.0f} W (Load First)", fontsize=11.5, color=TEXT, loc="left")
    ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=TEXT)
    fig.savefig(OUT / "E_real_batt_first.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


# ── Simulation: charger levels and priority ──────────────────────────────────

LEVEL_SETUPS = [
    ("5 settings", "openevse-5", False),
    ("9 levels", "openevse-9", False),
    ("Every amp (43)", "openevse-every-amp", False),
    ("9 levels + battery first", "openevse-9", True),
]


def energy_split(r: pd.DataFrame) -> dict:
    """Where the solar went, and what the grid paid for (kWh/week)."""
    k = lambda s: float(s.sum() / 60)
    site = r["cooler_kw"] + r["other_load_kw"]
    batt_from_grid = r["battery_grid_charge_kw"]
    batt_from_pv = r["battery_kw"].clip(lower=0) - batt_from_grid
    pv_left = (r["pv_kw"] - site).clip(lower=0)
    ev_from_pv = (pv_left - batt_from_pv - r["grid_sell_kw"]).clip(lower=0)
    night = (r.index.hour >= 19) | (r.index.hour < 9)
    discharge = (-r["battery_kw"]).clip(lower=0)
    loads_from_grid = (site - r["pv_kw"] - discharge).clip(lower=0)
    return {
        "Solar → EV": k(ev_from_pv),
        "Solar → battery": k(batt_from_pv),
        "Solar sold": k(r["grid_sell_kw"]),
        "Grid → battery": k(batt_from_grid),
        "Grid → cooler + base at night": k(loads_from_grid[night]),
        "Grid → EV": k((r["ev_kw"] - ev_from_pv).clip(lower=0)),
        "Grid CO₂ (lb)": float((r["grid_buy_kw"] * r["moer_lb_per_mwh"]).sum() / 60 / 1000),
        "EV back to 95%": sim.summarize_week(r, "staged-ems")["ev_back_to_target_at"],
    }


def run_levels(inputs: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    sim.BATT_MODULES = 1
    rows, runs = [], {}
    for grid_charge in (True, False):
        sim.BATT_GRID_CHARGE = grid_charge
        for label, charger, first in LEVEL_SETUPS:
            sim.CHARGER, sim.BATTERY_FIRST = charger, first
            r = sim.run_week(inputs, "staged-ems")
            runs[(grid_charge, label)] = r
            rows.append({"Charge": "✓ (grid allowed)" if grid_charge else "✗ (PV only)",
                         "setup": label, **energy_split(r)})
    sim.BATTERY_FIRST = False
    return pd.DataFrame(rows), runs


def chart_levels_energy(table: pd.DataFrame) -> None:
    fig = co.new_figure(15, 7.2, "Charger levels: where the solar and grid energy go",
                        "Simulation, Staged EMS, 1 battery, week of 9/20 · color = where the energy went "
                        "(EV / battery / cooler + base) · left: solar · middle: grid · right: grid CO₂")
    setups = [s[0] for s in LEVEL_SETUPS]
    for row, charge in enumerate(("✓ (grid allowed)", "✗ (PV only)")):
        data = table[table["Charge"] == charge].set_index("setup").loc[setups]
        y = np.arange(len(setups))[::-1]
        panels = [
            ([("Solar → EV", BLUE), ("Solar → battery", AQUA)], "Solar used (kWh/week)"),
            ([("Grid → EV", BLUE), ("Grid → battery", AQUA), ("Grid → cooler + base at night", ORANGE)],
             "Grid bought (kWh/week)"),
            ([("Grid CO₂ (lb)", BLUE)], "Grid CO₂ (lb/week)"),
        ]
        for col, (parts, xlabel) in enumerate(panels):
            ax = fig.add_axes([0.17 + col * 0.28, 0.5 - row * 0.4, 0.24, 0.3])
            co.style(ax)
            ax.grid(True, axis="x", color=GRID, linewidth=0.8)
            ax.grid(False, axis="y")
            height = 0.7 / len(parts)
            for i, (column, color) in enumerate(parts):
                offset = (i - (len(parts) - 1) / 2) * height
                bars = ax.barh(y + offset, data[column], height=height * 0.92, color=color,
                               edgecolor=SURFACE, linewidth=1, label=column)
                for bar, value in zip(bars, data[column]):
                    ax.text(bar.get_width() + 0.5, bar.get_y() + bar.get_height() / 2, f"{value:.1f}",
                            va="center", fontsize=8.5, color=TEXT)
            ax.set_yticks(y, setups if col == 0 else [""] * len(setups), fontsize=9.5)
            ax.set_xlim(0, table[[p[0] for p in parts]].to_numpy().max() * 1.25)
            if row == 1:
                ax.set_xlabel(xlabel)
            if col == 0:
                ax.set_title(f"Charge {charge}", fontsize=11.5, color=TEXT, loc="left", x=-0.5)
            if row == 0 and len(parts) > 1:
                ax.legend(loc="lower right", bbox_to_anchor=(1.0, 1.02), ncol=len(parts), frameon=False,
                          fontsize=8, labelcolor=TEXT, handlelength=1.2, columnspacing=0.8)
    fig.savefig(OUT / "C_levels_where_energy_goes.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


def chart_levels_day(runs: dict) -> None:
    day = pd.Timestamp("2026-09-24")
    setups = ["5 settings", "9 levels", "Every amp (43)", "9 levels + battery first"]
    fig = co.new_figure(15, 11, "Thursday after the trip: who gets the solar? (Charge ✗, PV only)",
                        "Simulation, Staged EMS, 1 battery, Thu 9/24 7 AM–7 PM · orange line before ~8 AM = "
                        "clean-grid trickle at the lowest charger setting")
    for i, label in enumerate(setups):
        r = runs[(False, label)]
        d = r[(r.index.tz_localize(None) >= day + pd.Timedelta(hours=7)) &
              (r.index.tz_localize(None) < day + pd.Timedelta(hours=19))]
        x = d.index.tz_localize(None)
        site = d["cooler_kw"] + d["other_load_kw"]
        pv_left = (d["pv_kw"] - site).clip(lower=0)
        batt_pv = d["battery_kw"].clip(lower=0) - d["battery_grid_charge_kw"]
        ev_pv = (pv_left - batt_pv - d["grid_sell_kw"]).clip(lower=0)
        ax = fig.add_axes([0.06, 0.7 - i * 0.215, 0.9, 0.155])
        co.style(ax)
        ax.fill_between(x, 0, ev_pv, color=BLUE, linewidth=0, step="mid", label="Solar → EV")
        ax.fill_between(x, ev_pv, ev_pv + batt_pv, color=AQUA, linewidth=0, step="mid",
                        label="Solar → battery")
        ax.plot(x, pv_left, color=TEXT, linewidth=1, label="Solar left after cooler + base",
                drawstyle="steps-mid")
        ax.step(x, d["ev_kw"], where="mid", color=ORANGE, linewidth=1.5, label="EV charger setting")
        soc_7pm = d["battery_soc_percent"].iloc[-1]
        batt_kwh = batt_pv.sum() / 60
        ax.set_title(f"{label}: battery got {batt_kwh:.1f} kWh of solar, at {soc_7pm:.0f}% by 7 PM",
                     fontsize=11.5, color=TEXT, loc="left")
        ax.set_ylabel("kW")
        ax.set_xlim(x[0], x[-1])
        ticks = pd.date_range(x[0], x[-1], freq="2h")
        ax.set_xticks(ticks, [t.strftime("%-I %p") for t in ticks] if i == len(setups) - 1 else [""] * len(ticks))
        if i == 0:
            handles, labels = ax.get_legend_handles_labels()
            fig.legend(handles, labels, loc="upper left", bbox_to_anchor=(0.06, 0.905), ncol=4,
                       frameon=False, fontsize=9.5, labelcolor=TEXT)
    fig.savefig(OUT / "D_levels_one_day.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    real = load_real()
    chart_real_by_hour(real)
    chart_real_morning(real)
    chart_batt_first(real)

    inputs = sim.week_inputs(co.WEEKS["9/20"], co.MOER, co.OUTDOOR)
    table, runs = run_levels(inputs)
    table.to_csv(OUT / "levels_energy_split.csv", index=False)
    print(table.round(1).to_string(index=False))
    chart_levels_energy(table)
    chart_levels_day(runs)
    print(f"Saved to {OUT.resolve()}")


if __name__ == "__main__":
    main()
