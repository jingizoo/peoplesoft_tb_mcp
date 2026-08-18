"""nVision-style report runner: tree x ledger x timespan grids.

A PS/nVision layout is, mechanically, rows defined by tree nodes or account
ranges, columns defined by (ledger, timespan), and amounts summed from the
ledger. Layout criteria live inside Excel .xnv files rather than the database,
so each layout is transcribed once into a small JSON file in reports/ — after
that the report runs from natural language ("run the income statement for
June") with the same trees, ledgers and timespans nVision would use.

Timespans here are the delivered-equivalent set (PER, YTD, BAL, YR, QTD,
Q1-Q4, ROLL12 and the -1Y variants) resolved relative to a (fiscal year,
period) context. Custom PS_TIMESPAN_DEFN timespans are not read yet — see
docs/NVISION.md.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

from . import queries as q
from .engine import TBEngine, r2

TIMESPAN_NAMES = [
    "PER", "YTD", "BAL", "YR", "QTD", "Q1", "Q2", "Q3", "Q4",
    "ROLL12", "PER-1Y", "YTD-1Y", "BAL-1Y", "YR-1Y",
]


class ReportError(RuntimeError):
    pass


def resolve_timespan(name: str, fy: int, per: int, max_regular: int = 12) -> dict:
    """Resolve a timespan name against a (fiscal year, period) context into
    single-fiscal-year segments of periods. Period 0 = balance forward."""
    n = (name or "YTD").strip().upper()

    def seg(y, ps):
        return {"fiscal_year": int(y), "periods": [int(p) for p in ps]}

    m = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", n)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if not (0 <= a <= b <= max_regular):
            raise ReportError(f"Period range {n!r} outside 0-{max_regular}")
        return {"timespan": n, "segments": [seg(fy, range(a, b + 1))],
                "adjustments_eligible": False,
                "description": f"periods {a}..{b} of FY{fy}",
                "max_regular_period": max_regular}

    if not (1 <= per <= max_regular):
        raise ReportError(
            f"Period {per} is outside 1-{max_regular}. For a post-adjustment "
            "year-end basis, run the report at the last regular period with "
            "include_adjustments=true."
        )
    q_start = ((per - 1) // 3) * 3 + 1
    table: dict[str, tuple[list, bool, str]] = {
        "PER": ([seg(fy, [per])], False, "activity for the period"),
        "YTD": ([seg(fy, range(1, per + 1))], True, "year-to-date activity"),
        "BAL": ([seg(fy, [0, *range(1, per + 1)])], True,
                "balance including balance forward (period 0)"),
        "YR": ([seg(fy, range(1, max_regular + 1))], True, "full-year activity"),
        "QTD": ([seg(fy, range(q_start, per + 1))], False, "quarter to date"),
        "PER-1Y": ([seg(fy - 1, [per])], False, "same period, prior year"),
        "YTD-1Y": ([seg(fy - 1, range(1, per + 1))], True, "prior-year YTD"),
        "BAL-1Y": ([seg(fy - 1, [0, *range(1, per + 1)])], True,
                   "prior-year balance"),
        "YR-1Y": ([seg(fy - 1, range(1, max_regular + 1))], True,
                  "prior full year"),
    }
    for i in (1, 2, 3, 4):
        lo = 3 * i - 2
        table[f"Q{i}"] = ([seg(fy, range(lo, min(lo + 2, max_regular) + 1))],
                          False, f"fiscal quarter {i}")
    if n == "ROLL12":
        segs = [seg(fy, range(1, per + 1))]
        if per < max_regular:
            segs.insert(0, seg(fy - 1, range(per + 1, max_regular + 1)))
        table["ROLL12"] = (segs, False, "trailing 12 periods")

    if n not in table:
        raise ReportError(
            f"Unknown timespan {n!r}. Available: {TIMESPAN_NAMES} "
            "or a period range like '4-6'."
        )
    segs, adj_ok, desc = table[n]
    return {"timespan": n, "segments": segs, "adjustments_eligible": adj_ok,
            "description": desc, "max_regular_period": max_regular}


def _account_matcher(spec: str) -> Callable[[str], bool]:
    a = (spec or "").strip()
    if not a:
        raise ReportError("Empty account selector")
    if re.fullmatch(r"[\w]+\s*-\s*[\w]+", a):
        lo, hi = [x.strip() for x in a.split("-", 1)]
        return lambda s: lo <= s <= hi
    if "," in a:
        vals = {x.strip() for x in a.split(",") if x.strip()}
        return lambda s: s in vals
    if a.endswith("%"):
        pre = a[:-1]
        return lambda s: s.startswith(pre)
    return lambda s: s == a


class ReportRunner:
    def __init__(self, engine: TBEngine):
        self.e = engine
        self.cfg = engine.cfg
        self.db = engine.db
        self._range_cache: dict[tuple, list] = {}

    # ------------------------------------------------------------ definitions
    def _reports_dir(self):
        return self.cfg.resolve_path(self.cfg.tools.reports_path)

    def _definitions(self) -> dict:
        out: dict = {}
        d = self._reports_dir()
        if d.exists():
            for f in sorted(d.glob("*.json")):
                try:
                    spec = json.loads(f.read_text())
                except (OSError, ValueError) as e:
                    raise ReportError(f"{f.name}: invalid report definition: {e}")
                out[spec.get("name") or f.stem] = spec
        return out

    def list_reports(self) -> dict:
        defs = self._definitions()
        return {
            "reports": [
                {
                    "name": k,
                    "title": v.get("title", k),
                    "description": v.get("description", ""),
                    "columns": [c.get("label", "?") for c in v.get("columns", [])],
                    "row_count": len(v.get("rows", [])),
                }
                for k, v in sorted(defs.items())
            ],
            "timespans": TIMESPAN_NAMES + ["<a-b> period range"],
            "note": "Run with run_report(report=<name>); ad-hoc rows/columns also supported.",
        }

    # ----------------------------------------------------------- tree lookups
    def node_accounts(self, business_unit: str = "", tree_name: str = "",
                      node: str = "") -> dict:
        """Flatten a tree node into its concrete ACCOUNT LIST — the set
        producer for chains.

        Trees attach RANGES to nodes, not accounts, so "the accounts in
        node X" genuinely cannot be answered by a join — the ranges must be
        resolved against the account master. For a trial balance BY node,
        rollup_trial_balance already does all of this in one call; this tool
        exists for every OTHER use of that account set: journals for these
        accounts, budget rows for them, an ad-hoc run_sql — chained by
        reference, never retyped.
        """
        bu = (business_unit or "").strip() or self.e.cfg.defaults.business_unit
        tree = (tree_name or "").strip() or self.e.cfg.defaults.account_tree
        if not (node or "").strip():
            raise ReportError("node_accounts needs a tree node name — "
                              "list_trees shows the trees; an unknown node "
                              "error lists the nodes.")
        ranges = self._node_ranges(bu, tree, node.strip())
        setid = self.e.resolve_setid(bu)
        p = self.db.prefix
        clauses, params = [], {"setid": setid}
        for i, (lo, hi) in enumerate(ranges[:500]):
            params[f"lo{i}"], params[f"hi{i}"] = lo, hi
            clauses.append(f"(A.ACCOUNT BETWEEN :lo{i} AND :hi{i})")
        rows, _ = self.db.query(
            f"""SELECT DISTINCT A.ACCOUNT AS account,
       {q.opt(self.db, 'PS_GL_ACCOUNT_TBL', 'DESCR', 'descr', 'A')}
  FROM {p}PS_GL_ACCOUNT_TBL A
 WHERE A.SETID = :setid AND ({' OR '.join(clauses)})
 ORDER BY A.ACCOUNT""",
            params, max_rows=5_000)
        return {
            "business_unit": bu, "tree": tree, "node": node.strip(),
            "ranges": [{"from": lo, "to": hi} for lo, hi in ranges],
            "accounts": [r["account"] for r in rows],
            "detail": rows,
            "account_count": len(rows),
            "note": ("These are the accounts the node's ranges resolve to in "
                     "the account master. To use them in another query, "
                     "REFERENCE this result (list_binds with from_result), "
                     "do not retype them. For a trial balance by node, call "
                     "rollup_trial_balance instead — it needs none of this."),
        }

    def _node_ranges(self, bu: str, tree: str, node: str) -> list:
        setid = self.e.resolve_setid(bu)
        key = (setid, tree, node, bu)
        if key in self._range_cache:
            return self._range_cache[key]
        effdt_params = {"setid": setid, "tree": tree}
        rows, _ = self.db.query(q.tree_effdt(self.db, effdt_params),
                                effdt_params, max_rows=1)
        effdt = rows[0]["effdt"] if rows else None
        if not effdt:
            trees, _ = self.db.query(q.trees_list(self.db), {}, max_rows=50)
            raise ReportError(
                f"Tree {tree!r} not found for setid {setid}. "
                f"Available: {sorted({t['tree_name'] for t in trees})}"
            )
        # Each statement gets exactly the binds it uses: python-oracledb (thin)
        # raises DPY-4008 on a bind name absent from the SQL, while sqlite3
        # silently ignores extras — a divergence tests on SQLite cannot catch.
        base = {"tsetid": setid, "tree": tree, "teffdt": str(effdt)[:10],
                "tctl": self.e.resolve_tree_ctl(setid, tree, bu)}
        span, _ = self.db.query(q.tree_node_span(self.db),
                                {**base, "node": node}, max_rows=1)
        if not span:
            nodes, _ = self.db.query(q.tree_nodes_list(self.db), dict(base),
                                     max_rows=30)
            raise ReportError(
                f"Node {node!r} not in tree {tree}. Nodes include: "
                f"{[n['tree_node'] for n in nodes]}"
            )
        ranges, truncated = self.db.query(
            q.tree_leaf_ranges(self.db),
            {**base, "a": span[0]["a"], "b": span[0]["b"]}, max_rows=50_000,
        )
        if truncated:
            # Refuse rather than compute a plausible-but-understated total
            # from a partial leaf list (which would also poison the cache).
            raise ReportError(
                f"Node {node!r} carries more than 50,000 leaf ranges — "
                "refusing to compute a partial total. Report a lower-level "
                "node instead."
            )
        out = [(r["range_from"], r["range_to"]) for r in ranges]
        if not out:
            raise ReportError(f"Node {node!r} has no account ranges attached")
        self._range_cache[key] = out
        return out

    # ------------------------------------------------------------------- run
    def _adhoc(self, rows: str, columns: str) -> dict:
        rws = []
        for tok in (rows or "").split(","):
            t = tok.strip()
            if not t:
                continue
            parts = t.split(":")
            flip = parts[-1].lower() == "flip"
            core = parts[:-1] if flip else parts
            if len(core) == 2 and core[0].lower() == "node":
                rws.append({"label": core[1], "node": core[1], "flip_sign": flip})
            elif len(core) == 2 and core[0].lower() in ("acct", "account"):
                rws.append({"label": core[1], "account": core[1], "flip_sign": flip})
            else:
                raise ReportError(
                    f"Row token {t!r} — use node:NAME or acct:RANGE "
                    "(append :flip to show credit balances positive)"
                )
        if not rws:
            raise ReportError("Ad-hoc report needs at least one row token")
        cls = []
        for tok in (columns or "").split(","):
            t = tok.strip()
            if not t:
                continue
            if ":" in t:
                lg, ts = t.split(":", 1)
            else:
                lg, ts = "", t
            cls.append({"label": t, "ledger": lg.strip().upper(),
                        "timespan": ts.strip().upper()})
        spec = {"name": "adhoc", "title": "Ad-hoc report", "rows": rws}
        if cls:
            spec["columns"] = cls
        return spec

    def run(
        self,
        report: str = "",
        business_unit: str = "",
        fiscal_year: int = 0,
        period: int = 0,
        ledger: str = "",
        rows: str = "",
        columns: str = "",
        include_adjustments: bool = False,
    ) -> dict:
        bu, fy, per, led = self.e._defaults(business_unit, fiscal_year, period, ledger)
        if report.strip():
            defs = self._definitions()
            if report.strip() not in defs:
                # The report pack is GL-shaped: rows are accounts or tree
                # nodes. A model reaching for an invented name like
                # "revenue_by_customer" is asking a cross-tab question about a
                # dimension no report carries, so point it at the tool that
                # does rather than leaving it to guess a second wrong name.
                raise ReportError(
                    f"Unknown report {report!r}. Available: {sorted(defs)} "
                    "(or pass ad-hoc rows=...). These report on GL ACCOUNTS. "
                    "For any other dimension across periods — revenue per "
                    "CUSTOMER by month, spend per VENDOR by quarter — use "
                    "run_sql with one grouped query and pivot={'row_field': "
                    "..., 'column_field': ..., 'value_field': ...}, which "
                    "returns the cross-tab with totals and change already "
                    "computed."
                )
            spec = defs[report.strip()]
        elif rows.strip():
            spec = self._adhoc(rows, columns)
        else:
            raise ReportError(
                "Pass report=<name> (see list_reports) or ad-hoc "
                'rows="node:REVENUE:flip,acct:5000-5999"'
            )

        maxreg = self.e._max_regular_period(fy)
        if per > maxreg:
            # Running a report "at" an adjustment period (998) means the
            # post-adjustment year-end basis — same semantics as
            # get_trial_balance(period=998). Without this clamp, YTD would
            # resolve to periods 1..998 and silently sweep adjustment rows in.
            per = maxreg
            include_adjustments = True
        tree_default = spec.get("tree") or self.cfg.defaults.account_tree

        # ---- columns: value columns resolve a timespan; computed come later
        cols: list[dict] = []
        for c in spec.get("columns") or [{"label": "YTD", "timespan": "YTD"}]:
            if "subtract" in c or "percent" in c:
                op = "subtract" if "subtract" in c else "percent"
                refs = c[op]
                if not (isinstance(refs, list) and len(refs) == 2):
                    raise ReportError(f"Column {c.get('label')!r}: {op} needs [a, b]")
                for ref in refs:
                    if ref not in [x["label"] for x in cols]:
                        raise ReportError(
                            f"Column {c.get('label')!r} references {ref!r}, "
                            "which must be defined earlier"
                        )
                cols.append({"label": c.get("label") or op, "kind": op, "refs": refs})
                continue
            ts = resolve_timespan(c.get("timespan", "YTD"), fy, per, maxreg)
            segs = [dict(s) for s in ts["segments"]]
            if include_adjustments and ts["adjustments_eligible"]:
                for s in segs:
                    s["periods"] = s["periods"] + self.e._adj_periods()
            cols.append({
                "label": c.get("label") or f"{c.get('ledger') or led} {ts['timespan']}",
                "kind": "value",
                "ledger": (c.get("ledger") or led).upper(),
                "timespan": ts["timespan"],
                "segments": segs,
            })

        # ---- one ledger fetch per (ledger, fiscal year)
        need = {(c["ledger"], s["fiscal_year"])
                for c in cols if c["kind"] == "value" for s in c["segments"]}
        data: dict = {}
        meta: dict = {}
        for lg, y in sorted(need):
            recs = self.e._period_sums(bu, lg, y, maxreg, include_adj=True)
            m = data.setdefault((lg, y), {})
            for rec in recs:
                acct = rec["account"]
                pmap = m.setdefault(acct, {})
                pnum = int(rec["period"])
                pmap[pnum] = pmap.get(pnum, 0.0) + float(rec["amt"] or 0.0)
                if rec.get("descr") and acct not in meta:
                    meta[acct] = {"descr": rec["descr"], "type": rec.get("acct_type")}

        base = {
            "report": spec.get("name", report or "adhoc"),
            "title": spec.get("title", report),
            "business_unit": bu, "ledger_default": led,
            "fiscal_year": fy, "period": per,
            "include_adjustments": bool(include_adjustments),
            "amount_basis": "base",
            "columns": [
                {k: c[k] for k in ("label", "kind", "ledger", "timespan", "refs")
                 if k in c}
                for c in cols
            ],
        }
        if not any(data.values()):
            diag = self.e._scope_diagnosis(bu, led, fy)
            return {**base, **diag, "rows": [],
                    "note": "NO DATA — nothing matched this scope. "
                            "This is not a zero report. " + diag["detail"]}

        all_accts = sorted({a for m in data.values() for a in m})

        def sum_cell(test: Callable[[str], bool], col: dict) -> Optional[float]:
            seen, tot = False, 0.0
            for s in col["segments"]:
                m = data.get((col["ledger"], s["fiscal_year"]))
                if not m:
                    continue
                seen = True
                pset = set(s["periods"])
                for acct, pers in m.items():
                    if test(acct):
                        tot += sum(v for p, v in pers.items() if p in pset)
            return tot if seen else None

        out_rows: list[dict] = []
        by_id: dict[str, list] = {}

        def record(row: dict, rid: Optional[str]) -> None:
            out_rows.append(row)
            if rid:
                # Preserve None (no data) — coercing to 0.0 here would let a
                # subtotal fabricate a balanced 0.00 for a year with no rows.
                by_id[rid] = list(row["cells"])

        for r in spec.get("rows") or []:
            label = r.get("label") or r.get("node") or r.get("account") or "?"
            if "subtotal" in r:
                cells: list = []
                for ci, c in enumerate(cols):
                    if c["kind"] != "value":
                        cells.append(None)
                        continue
                    vals = []
                    for ref in r["subtotal"]:
                        sign = -1.0 if str(ref).startswith("-") else 1.0
                        rid = str(ref).lstrip("+-")
                        if rid not in by_id:
                            raise ReportError(
                                f"Subtotal {label!r}: row id {rid!r} is not "
                                "defined earlier in rows"
                            )
                        v = by_id[rid][ci]
                        vals.append(None if v is None else sign * v)
                    if not vals or all(v is None for v in vals):
                        cells.append(None)  # no data anywhere -> no subtotal
                    else:
                        cells.append(r2(sum(v for v in vals if v is not None)))
                record({"label": label, "kind": "subtotal", "cells": cells}, r.get("id"))
                continue

            flip = -1.0 if r.get("flip_sign") else 1.0
            if "node" in r:
                ranges = self._node_ranges(bu, r.get("tree") or tree_default, r["node"])
                test = (lambda s, rg=tuple(ranges):
                        any(lo <= s <= hi for lo, hi in rg))
                kind = "node"
            elif "account" in r:
                test = _account_matcher(str(r["account"]))
                kind = "account"
            else:
                raise ReportError(
                    f"Row {label!r} needs one of: node, account, subtotal"
                )
            cells = [
                (None if (v := sum_cell(test, c)) is None else r2(flip * v))
                if c["kind"] == "value" else None
                for c in cols
            ]
            record({"label": label, "kind": kind, "cells": cells,
                    "flip_sign": bool(r.get("flip_sign"))}, r.get("id"))

            if r.get("expand"):
                for acct in all_accts:
                    if not test(acct):
                        continue
                    dcells = [
                        (None if (v := sum_cell(lambda s, a=acct: s == a, c)) is None
                         else r2(flip * v))
                        if c["kind"] == "value" else None
                        for c in cols
                    ]
                    if any(abs(x) > 0.005 for x in dcells if x is not None):
                        descr = (meta.get(acct) or {}).get("descr") or ""
                        out_rows.append({"label": f"{acct}  {descr}".strip(),
                                         "kind": "detail", "indent": 1,
                                         "cells": dcells})

        # ---- computed columns, applied uniformly to every row
        label_ix = {c["label"]: i for i, c in enumerate(cols)}
        for row in out_rows:
            for ci, c in enumerate(cols):
                if c["kind"] == "value":
                    continue
                a = row["cells"][label_ix[c["refs"][0]]]
                b = row["cells"][label_ix[c["refs"][1]]]
                if a is None or b is None:
                    row["cells"][ci] = None
                elif c["kind"] == "subtract":
                    row["cells"][ci] = r2(a - b)
                else:  # percent
                    row["cells"][ci] = r2(a / abs(b) * 100) if abs(b) > 0.005 else None

        extra_note = (spec.get("note") or "").strip()
        return {
            **base,
            "rows": out_rows,
            "scope_status": "ok",
            "note": (extra_note + " " if extra_note else "") + (
                "Base currency; statistical rows excluded. Rows marked "
                "flip_sign show credit balances (revenue, liabilities) as "
                "positive. '—' means the ledger has no data for that column's "
                "scope. Quarters assume periods 1-12; see docs/NVISION.md for "
                "13-period calendars."
            ),
        }
