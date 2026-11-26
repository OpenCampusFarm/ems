"""Time windows and SVG chart data for the Portal power-flow views."""

from datetime import datetime, time, timedelta
from math import ceil
from typing import NamedTuple
from zoneinfo import ZoneInfo

from telemetry_store import DAY, numeric

SERIES = {"pv": "Solar production", "grid": "Grid power", "ev": "EV charging"}
CARBON_SERIES = {"avoided": "Estimated CO₂ avoided"}
FARM_TIME = ZoneInfo("America/Detroit")
HOUR = 3600
GAP = 12 * 60


class Period(NamedTuple):
    label: str
    duration: int
    summary: str
    tick_format: str
    bucket: int = 0


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


def build_chart(rows, end, period, *, series=SERIES, step=1000, unit_scale=1000):
    start = end - period.duration
    values = [row[key] for row in rows for key in series if numeric(row.get(key))]
    high = max(step, ceil(max(values, default=0) / step) * step)
    low = min(0, -ceil(-min(values, default=0) / step) * step)

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

    ticks = [(12, f"{high / unit_scale:g}"), (y(0), "0")]
    if low:
        ticks.append((164, f"{low / unit_scale:g}"))
    times = [
        datetime.fromtimestamp(start + period.duration * i / 4, FARM_TIME).strftime(period.tick_format)
        for i in range(5)
    ]
    return {
        "paths": paths, "readout": readout, "ticks": ticks, "available": bool(values), "times": times,
        "start": start, "end": end,
    }
