"""Asked "what is revenue for customer CIBC", the agent went to the ledger.

Two independent defects compounded into one bad answer, and both are pinned
here because either one alone reproduces it.

THE ROUTING. "Revenue" is a general-ledger word — a trial-balance caption,
an account type. But PS_LEDGER has no customer column: its dimensions are
business unit, ledger, year, period, account, department, product, project.
So a customer-scoped revenue question asked of the ledger either drops the
customer silently and reports the whole company, or filters on nothing and
returns zero. Both look like answers. Revenue by customer lives in billing.

THE LITERAL ID. Even routed correctly, the tool took cust_id as a key and
compared it to CUST_ID. A person types the company's NAME, because that is
what they have. "CIBC" came back "does not exist" — which reads as a zero
for a customer that may be perfectly real.

The fix for the second is machinery, not instruction: the tool resolves a
name the way a person would, and says what it resolved. A prompt rule that
says "call search_customers first" is a rule that gets skipped.
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling  # noqa: E402
from pstb.config import load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine, resolve_party_ref  # noqa: E402
from pstb.relationships import Relationships  # noqa: E402

BU = "US001"


def _rel() -> Relationships:
    cfg = load_config(str(ROOT / "config.yaml"))
    db = Database(cfg)
    return Relationships(ARBilling(TBEngine(db, cfg)))


class LedgerHasNoCustomerTests(unittest.TestCase):
    """The fact the routing rule rests on, asserted rather than assumed."""

    def test_ps_ledger_carries_no_customer_dimension(self) -> None:
        con = sqlite3.connect(str(ROOT / "sample_data" / "ps_sample.db"))
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(PS_LEDGER)")}
        finally:
            con.close()
        self.assertTrue(cols, "the sample ledger is missing")
        for col in ("CUST_ID", "CUSTOMER_ID", "VENDOR_ID"):
            self.assertNotIn(col, cols)


class PromptRoutingTests(unittest.TestCase):
    """The prompt must send a customer-revenue question to billing."""

    @staticmethod
    def _prompt() -> str:
        from pstb.client.prompt import system_prompt
        return system_prompt(load_config(str(ROOT / "config.yaml")), "gui")

    def test_it_says_the_ledger_cannot_answer_it(self) -> None:
        p = self._prompt()
        self.assertIn("PS_LEDGER has no customer column", p)
        self.assertIn("get_customer_financial_360", p)

    def test_the_word_revenue_appears_in_the_customer_routing(self) -> None:
        # The defect was a vocabulary gap: the customer block's triggers were
        # "what does X owe" and "tell me about X", so a question phrased with
        # "revenue" matched only the GL block.
        p = self._prompt()
        for word in ("revenue", "billings", "sales"):
            self.assertIn(word, p.split("ONE named customer")[0][-3000:]
                          + p.split("ONE named customer")[-1][:3000], word)

    def test_no_example_steers_revenue_by_customer_at_ad_hoc_sql(self) -> None:
        # prompt.py used to give `"revenue by customer" -> join_path(...)`
        # as the worked example for hand-joining, which is exactly the
        # question a curated tool already answers.
        p = self._prompt()
        self.assertNotIn('"revenue by customer" -> join_path', p)


class NameResolutionTests(unittest.TestCase):
    """cust_id takes what the person actually has: the company's name."""

    def setUp(self) -> None:
        self.rel = _rel()

    def _360(self, who: str) -> dict:
        return self.rel.customer_financial_360(cust_id=who, business_unit=BU)

    def test_an_id_still_resolves_to_itself(self) -> None:
        out = self._360("C1001")
        self.assertEqual(out["customer"]["cust_id"], "C1001")
        self.assertEqual([n for n in out["record_notes"] if "Read " in n], [],
                         "an id is not a resolution and needs no note")

    def test_a_full_name_resolves_and_says_so(self) -> None:
        out = self._360("ACME Industrial")
        self.assertEqual(out["customer"]["cust_id"], "C1001")
        self.assertTrue(any("ACME Industrial" in n and "C1001" in n
                            for n in out["record_notes"]),
                        "resolving a name is a substitution and must be "
                        f"disclosed: {out['record_notes']}")

    def test_an_exact_name_beats_a_fragment_of_a_longer_one(self) -> None:
        # "ACME Industrial" is also a prefix of "ACME Industrial West". An
        # exact name is an answer, not an ambiguity.
        self.assertEqual(self._360("ACME Industrial")["customer"]["cust_id"],
                         "C1001")

    def test_a_revenue_question_reaches_billing(self) -> None:
        # The whole point: the number a person is asking for is in the
        # payload, from the tool the prompt now routes to.
        out = self._360("ACME Industrial")
        finalized = [r for r in out["billing"]["by_status"]
                     if r.get("finalized")]
        self.assertTrue(finalized, "no invoiced revenue for a customer "
                                   "that has bills")
        self.assertGreater(finalized[0]["amount"], 0)
        self.assertTrue(finalized[0]["currency"],
                        "revenue without a currency cannot be quoted")

    def test_an_ambiguous_fragment_asks_instead_of_guessing(self) -> None:
        out = self._360("ACME")
        self.assertEqual(out["scope_status"], "ambiguous_customer")
        self.assertGreater(len(out["multiple_matches"]), 1)
        self.assertNotIn("billing", out,
                         "a guess dressed as an answer is the failure mode "
                         "this whole path exists to avoid")

    def test_a_name_nobody_has_is_NO_DATA_not_a_zero(self) -> None:
        out = self._360("Nonesuch Holdings")
        self.assertEqual(out["scope_status"], "customer_not_found")
        self.assertIn("NO DATA", out["detail"])
        self.assertNotIn("zero balance", out["detail"].split("NO DATA")[0])

    def test_it_does_not_dump_arbitrary_ids_to_pick_from(self) -> None:
        # The old refusal listed the first 15 CUST_IDs in the SETID. On a
        # real instance that is fifteen strangers, and offering them invites
        # the model to answer about one of them.
        out = self._360("Nonesuch Holdings")
        self.assertNotIn("known_customer_ids", out)
        self.assertEqual(out["did_you_mean"], [],
                         "nothing resembles this name, so nothing is offered")


class LooseRetryTests(unittest.TestCase):
    """A longer name than the master holds still finds its way home."""

    def test_the_longest_word_is_retried_and_only_suggested(self) -> None:
        rows = [{"cust_id": "C1002", "name": "Northwind Retail"}]

        def search(term):
            return rows if term.upper() == "NORTHWIND" else []

        cid, note, refusal = resolve_party_ref(
            "Northwind Retail Group Inc", search, "cust_id", "customer")
        self.assertEqual(cid, "")
        self.assertEqual(refusal["scope_status"], "customer_not_found")
        self.assertEqual(refusal["did_you_mean"],
                         [{"cust_id": "C1002", "name": "Northwind Retail"}])

    def test_a_loose_match_is_never_applied_silently(self) -> None:
        # Offered, not substituted. Answering about a different legal entity
        # than the one asked about is not a rounding error.
        rows = [{"cust_id": "C1002", "name": "Northwind Retail"}]
        cid, _, refusal = resolve_party_ref(
            "Northwind Retail Group Inc",
            lambda t: rows if t.upper() == "NORTHWIND" else [],
            "cust_id", "customer")
        self.assertFalse(cid)
        self.assertIsNotNone(refusal)

    def test_short_words_are_not_worth_retrying(self) -> None:
        seen = []

        def search(term):
            seen.append(term)
            return []

        resolve_party_ref("A B C", search, "cust_id", "customer")
        self.assertEqual(seen, ["A B C"],
                         "retrying on a two-letter token matches half the "
                         "customer master")


class SupplierSideTests(unittest.TestCase):
    """The same resolution on the payables side, or it is not fixed."""

    @staticmethod
    def _net():
        from pstb.modules import ModulePacks
        from pstb.vendors import VendorNetwork
        cfg = load_config(str(ROOT / "config.yaml"))
        db = Database(cfg)
        return VendorNetwork(ModulePacks(TBEngine(db, cfg)))

    def test_a_supplier_name_resolves(self) -> None:
        out = self._net().vendor_payables_network(vendor_id="Ridgeline Supply Co",
                                                  business_unit=BU)
        self.assertEqual(out["supplier"]["vendor_id"], "V1001")

    def test_an_ambiguous_supplier_name_asks(self) -> None:
        out = self._net().vendor_payables_network(vendor_id="Ridgeline",
                                                  business_unit=BU)
        self.assertEqual(out["scope_status"], "ambiguous_supplier")
        self.assertGreater(len(out["multiple_matches"]), 1)


class RendererTests(unittest.TestCase):
    """"Which one?" is a question. It must not render as "No data"."""

    def test_the_card_shows_the_candidates(self) -> None:
        html = (ROOT / "pstb" / "gui" / "static" / "index.html").read_text()
        self.assertIn("multiple_matches", html)
        self.assertIn("did_you_mean", html)
        self.assertIn("Which one?", html,
                      "four matches is not an absence of data")


if __name__ == "__main__":
    unittest.main()
