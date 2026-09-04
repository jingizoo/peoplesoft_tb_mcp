"""The two arms of the provable-answers harness, and nothing shared.

The pstb arm runs the REAL agent loop -- prompt, tools, guards -- and
observes it through the runtime's own public contract: a throwaway
QuestionLog written into the report's owner-only directory, whose turn
record carries the guard pseudo-calls (`_number_guard`) with their
names intact and whose quality record carries the runtime's own
groundedness verdict. The harness then audits that verdict by
recomputing ungrounded_figures over the payloads it observed itself:
the guard is measured, never trusted -- a disagreement is a
harness-integrity error that fails the run, not a scoring datum.

The raw arm is one prompt in, one text out, through the same
one_shot_completion the product uses for provider-neutral text. It
gets a checked-in NEUTRAL prompt variant, never the tool-era scope
block -- handing a tool-free model instructions about tools it does
not have is entrapment, and the numbers it produced would say nothing.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .runner import (_decoded_result, _grade, _result_values,
                     _runtime_profile, _runtime_scope)


class HarnessIntegrityError(RuntimeError):
    """The instrument disagreed with itself; no score may be reported."""


def integrity_audit(case_id: str, answer: str, payloads, *,
                    guard_withheld: bool, status: str) -> list:
    """The runtime's verdict against this harness's own recompute.

    "passed" with figures the recompute cannot ground, or a fired
    guard without a "blocked" verdict, means the instrument is broken
    -- raise rather than score. Returns the recomputed ungrounded list
    so the scorer audits the guard instead of trusting it.
    """
    from pstb.guards import ungrounded_figures
    recomputed = ungrounded_figures(answer, payloads)
    if status == "passed" and recomputed:
        raise HarnessIntegrityError(
            f"runtime groundedness said passed on {case_id!r} but the "
            f"recompute found {len(recomputed)} unsupported figure(s)")
    if guard_withheld and status and status != "blocked":
        raise HarnessIntegrityError(
            f"the number guard fired on {case_id!r} but the quality "
            f"record says {status!r}")
    return recomputed


def _provider_class(provider_name: str):
    if provider_name == "gemini":
        from pstb.client.llm_gemini import GeminiVertexProvider as P
    elif provider_name == "claude":
        from pstb.client.llm_claude import ClaudeProvider as P
    else:
        from pstb.client.llm_ollama import OllamaProvider as P
    return P


def _read_qlog_records(path: Path) -> tuple[dict, dict]:
    """The turn and quality records of the single turn just run."""
    turn, quality = {}, {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") == "turn":
                turn = record
            elif record.get("type") == "quality":
                quality = record
    return turn, quality


async def run_pstb_arm(session, cfg, provider_name: str, case: dict, *,
                       report_dir: Path,
                       provider_factory=None) -> dict:
    """One case through the real loop, observed via a throwaway qlog.

    ``provider_factory`` (CI mode) builds the provider from
    (prompt, tools) -- a TemplatedScriptedProvider over a transcript;
    omitted (box mode) the real provider for ``provider_name`` is used.
    The temp qlog lives inside the report's own 0o600 directory and is
    deleted before this function returns, pass or fail.
    """
    from pstb.client.chat import agent_turn, tool_specs
    from pstb.qlog import QuestionLog

    all_tools = tool_specs(await session.list_tools())
    scope = _runtime_scope(case)
    runtime_case = {**case, "scope": scope}
    prompt, tools = _runtime_profile(cfg, provider_name, runtime_case,
                                     all_tools)

    seen: list = []
    payloads: list = []

    def observe(name, args, out, ms, ok):
        decoded = _decoded_result(out)
        seen.append({"tool": name, "args": dict(args or {}), "ok": ok,
                     "ms": ms, "_result": decoded})
        if ok:
            payloads.append(decoded)

    if provider_factory is not None:
        # CI: a scripted transcript that resolves its figure placeholders
        # from the very list the observer fills -- real payloads only.
        provider = provider_factory(prompt, tools, seen)
    else:
        provider = _provider_class(provider_name)(cfg, prompt, tools)

    qdir = Path(report_dir) / f".qlog-{case.get('id') or 'case'}"
    qdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    started = time.time()
    try:
        qlog = QuestionLog("questions.jsonl", qdir)
        answer = await agent_turn(
            provider, session, case["question"], surface="gui",
            scope=scope, tool_observer=observe, qlog=qlog)
        turn_record, quality_record = _read_qlog_records(
            qdir / "questions.jsonl")
    finally:
        shutil.rmtree(qdir, ignore_errors=True)

    logged_tools = [str(t.get("tool") or "")
                    for t in turn_record.get("tools") or []]
    guard_withheld = "_number_guard" in logged_tools
    groundedness = (quality_record.get("groundedness")
                    if isinstance(quality_record.get("groundedness"), dict)
                    else {})
    status = str(groundedness.get("status") or "")
    recomputed = integrity_audit(
        str(case.get("id") or ""), answer, payloads,
        guard_withheld=guard_withheld, status=status)

    return {
        "id": str(case.get("id") or ""),
        "answer": answer,
        "calls": seen,
        "payloads": payloads,
        "grade_problems": _grade(runtime_case, answer, seen),
        "guard_withheld": guard_withheld,
        "groundedness_status": status,
        "seconds": round(time.time() - started, 1),
    }


def run_raw_arm(cfg, provider_name: str, prompt_text: str,
                question: str) -> dict:
    """The same question, verbatim, to a tool-free model."""
    from pstb.client.chat import one_shot_completion
    started = time.time()
    text, provider, model = one_shot_completion(
        cfg, provider_name, prompt_text, question)
    return {"answer": str(text or ""), "provider": provider,
            "model": model, "seconds": round(time.time() - started, 1)}


class TranscriptExhausted(AssertionError):
    """The scripted transcript and the loop disagreed about length."""


class TemplatedScriptedProvider:
    """A scripted provider whose answers quote the REAL run's payloads.

    CI transcripts must contain figures, and frozen figures rot with
    every reseed. So a text step may embed ``{{tool.path.to.value}}``
    placeholders, resolved at answer time from the payloads actually
    observed in THIS run (latest successful call of that tool, walked
    with the grading contract's own path grammar). Figures in scripted
    answers are therefore always real, and a payload-shape drift fails
    loudly as an unresolved placeholder instead of silently pinning a
    stale number.
    """

    name = "scripted"
    model = "transcript"

    def __init__(self, steps: list, observed: list):
        from pstb.client.llm_base import LLMResponse, ToolCall
        self._responses = []
        self._observed = observed        # shared with the tool observer
        for step in steps:
            if "text" in step:
                self._responses.append(("text", str(step["text"])))
            else:
                calls = [ToolCall(id=f"c{n}", name=item["name"],
                                  args=dict(item.get("args") or {}))
                         for n, item in enumerate(step["tool_calls"])]
                self._responses.append(
                    ("calls", LLMResponse(tool_calls=calls)))

    def _resolve(self, text: str) -> str:
        from pstb.client.llm_base import LLMResponse
        import re
        def sub(match):
            spec = match.group(1)
            tool, _, path = spec.partition(".")
            for call in reversed(self._observed):
                if call.get("tool") == tool and call.get("ok"):
                    values = _result_values(call.get("_result"), path)
                    if len(values) == 1:
                        value = values[0]
                        if isinstance(value, float):
                            return f"{value:,.2f}"
                        return str(value)
            raise TranscriptExhausted(
                f"transcript placeholder {{{{{spec}}}}} did not resolve "
                "against any observed payload -- the transcript and the "
                "server have drifted apart")
        resolved = re.sub(r"\{\{([^{}]+)\}\}", sub, text)
        return LLMResponse(text=resolved)

    def _next(self):
        if not self._responses:
            raise TranscriptExhausted(
                "the scripted transcript ran out of responses")
        kind, item = self._responses.pop(0)
        return self._resolve(item) if kind == "text" else item

    def send_user(self, _text):
        return self._next()

    def send_tool_results(self, _results):
        return self._next()

    def reset(self):
        self._responses = []
