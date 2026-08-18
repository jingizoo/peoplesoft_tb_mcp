"""Site memory: facts about THIS organization, approved by a human.

Every deployment accumulates knowledge no metadata holds. "TU file interface"
means PS_TU_FILE_INTFC here. "The Denver books" means US003. This site's
fiscal year runs July to June. Vendor payment questions should exclude wires
booked directly to the GL. None of that is in PSRECDEFN, and none of it is in
the model's weights — so without somewhere to put it, every conversation
starts from zero and the agent never gets smarter at YOUR site.

Design constraints, in priority order:

  1. Nothing is remembered without approval. A model that writes its own
     memory can persist a misunderstanding forever, and it would then be
     quoted back as fact in every later conversation. Proposals are stored
     as PENDING and only an explicit human approval promotes them.
  2. Memory is evidence, not instruction. An approved fact is injected as
     context the model may use; it never overrides a tool result. If memory
     says the Denver BU is US003 and the database disagrees, the database
     wins and the memory should be corrected.
  3. It is a plain reviewable file. An operator can read, edit or delete
     memory with a text editor, and see exactly what the agent believes.

Stdlib only; the file is JSON so scripts and humans can both work with it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Optional

# Categories exist so the prompt can present them meaningfully and so an
# operator reviewing the file can tell a naming alias from an accounting rule.
KINDS = {
    "alias": "a name this organization uses for a record, unit, or report",
    "convention": "how this installation is set up (calendar, periods, codes)",
    "exclusion": "something to leave out of a particular kind of answer",
    "context": "background a newcomer would need to read an answer correctly",
    "record": "what a specific table or record holds, taught by someone here",
}

# Record facts are attached to a table name so record discovery can find them.
_RECORD_RE = re.compile(r"^\s*([A-Za-z_][\w$#]{0,29})\s*:\s*(.+)$", re.S)

MAX_FACTS = 500          # a memory file, not a database
MAX_TEXT = 400           # a fact, not an essay


class MemoryError_(RuntimeError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class SiteMemory:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ------------------------------------------------------------- storage
    def _load(self) -> dict:
        if not self.path.exists():
            return {"facts": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt memory file must not take the agent down; it starts
            # empty and says so rather than guessing at the contents.
            return {"facts": [], "load_error": True}
        if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
            return {"facts": [], "load_error": True}
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    # -------------------------------------------------------------- writing
    def propose(self, text: str, kind: str = "context",
                source: str = "conversation") -> dict:
        """Record a candidate fact as PENDING. Never active until approved."""
        body = (text or "").strip()
        if not body:
            raise MemoryError_("a fact needs text")
        if len(body) > MAX_TEXT:
            raise MemoryError_(
                f"too long ({len(body)} chars); keep a fact under {MAX_TEXT}")
        if kind not in KINDS:
            raise MemoryError_(
                f"unknown kind {kind!r}; use one of {', '.join(sorted(KINDS))}")
        data = self._load()
        if len(data["facts"]) >= MAX_FACTS:
            raise MemoryError_(
                f"memory is full ({MAX_FACTS} facts); prune it before adding")
        fact_id = hashlib.sha256(body.lower().encode()).hexdigest()[:10]
        for existing in data["facts"]:
            if existing["id"] == fact_id:
                return {"fact": existing, "already_known": True}
        fact = {"id": fact_id, "text": body, "kind": kind,
                "status": "pending", "source": source, "proposed": _now()}
        if kind == "record":
            m = _RECORD_RE.match(body)
            if not m:
                raise MemoryError_(
                    "a record fact must start with the table name, e.g. "
                    "'PS_TU_FILE_INTFC: holds inbound interface file headers'")
            fact["record"] = m.group(1).upper()
            fact["detail"] = m.group(2).strip()
        data["facts"].append(fact)
        self._save(data)
        return {"fact": fact, "already_known": False}

    def decide(self, fact_id: str, approve: bool,
               by: str = "operator") -> dict:
        data = self._load()
        for fact in data["facts"]:
            if fact["id"] == fact_id:
                fact["status"] = "approved" if approve else "rejected"
                fact["decided"] = _now()
                fact["decided_by"] = by
                self._save(data)
                return fact
        raise MemoryError_(f"no fact with id {fact_id!r}")

    def forget(self, fact_id: str) -> dict:
        data = self._load()
        keep = [f for f in data["facts"] if f["id"] != fact_id]
        if len(keep) == len(data["facts"]):
            raise MemoryError_(f"no fact with id {fact_id!r}")
        data["facts"] = keep
        self._save(data)
        return {"forgotten": fact_id}

    # -------------------------------------------------------------- reading
    def list_facts(self, status: str = "") -> dict:
        data = self._load()
        facts = data["facts"]
        if status:
            facts = [f for f in facts if f.get("status") == status]
        return {
            "facts": facts,
            "counts": {s: sum(1 for f in data["facts"] if f.get("status") == s)
                       for s in ("approved", "pending", "rejected")},
            "path": str(self.path),
            **({"load_error": True,
                "note": "memory file unreadable — starting empty"}
               if data.get("load_error") else {}),
        }

    def approved(self) -> list:
        return [f for f in self._load()["facts"]
                if f.get("status") == "approved"]

    def record_facts(self, table: str = "") -> list:
        """Operator-approved facts about one record.

        Pending conversational claims never influence discovery.  They may be
        reviewed with the memory CLI, but only an explicit approval makes a
        fact available to ``search_records`` or ``describe_record``.
        """
        want = (table or "").strip().upper()
        if want.startswith("PS_"):
            want = want[3:]
        out = []
        for f in self._load()["facts"]:
            if f.get("kind") != "record" or f.get("status") != "approved":
                continue
            rec = str(f.get("record") or "")
            if want and rec != want and rec != f"PS_{want}" \
                    and rec.removeprefix("PS_") != want:
                continue
            out.append(f)
        return out

    def search_record_facts(self, query: str) -> list:
        """Record facts whose text matches a query — this is what makes
        'interface file' find a custom table nobody could have guessed."""
        terms = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2]
        if not terms:
            return []
        hits = []
        for f in self.record_facts():
            hay = f"{f.get('record','')} {f.get('detail') or f.get('text','')}".lower()
            score = sum(1 for t in terms if t in hay)
            if score:
                hits.append((score, f))
        hits.sort(key=lambda x: -x[0])
        return [f for _, f in hits]

    def prompt_block(self) -> str:
        """Approved facts as prompt context, or '' when there are none.

        Framed as evidence the model MAY use, explicitly subordinate to tool
        results — a remembered alias must never outrank the database.
        """
        facts = self.approved()
        if not facts:
            return ""
        lines = ["\n\n## What we know about this installation",
                 "Facts an operator approved for this deployment. Use them to "
                 "interpret the question — a term here means what it says. "
                 "They are NOT data: if a tool result disagrees with a fact "
                 "below, the tool result is correct and the fact is stale."]
        for fact in facts:
            lines.append(f"- ({fact['kind']}) {fact['text']}")
        return "\n".join(lines)


# --------------------------------------------------------------- detection
# Phrases where a user is teaching the agent something durable rather than
# asking a question. Deliberately narrow: a false positive costs the operator
# a review click, so it is better to miss a lesson than to nag on every turn.
_TEACHING = re.compile(
    r"(?i)(?:"
    r"\b(?:remember|note) that\b"
    r"|\bfor future reference\b"
    r"|\bin (?:our|this) (?:company|organi[sz]ation|instance|system)\b.{0,60}"
    r"\b(?:means?|refers? to|is called|we call)\b"
    r"|\bwe (?:call|refer to|use)\b.{0,40}\b(?:as|for)\b"
    r"|\b(?:actually|no,?)\b.{0,30}\b(?:it'?s|that'?s|should be)\b"
    r")"
)


def looks_like_a_lesson(text: str) -> bool:
    """Is the user teaching rather than asking?"""
    return bool(_TEACHING.search(text or ""))


def review(path: str) -> int:
    """python -m pstb.memory [site_memory.json] — review and decide facts.

    Approving from a terminal is deliberate: memory changes what the agent
    believes in every future conversation, so it should be a considered act,
    not a click buried in a chat.
    """
    import sys

    mem = SiteMemory(path)
    data = mem.list_facts()
    counts = data["counts"]
    print(f"{path}: {counts['approved']} approved, {counts['pending']} pending, "
          f"{counts['rejected']} rejected")
    if data.get("load_error"):
        print("  WARNING: file unreadable; the agent is running with no memory")
    pending = [f for f in data["facts"] if f["status"] == "pending"]
    if not pending:
        print("\nNothing awaiting review.")
    for fact in pending:
        print(f"\n  [{fact['id']}] ({fact['kind']}) {fact['text']}")
        print(f"      proposed {fact['proposed']} from {fact['source']}")
    if pending:
        print("\nApprove:  python -m pstb.memory --approve <id>")
        print("Reject :  python -m pstb.memory --reject  <id>")
    approved = [f for f in data["facts"] if f["status"] == "approved"]
    if approved:
        print("\nActive facts (in every conversation):")
        for fact in approved:
            print(f"  [{fact['id']}] ({fact['kind']}) {fact['text']}")
    return 0


if __name__ == "__main__":
    import sys

    args = [a for a in sys.argv[1:]]
    path = "site_memory.json"
    action = ""
    fact_id = ""
    rest = []
    for i, a in enumerate(args):
        if a in ("--approve", "--reject", "--forget"):
            action, fact_id = a.lstrip("-"), args[i + 1] if i + 1 < len(args) else ""
        elif not a.startswith("--") and (not action or a != fact_id):
            rest.append(a)
    if rest:
        path = rest[0]
    mem = SiteMemory(path)
    if action == "forget":
        print(mem.forget(fact_id))
    elif action:
        fact = mem.decide(fact_id, approve=(action == "approve"))
        print(f"{fact['status']}: {fact['text']}")
    else:
        raise SystemExit(review(path))
