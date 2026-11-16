"""Collection: read local source files into immutable raw_record rows.

Design points
-------------
* Input files are untrusted data: we only ever parse them as JSON, never
  import or execute them. A malformed file produces a *failed* ingest_run
  and changes nothing else, so collection can resume after the file is
  fixed (failure recovery).
* The verbatim payload and its SHA-256 hash are stored forever; later
  code may reinterpret evidence but must never rewrite it.
* Re-collecting an unchanged file is a no-op (deduplicated by hash and
  per-record natural keys).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config, source as source_mod
from .util import dumps, loads, require_date, require_ts, sha256_bytes, utcnow

_ALLOWED_KINDS = {
    "names": {"name"},
    "boundaries": {"boundary"},
    "notices": {"notice"},
}

_NAME_FIELDS = ("region_id", "name", "effective_date")
_BOUNDARY_FIELDS = ("region_id", "boundary_ref", "effective_date")
_NOTICE_FIELDS = ("notice_id", "event", "effective_date")


class IngestError(Exception):
    pass


def _record_key(kind: str, record: dict) -> str:
    if kind == "name":
        return f"{record['region_id']}@{record['effective_date']}"
    if kind == "boundary":
        return f"{record['region_id']}@{record['effective_date']}"
    return record["notice_id"]


def _validate(source_kind: str, envelope: dict) -> list[dict]:
    if not isinstance(envelope, dict):
        raise IngestError("top-level JSON must be an object")
    batch_id = envelope.get("batch_id")
    published_at = envelope.get("published_at")
    records = envelope.get("records")
    if not isinstance(batch_id, str) or not batch_id:
        raise IngestError("missing batch_id")
    require_ts(published_at, "published_at")
    if not isinstance(records, list) or not records:
        raise IngestError("records must be a non-empty list")
    allowed = _ALLOWED_KINDS[source_kind]
    seen_keys: set[str] = set()
    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise IngestError(f"record #{idx} is not an object")
        kind = rec.get("kind")
        if kind not in allowed:
            raise IngestError(
                f"record #{idx}: kind {kind!r} not allowed for {source_kind}"
            )
        fields = {
            "name": _NAME_FIELDS,
            "boundary": _BOUNDARY_FIELDS,
            "notice": _NOTICE_FIELDS,
        }[kind]
        for field in fields:
            if field not in rec or rec[field] in (None, ""):
                raise IngestError(f"record #{idx}: missing field {field}")
        require_date(rec["effective_date"], f"record #{idx}.effective_date")
        if kind == "notice":
            event = rec["event"]
            if event == "split" and not rec.get("to_region_ids"):
                raise IngestError(f"record #{idx}: split needs to_region_ids")
            if event == "merge" and not rec.get("from_region_ids"):
                raise IngestError(f"record #{idx}: merge needs from_region_ids")
            if event in ("rename", "reuse") and not rec.get("to_region_id"):
                raise IngestError(f"record #{idx}: {event} needs to_region_id")
        key = _record_key(kind, rec)
        if key in seen_keys:
            raise IngestError(f"duplicate record key inside file: {key}")
        seen_keys.add(key)
    return records


def _open_run(conn, source_id: str, path: Path, digest: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO ingest_run(source_id, file_path, file_sha256, status, "
        "started_at, finished_at) VALUES (?, ?, ?, 'failed', ?, ?)",
        (source_id, str(path), digest, utcnow(), utcnow()),
    )
    return cur.lastrowid


def _finish_run(conn, run_id: int, status: str, loaded: int, error: str | None) -> None:
    conn.execute(
        "UPDATE ingest_run SET status=?, error=?, records_loaded=?, finished_at=? "
        "WHERE run_id=?",
        (status, error, loaded, utcnow(), run_id),
    )


def ingest_file(conn: sqlite3.Connection, source_id: str, path: Path) -> dict:
    """Ingest one batch file. Never raises for bad data: returns summary."""
    src = conn.execute(
        "SELECT kind FROM source WHERE source_id=?", (source_id,)
    ).fetchone()
    if src is None:
        raise IngestError(f"unknown source {source_id}; register it first")
    path = Path(path)
    started = utcnow()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        run_id = _open_run(conn, source_id, path, None)
        _finish_run(conn, run_id, "failed", 0, f"unreadable file: {exc}")
        conn.commit()
        return {"file": str(path), "status": "failed", "error": str(exc), "loaded": 0}
    digest = sha256_bytes(raw)

    previous = conn.execute(
        "SELECT run_id FROM ingest_run WHERE source_id=? AND file_path=? "
        "AND file_sha256=? AND status='success' LIMIT 1",
        (source_id, str(path), digest),
    ).fetchone()
    if previous is not None:
        # Unchanged file already collected: record the rerun but load nothing.
        run_id = _open_run(conn, source_id, path, digest)
        _finish_run(conn, run_id, "success", 0, None)
        conn.commit()
        return {"file": str(path), "status": "unchanged", "loaded": 0}

    run_id = _open_run(conn, source_id, path, digest)
    try:
        envelope = loads(raw.decode("utf-8"))
        records = _validate(src["kind"], envelope)
    except (ValueError, UnicodeDecodeError, IngestError) as exc:
        _finish_run(conn, run_id, "failed", 0, f"parse/validation: {exc}")
        conn.commit()
        return {"file": str(path), "status": "failed", "error": str(exc), "loaded": 0}

    batch_id = envelope["batch_id"]
    published_at = envelope["published_at"]
    loaded = 0
    try:
        for rec in records:
            key = _record_key(rec["kind"], rec)
            cur = conn.execute(
                "INSERT OR IGNORE INTO raw_record(source_id, batch_id, published_at, "
                "effective_date, kind, record_key, payload, file_sha256, run_id, ingested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    batch_id,
                    published_at,
                    rec["effective_date"],
                    rec["kind"],
                    key,
                    dumps(rec),
                    digest,
                    run_id,
                    started,
                ),
            )
            loaded += cur.rowcount
    except sqlite3.DatabaseError as exc:  # pragma: no cover - defensive
        conn.rollback()
        run_id = _open_run(conn, source_id, path, digest)
        _finish_run(conn, run_id, "failed", 0, f"database: {exc}")
        conn.commit()
        return {"file": str(path), "status": "failed", "error": str(exc), "loaded": 0}

    _finish_run(conn, run_id, "success", loaded, None)
    conn.commit()
    return {"file": str(path), "status": "success", "loaded": loaded}


def ingest_source(
    conn: sqlite3.Connection, source_id: str, root: Path | None = None
) -> list[dict]:
    root = Path(root) if root is not None else config.SOURCES_ROOT / source_id
    if not root.exists():
        return [{"file": str(root), "status": "failed", "error": "directory not found", "loaded": 0}]
    if root.is_file():
        return [ingest_file(conn, source_id, root)]
    results = [
        ingest_file(conn, source_id, p)
        for p in sorted(root.glob("*.json"))
    ]
    return results


def ingest_all(conn: sqlite3.Connection, root: Path | None = None) -> dict[str, list[dict]]:
    source_mod.register_defaults(conn)
    base = Path(root) if root is not None else config.SOURCES_ROOT
    out: dict[str, list[dict]] = {}
    for src in source_mod.DEFAULT_SOURCES:
        out[src["source_id"]] = ingest_source(conn, src["source_id"], base / src["source_id"])
    return out


def list_runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT i.*, s.name AS source_name FROM ingest_run i JOIN source s "
            "ON s.source_id=i.source_id ORDER BY run_id DESC"
        )
    )
