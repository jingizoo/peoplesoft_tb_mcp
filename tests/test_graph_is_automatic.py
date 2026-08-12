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


class HierarchyTests(unittest.TestCase):
    """The grouping itself, at the shapes real master data contains.

    Every case here was found by an adversarial pass over the first draft,
    and every one of them produced a wrong combined exposure that looked
    exactly as authoritative as a right one.
    """

    @staticmethod
    def _row(cid, parent, total, name=None, disputed=0.0):
        return {"cust_id": cid, "name": name or cid, "total": total,
                "corporate_parent": parent, "disputed_amt": disputed}

    def test_a_three_level_chain_is_ONE_family(self) -> None:
        # Grouping one hop deep put the middle company in two families and
        # reported the top group short by everything beneath it.
        out = _families([self._row("TOP", "TOP", 1000),
                         self._row("MID", "TOP", 500),
                         self._row("LEAF", "MID", 200)], 1700.0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["corporate_parent"], "TOP")
        self.assertEqual(out[0]["member_count"], 3)
        self.assertEqual(out[0]["combined_total"], 1700.0)

    def test_a_pointer_cycle_lands_everyone_in_one_family(self) -> None:
        # A owns B owns A exists in master data. Breaking the loop at a
        # different place for each of them is the same bug in a costume.
        out = _families([self._row("A", "B", 10), self._row("B", "A", 20)],
                        30.0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["member_count"], 2)

    def test_a_group_is_never_labelled_with_a_members_name(self) -> None:
        # A holding company with no open items is not "Globex - West".
        out = _families([self._row("C9", "C1", 60, "Globex - West"),
                         self._row("C8", "C1", 40, "Globex - East")], 100.0)
        self.assertIsNone(out[0]["name"])
        self.assertTrue(out[0]["parent_has_no_open_items"])

    def test_a_members_credit_cannot_inflate_the_concentration(self) -> None:
        # Dividing the largest member by the NETTED total published 805.7%
        # as a concentration measure — and the number guard certifies it,
        # because a rate a tool declared is a rate the guard trusts.
        out = _families([self._row("P", "P", 100_000),
                         self._row("S", "P", -95_000)], 5_000.0)
        self.assertEqual(out[0]["largest_member_share_pct"], 100.0)

    def test_an_all_credit_family_reports_no_concentration(self) -> None:
        out = _families([self._row("P", "P", -1_000),
                         self._row("S", "P", -4_000)], -5_000.0)
        self.assertIsNone(out[0]["largest_member_share_pct"])
        self.assertIsNone(out[0]["share_of_ar_pct"],
                          "a share of a negative total is not a share")

    def test_nothing_is_dropped_without_saying_so(self) -> None:
        from pstb.ar import FAMILY_CAP
        rows = []
        for i in range(FAMILY_CAP + 4):
            rows += [self._row(f"P{i:02d}", f"P{i:02d}", 1000 - i),
                     self._row(f"S{i:02d}", f"P{i:02d}", 10)]
        self.assertEqual(len(_families(rows, 100000.0)), FAMILY_CAP + 4,
                         "the helper returns everything; the CALLER caps")
        block = _ar()._family_block(_families(rows, 1e5)[:FAMILY_CAP],
                                    FAMILY_CAP + 4, True, False)
        self.assertTrue(block["truncated"])
        self.assertEqual(block["families_found"], FAMILY_CAP + 4)
        self.assertIn(f"of {FAMILY_CAP + 4}", block["note"])

    def test_unknown_is_not_reported_as_no(self) -> None:
        # The failure this whole feature exists to prevent, made by the
        # feature itself: telling a site that cannot know that its
        # customers are unrelated.
        cannot_know = _ar()._family_block([], 0, False, False)
        self.assertIn("UNKNOWN", cannot_know["note"])
        knows = _ar()._family_block([], 0, True, False)
        self.assertIn("No customer in this aging", knows["note"])


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

    def test_searching_the_PARENT_by_name_also_reveals_the_group(self):
        # The likelier half of the failure: the parent is what people type,
        # and its own row cannot say it owns anything — the children are
        # rows the WHERE clause excluded.
        out = _ar().search_customers(query="ACME Industrial",
                                     business_unit=BU)
        self.assertIn("C1001", out["heads_a_corporate_family"])
        head = next(c for c in out["customers"] if c["cust_id"] == "C1001")
        self.assertTrue(head["heads_a_corporate_family"])

    def test_a_site_without_the_column_pays_nothing_for_the_lookup(self):
        class NoCorporate(Database):
            def columns(self, table):
                cols = super().columns(table)
                return ({c for c in cols if c != "CORPORATE_CUST_ID"}
                        if table == "PS_CUSTOMER" else cols)

        ar = _ar(NoCorporate)
        ar.search_customers(query="ACME", business_unit=BU)   # warm
        seen: list = []
        original = ar.db.query

        def spy(sql, params=None, **kw):
            seen.append(sql)
            return original(sql, params, **kw)

        ar.db.query = spy
        try:
            out = ar.search_customers(query="ACME", business_unit=BU)
        finally:
            ar.db.query = original
        self.assertEqual(len(seen), 1, seen)
        self.assertNotIn("heads_a_corporate_family", out)

    def test_a_lone_customer_gets_no_next_step(self) -> None:
        out = _ar().search_customers(query="Harborview", business_unit=BU)
        self.assertTrue(out["customers"])
        self.assertNotIn("next_step", out)


class CustomerARTests(unittest.TestCase):
    """The tool the prompt routes "what does X owe" to, and where it stops."""

    def test_the_balance_says_whether_it_is_the_whole_company(self) -> None:
        out = _ar().customer(customer="C1001", business_unit=BU)
        fam = out["corporate_family"]
        self.assertEqual(fam["role"], "parent")
        self.assertIn("legal entity ALONE", fam["next_step"])
        self.assertIn("get_customer_financial_360", fam["next_step"])

    def test_a_subsidiary_is_told_who_owns_it(self) -> None:
        fam = _ar().customer(customer="C1009",
                             business_unit=BU)["corporate_family"]
        self.assertEqual(fam["role"], "subsidiary")
        self.assertEqual(fam["corporate_parent"], "C1001")

    def test_a_standalone_customer_gets_no_family_block(self) -> None:
        # Silence is the honest answer, and a block on every customer would
        # teach people to ignore it.
        self.assertNotIn("corporate_family",
                         _ar().customer(customer="C1007", business_unit=BU))


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

    def test_the_wide_route_resolves_the_name_before_the_id(self) -> None:
        # This used to require a search_customers round FIRST, because the
        # 360 compared cust_id to CUST_ID and a name got customer_not_found.
        # The tool resolves the name itself now, so the instruction is the
        # opposite one — and the model is told what the two refusal payloads
        # mean, since being handed a question it must relay is the case a
        # rule about calling order never covered.
        wide = self.text[self.text.index("ONE named customer"):]
        self.assertIn("cust_id accepts an id OR a name", wide[:1600])
        self.assertIn("ambiguous_customer", wide[:1600])
        self.assertIn("customer_not_found", wide[:1600])
        self.assertNotIn("search_customers to turn the NAME into a cust_id",
                         self.text, "a round trip the server now does")

    def test_the_payload_keys_that_signal_a_family_are_named(self) -> None:
        # A prompt that describes the idea but not the key the model will
        # actually see is a prompt the model cannot act on.
        for key in ("corporate_parent", "belongs_to_a_corporate_family",
                    "heads_a_corporate_family", "corporate_family",
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

    def test_it_does_not_send_the_model_back_for_what_the_turn_ran(self):
        # The 360 for C1004 and an aging in the same batch: the aging would
        # otherwise point at the 360 for C1004, which is already on screen.
        already = {("get_customer_financial_360", "C1004")}
        steps = _observed_next_steps(
            "get_ar_aging", self.ar.aging(business_unit=BU), "", BU, set(),
            already)
        self.assertNotIn(
            "Show the complete financial picture for customer C1004",
            [s["ask"] for s in steps])

    def test_one_customers_AR_produces_pointers_too(self) -> None:
        # get_customer_ar reports its customer under a singular key, so the
        # rule registered for it silently found nothing for half its tools.
        one = self.ar.customer(customer="C1004", business_unit=BU)
        self.assertTrue(_observed_next_steps("get_customer_ar", one, "", BU,
                                             set()))

    def test_a_clean_result_gets_no_pointers(self) -> None:
        self.assertEqual(_observed_next_steps(
            "get_open_payables", {"business_unit": BU, "overdue_total": 0},
            "", BU, set()), [])


if __name__ == "__main__":
    unittest.main()
