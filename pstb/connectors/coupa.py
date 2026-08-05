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
        coupa = [r for r in self._invoices()
                 if r["status"].lower() in {"approved", "paid"}
                 and r["invoice_date"] >= since]
        p = db.prefix
        try:
            vouchers, _ = db.query(
                f"SELECT V.INVOICE_ID AS inv, V.VOUCHER_ID AS voucher, "
                f"V.GROSS_AMT AS amt, V.CURRENCY_CD AS currency, "
                f"N.NAME1 AS vendor "
                f"FROM {p}PS_VOUCHER V LEFT JOIN {p}PS_VENDOR N "
                f"ON N.VENDOR_ID = V.VENDOR_ID "
                f"WHERE V.INVOICE_DT >= {db.date_bind('since')}",
                {"since": since}, max_rows=5000)
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
            "since": since, "match_basis": (
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
