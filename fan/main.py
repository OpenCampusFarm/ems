import asyncio
import enum
import logging
import os
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
from dotenv import load_dotenv

from coolbot import CoolBotClient

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


class FanMode(enum.Enum):
    AUTO = "auto"  # normal EMS-controlled behavior
    FORCE_ON = "on"  # always on, regardless of conditions
    FORCE_OFF = "off"  # always off, regardless of conditions


# --- Fan / Relay config ---
RELAY_PIN = 23
POLL_INTERVAL = 30  # seconds between checks
FAN_MODE = FanMode.FORCE_OFF
HYSTERESIS = 2.0  # °F deadband

# The coordinates for the UM Campus Farm
FARM_LAT = 42.3005
FARM_LON = -83.6655


# --- Outdoor temperature ---


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
            async with CoolBotClient() as cb:
                outdoor = get_outdoor_temp()
                room = cb.room_temp
                setpoint = cb.set_temp_f

                if FAN_MODE is FanMode.FORCE_ON:
                    log.info(
                        "[Fan] FORCE_ON — fan forced ON (outdoor=%s°F, room=%s°F)",
                        f"{outdoor:.1f}" if outdoor is not None else "N/A",
                        f"{room:.1f}" if room is not None else "N/A",
                    )
                    fan_on = True
                    set_fan(True)
                elif FAN_MODE is FanMode.FORCE_OFF:
                    log.info(
                        "[Fan] FORCE_OFF — fan forced OFF (outdoor=%s°F, room=%s°F)",
                        f"{outdoor:.1f}" if outdoor is not None else "N/A",
                        f"{room:.1f}" if room is not None else "N/A",
                    )
                    fan_on = False
                    set_fan(False)
                elif not cb.is_running:
                    log.warning("[CoolBot] Offline or off — fan OFF")
                    fan_on = False
                    set_fan(False)
                elif None in (outdoor, room, setpoint):
                    log.warning(
                        "[Fan] Missing data (outdoor=%s, room=%s, setpoint=%s) — fan OFF",
                        outdoor,
                        room,
                        setpoint,
                    )
                    fan_on = False
                    set_fan(False)
                elif not fan_on and (
                    outdoor + HYSTERESIS < setpoint and room > setpoint + HYSTERESIS
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
