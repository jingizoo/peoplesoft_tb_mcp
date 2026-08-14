"""A cross-unit answer must not cross the units a person was never granted.

Every other row-security gate in this app checks a business_unit ARGUMENT,
and for a tool that answers about one unit that is enough. It is not enough
for a tool that answers about all of them: business_unit="ALL" is not a unit
anybody was denied, so the gate waved it through as harmless. It is not
harmless — it means every unit that EXISTS.

The leak this file was written for, reproduced below: a user granted US001
called get_top_billing_customers(business_unit="ALL") and received EU001's
customers and amounts, 200 OK, no warning. The guard said allow; the tool
never asked who was calling.

Two paths, two enforcement points, because they are genuinely different:

  IN PROCESS (the GUI's own /api endpoints call ARBilling directly)
      The caller is bound to a context variable by the request middleware
      and the tool narrows its own SQL to the grant, then SAYS it narrowed.
      A top-10 that silently dropped half the company reads exactly like a
      top-10 of the company, so the disclosure is not decoration.

  OVER MCP (the chat path talks to a server in a SEPARATE PROCESS)
      That process cannot know who is asking, and the grant cannot travel
      as a tool argument — tool arguments are written by the MODEL, so an
      "allowed_units" parameter is a grant the model can widen by typing a
      different value. So the client-side gate refuses ALL and names the
      units that would work.

Both are pinned here, and so is the thing that must NOT change: a
deployment with row security off, or a privileged user, sees exactly what
it saw before.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling, ARError  # noqa: E402
from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.guards import unit_access_block  # noqa: E402
from pstb.security import (Access, access_scope, allowed_units,  # noqa: E402
                           current_access)

MINE = Access(oprid="FIN_US001", units=frozenset({"US001"}))
BOTH = Access(oprid="FIN_BOTH", units=frozenset({"US001", "EU001"}))
ADMIN = Access(oprid="ADMIN", all_units=True)


class _TwoUnits(unittest.TestCase):
    """US001 (the sample) plus EU001, whose billing must not leak."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="pstb-xunit-"))
        dst = cls.tmp / "s.db"
        shutil.copy(ROOT / "sample_data" / "ps_sample.db", dst)
        con = sqlite3.connect(dst)
        con.execute("INSERT INTO PS_BUS_UNIT_TBL_GL VALUES ('EU001','EUR')")
        con.execute("INSERT INTO PS_BUS_UNIT_TBL_FS VALUES "
                    "('EU001','European Ops','DEU')")
        cols = [r[1] for r in con.execute("PRAGMA table_info(PS_BI_HDR)")]
        row = {"BUSINESS_UNIT": "EU001", "INVOICE": "EU-1",
               "BILL_TO_CUST_ID": "C1002", "BILL_STATUS": "INV",
               "INVOICE_AMOUNT": 999999.0, "INVOICE_DT": "2026-07-01",
               "BI_CURRENCY_CD": "USD"}
        con.execute(f"INSERT INTO PS_BI_HDR VALUES "
                    f"({','.join('?' * len(cols))})",
                    [row.get(c) for c in cols])
        con.commit()
        con.close()
        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(dst)
        cfg.db.use_views = False
        cls.db = Database(cfg)
        cls.engine = TBEngine(cls.db, cfg)
        cls.ar = ARBilling(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def rank(self, **kw) -> dict:
        return self.ar.top_billing_customers(
            business_unit="ALL", display_currency="USD", n=20,
            as_of_date="2026-08-14", **kw)

    @staticmethod
    def units_in(out) -> list:
        return sorted({u for c in out.get("customers", [])
                       for u in (c.get("business_units")
                                 or [c.get("business_unit")]) if u})


class InProcessTests(_TwoUnits):
    def test_the_leak_is_closed(self) -> None:
        with access_scope(MINE):
            out = self.rank()
        self.assertEqual(self.units_in(out), ["US001"],
                         "a US001-only user must not receive EU001 billing")

    def test_the_amount_does_not_survive_either(self) -> None:
        # The unit label being filtered is not enough — the FIGURE is the
        # thing that leaks. EU001's invoice is a deliberate 999,999.
        with access_scope(MINE):
            out = self.rank()
        self.assertNotIn("999999", str(out).replace(",", ""))

    def test_a_narrowed_ranking_says_it_was_narrowed(self) -> None:
        with access_scope(MINE):
            out = self.rank()
        self.assertTrue(out["restricted_to_granted_units"])
        self.assertEqual(out["units_ranked"], ["US001"])
        self.assertIn("NOT every unit", out["scope"])
        self.assertIn("would not appear", out["note"],
                      "the reader has to know a bigger customer may exist "
                      "outside what they can see")

    def test_a_multi_unit_grant_still_ranks_across_its_units(self) -> None:
        with access_scope(BOTH):
            out = self.rank()
        self.assertEqual(self.units_in(out), ["EU001", "US001"])
        self.assertEqual(out["units_ranked"], ["EU001", "US001"])

    def test_a_grant_of_nothing_refuses_rather_than_ranking_nothing(self):
        with access_scope(Access(oprid="NOBODY", units=frozenset())):
            with self.assertRaises(ARError) as ctx:
                self.rank()
        self.assertIn("granted no business units", str(ctx.exception))

    def test_business_units_are_listed_only_where_granted(self) -> None:
        with access_scope(MINE):
            names = [b["business_unit"]
                     for b in self.engine.list_business_units()
                     ["business_units"]]
        self.assertNotIn("EU001", names)


class UnrestrictedIsUnchangedTests(_TwoUnits):
    """The common deployment today has security off. Nothing may change."""

    def test_no_caller_bound_sees_everything(self) -> None:
        out = self.rank()
        self.assertEqual(self.units_in(out), ["EU001", "US001"])
        self.assertNotIn("restricted_to_granted_units", out)

    def test_a_privileged_caller_sees_everything(self) -> None:
        with access_scope(ADMIN):
            out = self.rank()
        self.assertEqual(self.units_in(out), ["EU001", "US001"])
        self.assertNotIn("restricted_to_granted_units", out)

    def test_a_single_unit_call_is_untouched(self) -> None:
        with access_scope(MINE):
            out = self.ar.top_billing_customers(
                business_unit="US001", display_currency="USD",
                as_of_date="2026-08-14")
        self.assertNotIn("restricted_to_granted_units", out)


class McpPathTests(unittest.TestCase):
    """The chat path cannot filter, so it refuses — and says what works."""

    def test_all_is_refused_for_a_restricted_caller(self) -> None:
        why = unit_access_block("get_top_billing_customers",
                                {"business_unit": "ALL"}, MINE)
        self.assertTrue(why)
        self.assertIn("US001", why, "the refusal must name what WOULD work")
        self.assertIn("refused rather than quietly narrowed", why)

    def test_a_star_is_the_same_request_by_another_spelling(self) -> None:
        self.assertTrue(unit_access_block("get_top_billing_customers",
                                          {"business_unit": "*"}, MINE))

    def test_a_privileged_caller_is_not_refused(self) -> None:
        self.assertEqual(unit_access_block("get_top_billing_customers",
                                           {"business_unit": "ALL"}, ADMIN),
                         "")

    def test_no_row_security_is_not_refused(self) -> None:
        self.assertEqual(unit_access_block("get_top_billing_customers",
                                           {"business_unit": "ALL"}, None),
                         "")

    def test_a_granted_unit_still_passes(self) -> None:
        self.assertEqual(unit_access_block("get_ar_aging",
                                           {"business_unit": "US001"}, MINE),
                         "")


class ContextTests(unittest.TestCase):
    """The seam itself: unforgeable, and honest about 'no security'."""

    def test_no_caller_reads_as_unrestricted_not_as_no_units(self) -> None:
        # A terminal session, a script, security disabled. Reading this as
        # "no units" would make every tool answer nothing on the machine of
        # the person who installed it.
        self.assertIsNone(current_access())
        self.assertEqual(allowed_units(["US001", "EU001"]),
                         (["US001", "EU001"], []))

    def test_the_scope_is_restored_after_the_request(self) -> None:
        with access_scope(MINE):
            self.assertIs(current_access(), MINE)
        self.assertIsNone(current_access())

    def test_it_is_restored_even_when_the_request_raises(self) -> None:
        with self.assertRaises(ValueError):
            with access_scope(MINE):
                raise ValueError("boom")
        self.assertIsNone(current_access())

    def test_narrow_reports_what_it_dropped(self) -> None:
        self.assertEqual(MINE.narrow(["US001", "EU001", "ca001"]),
                         (["US001"], ["EU001", "CA001"]))
        self.assertEqual(ADMIN.narrow(["US001", "EU001"]),
                         (["US001", "EU001"], []))


class NoForgeryTests(unittest.TestCase):
    """The grant must not be reachable from anything the model writes."""

    def test_the_tools_take_no_units_argument(self) -> None:
        # If a grant were a parameter, the model could widen it by typing a
        # different value — and the model is steered by the question, which
        # is written by whoever is asking.
        import inspect

        from pstb import server as srv
        for name in ("get_top_billing_customers", "get_ar_aging",
                     "list_business_units"):
            params = set(inspect.signature(getattr(srv, name)).parameters)
            for forgeable in ("access", "allowed_units", "units", "oprid"):
                self.assertNotIn(forgeable, params, f"{name}.{forgeable}")

    def test_the_gui_binds_the_caller_around_the_handler(self) -> None:
        source = (ROOT / "pstb" / "gui" / "app.py").read_text()
        self.assertIn("with access_scope(access):", source)


if __name__ == "__main__":
    unittest.main()
