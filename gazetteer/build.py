"""Reconciliation engine: rebuild derived facts from immutable evidence.

The rebuild is deterministic and safe to re-run at any time, including
after late batches or historical backfill arrive:

* raw_record rows are the only input and are never modified;
* region / region_version / evolution_link rows are keyed by natural
  keys and upserted, so repeated rebuilds are idempotent and historical
  version rows are never deleted — a fact change closes the open
  version and opens a new one instead of overwriting the past;
* released snapshots and the version rows they point at are left
  completely untouched (a release is immutable);
* relationships the sources do not jointly support stay *proposed* and
  enter pending_conflict instead of being guessed;
* adjudications are re-applied after every rebuild, so new collection
  (including late historical backfill) can never revoke a human
  decision.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from .util import dumps, loads, utcnow


class BuildError(Exception):
    pass


def _all_records(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM raw_record ORDER BY effective_date, published_at, record_id"
        )
    )


def _reset_transient(conn: sqlite3.Connection) -> None:
    """Only the work queue is rebuilt. Releases and versions survive."""
    conn.execute("DELETE FROM pending_conflict")
    conn.commit()


def _add_pending(conn, kind: str, region_id: str | None, date: str | None, detail: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO pending_conflict(kind, region_id, effective_date, "
        "detail_json, status, created_at) VALUES (?, ?, ?, ?, 'open', ?)",
        (kind, region_id, date, dumps(detail), utcnow()),
    )


def _open_version(conn, region_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM region_version WHERE region_id=? AND valid_to IS NULL",
        (region_id,),
    ).fetchone()


def _version_at(conn, region_id: str, date: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM region_version WHERE region_id=? AND valid_from<=? "
        "AND (valid_to IS NULL OR valid_to>?) ORDER BY valid_from DESC, version_id DESC LIMIT 1",
        (region_id, date, date),
    ).fetchone()


def _evidence(row: sqlite3.Row) -> dict:
    return {
        "source_id": row["source_id"],
        "batch_id": row["batch_id"],
        "record_id": row["record_id"],
        "published_at": row["published_at"],
    }


def _merge_sources(conn, table: str, key_col: str, key: int, ev: dict) -> None:
    row = conn.execute(
        f"SELECT source_evidence_json FROM {table} WHERE {key_col}=?", (key,)
    ).fetchone()
    sources = loads(row["source_evidence_json"])
    if not any(s.get("record_id") == ev["record_id"] for s in sources):
        sources.append(ev)
        sources.sort(key=lambda s: (s.get("published_at", ""), s.get("record_id", 0)))
        conn.execute(
            f"UPDATE {table} SET source_evidence_json=? WHERE {key_col}=?",
            (dumps(sources), key),
        )


def _has_evidence(conn, source_id: str, region_id: str, date: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM raw_record WHERE source_id=? AND effective_date=? "
        "AND payload LIKE ? LIMIT 1",
        (source_id, date, f'%"{region_id}"%'),
    ).fetchall())


def build(conn: sqlite3.Connection) -> dict:
    """Rebuild region versions, evolution links and the conflict queue."""
    conn.execute("BEGIN")
    try:
        _reset_transient(conn)
        records = _all_records(conn)

        name_claims: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
        boundary_rows: list[sqlite3.Row] = []
        notice_rows: list[sqlite3.Row] = []
        for row in records:
            payload = loads(row["payload"])
            if row["kind"] == "name":
                name_claims[(payload["region_id"], row["effective_date"])].append(row)
            elif row["kind"] == "boundary":
                boundary_rows.append(row)
            else:
                notice_rows.append(row)

        # ---- pass 1: name records -> region identity + versions ----------
        for region_id, date in sorted(name_claims, key=lambda k: (k[1], k[0])):
            conn.execute(
                "INSERT OR IGNORE INTO region(region_id, first_seen_date) VALUES (?, ?)",
                (region_id, date),
            )

        for region_id, date in sorted(name_claims, key=lambda k: (k[1], k[0])):
            claims = name_claims[(region_id, date)]
            payloads = [loads(r["payload"]) for r in claims]
            names = {p["name"] for p in payloads}
            parents = {p.get("parent_id") for p in payloads}
            retired = any(p.get("status") == "retired" for p in payloads)
            open_ver = _open_version(conn, region_id)

            if retired:
                if open_ver is not None and open_ver["valid_from"] <= date:
                    conn.execute(
                        "UPDATE region_version SET valid_to=? WHERE version_id=?",
                        (date, open_ver["version_id"]),
                    )
                continue

            winner = sorted(claims, key=lambda r: (r["published_at"], r["record_id"]))[0]
            win = loads(winner["payload"])
            conflict = None
            if len(names) > 1 or len(parents) > 1:
                # The newer batch must not silently replace the historical
                # name: keep the earliest-published claim and queue the
                # disagreement for human adjudication.
                conflict = {
                    "field": "name/parent_id",
                    "kept": {"name": win["name"], "parent_id": win.get("parent_id")},
                    "claims": [
                        {
                            "name": p["name"],
                            "parent_id": p.get("parent_id"),
                            "source_id": r["source_id"],
                            "batch_id": r["batch_id"],
                            "published_at": r["published_at"],
                        }
                        for r, p in zip(claims, payloads)
                    ],
                }
                _add_pending(
                    conn, "field_conflict", region_id, date,
                    {"reason": "来源对同一业务日期的名称/归属表述不一致", **conflict},
                )

            # Same business date with conflicting claims is still ONE
            # version carrying the earliest-published value; the rival
            # claims stay in conflict_json until a field adjudication
            # overrides the value on read. So compare attributes against
            # the existing version only when its interval actually
            # started before this business date.
            if open_ver is not None and open_ver["valid_from"] < date \
                    and open_ver["name"] == win["name"] \
                    and (open_ver["parent_id"] or None) == win.get("parent_id"):
                sources = loads(open_ver["sources_json"])
                for r in claims:
                    ev = _evidence(r)
                    if not any(s["record_id"] == ev["record_id"] for s in sources):
                        sources.append(ev)
                sources.sort(key=lambda s: (s["published_at"], s["record_id"]))
                conn.execute(
                    "UPDATE region_version SET sources_json=?, conflict_json=? "
                    "WHERE version_id=?",
                    (dumps(sources),
                     dumps(conflict) if conflict else open_ver["conflict_json"],
                     open_ver["version_id"]),
                )
            else:
                existing_on_date = conn.execute(
                    "SELECT * FROM region_version WHERE region_id=? AND valid_from=?",
                    (region_id, date),
                ).fetchone()
                if existing_on_date is not None:
                    # Same-date variant batches: keep one version, merge
                    # evidence, never overwrite the historical value.
                    sources = loads(existing_on_date["sources_json"])
                    for r in claims:
                        ev = _evidence(r)
                        if not any(s["record_id"] == ev["record_id"] for s in sources):
                            sources.append(ev)
                    sources.sort(key=lambda s: (s["published_at"], s["record_id"]))
                    conn.execute(
                        "UPDATE region_version SET sources_json=?, conflict_json=? "
                        "WHERE version_id=?",
                        (dumps(sources),
                         dumps(conflict) if conflict else existing_on_date["conflict_json"],
                         existing_on_date["version_id"]),
                    )
                    continue
                if open_ver is not None:
                    conn.execute(
                        "UPDATE region_version SET valid_to=? WHERE version_id=?",
                        (date, open_ver["version_id"]),
                    )
                dedup = f"{region_id}|{date}|{win['name']}|{win.get('parent_id') or ''}"
                conn.execute(
                    "INSERT OR IGNORE INTO region_version(region_id, name, parent_id, "
                    "valid_from, dedup_key, sources_json, conflict_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (region_id, win["name"], win.get("parent_id"), date, dedup,
                     dumps([_evidence(r) for r in claims]),
                     dumps(conflict) if conflict else None, utcnow()),
                )
                ver = conn.execute(
                    "SELECT * FROM region_version WHERE dedup_key=?", (dedup,)
                ).fetchone()
                sources = loads(ver["sources_json"])
                for r in claims:
                    ev = _evidence(r)
                    if not any(s["record_id"] == ev["record_id"] for s in sources):
                        sources.append(ev)
                sources.sort(key=lambda s: (s["published_at"], s["record_id"]))
                conn.execute(
                    "UPDATE region_version SET sources_json=?, conflict_json=? "
                    "WHERE version_id=?",
                    (dumps(sources), dumps(conflict) if conflict else None,
                     ver["version_id"]),
                )

        # ---- pass 2: boundary evidence attaches to covering versions ------
        for row in sorted(boundary_rows, key=lambda r: (r["effective_date"], r["record_id"])):
            payload = loads(row["payload"])
            region_id, date = payload["region_id"], row["effective_date"]
            ver = _version_at(conn, region_id, date)
            if ver is None:
                _add_pending(
                    conn, "boundary_orphan", region_id, date,
                    {"reason": "边界引用找不到同业务日期的地名登记版本",
                     "boundary_ref": payload.get("boundary_ref"),
                     "batch_id": row["batch_id"], "published_at": row["published_at"]},
                )
                continue
            sources = loads(ver["sources_json"])
            ev = _evidence(row)
            if not any(s["record_id"] == ev["record_id"] for s in sources):
                sources.append(ev)
                sources.sort(key=lambda s: (s["published_at"], s["record_id"]))
                conn.execute(
                    "UPDATE region_version SET sources_json=? WHERE version_id=?",
                    (dumps(sources), ver["version_id"]),
                )

        # ---- pass 3: notices + name-reuse scan -> evolution links ---------
        def ensure_link(link_type, from_id, to_id, group_key, date, ev, reason=None):
            conn.execute(
                "INSERT OR IGNORE INTO evolution_link(link_type, from_region_id, "
                "to_region_id, group_key, effective_date, status, source_evidence_json, reason) "
                "VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?)",
                (link_type, from_id, to_id, group_key, date, dumps([ev]), reason),
            )
            lk = conn.execute(
                "SELECT * FROM evolution_link WHERE group_key=? AND "
                "COALESCE(from_region_id,'')=COALESCE(?, '') AND "
                "COALESCE(to_region_id,'')=COALESCE(?, '')",
                (group_key, from_id, to_id),
            ).fetchone()
            _merge_sources(conn, "evolution_link", "link_id", lk["link_id"], ev)
            return conn.execute(
                "SELECT * FROM evolution_link WHERE link_id=?", (lk["link_id"],)
            ).fetchone()

        # Independent detection: same display name, different stable identity.
        name_owners: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for vrow in conn.execute("SELECT * FROM region_version ORDER BY valid_from, version_id"):
            name_owners[vrow["name"]].append((vrow["region_id"], vrow["valid_from"]))
        for name, owners in name_owners.items():
            distinct = sorted({r for r, _ in owners})
            if len(distinct) < 2:
                continue
            seen: dict[str, str] = {}
            for region_id, valid_from in sorted(owners, key=lambda t: (t[1], t[0])):
                if region_id in seen:
                    continue
                for prior_region in list(seen.values()):
                    if prior_region == region_id:
                        continue
                    ensure_link(
                        "reuse", prior_region, region_id,
                        f"reuse:{prior_region}:{region_id}:{name}", valid_from,
                        {"source_id": "detected", "batch_id": "name-reuse-scan",
                         "record_id": 0, "published_at": valid_from + "T00:00:00Z"},
                        reason=f"名称“{name}”被不同地区复用，需人工确认是否同一沿革主体",
                    )
                seen[region_id] = region_id

        for row in sorted(notice_rows, key=lambda r: (r["effective_date"], r["record_id"])):
            p, ev = loads(row["payload"]), _evidence(row)
            date, event = row["effective_date"], p["event"]
            if event == "split":
                for child in p["to_region_ids"]:
                    ensure_link("split", p["from_region_id"], child,
                                f"split:{p['notice_id']}", date, ev, reason=p.get("title"))
            elif event == "merge":
                for parent in p["from_region_ids"]:
                    ensure_link("merge", parent, p["to_region_id"],
                                f"merge:{p['notice_id']}", date, ev, reason=p.get("title"))
            elif event == "rename":
                ensure_link("rename", p.get("from_region_id"), p["to_region_id"],
                            f"rename:{p['notice_id']}", date, ev, reason=p.get("title"))
            elif event == "reuse":
                ensure_link("reuse", p.get("from_region_id"), p["to_region_id"],
                            f"reuse:{p['notice_id']}", date, ev, reason=p.get("title"))

        # ---- pass 4: automatic confirmation only on full alignment --------
        for lk in conn.execute("SELECT * FROM evolution_link WHERE status='proposed'"):
            if lk["link_type"] == "reuse":
                # Reused names are never auto-merged: identity is a human call.
                _add_pending(
                    conn, "name_reuse", lk["to_region_id"], lk["effective_date"],
                    {"link_id": lk["link_id"], "group_key": lk["group_key"],
                     "reason": "名称复用，等待人工确认两个地区是否同一沿革主体"},
                )
                continue
            check_regions = {lk["to_region_id"]}
            if lk["link_type"] in ("split", "merge"):
                peers = conn.execute(
                    "SELECT * FROM evolution_link WHERE group_key=?", (lk["group_key"],)
                ).fetchall()
                check_regions = (
                    {x["to_region_id"] for x in peers} if lk["link_type"] == "split"
                    else {x["from_region_id"] for x in peers}
                )
            missing = []
            for region_id in sorted(r for r in check_regions if r):
                if not _has_evidence(conn, "names", region_id, lk["effective_date"]):
                    missing.append(f"{region_id} 缺地名登记")
                if not _has_evidence(conn, "boundaries", region_id, lk["effective_date"]):
                    missing.append(f"{region_id} 缺边界引用")
            if missing:
                _add_pending(
                    conn, "source_gap", lk["to_region_id"], lk["effective_date"],
                    {"link_id": lk["link_id"], "group_key": lk["group_key"],
                     "reason": "三方来源未对齐，关系保持 proposed", "missing": missing},
                )
            else:
                conn.execute(
                    "UPDATE evolution_link SET status='confirmed' WHERE link_id=?",
                    (lk["link_id"],),
                )

        # ---- pass 5: re-apply human adjudication --------------------------
        for dec in conn.execute("SELECT * FROM adjudication ORDER BY decision_id"):
            if dec["subject_type"] in ("link", "name_reuse") and dec["link_id"] is not None:
                conn.execute(
                    "UPDATE evolution_link SET status=?, decided_by=?, decided_at=?, reason=? "
                    "WHERE link_id=?",
                    ("adjudicated" if dec["decision"] == "confirm" else "rejected",
                     dec["decided_by"], dec["decided_at"], dec["rationale"], dec["link_id"]),
                )
            if dec["subject_type"] == "field":
                conn.execute(
                    "UPDATE pending_conflict SET status='resolved', "
                    "resolution=?, decision_id=? WHERE kind='field_conflict' "
                    "AND region_id=? AND effective_date=?",
                    (f"人工裁决 {dec['decided_by']}: {dec['rationale']} "
                     f"(取值 {dec['field_name']}={dec['override_value']})",
                     dec["decision_id"], dec["region_id"], dec["effective_date"]),
                )
            if dec["subject_type"] == "name_reuse":
                conn.execute(
                    "UPDATE pending_conflict SET status='resolved', "
                    "resolution=?, decision_id=? WHERE kind='name_reuse' AND region_id=?",
                    (f"人工裁决 {dec['decided_by']}: {dec['rationale']}",
                     dec["decision_id"], dec["region_id"]),
                )
            if dec["subject_type"] in ("boundary_orphan", "source_gap"):
                # Generic resolutions are re-matched by region + date + kind.
                conn.execute(
                    "UPDATE pending_conflict SET status='resolved', "
                    "resolution=?, decision_id=? WHERE kind=? AND "
                    "COALESCE(region_id,'')=COALESCE(?, '') AND "
                    "COALESCE(effective_date,'')=COALESCE(?, '')",
                    (f"人工裁决 {dec['decided_by']}: {dec['rationale']}",
                     dec["decision_id"], dec["subject_type"],
                     dec["region_id"], dec["effective_date"]),
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "regions": conn.execute("SELECT COUNT(*) FROM region").fetchone()[0],
        "versions": conn.execute("SELECT COUNT(*) FROM region_version").fetchone()[0],
        "links": conn.execute("SELECT COUNT(*) FROM evolution_link").fetchone()[0],
        "confirmed_links": conn.execute(
            "SELECT COUNT(*) FROM evolution_link WHERE status IN ('confirmed','adjudicated')"
        ).fetchone()[0],
        "pending": conn.execute(
            "SELECT COUNT(*) FROM pending_conflict WHERE status='open'"
        ).fetchone()[0],
    }
