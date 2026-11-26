"""Shared desired modes for the fan, EMS, and operator portal."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("EMS_SYSTEM_DB", ROOT / "system.db"))
MODES = {"fan": ("off", "on", "auto"), "ems": ("off", "on")}
DEFAULTS = {"fan": "auto", "ems": "on"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS system (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fan TEXT NOT NULL CHECK (fan IN ('off', 'on', 'auto')),
    ems TEXT NOT NULL CHECK (ems IN ('off', 'on')),
    fan_pending INTEGER NOT NULL DEFAULT 0 CHECK (fan_pending IN (0, 1)),
    ems_pending INTEGER NOT NULL DEFAULT 0 CHECK (ems_pending IN (0, 1))
)
"""


def read_state(path=DATABASE):
    try:
        path.stat()
    except FileNotFoundError:
        return {**DEFAULTS, "fan_pending": 0, "ems_pending": 0}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM system WHERE id = 1").fetchone()
    if row is None:
        raise ValueError("system.db is missing its system settings row")
    return dict(row)


def _initialize(path):
    try:
        path.stat()
    except FileNotFoundError:
        # Publish a complete database so a controller can read it during startup.
        with TemporaryDirectory(prefix=".system-db-", dir=path.parent) as directory:
            temporary = Path(directory) / path.name
            with closing(sqlite3.connect(temporary)) as db, db:
                db.execute(SCHEMA)
                db.execute("INSERT INTO system (id, fan, ems) VALUES (1, ?, ?)",
                           (DEFAULTS["fan"], DEFAULTS["ems"]))
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass


def save_mode(target, mode, path=DATABASE):
    if mode not in MODES[target]:
        raise ValueError("Invalid controller mode")
    _initialize(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        result = db.execute(
            f"UPDATE system SET {target} = ?, {target}_pending = 1 WHERE id = 1 AND {target} != ?",
            (mode, mode),
        )
        pending = db.execute(f"SELECT {target}_pending FROM system WHERE id = 1").fetchone()
    return bool(result.rowcount or pending[0])


def mark_applied(target, path=DATABASE):
    if target not in MODES:
        raise ValueError("Invalid controller")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f"UPDATE system SET {target}_pending = 0 WHERE id = 1")
