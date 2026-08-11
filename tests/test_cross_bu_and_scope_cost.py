"""Two complaints from the box, and what was actually behind them.

"Listing business units is slow." The question "which ledgers does this
unit have" was being asked of the BALANCE table, where the answer costs a
range scan of every row for that unit to return three strings. Setup
tables hold the same answer in tens of rows.

"If my question spans more than one BU it should fire SQL." It should, and
it did not: the crossing was detected only when the user SAID it ("across
all business units"), never when they NAMED the units ("compare US001 and
CA001"). So the scope lock pinned the chip's unit, the model called a
single-unit tool with it, and the answer covered one of the two units the
person asked about while looking exactly as confident as a correct one.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb import queries as q  # noqa: E402
from pstb.config import load_config  # noqa: E402
from pstb.db import Database, DbError  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.guards import (spans_business_units,  # noqa: E402
                         units_named_in, wants_all_business_units)

KNOWN = ["US001", "CA001", "US200"]


class CrossUnitDetectionTests(unittest.TestCase):
    def test_naming_two_units_is_a_crossing(self) -> None:
        for question in ("compare cash between US001 and CA001",
                         "revenue for US001 and US200 side by side",
                         "us001 vs ca001 payables"):
            self.assertTrue(spans_business_units(question, KNOWN, "US001"),
                            question)

    def test_naming_a_unit_other_than_the_selected_one_is_a_crossing(self):
        # The chip says US001 and the question says CA001: the words are
        # the newer instruction, and pinning the chip answers the wrong
        # company.
        self.assertTrue(spans_business_units("show me CA001 revenue",
                                             KNOWN, "US001"))

    def test_the_selected_unit_alone_is_not_a_crossing(self) -> None:
        for question in ("what is revenue in US001", "total revenue",
                         "is the trial balance balanced"):
            self.assertFalse(spans_business_units(question, KNOWN, "US001"),
                             question)

    def test_the_spoken_form_still_works(self) -> None:
        self.assertTrue(wants_all_business_units("across all business units"))
        self.assertTrue(spans_business_units("across all business units",
                                             KNOWN, "US001"))

    def test_unit_codes_match_on_boundaries_only(self) -> None:
        # US001 must not match inside US0012, and a code must not be found
        # inside an ordinary word — otherwise every account number in the
        # sentence unlocks the scope.
        self.assertEqual(units_named_in("balance for US0012", KNOWN), [])
        self.assertEqual(units_named_in("US001 only", KNOWN), ["US001"])

    def test_no_catalog_means_no_false_crossing(self) -> None:
        # Before the catalog loads there is nothing to match against; the
        # scope lock must stay on rather than fall open.
        self.assertFalse(spans_business_units("compare US001 and CA001",
                                              [], "US001"))


class LedgerLookupCostTests(unittest.TestCase):
    """The shape of the query matters more than the number of them."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_config(str(ROOT / "config.yaml"))
        cls.db = Database(cls.cfg)
        cls.engine = TBEngine(cls.db, cls.cfg)

    def test_ledgers_come_from_setup_not_the_balance_table(self) -> None:
        sql = q.ledgers_for_bu_setup(self.db)
        self.assertIn("PS_BUS_UNIT_LED", sql)
        self.assertNotIn("PS_LEDGER ", sql,
                         "asking the balance table which ledgers exist reads "
                         "every row for the unit to return three strings")

    def test_the_setup_path_is_the_one_actually_taken(self) -> None:
        seen: list = []
        original = self.db.query

        def spy(sql, params=None, **kw):
            seen.append(sql)
            return original(sql, params, **kw)

        self.engine._ledgers_cache.clear()
        self.db.query = spy
        try:
            out = self.engine.list_ledgers("US001")
        finally:
            self.db.query = original
        self.assertIn("ACTUALS", out["ledgers"])
        self.assertTrue(any("PS_BUS_UNIT_LED" in s for s in seen), seen)
        self.assertFalse(
            any("DISTINCT LEDGER" in s.upper() for s in seen),
            "the balance-table fallback ran even though setup answered")

    def test_it_still_answers_when_setup_is_not_granted(self) -> None:
        # A read-only account without PS_BUS_UNIT_LED must degrade to the
        # slow-but-correct form, not fail.
        class NoSetup(Database):
            def query(self, sql, params=None, **kw):
                if "PS_BUS_UNIT_LED" in sql:
                    raise DbError("ORA-00942: table or view does not exist")
                return super().query(sql, params, **kw)

        engine = TBEngine(NoSetup(self.cfg), self.cfg)
        self.assertIn("ACTUALS", engine.list_ledgers("US001")["ledgers"])

    def test_catalog_probes_short_circuit_per_pair(self) -> None:
        # EXISTS stops at the first row; DISTINCT cannot, so proving "these
        # ten units have data" meant reading every ledger row belonging to
        # all ten.
        seen: list = []
        original = self.db.query

        def spy(sql, params=None, **kw):
            seen.append(sql)
            return original(sql, params, **kw)

        self.db.query = spy
        try:
            self.engine._with_ledger_data([("US001", "ACTUALS"),
                                           ("US001", "BUDGET")])
        finally:
            self.db.query = original
        probe = [s for s in seen if "PS_LEDGER" in s]
        self.assertTrue(probe, seen)
        self.assertIn("EXISTS", probe[0].upper())
        self.assertNotIn("SELECT DISTINCT BUSINESS_UNIT", probe[0].upper())

    def test_the_probe_still_drops_pairs_with_no_rows(self) -> None:
        kept = self.engine._with_ledger_data(
            [("US001", "ACTUALS"), ("NOPE", "GHOST")])
        self.assertIn(("US001", "ACTUALS"), kept)
        self.assertNotIn(("NOPE", "GHOST"), kept)


if __name__ == "__main__":
    unittest.main()
