"""Large results become streamed, private files rather than chat payloads."""
from __future__ import annotations

import csv
import io
import time
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

from pstb.batch_export import BatchExportError, BatchExportManager
from pstb.config import Config
from pstb.db import Database
from pstb.engine import TBEngine
from pstb.export import (BATCH_REPLAY_TOOLS, batch_hint, batch_to_file,
                         build_registry, preview_payload)

ROOT = Path(__file__).resolve().parents[1]


class BrowserPreviewTests(unittest.TestCase):
    def test_batch_capability_list_cannot_drift_from_export_registry(self):
        class Pack:
            def __getattr__(self, _name):
                return lambda **_kwargs: {}

        pack = Pack()
        registry = build_registry(
            engine=pack, ar=pack, modules=pack, report_runner=pack,
            coupa=pack, qas=pack)
        self.assertEqual(set(registry), set(BATCH_REPLAY_TOOLS))

    def test_large_rerunnable_result_gets_batch_action_and_100_row_preview(self):
        payload = {
            "rows": [{"id": i} for i in range(150)],
            "row_count": 150, "truncated": False,
            "source_database": "default",
        }
        hint = batch_hint("run_sql", payload, inline_rows=100)
        preview, omitted = preview_payload(payload, 100)
        self.assertTrue(hint["required"])
        self.assertEqual(len(preview["rows"]), 100)
        self.assertEqual(omitted, 50)
        self.assertEqual(preview["row_count"], 150,
                         "the preview changed the server-issued population")

    def test_truncated_result_gets_action_even_when_preview_is_under_100(self):
        hint = batch_hint(
            "run_sql", {"rows": [{"id": i} for i in range(25)],
                        "row_count": 25, "truncated": True},
            inline_rows=100)
        self.assertTrue(hint["source_truncated"])

    def test_truncated_control_without_rows_gets_no_broken_download(self):
        self.assertIsNone(batch_hint(
            "get_trial_balance", {"truncated": True, "control_status": "ok"},
            inline_rows=100))

    def test_context_trim_is_also_treated_as_a_large_result(self):
        hint = batch_hint(
            "get_ar_aging", {"customers": [{"id": i} for i in range(25)],
                             "rows_omitted_for_context": 300},
            inline_rows=100)
        self.assertTrue(hint["source_truncated"])

    def test_result_only_card_does_not_promise_a_full_batch(self):
        self.assertIsNone(batch_hint(
            "some_future_control",
            {"rows": [{"id": i} for i in range(150)], "truncated": True},
            inline_rows=100))

    def test_secondary_sources_batch_only_guarded_sql(self):
        payload = {"rows": [{"id": i} for i in range(101)]}
        self.assertIsNotNone(batch_hint(
            "run_sql", payload, inline_rows=100, source="warehouse"))
        self.assertIsNone(batch_hint(
            "get_ar_aging", payload, inline_rows=100, source="warehouse"))


class StreamedSqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = Config.sample(ROOT)
        cls.db = Database(cls.cfg)
        cls.engine = TBEngine(cls.db, cls.cfg)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_database_rows_stream_to_disk_past_old_50k_style_path(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "result.csv.part"
            progress = []
            out = batch_to_file(
                "run_sql", {
                    "sql": "SELECT ACCOUNT, POSTED_TOTAL_AMT "
                           "FROM PS_LEDGER WHERE BUSINESS_UNIT='US001'"
                }, {}, engine=self.engine, path=path, row_cap=1_000_000,
                fetch_size=17, progress=progress.append)
            self.assertEqual(out["rows"], 642)
            self.assertFalse(out["truncated"])
            self.assertGreater(len(progress), 1,
                               "all rows were retained and written at once")
            self.assertEqual(progress[-1], 642)
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.reader(fh))
            self.assertEqual(len(rows), 643)  # header + population

    def test_hard_ceiling_is_probed_and_disclosed(self):
        with TemporaryDirectory() as td:
            out = batch_to_file(
                "run_sql", {
                    "sql": "SELECT ACCOUNT FROM PS_LEDGER "
                           "WHERE BUSINESS_UNIT='US001'"
                }, {}, engine=self.engine, path=Path(td) / "cut.csv.part",
                row_cap=7, fetch_size=3)
            self.assertEqual(out["rows"], 7)
            self.assertTrue(out["truncated"])
            self.assertIn("TRUNCATED_at_7_rows", out["filename"])

    def test_stream_keeps_formula_injection_neutralized(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "safe.csv.part"
            batch_to_file(
                "run_sql", {
                    "sql": "SELECT '=cmd' AS DANGEROUS, "
                           "POSTED_TOTAL_AMT AS AMT FROM PS_LEDGER "
                           "WHERE BUSINESS_UNIT='US001'"
                }, {}, engine=self.engine, path=path, row_cap=1)
            text = path.read_text(encoding="utf-8-sig")
            self.assertIn("'=cmd", text)

    def test_file_size_ceiling_fails_instead_of_filling_the_disk(self):
        with TemporaryDirectory() as td:
            with self.assertRaisesRegex(
                    RuntimeError, "configured file-size ceiling"):
                batch_to_file(
                    "run_sql", {
                        "sql": "SELECT ACCOUNT FROM PS_LEDGER "
                               "WHERE BUSINESS_UNIT='US001'"
                    }, {}, engine=self.engine,
                    path=Path(td) / "too-large.csv.part", row_cap=100,
                    fetch_size=2, max_bytes=8)

    def test_dead_session_retry_restarts_the_sink_not_appends_duplicates(self):
        db = Database(self.cfg)
        calls = 0
        observed = []

        def fake(_sql, _params, _cap, _chunk, on_start, on_rows):
            nonlocal calls
            calls += 1
            on_start(["id"])
            observed.clear()
            if calls == 1:
                on_rows([{"id": "partial"}])
                raise RuntimeError("DPY-4011 connection closed")
            on_rows([{"id": "complete"}])
            return 1, False, 1

        db._stream_execute = fake
        db._discard_session = lambda: None
        rows, truncated, columns = db.stream_query(
            "SELECT 1", max_rows=10, on_start=lambda _cols: observed.clear(),
            on_rows=observed.extend)
        self.assertEqual((rows, truncated, columns), (1, False, 1))
        self.assertEqual(observed, [{"id": "complete"}])
        self.assertEqual(calls, 2)
        db.close()


class ManagerTests(unittest.TestCase):
    def test_job_is_private_and_finishes_with_an_expiring_link(self):
        with TemporaryDirectory() as td:
            manager = BatchExportManager(
                Path(td), max_rows=2_000_000, workers=1, max_queued=2,
                ttl_seconds=60)
            self.assertEqual(manager.max_rows, 1_000_000,
                             "configuration lifted the runtime hard ceiling")

            def produce(path, cap, progress):
                path.write_text("id\r\n1\r\n", encoding="utf-8")
                progress(1)
                return {"rows": 1, "columns": 1, "truncated": False,
                        "filename": "rows.csv", "note": "complete"}

            started = manager.submit(
                owner="oprid:A", source="default", tool="run_sql",
                producer=produce)
            for _ in range(100):
                status = manager.status(started["job_id"], owner="oprid:A")
                if status["state"] not in {"queued", "running"}:
                    break
                time.sleep(0.01)
            self.assertEqual(status["state"], "ready")
            self.assertIn("download_url", status)
            self.assertEqual(status["rows"], 1)
            with self.assertRaises(BatchExportError):
                manager.status(started["job_id"], owner="oprid:B")
            path, _ = manager.file(started["job_id"], owner="oprid:A")
            self.assertEqual(path.read_text(encoding="utf-8"), "id\n1\n")
            manager.close()


class WorkerSizingTests(unittest.TestCase):
    def test_oracle_exports_leave_one_pool_session_for_chat(self):
        from pstb.gui import app as gapp

        with mock.patch.object(gapp.cfg.db, "backend", "oracle"), \
                mock.patch.object(gapp.cfg.db, "pool_max", 2), \
                mock.patch.object(gapp.cfg.batch_exports, "workers", 8):
            self.assertEqual(gapp._batch_worker_count(), 1)

        with mock.patch.object(gapp.cfg.db, "backend", "oracle"), \
                mock.patch.object(gapp.cfg.db, "pool_max", 8), \
                mock.patch.object(gapp.cfg.batch_exports, "workers", 2):
            self.assertEqual(gapp._batch_worker_count(), 2)


class EndpointTests(unittest.TestCase):
    def test_background_endpoint_returns_status_then_download_link(self):
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp

        with TestClient(
                gapp.app, base_url="http://127.0.0.1:8000",
                client=("127.0.0.1", 50000)) as client:
            advertised = client.get("/api/meta").json()["batch_exports"]
            self.assertEqual(advertised["inline_rows"], 100)
            self.assertEqual(advertised["max_rows"], 1_000_000)
            self.assertEqual(advertised["workers"], 2)
            start = client.post(
                "/api/source/finance/batch-exports", json={
                    "tool": "run_sql",
                    "args": {"sql":
                             "SELECT ACCOUNT, POSTED_TOTAL_AMT FROM PS_LEDGER "
                             "WHERE BUSINESS_UNIT='US001'"},
                })
            self.assertEqual(start.status_code, 202, start.text)
            status = start.json()
            for _ in range(200):
                status_response = client.get(
                    f"/api/batch-exports/{status['job_id']}")
                self.assertEqual(status_response.status_code, 200)
                status = status_response.json()
                if status["state"] not in {"queued", "running"}:
                    break
                time.sleep(0.01)
            self.assertEqual(status["state"], "ready", status)
            self.assertEqual(status["rows"], 642)
            self.assertIn("finance_run_sql_", status["filename"])
            download = client.get(status["download_url"])
            self.assertEqual(download.status_code, 200)
            self.assertIn("text/csv", download.headers["content-type"])
            self.assertEqual(download.headers["X-Export-Rows"], "642")
            self.assertTrue(download.content.startswith(b"\xef\xbb\xbf"))

    def test_unknown_result_only_tool_cannot_queue_a_fake_full_export(self):
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp

        with TestClient(
                gapp.app, base_url="http://127.0.0.1:8000",
                client=("127.0.0.1", 50000)) as client:
            response = client.post(
                "/api/source/finance/batch-exports", json={
                    "tool": "coupa_health",
                    "result": {"rows": [{"id": i} for i in range(101)]},
                })
            self.assertEqual(response.status_code, 400)
            self.assertIn("cannot re-create", response.json()["detail"])

    def test_policy_figure_is_resolved_again_inside_the_batch_worker(self):
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp
        from pstb.wiki import LocalDocsWiki

        with TemporaryDirectory() as wiki_dir:
            Path(wiki_dir, "asset-policy.md").write_text(
                "# Fixed Asset Policy\n\nThe capitalization threshold is "
                "$10,000. Equipment at or above this amount is capitalized.\n",
                encoding="utf-8")
            real_policy = LocalDocsWiki(Path(wiki_dir))
            with mock.patch.object(gapp, "wiki", real_policy), TestClient(
                    gapp.app, base_url="http://127.0.0.1:8000",
                    client=("127.0.0.1", 50000)) as client:
                start = client.post(
                    "/api/source/finance/batch-exports", json={
                        "tool": "run_sql",
                        "args": {
                            "sql": "SELECT ASSET_ID, COST FROM PS_COST "
                                   "WHERE COST >= :threshold",
                            "policy_binds": {
                                "threshold": "capitalization_threshold"},
                        },
                    })
                self.assertEqual(start.status_code, 202, start.text)
                status = start.json()
                for _ in range(200):
                    response = client.get(
                        f"/api/batch-exports/{status['job_id']}")
                    self.assertEqual(response.status_code, 200)
                    status = response.json()
                    if status["state"] not in {"queued", "running"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(status["state"], "ready", status)
                self.assertEqual(status["rows"], 5)
                csv_text = client.get(status["download_url"]).content.decode(
                    "utf-8-sig")
                self.assertIn("A-0001", csv_text)
                self.assertNotIn("-35000", csv_text)


class BrowserWiringTests(unittest.TestCase):
    def test_large_card_uses_lazy_batch_route_not_sync_blob_download(self):
        page = (ROOT / "pstb/gui/static/index.html").read_text(
            encoding="utf-8")
        self.assertIn("function prepareBatchExport", page)
        self.assertIn("c.batch_export&&c.batch_export.required", page)
        self.assertIn("/batch-exports'", page)
        self.assertIn("Prepare full CSV", page)
        self.assertIn("reflects live data", page)


if __name__ == "__main__":
    unittest.main()
