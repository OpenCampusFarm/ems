"""Pure current selection and OpenEVSE claim construction; no I/O or .env.

The binary/staged rules are integrated from Charlotte commit 9ce3af9.
These policy values are not verification of the installed circuit rating.
"""

import math

EV_CHARGER_VOLTS = 240
EV_CHARGE_AMPS = 12
EV_STAGED_AMPS = (6, 20, 34, 48)
MIN_CURRENT_AMPS = 6


def excess_pv_watts(pv_w, load_w, ev_w):
    return pv_w - max(load_w - ev_w, 0.0)


def choose_ev_amps(excess_w: float, mode: str) -> int:
    if mode not in ("binary", "staged"):
        raise ValueError("mode must be binary or staged")
    if (
        isinstance(excess_w, bool)
        or not isinstance(excess_w, (int, float))
        or not math.isfinite(excess_w)
    ):
        raise ValueError("excess power must be finite")
    levels = (EV_CHARGE_AMPS,) if mode == "binary" else EV_STAGED_AMPS
    return max((amps for amps in levels if amps * EV_CHARGER_VOLTS <= excess_w), default=0)


def claim_body(enabled, amps=None):
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a boolean")
    if (
        enabled
        and amps is not None
        and (
            isinstance(amps, bool)
            or not isinstance(amps, int)
            or amps < MIN_CURRENT_AMPS
        )
    ):
        raise ValueError(
            "charging current must be integer amps >= 6; use disabled to stop"
        )
    result = {"state": "active" if enabled else "disabled", "auto_release": True}
    if enabled and amps is not None:
        result["charge_current"] = amps
    return result
