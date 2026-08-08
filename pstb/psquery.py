"""PeopleSoft query and Integration Broker DISCOVERY, over plain SQL.

Two facts make this cheap and safe, and they are the whole design:

* The query catalog (PSQRYDEFN / PSQRYBIND / PSQRYRECORD) and the IB
  catalog (PSIBSVCSETUP / PSSERVICE / PSOPERATION) are ORDINARY TABLES.
  Finding what a site already built needs no gateway, no credentials and
  no network — the read-only account we already hold is enough.
* Only EXECUTION needs the Query Access Service, because PSQuery
  assembles its SQL at runtime and never stores it whole. That is also
  where the security benefit lives: QAS runs as a named PeopleSoft user,
  so results respect permission lists that a direct database account
  bypasses entirely.

Reusing an existing query is the strongest provenance this product can
offer — "the same query Finance has run for years" beats "SQL we wrote"
in any audit conversation. So discovery exists to find that query, read
its prompts, and hand the model something better than a blank page.

DISCOVERY IS NOT INVOCATION. Integration Broker carries write-capable
operations (voucher build, journal load). This module reports everything
it finds and classifies each operation, so a later execution feature can
allowlist the read-only ones. Seeing an operation must never imply
permission to call it.
"""
from __future__ import annotations

import re

from .db import DbError

# Field-type codes used by PSQRYBIND prompts. Enough to tell a caller
# what to supply; anything unrecognised is reported as its raw code
# rather than guessed at.
_FIELD_TYPES = {0: "character", 1: "number", 2: "signed number",
                3: "date", 4: "date", 5: "time", 6: "datetime"}

# Operations safe to CALL later: they read. Everything else is reported
# by discovery and refused by invocation. The list is deliberately an
# allowlist of exact names, not a pattern — "anything containing GET"
# would admit a service called GET_APPROVAL_AND_POST.
READ_ONLY_OPERATIONS = {
    "QAS_LISTQUERIES", "QAS_GETQUERYPROPERTIES",
    "QAS_EXECUTEQUERY", "QAS_EXECUTENONBLOCKING",
}

_WRITE_HINT = re.compile(
    r"(?i)\b(load|build|post|create|update|delete|insert|submit|approve|"
    r"cancel|sync|publish)\b")


class QueryDiscoveryError(RuntimeError):
    pass


class QueryCatalog:
    """Read-only discovery over the PeopleTools query and IB catalogs."""

    def __init__(self, engine):
        self.e = engine
        self.db = engine.db

    def _present(self, table: str) -> bool:
        try:
            return bool(self.db.columns(table))
        except DbError:
            return False

    def search_queries(self, text: str = "", record: str = "",
                       include_private: bool = False,
                       limit: int = 25) -> dict:
        """Existing PSQueries matching a description, name, or record.

        record filters to queries that READ a given record ("show me the
        queries that already touch PS_VOUCHER"), which is how a model
        finds prior art before writing SQL of its own.
        """
        if not self._present("PSQRYDEFN"):
            return {"available": False, "queries": [],
                    "detail": ("PSQRYDEFN is not readable by this account. "
                               "Query discovery needs SELECT on the "
                               "PeopleTools query catalog (PSQRYDEFN, "
                               "PSQRYBIND, PSQRYRECORD) — ask your DBA for "
                               "those grants; no gateway or API is "
                               "involved.")}
        p = self.db.prefix
        cols = self.db.columns("PSQRYDEFN")
        runcnt = "QRYRUNCNT" in cols
        where = ["1=1"]
        params: dict = {}
        if not include_private:
            # OPRID is blank on public queries and set on private ones.
            where.append("(Q.OPRID IS NULL OR TRIM(Q.OPRID) = '')")
        needle = (text or "").strip()
        if needle:
            where.append("(UPPER(Q.QRYNAME) LIKE :q "
                         "OR UPPER(Q.DESCR) LIKE :q)")
            params["q"] = f"%{needle.upper()}%"
        rec = (record or "").strip().upper()
        if rec:
            if not self._present("PSQRYRECORD"):
                return {"available": False, "queries": [],
                        "detail": ("PSQRYRECORD is not readable, so queries "
                                   "cannot be filtered by record.")}
            # Sites write the record with or without its PS_ prefix.
            bare = rec[3:] if rec.startswith("PS_") else rec
            where.append(
                f"EXISTS (SELECT 1 FROM {p}PSQRYRECORD R "
                "WHERE R.QRYNAME = Q.QRYNAME AND R.OPRID = Q.OPRID "
                "AND UPPER(R.RECNAME) IN (:r1, :r2))")
            params.update({"r1": bare, "r2": rec})
        order = "Q.QRYRUNCNT DESC" if runcnt else "Q.QRYNAME"
        rows, truncated = self.db.query(
            f"SELECT Q.QRYNAME AS name, Q.OPRID AS owner, Q.DESCR AS descr, "
            + (f"Q.QRYRUNCNT AS runs, " if runcnt else "0 AS runs, ")
            + "Q.LASTUPDDTTM AS updated, Q.LASTUPDOPRID AS updated_by "
            f"FROM {p}PSQRYDEFN Q WHERE {' AND '.join(where)} "
            f"ORDER BY {order}",
            params, max_rows=max(int(limit or 25), 1))
        out = []
        for r in rows:
            owner = str(r.get("owner") or "").strip()
            out.append({
                "query": str(r["name"]),
                "descr": str(r.get("descr") or ""),
                "visibility": "private" if owner else "public",
                "owner": owner or None,
                "run_count": int(r.get("runs") or 0),
                "last_updated": str(r.get("updated") or "")[:10] or None,
                "last_updated_by": str(r.get("updated_by") or "") or None,
            })
        return {
            "available": True, "queries": out, "count": len(out),
            "truncated": truncated,
            "searched": {"text": needle, "record": rec or None,
                         "include_private": bool(include_private)},
            "note": ("Existing queries encode logic someone already built "
                     "and validated. Prefer reusing one over writing new "
                     "SQL — cite the query name when you do. Run counts "
                     "show what the business actually uses."
                     + ("" if include_private else
                        " Private queries are excluded; pass "
                        "include_private=true to see them, remembering "
                        "another user's private query may not be yours "
                        "to run.")),
        }

    def query_detail(self, query: str, owner: str = "") -> dict:
        """One query's prompts and the records it reads."""
        name = (query or "").strip().upper()
        if not name:
            raise QueryDiscoveryError("query name is required")
        if not self._present("PSQRYDEFN"):
            return {"available": False, "query": name,
                    "detail": "PSQRYDEFN is not readable by this account."}
        p = self.db.prefix
        params = {"n": name, "o": (owner or "").strip()}
        rows, _ = self.db.query(
            f"SELECT QRYNAME AS name, OPRID AS owner, DESCR AS descr "
            f"FROM {p}PSQRYDEFN WHERE UPPER(QRYNAME) = :n "
            f"AND (TRIM(COALESCE(OPRID,'')) = :o OR :o = '')",
            params, max_rows=5)
        if not rows:
            return {"available": True, "found": False, "query": name,
                    "detail": (f"No query named {name!r} is visible to this "
                               "account. Search with search_ps_queries "
                               "first — names are exact and case matters "
                               "at some sites.")}
        head = rows[0]
        prompts = []
        if self._present("PSQRYBIND"):
            binds, _ = self.db.query(
                f"SELECT BNDNUM AS n, BNDNAME AS bind, BNDDESCR AS descr, "
                f"FIELDTYPE AS ftype FROM {p}PSQRYBIND "
                f"WHERE UPPER(QRYNAME) = :n ORDER BY BNDNUM",
                {"n": name}, max_rows=50)
            for b in binds:
                code = b.get("ftype")
                prompts.append({
                    "position": int(b.get("n") or 0),
                    "bind": str(b.get("bind") or ""),
                    "prompt": str(b.get("descr") or ""),
                    "type": _FIELD_TYPES.get(
                        int(code) if code is not None else -1,
                        f"code {code}"),
                })
        records = []
        if self._present("PSQRYRECORD"):
            recs, _ = self.db.query(
                f"SELECT RECNAME AS rec, CORRNAME AS alias "
                f"FROM {p}PSQRYRECORD WHERE UPPER(QRYNAME) = :n "
                f"ORDER BY SELNUM", {"n": name}, max_rows=50)
            records = [{"record": str(r.get("rec") or ""),
                        "alias": str(r.get("alias") or "") or None}
                       for r in recs]
        owner_val = str(head.get("owner") or "").strip()
        return {
            "available": True, "found": True,
            "query": str(head["name"]),
            "descr": str(head.get("descr") or ""),
            "visibility": "private" if owner_val else "public",
            "owner": owner_val or None,
            "prompts": prompts, "records": records,
            "note": ("Prompts are the values this query expects at run "
                     "time, in order. Running it needs the Query Access "
                     "Service; this catalog read cannot execute anything. "
                     "The records listed are what the query reads — useful "
                     "for judging whether it answers your question before "
                     "anyone runs it."),
        }

    def integration_endpoints(self) -> dict:
        """The site's published IB target location and its operations.

        Reported so a site's gateway URL never has to live in our config.
        Every operation is classified, and only the read-only allowlist is
        ever callable — IB carries voucher builds and journal loads, and
        discovering one must not imply permission to invoke it.
        """
        p = self.db.prefix
        target = None
        # The column name for the gateway URL is NOT stable across tools
        # releases, and PSIBSVCSETUP existing does not mean it carries the
        # one we want. Selecting it blind raised ORA-00904 on a real
        # instance. Ask the catalog which of the known spellings is there
        # and skip the lookup entirely when none is — this is DISCOVERY,
        # and a site without it is a site that configures the URL by hand,
        # not an error.
        for column in ("TARGETLOCATION", "TARGET_LOCATION", "URL",
                       "IB_TARGETLOCATION"):
            if not self.db.has_column("PSIBSVCSETUP", column):
                continue
            try:
                rows, _ = self.db.query(
                    f"SELECT {column} AS loc FROM {p}PSIBSVCSETUP",
                    {}, max_rows=5)
            except DbError:
                continue
            target = next((str(r["loc"]) for r in rows
                           if str(r.get("loc") or "").strip()), None)
            if target:
                break
        operations = []
        if self._present("PSOPERATION") and all(
                self.db.has_column("PSOPERATION", c) for c in
                ("IB_OPERATIONNAME", "SERVICE", "DESCR", "OPERTYPE",
                 "IB_OPERSTATUS")):
            rows, _ = self.db.query(
                f"SELECT IB_OPERATIONNAME AS op, SERVICE AS service, "
                f"DESCR AS descr, OPERTYPE AS optype, "
                f"IB_OPERSTATUS AS status FROM {p}PSOPERATION "
                f"ORDER BY SERVICE, IB_OPERATIONNAME", {}, max_rows=500)
            for r in rows:
                op = str(r.get("op") or "")
                descr = str(r.get("descr") or "")
                callable_ = op.upper() in READ_ONLY_OPERATIONS
                operations.append({
                    "operation": op,
                    "service": str(r.get("service") or ""),
                    "descr": descr,
                    "type": str(r.get("optype") or ""),
                    "active": str(r.get("status") or "") == "A",
                    "callable": callable_,
                    "why": ("read-only, on the invocation allowlist"
                            if callable_ else
                            ("looks write-capable — discovery only"
                             if _WRITE_HINT.search(f"{op} {descr}")
                             else "not on the read-only allowlist — "
                                  "discovery only")),
                })
        qas = [o for o in operations if o["service"].upper() == "QAS"]
        return {
            "target_location": target,
            "target_source": ("PSIBSVCSETUP — the site's own published "
                              "location, not configured here"),
            "operations": operations,
            "operation_count": len(operations),
            "callable_count": sum(1 for o in operations if o["callable"]),
            "query_service_available": bool(qas),
            "note": (
                "DISCOVERY IS NOT INVOCATION. Integration Broker carries "
                "write-capable operations; every one found is listed so "
                "you can see what exists, and only the read-only query "
                "operations are on the invocation allowlist. Executing a "
                "query additionally needs a PeopleSoft user, and its "
                "results respect that user's permission lists — which may "
                "legitimately differ from what this database account sees."
            ),
        }
