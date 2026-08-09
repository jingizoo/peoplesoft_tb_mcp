"""What the Anthropic SDK actually puts on the wire, offline.

The other Claude tests replace the SDK with a stub, which proves the
provider's own logic and nothing about the SDK. This one keeps the real
`anthropic` client and swaps only the HTTP transport, so the request is
genuinely serialised and a genuine SSE stream is parsed back.

That is the difference that matters when a version moves: a parameter this
SDK does not accept (output_config, thinking, fallbacks="default") fails
here, at the shape, rather than on the box with a live key. Skipped when
the optional package is absent — CI installs only the GUI extra.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import anthropic
    import httpx
except ImportError:  # pragma: no cover - optional extra
    anthropic = None

from pstb.client.llm_base import ToolResult, ToolSpec  # noqa: E402
from pstb.config import Config  # noqa: E402

TOOLS = [
    ToolSpec(name="get_trial_balance", description="Trial balance.",
             schema={"type": "object",
                     "properties": {"business_unit": {"type": "string"}},
                     "required": ["business_unit"]}),
    ToolSpec(name="list_financial_scopes", description="Scopes.", schema={}),
]


def _sse(events) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(body)}\n\n"
                   for name, body in events)


def _start(msg_id):
    return ("message_start", {"type": "message_start", "message": {
        "id": msg_id, "type": "message", "role": "assistant",
        "model": "claude-opus-5", "content": [], "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 0}}})


def _end(stop_reason):
    return [("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": stop_reason,
                                         "stop_sequence": None},
                               "usage": {"output_tokens": 12}}),
            ("message_stop", {"type": "message_stop"})]


# A thinking block then a tool call, exactly as a real turn arrives.
TOOL_TURN = _sse([
    _start("msg_1"),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "thinking",
                                               "thinking": "",
                                               "signature": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "thinking_delta",
                                       "thinking": "check the ledger"}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "signature_delta",
                                       "signature": "sig-abc"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("content_block_start", {"type": "content_block_start", "index": 1,
                             "content_block": {"type": "tool_use",
                                               "id": "toolu_01",
                                               "name": "get_trial_balance",
                                               "input": {}}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 1,
                             "delta": {"type": "input_json_delta",
                                       "partial_json":
                                       '{"business_unit": "US001"}'}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
    *_end("tool_use"),
])

TEXT_TURN = _sse([
    _start("msg_2"),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta",
                                       "text": "It balances."}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    *_end("end_turn"),
])


@unittest.skipUnless(anthropic is not None,
                     "optional extra: pip install -e '.[llm]'")
class WireShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        from pstb.client.llm_claude import ClaudeProvider

        self.sent: list = []
        replies = [TOOL_TURN, TEXT_TURN]

        def handle(request):
            self.sent.append((str(request.url), dict(request.headers),
                              json.loads(request.content)))
            body = replies[min(len(self.sent) - 1, len(replies) - 1)]
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=body)

        real = anthropic.Anthropic

        def patched(**kwargs):
            kwargs.setdefault("api_key", "sk-ant-not-a-real-key")
            kwargs["http_client"] = httpx.Client(
                transport=httpx.MockTransport(handle))
            return real(**kwargs)

        self.addCleanup(setattr, anthropic, "Anthropic", real)
        anthropic.Anthropic = patched
        cfg = Config.sample(ROOT)
        self.provider = ClaudeProvider(cfg, "SYSTEM PROMPT " * 200, TOOLS)

    def test_a_tool_round_trips_through_a_real_stream(self) -> None:
        first = self.provider.send_user("Does the trial balance balance?")
        self.assertEqual([c.name for c in first.tool_calls],
                         ["get_trial_balance"])
        # Streamed input_json_delta must reassemble into real arguments;
        # a partial JSON string reaching the MCP call would be a silent
        # wrong-answer bug, not an error.
        self.assertEqual(first.tool_calls[0].args,
                         {"business_unit": "US001"})
        second = self.provider.send_tool_results(
            [ToolResult(call_id=first.tool_calls[0].id,
                        name="get_trial_balance",
                        content='{"balanced": true}')])
        self.assertEqual(second.text, "It balances.")

    def test_the_sdk_accepts_every_parameter_this_provider_sends(self) -> None:
        self.provider.send_user("q")
        url, headers, body = self.sent[0]
        self.assertIn("beta=true", url)
        self.assertEqual(headers.get("anthropic-beta"),
                         "server-side-fallback-2026-07-01")
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertEqual(body["output_config"], {"effort": "high"})
        self.assertEqual(body["tool_choice"], {"type": "any"})
        self.assertTrue(body["stream"])
        # Opus 5 rejects these outright.
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, body)

    def test_the_thinking_signature_survives_the_round_trip(self) -> None:
        first = self.provider.send_user("q")
        self.provider.send_tool_results(
            [ToolResult(call_id=first.tool_calls[0].id,
                        name="get_trial_balance", content="{}")])
        _, _, body = self.sent[1]
        roles = [(m["role"], [b["type"] for b in m["content"]])
                 for m in body["messages"]]
        self.assertEqual(roles, [("user", ["text"]),
                                 ("assistant", ["thinking", "tool_use"]),
                                 ("user", ["tool_result"])])
        self.assertEqual(body["messages"][1]["content"][0]["signature"],
                         "sig-abc")


if __name__ == "__main__":
    unittest.main()
