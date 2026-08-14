"""How work actually flows through this installation — pages, records, docs.

THE GAP THIS FILLS. Asked "how do we do invoicing for India", nothing in
this app could answer. It knows how to JOIN records (pstb/graph.py) and how
to total them, but not what a process IS: which pages a person opens, which
component those pages belong to, what the navigation path is, which records
the pages write, which setup tables govern them, and which written procedure
describes the whole thing. That knowledge exists — it is spread across
PeopleTools metadata, the live catalog, the wiki and the curated record map
— and no single query assembles it.

TWO GRAPHS, DIFFERENT JOBS. Keep them apart:

    pstb/graph.py   DATA graph. "How do I get from PS_ITEM to PS_CUSTOMER?"
                    Built live over a bounded universe, thrown away, weighted
                    by whether the shared columns lead an index. It exists to
                    make one SQL statement fast and correct.

    this module     PROCESS graph. "How does invoicing work here?" Wide, slow
                    to build, stable between customizations, and read
                    hundreds of times per build. It exists to explain, not to
                    compute.

They want opposite storage. A process graph rebuilt per question would put a
PeopleTools metadata crawl on the critical path of a chat turn, which is the
one thing this project protects hardest. So it is built OFFLINE by
scripts/build_process_graph.py into a SQLite artifact and only read at
question time: a full-text hit to find the seeds, then a bounded walk. Cost
at question time is a handful of indexed reads against a local file.

WHY SQLITE AND NOT A GRAPH DATABASE. The traversals are shallow (3-4 hops
from a seed) and the whole graph is about a hundred thousand nodes/edges by
default — neither needs a graph engine. SQLite is already a dependency,
gives FTS5 for seed matching and indexed adjacency for the walk, rebuilds
atomically, and adds no service to operate. The financial system of record
stays where it is; this is a derived, refreshable INDEX over it and holds no
amounts.

WHAT IS IN IT, AND WHAT IS NOT. Structure only: record names, page names,
component and navigation names, module membership, document titles, and the
edges between them. No balances, no customer or supplier names, no bank or
tax identifiers, no row values other than the SETUP CODES that define scope
(business unit, SETID, currency, country). A process graph that quietly
became a data extract would be a much bigger thing to secure than it looks.

EVERY EDGE NAMES ITS SOURCE. A page-to-record edge read from PSPNLFIELD is a
fact about this instance. A record-to-record edge from shared column names
is EVIDENCE. A doc-to-record edge from a wiki page mentioning a table is a
weaker signal still. They are all useful and they are not the same thing, so
every edge carries its source and its weight, the walk ranks by them, and
the answer says which is which. Nothing here is inferred by a model.

SITES DIFFER, SO EVERY SOURCE IS OPTIONAL. Harvesters probe before they
read and degrade one at a time: an instance whose PSPNLFIELD is not granted
loses the page layer and is TOLD so in the build report, rather than failing
the build or, worse, silently producing a graph with a hole in it that reads
as "invoicing touches no pages".
"""
from __future__ import annotations

import itertools
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_FILENAME = "process_graph.db"

# Build-time defaults. These used to be unrelated 2k/4k/6k ceilings (except
# PSPNLFIELD at 60k), so a normal large PeopleTools installation quietly wrote
# a graph missing everything after the alphabetical cutoff. 100k is large
# enough for the intended installation-wide index; absolute ceilings below
# remain as a guard against turning it into an unbounded catalog mirror.
MAX_RECORDS = 100_000
MAX_PAGES = 100_000
MAX_PAGE_FIELDS = 100_000
MAX_COMPONENTS = 100_000
MAX_NAV = 100_000
MAX_QUERIES = 100_000
MAX_DOCS = 100_000
MAX_FIELDS_PER_RECORD = 40

HARD_MAX_CATALOG_ROWS = 1_000_000
HARD_MAX_NODES = 1_000_000
HARD_MAX_EDGES = 2_000_000
HARD_MAX_MEMORY_MB = 2_048
HARD_MAX_PAGE_SIZE = 25_000


@dataclass(frozen=True)
class GraphBuildLimits:
    """Configurable build ceilings, validated against absolute safeguards."""

    max_records: int = MAX_RECORDS
    max_pages: int = MAX_PAGES
    max_page_fields: int = MAX_PAGE_FIELDS
    max_components: int = MAX_COMPONENTS
    max_navigation: int = MAX_NAV
    max_queries: int = MAX_QUERIES
    query_page_size: int = 5_000
    max_nodes: int = 100_000
    max_edges: int = 100_000
    memory_budget_mb: int = 512
    write_batch_size: int = 2_000

    @classmethod
    def from_config(cls, cfg=None, **overrides):
        source = cfg or object()
        values = {
            name: overrides.get(name, getattr(source, name, field.default))
            for name, field in cls.__dataclass_fields__.items()
        }
        try:
            limits = cls(**{k: int(v) for k, v in values.items()})
        except (TypeError, ValueError) as e:
            raise ProcessGraphError(
                f"process_graph limits must be whole numbers: {e}") from e
        limits.validate()
        return limits

    def validate(self) -> None:
        catalog = {
            "max_records": self.max_records, "max_pages": self.max_pages,
            "max_page_fields": self.max_page_fields,
            "max_components": self.max_components,
            "max_navigation": self.max_navigation,
            "max_queries": self.max_queries,
        }
        for name, value in catalog.items():
            _bounded_limit(name, value, HARD_MAX_CATALOG_ROWS)
        _bounded_limit("query_page_size", self.query_page_size,
                       HARD_MAX_PAGE_SIZE)
        _bounded_limit("max_nodes", self.max_nodes, HARD_MAX_NODES)
        _bounded_limit("max_edges", self.max_edges, HARD_MAX_EDGES)
        _bounded_limit("memory_budget_mb", self.memory_budget_mb,
                       HARD_MAX_MEMORY_MB)
        _bounded_limit("write_batch_size", self.write_batch_size, 25_000)


def _bounded_limit(name: str, value: int, hard_max: int) -> None:
    if value < 1 or value > hard_max:
        raise ProcessGraphError(
            f"process_graph.{name} must be between 1 and {hard_max:,}; "
            f"received {value:,}")

# Query-time caps. The walk is bounded by all three: whichever binds first.
MAX_HOPS = 3
MAX_VISITED = 400
MAX_SEEDS = 12
RESULT_CAP = 40
# A node reached only through a chain of weak edges is not part of the
# process; it is somewhere the graph can eventually get to. Without a floor,
# "how do we do invoicing" returns Asset Management — reachable, irrelevant,
# and indistinguishable from a real answer to anyone reading the list.
MIN_RELEVANCE = 0.25
# And within a layer, anything far below the best match is noise even if it
# clears the floor: five modules where one is the answer.
LAYER_DROPOFF = 0.45

# How much a hop is worth. This IS the ranking, so it is a table rather than
# scattered constants: an edge read from the instance's own metadata outranks
# one inferred from a shared column name, and both outrank a document that
# merely mentions a table.
EDGE_WEIGHTS = {
    "nav_reaches_component": 0.95,   # PSPRSMDEFN — the breadcrumb a user reads
    "component_has_page": 0.95,      # PSPNLGROUP
    "page_reads_record": 0.90,       # PSPNLFIELD — the page/record link
    "record_has_field": 0.30,
    "record_in_module": 0.55,        # curated record map
    "tool_reads_record": 0.80,       # our own curated tools
    # Shared indexed columns — EVIDENCE, and deliberately weak enough that
    # TWO join hops fall below MIN_RELEVANCE. PeopleSoft records nearly all
    # share BUSINESS_UNIT and SETID, so these edges form a near-complete
    # graph: at 0.6 a three-hop walk out of Billing reached Asset Management
    # and ranked it alongside the invoice tables. One hop is a real
    # neighbour; two is just the schema being connected.
    "record_joins_record": 0.45,
    "doc_describes_record": 0.70,    # a written procedure naming the record
    "doc_describes_module": 0.55,
    "query_reads_record": 0.50,      # a saved PSQuery someone actually built
    "fact_describes_record": 0.75,   # an approved site-memory fact
    "scope_covers_record": 0.85,     # facet (country/BU/SETID) -> record
    "scope_covers_unit": 0.95,
}

# Node kinds, ordered the way a person reads a process: where you go, what
# you open, what it writes, what governs it, what explains it.
KIND_ORDER = ("navigation", "component", "page", "record", "field",
              "setup", "module", "tool", "query", "doc", "fact", "scope")

# Reached, reported, never travelled THROUGH. These kinds describe records
# rather than participate in a flow, which makes each of them a hub: the
# month-end checklist names a dozen records, so walking out of it carried
# "how do we do invoicing" to Asset Management in two hops and scored it
# like a real neighbour. A module, a tool, a saved query and a taught fact
# are hubs in the same way. Being a SEED is different — asking about the
# close checklist by name should absolutely expand from it — so this applies
# only to nodes the walk arrived at.
TERMINAL_KINDS = frozenset({"doc", "tool", "module", "query", "fact"})

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")

# Nobody asks "how do we do billing" when the delivered module is Billing and
# their own word is invoicing. Two mechanisms close that gap, in this order:
#
#   1. STEMS. "invoicing" and "invoice" and "invoices" all reduce to invoic,
#      matched as a prefix. This is doing most of the work, because the system
#      already describes itself in the user's nouns — PSRECDEFN says "Bill
#      Header", a page is called "Invoice Summary", a portal label reads
#      "Billing > Maintain Bills". Those descriptions are free vocabulary and
#      a stem reaches them without anyone curating a thesaurus.
#   2. MODULE VOCABULARY, for the gaps a stem cannot cross: "who owes us" is
#      receivables and no amount of suffix-stripping gets there. Deliberately
#      small and only on module nodes, because a long synonym list is a second
#      thing to keep true.
_SUFFIXES = ("ings", "ing", "ions", "ion", "ings", "ses", "es", "ers", "er",
             "ed", "als", "s")
MODULE_VOCABULARY = {
    "general_ledger": "gl ledger journal posting close trial balance chartfield",
    "billing": "invoicing invoice bill billed charge raise sales revenue",
    # Surface forms are spelled out where a stem cannot reach them: "owes"
    # is four letters, so nothing can be stripped without destroying it, and
    # "who owes us" is how people actually ask about receivables.
    "receivables": "ar collection dunning owe owes owed owing debtor customer "
                   "aging chase chasing overdue cash application",
    "payables": "ap voucher supplier vendor payment pay paying paid creditor "
                "spend disbursement",
    "asset_management": "am fixed asset depreciation capitalization",
    "commitment_control": "kk budget check encumbrance pre-encumbrance",
    "projects_expenses": "project expense report timesheet",
    "chartfields_setup": "setup configuration reference coa chart of accounts",
}


def _stem(word: str) -> str:
    """A crude, deliberate stem: enough to bridge invoicing/invoice.

    Not a linguistics exercise. It only has to make one FTS prefix out of the
    forms of a business noun, and the minimum-length floor is what stops
    "billed" collapsing to something that matches half the catalog.
    """
    w = (word or "").lower()
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[:-len(suf)]
            break
    # And the silent -e, which is why this is not just suffix stripping:
    # "invoicing" loses "ing" and becomes invoic, while "invoice" keeps its
    # e. Prefix-matching invoic* finds invoice, but the reverse — a user who
    # types the noun against an index full of the gerund — does not. Drop it
    # so both spellings land on the same root whichever side they start on.
    return w[:-1] if len(w) >= 6 and w.endswith("e") else w


_STOP = frozenset("""
a an the how do we does did is are was were what which who whom whose when
where why our their its for from with without into onto about across and or
but not all any some this that these those there here to in on of at by as
you your i me my it be been being have has had will would can could should
process processes step steps work works working use used using set setup
configure configured show tell explain me please system
""".split())


class ProcessGraphError(RuntimeError):
    """A build or a read that cannot honestly continue."""


def _norm(name: str) -> str:
    return (name or "").strip().upper()


def _record_variants(name: str) -> set:
    """PS_BI_HDR and BI_HDR are the same record wearing two hats.

    PeopleTools names records without the PS_ prefix (PSRECDEFN.RECNAME =
    BI_HDR); SQL uses the table (PS_BI_HDR). Matching one form against the
    other is the difference between a page layer that connects to the data
    layer and two disconnected islands in the same graph.
    """
    n = _norm(name)
    if not n:
        return set()
    return {n, n[3:] if n.startswith("PS_") else f"PS_{n}"}


def node_id(kind: str, name: str) -> str:
    return f"{kind}:{_norm(name)}"


class Harvest:
    """What one source contributed, and what it could not read.

    A harvester that finds nothing and a harvester that was refused look
    identical in the finished graph. They are not the same, and only the
    build report can tell them apart, so every harvester returns its own
    notes rather than raising.
    """

    def __init__(self, source: str):
        self.source = source
        self.nodes: dict = {}
        self.edges: dict = {}
        self.notes: list = []
        self.ok = True
        self.partial = False
        self.limit_hits: list = []

    def node(self, kind: str, name: str, label: str = "", module: str = "",
             **attrs) -> str:
        nid = node_id(kind, name)
        cur = self.nodes.get(nid)
        if cur is None:
            self.nodes[nid] = {"id": nid, "kind": kind, "name": _norm(name),
                               "label": label, "module": module,
                               "source": self.source, "attrs": attrs}
        else:
            # First writer wins on identity; later ones may only fill blanks,
            # so a field harvester cannot overwrite a record's real
            # description with an empty string.
            if label and not cur["label"]:
                cur["label"] = label
            if module and not cur["module"]:
                cur["module"] = module
            cur["attrs"].update({k: v for k, v in attrs.items() if v})
        return nid

    def edge(self, src: str, dst: str, kind: str, evidence: str = "",
             weight: float = 0.0) -> None:
        if not src or not dst or src == dst:
            return
        key = (src, dst, kind)
        w = weight or EDGE_WEIGHTS.get(kind, 0.4)
        cur = self.edges.get(key)
        if cur is None or w > cur["weight"]:
            self.edges[key] = {"src": src, "dst": dst, "kind": kind,
                               "weight": w, "evidence": evidence,
                               "source": self.source}

    def note(self, text: str, ok: bool = True) -> None:
        self.notes.append(text)
        if not ok:
            self.ok = False

    def limit(self, table: str, cap: int, rows_kept: int) -> None:
        """Record a deliberate partial harvest as degraded, not healthy."""
        hit = {"table": table, "limit": int(cap), "rows_kept": int(rows_kept)}
        self.limit_hits.append(hit)
        self.partial = True
        self.note(
            f"{table} reached the configured {cap:,}-row limit; {rows_kept:,} "
            "rows were kept and this source is PARTIAL. Raise the matching "
            "process_graph limit and rebuild if later catalog entries "
            "are required.", ok=False)


# --------------------------------------------------------------- harvesters
def _probe(db, table: str) -> set:
    """Columns of a PeopleTools table, or an empty set if unreadable.

    db.columns() already caches both outcomes, so probing every optional
    table costs one describe each on the first build and nothing after.
    """
    try:
        return {c.upper() for c in db.columns(table)}
    except Exception:                                   # noqa: BLE001
        return set()


def harvest_peopletools(db, limits: GraphBuildLimits | None = None) -> Harvest:
    """The layer nothing else in this app reads: pages, components, menus.

    PSRECDEFN and PSRECFIELD were already used for record search. The rest of
    the chain — PSPNLDEFN (pages), PSPNLFIELD (which records a page touches),
    PSPNLGROUP (which pages a component contains), PSPRSMDEFN (where the
    component sits in the navigation a user actually clicks) — was not, and
    it is exactly the "how do I DO this" half of the question.
    """
    limits = limits or GraphBuildLimits()
    limits.validate()
    h = Harvest("peopletools")
    p = getattr(db, "prefix", "")

    def rows(table: str, cols: str, order: str, cap: int,
             where: str = "") -> list:
        """Read an ordered catalog with portable keyset pagination.

        ``Database.query`` intentionally returns at most ``max_rows`` and a
        truncation flag. Passing the installation-wide cap there made that
        safety feature an accidental one-shot graph limit. Keyset pages keep
        each query and result allocation small without OFFSET's increasingly
        expensive rescans on large PeopleTools catalogs.
        """
        keys = [part.strip().split()[0] for part in order.split(",")]
        out = []
        after = None
        while len(out) < cap:
            clauses = [f"({where})"] if where else []
            params = {}
            if after is not None:
                alternatives = []
                for i, key in enumerate(keys):
                    equal = [f"{keys[j]} = :pg{j}" for j in range(i)]
                    alternatives.append("(" + " AND ".join(
                        equal + [f"{key} > :pg{i}"]) + ")")
                clauses.append("(" + " OR ".join(alternatives) + ")")
                params = {f"pg{i}": value
                          for i, value in enumerate(after)}
            sql = f"SELECT {cols} FROM {p}{table}"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            page_size = min(limits.query_page_size, cap - len(out))
            try:
                page, truncated = db.query(
                    f"{sql} ORDER BY {order}", params, max_rows=page_size)
            except Exception as e:                      # noqa: BLE001
                if out:
                    h.partial = True
                    h.note(
                        f"{table} failed after {len(out):,} rows "
                        f"({type(e).__name__}); those rows were kept but "
                        "this layer is PARTIAL.", ok=False)
                else:
                    h.note(
                        f"{table} could not be read ({type(e).__name__}); "
                        "that layer is missing from the graph.", ok=False)
                return out
            if not page:
                break
            out.extend(page)
            if not truncated:
                break
            if len(out) >= cap:
                h.limit(table, cap, len(out))
                break
            next_after = tuple(page[-1].get(key.lower()) for key in keys)
            if any(value is None for value in next_after) or \
                    next_after == after:
                h.partial = True
                h.note(
                    f"{table} pagination stopped after {len(out):,} rows "
                    "because its ordering key was null or did not advance; "
                    "this layer is PARTIAL.", ok=False)
                break
            after = next_after
        return out

    # Records. RECTYPE tells a table from a view from a derived work record,
    # which is what separates "where the data lives" from "what the page uses
    # to show it" — a distinction a reader of the answer needs.
    rec_cols = _probe(db, "PSRECDEFN")
    rectypes = {0: "table", 1: "view", 2: "derived", 3: "subrecord",
                5: "dynamic view", 6: "query view", 7: "temp table"}
    if rec_cols:
        sel = ["RECNAME"]
        for opt in ("RECDESCR", "RECTYPE", "SQLTABLENAME"):
            if opt in rec_cols:
                sel.append(opt)
        for r in rows("PSRECDEFN", ", ".join(sel), "RECNAME",
                      limits.max_records):
            name = _norm(r.get("recname"))
            if not name:
                continue
            rt = r.get("rectype")
            h.node("record", name, label=str(r.get("recdescr") or ""),
                   rectype=rectypes.get(int(rt), str(rt)) if rt is not None
                   else "",
                   table=_norm(r.get("sqltablename")) or f"PS_{name}")
    else:
        h.note("PSRECDEFN is not readable; record descriptions and types are "
               "missing. The graph still has whatever the catalog and the "
               "record map contribute.", ok=False)

    # Pages, and the records they touch. PSPNLFIELD.RECNAME is the single
    # most valuable edge in this graph: it is the instance's own statement
    # that this screen reads this table.
    pnl_cols = _probe(db, "PSPNLDEFN")
    if pnl_cols:
        sel = [c for c in ("MARKET", "PNLNAME", "DESCR")
               if c in pnl_cols]
        pnl_order = "PNLNAME, MARKET" if "MARKET" in pnl_cols else "PNLNAME"
        for r in rows("PSPNLDEFN", ", ".join(sel), pnl_order,
                      limits.max_pages):
            if _norm(r.get("pnlname")):
                h.node("page", r["pnlname"], label=str(r.get("descr") or ""))
    else:
        h.note("PSPNLDEFN is not readable — this instance's pages are not in "
               "the graph, so answers name records but not the screens that "
               "maintain them.")

    fld_cols = _probe(db, "PSPNLFIELD")
    if fld_cols and "RECNAME" in fld_cols and "PNLNAME" in fld_cols:
        for r in rows("PSPNLFIELD", "DISTINCT PNLNAME, RECNAME",
                      "PNLNAME, RECNAME", limits.max_page_fields,
                      where="RECNAME IS NOT NULL AND RECNAME <> ' '"):
            page, rec = _norm(r.get("pnlname")), _norm(r.get("recname"))
            if not page or not rec:
                continue
            h.edge(h.node("page", page), h.node("record", rec),
                   "page_reads_record", evidence="PSPNLFIELD.RECNAME")
    elif pnl_cols:
        h.note("PSPNLFIELD has no readable PNLNAME/RECNAME pair, so pages "
               "are in the graph but not linked to their records.")

    # Components group pages; the portal registry puts components under the
    # navigation a person is actually told to follow.
    grp_cols = _probe(db, "PSPNLGROUP")
    if grp_cols and "PNLGRPNAME" in grp_cols and "PNLNAME" in grp_cols:
        grp_sel = ["PNLGRPNAME"]
        if "MARKET" in grp_cols:
            grp_sel.append("MARKET")
        grp_sel.append("PNLNAME")
        for r in rows("PSPNLGROUP", "DISTINCT " + ", ".join(grp_sel),
                      ", ".join(grp_sel), limits.max_components):
            comp, page = _norm(r.get("pnlgrpname")), _norm(r.get("pnlname"))
            if comp and page:
                h.edge(h.node("component", comp), h.node("page", page),
                       "component_has_page", evidence="PSPNLGROUP")
    prsm_cols = _probe(db, "PSPRSMDEFN")
    if prsm_cols and "PORTAL_OBJNAME" in prsm_cols:
        sel = (["PORTAL_NAME"] if "PORTAL_NAME" in prsm_cols else [])
        sel.append("PORTAL_OBJNAME")
        for opt in ("PORTAL_LABEL", "PORTAL_URI_SEG2", "PORTAL_PRNTOBJNAME"):
            if opt in prsm_cols:
                sel.append(opt)
        nav_order = ("PORTAL_NAME, PORTAL_OBJNAME"
                     if "PORTAL_NAME" in prsm_cols else "PORTAL_OBJNAME")
        for r in rows("PSPRSMDEFN", ", ".join(sel), nav_order,
                      limits.max_navigation):
            obj = _norm(r.get("portal_objname"))
            comp = _norm(r.get("portal_uri_seg2"))
            label = str(r.get("portal_label") or "")
            if not obj or not comp:
                continue
            nav = h.node("navigation", obj, label=label,
                         parent=_norm(r.get("portal_prntobjname")))
            h.edge(nav, h.node("component", comp), "nav_reaches_component",
                   evidence="PSPRSMDEFN.PORTAL_URI_SEG2")
    elif grp_cols:
        h.note("PSPRSMDEFN is not readable — components are in the graph but "
               "without the navigation path a user would be told to follow.")

    # Saved queries are a record of what people at this site actually pull.
    q_cols = _probe(db, "PSQRYRECORD")
    d_cols = _probe(db, "PSQRYDEFN")
    if q_cols and "QRYNAME" in q_cols and "RECNAME" in q_cols:
        descrs = {}
        if d_cols and "DESCR" in d_cols:
            qdef_sel = (["OPRID"] if "OPRID" in d_cols else [])
            qdef_sel += ["QRYNAME", "DESCR"]
            qdef_order = ("OPRID, QRYNAME" if "OPRID" in d_cols
                          else "QRYNAME")
            for r in rows("PSQRYDEFN", ", ".join(qdef_sel), qdef_order,
                          limits.max_queries):
                descrs[_norm(r.get("qryname"))] = str(r.get("descr") or "")
        qrec_sel = (["OPRID"] if "OPRID" in q_cols else [])
        qrec_sel += ["QRYNAME", "RECNAME"]
        for r in rows("PSQRYRECORD", "DISTINCT " + ", ".join(qrec_sel),
                      ", ".join(qrec_sel), limits.max_queries):
            qn, rec = _norm(r.get("qryname")), _norm(r.get("recname"))
            if qn and rec:
                h.edge(h.node("query", qn, label=descrs.get(qn, "")),
                       h.node("record", rec), "query_reads_record",
                       evidence="PSQRYRECORD")
    return h


def harvest_record_map(engine) -> Harvest:
    """The curated map: module membership, and which tool answers from what.

    This is the only source that knows a record belongs to "billing" as a
    business idea rather than as a name prefix, and the only one that can
    close the loop from a process back to a tool that can be CALLED.
    """
    h = Harvest("record_map")
    try:
        rmap = getattr(engine, "RECORD_MAP", {}) or {}
    except Exception as e:                              # noqa: BLE001
        h.note(f"the curated record map is unavailable ({e}).", ok=False)
        return h
    for module, records in rmap.items():
        mod = h.node("module", module, label=module.replace("_", " "))
        for entry in records:
            name = entry[0] if entry else ""
            kind = entry[1] if len(entry) > 1 else ""
            label = entry[2] if len(entry) > 2 else ""
            tools = entry[3] if len(entry) > 3 else ""
            if not name:
                continue
            rid = h.node("setup" if kind == "reference" else "record", name,
                         label=label, module=module, role=kind)
            h.edge(mod, rid, "record_in_module", evidence="curated record map")
            for tool in re.split(r"[\s/]+", tools or ""):
                tool = tool.strip()
                if tool:
                    h.edge(h.node("tool", tool, label="callable tool"), rid,
                           "tool_reads_record", evidence="curated record map")
    return h


def harvest_joins(engine, records) -> Harvest:
    """Record-to-record hops, reusing the data graph rather than re-deriving.

    These are the weakest structural edges in the graph and are labelled as
    such: shared column names are evidence a join EXISTS, never proof it
    means what a reader assumes.
    """
    h = Harvest("join_graph")
    try:
        from .graph import RecordGraph
        rg = RecordGraph(engine.db, seed_records=list(records))
        universe = rg.universe()
    except Exception as e:                              # noqa: BLE001
        h.note(f"join edges were not built ({type(e).__name__}: {e}).")
        return h
    if not universe:
        h.note("no record in the universe could be described, so the graph "
               "has no join edges. Check the read-only grants.")
        return h
    # O(n^2) over the BOUNDED universe, on column sets RecordGraph has
    # already cached — no extra catalog reads, and the universe is tens of
    # records, not the tens of thousands a real instance holds.
    for i, left in enumerate(universe):
        for right in universe[i + 1:]:
            try:
                hop = rg.hop(left, right)
            except Exception:                           # noqa: BLE001
                continue
            if hop is None or not hop.specific:
                continue
            how = ("both sides index it" if hop.indexed else
                   "indexable once the scope is pinned" if hop.indexable
                   else "neither side indexes it")
            # A hop that scans is still a real relationship; it is just a
            # bad way to travel. Keep it, rank it below the rest.
            h.edge(h.node("record", left), h.node("record", right),
                   "record_joins_record",
                   evidence=f"shared {', '.join(hop.on[:4])} — {how}",
                   weight=(EDGE_WEIGHTS["record_joins_record"]
                           * (1.0 if hop.indexed
                              else 0.8 if hop.indexable else 0.5)))
    return h


def harvest_wiki(wiki, records, modules=()) -> Harvest:
    """Written procedure, linked to the records and modules it names.

    A document that says PS_BI_HDR is describing billing whether or not
    anybody tagged it that way, and that mention is the cheapest reliable
    bridge there is between how a process is DESCRIBED and where it LIVES.

    Harvested by SEARCHING for each name rather than by listing every page,
    because search() is the only page-finding method both wiki providers
    have; Confluence has no cheap "give me everything". That costs one query
    per name at BUILD time and nothing afterwards, and it means a hit is
    already the evidence for the edge it creates.
    """
    h = Harvest("wiki")
    targets = [(r, "doc_describes_record", "record") for r in records]
    targets += [(m, "doc_describes_module", "module") for m in modules]
    if not targets:
        return h
    hit_any = False
    for name, edge_kind, node_kind in targets:
        # PS_BI_HDR is how SQL spells it; a procedure writer is as likely to
        # have typed BI_HDR. Search the bare form, which matches both.
        term = _norm(name)
        bare = term[3:] if term.startswith("PS_") else term
        try:
            hits = wiki.search(bare.replace("_", " "), limit=3) or []
        except Exception as e:                          # noqa: BLE001
            h.note(f"the wiki was not searchable ({type(e).__name__}); the "
                   "graph has no written procedure in it.")
            return h
        for hit in hits[:3]:
            title = str(hit.get("title") or "").strip()
            if not title:
                continue
            # A search RANKS; it does not promise the term is on the page.
            # Taking the top hits regardless linked all four sample documents
            # to nearly every record, and the doc layer then read the same
            # for every question — present, useless, and indistinguishable
            # from a real match. Require the words to actually appear.
            hay = (title + " " + str(hit.get("snippet") or "")).upper()
            if not any(w in hay for w in bare.split("_") if len(w) > 2):
                continue
            hit_any = True
            did = h.node("doc", title, label=title,
                         page_id=str(hit.get("id") or ""),
                         url=str(hit.get("url") or ""))
            h.edge(did, h.node(node_kind, name), edge_kind,
                   evidence=f"wiki search matched {bare}")
    if not hit_any:
        h.note("no wiki page mentions any known record or module, so the "
               "graph can point at tables but not at written procedure.")
    return h


def harvest_memory(memory) -> Harvest:
    """Approved site facts. What THIS organization has taught the app."""
    h = Harvest("site_memory")
    try:
        facts = memory.approved()
    except Exception as e:                              # noqa: BLE001
        h.note(f"site memory was not read ({type(e).__name__}).")
        return h
    for fact in facts:
        text = str(fact.get("text") or "").strip()
        if not text:
            continue
        fid = h.node("fact", (fact.get("id") or text)[:60], label=text[:200])
        for word in set(_WORD.findall(text.upper())):
            if word.startswith("PS_") or "_" in word:
                h.edge(fid, h.node("record", word), "fact_describes_record",
                       evidence="approved site fact")
    return h


def harvest_scopes(engine) -> Harvest:
    """What makes "for India" a question the graph can answer generically.

    A qualifier in a process question is almost always a SCOPE: a country, a
    business unit, a SETID, a currency. Rather than teach the graph about
    India, harvest the scope codes this installation actually has and let any
    of them narrow any process. The country layer only appears when
    PS_BUS_UNIT_TBL_FS carries COUNTRY here — many do not, and a graph that
    invented the mapping would be worse than one that says it has none.
    """
    h = Harvest("scopes")
    db = engine.db
    p = getattr(db, "prefix", "")

    # Countries first, by NAME. PeopleSoft stores COUNTRY as a three-letter
    # code — IND, USA, CAN — and nobody types IND. PS_COUNTRY_TBL is the
    # instance's own code-to-name table, which is why the mapping is read
    # rather than shipped: a hard-coded country list would be one more thing
    # to keep true, and would still be wrong for a site using custom codes.
    country_names = {}
    ctry_cols = _probe(db, "PS_COUNTRY_TBL")
    if ctry_cols and "DESCR" in ctry_cols:
        try:
            rows, _ = db.query(
                f"SELECT COUNTRY, DESCR FROM {p}PS_COUNTRY_TBL "
                "ORDER BY COUNTRY", {}, max_rows=500)
            country_names = {_norm(r.get("country")): str(r.get("descr") or "")
                             for r in rows if _norm(r.get("country"))}
        except Exception:                               # noqa: BLE001
            h.note("PS_COUNTRY_TBL was not read; a country can only be named "
                   "by its code (IND, USA), not by its name.")
    elif ctry_cols is not None and not ctry_cols:
        h.note("PS_COUNTRY_TBL is not present or not granted; name a country "
               "by its code (IND, USA) rather than by its name.")

    cols = _probe(db, "PS_BUS_UNIT_TBL_FS")
    if not cols:
        h.note("PS_BUS_UNIT_TBL_FS is not readable; the graph has no "
               "business-unit scope and cannot narrow a process by country.")
        return h
    sel = ["BUSINESS_UNIT"]
    for opt in ("DESCR", "COUNTRY"):
        if opt in cols:
            sel.append(opt)
    try:
        rows, _ = db.query(
            f"SELECT {', '.join(sel)} FROM {p}PS_BUS_UNIT_TBL_FS "
            "ORDER BY BUSINESS_UNIT", {}, max_rows=2_000)
    except Exception as e:                              # noqa: BLE001
        h.note(f"business units were not read ({type(e).__name__}).")
        return h
    has_country = "COUNTRY" in cols
    used_countries = set()
    for r in rows:
        bu = _norm(r.get("business_unit"))
        if not bu:
            continue
        unit = h.node("scope", f"BU:{bu}", label=str(r.get("descr") or bu),
                      facet="business_unit", value=bu)
        country = _norm(r.get("country")) if has_country else ""
        if country:
            used_countries.add(country)
            h.edge(h.node("scope", f"COUNTRY:{country}",
                          label=country_names.get(country, country),
                          facet="country", value=country),
                   unit, "scope_covers_unit",
                   evidence="PS_BUS_UNIT_TBL_FS.COUNTRY")
    # Every country this instance knows, not only the ones a unit sits in.
    # A country node with no units is what lets the answer say "India is a
    # country this system knows and no business unit here is in it" instead
    # of the far worse "no match, try different words".
    for code, name in list(country_names.items())[:500]:
        if code not in used_countries:
            h.node("scope", f"COUNTRY:{code}", label=name, facet="country",
                   value=code, no_units="yes")
    if not has_country:
        h.note("PS_BUS_UNIT_TBL_FS here has no COUNTRY column, so a country "
               "cannot be resolved to business units. Name the unit instead, "
               "or teach the mapping with remember_site_fact.")
    try:
        setids, _ = db.query(
            f"SELECT DISTINCT SETID FROM {p}PS_SET_CNTRL_REC ORDER BY SETID",
            {}, max_rows=500)
        for r in setids:
            sid = _norm(r.get("setid"))
            if sid:
                h.node("scope", f"SETID:{sid}", label=f"SETID {sid}",
                       facet="setid", value=sid)
    except Exception:                                   # noqa: BLE001
        h.note("PS_SET_CNTRL_REC was not read; SETID is not a usable facet.")
    return h


# -------------------------------------------------------------- persistence
_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE nodes (
  id      TEXT PRIMARY KEY,
  kind    TEXT NOT NULL,
  name    TEXT NOT NULL,
  label   TEXT,
  module  TEXT,
  source  TEXT NOT NULL,
  attrs   TEXT
);
CREATE INDEX nodes_kind ON nodes(kind);
CREATE INDEX nodes_name ON nodes(name);
CREATE TABLE edges (
  src      TEXT NOT NULL,
  dst      TEXT NOT NULL,
  kind     TEXT NOT NULL,
  weight   REAL NOT NULL,
  evidence TEXT,
  source   TEXT NOT NULL,
  PRIMARY KEY (src, dst, kind)
);
CREATE INDEX edges_src ON edges(src);
CREATE INDEX edges_dst ON edges(dst);
CREATE TABLE notes (source TEXT, note TEXT, ok INTEGER);
"""


def _searchable(node: dict) -> str:
    """The text a seed match runs against.

    Underscores are also emitted as spaces: nobody searching for "invoice
    header" types BI_HDR, and nobody typing BI_HDR wants it tokenized away.
    """
    module = node.get("module") or ""
    bits = [node["name"], node["name"].replace("_", " "),
            node.get("label") or "", module, module.replace("_", " ")]
    attrs = node.get("attrs") or {}
    if isinstance(attrs, dict):
        bits.extend(str(v) for v in attrs.values() if isinstance(v, str))
    # The one place curated vocabulary enters, and only for modules: it is
    # what carries "who owes us" to receivables when no stem can.
    if node["kind"] == "module":
        bits.append(MODULE_VOCABULARY.get(node["name"].lower(), ""))
    elif module:
        bits.append(MODULE_VOCABULARY.get(module.lower(), ""))
    return " ".join(b for b in bits if b)


def _estimated_working_bytes(nodes: dict, edges: dict, notes: list) -> int:
    """Conservative build-memory estimate without another full allocation."""
    total = 0
    for node in nodes.values():
        text = (node.get("id"), node.get("kind"), node.get("name"),
                node.get("label"), node.get("module"), node.get("source"))
        attrs = json.dumps(node.get("attrs") or {}, sort_keys=True)
        total += 480 + 4 * sum(len(str(v or "").encode("utf-8"))
                               for v in (*text, attrs))
    for edge in edges.values():
        text = (edge.get("src"), edge.get("dst"), edge.get("kind"),
                edge.get("evidence"), edge.get("source"))
        total += 400 + 4 * sum(len(str(v or "").encode("utf-8"))
                               for v in text)
    total += sum(160 + 4 * (len(str(source).encode("utf-8"))
                            + len(str(note).encode("utf-8")))
                 for source, note, _ in notes)
    return total


def _executemany_batched(con, sql: str, values, size: int) -> None:
    """Serialize only one bounded insert batch at a time."""
    iterator = iter(values)
    while True:
        batch = list(itertools.islice(iterator, size))
        if not batch:
            return
        con.executemany(sql, batch)


def write_graph(path, harvests, meta=None,
                limits: GraphBuildLimits | None = None) -> dict:
    """Write every harvest into one SQLite file, atomically.

    Atomic because the alternative is a half-written graph being read by a
    live GUI mid-rebuild: build beside the target, then rename over it.
    """
    limits = limits or GraphBuildLimits()
    limits.validate()
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".building")
    if tmp.exists():
        tmp.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    nodes: dict = {}
    edges: dict = {}
    notes: list = []
    for h in harvests:
        for nid, node in h.nodes.items():
            cur = nodes.get(nid)
            if cur is None:
                nodes[nid] = dict(node)
            else:
                if node.get("label") and not cur.get("label"):
                    cur["label"] = node["label"]
                if node.get("module") and not cur.get("module"):
                    cur["module"] = node["module"]
                cur["attrs"] = {**(node.get("attrs") or {}),
                                **(cur.get("attrs") or {})}
        for key, edge in h.edges.items():
            cur = edges.get(key)
            if cur is None or edge["weight"] > cur["weight"]:
                edges[key] = edge
        notes.extend((h.source, n, 1 if h.ok else 0) for n in h.notes)

    # An edge to a node no harvester declared would make the walk return an
    # id with no name behind it. Declare the stub rather than drop the edge:
    # a record named by a page and absent from PSRECDEFN is a real finding.
    for src, dst, _kind in list(edges):
        for nid in (src, dst):
            if nid not in nodes:
                kind, _, name = nid.partition(":")
                nodes[nid] = {"id": nid, "kind": kind, "name": name,
                              "label": "", "module": "", "source": "implied",
                              "attrs": {}}
    nodes, edges = _canonicalise(nodes, edges)
    if len(nodes) > limits.max_nodes:
        raise ProcessGraphError(
            f"Process graph would contain {len(nodes):,} nodes, exceeding "
            f"process_graph.max_nodes={limits.max_nodes:,}. The existing "
            "graph was not replaced. Raise that configured limit (up to "
            f"{HARD_MAX_NODES:,}) or reduce the selected sources.")
    if len(edges) > limits.max_edges:
        raise ProcessGraphError(
            f"Process graph would contain {len(edges):,} edges, exceeding "
            f"process_graph.max_edges={limits.max_edges:,}. The existing "
            "graph was not replaced. Raise that configured limit (up to "
            f"{HARD_MAX_EDGES:,}) or reduce the selected sources.")
    estimated_bytes = _estimated_working_bytes(nodes, edges, notes)
    budget_bytes = limits.memory_budget_mb * 1024 * 1024
    if estimated_bytes > budget_bytes:
        raise ProcessGraphError(
            f"Process graph build is estimated to need "
            f"{estimated_bytes / 1024 / 1024:.1f} MiB, exceeding "
            f"process_graph.memory_budget_mb={limits.memory_budget_mb:,}. "
            "The existing graph was not replaced. Raise the budget (up to "
            f"{HARD_MAX_MEMORY_MB:,} MiB) or reduce the selected sources.")

    con = None
    try:
        con = sqlite3.connect(str(tmp))
        # The target is a disposable scratch artifact until os.replace. Keep
        # SQLite's own transient memory bounded and avoid journaling a file
        # that no reader can see yet.
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA temp_store=FILE")
        con.executescript(_DDL)
        fts = _try_fts(con)
        _executemany_batched(
            con,
            "INSERT INTO nodes (id, kind, name, label, module, source, attrs)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ((n["id"], n["kind"], n["name"], n.get("label") or "",
              n.get("module") or "", n["source"],
              json.dumps(n.get("attrs") or {}, sort_keys=True))
             for n in nodes.values()), limits.write_batch_size)
        _executemany_batched(
            con,
            "INSERT INTO edges (src, dst, kind, weight, evidence, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ((e["src"], e["dst"], e["kind"], e["weight"],
              e.get("evidence") or "", e["source"])
             for e in edges.values()), limits.write_batch_size)
        _executemany_batched(
            con, "INSERT INTO notes (source, note, ok) VALUES (?,?,?)",
            iter(notes), limits.write_batch_size)
        if fts:
            _executemany_batched(
                con,
                "INSERT INTO node_fts (id, text) VALUES (?, ?)",
                ((n["id"], _searchable(n)) for n in nodes.values()),
                limits.write_batch_size)
        limit_hits = [dict(source=h.source, **hit)
                      for h in harvests for hit in h.limit_hits]
        info = {
            "schema_version": str(SCHEMA_VERSION),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "nodes": str(len(nodes)),
            "edges": str(len(edges)),
            "fts": "yes" if fts else "no",
            "sources": ",".join(sorted({h.source for h in harvests})),
            "degraded": ",".join(sorted({h.source for h in harvests
                                         if not h.ok})),
            "partial": "yes" if any(h.partial for h in harvests) else "no",
            "limit_hits": json.dumps(limit_hits, sort_keys=True),
            "estimated_build_mb": f"{estimated_bytes / 1024 / 1024:.1f}",
        }
        info.update({k: str(v) for k, v in (meta or {}).items()})
        con.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                        list(info.items()))
        con.commit()
        con.close()
        con = None
        os.replace(str(tmp), str(path))
    except Exception:
        if con is not None:
            con.close()
        if tmp.exists():
            tmp.unlink()
        raise
    return {**info, "path": str(path),
            "notes": [{"source": s, "note": n} for s, n, _ in notes]}


_RECORD_KINDS = ("setup", "record")


def _canonicalise(nodes: dict, edges: dict) -> tuple:
    """Merge BI_HDR into PS_BI_HDR — one record, not two islands.

    PeopleTools names a record BI_HDR; SQL, the curated map and every tool in
    this app call the same thing PS_BI_HDR. Left alone, the page layer
    attaches to one node and the data layer to the other, and the graph
    quietly reports that invoicing pages touch no records anybody queries —
    a hole shaped exactly like a working answer.

    Done here, once, rather than in each harvester: they arrive at record
    names from four different directions and any one of them forgetting
    re-opens the split. `setup` wins over `record` when both exist, because
    the curated map is the only source that knows a table is REFERENCE data.
    """
    kind_of: dict = {}
    for node in nodes.values():
        if node["kind"] in _RECORD_KINDS:
            bare = node["name"][3:] if node["name"].startswith("PS_") \
                else node["name"]
            cur = kind_of.get(bare)
            if cur is None or (cur == "record" and node["kind"] == "setup"):
                kind_of[bare] = node["kind"]

    def canon(nid: str) -> str:
        kind, _, name = nid.partition(":")
        if kind not in _RECORD_KINDS:
            return nid
        bare = name[3:] if name.startswith("PS_") else name
        return f"{kind_of.get(bare, kind)}:PS_{bare}"

    merged: dict = {}
    for nid, node in nodes.items():
        cid = canon(nid)
        cur = merged.get(cid)
        if cur is None:
            merged[cid] = {**node, "id": cid,
                           "name": cid.split(":", 1)[1],
                           "kind": cid.split(":", 1)[0]}
            continue
        # Keep the richer description; a PeopleTools RECDESCR and a curated
        # label say different useful things, so prefer the longer one and
        # never lose a module assignment.
        if len(node.get("label") or "") > len(cur.get("label") or ""):
            cur["label"] = node["label"]
        if node.get("module") and not cur.get("module"):
            cur["module"] = node["module"]
        cur["attrs"] = {**(node.get("attrs") or {}), **(cur.get("attrs") or {})}
        if cur["source"] != node["source"]:
            cur["source"] = "+".join(sorted({cur["source"], node["source"]}))

    out: dict = {}
    for (src, dst, kind), edge in edges.items():
        a, b = canon(src), canon(dst)
        if a == b:
            continue                     # a record joining itself post-merge
        key = (a, b, kind)
        cur = out.get(key)
        if cur is None or edge["weight"] > cur["weight"]:
            out[key] = {**edge, "src": a, "dst": b}
    return merged, out


def _try_fts(con) -> bool:
    """FTS5 when the interpreter's SQLite has it, LIKE when it does not.

    Python is shipped without FTS5 often enough that requiring it would make
    the whole feature unavailable on some machines for a seed-matching
    convenience. The fallback is slower and blunter, and describe() says
    which one is in force rather than leaving a reader guessing why a search
    behaves differently on two boxes.
    """
    try:
        con.execute("CREATE VIRTUAL TABLE node_fts USING fts5"
                    "(id UNINDEXED, text)")
        return True
    except sqlite3.OperationalError:
        return False


# ------------------------------------------------------------------ reading
def graph_path(cfg) -> Path:
    """Beside the config, like every other per-deployment artifact."""
    root = Path(getattr(cfg, "root", ".") or ".")
    return root / DEFAULT_FILENAME


class ProcessGraph:
    """Read-only reader. Opens per call; the file is small and local."""

    def __init__(self, path):
        self.path = Path(path)

    def available(self) -> bool:
        return self.path.exists()

    def _open(self):
        if not self.path.exists():
            raise ProcessGraphError(
                f"No process graph at {self.path.name}. Build it with: "
                "python scripts/build_process_graph.py")
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    # ------------------------------------------------------------- describe
    def describe(self) -> dict:
        if not self.available():
            return {"available": False,
                    "detail": f"No process graph at {self.path.name}.",
                    "how_to_build": "python scripts/build_process_graph.py"}
        con = self._open()
        try:
            meta = {r["key"]: r["value"] for r in
                    con.execute("SELECT key, value FROM meta")}
            kinds = [{"kind": r["kind"], "nodes": r["n"]} for r in con.execute(
                "SELECT kind, COUNT(*) AS n FROM nodes GROUP BY kind "
                "ORDER BY n DESC")]
            edges = [{"edge": r["kind"], "count": r["n"]} for r in con.execute(
                "SELECT kind, COUNT(*) AS n FROM edges GROUP BY kind "
                "ORDER BY n DESC")]
            notes = [{"source": r["source"], "note": r["note"]}
                     for r in con.execute(
                         "SELECT source, note FROM notes ORDER BY source")]
        finally:
            con.close()
        degraded = [s for s in (meta.get("degraded") or "").split(",") if s]
        partial = meta.get("partial") == "yes"
        try:
            limit_hits = json.loads(meta.get("limit_hits") or "[]")
        except (TypeError, ValueError):
            limit_hits = []
        return {
            "available": True, "path": self.path.name,
            "built_at": meta.get("built_at", ""),
            "schema_version": meta.get("schema_version", ""),
            "sources": [s for s in (meta.get("sources") or "").split(",") if s],
            "sources_degraded": degraded,
            "partial": partial,
            "limit_hits": limit_hits,
            "estimated_build_mb": meta.get("estimated_build_mb", ""),
            "seed_search": ("full text" if meta.get("fts") == "yes"
                            else "substring (FTS5 unavailable here)"),
            "node_kinds": kinds, "edge_kinds": edges,
            "build_notes": notes,
            "coverage_note": (
                "This graph is a snapshot of structure taken at build time. "
                "It answers what connects to what, never what anything is "
                "worth — every amount still comes from a financial tool."
                + (" PARTIAL: one or more catalog limits were reached; "
                   "limit_hits identifies the retained rows and configured "
                   "ceiling." if partial else "")
                + (f" Degraded sources: {', '.join(degraded)}."
                   if degraded else "")),
        }

    # ---------------------------------------------------------------- seeds
    def _seeds(self, con, terms, kinds=(), limit=MAX_SEEDS) -> list:
        if not terms:
            return []
        has_fts = bool(list(con.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'node_fts'")))
        found: dict = {}
        stems = list(dict.fromkeys(_stem(t) for t in terms))
        if has_fts:
            # Prefix on the STEM, so "invoicing" reaches "Invoice Summary".
            expr = " OR ".join(f'"{s}"*' for s in stems)
            try:
                for r in con.execute(
                        "SELECT n.id, n.kind, n.name, n.label, n.module, "
                        "  bm25(node_fts) AS rank "
                        "FROM node_fts JOIN nodes n ON n.id = node_fts.id "
                        "WHERE node_fts MATCH ? ORDER BY rank LIMIT ?",
                        (expr, limit * 4)):
                    found[r["id"]] = dict(r)
            except sqlite3.OperationalError:
                has_fts = False
        if not has_fts:
            for stem in stems:
                like = f"%{stem}%"
                for r in con.execute(
                        "SELECT id, kind, name, label, module FROM nodes "
                        "WHERE name LIKE ? OR label LIKE ? OR module LIKE ? "
                        "LIMIT ?", (like, like, like, limit * 2)):
                    found.setdefault(r["id"], dict(r))
        rows = [r for r in found.values() if r["kind"] != "scope"]
        if kinds:
            rows = [r for r in rows if r["kind"] in kinds] or rows
        # A node whose NAME matches is a better seed than one that merely
        # mentions the word in a description.
        upper = [s.upper() for s in stems]
        rows.sort(key=lambda r: (
            0 if any(t in r["name"] for t in upper) else 1,
            -sum(1 for t in upper if t in _norm(r.get("label") or "")),
            len(r["name"])))
        return rows[:limit]

    # ----------------------------------------------------------------- walk
    def _walk(self, con, seeds, hops, scope_ids, kind_of=None) -> dict:
        """Weighted breadth-first, best score wins, bounded three ways.

        Scores multiply along the path, so a chain of strong metadata edges
        outranks one strong edge followed by two guesses — which is the
        ordering a reader needs, because the guesses are where the answer
        stops being a fact about this instance.
        """
        best = {s["id"]: (1.0, 0, None, "") for s in seeds}
        for sid in scope_ids:
            best.setdefault(sid, (1.0, 0, None, "scope"))
        frontier = list(best)
        for depth in range(hops):
            if not frontier or len(best) >= MAX_VISITED:
                break
            marks = ",".join("?" * len(frontier))
            rows = con.execute(
                f"SELECT src, dst, kind, weight, evidence FROM edges "
                f"WHERE src IN ({marks}) OR dst IN ({marks})",
                frontier + frontier).fetchall()
            nxt = []
            for r in rows:
                for a, b in ((r["src"], r["dst"]), (r["dst"], r["src"])):
                    if a not in best:
                        continue
                    score = best[a][0] * r["weight"]
                    prior = best.get(b)
                    if prior is None or score > prior[0]:
                        if prior is None and len(best) >= MAX_VISITED:
                            continue
                        best[b] = (score, depth + 1, a, r["kind"])
                        if b.partition(":")[0] not in TERMINAL_KINDS:
                            nxt.append(b)
            frontier = list(dict.fromkeys(nxt))
        return best

    def trace(self, question: str, hops: int = MAX_HOPS,
              kinds=(), limit: int = RESULT_CAP) -> dict:
        """The whole point: a question in, a process laid out in order."""
        if not self.available():
            return {"available": False, "question": question,
                    "detail": f"No process graph at {self.path.name} — "
                              "nothing has been indexed yet.",
                    "how_to_build": "python scripts/build_process_graph.py"}
        terms = [w for w in
                 (m.group(0).lower() for m in _WORD.finditer(question or ""))
                 if w not in _STOP and len(w) > 2]
        if not terms:
            return {"available": True, "question": question, "seeds": [],
                    "detail": "Nothing in that question names a process, a "
                              "record, a page or a scope to start from."}
        con = self._open()
        try:
            hops = max(1, min(int(hops or MAX_HOPS), MAX_HOPS))
            scopes = self._scope_hits(con, terms)
            scope_block = [self._scope_entry(con, s) for s in scopes]
            seeds = self._seeds(con, terms,
                                kinds=tuple(kinds) if kinds else ())
            if not seeds:
                # A scope with no process is "invoicing for India" minus the
                # invoicing: say which half was understood rather than
                # answering the half that was.
                return {"available": True, "question": question, "seeds": [],
                        "terms": terms, "scope_applied": scope_block,
                        "detail": (
                            "No page, record, module or document in this "
                            "instance matches those words."
                            + (" The scope was understood; the process was "
                               "not — name what is being done (invoicing, "
                               "payments, journals) as well as where."
                               if scope_block else
                               " Try a record name, a module, or a menu "
                               "label.")),
                        "known_modules": self._modules(con)}
            # Scopes NARROW an answer; they are not a place to walk from. A
            # business unit touches every record in the system, so walking
            # out of one returns the whole graph and calls it relevant.
            best = self._walk(con, seeds, hops, [])
            ids = list(best)
            rows = {}
            for chunk in (ids[i:i + 400] for i in range(0, len(ids), 400)):
                marks = ",".join("?" * len(chunk))
                for r in con.execute(
                        f"SELECT id, kind, name, label, module, source, attrs "
                        f"FROM nodes WHERE id IN ({marks})", chunk):
                    rows[r["id"]] = dict(r)
            layers = self._layer(rows, best, limit)
            return {
                "available": True,
                "question": question,
                "terms": terms,
                "seeds": [{"id": s["id"], "kind": s["kind"],
                           "name": s["name"], "label": s["label"]}
                          for s in seeds],
                "scope_applied": scope_block,
                "layers": layers,
                "how_to_read": HOW_TO_READ,
                "basis": ("Structure read from this instance's own metadata, "
                          "catalog, record map and documents at graph build "
                          "time. It holds no amounts — call a financial tool "
                          "for any figure."),
            }
        finally:
            con.close()

    # ----------------------------------------------------------- assemblers
    def _scope_hits(self, con, terms) -> list:
        """A qualifier in the question — a country, a unit, a SETID.

        Ranked by how much of the scope's LABEL the question covers, and cut
        below full coverage when full coverage exists: "invoicing for the
        United States" says United States, and offering United Kingdom
        beside it because both contain "united" turned one resolved scope
        into a resolved one plus a spurious refusal.
        """
        upper = {t.upper() for t in terms}
        hits = []
        for term in terms:
            like = f"%{term.upper()}%"
            for r in con.execute(
                    "SELECT id, kind, name, label, attrs FROM nodes "
                    "WHERE kind = 'scope' AND (name LIKE ? OR "
                    "UPPER(label) LIKE ?) LIMIT 20", (like, like)):
                words = [w for w in _WORD.findall(
                    (r["label"] or r["name"]).upper()) if w not in _STOP]
                cover = (sum(1 for w in words if w in upper) / len(words)
                         if words else 0.0)
                hits.append((cover, dict(r)))
        best: dict = {}
        for cover, row in hits:
            cur = best.get(row["id"])
            if cur is None or cover > cur[0]:
                best[row["id"]] = (cover, row)
        ranked = sorted(best.values(), key=lambda x: -x[0])
        if any(c >= 1.0 for c, _ in ranked):
            ranked = [(c, r) for c, r in ranked if c >= 1.0]
        return [r for _, r in ranked[:8]]

    def _units_under(self, con, scope_id) -> list:
        return [r["name"].split(":", 1)[-1] for r in con.execute(
            "SELECT n.name FROM edges e JOIN nodes n ON n.id = e.dst "
            "WHERE e.src = ? AND e.kind = 'scope_covers_unit' "
            "ORDER BY n.name LIMIT 50", (scope_id,))]

    def _scope_entry(self, con, scope) -> dict:
        """What a qualifier resolved to, and what to DO with it.

        The whole value of understanding "for India" is the business units it
        names, because those are what a financial tool takes as an argument.
        A scope that resolves to nothing is reported as such — an answer
        scoped to a country this installation does not operate in would be
        the whole company's process wearing a label that makes it look local.
        """
        facet = _facet_of(scope)
        value = (scope.get("name") or "").split(":", 1)[-1]
        if facet == "country":
            units = self._units_under(con, scope["id"])
        elif facet in ("bu", "business_unit"):
            units = [value]
        else:
            units = []
        entry = {"facet": facet, "value": value,
                 "name": scope.get("label") or value,
                 "business_units": units}
        if facet == "country" and not units:
            entry["note"] = (
                f"No business unit here records its country as {value}. The "
                "process below is this installation's, NOT a local variant — "
                "say so rather than presenting it as the local one.")
        elif units:
            entry["next_step"] = (
                "Pass business_unit=" + units[0] + " to the financial tools "
                "for figures in this scope"
                + (f" (also {', '.join(units[1:6])})" if len(units) > 1
                   else "") + ".")
        return entry

    def _modules(self, con) -> list:
        return [r["name"] for r in con.execute(
            "SELECT name FROM nodes WHERE kind = 'module' ORDER BY name")]

    def _layer(self, rows, best, limit) -> list:
        """Group by kind in reading order, each group ranked by score.

        A flat ranked list of forty things is a search result. A process is
        the same forty things in the order someone would meet them, which is
        what makes the answer usable without a second question.
        """
        buckets: dict = {}
        for nid, (score, depth, via, ekind) in best.items():
            node = rows.get(nid)
            if not node or score < MIN_RELEVANCE:
                continue
            attrs = {}
            try:
                attrs = json.loads(node.get("attrs") or "{}")
            except ValueError:
                pass
            buckets.setdefault(node["kind"], []).append({
                "name": node["name"], "label": node["label"] or "",
                "module": node["module"] or "",
                "role": attrs.get("role") or attrs.get("rectype") or "",
                "hops_away": depth,
                "reached_by": ekind or "seed",
                "relevance": round(score, 3),
                "source": node["source"],
            })
        def finish(kind, items):
            items.sort(key=lambda x: (-x["relevance"], x["name"]))
            cut = items[0]["relevance"] * LAYER_DROPOFF
            kept = [i for i in items if i["relevance"] >= cut][:limit]
            entry = {"layer": kind, "meaning": LAYER_MEANING.get(kind, ""),
                     "items": kept}
            dropped = len(items) - len(kept)
            if dropped:
                entry["also_reachable"] = dropped
                entry["note"] = (
                    "further nodes were reachable but scored too far below "
                    "these to be part of the same process; raise limit or "
                    "ask about them by name")
            return entry

        out = []
        for kind in KIND_ORDER:
            items = buckets.pop(kind, [])
            if items:
                out.append(finish(kind, items))
        for kind, items in sorted(buckets.items()):       # unknown kinds last
            out.append(finish(kind, items))
        return out


def _facet_of(scope_row) -> str:
    name = scope_row.get("name") or ""
    return name.split(":", 1)[0].lower() if ":" in name else "scope"


LAYER_MEANING = {
    "navigation": "where a user goes in the menu",
    "component": "the component those pages belong to",
    "page": "the screens that maintain this",
    "record": "the tables the work is written to",
    "setup": "reference and configuration tables that govern it",
    "module": "which part of the system owns it",
    "tool": "tools here that can answer from these records",
    "query": "saved queries at this site that read them",
    "doc": "written procedure that describes it",
    "fact": "what this organization has taught the app",
    "field": "columns reached along the way",
    "scope": "the business units and setids this was narrowed to",
}

HOW_TO_READ = (
    "Layers are ordered the way a person meets a process: navigation, then "
    "the pages, then the records those pages write, then what governs and "
    "explains them. 'relevance' is the product of the edge weights along the "
    "path — a page linked by PSPNLFIELD scores above a record linked only by "
    "a shared column name, because the first is a fact about this instance "
    "and the second is evidence. 'reached_by' names the edge, so a weak link "
    "is visible rather than averaged away."
)
