"""Who deals with whom: customers, products, suppliers, business units.

THE GAP. Three graphs already exist here and none of them holds an ACTOR.
pstb/graph.py knows PS_ITEM reaches PS_CUSTOMER; pstb/procgraph.py knows
which page maintains it; pstb/relationships.py knows everything about ONE
customer. Nothing knows that ACME and Northwind both buy LIC-SAAS, that one
product carries half of a unit's billing, or that a customer is also a
supplier. Those are questions about the shape of the business, and they are
answered by the edges between actors rather than by any record.

WHAT AN ACTOR IS. A customer, a supplier, a product, a business unit — the
things a person names in a question. What a FLOW is: a real transaction
population between two of them, aggregated. customer -buys-> product comes
from finalized bill lines; customer -billed_by-> business unit from invoice
headers; supplier -billed_to-> business unit from vouchers. Every flow
carries its transaction count, its window, and — kept apart from all of
that — a derived amount.

    THE AMOUNT IS DERIVED AND DATED, AND IT IS NOT THE LEDGER.
    Oracle remains the record. Amounts here are a WEIGHT: they exist so
    "biggest" and "share of" can be computed without ten round trips, and
    every payload that quotes one stamps it as_of the build and names the
    live tool that confirms it. The graph narrows; the ledger answers.

ROW SECURITY IS THE REASON THIS IS HARD, and it is why the filter lives on
the FLOW rather than the actor. A customer is not "in" a business unit —
customers are SETID-keyed and a person granted one unit may legitimately
see a customer that also trades in three others. What they may not see is
the trade. So each flow records the unit it happened in, queries keep only
flows in granted units, and an actor is visible exactly when it has one.
The alternative — tagging actors with units and filtering those — either
hides customers people are entitled to see or leaks the units they are not.

A precomputed cross-unit index is the easiest place in this codebase to
leak, so it reads its caller the same way the live tools do, through
pstb.security.current_access, which the model cannot write to. Every query
below funnels through _visible_units(). There is no path that skips it.

NO IDENTITY GUESSING. Two actors are the same only where the system says
so: PS_CUSTOMER.CORPORATE_CUST_ID and the supplier equivalent. Nothing here
matches on name similarity, address or anything else — a consolidation
nobody approved looks exactly as authoritative as a correct one.

NO PERSONAL DATA. Names of companies and products, ids, unit codes, counts,
dates and derived totals. No bank accounts, no tax identifiers, no
addresses, no contacts. The vendor network hashes identity keys precisely
so they never reach a payload, and nothing here undoes that.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from . import queries as q
from .procgraph import Harvest, node_id
from .security import allowed_units

SCHEMA_VERSION = 1
DEFAULT_FILENAME = "entity_graph.db"

# Build caps. A real instance has hundreds of thousands of customers; this
# is an index for answering questions, not a copy of the master.
ACTOR_CAP = 20_000
FLOW_CAP = 200_000
# Query caps.
RESULT_CAP = 40
MAX_HOPS = 3
MAX_VISITED = 600

ACTOR_KINDS = ("customer", "supplier", "product", "business_unit")

# What each flow means in words, because "buys" and "billed_by" are only
# obvious to whoever wrote them.
FLOW_MEANING = {
    "buys": "finalized bill lines: this customer was invoiced for this "
            "product",
    "billed_by": "finalized invoices raised for this customer by this "
                 "business unit",
    "sold_by": "finalized bill lines for this product in this business unit",
    "owes": "open receivable items for this customer in this business unit",
    "billed_to": "supplier vouchers entered in this business unit",
    "subsidiary_of": "the corporate hierarchy the system records — not a "
                     "name match",
}
# Hierarchy is a fact about master data, not a transaction, so it has no
# business unit of its own and is shown only between two actors already
# visible through their own trade.
STRUCTURAL_FLOWS = frozenset({"subsidiary_of"})

# Which flow is RANKED for each actor kind, and which flow its PARTNERS are
# counted from. They are deliberately different. Ranking a customer by
# billed_by measures money, and counting partners on that same flow measures
# how many business units billed them — which is 1 for almost everybody and
# says nothing. The concentration question is "how many products does this
# customer buy" and "how many customers does this product have", and a
# product with exactly one customer is the finding.
RANK_FLOW = {"customer": "billed_by", "product": "sold_by",
             "supplier": "billed_to", "business_unit": "billed_by"}
PARTNER_FLOW = {"customer": "buys", "product": "buys",
                "supplier": "billed_to", "business_unit": "billed_by"}
PARTNER_MEANING = {"customer": "distinct products bought",
                   "product": "distinct customers who bought it",
                   "supplier": "distinct business units billed",
                   "business_unit": "distinct customers billed"}


class EntityGraphError(RuntimeError):
    """A build or a read that cannot honestly continue."""


def graph_path(cfg) -> Path:
    root = Path(getattr(cfg, "root", ".") or ".")
    return root / DEFAULT_FILENAME


def _s(v) -> str:
    return str(v or "").strip()


def _u(v) -> str:
    return _s(v).upper()


# ------------------------------------------------------------- harvesting
def harvest_entities(engine, months: int = 12, as_of_date: str = "") -> Harvest:
    """One pass over the transaction records, producing actors and flows.

    Aggregated in SQL, never row by row: the point of an offline build is to
    do the GROUP BY once so that question time is a local index read. Every
    read is optional and degrades with a note, because a site missing
    PS_BI_LINE has no product dimension and must be told that rather than
    shown an empty one.
    """
    from .ar import _iso, _months_before

    h = Harvest("peoplesoft")
    db = engine.db
    p = getattr(db, "prefix", "")
    asof = _s(as_of_date) or time.strftime("%Y-%m-%d")
    try:
        since = _months_before(_iso(asof), max(int(months or 12), 1)).isoformat()
    except Exception:                                   # noqa: BLE001
        since = asof
    h.window = {"months": months, "since": since, "as_of": asof}

    def cols(table: str) -> set:
        try:
            return {c.upper() for c in db.columns(table)}
        except Exception:                               # noqa: BLE001
            return set()

    def flow(src, dst, kind, bu, currency, txns, amount, first, last):
        h.edge(src, dst, kind, evidence=FLOW_MEANING.get(kind, ""))
        key = (src, dst, kind)
        e = h.edges[key]
        slot = e.setdefault("slices", {})
        k = (_u(bu), _u(currency))
        cur = slot.setdefault(k, {"business_unit": _u(bu),
                                  "currency": _u(currency),
                                  "transactions": 0, "amount": 0.0,
                                  "first_seen": "", "last_seen": ""})
        cur["transactions"] += int(txns or 0)
        cur["amount"] += float(amount or 0)
        f, l = _s(first), _s(last)
        if f and (not cur["first_seen"] or f < cur["first_seen"]):
            cur["first_seen"] = f
        if l and l > cur["last_seen"]:
            cur["last_seen"] = l

    # ---- business units: every actor's stage --------------------------
    units: dict = {}
    try:
        rows, _ = db.query(
            f"SELECT BUSINESS_UNIT AS bu, DESCR AS descr "
            f"FROM {p}PS_BUS_UNIT_TBL_FS ORDER BY BUSINESS_UNIT",
            {}, max_rows=5_000)
        for r in rows:
            bu = _u(r["bu"])
            if bu:
                units[bu] = _s(r.get("descr"))
                h.node("business_unit", bu, label=_s(r.get("descr")) or bu)
    except Exception as e:                              # noqa: BLE001
        h.note(f"business units were not read ({type(e).__name__}); flows "
               "will still carry their unit code.")

    # ---- customer names, once -----------------------------------------
    cust_names: dict = {}
    c_cols = cols("PS_CUSTOMER")
    if c_cols:
        name_c = "NAME1" if "NAME1" in c_cols else ""
        corp_c = "CORPORATE_CUST_ID" if "CORPORATE_CUST_ID" in c_cols else ""
        sel = ["CUST_ID"] + [c for c in (name_c, corp_c) if c]
        try:
            rows, _ = db.query(
                f"SELECT DISTINCT {', '.join(sel)} FROM {p}PS_CUSTOMER",
                {}, max_rows=ACTOR_CAP)
            for r in rows:
                cid = _u(r["cust_id"])
                if not cid:
                    continue
                cust_names[cid] = _s(r.get("name1")) if name_c else ""
                parent = _u(r.get("corporate_cust_id")) if corp_c else ""
                if parent and parent != cid:
                    h.edge(node_id("customer", cid),
                           node_id("customer", parent), "subsidiary_of",
                           evidence="PS_CUSTOMER.CORPORATE_CUST_ID")
        except Exception as e:                          # noqa: BLE001
            h.note(f"PS_CUSTOMER was not read ({type(e).__name__}); "
                   "customers appear by id without names.")
        if not corp_c:
            h.note("PS_CUSTOMER here has no CORPORATE_CUST_ID, so no "
                   "customer hierarchy is recorded and none is inferred.")

    # ---- customer <-> business unit, from finalized invoices -----------
    bi = cols("PS_BI_HDR")
    if bi and {"BILL_TO_CUST_ID", "INVOICE_DT"} <= bi:
        cur_sel = "H.BI_CURRENCY_CD" if "BI_CURRENCY_CD" in bi else "''"
        amt_sel = "SUM(H.INVOICE_AMOUNT)" if "INVOICE_AMOUNT" in bi else "0"
        try:
            rows, _ = db.query(
                f"SELECT H.BUSINESS_UNIT AS bu, H.BILL_TO_CUST_ID AS cid, "
                f"{cur_sel} AS currency, COUNT(*) AS n, {amt_sel} AS amt, "
                f"MIN(H.INVOICE_DT) AS first_dt, MAX(H.INVOICE_DT) AS last_dt "
                f"FROM {p}PS_BI_HDR H WHERE H.BILL_STATUS = 'INV' "
                f"AND H.INVOICE_DT >= {db.date_bind('since')} "
                f"AND H.INVOICE_DT <= {db.date_bind('asof')} "
                f"GROUP BY H.BUSINESS_UNIT, H.BILL_TO_CUST_ID, {cur_sel}",
                {"since": since, "asof": asof}, max_rows=FLOW_CAP)
            for r in rows:
                cid, bu = _u(r["cid"]), _u(r["bu"])
                if not cid or not bu:
                    continue
                src = h.node("customer", cid, label=cust_names.get(cid, ""))
                dst = h.node("business_unit", bu, label=units.get(bu, bu))
                flow(src, dst, "billed_by", bu, r.get("currency"),
                     r.get("n"), r.get("amt"), r.get("first_dt"),
                     r.get("last_dt"))
        except Exception as e:                          # noqa: BLE001
            h.note(f"billing headers were not read ({type(e).__name__}); "
                   "the customer-to-unit flows are missing.", ok=False)
    else:
        h.note("PS_BI_HDR is not readable here — no billing flows.",
               ok=False)

    # ---- customer <-> product, and product <-> unit -------------------
    bl = cols("PS_BI_LINE")
    if bl and "IDENTIFIER" in bl and bi:
        amt_sel = ("SUM(L.NET_EXTENDED_AMT)"
                   if "NET_EXTENDED_AMT" in bl else "0")
        cur_sel = "H.BI_CURRENCY_CD" if "BI_CURRENCY_CD" in bi else "''"
        desc_sel = "MAX(L.DESCR)" if "DESCR" in bl else "''"
        try:
            rows, _ = db.query(
                f"SELECT H.BUSINESS_UNIT AS bu, H.BILL_TO_CUST_ID AS cid, "
                f"L.IDENTIFIER AS product, {desc_sel} AS descr, "
                f"{cur_sel} AS currency, COUNT(*) AS n, {amt_sel} AS amt, "
                f"MIN(H.INVOICE_DT) AS first_dt, MAX(H.INVOICE_DT) AS last_dt "
                f"FROM {p}PS_BI_LINE L "
                f"JOIN {p}PS_BI_HDR H ON H.BUSINESS_UNIT = L.BUSINESS_UNIT "
                f"AND H.INVOICE = L.INVOICE "
                f"WHERE H.BILL_STATUS = 'INV' "
                f"AND H.INVOICE_DT >= {db.date_bind('since')} "
                f"AND H.INVOICE_DT <= {db.date_bind('asof')} "
                f"AND {q.nonblank('L.IDENTIFIER')} "
                f"GROUP BY H.BUSINESS_UNIT, H.BILL_TO_CUST_ID, "
                f"L.IDENTIFIER, {cur_sel}",
                {"since": since, "asof": asof}, max_rows=FLOW_CAP)
            for r in rows:
                cid, bu = _u(r["cid"]), _u(r["bu"])
                prod = _u(r["product"])
                if not cid or not prod:
                    continue
                pnode = h.node("product", prod, label=_s(r.get("descr")))
                cnode = h.node("customer", cid, label=cust_names.get(cid, ""))
                flow(cnode, pnode, "buys", bu, r.get("currency"),
                     r.get("n"), r.get("amt"), r.get("first_dt"),
                     r.get("last_dt"))
                if bu:
                    flow(pnode, h.node("business_unit", bu,
                                       label=units.get(bu, bu)),
                         "sold_by", bu, r.get("currency"), r.get("n"),
                         r.get("amt"), r.get("first_dt"), r.get("last_dt"))
        except Exception as e:                          # noqa: BLE001
            h.note(f"bill lines were not read ({type(e).__name__}); there "
                   "is no product dimension in this graph.")
    else:
        h.note("PS_BI_LINE has no IDENTIFIER at this site, so products are "
               "not a dimension here — customer and unit flows still are.")

    # ---- customer -owes-> unit, from OPEN items -----------------------
    it = cols("PS_ITEM")
    if it and {"CUST_ID", "BAL_AMT"} <= it:
        cur_sel = "I.BAL_CURRENCY" if "BAL_CURRENCY" in it else "''"
        date_c = ("ACCTG_DT" if "ACCTG_DT" in it
                  else ("ASOF_DT" if "ASOF_DT" in it else ""))
        dsel = f"MIN(I.{date_c})" if date_c else "''"
        lsel = f"MAX(I.{date_c})" if date_c else "''"
        try:
            rows, _ = db.query(
                f"SELECT I.BUSINESS_UNIT AS bu, I.CUST_ID AS cid, "
                f"{cur_sel} AS currency, COUNT(*) AS n, "
                f"SUM(I.BAL_AMT) AS amt, {dsel} AS first_dt, "
                f"{lsel} AS last_dt FROM {p}PS_ITEM I "
                f"WHERE I.ITEM_STATUS = 'O' "
                f"GROUP BY I.BUSINESS_UNIT, I.CUST_ID, {cur_sel}",
                {}, max_rows=FLOW_CAP)
            for r in rows:
                cid, bu = _u(r["cid"]), _u(r["bu"])
                if not cid or not bu:
                    continue
                flow(h.node("customer", cid, label=cust_names.get(cid, "")),
                     h.node("business_unit", bu, label=units.get(bu, bu)),
                     "owes", bu, r.get("currency"), r.get("n"),
                     r.get("amt"), r.get("first_dt"), r.get("last_dt"))
        except Exception as e:                          # noqa: BLE001
            h.note(f"open items were not read ({type(e).__name__}).")

    # ---- supplier -billed_to-> unit, and supplier hierarchy -----------
    v_cols = cols("PS_VENDOR")
    vend_names: dict = {}
    if v_cols:
        name_c = "NAME1" if "NAME1" in v_cols else ""
        corp_c = ("CORPORATE_VENDOR" if "CORPORATE_VENDOR" in v_cols else "")
        sel = ["VENDOR_ID"] + [c for c in (name_c, corp_c) if c]
        try:
            rows, _ = db.query(
                f"SELECT DISTINCT {', '.join(sel)} FROM {p}PS_VENDOR",
                {}, max_rows=ACTOR_CAP)
            for r in rows:
                vid = _u(r["vendor_id"])
                if not vid:
                    continue
                vend_names[vid] = _s(r.get("name1")) if name_c else ""
                parent = _u(r.get("corporate_vendor")) if corp_c else ""
                if parent and parent != vid:
                    h.edge(node_id("supplier", vid),
                           node_id("supplier", parent), "subsidiary_of",
                           evidence="PS_VENDOR.CORPORATE_VENDOR")
        except Exception as e:                          # noqa: BLE001
            h.note(f"PS_VENDOR was not read ({type(e).__name__}).")
    vch = cols("PS_VOUCHER")
    if vch and {"VENDOR_ID", "GROSS_AMT"} <= vch:
        cur_sel = "V.CURRENCY_CD" if "CURRENCY_CD" in vch else "''"
        has_dt = "INVOICE_DT" in vch
        dsel = "MIN(V.INVOICE_DT)" if has_dt else "''"
        lsel = "MAX(V.INVOICE_DT)" if has_dt else "''"
        where = (f"WHERE V.INVOICE_DT >= {db.date_bind('since')} "
                 f"AND V.INVOICE_DT <= {db.date_bind('asof')}"
                 if has_dt else "")
        try:
            rows, _ = db.query(
                f"SELECT V.BUSINESS_UNIT AS bu, V.VENDOR_ID AS vid, "
                f"{cur_sel} AS currency, COUNT(*) AS n, "
                f"SUM(V.GROSS_AMT) AS amt, {dsel} AS first_dt, "
                f"{lsel} AS last_dt FROM {p}PS_VOUCHER V {where} "
                f"GROUP BY V.BUSINESS_UNIT, V.VENDOR_ID, {cur_sel}",
                {"since": since, "asof": asof}, max_rows=FLOW_CAP)
            for r in rows:
                vid, bu = _u(r["vid"]), _u(r["bu"])
                if not vid or not bu:
                    continue
                flow(h.node("supplier", vid, label=vend_names.get(vid, "")),
                     h.node("business_unit", bu, label=units.get(bu, bu)),
                     "billed_to", bu, r.get("currency"), r.get("n"),
                     r.get("amt"), r.get("first_dt"), r.get("last_dt"))
        except Exception as e:                          # noqa: BLE001
            h.note(f"vouchers were not read ({type(e).__name__}); there are "
                   "no supplier flows.")
    return h


# ------------------------------------------------------------ persistence
_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE actors (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
  label TEXT, attrs TEXT
);
CREATE INDEX actors_kind ON actors(kind);
CREATE INDEX actors_name ON actors(name);
-- One row per (pair, kind, business unit, currency). The business unit is
-- on the FLOW because that is what row security governs: an actor is not
-- "in" a unit, its trade is. Currencies are kept apart for the same reason
-- they are everywhere else here — adding euros to dollars is not a total.
CREATE TABLE flows (
  src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL,
  business_unit TEXT NOT NULL, currency TEXT NOT NULL,
  transactions INTEGER NOT NULL, amount REAL NOT NULL,
  first_seen TEXT, last_seen TEXT, evidence TEXT,
  PRIMARY KEY (src, dst, kind, business_unit, currency)
);
CREATE INDEX flows_src ON flows(src, business_unit);
CREATE INDEX flows_dst ON flows(dst, business_unit);
CREATE INDEX flows_bu ON flows(business_unit);
CREATE TABLE notes (note TEXT, ok INTEGER);
"""


def write_graph(path, harvest: Harvest, meta=None) -> dict:
    """Write one harvest atomically. Built beside the target, then renamed,
    so a rebuild never leaves a half-written file for a live server."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".building")
    if tmp.exists():
        tmp.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    flow_rows = []
    for (src, dst, kind), e in harvest.edges.items():
        slices = e.get("slices")
        if not slices:
            # A structural edge — hierarchy — with no transaction behind it.
            flow_rows.append((src, dst, kind, "", "", 0, 0.0, "", "",
                              e.get("evidence") or ""))
            continue
        for s in slices.values():
            flow_rows.append((src, dst, kind, s["business_unit"],
                              s["currency"], s["transactions"],
                              round(float(s["amount"]), 2),
                              s["first_seen"], s["last_seen"],
                              e.get("evidence") or ""))

    con = sqlite3.connect(str(tmp))
    try:
        con.executescript(_DDL)
        con.executemany(
            "INSERT INTO actors (id, kind, name, label, attrs) "
            "VALUES (?, ?, ?, ?, ?)",
            [(n["id"], n["kind"], n["name"], n.get("label") or "",
              json.dumps(n.get("attrs") or {}, sort_keys=True))
             for n in harvest.nodes.values()])
        con.executemany(
            "INSERT INTO flows (src, dst, kind, business_unit, currency, "
            "transactions, amount, first_seen, last_seen, evidence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", flow_rows)
        con.executemany("INSERT INTO notes (note, ok) VALUES (?, ?)",
                        [(n, 1 if harvest.ok else 0) for n in harvest.notes])
        window = getattr(harvest, "window", {}) or {}
        info = {
            "schema_version": str(SCHEMA_VERSION),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "actors": str(len(harvest.nodes)),
            "flows": str(len(flow_rows)),
            "degraded": "" if harvest.ok else harvest.source,
            "window_months": str(window.get("months", "")),
            "window_start": str(window.get("since", "")),
            "as_of": str(window.get("as_of", "")),
        }
        info.update({k: str(v) for k, v in (meta or {}).items()})
        con.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                        list(info.items()))
        con.commit()
    finally:
        con.close()
    os.replace(str(tmp), str(path))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return {**info, "path": str(path), "notes": list(harvest.notes)}


# ---------------------------------------------------------------- reading
class EntityGraph:
    """Read-only. Every query narrows to the caller's granted units first."""

    def __init__(self, path):
        self.path = Path(path)

    def available(self) -> bool:
        return self.path.exists()

    def _open(self):
        if not self.available():
            raise EntityGraphError(
                f"No entity graph at {self.path.name}. Build it with: "
                "python scripts/build_entity_graph.py")
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def _unbuilt(self, **extra) -> dict:
        return {"available": False,
                "detail": f"No entity graph at {self.path.name} — nothing "
                          "has been indexed yet.",
                "how_to_build": "python scripts/build_entity_graph.py",
                **extra}

    # ---- THE gate. Every query passes through here, none may skip it ----
    def _visible_units(self, con, business_unit: str = "") -> tuple:
        """(units, withheld) this caller may read flows for.

        Read from the bound caller, which the model cannot write to. An
        explicit business_unit narrows further but can never widen: asking
        for a unit you were not granted returns nothing, not everything.
        """
        every = [r["business_unit"] for r in con.execute(
            "SELECT DISTINCT business_unit FROM flows "
            "WHERE business_unit <> ''")]
        granted, withheld = allowed_units(every)
        wanted = _u(business_unit)
        if wanted and wanted not in {"ALL", "*"}:
            granted = [u for u in granted if u == wanted]
        return granted, withheld

    def _flow_filter(self, units, alias: str = "f") -> tuple:
        if not units:
            return f"{alias}.business_unit = :__never", {"__never": "\x00"}
        binds = {f"vu{i}": u for i, u in enumerate(units)}
        expr = ", ".join(f":{k}" for k in binds)
        return f"{alias}.business_unit IN ({expr})", binds

    def _actors(self, con, ids) -> dict:
        out: dict = {}
        ids = list(ids)
        for chunk in (ids[i:i + 400] for i in range(0, len(ids), 400)):
            marks = ",".join("?" * len(chunk))
            for r in con.execute(
                    f"SELECT id, kind, name, label FROM actors "
                    f"WHERE id IN ({marks})", chunk):
                out[r["id"]] = dict(r)
        return out

    # ------------------------------------------------------------ describe
    def describe(self) -> dict:
        if not self.available():
            return self._unbuilt()
        con = self._open()
        try:
            meta = {r["key"]: r["value"] for r in
                    con.execute("SELECT key, value FROM meta")}
            units, withheld = self._visible_units(con)
            where, binds = self._flow_filter(units)
            kinds = [{"kind": r["kind"], "actors": r["n"]} for r in
                     con.execute("SELECT kind, COUNT(*) AS n FROM actors "
                                 "GROUP BY kind ORDER BY n DESC")]
            flows = [{"flow": r["kind"], "meaning": FLOW_MEANING.get(
                          r["kind"], ""), "rows": r["n"]}
                     for r in con.execute(
                         f"SELECT kind, COUNT(*) AS n FROM flows f "
                         f"WHERE {where} OR business_unit = '' "
                         f"GROUP BY kind ORDER BY n DESC", binds)]
            notes = [r["note"] for r in con.execute("SELECT note FROM notes")]
        finally:
            con.close()
        out = {
            "available": True, "path": self.path.name,
            "built_at": meta.get("built_at", ""),
            "as_of": meta.get("as_of", ""),
            "window_months": meta.get("window_months", ""),
            "window_start": meta.get("window_start", ""),
            "actor_kinds": kinds, "flow_kinds": flows,
            "business_units_visible": units,
            "build_notes": notes,
            "amounts_note": (
                "Amounts here are a DERIVED WEIGHT taken at build time, not "
                "the ledger. They exist so 'biggest' and 'share of' can be "
                "computed without a round trip per actor. Quote them as of "
                f"{meta.get('as_of', 'the build')} and confirm any figure "
                "that matters with the live tool named in next_steps."),
        }
        if withheld:
            out["units_withheld"] = len(withheld)
            out["restricted_to_granted_units"] = True
        return out

    # ------------------------------------------------- one actor's network
    def neighbourhood(self, entity: str = "", kind: str = "",
                      business_unit: str = "", limit: int = RESULT_CAP
                      ) -> dict:
        """Everything one actor trades with, grouped by what the link means."""
        term = _s(entity)
        if not term:
            return {"scope_status": "entity_required",
                    "detail": "Name a customer, supplier, product or "
                              "business unit — an id or a name."}
        if not self.available():
            return self._unbuilt(entity=term)
        con = self._open()
        try:
            units, withheld = self._visible_units(con, business_unit)
            if not units:
                return self._no_units(business_unit, withheld)
            actor = self._resolve(con, term, kind, units)
            # A resolved actor and a refusal are both dicts; only the
            # refusal carries scope_status. Testing the TYPE here returned
            # the actor as the whole answer and dropped every link.
            if "scope_status" in actor:
                return actor
            where, binds = self._flow_filter(units)
            rows = list(con.execute(
                f"SELECT f.src, f.dst, f.kind, f.business_unit, f.currency, "
                f"f.transactions, f.amount, f.first_seen, f.last_seen "
                f"FROM flows f WHERE (f.src = :me OR f.dst = :me) "
                f"AND ({where})", {"me": actor["id"], **binds}))
            # Hierarchy carries no unit; show it only between actors this
            # caller can already see through their own trade.
            struct = list(con.execute(
                "SELECT src, dst, kind, evidence FROM flows "
                "WHERE (src = :me OR dst = :me) AND business_unit = ''",
                {"me": actor["id"]}))
            ids = {actor["id"]}
            for r in rows:
                ids.add(r["src"])
                ids.add(r["dst"])
            visible = self._visible_actors(con, units)
            for r in struct:
                other = r["dst"] if r["src"] == actor["id"] else r["src"]
                if other in visible:
                    ids.add(other)
            names = self._actors(con, ids)
            groups: dict = {}
            for r in rows:
                other_id = r["dst"] if r["src"] == actor["id"] else r["src"]
                other = names.get(other_id) or {}
                direction = "out" if r["src"] == actor["id"] else "in"
                g = groups.setdefault(r["kind"], {})
                cur = g.setdefault(other_id, {
                    "id": other_id, "kind": other.get("kind", ""),
                    "name": other.get("name", ""),
                    "label": other.get("label", ""),
                    "direction": direction,
                    "transactions": 0, "by_currency": {},
                    "business_units": set(), "last_seen": ""})
                cur["transactions"] += int(r["transactions"] or 0)
                c = _u(r["currency"]) or "?"
                cur["by_currency"][c] = round(
                    cur["by_currency"].get(c, 0.0) + float(r["amount"] or 0), 2)
                cur["business_units"].add(r["business_unit"])
                if _s(r["last_seen"]) > cur["last_seen"]:
                    cur["last_seen"] = _s(r["last_seen"])
            links = []
            for fkind, members in sorted(groups.items()):
                items = sorted(members.values(),
                               key=lambda x: -max(
                                   list(x["by_currency"].values()) or [0]))
                links.append({
                    "flow": fkind, "meaning": FLOW_MEANING.get(fkind, ""),
                    "count": len(items),
                    "items": [{**m,
                               "business_units": sorted(m["business_units"]),
                               "amounts": [{"currency": c, "amount": a}
                                           for c, a in
                                           sorted(m["by_currency"].items())],
                               "by_currency": None}
                              for m in items[:limit]],
                })
            for group in links:
                for item in group["items"]:
                    item.pop("by_currency", None)
            family = [{"id": (r["dst"] if r["src"] == actor["id"]
                              else r["src"]),
                       "relation": ("parent" if r["src"] == actor["id"]
                                    else "subsidiary"),
                       "name": (names.get(r["dst"] if r["src"] == actor["id"]
                                          else r["src"], {}) or {})
                       .get("label", ""),
                       "evidence": r["evidence"]}
                      for r in struct
                      if (r["dst"] if r["src"] == actor["id"]
                          else r["src"]) in ids]
            return self._stamp(con, {
                "actor": {k: actor[k] for k in
                          ("id", "kind", "name", "label")},
                "links": links,
                "corporate_family": family,
                "business_units_covered": units,
                "next_steps": self._next_steps(actor),
            }, units, withheld)
        finally:
            con.close()

    # ------------------------------------------------------- concentration
    def concentration(self, kind: str = "customer", by: str = "",
                      business_unit: str = "", limit: int = 10) -> dict:
        """Who carries the weight, and what depends on a single actor.

        The question behind "which customers matter" and "what happens if we
        lose this product" — computed as SHARES, because a share survives
        being a day out of date in a way an amount does not.
        """
        if not self.available():
            return self._unbuilt()
        kind = _s(kind).lower() or "customer"
        if kind not in ACTOR_KINDS:
            return {"scope_status": "unknown_actor_kind",
                    "detail": f"{kind!r} is not an actor kind here.",
                    "actor_kinds": list(ACTOR_KINDS)}
        flow_kind = _s(by).lower() or RANK_FLOW[kind]
        con = self._open()
        try:
            units, withheld = self._visible_units(con, business_unit)
            if not units:
                return self._no_units(business_unit, withheld)
            where, binds = self._flow_filter(units)
            rows = list(con.execute(
                f"SELECT a.id, a.name, a.label, f.currency, "
                f"SUM(f.amount) AS amount, SUM(f.transactions) AS txns, "
                f"MAX(f.last_seen) AS last_seen, "
                f"COUNT(DISTINCT f.business_unit) AS units "
                f"FROM flows f JOIN actors a "
                f"  ON a.id = f.src OR a.id = f.dst "
                f"WHERE a.kind = :kind AND f.kind = :flow AND ({where}) "
                f"GROUP BY a.id, a.name, a.label, f.currency",
                {"kind": kind, "flow": flow_kind, **binds}))
            # Partners come from a DIFFERENT flow than the ranking one; see
            # PARTNER_FLOW. Counted in its own pass rather than as another
            # aggregate on the query above, where the join to the ranking
            # flow would have already collapsed them.
            pflow = PARTNER_FLOW[kind]
            partners = {r["id"]: int(r["n"] or 0) for r in con.execute(
                f"SELECT a.id AS id, COUNT(DISTINCT CASE WHEN a.id = f.src "
                f"  THEN f.dst ELSE f.src END) AS n "
                f"FROM flows f JOIN actors a ON a.id = f.src OR a.id = f.dst "
                f"WHERE a.kind = :kind AND f.kind = :pflow AND ({where}) "
                f"GROUP BY a.id",
                {"kind": kind, "pflow": pflow, **binds})}
            if not rows:
                return self._stamp(con, {
                    "kind": kind, "measured_by": flow_kind,
                    "detail": f"No {flow_kind} flows for any {kind} in the "
                              "units this caller can see. This is NO DATA, "
                              "not a zero.",
                    "ranked": [],
                }, units, withheld)
            by_currency: dict = {}
            for r in rows:
                by_currency.setdefault(_u(r["currency"]) or "?", []).append(r)
            blocks = []
            for currency, group in sorted(by_currency.items()):
                total = sum(float(r["amount"] or 0) for r in group)
                group.sort(key=lambda r: -float(r["amount"] or 0))
                ranked = []
                running = 0.0
                for r in group[:limit]:
                    amount = float(r["amount"] or 0)
                    running += amount
                    ranked.append({
                        "id": r["id"], "name": r["name"],
                        "label": r["label"],
                        "amount": round(amount, 2),
                        "share_pct": (round(amount / total * 100, 1)
                                      if total else None),
                        "transactions": int(r["txns"] or 0),
                        "partners": partners.get(r["id"], 0),
                        "business_units": int(r["units"] or 0),
                        "last_seen": _s(r["last_seen"]),
                    })
                blocks.append({
                    "currency": currency,
                    "population": len(group),
                    "total": round(total, 2),
                    "sum_only": True,
                    "top_n_share_pct": (round(running / total * 100, 1)
                                        if total else None),
                    "ranked": ranked,
                    # Exactly one partner is a dependency, not a statistic:
                    # a product with one customer disappears with them.
                    "single_partner": [
                        x for x in ranked if x["partners"] == 1],
                })
            return self._stamp(con, {
                "kind": kind, "measured_by": flow_kind,
                "meaning": FLOW_MEANING.get(flow_kind, ""),
                "partners_are": PARTNER_MEANING.get(kind, ""),
                "by_currency": blocks,
                "share_basis": (
                    "Shares are of the population VISIBLE to this caller in "
                    "the units listed, not of the company. A share is the "
                    "durable figure here; the amount behind it is as of the "
                    "build."),
                "next_steps": [
                    "get_top_billing_customers(business_unit=…) confirms any "
                    "billing figure against the live ledger."],
            }, units, withheld)
        finally:
            con.close()

    # --------------------------------------------------------- connections
    def connection(self, source: str = "", target: str = "",
                   business_unit: str = "", hops: int = MAX_HOPS) -> dict:
        """How two actors are connected, with the evidence on every hop.

        The question no single record answers: "is this supplier anything to
        do with that customer?" A path through a shared business unit is a
        weak connection and a path through a recorded hierarchy is a strong
        one, so each hop names which it is rather than being averaged into
        a score.
        """
        if not self.available():
            return self._unbuilt()
        if not _s(source) or not _s(target):
            return {"scope_status": "two_entities_required",
                    "detail": "Name two actors to connect."}
        con = self._open()
        try:
            units, withheld = self._visible_units(con, business_unit)
            if not units:
                return self._no_units(business_unit, withheld)
            a = self._resolve(con, source, "", units)
            if "scope_status" in a:
                return a
            b = self._resolve(con, target, "", units)
            if "scope_status" in b:
                return b
            if a["id"] == b["id"]:
                return {"scope_status": "same_entity",
                        "detail": "Those are the same actor."}
            where, binds = self._flow_filter(units)
            hops = max(1, min(int(hops or MAX_HOPS), MAX_HOPS))
            prev: dict = {a["id"]: None}
            frontier = [a["id"]]
            found = False
            for _ in range(hops):
                if not frontier or found:
                    break
                # All-positional: this statement mixes a variable-length
                # frontier with the unit filter, and the two binding styles
                # cannot be combined in one sqlite statement.
                marks = ",".join("?" * len(frontier))
                unit_marks = ",".join("?" * len(units))
                rows = con.execute(
                    f"SELECT src, dst, kind, business_unit, evidence "
                    f"FROM flows WHERE (src IN ({marks}) "
                    f"OR dst IN ({marks})) "
                    f"AND (business_unit IN ({unit_marks}) "
                    f"     OR business_unit = '')",
                    frontier + frontier + list(units)).fetchall()
                nxt = []
                for r in rows:
                    for x, y in ((r["src"], r["dst"]), (r["dst"], r["src"])):
                        if x in prev and y not in prev:
                            if len(prev) >= MAX_VISITED:
                                continue
                            # Carry the STORED direction, not the direction
                            # the walk happened to cross it in. The walk is
                            # undirected; subsidiary_of is not, and reading
                            # a hop backwards turns "West is a subsidiary of
                            # ACME" into the opposite claim.
                            prev[y] = (x, r["kind"], r["business_unit"],
                                       r["evidence"], r["src"], r["dst"])
                            nxt.append(y)
                            if y == b["id"]:
                                found = True
                frontier = nxt
            if b["id"] not in prev:
                return self._stamp(con, {
                    "from": a["name"], "to": b["name"], "connected": False,
                    "detail": (f"No connection within {hops} hops through "
                               "the units this caller can see. That is not "
                               "proof they are unconnected — it is proof "
                               "this graph has no path."),
                }, units, withheld)
            chain = []
            node = b["id"]
            while prev.get(node):
                src, kind, bu, evidence, e_src, e_dst = prev[node]
                chain.append({"from": src, "to": node, "flow": kind,
                              "business_unit": bu,
                              "meaning": FLOW_MEANING.get(kind, ""),
                              "strength": ("recorded hierarchy"
                                           if kind in STRUCTURAL_FLOWS
                                           else "shared transactions"),
                              "stated_from": e_src, "stated_to": e_dst,
                              "traversed_backwards": e_src != src,
                              "evidence": evidence})
                node = src
            chain.reverse()
            names = self._actors(con, {h["from"] for h in chain}
                                 | {h["to"] for h in chain})
            def label_of(nid: str) -> str:
                a = names.get(nid, {}) or {}
                return a.get("label") or a.get("name") or nid

            for hop in chain:
                hop["from_name"] = label_of(hop["from"])
                hop["to_name"] = label_of(hop["to"])
                # The sentence the reader should believe, always in the
                # direction the SYSTEM records it.
                hop["reads"] = (f"{label_of(hop['stated_from'])} "
                                f"{hop['flow'].replace('_', ' ')} "
                                f"{label_of(hop['stated_to'])}")
            return self._stamp(con, {
                "from": a["name"], "to": b["name"], "connected": True,
                "hops": len(chain), "path": chain,
                "caution": (
                    "A path is a CONNECTION, never a conclusion. Two actors "
                    "sharing a business unit share it with everyone else in "
                    "that unit; only a hop marked 'recorded hierarchy' says "
                    "the system considers them related."),
            }, units, withheld)
        finally:
            con.close()

    # -------------------------------------------------------------- shared
    def _visible_actors(self, con, units) -> set:
        where, binds = self._flow_filter(units)
        return {r["id"] for r in con.execute(
            f"SELECT DISTINCT a.id FROM actors a JOIN flows f "
            f"ON a.id = f.src OR a.id = f.dst WHERE {where}", binds)}

    def _resolve(self, con, term: str, kind: str, units):
        """An id or a name to ONE visible actor, or a question to ask."""
        t = _u(term)
        visible = self._visible_actors(con, units)
        params = {"exact": t, "like": f"%{t}%"}
        sql = ("SELECT id, kind, name, label FROM actors "
               "WHERE (UPPER(name) = :exact OR UPPER(label) = :exact "
               "OR UPPER(name) LIKE :like OR UPPER(label) LIKE :like)")
        if kind:
            sql += " AND kind = :kind"
            params["kind"] = _s(kind).lower()
        rows = [dict(r) for r in con.execute(sql + " LIMIT 60", params)]
        rows = [r for r in rows if r["id"] in visible]
        if not rows:
            return {"scope_status": "entity_not_found",
                    "detail": (f"No actor matches {term!r} in the business "
                               "units this caller can see. This is NO DATA, "
                               "not a zero — and an actor trading only in a "
                               "unit they are not granted looks identical "
                               "to one that does not exist.")}
        exact = [r for r in rows
                 if _u(r["name"]) == t or _u(r["label"]) == t]
        if len(exact) == 1:
            return exact[0]
        if len(rows) == 1:
            return rows[0]
        pool = exact or rows
        if len(pool) == 1:
            return pool[0]
        return {"scope_status": "ambiguous_entity",
                "detail": f"{len(pool)} actors match {term!r}. Ask which one "
                          "is meant, then call again with its id.",
                "multiple_matches": [
                    {"id": r["id"], "kind": r["kind"], "name": r["name"],
                     "label": r["label"]} for r in pool[:10]]}

    def _no_units(self, business_unit, withheld) -> dict:
        wanted = _u(business_unit)
        return {
            "scope_status": "no_visible_units",
            "detail": (
                f"This caller is granted no business unit matching "
                f"{wanted!r}." if wanted else
                "This caller is granted no business unit that has any "
                "recorded flow, so there is nothing to answer from."),
            "units_withheld": len(withheld),
        }

    def _next_steps(self, actor) -> list:
        kind = actor.get("kind")
        name = actor.get("name")
        if kind == "customer":
            return [f"get_customer_financial_360(cust_id={name!r}) for the "
                    "live billing, receivables and cash picture."]
        if kind == "supplier":
            return [f"get_vendor_payables_network(vendor_id={name!r}) for "
                    "the live payables picture and identity links."]
        if kind == "business_unit":
            return [f"get_trial_balance(business_unit={name!r}) for the "
                    "live ledger."]
        return ["get_top_billing_customers(...) confirms any billing figure "
                "against the live ledger."]

    def _stamp(self, con, payload: dict, units, withheld) -> dict:
        """Every answer says when it was true and who it was true for."""
        meta = {r["key"]: r["value"] for r in
                con.execute("SELECT key, value FROM meta")}
        payload["available"] = True
        payload["as_of"] = meta.get("as_of", "")
        payload["built_at"] = meta.get("built_at", "")
        payload["business_units_covered"] = units
        payload["basis"] = (
            "Derived from PeopleSoft transactions at graph build time "
            f"({meta.get('built_at', '')}), over the "
            f"{meta.get('window_months', '')} months to "
            f"{meta.get('as_of', '')}. Amounts are a weight for ranking, "
            "NOT the ledger — confirm any figure that matters with the "
            "live tool.")
        if withheld:
            payload["restricted_to_granted_units"] = True
            payload["units_withheld"] = len(withheld)
            payload["restriction_note"] = (
                f"{len(withheld)} business units are not granted to this "
                "user and were excluded. An actor trading only there does "
                "not appear at all, so this view is partial by design.")
        return payload
