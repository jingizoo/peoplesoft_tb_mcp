"""Metadata-led, read-only transaction and process anomaly detection.

The detector deliberately does not carry a list of ``PS_*`` tables.  Physical
objects come from the database catalog and are associated with PeopleTools
records through ``PSRECDEFN.SQLTABLENAME`` or a unique catalog suffix match.
That makes delivered records, site-prefixed tables, and company-owned tables
first-class inputs to the same analysis.

Every database statement in this module is a SELECT over a bounded date range
or a metadata catalog.  Identifiers are interpolated only after they have been
read from that catalog and validated; dates and scope values are always binds.
"""
from __future__ import annotations

import datetime as dt
import math
import re
import statistics
import threading
import time
from collections import defaultdict
from typing import Optional

from .db import DbError
from .graph import WEAK_ALONE, is_joinable_column


class AnomalyError(RuntimeError):
    pass


_IDENT = re.compile(r"^[A-Z][A-Z0-9_$#]*$")
_DATE_NAME = re.compile(r"(?:^|_)(?:DATE|DT|DTTM|DATETIME|TIMESTAMP)$")
_EVENT_WORDS = (
    "ACCOUNTING", "ACCTG", "CREATED", "CREATE", "ENTRY", "INVOICE",
    "JOURNAL", "PAYMENT", "POSTED", "PROCESS", "RECEIPT", "RECV",
    "RUN", "START", "BEGIN", "TRANSACTION", "TXN",
)
_AUDIT_WORDS = ("EFFDT", "LASTUPD", "UPDATED", "MODIFIED", "SYNC")
_START_WORDS = ("START", "BEGIN", "BEGINDTTM", "STARTDTTM", "RUNDTTM")
_END_WORDS = ("END", "FINISH", "COMPLETE", "ENDDTTM")
_DURATION_WORDS = ("DURATION", "ELAPSED", "RUN_SECONDS", "SECONDS")
_PROCESS_NAME_WORDS = (
    "PROCESS_NAME", "PRCSNAME", "PROCESS_TYPE", "JOB_NAME", "RUN_NAME",
)
_STATUS_WORDS = ("RUNSTATUS", "RUN_STATUS", "PROCESS_STATUS", "STATUS")


def _iso_date(value: str) -> dt.date:
    text = (value or "").strip()
    if not text:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(text)
    except ValueError as e:
        raise AnomalyError(
            f"as_of_date must be YYYY-MM-DD; received {value!r}") from e


def _months_before(day: dt.date, months: int) -> dt.date:
    total = day.year * 12 + day.month - 1 - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    # Beginning at the same day-of-month makes the disclosed window exactly
    # three/six calendar months.  Clamp 31st into shorter months.
    import calendar
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * fraction
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return float(ordered[lo])
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo))


def _pearson(left: list[float], right: list[float]) -> Optional[float]:
    if len(left) != len(right) or len(left) < 8:
        return None
    lm, rm = statistics.mean(left), statistics.mean(right)
    ldev, rdev = [x - lm for x in left], [x - rm for x in right]
    denom = math.sqrt(sum(x * x for x in ldev) * sum(x * x for x in rdev))
    if denom <= 0:
        return None
    return sum(a * b for a, b in zip(ldev, rdev)) / denom


def _confidence(score: float) -> str:
    return "high" if score >= 0.8 else "medium" if score >= 0.55 else "low"


def _severity(z: float, change_pct: Optional[float], zero_miss: bool = False) -> str:
    magnitude = abs(z)
    pct = abs(change_pct or 0.0)
    if zero_miss or magnitude >= 6 or pct >= 2:
        return "critical"
    if magnitude >= 4 or pct >= 1:
        return "high"
    return "medium"


class AnomalyDetector:
    """Discover and evaluate daily transaction/process signals."""

    def __init__(self, db, cfg) -> None:
        self.db = db
        self.cfg = cfg
        self.acfg = cfg.anomalies
        self._meta_lock = threading.RLock()
        self._catalog_cache = None
        self._peopletools_cache = None

    # ------------------------------------------------------------ catalog
    @staticmethod
    def _ident(value: str, label: str = "identifier") -> str:
        name = str(value or "").strip().upper()
        if not _IDENT.fullmatch(name):
            raise AnomalyError(f"Invalid {label}: {value!r}")
        return name

    @staticmethod
    def _rule_dicts(value, section: str) -> tuple[list[dict], list[dict]]:
        if value in (None, []):
            return [], []
        if not isinstance(value, list):
            return [], [{"rule": section,
                         "error": f"{section} must be a YAML list of mappings"}]
        rules, errors = [], []
        for index, raw in enumerate(value):
            if isinstance(raw, dict):
                rules.append(raw)
            else:
                errors.append({
                    "rule": f"{section}[{index}]",
                    "error": "rule must be a YAML mapping of named fields",
                })
        return rules, errors

    def _catalog_columns(self) -> tuple[dict[str, set], dict[str, Optional[int]], list]:
        """One bounded metadata read; never crawl tables looking for a prefix."""
        now = time.monotonic()
        with self._meta_lock:
            if self._catalog_cache and now < self._catalog_cache[0]:
                _, cols, approx, notes = self._catalog_cache
                return cols, approx, list(notes)
        cap = max(1000, min(int(self.acfg.catalog_column_cap or 50000), 100000))
        rows: list = []
        notes: list = []
        try:
            if self.db.dialect == "oracle":
                owner = self.cfg.db.schema.strip().rstrip(".").upper()
                if owner:
                    sql = (
                        "SELECT C.TABLE_NAME AS table_name, C.COLUMN_NAME AS column_name, "
                        "C.DATA_TYPE AS data_type, T.NUM_ROWS AS approx_rows "
                        "FROM ALL_TAB_COLUMNS C LEFT JOIN ALL_TABLES T "
                        "ON T.OWNER=C.OWNER AND T.TABLE_NAME=C.TABLE_NAME "
                        "WHERE C.OWNER=:owner ORDER BY C.TABLE_NAME, C.COLUMN_ID")
                    params = {"owner": owner}
                else:
                    sql = (
                        "SELECT C.TABLE_NAME AS table_name, C.COLUMN_NAME AS column_name, "
                        "C.DATA_TYPE AS data_type, T.NUM_ROWS AS approx_rows "
                        "FROM USER_TAB_COLUMNS C LEFT JOIN USER_TABLES T "
                        "ON T.TABLE_NAME=C.TABLE_NAME ORDER BY C.TABLE_NAME, C.COLUMN_ID")
                    params = {}
                rows, truncated = self.db.query(sql, params, max_rows=cap)
            elif self.db.dialect == "sqlserver":
                schema = self.cfg.db.schema.strip().rstrip(".")
                where = " WHERE TABLE_SCHEMA = :owner" if schema else ""
                params = {"owner": schema} if schema else {}
                rows, truncated = self.db.query(
                    "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name, "
                    "DATA_TYPE AS data_type, NULL AS approx_rows "
                    f"FROM INFORMATION_SCHEMA.COLUMNS{where} "
                    "ORDER BY TABLE_NAME, ORDINAL_POSITION", params, max_rows=cap)
            else:
                objects, obj_truncated = self.db.query(
                    "SELECT name AS table_name FROM sqlite_master "
                    "WHERE type IN ('table','view') ORDER BY name", {},
                    max_rows=max(200, int(self.acfg.catalog_object_cap or 5000)))
                truncated = obj_truncated
                for obj in objects:
                    table = self._ident(obj.get("table_name"), "catalog table")
                    cols, cut = self.db.query(f"PRAGMA table_info({table})", {},
                                              max_rows=1000)
                    truncated = truncated or cut
                    for col in cols:
                        rows.append({"table_name": table,
                                     "column_name": col.get("name"),
                                     "data_type": col.get("type"),
                                     "approx_rows": None})
                    if len(rows) >= cap:
                        truncated = True
                        rows = rows[:cap]
                        break
        except (DbError, ValueError) as e:
            raise AnomalyError(f"Database catalog could not be read: {e}") from e

        columns: dict[str, set] = defaultdict(set)
        approx: dict[str, Optional[int]] = {}
        for row in rows:
            table = str(row.get("table_name") or "").upper()
            column = str(row.get("column_name") or "").upper()
            if _IDENT.fullmatch(table) and _IDENT.fullmatch(column):
                columns[table].add(column)
                n = row.get("approx_rows")
                if n is not None:
                    try:
                        approx[table] = int(n)
                    except (TypeError, ValueError):
                        pass
        if truncated:
            notes.append(
                f"Catalog discovery hit its {cap:,}-column safety cap; automatic "
                "coverage is partial. Explicit anomaly rules are still evaluated.")
        result = (dict(columns), approx, notes)
        ttl = max(0, int(self.acfg.metadata_cache_seconds or 900))
        with self._meta_lock:
            self._catalog_cache = (now + ttl, result[0], result[1], tuple(notes))
        return result[0], result[1], list(notes)

    def _peopletools(self, objects: set) -> tuple[dict, dict, list]:
        """Record descriptions plus page/query co-use, all best effort."""
        now = time.monotonic()
        object_key = frozenset(objects)
        with self._meta_lock:
            if (self._peopletools_cache and now < self._peopletools_cache[0]
                    and object_key == self._peopletools_cache[1]):
                _, _, definitions, groups, notes = self._peopletools_cache
                return definitions, groups, list(notes)
        definitions: dict = {}
        groups: dict[str, set] = defaultdict(set)
        notes: list = []
        cap = max(1000, min(int(self.acfg.metadata_row_cap or 20000), 50000))
        try:
            rows, cut = self.db.query(
                f"SELECT RECNAME AS recname, RECDESCR AS recdescr, "
                f"SQLTABLENAME AS sqltablename FROM {self.db.prefix}PSRECDEFN "
                "WHERE RECTYPE IN (0,1,7) ORDER BY RECNAME", {}, max_rows=cap)
            if cut:
                notes.append("PSRECDEFN discovery was capped; some records were omitted.")
            for row in rows:
                rec = str(row.get("recname") or "").strip().upper()
                if not rec:
                    continue
                table, basis = self._resolve_physical(
                    rec, str(row.get("sqltablename") or ""), objects)
                if table:
                    definitions[rec] = {
                        "table": table, "descr": str(row.get("recdescr") or "").strip(),
                        "mapping_basis": basis,
                    }
        except Exception as e:  # optional metadata must not block configured rules
            notes.append(
                "PeopleTools record metadata was unavailable; discovery uses the "
                f"database catalog only ({type(e).__name__}: {e}).")

        for table, group_col, prefix in (
            ("PSQRYRECORD", "QRYNAME", "query"),
            ("PSPNLFIELD", "PNLNAME", "page"),
        ):
            try:
                rows, cut = self.db.query(
                    f"SELECT {group_col} AS group_name, RECNAME AS recname "
                    f"FROM {self.db.prefix}{table} ORDER BY {group_col}, RECNAME",
                    {}, max_rows=cap)
                if cut:
                    notes.append(f"{table} co-use evidence was capped.")
                for row in rows:
                    rec = str(row.get("recname") or "").upper()
                    physical = definitions.get(rec, {}).get("table")
                    group = str(row.get("group_name") or "").strip()
                    if physical and group:
                        groups[f"{prefix}:{group}"].add(physical)
            except Exception:
                continue
        group_result = dict(groups)
        ttl = max(0, int(self.acfg.metadata_cache_seconds or 900))
        with self._meta_lock:
            self._peopletools_cache = (
                now + ttl, object_key, definitions, group_result, tuple(notes))
        return definitions, group_result, list(notes)

    @staticmethod
    def _resolve_physical(recname: str, override: str, objects: set) -> tuple[str, str]:
        """Resolve against real objects without manufacturing a PS_ name."""
        rec, override = recname.upper(), override.strip().upper()
        if override and override in objects:
            return override, "PSRECDEFN.SQLTABLENAME"
        if rec in objects:
            return rec, "exact catalog/record-name match"
        matches = sorted(t for t in objects if t.endswith("_" + rec))
        if len(matches) == 1:
            return matches[0], "unique catalog suffix matching the PeopleTools record"
        return "", ""

    @staticmethod
    def _date_score(column: str) -> int:
        col = column.upper()
        if col in _AUDIT_WORDS or any(w in col for w in _AUDIT_WORDS):
            return -20
        date_like = bool(_DATE_NAME.search(col) or col.endswith(
            ("DATE", "DTTM", "DATETIME", "TIMESTAMP")) or col == "EFFDT")
        if not date_like:
            return 0
        score = 1
        score += sum(3 for word in _EVENT_WORDS if word in col)
        if col in ("EFFDT", "EFF_DATE", "ASOFDATE"):
            score -= 15
        return score

    def _date_column(self, columns: set, preferred: str = "") -> str:
        if preferred:
            col = self._ident(preferred, "date column")
            if col not in columns:
                raise AnomalyError(f"Configured date column {col} does not exist")
            return col
        ranked = sorted(((self._date_score(c), c) for c in columns),
                        key=lambda item: (-item[0], item[1]))
        return ranked[0][1] if ranked and ranked[0][0] > 0 else ""

    def _date_is_indexable(self, table: str, date_col: str, columns: set) -> tuple[bool, str]:
        try:
            indexes = self.db.indexes(table) or []
        except Exception:
            indexes = []
        # Automatic queries can bind only BUSINESS_UNIT.  Calling an index on
        # (SETID, EVENT_DT) usable without supplying SETID would turn the scan
        # safety check into wishful thinking.
        scope = {"BUSINESS_UNIT"}
        for idx in indexes:
            names = [str(c).upper() for c in idx.get("columns") or []]
            if date_col not in names:
                continue
            prior = names[:names.index(date_col)]
            if not prior or all(c in scope and c in columns for c in prior):
                return True, f"index {idx.get('name') or '(unnamed)'} supports the date range"
        return False, "no catalog index supports the date range"

    def _configured_tables(self, columns: dict) -> tuple[dict, list]:
        specs: dict = {}
        rules, errors = self._rule_dicts(
            self.acfg.table_rules, "anomalies.table_rules")
        relation_rules, _ = self._rule_dicts(
            self.acfg.relationship_rules, "anomalies.relationship_rules")
        rules = list(rules)
        for relation in relation_rules:
            for side in ("left", "right"):
                table = relation.get(f"{side}_table")
                if table:
                    rules.append({
                        "name": relation.get(f"{side}_name") or table,
                        "table": table,
                        "date_column": relation.get(f"{side}_date_column") or "",
                        "scope_column": relation.get(f"{side}_scope_column") or
                                        relation.get("scope_column") or "",
                        "configured_by": f"relationship rule {relation.get('name') or ''}".strip(),
                    })
        for rule in rules:
            try:
                table = self._ident(rule.get("table"), "configured table")
                if table not in columns:
                    raise AnomalyError(f"{table} is not present/readable in the catalog")
                date_col = self._date_column(columns[table], rule.get("date_column") or "")
                if not date_col:
                    raise AnomalyError(f"no event-date column could be identified on {table}")
                scope_col = str(rule.get("scope_column") or "").strip().upper()
                if scope_col:
                    scope_col = self._ident(scope_col, "scope column")
                    if scope_col not in columns[table]:
                        raise AnomalyError(f"scope column {scope_col} does not exist on {table}")
                specs[table] = {
                    "table": table, "name": rule.get("name") or table,
                    "date_column": date_col, "scope_column": scope_col,
                    "source": "configured", "confidence": 1.0,
                    "evidence": [rule.get("configured_by") or "anomalies.table_rules"],
                }
            except (AnomalyError, AttributeError) as e:
                errors.append({"rule": rule.get("name") or rule.get("table"),
                               "error": str(e)})
        return specs, errors

    def _discover_tables(self, columns: dict, approx: dict, definitions: dict,
                         groups: dict) -> tuple[dict, list]:
        if not self.acfg.infer_tables:
            return {}, []
        reverse = {d["table"]: (r, d) for r, d in definitions.items()}
        usage = defaultdict(int)
        for members in groups.values():
            for table in members:
                usage[table] += 1
        found = []
        skipped = []
        for table, cols in columns.items():
            date_col = self._date_column(cols)
            if not date_col:
                continue
            specific_keys = [c for c in cols if is_joinable_column(c)
                             and c not in WEAK_ALONE]
            txn_signals = sum(1 for c in cols if c.endswith(
                ("_AMT", "_QTY", "_ID", "_NBR", "_NO")))
            score = min(len(specific_keys), 4) * 0.08 + min(txn_signals, 5) * 0.05
            score += min(usage[table], 4) * 0.08
            if table in reverse:
                score += 0.12
            indexed, index_note = self._date_is_indexable(table, date_col, cols)
            n = approx.get(table)
            safe_small = n is not None and n <= int(self.acfg.max_unindexed_rows or 50000)
            if not indexed and not safe_small and self.db.dialect != "sqlite":
                skipped.append({
                    "table": table, "date_column": date_col,
                    "reason": index_note + " and optimizer row count is unknown/above "
                              f"{int(self.acfg.max_unindexed_rows or 50000):,}; not scanned automatically",
                })
                continue
            rec, meta = reverse.get(table, ("", {}))
            evidence = [f"event-date candidate {date_col}", index_note]
            if rec:
                evidence.append(
                    f"PeopleTools record {rec} ({meta.get('mapping_basis')})")
            if usage[table]:
                evidence.append(f"used by {usage[table]} saved-query/page group(s)")
            found.append((score, table, {
                "table": table, "name": meta.get("descr") or rec or table,
                "date_column": date_col,
                "scope_column": "BUSINESS_UNIT" if "BUSINESS_UNIT" in cols else "",
                "source": "inferred", "confidence": min(0.85, 0.35 + score),
                "evidence": evidence, "approx_rows": n,
            }))
        found.sort(key=lambda item: (-item[0], item[1]))
        cap = max(1, min(int(self.acfg.candidate_limit or 20), 50))
        return {table: spec for _, table, spec in found[:cap]}, skipped

    # -------------------------------------------------------------- counts
    def _day_expr(self, column: str) -> str:
        if self.db.dialect == "oracle":
            return f"TRUNC({column})"
        if self.db.dialect == "sqlserver":
            return f"CAST({column} AS DATE)"
        return f"DATE({column})"

    def _daily_counts(self, spec: dict, start: dt.date, end: dt.date,
                      business_unit: str) -> tuple[dict[dt.date, int], bool]:
        table, date_col = spec["table"], spec["date_column"]
        day = self._day_expr(date_col)
        where = (f"{date_col} >= {self.db.date_bind('history_start')} AND "
                 f"{date_col} < {self.db.date_bind('history_end')}")
        params = {"history_start": start.isoformat(), "history_end": end.isoformat()}
        scope_col = spec.get("scope_column") or ""
        if business_unit and scope_col:
            where += f" AND {scope_col} = :business_unit"
            params["business_unit"] = business_unit
        rows, truncated = self.db.query(
            f"SELECT {day} AS activity_date, COUNT(*) AS volume "
            f"FROM {self.db.prefix}{table} WHERE {where} "
            f"GROUP BY {day} ORDER BY {day}", params,
            max_rows=max(370, (end - start).days + 2))
        counts = {}
        for row in rows:
            try:
                key = dt.date.fromisoformat(str(row.get("activity_date"))[:10])
                counts[key] = int(row.get("volume") or 0)
            except (TypeError, ValueError):
                continue
        return counts, truncated

    def _baseline(self, counts: dict, start: dt.date, asof: dt.date) -> dict:
        days = [start + dt.timedelta(days=i) for i in range((asof - start).days)]
        all_values = [float(counts.get(day, 0)) for day in days]
        weekday_days = [d for d in days if d.weekday() == asof.weekday()]
        weekday_values = [float(counts.get(day, 0)) for day in weekday_days]
        active_all = sum(v > 0 for v in all_values)
        active_weekday = sum(v > 0 for v in weekday_values)
        min_active = max(4, int(self.acfg.min_active_days or 12))

        if (len(weekday_values) >= 8 and active_weekday >= min(6, min_active)
                and active_weekday / len(weekday_values) >= 0.4):
            cohort, method = weekday_values, "same-weekday seasonal baseline"
            confidence = min(0.95, 0.65 + len(cohort) / 100)
        elif (len(all_values) >= int(self.acfg.min_history_days or 28)
              and active_all >= min_active
              and active_all / max(len(all_values), 1) >= 0.25):
            cohort, method = all_values, "all-calendar-day baseline"
            confidence = min(0.85, 0.5 + active_all / max(len(all_values), 1) * 0.3)
        else:
            active_values = [v for v in all_values if v > 0]
            return {
                "status": "sparse_history", "method": "active-day context only",
                "calendar_days": len(all_values), "active_days": active_all,
                "active_ratio": round(active_all / max(len(all_values), 1), 3),
                "sample_days": len(active_values),
                "median": round(statistics.median(active_values), 2) if active_values else 0.0,
                "confidence": 0.25,
                "note": "Missing dates are treated as zero, but activity is too sparse "
                        "to interpret a zero today as an anomaly.",
            }

        median = statistics.median(cohort)
        mad = statistics.median([abs(v - median) for v in cohort])
        scale = max(1.0, 1.4826 * mad, math.sqrt(max(median, 1.0)))
        return {
            "status": "ready", "method": method,
            "calendar_days": len(all_values), "active_days": active_all,
            "active_ratio": round(active_all / max(len(all_values), 1), 3),
            "sample_days": len(cohort), "median": round(median, 2),
            "mean": round(statistics.mean(cohort), 2), "mad": round(mad, 2),
            "p10": round(_percentile(cohort, .1), 2),
            "p90": round(_percentile(cohort, .9), 2),
            "robust_scale": round(scale, 3), "confidence": round(confidence, 3),
        }

    def _volume_alert(self, spec: dict, today: int, baseline: dict,
                      asof: dt.date) -> Optional[dict]:
        if baseline.get("status") != "ready":
            return None
        expected = float(baseline["median"])
        diff = today - expected
        z = diff / float(baseline["robust_scale"])
        pct = diff / expected if expected else None
        material = abs(diff) >= max(float(self.acfg.material_count or 10),
                                    expected * float(self.acfg.material_pct or .5))
        if abs(z) < float(self.acfg.z_threshold or 3.5) or not material:
            return None
        direction = "above" if diff > 0 else "below"
        conf = min(float(spec.get("confidence") or .5),
                   float(baseline.get("confidence") or .5))
        return {
            "id": f"volume:{spec['table']}:{asof.isoformat()}",
            "kind": "daily_volume_deviation",
            "severity": _severity(z, pct),
            "confidence": _confidence(conf), "confidence_score": round(conf, 2),
            "subject": spec["table"],
            "observed": {"date": asof.isoformat(), "rows": today},
            "expected": {
                "rows": expected, "range_p10_p90": [baseline["p10"], baseline["p90"]],
                "baseline_method": baseline["method"],
                "historical_sample_days": baseline["sample_days"],
            },
            "deviation": {"rows": round(diff, 2), "percent": (
                round(pct * 100, 1) if pct is not None else None),
                "robust_z": round(z, 2)},
            "explanation": (
                f"{spec['table']} recorded {today:,} rows on {asof}; its "
                f"{baseline['method']} expects about {expected:,.1f}. That is "
                f"{abs(diff):,.1f} rows {direction} baseline (robust z={z:.2f})."),
        }

    # ---------------------------------------------------------- relations
    def _relation_stats(self, left: dict, right: dict, start: dt.date,
                        asof: dt.date) -> dict:
        days = [start + dt.timedelta(days=i) for i in range((asof - start).days)]
        lv, rv = [left.get(d, 0) for d in days], [right.get(d, 0) for d in days]
        left_active, right_active = sum(v > 0 for v in lv), sum(v > 0 for v in rv)
        both = sum(a > 0 and b > 0 for a, b in zip(lv, rv))
        either = sum(a > 0 or b > 0 for a, b in zip(lv, rv))
        ratios = [b / a for a, b in zip(lv, rv) if a > 0 and b > 0]
        corr = _pearson([math.log1p(v) for v in lv], [math.log1p(v) for v in rv])
        return {
            "left_active_days": left_active, "right_active_days": right_active,
            "coactive_days": both,
            "coactivity": both / max(either, 1),
            "right_given_left": both / max(left_active, 1),
            "left_given_right": both / max(right_active, 1),
            "median_right_per_left": statistics.median(ratios) if ratios else None,
            "count_correlation": corr,
        }

    def _co_use(self, left: str, right: str, groups: dict) -> list:
        return sorted(name for name, members in groups.items()
                      if left in members and right in members)[:5]

    def _inferred_relations(self, specs: dict, histories: dict, columns: dict,
                            groups: dict, start: dt.date, asof: dt.date) -> list:
        proposals = []
        names = sorted(set(specs) & set(histories))
        for i, left in enumerate(names):
            for right in names[i + 1:]:
                shared = sorted(c for c in columns[left] & columns[right]
                                if is_joinable_column(c) and c not in WEAK_ALONE)
                co_use = self._co_use(left, right, groups)
                rel = self._relation_stats(histories[left], histories[right], start, asof)
                structural = min(len(shared), 3) * .09 + min(len(co_use), 2) * .14
                observed = rel["coactivity"] * .28
                if rel["count_correlation"] is not None:
                    observed += max(0.0, rel["count_correlation"]) * .18
                score = min(.95, .18 + structural + observed)
                if not shared and not co_use:
                    continue
                if rel["coactive_days"] < int(self.acfg.min_relation_days or 8):
                    continue
                if score < float(self.acfg.min_relation_confidence or .55):
                    continue
                if rel["right_given_left"] >= .8 and rel["left_given_right"] >= .8:
                    direction = "mutual"
                elif rel["right_given_left"] >= .8:
                    direction = "left_requires_right"
                elif rel["left_given_right"] >= .8:
                    direction = "right_requires_left"
                else:
                    continue
                evidence = []
                if shared:
                    evidence.append("shared identifying columns: " + ", ".join(shared[:6]))
                if co_use:
                    evidence.append("co-used by " + ", ".join(co_use))
                evidence.append(
                    f"historically co-active on {rel['coactive_days']} days "
                    f"({rel['coactivity']:.0%} of days either was active)")
                if rel["count_correlation"] is not None:
                    evidence.append(f"log daily-count correlation {rel['count_correlation']:.2f}")
                proposals.append({
                    "name": f"inferred:{left}:{right}", "left_table": left,
                    "right_table": right, "direction": direction,
                    "source": "inferred", "confidence": round(score, 3),
                    "evidence": evidence, "historical": rel,
                })
        proposals.sort(key=lambda r: (-r["confidence"], r["name"]))
        return proposals[:max(1, min(int(self.acfg.max_inferred_relations or 20), 50))]

    def _configured_relations(self, specs: dict) -> tuple[list, list]:
        valid = []
        rules, errors = self._rule_dicts(
            self.acfg.relationship_rules, "anomalies.relationship_rules")
        for raw in rules:
            try:
                left = self._ident(raw.get("left_table"), "left table")
                right = self._ident(raw.get("right_table"), "right table")
                if left not in specs or right not in specs:
                    raise AnomalyError("both relation tables need valid daily-count specifications")
                direction = str(raw.get("direction") or "mutual").strip().lower()
                if direction not in ("mutual", "left_requires_right", "right_requires_left"):
                    raise AnomalyError(
                        "direction must be mutual, left_requires_right, or right_requires_left")
                valid.append({
                    "name": raw.get("name") or f"{left}:{right}",
                    "left_table": left, "right_table": right,
                    "direction": direction, "source": "configured",
                    "confidence": max(0.0, min(float(raw.get("confidence", 1.0)), 1.0)),
                    "minimum_trigger_count": max(1, int(raw.get(
                        "minimum_trigger_count", self.acfg.material_count or 10))),
                    "evidence": [raw.get("explanation") or
                                 "explicit anomalies.relationship_rules configuration"],
                })
            except (AnomalyError, AttributeError, TypeError, ValueError) as e:
                errors.append({"rule": raw.get("name"), "error": str(e)})
        return valid, errors

    def _relation_alert(self, rule: dict, histories: dict, start: dt.date,
                        asof: dt.date) -> Optional[dict]:
        left, right = rule["left_table"], rule["right_table"]
        lv, rv = histories[left].get(asof, 0), histories[right].get(asof, 0)
        minimum = int(rule.get("minimum_trigger_count") or self.acfg.material_count or 10)
        missing = ""
        if rule["direction"] in ("mutual", "left_requires_right") and lv >= minimum and rv == 0:
            missing = right
        elif rule["direction"] in ("mutual", "right_requires_left") and rv >= minimum and lv == 0:
            missing = left
        if not missing:
            return None
        hist = rule.get("historical") or self._relation_stats(
            histories[left], histories[right], start, asof)
        source = left if missing == right else right
        source_value = lv if source == left else rv
        source_history_days = (hist["left_active_days"] if source == left
                               else hist["right_active_days"])
        expected_prob = ((hist["right_given_left"] if missing == right
                          else hist["left_given_right"])
                         if source_history_days else None)
        confidence = (float(rule.get("confidence") or 1.0)
                      if rule.get("source") == "configured"
                      else min(float(rule.get("confidence") or .5),
                               max(.3, expected_prob or 0.0)))
        if rule.get("source") == "configured":
            explanation = (
                f"{source} recorded {source_value:,} rows on {asof}, while "
                f"configured counterpart {missing} recorded 0. The explicit "
                f"{rule['direction']} rule expects that counterpart whenever "
                f"the source reaches {minimum:,} rows.")
            if expected_prob is not None:
                explanation += (
                    f" Observed history also shows it active on "
                    f"{expected_prob:.0%} of {source}'s active days.")
        else:
            explanation = (
                f"{source} recorded {source_value:,} rows on {asof}, but inferred "
                f"counterpart {missing} recorded 0. Historically the counterpart "
                f"is active on {(expected_prob or 0):.0%} of {source}'s active days.")
        return {
            "id": f"relation:{left}:{right}:{asof.isoformat()}",
            "kind": "related_table_volume_mismatch", "severity": "critical",
            "confidence": _confidence(confidence),
            "confidence_score": round(confidence, 2),
            "subject": rule["name"],
            "observed": {"date": asof.isoformat(), left: lv, right: rv},
            "expected": {
                "relationship": rule["direction"],
                "counterpart": missing,
                "historical_counterpart_probability": (
                    round(expected_prob, 3) if expected_prob is not None else None),
                "coactive_days": hist["coactive_days"],
            },
            "evidence": rule.get("evidence") or [],
            "explanation": explanation,
        }

    # ----------------------------------------------------------- processes
    @staticmethod
    def _find_column(columns: set, preferred: str, words: tuple) -> str:
        if preferred:
            col = str(preferred).strip().upper()
            return col if col in columns else ""
        for exact in words:
            if exact in columns:
                return exact
        ranked = sorted(c for c in columns if any(w in c for w in words))
        return ranked[0] if ranked else ""

    def _process_specs(self, columns: dict, table_specs: dict,
                       allow_inferred: bool = True) -> tuple[list, list]:
        specs = []
        rules, errors = self._rule_dicts(
            self.acfg.process_rules, "anomalies.process_rules")
        configured_tables = set()
        for raw in rules:
            try:
                table = self._ident(raw.get("table"), "process table")
                configured_tables.add(table)
                if table not in columns:
                    raise AnomalyError(f"{table} is not present/readable")
                cols = columns[table]
                start_col = self._find_column(cols, raw.get("start_column") or "", _START_WORDS)
                end_col = self._find_column(cols, raw.get("end_column") or "", _END_WORDS)
                duration = self._find_column(cols, raw.get("duration_column") or "", _DURATION_WORDS)
                date_col = self._date_column(cols, raw.get("date_column") or start_col)
                status = self._find_column(cols, raw.get("status_column") or "", _STATUS_WORDS)
                name_col = self._find_column(cols, raw.get("process_name_column") or "", _PROCESS_NAME_WORDS)
                for key, resolved in (("start_column", start_col),
                                      ("end_column", end_col),
                                      ("duration_column", duration),
                                      ("status_column", status),
                                      ("process_name_column", name_col)):
                    if raw.get(key) and not resolved:
                        raise AnomalyError(
                            f"configured {key} {raw.get(key)!r} does not exist on {table}")
                if not date_col or (not duration and not (start_col and end_col) and not status):
                    raise AnomalyError("needs a date plus duration/start-end and/or status columns")
                scope_col = str(raw.get("scope_column") or (
                    "BUSINESS_UNIT" if "BUSINESS_UNIT" in cols else "")).upper()
                if scope_col:
                    scope_col = self._ident(scope_col, "process scope column")
                    if scope_col not in cols:
                        raise AnomalyError(
                            f"configured scope column {scope_col} does not exist on {table}")
                specs.append({
                    "name": raw.get("name") or table, "table": table,
                    "date_column": date_col, "start_column": start_col,
                    "end_column": end_col, "duration_column": duration,
                    "duration_unit": str(raw.get("duration_unit") or "seconds").lower(),
                    "status_column": status, "process_name_column": name_col,
                    "scope_column": scope_col,
                    "success_values": [str(v) for v in raw.get("success_values") or []],
                    "source": "configured", "confidence": 1.0,
                    "evidence": [raw.get("explanation") or
                                 "explicit anomalies.process_rules configuration"],
                })
            except (AnomalyError, AttributeError) as e:
                errors.append({"rule": raw.get("name") or raw.get("table"),
                               "error": str(e)})
        if allow_inferred and self.acfg.infer_processes:
            for table, cols in columns.items():
                if table in configured_tables:
                    continue
                start_col = self._find_column(cols, "", _START_WORDS)
                end_col = self._find_column(cols, "", _END_WORDS)
                duration = self._find_column(cols, "", _DURATION_WORDS)
                status = self._find_column(cols, "", _STATUS_WORDS)
                name_col = self._find_column(cols, "", _PROCESS_NAME_WORDS)
                date_col = self._date_column(cols, start_col)
                if not date_col or (not duration and not (start_col and end_col)):
                    continue
                if not ("PROCESS_INSTANCE" in cols or name_col):
                    continue
                # Reuse only tables already admitted by bounded transaction
                # discovery.  This prevents process inference opening a second,
                # unbounded scan path through the catalog.
                if table not in table_specs:
                    continue
                specs.append({
                    "name": table, "table": table, "date_column": date_col,
                    "start_column": start_col, "end_column": end_col,
                    "duration_column": duration, "duration_unit": "seconds",
                    "status_column": status, "process_name_column": name_col,
                    "scope_column": "BUSINESS_UNIT" if "BUSINESS_UNIT" in cols else "",
                    "success_values": [], "source": "inferred", "confidence": .65,
                    "evidence": ["catalog contains process identity plus start/end or duration fields"],
                })
        return specs[:max(1, int(self.acfg.process_candidate_limit or 8))], errors

    def _duration_expr(self, spec: dict) -> str:
        duration = spec.get("duration_column")
        if duration:
            factor = {"seconds": 1, "minutes": 60, "hours": 3600}.get(
                spec.get("duration_unit"), 1)
            return f"({duration} * {factor})"
        start, end = spec["start_column"], spec["end_column"]
        if not (start and end):
            return "NULL"
        if self.db.dialect == "oracle":
            return f"((CAST({end} AS DATE) - CAST({start} AS DATE)) * 86400)"
        if self.db.dialect == "sqlserver":
            return f"DATEDIFF(second, {start}, {end})"
        return f"((julianday({end}) - julianday({start})) * 86400)"

    def _process_rows(self, spec: dict, start: dt.date, end: dt.date,
                      business_unit: str) -> tuple[list, bool]:
        date_col, table = spec["date_column"], spec["table"]
        day = self._day_expr(date_col)
        process = spec.get("process_name_column") or ""
        status = spec.get("status_column") or ""
        process_sel = process if process else "'__TABLE__'"
        status_sel = status if status else "'__NO_STATUS__'"
        duration = self._duration_expr(spec)
        where = (f"{date_col} >= {self.db.date_bind('history_start')} AND "
                 f"{date_col} < {self.db.date_bind('history_end')}")
        params = {"history_start": start.isoformat(), "history_end": end.isoformat()}
        if business_unit and spec.get("scope_column"):
            where += f" AND {spec['scope_column']} = :business_unit"
            params["business_unit"] = business_unit
        group = [day]
        if process:
            group.append(process)
        if status:
            group.append(status)
        return self.db.query(
            f"SELECT {day} AS activity_date, {process_sel} AS process_name, "
            f"{status_sel} AS process_status, COUNT(*) AS runs, "
            f"AVG({duration}) AS avg_duration_seconds "
            f"FROM {self.db.prefix}{table} WHERE {where} "
            f"GROUP BY {', '.join(group)} ORDER BY {', '.join(group)}",
            params, max_rows=max(1000, int(self.acfg.process_result_cap or 5000)))

    def _process_alerts(self, spec: dict, rows: list, start: dt.date,
                        asof: dt.date) -> tuple[list, list]:
        by_process: dict = defaultdict(lambda: defaultdict(list))
        for row in rows:
            try:
                day = dt.date.fromisoformat(str(row.get("activity_date"))[:10])
            except ValueError:
                continue
            name = str(row.get("process_name") or "__TABLE__")
            by_process[name][day].append(row)
        alerts, summaries = [], []
        for name, day_rows in sorted(by_process.items()):
            historical = [d for d in day_rows if start <= d < asof]
            today_rows = day_rows.get(asof, [])
            if not today_rows:
                continue
            has_duration = bool(spec.get("duration_column") or
                                (spec.get("start_column") and spec.get("end_column")))
            daily_duration = {}
            for day in historical:
                total = sum(int(r.get("runs") or 0) for r in day_rows[day])
                if total:
                    daily_duration[day] = sum(
                        float(r.get("avg_duration_seconds") or 0) * int(r.get("runs") or 0)
                        for r in day_rows[day]) / total
            today_runs = sum(int(r.get("runs") or 0) for r in today_rows)
            today_duration = (sum(float(r.get("avg_duration_seconds") or 0) *
                                  int(r.get("runs") or 0) for r in today_rows) /
                              max(today_runs, 1))
            seasonal = [value for day, value in daily_duration.items()
                        if day.weekday() == asof.weekday()]
            seasonal_ready = len(seasonal) >= int(
                self.acfg.min_process_history_days or 8)
            values = seasonal if seasonal_ready else list(daily_duration.values())
            duration_method = ("same-weekday daily median" if seasonal_ready
                               else "all-run-day daily median")
            summary = {"table": spec["table"], "process": name,
                       "today_runs": today_runs,
                       "today_avg_duration_seconds": round(today_duration, 2),
                       "historical_days": len(values), "source": spec["source"]}
            if has_duration and len(values) >= int(
                    self.acfg.min_process_history_days or 8):
                median = statistics.median(values)
                mad = statistics.median(abs(v - median) for v in values)
                scale = max(1.0, 1.4826 * mad, math.sqrt(max(median, 1.0)))
                z = (today_duration - median) / scale
                pct = ((today_duration - median) / median) if median else None
                summary["baseline_avg_duration_seconds"] = round(median, 2)
                summary["duration_baseline_method"] = duration_method
                if (z >= float(self.acfg.z_threshold or 3.5)
                        and today_duration - median >= float(
                            self.acfg.min_duration_increase_seconds or 30)
                        and (pct is None or pct >= float(
                            self.acfg.process_material_pct or .5))):
                    conf = min(float(spec["confidence"]), .6 + len(values) / 50)
                    alerts.append({
                        "id": f"process-duration:{spec['table']}:{name}:{asof}",
                        "kind": "process_duration_degradation",
                        "severity": _severity(z, pct),
                        "confidence": _confidence(conf),
                        "confidence_score": round(conf, 2),
                        "subject": name if name != "__TABLE__" else spec["name"],
                        "observed": {"date": asof.isoformat(), "runs": today_runs,
                                     "avg_duration_seconds": round(today_duration, 2)},
                        "expected": {"median_duration_seconds": round(median, 2),
                                     "historical_days": len(values),
                                     "baseline_method": duration_method},
                        "deviation": {"seconds": round(today_duration - median, 2),
                                      "percent": round((pct or 0) * 100, 1),
                                      "robust_z": round(z, 2)},
                        "evidence": spec["evidence"],
                        "explanation": (
                            f"{name} averaged {today_duration:,.1f}s across {today_runs} "
                            f"run(s) on {asof}, versus a historical daily median of "
                            f"{median:,.1f}s ({duration_method}, {len(values)} days; "
                            f"robust z={z:.2f})."),
                    })

            # Configured success codes are semantic.  For inferred rules, the
            # dominant historical code is only an observed baseline and is
            # explicitly labelled as such.
            statuses = defaultdict(int)
            seasonal_status_days = [d for d in historical
                                    if d.weekday() == asof.weekday()]
            status_days = (seasonal_status_days
                           if len(seasonal_status_days) >= int(
                               self.acfg.min_process_history_days or 8)
                           else historical)
            status_method = ("same-weekday runs" if status_days is seasonal_status_days
                             else "all historical runs")
            for day in status_days:
                for row in day_rows[day]:
                    statuses[str(row.get("process_status"))] += int(row.get("runs") or 0)
            success = list(spec.get("success_values") or [])
            status_basis = "configured success code(s)"
            if not success and statuses and spec.get("status_column"):
                success = [max(statuses, key=statuses.get)]
                status_basis = "historically dominant status code (semantic meaning not inferred)"
            hist_total = sum(statuses.values())
            hist_good = sum(statuses[s] for s in success)
            today_good = sum(int(r.get("runs") or 0) for r in today_rows
                             if str(r.get("process_status")) in success)
            if spec.get("status_column") and success and hist_total >= 20 and today_runs >= int(
                    self.acfg.min_process_runs or 3):
                hist_rate, today_rate = hist_good / hist_total, today_good / today_runs
                drop = hist_rate - today_rate
                summary.update({"baseline_success_rate": round(hist_rate, 3),
                                "today_success_rate": round(today_rate, 3),
                                "success_codes": success, "status_basis": status_basis,
                                "status_baseline_method": status_method})
                if drop >= float(self.acfg.success_rate_drop or .2):
                    conf = float(spec["confidence"]) if spec["source"] == "configured" \
                        else min(float(spec["confidence"]), .65)
                    alerts.append({
                        "id": f"process-status:{spec['table']}:{name}:{asof}",
                        "kind": "process_success_rate_degradation",
                        "severity": "critical" if today_rate == 0 else "high",
                        "confidence": _confidence(conf), "confidence_score": round(conf, 2),
                        "subject": name if name != "__TABLE__" else spec["name"],
                        "observed": {"date": asof.isoformat(), "runs": today_runs,
                                     "success_rate": round(today_rate, 3)},
                        "expected": {"success_rate": round(hist_rate, 3),
                                     "historical_runs": hist_total,
                                     "status_codes": success, "basis": status_basis,
                                     "baseline_method": status_method},
                        "evidence": spec["evidence"],
                        "explanation": (
                            f"{name} met {status_basis} on {today_rate:.1%} of "
                            f"{today_runs} run(s), versus {hist_rate:.1%} across "
                            f"{hist_total} historical runs."),
                    })
            summaries.append(summary)
        return alerts, summaries

    # --------------------------------------------------------------- API
    def detect(self, as_of_date: str = "", history_months: int = 3,
               business_unit: str = "", include_inferred: bool = True) -> dict:
        if int(history_months) not in (3, 6):
            raise AnomalyError("history_months must be 3 or 6")
        asof = _iso_date(as_of_date)
        start = _months_before(asof, int(history_months))
        end = asof + dt.timedelta(days=1)
        columns, approx, notes = self._catalog_columns()
        definitions, groups, meta_notes = self._peopletools(set(columns))
        notes.extend(meta_notes)
        configured, config_errors = self._configured_tables(columns)
        if include_inferred:
            inferred, skipped = self._discover_tables(
                columns, approx, definitions, groups)
        else:
            inferred, skipped = {}, []
        specs = {**inferred, **configured}  # explicit definitions always win

        histories, table_results, incomplete = {}, [], []
        for table, spec in sorted(specs.items()):
            try:
                counts, cut = self._daily_counts(spec, start, end, business_unit)
                histories[table] = counts
                baseline = self._baseline(counts, start, asof)
                today = counts.get(asof, 0)
                entry = {
                    **spec, "today_rows": today, "baseline": baseline,
                    "history_truncated": cut,
                }
                table_results.append(entry)
                if cut:
                    incomplete.append(f"{table} daily history was truncated")
            except (DbError, AnomalyError) as e:
                incomplete.append(f"{table}: {e}")

        alerts = []
        for result in table_results:
            alert = self._volume_alert(result, result["today_rows"],
                                       result["baseline"], asof)
            if alert:
                alerts.append(alert)

        configured_rel, relation_errors = self._configured_relations(specs)
        inferred_rel = self._inferred_relations(
            specs, histories, columns, groups, start, asof) if include_inferred else []
        relations = configured_rel + inferred_rel
        for rule in relations:
            if rule["left_table"] not in histories or rule["right_table"] not in histories:
                continue
            alert = self._relation_alert(rule, histories, start, asof)
            if alert:
                alerts.append(alert)

        process_specs, process_errors = self._process_specs(
            columns, specs, allow_inferred=include_inferred)
        process_results = []
        for spec in process_specs:
            try:
                rows, cut = self._process_rows(spec, start, end, business_unit)
                found, summaries = self._process_alerts(spec, rows, start, asof)
                alerts.extend(found)
                process_results.extend(summaries)
                if cut:
                    incomplete.append(f"{spec['table']} process history was truncated")
            except (DbError, AnomalyError) as e:
                incomplete.append(f"{spec['table']} process metrics: {e}")

        configuration_errors = config_errors + relation_errors + process_errors
        if configuration_errors:
            incomplete.append(
                f"{len(configuration_errors)} anomaly configuration rule(s) are invalid")
        if skipped:
            incomplete.append(
                f"{len(skipped)} inferred table candidate(s) were not scanned for safety")
        if not specs and not process_specs:
            incomplete.append(
                "No transaction or process checks were configured or safely inferred")

        rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        alerts.sort(key=lambda a: (rank.get(a["severity"], 9), a["id"]))
        return {
            "as_of_date": asof.isoformat(), "history_months": int(history_months),
            "history_start": start.isoformat(), "history_end_exclusive": end.isoformat(),
            "business_unit": business_unit or None,
            "status": "checks_incomplete" if incomplete else "complete",
            "alert_count": len(alerts), "alerts": alerts,
            "tables_evaluated": table_results,
            "relationships_evaluated": relations,
            "processes_evaluated": process_results,
            "discovery": {
                "configured_tables": sorted(configured),
                "inferred_tables": sorted(set(specs) - set(configured)),
                "physical_name_basis": (
                    "live catalog plus PSRECDEFN.SQLTABLENAME/exact/unique-suffix "
                    "mapping; no table-name prefix is assumed"),
                "skipped_for_scan_safety": skipped,
                "notes": notes,
            },
            "configuration_errors": configuration_errors,
            "checks_incomplete": incomplete,
            "methodology": {
                "missing_dates": "represented as zero in dense daily baselines",
                "seasonality": "same weekday is preferred when it has enough observations",
                "sparse_history": "reported but not treated as a clean baseline",
                "statistics": "median/MAD robust z-score plus absolute and percentage materiality",
                "read_only": True,
            },
        }
