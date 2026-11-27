"""Time windows and SVG chart data for the Portal power-flow views."""

from datetime import datetime, time, timedelta
from math import ceil, floor
from typing import NamedTuple
from zoneinfo import ZoneInfo

from telemetry_store import DAY, numeric

FARM_TIME = ZoneInfo("America/Detroit")
HOUR = 3600
GAP = 12 * 60


class Period(NamedTuple):
    label: str
    duration: int
    summary: str
    tick_format: str
    bucket: int = 0


class ChartView(NamedTuple):
    label: str
    unit: str
    step: float
    precision: int
    series: dict
    include_zero: bool = True


VIEWS = {
    "power": ChartView("Power", "W", 1000, 0, {
        "pv": "Solar production",
        "grid": "Grid power",
        "ev": "EV circuit power",
        "inverter_load_w": "Inverter load",
        "battery_w": "Stationary battery power",
        "cooler_w": "Cooler circuit power",
        "grid_l1_w": "Grid L1 power",
        "grid_l2_w": "Grid L2 power",
    }),
    "voltage": ChartView("Voltage", "V", 25, 1, {
        "l1_v": "L1 voltage",
        "l2_v": "L2 voltage",
        "l3_v": "L3 voltage (unused)",
    }),
    "current": ChartView("Current", "A", 2, 1, {
        "ev_current_a": "EV charge current",
        "grid_l1_a": "Grid L1 current",
        "grid_l2_a": "Grid L2 current",
        "cooler_a": "Cooler current",
    }),
    "percent": ChartView("Percent", "%", 100, 1, {
        "battery_soc": "Stationary battery charge",
        "meter_humidity": "eGauge humidity",
    }),
    "temperature": ChartView("Meter temperature", "°F", 5, 1, {
        "meter_temp_f": "eGauge temperature",
    }, include_zero=False),
    "frequency": ChartView("Frequency", "Hz", 0.05, 3, {
        "grid_hz": "Grid frequency",
    }, include_zero=False),
}
CARBON_VIEW = ChartView("Estimated CO₂ avoided", "kg/h", 0.25, 3, {"avoided": "Estimated CO₂ avoided"})
DEFAULT_VIEW = "power"

METER_SOURCES = {
    key: "Sol-Ark" if key in ("pv", "inverter_load_w", "battery_w", "battery_soc") else "eGauge"
    for view in VIEWS.values() for key in view.series
}
# Older EV telemetry was collected directly from the charger.
METER_SOURCES.update(ev="OpenEVSE", ev_current_a="OpenEVSE")


PERIODS = {
    "minutes": Period("Minutes", HOUR, "Last 60 min · Recorded samples", "%H:%M"),
    "hours": Period("Hours", 12 * HOUR, "Last 12 hours · Hourly mean", "%H:%M", HOUR),
    "day": Period("Day", DAY, "Last 24 hours · Recorded samples", "%H:%M"),
    "week": Period("Week", 7 * DAY, "Last 7 days · Hourly mean", "%m/%d", HOUR),
    "month": Period("Month", 30 * DAY, "Last 30 days · Daily mean", "%m/%d", DAY),
    "quarter": Period("3 months", 90 * DAY, "Last 90 days · Daily mean", "%m/%d", DAY),
    "year": Period("Year", 365 * DAY, "Last 365 days · Weekly mean", "%b %Y", 7 * DAY),
}
DEFAULT_PERIOD = "day"


def chart_buckets(start, end, size):
    if not size:
        return []
    spans = []
    if size == HOUR:
        boundary = int(start) // HOUR * HOUR
        while boundary < end:
            spans.append((max(start, boundary), min(end, boundary + HOUR)))
            boundary += HOUR
        return spans

    day = datetime.fromtimestamp(start, FARM_TIME).date()
    if size == 7 * DAY:
        day -= timedelta(days=day.weekday())
    step = timedelta(days=size // DAY)
    boundary = datetime.combine(day, time.min, FARM_TIME).timestamp()
    while boundary < end:
        day += step
        next_boundary = datetime.combine(day, time.min, FARM_TIME).timestamp()
        spans.append((max(start, boundary), min(end, next_boundary)))
        boundary = next_boundary
    return spans


def build_chart(rows, end, period, view):
    start = end - period.duration
    series, step = view.series, view.step
    values = [row[key] for row in rows for key in series if numeric(row.get(key))]
    if view.include_zero:
        low = min(0, floor(min(values, default=0) / step) * step)
        high = max(step, ceil(max(values, default=0) / step) * step)
    else:
        low = floor(min(values, default=0) / step) * step
        high = max(low + step, ceil(max(values, default=0) / step) * step)

    def y(value):
        return 12 + (high - value) * 152 / (high - low)

    paths = {}
    readout = {}
    for key in series:
        segments = []
        points = []
        previous = None
        for row in rows:
            value = row.get(key)
            if not numeric(value):
                previous = None
                continue
            stamp = row["ts"]
            # Aggregated rows include explicit empty buckets. Raw rows may have time gaps.
            if previous is None or (not period.bucket and stamp - previous > GAP):
                segments.append([])
            x = 52 + (stamp - start) * 632 / period.duration
            point = (round(x, 2), round(y(value), 2))
            segments[-1].append(point)
            points.append({
                "x": point[0], "y": point[1], "ts": stamp, "value": value,
                "start": row.get("bucket_start"), "end": row.get("bucket_end"),
            })
            previous = stamp
        paths[key] = segments
        readout[key] = points

    ticks = [(12, f"{high:g}"), (164, f"{low:g}")]
    if low < 0 < high:
        ticks.insert(1, (y(0), "0"))
    elif high - low > step:
        middle = (high + low) / 2
        ticks.insert(1, (y(middle), f"{middle:g}"))
    times = [
        datetime.fromtimestamp(start + period.duration * i / 4, FARM_TIME).strftime(period.tick_format)
        for i in range(5)
    ]
    return {
        "paths": paths, "readout": readout, "ticks": ticks, "available": bool(values), "times": times,
        "start": start, "end": end,
    }
