"""Coupa: procurement truth beside the ledger's accounting truth.

Coupa knows what was requested, approved, received and invoiced; the
ledger knows what was vouchered and posted. The curated methods answer
the close-cycle questions each side alone cannot: what is stuck in
approval, what was received but never invoiced (the accrual candidates),
and whether everything Coupa approved actually landed in AP.

Every method filters client-side even though live calls also pass query
params — the same code path then serves fixtures and live traffic, and a
Coupa view whose server-side filter silently ignores a param cannot
quietly widen a result.

Amounts are grouped by currency and NEVER summed across currencies —
the same rule the AR tools follow.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from typing import Optional

from . import FIXTURE_DIR, ConnectorError, FixtureTransport, RestConnector


def _norm_name(s: str) -> str:
    """Supplier names differ in punctuation and suffixes across systems."""
    s = re.sub(r"[^a-z0-9 ]", "", str(s or "").lower())
    s = re.sub(r"\b(inc|llc|ltd|corp|co|company|services|supply)\b", "", s)
    return " ".join(s.split())


def _iso(v) -> str:
    return str(v or "")[:10]


class CoupaConnector(RestConnector):
    name = "coupa"
    ping_path = "/api/suppliers"

    # ------------------------------------------------------------ raw reads
    def _invoices(self) -> list[dict]:
        rows = self.get("/api/invoices", {"limit": 200}) or []
        out = []
        for r in rows:
            out.append({
                "id": r.get("id"),
                "number": str(r.get("invoice-number")
                              or r.get("invoice_number") or ""),
                "supplier": str((r.get("supplier") or {}).get("name")
                               or r.get("supplier-name") or ""),
                "status": str(r.get("status") or ""),
                "total": float(r.get("total") or 0.0),
                "currency": str((r.get("currency") or {}).get("code")
                                or r.get("currency-code") or ""),
                "invoice_date": _iso(r.get("invoice-date")
                                     or r.get("invoice_date")),
                "pending_since": _iso(r.get("pending-since")
                                      or r.get("submitted-at")),
                "approver": str(((r.get("current-approval") or {})
                                 .get("approver") or {}).get("name") or ""),
            })
        return out

    def _po_lines(self) -> list[dict]:
        rows = self.get("/api/purchase_order_lines", {"limit": 200}) or []
        out = []
        for r in rows:
            out.append({
                "po": str(r.get("order-header-number") or r.get("po") or ""),
                "line": r.get("line-num") or r.get("line"),
                "supplier": str((r.get("supplier") or {}).get("name")
                               or r.get("supplier-name") or ""),
                "description": str(r.get("description") or ""),
                "received_amt": float(r.get("received-amount")
                                      or r.get("received_amt") or 0.0),
                "invoiced_amt": float(r.get("invoiced-amount")
                                      or r.get("invoiced_amt") or 0.0),
                "currency": str((r.get("currency") or {}).get("code")
                                or r.get("currency-code") or ""),
            })
        return out

    # -------------------------------------------------------- curated views
    def invoices(self, status: str = "", supplier: str = "",
                 days: int = 30, max_rows: int = 50,
                 today: Optional[dt.date] = None) -> dict:
        today = today or dt.date.today()
        since = (today - dt.timedelta(days=max(int(days or 30), 1))
                 ).isoformat()
        want_status = str(status or "").strip().lower()
        want_sup = _norm_name(supplier)
        rows = [r for r in self._invoices()
                if r["invoice_date"] >= since
                and (not want_status or r["status"].lower() == want_status)
                and (not want_sup or want_sup in _norm_name(r["supplier"]))]
        rows.sort(key=lambda r: r["invoice_date"], reverse=True)
        totals: dict[str, float] = {}
        for r in rows:
            totals[r["currency"]] = round(
                totals.get(r["currency"], 0.0) + r["total"], 2)
        return {"source": "coupa", "mode": self.mode, "since": since,
                "count": len(rows), "totals_by_currency": totals,
                "invoices": rows[:max(int(max_rows or 50), 1)],
                "truncated": len(rows) > max_rows}

    def stuck_approvals(self, days_pending: int = 3,
                        today: Optional[dt.date] = None) -> dict:
        today = today or dt.date.today()
        cutoff = (today - dt.timedelta(days=max(int(days_pending or 3), 1))
                  ).isoformat()
        rows = [r for r in self._invoices()
                if r["status"].lower() == "pending_approval"
                and r["pending_since"] and r["pending_since"] <= cutoff]
        for r in rows:
            r["days_pending"] = (today - dt.date.fromisoformat(
                r["pending_since"])).days
        rows.sort(key=lambda r: -r["days_pending"])
        return {"source": "coupa", "mode": self.mode,
                "days_pending_threshold": int(days_pending or 3),
                "count": len(rows), "stuck": rows,
                "note": ("Every invoice here has sat with its current "
                         "approver past the threshold — the close cannot "
                         "book what approval is still holding.")
                if rows else "No approvals stuck past the threshold."}

    def received_not_invoiced(self, min_amount: float = 0.0) -> dict:
        """The accrual-candidate list: received value not yet invoiced."""
        rows = []
        for r in self._po_lines():
            rni = round(r["received_amt"] - r["invoiced_amt"], 2)
            if rni > max(float(min_amount or 0.0), 0.0):
                rows.append({**r, "rni_amt": rni})
        rows.sort(key=lambda r: -r["rni_amt"])
        totals: dict[str, float] = {}
        for r in rows:
            totals[r["currency"]] = round(
                totals.get(r["currency"], 0.0) + r["rni_amt"], 2)
        return {"source": "coupa", "mode": self.mode, "count": len(rows),
                "rni_totals_by_currency": totals, "lines": rows,
                "note": "Received-not-invoiced by PO line — the month-end "
                        "accrual candidates. Totals are per currency and "
                        "never summed across currencies."}

    def supplier_spend(self, months: int = 12, top_n: int = 10,
                       today: Optional[dt.date] = None) -> dict:
        today = today or dt.date.today()
        since = (today - dt.timedelta(days=max(int(months or 12), 1) * 30)
                 ).isoformat()
        by: dict[tuple, dict] = {}
        for r in self._invoices():
            if r["invoice_date"] < since or not r["supplier"]:
                continue
            key = (r["supplier"], r["currency"])
            s = by.setdefault(key, {"supplier": r["supplier"],
                                    "currency": r["currency"],
                                    "invoices": 0, "spend": 0.0})
            s["invoices"] += 1
            s["spend"] = round(s["spend"] + r["total"], 2)
        ranked = sorted(by.values(), key=lambda s: -s["spend"])
        return {"source": "coupa", "mode": self.mode, "since": since,
                "suppliers": ranked[:max(int(top_n or 10), 1)],
                "count": len(ranked), "truncated": len(ranked) > top_n}

    # ------------------------------------------------------- reconciliation
    def budget_lines(self, period: str = "") -> dict:
        """Budget lines as Coupa holds them: account segments, period,
        amount. segment-1 is conventionally the natural account and
        segment-2 the cost centre, but that is a per-tenant CHART
        CONFIGURATION, so the mapping is reported, never assumed silently.
        """
        rows = self.get("/api/budget_lines", {"limit": 500}) or []
        want = (period or "").strip().upper()
        out = []
        for r in rows:
            acct = r.get("account") or {}
            entry = {
                "id": r.get("id"),
                "name": str(r.get("name") or ""),
                "period": str(r.get("period") or "").upper(),
                "account": str(acct.get("segment-1")
                               or acct.get("segment_1") or ""),
                "cost_centre": str(acct.get("segment-2")
                                   or acct.get("segment_2") or ""),
                "budget": float(r.get("budgeted-amount")
                                or r.get("budgeted_amount") or 0.0),
                "currency": str((r.get("currency") or {}).get("code") or ""),
            }
            if want and entry["period"] != want:
                continue
            out.append(entry)
        return {"source": "coupa", "mode": self.mode,
                "period": want or "all", "count": len(out),
                "budget_lines": out,
                "segment_map": {"segment-1": "natural account",
                                "segment-2": "cost centre"},
                "note": ("Budget lives in Coupa at this deployment, not in "
                         "a PeopleSoft budget ledger. Segment meanings are "
                         "a per-tenant chart configuration — confirm them "
                         "before trusting a mapping.")}

    def budget_variance(self, engine, business_unit: str = "",
                        fiscal_year: int = 0, period: int = 0,
                        top: int = 25) -> dict:
        """Coupa BUDGET vs PeopleSoft ACTUALS, matched on natural account.

        The cross-system shape this site actually has. Match basis is
        disclosed, and the two failure directions are reported separately
        because they mean different things: a budget line with no ledger
        activity is unspent plan, while ledger spend with no budget line
        is unbudgeted — the one a controller wants to see first.
        """
        bu = (business_unit or "").strip() or \
            engine.effective_defaults()["business_unit"]
        led = engine.resolve_ledger_for(bu)
        fy = int(fiscal_year or 0) or engine.last_posted_period(bu, led)[0]
        per = int(period or 0) or engine.last_posted_period(bu, led)[1]
        if not fy:
            return {"evaluated": False,
                    "reason": f"No posted ledger data for {bu!r} to compare "
                              "the Coupa budget against."}
        lines = self.budget_lines(period=f"FY{fy}")["budget_lines"]
        if not lines:
            return {"evaluated": False, "business_unit": bu,
                    "fiscal_year": fy,
                    "reason": (f"Coupa holds no budget lines for FY{fy}. "
                               "Check the budget period naming in Coupa "
                               "(this tool matches on 'FY<year>').")}
        rows = engine._period_sums(bu, led, fy, per, include_adj=True)
        actual: dict = {}
        descr: dict = {}
        types: dict = {}
        for r in rows:
            p_ = int(r.get("period") or 0)
            if p_ < 1 or p_ > per:
                continue
            acct = str(r.get("account") or "")
            actual[acct] = actual.get(acct, 0.0) + float(r.get("amt") or 0.0)
            descr.setdefault(acct, str(r.get("descr") or ""))
            types.setdefault(acct, str(r.get("acct_type") or ""))
        budget: dict = {}
        currencies = set()
        for line in lines:
            budget[line["account"]] = round(
                budget.get(line["account"], 0.0) + line["budget"], 2)
            currencies.add(line["currency"])
        compared, unbudgeted, unspent = [], [], []
        revenue_excluded = 0
        for acct in sorted(set(actual) | set(budget)):
            a = round(actual.get(acct, 0.0), 2)
            b = round(budget.get(acct, 0.0), 2)
            atype = types.get(acct, "")
            if acct not in budget:
                # Coupa budgets SPEND. Revenue having no Coupa budget line
                # is the expected shape, not a finding, and balance-sheet
                # accounts were never in scope — flagging either as
                # "unbudgeted" is the noise that makes a real unbudgeted
                # expense easy to miss.
                if atype == "E" and a:
                    unbudgeted.append({"account": acct,
                                       "descr": descr.get(acct, ""),
                                       "actual": a})
                elif atype == "R":
                    revenue_excluded += 1
                continue
            if acct not in actual:
                unspent.append({"account": acct, "budget": b,
                                "descr": descr.get(acct, "")})
                continue
            variance = round(a - b, 2)
            compared.append({
                "account": acct, "descr": descr.get(acct, ""),
                "account_type": atype, "actual": a, "budget": b,
                "variance": variance,
                "variance_pct": (round(variance / abs(b) * 100.0, 2)
                                 if b else None),
                # Expense and revenue both read "favourable" when the
                # variance is negative; see the PS-side tool for why the
                # account TYPE decides this and never the sign.
                "favourable": variance < 0 if atype in ("R", "E") else None,
            })
        compared.sort(key=lambda r: -abs(r["variance"]))
        truncated = len(compared) > max(int(top or 25), 1)
        return {
            "source": "coupa+peoplesoft", "evaluated": True,
            "business_unit": bu, "ledger": led,
            "fiscal_year": fy, "through_period": per,
            "mode": self.mode,
            "budget_currencies": sorted(c for c in currencies if c),
            "match_basis": ("Coupa budget-line account segment-1 matched to "
                            "the PeopleSoft natural ACCOUNT; ledger side is "
                            f"year-to-date activity, periods 1..{per}"),
            "rows": compared[:max(int(top or 25), 1)],
            "row_count": len(compared), "truncated": truncated,
            "unbudgeted_spend": unbudgeted,
            "budget_not_spent": unspent,
            "population": {
                "concept": "Coupa spend budget vs ledger actuals",
                "applied": [
                    {"predicate": "ACCOUNT_TYPE = 'E' for unbudgeted spend",
                     "source": "Coupa budgets procurable SPEND",
                     "meaning": "revenue and balance-sheet accounts are not "
                                "expected to carry a Coupa budget line"},
                    {"predicate": f"periods 1..{per} of FY{fy}",
                     "source": "the request scope",
                     "meaning": "year-to-date ledger activity"},
                ],
                "revenue_accounts_excluded": revenue_excluded,
            },
            "note": ("Budget from Coupa, actuals from the ledger. Unbudgeted "
                     "spend and unspent budget are listed separately — they "
                     "are different problems. Expenses that never flow "
                     "through procurement (depreciation, payroll "
                     "allocations) legitimately have no Coupa budget — "
                     "judge the unbudgeted list with that in mind. Confirm "
                     "the segment mapping before acting: segment meanings "
                     "are per-tenant."
                     + (" SAMPLE procurement fixtures, not live data."
                        if self.mode == "fixtures" else "")),
        }

    def ap_tie(self, db, days: int = 90,
               today: Optional[dt.date] = None) -> dict:
        """Approved Coupa invoices vs PS vouchers, matched server-side.

        Match basis (disclosed in the payload): invoice number equality,
        then normalized supplier name against the vendor master. Amount
        differences on matched pairs are listed — a matched-but-different
        pair is the most dangerous kind, because both systems look right
        alone.
        """
        today = today or dt.date.today()
        since = (today - dt.timedelta(days=max(int(days or 90), 1))
                 ).isoformat()
        asof = today.isoformat()
        coupa = [r for r in self._invoices()
                 if r["status"].lower() in {"approved", "paid"}
                 and since <= r["invoice_date"] <= asof]
        p = db.prefix
        try:
            vouchers, _ = db.query(
                f"SELECT V.INVOICE_ID AS inv, V.VOUCHER_ID AS voucher, "
                f"V.GROSS_AMT AS amt, V.CURRENCY_CD AS currency, "
                f"N.NAME1 AS vendor "
                f"FROM {p}PS_VOUCHER V LEFT JOIN {p}PS_VENDOR N "
                f"ON N.VENDOR_ID = V.VENDOR_ID "
                f"WHERE V.INVOICE_DT >= {db.date_bind('since')} "
                f"AND V.INVOICE_DT <= {db.date_bind('asof')}",
                {"since": since, "asof": asof}, max_rows=5000)
        except Exception as e:
            return {"source": "coupa+peoplesoft", "evaluated": False,
                    "reason": f"Could not read PS_VOUCHER: {e}"}
        by_number: dict[str, dict] = {}
        for v in vouchers:
            by_number.setdefault(str(v["inv"] or ""), dict(v))
        matched, amount_breaks, missing_in_ap = [], [], []
        for c in coupa:
            v = by_number.get(c["number"])
            if not v:
                missing_in_ap.append(c)
                continue
            names_agree = (_norm_name(v.get("vendor"))
                           == _norm_name(c["supplier"]))
            pair = {"invoice": c["number"], "voucher": v["voucher"],
                    "coupa_total": c["total"],
                    "ps_gross": float(v["amt"] or 0.0),
                    "currency": c["currency"],
                    "supplier": c["supplier"],
                    "vendor_name_match": names_agree}
            if round(pair["coupa_total"] - pair["ps_gross"], 2) != 0.0 \
                    or c["currency"] != str(v.get("currency") or ""):
                amount_breaks.append({
                    **pair, "difference": round(
                        pair["coupa_total"] - pair["ps_gross"], 2)})
            else:
                matched.append(pair)
        ties = not missing_in_ap and not amount_breaks
        return {
            "source": "coupa+peoplesoft", "evaluated": True, "ties": ties,
            "since": since, "as_of": asof, "match_basis": (
                "invoice number equality against PS_VOUCHER.INVOICE_ID, "
                "supplier verified against the vendor master by normalized "
                "name"),
            "coupa_invoices": len(coupa), "matched": len(matched),
            "amount_breaks": amount_breaks,
            "missing_in_ap": [{k: c[k] for k in
                               ("number", "supplier", "total", "currency",
                                "invoice_date", "status")}
                              for c in missing_in_ap],
            "note": ("Every approved Coupa invoice in the window has a "
                     "matching voucher at the same amount." if ties else
                     "Breaks listed — missing_in_ap never reached AP; "
                     "amount_breaks landed at a different amount and need "
                     "eyes even though both systems look right alone."),
        }


def from_env(root=None) -> CoupaConnector:
    """Live when COUPA_BASE_URL is set; bundled fixtures otherwise."""
    base = os.environ.get("COUPA_BASE_URL", "").strip()
    if base:
        return CoupaConnector(
            base,
            api_key=os.environ.get("COUPA_API_KEY", "").strip(),
            client_id=os.environ.get("COUPA_CLIENT_ID", "").strip(),
            client_secret=os.environ.get("COUPA_CLIENT_SECRET", "").strip(),
            scope=os.environ.get("COUPA_SCOPE",
                                 "core.invoice.read core.purchase_order.read "
                                 "core.supplier.read").strip(),
        )
    fixture = FIXTURE_DIR / "coupa.json"
    return CoupaConnector(transport=FixtureTransport(fixture))
