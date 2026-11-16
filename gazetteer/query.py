"""Point-in-time queries: what did the region look like on business date D,
and which source batches prove it.

Evidence always points back at immutable raw_record rows. Field-level
adjudication overrides are applied on read and labelled as overrides, so
the stored historical version is never mutated.
"""
from __future__ import annotations

import sqlite3

from .util import loads


def _apply_overrides(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    data = dict(row)
    data["sources"] = loads(row["sources_json"])
    data["conflict"] = loads(row["conflict_json"]) if row["conflict_json"] else None
    data["overrides"] = []
    for dec in conn.execute(
        "SELECT * FROM adjudication WHERE subject_type='field' AND region_id=? "
        "AND effective_date=? ORDER BY decision_id",
        (row["region_id"], row["valid_from"]),
    ):
        data["overrides"].append(dict(dec))
        if dec["field_name"] in ("name", "parent_id"):
            data[dec["field_name"]] = dec["override_value"]
    return data


def region_at(conn: sqlite3.Connection, region_id: str, business_date: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM region_version WHERE region_id=? AND valid_from<=? "
        "AND (valid_to IS NULL OR valid_to>?) ORDER BY valid_from DESC LIMIT 1",
        (region_id, business_date, business_date),
    ).fetchone()
    if row is None:
        return None
    data = _apply_overrides(conn, row)
    data["evidence"] = evidence_for(conn, data["sources"])
    return data


def evidence_for(conn: sqlite3.Connection, sources: list[dict]) -> list[dict]:
    out = []
    for ref in sources:
        rr = conn.execute(
            "SELECT r.*, s.name AS source_name, s.kind FROM raw_record r "
            "JOIN source s ON s.source_id=r.source_id WHERE r.record_id=?",
            (ref["record_id"],),
        ).fetchone()
        if rr is None:  # detected (derived) evidence has record_id 0
            out.append({**ref, "payload": None, "source_name": "系统扫描"})
            continue
        item = dict(rr)
        item["payload"] = loads(rr["payload"])
        out.append(item)
    return out


def regions_at(conn: sqlite3.Connection, business_date: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM region_version WHERE valid_from<=? "
        "AND (valid_to IS NULL OR valid_to>?) ORDER BY region_id",
        (business_date, business_date),
    ).fetchall()
    return [_apply_overrides(conn, r) for r in rows]


def region_history(conn: sqlite3.Connection, region_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM region_version WHERE region_id=? ORDER BY valid_from",
        (region_id,),
    ).fetchall()
    return [_apply_overrides(conn, r) for r in rows]


def evolution_for(conn: sqlite3.Connection, region_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM evolution_link WHERE from_region_id=? OR to_region_id=? "
        "ORDER BY effective_date, link_id",
        (region_id, region_id),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["source_evidence"] = loads(r["source_evidence_json"])
        out.append(d)
    return out


def list_pending(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for row in conn.execute(
        "SELECT * FROM pending_conflict WHERE status='open' ORDER BY effective_date, conflict_id"
    ):
        d = dict(row)
        d["detail"] = loads(row["detail_json"])
        out.append(d)
    return out


def list_links(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for row in conn.execute(
        "SELECT * FROM evolution_link ORDER BY effective_date, link_id"
    ):
        d = dict(row)
        d["source_evidence"] = loads(row["source_evidence_json"])
        out.append(d)
    return out
