"""Poll the CoolBot WebSocket independently of EMS automation."""

import asyncio
import logging
import time
from threading import Thread
from typing import NamedTuple

log = logging.getLogger(__name__)
POLL_SECONDS = 30
STALE_SECONDS = 60


class CoolBotReading(NamedTuple):
    room_f: float
    setpoint_f: float
    ts: float


class CoolBotMonitor:
    def __init__(self):
        self._reading = None

    def read(self):
        return self._reading

    def start(self):
        from core.Loads.coolbot import EMAIL, PASSWORD, CoolBotClient

        if not EMAIL or not PASSWORD:
            log.warning("[CoolBot] Configure SIT_EMAIL and SIT_PASSWORD in core/.env")
            return
        Thread(target=lambda: asyncio.run(self._poll(CoolBotClient)),
               name="coolbot-monitor", daemon=True).start()

    async def _poll(self, client_factory):
        while True:
            client = client_factory()
            try:
                await asyncio.wait_for(client.__aenter__(), timeout=15)
                self._reading = CoolBotReading(client.room_temp, client.set_temp_f, time.time())
            except Exception as error:
                log.warning("[CoolBot] Telemetry read failed: %s", error)
            finally:
                await client.__aexit__()
            await asyncio.sleep(POLL_SECONDS)
