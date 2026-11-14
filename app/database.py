"""SQLite storage for hourly PM2.5 observations and publish previews."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS published_records (
    date        TEXT    NOT NULL,
    station     TEXT    NOT NULL,
    hour        TEXT    NOT NULL,
    revision    INTEGER NOT NULL CHECK (revision >= 0),
    value       TEXT    NOT NULL,
    batch_id    TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (date, station, hour)
);

CREATE INDEX IF NOT EXISTS idx_published_date_hour
    ON published_records (date, hour, station);

CREATE TABLE IF NOT EXISTS day_versions (
    date       TEXT    PRIMARY KEY,
    version    INTEGER NOT NULL CHECK (version >= 0),
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS previews (
    id          TEXT PRIMARY KEY,
    date        TEXT NOT NULL,
    day_version INTEGER NOT NULL,
    status      TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    plan_json   TEXT NOT NULL,
    result_json TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_previews_date_status
    ON previews (date, status);
"""


def default_db_path() -> Path:
    configured = os.environ.get("PM25_DB_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "data" / "pm25.db"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open one short-lived connection with explicit transaction control."""
    connection = sqlite3.connect(
        str(db_path),
        timeout=15,
        isolation_level=None,  # transactions are started explicitly
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def initialize(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.executescript(SCHEMA)
    finally:
        connection.close()
