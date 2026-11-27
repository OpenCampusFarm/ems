"""
OpenEVSE charger control over the local HTTP API (mDNS: http://openevse.local).

Charging is controlled with the *EVSE Claim* API (soft control), NOT the manual
override. A claim made through the HTTP API has priority 500, which is lower
than the manual override (priority 1000) used by the charger's LCD / web UI, so
a person at the charger can always override the EMS.

  POST   /claims/<client_id>   {"state": "active" | "disabled", "auto_release": true}
                              Active claims can also specify "charge_current" in amps.
  DELETE /claims/<client_id>   release the claim
"""

from __future__ import annotations

import logging
from math import isfinite
import os
from pathlib import Path

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

TIMEOUT = 10  # seconds

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


def get_requested_current() -> int | None:
    """Read the EMS claim or the charger's pilot current when no rate is claimed."""
    try:
        try:
            claim = _request("GET", f"/claims/{OPENEVSE_CLAIM_ID}", timeout=3).json()
        except requests.HTTPError as error:
            if error.response is None or error.response.status_code != 404:
                raise
            claim = None

        if isinstance(claim, dict):
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
    """Allow charging at the requested current, or block it via an EVSE claim."""
    body = claim_body(enabled, amps)
    return _request("POST", f"/claims/{OPENEVSE_CLAIM_ID}", json=body).json()


def release_claim() -> None:
    """Drop our claim so the charger falls back to its own control logic."""
    _request("DELETE", f"/claims/{OPENEVSE_CLAIM_ID}")
