"""SQLite storage for measured power flow, separate from controller settings."""

import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from math import isfinite
from pathlib import Path

from system_state import DATABASE

TELEMETRY_DB = Path(os.environ.get("EMS_TELEMETRY_DB", DATABASE.with_name("telemetry.db")))
LEGACY_TELEMETRY = Path(os.environ.get("EMS_TELEMETRY", DATABASE.with_name("ems-telemetry.json")))
RETENTION_DAYS = 3650
DAY = 86400
KG_PER_LB = 0.45359237

log = logging.getLogger(__name__)

ELECTRICAL_FIELDS = (
    "grid_l1_w", "grid_l2_w", "cooler_w", "inverter_load_w", "battery_w", "battery_soc",
    "l1_v", "l2_v", "l3_v", "grid_l1_a", "grid_l2_a", "cooler_a", "meter_humidity",
    "meter_temp_f", "grid_hz",
)
SOURCE_FIELDS = ("grid_source", "ev_source", "ev_current_a_source")
NUMERIC_FIELDS = ("pv", "grid", "ev", "room", "moer", "ev_current_a", "coolbot_setpoint_f", *ELECTRICAL_FIELDS)
TEXT_FIELDS = ("ev_state", "policy", *SOURCE_FIELDS)
POWER_CHART_FIELDS = ("pv", "grid", "ev", "grid_l1_w", "grid_l2_w", "cooler_w",
                      "inverter_load_w", "battery_w", "battery_soc")
EXTRA_COLUMNS = [*(f"{field} REAL" for field in ELECTRICAL_FIELDS), *(f"{field} TEXT" for field in SOURCE_FIELDS)]
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS samples (
    ts REAL PRIMARY KEY,
    pv REAL,
    grid REAL,
    ev REAL,
    room REAL,
    moer REAL,
    ev_state TEXT,
    policy TEXT,
    ev_current_a REAL,
    coolbot_setpoint_f REAL,
    {", ".join(EXTRA_COLUMNS)}
) WITHOUT ROWID
"""
CARBON_SCHEMA = """
CREATE TABLE IF NOT EXISTS carbon_samples (
    ts REAL PRIMARY KEY,
    minutes INTEGER NOT NULL CHECK (minutes > 0),
    without_ems_lb REAL NOT NULL,
    actual_lb REAL NOT NULL
) WITHOUT ROWID
"""
SAMPLE_FIELDS = ("ts", *NUMERIC_FIELDS, *TEXT_FIELDS)
INSERT = f"""
INSERT OR REPLACE INTO samples ({", ".join(SAMPLE_FIELDS)})
VALUES ({", ".join("?" for _ in SAMPLE_FIELDS)})
"""


def numeric(value):
    return type(value) in (int, float) and isfinite(value)


def _values(samples, cutoff):
    for sample in samples:
        if not isinstance(sample, dict) or not numeric(sample.get("ts")) or sample["ts"] < cutoff:
            continue
        numbers = tuple(sample[key] if numeric(sample.get(key)) else None for key in NUMERIC_FIELDS)
        strings = tuple(sample[key] if isinstance(sample.get(key), str) else None for key in TEXT_FIELDS)
        yield (sample["ts"], *numbers, *strings)


def _legacy_samples(path):
    if path is None:
        return []
    try:
        samples = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("Could not read legacy EMS telemetry: %s", exc)
        return []
    return samples if isinstance(samples, list) else []


def _begin_write(db, legacy, cutoff):
    if db.execute("PRAGMA user_version").fetchone()[0] == 0:
        db.execute("PRAGMA journal_mode=WAL")
    db.execute("BEGIN IMMEDIATE")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        db.execute(SCHEMA)
        db.executemany(INSERT, _values(_legacy_samples(legacy), cutoff))
    if version < 2:
        db.execute(CARBON_SCHEMA)
    if 1 <= version < 3:
        db.execute("ALTER TABLE samples ADD COLUMN ev_current_a REAL")
    if 1 <= version < 4:
        db.execute("ALTER TABLE samples ADD COLUMN coolbot_setpoint_f REAL")
    if 1 <= version < 5:
        for column in EXTRA_COLUMNS:
            db.execute(f"ALTER TABLE samples ADD COLUMN {column}")
    if version < 5:
        db.execute("PRAGMA user_version=5")


def record_samples(samples, path=TELEMETRY_DB, legacy=LEGACY_TELEMETRY, *, now=None):
    """Append power measurements, import legacy telemetry, and prune old rows."""
    cutoff = (time.time() if now is None else now) - RETENTION_DAYS * DAY
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        _begin_write(db, legacy, cutoff)
        db.executemany(INSERT, _values(samples, cutoff))
        db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))


def record_carbon(samples, path=TELEMETRY_DB, legacy=LEGACY_TELEMETRY, *, now=None):
    """Save timestamp, duration and the modeled/actual import emissions in pounds."""
    cutoff = (time.time() if now is None else now) - RETENTION_DAYS * DAY
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        _begin_write(db, legacy, cutoff)
        db.executemany(
            "INSERT OR REPLACE INTO carbon_samples VALUES (?, ?, ?, ?)",
            (row for row in samples if row[0] >= cutoff and all(numeric(value) for value in row)),
        )
        db.execute("DELETE FROM carbon_samples WHERE ts < ?", (cutoff,))


def _dashboard_rows(db, start, end, buckets):
    db.row_factory = sqlite3.Row
    available = {column["name"] for column in db.execute("PRAGMA table_info(samples)")}
    fields = {field: f"samples.{field}" for field in POWER_CHART_FIELDS if field in available}
    # Legacy grid readings were from eGauge, and legacy EV readings were from OpenEVSE.
    meter_fields = {
        "egauge_grid_w": (
            "CASE WHEN COALESCE(samples.grid_source, 'eGauge') = 'eGauge' THEN samples.grid END"
            if "grid_source" in available else "samples.grid"
        ),
        "egauge_ev_w": (
            "CASE WHEN samples.ev_source = 'eGauge' THEN samples.ev END"
            if "ev_source" in available else "NULL"
        ),
    }
    fields.update(meter_fields)
    meter_columns = ", ".join(f"{expr} AS {name}" for name, expr in meter_fields.items())
    latest = db.execute(
        f"SELECT *, {meter_columns} FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (end - RETENTION_DAYS * DAY, end),
    ).fetchone()
    if buckets:
        # The primary key allows an indexed scan over each bucket's time range.
        spans = ", ".join("(?, ?)" for _ in buckets)
        boundaries = [value for bucket in buckets for value in bucket]
        averages = ", ".join(f"AVG({expr}) AS {name}" for name, expr in fields.items())
        rows = db.execute(
            f"""
            WITH buckets(start, finish) AS (VALUES {spans})
            SELECT (buckets.start + buckets.finish) / 2.0 AS ts,
                   buckets.start AS bucket_start, buckets.finish AS bucket_end,
                   {averages}
            FROM buckets
            LEFT JOIN samples ON samples.ts >= buckets.start AND samples.ts < buckets.finish
            GROUP BY buckets.start, buckets.finish
            ORDER BY buckets.start
            """,
            boundaries,
        )
    else:
        columns = ", ".join(f"{expr} AS {name}" for name, expr in fields.items())
        rows = db.execute(
            f"SELECT ts, {columns} FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (start, end),
        )
    return dict(latest) if latest is not None else {}, [dict(row) for row in rows]


def read_dashboard(path, legacy, start, end, buckets=()):
    # Readers never create or change the on-disk database.
    try:
        path.stat()
    except FileNotFoundError:
        pass
    else:
        uri = path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute("BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] != 0:
                return _dashboard_rows(db, start, end, buckets)

    # The Pi may start the portal before the first successful EMS cycle.
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute(SCHEMA)
        db.executemany(INSERT, _values(_legacy_samples(legacy), end - RETENTION_DAYS * DAY))
        return _dashboard_rows(db, start, end, buckets)


def read_carbon(path, start, end, buckets=()):
    """Return net CO₂ avoided per hour, weighted by recorded minutes in each bucket."""
    if not path.exists():
        return []
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] < 2:
            return []
        if buckets:
            spans = ", ".join("(?, ?)" for _ in buckets)
            boundaries = [value for bucket in buckets for value in bucket]
            rows = db.execute(
                f"""
                WITH buckets(start, finish) AS (VALUES {spans})
                SELECT (buckets.start + buckets.finish) / 2.0 AS ts,
                       buckets.start AS bucket_start, buckets.finish AS bucket_end,
                       SUM(without_ems_lb - actual_lb) * ? / SUM(minutes) AS avoided
                FROM buckets
                LEFT JOIN carbon_samples
                     ON carbon_samples.ts >= buckets.start AND carbon_samples.ts < buckets.finish
                GROUP BY buckets.start, buckets.finish
                ORDER BY buckets.start
                """,
                (*boundaries, 60 * KG_PER_LB),
            )
        else:
            rows = db.execute(
                """
                SELECT ts, (without_ems_lb - actual_lb) * ? / minutes AS avoided
                FROM carbon_samples WHERE ts >= ? AND ts <= ? ORDER BY ts
                """,
                (60 * KG_PER_LB, start, end),
            )
        return [dict(row) for row in rows]
