"""Plot cooler results from summary.csv and controller minute CSVs.

Usage:
    python core/plot_cooler_summary.py \
        results/integrated_simulation/<week-directory>/summary.csv

Optional measured cooler-energy comparison:
    python core/plot_cooler_summary.py \
        results/integrated_simulation/<week-directory>/summary.csv \
        --measured-cooler-kwh 42

Expected minute files beside summary.csv:
    without-ems_minutes.csv
    with-ems_minutes.csv
    staged-ems_minutes.csv

Output:
    Saves cooler_summary.png 
"""

import argparse
import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


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

TMIN = 34.0
TMAX = 55.0


def find_minute_file(directory: Path, controller: str) -> Path:
    """Locate a controller's minute CSV, including the supplied typo."""
    candidates = [directory / f"{controller}_minutes.csv"]

    if controller == "with-ems":
        candidates.append(directory / "with-ems_minutes.csv")

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(
        f"No minute file for {controller!r}. Expected: "
        + ", ".join(str(path) for path in candidates)
    )


def load_summary(csv_path: Path) -> pd.DataFrame:
    """Combine summary energy with metrics derived from minute files."""
    data = pd.read_csv(csv_path)

    required = {"controller", "cooler_kwh", "cooler_unsafe_minutes"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(
            "Summary CSV is missing required columns: "
            + ", ".join(sorted(missing))
        )

    if data.empty:
        raise ValueError("Summary CSV contains no rows.")

    if data["controller"].isna().any():
        raise ValueError("Summary CSV contains a missing controller.")

    data["controller"] = data["controller"].astype(str).str.strip()

    if data["controller"].eq("").any():
        raise ValueError("Summary CSV contains an empty controller.")

    if data["controller"].duplicated().any():
        raise ValueError("Expected one summary row per controller.")

    for column in ("cooler_kwh", "cooler_unsafe_minutes"):
        data[column] = pd.to_numeric(data[column], errors="raise")
        if not np.isfinite(data[column].to_numpy(dtype=float)).all():
            raise ValueError(f"{column} contains missing or nonfinite values.")
        if (data[column] < 0).any():
            raise ValueError(f"{column} must be nonnegative.")

    # Keep the reported value for comparison with the calculated duration.
    data = data.rename(
        columns={"cooler_unsafe_minutes": "reported_unsafe_minutes"}
    )

    records = []
    reference_timestamps = None

    for row in data.to_dict("records"):
        controller = row["controller"]
        minute_path = find_minute_file(csv_path.parent, controller)
        minute_data = pd.read_csv(minute_path)

        required_minutes = {"timestamp", "cooler_temp_f"}
        missing = required_minutes - set(minute_data.columns)
        if missing:
            raise ValueError(
                f"{minute_path.name} is missing columns: "
                + ", ".join(sorted(missing))
            )

        if minute_data.empty:
            raise ValueError(f"{minute_path.name} contains no rows.")

        # UTC parsing handles timezone offsets consistently for validation.
        timestamps = pd.to_datetime(
            minute_data["timestamp"], errors="raise", utc=True
        )
        temperatures = pd.to_numeric(
            minute_data["cooler_temp_f"], errors="raise"
        )

        if timestamps.isna().any():
            raise ValueError(f"{minute_path.name} has missing timestamps.")

        if not np.isfinite(temperatures.to_numpy(dtype=float)).all():
            raise ValueError(
                f"{minute_path.name} has missing or nonfinite temperatures."
            )
        steps = timestamps.diff().iloc[1:]
        if not steps.eq(pd.Timedelta(minutes=1)).all():
            raise ValueError(
                f"{minute_path.name} must contain ordered, consecutive "
                "one-minute timestamps with no duplicates or gaps."
            )

        if reference_timestamps is None:
            reference_timestamps = timestamps
        elif not timestamps.equals(reference_timestamps):
            raise ValueError(
                f"{minute_path.name} covers different timestamps "
                "from the other controller files."
            )

        cold_minutes = int((temperatures < TMIN).sum())
        hot_minutes = int((temperatures > TMAX).sum())
        unsafe_minutes = cold_minutes + hot_minutes

        if not np.isclose(
            unsafe_minutes,
            row["reported_unsafe_minutes"],
            rtol=0,
            atol=1e-6,
        ):
            warnings.warn(
                f"{controller}: summary.csv reports "
                f"{row['reported_unsafe_minutes']:g} unsafe minutes, "
                f"but the minute data gives {unsafe_minutes} using "
                f"{TMIN:g}–{TMAX:g} °F. Plotting the calculated value.",
                stacklevel=2,
            )

        start = pd.Timestamp(minute_data["timestamp"].iloc[0])
        end = (
            pd.Timestamp(minute_data["timestamp"].iloc[-1])
            + pd.Timedelta(minutes=1)
        )

        row.update(
            start=start,
            end=end,
            cooler_min_f=float(temperatures.min()),
            cooler_max_f=float(temperatures.max()),
            cooler_too_cold_minutes=cold_minutes,
            cooler_too_hot_minutes=hot_minutes,
            cooler_unsafe_minutes=unsafe_minutes,
        )
        records.append(row)

    result = pd.DataFrame(records)

    order = {
        controller: index
        for index, controller in enumerate(CONTROLLER_LABELS)
    }
    result["_order"] = result["controller"].map(order).fillna(len(order))
    result = (
        result.sort_values("_order", kind="stable")
        .drop(columns="_order")
        .reset_index(drop=True)
    )

    result["label"] = (
        result["controller"]
        .map(CONTROLLER_LABELS)
        .fillna(result["controller"])
    )
    return result


def add_bar_labels(
    ax: plt.Axes,
    bars,
    decimals: int = 1,
    suffix: str = "",
) -> None:
    """Place value labels above vertical bars."""
    for bar in bars:
        value = bar.get_height()
        ax.annotate(
            f"{value:.{decimals}f}{suffix}",
            xy=(bar.get_x() + bar.get_width() / 2, value),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=9,
        )


def plot_cooler_summary(
    data: pd.DataFrame,
    output_path: Path,
    measured_cooler_kwh: float | None = None,
) -> None:
    labels = data["label"].tolist()
    colors = [
        CONTROLLER_COLORS.get(controller, "slategray")
        for controller in data["controller"]
    ]
    x = np.arange(len(data))

    start = data["start"].min().strftime("%Y-%m-%d %H:%M %z")
    end = data["end"].max().strftime("%Y-%m-%d %H:%M %z")

    fig, axes = plt.subplots(3, 1, figsize=(14, 10))
    fig.suptitle(
        f"Cooler simulation summary: {start} to {end}",
        fontsize=14,
        fontweight="bold",
    )

    # Panel 1: cooler electricity from summary.csv.
    ax = axes[0]
    bars = ax.bar(
        x,
        data["cooler_kwh"],
        color=colors,
        edgecolor="black",
        linewidth=0.5,
    )
    add_bar_labels(ax, bars, decimals=2, suffix=" kWh")

    if measured_cooler_kwh is not None:
        ax.axhline(
            measured_cooler_kwh,
            color="crimson",
            linestyle="--",
            linewidth=2,
            label=f"Measured: {measured_cooler_kwh:.2f} kWh",
        )

        for i, simulated in enumerate(data["cooler_kwh"]):
            difference = simulated - measured_cooler_kwh
            comparison = f"{difference:+.2f} kWh"

            if measured_cooler_kwh > 0:
                percentage = 100 * difference / measured_cooler_kwh
                comparison += f"\n({percentage:+.1f}%)"
            else:
                comparison += "\n(% undefined: zero reference)"

            ax.text(
                i,
                simulated / 2,
                comparison,
                ha="center",
                va="center",
                fontsize=8,
                color="white" if simulated > 10 else "black",
                fontweight="bold",
            )

        ax.legend(frameon=False)

    ax.margins(y=0.20)
    ax.set_title("Cooler electricity", pad=14)
    ax.set_ylabel("Energy (kWh)")

    # Panel 2: temperature extrema from minute CSVs.
    ax = axes[1]
    minimums = data["cooler_min_f"].to_numpy()
    maximums = data["cooler_max_f"].to_numpy()
    centers = (minimums + maximums) / 2

    ax.axhspan(
        TMIN,
        TMAX,
        color="green",
        alpha=0.08,
        label=f"Evaluation range: {TMIN:.0f}–{TMAX:.0f} °F",
    )
    ax.axhline(TMIN, color="royalblue", linestyle="--", linewidth=1)
    ax.axhline(TMAX, color="crimson", linestyle="--", linewidth=1)

    for i, color in enumerate(colors):
        ax.errorbar(
            x[i],
            centers[i],
            yerr=np.array([
                [centers[i] - minimums[i]],
                [maximums[i] - centers[i]],
            ]),
            fmt="o",
            markersize=9,
            capsize=9,
            capthick=2,
            elinewidth=4,
            markerfacecolor=color,
            markeredgecolor="black",
            color=color,
            ecolor=color,
        )
        ax.text(
            i, minimums[i] - 0.35, f"{minimums[i]:.1f}°",
            ha="center", va="top", fontsize=9,
        )
        ax.text(
            i, maximums[i] + 0.35, f"{maximums[i]:.1f}°",
            ha="center", va="bottom", fontsize=9,
        )

    ax.margins(y=0.20)
    ax.set_title("Cooler temperature range over simulation period", pad=14)
    ax.set_ylabel("Temperature (°F)")
    ax.legend(frameon=False, loc="best")

    # Panel 3: threshold-violation durations from minute CSVs.
    ax = axes[2]
    cold = data["cooler_too_cold_minutes"].to_numpy()
    hot = data["cooler_too_hot_minutes"].to_numpy()
    totals = cold + hot

    ax.bar(
        x,
        cold,
        color="royalblue",
        edgecolor="black",
        linewidth=0.5,
        label=f"Below {TMIN:.0f} °F",
    )
    ax.bar(
        x,
        hot,
        bottom=cold,
        color="crimson",
        edgecolor="black",
        linewidth=0.5,
        hatch="//",
        label=f"Above {TMAX:.0f} °F",
    )

    for i, total in enumerate(totals):
        ax.annotate(
            f"{total:g} min",
            xy=(i, total),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=9,
        )

    if totals.max() == 0:
        ax.set_ylim(0, 1)
        ax.set_yticks([0, 1])
        ax.text(
            0.5,
            0.5,
            "No threshold violations",
            transform=ax.transAxes,
            ha="center",
            va="center",
            fontsize=12,
            fontweight="bold",
        )
    else:
        ax.margins(y=0.20)

    ax.set_title("Temperature-threshold violations", pad=14)
    ax.set_ylabel("Minutes")
    ax.legend(frameon=False)

    for ax in axes:
        ax.set_xticks(x, labels)
        ax.grid(axis="y", alpha=0.25)
        ax.set_axisbelow(True)

    fig.tight_layout(rect=(0, 0.02, 1, 0.95))
    fig.savefig(output_path, dpi=160, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Plot cooler metrics from summary.csv and adjacent "
            "controller minute CSVs."
        )
    )
    parser.add_argument(
        "summary_csv",
        type=Path,
        help="Path to the simulation summary.csv file",
    )
    parser.add_argument(
        "--measured-cooler-kwh",
        type=float,
        help="Measured cooler energy for the same simulation period",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output PNG; default is cooler_summary.png beside summary.csv",
    )
    args = parser.parse_args()

    if not args.summary_csv.is_file():
        parser.error(f"File does not exist: {args.summary_csv}")

    if args.measured_cooler_kwh is not None:
        if (
            not np.isfinite(args.measured_cooler_kwh)
            or args.measured_cooler_kwh < 0
        ):
            parser.error("--measured-cooler-kwh must be finite and nonnegative.")

    output_path = (
        args.output
        if args.output is not None
        else args.summary_csv.parent / "cooler_summary.png"
    )

    try:
        data = load_summary(args.summary_csv)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plot_cooler_summary(
        data,
        output_path,
        measured_cooler_kwh=args.measured_cooler_kwh,
    )
    print(f"Saved: {output_path.resolve()}")


if __name__ == "__main__":
    main()