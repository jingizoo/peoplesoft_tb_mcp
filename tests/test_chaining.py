"""Chaining by reference: sets flow between tools without model retyping.

"All the accounts in tree node X, then something else about them" cannot be
a join — trees attach RANGES, not accounts — so it is a genuine chain. The
weak link in any model-driven chain is the handoff: reading forty accounts
out of one result and typing them into the next call is where thirty-nine
arrive and one transposes, and the number guard checks answers, not
arguments.

So results carry ids (r1, r2, ...), the model wires REFERENCES
({"from_result": "r1", "field": "accounts"}), and the client substitutes
the real values from the stored result — the same mechanism as scope
injection. These tests run the chain through the REAL agent loop and assert
the values that reached the second call are exactly the producer's output.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.client.chat import (ResultRefError, _resolve_path,  # noqa: E402
                              agent_turn, resolve_result_refs)
from pstb.client.llm_base import LLMResponse, ToolCall  # noqa: E402
from pstb.config import load_config  # noqa: E402
from pstb.db import Database  # noqa: E402
from pstb.engine import EngineError, TBEngine  # noqa: E402
from pstb.report import ReportRunner  # noqa: E402


class PathTests(unittest.TestCase):
    def test_plain_list_field(self):
        self.assertEqual(_resolve_path({"accounts": ["6000", "6100"]},
                                       "accounts"), ["6000", "6100"])

    def test_nested_list_of_dicts(self):
        payload = {"rows": [{"account": "6000"}, {"account": "6100"}]}
        self.assertEqual(_resolve_path(payload, "rows[].account"),
                         ["6000", "6100"])

    def test_scalars_only_no_structures(self):
        payload = {"rows": [{"a": {"deep": 1}}, {"a": 2}]}
        self.assertEqual(_resolve_path(payload, "rows[].a"), [2])


class ResolveRefTests(unittest.TestCase):
    STORE = {"r1": {"accounts": ["6000", "6100", "6200"]}}

    def test_reference_becomes_the_real_values(self):
        args = {"sql": "…", "list_binds": {
            "accts": {"from_result": "r1", "field": "accounts"}}}
        out = resolve_result_refs(args, self.STORE)
        self.assertEqual(out["list_binds"]["accts"],
                         ["6000", "6100", "6200"])

    def test_unknown_result_id_names_what_exists(self):
        with self.assertRaises(ResultRefError) as ctx:
            resolve_result_refs({"x": {"from_result": "r9", "field": "a"}},
                                self.STORE)
        self.assertIn("r1", str(ctx.exception))
        self.assertIn("NEXT round", str(ctx.exception))

    def test_a_path_reaching_nothing_shows_the_keys(self):
        with self.assertRaises(ResultRefError) as ctx:
            resolve_result_refs(
                {"x": {"from_result": "r1", "field": "wrong"}}, self.STORE)
        self.assertIn("accounts", str(ctx.exception))

    def test_ordinary_args_pass_through_untouched(self):
        args = {"sql": "SELECT 1", "max_rows": 5, "nested": {"a": [1, 2]}}
        self.assertEqual(resolve_result_refs(args, {}), args)


class TreeSetProducerTests(unittest.TestCase):
    def setUp(self):
        cfg = load_config(str(ROOT / "config.yaml"))
        self.db = Database(cfg)
        self.runner = ReportRunner(TBEngine(self.db, cfg))

    def tearDown(self):
        self.db.close()

    def test_a_node_flattens_to_its_accounts(self):
        out = self.runner.node_accounts(node="EXPENSES")
        self.assertIn("6100", out["accounts"])
        self.assertGreaterEqual(out["account_count"], 5)
        self.assertIn("from_result", out["note"])

    def test_an_unknown_node_lists_the_real_ones(self):
        from pstb.report import ReportError

        with self.assertRaises(ReportError) as ctx:
            self.runner.node_accounts(node="NOPE")
        self.assertIn("EXPENSES", str(ctx.exception))


class EndToEndChainTests(unittest.IsolatedAsyncioTestCase):
    """The whole loop: produce in round 1, reference in round 2, and the
    exact values arrive at the database without the model typing one."""

    class Provider:
        name = "test"
        model = "scripted"

        def __init__(self):
            self.seen_results = []
            self.responses = [
                LLMResponse(tool_calls=[ToolCall(
                    id="a", name="get_tree_node_accounts",
                    args={"node": "EXPENSES"})]),
                None,   # placeholder — filled after we see r-id
                LLMResponse(text="Chained fine."),
            ]

        def send_user(self, text):
            return self.responses.pop(0)

        def send_tool_results(self, results):
            self.seen_results.append(results)
            if self.responses[0] is None:
                # Read the result_id the client stamped, as a model would.
                payload = json.loads(results[0].content)
                rid = payload["result_id"]
                self.responses[0] = LLMResponse(tool_calls=[ToolCall(
                    id="b", name="run_sql",
                    args={"sql": ("SELECT ACCOUNT, SUM(POSTED_TOTAL_AMT) AS amt "
                                  "FROM PS_LEDGER WHERE BUSINESS_UNIT = 'US001' "
                                  "AND ACCOUNT IN (:accts) GROUP BY ACCOUNT"),
                          "list_binds": {"accts": {"from_result": rid,
                                                   "field": "accounts"}}})])
            return self.responses.pop(0)

        def reset(self):
            pass

    class RealSession:
        """Drives the real engine, recording what run_sql actually received."""

        def __init__(self, engine, runner):
            self.engine = engine
            self.runner = runner
            self.run_sql_args = None

        async def call_tool(self, name, arguments):
            if name == "get_tree_node_accounts":
                out = self.runner.node_accounts(**arguments)
            elif name == "run_sql":
                self.run_sql_args = dict(arguments)
                out = self.engine.run_sql(**{
                    k: v for k, v in arguments.items()
                    if k in ("sql", "max_rows", "list_binds")})
            else:
                raise AssertionError(f"unexpected tool {name}")
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(out))],
                is_error=False)

    async def test_the_chain_carries_exact_values(self):
        cfg = load_config(str(ROOT / "config.yaml"))
        db = Database(cfg)
        try:
            engine = TBEngine(db, cfg)
            runner = ReportRunner(engine)
            expected = runner.node_accounts(node="EXPENSES")["accounts"]
            provider = self.Provider()
            session = self.RealSession(engine, runner)
            with patch("pstb.client.chat.MAX_NUDGES", 0):
                answer = await agent_turn(
                    provider, session,
                    "journal totals for every account in tree node EXPENSES",
                    surface="gui",
                    scope={"business_unit": "US001", "ledger": "ACTUALS"})
            self.assertIsNotNone(session.run_sql_args)
            arrived = session.run_sql_args["list_binds"]["accts"]
            self.assertEqual(
                arrived, expected,
                "the account set that reached run_sql differs from the "
                "producer's output — the handoff is not mechanical")
            self.assertIn("Chained", answer)
        finally:
            db.close()

    async def test_a_same_batch_reference_fails_with_guidance(self):
        # A reference to a result produced in the SAME batch cannot resolve
        # (its id does not exist until bookkeeping) — the error must teach
        # the two-round shape rather than crash the turn.
        cfg = load_config(str(ROOT / "config.yaml"))
        db = Database(cfg)
        try:
            engine = TBEngine(db, cfg)
            runner = ReportRunner(engine)

            provider_responses = [
                LLMResponse(tool_calls=[
                    ToolCall(id="a", name="get_tree_node_accounts",
                             args={"node": "EXPENSES"}),
                    ToolCall(id="b", name="run_sql",
                             args={"sql": "SELECT 1 FROM PS_LEDGER "
                                          "WHERE ACCOUNT IN (:a)",
                                   "list_binds": {"a": {
                                       "from_result": "r1",
                                       "field": "accounts"}}}),
                ]),
                LLMResponse(text="ok"),
            ]

            class P:
                name = "t"
                model = "m"
                seen = []

                def send_user(self, text):
                    return provider_responses.pop(0)

                def send_tool_results(self, results):
                    P.seen.append(results)
                    return provider_responses.pop(0)

                def reset(self):
                    pass

            session = self.RealSession(engine, runner)
            with patch("pstb.client.chat.MAX_NUDGES", 0):
                await agent_turn(
                    P(), session, "chain in one batch",
                    surface="gui",
                    scope={"business_unit": "US001", "ledger": "ACTUALS"})
            self.assertIsNone(session.run_sql_args,
                              "the unresolved reference still hit the engine")
            blocked = P.seen[0][1].content
            self.assertIn("NEXT round", blocked)
        finally:
            db.close()


class EngineListBindTests(unittest.TestCase):
    def setUp(self):
        cfg = load_config(str(ROOT / "config.yaml"))
        self.db = Database(cfg)
        self.engine = TBEngine(self.db, cfg)

    def tearDown(self):
        self.db.close()

    def test_in_list_expands_and_filters(self):
        out = self.engine.run_sql(
            "SELECT ACCOUNT, SUM(POSTED_TOTAL_AMT) AS amt FROM PS_LEDGER "
            "WHERE BUSINESS_UNIT = 'US001' AND ACCOUNT IN (:accts) "
            "GROUP BY ACCOUNT",
            list_binds={"accts": ["6000", "6100"]})
        self.assertEqual(sorted(r["account"] for r in out["rows"]),
                         ["6000", "6100"])

    def test_an_empty_set_refuses_rather_than_unfiltering(self):
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql(
                "SELECT 1 AS x FROM PS_LEDGER WHERE ACCOUNT IN (:a)",
                list_binds={"a": []})
        self.assertIn("NO values", str(ctx.exception))

    def test_an_unreferenced_list_refuses(self):
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql("SELECT 1 AS x FROM PS_LEDGER",
                                list_binds={"a": ["1"]})
        self.assertIn(":a", str(ctx.exception))

    def test_the_oracle_ceiling_is_enforced(self):
        with self.assertRaises(EngineError) as ctx:
            self.engine.run_sql(
                "SELECT 1 AS x FROM PS_LEDGER WHERE ACCOUNT IN (:a)",
                list_binds={"a": [str(i) for i in range(1001)]})
        self.assertIn("1000", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
