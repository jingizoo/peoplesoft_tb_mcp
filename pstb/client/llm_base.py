"""Provider-agnostic LLM interface for the tool-calling chat loop.

Each provider keeps its own native transcript (no lossy translation between
formats); the chat loop only sees ToolCall/ToolResult/LLMResponse.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

SCHEMA_DROP_KEYS = {"title", "default", "$schema", "$defs", "additionalProperties", "examples"}


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


def clean_schema(node: Any) -> Any:
    """Strip JSON-schema keys that trip up provider validators (Gemini in
    particular); collapse anyOf to its first non-null branch."""
    if isinstance(node, list):
        return [clean_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    node = {k: v for k, v in node.items() if k not in SCHEMA_DROP_KEYS}
    if "anyOf" in node:
        branches = [b for b in node["anyOf"] if b.get("type") != "null"] or node["anyOf"]
        merged = dict(branches[0])
        merged.setdefault("description", node.get("description", ""))
        node = merged
    return {k: clean_schema(v) for k, v in node.items()}


class LLMProvider(ABC):
    name: str = "?"
    model: str = "?"

    @abstractmethod
    def send_user(self, text: str) -> LLMResponse: ...

    @abstractmethod
    def send_tool_results(self, results: list[ToolResult]) -> LLMResponse: ...

    @abstractmethod
    def reset(self) -> None:
        """Clear conversation history (keep system prompt and tools)."""
