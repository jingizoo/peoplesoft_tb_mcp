"""In-GUI diagnostics: the checks an operator could only get from a shell.

The panel must degrade by section — a broken catalog view may blank ITS
card, never the whole panel — and the DBA summary must be paste-ready
plain text, because that text is the actual deliverable: it is what gets
sent to the person who can gather stats or add an index.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb import diagnostics  # noqa: E402
from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402


class DiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cfg = Config.sample(ROOT)
        cls.db = Database(cfg)
        cls.engine = TBEngine(cls.db, cfg)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_sqlite_reports_stats_honestly_unavailable(self) -> None:
        st = diagnostics.stats_age(self.db)
        self.assertFalse(st["available"])
        self.assertIn("Oracle", st["note"])

    def test_index_summary_lists_hot_tables_present_at_site(self) -> None:
        ix = diagnostics.index_summary(self.db)
        tables = [t["table"] for t in ix["tables"]]
        self.assertIn("PS_LEDGER", tables)
        for t in ix["tables"]:
            if not t["indexes"]:
                self.assertIn("full scan", t["note"])

    def test_timing_run_measures_the_real_playbook(self) -> None:
        tm = diagnostics.input_timings(self.engine)
        self.assertGreater(tm["total_ms"], 0)
        self.assertTrue(tm["inputs"], "no inputs timed — the measurement "
                                      "is the whole point")
        self.assertEqual(tm["inputs"],
                         sorted(tm["inputs"], key=lambda i: -i["ms"]),
                         "slowest input must come first")

    def test_run_is_fault_isolated_per_section(self) -> None:
        broken = Database(Config.sample(ROOT))
        try:
            broken.indexes = None  # force a TypeError inside one section
            out = diagnostics.run(broken, self.engine)
            self.assertIn("error", out["indexes"])
            self.assertNotIn("error", out.get("stats", {}),
                             "one broken section must not take out the rest")
            self.assertIn("dba_summary", out)
        finally:
            broken.close()

    def test_dba_summary_is_paste_ready(self) -> None:
        out = diagnostics.run(self.db, self.engine, include_timings=True)
        text = out["dba_summary"]
        self.assertIn("database health summary", text)
        self.assertIn("Close-readiness inputs", text)
        self.assertNotIn("{", text, "no unformatted placeholders")

    def test_quick_mode_never_touches_data_tables(self) -> None:
        # The quick panel must stay instant on a box where PS_LEDGER scans
        # take minutes: catalog/PRAGMA reads only, no data-table queries.
        seen = []
        orig = self.db.query
        self.db.query = lambda sql, p=None, **kw: (
            seen.append(sql), orig(sql, p, **kw))[1]
        try:
            diagnostics.run(self.db, self.engine, include_timings=False)
        finally:
            self.db.query = orig
        offenders = [s for s in seen
                     if "PRAGMA" not in s.upper()
                     and "ALL_TABLES" not in s.upper()
                     and "ALL_IND" not in s.upper()
                     and "SELECT" in s.upper()]
        self.assertEqual(offenders, [],
                         "quick diagnostics ran a data query: " +
                         "; ".join(o[:60] for o in offenders))


class EndpointTests(unittest.TestCase):
    def test_endpoint_serves_quick_diagnostics(self) -> None:
        from fastapi.testclient import TestClient
        from pstb.gui import app as gapp
        with TestClient(gapp.app) as client:
            r = client.get("/api/diagnostics")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertIn("indexes", body)
            self.assertIn("dba_summary", body)
            self.assertNotIn("timings", body,
                             "timings must stay behind include_timings=1")


if __name__ == "__main__":
    unittest.main()
