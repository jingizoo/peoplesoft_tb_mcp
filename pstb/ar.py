"""AR aging and Billing pipeline over PS_ITEM / PS_CUSTOMER / PS_BI_HDR / INTFC_BI.

Semantics (deliberate, reviewed):
  - PS_ITEM.BAL_AMT is the open receivable: positive = the customer owes it,
    negative = a credit memo or on-account/unapplied receipt.
  - PS_ITEM is a CURRENT-STATE record. A backdated as-of date therefore does
    NOT reconstruct history — items closed since then are gone, partial
    payments show today's residual. Such results are labeled an approximation;
    true historical aging needs PS_ITEM_ACTIVITY reconstruction (future work).
  - Aging buckets classify by days past COALESCE(DUE_DT, <item date>) at the
    as-of date; items count when <item date> <= as-of. The item-date column
    VARIES BY SITE (ACCTG_DT on some releases, ASOF_DT on others), so the
    PS_ITEM shape is introspected at runtime and every adaptation is
    disclosed in record_notes — curated SQL adapts to the database in front
    of it, never assumes the reference layout.
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

from . import queries as q
from .db import DbError
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

# "Column has a non-blank value", true on both dialects.
# NOT `COALESCE(TRIM(x),'') <> ''`: on Oracle the literal '' IS NULL, so that
# predicate is UNKNOWN for every row and disputed amounts silently read 0.00.
# LENGTH(TRIM(x)) > 0 is UNKNOWN only for genuinely blank/NULL values, which
# is the intended answer for them.
_NONBLANK = "LENGTH(TRIM({col})) > 0"


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


def _families(customers: list, grand_total: float) -> list:
    """Group the aging by the corporate hierarchy the system recorded.

    This is the whole reason a relationship layer earns its place in an
    answer nobody asked a relationship question about. Ask for an aging and
    ACME Industrial, ACME Industrial - West and ACME Industrial - Components
    come back at ranks 1, 8 and 9 as three strangers; the fact that they are
    one company owing 39% of the receivable is not on the screen anywhere,
    and no amount of reading the rows produces it.

    Grouping is on CORPORATE_CUST_ID and nothing else. Names are not
    compared: "ACME Logistics Group" is a different company and folding it
    in would invent a consolidation nobody approved — a wrong combined
    exposure looks exactly as authoritative as a right one.

    Pure arithmetic over rows already in hand. No query, no database access,
    no second pass over PS_ITEM.
    """
    groups: dict = {}
    for c in customers:
        parent = c.get("corporate_parent") or ""
        if not parent or parent == c["cust_id"]:
            continue                      # its own parent: not a family
        groups.setdefault(parent, []).append(c)
    out = []
    for parent, members in groups.items():
        # The parent itself is a customer in its own right and may or may
        # not have open items; include it only when this aging shows it.
        head = next((c for c in customers if c["cust_id"] == parent), None)
        rows = ([head] if head is not None else []) + [
            m for m in members if m["cust_id"] != parent]
        if len(rows) < 2:
            continue                      # a family of one is just a customer
        combined = r2(sum(c["total"] for c in rows))
        out.append({
            "corporate_parent": parent,
            "name": (head or rows[0]).get("name") or parent,
            "members": [{"cust_id": c["cust_id"], "name": c.get("name"),
                         "total": c["total"],
                         "disputed_amt": c.get("disputed_amt", 0.0)}
                        for c in sorted(rows, key=lambda x: -x["total"])],
            "member_count": len(rows),
            "combined_total": combined,
            "combined_disputed": r2(sum(c.get("disputed_amt", 0.0)
                                        for c in rows)),
            "share_of_ar_pct": (r2(combined / grand_total * 100.0)
                                if grand_total else None),
            "largest_member_share_pct": (
                r2(max(c["total"] for c in rows) / combined * 100.0)
                if combined else None),
        })
    out.sort(key=lambda f: -abs(f["combined_total"]))
    return out[:10]


class ARBilling:
    def __init__(self, engine: TBEngine):
        self.e = engine
        self.cfg = engine.cfg
        self.db = engine.db
        self._latest_posted: dict[str, tuple] = {}
        self._colcache: dict[str, set] = {}
        self._shapes: dict[str, dict] = {}

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
        posted rows in the BU's resolved ledger — the only boundary a
        current-state subledger can honestly be reconciled at."""
        if bu in self._latest_posted:
            return self._latest_posted[bu]
        # Resolve the ledger for this BU rather than copying the configured
        # default BU's ledger. TBEngine also applies the site's configured
        # adjustment-period exclusions, so legitimate period 13 remains a
        # regular period and the AR-to-GL comparison uses the correct cutoff.
        led = self.e.resolve_ledger_for(bu)
        fy, per = self.e.last_posted_period(bu, led)
        if not fy:
            self._latest_posted[bu] = (0, 0, None)
            return self._latest_posted[bu]
        end_dt = None
        try:
            cal = self.e.list_periods(fy)["periods"]
            end_dt = next((str(x["end_dt"])[:10] for x in cal
                           if int(x["period"]) == per), None)
        except Exception:
            pass
        self._latest_posted[bu] = (fy, per, end_dt)
        return self._latest_posted[bu]

    # -------------------------------------------------- record-shape adaption
    _ITEM_REQUIRED = ("BUSINESS_UNIT", "CUST_ID", "ITEM", "ITEM_STATUS",
                      "BAL_AMT")

    def _cols(self, table: str) -> set:
        """Column names a record ACTUALLY has at this site — the shared
        db-level catalog (one cache, one mechanism, for every module)."""
        return self.db.columns(table)

    def _customer_shape(self) -> dict:
        """Which optional PS_CUSTOMER columns exist here.

        NAME1 is treated in two different ways on purpose. As a DISPLAY label
        it degrades to NULL like any other decoration. As the SEARCH PREDICATE
        in search_customers it must not: `UPPER(NULL) LIKE :q` matches nothing,
        so a site without NAME1 would be told "no such customer" about
        customers that plainly exist. Absent there, name search is withdrawn
        and said so, and the ID search still runs.
        """
        if "PS_CUSTOMER" in self._shapes:
            return self._shapes["PS_CUSTOMER"]
        cols = self._cols("PS_CUSTOMER")
        shape = {
            "name": "NAME1" if (not cols or "NAME1" in cols) else "",
            "status": "CUST_STATUS" if (not cols or "CUST_STATUS" in cols) else "",
            # The corporate hierarchy the site records. Optional because
            # plenty of installations never populate it — and where it is
            # absent the honest answer is a flat list, never a family
            # guessed from names.
            "corp": ("CORPORATE_CUST_ID"
                     if (not cols or "CORPORATE_CUST_ID" in cols) else ""),
            "notes": [],
        }
        if cols and not shape["name"]:
            shape["notes"].append(
                "PS_CUSTOMER here has no NAME1; customers are identified by ID "
                "only and searching by customer NAME is not available.")
        if cols and not shape["status"]:
            shape["notes"].append(
                "PS_CUSTOMER here has no CUST_STATUS; active/inactive is not "
                "reported.")
        if cols and not shape["corp"]:
            shape["notes"].append(
                "PS_CUSTOMER here has no CORPORATE_CUST_ID; this site does "
                "not record a corporate hierarchy, so customers that belong "
                "to the same group are listed separately and no combined "
                "exposure is reported.")
        if cols:
            self._shapes["PS_CUSTOMER"] = shape
        return shape

    def _item_shape(self) -> dict:
        """Which optional PS_ITEM columns exist here, with fallbacks: item
        date ACCTG_DT -> ASOF_DT -> due-date-only aging; DISPUTE_STATUS and
        BAL_CURRENCY may be absent. Every adaptation becomes a record_note."""
        if "PS_ITEM" in self._shapes:
            return self._shapes["PS_ITEM"]
        cols = self._cols("PS_ITEM")
        if not cols:
            # Not cached: if introspection heals on a later call, adapt then.
            return {"date": "ACCTG_DT", "due": "DUE_DT",
                    "dispute": "DISPUTE_STATUS", "currency": "BAL_CURRENCY",
                    "notes": ["Could not read PS_ITEM's column list "
                              "(permissions?); assuming the reference shape."]}
        missing = [c for c in self._ITEM_REQUIRED if c not in cols]
        if missing:
            raise ARError(
                f"PS_ITEM at this site is missing required column(s) "
                f"{', '.join(missing)} — AR tools cannot run against it. "
                "Check that db.schema in config.yaml names the record owner "
                "(usually SYSADM) and run python scripts/diagnose_db.py."
            )
        notes: list[str] = []
        date_c = next((c for c in ("ACCTG_DT", "ASOF_DT") if c in cols), "")
        due_c = "DUE_DT" if "DUE_DT" in cols else ""
        if not date_c and not due_c:
            raise ARError(
                "PS_ITEM at this site has none of ACCTG_DT, ASOF_DT, DUE_DT — "
                "aging needs at least one date column. Run "
                "python scripts/diagnose_db.py to see the real shape."
            )
        if date_c != "ACCTG_DT":
            notes.append(
                f"PS_ITEM here has no ACCTG_DT; item dating uses "
                f"{date_c or 'DUE_DT only'}."
                + ("" if date_c else " The as-of cutoff cannot be applied — "
                   "items shown are the current open set, and items without "
                   "a due date are bucketed as current.")
            )
        if not due_c:
            notes.append(f"PS_ITEM here has no DUE_DT; buckets age by days "
                         f"since {date_c}.")
        dispute_c = "DISPUTE_STATUS" if "DISPUTE_STATUS" in cols else ""
        if not dispute_c:
            notes.append("PS_ITEM here has no DISPUTE_STATUS; dispute "
                         "amounts are not available.")
        cur_c = "BAL_CURRENCY" if "BAL_CURRENCY" in cols else ""
        if not cur_c:
            notes.append("PS_ITEM here has no BAL_CURRENCY; amounts are "
                         "assumed to be in the BU base currency.")
        shape = {"date": date_c, "due": due_c, "dispute": dispute_c,
                 "currency": cur_c, "notes": notes}
        self._shapes["PS_ITEM"] = shape
        return shape

    def _aging_basis(self, shape: dict) -> str:
        parts = [f"I.{c}" for c in (shape["due"], shape["date"]) if c]
        return f"COALESCE({', '.join(parts)})" if len(parts) > 1 else parts[0]

    # ------------------------------------------------------------ GL tie-out
    def _gl_tie(self, bu: str) -> dict:
        """Reconcile ALL current open items to the AR control through the
        latest posted period. Failures are reported, never swallowed — a tie
        that could not be evaluated must not read as a pass."""
        accounts = [str(a) for a in (self.cfg.defaults.ar_control_accounts or ["1100"])]
        p = self.db.prefix
        base = self.e.base_currency_for(bu) or "USD"
        shape = self._item_shape()
        if shape["currency"]:
            rows, _ = self.db.query(
                f"SELECT {shape['currency']} AS currency, SUM(BAL_AMT) AS bal "
                f"FROM {p}PS_ITEM WHERE BUSINESS_UNIT = :bu AND ITEM_STATUS = 'O' "
                f"GROUP BY {shape['currency']}",
                {"bu": bu}, max_rows=50,
            )
        else:
            one, _ = self.db.query(
                f"SELECT SUM(BAL_AMT) AS bal FROM {p}PS_ITEM "
                "WHERE BUSINESS_UNIT = :bu AND ITEM_STATUS = 'O'",
                {"bu": bu}, max_rows=1,
            )
            rows = [{"currency": "", "bal": one[0]["bal"] if one else 0.0}]
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
            # Name what DOES exist instead of dead-ending: unknown BU, an
            # unposted ledger, or the wrong ledger name each read identically
            # as "no ledger data" and sent users away with no next move.
            try:
                diag = self.e._scope_diagnosis(
                    bu, self.e.resolve_ledger_for(bu), 0)
            except Exception:
                diag = None
            return {"evaluated": False, "control_accounts": accounts,
                    "subledger_total": r2(subledger),
                    "reason": f"No posted ledger data for business unit {bu!r} — "
                              "nothing to reconcile against.",
                    **({"scope_diagnosis": diag} if diag else {})}
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
                     cust_filter: bool, shape: dict) -> str:
        p = self.db.prefix
        dd = self.db.days_past_expr(self._aging_basis(shape), "asof")
        # A NULL aging basis (item with no usable date) must land in a bucket,
        # not silently fall out of every CASE and vanish from the totals.
        cases = [f"SUM(CASE WHEN {dd} <= 0 OR ({dd}) IS NULL "
                 "THEN I.BAL_AMT ELSE 0 END) AS b0"]
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
        if shape["dispute"]:
            dispute = _NONBLANK.format(col=f"I.{shape['dispute']}")
            disp_term = (f"SUM(CASE WHEN {dispute} THEN I.BAL_AMT ELSE 0 END) "
                         "AS disputed_amt")
        else:
            disp_term = "SUM(0) AS disputed_amt"
        cur_sel = (f"I.{shape['currency']} AS currency" if shape["currency"]
                   else "'' AS currency")
        group_cur = f", I.{shape['currency']}" if shape["currency"] else ""
        asof_cut = (f"\n   AND I.{shape['date']} <= {self.db.date_bind('asof')}"
                    if shape["date"] else "")
        cust_clause = " AND I.CUST_ID = :cust" if cust_filter else ""
        cs = self._customer_shape()
        c_name = f"C.{cs['name']} AS name" if cs["name"] else "NULL AS name"
        c_stat = (f"C.{cs['status']} AS cust_status" if cs["status"]
                  else "NULL AS cust_status")
        # Free: the LEFT JOIN to PS_CUSTOMER is already in this statement,
        # so the corporate parent costs one column, not one query.
        c_corp = (f"C.{cs['corp']} AS corporate_parent" if cs["corp"]
                  else "NULL AS corporate_parent")
        c_group = "".join(f", C.{c}" for c in
                          (cs["name"], cs["status"], cs["corp"]) if c)
        return f"""SELECT I.CUST_ID AS cust_id, {c_name},
       {c_stat}, {c_corp}, {cur_sel},
       {', '.join(cases)},
       SUM(I.BAL_AMT) AS total,
       SUM(CASE WHEN I.BAL_AMT < 0 THEN I.BAL_AMT ELSE 0 END) AS credit_amt,
       {disp_term},
       MAX(CASE WHEN {dd} > 0 THEN {dd} ELSE 0 END) AS oldest_days,
       COUNT(*) AS item_count
  FROM {p}PS_ITEM I
  LEFT JOIN {p}PS_CUSTOMER C ON C.SETID = :setid AND C.CUST_ID = I.CUST_ID
 WHERE I.BUSINESS_UNIT = :bu
   AND I.ITEM_STATUS = 'O'{asof_cut}{cust_clause}
 GROUP BY I.CUST_ID{c_group}{group_cur}"""

    def _detail_sql(self, cust_filter: bool, shape: dict) -> str:
        p = self.db.prefix
        cust_clause = " AND I.CUST_ID = :cust" if cust_filter else ""
        date_sel = (f"I.{shape['date']} AS acctg_dt" if shape["date"]
                    else "NULL AS acctg_dt")
        due_sel = (f"I.{shape['due']} AS due_dt" if shape["due"]
                   else "NULL AS due_dt")
        cur_sel = (f"I.{shape['currency']} AS currency" if shape["currency"]
                   else "'' AS currency")
        disp_sel = (f"I.{shape['dispute']} AS dispute" if shape["dispute"]
                    else "NULL AS dispute")
        asof_cut = (f"\n   AND I.{shape['date']} <= {self.db.date_bind('asof')}"
                    if shape["date"] else "")
        order_c = shape["due"] or shape["date"]
        return f"""SELECT I.CUST_ID AS cust_id, I.ITEM AS item, I.BAL_AMT AS bal_amt,
       {cur_sel},
       {date_sel}, {due_sel}, {disp_sel}
  FROM {p}PS_ITEM I
 WHERE I.BUSINESS_UNIT = :bu AND I.ITEM_STATUS = 'O'{asof_cut}{cust_clause}
 ORDER BY I.CUST_ID, I.{order_c}"""

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

        shape = self._item_shape()
        params: dict = {"bu": bu, "setid": setid, "asof": asof}
        if cust:
            params["cust"] = cust
        rows, _ = self.db.query(
            self._summary_sql(edges, labels, bool(cust), shape),
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
                "corporate_parent": (str(r.get("corporate_parent"))
                                     if r.get("corporate_parent") else ""),
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

        # Declare the bucket shares rather than leaving the model to divide
        # in prose. "How much of our AR is overdue" is asked as a PERCENTAGE
        # far more often than as an amount, and a percentage the machinery
        # did not emit is one the answer guard cannot verify — so an
        # undeclared share is both an ungrounded figure and a missing
        # answer. Costs no query: these are the totals already computed.
        overdue = r2(sum(totals[lb] for lb in labels[1:]))
        base = totals["total"] or 0.0
        shares = ({lb: r2(totals[lb] / base * 100.0) for lb in labels}
                  if base else {})
        # Who is actually one company. Costs no query — the corporate
        # parent rode in on the join the summary already performs.
        families = _families(customers, totals["total"])
        out = {
            "business_unit": bu,
            "as_of": asof,
            "display_currency": disp,
            "buckets": labels,
            "customers": customers,
            "totals": totals,
            "overdue_total": overdue,
            "overdue_pct": r2(overdue / base * 100.0) if base else None,
            "bucket_share_pct": shares,
            "gl_tie": self._gl_tie(bu),
            "corporate_families": {
                "families": families,
                "basis": "PS_CUSTOMER.CORPORATE_CUST_ID — the hierarchy this "
                         "system records. Customers are never grouped by "
                         "name; a same-named company that is its own "
                         "corporate parent is a different company.",
                "note": ("Customers below that belong to one corporate "
                         "family, with their combined exposure. The rows in "
                         "'customers' are per legal entity and already sum "
                         "to the totals — these do NOT add to them."
                         if families else
                         "No customer in this aging shares a corporate "
                         "parent with another."),
            } if not cust else {"families": [], "note":
                                "Single-customer aging; no family rollup."},
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
        # PS_CUSTOMER's adaptations belong here too. A site with no
        # CORPORATE_CUST_ID gets a flat list either way; the difference
        # between "these customers are unrelated" and "this site does not
        # record who owns whom" is the whole value of saying it.
        notes = list(shape["notes"]) + list(
            self._customer_shape().get("notes") or [])
        if notes:
            out["record_notes"] = notes

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
            # Pass ONLY the binds the detail SQL actually references — a
            # stray bind name is a DPY-4008 on Oracle thin mode (sqlite
            # silently ignores extras, so tests here cannot catch it).
            dparams: dict = {"bu": bu}
            if cust:
                dparams["cust"] = cust
            if shape["date"]:
                dparams["asof"] = asof
            items, truncated = self.db.query(
                self._detail_sql(bool(cust), shape),
                dparams, max_rows=DETAIL_ROW_CAP)
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
        matches = self.search_customers(
            c, limit=5, business_unit=bu
        )["customers"]
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
        for k in ("historical_approximation", "warning", "fx_applied",
                  "record_notes"):
            if k in result:
                out[k] = result[k]
        return out

    def search_customers(
        self, query: str = "", limit: int = 25, business_unit: str = ""
    ) -> dict:
        bu = self._bu(business_unit)
        setid = self.e.resolve_setid(bu, "CUSTOMER")
        p = self.db.prefix
        params = {
            "setid": setid, "bu": bu,
            "q": f"%{(query or '').upper()}%",
            "qa": f"{(query or '').strip().upper()}%",
        }
        cs = self._customer_shape()
        c_name = f"C.{cs['name']} AS name" if cs["name"] else "NULL AS name"
        c_stat = (f"C.{cs['status']} AS status" if cs["status"]
                  else "NULL AS status")
        # Resolving a NAME to an id is the moment to say whether that id is
        # part of something bigger. Without it, "how much does ACME owe" is
        # answered for one legal entity out of three and looks complete.
        # One more column on a row already being selected.
        c_corp = (f"C.{cs['corp']} AS corporate_parent" if cs["corp"]
                  else "NULL AS corporate_parent")
        # Withdraw the name predicate rather than let it match nothing.
        name_pred = f"UPPER(C.{cs['name']}) LIKE :q OR " if cs["name"] else ""
        sql = f"""SELECT C.CUST_ID AS cust_id, {c_name},
       {c_stat}, {c_corp}, COALESCE(B.bal, 0) AS open_balance
  FROM {p}PS_CUSTOMER C
  LEFT JOIN (SELECT CUST_ID, SUM(BAL_AMT) AS bal FROM {p}PS_ITEM
              WHERE BUSINESS_UNIT = :bu AND ITEM_STATUS = 'O'
              GROUP BY CUST_ID) B ON B.CUST_ID = C.CUST_ID
 WHERE C.SETID = :setid
   AND ({name_pred}UPPER(C.CUST_ID) LIKE :qa)
 ORDER BY C.CUST_ID"""
        rows, truncated = self.db.query(sql, params, max_rows=max(int(limit or 25), 1))
        in_a_group = []
        for r in rows:
            r["open_balance"] = r2(r["open_balance"])
            parent = str(r.get("corporate_parent") or "")
            r["corporate_parent"] = parent
            if parent and parent != r["cust_id"]:
                in_a_group.append(r["cust_id"])
        out = {"customers": rows, "count": len(rows), "truncated": truncated,
               "note": "status A=active, I=inactive; open_balance sums open items"}
        if in_a_group:
            out["belongs_to_a_corporate_family"] = in_a_group
            out["next_step"] = (
                "One or more of these customers is a subsidiary — "
                "corporate_parent names the company that owns it. "
                "open_balance above is that legal entity ALONE. For the "
                "group's combined position call "
                "get_customer_financial_360(cust_id=<the parent>), which "
                "rolls the family up from the recorded hierarchy."
            )
        if cs["notes"]:
            out["record_notes"] = cs["notes"]
        return out

    # Bill statuses by lifecycle class. INV is the only status that IS
    # revenue; the pipeline statuses can still become revenue; CAN never
    # will. Splitting the exclusions is the point: lumping a cancelled
    # bill with a ready-to-invoice one tells a controller upside that
    # does not exist.
    _BILL_PIPELINE = {"NEW": "new bill", "RDY": "ready to invoice",
                      "HLD": "on hold", "TMP": "temporary", "PND": "pending"}
    _BILL_TERMINAL = {"CAN": "cancelled"}

    def invoice_totals(self, business_unit: str = "",
                       fiscal_year: int = 0) -> dict:
        """Total FINALIZED invoice amount, with a population block that
        says exactly what was counted and what was left out.

        "Give me total invoice amount" means finalized bills only
        (BILL_STATUS = 'INV') — but the default must be VISIBLE, split
        into pipeline (could still become revenue) vs cancelled (never
        will), and it must refuse rather than answer 0.00 when the
        default itself emptied the result. One query: the same grouped
        aggregate produces the answer AND its counterfactual.
        """
        from .semantics import resolve as resolve_concept
        bu = self._bu(business_unit)
        p = self.db.prefix
        sem = resolve_concept("billing_invoiced", cfg=self.cfg,
                              memory=getattr(self.e, "_memory", None))
        finalized = set(sem.values)
        cols = self._cols("PS_BI_HDR")
        date_col = "ACCOUNTING_DT"
        notes: list = []
        if cols and "ACCOUNTING_DT" not in cols:
            date_col = "INVOICE_DT"
            notes.append("PS_BI_HDR here has no ACCOUNTING_DT; the window "
                         "uses INVOICE_DT instead.")
        fy = int(fiscal_year or 0)
        window = None
        if fy:
            try:
                periods = self.e.list_periods(fy)["periods"]
                window = (str(periods[0]["begin_dt"])[:10],
                          str(periods[-1]["end_dt"])[:10])
            except Exception:
                notes.append(f"No fiscal calendar for {fy}; the total "
                             "covers the entire history.")
        where = f"BUSINESS_UNIT = :bu"
        params: dict = {"bu": bu}
        if window:
            where += (f" AND {date_col} >= {self.db.date_bind('w0')}"
                      f" AND {date_col} <= {self.db.date_bind('w1')}")
            params.update({"w0": window[0], "w1": window[1]})
        rows, _ = self.db.query(
            f"SELECT BILL_STATUS AS status, BI_CURRENCY_CD AS currency, "
            f"COUNT(*) AS n, SUM(INVOICE_AMOUNT) AS amount "
            f"FROM {p}PS_BI_HDR WHERE {where} "
            f"GROUP BY BILL_STATUS, BI_CURRENCY_CD",
            params, max_rows=200,
        )
        by_status = []
        inv_total: dict[str, float] = {}
        inv_count = 0
        pipeline: list = []
        terminal: list = []
        for r in rows:
            status = str(r["status"] or "")
            cur = str(r["currency"] or "")
            n = int(r["n"] or 0)
            amt = r2(float(r["amount"] or 0.0))
            if status in finalized:
                cls = "finalized"
                inv_total[cur] = r2(inv_total.get(cur, 0.0) + amt)
                inv_count += n
            elif status in self._BILL_TERMINAL:
                cls = "excluded_terminal"
                terminal.append({"status": status,
                                 "descr": self._BILL_TERMINAL[status],
                                 "currency": cur, "n": n, "amount": amt})
            else:
                cls = "excluded_pipeline"
                pipeline.append({"status": status,
                                 "descr": self._BILL_PIPELINE.get(
                                     status, "unrecognized status"),
                                 "currency": cur, "n": n, "amount": amt})
            by_status.append({"status": status, "class": cls,
                              "currency": cur, "n": n, "amount": amt})
        window_applied = {
            "predicate": sem.predicate,
            "source": sem.source,
            "meaning": sem.concept.meaning,
        }
        notes.extend(sem.notes)
        applied = [window_applied,
                   {"predicate": f"BUSINESS_UNIT = '{bu}'",
                    "source": "request scope", "meaning": "your selected "
                    "business unit"}]
        if window:
            applied.append({
                "predicate": f"{date_col} in {window[0]}..{window[1]}",
                "source": f"fiscal year {fy} from the request scope",
                "meaning": "the scope's fiscal year, by accounting date"})
        else:
            applied.append({
                "predicate": "no date window",
                "source": "no fiscal year in scope",
                "meaning": "the total covers the entire history"})
        # A live instance often carries 0.00 on non-final headers until the
        # finalization run writes the amount — a currency total over those
        # rows would be a confident falsehood, so render counts only.
        amount_basis = "header"
        if any(x["n"] > 0 and x["amount"] == 0.0 for x in pipeline + terminal):
            amount_basis = "unavailable_pre_finalization"
            notes.append("One or more excluded statuses carry zero header "
                         "amounts (normal before finalization) — excluded "
                         "AMOUNTS are unreliable here; trust the counts.")
        pipeline_share_pct: dict[str, float] = {}
        if amount_basis == "header":
            for cur in inv_total:
                pipe_cur = sum(x["amount"] for x in pipeline
                               if x["currency"] == cur)
                base = inv_total[cur] + pipe_cur
                if base:
                    pipeline_share_pct[cur] = r2(pipe_cur / base * 100.0)
        if inv_count == 0 and rows:
            return {
                "scope_status": "empty_after_default",
                "business_unit": bu,
                "detail": (
                    f"No FINALIZED bills ({sem.predicate}) match this "
                    "scope — the zero comes from the finalized-only "
                    "default, not from an empty table. Statuses that DO "
                    "exist here: "
                    + ", ".join(f"{x['status']} ({x['n']})"
                                for x in by_status)
                    + ". Ask for 'all bill statuses' to see everything."),
                "by_status": by_status,
            }
        return {
            "business_unit": bu,
            "invoiced_total_by_currency": inv_total,
            "invoice_count": inv_count,
            "by_status": by_status,
            "population": {
                "concept": "total invoiced (finalized billing)",
                "applied": applied,
                "date_governor": date_col,
                "excluded_pipeline": pipeline,
                "excluded_terminal": terminal,
                "pipeline_total_by_currency": {
                    cur: r2(sum(x["amount"] for x in pipeline
                                if x["currency"] == cur))
                    for cur in {x["currency"] for x in pipeline}},
                "pipeline_share_pct": pipeline_share_pct,
                "amount_basis": amount_basis,
                "override": "ask for 'all bill statuses' or name a status",
            },
            **({"record_notes": notes} if notes else {}),
            "note": ("Finalized bills only; the population block lists "
                     "every applied default and everything excluded, "
                     "pipeline separated from cancelled. Totals are per "
                     "currency and never summed across."),
        }

    def invoice_lifecycle(self, business_unit: str = "",
                          as_of_date: str = "",
                          lookback_days: int = 365) -> dict:
        """Where is the billing delay: the order-to-cash pipeline as
        stages with counts, amounts and ages, and the bottleneck named.

        Visibility starts at the billing interface (order-management
        records vary too much per site to assume). Historical cycle times
        need a creation timestamp; where PS_BI_HDR has none, the tool says
        so and reports current pipeline AGES instead — an honest "where
        is it stuck now" rather than a fabricated "how fast was it".
        """
        bu = self._bu(business_unit)
        asof = _iso(self._asof(as_of_date))
        p = self.db.prefix
        stages: list = []
        notes: list = []

        # Stage 1: the billing interface — rows waiting or in error.
        if self._cols("PS_INTFC_BI"):
            rows, _ = self.db.query(
                f"SELECT LOAD_STATUS_BI AS st, COUNT(*) AS n "
                f"FROM {p}PS_INTFC_BI WHERE BUSINESS_UNIT = :bu "
                f"GROUP BY LOAD_STATUS_BI", {"bu": bu}, max_rows=20)
            by = {str(r["st"]): int(r["n"] or 0) for r in rows}
            stages.append({"stage": "interface_waiting", "n": by.get("NEW", 0),
                           "amount": None, "oldest_days": None,
                           "meaning": "loaded, not yet a bill"})
            stages.append({"stage": "interface_error", "n": by.get("ERR", 0),
                           "amount": None, "oldest_days": None,
                           "meaning": "stuck until someone fixes the row"})
        else:
            notes.append("PS_INTFC_BI not present — interface visibility "
                         "not available at this site.")

        # Stage 2: bills in the pipeline, aged by accounting date.
        rows, _ = self.db.query(
            f"SELECT BILL_STATUS AS st, COUNT(*) AS n, "
            f"SUM(INVOICE_AMOUNT) AS amt, MIN(ACCOUNTING_DT) AS oldest "
            f"FROM {p}PS_BI_HDR WHERE BUSINESS_UNIT = :bu "
            f"AND BILL_STATUS IN ('NEW','PND','HLD','RDY','TMP') "
            f"GROUP BY BILL_STATUS", {"bu": bu}, max_rows=20)
        for r in rows:
            oldest = _iso_opt(r.get("oldest"))
            stages.append({
                "stage": f"bill_{str(r['st']).lower()}",
                "n": int(r["n"] or 0),
                "amount": r2(float(r["amt"] or 0)),
                "oldest_days": (asof - oldest).days if oldest else None,
                "meaning": BILL_STATUS_DESCR.get(str(r["st"]),
                                                 str(r["st"]))})

        # Stage 3: finalized but never reached AR — billed, invisible.
        # Date-floored, same as the workbench and for the same reason: an
        # unfloored NOT EXISTS is a full-history anti-join on a real
        # PS_BI_HDR (millions of rows), and the review that added this
        # floor found the hazard already documented one screen down.
        since = (asof - dt.timedelta(
            days=max(int(lookback_days or 365), 1))).isoformat()
        rows, _ = self.db.query(
            f"SELECT COUNT(*) AS n, SUM(H.INVOICE_AMOUNT) AS amt "
            f"FROM {p}PS_BI_HDR H WHERE H.BUSINESS_UNIT = :bu "
            f"AND H.BILL_STATUS = 'INV' "
            f"AND H.INVOICE_DT >= {self.db.date_bind('since')} "
            f"AND NOT EXISTS "
            f"(SELECT 1 FROM {p}PS_ITEM I WHERE "
            f"I.BUSINESS_UNIT = H.BUSINESS_UNIT AND I.ITEM = H.INVOICE)",
            {"bu": bu, "since": since}, max_rows=1)
        orphan = rows[0] if rows else {}
        stages.append({"stage": "finalized_not_in_ar",
                       "n": int(orphan.get("n") or 0),
                       "amount": r2(float(orphan.get("amt") or 0)),
                       "oldest_days": None,
                       "meaning": "billed but not yet an open item — "
                                  "revenue the collectors cannot see"})

        # Stage 4: open in AR, aged by due date.
        rows, _ = self.db.query(
            f"SELECT COUNT(*) AS n, SUM(BAL_AMT) AS amt, "
            f"MIN(DUE_DT) AS oldest FROM {p}PS_ITEM "
            f"WHERE BUSINESS_UNIT = :bu AND ITEM_STATUS = 'O'",
            {"bu": bu}, max_rows=1)
        open_row = rows[0] if rows else {}
        oldest = _iso_opt(open_row.get("oldest"))
        stages.append({"stage": "open_in_ar",
                       "n": int(open_row.get("n") or 0),
                       "amount": r2(float(open_row.get("amt") or 0)),
                       "oldest_days": (asof - oldest).days if oldest else None,
                       "meaning": "awaiting payment"})

        # Cycle times only where a creation timestamp exists.
        cols = self._cols("PS_BI_HDR")
        add_col = next((c for c in ("ADD_DTTM", "ADD_DT", "ENTRY_DT")
                        if cols and c in cols), None)
        cycle = None
        if add_col:
            rows, _ = self.db.query(
                f"SELECT {add_col} AS created, INVOICE_DT AS finalized "
                f"FROM {p}PS_BI_HDR WHERE BUSINESS_UNIT = :bu "
                f"AND BILL_STATUS = 'INV' "
                f"AND INVOICE_DT >= {self.db.date_bind('since')}",
                {"bu": bu, "since": since}, max_rows=5000)
            gaps = sorted((_iso(str(r["finalized"])) -
                           _iso(str(r["created"]))).days
                          for r in rows
                          if r.get("created") and r.get("finalized"))
            if gaps:
                cycle = {"basis": f"{add_col} -> INVOICE_DT",
                         "n": len(gaps),
                         "p50_days": gaps[len(gaps) // 2],
                         "p90_days": gaps[min(len(gaps) - 1,
                                              int(len(gaps) * 0.9))]}
        else:
            notes.append("PS_BI_HDR carries no creation timestamp at this "
                         "site — historical cycle times are unavailable; "
                         "the stage ages above are the honest substitute.")

        candidates = [s2 for s2 in stages
                      if s2["n"] and s2["stage"] not in ("open_in_ar",)]
        bottleneck = (max(candidates,
                          key=lambda s2: (s2.get("amount") or 0,
                                          s2.get("oldest_days") or 0))
                      if candidates else None)
        return {
            "business_unit": bu, "as_of": asof.isoformat(),
            "stages": stages,
            **({"cycle_time": cycle} if cycle else {}),
            "bottleneck": (
                {"stage": bottleneck["stage"],
                 "amount": bottleneck.get("amount"),
                 "n": bottleneck["n"],
                 "why": "largest value sitting outside AR right now"}
                if bottleneck else None),
            **({"record_notes": notes} if notes else {}),
            "lookback_days": int(lookback_days or 365),
            "note": ("Pipeline from the billing interface to AR. "
                     "Visibility starts at the interface; upstream order "
                     "records vary by site and are not assumed. Orphan and "
                     "cycle checks cover the lookback window, not all "
                     "history."),
        }

    def dso_trend(self, business_unit: str = "",
                  fiscal_year: int = 0) -> dict:
        """Monthly DSO from the ledger's own numbers, formula disclosed.

        DSO(period) = ending AR balance / period revenue * days in period.
        Both sides come straight from PS_LEDGER — the AR control account's
        cumulative balance and the signed sum of ACCOUNT_TYPE='R' accounts
        (credits negative, so revenue = -sum). No model, no estimate.
        """
        bu = self._bu(business_unit)
        led = self.e.resolve_ledger_for(bu)
        fy = int(fiscal_year or 0) or self.e.last_posted_period(bu, led)[0]
        if not fy:
            raise ARError(f"No posted ledger data for {bu!r} — DSO needs "
                          "a fiscal year with activity.")
        p = self.db.prefix
        controls = [str(a) for a in
                    (self.cfg.defaults.ar_control_accounts or ["1100"])]
        abinds = {f"a{i}": a for i, a in enumerate(controls)}
        aexpr = "(" + ", ".join(f":{k}" for k in abinds) + ")"
        ar_rows, _ = self.db.query(
            f"SELECT ACCOUNTING_PERIOD AS per, SUM(POSTED_TOTAL_AMT) AS amt "
            f"FROM {p}PS_LEDGER WHERE BUSINESS_UNIT = :bu AND LEDGER = :led "
            f"AND FISCAL_YEAR = :fy AND ACCOUNT IN {aexpr} "
            f"GROUP BY ACCOUNTING_PERIOD",
            {"bu": bu, "led": led, "fy": fy, **abinds}, max_rows=30)
        setid = self.e.resolve_setid(bu, "GL_ACCOUNT")
        rev_rows, _ = self.db.query(
            f"SELECT L.ACCOUNTING_PERIOD AS per, "
            f"SUM(L.POSTED_TOTAL_AMT) AS amt "
            f"FROM {p}PS_LEDGER L JOIN {p}PS_GL_ACCOUNT_TBL A "
            f"ON A.ACCOUNT = L.ACCOUNT AND A.SETID = :setid "
            f"WHERE L.BUSINESS_UNIT = :bu AND L.LEDGER = :led "
            f"AND L.FISCAL_YEAR = :fy AND A.ACCOUNT_TYPE = 'R' "
            f"GROUP BY L.ACCOUNTING_PERIOD",
            {"bu": bu, "led": led, "fy": fy, "setid": setid}, max_rows=30)
        adj = set(self.e._adj_periods()) | {0}
        ar_by = {int(r["per"]): float(r["amt"] or 0) for r in ar_rows}
        rev_by = {int(r["per"]): -float(r["amt"] or 0) for r in rev_rows}
        periods = sorted((set(ar_by) | set(rev_by)) - adj)
        try:
            cal = {int(x["period"]): x
                   for x in self.e.list_periods(fy)["periods"]}
        except Exception:
            cal = {}
        rows_out = []
        running = sum(v for k, v in ar_by.items() if k == 0)
        for per in periods:
            running = r2(running + ar_by.get(per, 0.0))
            revenue = r2(rev_by.get(per, 0.0))
            entry = cal.get(per, {})
            days = 30
            if entry:
                days = ((_iso(str(entry["end_dt"])) -
                         _iso(str(entry["begin_dt"]))).days + 1)
            dso = r2(running / revenue * days) if revenue > 0 else None
            rows_out.append({
                "fiscal_year": fy, "period": per,
                "ar_ending": running, "revenue": revenue,
                "dso_days": dso})
        return {
            "business_unit": bu, "ledger": led, "fiscal_year": fy,
            "rows": rows_out,
            "formula": ("DSO = ending AR control balance / period revenue "
                        "x days in period; revenue = -(sum of "
                        "ACCOUNT_TYPE 'R' postings), credits negative"),
            "ar_control_accounts": controls,
            "note": ("Computed from the ledger only. A period with zero "
                     "or negative revenue shows no DSO rather than a "
                     "misleading number."),
        }

    def cash_outlook(self, business_unit: str = "",
                     weeks: int = 8, as_of_date: str = "") -> dict:
        """Expected cash by week from DUE DATES — arithmetic, not a
        forecast, and it says so. Inflows are open AR items; outflows are
        open vouchers. Overdue lands in its own bucket because 'due last
        month' is not a plan for next week."""
        bu = self._bu(business_unit)
        asof = _iso(self._asof(as_of_date))
        weeks = max(1, min(int(weeks or 8), 13))
        p = self.db.prefix
        items, _ = self.db.query(
            f"SELECT DUE_DT AS due, BAL_AMT AS amt, BAL_CURRENCY AS cur "
            f"FROM {p}PS_ITEM WHERE BUSINESS_UNIT = :bu "
            f"AND ITEM_STATUS = 'O'", {"bu": bu}, max_rows=DETAIL_ROW_CAP)
        vcols = self._cols("PS_VOUCHER")
        notes: list = []
        if not vcols or "CLOSE_STATUS" in vcols:
            open_pred = "V.CLOSE_STATUS = 'O'"
        else:
            open_pred = (f"NOT EXISTS (SELECT 1 FROM "
                         f"{p}PS_PYMNT_VCHR_XREF X "
                         f"WHERE X.BUSINESS_UNIT = V.BUSINESS_UNIT "
                         f"AND X.VOUCHER_ID = V.VOUCHER_ID)")
            notes.append("PS_VOUCHER here has no CLOSE_STATUS; 'open' "
                         "means no payment cross-reference exists, which "
                         "misses partial payments.")
        vchr, _ = self.db.query(
            f"SELECT V.DUE_DT AS due, V.GROSS_AMT AS amt, "
            f"V.CURRENCY_CD AS cur FROM {p}PS_VOUCHER V "
            f"WHERE V.BUSINESS_UNIT = :bu AND {open_pred}",
            {"bu": bu}, max_rows=DETAIL_ROW_CAP)
        starts = [asof + dt.timedelta(days=7 * i) for i in range(weeks)]

        def bucket(due):
            if due is None:
                return None
            if due < asof:
                return "overdue"
            for i, start in enumerate(starts):
                if due < start + dt.timedelta(days=7):
                    return start.isoformat()
            return "beyond"

        acc: dict = {}
        currencies: set = set()
        for src, sign_key in ((items, "expected_in"), (vchr, "expected_out")):
            for r in src:
                cur = str(r.get("cur") or "")
                currencies.add(cur)
                b = bucket(_iso_opt(r.get("due")))
                if b is None:
                    b = "overdue"
                slot = acc.setdefault((b, cur), {"expected_in": 0.0,
                                                 "expected_out": 0.0})
                slot[sign_key] = r2(slot[sign_key] + float(r["amt"] or 0))
        order = ["overdue"] + [s.isoformat() for s in starts] + ["beyond"]
        rows_out = []
        for b in order:
            for cur in sorted(currencies):
                slot = acc.get((b, cur))
                if not slot:
                    continue
                rows_out.append({
                    "week": b, "currency": cur,
                    "expected_in": r2(slot["expected_in"]),
                    "expected_out": r2(slot["expected_out"]),
                    "net": r2(slot["expected_in"] - slot["expected_out"])})
        # The totals a summary sentence needs, precomputed — a model that
        # adds buckets in prose states figures no payload contains, and
        # the guard rightly withholds the answer. Give it the numbers.
        totals: dict = {}
        for r in rows_out:
            t = totals.setdefault(r["currency"], {
                "expected_in": 0.0, "expected_out": 0.0, "net": 0.0,
                "overdue_in": 0.0, "overdue_out": 0.0})
            if r["week"] == "overdue":
                t["overdue_in"] = r2(t["overdue_in"] + r["expected_in"])
                t["overdue_out"] = r2(t["overdue_out"] + r["expected_out"])
            else:
                t["expected_in"] = r2(t["expected_in"] + r["expected_in"])
                t["expected_out"] = r2(t["expected_out"] + r["expected_out"])
                t["net"] = r2(t["net"] + r["net"])
        return {
            "business_unit": bu, "as_of": asof.isoformat(),
            "weeks": weeks, "rows": rows_out,
            "totals_by_currency": totals,
            **({"record_notes": notes} if notes else {}),
            "note": ("Due-date arithmetic over open AR items and open "
                     "vouchers — the starting point a treasurer refines, "
                     "NOT a payment-behavior forecast. Overdue is its own "
                     "bucket (and its own totals); amounts stay per "
                     "currency. State totals from totals_by_currency — "
                     "never add buckets yourself."),
        }

    def customer_intelligence(self, business_unit: str = "", n: int = 20,
                              months: int = 12, display_currency: str = "",
                              as_of_date: str = "") -> dict:
        """Top customers enriched with WHERE they are, WHAT they buy, and
        HOW they pay — plus computed observations, each carrying its own
        figures so the guard can verify every claim.

        The advisory rung done honestly: an "observation" is arithmetic
        over records (terms gap, concentration, disputes, currency
        exposure, lapsed buyers), never generated advice. Enrichments are
        shape-tolerant — a site without PS_CUST_ADDRESS or PS_BI_LINE
        loses that column and is told so, never crashed on.
        """
        bu = self._bu(business_unit)
        base = self.e.base_currency_for(bu) or "USD"
        disp = (display_currency or "").strip().upper() or base
        top = self.top_billing_customers(
            business_unit=bu, n=n, months=months, display_currency=disp,
            as_of_date=as_of_date)
        if top.get("mixed_currencies") and not top.get("customers"):
            return top  # ranking impossible; pass the refusal through
        ranked = top.get("customers") or []
        ids = [c["cust_id"] for c in ranked]
        notes: list = list(top.get("record_notes") or [])
        asof = self._asof(as_of_date)
        p = self.db.prefix
        setid = self.e.resolve_setid(bu, "CUSTOMER")

        def in_binds(prefix: str) -> tuple:
            binds = {f"{prefix}{i}": cid for i, cid in enumerate(ids)}
            expr = "(" + ", ".join(f":{k}" for k in binds) + ")"
            return expr, binds

        # WHERE they are: primary address per customer.
        locations: dict = {}
        if ids and self._cols("PS_CUST_ADDRESS"):
            expr, binds = in_binds("a")
            rows, _ = self.db.query(
                f"SELECT CUST_ID AS cid, CITY AS city, STATE AS state, "
                f"COUNTRY AS country, ADDRESS_SEQ_NUM AS seq "
                f"FROM {p}PS_CUST_ADDRESS WHERE SETID = :setid "
                f"AND CUST_ID IN {expr} ORDER BY ADDRESS_SEQ_NUM",
                {"setid": setid, **binds}, max_rows=len(ids) * 5)
            for r in rows:
                locations.setdefault(str(r["cid"]), {
                    "city": r.get("city"), "state": r.get("state"),
                    "country": r.get("country")})
        elif ids:
            notes.append("PS_CUST_ADDRESS is not present at this site — "
                         "customer geography is not available.")

        # WHAT they buy: finalized bill lines over the same window, with
        # the finalized statuses resolved through the concept register so
        # a taught status override applies here too.
        from .semantics import resolve as resolve_concept
        sem = resolve_concept("billing_invoiced", cfg=self.cfg,
                              memory=getattr(self.e, "_memory", None))
        products: dict = {}
        if ids and self._cols("PS_BI_LINE"):
            expr, binds = in_binds("l")
            status_binds = {f"st{i}": v for i, v in enumerate(sem.values)}
            st_expr = "(" + ", ".join(f":{k}" for k in status_binds) + ")"
            since = (_iso(asof) - dt.timedelta(
                days=max(int(months or 12), 1) * 30)).isoformat()
            rows, _ = self.db.query(
                f"SELECT H.BILL_TO_CUST_ID AS cid, L.IDENTIFIER AS ident, "
                f"MAX(L.DESCR) AS descr, SUM(L.NET_EXTENDED_AMT) AS amt, "
                f"MAX(H.BI_CURRENCY_CD) AS currency "
                f"FROM {p}PS_BI_LINE L JOIN {p}PS_BI_HDR H "
                f"ON H.BUSINESS_UNIT = L.BUSINESS_UNIT "
                f"AND H.INVOICE = L.INVOICE "
                f"WHERE H.BUSINESS_UNIT = :bu AND H.BILL_STATUS IN {st_expr} "
                f"AND H.INVOICE_DT >= {self.db.date_bind('since')} "
                f"AND H.BILL_TO_CUST_ID IN {expr} "
                f"GROUP BY H.BILL_TO_CUST_ID, L.IDENTIFIER",
                {"bu": bu, "since": since, **binds, **status_binds},
                max_rows=len(ids) * 50)
            for r in rows:
                products.setdefault(str(r["cid"]), []).append(
                    {"identifier": str(r["ident"] or ""),
                     "descr": str(r["descr"] or ""),
                     "amount": r2(float(r["amt"] or 0)),
                     "currency": str(r["currency"] or "")})
            for cid in products:
                products[cid].sort(key=lambda x: -x["amount"])
        elif ids:
            notes.append("PS_BI_LINE is not present at this site — product "
                         "mix is not available.")

        # HOW they pay: open-item behavior computed in Python (no dialect-
        # specific date arithmetic in SQL).
        behavior: dict = {}
        if ids:
            expr, binds = in_binds("b")
            rows, _ = self.db.query(
                f"SELECT CUST_ID AS cid, BAL_AMT AS bal, DUE_DT AS due, "
                f"DISPUTE_STATUS AS dispute "
                f"FROM {p}PS_ITEM WHERE BUSINESS_UNIT = :bu "
                f"AND ITEM_STATUS = 'O' AND CUST_ID IN {expr}",
                {"bu": bu, **binds}, max_rows=DETAIL_ROW_CAP)
            today = _iso(asof)
            for r in rows:
                b = behavior.setdefault(str(r["cid"]), {
                    "open_ar": 0.0, "overdue_amt": 0.0,
                    "late_weight": 0.0, "disputed_amt": 0.0})
                bal = float(r["bal"] or 0)
                b["open_ar"] = r2(b["open_ar"] + bal)
                due = _iso_opt(r.get("due"))
                if due and due < today and bal > 0:
                    days = (today - due).days
                    b["overdue_amt"] = r2(b["overdue_amt"] + bal)
                    b["late_weight"] += bal * days
                if str(r.get("dispute") or "").strip() and bal > 0:
                    b["disputed_amt"] = r2(b["disputed_amt"] + bal)

        customers = []
        for c in ranked:
            cid = c["cust_id"]
            b = behavior.get(cid, {})
            overdue = b.get("overdue_amt", 0.0)
            entry = {
                **c,
                "location": locations.get(cid),
                "top_products": (products.get(cid) or [])[:3],
                "open_ar": r2(b.get("open_ar", 0.0)),
                "overdue_amt": r2(overdue),
                "overdue_share_pct": (
                    r2(overdue / b["open_ar"] * 100.0)
                    if b.get("open_ar") else 0.0),
                "avg_days_late": (int(round(b["late_weight"] / overdue))
                                  if overdue else 0),
                "disputed_amt": r2(b.get("disputed_amt", 0.0)),
            }
            customers.append(entry)

        # Computed observations — arithmetic, not advice. Every figure in
        # the text also exists as a field, so the guard grounds it.
        observations = []
        if customers:
            top1 = customers[0]
            if top1.get("share_pct", 0) >= 25:
                observations.append({
                    "kind": "concentration",
                    "share_pct": top1["share_pct"],
                    "text": (f"{top1.get('name') or top1['cust_id']} is "
                             f"{top1['share_pct']}% of billings — "
                             "concentration risk worth a credit review.")})
            for c in customers:
                if c["overdue_amt"] > 0 and c["overdue_share_pct"] >= 50:
                    observations.append({
                        "kind": "late_payment",
                        "cust_id": c["cust_id"],
                        "overdue_amt": c["overdue_amt"],
                        "avg_days_late": c["avg_days_late"],
                        "text": (f"{c.get('name') or c['cust_id']} has "
                                 f"{c['overdue_amt']:,.2f} overdue, on "
                                 f"average {c['avg_days_late']} days late — "
                                 "working capital sitting with the "
                                 "customer; a collections touch or terms "
                                 "conversation is the lever.")})
                if c["disputed_amt"] > 0:
                    observations.append({
                        "kind": "disputes",
                        "cust_id": c["cust_id"],
                        "disputed_amt": c["disputed_amt"],
                        "text": (f"{c.get('name') or c['cust_id']} is "
                                 f"disputing {c['disputed_amt']:,.2f} — "
                                 "resolve the dispute before it ages into "
                                 "a write-off conversation.")})
            lapsed = [c for c in customers
                      if c.get("last_invoice")
                      and (_iso(asof) - _iso(c["last_invoice"])).days > 90]
            for c in lapsed:
                observations.append({
                    "kind": "lapsed",
                    "cust_id": c["cust_id"],
                    "last_invoice": c["last_invoice"],
                    "text": (f"{c.get('name') or c['cust_id']} last "
                             f"invoiced {c['last_invoice'][:10]} — a top "
                             "biller gone quiet for 90+ days is a churn "
                             "signal worth an account call.")})

        return {
            "business_unit": bu,
            "as_of": asof,
            "window_months": int(months or 12),
            "display_currency": disp,
            "customers": customers,
            "observations": observations,
            "population": {
                "concept": "customer intelligence over finalized billing",
                "applied": [
                    {"predicate": sem.predicate, "source": sem.source,
                     "meaning": sem.concept.meaning},
                    {"predicate": f"trailing {int(months or 12)} months",
                     "source": "tool default",
                     "meaning": "billing ranked over this window"},
                ],
            },
            **({"record_notes": notes} if notes else {}),
            "note": ("Observations are computed from the records above — "
                     "concentration, overdue, disputes and lapsed activity "
                     "— never generated advice. Billing ranked in "
                     f"{disp}; open-AR figures are in item currency."),
        }

    def top_billing_customers(self, business_unit: str = "", n: int = 10,
                              months: int = 12, as_of_date: str = "",
                              display_currency: str = "",
                              active_within_months: int = 0) -> dict:
        """Top customers by FINALIZED billing (BILL_STATUS='INV') over a
        trailing window. Groups by customer AND currency — mixed currencies are
        never silently summed; pass display_currency to convert server-side.

        business_unit="ALL" ranks across EVERY business unit in one query.
        "Top 20 customers across all BUs still buying" is one question to an
        accountant and used to be five to this system — a per-BU loop over
        separate model rounds that ran out of turns before it ran out of BUs.
        The whole chain now runs here: one grouped query, per-BU currency
        normalization, one ranking.

        active_within_months=N keeps only customers with an invoice in the
        last N months — the mechanical meaning of "still buying". Every row
        carries last_invoice_dt so the claim is grounded, not inferred.
        """
        bu_all = str(business_unit or "").strip().upper() in {"ALL", "*"}
        bu = "ALL" if bu_all else self._bu(business_unit)
        asof = self._asof(as_of_date)
        since = (_iso(asof) - dt.timedelta(days=max(int(months or 12), 1) * 30)
                 ).isoformat()
        active_since = ""
        if int(active_within_months or 0) > 0:
            active_since = (_iso(asof) - dt.timedelta(
                days=int(active_within_months) * 30)).isoformat()
        p = self.db.prefix
        setid = None if bu_all else self.e.resolve_setid(bu, "CUSTOMER")
        bi = self._cols("PS_BI_HDR")
        record_notes: list[str] = []
        if bi:
            need = [c for c in ("INVOICE_DT", "INVOICE_AMOUNT",
                                "BILL_TO_CUST_ID") if c not in bi]
            if need:
                raise ARError(
                    f"PS_BI_HDR at this site is missing {', '.join(need)} — "
                    "cannot rank billing volume. Run "
                    "python scripts/diagnose_db.py to see the real shape."
                )
        has_cur = (not bi) or ("BI_CURRENCY_CD" in bi)
        cur_sel = ("H.BI_CURRENCY_CD AS currency" if has_cur
                   else "'' AS currency")
        group_cur = ", H.BI_CURRENCY_CD" if has_cur else ""
        if not has_cur:
            record_notes.append("PS_BI_HDR here has no BI_CURRENCY_CD; "
                                "invoice amounts are assumed to be in the BU "
                                "base currency.")
        _cs = self._customer_shape()
        tb_name = f"C.{_cs['name']} AS name" if _cs["name"] else "NULL AS name"
        tb_group = f", C.{_cs['name']}" if _cs["name"] else ""
        record_notes.extend(n for n in _cs["notes"] if n not in record_notes)
        having = (f" HAVING MAX(H.INVOICE_DT) >= {self.db.date_bind('active_since')}"
                  if active_since else "")
        params: dict = {"bu": bu, "setid": setid, "since": since,
                        "active_since": active_since}
        if bu_all:
            # Across BUs the setid-scoped name join no longer applies (each
            # BU may resolve a different customer setid), so names attach
            # from a deduplicated one-row-per-customer subquery instead of a
            # keyed join — a customer defined under two setids must not rank
            # twice. BUSINESS_UNIT stays in the group so a blank invoice
            # currency can be normalized to THAT unit's base, not the
            # scoped one.
            name_col = (f"MAX(N.{_cs['name']})" if _cs["name"] else "NULL")
            rows, _ = self.db.query(
                f"""SELECT H.BILL_TO_CUST_ID AS cust_id,
       {name_col} AS name,
       H.BUSINESS_UNIT AS bu, {cur_sel}, COUNT(*) AS invoices,
       SUM(H.INVOICE_AMOUNT) AS billed,
       MAX(H.INVOICE_DT) AS last_invoice
  FROM {p}PS_BI_HDR H
  LEFT JOIN (SELECT CUST_ID{', ' + _cs['name'] if _cs['name'] else ''}
               FROM {p}PS_CUSTOMER GROUP BY CUST_ID{
                   ', ' + _cs['name'] if _cs['name'] else ''}) N
    ON N.CUST_ID = H.BILL_TO_CUST_ID
 WHERE H.BILL_STATUS = 'INV'
   AND H.INVOICE_DT >= {self.db.date_bind('since')}
 GROUP BY H.BILL_TO_CUST_ID, H.BUSINESS_UNIT{group_cur}{having}""",
                params, max_rows=50_000,
            )
        else:
            rows, _ = self.db.query(
                f"""SELECT H.BILL_TO_CUST_ID AS cust_id, {tb_name},
       H.BUSINESS_UNIT AS bu,
       {cur_sel}, COUNT(*) AS invoices,
       SUM(H.INVOICE_AMOUNT) AS billed,
       MAX(H.INVOICE_DT) AS last_invoice
  FROM {p}PS_BI_HDR H
  LEFT JOIN {p}PS_CUSTOMER C ON C.SETID = :setid AND C.CUST_ID = H.BILL_TO_CUST_ID
 WHERE H.BUSINESS_UNIT = :bu AND H.BILL_STATUS = 'INV'
   AND H.INVOICE_DT >= {self.db.date_bind('since')}
 GROUP BY H.BILL_TO_CUST_ID{tb_group}, H.BUSINESS_UNIT{group_cur}{having}""",
                params, max_rows=10_000,
            )
        base_by_bu: dict = {}
        for r in rows:
            row_bu = str(r.get("bu") or "")
            if row_bu not in base_by_bu:
                base_by_bu[row_bu] = (self.e.base_currency_for(row_bu)
                                      or "USD")
        base = (base_by_bu.get(bu) or "USD") if not bu_all else "USD"
        # A blank BI_CURRENCY_CD means the invoice is in the BU base currency
        # — normalize it so mixed-currency detection and conversion see it.
        currencies = sorted({(r["currency"] or "").upper()
                             or base_by_bu.get(str(r.get("bu") or ""), base)
                             for r in rows})
        disp = (display_currency or "").strip().upper()
        mixed = len(currencies) > 1
        fx_notes = []
        by_cust: dict[str, dict] = {}
        for r in rows:
            amt = float(r["billed"] or 0)
            cur = ((r["currency"] or "").upper()
                   or base_by_bu.get(str(r.get("bu") or ""), base))
            if disp and cur != disp:
                fx = self.e.exchange_rate(cur, disp, as_of_date=asof)
                amt = amt * fx["rate"]
                note = f"{cur}->{disp} @ {fx['rate']}"
                if note not in fx_notes:
                    fx_notes.append(note)
            c = by_cust.setdefault(r["cust_id"], {
                "cust_id": r["cust_id"], "name": r.get("name"),
                "invoices": 0, "billed": 0.0, "currencies": set(),
                "business_units": set(), "last_invoice": "",
            })
            c["invoices"] += int(r["invoices"] or 0)
            c["billed"] += amt
            c["currencies"].add(disp or cur)
            if r.get("bu"):
                c["business_units"].add(str(r["bu"]))
            last = str(r.get("last_invoice") or "")
            if last > c["last_invoice"]:
                c["last_invoice"] = last
        if mixed and not disp:
            return {
                "business_unit": bu, "window_months": int(months or 12),
                "since": since, "as_of": asof,
                "mixed_currencies": currencies,
                # business_unit rides along on the ALL ranking, exactly as
                # it does on the converted path below. Without it a
                # cross-unit ranking names customers and amounts but never
                # says which company billed them — and the scope chip still
                # reads one unit, so the reader has no way to tell the
                # answer widened.
                "by_currency": [
                    {"cust_id": r["cust_id"], "name": r.get("name"),
                     "currency": r["currency"],
                     "invoices": int(r["invoices"] or 0),
                     "billed": r2(float(r["billed"] or 0)),
                     **({"business_unit": str(r["bu"])} if bu_all else {})}
                    for r in sorted(rows, key=lambda x: -float(x["billed"] or 0))
                ],
                "note": (
                    "Invoices exist in multiple currencies; totals are NOT "
                    "summed across currencies. Pass display_currency (e.g. "
                    "'USD' or 'INR') to rank on converted totals."
                    + (" Ranked across ALL business units, not only the "
                       "selected one — every row names the unit that "
                       "billed it." if bu_all else "")
                ),
                **({"record_notes": record_notes} if record_notes else {}),
            }
        ranked = sorted(by_cust.values(), key=lambda c: -c["billed"])
        total = sum(c["billed"] for c in ranked) or 1.0
        top = []
        for c in ranked[: max(int(n or 10), 1)]:
            entry = {
                "cust_id": c["cust_id"], "name": c["name"],
                "invoices": c["invoices"], "billed": r2(c["billed"]),
                "share_pct": r2(c["billed"] / total * 100),
                "currency": disp or (currencies[0] if currencies else ""),
                "last_invoice_dt": c["last_invoice"] or None,
            }
            if bu_all:
                entry["business_units"] = sorted(c["business_units"])
            top.append(entry)
        out = {
            "business_unit": bu, "window_months": int(months or 12),
            "since": since, "as_of": asof,
            "currency": disp or (currencies[0] if currencies else ""),
            "customers": top,
            "total_billed": r2(sum(c["billed"] for c in ranked)),
            "customer_count": len(ranked),
            "note": ("Finalized (INV) invoices only, by invoice date window. "
                     "This is BILLING volume, not open AR — see get_ar_aging "
                     "for what is owed."
                     + (" Ranked across ALL business units, not only the "
                        "selected one — business_units on each row names "
                        "which billed that customer." if bu_all else "")),
        }
        if bu_all:
            out["scope"] = ("ALL business units — cross-unit ranking was "
                            "requested in the question; each customer row "
                            "lists the units it billed under")
        if active_since:
            out["active_within_months"] = int(active_within_months)
            out["active_since"] = active_since
            out["activity_note"] = (
                f"only customers with a finalized invoice on or after "
                f"{active_since} are included — that is the operational "
                "meaning of 'still buying' here; each row's "
                "last_invoice_dt is the evidence")
        if fx_notes:
            out["fx_applied"] = fx_notes
            out["fx_note"] = ("Converted server-side at effective-dated "
                              "PS_RT_RATE_TBL rates — copy figures verbatim.")
        if record_notes:
            out["record_notes"] = record_notes
        return out

    # ---------------------------------------------------------------- billing
    def billing_workbench(self, business_unit: str = "", days_stuck: int = 5,
                          as_of_date: str = "", lookback_days: int = 365) -> dict:
        bu = self._bu(business_unit)
        asof = self._asof(as_of_date)
        asof_d = _iso(asof)
        p = self.db.prefix

        # Adapt to this site's PS_BI_HDR shape; unknown shape (introspection
        # failed) assumes the reference layout.
        bi = self._cols("PS_BI_HDR")
        record_notes: list[str] = []
        if bi:
            req = [c for c in ("INVOICE", "BILL_STATUS") if c not in bi]
            if req:
                raise ARError(
                    f"PS_BI_HDR at this site is missing required column(s) "
                    f"{', '.join(req)} — run python scripts/diagnose_db.py "
                    "and check db.schema in config.yaml."
                )
        def _h(col: str, alias: str) -> str:
            if bi and col not in bi:
                return f"NULL AS {alias}"
            return f"{col} AS {alias}"
        has_amt = (not bi) or ("INVOICE_AMOUNT" in bi)
        has_dt = (not bi) or ("INVOICE_DT" in bi)
        if not has_amt:
            record_notes.append("PS_BI_HDR here has no INVOICE_AMOUNT; "
                                "billing amounts are not available.")
        if not has_dt:
            record_notes.append("PS_BI_HDR here has no INVOICE_DT; "
                                "days-pending and the not-loaded-to-AR check "
                                "are not available.")
        for c_, what in (("BILL_TO_CUST_ID", "customer ids"),
                         ("BILL_SOURCE_ID", "billing sources")):
            if bi and c_ not in bi:
                record_notes.append(f"PS_BI_HDR here has no {c_}; "
                                    f"{what} are not shown.")

        rows, _ = self.db.query(
            f"""SELECT BILL_STATUS AS status, COUNT(*) AS n,
       {'SUM(INVOICE_AMOUNT)' if has_amt else 'SUM(0)'} AS amount
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
       {_h('BILL_TO_CUST_ID', 'cust_id')}, {_h('INVOICE_DT', 'invoice_dt')},
       {_h('INVOICE_AMOUNT', 'amount')}, {_h('BILL_SOURCE_ID', 'source')}
  FROM {p}PS_BI_HDR
 WHERE BUSINESS_UNIT = :bu
   AND BILL_STATUS IN ({", ".join("'" + s + "'" for s in NOT_FINAL)})
 ORDER BY {'INVOICE_DT' if has_dt else 'INVOICE'}""",
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

        # The billing interface table may be absent or unreadable at a site —
        # degrade that section with a note rather than failing the workbench.
        try:
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
        except DbError as e:
            interface, errs = [], []
            intfc_ok = False
            record_notes.append(
                f"Billing interface (PS_INTFC_BI) not readable here — "
                f"interface checks skipped: {e}"
            )
        else:
            intfc_ok = True

        # Date-floored: an unfloored NOT EXISTS over all finalized history is a
        # full-history anti-join on a real PS_BI_HDR.
        since = (asof_d - dt.timedelta(days=max(int(lookback_days or 365), 1))
                 ).isoformat()
        if has_dt:
            orphans, orph_trunc = self.db.query(
                f"""SELECT H.INVOICE AS invoice, {_h('BILL_TO_CUST_ID', 'cust_id')},
       H.INVOICE_DT AS invoice_dt, {_h('INVOICE_AMOUNT', 'amount')}
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
        else:
            orphans, orph_trunc = [], False

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
            # A skipped check is NOT a pass: when days-pending, the AR-load
            # check, or the interface checks could not run, say so.
            "control_status": (
                "exceptions_found" if issues
                else ("checks_incomplete" if (not has_dt or not intfc_ok)
                      else "passed")
            ),
            "note": (
                "Lifecycle: NEW/PND -> HLD -> RDY -> INV (finalized) or CAN. "
                "'Finalized not in AR' means the invoice exists in Billing but "
                "has no open-item row — the AR update has not run or failed."
            ),
        }
        if pend_trunc or orph_trunc:
            out["truncated"] = True
        if record_notes:
            out["record_notes"] = record_notes
        return out
