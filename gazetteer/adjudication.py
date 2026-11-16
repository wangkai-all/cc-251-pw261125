"""Manual adjudication.

Adjudication only ever *adds* rows; nothing here deletes evidence or
rewrites history. After a decision is recorded the derived tables are
rebuilt, and rebuild re-applies every adjudication, so continued
collection (including old/backfilled batches) cannot undo a decision.
"""
from __future__ import annotations

import sqlite3

from .build import build
from .util import require_date, utcnow


class AdjudicationError(Exception):
    pass


def _get_link(conn: sqlite3.Connection, link_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM evolution_link WHERE link_id=?", (link_id,)
    ).fetchone()
    if row is None:
        raise AdjudicationError(f"link {link_id} does not exist")
    return row


def decide_link(
    conn: sqlite3.Connection,
    link_id: int,
    decision: str,
    rationale: str,
    decided_by: str = "operator",
) -> int:
    """Confirm or reject a proposed evolution relationship."""
    if decision not in ("confirm", "reject"):
        raise AdjudicationError("decision must be confirm or reject")
    if not rationale.strip():
        raise AdjudicationError("rationale is required for an audit trail")
    link = _get_link(conn, link_id)
    cur = conn.execute(
        "INSERT INTO adjudication(subject_type, link_id, decision, rationale, "
        "decided_by, decided_at) VALUES ('link', ?, ?, ?, ?, ?)",
        (link_id, decision, rationale, decided_by, utcnow()),
    )
    conn.commit()
    build(conn)  # re-applies the decision on top of current evidence
    return cur.lastrowid


def resolve_field_conflict(
    conn: sqlite3.Connection,
    region_id: str,
    effective_date: str,
    field_name: str,
    override_value: str,
    rationale: str,
    decided_by: str = "operator",
) -> int:
    """Settle a field-level source disagreement with an explicit value."""
    require_date(effective_date, "effective_date")
    if not rationale.strip():
        raise AdjudicationError("rationale is required for an audit trail")
    cur = conn.execute(
        "INSERT INTO adjudication(subject_type, region_id, effective_date, "
        "field_name, decision, override_value, rationale, decided_by, decided_at) "
        "VALUES ('field', ?, ?, ?, 'override', ?, ?, ?, ?)",
        (region_id, effective_date, field_name, override_value,
         rationale, decided_by, utcnow()),
    )
    conn.commit()
    build(conn)
    return cur.lastrowid


def decide_name_reuse(
    conn: sqlite3.Connection,
    link_id: int,
    decision: str,
    rationale: str,
    decided_by: str = "operator",
) -> int:
    """Record whether two regions sharing a display name are the same
    historical entity (confirm) or merely homonyms (reject)."""
    link = _get_link(conn, link_id)
    if link["link_type"] != "reuse":
        raise AdjudicationError(f"link {link_id} is not a name-reuse link")
    if decision not in ("confirm", "reject"):
        raise AdjudicationError("decision must be confirm or reject")
    cur = conn.execute(
        "INSERT INTO adjudication(subject_type, link_id, region_id, decision, "
        "rationale, decided_by, decided_at) VALUES ('name_reuse', ?, ?, ?, ?, ?, ?)",
        (link_id, link["to_region_id"], decision, rationale, decided_by, utcnow()),
    )
    conn.commit()
    build(conn)
    return cur.lastrowid


def resolve_pending(
    conn: sqlite3.Connection,
    conflict_id: int,
    resolution: str,
    decided_by: str = "operator",
) -> int:
    """Close any generic pending item (e.g. boundary orphan, source gap
    accepted by a human) with an explanatory resolution."""
    if not resolution.strip():
        raise AdjudicationError("resolution text is required for an audit trail")
    row = conn.execute(
        "SELECT * FROM pending_conflict WHERE conflict_id=?", (conflict_id,)
    ).fetchone()
    if row is None:
        raise AdjudicationError(f"pending conflict {conflict_id} does not exist")
    if row["status"] == "resolved":
        raise AdjudicationError(f"pending conflict {conflict_id} is already resolved")
    cur = conn.execute(
        "INSERT INTO adjudication(subject_type, region_id, effective_date, "
        "decision, rationale, decided_by, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (row["kind"], row["region_id"], row["effective_date"],
         "override", f"#{conflict_id}: {resolution}", decided_by, utcnow()),
    )
    conn.commit()
    build(conn)
    return cur.lastrowid


def list_decisions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM adjudication ORDER BY decision_id")
    )
