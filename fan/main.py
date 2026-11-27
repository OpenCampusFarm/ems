import asyncio
import enum
import logging
import signal
import sys
from pathlib import Path

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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fan_runtime import record_fan_status
from outdoor_temperature import get_outdoor_temp_api, read_outdoor_temp
from system_state import read_state

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
HYSTERESIS = 2.0  # °F deadband


def get_outdoor_temp() -> float | None:
    temp_f = read_outdoor_temp()
    return temp_f if temp_f is not None else get_outdoor_temp_api()


# --- Fan relay ---


def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(RELAY_PIN, GPIO.OUT, initial=GPIO.LOW)


def set_fan(mode: FanMode, on: bool):
    GPIO.output(RELAY_PIN, GPIO.HIGH if on else GPIO.LOW)
    log.info("[Fan] %s", "ON" if on else "OFF")
    try:
        record_fan_status(mode.value, on)
    except OSError as error:
        log.warning("[Fan] Could not report relay status: %s", error)


# --- Main control loop ---


async def control_loop(mode: FanMode):
    if mode is not FanMode.AUTO:
        set_fan(mode, mode is FanMode.FORCE_ON)
        await asyncio.Event().wait()
        return

    fan_on = False
    while True:
        try:
            async with CoolBotClient() as cb:
                outdoor = get_outdoor_temp()
                room = cb.room_temp
                setpoint = cb.set_temp_f

                if not cb.is_running:
                    log.warning("[CoolBot] Offline or off — fan OFF")
                    fan_on = False
                    set_fan(mode, False)
                elif None in (outdoor, room, setpoint):
                    log.warning(
                        "[Fan] Missing data (outdoor=%s, room=%s, setpoint=%s) — fan OFF",
                        outdoor,
                        room,
                        setpoint,
                    )
                    fan_on = False
                    set_fan(mode, False)
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
                    set_fan(mode, True)
                elif fan_on and (outdoor >= setpoint or room <= setpoint):
                    fan_on = False
                    log.info(
                        "[Fan] outdoor=%.1f°F, setpoint=%.1f°F, room=%.1f°F — fan OFF",
                        outdoor,
                        setpoint,
                        room,
                    )
                    set_fan(mode, False)
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
            set_fan(mode, False)

        await asyncio.sleep(POLL_INTERVAL)


def _install_shutdown_handlers(loop: asyncio.AbstractEventLoop, task: asyncio.Task):
    # signal.signal (not loop.add_signal_handler) so this works on Windows too.
    # SIGBREAK only exists on Windows (Ctrl-Break / console close).
    def handler(_signum, _frame):
        loop.call_soon_threadsafe(task.cancel)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, handler)


async def run(mode: FanMode):
    # Any shutdown signal cancels the control loop so the CoolBot connection
    # closes cleanly and main() can switch the relay off.
    task = asyncio.create_task(control_loop(mode))
    _install_shutdown_handlers(asyncio.get_running_loop(), task)
    try:
        await task
    except asyncio.CancelledError:
        log.info("[Fan] Shutdown signal received")


def main():
    log.info("[Fan] Starting up...")
    mode = FanMode(read_state()["fan"])
    log.info("[Fan] Mode: %s", mode.value)
    setup_gpio()
    try:
        if mode is FanMode.AUTO:
            set_fan(mode, False)
        asyncio.run(run(mode))
    finally:
        log.info("[Fan] Shutting down — fan OFF, releasing GPIO")
        set_fan(mode, False)
        GPIO.cleanup()


if __name__ == "__main__":
    main()
