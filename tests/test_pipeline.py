"""End-to-end invariant tests with synthetic fictional batches."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from gazetteer import config  # noqa: E402
from gazetteer.adjudication import (  # noqa: E402
    AdjudicationError,
    decide_link,
    decide_name_reuse,
    resolve_field_conflict,
    resolve_pending,
)
from gazetteer.build import build  # noqa: E402
from gazetteer.db import connect, init_db  # noqa: E402
from gazetteer.ingest import ingest_file  # noqa: E402
from gazetteer.query import (  # noqa: E402
    evolution_for,
    list_pending,
    region_at,
    region_history,
    regions_at,
)
from gazetteer.release import ReleaseError, create_release, get_release  # noqa: E402
from gazetteer.source import register_defaults  # noqa: E402


def _batch(batch_id: str, published_at: str, records: list[dict]) -> dict:
    return {"batch_id": batch_id, "published_at": published_at, "records": records}


def _write(root: Path, source: str, name: str, envelope: dict) -> Path:
    d = root / source
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    return path


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.src = Path(self.tmp) / "sources"
        self.db = Path(self.tmp) / "g.db"
        self.conn = connect(self.db)
        init_db(self.conn)
        register_defaults(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ingest(self, source: str, name: str, envelope: dict):
        return ingest_file(self.conn, source, _write(self.src, source, name, envelope))

    def _seed_baseline(self) -> None:
        self._ingest("names", "n0.json", _batch("n0", "2020-01-10T00:00:00Z", [
            {"kind": "name", "region_id": "R1", "name": "甲市", "parent_id": None,
             "effective_date": "2020-01-01"},
            {"kind": "name", "region_id": "R2", "name": "乙县", "parent_id": "R1",
             "effective_date": "2020-01-01"},
        ]))
        self._ingest("boundaries", "b0.json", _batch("b0", "2020-02-01T00:00:00Z", [
            {"kind": "boundary", "region_id": "R1", "boundary_ref": "BD-1",
             "effective_date": "2020-01-01"},
            {"kind": "boundary", "region_id": "R2", "boundary_ref": "BD-2",
             "effective_date": "2020-01-01"},
        ]))
        build(self.conn)

    # 1. raw evidence is immutable and collection failures are recoverable
    def test_bad_file_fails_without_side_effects_and_is_recoverable(self):
        good = _batch("n0", "2020-01-10T00:00:00Z", [
            {"kind": "name", "region_id": "R1", "name": "甲市", "parent_id": None,
             "effective_date": "2020-01-01"}])
        bad = _write(self.src, "names", "bad.json", good)
        bad.write_text(bad.read_text(encoding="utf-8").replace('"records"', '"records"', 1)[:-5],
                       encoding="utf-8")
        res = ingest_file(self.conn, "names", bad)
        self.assertEqual(res["status"], "failed")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0], 0)
        run = self.conn.execute("SELECT status FROM ingest_run").fetchone()
        self.assertEqual(run["status"], "failed")
        # fixed file with the same logical batch ingests fine
        fixed = _write(self.src, "names", "fixed.json", good)
        res2 = ingest_file(self.conn, "names", fixed)
        self.assertEqual(res2["status"], "success")
        self.assertEqual(res2["loaded"], 1)

    def test_reingest_unchanged_file_is_noop(self):
        self._seed_baseline()
        path = self.src / "names" / "n0.json"
        again = ingest_file(self.conn, "names", path)
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0], 4)

    # 2. point-in-time query never replaces history with the present
    def test_history_is_not_overwritten_by_later_names(self):
        self._seed_baseline()
        self._ingest("names", "n1.json", _batch("n1", "2022-01-10T00:00:00Z", [
            {"kind": "name", "region_id": "R2", "name": "乙区", "parent_id": "R1",
             "effective_date": "2022-01-01"}]))
        build(self.conn)
        self.assertEqual(region_at(self.conn, "R2", "2021-06-01")["name"], "乙县")
        self.assertEqual(region_at(self.conn, "R2", "2022-06-01")["name"], "乙区")
        self.assertEqual(len(region_history(self.conn, "R2")), 2)

    # 3. split stays proposed until all three sources align
    def test_split_proposed_then_confirmed_when_evidence_lands(self):
        self._seed_baseline()
        self._ingest("notices", "g1.json", _batch("g1", "2021-07-20T00:00:00Z", [
            {"kind": "notice", "notice_id": "G1", "event": "split",
             "from_region_id": "R2", "to_region_ids": ["R3", "R4"],
             "effective_date": "2021-07-01"}]))
        build(self.conn)
        links = [l for l in evolution_for(self.conn, "R2") if l["link_type"] == "split"]
        self.assertTrue(links)
        self.assertTrue(all(l["status"] == "proposed" for l in links))
        self.assertTrue(any(p["kind"] == "source_gap" for p in list_pending(self.conn)))

        # aligned release is blocked; early release excludes whole group
        with self.assertRaises(ReleaseError):
            create_release(self.conn, "2021-08-01", "aligned")
        early = create_release(self.conn, "2021-08-01", "early")
        self.assertNotIn("R2", early["included"])
        self.assertNotIn("R3", early["included"])

        # late evidence arrives
        self._ingest("names", "n2.json", _batch("n2", "2021-08-15T00:00:00Z", [
            {"kind": "name", "region_id": "R2", "name": "乙县", "parent_id": "R1",
             "status": "retired", "effective_date": "2021-07-01"},
            {"kind": "name", "region_id": "R3", "name": "丙县", "parent_id": "R1",
             "effective_date": "2021-07-01"},
            {"kind": "name", "region_id": "R4", "name": "丁县", "parent_id": "R1",
             "effective_date": "2021-07-01"}]))
        self._ingest("boundaries", "b1.json", _batch("b1", "2021-09-01T00:00:00Z", [
            {"kind": "boundary", "region_id": "R3", "boundary_ref": "BD-3",
             "effective_date": "2021-07-01"},
            {"kind": "boundary", "region_id": "R4", "boundary_ref": "BD-4",
             "effective_date": "2021-07-01"}]))
        build(self.conn)
        links = [l for l in evolution_for(self.conn, "R2") if l["link_type"] == "split"]
        self.assertTrue(all(l["status"] == "confirmed" for l in links))
        # after confirmation, release contains children but not retired parent
        rel = create_release(self.conn, "2021-10-01", "aligned")
        items = {i["region_id"] for i in get_release(self.conn, rel["release_id"])["items"]}
        self.assertIn("R3", items)
        self.assertIn("R4", items)
        self.assertNotIn("R2", items)

    # 4. field conflict keeps one historical version and needs adjudication
    def test_field_conflict_single_version_override_on_read(self):
        self._seed_baseline()
        self._ingest("names", "c1.json", _batch("c1", "2022-03-01T00:00:00Z", [
            {"kind": "name", "region_id": "R2", "name": "新名甲", "parent_id": "R1",
             "effective_date": "2022-01-01"}]))
        self._ingest("names", "c2.json", _batch("c2", "2022-06-01T00:00:00Z", [
            {"kind": "name", "region_id": "R2", "name": "新名乙", "parent_id": "R1",
             "effective_date": "2022-01-01"}]))
        build(self.conn)
        # earliest-published claim wins; only ONE version for that date
        v = region_at(self.conn, "R2", "2022-02-01")
        self.assertEqual(v["name"], "新名甲")
        self.assertIsNotNone(v["conflict"])
        same_date = [h for h in region_history(self.conn, "R2")
                     if h["valid_from"] == "2022-01-01"]
        self.assertEqual(len(same_date), 1)
        self.assertTrue(any(p["kind"] == "field_conflict" for p in list_pending(self.conn)))

        resolve_field_conflict(self.conn, "R2", "2022-01-01", "name", "新名乙",
                               "以后发勘误为准")
        v2 = region_at(self.conn, "R2", "2022-02-01")
        self.assertEqual(v2["name"], "新名乙")
        self.assertEqual(v2["overrides"][0]["override_value"], "新名乙")
        # original raw evidence still says 新名甲
        raw = self.conn.execute(
            "SELECT payload FROM raw_record WHERE batch_id='c1'").fetchone()
        self.assertIn("新名甲", raw["payload"])
        self.assertEqual(list_pending(self.conn), [])

    # 5. name reuse can never be auto-merged
    def test_name_reuse_requires_human_decision(self):
        self._seed_baseline()
        self._ingest("names", "n3.json", _batch("n3", "2023-05-01T00:00:00Z", [
            {"kind": "name", "region_id": "R5", "name": "乙县", "parent_id": "R1",
             "effective_date": "2023-05-01"}]))
        build(self.conn)
        reuse = [l for l in evolution_for(self.conn, "R5") if l["link_type"] == "reuse"]
        self.assertEqual(len(reuse), 1)
        self.assertEqual(reuse[0]["status"], "proposed")
        with self.assertRaises(ReleaseError):
            create_release(self.conn, "2023-06-01", "aligned")
        decide_name_reuse(self.conn, reuse[0]["link_id"], "reject", "同名不同体")
        after = [l for l in evolution_for(self.conn, "R5") if l["link_type"] == "reuse"]
        self.assertEqual(after[0]["status"], "rejected")

    # 6. repeated rebuilds / late backfill cannot revoke adjudication
    def test_rebuild_and_backfill_preserve_decisions_and_releases(self):
        self.test_split_proposed_then_confirmed_when_evidence_lands()
        # create a release, then rebuild many times
        rel_before = create_release(self.conn, "2022-01-01", "early")
        for _ in range(3):
            build(self.conn)
        still = get_release(self.conn, rel_before["release_id"])
        self.assertEqual(len(still["items"]), len(
            self.conn.execute("SELECT * FROM release_item WHERE release_id=?",
                              (rel_before["release_id"],)).fetchall()))

        # backfill an old merge; it must not touch existing decisions
        self._ingest("notices", "bf.json", _batch("bf", "2022-09-30T00:00:00Z", [
            {"kind": "notice", "notice_id": "BF1", "event": "merge",
             "from_region_ids": ["R9"], "to_region_id": "R1",
             "effective_date": "2018-01-01"}]))
        build(self.conn)
        confirmed = self.conn.execute(
            "SELECT status FROM evolution_link WHERE group_key='split:G1'").fetchall()
        self.assertTrue(all(r["status"] == "confirmed" for r in confirmed))
        # releases remain immutable and re-creating is rejected
        with self.assertRaises(ReleaseError):
            create_release(self.conn, "2022-01-01", "early")

    def test_manual_link_decision_survives_rebuild(self):
        self._seed_baseline()
        self._ingest("notices", "g2.json", _batch("g2", "2021-07-20T00:00:00Z", [
            {"kind": "notice", "notice_id": "G2", "event": "split",
             "from_region_id": "R2", "to_region_ids": ["R6"],
             "effective_date": "2021-07-01"}]))
        build(self.conn)
        link = self.conn.execute(
            "SELECT link_id FROM evolution_link WHERE group_key='split:G2'").fetchone()
        decide_link(self.conn, link["link_id"], "reject", "通知系误发，人工否决")
        build(self.conn)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM evolution_link WHERE link_id=?",
                (link["link_id"],)).fetchone()["status"],
            "rejected")

    def test_generic_pending_resolution(self):
        self._seed_baseline()
        self._ingest("boundaries", "bx.json", _batch("bx", "2020-03-01T00:00:00Z", [
            {"kind": "boundary", "region_id": "RX", "boundary_ref": "BD-X",
             "effective_date": "2020-01-01"}]))
        build(self.conn)
        orphans = [p for p in list_pending(self.conn) if p["kind"] == "boundary_orphan"]
        self.assertEqual(len(orphans), 1)
        resolve_pending(self.conn, orphans[0]["conflict_id"], "边界档案有效，挂账处理")
        build(self.conn)
        self.assertEqual(
            [p for p in list_pending(self.conn) if p["kind"] == "boundary_orphan"], [])

    def test_adjudication_requires_rationale(self):
        self._seed_baseline()
        self._ingest("notices", "g3.json", _batch("g3", "2021-07-20T00:00:00Z", [
            {"kind": "notice", "notice_id": "G3", "event": "split",
             "from_region_id": "R2", "to_region_ids": ["R7"],
             "effective_date": "2021-07-01"}]))
        build(self.conn)
        link = self.conn.execute(
            "SELECT link_id FROM evolution_link WHERE group_key='split:G3'").fetchone()
        with self.assertRaises(AdjudicationError):
            decide_link(self.conn, link["link_id"], "confirm", "  ")

    def test_release_notes_describe_coverage_cost(self):
        self._seed_baseline()
        self._ingest("notices", "g1.json", _batch("g1", "2021-07-20T00:00:00Z", [
            {"kind": "notice", "notice_id": "G1", "event": "split",
             "from_region_id": "R2", "to_region_ids": ["R3", "R4"],
             "effective_date": "2021-07-01"}]))
        build(self.conn)
        early = create_release(self.conn, "2021-08-01", "early")
        self.assertIn("覆盖代价", early["note"])
        self.assertIn("未对齐范围", early["note"])
        self.assertIn("虚构", early["note"])

    def test_point_in_time_views_cover_all_seeded_regions(self):
        self._seed_baseline()
        rows = regions_at(self.conn, "2020-06-01")
        self.assertEqual({r["region_id"] for r in rows}, {"R1", "R2"})
        self.assertEqual(regions_at(self.conn, "2019-01-01"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
