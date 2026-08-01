"""Read PeopleTools record metadata from one instance (9.1 or 9.2).

Everything here is SELECT-only against the PeopleTools tables. Subrecords are
expanded in Python rather than read from PSRECFIELDDB so the same code runs
against the real Oracle instances and the SQLite fixtures in tests, and so a
tools-release difference in the flattened table can never skew a compare.
"""
from __future__ import annotations

from ..db import Database
from .spec import FieldDef, MigrateError, RecordDef

# Expanding a subrecord that (pathologically) contains itself must terminate.
_MAX_SUBREC_DEPTH = 5

# Caps are generous and surfaced: metadata pulls must never silently truncate,
# or a missing field becomes a phantom shape difference.
_MAX_FIELDS = 5000
_MAX_DISCOVER = 20000


class RecordCatalog:
    def __init__(self, db: Database, custom_prefixes: list):
        self.db = db
        self.custom_prefixes = [p.upper() for p in custom_prefixes if p.strip()]
        self._cache: dict = {}

    @property
    def prefix(self) -> str:
        return self.db.prefix

    def is_custom(self, recname: str) -> bool:
        up = recname.upper()
        return any(up.startswith(p) for p in self.custom_prefixes)

    # ---- discovery -------------------------------------------------------
    def discover_custom(self, mode: str = "both", limit: int = 0) -> list:
        """Candidate custom records by naming standard and/or last-update
        operator. Both signals are heuristics: prefixes miss the record the
        one developer named badly in 2011, LASTUPDOPRID flags delivered
        records a site admin merely re-saved. The union is a REVIEW list."""
        mode = (mode or "both").lower()
        if mode not in ("prefix", "oprid", "both"):
            raise MigrateError(f"discovery mode must be prefix|oprid|both, got {mode!r}")
        clauses, params = [], {}
        if mode in ("prefix", "both") and self.custom_prefixes:
            likes = []
            for i, p in enumerate(self.custom_prefixes):
                likes.append(f"RECNAME LIKE :pfx{i}")
                params[f"pfx{i}"] = f"{p}%"
            clauses.append("(" + " OR ".join(likes) + ")")
        if mode in ("oprid", "both"):
            clauses.append("(LASTUPDOPRID IS NOT NULL AND LASTUPDOPRID <> 'PPLSOFT'"
                           " AND LASTUPDOPRID <> '')")
        if not clauses:
            raise MigrateError("No discovery signal: set migrate.custom_prefixes "
                               "or use discovery mode 'oprid'.")
        sql = (
            f"SELECT RECNAME, RECTYPE, SQLTABLENAME, LASTUPDOPRID, RECDESCR "
            f"FROM {self.prefix}PSRECDEFN WHERE " + " OR ".join(clauses) +
            " ORDER BY RECNAME"
        )
        cap = limit or _MAX_DISCOVER
        rows, truncated = self.db.query(sql, params, max_rows=cap)
        out = []
        for r in rows:
            out.append({
                "recname": str(r.get("recname") or "").upper(),
                "rectype": int(r.get("rectype") or 0),
                "sqltablename": str(r.get("sqltablename") or "").strip(),
                "lastupdoprid": str(r.get("lastupdoprid") or "").strip(),
                "descr": str(r.get("recdescr") or "").strip(),
                "matched_prefix": self.is_custom(str(r.get("recname") or "")),
            })
        if truncated:
            # Surfaced, not raised: the caller decides whether a capped
            # discovery is acceptable for a first look.
            out.append({"recname": "", "truncated": True, "cap": cap})
        return out

    # ---- one record ------------------------------------------------------
    def record_exists(self, recname: str) -> bool:
        rows, _ = self.db.query(
            f"SELECT RECNAME FROM {self.prefix}PSRECDEFN WHERE RECNAME = :r",
            {"r": recname.upper()}, max_rows=1)
        return bool(rows)

    def record(self, recname: str) -> RecordDef | None:
        """Full definition with subrecords expanded, or None if absent."""
        key = recname.upper()
        if key in self._cache:
            return self._cache[key]
        rows, _ = self.db.query(
            f"SELECT RECNAME, RECTYPE, SQLTABLENAME, RELLANGRECNAME, "
            f"AUDITRECNAME, LASTUPDOPRID, RECDESCR "
            f"FROM {self.prefix}PSRECDEFN WHERE RECNAME = :r",
            {"r": key}, max_rows=1)
        if not rows:
            return None
        h = rows[0]
        rec = RecordDef(
            recname=key,
            rectype=int(h.get("rectype") or 0),
            sqltablename=str(h.get("sqltablename") or "").strip(),
            rellang_recname=str(h.get("rellangrecname") or "").strip(),
            audit_recname=str(h.get("auditrecname") or "").strip(),
            lastupdoprid=str(h.get("lastupdoprid") or "").strip(),
            descr=str(h.get("recdescr") or "").strip(),
        )
        rec.fields, rec.subrecords = self._expand_fields(key, set(), 0)
        self._cache[key] = rec
        return rec

    def _direct_fields(self, recname: str) -> list:
        sql = (
            f"SELECT F.FIELDNAME, F.FIELDNUM, F.USEEDIT, F.EDITTABLE, "
            f"D.FIELDTYPE, D.LENGTH AS FLDLEN, D.DECIMALPOS "
            f"FROM {self.prefix}PSRECFIELD F "
            f"LEFT JOIN {self.prefix}PSDBFIELD D ON D.FIELDNAME = F.FIELDNAME "
            f"WHERE F.RECNAME = :r ORDER BY F.FIELDNUM"
        )
        rows, truncated = self.db.query(sql, {"r": recname}, max_rows=_MAX_FIELDS)
        if truncated:
            raise MigrateError(
                f"{recname} returned more than {_MAX_FIELDS} field rows — "
                "refusing to compare a truncated shape.")
        return rows

    def _is_subrecord(self, name: str) -> bool:
        rows, _ = self.db.query(
            f"SELECT RECTYPE FROM {self.prefix}PSRECDEFN "
            f"WHERE RECNAME = :r AND RECTYPE = 3",
            {"r": name}, max_rows=1)
        return bool(rows)

    def _expand_fields(self, recname: str, seen: set, depth: int,
                       origin: str = "") -> tuple:
        """PSRECFIELD holds direct fields; a row whose FIELDNAME is itself a
        RECTYPE=3 record is a subrecord reference and splices in that
        record's fields at its position — recursively, as App Designer does."""
        if depth > _MAX_SUBREC_DEPTH:
            raise MigrateError(
                f"Subrecord nesting deeper than {_MAX_SUBREC_DEPTH} at "
                f"{recname} — check for a subrecord cycle.")
        fields: list = []
        subrecs: list = []
        for r in self._direct_fields(recname):
            fname = str(r.get("fieldname") or "").upper()
            has_dbfield = r.get("fieldtype") is not None
            if not has_dbfield and fname not in seen and self._is_subrecord(fname):
                seen = seen | {fname}
                sub_fields, sub_subs = self._expand_fields(
                    fname, seen, depth + 1, origin or fname)
                subrecs.append(fname)
                subrecs.extend(s for s in sub_subs if s not in subrecs)
                fields.extend(sub_fields)
                continue
            fields.append(FieldDef(
                fieldname=fname,
                fieldnum=int(r.get("fieldnum") or 0),
                fieldtype=int(r["fieldtype"]) if has_dbfield else None,
                length=int(r.get("fldlen") or 0),
                decimalpos=int(r.get("decimalpos") or 0),
                useedit=int(r.get("useedit") or 0),
                edittable=str(r.get("edittable") or "").strip(),
                from_subrecord=origin,
            ))
        return fields, subrecs

    # ---- physical side ---------------------------------------------------
    def view_sql(self, recname: str) -> str:
        """View text from PSSQLTEXTDEFN, "" when unavailable. Read for
        dependency hints only, so an unreadable tools table degrades to
        'no hints', never to a failed plan."""
        try:
            rows, _ = self.db.query(
                f"SELECT SQLTEXT FROM {self.prefix}PSSQLTEXTDEFN "
                f"WHERE SQLID = :r ORDER BY SEQNUM",
                {"r": recname.upper()}, max_rows=100)
        except Exception:
            return ""
        return "\n".join(str(r.get("sqltext") or "") for r in rows).strip()

    def physical_columns(self, table_name: str) -> set:
        return self.db.columns(table_name)

    def table_row_count(self, table_name: str, where: str = "") -> int:
        sql = f"SELECT COUNT(*) AS N FROM {self.prefix}{table_name}"
        if where:
            sql += f" WHERE {where}"
        rows, _ = self.db.query(sql, {}, max_rows=1)
        return int(rows[0]["n"]) if rows else 0

    def column_sum(self, table_name: str, column: str, where: str = "") -> float:
        sql = f"SELECT SUM({column}) AS S FROM {self.prefix}{table_name}"
        if where:
            sql += f" WHERE {where}"
        rows, _ = self.db.query(sql, {}, max_rows=1)
        v = rows[0].get("s") if rows else None
        return float(v) if v is not None else 0.0

    def discover_delivered(self, like: str, limit: int = 0) -> list:
        """Delivered (non-custom) records matching a name pattern — how an
        operator names the delivered tables a reimplementation has to carry,
        e.g. like='LEDGER%' or like='%JRNL%'. Custom records are excluded so
        this never overlaps discover_custom()."""
        pattern = (like or "").strip().upper()
        if not pattern:
            raise MigrateError("discover_delivered needs a LIKE pattern, "
                               "e.g. 'JRNL%' or '%VOUCHER%'.")
        rows, truncated = self.db.query(
            f"SELECT RECNAME, RECTYPE, SQLTABLENAME, LASTUPDOPRID, RECDESCR "
            f"FROM {self.prefix}PSRECDEFN WHERE RECNAME LIKE :p "
            f"ORDER BY RECNAME",
            {"p": pattern}, max_rows=limit or _MAX_DISCOVER)
        out = []
        for r in rows:
            name = str(r.get("recname") or "").upper()
            if self.is_custom(name):
                continue
            out.append({
                "recname": name,
                "rectype": int(r.get("rectype") or 0),
                "sqltablename": str(r.get("sqltablename") or "").strip(),
                "descr": str(r.get("recdescr") or "").strip(),
            })
        if truncated:
            out.append({"recname": "", "truncated": True})
        return out
