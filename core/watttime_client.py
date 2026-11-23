"""
WattTime client: the current grid MOER (lb CO2/MWh) for the farm's region.

Same calls as real_time_ems.py's WattTime section, in its own file so the power/carbon
publisher can use it without loading the EMS's CoolBot, OpenEVSE-control and eGauge code.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
log = logging.getLogger(__name__)

WT_USERNAME = os.getenv("WT_USERNAME")
WT_PASSWORD = os.getenv("WT_PASSWORD")
WT_REGION = os.getenv("WT_REGION", "MISO_DETROIT")
WT_BASE = "https://api.watttime.org"

_wt_token: str | None = None
_wt_token_ts: float = 0.0
_WT_TOKEN_TTL = 25 * 60


def _get_wt_token() -> str | None:
    global _wt_token, _wt_token_ts
    if _wt_token and (time.time() - _wt_token_ts) < _WT_TOKEN_TTL:
        return _wt_token
    if not WT_USERNAME or not WT_PASSWORD:
        log.error("[WattTime] Set WT_USERNAME and WT_PASSWORD in core/.env")
        return None
    try:
        resp = requests.get(f"{WT_BASE}/login", auth=(WT_USERNAME, WT_PASSWORD), timeout=15)
        resp.raise_for_status()
        _wt_token = resp.json()["token"]
        _wt_token_ts = time.time()
        log.info("[WattTime] Token refreshed")
        return _wt_token
    except Exception as exc:
        log.error("[WattTime] Login failed: %s", exc)
        return None


def get_grid_moer() -> float | None:
    """Latest MOER in lb CO2/MWh, or None if WattTime can't be reached."""
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
