"""The database selector is a guard, not a label.

The badge says which database answered. This says which database the model
is ALLOWED to reach: a source the person did not select is refused, the same
way a business unit they did not select already was.

Why hard rather than soft. fiscal_year and period are defaults the question
may override — "show me period 3" while the chip reads P6 is a legitimate
question. A database is not that. Answering from a warehouse when the reader
selected the finance system is not a narrower answer to their question, it
is an answer to a different one, and the figures carry the same column names
either way.

Why the primary is not pinned. Every deployment today has one database and
sends no source argument. Pinning "default" would make the guard refuse the
normal case, so only a NON-primary selection travels in the scope.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.guards import (ScopeConflict, _SOURCE_SCOPED_TOOLS,  # noqa: E402
                         _TOOL_SCOPE_ARGS, apply_request_scope,
                         normalize_request_scope)

MART = {"source": "p2go", "business_unit": "US001", "ledger": "ACTUALS"}
PRIMARY = {"business_unit": "US001", "ledger": "ACTUALS"}


class LockTests(unittest.TestCase):
    def test_an_unselected_database_is_refused(self) -> None:
        with self.assertRaises(ScopeConflict) as ctx:
            apply_request_scope("run_sql", {"source": "default"}, MART)
        self.assertIn("source", str(ctx.exception))

    def test_the_selected_database_is_injected_when_omitted(self) -> None:
        out = apply_request_scope("list_tables", {}, MART)
        self.assertEqual(out["source"], "p2go")

    def test_a_matching_choice_passes(self) -> None:
        out = apply_request_scope("list_tables", {"source": "p2go"}, MART)
        self.assertEqual(out["source"], "p2go")

    def test_case_and_primary_aliases_are_one_database(self) -> None:
        # "peoplesoft" / "main" / "" all mean the primary; comparing raw
        # strings made a matching selection look like a conflict.
        self.assertEqual(
            apply_request_scope("list_tables", {"source": "P2GO"}, MART)
            ["source"], "P2GO")
        for alias in ("peoplesoft", "main", "PS", "default"):
            with self.subTest(alias):
                out = apply_request_scope(
                    "list_tables", {"source": alias},
                    {"source": "default", **PRIMARY})
                self.assertEqual(out["source"], alias)

    def test_reaching_elsewhere_from_the_primary_is_refused(self) -> None:
        with self.assertRaises(ScopeConflict):
            apply_request_scope("list_tables", {"source": "p2go"},
                                {"source": "default", **PRIMARY})

    def test_every_source_taking_tool_is_locked(self) -> None:
        import inspect

        from pstb import server as srv
        for name in _SOURCE_SCOPED_TOOLS:
            with self.subTest(name):
                self.assertEqual(_TOOL_SCOPE_ARGS[name].get("source"),
                                 "source")
                fn = getattr(srv, name, None)
                if fn is not None:
                    self.assertIn("source",
                                  inspect.signature(fn).parameters,
                                  "a locked tool that cannot accept the "
                                  "argument is the blank-card bug again")

    def test_curated_financial_tools_are_not_source_locked(self) -> None:
        # They answer from the primary by construction and take no source
        # argument; locking them would pin an argument they never send.
        for name in ("get_trial_balance", "get_ar_aging",
                     "get_customer_financial_360"):
            with self.subTest(name):
                self.assertNotIn("source", _TOOL_SCOPE_ARGS.get(name, {}))
                out = apply_request_scope(name, {}, MART)
                self.assertNotIn("source", out)


class NoSelectionTests(unittest.TestCase):
    """The single-database deployment must not notice any of this."""

    def test_no_source_in_scope_leaves_the_argument_alone(self) -> None:
        self.assertEqual(
            apply_request_scope("list_tables", {"source": "p2go"}, PRIMARY),
            {"source": "p2go"})
        self.assertEqual(apply_request_scope("list_tables", {}, PRIMARY), {})

    def test_a_blank_source_is_not_a_constraint(self) -> None:
        self.assertEqual(normalize_request_scope({"source": ""}), {})
        self.assertEqual(normalize_request_scope({"source": "   "}), {})


class ClientTests(unittest.TestCase):
    def test_only_a_non_primary_choice_is_sent(self) -> None:
        html = (ROOT / "pstb" / "gui" / "static" / "index.html").read_text()
        self.assertIn("!=='default') out.source=String(value.source)", html,
                      "sending 'default' would pin every ad-hoc call and "
                      "refuse the single-database deployment")

    def test_the_chooser_hides_when_there_is_nothing_to_choose(self) -> None:
        html = (ROOT / "pstb" / "gui" / "static" / "index.html").read_text()
        self.assertIn("if(list.length<2){ wrap.hidden=true; return; }", html)

    def test_finance_only_chips_hide_on_another_database(self) -> None:
        html = (ROOT / "pstb" / "gui" / "static" / "index.html").read_text()
        self.assertIn("const psOnly = sel.value==='default';", html)
        self.assertIn("curated financial tools", html)


if __name__ == "__main__":
    unittest.main()
