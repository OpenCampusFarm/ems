import fcntl
import hmac
import json
import logging
import os
import secrets
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timedelta
from http.client import HTTPException
from pathlib import Path
from threading import Lock
from xmlrpc.client import Error as RPCError

import requests
from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, session, url_for

from coolbot_monitor import STALE_SECONDS, CoolBotMonitor
from process_control import apply_mode, service_info, service_status

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.append(str(Path(__file__).resolve().parents[1] / "core"))
from fan_runtime import read_auto_fan_status
from outdoor_monitor import OutdoorMonitor
from power_flow import (
    CARBON_VIEW, DEFAULT_PERIOD, EGAUGE_VIEW, FARM_TIME, GAP, OVERVIEW_VIEW, PERIODS, SOLARK_VIEWS, ChartView,
    build_chart, chart_buckets,
)
from system_state import (
    COOLBOT_MAX_F, COOLBOT_MIN_F, DATABASE, EV_MAX_A, EV_MIN_A, MODES, edit_ev_current,
    ev_control_lock, mark_applied, read_state, save_coolbot_targets, save_mode,
)
from telemetry_store import (
    LEGACY_TELEMETRY, TELEMETRY_DB, numeric, read_dashboard, read_egauge_snapshots,
    read_equipment_carbon, read_solark_history, read_solark_workdata,
)

log = logging.getLogger(__name__)
CONTROL_LABELS = {"fan": "Fan mode", "ems": "EMS mode", "charging_policy": "Charging policy"}


def diagnostic_field(key):
    name, _, register = key.rpartition("/")
    label, _, unit = name[:-1].rpartition("(") if name.endswith(")") else (name, "", "")
    return label, unit, register


def create_app(database=DATABASE, telemetry=TELEMETRY_DB, demo=False, *,
               legacy=LEGACY_TELEMETRY, read_coolbot=None, read_outdoor=None):
    load_dotenv(Path(__file__).with_name(".env"))

    app = Flask(__name__)
    username = os.environ.get("PORTAL_USERNAME", "campusfarm")
    password = os.environ.get("PORTAL_PASSWORD", "")
    secret_key = os.environ.get("PORTAL_SECRET_KEY", "")
    valid_secret = len(secret_key) >= 32 and not secret_key.startswith("replace-with-")
    if demo:
        from demo import sample_coolbot, sample_outdoor

        if read_coolbot is None:
            read_coolbot = sample_coolbot
        if read_outdoor is None:
            read_outdoor = sample_outdoor
        if not password or password.startswith("replace-with-"):
            username, password = "operator", "farm-preview"
        # A configured preview stays signed in across restarts with its own signing key.
        secret_key = (
            hmac.digest(secret_key.encode(), b"farm-portal-preview", "sha256").hex()
            if valid_secret else secrets.token_hex(32)
        )
    elif not password or password.startswith("replace-with-") or not valid_secret:
        raise RuntimeError(
            "Set PORTAL_PASSWORD and a random PORTAL_SECRET_KEY of at least 32 characters in portal/.env"
        )
    if not demo:
        from Loads.openevse import get_requested_current, resume_charging, set_charging

    app.config.update(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_SECURE=not demo,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(days=60),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=4096,
    )
    attempts = deque()
    login_lock = Lock()

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
        return response

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def verify_csrf():
        if not hmac.compare_digest(csrf_token().encode(), request.form.get("csrf", "").encode()):
            abort(403)

    @app.before_request
    def require_login():
        if request.endpoint not in ("static", "login") and not session.get("authenticated"):
            response = redirect(url_for("login"))
            if request.headers.get("HX-Request") == "true":
                return "", 200, {"HX-Redirect": url_for("login")}
            return response

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            verify_csrf()
            with login_lock:
                now = time.monotonic()
                while attempts and now - attempts[0] >= 60:
                    attempts.popleft()
                if len(attempts) >= 8:
                    return render_template("login.html", csrf=csrf_token(), demo=demo,
                                           error="Too many attempts. Try again in one minute."), 429
                valid_user = hmac.compare_digest(request.form.get("username", "").encode(), username.encode())
                valid_password = hmac.compare_digest(request.form.get("password", "").encode(), password.encode())
                if valid_user and valid_password:
                    attempts.clear()
                    session.clear()
                    session["authenticated"] = True
                    session.permanent = True
                    csrf_token()
                    log.info("[Portal] Operator signed in")
                    return redirect(url_for("index"), code=303)
                attempts.append(now)
                log.warning("[Portal] Sign-in rejected")
            error = "Check your username and password."
        return render_template("login.html", csrf=csrf_token(), demo=demo, error=error)

    @app.post("/logout")
    def logout():
        verify_csrf()
        session.clear()
        log.info("[Portal] Operator signed out")
        return redirect(url_for("login"), code=303)

    @app.get("/fan/status")
    def fan_auto_status():
        if demo:
            return "Auto · Off"
        status, pid = service_info("fan")
        on = read_auto_fan_status(pid) if status == "Running" and pid else None
        if on is None:
            return "Auto · Status unavailable"
        return "Auto · On" if on else "Auto · Off"

    def controller_cards(message=None, error=None, *, manual_open=False, current_only=False):
        try:
            state = read_state(database)
        except (OSError, ValueError, sqlite3.Error):
            log.exception("[Portal] Unable to read controller settings")
            state = None
            error = "Controller settings are unavailable. Check system.db."
        charger_amps = None
        if manual_open and state and state["ems"] == "on" and not error:
            if demo:
                saved_amps = state["ev_manual_amps"]
                charger_amps = 20 if saved_amps is None else saved_amps
            else:
                charger_amps = get_requested_current()
                if charger_amps and not EV_MIN_A <= charger_amps <= EV_MAX_A:
                    charger_amps = None
        return render_template("ev_current.html" if current_only else "controls.html",
                               state=state, modes=MODES, demo=demo, manual_open=manual_open,
                               fan_status=fan_auto_status() if not current_only and state and state["fan"] == "auto" else None,
                               coolbot_min=COOLBOT_MIN_F, coolbot_max=COOLBOT_MAX_F,
                               ev_min=EV_MIN_A, ev_max=EV_MAX_A, charger_amps=charger_amps,
                               csrf=csrf_token(), message=message, error=None if current_only else error,
                               current_error=error if current_only else None)

    def controls_response(message=None, error=None, *, manual_open=False):
        cards = controller_cards(message, error, manual_open=manual_open)
        if request.headers.get("HX-Request") == "true":
            return cards
        return render_template("index.html", controls=cards, monitoring=monitoring(),
                               demo=demo, csrf=csrf_token())

    @app.get("/")
    def index():
        return render_template("index.html", controls=controller_cards(), monitoring=monitoring(),
                               demo=demo, csrf=csrf_token())

    @app.post("/coolbot/targets")
    def set_coolbot_targets():
        verify_csrf()
        try:
            clean_f = int(request.form.get("clean_f", ""))
            other_f = int(request.form.get("other_f", ""))
        except ValueError:
            return controls_response(error="Enter both CoolBot temperatures in whole degrees Fahrenheit.")
        try:
            save_coolbot_targets(clean_f, other_f, database)
        except ValueError as error:
            return controls_response(error=str(error))
        except (OSError, sqlite3.Error):
            log.exception("[Portal] Unable to save CoolBot targets")
            return controls_response(error="Could not save the CoolBot targets.")
        log.info("[Portal] CoolBot targets saved: clean=%d°F, otherwise=%d°F", clean_f, other_f)
        return controls_response(message="CoolBot targets saved. EMS uses them on its next cycle while automation is on.")

    @app.post("/mode/<target>")
    def set_mode(target):
        mode = request.form.get("mode")
        if target == "charging_policy" and mode == "manual":
            verify_csrf()
            return controls_response(manual_open=True)
        if target not in MODES or mode not in MODES[target]:
            abort(400)
        verify_csrf()
        label = CONTROL_LABELS[target]
        display_mode = mode.title()

        try:
            with database.with_name(database.name + ".lock").open("a") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if not save_mode(target, mode, database):
                    return controls_response(message=f"{label} is already set to {display_mode}.")
                try:
                    process_target, process_mode = target, mode
                    if target == "charging_policy":
                        process_target, process_mode = "ems", read_state(database)["ems"]
                    saved_for_later = target == "charging_policy" and process_mode == "off"
                    if not demo and not saved_for_later:
                        apply_mode(process_target, process_mode)
                    if target == "ems" and mode == "on":
                        mark_applied("charging_policy", database)
                    mark_applied(target, database)
                except (OSError, ValueError, HTTPException, RPCError, sqlite3.Error):
                    log.exception("[Portal] Unable to apply %s %s", label, mode)
                    return controls_response(error=f"{label} was saved. The process change could not be confirmed. Retry to apply it.")
        except BlockingIOError:
            return controls_response(error="A mode change is in progress. Try again shortly.")
        except (OSError, sqlite3.Error):
            log.exception("[Portal] Unable to save %s %s", label, mode)
            return controls_response(error=f"Could not save {label.lower()}.")
        if saved_for_later:
            log.info("[Portal] Charging policy saved while EMS is off: %s", mode)
            return controls_response(message=f"{label} saved as {display_mode}. It will be used when EMS is on.")
        log.info("[Portal] %s applied: %s", label, mode)
        return controls_response(message=f"{label} applied: {display_mode}.")

    @app.post("/charging/current")
    def set_ev_current():
        verify_csrf()
        error = None
        try:
            mode = request.form.get("mode")
            if mode == "pause":
                amps = 0
            elif mode == "minimum":
                amps = EV_MIN_A
            else:
                try:
                    amps = int(request.form.get("amps", ""))
                except ValueError:
                    raise ValueError(f"Choose a current between {EV_MIN_A} and {EV_MAX_A} A.") from None
            with database.with_name(database.name + ".lock").open("a") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with ev_control_lock(database):
                    previous = read_state(database)["ev_manual_amps"] if amps > 0 and not demo else None
                    with edit_ev_current(amps, database):
                        if not demo:
                            if amps > 0 and (previous == 0 or (previous is None and get_requested_current() == 0)):
                                resume_charging(amps)
                            else:
                                set_charging(amps > 0, amps)
            log.info("[Portal] Manual EV charging requested %d A", amps)
        except BlockingIOError:
            error = "A mode change is in progress. Try again shortly."
        except (requests.RequestException, OSError, sqlite3.Error):
            log.exception("[Portal] Could not confirm manual EV current")
            error = "Could not confirm the charging current. Check OpenEVSE and try again."
        except ValueError as exc:
            error = str(exc)
        if request.headers.get("HX-Request") == "true":
            return controller_cards(error=error, current_only=True)
        return controls_response(error=error, manual_open=True)

    @app.get("/monitoring")
    def monitoring():
        shared_range = request.args.get("range", DEFAULT_PERIOD)
        if shared_range not in PERIODS:
            shared_range = DEFAULT_PERIOD
        ranges = {}
        for chart in ("overview", "solark", "egauge", "carbon"):
            selected = request.args.get(f"{chart}_range", shared_range)
            ranges[chart] = selected if selected in PERIODS else shared_range
        selected_view = request.args.get("view", "power")
        if selected_view not in (*SOLARK_VIEWS, "diagnostics"):
            selected_view = "power"
        solark_view = SOLARK_VIEWS.get(selected_view, SOLARK_VIEWS["power"])
        now = time.time()

        def window(key):
            period = PERIODS[key]
            start = now - period.duration
            return start, chart_buckets(start, now, period.bucket)

        unavailable = False
        latest = {}
        meter_rows = {}
        meter_ranges = [ranges["overview"], ranges["egauge"]]
        if selected_view != "diagnostics":
            meter_ranges.append(ranges["solark"])
        for key in dict.fromkeys(meter_ranges):
            start, buckets = window(key)
            try:
                sample, meter_rows[key] = read_dashboard(
                    telemetry, None if demo else legacy, start, now, buckets
                )
                if not latest:
                    latest = sample
            except (OSError, sqlite3.Error):
                log.exception("[Portal] Could not read telemetry")
                meter_rows[key] = []
                unavailable = True
        try:
            start, buckets = window(ranges["carbon"])
            carbon_rows = read_equipment_carbon(telemetry, start, now, buckets)
        except (OSError, sqlite3.Error):
            log.exception("[Portal] Could not read carbon estimates")
            carbon_rows = []
        carbon_totals = {}
        for key in CARBON_VIEW.series:
            field = f"{key}_kg"
            values = [row[field] for row in carbon_rows if numeric(row[field])]
            carbon_totals[key] = sum(values) if values else None
        diagnostics_time = None
        diagnostics = {}
        try:
            diagnostics_time, diagnostics = read_solark_workdata(telemetry, now)
        except (OSError, sqlite3.Error, ValueError):
            log.exception("[Portal] Could not read Sol-Ark diagnostics")
        egauge_details = {}
        try:
            for source, (stamp, readings) in read_egauge_snapshots(telemetry).items():
                formatted_time = datetime.fromtimestamp(stamp, FARM_TIME).strftime("%b %d, %H:%M")
                egauge_details[source] = (formatted_time, readings, now - stamp > GAP)
        except (OSError, sqlite3.Error, ValueError):
            log.exception("[Portal] Could not read eGauge details")
        fields = {key: diagnostic_field(key) for key in diagnostics}
        measurable = [key for key, value in diagnostics.items() if numeric(value)]
        chosen = request.args.get("metric", "BatteryVolt(V)/183")
        if chosen not in measurable:
            chosen = next(iter(measurable), None)
        solark_rows = meter_rows.get(ranges["solark"], [])
        if selected_view == "diagnostics":
            solark_rows = []
            if chosen:
                label, unit, _ = fields[chosen]
                solark_view = ChartView(label, unit, 0.01, 3, {"reading": label}, include_zero=False)
                try:
                    start, buckets = window(ranges["solark"])
                    solark_rows = read_solark_history(telemetry, chosen, start, now, buckets)
                except (OSError, sqlite3.Error, ValueError):
                    log.exception("[Portal] Could not read historical Sol-Ark diagnostics")
            else:
                solark_view = ChartView("Inverter diagnostics", "", 1, 0, {})
        status = json.loads(latest.get("solark_status") or "{}")
        coolbot = read_coolbot() if read_coolbot else None
        coolbot_last_read = (
            datetime.fromtimestamp(coolbot.ts, FARM_TIME).strftime("%b %d, %H:%M")
            if coolbot and now - coolbot.ts > STALE_SECONDS
            else None
        )
        range_params = {f"{chart}_range": key for chart, key in ranges.items()}
        query = {**range_params, "view": selected_view, "metric": chosen}

        def chart_url(endpoint, **changes):
            return url_for(endpoint, **{**query, **changes})

        def range_url(endpoint, chart, key):
            return chart_url(endpoint, **{f"{chart}_range": key})

        chart_periods = {chart: PERIODS[key] for chart, key in ranges.items()}
        html = render_template(
            "monitoring.html", latest=latest,
            overview=build_chart(meter_rows[ranges["overview"]], now, chart_periods["overview"], OVERVIEW_VIEW),
            overview_view=OVERVIEW_VIEW,
            solark=build_chart(solark_rows, now, chart_periods["solark"], solark_view), solark_view=solark_view,
            egauge=build_chart(meter_rows[ranges["egauge"]], now, chart_periods["egauge"], EGAUGE_VIEW),
            egauge_view=EGAUGE_VIEW, egauge_details=egauge_details,
            carbon=build_chart(carbon_rows, now, chart_periods["carbon"], CARBON_VIEW), carbon_view=CARBON_VIEW,
            carbon_totals=carbon_totals,
            selected_view=selected_view, selected_metric=chosen, diagnostics=diagnostics,
            solark_power_series=SOLARK_VIEWS["power"].series,
            fields=fields, measurable=measurable, solark_status=status,
            diagnostics_time=(datetime.fromtimestamp(diagnostics_time, FARM_TIME).strftime("%b %d, %H:%M")
                              if diagnostics_time is not None else None),
            periods=PERIODS, ranges=ranges, chart_periods=chart_periods, unavailable=unavailable,
            chart_url=chart_url, range_url=range_url, range_params=range_params,
            last_sample=datetime.fromtimestamp(latest["ts"], FARM_TIME).strftime("%b %d, %H:%M")
            if latest else None,
            stale=bool(latest and now - latest["ts"] > GAP),
            coolbot=coolbot, coolbot_last_read=coolbot_last_read,
            outdoor=read_outdoor() if read_outdoor else None,
            ems_service="Preview" if demo else service_status("ems"),
            fan_service="Preview" if demo else service_status("fan"),
            numeric=numeric,
        )
        if (request.endpoint == "monitoring" and request.args.get("metric_form")
                and request.headers.get("HX-Request") == "true"):
            return html, {"HX-Replace-Url": chart_url("index")}
        return html
    log.info("[Portal] Initialized in %s mode", "demo" if demo else "production")
    return app


if __name__ == "__main__":
    from tempfile import TemporaryDirectory
    from waitress import serve

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if sys.argv[1:] == ["--demo"]:
        from demo import write_samples

        with TemporaryDirectory(prefix="campus-portal-") as directory:
            data = Path(directory)
            telemetry = data / "telemetry.db"
            write_samples(telemetry)
            serve(create_app(data / "system.db", telemetry, demo=True), host="127.0.0.1", port=8080,
                  channel_timeout=150)
    elif not sys.argv[1:]:
        monitor = CoolBotMonitor()
        outdoor_monitor = OutdoorMonitor()
        app = create_app(read_coolbot=monitor.read, read_outdoor=outdoor_monitor.read)
        monitor.start()
        outdoor_monitor.start()
        serve(app, host="127.0.0.1", port=8080, channel_timeout=150)
    else:
        sys.exit("Usage: python app.py [--demo]")
