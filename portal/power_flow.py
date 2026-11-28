"""Time windows and SVG chart data for the Portal power-flow views."""

from datetime import datetime, time, timedelta
from math import ceil, floor
from typing import NamedTuple
from zoneinfo import ZoneInfo

from telemetry_store import DAY, numeric

FARM_TIME = ZoneInfo("America/Detroit")
HOUR = 3600
WEEK = 7 * DAY
YEAR = 365 * DAY
GAP = 12 * 60


class Period(NamedTuple):
    label: str
    duration: float
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


OVERVIEW_VIEW = ChartView("Power flow", "W", 1000, 0, {
    "pv": "Solar production",
    "solark_grid_w": "Grid power",
    "ev": "EV circuit power",
})
SOLARK_VIEWS = {
    "power": ChartView("Inverter power", "W", 1000, 0, {
        "pv": "Solar production",
        "solark_grid_w": "Grid power",
        "inverter_load_w": "Inverter load",
        "battery_w": "Stationary battery power",
        "solark_generator_w": "Generator power",
        "solark_micro_w": "Microinverter power",
    }),
    "charge": ChartView("Battery charge", "%", 100, 1, {
        "battery_soc": "Stationary battery charge",
    }),
}
EGAUGE_VIEW = ChartView("Grid and circuit power", "W", 1000, 0, {
    "egauge_grid_w": "Grid power",
    "grid_l1_w": "Grid L1 power",
    "grid_l2_w": "Grid L2 power",
    "cooler_w": "Cooler circuit power",
    "egauge_ev_w": "EV circuit power",
})
CARBON_VIEW = ChartView("Estimated CO₂ avoided", "kg/h", 0.25, 3, {
    "fan_avoided": "Ventilation fans",
    "ev_avoided": "EV charging",
})


PERIODS = {
    "15min": Period("15 min", 15 * 60, "Last 15 min · Recorded samples", "%H:%M"),
    "minutes": Period("1 hour", HOUR, "Last 1 hour · Recorded samples", "%H:%M"),
    "3hours": Period("3 hours", 3 * HOUR, "Last 3 hours · Recorded samples", "%H:%M"),
    "hours": Period("12 hours", 12 * HOUR, "Last 12 hours · Hourly mean", "%H:%M", HOUR),
    "day": Period("24 hours", DAY, "Last 24 hours · Recorded samples", "%H:%M"),
    "week": Period("7 days", 7 * DAY, "Last 7 days · Hourly mean", "%m/%d", HOUR),
    "month": Period("30 days", 30 * DAY, "Last 30 days · Daily mean", "%m/%d", DAY),
    "quarter": Period("90 days", 90 * DAY, "Last 90 days · Daily mean", "%m/%d", DAY),
    "year": Period("1 year", YEAR, "Last 1 year · Weekly mean", "%b %Y", WEEK),
    "3years": Period("3 years", 3 * YEAR, "Last 3 years · Weekly mean", "%b %Y", WEEK),
    "5years": Period("5 years", 5 * YEAR, "Last 5 years · Weekly mean", "%b %Y", WEEK),
    "max": Period("Max", DAY, "All recorded data", "%H:%M"),
}
DEFAULT_PERIOD = "day"


def max_history_period(first_sample, end):
    if first_sample is None:
        return PERIODS["max"]
    duration = max(60, end - first_sample)
    for limit, bucket, summary, tick_format in (
        (15 * 60, 0, "Recorded samples", "%H:%M:%S"),
        (DAY, 0, "Recorded samples", "%H:%M"),
        (WEEK, HOUR, "Hourly mean", "%m/%d %H:%M"),
        (90 * DAY, DAY, "Daily mean", "%m/%d"),
        (5 * YEAR, WEEK, "Weekly mean", "%b %Y"),
        (float("inf"), 2 * WEEK, "Two-week mean", "%b %Y"),
    ):
        if duration <= limit:
            return Period("Max", duration, f"All recorded data · {summary}", tick_format, bucket)


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
    if size % WEEK == 0:
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
    low = min(0, floor(min(values, default=0) / step) * step)
    high = max(step, ceil(max(values, default=0) / step) * step)
    if values and not view.include_zero:
        low = floor(min(values) / step) * step
        high = max(low + step, ceil(max(values) / step) * step)

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
