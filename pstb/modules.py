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
import math
import re

from .db import DbError
from .engine import EngineError, TBEngine, r2


class ModuleError(RuntimeError):
    pass


def _iso_opt(day):
    """A date or None — blank strings and NULLs are honest absences."""
    if day is None or str(day).strip() in ("", "None"):
        return None
    return _iso(str(day))


def _iso(s: str) -> dt.date:
    return dt.date.fromisoformat(str(s)[:10])


class ModulePacks:
    AP_RECON_LINE_CAP = 50_000

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

    def _vendor_names(self) -> str:
        """One row per vendor id, from a record keyed on (SETID, VENDOR_ID).

        Joining PS_VENDOR directly on VENDOR_ID alone is a FAN-OUT: a
        supplier set up under SHARE and two business SETIDs multiplies every
        voucher row by three. Downstream that is not a cosmetic error —
        COUNT(*) > 1 then reports a single voucher as a duplicate payment,
        and a summed spend triples. It never shows on the bundled sample,
        which has one SETID.

        MAX(NAME1) rather than a SETID predicate on purpose: the caller has
        a business unit, not a vendor SETID, and resolving one to the other
        is a per-BU setup read this join does not need — the name is a
        label, and the id is the thing that identifies.
        """
        return (f"(SELECT VENDOR_ID, MAX(NAME1) AS NAME1 "
                f"FROM {self.db.prefix}PS_VENDOR GROUP BY VENDOR_ID)")

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
        # ACCOUNTING_DT is the best available accounting cut-off.  Some
        # installations expose only INVOICE_DT (including the bundled
        # sample), while a few custom voucher views expose ENTERED_DT.  An
        # as-of answer must not pull a voucher that did not exist in the
        # requested population yet.  Apply every available cut-off field:
        # an accounting date in range must not hide an invoice or entry date
        # that is still after the requested date.  Disclose the exact fields.
        date_columns = [c for c in ("ACCOUNTING_DT", "INVOICE_DT",
                                    "ENTERED_DT") if c in cols]
        date_pred = ""
        params = {"bu": bu}
        if date_columns:
            date_pred = "".join(
                f" AND V.{column} <= {self.db.date_bind('asof')}"
                for column in date_columns)
            params["asof"] = asof
        else:
            notes.append(
                "PS_VOUCHER here has no ACCOUNTING_DT, INVOICE_DT or "
                "ENTERED_DT; future-entered vouchers cannot be excluded "
                "from this as-of answer.")
        rows, truncated = self.db.query(
            f"""SELECT V.VENDOR_ID AS vendor_id, {name} AS vendor,
       V.VOUCHER_ID AS voucher_id, V.GROSS_AMT AS amount,
       {cur} AS currency, {due or 'NULL'} AS due_dt,
       {entry} AS entry_status, {post} AS post_status
  FROM {p}PS_VOUCHER V
  LEFT JOIN {self._vendor_names()} N ON N.VENDOR_ID = V.VENDOR_ID
 WHERE V.BUSINESS_UNIT = :bu AND {open_pred}{date_pred}""",
            params, max_rows=10_000)

        if truncated:
            notes.append(
                "More than 10,000 open vouchers matched; totals and vendor "
                "rankings cover only the returned rows and are incomplete.")

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
        today = dt.date.today().isoformat()
        point_in_time_complete = (
            bool(date_columns) and "CLOSE_STATUS" in cols and not truncated
            and asof == today)
        if point_in_time_complete:
            point_in_time_reason = "current open status is available"
        elif asof < today:
            point_in_time_reason = (
                "Voucher dates were capped at as_of, but current open/close "
                "status cannot reconstruct vouchers that were open then and "
                "closed later.")
        elif asof > today:
            point_in_time_reason = (
                f"The requested as_of {asof} is in the future; only current "
                f"voucher state through {today} is available.")
        elif not date_columns:
            point_in_time_reason = "no voucher date is available for a cut-off"
        elif "CLOSE_STATUS" not in cols:
            point_in_time_reason = (
                "CLOSE_STATUS is unavailable; payment-cross-reference "
                "existence is only a current approximation of open status.")
        else:
            point_in_time_reason = "the 10,000-row result cap was reached"
        out = {
            "business_unit": bu, "as_of": asof,
            "as_of_filter_applied": bool(date_columns),
            "as_of_basis": (
                " and ".join(f"PS_VOUCHER.{column} <= as_of"
                             for column in date_columns)
                if date_columns else "no voucher date available"),
            # CLOSE_STATUS and payment xrefs are current-state attributes.
            # Date-capping prevents later vouchers from leaking into an old
            # answer, but cannot resurrect a voucher closed after that date.
            "status_basis": ("current PS_VOUCHER.CLOSE_STATUS"
                             if "CLOSE_STATUS" in cols else
                             "current payment-cross-reference existence"),
            "point_in_time_complete": point_in_time_complete,
            "point_in_time_reason": point_in_time_reason,
            "open_total": r2(totals["open"]),
            "overdue_total": r2(totals["overdue"]),
            "due_within_7_days": r2(totals["due_7_days"]),
            "voucher_count": len(rows),
            "by_vendor": ranked,
            "pipeline_exceptions": exceptions,
            "note": ("Open vouchers only. overdue = due date before as_of; "
                     + ("vouchers dated after as_of are excluded using "
                        + ", ".join(f"PS_VOUCHER.{column}"
                                    for column in date_columns)
                        + "; " if date_columns else "")
                     + "pipeline_exceptions are vouchers in recycle or "
                       "unposted status — money that is owed but invisible to "
                       "a payment run until someone fixes the entry. Current "
                       "open/close status cannot reconstruct which vouchers "
                       "were still open on a past date."),
        }
        if len(currencies - {"?"}) > 1:
            out["mixed_currencies"] = sorted(currencies - {"?"})
            out["currency_note"] = ("Vendors bill in multiple currencies; "
                                    "totals here sum face amounts and should "
                                    "be read per currency.")
        if notes:
            out["record_notes"] = notes
        return out

    # ---- AP: accounting activity to GL journals -------------------------
    @staticmethod
    def _ap_control_accounts(value, configured=None) -> tuple[list[str], str]:
        """Return the governed account list and disclose where it came from."""
        raw = value if value not in (None, "", []) else configured
        source = "caller" if value not in (None, "", []) else (
            "config defaults.ap_control_accounts" if raw else "not supplied")
        if isinstance(raw, str):
            values = raw.split(",")
        elif isinstance(raw, (list, tuple, set)):
            values = list(raw)
        else:
            values = []
        accounts = list(dict.fromkeys(
            str(account).strip() for account in values
            if str(account).strip()))
        return accounts, source

    def reconcile_ap_to_gl(
        self,
        business_unit: str = "",
        control_accounts: str = "",
        ledger: str = "",
        fiscal_year: int = 0,
        period: int = 0,
        as_of_date: str = "",
    ) -> dict:
        """Reconcile AP accounting activity to posted GL journal activity.

        This is the APY1410/APY1420 control, not an open-liability balance
        reconstruction.  Only AP accounting lines attributed to an approved
        account and marked distributed by Journal Generator are eligible.
        They are matched to posted GL journal lines on the complete available
        Journal Generator key.  Voucher-header gross amounts and payment
        cross-references never produce a reconciliation verdict here.
        """
        bu = self._bu(business_unit)
        today = dt.date.today().isoformat()
        supplied_asof = bool((as_of_date or "").strip())
        try:
            requested_asof = self._asof(as_of_date)
        except ValueError as exc:
            raise ModuleError(
                "as_of_date must be an ISO date in YYYY-MM-DD format") from exc

        configured = getattr(self.e.cfg.defaults, "ap_control_accounts", [])
        accounts, account_source = self._ap_control_accounts(
            control_accounts, configured)
        led = self.e.resolve_ledger_for(bu, ledger)
        base_currency = (self.e.base_currency_for(bu) or "").strip().upper()
        base = {
            "status": "incomplete",
            "evaluated": False,
            "ties": None,
            "aggregate_ties": None,
            "conclusion": "not_evaluated",
            "business_unit": bu,
            "ledger": led,
            "as_of": requested_asof,
            "control_accounts": accounts,
            "control_accounts_source": account_source,
            "subledger_total": None,
            "gl_total": None,
            "gl_balance": None,
            "difference": None,
            "currency": base_currency or None,
            "tolerance": 0.01,
            "reconciling_categories": [],
        }

        def fail(reason: str, **extra) -> dict:
            return {**base, "reason": reason, **extra}

        if not accounts:
            return fail(
                "AP control accounts were not supplied. Pass the Finance-"
                "approved comma-separated account list in control_accounts; "
                "no account is assumed from its number or description.",
                cutoff={"aligned": False,
                        "reason": "control-account basis is missing"},
            )
        if len(accounts) > 25:
            return fail(
                f"{len(accounts)} control accounts were supplied; the safety "
                "cap is 25. Use the governed AP control-account set, not a "
                "broad account range."
            )
        if bool(fiscal_year) != bool(period):
            return fail(
                "fiscal_year and period must be supplied together, or both "
                "left blank to resolve from as_of_date/latest posted period."
            )
        if requested_asof > today:
            return fail(
                f"as_of_date {requested_asof} is after the current data date "
                f"{today}; future activity is not evaluated."
            )
        if not base_currency:
            return fail(
                f"The GL base currency for business unit {bu} is unavailable. "
                "AP and GL journal amounts are not compared without a governed "
                "base-currency basis."
            )

        latest_fy, latest_period = self.e.last_posted_period(bu, led)
        if not latest_fy:
            try:
                diagnosis = self.e._scope_diagnosis(bu, led, 0)
            except Exception:
                diagnosis = {}
            return {
                **base,
                "status": "no_data",
                "scope_status": "no_data",
                "reason": (
                    f"No posted GL period exists for {bu}/{led}; there is no "
                    "journal population to reconcile. This is not a zero or "
                    "pass."
                ),
                **({"scope_diagnosis": diagnosis} if diagnosis else {}),
            }

        resolved_asof = None
        if supplied_asof:
            try:
                resolved_asof = self.e.resolve_period(requested_asof)
            except EngineError as exc:
                return fail(str(exc))
        if fiscal_year and period:
            fy, per = int(fiscal_year), int(period)
            if resolved_asof and (
                    fy != int(resolved_asof["fiscal_year"])
                    or per != int(resolved_asof["period"])):
                return fail(
                    f"as_of_date {requested_asof} belongs to FY"
                    f"{resolved_asof['fiscal_year']} period "
                    f"{resolved_asof['period']}, not requested FY{fy} "
                    f"period {per}. The two sides must use one period."
                )
        elif resolved_asof:
            fy = int(resolved_asof["fiscal_year"])
            per = int(resolved_asof["period"])
        else:
            fy, per = int(latest_fy), int(latest_period)

        selected_period = None
        try:
            selected_period = next((
                row for row in self.e.list_periods(fy).get("periods", [])
                if int(row.get("period") or 0) == per
            ), None)
        except Exception:
            selected_period = None
        period_begin = (str(selected_period.get("begin_dt") or "")[:10]
                        if selected_period else "")
        period_end = (str(selected_period.get("end_dt") or "")[:10]
                      if selected_period else "")
        if not period_begin or not period_end:
            return fail(
                f"Fiscal-calendar dates for FY{fy} period {per} are not "
                "available. AP accounting activity cannot be cut to the same "
                "date basis as GL journal activity."
            )
        cutoff_date = requested_asof if supplied_asof else min(period_end, today)
        if cutoff_date < period_begin or cutoff_date > period_end:
            return fail(
                f"Cutoff {cutoff_date} is outside FY{fy} period {per} "
                f"({period_begin} through {period_end})."
            )

        cutoff = {
            "aligned": True,
            "fiscal_year": fy,
            "period": per,
            "period_begin": period_begin,
            "period_end": period_end,
            "through_date": cutoff_date,
            "date_basis": (
                "AP: ACCOUNTING_DT within selected period through cutoff; "
                "GL: posted journal header FY/period and JOURNAL_DATE through "
                "the same cutoff"
            ),
            "status_basis": (
                "AP POST_STATUS_AP='P' and GL_DISTRIB_STATUS='D'; GL "
                "JRNL_HDR_STATUS='P'"
            ),
            "reason": (
                "Both populations use the selected business unit, ledger, "
                "accounts, fiscal period and through-date."
            ),
        }
        base.update({
            "fiscal_year": fy,
            "period": per,
            "latest_posted": {"fiscal_year": int(latest_fy),
                              "period": int(latest_period)},
            "as_of": cutoff_date,
            "cutoff": cutoff,
        })

        # Resolve from live metadata. A PeopleTools physical mapping wins,
        # followed by one unique live-catalog suffix (including a company
        # prefix). The delivered PS_ convention is only an exact-catalog
        # fallback when richer metadata is unavailable; ambiguity never picks
        # the first sort result.
        source = ""
        source_basis = ""
        source_columns: set = set()
        mapped = ""
        try:
            mapping_rows, _ = self.db.query(
                "SELECT SQLTABLENAME AS sqltablename "
                f"FROM {self.db.prefix}PSRECDEFN "
                "WHERE UPPER(RECNAME) = :rec",
                {"rec": "VCHR_ACCTG_LINE"},
                max_rows=2,
            )
            if len(mapping_rows) == 1:
                mapped = str(mapping_rows[0].get("sqltablename")
                             or "").strip().upper()
            elif len(mapping_rows) > 1:
                return fail(
                    "PSRECDEFN returned more than one VCHR_ACCTG_LINE "
                    "definition; no physical object is selected."
                )
        except DbError:
            pass
        if mapped and not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", mapped):
            return fail(
                "PSRECDEFN.SQLTABLENAME for VCHR_ACCTG_LINE is not a safe "
                "single catalog identifier; no fallback is attempted.",
                accounting_source=mapped,
                accounting_source_basis="PSRECDEFN.SQLTABLENAME",
            )
        if mapped and re.fullmatch(r"[A-Z][A-Z0-9_$#]*", mapped):
            columns = self._cols(mapped)
            if columns:
                source, source_columns = mapped, columns
                source_basis = "PSRECDEFN.SQLTABLENAME"
            else:
                return fail(
                    f"PSRECDEFN maps VCHR_ACCTG_LINE to {mapped}, but that "
                    "physical object is not readable. The tool will not fall "
                    "back to a similarly named table.",
                    accounting_source=mapped,
                    accounting_source_basis="PSRECDEFN.SQLTABLENAME",
                )

        suffix_candidates: list[str] = []
        if not source:
            try:
                for row in self.e.list_tables("VCHR_ACCTG_LINE").get(
                        "tables", []):
                    name = str(row.get("table_name") or "").strip().upper()
                    if (name == "VCHR_ACCTG_LINE"
                            or name.endswith("_VCHR_ACCTG_LINE")):
                        suffix_candidates.append(name)
            except Exception:
                pass
            suffix_candidates = list(dict.fromkeys(suffix_candidates))
        if not source and len(suffix_candidates) > 1:
            return fail(
                "More than one physical object matches VCHR_ACCTG_LINE and "
                "PeopleTools metadata did not identify one governed mapping. "
                "No object is chosen by prefix or sort order.",
                accounting_source_candidates=suffix_candidates,
            )
        candidates = suffix_candidates or ["PS_VCHR_ACCTG_LINE"]
        for candidate in candidates if not source else []:
            if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", candidate):
                continue
            columns = self._cols(candidate)
            if columns:
                source, source_columns = candidate, columns
                source_basis = (
                    "unique live-catalog suffix"
                    if suffix_candidates else
                    "exact delivered catalog fallback; richer mapping unavailable"
                )
                break
        if not source:
            return fail(
                "No unambiguous readable AP accounting-line source was "
                "found for VCHR_ACCTG_LINE. Voucher headers and payment "
                "cross-references are not an account-attributed substitute. "
                "Run the delivered APY1410/APY1420 reconciliation, or "
                "APY1400/APY1405 for open liability."
            )
        base.update({
            "accounting_source": source,
            "accounting_source_basis": source_basis,
        })

        required_ap = {
            "BUSINESS_UNIT", "BUSINESS_UNIT_GL", "ACCOUNT", "ACCOUNTING_DT",
            "MONETARY_AMOUNT", "CURRENCY_CD", "POST_STATUS_AP",
            "GL_DISTRIB_STATUS", "JOURNAL_ID", "JOURNAL_DATE",
            "UNPOST_SEQ", "JOURNAL_LINE", "LEDGER", "FISCAL_YEAR",
            "ACCOUNTING_PERIOD", "POSTING_PROCESS",
        }
        missing_ap = sorted(required_ap - source_columns)
        if missing_ap:
            return fail(
                f"{source} is missing the account/Journal Generator fields "
                f"required for an exact AP-to-GL activity reconciliation: "
                f"{', '.join(missing_ap)}. No voucher-header approximation "
                "is made. Use APY1410/APY1420 (or APY1400/APY1405 for open "
                "liability).",
                accounting_source=source,
                accounting_source_basis=source_basis,
                missing_columns=missing_ap,
            )

        header_columns = self._cols("PS_JRNL_HEADER")
        line_columns = self._cols("PS_JRNL_LN")
        required_header = {
            "BUSINESS_UNIT", "JOURNAL_ID", "JOURNAL_DATE", "UNPOST_SEQ",
            "JRNL_HDR_STATUS", "FISCAL_YEAR", "ACCOUNTING_PERIOD",
        }
        required_line = {
            "BUSINESS_UNIT", "JOURNAL_ID", "JOURNAL_DATE", "UNPOST_SEQ",
            "JOURNAL_LINE", "LEDGER", "ACCOUNT", "CURRENCY_CD",
            "MONETARY_AMOUNT",
        }
        missing_gl = [
            *(f"PS_JRNL_HEADER.{column}"
              for column in sorted(required_header - header_columns)),
            *(f"PS_JRNL_LN.{column}"
              for column in sorted(required_line - line_columns)),
        ]
        if missing_gl:
            return fail(
                "The exact posted GL journal population is unavailable: "
                + ", ".join(missing_gl)
                + ". No PS_LEDGER ending balance is substituted for period "
                  "journal activity. Use APY1410/APY1420.",
                accounting_source=source,
                accounting_source_basis=source_basis,
                missing_columns=missing_gl,
            )

        params: dict = {
            "bu": bu,
            "led": led,
            "fy": fy,
            "per": per,
            "begin": period_begin,
            "cutoff": cutoff_date,
        }
        account_binds = []
        for index, account in enumerate(accounts):
            name = f"acct_{index}"
            params[name] = account
            account_binds.append(f":{name}")
        accounts_sql = ", ".join(account_binds)
        p = self.db.prefix
        configured_cap = getattr(
            self.e.cfg.tools, "ap_reconciliation_line_cap",
            self.AP_RECON_LINE_CAP)
        try:
            line_cap = max(1, min(int(configured_cap), 100_000))
        except (TypeError, ValueError):
            line_cap = self.AP_RECON_LINE_CAP
        try:
            ap_rows, ap_truncated = self.db.query(
                "SELECT A.ACCOUNT AS account, A.ACCOUNTING_DT AS accounting_dt, "
                "A.MONETARY_AMOUNT AS amount, A.CURRENCY_CD AS currency, "
                "A.POST_STATUS_AP AS post_status_ap, "
                "A.GL_DISTRIB_STATUS AS gl_distrib_status, "
                "A.POSTING_PROCESS AS posting_process, "
                "A.BUSINESS_UNIT_GL AS business_unit_gl, "
                "A.JOURNAL_ID AS journal_id, A.JOURNAL_DATE AS journal_date, "
                "A.UNPOST_SEQ AS unpost_seq, A.JOURNAL_LINE AS journal_line, "
                "A.LEDGER AS ledger "
                f"FROM {p}{source} A "
                "WHERE A.BUSINESS_UNIT = :bu AND A.BUSINESS_UNIT_GL = :bu "
                "AND A.FISCAL_YEAR = :fy AND A.ACCOUNTING_PERIOD = :per "
                "AND (A.LEDGER = :led OR A.LEDGER IS NULL OR A.LEDGER = '') "
                f"AND A.ACCOUNT IN ({accounts_sql}) "
                "AND (A.ACCOUNTING_DT IS NULL OR ("
                f"A.ACCOUNTING_DT >= {self.db.date_bind('begin')} "
                f"AND A.ACCOUNTING_DT <= {self.db.date_bind('cutoff')}))",
                params,
                max_rows=line_cap,
            )
            gl_rows, gl_truncated = self.db.query(
                "SELECT L.ACCOUNT AS account, L.MONETARY_AMOUNT AS amount, "
                "L.CURRENCY_CD AS currency, H.JRNL_HDR_STATUS AS header_status, "
                "L.BUSINESS_UNIT AS business_unit_gl, "
                "L.JOURNAL_ID AS journal_id, L.JOURNAL_DATE AS journal_date, "
                "L.UNPOST_SEQ AS unpost_seq, L.JOURNAL_LINE AS journal_line, "
                "L.LEDGER AS ledger "
                f"FROM {p}PS_JRNL_LN L JOIN {p}PS_JRNL_HEADER H ON "
                "H.BUSINESS_UNIT = L.BUSINESS_UNIT "
                "AND H.JOURNAL_ID = L.JOURNAL_ID "
                "AND H.JOURNAL_DATE = L.JOURNAL_DATE "
                "AND H.UNPOST_SEQ = L.UNPOST_SEQ "
                "WHERE H.BUSINESS_UNIT = :bu AND H.FISCAL_YEAR = :fy "
                "AND H.ACCOUNTING_PERIOD = :per AND L.LEDGER = :led "
                f"AND L.ACCOUNT IN ({accounts_sql}) "
                f"AND H.JOURNAL_DATE <= {self.db.date_bind('cutoff')}",
                params,
                max_rows=line_cap,
            )
        except DbError as exc:
            return fail(
                "The AP/GL journal populations could not be read safely: "
                f"{exc}. No reconciliation verdict is produced.",
                accounting_source=source,
            )
        if ap_truncated or gl_truncated:
            return fail(
                f"The selected period exceeds the {line_cap:,}-"
                "row safety cap on AP or GL journal lines. Results are "
                "partial, so no total or tie is reported; narrow the account "
                "set/date or run APY1410/APY1420.",
                accounting_source=source,
                population={
                    "status": "partial",
                    "ap_rows_returned": len(ap_rows),
                    "gl_rows_returned": len(gl_rows),
                    "ap_truncated": ap_truncated,
                    "gl_truncated": gl_truncated,
                },
            )

        def text_value(row: dict, name: str) -> str:
            return str(row.get(name) or "").strip().upper()

        def numeric_amount(row: dict, source_name: str) -> float:
            raw = row.get("amount")
            if raw is None:
                raise ValueError(
                    f"{source_name}.MONETARY_AMOUNT is null")
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{source_name}.MONETARY_AMOUNT is nonnumeric") from exc
            if not math.isfinite(value):
                raise ValueError(
                    f"{source_name}.MONETARY_AMOUNT is not finite")
            return value

        def key_for(row: dict) -> tuple:
            return (
                text_value(row, "business_unit_gl"),
                text_value(row, "journal_id"),
                str(row.get("journal_date") or "")[:10],
                str(row.get("journal_line") if row.get("journal_line") is not None
                    else ""),
                text_value(row, "ledger"),
                text_value(row, "account"),
            )

        def valid_journal_line(row: dict) -> bool:
            try:
                return int(row.get("journal_line")) > 0
            except (TypeError, ValueError):
                return False

        ap_eligible: list[dict] = []
        ap_status_groups: dict[tuple, dict] = {}
        missing_keys: list[dict] = []
        ledger_ambiguous: list[dict] = []
        for row in ap_rows:
            accounting_date = str(row.get("accounting_dt") or "")[:10]
            try:
                parsed_accounting_date = _iso(accounting_date).isoformat()
            except (TypeError, ValueError):
                return fail(
                    f"{source}.ACCOUNTING_DT is blank or invalid inside the "
                    "selected fiscal population; no cut-off-safe AP total is "
                    "reported.", accounting_source=source)
            if not (period_begin <= parsed_accounting_date <= cutoff_date):
                return fail(
                    f"{source}.ACCOUNTING_DT {parsed_accounting_date} is "
                    "outside the selected cut-off despite its fiscal-year/"
                    "period classification; no AP total is reported.",
                    accounting_source=source)
            post_status = text_value(row, "post_status_ap")
            distrib_status = text_value(row, "gl_distrib_status")
            ledger_value = text_value(row, "ledger")
            status_key = (post_status or "(blank)",
                          distrib_status or "(blank)",
                          ledger_value or "(blank)",
                          text_value(row, "posting_process") or "(blank)")
            status_group = ap_status_groups.setdefault(status_key, {
                "post_status_ap": status_key[0],
                "gl_distrib_status": status_key[1],
                "ledger": status_key[2],
                "posting_process": status_key[3],
                "line_count": 0,
                "signed_amount": 0.0,
            })
            status_group["line_count"] += 1
            try:
                row["_numeric_amount"] = numeric_amount(row, source)
                status_group["signed_amount"] += row["_numeric_amount"]
            except ValueError as exc:
                return fail(
                    f"{exc}; "
                    "no AP total is reported.", accounting_source=source)
            if post_status != "P" or distrib_status != "D":
                continue
            if not ledger_value:
                ledger_ambiguous.append({
                    "account": text_value(row, "account"),
                    "ledger": ledger_value or None,
                    "journal_id": text_value(row, "journal_id") or None,
                })
                continue
            key = key_for(row)
            if any(value == "" for value in key) or not valid_journal_line(row):
                missing_keys.append({
                    "account": text_value(row, "account"),
                    "journal_id": text_value(row, "journal_id") or None,
                    "journal_date": str(row.get("journal_date") or "")[:10] or None,
                    "journal_line": row.get("journal_line"),
                })
                continue
            ap_eligible.append(row)

        if ledger_ambiguous:
            return fail(
                "Distributed AP accounting lines have a blank ledger, so "
                "they cannot be assigned to the selected ledger "
                f"{led} without assumption.",
                accounting_source=source,
                ledger_evidence_issues=ledger_ambiguous[:20],
            )
        if missing_keys:
            return fail(
                "Distributed AP accounting lines lack a complete Journal "
                "Generator drill-down key. An aggregate amount can coincide "
                "without proving transfer to GL, so no tie is reported. Run "
                "APY1410/APY1420.",
                accounting_source=source,
                journal_key_issues=missing_keys[:20],
            )

        try:
            for row in gl_rows:
                row["_numeric_amount"] = numeric_amount(row, "PS_JRNL_LN")
        except ValueError as exc:
            return fail(
                f"{exc}; no GL total is reported.",
                accounting_source=source,
            )

        posted_gl = [row for row in gl_rows
                     if text_value(row, "header_status") == "P"]
        nonposted_gl = [row for row in gl_rows
                        if text_value(row, "header_status") != "P"]
        if not ap_eligible and not posted_gl and (ap_rows or gl_rows):
            return fail(
                "Rows exist in the selected scope, but none form an eligible "
                "distributed-AP-to-posted-GL population. A zero from excluded "
                "posting statuses is not a reconciliation pass.",
                accounting_source=source,
                population={
                    "status": "incomplete",
                    "ap_rows_read": len(ap_rows),
                    "gl_rows_read": len(gl_rows),
                    "ap_status_groups": [
                        {**group,
                         "signed_amount": r2(group["signed_amount"])}
                        for _, group in sorted(ap_status_groups.items())
                    ],
                },
            )
        ap_currencies = {text_value(row, "currency") for row in ap_eligible}
        gl_currencies = {text_value(row, "currency") for row in posted_gl}
        observed_currencies = ap_currencies | gl_currencies
        if "" in observed_currencies or (
                observed_currencies and observed_currencies != {base_currency}):
            return fail(
                "AP and posted GL journal activity is not one governed base-"
                f"currency population. GL base is {base_currency}; observed "
                f"currencies are {', '.join(sorted(c or '(blank)' for c in observed_currencies))}. "
                "Mixed or blank currencies are not summed or translated.",
                accounting_source=source,
                mixed_currencies=sorted(c or "(blank)"
                                        for c in observed_currencies),
            )

        ap_by_key: dict[tuple, dict] = {}
        for row in ap_eligible:
            key = key_for(row)
            bucket = ap_by_key.setdefault(key, {"amount": 0.0,
                                                "source_line_count": 0})
            bucket["amount"] += row["_numeric_amount"]
            bucket["source_line_count"] += 1

        gl_by_key: dict[tuple, dict] = {}
        duplicate_gl_keys: list[dict] = []
        for row in posted_gl:
            key = key_for(row)
            if any(value == "" for value in key) or not valid_journal_line(row):
                return fail(
                    "A posted GL control-account journal line lacks the exact "
                    "journal key required for reconciliation.",
                    accounting_source=source,
                )
            if key in gl_by_key:
                duplicate_gl_keys.append({
                    "journal_id": key[1], "journal_date": key[2],
                    "journal_line": key[3], "account": key[5],
                })
            else:
                gl_by_key[key] = {"amount": row["_numeric_amount"]}
        if duplicate_gl_keys:
            return fail(
                "Posted GL journal keys are not unique, so an exact AP-to-GL "
                "match would fan out. No tie is reported.",
                accounting_source=source,
                duplicate_gl_keys=duplicate_gl_keys[:20],
            )

        ap_total = r2(sum(row["amount"] for row in ap_by_key.values()))
        gl_total = r2(sum(row["amount"] for row in gl_by_key.values()))
        if not ap_rows and not gl_rows:
            return {
                **base,
                "status": "no_data",
                "scope_status": "no_data",
                "accounting_source": source,
                "reason": (
                    "No AP accounting lines or GL control-account journal "
                    "lines exist in the selected period/cutoff. This is not "
                    "a reconciliation pass or a zero balance."
                ),
                "population": {"status": "no_data", "ap_rows": 0,
                               "gl_rows": 0},
            }

        ap_keys, gl_keys = set(ap_by_key), set(gl_by_key)
        only_ap = sorted(ap_keys - gl_keys)
        only_gl = sorted(gl_keys - ap_keys)
        amount_mismatches = []
        for key in sorted(ap_keys & gl_keys):
            ap_amount = r2(ap_by_key[key]["amount"])
            gl_amount = r2(gl_by_key[key]["amount"])
            if abs(ap_amount - gl_amount) >= 0.01:
                amount_mismatches.append({
                    "journal_id": key[1], "journal_date": key[2],
                    "journal_line": key[3], "account": key[5],
                    "ap_amount": ap_amount,
                    "gl_amount": gl_amount,
                    "difference": r2(ap_amount - gl_amount),
                })

        pending_ap_groups = []
        outside_ap_groups = []
        for (post_status, distrib_status, row_ledger, _process), group in sorted(
                ap_status_groups.items()):
            item = {**group, "signed_amount": r2(group["signed_amount"])}
            if post_status == "P" and distrib_status != "D":
                pending_ap_groups.append(item)
            elif post_status != "P":
                outside_ap_groups.append(item)

        categories: list[dict] = []
        if only_ap:
            only_ap_amount = r2(sum(ap_by_key[key]["amount"]
                                    for key in only_ap))
            categories.append({
                "category": "distributed_ap_without_posted_gl_key",
                "evidence": "observed",
                "key_count": len(only_ap),
                "signed_amount": only_ap_amount,
                "amount": only_ap_amount,
                "included_in_subledger_total": True,
                "note": (
                    "Journal Generator keys exist on AP accounting lines but "
                    "no posted GL line with the same complete key is in scope."
                ),
            })
        if only_gl:
            only_gl_amount = r2(sum(gl_by_key[key]["amount"]
                                    for key in only_gl))
            categories.append({
                "category": "posted_gl_without_ap_accounting_key",
                "evidence": "observed",
                "key_count": len(only_gl),
                "signed_amount": only_gl_amount,
                "amount": only_gl_amount,
                "included_in_gl_total": True,
                "note": (
                    "A posted control-account journal line has no matching AP "
                    "accounting key. This may be a direct/reclassification "
                    "journal or a mapping gap; this tool did not attribute "
                    "the cause."
                ),
            })
        if amount_mismatches:
            categories.append({
                "category": "matched_journal_key_amount_difference",
                "evidence": "observed",
                "key_count": len(amount_mismatches),
                "rows": amount_mismatches[:20],
            })
        if pending_ap_groups:
            categories.append({
                "category": "ap_accounting_not_distributed_to_gl",
                "evidence": "observed",
                "status_groups": pending_ap_groups,
                "included_in_subledger_total": False,
                "note": (
                    "These AP-posted lines are not GL_DISTRIB_STATUS D and "
                    "remain outside the distributed reconciliation population."
                ),
            })
        if outside_ap_groups:
            categories.append({
                "category": "ap_accounting_not_ap_posted",
                "evidence": "observed",
                "status_groups": outside_ap_groups,
                "included_in_subledger_total": False,
            })
        if nonposted_gl:
            nonposted_gl_amount = r2(sum(row["_numeric_amount"]
                                         for row in nonposted_gl))
            categories.append({
                "category": "control_account_journals_not_gl_posted",
                "evidence": "observed",
                "line_count": len(nonposted_gl),
                "signed_amount": nonposted_gl_amount,
                "amount": nonposted_gl_amount,
                "header_statuses": sorted({
                    text_value(row, "header_status") or "(blank)"
                    for row in nonposted_gl
                }),
                "included_in_gl_total": False,
            })

        difference = r2(ap_total - gl_total)
        aggregate_ties = abs(difference) < 0.01
        key_complete = not (only_ap or only_gl or amount_mismatches
                            or pending_ap_groups or outside_ap_groups
                            or nonposted_gl)
        ties = bool(aggregate_ties and key_complete)
        if difference:
            categories.append({
                "category": "ap_gl_activity_residual",
                "evidence": "observed_residual",
                "signed_amount": difference,
                "amount": difference,
                "calculation": "distributed AP activity - posted GL activity",
                "note": "The amount is observed; its cause is not asserted.",
            })

        population = {
            "status": "complete",
            "basis": (
                f"All {source} rows in FY{fy} period {per} through "
                f"{cutoff_date} for the selected AP/GL business unit and "
                "control accounts; the compared AP total includes every "
                "posting process whose row is AP-posted, GL-distributed and "
                f"assigned to ledger {led}."
            ),
            "date_basis": cutoff["date_basis"],
            "status_basis": cutoff["status_basis"],
            "accounting_source": source,
            "ap_rows_read": len(ap_rows),
            "ap_distributed_source_lines": sum(
                row["source_line_count"] for row in ap_by_key.values()),
            "ap_distributed_journal_keys": len(ap_by_key),
            "gl_rows_read": len(gl_rows),
            "gl_posted_journal_lines": len(posted_gl),
            "matched_journal_keys": len(ap_keys & gl_keys),
            "unmatched_ap_keys": len(only_ap),
            "unmatched_gl_keys": len(only_gl),
            "journal_key": [
                "BUSINESS_UNIT_GL", "JOURNAL_ID", "JOURNAL_DATE",
                "JOURNAL_LINE", "LEDGER", "ACCOUNT",
            ],
            "status_groups": [
                {**group, "signed_amount": r2(group["signed_amount"])}
                for _, group in sorted(ap_status_groups.items())
            ],
            "truncated": False,
            "line_safety_cap": line_cap,
        }
        conclusion = (
            "reconciled"
            if ties else
            "aggregate_tie_with_key_or_status_exceptions"
            if aggregate_ties else
            "difference"
        )
        reason = (
            "Distributed AP accounting activity matches posted GL journal "
            "activity on every available Journal Generator key within 0.01."
            if ties else
            "The aggregate amounts are equal, but exact journal-key or "
            "posting-status exceptions prevent a clean reconciliation."
            if aggregate_ties else
            "Distributed AP accounting activity does not reconcile to posted "
            "GL journal activity. Observed categories identify where to "
            "investigate; they do not assert a cause."
        )
        return {
            **base,
            "status": "evaluated",
            "evaluated": True,
            "ties": ties,
            "aggregate_ties": aggregate_ties,
            "conclusion": conclusion,
            "accounting_source": source,
            "subledger_total": ap_total,
            "gl_total": gl_total,
            # Compatibility alias for the evidence gate/controller card. The
            # amount_basis below is authoritative: this is signed period
            # activity, not an ending balance despite the legacy key name.
            "gl_balance": gl_total,
            "difference": difference,
            "currency_basis": (
                f"{base_currency} base-currency MONETARY_AMOUNT on both AP "
                "accounting lines and GL journal lines; mixed or blank "
                "currency populations fail closed"
            ),
            "gl_sign_basis": (
                "Signed selected-period activity: debits positive, credits "
                "negative; gl_balance is a compatibility alias for gl_total, "
                "not an ending balance"
            ),
            "population": population,
            "reconciling_categories": categories,
            "reason": reason,
            "amount_basis": (
                "Signed base-currency period activity: debits positive, "
                "credits negative. This is not an AP open-liability ending "
                "balance. Use APY1400/APY1405 for that separate control."
            ),
            "report_precedent": (
                "PeopleSoft APY1410/APY1420 journal/account reconciliation; "
                "APY1400/APY1405 remains the open-liability reconciliation"
            ),
        }

    # ---- AP: whom did we pay ---------------------------------------------
    def vendor_intelligence(self, business_unit: str = "",
                            months: int = 12, n: int = 20,
                            as_of_date: str = "") -> dict:
        """Top vendors with HOW WE PAY them — terms versus actual behavior
        — plus computed observations, mirror of customer intelligence.

        Days early/late per payment = payment date minus voucher due date
        via the payment cross-reference, weighted by amount and computed
        in Python. Negative = we pay early (cash handed over sooner than
        required); positive = late (relationship and terms risk). Both are
        observations with figures, never advice from thin air.
        """
        bu = self._bu(business_unit)
        asof = self._asof(as_of_date)
        since = (_iso(asof) - dt.timedelta(
            days=max(int(months or 12), 1) * 30)).isoformat()
        self._need("PS_VOUCHER", ["BUSINESS_UNIT", "VOUCHER_ID",
                                  "VENDOR_ID", "GROSS_AMT"])
        # This tool reads three records, and only one of them was ever
        # checked. On a site without the payment cross-reference the SELECT
        # below raised ORA-00942 with no remedy in it, while every sibling
        # in this file answers a missing record by name.
        self._need("PS_PYMNT_VCHR_XREF", ["BUSINESS_UNIT", "VOUCHER_ID",
                                          "PYMNT_ID", "PAID_AMT"])
        self._need("PS_PAYMENT_TBL", ["PYMNT_ID", "PYMNT_DT"])
        p = self.db.prefix
        notes: list = []
        # Optional columns, guarded the way open_payables guards its own.
        # Timing is the POINT of this tool, so losing DUE_DT has to be said
        # out loud rather than reported as "no vendor pays early or late".
        if "DUE_DT" in self._cols("PS_VOUCHER"):
            due_sel = "V.DUE_DT"
        else:
            due_sel = "NULL"
            notes.append("PS_VOUCHER here has no DUE_DT; days early/late "
                         "cannot be computed, so avg_days_vs_due is null "
                         "for every vendor and the early/late observations "
                         "are withheld — not evidence that payment timing "
                         "is on time.")
        if self.db.has_column("PS_VENDOR", "NAME1"):
            name_sel = "N.NAME1"
        else:
            name_sel = "NULL"
            notes.append("PS_VENDOR here has no NAME1; vendors are "
                         "identified by ID only.")
        if self.db.has_column("PS_PAYMENT_TBL", "CURRENCY_CD"):
            cur_sel = "P.CURRENCY_CD"
        else:
            cur_sel = "NULL"
            notes.append("PS_PAYMENT_TBL here has no CURRENCY_CD; amounts "
                         "are assumed to be in one currency.")
        rows, _ = self.db.query(
            f"SELECT V.VENDOR_ID AS vendor_id, {name_sel} AS vendor, "
            f"P.PYMNT_DT AS paid_dt, {due_sel} AS due_dt, "
            f"X.PAID_AMT AS amount, {cur_sel} AS currency "
            f"FROM {p}PS_PYMNT_VCHR_XREF X "
            f"JOIN {p}PS_VOUCHER V ON V.BUSINESS_UNIT = X.BUSINESS_UNIT "
            f"AND V.VOUCHER_ID = X.VOUCHER_ID "
            f"JOIN {p}PS_PAYMENT_TBL P ON P.PYMNT_ID = X.PYMNT_ID "
            f"LEFT JOIN {self._vendor_names()} N "
            f"ON N.VENDOR_ID = V.VENDOR_ID "
            f"WHERE V.BUSINESS_UNIT = :bu "
            f"AND P.PYMNT_DT >= {self.db.date_bind('since')}",
            {"bu": bu, "since": since}, max_rows=10_000)
        vendors: dict = {}
        for r in rows:
            v = vendors.setdefault(str(r["vendor_id"]), {
                "vendor_id": str(r["vendor_id"]),
                "vendor": str(r["vendor"] or ""),
                "payments": 0, "paid_total": 0.0, "currency":
                str(r["currency"] or ""), "timing_weight": 0.0,
                "timed_amount": 0.0})
            amt = float(r["amount"] or 0)
            v["payments"] += 1
            v["paid_total"] = r2(v["paid_total"] + amt)
            paid, due = _iso_opt(r.get("paid_dt")), _iso_opt(r.get("due_dt"))
            if paid and due and amt:
                v["timing_weight"] += (paid - due).days * amt
                v["timed_amount"] += amt
        ranked = sorted(vendors.values(), key=lambda v: -v["paid_total"])
        ranked = ranked[:max(int(n or 20), 1)]
        total_paid = sum(v["paid_total"] for v in vendors.values()) or 1.0
        observations = []
        for v in ranked:
            v["share_pct"] = r2(v["paid_total"] / total_paid * 100.0)
            v["avg_days_vs_due"] = (
                int(round(v["timing_weight"] / v["timed_amount"]))
                if v["timed_amount"] else None)
            del v["timing_weight"], v["timed_amount"]
            days = v["avg_days_vs_due"]
            if days is not None and days <= -5:
                observations.append({
                    "kind": "early_payment", "vendor_id": v["vendor_id"],
                    "avg_days_vs_due": days, "paid_total": v["paid_total"],
                    "text": (f"{v['vendor'] or v['vendor_id']} is paid on "
                             f"average {-days} days BEFORE due across "
                             f"{v['paid_total']:,.2f} — cash handed over "
                             "early; if no early-pay discount exists, the "
                             "terms are a free lever.")})
            if days is not None and days >= 10:
                observations.append({
                    "kind": "late_payment", "vendor_id": v["vendor_id"],
                    "avg_days_vs_due": days, "paid_total": v["paid_total"],
                    "text": (f"{v['vendor'] or v['vendor_id']} is paid on "
                             f"average {days} days AFTER due — a supplier "
                             "relationship and terms-compliance risk worth "
                             "a look.")})
        if ranked and ranked[0]["share_pct"] >= 40:
            observations.append({
                "kind": "concentration", "vendor_id": ranked[0]["vendor_id"],
                "share_pct": ranked[0]["share_pct"],
                "text": (f"{ranked[0]['vendor'] or ranked[0]['vendor_id']} "
                         f"receives {ranked[0]['share_pct']}% of payment "
                         "value — supply concentration worth a second "
                         "source conversation.")})
        # Append, never reassign: the shape notes gathered before the query
        # say which columns this site does not have, and rebinding the list
        # here threw all of them away.
        if not self._cols("PS_VENDOR_ADDR"):
            notes.append("PS_VENDOR_ADDR not present — vendor geography "
                         "is not available at this site.")
        return {
            "business_unit": bu, "since": since, "as_of": asof,
            "window_months": int(months or 12),
            "vendors": ranked, "observations": observations,
            **({"record_notes": notes} if notes else {}),
            "note": ("Timing = payment date minus voucher due date via the "
                     "payment cross-reference, amount-weighted. "
                     "Observations are computed, never invented."),
        }

    def search_vendors(self, query: str = "", limit: int = 25,
                       business_unit: str = "") -> dict:
        """Find a supplier by id or name, and say if it is part of a group.

        The AP counterpart to search_customers, and the thing the network's
        refusal message needs to be able to name. Resolving a NAME to an id
        is also the moment to say that id is part of something bigger:
        without it, "how much do we owe Ridgeline" is answered for one
        legal entity out of three and looks complete.
        """
        bu = self._bu(business_unit)
        setid = self.e.resolve_setid(bu, "VENDOR")
        p = self.db.prefix
        cols = self._cols("PS_VENDOR")
        name_c = "NAME1" if (not cols or "NAME1" in cols) else ""
        stat_c = ("VENDOR_STATUS" if (not cols or "VENDOR_STATUS" in cols)
                  else "")
        corp_c = next((c for c in ("CORPORATE_VENDOR", "CORPORATE_VNDR_ID")
                       if not cols or c in cols), "")
        notes: list = []
        if cols and not name_c:
            notes.append("PS_VENDOR here has no NAME1; suppliers are "
                         "identified by ID only and name search is not "
                         "available.")
        if cols and not corp_c:
            notes.append("PS_VENDOR here records no corporate supplier "
                         "column, so whether these suppliers belong to one "
                         "group is UNKNOWN — not answered as no.")
        term = (query or "").strip().upper()
        params = {"setid": setid, "bu": bu,
                  "q": f"%{term}%", "qa": f"{term}%"}
        # Withdrawn rather than left to match nothing: UPPER(NULL) LIKE :q
        # is false for every row, which would report "no such supplier"
        # about suppliers that plainly exist.
        name_pred = f"UPPER(V.{name_c}) LIKE :q OR " if name_c else ""
        name_sel = f"V.{name_c}" if name_c else "NULL"
        stat_sel = f"V.{stat_c}" if stat_c else "NULL"
        corp_sel = f"V.{corp_c}" if corp_c else "NULL"
        rows, truncated = self.db.query(
            f"SELECT V.VENDOR_ID AS vendor_id, {name_sel} AS name, "
            f"{stat_sel} AS status, {corp_sel} AS corporate_parent, "
            "COALESCE(O.owed, 0) AS open_payables "
            f"FROM {p}PS_VENDOR V "
            f"LEFT JOIN (SELECT VENDOR_ID, SUM(GROSS_AMT) AS owed "
            f"FROM {p}PS_VOUCHER WHERE BUSINESS_UNIT = :bu "
            "GROUP BY VENDOR_ID) O ON O.VENDOR_ID = V.VENDOR_ID "
            f"WHERE V.SETID = :setid AND ({name_pred}"
            "UPPER(V.VENDOR_ID) LIKE :qa) ORDER BY V.VENDOR_ID",
            params, max_rows=max(int(limit or 25), 1))
        subs: list = []
        for r in rows:
            r["open_payables"] = r2(float(r["open_payables"] or 0))
            parent = str(r.get("corporate_parent") or "")
            r["corporate_parent"] = parent
            if parent and parent != r["vendor_id"]:
                subs.append(r["vendor_id"])
        heads = self._vendor_family_heads(
            setid, [r["vendor_id"] for r in rows], corp_c)
        for r in rows:
            r["heads_a_corporate_family"] = r["vendor_id"] in heads
        out = {"vendors": rows, "count": len(rows), "truncated": truncated,
               "note": "status A=active, I=inactive; open_payables sums "
                       "vouchers for this business unit"}
        if subs or heads:
            out["belongs_to_a_corporate_family"] = subs
            out["heads_a_corporate_family"] = sorted(heads)
            out["next_step"] = (
                "This is a supplier group, not a standalone supplier. "
                "open_payables above is each legal entity ALONE. For the "
                "group's combined position, its shared bank accounts and "
                "its duplicate vouchers, call "
                "get_vendor_payables_network(vendor_id=<the parent>).")
        if notes:
            out["record_notes"] = notes
        return out

    def _vendor_family_heads(self, setid: str, ids: list,
                             corp_c: str) -> set:
        """Which of these suppliers OWN others — one grouped read.

        A supplier's own row cannot say it owns anything; the children are
        rows the WHERE clause excluded. Searching the PARENT by name is the
        likelier half of the question, because the parent is what people
        type.
        """
        if not corp_c or not ids:
            return set()
        binds = {f"h{i}": v for i, v in enumerate(ids)}
        expr = "(" + ", ".join(f":{k}" for k in binds) + ")"
        try:
            rows, _ = self.db.query(
                f"SELECT {corp_c} AS parent, COUNT(*) AS n "
                f"FROM {self.db.prefix}PS_VENDOR "
                f"WHERE SETID = :setid AND {corp_c} IN {expr} "
                f"AND VENDOR_ID <> {corp_c} GROUP BY {corp_c}",
                {"setid": setid, **binds}, max_rows=len(ids) + 1)
        except DbError:
            return set()
        return {str(r["parent"]) for r in rows if int(r["n"] or 0) > 0}

    def duplicate_payments(self, business_unit: str = "",
                           months: int = 12, tolerance_days: int = 7,
                           as_of_date: str = "") -> dict:
        """Duplicate-voucher candidates plus confirmed payment evidence.

        Two mechanical checks, disclosed separately because they carry
        different confidence: the SAME invoice number vouchered twice for
        one vendor, and the same vendor billed the SAME amount within a few
        days under different invoice numbers.  Neither proves cash went out
        twice.  Confirmation requires two distinct, non-void payment headers
        linked to two vouchers; candidates and confirmed disbursements are
        therefore returned as different populations.
        """
        bu = self._bu(business_unit)
        asof = self._asof(as_of_date)
        since = (_iso(asof) - dt.timedelta(
            days=max(int(months or 12), 1) * 30)).isoformat()
        self._need("PS_VOUCHER", ["BUSINESS_UNIT", "VOUCHER_ID", "VENDOR_ID",
                                  "INVOICE_ID", "INVOICE_DT", "GROSS_AMT"])
        p = self.db.prefix
        # Strong voucher candidate: one vendor/invoice, several vouchers.
        # Payment confirmation is a separate population below.
        rows, exact_truncated = self.db.query(
            f"SELECT V.VENDOR_ID AS vendor_id, MAX(N.NAME1) AS vendor, "
            f"V.INVOICE_ID AS invoice_id, COUNT(*) AS n, "
            f"SUM(V.GROSS_AMT) AS total "
            f"FROM {p}PS_VOUCHER V "
            f"LEFT JOIN {self._vendor_names()} N ON N.VENDOR_ID = V.VENDOR_ID "
            f"WHERE V.BUSINESS_UNIT = :bu "
            f"AND V.INVOICE_DT >= {self.db.date_bind('since')} "
            f"AND V.INVOICE_DT <= {self.db.date_bind('asof')} "
            f"AND V.INVOICE_ID IS NOT NULL "
            f"AND TRIM(V.INVOICE_ID) <> '' "
            f"GROUP BY V.VENDOR_ID, V.INVOICE_ID HAVING COUNT(*) > 1",
            {"bu": bu, "since": since, "asof": asof}, max_rows=200)
        exact = [{"vendor_id": str(r["vendor_id"]),
                  "vendor": str(r["vendor"] or ""),
                  "invoice_id": str(r["invoice_id"]),
                  "vouchers": int(r["n"] or 0),
                  "total": r2(float(r["total"] or 0)),
                  "finding_type": "duplicate_voucher_candidate"}
                 for r in rows]

        # Worth eyes: same vendor, same amount, different invoice numbers,
        # entered within tolerance_days of each other. Day math happens in
        # Python — no dialect-specific date arithmetic in SQL.
        cand, cand_truncated = self.db.query(
            f"SELECT V.VENDOR_ID AS vendor_id, MAX(N.NAME1) AS vendor, "
            f"V.GROSS_AMT AS amount, COUNT(*) AS n "
            f"FROM {p}PS_VOUCHER V "
            f"LEFT JOIN {self._vendor_names()} N ON N.VENDOR_ID = V.VENDOR_ID "
            f"WHERE V.BUSINESS_UNIT = :bu "
            f"AND V.INVOICE_DT >= {self.db.date_bind('since')} "
            f"AND V.INVOICE_DT <= {self.db.date_bind('asof')} "
            f"GROUP BY V.VENDOR_ID, V.GROSS_AMT HAVING COUNT(*) > 1",
            {"bu": bu, "since": since, "asof": asof}, max_rows=200)

        xref_cols = self._cols("PS_PYMNT_VCHR_XREF")
        payment_cols = self._cols("PS_PAYMENT_TBL")
        payment_shape = (
            {"BUSINESS_UNIT", "VOUCHER_ID", "PYMNT_ID", "PAID_AMT"}
            <= xref_cols
            and {"PYMNT_ID", "PYMNT_DT"} <= payment_cols)
        void_status_available = "PYMNT_STATUS" in payment_cols
        payment_evidence_evaluated = bool(payment_shape
                                          and void_status_available)
        payment_select = ""
        payment_joins = ""
        if payment_shape:
            status = ("P.PYMNT_STATUS" if void_status_available else "NULL")
            payment_select = (
                ", X.PYMNT_ID AS payment_id, X.PAID_AMT AS paid_amount, "
                f"P.PYMNT_DT AS payment_dt, {status} AS payment_status")
            payment_joins = (
                f" LEFT JOIN {p}PS_PYMNT_VCHR_XREF X ON "
                "X.BUSINESS_UNIT = V.BUSINESS_UNIT "
                "AND X.VOUCHER_ID = V.VOUCHER_ID"
                f" LEFT JOIN {p}PS_PAYMENT_TBL P ON "
                "P.PYMNT_ID = X.PYMNT_ID")

        near = []
        detail: list = []
        truncated = False
        if cand or exact:
            detail, truncated = self.db.query(
                f"SELECT V.VENDOR_ID AS vendor_id, "
                f"V.VOUCHER_ID AS voucher_id, V.INVOICE_ID AS invoice_id, "
                f"V.INVOICE_DT AS dt, V.GROSS_AMT AS amount"
                f"{payment_select} FROM {p}PS_VOUCHER V{payment_joins} "
                f"WHERE V.BUSINESS_UNIT = :bu "
                f"AND V.INVOICE_DT >= {self.db.date_bind('since')} "
                f"AND V.INVOICE_DT <= {self.db.date_bind('asof')}",
                {"bu": bu, "since": since, "asof": asof}, max_rows=5000)

        # Collapse the payment join back to one voucher.  A voucher may have
        # several scheduled payments; that must enrich its evidence rather
        # than turn the voucher itself into several duplicate candidates.
        vouchers: dict[str, dict] = {}
        for r in detail:
            voucher_id = str(r["voucher_id"])
            v = vouchers.setdefault(voucher_id, {
                "vendor_id": str(r["vendor_id"]),
                "voucher_id": voucher_id,
                "invoice_id": str(r.get("invoice_id") or ""),
                "dt": str(r.get("dt") or "")[:10],
                "amount": float(r.get("amount") or 0),
                "payments": [],
                "void_payment_rows": 0,
                "nonpositive_payment_rows": 0,
            })
            if not payment_shape or not r.get("payment_id") \
                    or not r.get("payment_dt"):
                continue
            payment_dt = str(r.get("payment_dt") or "")[:10]
            if payment_dt > asof:
                continue
            status = str(r.get("payment_status") or "").strip().upper()
            if void_status_available and status == "V":
                v["void_payment_rows"] += 1
                continue
            # A status-bearing record with a blank status cannot prove it is
            # non-void.  Keep confirmation conservative.
            if void_status_available and not status:
                continue
            paid_amount = r2(float(r.get("paid_amount") or 0))
            if paid_amount <= 0:
                v["nonpositive_payment_rows"] += 1
                continue
            evidence = {
                "payment_id": str(r["payment_id"]),
                "payment_dt": payment_dt,
                "paid_amount": paid_amount,
                "payment_status": status or None,
                "voucher_id": voucher_id,
            }
            if not any(x["payment_id"] == evidence["payment_id"]
                       for x in v["payments"]):
                v["payments"].append(evidence)

        detail_complete = not (truncated or exact_truncated or cand_truncated)
        exact_by_key: dict[tuple[str, str], list] = {}
        for v in vouchers.values():
            exact_by_key.setdefault((v["vendor_id"], v["invoice_id"]),
                                    []).append(v)

        for candidate in exact:
            members = exact_by_key.get((candidate["vendor_id"],
                                        candidate["invoice_id"]), [])
            evidence_available = (payment_evidence_evaluated
                                  and detail_complete
                                  and len(members) == candidate["vouchers"])
            paid_rows = [pay for v in members for pay in v["payments"]]
            paid_vouchers = {pay["voucher_id"] for pay in paid_rows}
            payment_ids = sorted({pay["payment_id"] for pay in paid_rows})
            confirmed = bool(evidence_available
                             and len(paid_vouchers) >= 2
                             and len(payment_ids) >= 2)
            paid_total = r2(sum(float(pay["paid_amount"])
                                for pay in paid_rows))
            # One legitimate voucher is the conservative baseline.  Use the
            # largest candidate voucher gross, not the largest payment row:
            # a legitimate voucher may have been paid in instalments.
            legitimate_gross = max((float(v["amount"]) for v in members),
                                   default=0.0)
            exposure = (r2(max(paid_total - legitimate_gross, 0.0))
                        if confirmed else 0.0)
            candidate.update({
                "voucher_ids": sorted(v["voucher_id"] for v in members),
                "confirmed_duplicate_payment": confirmed,
                "payment_evidence_status": (
                    "confirmed_duplicate_disbursement" if confirmed else
                    "voucher_duplicate_only" if evidence_available else
                    "unavailable"),
                "payment_evidence": {
                    "evaluated": evidence_available,
                    "confirmed": confirmed,
                    "payment_count": len(payment_ids),
                    "payment_ids": payment_ids,
                    "paid_voucher_count": len(paid_vouchers),
                    "paid_total": paid_total,
                    "duplicate_exposure": exposure,
                    "exposure_basis": (
                        "confirmed paid total less the largest candidate "
                        "voucher gross (one legitimate voucher baseline)"),
                    "voids_excluded": sum(v["void_payment_rows"]
                                          for v in members),
                    "nonpositive_rows_excluded": sum(
                        v["nonpositive_payment_rows"] for v in members),
                    "basis": ("distinct non-void PS_PAYMENT_TBL headers "
                              "linked through PS_PYMNT_VCHR_XREF, with "
                              "payment date through as_of"),
                },
            })

        if cand:
            wanted = {(str(c["vendor_id"]), float(c["amount"] or 0))
                      for c in cand}
            names = {str(c["vendor_id"]): str(c["vendor"] or "")
                     for c in cand}
            groups: dict = {}
            for r in vouchers.values():
                key = (r["vendor_id"], float(r["amount"] or 0))
                if key in wanted:
                    groups.setdefault(key, []).append(r)
            for (vid, amount), members in sorted(groups.items()):
                members.sort(key=lambda r: str(r["dt"]))
                for a, b in zip(members, members[1:]):
                    if str(a["invoice_id"]) == str(b["invoice_id"]):
                        continue  # already in the exact list
                    gap = abs((_iso(str(b["dt"])) - _iso(str(a["dt"]))).days)
                    if gap <= max(int(tolerance_days or 7), 0):
                        near.append({
                            "vendor_id": vid, "vendor": names.get(vid, ""),
                            "amount": r2(amount), "days_apart": gap,
                            "vouchers": [str(a["voucher_id"]),
                                         str(b["voucher_id"])],
                            "invoices": [str(a["invoice_id"]),
                                         str(b["invoice_id"])],
                            "finding_type": "same_amount_review_candidate",
                            "payment_evidence": {
                                "evaluated": payment_evidence_evaluated
                                             and detail_complete,
                                "confirmed": False,
                                "paid_voucher_count": sum(
                                    1 for v in (a, b) if v["payments"]),
                                "payment_ids": sorted({
                                    pay["payment_id"] for v in (a, b)
                                    for pay in v["payments"]}),
                                "note": ("Payments may be confirmed, but "
                                         "different invoice numbers remain "
                                         "a review candidate, not proof of "
                                         "a duplicate disbursement."),
                            }})
        exact_total = r2(sum(x["total"] for x in exact))
        notes = []
        if exact_truncated or cand_truncated:
            notes.append("More than 200 duplicate candidate groups matched; "
                         "the candidate population is incomplete.")
        if (cand or exact) and truncated:
            notes.append("More than 5,000 vouchers in the window — the "
                         "pair scan and payment confirmation covered the "
                         "first 5,000; narrow the window for full coverage.")
        if not payment_shape:
            notes.append(
                "PS_PYMNT_VCHR_XREF and PS_PAYMENT_TBL do not expose the "
                "required payment-link fields here; duplicate vouchers can "
                "be listed, but duplicate disbursement cannot be confirmed.")
        elif not void_status_available:
            notes.append(
                "PS_PAYMENT_TBL has no PYMNT_STATUS; voids cannot be "
                "excluded, so duplicate disbursement is not confirmed.")
        confirmed = [
            {**x, "finding_type": "confirmed_duplicate_payment"}
            for x in exact if x.get("confirmed_duplicate_payment")]
        confirmed_paid_total = r2(sum(
            float(x["payment_evidence"]["paid_total"]) for x in confirmed))
        confirmed_exposure = r2(sum(
            float(x["payment_evidence"]["duplicate_exposure"])
            for x in confirmed))
        return {
            **({"record_notes": notes} if notes else {}),
            "business_unit": bu, "since": since, "as_of": asof,
            "window_months": int(months or 12),
            "tolerance_days": int(tolerance_days or 7),
            "exact_invoice_duplicates": exact,
            "duplicate_voucher_candidates": exact,
            "same_amount_pairs": near,
            "exact_total": exact_total,
            "candidate_gross_total": exact_total,
            "payment_evidence_evaluated": (payment_evidence_evaluated
                                           and detail_complete),
            "confirmed_duplicate_payments": confirmed,
            "confirmed_duplicate_payment_count": len(confirmed),
            "confirmed_duplicate_payment_total": confirmed_paid_total,
            "confirmed_duplicate_exposure": confirmed_exposure,
            "note": ("Exact list = one vendor invoice number vouchered more "
                     "than once: a duplicate-VOUCHER candidate, not proof "
                     "that cash was paid twice. confirmed_duplicate_payments "
                     "requires two distinct non-void payment headers linked "
                     "to two vouchers as of the requested date. Same-amount "
                     "list "
                     "= same vendor and amount within the tolerance window "
                     "under different invoice numbers — a REVIEW list, not "
                     "an accusation; recurring charges look like this too. "
                     "No candidates found is a real answer, not a failure."
                     ),
        }

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
        params: dict = {"since": since, "asof": asof}
        vend_pred = ""
        term = (vendor or "").strip()
        if term:
            params["vid"] = term.upper()
            params["vname"] = f"%{term.upper()}%"
            vend_pred = (" AND (UPPER(P.VENDOR_ID) = :vid"
                         + (" OR UPPER(N.NAME1) LIKE :vname" if name != "NULL"
                            else "") + ")")
        void_pred = " AND P.PYMNT_STATUS <> 'V'" if has_status else ""
        # PS_PAYMENT_TBL is BANK-level: a payment belongs to a pay cycle and
        # a bank account, not to a business unit. There is no BUSINESS_UNIT
        # column to filter on, and this tool used to print the unit beside a
        # total that covered the whole installation — the shape of wrong
        # answer that reads as precise. The link exists, one hop away, in
        # the voucher cross-reference; EXISTS stops at the first matching
        # voucher rather than counting them.
        # Attempt the scoped read, and report what actually happened rather
        # than what the catalog implied. Deciding from introspection alone
        # is wrong in both directions: an empty column list means "could not
        # read", not "not there", so assuming the reference shape claims a
        # scope that may never have applied, and assuming absence widens the
        # population on a site where the record is fine.
        xref = self._cols("PS_PYMNT_VCHR_XREF")
        bu_pred = ("" if (xref and not {"BUSINESS_UNIT", "PYMNT_ID"} <= xref)
                   else f" AND EXISTS (SELECT 1 FROM {p}PS_PYMNT_VCHR_XREF X "
                        "WHERE X.PYMNT_ID = P.PYMNT_ID "
                        "AND X.BUSINESS_UNIT = :bu)")
        if bu_pred:
            params["bu"] = bu
        notes: list = []
        def _read(scope_pred: str, binds: dict):
            return self.db.query(
                f"""SELECT P.VENDOR_ID AS vendor_id, {name} AS vendor,
       COUNT(*) AS payments, SUM(P.PYMNT_AMT) AS paid,
       MAX(P.PYMNT_DT) AS last_payment_dt
 FROM {p}PS_PAYMENT_TBL P
  LEFT JOIN {self._vendor_names()} N ON N.VENDOR_ID = P.VENDOR_ID
 WHERE P.PYMNT_DT >= {self.db.date_bind('since')}
   AND P.PYMNT_DT <= {self.db.date_bind('asof')}"""
                f"{scope_pred}{void_pred}{vend_pred}\n"
                f" GROUP BY P.VENDOR_ID"
                f"{', ' + name if name != 'NULL' else ''}\n"
                f" ORDER BY 4 DESC", binds, max_rows=5_000)

        scoped = bool(bu_pred)
        try:
            rows, _ = _read(bu_pred, params)
        except DbError as e:
            if not scoped:
                raise
            scoped = False
            rows, _ = _read("", {k: v for k, v in params.items()
                                 if k != "bu"})
            notes.append(
                f"The payment cross-reference could not be read ({e}), and "
                "PS_PAYMENT_TBL carries no business unit — a payment is made "
                "by a pay cycle, not by a unit.")
        if not scoped and not notes:
            notes.append(
                "PS_PYMNT_VCHR_XREF has no BUSINESS_UNIT/PYMNT_ID pair here, "
                "and PS_PAYMENT_TBL carries no business unit — a payment is "
                "made by a pay cycle, not by a unit.")
        if not scoped:
            notes[-1] += (f" The totals below therefore cover the WHOLE "
                          f"INSTALLATION, not {bu} alone.")
        out = {
            "business_unit": bu,
            # Say whether the unit above actually bounded the figures.
            "scoped_to_business_unit": scoped,
            "window_months": int(months or 12),
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
            "note": ("Payments by payment date through as_of (inclusive); "
                     "voided payments excluded"
                     if has_status else
                     "Payments by payment date through as_of (inclusive); "
                     "PS_PAYMENT_TBL here has no "
                     "PYMNT_STATUS, so voids cannot be excluded")
            + (f"; scoped to {bu} through the voucher cross-reference"
               if scoped else "; NOT scoped to a business unit — see "
                              "record_notes"),
        }
        if notes:
            out["record_notes"] = notes
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
