import argparse
from pathlib import Path

from matplotlib import pyplot as plt
import numpy as np
import pandas as pd

from simulation_ems import BATTERY_BEFORE_TRUCK, BINARY_AMPS, CONTROLLER_LABELS, CONTROLLERS, DEFAULT_MOER_CSV, DEFAULT_OUTDOOR_CSV, DEFAULT_WEEK_PV, EV_DEPARTURE_MIN, EV_SOC_TARGET, EV_TRIP_SOC_USED, FAN_INSTALLED, STAGED_AMPS, TMAX, TMIN, run_week, week_inputs


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
        "ev_charging_hours": float((results.ev_kw > 0).sum()/60),
        "ev_reserve_events": int((results.ev_reserve_override & ~results.ev_reserve_override.shift(fill_value=False)).sum()),
        "ev_reserve_kwh": float(results.ev_kw.where(results.ev_reserve_override, 0).sum()/60),
        "ev_min_soc_percent": float(results.ev_soc_percent.min()),
        "ev_departure_failures": int((departures < EV_DEPARTURE_MIN*100 - 1e-6).sum()),
        "fan_kwh": kwh("fan_kw"),
        "compressor_kwh": kwh("compressor_kw"),
        "ev_charger_starts": int((charging[1:] & ~charging[:-1]).sum() + charging[0]),
        "ev_soc_at_departures": " / ".join(f"{t:%a} {soc:.1f}%" for t, soc in departures.items()),
        "ev_lowest_departure_soc_percent": float(departures.min()),
        "ev_final_soc_percent": float(results["ev_soc_percent"].iloc[-1]),
        "cooler_min_f": float(results.cooler_temp_f.min()),
        "cooler_max_f": float(results.cooler_temp_f.max()),
        "cooler_below_40_minutes": int((results.cooler_temp_f < TMIN).sum()),
        "cooler_above_55_minutes": int((results.cooler_temp_f > TMAX).sum()),
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
                      output_dir: Path, show_plot: bool = False, compare_fan_power: bool = False) -> pd.DataFrame:
    """Run all three modes on one week; save CSVs and charts to output_dir/week-<date>/."""
    inputs = week_inputs(pv_xlsx, moer_csv, outdoor_csv)
    label = f"week-{inputs.index[0].date().isoformat()}" + ("_battery-first" if BATTERY_BEFORE_TRUCK else "")
    week_dir = output_dir / label
    week_dir.mkdir(parents=True, exist_ok=True)

    # Preserve older generated outputs without mixing them with the current five figures.
    import shutil
    from datetime import datetime
    old = [p for p in week_dir.iterdir() if p.is_file() and p.suffix in ('.png', '.csv', '.json')]
    if old:
        archive = week_dir / '_previous' / datetime.now().strftime('%Y%m%d_%H%M%S')
        archive.mkdir(parents=True, exist_ok=True)
        for p in old:
            shutil.move(str(p), archive / p.name)
    data_dir = week_dir / 'data'
    data_dir.mkdir(exist_ok=True)
    runs = {}
    summaries = []
    for controller in CONTROLLERS:
        results = run_week(inputs, controller)
        runs[controller] = results
        summaries.append(summarize_week(results, controller))
        results.to_csv(data_dir / f"{controller}_minutes.csv")
        from ev_demo_outputs import save_ev_outputs
        save_ev_outputs(results, controller, data_dir, charts=False)

    summary = pd.DataFrame(summaries)
    summary.to_csv(week_dir / "summary.csv", index=False)
    from integrated_demo_outputs import build_outputs
    comparison = {}
    if compare_fan_power:
        original = FAN_INSTALLED
        try:
            FAN_INSTALLED = False
            comparison = {c: run_week(inputs, c) for c in CONTROLLERS if c != "without-ems"}
            for c, r in comparison.items():
                r.to_csv(data_dir / f"{c}_fan_off_minutes.csv")
        finally:
            FAN_INSTALLED = original
    build_outputs(inputs, runs, summary, comparison, week_dir, label, str(outdoor_csv), show_plot)
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
    parser.add_argument("--output-dir", type=Path, default=Path("results/integrated_simulation"))
    parser.add_argument("--compare-fan-power", action="store_true")
    parser.add_argument("--show-plot", action="store_true")
    args = parser.parse_args()

    run_week_scenario(args.week_pv_xlsx, args.moer_csv, args.outdoor_csv,
                      args.output_dir, show_plot=args.show_plot, compare_fan_power=args.compare_fan_power)