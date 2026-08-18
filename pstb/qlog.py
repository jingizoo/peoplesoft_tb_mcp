"""Question log: every chat turn appended to logs/questions.jsonl with
auto-detected failure flags, so unanswered/misrouted prompts become a
reviewable backlog instead of vanishing.

Auto flags per turn:
  tool_error       — at least one tool call returned an error
  no_tool_calls    — a data-sounding question answered with no tool call
  max_rounds       — the agent loop hit its round limit
  gave_up          — the answer says it can't / data not available

The user can also mark a turn bad from the web UI (thumbs-down), which appends
a feedback record referencing the turn id.

Review the backlog:  python -m pstb.qlog [logs/questions.jsonl]
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import threading
import uuid
from pathlib import Path
from typing import Optional

_DATAISH = re.compile(
    r"(?i)\b(balance|aging|invoice|customer|journal|ledger|budget|revenue|"
    r"expense|account|period|fiscal|report|billing|suspense|variance|rate|"
    r"total|how (much|many)|top \d+)\b"
)
_GAVE_UP = re.compile(
    r"(?i)(not available|cannot (find|answer|determine)|no data|unable to|"
    r"could not find|doesn'?t exist)"
)


class QuestionLog:
    def __init__(self, path: Optional[str], root: Path):
        self.path: Optional[Path] = None
        self._lock = threading.Lock()
        if path:
            p = Path(path)
            self.path = p if p.is_absolute() else root / p

    def _append(self, rec: dict) -> None:
        if not self.path:
            return
        try:
            # One QuestionLog instance serves concurrent GUI turns. Keep each
            # JSON record as one serialized append so two silo completions
            # cannot interleave into a torn line.
            line = json.dumps(rec, default=str) + "\n"
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            pass  # logging must never break the answer

    def log_turn(self, *, surface: str, provider: str, question: str,
                 calls: list[dict], rounds: int, answer: str,
                 hit_round_limit: bool = False,
                 scope: Optional[dict] = None) -> str:
        ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        # Timestamp+question collided whenever two silos asked the same thing
        # in one second, making feedback attach to both/the wrong answer.
        # A random 128-bit ID is independent of wording, clock resolution and
        # process concurrency.
        turn_id = uuid.uuid4().hex
        active_scope = dict(scope or {})
        source_database = str(
            active_scope.get("source") or active_scope.get("db") or "default"
        ).strip() or "default"
        logged_scope = {"source": source_database}
        for key in ("business_unit", "ledger", "fiscal_year", "period"):
            value = active_scope.get(key)
            if value not in (None, ""):
                logged_scope[key] = value
        flags = []
        if any(not c.get("ok", True) for c in calls):
            flags.append("tool_error")
        if not calls and _DATAISH.search(question or ""):
            flags.append("no_tool_calls")
        if hit_round_limit:
            flags.append("max_rounds")
        if _GAVE_UP.search(answer or ""):
            flags.append("gave_up")
        self._append({
            "type": "turn", "turn_id": turn_id, "ts": ts,
            "surface": surface, "provider": provider,
            "source_database": source_database,
            "scope": logged_scope,
            "question": question,
            "tools": [{"tool": c.get("tool"), "ok": c.get("ok", True),
                       "ms": c.get("ms"),
                       **({"error": c["error"]} if c.get("error") else {})}
                      for c in calls],
            "rounds": rounds,
            "answer_chars": len(answer or ""),
            "failed": bool(flags), "flags": flags,
        })
        return turn_id

    def log_feedback(self, turn_id: str, verdict: str, note: str = "") -> None:
        self._append({
            "type": "feedback", "turn_id": turn_id,
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "verdict": verdict, "note": note,
        })


def review(path: str) -> int:
    p = Path(path)
    if not p.exists():
        print(f"No log at {p}")
        return 1
    turns, feedback = [], {}
    for line in p.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") == "turn":
            turns.append(rec)
        elif rec.get("type") == "feedback":
            feedback[rec.get("turn_id")] = rec.get("verdict")
    bad = [t for t in turns
           if t.get("failed") or feedback.get(t.get("turn_id")) == "bad"]
    print(f"{len(turns)} turns logged, {len(bad)} flagged as failed/bad:\n")
    for t in bad:
        fb = feedback.get(t["turn_id"])
        tags = ",".join(t.get("flags", [])) + (",user_bad" if fb == "bad" else "")
        tools = ",".join(x["tool"] for x in t.get("tools", [])) or "-"
        print(f"  [{t['ts'][:16]}] ({tags}) tools={tools}")
        print(f"      Q: {t['question'][:120]}")
    if not bad:
        print("  none — nothing flagged")
    return 0


if __name__ == "__main__":
    sys.exit(review(sys.argv[1] if len(sys.argv) > 1 else "logs/questions.jsonl"))
