"""How to get from one record to another, proved against this instance.

THE GAP THIS FILLS. A model writing ad-hoc SQL has to invent its joins.
It has the record names (search_records), the columns (describe_table) and
the optimizer's opinion of a finished query (explain_query) — but nothing
tells it that PS_ITEM reaches PS_CUSTOMER through SETID and CUST_ID, or
that the hop is only cheap if those columns lead an index. So it guesses,
and a guessed join against a transaction table does not come back wrong,
it comes back in four minutes or not at all.

WHAT THE EDGES ARE MADE OF. Shared COLUMN NAMES, read from the live
catalog, weighted by whether those columns are a leading prefix of a real
index on each side. Both facts come from the database itself —
db.columns() and db.indexes() — not from an assumed PeopleTools shape.
That matters twice over: sites customise records, and this app has been
burned before by confidently naming a PeopleTools column that does not
exist. Nothing here is asserted; it is all read.

WHY LEADING-PREFIX IS THE WHOLE RANKING. An index can only be used when
the predicate covers its LEADING columns, so "these two records share
BUSINESS_UNIT and CUST_ID, and both index on (BUSINESS_UNIT, CUST_ID…)"
is a hop the optimizer can do with range scans. The same two records
sharing only DESCR is a hop that reads both tables end to end. They look
identical in a schema diagram and differ by hours at runtime, which is
exactly the distinction a model cannot make and a catalog can.

WHY THE UNIVERSE IS BOUNDED. A real instance has tens of thousands of
records; every-pair edge building is O(n²) catalog reads and would be its
own outage. The graph is built over a RELEVANT universe — the curated
record map, plus whatever records the caller names — which is tens of
records, each already cached by Database. Expansion is explicit, never a
crawl.

WHAT THIS IS NOT. It does not run anything, and it does not promise a
join is CORRECT for a question — shared column names are evidence, not
proof of a foreign key, and the path says so with a confidence. It
narrows "guess a join" to "pick from paths that exist and say which one
the indexes support", and leaves the arithmetic and the semantics where
they already live.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# Columns that appear on almost every PeopleSoft record and join almost
# nothing on their own. Sharing one of these is not evidence of a
# relationship — SETID and BUSINESS_UNIT are the two most load-bearing
# columns in the product AND the two most likely to connect records that
# have nothing to do with each other. They still count when they appear
# ALONGSIDE a specific key (SETID + CUST_ID is a real join); they never
# make an edge by themselves.
WEAK_ALONE = frozenset({
    "SETID", "BUSINESS_UNIT", "SETCNTRLVALUE", "EFFDT", "EFF_STATUS",
    "DESCR", "DESCR254", "DESCR254_MIXED", "DESCRSHORT", "CURRENCY_CD",
    "BASE_CURRENCY", "LEDGER", "FISCAL_YEAR", "ACCOUNTING_PERIOD",
    "OPRID", "LASTUPDDTTM", "LASTUPDOPRID", "ROW_ADDED_DTTM", "SYNCID",
    "STATUS", "CREATED_DTTM", "PROCESS_INSTANCE",
})

# Never worth walking THROUGH: metadata records that share key names with
# half the dictionary and would make every record two hops from every
# other one.
NEVER_INTERMEDIATE = frozenset({
    "PSRECDEFN", "PSRECFIELD", "PSQRYDEFN", "PSQRYBIND", "PSQRYRECORD",
    "PSQRYSTATS", "PSOPRDEFN", "PSTREEDEFN",
})

# A shared column name is only a candidate join if it identifies something.
# PS_BI_HDR and PS_VOUCHER both carry INVOICE_DT; PS_VOUCHER and PS_ITEM both
# carry DUE_DT. Joining bills to vouchers on a DATE is not a relationship, it
# is a cartesian product with a plausible-looking ON clause — and it was the
# first thing this produced before the rule existed. Dates, amounts, rates,
# quantities, flags and descriptions are VALUES: two records agreeing on one
# says nothing about them being about the same thing.
_VALUE_SUFFIXES = ("_DT", "_DTTM", "_TM", "_AMT", "_AMOUNT", "_BAL", "_PCT",
                   "_RATE", "_QTY", "_FLG", "_FLAG", "_SW", "_STATUS",
                   "_TOTAL", "_COUNT")
_VALUE_EXACT = frozenset({"AMOUNT", "MONETARY_AMOUNT", "QTY", "RATE", "DESCR",
                          "NAME1", "NAME2", "ADDRESS1", "ADDRESS2", "CITY",
                          "STATE", "COUNTRY", "POSTAL"})


def is_joinable_column(name: str) -> bool:
    """Could this column key a join, or is it a value that merely matches?"""
    col = str(name or "").strip().upper()
    if not col or col in _VALUE_EXACT:
        return False
    if col.startswith("DESCR"):
        return False
    return not col.endswith(_VALUE_SUFFIXES)


MAX_HOPS = 4
CACHE_TTL_SECONDS = 900


@dataclass(frozen=True)
class Hop:
    """One join, and whether the database can do it cheaply."""

    left: str
    right: str
    on: tuple                      # shared column names, most specific first
    left_leads: bool               # `on` is a leading prefix of a left index
    right_leads: bool
    specific: tuple                # the columns that are not weak-alone
    # Constants that would turn a scan into a range scan on each side —
    # normally SETID or BUSINESS_UNIT, which the question already fixed.
    left_supply: tuple = ()
    right_supply: tuple = ()

    @property
    def indexed(self) -> bool:
        return self.left_leads and self.right_leads

    @property
    def indexable(self) -> bool:
        """Reachable by adding constants the caller already has."""
        return ((self.left_leads or bool(self.left_supply))
                and (self.right_leads or bool(self.right_supply)))

    def cost(self) -> float:
        """Lower is better. A hop neither side indexes is not 'slower', it
        is a different kind of query — hence the gap, not a nudge."""
        if self.indexed:
            return 1.0
        if self.indexable:
            return 2.0
        if self.left_leads or self.right_leads:
            return 3.0
        return 12.0

    def supply(self) -> tuple:
        return tuple(sorted(set(self.left_supply) | set(self.right_supply)))

    def sql(self, left_alias: str, right_alias: str) -> str:
        return " AND ".join(f"{left_alias}.{c} = {right_alias}.{c}"
                            for c in self.on)

    def describe(self) -> str:
        if self.indexed:
            how = "both sides index it"
        elif self.indexable:
            how = ("indexed once you also pin "
                   + ", ".join(self.supply()) + " as a constant")
        elif self.left_leads or self.right_leads:
            how = "one side indexes it"
        else:
            how = "NEITHER side indexes it — this hop scans"
        return f"{self.left} -> {self.right} on {', '.join(self.on)} ({how})"


@dataclass
class JoinPath:
    hops: list = field(default_factory=list)

    @property
    def records(self) -> list:
        if not self.hops:
            return []
        return [self.hops[0].left] + [h.right for h in self.hops]

    def cost(self) -> float:
        return sum(h.cost() for h in self.hops)

    def confidence(self) -> str:
        """Shared column names are EVIDENCE of a relationship, not proof of
        one. Said out loud, because a path presented as fact is a path the
        model will not sanity-check."""
        if not self.hops:
            return "none"
        if (all(h.indexed or h.indexable for h in self.hops)
                and all(h.specific for h in self.hops)):
            return "high"
        if any(not h.specific for h in self.hops):
            return "low"
        return "medium"

    def to_sql(self) -> str:
        """A FROM/JOIN skeleton. Deliberately not a whole SELECT: what to
        select and how to filter is the question's business, and a
        half-guessed SELECT list is how a plausible wrong answer starts."""
        if not self.hops:
            return ""
        alias = {}
        for i, name in enumerate(self.records):
            alias[name] = f"T{i}"
        lines = [f"FROM {self.records[0]} {alias[self.records[0]]}"]
        for hop in self.hops:
            lines.append(f"  JOIN {hop.right} {alias[hop.right]} "
                         f"ON {hop.sql(alias[hop.left], alias[hop.right])}")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "records": self.records,
            "hops": [{"from": h.left, "to": h.right, "on": list(h.on),
                      "indexed_both_sides": h.indexed,
                      "left_leads_index": h.left_leads,
                      "right_leads_index": h.right_leads,
                      "pin_as_constants": list(h.supply()),
                      "note": h.describe()} for h in self.hops],
            "cost": round(self.cost(), 1),
            "confidence": self.confidence(),
            "sql": self.to_sql(),
        }


# Columns a caller can nearly always supply as a literal, because the
# question already fixed them: the scope bar picks the unit, SETID is
# resolved from it. They are exactly the columns PeopleSoft leads its
# indexes with, which is why so many joins look unindexed until you notice
# the constant is available.
SUPPLIABLE = frozenset({"SETID", "BUSINESS_UNIT", "SETCNTRLVALUE",
                        "LEDGER", "FISCAL_YEAR", "ACCOUNTING_PERIOD"})


def _index_fit(indexes, columns: set) -> tuple:
    """(leads, supply) — can an index be used, and what would it take?

    Leading position, not membership: the optimizer cannot range-scan an
    index whose first column is missing from the predicate, so an index on
    (SETID, CUST_ID) is useless to a join supplying only CUST_ID.

    But "useless" is usually wrong by one step, and that step is the
    useful part of this whole file. PeopleSoft leads its indexes with
    SETID and BUSINESS_UNIT — values the caller ALREADY HAS, because the
    scope bar fixed them. So when the only thing standing between a join
    and a range scan is a constant the question already knows, this
    reports the constant instead of shrugging: "add SETID = :setid and
    this stops being a scan."
    """
    best_supply: Optional[tuple] = None
    for index in indexes or ():
        cols = [str(c).upper() for c in (index.get("columns") or [])]
        if not cols:
            continue
        if cols[0] in columns:
            return True, ()
        # How much of the leading run could be satisfied by constants the
        # caller can supply, before we reach a column the join provides?
        supply = []
        for col in cols:
            if col in columns:
                candidate = tuple(supply)
                if best_supply is None or len(candidate) < len(best_supply):
                    best_supply = candidate
                break
            if col in SUPPLIABLE:
                supply.append(col)
                continue
            break
    return False, (best_supply or ())


class RecordGraph:
    """Join paths between records, derived from the live catalog."""

    def __init__(self, db, seed_records=()):
        self.db = db
        self._seed = [r.upper() for r in (seed_records or ())]
        self._lock = threading.RLock()
        self._cols: dict = {}
        self._idx: dict = {}
        self._expires = 0.0

    # ---- the catalog, read once and cached -------------------------------
    def _load(self, records) -> None:
        now = time.monotonic()
        with self._lock:
            if now > self._expires:
                self._cols.clear()
                self._idx.clear()
                self._expires = now + CACHE_TTL_SECONDS
            missing = [r for r in records if r not in self._cols]
        for record in missing:
            try:
                cols = {c.upper() for c in self.db.columns(record)}
            except Exception:
                cols = set()
            try:
                idx = self.db.indexes(record)
            except Exception:
                idx = []
            with self._lock:
                self._cols[record] = cols
                self._idx[record] = idx

    def universe(self, extra=()) -> list:
        names = list(dict.fromkeys(
            self._seed + [r.upper() for r in (extra or ()) if r]))
        self._load(names)
        return [n for n in names if self._cols.get(n)]

    # ---- edges -----------------------------------------------------------
    def hop(self, left: str, right: str) -> Optional[Hop]:
        """The join between two records, or None when they share nothing
        that could be one."""
        left, right = left.upper(), right.upper()
        self._load([left, right])
        shared = {c for c in (self._cols.get(left, set())
                              & self._cols.get(right, set()))
                  if is_joinable_column(c)}
        if not shared:
            return None
        specific = tuple(sorted(shared - WEAK_ALONE))
        if not specific:
            # Only columns every record carries. PS_GL_ACCOUNT_TBL and
            # PS_DEPT_TBL both have SETID and EFFDT, and joining them on
            # that pair produces every account against every department —
            # which the path finder happily routed THROUGH before this
            # rule, because two weak columns looked like more evidence
            # than one. Two coincidences are not a relationship.
            return None
        ordered = specific + tuple(sorted(shared & WEAK_ALONE))
        left_leads, left_supply = _index_fit(self._idx.get(left), set(ordered))
        right_leads, right_supply = _index_fit(self._idx.get(right),
                                               set(ordered))
        return Hop(
            left=left, right=right, on=ordered,
            left_leads=left_leads, right_leads=right_leads,
            specific=specific,
            left_supply=left_supply, right_supply=right_supply,
        )

    def neighbours(self, record: str, among) -> list:
        out = []
        for other in among:
            if other == record:
                continue
            hop = self.hop(record, other)
            if hop is not None:
                out.append(hop)
        return out

    # ---- the question ----------------------------------------------------
    def path(self, start: str, goal: str, extra=(),
             max_hops: int = MAX_HOPS, exclude=()) -> Optional[JoinPath]:
        """Cheapest join path, or None.

        Cheapest by INDEX SUPPORT first and hop count second — a two-hop
        walk both sides index beats a direct join neither side does, and
        that ordering is the entire point of building this from the
        catalog rather than from a diagram.
        """
        start, goal = start.upper(), goal.upper()
        blocked = {str(name or "").strip().upper()
                   for name in (exclude or ()) if str(name or "").strip()}
        pool = [name for name in self.universe(list(extra) + [start, goal])
                if name not in blocked]
        if start not in pool or goal not in pool:
            return None
        if start == goal:
            return JoinPath(hops=[])

        # Small graph, so a plain Dijkstra over (cost, hops) is enough and
        # stays readable — no heap, no early-exit cleverness to get wrong.
        best: dict = {start: (0.0, JoinPath(hops=[]))}
        frontier = {start}
        for _ in range(max_hops):
            nxt: set = set()
            for node in frontier:
                node_cost, node_path = best[node]
                for hop in self.neighbours(node, pool):
                    target = hop.right
                    if target != goal and target in NEVER_INTERMEDIATE:
                        continue
                    cost = node_cost + hop.cost()
                    if target in best and best[target][0] <= cost:
                        continue
                    best[target] = (cost,
                                    JoinPath(hops=node_path.hops + [hop]))
                    nxt.add(target)
            if not nxt:
                break
            frontier = nxt
        found = best.get(goal)
        return found[1] if found else None

    def paths_from(self, record: str, extra=(), exclude=()) -> list:
        """Everything this record can reach in one hop, cheapest first —
        the answer to "what can I join this to", which is the question
        somebody asks before they know the target."""
        record = record.upper()
        blocked = {str(name or "").strip().upper()
                   for name in (exclude or ()) if str(name or "").strip()}
        pool = [name for name in self.universe(list(extra) + [record])
                if name not in blocked]
        hops = self.neighbours(record, pool)
        hops.sort(key=lambda h: (h.cost(), -len(h.specific), h.right))
        return hops
