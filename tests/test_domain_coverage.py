"""Every fact domain a question can require must be reachable.

The evidence gate is `required_domains.issubset(covered_domains)`. Splitting a
broad domain into a narrow one is how guards.py stops a nearby result from
answering a question it does not cover — but the split has a failure mode with
no symptom in any other test: name a domain in a question pattern, forget to
name it on a tool, and that question becomes permanently unanswerable. The
turn still runs, the tool still succeeds, and the reader gets "I could not
obtain a successful PeopleSoft result".

Two real regressions arrived that way in one commit: `grni` was added to the
question patterns and given to no tool, and `journal_netting` took the
`balance` domain away from every "which journals make up this balance"
drill-down. Both suites were green.

So the invariant is checked mechanically. Deliberate holes are allowed —
some questions SHOULD be unanswerable — but they must be declared in
guards.UNSUPPORTED_DOMAIN_REASONS with a sentence saying what is missing.
"""
from __future__ import annotations

import ast
import inspect
import unittest

from pstb import guards
from pstb.guards import (
    UNSUPPORTED_DOMAIN_REASONS,
    financial_tool_is_relevant,
    question_financial_domains,
    unsupported_domain_reason,
)


def emittable_domains() -> set:
    """Every domain question_financial_domains can put in its result.

    Read from the source rather than a hand-kept list: a hand-kept list is
    the thing that goes stale, and staleness here is invisible.
    """
    tree = ast.parse(inspect.getsource(guards))
    fn = next(node for node in ast.walk(tree)
              if isinstance(node, ast.FunctionDef)
              and node.name == "question_financial_domains")
    added = {
        node.args[0].value
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    return set(guards._QUESTION_DOMAINS) | added


def owned_domains() -> set:
    return {domain
            for domains in guards._TOOL_DOMAINS.values()
            for domain in domains}


class DomainCoverageTests(unittest.TestCase):
    def test_the_ast_scan_actually_finds_the_added_domains(self):
        """Guard the guard: a silent scan finds nothing and passes."""
        found = emittable_domains()
        self.assertIn("po_grni_candidates", found,
                      "the source scan stopped seeing domains.add() calls, "
                      "so this file would pass while proving nothing")
        self.assertGreater(len(found), len(guards._QUESTION_DOMAINS))

    def test_every_required_domain_is_reachable_or_declared_unreachable(self):
        orphans = sorted(
            emittable_domains() - owned_domains()
            - set(UNSUPPORTED_DOMAIN_REASONS))
        self.assertEqual(
            orphans, [],
            "these domains can be required by a question but no tool "
            f"declares them: {orphans}. Either give one to a tool in "
            "_TOOL_DOMAINS, or — if the question genuinely cannot be "
            "answered here — add it to UNSUPPORTED_DOMAIN_REASONS with a "
            "sentence saying what is missing and what to ask instead.")

    def test_declared_holes_are_not_secretly_answerable(self):
        """A domain in both maps means the refusal text is a lie."""
        both = sorted(set(UNSUPPORTED_DOMAIN_REASONS) & owned_domains())
        self.assertEqual(both, [],
                         f"{both} claim to be unanswerable but a tool "
                         "declares them")

    def test_a_hole_refuses_with_a_remedy_not_a_shrug(self):
        reason, also_failed = unsupported_domain_reason({"grni_booked"})
        self.assertIn("RECV_LN_ACCTG", reason)
        self.assertIn("review", reason.lower())
        self.assertFalse(also_failed)

    def test_a_mixed_failure_reports_both_halves(self):
        """One hole plus one real outage must not hide either."""
        reason, also_failed = unsupported_domain_reason({"grni_booked", "ap"})
        self.assertIn("RECV_LN_ACCTG", reason)
        self.assertTrue(also_failed, "the ap miss is an ordinary failure and "
                                     "must not read as a design decision")

    def test_an_ordinary_failure_says_nothing_about_design(self):
        self.assertEqual(unsupported_domain_reason({"ap"}), ("", True))
        self.assertEqual(unsupported_domain_reason(set()), ("", False))


class JournalDrillDownTests(unittest.TestCase):
    """'Which journals make up this balance' is drill_to_journals' question."""

    DRILL_DOWNS = (
        "What journals make up the 1100 balance?",
        "Which journals affected the AR balance in period 6?",
        "Show me the journals behind the cash balance change",
        "Drill to the journals for account 1000 and show the balance",
        "What is the balance of 1100 and which journals moved it?",
    )

    def test_a_drill_down_keeps_the_balance_domain(self):
        for question in self.DRILL_DOWNS:
            with self.subTest(question=question):
                domains = question_financial_domains(question)
                self.assertNotIn(
                    "journal_netting", domains,
                    "asking which journals sit behind a balance is not "
                    "asking whether the journal nets to zero")
                self.assertTrue(
                    financial_tool_is_relevant("drill_to_journals", question),
                    "drill_to_journals stopped being relevant to the "
                    "drill-down it exists for")

    def test_a_real_netting_question_still_routes_to_the_status_control(self):
        for question in ("Does journal J0001 net to zero?",
                         "Do the journal lines balance?",
                         "Is journal 0000000123 out of balance?",
                         "Are the June journals balanced?",
                         "Does the journal net out?"):
            with self.subTest(question=question):
                self.assertIn("journal_netting",
                              question_financial_domains(question))

    def test_posted_by_needs_a_journal_subject(self):
        """PS_JRNL_HEADER cannot answer a question about vouchers."""
        vouchers = "Were the AP vouchers posted at period end?"
        self.assertNotIn("journal_posted_by",
                         question_financial_domains(vouchers))
        journals = "Was the accrual journal posted by 30 June?"
        self.assertIn("journal_posted_by",
                      question_financial_domains(journals))


class ReceivedNotInvoicedTests(unittest.TestCase):
    PLAIN = (
        "What is our GRNI balance for US001?",
        "Show me received not invoiced for US001",
        "What uninvoiced receipts do we have?",
        "How much RNI is outstanding?",
        "what is our receipt accrual?",
    )

    def test_the_plain_question_reaches_the_grni_tool(self):
        for question in self.PLAIN:
            with self.subTest(question=question):
                self.assertTrue(
                    financial_tool_is_relevant("get_po_grni_candidates",
                                               question),
                    "the received-not-invoiced tool is unreachable from the "
                    "plainest way to ask for it")

    def test_a_grni_question_does_not_also_demand_a_ledger_balance(self):
        domains = question_financial_domains("What is our GRNI balance?")
        self.assertNotIn("balance", domains)
        self.assertIn("grni", domains)

    def test_a_booked_liability_claim_is_refused_by_name(self):
        for question in ("What GRNI liability is booked in the GL?",
                         "How much receipt accrual was posted to the "
                         "general ledger?"):
            with self.subTest(question=question):
                domains = question_financial_domains(question)
                self.assertIn("grni_booked", domains)
                self.assertNotIn("grni", domains)
                self.assertFalse(
                    financial_tool_is_relevant("get_po_grni_candidates",
                                               question),
                    "a candidate list must not authorize a booked-liability "
                    "claim")


if __name__ == "__main__":
    unittest.main()
