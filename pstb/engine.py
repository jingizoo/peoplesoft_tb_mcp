"""Trial-balance engine: turns PS_LEDGER period buckets into TB answers.

Conventions (PeopleSoft GL):
  - Amounts are signed: debits positive, credits negative.
  - Period 0 holds beginning balances written by year-end close.
  - Regular periods follow the installation's fiscal calendar (often 12,
    sometimes 13); configured adjustment periods hold audit/adjusting entries
    and are included only on request.
  - Ending balance through period P = sum(periods 0..P) [+ adjustments].
"""
from __future__ import annotations

import datetime as dt
import re
import time
from typing import Optional

from . import queries as q
from .config import Config
from .db import Database, DbError

BALANCE_EPS = 0.005
INTERNAL_ROW_CAP = 100_000
SCOPE_ROW_CAP = 5_000          # catalog reads are bounded — never a full ledger scan
SCOPE_PROBE_CAP = 250          # max existence probes when filtering the catalog
SCOPE_BATCH = 50               # pairs verified per round trip
SCOPE_CACHE_TTL_SECONDS = 900.0  # 15 min: the catalog is setup data, not balances

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
        # Set by the server: named extra databases for ad-hoc questions.
        self.registry = None
        self._source_engines: dict = {}
        self.cfg = cfg
        self._setid_cache: dict[tuple[str, str], str] = {}
        self._eff_defaults: Optional[dict] = None
        self._eff_defaults_expires_at = 0.0
        self._scope_cache_ttl_seconds = SCOPE_CACHE_TTL_SECONDS
        self._tree_ctl: dict = {}
        self._record_cols: dict = {}

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

    def _pair_has_data(self, bu: str, ledger: str) -> bool:
        """Cheap existence probe: equality on the leading index columns stops
        at the first row (ROWNUM=1 / LIMIT 1). This is what makes a
        setup-derived catalog safe to filter — unlike a DISTINCT, it never
        reads the whole ledger."""
        rows, _ = self.db.query(
            self.db.exists_sql(
                f"SELECT 1 FROM {self.db.prefix}PS_LEDGER "
                "WHERE BUSINESS_UNIT = :bu AND LEDGER = :led"
            ),
            {"bu": bu, "led": ledger}, max_rows=1,
        )
        return bool(rows)

    def _with_ledger_data(self, pairs: list) -> list:
        """Drop catalog entries that carry no ledger rows, so the UI never
        offers a scope that cannot answer. Probing is capped: past the cap the
        catalog is returned unfiltered rather than paying thousands of
        round-trips on a very large installation."""
        if len(pairs) > SCOPE_PROBE_CAP:
            return pairs
        # ONE query per batch, not one per pair. Each probe is milliseconds of
        # database work but a full network round trip; on a WAN with a few
        # hundred BU/ledger pairs the serial loop was the minute-long first
        # page load reported from the work box. A grouped query costs the
        # same index work and 2-3 round trips total.
        kept: list = []
        p = self.db.prefix
        for start in range(0, len(pairs), SCOPE_BATCH):
            chunk = pairs[start:start + SCOPE_BATCH]
            params: dict = {}
            clauses = []
            for i, (bu, ledger) in enumerate(chunk):
                params[f"b{i}"], params[f"l{i}"] = bu, ledger
                clauses.append(f"(BUSINESS_UNIT = :b{i} AND LEDGER = :l{i})")
            sql = (f"SELECT DISTINCT BUSINESS_UNIT AS bu, LEDGER AS led "
                   f"FROM {p}PS_LEDGER WHERE " + " OR ".join(clauses))
            try:
                rows, _ = self.db.query(sql, params, max_rows=SCOPE_BATCH)
                found = {(str(r["bu"]).strip(), str(r["led"]).strip())
                         for r in rows}
                kept.extend(pair for pair in chunk if pair in found)
            except DbError:
                kept.extend(chunk)      # cannot prove empty — keep them
        return kept or pairs

    def _ledger_scope_pairs(self) -> tuple[list[tuple[str, str]], bool]:
        """Accessible BU/ledger pairs, from SETUP tables wherever possible.

        Order matters for a real instance: setup tables are small and indexed;
        PS_LEDGER is not. Falling back to a DISTINCT over the balance table is
        the last resort and is capped, because on Oracle that is a full scan
        of the ledger index (SQLite hides this — it has a skip-scan).
        """
        def _collect(sql: str, cap: int) -> tuple[list[tuple[str, str]], bool]:
            rows, truncated = self.db.query(sql, {}, max_rows=cap)
            pairs: list[tuple[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for row in rows:
                bu = str(row.get("business_unit") or "").strip()
                ledger = str(row.get("ledger") or "").strip()
                if bu and ledger and (bu, ledger) not in seen:
                    pairs.append((bu, ledger))
                    seen.add((bu, ledger))
            return pairs, truncated

        try:
            pairs, truncated = _collect(q.scope_setup_pairs(self.db),
                                        SCOPE_ROW_CAP)
            if pairs:
                return self._with_ledger_data(pairs), truncated
        except DbError:
            pass  # setup records not granted at this site — try the next source

        # BU list from setup, then each BU's ledgers from PS_LEDGER filtered
        # BY THAT BU — an index range scan on the leading column, not a scan
        # of the whole table. A BU defined in setup but never posted to
        # contributes no pairs, so it is not offered as an answerable scope.
        try:
            rows, truncated = self.db.query(q.scope_bu_list(self.db), {},
                                            max_rows=SCOPE_ROW_CAP)
            bus = [str(r.get("business_unit") or "").strip() for r in rows]
            bus = [b for b in bus if b][:SCOPE_PROBE_CAP]
            pairs = []
            for bu in bus:
                led_rows, _ = self.db.query(q.ledgers_for_bu(self.db),
                                            {"bu": bu}, max_rows=200)
                for lr in led_rows:
                    led = str(lr.get("ledger") or lr.get("l") or "").strip()
                    if led:
                        pairs.append((bu, led))
            if pairs:
                return pairs, truncated
        except DbError:
            pass

        # Last resort: the balance table itself. Capped, and slow by nature.
        return _collect(q.financial_scope_pairs(self.db), SCOPE_ROW_CAP)

    def _effective_defaults_from_pairs(
        self, pairs: list[tuple[str, str]]
    ) -> dict:
        cfg_bu = (self.cfg.defaults.business_unit or "").strip()
        cfg_led = (self.cfg.defaults.ledger or "").strip()
        by_bu: dict[str, list[str]] = {}
        for bu, ledger in pairs:
            by_bu.setdefault(bu, []).append(ledger)

        notes: list[str] = []
        if not by_bu:
            return {
                "business_unit": cfg_bu,
                "ledger": cfg_led,
                "ledgers": [],
                "discovered": False,
                "notes": ["No accessible business-unit/ledger scopes were found in PS_LEDGER."],
            }

        bu = cfg_bu if cfg_bu in by_bu else next(iter(by_bu))
        if bu != cfg_bu:
            notes.append(
                f"configured business_unit {cfg_bu!r} has no ledger data; "
                f"using {bu!r} discovered from PS_LEDGER"
            )

        ledgers = by_bu[bu]
        ledger = cfg_led
        if cfg_led not in ledgers:
            ledger = (
                next((name for name in ledgers if name.upper() == "ACTUALS"), None)
                or next((name for name in ledgers if "ACTUAL" in name.upper()), None)
                or ledgers[0]
            )
            notes.append(
                f"configured ledger {cfg_led!r} not found for {bu}; using {ledger!r}"
            )
        return {
            "business_unit": bu,
            "ledger": ledger,
            "ledgers": list(ledgers),
            "discovered": bool(notes),
            "notes": notes,
        }

    def _remember_effective_defaults(self, defaults: dict) -> dict:
        self._eff_defaults = defaults
        self._eff_defaults_expires_at = (
            time.monotonic() + max(float(self._scope_cache_ttl_seconds), 0.0)
        )
        return defaults

    def invalidate_scope_cache(self) -> None:
        """Force scope/default discovery on the next request.

        The normal cache is deliberately short-lived, and this explicit hook is
        useful after a PeopleSoft grant or ledger onboarding change.
        """
        self._eff_defaults = None
        self._eff_defaults_expires_at = 0.0

    def effective_defaults(self) -> dict:
        """Config defaults validated against PS_LEDGER's accessible catalog.

        Discovery never depends on PS_BUS_UNIT_TBL_GL grants. The result has a
        short TTL rather than living for the whole process, so a newly granted
        or onboarded business unit becomes visible without a service restart.
        """
        now = time.monotonic()
        if self._eff_defaults is not None and now < self._eff_defaults_expires_at:
            return self._eff_defaults
        pairs, _ = self._ledger_scope_pairs()
        return self._remember_effective_defaults(
            self._effective_defaults_from_pairs(pairs)
        )

    def resolve_tree_ctl(self, setid: str, tree: str, business_unit: str) -> str:
        """SETCNTRLVALUE for a tree: the business unit when the tree is
        BU-controlled, otherwise the blank SetID-controlled value. Without it
        a BU-controlled tree matches every unit's copy at once and node totals
        come out multiplied — while still looking balanced."""
        key = (setid, tree, business_unit)
        if key in self._tree_ctl:
            return self._tree_ctl[key]
        try:
            rows, _ = self.db.query(q.tree_ctl_values(self.db),
                                    {"setid": setid, "tree": tree}, max_rows=50)
        except DbError:
            rows = []
        values = [str(r.get("setcntrlvalue") or "") for r in rows]
        chosen = ""
        if business_unit in values:
            chosen = business_unit
        else:
            blanks = [v for v in values if not v.strip()]
            chosen = blanks[0] if blanks else (values[0] if values else "")
        self._tree_ctl[key] = chosen
        return chosen

    def resolve_ledger_for(self, business_unit: str, ledger: str = "") -> str:
        """Resolve an omitted ledger against the selected business unit.

        A configured/effective ledger belongs to its own default BU and cannot
        safely be copied to another BU. Explicit ledger values are preserved so
        the normal no-data diagnosis can explain an invalid user selection.
        """
        requested = (ledger or "").strip()
        if requested:
            return requested
        bu = (business_unit or "").strip()
        if not bu:
            return self.effective_defaults()["ledger"]
        rows, _ = self.db.query(
            q.ledgers_for_bu(self.db), {"bu": bu}, max_rows=500
        )
        candidates: list[str] = []
        for row in rows:
            name = str(row.get("ledger") or "").strip()
            if name and name not in candidates:
                candidates.append(name)
        if candidates:
            configured = (self.cfg.defaults.ledger or "").strip()
            return (
                configured if configured in candidates
                else next(
                    (name for name in candidates if name.upper() == "ACTUALS"),
                    None,
                )
                or next(
                    (name for name in candidates if "ACTUAL" in name.upper()),
                    None,
                )
                or candidates[0]
            )
        eff = self.effective_defaults()
        if bu == eff["business_unit"]:
            return eff["ledger"]
        return (self.cfg.defaults.ledger or "").strip()

    def last_posted_period(self, bu: str = "", ledger: str = "") -> tuple[int, int]:
        """Newest regular period with posted rows for the scope — targeted MAX
        queries on the indexed (BU, LEDGER, FY, PERIOD) path."""
        bu = (bu or "").strip()
        led = (ledger or "").strip()
        if not bu:
            eff = self.effective_defaults()
            bu = eff["business_unit"]
        led = self.resolve_ledger_for(bu, led)
        rows, _ = self.db.query(
            q.scope_year_bounds(self.db, self._adj_periods()),
            {"bu": bu, "led": led}, max_rows=1,
        )
        fy = (
            int(rows[0]["last_regular_fy"])
            if rows and rows[0].get("last_regular_fy") is not None
            else 0
        )
        if not fy:
            return (0, 0)
        rows, _ = self.db.query(
            q.scope_last_regular_period(self.db, self._adj_periods()),
            {"bu": bu, "led": led, "fy": fy}, max_rows=1,
        )
        period = (
            int(rows[0]["last_period"])
            if rows and rows[0].get("last_period") is not None
            else 0
        )
        return (fy, period)

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
        # Only pay scope discovery when the caller actually omitted the scope.
        # Calling it unconditionally put a catalog query in front of EVERY
        # tool call, including the ones that named their business unit.
        bu = (business_unit or "").strip()
        if not bu:
            bu = self.effective_defaults()["business_unit"]
        led = self.resolve_ledger_for(bu, ledger)
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
                q.ledger_business_units(self.db), {}, max_rows=25,
            )
            return {
                "scope_status": "business_unit_not_found",
                "detail": f"No ledger data exists for business unit {bu!r}.",
                "known_business_units": [r["business_unit"] for r in known],
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
        bu = (business_unit or "").strip()
        key = ("__basecur__", bu)
        if key not in self._setid_cache:
            cur = ""
            try:
                rows, _ = self.db.query(
                    f"SELECT BASE_CURRENCY AS c FROM {self.db.prefix}PS_BUS_UNIT_TBL_GL "
                    "WHERE BUSINESS_UNIT = :bu",
                    {"bu": bu}, max_rows=1,
                )
                cur = (rows[0]["c"] or "").strip() if rows else ""
            except Exception:
                cur = ""
            if not cur:
                try:
                    rows, _ = self.db.query(
                        q.business_unit_base_currency(self.db),
                        {"bu": bu},
                        max_rows=1,
                    )
                    cur = (
                        str(rows[0].get("base_currency") or "").strip()
                        if rows else ""
                    )
                except Exception:
                    cur = ""
            self._setid_cache[key] = cur or (self.cfg.defaults.base_currency or "")
        return self._setid_cache[key]

    def _max_regular_period(
        self, fy: int, business_unit: str = "", ledger: str = ""
    ) -> int:
        """Highest non-adjustment period in the calendar for this year (supports
        13-period calendars); falls back to scoped ledger activity, then 12."""
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
            regular = [
                int(r["period"])
                for r in rows
                if int(r["period"]) > 0
                and int(r["period"]) != 999
                and int(r["period"]) not in adj
            ]
            if regular:
                return max(regular)
        except Exception:
            pass
        bu = (business_unit or "").strip()
        led = (ledger or "").strip()
        if bu and led:
            try:
                rows, _ = self.db.query(
                    q.scope_last_regular_period(self.db, self._adj_periods()),
                    {"bu": bu, "led": led, "fy": int(fy)},
                    max_rows=1,
                )
                if rows and rows[0].get("last_period") is not None:
                    return int(rows[0]["last_period"])
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
        basis = "regular"
        if per in self._adj_periods():
            # Asking for "period 998" means the post-adjustment year-end
            # basis, not a monthly bucket: clamp to the last regular period
            # and fold the adjustments in, exactly as account_balance does.
            # Passing 998 through as :maxper swept the adjustments in even
            # with include_adjustments=False and then labeled them as
            # activity of a nonexistent period.
            per = self._max_regular_period(fy, bu, led)
            include_adjustments = True
            basis = "post_adjustment"
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
            "basis": basis,
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
        max_regular = self._max_regular_period(fy, bu, led)
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
        # A prior-YEAR ending must be the POST-adjustment basis: the current
        # year's period-0 opening was written by year-end close and therefore
        # includes 998 adjustments. Comparing it against a pre-adjustment
        # prior ending fabricates a "mover" for every adjusted account.
        prior_year = vfy < fy
        prior_adj = prior_year and vper >= self._max_regular_period(vfy, bu, led)
        base = self._pivot(
            self._period_sums(bu, led, vfy, vper, dept=dept, account=account,
                              include_adj=prior_adj),
            ["account"], vper, prior_adj,
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

    def search_accounts(self, query: str = "", account_type: str = "",
                    limit: int = 50, business_unit: str = "") -> dict:
        setid = self.resolve_setid(
            (business_unit or "").strip()
            or self.cfg.defaults.business_unit)
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
            "tctl": self.resolve_tree_ctl(setid, tree, bu),
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

    def record_columns(self, table: str) -> set:
        """Columns a record actually has here — the shared db-level catalog."""
        return self.db.columns(table)

    # Every record the curated tools touch, with the columns they NEED versus
    # the ones they merely PREFER. One manifest, checked in one pass, so a
    # port reports every shape difference up front instead of leaking them
    # one ORA-00904 at a time as questions happen to hit them.
    RECORD_MANIFEST = {
        "PS_LEDGER": {
            "required": ["BUSINESS_UNIT", "LEDGER", "FISCAL_YEAR",
                         "ACCOUNTING_PERIOD", "ACCOUNT", "POSTED_TOTAL_AMT"],
            "optional": ["DEPTID", "CURRENCY_CD", "BASE_CURRENCY",
                         "STATISTICS_CODE", "POSTED_TRAN_AMT"],
        },
        "PS_GL_ACCOUNT_TBL": {
            "required": ["SETID", "ACCOUNT", "EFFDT"],
            "optional": ["DESCR", "ACCOUNT_TYPE", "EFF_STATUS"],
        },
        "PS_BUS_UNIT_TBL_GL": {
            "required": ["BUSINESS_UNIT"],
            "optional": ["DESCR", "BASE_CURRENCY"],
        },
        "PS_SET_CNTRL_REC": {
            "required": ["SETCNTRLVALUE", "RECNAME", "SETID"], "optional": [],
        },
        "PS_CAL_DETP_TBL": {
            "required": ["SETID", "CALENDAR_ID", "FISCAL_YEAR",
                         "ACCOUNTING_PERIOD"],
            "optional": [],
        },
        "PS_JRNL_HEADER": {
            "required": ["BUSINESS_UNIT", "JOURNAL_ID", "JOURNAL_DATE"],
            "optional": ["SOURCE", "OPRID", "JRNL_HDR_STATUS", "POSTED_DATE"],
        },
        "PS_JRNL_LN": {
            "required": ["BUSINESS_UNIT", "JOURNAL_ID", "ACCOUNT"],
            "optional": ["LINE_DESCR", "DEPTID", "MONETARY_AMOUNT"],
        },
        "PS_ITEM": {
            "required": ["BUSINESS_UNIT", "CUST_ID", "ITEM", "ITEM_STATUS",
                         "BAL_AMT"],
            "optional": ["ACCTG_DT", "ASOF_DT", "DUE_DT", "DISPUTE_STATUS",
                         "BAL_CURRENCY"],
        },
        "PS_CUSTOMER": {
            "required": ["SETID", "CUST_ID"],
            "optional": ["NAME1", "CUST_STATUS"],
        },
        "PS_BI_HDR": {
            "required": ["BUSINESS_UNIT", "INVOICE", "BILL_STATUS"],
            "optional": ["BILL_TO_CUST_ID", "INVOICE_DT", "INVOICE_AMOUNT",
                         "BILL_SOURCE_ID", "BI_CURRENCY_CD"],
        },
        "PS_INTFC_BI": {
            "required": [],
            "optional": ["INTFC_ID", "INTFC_LINE_NUM", "LOAD_STATUS_BI",
                         "BILL_TO_CUST_ID", "BILL_SOURCE_ID"],
        },
        "PS_RT_RATE_TBL": {
            "required": [],
            "optional": ["FROM_CUR", "TO_CUR", "RT_TYPE", "EFFDT", "RATE_MULT",
                         "RATE_DIV"],
        },
        "PSTREEDEFN": {
            "required": [],
            "optional": ["SETID", "SETCNTRLVALUE", "TREE_NAME", "EFFDT"],
        },
        "PSRECDEFN": {
            "required": [],
            "optional": ["RECNAME", "RECDESCR", "RECTYPE", "SQLTABLENAME"],
        },
    }

    def audit_record_shapes(self) -> dict:
        """Compare every record the tools touch against the live catalog.

        One pass, run at port time (diagnose_db) or on demand: reports
        missing REQUIRED columns (the tool cannot work), missing OPTIONAL
        columns (a feature degrades, with disclosure), and unreadable
        records (grants). This is the metadata-first contract: the shape is
        read before any curated SQL assumes it.
        """
        report = {"unreadable": [], "missing_required": {},
                  "missing_optional": {}, "clean": []}
        for table, spec in self.RECORD_MANIFEST.items():
            cols = self.db.columns(table)
            if not cols:
                report["unreadable"].append(table)
                continue
            need = [c for c in spec["required"] if c not in cols]
            nice = [c for c in spec["optional"] if c not in cols]
            if need:
                report["missing_required"][table] = need
            if nice:
                report["missing_optional"][table] = nice
            if not need and not nice:
                report["clean"].append(table)
        report["ok"] = not report["missing_required"]
        return report

    def _business_unit_enrichment(self) -> dict[str, dict]:
        """Best-effort setup metadata, never the authority for valid scopes."""
        try:
            rows, _ = self.db.query(
                q.business_units(self.db), {}, max_rows=INTERNAL_ROW_CAP
            )
        except Exception:
            return {}
        enriched: dict[str, dict] = {}
        for row in rows:
            bu = str(row.get("business_unit") or "").strip()
            if not bu:
                continue
            enriched[bu] = {
                "descr": str(row.get("descr") or "").strip() or None,
                "base_currency": (
                    str(row.get("base_currency") or "").strip() or None
                ),
            }
        return enriched

    def _ledger_scope_currency(self, bu: str, ledger: str) -> Optional[str]:
        try:
            rows, _ = self.db.query(
                q.scope_base_currency(self.db),
                {"bu": bu, "led": ledger},
                max_rows=1,
            )
        except Exception:
            return None
        if not rows:
            return None
        return str(rows[0].get("base_currency") or "").strip() or None

    def _scope_period_details(
        self, bu: str, ledger: str
    ) -> tuple[list[int], Optional[dict]]:
        rows, _ = self.db.query(
            q.scope_year_bounds(self.db, self._adj_periods()),
            {"bu": bu, "led": ledger},
            max_rows=1,
        )
        if not rows:
            return [], None
        first = rows[0].get("first_fy")
        last = rows[0].get("last_fy")
        regular = rows[0].get("last_regular_fy")
        fiscal_years = (
            [int(first), int(last)]
            if first is not None and last is not None
            else []
        )
        if regular is None:
            return fiscal_years, None
        fy = int(regular)
        rows, _ = self.db.query(
            q.scope_last_regular_period(self.db, self._adj_periods()),
            {"bu": bu, "led": ledger, "fy": fy},
            max_rows=1,
        )
        if not rows or rows[0].get("last_period") is None:
            return fiscal_years, None
        return fiscal_years, {
            "fiscal_year": fy,
            "period": int(rows[0]["last_period"]),
        }

    def list_business_units(self) -> dict:
        pairs, truncated = self._ledger_scope_pairs()
        enriched = self._business_unit_enrichment()
        first_ledger: dict[str, str] = {}
        for bu, ledger in pairs:
            first_ledger.setdefault(bu, ledger)
        rows = []
        for bu, ledger in first_ledger.items():
            meta = enriched.get(bu, {})
            rows.append({
                "business_unit": bu,
                "descr": meta.get("descr"),
                "base_currency": (
                    meta.get("base_currency")
                    or self._ledger_scope_currency(bu, ledger)
                ),
            })
        return {"business_units": rows, "truncated": truncated}

    def list_financial_scopes(self, include_activity: bool = True) -> dict:
        """Business units with base currency, their ledgers, and the fiscal
        years/periods that hold data — in one deterministic catalog response.

        Two separate calls (list_business_units then list_ledgers) cannot be
        chained reliably by a model: both are emitted in the same turn, so the
        second runs before the first returns and silently falls back to the
        configured default.

        Valid BU/ledger pairs always come from PS_LEDGER. PS_BUS_UNIT_TBL_GL is
        optional enrichment only, because read-only production users often do
        not have grants to that setup record. The default inventory is fast:
        it does not probe every scope's history. Set ``include_activity`` only
        when fiscal-year ranges and latest periods are actually required.
        """
        pairs, truncated = self._ledger_scope_pairs()
        enriched = self._business_unit_enrichment()
        scopes: dict[str, dict] = {}
        for bu, ledger in pairs:
            meta = enriched.get(bu, {})
            currency = (
                meta.get("base_currency")
                or (self._ledger_scope_currency(bu, ledger)
                    if include_activity else None)
            )
            scope = scopes.setdefault(bu, {
                "business_unit": bu,
                "descr": meta.get("descr"),
                "base_currency": currency,
                "ledgers": [],
            })
            if not scope.get("base_currency") and currency:
                scope["base_currency"] = currency
            if include_activity:
                fiscal_years, last_posted = self._scope_period_details(
                    bu, ledger
                )
            else:
                fiscal_years, last_posted = [], None
            scope["ledgers"].append({
                "ledger": ledger,
                "fiscal_years": fiscal_years,
                "last_posted": last_posted,
                # Preserve the public key while avoiding the production-scale
                # full COUNT(*) that previously made discovery appear stuck.
                "row_count": None,
            })

        effective = self._remember_effective_defaults(
            self._effective_defaults_from_pairs(pairs)
        )
        note = (
            "Use these exact values; do not invent a business unit, ledger, or year. "
            "row_count is intentionally not calculated during scope discovery. "
            + (
                "Fiscal-year and latest-period activity was included."
                if include_activity
                else "Activity detail is deferred until a scope is selected."
            )
        )
        if truncated:
            note += (
                f" The catalog exceeded the safety cap of {INTERNAL_ROW_CAP} "
                "BU/ledger pairs and is truncated."
            )
        return {
            "scopes": list(scopes.values()),
            "default": {
                "business_unit": effective["business_unit"],
                "ledger": effective["ledger"],
            },
            "note": note,
            "truncated": truncated,
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
        "payables": [
            ("PS_VOUCHER", "transaction",
             "AP voucher headers (supplier invoices); ENTRY_STATUS P=posted",
             ""),
            ("PS_VOUCHER_LINE", "transaction", "voucher lines", ""),
            ("PS_DISTRIB_LINE", "transaction",
             "voucher accounting distribution — ties AP to the GL", ""),
            ("PS_PYMNT_VCHR_XREF", "transaction",
             "payment-to-voucher cross reference: how a payment maps to the "
             "vouchers it paid", ""),
            ("PS_PAYMENT_TBL", "transaction",
             "payment headers (PYMNT_ID, PYMNT_AMT, PYMNT_DT, bank)", ""),
            ("PS_VENDOR", "reference", "suppliers/vendors", ""),
        ],
        "asset_management": [
            ("PS_ASSET", "reference", "asset master", ""),
            ("PS_COST", "transaction", "asset cost rows", ""),
            ("PS_DEPRECIATION", "transaction", "depreciation by period", ""),
        ],
        "commitment_control": [
            ("PS_KK_ACTIVITY_LOG", "transaction",
             "budget-check activity (encumbrance/pre-encumbrance)", ""),
        ],
        "projects_expenses": [
            ("PS_PROJECT", "reference", "projects", ""),
            ("PS_EX_SHEET_HDR", "transaction", "expense report headers", ""),
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
    _FUNC_FROM_HEAD = re.compile(r"(?is)\b(EXTRACT|TRIM|SUBSTRING)\s*\(")

    @staticmethod
    def _scrub_sql(s: str) -> str:
        """Blank out string literals and comments in ONE character pass,
        PRESERVING LENGTH so offsets still map onto the original statement
        (schema qualification splices at those offsets).

        Two regex passes desync on real input — an apostrophe inside a comment
        ("-- don't") swallowed the FROM clause and let FOR UPDATE past the
        deny list; '--' inside a literal would eat the rest of the line. A
        literal-aware scanner has neither failure mode."""
        out: list = []
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c == "'":
                start = i
                i += 1
                while i < n:
                    if s[i] == "'":
                        if i + 1 < n and s[i + 1] == "'":  # '' escape
                            i += 2
                            continue
                        i += 1
                        break
                    i += 1
                out.append("'" + " " * (i - start - 2) + "'" if i - start >= 2
                           else " " * (i - start))
            elif c == "-" and s[i:i + 2] == "--":
                j = s.find("\n", i)
                end = n if j < 0 else j  # newline preserved
                out.append(" " * (end - i))
                i = end
            elif c == "/" and s[i:i + 2] == "/*":
                j = s.find("*/", i + 2)
                end = n if j < 0 else j + 2
                out.append(" " * (end - i))
                i = end
            else:
                out.append(c)
                i += 1
        return "".join(out)

    @classmethod
    def _neutralize_func_from(cls, t: str) -> str:
        """ANSI functions carry a FROM that is NOT a table reference:
        EXTRACT(YEAR FROM col) / TRIM(TRAILING CHR(32) FROM col) /
        SUBSTRING(x FROM n). Rewrite the function's own FROM (the one at
        paren depth 1 relative to the function) to a comma so the table-ref
        scan does not read the COLUMN as a table (seen live: EXTRACT(YEAR
        FROM INVOICE_DT) -> "INVOICE_DT does not exist"). Depth tracking
        keeps a subquery inside TRIM(...) fully validated."""
        out = list(t)
        for m in cls._FUNC_FROM_HEAD.finditer(t):
            depth, i = 1, m.end()
            while i < len(t) and depth:
                c = t[i]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                elif (depth == 1 and t[i:i + 4].upper() == "FROM"
                      and not (i and (t[i - 1].isalnum() or t[i - 1] == "_"))
                      and not (i + 4 < len(t)
                               and (t[i + 4].isalnum() or t[i + 4] == "_"))):
                    out[i:i + 4] = list(",   ")
                    break
                i += 1
        return "".join(out)

    def _table_refs(self, scrubbed: str) -> set:
        """Names referenced as tables by FROM/JOIN (input must already be
        comment- and literal-scrubbed via _scrub_sql)."""
        return set(self._TABLE_REF_RE.findall(
            self._neutralize_func_from(scrubbed)))

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

    def for_source(self, source: str = "") -> "TBEngine":
        """Engine bound to a named source's Database (default: this one).

        Only the ad-hoc tools route through this; curated PeopleSoft tools
        always answer from the primary. A per-source engine is cheap (its
        caches start empty and belong to that source's catalog) and reuses
        the exact same guard pipeline — one code path, N databases.
        """
        name = (source or "").strip()
        if name in ("", "default"):
            return self
        if self.registry is None:
            from .db import DbError
            raise DbError("No extra sources are configured; add them under "
                          "'sources:' in config.yaml.")
        if name not in self._source_engines:
            eng = TBEngine(self.registry.get(name), self.cfg)
            self._source_engines[name] = eng
        return self._source_engines[name]

    def run_sql(self, sql: str, max_rows: int = 100,
                business_unit: str = "") -> dict:
        if not self.cfg.tools.allow_raw_sql:
            raise EngineError("Raw SQL is disabled (tools.allow_raw_sql: false)")
        s = (sql or "").strip().rstrip(";").strip()
        if not s:
            raise EngineError("Empty SQL")
        scrubbed = self._scrub_sql(s)
        if ";" in scrubbed:
            raise EngineError("Multiple statements are not allowed")
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
        problems, unqualified = [], []
        for ref in self._table_refs(scrubbed):
            bare = ref.split(".")[-1]
            if bare.upper() in ctes or bare.upper() == "DUAL":
                continue
            if not self._table_exists(bare):
                sugg = self._suggest_tables(bare)
                problems.append(
                    f"{bare} does not exist"
                    + (f" — did you mean: {', '.join(sugg)}?" if sugg else "")
                )
            elif "." not in ref:
                unqualified.append(ref)
        if problems:
            raise EngineError(
                "Rejected before execution: " + "; ".join(sorted(problems))
                + ". Verify names with list_tables or describe_table."
            )
        # Qualify bare names with the record owner. The validator resolves
        # names against db.schema (e.g. SYSADM), so executing them unqualified
        # would look them up in the LOGIN schema instead — the read-only
        # account owns nothing, so every ad-hoc query died with ORA-00942
        # right after the validator confirmed the table exists. An explicitly
        # qualified name (OTHER_OWNER.CUSTOM_TBL) is left exactly as written.
        s = self._qualify_tables(s, unqualified) if self.db.prefix else s
        cap = min(max(int(max_rows or 100), 1), 500)
        rows, truncated = self.db.query(s, {}, max_rows=cap)
        out = {"rows": rows, "row_count": len(rows), "truncated": truncated,
               "sql_executed": s}
        # Disclose, never rewrite. When a business unit is selected, say
        # plainly whether this query was restricted to it, so a cross-BU
        # result is never mistaken for a scoped one.
        bu = (business_unit or "").strip()
        if bu:
            # Match the executed text, NOT the scrubbed copy: the business
            # unit appears inside a string literal ('US001'), and scrubbing
            # blanks literals — which reported every correctly-filtered query
            # as unfiltered. Comments are stripped so a mention in prose does
            # not count as a filter.
            probe = self._scrub_sql(s)
            for lit in re.findall(r"'[^']*'", s):
                probe += " " + lit
            filtered = bool(re.search(rf"(?i)(?<![\w]){re.escape(bu)}(?![\w])",
                                      probe))
            out["scope_filtered"] = filtered
            out["scope_note"] = (
                f"Restricted to business unit {bu}."
                if filtered else
                f"NOT restricted to business unit {bu} — these rows may span "
                "business units. Add a WHERE BUSINESS_UNIT = '"
                f"{bu}' filter if the record has that column and you want "
                "only the selected unit."
            )
        return out

    def _qualify_tables(self, sql: str, names: list) -> str:
        """Prefix the given bare FROM/JOIN targets with the schema owner.

        Matching runs on a masked copy (literals/comments blanked, ANSI
        function-FROM neutralized) that is the same length as the original, so
        each match offset splices straight into the real statement. Matching
        on the raw text would rewrite `EXTRACT(YEAR FROM INVOICE_DT)` into a
        schema-qualified column.
        """
        if not names:
            return sql
        wanted = {n.upper() for n in names}
        masked = self._neutralize_func_from(self._scrub_sql(sql))
        edits = []
        for m in self._TABLE_REF_RE.finditer(masked):
            name = m.group(1)
            if "." in name or name.upper() not in wanted:
                continue
            edits.append(m.start(1))
        out = sql
        for pos in sorted(edits, reverse=True):
            out = out[:pos] + self.db.prefix + out[pos:]
        return out

    _RECTYPE = {0: "table", 1: "view", 7: "temp table"}

    def _physical_name(self, recname: str, sqltablename: str) -> str:
        """Physical object for a PeopleSoft record. A site can override the
        name in PSRECDEFN.SQLTABLENAME; otherwise it is PS_ + RECNAME."""
        override = (sqltablename or "").strip()
        return override or f"PS_{(recname or '').strip()}"

    def search_records(self, query: str = "", limit: int = 25) -> dict:
        """Find the right PeopleSoft record for a question, using PeopleTools
        metadata rather than guessing at table names.

        Searches PSRECDEFN by record name AND description (so "file
        interface" finds TU_FILE_INTFC even though the words are not in the
        table name), then PSRECFIELD by field name. Falls back to the
        database catalog when the PeopleTools tables are not granted, so this
        still returns something useful on a locked-down account.
        """
        term = (query or "").strip()
        if not term:
            raise EngineError("search_records needs something to search for")
        cap = min(max(int(limit or 25), 1), 100)
        like = f"%{term.upper()}%"
        out: list = []
        seen: set = set()
        source = "psrecdefn"
        notes: list = []

        def add(r: dict, matched_on: str) -> None:
            rec = str(r.get("recname") or "").strip()
            if not rec or rec in seen:
                return
            seen.add(rec)
            phys = self._physical_name(rec, r.get("sqltablename"))
            entry = {
                "record": rec,
                "table": phys,
                "descr": (str(r.get("recdescr") or "").strip() or None),
                "kind": self._RECTYPE.get(int(r.get("rectype") or 0), "table"),
                "matched_on": matched_on,
            }
            if r.get("fieldname"):
                entry["matched_field"] = r["fieldname"]
            rows = self._approx_rows(phys)
            if rows is not None:
                entry["approx_rows"] = rows
            out.append(entry)

        try:
            recs, _ = self.db.query(q.psrecdefn_search(self.db), {"q": like},
                                    max_rows=cap * 4)
            for r in recs:
                add(r, "record name or description")
            if len(out) < cap:
                flds, _ = self.db.query(q.psrecfield_search(self.db),
                                        {"q": like}, max_rows=cap * 4)
                for r in flds:
                    add(r, "field name")
        except DbError as e:
            # PeopleTools metadata not granted — degrade to the catalog.
            source = "database catalog"
            notes.append(
                "PeopleTools metadata (PSRECDEFN/PSRECFIELD) is not readable "
                f"by this account, so descriptions are unavailable: {e} "
                "Ask your DBA for SELECT on PSRECDEFN and PSRECFIELD to get "
                "description-based record search."
            )
            try:
                tabs = self.list_tables(term)["tables"]
            except EngineError:
                tabs = []
            for t in tabs:
                name = str(t.get("table_name") or "")
                if name and name not in seen:
                    seen.add(name)
                    entry = {"record": name, "table": name, "descr": None,
                             "kind": str(t.get("object_type") or "table"),
                             "matched_on": "table name"}
                    rows = self._approx_rows(name)
                    if rows is not None:
                        entry["approx_rows"] = rows
                    out.append(entry)

        # Populated objects first: a record with rows is the likelier answer
        # than an identically-named staging or history shell.
        out.sort(key=lambda x: (-(x.get("approx_rows") or 0), x["record"]))
        return {
            "query": term,
            "records": out[:cap],
            "count": len(out[:cap]),
            "source": source,
            "notes": notes,
            "note": (
                "Query these with run_sql using the 'table' value (the "
                "physical object). 'record' is the PeopleTools record name. "
                "approx_rows comes from optimizer statistics and may be "
                "stale or absent."
            ),
        }

    def describe_record(self, record: str) -> dict:
        """Fields of a PeopleSoft record from PeopleTools, with the physical
        column list as a cross-check."""
        rec = (record or "").strip().upper()
        if not rec:
            raise EngineError("describe_record needs a record name")
        if rec.startswith("PS_"):
            rec = rec[3:]
        out: dict = {"record": rec}
        try:
            defn, _ = self.db.query(q.psrecdefn_search(self.db),
                                    {"q": rec}, max_rows=50)
            match = next((d for d in defn
                          if str(d.get("recname") or "").upper() == rec), None)
            if match:
                out["descr"] = str(match.get("recdescr") or "").strip() or None
                out["kind"] = self._RECTYPE.get(int(match.get("rectype") or 0),
                                                "table")
                out["table"] = self._physical_name(rec, match.get("sqltablename"))
            flds, _ = self.db.query(q.psrecfield_for_record(self.db),
                                    {"rec": rec}, max_rows=500)
            if flds:
                out["fields"] = [f["fieldname"] for f in flds]
        except DbError:
            pass
        table = out.get("table") or f"PS_{rec}"
        out["table"] = table
        try:
            out["columns"] = [c["column_name"]
                              for c in self.describe_table(table)["columns"]]
        except EngineError as e:
            out["columns_error"] = str(e)
        return out

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
