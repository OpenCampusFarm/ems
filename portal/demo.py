"""Synthetic data for the isolated local preview."""

import math
import time
from datetime import datetime
from itertools import chain

from power_flow import FARM_TIME
from telemetry_store import DAY, record_carbon, record_samples


def _sample(stamp):
    local = datetime.fromtimestamp(stamp, FARM_TIME)
    hour = local.hour + local.minute / 60
    season = 0.76 + 0.21 * math.cos(2 * math.pi * (local.timetuple().tm_yday - 172) / 365)
    weather = 0.79 + 0.18 * math.sin(stamp / (DAY * 0.72))
    solar = 4900 * season * weather * math.sin(math.pi * (hour - 7) / 11) if 7 <= hour <= 18 else 0
    ev = 2200 if 9 < hour < 12 else 1250 if 12 <= hour < 14 else 0
    return {
        "ts": stamp,
        "pv": solar, "grid": 600 + ev - solar, "ev": ev, "ev_current_a": ev / 240,
        "room": 44 + 1.4 * math.sin(hour / 4), "moer": 1040,
        "policy": "staged", "ev_state": "charging" if ev else "idle",
    }


def write_samples(path):
    now = time.time()
    older = range(366 * DAY, DAY, -3600)
    recent = range(DAY, -1, -300)
    record_samples((_sample(now - offset) for offset in chain(older, recent)), path, legacy=None, now=now)
    record_carbon(
        ((now - offset, 5, 0.08 + 0.04 * math.sin(offset / 7900), 0.05)
         for offset in chain(older, recent)),
        path, legacy=None, now=now,
    )
