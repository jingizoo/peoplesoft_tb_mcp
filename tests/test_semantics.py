"""The concept register: sourced, governed, and injection-free.

What must hold: the ladder's precedence (approved memory > config > seed)
with provenance travelling into the answer; a PENDING fact never takes
effect — approval is the whole governance story; a malformed override is
skipped and named, never crashed on and never silently accepted; and an
override changes result classification only — the SQL the database sees
is byte-identical, so a taught value has zero injection surface.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.ar import ARBilling  # noqa: E402
from pstb.config import Config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.memory import SiteMemory  # noqa: E402
from pstb.semantics import SEEDS, resolve  # noqa: E402


class LadderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config.sample(ROOT)
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.memory = SiteMemory(Path(self.tmp.name))

    def _teach(self, text, approve=True):
        fact = self.memory.propose(text, kind="convention")["fact"]
        if approve:
            self.memory.decide(fact["id"], approve=True)
        return fact

    def test_the_seed_is_the_floor(self) -> None:
        r = resolve("billing_invoiced", cfg=self.cfg)
        self.assertEqual((r.rung, r.values), ("seed", ("INV",)))
        self.assertIn("built-in default", r.source)

    def test_config_beats_seed(self) -> None:
        self.cfg.semantics = {"billing_invoiced": {"values": ["INV", "PRO"]}}
        r = resolve("billing_invoiced", cfg=self.cfg)
        self.assertEqual((r.rung, r.values), ("config", ("INV", "PRO")))

    def test_approved_memory_beats_config(self) -> None:
        self.cfg.semantics = {"billing_invoiced": {"values": ["INV"]}}
        self._teach("semantics: billing_invoiced = BILL_STATUS IN (INV, RDY)")
        r = resolve("billing_invoiced", cfg=self.cfg, memory=self.memory)
        self.assertEqual((r.rung, r.values), ("memory", ("INV", "RDY")))
        self.assertIn("approved site memory", r.source)

    def test_a_pending_fact_never_takes_effect(self) -> None:
        self._teach("semantics: billing_invoiced = BILL_STATUS IN (RDY)",
                    approve=False)
        r = resolve("billing_invoiced", cfg=self.cfg, memory=self.memory)
        self.assertEqual(r.rung, "seed",
                         "an unapproved override changed the population — "
                         "approval is the entire governance story")

    def test_a_column_change_is_refused_with_the_reason(self) -> None:
        self._teach("semantics: billing_invoiced = BILL_TYPE_ID IN (STD)")
        r = resolve("billing_invoiced", cfg=self.cfg, memory=self.memory)
        self.assertEqual(r.rung, "seed")
        self.assertTrue(any("changing the column needs a code change" in n
                            for n in r.notes))

    def test_bad_codes_fall_through_named(self) -> None:
        self.cfg.semantics = {"billing_invoiced":
                              {"values": ["INV; DROP TABLE"]}}
        r = resolve("billing_invoiced", cfg=self.cfg)
        self.assertEqual(r.rung, "seed")
        self.assertTrue(any("override ignored" in n for n in r.notes))

    def test_unknown_concepts_raise(self) -> None:
        with self.assertRaises(KeyError):
            resolve("asset_capitalized", cfg=self.cfg)

    def test_exactly_one_seed_exists(self) -> None:
        # Every other candidate examined needed a governor set, a
        # cross-record amount, or a site election. A second seed must
        # arrive with the same evidence the first did — not by analogy.
        self.assertEqual(list(SEEDS), ["billing_invoiced"])


class RegisterDrivesTheAnswerTests(unittest.TestCase):
    def _totals(self, cfg):
        db = Database(cfg)
        try:
            return ARBilling(TBEngine(db, cfg)).invoice_totals(
                business_unit="US001")
        finally:
            db.close()

    def test_an_override_reclassifies_without_touching_sql(self) -> None:
        base = self._totals(Config.sample(ROOT))
        cfg = Config.sample(ROOT)
        cfg.semantics = {"billing_invoiced": {"values": ["INV", "RDY"]}}
        widened = self._totals(cfg)
        self.assertAlmostEqual(
            widened["invoiced_total_by_currency"]["USD"],
            base["invoiced_total_by_currency"]["USD"] + 31250.0, places=2,
            msg="RDY bills must move INTO the finalized total")
        self.assertFalse(
            any(x["status"] == "RDY" for x in
                widened["population"]["excluded_pipeline"]),
            "a status counted as finalized cannot also sit in the pipeline")
        self.assertEqual(
            widened["population"]["applied"][0]["source"],
            "config.yaml semantics",
            "the population block must say WHO defined the population")

    def test_the_disclosed_predicate_matches_the_resolution(self) -> None:
        cfg = Config.sample(ROOT)
        cfg.semantics = {"billing_invoiced": {"values": ["INV", "RDY"]}}
        out = self._totals(cfg)
        self.assertEqual(out["population"]["applied"][0]["predicate"],
                         "BILL_STATUS IN ('INV', 'RDY')")


if __name__ == "__main__":
    unittest.main()
