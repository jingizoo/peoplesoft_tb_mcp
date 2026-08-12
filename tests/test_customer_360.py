"""One customer, everything connected — and the lines it must not cross.

The value of this tool is that four states of the same money sit next to
each other: owed, disputed, billed-but-not-finalized, and paid-but-never-
applied. Split across four screens each one looks ordinary. So the tests
are mostly about the JOINS being real and the totals being honest:

  1. A corporate family is what the system recorded, not what the names
     look like. ACME Logistics Group is a different company from ACME
     Industrial, and any similarity match folds them together into a
     parent total that looks exactly as authoritative as a correct one.
  2. Totals are aggregated by the database; row lists are capped. A capped
     list must never quietly become a total.
  3. Currencies are never added together.
  4. A section that cannot be read costs that section, not the answer.
  5. A customer that does not exist is NO DATA, not a zero balance.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling, ARError  # noqa: E402
from pstb.config import load_config  # noqa: E402
from pstb.db import Database, DbError  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.guards import unit_access_block  # noqa: E402
from pstb.relationships import Relationships  # noqa: E402
from pstb.security import Access  # noqa: E402

BU, ASOF = "US001", "2026-06-30"


def _rel(db_cls=Database):
    cfg = load_config(str(ROOT / "config.yaml"))
    return Relationships(ARBilling(TBEngine(db_cls(cfg), cfg)))


class FamilyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rel = _rel()
        cls.out = cls.rel.customer_financial_360(
            cust_id="C1001", business_unit=BU, as_of_date=ASOF)

    def test_the_family_is_the_recorded_hierarchy(self) -> None:
        ids = {m["cust_id"] for m in self.out["family"]["members"]}
        self.assertEqual(ids, {"C1001", "C1009", "C1010"})
        self.assertIn("CORPORATE_CUST_ID", self.out["family"]["basis"])

    def test_a_same_named_company_is_NOT_family(self) -> None:
        # C1011 is "ACME Logistics Group" and is its own corporate parent.
        # Folding it in would invent a consolidation nobody approved, and
        # the parent total would look exactly as authoritative as a right
        # one.
        ids = {m["cust_id"] for m in self.out["family"]["members"]}
        self.assertNotIn("C1011", ids)

    def test_the_parent_total_is_its_members_added_up(self) -> None:
        total = next(t for t in self.out["receivables"]["totals_by_currency"]
                     if t["currency"] == "USD")
        parts = sum(r["open_balance"]
                    for m in self.out["family"]["members"]
                    for r in m["receivables"] if r["currency"] == "USD")
        self.assertAlmostEqual(total["open_balance"], parts, places=2)

    def test_the_subsidiary_driving_the_overdue_is_named(self) -> None:
        # "Which children contribute to the parent's overdue balance" is
        # the question; a single rolled-up number does not answer it.
        overdue = next(a for a in self.out["needs_attention"]
                       if a["kind"] == "overdue_receivable")
        who = {c["cust_id"]: c["overdue"] for c in overdue["contributors"]}
        self.assertEqual(who.get("C1010"), 19_900.00)
        self.assertAlmostEqual(sum(who.values()), overdue["amount"], places=2)

    def test_asking_for_one_company_gets_one_company(self) -> None:
        out = self.rel.customer_financial_360(
            cust_id="C1001", business_unit=BU, as_of_date=ASOF,
            include_family=False)
        self.assertEqual(out["family"]["member_count"], 1)
        total = out["receivables"]["totals_by_currency"][0]["open_balance"]
        self.assertLess(total,
                        self.out["receivables"]["totals_by_currency"][0][
                            "open_balance"])

    def test_a_subsidiary_asked_about_directly_sees_its_parent(self) -> None:
        out = self.rel.customer_financial_360(
            cust_id="C1010", business_unit=BU, as_of_date=ASOF)
        self.assertEqual(out["customer"]["corporate_parent"], "C1001")
        self.assertIn("subsidiary", out["family"]["members"][0]["role"])

    def test_a_site_with_no_hierarchy_says_so_instead_of_guessing(self):
        class NoCorporate(Database):
            def columns(self, table):
                cols = super().columns(table)
                return ({c for c in cols if c != "CORPORATE_CUST_ID"}
                        if table == "PS_CUSTOMER" else cols)

        out = _rel(NoCorporate).customer_financial_360(
            cust_id="C1001", business_unit=BU, as_of_date=ASOF)
        self.assertEqual(out["family"]["member_count"], 1)
        self.assertTrue(any("CORPORATE_CUST_ID" in n
                            for n in out["record_notes"]),
                        out["record_notes"])
        # Still a real answer for the one customer.
        self.assertTrue(out["receivables"]["totals_by_currency"])


class MoneyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rel = _rel()

    def _for(self, cid, **kw):
        return self.rel.customer_financial_360(
            cust_id=cid, business_unit=BU, as_of_date=ASOF, **kw)

    def test_cash_received_but_never_applied_is_surfaced(self) -> None:
        # The state that hides: the company holds the money and the
        # customer still shows as owing it. Neither the AR balance nor the
        # payment row says so on its own.
        out = self._for("C1004")
        hit = next(a for a in out["needs_attention"]
                   if a["kind"] == "cash_received_not_applied")
        self.assertEqual(hit["amount"], 10_000.00)
        self.assertEqual(hit["deposits"][0]["deposit_id"], "DEP-26063")

    def test_applied_cash_shows_as_paid_down_not_as_a_lower_bill(self):
        item = next(i for i in self._for("C1001")[
            "receivables"]["largest_open_items"] if i["item"] == "INV-260602")
        self.assertEqual(item["original_amount"], 61_250.00)
        self.assertEqual(item["balance"], 36_250.00)
        self.assertEqual(item["paid_down"], 25_000.00)

    def test_the_credit_rebill_chain_names_what_was_given_up(self) -> None:
        chain = self._for("C1001")["adjustments"]["chains"][0]
        self.assertEqual(chain["original_invoice"], "INV-260301")
        self.assertEqual(chain["original_amount"], 52_000.00)
        self.assertEqual(chain["net_billed"], 38_500.00)
        self.assertEqual(chain["given_up"], 13_500.00)
        self.assertEqual({c["adjustment_type"] for c in chain["corrections"]},
                         {"CR", "RB"})

    def test_a_dispute_is_reported_as_uncollectable_until_settled(self):
        out = self._for("C1004")
        hit = next(a for a in out["needs_attention"] if a["kind"] == "disputed")
        self.assertEqual(hit["amount"], 42_000.00)

    def test_currencies_are_reported_apart_never_added(self) -> None:
        out = self._for("C1006")
        curs = [t["currency"] for t in
                out["receivables"]["totals_by_currency"]]
        self.assertIn("EUR", curs)
        self.assertIn("USD", curs)
        self.assertTrue(any("not added together" in n
                            for n in out["record_notes"]), out["record_notes"])

    def test_totals_stay_exact_when_the_item_list_truncates(self) -> None:
        from pstb import relationships as mod
        saved = mod.ITEM_DETAIL_CAP
        mod.ITEM_DETAIL_CAP = 1
        try:
            out = self._for("C1001")
        finally:
            mod.ITEM_DETAIL_CAP = saved
        self.assertEqual(len(out["receivables"]["largest_open_items"]), 1)
        self.assertEqual(
            out["receivables"]["totals_by_currency"][0]["open_items"], 5)
        self.assertTrue(any("are complete" in n for n in out["record_notes"]),
                        out["record_notes"])

    def test_billing_still_in_flight_is_separated_from_finalized(self):
        out = self.rel.customer_financial_360(
            cust_id="C1004", business_unit=BU, as_of_date="2026-08-12")
        pending = out["billing"]["not_yet_finalized"]
        self.assertEqual([b["invoice"] for b in pending], ["INV-260703"])
        self.assertTrue(all(not s["finalized"]
                            or s["status"] == "INV"
                            for s in out["billing"]["by_status"]))
        self.assertIn("current state", out["billing"]["basis"])


class GraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.out = _rel().customer_financial_360(
            cust_id="C1001", business_unit=BU, as_of_date=ASOF)
        cls.g = cls.out["relationships"]

    def test_every_edge_has_two_real_endpoints(self) -> None:
        ids = {n["id"] for n in self.g["nodes"]}
        for e in self.g["edges"]:
            self.assertIn(e["from"], ids, e)
            self.assertIn(e["to"], ids, e)

    def test_the_hierarchy_edge_cites_the_column_it_came_from(self) -> None:
        sub = [e for e in self.g["edges"] if e["type"] == "SUBSIDIARY_OF"]
        self.assertEqual({e["from"] for e in sub},
                         {"CUST:C1009", "CUST:C1010"})
        self.assertTrue(all("CORPORATE_CUST_ID" in e["basis"] for e in sub))

    def test_a_payment_links_to_the_item_it_paid(self) -> None:
        applied = [e for e in self.g["edges"] if e["type"] == "APPLIED_TO"]
        self.assertEqual(applied[0]["to"], "ITEM:INV-260602")
        self.assertEqual(applied[0]["amount"], 25_000.00)

    def test_the_credit_points_at_the_invoice_it_corrects(self) -> None:
        corrects = [e for e in self.g["edges"] if e["type"] == "CORRECTS"]
        self.assertEqual({e["to"] for e in corrects}, {"BILL:INV-260301"})

    def test_nothing_is_linked_by_resemblance(self) -> None:
        self.assertIn("inferred from names", self.g["basis"].lower())
        self.assertNotIn("CUST:C1011", {n["id"] for n in self.g["nodes"]})


class DegradeTests(unittest.TestCase):
    """A record this site does not grant must cost its own section."""

    def test_no_payment_record_still_answers_the_rest(self) -> None:
        class NoPayments(Database):
            def columns(self, table):
                return (set() if table == "PS_PAYMENT"
                        else super().columns(table))

        out = _rel(NoPayments).customer_financial_360(
            cust_id="C1001", business_unit=BU, as_of_date=ASOF)
        self.assertFalse(out["cash"]["supported"])
        self.assertTrue(any("PS_PAYMENT" in n for n in out["record_notes"]))
        self.assertTrue(out["receivables"]["totals_by_currency"])
        self.assertTrue(out["adjustments"]["chains"])

    def test_no_rebill_column_says_so_and_keeps_the_credits(self) -> None:
        class NoRebill(Database):
            def columns(self, table):
                cols = super().columns(table)
                return ({c for c in cols if c != "CR_REBILL_INV"}
                        if table == "PS_BI_HDR" else cols)

        out = _rel(NoRebill).customer_financial_360(
            cust_id="C1001", business_unit=BU, as_of_date=ASOF)
        self.assertFalse(out["adjustments"]["supported"])
        self.assertTrue(any("CR_REBILL_INV" in n for n in out["record_notes"]))
        self.assertTrue(out["receivables"]["totals_by_currency"])

    def test_a_branch_that_raises_does_not_take_the_answer_down(self):
        class ItemsExplode(Database):
            def query(self, sql, params=None, **kw):
                if "PS_BI_HDR" in sql:
                    raise DbError("ORA-00942: table or view does not exist")
                return super().query(sql, params, **kw)

        out = _rel(ItemsExplode).customer_financial_360(
            cust_id="C1001", business_unit=BU, as_of_date=ASOF)
        self.assertEqual(out["billing"]["by_status"], [])
        self.assertTrue(any("could not be read" in n
                            for n in out["record_notes"]))
        self.assertTrue(out["receivables"]["totals_by_currency"])


class RefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rel = _rel()

    def test_an_unknown_customer_is_NO_DATA_not_a_zero_balance(self) -> None:
        out = self.rel.customer_financial_360(cust_id="C9999",
                                              business_unit=BU)
        self.assertEqual(out["scope_status"], "customer_not_found")
        self.assertIn("NO DATA", out["detail"])
        self.assertIn("C1001", out["known_customer_ids"])

    def test_no_customer_at_all_is_refused_with_the_way_forward(self):
        with self.assertRaises(ARError) as ctx:
            self.rel.customer_financial_360(business_unit=BU)
        self.assertIn("search_customers", str(ctx.exception))

    def test_the_tool_is_gated_by_business_unit(self) -> None:
        restricted = Access(oprid="FIN_US001", units=frozenset({"US001"}))
        self.assertEqual(unit_access_block(
            "get_customer_financial_360", {"business_unit": "US001"},
            restricted), "")
        why = unit_access_block("get_customer_financial_360",
                                {"business_unit": "CA001"}, restricted)
        self.assertIn("CA001", why)


class WebTests(unittest.TestCase):
    """The dashboard endpoint, gated the same way every other one is."""

    LOOP = {"base_url": "http://127.0.0.1:8000", "client": ("127.0.0.1", 1)}

    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient
        from pstb.gui import app as gapp
        cls.TestClient, cls.gapp = TestClient, gapp

    def test_it_answers_for_the_selected_unit(self) -> None:
        client = self.TestClient(self.gapp.app, **self.LOOP)
        body = client.get("/api/customer-360?cust_id=C1001"
                          "&business_unit=US001&as_of_date=2026-06-30").json()
        self.assertEqual(body["family"]["member_count"], 3)
        self.assertTrue(body["needs_attention"])

    def test_a_foreign_unit_is_refused_not_answered(self) -> None:
        saved = (self.gapp.cfg.security.enabled,
                 list(self.gapp.cfg.security.privileged_users))
        self.gapp.cfg.security.enabled = True
        self.gapp.cfg.security.privileged_users = ["ADMIN"]
        self.gapp.row_security.invalidate()
        try:
            client = self.TestClient(self.gapp.app, **self.LOOP)
            self.assertEqual(
                client.post("/api/signin",
                            json={"oprid": "FIN_US001"}).status_code, 200)
            self.assertEqual(client.get(
                "/api/customer-360?cust_id=C1001&business_unit=CA001"
            ).status_code, 403)
        finally:
            (self.gapp.cfg.security.enabled,
             self.gapp.cfg.security.privileged_users) = saved
            self.gapp.row_security.invalidate()


if __name__ == "__main__":
    unittest.main()
