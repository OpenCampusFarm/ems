"""Publish measured values for the local operator dashboard."""

import logging
import sqlite3
import time

from telemetry_store import numeric, record_samples

log = logging.getLogger(__name__)

EGAUGE_FIELDS = {
    "grid_l1_w": "grid_l1_power",
    "grid_l2_w": "grid_l2_power",
    "grid_l1_a": "grid_l1_current",
    "grid_l2_a": "grid_l2_current",
    "meter_humidity": "internal_humidity",
}


def _reading(value, fallback=None):
    return value if numeric(value) else fallback if numeric(fallback) else None


def _magnitude(value):
    return abs(value) if numeric(value) else None


def _grid_watts(power):
    watts = power.get("grid")
    if not numeric(watts) or watts == 0:
        return _reading(watts)
    buying, selling = power.get("grid_buying"), power.get("grid_selling")
    if buying is True and selling is not True:
        return abs(watts)
    if (selling is True and buying is not True) or (buying is False and selling is None):
        return -abs(watts)
    return None


def _battery_watts(power):
    watts = power.get("battery")
    if not numeric(watts):
        return None
    if power.get("battery_charging") is True:
        return -abs(watts)
    if power.get("battery_discharging") is True:
        return abs(watts)
    return watts


def build_sample(power, egauge, ev, room_temp, moer, policy, *, coolbot_setpoint_f=None):
    power, egauge, ev = power or {}, egauge or {}, ev or {}
    grid = _reading(egauge.get("grid_power"))
    solark_grid = _grid_watts(power)
    ev_w = _magnitude(egauge.get("evcharger_power"))
    ev_a = _reading(ev.get("current_a"))
    temperature_c = egauge.get("internal_temperature_c")
    meter_hz = next((
        rate for rate in (egauge.get("l1_frequency"), egauge.get("l2_frequency"))
        if numeric(rate) and rate > 0
    ), None)
    return {
        "ts": time.time(),
        "pv": power.get("pv"),
        "grid": _reading(grid, solark_grid),
        "grid_source": "eGauge" if grid is not None else "Sol-Ark" if solark_grid is not None else None,
        "ev": _reading(ev_w, ev.get("power_w")),
        "ev_source": "eGauge" if ev_w is not None else "OpenEVSE" if numeric(ev.get("power_w")) else None,
        "ev_current_a": ev_a,
        "ev_current_a_source": "OpenEVSE" if ev_a is not None else None,
        "battery_w": _battery_watts(power),
        "battery_soc": power.get("soc"),
        "inverter_load_w": power.get("load"),
        **{key: egauge.get(field) for key, field in EGAUGE_FIELDS.items()},
        "cooler_w": _magnitude(egauge.get("cooler_power")),
        "cooler_a": _magnitude(egauge.get("cooler_current")),
        **{f"{leg}_v": _reading(egauge.get(f"{leg}_voltage"), egauge.get(f"{leg}_rms_voltage"))
           for leg in ("l1", "l2", "l3")},
        "meter_temp_f": temperature_c * 1.8 + 32 if numeric(temperature_c) else None,
        "grid_hz": meter_hz,
        "room": room_temp,
        "coolbot_setpoint_f": coolbot_setpoint_f,
        "moer": moer,
        "ev_state": str(ev["state"]) if "state" in ev else None,
        "policy": policy,
    }


def publish(power, egauge, ev, room_temp, moer, policy, *, coolbot_setpoint_f=None):
    sample = build_sample(power, egauge, ev, room_temp, moer, policy,
                          coolbot_setpoint_f=coolbot_setpoint_f)
    try:
        record_samples((sample,))
    except (OSError, sqlite3.Error) as exc:
        log.warning("Could not publish portal telemetry: %s", exc)
