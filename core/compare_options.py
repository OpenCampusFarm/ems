"""Compare the four charger / battery / Time of Use options and draw slide charts.

Runs every option (1–2 batteries × 12 A / 6 A lowest charger setting) for every strategy (No EMS / Binary / Staged) over both weeks and saves
to results/integrated_simulation/compare_options/:

  summary_all_options.csv     one row per week x option x strategy
  1_co2_heatmap.png           grid CO2 for every option and strategy (week of 9/20)
  2_path_to_best.png          step by step from today's setup to the best option
  3_grid_buy_breakdown.png    what the grid energy was used for
  4_battery_two_nights.png    battery charge, Mon–Wed, with Charge checked vs unchecked
  5_co2_vs_ev_ready.png       grid CO2 vs how long the EV takes to recharge

Usage:
    python core/compare_options.py
"""

import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap

import simulation as sim

warnings.filterwarnings("ignore", category=UserWarning)

DATA = Path("core/Data")
MOER = DATA / "watttime_MISO_DETROIT_2026-09-13_to_09-26_utc.csv"
OUTDOOR = DATA / "outdoor_temperatures_2026-09-12_to_09-27.csv"
WEEKS = {
    "9/20": DATA / "PV_2026-09-20_to_2026-09-27.xlsx",
    "9/13": DATA / "PV_2026-09-13_to_2026-09-20.xlsx",
}
SLIDE_WEEK = "9/20"
OUT = Path("results/integrated_simulation/compare_options")

# (option, charger, Time of Use Charge checked, battery modules, battery first)
# (option, charger, Time of Use Charge checked, battery modules, battery first, solar priority)
# The battery charges from excess solar only (Charge unchecked), as in the real inverter data.
OPTIONS = {
    1: ("openevse-5", False, 1, False, "load-first"),
    2: ("openevse-5", False, 2, False, "load-first"),
    3: ("openevse-5-6a", False, 1, False, "load-first"),
    4: ("openevse-5-6a", False, 2, False, "load-first"),
    5: ("openevse-5-6a", False, 1, False, "load-first"),   # same as 3, paired with 6
    6: ("openevse-5-6a", False, 1, False, "batt-first"),
}
STRATEGIES = {"without-ems": "No EMS", "with-ems": "Binary EMS", "staged-ems": "Staged EMS"}

# Reference palette (dataviz skill), validated for these three slots, light mode
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e4e3df"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
NEUTRAL = "#a8a7a2"
BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]


def option_label(number: int) -> str:
    charger, _, modules, _, priority = OPTIONS[number]
    levels = "Equally spaced (12 A lowest)" if charger == "openevse-5" else "Lowest at 6 A"
    label = f"{number}. {levels}, {modules} batt"
    if number >= 5:
        label += ", Load First" if priority == "load-first" else ", Batt First"
    return label


def configure(number: int) -> None:
    charger, grid_charge, modules, first, priority = OPTIONS[number]
    sim.SOLAR_PRIORITY = priority
    sim.CHARGER = charger
    sim.BATT_GRID_CHARGE = grid_charge
    sim.BATT_MODULES = modules
    sim.BATTERY_FIRST = first


def grid_breakdown(results: pd.DataFrame) -> dict:
    """Split grid purchases into what they powered.

    Solar that went into the battery isn't available to the loads, so this works for both
    Load First and Batt First.
    """
    site = results["cooler_kw"] + results["other_load_kw"]
    discharge = (-results["battery_kw"]).clip(lower=0)
    battery_grid = results["battery_grid_charge_kw"]
    battery_from_pv = results["battery_kw"].clip(lower=0) - battery_grid
    site_grid = (site - (results["pv_kw"] - battery_from_pv) - discharge).clip(lower=0)
    ev_grid = (results["grid_buy_kw"] - battery_grid - site_grid).clip(lower=0)
    return {
        "grid for EV kWh": ev_grid.sum() / 60,
        "grid for cooler + base kWh": site_grid.sum() / 60,
        "grid for battery kWh": battery_grid.sum() / 60,
    }


def ev_ready_hours(results: pd.DataFrame) -> float:
    """Hours from plugging back in after the Wednesday trip until the EV is at 95%."""
    away = results.index[~results["ev_plugged"].astype(bool)]
    back = away[-1] + pd.Timedelta(minutes=1)
    after = results.loc[back:]
    ready = after[after["ev_soc_percent"] >= sim.EV_SOC_TARGET * 100 - 1e-6]
    return (ready.index[0] - back).total_seconds() / 3600 if len(ready) else np.nan


def run_all() -> tuple[pd.DataFrame, dict]:
    rows = []
    soc_traces = {}
    for week, pv in WEEKS.items():
        inputs = sim.week_inputs(pv, MOER, OUTDOOR)
        for number in OPTIONS:
            configure(number)
            for controller, name in STRATEGIES.items():
                results = sim.run_week(inputs, controller)
                summary = sim.summarize_week(results, controller)
                rows.append({
                    "week": week,
                    "option": number,
                    "option label": option_label(number),
                    "strategy": name,
                    **{k: v for k, v in summary.items() if k not in ("controller", "start", "end")},
                    **grid_breakdown(results),
                    "EV ready hours after return": ev_ready_hours(results),
                })
                if week == SLIDE_WEEK and controller == "without-ems":
                    soc_traces[number] = results["battery_soc_percent"]
            print(f"week {week}: option {number} done")
    return pd.DataFrame(rows), soc_traces


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


def chart_heatmap(df: pd.DataFrame) -> None:
    week = df[df.week == SLIDE_WEEK]
    table = week.pivot(index="option label", columns="strategy", values="grid_emissions_lb")
    table = table.loc[[option_label(n) for n in OPTIONS], list(STRATEGIES.values())]
    fig = new_figure(10, 5.4, "Grid CO₂ for every option (lb per week)",
                     "Week of 9/20 · darker = more CO₂ · best cell outlined")
    ax = fig.add_axes([0.36, 0.05, 0.6, 0.7])
    cmap = LinearSegmentedColormap.from_list("blue", BLUE_RAMP)
    ax.imshow(table.values, cmap=cmap, aspect="auto",
              vmin=table.values.min() * 0.8, vmax=table.values.max())
    best = np.unravel_index(np.argmin(table.values), table.shape)
    for (i, j), value in np.ndenumerate(table.values):
        shade = (value - table.values.min() * 0.8) / (table.values.max() - table.values.min() * 0.8)
        ax.text(j, i, f"{value:.0f}", ha="center", va="center", fontsize=12,
                color="white" if shade > 0.55 else TEXT, fontweight="bold" if (i, j) == best else None)
    ax.add_patch(plt.Rectangle((best[1] - 0.47, best[0] - 0.45), 0.94, 0.9, fill=False,
                               edgecolor=SERIES[1], linewidth=3, zorder=5))
    ax.set_xticks(range(table.shape[1]), table.columns, fontsize=11, color=TEXT)
    ax.set_yticks(range(table.shape[0]), table.index, fontsize=10.5, color=TEXT)
    ax.xaxis.tick_top()
    ax.tick_params(length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_xticks(np.arange(-0.5, table.shape[1]), minor=True)
    ax.set_yticks(np.arange(-0.5, table.shape[0]), minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=3)
    ax.tick_params(which="minor", length=0)
    fig.savefig(OUT / "1_co2_heatmap.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


def chart_path_to_best(df: pd.DataFrame) -> None:
    week = df[df.week == SLIDE_WEEK].set_index(["option", "strategy"])["grid_emissions_lb"]
    steps = [
        ("Today's setup\n(opt 1, No EMS)", week[(1, "No EMS")]),
        ("Use Staged EMS\n(opt 1)", week[(1, "Staged EMS")]),
        ("Lowest setting\n6 A (opt 3)", week[(3, "Staged EMS")]),
        ("Add 2nd battery\n(opt 4)", week[(4, "Staged EMS")]),
    ]
    fig = new_figure(11, 5.6, "From today's setup to the best option: grid CO₂ per week",
                     f"Week of 9/20 · each bar is one change · total cut "
                     f"{steps[0][1] - steps[-1][1]:.0f} lb ({(1 - steps[-1][1] / steps[0][1]) * 100:.0f}%)")
    ax = fig.add_axes([0.07, 0.14, 0.9, 0.66])
    style(ax)
    labels = [s[0] for s in steps] + ["Best option"]
    for i, (label, value) in enumerate(steps):
        if i == 0:
            ax.bar(i, value, width=0.6, color=NEUTRAL)
            ax.text(i, value + 3, f"{value:.0f} lb", ha="center", color=TEXT, fontsize=11)
            continue
        previous = steps[i - 1][1]
        change = value - previous
        bottom, height = (value, -change) if change < 0 else (previous, change)
        ax.bar(i, height, bottom=bottom, width=0.6, color=SERIES[0] if change < 0 else SERIES[1])
        ax.plot([i - 1.3, i + 0.3], [previous, previous], color=TEXT_2, linewidth=0.8, linestyle=":")
        ax.text(i, max(value, previous) + 3, f"{change:+.0f} lb", ha="center", color=TEXT, fontsize=11)
    ax.bar(len(steps), steps[-1][1], width=0.6, color=NEUTRAL)
    ax.text(len(steps), steps[-1][1] + 3, f"{steps[-1][1]:.0f} lb", ha="center", color=TEXT, fontsize=11)
    ax.set_xticks(range(len(labels)), labels, fontsize=10, color=TEXT)
    ax.set_ylabel("Grid CO₂ (lb/week)")
    ax.set_ylim(0, steps[0][1] * 1.15)
    fig.savefig(OUT / "2_path_to_best.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


def chart_grid_breakdown(df: pd.DataFrame) -> None:
    week = df[df.week == SLIDE_WEEK]
    parts = [("grid for EV kWh", "EV charging"), ("grid for cooler + base kWh", "Cooler + base load")]
    fig = new_figure(14, 6.2, "What the grid energy was used for (kWh per week)",
                     "Week of 9/20 · options 1–6 · battery charged from excess solar only")
    for k, (controller, name) in enumerate(STRATEGIES.items()):
        ax = fig.add_axes([0.05 + k * 0.32, 0.13, 0.29, 0.65])
        style(ax)
        data = week[week.strategy == name].set_index("option").loc[list(OPTIONS)]
        bottom = np.zeros(len(data))
        for (column, label), color in zip(parts, SERIES):
            ax.bar(data.index, data[column], bottom=bottom, width=0.7, color=color,
                   edgecolor=SURFACE, linewidth=1.5, label=label)
            bottom += data[column].to_numpy()
        for x, total in zip(data.index, bottom):
            ax.text(x, total + 1.5, f"{total:.0f}", ha="center", fontsize=9, color=TEXT)
        ax.set_title(name, fontsize=12, color=TEXT, loc="left")
        ax.set_xticks(list(OPTIONS))
        ax.set_xlabel("Option")
        ax.set_ylim(0, week[[p[0] for p in parts]].sum(axis=1).max() * 1.12)
        if k == 0:
            ax.set_ylabel("Grid buy (kWh/week)")
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper right", bbox_to_anchor=(0.99, 0.99), frameon=False,
               fontsize=10, ncol=3, labelcolor=TEXT)
    fig.savefig(OUT / "3_grid_buy_breakdown.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


def chart_battery_nights(soc_traces: dict) -> None:
    start, end = pd.Timestamp("2026-09-21 00:00"), pd.Timestamp("2026-09-23 09:00")
    fig = new_figure(13, 5.4, "Battery charge Mon–Wed: one vs two batteries",
                     "No EMS · shaded = 9 AM–7 PM (battery held, no discharge) · the charger setting doesn't change these lines")
    ax = fig.add_axes([0.06, 0.12, 0.92, 0.68])
    style(ax)
    ax.grid(True, axis="x", color=GRID, linewidth=0.5)
    ax.set_xlim(start, end)
    for day in pd.date_range(start.normalize(), end, freq="D"):
        ax.axvspan(day + pd.Timedelta(hours=9), day + pd.Timedelta(hours=19), color=GRID, alpha=0.45, linewidth=0)
    for number, color in zip((1, 2), SERIES):
        trace = soc_traces[number]
        trace = trace[(trace.index.tz_localize(None) >= start) & (trace.index.tz_localize(None) <= end)]
        x = trace.index.tz_localize(None)
        ax.plot(x, trace, color=color, linewidth=2,
                label="1 battery" if OPTIONS[number][2] == 1 else f"{OPTIONS[number][2]} batteries")
    ax.set_ylabel("Battery charge (%)")
    ax.set_ylim(0, 105)
    ticks = pd.date_range(start, end, freq="6h")
    ax.set_xticks(ticks, [t.strftime("%a %-I %p") for t in ticks], fontsize=9)
    ax.legend(loc="lower center", ncol=4, frameon=True, facecolor=SURFACE, edgecolor=GRID,
              fontsize=9.5, labelcolor=TEXT)
    fig.savefig(OUT / "4_battery_two_nights.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


def chart_co2_vs_ready(df: pd.DataFrame) -> None:
    week = df[df.week == SLIDE_WEEK]
    fig = new_figure(11, 6, "Cleaner charging takes longer: grid CO₂ vs. time to recharge the EV",
                     "Week of 9/20 · each dot is one option and strategy · lower = cleaner, left = EV ready sooner")
    ax = fig.add_axes([0.08, 0.11, 0.66, 0.7])
    style(ax)
    ax.grid(True, axis="x", color=GRID, linewidth=0.8)
    for (controller, name), color in zip(STRATEGIES.items(), SERIES):
        data = week[week.strategy == name]
        ax.scatter(data["EV ready hours after return"], data["grid_emissions_lb"], s=70, color=color,
                   edgecolor=SURFACE, linewidth=1.5, label=name, zorder=3)
        points = data.assign(x=data["EV ready hours after return"].round(0),
                             y=data["grid_emissions_lb"].round(0))
        grouped = points.groupby(["x", "y"])["option"].apply(lambda o: ", ".join(map(str, sorted(o))))
        middle = points["EV ready hours after return"].median()
        last_y = {True: -1e9, False: -1e9}  # last labeled CO2 on the right / left side
        for (_, co2), label in sorted(grouped.items(), key=lambda kv: kv[0][1]):
            first = int(label.split(",")[0])
            hours = points.loc[points.option == first, "EV ready hours after return"].iloc[0]
            # label on the side the dot sits on, unless that side already has a label within 4 lb
            right = hours >= middle
            if co2 - last_y[right] < 4:
                right = not right
            last_y[right] = co2
            ax.annotate(f"opt {label}", (hours, co2), xytext=(8 if right else -8, 0),
                        textcoords="offset points", ha="left" if right else "right",
                        va="center", fontsize=9, color=TEXT)
    ax.set_xlabel("Hours from plugging in (Wed 4 PM) until the EV is back at 95%")
    ax.set_ylabel("Grid CO₂ (lb/week)")
    ax.set_ylim(0, None)
    ax.set_xlim(0, None)
    ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False, fontsize=10, labelcolor=TEXT)
    fig.savefig(OUT / "5_co2_vs_ev_ready.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    df, soc_traces = run_all()
    df.to_csv(OUT / "summary_all_options.csv", index=False)
    chart_heatmap(df)
    chart_path_to_best(df)
    chart_grid_breakdown(df)
    chart_battery_nights(soc_traces)
    chart_co2_vs_ready(df)
    print(f"Saved to {OUT.resolve()}")


if __name__ == "__main__":
    main()
