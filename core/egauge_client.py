import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path
from secrets import token_hex

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

TIMEOUT = 10  # seconds


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

    def get_registers(self):
        """Named registers as {name: rate}, e.g. "Grid", "Cooler", "EVC", "L1 Voltage".

        The register names/formulas are defined on the meter itself, so this
        follows whatever is configured there instead of re-deriving power from
        raw channels.
        """
        response = requests.get(
            f"{self.uri}/api/register",
            headers=self._get_headers(),
            params={"rate": ""},
            timeout=TIMEOUT,
        )
        if response.status_code == 200:
            return {r["name"]: r["rate"] for r in response.json()["registers"]}
        raise Exception(f"Failed to get registers: {response.status_code}")

    def get_grid_power(self):
        return self.get_registers()["Grid"]

    def get_cooler_power(self):
        return self.get_registers()["Cooler"]

    def get_evcharger_power(self):
        return self.get_registers()["EVC"]

    def get_all_values(self):
        regs = self.get_registers()
        return {
            # L1/L2 are the two 120 V legs; L3 is an unused voltage input (~2 V)
            "l1_voltage":        regs["L1 Voltage"],
            "l2_voltage":        regs["L2 Voltage"],
            "l3_voltage":        regs["L3 Voltage"],
            "grid_power":        regs["Grid"],
            "grid_l1_power":     regs["Grid L1"],
            "grid_l2_power":     regs["Grid L2"],
            "cooler_power":      regs["Cooler"],
            "evcharger_power":   regs["EVC"],
        }
