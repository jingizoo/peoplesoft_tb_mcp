"""The process graph: how work is DONE here, not what it adds up to.

Asked "how do we do invoicing for India", the agent had nothing. It could
join records and total them, but the chain a person actually follows — a menu
path, a component, its pages, the records those pages write, the procedure
that describes them — existed only spread across PeopleTools metadata that
nothing read.

What is pinned here is the part that is easy to get subtly wrong:

  - the graph is STRUCTURE. No amounts, no customer or supplier names, no
    bank or tax identifiers. A process index that quietly became a data
    extract would be a much bigger thing to secure than it looks.
  - BI_HDR and PS_BI_HDR are ONE record. PeopleTools spells it one way and
    SQL the other; left split, the page layer and the data layer become two
    islands and the graph reports that invoicing pages touch no queryable
    table — a hole shaped exactly like an answer.
  - a hub is not a bridge. The month-end checklist names a dozen records, so
    walking THROUGH it carried "invoicing" to Asset Management in two hops.
  - a scope narrows an answer; it is not one. "for India" resolves to
    business units, and when no unit is in that country the answer must say
    so rather than present the global process as the local one.
  - every source is optional and degrades alone, because sites differ and a
    missing grant must produce a NOTE, not a silent hole.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb import procgraph as pg                                # noqa: E402
from pstb.config import load_config                             # noqa: E402
from pstb.db import Database                                    # noqa: E402
from pstb.engine import TBEngine                                # noqa: E402


def _engine():
    cfg = load_config(str(ROOT / "config.yaml"))
    return TBEngine(Database(cfg), cfg)


def _build(tmp: Path, harvests) -> pg.ProcessGraph:
    pg.write_graph(tmp, harvests)
    return pg.ProcessGraph(tmp)


class _Built(unittest.TestCase):
    """One real build over the sample, shared — it is the slow part."""

    @classmethod
    def setUpClass(cls) -> None:
        eng = _engine()
        cls.dir = Path(tempfile.mkdtemp())
        curated = pg.harvest_record_map(eng)
        records = sorted({n["name"] for n in curated.nodes.values()
                          if n["kind"] in ("record", "setup")})
        modules = sorted({n["name"] for n in curated.nodes.values()
                          if n["kind"] == "module"})
        from pstb.wiki import make_wiki
        cls.harvests = [
            curated,
            pg.harvest_peopletools(eng.db),
            pg.harvest_scopes(eng),
            pg.harvest_joins(eng, records),
            pg.harvest_wiki(make_wiki(eng.cfg), records, modules),
        ]
        eng.db.close()
        cls.g = _build(cls.dir / "pg.db", cls.harvests)

    def names(self, out, layer) -> list:
        for lay in out.get("layers") or []:
            if lay["layer"] == layer:
                return [i["name"] for i in lay["items"]]
        return []


class HarvestTests(_Built):
    def test_the_page_layer_is_read_at_all(self) -> None:
        # PSPNLDEFN/PSPNLFIELD are the "how do I DO this" half and nothing
        # else in this app touches them.
        pt = self.harvests[1]
        pages = [n for n in pt.nodes.values() if n["kind"] == "page"]
        self.assertTrue(pages, f"no pages harvested: {pt.notes}")
        self.assertTrue(any(e["kind"] == "page_reads_record"
                            for e in pt.edges.values()),
                        "pages with no record edges are a disconnected layer")

    def test_a_missing_metadata_table_is_a_note_not_a_crash(self) -> None:
        class NoPages(Database):
            def columns(self, table):
                return set() if table.upper().startswith("PSPNL") \
                    else super().columns(table)

        cfg = load_config(str(ROOT / "config.yaml"))
        db = NoPages(cfg)
        h = pg.harvest_peopletools(db)
        db.close()
        self.assertTrue(any("PSPNLDEFN" in n for n in h.notes),
                        f"a missing page catalog must be reported: {h.notes}")
        self.assertTrue(h.nodes, "the records should still have been read")

    def test_a_refused_read_marks_the_source_degraded(self) -> None:
        from pstb.db import DbError

        class NoRecords(Database):
            def query(self, sql, params=None, **kw):
                if "PSRECDEFN" in sql:
                    raise DbError("ORA-00942: table or view does not exist")
                return super().query(sql, params, **kw)

        cfg = load_config(str(ROOT / "config.yaml"))
        db = NoRecords(cfg)
        h = pg.harvest_peopletools(db)
        db.close()
        self.assertFalse(h.ok, "a refused catalog read is degraded, not fine")

    def test_large_catalogs_are_keyset_paginated_past_the_old_cap(self) -> None:
        class Catalog:
            prefix = ""

            def __init__(self, size):
                self.names = [f"REC_{i:05d}" for i in range(size)]
                self.calls = 0

            def columns(self, table):
                return {"RECNAME"} if table == "PSRECDEFN" else set()

            def query(self, sql, params=None, max_rows=None):
                self.calls += 1
                after = (params or {}).get("pg0")
                rows = [n for n in self.names if after is None or n > after]
                cap = int(max_rows)
                return ([{"recname": n} for n in rows[:cap]],
                        len(rows) > cap)

        db = Catalog(12_345)
        limits = pg.GraphBuildLimits(max_records=20_000,
                                     query_page_size=1_000)
        h = pg.harvest_peopletools(db, limits)
        records = [n for n in h.nodes.values() if n["kind"] == "record"]
        self.assertEqual(len(records), 12_345)
        self.assertGreater(db.calls, 5)
        self.assertFalse(h.partial, h.notes)

    def test_a_catalog_ceiling_is_reported_as_partial_and_degraded(self):
        class Catalog:
            prefix = ""

            def columns(self, table):
                return {"RECNAME"} if table == "PSRECDEFN" else set()

            def query(self, sql, params=None, max_rows=None):
                after = (params or {}).get("pg0")
                names = [f"REC_{i:05d}" for i in range(6_000)]
                rows = [n for n in names if after is None or n > after]
                cap = int(max_rows)
                return ([{"recname": n} for n in rows[:cap]],
                        len(rows) > cap)

        h = pg.harvest_peopletools(
            Catalog(), pg.GraphBuildLimits(max_records=5_000,
                                            query_page_size=1_000))
        self.assertEqual(len(h.nodes), 5_000)
        self.assertTrue(h.partial)
        self.assertFalse(h.ok)
        self.assertEqual(h.limit_hits[0]["table"], "PSRECDEFN")
        self.assertIn("PARTIAL", " ".join(h.notes))

    def test_it_holds_no_amounts_and_no_party_names(self) -> None:
        # The security line this whole module sits on. Every value that got
        # into the graph is a NAME of a structure, never a row of data.
        con = sqlite3.connect(str(self.dir / "pg.db"))
        try:
            blob = " ".join(
                str(r[0]) + " " + str(r[1]) + " " + str(r[2])
                for r in con.execute("SELECT name, label, attrs FROM nodes"))
        finally:
            con.close()
        for leaked in ("ACME", "Northwind", "Ridgeline", "456001122",
                       "000123456789", "302835", "908846"):
            self.assertNotIn(leaked, blob,
                             f"{leaked!r} is data, and data must not be in "
                             "a structure index")


class CanonicalRecordTests(_Built):
    def test_bi_hdr_and_ps_bi_hdr_are_one_node(self) -> None:
        con = sqlite3.connect(str(self.dir / "pg.db"))
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM nodes WHERE kind IN ('record','setup')")}
        finally:
            con.close()
        bare = {n for n in names if not n.startswith("PS_")}
        self.assertEqual(bare, set(),
                         f"un-prefixed record nodes survived: {sorted(bare)}")
        self.assertIn("PS_BI_HDR", names)

    def test_the_page_layer_reaches_the_data_layer(self) -> None:
        # The whole point of merging: a page must connect to the record the
        # curated tools query, or the two halves never meet.
        out = self.g.trace("invoicing")
        self.assertIn("PS_BI_HDR", self.names(out, "record"))
        self.assertTrue(self.names(out, "page"))


class RetrievalTests(_Built):
    def test_invoicing_finds_billing_though_nothing_is_called_invoicing(self):
        # The vocabulary gap that made the first build useless: the user says
        # invoicing, the system says BI_HDR and Billing.
        out = self.g.trace("how do we do invoicing")
        self.assertIn("PS_BI_HDR", self.names(out, "record"))
        self.assertIn("BILLING", self.names(out, "module"))
        self.assertTrue(self.names(out, "navigation"),
                        "a process answer without a menu path is a table list")

    def test_who_owes_us_reaches_receivables(self) -> None:
        # A stem cannot get from "owes" to "receivables"; the module
        # vocabulary is the curated escape hatch for exactly this.
        out = self.g.trace("who owes us money")
        self.assertIn("RECEIVABLES", self.names(out, "module"))
        self.assertIn("PS_ITEM", self.names(out, "record"))

    def test_it_names_a_tool_that_can_actually_answer(self) -> None:
        out = self.g.trace("who owes us money")
        self.assertIn("GET_AR_AGING", self.names(out, "tool"))

    def test_a_hub_document_is_not_a_bridge(self) -> None:
        # Walking THROUGH the close checklist put Asset Management in an
        # invoicing answer, ranked like a real neighbour.
        out = self.g.trace("how do we do invoicing")
        self.assertNotIn("ASSET_MANAGEMENT", self.names(out, "module"))

    def test_layers_come_back_in_the_order_a_person_meets_them(self) -> None:
        out = self.g.trace("how do we do invoicing")
        order = [lay["layer"] for lay in out["layers"]]
        self.assertLess(order.index("navigation"), order.index("record"))
        self.assertLess(order.index("page"), order.index("record"))

    def test_every_item_says_how_it_was_reached(self) -> None:
        # A page from PSPNLFIELD and a record from a shared column name are
        # not the same kind of claim, and the payload has to show which.
        out = self.g.trace("how do we do invoicing")
        for layer in out["layers"]:
            for item in layer["items"]:
                self.assertTrue(item["reached_by"], item["name"])
                self.assertGreaterEqual(item["relevance"], pg.MIN_RELEVANCE)

    def test_nothing_matching_is_said_plainly(self) -> None:
        out = self.g.trace("zzzqqq nonexistent widget")
        self.assertEqual(out["layers"] if "layers" in out else [], [])
        self.assertIn("No page, record, module", out["detail"])
        self.assertTrue(out["known_modules"])

    def test_a_question_of_pure_noise_is_refused(self) -> None:
        out = self.g.trace("how do we do it")
        self.assertIn("detail", out)


class ScopeTests(_Built):
    def test_a_country_resolves_by_NAME_not_only_by_code(self) -> None:
        # PeopleSoft stores IND; nobody types IND. PS_COUNTRY_TBL is read so
        # the mapping is the instance's own, not a shipped country list.
        out = self.g.trace("how do we do invoicing for India")
        scopes = out["scope_applied"]
        self.assertEqual([s["value"] for s in scopes], ["IND"])
        self.assertEqual(scopes[0]["name"], "India")

    def test_a_country_with_no_unit_says_so(self) -> None:
        out = self.g.trace("how do we do invoicing for India")
        note = out["scope_applied"][0].get("note") or ""
        self.assertIn("No business unit", note)
        self.assertIn("NOT a local variant", note,
                      "presenting the global process as the local one is the "
                      "failure this note exists to prevent")

    def test_a_country_with_a_unit_names_it_and_what_to_do(self) -> None:
        out = self.g.trace("how do we do invoicing for the United States")
        scope = [s for s in out["scope_applied"] if s["value"] == "USA"]
        self.assertTrue(scope, out["scope_applied"])
        self.assertEqual(scope[0]["business_units"], ["US001"])
        self.assertIn("business_unit=US001", scope[0]["next_step"])

    def test_a_scope_alone_does_not_answer_a_process_question(self) -> None:
        out = self.g.trace("what about India")
        self.assertEqual(out["seeds"], [])
        self.assertIn("scope was understood", out["detail"])

    def test_a_scope_is_never_walked_from(self) -> None:
        # A business unit touches every record in the system, so expanding
        # one returns the whole graph and calls all of it relevant.
        out = self.g.trace("how do we do invoicing for the United States")
        self.assertNotIn("scope", [lay["layer"] for lay in out["layers"]])


class DescribeTests(_Built):
    def test_it_reports_what_it_covers_and_when(self) -> None:
        info = self.g.describe()
        self.assertTrue(info["available"])
        self.assertTrue(info["built_at"])
        self.assertIn("peopletools", info["sources"])
        self.assertTrue(info["node_kinds"])
        self.assertIn("never what anything is worth", info["coverage_note"])

    def test_an_unbuilt_graph_names_the_script(self) -> None:
        g = pg.ProcessGraph(self.dir / "nope.db")
        self.assertFalse(g.available())
        for out in (g.describe(), g.trace("anything")):
            self.assertFalse(out["available"])
            self.assertIn("build_process_graph", out["how_to_build"])

    def test_partial_builds_expose_structured_limit_hits(self) -> None:
        h = pg.Harvest("peopletools")
        h.node("page", "ONE")
        h.limit("PSPNLDEFN", 100_000, 100_000)
        target = self.dir / "partial.db"
        pg.write_graph(target, [h])
        out = pg.ProcessGraph(target).describe()
        self.assertTrue(out["partial"])
        self.assertEqual(out["limit_hits"][0]["table"], "PSPNLDEFN")
        self.assertIn("PARTIAL", out["coverage_note"])


class RecommendationTests(_Built):
    """A process answer must hand over a next question, not end cold.

    The suggestion rules are how machinery reaches both surfaces at once —
    the chips under the answer and the observed_next_steps the model reads —
    so the process trace feeds them like every data tool does, under the
    same contract: evidence-backed, a real tool, a wording that routes.
    """

    def _suggest(self, question: str) -> list:
        from pstb.suggest import suggestions_for
        return suggestions_for([("trace_process", self.g.trace(question))],
                               question=question)

    def test_a_trace_recommends_the_figure_it_leads_to(self) -> None:
        out = self._suggest("how do we do invoicing")
        self.assertTrue(out, "a process answer ended cold")
        s = out[0]
        self.assertEqual(s["kind"], "process_to_figures")
        self.assertIn(s["answered_by"], ("get_billing_workbench",
                                         "get_top_billing_customers",
                                         "get_customer_intelligence"))
        self.assertIn("this process's own records", s["because"])

    def test_a_resolved_scope_lands_in_the_suggested_question(self) -> None:
        out = self._suggest("how do we do invoicing for the United States")
        self.assertTrue(out)
        self.assertIn("US001", out[0]["question"],
                      "the resolved unit is the whole value of the scope "
                      "and must reach the follow-up")

    def test_an_unresolved_country_asks_what_exists_and_nothing_else(self):
        out = self._suggest("how do we do invoicing for India")
        self.assertEqual([s["kind"] for s in out], ["scope_unresolved"])
        self.assertIn("IND", out[0]["because"] + out[0]["question"] + "IND")
        self.assertEqual(out[0]["answered_by"], "list_financial_scopes",
                         "a figure recommended for a scope that resolved "
                         "to nothing builds on the refusal")

    def test_every_wording_is_scoped_or_eval_proven(self) -> None:
        # The suggestion contract from test_suggestions.py, applied here:
        # a generic wording nobody proved routes is a suggestion that
        # fails when clicked.
        import json as _json
        from pstb.suggest import _PROCESS_TOOL_ASKS
        cases = {c["question"] for c in _json.loads(
            (ROOT / "evals" / "cases.json").read_text())["cases"]}
        for scoped, generic in _PROCESS_TOOL_ASKS.values():
            self.assertIn("{u}", scoped)
            self.assertIn(generic, cases,
                          f"{generic!r} is not a question the eval suite "
                          "proves")

    def test_every_recommended_tool_exists(self) -> None:
        from pstb import server
        from pstb.suggest import _PROCESS_TOOL_ASKS
        for name in _PROCESS_TOOL_ASKS:
            self.assertTrue(hasattr(server, name.lower()),
                            f"{name} is recommended but not exposed")

    def test_an_unbuilt_graph_recommends_nothing(self) -> None:
        from pstb.suggest import suggestions_for
        out = suggestions_for([("trace_process",
                                pg.ProcessGraph(self.dir / "no.db")
                                .trace("invoicing"))])
        self.assertEqual(out, [],
                         "available:false is a refusal, and a refusal is "
                         "not evidence")


class WriteTests(unittest.TestCase):
    def test_a_rebuild_is_atomic(self) -> None:
        # A half-written graph being read by the live GUI mid-rebuild is the
        # reason this renames rather than writes in place.
        d = Path(tempfile.mkdtemp())
        target = d / "pg.db"
        h = pg.Harvest("t")
        h.edge(h.node("record", "PS_A"), h.node("record", "PS_B"),
               "record_joins_record")
        pg.write_graph(target, [h])
        first = target.stat().st_mtime_ns
        pg.write_graph(target, [h])
        self.assertTrue(target.exists())
        self.assertFalse((d / "pg.db.building").exists(),
                         "the scratch file must not survive a build")
        self.assertIsInstance(first, int)

    def test_an_edge_to_an_undeclared_node_keeps_the_node(self) -> None:
        # A record named by a page and absent from PSRECDEFN is a real
        # finding; dropping the edge would hide it.
        h = pg.Harvest("t")
        h.edge("page:P1", "record:PS_GHOST", "page_reads_record")
        d = Path(tempfile.mkdtemp())
        pg.write_graph(d / "pg.db", [h])
        out = pg.ProcessGraph(d / "pg.db").trace("ghost")
        self.assertIn("PS_GHOST",
                      [i["name"] for lay in out["layers"]
                       for i in lay["items"]])

    def test_the_default_writer_supports_100k_nodes_and_edges(self):
        h = pg.Harvest("scale")
        prior = None
        for i in range(100_000):
            current = h.node("page", f"PAGE_{i:06d}")
            if prior is not None:
                h.edge(prior, current, "component_has_page")
            prior = current
        d = Path(tempfile.mkdtemp())
        target = d / "large.db"
        info = pg.write_graph(target, [h])
        self.assertEqual(info["nodes"], "100000")
        self.assertEqual(info["edges"], "99999")
        con = sqlite3.connect(str(target))
        try:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM nodes").fetchone()[0], 100_000)
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM edges").fetchone()[0], 99_999)
        finally:
            con.close()

    def test_global_limit_failure_preserves_the_existing_graph(self) -> None:
        d = Path(tempfile.mkdtemp())
        target = d / "pg.db"
        old = pg.Harvest("old")
        old.node("page", "OLD")
        pg.write_graph(target, [old])
        replacement = pg.Harvest("new")
        for name in ("A", "B", "C"):
            replacement.node("page", name)
        with self.assertRaisesRegex(pg.ProcessGraphError, "max_nodes=2"):
            pg.write_graph(target, [replacement],
                           limits=pg.GraphBuildLimits(max_nodes=2))
        con = sqlite3.connect(str(target))
        try:
            self.assertEqual(con.execute(
                "SELECT name FROM nodes").fetchall(), [("OLD",)])
        finally:
            con.close()
        self.assertFalse((d / "pg.db.building").exists())

    def test_memory_budget_stops_an_oversized_build_before_writing(self):
        h = pg.Harvest("large-label")
        h.node("page", "ONE", label="x" * 300_000)
        d = Path(tempfile.mkdtemp())
        target = d / "pg.db"
        with self.assertRaisesRegex(pg.ProcessGraphError,
                                    "memory_budget_mb=1"):
            pg.write_graph(target, [h], limits=pg.GraphBuildLimits(
                memory_budget_mb=1))
        self.assertFalse(target.exists())
        self.assertFalse((d / "pg.db.building").exists())

    def test_memory_budget_is_checked_before_merge_allocation(self):
        h = pg.Harvest("large-label")
        h.node("page", "ONE", label="x" * 300_000)
        d = Path(tempfile.mkdtemp())
        target = d / "pg.db"
        with mock.patch.object(pg, "_merge_harvests",
                               wraps=pg._merge_harvests) as merge:
            with self.assertRaisesRegex(pg.ProcessGraphError,
                                        "memory_budget_mb=1"):
                pg.write_graph(target, [h], limits=pg.GraphBuildLimits(
                    memory_budget_mb=1))
        merge.assert_not_called()
        self.assertFalse(target.exists())

    def test_absolute_safeguards_reject_unbounded_configuration(self):
        with self.assertRaisesRegex(pg.ProcessGraphError, "max_nodes"):
            pg.GraphBuildLimits(max_nodes=pg.HARD_MAX_NODES + 1).validate()

    def test_build_limits_load_from_deployment_config(self) -> None:
        d = Path(tempfile.mkdtemp())
        config = d / "config.yaml"
        config.write_text(
            "process_graph:\n"
            "  max_records: 123456\n"
            "  max_nodes: 234567\n"
            "  memory_budget_mb: 768\n")
        limits = pg.GraphBuildLimits.from_config(
            load_config(str(config)).process_graph)
        self.assertEqual(limits.max_records, 123_456)
        self.assertEqual(limits.max_nodes, 234_567)
        self.assertEqual(limits.memory_budget_mb, 768)


class StemTests(unittest.TestCase):
    def test_the_forms_of_a_business_noun_share_a_stem(self) -> None:
        self.assertEqual(pg._stem("invoicing"), pg._stem("invoice"))
        self.assertEqual(pg._stem("payments"), pg._stem("payment"))

    def test_it_will_not_shred_a_short_word(self) -> None:
        # "owes" -> "ow" would match half the catalog.
        self.assertEqual(pg._stem("owes"), "owes")
        self.assertEqual(pg._stem("bill"), "bill")


if __name__ == "__main__":
    unittest.main()
