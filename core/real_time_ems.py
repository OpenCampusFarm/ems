"""
Campus Farm EMS — real-time control loop.

Decision logic (every POLL_INTERVAL seconds):
  1. Read SolArk inverter: PV watts, grid watts
  2. Read WattTime grid MOER (lbs CO2/MWh)
  3. "Clean" if PV is producing (>= PV_MIN_WATTS) OR grid MOER < CO2_CLEAN_THRESHOLD
  4. If clean  → CoolBot setpoint = SETPOINT_COOLTH
     If dirty  → CoolBot setpoint = SETPOINT_ECON
  5. EV charging (EV_MODE) only uses excess PV, never grid or night charging, on any day.
     The truck needs little energy and stops by itself when full.
       binary: charge at EV_CHARGE_AMPS while excess PV covers it, otherwise block
       staged: charge at the highest of EV_STAGED_AMPS that excess PV covers, otherwise block
  EV charging is controlled on the OpenEVSE charger via an EVSE claim (soft control),
  so the charger's LCD manual override still wins.
"""

import logging
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from Loads.coolbot import change_setpoint, get_room_temp
from Loads.openevse import get_status as get_ev_status
from Loads.openevse import release_claim, set_charging
from egauge_client import EGaugeClient
from solArk_inverter import get_inverter_data

load_dotenv(Path(__file__).parent / ".env")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── WattTime ──────────────────────────────────────────────────────────────────
WT_USERNAME = os.getenv("WT_USERNAME")
WT_PASSWORD = os.getenv("WT_PASSWORD")
WT_REGION = os.getenv("WT_REGION", "MISO_DETROIT")
WT_BASE = "https://api.watttime.org"

CO2_CLEAN_THRESHOLD = 1400.0
PV_MIN_WATTS = 500.0

_wt_token: str | None = None
_wt_token_ts: float = 0.0
_WT_TOKEN_TTL = 25 * 60

# ── CoolBot setpoints ─────────────────────────────────────────────────────────
SETPOINT_COOLTH = 45  # °F — low setpoint (clean energy)
SETPOINT_ECON = 50  # °F — high setpoint (dirty energy)
SETPOINT_DEFAULT = 48  # °F — neutral fallback

# ── EV charging ───────────────────────────────────────────────────────────────
EV_MODE = (os.getenv("EV_MODE") or "binary").lower()  # "binary" or "staged"
if EV_MODE not in ("binary", "staged"):
    raise ValueError(f"EV_MODE must be binary or staged, not {EV_MODE!r}")

EV_CHARGER_VOLTS = 240
EV_CHARGE_AMPS = 12  # A, binary — chosen in core/binary_charge_speed.py
EV_STAGED_AMPS = (6, 20, 34, 48)  # A, staged — simulation.py charger "openevse-5-6a"

# ── Polling ───────────────────────────────────────────────────────────────────
POLL_INTERVAL = 300  # seconds


# ── Generic retry helper ──────────────────────────────────────────────────────


def _retry(fn, retries: int = 3, label: str = ""):
    name = label or fn.__name__
    for attempt in range(1, retries + 1):
        try:
            result = fn()
            if result is not None:
                return result
        except Exception as exc:
            log.warning("[%s] attempt %d/%d failed: %s", name, attempt, retries, exc)
        if attempt < retries:
            time.sleep(2)
    log.error("[%s] unavailable after %d attempts", name, retries)
    return None


# ── WattTime client ───────────────────────────────────────────────────────────


def _get_wt_token() -> str | None:
    global _wt_token, _wt_token_ts
    if _wt_token and (time.time() - _wt_token_ts) < _WT_TOKEN_TTL:
        return _wt_token
    try:
        resp = requests.get(
            f"{WT_BASE}/login",
            auth=(WT_USERNAME, WT_PASSWORD),
            timeout=15,
        )
        resp.raise_for_status()
        _wt_token = resp.json()["token"]
        _wt_token_ts = time.time()
        log.info("[WattTime] Token refreshed")
        return _wt_token
    except Exception as exc:
        log.error("[WattTime] Login failed: %s", exc)
        return None


def get_grid_moer() -> float | None:
    global _wt_token
    token = _get_wt_token()
    if not token:
        return None
    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=10)
        resp = requests.get(
            f"{WT_BASE}/v3/historical",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "region": WT_REGION,
                "signal_type": "co2_moer",
                "start": start.strftime("%Y-%m-%dT%H:%MZ"),
                "end": now.strftime("%Y-%m-%dT%H:%MZ"),
            },
            timeout=15,
        )
        if resp.status_code == 401:
            _wt_token = None
            return None
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if data:
            moer = float(data[-1]["value"])
            log.info("[WattTime] MOER = %.1f lbs CO2/MWh", moer)
            return moer
    except Exception as exc:
        log.error("[WattTime] MOER fetch failed: %s", exc)
    return None


# ── SolArk inverter ───────────────────────────────────────────────────────────


def get_power_data() -> dict | None:
    raw = _retry(get_inverter_data, retries=3, label="SolArk")
    if not raw:
        return None
    return {
        "pv": float(raw.get("Solar W", 0)),
        "grid": float(raw.get("Grid W", 0)),
        "load": float(raw.get("Consumed W", 0)),
        "soc": float(raw.get("soc", 0)),
    }


# ── eGauge meter (measurement only) ───────────────────────────────────────────

_egauge: EGaugeClient | None = None


def get_egauge_data() -> dict | None:
    global _egauge
    if _egauge is None:
        _egauge = EGaugeClient()
    raw = _egauge.get_all_values()
    # Grid: negative = exporting. CT polarity makes the load readings negative,
    # so report them as positive consumption.
    return {
        "grid": float(raw["grid_power"]),
        "cooler": abs(float(raw["cooler_power"])),
        "ev": abs(float(raw["evcharger_power"])),
    }


# ── EMS decision ─────────────────────────────────────────────────────────────

_current_setpoint: int = SETPOINT_DEFAULT


def excess_pv_watts(pv_w: float, load_w: float, ev_w: float) -> float:
    """PV left after site loads. The Sol-Ark load includes the EV, so take the EV back out
    or charging would count against itself and switch the charger off."""
    site_load_w = max(load_w - ev_w, 0.0)
    return pv_w - site_load_w


def choose_ev_amps(excess_w: float) -> int:
    """Charging current for this cycle, 0 to block. A level is only used if excess PV
    covers its full power (amps × volts), so the truck never needs the grid."""
    levels = (EV_CHARGE_AMPS,) if EV_MODE == "binary" else EV_STAGED_AMPS
    covered = [amps for amps in levels if amps * EV_CHARGER_VOLTS <= excess_w]
    return max(covered, default=0)


def run_ems_cycle() -> None:
    global _current_setpoint

    power = get_power_data()
    if power is None:
        log.warning("[EMS] Inverter data unavailable — skipping cycle")
        return

    moer = get_grid_moer()
    room_temp = _retry(get_room_temp, retries=2, label="CoolBot room temp")
    # Local-network device: no retries, the next cycle tries again.
    ev_data = get_ev_status()
    egauge = _retry(get_egauge_data, retries=2, label="eGauge")

    pv_w = power["pv"]
    grid_w = power["grid"]
    load_w = power["load"]

    log.info(
        "[EMS] %s | PV=%.0fW  Grid=%.0fW  Load=%.0fW  MOER=%s  Room=%s",
        datetime.now().strftime("%H:%M:%S"),
        pv_w,
        grid_w,
        load_w,
        f"{moer:.0f}" if moer is not None else "N/A",
        f"{room_temp:.1f}°F" if room_temp is not None else "N/A",
    )
    if egauge is not None:
        log.info(
            "[eGauge] Grid=%.0fW (neg=export)  Cooler=%.0fW  EV=%.0fW",
            egauge["grid"],
            egauge["cooler"],
            egauge["ev"],
        )
    else:
        log.warning("[eGauge] unavailable — measurement skipped")
    if ev_data is not None:
        log.info(
            "[EV] state=%s connected=%s power=%.0fW",
            ev_data["state"],
            ev_data["connected"],
            ev_data["power_w"],
        )

    pv_producing = pv_w >= PV_MIN_WATTS
    grid_clean = moer is not None and moer < CO2_CLEAN_THRESHOLD
    energy_clean = pv_producing or grid_clean

    if moer is None:
        log.warning("[EMS] WattTime unavailable — using PV-only signal")

    log.info(
        "[EMS] PV producing=%s  Grid clean=%s  → energy_clean=%s",
        pv_producing,
        grid_clean,
        energy_clean,
    )

    new_setpoint = SETPOINT_COOLTH if energy_clean else SETPOINT_ECON
    if new_setpoint != _current_setpoint:
        change_setpoint(new_setpoint)
        _current_setpoint = new_setpoint
        log.info("[CoolBot] Setpoint → %d°F", new_setpoint)
    else:
        log.info("[CoolBot] Setpoint unchanged at %d°F", _current_setpoint)

    if ev_data is None:
        log.info("[EV] OpenEVSE unavailable — skipping charging decision")
        return

    excess_w = excess_pv_watts(pv_w, load_w, ev_data["power_w"])
    ev_amps = choose_ev_amps(excess_w)
    charge_ev = ev_amps > 0

    try:
        set_charging(charge_ev, ev_amps)
        if charge_ev:
            log.info("[EV] %s: charging at %d A (excess PV %.0fW)", EV_MODE, ev_amps, excess_w)
        else:
            log.info("[EV] %s: charging blocked (excess PV %.0fW)", EV_MODE, excess_w)
    except Exception as exc:
        log.error("[EV] set_charging(%s, %d) failed: %s", charge_ev, ev_amps, exc)


# ── Entry point ───────────────────────────────────────────────────────────────


_stop = threading.Event()


def _handle_signal(signum, _frame) -> None:
    if _stop.is_set():
        raise KeyboardInterrupt  # second signal: stop waiting for the current cycle
    log.info(
        "[EMS] %s received — finishing current cycle, then shutting down "
        "(signal again to force)",
        signal.Signals(signum).name,
    )
    _stop.set()


def _sleep_interruptible(seconds: float) -> None:
    # Wait in short slices: on Windows a long Event.wait() isn't interrupted by
    # Ctrl-C until it times out.
    deadline = time.monotonic() + seconds
    while not _stop.is_set() and (remaining := deadline - time.monotonic()) > 0:
        _stop.wait(min(1.0, remaining))


def shutdown() -> None:
    """Hand EV control back to the charger so a stopped EMS never leaves it blocked."""
    try:
        release_claim()
        log.info("[EV] Claim released")
    except (requests.ConnectionError, requests.Timeout):
        log.info("[EV] OpenEVSE not reachable — nothing to release")
    except Exception as exc:
        log.warning("[EV] Failed to release claim: %s", exc)


def main() -> None:
    # SIGBREAK only exists on Windows (Ctrl-Break / console close).
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _handle_signal)
    log.info("[EMS] Starting up...")
    log.info("Campus Farm EMS starting — poll every %ds", POLL_INTERVAL)
    try:
        while not _stop.is_set():
            try:
                run_ems_cycle()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                log.error("Unexpected error: %s", exc, exc_info=True)
            _sleep_interruptible(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("[EMS] Shutting down...")
        shutdown()
        log.info("[EMS] Stopped")
