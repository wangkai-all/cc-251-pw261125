"""Independent data-source registry.

The three local sources are deliberately independent: they publish on
different schedules, use different record shapes and do not reference
each other. Each input file carries its own published_at timestamp so
that ingestion order never determines historical truth.
"""
from __future__ import annotations

import sqlite3

from .util import utcnow

# Fictional, clearly-labelled local sources. Reliability only annotates
# evidence; it never silently picks a winner in a conflict.
DEFAULT_SOURCES = [
    {
        "source_id": "names",
        "name": "云川省地名登记册（虚构）",
        "kind": "names",
        "description": "地名与上级归属登记，按批次发布。虚构演示数据，非官方资料。",
        "reliability": 95,
    },
    {
        "source_id": "boundaries",
        "name": "云川省边界引用档案（虚构）",
        "kind": "boundaries",
        "description": "行政区边界引用与测绘批次，发布通常滞后于登记册。虚构演示数据。",
        "reliability": 85,
    },
    {
        "source_id": "notices",
        "name": "云川省沿革通知汇编（虚构）",
        "kind": "notices",
        "description": "拆分、合并、改名通知，含历史回补卷。虚构演示数据。",
        "reliability": 90,
    },
]


def register_defaults(conn: sqlite3.Connection) -> None:
    now = utcnow()
    for src in DEFAULT_SOURCES:
        conn.execute(
            "INSERT OR IGNORE INTO source(source_id, name, kind, description, "
            "reliability, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                src["source_id"],
                src["name"],
                src["kind"],
                src["description"],
                src["reliability"],
                now,
            ),
        )
    conn.commit()


def list_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM raw_record r WHERE r.source_id = s.source_id) AS record_count, "
            "(SELECT MAX(published_at) FROM raw_record r WHERE r.source_id = s.source_id) AS latest_published_at, "
            "(SELECT COUNT(*) FROM ingest_run i WHERE i.source_id = s.source_id AND i.status='failed') AS failed_runs "
            "FROM source s ORDER BY s.kind, s.source_id"
        )
    )
