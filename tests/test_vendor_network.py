"""One supplier, everything connected — and the values that must not travel.

The AR side of this shipped first and most of the contract is the same:
family from the recorded hierarchy only, totals aggregated by the database,
a map that carries no money, a section that fails costs that section. What
is new here is the payables-side question a controller actually asks — are
two suppliers the same, or worse, are we paying two suppliers into one
account — and that question is about values nobody should be handed.

So the rules under test, hardest first:

  1. NOTHING LEAKS. No bank account number, no taxpayer id, no masked or
     partial form of either, reaches the payload. Not last-4, not
     `***-**-1234`: both were measured feeding real digits to the number
     guard, which then vouches for them. The token is letters only for the
     same reason.
  2. NO SALT, NO CHECK. An unsalted hash of a nine-digit identifier is a
     billion-entry lookup, so an unsalted "hash" is the raw value with
     extra steps. There is deliberately no unsalted path.
  3. FORMAT IS NOT IDENTITY. "045.600.1122" and "45-6001122" are one
     taxpayer id typed twice. A literal-equality check misses exactly the
     pair it exists to find, which is why the sample has that pair.
  4. A SHARED KEY IS NOT A FAMILY. It never becomes a family edge, never
     gets a confidence score, and the payload says out loud that it is a
     reason to look rather than a finding.
  5. NAMES NEVER GROUP ANYTHING. "Ridgeline Supply Group" is a different
     company from "Ridgeline Supply Co" and is in the sample to prove it.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import load_config  # noqa: E402
from pstb.db import Database, DbError  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.guards import payload_numbers, unit_access_block  # noqa: E402
from pstb.modules import ModuleError, ModulePacks  # noqa: E402
from pstb.security import Access  # noqa: E402
from pstb.vendors import SALT_ENV, VendorNetwork, _normalise, _token  # noqa: E402

BU, ASOF, SALT = "US001", "2026-08-12", "unit-test-salt-value"

# Every raw value the sample carries that must never appear in a payload.
SECRETS = ("000123456789", "0000123456789", "8837-2210-04", "5590-8811-72",
           "8837-2210-99", "45-6001122", "045.600.1122", "94-3177001",
           "81-2299450", "27-8830014", "33-5512009")


def _net(db_cls=Database):
    cfg = load_config(str(ROOT / "config.yaml"))
    return VendorNetwork(ModulePacks(TBEngine(db_cls(cfg), cfg)))


class _Salted(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get(SALT_ENV)
        os.environ[SALT_ENV] = SALT
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            os.environ.pop(SALT_ENV, None)
        else:
            os.environ[SALT_ENV] = self._saved

    def net(self, vendor="V1001", **kw):
        return _net().vendor_payables_network(
            vendor_id=vendor, business_unit=BU, as_of_date=ASOF, **kw)


class LeakTests(_Salted):
    """The rule with no exceptions."""

    def test_no_raw_identifier_appears_in_any_payload(self) -> None:
        for vendor in ("V1001", "V1002", "V1003", "V1006", "V1007"):
            blob = json.dumps(self.net(vendor), default=str)
            for secret in SECRETS:
                self.assertNotIn(secret, blob, f"{vendor} leaked {secret}")

    def test_no_partial_or_masked_form_either(self) -> None:
        # last-4 grounds 6789; "***-**-1234" grounds -1234. A partial is
        # not a safer secret, it is a smaller one plus a real number in the
        # allowlist.
        blob = json.dumps(self.net("V1002"), default=str)
        for tail in ("6789", "3456789", "2210", "8811"):
            self.assertNotIn(tail, blob, f"a partial identifier ({tail}) "
                                         "reached the payload")

    def test_the_token_adds_no_figure_the_model_may_quote(self) -> None:
        link = self.net("V1002")["identity_links"]["links"][0]
        self.assertEqual(payload_numbers([json.dumps({"t": link["token"]})]),
                         set(),
                         "a token containing digits hands the grounding "
                         "guard figures it will then vouch for")
        self.assertRegex(link["token"], r"^(BANK|TAXID)-[A-HJ-NP-R]+$")

    def test_the_token_is_keyed_not_a_plain_digest(self) -> None:
        # An unsalted digest of a nine-digit id is a lookup table.
        same = _token("BANK", "000123456789", b"salt-one")
        other = _token("BANK", "000123456789", b"salt-two")
        self.assertNotEqual(same, other)
        self.assertEqual(same, _token("BANK", "000123456789", b"salt-one"))


class SaltTests(unittest.TestCase):
    def test_no_salt_means_no_check_and_says_why(self) -> None:
        saved = os.environ.pop(SALT_ENV, None)
        try:
            out = _net().vendor_payables_network(vendor_id="V1002",
                                                 business_unit=BU)
        finally:
            if saved is not None:
                os.environ[SALT_ENV] = saved
        links = out["identity_links"]
        self.assertFalse(links["supported"])
        self.assertEqual(links["links"], [])
        self.assertIn(SALT_ENV, links["note"])
        self.assertIn("reversible", links["note"])
        # And the rest of the answer still works.
        self.assertTrue(out["payables"]["totals_by_currency"])


class NormalisationTests(_Salted):
    def test_the_same_id_typed_two_ways_is_one_id(self) -> None:
        self.assertEqual(_normalise("045.600.1122"), _normalise("45-6001122"))
        self.assertEqual(_normalise("000123456789"),
                         _normalise("0000123456789"))
        self.assertNotEqual(_normalise("45-6001122"), _normalise("45-6001123"))

    def test_the_shared_tax_id_is_found_despite_the_formatting(self) -> None:
        # The pair a literal-equality check misses. V1003 writes it
        # 45-6001122; V1008 writes it 045.600.1122.
        link = next(x for x in self.net("V1003")["identity_links"]["links"]
                    if x["kind"] == "shared_tax_id")
        self.assertEqual(link["suppliers"], ["V1003", "V1008"])

    def test_the_shared_bank_account_is_found_too(self) -> None:
        link = next(x for x in self.net("V1002")["identity_links"]["links"]
                    if x["kind"] == "shared_bank_account")
        self.assertEqual(link["suppliers"], ["V1002", "V1007"])

    def test_both_ends_of_a_link_see_the_same_token(self) -> None:
        a = self.net("V1002")["identity_links"]["links"][0]["token"]
        b = self.net("V1007")["identity_links"]["links"][0]["token"]
        self.assertEqual(a, b)

    def test_a_supplier_with_its_own_account_gets_no_link(self) -> None:
        self.assertEqual(self.net("V1001")["identity_links"]["links"], [])


class NotAFamilyTests(_Salted):
    """A shared key is evidence to check. It is not an identity."""

    def test_a_shared_key_never_becomes_a_family_member(self) -> None:
        out = self.net("V1002")
        self.assertEqual([m["vendor_id"] for m in out["family"]["members"]],
                         ["V1002"])
        self.assertNotIn("V1007",
                         json.dumps(out["family"], default=str))

    def test_the_payload_says_what_a_shared_key_means(self) -> None:
        links = self.net("V1002")["identity_links"]
        self.assertIn("reason to LOOK", links["read_this_as"])
        self.assertIn("recorded corporate hierarchy", links["read_this_as"])

    def test_the_attention_line_refuses_to_call_it_identity(self) -> None:
        hit = next(a for a in self.net("V1002")["needs_attention"]
                   if a["kind"] == "shared_bank_account")
        self.assertIn("not, by itself, evidence", hit["headline"])
        self.assertIn("recorded hierarchy", hit["headline"])

    def test_no_confidence_score_anywhere(self) -> None:
        # 0.82 is a number the model restates as a fact.
        blob = json.dumps(self.net("V1002"), default=str)
        for word in ("confidence", "probability", "likelihood", "score"):
            self.assertNotIn(word, blob.lower())


class FamilyTests(_Salted):
    def test_the_family_is_the_recorded_hierarchy(self) -> None:
        ids = {m["vendor_id"] for m in self.net("V1001")["family"]["members"]}
        self.assertEqual(ids, {"V1001", "V1004", "V1005"})

    def test_the_lookalike_is_never_folded_in(self) -> None:
        # "Ridgeline Supply Group" is its own corporate parent.
        out = self.net("V1001")
        self.assertNotIn("V1006",
                         {m["vendor_id"] for m in out["family"]["members"]})
        self.assertIn("never grouped by name", out["family"]["basis"])

    def test_the_group_total_is_its_members_added_up(self) -> None:
        out = self.net("V1001")
        total = out["payables"]["totals_by_currency"][0]["open_amount"]
        parts = sum(row["open_amount"]
                    for m in out["family"]["members"]
                    for row in m["payables"])
        self.assertAlmostEqual(total, parts, places=2)
        self.assertEqual(total, 50_150.00)

    def test_one_supplier_asked_for_is_one_supplier(self) -> None:
        out = self.net("V1001", include_family=False)
        self.assertEqual(out["family"]["member_count"], 1)
        self.assertEqual(
            out["payables"]["totals_by_currency"][0]["open_amount"],
            18_400.00)

    def test_a_site_with_no_supplier_hierarchy_says_so(self) -> None:
        class NoCorp(Database):
            def columns(self, table):
                cols = super().columns(table)
                return ({c for c in cols
                         if c not in ("CORPORATE_VENDOR",
                                      "CORPORATE_VNDR_ID")}
                        if table == "PS_VENDOR" else cols)

        out = _net(NoCorp).vendor_payables_network(vendor_id="V1001",
                                                   business_unit=BU)
        self.assertEqual(out["family"]["member_count"], 1)
        self.assertTrue(any("corporate supplier column" in n
                            for n in out["record_notes"]))


class PayablesTests(_Salted):
    def test_overdue_and_stuck_are_reported_separately(self) -> None:
        out = self.net("V1003")
        kinds = {a["kind"] for a in out["needs_attention"]}
        self.assertIn("stuck_in_pipeline", kinds)
        stuck = next(a for a in out["needs_attention"]
                     if a["kind"] == "stuck_in_pipeline")
        self.assertIn("VCHR90004", stuck["vouchers"])

    def test_duplicates_are_delegated_not_recomputed(self) -> None:
        # The same exposure counted two ways in one conversation is the
        # failure this avoids.
        out = self.net("V1001")
        self.assertEqual(out["duplicates"]["from_tool"],
                         "get_duplicate_payments")
        mine = out["duplicates"]["exact_invoice_duplicates"]
        theirs = _net().mp.duplicate_payments(
            business_unit=BU, as_of_date=ASOF)["exact_invoice_duplicates"]
        self.assertEqual(mine, [d for d in theirs
                                if d["vendor_id"] in ("V1001", "V1004",
                                                      "V1005")])

    def test_the_map_carries_no_money(self) -> None:
        graph = self.net("V1002")["relationships"]
        for node in graph["nodes"]:
            self.assertEqual(set(node), {"id", "type", "label"}, node)
        self.assertIn("SHARES_KEY is an observation", graph["read_this_as"])

    def test_every_edge_has_two_real_endpoints(self) -> None:
        graph = self.net("V1001")["relationships"]
        ids = {n["id"] for n in graph["nodes"]}
        for e in graph["edges"]:
            self.assertIn(e["from"], ids, e)
            self.assertIn(e["to"], ids, e)


class DegradeTests(_Salted):
    def test_no_bank_record_is_UNKNOWN_not_none_found(self) -> None:
        class NoBank(Database):
            def columns(self, table):
                return (set() if table == "PS_VNDR_BANK_ACCT"
                        else super().columns(table))

            def query(self, sql, params=None, **kw):
                if "PS_VNDR_BANK_ACCT" in sql:
                    raise DbError("ORA-00942: table or view does not exist")
                return super().query(sql, params, **kw)

        out = _net(NoBank).vendor_payables_network(vendor_id="V1002",
                                                   business_unit=BU)
        note = out["identity_links"]["note"]
        self.assertIn("UNKNOWN", note)
        self.assertIn("PS_VNDR_BANK_ACCT", note)
        # The tax check still ran.
        self.assertIn("tax id", out["identity_links"]["checked"])

    def test_a_branch_that_raises_costs_its_own_section(self) -> None:
        class NoVouchers(Database):
            def query(self, sql, params=None, **kw):
                if "PS_VOUCHER" in sql:
                    raise DbError("ORA-00942: table or view does not exist")
                return super().query(sql, params, **kw)

        out = _net(NoVouchers).vendor_payables_network(vendor_id="V1002",
                                                       business_unit=BU)
        self.assertEqual(out["payables"]["totals_by_currency"], [])
        self.assertTrue(any("could not be read" in n
                            for n in out["record_notes"]))
        self.assertTrue(out["identity_links"]["supported"])


class RefusalTests(_Salted):
    def test_an_unknown_supplier_is_NO_DATA_not_a_zero_balance(self) -> None:
        out = _net().vendor_payables_network(vendor_id="V9999",
                                             business_unit=BU)
        self.assertEqual(out["scope_status"], "vendor_not_found")
        self.assertIn("NO DATA", out["detail"])
        self.assertIn("V1001", out["known_vendor_ids"])

    def test_no_supplier_at_all_names_the_way_forward(self) -> None:
        with self.assertRaises(ModuleError) as ctx:
            _net().vendor_payables_network(business_unit=BU)
        self.assertIn("search_vendors", str(ctx.exception))

    def test_it_is_gated_by_business_unit(self) -> None:
        restricted = Access(oprid="FIN_US001", units=frozenset({"US001"}))
        self.assertEqual(unit_access_block(
            "get_vendor_payables_network", {"business_unit": "US001"},
            restricted), "")
        self.assertIn("CA001", unit_access_block(
            "get_vendor_payables_network", {"business_unit": "CA001"},
            restricted))


class SearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.out = _net().mp.search_vendors(query="Ridgeline",
                                           business_unit=BU)

    def test_resolving_a_name_reveals_the_group(self) -> None:
        self.assertEqual(set(self.out["belongs_to_a_corporate_family"]),
                         {"V1004", "V1005"})
        self.assertIn("V1001", self.out["heads_a_corporate_family"])

    def test_the_lookalike_is_returned_but_not_grouped(self) -> None:
        v1006 = next(v for v in self.out["vendors"]
                     if v["vendor_id"] == "V1006")
        self.assertEqual(v1006["corporate_parent"], "V1006")
        self.assertFalse(v1006["heads_a_corporate_family"])
        self.assertNotIn("V1006", self.out["belongs_to_a_corporate_family"])

    def test_it_names_the_tool_that_rolls_the_group_up(self) -> None:
        self.assertIn("get_vendor_payables_network", self.out["next_step"])
        self.assertIn("legal entity ALONE", self.out["next_step"])

    def test_a_standalone_supplier_gets_no_next_step(self) -> None:
        out = _net().mp.search_vendors(query="Cobalt", business_unit=BU)
        self.assertTrue(out["vendors"])
        self.assertNotIn("next_step", out)


class PromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from pstb.client.prompt import system_prompt
        cls.text = system_prompt(load_config(str(ROOT / "config.yaml")),
                                 provider="claude")

    def test_the_wide_supplier_question_routes_to_the_network(self) -> None:
        self.assertIn("get_vendor_payables_network", self.text)
        block = self.text[self.text.index("Suppliers work the same way"):]
        self.assertIn("search_vendors to", block[:900])

    def test_the_prompt_forbids_claiming_to_know_the_value(self) -> None:
        block = self.text[self.text.index("IDENTITY LINKS"):]
        self.assertIn("never claim to know the value", block[:600])
        self.assertIn("never a statement that two", block[:900])


if __name__ == "__main__":
    unittest.main()
