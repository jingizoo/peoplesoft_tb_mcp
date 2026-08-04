"""Curated tools for AP, AM and PC — the questions a financial consultant
actually asks each module, answered server-side.

Each module has a small set of questions that cover most of what its users
want, and each is a CHAIN — filter, group, compare, flag — that must not be
reassembled from per-step model rounds (the lesson every feature this
codebase learned): the chain runs here, every derived figure is in the
payload, and the model narrates.

  AP  what do we owe and to whom; is any of it overdue or stuck;
      whom did we pay, how much, when
  AM  what do we own, by category; what was added or retired; what
      is still in service
  PC  what has each project spent against its budget; which are over;
      which are stale

The same survival rules as ar.py apply: record shapes vary by site, so
optional columns adapt (record_notes disclose), required ones fail loudly,
and mixed currencies are never silently summed.
"""
from __future__ import annotations

import datetime as dt

from .engine import EngineError, TBEngine, r2


class ModuleError(RuntimeError):
    pass


def _iso(s: str) -> dt.date:
    return dt.date.fromisoformat(str(s)[:10])


class ModulePacks:
    def __init__(self, engine: TBEngine):
        self.e = engine
        self.db = engine.db

    # ---- shared helpers --------------------------------------------------
    def _bu(self, business_unit: str) -> str:
        return (business_unit or "").strip() or \
            self.e.cfg.defaults.business_unit

    def _asof(self, as_of_date: str) -> str:
        s = (as_of_date or "").strip()
        if s:
            return _iso(s).isoformat()
        return dt.date.today().isoformat()

    def _cols(self, table: str) -> set:
        return self.db.columns(table)

    def _need(self, table: str, columns: list) -> None:
        cols = self._cols(table)
        if not cols:
            raise ModuleError(
                f"{table} is not readable here (missing table or grants). "
                "Use search_records to find this site's record for it.")
        missing = [c for c in columns if c not in cols]
        if missing:
            raise ModuleError(
                f"{table} at this site is missing {', '.join(missing)} — "
                "run python scripts/diagnose_db.py to see the real shape.")

    # ---- AP: what do we owe ----------------------------------------------
    def open_payables(self, business_unit: str = "",
                      as_of_date: str = "") -> dict:
        """Open vouchers by vendor with due-date urgency and exceptions.

        The consultant's AP questions in one pass: what do we owe, to whom,
        how much of it is past due or due within a week, and what is stuck
        in the entry pipeline (recycle / unposted) where nobody is looking.
        """
        bu = self._bu(business_unit)
        asof = self._asof(as_of_date)
        p = self.db.prefix
        self._need("PS_VOUCHER", ["BUSINESS_UNIT", "VOUCHER_ID", "VENDOR_ID",
                                  "GROSS_AMT"])
        cols = self._cols("PS_VOUCHER")
        notes: list = []
        if "CLOSE_STATUS" in cols:
            open_pred = "V.CLOSE_STATUS = 'O'"
        else:
            # No close status: a voucher with no payment cross-reference is
            # the honest approximation of open, and it is disclosed.
            open_pred = (f"NOT EXISTS (SELECT 1 FROM {p}PS_PYMNT_VCHR_XREF X "
                         "WHERE X.BUSINESS_UNIT = V.BUSINESS_UNIT "
                         "AND X.VOUCHER_ID = V.VOUCHER_ID)")
            notes.append("PS_VOUCHER here has no CLOSE_STATUS; 'open' means "
                         "no payment cross-reference exists, which misses "
                         "partial payments.")
        due = "V.DUE_DT" if "DUE_DT" in cols else None
        if not due:
            notes.append("PS_VOUCHER here has no DUE_DT; overdue/due-soon "
                         "cannot be computed, amounts are grouped by vendor "
                         "only.")
        entry = "V.ENTRY_STATUS" if "ENTRY_STATUS" in cols else "NULL"
        post = "V.POST_STATUS" if "POST_STATUS" in cols else "NULL"
        cur = ("V.CURRENCY_CD" if "CURRENCY_CD" in cols else "NULL")
        name = ("N.NAME1" if self.db.has_column("PS_VENDOR", "NAME1")
                else "NULL")
        rows, _ = self.db.query(
            f"""SELECT V.VENDOR_ID AS vendor_id, {name} AS vendor,
       V.VOUCHER_ID AS voucher_id, V.GROSS_AMT AS amount,
       {cur} AS currency, {due or 'NULL'} AS due_dt,
       {entry} AS entry_status, {post} AS post_status
  FROM {p}PS_VOUCHER V
  LEFT JOIN (SELECT VENDOR_ID, MAX(NAME1) AS NAME1 FROM {p}PS_VENDOR
              GROUP BY VENDOR_ID) N ON N.VENDOR_ID = V.VENDOR_ID
 WHERE V.BUSINESS_UNIT = :bu AND {open_pred}""",
            {"bu": bu}, max_rows=10_000)

        vendors: dict = {}
        exceptions: list = []
        totals = {"open": 0.0, "overdue": 0.0, "due_7_days": 0.0}
        currencies: set = set()
        week = (_iso(asof) + dt.timedelta(days=7)).isoformat()
        for r in rows:
            amt = float(r["amount"] or 0)
            currencies.add((r.get("currency") or "").upper() or "?")
            v = vendors.setdefault(r["vendor_id"], {
                "vendor_id": r["vendor_id"], "vendor": r.get("vendor"),
                "vouchers": 0, "open_amount": 0.0, "overdue_amount": 0.0,
                "oldest_due_dt": None})
            v["vouchers"] += 1
            v["open_amount"] += amt
            totals["open"] += amt
            due_dt = str(r.get("due_dt") or "")[:10]
            if due_dt:
                if v["oldest_due_dt"] is None or due_dt < v["oldest_due_dt"]:
                    v["oldest_due_dt"] = due_dt
                if due_dt < asof:
                    v["overdue_amount"] += amt
                    totals["overdue"] += amt
                elif due_dt <= week:
                    totals["due_7_days"] += amt
            if (r.get("entry_status") == "R"
                    or r.get("post_status") == "U"):
                exceptions.append({
                    "voucher_id": r["voucher_id"],
                    "vendor_id": r["vendor_id"],
                    "amount": r2(amt),
                    "entry_status": r.get("entry_status"),
                    "post_status": r.get("post_status"),
                    "why": "recycle status" if r.get("entry_status") == "R"
                           else "not posted"})
        ranked = sorted(vendors.values(), key=lambda x: -x["open_amount"])
        for v in ranked:
            v["open_amount"] = r2(v["open_amount"])
            v["overdue_amount"] = r2(v["overdue_amount"])
        out = {
            "business_unit": bu, "as_of": asof,
            "open_total": r2(totals["open"]),
            "overdue_total": r2(totals["overdue"]),
            "due_within_7_days": r2(totals["due_7_days"]),
            "voucher_count": len(rows),
            "by_vendor": ranked,
            "pipeline_exceptions": exceptions,
            "note": ("Open vouchers only. overdue = due date before as_of; "
                     "pipeline_exceptions are vouchers in recycle or "
                     "unposted status — money that is owed but invisible to "
                     "a payment run until someone fixes the entry."),
        }
        if len(currencies - {"?"}) > 1:
            out["mixed_currencies"] = sorted(currencies - {"?"})
            out["currency_note"] = ("Vendors bill in multiple currencies; "
                                    "totals here sum face amounts and should "
                                    "be read per currency.")
        if notes:
            out["record_notes"] = notes
        return out

    # ---- AP: whom did we pay ---------------------------------------------
    def vendor_payments(self, business_unit: str = "", vendor: str = "",
                        months: int = 12, n: int = 20,
                        as_of_date: str = "") -> dict:
        """Payments made over a trailing window, ranked by vendor.

        Answers "how many payments did we make to X and for how much" — the
        question that was once refused as out of scope. vendor filters by id
        or name substring; empty ranks all vendors.
        """
        bu = self._bu(business_unit)
        asof = self._asof(as_of_date)
        since = (_iso(asof) - dt.timedelta(
            days=max(int(months or 12), 1) * 30)).isoformat()
        p = self.db.prefix
        self._need("PS_PAYMENT_TBL", ["PYMNT_ID", "VENDOR_ID", "PYMNT_DT",
                                      "PYMNT_AMT"])
        has_status = self.db.has_column("PS_PAYMENT_TBL", "PYMNT_STATUS")
        name = ("N.NAME1" if self.db.has_column("PS_VENDOR", "NAME1")
                else "NULL")
        params: dict = {"since": since}
        vend_pred = ""
        term = (vendor or "").strip()
        if term:
            params["vid"] = term.upper()
            params["vname"] = f"%{term.upper()}%"
            vend_pred = (" AND (UPPER(P.VENDOR_ID) = :vid"
                         + (" OR UPPER(N.NAME1) LIKE :vname" if name != "NULL"
                            else "") + ")")
        void_pred = " AND P.PYMNT_STATUS <> 'V'" if has_status else ""
        rows, _ = self.db.query(
            f"""SELECT P.VENDOR_ID AS vendor_id, {name} AS vendor,
       COUNT(*) AS payments, SUM(P.PYMNT_AMT) AS paid,
       MAX(P.PYMNT_DT) AS last_payment_dt
  FROM {p}PS_PAYMENT_TBL P
  LEFT JOIN (SELECT VENDOR_ID, MAX(NAME1) AS NAME1 FROM {p}PS_VENDOR
              GROUP BY VENDOR_ID) N ON N.VENDOR_ID = P.VENDOR_ID
 WHERE P.PYMNT_DT >= {self.db.date_bind('since')}{void_pred}{vend_pred}
 GROUP BY P.VENDOR_ID{', ' + name if name != 'NULL' else ''}
 ORDER BY 4 DESC""",
            params, max_rows=5_000)
        out = {
            "business_unit": bu, "window_months": int(months or 12),
            "since": since, "as_of": asof,
            "vendors": [
                {"vendor_id": r["vendor_id"], "vendor": r.get("vendor"),
                 "payments": int(r["payments"] or 0),
                 "paid": r2(float(r["paid"] or 0)),
                 "last_payment_dt": str(r.get("last_payment_dt") or "")[:10]}
                for r in rows[: max(int(n or 20), 1)]
            ],
            "vendor_count": len(rows),
            "total_paid": r2(sum(float(r["paid"] or 0) for r in rows)),
            "note": ("Payments by payment date; voided payments excluded"
                     if has_status else
                     "Payments by payment date; PS_PAYMENT_TBL here has no "
                     "PYMNT_STATUS, so voids cannot be excluded"),
        }
        if term and not rows:
            out["note"] = (f"No payments to a vendor matching {term!r} in "
                           f"the last {months} months. Widen the window, or "
                           "search_records/run_sql if the name is spelled "
                           "differently in PS_VENDOR.")
        return out

    # ---- AM: what do we own ----------------------------------------------
    def asset_register(self, business_unit: str = "", months: int = 12,
                       as_of_date: str = "") -> dict:
        """The register by category, plus this window's additions and
        retirements. Cost-based: net book value needs the depreciation
        record, and where it is absent that is said, not approximated."""
        bu = self._bu(business_unit)
        asof = self._asof(as_of_date)
        since = (_iso(asof) - dt.timedelta(
            days=max(int(months or 12), 1) * 30)).isoformat()
        p = self.db.prefix
        self._need("PS_COST", ["BUSINESS_UNIT", "ASSET_ID", "COST"])
        cols = self._cols("PS_COST")
        notes: list = []
        cat = "C.CATEGORY" if "CATEGORY" in cols else "'(all)'"
        ttype = "C.TRANS_TYPE" if "TRANS_TYPE" in cols else "NULL"
        tdt = "C.TRANS_DT" if "TRANS_DT" in cols else "NULL"
        if "TRANS_DT" not in cols:
            notes.append("PS_COST here has no TRANS_DT; additions and "
                         "retirements in the window cannot be isolated.")
        status_join = ""
        status_pred = ""
        if self._cols("PS_ASSET") and \
                self.db.has_column("PS_ASSET", "ASSET_STATUS"):
            status_join = (f" LEFT JOIN {p}PS_ASSET A ON "
                           "A.BUSINESS_UNIT = C.BUSINESS_UNIT "
                           "AND A.ASSET_ID = C.ASSET_ID")
            status_pred = ""
        rows, _ = self.db.query(
            f"""SELECT {cat} AS category, C.ASSET_ID AS asset_id,
       {ttype} AS trans_type, {tdt} AS trans_dt, C.COST AS cost
  FROM {p}PS_COST C{status_join}
 WHERE C.BUSINESS_UNIT = :bu{status_pred}""",
            {"bu": bu}, max_rows=50_000)
        by_cat: dict = {}
        additions: list = []
        retirements: list = []
        for r in rows:
            c = by_cat.setdefault(r["category"] or "(none)", {
                "category": r["category"] or "(none)",
                "assets": set(), "cost": 0.0})
            c["assets"].add(r["asset_id"])
            c["cost"] += float(r["cost"] or 0)
            t_dt = str(r.get("trans_dt") or "")[:10]
            if t_dt and since <= t_dt <= asof:
                entry = {"asset_id": r["asset_id"],
                         "category": r["category"],
                         "trans_dt": t_dt,
                         "amount": r2(float(r["cost"] or 0))}
                if r.get("trans_type") == "RET":
                    retirements.append(entry)
                elif r.get("trans_type") in ("ADD", "ADJ"):
                    additions.append(entry)
        cats = sorted(by_cat.values(), key=lambda x: -x["cost"])
        for c in cats:
            c["assets"] = len(c["assets"])
            c["cost"] = r2(c["cost"])
        out = {
            "business_unit": bu, "as_of": asof,
            "by_category": cats,
            "total_cost": r2(sum(c["cost"] for c in cats)),
            "asset_count": len({r["asset_id"] for r in rows}),
            "window_months": int(months or 12),
            "additions_in_window": additions,
            "retirements_in_window": retirements,
            "note": ("COST basis only: net book value needs the depreciation "
                     "record, which this tool does not assume. A retired "
                     "asset's negative RET row nets its category down."),
        }
        if notes:
            out["record_notes"] = notes
        return out

    # ---- PC: budget vs actual --------------------------------------------
    def project_costs(self, business_unit: str = "", project: str = "",
                      stale_after_months: int = 3,
                      as_of_date: str = "") -> dict:
        """Every project's actuals against its budget, with the two flags a
        consultant scans for first: OVER BUDGET and STALE (money budgeted,
        nothing happening). Actuals and budgets are PS_PROJ_RESOURCE rows
        distinguished by ANALYSIS_TYPE — a fact people new to PC always
        trip over, so it is handled here, not in prose."""
        bu = self._bu(business_unit)
        asof = self._asof(as_of_date)
        p = self.db.prefix
        self._need("PS_PROJ_RESOURCE", ["BUSINESS_UNIT", "PROJECT_ID",
                                        "RESOURCE_AMOUNT"])
        cols = self._cols("PS_PROJ_RESOURCE")
        atype = ("R.ANALYSIS_TYPE" if "ANALYSIS_TYPE" in cols else "'ACT'")
        tdt = "R.TRANS_DT" if "TRANS_DT" in cols else "NULL"
        name = ("P2.DESCR" if self.db.has_column("PS_PROJECT", "DESCR")
                else "NULL")
        params: dict = {"bu": bu}
        proj_pred = ""
        if (project or "").strip():
            params["proj"] = project.strip().upper()
            proj_pred = " AND UPPER(R.PROJECT_ID) = :proj"
        rows, _ = self.db.query(
            f"""SELECT R.PROJECT_ID AS project_id, {name} AS descr,
       {atype} AS analysis_type, SUM(R.RESOURCE_AMOUNT) AS amount,
       MAX({tdt}) AS last_activity
  FROM {p}PS_PROJ_RESOURCE R
  LEFT JOIN (SELECT PROJECT_ID, MAX(DESCR) AS DESCR FROM {p}PS_PROJECT
              GROUP BY PROJECT_ID) P2 ON P2.PROJECT_ID = R.PROJECT_ID
 WHERE R.BUSINESS_UNIT = :bu{proj_pred}
 GROUP BY R.PROJECT_ID, {atype}{', ' + name if name != 'NULL' else ''}""",
            params, max_rows=20_000)
        projects: dict = {}
        for r in rows:
            pr = projects.setdefault(r["project_id"], {
                "project_id": r["project_id"], "descr": r.get("descr"),
                "actual": 0.0, "budget": 0.0, "last_activity": None})
            at = str(r.get("analysis_type") or "ACT").upper()
            amt = float(r["amount"] or 0)
            if at.startswith("BUD") or at in ("BD", "TOT"):
                pr["budget"] += amt
            else:
                pr["actual"] += amt
                last = str(r.get("last_activity") or "")[:10]
                if last and (pr["last_activity"] is None
                             or last > pr["last_activity"]):
                    pr["last_activity"] = last
        stale_cut = (_iso(asof) - dt.timedelta(
            days=max(int(stale_after_months or 3), 1) * 30)).isoformat()
        out_rows: list = []
        for pr in projects.values():
            entry = {
                "project_id": pr["project_id"], "descr": pr["descr"],
                "actual": r2(pr["actual"]), "budget": r2(pr["budget"]),
                "remaining": r2(pr["budget"] - pr["actual"]),
                "last_activity": pr["last_activity"],
            }
            entry["pct_used"] = (r2(pr["actual"] / pr["budget"] * 100)
                                 if pr["budget"] else None)
            entry["over_budget"] = bool(pr["budget"]
                                        and pr["actual"] > pr["budget"])
            entry["stale"] = bool(pr["last_activity"]
                                  and pr["last_activity"] < stale_cut
                                  and pr["budget"] - pr["actual"] > 0)
            out_rows.append(entry)
        out_rows.sort(key=lambda x: -(x["actual"] or 0))
        return {
            "business_unit": bu, "as_of": asof,
            "projects": out_rows,
            "project_count": len(out_rows),
            "over_budget": [x["project_id"] for x in out_rows
                            if x["over_budget"]],
            "stale": [x["project_id"] for x in out_rows if x["stale"]],
            "stale_after_months": int(stale_after_months or 3),
            "note": ("Actuals vs budget from PS_PROJ_RESOURCE by "
                     "ANALYSIS_TYPE (BUD* = budget, everything else "
                     "actuals). pct_used is null when no budget row exists "
                     "— that is 'unbudgeted', not 0% or 100%. stale = no "
                     "actuals for the stale window AND budget remaining."),
        }
