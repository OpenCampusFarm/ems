"""Synthetic data for the isolated local preview."""

import json
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo


def write_samples(path):
    now = time.time()
    rows = []
    farm_time = ZoneInfo("America/Detroit")
    for index in range(289):
        stamp = now - (288 - index) * 300
        local = datetime.fromtimestamp(stamp, farm_time)
        hour = local.hour + local.minute / 60
        solar = 4900 * math.sin(math.pi * (hour - 7) / 11) if 7 <= hour <= 18 else 0
        solar *= 0.91 + 0.06 * math.sin(index * 0.26)
        ev = 2200 if 9 < hour < 12 else 1250 if 12 <= hour < 14 else 0
        rows.append({
            "ts": stamp,
            "pv": solar, "grid": 600 + ev - solar, "ev": ev,
            "room": 44 + 1.4 * math.sin(index / 35), "moer": 1040,
            "policy": "staged", "ev_state": "charging" if ev else "idle",
        })
    path.write_text(json.dumps(rows))
