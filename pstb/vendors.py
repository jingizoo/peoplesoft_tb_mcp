"""The connected view of a supplier: family, payables, cash, and who they
are the same as.

The AR side of this shipped first (``pstb/relationships.py``) and this is
its mirror, deliberately: the same section shapes, the same disclosure
keys, the same rule that the map carries no money. What is genuinely
different is on the payables side — a supplier can share a bank account or
a taxpayer id with another supplier, and that is the single most useful
thing a payables team can be told, because it is how a duplicate vendor
master or a redirected payment shows up in the data before it shows up in
the bank statement.

Which is also where the care goes.

  - **Bank account numbers and taxpayer ids never leave the database.**
    The equality test happens in SQL; only values already known to be
    shared are hashed, and only the hash reaches the payload. There is no
    masked form, no last four, no partial: every one of those was measured
    leaking through the grounding guard as a figure the model may then
    quote. The token is letters only for the same reason — a hex digest
    injects eight phantom numbers into the allowlist.
  - **The hash is keyed.** An unsalted digest of a nine-digit taxpayer id
    is a billion-entry rainbow table, which is the raw value with extra
    steps. No salt configured means the whole identity section reports
    itself unsupported; there is no unsalted path.
  - **Two suppliers are the same company only when the system says so.**
    Membership comes from PS_VENDOR's corporate hierarchy and nothing
    else. A shared bank account is reported as a shared bank account — an
    observation to investigate — and never as an identity, never as a
    family edge, never with a confidence score. "Ridgeline Supply Group"
    is a different company from "Ridgeline Supply Co", the sample contains
    it on purpose, and a test walks into that trap.

Cost: two reads to resolve the family, then a concurrent fan-out. Nothing
here runs at page load.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .db import DbError
from .engine import r2
from .modules import ModuleError, ModulePacks, _iso, _iso_opt

MEMBER_CAP = 60
VOUCHER_DETAIL_CAP = 300
PAYMENT_CAP = 200
# Rows a shared-key self-join may return. The join is bounded by the number
# of COLLISIONS, not by the supplier count, so this is generous by design —
# but an installation with a shared-services remit account would otherwise
# return a cross product of itself.
LINK_SCAN_CAP = 20_000
SHOWN_ROWS = 25
NODE_CAP = 400

SALT_ENV = "PSTB_MATCH_SALT"
TWO_QUOTES = "''"      # a SQL empty-string literal inside an f-string

# Digits are forbidden in a token, and this is not cosmetic. The grounding
# guard treats every numeral in a payload as a figure the model is allowed
# to state, so a hex digest "a3f9c24b7e015d8829" grounds 3, 7, 9, 15, 24,
# 8829 and more — measured, not assumed. Sixteen letters carry the same
# four bits per character with none of that, and I/O/0/1 are left out so a
# token read aloud or retyped cannot be confused.
_ALPHABET = "ABCDEFGHJKLMNPQR"
_STRIP = re.compile(r"[^A-Z0-9]")


def _salt() -> bytes:
    return (os.environ.get(SALT_ENV) or "").strip().encode()


def _normalise(value: str) -> str:
    """Compare what the identifier IS, not how it was typed.

    "045.600.1122" and "45-6001122" are one taxpayer id entered by two
    people. Upper-case, drop everything that is not a letter or digit,
    then drop leading zeros — a formatting difference is not a different
    company, and treating it as one is how the check misses the pair it
    exists to find.
    """
    bare = _STRIP.sub("", (value or "").upper())
    return bare.lstrip("0") or bare


def _token(prefix: str, value: str, salt: bytes) -> str:
    """A stable, keyed, digit-free label for a value that must not travel."""
    digest = hashlib.blake2b(_normalise(value).encode(), key=salt,
                             digest_size=8).digest()
    out = []
    for byte in digest:
        out.append(_ALPHABET[byte >> 4])
        out.append(_ALPHABET[byte & 0x0F])
    return f"{prefix}-{''.join(out)}"


def _days(a, b) -> int:
    try:
        return (_iso(b) - _iso(a)).days
    except Exception:                       # noqa: BLE001
        return 0


class VendorNetwork:
    """Payables-side relationships over the records the AP tools already use.

    Constructed from ModulePacks rather than duplicating it: a site whose
    PS_VOUCHER lacks CLOSE_STATUS must not need that adaptation applied
    twice, and duplicate detection is delegated rather than reimplemented
    so the same exposure is never counted two different ways.
    """

    def __init__(self, modules: ModulePacks):
        self.mp = modules
        self.e = modules.e
        self.db = modules.db
        self.cfg = modules.cfg if hasattr(modules, "cfg") else modules.e.cfg

    # ------------------------------------------------------------- helpers
    def _in(self, prefix: str, values) -> tuple:
        binds = {f"{prefix}{i}": v for i, v in enumerate(values)}
        return "(" + ", ".join(f":{k}" for k in binds) + ")", binds

    def _pick(self, table: str, candidates: list) -> str:
        """The first candidate column this site actually has.

        Several PeopleSoft AP columns are spelled differently across tools
        releases, and hardcoding one spelling turns a working instance into
        an ORA-00904. An unreadable catalog means "assume the reference
        shape", never "the column is absent".
        """
        cols = self.db.columns(table)
        if not cols:
            return candidates[0]
        return next((c for c in candidates if c in cols), "")

    # -------------------------------------------------------------- family
    def _family(self, setid: str, vendor_id: str, include_family: bool,
                notes: list) -> list:
        cols = self.db.columns("PS_VENDOR")
        name_c = self._pick("PS_VENDOR", ["NAME1", "VENDOR_NAME_SHORT"])
        stat_c = self._pick("PS_VENDOR", ["VENDOR_STATUS"])
        # Two spellings in the wild, and the concept is the same one
        # PS_CUSTOMER spells CORPORATE_CUST_ID.
        corp_c = self._pick("PS_VENDOR", ["CORPORATE_VENDOR",
                                          "CORPORATE_VNDR_ID"])
        p = self.db.prefix
        name_sel = f"V.{name_c} AS name" if name_c else "NULL AS name"
        stat_sel = f"V.{stat_c} AS status" if stat_c else "NULL AS status"
        corp_sel = f"V.{corp_c} AS parent" if corp_c else "NULL AS parent"

        try:
            rows, _ = self.db.query(
                f"SELECT V.VENDOR_ID AS vendor_id, {name_sel}, {stat_sel}, "
                f"{corp_sel} FROM {p}PS_VENDOR V "
                "WHERE V.SETID = :setid AND V.VENDOR_ID = :vid",
                {"setid": setid, "vid": vendor_id}, max_rows=1)
        except DbError as e:
            if not corp_c:
                raise
            notes.append(f"PS_VENDOR.{corp_c} could not be read ({e}); the "
                         "corporate supplier hierarchy was not consulted and "
                         "this answer covers one supplier.")
            corp_c = ""
            rows, _ = self.db.query(
                f"SELECT V.VENDOR_ID AS vendor_id, {name_sel}, {stat_sel}, "
                f"NULL AS parent FROM {p}PS_VENDOR V "
                "WHERE V.SETID = :setid AND V.VENDOR_ID = :vid",
                {"setid": setid, "vid": vendor_id}, max_rows=1)
        if not rows:
            return []

        def member(r, role: str) -> dict:
            return {"vendor_id": str(r["vendor_id"]),
                    "name": str(r["name"]) if r.get("name") else "",
                    "status": str(r["status"]) if r.get("status") else "",
                    "corporate_parent": (str(r["parent"])
                                         if r.get("parent") else ""),
                    "role": role}

        anchor = member(rows[0], "anchor")
        family = [anchor]
        if not corp_c:
            if cols and include_family:
                notes.append(
                    "PS_VENDOR here records no corporate supplier column, so "
                    "this site does not record a supplier hierarchy and the "
                    "answer covers this supplier only. Grouping suppliers "
                    "any other way — by name, address or bank account — "
                    "would be a consolidation nobody approved.")
            return family
        if not include_family:
            return family
        parent = anchor["corporate_parent"] or anchor["vendor_id"]
        try:
            rows, _ = self.db.query(
                f"SELECT V.VENDOR_ID AS vendor_id, {name_sel}, {stat_sel}, "
                f"V.{corp_c} AS parent FROM {p}PS_VENDOR V "
                f"WHERE V.SETID = :setid AND V.{corp_c} = :parent "
                "ORDER BY V.VENDOR_ID",
                {"setid": setid, "parent": parent}, max_rows=MEMBER_CAP + 1)
        except DbError as e:
            notes.append(f"The supplier family could not be read ({e}); this "
                         "answer covers one supplier.")
            return family
        seen = {anchor["vendor_id"]}
        for r in rows[:MEMBER_CAP]:
            vid = str(r["vendor_id"])
            if vid in seen:
                continue
            seen.add(vid)
            family.append(member(r, "parent" if vid == parent else "member"))
        if len(rows) > MEMBER_CAP:
            notes.append(
                f"This supplier family has more than {MEMBER_CAP} members; "
                f"the first {MEMBER_CAP} by supplier ID are included and the "
                "totals cover only those.")
        if anchor["corporate_parent"] and \
                anchor["corporate_parent"] != anchor["vendor_id"]:
            anchor["role"] = "anchor (subsidiary)"
        return family

    # ------------------------------------------------------------ branches
    def _payables(self, bu: str, ids: list, asof: str) -> dict:
        """Open vouchers per supplier — exact, aggregated by the database."""
        cols = self.db.columns("PS_VOUCHER")
        p = self.db.prefix
        expr, binds = self._in("v", ids)
        due_c = "DUE_DT" if (not cols or "DUE_DT" in cols) else ""
        cur_c = "CURRENCY_CD" if (not cols or "CURRENCY_CD" in cols) else ""
        notes: list = []
        if not due_c:
            notes.append("PS_VOUCHER here has no DUE_DT; overdue cannot be "
                         "computed and amounts are grouped by supplier only.")
        if "CLOSE_STATUS" in (cols or {"CLOSE_STATUS"}):
            open_pred = "W.CLOSE_STATUS = 'O'"
        else:
            open_pred = (f"NOT EXISTS (SELECT 1 FROM {p}PS_PYMNT_VCHR_XREF X "
                         "WHERE X.BUSINESS_UNIT = W.BUSINESS_UNIT "
                         "AND X.VOUCHER_ID = W.VOUCHER_ID)")
            notes.append("PS_VOUCHER here has no CLOSE_STATUS; 'open' means "
                         "no payment cross-reference exists, which misses "
                         "partial payments.")
        cur_sel = f"W.{cur_c}" if cur_c else "''"
        due_sel = f"MIN(W.{due_c})" if due_c else "NULL"
        cur_grp = f", W.{cur_c}" if cur_c else ""
        rows, _ = self.db.query(
            f"SELECT W.VENDOR_ID AS vendor_id, {cur_sel} AS currency, "
            "COUNT(*) AS vouchers, SUM(W.GROSS_AMT) AS open_amount, "
            f"{due_sel} AS oldest_due "
            f"FROM {p}PS_VOUCHER W WHERE W.BUSINESS_UNIT = :bu "
            f"AND {open_pred} AND W.VENDOR_ID IN {expr} "
            f"GROUP BY W.VENDOR_ID{cur_grp}",
            {"bu": bu, **binds}, max_rows=len(ids) * 10)

        by_vendor: dict = {}
        by_currency: dict = {}
        for r in rows:
            cur = str(r["currency"] or "")
            entry = {"vendor_id": str(r["vendor_id"]), "currency": cur,
                     "open_amount": r2(float(r["open_amount"] or 0)),
                     "vouchers": int(r["vouchers"] or 0),
                     "oldest_due_dt": (str(r["oldest_due"])[:10]
                                       if r.get("oldest_due") else "")}
            by_vendor.setdefault(entry["vendor_id"], []).append(entry)
            agg = by_currency.setdefault(cur, {
                "currency": cur, "open_amount": 0.0, "vouchers": 0})
            agg["open_amount"] = r2(agg["open_amount"] + entry["open_amount"])
            agg["vouchers"] += entry["vouchers"]
        return {"by_vendor": by_vendor,
                "totals_by_currency": sorted(by_currency.values(),
                                             key=lambda x: -x["open_amount"]),
                "notes": notes}

    def _open_vouchers(self, bu: str, ids: list, asof: str) -> dict:
        """The individual open vouchers, for the map and for looking at."""
        cols = self.db.columns("PS_VOUCHER") or set()
        p = self.db.prefix
        expr, binds = self._in("v", ids)

        def col(name, default="NULL"):
            return f"W.{name}" if (not cols or name in cols) else default

        if "CLOSE_STATUS" in (cols or {"CLOSE_STATUS"}):
            open_pred = "W.CLOSE_STATUS = 'O'"
        else:
            open_pred = (f"NOT EXISTS (SELECT 1 FROM {p}PS_PYMNT_VCHR_XREF X "
                         "WHERE X.BUSINESS_UNIT = W.BUSINESS_UNIT "
                         "AND X.VOUCHER_ID = W.VOUCHER_ID)")
        rows, _ = self.db.query(
            f"SELECT W.VENDOR_ID AS vendor_id, W.VOUCHER_ID AS voucher_id, "
            f"{col('INVOICE_ID')} AS invoice_id, W.GROSS_AMT AS amount, "
            f"{col('CURRENCY_CD', TWO_QUOTES)} AS currency, "
            f"{col('INVOICE_DT')} AS invoice_dt, {col('DUE_DT')} AS due_dt, "
            f"{col('ENTRY_STATUS')} AS entry_status, "
            f"{col('POST_STATUS')} AS post_status "
            f"FROM {p}PS_VOUCHER W WHERE W.BUSINESS_UNIT = :bu "
            f"AND {open_pred} AND W.VENDOR_ID IN {expr} "
            "ORDER BY W.VENDOR_ID, W.VOUCHER_ID",
            {"bu": bu, **binds}, max_rows=VOUCHER_DETAIL_CAP + 1)
        truncated = len(rows) > VOUCHER_DETAIL_CAP
        out = []
        for r in rows[:VOUCHER_DETAIL_CAP]:
            due = _iso_opt(r.get("due_dt"))
            out.append({
                "vendor_id": str(r["vendor_id"]),
                "voucher_id": str(r["voucher_id"]),
                "invoice_id": str(r.get("invoice_id") or ""),
                "amount": r2(float(r["amount"] or 0)),
                "currency": str(r.get("currency") or ""),
                "invoice_date": str(r.get("invoice_dt") or "")[:10],
                "due_date": str(r.get("due_dt") or "")[:10],
                "days_past_due": (max(0, (_iso(asof) - due).days)
                                  if due else 0),
                "entry_status": str(r.get("entry_status") or ""),
                "post_status": str(r.get("post_status") or ""),
            })
        out.sort(key=lambda v: -abs(v["amount"]))
        return {"vouchers": out, "truncated": truncated}

    def _spend(self, bu: str, ids: list, since: str) -> dict:
        """Cash actually paid, scoped through the voucher cross-reference.

        PS_PAYMENT_TBL carries no business unit — a payment belongs to a pay
        cycle — so the unit is applied one hop away or disclosed as absent.
        """
        if not self.db.columns("PS_PAYMENT_TBL"):
            return {"supported": False, "payments": [],
                    "note": "PS_PAYMENT_TBL is not readable here; cash paid "
                            "is not reported. Open payables are unaffected."}
        p = self.db.prefix
        expr, binds = self._in("v", ids)
        xref = self.db.columns("PS_PYMNT_VCHR_XREF")
        bu_pred = ("" if (xref and not {"BUSINESS_UNIT", "PYMNT_ID"} <= xref)
                   else f" AND EXISTS (SELECT 1 FROM {p}PS_PYMNT_VCHR_XREF X "
                        "WHERE X.PYMNT_ID = M.PYMNT_ID "
                        "AND X.BUSINESS_UNIT = :bu)")
        params = {"since": since, **binds}
        if bu_pred:
            params["bu"] = bu
        has_status = self.db.has_column("PS_PAYMENT_TBL", "PYMNT_STATUS")
        void = " AND M.PYMNT_STATUS <> 'V'" if has_status else ""
        sql = (f"SELECT M.VENDOR_ID AS vendor_id, COUNT(*) AS payments, "
               "SUM(M.PYMNT_AMT) AS paid, MAX(M.PYMNT_DT) AS last_dt "
               f"FROM {p}PS_PAYMENT_TBL M "
               f"WHERE M.PYMNT_DT >= {self.db.date_bind('since')}"
               f"{void} AND M.VENDOR_ID IN {expr}{{scope}} "
               "GROUP BY M.VENDOR_ID")
        scoped, note = True, ""
        try:
            rows, _ = self.db.query(sql.format(scope=bu_pred), params,
                                    max_rows=len(ids) * 4)
        except DbError as e:
            if not bu_pred:
                raise
            scoped = False
            note = (f"The payment cross-reference could not be read ({e}), "
                    "and PS_PAYMENT_TBL carries no business unit, so the "
                    "cash figures cover the WHOLE INSTALLATION.")
            rows, _ = self.db.query(
                sql.format(scope=""),
                {k: v for k, v in params.items() if k != "bu"},
                max_rows=len(ids) * 4)
        if not bu_pred and not note:
            scoped = False
            note = ("PS_PYMNT_VCHR_XREF has no BUSINESS_UNIT/PYMNT_ID pair "
                    "here, so the cash figures cover the WHOLE "
                    "INSTALLATION, not this unit alone.")
        return {"supported": True, "scoped_to_business_unit": scoped,
                "note": note,
                "by_vendor": {str(r["vendor_id"]): {
                    "vendor_id": str(r["vendor_id"]),
                    "payments": int(r["payments"] or 0),
                    "paid": r2(float(r["paid"] or 0)),
                    "last_payment_dt": str(r.get("last_dt") or "")[:10],
                } for r in rows}}

    def _identity(self, setid: str, ids: list) -> dict:
        """Suppliers that share a bank account or a taxpayer id.

        The equality test runs in the DATABASE. Only values already known
        to collide are read, and only their keyed hash is returned — the
        account number and the taxpayer id never enter this process's
        payload, are never logged, and are never shown. A shared key is an
        observation to investigate, not a claim that two suppliers are one
        company; nothing here creates a family edge.
        """
        salt = _salt()
        if not salt:
            return {"supported": False, "links": [], "note": (
                f"{SALT_ENV} is not set, so shared bank accounts and shared "
                "taxpayer ids cannot be checked. The check compares KEYED "
                "hashes; an unsalted hash of a nine-digit identifier is "
                "reversible, so there is deliberately no unsalted path. "
                "Set it in the console (Settings -> Secrets) and restart.")}
        p = self.db.prefix
        links, notes, checked = [], [], []

        def collide(record: str, key_col: str, id_col: str, prefix: str,
                    label: str, extra_where: str = "") -> None:
            """Group suppliers by the NORMALISED key, never by the raw one.

            A SQL self-join on the raw column would be cheaper and would
            miss the case this check exists for: "045.600.1122" and
            "45-6001122" are one taxpayer id typed by two people, and
            literal equality says they are two. So the key column is read
            once for this SETID — a vendor-sized table, one row per
            supplier, not a ledger — normalised, and hashed.

            The raw value lives in a local for exactly as long as it takes
            to hash it. It is never stored, never logged, and never put in
            a payload; the token is what the rest of this module sees.
            """
            if not key_col:
                notes.append(
                    f"{record} has no recognisable {label} column here, so "
                    f"suppliers were NOT checked for a shared {label}. That "
                    "is UNKNOWN, not 'none found'.")
                return
            try:
                rows, truncated = self.db.query(
                    f"SELECT {id_col} AS vid, {key_col} AS k "
                    f"FROM {p}{record} "
                    f"WHERE SETID = :setid AND {key_col} IS NOT NULL"
                    f"{extra_where}",
                    {"setid": setid}, max_rows=LINK_SCAN_CAP)
            except DbError as e:
                notes.append(f"{record} could not be read ({e}), so "
                             f"suppliers were NOT checked for a shared "
                             f"{label}. That is UNKNOWN, not 'none found'.")
                return
            checked.append(label)
            groups: dict = {}
            for r in rows:
                raw = str(r["k"] or "")
                if not _normalise(raw):
                    continue          # blank or all-zero is not an identifier
                groups.setdefault(_token(prefix, raw, salt),
                                  set()).add(str(r["vid"]))
            for token, members in groups.items():
                if len(members) < 2:
                    continue          # one supplier, one key: normal
                shared = sorted(members)
                links.append({
                    "kind": f"shared_{label.replace(' ', '_')}",
                    "token": token,
                    "suppliers": shared,
                    "involves_this_family": sorted(set(shared) & set(ids)),
                    "basis": f"{record}.{key_col}, normalised then compared "
                             "as a keyed hash. The value itself is never "
                             "returned, logged or displayed.",
                })
            if truncated:
                notes.append(
                    f"More than {LINK_SCAN_CAP} {label} rows were read; "
                    "suppliers beyond that were not compared.")

        bank_col = self._pick("PS_VNDR_BANK_ACCT",
                              ["BANK_ACCOUNT_NUM", "BANK_ACCT_NBR",
                               "EFT_ACCT_NBR", "ACCOUNT_NUM"])
        collide("PS_VNDR_BANK_ACCT", bank_col, "VENDOR_ID", "BANK",
                "bank account")
        tax_col = self._pick("PS_VENDOR", ["VNDR_TIN", "TIN", "TAX_ID"])
        collide("PS_VENDOR", tax_col, "VENDOR_ID", "TAXID", "tax id")

        mine = [x for x in links if x["involves_this_family"]]
        return {
            "supported": True,
            "checked": checked,
            "links": mine,
            "other_collisions": len(links) - len(mine),
            "note": " ".join(notes) if notes else "",
            "read_this_as": (
                "A shared key is a reason to LOOK, not a finding that two "
                "suppliers are one company. Only the recorded corporate "
                "hierarchy says that. The token is a keyed hash so two rows "
                "can be compared without the account number or taxpayer id "
                "ever being shown."),
        }

    def _duplicates(self, bu: str, ids: list, months: int, asof: str) -> dict:
        """Delegated, never reimplemented.

        get_duplicate_payments already owns this and its figures are the
        ones the guard has seen. Recomputing them here would let the same
        exposure be counted two different ways in one conversation.
        """
        try:
            out = self.mp.duplicate_payments(business_unit=bu, months=months,
                                             as_of_date=asof)
        except (ModuleError, DbError) as e:
            return {"supported": False,
                    "note": f"Duplicate detection could not run ({e})."}
        keep = set(ids)
        return {
            "supported": True,
            "from_tool": "get_duplicate_payments",
            "exact_invoice_duplicates": [
                d for d in out.get("exact_invoice_duplicates") or []
                if str(d.get("vendor_id")) in keep],
            "same_amount_pairs": [
                d for d in out.get("same_amount_pairs") or []
                if str(d.get("vendor_id")) in keep],
            "window_months": out.get("window_months"),
            "note": ("Duplicate VOUCHERS, not duplicate cash out — whether "
                     "one was actually paid twice is get_vendor_payments. "
                     "Figures come from get_duplicate_payments unchanged."),
        }

    def _locations(self, setid: str, ids: list) -> dict:
        if not self.db.columns("PS_VENDOR_ADDR"):
            return {}
        expr, binds = self._in("v", ids)
        try:
            rows, _ = self.db.query(
                f"SELECT VENDOR_ID AS vendor_id, CITY AS city, "
                f"STATE AS state, COUNTRY AS country "
                f"FROM {self.db.prefix}PS_VENDOR_ADDR "
                f"WHERE SETID = :setid AND VENDOR_ID IN {expr} "
                "ORDER BY ADDRESS_SEQ_NUM",
                {"setid": setid, **binds}, max_rows=len(ids) * 5)
        except DbError:
            return {}
        out: dict = {}
        for r in rows:
            out.setdefault(str(r["vendor_id"]), {
                "city": str(r.get("city") or ""),
                "state": str(r.get("state") or ""),
                "country": str(r.get("country") or "")})
        return out

    # ------------------------------------------------------------ the map
    def _graph(self, family: list, vouchers: dict, spend: dict,
               identity: dict) -> dict:
        """What connects to what, carrying no money.

        Same rule as the customer map: amounts live in the readable
        sections exactly once. Repeating them here fed the grounding guard
        an allowlist the readable sections had capped, and nothing consumed
        them.
        """
        nodes, edges, seen = [], [], set()

        def node(nid, kind, label):
            if nid not in seen and len(nodes) < NODE_CAP:
                seen.add(nid)
                nodes.append({"id": nid, "type": kind, "label": label})
            return nid

        def edge(src, dst, kind, **extra):
            if src in seen and dst in seen:
                edges.append({"from": src, "to": dst, "type": kind, **extra})

        for m in family:
            node(f"VEND:{m['vendor_id']}", "Supplier",
                 m["name"] or m["vendor_id"])
        for m in family:
            parent = m.get("corporate_parent")
            if parent and parent != m["vendor_id"]:
                edge(f"VEND:{m['vendor_id']}", f"VEND:{parent}",
                     "SUBSIDIARY_OF", basis="PS_VENDOR corporate hierarchy")
        for v in vouchers["vouchers"][:SHOWN_ROWS]:
            nid = node(f"VCHR:{v['voucher_id']}", "Payable", v["voucher_id"])
            edge(f"VEND:{v['vendor_id']}", nid, "OWES")
        for vid, row in (spend.get("by_vendor") or {}).items():
            nid = node(f"PAID:{vid}", "PaymentHistory",
                       f"{row['payments']} payments")
            edge(f"VEND:{vid}", nid, "PAID")
        for link in identity.get("links") or []:
            nid = node(f"KEY:{link['token']}", "SharedKey", link["token"])
            for vid in link["suppliers"]:
                node(f"VEND:{vid}", "Supplier", vid)
                edge(f"VEND:{vid}", nid, "SHARES_KEY",
                     shared=link["kind"])
        kinds = Counter(n["type"] for n in nodes)
        return {
            "nodes": nodes, "edges": edges,
            "mapped": dict(kinds),
            "truncated": (len(seen) >= NODE_CAP
                          or len(vouchers["vouchers"]) > SHOWN_ROWS),
            "basis": "Edges come from key columns in the records: the "
                     "recorded corporate hierarchy, the payment "
                     "cross-reference, and a keyed-hash equality on a bank "
                     "account or tax id. None is inferred from names.",
            "read_this_as": "A map, not a source of figures. Every amount is "
                            "in payables, spend or duplicates above. Note "
                            "that SHARES_KEY is an observation to check, NOT "
                            "a statement that two suppliers are one company.",
        }

    # ------------------------------------------------------ what to do now
    def _attention(self, family: list, payables: dict, vouchers: dict,
                   spend: dict, identity: dict, duplicates: dict,
                   asof: str) -> list:
        out: list = []
        multi = len(family) > 1
        for agg in payables["totals_by_currency"]:
            if agg["open_amount"]:
                contributors = []
                if multi:
                    for m in family:
                        for row in payables["by_vendor"].get(
                                m["vendor_id"], []):
                            if row["currency"] == agg["currency"]:
                                contributors.append({
                                    "vendor_id": m["vendor_id"],
                                    "name": m["name"],
                                    "open_amount": row["open_amount"]})
                    contributors.sort(key=lambda c: -c["open_amount"])
                out.append({
                    "kind": "open_payable",
                    "headline": f"{agg['open_amount']:,.2f} "
                                f"{agg['currency']} is open across "
                                f"{agg['vouchers']} voucher(s).",
                    "amount": agg["open_amount"],
                    "currency": agg["currency"],
                    "contributors": contributors,
                })
        overdue = [v for v in vouchers["vouchers"] if v["days_past_due"] > 0]
        if overdue:
            total = r2(sum(v["amount"] for v in overdue))
            worst = max(overdue, key=lambda v: v["days_past_due"])
            out.append({
                "kind": "overdue_payable",
                "headline": f"{total:,.2f} across {len(overdue)} voucher(s) "
                            f"is past due, the oldest by "
                            f"{worst['days_past_due']} days.",
                "amount": total, "oldest_days": worst["days_past_due"],
                "oldest_voucher": worst["voucher_id"],
            })
        stuck = [v for v in vouchers["vouchers"]
                 if v["entry_status"] == "R" or v["post_status"] == "U"]
        if stuck:
            total = r2(sum(v["amount"] for v in stuck))
            out.append({
                "kind": "stuck_in_pipeline",
                "headline": f"{total:,.2f} across {len(stuck)} voucher(s) is "
                            "in recycle or unposted — owed, and in a queue "
                            "nobody is watching.",
                "amount": total,
                "vouchers": [v["voucher_id"] for v in stuck],
            })
        for link in identity.get("links") or []:
            others = [v for v in link["suppliers"]
                      if v not in link["involves_this_family"]]
            what = link["kind"].replace("shared_", "").replace("_", " ")
            # Say what the specific collision means. A shared remit account
            # and a shared taxpayer id are different findings and lumping
            # them under one sentence is how a reader stops reading them.
            why = ("Two suppliers paid to one account is worth confirming "
                   "before the next payment run."
                   if "bank" in link["kind"] else
                   "One taxpayer id across two supplier records is usually "
                   "a duplicate vendor master.")
            out.append({
                "kind": link["kind"],
                "headline": f"This supplier shares a {what} with "
                            f"{', '.join(others) or 'another supplier'}. "
                            f"{why} It is not, by itself, evidence that they "
                            "are the same company — only the recorded "
                            "hierarchy says that.",
                "token": link["token"],
                "suppliers": link["suppliers"],
            })
        exact = duplicates.get("exact_invoice_duplicates") or []
        if exact:
            total = r2(sum(float(d.get("total") or 0) for d in exact))
            out.append({
                "kind": "duplicate_voucher",
                "headline": f"{len(exact)} invoice number(s) vouchered more "
                            f"than once, totalling {total:,.2f}.",
                "amount": total,
                "invoices": [d.get("invoice_id") for d in exact],
            })
        return out

    # ------------------------------------------------------------ the tool
    def vendor_payables_network(self, vendor_id: str = "",
                                business_unit: str = "",
                                include_family: bool = True,
                                months: int = 12,
                                as_of_date: str = "") -> dict:
        vid = (vendor_id or "").strip()
        if not vid:
            raise ModuleError(
                "get_vendor_payables_network needs a supplier ID. Use "
                "search_vendors to find one by name.")
        bu = self.mp._bu(business_unit)
        asof = self.mp._asof(as_of_date)
        setid = self.e.resolve_setid(bu, "VENDOR")
        window = max(int(months or 12), 1)
        since = (_iso(asof) - dt.timedelta(days=window * 31)).isoformat()
        notes: list = []

        family = self._family(setid, vid, include_family, notes)
        if not family:
            known, _ = self.db.query(
                f"SELECT VENDOR_ID AS v FROM {self.db.prefix}PS_VENDOR "
                "WHERE SETID = :setid ORDER BY VENDOR_ID",
                {"setid": setid}, max_rows=15)
            return {
                "scope_status": "vendor_not_found",
                "detail": f"Supplier {vid!r} does not exist in SETID "
                          f"{setid!r}. This is NO DATA, not a zero balance.",
                "business_unit": bu, "setid": setid,
                "known_vendor_ids": [str(r["v"]) for r in known],
            }
        ids = [m["vendor_id"] for m in family]

        def run(fn, *a):
            try:
                return fn(*a)
            except Exception as e:              # noqa: BLE001
                return {"error": f"{type(e).__name__}: {e}"}

        with ThreadPoolExecutor(max_workers=6) as pool:
            f_pay = pool.submit(run, self._payables, bu, ids, asof)
            f_vch = pool.submit(run, self._open_vouchers, bu, ids, asof)
            f_spd = pool.submit(run, self._spend, bu, ids, since)
            f_ident = pool.submit(run, self._identity, setid, ids)
            f_dup = pool.submit(run, self._duplicates, bu, ids, window, asof)
            f_loc = pool.submit(run, self._locations, setid, ids)
            payables, vouchers = f_pay.result(), f_vch.result()
            spend, identity = f_spd.result(), f_ident.result()
            duplicates, loc = f_dup.result(), f_loc.result()

        blanks = {
            "payables": {"by_vendor": {}, "totals_by_currency": [],
                         "notes": []},
            "vouchers": {"vouchers": [], "truncated": False},
            "spend": {"supported": False, "by_vendor": {}},
            "identity": {"supported": False, "links": []},
            "duplicates": {"supported": False},
            "locations": {},
        }
        named = {"payables": payables, "vouchers": vouchers, "spend": spend,
                 "identity": identity, "duplicates": duplicates,
                 "locations": loc}
        for key, value in list(named.items()):
            if isinstance(value, dict) and value.get("error"):
                notes.append(f"The {key} section could not be read: "
                             f"{value['error']}")
                named[key] = blanks[key]
        payables, vouchers = named["payables"], named["vouchers"]
        spend, identity = named["spend"], named["identity"]
        duplicates, loc = named["duplicates"], named["locations"]
        notes.extend(payables.get("notes") or [])
        for section in (spend, identity, duplicates):
            if section.get("note"):
                notes.append(section["note"])
        if vouchers.get("truncated"):
            notes.append(f"More than {VOUCHER_DETAIL_CAP} open vouchers — "
                         "the list below is the first page. The totals are "
                         "from the database and are complete.")

        for m in family:
            m["location"] = loc.get(m["vendor_id"], {})
            m["payables"] = payables["by_vendor"].get(m["vendor_id"], [])
            m["spend"] = (spend.get("by_vendor") or {}).get(m["vendor_id"])

        attention = self._attention(family, payables, vouchers, spend,
                                    identity, duplicates, asof)
        graph = self._graph(family, vouchers, spend, identity)
        anchor = family[0]
        return {
            "business_unit": bu, "setid": setid, "as_of_date": asof,
            "window_months": window, "window_start": since,
            "supplier": {k: anchor[k] for k in
                         ("vendor_id", "name", "status", "corporate_parent")}
            | {"location": anchor.get("location", {})},
            "family": {
                "members": family,
                "member_count": len(family),
                "basis": "The corporate supplier hierarchy recorded in the "
                         "financial system. Suppliers are never grouped by "
                         "name, by address, or by a shared bank account.",
                "included_in_totals": len(family) > 1,
            },
            "payables": {
                "totals_by_currency": payables["totals_by_currency"],
                "largest_open_vouchers": vouchers["vouchers"][:SHOWN_ROWS],
            },
            "spend": spend,
            "identity_links": identity,
            "duplicates": duplicates,
            "needs_attention": attention,
            "relationships": graph,
            "record_notes": notes,
            "basis": ("Read live from the financial system at the time of "
                      "this call. Bank account numbers and taxpayer ids are "
                      "never returned: they are compared inside the "
                      "database and reported only as a keyed hash. Aging is "
                      f"measured against {asof}."),
        }
