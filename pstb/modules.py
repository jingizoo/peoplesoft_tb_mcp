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
        rows, _ = self.db.query(
            f"""SELECT V.VENDOR_ID AS vendor_id, {name} AS vendor,
       V.VOUCHER_ID AS voucher_id, V.GROSS_AMT AS amount,
       {cur} AS currency, {due or 'NULL'} AS due_dt,
       {entry} AS entry_status, {post} AS post_status
  FROM {p}PS_VOUCHER V
  LEFT JOIN {self._vendor_names()} N ON N.VENDOR_ID = V.VENDOR_ID
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
        """Possible duplicate vouchers — the AP audit question every close
        asks and nobody enjoys checking by hand.

        Two mechanical checks, disclosed separately because they carry
        different confidence: the SAME invoice number vouchered twice for
        one vendor (near-certain duplicate), and the same vendor billed the
        SAME amount within a few days under different invoice numbers
        (worth eyes — recurring charges legitimately look like this, so it
        is a review list, never an accusation).
        """
        bu = self._bu(business_unit)
        asof = self._asof(as_of_date)
        since = (_iso(asof) - dt.timedelta(
            days=max(int(months or 12), 1) * 30)).isoformat()
        self._need("PS_VOUCHER", ["BUSINESS_UNIT", "VOUCHER_ID", "VENDOR_ID",
                                  "INVOICE_ID", "INVOICE_DT", "GROSS_AMT"])
        p = self.db.prefix
        # Near-certain: one vendor, one invoice number, several vouchers.
        rows, _ = self.db.query(
            f"SELECT V.VENDOR_ID AS vendor_id, MAX(N.NAME1) AS vendor, "
            f"V.INVOICE_ID AS invoice_id, COUNT(*) AS n, "
            f"SUM(V.GROSS_AMT) AS total "
            f"FROM {p}PS_VOUCHER V "
            f"LEFT JOIN {self._vendor_names()} N ON N.VENDOR_ID = V.VENDOR_ID "
            f"WHERE V.BUSINESS_UNIT = :bu "
            f"AND V.INVOICE_DT >= {self.db.date_bind('since')} "
            f"GROUP BY V.VENDOR_ID, V.INVOICE_ID HAVING COUNT(*) > 1",
            {"bu": bu, "since": since}, max_rows=200)
        exact = [{"vendor_id": str(r["vendor_id"]),
                  "vendor": str(r["vendor"] or ""),
                  "invoice_id": str(r["invoice_id"]),
                  "vouchers": int(r["n"] or 0),
                  "total": r2(float(r["total"] or 0))} for r in rows]

        # Worth eyes: same vendor, same amount, different invoice numbers,
        # entered within tolerance_days of each other. Day math happens in
        # Python — no dialect-specific date arithmetic in SQL.
        cand, _ = self.db.query(
            f"SELECT V.VENDOR_ID AS vendor_id, MAX(N.NAME1) AS vendor, "
            f"V.GROSS_AMT AS amount, COUNT(*) AS n "
            f"FROM {p}PS_VOUCHER V "
            f"LEFT JOIN {self._vendor_names()} N ON N.VENDOR_ID = V.VENDOR_ID "
            f"WHERE V.BUSINESS_UNIT = :bu "
            f"AND V.INVOICE_DT >= {self.db.date_bind('since')} "
            f"GROUP BY V.VENDOR_ID, V.GROSS_AMT HAVING COUNT(*) > 1",
            {"bu": bu, "since": since}, max_rows=200)
        near = []
        if cand:
            detail, truncated = self.db.query(
                f"SELECT VENDOR_ID AS vendor_id, VOUCHER_ID AS voucher_id, "
                f"INVOICE_ID AS invoice_id, INVOICE_DT AS dt, "
                f"GROSS_AMT AS amount "
                f"FROM {p}PS_VOUCHER WHERE BUSINESS_UNIT = :bu "
                f"AND INVOICE_DT >= {self.db.date_bind('since')}",
                {"bu": bu, "since": since}, max_rows=5000)
            wanted = {(str(c["vendor_id"]), float(c["amount"] or 0))
                      for c in cand}
            names = {str(c["vendor_id"]): str(c["vendor"] or "")
                     for c in cand}
            groups: dict = {}
            for r in detail:
                key = (str(r["vendor_id"]), float(r["amount"] or 0))
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
                                         str(b["invoice_id"])]})
        exact_total = r2(sum(x["total"] for x in exact))
        notes = []
        if cand and truncated:
            notes.append("More than 5,000 vouchers in the window — the "
                         "same-amount pair scan covered the first 5,000; "
                         "narrow the window for full coverage.")
        return {
            **({"record_notes": notes} if notes else {}),
            "business_unit": bu, "since": since, "as_of": asof,
            "window_months": int(months or 12),
            "tolerance_days": int(tolerance_days or 7),
            "exact_invoice_duplicates": exact,
            "same_amount_pairs": near,
            "exact_total": exact_total,
            "note": ("Exact list = one vendor invoice number vouchered more "
                     "than once (near-certain duplicate). Same-amount list "
                     "= same vendor and amount within the tolerance window "
                     "under different invoice numbers — a REVIEW list, not "
                     "an accusation; recurring charges look like this too. "
                     "No duplicates found is a real answer, not a failure."
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
 WHERE P.PYMNT_DT >= {self.db.date_bind('since')}"""
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
            "note": ("Payments by payment date; voided payments excluded"
                     if has_status else
                     "Payments by payment date; PS_PAYMENT_TBL here has no "
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
