"""Policy figures from the wiki, resolved into query parameters.

"Show me every asset above the capitalization threshold" is two questions
wearing one coat. The threshold lives in a policy document; the assets live in
the ledger. The agent can already read both — but the join between them was
being made in the model's head, which is the one place it must not happen: a
paraphrased $10,000 where the policy says $5,000 silently changes which rows
the user sees, and the answer looks equally confident either way.

So the number is extracted MECHANICALLY here, the same principle the number
guard in guards.py applies to output. A regex reads the retrieved passage, the
sentence it came from is quoted verbatim, and the value travels as a bind
parameter the model never types. What the model contributes is the SQL shape
(`WHERE COST >= :threshold`); what this module contributes is the value and
the receipt for it.

Four refusals, all deliberate:

  ambiguous     two different figures in the corpus -> return both, pick
                neither. A capitalization threshold that changed in 2024 and
                a page that was never updated look identical to a regex, and
                guessing between them is how you file the wrong number.
  not_found     no passage carries a figure -> say so. The alternative is the
                model supplying one from its training data, which is exactly
                the failure this module exists to remove.
  demo_content  the bundled sample policies are fictional. Letting them filter
                a real ledger pairs a live balance with an invented rule, so
                the value is returned labelled and refused as a filter.
  no_wiki       nothing configured to read.

Precedence is deliberate too: an APPROVED site-memory fact outranks a wiki
scrape, because a human confirmed it for this deployment. That is disclosed in
the result rather than silently applied.
"""
from __future__ import annotations

import copy as _copy
import re
import time as _time
from typing import Optional


class PolicyError(RuntimeError):
    pass


class PolicyTerm:
    """A policy figure worth resolving, and how to find it."""

    def __init__(self, key: str, label: str, unit: str, queries: list,
                 match_words: list, meaning: str):
        self.key = key
        self.label = label
        self.unit = unit                  # currency | percent | days | count
        self.queries = queries            # what to ask the wiki
        self.match_words = match_words    # a sentence must contain one of these
        self.meaning = meaning            # how the value is meant to be applied


POLICY_TERMS: dict = {
    t.key: t for t in [
        PolicyTerm(
            "capitalization_threshold", "capitalization threshold", "currency",
            ["capitalization threshold fixed asset",
             "minimum cost to capitalize an asset",
             "expense versus capitalize policy"],
            ["capitaliz", "capital threshold", "fixed asset"],
            "Asset cost at or above this amount is capitalized; below it the "
            "cost is expensed."),
        PolicyTerm(
            "purchase_approval_limit", "purchase approval limit", "currency",
            ["purchase order approval limit authorization",
             "requisition approval threshold"],
            ["approval", "authoriz", "purchase order", "requisition"],
            "Purchases at or above this amount need the next approval level."),
        PolicyTerm(
            "write_off_limit", "write-off limit", "currency",
            ["receivable write-off limit small balance",
             "bad debt write off threshold"],
            ["write-off", "write off", "writeoff", "bad debt"],
            "Open receivables at or below this amount may be written off "
            "without escalation."),
        PolicyTerm(
            "materiality_threshold", "materiality threshold", "currency",
            ["materiality threshold for variance investigation",
             "material variance explanation required"],
            ["material", "variance"],
            "Variances at or above this amount require written explanation."),
        PolicyTerm(
            "suspense_clearing_days", "suspense clearing period", "days",
            ["suspense account clearing days policy",
             "how long may items remain in suspense"],
            ["suspense", "clear"],
            "Items may sit in suspense no longer than this many days."),
        PolicyTerm(
            "invoice_payment_terms_days", "standard payment terms", "days",
            ["standard customer payment terms net days",
             "invoice due days policy"],
            ["payment term", "net ", "due", "invoice"],
            "Days from invoice date until an invoice is due."),
    ]
}

# ---- mechanical extraction ------------------------------------------------

_MULT = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000,
         "mm": 1_000_000, "bn": 1_000_000_000, "billion": 1_000_000_000}

_CURRENCY = re.compile(
    r"(?:(?P<sym>[$€£])\s?(?P<a1>\d[\d,]*(?:\.\d+)?)\s*(?P<m1>k|m|mm|bn|thousand|million|billion)?"
    r"|(?P<cur>USD|EUR|GBP|CAD|AUD|INR)\s?(?P<a2>\d[\d,]*(?:\.\d+)?)\s*(?P<m2>k|m|mm|bn|thousand|million|billion)?"
    r"|(?P<a3>\d[\d,]*(?:\.\d+)?)\s*(?P<m3>k|m|mm|bn|thousand|million|billion)?\s*(?P<cur2>USD|EUR|GBP|CAD|AUD|INR))",
    re.IGNORECASE)
_PERCENT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:%|percent\b)", re.IGNORECASE)
_DAYS = re.compile(
    r"(\d[\d,]*)\s*(?:calendar|business|working)?\s*days?\b", re.IGNORECASE)
# "Net 30" is how payment terms are usually written.
_NET_TERMS = re.compile(r"\bnet\s+(\d{1,3})\b", re.IGNORECASE)

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+|(?:^|\s)[-*•]\s+")


def _num(raw: str, mult: Optional[str]) -> float:
    value = float(raw.replace(",", ""))
    if mult:
        value *= _MULT.get(mult.lower(), 1)
    return value


def sentences(text: str) -> list:
    return [s.strip() for s in _SENTENCE.split(text or "") if s and s.strip()]


def extract_candidates(text: str, term: PolicyTerm) -> list:
    """Figures of the right unit, from sentences that are about this term.

    Sentence scoping is what keeps this honest. A capitalization policy page
    also mentions depreciation lives, useful-life years and approval chains;
    grabbing the first number on the page would return any of them. A figure
    counts only when it shares a sentence with the term's own vocabulary.
    """
    out: list = []
    for sent in sentences(text):
        low = sent.lower()
        if not any(w in low for w in term.match_words):
            continue
        if term.unit == "currency":
            for m in _CURRENCY.finditer(sent):
                raw = m.group("a1") or m.group("a2") or m.group("a3")
                mult = m.group("m1") or m.group("m2") or m.group("m3")
                cur = (m.group("cur") or m.group("cur2") or "")
                if m.group("sym"):
                    cur = {"$": "USD", "€": "EUR", "£": "GBP"}[m.group("sym")]
                out.append({"value": _num(raw, mult),
                            "unit": (cur or "").upper() or "currency",
                            "quote": sent})
        elif term.unit == "percent":
            for m in _PERCENT.finditer(sent):
                out.append({"value": _num(m.group(1), None), "unit": "percent",
                            "quote": sent})
        elif term.unit == "days":
            found = [_num(m.group(1), None) for m in _NET_TERMS.finditer(sent)]
            found += [_num(m.group(1), None) for m in _DAYS.finditer(sent)]
            for v in found:
                out.append({"value": v, "unit": "days", "quote": sent})
    return out


# ---- resolution -----------------------------------------------------------

class PolicyResolver:
    """Resolve a named policy figure to a value plus its receipt."""

    TTL = 300.0        # seconds

    def __init__(self, wiki=None, memory=None):
        self.wiki = wiki
        self.memory = memory
        self._cache: dict = {}

    def _cached(self, key: str):
        hit = self._cache.get(key)
        if hit and hit[0] > _time.monotonic():
            return _copy.deepcopy(hit[1])
        return None

    def _store(self, key: str, value: dict) -> dict:
        self._cache[key] = (_time.monotonic() + self.TTL, _copy.deepcopy(value))
        return value

    def invalidate(self) -> None:
        self._cache.clear()

    # -- site memory takes precedence over anything scraped -----------------
    def _from_memory(self, term: PolicyTerm) -> Optional[dict]:
        if self.memory is None:
            return None
        try:
            facts = self.memory.approved()
        except Exception:
            return None
        for fact in facts:
            text = str(fact.get("fact") or fact.get("text") or "")
            low = text.lower()
            if term.key in low or term.label.lower() in low:
                got = extract_candidates(text, term)
                if len(got) == 1:
                    return {
                        "status": "resolved",
                        "policy": term.key,
                        "label": term.label,
                        "value": got[0]["value"],
                        "unit": got[0]["unit"],
                        "quote": got[0]["quote"],
                        "source": {"kind": "site_memory",
                                   "title": "approved site memory",
                                   "approved_by": fact.get("approved_by"),
                                   "decided_at": fact.get("decided_at")},
                        "usable_as_filter": True,
                        "note": ("This deployment has a human-approved value "
                                 "for this policy; it takes precedence over "
                                 "anything found in the wiki."),
                    }
        return None

    def resolve(self, policy: str, max_pages: int = 4) -> dict:
        key = (policy or "").strip().lower().replace(" ", "_").replace("-", "_")
        term = POLICY_TERMS.get(key)
        if term is None:
            raise PolicyError(
                f"Unknown policy {policy!r}. Known: "
                f"{', '.join(sorted(POLICY_TERMS))}. For a figure outside this "
                "list, read the page with wiki_lookup and apply it yourself — "
                "but state the source and do not use it as a silent filter.")

        cached = self._cached(key)
        if cached is not None:
            return cached

        from_mem = self._from_memory(term)
        if from_mem:
            return self._store(key, from_mem)

        base = {"status": "not_found", "policy": term.key, "label": term.label,
                "unit": term.unit, "meaning": term.meaning,
                "usable_as_filter": False, "candidates": [], "sources": []}

        if self.wiki is None:
            base["status"] = "no_wiki"
            base["note"] = ("No wiki is configured, so policy figures cannot "
                            "be looked up. Ask the user for the value and say "
                            "that it was supplied by hand.")
            return self._store(key, base)

        # Is this the bundled fiction? Decide BEFORE reading, so a demo page
        # can never reach a filter on a real ledger.
        demo = False
        try:
            demo = bool((self.wiki.health() or {}).get("is_bundled_demo_content"))
        except Exception:
            demo = False

        from . import wiki as wiki_mod

        candidates: list = []
        sources: list = []
        for query in term.queries:
            try:
                hit = wiki_mod.lookup(self.wiki, query, max_pages=max_pages)
            except Exception as e:
                base["note"] = f"wiki lookup failed: {type(e).__name__}: {e}"
                continue
            for src in hit.get("sources") or []:
                if src not in sources:
                    sources.append(src)
            for passage in hit.get("passages") or []:
                text = passage.get("text") or passage.get("passage") or ""
                for cand in extract_candidates(text, term):
                    cand["source"] = {
                        "title": passage.get("title") or passage.get("page"),
                        "url": passage.get("url"),
                        "kind": "wiki",
                    }
                    cand["term_coverage"] = passage.get("term_coverage")
                    cand["passage"] = text[:600]
                    if not any(c["value"] == cand["value"]
                               and c["quote"] == cand["quote"]
                               for c in candidates):
                        candidates.append(cand)
            if candidates:
                break        # first phrasing that finds figures is enough

        base["candidates"] = candidates
        base["sources"] = sources
        if not candidates:
            base["note"] = (
                f"No figure for the {term.label} was found in the wiki. Do NOT "
                "supply one from memory — ask the user for it, or run the "
                "query without that filter and say it is unfiltered.")
            return self._store(key, base)

        distinct = sorted({c["value"] for c in candidates})
        if len(distinct) > 1:
            base["status"] = "ambiguous"
            base["values_found"] = distinct
            base["note"] = (
                f"The wiki carries {len(distinct)} different figures for the "
                f"{term.label} ({', '.join(f'{v:,.2f}' for v in distinct)}). "
                "Quote all of them with their sources and ask the user which "
                "applies — one of these pages is likely out of date.")
            return self._store(key, base)

        winner = candidates[0]
        # A figure of the right SHAPE is not a figure on the right SUBJECT.
        # The sentence scoping in extract_candidates keeps out most of it, but
        # a page about a neighbouring topic can still carry a sentence with
        # both the vocabulary and a number. When the retrieved passage barely
        # matched the question, hand the model the evidence and let it judge
        # instead of quietly binding the number into a WHERE clause.
        coverage = winner.get("term_coverage")
        weak = coverage is not None and coverage < 0.5
        base.update({
            "status": ("demo_content" if demo
                       else "needs_review" if weak else "resolved"),
            "value": winner["value"],
            "unit": winner["unit"],
            "quote": winner["quote"],
            "source": winner["source"],
            "passage": winner.get("passage"),
            "term_coverage": coverage,
            "corroborating_passages": len(candidates),
            "usable_as_filter": not demo and not weak,
        })
        if weak and not demo:
            base["note"] = (
                f"A figure was found, but the passage matched only "
                f"{coverage:.0%} of the search terms — it may be about a "
                f"different rule. READ the passage below and decide whether it "
                f"really states the {term.label}. If it does, apply the value "
                "and cite the page; if it does not, say the wiki does not "
                "record this figure. It is deliberately not usable as a "
                "silent filter.")
            return self._store(key, base)
        if demo:
            base["note"] = (
                "This value came from the BUNDLED DEMO policy pages shipped "
                "with the repo, which are fictional. It must not be used to "
                "filter real ledger data. Point wiki.provider at the company "
                "Confluence space to get real policy figures.")
        else:
            base["note"] = (
                f"{term.meaning} Value taken verbatim from the wiki, not from "
                "the model. Cite the source page when you use it.")
        return self._store(key, base)

    def resolve_binds(self, policy_binds: dict) -> tuple:
        """Resolve {bind_name: policy_key} into (binds, provenance, refusals).

        Used by run_sql so the model writes `>= :threshold` and never types
        the figure. Anything unresolved is a refusal, not a default: running
        the query with a guessed threshold returns a plausible, wrong row set.
        """
        binds: dict = {}
        provenance: list = []
        refusals: list = []
        for bind, policy in (policy_binds or {}).items():
            if not re.match(r"^[A-Za-z_]\w*$", str(bind or "")):
                refusals.append(f"{bind!r} is not a usable bind name")
                continue
            try:
                got = self.resolve(str(policy))
            except PolicyError as e:
                refusals.append(str(e))
                continue
            if got.get("usable_as_filter") and got.get("value") is not None:
                binds[bind] = got["value"]
                provenance.append({
                    "bind": bind, "policy": got["policy"],
                    "label": got["label"], "value": got["value"],
                    "unit": got.get("unit"), "quote": got.get("quote"),
                    "source": got.get("source"),
                })
            else:
                refusals.append(
                    f":{bind} — {got.get('label')} is {got.get('status')}. "
                    f"{got.get('note') or ''}".strip())
        return binds, provenance, refusals


def list_terms() -> dict:
    return {
        "policies": [
            {"policy": t.key, "label": t.label, "unit": t.unit,
             "meaning": t.meaning}
            for t in POLICY_TERMS.values()
        ],
        "note": ("Resolve one with resolve_policy_value, or pass "
                 "policy_binds={'threshold': 'capitalization_threshold'} to "
                 "run_sql and write `>= :threshold` in the SQL — the value is "
                 "bound server-side from the wiki, so it is never retyped."),
    }
