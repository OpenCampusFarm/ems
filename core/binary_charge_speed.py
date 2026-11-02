"""Find the slowest Binary charging current that still keeps up with the truck's real use.

Actual use modelled here (differs from simulation.py's Wednesday trip to 20% SOC):
  - Deliveries Tuesday and Thursday, each uses EV_TRIP_SOC of the truck's battery
  - Truck is unplugged during a delivery (TRIP_START_HOUR to TRIP_END_HOUR)
  - One fixed charging current, on or off (Binary), no charging on clean night-time grid
  - EMS decides every DECISION_MINUTES, like the real loop, and holds until the next decision
  - Inverter on Load First, 1 battery, battery charged from solar only, tuned model
  - Cooler keeps the real EMS setpoints (45/50 °F) and clean/dirty rule

Reported for each current and rule: SOC before each Tuesday delivery, and whether the truck
keeps up over time (no SOC lost from one loop of the data to the next). The two September
weeks repeat as a loop; the first pass is warm-up and only the second counts.

Usage:
    python core/binary_charge_speed.py
    python core/binary_charge_speed.py --pv-xlsx <one week> --outdoor-csv <csv> --name october
        (a Sunday-to-Saturday week, looped; MOER comes from the week of 9/20, so it only
        steers the cooler setpoint and CO2, not a PV-only truck rule)
    python core/binary_charge_speed.py --weather-test
        (real September days repeated all week: is the truck full again one week later?)
Outputs: results/integrated_simulation/binary_charge_speed/
"""

import argparse
from multiprocessing import Pool
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import simulation as sim

DATA = Path("core/Data")
MOER = DATA / "watttime_MISO_DETROIT_2026-09-13_to_09-26_utc.csv"
OUTDOOR = DATA / "outdoor_temperatures_2026-09-12_to_09-27.csv"
PV_WEEKS = (DATA / "PV_2026-09-13_to_2026-09-20.xlsx", DATA / "PV_2026-09-20_to_2026-09-27.xlsx")
OUT = Path("results/integrated_simulation/binary_charge_speed")

EV_TRIP_SOC = 0.10          # SOC used per delivery
TRIP_WEEKDAYS = (1, 3)      # Tuesday, Thursday (Monday = 0)
TRIP_START_HOUR = 9         # assumed, same window as simulation.py
TRIP_END_HOUR = 16
EV_SOC_START = 0.95         # also the charging target
EV_RESUME_MARGIN = 0.01     # only restart charging this far below target
DECISION_MINUTES = 5        # real EMS POLL_INTERVAL
FARM_SETPOINT_CLEAN = 45    # °F, core/real_time_ems.py SETPOINT_COOLTH
FARM_SETPOINT_DIRTY = 50    # °F, core/real_time_ems.py SETPOINT_ECON

AMPS = (6, 8, 10, 12, 16, 20, 24, 32, 40, 47)
PV_THRESHOLDS_KW = (0.5, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0)   # 0.5 kW is today's threshold
RULES = {
    **{f"pv-{kw}kw": f"PV at least {kw} kW" for kw in PV_THRESHOLDS_KW},
    "excess-3kw": "PV left after site loads is at least 3 kW",
    "covers-charger": "PV left after site loads covers the charger",
    "after-battery": "PV left after site loads and battery covers the charger",
}
DAYS = {
    "every-day": (0, 1, 2, 3, 4, 5, 6),
    "fri-mon": (4, 5, 6, 0),            # no charging from Tuesday through Thursday
}
# name: (PV multiplier, battery may power the EV)
CASES = {
    "september": (1.0, False),
    "september-batt-to-ev": (1.0, True),
    "low-pv-40pct": (0.4, False),       # rough late-fall stress test, not measured data
}
WEEK_MINUTES = 7 * 24 * 60

# name: real days repeated through the week
WEATHER = {
    "Partly sunny (9/22 every day)": ("2026-09-22",),
    "Cloudy (9/19, 9/20, 9/21 repeating)": ("2026-09-19", "2026-09-20", "2026-09-21"),
}


def load_inputs() -> pd.DataFrame:
    frames = [sim.week_inputs(path, MOER, OUTDOOR) for path in PV_WEEKS]
    inputs = pd.concat(frames)
    inputs = inputs[~inputs.index.duplicated()]
    if len(inputs) != 2 * WEEK_MINUTES:
        raise ValueError(f"expected {2 * WEEK_MINUTES} minutes of data, got {len(inputs)}")
    return inputs[["pv_kw", "moer_lb_per_mwh", "outdoor_f"]]


def load_week(pv_xlsx: Path, outdoor_csv: Path) -> pd.DataFrame:
    """One Sunday-to-Saturday week of PV and temperature, with the MOER of the week of 9/20."""
    pv = sim.load_solark_pv_range(pv_xlsx)
    if len(pv) != WEEK_MINUTES or pv.index[0].weekday() != 6:
        raise ValueError(f"{pv_xlsx} must be one Sunday-to-Saturday week")
    september = sim.week_inputs(PV_WEEKS[1], MOER, OUTDOOR)
    return pd.DataFrame(
        {
            "pv_kw": pv.to_numpy(),
            "moer_lb_per_mwh": september["moer_lb_per_mwh"].to_numpy(),
            "outdoor_f": sim.load_outdoor_range(outdoor_csv, pv.index).to_numpy(),
        },
        index=pv.index,
    )


def repeat_days(source: pd.DataFrame, days: tuple[str, ...]) -> pd.DataFrame:
    """One Sunday-to-Saturday week built by repeating real days in order."""
    index = pd.date_range("2026-09-13", periods=WEEK_MINUTES, freq="1min", tz=source.index.tz)
    picked = [source.loc[days[i % len(days)]] for i in range(7)]
    week = pd.concat(picked)
    if len(week) != WEEK_MINUTES:
        raise ValueError(f"{days} don't make a full week")
    return week.set_axis(index)


def run(job: tuple) -> dict:
    inputs, case, pv_scale, batt_powers_ev, amps, rule, days = job
    pass_minutes = len(inputs)
    passes = 2 if pass_minutes > WEEK_MINUTES else 4
    sim.use_tuned_model(True)
    sim.SOLAR_PRIORITY = "load-first"
    sim.BATT_POWERS_EV = batt_powers_ev
    sim.SETPOINT_COOLTH = FARM_SETPOINT_CLEAN
    sim.SETPOINT_ECON = FARM_SETPOINT_DIRTY

    cooler = sim.Cooler(electric_kw=sim.COOLER_ELECTRIC_KW,
                        electric_kw_per_f=sim.COOLER_ELECTRIC_KW_PER_F,
                        electric_ref_f=sim.COOLER_ELECTRIC_REF_F)
    ev = sim.SimEV(soc_init=EV_SOC_START, charge_rate_kw=sim.EV_CHARGER_KW)
    ev.target = EV_SOC_START
    battery = sim.Battery(soc_init=50, modules=1)
    cooler.ambient = float(inputs["outdoor_f"].iloc[0])

    charger_kw = min(amps * sim.OPENEVSE_VOLTS / 1000, ev.rate)
    allowed_days = DAYS[days] if days else ()
    trip_drop_per_min = EV_TRIP_SOC / ((TRIP_END_HOUR - TRIP_START_HOUR) * 60)
    wants_charge, rule_on = False, False

    pv_in = inputs["pv_kw"].to_numpy() * pv_scale
    moer_in = inputs["moer_lb_per_mwh"].to_numpy()
    outdoor_in = inputs["outdoor_f"].to_numpy()
    hours = inputs.index.hour.to_numpy()
    weekdays = inputs.index.weekday.to_numpy()
    minutes_of_hour = inputs.index.minute.to_numpy()

    count = passes * pass_minutes
    grid_buy, grid_sell, ev_kw, moer_out, soc_out = (np.zeros(count) for _ in range(5))
    tuesday_soc: list[tuple[int, float]] = []

    for step in range(count):
        i = step % pass_minutes
        pv_kw, moer, hour, weekday = pv_in[i], moer_in[i], hours[i], weekdays[i]
        pv_ac_kw = pv_kw * (1.0 - sim.INVERTER_PV_LOSS)
        standby_kw = (sim.INVERTER_STANDBY_DAY_KW if pv_kw > sim.PV_STANDBY_THRESHOLD_KW
                      else sim.INVERTER_STANDBY_KW)
        other_kw = sim.OTHER_LOAD_KW

        cooler.change_setpoint(sim.ems_setpoint(pv_kw, moer))
        cooler.update(outdoor_f=float(outdoor_in[i]))

        away = weekday in TRIP_WEEKDAYS and TRIP_START_HOUR <= hour < TRIP_END_HOUR
        if weekday == 1 and hour == TRIP_START_HOUR and minutes_of_hour[i] == 0:
            tuesday_soc.append((step, ev.soc))

        if away:
            ev.soc -= trip_drop_per_min
            ev.charging = False
            ev.input_power_kw = 0.0
        else:
            if ev.soc <= ev.target - EV_RESUME_MARGIN:
                wants_charge = True
            elif ev.soc >= ev.target - 1e-9:
                wants_charge = False

            if step % DECISION_MINUTES == 0:
                surplus_kw = pv_ac_kw - cooler.instant_power_kw - other_kw - standby_kw
                if rule.startswith("pv-"):
                    rule_on = pv_kw >= float(rule[3:-2])
                elif rule == "excess-3kw":
                    rule_on = surplus_kw >= 3.0
                elif rule == "covers-charger":
                    rule_on = surplus_kw >= charger_kw
                elif rule == "after-battery":
                    rule_on = surplus_kw - battery.charge_acceptance_kw() >= charger_kw

            if amps > 0 and wants_charge and rule_on and weekday in allowed_days:
                ev.charge(charger_kw)
            else:
                ev.idle()

        _, sell, buy, _ = battery.dispatch(
            pv_ac_kw, cooler.instant_power_kw, ev.input_power_kw, int(hour),
            other_kw=other_kw + standby_kw,
        )
        grid_buy[step], grid_sell[step] = buy, sell
        ev_kw[step], moer_out[step], soc_out[step] = ev.input_power_kw, moer, ev.soc

    # second pass only, per week
    last = np.arange(count) >= pass_minutes
    weeks = pass_minutes / WEEK_MINUTES * (passes - 1)
    per_week = lambda series: float(series[last].sum() / 60.0 / weeks)
    starts = np.diff((ev_kw[last] > 0).astype(int)) == 1
    every_tuesday = [soc * 100.0 for _, soc in tuesday_soc]
    tuesdays = every_tuesday[int(pass_minutes / WEEK_MINUTES):]
    return {
        "case": case, "amps": amps, "charger_kw": charger_kw, "rule": rule, "days": days,
        "soc_tue_week1": tuesdays[0],
        "soc_tue_week2": tuesdays[1],
        "lowest_tuesday_soc": min(tuesdays),
        "back_every_tuesday": min(tuesdays) >= (EV_SOC_START - EV_RESUME_MARGIN) * 100.0 - 1e-6,
        # same data, one loop later: a truck that keeps up doesn't end lower than it did
        "soc_tue_latest": every_tuesday[-1],
        "soc_after_1_week": every_tuesday[1],
        "keeps_up": every_tuesday[-1] >= every_tuesday[-1 - int(pass_minutes / WEEK_MINUTES)] - 0.5,
        "ev_soc_end": float(soc_out[-1] * 100.0),
        "ev_kwh_week": per_week(ev_kw),
        "grid_buy_kwh_week": per_week(grid_buy),
        "grid_sell_kwh_week": per_week(grid_sell),
        "co2_lb_week": per_week(grid_buy * moer_out) / 1000.0,
        "charge_hours_week": float((ev_kw[last] > 0).sum() / 60.0 / weeks),
        "starts_week": float(starts.sum() / weeks),
    }


def plot(table: pd.DataFrame) -> None:
    """SOC before the worst Tuesday and added grid energy vs current, charging Fri-Mon."""
    surface, text, muted, grid = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
    series = {
        "pv-0.5kw": ("PV ≥ 0.5 kW (today)", "#2a78d6"),
        "pv-3.0kw": ("PV ≥ 3 kW", "#eb6834"),
        "covers-charger": ("Surplus covers charger", "#1baf7a"),
    }
    rows = {"september": "September PV", "low-pv-40pct": "PV × 0.4"}
    fig, axes = plt.subplots(2, 2, figsize=(11, 7), sharex=True, facecolor=surface)
    for row, (case, case_label) in enumerate(rows.items()):
        data = table[(table["case"] == case) & (table["days"] == "fri-mon")]
        for col, column in enumerate(("lowest_tuesday_soc", "added_grid_buy_kwh_week")):
            ax = axes[row, col]
            ax.set_facecolor(surface)
            for rule, (label, color) in series.items():
                line = data[data["rule"] == rule].sort_values("amps")
                ax.plot(line["amps"], line[column], color=color, linewidth=2, marker="o",
                        markersize=5, label=label)
            ax.grid(axis="y", color=grid, linewidth=0.8)
            ax.spines[["top", "right"]].set_visible(False)
            ax.spines[["left", "bottom"]].set_color(grid)
            ax.tick_params(colors=muted)
            ax.set_ylabel("Truck SOC, %" if col == 0 else "kWh per week", color=muted)
            title = "Lowest SOC before a Tuesday delivery" if col == 0 else "Grid energy added by the truck"
            ax.set_title(f"{case_label}: {title}", loc="left", color=text, fontsize=11)
            if col == 0:
                ax.axhline(94, color=muted, linewidth=1, linestyle=":")
                ax.set_ylim(0, 100)
            else:
                ax.set_ylim(bottom=0)
    for ax in axes[1]:
        ax.set_xlabel("Charging current, A", color=muted)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=3, frameon=False,
               labelcolor=text, bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(OUT / "charging_speed.png", dpi=160)
    plt.close(fig)


def plot_weather(table: pd.DataFrame, base: pd.DataFrame, out: Path) -> None:
    """SOC one week after starting full, and grid bought, against charging current."""
    surface, text, muted, grid = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
    series = {
        "excess-3kw": ("Excess PV ≥ 3 kW", "#eb6834"),
        "covers-charger": ("Excess PV ≥ charger power", "#1baf7a"),
    }
    fig, axes = plt.subplots(len(WEATHER), 2, figsize=(11, 7), sharex=True, facecolor=surface)
    for row, name in enumerate(WEATHER):
        data = table[table["case"] == name]
        for col, column in enumerate(("soc_after_1_week", "grid_buy_kwh_week")):
            ax = axes[row, col]
            ax.set_facecolor(surface)
            for rule, (label, color) in series.items():
                line = data[data["rule"] == rule].sort_values("amps")
                ax.plot(line["amps"], line[column], color=color, linewidth=2, marker="o",
                        markersize=5, label=label)
            ax.grid(axis="y", color=grid, linewidth=0.8)
            ax.spines[["top", "right"]].set_visible(False)
            ax.spines[["left", "bottom"]].set_color(grid)
            ax.tick_params(colors=muted)
            if col == 0:
                ax.axhline(EV_SOC_START * 100, color=muted, linewidth=1, linestyle=":",
                           label="Full (95%)")
                ax.set_ylim(70, 100)
                ax.set_ylabel("Truck SOC, %", color=muted)
                ax.set_title(f"{name}: SOC after one week", loc="left", color=text, fontsize=11)
            else:
                ax.axhline(base.loc[name, "grid_buy_kwh_week"], color=muted, linewidth=1.2,
                           linestyle="--", label="Farm without truck charging")
                ax.set_ylim(bottom=0)
                ax.set_ylabel("kWh per week", color=muted)
                ax.set_title(f"{name}: grid bought", loc="left", color=text, fontsize=11)
    for ax in axes[-1]:
        ax.set_xlabel("Charging current, A", color=muted)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    more_handles, more_labels = axes[0, 1].get_legend_handles_labels()
    handles.append(more_handles[-1])
    labels.append(more_labels[-1])
    fig.legend(handles, labels, loc="upper center", ncol=4, frameon=False, labelcolor=text,
               bbox_to_anchor=(0.5, 1.0))
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(out / "weather_test.png", dpi=160)
    plt.close(fig)


def weather_test() -> None:
    out = OUT / "weather_test"
    out.mkdir(parents=True, exist_ok=True)
    sim.use_tuned_model(True)
    source = load_inputs()
    weeks = {name: repeat_days(source, days) for name, days in WEATHER.items()}
    jobs = [(week, name, 1.0, False, 0, "covers-charger", None) for name, week in weeks.items()]
    jobs += [
        (week, name, 1.0, False, amps, rule, "every-day")
        for name, week in weeks.items() for amps in AMPS
        for rule in ("excess-3kw", "covers-charger")
    ]
    with Pool() as pool:
        table = pd.DataFrame(pool.map(run, jobs, chunksize=2))
    base = table[table["amps"] == 0].set_index("case")
    table = table[table["amps"] > 0]
    table.to_csv(out / "results.csv", index=False)
    plot_weather(table, base, out)

    columns = ["amps", "rule", "soc_after_1_week", "soc_tue_latest", "ev_kwh_week",
               "charge_hours_week", "starts_week", "grid_buy_kwh_week"]
    for name, week in weeks.items():
        print(f"=== {name}: {week['pv_kw'].sum() / 60:.0f} kWh PV per week, "
              f"{base.loc[name, 'grid_buy_kwh_week']:.1f} kWh grid without truck ===")
        rows = table[table["case"] == name].sort_values(["rule", "amps"])
        print(rows[columns].round(1).to_string(index=False), "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pv-xlsx", type=Path, help="one Sunday-to-Saturday Sol-Ark week")
    parser.add_argument("--outdoor-csv", type=Path, default=OUTDOOR)
    parser.add_argument("--name", default="custom-week")
    parser.add_argument("--weather-test", action="store_true")
    args = parser.parse_args()
    if args.weather_test:
        weather_test()
        return

    sim.use_tuned_model(True)
    if args.pv_xlsx:
        inputs = load_week(args.pv_xlsx, args.outdoor_csv)
        cases = {args.name: (1.0, False)}
        out = OUT / args.name
    else:
        inputs = load_inputs()
        cases = CASES
        out = OUT
    out.mkdir(parents=True, exist_ok=True)

    jobs = [(inputs, case, *cases[case], 0, "pv-0.5kw", None) for case in cases]
    jobs += [
        (inputs, case, *cases[case], amps, rule, days)
        for case in cases for amps in AMPS for rule in RULES for days in DAYS
    ]
    with Pool() as pool:
        table = pd.DataFrame(pool.map(run, jobs, chunksize=4))

    # energy and CO2 the truck adds, compared with never charging it
    base = table[table["amps"] == 0].set_index("case")
    for column in ("grid_buy_kwh_week", "grid_sell_kwh_week", "co2_lb_week"):
        table[f"added_{column}"] = table[column] - table["case"].map(base[column])
    table["ev_grid_percent"] = (
        100.0 * table["added_grid_buy_kwh_week"] / table["ev_kwh_week"].replace(0, np.nan)
    )
    table = table[table["amps"] > 0]
    table.to_csv(out / "all_results.csv", index=False)

    if not args.pv_xlsx:
        plot(table)

    baseline_cols = ["grid_buy_kwh_week", "grid_sell_kwh_week", "co2_lb_week"]
    print("No truck charging, per week:\n", base[baseline_cols].round(1).to_string(), "\n")
    show = ["amps", "rule", "soc_tue_week1", "soc_tue_week2", "keeps_up", "charge_hours_week",
            "starts_week", "added_grid_buy_kwh_week", "added_co2_lb_week", "ev_grid_percent"]
    for case in cases:
        print(f"=== {case}, charging every day: rules that keep up ===")
        rows = table[(table["case"] == case) & (table["days"] == "every-day") & table["keeps_up"]]
        print(rows.sort_values(["added_grid_buy_kwh_week", "amps"])[show].round(1).head(25)
              .to_string(index=False), "\n")


if __name__ == "__main__":
    main()
