"""SQLite connection management and schema initialisation."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA trusted_schema = OFF")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(config.SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
