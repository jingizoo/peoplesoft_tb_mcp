"""The connected view of a customer: family, billing, receivables, cash.

Every figure here is read from the financial system. This module owns the
RELATIONSHIPS between the figures — which company owns which, which credit
corrects which invoice, which deposit was applied to which item — because
that is the part no single record answers and the part a person otherwise
assembles by hand across five panels.

Rules this module is built on, and will not quietly relax:

  - Two customers are the same company only when the system says so.
    Membership comes from PS_CUSTOMER.CORPORATE_CUST_ID — PeopleSoft's own
    corporate hierarchy — and from nothing else. Matching on NAME1, address
    or tax id would invent a consolidation that no one approved, and the
    resulting parent total would look exactly as authoritative as a correct
    one. A site without that column gets a single-customer answer and is
    told why, which is the honest failure.
  - Amounts and statuses come from the ledger and its subledgers, as of the
    moment of the call. Nothing here is cached, derived overnight, or
    reconciled after the fact.
  - Totals are aggregated by the database (exact); row-level detail is
    capped and labeled when it truncates. A capped list never becomes a
    total.
  - Currencies are kept apart. A customer billing in two currencies gets
    two totals, never one added-up number.
  - Bank account numbers and tax identifiers are not selected by anything
    in this module. When the shared-bank-account check arrives it compares
    salted hashes; the raw values must not reach a payload or a model.

Cost: one query to resolve the family, then a concurrent fan-out over the
members. Wall time is the slowest branch, not the sum, and the tool is
called on demand — nothing here runs at page load.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .ar import ARBilling, ARError, _NONBLANK, _iso, _iso_opt
from .db import DbError
from .engine import r2, resolve_party_ref

# A corporate family with more members than this is a group structure, not
# a customer question; the answer would be a wall of rows either way.
MEMBER_CAP = 60
# Row-level detail exists to be LOOKED at. Past a few hundred rows nobody
# does, and the exact totals are computed separately anyway.
ITEM_DETAIL_CAP = 300
BILL_DETAIL_CAP = 300
CHAIN_CAP = 100
PAYMENT_CAP = 200
NODE_CAP = 400
# The graph maps the rows the payload actually SHOWS. Mapping all 300 open
# items when the readable list stops at 25 made the map the largest thing
# in the answer and pushed it past the context limit, at which point the
# whole payload is cut mid-object and the reader gets nothing at all.
SHOWN_ROWS = 25


def _days(a, b) -> int:
    """Whole days from a to b, or 0 when either date is unusable."""
    try:
        return (_iso(b) - _iso(a)).days
    except (ARError, TypeError):
        return 0


class Relationships:
    """Relationship-shaped reads over the same records the AR tools use.

    Constructed from ARBilling rather than duplicating its record-shape
    adaptation: a site whose PS_ITEM dates on ASOF_DT instead of ACCTG_DT
    must not need the fix applied twice.
    """

    def __init__(self, ar: ARBilling):
        self.ar = ar
        self.e = ar.e
        self.db = ar.db
        self.cfg = ar.cfg

    # ------------------------------------------------------------- helpers
    def _in(self, prefix: str, values) -> tuple:
        binds = {f"{prefix}{i}": v for i, v in enumerate(values)}
        return "(" + ", ".join(f":{k}" for k in binds) + ")", binds

    def _family(self, setid: str, cust_id: str, include_family: bool,
                notes: list) -> list:
        """The approved corporate family, anchor first.

        Not a guess and not a search: one column, PeopleSoft's own. If the
        column is absent, or the read fails, the answer is the one customer
        that was asked for and a note saying the family was not consulted —
        never a family assembled some other way.
        """
        cs = self.ar._customer_shape()
        cols = self.ar._cols("PS_CUSTOMER")
        p = self.db.prefix
        name_sel = f"C.{cs['name']} AS name" if cs["name"] else "NULL AS name"
        stat_sel = (f"C.{cs['status']} AS status" if cs["status"]
                    else "NULL AS status")
        has_corp = (not cols) or "CORPORATE_CUST_ID" in cols

        anchor_sql = (
            f"SELECT C.CUST_ID AS cust_id, {name_sel}, {stat_sel}"
            + (", C.CORPORATE_CUST_ID AS parent" if has_corp
               else ", NULL AS parent")
            + f" FROM {p}PS_CUSTOMER C "
              "WHERE C.SETID = :setid AND C.CUST_ID = :cust")
        try:
            rows, _ = self.db.query(anchor_sql, {"setid": setid,
                                                 "cust": cust_id}, max_rows=1)
        except DbError as e:
            if not has_corp:
                raise
            # The column list said CORPORATE_CUST_ID was there and the read
            # disagreed (a view, a synonym, a stale catalog). Fall back to
            # the customer alone rather than failing the whole answer.
            notes.append(f"PS_CUSTOMER.CORPORATE_CUST_ID could not be read "
                         f"({e}); the corporate family was not consulted and "
                         "this answer covers one customer.")
            has_corp = False
            rows, _ = self.db.query(
                f"SELECT C.CUST_ID AS cust_id, {name_sel}, {stat_sel}, "
                f"NULL AS parent FROM {p}PS_CUSTOMER C "
                "WHERE C.SETID = :setid AND C.CUST_ID = :cust",
                {"setid": setid, "cust": cust_id}, max_rows=1)
        if not rows:
            return []

        def member(r, role: str) -> dict:
            return {"cust_id": str(r["cust_id"]),
                    "name": (str(r["name"]) if r.get("name") else ""),
                    "status": (str(r["status"]) if r.get("status") else ""),
                    "corporate_parent": (str(r["parent"])
                                         if r.get("parent") else ""),
                    "role": role}

        anchor = member(rows[0], "anchor")
        family = [anchor]
        if not has_corp:
            if include_family and not any("CORPORATE_CUST_ID" in n
                                          for n in notes):
                notes.append(
                    "PS_CUSTOMER here has no CORPORATE_CUST_ID; this site "
                    "does not record a corporate hierarchy, so the answer "
                    "covers this customer only. Grouping customers any "
                    "other way would be a consolidation nobody approved.")
            return family
        parent = anchor["corporate_parent"] or anchor["cust_id"]
        if not include_family:
            return family
        try:
            rows, _ = self.db.query(
                f"SELECT C.CUST_ID AS cust_id, {name_sel}, {stat_sel}, "
                f"C.CORPORATE_CUST_ID AS parent FROM {p}PS_CUSTOMER C "
                "WHERE C.SETID = :setid AND C.CORPORATE_CUST_ID = :parent "
                "ORDER BY C.CUST_ID",
                {"setid": setid, "parent": parent}, max_rows=MEMBER_CAP + 1)
        except DbError as e:
            notes.append(f"The corporate family could not be read ({e}); "
                         "this answer covers one customer.")
            return family
        seen = {anchor["cust_id"]}
        for r in rows[:MEMBER_CAP]:
            cid = str(r["cust_id"])
            if cid in seen:
                continue
            seen.add(cid)
            family.append(member(r, "parent" if cid == parent else "member"))
        if len(rows) > MEMBER_CAP:
            notes.append(
                f"This corporate family has more than {MEMBER_CAP} members; "
                f"the first {MEMBER_CAP} by customer ID are included and the "
                "totals below cover only those. Ask about a subsidiary "
                "directly for a complete answer.")
        if anchor["corporate_parent"] and anchor["corporate_parent"] != \
                anchor["cust_id"]:
            anchor["role"] = "anchor (subsidiary)"
        return family

    # ------------------------------------------------------------- branches
    def _receivables(self, bu: str, ids: list, asof: str) -> dict:
        """Exact per-customer, per-currency open AR with aging buckets.

        Aggregated by the database: this is the total the answer quotes, and
        it must not depend on a row cap.
        """
        shape = self.ar._item_shape()
        edges = self.ar._bucket_edges()
        labels = self.ar._bucket_labels(edges)
        dd = self.db.days_past_expr(self.ar._aging_basis(shape), "asof")
        p = self.db.prefix
        expr, binds = self._in("c", ids)
        cases = [f"SUM(CASE WHEN {dd} <= 0 OR ({dd}) IS NULL "
                 "THEN I.BAL_AMT ELSE 0 END) AS b0"]
        lo = 1
        for i, e in enumerate(edges, start=1):
            cases.append(f"SUM(CASE WHEN {dd} BETWEEN {lo} AND {int(e)} "
                         f"THEN I.BAL_AMT ELSE 0 END) AS b{i}")
            lo = int(e) + 1
        cases.append(f"SUM(CASE WHEN {dd} > {int(edges[-1])} "
                     f"THEN I.BAL_AMT ELSE 0 END) AS b{len(edges) + 1}")
        disputed = (
            f"SUM(CASE WHEN {_NONBLANK.format(col='I.' + shape['dispute'])} "
            "THEN I.BAL_AMT ELSE 0 END) AS disputed"
            if shape["dispute"] else "SUM(0) AS disputed")
        cur_sel = (f"I.{shape['currency']} AS currency" if shape["currency"]
                   else "'' AS currency")
        group_cur = f", I.{shape['currency']}" if shape["currency"] else ""
        asof_cut = (f" AND I.{shape['date']} <= {self.db.date_bind('asof')}"
                    if shape["date"] else "")
        rows, _ = self.db.query(
            f"SELECT I.CUST_ID AS cust_id, {cur_sel}, {', '.join(cases)}, "
            "SUM(I.BAL_AMT) AS total, "
            "SUM(CASE WHEN I.BAL_AMT < 0 THEN I.BAL_AMT ELSE 0 END) AS credits, "
            f"{disputed}, "
            f"MAX(CASE WHEN {dd} > 0 THEN {dd} ELSE 0 END) AS oldest_days, "
            "COUNT(*) AS items "
            f"FROM {p}PS_ITEM I "
            f"WHERE I.BUSINESS_UNIT = :bu AND I.ITEM_STATUS = 'O'{asof_cut} "
            f"AND I.CUST_ID IN {expr} "
            f"GROUP BY I.CUST_ID{group_cur}",
            {"bu": bu, "asof": asof, **binds}, max_rows=len(ids) * 10)

        by_customer: dict = {}
        by_currency: dict = {}
        for r in rows:
            cur = str(r["currency"] or "")
            buckets = {labels[i]: r2(float(r[f"b{i}"] or 0))
                       for i in range(len(labels))}
            entry = {
                "cust_id": str(r["cust_id"]), "currency": cur,
                "open_balance": r2(float(r["total"] or 0)),
                "overdue": r2(sum(v for k, v in buckets.items()
                                  if k != labels[0])),
                "credits_on_account": r2(float(r["credits"] or 0)),
                "disputed": r2(float(r["disputed"] or 0)),
                "oldest_days_past_due": int(r["oldest_days"] or 0),
                "open_items": int(r["items"] or 0),
                "buckets": buckets,
            }
            by_customer.setdefault(str(r["cust_id"]), []).append(entry)
            agg = by_currency.setdefault(cur, {
                "currency": cur, "open_balance": 0.0, "overdue": 0.0,
                "credits_on_account": 0.0, "disputed": 0.0,
                "oldest_days_past_due": 0, "open_items": 0,
                "buckets": {k: 0.0 for k in labels}})
            for key in ("open_balance", "overdue", "credits_on_account",
                        "disputed", "open_items"):
                agg[key] = r2(agg[key] + entry[key])
            agg["oldest_days_past_due"] = max(agg["oldest_days_past_due"],
                                              entry["oldest_days_past_due"])
            for k, v in buckets.items():
                agg["buckets"][k] = r2(agg["buckets"][k] + v)
        for agg in by_currency.values():
            agg["open_items"] = int(agg["open_items"])
        return {"by_customer": by_customer,
                "totals_by_currency": sorted(by_currency.values(),
                                             key=lambda x: -abs(
                                                 x["open_balance"])),
                "bucket_labels": labels,
                "aging_basis": self.ar._aging_basis(shape).replace("I.", ""),
                "notes": list(shape["notes"])}

    def _open_items(self, bu: str, ids: list, asof: str) -> dict:
        """The largest open items, for the graph and for looking at."""
        shape = self.ar._item_shape()
        p = self.db.prefix
        expr, binds = self._in("c", ids)
        due_sel = (f"I.{shape['due']} AS due_dt" if shape["due"]
                   else "NULL AS due_dt")
        date_sel = (f"I.{shape['date']} AS item_dt" if shape["date"]
                    else "NULL AS item_dt")
        disp_sel = (f"I.{shape['dispute']} AS dispute" if shape["dispute"]
                    else "NULL AS dispute")
        cur_sel = (f"I.{shape['currency']} AS currency" if shape["currency"]
                   else "'' AS currency")
        asof_cut = (f" AND I.{shape['date']} <= {self.db.date_bind('asof')}"
                    if shape["date"] else "")
        orig = ("I.ORIG_ITEM_AMT AS orig_amt"
                if "ORIG_ITEM_AMT" in (self.ar._cols("PS_ITEM") or
                                       {"ORIG_ITEM_AMT"})
                else "NULL AS orig_amt")
        rows, _ = self.db.query(
            f"SELECT I.CUST_ID AS cust_id, I.ITEM AS item, "
            f"I.BAL_AMT AS bal, {orig}, {cur_sel}, {date_sel}, {due_sel}, "
            f"{disp_sel} FROM {p}PS_ITEM I "
            f"WHERE I.BUSINESS_UNIT = :bu AND I.ITEM_STATUS = 'O'{asof_cut} "
            f"AND I.CUST_ID IN {expr} "
            "ORDER BY I.CUST_ID, I.ITEM",
            {"bu": bu, "asof": asof, **binds}, max_rows=ITEM_DETAIL_CAP + 1)
        truncated = len(rows) > ITEM_DETAIL_CAP
        items = []
        for r in rows[:ITEM_DETAIL_CAP]:
            due = _iso_opt(r.get("due_dt")) or _iso_opt(r.get("item_dt"))
            bal = r2(float(r["bal"] or 0))
            items.append({
                "cust_id": str(r["cust_id"]), "item": str(r["item"]),
                "balance": bal,
                "original_amount": (r2(float(r["orig_amt"]))
                                    if r.get("orig_amt") is not None else None),
                "paid_down": (r2(float(r["orig_amt"]) - bal)
                              if r.get("orig_amt") is not None else None),
                "currency": str(r["currency"] or ""),
                "item_date": (str(r["item_dt"])[:10] if r.get("item_dt")
                              else ""),
                "due_date": (str(r["due_dt"])[:10] if r.get("due_dt") else ""),
                "days_past_due": (max(0, (_iso(asof) - due).days) if due
                                  else 0),
                "in_dispute": bool(str(r.get("dispute") or "").strip()),
                "dispute_status": str(r.get("dispute") or "").strip(),
            })
        items.sort(key=lambda x: -abs(x["balance"]))
        return {"items": items, "truncated": truncated}

    def _billing(self, bu: str, ids: list, since: str, asof: str) -> dict:
        """Bills by status (exact) plus the ones not yet finalized.

        The date cut matches receivables — bills raised on or before the
        as-of date — but a bill STATUS is current state: PeopleSoft does not
        keep the status a bill held on some earlier day. So this answers
        "of the bills raised by then, which are still not finalized", and
        the payload says exactly that rather than implying a replay.
        """
        p = self.db.prefix
        expr, binds = self._in("c", ids)
        cols = self.ar._cols("PS_BI_HDR")
        cur = "BI_CURRENCY_CD" if (not cols or
                                   "BI_CURRENCY_CD" in cols) else ""
        cur_sel = f"H.{cur} AS currency" if cur else "'' AS currency"
        cur_grp = f", H.{cur}" if cur else ""
        rows, _ = self.db.query(
            f"SELECT H.BILL_STATUS AS status, {cur_sel}, "
            "COUNT(*) AS bills, SUM(H.INVOICE_AMOUNT) AS amount, "
            "MIN(H.INVOICE_DT) AS oldest "
            f"FROM {p}PS_BI_HDR H WHERE H.BUSINESS_UNIT = :bu "
            f"AND H.BILL_TO_CUST_ID IN {expr} "
            f"AND H.INVOICE_DT >= {self.db.date_bind('since')} "
            f"AND H.INVOICE_DT <= {self.db.date_bind('asof')} "
            f"GROUP BY H.BILL_STATUS{cur_grp} ORDER BY H.BILL_STATUS",
            {"bu": bu, "since": since, "asof": asof, **binds}, max_rows=200)
        from .ar import BILL_STATUS_DESCR
        sem = self.ar._billing_invoiced_semantics()
        finalized = set(sem["values"])
        by_status = [{
            "status": str(r["status"] or ""),
            "meaning": BILL_STATUS_DESCR.get(str(r["status"] or ""), ""),
            "finalized": str(r["status"] or "") in finalized,
            "class": (
                "finalized" if str(r["status"] or "") in finalized else
                ("terminal" if str(r["status"] or "") in
                 self.ar._BILL_TERMINAL else "pipeline")),
            "currency": str(r["currency"] or ""),
            "bills": int(r["bills"] or 0),
            "amount": r2(float(r["amount"] or 0)),
            "oldest_bill_date": str(r["oldest"] or "")[:10],
        } for r in rows]

        # Negative classification preserves the old all-history detail read
        # while allowing any custom status: everything that is neither a
        # governed finalized code nor a terminal cancellation is pipeline.
        # This also finds an old pipeline status absent from the summary
        # window above.
        fin_expr, fin_binds = self._in("f", sem["values"])
        term_expr, term_binds = self._in(
            "t", sorted(self.ar._BILL_TERMINAL))
        rows, _ = self.db.query(
            f"SELECT H.INVOICE AS invoice, H.BILL_STATUS AS status, "
            f"H.BILL_TO_CUST_ID AS cust_id, H.INVOICE_DT AS invoice_dt, "
            f"H.INVOICE_AMOUNT AS amount, {cur_sel} "
            f"FROM {p}PS_BI_HDR H WHERE H.BUSINESS_UNIT = :bu "
            f"AND H.BILL_TO_CUST_ID IN {expr} "
            f"AND H.BILL_STATUS NOT IN {fin_expr} "
            f"AND H.BILL_STATUS NOT IN {term_expr} "
            f"AND H.INVOICE_DT <= {self.db.date_bind('asof')} "
            "ORDER BY H.INVOICE_DT",
            {"bu": bu, "asof": asof, **binds, **fin_binds, **term_binds},
            max_rows=BILL_DETAIL_CAP + 1)
        in_flight = [{
            "invoice": str(r["invoice"]),
            "cust_id": str(r["cust_id"] or ""),
            "status": str(r["status"] or ""),
            "meaning": BILL_STATUS_DESCR.get(str(r["status"] or ""), ""),
            "invoice_date": str(r["invoice_dt"] or "")[:10],
            "amount": r2(float(r["amount"] or 0)),
            "currency": str(r["currency"] or ""),
        } for r in rows[:BILL_DETAIL_CAP]]
        return {"by_status": by_status, "not_yet_finalized": in_flight,
                "truncated": len(rows) > BILL_DETAIL_CAP,
                "population": {"concept": "finalized billing",
                               "predicate": sem["predicate"],
                               "source": sem["source"],
                               "meaning": sem["meaning"]},
                **({"record_notes": sem["notes"]} if sem["notes"] else {}),
                "basis": f"Bills dated {since} to {asof}. Finalized means "
                         f"{sem['predicate']} ({sem['source']}); cancelled "
                         "bills are terminal, never finalized or pipeline. "
                         "A bill's STATUS is "
                         "current state — PeopleSoft does not keep the status "
                         "it held on an earlier day — so this is: of the "
                         "bills raised in that window, which are still not "
                         "finalized now."}

    def _chains(self, bu: str, ids: list) -> dict:
        """Credit / rebill / adjustment chains, and what they net to.

        A credit memo on its own says a bill was wrong. The chain says
        whether the money was ever re-billed, and what was given up if it
        was re-billed for less. That difference is invisible one row at a
        time, which is exactly why it goes unnoticed.
        """
        cols = self.ar._cols("PS_BI_HDR")
        if cols and "CR_REBILL_INV" not in cols:
            return {"chains": [], "supported": False,
                    "note": "PS_BI_HDR here has no CR_REBILL_INV; credit and "
                            "rebill invoices cannot be linked to the invoice "
                            "they correct, so no chain is reported. Credit "
                            "memos still appear in receivables."}
        adj = "H.ADJUSTMENT_TYPE" if (not cols or
                                      "ADJUSTMENT_TYPE" in cols) else "''"
        p = self.db.prefix
        expr, binds = self._in("c", ids)
        try:
            rows, _ = self.db.query(
                f"SELECT H.INVOICE AS invoice, H.BILL_TO_CUST_ID AS cust_id, "
                f"H.CR_REBILL_INV AS corrects, {adj} AS adj_type, "
                "H.INVOICE_DT AS invoice_dt, H.INVOICE_AMOUNT AS amount, "
                "H.BILL_STATUS AS status "
                f"FROM {p}PS_BI_HDR H WHERE H.BUSINESS_UNIT = :bu "
                f"AND H.BILL_TO_CUST_ID IN {expr} "
                f"AND {_NONBLANK.format(col='H.CR_REBILL_INV')} "
                "ORDER BY H.CR_REBILL_INV, H.INVOICE_DT",
                {"bu": bu, **binds}, max_rows=CHAIN_CAP + 1)
        except DbError as e:
            return {"chains": [], "supported": False,
                    "note": f"Credit/rebill links could not be read ({e})."}
        rows = rows[:CHAIN_CAP]
        if not rows:
            return {"chains": [], "supported": True}

        originals = sorted({str(r["corrects"]) for r in rows if r["corrects"]})
        o_expr, o_binds = self._in("o", originals)
        base, _ = self.db.query(
            "SELECT H.INVOICE AS invoice, H.BILL_TO_CUST_ID AS cust_id, "
            "H.INVOICE_DT AS invoice_dt, H.INVOICE_AMOUNT AS amount, "
            "H.BILL_STATUS AS status "
            f"FROM {p}PS_BI_HDR H WHERE H.BUSINESS_UNIT = :bu "
            f"AND H.INVOICE IN {o_expr}",
            {"bu": bu, **o_binds}, max_rows=len(originals) + 1)
        head = {str(r["invoice"]): r for r in base}

        chains = []
        for inv in originals:
            parts = [r for r in rows if str(r["corrects"]) == inv]
            orig = head.get(inv)
            orig_amt = r2(float(orig["amount"] or 0)) if orig else None
            corrections = [{
                "invoice": str(r["invoice"]),
                "adjustment_type": str(r["adj_type"] or "").strip(),
                "invoice_date": str(r["invoice_dt"] or "")[:10],
                "amount": r2(float(r["amount"] or 0)),
                "status": str(r["status"] or ""),
            } for r in parts]
            net = r2((orig_amt or 0.0)
                     + sum(c["amount"] for c in corrections))
            chains.append({
                "original_invoice": inv,
                "cust_id": str(orig["cust_id"]) if orig else str(
                    parts[0]["cust_id"] or ""),
                "original_amount": orig_amt,
                "original_date": (str(orig["invoice_dt"] or "")[:10]
                                  if orig else ""),
                "corrections": corrections,
                "net_billed": net if orig_amt is not None else None,
                "given_up": (r2((orig_amt or 0.0) - net)
                             if orig_amt is not None else None),
                "original_found": orig is not None,
            })
        chains.sort(key=lambda c: -abs(c.get("given_up") or 0))
        return {"chains": chains, "supported": True,
                "truncated": len(rows) >= CHAIN_CAP}

    def _cash(self, bu: str, ids: list, since: str) -> dict:
        """Payments received, and how much of each was actually applied.

        Kept as two reads on purpose. A deposit that arrived and was never
        applied is money the company is holding while the customer still
        shows as owing it — collapse the two and that state disappears.
        """
        if not self.ar._cols("PS_PAYMENT"):
            return {"supported": False, "payments": [],
                    "note": "PS_PAYMENT is not readable at this site; cash "
                            "received is not reported. Item balances still "
                            "reflect payments that were applied."}
        p = self.db.prefix
        expr, binds = self._in("c", ids)
        try:
            rows, _ = self.db.query(
                "SELECT P.DEPOSIT_ID AS deposit, P.PAYMENT_SEQ_NUM AS seq, "
                "P.CUST_ID AS cust_id, P.PAYMENT_AMT AS amount, "
                "P.PAYMENT_DT AS payment_dt, "
                "P.PAYMENT_CURRENCY AS currency, "
                "P.PAYMENT_STATUS AS status "
                f"FROM {p}PS_PAYMENT P WHERE P.DEPOSIT_BU = :bu "
                f"AND P.CUST_ID IN {expr} "
                f"AND P.PAYMENT_DT >= {self.db.date_bind('since')} "
                "ORDER BY P.PAYMENT_DT DESC",
                {"bu": bu, "since": since, **binds},
                max_rows=PAYMENT_CAP + 1)
        except DbError as e:
            return {"supported": False, "payments": [],
                    "note": f"PS_PAYMENT could not be read ({e})."}
        truncated = len(rows) > PAYMENT_CAP
        rows = rows[:PAYMENT_CAP]

        applied: dict = {}
        applications: list = []
        if rows and self.ar._cols("PS_PAYMENT_ITEM"):
            d_expr, d_binds = self._in(
                "d", sorted({str(r["deposit"]) for r in rows}))
            try:
                arows, _ = self.db.query(
                    "SELECT A.DEPOSIT_ID AS deposit, "
                    "A.PAYMENT_SEQ_NUM AS seq, A.CUST_ID AS cust_id, "
                    "A.ITEM AS item, A.APPLIED_AMT AS amount "
                    f"FROM {p}PS_PAYMENT_ITEM A WHERE A.DEPOSIT_BU = :bu "
                    f"AND A.DEPOSIT_ID IN {d_expr}",
                    {"bu": bu, **d_binds}, max_rows=PAYMENT_CAP * 20)
            except DbError:
                arows = []
            for r in arows:
                key = (str(r["deposit"]), int(r["seq"] or 0))
                applied[key] = r2(applied.get(key, 0.0)
                                  + float(r["amount"] or 0))
                applications.append({
                    "deposit_id": str(r["deposit"]),
                    "payment_seq": int(r["seq"] or 0),
                    "cust_id": str(r["cust_id"] or ""),
                    "item": str(r["item"] or ""),
                    "applied_amount": r2(float(r["amount"] or 0)),
                })

        payments, by_currency = [], {}
        for r in rows:
            key = (str(r["deposit"]), int(r["seq"] or 0))
            amount = r2(float(r["amount"] or 0))
            app = r2(applied.get(key, 0.0))
            cur = str(r["currency"] or "")
            payments.append({
                "deposit_id": key[0], "payment_seq": key[1],
                "cust_id": str(r["cust_id"] or ""),
                "amount": amount, "applied": app,
                "unapplied": r2(amount - app),
                "payment_date": str(r["payment_dt"] or "")[:10],
                "currency": cur, "status": str(r["status"] or ""),
            })
            agg = by_currency.setdefault(cur, {
                "currency": cur, "received": 0.0, "applied": 0.0,
                "unapplied": 0.0, "payments": 0})
            agg["received"] = r2(agg["received"] + amount)
            agg["applied"] = r2(agg["applied"] + app)
            agg["unapplied"] = r2(agg["unapplied"] + amount - app)
            agg["payments"] += 1
        return {"supported": True, "payments": payments,
                "applications": applications,
                "totals_by_currency": sorted(by_currency.values(),
                                             key=lambda x: -x["received"]),
                "truncated": truncated}

    def _locations(self, setid: str, ids: list) -> dict:
        if not self.ar._cols("PS_CUST_ADDRESS"):
            return {}
        expr, binds = self._in("c", ids)
        rows, _ = self.db.query(
            f"SELECT CUST_ID AS cust_id, CITY AS city, STATE AS state, "
            f"COUNTRY AS country FROM {self.db.prefix}PS_CUST_ADDRESS "
            f"WHERE SETID = :setid AND CUST_ID IN {expr} "
            "ORDER BY ADDRESS_SEQ_NUM",
            {"setid": setid, **binds}, max_rows=len(ids) * 5)
        out: dict = {}
        for r in rows:
            out.setdefault(str(r["cust_id"]), {
                "city": str(r.get("city") or ""),
                "state": str(r.get("state") or ""),
                "country": str(r.get("country") or "")})
        return out

    # ------------------------------------------------------------ the graph
    def _graph(self, family: list, receivables: dict, items: dict,
               billing: dict, chains: dict, cash: dict) -> dict:
        """A MAP of what connects to what — deliberately carrying no money.

        Every edge exists because a column in one record names a key in
        another: CORPORATE_CUST_ID names the parent, PS_PAYMENT_ITEM names
        the item a deposit paid, CR_REBILL_INV names the invoice a credit
        corrects. No edge is created from resemblance.

        The nodes used to repeat every balance, amount and due date that
        already sat in the sections above. Measured on the sample that was
        a third of a small payload and most of a large one, for figures
        NOTHING read: the browser card uses id, label and the edge triple
        and ignores the rest. Worse, the duplicates were live to the
        grounding guard, which grounds per-column sums over any list of row
        dicts — so a 300-item graph handed the model an allowlist of ~275
        balances the readable sections had capped at 25, and pushed the
        payload toward the truncation that blanks the card outright.

        So the map is now a map. Amounts live in receivables, billing,
        adjustments and cash, exactly once, and this says how those rows
        reach each other.
        """
        nodes, edges, seen = [], [], set()

        def node(nid: str, kind: str, label: str) -> str:
            if nid not in seen and len(nodes) < NODE_CAP:
                seen.add(nid)
                nodes.append({"id": nid, "type": kind, "label": label})
            return nid

        def edge(src: str, dst: str, kind: str, **extra) -> None:
            if src in seen and dst in seen:
                edges.append({"from": src, "to": dst, "type": kind, **extra})

        for m in family:
            node(f"CUST:{m['cust_id']}", "Customer",
                 m["name"] or m["cust_id"])
        for m in family:
            parent = m.get("corporate_parent")
            if parent and parent != m["cust_id"]:
                edge(f"CUST:{m['cust_id']}", f"CUST:{parent}",
                     "SUBSIDIARY_OF", basis="PS_CUSTOMER.CORPORATE_CUST_ID")

        # Same slice the payload displays, so every node on the map is a
        # row the reader can also look up. An edge whose endpoint fell
        # outside the slice is dropped by edge() rather than left dangling.
        for it in items["items"][:SHOWN_ROWS]:
            nid = node(f"ITEM:{it['item']}", "Receivable", it["item"])
            edge(f"CUST:{it['cust_id']}", nid, "OWES")
        for b in billing["not_yet_finalized"][:SHOWN_ROWS]:
            nid = node(f"BILL:{b['invoice']}", "Bill", b["invoice"])
            edge(f"CUST:{b['cust_id']}", nid, "BILLED_TO")
        for ch in chains.get("chains") or []:
            oid = node(f"BILL:{ch['original_invoice']}", "Bill",
                       ch["original_invoice"])
            edge(f"CUST:{ch['cust_id']}", oid, "BILLED_TO")
            for c in ch["corrections"]:
                cid = node(f"BILL:{c['invoice']}",
                           "Credit" if c["amount"] < 0 else "Rebill",
                           c["invoice"])
                edge(cid, oid, "CORRECTS", basis="PS_BI_HDR.CR_REBILL_INV")
        for pay in (cash.get("payments") or [])[:SHOWN_ROWS]:
            nid = node(f"PAY:{pay['deposit_id']}:{pay['payment_seq']}",
                       "Payment", pay["deposit_id"])
            edge(f"CUST:{pay['cust_id']}", nid, "PAID")
        for app in cash.get("applications") or []:
            edge(f"PAY:{app['deposit_id']}:{app['payment_seq']}",
                 f"ITEM:{app['item']}", "APPLIED_TO")

        # Count the nodes that exist, not the rows that were offered: the
        # credit/rebill chain adds bills the pipeline slice never saw, and
        # a node whose id repeats is stored once. Disclosure that is
        # re-derived from the inputs is disclosure of the wrong number.
        kinds = Counter(n["type"] for n in nodes)
        mapped = {"items": kinds["Receivable"],
                  "bills": kinds["Bill"] + kinds["Credit"] + kinds["Rebill"],
                  "payments": kinds["Payment"],
                  "customers": kinds["Customer"]}
        return {"nodes": nodes, "edges": edges,
                "truncated": (len(seen) >= NODE_CAP
                              or len(items["items"]) > SHOWN_ROWS
                              or len(billing["not_yet_finalized"])
                              > SHOWN_ROWS
                              or len(cash.get("payments") or []) > SHOWN_ROWS),
                "maps_at_most_per_kind": SHOWN_ROWS, "mapped": mapped,
                "basis": "Edges come from key columns in the records "
                         "themselves — corporate hierarchy, payment "
                         "application, credit/rebill reference. None is "
                         "inferred from names or amounts.",
                "read_this_as": "A map, not a source of figures. It says "
                                "WHICH records connect; every amount is in "
                                "receivables, billing, adjustments or cash "
                                "above. Quote figures from those, and use "
                                "this to explain how they relate.",
                }

    # -------------------------------------------------------- what to do now
    def _attention(self, family: list, receivables: dict, items: dict,
                   billing: dict, chains: dict, cash: dict,
                   asof: str) -> list:
        """Arithmetic over what was read, each item carrying its figures.

        Not advice and not a model's opinion: every entry below is a
        comparison between numbers already in the payload, so the guard can
        verify each one and a reader can check it by hand.
        """
        out: list = []
        multi = len(family) > 1

        for agg in receivables["totals_by_currency"]:
            if agg["overdue"] > 0:
                contributors = []
                if multi:
                    for m in family:
                        for t in (receivables["by_customer"].get(m["cust_id"])
                                  or []):
                            if t["currency"] == agg["currency"] and \
                                    t["overdue"] > 0:
                                contributors.append(
                                    {"cust_id": m["cust_id"],
                                     "name": m["name"],
                                     "overdue": t["overdue"]})
                    contributors.sort(key=lambda c: -c["overdue"])
                out.append({
                    "kind": "overdue_receivable",
                    "headline": f"{agg['overdue']:,.2f} {agg['currency']} "
                                f"is past due, the oldest by "
                                f"{agg['oldest_days_past_due']} days.",
                    "amount": agg["overdue"], "currency": agg["currency"],
                    "oldest_days_past_due": agg["oldest_days_past_due"],
                    "contributors": contributors,
                })
            if agg["disputed"]:
                out.append({
                    "kind": "disputed",
                    "headline": f"{abs(agg['disputed']):,.2f} "
                                f"{agg['currency']} of the open balance is "
                                "flagged in dispute and will not collect "
                                "until the dispute is settled.",
                    "amount": agg["disputed"], "currency": agg["currency"],
                })
            if agg["credits_on_account"] < 0:
                out.append({
                    "kind": "credits_not_applied",
                    "headline": f"{abs(agg['credits_on_account']):,.2f} "
                                f"{agg['currency']} sits as credits or "
                                "on-account amounts that have not been "
                                "applied to any invoice.",
                    "amount": agg["credits_on_account"],
                    "currency": agg["currency"],
                })

        for agg in cash.get("totals_by_currency") or []:
            if agg["unapplied"] > 0.005:
                who = [p for p in cash["payments"] if p["unapplied"] > 0.005
                       and p["currency"] == agg["currency"]]
                out.append({
                    "kind": "cash_received_not_applied",
                    "headline": f"{agg['unapplied']:,.2f} {agg['currency']} "
                                "was received and never applied to an "
                                "invoice — the customer still shows as "
                                "owing it.",
                    "amount": agg["unapplied"], "currency": agg["currency"],
                    "deposits": [{"deposit_id": p["deposit_id"],
                                  "payment_date": p["payment_date"],
                                  "unapplied": p["unapplied"]} for p in who],
                })

        for ch in chains.get("chains") or []:
            given = ch.get("given_up")
            if given and abs(given) >= 0.01:
                out.append({
                    "kind": "credit_not_fully_rebilled",
                    "headline": f"Invoice {ch['original_invoice']} was "
                                f"credited and re-billed for "
                                f"{abs(given):,.2f} less than the original.",
                    "amount": given,
                    "original_invoice": ch["original_invoice"],
                    "original_amount": ch["original_amount"],
                    "net_billed": ch["net_billed"],
                })

        stuck: dict = {}
        for b in billing["not_yet_finalized"]:
            age = _days(b["invoice_date"], asof) if b["invoice_date"] else 0
            agg = stuck.setdefault(b["currency"], {"amount": 0.0, "bills": 0,
                                                   "oldest": 0})
            agg["amount"] = r2(agg["amount"] + b["amount"])
            agg["bills"] += 1
            agg["oldest"] = max(agg["oldest"], age)
        for cur, agg in stuck.items():
            if agg["amount"]:
                out.append({
                    "kind": "revenue_not_yet_billed",
                    "headline": f"{agg['amount']:,.2f} {cur} across "
                                f"{agg['bills']} bill(s) has not been "
                                f"finalized; the oldest has been waiting "
                                f"{agg['oldest']} days.",
                    "amount": agg["amount"], "currency": cur,
                    "bills": agg["bills"], "oldest_days": agg["oldest"],
                })

        for it in items["items"][:5]:
            if it["days_past_due"] >= 90 and it["balance"] > 0:
                out.append({
                    "kind": "aged_item",
                    "headline": f"Item {it['item']} "
                                f"({it['balance']:,.2f} {it['currency']}) is "
                                f"{it['days_past_due']} days past due.",
                    "amount": it["balance"], "currency": it["currency"],
                    "item": it["item"],
                    "days_past_due": it["days_past_due"],
                })
        return out

    # ------------------------------------------------------------------ tool
    def customer_financial_360(self, cust_id: str = "",
                               business_unit: str = "",
                               include_family: bool = True,
                               months: int = 12,
                               as_of_date: str = "") -> dict:
        bu = self.ar._bu(business_unit)
        asof = self.ar._asof(as_of_date)
        setid = self.e.resolve_setid(bu, "CUSTOMER")
        window = max(int(months or 12), 1)
        since = (_iso(asof) - dt.timedelta(days=window * 31)).isoformat()
        notes: list = []

        # cust_id is what the caller HAS, which for a person asking about a
        # company is its NAME. Compare it to CUST_ID and stop, and "revenue
        # for CIBC" comes back "does not exist" for a customer that does.
        #
        # The id is tried FIRST, on the anchor read this needs anyway, so
        # the common path costs exactly what it did before: no search, no
        # extra round trip, and no dependence on the records search_
        # customers joins — an id lookup must not start failing because
        # PS_ITEM is unreadable. Only a miss falls through to the name.
        cust = (cust_id or "").strip()
        probe: list = []
        family = self._family(setid, cust, include_family, probe) if cust \
            else []
        if family:
            notes.extend(probe)
        else:
            cust, read_as, refusal = resolve_party_ref(
                cust_id,
                lambda term: self.ar.search_customers(
                    term, limit=10, business_unit=bu)["customers"],
                "cust_id", "customer")
            if refusal:
                refusal.update({"business_unit": bu, "setid": setid})
                return refusal
            if read_as:
                notes.append(read_as)
            family = self._family(setid, cust, include_family, notes)
            if not family:
                # Search saw it, the anchor read did not: a row outside this
                # SETID, or one the scope cannot see.
                return {
                    "scope_status": "customer_not_found",
                    "detail": f"Customer {cust!r} does not exist in SETID "
                              f"{setid!r}. This is NO DATA, not a zero "
                              "balance.",
                    "business_unit": bu, "setid": setid,
                }
        ids = [m["cust_id"] for m in family]

        # The branches read different records and do not depend on each
        # other, so the answer costs the slowest one rather than their sum.
        # This matters on a real instance, where PS_ITEM and PS_BI_HDR are
        # both large and both indexed on (BUSINESS_UNIT, CUST_ID, ...).
        def run(fn, *a):
            try:
                return fn(*a)
            except Exception as e:              # noqa: BLE001
                return {"error": f"{type(e).__name__}: {e}"}

        with ThreadPoolExecutor(max_workers=6) as pool:   # one per branch
            f_recv = pool.submit(run, self._receivables, bu, ids, asof)
            f_items = pool.submit(run, self._open_items, bu, ids, asof)
            f_bill = pool.submit(run, self._billing, bu, ids, since, asof)
            f_chain = pool.submit(run, self._chains, bu, ids)
            f_cash = pool.submit(run, self._cash, bu, ids, since)
            f_loc = pool.submit(run, self._locations, setid, ids)
            recv, items = f_recv.result(), f_items.result()
            bill, chains = f_bill.result(), f_chain.result()
            cash, loc = f_cash.result(), f_loc.result()

        # A branch that failed must cost its own section, not the answer.
        blanks = {"receivables": {"by_customer": {}, "totals_by_currency": [],
                                  "bucket_labels": [], "aging_basis": "",
                                  "notes": []},
                  "items": {"items": [], "truncated": False},
                  "billing": {"by_status": [], "not_yet_finalized": [],
                              "truncated": False, "basis": ""},
                  "chains": {"chains": [], "supported": False},
                  "cash": {"supported": False, "payments": []},
                  "locations": {}}
        named = {"receivables": recv, "items": items, "billing": bill,
                 "chains": chains, "cash": cash, "locations": loc}
        for key, value in list(named.items()):
            if isinstance(value, dict) and value.get("error"):
                notes.append(f"The {key} section could not be read: "
                             f"{value['error']}")
                named[key] = blanks[key]
        recv, items = named["receivables"], named["items"]
        bill, chains = named["billing"], named["chains"]
        cash, loc = named["cash"], named["locations"]
        notes.extend(recv.get("notes") or [])
        for section in (chains, cash):
            if section.get("note"):
                notes.append(section["note"])
        if items.get("truncated"):
            notes.append(
                f"More than {ITEM_DETAIL_CAP} open items — the item list "
                "below is the first page. The balances and buckets are "
                "totals from the database and are complete.")
        if len(recv.get("totals_by_currency") or []) > 1:
            notes.append(
                "This customer transacts in more than one currency. Totals "
                "are reported per currency and are not added together; ask "
                "get_ar_aging with display_currency for a converted view.")

        for m in family:
            m["location"] = loc.get(m["cust_id"], {})
            m["receivables"] = recv["by_customer"].get(m["cust_id"], [])

        attention = self._attention(family, recv, items, bill, chains, cash,
                                    asof)
        graph = self._graph(family, recv, items, bill, chains, cash)
        anchor = family[0]
        return {
            "business_unit": bu, "setid": setid, "as_of_date": asof,
            "window_months": window, "window_start": since,
            "customer": {k: anchor[k] for k in
                         ("cust_id", "name", "status", "corporate_parent")}
            | {"location": anchor.get("location", {})},
            "family": {
                "members": family,
                "member_count": len(family),
                "basis": "PS_CUSTOMER.CORPORATE_CUST_ID — the corporate "
                         "hierarchy recorded in the financial system. "
                         "Customers are never grouped by name or address.",
                "included_in_totals": len(family) > 1,
            },
            "receivables": {
                "totals_by_currency": recv["totals_by_currency"],
                "bucket_labels": recv["bucket_labels"],
                "aging_basis": recv["aging_basis"],
                "largest_open_items": items["items"][:SHOWN_ROWS],
            },
            "billing": bill,
            "adjustments": chains,
            "cash": cash,
            "needs_attention": attention,
            "relationships": graph,
            "record_notes": notes,
            "basis": (
                "Read live from the financial system at the time of this "
                "call. Totals are aggregated by the database; row lists are "
                "capped and say so when they truncate. Aging is measured "
                f"against {asof}."),
        }
