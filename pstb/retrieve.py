"""Passage splitting and lexical ranking for wiki content. Stdlib only.

Why lexical (BM25) rather than embeddings: the corpus is a few hundred finance
policy pages, and the failure mode we are fixing is "the model never saw the
text", not "the right page was not found". Confluence already maintains a
full-text index; this adds passage-level ranking on top so the agent receives
the two or three paragraphs that answer the question instead of whole pages.
See docs/RAG.md for when a vector store does earn its place.
"""
from __future__ import annotations

import math
import re
from typing import Iterable, Optional

_WORD = re.compile(r"[A-Za-z0-9$%.]+")
# Terms that carry no retrieval signal in a finance wiki.
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "for", "on",
    "at", "by", "with", "as", "it", "this", "that", "be", "we", "our", "from",
    "what", "which", "how", "do", "does", "any", "all", "can", "should", "must",
    "if", "when", "there", "их", "us", "you", "your",
}


def tokens(text: str) -> list[str]:
    return [w for w in (t.lower().strip(".") for t in _WORD.findall(text or ""))
            if w and w not in _STOP and len(w) > 1]


def split_passages(text: str, title: str = "",
                   target_chars: int = 900) -> list[dict]:
    """Split page text into heading-aware passages.

    Markdown/Confluence-extracted text carries headings on their own lines;
    keeping the nearest heading with each passage is what lets an answer cite
    "Suspense account policy" rather than just the page name.
    """
    text = (text or "").replace("\r\n", "\n")
    if not text.strip():
        return []
    lines = text.split("\n")
    section = title or ""
    buf: list[str] = []
    out: list[dict] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if len(body) >= 40:  # ignore stubs like a lone heading
            out.append({"section": section, "text": body})
        buf.clear()

    for line in lines:
        s = line.strip()
        is_heading = (
            (s.startswith("#") and len(s) < 120)
            or (0 < len(s) < 80 and s.endswith(":") and not s.endswith("::"))
        )
        if is_heading:
            flush()
            section = s.lstrip("#").strip().rstrip(":") or section
            continue
        buf.append(line)
        if sum(len(x) for x in buf) >= target_chars and not s:
            flush()
    flush()

    # Very long sections still get chunked so one passage cannot dominate.
    chunked: list[dict] = []
    for p in out:
        t = p["text"]
        if len(t) <= target_chars * 2:
            chunked.append(p)
            continue
        paras = [x for x in t.split("\n\n") if x.strip()]
        cur: list[str] = []
        for para in paras:
            cur.append(para)
            if sum(len(x) for x in cur) >= target_chars:
                chunked.append({"section": p["section"], "text": "\n\n".join(cur)})
                cur = []
        if cur:
            chunked.append({"section": p["section"], "text": "\n\n".join(cur)})
    return chunked


def rank_passages(question: str, passages: list[dict], top: int = 6,
                  k1: float = 1.5, b: float = 0.75) -> list[dict]:
    """BM25 over passages. Returns the top matches with a score and the terms
    that matched, so an answer can be audited for why a passage was used."""
    q = tokens(question)
    if not q or not passages:
        return []
    docs = [tokens(p["text"] + " " + p.get("section", "")) for p in passages]
    n = len(docs)
    avgdl = sum(len(d) for d in docs) / n if n else 0.0
    df: dict[str, int] = {}
    for d in docs:
        for w in set(d):
            df[w] = df.get(w, 0) + 1

    scored = []
    for i, d in enumerate(docs):
        if not d:
            continue
        tf: dict[str, int] = {}
        for w in d:
            tf[w] = tf.get(w, 0) + 1
        score = 0.0
        matched = []
        for w in set(q):
            if w not in tf:
                continue
            idf = math.log(1 + (n - df.get(w, 0) + 0.5) / (df.get(w, 0) + 0.5))
            denom = tf[w] + k1 * (1 - b + b * (len(d) / (avgdl or 1)))
            score += idf * (tf[w] * (k1 + 1)) / (denom or 1)
            matched.append(w)
        if score > 0:
            # Coverage, not the raw BM25 score, is what a reader can act on.
            # A score of 4.1 means nothing on its own — it is not comparable
            # between questions — whereas "matched 2 of the 5 words you asked
            # about" says plainly how much of the question this passage speaks
            # to, and lets a weak match be recognised as weak.
            scored.append({**passages[i], "score": round(score, 3),
                           "matched_terms": sorted(matched)[:8],
                           "term_coverage": round(len(set(matched)) / len(set(q)), 2)})
    scored.sort(key=lambda x: -x["score"])
    return scored[: max(int(top or 6), 1)]
