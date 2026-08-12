"""Relationships have to arrive without being asked for.

A relationship layer that only fires when someone types the right question
is not a relationship layer, it is a tool nobody finds. The complaint that
produced these tests was exact: "if i ask a question are you using it for
insights? without even me telling go 360."

So the tests are about four places where the connection now shows up on
its own:

  1. get_ar_aging returns the corporate families. Three ACME rows at ranks
     1, 8 and 9 are three strangers on the screen; the fact that they are
     one company holding 39% of the receivable exists nowhere in the row
     list and cannot be read out of it.
  2. search_customers — the moment a NAME becomes an id — says whether
     that id is part of something bigger, and names the tool that rolls it
     up. A model that misses this answers "how much does ACME owe" for one
     legal entity out of three, and looks complete doing it.
  3. The system prompt routes a whole-customer question to the 360. It
     used to route it to get_customer_ar, which is the balance alone.
  4. Every tool result carries the next steps the machinery already
     computed from its own figures, instead of leaving them in the browser
     where only a human clicking a chip would ever see them.

And two things that must NOT happen: no family may be invented from names,
and none of this may cost a query.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling, _families  # noqa: E402
from pstb.client.chat import _observed_next_steps  # noqa: E402
from pstb.client.prompt import system_prompt  # noqa: E402
from pstb.config import load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.relationships import Relationships  # noqa: E402

BU = "US001"


def _ar(db_cls=Database):
    cfg = load_config(str(ROOT / "config.yaml"))
    return ARBilling(TBEngine(db_cls(cfg), cfg))


class AgingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ar = _ar()
        cls.out = cls.ar.aging(business_unit=BU)
        cls.fam = cls.out["corporate_families"]

    def test_the_aging_says_who_is_one_company(self) -> None:
        acme = next(f for f in self.fam["families"]
                    if f["corporate_parent"] == "C1001")
        self.assertEqual({m["cust_id"] for m in acme["members"]},
                         {"C1001", "C1009", "C1010"})
        self.assertEqual(acme["combined_total"], 357_485.19)
        self.assertEqual(acme["share_of_ar_pct"], 39.33)

    def test_the_group_is_bigger_than_any_row_in_the_list(self) -> None:
        # The point of the rollup: the biggest single customer row is not
        # the biggest exposure, and nothing else on the screen says so.
        acme = next(f for f in self.fam["families"]
                    if f["corporate_parent"] == "C1001")
        biggest_row = max(c["total"] for c in self.out["customers"])
        self.assertGreater(acme["combined_total"], biggest_row)

    def test_a_same_named_company_is_never_folded_in(self) -> None:
        # C1011 is "ACME Logistics Group" and is its own corporate parent.
        # A wrong combined exposure reads exactly as authoritative as a
        # right one, so this is the failure that must not be possible.
        members = {m["cust_id"] for f in self.fam["families"]
                   for m in f["members"]}
        self.assertNotIn("C1011", members)
        self.assertIn("never grouped by name", self.fam["basis"])

    def test_families_do_not_double_count_the_totals(self) -> None:
        rows = sum(c["total"] for c in self.out["customers"])
        self.assertAlmostEqual(rows, self.out["totals"]["total"], places=2)
        self.assertIn("do NOT add", self.fam["note"])

    def test_the_rollup_touches_no_database(self) -> None:
        # It is arithmetic over rows already in hand. If it ever needs a
        # query it stops being free and starts being a reason aging is
        # slow — the complaint this project has had twice.
        rows = [{"cust_id": "A", "name": "A", "total": 10.0,
                 "corporate_parent": "P", "disputed_amt": 0.0},
                {"cust_id": "P", "name": "P", "total": 90.0,
                 "corporate_parent": "P", "disputed_amt": 0.0}]
        out = _families(rows, 100.0)
        self.assertEqual(out[0]["combined_total"], 100.0)
        self.assertEqual(out[0]["share_of_ar_pct"], 100.0)
        self.assertNotIn("db", _families.__code__.co_names)

    def test_it_costs_no_extra_query(self) -> None:
        # The corporate parent rides in on the LEFT JOIN the summary
        # already performs.
        seen: list = []
        original = self.ar.db.query

        def spy(sql, params=None, **kw):
            seen.append(sql)
            return original(sql, params, **kw)

        self.ar.db.query = spy
        try:
            self.ar.aging(business_unit=BU)
        finally:
            self.ar.db.query = original
        self.assertLessEqual(len(seen), 3, seen)
        self.assertTrue(any("CORPORATE_CUST_ID" in s for s in seen))

    def test_a_family_of_one_is_just_a_customer(self) -> None:
        self.assertTrue(all(f["member_count"] >= 2
                            for f in self.fam["families"]))

    def test_a_site_with_no_hierarchy_gets_a_flat_list_and_a_note(self):
        class NoCorporate(Database):
            def columns(self, table):
                cols = super().columns(table)
                return ({c for c in cols if c != "CORPORATE_CUST_ID"}
                        if table == "PS_CUSTOMER" else cols)

        out = _ar(NoCorporate).aging(business_unit=BU)
        self.assertEqual(out["corporate_families"]["families"], [])
        self.assertTrue(any("CORPORATE_CUST_ID" in n
                            for n in out.get("record_notes") or []),
                        out.get("record_notes"))
        self.assertTrue(out["customers"])

    def test_one_customers_aging_does_not_pretend_to_roll_up(self) -> None:
        out = self.ar.aging(business_unit=BU, customer_id="C1001")
        self.assertEqual(out["corporate_families"]["families"], [])
        self.assertIn("Single-customer", out["corporate_families"]["note"])


class SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.out = _ar().search_customers(query="ACME", business_unit=BU)

    def test_resolving_a_name_reveals_the_group(self) -> None:
        self.assertEqual(set(self.out["belongs_to_a_corporate_family"]),
                         {"C1009", "C1010"})

    def test_it_names_the_tool_that_rolls_the_group_up(self) -> None:
        self.assertIn("get_customer_financial_360", self.out["next_step"])
        self.assertIn("legal entity ALONE", self.out["next_step"])

    def test_the_unrelated_company_is_not_flagged(self) -> None:
        self.assertNotIn("C1011",
                         self.out["belongs_to_a_corporate_family"])
        c1011 = next(c for c in self.out["customers"]
                     if c["cust_id"] == "C1011")
        self.assertEqual(c1011["corporate_parent"], "C1011")

    def test_a_lone_customer_gets_no_next_step(self) -> None:
        out = _ar().search_customers(query="Harborview", business_unit=BU)
        self.assertTrue(out["customers"])
        self.assertNotIn("next_step", out)


class PromptTests(unittest.TestCase):
    """The routing bug: the prompt sent whole-customer questions away."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = system_prompt(load_config(str(ROOT / "config.yaml")),
                                 provider="claude")

    def test_the_360_is_named_and_routed_to(self) -> None:
        self.assertIn("get_customer_financial_360", self.text)

    def test_the_wide_question_no_longer_lands_on_the_balance_tool(self):
        # It used to read "one customer's balance/items -> search_customers
        # then get_customer_ar" with nothing else, so "tell me about ACME"
        # got a balance and stopped.
        self.assertNotIn("one customer's balance/items -> search_customers\n",
                         self.text)
        wide = self.text[self.text.index("ONE named customer"):]
        self.assertIn("get_customer_financial_360", wide[:900])

    def test_it_forbids_grouping_companies_by_name(self) -> None:
        self.assertIn("CORPORATE_CUST_ID", self.text)
        self.assertIn("NEVER from names looking alike", self.text)

    def test_the_payload_keys_that_signal_a_family_are_named(self) -> None:
        # A prompt that describes the idea but not the key the model will
        # actually see is a prompt the model cannot act on.
        for key in ("corporate_parent", "belongs_to_a_corporate_family",
                    "corporate_families"):
            self.assertIn(key, self.text)


class NextStepTests(unittest.TestCase):
    """Findings the machinery already computed, made machine-visible."""

    @classmethod
    def setUpClass(cls) -> None:
        cfg = load_config(str(ROOT / "config.yaml"))
        engine = TBEngine(Database(cfg), cfg)
        cls.ar = ARBilling(engine)
        cls.rel = Relationships(cls.ar)

    def test_a_result_carries_its_own_next_steps(self) -> None:
        p = self.rel.customer_financial_360(cust_id="C1004",
                                            business_unit=BU,
                                            as_of_date="2026-08-12")
        steps = _observed_next_steps("get_customer_financial_360", p,
                                     "tell me about C1004", BU, set())
        self.assertTrue(steps)
        for s in steps:
            self.assertTrue(s["finding"] and s["ask"] and s["tool"])
        self.assertTrue(any("never applied" in s["finding"] for s in steps),
                        steps)
        self.assertTrue(any(s["tool"] == "get_customer_ar" for s in steps),
                        "the pointer that matters is where to apply the cash")

    def test_it_is_capped(self) -> None:
        p = self.rel.customer_financial_360(cust_id="C1004",
                                            business_unit=BU,
                                            as_of_date="2026-08-12")
        self.assertLessEqual(
            len(_observed_next_steps("get_customer_financial_360", p, "", BU,
                                     set())), 3)

    def test_the_same_pointer_is_not_repeated_across_tools(self) -> None:
        # Aging and the 360 both notice the same dispute. The model should
        # be told once.
        seen: set = set()
        first = _observed_next_steps(
            "get_ar_aging", self.ar.aging(business_unit=BU), "", BU, seen)
        second = _observed_next_steps(
            "get_ar_aging", self.ar.aging(business_unit=BU), "", BU, seen)
        self.assertTrue(first)
        self.assertEqual(second, [])

    def test_a_broken_payload_costs_the_pointer_and_nothing_else(self):
        self.assertEqual(_observed_next_steps("get_ar_aging", None, "", BU,
                                              set()), [])
        self.assertEqual(_observed_next_steps("get_ar_aging", {"x": object()},
                                              "", BU, set()), [])

    def test_a_clean_result_gets_no_pointers(self) -> None:
        self.assertEqual(_observed_next_steps(
            "get_open_payables", {"business_unit": BU, "overdue_total": 0},
            "", BU, set()), [])


if __name__ == "__main__":
    unittest.main()
