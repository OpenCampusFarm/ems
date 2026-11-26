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
from math import ceil, isfinite
from pathlib import Path
from threading import Lock
from xmlrpc.client import Error as RPCError
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, session, url_for

from process_control import apply_mode, service_status

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from system_state import DATABASE, MODES, TELEMETRY, mark_applied, read_state, save_mode

log = logging.getLogger(__name__)

SERIES = {"pv": "Solar production", "grid": "Grid power", "ev": "EV charging"}
WINDOW = 24 * 3600
GAP = 12 * 60
FARM_TIME = ZoneInfo("America/Detroit")


def numeric(value):
    return type(value) in (float, int) and isfinite(value)


def build_chart(rows, now):
    start = now - WINDOW
    values = [row[key] for row in rows for key in SERIES if numeric(row.get(key))]
    high = max(1000, ceil(max(values, default=0) / 1000) * 1000)
    low = min(0, -ceil(-min(values, default=0) / 1000) * 1000)

    def y(value):
        return 12 + (high - value) * 152 / (high - low)

    paths = {}
    for key in SERIES:
        segments = []
        previous = None
        for row in rows:
            value = row.get(key)
            if not numeric(value):
                previous = None
                continue
            stamp = row["ts"]
            if previous is None or stamp - previous > GAP:
                segments.append([])
            x = 52 + (stamp - start) * 632 / WINDOW
            segments[-1].append((round(x, 2), round(y(value), 2)))
            previous = stamp
        paths[key] = segments

    ticks = [(12, f"{high / 1000:g}"), (y(0), "0")]
    if low:
        ticks.append((164, f"{low / 1000:g}"))
    return {
        "paths": paths, "ticks": ticks, "available": bool(values),
        "times": [datetime.fromtimestamp(start + WINDOW * i / 4, FARM_TIME).strftime("%H:%M")
                  for i in range(5)],
    }


def create_app(database=DATABASE, telemetry=TELEMETRY, demo=False):
    load_dotenv(Path(__file__).with_name(".env"))

    app = Flask(__name__)
    username = os.environ.get("PORTAL_USERNAME", "campusfarm")
    password = os.environ.get("PORTAL_PASSWORD", "")
    secret_key = os.environ.get("PORTAL_SECRET_KEY", "")
    valid_secret = len(secret_key) >= 32 and not secret_key.startswith("replace-with-")
    if demo:
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
                               csrf=csrf_token(), message=message, error=error)

    @app.get("/")
    def index():
        return render_template("index.html", controls=controller_cards(), monitoring=monitoring(),
                               demo=demo, csrf=csrf_token())

    @app.post("/mode/<target>")
    def set_mode(target):
        mode = request.form.get("mode")
        if target not in MODES or mode not in MODES[target]:
            abort(400)
        verify_csrf()
        label = "Fan" if target == "fan" else "EMS"

        def result(message=None, error=None):
            cards = controller_cards(message, error)
            if request.headers.get("HX-Request") == "true":
                return cards
            return render_template("index.html", controls=cards, monitoring=monitoring(),
                                   demo=demo, csrf=csrf_token())

        try:
            with database.with_name(database.name + ".lock").open("a") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if not save_mode(target, mode, database):
                    return result(message=f"{label} is already set to {mode}.")
                try:
                    if not demo:
                        apply_mode(target, mode)
                    mark_applied(target, database)
                except (OSError, HTTPException, RPCError, sqlite3.Error):
                    log.exception("[Portal] Unable to apply %s mode %s", label, mode)
                    return result(error=f"{label} mode was saved. The process change could not be confirmed. Retry to apply it.")
        except BlockingIOError:
            return result(error="A mode change is in progress. Try again shortly.")
        except (OSError, sqlite3.Error):
            log.exception("[Portal] Unable to save %s mode %s", label, mode)
            return result(error=f"Could not save {label} mode.")
        log.info("[Portal] %s mode applied: %s", label, mode)
        return result(message=f"{label} mode applied: {mode}.")

    @app.get("/monitoring")
    def monitoring():
        now = time.time()
        try:
            rows = json.loads(telemetry.read_text())
        except (OSError, ValueError):
            rows = []
        rows = sorted(
            (row for row in rows if isinstance(row, dict) and numeric(row.get("ts"))
             and now - WINDOW <= row["ts"] <= now),
            key=lambda row: row["ts"],
        ) if isinstance(rows, list) else []
        latest = rows[-1] if rows else {}
        return render_template(
            "monitoring.html", latest=latest, chart=build_chart(rows, now), series=SERIES,
            last_sample=datetime.fromtimestamp(latest["ts"], FARM_TIME).strftime("%b %d, %H:%M")
            if latest else None,
            stale=bool(latest and now - latest["ts"] > GAP),
            ems_service="Preview" if demo else service_status("ems"),
            fan_service="Preview" if demo else service_status("fan"),
            numeric=numeric,
        )
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
            telemetry = data / "ems-telemetry.json"
            write_samples(telemetry)
            serve(create_app(data / "system.db", telemetry, demo=True), host="127.0.0.1", port=8080,
                  channel_timeout=150)
    elif not sys.argv[1:]:
        serve(create_app(), host="127.0.0.1", port=8080, channel_timeout=150)
    else:
        sys.exit("Usage: python app.py [--demo]")
