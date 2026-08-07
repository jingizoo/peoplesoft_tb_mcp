"""Resilience and context fixes found by an improvement review.

Four defects, none of which surfaced as a crash — the worst kind:
a per-turn resource leak visible only after hours of errors, a process
global that let one browser session decide another's truncation, a
monitor that diffs across period boundaries on the one morning it must
be right, and a local model reading a prompt the runtime silently cut in
half.
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.client import chat as chat_mod  # noqa: E402
from pstb.config import Config  # noqa: E402


class PerTurnStackTests(unittest.TestCase):
    """The fallback path holds an MCP subprocess. Closing it only on the
    success path leaked one process per errored turn."""

    def test_the_stack_is_closed_in_a_finally(self) -> None:
        from pstb.gui import app as gapp
        src = inspect.getsource(gapp.chat)
        self.assertIn("per_turn: \"contextlib.AsyncExitStack | None\" = None",
                      src, "the stack must be owned outside the try")
        self.assertIn("finally:", src)
        finally_tail = src[src.rindex("finally:"):]
        self.assertIn("per_turn.aclose()", finally_tail,
                      "a failed turn must still close the subprocess")

    def test_no_close_remains_on_the_success_path_only(self) -> None:
        from pstb.gui import app as gapp
        src = inspect.getsource(gapp.chat)
        self.assertEqual(src.count("await per_turn.aclose()"), 1,
                         "exactly one close, in the finally")


class ToolResultLimitTests(unittest.TestCase):
    """A module global mutated from a request handler is shared state
    between browser sessions."""

    def test_the_limit_is_a_pure_function(self) -> None:
        cfg = Config.sample(ROOT)
        self.assertEqual(chat_mod.tool_result_limit(cfg, "gemini"), 120_000)
        self.assertEqual(chat_mod.tool_result_limit(cfg, "ollama"), 24_000)

    def test_an_explicit_config_value_wins(self) -> None:
        cfg = Config.sample(ROOT)
        cfg.llm.max_tool_result_chars = 9_000
        self.assertEqual(chat_mod.tool_result_limit(cfg, "gemini"), 9_000)

    def test_computing_the_limit_does_not_mutate_the_global(self) -> None:
        before = chat_mod.MAX_TOOL_RESULT_CHARS
        chat_mod.tool_result_limit(Config.sample(ROOT), "gemini")
        self.assertEqual(chat_mod.MAX_TOOL_RESULT_CHARS, before,
                         "reading a limit must not change what every other "
                         "session sees")

    def test_the_gui_passes_it_per_turn(self) -> None:
        from pstb.gui import app as gapp
        src = inspect.getsource(gapp.chat)
        self.assertIn("result_limit=result_limit", src)
        self.assertNotIn("set_tool_result_limit", src,
                         "the GUI must not mutate the process global")

    def test_call_mcp_tool_honours_the_per_call_limit(self) -> None:
        sig = inspect.signature(chat_mod.call_mcp_tool)
        self.assertIn("limit", sig.parameters)
        big = '{"rows": [' + ", ".join(
            '{"a": %d}' % i for i in range(400)) + "]}"
        self.assertLess(len(chat_mod._truncate_json(big, 500)), len(big))


class MonitorScopeTests(unittest.TestCase):
    """A period change is a new baseline, not a diff."""

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            "monitor_mod", ROOT / "scripts" / "monitor.py")
        cls.mon = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mon)

    def test_periods_get_separate_snapshots(self) -> None:
        p6 = self.mon._key("daily_brief", "US001", "ACTUALS", 2026, 6)
        p7 = self.mon._key("daily_brief", "US001", "ACTUALS", 2026, 7)
        self.assertNotEqual(p6, p7,
                            "diffing period 7 against period 6 reports every "
                            "cleared item as fixed and every new one as a "
                            "regression")

    def test_years_get_separate_snapshots(self) -> None:
        self.assertNotEqual(
            self.mon._key("close_readiness", "US001", "ACTUALS", 2026, 12),
            self.mon._key("close_readiness", "US001", "ACTUALS", 2027, 12))

    def test_scopes_still_separate_and_keys_stay_filesystem_safe(self) -> None:
        self.assertNotEqual(
            self.mon._key("daily_brief", "US001", "ACTUALS", 2026, 6),
            self.mon._key("daily_brief", "EU001", "ACTUALS", 2026, 6))
        key = self.mon._key("daily/brief", "US 001", "ACT:UALS", 2026, 6)
        self.assertTrue(all(c.isalnum() or c in "-_" for c in key), key)

    def test_a_scopeless_playbook_still_keys(self) -> None:
        self.assertTrue(self.mon._key("x", "US001", "ACTUALS"))


class OllamaContextTests(unittest.TestCase):
    """Ollama defaults to 2048 tokens of context. This system prompt alone
    is ~5,500, so the default silently truncated it — every local-model
    routing failure was measured against a prompt it never fully saw."""

    def test_the_context_default_fits_the_prompt_and_then_some(self) -> None:
        cfg = Config.sample(ROOT)
        self.assertGreaterEqual(cfg.llm.ollama_num_ctx, 16384)

    def test_the_prompt_would_not_fit_the_ollama_default(self) -> None:
        from pstb.client.prompt import system_prompt
        approx_tokens = len(system_prompt(Config.sample(ROOT))) // 4
        self.assertGreater(approx_tokens, 2048,
                           "if the prompt ever fits 2048 tokens this test "
                           "should be revisited, not deleted")

    def test_the_provider_sends_num_ctx(self) -> None:
        src = (ROOT / "pstb" / "client" / "llm_ollama.py").read_text()
        self.assertIn('"num_ctx": self.num_ctx', src)


if __name__ == "__main__":
    unittest.main()
