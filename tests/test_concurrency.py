"""Concurrent evidence gathering.

A model that emits a database call and a wiki call in one batch was paying
for them in sequence: an Oracle query and a Confluence fetch have no reason
to wait for one another, and on a WAN each is hundreds of milliseconds.
Tool batches now run concurrently in two phases — database first, wiki
after — because the mixed-intent gate decides the wiki's fate from the
database's OUTCOME, which does not exist until the first phase completes.

Assertions here are STRUCTURAL, not stopwatch-based: each fake tool records
its (start, end) interval, and the tests assert intervals overlap (ran
together) or are disjoint in order (the gate held). A wall-clock threshold
would flake on a loaded CI machine; interval arithmetic cannot.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.client.chat import agent_turn  # noqa: E402
from pstb.client.llm_base import LLMResponse, ToolCall  # noqa: E402


class ScriptedProvider:
    name = "test"
    model = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)

    def _next(self):
        if not self.responses:
            raise AssertionError("scripted provider ran out of responses")
        return self.responses.pop(0)

    def send_user(self, text):
        return self._next()

    def send_tool_results(self, results):
        self.tool_batches = getattr(self, "tool_batches", [])
        self.tool_batches.append(results)
        return self._next()

    def reset(self):
        pass


class TimedSession:
    """Fake MCP session that records each call's (start, end) interval."""

    def __init__(self, outputs, delay: float = 0.15):
        self.outputs = outputs
        self.delay = delay
        self.intervals: dict = {}

    async def call_tool(self, name, arguments):
        start = time.perf_counter()
        await asyncio.sleep(self.delay)
        self.intervals[name] = (start, time.perf_counter())
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(self.outputs[name]))],
            is_error=False,
        )


def call(call_id, name, **args):
    return ToolCall(id=call_id, name=name, args=args)


def overlap(a: tuple, b: tuple) -> bool:
    return a[0] < b[1] and b[0] < a[1]


class ConcurrentBatchTests(unittest.IsolatedAsyncioTestCase):
    SCOPE = {"business_unit": "US001", "ledger": "ACTUALS"}

    async def test_database_calls_in_one_batch_run_together(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[
                call("a", "get_trial_balance"),
                call("b", "get_ar_aging"),
            ]),
            LLMResponse(text="Both retrieved."),
        ])
        session = TimedSession({
            "get_trial_balance": {"scope_status": "ok",
                                  "totals": {"in_balance": True}},
            "get_ar_aging": {"scope_status": "ok", "buckets": []},
        })
        with patch("pstb.client.chat.MAX_NUDGES", 0):
            await agent_turn(provider, session, "show me the trial balance",
                             surface="gui", scope=self.SCOPE)
        a = session.intervals["get_trial_balance"]
        b = session.intervals["get_ar_aging"]
        self.assertTrue(overlap(a, b),
                        f"calls ran in sequence: {a} then {b} — the batch "
                        "is still serial")

    async def test_results_return_in_the_models_original_order(self):
        # Concurrency must not reorder the transcript: providers match
        # results to calls positionally as well as by id.
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[
                call("a", "get_trial_balance"),
                call("b", "get_ar_aging"),
                call("c", "list_periods"),
            ]),
            LLMResponse(text="done"),
        ])
        session = TimedSession({
            "get_trial_balance": {"scope_status": "ok"},
            "get_ar_aging": {"scope_status": "ok"},
            "list_periods": {"periods": []},
        }, delay=0.02)
        with patch("pstb.client.chat.MAX_NUDGES", 0):
            await agent_turn(provider, session, "show me the trial balance",
                             surface="gui", scope=self.SCOPE)
        returned = [r.name for r in provider.tool_batches[0]]
        self.assertEqual(returned, ["get_trial_balance", "get_ar_aging",
                                    "list_periods"])

    async def test_mixed_questions_still_gate_the_wiki_on_db_outcome(self):
        # The reason phases exist at all. The wiki call must START after the
        # financial call ENDS — its block decision reads the database's
        # outcome — and must then run unblocked because that outcome was
        # success.
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[
                call("w", "wiki_lookup", question="suspense policy"),
                call("d", "get_trial_balance"),
            ]),
            LLMResponse(text="Compared to policy, the balance is fine."),
        ])
        session = TimedSession({
            "get_trial_balance": {"scope_status": "ok",
                                  "totals": {"in_balance": True}},
            "wiki_lookup": {"passages": [{"text": "clear within 30 days",
                                          "term_coverage": 0.9}],
                            "relevance": "strong"},
        })
        with patch("pstb.client.chat.MAX_NUDGES", 0):
            await agent_turn(
                provider, session,
                "What is the policy threshold and current suspense balance?",
                surface="gui", scope=self.SCOPE)
        db = session.intervals["get_trial_balance"]
        wiki = session.intervals.get("wiki_lookup")
        self.assertIsNotNone(wiki, "the wiki call never executed — the gate "
                                   "blocked it despite DB success")
        self.assertGreaterEqual(wiki[0], db[1],
                                "the wiki ran before the database finished; "
                                "the mixed gate would be deciding from an "
                                "outcome that does not exist yet")

    async def test_mixed_gate_still_blocks_wiki_when_the_db_fails(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[
                call("d", "get_trial_balance"),
                call("w", "wiki_lookup", question="suspense policy"),
            ]),
            LLMResponse(text="Could not obtain the balance."),
        ])
        session = TimedSession({
            "get_trial_balance": {"scope_status": "no_data",
                                  "detail": "no rows for scope"},
            "wiki_lookup": {"passages": [{"text": "x"}]},
        })
        with patch("pstb.client.chat.MAX_NUDGES", 0):
            await agent_turn(
                provider, session,
                "What is the policy threshold and current suspense balance?",
                surface="gui", scope=self.SCOPE)
        self.assertNotIn("wiki_lookup", session.intervals,
                         "the wiki executed although the financial evidence "
                         "failed — concurrency loosened the gate")


class ParallelPageFetchTests(unittest.TestCase):
    def test_lookup_fetches_pages_together_not_in_sequence(self):
        from pstb import wiki as wiki_mod

        wiki_mod.clear_lookup_cache()

        class SlowWiki:
            provider_name = "fake"
            DELAY = 0.15

            def __init__(self):
                self.intervals = {}

            def health(self):
                return {"is_bundled_demo_content": False}

            def search(self, query, limit=8):
                return [{"id": f"p{i}", "title": f"Page {i}", "text": ""}
                        for i in range(3)]

            def get_page(self, page_id):
                start = time.perf_counter()
                time.sleep(self.DELAY)
                self.intervals[page_id] = (start, time.perf_counter())
                return {"id": page_id, "title": f"Title {page_id}",
                        "text": ("integration spec body for " + page_id + " ")
                        * 30}

        w = SlowWiki()
        out = wiki_mod.lookup(w, "integration spec")
        spans = list(w.intervals.values())
        self.assertEqual(len(spans), 3)
        joint = max(e for _, e in spans) - min(s for s, _ in spans)
        total = sum(e - s for s, e in spans)
        self.assertLess(joint, total * 0.8,
                        f"three {SlowWiki.DELAY}s fetches spanned {joint:.2f}s "
                        f"against {total:.2f}s of work — they ran in sequence")
        # Order and content survive the parallel fetch.
        self.assertEqual([s["id"] for s in out["sources"]],
                         ["p0", "p1", "p2"])
        self.assertTrue(out["passages"])

    def test_a_failing_page_is_reported_and_the_rest_still_arrive(self):
        from pstb import wiki as wiki_mod

        wiki_mod.clear_lookup_cache()

        class OneBadPage:
            provider_name = "fake2"

            def health(self):
                return {"is_bundled_demo_content": False}

            def search(self, query, limit=8):
                return [{"id": "good1", "title": "Good 1", "text": ""},
                        {"id": "bad", "title": "Bad", "text": ""},
                        {"id": "good2", "title": "Good 2", "text": ""}]

            def get_page(self, page_id):
                if page_id == "bad":
                    raise RuntimeError("HTTP 500")
                return {"id": page_id, "title": page_id,
                        "text": "useful spec content " * 40}

        out = wiki_mod.lookup(OneBadPage(), "spec")
        errors = [s for s in out["sources"] if s.get("error")]
        self.assertEqual(len(errors), 1)
        self.assertIn("HTTP 500", errors[0]["error"])
        good = [s for s in out["sources"] if not s.get("error")]
        self.assertEqual(len(good), 2,
                         "one unreadable page cost the readable ones")


if __name__ == "__main__":
    unittest.main()


class ConcurrentPlaybookContextTests(unittest.TestCase):
    """close_readiness gathers its four inputs together, not in sequence.

    The inputs are independent — integrity reads the ledger, aging reads AR,
    the workbench reads billing — and running them serially made the playbook
    cost the SUM of the site's slowest queries, which is what a close-
    readiness timeout on a real instance is made of.
    """

    def _runner_with_slow_inputs(self, delay=0.15):
        from pstb.config import load_config
        from pstb.db import Database
        from pstb.engine import TBEngine
        from pstb.ar import ARBilling
        from pstb.playbooks import PlaybookRunner

        cfg = load_config(str(ROOT / "config.yaml"))
        db = Database(cfg)
        engine = TBEngine(db, cfg)
        ar = ARBilling(engine)
        intervals = {}

        def slow(name, fn):
            def wrapper(*a, **k):
                start = time.perf_counter()
                time.sleep(delay)
                out = fn(*a, **k)
                intervals[name] = (start, time.perf_counter())
                return out
            return wrapper

        engine.tb_integrity_check = slow("integrity", engine.tb_integrity_check)
        ar.aging = slow("aging", ar.aging)
        ar.billing_workbench = slow("workbench", ar.billing_workbench)
        return PlaybookRunner(engine, ar), intervals, db

    def test_the_inputs_overlap(self):
        runner, intervals, db = self._runner_with_slow_inputs()
        try:
            result = runner.run("close_readiness")
        finally:
            db.close()
        self.assertEqual(len(intervals), 3)
        spans = list(intervals.values())
        joint = max(e for _, e in spans) - min(s for s, _ in spans)
        total = sum(e - s for s, e in spans)
        self.assertLess(joint, total * 0.8,
                        f"inputs spanned {joint:.2f}s against {total:.2f}s of "
                        "work — the context gather is serial again")
        self.assertEqual(result["verdict"], "exceptions_found")

    def test_concurrency_changes_no_step_outcome(self):
        # The whole point of a deterministic playbook is that HOW it runs
        # never changes WHAT it reports.
        from pstb.config import load_config
        from pstb.db import Database
        from pstb.engine import TBEngine
        from pstb.ar import ARBilling
        from pstb.playbooks import PlaybookRunner

        cfg = load_config(str(ROOT / "config.yaml"))
        db = Database(cfg)
        try:
            engine = TBEngine(db, cfg)
            plain = PlaybookRunner(engine, ARBilling(engine)).run(
                "close_readiness")
        finally:
            db.close()
        runner, _, db2 = self._runner_with_slow_inputs(delay=0.05)
        try:
            slowed = runner.run("close_readiness")
        finally:
            db2.close()
        self.assertEqual(
            [(s["step"], s["status"]) for s in plain["steps"]],
            [(s["step"], s["status"]) for s in slowed["steps"]])

    def test_a_slow_run_names_its_culprit(self):
        runner, _, db = self._runner_with_slow_inputs(delay=0.05)
        try:
            result = runner.run("close_readiness")
        finally:
            db.close()
        timings = result["input_timings_ms"]
        self.assertEqual(
            set(timings), {"integrity", "aging", "workbench", "latest_posted"})
        # The stubs slept, so their inputs must report measurable time.
        self.assertGreaterEqual(timings["integrity"], 50)
        # Integrity itself itemises: the aggregate and each control probe.
        balance = next(s for s in result["steps"] if s["step"] == "balance")
        probes = balance["detail"]["probe_timings_ms"]
        self.assertIn("tb_aggregate", probes)
        self.assertIn("out_of_balance_journals", probes)


class ToolStartedHookTests(unittest.IsolatedAsyncioTestCase):
    """The UI can only say "running get_trial_balance" if a hook fires at
    DISPATCH — the completion observer alone leaves a 40s query mute."""

    async def test_started_fires_before_completion(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[call("a", "get_trial_balance")]),
            LLMResponse(text="ok"),
        ])
        session = TimedSession({
            "get_trial_balance": {"scope_status": "ok"}}, delay=0.05)
        events = []
        with patch("pstb.client.chat.MAX_NUDGES", 0):
            await agent_turn(
                provider, session, "show me the trial balance",
                surface="gui",
                scope={"business_unit": "US001", "ledger": "ACTUALS"},
                tool_observer=lambda n, a, o, ms, ok: events.append(
                    ("done", n)),
                tool_started=lambda n, a, blocked: events.append(
                    ("started", n, blocked)))
        self.assertEqual(events[0], ("started", "get_trial_balance", False))
        self.assertEqual(events[1], ("done", "get_trial_balance"))

    async def test_a_crashing_hook_cannot_break_the_turn(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[call("a", "get_trial_balance")]),
            LLMResponse(text="fine"),
        ])
        session = TimedSession({
            "get_trial_balance": {"scope_status": "ok"}}, delay=0.02)

        def bomb(*a):
            raise RuntimeError("observer crashed")

        with patch("pstb.client.chat.MAX_NUDGES", 0):
            answer = await agent_turn(
                provider, session, "show me the trial balance",
                surface="gui",
                scope={"business_unit": "US001", "ledger": "ACTUALS"},
                tool_started=bomb)
        self.assertIn("fine", answer)
