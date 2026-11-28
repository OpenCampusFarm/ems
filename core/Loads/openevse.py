"""
OpenEVSE charger control over the local HTTP API (mDNS: http://openevse.local).

Charging is controlled with the *EVSE Claim* API (soft control). A claim made
through the HTTP API has priority 500. The charger's LCD and web UI use the
manual override at priority 1000. Resuming a pause from the Portal clears that
override after setting the new charging current.

  POST   /claims/<client_id>   {"state": "active" | "disabled", "auto_release": true}
                              Active claims can also specify "charge_current" in amps.
  DELETE /claims/<client_id>   release the claim
"""

from __future__ import annotations

import logging
from math import isfinite
import os
from pathlib import Path
import time

import requests
from dotenv import load_dotenv

from ev_current_policy import MIN_CURRENT_AMPS, claim_body

load_dotenv(Path(__file__).parent.parent / ".env")

log = logging.getLogger(__name__)

OPENEVSE_URI = os.environ.get("OPENEVSE_URI", "http://openevse.local").rstrip("/")
OPENEVSE_USER = os.environ.get("OPENEVSE_USER")
OPENEVSE_PASSWORD = os.environ.get("OPENEVSE_PASSWORD")
# Numeric client id for our claim (the firmware parses it with toInt(); 0 is
# what a non-numeric id like "client" resolves to, so use a non-zero number).
OPENEVSE_CLAIM_ID = int(os.environ.get("OPENEVSE_CLAIM_ID", "20"))
CLAIM_PATH = f"/claims/{OPENEVSE_CLAIM_ID}"

TIMEOUT = 10  # seconds
CLAIM_TIMEOUT = 3  # seconds per HTTP request
CLAIM_ATTEMPTS = 3
CLAIM_RETRY_DELAY = 1  # second

# /status "state" codes
STATE_NOT_CONNECTED = 1
STATE_CONNECTED = 2
STATE_CHARGING = 3
STATE_SLEEPING = 254
STATE_DISABLED = 255


def _request(method: str, path: str, *, timeout=TIMEOUT, **kwargs) -> requests.Response:
    auth = (OPENEVSE_USER, OPENEVSE_PASSWORD) if OPENEVSE_USER else None
    resp = requests.request(
        method, f"{OPENEVSE_URI}{path}", auth=auth, timeout=timeout, **kwargs
    )
    resp.raise_for_status()
    return resp


def _read_claim() -> dict | None:
    try:
        claim = _request("GET", CLAIM_PATH, timeout=CLAIM_TIMEOUT).json()
    except requests.HTTPError as error:
        if error.response is not None and error.response.status_code == 404:
            return None
        raise
    return claim if isinstance(claim, dict) else None


def get_requested_current() -> int | None:
    """Read the EMS claim or the charger's pilot current when no rate is claimed."""
    try:
        claim = _read_claim()
        if claim is not None:
            if claim.get("state") == "disabled":
                return 0
            amps = claim.get("charge_current")
            if type(amps) is int and amps >= MIN_CURRENT_AMPS:
                return amps

        status = _request("GET", "/status", timeout=3).json()
        pilot = status.get("pilot") if isinstance(status, dict) else None
        return pilot if type(pilot) is int and pilot >= MIN_CURRENT_AMPS else None
    except requests.RequestException as error:
        log.warning("[EV] Unable to read requested current: %s", error)
        return None


def get_status() -> dict | None:
    """Return the charger status, or None if it can't be read."""
    try:
        raw = _request("GET", "/status").json()
        state = raw.get("state")
        try:
            milliamps = float(raw["amp"])
        except (KeyError, TypeError, ValueError):
            milliamps = -1.0
        status = {
            "state": state,
            "connected": bool(raw.get("vehicle"))
            or state in (STATE_CONNECTED, STATE_CHARGING),
            "charging": state == STATE_CHARGING,
            "current_a": milliamps / 1000 if isfinite(milliamps) and milliamps >= 0 else None,
            "power_w": float(raw.get("power", 0) or 0),
        }
        log.info(
            "OpenEVSE state=%s connected=%s power=%.0fW",
            state,
            status["connected"],
            status["power_w"],
        )
        return status
    except (requests.ConnectionError, requests.Timeout):
        log.info("OpenEVSE not reachable at %s (not on the farm network?)", OPENEVSE_URI)
        return None
    except Exception as e:
        log.error("Error fetching OpenEVSE status: %s", e)
        return None


def set_charging(enabled: bool, amps: int | None = None) -> dict:
    """Set and confirm the EMS claim, retrying if the charger hasn't stored it."""
    body = claim_body(enabled, amps)
    for attempt in range(1, CLAIM_ATTEMPTS + 1):
        post_error = None
        try:
            _request("POST", CLAIM_PATH, timeout=CLAIM_TIMEOUT, json=body)
        except requests.RequestException as error:
            post_error = error

        # Read the EMS claim even if the HTTP acknowledgment was lost. The
        # pilot can differ when a higher-priority charger limit is in effect.
        try:
            claim = _read_claim()
            if claim is not None and all(claim.get(key) == value for key, value in body.items()):
                return claim
            last_error = post_error or requests.RequestException("OpenEVSE claim readback did not match")
        except requests.RequestException as error:
            last_error = post_error or error

        if attempt < CLAIM_ATTEMPTS:
            log.warning("[EV] Claim not confirmed (attempt %d/%d): %s", attempt, CLAIM_ATTEMPTS, last_error)
            time.sleep(CLAIM_RETRY_DELAY)
    raise last_error


def resume_charging(amps: int) -> None:
    """Resume at the requested rate and release the charger's manual override."""
    try:
        override = _request("GET", "/override", timeout=3).json()
    except requests.HTTPError as error:
        if error.response is None or error.response.status_code != 404:
            raise
        override = {}
    if not isinstance(override, dict):
        raise requests.RequestException("Unexpected OpenEVSE manual override response")

    try:
        # Have the new EMS rate in place before releasing the higher-priority override.
        set_charging(True, amps)
        if override:
            _request("DELETE", "/override", timeout=3)
    except requests.RequestException:
        try:
            set_charging(False)
        except requests.RequestException:
            log.exception("[EV] Unable to restore the pause after a failed resume")
        raise


def release_claim() -> None:
    """Drop our claim so the charger falls back to its own control logic."""
    _request("DELETE", CLAIM_PATH)
