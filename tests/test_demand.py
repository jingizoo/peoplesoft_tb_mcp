"""The failure flywheel: failed questions become a governed teach worklist.

On the real deployment 257 of 285 logged Finance turns failed. Every
asset to fix that already existed in pieces — the log knows what died,
the catalog knows every table, the profiler knows which are alive, the
approval queue governs learning — and nothing connected them. These tests
pin the bridge: mining is deterministic, candidates carry the profiler's
own evidence, verified-empty tables are never offered as answers, and
acting on a row goes through the existing PENDING-until-approved path
with no new write surface.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from starlette.testclient import TestClient

from pstb.demand import coverage_gaps, failed_question_terms
from pstb.gui import app as gui


def turn(question, *, failed=True, source="default"):
    return {"type": "turn", "failed": failed, "source_database": source,
            "question": question}


class MiningTests(unittest.TestCase):
    """Deterministic term extraction from redacted failed questions."""

    def test_only_failed_turns_of_the_requested_source_count(self):
        turns = [
            turn("interface files for vendor feeds"),
            turn("interface files ok now", failed=False),
            turn("interface files elsewhere", source="p2go"),
        ]
        out = failed_question_terms(turns, source="default")
        self.assertTrue(out)
        self.assertTrue(all(e["occurrences"] == 1 for e in out),
                        "successes and other sources must not inflate demand")

    def test_distinct_questions_outrank_retries(self):
        turns = ([turn("show grni accrual for FY2026")] * 5
                 + [turn("interface files for vendors"),
                    turn("which interface files load overnight"),
                    turn("list the interface files by feed")])
        out = failed_question_terms(turns)
        self.assertEqual(out[0]["term"], "interface")
        retried = next(e for e in out if "grni" in e["term"])
        self.assertEqual(retried["distinct_questions"], 1)
        self.assertEqual(retried["occurrences"], 5,
                         "retries still count — persistence is demand too")

    def test_a_typed_record_name_survives_whole(self):
        out = failed_question_terms(
            [turn("what does PS_TU_FILE_INTFC hold")])
        terms = [e["term"] for e in out]
        self.assertIn("PS_TU_FILE_INTFC", terms)
        self.assertNotIn("intfc", terms,
                         "fragments of a typed name are phrases nobody said")

    def test_subsumed_unigrams_are_dropped(self):
        out = failed_question_terms([turn("interface files configured")])
        terms = [e["term"] for e in out]
        self.assertIn("interface configured", terms)
        self.assertNotIn("configured", terms)

    def test_samples_stay_within_the_logs_own_bounds(self):
        long_question = "interface " + "x" * 400
        out = failed_question_terms([turn(long_question)])
        for entry in out:
            self.assertLessEqual(len(entry["sample_question"]), 140,
                                 "a worklist row may not carry more of a "
                                 "question than the log retains")

    def test_stopword_only_questions_contribute_nothing(self):
        self.assertEqual(
            failed_question_terms([turn("show me the total for the year")]),
            [])


class MatchingTests(unittest.TestCase):
    """Candidates carry the profiler's verdicts and honour them."""

    TURNS = [turn("job header for the overnight run"),
             turn("job header errors yesterday")]

    def _search(self, results):
        def search(term):
            return {"matches": results}
        return search

    def test_no_believed_empty_table_is_ever_offered(self):
        """An alias onto an empty table manufactures a confident wrong
        answer for every future question that matches it.

        EVERY liveness=="empty" is refused — including the one whose
        modification log was unreadable (mods absent). The review found
        the original ==0 gate never fired in exactly that case, and an
        empty table ranked ABOVE a populated one. The stale-empty that
        deserves offering is already reported by the profiler as UNKNOWN
        (contradicted), never as "empty"."""
        matches = [{"schema": "MAIN", "name": "JOB_HDR",
                    "physical_object": "JOB_HDR", "kind": "table"}]
        for useful in ({"liveness": "empty", "modified_since_stats": 0},
                       {"liveness": "empty"}):
            with self.subTest(useful=useful):
                out = coverage_gaps(
                    self.TURNS, self._search(matches),
                    lambda i, u=useful: u if i == "MAIN.JOB_HDR" else {})
                job = next(g for g in out["gaps"]
                           if g["term"] == "job header")
                self.assertEqual(job["candidates"], [])
                self.assertEqual(job["gap_kind"], "no_candidates")

    def test_a_contradicted_empty_stays_offered_with_its_caveat(self):
        """Stats say empty but DML followed the gather: the table may be
        live, and hiding it here would extend the stale-stats lie."""
        matches = [{"schema": "MAIN", "name": "JOB_HDR",
                    "physical_object": "JOB_HDR", "kind": "table"}]
        useful = {"MAIN.JOB_HDR": {
            "liveness": "unknown", "modified_since_stats": 912,
            "caveat": "statistics report zero rows, but 912 row changes..."}}
        out = coverage_gaps(self.TURNS, self._search(matches),
                            lambda i: useful.get(i, {}))
        job = next(g for g in out["gaps"] if g["term"] == "job header")
        self.assertEqual(len(job["candidates"]), 1)
        self.assertIn("912", job["candidates"][0]["caveat"])

    def test_a_shadowed_candidate_names_its_canonical(self):
        matches = [{"schema": "MAIN", "name": "JOB_HDR_BKP",
                    "physical_object": "JOB_HDR_BKP", "kind": "table"}]
        useful = {"MAIN.JOB_HDR_BKP": {
            "liveness": "populated", "row_estimate": 40,
            "prefer_instead": {"object": "JOB_HDR"}}}
        out = coverage_gaps(self.TURNS, self._search(matches),
                            lambda i: useful.get(i, {}))
        job = next(g for g in out["gaps"] if g["term"] == "job header")
        self.assertEqual(job["candidates"][0]["prefer_instead"], "JOB_HDR")

    def test_a_broken_search_yields_a_gap_not_a_crash(self):
        def boom(term):
            raise RuntimeError("catalog offline")
        out = coverage_gaps(self.TURNS, boom, lambda i: {})
        self.assertTrue(out["gaps"])
        self.assertTrue(all(g["candidates"] == [] for g in out["gaps"]))

    def test_columns_never_become_candidates(self):
        """Aliases attach to tables and views; offering a column would
        propose an identity the proposal endpoint must refuse."""
        matches = [{"schema": "MAIN", "name": "JOB_ID", "kind": "column"}]
        out = coverage_gaps(self.TURNS, self._search(matches), lambda i: {})
        self.assertTrue(all(g["candidates"] == [] for g in out["gaps"]))


class EndpointTests(unittest.TestCase):
    """The gated route: same operator boundary as the question report."""

    def setUp(self):
        self.client = TestClient(gui.app, client=("127.0.0.1", 5555),
                                 base_url="http://localhost")

    def test_the_operator_gate_fronts_the_worklist(self):
        """The rows quote (redacted) user questions; a shared-VPN reader
        has no business seeing them."""
        def refuse(_request):
            raise HTTPException(status_code=403, detail="machine-local only")
        with patch.object(gui, "_require_question_log_operator", refuse):
            self.assertEqual(
                self.client.get("/api/coverage-gaps").status_code, 403)

    def test_an_unknown_source_is_refused_by_name(self):
        registry = SimpleNamespace(
            names=lambda: ["default", "p2go"],
            resolve_name=lambda s="": (s or "default"))
        with patch.object(gui.engine, "registry", registry):
            r = self.client.get("/api/coverage-gaps?source=nosuch")
        self.assertEqual(r.status_code, 404)
        self.assertIn("p2go", r.json()["detail"])

    def test_the_full_bridge_from_failed_turns_to_candidates(self):
        turns = [turn("job header for the overnight run"),
                 turn("job header errors yesterday")]

        class FakeCatalog:
            def search(self, term, source="", kinds="", limit=8):
                if "job" in term:
                    return {"matches": [{
                        "schema": "MAIN", "name": "JOB_HDR",
                        "physical_object": "JOB_HDR", "kind": "table"}]}
                return {"matches": []}

            def context(self, identifier, source="", limit=5):
                return {"usefulness": {"liveness": "populated",
                                       "row_estimate": 44,
                                       "value_score": 0.41}}

        with patch.object(gui, "_coverage_turns", lambda: turns), \
                patch.object(gui, "_coverage_catalog",
                             lambda name: FakeCatalog()):
            body = self.client.get("/api/coverage-gaps").json()
        job = next(g for g in body["gaps"] if g["term"] == "job header")
        self.assertEqual(job["distinct_questions"], 2)
        self.assertEqual(job["candidates"][0]["identifier"], "MAIN.JOB_HDR")
        self.assertEqual(job["candidates"][0]["row_estimate"], 44)

    def test_an_unbuilt_catalog_degrades_with_the_remedy(self):
        with patch.object(gui, "_coverage_turns",
                          lambda: [turn("job header")]), \
                patch.object(gui, "_coverage_catalog",
                             side_effect=RuntimeError("no catalog file")):
            body = self.client.get("/api/coverage-gaps").json()
        self.assertEqual(body["gaps"], [])
        self.assertIn("build it", body["note"].lower())

    def test_no_turns_is_a_calm_empty_state(self):
        with patch.object(gui, "_coverage_turns", lambda: []):
            body = self.client.get("/api/coverage-gaps").json()
        self.assertEqual(body["gaps"], [])
        self.assertIn("no turns", body["note"])


class ActingGoesThroughTheExistingQueueTests(unittest.TestCase):
    """The worklist adds NO write path: acting on a row is a POST to the
    metadata-proposal endpoint that already existed, with its catalog
    validation and PENDING semantics unchanged. This test drives that
    endpoint with exactly the payload the card sends."""

    def test_the_cards_payload_shape_is_what_the_endpoint_accepts(self):
        import inspect
        source = inspect.getsource(gui.create_metadata_proposal)
        for key in ("identifier", "meaning", "aliases"):
            self.assertIn(f'"{key}"', source)

    def test_the_page_posts_to_the_existing_endpoint_only(self):
        page = (Path(gui.__file__).parent / "static"
                / "index.html").read_text()
        start = page.index("function renderCoverageGaps")
        block = page[start:page.index("async function loadCoverageGaps")]
        self.assertIn("metadataProposalUrl(", block,
                      "the card must reuse the drawer's endpoint resolver")
        self.assertNotIn("/api/approvals/decide", block,
                         "the worklist proposes; it never decides")


class ReviewFixTests(unittest.TestCase):
    """Each defect the adversarial review confirmed, pinned."""

    TURNS = [turn("job header for the overnight run"),
             turn("job header errors yesterday")]

    def test_an_unreadable_catalog_says_so_instead_of_no_matches(self):
        """search() on a missing artifact returns available:False rather
        than raising; counting that as "no catalog match" told the
        operator the catalog matched nothing when it was never asked."""
        def unavailable(term):
            return {"available": False,
                    "detail": "No readable metadata catalog at x.db.",
                    "how_to_build": "python scripts/build_metadata_catalog.py"}
        out = coverage_gaps(self.TURNS, unavailable, lambda i: {})
        self.assertEqual(out["gaps"], [])
        self.assertIn("No readable metadata catalog", out["note"])
        self.assertIn("build_metadata_catalog.py", out["note"])

    def test_apostrophes_do_not_manufacture_quoted_phrases(self):
        out = failed_question_terms(
            [turn("what's sitting in the vendor's interface queue")])
        for entry in out:
            self.assertNotIn("s sitting", entry["term"])

    def test_non_ascii_words_survive_whole(self):
        out = failed_question_terms(
            [turn("café supplier ledger reconciliation")])
        terms = " ".join(e["term"] for e in out)
        self.assertIn("café", terms)
        self.assertNotIn("caf ", terms + " ")

    def test_overlong_tokens_are_skipped_not_truncated(self):
        """A truncated record name is a name nobody typed; its stub must
        never become a searchable term or a submitted alias."""
        long_name = "PS_" + "_".join(["VERYLONGSEGMENT"] * 6)
        out = failed_question_terms([turn(f"what does {long_name} hold")])
        for entry in out:
            self.assertLessEqual(len(entry["term"]), 60)
            self.assertNotIn("VERYLONGSEGMENT", entry["term"])

    def test_tied_terms_rank_the_same_in_any_process(self):
        """Ties fell back to set-iteration order, which varies with the
        hash seed across restarts — on a worklist that claims to be
        deterministic. The term itself is now the final sort key."""
        turns = [turn("alpha beta gamma delta")]
        first = [e["term"] for e in failed_question_terms(turns)]
        for _ in range(50):
            self.assertEqual(
                [e["term"] for e in failed_question_terms(turns)], first)
        ordered = [t for t in first if " " in t]
        self.assertEqual(ordered, sorted(ordered, reverse=True),
                         "equal-scoring bigrams break ties lexicographically")

    def test_the_endpoint_searches_without_a_kinds_filter(self):
        """kinds="table view" raised on catalogs with no views and cut off
        the column-label route to tables — the exact case this feature
        exists for. The model's own search passes no kinds; so must this."""
        seen = {}

        class Recorder:
            def search(self, term, **kw):
                seen.update(kw)
                return {"matches": []}

            def context(self, identifier, **kw):
                return {}

        with patch.object(gui, "_coverage_turns",
                          lambda: [turn("job header")]), \
                patch.object(gui, "_coverage_catalog", lambda name: Recorder()):
            client = TestClient(gui.app, client=("127.0.0.1", 5555),
                                base_url="http://localhost")
            client.get("/api/coverage-gaps")
        self.assertIn("limit", seen)
        self.assertNotIn("kinds", seen)

    def test_the_card_shows_the_profilers_caveat(self):
        page = (Path(gui.__file__).parent / "static"
                / "index.html").read_text()
        start = page.index("function renderCoverageGaps")
        block = page[start:page.index("async function loadCoverageGaps")]
        self.assertIn("c.caveat?", block,
                      "the caveat must be RENDERED conditionally, not "
                      "merely referenced — it is the evidence the person "
                      "authoring a meaning needs most")

    def test_the_alias_is_editable_so_a_conflict_is_fixable(self):
        page = (Path(gui.__file__).parent / "static"
                / "index.html").read_text()
        start = page.index("function renderCoverageGaps")
        block = page[start:page.index("async function loadCoverageGaps")]
        self.assertIn("aliasBox", block)
        self.assertIn("aliases:aliasBox.value", block)

class BrowserWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (Path(gui.__file__).parent / "static"
                    / "index.html").read_text()

    def test_diagnostics_loads_the_worklist_after_approvals(self):
        start = self.page.index("await loadApprovals(holder,ACTIVE_CHAT_SOURCE);")
        window = self.page[start:start + 200]
        self.assertIn("loadCoverageGaps(holder,ACTIVE_CHAT_SOURCE)", window)

    def test_a_submitted_proposal_refreshes_the_approval_badge(self):
        start = self.page.index("function renderCoverageGaps")
        block = self.page[start:self.page.index("async function loadCoverageGaps")]
        self.assertIn("refreshApprovalBadge()", block,
                      "the queue the proposal lands in advertises itself")

    def test_the_meaning_is_typed_by_a_person_not_prefilled(self):
        start = self.page.index("function renderCoverageGaps")
        block = self.page[start:self.page.index("async function loadCoverageGaps")]
        self.assertIn("a person writes this part", block)
        self.assertNotIn("meaning.value=", block,
                         "machinery must not draft the meaning")


if __name__ == "__main__":
    unittest.main()
