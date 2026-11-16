"""Filesystem locations and runtime configuration.

Everything can be redirected with the GAZETTEER_HOME / GAZETTEER_SOURCES
environment variables so that tests and demos never touch real data.
"""
from __future__ import annotations

import os
from pathlib import Path

CWD = Path.cwd()
HOME = Path(os.environ.get("GAZETTEER_HOME", CWD / ".gazetteer"))
DB_PATH = Path(os.environ.get("GAZETTEER_DB", str(HOME / "gazetteer.db")))
SOURCES_ROOT = Path(
    os.environ.get("GAZETTEER_SOURCES", CWD / "data" / "sources")
)
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def ensure_home() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME
