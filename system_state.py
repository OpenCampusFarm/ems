"""Shared settings for the fan, EMS, and operator portal."""

import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from core.ev_current_policy import EV_STAGED_AMPS, MIN_CURRENT_AMPS

ROOT = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("EMS_SYSTEM_DB", ROOT / "system.db"))
MODES = {"fan": ("off", "on", "auto"), "ems": ("off", "on"), "charging_policy": ("binary", "staged")}
COOLBOT_MIN_F = 34
COOLBOT_MAX_F = 65
EV_MIN_A = MIN_CURRENT_AMPS
EV_MAX_A = EV_STAGED_AMPS[-1]
DEFAULTS = {
    "fan": "auto",
    "ems": "on",
    "charging_policy": "staged",
    "coolbot_clean_f": 45,
    "coolbot_other_f": 50,
    "ev_manual_amps": None,
}
ADDED_COLUMNS = {
    "charging_policy": "TEXT NOT NULL DEFAULT 'staged' CHECK (charging_policy IN ('binary', 'staged'))",
    "charging_policy_pending": "INTEGER NOT NULL DEFAULT 0 CHECK (charging_policy_pending IN (0, 1))",
    "ev_manual_amps": (
        "INTEGER CHECK (ev_manual_amps IS NULL OR (typeof(ev_manual_amps) = 'integer' "
        f"AND (ev_manual_amps = 0 OR ev_manual_amps BETWEEN {EV_MIN_A} AND {EV_MAX_A})))"
    ),
}
for column in ("coolbot_clean_f", "coolbot_other_f"):
    ADDED_COLUMNS[column] = (
        f"INTEGER NOT NULL DEFAULT {DEFAULTS[column]} "
        f"CHECK (typeof({column}) = 'integer' AND {column} BETWEEN {COOLBOT_MIN_F} AND {COOLBOT_MAX_F})"
    )

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS system (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fan TEXT NOT NULL CHECK (fan IN ('off', 'on', 'auto')),
    ems TEXT NOT NULL CHECK (ems IN ('off', 'on')),
    fan_pending INTEGER NOT NULL DEFAULT 0 CHECK (fan_pending IN (0, 1)),
    ems_pending INTEGER NOT NULL DEFAULT 0 CHECK (ems_pending IN (0, 1)),
    {", ".join(f"{name} {definition}" for name, definition in ADDED_COLUMNS.items())}
)
"""


def read_state(path=DATABASE):
    defaults = {**DEFAULTS, **{f"{target}_pending": 0 for target in MODES}}
    try:
        path.stat()
    except FileNotFoundError:
        return defaults
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM system WHERE id = 1").fetchone()
    if row is None:
        raise ValueError("system.db is missing its system settings row")
    return {**defaults, **dict(row)}


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


@contextmanager
def _edit_state(path):
    _initialize(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        columns = {column[1] for column in db.execute("PRAGMA table_info(system)")}
        for name, definition in ADDED_COLUMNS.items():
            if name not in columns:
                db.execute(f"ALTER TABLE system ADD COLUMN {name} {definition}")
        yield db


def save_mode(target, mode, path=DATABASE):
    if mode not in MODES[target]:
        raise ValueError("Invalid controller mode")
    with _edit_state(path) as db:
        if target == "charging_policy":
            result = db.execute(
                "UPDATE system SET charging_policy = ?, charging_policy_pending = 1, ev_manual_amps = NULL "
                "WHERE id = 1 AND (charging_policy != ? OR ev_manual_amps IS NOT NULL)",
                (mode, mode),
            )
        else:
            result = db.execute(
                f"UPDATE system SET {target} = ?, {target}_pending = 1 WHERE id = 1 AND {target} != ?",
                (mode, mode),
            )
        if target == "ems" and mode == "off":
            db.execute("UPDATE system SET ev_manual_amps = NULL WHERE id = 1")
        row = db.execute(f"SELECT {target}_pending FROM system WHERE id = 1").fetchone()
    return bool(result.rowcount or row[0])


@contextmanager
def ev_control_lock(path=DATABASE):
    """Serialize charger writes by the Portal and the EMS on the Pi."""
    if os.name == "nt":
        yield  # The Portal runs on POSIX; the Windows core has no second writer.
        return
    import fcntl

    with path.with_name(path.name + "-ev.lock").open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield


@contextmanager
def edit_ev_current(amps, path=DATABASE):
    """Commit the manual current only after the charger accepts the request."""
    if type(amps) is not int or not (amps == 0 or EV_MIN_A <= amps <= EV_MAX_A):
        raise ValueError(f"Choose 0 A to pause or {EV_MIN_A}–{EV_MAX_A} A to charge.")
    with _edit_state(path) as db:
        result = db.execute("UPDATE system SET ev_manual_amps = ? WHERE id = 1 AND ems = 'on'", (amps,))
        if result.rowcount != 1:
            raise ValueError("Turn on EMS automation to set charging current.")
        yield


def save_coolbot_targets(clean_f, other_f, path=DATABASE):
    for value in (clean_f, other_f):
        if type(value) is not int or not COOLBOT_MIN_F <= value <= COOLBOT_MAX_F:
            raise ValueError(f"Enter temperatures from {COOLBOT_MIN_F} to {COOLBOT_MAX_F}°F in whole degrees.")
    if clean_f > other_f:
        raise ValueError("The clean-energy target must be at or below the Otherwise target.")
    with _edit_state(path) as db:
        result = db.execute(
            "UPDATE system SET coolbot_clean_f = ?, coolbot_other_f = ? WHERE id = 1",
            (clean_f, other_f),
        )
        if result.rowcount != 1:
            raise ValueError("system.db is missing its system settings row")


def mark_applied(target, path=DATABASE):
    if target not in MODES:
        raise ValueError("Invalid controller")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f"UPDATE system SET {target}_pending = 0 WHERE id = 1")
