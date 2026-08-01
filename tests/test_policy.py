"""Policy figures from the wiki, bound into queries instead of retyped.

The failure this guards against is quiet. "Show me assets above the
capitalization threshold" is answered with a number the model supplies; if it
recalls $10,000 where the policy says $5,000, the row set is wrong and looks
exactly as authoritative as the right one. Nothing errors, nothing is flagged,
and the user has no way to tell.

So the value is extracted mechanically, travels as a bind, and every path that
cannot produce a trustworthy figure REFUSES rather than falling back.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import EngineError, TBEngine  # noqa: E402
from pstb.policy import (POLICY_TERMS, PolicyError, PolicyResolver,  # noqa: E402
                         extract_candidates)
from pstb.wiki import make_wiki  # noqa: E402

CURRENT = """# Fixed Asset Policy

The capitalization threshold is $10,000. Equipment at or above this amount is
capitalized and depreciated over its useful life; below it, the cost is
expensed in the period incurred.

Useful life for computer equipment is 3 years and for furniture 7 years.
Depreciation begins in the month the asset is placed in service.
"""

STALE = """# Capital Asset Guidance (superseded FY2024)

The capitalization threshold is $5,000 for all equipment purchases.
"""


class ExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.term = POLICY_TERMS["capitalization_threshold"]

    def test_the_figure_comes_out_with_its_own_sentence(self) -> None:
        got = extract_candidates(CURRENT, self.term)
        self.assertTrue(got)
        self.assertEqual(got[0]["value"], 10000.0)
        self.assertEqual(got[0]["unit"], "USD")
        self.assertIn("capitalization threshold", got[0]["quote"].lower())

    def test_unrelated_numbers_on_the_same_page_are_not_picked_up(self) -> None:
        # "3 years", "7 years" and the depreciation sentence share the page
        # with the threshold. Grabbing the first number on a policy page is
        # how you end up filtering assets at $3.
        values = [c["value"] for c in extract_candidates(CURRENT, self.term)]
        self.assertEqual(values, [10000.0])

    def test_shorthand_and_currency_words_are_understood(self) -> None:
        for text, expect in [
            ("The capitalization threshold is $10k.", 10000.0),
            ("Capitalization threshold: USD 2,500", 2500.0),
            ("Fixed asset capitalization threshold of 1.5 million USD", 1_500_000.0),
            ("Capitalize fixed asset purchases at or above €7,500", 7500.0),
        ]:
            got = extract_candidates(text, self.term)
            self.assertTrue(got, f"nothing extracted from {text!r}")
            self.assertEqual(got[0]["value"], expect, text)

    def test_day_based_policies_read_net_terms(self) -> None:
        term = POLICY_TERMS["invoice_payment_terms_days"]
        got = extract_candidates("Standard customer payment terms are Net 45.",
                                 term)
        self.assertEqual([c["value"] for c in got], [45.0])


class _WikiCase(unittest.TestCase):
    PAGES: dict = {}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pstb-policy-"))
        for name, body in self.PAGES.items():
            (self.tmp / name).write_text(body)
        cfg = Config.sample(ROOT)
        cfg.wiki.provider = "localdocs"
        cfg.wiki.localdocs_path = str(self.tmp)
        self.cfg = cfg
        self.wiki = make_wiki(cfg)
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)
        self.engine.attach_policy(self.wiki, None)

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class ResolutionTests(_WikiCase):
    PAGES = {"fixed-asset-policy.md": CURRENT}

    def test_a_single_clear_figure_resolves_with_provenance(self) -> None:
        got = PolicyResolver(self.wiki, None).resolve("capitalization_threshold")
        self.assertEqual(got["status"], "resolved")
        self.assertEqual(got["value"], 10000.0)
        self.assertTrue(got["usable_as_filter"])
        # The receipt is the point: value without source is just a guess with
        # better manners.
        self.assertIn("$10,000", got["quote"])
        self.assertTrue(got["source"]["title"])

    def test_a_policy_the_wiki_does_not_cover_is_not_invented(self) -> None:
        got = PolicyResolver(self.wiki, None).resolve("materiality_threshold")
        self.assertEqual(got["status"], "not_found")
        self.assertIsNone(got.get("value"))
        self.assertFalse(got["usable_as_filter"])
        self.assertIn("do not supply one", got["note"].lower())

    def test_an_unknown_policy_name_lists_what_is_available(self) -> None:
        with self.assertRaises(PolicyError) as ctx:
            PolicyResolver(self.wiki, None).resolve("vacation_accrual_rate")
        self.assertIn("capitalization_threshold", str(ctx.exception))

    def test_no_wiki_configured_refuses_rather_than_recalling(self) -> None:
        got = PolicyResolver(None, None).resolve("capitalization_threshold")
        self.assertEqual(got["status"], "no_wiki")
        self.assertFalse(got["usable_as_filter"])


class AmbiguityTests(_WikiCase):
    """A stale page nobody retired looks identical to a current one."""

    PAGES = {"fixed-asset-policy.md": CURRENT, "old-asset-guidance.md": STALE}

    def test_conflicting_figures_refuse_to_pick_one(self) -> None:
        got = PolicyResolver(self.wiki, None).resolve("capitalization_threshold")
        self.assertEqual(got["status"], "ambiguous")
        self.assertEqual(sorted(got["values_found"]), [5000.0, 10000.0])
        self.assertFalse(got["usable_as_filter"])
        self.assertIsNone(got.get("value"))

    def test_both_candidates_keep_their_sources(self) -> None:
        got = PolicyResolver(self.wiki, None).resolve("capitalization_threshold")
        titles = {c["source"]["title"] for c in got["candidates"]}
        self.assertGreaterEqual(len(titles), 2,
                                "the user cannot choose without knowing which "
                                "page each figure came from")

    def test_an_ambiguous_policy_cannot_filter_a_query(self) -> None:
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql(
                "SELECT 1 AS x FROM PS_JRNL_LN WHERE MONETARY_AMOUNT >= :t",
                policy_binds={"t": "capitalization_threshold"})
        self.assertIn("NOT run", str(ctx.exception))


class BoundQueryTests(_WikiCase):
    PAGES = {"fixed-asset-policy.md": CURRENT}

    def test_the_value_is_bound_not_typed(self) -> None:
        out = self.engine.run_sql(
            "SELECT COUNT(*) AS n FROM PS_JRNL_LN "
            "WHERE MONETARY_AMOUNT >= :threshold",
            policy_binds={"threshold": "capitalization_threshold"})
        self.assertIn("policy_basis", out)
        basis = out["policy_basis"][0]
        self.assertEqual(basis["value"], 10000.0)
        self.assertEqual(basis["bind"], "threshold")
        self.assertIn("$10,000", basis["quote"])

    def test_the_filter_actually_changed_the_row_set(self) -> None:
        # Proving the bind reached the database, not just the response.
        filtered = self.engine.run_sql(
            "SELECT COUNT(*) AS n FROM PS_JRNL_LN "
            "WHERE MONETARY_AMOUNT >= :threshold",
            policy_binds={"threshold": "capitalization_threshold"})["rows"][0]["n"]
        everything = self.engine.run_sql(
            "SELECT COUNT(*) AS n FROM PS_JRNL_LN")["rows"][0]["n"]
        self.assertLess(filtered, everything)
        self.assertGreater(filtered, 0)

    def test_naming_a_policy_the_sql_ignores_is_refused(self) -> None:
        # Otherwise the model asks for a threshold, forgets the WHERE clause,
        # and reports the unfiltered count as "assets above the threshold".
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql(
                "SELECT COUNT(*) AS n FROM PS_JRNL_LN",
                policy_binds={"threshold": "capitalization_threshold"})
        self.assertIn(":threshold", str(ctx.exception))

    def test_ordinary_queries_are_unaffected(self) -> None:
        out = self.engine.run_sql("SELECT COUNT(*) AS n FROM PS_JRNL_LN")
        self.assertNotIn("policy_basis", out)
        self.assertTrue(out["rows"])


class LookupCacheTests(unittest.TestCase):
    """The lookup cache must key on the CORPUS, not the provider type."""

    def test_two_corpora_do_not_share_cached_answers(self) -> None:
        from pstb import wiki as wiki_mod

        wiki_mod.clear_lookup_cache()
        made = []
        for body in (CURRENT, STALE):
            d = Path(tempfile.mkdtemp(prefix="pstb-corpus-"))
            (d / "policy.md").write_text(body)
            cfg = Config.sample(ROOT)
            cfg.wiki.provider = "localdocs"
            cfg.wiki.localdocs_path = str(d)
            made.append((d, make_wiki(cfg)))
        try:
            q = "What is our capitalization threshold?"
            first = wiki_mod.lookup(made[0][1], q)
            second = wiki_mod.lookup(made[1][1], q)
            t1 = {s.get("title") for s in first.get("sources") or []}
            t2 = {s.get("title") for s in second.get("sources") or []}
            self.assertTrue(first.get("passages") and second.get("passages"))
            # Both corpora answer the same question from DIFFERENT pages, so a
            # shared cache entry shows up as identical passage text.
            self.assertNotEqual(
                first["passages"][0]["text"], second["passages"][0]["text"],
                f"one corpus was served the other's cached passages "
                f"({t1} vs {t2})")
        finally:
            for d, _ in made:
                shutil.rmtree(d, ignore_errors=True)

    def test_repeat_lookups_do_not_re_search(self) -> None:
        from pstb import wiki as wiki_mod

        wiki_mod.clear_lookup_cache()
        d = Path(tempfile.mkdtemp(prefix="pstb-cache-"))
        (d / "policy.md").write_text(CURRENT)
        cfg = Config.sample(ROOT)
        cfg.wiki.provider = "localdocs"
        cfg.wiki.localdocs_path = str(d)
        w = make_wiki(cfg)
        hits = {"n": 0}
        original = w.search

        def counted(query, limit=8):
            hits["n"] += 1
            return original(query, limit)

        w.search = counted
        try:
            for _ in range(4):
                wiki_mod.lookup(w, "What is our capitalization threshold?")
            self.assertEqual(hits["n"], 1,
                             "each repeat lookup hit the wiki again — over "
                             "Confluence that is an HTTP round trip per call")
        finally:
            shutil.rmtree(d, ignore_errors=True)


class RelevanceTests(_WikiCase):
    """Retrieval finding something is not retrieval finding the answer."""

    PAGES = {"fixed-asset-policy.md": CURRENT}

    def test_an_uncovered_subject_is_marked_weak(self) -> None:
        from pstb import wiki as wiki_mod

        out = wiki_mod.lookup(
            self.wiki, "What is our cryptocurrency treasury holding policy?")
        self.assertEqual(out["relevance"], "weak")
        self.assertIn("relevance_note", out)
        self.assertIn("does not cover", out["relevance_note"])

    def test_a_covered_subject_is_marked_strong(self) -> None:
        from pstb import wiki as wiki_mod

        out = wiki_mod.lookup(self.wiki, "capitalization threshold")
        self.assertEqual(out["relevance"], "strong")
        self.assertNotIn("relevance_note", out)


class ResolutionCacheTests(_WikiCase):
    """Resolution is cached, INCLUDING the negative answers.

    A policy the wiki does not carry is the most expensive lookup there is —
    it exhausts every search phrasing and fetches every candidate page before
    concluding nothing. Repeating that on each ask is the worst case, not an
    edge case: "we do not have that figure" is exactly the answer a user
    probes at at few times in a row.
    """

    PAGES = {"fixed-asset-policy.md": CURRENT}

    def _counted(self):
        from pstb import wiki as wiki_mod

        wiki_mod.clear_lookup_cache()
        hits = {"n": 0}
        s, g = self.wiki.search, self.wiki.get_page
        self.wiki.search = lambda q, limit=8: (
            hits.__setitem__("n", hits["n"] + 1) or s(q, limit))
        self.wiki.get_page = lambda i: (
            hits.__setitem__("n", hits["n"] + 1) or g(i))
        return PolicyResolver(self.wiki, None), hits

    def test_a_resolved_policy_is_not_looked_up_twice(self) -> None:
        r, hits = self._counted()
        r.resolve("capitalization_threshold")
        cold = hits["n"]
        self.assertGreater(cold, 0)
        hits["n"] = 0
        for _ in range(5):
            r.resolve("capitalization_threshold")
        self.assertEqual(hits["n"], 0,
                         "each repeat re-read the wiki; over Confluence that "
                         "is an HTTP round trip per call")

    def test_a_missing_policy_is_not_re_searched(self) -> None:
        r, hits = self._counted()
        r.resolve("materiality_threshold")
        hits["n"] = 0
        for _ in range(5):
            got = r.resolve("materiality_threshold")
        self.assertEqual(hits["n"], 0)
        self.assertEqual(got["status"], "not_found")

    def test_the_cached_answer_is_a_copy(self) -> None:
        # A caller mutating a result must not corrupt what the next one gets.
        r, _ = self._counted()
        first = r.resolve("capitalization_threshold")
        first["value"] = 999999.0
        first["candidates"].clear()
        second = r.resolve("capitalization_threshold")
        self.assertEqual(second["value"], 10000.0)

    def test_invalidate_forces_a_fresh_read(self) -> None:
        from pstb import wiki as wiki_mod

        r, hits = self._counted()
        r.resolve("capitalization_threshold")
        r.invalidate()
        wiki_mod.clear_lookup_cache()
        hits["n"] = 0
        r.resolve("capitalization_threshold")
        self.assertGreater(hits["n"], 0)


class DemoContentTests(unittest.TestCase):
    """The bundled sample policies are fictional. They must never filter a
    real ledger — that pairs a live balance with an invented rule."""

    def setUp(self) -> None:
        cfg = Config.sample(ROOT)
        cfg.wiki.provider = "localdocs"
        cfg.wiki.localdocs_path = str(ROOT / "sample_wiki")
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)
        self.wiki = make_wiki(cfg)
        self.engine.attach_policy(self.wiki, None)

    def tearDown(self) -> None:
        self.db.close()

    def test_the_demo_figure_is_returned_but_labelled(self) -> None:
        got = PolicyResolver(self.wiki, None).resolve("capitalization_threshold")
        self.assertEqual(got["status"], "demo_content")
        self.assertEqual(got["value"], 5000.0)      # it did find it
        self.assertFalse(got["usable_as_filter"])   # but it cannot be used
        self.assertIn("fictional", got["note"].lower())

    def test_demo_content_cannot_filter_a_query(self) -> None:
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql(
                "SELECT 1 AS x FROM PS_JRNL_LN WHERE MONETARY_AMOUNT >= :t",
                policy_binds={"t": "capitalization_threshold"})
        self.assertIn("NOT run", str(ctx.exception))


class SiteMemoryPrecedenceTests(_WikiCase):
    """A human-approved value for this deployment outranks a wiki scrape."""

    PAGES = {"fixed-asset-policy.md": CURRENT}

    class _Memory:
        def approved(self):
            return [{"fact": "Our capitalization_threshold is $25,000 for "
                             "FY2026 per the controller.",
                     "approved_by": "controller", "decided_at": "2026-01-15"}]

    def test_approved_memory_wins_and_says_so(self) -> None:
        got = PolicyResolver(self.wiki, self._Memory()).resolve(
            "capitalization_threshold")
        self.assertEqual(got["status"], "resolved")
        self.assertEqual(got["value"], 25000.0)
        self.assertEqual(got["source"]["kind"], "site_memory")
        self.assertIn("precedence", got["note"].lower())

    def test_without_memory_the_wiki_value_is_used(self) -> None:
        got = PolicyResolver(self.wiki, None).resolve("capitalization_threshold")
        self.assertEqual(got["value"], 10000.0)


if __name__ == "__main__":
    unittest.main()
