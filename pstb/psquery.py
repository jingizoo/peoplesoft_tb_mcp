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
        # Run counts do NOT live on PSQRYDEFN — there is no QRYRUNCNT in any
        # documented release. They live in PSQRYSTATS (EXECCOUNT,
        # LASTEXECDTTM, keys OPRID+QRYNAME), populated only when a query has
        # statistics logging enabled, so COALESCE to 0 rather than dropping
        # rows. Verified against the PT 8.58 data dictionary
        # (go-faster.co.uk/peopletools/psqrydefn.htm, psqrystats.htm).
        stats = (self._present("PSQRYSTATS")
                 and self.db.has_column("PSQRYSTATS", "EXECCOUNT"))
        where = ["1=1"]
        params: dict = {}
        if not include_private:
            # OPRID is blank on public queries and set on private ones.
            # Oracle stores an empty string as NULL, so equality to '' is
            # never true there. TRIM(... ) IS NULL is portable to Oracle,
            # SQL Server and SQLite and excludes private owners.
            where.append("(TRIM(Q.OPRID) IS NULL OR TRIM(Q.OPRID) = '')")
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
                "WHERE R.QRYNAME = Q.QRYNAME AND "
                "(R.OPRID = Q.OPRID OR (((TRIM(R.OPRID) IS NULL OR "
                "TRIM(R.OPRID)='') AND (TRIM(Q.OPRID) IS NULL OR "
                "TRIM(Q.OPRID)='')))) "
                "AND UPPER(R.RECNAME) IN (:r1, :r2))")
            params.update({"r1": bare, "r2": rec})
        join = (f"LEFT JOIN {p}PSQRYSTATS S ON S.QRYNAME = Q.QRYNAME "
                "AND (S.OPRID = Q.OPRID OR (((TRIM(S.OPRID) IS NULL OR "
                "TRIM(S.OPRID)='') AND (TRIM(Q.OPRID) IS NULL OR "
                "TRIM(Q.OPRID)='')))) "
                if stats else "")
        rows, truncated = self.db.query(
            f"SELECT Q.QRYNAME AS name, Q.OPRID AS owner, Q.DESCR AS descr, "
            + ("COALESCE(S.EXECCOUNT, 0) AS runs, " if stats
               else "0 AS runs, ")
            + "Q.LASTUPDDTTM AS updated, Q.LASTUPDOPRID AS updated_by "
            f"FROM {p}PSQRYDEFN Q {join}WHERE {' AND '.join(where)} "
            f"ORDER BY {'runs DESC, Q.QRYNAME' if stats else 'Q.QRYNAME'}",
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
        owner_filter = (owner or "").strip() or None
        params = {"n": name, "o": owner_filter}
        rows, _ = self.db.query(
            f"SELECT QRYNAME AS name, OPRID AS owner, DESCR AS descr "
            f"FROM {p}PSQRYDEFN WHERE UPPER(QRYNAME) = :n "
            f"AND ((:o IS NULL AND (TRIM(OPRID) IS NULL OR "
            f"TRIM(OPRID)='')) "
            f"OR TRIM(OPRID) = :o)",
            params, max_rows=5)
        if not rows:
            return {"available": True, "found": False, "query": name,
                    "detail": (f"No query named {name!r} is visible to this "
                               "account. Search with search_ps_queries "
                               "first — names are exact and case matters "
                               "at some sites.")}
        head = rows[0]
        head_owner = str(head.get("owner") or "").strip()
        prompts = []
        if self._present("PSQRYBIND"):
            # The prompt's label column is HEADING (no BNDDESCR exists in
            # any documented release); '' when a site's shape lacks it.
            # Scope to the chosen head's OPRID: a private query sharing a
            # public one's name must not merge its binds in.
            label = ("HEADING AS descr"
                     if self.db.has_column("PSQRYBIND", "HEADING")
                     else "'' AS descr")
            binds, _ = self.db.query(
                f"SELECT BNDNUM AS n, BNDNAME AS bind, {label}, "
                f"FIELDTYPE AS ftype FROM {p}PSQRYBIND "
                f"WHERE UPPER(QRYNAME) = :n "
                f"AND ((:own IS NULL AND (TRIM(OPRID) IS NULL OR "
                f"TRIM(OPRID)='')) "
                f"OR TRIM(OPRID) = :own) ORDER BY BNDNUM",
                {"n": name, "own": head_owner or None}, max_rows=50)
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
                f"AND ((:own IS NULL AND (TRIM(OPRID) IS NULL OR "
                f"TRIM(OPRID)='')) "
                f"OR TRIM(OPRID) = :own) "
                f"ORDER BY SELNUM", {"n": name, "own": head_owner or None},
                max_rows=50)
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
        # PSIBSVCSETUP's target-location columns, verified against the
        # PT 8.58 data dictionary (go-faster.co.uk/peopletools/
        # psibsvcsetup.htm; PT 8.52 PeopleBook tiba08 confirms the same
        # names): IB_TGTLOCATION and IB_SECTGTLOCATION for the SOAP
        # listening connector, IB_RESTTGTLOC and IB_RESTSECTGTLOC for the
        # REST one. Selecting a guessed name blind raised ORA-00904 on a
        # real instance, so each is probed against the catalog first, and
        # REST is preferred: it is already the base QAS needs, with no
        # SOAP-to-REST derivation. A site where none carries a value
        # configures ps_api.target_location by hand — that is a supported
        # posture, not an error.
        target_column = None
        for column in ("IB_RESTSECTGTLOC", "IB_RESTTGTLOC",
                       "IB_SECTGTLOCATION", "IB_TGTLOCATION"):
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
                target_column = column
                break
        operations = []
        # PSOPERATION carries IB_OPERATIONNAME and DESCR (PT 8.58
        # dictionary). It does NOT carry SERVICE, OPERTYPE or
        # IB_OPERSTATUS — the service link is the PSSERVICEOPR bridge
        # (IB_SERVICENAME + IB_OPERATIONNAME), and active/inactive is
        # per-version in PSOPRVERDFN. Type and status are therefore not
        # reported at all: a field this discovery cannot source honestly
        # is omitted, not guessed.
        if self._present("PSOPERATION") and all(
                self.db.has_column("PSOPERATION", c) for c in
                ("IB_OPERATIONNAME", "DESCR")):
            svc_link = (self._present("PSSERVICEOPR") and all(
                self.db.has_column("PSSERVICEOPR", c) for c in
                ("IB_SERVICENAME", "IB_OPERATIONNAME")))
            if svc_link:
                sql = (f"SELECT O.IB_OPERATIONNAME AS op, "
                       f"SO.IB_SERVICENAME AS service, O.DESCR AS descr "
                       f"FROM {p}PSOPERATION O "
                       f"LEFT JOIN {p}PSSERVICEOPR SO "
                       f"ON SO.IB_OPERATIONNAME = O.IB_OPERATIONNAME "
                       f"ORDER BY SO.IB_SERVICENAME, O.IB_OPERATIONNAME")
            else:
                sql = (f"SELECT IB_OPERATIONNAME AS op, '' AS service, "
                       f"DESCR AS descr FROM {p}PSOPERATION "
                       f"ORDER BY IB_OPERATIONNAME")
            rows, _ = self.db.query(sql, {}, max_rows=500)
            for r in rows:
                op = str(r.get("op") or "")
                descr = str(r.get("descr") or "")
                callable_ = op.upper() in READ_ONLY_OPERATIONS
                operations.append({
                    "operation": op,
                    "service": str(r.get("service") or ""),
                    "descr": descr,
                    "callable": callable_,
                    "why": ("read-only, on the invocation allowlist"
                            if callable_ else
                            ("looks write-capable — discovery only"
                             if _WRITE_HINT.search(f"{op} {descr}")
                             else "not on the read-only allowlist — "
                                  "discovery only")),
                })
        qas = [o for o in operations
               if o["service"].upper() == "QAS"
               or o["operation"].upper().startswith("QAS_")]
        # Oracle raises the SAME error for a missing table and a missing
        # SELECT grant (ORA-00942), and PSOPERATION is PeopleTools metadata
        # a read-only reporting account often cannot see. An unreadable
        # catalog therefore must not be reported as "QAS is not available"
        # — that answer steers the model away from run_ps_query on exactly
        # the sites where it would work.
        catalog_readable = bool(operations)
        return {
            "target_location": target,
            "target_source": (
                f"PSIBSVCSETUP.{target_column} — the site's own published "
                "location, not configured here" if target_column
                else "not discovered — set ps_api.target_location"),
            "operations": operations,
            "operation_count": len(operations),
            "callable_count": sum(1 for o in operations if o["callable"]),
            "query_service_available": (bool(qas) if catalog_readable
                                        else None),
            "catalog_readable": catalog_readable,
            "note": (
                ("" if catalog_readable else
                 "The IB operation catalog was not readable — on Oracle a "
                 "missing table and a missing SELECT grant raise the same "
                 "ORA-00942, so this says nothing about whether QAS is "
                 "configured. run_ps_query may still work; to enable "
                 "discovery ask the DBA for SELECT on PSOPERATION and "
                 "PSSERVICEOPR, or set ps_api.target_location by hand. ")
                + "DISCOVERY IS NOT INVOCATION. Integration Broker carries "
                "write-capable operations; every one found is listed so "
                "you can see what exists, and only the read-only query "
                "operations are on the invocation allowlist. Executing a "
                "query additionally needs a PeopleSoft user, and its "
                "results respect that user's permission lists — which may "
                "legitimately differ from what this database account sees."
            ),
        }
