"""HTTP-level tests for the stdlib web UI."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer

from gazetteer import config
from gazetteer.build import build
from gazetteer.db import connect, init_db
from gazetteer.ingest import ingest_all
from gazetteer.source import register_defaults
from gazetteer.web import Handler


def _batch(batch_id, published_at, records):
    return {"batch_id": batch_id, "published_at": published_at, "records": records}


class WebTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        root = Path(self.tmp)
        (root / "sources" / "names").mkdir(parents=True)
        (root / "sources" / "boundaries").mkdir(parents=True)
        (root / "sources" / "notices").mkdir(parents=True)
        (root / "sources" / "names" / "n0.json").write_text(
            json.dumps(_batch("n0", "2020-01-10T00:00:00Z", [
                {"kind": "name", "region_id": "R1", "name": "甲市",
                 "parent_id": None, "effective_date": "2020-01-01"}])),
            encoding="utf-8")
        self._old_home, self._old_db, self._old_src = (
            config.HOME, config.DB_PATH, config.SOURCES_ROOT)
        config.HOME = root / "home"
        config.DB_PATH = root / "home" / "g.db"
        config.SOURCES_ROOT = root / "sources"
        conn = connect()
        init_db(conn)
        register_defaults(conn)
        ingest_all(conn)
        build(conn)
        conn.close()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        config.HOME, config.DB_PATH, config.SOURCES_ROOT = (
            self._old_home, self._old_db, self._old_src)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get(self, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
            return resp.status, resp.read().decode("utf-8")

    def _post(self, path: str, data: dict) -> tuple[int, str]:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def test_pages_render_and_carry_disclaimer(self):
        for path in ("/", "/sources", "/regions?date=2020-06-01",
                     "/region/R1", "/pending", "/releases", "/healthz"):
            status, text = self._get(path)
            self.assertEqual(status, 200, path)
        _, home = self._get("/")
        self.assertIn("虚构", home)
        _, regions = self._get("/regions?date=2020-06-01")
        self.assertIn("甲市", regions)

    def test_unknown_page_404(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_release_creation_and_immutability_via_http(self):
        # urllib follows the 303 redirect, so the first POST lands on the
        # release detail page (200).
        status, body = self._post("/release/create",
                                  {"business_date": "2020-06-01", "policy": "aligned"})
        self.assertEqual(status, 200)
        self.assertIn("发布 #", body)
        status2, body2 = self._post("/release/create",
                                    {"business_date": "2020-06-01", "policy": "aligned"})
        self.assertEqual(status2, 409)
        self.assertIn("immutable", body2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
