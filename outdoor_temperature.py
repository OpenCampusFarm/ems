"""Read the Campus Farm's outdoor temperature in Fahrenheit."""

from __future__ import annotations

import json
import logging
from math import isfinite
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

log = logging.getLogger(__name__)
SENSORS = Path("/sys/bus/w1/devices")
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
FARM_LAT = 42.3005
FARM_LON = -83.6655


def read_outdoor_temp() -> float | None:
    try:
        sensor = next(SENSORS.glob("28-*/w1_slave"), None)
        if sensor is None:
            return None
        lines = sensor.read_text().splitlines()
        if len(lines) < 2 or not lines[0].endswith("YES"):
            return None
        _, separator, value = lines[1].partition("t=")
        if separator:
            temp_c = int(value) / 1000
            return round(temp_c * 9 / 5 + 32, 2)
    except (OSError, ValueError) as error:
        log.warning("[Sensor] Outdoor temperature read failed: %s", error)
    return None


def get_outdoor_temp_api() -> float | None:
    params = urlencode({
        "latitude": FARM_LAT,
        "longitude": FARM_LON,
        "current": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "forecast_days": 1,
    })
    try:
        with urlopen(f"{WEATHER_URL}?{params}", timeout=10) as response:
            temp_f = float(json.load(response)["current"]["temperature_2m"])
        if isfinite(temp_f):
            return temp_f
    except (OSError, ValueError, TypeError, KeyError) as error:
        log.warning("[Open-Meteo] Outdoor temperature fetch failed: %s", error)
    return None
