"""Publish measured values for the local operator dashboard."""

import logging
import sqlite3
import time

from telemetry_store import record_samples

log = logging.getLogger(__name__)


def publish(power, egauge, ev, room_temp, moer, policy):
    sample = {
        "ts": time.time(),
        "pv": power["pv"],
        "grid": egauge["grid"] if egauge else None,
        "ev": ev["power_w"] if ev else None,
        "ev_current_a": ev.get("current_a") if ev else None,
        "room": room_temp,
        "moer": moer,
        "ev_state": str(ev["state"]) if ev else None,
        "policy": policy,
    }
    try:
        record_samples((sample,))
    except (OSError, sqlite3.Error) as exc:
        log.warning("Could not publish portal telemetry: %s", exc)
