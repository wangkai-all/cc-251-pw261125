"""Immutable versioned releases.

Two publishing policies model the central trade-off:

* ``early``    — publish trustworthy regions as soon as *they* are
  aligned, excluding only regions trapped in unconfirmed split/merge
  groups. Coverage note states what is held back and why.
* ``aligned``  — wait until the whole batch reconciles; any open
  conflict blocks the release, so nothing is published until alignment.

Both policies enforce the same hard invariant: for every confirmed
split/merge group effective on the business date, a release never
contains the parent and the children at the same time, so downstream
statistics cannot double-count.
"""
from __future__ import annotations

import sqlite3

from .query import list_pending
from .util import loads, utcnow


class ReleaseError(Exception):
    pass


def _group_members(conn: sqlite3.Connection, group_key: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM evolution_link WHERE group_key=?", (group_key,)
    ).fetchall()


def _versions_on_date(conn: sqlite3.Connection, business_date: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM region_version WHERE valid_from<=? "
        "AND (valid_to IS NULL OR valid_to>?) ORDER BY region_id",
        (business_date, business_date),
    ).fetchall()
    return {r["region_id"]: r for r in rows}


def _assert_no_double_count(included: set[str], confirmed_groups: list[sqlite3.Row]) -> None:
    """A split/merge only double-counts when the *parent side* and the
    *child side* of the same group are published together. Children of
    one split coexisting after the parent has retired is the normal
    end state, not double counting."""
    parents: dict[str, set[str]] = {}
    children: dict[str, set[str]] = {}
    for link in confirmed_groups:
        gk = link["group_key"]
        if link["link_type"] == "split":
            if link["from_region_id"]:
                parents.setdefault(gk, set()).add(link["from_region_id"])
            if link["to_region_id"]:
                children.setdefault(gk, set()).add(link["to_region_id"])
        else:  # merge: parents dissolve into the single child region
            if link["from_region_id"]:
                parents.setdefault(gk, set()).add(link["from_region_id"])
            if link["to_region_id"]:
                children.setdefault(gk, set()).add(link["to_region_id"])
    for gk in set(parents) | set(children):
        parent_in = parents.get(gk, set()) & included
        child_in = children.get(gk, set()) & included
        if parent_in and child_in:
            raise ReleaseError(
                f"double-count guard tripped for group {gk}: parent side "
                f"{sorted(parent_in)} and child side {sorted(child_in)} "
                "would be counted in the same release"
            )


def create_release(
    conn: sqlite3.Connection,
    business_date: str,
    policy: str,
    created_by: str = "operator",
) -> dict:
    if policy not in ("early", "aligned"):
        raise ReleaseError("policy must be 'early' or 'aligned'")
    existing = conn.execute(
        "SELECT release_id FROM release WHERE business_date=? AND policy=?",
        (business_date, policy),
    ).fetchone()
    if existing is not None:
        raise ReleaseError(f"{policy} release for {business_date} already exists; releases are immutable")

    versions = _versions_on_date(conn, business_date)
    pending = list_pending(conn)
    links = conn.execute("SELECT * FROM evolution_link ORDER BY group_key").fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for lk in links:
        groups.setdefault(lk["group_key"], []).append(lk)

    excluded: dict[str, str] = {}
    blocked: list[str] = []

    if policy == "aligned":
        for pc in pending:
            blocked.append(
                f"{pc['kind']}（{pc['region_id'] or '-'} @ {pc['effective_date'] or '-'}）"
            )
        if blocked:
            raise ReleaseError(
                "aligned 策略要求整批对齐，但仍有待决事项，拒绝发布: " + "; ".join(blocked)
            )

    # --- exclusion rules shared by both policies -------------------------
    for group_key, members in groups.items():
        statuses = {m["status"] for m in members}
        kinds = {m["link_type"] for m in members}
        if "rejected" in statuses and statuses <= {"rejected"}:
            continue  # adjudicated as not a real relationship: no exclusion
        if "proposed" in statuses:
            # Unconfirmed split/merge: keep whole group out under early
            # policy (aligned would already have blocked above).
            if kinds & {"split", "merge"}:
                for m in members:
                    for rid in (m["from_region_id"], m["to_region_id"]):
                        if rid:
                            excluded[rid] = (
                                f"沿革组 {group_key} 尚未确认，父/子地区暂不发布以免重复统计"
                            )
        # confirmed/adjudicated split & merge: only one side is active on
        # the effective date in correct data; guard below enforces it.

    # name reuse never blocks a region by itself, but is flagged in notes
    reuse_open = [
        p for p in pending if p["kind"] == "name_reuse"
    ]
    field_open = [p for p in pending if p["kind"] == "field_conflict"]
    gap_open = [p for p in pending if p["kind"] == "source_gap"]
    orphan_open = [p for p in pending if p["kind"] == "boundary_orphan"]

    included = {rid for rid in versions if rid not in excluded}

    confirmed_groups = [
        lk for lk in links
        if lk["status"] in ("confirmed", "adjudicated")
        and lk["link_type"] in ("split", "merge")
        and lk["effective_date"] <= business_date
    ]
    _assert_no_double_count(included, confirmed_groups)

    note_parts = [
        f"策略={policy}；业务日期={business_date}",
        f"候选地区 {len(versions)} 个，纳入 {len(included)} 个，暂缓 {len(excluded)} 个。",
    ]
    if excluded:
        note_parts.append(
            "未对齐范围（暂缓发布）: "
            + "; ".join(f"{rid}：{why}" for rid, why in sorted(excluded.items()))
        )
    note_parts.append(
        f"待决队列：来源缺口 {len(gap_open)}、字段冲突 {len(field_open)}、"
        f"边界孤儿 {len(orphan_open)}、名称复用 {len(reuse_open)}。"
    )
    note_parts.append(
        "覆盖代价：early 策略以牺牲整批一致性换取时效，暂缓地区在本版本中不可查询；"
        "aligned 策略要求全部待决清零后才可发布。"
    )
    note_parts.append("本系统全部数据为虚构演示，非官方资料。")

    cur = conn.execute(
        "INSERT INTO release(business_date, policy, created_at, created_by, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (business_date, policy, utcnow(), created_by, " ".join(note_parts)),
    )
    release_id = cur.lastrowid
    for rid in sorted(included):
        conn.execute(
            "INSERT INTO release_item(release_id, region_id, version_id) VALUES (?, ?, ?)",
            (release_id, rid, versions[rid]["version_id"]),
        )
    conn.commit()
    return {
        "release_id": release_id,
        "business_date": business_date,
        "policy": policy,
        "included": sorted(included),
        "excluded": excluded,
        "note": " ".join(note_parts),
    }


def list_releases(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM release ORDER BY release_id DESC")
    )


def get_release(conn: sqlite3.Connection, release_id: int) -> dict:
    rel = conn.execute("SELECT * FROM release WHERE release_id=?", (release_id,)).fetchone()
    if rel is None:
        raise ReleaseError(f"release {release_id} not found")
    items = conn.execute(
        "SELECT i.*, v.name, v.parent_id, v.valid_from, v.valid_to "
        "FROM release_item i JOIN region_version v ON v.version_id=i.version_id "
        "WHERE i.release_id=? ORDER BY i.region_id",
        (release_id,),
    ).fetchall()
    return {"release": dict(rel), "items": [dict(r) for r in items]}
