"""Which physical tables are worth reading, decided without trusting names.

The PeopleSoft side of this deployment can be decoded: PeopleTools carries
record descriptions, field labels and translate values, and the catalog
already mines them. The custom schemas cannot. There the names lie or say
nothing, and a live table sits beside ``_OLD``, ``_BKP`` and ``_2024``
copies of itself with nothing in any data dictionary to say which one an
answer should come from.

So nothing here reads meaning out of a name. Every signal is either a
statistic the database already computed, or a structural fact the catalog
already holds:

* is anything in it (row estimate),
* is any of it filled in (columns the optimizer saw distinct values in),
* does anything else point at it (views, foreign keys, indexes, records),
* and do we even know (a table that was never analyzed is UNKNOWN, which
  is a different thing from empty and must never be reported as empty).

Names are used for exactly one job, and only ever as the *second* of two
independent signals: once two tables are already known to have identical
column signatures, a name like ``PS_VOUCHER_BKP`` next to ``PS_VOUCHER``
is what tells them apart. Shape alone is not enough -- schemas are full of
legitimately identical shapes -- and a name alone is not enough either.

Everything in this module is a pure function over plain data so the
judgements can be tested without a database, and so the caller can show
its work: every score comes back with the components that produced it.
"""
from __future__ import annotations

import re
from typing import Iterable, Mapping, Sequence

# A table the optimizer has never analyzed reports NUM_ROWS as NULL. That
# is not zero, and the difference is the whole point: "empty" retires a
# table, "unknown" only says nobody has measured it. Conflating them would
# quietly hide live data behind a confident-looking verdict.
UNKNOWN = "unknown"
EMPTY = "empty"
POPULATED = "populated"

# Suffixes and prefixes that a human attaches when they keep a copy. Each
# is only ever consulted for two tables that ALREADY share a column
# signature, so "ARCHIVE" here does not mean "junk" -- it means "when a
# question is about this shape, the other one is the table to read".
_COPY_WORDS = (
    "OLD", "BAK", "BKP", "BACKUP", "COPY", "COPIA", "SAVE", "SAVED",
    "ORIG", "ORIGINAL", "PREV", "PRIOR", "ARCH", "ARCHIVE", "ARCHIVED",
    "HIST", "HISTORY", "DEL", "DELETE", "DELETED", "DROP", "OBS",
    "OBSOLETE", "UNUSED", "TMP", "TEMP", "WRK", "WORK", "SCRATCH",
    "TEST", "DUMMY", "DUPE", "DUPLICATE", "BROKEN", "BAD", "FIX",
)
# 2024 / 202401 / 20240115 / 240115, and PeopleTools' numbered temp
# instances (PS_AP_TAO1 .. PS_AP_TAO9).
_DATEISH = re.compile(r"^(19|20)\d{2}(\d{2}){0,2}$")
_SHORT_DATE = re.compile(r"^\d{6}$")
_TRAILING_DIGITS = re.compile(r"^(?P<stem>.*?)(?P<digits>\d{1,3})$")
_SPLIT = re.compile(r"[^A-Z0-9]+")

COPY_MARKER = "copy_marker"
NUMBERED_SIBLING = "numbered_sibling"


def column_signature(columns: Iterable[Mapping]) -> str:
    """A shape fingerprint: ordered column names and their base types.

    Deliberately order-INSENSITIVE and length-insensitive. A backup taken
    with ``CREATE TABLE ... AS SELECT`` keeps the columns but routinely
    loses declared lengths and column order, so folding those in would
    make a copy look like a different table -- which is the one thing this
    must not do.
    """
    parts = []
    for column in columns or ():
        name = str(column.get("name") or "").strip().upper()
        if not name:
            continue
        parts.append(f"{name}:{_base_type(column.get('data_type'))}")
    return "|".join(sorted(parts))


def _base_type(declared: object) -> str:
    """VARCHAR2(30) -> VARCHAR2. NUMBER(15,2) -> NUMBER."""
    text = str(declared or "").strip().upper()
    if not text:
        return ""
    return _SPLIT.split(text)[0] if text else ""


def _tokens(name: str) -> list[str]:
    return [t for t in _SPLIT.split(str(name or "").upper()) if t]


def name_relation(canonical: str, other: str) -> str:
    """How ``other``'s name differs from ``canonical``'s, or "" if it does not.

    Only ever called for two objects already proven to share a column
    signature. The answer is one of COPY_MARKER, NUMBERED_SIBLING or "".

    Returning "" for an unrecognised difference is the important case: two
    tables with the same shape and unrelated names are NOT evidence of a
    copy. Schemas are full of identically shaped tables that are genuinely
    different things, and a wrong "prefer that one instead" is worse than
    saying nothing.
    """
    left, right = _tokens(canonical), _tokens(other)
    if not left or not right or left == right:
        return ""

    # other == canonical + marker  (PS_VOUCHER -> PS_VOUCHER_BKP)
    if len(right) > len(left) and right[:len(left)] == left:
        return _marker_kind(right[len(left):])
    # other == marker + canonical  (PS_VOUCHER -> OLD_PS_VOUCHER)
    if len(right) > len(left) and right[-len(left):] == left:
        return _marker_kind(right[:-len(left)])

    # Same token count, last token differs only by a trailing number or a
    # copy word glued on: PS_AP_TAO -> PS_AP_TAO1, PS_JRNL -> PS_JRNLOLD.
    if len(right) == len(left) and right[:-1] == left[:-1]:
        return _glued_marker(left[-1], right[-1])
    return ""


def _marker_kind(extra: Sequence[str]) -> str:
    """Classify the tokens one name carries and the other does not."""
    if not extra or len(extra) > 2:
        return ""
    kinds = set()
    for token in extra:
        if token in _COPY_WORDS:
            kinds.add(COPY_MARKER)
        elif _DATEISH.match(token) or _SHORT_DATE.match(token):
            kinds.add(COPY_MARKER)
        elif token.isdigit():
            kinds.add(NUMBERED_SIBLING)
        else:
            return ""            # an unrecognised word is a real difference
    if COPY_MARKER in kinds:
        return COPY_MARKER
    return NUMBERED_SIBLING if kinds else ""


def _glued_marker(base: str, variant: str) -> str:
    """PS_AP_TAO vs PS_AP_TAO1, or PS_JRNL vs PS_JRNLOLD."""
    if not variant.startswith(base) or variant == base:
        return ""
    tail = variant[len(base):]
    if tail.isdigit():
        return NUMBERED_SIBLING
    if tail in _COPY_WORDS or _DATEISH.match(tail) or _SHORT_DATE.match(tail):
        return COPY_MARKER
    return ""


def reconcile_liveness(state: str, modified_since_stats) -> tuple:
    """(state, contradicted): an EMPTY verdict with DML recorded after the
    statistics were gathered is no verdict at all.

    Oracle's modification tracking is cleared every time statistics are
    gathered, so any surviving count is change the stats have not seen.
    Only EMPTY is reconciled, because only EMPTY is dangerous when stale:
    it makes ranking skip the table and shadow detection redirect away
    from it, and both read as correct. A stale POPULATED fails soft -- a
    query finds nothing and says so -- and UNKNOWN asserts nothing that
    could be contradicted (activity on a never-analyzed table is surfaced
    as a caveat instead, not a state change).
    """
    try:
        mods = 0 if modified_since_stats is None else int(modified_since_stats)
    except (TypeError, ValueError):
        mods = 0
    if state == EMPTY and mods > 0:
        return UNKNOWN, True
    return state, False


def liveness(row_estimate: object, *, analyzed: bool = True) -> str:
    """POPULATED / EMPTY / UNKNOWN from a row estimate.

    ``analyzed=False`` forces UNKNOWN even when a number arrived, for the
    dialects where the number is a guess rather than a measurement.
    """
    if not analyzed or row_estimate is None:
        return UNKNOWN
    try:
        rows = int(row_estimate)
    except (TypeError, ValueError):
        return UNKNOWN
    if rows < 0:
        return UNKNOWN
    return EMPTY if rows == 0 else POPULATED


def _saturate(value: float, full: float) -> float:
    """0..1, rising fast at first and flattening -- 10 rows and 10 million
    rows are both 'has data', and the difference between 3 references and
    4 matters far more than between 300 and 400."""
    if value <= 0 or full <= 0:
        return 0.0
    import math
    return min(1.0, math.log10(1.0 + value) / math.log10(1.0 + full))


def value_score(profile: Mapping) -> dict:
    """How likely this object is to be worth reading, with its reasons.

    Returns ``{"score": float, "components": {...}}``. The components are
    part of the contract: a bare number nobody can argue with is not a
    reviewable judgement, and every consumer of this shows its work.

    NOT included, deliberately:

    * Anything derived from the object's name. That is the whole premise.
    * Data recency. The statistics carry LAST_ANALYZED, which says when the
      optimizer last looked -- not when a row last changed. Treating stats
      age as data age would retire a busy table that nobody has re-analyzed
      since 2019, which in a mismanaged schema is most of them. Real
      recency needs DBA_TAB_MODIFICATIONS or an audit-column probe, and
      neither is available on a read-only account without a grant.
    """
    state = str(profile.get("liveness") or UNKNOWN)
    rows = profile.get("row_estimate")
    columns = int(profile.get("column_count") or 0)
    # None is "we could not measure this", which is not the same as zero
    # populated columns. Only per-column optimizer statistics can answer
    # it, and a dialect or an account without them must not be scored as
    # though every column were empty.
    populated = profile.get("populated_columns")
    references = int(profile.get("reference_count") or 0)
    inherited = profile.get("inherited_rows")

    # An object nobody can read is worth nothing regardless of its shape.
    if state == EMPTY:
        population, basis = 0.0, "measured"
    elif state == POPULATED:
        population, basis = _saturate(float(rows or 0), 1_000_000.0), "measured"
    elif inherited is not None:
        # A view is never in table statistics, so its own row count can
        # never be measured. What it selects FROM can be, and a view over
        # a live table is live.
        population, basis = (_saturate(float(inherited), 1_000_000.0),
                             "inherited")
    elif profile.get("stats_contradicted"):
        # The statistics said EMPTY and the modification log says rows
        # changed after they were gathered. Same midpoint as unmeasured --
        # the honest amount of knowledge is the same -- but the basis is
        # different and a reader deciding whether to trust a skip needs to
        # see WHY this table is unknown.
        population, basis = 0.5, "contradicted"
    else:
        # A PRIOR, not a measurement, and it is reported as one. Unknown
        # means unmeasured -- in a schema where nothing has been analyzed,
        # scoring it zero would flatten every table and rank on
        # connectivity alone. Held at the midpoint, which does mean an
        # unmeasured object can outrank a measured near-empty one; the
        # basis is in the components so a reader can see that happening
        # rather than discovering it in a ranking they cannot explain.
        population, basis = 0.5, "unmeasured"

    if state == EMPTY:
        breadth = 0.0
    elif populated is None or not columns:
        breadth = 0.5                    # unmeasured, held at the midpoint
    elif state == UNKNOWN:
        breadth = 0.5
    else:
        breadth = min(1.0, int(populated) / columns)
    connectivity = _saturate(float(references), 25.0)

    components = {
        "population": round(population, 4),
        "population_basis": basis,
        "breadth": round(breadth, 4),
        "connectivity": round(connectivity, 4),
    }
    score = (0.45 * population) + (0.25 * breadth) + (0.30 * connectivity)
    return {"score": round(score, 4), "components": components}


def _rank_key(profile: Mapping) -> tuple:
    """Which of two identically shaped tables is the one to read."""
    state = str(profile.get("liveness") or UNKNOWN)
    return (
        1 if state == POPULATED else (0 if state == UNKNOWN else -1),
        int(profile.get("row_estimate") or 0),
        int(profile.get("reference_count") or 0),
        # Shortest name last: PS_VOUCHER beats PS_VOUCHER_BKP on a tie, and
        # ties are common when nothing in the schema has been analyzed.
        -len(str(profile.get("name") or "")),
    )


def shadow_candidates(profiles: Sequence[Mapping]) -> list[dict]:
    """Pairs where one object is a copy of another and should not be read.

    Requires BOTH signals: an identical column signature AND a name that
    differs only by a recognised copy marker or trailing number. Same shape
    with unrelated names yields nothing -- see name_relation.

    Never proposes a canonical that is itself empty while the candidate has
    rows. A backup that was kept because the original was truncated is a
    real thing, and pointing an answer at the empty one would be worse than
    the ambiguity it is trying to resolve.
    """
    by_signature: dict[str, list[Mapping]] = {}
    for profile in profiles:
        signature = str(profile.get("signature") or "")
        if not signature:
            continue                      # a shape we could not read
        by_signature.setdefault(signature, []).append(profile)

    out: list[dict] = []
    for signature, group in sorted(by_signature.items()):
        if len(group) < 2:
            continue
        ordered = sorted(group, key=_rank_key, reverse=True)

        # A copy marker is a deliberate human act; a row estimate is an
        # artifact of whether a DBA ever ran gather_stats. So a name that
        # marks itself a copy of another member can never be the canonical
        # one, even when it is the only member with statistics. Choosing
        # PS_VOUCHER_OLD over an unanalyzed PS_VOUCHER because the copy
        # happens to have been analyzed is exactly backwards.
        marked = {
            id(member) for member in group
            if any(member is not base
                   and name_relation(str(base.get("name") or ""),
                                     str(member.get("name") or ""))
                   for base in group)
        }
        unmarked = [m for m in ordered if id(m) not in marked]
        canonical = unmarked[0] if unmarked else ordered[0]

        for other in ordered:
            if other is canonical:
                continue
            relation = name_relation(str(canonical.get("name") or ""),
                                     str(other.get("name") or ""))
            if not relation:
                continue
            if (str(canonical.get("liveness")) == EMPTY
                    and str(other.get("liveness")) == POPULATED):
                continue
            out.append({
                "canonical_id": canonical.get("node_id"),
                "canonical": canonical.get("name"),
                "shadow_id": other.get("node_id"),
                "shadow": other.get("name"),
                "relation": relation,
                "signature_columns": len(signature.split("|")),
                "canonical_rows": canonical.get("row_estimate"),
                "shadow_rows": other.get("row_estimate"),
            })
    return out
