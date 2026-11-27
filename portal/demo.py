"""Synthetic data for the isolated local preview."""

import math
import time
from datetime import datetime
from itertools import chain

from coolbot_monitor import CoolBotReading
from outdoor_monitor import OutdoorReading
from power_flow import FARM_TIME
from telemetry_store import DAY, record_carbon, record_samples, record_solark_workdata


def _sample(stamp):
    local = datetime.fromtimestamp(stamp, FARM_TIME)
    hour = local.hour + local.minute / 60
    season = 0.76 + 0.21 * math.cos(2 * math.pi * (local.timetuple().tm_yday - 172) / 365)
    weather = 0.79 + 0.18 * math.sin(stamp / (DAY * 0.72))
    solar = 4900 * season * weather * math.sin(math.pi * (hour - 7) / 11) if 7 <= hour <= 18 else 0
    ev = 2200 if 9 < hour < 12 else 1250 if 12 <= hour < 14 else 0
    cooler = 420 if int(stamp / 600) % 3 else 0
    load = 180 + cooler + ev
    battery = -0.35 * max(solar - load, 0) if solar > load else min(110, load - solar)
    grid = load - solar - battery
    return {
        "ts": stamp,
        "pv": solar, "grid": grid, "ev": ev, "ev_current_a": ev / 240,
        "grid_source": "eGauge", "ev_source": "eGauge", "ev_current_a_source": "OpenEVSE",
        "solark_grid_w": grid * .98, "solark_generator_w": 0, "solark_micro_w": 0,
        "solark_status": '{"Generator configured":false,"Microinverter configured":false}',
        "inverter_load_w": load, "battery_w": battery, "cooler_w": cooler,
        "grid_l1_w": grid * .46, "grid_l2_w": grid * .54,
        "l1_v": 123 + 1.1 * math.sin(hour / 3), "l2_v": 124 + .8 * math.sin(hour / 2),
        "l3_v": 1.9, "grid_l1_a": abs(grid * .46 / 123), "grid_l2_a": abs(grid * .54 / 124),
        "cooler_a": cooler / 123, "battery_soc": 47 + 24 * math.sin((hour - 9) / 5),
        "meter_humidity": 38 + 4 * math.cos(hour / 3), "meter_temp_f": 59 + 3 * math.sin(hour / 5),
        "grid_hz": 60 + .021 * math.sin(stamp / 850),
        "moer": 1040,
        "policy": "staged", "ev_state": "charging" if ev else "idle",
    }


def sample_coolbot():
    now = time.time()
    hour = datetime.fromtimestamp(now, FARM_TIME).hour
    return CoolBotReading(44 + 1.4 * math.sin(hour / 4), 45, now)


def sample_outdoor():
    return OutdoorReading(42.1, False)


def write_samples(path):
    now = time.time()
    older = range(366 * DAY, DAY, -3600)
    recent = range(DAY, -1, -300)
    latest = _sample(now)
    snapshots = (
        ("registers", now, [
            {"name": "Grid", "value": latest["grid"], "unit": "W"},
            {"name": "L1 Voltage", "value": latest["l1_v"], "unit": "V"},
            {"name": "Internal Humidity", "value": latest["meter_humidity"], "unit": "%"},
        ]),
        ("channels", now, [
            {"name": "L1 · Frequency", "value": latest["grid_hz"], "unit": "Hz"},
            {"name": "S1 · Normal", "value": latest["grid_l1_a"], "unit": "A"},
        ]),
    )
    record_samples((_sample(now - offset) for offset in chain(older, recent)),
                   path, legacy=None, now=now, egauge_snapshots=snapshots)
    record_carbon(
        ((now - offset, 5, 0.08 + 0.04 * math.sin(offset / 7900), 0.05,
          0.015 * max(0, math.sin(offset / 7900)) if offset <= DAY else None)
         for offset in chain(older, recent)),
        path, legacy=None, now=now,
    )
    for offset in range(DAY, -1, -300):
        stamp = now - offset
        record_solark_workdata(stamp, {
            "BatteryVolt(V)/183": 52.1 + .4 * math.sin(offset / 11000),
            "BatteryTemp(℃)/182": 9 + math.sin(offset / 7900),
            "GridBuyToday(kWh)/76": 4.4 * (DAY - offset) / DAY,
            "Fac(Hz)/79": 60 + .02 * math.sin(offset / 400),
            "RunStatus/59": "2",
        }, path, legacy=None, now=now)
