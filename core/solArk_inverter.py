import os
import time
from datetime import datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

cached_token: str | None = None
token_expiry: float = 0

API_USERNAME = os.getenv("SOLARK_USERNAME")
API_PASSWORD = os.getenv("SOLARK_PASSWORD")
PLANT_ID = os.getenv("SOLARK_PLANT_ID", "139155")
BASE_URL = "https://ecsprod-api-new.solarkcloud.com"
WORKDATA_URL = "https://api.solarkcloud.com/api/v1/workdata/dynamic"
DEVICE_SN = os.getenv("SOLARK_DEVICE_SN") or ("2308170261" if PLANT_ID == "139155" else None)
FARM_TIME = ZoneInfo("America/Detroit")

FLOW_FIELDS = {
    "pv": "pvPower",
    "battery": "battPower",
    "grid": "gridOrMeterPower",
    "load": "loadOrEpsPower",
    "soc": "soc",
}
FLOW_STATUS = {
    "pvTo": "Solar path active",
    "toLoad": "Supplying load",
    "gridTo": "Importing from grid",
    "toGrid": "Exporting to grid",
    "toBat": "Charging battery",
    "batTo": "Discharging battery",
    "genTo": "Generator path active",
    "minTo": "Microinverter path active",
    "existsGen": "Generator configured",
    "existsMin": "Microinverter configured",
    "existsMeter": "External meter configured",
    "genOn": "Generator on",
    "microOn": "Microinverter on",
    "bmsCommFaultFlag": "Battery communication fault",
    "existThinkPower": "ThinkPower detected",
}


def _finite_number(value):
    try:
        number = float(value) if not isinstance(value, bool) else float("nan")
    except (ValueError, TypeError):
        return None
    return number if isfinite(number) else None


def get_access_token() -> str | None:
    global cached_token, token_expiry

    if cached_token and time.time() < token_expiry:
        return cached_token

    url = f"{BASE_URL}/oauth/token"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "origin": BASE_URL,
        "referer": BASE_URL,
    }
    data = {
        "client_id": "csp-web",
        "grant_type": "password",
        "password": API_PASSWORD,
        "username": API_USERNAME,
    }

    try:
        response = requests.post(url, json=data, headers=headers, timeout=30)
        response.raise_for_status()
        result = response.json()["data"]
        cached_token = result["access_token"]
        token_expiry = time.time() + (23 * 3600)
        print("[AUTH] New access token obtained, expires in 23 hours")
        return cached_token
    except Exception as e:
        print(f"[ERROR] Failed to get access token: {e}")
        return None


def parse_flow_data(flow_response: dict[str, Any], *, preserve_missing=False) -> dict:
    data = flow_response.get("data") or {}
    missing = None if preserve_missing else 0.0
    flow = {}
    for name, field in FLOW_FIELDS.items():
        number = _finite_number(data.get(field))
        flow[name] = number if number is not None else missing

    flow["grid_buying"] = data.get("gridTo") if preserve_missing else bool(data.get("gridTo"))
    if preserve_missing:
        flow.update(
            grid_selling=data.get("toGrid"),
            battery_charging=data.get("toBat"),
            battery_discharging=data.get("batTo"),
            generator=_finite_number(data.get("genPower")),
            microinverter=_finite_number(data.get("minPower")),
            status={label: data[key] for key, label in FLOW_STATUS.items() if isinstance(data.get(key), bool)},
        )
    return flow


def fetch_plant_data(*, preserve_missing=False) -> dict | None:
    global cached_token, token_expiry

    token = get_access_token()
    if not token:
        return None

    current_date = datetime.now().strftime("%Y-%m-%d")
    url = f"{BASE_URL}/api/v1/plant/energy/{PLANT_ID}/flow"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    params = {"date": current_date}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)

        if response.status_code == 401:
            print("[AUTH] Token expired, refreshing...")
            cached_token = None
            token_expiry = 0
            token = get_access_token()
            if not token:
                return None
            headers["Authorization"] = f"Bearer {token}"
            response = requests.get(url, headers=headers, params=params, timeout=30)

        response.raise_for_status()
        raw_data = response.json()

        if raw_data.get("code") != 0:
            print(f"[ERROR] API error code: {raw_data.get('code')}, msg: {raw_data.get('msg')}")
            return None

        parsed_data = parse_flow_data(raw_data, preserve_missing=preserve_missing)
        print(
            f"[DATA] PV={parsed_data['pv']}W  Battery={parsed_data['battery']}W  "
            f"Grid={parsed_data['grid']}W  Load={parsed_data['load']}W  SOC={parsed_data['soc']}%"
        )
        return parsed_data
    except Exception as e:
        print(f"[ERROR] Failed to fetch flow data: {e}")
        return None


def parse_workdata_record(record):
    stamp = datetime.fromisoformat(record["Time"])
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=FARM_TIME)
    readings = {}
    for key, value in record.items():
        # The suffix is the register ID. Leave unitless status codes as text.
        if "/" not in key or not isinstance(value, (str, int, float, bool, type(None))):
            continue
        number = _finite_number(value) if ")/" in key else None
        readings[key] = number if number is not None else str(value) if value is not None else None
    return stamp.timestamp(), readings


def fetch_workdata():
    # The plant-flow request already authenticated this cycle.
    token = cached_token
    if not DEVICE_SN or not token or time.time() >= token_expiry:
        return None
    today = datetime.now(FARM_TIME).date()
    try:
        response = requests.get(WORKDATA_URL, headers={
            "Authorization": f"Bearer {token}",
            "Origin": "https://www.solarkcloud.com",
            "Referer": "https://www.solarkcloud.com/",
        }, params={
            "page": 1, "limit": 1, "sn": DEVICE_SN,
            "dateRange": f"{today - timedelta(days=1)},{today}",
            "type": 1, "lan": "en", "sgip": "false",
        }, timeout=8)
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            return None
        records = (body.get("data") or {}).get("record") or []
        return parse_workdata_record(records[0]) if records else None
    except (requests.RequestException, ValueError, TypeError, KeyError):
        print("[Sol-Ark] Inverter diagnostics unavailable this cycle")
        return None


def get_inverter_data() -> dict[str, str]:
    data = fetch_plant_data() or {}
    return {
        "Solar W":    str(data.get("pv", 0)),
        "Battery W":  str(data.get("battery", 0)),
        "Grid W":     str(data.get("grid", 0)),
        "Consumed W": str(data.get("load", 0)),
        "soc":        str(data.get("soc", 0)),
    }
