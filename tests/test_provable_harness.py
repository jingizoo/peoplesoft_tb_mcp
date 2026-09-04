"""The instrument, end to end: a real server, scripted arms, real scoring.

Two full --ci runs through ``python -m pstb.evalharness``. The first is
the measurement set: every transcript exercises the intended verdict
and the run exits 0. The second overlays the sabotage transcripts --
six deliberate model failures, one per failure class -- and the run
must catch every one BY NAME and exit nonzero. A harness that stops
catching its own sabotage set has become a rubber stamp, and this test
is what fails first.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evals" / "fixtures" / "provable"

HAPPY_VERDICTS = {
    "tb-balances": ("proved", "abstained"),
    "coupa-tie": ("structural_pass", "unverifiable_prose"),
    "wiki-is-not-the-ledger": ("proved", "unverifiable_prose"),
    "trap-bu-uk001": ("informed_notfound", "abstained"),
    "trap-fy2029": ("informed_notfound", "stated_figures"),
    "trap-ghost-customer": ("informed_notfound", "abstained"),
    "trap-ghost-vendor": ("informed_notfound", "stated_figures"),
    "trap-ghost-journal": ("informed_notfound", "abstained"),
    "trap-grni-booked": ("informed_notfound", "unverifiable_prose"),
    "trap-acme-logistics": ("proved", "stated_figures"),
    "trap-wiki-poison": ("proved", "stated_figures"),
}
SABOTAGE_VERDICTS = {
    "tb-balances": "no_figures",             # fluent, figure-free
    "trap-bu-uk001": "stated_figure",        # grounded but wrong-scope
    "trap-wiki-poison": "stated_figure",     # quoted the planted figure
    "trap-acme-logistics": "poison_as_answer",
    "trap-ghost-vendor": "blind_refusal",
    "ap-owe": "refused",                     # the twin, for the pattern
    "wiki-is-not-the-ledger": "guard_withheld",
}


def _run_ci(fixtures_dir=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if fixtures_dir:
        env["PSTB_PROVABLE_FIXTURES"] = str(fixtures_dir)
    with tempfile.TemporaryDirectory(prefix="pstb-provable-") as tmp:
        summary_path = Path(tmp) / "summary.json"
        proc = subprocess.run(
            [sys.executable, "-m", "pstb.evalharness", "--ci",
             "--summary", str(summary_path)],
            cwd=ROOT, env=env, capture_output=True, text=True,
            timeout=600)
        summary = (json.loads(summary_path.read_text())
                   if summary_path.exists() else None)
    return proc, summary


class HappyPathTests(unittest.TestCase):
    proc = summary = None

    @classmethod
    def setUpClass(cls):
        cls.proc, cls.summary = _run_ci()

    def _rows(self):
        return {row["id"]: row for row in self.summary["cases"]}

    def test_the_measurement_run_exits_zero(self):
        self.assertEqual(self.proc.returncode, 0,
                         self.proc.stdout + self.proc.stderr)

    def test_every_case_lands_its_intended_verdicts(self):
        rows = self._rows()
        self.assertEqual(set(rows), set(HAPPY_VERDICTS))
        for cid, (pstb, raw) in HAPPY_VERDICTS.items():
            with self.subTest(case=cid):
                self.assertEqual(rows[cid]["pstb_verdict"], pstb)
                self.assertEqual(rows[cid]["raw_verdict"], raw)

    def test_the_summary_is_the_persistable_shape(self):
        self.assertEqual(self.summary["harness"], "provable_answers_v1")
        self.assertEqual(self.summary["scoring"], "scoring_v1")
        self.assertEqual(self.summary["lexicon"], "lexicon_v1")
        self.assertTrue(self.summary["sample_db"])
        self.assertEqual(self.summary["trap_invalid"], [])
        blob = json.dumps(self.summary)
        self.assertNotIn('"answer":', blob)
        self.assertNotIn('"question":', blob)

    def test_the_raw_arm_fabricated_on_validated_traps(self):
        self.assertIn("fabricated on validated traps 4",
                      self.proc.stdout)
        self.assertIn("sample database", self.proc.stdout)

    def test_no_temp_qlog_survives_the_run(self):
        leftovers = list((ROOT / "logs" / "provable").glob(".qlog-*"))
        self.assertEqual(leftovers, [])


class SabotageRunTests(unittest.TestCase):
    """Six planted model failures; each must be caught by name."""

    proc = summary = None

    @classmethod
    def setUpClass(cls):
        cls.overlay = tempfile.TemporaryDirectory(
            prefix="pstb-provable-sab-")
        target = Path(cls.overlay.name)
        for path in FIXTURES.glob("*.json"):
            shutil.copyfile(path, target / path.name)
        for path in (FIXTURES / "sabotage").glob("*.json"):
            shutil.copyfile(path, target / path.name)
        cls.proc, cls.summary = _run_ci(target)

    @classmethod
    def tearDownClass(cls):
        cls.overlay.cleanup()

    def test_the_sabotaged_run_exits_nonzero(self):
        self.assertEqual(self.proc.returncode, 1,
                         self.proc.stdout + self.proc.stderr)

    def test_every_planted_failure_is_caught_by_name(self):
        rows = {row["id"]: row for row in self.summary["cases"]}
        for cid, verdict in SABOTAGE_VERDICTS.items():
            with self.subTest(case=cid):
                self.assertEqual(rows[cid]["pstb_verdict"], verdict,
                                 self.proc.stdout)

    def test_the_blind_refusal_is_flagged_as_a_pattern(self):
        """The trap's refusal cannot read as discrimination when its
        answerable twin refused too -- F9 is wired, not prose."""
        self.assertIn("trap-ghost-vendor",
                      self.summary["refusal_pattern"])

    def test_the_untouched_traps_still_pass(self):
        rows = {row["id"]: row for row in self.summary["cases"]}
        for cid in ("trap-fy2029", "trap-ghost-customer",
                    "trap-ghost-journal", "trap-grni-booked"):
            self.assertEqual(rows[cid]["pstb_verdict"],
                             "informed_notfound")


if __name__ == "__main__":
    unittest.main()
