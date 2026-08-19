"""The purchase-to-pay chain: order, receipt, voucher, payment — tied out.

THE QUESTION THIS ANSWERS. "Why is this voucher stuck?" and "did we get
what we paid for?" are the daily AP questions, and both are THREE-WAY MATCH
questions: the order says what was agreed, the receipt says what arrived,
the voucher says what the supplier billed. Each document is fine alone; the
finding is always in the comparison, and the comparison is exactly what a
person stops doing when it means five panels per voucher.

TWO VERDICTS, KEPT APART. PeopleSoft stamps its own verdict on the voucher
(MATCH_STATUS_VCHR). This module also RECOMPUTES the arithmetic from the
lines. Both are reported, labelled — "the system flags it E; the numbers
show 750.00 over the order price" — and they are never merged, because when
they disagree, that disagreement IS the finding: an override someone typed,
a tolerance nobody remembers setting, or rules changed since the stamp.

WHAT A BREAK IS. Every break carries the two figures that disagree and the
document ids that carry them. No break fires on an absent number: a site
without receipt detail gets "the receipt leg is not recorded here" as a
NOTE, never a wall of no-receipt exceptions against vouchers that were
matched two-way on purpose.

THE CANCELED-ORDER TRAP. An order nothing arrived against is "awaiting
receipt" only while somebody still expects it to arrive. A canceled order
looks identical in every column this check reads except PO_STATUS — so the
status is read, and the trap is seeded (PO2006) to keep it read.

Shapes vary by site and every optional read degrades alone, with a note:
schedules absent -> line amounts unknown, receipts absent -> two-way only,
voucher lines absent -> the tie cannot be computed at all and says so.
Currencies are never summed across; totals are per currency like everywhere
else in this codebase.
"""
from __future__ import annotations

import datetime as dt

from . import queries as q
from .engine import r2
from .modules import ModuleError, ModulePacks, _iso
from .engine import resolve_party_ref

# Bounded output: the workbench ranks, the chain shows one supplier's tail.
EXCEPTION_CAP = 50
PO_CAP = 20
DETAIL_CAP = 200

PO_STATUS = {"A": "Approved", "D": "Dispatched", "O": "Open",
             "PA": "Pending approval", "C": "Complete", "X": "Canceled"}
RECV_STATUS = {"O": "Open", "H": "Hold", "M": "Moved", "C": "Complete",
               "X": "Canceled"}
MATCH_STATUS = {"T": "Matched", "E": "Match exceptions exist",
                "D": "Match dispute", "N": "No match required",
                "O": "Match overridden"}


class Procurement:
    """Chain reads over the same connection and shape caches the AP pack
    uses — a site whose PS_VOUCHER dates differently must not need the fix
    twice."""

    def __init__(self, mp: ModulePacks):
        self.mp = mp
        self.e = mp.e
        self.db = mp.db

    # ------------------------------------------------------------- helpers
    def _seq_col(self) -> str:
        """RECV_SHIP_SEQ_NBR, as this site spells it.

        The delivered name has SHIP in full; at least one customization in
        the wild abbreviates it to SHP. This was the open question that
        deferred the whole feature once — resolved the way every other
        shape question here is: ask the catalog, never assume.
        """
        cols = self.mp._cols("PS_RECV_LN_SHIP")
        for name in ("RECV_SHIP_SEQ_NBR", "RECV_SHP_SEQ_NBR"):
            if name in cols:
                return name
        return ""

    def _po_amounts(self, bu: str, po_ids: list, notes: list) -> dict:
        """Ordered qty/amount per (po, line, sched) — from the SCHEDULE.

        Delivered FSCM prices the schedule, not the line. A site whose
        PS_PO_LINE_SHIP is absent loses ordered amounts, and every match
        that needs them says UNKNOWN rather than treating missing as zero —
        zero would read as 'everything vouchered is over the order'.
        """
        if not po_ids:
            return {}
        cols = self.mp._cols("PS_PO_LINE_SHIP")
        if not cols:
            notes.append("PS_PO_LINE_SHIP is not readable here — ordered "
                         "amounts are unknown, so price and quantity checks "
                         "against the order did not run.")
            return {}
        expr, binds = self._in("po", po_ids)
        rows, _ = self.db.query(
            f"SELECT PO_ID AS po, LINE_NBR AS ln, SCHED_NBR AS sched, "
            f"QTY_PO AS qty, PRICE_PO AS price, MERCHANDISE_AMT AS amt "
            f"FROM {self.db.prefix}PS_PO_LINE_SHIP "
            f"WHERE BUSINESS_UNIT = :bu AND PO_ID IN {expr}",
            {"bu": bu, **binds}, max_rows=DETAIL_CAP * 2)
        return {(str(r["po"]), int(r["ln"] or 0), int(r["sched"] or 0)): r
                for r in rows}

    def _receipts(self, bu: str, po_ids: list, notes: list) -> list:
        if not po_ids:
            return []
        cols = self.mp._cols("PS_RECV_LN_SHIP")
        if not cols:
            notes.append("PS_RECV_LN_SHIP is not readable here — the "
                         "receipt leg is not recorded, so the match is "
                         "voucher-to-order (two-way) only.")
            return []
        expr, binds = self._in("po", po_ids)
        rows, _ = self.db.query(
            f"SELECT RECEIVER_ID AS recv, RECV_LN_NBR AS ln, "
            f"PO_ID AS po, LINE_NBR AS po_ln, SCHED_NBR AS sched, "
            f"QTY_SH_ACCPT_VUOM AS qty, MERCHANDISE_AMT AS amt "
            f"FROM {self.db.prefix}PS_RECV_LN_SHIP "
            f"WHERE BUSINESS_UNIT = :bu AND PO_ID IN {expr}",
            {"bu": bu, **binds}, max_rows=DETAIL_CAP * 2)
        return rows

    def _in(self, prefix: str, values) -> tuple:
        binds = {f"{prefix}{i}": v for i, v in enumerate(values)}
        return "(" + ", ".join(f":{k}" for k in binds) + ")", binds

    def _vendor_name(self, setid: str, vendor_id: str) -> str:
        try:
            rows, _ = self.db.query(
                f"SELECT NAME1 AS n FROM {self.db.prefix}PS_VENDOR "
                "WHERE SETID = :s AND VENDOR_ID = :v",
                {"s": setid, "v": vendor_id}, max_rows=1)
            return str(rows[0]["n"]) if rows else ""
        except Exception:                               # noqa: BLE001
            return ""

    # -------------------------------------------------------- the workbench
    def match_exceptions(self, business_unit: str = "", months: int = 12,
                         as_of_date: str = "") -> dict:
        """Every break in the purchase-to-pay tie, ranked by money.

        Four populations, each computed, none guessed:
          over_order        vouchered above the ordered amount (price)
          not_received      vouchered quantity above accepted quantity
          no_receipt        a PO-referenced voucher with no receipt at all
          never_invoiced    accepted receipts no voucher ever referenced
        plus awaiting_receipt: dispatched orders nothing has arrived
        against — excluding canceled orders, which merely LOOK the same.
        """
        bu = self.mp._bu(business_unit)
        asof = self.mp._asof(as_of_date)
        setid = self.e.resolve_setid(bu, "VENDOR")
        window = max(int(months or 12), 1)
        anchor = _iso(asof)
        month = anchor.month - window
        year = anchor.year
        while month <= 0:
            month += 12
            year -= 1
        since = anchor.replace(year=year, month=month,
                               day=min(anchor.day, 28)).isoformat()
        notes: list = []
        p = self.db.prefix

        self.mp._need("PS_PO_HDR", ["BUSINESS_UNIT", "PO_ID", "VENDOR_ID"])
        vl_cols = self.mp._cols("PS_VOUCHER_LINE")
        if not vl_cols:
            return {
                "supported": False, "business_unit": bu,
                "detail": "PS_VOUCHER_LINE is not readable here, and the "
                          "voucher-to-order tie lives on it. Without that "
                          "record the three-way match cannot be computed — "
                          "this is UNKNOWN, not 'no exceptions'.",
            }

        # Voucher lines that reference a PO, joined to their vouchers.
        vch_cols = self.mp._cols("PS_VOUCHER")
        match_sel = ("V.MATCH_STATUS_VCHR" if "MATCH_STATUS_VCHR" in vch_cols
                     else "NULL")
        vlines, _ = self.db.query(
            f"SELECT L.VOUCHER_ID AS vid, L.PO_ID AS po, "
            f"L.LINE_NBR AS po_ln, L.SCHED_NBR AS sched, "
            f"L.RECEIVER_ID AS recv, L.QTY_VCHR AS qty, "
            f"L.MERCHANDISE_AMT AS amt, V.VENDOR_ID AS vendor, "
            f"V.INVOICE_DT AS inv_dt, V.CURRENCY_CD AS currency, "
            f"V.CLOSE_STATUS AS close_status, {match_sel} AS match_status "
            f"FROM {p}PS_VOUCHER_LINE L "
            f"JOIN {p}PS_VOUCHER V ON V.BUSINESS_UNIT = L.BUSINESS_UNIT "
            f"AND V.VOUCHER_ID = L.VOUCHER_ID "
            f"WHERE L.BUSINESS_UNIT = :bu "
            f"AND {q.nonblank('L.PO_ID')} AND V.INVOICE_DT >= :since "
            f"AND V.INVOICE_DT <= :asof",
            {"bu": bu, "since": since, "asof": asof},
            max_rows=DETAIL_CAP * 4)

        po_ids = sorted({str(r["po"]) for r in vlines})
        # Receipts and orders for every PO in the window — vouchered or not,
        # because never-invoiced receipts are by definition on POs no
        # voucher names.
        hdrs, _ = self.db.query(
            f"SELECT PO_ID AS po, VENDOR_ID AS vendor, PO_STATUS AS status, "
            f"PO_DT AS po_dt, CURRENCY_CD AS currency "
            f"FROM {p}PS_PO_HDR WHERE BUSINESS_UNIT = :bu "
            f"AND PO_DT >= :since AND PO_DT <= :asof ORDER BY PO_ID",
            {"bu": bu, "since": since, "asof": asof}, max_rows=DETAIL_CAP * 2)
        all_pos = sorted({str(r["po"]) for r in hdrs} | set(po_ids))
        ordered = self._po_amounts(bu, all_pos, notes)
        receipts = self._receipts(bu, all_pos, notes)
        have_receipts = bool(self.mp._cols("PS_RECV_LN_SHIP"))

        recv_by_key: dict = {}
        for r in receipts:
            key = (str(r["po"]), int(r["po_ln"] or 0), int(r["sched"] or 0))
            cur = recv_by_key.setdefault(key, {"qty": 0.0, "amt": 0.0,
                                               "receivers": set()})
            cur["qty"] += float(r["qty"] or 0)
            cur["amt"] += float(r["amt"] or 0)
            cur["receivers"].add(str(r["recv"]))

        hdr_by_po = {str(r["po"]): r for r in hdrs}
        over_order: list = []
        not_received: list = []
        no_receipt: list = []
        vouchered_keys = set()
        vouchered_pos = set()
        for r in vlines:
            key = (str(r["po"]), int(r["po_ln"] or 0), int(r["sched"] or 0))
            vouchered_keys.add((str(r["po"]), int(r["po_ln"] or 0)))
            vouchered_pos.add(str(r["po"]))
            amt = float(r["amt"] or 0)
            qty = float(r["qty"] or 0)
            entry = {
                "voucher_id": str(r["vid"]), "po_id": str(r["po"]),
                "vendor_id": str(r["vendor"] or ""),
                "currency": str(r["currency"] or ""),
                "system_match_status": (str(r["match_status"])
                                        if r["match_status"] else ""),
                "voucher_open": str(r["close_status"] or "") == "O",
            }
            po_row = ordered.get(key)
            if po_row is not None and amt > float(po_row["amt"] or 0) + 0.005:
                over_order.append({
                    **entry, "kind": "over_order",
                    "vouchered_amt": r2(amt),
                    "ordered_amt": r2(float(po_row["amt"] or 0)),
                    "over_by": r2(amt - float(po_row["amt"] or 0)),
                    "detail": (f"vouchered {r2(amt):,.2f} against an order "
                               f"of {r2(float(po_row['amt'] or 0)):,.2f}"),
                })
            if have_receipts:
                got = recv_by_key.get(key)
                if got is None:
                    no_receipt.append({
                        **entry, "kind": "no_receipt",
                        "vouchered_amt": r2(amt),
                        "detail": "no receipt is recorded against this "
                                  "order schedule",
                    })
                elif qty > float(got["qty"]) + 0.005:
                    price = (amt / qty) if qty else 0.0
                    not_received.append({
                        **entry, "kind": "not_received",
                        "vouchered_qty": qty, "received_qty": got["qty"],
                        "not_received_qty": r2(qty - got["qty"]),
                        "not_received_amt": r2((qty - got["qty"]) * price),
                        "detail": (f"vouchered {qty:g} units, received "
                                   f"{got['qty']:g}"),
                    })

        never_invoiced: list = []
        for key, got in recv_by_key.items():
            po, ln, _sched = key
            if (po, ln) in vouchered_keys:
                continue
            hdr = hdr_by_po.get(po, {})
            if str(hdr.get("status") or "") == "X":
                continue
            days = 0
            try:
                rcv_rows = [r for r in receipts
                            if str(r["po"]) == po
                            and int(r["po_ln"] or 0) == ln]
                # age from the receipt header date
                rh, _ = self.db.query(
                    f"SELECT MIN(RECEIPT_DT) AS d FROM {p}PS_RECV_HDR "
                    f"WHERE BUSINESS_UNIT = :bu AND RECEIVER_ID IN "
                    + self._in("rc", sorted({str(x['recv'])
                                             for x in rcv_rows}))[0],
                    {"bu": bu, **self._in("rc", sorted({
                        str(x["recv"]) for x in rcv_rows}))[1]}, max_rows=1)
                if rh and rh[0]["d"]:
                    days = (anchor - _iso(str(rh[0]["d"]))).days
            except Exception:                           # noqa: BLE001
                days = 0
            never_invoiced.append({
                "kind": "never_invoiced", "po_id": po,
                "vendor_id": str(hdr.get("vendor") or ""),
                "currency": str(hdr.get("currency") or ""),
                "received_amt": r2(got["amt"]),
                "received_qty": got["qty"],
                "receivers": sorted(got["receivers"]),
                "days_since_receipt": days,
                "detail": (f"{r2(got['amt']):,.2f} accepted "
                           f"{days} days ago and never vouchered"),
            })

        awaiting: list = []
        received_pos = {k[0] for k in recv_by_key}
        for po, hdr in hdr_by_po.items():
            status = str(hdr.get("status") or "")
            if status == "X":
                continue                    # the trap: canceled is not late
            if po in received_pos or po in vouchered_pos:
                continue
            amt = r2(sum(float(v["amt"] or 0) for k, v in ordered.items()
                         if k[0] == po))
            awaiting.append({
                "kind": "awaiting_receipt", "po_id": po,
                "vendor_id": str(hdr.get("vendor") or ""),
                "po_status": status,
                "po_status_meaning": PO_STATUS.get(status, status),
                "ordered_amt": amt,
                "currency": str(hdr.get("currency") or ""),
                "po_dt": str(hdr.get("po_dt") or ""),
            })

        for bucket in (over_order, not_received, no_receipt, never_invoiced):
            bucket.sort(key=lambda x: -(x.get("over_by")
                                        or x.get("not_received_amt")
                                        or x.get("vouchered_amt")
                                        or x.get("received_amt") or 0))

        def money(bucket, field):
            out: dict = {}
            for x in bucket:
                cur = x.get("currency") or "?"
                out[cur] = r2(out.get(cur, 0.0) + float(x.get(field) or 0))
            return [{"currency": c, "amount": a} for c, a in sorted(out.items())]

        system_counts: dict = {}
        for r in vlines:
            st = str(r["match_status"] or "")
            if st:
                system_counts[st] = system_counts.get(st, 0) + 0
        # count per voucher, not per line
        seen_vch = set()
        for r in vlines:
            vid = str(r["vid"])
            if vid in seen_vch:
                continue
            seen_vch.add(vid)
            st = str(r["match_status"] or "")
            if st:
                system_counts[st] = system_counts.get(st, 0) + 1

        return {
            "business_unit": bu, "as_of_date": asof,
            "window_months": window, "window_start": since,
            "supported": True,
            "exceptions": {
                "over_order": over_order[:EXCEPTION_CAP],
                "not_received": not_received[:EXCEPTION_CAP],
                "no_receipt": no_receipt[:EXCEPTION_CAP],
                "never_invoiced": never_invoiced[:EXCEPTION_CAP],
                "awaiting_receipt": awaiting[:EXCEPTION_CAP],
            },
            "totals": {
                "over_order": money(over_order, "over_by"),
                "not_received": money(not_received, "not_received_amt"),
                "no_receipt": money(no_receipt, "vouchered_amt"),
                "never_invoiced": money(never_invoiced, "received_amt"),
                "awaiting_receipt": money(awaiting, "ordered_amt"),
            },
            "counts": {k: len(v) for k, v in (
                ("over_order", over_order), ("not_received", not_received),
                ("no_receipt", no_receipt),
                ("never_invoiced", never_invoiced),
                ("awaiting_receipt", awaiting))},
            "system_match_flags": {
                "counts": [{"status": s, "meaning": MATCH_STATUS.get(s, s),
                            "vouchers": n}
                           for s, n in sorted(system_counts.items())],
                "note": ("The system's own MATCH_STATUS_VCHR, counted per "
                         "voucher. The exceptions above are recomputed from "
                         "the document lines; where the two disagree, the "
                         "disagreement is itself worth reading — an "
                         "override, a tolerance, or rules changed since "
                         "the stamp.")
                if "MATCH_STATUS_VCHR" in vch_cols else
                "PS_VOUCHER here has no MATCH_STATUS_VCHR; only the "
                "recomputed exceptions above exist.",
            },
            "population": (
                f"PO-referenced voucher lines invoiced {since} to {asof} in "
                f"{bu}, orders dated in the same window, and the receipts "
                "against them. Vouchers with no PO reference are outside "
                "the match by definition."),
            "record_notes": notes,
        }

    # ------------------------------------------------------------ the chain
    def procurement_chain(self, reference: str = "", business_unit: str = "",
                          as_of_date: str = "") -> dict:
        """One reference — a PO, a receipt, a voucher, or a supplier — and
        the whole documentary chain around it, tied out per schedule."""
        ref = (reference or "").strip()
        if not ref:
            return {
                "scope_status": "reference_required",
                "detail": "Give a PO id, a receiver id, a voucher id, or a "
                          "supplier (id or name).",
            }
        bu = self.mp._bu(business_unit)
        asof = self.mp._asof(as_of_date)
        setid = self.e.resolve_setid(bu, "VENDOR")
        notes: list = []
        p = self.db.prefix
        self.mp._need("PS_PO_HDR", ["BUSINESS_UNIT", "PO_ID", "VENDOR_ID"])

        po_ids, how = self._resolve_reference(ref, bu, setid, notes)
        if isinstance(po_ids, dict):
            return po_ids                    # a refusal from the resolver
        if not po_ids:
            return {
                "scope_status": "reference_not_found",
                "detail": f"{ref!r} matches no purchase order, receipt, "
                          f"voucher or supplier in {bu}. This is NO DATA, "
                          "not a zero.",
                "business_unit": bu,
            }
        capped = False
        if len(po_ids) > PO_CAP:
            po_ids, capped = po_ids[:PO_CAP], True

        hdr_expr, hdr_binds = self._in("po", po_ids)
        hdrs, _ = self.db.query(
            f"SELECT PO_ID AS po, VENDOR_ID AS vendor, PO_STATUS AS status, "
            f"PO_DT AS po_dt, CURRENCY_CD AS currency "
            f"FROM {p}PS_PO_HDR WHERE BUSINESS_UNIT = :bu "
            f"AND PO_ID IN {hdr_expr} ORDER BY PO_DT, PO_ID",
            {"bu": bu, **hdr_binds}, max_rows=PO_CAP)
        ordered = self._po_amounts(bu, po_ids, notes)
        receipts = self._receipts(bu, po_ids, notes)
        have_receipts = bool(self.mp._cols("PS_RECV_LN_SHIP"))

        vl_cols = self.mp._cols("PS_VOUCHER_LINE")
        vlines: list = []
        if vl_cols:
            vlines, _ = self.db.query(
                f"SELECT L.VOUCHER_ID AS vid, L.PO_ID AS po, "
                f"L.LINE_NBR AS po_ln, L.SCHED_NBR AS sched, "
                f"L.RECEIVER_ID AS recv, L.QTY_VCHR AS qty, "
                f"L.MERCHANDISE_AMT AS amt, V.INVOICE_ID AS invoice, "
                f"V.INVOICE_DT AS inv_dt, V.DUE_DT AS due_dt, "
                f"V.GROSS_AMT AS gross, V.CLOSE_STATUS AS close_status, "
                f"V.CURRENCY_CD AS currency, V.VENDOR_ID AS vendor "
                f"FROM {p}PS_VOUCHER_LINE L "
                f"JOIN {p}PS_VOUCHER V ON V.BUSINESS_UNIT = L.BUSINESS_UNIT "
                f"AND V.VOUCHER_ID = L.VOUCHER_ID "
                f"WHERE L.BUSINESS_UNIT = :bu AND L.PO_ID IN {hdr_expr}",
                {"bu": bu, **hdr_binds}, max_rows=DETAIL_CAP * 2)
        else:
            notes.append("PS_VOUCHER_LINE is not readable here — vouchers "
                         "cannot be tied to these orders, so the voucher "
                         "and payment legs are missing from this chain.")

        # Payments for the vouchers found, via the cross reference.
        vids = sorted({str(r["vid"]) for r in vlines})
        paid_by_vch: dict = {}
        if vids and self.mp._cols("PS_PYMNT_VCHR_XREF"):
            v_expr, v_binds = self._in("v", vids)
            prows, _ = self.db.query(
                f"SELECT VOUCHER_ID AS vid, PYMNT_ID AS pid, "
                f"PAID_AMT AS amt FROM {p}PS_PYMNT_VCHR_XREF "
                f"WHERE BUSINESS_UNIT = :bu AND VOUCHER_ID IN {v_expr}",
                {"bu": bu, **v_binds}, max_rows=DETAIL_CAP)
            for r in prows:
                cur = paid_by_vch.setdefault(str(r["vid"]),
                                             {"paid": 0.0, "payments": []})
                cur["paid"] += float(r["amt"] or 0)
                cur["payments"].append(str(r["pid"]))

        recv_hdrs: dict = {}
        if receipts:
            rc_ids = sorted({str(r["recv"]) for r in receipts})
            rc_expr, rc_binds = self._in("rc", rc_ids)
            rh, _ = self.db.query(
                f"SELECT RECEIVER_ID AS recv, RECEIPT_DT AS dt, "
                f"RECV_STATUS AS status FROM {p}PS_RECV_HDR "
                f"WHERE BUSINESS_UNIT = :bu AND RECEIVER_ID IN {rc_expr}",
                {"bu": bu, **rc_binds}, max_rows=DETAIL_CAP)
            recv_hdrs = {str(r["recv"]): r for r in rh}

        chain: list = []
        breaks: list = []
        totals: dict = {}
        for hdr in hdrs:
            po = str(hdr["po"])
            status = str(hdr["status"] or "")
            cur = str(hdr["currency"] or "")
            po_scheds = {k: v for k, v in ordered.items() if k[0] == po}
            po_recv = [r for r in receipts if str(r["po"]) == po]
            po_vch = [r for r in vlines if str(r["po"]) == po]
            ordered_amt = r2(sum(float(v["amt"] or 0)
                                 for v in po_scheds.values()))
            received_amt = r2(sum(float(r["amt"] or 0) for r in po_recv))
            vouchered_amt = r2(sum(float(r["amt"] or 0) for r in po_vch))
            paid_amt = r2(sum(paid_by_vch.get(str(r["vid"]), {})
                              .get("paid", 0.0)
                              for r in {str(x["vid"]): x
                                        for x in po_vch}.values()))
            t = totals.setdefault(cur or "?", {"ordered": 0.0, "received": 0.0,
                                               "vouchered": 0.0, "paid": 0.0})
            t["ordered"] = r2(t["ordered"] + ordered_amt)
            t["received"] = r2(t["received"] + received_amt)
            t["vouchered"] = r2(t["vouchered"] + vouchered_amt)
            t["paid"] = r2(t["paid"] + paid_amt)

            entry = {
                "po_id": po, "vendor_id": str(hdr["vendor"] or ""),
                "po_status": status,
                "po_status_meaning": PO_STATUS.get(status, status),
                "po_dt": str(hdr["po_dt"] or ""), "currency": cur,
                "ordered_amt": ordered_amt if po_scheds else None,
                "received_amt": received_amt if have_receipts else None,
                "vouchered_amt": vouchered_amt if vl_cols else None,
                "paid_amt": paid_amt if vl_cols else None,
                "receipts": [
                    {"receiver_id": rc,
                     "receipt_dt": str((recv_hdrs.get(rc) or {})
                                       .get("dt") or ""),
                     "status": str((recv_hdrs.get(rc) or {})
                                   .get("status") or ""),
                     "accepted_qty": r2(sum(float(x["qty"] or 0)
                                            for x in po_recv
                                            if str(x["recv"]) == rc)),
                     "amount": r2(sum(float(x["amt"] or 0) for x in po_recv
                                      if str(x["recv"]) == rc))}
                    for rc in sorted({str(x["recv"]) for x in po_recv})],
                "vouchers": [
                    {"voucher_id": vid,
                     "invoice": str(rows[0]["invoice"] or ""),
                     "invoice_dt": str(rows[0]["inv_dt"] or ""),
                     "due_dt": str(rows[0]["due_dt"] or ""),
                     "open": str(rows[0]["close_status"] or "") == "O",
                     "amount": r2(sum(float(x["amt"] or 0) for x in rows)),
                     "paid": r2(paid_by_vch.get(vid, {}).get("paid", 0.0)),
                     "payments": paid_by_vch.get(vid, {}).get("payments", [])}
                    for vid, rows in
                    {v: [x for x in po_vch if str(x["vid"]) == v]
                     for v in sorted({str(x["vid"]) for x in po_vch})}
                    .items()],
            }

            # The breaks, per order, from the same figures shown above.
            if po_scheds and vl_cols and vouchered_amt > ordered_amt + 0.005:
                breaks.append({
                    "po_id": po, "kind": "over_order", "currency": cur,
                    "vouchered_amt": vouchered_amt,
                    "ordered_amt": ordered_amt,
                    "over_by": r2(vouchered_amt - ordered_amt),
                    "detail": f"vouchered {vouchered_amt:,.2f} against an "
                              f"order of {ordered_amt:,.2f}",
                })
            if have_receipts and po_vch:
                v_qty = sum(float(x["qty"] or 0) for x in po_vch)
                r_qty = sum(float(x["qty"] or 0) for x in po_recv)
                if not po_recv:
                    breaks.append({
                        "po_id": po, "kind": "no_receipt", "currency": cur,
                        "vouchered_amt": vouchered_amt,
                        "detail": "vouchered with no receipt recorded",
                    })
                elif v_qty > r_qty + 0.005:
                    breaks.append({
                        "po_id": po, "kind": "not_received", "currency": cur,
                        "vouchered_qty": v_qty, "received_qty": r_qty,
                        "detail": f"vouchered {v_qty:g} units, received "
                                  f"{r_qty:g}",
                    })
            if have_receipts and po_recv and vl_cols and not po_vch \
                    and status != "X":
                breaks.append({
                    "po_id": po, "kind": "never_invoiced", "currency": cur,
                    "received_amt": received_amt,
                    "detail": f"{received_amt:,.2f} received and never "
                              "vouchered",
                })
            if status == "X":
                entry["note"] = ("This order is CANCELED — nothing is "
                                 "expected against it and nothing here "
                                 "counts it as late.")
            chain.append(entry)

        vendors = sorted({c["vendor_id"] for c in chain if c["vendor_id"]})
        out = {
            "business_unit": bu, "as_of_date": asof,
            "resolved": how, "reference": ref,
            "orders": chain,
            "breaks": breaks,
            "chain_totals": [
                {"currency": c, **{k: r2(v) for k, v in t.items()},
                 "sum_only": True}
                for c, t in sorted(totals.items())],
            "suppliers": vendors,
            "basis": ("Order amounts from the PO schedules, receipts from "
                      "the shipment lines, vouchers from the voucher lines "
                      "that reference the order, payments from the "
                      "cross-reference. Compared per schedule; nothing is "
                      "estimated."),
            "record_notes": notes,
        }
        if capped:
            out["orders_truncated"] = True
            out["orders_note"] = (f"the {PO_CAP} most recent orders; narrow "
                                  "to one PO id for the full detail")
        if len(vendors) == 1 and breaks:
            out["next_steps"] = [
                f"get_vendor_payables_network(vendor_id={vendors[0]!r}) "
                "shows this supplier's whole payables picture, including "
                "identity links."]
        return out

    def _resolve_reference(self, ref: str, bu: str, setid: str,
                           notes: list):
        """PO first, then receipt, then voucher, then supplier by id/name.

        The order matters only for the pathological id that exists in two
        masters at once; document ids win over supplier ids because a
        document names ONE chain and a supplier names many.
        """
        p = self.db.prefix
        r = ref.upper()
        rows, _ = self.db.query(
            f"SELECT PO_ID AS x FROM {p}PS_PO_HDR WHERE BUSINESS_UNIT = :bu "
            "AND UPPER(PO_ID) = :r", {"bu": bu, "r": r}, max_rows=1)
        if rows:
            return [str(rows[0]["x"])], {"kind": "po", "id": str(rows[0]["x"])}
        if self.mp._cols("PS_RECV_LN_SHIP"):
            rows, _ = self.db.query(
                f"SELECT DISTINCT PO_ID AS x FROM {p}PS_RECV_LN_SHIP "
                "WHERE BUSINESS_UNIT = :bu AND UPPER(RECEIVER_ID) = :r "
                "ORDER BY PO_ID", {"bu": bu, "r": r}, max_rows=PO_CAP + 1)
            if rows:
                return ([str(x["x"]) for x in rows],
                        {"kind": "receipt", "id": ref.upper()})
        if self.mp._cols("PS_VOUCHER_LINE"):
            rows, _ = self.db.query(
                f"SELECT DISTINCT PO_ID AS x FROM {p}PS_VOUCHER_LINE "
                "WHERE BUSINESS_UNIT = :bu AND UPPER(VOUCHER_ID) = :r "
                f"AND {q.nonblank('PO_ID')} ORDER BY PO_ID",
                {"bu": bu, "r": r}, max_rows=PO_CAP + 1)
            if rows:
                return ([str(x["x"]) for x in rows],
                        {"kind": "voucher", "id": ref.upper()})
        # A supplier, by id or name — the same resolution every party-named
        # tool uses, with the same ask-when-ambiguous behaviour.
        vid, read_as, refusal = resolve_party_ref(
            ref, lambda term: self.mp.search_vendors(
                term, limit=10, business_unit=bu)["vendors"],
            "vendor_id", "supplier")
        if refusal:
            refusal.setdefault("business_unit", bu)
            # An AMBIGUOUS supplier is a real question — pass it through.
            # A supplier miss is not the finding here: the reference already
            # missed three document masters too, and "no supplier matches
            # 'PO9999'" reads as if only suppliers were tried.
            if refusal.get("scope_status") == "supplier_not_found":
                refusal["scope_status"] = "reference_not_found"
                refusal["detail"] = (
                    f"{ref!r} matches no purchase order, receipt, voucher "
                    f"or supplier in {bu}. This is NO DATA, not a zero.")
            return refusal, {}
        if read_as:
            notes.append(read_as)
        rows, _ = self.db.query(
            f"SELECT PO_ID AS x FROM {p}PS_PO_HDR WHERE BUSINESS_UNIT = :bu "
            "AND VENDOR_ID = :v ORDER BY PO_DT DESC, PO_ID DESC",
            {"bu": bu, "v": vid}, max_rows=PO_CAP * 2)
        return ([str(x["x"]) for x in rows],
                {"kind": "supplier", "id": vid,
                 "name": self._vendor_name(setid, vid)})
