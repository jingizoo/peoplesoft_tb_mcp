"""Index-aware planning: reason about a join BEFORE running it.

Queries on the real instance time out rather than erroring, and a timeout
teaches nothing. Three mechanisms turn that into something actionable:

  - the index catalog (db.indexes) says what each table's indexes LEAD with,
    which is the one fact a query writer needs and never had
  - explain_query asks the optimizer for the plan without executing, and
    translates full scans into "add these leading columns to your WHERE"
  - the heavy journal probes in the integrity check pre-flight their plan:
    a full-year scan gets narrowed to the current period (disclosed), and
    when even that would scan everything, the check refuses in milliseconds
    with the index that would fix it — instead of a two-minute timeout that
    takes the whole playbook down.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import EngineError, TBEngine  # noqa: E402
from pstb.playbooks import PlaybookRunner  # noqa: E402

H_JOIN = """SELECT J.JOURNAL_ID, SUM(J.MONETARY_AMOUNT)
  FROM PS_JRNL_LN J JOIN PS_JRNL_HEADER H
    ON H.BUSINESS_UNIT = J.BUSINESS_UNIT AND H.JOURNAL_ID = J.JOURNAL_ID
 WHERE H.FISCAL_YEAR = 2026 GROUP BY J.JOURNAL_ID"""


class _EngineCase(unittest.TestCase):
    def setUp(self) -> None:
        cfg = load_config(str(ROOT / "config.yaml"))
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)

    def tearDown(self) -> None:
        self.db.close()


class IndexCatalogTests(_EngineCase):
    def test_indexes_report_their_column_order(self) -> None:
        idx = self.db.indexes("PS_LEDGER")
        self.assertTrue(idx)
        lead = idx[0]["columns"][0]
        self.assertEqual(lead, "BUSINESS_UNIT",
                         "column order is the point — an index is only "
                         "usable through its leading columns")

    def test_an_unreadable_table_reports_unknown_not_none(self) -> None:
        self.assertEqual(self.db.indexes("NO_SUCH_TABLE_XYZ"), [])

    def test_describe_table_carries_the_indexes(self) -> None:
        out = self.engine.describe_table("PS_LEDGER")
        self.assertTrue(out["indexes"])
        self.assertIn("LEADING", out["index_note"])

    def test_a_table_with_no_index_says_so_plainly(self) -> None:
        out = self.engine.describe_table("PS_ITEM")
        self.assertEqual(out["indexes"], [])
        self.assertIn("full", out["index_note"].lower())


class ExplainQueryTests(_EngineCase):
    def test_advice_names_the_real_table_not_the_alias(self) -> None:
        # SQLite's plan reports the alias ("H"); advice keyed on it claimed
        # "no index" for a table with one. The alias must resolve.
        out = self.engine.explain_query(H_JOIN)
        text = " ".join(out["advice"])
        self.assertIn("PS_JRNL_HEADER", text)
        self.assertNotIn(" of H ", text)
        self.assertIn("IX_JRNL_HDR", text)
        self.assertIn("BUSINESS_UNIT", text,
                      "the advice must say what the index LEADS with")

    def test_an_indexed_query_is_told_to_run(self) -> None:
        out = self.engine.explain_query(
            "SELECT * FROM PS_LEDGER "
            "WHERE BUSINESS_UNIT='US001' AND LEDGER='ACTUALS'")
        self.assertIn("run it", " ".join(out["advice"]).lower())

    def test_it_never_executes(self) -> None:
        calls = []
        original = self.db.query

        def spy(sql, params=None, max_rows=None):
            calls.append(" ".join(str(sql).split())[:60])
            return original(sql, params, max_rows=max_rows)

        self.db.query = spy
        try:
            self.engine.explain_query(H_JOIN)
        finally:
            self.db.query = original
        self.assertFalse(
            [c for c in calls if c.upper().startswith("SELECT J.JOURNAL_ID")],
            "explain_query ran the statement it was asked to explain")

    def test_dml_is_refused(self) -> None:
        with self.assertRaises(EngineError):
            self.engine.explain_query("DELETE FROM PS_LEDGER")


class _GatedCase(_EngineCase):
    """Fabricate optimizer plans so the journal-probe gate is exercisable:
    the sample's tables are indexed, so real plans never trip it — which is
    itself the correct behaviour, asserted in the first test below."""

    def fake_plans(self, full_range_scans: bool, narrowed_scans: bool) -> None:
        orig_plan = self.db.explain_plan
        orig_rows = self.engine._approx_rows
        self.engine._approx_rows = (
            lambda t: 40_000_000 if t in ("PS_JRNL_HEADER", "PS_JRNL_LN")
            else orig_rows(t))

        def plan(sql, params=None):
            if "PS_JRNL" in sql and isinstance(params, dict) \
                    and "minper" in params:
                first = params["minper"] in (0, 1)
                scans = full_range_scans if first else narrowed_scans
                if scans:
                    return {"available": True, "steps": [
                        {"operation": "SCAN", "options": "FULL",
                         "object": "H", "rows": 40_000_000, "cost": 999999}]}
                return {"available": True, "steps": [
                    {"operation": "SEARCH", "options": "", "object": "H"}]}
            return orig_plan(sql, params)

        self.db.explain_plan = plan


class JournalProbeGateTests(_GatedCase):
    def test_indexed_plans_run_the_full_range_unchanged(self) -> None:
        ic = self.engine.tb_integrity_check()
        self.assertEqual(ic["checks_narrowed"], [])
        self.assertEqual(ic["checks_incomplete"], [])
        self.assertTrue(ic["unposted_journals"],
                        "the sample's unposted journal must still be found")

    def test_a_costly_full_range_narrows_to_the_current_period(self) -> None:
        self.fake_plans(full_range_scans=True, narrowed_scans=False)
        ic = self.engine.tb_integrity_check()
        narrowed = {n["check"] for n in ic["checks_narrowed"]}
        self.assertIn("out_of_balance_journals", narrowed)
        self.assertEqual(ic["checks_incomplete"], [])
        entry = next(n for n in ic["checks_narrowed"]
                     if n["check"] == "out_of_balance_journals")
        self.assertIn("period", entry["scope"])
        self.assertIn("IX_JRNL_HDR", entry["reason"],
                      "the narrowing reason must name the index that would "
                      "restore the full check")

    def test_when_even_narrowed_scans_it_refuses_fast_with_advice(self) -> None:
        self.fake_plans(full_range_scans=True, narrowed_scans=True)
        ic = self.engine.tb_integrity_check()
        names = {c["check"] for c in ic["checks_incomplete"]}
        self.assertIn("out_of_balance_journals", names)
        reason = next(c["reason"] for c in ic["checks_incomplete"]
                      if c["check"] == "out_of_balance_journals")
        self.assertIn("IX_JRNL_HDR", reason)
        # The refusal costs milliseconds, and the balance is unaffected.
        self.assertTrue(ic["balanced"])
        self.assertEqual(ic["control_status"], "checks_incomplete")

    def test_the_playbook_discloses_a_narrowed_check(self) -> None:
        self.fake_plans(full_range_scans=True, narrowed_scans=False)
        result = PlaybookRunner(self.engine).run("close_readiness")
        step = next(s for s in result["steps"]
                    if s["step"] == "journal_balance")
        self.assertIn("period", step["headline"])
        self.assertIn("IX_JRNL_HDR", step["headline"],
                      "a narrowed check must not impersonate the full one")

    def test_the_playbook_skips_not_passes_a_refused_check(self) -> None:
        self.fake_plans(full_range_scans=True, narrowed_scans=True)
        result = PlaybookRunner(self.engine).run("close_readiness")
        step = next(s for s in result["steps"]
                    if s["step"] == "journal_balance")
        self.assertEqual(step["status"], "skipped")
        self.assertEqual(result["verdict"], "incomplete")


class CostGateHintTests(_EngineCase):
    def test_the_refusal_names_the_available_indexes(self) -> None:
        orig = self.engine._approx_rows
        self.engine._approx_rows = (
            lambda t: 40_000_000 if t == "PS_LEDGER" else orig(t))
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql("SELECT * FROM PS_LEDGER")
        msg = str(ctx.exception)
        self.assertIn("IX_LEDGER", msg,
                      "the refusal is the moment the model needs the index "
                      "catalog — it must be in the message")
        self.assertIn("explain_query", msg)


if __name__ == "__main__":
    unittest.main()


class WhereDetectionTests(_EngineCase):
    """The cost gate must recognize a filter wherever WHERE sits.

    Models write multi-line SQL, so WHERE follows a NEWLINE, not a space.
    The space-delimited test read a four-predicate query as unfiltered and
    refused it with "no filter at all" — a message blaming the SQL for a
    filter it plainly had. Found live on the real instance (2.8M-row
    PS_BI_HDR, screenshot 2026-08-06).
    """

    def _huge(self):
        orig = self.engine._approx_rows
        self.engine._approx_rows = (
            lambda t: 2_800_000 if t == "PS_BI_HDR" else orig(t))

    def test_a_newline_before_where_still_counts_as_filtered(self) -> None:
        self._huge()
        out = self.engine.run_sql(
            "SELECT BUSINESS_UNIT, COUNT(*) AS n\n"
            "FROM PS_BI_HDR\n"
            "WHERE BILL_STATUS = 'INV' AND INVOICE_AMOUNT > 0\n"
            "GROUP BY BUSINESS_UNIT")
        self.assertTrue(out["rows"] is not None)
        self.assertIn("It ran", (out.get("plan") or {}).get("warning", ""))

    def test_where_inside_an_identifier_is_not_a_filter(self) -> None:
        # An alias like SOMEWHERE must not read as a WHERE clause — this
        # query is genuinely unfiltered and must still refuse.
        self._huge()
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql(
                "SELECT BUSINESS_UNIT AS SOMEWHERE FROM PS_BI_HDR "
                "GROUP BY BUSINESS_UNIT")
        self.assertIn("no filter at all", str(ctx.exception))

    def test_unfiltered_still_refuses(self) -> None:
        self._huge()
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql(
                "SELECT BUSINESS_UNIT, COUNT(*) FROM PS_BI_HDR "
                "GROUP BY BUSINESS_UNIT")
        self.assertIn("no filter at all", str(ctx.exception))
