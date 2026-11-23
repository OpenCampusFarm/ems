"""Read the latest temperature from a Lascar EasyLog WiFi logger via EasyLog Cloud.

EasyLog Cloud has no public API; these are the endpoints the web portal
(portal.easylogcloud.com) uses:

    POST /auth/login          {"emailOrUsername", "password"} -> {"accessToken", ...}
    GET  /api/v1/devices      -> items[].probes[].channels[] with "reading"

The access token is a short-lived JWT (~15 min), so it is cached and renewed
by logging in again shortly before it expires, or after a 401.
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

API_BASE = "https://apicf-portal.easylogcloud.com"
EMAIL = os.environ.get("EASYLOG_EMAIL")
PASSWORD = os.environ.get("EASYLOG_PASSWORD")
# Which logger to read: matches the device name or MAC address shown in EasyLog.
# Empty = use the first device on the account.
DEVICE = os.environ.get("EASYLOG_DEVICE", "").strip()

STALE_AFTER = 15 * 60  # seconds; older readings are treated as missing
TOKEN_MARGIN = 60  # renew the token this many seconds before it expires
TIMEOUT = 10

_session = requests.Session()
_token: str | None = None
_token_exp: float = 0.0


def _jwt_exp(token: str) -> float:
    """Return the JWT's exp claim (unix seconds), or 10 minutes from now if unreadable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return time.time() + 600


def _login() -> None:
    global _token, _token_exp
    if not EMAIL or not PASSWORD:
        raise RuntimeError("EASYLOG_EMAIL / EASYLOG_PASSWORD not set in .env")
    resp = _session.post(
        f"{API_BASE}/auth/login",
        json={"emailOrUsername": EMAIL, "password": PASSWORD},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("requiresMfa") or data.get("requiresMfaSetup"):
        raise RuntimeError("EasyLog account requires MFA; automatic login not possible")
    _token = data["accessToken"]
    _token_exp = _jwt_exp(_token)


def _get_devices() -> list[dict]:
    if _token is None or time.time() > _token_exp - TOKEN_MARGIN:
        _login()
    for attempt in range(2):
        resp = _session.get(
            f"{API_BASE}/api/v1/devices",
            params={"Page": 1, "PageSize": 1000},
            headers={"Authorization": f"Bearer {_token}"},
            timeout=TIMEOUT,
        )
        if resp.status_code == 401 and attempt == 0:
            _login()  # token revoked or expired early
            continue
        resp.raise_for_status()
        return resp.json().get("items", [])
    return []


def _pick_device(devices: list[dict]) -> dict | None:
    if not devices:
        return None
    if not DEVICE:
        return devices[0]
    want = DEVICE.lower()
    for d in devices:
        if want in ((d.get("name") or "").lower(), (d.get("macAddress") or "").lower()):
            return d
    return None


def _to_fahrenheit(value: float, unit: str) -> float:
    return value * 9 / 5 + 32 if "C" in unit.upper() else value


def get_easylog_temp() -> float | None:
    """Latest temperature in °F, or None if unavailable or stale."""
    try:
        device = _pick_device(_get_devices())
        if device is None:
            log.warning("[EasyLog] Device %r not found on account", DEVICE or "(first)")
            return None
        if device.get("hasLostCommunication"):
            log.warning("[EasyLog] %s has lost communication", device.get("name"))
            return None

        last = device.get("lastReadingTime")
        if last:
            # Timestamps come back without a zone and are UTC.
            ts = datetime.fromisoformat(last).replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > STALE_AFTER:
                log.warning("[EasyLog] Reading is %.0f min old — ignoring", age / 60)
                return None

        for probe in device.get("probes") or []:
            for ch in probe.get("channels") or []:
                if ch.get("channelType") == "Temperature" and ch.get("reading") not in (None, ""):
                    temp = round(_to_fahrenheit(float(ch["reading"]), ch.get("unitSymbol") or "°F"), 2)
                    log.info("[EasyLog] Outdoor temp = %.1f°F (%s)", temp, device.get("name"))
                    return temp

        log.warning("[EasyLog] No temperature channel on %s", device.get("name"))
        return None
    except Exception as e:
        log.error("[EasyLog] Error reading temperature: %s", e)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
    print(get_easylog_temp())
