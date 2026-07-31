"""Focused tests for DB-first evidence routing and request-scope enforcement."""
from __future__ import annotations

import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pstb.client.chat import agent_turn
from pstb.client.llm_base import LLMResponse, ToolCall
from pstb.guards import (
    ScopeConflict,
    apply_request_scope,
    evidence_intent,
    financial_tool_is_relevant,
    normalize_request_scope,
    question_financial_domains,
)


class ScriptedProvider:
    name = "test"
    model = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.user_messages = []
        self.tool_batches = []

    def _next(self):
        if not self.responses:
            raise AssertionError("scripted provider ran out of responses")
        return self.responses.pop(0)

    def send_user(self, text):
        self.user_messages.append(text)
        return self._next()

    def send_tool_results(self, results):
        self.tool_batches.append(results)
        return self._next()

    def reset(self):
        pass


class FakeSession:
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        value = self.outputs[name]
        if isinstance(value, list):
            value = value.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(value))],
            is_error=False,
        )


def call(call_id, name, **args):
    return ToolCall(id=call_id, name=name, args=args)


class RequestScopeTests(unittest.TestCase):
    def test_scope_aliases_are_normalized_and_injected(self):
        scope = normalize_request_scope({
            "bu": " US200 ", "ledger": "ACTUALS", "fy": "2026", "per": "6",
        })
        self.assertEqual(scope, {
            "business_unit": "US200",
            "ledger": "ACTUALS",
            "fiscal_year": 2026,
            "period": 6,
        })
        args = apply_request_scope(
            "get_account_balance", {"account": "1000"}, scope
        )
        self.assertEqual(args["business_unit"], "US200")
        self.assertEqual(args["ledger"], "ACTUALS")
        self.assertEqual(args["fiscal_year"], 2026)
        self.assertEqual(args["through_period"], 6)
        customer_args = apply_request_scope(
            "search_customers", {"query": "ACME"}, scope
        )
        self.assertEqual(customer_args["business_unit"], "US200")

    def test_conflicting_model_scope_is_rejected(self):
        with self.assertRaisesRegex(ScopeConflict, "REQUEST|conflicts"):
            apply_request_scope(
                "get_trial_balance",
                {"business_unit": "US999"},
                {"business_unit": "US200"},
            )

    def test_full_scope_discovery_is_never_constrained(self):
        args = {"include_inactive": True}
        self.assertEqual(
            apply_request_scope(
                "list_financial_scopes",
                args,
                {"business_unit": "US200", "ledger": "ACTUALS"},
            ),
            args,
        )

    def test_raw_sql_cannot_bypass_a_selected_scope(self):
        with self.assertRaisesRegex(ScopeConflict, "cannot be proven"):
            apply_request_scope(
                "run_sql",
                {"sql": "SELECT * FROM PS_LEDGER"},
                {"business_unit": "US200", "ledger": "ACTUALS"},
            )

    def test_intent_split(self):
        self.assertEqual(evidence_intent("What is our capitalization policy?"), "policy")
        self.assertEqual(evidence_intent("Show all business units"), "data")
        self.assertEqual(evidence_intent("Which BUs exist?"), "data")
        self.assertEqual(
            evidence_intent("What is the suspense balance threshold?"), "mixed"
        )
        self.assertEqual(
            evidence_intent(
                "What is the policy threshold and current suspense balance?"
            ),
            "mixed",
        )
        self.assertEqual(
            evidence_intent(
                "What is the policy threshold and FY2025 suspense balance?"
            ),
            "mixed",
        )
        self.assertEqual(
            evidence_intent(
                "Explain the policy and show the suspense balance for period 6."
            ),
            "mixed",
        )
        self.assertEqual(
            evidence_intent(
                "What is the threshold and account 1999 balance?"
            ),
            "mixed",
        )
        self.assertEqual(
            evidence_intent("Is the suspense balance within policy?"), "mixed"
        )
        self.assertEqual(
            evidence_intent("Are there unposted journals?"), "data"
        )
        self.assertEqual(
            evidence_intent("Give me the suspense balance and policy threshold."),
            "mixed",
        )
        self.assertEqual(
            evidence_intent("Tell me the invoice amount and approval rule."),
            "mixed",
        )
        # A domain noun inside a policy question does not make it a data
        # question. Classifying these as "mixed" blocked wiki_lookup until a
        # financial tool succeeded — and since no financial tool can answer
        # "must invoices be approved", the user got a refusal with the wiki
        # never called. The rule lives in the wiki; there is no figure to get.
        self.assertEqual(
            evidence_intent("Must invoices be approved?"),
            "policy",
        )
        self.assertEqual(
            evidence_intent("Are journal approvals required?"),
            "policy",
        )
        self.assertEqual(
            evidence_intent("Who is allowed to approve journals?"),
            "policy",
        )
        self.assertEqual(evidence_intent("Who owes us?"), "data")
        self.assertEqual(
            evidence_intent("How much is due from ACME?"), "data"
        )
        self.assertEqual(
            evidence_intent("How much did we earn?"), "data"
        )

    def test_financial_domains_require_complete_relevant_coverage(self):
        self.assertEqual(
            question_financial_domains("What is customer ACME balance?"),
            {"customer", "balance"},
        )
        self.assertTrue(
            financial_tool_is_relevant(
                "get_customer_ar", "What is customer ACME balance?"
            )
        )
        self.assertFalse(
            financial_tool_is_relevant(
                "get_trial_balance", "What is customer ACME balance?"
            )
        )
        self.assertTrue(
            financial_tool_is_relevant(
                "get_top_billing_customers", "Show the top 20 customers"
            )
        )
        composite = "What is current cash balance converted to CAD currency?"
        self.assertEqual(
            question_financial_domains(composite), {"balance", "fx"}
        )
        self.assertFalse(
            financial_tool_is_relevant("get_exchange_rate", composite)
        )


class EvidenceRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_synchronous_provider_call_does_not_block_event_loop(self):
        class SlowProvider:
            name = "slow"
            model = "test"

            def send_user(self, _text):
                time.sleep(0.05)
                return LLMResponse(text="Hello.")

        task = asyncio.create_task(
            agent_turn(SlowProvider(), FakeSession({}), "hello")
        )
        await asyncio.sleep(0.005)
        self.assertFalse(task.done())
        self.assertEqual(await task, "Hello.")

    async def test_unscoped_gui_cannot_use_configured_default(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[
                call("d1", "get_trial_balance"),
            ]),
            LLMResponse(text="The default BU balance is 999.00."),
        ])
        session = FakeSession({
            "get_trial_balance": {
                "scope_status": "ok",
                "totals": {"ending": 999.0},
            },
        })

        with patch("pstb.client.chat.MAX_NUDGES", 0):
            answer = await agent_turn(
                provider,
                session,
                "How much did we earn?",
                surface="gui",
                scope=None,
            )

        self.assertEqual(session.calls, [])
        self.assertNotIn("999.00", answer)
        self.assertIn("Choose a PeopleSoft business unit", answer)

    async def test_composite_financial_ask_requires_all_domains(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[
                call(
                    "d1",
                    "get_exchange_rate",
                    from_currency="USD",
                    to_currency="CAD",
                ),
            ]),
            LLMResponse(text="The CAD cash balance is 999.00."),
        ])
        session = FakeSession({
            "get_exchange_rate": {
                "from_currency": "USD",
                "to_currency": "CAD",
                "rate": 1.37,
            },
        })

        with patch("pstb.client.chat.MAX_NUDGES", 0):
            answer = await agent_turn(
                provider,
                session,
                "What is current cash balance converted to CAD currency?",
                scope={
                    "business_unit": "US200",
                    "ledger": "ACTUALS",
                    "fiscal_year": 2026,
                    "period": 6,
                },
            )

        self.assertNotIn("999.00", answer)
        self.assertIn("could not obtain a successful PeopleSoft", answer)

    async def test_metadata_tool_cannot_ground_a_balance_answer(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[
                call("d1", "resolve_period", date="today"),
            ]),
            LLMResponse(text="The balance is 999.00."),
        ])
        session = FakeSession({
            "resolve_period": {"fiscal_year": 2026, "period": 6},
        })

        with patch("pstb.client.chat.MAX_NUDGES", 0):
            answer = await agent_turn(
                provider,
                session,
                "What is the current cash balance?",
                scope={
                    "business_unit": "US200",
                    "ledger": "ACTUALS",
                    "fiscal_year": 2026,
                    "period": 6,
                },
            )

        self.assertEqual(
            [name for name, _ in session.calls], ["resolve_period"]
        )
        self.assertNotIn("999.00", answer)
        self.assertIn("could not obtain a successful PeopleSoft", answer)

    async def test_irrelevant_financial_tool_cannot_ground_a_balance_answer(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[
                call(
                    "d1",
                    "get_exchange_rate",
                    from_currency="USD",
                    to_currency="CAD",
                ),
            ]),
            LLMResponse(text="The current cash balance is 999.00."),
        ])
        session = FakeSession({
            "get_exchange_rate": {
                "from_currency": "USD",
                "to_currency": "CAD",
                "rate": 1.37,
            },
        })

        with patch("pstb.client.chat.MAX_NUDGES", 0):
            answer = await agent_turn(
                provider,
                session,
                "What is the current cash balance?",
                scope={
                    "business_unit": "US200",
                    "ledger": "ACTUALS",
                    "fiscal_year": 2026,
                    "period": 6,
                },
            )

        self.assertEqual(
            [name for name, _ in session.calls], ["get_exchange_rate"]
        )
        self.assertNotIn("999.00", answer)
        self.assertIn("could not obtain a successful PeopleSoft", answer)

    async def test_mixed_batch_runs_db_before_wiki_and_injects_scope(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[
                call("w1", "wiki_lookup", question="suspense threshold"),
                call("d1", "get_trial_balance", account="1999"),
            ]),
            LLMResponse(text="The retrieved balance is within policy."),
        ])
        session = FakeSession({
            "get_trial_balance": {
                "scope_status": "ok",
                "rows": [{"account": "1999", "ending": 50.0}],
                "totals": {"ending": 50.0},
            },
            "wiki_lookup": {
                "passages": [{"page": "Suspense policy", "text": "Limit is 100."}]
            },
        })

        observed = []
        answer = await agent_turn(
            provider,
            session,
            "Is the suspense balance within policy?",
            scope={
                "business_unit": "US200",
                "ledger": "ACTUALS",
                "fiscal_year": 2026,
                "period": 6,
            },
            tool_observer=lambda *values: observed.append(values),
        )

        self.assertEqual(
            [name for name, _ in session.calls],
            ["get_trial_balance", "wiki_lookup"],
        )
        db_args = session.calls[0][1]
        self.assertEqual(
            {
                key: db_args[key]
                for key in ("business_unit", "ledger", "fiscal_year", "period")
            },
            {
                "business_unit": "US200",
                "ledger": "ACTUALS",
                "fiscal_year": 2026,
                "period": 6,
            },
        )
        # Function results are returned in the original model call order even
        # though execution was DB-first.
        self.assertEqual(
            [result.name for result in provider.tool_batches[0]],
            ["wiki_lookup", "get_trial_balance"],
        )
        self.assertEqual(answer, "The retrieved balance is within policy.")
        self.assertEqual([item[0] for item in observed],
                         ["get_trial_balance", "wiki_lookup"])
        self.assertEqual(observed[0][1]["business_unit"], "US200")
        self.assertEqual(
            session.calls[1][1]["question"],
            "Is the suspense balance within policy?",
        )
        self.assertTrue(all(item[4] for item in observed))

    async def test_db_no_data_blocks_wiki_and_replaces_verdict(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[
                call("w1", "wiki_lookup", question="suspense threshold"),
                call("d1", "get_trial_balance", account="1999"),
            ]),
            LLMResponse(text="It is compliant."),
        ])
        session = FakeSession({
            "get_trial_balance": {
                "scope_status": "business_unit_not_found",
                "detail": "Business unit NOPE does not exist.",
                "rows": [],
                "note": "NO DATA — not a zero balance.",
            },
            "wiki_lookup": {
                "passages": [{"page": "Policy", "text": "Limit is 100."}]
            },
        })

        with patch("pstb.client.chat.MAX_NUDGES", 0):
            answer = await agent_turn(
                provider,
                session,
                "Is the suspense balance compliant with policy?",
                scope={"business_unit": "NOPE"},
            )

        self.assertEqual([name for name, _ in session.calls], ["get_trial_balance"])
        wiki_result = provider.tool_batches[0][0]
        self.assertEqual(wiki_result.name, "wiki_lookup")
        self.assertEqual(json.loads(wiki_result.content)["evidence_gate"], "blocked")
        self.assertIn("could not obtain successful PeopleSoft", answer)
        self.assertNotIn("It is compliant", answer)

    async def test_pure_policy_can_go_directly_to_wiki(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[
                call("w1", "wiki_lookup", question="capitalization policy"),
            ]),
            LLMResponse(text="The policy passage above gives the threshold."),
        ])
        session = FakeSession({
            "wiki_lookup": {
                "passages": [
                    {"page": "Capitalization", "text": "Approved rule text."}
                ]
            },
        })

        answer = await agent_turn(
            provider, session, "What is our capitalization policy?"
        )

        self.assertEqual([name for name, _ in session.calls], ["wiki_lookup"])
        self.assertEqual(
            session.calls[0][1]["question"],
            "What is our capitalization policy?",
        )
        self.assertEqual(answer, "The policy passage above gives the threshold.")

    async def test_data_only_blocks_wiki_but_allows_unscoped_catalog(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[
                call("w1", "wiki_lookup", question="business units"),
                call("d1", "list_financial_scopes"),
            ]),
            LLMResponse(text="The available scopes are shown above."),
        ])
        session = FakeSession({
            "wiki_lookup": {
                "passages": [{"page": "Org", "text": "An organizational page."}]
            },
            "list_financial_scopes": {
                "scopes": [
                    {"business_unit": "US100"},
                    {"business_unit": "US200"},
                ]
            },
        })

        answer = await agent_turn(
            provider,
            session,
            "What are all the business units?",
            scope={"business_unit": "US100", "ledger": "ACTUALS"},
        )

        self.assertEqual(session.calls, [("list_financial_scopes", {})])
        self.assertEqual(answer, "The available scopes are shown above.")
        blocked = json.loads(provider.tool_batches[0][0].content)
        self.assertEqual(blocked["evidence_gate"], "blocked")


if __name__ == "__main__":
    unittest.main()
