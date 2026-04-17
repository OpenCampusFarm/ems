import asyncio
import base64
import hashlib
import json
import os
import struct
import threading
import zlib
from pathlib import Path

import websockets
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# --- Config ---
BLYNK_URL = "wss://cbws.storeitcold.com/websocket"
EMAIL    = os.environ.get("SIT_EMAIL")
PASSWORD = os.environ.get("SIT_PASSWORD")

# --- Command Codes ---
CMD_RESPONSE              = 0x00
CMD_LOGIN                 = 0x02
CMD_HARDWARE_CONNECTED    = 0x04
CMD_PING                  = 0x06
CMD_ACTIVATE_DASHBOARD    = 0x07
CMD_HARDWARE              = 0x14
CMD_LOAD_PROFILE_GZIPPED  = 0x18
CMD_APP_SYNC              = 0x19
CMD_HARDWARE_DISCONNECTED = 0x47

PIN_ROOM_TEMP     = 0
PIN_FINS_TEMP     = 1
PIN_SET_TEMP      = 4
PIN_FINS_SET_TEMP = 6
PIN_TOO_HOT       = 12
PIN_TOO_COLD      = 16
PIN_POWER_ON      = 9


def hash_password(password: str, email: str) -> str:
    email_hash = hashlib.sha256(email.lower().encode("utf-8")).digest()
    return base64.b64encode(
        hashlib.sha256(password.encode("utf-8") + email_hash).digest()
    ).decode()


def build_login_packet(email: str, msg_id: int = 1) -> bytes:
    body = "\0".join(
        [email, hash_password(PASSWORD, email), "Other", "12220000", "Blynk"]
    ).encode()
    return struct.pack(">BHH", CMD_LOGIN, msg_id, len(body)) + body


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
        return {}
    cmd, msg_id, field = struct.unpack(">BHH", data[:5])
    result = {"command": cmd, "msg_id": msg_id}
    if cmd == CMD_RESPONSE:
        result["success"] = field == 200
    elif cmd == CMD_LOAD_PROFILE_GZIPPED:
        try:
            result["profile"] = json.loads(zlib.decompress(data[5:5 + field]))
        except Exception as e:
            result["decompress_error"] = str(e)
    else:
        parts = data[5:5 + field].decode("utf-8", errors="replace").split("\0")
        result["parts"] = parts
        if cmd in (CMD_HARDWARE, CMD_APP_SYNC) and len(parts) >= 3:
            result["device_ref"] = parts[0]
            result["pin_type"]   = parts[1]
            result["pin"]        = parts[2]
            result["value"]      = parts[3:]
    return result


async def blynk_login(ws) -> bool:
    await ws.send(build_login_packet(EMAIL))
    resp = await ws.recv()
    data = resp if isinstance(resp, bytes) else resp.encode()
    return parse_packet(data).get("success", False)


class CoolBotClient:
    """Persistent WebSocket connection to the CoolBot."""

    def __init__(self):
        self.room_temp:  float | None = None
        self.set_temp_f: float | None = None
        self.power_on:   bool  | None = None
        self.hw_online:  bool  | None = None
        self._dashboard_id: int | None = None
        self._device_id:    int | None = None
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

        await self._ws.send(struct.pack(">BHH", CMD_LOAD_PROFILE_GZIPPED, 2, 0))
        while True:
            raw  = await self._ws.recv()
            data = raw if isinstance(raw, bytes) else raw.encode()
            p    = parse_packet(data)
            if p.get("command") == CMD_PING:
                await self._ws.send(build_response_packet(p["msg_id"]))
            elif p.get("command") == CMD_LOAD_PROFILE_GZIPPED:
                profile    = p.get("profile", {})
                dashboards = profile.get("dashBoards", [])
                if not dashboards:
                    raise RuntimeError("No dashboards found in CoolBot profile")
                self._dashboard_id = dashboards[0]["id"]
                devices            = dashboards[0].get("devices", [])
                self._device_id    = devices[0]["id"] if devices else 0
                if devices:
                    self.hw_online = devices[0].get("status", "") == "ONLINE"
                pins_storage = dashboards[0].get("pinsStorage", {})
                raw_power = pins_storage.get(f"{self._device_id}-v{PIN_POWER_ON}")
                if raw_power is not None:
                    try:
                        self.power_on = bool(int(raw_power))
                    except (ValueError, TypeError):
                        pass
                break

        await self._ws.send(build_text_packet(CMD_ACTIVATE_DASHBOARD, str(self._dashboard_id), msg_id=3))
        await self._ws.send(build_text_packet(CMD_APP_SYNC,           str(self._dashboard_id), msg_id=4))

        self._listen_task = asyncio.create_task(self._listen())
        await self._ready.wait()
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
            except (asyncio.CancelledError, Exception):
                pass

    async def _listen(self):
        async for raw in self._ws:
            data = raw if isinstance(raw, bytes) else raw.encode()
            p    = parse_packet(data)
            cmd  = p.get("command")

            if cmd == CMD_PING:
                await self._ws.send(build_response_packet(p["msg_id"]))
            elif cmd == CMD_HARDWARE_CONNECTED:
                self.hw_online = True
            elif cmd == CMD_HARDWARE_DISCONNECTED:
                self.hw_online = False
            elif cmd in (CMD_APP_SYNC, CMD_HARDWARE):
                if (p.get("pin_type") == "vw"
                        and p.get("device_ref") == str(self._dashboard_id)):
                    pin    = p.get("pin")
                    values = p.get("value", [])
                    if values:
                        if pin == str(PIN_ROOM_TEMP):
                            self.room_temp = float(values[0])
                        elif pin == str(PIN_SET_TEMP):
                            self.set_temp_f = float(values[0])
                        elif pin == str(PIN_POWER_ON):
                            self.power_on = bool(int(values[0]))

                if (not self._ready.is_set()
                        and self.room_temp  is not None
                        and self.set_temp_f is not None):
                    self._ready.set()

    async def set_temp(self, temperature: int) -> None:
        await self._ws.send(
            build_hardware_packet(self._dashboard_id, self._device_id, PIN_SET_TEMP, temperature)
        )


# ── Persistent background client ──────────────────────────────────────────────

_client: CoolBotClient | None = None
_loop:   asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _ensure_client() -> CoolBotClient:
    global _client, _loop, _thread

    if _client is not None:
        return _client

    _loop = asyncio.new_event_loop()
    _thread = threading.Thread(target=_run_loop, args=(_loop,), daemon=True)
    _thread.start()

    async def _connect():
        global _client
        _client = CoolBotClient()
        await _client.__aenter__()

    asyncio.run_coroutine_threadsafe(_connect(), _loop).result(timeout=15)
    return _client


def change_setpoint(temperature: int) -> None:
    try:
        client = _ensure_client()
        asyncio.run_coroutine_threadsafe(client.set_temp(temperature), _loop).result(timeout=10)
        print(f"[CoolBot] Setpoint → {temperature}°F")
    except Exception as e:
        print(f"[CoolBot] change_setpoint failed: {e}")


def get_room_temp() -> float | None:
    try:
        return _ensure_client().room_temp
    except Exception as e:
        print(f"[CoolBot] get_room_temp failed: {e}")
        return None


def get_coolbot_temp() -> float | None:
    try:
        return _ensure_client().set_temp_f
    except Exception as e:
        print(f"[CoolBot] get_coolbot_temp failed: {e}")
        return None


def is_running() -> bool | None:
    try:
        return _ensure_client().is_running
    except Exception as e:
        print(f"[CoolBot] is_running failed: {e}")
        return None
