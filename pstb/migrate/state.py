"""Migration state: one SQLite file, one row per record, plain and auditable.

The port runs for days and crosses tools the pipeline does not control (App
Designer, Data Mover), so progress lives outside any process: replan safely,
mark manual steps done as they happen, resume after a restart. Timestamps are
UTC ISO strings; via/notes/shape_diff are stored as JSON text so the file is
inspectable with any sqlite client.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .spec import STATUSES, MigrateError, PlanItem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS migrate_records (
    recname        TEXT PRIMARY KEY,
    rectype        INTEGER,
    classification TEXT,
    data_plan      TEXT,
    via            TEXT,
    notes          TEXT,
    shape_diff     TEXT,
    row_count      INTEGER,
    status         TEXT,
    status_note    TEXT,
    updated_utc    TEXT
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MigrateState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.execute(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        # Short-lived connections: the state db is low-traffic and this keeps
        # it usable from the CLI, the MCP server, and tests concurrently.
        return sqlite3.connect(str(self.path), timeout=30)

    # ---- plan ------------------------------------------------------------
    def upsert_plan(self, items: list) -> None:
        """Refresh plan columns. Existing status survives a replan unless the
        classification changed — a record that moved (say load_only ->
        drift_review because someone edited 9.2) must be re-walked."""
        with self._lock, self._conn() as c:
            for it in items:
                row = c.execute(
                    "SELECT classification, status FROM migrate_records "
                    "WHERE recname = ?", (it.recname,)).fetchone()
                status, note = "planned", ""
                if row and row[0] == it.classification and row[1]:
                    status = row[1]
                elif row and row[0] != it.classification:
                    note = (f"reclassified {row[0]} -> {it.classification}; "
                            "progress reset")
                c.execute(
                    "INSERT INTO migrate_records (recname, rectype, "
                    "classification, data_plan, via, notes, shape_diff, "
                    "row_count, status, status_note, updated_utc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(recname) DO UPDATE SET rectype=excluded.rectype, "
                    "classification=excluded.classification, "
                    "data_plan=excluded.data_plan, via=excluded.via, "
                    "notes=excluded.notes, shape_diff=excluded.shape_diff, "
                    "row_count=excluded.row_count, status=excluded.status, "
                    "status_note=excluded.status_note, "
                    "updated_utc=excluded.updated_utc",
                    (it.recname, it.rectype, it.classification, it.data_plan,
                     json.dumps(it.via), json.dumps(it.notes),
                     json.dumps(it.shape_diff), it.row_count, status, note,
                     _now()))

    # ---- progress --------------------------------------------------------
    def set_status(self, recname: str, status: str, note: str = "") -> dict:
        if status not in STATUSES:
            raise MigrateError(
                f"Unknown status {status!r}. Valid: {', '.join(STATUSES)}")
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE migrate_records SET status = ?, status_note = ?, "
                "updated_utc = ? WHERE recname = ?",
                (status, note, _now(), recname.upper()))
            if cur.rowcount == 0:
                raise MigrateError(
                    f"{recname} is not in the plan — run plan first.")
        return self.get(recname)

    def get(self, recname: str) -> dict:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM migrate_records WHERE recname = ?",
                            (recname.upper(),)).fetchone()
        if row is None:
            raise MigrateError(f"{recname} is not in the plan.")
        return self._to_dict(row)

    def all(self) -> list:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM migrate_records ORDER BY recname").fetchall()
        return [self._to_dict(r) for r in rows]

    def items(self) -> list:
        """Rehydrate PlanItems so emit/reconcile run from state, not from a
        replan — what you emit is exactly what was reviewed."""
        out = []
        for d in self.all():
            out.append(PlanItem(
                recname=d["recname"], rectype=d["rectype"],
                classification=d["classification"], data_plan=d["data_plan"],
                via=d["via"], notes=d["notes"], shape_diff=d["shape_diff"],
                row_count=d["row_count"]))
        return out

    def summary(self) -> dict:
        with self._conn() as c:
            by_class = dict(c.execute(
                "SELECT classification, COUNT(*) FROM migrate_records "
                "GROUP BY classification").fetchall())
            by_status = dict(c.execute(
                "SELECT status, COUNT(*) FROM migrate_records "
                "GROUP BY status").fetchall())
            total = c.execute(
                "SELECT COUNT(*) FROM migrate_records").fetchone()[0]
        return {"records": total, "by_classification": by_class,
                "by_status": by_status, "state_file": str(self.path)}

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in ("via", "notes", "shape_diff"):
            try:
                d[k] = json.loads(d.get(k) or "null") or ([] if k != "shape_diff" else {})
            except (TypeError, ValueError):
                d[k] = [] if k != "shape_diff" else {}
        return d
