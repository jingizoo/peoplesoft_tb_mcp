"""Trial-balance engine: turns PS_LEDGER period buckets into TB answers.

Conventions (PeopleSoft GL):
  - Amounts are signed: debits positive, credits negative.
  - Period 0 holds beginning balances written by year-end close.
  - Periods 1..12 are fiscal months; adjustment periods (e.g. 998) hold
    audit/adjusting entries and are included only on request.
  - Ending balance through period P = sum(periods 0..P) [+ adjustments].
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from . import queries as q
from .config import Config
from .db import Database, DbError

BALANCE_EPS = 0.005
INTERNAL_ROW_CAP = 100_000

_SQL_DENY = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"EXECUTE|EXEC|CALL|COMMIT|ROLLBACK|LOCK|RENAME|PRAGMA|ATTACH|VACUUM)\b",
    re.IGNORECASE,
)

JRNL_STATUS = {
    "P": "Posted",
    "V": "Valid, not posted",
    "E": "Edit errors",
    "N": "No status / not edited",
    "I": "Posting in process",
    "T": "Journal generated, not edited",
    "U": "Unposted",
    "D": "Deleted",
}


class EngineError(RuntimeError):
    pass


def r2(x: float) -> float:
    return round(float(x or 0.0), 2)


def _dr_cr(ending: float) -> tuple[float, float]:
    return (r2(ending), 0.0) if ending >= 0 else (0.0, r2(-ending))


class TBEngine:
    def __init__(self, db: Database, cfg: Config):
        self.db = db
        self.cfg = cfg
        self._setid_cache: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------ utils
    def _adj_periods(self) -> list[int]:
        return [int(x) for x in (self.cfg.defaults.adjustment_periods or [])]

    def resolve_setid(self, business_unit: str, recname: str = "GL_ACCOUNT_TBL") -> str:
        key = (business_unit, recname)
        if key not in self._setid_cache:
            try:
                rows, _ = self.db.query(
                    q.setid_for(self.db), {"bu": business_unit, "recname": recname}, max_rows=1
                )
                self._setid_cache[key] = rows[0]["setid"] if rows else self.cfg.defaults.setid
            except DbError:
                raise
            except Exception:
                self._setid_cache[key] = self.cfg.defaults.setid
        return self._setid_cache[key]

    def resolve_period(self, date: str = "") -> dict:
        """Map an ISO date (or ''/'today'/'current') to fiscal year + period."""
        d = (date or "").strip().lower()
        if d in ("", "today", "current", "now"):
            d = dt.date.today().isoformat()
        try:
            d = dt.date.fromisoformat(d[:10]).isoformat()
        except ValueError:
            raise EngineError(f"Could not parse date {date!r} — pass ISO format YYYY-MM-DD")
        params = {
            "setid": self.cfg.defaults.setid,
            "cal": self.cfg.defaults.calendar_id,
            "d": d,
        }
        rows, _ = self.db.query(q.cal_resolve(self.db), params, max_rows=1)
        if not rows:
            raise EngineError(
                f"No calendar period found for {d} "
                f"(setid={params['setid']}, calendar={params['cal']})"
            )
        r = rows[0]
        return {
            "date": d,
            "fiscal_year": int(r["fiscal_year"]),
            "period": int(r["period"]),
            "period_begin": r["begin_dt"],
            "period_end": r["end_dt"],
            "setid": params["setid"],
            "calendar_id": params["cal"],
        }

    def _bu_has_data(self, bu: str) -> bool:
        rows, _ = self.db.query(
            self.db.exists_sql(
                f"SELECT 1 FROM {self.db.prefix}PS_LEDGER WHERE BUSINESS_UNIT = :bu"
            ),
            {"bu": bu}, max_rows=1,
        )
        return bool(rows)

    def effective_defaults(self) -> dict:
        """Config defaults validated against the connected database; when the
        configured business unit or ledger has no data (typical right after
        pointing at a real instance), discover real ones with bounded indexed
        probes — never a full-ledger scan. Cached per process."""
        if getattr(self, "_eff_defaults", None):
            return self._eff_defaults
        p = self.db.prefix
        cfg_bu = self.cfg.defaults.business_unit
        cfg_led = self.cfg.defaults.ledger
        notes: list[str] = []

        bu = cfg_bu
        if not self._bu_has_data(bu):
            cands, _ = self.db.query(
                f"SELECT BUSINESS_UNIT AS bu FROM {p}PS_BUS_UNIT_TBL_GL "
                "ORDER BY BUSINESS_UNIT", {}, max_rows=50,
            )
            bu = next((c["bu"] for c in cands if self._bu_has_data(c["bu"])), cfg_bu)
            if bu != cfg_bu:
                notes.append(
                    f"configured business_unit {cfg_bu!r} has no ledger data; "
                    f"using {bu!r} discovered from the database"
                )
        leds, _ = self.db.query(q.ledgers_for_bu(self.db), {"bu": bu}, max_rows=50)
        led_names = [r["ledger"] for r in leds]
        led = cfg_led
        if led_names and cfg_led not in led_names:
            led = next((l for l in led_names if l.upper() == "ACTUALS"), None)                 or next((l for l in led_names if "ACTUAL" in l.upper()), led_names[0])
            notes.append(
                f"configured ledger {cfg_led!r} not found for {bu}; using {led!r}"
            )
        self._eff_defaults = {
            "business_unit": bu, "ledger": led, "ledgers": led_names,
            "discovered": bool(notes), "notes": notes,
        }
        return self._eff_defaults

    def last_posted_period(self, bu: str = "", ledger: str = "") -> tuple[int, int]:
        """Newest regular period with posted rows for the scope — targeted MAX
        queries on the indexed (BU, LEDGER, FY, PERIOD) path."""
        eff = self.effective_defaults()
        bu = (bu or "").strip() or eff["business_unit"]
        led = (ledger or "").strip() or eff["ledger"]
        p = self.db.prefix
        rows, _ = self.db.query(
            f"SELECT MAX(FISCAL_YEAR) AS fy FROM {p}PS_LEDGER "
            "WHERE BUSINESS_UNIT = :bu AND LEDGER = :led "
            "AND ACCOUNTING_PERIOD BETWEEN 1 AND 12",
            {"bu": bu, "led": led}, max_rows=1,
        )
        fy = int(rows[0]["fy"]) if rows and rows[0]["fy"] is not None else 0
        if not fy:
            return (0, 0)
        rows, _ = self.db.query(
            f"SELECT MAX(ACCOUNTING_PERIOD) AS p FROM {p}PS_LEDGER "
            "WHERE BUSINESS_UNIT = :bu AND LEDGER = :led AND FISCAL_YEAR = :fy "
            "AND ACCOUNTING_PERIOD BETWEEN 1 AND 12",
            {"bu": bu, "led": led, "fy": fy}, max_rows=1,
        )
        return (fy, int(rows[0]["p"]) if rows and rows[0]["p"] is not None else 12)

    def _current_fy_period(self) -> tuple[int, int]:
        try:
            r = self.resolve_period("")
            return r["fiscal_year"], r["period"]
        except EngineError:
            fy, per = self.last_posted_period()
            if fy:
                return fy, per
            return dt.date.today().year, 12

    def _defaults(
        self, business_unit: str, fiscal_year: int, period: int, ledger: str
    ) -> tuple[str, int, int, str]:
        eff = self.effective_defaults()
        bu = (business_unit or "").strip() or eff["business_unit"]
        led = (ledger or "").strip() or eff["ledger"]
        fy, per = int(fiscal_year or 0), int(period or 0)
        if fy == 0 or per == 0:
            cur_fy, cur_per = self._current_fy_period()
            if fy == 0:
                fy = cur_fy
            if per == 0:
                per = cur_per if fy == cur_fy else 12
        return bu, fy, per, led

    def _scope_diagnosis(self, bu: str, led: str, fy: int) -> dict:
        """Explain why a scope returned no ledger rows: unknown BU, unknown
        ledger, or no data for that fiscal year. Never let 'nothing found' be
        reported as a clean, balanced ledger."""
        p = self.db.prefix
        # Existence checks only. COUNT(*) here means a full scan of a real
        # PS_LEDGER (tens of millions of rows) purely to say "not found", which
        # is how a mistyped business unit turns into an apparent hang.
        rows, _ = self.db.query(
            self.db.exists_sql(f"SELECT 1 FROM {p}PS_LEDGER WHERE BUSINESS_UNIT = :bu"),
            {"bu": bu}, max_rows=1,
        )
        if not rows:
            known, _ = self.db.query(
                f"SELECT BUSINESS_UNIT AS bu FROM {p}PS_BUS_UNIT_TBL_GL ORDER BY BUSINESS_UNIT",
                {}, max_rows=25,
            )
            return {
                "scope_status": "business_unit_not_found",
                "detail": f"No ledger data exists for business unit {bu!r}.",
                "known_business_units": [r["bu"] for r in known],
            }
        rows, _ = self.db.query(
            self.db.exists_sql(
                f"SELECT 1 FROM {p}PS_LEDGER WHERE BUSINESS_UNIT = :bu AND LEDGER = :led"
            ),
            {"bu": bu, "led": led}, max_rows=1,
        )
        if not rows:
            known, _ = self.db.query(
                f"SELECT DISTINCT LEDGER AS l FROM {p}PS_LEDGER WHERE BUSINESS_UNIT = :bu",
                {"bu": bu}, max_rows=25,
            )
            return {
                "scope_status": "ledger_not_found",
                "detail": f"Business unit {bu} has no ledger named {led!r}.",
                "known_ledgers": [r["l"] for r in known],
            }
        known, _ = self.db.query(
            f"SELECT DISTINCT FISCAL_YEAR AS fy FROM {p}PS_LEDGER "
            "WHERE BUSINESS_UNIT = :bu AND LEDGER = :led ORDER BY FISCAL_YEAR",
            {"bu": bu, "led": led}, max_rows=25,
        )
        years = [int(r["fy"]) for r in known]
        return {
            "scope_status": "no_data_for_period",
            "detail": (
                f"No {led} rows for {bu} in fiscal year {fy}. "
                + (f"Retry with one of these fiscal years: {years}."
                   if years else "This ledger has no data at all.")
            ),
            "fiscal_years_with_data": years,
        }

    def base_currency_for(self, business_unit: str) -> str:
        """Base currency of a GL business unit, cached. Used to bind a literal
        currency into ledger queries instead of a column-to-column predicate."""
        key = ("__basecur__", business_unit)
        if key not in self._setid_cache:
            cur = ""
            try:
                rows, _ = self.db.query(
                    f"SELECT BASE_CURRENCY AS c FROM {self.db.prefix}PS_BUS_UNIT_TBL_GL "
                    "WHERE BUSINESS_UNIT = :bu",
                    {"bu": business_unit}, max_rows=1,
                )
                cur = (rows[0]["c"] or "").strip() if rows else ""
            except Exception:
                cur = ""
            self._setid_cache[key] = cur or (self.cfg.defaults.base_currency or "")
        return self._setid_cache[key]

    def _max_regular_period(self, fy: int) -> int:
        """Highest non-adjustment period in the calendar for this year (supports
        13-period calendars); falls back to 12."""
        adj = set(self._adj_periods())
        try:
            rows, _ = self.db.query(
                q.cal_periods(self.db),
                {
                    "setid": self.cfg.defaults.setid,
                    "cal": self.cfg.defaults.calendar_id,
                    "fy": fy,
                },
                max_rows=999,
            )
            regular = [int(r["period"]) for r in rows if int(r["period"]) not in adj]
            if regular:
                return max(regular)
        except Exception:
            pass
        return 12

    def _parse_group_by(self, group_by: str) -> list[str]:
        extras: list[str] = []
        for tok in (group_by or "").replace(";", ",").split(","):
            t = tok.strip().upper()
            if not t or t == "ACCOUNT":
                continue
            if t not in q.GROUPABLE_CHARTFIELDS:
                raise EngineError(
                    f"Cannot group by {t!r}. Allowed: {sorted(q.GROUPABLE_CHARTFIELDS)}"
                )
            if t not in extras:
                extras.append(t)
        return extras

    # -------------------------------------------------------------- core fetch
    def _period_sums(
        self,
        bu: str,
        ledger: str,
        fy: int,
        maxper: int,
        *,
        extras: Optional[list[str]] = None,
        dept: str = "",
        currency: str = "",
        account: str = "",
        include_adj: bool = False,
        amount_basis: str = "base",
    ) -> list[dict]:
        extras = extras or []
        params: dict = {
            "bu": bu,
            "ledger": ledger,
            "fy": fy,
            "maxper": maxper,
            "setid": self.resolve_setid(bu),
        }
        sql = q.tb_period_sums(
            self.db,
            extras=extras,
            include_adj=include_adj,
            adj_periods=self._adj_periods(),
            dept=dept,
            currency=currency,
            account=account,
            params=params,
            amount_basis=amount_basis,
            base_currency=self.base_currency_for(bu),
        )
        if self.cfg.db.use_views:
            params.pop("setid", None)
        rows, truncated = self.db.query(sql, params, max_rows=INTERNAL_ROW_CAP)
        if truncated:
            raise EngineError(
                "Query returned more than the internal row cap — narrow the filters"
            )
        return rows

    def _pivot(
        self,
        rows: list[dict],
        key_fields: list[str],
        period: int,
        include_adj: bool,
    ) -> dict:
        """Collapse period-level rows into beginning/activity/adjustments/ending per key."""
        adj = set(self._adj_periods())
        out: dict = {}
        for r in rows:
            key = tuple(r.get(f) for f in key_fields)
            slot = out.setdefault(
                key,
                {
                    "meta": {
                        k: r.get(k)
                        for k in ("descr", "acct_type", "eff_status", "node_ord")
                        if k in r
                    },
                    "beginning": 0.0,
                    "activity": 0.0,
                    "adjustments": 0.0,
                },
            )
            per = int(r["period"])
            amt = float(r["amt"] or 0.0)
            if per in adj and per != period:
                slot["adjustments"] += amt
            elif per < period:
                slot["beginning"] += amt
            elif per == period:
                slot["activity"] += amt
        for slot in out.values():
            slot["ending"] = slot["beginning"] + slot["activity"] + (
                slot["adjustments"] if include_adj else 0.0
            )
        return out

    # ---------------------------------------------------------------- tools
    def trial_balance(
        self,
        business_unit: str = "",
        fiscal_year: int = 0,
        period: int = 0,
        ledger: str = "",
        group_by: str = "",
        dept: str = "",
        account: str = "",
        currency: str = "",
        include_adjustments: bool = False,
        max_rows: int = 0,
    ) -> dict:
        bu, fy, per, led = self._defaults(business_unit, fiscal_year, period, ledger)
        extras = self._parse_group_by(group_by)
        if currency.lower() == "detail" and "CURRENCY_CD" not in extras:
            extras.append("CURRENCY_CD")
        rows = self._period_sums(
            bu, led, fy, per,
            extras=extras, dept=dept, currency=currency, account=account,
            include_adj=include_adjustments,
        )
        key_fields = ["account"] + [e.lower() for e in extras]
        piv = self._pivot(rows, key_fields, per, include_adjustments)

        out_rows = []
        tot_beg = tot_act = tot_adj = tot_end = tot_dr = tot_cr = 0.0
        for key in sorted(piv.keys(), key=lambda k: tuple(str(x or "") for x in k)):
            slot = piv[key]
            ending = slot["ending"]
            dr, cr = _dr_cr(ending)
            row = {"account": key[0]}
            for i, e in enumerate(extras):
                row[e.lower()] = key[i + 1]
            row.update(
                {
                    "descr": slot["meta"].get("descr"),
                    "type": slot["meta"].get("acct_type"),
                    "beginning": r2(slot["beginning"]),
                    "period_activity": r2(slot["activity"]),
                    "ending": r2(ending),
                    "ending_dr": dr,
                    "ending_cr": cr,
                }
            )
            if include_adjustments:
                row["adjustments"] = r2(slot["adjustments"])
            if slot["meta"].get("eff_status") not in ("A", None):
                row["account_status"] = slot["meta"]["eff_status"]
            out_rows.append(row)
            tot_beg += slot["beginning"]
            tot_act += slot["activity"]
            tot_adj += slot["adjustments"]
            tot_end += ending
            tot_dr += dr
            tot_cr += cr

        cap = int(max_rows or 0) or self.cfg.tools.max_rows
        truncated = len(out_rows) > cap
        result = {
            "business_unit": bu,
            "ledger": led,
            "fiscal_year": fy,
            "period": per,
            "include_adjustments": include_adjustments,
            "group_by": ["ACCOUNT"] + extras,
            "rows": out_rows[:cap],
            "row_count": len(out_rows),
            "truncated": truncated,
            "scope_status": "ok",
            "amount_basis": "base",
            "currency_filter": currency or "base currency only",
            "totals": {
                "beginning": r2(tot_beg),
                "period_activity": r2(tot_act),
                "adjustments": r2(tot_adj),
                "ending": r2(tot_end),
                "ending_dr": r2(tot_dr),
                "ending_cr": r2(tot_cr),
                "in_balance": abs(tot_end) < BALANCE_EPS,
            },
            "note": ("Amounts are signed: debits positive, credits negative. "
                     "Base-currency rows only; statistical rows excluded."),
        }
        if not out_rows:
            diag = self._scope_diagnosis(bu, led, fy)
            result.update(diag)
            result["totals"]["in_balance"] = None
            result["note"] = (
                "NO DATA — this is not a balanced trial balance. "
                + diag["detail"]
                + " Report this as 'no data found', never as a zero or balanced TB."
            )
        return result

    def account_balance(
        self,
        account: str,
        business_unit: str = "",
        fiscal_year: int = 0,
        through_period: int = 0,
        ledger: str = "",
        dept: str = "",
    ) -> dict:
        if not account:
            raise EngineError("account is required")
        bu, fy, per, led = self._defaults(business_unit, fiscal_year, through_period, ledger)
        adj = set(self._adj_periods())
        # An adjustment period is a reporting basis, not a point on the monthly
        # trend: "through 998" means through the last regular period, adjustments
        # included. Walking 1..998 would both fabricate periods and double-count.
        requested_adjustment_basis = per in adj
        max_regular = self._max_regular_period(fy)
        regular_through = max_regular if (requested_adjustment_basis or per > max_regular) else per

        rows = self._period_sums(
            bu, led, fy, max(per, regular_through), dept=dept,
            account=account.strip(), include_adj=True,
        )
        if not rows:
            diag = self._scope_diagnosis(bu, led, fy)
            raise EngineError(
                f"No ledger rows for account {account} in {bu}/{led}/FY{fy} "
                f"through period {regular_through}. {diag['detail']}"
            )
        by_per: dict[int, float] = {}
        meta = {"descr": rows[0].get("descr"), "type": rows[0].get("acct_type")}
        for r in rows:
            by_per[int(r["period"])] = by_per.get(int(r["period"]), 0.0) + float(r["amt"] or 0)
            if r.get("descr"):
                meta = {"descr": r.get("descr"), "type": r.get("acct_type")}
        beginning_of_year = by_per.get(0, 0.0)
        trend = []
        running = beginning_of_year
        for p in range(1, regular_through + 1):
            if p in adj:
                continue
            act = by_per.get(p, 0.0)
            running += act
            trend.append({"period": p, "activity": r2(act), "ending": r2(running)})
        adj_amt = sum(v for k, v in by_per.items() if k in adj)
        return {
            "account": account.strip(),
            "descr": meta["descr"],
            "type": meta["type"],
            "business_unit": bu,
            "ledger": led,
            "fiscal_year": fy,
            "through_period": regular_through,
            "requested_period": per,
            "basis": "post_adjustment" if requested_adjustment_basis else "regular_periods",
            "dept": dept or None,
            "beginning_of_year": r2(beginning_of_year),
            "periods": trend,
            "ending_through_period": r2(running),
            "adjustment_period_amount": r2(adj_amt),
            "ending_incl_adjustments": r2(running + adj_amt),
            "note": (
                "ending_through_period covers regular periods only; "
                "ending_incl_adjustments adds adjustment period(s) "
                f"{sorted(adj)} once. Do not add them together."
            ),
        }

    def compare_trial_balance(
        self,
        business_unit: str = "",
        fiscal_year: int = 0,
        period: int = 0,
        vs_fiscal_year: int = 0,
        vs_period: int = 0,
        ledger: str = "",
        dept: str = "",
        account: str = "",
        min_abs_change: float = 0.0,
        top: int = 25,
    ) -> dict:
        bu, fy, per, led = self._defaults(business_unit, fiscal_year, period, ledger)
        vfy = int(vs_fiscal_year or 0) or fy
        vper = int(vs_period or 0)
        if vper == 0:
            if vfy == fy:
                vper = per - 1 if per > 1 else per
                if per == 1:
                    vfy, vper = fy - 1, 12
            else:
                vper = per
        cur = self._pivot(
            self._period_sums(bu, led, fy, per, dept=dept, account=account),
            ["account"], per, False,
        )
        base = self._pivot(
            self._period_sums(bu, led, vfy, vper, dept=dept, account=account),
            ["account"], vper, False,
        )
        keys = set(cur) | set(base)
        rows = []
        for k in keys:
            c = cur.get(k)
            b = base.get(k)
            c_end = c["ending"] if c else 0.0
            b_end = b["ending"] if b else 0.0
            delta = c_end - b_end
            # A zero change is not a "mover". Without this, min_abs_change=0
            # lets every unchanged account through and the caller sees a long
            # list of +0.00 rows.
            if abs(delta) < max(float(min_abs_change or 0.0), BALANCE_EPS):
                continue
            meta = (c or b)["meta"]
            rows.append(
                {
                    "account": k[0],
                    "descr": meta.get("descr"),
                    "type": meta.get("acct_type"),
                    "current_ending": r2(c_end),
                    "prior_ending": r2(b_end),
                    "change": r2(delta),
                    "pct_change": r2(delta / abs(b_end) * 100) if abs(b_end) > BALANCE_EPS else None,
                    "is_new": b is None,
                    "is_gone": c is None,
                }
            )
        rows.sort(key=lambda r: abs(r["change"]), reverse=True)
        capped = rows[: max(int(top or 25), 1)]
        return {
            "business_unit": bu,
            "ledger": led,
            "current": {"fiscal_year": fy, "period": per},
            "comparison": {"fiscal_year": vfy, "period": vper},
            "movers": capped,
            "total_accounts_compared": len(keys),
            "accounts_changed": len(rows),
            "shown": len(capped),
            "total_change": r2(sum(r["change"] for r in rows)),
        }

    def drill_to_journals(
        self,
        account: str,
        period: int,
        business_unit: str = "",
        fiscal_year: int = 0,
        ledger: str = "",
        dept: str = "",
        limit: int = 100,
    ) -> dict:
        if not account:
            raise EngineError("account is required")
        if not period:
            raise EngineError("period is required for journal drill-down")
        bu, fy, per, led = self._defaults(business_unit, fiscal_year, period, ledger)
        params: dict = {
            "bu": bu, "ledger": led, "acct": account.strip(), "fy": fy, "per": per,
        }
        sql = q.journal_lines(self.db, dept, params)
        cap = max(int(limit or 100), 1)
        lines, truncated = self.db.query(sql, params, max_rows=cap)
        jrnl_total = sum(float(l["amount"] or 0) for l in lines)
        for l in lines:
            l["amount"] = r2(l["amount"])

        ledger_rows = self._period_sums(bu, led, fy, per, dept=dept, account=account.strip())
        ledger_activity = sum(
            float(r["amt"] or 0) for r in ledger_rows if int(r["period"]) == per
        )
        tie = (not truncated) and abs(jrnl_total - ledger_activity) < BALANCE_EPS
        return {
            "business_unit": bu,
            "ledger": led,
            "account": account.strip(),
            "fiscal_year": fy,
            "period": per,
            "dept": dept or None,
            "journal_lines": lines,
            "line_count": len(lines),
            "truncated": truncated,
            "journal_total": r2(jrnl_total),
            "ledger_activity": r2(ledger_activity),
            "ties_to_ledger": tie,
            "note": (
                "Journal lines tie to ledger activity."
                if tie
                else "Journal total differs from ledger activity — lines may be truncated, "
                "or activity came from sources not captured here (e.g. summarized posts)."
            ),
        }

    def search_accounts(self, query: str = "", account_type: str = "", limit: int = 50) -> dict:
        setid = self.resolve_setid(self.cfg.defaults.business_unit)
        params: dict = {
            "setid": setid,
            "qd": f"%{(query or '').upper()}%",
            "qa": f"{(query or '').strip()}%" if query else "%",
        }
        sql = q.accounts_search(self.db, account_type, params)
        rows, truncated = self.db.query(sql, params, max_rows=max(int(limit or 50), 1))
        return {"setid": setid, "accounts": rows, "count": len(rows), "truncated": truncated}

    def list_periods(self, fiscal_year: int = 0) -> dict:
        fy = int(fiscal_year or 0) or self._current_fy_period()[0]
        params = {
            "setid": self.cfg.defaults.setid,
            "cal": self.cfg.defaults.calendar_id,
            "fy": fy,
        }
        rows, _ = self.db.query(q.cal_periods(self.db), params, max_rows=20)
        return {"fiscal_year": fy, "periods": rows}

    def tb_integrity_check(
        self,
        business_unit: str = "",
        fiscal_year: int = 0,
        period: int = 0,
        ledger: str = "",
    ) -> dict:
        bu, fy, per, led = self._defaults(business_unit, fiscal_year, period, ledger)
        issues: list[str] = []

        piv = self._pivot(
            self._period_sums(bu, led, fy, per, include_adj=True), ["account"], per, True
        )
        if not piv:
            diag = self._scope_diagnosis(bu, led, fy)
            return {
                "business_unit": bu,
                "ledger": led,
                "fiscal_year": fy,
                "through_period": per,
                "control_status": "not_run",
                "balanced": None,
                "clean": None,
                "summary": f"NO DATA for this scope — checks did not run. {diag['detail']}",
                **diag,
                "issues": [
                    f"Integrity checks did not run — no ledger data in scope. {diag['detail']}"
                ],
                "note": (
                    "An empty scope is NOT a clean ledger. Tell the user no data was "
                    "found for this scope and confirm the business unit, ledger, and "
                    "fiscal year before drawing any conclusion."
                ),
            }
        total_ending = sum(s["ending"] for s in piv.values())
        balanced = abs(total_ending) < BALANCE_EPS
        if not balanced:
            issues.append(f"TB does not net to zero: {r2(total_ending)}")
        # Report the actual debit/credit totals here: "does it balance, what are
        # DR and CR" is one question, and a tool that answers half of it invites
        # the model to invent the other half.
        total_dr = sum(s["ending"] for s in piv.values() if s["ending"] > 0)
        total_cr = -sum(s["ending"] for s in piv.values() if s["ending"] < 0)

        suspense = []
        for acct in self.cfg.defaults.suspense_accounts or []:
            slot = piv.get((acct,))
            if slot and abs(slot["ending"]) > BALANCE_EPS:
                suspense.append(
                    {"account": acct, "descr": slot["meta"].get("descr"), "ending": r2(slot["ending"])}
                )
        if suspense:
            issues.append(f"{len(suspense)} suspense account(s) carry a balance")

        orphans = [
            {"account": k[0], "ending": r2(s["ending"])}
            for k, s in piv.items()
            if s["meta"].get("descr") is None
        ]
        inactive = [
            {"account": k[0], "status": s["meta"].get("eff_status"), "ending": r2(s["ending"])}
            for k, s in piv.items()
            if s["meta"].get("eff_status") not in ("A", None) and abs(s["ending"]) > BALANCE_EPS
        ]
        if orphans:
            issues.append(f"{len(orphans)} account(s) have balances but no chartfield definition")
        if inactive:
            issues.append(f"{len(inactive)} inactive account(s) carry balances")

        params = {"bu": bu, "fy": fy, "maxper": per, "ledger": led}
        unposted, _ = self.db.query(q.unposted_journals(self.db), params, max_rows=50)
        for u in unposted:
            u["status_descr"] = JRNL_STATUS.get(u.get("status"), u.get("status"))
        if unposted:
            issues.append(f"{len(unposted)} journal(s) in periods 1-{per} are not posted")

        params = {"bu": bu, "ledger": led, "fy": fy, "maxper": per}
        oob, _ = self.db.query(q.out_of_balance_journals(self.db), params, max_rows=50)
        if oob:
            issues.append(f"{len(oob)} posted journal(s) do not net to zero")

        re_roll = self._retained_earnings_roll(bu, led, fy)
        if re_roll.get("status") == "mismatch":
            issues.append("Beginning balances do not roll from prior-year close")

        return {
            "business_unit": bu,
            "ledger": led,
            "fiscal_year": fy,
            "through_period": per,
            "balanced": balanced,
            "total_ending": r2(total_ending),
            "total_debits": r2(total_dr),
            "total_credits": r2(total_cr),
            "account_count": len(piv),
            "suspense_balances": suspense,
            "accounts_missing_definition": orphans[:20],
            "inactive_accounts_with_balances": inactive[:20],
            "unposted_journals": unposted,
            "out_of_balance_journals": oob,
            "retained_earnings_roll": re_roll,
            "issues": issues,
            # "control_status" describes the exception checks, NOT whether the
            # books balance — keep those two verdicts separately named so a
            # reader (human or model) cannot conflate them.
            "control_status": "passed" if not issues else "exceptions_found",
            "clean": not issues,
            "scope_status": "ok",
            "summary": (
                f"Trial balance {'BALANCES' if balanced else 'DOES NOT BALANCE'} "
                f"(debits {r2(total_dr):,.2f} = credits {r2(total_cr):,.2f}). "
                + (f"{len(issues)} control exception(s) to review; these do not "
                   "affect whether the TB balances."
                   if issues else "No control exceptions found.")
            ),
        }

    def _retained_earnings_roll(self, bu: str, led: str, fy: int) -> dict:
        prior = self._pivot(
            self._period_sums(bu, led, fy - 1, 999, include_adj=True), ["account"], 999, True
        )
        if not prior:
            return {"status": "no_prior_year_data", "prior_fiscal_year": fy - 1}
        p0 = self._pivot(self._period_sums(bu, led, fy, 0), ["account"], 0, False)
        re_acct = self.cfg.defaults.retained_earnings_account
        ni_prior = 0.0
        mismatches = []
        for k, s in prior.items():
            t = s["meta"].get("acct_type")
            end_prior = s["ending"]
            if t in ("R", "E"):
                ni_prior += end_prior
                continue
            open_cur = p0.get(k, {}).get("ending", 0.0)
            expect = end_prior
            if k[0] == re_acct:
                continue  # RE checked separately below
            if abs(open_cur - expect) > BALANCE_EPS:
                mismatches.append(
                    {"account": k[0], "prior_ending": r2(expect), "opening": r2(open_cur)}
                )
        re_prior = prior.get((re_acct,), {}).get("ending", 0.0)
        re_open = p0.get((re_acct,), {}).get("ending", 0.0)
        re_ok = abs(re_open - (re_prior + ni_prior)) < BALANCE_EPS
        status = "ok" if (re_ok and not mismatches) else "mismatch"
        return {
            "status": status,
            "prior_fiscal_year": fy - 1,
            "prior_year_net_income": r2(-ni_prior),
            "retained_earnings_account": re_acct,
            "re_prior_ending": r2(re_prior),
            "re_opening": r2(re_open),
            "re_roll_ok": re_ok,
            "balance_sheet_mismatches": mismatches[:20],
            "note": "net income shown as positive = profit (credit)",
        }

    def rollup_trial_balance(
        self,
        business_unit: str = "",
        fiscal_year: int = 0,
        period: int = 0,
        tree_name: str = "",
        level: int = 2,
        ledger: str = "",
    ) -> dict:
        bu, fy, per, led = self._defaults(business_unit, fiscal_year, period, ledger)
        tree = (tree_name or "").strip() or self.cfg.defaults.account_tree
        setid = self.resolve_setid(bu)
        rows, _ = self.db.query(
            q.tree_effdt(self.db), {"setid": setid, "tree": tree}, max_rows=1
        )
        effdt = rows[0]["effdt"] if rows else None
        if not effdt:
            trees, _ = self.db.query(q.trees_list(self.db), {}, max_rows=50)
            names = sorted({t["tree_name"] for t in trees})
            raise EngineError(f"Tree {tree!r} not found for setid {setid}. Available: {names}")
        params = {
            "tsetid": setid,
            "tree": tree,
            "teffdt": str(effdt)[:10],
            "lvl": int(level or 2),
            "bu": bu,
            "ledger": led,
            "fy": fy,
            "maxper": per,
        }
        raw, _ = self.db.query(
            q.tree_rollup(self.db, params, base_currency=self.base_currency_for(bu)),
            params, max_rows=INTERNAL_ROW_CAP,
        )
        piv = self._pivot(raw, ["tree_node"], per, False)
        nodes = []
        order = {r["tree_node"]: r["node_ord"] for r in raw}
        for k in sorted(piv.keys(), key=lambda k: order.get(k[0], 0)):
            s = piv[k]
            dr, cr = _dr_cr(s["ending"])
            nodes.append(
                {
                    "node": k[0],
                    "beginning": r2(s["beginning"]),
                    "period_activity": r2(s["activity"]),
                    "ending": r2(s["ending"]),
                    "ending_dr": dr,
                    "ending_cr": cr,
                }
            )
        return {
            "business_unit": bu,
            "ledger": led,
            "fiscal_year": fy,
            "period": per,
            "tree": tree,
            "setid": setid,
            "tree_effdt": str(effdt)[:10],
            "level": int(level or 2),
            "nodes": nodes,
            "total_ending": r2(sum(n["ending"] for n in nodes)),
            "note": "Accounts not covered by any leaf range at this level are excluded.",
        }

    def list_trees(self) -> dict:
        rows, _ = self.db.query(q.trees_list(self.db), {}, max_rows=100)
        return {"trees": rows}

    def list_business_units(self) -> dict:
        rows, _ = self.db.query(q.business_units(self.db), {}, max_rows=200)
        return {"business_units": rows}

    def list_financial_scopes(self) -> dict:
        """Business units with base currency, their ledgers, and the fiscal
        years/periods that hold data — in one round trip.

        Two separate calls (list_business_units then list_ledgers) cannot be
        chained reliably by a model: both are emitted in the same turn, so the
        second runs before the first returns and silently falls back to the
        configured default.
        """
        p = self.db.prefix
        rows, _ = self.db.query(
            f"""SELECT L.BUSINESS_UNIT AS business_unit, L.LEDGER AS ledger,
       MIN(L.FISCAL_YEAR) AS first_fy, MAX(L.FISCAL_YEAR) AS last_fy,
       MAX(CASE WHEN L.ACCOUNTING_PERIOD BETWEEN 1 AND 12
                THEN L.FISCAL_YEAR * 100 + L.ACCOUNTING_PERIOD END) AS last_yp,
       MAX(L.BASE_CURRENCY) AS base_currency, COUNT(*) AS row_count
  FROM {p}PS_LEDGER L
 GROUP BY L.BUSINESS_UNIT, L.LEDGER
 ORDER BY L.BUSINESS_UNIT, L.LEDGER""",
            {}, max_rows=500,
        )
        descr = {}
        try:
            for b in self.list_business_units()["business_units"]:
                descr[b["business_unit"]] = b.get("descr")
        except Exception:
            pass
        scopes: dict = {}
        for r in rows:
            bu = r["business_unit"]
            s = scopes.setdefault(bu, {
                "business_unit": bu, "descr": descr.get(bu),
                "base_currency": r.get("base_currency"), "ledgers": [],
            })
            yp = int(r.get("last_yp") or 0)
            s["ledgers"].append({
                "ledger": r["ledger"],
                "fiscal_years": [int(r["first_fy"]), int(r["last_fy"])],
                "last_posted": ({"fiscal_year": yp // 100, "period": yp % 100}
                                if yp else None),
                "row_count": int(r["row_count"]),
            })
        return {
            "scopes": list(scopes.values()),
            "default": {"business_unit": self.cfg.defaults.business_unit,
                        "ledger": self.cfg.defaults.ledger},
            "note": "Use these exact values; do not invent a business unit, ledger, or year.",
        }

    def list_ledgers(self, business_unit: str = "") -> dict:
        bu = (business_unit or "").strip()
        if not bu:
            raise EngineError(
                "business_unit is required. Call list_financial_scopes to get "
                "business units and their ledgers together in one call."
            )
        rows, _ = self.db.query(q.ledgers_for_bu(self.db), {"bu": bu}, max_rows=50)
        if not rows:
            return {"business_unit": bu, "ledgers": [],
                    **self._scope_diagnosis(bu, self.cfg.defaults.ledger, 0)}
        return {"business_unit": bu, "ledgers": [r["ledger"] for r in rows]}

    # ------------------------------------------------ semantic record map / FX
    # Curated concept -> record dictionary. kind='transaction' rows carry
    # amounts and are expected to be LARGE in a live instance; kind='reference'
    # rows are setup/master data and are legitimately small — the row-count
    # sanity check applies ONLY to transaction records.
    RECORD_MAP = {
        "general_ledger": [
            ("PS_LEDGER", "transaction", "period balances by chartfield (signed)",
             "get_trial_balance / get_account_balance / run_report"),
            ("PS_JRNL_HEADER", "transaction", "journal headers", "drill_to_journals"),
            ("PS_JRNL_LN", "transaction",
             "journal lines — the record is PS_JRNL_LN, NOT PS_JRNL_LINE",
             "drill_to_journals"),
        ],
        "billing": [
            ("PS_BI_HDR", "transaction",
             "invoice headers; BILL_STATUS INV = finalized",
             "get_billing_workbench / get_top_billing_customers"),
            ("PS_BI_LINE", "transaction", "invoice lines", ""),
            ("PS_INTFC_BI", "transaction", "billing interface staging",
             "get_billing_workbench"),
        ],
        "receivables": [
            ("PS_ITEM", "transaction", "open AR items (BAL_AMT signed)",
             "get_ar_aging / get_customer_ar"),
            ("PS_ITEM_ACTIVITY", "transaction", "AR item activity", ""),
            ("PS_CUSTOMER", "reference", "customers", "search_customers"),
        ],
        "chartfields_setup": [
            ("PS_GL_ACCOUNT_TBL", "reference", "accounts (effective-dated)",
             "search_accounts"),
            ("PS_DEPT_TBL", "reference", "departments", ""),
            ("PS_BUS_UNIT_TBL_GL", "reference", "GL business units",
             "list_business_units"),
            ("PS_SET_CNTRL_REC", "reference", "setid indirection", ""),
            ("PS_CAL_DETP_TBL", "reference", "period calendar", "resolve_period"),
            ("PSTREENODE", "reference", "tree nodes", "rollup_trial_balance"),
            ("PSTREELEAF", "reference", "tree leaf ranges", "rollup_trial_balance"),
        ],
        "currency": [
            ("PS_RT_RATE_TBL", "reference",
             "exchange rates FROM_CUR->TO_CUR by RT_TYPE, effective-dated",
             "get_exchange_rate"),
        ],
    }

    def _approx_rows(self, table: str) -> Optional[int]:
        """Approximate row count without scanning: Oracle optimizer stats
        (ALL_TABLES.NUM_ROWS); exact COUNT only on SQLite (sample-sized)."""
        try:
            if self.db.dialect == "oracle":
                owner = self.cfg.db.schema.strip().rstrip(".").upper()
                if owner:
                    rows, _ = self.db.query(
                        "SELECT NUM_ROWS AS n FROM ALL_TABLES "
                        "WHERE OWNER = :o AND TABLE_NAME = :t",
                        {"o": owner, "t": table.upper()}, max_rows=1)
                else:
                    rows, _ = self.db.query(
                        "SELECT NUM_ROWS AS n FROM USER_TABLES WHERE TABLE_NAME = :t",
                        {"t": table.upper()}, max_rows=1)
                return int(rows[0]["n"]) if rows and rows[0]["n"] is not None else None
            if self.db.dialect == "sqlite":
                if not self._table_exists(table):
                    return None
                rows, _ = self.db.query(f"SELECT COUNT(*) AS n FROM {table}",
                                        {}, max_rows=1)
                return int(rows[0]["n"])
        except Exception:
            return None
        return None

    def get_record_map(self) -> dict:
        threshold = int(getattr(self.cfg.tools, "txn_row_threshold", 1000) or 1000)
        domains = {}
        for domain, recs in self.RECORD_MAP.items():
            out = []
            for name, kind, descr, tool in recs:
                exists = self._table_exists(name)
                entry = {"record": name, "kind": kind, "descr": descr,
                         "present": exists}
                if tool:
                    entry["prefer_tool"] = tool
                if exists:
                    n = self._approx_rows(name)
                    entry["approx_rows"] = n
                    if kind == "transaction":
                        if n is not None and n < threshold:
                            entry["warning"] = (
                                f"only ~{n} rows — small for a transaction "
                                "record; verify this is the table your "
                                "organization actually posts to (fine in a "
                                "demo/sample database)"
                            )
                else:
                    entry["note"] = "not present in this database"
                out.append(entry)
            domains[domain] = out
        return {
            "domains": domains,
            "txn_row_threshold": threshold,
            "note": (
                "Transaction records carry amounts and should be large in a "
                "live instance; reference records are setup/master data and "
                "are legitimately small. When a curated tool is listed under "
                "prefer_tool, use it instead of run_sql."
            ),
        }

    def exchange_rate(self, from_currency: str, to_currency: str,
                      as_of_date: str = "", rate_type: str = "",
                      amounts: str = "") -> dict:
        """Effective-dated FX from PS_RT_RATE_TBL; converts amounts SERVER-SIDE
        so the model never does the multiplication."""
        fc = (from_currency or "").strip().upper()
        tc = (to_currency or "").strip().upper()
        if not fc or not tc:
            raise EngineError("from_currency and to_currency are required")
        rt = (rate_type or "").strip().upper() or             getattr(self.cfg.defaults, "rate_type", "CRRNT")
        d = (as_of_date or "").strip() or dt.date.today().isoformat()
        try:
            d = dt.date.fromisoformat(d[:10]).isoformat()
        except ValueError:
            raise EngineError(f"Bad as_of_date {as_of_date!r} — use YYYY-MM-DD")
        p = self.db.prefix
        sql = (f"SELECT RATE_MULT AS m, RATE_DIV AS dv, EFFDT AS effdt "
               f"FROM {p}PS_RT_RATE_TBL WHERE FROM_CUR = :f AND TO_CUR = :t "
               f"AND RT_TYPE = :rt AND EFFDT = ("
               f"SELECT MAX(EFFDT) FROM {p}PS_RT_RATE_TBL "
               f"WHERE FROM_CUR = :f AND TO_CUR = :t AND RT_TYPE = :rt "
               f"AND EFFDT <= {self.db.date_bind('d')})")
        rows, _ = self.db.query(sql, {"f": fc, "t": tc, "rt": rt, "d": d},
                                max_rows=1)
        inverted = False
        cross_via = None
        if not rows:
            rows, _ = self.db.query(sql, {"f": tc, "t": fc, "rt": rt, "d": d},
                                    max_rows=1)
            inverted = True
        if rows:
            m, dv = float(rows[0]["m"] or 0), float(rows[0]["dv"] or 1) or 1.0
            rate = (dv / m) if inverted else (m / dv)
            effdt = str(rows[0]["effdt"])[:10]
        else:
            # Triangulate through the base currency (standard FX practice when
            # no direct pair is maintained): FROM->BASE then BASE->TO.
            base = (self.cfg.defaults.base_currency or "USD").upper()
            if fc != base and tc != base:
                try:
                    leg1 = self.exchange_rate(fc, base, as_of_date=d, rate_type=rt)
                    leg2 = self.exchange_rate(base, tc, as_of_date=d, rate_type=rt)
                    rate = leg1["rate"] * leg2["rate"]
                    inverted = False
                    cross_via = base
                    effdt = min(leg1["effective_date"], leg2["effective_date"])
                    rows = [True]
                except EngineError:
                    rows = []
        if not rows:
            pairs, _ = self.db.query(
                f"SELECT DISTINCT FROM_CUR AS f, TO_CUR AS t "
                f"FROM {p}PS_RT_RATE_TBL WHERE RT_TYPE = :rt",
                {"rt": rt}, max_rows=50)
            raise EngineError(
                f"No {rt} rate for {fc}->{tc} on or before {d} (direct, "
                f"inverse, or via base). Available pairs: "
                f"{[(x['f'], x['t']) for x in pairs]}"
            )
        out = {
            "from_currency": fc, "to_currency": tc, "rate_type": rt,
            "as_of": d, "effective_date": effdt,
            "rate": round(rate, 8),
            "inverted_from_reverse_pair": inverted,
            **({"cross_via": cross_via} if cross_via else {}),
            "note": f"1 {fc} = {round(rate, 6)} {tc} "
                    f"({rt}, effective {effdt}"
                    + (f", triangulated via {cross_via}" if cross_via else "")
                    + ")",
        }
        raw = [a.strip() for a in (amounts or "").split(",") if a.strip()]
        if raw:
            try:
                vals = [float(a.replace(",", "")) for a in raw]
            except ValueError as ex:
                raise EngineError(f"amounts must be numbers: {ex}")
            conv = [r2(v * rate) for v in vals]
            out["conversions"] = [
                {"amount": r2(v), "converted": c} for v, c in zip(vals, conv)
            ]
            out["converted_total"] = r2(sum(conv))
            out["conversion_note"] = (
                "Converted server-side at the quoted rate — copy these figures "
                "verbatim; do not recompute."
            )
        return out

    # ------------------------------------------------------------- raw SQL
    _TABLE_REF_RE = re.compile(
        r"(?is)\b(?:FROM|JOIN)\s+([A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*)?)"
    )
    _CTE_RE = re.compile(r"(?is)\b([A-Za-z_]\w*)\s+AS\s*\(")

    def _table_exists(self, name: str) -> bool:
        n = name.upper()
        if self.db.dialect == "sqlite":
            rows, _ = self.db.query(
                "SELECT 1 AS x FROM sqlite_master WHERE type IN ('table','view') "
                "AND UPPER(name) = :n", {"n": n}, max_rows=1)
            return bool(rows)
        if self.db.dialect == "oracle":
            owner = self.cfg.db.schema.strip().rstrip(".").upper()
            if owner:
                rows, _ = self.db.query(
                    "SELECT 1 AS x FROM ALL_OBJECTS WHERE OWNER = :o "
                    "AND OBJECT_NAME = :n AND OBJECT_TYPE IN "
                    "('TABLE','VIEW','SYNONYM','MATERIALIZED VIEW')",
                    {"o": owner, "n": n}, max_rows=1)
            else:
                rows, _ = self.db.query(
                    "SELECT 1 AS x FROM USER_OBJECTS WHERE OBJECT_NAME = :n "
                    "AND OBJECT_TYPE IN ('TABLE','VIEW','SYNONYM',"
                    "'MATERIALIZED VIEW')", {"n": n}, max_rows=1)
            return bool(rows)
        rows, _ = self.db.query(
            "SELECT 1 AS x FROM INFORMATION_SCHEMA.TABLES WHERE UPPER(TABLE_NAME) = :n",
            {"n": n}, max_rows=1)
        return bool(rows)

    def _suggest_tables(self, name: str) -> list[str]:
        """Close matches for a nonexistent table name, so 'PS_JRNL_LINE' comes
        back with 'did you mean PS_JRNL_LN'. Prefix-probes the catalog rather
        than loading it (a real PS schema has tens of thousands of objects)."""
        import difflib

        base = name.split(".")[-1].upper()
        for cut in (len(base), 12, 10, 8, 7, 6, 5, 4):
            pre = base[:cut].rstrip("_%")
            if len(pre) < 3:
                break
            params: dict = {"pat": pre + "%"}
            try:
                rows, _ = self.db.query(q.table_list(self.db, params), params,
                                        max_rows=25)
            except Exception:
                return []
            names = [str(r["table_name"]).upper() for r in rows]
            if names:
                ranked = difflib.get_close_matches(base, names, n=5, cutoff=0.0) \
                    or names[:5]
                # Among close matches, surface populated tables first — an
                # empty look-alike is rarely the record the question means.
                counts = {n: (self._approx_rows(n) or 0) for n in ranked}
                ranked.sort(key=lambda n: -counts.get(n, 0))
                return ranked
        return []

    def run_sql(self, sql: str, max_rows: int = 100) -> dict:
        if not self.cfg.tools.allow_raw_sql:
            raise EngineError("Raw SQL is disabled (tools.allow_raw_sql: false)")
        s = (sql or "").strip().rstrip(";").strip()
        if not s:
            raise EngineError("Empty SQL")
        if ";" in s:
            raise EngineError("Multiple statements are not allowed")
        scrubbed = re.sub(r"'[^']*'", "''", s)
        if not re.match(r"(?is)^\s*(SELECT|WITH)\b", scrubbed):
            raise EngineError("Only SELECT/WITH statements are allowed")
        m = _SQL_DENY.search(scrubbed)
        if m:
            raise EngineError(f"Statement rejected — contains {m.group(1).upper()}")
        # Validate every referenced table against the live catalog BEFORE
        # executing. A model-invented name (PS_JRNL_LINE for PS_JRNL_LN) should
        # come back instantly with a correction, not as an opaque ORA-00942 —
        # or worse, burn the query timeout first.
        ctes = {c.upper() for c in self._CTE_RE.findall(scrubbed)}
        problems = []
        for ref in {r for r in self._TABLE_REF_RE.findall(scrubbed)}:
            bare = ref.split(".")[-1]
            if bare.upper() in ctes or bare.upper() == "DUAL":
                continue
            if not self._table_exists(bare):
                sugg = self._suggest_tables(bare)
                problems.append(
                    f"{bare} does not exist"
                    + (f" — did you mean: {', '.join(sugg)}?" if sugg else "")
                )
        if problems:
            raise EngineError(
                "Rejected before execution: " + "; ".join(sorted(problems))
                + ". Verify names with list_tables or describe_table."
            )
        cap = min(max(int(max_rows or 100), 1), 500)
        rows, truncated = self.db.query(s, {}, max_rows=cap)
        return {"rows": rows, "row_count": len(rows), "truncated": truncated}

    def list_tables(self, pattern: str = "") -> dict:
        if not self.cfg.tools.allow_raw_sql:
            raise EngineError("Raw SQL tools are disabled")
        pat = (pattern or "").strip().upper().replace("*", "%") or "%"
        if "%" not in pat:
            pat = f"%{pat}%"
        params = {"pat": pat if self.db.dialect != "sqlite" else pat.upper()}
        if self.db.dialect == "sqlite":
            params["pat"] = pat  # sqlite LIKE is case-insensitive for ASCII
        rows, truncated = self.db.query(q.table_list(self.db, params), params, max_rows=200)
        return {"tables": rows, "count": len(rows), "truncated": truncated}

    def describe_table(self, table_name: str) -> dict:
        if not self.cfg.tools.allow_raw_sql:
            raise EngineError("Raw SQL tools are disabled")
        params: dict = {}
        try:
            sql = q.table_describe(self.db, (table_name or "").strip(), params)
        except ValueError as e:
            raise EngineError(str(e))
        rows, _ = self.db.query(sql, params, max_rows=500)
        if self.db.dialect == "sqlite":
            rows = [
                {"column_name": r["name"], "data_type": r["type"],
                 "nullable": "N" if r["notnull"] else "Y"}
                for r in rows
            ]
        if not rows:
            sugg = self._suggest_tables(table_name.strip())
            raise EngineError(
                f"Table {table_name.strip()} not found"
                + (f". Close matches: {', '.join(sugg)}" if sugg else "")
                + ". Use list_tables to browse."
            )
        return {"table": table_name.strip(), "columns": rows}
