"""Database access layer: SQLite (sample), Oracle (python-oracledb thin), SQL Server (pyodbc).

All queries use :name bind parameters; the SQL Server path converts them to qmark
style. Result rows come back as dicts with lowercase keys, dates as ISO strings.
"""
from __future__ import annotations

import contextlib
import re
import threading
from typing import Any, Optional

from .config import Config


class DbError(RuntimeError):
    pass


_BIND_RE = re.compile(r"(?<![:\w]):(\w+)")
_CATALOG_IDENTIFIER = (
    r'(?:[A-Za-z][A-Za-z0-9_$#]*|"[A-Za-z][A-Za-z0-9_$#]*"|'
    r'\[[A-Za-z][A-Za-z0-9_$#]*\])'
)
_CATALOG_OBJECT_RE = re.compile(
    rf"^\s*({_CATALOG_IDENTIFIER})"
    rf"(?:\s*\.\s*({_CATALOG_IDENTIFIER}))?\s*$"
)


def _catalog_identifier_value(token: str) -> str:
    text = str(token or "").strip()
    if ((text.startswith('"') and text.endswith('"'))
            or (text.startswith("[") and text.endswith("]"))):
        return text[1:-1]
    return text.upper()


def _to_qmark(sql: str, params: dict) -> tuple[str, list]:
    """Convert :name binds to ? placeholders (pyodbc)."""
    ordered: list = []

    def sub(m: re.Match) -> str:
        ordered.append(params[m.group(1)])
        return "?"

    return _BIND_RE.sub(sub, sql), ordered


def _blank_literals(sql: str) -> str:
    """Blank out quoted string literals, preserving length.

    A :name inside a literal is text, not a bind. Length is preserved so
    offsets into the result still line up with the original statement.
    """
    out = list(sql)
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch not in ("'", '"'):
            i += 1
            continue
        out[i] = " "
        j = i + 1
        while j < n:
            if sql[j] == ch:
                if j + 1 < n and sql[j + 1] == ch:   # '' escapes a quote
                    out[j] = out[j + 1] = " "
                    j += 2
                    continue
                out[j] = " "
                break
            out[j] = " "
            j += 1
        i = j + 1
    return "".join(out)


def bind_names(sql: str) -> set:
    """Lowercase bind names the statement actually references."""
    return {m.lower() for m in _BIND_RE.findall(_blank_literals(sql))}


def _bound_params(sql: str, params: dict) -> dict:
    """Drop parameters the statement does not reference.

    python-oracledb in thin mode raises DPY-4008 when handed a bind the
    statement never uses. That now happens by design: opt_expr() replaces a
    predicate like `A2.SETID = :setid` with `1 = 1` on a record that has no
    SETID, while the caller still passes setid. Filtering at the point of
    execution lets any builder drop a predicate without also having to reach
    back and prune its caller's parameter dict — otherwise every optional
    column carrying a bind would be a DPY-4008 waiting for the one site whose
    record lacks it.
    """
    if not params:
        return params
    used = bind_names(sql)
    if all(k.lower() in used for k in params):
        return params
    return {k: v for k, v in params.items() if k.lower() in used}


def _bind_spans(sql: str) -> list:
    """(start, end, name) for every REAL bind, located on the blanked
    copy so :name inside a string literal stays text. _blank_literals is
    length-preserving, so the spans map straight onto the original."""
    return [(m.start(), m.end(), m.group(1))
            for m in _BIND_RE.finditer(_blank_literals(sql))]


def _to_pyformat(sql: str, params: dict) -> tuple[str, dict]:
    """:name -> %(name)s for the BigQuery DB-API shim (pyformat).

    The shim renders the operation with Python %-formatting, so every
    literal % in non-bind text must become %%. Span-based on purpose:
    _to_qmark's regex substitutes inside string literals, which is a
    known weakness this translator must not inherit."""
    spans = _bind_spans(sql)
    out = []
    cursor = 0
    used: dict = {}
    lowered = {k.lower(): k for k in (params or {})}
    for start, end, name in spans:
        out.append(sql[cursor:start].replace("%", "%%"))
        out.append(f"%({name})s")
        original = lowered.get(name.lower())
        if original is not None:
            used[name] = params[original]
        cursor = end
    out.append(sql[cursor:].replace("%", "%%"))
    return "".join(out), used


def _to_bq_named(sql: str, params: dict) -> tuple[str, dict]:
    """:name -> @name, for the dry-run path that calls the client
    directly (pyformat is a shim-layer convention the raw client does
    not speak)."""
    spans = _bind_spans(sql)
    out = []
    cursor = 0
    used: dict = {}
    lowered = {k.lower(): k for k in (params or {})}
    for start, end, name in spans:
        out.append(sql[cursor:start])
        out.append(f"@{name}")
        original = lowered.get(name.lower())
        if original is not None:
            used[name] = params[original]
        cursor = end
    out.append(sql[cursor:])
    return "".join(out), used


def _bq_query_parameters(params: dict) -> list:
    """Typed ScalarQueryParameters for the dry-run path. bool before
    int on purpose: bool is an int subclass and would silently become
    INT64."""
    import datetime as _dt
    import decimal as _decimal

    from google.cloud import bigquery
    typed = []
    for name, value in (params or {}).items():
        if isinstance(value, bool):
            kind = "BOOL"
        elif isinstance(value, int):
            kind = "INT64"
        elif isinstance(value, float):
            kind = "FLOAT64"
        elif isinstance(value, _decimal.Decimal):
            kind = "NUMERIC"
        elif isinstance(value, _dt.datetime):
            kind = "TIMESTAMP"
        elif isinstance(value, _dt.date):
            kind = "DATE"
        else:
            kind = "STRING"
            value = None if value is None else str(value)
        typed.append(bigquery.ScalarQueryParameter(name, kind, value))
    return typed


def _jsonable(v: Any) -> Any:
    if hasattr(v, "isoformat"):  # datetime.date / datetime.datetime
        return v.isoformat()[:10] if getattr(v, "hour", None) in (None, 0) else v.isoformat()
    import decimal
    if isinstance(v, decimal.Decimal):
        # BigQuery NUMERIC arrives as Decimal; pivot arithmetic needs a
        # number. Finance amounts sit far below 2**53, so float is exact
        # for every value this app should ever see.
        return float(v)
    return v


# Driver/server errors meaning "this session is gone" — reconnect and retry
# once rather than failing every later question until the process restarts.
# Corporate networks reap idle TCP sessions and Oracle profiles set IDLE_TIME,
# so a long-running GUI WILL hit these.
_DEAD_CONN_MARKERS = (
    "dpy-4011",      # the database or network closed the connection
    "dpy-1001",      # not connected
    "dpy-6005",      # connect failed / cannot reconnect
    "ora-03113",     # end-of-file on communication channel
    "ora-03114",     # not connected to ORACLE
    "ora-01012",     # not logged on
    "ora-02396",     # exceeded maximum idle time
    "ora-00028",     # your session has been killed
    "ora-01089",     # immediate shutdown in progress
    "connection is closed",
    "closed connection",
    "cannot operate on a closed database",
)


def _is_dead_connection(msg: str) -> bool:
    low = msg.lower()
    return any(m in low for m in _DEAD_CONN_MARKERS)


# Errors where trying again is not merely useless but HARMFUL. Oracle counts a
# rejected logon against FAILED_LOGIN_ATTEMPTS in the account's profile — 10 in
# the shipped DEFAULT profile — and connecting here is lazy and per-query, so a
# wrong password would otherwise be re-offered on every single query until the
# service account locks. That is a few minutes of ordinary clicking, and it
# locks the account for everything else that uses it, not just this app.
#
# ORA-28002 is deliberately absent: "the password will expire within N days"
# accompanies a SUCCESSFUL logon and must not block anything.
_CREDENTIAL_MARKERS = (
    "ora-01017",     # invalid username/password; logon denied
    "ora-01005",     # null password given
    "ora-28000",     # the account is locked -- already too late; stop here
    "ora-28001",     # the password has expired
    "ora-01045",     # user lacks CREATE SESSION
    "login failed for user",          # SQL Server
)


def _is_credential_failure(msg: str) -> bool:
    low = msg.lower()
    return any(m in low for m in _CREDENTIAL_MARKERS)


_CREDENTIAL_REMEDY = (
    "Not retrying: every rejected logon counts against the account's "
    "FAILED_LOGIN_ATTEMPTS profile limit, and this app connects lazily per "
    "query — retrying would lock the service account for every other consumer "
    "within a few clicks. Correct the credentials and reload.\n"
    "  - The password is read from ORACLE_PASSWORD in the .env file beside "
    "config.yaml, falling back to db.oracle_password in config.yaml. The "
    "environment variable wins.\n"
    "  - A password containing $ or {} must be single-quoted in .env, and must "
    "NOT be exported from a shell that expands it — a shell-mangled password "
    "is indistinguishable from a wrong one here.\n"
    "  - After fixing it: the configuration console's reload picks it up for "
    "the dashboards. The chat's answer engine is a separate process that "
    "read its password at startup, so chat needs a service restart."
)


class Database:
    """Connection manager sized for MULTIPLE CONCURRENT CHAT CHANNELS.

    Oracle uses a session pool: each query acquires its own session, so N chat
    channels run N queries in parallel instead of serializing behind one
    connection and one global lock. SQLite (sample data only) uses one
    connection per thread. Dead sessions are detected and retried once.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dialect = cfg.db.backend.lower()
        if self.dialect not in ("sqlite", "oracle", "sqlserver",
                                "bigquery"):
            raise DbError(f"Unsupported db backend: {self.dialect}")
        self._lock = threading.Lock()          # guards pool/connection setup
        self._conn = None                      # sqlserver + legacy single conn
        self._pool = None                      # oracle session pool
        self._local = threading.local()        # per-thread sqlite connections
        self._bq_client = None                 # shared thread-safe client
        self._bq_stats = threading.local()     # last bytes-billed, per channel
        self._catalog: dict = {}               # table -> real column names
        self._catalog_lock = threading.Lock()
        # Set once the database has REJECTED these credentials. Every later
        # connection attempt is refused from here instead of being offered to
        # the server again. Cleared only by building a new Database, which is
        # what a restart and the console's reload both do.
        self._credentials_refused = ""

    # ---- dialect helpers -------------------------------------------------
    @property
    def default_schema(self) -> str:
        """Schema used for an unqualified object name.

        Configuration loading normalizes this to one scalar.  Keeping the
        small defensive conversion here also makes Database safe in focused
        tests and integrations that construct ``DbCfg`` directly.
        """
        raw = getattr(self.cfg.db, "schema", "")
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else ""
        return str(raw or "").strip().rstrip(".").upper()

    @property
    def allowed_schemas(self) -> tuple[str, ...]:
        """Configured same-database schema boundary, default first."""
        raw = getattr(self.cfg.db, "schemas", []) or []
        if isinstance(raw, str):
            raw = [raw]
        ordered: list[str] = []
        configured = ([self.default_schema] if self.default_schema else []) \
            + list(raw)
        for value in configured:
            schema = str(value or "").strip().rstrip(".").upper()
            if schema and schema not in ordered:
                ordered.append(schema)
        return tuple(ordered)

    def table_scope(self, table: str) -> tuple[str, str]:
        """Return ``(owner, object)`` within this connection's allowlist.

        An explicit owner is never trusted merely because the login can read
        it.  In particular, a DBA-capable Oracle account remains restricted
        to the schemas configured for the selected source.
        """
        match = _CATALOG_OBJECT_RE.fullmatch(str(table or ""))
        if not match:
            raise DbError(f"Invalid table name: {table!r}")
        explicit = match.group(2) is not None
        first = _catalog_identifier_value(match.group(1))
        second = _catalog_identifier_value(match.group(2) or "")
        owner = first if explicit else self.default_schema
        name = second if explicit else first
        allowed = self.allowed_schemas
        if explicit and allowed and owner not in allowed:
            raise DbError(
                f"Schema {owner!r} is outside this source's configured "
                f"boundary ({', '.join(allowed)})"
            )
        return owner, name

    @property
    def prefix(self) -> str:
        if self.dialect == "bigquery":
            # The uppercased validation-space default must never be
            # spliced into executed SQL: BigQuery names are case
            # sensitive, and unqualified names resolve server-side via
            # default_dataset on the job config.
            return ""
        s = self.default_schema
        return f"{s}." if s else ""

    @property
    def bigquery_dataset(self) -> str:
        """The TRUE-CASE configured dataset, for internal SQL only.

        Validation compares uppercase (default_schema); execution and
        interpolation use this verbatim value. Operator-configured and
        identifier-validated at load -- the same trust level as prefix,
        never model-influenced."""
        raw = getattr(self.cfg.db, "schema", "")
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else ""
        return str(raw or "").strip().rstrip(".")

    # ---- schema catalog --------------------------------------------------
    def columns(self, table: str) -> set:
        """Columns this site's copy of a record ACTUALLY has (cached).

        PeopleSoft record shapes vary by release, by module install and by
        customization: PS_ITEM may date items with ACCTG_DT or ASOF_DT,
        PS_BUS_UNIT_TBL_GL may carry no DESCR at all. Every optional column
        must therefore be checked here before a builder selects it, rather
        than each query discovering its own ORA-00904 in production.

        Returns an empty set when the catalog cannot be read, which callers
        must treat as "assume the reference shape" — never as "no columns".
        """
        try:
            owner, name = self.table_scope(table)
        except DbError:
            return set()
        key = f"{owner}.{name}" if owner else name
        with self._catalog_lock:
            if key in self._catalog:
                return self._catalog[key]
        names: set = set()
        try:
            from . import queries as _q

            params: dict = {}
            sql = _q.table_describe(self, key, params)
            rows, _ = self.query(sql, params, max_rows=1000)
            names = {str(r.get("column_name") or r.get("name") or "").upper()
                     for r in rows} - {""}
        except Exception:
            # A transient connection failure is not evidence a record is
            # absent, so this one stays UNCACHED — the next call asks again.
            return set()
        # A describe that SUCCEEDED and found nothing is a real answer, and
        # refusing to remember it meant one dictionary read per absent
        # record per catalog build, forever. On Oracle that is an
        # ALL_TAB_COLUMNS query, not a free lookup.
        with self._catalog_lock:
            self._catalog[key] = names
        return names

    def has_column(self, table: str, column: str) -> bool:
        """True when the column exists, or when the catalog is unreadable
        (fall back to the reference shape rather than dropping the column)."""
        cols = self.columns(table)
        return (not cols) or column.upper() in cols

    def indexes(self, table: str) -> list:
        """Indexes on a table, with their column lists in order (cached).

        This is what lets a join be REASONED ABOUT before it is run: the
        optimizer can only use an index whose LEADING columns appear in the
        predicates, so "which indexes exist and what do they lead with" is
        exactly the information a query writer needs and never had. Returns
        [] when unreadable — callers treat that as "unknown", never "none".
        """
        try:
            owner, table_name = self.table_scope(table)
        except DbError:
            return []
        scoped_name = f"{owner}.{table_name}" if owner else table_name
        key = ("IDX", scoped_name)
        with self._catalog_lock:
            if key in self._catalog:
                return self._catalog[key]
        out: list = []
        try:
            if self.dialect == "oracle":
                where = "C.TABLE_NAME = :t"
                params: dict = {"t": table_name}
                if owner:
                    where += " AND C.TABLE_OWNER = :o"
                    params["o"] = owner
                rows, _ = self.query(
                    f"SELECT C.INDEX_NAME AS name, C.COLUMN_NAME AS col, "
                    f"C.COLUMN_POSITION AS pos, I.UNIQUENESS AS uniq "
                    f"FROM ALL_IND_COLUMNS C JOIN ALL_INDEXES I "
                    f"ON I.INDEX_NAME = C.INDEX_NAME "
                    f"AND I.TABLE_NAME = C.TABLE_NAME "
                    f"AND I.OWNER = C.INDEX_OWNER "
                    f"WHERE {where} ORDER BY C.INDEX_NAME, C.COLUMN_POSITION",
                    params, max_rows=500)
                by_name: dict = {}
                for r in rows:
                    entry = by_name.setdefault(
                        str(r["name"]),
                        {"name": str(r["name"]),
                         "unique": str(r.get("uniq") or "") == "UNIQUE",
                         "columns": []})
                    entry["columns"].append(str(r["col"]))
                out = list(by_name.values())
            elif self.dialect == "sqlite":
                if not self.columns(scoped_name):
                    return []
                rows, _ = self.query(f"PRAGMA index_list({table_name})",
                                     {}, max_rows=100)
                for r in rows:
                    name = str(r.get("name") or "")
                    cols, _ = self.query(f"PRAGMA index_info({name})",
                                         {}, max_rows=50)
                    out.append({
                        "name": name,
                        "unique": bool(r.get("unique")),
                        "columns": [str(c.get("name") or "")
                                    for c in sorted(
                                        cols, key=lambda x: x.get("seqno", 0))],
                    })
            elif self.dialect == "sqlserver":
                where = "UPPER(T.name) = :t"
                params = {"t": table_name}
                if owner:
                    where += " AND UPPER(S.name) = :o"
                    params["o"] = owner
                rows, _ = self.query(
                    "SELECT I.name AS name, C.name AS col, "
                    "IC.key_ordinal AS pos, I.is_unique AS uniq "
                    "FROM sys.tables T JOIN sys.schemas S "
                    "ON S.schema_id = T.schema_id "
                    "JOIN sys.indexes I ON I.object_id = T.object_id "
                    "JOIN sys.index_columns IC ON IC.object_id = I.object_id "
                    "AND IC.index_id = I.index_id "
                    "JOIN sys.columns C ON C.object_id = IC.object_id "
                    "AND C.column_id = IC.column_id "
                    f"WHERE {where} AND I.index_id > 0 "
                    "AND I.is_hypothetical = 0 AND IC.key_ordinal > 0 "
                    "ORDER BY I.name, IC.key_ordinal",
                    params, max_rows=500)
                by_name: dict = {}
                for r in rows:
                    name = str(r["name"]).upper()
                    entry = by_name.setdefault(
                        name,
                        {"name": name,
                         "unique": bool(r.get("uniq")), "columns": []})
                    entry["columns"].append(str(r["col"]).upper())
                out = list(by_name.values())
        except Exception:
            return []
        with self._catalog_lock:
            self._catalog[key] = out
        return out

    def clear_catalog(self) -> None:
        with self._catalog_lock:
            self._catalog.clear()

    def today_expr(self) -> str:
        return {
            "sqlite": "DATE('now')",
            "oracle": "TRUNC(SYSDATE)",
            "sqlserver": "CAST(GETDATE() AS DATE)",
            "bigquery": "CURRENT_DATE()",
        }[self.dialect]

    def exists_sql(self, sql: str) -> str:
        """Wrap a SELECT so it stops at the first matching row."""
        if self.dialect == "oracle":
            return f"SELECT * FROM ({sql}) WHERE ROWNUM = 1"
        if self.dialect == "sqlserver":
            return sql.replace("SELECT 1", "SELECT TOP 1 1", 1)
        # sqlite and bigquery both speak LIMIT. On BigQuery LIMIT bounds
        # ROWS, not bytes billed -- the client's byte cap is the cost
        # guard, this is only an early stop.
        return f"{sql} LIMIT 1"

    def days_past_expr(self, date_col: str, asof_bind: str) -> str:
        """Whole days from date_col to the :asof bind (positive = past due)."""
        if self.dialect == "oracle":
            return f"TRUNC(TO_DATE(:{asof_bind}, 'YYYY-MM-DD') - {date_col})"
        if self.dialect == "sqlserver":
            return f"DATEDIFF(day, {date_col}, :{asof_bind})"
        if self.dialect == "bigquery":
            # CAST is legal on both DATE and TIMESTAMP columns.
            return (f"DATE_DIFF(CAST(:{asof_bind} AS DATE), "
                    f"CAST({date_col} AS DATE), DAY)")
        return f"CAST(julianday(:{asof_bind}) - julianday({date_col}) AS INTEGER)"

    def date_bind(self, name: str) -> str:
        """Expression that binds an ISO yyyy-mm-dd string as a date."""
        if self.dialect == "oracle":
            return f"TO_DATE(:{name}, 'YYYY-MM-DD')"
        if self.dialect == "bigquery":
            return f"CAST(:{name} AS DATE)"
        return f":{name}"

    # ---- connection ------------------------------------------------------
    @property
    def _timeout_ms(self) -> int:
        return int(getattr(self.cfg.db, "query_timeout_seconds", 120) or 0) * 1000

    def _oracle_pool(self):
        """Session pool so concurrent chat channels do not serialize."""
        if self._pool is not None:
            return self._pool
        if self._credentials_refused:
            raise DbError(self._credentials_refused)
        with self._lock:
            if self._pool is not None:
                return self._pool
            if self._credentials_refused:
                raise DbError(self._credentials_refused)
            try:
                import oracledb
            except ImportError as e:
                raise DbError(
                    "python-oracledb not installed — pip install -e '.[oracle]'"
                ) from e
            c = self.cfg.db
            if not (c.oracle_dsn and c.oracle_user):
                raise DbError("Set ORACLE_DSN / ORACLE_USER / ORACLE_PASSWORD in .env")
            kw: dict = {
                "user": c.oracle_user, "password": c.oracle_password,
                "dsn": c.oracle_dsn,
                "min": 1, "max": max(int(getattr(c, "pool_max", 8) or 8), 1),
                "increment": 1,
                # Hand out a session only after checking it is alive, so a
                # session reaped by an idle timeout never reaches a query.
                "ping_interval": 0,
                "getmode": getattr(oracledb, "POOL_GETMODE_WAIT", 1),
            }
            # Optional wallet / tnsnames directory (thin mode reads TNS_ADMIN
            # too, but an explicit config_dir is what most sites need).
            cfg_dir = (getattr(c, "oracle_config_dir", "") or "").strip()
            if cfg_dir:
                kw["config_dir"] = str(self.cfg.resolve_path(cfg_dir))
            wallet = (getattr(c, "oracle_wallet_dir", "") or "").strip()
            if wallet:
                kw["wallet_location"] = str(self.cfg.resolve_path(wallet))
                wpw = (getattr(c, "oracle_wallet_password", "") or "").strip()
                if wpw:
                    kw["wallet_password"] = wpw
            try:
                self._pool = oracledb.create_pool(**kw)
            except Exception as e:
                if _is_credential_failure(str(e)):
                    self._credentials_refused = (
                        f"Oracle refused these credentials for "
                        f"{c.oracle_user}@{c.oracle_dsn}: {e}\n"
                        f"{_CREDENTIAL_REMEDY}")
                    raise DbError(self._credentials_refused) from e
                raise DbError(f"Oracle connection failed: {e}") from e
            return self._pool

    def _sqlite_conn(self):
        import sqlite3

        conn = getattr(self._local, "sqlite", None)
        if conn is not None:
            return conn
        path = self.cfg.resolve_path(self.cfg.db.sqlite_path)
        if not path.exists():
            raise DbError(
                f"SQLite sample database not found at {path}. "
                "Run: python3 scripts/seed_sample_data.py"
            )
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._local.sqlite = conn
        return conn

    def _sqlserver_conn(self):
        if self._conn is not None:
            return self._conn
        if self._credentials_refused:
            raise DbError(self._credentials_refused)
        with self._lock:
            if self._conn is not None:
                return self._conn
            # Re-checked INSIDE the lock, as the Oracle path does: two
            # threads can both pass the unlocked check, and the second
            # would burn one more failed logon after the first latched.
            if self._credentials_refused:
                raise DbError(self._credentials_refused)
            try:
                import pyodbc
            except ImportError as e:
                raise DbError("pyodbc not installed — pip install -e '.[sqlserver]'") from e
            if not self.cfg.db.mssql_conn_str:
                raise DbError("Set MSSQL_CONN_STR in .env")
            try:
                self._conn = pyodbc.connect(self.cfg.db.mssql_conn_str)
            except Exception as e:
                # SQL Server logins lock out too, on the same principle.
                if _is_credential_failure(str(e)):
                    self._credentials_refused = (
                        f"SQL Server refused these credentials: {e}\n"
                        f"{_CREDENTIAL_REMEDY}")
                    raise DbError(self._credentials_refused) from e
                raise DbError(f"SQL Server connection failed: {e}") from e
            if self._timeout_ms > 0:
                self._conn.timeout = self._timeout_ms // 1000
            return self._conn

    @contextlib.contextmanager
    def _session(self):
        """Yield (connection, needs_lock). Oracle sessions come from the pool
        and are independent, so no cross-channel lock is taken."""
        if self.dialect == "oracle":
            pool = self._oracle_pool()
            # The latch has to guard the ACQUIRE, not just pool creation.
            # In thin mode create_pool() builds the pool object without
            # logging on — the logon happens here, lazily, per session. So
            # this is where a wrong password actually surfaces, and without
            # the check here every query re-offered it to the server:
            # exactly the FAILED_LOGIN_ATTEMPTS lockout the latch exists to
            # prevent, still live on the one path a real instance takes.
            if self._credentials_refused:
                raise DbError(self._credentials_refused)
            try:
                conn = pool.acquire()
            except Exception as e:
                if _is_credential_failure(str(e)):
                    c = self.cfg.db
                    self._credentials_refused = (
                        f"Oracle refused these credentials for "
                        f"{c.oracle_user}@{c.oracle_dsn}: {e}\n"
                        f"{_CREDENTIAL_REMEDY}")
                    raise DbError(self._credentials_refused) from e
                raise DbError(f"Oracle connection failed: {e}") from e
            if self._timeout_ms > 0:
                conn.call_timeout = self._timeout_ms
            try:
                yield conn, False
            finally:
                try:
                    pool.release(conn)
                except Exception:
                    pass
        elif self.dialect == "sqlite":
            yield self._sqlite_conn(), False
        elif self.dialect == "bigquery":
            try:
                from google.cloud.bigquery import dbapi
            except ImportError as e:
                raise DbError(
                    "google-cloud-bigquery is not installed -- "
                    "pip install -e '.[bigquery]'") from e
            # One shared thread-safe Client; a fresh, free dbapi
            # Connection per session (no logon happens at connect).
            # needs_lock=False: HTTP jobs are independent, so chat
            # channels get Oracle-pool concurrency with zero pool code.
            conn = dbapi.connect(self._bigquery_client())
            try:
                yield conn, False
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        else:
            yield self._sqlserver_conn(), True

    def _bigquery_client(self):
        """The shared client, built once under the lock.

        The default job config is THE cost guard: maximum_bytes_billed
        rides on every query because no call-site ever passes its own
        job config -- there is no path that can forget the cap.

        NO credential latch for this dialect, deliberately: the Oracle
        latch exists because retrying harms (FAILED_LOGIN_ATTEMPTS
        lockout). ADC has no lockout counter, and a transient
        metadata-server blip at Cloud Run cold start looks exactly like
        a credential refusal -- latching it would brick a healthy
        revision until restart. Every credential-shaped failure is
        translated with the full remedy, every time.
        """
        with self._lock:
            if self._bq_client is not None:
                return self._bq_client
            try:
                from google.cloud import bigquery
            except ImportError as e:
                raise DbError(
                    "google-cloud-bigquery is not installed -- "
                    "pip install -e '.[bigquery]'") from e
            c = self.cfg.db
            job_cfg = bigquery.QueryJobConfig(
                maximum_bytes_billed=int(
                    getattr(c, "bigquery_max_bytes_billed", 0)
                    or 1_073_741_824),
                use_query_cache=True,
                default_dataset=(
                    f"{c.bigquery_project}.{self.bigquery_dataset}"),
                labels={"app": "pstb"})
            if self._timeout_ms > 0:
                job_cfg.job_timeout_ms = self._timeout_ms
            try:
                self._bq_client = bigquery.Client(
                    project=c.bigquery_project,
                    location=getattr(c, "bigquery_location", "") or None,
                    default_query_job_config=job_cfg)
            except Exception as e:
                raise self._translate(e) from e
            return self._bq_client

    def _discard_session(self) -> None:
        """Drop cached state after a dead-connection error so the retry (and
        every later question) gets a fresh session."""
        if self.dialect == "sqlite":
            conn = getattr(self._local, "sqlite", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                self._local.sqlite = None
        elif self.dialect == "oracle":
            pool, self._pool = self._pool, None
            if pool is not None:
                try:
                    pool.close(force=True)
                except Exception:
                    pass
        elif self.dialect == "bigquery":
            with self._lock:
                client, self._bq_client = self._bq_client, None
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        else:
            conn, self._conn = self._conn, None
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _connect(self):
        """Back-compat single-connection accessor (diagnostics/tests)."""
        with self._session() as (conn, _):
            return conn


    def _capture_bq_stats(self, cur) -> None:
        """Actual spend, per channel (threading.local -- chat channels
        run concurrently and must not read each other's bill)."""
        job = getattr(cur, "query_job", None)
        if job is None:
            return
        try:
            self._bq_stats.last = {
                "bytes_billed": int(
                    getattr(job, "total_bytes_billed", 0) or 0),
                "cache_hit": bool(getattr(job, "cache_hit", False)),
            }
        except Exception:
            pass

    def last_query_stats(self) -> dict:
        """The most recent bigquery job's billing facts for THIS thread;
        empty for every other dialect."""
        return dict(getattr(self._bq_stats, "last", {}) or {})

    def object_names(self) -> tuple:
        """(SCHEMA_UPPER, NAME_UPPER) for every table/view this
        connection may see, cached for the process lifetime.

        The suggestion ladder used to probe the live dictionary with up
        to eight LIKE queries per unresolved name -- on a PeopleSoft
        instance whose dictionary sits behind row-security policies,
        that was the hottest statement the DBA saw. One bounded read
        per process replaces all of them. Staleness is the same the
        columns cache already accepts; the console reload invalidates
        by REPLACING the Database (a fresh instance has an empty
        catalog), and clear_catalog() remains the manual/test
        primitive.
        """
        key = ("ALL_OBJECT_NAMES",)
        with self._catalog_lock:
            cached = self._catalog.get(key)
        if cached is not None:
            return cached
        if self.dialect == "sqlite":
            rows, _ = self.query(
                "SELECT 'MAIN' AS s, name AS n FROM sqlite_master "
                "WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%'", {}, max_rows=250_000)
        elif self.dialect == "oracle":
            owners = self.allowed_schemas
            if owners:
                marks = ", ".join(f":o{i}" for i in range(len(owners)))
                params = {f"o{i}": o for i, o in enumerate(owners)}
                rows, _ = self.query(
                    "SELECT OWNER AS s, OBJECT_NAME AS n FROM "
                    f"ALL_OBJECTS WHERE OWNER IN ({marks}) "
                    "AND OBJECT_TYPE IN ('TABLE','VIEW')",
                    params, max_rows=250_000)
            else:
                rows, _ = self.query(
                    "SELECT USER AS s, OBJECT_NAME AS n FROM "
                    "USER_OBJECTS WHERE OBJECT_TYPE IN ('TABLE','VIEW')",
                    {}, max_rows=250_000)
        elif self.dialect == "sqlserver":
            rows, _ = self.query(
                "SELECT S.name AS s, O.name AS n FROM sys.objects O "
                "JOIN sys.schemas S ON S.schema_id = O.schema_id "
                "WHERE O.type IN ('U','V')", {}, max_rows=250_000)
        else:
            rows = [(self.bigquery_dataset.upper(), n)
                    for n in sorted(self.table_names())]
            rows = [{"s": s, "n": n} for s, n in rows]
        pairs = tuple(sorted(
            (str(r.get("s") or "").upper(), str(r.get("n") or "").upper())
            for r in rows if str(r.get("n") or "")))
        with self._catalog_lock:
            self._catalog[key] = pairs
        return pairs

    def table_names(self) -> frozenset:
        """Uppercase table/view names of the configured dataset, cached
        for the process lifetime (same staleness the columns cache
        already accepts). One 10MB-minimum job per process instead of
        one per referenced table per question."""
        if self.dialect != "bigquery":
            raise DbError("table_names() is a bigquery-only accessor")
        key = ("BQ_TABLES",)
        with self._catalog_lock:
            cached = self._catalog.get(key)
        if cached is not None:
            return cached
        ds = self.bigquery_dataset
        rows, _ = self.query(
            f"SELECT table_name FROM {ds}.INFORMATION_SCHEMA.TABLES",
            {}, max_rows=100_000)
        names = frozenset(str(r.get("table_name") or "").upper()
                          for r in rows)
        with self._catalog_lock:
            self._catalog[key] = names
        return names

    def explain_plan(self, sql: str, params: Optional[dict] = None) -> dict:
        """Optimizer plan for a SELECT, WITHOUT executing it.

        On Oracle this is EXPLAIN PLAN + a read of PLAN_TABLE: the database
        estimates cost and access paths for free, which is a far better cost
        ceiling than hoping a model writes sargable SQL. On SQLite it is
        EXPLAIN QUERY PLAN, which is enough to prove the mechanism in tests.

        Returns {"available": False, ...} rather than raising when the plan
        cannot be read — a missing PLAN_TABLE or a denied grant must degrade
        to "unknown cost", never block a legitimate query.
        """
        if self.dialect == "oracle":
            stmt_id = "pstb_{0}".format(abs(hash(sql)) % 10_000_000)
            try:
                with self._session() as (conn, _):
                    cur = conn.cursor()
                    try:
                        cur.execute(
                            f"EXPLAIN PLAN SET STATEMENT_ID = '{stmt_id}' FOR {sql}")
                        cur.execute(
                            "SELECT OPERATION, OPTIONS, OBJECT_OWNER, "
                            "OBJECT_NAME, CARDINALITY, COST FROM PLAN_TABLE "
                            "WHERE STATEMENT_ID = :sid ORDER BY ID",
                            {"sid": stmt_id})
                        rows = [
                            {"operation": r[0], "options": r[1],
                             "object_owner": r[2], "object": r[3],
                             "rows": r[4], "cost": r[5]}
                            for r in cur.fetchall()
                        ]
                        cur.execute(
                            "DELETE FROM PLAN_TABLE WHERE STATEMENT_ID = :sid",
                            {"sid": stmt_id})
                        conn.commit()
                    finally:
                        try:
                            cur.close()
                        except Exception:
                            pass
            except Exception as e:
                return {"available": False, "reason": str(e)[:200]}
            return {"available": True, "steps": rows}

        if self.dialect == "sqlite":
            try:
                # SQLite refuses to plan a statement whose binds have no
                # values; Oracle's EXPLAIN PLAN parses without them.
                rows, _ = self.query("EXPLAIN QUERY PLAN " + sql, params or {},
                                     max_rows=200)
            except Exception as e:
                return {"available": False, "reason": str(e)[:200]}
            steps = []
            for row in rows:
                detail = str(row.get("detail") or "")
                upper = detail.upper()
                steps.append({
                    "operation": "SCAN" if upper.startswith("SCAN") else "SEARCH",
                    "options": "FULL" if upper.startswith("SCAN") else "",
                    "object_owner": "",
                    "object": detail.split()[1] if len(detail.split()) > 1 else "",
                    "rows": None, "cost": None, "detail": detail,
                })
            return {"available": True, "steps": steps}

        if self.dialect == "bigquery":
            try:
                from google.cloud import bigquery
                named, used = _to_bq_named(sql, params or {})
                job_cfg = bigquery.QueryJobConfig(
                    dry_run=True, use_query_cache=False,
                    default_dataset=(
                        f"{self.cfg.db.bigquery_project}."
                        f"{self.bigquery_dataset}"),
                    query_parameters=_bq_query_parameters(used))
                job = self._bigquery_client().query(named, job_config=job_cfg)
                estimated = int(getattr(job, "total_bytes_processed", 0) or 0)
            except Exception as e:
                return {"available": False, "reason": str(e)[:200]}
            cap = int(getattr(self.cfg.db, "bigquery_max_bytes_billed", 0)
                      or 1_073_741_824)
            return {
                "available": True,
                "steps": [{"operation": "ESTIMATE", "options": "DRY_RUN",
                           "object_owner": "", "object": "",
                           "rows": None, "cost": estimated}],
                "estimated_bytes": estimated,
                # on-demand list price; an estimate for a person, not a bill
                "estimated_cost_usd": round(estimated / 2**40 * 6.25, 4),
                "cap_bytes": cap,
                "within_cap": estimated <= cap,
            }

        return {"available": False, "reason": f"no plan support for {self.dialect}"}

    # ---- querying --------------------------------------------------------
    def query(
        self, sql: str, params: Optional[dict] = None, max_rows: Optional[int] = None
    ) -> tuple[list[dict], bool]:
        """Run a SELECT; returns (rows, truncated). Rows are lowercase-keyed dicts."""
        params = params or {}
        cap = max_rows if max_rows is not None else self.cfg.tools.max_rows
        try:
            return self._execute(sql, params, cap)
        except DbError:
            raise
        except Exception as ex:
            # No dead-session retry on BigQuery: HTTP is stateless,
            # google-api-core already retries transient transport faults,
            # and re-running a query here RE-BILLS it.
            if self.dialect == "bigquery" or not _is_dead_connection(str(ex)):
                raise self._translate(ex) from ex
            # The session died (idle reaped, DBA kill, network drop). Drop it
            # and try once on a fresh one — otherwise every later question in
            # every channel fails until the process is restarted.
            self._discard_session()
            try:
                return self._execute(sql, params, cap)
            except Exception as ex2:
                raise self._translate(ex2) from ex2

    def stream_query(
        self, sql: str, params: Optional[dict] = None, *,
        max_rows: int, batch_size: int = 2_000,
        on_start, on_rows,
    ) -> tuple[int, bool, int]:
        """Fetch a SELECT in bounded chunks and hand each chunk to a sink.

        Returns ``(rows_written, truncated, column_count)``.  At no point is
        the full population retained in memory.  A dead session is retried
        once from the beginning; ``on_start`` is called for each attempt so a
        file sink can truncate the partial first attempt before replaying.
        """
        params = params or {}
        cap = max(1, int(max_rows))
        chunk = max(1, min(int(batch_size or 2_000), 20_000))
        for attempt in range(2):
            try:
                return self._stream_execute(
                    sql, params, cap, chunk, on_start, on_rows)
            except DbError:
                raise
            except Exception as ex:
                if (attempt == 0 and self.dialect != "bigquery"
                        and _is_dead_connection(str(ex))):
                    self._discard_session()
                    continue
                raise self._translate(ex) from ex
        raise DbError("Streamed query could not be completed")

    def _stream_execute(self, sql: str, params: dict, cap: int,
                        batch_size: int, on_start, on_rows
                        ) -> tuple[int, bool, int]:
        with self._session() as (conn, needs_lock):
            lock = self._lock if needs_lock else contextlib.nullcontext()
            with lock:
                cur = conn.cursor()
                try:
                    # Driver prefetch stays bounded too.  Oracle may expose
                    # either attribute depending on driver generation.
                    for attr in ("arraysize", "prefetchrows"):
                        try:
                            setattr(cur, attr, batch_size)
                        except Exception:
                            pass
                    if self.dialect == "sqlserver":
                        qsql, seq = _to_qmark(sql, params)
                        cur.execute(qsql, seq)
                    elif self.dialect == "bigquery":
                        qsql, used = _to_pyformat(sql, params)
                        cur.execute(qsql, used)
                        self._capture_bq_stats(cur)
                    else:
                        cur.execute(sql, _bound_params(sql, params))
                    cols = [d[0].lower() for d in cur.description]
                    on_start(cols)
                    written = 0
                    truncated = False
                    while True:
                        # One extra row proves whether the hard ceiling cut a
                        # population.  Without it an exact-cap result and a
                        # truncated result would receive the same filename.
                        remaining = cap - written
                        request = min(batch_size, remaining + 1)
                        raw = cur.fetchmany(max(request, 1))
                        if not raw:
                            break
                        allowed = raw[:remaining]
                        if allowed:
                            rows = [
                                {k: _jsonable(v) for k, v in zip(cols, row)}
                                for row in allowed
                            ]
                            on_rows(rows)
                            written += len(rows)
                        if len(raw) > len(allowed):
                            truncated = True
                            break
                        if written >= cap:
                            extra = cur.fetchmany(1)
                            truncated = bool(extra)
                            break
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
        return written, truncated, len(cols)

    def _execute(self, sql: str, params: dict, cap: int) -> tuple[list[dict], bool]:
        with self._session() as (conn, needs_lock):
            lock = self._lock if needs_lock else contextlib.nullcontext()
            with lock:
                cur = conn.cursor()
                try:
                    if self.dialect == "sqlserver":
                        qsql, seq = _to_qmark(sql, params)
                        cur.execute(qsql, seq)
                    elif self.dialect == "bigquery":
                        qsql, used = _to_pyformat(sql, params)
                        cur.execute(qsql, used)
                        self._capture_bq_stats(cur)
                    else:
                        cur.execute(sql, _bound_params(sql, params))
                    cols = [d[0].lower() for d in cur.description]
                    raw = cur.fetchmany(cap + 1)
                    truncated = len(raw) > cap
                    rows = [
                        {k: _jsonable(v) for k, v in zip(cols, r)} for r in raw[:cap]
                    ]
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
        return rows, truncated

    def _translate_bigquery(self, msg: str, low: str) -> DbError:
        """BigQuery errors -> remedies in this deployment's own terms.

        Credential-shaped failures are translated with the full remedy
        EVERY time and never latched (see _bigquery_client)."""
        c = self.cfg.db
        cap = int(getattr(c, "bigquery_max_bytes_billed", 0)
                  or 1_073_741_824)
        if "bytes billed" in low or "exceeded limit" in low:
            usd = round(cap / 2**40 * 6.25, 4)
            return DbError(
                "This query would scan more than the per-query byte cap "
                f"({cap:,} bytes ~= ${usd} at on-demand pricing; "
                "sources.<name>.bigquery_max_bytes_billed, operator-"
                "owned). Add a partition or date filter, or select fewer "
                "columns -- LIMIT does not reduce bytes billed on this "
                "backend.")
        if "not found: table" in low or "not found: dataset" in low:
            return DbError(
                f"Not found -- {msg.strip()[:300]}. Names are case-"
                "sensitive on this backend: copy them exactly as "
                "list_tables returns them.")
        if "unrecognized name" in low:
            return DbError(
                f"Column not found -- {msg.strip()[:300]}. Use "
                "describe_table to see this table's actual columns.")
        if ("access denied" in low or "permission denied" in low
                or "403" in low):
            return DbError(
                "The account authenticated but lacks a grant: the "
                "service account needs roles/bigquery.jobUser on the "
                f"project ({getattr(c, 'bigquery_project', '')}) and "
                "roles/bigquery.dataViewer on the dataset "
                f"({self.bigquery_dataset}).")
        if ("could not automatically determine credentials" in low
                or "defaultcredentialserror" in low
                or "invalid_grant" in low
                or "reauthentication is needed" in low
                or "401" in low):
            return DbError(
                "No usable Google credentials. Locally: run "
                "'gcloud auth application-default login' and reload. On "
                "Cloud Run: attach a service account with "
                "roles/bigquery.jobUser (project) and "
                "roles/bigquery.dataViewer (dataset) and redeploy.")
        if "deadline" in low or "timeout" in low:
            return DbError(
                f"Query exceeded the "
                f"{self.cfg.db.query_timeout_seconds}s timeout "
                "(db.query_timeout_seconds; enforced server-side as "
                "job_timeout_ms). Narrow the date range or add a "
                "partition filter.")
        return DbError(msg.strip())

    def _translate(self, ex: Exception) -> DbError:
        """Driver errors -> actionable remediation."""
        if isinstance(ex, DbError):
            return ex
        msg = str(ex)
        low = msg.lower()
        if self.dialect == "bigquery":
            # Dispatched FIRST: the generic head branch below matches any
            # message containing "timeout" and would dress BigQuery
            # errors in PS_LEDGER advice.
            return self._translate_bigquery(msg, low)
        if "DPY-4024" in msg or "call timeout" in low or "timeout" in low:
            return DbError(
                f"Query exceeded the {self.cfg.db.query_timeout_seconds}s timeout. "
                "Narrow the scope (business unit, fiscal year, period, account "
                "range), confirm PS_LEDGER indexes, or raise "
                "db.query_timeout_seconds in config.yaml."
            )
        if "ora-00904" in low or "invalid identifier" in low \
                or "no such column" in low:
            return DbError(
                f"Column not found — {msg.strip()}. This database's "
                "record shape differs from the reference layout "
                "(PeopleSoft records vary by release and site). Use "
                "describe_table to see the real columns before "
                "run_sql; run python scripts/diagnose_db.py to report "
                "the record shapes the curated tools adapt to."
            )
        if "ora-00942" in low or "no such table" in low \
                or "table or view does not exist" in low:
            return DbError(
                f"Table or view not found — {msg.strip()}. Qualify the name "
                "with its owner (e.g. SYSADM.PS_XYZ), check db.schema in "
                "config.yaml, and confirm the read-only user has SELECT "
                "grants; use list_tables to browse what is visible."
            )
        if "ora-01031" in low or "insufficient privileges" in low:
            return DbError(
                f"Insufficient privileges — {msg.strip()}. The read-only "
                "account needs SELECT on this record; ask your DBA for the "
                "grant, or use list_tables to see what is visible."
            )
        return DbError(msg.strip())

    def close(self) -> None:
        self._discard_session()
