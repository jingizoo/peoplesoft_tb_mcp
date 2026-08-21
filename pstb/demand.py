"""What the agent could not answer, matched to what could have answered it.

On the deployment this targets, 257 of 285 logged Finance turns failed.
Each failure is recorded — the redacted question, the flags, the source —
and each one names a demand: a business phrase this deployment uses that
retrieval could not ground. The catalog knows every table, the profiler
knows which of them are alive and worth reading, and the approval queue
governs how a meaning becomes durable. This module is the bridge between
them: it mines the failed turns for the phrases that keep missing, asks
the same search the model uses what those phrases WOULD match today, and
hands the operator a ranked worklist whose every row can become a pending
metadata proposal with one click.

Deliberately deterministic. No model drafts anything here: machinery
computes the evidence — which phrase failed, how often, which live tables
plausibly answer it — and a person authors the meaning. The proposal then
takes the exact same path a hand-typed one takes: catalog-validated,
PENDING, inert until approved. The flywheel is failures -> worklist ->
proposal -> human approval -> retrieval improves, and every arrow in it
already existed; this module only closes the circle.

Privacy note: questions arrive here ALREADY redacted (qlog stores them
through redact_private_text) and leave truncated. Nothing in a worklist
row may carry more of a question than the log itself retains.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Callable, Iterable, Mapping

# Words that appear in nearly every financial question and identify
# nothing: asking about "the total" is not demand for a TOTAL table.
_STOPWORDS = frozenset("""
a an and are as at balance be by can compare could did do does for
from get give had has have how i in is it its last list me month my not of
on or our per period please que quarter report rows should show shows some
tell than that the their them then there these this those to top us vs was
we were what when where which who why will with would year years you your
all any amount amounts number numbers value values data database table
tables record records column columns field fields file files status
open closed current new old total totals count sum detail details between
during each every much many
""".split())

# A token that looks like a physical object the user typed verbatim:
# PS_TU_FILE_INTFC, JOB_HDR, XX_TB_ACCT_VW. These are the strongest
# demand signal of all — the user knows the table and retrieval still
# failed — and they are matched as-is, never split into words.
_RECORDISH = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
# The ASCII apostrophe is NOT a quote delimiter: "what's ... the
# vendor's queue" would otherwise bracket a bogus "quoted phrase"
# nobody typed. Double quotes and directional quotes still capture.
_QUOTED = re.compile(r"[\"“‘]([^\"”’]{3,60})[\"”’]")
# Any-script words, three letters or longer: an ASCII-only class
# minced non-English questions into fragments that became aliases.
_WORD = re.compile(r"[^\W\d_][^\W\d_0-9]{2,}", re.UNICODE)

_SAMPLE_CHARS = 140
_TERM_CHARS = 60


def _norm_shape(question: str) -> str:
    """One key per question SHAPE, so a user retrying the same question
    five times counts as one cluster (retries are still counted as
    occurrences — persistence is demand too)."""
    text = str(question or "").casefold()
    text = re.sub(r"\d+", "#", text)
    return " ".join(re.findall(r"[^\W\d_]+|#", text))


def _terms_of(question: str) -> set:
    """The candidate business phrases one failed question contributes."""
    text = str(question or "")
    out: set[str] = set()
    # Overlong tokens are SKIPPED, not truncated: a truncated record name
    # stops matching _RECORDISH, gets treated as a phrase, and its stub
    # could end up submitted as an alias for a name nobody typed.
    for match in _RECORDISH.finditer(text):
        if len(match.group(0)) <= _TERM_CHARS:
            out.add(match.group(0))
    for match in _QUOTED.finditer(text):
        phrase = " ".join(match.group(1).split()).casefold()
        if (phrase and len(phrase) <= _TERM_CHARS
                and not all(w in _STOPWORDS for w in phrase.split())):
            out.add(phrase)
    # Words are extracted from the text WITHOUT its record-ish tokens:
    # PS_TU_FILE_INTFC already contributed itself whole, and letting its
    # fragments ("intfc") into bigrams manufactures phrases nobody typed.
    prose = _RECORDISH.sub(" ", text)
    words = [w for w in _WORD.findall(prose.casefold())
             if w not in _STOPWORDS]
    out.update(words)
    for first, second in zip(words, words[1:]):
        bigram = f"{first} {second}"
        if len(bigram) <= _TERM_CHARS:
            out.add(bigram)
    return {term for term in out if len(term) <= _TERM_CHARS}


def failed_question_terms(turns: Iterable[Mapping], *,
                          source: str = "default",
                          max_terms: int = 12) -> list:
    """The phrases failed questions keep using, most demanded first.

    Ranking is (distinct question shapes, then total occurrences): a
    phrase that fails across five DIFFERENT questions outranks one the
    same question retried five times, but retries still break ties —
    somebody wanted that answer badly.
    """
    wanted = str(source or "default").strip() or "default"
    occurrences: dict[str, int] = defaultdict(int)
    shapes: dict[str, set] = defaultdict(set)
    samples: dict[str, str] = {}
    for turn in turns or ():
        if not isinstance(turn, Mapping) or not turn.get("failed"):
            continue
        turn_source = str(turn.get("source_database") or "default")
        if turn_source != wanted:
            continue
        question = str(turn.get("question") or "")
        if not question.strip():
            continue
        shape = _norm_shape(question)
        for term in sorted(_terms_of(question)):
            occurrences[term] += 1
            shapes[term].add(shape)
            # keep the SHORTEST sample: it is the least likely to carry
            # incidental context, and the log's redaction already ran.
            prior = samples.get(term)
            if prior is None or len(question) < len(prior):
                samples[term] = question
    # The term itself is the final key: without it, exact ties kept the
    # hash-dependent set-iteration order and the worklist changed between
    # server restarts on an identical log — while claiming to be
    # deterministic. reverse=True makes the tiebreak reverse-lexicographic;
    # stable either way, which is all that matters.
    ranked = sorted(
        occurrences,
        key=lambda t: (len(shapes[t]), occurrences[t], len(t), t),
        reverse=True)
    out = []
    for term in ranked:
        # Single-word unigrams that only ever appear inside a ranked
        # bigram add noise, not signal: "interface files" subsumes both
        # halves when their counts are identical.
        if " " not in term and not _RECORDISH.fullmatch(term):
            subsumed = any(
                " " in other and term in other.split()
                and shapes[other] == shapes[term]
                and occurrences[other] == occurrences[term]
                for other in ranked)
            if subsumed:
                continue
        out.append({
            "term": term,
            "occurrences": occurrences[term],
            "distinct_questions": len(shapes[term]),
            "sample_question": samples[term][:_SAMPLE_CHARS],
        })
        if len(out) >= max_terms:
            break
    return out


def _candidate(match: Mapping, useful: Mapping) -> dict:
    prefer = (useful or {}).get("prefer_instead") or {}
    return {
        "identifier": ".".join(
            part for part in (match.get("schema"),
                              match.get("physical_object")
                              or match.get("name")) if part),
        "name": match.get("name"),
        "kind": match.get("kind"),
        "label": match.get("label") or "",
        "matched_on": match.get("match_reasons") or [],
        "liveness": (useful or {}).get("liveness") or "unknown",
        "row_estimate": (useful or {}).get("row_estimate"),
        "value_score": (useful or {}).get("value_score"),
        "caveat": (useful or {}).get("caveat") or "",
        "prefer_instead": prefer.get("object") or "",
    }


def coverage_gaps(turns: Iterable[Mapping],
                  search: Callable[[str], Mapping],
                  usefulness: Callable[[str], Mapping], *,
                  source: str = "default",
                  max_terms: int = 12,
                  max_candidates: int = 4) -> dict:
    """The worklist: each demanded phrase with the live tables that
    plausibly answer it, ranked by the profiler's own value evidence.

    ``search`` is THE SAME retrieval the model uses (catalog.search), on
    purpose: the worklist must reflect what grounding can actually find
    today, not a friendlier private matcher — a phrase this search cannot
    hit is exactly a phrase the agent could not ground.

    ``usefulness`` maps a candidate identifier to the profiler's verdict
    (liveness, value_score, prefer_instead). Verified-empty candidates
    are dropped — proposing an alias onto a table with provably nothing
    in it manufactures a confident wrong answer — and a candidate the
    shadow detector redirects is replaced by a pointer to its canonical
    table.
    """
    gaps = []
    for entry in failed_question_terms(list(turns or ()), source=source,
                                       max_terms=max_terms):
        term = entry["term"]
        try:
            result = search(term) or {}
        except Exception as exc:                # noqa: BLE001
            result = {"error": str(exc)}
        if isinstance(result, Mapping) and result.get("available") is False:
            # The catalog itself is unreadable. Every term would come back
            # candidate-less, and the worklist would confidently claim "no
            # catalog match" for phrases the catalog was never asked about.
            detail = str(result.get("detail") or
                         "the metadata catalog is not readable")
            how = str(result.get("how_to_build") or "").strip()
            return {
                "source": str(source or "default").strip() or "default",
                "gaps": [],
                "note": detail + (f" Build it with: {how}" if how else ""),
            }
        matches = result.get("matches") or []
        candidates = []
        for match in matches:
            if not isinstance(match, Mapping):
                continue
            if str(match.get("kind") or "").lower() not in ("table", "view"):
                continue
            try:
                useful = usefulness(
                    ".".join(part for part in (
                        match.get("schema"),
                        match.get("physical_object") or match.get("name"))
                        if part)) or {}
            except Exception:                   # noqa: BLE001
                useful = {}
            liveness = str(useful.get("liveness") or "unknown")
            if liveness == "empty":
                # EVERY believed-empty table is refused as a candidate: an
                # alias onto it would answer future questions from nothing.
                # This is safe precisely because of the stale-stats work --
                # an "empty" contradicted by later DML is already reported
                # as UNKNOWN (contradicted) by the profiler and stays
                # offered with its caveat; what still says "empty" here is
                # either verified current or the best evidence available.
                continue
            candidates.append(_candidate(match, useful))
            if len(candidates) >= max_candidates:
                break
        gaps.append({
            **entry,
            "candidates": candidates,
            "gap_kind": ("named_object" if _RECORDISH.fullmatch(term)
                         else ("no_candidates" if not candidates
                               else "unaliased_term")),
        })
    return {
        "source": str(source or "default").strip() or "default",
        "gaps": gaps,
        "note": (
            "Deterministic mining of the question log: no model drafted "
            "anything here. Candidates come from the same catalog search "
            "the agent uses, ranked with the profiler's liveness and value "
            "evidence. Proposing creates a PENDING metadata proposal that "
            "changes nothing until a human approves it."),
    }
