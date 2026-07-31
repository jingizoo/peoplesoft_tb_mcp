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


def _to_qmark(sql: str, params: dict) -> tuple[str, list]:
    """Convert :name binds to ? placeholders (pyodbc)."""
    ordered: list = []

    def sub(m: re.Match) -> str:
        ordered.append(params[m.group(1)])
        return "?"

    return _BIND_RE.sub(sub, sql), ordered


def _jsonable(v: Any) -> Any:
    if hasattr(v, "isoformat"):  # datetime.date / datetime.datetime
        return v.isoformat()[:10] if getattr(v, "hour", None) in (None, 0) else v.isoformat()
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
        if self.dialect not in ("sqlite", "oracle", "sqlserver"):
            raise DbError(f"Unsupported db backend: {self.dialect}")
        self._lock = threading.Lock()          # guards pool/connection setup
        self._conn = None                      # sqlserver + legacy single conn
        self._pool = None                      # oracle session pool
        self._local = threading.local()        # per-thread sqlite connections
        self._catalog: dict = {}               # table -> real column names
        self._catalog_lock = threading.Lock()

    # ---- dialect helpers -------------------------------------------------
    @property
    def prefix(self) -> str:
        s = self.cfg.db.schema.strip().rstrip(".")
        return f"{s}." if s else ""

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
        key = table.upper()
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
            return set()
        if names:
            with self._catalog_lock:
                self._catalog[key] = names
        return names

    def has_column(self, table: str, column: str) -> bool:
        """True when the column exists, or when the catalog is unreadable
        (fall back to the reference shape rather than dropping the column)."""
        cols = self.columns(table)
        return (not cols) or column.upper() in cols

    def clear_catalog(self) -> None:
        with self._catalog_lock:
            self._catalog.clear()

    def today_expr(self) -> str:
        return {
            "sqlite": "DATE('now')",
            "oracle": "TRUNC(SYSDATE)",
            "sqlserver": "CAST(GETDATE() AS DATE)",
        }[self.dialect]

    def exists_sql(self, sql: str) -> str:
        """Wrap a SELECT so it stops at the first matching row."""
        if self.dialect == "oracle":
            return f"SELECT * FROM ({sql}) WHERE ROWNUM = 1"
        if self.dialect == "sqserver":  # pragma: no cover
            return sql
        if self.dialect == "sqlserver":
            return sql.replace("SELECT 1", "SELECT TOP 1 1", 1)
        return f"{sql} LIMIT 1"

    def days_past_expr(self, date_col: str, asof_bind: str) -> str:
        """Whole days from date_col to the :asof bind (positive = past due)."""
        if self.dialect == "oracle":
            return f"TRUNC(TO_DATE(:{asof_bind}, 'YYYY-MM-DD') - {date_col})"
        if self.dialect == "sqlserver":
            return f"DATEDIFF(day, {date_col}, :{asof_bind})"
        return f"CAST(julianday(:{asof_bind}) - julianday({date_col}) AS INTEGER)"

    def date_bind(self, name: str) -> str:
        """Expression that binds an ISO yyyy-mm-dd string as a date."""
        if self.dialect == "oracle":
            return f"TO_DATE(:{name}, 'YYYY-MM-DD')"
        return f":{name}"

    # ---- connection ------------------------------------------------------
    @property
    def _timeout_ms(self) -> int:
        return int(getattr(self.cfg.db, "query_timeout_seconds", 120) or 0) * 1000

    def _oracle_pool(self):
        """Session pool so concurrent chat channels do not serialize."""
        if self._pool is not None:
            return self._pool
        with self._lock:
            if self._pool is not None:
                return self._pool
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
        with self._lock:
            if self._conn is not None:
                return self._conn
            try:
                import pyodbc
            except ImportError as e:
                raise DbError("pyodbc not installed — pip install -e '.[sqlserver]'") from e
            if not self.cfg.db.mssql_conn_str:
                raise DbError("Set MSSQL_CONN_STR in .env")
            self._conn = pyodbc.connect(self.cfg.db.mssql_conn_str)
            if self._timeout_ms > 0:
                self._conn.timeout = self._timeout_ms // 1000
            return self._conn

    @contextlib.contextmanager
    def _session(self):
        """Yield (connection, needs_lock). Oracle sessions come from the pool
        and are independent, so no cross-channel lock is taken."""
        if self.dialect == "oracle":
            pool = self._oracle_pool()
            try:
                conn = pool.acquire()
            except Exception as e:
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
        else:
            yield self._sqlserver_conn(), True

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
            if not _is_dead_connection(str(ex)):
                raise self._translate(ex) from ex
            # The session died (idle reaped, DBA kill, network drop). Drop it
            # and try once on a fresh one — otherwise every later question in
            # every channel fails until the process is restarted.
            self._discard_session()
            try:
                return self._execute(sql, params, cap)
            except Exception as ex2:
                raise self._translate(ex2) from ex2

    def _execute(self, sql: str, params: dict, cap: int) -> tuple[list[dict], bool]:
        with self._session() as (conn, needs_lock):
            lock = self._lock if needs_lock else contextlib.nullcontext()
            with lock:
                cur = conn.cursor()
                try:
                    if self.dialect == "sqlserver":
                        qsql, seq = _to_qmark(sql, params)
                        cur.execute(qsql, seq)
                    else:
                        cur.execute(sql, params)
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

    def _translate(self, ex: Exception) -> DbError:
        """Driver errors -> actionable remediation."""
        if isinstance(ex, DbError):
            return ex
        msg = str(ex)
        low = msg.lower()
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
