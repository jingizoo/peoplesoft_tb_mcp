"""AR aging and Billing pipeline over PS_ITEM / PS_CUSTOMER / PS_BI_HDR / INTFC_BI.

Semantics (deliberate, reviewed):
  - PS_ITEM.BAL_AMT is the open receivable: positive = the customer owes it,
    negative = a credit memo or on-account/unapplied receipt.
  - PS_ITEM is a CURRENT-STATE record. A backdated as-of date therefore does
    NOT reconstruct history — items closed since then are gone, partial
    payments show today's residual. Such results are labeled an approximation;
    true historical aging needs PS_ITEM_ACTIVITY reconstruction (future work).
  - Aging buckets classify by days past COALESCE(DUE_DT, ACCTG_DT) at the
    as-of date; items count when ACCTG_DT <= as-of.
  - The GL tie-out is decoupled from the as-of date: it compares ALL current
    open items against the AR control balance through the LATEST POSTED
    period, and says so ("basis"). Comparing a date-cut subledger to a
    period-end GL fabricates breaks for any mid-period date. If a control
    lookup fails or the scope is empty, the tie is reported as NOT EVALUATED —
    never as a pass.
  - Customer summaries aggregate in SQL (one GROUP BY, scales to a real
    PS_ITEM); item detail is fetched row-wise only when asked, with a cap.
  - Billing lifecycle: NEW/PND -> HLD -> RDY -> INV (finalized) or CAN.
    Finalized invoices missing from PS_ITEM have not reached AR.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from .engine import EngineError, TBEngine, r2

BILL_STATUS_DESCR = {
    "NEW": "New bill",
    "PND": "Pending",
    "HLD": "On hold",
    "RDY": "Ready to invoice",
    "INV": "Finalized (invoiced)",
    "CAN": "Canceled",
    "TMP": "Temporary bill",
}
LOAD_STATUS_DESCR = {"NEW": "Awaiting processing", "DON": "Processed", "ERR": "Error"}
NOT_FINAL = ("NEW", "PND", "HLD", "RDY", "TMP")
DETAIL_ROW_CAP = 5_000

# Oracle-safe "column has a non-blank value" ('' is NULL on Oracle, ' ' common)
_NONBLANK = "COALESCE(TRIM({col}), '') <> ''"


class ARError(RuntimeError):
    pass


def _iso(day) -> dt.date:
    try:
        return dt.date.fromisoformat(str(day)[:10])
    except (ValueError, TypeError) as e:
        raise ARError(f"Bad date {day!r} — use YYYY-MM-DD") from e


def _iso_opt(day) -> Optional[dt.date]:
    if day is None or str(day).strip() in ("", "None"):
        return None
    return _iso(day)


class ARBilling:
    def __init__(self, engine: TBEngine):
        self.e = engine
        self.cfg = engine.cfg
        self.db = engine.db
        self._latest_posted: dict[str, tuple] = {}

    # ------------------------------------------------------------------ utils
    def _asof(self, as_of_date: str) -> str:
        d = (as_of_date or "").strip().lower()
        if d in ("", "today", "now", "current"):
            return dt.date.today().isoformat()
        return _iso(d).isoformat()

    def _bu(self, business_unit: str) -> str:
        return ((business_unit or "").strip()
                or self.e.effective_defaults()["business_unit"])

    def _bucket_edges(self) -> list[int]:
        edges = sorted(int(x) for x in (self.cfg.defaults.aging_buckets or [30, 60, 90]))
        if not edges or edges[0] <= 0:
            raise ARError("defaults.aging_buckets must be ascending positive day counts")
        return edges

    def _bucket_labels(self, edges: list[int]) -> list[str]:
        labels, lo = ["current"], 1
        for e in edges:
            labels.append(f"{lo}-{e}")
            lo = e + 1
        labels.append(f"over_{edges[-1]}")
        return labels

    def _bucket_of(self, days: int, edges: list[int], labels: list[str]) -> str:
        if days <= 0:
            return labels[0]
        for i, e in enumerate(edges):
            if days <= e:
                return labels[i + 1]
        return labels[-1]

    def _bu_exists(self, bu: str) -> bool:
        p = self.db.prefix
        rows, _ = self.db.query(
            self.db.exists_sql(
                f"SELECT 1 FROM {p}PS_BUS_UNIT_TBL_GL WHERE BUSINESS_UNIT = :bu"
            ),
            {"bu": bu}, max_rows=1,
        )
        return bool(rows)

    def _latest_posted_period(self, bu: str) -> tuple[int, int, Optional[str]]:
        """(fy, period, period_end_date) of the newest regular period with
        posted rows in the default ledger — the only boundary a current-state
        subledger can honestly be reconciled at."""
        if bu in self._latest_posted:
            return self._latest_posted[bu]
        p = self.db.prefix
        led = self.cfg.defaults.ledger
        rows, _ = self.db.query(
            f"SELECT MAX(FISCAL_YEAR) AS fy FROM {p}PS_LEDGER "
            "WHERE BUSINESS_UNIT = :bu AND LEDGER = :led "
            "AND ACCOUNTING_PERIOD BETWEEN 1 AND 12",
            {"bu": bu, "led": led}, max_rows=1,
        )
        fy = int(rows[0]["fy"]) if rows and rows[0]["fy"] is not None else 0
        if not fy:
            self._latest_posted[bu] = (0, 0, None)
            return self._latest_posted[bu]
        rows, _ = self.db.query(
            f"SELECT MAX(ACCOUNTING_PERIOD) AS p FROM {p}PS_LEDGER "
            "WHERE BUSINESS_UNIT = :bu AND LEDGER = :led AND FISCAL_YEAR = :fy "
            "AND ACCOUNTING_PERIOD BETWEEN 1 AND 12",
            {"bu": bu, "led": led, "fy": fy}, max_rows=1,
        )
        per = int(rows[0]["p"]) if rows and rows[0]["p"] is not None else 0
        end_dt = None
        try:
            cal = self.e.list_periods(fy)["periods"]
            end_dt = next((str(x["end_dt"])[:10] for x in cal
                           if int(x["period"]) == per), None)
        except Exception:
            pass
        self._latest_posted[bu] = (fy, per, end_dt)
        return self._latest_posted[bu]

    # ------------------------------------------------------------ GL tie-out
    def _gl_tie(self, bu: str) -> dict:
        """Reconcile ALL current open items to the AR control through the
        latest posted period. Failures are reported, never swallowed — a tie
        that could not be evaluated must not read as a pass."""
        accounts = [str(a) for a in (self.cfg.defaults.ar_control_accounts or ["1100"])]
        p = self.db.prefix
        base = self.e.base_currency_for(bu) or "USD"
        rows, _ = self.db.query(
            f"SELECT BAL_CURRENCY AS currency, SUM(BAL_AMT) AS bal "
            f"FROM {p}PS_ITEM WHERE BUSINESS_UNIT = :bu AND ITEM_STATUS = 'O' "
            "GROUP BY BAL_CURRENCY",
            {"bu": bu}, max_rows=50,
        )
        # Convert at PERIOD-END rates: the GL side is the balance through the
        # latest posted period, so a rate that changed after period end must
        # not move the subledger side and fabricate a break.
        fy, per, end_dt = self._latest_posted_period(bu)
        rate_dt = end_dt or dt.date.today().isoformat()
        subledger, fx_cache = 0.0, {}
        try:
            for r in rows:
                rate = self._rate_to(r.get("currency"), base, rate_dt,
                                     fx_cache, base=base)
                subledger += float(r["bal"] or 0.0) * rate
        except (EngineError, ARError) as e:
            return {"evaluated": False, "control_accounts": accounts,
                    "reason": f"Cannot convert subledger to base currency "
                              f"{base}: {e}"}

        if not fy:
            return {"evaluated": False, "control_accounts": accounts,
                    "subledger_total": r2(subledger),
                    "reason": f"No posted ledger data for business unit {bu!r} — "
                              "nothing to reconcile against."}
        gl_total, failures = 0.0, []
        for acct in accounts:
            try:
                ab = self.e.account_balance(acct, business_unit=bu,
                                            fiscal_year=fy, through_period=per)
                gl_total += ab["ending_through_period"]
            except EngineError as e:
                failures.append(f"{acct}: {e}")
        if failures:
            return {"evaluated": False, "control_accounts": accounts,
                    "subledger_total": r2(subledger),
                    "reason": "Control-account lookup failed — " + "; ".join(failures)}
        if abs(subledger) < 0.005 and abs(gl_total) < 0.005:
            return {"evaluated": False, "control_accounts": accounts,
                    "subledger_total": 0.0, "gl_balance": 0.0,
                    "reason": "Both sides are zero — no open items and no control "
                              "balance. Check the business unit before calling "
                              "this reconciled."}
        diff = r2(subledger - gl_total)
        ties = abs(diff) < 0.01
        return {
            "evaluated": True,
            "control_accounts": accounts,
            "basis": (f"all current open items (converted to base {base} at "
                      f"{rate_dt} rates) vs GL through FY{fy} P{per} "
                      "(latest posted period)"),
            "gl_balance": r2(gl_total),
            "subledger_total": r2(subledger),
            "difference": diff,
            "ties": ties,
            "note": (
                "Open items reconcile to the GL AR control." if ties else
                "Difference may be items or payments posted to one side and "
                "not yet the other (in-transit), direct journals to the "
                "control account, or foreign-currency items whose booked GL "
                "rate differs from the period-end rate (unrevalued FX)."
            ),
        }

    # ------------------------------------------------------------------ aging
    def _summary_sql(self, edges: list[int], labels: list[str],
                     cust_filter: bool) -> str:
        p = self.db.prefix
        dd = self.db.days_past_expr("COALESCE(I.DUE_DT, I.ACCTG_DT)", "asof")
        cases = [f"SUM(CASE WHEN {dd} <= 0 THEN I.BAL_AMT ELSE 0 END) AS b0"]
        lo = 1
        for i, e in enumerate(edges, start=1):
            cases.append(
                f"SUM(CASE WHEN {dd} BETWEEN {lo} AND {int(e)} "
                f"THEN I.BAL_AMT ELSE 0 END) AS b{i}"
            )
            lo = int(e) + 1
        cases.append(
            f"SUM(CASE WHEN {dd} > {int(edges[-1])} THEN I.BAL_AMT ELSE 0 END) "
            f"AS b{len(edges) + 1}"
        )
        dispute = _NONBLANK.format(col="I.DISPUTE_STATUS")
        cust_clause = " AND I.CUST_ID = :cust" if cust_filter else ""
        return f"""SELECT I.CUST_ID AS cust_id, C.NAME1 AS name,
       C.CUST_STATUS AS cust_status, I.BAL_CURRENCY AS currency,
       {', '.join(cases)},
       SUM(I.BAL_AMT) AS total,
       SUM(CASE WHEN I.BAL_AMT < 0 THEN I.BAL_AMT ELSE 0 END) AS credit_amt,
       SUM(CASE WHEN {dispute} THEN I.BAL_AMT ELSE 0 END) AS disputed_amt,
       MAX(CASE WHEN {dd} > 0 THEN {dd} ELSE 0 END) AS oldest_days,
       COUNT(*) AS item_count
  FROM {p}PS_ITEM I
  LEFT JOIN {p}PS_CUSTOMER C ON C.SETID = :setid AND C.CUST_ID = I.CUST_ID
 WHERE I.BUSINESS_UNIT = :bu
   AND I.ITEM_STATUS = 'O'
   AND I.ACCTG_DT <= {self.db.date_bind('asof')}{cust_clause}
 GROUP BY I.CUST_ID, C.NAME1, C.CUST_STATUS, I.BAL_CURRENCY"""

    def _detail_sql(self, cust_filter: bool) -> str:
        p = self.db.prefix
        cust_clause = " AND I.CUST_ID = :cust" if cust_filter else ""
        return f"""SELECT I.CUST_ID AS cust_id, I.ITEM AS item, I.BAL_AMT AS bal_amt,
       I.BAL_CURRENCY AS currency,
       I.ACCTG_DT AS acctg_dt, I.DUE_DT AS due_dt, I.DISPUTE_STATUS AS dispute
  FROM {p}PS_ITEM I
 WHERE I.BUSINESS_UNIT = :bu AND I.ITEM_STATUS = 'O'
   AND I.ACCTG_DT <= {self.db.date_bind('asof')}{cust_clause}
 ORDER BY I.CUST_ID, I.DUE_DT"""

    def _rate_to(self, cur: str, disp: str, asof: str,
                 cache: dict, base: str = "") -> float:
        """Server-side conversion rate; raises (fail closed) when missing —
        mixed currencies are NEVER silently summed. A blank BAL_CURRENCY means
        the item is in the BU base currency — it must still be converted when
        the display currency differs, never passed through at 1.0."""
        cur = (cur or "").upper() or (base or "").upper() or disp
        if cur == disp:
            return 1.0
        if cur not in cache:
            try:
                fx = self.e.exchange_rate(cur, disp, as_of_date=asof)
            except EngineError as e:
                raise ARError(
                    f"Cannot express AR in {disp}: {e} "
                    "Amounts in different currencies are never summed "
                    "without a rate."
                ) from e
            cache[cur] = (fx["rate"],
                          f"{cur}->{disp} @ {fx['rate']}"
                          + (f" via {fx['cross_via']}" if fx.get("cross_via")
                             else ""))
        return cache[cur][0]

    def aging(
        self,
        business_unit: str = "",
        as_of_date: str = "",
        customer_id: str = "",
        detail: bool = False,
        display_currency: str = "",
    ) -> dict:
        bu = self._bu(business_unit)
        asof = self._asof(as_of_date)
        asof_d = _iso(asof)
        edges = self._bucket_edges()
        labels = self._bucket_labels(edges)
        setid = self.e.resolve_setid(bu, "CUSTOMER")
        cust = (customer_id or "").strip()

        params: dict = {"bu": bu, "setid": setid, "asof": asof}
        if cust:
            params["cust"] = cust
        rows, _ = self.db.query(self._summary_sql(edges, labels, bool(cust)),
                                params, max_rows=10_000)
        if not rows and not self._bu_exists(bu):
            known, _ = self.db.query(
                f"SELECT BUSINESS_UNIT AS bu FROM {self.db.prefix}PS_BUS_UNIT_TBL_GL "
                "ORDER BY BUSINESS_UNIT", {}, max_rows=25)
            return {"scope_status": "business_unit_not_found",
                    "detail": f"Business unit {bu!r} does not exist.",
                    "known_business_units": [r["bu"] for r in known],
                    "customers": [], "note": "NO DATA — not an empty aging."}

        # One SQL row per (customer, currency); convert server-side into the
        # display currency and merge — an EUR and a USD item are never added
        # raw, and a missing rate aborts rather than mis-summing.
        base = self.e.base_currency_for(bu) or "USD"
        disp = (display_currency or "").strip().upper() or base
        fx_cache: dict = {}
        by_cust: dict[str, dict] = {}
        for r in rows:
            rate = self._rate_to(r.get("currency"), disp, asof, fx_cache,
                                 base=base)
            c = by_cust.setdefault(r["cust_id"], {
                "cust_id": r["cust_id"], "name": r.get("name"),
                "customer_status": r.get("cust_status"),
                **{lb: 0.0 for lb in labels},
                "total": 0.0, "credit_amt": 0.0, "disputed_amt": 0.0,
                "oldest_days_past_due": 0, "item_count": 0,
                "currencies": set(),
            })
            for i, lb in enumerate(labels):
                c[lb] += float(r.get(f"b{i}") or 0) * rate
            c["total"] += float(r.get("total") or 0) * rate
            c["credit_amt"] += float(r.get("credit_amt") or 0) * rate
            c["disputed_amt"] += float(r.get("disputed_amt") or 0) * rate
            c["oldest_days_past_due"] = max(c["oldest_days_past_due"],
                                            int(r.get("oldest_days") or 0))
            c["item_count"] += int(r.get("item_count") or 0)
            c["currencies"].add((r.get("currency") or "").upper() or base)
        customers = []
        for c in by_cust.values():
            # Round buckets first, then derive the total FROM the rounded
            # buckets — rounding each independently after an FX multiply can
            # leave bucket sums a cent off the total.
            for lb in labels + ["credit_amt", "disputed_amt"]:
                c[lb] = r2(c[lb])
            c["total"] = r2(sum(c[lb] for lb in labels))
            c["currencies"] = sorted(c.pop("currencies"))
            customers.append(c)
        customers.sort(key=lambda c: -c["total"])
        totals = {lb: r2(sum(c[lb] for c in customers)) for lb in labels}
        totals["total"] = r2(sum(totals[lb] for lb in labels))
        for k in ("credit_amt", "disputed_amt"):
            totals[k] = r2(sum(c[k] for c in customers))

        out = {
            "business_unit": bu,
            "as_of": asof,
            "display_currency": disp,
            "buckets": labels,
            "customers": customers,
            "totals": totals,
            "gl_tie": self._gl_tie(bu),
            "note": (
                f"All amounts converted server-side to {disp}. Positive = owed "
                "by the customer; negative = credit memo or on-account receipt. "
                "Buckets by days past DUE_DT (ACCTG_DT when no due date) at the "
                "as-of date."
            ),
        }
        if any(len(c["currencies"]) > 1 or c["currencies"] != [disp]
               for c in customers):
            out["fx_applied"] = sorted(n for _, n in fx_cache.values())

        _fy, _per, latest_end = self._latest_posted_period(bu)
        if latest_end and asof < latest_end:
            out["historical_approximation"] = True
            out["warning"] = (
                "PS_ITEM is current-state: a backdated as-of shows today's open "
                "items entered on or before that date, NOT the book as it stood "
                "then — items closed since are missing. Treat buckets as an "
                "approximation; true historical aging needs PS_ITEM_ACTIVITY "
                "reconstruction."
            )

        if detail or cust:
            items, truncated = self.db.query(self._detail_sql(bool(cust)),
                                             params, max_rows=DETAIL_ROW_CAP)
            det = []
            for r in items:
                basis = _iso_opt(r["due_dt"]) or _iso_opt(r["acctg_dt"])
                days = (asof_d - basis).days if basis else 0
                cur = (r.get("currency") or "").upper() or base
                rate = self._rate_to(cur, disp, asof, fx_cache, base=base)
                d = {
                    "cust_id": r["cust_id"], "item": r["item"],
                    "balance": r2(float(r["bal_amt"] or 0) * rate),
                    "currency": disp,
                    **({"original": r2(float(r["bal_amt"] or 0)),
                        "original_currency": cur} if cur != disp else {}),
                    "due_dt": r["due_dt"],
                    "days_past_due": max(days, 0),
                    "bucket": self._bucket_of(days, edges, labels),
                    "dispute": (str(r.get("dispute") or "").strip() or None),
                }
                if _iso_opt(r["due_dt"]) is None:
                    d["no_due_date"] = True
                det.append(d)
            out["items"] = det
            if truncated:
                out["items_truncated"] = True
                out["items_note"] = (
                    f"Only the first {DETAIL_ROW_CAP} items shown; the customer "
                    "summary above covers the full population."
                )
        return out

    def customer(self, customer: str, business_unit: str = "",
                 as_of_date: str = "", display_currency: str = "") -> dict:
        c = (customer or "").strip()
        if not c:
            raise ARError("customer is required (an ID like C1001, or a name)")
        bu = self._bu(business_unit)
        matches = self.search_customers(c, limit=5)["customers"]
        exact = [m for m in matches if m["cust_id"].upper() == c.upper()]
        if exact:
            cust_id = exact[0]["cust_id"]
        elif len(matches) == 1:
            cust_id = matches[0]["cust_id"]
        elif not matches:
            raise ARError(f"No customer matches {customer!r} — try search_customers")
        else:
            return {"multiple_matches": matches,
                    "note": "Ask the user which customer they mean, then call "
                            "again with the exact cust_id."}
        result = self.aging(business_unit=bu, as_of_date=as_of_date,
                            customer_id=cust_id,
                            display_currency=display_currency)
        me = next((x for x in result.get("customers", [])
                   if x["cust_id"] == cust_id), None)
        out = {
            "business_unit": bu,
            "as_of": result["as_of"],
            "customer": me or {"cust_id": cust_id, "total": 0.0,
                               "note": "no open items"},
            "buckets": result["buckets"],
            "items": result.get("items", []),
            "gl_tie": result["gl_tie"],
            "note": result["note"],
        }
        out["display_currency"] = result.get("display_currency")
        for k in ("historical_approximation", "warning", "fx_applied"):
            if k in result:
                out[k] = result[k]
        return out

    def search_customers(self, query: str = "", limit: int = 25) -> dict:
        bu = self._bu("")
        setid = self.e.resolve_setid(bu, "CUSTOMER")
        p = self.db.prefix
        params = {
            "setid": setid, "bu": bu,
            "q": f"%{(query or '').upper()}%",
            "qa": f"{(query or '').strip().upper()}%",
        }
        sql = f"""SELECT C.CUST_ID AS cust_id, C.NAME1 AS name,
       C.CUST_STATUS AS status, COALESCE(B.bal, 0) AS open_balance
  FROM {p}PS_CUSTOMER C
  LEFT JOIN (SELECT CUST_ID, SUM(BAL_AMT) AS bal FROM {p}PS_ITEM
              WHERE BUSINESS_UNIT = :bu AND ITEM_STATUS = 'O'
              GROUP BY CUST_ID) B ON B.CUST_ID = C.CUST_ID
 WHERE C.SETID = :setid
   AND (UPPER(C.NAME1) LIKE :q OR UPPER(C.CUST_ID) LIKE :qa)
 ORDER BY C.CUST_ID"""
        rows, truncated = self.db.query(sql, params, max_rows=max(int(limit or 25), 1))
        for r in rows:
            r["open_balance"] = r2(r["open_balance"])
        return {"customers": rows, "count": len(rows), "truncated": truncated,
                "note": "status A=active, I=inactive; open_balance sums open items"}

    def top_billing_customers(self, business_unit: str = "", n: int = 10,
                              months: int = 12, as_of_date: str = "",
                              display_currency: str = "") -> dict:
        """Top customers by FINALIZED billing (BILL_STATUS='INV') over a
        trailing window. Groups by customer AND currency — mixed currencies are
        never silently summed; pass display_currency to convert server-side."""
        bu = self._bu(business_unit)
        asof = self._asof(as_of_date)
        since = (_iso(asof) - dt.timedelta(days=max(int(months or 12), 1) * 30)
                 ).isoformat()
        p = self.db.prefix
        setid = self.e.resolve_setid(bu, "CUSTOMER")
        rows, _ = self.db.query(
            f"""SELECT H.BILL_TO_CUST_ID AS cust_id, C.NAME1 AS name,
       H.BI_CURRENCY_CD AS currency, COUNT(*) AS invoices,
       SUM(H.INVOICE_AMOUNT) AS billed
  FROM {p}PS_BI_HDR H
  LEFT JOIN {p}PS_CUSTOMER C ON C.SETID = :setid AND C.CUST_ID = H.BILL_TO_CUST_ID
 WHERE H.BUSINESS_UNIT = :bu AND H.BILL_STATUS = 'INV'
   AND H.INVOICE_DT >= {self.db.date_bind('since')}
 GROUP BY H.BILL_TO_CUST_ID, C.NAME1, H.BI_CURRENCY_CD""",
            {"bu": bu, "setid": setid, "since": since}, max_rows=10_000,
        )
        base = self.e.base_currency_for(bu) or "USD"
        # A blank BI_CURRENCY_CD means the invoice is in the BU base currency
        # — normalize it so mixed-currency detection and conversion see it.
        currencies = sorted({(r["currency"] or "").upper() or base for r in rows})
        disp = (display_currency or "").strip().upper()
        mixed = len(currencies) > 1
        fx_notes = []
        by_cust: dict[str, dict] = {}
        for r in rows:
            amt = float(r["billed"] or 0)
            cur = (r["currency"] or "").upper() or base
            if disp and cur != disp:
                fx = self.e.exchange_rate(cur, disp, as_of_date=asof)
                amt = amt * fx["rate"]
                note = f"{cur}->{disp} @ {fx['rate']}"
                if note not in fx_notes:
                    fx_notes.append(note)
            c = by_cust.setdefault(r["cust_id"], {
                "cust_id": r["cust_id"], "name": r.get("name"),
                "invoices": 0, "billed": 0.0, "currencies": set(),
            })
            c["invoices"] += int(r["invoices"] or 0)
            c["billed"] += amt
            c["currencies"].add(disp or cur)
        if mixed and not disp:
            return {
                "business_unit": bu, "window_months": int(months or 12),
                "since": since, "as_of": asof,
                "mixed_currencies": currencies,
                "by_currency": [
                    {"cust_id": r["cust_id"], "name": r.get("name"),
                     "currency": r["currency"], "invoices": int(r["invoices"] or 0),
                     "billed": r2(float(r["billed"] or 0))}
                    for r in sorted(rows, key=lambda x: -float(x["billed"] or 0))
                ],
                "note": (
                    "Invoices exist in multiple currencies; totals are NOT "
                    "summed across currencies. Pass display_currency (e.g. "
                    "'USD' or 'INR') to rank on converted totals."
                ),
            }
        ranked = sorted(by_cust.values(), key=lambda c: -c["billed"])
        total = sum(c["billed"] for c in ranked) or 1.0
        top = []
        for c in ranked[: max(int(n or 10), 1)]:
            top.append({
                "cust_id": c["cust_id"], "name": c["name"],
                "invoices": c["invoices"], "billed": r2(c["billed"]),
                "share_pct": r2(c["billed"] / total * 100),
                "currency": disp or (currencies[0] if currencies else ""),
            })
        out = {
            "business_unit": bu, "window_months": int(months or 12),
            "since": since, "as_of": asof,
            "currency": disp or (currencies[0] if currencies else ""),
            "customers": top,
            "total_billed": r2(sum(c["billed"] for c in ranked)),
            "customer_count": len(ranked),
            "note": ("Finalized (INV) invoices only, by invoice date window. "
                     "This is BILLING volume, not open AR — see get_ar_aging "
                     "for what is owed."),
        }
        if fx_notes:
            out["fx_applied"] = fx_notes
            out["fx_note"] = ("Converted server-side at effective-dated "
                              "PS_RT_RATE_TBL rates — copy figures verbatim.")
        return out

    # ---------------------------------------------------------------- billing
    def billing_workbench(self, business_unit: str = "", days_stuck: int = 5,
                          as_of_date: str = "", lookback_days: int = 365) -> dict:
        bu = self._bu(business_unit)
        asof = self._asof(as_of_date)
        asof_d = _iso(asof)
        p = self.db.prefix

        rows, _ = self.db.query(
            f"""SELECT BILL_STATUS AS status, COUNT(*) AS n,
       SUM(INVOICE_AMOUNT) AS amount
  FROM {p}PS_BI_HDR WHERE BUSINESS_UNIT = :bu GROUP BY BILL_STATUS""",
            {"bu": bu}, max_rows=25,
        )
        statuses = [
            {**r, "amount": r2(r["amount"] or 0),
             "descr": BILL_STATUS_DESCR.get(r["status"], r["status"])}
            for r in rows
        ]

        pend, pend_trunc = self.db.query(
            f"""SELECT INVOICE AS invoice, BILL_STATUS AS status,
       BILL_TO_CUST_ID AS cust_id, INVOICE_DT AS invoice_dt,
       INVOICE_AMOUNT AS amount, BILL_SOURCE_ID AS source
  FROM {p}PS_BI_HDR
 WHERE BUSINESS_UNIT = :bu
   AND BILL_STATUS IN ({", ".join("'" + s + "'" for s in NOT_FINAL)})
 ORDER BY INVOICE_DT""",
            {"bu": bu}, max_rows=1_000,
        )
        stuck = []
        for r in pend:
            inv_d = _iso_opt(r["invoice_dt"])
            days = (asof_d - inv_d).days if inv_d else 0
            if days >= max(int(days_stuck or 5), 0):
                entry = {**r, "amount": r2(r["amount"] or 0),
                         "days_pending": days,
                         "status_descr": BILL_STATUS_DESCR.get(r["status"],
                                                               r["status"])}
                if inv_d is None:
                    entry["no_invoice_date"] = True
                stuck.append(entry)

        intfc, _ = self.db.query(
            f"""SELECT LOAD_STATUS_BI AS status, COUNT(*) AS n
  FROM {p}PS_INTFC_BI WHERE BUSINESS_UNIT = :bu GROUP BY LOAD_STATUS_BI""",
            {"bu": bu}, max_rows=10,
        )
        interface = [
            {**r, "descr": LOAD_STATUS_DESCR.get(r["status"], r["status"])}
            for r in intfc
        ]
        errs, _ = self.db.query(
            f"""SELECT INTFC_ID AS intfc_id, INTFC_LINE_NUM AS line,
       BILL_TO_CUST_ID AS cust_id, BILL_SOURCE_ID AS source
  FROM {p}PS_INTFC_BI
 WHERE BUSINESS_UNIT = :bu AND LOAD_STATUS_BI = 'ERR'
 ORDER BY INTFC_ID, INTFC_LINE_NUM""",
            {"bu": bu}, max_rows=100,
        )

        # Date-floored: an unfloored NOT EXISTS over all finalized history is a
        # full-history anti-join on a real PS_BI_HDR.
        since = (asof_d - dt.timedelta(days=max(int(lookback_days or 365), 1))
                 ).isoformat()
        orphans, orph_trunc = self.db.query(
            f"""SELECT H.INVOICE AS invoice, H.BILL_TO_CUST_ID AS cust_id,
       H.INVOICE_DT AS invoice_dt, H.INVOICE_AMOUNT AS amount
  FROM {p}PS_BI_HDR H
 WHERE H.BUSINESS_UNIT = :bu AND H.BILL_STATUS = 'INV'
   AND H.INVOICE_DT >= {self.db.date_bind('since')}
   AND NOT EXISTS (SELECT 1 FROM {p}PS_ITEM I
                    WHERE I.BUSINESS_UNIT = H.BUSINESS_UNIT
                      AND I.ITEM = H.INVOICE)
 ORDER BY H.INVOICE_DT""",
            {"bu": bu, "since": since}, max_rows=50,
        )
        for o in orphans:
            o["amount"] = r2(o["amount"] or 0)

        issues = []
        if stuck:
            issues.append(f"{len(stuck)} invoice(s) pending longer than "
                          f"{days_stuck} days")
        if errs:
            issues.append(f"{len(errs)} billing-interface line(s) in error")
        if orphans:
            issues.append(f"{len(orphans)} finalized invoice(s) not loaded to AR "
                          f"(last {lookback_days} days)")
        out = {
            "business_unit": bu,
            "as_of": asof,
            "statuses": statuses,
            "stuck_invoices": stuck,
            "interface": interface,
            "interface_errors": errs,
            "finalized_not_in_ar": orphans,
            "lookback_days": int(lookback_days or 365),
            "issues": issues,
            "control_status": "exceptions_found" if issues else "passed",
            "note": (
                "Lifecycle: NEW/PND -> HLD -> RDY -> INV (finalized) or CAN. "
                "'Finalized not in AR' means the invoice exists in Billing but "
                "has no open-item row — the AR update has not run or failed."
            ),
        }
        if pend_trunc or orph_trunc:
            out["truncated"] = True
        return out
