"""Poll the farm's outdoor sensor without blocking dashboard requests."""

import time
from threading import Thread
from typing import NamedTuple

from outdoor_temperature import get_outdoor_temp_api, read_outdoor_temp

POLL_SECONDS = 30
WEATHER_REFRESH_SECONDS = 900
WEATHER_RETRY_SECONDS = 60


class OutdoorReading(NamedTuple):
    temp_f: float
    estimated: bool


class OutdoorMonitor:
    def __init__(self):
        self._reading = None
        self._weather = None
        self._next_weather_poll = 0.0

    def read(self):
        return self._reading

    def start(self):
        Thread(target=self._poll, name="outdoor-monitor", daemon=True).start()

    def _poll(self):
        while True:
            self._refresh()
            time.sleep(POLL_SECONDS)

    def _refresh(self):
        temp_f = read_outdoor_temp()
        if temp_f is not None:
            self._reading = OutdoorReading(temp_f, False)
            return

        if time.monotonic() >= self._next_weather_poll:
            temp_f = get_outdoor_temp_api()
            self._weather = OutdoorReading(temp_f, True) if temp_f is not None else None
            interval = WEATHER_REFRESH_SECONDS if self._weather else WEATHER_RETRY_SECONDS
            self._next_weather_poll = time.monotonic() + interval
        self._reading = self._weather
