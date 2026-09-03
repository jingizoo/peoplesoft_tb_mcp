"""Record profiling: enough evidence to CHOOSE a table, not just name one.

search_records finds candidates by name and description. That is often not
enough to pick between them, because PeopleSoft ships many records with
near-identical names and a site adds more: PS_VOUCHER against PS_VCHR_ACCTG_LINE
against a custom PS_XX_VCHR_STG, or a live record beside its history shell and
its staging table. Names cannot separate those. What separates them is what is
actually IN them — which columns exist, which are populated, what the status
flags really contain, and what a row looks like.

So this module answers "which of these records fits my question" with evidence:

  shape        every column, its type, and whether it is null in practice
  fill         how many sampled rows have a value — an all-null column is a
               column this site does not use, whatever the catalog says
  values       for low-cardinality columns, the actual codes and their counts.
               This is the highest-value signal in the file: knowing
               ITEM_STATUS holds 'O' and 'C' turns a guess into a predicate
  rows         a few real rows, masked

MASKING IS NOT OPTIONAL. Sample rows leave this process and reach whichever
model is configured, and on the Gemini path that means they leave the company's
network. Customer names, addresses, tax IDs, bank details and email addresses
are therefore masked to a shape-preserving placeholder before they go anywhere:
the model learns the column holds a 14-character name, not whose name it is.
Identifiers, codes, dates, amounts and flags pass through, because those are
the analytic signal and a masked amount would defeat the purpose.

Sites that cannot send any real data at all set tools.sample_rows = 0, which
drops the row sample and keeps everything else.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from .db import Database, DbError

# Column-name families whose values are personal or otherwise sensitive.
# Matched against the whole name and its underscore-separated parts, so
# BILL_TO_EMAIL_ADDR matches on EMAIL and VENDOR_NAME1 on NAME.
_SENSITIVE = (
    "NAME", "NAME1", "NAME2", "FIRST", "LAST", "MIDDLE", "CONTACT",
    "ADDRESS", "ADDR", "ADDR1", "ADDR2", "ADDR3", "ADDR4", "CITY", "POSTAL",
    "ZIP", "COUNTY", "PHONE", "FAX", "EMAIL", "EMAILID", "URL",
    "SSN", "NID", "NATIONAL", "TAXPAYER", "TIN", "VAT", "TAX_ID",
    "BIRTH", "BIRTHDATE", "DOB", "GENDER", "ETHNIC", "MARITAL",
    "IBAN", "SWIFT", "BANK", "ACCOUNT_NUM", "ACCOUNT_NO", "BANK_ACCT",
    "CARD", "CREDIT_CARD", "CCNBR", "PASSWORD", "PASSWD", "SECRET", "TOKEN",
    "SALARY", "COMPRATE", "ANNUAL_RT", "HOURLY_RT",
)
# Never masked even if a family above matches part of the name: these are
# structural identifiers and codes the model must see to build a predicate.
_NEVER_MASK = (
    "BUSINESS_UNIT", "SETID", "LEDGER", "ACCOUNT", "DEPTID", "PRODUCT",
    "PROJECT_ID", "CUST_ID", "VENDOR_ID", "ITEM", "ITEM_STATUS", "JOURNAL_ID",
    "CURRENCY_CD", "BASE_CURRENCY", "FISCAL_YEAR", "ACCOUNTING_PERIOD",
    "RECNAME", "FIELDNAME", "TREE_NAME", "BANK_CD", "BANK_ACCT_KEY",
)
# Customizations rarely keep the delivered long suffixes.  X_APPR_STAT,
# HOLD_STS, ERROR_FLG and ACTIVE_IND are exactly the low-cardinality columns
# whose values distinguish a live transaction record from a staging shell.
# Treat them like STATUS/FLAG for profiling; the distinct-value cap below is
# still the noise and data-volume guard.
_ID_LIKE = re.compile(
    r"(_ID|_CD|_KEY|_NBR|_NO|_NUM|_STAT|_STS|_FLG|_IND|"
    r"ID|CODE|STATUS|FLAG|TYPE)$")

# A column with more distinct values than this is not a status/type flag, so
# listing its values would be noise rather than a fit signal.
_MAX_DISTINCT_FOR_VALUES = 12


def is_sensitive(column: str) -> bool:
    """Whether values of this column must be masked before leaving the process.

    Trailing digits are stripped from each name part before matching, because
    PeopleSoft numbers its repeating columns: ADDRESS1..ADDRESS4, NAME1, NAME2,
    PHONE1. Matching the bare families only would have masked NAME1 (it is
    listed) while letting ADDRESS1 through — which is how a masking rule ends
    up protecting the example everyone tested and nothing else.
    """
    col = (column or "").upper()
    if col in _NEVER_MASK:
        return False
    stem = re.sub(r"\d+$", "", col)
    parts = {re.sub(r"\d+$", "", part) for part in col.split("_")}
    parts |= {col, stem}
    parts.discard("")
    if parts & set(_NEVER_MASK) and col in _NEVER_MASK:
        return False
    return any(fam in parts for fam in _SENSITIVE) or any(
        stem.startswith(fam + "_") or stem.endswith("_" + fam)
        for fam in _SENSITIVE)


def mask(value, column: str = "") -> object:
    """Replace a sensitive value with a shape-preserving placeholder.

    Shape is preserved because it carries real information for table
    selection — a 14-character name column and a 3-character one are
    different findings — while the value itself is not disclosed.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "###"
    text = str(value)
    if not text.strip():
        return text
    return f"{text[0]}{'*' * max(len(text.strip()) - 1, 1)} ({len(text.strip())} chars)"


class RecordProfiler:
    """Evidence about one record, gathered with bounded cost."""

    def __init__(self, db: Database, cfg) -> None:
        self.db = db
        self.cfg = cfg

    # ---- helpers ---------------------------------------------------------
    def _sample_limit(self) -> int:
        n = int(getattr(self.cfg.tools, "sample_rows", 3) or 0)
        return max(0, min(n, 10))

    def _rows(self, sql: str, params: dict, cap: int) -> list:
        try:
            rows, _ = self.db.query(sql, params, max_rows=cap)
            return rows
        except DbError:
            return []

    def _limited(self, table: str, cap: int) -> str:
        """A capped SELECT * that never scans the whole record."""
        p = self.db.prefix
        if self.db.dialect == "oracle":
            return f"SELECT * FROM {p}{table} WHERE ROWNUM <= {cap}"
        if self.db.dialect == "sqlserver":
            return f"SELECT TOP {cap} * FROM {p}{table}"
        return f"SELECT * FROM {p}{table} LIMIT {cap}"

    # ---- profiling -------------------------------------------------------
    def profile(self, table: str, sample_rows: Optional[int] = None) -> dict:
        """Shape, fill, value distributions and masked sample rows."""
        if self.db.dialect == "bigquery":
            # SELECT * bills the FULL table bytes however few rows come
            # back (LIMIT bounds rows, not bytes), and the uppercased
            # name would miss anyway on a case-sensitive backend.
            raise DbError(
                "profile_record is not available on a BigQuery source: "
                "a sampled SELECT * bills the whole table's bytes on "
                "this backend. Use describe_table for the shape.")
        tbl = (table or "").strip().upper()
        if not tbl:
            raise ValueError("profile_record needs a table or record name")
        if not re.fullmatch(r"[A-Z0-9_$#.]+", tbl):
            raise ValueError(f"{table!r} is not a valid record name")

        cols = sorted(self.db.columns(tbl))
        out: dict = {"table": tbl, "columns": cols, "column_count": len(cols)}
        if not cols:
            out["readable"] = False
            out["note"] = (
                f"{tbl} is not readable here — it may not exist on this "
                "instance, or this account may lack SELECT on it. Use "
                "search_records to find the record this site actually uses."
            )
            return out
        out["readable"] = True

        want = self._sample_limit() if sample_rows is None else max(
            0, min(int(sample_rows), 10))
        # One pass fuels fill, values and the sample. Profile on a wider slice
        # than is shown, so "always null" means across 50 rows, not across 3.
        probe = self._rows(self._limited(tbl, 50), {}, 50)
        out["sampled_rows"] = len(probe)
        if not probe:
            out["populated"] = False
            out["note"] = (f"{tbl} is readable but EMPTY here. A record with no "
                           "rows is rarely the one a question means — check "
                           "for a sibling record that carries the data.")
            return out
        out["populated"] = True

        fill: dict = {}
        distinct: dict = {}
        for c in cols:
            vals = [r.get(c.lower()) for r in probe]
            present = [v for v in vals if v is not None and str(v).strip() != ""]
            fill[c] = round(100.0 * len(present) / len(vals))
            if present and _ID_LIKE.search(c) and not is_sensitive(c):
                uniq = {str(v) for v in present}
                if len(uniq) <= _MAX_DISTINCT_FOR_VALUES:
                    counts: dict = {}
                    for v in present:
                        counts[str(v)] = counts.get(str(v), 0) + 1
                    distinct[c] = dict(sorted(counts.items(),
                                              key=lambda kv: -kv[1]))
        out["fill_percent"] = fill
        out["always_null"] = [c for c, pct in fill.items() if pct == 0]
        if distinct:
            out["value_counts"] = distinct
        masked_cols = [c for c in cols if is_sensitive(c)]
        if masked_cols:
            out["masked_columns"] = masked_cols

        if want:
            out["sample"] = [
                {c.lower(): (mask(r.get(c.lower()), c) if is_sensitive(c)
                             else _plain(r.get(c.lower())))
                 for c in cols}
                for r in probe[:want]
            ]
        out["note"] = (
            "fill_percent and value_counts are measured over the first "
            f"{len(probe)} rows read, not the whole record — treat them as "
            "evidence about shape, never as totals. "
            + ("Values in masked_columns are replaced with a shape-preserving "
               "placeholder before leaving the database. "
               if masked_cols else "")
            + "always_null names columns this site does not populate, whatever "
              "the catalog says."
        )
        return out

    def compare(self, tables: list, sample_rows: Optional[int] = None) -> dict:
        """Profile several candidates side by side so one can be chosen."""
        names = [str(t).strip().upper() for t in (tables or []) if str(t).strip()]
        if not names:
            raise ValueError("compare_records needs at least one table")
        profiles = [self.profile(t, sample_rows=sample_rows) for t in names[:6]]
        usable = [p for p in profiles if p.get("populated")]
        ranked = sorted(
            usable,
            key=lambda p: (-len([c for c, v in p.get("fill_percent", {}).items()
                                 if v > 0]), p["table"]))
        return {
            "candidates": profiles,
            "readable_and_populated": [p["table"] for p in ranked],
            "empty_or_unreadable": [p["table"] for p in profiles
                                    if not p.get("populated")],
            "note": (
                "Choose from readable_and_populated. An empty or unreadable "
                "record is rarely what a question means, even when its name "
                "matches best. Confirm the choice against value_counts — a "
                "status column holding the codes the question implies is "
                "stronger evidence than any table name."
            ),
        }


def _plain(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v
