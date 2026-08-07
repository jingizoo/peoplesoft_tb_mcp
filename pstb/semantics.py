"""The concept register: which codes define a question's population.

"Total invoice amount" is defined by BILL_STATUS = 'INV' — at MOST sites.
A site that finalizes through a custom PRO status, or wants pro-forma
bills counted, should not need a code change to be answered correctly.
So the defining codes for a named CONCEPT resolve through a sourcing
ladder, and the winning rung travels with the answer as provenance:

    approved site memory  >  config.yaml semantics:  >  built-in seed

Two deliberate restraints, both lessons from the design review:

* ONE seeded concept. Every other candidate examined (asset cost, journal
  amounts, voucher status, payment status) turned out to need a governor
  set, a cross-record amount, or a site-specific election — seeding them
  from memory of PeopleSoft is how a register becomes a liability. A new
  seed must arrive with the same evidence this one did.
* Overrides change VALUES, never the column and never SQL. The resolved
  codes drive result CLASSIFICATION in Python (the grouped query already
  returns every status), so a taught override has zero injection surface
  and cannot even change what the database is asked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Concept:
    name: str
    record: str
    column: str
    values: tuple
    meaning: str


SEEDS = {
    "billing_invoiced": Concept(
        name="billing_invoiced",
        record="PS_BI_HDR",
        column="BILL_STATUS",
        values=("INV",),
        meaning="finalized bills only — the only status that is revenue",
    ),
}

# An approved site-memory fact overrides a concept with this exact syntax
# (kind "convention"):  semantics: billing_invoiced = BILL_STATUS IN (INV, PRO)
_FACT_RE = re.compile(
    r"(?i)^\s*semantics\s*:\s*([a-z_]+)\s*=\s*([A-Z_][A-Z0-9_]*)\s*"
    r"IN\s*\(\s*([A-Z0-9_,\s]+)\s*\)\s*$")

_CODE_RE = re.compile(r"^[A-Z0-9_]{1,12}$")


@dataclass
class Resolution:
    concept: Concept
    values: tuple
    rung: str          # memory | config | seed
    source: str        # human-readable provenance for the population block
    notes: list = field(default_factory=list)

    @property
    def predicate(self) -> str:
        if len(self.values) == 1:
            return f"{self.concept.column} = '{self.values[0]}'"
        return (f"{self.concept.column} IN ("
                + ", ".join(f"'{v}'" for v in self.values) + ")")


def _clean_values(raw, notes: list, rung: str):
    """Validate an override's codes; a bad override is skipped, named, and
    the ladder falls through — never a crash, never a silent acceptance."""
    values = []
    for v in (raw or []):
        code = str(v).strip().upper()
        if not _CODE_RE.match(code):
            notes.append(f"{rung} override ignored: {v!r} is not a valid "
                         "status code")
            return None
        values.append(code)
    if not values:
        notes.append(f"{rung} override ignored: no status codes given")
        return None
    return tuple(dict.fromkeys(values))


def resolve(name: str, cfg=None, memory=None) -> Resolution:
    """Walk the ladder for one concept. Unknown names raise — a concept
    that was never seeded has no safe default to fall back to."""
    seed = SEEDS.get(name)
    if seed is None:
        raise KeyError(f"unknown concept {name!r}; seeded: "
                       + ", ".join(sorted(SEEDS)))
    notes: list = []

    # Rung 1: approved site memory. Pending facts NEVER take effect —
    # approval is the entire governance story.
    for fact in (memory.approved() if memory is not None else []):
        m = _FACT_RE.match(str(fact.get("text") or ""))
        if not m or m.group(1).lower() != name:
            continue
        if m.group(2).upper() != seed.column:
            notes.append(
                f"site-memory override ignored: it names column "
                f"{m.group(2).upper()}, but {name} is defined on "
                f"{seed.column} — changing the column needs a code change")
            continue
        values = _clean_values(m.group(3).split(","), notes, "site-memory")
        if values:
            return Resolution(
                concept=seed, values=values, rung="memory",
                source=("approved site memory"
                        + (f" ({fact.get('decided', '')[:10]})"
                           if fact.get("decided") else "")),
                notes=notes)

    # Rung 2: config.yaml semantics: {name: {values: [...]}}
    override = (getattr(cfg, "semantics", None) or {}).get(name)
    if isinstance(override, dict):
        values = _clean_values(override.get("values"), notes, "config")
        if values:
            return Resolution(concept=seed, values=values, rung="config",
                              source="config.yaml semantics", notes=notes)

    return Resolution(concept=seed, values=seed.values, rung="seed",
                      source=f"built-in default: {name}", notes=notes)
