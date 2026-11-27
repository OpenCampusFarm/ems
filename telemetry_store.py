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
SOLARK_POWER_FIELDS = ("solark_grid_w", "solark_generator_w", "solark_micro_w")
NUMERIC_FIELDS = ("pv", "grid", "ev", "room", "moer", "ev_current_a", "coolbot_setpoint_f",
                  *ELECTRICAL_FIELDS, *SOLARK_POWER_FIELDS)
TEXT_FIELDS = ("ev_state", "policy", *SOURCE_FIELDS, "solark_status")
POWER_CHART_FIELDS = ("pv", "grid", "ev", "grid_l1_w", "grid_l2_w", "cooler_w",
                      "inverter_load_w", "battery_w", "battery_soc", *SOLARK_POWER_FIELDS)
EXTRA_COLUMNS = [*(f"{field} REAL" for field in ELECTRICAL_FIELDS), *(f"{field} TEXT" for field in SOURCE_FIELDS)]
SOLARK_COLUMNS = [*(f"{field} REAL" for field in SOLARK_POWER_FIELDS), "solark_status TEXT"]
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
    {", ".join(EXTRA_COLUMNS + SOLARK_COLUMNS)}
) WITHOUT ROWID
"""
WORKDATA_SCHEMA = """
CREATE TABLE IF NOT EXISTS solark_workdata (
    ts REAL PRIMARY KEY,
    readings TEXT NOT NULL
) WITHOUT ROWID
"""
EGAUGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS egauge_snapshots (
    source TEXT PRIMARY KEY CHECK (source IN ('registers', 'channels')),
    ts REAL NOT NULL,
    readings TEXT NOT NULL
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
EQUIPMENT_CARBON_SCHEMA = """
CREATE TABLE IF NOT EXISTS equipment_carbon_samples (
    ts REAL PRIMARY KEY,
    minutes INTEGER NOT NULL CHECK (minutes > 0),
    fan_avoided_lb REAL,
    ev_avoided_lb REAL
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
    if 1 <= version < 6:
        for column in SOLARK_COLUMNS:
            db.execute(f"ALTER TABLE samples ADD COLUMN {column}")
    if version < 6:
        db.execute(WORKDATA_SCHEMA)
    if version < 7:
        db.execute(EGAUGE_SCHEMA)
    if version < 8:
        db.execute("ALTER TABLE carbon_samples ADD COLUMN fan_avoided_lb REAL")
    if version < 9:
        db.execute(EQUIPMENT_CARBON_SCHEMA)
        db.execute(
            """INSERT INTO equipment_carbon_samples (ts, minutes, fan_avoided_lb)
               SELECT ts, minutes, fan_avoided_lb FROM carbon_samples WHERE fan_avoided_lb IS NOT NULL"""
        )
        db.execute("PRAGMA user_version=9")


def record_samples(samples, path=TELEMETRY_DB, legacy=LEGACY_TELEMETRY, *, now=None, egauge_snapshots=()):
    """Append power measurements, import legacy telemetry, and prune old rows."""
    cutoff = (time.time() if now is None else now) - RETENTION_DAYS * DAY
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        _begin_write(db, legacy, cutoff)
        db.executemany(INSERT, _values(samples, cutoff))
        db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        db.executemany(
            """INSERT INTO egauge_snapshots (source, ts, readings) VALUES (?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET ts = excluded.ts, readings = excluded.readings
               WHERE excluded.ts >= egauge_snapshots.ts""",
            ((source, stamp, json.dumps(readings, separators=(",", ":"), allow_nan=False))
             for source, stamp, readings in egauge_snapshots if readings and numeric(stamp) and stamp >= cutoff),
        )


def read_egauge_snapshots(path):
    if not path.exists():
        return {}
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] < 7:
            return {}
        return {source: (stamp, json.loads(readings))
                for source, stamp, readings in db.execute("SELECT source, ts, readings FROM egauge_snapshots")}


def record_equipment_carbon(samples, path=TELEMETRY_DB, legacy=LEGACY_TELEMETRY, *, now=None):
    """Save interval end, minutes, and estimated fan and EV savings in pounds."""
    cutoff = (time.time() if now is None else now) - RETENTION_DAYS * DAY
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        _begin_write(db, legacy, cutoff)
        db.executemany(
            """INSERT OR REPLACE INTO equipment_carbon_samples
               (ts, minutes, fan_avoided_lb, ev_avoided_lb) VALUES (?, ?, ?, ?)""",
            ((stamp, minutes, fan_lb if numeric(fan_lb) else None, ev_lb if numeric(ev_lb) else None)
             for stamp, minutes, fan_lb, ev_lb in samples if numeric(stamp) and stamp >= cutoff),
        )
        db.execute("DELETE FROM equipment_carbon_samples WHERE ts < ?", (cutoff,))


def record_solark_workdata(stamp, readings, path=TELEMETRY_DB, legacy=LEGACY_TELEMETRY, *, now=None):
    cutoff = (time.time() if now is None else now) - RETENTION_DAYS * DAY
    if not numeric(stamp) or stamp < cutoff or not readings:
        return
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        _begin_write(db, legacy, cutoff)
        db.execute("INSERT OR REPLACE INTO solark_workdata VALUES (?, ?)",
                   (stamp, json.dumps(readings, separators=(",", ":"), allow_nan=False)))
        db.execute("DELETE FROM solark_workdata WHERE ts < ?", (cutoff,))


def read_solark_workdata(path, end):
    if not path.exists():
        return None, {}
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        if db.execute("PRAGMA user_version").fetchone()[0] < 6:
            return None, {}
        row = db.execute(
            "SELECT ts, readings FROM solark_workdata WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (end,)
        ).fetchone()
    return (row[0], json.loads(row[1])) if row else (None, {})


def read_solark_history(path, metric, start, end, buckets):
    # Read one register from each record. Unitless strings and invalid values leave gaps.
    field = "$." + json.dumps(metric, ensure_ascii=False)
    value = "CASE WHEN json_type(readings, ?) IN ('integer', 'real') THEN json_extract(readings, ?) END"
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        if buckets:
            spans = ", ".join("(?, ?)" for _ in buckets)
            boundaries = [value for bucket in buckets for value in bucket]
            rows = db.execute(
                f"""WITH buckets(start, finish) AS (VALUES {spans})
                    SELECT (start + finish) / 2.0 AS ts, start AS bucket_start, finish AS bucket_end,
                           AVG({value}) AS reading
                    FROM buckets
                    LEFT JOIN solark_workdata ON solark_workdata.ts >= start AND solark_workdata.ts < finish
                    GROUP BY start, finish ORDER BY start""",
                (*boundaries, field, field),
            )
        else:
            rows = db.execute(
                f"""SELECT ts, {value} AS reading FROM solark_workdata
                    WHERE ts >= ? AND ts <= ? ORDER BY ts""", (field, field, start, end),
            )
        return [dict(row) for row in rows]


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


def read_equipment_carbon(path, start, end, buckets=()):
    """Return fan and EV savings as kg/h rates and kg totals."""
    if not path.exists():
        return []
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version < 8:
            return []
        source = "equipment_carbon_samples" if version >= 9 else "carbon_samples"
        fields = {"fan": "fan_avoided_lb", "ev": "ev_avoided_lb" if version >= 9 else "NULL"}
        if buckets:
            spans = ", ".join("(?, ?)" for _ in buckets)
            boundaries = [value for bucket in buckets for value in bucket]
            estimates = ", ".join(
                f"SUM({field}) * ? / SUM(CASE WHEN {field} IS NOT NULL THEN minutes END) AS {key}_avoided, "
                f"SUM({field}) * ? AS {key}_avoided_kg"
                for key, field in fields.items()
            )
            rows = db.execute(
                f"""
                WITH buckets(start, finish) AS (VALUES {spans})
                SELECT (buckets.start + buckets.finish) / 2.0 AS ts,
                       buckets.start AS bucket_start, buckets.finish AS bucket_end,
                       {estimates}
                FROM buckets
                LEFT JOIN {source} ON {source}.ts >= buckets.start AND {source}.ts < buckets.finish
                GROUP BY buckets.start, buckets.finish
                ORDER BY buckets.start
                """,
                (*boundaries, 60 * KG_PER_LB, KG_PER_LB, 60 * KG_PER_LB, KG_PER_LB),
            )
        else:
            estimates = ", ".join(
                f"{field} * ? / minutes AS {key}_avoided, {field} * ? AS {key}_avoided_kg"
                for key, field in fields.items()
            )
            rows = db.execute(
                f"""
                SELECT ts, {estimates} FROM {source} WHERE ts >= ? AND ts <= ? ORDER BY ts
                """,
                (60 * KG_PER_LB, KG_PER_LB, 60 * KG_PER_LB, KG_PER_LB, start, end),
            )
        return [dict(row) for row in rows]
