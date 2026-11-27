"""
Campus Farm dashboard publisher: sends live readings to the Drupal website.

Every POWER_PUBLISH_INTERVAL seconds it reads PV / grid / load power from the
SolArk inverter API and POSTs it to the site's cf_data module
(`/api/cf/power`), for the cf_power_flow diagram.

By default it stores separate fan and EV charging estimates for the Portal.
When Drupal is configured, it also runs the whole-farm model and POSTs its
three carbon lines to `/api/cf/carbon` every CARBON_PUBLISH_MINUTES.

The carbon shadow farm (simulated truck and battery) keeps its state in
carbon_live_state.json, so it carries on across restarts. When that file
doesn't exist it starts fresh: the truck at CARBON_START_EV_SOC_PERCENT (read
off the Ford website that day) and the battery at Sol-Ark's real %. To start
over, e.g. when the EMS goes live: set CARBON_START_EV_SOC_PERCENT, delete
carbon_live_state.json and restart.

Runs independently of real_time_ems.py: it only publishes readings and makes
no EMS control decisions.
"""

import logging
import sqlite3
import sys
import os
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

from carbon_live import STATE_FILE, CarbonTracker, co2_lb, estimate_fan_avoided_lb
from Loads.openevse import get_status as get_ev_status
from ev_current_policy import excess_pv_watts
from simulation_ems import INVERTER_PV_LOSS
from watttime_client import get_grid_moer
from solArk_inverter import fetch_plant_data

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard_telemetry import build_sample
from fan_runtime import read_auto_fan_reading
from telemetry_store import numeric, record_equipment_carbon

load_dotenv(Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DRUPAL_BASE_URL = os.getenv("DRUPAL_BASE_URL", "").rstrip("/")
DRUPAL_POWER_ENDPOINT = f"{DRUPAL_BASE_URL}/api/cf/power"
DRUPAL_CARBON_ENDPOINT = f"{DRUPAL_BASE_URL}/api/cf/carbon"
CF_DATA_TOKEN = os.getenv("CF_DATA_TOKEN", "")  # must match $settings['cf_data_token'] on the site
PUBLISH_INTERVAL = int(os.getenv("POWER_PUBLISH_INTERVAL") or "60")  # seconds

# ── Carbon ────────────────────────────────────────────────────────────────────
PUBLISH_CARBON = (os.getenv("PUBLISH_CARBON") or "1") == "1"
CARBON_PUBLISH_MINUTES = int(os.getenv("CARBON_PUBLISH_MINUTES", "5"))   # one carbon row per this many minutes
CARBON_MAX_PENDING_MINUTES = 60   # /api/cf/carbon accepts up to 60 minutes per row
CARBON_START_EV_SOC_PERCENT = float(os.getenv("CARBON_START_EV_SOC_PERCENT") or "80")   # fresh start only
CARBON_FAN_POWER_W = float(os.getenv("CARBON_FAN_POWER_W") or "124")  # four fans at an assumed 31 W each
FAN_READING_MAX_AGE_SECONDS = 90

MOER_REFRESH_SECONDS = 300        # WattTime publishes MOER every 5 minutes
MOER_MAX_AGE_SECONDS = 1800       # older than this = no MOER, carbon waits
OUTDOOR_REFRESH_SECONDS = 900     # Open-Meteo "current" updates every 15 minutes
OUTDOOR_MAX_AGE_SECONDS = 3 * 3600

FARM_LATITUDE = 42.2995           # Campus Farm, Matthaei Botanical Gardens
FARM_LONGITUDE = -83.6635
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def signed_grid_watts(flow: dict) -> float:
    """Positive when selling, negative when buying.

    SolArk's grid power can be positive while buying, so direction comes from
    its gridTo flag.
    """
    watts = abs(flow["grid"])
    return -watts if flow["grid_buying"] else watts


def post_to_drupal(endpoint: str, payload: dict) -> bool:
    """POST one JSON payload with the API token; True if Drupal saved it."""
    try:
        response = requests.post(
            endpoint,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-CF-Token": CF_DATA_TOKEN,
            },
            timeout=15,
        )
        if response.status_code == 201:
            return True
        log.error("[Publish] %s returned %s: %s", endpoint, response.status_code, response.text)
    except Exception as exc:
        log.error("[Publish] Failed to reach %s: %s", endpoint, exc)
    return False


def publish_power(flow: dict) -> bool:
    """POST one SolArk reading (PV/grid/load power, W) to Drupal."""
    payload = {
        "pv_w": flow["pv"],
        "grid_w": signed_grid_watts(flow),   # + selling, - buying
        "load_w": flow["load"],
    }
    if post_to_drupal(DRUPAL_POWER_ENDPOINT, payload):
        log.info(
            "[Publish] pv=%.0fW grid=%.0fW load=%.0fW -> %s",
            payload["pv_w"], payload["grid_w"], payload["load_w"], DRUPAL_POWER_ENDPOINT,
        )
        return True
    return False


def get_outdoor_f() -> float | None:
    """Current outdoor temperature at the farm (Open-Meteo, same source as the simulation's CSVs)."""
    try:
        response = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": FARM_LATITUDE,
                "longitude": FARM_LONGITUDE,
                "current": "temperature_2m",
                "temperature_unit": "fahrenheit",
            },
            timeout=15,
        )
        response.raise_for_status()
        return float(response.json()["current"]["temperature_2m"])
    except Exception as exc:
        log.error("[Open-Meteo] Outdoor temperature fetch failed: %s", exc)
        return None


def estimate_ev_avoided_lb(sample, ev_status, moer, minutes):
    """Compare the solar-powered share of measured charging with the same energy from the grid."""
    if not ev_status or ev_status.get("state") is None:
        return None
    if not ev_status["charging"]:
        return 0.0
    ev_w = sample["ev"]
    if not numeric(ev_w) or ev_w <= 0:
        return None
    pv_w, load_w, battery_w, grid_w = (sample[key] for key in ("pv", "inverter_load_w", "battery_w", "grid"))
    if not all(numeric(value) for value in (pv_w, load_w, battery_w, grid_w)):
        return None
    # Battery is positive while discharging and grid is positive while importing.
    solar_after_loads = excess_pv_watts(pv_w * (1 - INVERTER_PV_LOSS), load_w, ev_w)
    solar_after_loads -= max(0.0, -battery_w)
    unserved_by_grid_or_battery = ev_w - max(0.0, grid_w) - max(0.0, battery_w)
    solar_to_ev_w = max(0.0, min(solar_after_loads, unserved_by_grid_or_battery))
    return co2_lb(solar_to_ev_w / 1000, moer, minutes)


class CarbonPublisher:
    """Save equipment-level estimates and publish the Drupal feed when configured."""

    def __init__(self, publish_drupal: bool) -> None:
        self.publish_drupal = publish_drupal
        self.tracker: CarbonTracker | None = None
        self.moer_lb_per_mwh: float | None = None
        self.moer_time = 0.0
        self.outdoor_f: float | None = None
        self.outdoor_time = 0.0
        self.ev_plugged = True     # truck is plugged in most of the week; kept if OpenEVSE can't be reached
        self._reset_pending()

    def _reset_pending(self) -> None:
        self.pending = {"minutes": 0, "grid_buy_kwh": 0.0, "moer_minutes": 0.0,
                        "baseline_co2_lb": 0.0, "without_ems_co2_lb": 0.0,
                        "with_ems_co2_lb": 0.0, "gas_truck_co2_lb": 0.0}

    def _refresh_moer(self) -> None:
        now = time.time()
        if now - self.moer_time >= MOER_REFRESH_SECONDS:
            moer = get_grid_moer()
            if moer is not None:
                self.moer_lb_per_mwh, self.moer_time = moer, now
    def _fan_avoided_lb(self, now: float, net_grid_w: float | None, minutes: int) -> float | None:
        reading = read_auto_fan_reading()
        if reading is None:
            return None
        stamp = reading.get("ts")
        if not numeric(stamp) or not 0 <= now - stamp <= FAN_READING_MAX_AGE_SECONDS:
            return None
        if not reading["on"]:
            return 0.0
        room_f, outdoor_f = reading.get("room_f"), reading.get("outdoor_f")
        if not all(numeric(value) for value in (room_f, outdoor_f, net_grid_w)):
            return None
        return estimate_fan_avoided_lb(
            room_f, outdoor_f, CARBON_FAN_POWER_W / 1000.0,
            net_grid_w / 1000.0, self.moer_lb_per_mwh, minutes,
        )

    def cycle(self, flow: dict, minutes: int) -> None:
        self._refresh_moer()
        now = time.time()
        if self.moer_lb_per_mwh is None or now - self.moer_time > MOER_MAX_AGE_SECONDS:
            log.warning("[Carbon] No recent MOER — skipping this reading")
            return
        ev_status = get_ev_status()
        sample = build_sample(flow, None, ev_status, None, self.moer_lb_per_mwh, None)
        try:
            record_equipment_carbon((
                (sample["ts"], minutes, self._fan_avoided_lb(sample["ts"], sample["grid"], minutes),
                 estimate_ev_avoided_lb(sample, ev_status, self.moer_lb_per_mwh, minutes)),
            ))
        except (OSError, sqlite3.Error) as exc:
            log.warning("[Carbon] Could not save Portal telemetry: %s", exc)
        if self.publish_drupal:
            self._farm_cycle(flow, sample["grid"], ev_status, minutes)

    def _farm_cycle(self, flow, net_grid_w, ev_status, minutes):
        if not all(numeric(value) for value in (flow["pv"], flow["soc"], net_grid_w)):
            log.warning("[Carbon] Incomplete solar, grid or battery readings — skipping Drupal carbon")
            return
        now = time.time()
        if now - self.outdoor_time >= OUTDOOR_REFRESH_SECONDS:
            outdoor_f = get_outdoor_f()
            if outdoor_f is not None:
                self.outdoor_f, self.outdoor_time = outdoor_f, now
        if ev_status is not None:
            self.ev_plugged = ev_status["connected"]
        if self.outdoor_f is None or now - self.outdoor_time > OUTDOOR_MAX_AGE_SECONDS:
            log.warning("[Carbon] No recent outdoor temperature — skipping this reading")
            return

        if self.tracker is None:
            if STATE_FILE.exists():
                self.tracker = CarbonTracker.load_state(outdoor_f=self.outdoor_f)
            else:
                # Fresh start from real values: truck % read off the Ford website, battery % from Sol-Ark.
                self.tracker = CarbonTracker(outdoor_f=self.outdoor_f, battery_soc_percent=flow["soc"],
                                             ev_soc=CARBON_START_EV_SOC_PERCENT / 100.0)
                log.info("[Carbon] Fresh start: truck %.0f%%, battery %.0f%% (Sol-Ark)",
                         CARBON_START_EV_SOC_PERCENT, flow["soc"])

        grid_buy_kw = max(0.0, net_grid_w) / 1000.0
        timestamp = pd.Timestamp.now(tz="America/Detroit")
        result = self.tracker.update(
            timestamp, minutes,
            pv_kw=flow["pv"] / 1000.0,
            moer_lb_per_mwh=self.moer_lb_per_mwh,
            outdoor_f=self.outdoor_f,
            ev_plugged=self.ev_plugged,
            grid_buy_kw=grid_buy_kw,
        )
        self.tracker.save_state()
        self.pending["minutes"] += minutes
        self.pending["grid_buy_kwh"] += grid_buy_kw * minutes / 60.0
        self.pending["moer_minutes"] += self.moer_lb_per_mwh * minutes
        for key in ("baseline_co2_lb", "without_ems_co2_lb", "with_ems_co2_lb", "gas_truck_co2_lb"):
            self.pending[key] += result[key]

        if self.pending["minutes"] >= CARBON_PUBLISH_MINUTES:
            self._publish()

    def _publish(self) -> None:
        pending = self.pending
        minutes = pending["minutes"]
        payload = {
            "interval_minutes": minutes,
            "moer_lb_per_mwh": pending["moer_minutes"] / minutes,       # average over the interval
            "grid_buy_kw": pending["grid_buy_kwh"] * 60.0 / minutes,    # average over the interval
            "baseline_co2_lb": pending["baseline_co2_lb"],
            "without_ems_co2_lb": pending["without_ems_co2_lb"],
            "with_ems_co2_lb": pending["with_ems_co2_lb"],
            "gas_truck_co2_lb": pending["gas_truck_co2_lb"],
        }
        if post_to_drupal(DRUPAL_CARBON_ENDPOINT, payload):
            log.info(
                "[Carbon] %d min: baseline=%.3f lb  without-ems=%.3f lb  with-ems=%.3f lb  "
                "(MOER=%.0f, grid buy=%.2f kW, plugged=%s, outdoor=%.1f°F) -> %s",
                minutes, payload["baseline_co2_lb"], payload["without_ems_co2_lb"],
                payload["with_ems_co2_lb"], payload["moer_lb_per_mwh"], payload["grid_buy_kw"],
                self.ev_plugged, self.outdoor_f, DRUPAL_CARBON_ENDPOINT,
            )
            self._reset_pending()
        elif minutes >= CARBON_MAX_PENDING_MINUTES:
            log.error("[Carbon] Could not publish for %d minutes — dropping those readings", minutes)
            self._reset_pending()
        # otherwise keep the readings and try again next cycle


def main() -> None:
    publish_drupal = bool(DRUPAL_BASE_URL and CF_DATA_TOKEN)
    if not publish_drupal and not PUBLISH_CARBON:
        # Exit cleanly so supervisord (autorestart=unexpected) doesn't keep restarting it.
        log.info("Dashboard publisher not configured: set DRUPAL_BASE_URL and CF_DATA_TOKEN in core/.env")
        sys.exit(0)

    log.info(
        "Dashboard publisher starting — poll every %ds, Drupal %s, carbon %s",
        PUBLISH_INTERVAL, "enabled" if publish_drupal else "disabled",
        "enabled" if PUBLISH_CARBON else "disabled",
    )
    carbon = CarbonPublisher(publish_drupal) if PUBLISH_CARBON else None
    cycle_minutes = max(1, round(PUBLISH_INTERVAL / 60))   # each reading stands for this many minutes
    while True:
        try:
            flow = fetch_plant_data(preserve_missing=True)
            if flow is None:
                log.warning("[SolArk] No flow data available — skipping publish")
            else:
                if publish_drupal and all(numeric(flow[key]) for key in ("pv", "grid", "load")):
                    publish_power(flow)
                if carbon is not None:
                    carbon.cycle(flow, cycle_minutes)
            time.sleep(PUBLISH_INTERVAL)
        except KeyboardInterrupt:
            log.info("Publisher stopped by user.")
            break
        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)
            time.sleep(PUBLISH_INTERVAL)


if __name__ == "__main__":
    main()
