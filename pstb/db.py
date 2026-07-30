"""Database access layer: SQLite (sample), Oracle (python-oracledb thin), SQL Server (pyodbc).

All queries use :name bind parameters; the SQL Server path converts them to qmark
style. Result rows come back as dicts with lowercase keys, dates as ISO strings.
"""
from __future__ import annotations

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


class Database:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dialect = cfg.db.backend.lower()
        if self.dialect not in ("sqlite", "oracle", "sqlserver"):
            raise DbError(f"Unsupported db backend: {self.dialect}")
        self._lock = threading.Lock()
        self._conn = None

    # ---- dialect helpers -------------------------------------------------
    @property
    def prefix(self) -> str:
        s = self.cfg.db.schema.strip().rstrip(".")
        return f"{s}." if s else ""

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
    def _connect(self):
        if self._conn is not None:
            return self._conn
        c = self.cfg.db
        if self.dialect == "sqlite":
            import sqlite3

            path = self.cfg.resolve_path(c.sqlite_path)
            if not path.exists():
                raise DbError(
                    f"SQLite sample database not found at {path}. "
                    "Run: python3 scripts/seed_sample_data.py"
                )
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
        elif self.dialect == "oracle":
            try:
                import oracledb
            except ImportError as e:
                raise DbError("python-oracledb not installed — pip install -e '.[oracle]'") from e
            if not (c.oracle_dsn and c.oracle_user):
                raise DbError("Set ORACLE_DSN / ORACLE_USER / ORACLE_PASSWORD in .env")
            self._conn = oracledb.connect(
                user=c.oracle_user, password=c.oracle_password, dsn=c.oracle_dsn
            )
            # Without this a slow query blocks forever and the caller just
            # appears to hang. call_timeout is milliseconds.
            timeout_s = int(getattr(c, "query_timeout_seconds", 120) or 0)
            if timeout_s > 0:
                self._conn.call_timeout = timeout_s * 1000
        else:  # sqlserver
            try:
                import pyodbc
            except ImportError as e:
                raise DbError("pyodbc not installed — pip install -e '.[sqlserver]'") from e
            if not c.mssql_conn_str:
                raise DbError("Set MSSQL_CONN_STR in .env")
            self._conn = pyodbc.connect(c.mssql_conn_str)
            timeout_s = int(getattr(c, "query_timeout_seconds", 120) or 0)
            if timeout_s > 0:
                self._conn.timeout = timeout_s
        return self._conn

    # ---- querying --------------------------------------------------------
    def query(
        self, sql: str, params: Optional[dict] = None, max_rows: Optional[int] = None
    ) -> tuple[list[dict], bool]:
        """Run a SELECT; returns (rows, truncated). Rows are lowercase-keyed dicts."""
        params = params or {}
        cap = max_rows if max_rows is not None else self.cfg.tools.max_rows
        with self._lock:
            conn = self._connect()
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
            except Exception as ex:
                msg = str(ex)
                if "DPY-4024" in msg or "call timeout" in msg.lower() or "timeout" in msg.lower():
                    raise DbError(
                        f"Query exceeded the {self.cfg.db.query_timeout_seconds}s timeout. "
                        "Narrow the scope (business unit, fiscal year, period, account "
                        "range), confirm PS_LEDGER indexes, or raise "
                        "db.query_timeout_seconds in config.yaml."
                    ) from ex
                raise
            finally:
                cur.close()
        return rows, truncated

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
