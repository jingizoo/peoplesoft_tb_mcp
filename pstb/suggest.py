"""What to look at next, derived from what the answer just showed.

A finance answer usually ends one step before the useful one. The aging
says a customer owes 110,400; the next question — is any of that in
dispute, is any of their cash sitting unapplied — is obvious to someone
who has done the job for ten years and invisible to everyone else. This
module writes that next question down, but only when the records already
in hand justify it.

The rules, and why each exists:

  1. A suggestion is EVIDENCE, not a guess. Every one carries the figure
     that produced it and names the tool that reported it. No rule fires
     on an absent number, an empty list, or a zero. "You might also want
     to check payables" with nothing behind it is a slot machine, and the
     third time it is wrong nobody reads the second one either.
  2. Every suggestion names the tool that will ANSWER it, and a test holds
     that tool to the real tool set. A suggested question that fails is
     worse than no suggestion — it teaches the person the thing cannot be
     asked.
  3. Ranked by what is at stake, not by the order the rules were written,
     or the first rule anyone adds wins forever.
  4. Bounded. Four is a next step; twelve is a wall to scroll past.
  5. Never suggest what was just answered. A turn that already ran the
     360 for C1004 must not offer to run it again.

Nothing here reaches the database. Rules read the payloads the person has
already been shown, so a suggestion can never surface a business unit
their row security withheld — the evidence had to pass that gate first.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

MAX_SUGGESTIONS = 4

# Severity tiers. Amount ranks WITHIN a tier: a 200.00 difference between
# the subledger and the ledger matters more than a 90,000.00 balance that
# is merely large, because one of them means the books disagree with
# themselves and the other is just Tuesday.
BOOKS_DISAGREE = 2      # the records contradict each other
MONEY_AT_RISK = 1       # real money in a state nobody chose
FOLLOW_UP = 0           # the ordinary next step


@dataclass(frozen=True)
class Suggestion:
    question: str            # exactly what will be asked if clicked
    because: str             # the figure that earned it, in words
    kind: str
    answered_by: str         # the tool that will answer it
    evidence_from: str       # the tool whose payload triggered it
    amount: float = 0.0
    currency: str = ""
    severity: int = FOLLOW_UP

    def as_dict(self) -> dict:
        return {"question": self.question, "because": self.because,
                "kind": self.kind, "answered_by": self.answered_by,
                "evidence_from": self.evidence_from,
                "amount": self.amount, "currency": self.currency}


@dataclass
class Context:
    """What this turn already did, so nothing is offered back to itself."""
    question: str = ""
    business_unit: str = ""
    asked: set = field(default_factory=set)     # (tool, subject) pairs run


def _money(value, currency: str = "") -> str:
    try:
        text = f"{abs(float(value)):,.2f}"
    except (TypeError, ValueError):
        return ""
    return f"{text} {currency}".strip()


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _attention(payload: dict, kind: str) -> list:
    return [a for a in (payload.get("needs_attention") or [])
            if a.get("kind") == kind]


# --------------------------------------------------------------- the rules
# Each rule takes (payload, ctx) and returns Suggestions. A rule that finds
# nothing returns nothing — silence is a valid answer and the common one.

def _from_customer_360(p: dict, ctx: Context) -> list:
    out: list = []
    cid = str((p.get("customer") or {}).get("cust_id") or "")
    if not cid:
        return out
    name = str((p.get("customer") or {}).get("name") or "") or cid

    for a in _attention(p, "cash_received_not_applied"):
        deposits = ", ".join(d.get("deposit_id", "")
                             for d in (a.get("deposits") or [])[:3])
        out.append(Suggestion(
            question=f"Show the open AR items for customer {cid}",
            because=f"{_money(a.get('amount'), a.get('currency'))} of this "
                    f"customer's cash was received and never applied"
                    + (f" ({deposits})" if deposits else "")
                    + " — the open items are what it should be matched to.",
            kind="unapplied_cash", answered_by="get_customer_ar",
            evidence_from="get_customer_financial_360",
            amount=_num(a.get("amount")), currency=a.get("currency", ""),
            severity=MONEY_AT_RISK))

    for a in _attention(p, "credit_not_fully_rebilled"):
        out.append(Suggestion(
            question="Where is the billing delay?",
            because=f"Invoice {a.get('original_invoice')} was credited and "
                    f"re-billed for {_money(a.get('amount'))} less. Value "
                    "leaving through the billing pipeline is what that view "
                    "measures.",
            kind="billing_leakage", answered_by="get_invoice_lifecycle",
            evidence_from="get_customer_financial_360",
            amount=_num(a.get("amount")), severity=MONEY_AT_RISK))

    for a in _attention(p, "overdue_receivable"):
        # The anchor is excluded before the max, not after: the parent is
        # usually its own largest contributor, and "have you considered
        # looking at the customer you just looked at" is the whole rule
        # wasted on the common case.
        others = [c for c in (a.get("contributors") or [])
                  if str(c.get("cust_id")) != cid]
        worst = max(others, key=lambda c: _num(c.get("overdue")), default=None)
        if worst and _num(worst.get("overdue")) > 0:
            sub = str(worst["cust_id"])
            out.append(Suggestion(
                question=f"Show the complete financial picture for customer "
                         f"{sub}",
                because=f"{worst.get('name') or sub} carries "
                        f"{_money(worst.get('overdue'), a.get('currency'))} "
                        f"of {name}'s past-due balance — more than any other "
                        "company in the family.",
                kind="subsidiary_drives_overdue",
                answered_by="get_customer_financial_360",
                evidence_from="get_customer_financial_360",
                amount=_num(worst.get("overdue")),
                currency=a.get("currency", ""), severity=MONEY_AT_RISK))

    for a in _attention(p, "disputed"):
        out.append(Suggestion(
            question=f"Which customers in {ctx.business_unit} have disputed "
                     f"balances?" if ctx.business_unit else
                     "Which customers have disputed balances?",
            because=f"{_money(a.get('amount'), a.get('currency'))} of this "
                    "customer's balance is in dispute and will not collect "
                    "until it is settled. Aging reports the same flag for "
                    "everyone else.",
            kind="disputes", answered_by="get_ar_aging",
            evidence_from="get_customer_financial_360",
            amount=_num(a.get("amount")), currency=a.get("currency", ""),
            severity=FOLLOW_UP))

    for a in _attention(p, "revenue_not_yet_billed"):
        out.append(Suggestion(
            question="Where is the billing delay?",
            because=f"{_money(a.get('amount'), a.get('currency'))} across "
                    f"{a.get('bills')} bill(s) has not been finalized, the "
                    f"oldest waiting {a.get('oldest_days')} days.",
            kind="stuck_revenue", answered_by="get_invoice_lifecycle",
            evidence_from="get_customer_financial_360",
            amount=_num(a.get("amount")), currency=a.get("currency", ""),
            severity=MONEY_AT_RISK))
    return out


def _from_ar_aging(p: dict, ctx: Context) -> list:
    out: list = []
    tie = p.get("gl_tie") or {}
    if tie.get("evaluated") and tie.get("ties") is False:
        out.append(Suggestion(
            question=f"What changed in account "
                     f"{(tie.get('control_accounts') or ['1100'])[0]}?",
            because=f"The open items and the AR control account differ by "
                    f"{_money(tie.get('difference'))}. Until that is "
                    "explained, every customer balance below is suspect.",
            kind="ar_gl_break", answered_by="explain_balance_change",
            evidence_from="get_ar_aging",
            amount=abs(_num(tie.get("difference"))),
            severity=BOOKS_DISAGREE))

    disp = str(p.get("display_currency") or "")
    # get_customer_ar is shape-identical except that its one customer sits
    # under "customer", singular. The rule is registered for both tools, so
    # without this it silently found nothing for half of them.
    rows = list(p.get("customers") or [])
    if not rows and isinstance(p.get("customer"), dict):
        rows = [p["customer"]]
    customers = [c for c in rows if _num(c.get("total")) > 0]
    if customers:
        worst = max(customers, key=lambda c: _num(c.get("total")))
        cid = str(worst.get("cust_id") or "")
        if cid:
            out.append(Suggestion(
                question=f"Show the complete financial picture for customer "
                         f"{cid}",
                because=f"{worst.get('name') or cid} carries the largest "
                        f"open balance here, "
                        f"{_money(worst.get('total'), disp)}, "
                        f"the oldest {worst.get('oldest_days_past_due')} days "
                        "past due.",
                kind="largest_debtor",
                answered_by="get_customer_financial_360",
                evidence_from="get_ar_aging",
                amount=_num(worst.get("total")), currency=disp,
                severity=FOLLOW_UP))
    disputed = [c for c in rows if _num(c.get("disputed_amt"))]
    if disputed:
        worst = max(disputed, key=lambda c: abs(_num(c.get("disputed_amt"))))
        out.append(Suggestion(
            question=f"Show the complete financial picture for customer "
                     f"{worst.get('cust_id')}",
            because=f"{_money(worst.get('disputed_amt'), disp)} of this "
                    f"customer's "
                    "balance is flagged in dispute — the 360 shows what else "
                    "is stuck alongside it.",
            kind="disputes", answered_by="get_customer_financial_360",
            evidence_from="get_ar_aging",
            amount=abs(_num(worst.get("disputed_amt"))), currency=disp,
            severity=MONEY_AT_RISK))
    return out


def _from_integrity(p: dict, ctx: Context) -> list:
    out: list = []
    if p.get("balanced") is False:
        out.append(Suggestion(
            question="Are there unposted journals?",
            because=f"Debits and credits differ by "
                    f"{_money(_num(p.get('total_debits')) - _num(p.get('total_credits')))}"
                    ". An unposted or half-posted journal is the usual cause.",
            kind="out_of_balance", answered_by="drill_to_journals",
            evidence_from="tb_integrity_check",
            amount=abs(_num(p.get("total_debits"))
                       - _num(p.get("total_credits"))),
            severity=BOOKS_DISAGREE))
    unposted = p.get("unposted_journals") or []
    if unposted:
        out.append(Suggestion(
            question="Are there unposted journals?",
            because=f"{len(unposted)} journal(s) in this range are not "
                    f"posted, the oldest {unposted[0].get('journal_id')} "
                    f"dated {str(unposted[0].get('journal_date'))[:10]}.",
            kind="unposted", answered_by="drill_to_journals",
            evidence_from="tb_integrity_check",
            amount=float(len(unposted)), severity=FOLLOW_UP))
    suspense = [s for s in (p.get("suspense_balances") or [])
                if _num(s.get("ending"))]
    if suspense:
        worst = max(suspense, key=lambda s: abs(_num(s.get("ending"))))
        out.append(Suggestion(
            question=f"Is our suspense balance in account "
                     f"{worst.get('account')} within policy?",
            because=f"Account {worst.get('account')} carries "
                    f"{_money(worst.get('ending'))}. Whether that is "
                    "acceptable is a policy question, not a ledger one.",
            kind="suspense", answered_by="wiki_lookup",
            evidence_from="tb_integrity_check",
            amount=abs(_num(worst.get("ending"))), severity=MONEY_AT_RISK))
    return out


def _from_billing(p: dict, ctx: Context) -> list:
    out: list = []
    orphans = p.get("finalized_not_in_ar") or []
    if orphans:
        total = sum(_num(o.get("amount")) for o in orphans)
        out.append(Suggestion(
            question="Where is the billing delay?",
            because=f"{len(orphans)} finalized invoice(s) worth "
                    f"{_money(total)} never reached AR — billed revenue that "
                    "is not collectable because nobody is asking for it.",
            kind="not_in_ar", answered_by="get_invoice_lifecycle",
            evidence_from="get_billing_workbench",
            amount=total, severity=MONEY_AT_RISK))
    stuck = p.get("stuck_invoices") or []
    if stuck:
        total = sum(_num(s.get("amount")) for s in stuck)
        out.append(Suggestion(
            question="Where is the billing delay?",
            because=f"{len(stuck)} bill(s) worth {_money(total)} have sat "
                    f"longer than the site's threshold.",
            kind="stuck_revenue", answered_by="get_invoice_lifecycle",
            evidence_from="get_billing_workbench",
            amount=total, severity=FOLLOW_UP))
    return out


def _from_lifecycle(p: dict, ctx: Context) -> list:
    b = p.get("bottleneck") or {}
    if not b.get("stage") or not _num(b.get("amount")):
        return []
    return [Suggestion(
        question=(f"Show the billing workbench for {ctx.business_unit}"
                  if ctx.business_unit else "Show the billing workbench"),
        because=f"{_money(b.get('amount'))} is sitting at "
                f"{b.get('stage')} — {b.get('why')}. The workbench names "
                "the individual bills.",
        kind="bottleneck", answered_by="get_billing_workbench",
        evidence_from="get_invoice_lifecycle",
        amount=_num(b.get("amount")), severity=MONEY_AT_RISK)]


def _from_duplicates(p: dict, ctx: Context) -> list:
    dupes = p.get("exact_invoice_duplicates") or []
    if not dupes:
        return []
    worst = max(dupes, key=lambda d: _num(d.get("total")))
    return [Suggestion(
        question=f"Show the vendor payments for {worst.get('vendor_id')}",
        because=f"{worst.get('vendor')} was invoiced "
                f"{worst.get('invoice_id')} on {worst.get('vouchers')} "
                f"separate vouchers totalling {_money(worst.get('total'))}. "
                "Whether it was PAID twice is the question that matters.",
        kind="duplicate_payment", answered_by="get_vendor_payments",
        evidence_from="get_duplicate_payments",
        amount=_num(worst.get("total")), severity=MONEY_AT_RISK)]


def _from_payables(p: dict, ctx: Context) -> list:
    overdue = _num(p.get("overdue_total"))
    if overdue <= 0:
        return []
    return [Suggestion(
        question="What is our cash outlook for the next 8 weeks?",
        because=f"{_money(overdue)} of payables is already past due; the "
                "outlook puts it next to what is coming in.",
        kind="overdue_payables", answered_by="get_cash_outlook",
        evidence_from="get_open_payables",
        amount=overdue, severity=FOLLOW_UP)]


# The follow-up question for a tool the process trace surfaced: a scoped
# form when the trace resolved a business unit, and otherwise a generic
# wording the eval suite already proves routes correctly — an invented
# phrasing that fails when clicked teaches the person the thing cannot be
# asked. Only tools answerable FROM A UNIT ALONE belong here; one whose
# question needs an account or a customer id has no honest generic form.
# Keys are as the graph stores tool names (uppercase).
_PROCESS_TOOL_ASKS = {
    "GET_AR_AGING": (
        "Show the AR aging in {u} as of today",
        "Show the AR aging"),
    "GET_BILLING_WORKBENCH": (
        "Where is the billing delay in {u} today?",
        "Where is the billing delay?"),
    "GET_TOP_BILLING_CUSTOMERS": (
        "Who are the top billing customers in {u} this year?",
        "Who are our top 5 billing customers across all business units in "
        "USD?"),
    "GET_CUSTOMER_INTELLIGENCE": (
        "Who are the top customers in {u} and what do they buy?",
        "Who are my top customers, where are they based and what do they "
        "buy?"),
    "GET_TRIAL_BALANCE": (
        "Does the trial balance in {u} balance?",
        "Does the trial balance balance?"),
    "GET_OPEN_PAYABLES": (
        "How much do we owe suppliers in {u} right now?",
        "How much do we owe our vendors right now?"),
    "GET_CASH_OUTLOOK": (
        "What is the cash outlook in {u} for the next 8 weeks?",
        "What is our cash outlook for the next 8 weeks?"),
    "GET_DSO_TREND": (
        "How is DSO trending in {u} this year?",
        "Show the DSO trend for this year"),
}


def _from_process(p: dict, ctx: Context) -> list:
    """A process answer ends where the numbers begin. Point across the gap.

    trace_process explains structure and holds no amounts, so the natural
    follow-up is always the FIGURE the structure leads to: the trace names
    the tools that answer from this process's records, and the scope names
    the unit to ask them about. Both facts are in the payload — the
    suggestion just puts them in one clickable sentence.

    The scope refusal is evidence too. "No unit operates in that country"
    earns exactly one next question — what units DO exist — and nothing
    else: recommending a figure for a scope that resolved to nothing would
    build on the refusal.
    """
    layers = {lay.get("layer"): lay.get("items") or []
              for lay in p.get("layers") or []}
    scopes = p.get("scope_applied") or []
    units = [u for s in scopes for u in (s.get("business_units") or [])]

    # Only a refusal when NOTHING resolved: a second, partial-word country
    # match beside a resolved one must not turn the answer into a refusal.
    empty = [s for s in scopes if s.get("facet") == "country"
             and not s.get("business_units")]
    if empty and not units:
        where = str(empty[0].get("name") or empty[0].get("value") or "")
        return [Suggestion(
            question="What business units and ledgers exist?",
            because=f"No business unit records its country as {where}; the "
                    "scope list shows where this installation actually "
                    "operates.",
            kind="scope_unresolved", answered_by="list_financial_scopes",
            evidence_from="trace_process", severity=FOLLOW_UP)]

    unit = units[0] if units else ""
    for item in layers.get("tool", []):
        asks = _PROCESS_TOOL_ASKS.get(str(item.get("name") or ""))
        if not asks:
            continue
        scoped, generic = asks
        return [Suggestion(
            question=scoped.format(u=unit) if unit else generic,
            because="The trace names this tool as one that answers from "
                    "this process's own records"
                    + (f", and the scope resolved to {unit}" if unit
                       else "") + ".",
            kind="process_to_figures",
            answered_by=str(item["name"]).lower(),
            evidence_from="trace_process", severity=FOLLOW_UP)]
    return []                     # the layer is ranked; one is a pointer


RULES: dict = {
    "get_customer_financial_360": _from_customer_360,
    "get_ar_aging": _from_ar_aging,
    "get_customer_ar": _from_ar_aging,      # same payload shape, one customer
    "tb_integrity_check": _from_integrity,
    "get_billing_workbench": _from_billing,
    "get_invoice_lifecycle": _from_lifecycle,
    "get_duplicate_payments": _from_duplicates,
    "get_open_payables": _from_payables,
    "trace_process": _from_process,
}

# The subject a tool's question is ABOUT, so a suggestion is not offered
# back to the turn that already answered it.
_SUBJECT_KEYS = ("cust_id", "customer", "vendor_id", "account", "invoice")


def _subject(question: str) -> str:
    """The identifier a suggested question turns on, for dedupe.

    Deliberately crude: an uppercase code or a bare account number. It only
    has to tell "the 360 for C1004" apart from "the 360 for C1010".
    """
    match = re.search(r"\b([A-Z]{1,4}\d{3,}|\d{4})\b", question)
    return match.group(1) if match else ""


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def suggestions_for(payloads, question: str = "",
                    business_unit: str = "") -> list:
    """Ranked next questions from this turn's tool payloads.

    ``payloads`` is the same ``(tool_name, payload)`` sequence the grounding
    guard reads — payload may be a JSON string or an already-parsed dict,
    because the GUI holds prior payloads as strings and callers in tests
    hand over dicts.
    """
    ctx = Context(question=question or "", business_unit=business_unit or "")
    asked_questions = {_normalize(question)}
    found: list = []

    for entry in payloads or ():
        name, payload = (entry if isinstance(entry, (tuple, list))
                         and len(entry) == 2 else ("", entry))
        rule = RULES.get(str(name))
        if rule is None:
            continue
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError):
                continue
        if not isinstance(payload, dict):
            continue
        # A refusal is not evidence. Suggesting a follow-up to "this
        # business unit does not exist" would be the tool confidently
        # building on its own failure.
        if payload.get("scope_status") not in (None, "ok") or \
                payload.get("error"):
            continue
        subject = str(payload.get("business_unit") or "")
        if subject and not ctx.business_unit:
            ctx.business_unit = subject
        for key in _SUBJECT_KEYS:
            value = payload.get(key) or (payload.get("customer") or {}).get(key)
            if isinstance(value, str) and value:
                ctx.asked.add((str(name), value))
        try:
            found.extend(rule(payload, ctx) or [])
        except Exception:            # noqa: BLE001
            # A rule that cannot read a site's payload shape costs that
            # rule. Suggestions are the last thing that should take down
            # an answer the person already has.
            continue

    ranked, seen = [], set()
    for s in sorted(found, key=lambda x: (-x.severity, -abs(x.amount))):
        key = _normalize(s.question)
        if key in asked_questions or key in seen:
            continue
        if (s.answered_by, _subject(s.question)) in ctx.asked:
            continue          # this turn already answered exactly that
        seen.add(key)
        ranked.append(s)
        if len(ranked) >= MAX_SUGGESTIONS:
            break
    return [s.as_dict() for s in ranked]
