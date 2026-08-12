"""A suggested question is a promise. These are the terms of it.

The rules under test, in the order they matter:

  1. Nothing is suggested that no tool can answer. A follow-up that fails
     teaches the person the thing cannot be asked, which is worse than
     silence — so every rule's ``answered_by`` is checked against the real
     tool set, and the generic wordings are checked against the eval suite
     that proves they route.
  2. Every suggestion carries the figure that earned it. A rule that fires
     on an empty list or a zero is a slot machine.
  3. Nothing is suggested back to the turn that just answered it.
  4. A refusal is not evidence: "this business unit does not exist" must
     not produce a confident next step built on the failure.
  5. Suggestions never cost the answer. A rule that trips over an
     unfamiliar payload loses its suggestion and nothing else.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling  # noqa: E402
from pstb.config import load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.modules import ModulePacks  # noqa: E402
from pstb.relationships import Relationships  # noqa: E402
from pstb.suggest import MAX_SUGGESTIONS, RULES, suggestions_for  # noqa: E402

BU = "US001"


def _stack():
    cfg = load_config(str(ROOT / "config.yaml"))
    engine = TBEngine(Database(cfg), cfg)
    ar = ARBilling(engine)
    return engine, ar, Relationships(ar), ModulePacks(engine)


class ContractTests(unittest.TestCase):
    """What the module promises before any payload is involved."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine, cls.ar, cls.rel, cls.modules = _stack()
        cls.eval_questions = {c["question"] for c in json.loads(
            (ROOT / "evals" / "cases.json").read_text())["cases"]}

    def _every_suggestion(self) -> list:
        """Fire every rule against real payloads and collect the output."""
        payloads = [
            ("get_customer_financial_360", self.rel.customer_financial_360(
                cust_id="C1004", business_unit=BU, as_of_date="2026-08-12")),
            ("get_customer_financial_360", self.rel.customer_financial_360(
                cust_id="C1001", business_unit=BU, as_of_date="2026-06-30")),
            ("get_ar_aging", self.ar.aging(business_unit=BU)),
            ("tb_integrity_check",
             self.engine.tb_integrity_check(business_unit=BU)),
            ("get_billing_workbench",
             self.ar.billing_workbench(business_unit=BU)),
            ("get_invoice_lifecycle",
             self.ar.invoice_lifecycle(business_unit=BU)),
            ("get_duplicate_payments",
             self.modules.duplicate_payments(business_unit=BU)),
            ("get_open_payables", self.modules.open_payables(
                business_unit=BU)),
        ]
        out = []
        for entry in payloads:                # one at a time, past the cap
            out.extend(suggestions_for([entry], business_unit=BU))
        return out

    def test_every_suggestion_names_a_tool_that_exists(self) -> None:
        from pstb import server
        tools = {name for name in dir(server) if name.startswith(
            ("get_", "run_", "search_", "drill_", "tb_", "wiki_", "explain_",
             "compare_", "rollup_", "list_", "describe_", "resolve_",
             "profile_", "coupa_"))}
        for s in self._every_suggestion():
            self.assertIn(s["answered_by"], tools,
                          f"{s['kind']} suggests a tool that is not exposed")

    def test_every_rule_is_keyed_to_a_tool_that_exists(self) -> None:
        from pstb import server
        for name in RULES:
            self.assertTrue(hasattr(server, name),
                            f"a rule reads {name}, which is not a tool")

    def test_a_generic_wording_is_one_the_eval_suite_proves(self) -> None:
        # A suggestion carrying no customer or vendor id is a question
        # anyone could type, so it must be one the suite already routes.
        # The ones WITH an id cannot be pinned by exact text; they are
        # covered by the tool check above instead.
        import re
        for s in self._every_suggestion():
            if re.search(r"\b[A-Z]{1,4}\d{3,}\b", s["question"]):
                continue
            if s["question"].endswith(f"for {BU}") or \
                    f"in {BU} " in s["question"]:
                continue          # scoped wording, same reasoning as an id
            self.assertIn(s["question"], self.eval_questions,
                          "a generic suggestion with no eval case behind it")

    def test_every_suggestion_carries_its_reason(self) -> None:
        for s in self._every_suggestion():
            self.assertTrue(s["because"].strip(), s)
            self.assertTrue(s["evidence_from"], s)

    def test_the_list_is_bounded(self) -> None:
        many = [("get_ar_aging", self.ar.aging(business_unit=BU)),
                ("tb_integrity_check",
                 self.engine.tb_integrity_check(business_unit=BU)),
                ("get_billing_workbench",
                 self.ar.billing_workbench(business_unit=BU)),
                ("get_duplicate_payments",
                 self.modules.duplicate_payments(business_unit=BU)),
                ("get_open_payables",
                 self.modules.open_payables(business_unit=BU))]
        self.assertLessEqual(len(suggestions_for(many, business_unit=BU)),
                             MAX_SUGGESTIONS)


class EvidenceTests(unittest.TestCase):
    """Each rule, against the payload that should and should not fire it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine, cls.ar, cls.rel, cls.modules = _stack()

    def _kinds(self, payloads, **kw) -> dict:
        return {s["kind"]: s for s in suggestions_for(payloads, **kw)}

    def test_unapplied_cash_points_at_the_open_items(self) -> None:
        p = self.rel.customer_financial_360(cust_id="C1004",
                                            business_unit=BU,
                                            as_of_date="2026-08-12")
        hit = self._kinds([("get_customer_financial_360", p)],
                          business_unit=BU)["unapplied_cash"]
        self.assertEqual(hit["question"],
                         "Show the open AR items for customer C1004")
        self.assertEqual(hit["amount"], 10_000.00)
        self.assertIn("DEP-26063", hit["because"])

    def test_the_subsidiary_named_is_not_the_one_just_asked_about(self):
        # The parent is usually its own largest contributor. Offering to
        # look at the customer already on screen is the rule wasted.
        p = self.rel.customer_financial_360(cust_id="C1001",
                                            business_unit=BU,
                                            as_of_date="2026-06-30")
        hit = self._kinds([("get_customer_financial_360", p)],
                          business_unit=BU)["subsidiary_drives_overdue"]
        self.assertIn("C1010", hit["question"])
        self.assertNotIn("C1001", hit["question"])
        self.assertEqual(hit["amount"], 19_900.00)

    def test_a_credit_never_rebilled_routes_to_the_pipeline(self) -> None:
        p = self.rel.customer_financial_360(cust_id="C1001",
                                            business_unit=BU,
                                            as_of_date="2026-06-30")
        hit = self._kinds([("get_customer_financial_360", p)],
                          business_unit=BU)["billing_leakage"]
        self.assertEqual(hit["question"], "Where is the billing delay?")
        self.assertIn("INV-260301", hit["because"])

    def test_aging_points_at_the_customer_worth_opening(self) -> None:
        kinds = self._kinds([("get_ar_aging", self.ar.aging(
            business_unit=BU))], business_unit=BU)
        self.assertIn("C1004", kinds["disputes"]["question"])
        self.assertIn("get_customer_financial_360",
                      kinds["disputes"]["answered_by"])

    def test_a_suspense_balance_becomes_a_policy_question(self) -> None:
        p = self.engine.tb_integrity_check(business_unit=BU)
        hit = self._kinds([("tb_integrity_check", p)],
                          business_unit=BU)["suspense"]
        self.assertEqual(
            hit["question"],
            "Is our suspense balance in account 1999 within policy?")
        self.assertEqual(hit["answered_by"], "wiki_lookup")

    def test_a_duplicate_asks_whether_it_was_actually_paid(self) -> None:
        p = self.modules.duplicate_payments(business_unit=BU)
        hit = self._kinds([("get_duplicate_payments", p)],
                          business_unit=BU)["duplicate_payment"]
        self.assertIn("V1001", hit["question"])
        self.assertIn("PAID twice", hit["because"])

    def test_a_clean_payload_suggests_nothing(self) -> None:
        # The common case. A tool that found no problem must not manufacture
        # a next step just to have something to show.
        empty = {"business_unit": BU, "customers": [], "gl_tie":
                 {"evaluated": True, "ties": True}, "display_currency": "USD"}
        self.assertEqual(suggestions_for([("get_ar_aging", empty)]), [])

    def test_zero_is_not_evidence(self) -> None:
        self.assertEqual(suggestions_for([("get_open_payables", {
            "business_unit": BU, "overdue_total": 0.0})]), [])


class HygieneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine, cls.ar, cls.rel, cls.modules = _stack()

    def test_it_never_offers_back_the_question_just_asked(self) -> None:
        p = self.ar.aging(business_unit=BU)
        asked = suggestions_for(
            [("get_ar_aging", p)], business_unit=BU,
            question="Show the complete financial picture for customer C1004")
        self.assertNotIn("Show the complete financial picture for customer "
                         "C1004", [s["question"] for s in asked])

    def test_it_never_offers_a_tool_this_turn_already_ran_on_that_id(self):
        # The 360 for C1004 reports a dispute; aging would then suggest
        # opening the 360 for C1004, which is the card already on screen.
        p360 = self.rel.customer_financial_360(cust_id="C1004",
                                               business_unit=BU,
                                               as_of_date="2026-08-12")
        both = suggestions_for(
            [("get_customer_financial_360", p360),
             ("get_ar_aging", self.ar.aging(business_unit=BU))],
            business_unit=BU)
        self.assertNotIn(
            "Show the complete financial picture for customer C1004",
            [s["question"] for s in both])

    def test_a_refusal_is_not_evidence(self) -> None:
        self.assertEqual(suggestions_for([("get_ar_aging", {
            "scope_status": "business_unit_not_found",
            "detail": "no such unit", "customers": []})]), [])
        self.assertEqual(suggestions_for([("get_customer_financial_360", {
            "error": "DbError: ORA-00942"})]), [])

    def test_a_payload_it_cannot_read_costs_that_rule_only(self) -> None:
        broken = {"business_unit": BU, "needs_attention": "not a list"}
        good = self.modules.duplicate_payments(business_unit=BU)
        out = suggestions_for([("get_customer_financial_360", broken),
                               ("get_duplicate_payments", good)],
                              business_unit=BU)
        self.assertEqual([s["kind"] for s in out], ["duplicate_payment"])

    def test_json_strings_are_accepted_like_dicts(self) -> None:
        # The GUI holds prior payloads as the raw JSON the tool returned.
        p = self.modules.duplicate_payments(business_unit=BU)
        self.assertEqual(
            suggestions_for([("get_duplicate_payments", json.dumps(p))]),
            suggestions_for([("get_duplicate_payments", p)]))

    def test_an_unknown_tool_is_ignored_not_guessed_at(self) -> None:
        self.assertEqual(suggestions_for(
            [("some_future_tool", {"business_unit": BU, "amount": 5})]), [])


class WebTests(unittest.TestCase):
    LOOP = {"base_url": "http://127.0.0.1:8000", "client": ("127.0.0.1", 1)}

    @classmethod
    def setUpClass(cls) -> None:
        from starlette.testclient import TestClient
        from pstb.gui import app as gapp
        cls.TestClient, cls.gapp = TestClient, gapp

    def test_the_endpoint_is_empty_for_a_session_that_never_asked(self):
        client = self.TestClient(self.gapp.app, **self.LOOP)
        body = client.get("/api/suggestions?session_id=abcdefgh12345678").json()
        self.assertEqual(body["suggestions"], [])

    def test_a_stored_turn_is_readable_by_session_and_dies_on_clear(self):
        session = "sugg_test_session_01"
        self.gapp._suggestions_store(session, [{"question": "q",
                                                "because": "b"}])
        client = self.TestClient(self.gapp.app, **self.LOOP)
        self.assertEqual(len(client.get(
            f"/api/suggestions?session_id={session}").json()["suggestions"]),
            1)
        client.post("/api/chat/reset", json={"session_id": session})
        self.assertEqual(client.get(
            f"/api/suggestions?session_id={session}").json()["suggestions"],
            [], "Clear must not leave a follow-up pointing at evidence the "
                "page no longer shows")

    def test_a_malformed_session_id_gets_nothing(self) -> None:
        client = self.TestClient(self.gapp.app, **self.LOOP)
        self.assertEqual(client.get("/api/suggestions?session_id=../x")
                         .json()["suggestions"], [])

    def test_the_helper_never_raises_on_a_bad_payload(self) -> None:
        self.assertEqual(
            self.gapp._suggestions_for_turn([("get_ar_aging", object())],
                                            "q", {"business_unit": BU}), [])


if __name__ == "__main__":
    unittest.main()
