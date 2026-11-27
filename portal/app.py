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

from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, session, url_for

from coolbot_monitor import STALE_SECONDS, CoolBotMonitor
from process_control import apply_mode, service_status

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from power_flow import (
    CARBON_VIEW, DEFAULT_PERIOD, EGAUGE_VIEW, FARM_TIME, GAP, OVERVIEW_VIEW, PERIODS, SOLARK_VIEWS, ChartView,
    build_chart, chart_buckets,
)
from system_state import (
    COOLBOT_MAX_F, COOLBOT_MIN_F, DATABASE, MODES, mark_applied, read_state,
    save_coolbot_targets, save_mode,
)
from telemetry_store import (
    LEGACY_TELEMETRY, TELEMETRY_DB, numeric, read_carbon, read_dashboard, read_solark_history,
    read_solark_workdata,
)

log = logging.getLogger(__name__)
CONTROL_LABELS = {"fan": "Fan mode", "ems": "EMS mode", "charging_policy": "Charging policy"}


def diagnostic_field(key):
    name, _, register = key.rpartition("/")
    label, _, unit = name[:-1].rpartition("(") if name.endswith(")") else (name, "", "")
    return label, unit, register


def create_app(database=DATABASE, telemetry=TELEMETRY_DB, demo=False, *,
               legacy=LEGACY_TELEMETRY, read_coolbot=None):
    load_dotenv(Path(__file__).with_name(".env"))

    app = Flask(__name__)
    username = os.environ.get("PORTAL_USERNAME", "campusfarm")
    password = os.environ.get("PORTAL_PASSWORD", "")
    secret_key = os.environ.get("PORTAL_SECRET_KEY", "")
    valid_secret = len(secret_key) >= 32 and not secret_key.startswith("replace-with-")
    if demo:
        if read_coolbot is None:
            from demo import sample_coolbot

            read_coolbot = sample_coolbot
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

    def controller_cards(message=None, error=None):
        try:
            state = read_state(database)
        except (OSError, ValueError, sqlite3.Error):
            log.exception("[Portal] Unable to read controller settings")
            state = None
            error = "Controller settings are unavailable. Check system.db."
        return render_template("controls.html", state=state, modes=MODES, demo=demo,
                               coolbot_min=COOLBOT_MIN_F, coolbot_max=COOLBOT_MAX_F,
                               csrf=csrf_token(), message=message, error=error)

    def controls_response(message=None, error=None):
        cards = controller_cards(message, error)
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

    @app.get("/monitoring")
    def monitoring():
        key = request.args.get("range", DEFAULT_PERIOD)
        if key not in PERIODS:
            key = DEFAULT_PERIOD
        period = PERIODS[key]
        selected_view = request.args.get("view", "power")
        if selected_view not in (*SOLARK_VIEWS, "diagnostics"):
            selected_view = "power"
        solark_view = SOLARK_VIEWS.get(selected_view, SOLARK_VIEWS["power"])
        now = time.time()
        start = now - period.duration
        buckets = chart_buckets(start, now, period.bucket)
        unavailable = False
        try:
            latest, rows = read_dashboard(
                telemetry, None if demo else legacy, start, now, buckets
            )
        except (OSError, sqlite3.Error):
            log.exception("[Portal] Could not read telemetry")
            latest, rows = {}, []
            unavailable = True
        try:
            carbon_rows = read_carbon(telemetry, start, now, buckets)
        except (OSError, sqlite3.Error):
            log.exception("[Portal] Could not read carbon estimates")
            carbon_rows = []
        diagnostics_time = None
        diagnostics = {}
        try:
            diagnostics_time, diagnostics = read_solark_workdata(telemetry, now)
        except (OSError, sqlite3.Error, ValueError):
            log.exception("[Portal] Could not read Sol-Ark diagnostics")
        fields = {key: diagnostic_field(key) for key in diagnostics}
        measurable = [key for key, value in diagnostics.items() if numeric(value)]
        chosen = request.args.get("metric", "BatteryVolt(V)/183")
        if chosen not in measurable:
            chosen = next(iter(measurable), None)
        solark_rows = rows
        if selected_view == "diagnostics":
            solark_rows = []
            if chosen:
                label, unit, _ = fields[chosen]
                solark_view = ChartView(label, unit, 0.01, 3, {"reading": label}, include_zero=False)
                try:
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
        html = render_template(
            "monitoring.html", latest=latest,
            overview=build_chart(rows, now, period, OVERVIEW_VIEW), overview_view=OVERVIEW_VIEW,
            solark=build_chart(solark_rows, now, period, solark_view), solark_view=solark_view,
            egauge=build_chart(rows, now, period, EGAUGE_VIEW), egauge_view=EGAUGE_VIEW,
            carbon=build_chart(carbon_rows, now, period, CARBON_VIEW), carbon_view=CARBON_VIEW,
            selected_view=selected_view, selected_metric=chosen, diagnostics=diagnostics,
            solark_power_series=SOLARK_VIEWS["power"].series,
            fields=fields, measurable=measurable, solark_status=status,
            diagnostics_time=(datetime.fromtimestamp(diagnostics_time, FARM_TIME).strftime("%b %d, %H:%M")
                              if diagnostics_time is not None else None),
            periods=PERIODS, selected=key, period=period, unavailable=unavailable,
            last_sample=datetime.fromtimestamp(latest["ts"], FARM_TIME).strftime("%b %d, %H:%M")
            if latest else None,
            stale=bool(latest and now - latest["ts"] > GAP),
            coolbot=coolbot, coolbot_last_read=coolbot_last_read,
            ems_service="Preview" if demo else service_status("ems"),
            fan_service="Preview" if demo else service_status("fan"),
            numeric=numeric,
        )
        if (request.endpoint == "monitoring" and request.args.get("metric_form")
                and request.headers.get("HX-Request") == "true"):
            return html, {"HX-Replace-Url": url_for("index", range=key, view=selected_view, metric=chosen)}
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
        app = create_app(read_coolbot=monitor.read)
        monitor.start()
        serve(app, host="127.0.0.1", port=8080, channel_timeout=150)
    else:
        sys.exit("Usage: python app.py [--demo]")
