"""Playbooks: accountant workflows composed from curated tools.

"Is the ledger ready to close?" is not one question — it is the eight checks a
close team already runs in sequence. Asking the model to orchestrate that is
where multi-step agents go wrong: it forgets a step, reorders them, or
narrates a conclusion the steps do not support.

So the SEQUENCE lives here, in Python, and runs server-side. Every step calls
an existing curated tool, so the semantics stay single-sourced — a playbook
can never disagree with the tool it wraps. The model triggers a playbook and
narrates the result; it never decides what the steps are.

Verdict rules, deliberately strict and consistent with the rest of this
codebase:
  passed              every step ran and found nothing to act on
  exceptions_found    every step ran; at least one found something
  incomplete          a step could NOT run — never reported as passed

An incomplete playbook is the important case: "we could not check the AR tie"
must never read as "the AR tie is fine".
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Callable, Optional

from .ar import ARBilling, ARError
from .engine import EngineError, TBEngine, r2


class PlaybookError(RuntimeError):
    pass


class Step:
    """One check: a callable returning (status, headline, detail).

    status is "ok" (nothing to act on), "attention" (a real finding), or
    "skipped" (could not run — the reason belongs in the headline).
    """

    def __init__(self, step_id: str, title: str, why: str,
                 run: Callable[[dict], tuple]):
        self.id = step_id
        self.title = title
        self.why = why
        self.run = run


def _fmt(amount) -> str:
    try:
        return f"{float(amount):,.2f}"
    except (TypeError, ValueError):
        return str(amount)


def _narrowed(ic: dict, check: str) -> Optional[dict]:
    """The reduced scope a control probe ran at, or None if it ran in full.

    A narrowed check is not incomplete — it ran and its findings are real —
    but reporting "every journal in scope is posted" when only one period
    was examined would let a reduced check impersonate the full one.
    """
    for row in (ic.get("checks_narrowed") or []):
        if row.get("check") == check:
            return row
    return None


def _incomplete(ic: dict, check: str) -> Optional[str]:
    """Why this specific control probe could not run, or None if it ran.

    Steps must consult this before reading a probe's result. Each probe
    defaults to an empty list when it fails, and reading that empty list as
    "nothing found" is the exact shape of a false pass: reporting "every
    journal in scope is posted" when in truth no journal was ever read.
    """
    for row in (ic.get("checks_incomplete") or []):
        if row.get("check") == check:
            return row.get("reason") or "check did not run"
    return None


# --------------------------------------------------------------- close steps
def _step_integrity(ctx: dict) -> tuple:
    ic = ctx["integrity"]
    if ic.get("scope_status") and ic["scope_status"] != "ok":
        return "skipped", f"no ledger data in scope ({ic['scope_status']})", ic
    if ic.get("balanced") is None:
        return "skipped", "balance could not be evaluated", ic
    if not ic["balanced"]:
        return ("attention",
                f"debits {_fmt(ic.get('total_debits'))} vs credits "
                f"{_fmt(ic.get('total_credits'))} — OUT OF BALANCE", ic)
    return "ok", f"debits equal credits at {_fmt(ic.get('total_debits'))}", ic


def _step_suspense(ctx: dict) -> tuple:
    rows = ctx["integrity"].get("suspense_balances") or []
    if ctx["integrity"].get("balanced") is None:
        return "skipped", "integrity check did not run", {}
    if not rows:
        return "ok", "no suspense balances", {"suspense_balances": []}
    total = sum(float(r.get("ending") or 0) for r in rows)
    return ("attention",
            f"{len(rows)} suspense account(s) holding {_fmt(total)} — clear "
            "or reclassify before close",
            {"suspense_balances": rows})


def _step_unposted(ctx: dict) -> tuple:
    ic = ctx["integrity"]
    rows = ic.get("unposted_journals") or []
    if ic.get("balanced") is None:
        return "skipped", "integrity check did not run", {}
    why = _incomplete(ic, "unposted_journals")
    if why:
        return "skipped", f"unposted-journal check could not run: {why}", {}
    scope_note = ""
    narrow = _narrowed(ic, "unposted_journals")
    if narrow:
        scope_note = (f" (checked {narrow['scope']} — {narrow['reason']})")
    if not rows:
        return ("ok", "every journal in scope is posted" + scope_note,
                {"unposted_journals": []})
    return ("attention", f"{len(rows)} unposted journal(s) — post or delete "
            "them before the period is closed" + scope_note,
            {"unposted_journals": rows})


def _step_out_of_balance_journals(ctx: dict) -> tuple:
    ic = ctx["integrity"]
    rows = ic.get("out_of_balance_journals") or []
    if ic.get("balanced") is None:
        return "skipped", "integrity check did not run", {}
    why = _incomplete(ic, "out_of_balance_journals")
    if why:
        return "skipped", f"journal-balance check could not run: {why}", {}
    scope_note = ""
    narrow = _narrowed(ic, "out_of_balance_journals")
    if narrow:
        scope_note = (f" (checked {narrow['scope']} — {narrow['reason']})")
    if not rows:
        return "ok", "all posted journals net to zero" + scope_note, {}
    return ("attention", f"{len(rows)} posted journal(s) do not net to zero"
            + scope_note, {"out_of_balance_journals": rows})


def _step_retained_earnings(ctx: dict) -> tuple:
    ic = ctx["integrity"]
    roll = ic.get("retained_earnings_roll") or {}
    why = _incomplete(ic, "retained_earnings_roll")
    if why:
        return "skipped", f"retained-earnings roll could not run: {why}", roll
    status = roll.get("status")
    if not status or status == "not_evaluated":
        return "skipped", "retained-earnings roll not evaluated", roll
    if status == "ok":
        return "ok", "beginning balances roll from the prior-year close", roll
    return ("attention",
            f"retained-earnings roll {status} — "
            f"{roll.get('detail') or 'prior-year close does not tie'}", roll)


def _step_ar_tie(ctx: dict) -> tuple:
    aging = ctx.get("aging")
    if isinstance(aging, str):                       # the call failed
        return "skipped", f"AR aging unavailable: {aging}", {}
    if aging.get("scope_status") and aging["scope_status"] != "ok":
        return "skipped", f"AR scope: {aging.get('detail') or 'no data'}", {}
    tie = aging.get("gl_tie") or {}
    if tie.get("evaluated") is False:
        return "skipped", f"AR tie not evaluated: {tie.get('reason')}", tie
    if tie.get("ties"):
        return ("ok", f"open items reconcile to the GL control "
                f"({_fmt(tie.get('subledger_total'))})", tie)
    return ("attention",
            f"AR does not tie: subledger {_fmt(tie.get('subledger_total'))} vs "
            f"GL {_fmt(tie.get('gl_balance'))}, difference "
            f"{_fmt(tie.get('difference'))}", tie)


def _step_billing_pipeline(ctx: dict) -> tuple:
    wb = ctx.get("workbench")
    if isinstance(wb, str):
        return "skipped", f"billing workbench unavailable: {wb}", {}
    status = wb.get("control_status")
    if status == "checks_incomplete":
        return ("skipped", "some billing checks could not run: "
                + "; ".join(wb.get("record_notes") or ["reason not reported"]),
                wb)
    issues = wb.get("issues") or []
    if not issues:
        return "ok", "billing pipeline clean", {"statuses": wb.get("statuses")}
    return "attention", "; ".join(issues), wb


def _step_period_position(ctx: dict) -> tuple:
    fy, per = ctx.get("latest_fy"), ctx.get("latest_period")
    if not fy:
        return "skipped", "latest posted period unknown", {}
    asked_fy, asked_per = ctx["fiscal_year"], ctx["period"]
    if (asked_fy, asked_per) == (fy, per):
        return ("ok", f"FY{fy} period {per} is the latest posted period",
                {"latest": {"fiscal_year": fy, "period": per}})
    return ("attention",
            f"checking FY{asked_fy} period {asked_per}, but the latest posted "
            f"period is FY{fy} period {per} — confirm this is intended",
            {"latest": {"fiscal_year": fy, "period": per}})


def _step_aging_overdue(ctx: dict) -> tuple:
    aging = ctx.get("aging")
    if isinstance(aging, str) or not isinstance(aging, dict):
        return "skipped", "AR aging unavailable", {}
    totals = aging.get("totals") or {}
    buckets = aging.get("buckets") or []
    overdue = sum(float(totals.get(b) or 0) for b in buckets[1:])
    if overdue <= 0:
        return "ok", "nothing past due", totals
    oldest = buckets[-1] if buckets else ""
    return ("attention",
            f"{_fmt(overdue)} past due, of which {_fmt(totals.get(oldest))} "
            f"in {oldest.replace('_', ' ')}",
            {"totals": totals, "buckets": buckets})


# ----------------------------------------------------- AP completeness steps
def _step_procurement_tie(ctx: dict) -> tuple:
    tie = ctx.get("ap_tie")
    if not isinstance(tie, dict):
        return "skipped", f"procurement tie could not run: {tie}", {}
    if not tie.get("evaluated"):
        return ("skipped",
                tie.get("reason") or "tie not evaluated", tie)
    mode = (" — SAMPLE procurement fixtures, not live data"
            if ctx.get("coupa_mode") == "fixtures" else "")
    breaks = tie.get("amount_breaks") or []
    missing = tie.get("missing_in_ap") or []
    if not breaks and not missing:
        return ("ok",
                f"every approved invoice has a matching voucher "
                f"({tie.get('matched')} matched){mode}", tie)
    missing_total = sum(float(m.get("total") or 0) for m in missing)
    return ("attention",
            f"{len(missing)} approved invoice(s) never reached AP "
            f"({_fmt(missing_total)}) and {len(breaks)} matched at a "
            f"different amount{mode}", tie)


def _step_accrual_candidates(ctx: dict) -> tuple:
    rni = ctx.get("rni")
    if not isinstance(rni, dict):
        return "skipped", f"received-not-invoiced could not run: {rni}", {}
    mode = (" — SAMPLE procurement fixtures, not live data"
            if ctx.get("coupa_mode") == "fixtures" else "")
    lines = rni.get("lines") or []
    if not lines:
        return "ok", f"nothing received awaits an invoice{mode}", rni
    totals = rni.get("rni_totals_by_currency") or {}
    return ("attention",
            f"{len(lines)} PO line(s) received but not invoiced — accrue "
            + " · ".join(f"{_fmt(v)} {c}" for c, v in sorted(totals.items()))
            + mode, rni)


def _step_voucher_pipeline(ctx: dict) -> tuple:
    pay = ctx.get("payables")
    if not isinstance(pay, dict):
        return "skipped", f"open payables could not run: {pay}", {}
    exceptions = pay.get("pipeline_exceptions") or []
    if not exceptions:
        return "ok", "no vouchers stuck in recycle or unposted", pay
    total = sum(float(x.get("amount") or 0) for x in exceptions)
    return ("attention",
            f"{len(exceptions)} voucher(s) in recycle/unposted holding "
            f"{_fmt(total)} — owed but invisible to a payment run",
            {"pipeline_exceptions": exceptions})


# ------------------------------------------------------ post-close steps
def _step_late_posts(ctx: dict) -> tuple:
    lp = ctx.get("late_posts")
    if not isinstance(lp, dict):
        return "skipped", f"late-post check could not run: {lp}", {}
    rows = lp.get("rows") or []
    if not lp.get("period_end"):
        return ("skipped",
                "no calendar end date for the watched period", lp)
    if not rows:
        return ("ok",
                f"no journals posted into {lp['fiscal_year']}/"
                f"{lp['period']} after {lp['period_end']}", lp)
    return ("attention",
            f"{len(rows)} journal(s) posted into the closed period AFTER "
            f"its end date ({lp['period_end']}) — the numbers moved after "
            "everyone stopped looking",
            lp)


PLAYBOOKS: dict = {
    "post_close_watch": {
        "title": "Post-close watch",
        "description": (
            "The dangerous window is AFTER the close: a late journal lands "
            "in a closed period, a tie that held on close day drifts, "
            "suspense refills. This re-checks the just-closed period — "
            "balance, suspense, unposted journals, and journals posted "
            "after the period ended."
        ),
        "context": ("integrity", "late_posts", "latest_posted"),
        "pin_to_latest_posted": True,
        "steps": [
            Step("balance_still", "Closed period still balances",
                 "an out-of-balance closed period means something moved "
                 "after close", _step_integrity),
            Step("suspense_still", "Suspense still clear",
                 "suspense refilling after close is unbooked reality",
                 _step_suspense),
            Step("unposted_into_closed", "No unposted journals remain",
                 "an unposted entry dated in a closed period will change "
                 "history when someone posts it", _step_unposted),
            Step("late_posts", "Nothing posted after the period ended",
                 "post-close journal activity is how closed numbers move "
                 "silently", _step_late_posts),
        ],
    },
    "ap_completeness": {
        "title": "AP completeness (month-end)",
        "description": (
            "Did everything that should hit payables actually hit it: "
            "approved procurement invoices reconciled to vouchers, received-"
            "not-invoiced value listed for accrual, and vouchers stuck in "
            "recycle or unposted surfaced before the period closes."
        ),
        "context": ("ap_tie", "rni", "payables"),
        "steps": [
            Step("procurement_tie", "Approved invoices reached AP",
                 "an approved invoice that never became a voucher is a "
                 "liability the ledger cannot see", _step_procurement_tie),
            Step("accruals", "Received-not-invoiced accruals",
                 "value received without an invoice must be accrued at "
                 "month-end", _step_accrual_candidates),
            Step("voucher_pipeline", "Vouchers stuck in the pipeline",
                 "recycle and unposted vouchers are owed but invisible to "
                 "payment runs", _step_voucher_pipeline),
        ],
    },
    "close_readiness": {
        "title": "Close readiness",
        "description": (
            "Everything a close team checks before declaring a period ready: "
            "balance, suspense, unposted and unbalanced journals, the "
            "retained-earnings roll, the AR tie-out, the billing pipeline, "
            "and whether you are looking at the period you think you are."
        ),
        "steps": [
            Step("balance", "Trial balance nets to zero",
                 "an out-of-balance ledger cannot be closed", _step_integrity),
            Step("suspense", "Suspense accounts",
                 "suspense must be cleared or reclassified before close",
                 _step_suspense),
            Step("unposted", "Unposted journals",
                 "unposted entries silently change the result after close",
                 _step_unposted),
            Step("journal_balance", "Posted journals net to zero",
                 "an unbalanced journal corrupts the ledger it posted to",
                 _step_out_of_balance_journals),
            Step("re_roll", "Retained-earnings roll",
                 "prior-year close must carry into this year's opening",
                 _step_retained_earnings),
            Step("ar_tie", "AR subledger ties to the GL control",
                 "a broken tie means the balance sheet misstates receivables",
                 _step_ar_tie),
            Step("billing", "Billing pipeline",
                 "invoices stuck before AR are revenue not yet recorded",
                 _step_billing_pipeline),
            Step("period", "Period position",
                 "checking the wrong period is the easiest mistake to make",
                 _step_period_position),
        ],
    },
    "receivables_health": {
        "title": "Receivables health",
        "description": (
            "Whether receivables are collectable and correctly stated: the "
            "aging profile, the GL tie-out, and the billing pipeline feeding "
            "it."
        ),
        "steps": [
            Step("ar_tie", "AR ties to the GL control",
                 "an untied subledger misstates the balance sheet",
                 _step_ar_tie),
            Step("overdue", "Overdue exposure",
                 "what is actually past due, and how old",
                 _step_aging_overdue),
            Step("billing", "Billing pipeline",
                 "invoices stuck before AR are revenue not yet recorded",
                 _step_billing_pipeline),
        ],
    },
}


class PlaybookRunner:
    def __init__(self, engine: TBEngine, ar: Optional[ARBilling] = None,
                 modules=None, coupa=None):
        self.e = engine
        self.ar = ar or ARBilling(engine)
        if modules is None:
            from .modules import ModulePacks
            modules = ModulePacks(engine)
        self.modules = modules
        if coupa is None:
            from .connectors import coupa as _coupa_mod
            coupa = _coupa_mod.from_env()
        self.coupa = coupa

    def list_playbooks(self) -> dict:
        return {
            "playbooks": [
                {"playbook": name, "title": spec["title"],
                 "description": spec["description"],
                 "steps": [{"step": s.id, "title": s.title, "why": s.why}
                           for s in spec["steps"]]}
                for name, spec in sorted(PLAYBOOKS.items())
            ],
            "note": ("Each playbook runs its whole sequence server-side and "
                     "returns one verdict. Report the verdict and the steps "
                     "needing attention; never re-run the steps yourself."),
        }

    def _context(self, bu: str, ledger: str, fy: int, period: int,
                 inputs: Optional[tuple] = None) -> dict:
        """Gather every input once. Steps read this rather than querying, so
        a playbook costs one pass over the tools it wraps.

        Every input is caught broadly and on purpose. DbError and EngineError
        are unrelated RuntimeError subclasses, so catching EngineError alone
        let a record-shape failure — the single likeliest thing to go wrong on
        a real instance — escape this method and abort the entire playbook
        with a traceback. One unreadable input must cost the steps that need
        it, not the seven checks that do not.
        """
        ctx: dict = {"business_unit": bu, "ledger": ledger,
                     "fiscal_year": fy, "period": period}

        def gather(key: str, fn, on_error) -> tuple:
            """(key, value, elapsed_ms) — run in a worker thread below."""
            started = time.perf_counter()
            try:
                value = fn()
            except Exception as e:                     # noqa: BLE001 — see above
                value = on_error(f"{type(e).__name__}: {e}")
            return key, value, int((time.perf_counter() - started) * 1000)

        def latest() -> tuple:
            try:
                return self.e.last_posted_period(bu, ledger)
            except Exception:                          # noqa: BLE001
                return None, None

        # The four inputs are INDEPENDENT — integrity reads the ledger, aging
        # reads AR, the workbench reads billing — yet they ran in sequence,
        # so the playbook's wall time was the SUM of the site's slowest
        # queries. On the bundled sample that sum is milliseconds; on a real
        # instance it is what made close readiness time out. The database
        # layer is built for this (Oracle session pool, per-thread SQLite),
        # so they now run together and the playbook costs the slowest input,
        # not the total. Each input's elapsed time is kept: when a run IS
        # slow, the result names the culprit instead of leaving "timed out"
        # as the whole diagnosis.
        from concurrent.futures import ThreadPoolExecutor

        # Each playbook declares which inputs it needs; gathering the
        # close-readiness set for an AP playbook would bill the user four
        # ledger queries for checks it never runs.
        available = {
            "integrity": ("integrity",
                          lambda: self.e.tb_integrity_check(
                              business_unit=bu, ledger=ledger,
                              fiscal_year=fy, period=period),
                          lambda msg: {"scope_status": "error",
                                       "detail": msg}),
            "aging": ("aging",
                      lambda: self.ar.aging(business_unit=bu), str),
            "workbench": ("workbench",
                          lambda: self.ar.billing_workbench(
                              business_unit=bu), str),
            "latest_posted": ("latest_posted", latest,
                              lambda msg: (None, None)),
            "rni": ("rni", lambda: self.coupa.received_not_invoiced(), str),
            "ap_tie": ("ap_tie",
                       lambda: self.coupa.ap_tie(self.e.db), str),
            "payables": ("payables",
                         lambda: self.modules.open_payables(
                             business_unit=bu), str),
            "late_posts": ("late_posts",
                           lambda: self._late_posts(bu, fy, period), str),
        }
        wanted = inputs or ("integrity", "aging", "workbench",
                            "latest_posted")
        ctx["coupa_mode"] = getattr(self.coupa, "mode", "")
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(gather, *available[name])
                       for name in wanted]
            timings: dict = {}
            for future in futures:
                key, value, elapsed_ms = future.result()
                timings[key] = elapsed_ms
                if key == "latest_posted":
                    lfy, lper = (value if isinstance(value, tuple)
                                 else (None, None))
                    ctx["latest_fy"], ctx["latest_period"] = lfy, lper
                else:
                    ctx[key] = value
        ctx["_input_timings_ms"] = timings
        return ctx

    def _late_posts(self, bu: str, fy: int, period: int) -> dict:
        """Journals in (fy, period) whose POSTED_DATE is after the
        period's calendar end — the numbers moved after close."""
        cols = self.e.db.columns("PS_JRNL_HEADER")
        if cols and "POSTED_DATE" not in cols:
            return {"rows": [], "period_end": None,
                    "note": "PS_JRNL_HEADER has no POSTED_DATE here"}
        end = None
        try:
            cal = self.e.list_periods(fy)["periods"]
            end = next((str(x["end_dt"])[:10] for x in cal
                        if int(x["period"]) == int(period)), None)
        except Exception:
            pass
        if not end:
            return {"rows": [], "period_end": None}
        p = self.e.db.prefix
        rows, _ = self.e.db.query(
            f"SELECT JOURNAL_ID AS journal_id, POSTED_DATE AS posted, "
            f"SOURCE AS source, OPRID AS oprid "
            f"FROM {p}PS_JRNL_HEADER WHERE BUSINESS_UNIT = :bu "
            f"AND FISCAL_YEAR = :fy AND ACCOUNTING_PERIOD = :per "
            f"AND POSTED_DATE > {self.e.db.date_bind('end')}",
            {"bu": bu, "fy": fy, "per": int(period), "end": end},
            max_rows=200)
        return {"fiscal_year": fy, "period": int(period),
                "period_end": end, "rows": rows}

    def run(self, playbook: str = "", business_unit: str = "",
            ledger: str = "", fiscal_year: int = 0, period: int = 0) -> dict:
        name = (playbook or "").strip() or "close_readiness"
        spec = PLAYBOOKS.get(name)
        if spec is None:
            raise PlaybookError(
                f"Unknown playbook {name!r}. Available: "
                f"{', '.join(sorted(PLAYBOOKS))}."
            )
        bu, fy, per, led = self.e._defaults(business_unit, fiscal_year,
                                            period, ledger)
        if spec.get("pin_to_latest_posted") and not (fiscal_year and period):
            # The just-closed period is the one under watch, not today's.
            lfy, lper = self.e.last_posted_period(bu, led)
            if lfy:
                fy, per = lfy, lper
        ctx = self._context(bu, led, fy, per,
                            inputs=spec.get("context"))

        results = []
        for step in spec["steps"]:
            try:
                status, headline, detail = step.run(ctx)
            except Exception as e:            # a broken step is skipped, loudly
                status, headline, detail = ("skipped",
                                            f"{type(e).__name__}: {e}", {})
            results.append({"step": step.id, "title": step.title,
                            "why": step.why, "status": status,
                            "headline": headline, "detail": detail})

        attention = [r for r in results if r["status"] == "attention"]
        skipped = [r for r in results if r["status"] == "skipped"]
        if skipped:
            verdict = "incomplete"
            summary = (f"{len(skipped)} of {len(results)} checks could not "
                       "run — this is NOT a pass")
        elif attention:
            verdict = "exceptions_found"
            summary = f"{len(attention)} of {len(results)} checks need attention"
        else:
            verdict = "passed"
            summary = f"all {len(results)} checks passed"

        timings = ctx.get("_input_timings_ms") or {}
        slowest = max(timings.values(), default=0)
        out_timing: dict = {"input_timings_ms": timings}
        if slowest >= 30_000:
            worst = max(timings, key=timings.get)
            out_timing["performance_note"] = (
                f"the {worst} input took {slowest / 1000:.0f}s — that is the "
                "query to tune, not the playbook. Run "
                "scripts/diagnose_db.py to see its individual statements.")
        return {
            "playbook": name,
            "title": spec["title"],
            "business_unit": bu, "ledger": led,
            "fiscal_year": fy, "period": per,
            "as_of": dt.date.today().isoformat(),
            "verdict": verdict,
            "summary": summary,
            **out_timing,
            "attention_count": len(attention),
            "skipped_count": len(skipped),
            "steps": results,
            "note": (
                "Verdict rules: 'passed' means every step ran and found "
                "nothing; 'exceptions_found' means every step ran and some "
                "found something; 'incomplete' means a step COULD NOT RUN and "
                "must never be read as a pass. Figures come from the same "
                "curated tools as the individual questions."
            ),
        }
