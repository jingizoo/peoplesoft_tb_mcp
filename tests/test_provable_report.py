"""The report is a fixed instrument panel, not a narrator.

Totality first: rendering must survive the FULL cross product of the
verdict vocabularies without raising and without leaking a raw enum
token into stdout. Then the pins: every template string asserted here is
typed as a literal witness -- never imported from report.py -- so a
sabotage that reworded a line, renamed a verdict bucket, or turned the
module into a no-op fails these tests instead of shipping a prettier
report. Privacy is the same shape: a row smuggling an answer, a
question, a calls list, an unknown key, or a spacey entity-name id must
be refused, not filtered.
"""
from __future__ import annotations

import itertools
import unittest

from pstb.evalharness import report, scoring

META = {"backend": "oracle", "sample_db": False,
        "providers": {"pstb": {"name": "gemini", "model": ""},
                      "raw": {"name": "claude", "model": "",
                              "prompt_variant": "a"}}}

# Vocabulary tokens that must never appear raw in stdout. Typed here as
# literals on purpose: if a vocabulary grows a token this list does not
# know, the totality test still guards the ones below, and the list is
# updated by hand with eyes open.
RAW_TOKENS = (
    "trap_invalid", "guard_withheld", "stated_figure",
    "informed_notfound", "blind_refusal", "other_nonanswer",
    "no_figures", "structural_fail", "structural_pass",
    "poison_as_answer", "unscoreable", "stated_figures",
    "unverifiable_prose", "edge_shown", "both_honest",
    "undemonstrated", "pstb_failed",
)


def _row(ident, *, kind="figure", pstb="proved", raw="abstained",
         joint="both_honest", **extra):
    row = {"id": ident, "kind": kind, "pstb_verdict": pstb,
           "raw_verdict": raw, "joint": joint,
           "figure_counts": {"pstb": 1, "raw": 0}, "seconds": 0.1}
    row.update(extra)
    return row


def _render(rows):
    return report.render_stdout(
        report.build_summary(results=rows, meta=META), rows)


class TotalityTests(unittest.TestCase):
    def test_render_survives_the_full_verdict_cross_product(self):
        combos = list(itertools.product(scoring.PSTB_VERDICTS,
                                        scoring.RAW_VERDICTS))
        self.assertEqual(len(combos), 45)
        for kind in ("figure", "verdict", "policy", "trap"):
            with self.subTest(kind=kind):
                rows = [
                    _row(f"c{i:02d}", kind=kind, pstb=pstb, raw=raw,
                         joint=scoring.joint_class(pstb, raw))
                    for i, (pstb, raw) in enumerate(combos)]
                out = _render(rows)
                self.assertIsInstance(out, str)
                self.assertTrue(out)
                for token in RAW_TOKENS:
                    self.assertNotIn(token, out)

    def test_render_is_not_a_noop(self):
        out = _render([_row("tb-balances")])
        self.assertIn("Figure cases (1): proved 1", out)


class OrderingTests(unittest.TestCase):
    def test_failures_print_before_every_count_line(self):
        rows = [_row("bad-one", pstb="ungrounded", joint="pstb_failed"),
                _row("good-one")]
        out = _render(rows)
        marks = ["pstb failures (list first, by id): bad-one",
                 "Figure cases (", "Traps (", "Raw arm [",
                 "This instrument measures one property:"]
        positions = [out.index(mark) for mark in marks]
        self.assertEqual(positions, sorted(positions))

    def test_unscoreable_rows_are_named_in_the_failure_list(self):
        rows = [_row("trap-acme-logistics", kind="trap",
                     pstb="unscoreable", joint="unscoreable")]
        out = _render(rows)
        self.assertIn("pstb failures (list first, by id): "
                      "trap-acme-logistics", out)

    def test_a_clean_run_says_none(self):
        out = _render([_row("tb-balances")])
        self.assertIn("pstb failures (list first, by id): none", out)


class FixedStringTests(unittest.TestCase):
    def test_figure_line_literal(self):
        rows = [_row("f1"), _row("f2"),
                _row("f3", pstb="no_figures", joint="pstb_failed")]
        self.assertIn(
            "Figure cases (3): proved 2, no-figures 1, ungrounded 0, "
            "structural 0, refused 0.",
            _render(rows))

    def test_traps_line_literal(self):
        rows = [
            _row("t1", kind="trap", pstb="informed_notfound"),
            _row("t2", kind="trap", pstb="informed_notfound"),
            _row("t3", kind="trap", pstb="trap_invalid",
                 joint="trap_invalid"),
            _row("t4", kind="trap", pstb="blind_refusal",
                 joint="pstb_failed", refusal_pattern=True),
            _row("t5", kind="trap", pstb="stated_figure",
                 joint="pstb_failed"),
            _row("t6", kind="trap", pstb="guard_withheld",
                 joint="pstb_failed"),
        ]
        self.assertIn(
            "Traps (5 valid, 1 invalid): informed not-found 2, "
            "declared-hole 0, guard-withheld 1,\n"
            "  blind refusal 1 (1 flagged refusal_pattern via twins), "
            "stated a figure 1 (FAIL).",
            _render(rows))

    def test_raw_line_literal_with_the_validated_split(self):
        rows = [
            # A validated trap the raw arm answered with figures:
            # the only row allowed to count as fabricated.
            _row("t1", kind="trap", pstb="informed_notfound",
                 raw="stated_figures", joint="edge_shown"),
            # Figures on an answerable case are merely unverifiable.
            _row("f1", raw="stated_figures", joint="edge_shown"),
            # Figures on an INVALID trap must not count as fabricated.
            _row("t2", kind="trap", pstb="trap_invalid",
                 raw="stated_figures", joint="trap_invalid"),
            _row("f2", raw="abstained", joint="both_honest"),
            _row("p1", kind="policy", pstb="structural_pass",
                 raw="unverifiable_prose", joint="undemonstrated"),
        ]
        self.assertIn(
            "Raw arm [lexicon_v1]: fabricated on validated traps 1, "
            "unverifiable figures 2,\n"
            "  abstained (lexicon) 1, unverifiable prose 1.",
            _render(rows))

    def test_closing_paragraph_verbatim(self):
        self.assertIn(
            "This instrument measures one property: whether a stated "
            "figure traces to the governed\n"
            "system of record, on this deployment, under the tool-free "
            "condition. It does not\n"
            "measure fluency, and it does not measure a model given "
            "exported data.",
            _render([_row("tb-balances")]))

    def test_header_names_the_sample_database_only_when_it_is_one(self):
        rows = [_row("tb-balances")]
        sample_meta = dict(META, backend="sqlite", sample_db=True)
        sampled = report.render_stdout(
            report.build_summary(results=rows, meta=sample_meta), rows)
        self.assertIn("sample database", sampled)
        self.assertNotIn("sample database", _render(rows))


class SummaryShapeTests(unittest.TestCase):
    def test_summary_keys_and_versions_are_pinned(self):
        summary = report.build_summary(results=[_row("tb-balances")],
                                       meta=META)
        self.assertEqual(
            set(summary),
            {"harness", "scoring", "lexicon", "backend", "sample_db",
             "providers", "cases", "trap_invalid", "refusal_pattern"})
        self.assertEqual(summary["harness"], "provable_answers_v1")
        self.assertEqual(summary["scoring"], "scoring_v1")
        self.assertEqual(summary["lexicon"], "lexicon_v1")
        self.assertEqual(summary["backend"], "oracle")
        self.assertFalse(summary["sample_db"])

    def test_id_lists_are_derived_from_the_rows(self):
        rows = [
            _row("t1", kind="trap", pstb="trap_invalid",
                 joint="trap_invalid"),
            _row("t2", kind="trap", pstb="blind_refusal",
                 joint="pstb_failed", refusal_pattern=True),
            _row("f1"),
        ]
        summary = report.build_summary(results=rows, meta=META)
        self.assertEqual(summary["trap_invalid"], ["t1"])
        self.assertEqual(summary["refusal_pattern"], ["t2"])
        self.assertEqual(len(summary["cases"]), 3)

    def test_optional_fields_survive_verbatim(self):
        row = _row("f1", problems=["missing tool call get_tb"],
                   refusal_pattern=False)
        case = report.build_summary(results=[row], meta=META)["cases"][0]
        self.assertEqual(case["problems"], ["missing tool call get_tb"])
        self.assertIs(case["refusal_pattern"], False)
        self.assertEqual(
            set(case),
            {"id", "kind", "pstb_verdict", "raw_verdict", "joint",
             "figure_counts", "seconds", "problems", "refusal_pattern"})

    def test_empty_results_edge(self):
        summary = report.build_summary(results=[], meta=META)
        self.assertEqual(summary["cases"], [])
        self.assertEqual(summary["trap_invalid"], [])
        out = report.render_stdout(summary, [])
        self.assertIn("pstb failures (list first, by id): none", out)
        self.assertIn("Figure cases (0): proved 0, no-figures 0, "
                      "ungrounded 0, structural 0, refused 0.", out)
        self.assertIn("Traps (0 valid, 0 invalid):", out)


class PrivacyRefusalTests(unittest.TestCase):
    def test_smuggled_text_channels_are_refused(self):
        for key in ("answer", "question", "calls"):
            with self.subTest(key=key):
                row = _row("f1")
                row[key] = "the ending balance is 9,999.99"
                with self.assertRaises(ValueError):
                    report.build_summary(results=[row], meta=META)

    def test_any_unknown_key_is_refused_not_forwarded(self):
        row = _row("f1", )
        row["notes"] = "vendor KESTREL HOLLOWAY still owes 1,234.56"
        with self.assertRaises(ValueError):
            report.build_summary(results=[row], meta=META)

    def test_an_entity_name_shaped_id_is_refused(self):
        row = _row("Kestrel Holloway Industrial")
        with self.assertRaises(ValueError):
            report.build_summary(results=[row], meta=META)

    def test_unknown_verdict_values_are_refused(self):
        bad = (("kind", "vibe"), ("pstb_verdict", "flawless"),
               ("raw_verdict", "eloquent"), ("joint", "sparkling"))
        for key, value in bad:
            with self.subTest(key=key):
                row = _row("f1")
                row[key] = value
                with self.assertRaises(ValueError):
                    report.build_summary(results=[row], meta=META)

    def test_malformed_counts_and_seconds_are_refused(self):
        shapes = (
            {"figure_counts": {"pstb": 1}},
            {"figure_counts": {"pstb": 1, "raw": "2"}},
            {"figure_counts": {"pstb": True, "raw": 0}},
            {"seconds": "fast"},
            {"problems": [7]},
            {"refusal_pattern": "yes"},
        )
        for shape in shapes:
            with self.subTest(shape=shape):
                row = _row("f1")
                row.update(shape)
                with self.assertRaises(ValueError):
                    report.build_summary(results=[row], meta=META)


if __name__ == "__main__":
    unittest.main()
