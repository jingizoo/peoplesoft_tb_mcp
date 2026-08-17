"""The card was blank because a tool refused an argument the guard sent.

Reported as "AR aging doesn't render — click the arrow and it's blank."
The chain: the scope guard learned to inject as_of_date, six MCP wrappers
never learned to accept it, FastMCP turned the TypeError into a plain-text
`TOOL ERROR:` string, the browser could not parse it as JSON, and a card
with no body renders as nothing at all. Every link was silent.

So three things are pinned here, and the third matters most:

  1. Every tool accepts every argument the scope guard can hand it. A
     mapping and a signature that disagree is not caught by any other
     test, because nothing else calls them together.
  2. A result that is not JSON is shown, not swallowed. The renderer's
     "no body" state is for a tool that genuinely returned nothing.
  3. The two pieces of per-turn state that used to be process-global.
"""
from __future__ import annotations

import inspect
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb import server  # noqa: E402
from pstb.guards import _TOOL_SCOPE_ARGS, apply_request_scope  # noqa: E402


class ScopeContractTests(unittest.TestCase):
    """A scope mapping is a promise about a signature."""

    def test_every_tool_accepts_what_the_guard_injects(self) -> None:
        broken = []
        for tool, mapping in _TOOL_SCOPE_ARGS.items():
            fn = getattr(server, tool, None)
            if fn is None:
                broken.append(f"{tool}: no such MCP tool")
                continue
            params = set(inspect.signature(fn).parameters)
            for arg in mapping.values():
                if arg not in params:
                    broken.append(f"{tool} cannot accept {arg!r}")
        self.assertEqual(broken, [], "; ".join(broken))

    def test_the_injected_arguments_actually_call(self) -> None:
        # A signature check proves the call will not raise TypeError. This
        # proves the tool then RUNS with the value — the six broken ones
        # would have failed here too.
        scope = {"business_unit": "US001", "ledger": "ACTUALS",
                 "fiscal_year": 2026, "period": 6, "as_of_date": "2026-06-30"}
        ran = 0
        for tool in sorted(_TOOL_SCOPE_ARGS):
            fn = getattr(server, tool, None)
            if fn is None:
                continue
            args = apply_request_scope(tool, {}, scope)
            # A tool with its OWN required argument (an account, a query,
            # a report name) cannot be called from a scope alone; the
            # signature check above already covers it. Derive the skip
            # rather than list it, so a new tool joins automatically.
            required = {name for name, prm
                        in inspect.signature(fn).parameters.items()
                        if prm.default is inspect.Parameter.empty}
            if required - set(args):
                continue
            out = fn(**args)
            ran += 1
            self.assertIsInstance(out, dict, tool)
            self.assertNotIn("TOOL ERROR", json.dumps(out, default=str)[:200],
                             tool)
        self.assertGreater(ran, 10, "the sweep stopped exercising tools")

    def test_a_tool_that_takes_a_date_is_mapped_to_receive_one(self) -> None:
        # The other direction: a date-taking tool left OUT of the mapping
        # silently ignores the selected period, which is the defect this
        # mapping was added to fix.
        missing = []
        for tool in dir(server):
            fn = getattr(server, tool)
            if not callable(fn) or tool.startswith("_"):
                continue
            try:
                params = set(inspect.signature(fn).parameters)
            except (TypeError, ValueError):
                continue
            if "as_of_date" in params and "business_unit" in params:
                mapped = _TOOL_SCOPE_ARGS.get(tool, {})
                if "as_of_date" not in mapped.values():
                    missing.append(tool)
        self.assertEqual(missing, [],
                         f"these take a date and never receive the selected "
                         f"period: {missing}")


class BlankCardTests(unittest.TestCase):
    """A result that cannot be parsed must be visible, not absent."""

    def test_a_non_json_result_becomes_a_readable_error(self) -> None:
        from pstb.gui import app as gapp
        source = inspect.getsource(gapp.chat)
        self.assertIn("non_json_result", source)
        self.assertNotIn("data = None", source,
                         "a null result is what renders as an empty card")

    def test_the_browser_only_shows_no_body_for_a_real_absence(self) -> None:
        html = (ROOT / "pstb" / "gui" / "static" / "index.html").read_text()
        self.assertIn("c.result==null||c.result===undefined", html)
        self.assertIn("could not be rendered", html,
                      "a renderer returning nothing for a payload that "
                      "exists must fall back to the raw value")


class FinancePresentationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "pstb" / "gui" / "static" / "index.html").read_text()

    def test_primary_evidence_is_not_left_collapsed(self) -> None:
        self.assertIn("primary.open=true", self.html)
        self.assertIn("resultBadge(c.result)", self.html)

    def test_custom_discovery_evidence_has_dedicated_renderers(self) -> None:
        for tool, renderer in (
                ("describe_record", "renderRecordDescription"),
                ("profile_record", "renderRecordProfile"),
                ("compare_records", "renderRecordCompare"),
                ("detect_transaction_anomalies", "renderAnomalies")):
            self.assertIn(f"name==='{tool}'", self.html)
            self.assertIn(renderer, self.html)
        self.assertIn("unresolved — do not guess", self.html)
        self.assertIn("r.taught_status", self.html)

    def test_ap_claims_distinguish_candidates_from_confirmed_cash(self) -> None:
        for renderer in ("renderOpenPayables", "renderVendorPayments",
                         "renderDuplicatePayments"):
            self.assertIn(renderer, self.html)
        self.assertIn("not confirmed paid twice", self.html)
        self.assertIn("historical limitation", self.html)

    def test_ap_reconciliation_leads_with_evaluation_status(self) -> None:
        self.assertIn(
            "if(name==='reconcile_ap_to_gl') return renderAPReconciliation(data);",
            self.html,
        )
        self.assertIn("function renderAPReconciliation", self.html)
        self.assertIn("Incomplete — no conclusion", self.html)
        self.assertIn("An incomplete result cannot support", self.html)
        self.assertIn("AP period activity", self.html)
        self.assertIn("Population coverage", self.html)
        self.assertIn("Amount basis", self.html)

    def test_starter_questions_are_grouped_by_controller_workstream(self) -> None:
        self.assertIn("starter-groups", self.html)
        for label in ("Billing & AR", "Accounts payable", "GL & close"):
            self.assertIn(label, self.html)


class PerTurnStateTests(unittest.TestCase):
    """Two colleagues asking at once must not share a turn."""

    def test_the_turn_id_comes_back_per_call(self) -> None:
        from pstb.client.chat import agent_turn
        self.assertIn("turn_meta", inspect.signature(agent_turn).parameters)

    def test_the_gui_reads_its_own_turn_id_not_the_function(self) -> None:
        from pstb.gui import app as gapp
        source = inspect.getsource(gapp.chat)
        self.assertIn('turn_meta.get("turn_id")', source)
        self.assertNotIn("agent_turn.last_turn_id", source,
                         "a module-level slot is overwritten by whichever "
                         "concurrent turn finishes last, and the feedback "
                         "button then logs against the wrong answer")

    def test_reading_the_result_limit_never_mutates_the_process(self) -> None:
        from pstb.client import chat as chat_mod
        before = chat_mod.MAX_TOOL_RESULT_CHARS
        from pstb.config import Config
        chat_mod.tool_result_limit(Config.sample(ROOT), "claude")
        self.assertEqual(chat_mod.MAX_TOOL_RESULT_CHARS, before)


class RawSqlCombinationTests(unittest.TestCase):
    """Ad-hoc SQL, a network bind and no row security is a combination."""

    @staticmethod
    def _start(host, security):
        import argparse
        import io
        import os
        from contextlib import redirect_stdout
        from unittest.mock import patch

        from pstb.gui import app as gapp
        saved = (gapp.cfg.tools.allow_raw_sql, gapp.cfg.security.enabled)
        gapp.cfg.tools.allow_raw_sql = True
        gapp.cfg.security.enabled = security
        args = argparse.Namespace(host=host, port=8016, open=False,
                                  share=False, allow_host=[])
        out = io.StringIO()
        try:
            with patch("argparse.ArgumentParser.parse_args",
                       return_value=args):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("PSTB_AUTH_TOKEN", None)
                    with patch("uvicorn.run"):
                        with redirect_stdout(out):
                            gapp.main()
            return gapp.cfg.tools.allow_raw_sql, out.getvalue()
        finally:
            gapp.cfg.tools.allow_raw_sql, gapp.cfg.security.enabled = saved

    def test_network_bind_without_security_switches_it_off(self) -> None:
        on, banner = self._start("0.0.0.0", False)
        self.assertFalse(on)
        self.assertIn("Ad-hoc SQL is OFF", banner)
        self.assertIn("security.enabled", banner)
        self.assertIn("raw_sql_on_shared_bind", banner,
                      "name BOTH ways back, or the next person edits the "
                      "guard out instead of choosing")

    def test_row_security_restores_it(self) -> None:
        # With security on, run_sql is already refused per user by
        # unit_access_block — the caller is identified again.
        on, banner = self._start("0.0.0.0", True)
        self.assertTrue(on)
        self.assertNotIn("Ad-hoc SQL is OFF", banner)

    def test_a_loopback_bind_is_untouched(self) -> None:
        # This is the workflow the product has always supported; refusing
        # it would be the guard doing harm.
        on, banner = self._start("127.0.0.1", False)
        self.assertTrue(on)
        self.assertNotIn("Ad-hoc SQL is OFF", banner)

    def test_an_operator_can_accept_the_risk_deliberately(self) -> None:
        from pstb.gui import app as gapp
        saved = gapp.cfg.tools.raw_sql_on_shared_bind
        gapp.cfg.tools.raw_sql_on_shared_bind = True
        try:
            on, banner = self._start("0.0.0.0", False)
        finally:
            gapp.cfg.tools.raw_sql_on_shared_bind = saved
        self.assertTrue(on)
        self.assertNotIn("Ad-hoc SQL is OFF", banner)


if __name__ == "__main__":
    unittest.main()
