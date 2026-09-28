"""EV-only simulation using real Sol-Ark PV and WattTime MOER data.

The current experiment uses the Enphase IQ 60 charger for every scenario.
Scenario 1 is the no-EMS Typical Day baseline. Scenario 2 will compare binary
EMS control across high-, typical-, and low-PV days, and Scenario 3 will test
25%-interval control on the Typical Day.

WattTime credentials are read from ``WATTTIME_API_TOKEN`` or the project's
existing ``WT_USERNAME`` and ``WT_PASSWORD`` environment variables. Never place
credentials directly in this file.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv


load_dotenv()


# Enphase IQ 60 maximum continuous output at 240 V is 11.5 kW.  The 90%
# efficiency remains a battery-charging assumption, not an IQ 60 specification.
EV_CAPACITY = 131.0
EV_CHARGE_RATE = 11.5
EV_CHARGE_EFF = 0.90
EV_MAX_MILES = 240
EV_SOC_INIT = 0.20
EV_SOC_TARGET = 0.95
PV_MIN_PRODUCING = 0.5
# Original MISO_DETROIT control threshold from the EMS design.
CO2_THRESHOLD = 1400.0
EV_MONTHLY_IDLE_LOSS = 0.02
CHARGER_POWER_LEVELS_KW = (0.0, 2.88, 5.75, 8.63, 11.5)
PV_STAGE_THRESHOLDS_KW = (2.88, 5.75, 8.63)

WATTTIME_BASE_URL = "https://api.watttime.org"
WATTTIME_HISTORICAL_URL = f"{WATTTIME_BASE_URL}/v3/historical"
WATTTIME_FORECAST_HISTORICAL_URL = (
    f"{WATTTIME_BASE_URL}/v3/forecast/historical"
)
SOLARK_PV_COLUMNS = ("Ppv1(W)/186", "Ppv2(W)/187", "Ppv3(W)/188", "Ppv4(W)/189")
REAL_SCENARIOS = {
    "real-data",
    "high-pv",
    "low-pv",
    "typical-day",
    "alternative-charger",
}
SCENARIO_EXPECTED_DATES = {
    "high-pv": date(2026, 9, 14),
    "low-pv": date(2026, 9, 19),
    "typical-day": date(2026, 9, 18),
}
SAMPLE_DAY_LABELS = ("High PV", "Typical Day", "Low PV")


class EV:
    def __init__(
        self,
        soc_init: float = EV_SOC_INIT,
        capacity_kwh: float = EV_CAPACITY,
        charge_rate_kw: float = EV_CHARGE_RATE,
        charge_eff: float = EV_CHARGE_EFF,
        soc_target: float = EV_SOC_TARGET,
        monthly_idle_loss_fraction: float = EV_MONTHLY_IDLE_LOSS,
    ):
        self.soc = soc_init
        self.capacity = capacity_kwh
        self.rate = charge_rate_kw
        self.eff = charge_eff
        self.target = soc_target
        self.monthly_idle_loss_fraction = monthly_idle_loss_fraction
        self.charging = False
        self.input_power_kw = 0.0

    def charge(
        self,
        power_kw: float | None = None,
        dt_hours: float = 1.0 / 60.0,
    ) -> None:
        selected_power = self.rate if power_kw is None else power_kw
        if not 0.0 <= selected_power <= self.rate:
            raise ValueError("Charging power must be between zero and charger maximum")
        if self.soc < self.target and selected_power > 0.0:
            self.soc = min(
                self.soc + (selected_power * self.eff * dt_hours) / self.capacity,
                self.target,
            )
            self.charging = True
            self.input_power_kw = selected_power
        else:
            self.charging = False
            self.input_power_kw = 0.0

    def idle(self, dt_hours: float = 1.0 / 60.0) -> None:
        # A 2% monthly loss means 0.02 SoC over 30 * 24 hours.  The previous
        # implementation multiplied the hourly step by 60 and overstated the
        # loss by a factor of 60 for one-minute simulation steps.
        idle_loss = self.monthly_idle_loss_fraction * dt_hours / (30.0 * 24.0)
        self.soc = max(0.0, self.soc - idle_loss)
        self.charging = False
        self.input_power_kw = 0.0


@dataclass(frozen=True)
class ScenarioSummary:
    scenario: str
    controller: str
    simulation_date: str
    moer_source: str
    moer_region: str
    charger_power_kw: float
    charger_efficiency: float
    initial_soc_percent: float
    final_soc_percent: float
    target_soc_percent: float
    target_reached: bool
    target_reached_at: str | None
    charging_minutes: int
    idle_minutes: int
    charger_input_energy_kwh: float
    average_charging_moer_lb_per_mwh: float | None
    moer_weighted_emissions_proxy_lb: float
    pv_condition_minutes: int
    moer_condition_minutes: int
    both_conditions_minutes: int
    neither_condition_minutes: int


def synthetic_pv_kw(minute: int) -> float:
    """Return the original synthetic PV profile from simulation.py."""
    daylight_start = 7 * 60
    daylight_end = 18 * 60 + 30
    duration = daylight_end - daylight_start
    elapsed = minute - daylight_start
    if 0 <= elapsed <= duration:
        return (0.96 * 13.2 / 2.0) * (
            math.sin(math.pi * elapsed / duration) + 1.0
        )
    return 0.0


def synthetic_moer(minute: int) -> float:
    """Return the original synthetic MOER signal from simulation.py."""
    hour = (minute // 60) % 24
    peak = 800.0 * math.sin(math.pi * max(0.0, hour - 7) / 14.0) ** 2
    return 800.0 + peak


def minute_index(day: date, timezone: str) -> pd.DatetimeIndex:
    start = pd.Timestamp(day).tz_localize(timezone)
    return pd.date_range(start=start, periods=24 * 60, freq="1min")


def load_solark_pv(xlsx_path: Path, timezone: str) -> pd.Series:
    """Load a Sol-Ark daily export and return one-minute total PV power in kW."""
    if not xlsx_path.exists():
        raise FileNotFoundError(f"Sol-Ark workbook not found: {xlsx_path}")

    frame = pd.read_excel(xlsx_path, sheet_name=0, header=5, engine="openpyxl")
    required = {"Time", *SOLARK_PV_COLUMNS}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"Sol-Ark workbook is missing columns: {missing}")

    timestamps = pd.to_datetime(frame["Time"], errors="coerce")
    if timestamps.isna().any():
        bad_rows = list(frame.index[timestamps.isna()] + 7)
        raise ValueError(f"Invalid Sol-Ark timestamps at worksheet rows: {bad_rows[:10]}")

    if timestamps.dt.tz is None:
        timestamps = timestamps.dt.tz_localize(
            timezone,
            ambiguous="raise",
            nonexistent="shift_forward",
        )
    else:
        timestamps = timestamps.dt.tz_convert(timezone)

    pv_watts = sum(
        pd.to_numeric(frame[column], errors="coerce") for column in SOLARK_PV_COLUMNS
    )
    if pv_watts.isna().any():
        bad_rows = list(frame.index[pv_watts.isna()] + 7)
        raise ValueError(f"Invalid Sol-Ark PV values at worksheet rows: {bad_rows[:10]}")

    pv = pd.Series(
        pv_watts.to_numpy(dtype=float) / 1000.0,
        index=pd.DatetimeIndex(timestamps).floor("min"),
        name="pv_kw",
    )
    pv = pv.groupby(level=0).mean().sort_index()
    day = pv.index[0].date()
    if any(timestamp.date() != day for timestamp in pv.index):
        raise ValueError("Sol-Ark workbook must contain exactly one local calendar day")

    aligned = pv.reindex(minute_index(day, timezone)).ffill()
    if aligned.isna().any():
        raise ValueError("Sol-Ark data do not cover the beginning of the selected day")
    if (aligned < 0).any():
        raise ValueError("Sol-Ark PV power must be nonnegative")
    return aligned


def _extract_watttime_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "historical", "signal_data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
            if isinstance(value, dict):
                for nested_key in ("data", "results", "values"):
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        return [row for row in nested if isinstance(row, dict)]
    raise ValueError("Unrecognized WattTime historical response structure")


def get_watttime_token() -> str:
    """Return a direct token or exchange the existing project credentials."""
    direct_token = os.environ.get("WATTTIME_API_TOKEN")
    if direct_token:
        return direct_token

    username = os.environ.get("WT_USERNAME")
    password = os.environ.get("WT_PASSWORD")
    if not username or not password:
        raise ValueError(
            "Set WATTTIME_API_TOKEN or both WT_USERNAME and WT_PASSWORD"
        )

    response = requests.get(
        f"{WATTTIME_BASE_URL}/login",
        auth=(username, password),
        timeout=30,
    )
    response.raise_for_status()
    token = response.json().get("token")
    if not token:
        raise ValueError("WattTime login response did not include a token")
    return str(token)


def fetch_watttime_moer(
    day: date,
    timezone: str,
    region: str,
    token: str,
    cache_path: Path | None = None,
) -> pd.Series:
    """Fetch WattTime v3 historical CO2 MOER and return one-minute values."""
    if cache_path and cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        local_start = pd.Timestamp(day).tz_localize(timezone)
        local_end = local_start + pd.Timedelta(days=1)
        params = {
            "region": region,
            "start": local_start.tz_convert("UTC").isoformat(),
            "end": local_end.tz_convert("UTC").isoformat(),
            "signal_type": "co2_moer",
        }
        response = requests.get(
            WATTTIME_HISTORICAL_URL,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    records = _extract_watttime_records(payload)
    rows: list[tuple[pd.Timestamp, float]] = []
    for record in records:
        raw_time = record.get("point_time") or record.get("timestamp")
        raw_value = record.get("value")
        if raw_value is None:
            raw_value = record.get("moer")
        if raw_time is None or raw_value is None:
            continue
        timestamp = pd.to_datetime(raw_time, utc=True, errors="coerce")
        value = pd.to_numeric(raw_value, errors="coerce")
        if pd.isna(timestamp) or pd.isna(value):
            continue
        rows.append((timestamp.tz_convert(timezone).floor("min"), float(value)))

    if not rows:
        raise ValueError("WattTime response contained no usable MOER records")

    moer = pd.Series(
        [value for _, value in rows],
        index=pd.DatetimeIndex([timestamp for timestamp, _ in rows]),
        name="moer_lb_per_mwh",
    )
    moer = moer.groupby(level=0).last().sort_index()
    aligned = moer.reindex(minute_index(day, timezone)).ffill()
    if aligned.isna().any():
        raise ValueError(
            "WattTime data do not cover the beginning of the local day. "
            "Check the date, timezone, region, and API access."
        )
    if (aligned < 0).any():
        raise ValueError("WattTime MOER values must be nonnegative")
    return aligned


def fetch_watttime_historical_forecast_moer(
    day: date,
    timezone: str,
    region: str,
    token: str,
    cache_path: Path | None = None,
) -> pd.Series:
    """Return the latest forecast available at each point in simulation time.

    The filter ``generated_at <= point_time`` prevents look-ahead bias. This
    series is a historical forecast replay, not actual historical MOER.
    """
    if cache_path and cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        local_start = pd.Timestamp(day).tz_localize(timezone)
        local_end = local_start + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        params = {
            "region": region,
            "start": local_start.tz_convert("UTC").isoformat(),
            "end": local_end.tz_convert("UTC").isoformat(),
            "signal_type": "co2_moer",
            "horizon_hours": 1,
        }
        response = requests.get(
            WATTTIME_FORECAST_HISTORICAL_URL,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    groups = payload.get("data", []) if isinstance(payload, dict) else []
    candidates: list[tuple[pd.Timestamp, pd.Timestamp, float]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        generated_at = pd.to_datetime(
            group.get("generated_at"), utc=True, errors="coerce"
        )
        if pd.isna(generated_at):
            continue
        for point in group.get("forecast", []):
            if not isinstance(point, dict):
                continue
            point_time = pd.to_datetime(
                point.get("point_time"), utc=True, errors="coerce"
            )
            value = pd.to_numeric(point.get("value"), errors="coerce")
            if pd.isna(point_time) or pd.isna(value):
                continue
            if generated_at <= point_time:
                candidates.append((point_time, generated_at, float(value)))

    if not candidates:
        raise ValueError(
            "WattTime historical forecast response contained no usable records"
        )

    frame = pd.DataFrame(
        candidates,
        columns=("point_time", "generated_at", "moer_lb_per_mwh"),
    )
    frame["local_time"] = frame["point_time"].dt.tz_convert(timezone).dt.floor(
        "min"
    )
    frame = frame[frame["local_time"].dt.date == day]
    frame = frame.sort_values("generated_at").groupby("local_time").last()
    moer = frame["moer_lb_per_mwh"].sort_index()
    moer.name = "moer_lb_per_mwh"
    aligned = moer.reindex(minute_index(day, timezone)).ffill()
    if aligned.isna().any():
        raise ValueError(
            "WattTime forecast data do not cover the beginning of the local day"
        )
    if (aligned < 0).any():
        raise ValueError("WattTime forecast MOER values must be nonnegative")
    return aligned


def synthetic_inputs(day: date, timezone: str) -> pd.DataFrame:
    index = minute_index(day, timezone)
    return pd.DataFrame(
        {
            "pv_kw": [synthetic_pv_kw(minute) for minute in range(24 * 60)],
            "moer_lb_per_mwh": [
                synthetic_moer(minute) for minute in range(24 * 60)
            ],
        },
        index=index,
    )


def real_inputs(
    pv_xlsx: Path,
    timezone: str,
    region: str,
    token: str,
    cache_path: Path | None,
    moer_source: str,
) -> pd.DataFrame:
    pv = load_solark_pv(pv_xlsx, timezone)
    day = pv.index[0].date()
    if moer_source == "historical":
        moer = fetch_watttime_moer(day, timezone, region, token, cache_path)
    elif moer_source == "forecast-historical":
        moer = fetch_watttime_historical_forecast_moer(
            day,
            timezone,
            region,
            token,
            cache_path,
        )
    else:
        raise ValueError(f"Unsupported MOER source: {moer_source}")
    result = pd.concat([pv, moer], axis=1)
    if result.isna().any().any():
        raise ValueError("Aligned PV and MOER inputs contain missing values")
    return result


def simulate_ev(
    inputs: pd.DataFrame,
    scenario: str,
    charger_power_kw: float,
    charger_efficiency: float,
    controller: str,
    moer_source: str,
    moer_region: str,
) -> tuple[pd.DataFrame, ScenarioSummary]:
    """Run one EV branch against aligned inputs.

    ``ems`` preserves the original clean-energy rule. ``without-ems`` is the
    comparison baseline: charge whenever the battery is below its target.
    """
    if charger_power_kw < 0:
        raise ValueError("charger_power_kw must be nonnegative")
    if not 0 < charger_efficiency <= 1:
        raise ValueError("charger_efficiency must be in (0, 1]")
    if controller not in {"with-ems", "without-ems", "staged-ems"}:
        raise ValueError(
            "controller must be 'with-ems', 'without-ems', or 'staged-ems'"
        )

    ev = EV(
        charge_rate_kw=charger_power_kw,
        charge_eff=charger_efficiency,
    )
    records: list[dict[str, Any]] = []

    for timestamp, row in inputs.iterrows():
        pv_kw = float(row["pv_kw"])
        moer = float(row["moer_lb_per_mwh"])
        pv_condition = pv_kw >= PV_MIN_PRODUCING
        moer_condition = moer < CO2_THRESHOLD

        energy_clean = pv_condition or moer_condition
        if controller == "without-ems":
            commanded_power_kw = charger_power_kw
        elif controller == "with-ems":
            commanded_power_kw = charger_power_kw if energy_clean else 0.0
        elif pv_kw >= PV_STAGE_THRESHOLDS_KW[2]:
            commanded_power_kw = CHARGER_POWER_LEVELS_KW[4]
        elif pv_kw >= PV_STAGE_THRESHOLDS_KW[1]:
            commanded_power_kw = CHARGER_POWER_LEVELS_KW[3]
        elif pv_kw >= PV_STAGE_THRESHOLDS_KW[0]:
            commanded_power_kw = CHARGER_POWER_LEVELS_KW[2]
        elif moer_condition:
            commanded_power_kw = CHARGER_POWER_LEVELS_KW[1]
        else:
            commanded_power_kw = 0.0

        charge_allowed = commanded_power_kw > 0.0
        ev.charge(commanded_power_kw) if charge_allowed else ev.idle()

        records.append(
            {
                "timestamp": timestamp,
                "pv_kw": pv_kw,
                "moer_lb_per_mwh": moer,
                "pv_condition": pv_condition,
                "moer_condition": moer_condition,
                "energy_clean": energy_clean,
                "charge_allowed": charge_allowed,
                "charging": ev.charging,
                "charger_input_kw": ev.input_power_kw,
                "ev_soc_percent": ev.soc * 100.0,
            }
        )

    results = pd.DataFrame.from_records(records).set_index("timestamp")
    target_rows = results[results["ev_soc_percent"] >= EV_SOC_TARGET * 100.0]
    reached_at = None if target_rows.empty else target_rows.index[0].isoformat()
    charging_minutes = int(results["charging"].sum())
    charging_rows = results[results["charging"]]
    charging_energy_kwh = float(results["charger_input_kw"].sum() / 60.0)
    if charging_energy_kwh > 0:
        average_charging_moer = float(
            (
                charging_rows["charger_input_kw"]
                * charging_rows["moer_lb_per_mwh"]
            ).sum()
            / charging_rows["charger_input_kw"].sum()
        )
        charging_emissions_lb = float(
            (
                charging_rows["charger_input_kw"]
                * charging_rows["moer_lb_per_mwh"]
                / 60.0
                / 1000.0
            ).sum()
        )
    else:
        average_charging_moer = None
        charging_emissions_lb = 0.0

    summary = ScenarioSummary(
        scenario=scenario,
        controller=controller,
        simulation_date=results.index[0].date().isoformat(),
        moer_source=moer_source,
        moer_region=moer_region,
        charger_power_kw=charger_power_kw,
        charger_efficiency=charger_efficiency,
        initial_soc_percent=EV_SOC_INIT * 100.0,
        final_soc_percent=float(results["ev_soc_percent"].iloc[-1]),
        target_soc_percent=EV_SOC_TARGET * 100.0,
        target_reached=not target_rows.empty,
        target_reached_at=reached_at,
        charging_minutes=charging_minutes,
        idle_minutes=len(results) - charging_minutes,
        charger_input_energy_kwh=charging_energy_kwh,
        average_charging_moer_lb_per_mwh=average_charging_moer,
        moer_weighted_emissions_proxy_lb=charging_emissions_lb,
        pv_condition_minutes=int(results["pv_condition"].sum()),
        moer_condition_minutes=int(results["moer_condition"].sum()),
        both_conditions_minutes=int(
            (results["pv_condition"] & results["moer_condition"]).sum()
        ),
        neither_condition_minutes=int(
            (~results["pv_condition"] & ~results["moer_condition"]).sum()
        ),
    )
    return results, summary


def save_paired_outputs(
    paired_results: dict[str, pd.DataFrame],
    summaries: dict[str, ScenarioSummary],
    output_dir: Path,
    show_plot: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    scenario = summaries["with-ems"].scenario
    stem = scenario.replace("-", "_")

    combined = pd.concat(
        {
            controller.replace("-", "_"): frame
            for controller, frame in paired_results.items()
        },
        axis=1,
    )
    combined.columns = [f"{group}__{field}" for group, field in combined.columns]
    combined.to_csv(output_dir / f"{stem}_comparison_timeseries.csv")
    (output_dir / f"{stem}_comparison_summary.json").write_text(
        json.dumps(
            {controller: asdict(summary) for controller, summary in summaries.items()},
            indent=2,
        ),
        encoding="utf-8",
    )

    inputs = paired_results["with-ems"]
    hours = np.arange(len(inputs)) / 60.0
    figure, axes = plt.subplots(3, 1, figsize=(13, 9), sharex=True)
    figure.suptitle(
        f"EV Simulation Comparison - {scenario}\n"
        f"MOER: {summaries['with-ems'].moer_source}, "
        f"{summaries['with-ems'].moer_region}",
        fontsize=14,
    )

    pv_axis = axes[0]
    pv_axis.plot(hours, inputs["pv_kw"], color="darkorange", label="PV power (kW)")
    pv_axis.axhline(
        PV_MIN_PRODUCING,
        color="darkorange",
        linestyle=":",
        label=f"PV threshold ({PV_MIN_PRODUCING:.1f} kW)",
    )
    moer_axis = pv_axis.twinx()
    moer_axis.plot(
        hours,
        inputs["moer_lb_per_mwh"],
        color="gray",
        linestyle="--",
        label="Grid MOER",
    )
    moer_axis.axhline(
        CO2_THRESHOLD,
        color="gray",
        linestyle=":",
        label=f"MOER threshold ({CO2_THRESHOLD:.0f})",
    )
    pv_axis.set_ylabel("PV power (kW)")
    moer_axis.set_ylabel("MOER (lb CO2/MWh)")
    pv_lines, pv_labels = pv_axis.get_legend_handles_labels()
    moer_lines, moer_labels = moer_axis.get_legend_handles_labels()
    pv_axis.legend(pv_lines + moer_lines, pv_labels + moer_labels, loc="upper right")
    pv_axis.grid(True, alpha=0.25)

    colors = {"with-ems": "purple", "without-ems": "steelblue"}
    labels = {"with-ems": "With EMS", "without-ems": "Without EMS"}
    for controller, frame in paired_results.items():
        axes[1].step(
            hours,
            frame["charger_input_kw"],
            where="post",
            color=colors[controller],
            label=f"{labels[controller]} charger input",
        )
    axes[1].set_ylabel("Power (kW)")
    axes[1].legend(loc="upper right")
    axes[1].grid(True, alpha=0.25)

    for controller, frame in paired_results.items():
        axes[2].plot(
            hours,
            frame["ev_soc_percent"],
            color=colors[controller],
            label=f"{labels[controller]} SoC",
        )
    axes[2].axhline(
        EV_SOC_TARGET * 100.0,
        color="gray",
        linestyle="--",
        label=f"Target {EV_SOC_TARGET * 100:.0f}%",
    )
    axes[2].set_xlabel("Hour of day")
    axes[2].set_ylabel("EV SoC (%)")
    axes[2].set_xlim(0, 24)
    axes[2].set_xticks(range(0, 25, 2))
    axes[2].legend(loc="lower right")
    axes[2].grid(True, alpha=0.25)

    figure.tight_layout()
    figure.savefig(output_dir / f"{stem}_comparison_overview.png", dpi=180)
    if show_plot:
        plt.show()
    else:
        plt.close(figure)


def save_scenario1_outputs(
    results: pd.DataFrame,
    summary: ScenarioSummary,
    output_dir: Path,
    show_plot: bool,
) -> None:
    """Save the Typical Day no-EMS baseline as one slide-friendly result set."""
    output_dir.mkdir(parents=True, exist_ok=True)
    results.to_csv(output_dir / "scenario_1_typical_day_timeseries.csv")
    (output_dir / "scenario_1_baseline_summary.json").write_text(
        json.dumps(asdict(summary), indent=2), encoding="utf-8"
    )

    figure, axes = plt.subplots(2, 1, figsize=(12, 7.5), sharex=True)
    figure.suptitle("Scenario 1: Baseline", fontsize=16, fontweight="bold")
    hours = np.arange(24 * 60) / 60.0
    input_axis = axes[0]
    input_axis.plot(
        hours, results["pv_kw"], color="darkorange", linewidth=1.5, label="PV"
    )
    input_axis.axhline(
        PV_MIN_PRODUCING,
        color="darkorange",
        linestyle=":",
        linewidth=1.2,
        label=f"PV threshold ({PV_MIN_PRODUCING:.1f} kW)",
    )
    input_axis.set_ylabel("PV (kW)")
    input_axis.grid(True, alpha=0.25)
    moer_axis = input_axis.twinx()
    moer_axis.plot(
        hours,
        results["moer_lb_per_mwh"],
        color="gray",
        linestyle="--",
        linewidth=1.2,
        label="MOER",
    )
    moer_axis.axhline(
        CO2_THRESHOLD,
        color="gray",
        linestyle=":",
        linewidth=1.2,
        label=f"MOER threshold ({CO2_THRESHOLD:.0f})",
    )
    moer_axis.set_ylabel("MOER (lb/MWh)")
    input_lines, input_labels = input_axis.get_legend_handles_labels()
    moer_lines, moer_labels = moer_axis.get_legend_handles_labels()
    input_axis.legend(
        input_lines + moer_lines,
        input_labels + moer_labels,
        loc="upper right",
        fontsize=9,
    )

    result_axis = axes[1]
    result_axis.step(
        hours,
        results["charger_input_kw"],
        where="post",
        color="steelblue",
        linewidth=1.5,
        label="Charger input",
    )
    result_axis.set_xlabel("Hour")
    result_axis.set_ylabel("Power (kW)")
    result_axis.set_xlim(0, 24)
    result_axis.set_xticks(range(0, 25, 2))
    result_axis.set_ylim(-0.5, EV_CHARGE_RATE + 1.0)
    result_axis.grid(True, alpha=0.25)
    soc_axis = result_axis.twinx()
    soc_axis.plot(
        hours,
        results["ev_soc_percent"],
        color="purple",
        linewidth=1.5,
        label="SoC",
    )
    soc_axis.axhline(
        EV_SOC_TARGET * 100.0,
        color="gray",
        linestyle=":",
        linewidth=1.0,
        label=f"Target ({EV_SOC_TARGET * 100:.0f}%)",
    )
    soc_axis.set_ylim(15, 100)
    soc_axis.set_ylabel("SoC (%)")
    power_lines, power_labels = result_axis.get_legend_handles_labels()
    soc_lines, soc_labels = soc_axis.get_legend_handles_labels()
    result_axis.legend(
        power_lines + soc_lines,
        power_labels + soc_labels,
        loc="center right",
        fontsize=9,
    )
    completion = (
        pd.Timestamp(summary.target_reached_at).strftime("%H:%M")
        if summary.target_reached_at
        else "not reached"
    )
    result_axis.text(
        0.98,
        0.08,
        f"Target: {completion}\nEnergy: {summary.charger_input_energy_kwh:.2f} kWh",
        transform=result_axis.transAxes,
        ha="right",
        va="bottom",
        fontsize=10,
        bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "none"},
    )

    figure.tight_layout(rect=(0, 0, 1, 0.94))
    figure.savefig(output_dir / "scenario_1_baseline_overview.png", dpi=180)
    if show_plot:
        plt.show()
    else:
        plt.close(figure)


def run_scenario1_baseline(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Run the IQ 60 no-EMS baseline for the Typical Day only."""
    if args.typical_pv_xlsx is None:
        parser.error("scenario-1-baseline requires --typical-pv-xlsx")

    pv_day = load_solark_pv(args.typical_pv_xlsx, args.timezone).index[0].date()
    cache_path = args.watttime_cache_dir / (
        f"{args.moer_source}_{args.watttime_region}_{pv_day.isoformat()}.json"
    )
    if cache_path.exists():
        token = "cache-only"
    else:
        try:
            token = get_watttime_token()
        except ValueError as exc:
            parser.error(str(exc))

    inputs = real_inputs(
        args.typical_pv_xlsx,
        args.timezone,
        args.watttime_region,
        token,
        cache_path,
        args.moer_source,
    )
    results, summary = simulate_ev(
        inputs,
        "scenario-1-baseline",
        EV_CHARGE_RATE,
        EV_CHARGE_EFF,
        "without-ems",
        args.moer_source,
        args.watttime_region,
    )
    save_scenario1_outputs(results, summary, args.output_dir, args.show_plot)
    completion = (
        pd.Timestamp(summary.target_reached_at).strftime("%H:%M")
        if summary.target_reached_at
        else "not reached"
    )
    print("Scenario 1: no-EMS baseline")
    print(f"Sample day:             Typical Day ({summary.simulation_date})")
    print(f"Target reached at:      {completion}")
    print(f"Charger input energy:   {summary.charger_input_energy_kwh:.2f} kWh")
    print(f"Average charging MOER:  {summary.average_charging_moer_lb_per_mwh:.2f} lb/MWh")
    print(f"Outputs: {args.output_dir.resolve()}")


def save_scenario2_outputs(
    daily_results: dict[str, dict[str, pd.DataFrame]],
    daily_summaries: dict[str, dict[str, ScenarioSummary]],
    output_dir: Path,
    show_plot: bool,
) -> None:
    """Save the three-day binary-EMS comparison and slide-ready figures."""
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_rows: list[dict[str, Any]] = []

    for label in SAMPLE_DAY_LABELS:
        stem = label.lower().replace(" ", "_")
        combined = pd.concat(
            {
                controller.replace("-", "_"): frame
                for controller, frame in daily_results[label].items()
            },
            axis=1,
        )
        combined.columns = [f"{group}__{field}" for group, field in combined.columns]
        combined.to_csv(output_dir / f"scenario_2_{stem}_timeseries.csv")
        for controller in ("without-ems", "with-ems"):
            summary_rows.append(
                {
                    "sample_day": label,
                    **asdict(daily_summaries[label][controller]),
                }
            )

    pd.DataFrame(summary_rows).to_csv(
        output_dir / "scenario_2_binary_summary.csv", index=False
    )
    (output_dir / "scenario_2_binary_summary.json").write_text(
        json.dumps(summary_rows, indent=2), encoding="utf-8"
    )

    hours = np.arange(24 * 60) / 60.0

    def plot_inputs(axis: Any, label: str, *, show_legend: bool = True) -> None:
        frame = daily_results[label]["with-ems"]
        axis.plot(hours, frame["pv_kw"], color="darkorange", label="PV")
        axis.axhline(
            PV_MIN_PRODUCING,
            color="darkorange",
            linestyle=":",
            label=f"PV threshold ({PV_MIN_PRODUCING:.1f})",
        )
        axis.set_ylabel("PV (kW)")
        axis.grid(True, alpha=0.25)
        moer_axis = axis.twinx()
        moer_axis.plot(
            hours,
            frame["moer_lb_per_mwh"],
            color="gray",
            linestyle="--",
            label="MOER",
        )
        moer_axis.axhline(
            CO2_THRESHOLD,
            color="gray",
            linestyle=":",
            label=f"MOER threshold ({CO2_THRESHOLD:.0f})",
        )
        moer_axis.set_ylabel("MOER (lb/MWh)")
        if show_legend:
            pv_lines, pv_labels = axis.get_legend_handles_labels()
            moer_lines, moer_labels = moer_axis.get_legend_handles_labels()
            axis.legend(
                pv_lines + moer_lines,
                pv_labels + moer_labels,
                loc="lower right",
                fontsize=7,
                framealpha=0.75,
                borderpad=0.4,
                labelspacing=0.3,
            )

    def plot_response(axis: Any, label: str, *, show_legend: bool = True) -> None:
        baseline = daily_results[label]["without-ems"]
        ems = daily_results[label]["with-ems"]
        axis.step(
            hours,
            baseline["charger_input_kw"],
            where="post",
            color="#2F6690",
            label="No EMS power",
        )
        axis.step(
            hours,
            ems["charger_input_kw"],
            where="post",
            color="#7A5195",
            label="Binary EMS power",
        )
        axis.set_ylabel("Power (kW)")
        axis.set_ylim(-0.5, EV_CHARGE_RATE + 1.0)
        axis.set_xlim(0, 24)
        axis.set_xticks(range(0, 25, 2))
        axis.set_xlabel("Hour")
        axis.grid(True, alpha=0.25)
        soc_axis = axis.twinx()
        soc_axis.plot(
            hours,
            baseline["ev_soc_percent"],
            color="#2F6690",
            linestyle="--",
            label="No EMS SoC",
        )
        soc_axis.plot(
            hours,
            ems["ev_soc_percent"],
            color="#7A5195",
            linestyle="--",
            label="Binary EMS SoC",
        )
        soc_axis.axhline(
            EV_SOC_TARGET * 100.0,
            color="gray",
            linestyle=":",
            label="Target",
        )
        soc_axis.set_ylabel("SoC (%)")
        soc_axis.set_ylim(15, 100)
        if show_legend:
            power_lines, power_labels = axis.get_legend_handles_labels()
            soc_lines, soc_labels = soc_axis.get_legend_handles_labels()
            axis.legend(
                power_lines + soc_lines,
                power_labels + soc_labels,
                loc="lower right",
                fontsize=7,
                framealpha=0.75,
                borderpad=0.4,
                labelspacing=0.3,
            )

    typical_figure, typical_axes = plt.subplots(2, 1, figsize=(12, 7.5), sharex=True)
    typical_figure.suptitle(
        "Scenario 2: Binary EMS — Typical Day", fontsize=16, fontweight="bold"
    )
    plot_inputs(typical_axes[0], "Typical Day")
    plot_response(typical_axes[1], "Typical Day")
    typical_figure.tight_layout(rect=(0, 0, 1, 0.94))
    typical_figure.savefig(output_dir / "scenario_2_typical_day.png", dpi=180)

    range_figure, range_axes = plt.subplots(2, 2, figsize=(15, 7.5), sharex="col")
    range_figure.suptitle(
        "Scenario 2: High- and Low-PV Days", fontsize=16, fontweight="bold"
    )
    for column, label in enumerate(("High PV", "Low PV")):
        range_axes[0, column].set_title(label)
        plot_inputs(range_axes[0, column], label)
        plot_response(range_axes[1, column], label)
    range_figure.tight_layout(rect=(0, 0, 1, 0.94))
    range_figure.savefig(output_dir / "scenario_2_high_low_days.png", dpi=180)

    labels = list(SAMPLE_DAY_LABELS)
    x = np.arange(len(labels))
    width = 0.34
    summary_figure, summary_axes = plt.subplots(1, 2, figsize=(12, 4.8))
    summary_figure.suptitle("Scenario 2: Outcome Summary", fontsize=15, fontweight="bold")
    baseline_color = "steelblue"
    ems_color = "purple"
    base_emissions = [
        daily_summaries[label]["without-ems"].moer_weighted_emissions_proxy_lb
        for label in labels
    ]
    ems_emissions = [
        daily_summaries[label]["with-ems"].moer_weighted_emissions_proxy_lb
        for label in labels
    ]
    base_bars = summary_axes[0].bar(
        x - width / 2, base_emissions, width, color=baseline_color, label="No EMS"
    )
    ems_bars = summary_axes[0].bar(
        x + width / 2, ems_emissions, width, color=ems_color, label="Binary EMS"
    )
    summary_axes[0].bar_label(base_bars, fmt="%.1f", padding=3, fontsize=9)
    summary_axes[0].bar_label(ems_bars, fmt="%.1f", padding=3, fontsize=9)
    summary_axes[0].set_ylabel("Emissions proxy (lb CO2)")
    summary_axes[0].set_xticks(x, labels)
    summary_axes[0].legend(
        loc="lower right", fontsize=8, framealpha=0.75, borderpad=0.4
    )
    summary_axes[0].grid(axis="y", alpha=0.25)

    base_moer = [
        daily_summaries[label]["without-ems"].average_charging_moer_lb_per_mwh
        for label in labels
    ]
    ems_moer = [
        daily_summaries[label]["with-ems"].average_charging_moer_lb_per_mwh
        for label in labels
    ]
    base_moer_bars = summary_axes[1].bar(
        x - width / 2, base_moer, width, color=baseline_color, label="No EMS"
    )
    ems_moer_bars = summary_axes[1].bar(
        x + width / 2, ems_moer, width, color=ems_color, label="Binary EMS"
    )
    summary_axes[1].bar_label(base_moer_bars, fmt="%.0f", padding=3, fontsize=9)
    summary_axes[1].bar_label(ems_moer_bars, fmt="%.0f", padding=3, fontsize=9)
    summary_axes[1].set_ylabel("Average charging MOER (lb/MWh)")
    summary_axes[1].set_xticks(x, labels)
    summary_axes[1].legend(
        loc="lower right", fontsize=8, framealpha=0.75, borderpad=0.4
    )
    summary_axes[1].grid(axis="y", alpha=0.25)
    summary_figure.tight_layout(rect=(0, 0, 1, 0.92))
    summary_figure.savefig(output_dir / "scenario_2_binary_summary.png", dpi=180)

    if show_plot:
        plt.show()
    else:
        plt.close(typical_figure)
        plt.close(range_figure)
        plt.close(summary_figure)


def run_scenario2_binary(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Run binary EMS control across the high-, typical-, and low-PV days."""
    workbooks = {
        "High PV": args.high_pv_xlsx,
        "Typical Day": args.typical_pv_xlsx,
        "Low PV": args.low_pv_xlsx,
    }
    if any(path is None for path in workbooks.values()):
        parser.error(
            "scenario-2-binary requires --high-pv-xlsx, "
            "--typical-pv-xlsx, and --low-pv-xlsx"
        )

    cache_paths: dict[str, Path] = {}
    for label, workbook in workbooks.items():
        day = load_solark_pv(workbook, args.timezone).index[0].date()
        cache_paths[label] = args.watttime_cache_dir / (
            f"{args.moer_source}_{args.watttime_region}_{day.isoformat()}.json"
        )
    if all(path.exists() for path in cache_paths.values()):
        token = "cache-only"
    else:
        try:
            token = get_watttime_token()
        except ValueError as exc:
            parser.error(str(exc))

    daily_results: dict[str, dict[str, pd.DataFrame]] = {}
    daily_summaries: dict[str, dict[str, ScenarioSummary]] = {}
    for label, workbook in workbooks.items():
        cache_path = cache_paths[label]
        inputs = real_inputs(
            workbook,
            args.timezone,
            args.watttime_region,
            token,
            cache_path,
            args.moer_source,
        )
        daily_results[label] = {}
        daily_summaries[label] = {}
        for controller in ("without-ems", "with-ems"):
            results, summary = simulate_ev(
                inputs,
                "scenario-2-binary",
                EV_CHARGE_RATE,
                EV_CHARGE_EFF,
                controller,
                args.moer_source,
                args.watttime_region,
            )
            daily_results[label][controller] = results
            daily_summaries[label][controller] = summary

    save_scenario2_outputs(
        daily_results, daily_summaries, args.output_dir, args.show_plot
    )
    print("Scenario 2: binary EMS")
    print(f"{'Sample day':<16}{'No EMS':>12}{'Binary EMS':>14}{'MOER change':>18}")
    print("-" * 60)
    for label in SAMPLE_DAY_LABELS:
        base = daily_summaries[label]["without-ems"]
        ems = daily_summaries[label]["with-ems"]
        base_time = (
            pd.Timestamp(base.target_reached_at).strftime("%H:%M")
            if base.target_reached_at
            else "N/R"
        )
        ems_time = (
            pd.Timestamp(ems.target_reached_at).strftime("%H:%M")
            if ems.target_reached_at
            else "N/R"
        )
        print(
            f"{label:<16}{base_time:>12}{ems_time:>14}"
            f"{base.average_charging_moer_lb_per_mwh:>8.0f} → "
            f"{ems.average_charging_moer_lb_per_mwh:.0f}"
        )
    print(f"Outputs: {args.output_dir.resolve()}")


def save_scenario3_outputs(
    results_by_controller: dict[str, pd.DataFrame],
    summaries: dict[str, ScenarioSummary],
    output_dir: Path,
    show_plot: bool,
) -> None:
    """Save Typical Day staged-control profiles and a compact summary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    combined = pd.concat(
        {
            controller.replace("-", "_"): frame
            for controller, frame in results_by_controller.items()
        },
        axis=1,
    )
    combined.columns = [f"{group}__{field}" for group, field in combined.columns]
    combined.to_csv(output_dir / "scenario_3_typical_day_timeseries.csv")
    (output_dir / "scenario_3_staged_summary.json").write_text(
        json.dumps(
            {controller: asdict(summary) for controller, summary in summaries.items()},
            indent=2,
        ),
        encoding="utf-8",
    )

    colors = {
        "without-ems": "steelblue",
        "with-ems": "purple",
        "staged-ems": "darkorange",
    }
    labels = {
        "without-ems": "No EMS",
        "with-ems": "Binary EMS",
        "staged-ems": "Staged EMS",
    }
    hours = np.arange(24 * 60) / 60.0
    staged = results_by_controller["staged-ems"]
    figure, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True)
    figure.suptitle("Scenario 3: Staged EMS", fontsize=16, fontweight="bold")

    input_axis = axes[0]
    input_axis.plot(hours, staged["pv_kw"], color="darkorange", label="PV")
    stage_trigger_labels = ("50%", "75%", "100%")
    for threshold, stage_label in zip(PV_STAGE_THRESHOLDS_KW, stage_trigger_labels):
        input_axis.axhline(
            threshold,
            color="darkorange",
            linestyle=":",
            linewidth=1.0,
            label=f"{stage_label} trigger ({threshold:.2f} kW)",
        )
    input_axis.set_ylabel("PV (kW)")
    input_axis.grid(True, alpha=0.25)
    moer_axis = input_axis.twinx()
    moer_axis.plot(
        hours,
        staged["moer_lb_per_mwh"],
        color="gray",
        linestyle="--",
        label="MOER",
    )
    moer_axis.axhline(
        CO2_THRESHOLD,
        color="gray",
        linestyle=":",
        label=f"MOER threshold ({CO2_THRESHOLD:.0f})",
    )
    moer_axis.set_ylabel("MOER (lb/MWh)")
    input_lines, input_labels = input_axis.get_legend_handles_labels()
    moer_lines, moer_labels = moer_axis.get_legend_handles_labels()
    input_axis.legend(
        input_lines + moer_lines,
        input_labels + moer_labels,
        loc="lower right",
        fontsize=7,
        framealpha=0.75,
        borderpad=0.4,
        labelspacing=0.3,
    )

    for controller in ("without-ems", "with-ems", "staged-ems"):
        charger_level_percent = (
            results_by_controller[controller]["charger_input_kw"]
            / EV_CHARGE_RATE
            * 100.0
        )
        axes[1].step(
            hours,
            charger_level_percent,
            where="post",
            color=colors[controller],
            label=labels[controller],
        )
    axes[1].set_ylabel("Charger level (%)")
    axes[1].set_ylim(-3, 103)
    axes[1].set_yticks((0, 25, 50, 75, 100))
    axes[1].grid(True, alpha=0.25)
    axes[1].legend(
        loc="lower right", fontsize=7, framealpha=0.75, borderpad=0.4
    )

    for controller in ("without-ems", "with-ems", "staged-ems"):
        axes[2].plot(
            hours,
            results_by_controller[controller]["ev_soc_percent"],
            color=colors[controller],
            label=labels[controller],
        )
    axes[2].axhline(
        EV_SOC_TARGET * 100.0,
        color="gray",
        linestyle=":",
        label="Target",
    )
    axes[2].set_xlabel("Hour")
    axes[2].set_ylabel("SoC (%)")
    axes[2].set_xlim(0, 24)
    axes[2].set_xticks(range(0, 25, 2))
    axes[2].set_ylim(15, 100)
    axes[2].grid(True, alpha=0.25)
    axes[2].legend(
        loc="lower right", fontsize=7, framealpha=0.75, borderpad=0.4
    )
    figure.tight_layout(rect=(0, 0, 1, 0.95))
    figure.savefig(output_dir / "scenario_3_staged_profiles.png", dpi=180)

    controller_order = ("without-ems", "with-ems", "staged-ems")
    summary_labels = [labels[controller] for controller in controller_order]
    summary_colors = [colors[controller] for controller in controller_order]
    completion_hours = []
    for controller in controller_order:
        reached_at = summaries[controller].target_reached_at
        if reached_at:
            reached = pd.Timestamp(reached_at)
            completion_hours.append(reached.hour + reached.minute / 60.0)
        else:
            completion_hours.append(np.nan)
    average_moer = [
        summaries[controller].average_charging_moer_lb_per_mwh
        for controller in controller_order
    ]
    emissions = [
        summaries[controller].moer_weighted_emissions_proxy_lb
        for controller in controller_order
    ]
    summary_figure, summary_axes = plt.subplots(1, 3, figsize=(14, 4.5))
    summary_figure.suptitle("Scenario 3: Outcome Summary", fontsize=15, fontweight="bold")
    metrics = (
        (completion_hours, "Completion time (hour)", "%.2f"),
        (average_moer, "Average charging MOER (lb/MWh)", "%.0f"),
        (emissions, "Emissions proxy (lb CO2)", "%.1f"),
    )
    for axis, (values, ylabel, value_format) in zip(summary_axes, metrics):
        bars = axis.bar(summary_labels, values, color=summary_colors)
        axis.bar_label(bars, fmt=value_format, padding=3, fontsize=9)
        axis.set_ylabel(ylabel)
        axis.grid(axis="y", alpha=0.25)
        axis.tick_params(axis="x", labelrotation=10)
    summary_figure.tight_layout(rect=(0, 0, 1, 0.92))
    summary_figure.savefig(output_dir / "scenario_3_staged_summary.png", dpi=180)

    if show_plot:
        plt.show()
    else:
        plt.close(figure)
        plt.close(summary_figure)


def run_scenario3_staged(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Run the three controllers on the Typical Day."""
    if args.typical_pv_xlsx is None:
        parser.error("scenario-3-staged requires --typical-pv-xlsx")
    day = load_solark_pv(args.typical_pv_xlsx, args.timezone).index[0].date()
    cache_path = args.watttime_cache_dir / (
        f"{args.moer_source}_{args.watttime_region}_{day.isoformat()}.json"
    )
    if cache_path.exists():
        token = "cache-only"
    else:
        try:
            token = get_watttime_token()
        except ValueError as exc:
            parser.error(str(exc))
    inputs = real_inputs(
        args.typical_pv_xlsx,
        args.timezone,
        args.watttime_region,
        token,
        cache_path,
        args.moer_source,
    )
    results_by_controller: dict[str, pd.DataFrame] = {}
    summaries: dict[str, ScenarioSummary] = {}
    for controller in ("without-ems", "with-ems", "staged-ems"):
        results, summary = simulate_ev(
            inputs,
            "scenario-3-staged",
            EV_CHARGE_RATE,
            EV_CHARGE_EFF,
            controller,
            args.moer_source,
            args.watttime_region,
        )
        results_by_controller[controller] = results
        summaries[controller] = summary
    save_scenario3_outputs(
        results_by_controller, summaries, args.output_dir, args.show_plot
    )
    print("Scenario 3: staged EMS — Typical Day")
    print(f"{'Controller':<16}{'Target':>12}{'Energy':>14}{'Avg MOER':>14}")
    print("-" * 56)
    controller_labels = {
        "without-ems": "No EMS",
        "with-ems": "Binary EMS",
        "staged-ems": "Staged EMS",
    }
    for controller in ("without-ems", "with-ems", "staged-ems"):
        summary = summaries[controller]
        target = (
            pd.Timestamp(summary.target_reached_at).strftime("%H:%M")
            if summary.target_reached_at
            else "N/R"
        )
        print(
            f"{controller_labels[controller]:<16}{target:>12}"
            f"{summary.charger_input_energy_kwh:>11.2f} kWh"
            f"{summary.average_charging_moer_lb_per_mwh:>14.0f}"
        )
    print(f"Outputs: {args.output_dir.resolve()}")


def parse_day(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the original EV charging logic with synthetic or real inputs"
    )
    parser.add_argument(
        "--scenario",
        choices=(
            "scenario-1-baseline",
            "scenario-2-binary",
            "scenario-3-staged",
            "synthetic-reference",
            "real-data",
            "high-pv",
            "low-pv",
            "typical-day",
            "alternative-charger",
        ),
        required=True,
    )
    parser.add_argument("--pv-xlsx", type=Path, help="Sol-Ark one-day Excel export")
    parser.add_argument("--high-pv-xlsx", type=Path)
    parser.add_argument("--typical-pv-xlsx", type=Path)
    parser.add_argument("--low-pv-xlsx", type=Path)
    parser.add_argument(
        "--date",
        type=parse_day,
        default=date(2026, 9, 14),
        help="Synthetic scenario date in YYYY-MM-DD format",
    )
    parser.add_argument("--timezone", default="America/Detroit")
    parser.add_argument(
        "--watttime-region",
        default=os.environ.get("WT_REGION", "MISO_DETROIT"),
        help="WattTime v3 region code; defaults to WT_REGION or MISO_DETROIT",
    )
    parser.add_argument(
        "--watttime-cache",
        type=Path,
        help="Optional raw WattTime JSON cache path",
    )
    parser.add_argument(
        "--watttime-cache-dir",
        type=Path,
        default=Path("results/ev_simulation/cache"),
        help="WattTime cache directory used by the new scenarios",
    )
    parser.add_argument(
        "--moer-source",
        choices=("historical", "forecast-historical"),
        default="historical",
        help="Use actual historical MOER or a replay of historical forecasts",
    )
    parser.add_argument(
        "--charger-power-kw",
        type=float,
        default=EV_CHARGE_RATE,
        help="Charger input power; change only for the alternative-charger scenario",
    )
    parser.add_argument(
        "--charger-efficiency",
        type=float,
        default=EV_CHARGE_EFF,
        help="Charger efficiency; change only for the alternative-charger scenario",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/ev_simulation"),
    )
    parser.add_argument("--show-plot", action="store_true")
    args = parser.parse_args()

    if args.scenario == "scenario-1-baseline":
        run_scenario1_baseline(args, parser)
        return
    if args.scenario == "scenario-2-binary":
        run_scenario2_binary(args, parser)
        return
    if args.scenario == "scenario-3-staged":
        run_scenario3_staged(args, parser)
        return

    if args.scenario == "synthetic-reference":
        inputs = synthetic_inputs(args.date, args.timezone)
        moer_source = "synthetic"
        moer_region = "synthetic"
    else:
        if args.pv_xlsx is None:
            parser.error(f"--pv-xlsx is required for {args.scenario}")
        using_cache = bool(args.watttime_cache and args.watttime_cache.exists())
        try:
            token = "cache-only" if using_cache else get_watttime_token()
        except ValueError as exc:
            parser.error(str(exc))
        inputs = real_inputs(
            args.pv_xlsx,
            args.timezone,
            args.watttime_region,
            token,
            args.watttime_cache,
            args.moer_source,
        )
        moer_source = args.moer_source
        moer_region = args.watttime_region

        expected_day = SCENARIO_EXPECTED_DATES.get(args.scenario)
        input_day = inputs.index[0].date()
        if expected_day is not None and input_day != expected_day:
            parser.error(
                f"{args.scenario} expects data for {expected_day}, "
                f"but the workbook contains {input_day}"
            )

    if args.scenario != "alternative-charger" and (
        args.charger_power_kw != EV_CHARGE_RATE
        or args.charger_efficiency != EV_CHARGE_EFF
    ):
        parser.error(
            "Custom charger parameters are allowed only for alternative-charger"
        )

    paired_results: dict[str, pd.DataFrame] = {}
    summaries: dict[str, ScenarioSummary] = {}
    for controller in ("without-ems", "with-ems"):
        results, summary = simulate_ev(
            inputs,
            args.scenario,
            args.charger_power_kw,
            args.charger_efficiency,
            controller,
            moer_source,
            moer_region,
        )
        paired_results[controller] = results
        summaries[controller] = summary

    save_paired_outputs(paired_results, summaries, args.output_dir, args.show_plot)

    print(f"Scenario:              {args.scenario}")
    print(f"Simulation date:       {summaries['with-ems'].simulation_date}")
    print(f"MOER source:           {moer_source}")
    print(f"MOER region:           {moer_region}")
    print()
    print(f"{'Metric':<28}{'Without EMS':>18}{'With EMS':>18}")
    print("-" * 64)
    for label, field, suffix in (
        ("Final EV SoC", "final_soc_percent", "%"),
        ("Charging time", "charging_minutes", " min"),
        ("Charger input energy", "charger_input_energy_kwh", " kWh"),
        ("Average charging MOER", "average_charging_moer_lb_per_mwh", " lb/MWh"),
        (
            "MOER emissions proxy",
            "moer_weighted_emissions_proxy_lb",
            " lb CO2",
        ),
    ):
        left = getattr(summaries["without-ems"], field)
        right = getattr(summaries["with-ems"], field)
        if left is None or right is None:
            left_text = "n/a" if left is None else f"{left:.2f}{suffix}"
            right_text = "n/a" if right is None else f"{right:.2f}{suffix}"
        elif isinstance(left, float):
            left_text, right_text = f"{left:.2f}{suffix}", f"{right:.2f}{suffix}"
        else:
            left_text, right_text = f"{left}{suffix}", f"{right}{suffix}"
        print(f"{label:<28}{left_text:>18}{right_text:>18}")
    target_times = {}
    for controller, summary in summaries.items():
        target_times[controller] = (
            pd.Timestamp(summary.target_reached_at).strftime("%H:%M")
            if summary.target_reached_at
            else "not reached"
        )
    print(
        f"{'Target reached at':<28}"
        f"{target_times['without-ems']:>18}"
        f"{target_times['with-ems']:>18}"
    )
    print(f"Outputs:                   {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
