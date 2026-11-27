import hashlib
import os
from datetime import datetime, timedelta
from math import isfinite
from pathlib import Path
from secrets import token_hex

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

TIMEOUT = 10  # seconds
RATE_UNITS = {
    "%": "%", "a": "°", "Ee": "W/m²", "F": "Hz", "h": "%", "I": "A",
    "m": "g", "P": "W", "Pa": "Pa", "ppm": "ppm", "var": "var",
    "Q": "g/s", "Qe": "Ah", "Qv": "m³/s", "R": "Ω", "S": "VA",
    "T": "°C", "THD": "%", "V": "V", "v": "m/s",
}


def _meter_reading(name, value, unit):
    return {
        "name": name,
        "value": value if type(value) in (int, float) and isfinite(value) else None,
        "unit": unit,
    }


class EGaugeClient:
    def __init__(self):
        self.meter = os.environ["EGAUGE_METER_NAME"]
        self.uri = f"https://{self.meter}.d.egauge.net"
        self.user = os.environ["EGAUGE_USER"]
        self.password = os.environ["EGAUGE_PASSWORD"]
        self.jwt = None
        self.last_token_time = None

    def _get_jwt(self):
        auth_req = requests.get(f"{self.uri}/api/auth/unauthorized", timeout=TIMEOUT).json()
        realm = auth_req["rlm"]
        nnc = auth_req["nnc"]
        cnnc = str(token_hex(64))

        ha1 = hashlib.md5(f"{self.user}:{realm}:{self.password}".encode()).hexdigest()
        hash_val = hashlib.md5(f"{ha1}:{nnc}:{cnnc}".encode()).hexdigest()

        payload = {"rlm": realm, "usr": self.user, "nnc": nnc, "cnnc": cnnc, "hash": hash_val}
        auth_login = requests.post(
            f"{self.uri}/api/auth/login", json=payload, timeout=TIMEOUT
        ).json()
        self.jwt = auth_login["jwt"]
        self.last_token_time = datetime.now()
        return self.jwt

    def _get_headers(self):
        if not self.jwt or (datetime.now() - self.last_token_time) > timedelta(minutes=5):
            self._get_jwt()
        return {"Authorization": f"Bearer {self.jwt}"}

    def get_live_data(self):
        url = f"{self.uri}/api/local"
        query_string = "env=all&l=all&s=all&values&energy&apparent&rate&cumul&type&normal&mean&freq"
        response = requests.get(
            url, headers=self._get_headers(), params=query_string, timeout=TIMEOUT
        )
        if response.status_code == 200:
            return response.json()
        raise Exception(f"Failed to get data: {response.status_code}")

    def get_l1(self):
        return self.get_live_data()["values"]["L1"]["rate"]["n"]

    def get_l2(self):
        return self.get_live_data()["values"]["L2"]["rate"]["n"]

    def get_s1(self):
        return self.get_live_data()["values"]["S1"]["rate"]["n"]

    def get_s2(self):
        return self.get_live_data()["values"]["S2"]["rate"]["n"]

    def get_evcharger_current(self):
        return self.get_live_data()["values"]["S5"]["rate"]["n"]

    def get_cooler_current(self):
        return self.get_live_data()["values"]["S8"]["rate"]["n"]

    def _get_register_rows(self):
        response = requests.get(
            f"{self.uri}/api/register",
            headers=self._get_headers(),
            params={"rate": ""},
            timeout=TIMEOUT,
        )
        if response.status_code == 200:
            return response.json()["registers"]
        raise Exception(f"Failed to get registers: {response.status_code}")

    def get_registers(self):
        """Named registers as {name: rate}, using the meter's configured formulas."""
        return {r["name"]: r.get("rate") for r in self._get_register_rows()}

    def get_grid_power(self):
        return self.get_registers()["Grid"]

    def get_cooler_power(self):
        return self.get_registers()["Cooler"]

    def get_evcharger_power(self):
        return self.get_registers()["EVC"]

    def get_all_values(self):
        rows = self._get_register_rows()
        regs = {row["name"]: row.get("rate") for row in rows}
        return {
            # L1/L2 are the two 120 V legs; L3 is an unused voltage input (~2 V)
            "l1_voltage": regs.get("L1 Voltage"),
            "l2_voltage": regs.get("L2 Voltage"),
            "l3_voltage": regs.get("L3 Voltage"),
            "grid_power": regs.get("Grid"),
            "grid_l1_power": regs.get("Grid L1"),
            "grid_l2_power": regs.get("Grid L2"),
            "cooler_power": regs.get("Cooler"),
            "evcharger_power": regs.get("EVC"),
            "internal_temperature_c": regs.get("Internal Temperature"),
            "internal_humidity": regs.get("Internal Humidity"),
            "register_readings": [
                _meter_reading(row["name"], row.get("rate"), RATE_UNITS.get(row.get("type"), ""))
                for row in rows
            ],
        }

    def get_channel_values(self):
        channels = self.get_live_data()["values"]

        def rate(name, key="n"):
            return (channels.get(name, {}).get("rate") or {}).get(key)

        readings = []
        for name, channel in channels.items():
            rates = channel.get("rate") or {}
            unit = RATE_UNITS.get(channel.get("type"), "")
            for key, label in (("n", "Normal"), ("m", "Mean"), ("f", "Frequency")):
                if key in rates:
                    readings.append(_meter_reading(f"{name} · {label}", rates[key], "Hz" if key == "f" else unit))

        return {
            "l1_rms_voltage": rate("L1"),
            "l2_rms_voltage": rate("L2"),
            "l3_rms_voltage": rate("L3"),
            "l1_frequency": rate("L1", "f"),
            "l2_frequency": rate("L2", "f"),
            "grid_l1_current": rate("S1"),
            "grid_l2_current": rate("S2"),
            "evcharger_current": rate("S5"),
            "cooler_current": rate("S8"),
            "channel_readings": readings,
        }
