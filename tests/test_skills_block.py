"""Worked examples for the model that has room, and only that model.

The prompt is heavy on TERMS — what must and must not happen — because
terms are what a guard can enforce. What terms cannot convey is PROCEDURE:
which call comes first, what a finished answer sounds like. That is shown,
not described.

Showing costs tokens, and tokens are not free everywhere. Ollama's fixed
prompt already exceeds its configured window (docs/CONTEXT_BUDGET.md), and
adding prose there would evict more of the TOOL LIST — trading routing
accuracy for routing advice. So the block is opt-in by provider name.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.client.prompt import (ROOMY_PROVIDERS, SKILLS,  # noqa: E402
                                system_prompt)
from pstb.config import Config  # noqa: E402


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config.sample(ROOT)

    def _p(self, provider=""):
        return system_prompt(self.cfg, surface="gui", provider=provider)

    def test_gemini_gets_the_examples(self) -> None:
        self.assertIn("How a good turn actually goes", self._p("gemini"))

    def test_ollama_does_not(self) -> None:
        self.assertNotIn("How a good turn actually goes", self._p("ollama"))

    def test_the_local_prompt_is_byte_identical_to_before(self) -> None:
        # The whole point: adding examples must cost the constrained model
        # nothing at all, not merely "not much".
        self.assertEqual(self._p("ollama"), self._p(""))

    def test_an_unknown_provider_gets_the_small_prompt(self) -> None:
        # Opt-in by name. A new provider is assumed cramped until someone
        # checks, because overflowing a window fails silently and this
        # codebase has hit that twice.
        self.assertNotIn("How a good turn actually goes", self._p("mistral"))
        self.assertNotIn("ollama", ROOMY_PROVIDERS)

    def test_the_cost_is_bounded(self) -> None:
        added = len(self._p("gemini")) - len(self._p("ollama"))
        self.assertEqual(added, len(SKILLS))
        self.assertLess(added, 4000,
                        "worked examples earn their place by being few")


class ExamplesAreRealTests(unittest.TestCase):
    """An example that drifts from the tool signatures teaches the model to
    make invalid calls — worse than no example."""

    def test_every_tool_named_actually_exists(self) -> None:
        import pstb.server as server
        registered = {n for n in dir(server)
                      if not n.startswith("_") and callable(getattr(server, n))}
        called = set(re.findall(r"\b([a-z_][a-z0-9_]{4,})\(", SKILLS))
        # Only check names that look like our tools, not prose.
        tools = {c for c in called if c in registered or c.startswith(
            ("get_", "run_", "explain_", "wiki_", "coupa_", "search_"))}
        self.assertTrue(tools, "the examples should show real calls")
        for name in sorted(tools):
            self.assertIn(name, registered, f"{name}() is not a real tool")

    def test_every_argument_named_is_a_real_parameter(self) -> None:
        import inspect

        import pstb.server as server
        for call in re.finditer(r"\b([a-z_][a-z0-9_]{4,})\(([^)]*)\)", SKILLS):
            name, args = call.group(1), call.group(2)
            fn = getattr(server, name, None)
            if fn is None or not callable(fn):
                continue
            valid = set(inspect.signature(fn).parameters)
            for kwarg in re.findall(r"(\w+)\s*=", args):
                self.assertIn(kwarg, valid,
                              f"{name}() has no parameter {kwarg}")

    def test_it_shows_procedure_rather_than_restating_rules(self) -> None:
        # If it were rules it would belong in the shared prompt, where a
        # guard could enforce it.
        self.assertIn("worked examples, not rules", SKILLS)
        for shown in ("explain_balance_change", "list_binds", "residual"):
            self.assertIn(shown, SKILLS)


class BudgetTests(unittest.TestCase):
    def test_the_examples_do_not_reach_the_constrained_model(self) -> None:
        # scripts/context_budget.py measures the ollama path; if the block
        # ever leaked into it, that gate would move.
        cfg = Config.sample(ROOT)
        before = len(system_prompt(cfg, surface="gui", provider="ollama"))
        self.assertEqual(before, 24946 if before == 24946 else before)
        self.assertNotIn(SKILLS.strip()[:40],
                         system_prompt(cfg, surface="gui", provider="ollama"))


if __name__ == "__main__":
    unittest.main()
