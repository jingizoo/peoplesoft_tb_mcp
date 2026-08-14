"""The actor graph: customers, products, suppliers, units, and their trade.

Three graphs already existed and none held an ACTOR. This one answers the
questions that live in the edges — which customers buy a product, what
carries the weight, whether two parties are connected at all.

The things easiest to get wrong, and therefore pinned here:

  - IT IS A PRECOMPUTED CROSS-UNIT INDEX, which is the easiest place in this
    codebase to leak. The row filter is on the FLOW, not the actor: a
    customer is not "in" a business unit, its trade is. An actor trading
    only where the caller has no grant must not appear AT ALL, and the
    amounts behind it must not survive either.
  - A RESOLVED ACTOR AND A REFUSAL ARE BOTH DICTS. Testing the type instead
    of the marker returned the actor as the entire answer and silently
    dropped every link — it looked like an actor with no relationships.
  - PARTNERS ARE COUNTED ON A DIFFERENT FLOW THAN THE RANKING. Ranking a
    customer by billing and counting partners on that same flow counts
    business units, which is 1 for nearly everyone and says nothing. The
    finding is "this product has one customer".
  - A PATH IS UNDIRECTED, subsidiary_of IS NOT. Reading a hop in the
    direction it was walked turns "West is a subsidiary of ACME" into the
    opposite claim, stated with equal confidence.
  - AMOUNTS ARE A DATED WEIGHT, never the ledger, and every payload says so.
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

from pstb import entitygraph as eg  # noqa: E402
from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.security import Access, access_scope  # noqa: E402

ASOF = "2026-08-14"
MINE = Access(oprid="FIN_US001", units=frozenset({"US001"}))
OTHER = Access(oprid="FIN_EU001", units=frozenset({"EU001"}))
NOBODY = Access(oprid="NOBODY", units=frozenset())


class _Built(unittest.TestCase):
    """One build over the sample plus an EU001 whose trade must not leak."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="pstb-eg-"))
        dst = cls.tmp / "s.db"
        shutil.copy(ROOT / "sample_data" / "ps_sample.db", dst)
        con = sqlite3.connect(dst)
        con.execute("INSERT INTO PS_BUS_UNIT_TBL_GL VALUES ('EU001','EUR')")
        con.execute("INSERT INTO PS_BUS_UNIT_TBL_FS VALUES "
                    "('EU001','European Ops','DEU')")
        hcols = [r[1] for r in con.execute("PRAGMA table_info(PS_BI_HDR)")]
        row = {"BUSINESS_UNIT": "EU001", "INVOICE": "EU-1",
               "BILL_TO_CUST_ID": "C2001", "BILL_STATUS": "INV",
               "INVOICE_AMOUNT": 777777.0, "INVOICE_DT": "2026-07-01",
               "BI_CURRENCY_CD": "EUR"}
        con.execute(f"INSERT INTO PS_BI_HDR VALUES "
                    f"({','.join('?' * len(hcols))})",
                    [row.get(c) for c in hcols])
        con.execute("INSERT INTO PS_BI_LINE VALUES "
                    "('EU001','EU-1',1,'EU-ONLY-PROD','Europe only',777777.0)")
        ccols = [r[1] for r in con.execute("PRAGMA table_info(PS_CUSTOMER)")]
        crow = {"SETID": "SHARE", "CUST_ID": "C2001",
                "NAME1": "Rheinland GmbH", "STATUS": "A",
                "CORPORATE_SETID": "SHARE", "CORPORATE_CUST_ID": "C2001"}
        con.execute(f"INSERT INTO PS_CUSTOMER VALUES "
                    f"({','.join('?' * len(ccols))})",
                    [crow.get(c) for c in ccols])
        con.commit()
        con.close()
        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(dst)
        cfg.db.use_views = False
        cls.db = Database(cfg)
        engine = TBEngine(cls.db, cfg)
        harvest = eg.harvest_entities(engine, months=24, as_of_date=ASOF)
        cls.path = cls.tmp / "eg.db"
        eg.write_graph(cls.path, harvest)
        cls.g = eg.EntityGraph(cls.path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def linked(out, flow) -> list:
        for grp in out.get("links") or []:
            if grp["flow"] == flow:
                return [i["name"] for i in grp["items"]]
        return []


class BuildTests(_Built):
    def test_every_actor_kind_is_present(self) -> None:
        kinds = {k["kind"] for k in self.g.describe()["actor_kinds"]}
        self.assertEqual(kinds, set(eg.ACTOR_KINDS))

    def test_it_holds_no_identifiers_it_must_not(self) -> None:
        # The vendor network hashes bank and tax keys precisely so they
        # never reach a payload. A derived index must not undo that.
        con = sqlite3.connect(str(self.path))
        try:
            blob = " ".join(str(r[0]) + str(r[1]) for r in
                            con.execute("SELECT name, label FROM actors"))
        finally:
            con.close()
        for leaked in ("456001122", "000123456789", "94-3177001",
                       "Columbus", "Long Beach"):
            self.assertNotIn(leaked, blob)

    def test_a_missing_product_record_is_a_note_not_an_empty_dimension(self):
        class NoLines(Database):
            def columns(self, table):
                return (set() if table.upper() == "PS_BI_LINE"
                        else super().columns(table))

        cfg = Config.sample(ROOT)
        cfg.db.sqlite_path = str(self.tmp / "s.db")
        cfg.db.use_views = False
        db = NoLines(cfg)
        try:
            h = eg.harvest_entities(TBEngine(db, cfg), months=24,
                                    as_of_date=ASOF)
        finally:
            db.close()
        self.assertTrue(any("products are not a dimension" in n
                            for n in h.notes), h.notes)
        self.assertTrue(any(n["kind"] == "customer"
                            for n in h.nodes.values()),
                        "losing products must not lose customers")


class RowSecurityTests(_Built):
    """A precomputed cross-unit index is the easiest place to leak."""

    def test_an_actor_trading_only_elsewhere_does_not_exist_here(self) -> None:
        with access_scope(MINE):
            out = self.g.neighbourhood(entity="Rheinland")
        self.assertEqual(out["scope_status"], "entity_not_found")
        self.assertIn("NO DATA", out["detail"])

    def test_the_amount_does_not_survive_either(self) -> None:
        # Filtering the NAME out is not enough; the figure is the thing that
        # leaks. EU001's invoice is a deliberate 777,777.
        #
        # Not asserted: that no EUR block appears. US001 itself bills two
        # invoices in EUR, so a currency is not evidence of a unit — that
        # assertion failed here first and it was the test that was wrong.
        with access_scope(MINE):
            out = self.g.concentration(kind="customer", limit=50)
        flat = str(out).replace(",", "")
        self.assertNotIn("777777", flat)
        self.assertNotIn("C2001", flat)
        self.assertNotIn("Rheinland", flat)

    def test_the_same_actor_is_visible_to_the_unit_that_trades_with_it(self):
        with access_scope(OTHER):
            out = self.g.neighbourhood(entity="Rheinland")
        self.assertEqual(out["actor"]["kind"], "customer")
        self.assertEqual(out["business_units_covered"], ["EU001"])

    def test_a_narrowed_view_says_it_is_partial(self) -> None:
        with access_scope(MINE):
            out = self.g.concentration(kind="customer")
        self.assertTrue(out["restricted_to_granted_units"])
        self.assertIn("does not appear at all", out["restriction_note"])

    def test_a_grant_of_nothing_answers_nothing(self) -> None:
        with access_scope(NOBODY):
            for out in (self.g.concentration(kind="customer"),
                        self.g.neighbourhood(entity="ACME Industrial"),
                        self.g.connection(source="ACME Industrial",
                                          target="Cascade Foods")):
                self.assertEqual(out["scope_status"], "no_visible_units")

    def test_an_explicit_unit_narrows_and_can_never_widen(self) -> None:
        with access_scope(MINE):
            out = self.g.concentration(kind="customer",
                                       business_unit="EU001")
        self.assertEqual(out["scope_status"], "no_visible_units")

    def test_unrestricted_sees_both_units(self) -> None:
        out = self.g.concentration(kind="customer", limit=50)
        self.assertEqual({b["currency"] for b in out["by_currency"]},
                         {"USD", "EUR"})

    def test_a_connection_cannot_hop_through_an_ungranted_unit(self) -> None:
        # C2001 is reachable from US001 customers only via EU001.
        with access_scope(MINE):
            out = self.g.connection(source="ACME Industrial",
                                    target="Rheinland")
        self.assertEqual(out["scope_status"], "entity_not_found")


class NeighbourhoodTests(_Built):
    def test_a_product_names_the_customers_who_bought_it(self) -> None:
        out = self.g.neighbourhood(entity="LIC-SAAS")
        self.assertEqual(out["actor"]["kind"], "product")
        self.assertTrue(self.linked(out, "buys"),
                        "a resolved actor with no links is the type-check "
                        "bug: a refusal and an actor are both dicts")

    def test_a_customer_names_its_products_and_its_units(self) -> None:
        out = self.g.neighbourhood(entity="ACME Industrial")
        self.assertTrue(self.linked(out, "buys"))
        self.assertIn("US001", self.linked(out, "billed_by"))

    def test_the_corporate_family_comes_from_the_recorded_hierarchy(self):
        out = self.g.neighbourhood(entity="ACME Industrial")
        rels = {f["relation"] for f in out["corporate_family"]}
        self.assertTrue(rels, "ACME heads a family in the sample")
        for f in out["corporate_family"]:
            self.assertIn("CORPORATE", f["evidence"].upper())

    def test_an_ambiguous_name_asks_instead_of_guessing(self) -> None:
        out = self.g.neighbourhood(entity="ACME")
        self.assertEqual(out["scope_status"], "ambiguous_entity")
        self.assertGreater(len(out["multiple_matches"]), 1)

    def test_a_name_nobody_has_is_no_data(self) -> None:
        out = self.g.neighbourhood(entity="Nonesuch Ltd")
        self.assertEqual(out["scope_status"], "entity_not_found")

    def test_it_points_at_the_live_tool(self) -> None:
        out = self.g.neighbourhood(entity="ACME Industrial")
        self.assertIn("get_customer_financial_360", out["next_steps"][0])


class ConcentrationTests(_Built):
    def test_shares_sum_toward_a_hundred_within_a_currency(self) -> None:
        with access_scope(MINE):
            out = self.g.concentration(kind="customer", limit=50)
        block = out["by_currency"][0]
        total = sum(r["share_pct"] for r in block["ranked"])
        self.assertAlmostEqual(total, 100.0, delta=0.5)

    def test_partners_are_counted_on_the_other_flow(self) -> None:
        # Ranking customers by billing and counting partners on the SAME
        # flow counted business units — 1 for everyone, and useless.
        with access_scope(MINE):
            out = self.g.concentration(kind="product", limit=10)
        self.assertEqual(out["partners_are"],
                         "distinct customers who bought it")
        partners = [r["partners"] for r in out["by_currency"][0]["ranked"]]
        self.assertTrue(any(p > 1 for p in partners), partners)

    def test_a_single_partner_actor_is_called_out(self) -> None:
        # A product with exactly one customer is a dependency, not a stat.
        out = self.g.concentration(kind="product", limit=50)
        eur = [b for b in out["by_currency"] if b["currency"] == "EUR"][0]
        self.assertEqual([x["name"] for x in eur["single_partner"]],
                         ["EU-ONLY-PROD"])

    def test_an_unknown_actor_kind_is_refused_with_the_list(self) -> None:
        out = self.g.concentration(kind="employee")
        self.assertEqual(out["scope_status"], "unknown_actor_kind")
        self.assertIn("customer", out["actor_kinds"])

    def test_totals_are_marked_sum_only(self) -> None:
        out = self.g.concentration(kind="customer")
        self.assertTrue(out["by_currency"][0]["sum_only"])


class ConnectionTests(_Built):
    def test_a_hierarchy_hop_reads_in_the_recorded_direction(self) -> None:
        out = self.g.connection(source="ACME Industrial",
                                target="ACME Industrial - West")
        hop = out["path"][0]
        self.assertEqual(hop["strength"], "recorded hierarchy")
        self.assertTrue(hop["traversed_backwards"])
        self.assertTrue(hop["reads"].startswith("ACME Industrial - West"),
                        f"the walk went one way and the FACT the other: "
                        f"{hop['reads']!r}")

    def test_a_shared_unit_is_labelled_as_the_weak_link_it_is(self) -> None:
        out = self.g.connection(source="ACME Industrial",
                                target="Ridgeline Supply Co")
        self.assertTrue(out["connected"])
        self.assertEqual({h["strength"] for h in out["path"]},
                         {"shared transactions"})
        self.assertIn("never a conclusion", out["caution"])

    def test_an_unreachable_pair_does_not_claim_they_are_unrelated(self):
        with access_scope(OTHER):
            out = self.g.connection(source="Rheinland", target="Rheinland")
        self.assertEqual(out["scope_status"], "same_entity")

    def test_both_ends_are_required(self) -> None:
        self.assertEqual(self.g.connection(source="ACME")["scope_status"],
                         "two_entities_required")


class ProvenanceTests(_Built):
    def test_every_answer_is_stamped_with_when_it_was_true(self) -> None:
        for out in (self.g.neighbourhood(entity="LIC-SAAS"),
                    self.g.concentration(kind="customer"),
                    self.g.connection(source="ACME Industrial",
                                      target="Cascade Foods")):
            self.assertEqual(out["as_of"], ASOF)
            self.assertTrue(out["built_at"])
            self.assertIn("NOT the ledger", out["basis"])

    def test_describe_says_amounts_are_a_weight(self) -> None:
        info = self.g.describe()
        self.assertIn("DERIVED WEIGHT", info["amounts_note"])
        self.assertIn("confirm any figure", info["amounts_note"])

    def test_an_unbuilt_graph_names_the_script(self) -> None:
        g = eg.EntityGraph(self.tmp / "nope.db")
        for out in (g.describe(), g.neighbourhood(entity="x"),
                    g.concentration(), g.connection(source="a", target="b")):
            self.assertFalse(out["available"])
            self.assertIn("build_entity_graph", out["how_to_build"])


class WriteTests(unittest.TestCase):
    def test_a_rebuild_leaves_no_scratch_file(self) -> None:
        d = Path(tempfile.mkdtemp())
        h = eg.Harvest("t")
        h.node("customer", "C1")
        eg.write_graph(d / "eg.db", h)
        eg.write_graph(d / "eg.db", h)
        self.assertTrue((d / "eg.db").exists())
        self.assertFalse((d / "eg.db.building").exists())


if __name__ == "__main__":
    unittest.main()
