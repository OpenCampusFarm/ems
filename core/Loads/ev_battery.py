import json
import os
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

HA_URI = os.environ.get("HA_URI", "https://ha.shian.fun")
HA_TOKEN = os.environ.get("HA_TOKEN")
HA_VIN = os.environ.get("HA_VIN", "1ft6w3l72rwg09684")

KM_TO_MILES = 0.621371


def ha_get(entity_id: str) -> str:
    url = f"{HA_URI}/api/states/{entity_id}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {HA_TOKEN}",
            "User-Agent": "curl/8.0",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["state"]


def check_battery() -> dict | None:
    try:
        percentage = float(ha_get(f"sensor.fordpass_{HA_VIN}_soc"))
        range_km = float(ha_get(f"sensor.fordpass_{HA_VIN}_elveh"))
        miles_left = range_km * KM_TO_MILES
        print(f"Charge Level: {percentage}%  Range: {miles_left:.1f} miles")
        return {"percentage": int(percentage), "miles_left": int(miles_left)}
    except Exception as e:
        print(f"Error fetching EV battery data: {e}")
        return None


def set_charging(enabled: bool, uri: str, token: str, vin: str) -> dict:
    action = "turn_on" if enabled else "turn_off"
    url = f"{uri}/api/services/switch/{action}"
    payload = json.dumps({"entity_id": f"switch.fordpass_{vin}_elvehcharge"}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "curl/8.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())
