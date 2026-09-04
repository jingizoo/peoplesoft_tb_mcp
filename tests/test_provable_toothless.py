"""A neutered instrument must refuse to measure.

The scoring canary (three embedded triples scored through the real
functions) runs before ANY case does; these tests prove the canary has
teeth -- silently constant-pass scoring raises, the harness entry
point runs the canary before it touches a server, and a broken canary
therefore produces no report at all.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from pstb.evalharness import scoring


class CanaryTests(unittest.TestCase):
    def test_the_canary_passes_on_the_real_scorer(self):
        scoring.self_check()      # must not raise

    def test_a_constant_pass_scorer_is_caught(self):
        with patch.object(scoring, "score_pstb",
                          lambda **_kwargs: "proved"):
            with self.assertRaises(scoring.ScoringSelfCheckError):
                scoring.self_check()

    def test_a_constant_raw_scorer_is_caught(self):
        with patch.object(scoring, "score_raw",
                          lambda **_kwargs: "abstained"):
            with self.assertRaises(scoring.ScoringSelfCheckError):
                scoring.self_check()


class HarnessRefusesTests(unittest.TestCase):
    def test_the_entry_point_runs_the_canary_before_any_server(self):
        """_run must call scoring.self_check() before building the MCP
        server. A sentinel raise from the canary must surface -- if a
        server process were spawned first, this test would hang or
        error differently, and a neutered scorer would still have
        produced a report."""
        import asyncio
        from types import SimpleNamespace
        from pstb.evalharness import __main__ as entry

        class Sentinel(scoring.ScoringSelfCheckError):
            pass

        args = SimpleNamespace(
            provider="", raw_provider="x", suite="all", case="",
            raw_prompt_variant="a", json="", summary="", ci=False)
        with patch.object(scoring, "self_check",
                          side_effect=Sentinel("neutered")):
            with self.assertRaises(Sentinel):
                asyncio.run(entry._run(args))


class IntegrityAuditTests(unittest.TestCase):
    """The guard is audited, never trusted -- and the audit itself has
    teeth: neutering it must fail here, not silently in a report."""

    PAYLOAD = [{"ending_balance": 1234.56}]

    def _audit(self, answer, *, guard_withheld=False, status="passed"):
        from pstb.evalharness import arms
        return arms.integrity_audit(
            "case-x", answer, self.PAYLOAD,
            guard_withheld=guard_withheld, status=status)

    def test_agreement_returns_the_recompute(self):
        self.assertEqual(self._audit("The balance is 1,234.56."), [])

    def test_passed_with_ungrounded_figures_refuses_to_score(self):
        from pstb.evalharness.arms import HarnessIntegrityError
        with self.assertRaises(HarnessIntegrityError):
            self._audit("The balance is 9,999.99.")

    def test_a_fired_guard_without_blocked_refuses_to_score(self):
        from pstb.evalharness.arms import HarnessIntegrityError
        with self.assertRaises(HarnessIntegrityError):
            self._audit("I withheld that answer: it stated 9,999.99.",
                        guard_withheld=True, status="passed")

    def test_a_fired_guard_with_blocked_is_consistent(self):
        recomputed = self._audit(
            "I withheld that answer: it stated 9,999.99.",
            guard_withheld=True, status="blocked")
        self.assertEqual(recomputed, ["9,999.99"])


if __name__ == "__main__":
    unittest.main()
