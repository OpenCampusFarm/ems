import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
import zlib
from pathlib import Path

import requests

try:
    import RPi.GPIO as GPIO
except ImportError:
    from unittest.mock import MagicMock

    GPIO = MagicMock()
    GPIO.BCM = "BCM"
    GPIO.OUT = "OUT"
    GPIO.HIGH = True
    GPIO.LOW = False
import websockets
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# --- Fan / Relay config ---
RELAY_PIN = 23
POLL_INTERVAL = 30  # seconds between checks
# Set to True to force fan ON regardless of conditions (for testing)
TEST_FAN_ON = False
HYSTERESIS = 2.0  # °F deadband
# --- CoolBot config ---
BLYNK_URL = "wss://cbws.storeitcold.com/websocket"
EMAIL = os.environ.get("SIT_EMAIL")
PASSWORD = os.environ.get("SIT_PASSWORD")

# --- CoolBot protocol constants ---
CMD_RESPONSE = 0x00
CMD_LOGIN = 0x02
CMD_HARDWARE_CONNECTED = 0x04
CMD_PING = 0x06
CMD_HARDWARE = 0x14
CMD_LOAD_PROFILE_GZIPPED = 0x18
CMD_ACTIVATE_DASHBOARD = 0x07
CMD_APP_SYNC = 0x19
CMD_HARDWARE_DISCONNECTED = 0x47

CMD_NAMES = {
    0x00: "RESPONSE",
    0x02: "LOGIN",
    0x04: "HARDWARE_CONNECTED",
    0x06: "PING",
    0x07: "ACTIVATE_DASHBOARD",
    0x14: "HARDWARE",
    0x18: "LOAD_PROFILE_GZIPPED",
    0x19: "APP_SYNC",
    0x47: "HARDWARE_DISCONNECTED",
}

STATUS_CODES = {
    200: "OK",
    2: "Illegal command",
    4: "Not authenticated",
    9: "Device went offline",
    11: "Server error",
}

PING_PACKET = struct.pack(">BHH", CMD_LOAD_PROFILE_GZIPPED, 2, 0)

PIN_ROOM_TEMP = 0
PIN_SET_TEMP = 4
PIN_POWER_ON = 9


# --- CoolBot helpers ---


def hash_password(password: str, email: str) -> str:
    email_hash = hashlib.sha256(email.lower().encode()).digest()
    return base64.b64encode(
        hashlib.sha256(password.encode() + email_hash).digest()
    ).decode()


def build_login_packet(email: str, msg_id: int = 1) -> bytes:
    body = "\0".join(
        [email, hash_password(PASSWORD, email), "Other", "12220000", "Blynk"]
    )
    body_bytes = body.encode()
    return struct.pack(">BHH", CMD_LOGIN, msg_id, len(body_bytes)) + body_bytes


def build_hardware_read_packet(
    dashboard_id: int, device_id: int, pin: int, msg_id: int = 3
) -> bytes:
    body = f"{dashboard_id}-{device_id}\x00vr\x00{pin}".encode()
    return struct.pack(">BHH", CMD_HARDWARE, msg_id, len(body)) + body


def build_hardware_packet(
    dashboard_id: int, device_id: int, pin: int, value, msg_id: int = 3
) -> bytes:
    body = f"{dashboard_id}-{device_id}\x00vw\x00{pin}\x00{value}".encode()
    return struct.pack(">BHH", CMD_HARDWARE, msg_id, len(body)) + body


def build_text_packet(cmd: int, body: str, msg_id: int) -> bytes:
    body_bytes = body.encode()
    return struct.pack(">BHH", cmd, msg_id, len(body_bytes)) + body_bytes


def build_response_packet(msg_id: int, status: int = 200) -> bytes:
    return struct.pack(">BHH", CMD_RESPONSE, msg_id, status)


def parse_packet(data: bytes) -> dict:
    if len(data) < 5:
        return {"error": f"Packet too short ({len(data)} bytes)"}

    cmd, msg_id, field = struct.unpack(">BHH", data[:5])
    result = {
        "command": cmd,
        "command_name": CMD_NAMES.get(cmd, f"UNKNOWN(0x{cmd:02X})"),
        "msg_id": msg_id,
    }

    if cmd == CMD_RESPONSE:
        result["status"] = field
        result["status_text"] = STATUS_CODES.get(field, f"Unknown ({field})")
        result["success"] = field == 200
    elif cmd == CMD_LOAD_PROFILE_GZIPPED:
        body_bytes = data[5 : 5 + field]
        try:
            result["profile"] = json.loads(zlib.decompress(body_bytes))
        except Exception as e:
            result["decompress_error"] = str(e)
    else:
        body_bytes = data[5 : 5 + field]
        parts = body_bytes.decode("utf-8", errors="replace").split("\0")
        result["body"] = parts
        if cmd in (CMD_HARDWARE, CMD_APP_SYNC) and len(parts) >= 3:
            result["device_ref"] = parts[0]
            result["pin_type"] = parts[1]
            result["pin"] = parts[2]
            result["value"] = parts[3:]

    return result


async def blynk_login(ws) -> bool:
    await ws.send(build_login_packet(EMAIL))
    resp = await ws.recv()
    data = resp if isinstance(resp, bytes) else resp.encode()
    return parse_packet(data).get("success", False)


class CoolBotClient:
    """Persistent WebSocket connection to the CoolBot."""

    def __init__(self):
        self.room_temp: float | None = None
        self.set_temp_f: float | None = None
        self.power_on: bool | None = None
        self.hw_online: bool | None = None
        self._dashboard_id: int | None = None
        self._device_id: int | None = None
        self._ws = None
        self._ready = asyncio.Event()
        self._listen_task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool | None:
        if self.hw_online is False:
            return False
        if self.power_on is None:
            return None
        return self.power_on

    async def __aenter__(self):
        self._ws = await websockets.connect(BLYNK_URL)
        if not await blynk_login(self._ws):
            raise RuntimeError("CoolBot login failed")

        await self._ws.send(PING_PACKET)
        while True:
            raw = await self._ws.recv()
            data = raw if isinstance(raw, bytes) else raw.encode()
            parsed = parse_packet(data)
            cmd = parsed.get("command")
            if cmd == CMD_PING:
                await self._ws.send(build_response_packet(parsed["msg_id"]))
            elif cmd == CMD_LOAD_PROFILE_GZIPPED:
                profile = parsed.get("profile", {})
                dashboards = profile.get("dashBoards", [])
                if not dashboards:
                    raise RuntimeError("No dashboards in profile")
                self._dashboard_id = dashboards[0]["id"]
                devices = dashboards[0].get("devices", [])
                self._device_id = devices[0]["id"] if devices else 0
                if devices:
                    self.hw_online = devices[0].get("status", "") == "ONLINE"
                pins_storage = dashboards[0].get("pinsStorage", {})
                raw_power = pins_storage.get(f"{self._device_id}-v{PIN_POWER_ON}")
                if raw_power is not None:
                    try:
                        self.power_on = bool(int(raw_power))
                    except ValueError, TypeError:
                        pass
                break

        await self._ws.send(
            build_text_packet(CMD_ACTIVATE_DASHBOARD, str(self._dashboard_id), msg_id=3)
        )
        await self._ws.send(
            build_text_packet(CMD_APP_SYNC, str(self._dashboard_id), msg_id=4)
        )

        self._listen_task = asyncio.create_task(self._listen())
        await asyncio.wait_for(self._ready.wait(), timeout=10)
        return self

    async def __aexit__(self, *_):
        if self._ws:
            try:
                self._ws.transport.close()
            except Exception:
                pass
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError, Exception:
                pass

    async def _listen(self):
        async for raw in self._ws:
            data = raw if isinstance(raw, bytes) else raw.encode()
            parsed = parse_packet(data)
            cmd = parsed.get("command")

            if cmd == CMD_PING:
                await self._ws.send(build_response_packet(parsed["msg_id"]))
            elif cmd == CMD_HARDWARE_CONNECTED:
                self.hw_online = True
            elif cmd == CMD_HARDWARE_DISCONNECTED:
                self.hw_online = False
            elif cmd in (CMD_APP_SYNC, CMD_HARDWARE):
                if parsed.get("pin_type") == "vw" and parsed.get("device_ref") == str(
                    self._dashboard_id
                ):
                    pin = parsed.get("pin")
                    values = parsed.get("value", [])
                    if values:
                        if pin == str(PIN_ROOM_TEMP):
                            self.room_temp = float(values[0])
                        elif pin == str(PIN_SET_TEMP):
                            self.set_temp_f = float(values[0])
                        elif pin == str(PIN_POWER_ON):
                            self.power_on = bool(int(values[0]))

            if (
                not self._ready.is_set()
                and self.room_temp is not None
                and self.set_temp_f is not None
            ):
                self._ready.set()


# --- Outdoor temperature ---

FARM_LAT = 42.2942
FARM_LON = -83.7104


def read_outdoor_temp() -> float | None:
    try:
        base_dir = "/sys/bus/w1/devices/"
        sensors = [f for f in os.listdir(base_dir) if f.startswith("28-")]
        if not sensors:
            log.warning("[Sensor] No DS18B20 sensors found, falling back to Open-Meteo")
            return None
        device_file = f"{base_dir}{sensors[0]}/w1_slave"
        with open(device_file) as f:
            lines = f.readlines()
        if "YES" in lines[0]:
            eq = lines[1].find("t=")
            if eq != -1:
                temp_c = float(lines[1][eq + 2 :]) / 1000.0
                return round((temp_c * 9 / 5) + 32, 2)
        return None
    except Exception as e:
        log.error("[Sensor] Error reading outdoor temp: %s", e)
        return None


def get_outdoor_temp_api() -> float | None:
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": FARM_LAT,
                "longitude": FARM_LON,
                "current": "temperature_2m",
                "temperature_unit": "fahrenheit",
                "forecast_days": 1,
            },
            timeout=10,
        )
        resp.raise_for_status()
        temp = float(resp.json()["current"]["temperature_2m"])
        log.info("[Sensor] Outdoor temp (Open-Meteo fallback) = %.1f°F", temp)
        return temp
    except Exception as e:
        log.error("[Sensor] Open-Meteo fallback failed: %s", e)
        return None


def get_outdoor_temp() -> float | None:
    return read_outdoor_temp() or get_outdoor_temp_api()


# --- Fan relay ---


def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(RELAY_PIN, GPIO.OUT)
    GPIO.output(RELAY_PIN, GPIO.LOW)  # LOW = relay off at startup


def set_fan(on: bool):
    GPIO.output(RELAY_PIN, GPIO.HIGH if on else GPIO.LOW)
    log.info("[Fan] %s", "ON" if on else "OFF")


# --- Main control loop ---


async def control_loop():
    fan_on = False
    while True:
        try:
            if TEST_FAN_ON:
                log.info("[Fan] TEST MODE — fan forced ON")
                set_fan(True)
            else:
                async with CoolBotClient() as cb:
                    if not cb.is_running:
                        log.warning("[CoolBot] Offline or off — fan OFF")
                        fan_on = False
                        set_fan(False)
                    else:
                        outdoor = get_outdoor_temp()
                        room = cb.room_temp
                        setpoint = cb.set_temp_f

                        if None in (outdoor, room, setpoint):
                            log.warning(
                                "[Fan] Missing data (outdoor=%s, room=%s, setpoint=%s) — fan OFF",
                                outdoor,
                                room,
                                setpoint,
                            )
                            fan_on = False
                            set_fan(False)
                        elif not fan_on and (
                            outdoor + HYSTERESIS < setpoint
                            and room > setpoint + HYSTERESIS
                        ):
                            fan_on = True
                            log.info(
                                "[Fan] outdoor=%.1f°F < setpoint=%.1f°F < room=%.1f°F — fan ON",
                                outdoor,
                                setpoint,
                                room,
                            )
                            set_fan(True)
                        elif fan_on and (outdoor >= setpoint or room <= setpoint):
                            fan_on = False
                            log.info(
                                "[Fan] outdoor=%.1f°F, setpoint=%.1f°F, room=%.1f°F — fan OFF",
                                outdoor,
                                setpoint,
                                room,
                            )
                            set_fan(False)
                        else:
                            log.info(
                                "[Fan] outdoor=%.1f°F, setpoint=%.1f°F, room=%.1f°F — fan %s (no change)",
                                outdoor,
                                setpoint,
                                room,
                                "ON" if fan_on else "OFF",
                            )

        except Exception as e:
            log.error("[Fan] Control loop error: %s — fan OFF", e, exc_info=True)
            fan_on = False
            set_fan(False)

        await asyncio.sleep(POLL_INTERVAL)


def main():
    log.info("[Fan] Starting up...")
    setup_gpio()
    try:
        asyncio.run(control_loop())
    except KeyboardInterrupt:
        log.info("[Fan] Shutting down...")
    finally:
        set_fan(False)
        GPIO.cleanup()


if __name__ == "__main__":
    main()
