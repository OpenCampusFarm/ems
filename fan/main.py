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
START_BELOW_SETPOINT_F = 1.0
ROOM_HYSTERESIS_F = 0.5
START_OUTDOOR_DIFFERENCE_F = 3.0
STOP_OUTDOOR_DIFFERENCE_F = 1.0


def get_outdoor_temp() -> float | None:
    temp_f = read_outdoor_temp()
    return temp_f if temp_f is not None else get_outdoor_temp_api()


# --- Fan relay ---


def setup_gpio():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(RELAY_PIN, GPIO.OUT, initial=GPIO.LOW)


def set_fan(on: bool):
    GPIO.output(RELAY_PIN, GPIO.HIGH if on else GPIO.LOW)
    log.info("[Fan] %s", "ON" if on else "OFF")


def publish_fan_status(mode: FanMode, on: bool, *, room_f=None, outdoor_f=None):
    try:
        record_fan_status(mode.value, on, room_f=room_f, outdoor_f=outdoor_f)
    except OSError as error:
        log.warning("[Fan] Could not report relay status: %s", error)


# --- Main control loop ---


async def control_loop(mode: FanMode):
    if mode is not FanMode.AUTO:
        fan_on = mode is FanMode.FORCE_ON
        set_fan(fan_on)
        publish_fan_status(mode, fan_on)
        await asyncio.Event().wait()
        return

    fan_on = False
    while True:
        room = outdoor = None
        try:
            async with CoolBotClient() as cb:
                outdoor = get_outdoor_temp()
                room = cb.room_temp
                setpoint = cb.set_temp_f

                if not cb.is_running:
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
                else:
                    state = read_state()
                    start_f = setpoint - START_BELOW_SETPOINT_F
                    if state["ems"] == "on" and setpoint == state["coolbot_other_f"]:
                        start_f = min(start_f, state["coolbot_clean_f"])
                    stop_f = start_f - ROOM_HYSTERESIS_F
                    cooler_by = room - outdoor
                    if fan_on:
                        next_on = not (room <= stop_f or cooler_by <= STOP_OUTDOOR_DIFFERENCE_F)
                    else:
                        next_on = room >= start_f and cooler_by >= START_OUTDOOR_DIFFERENCE_F
                    if next_on != fan_on:
                        fan_on = next_on
                        set_fan(fan_on)
                    log.info(
                        "[Fan] outdoor=%.1f°F, setpoint=%.1f°F, room=%.1f°F, start=%.1f°F, stop=%.1f°F — fan %s",
                        outdoor, setpoint, room, start_f, stop_f, "ON" if fan_on else "OFF",
                    )

        except Exception as e:
            log.error("[Fan] Control loop error: %s — fan OFF", e, exc_info=True)
            fan_on = False
            set_fan(False)

        publish_fan_status(mode, fan_on, room_f=room, outdoor_f=outdoor)
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
            set_fan(False)
            publish_fan_status(mode, False)
        asyncio.run(run(mode))
    finally:
        log.info("[Fan] Shutting down — fan OFF, releasing GPIO")
        set_fan(False)
        publish_fan_status(mode, False)
        GPIO.cleanup()


if __name__ == "__main__":
    main()
