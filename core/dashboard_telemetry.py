"""Publish recent measured values for the local operator dashboard."""

import json
import logging
import math
from datetime import datetime, timezone

from system_state import TELEMETRY

log = logging.getLogger(__name__)
HISTORY_LIMIT = 290


def _number(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def publish(power, egauge, ev, room_temp, moer, policy):
    sample = {
        "ts": datetime.now(timezone.utc).timestamp(),
        "pv": _number(power["pv"]),
        "grid": _number(egauge["grid"]) if egauge else None,
        "ev": _number(ev["power_w"]) if ev else None,
        "room": _number(room_temp),
        "moer": _number(moer),
        "ev_state": str(ev["state"]) if ev else None,
        "policy": policy,
    }
    temporary = TELEMETRY.with_suffix(".json.tmp")
    try:
        try:
            history = json.loads(TELEMETRY.read_text())
        except (FileNotFoundError, ValueError):
            history = []
        if not isinstance(history, list):
            history = []
        temporary.write_text(json.dumps([*history[-(HISTORY_LIMIT - 1):], sample]))
        temporary.replace(TELEMETRY)
    except OSError as exc:
        log.warning("Could not publish portal telemetry: %s", exc)
