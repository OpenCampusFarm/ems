"""
OpenEVSE charger control over the local HTTP API (mDNS: http://openevse.local).

Charging is controlled with the *EVSE Claim* API (soft control), NOT the manual
override. A claim made through the HTTP API has priority 500, which is lower
than the manual override (priority 1000) used by the charger's LCD / web UI, so
a person at the charger can always override the EMS.

  POST   /claims/<client_id>   {"state": "active" | "disabled", "auto_release": true}
  DELETE /claims/<client_id>   release the claim
"""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

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


def _request(method: str, path: str, **kwargs) -> requests.Response:
    auth = (OPENEVSE_USER, OPENEVSE_PASSWORD) if OPENEVSE_USER else None
    resp = requests.request(
        method, f"{OPENEVSE_URI}{path}", auth=auth, timeout=TIMEOUT, **kwargs
    )
    resp.raise_for_status()
    return resp


def get_status() -> dict | None:
    """Return the charger status, or None if it can't be read."""
    try:
        raw = _request("GET", "/status").json()
        state = raw.get("state")
        status = {
            "state": state,
            "connected": bool(raw.get("vehicle"))
            or state in (STATE_CONNECTED, STATE_CHARGING),
            "charging": state == STATE_CHARGING,
            "power_w": float(raw.get("power", 0) or 0),
        }
        print(
            f"OpenEVSE state={state} connected={status['connected']} "
            f"power={status['power_w']:.0f}W"
        )
        return status
    except Exception as e:
        print(f"Error fetching OpenEVSE status: {e}")
        return None


def set_charging(enabled: bool) -> dict:
    """Allow (active) or block (disabled) charging via an EVSE claim."""
    body = {"state": "active" if enabled else "disabled", "auto_release": True}
    return _request("POST", f"/claims/{OPENEVSE_CLAIM_ID}", json=body).json()


def release_claim() -> None:
    """Drop our claim so the charger falls back to its own control logic."""
    _request("DELETE", f"/claims/{OPENEVSE_CLAIM_ID}")
