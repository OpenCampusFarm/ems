from matplotlib import pyplot as plt
import pandas as pd

from simulation import Cooler, ems_setpoint, synthetic_moer, PV


# --------------------------------------------------
# Actual cooler data
# --------------------------------------------------

cooler_temp_data = pd.read_csv(
    "core/Data/room_temperature_30d.csv",
    usecols=["timestamp_local", "room_temp_f", "set_temp_f"]
)

cooler_temp_data["timestamp_local"] = pd.to_datetime(
    cooler_temp_data["timestamp_local"]
)

cooler_temp_data["room_temp_f"] = pd.to_numeric(
    cooler_temp_data["room_temp_f"],
    errors="coerce"
)

cooler_temp_data = (
    cooler_temp_data
    .sort_values("timestamp_local")
    .reset_index(drop=True)
)

cooler_temp_data["set_temp_f"] = (
    pd.to_numeric(cooler_temp_data["set_temp_f"], errors="coerce")
    .ffill()
)


# --------------------------------------------------
# Outdoor-temperature data
# --------------------------------------------------

outdoor_temp_data = pd.read_csv(
    "core/Data/outdoor_temperatures.csv",
    usecols=["timestamp", "temperature_f"]
)

outdoor_temp_data["timestamp"] = pd.to_datetime(
    outdoor_temp_data["timestamp"]
)

outdoor_temp_data["temperature_f"] = pd.to_numeric(
    outdoor_temp_data["temperature_f"],
    errors="coerce"
)

outdoor_temp_data = (
    outdoor_temp_data
    .dropna(subset=["timestamp"])
    .sort_values("timestamp")
    .drop_duplicates(subset=["timestamp"])
    .reset_index(drop=True)
)


TMIN = 34.0
TMAX = 55.0

first_setpoint = cooler_temp_data["set_temp_f"].first_valid_index()

if first_setpoint is None:
    raise ValueError("No valid setpoint exists in the cooler data")

SETPOINT = cooler_temp_data.loc[first_setpoint, "set_temp_f"]


# --------------------------------------------------
# Timestamp alignment
# --------------------------------------------------

def align_outdoor_temps(target_times):
    """
    Match hourly outdoor temperatures to target timestamps.
    Both sides are converted to UTC to ensure compatible datatypes.
    """
    target_timestamp = pd.to_datetime(target_times)

    if target_timestamp.dt.tz is None:
        target_timestamp = target_timestamp.dt.tz_localize(
            "America/Detroit",
            ambiguous="infer",
            nonexistent="shift_forward"
        )
    else:
        target_timestamp = target_timestamp.dt.tz_convert(
            "America/Detroit"
        )

    targets = pd.DataFrame({
        "timestamp": target_timestamp.dt.tz_convert("UTC"),
        "_original_order": range(len(target_timestamp))
    }).sort_values("timestamp")

    outdoor = outdoor_temp_data.copy()

    outdoor_timestamp = pd.to_datetime(outdoor["timestamp"])

    if outdoor_timestamp.dt.tz is None:
        outdoor_timestamp = outdoor_timestamp.dt.tz_localize(
            "America/Detroit",
            ambiguous="infer",
            nonexistent="shift_forward"
        )
    else:
        outdoor_timestamp = outdoor_timestamp.dt.tz_convert(
            "America/Detroit"
        )

    outdoor["timestamp"] = outdoor_timestamp.dt.tz_convert("UTC")

    outdoor = (
        outdoor
        .dropna(subset=["timestamp"])
        .sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"])
    )

    aligned = pd.merge_asof(
        targets,
        outdoor[["timestamp", "temperature_f"]],
        on="timestamp",
        direction="nearest",
        tolerance=pd.Timedelta("1 hour")
    )

    return (
        aligned
        .sort_values("_original_order")["temperature_f"]
        .reset_index(drop=True)
    )


# --------------------------------------------------
# Plotting functions
# --------------------------------------------------

def plot_cooler(
    cooler_temps,
    time_h,
    setpoints,
    outdoor_temps,
    title
):
    correlation = outdoor_cooler_correlation(
        cooler_temps,
        outdoor_temps
    )

    if pd.isna(correlation):
        correlation_label = "Pearson r = unavailable"
    else:
        correlation_label = f"Pearson r = {correlation:.3f}"

    fig, ax = plt.subplots(figsize=(13, 6))

    fig.suptitle(
        f"{title}\nOutdoor vs. cooler temperature: {correlation_label}",
        fontsize=13
    )

    ax.plot(
        time_h,
        setpoints,
        color="steelblue",
        label="Setpoint",
        linewidth=1.5
    )

    ax.plot(
        time_h,
        cooler_temps,
        color="crimson",
        label="Cooler temperature",
        linewidth=1.2,
        alpha=0.8
    )

    ax.plot(
        time_h,
        outdoor_temps,
        color="darkorange",
        label="Outdoor temperature",
        linewidth=1.0,
        alpha=0.75
    )

    ax.axhline(
        TMIN,
        color="blue",
        linestyle="--",
        linewidth=0.8,
        label=f"TMIN = {TMIN}°F"
    )

    ax.axhline(
        TMAX,
        color="red",
        linestyle="--",
        linewidth=0.8,
        label=f"TMAX = {TMAX}°F"
    )

    ax.fill_between(
        time_h,
        TMIN,
        TMAX,
        color="green",
        alpha=0.05,
        label="Safe zone"
    )

    ax.set_xlabel("Time")
    ax.set_ylabel("Temperature (°F)")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.3)

    fig.autofmt_xdate()
    plt.tight_layout()
    plt.show()


def plot_cooler_comp(
    cooler_temps_data,
    cooler_temps_sim,
    outdoor_temps,
    time_h
):
    actual_correlation = outdoor_cooler_correlation(
        cooler_temps_data,
        outdoor_temps
    )

    simulated_correlation = outdoor_cooler_correlation(
        cooler_temps_sim,
        outdoor_temps
    )

    actual_label = (
        "unavailable"
        if pd.isna(actual_correlation)
        else f"{actual_correlation:.3f}"
    )

    simulated_label = (
        "unavailable"
        if pd.isna(simulated_correlation)
        else f"{simulated_correlation:.3f}"
    )

    fig, ax = plt.subplots(figsize=(13, 6))

    fig.suptitle(
        "Campus Farm Cooler vs. Simulated Cooler\n"
        f"Outdoor correlation: actual r = {actual_label}, "
        f"simulated r = {simulated_label}",
        fontsize=13
    )

    ax.plot(
        time_h,
        cooler_temps_data,
        color="purple",
        label="Campus Farm Cooler",
        linewidth=1.5
    )

    ax.plot(
        time_h,
        cooler_temps_sim,
        color="green",
        label="Simulated Cooler",
        linewidth=1.2,
        alpha=0.8
    )

    ax.plot(
        time_h,
        outdoor_temps,
        color="darkorange",
        label="Outdoor temperature",
        linewidth=1.0,
        alpha=0.75
    )

    ax.axhline(
        TMIN,
        color="blue",
        linestyle="--",
        linewidth=0.8,
        label=f"TMIN = {TMIN}°F"
    )

    ax.axhline(
        TMAX,
        color="red",
        linestyle="--",
        linewidth=0.8,
        label=f"TMAX = {TMAX}°F"
    )

    ax.fill_between(
        time_h,
        TMIN,
        TMAX,
        color="green",
        alpha=0.05,
        label="Safe zone"
    )

    ax.set_xlabel("Time")
    ax.set_ylabel("Temperature (°F)")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(True, alpha=0.3)

    fig.autofmt_xdate()
    plt.tight_layout()
    plt.show()

def outdoor_cooler_correlation(cooler_temps, outdoor_temps):
    """
    Calculate the Pearson correlation between cooler and outdoor temperature.
    """
    paired = pd.DataFrame({
        "cooler_temp_f": pd.to_numeric(cooler_temps, errors="coerce").to_numpy(),
        "outdoor_temp_f": pd.to_numeric(outdoor_temps, errors="coerce").to_numpy(),
    }).dropna()

    if len(paired) < 2:
        return float("nan")

    return paired["cooler_temp_f"].corr(
        paired["outdoor_temp_f"],
        method="pearson"
    )
# --------------------------------------------------
# Actual temperatures
# --------------------------------------------------

actual_temps = cooler_temp_data["room_temp_f"]
actual_setpoints = cooler_temp_data["set_temp_f"]
actual_times = cooler_temp_data["timestamp_local"]

actual_outdoor_temps = align_outdoor_temps(actual_times)

plot_cooler(
    actual_temps,
    actual_times,
    actual_setpoints,
    actual_outdoor_temps,
    "Campus Farm Cooler - 30 days"
)


# --------------------------------------------------
# Run simulation
# --------------------------------------------------

outdoor_by_minute = (
    outdoor_temp_data
    .set_index("timestamp")["temperature_f"]
    .sort_index()
    .resample("1min")
    .interpolate(method="time")
)
cooler = Cooler(
    ambient_f=float(outdoor_by_minute.iloc[0]),
    setpoint_f=SETPOINT,
    ri=3.0,
    cop = 2,
    ci = 0.2
    
)

cooler.temp = 52.8

pv = PV()

sim_temps = []
sim_setpoints = []
sim_times = []
sim_outdoor_temps = []


simulation_start = pd.Timestamp(
    cooler_temp_data["timestamp_local"].iloc[0]
)

if simulation_start.tzinfo is None:
    simulation_start = simulation_start.tz_localize("America/Detroit")
else:
    simulation_start = simulation_start.tz_convert("America/Detroit")

simulation_times = pd.date_range(
    start=simulation_start,
    periods=30 * 24 * 60,
    freq="1min",
)

simulation_times_naive = simulation_times.tz_localize(None)

simulation_outdoor = outdoor_by_minute.reindex(
    simulation_times_naive,
    method="nearest",
    tolerance=pd.Timedelta("1 hour"),
)
for step, timestamp in enumerate(simulation_times):
    outdoor_f = simulation_outdoor.iloc[step]

    if pd.isna(outdoor_f):
        continue

    minute_of_day = timestamp.hour * 60 + timestamp.minute

    pv_kw = pv.update(minute_of_day)
    moer = synthetic_moer(minute_of_day)

    sp = ems_setpoint(
        pv_kw,
        moer,
        cooler.temp,
    )

    cooler.change_setpoint(sp)
    cooler.update(outdoor_f=float(outdoor_f))

    sim_times.append(timestamp)
    sim_temps.append(cooler.temp)
    sim_setpoints.append(sp)
    sim_outdoor_temps.append(float(outdoor_f))



# --------------------------------------------------
# Create simulation DataFrame
# --------------------------------------------------

simulation_data = pd.DataFrame({
    "timestamp_local": sim_times,
    "room_temp_f": sim_temps,
    "setpoint": sim_setpoints,
    "outdoor_temp_f": sim_outdoor_temps,
})


# --------------------------------------------------
# Match actual and simulated values by timestamp
# --------------------------------------------------

comparison_data = cooler_temp_data.merge(
    simulation_data,
    on="timestamp_local",
    how="inner",
    suffixes=("_actual", "_sim")
)

comparison_outdoor_temps = align_outdoor_temps(
    comparison_data["timestamp_local"]
)


# --------------------------------------------------
# Plot simulation at actual measurement times
# --------------------------------------------------

plot_cooler(
    comparison_data["room_temp_f_sim"],
    comparison_data["timestamp_local"],
    comparison_data["setpoint"],
    comparison_outdoor_temps,
    "Simulated Cooler - 30 days"
)

plot_cooler_comp(
    comparison_data["room_temp_f_actual"],
    comparison_data["room_temp_f_sim"],
    comparison_outdoor_temps,
    comparison_data["timestamp_local"]
)