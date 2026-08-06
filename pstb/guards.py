"""Answer and tool-call guards for the finance agent. Stdlib only.

These exist because prompt rules do not reliably constrain smaller models. Two
failure modes were observed repeatedly and are handled here as mechanism:

  - the turn ENDS with "I will call get_account_balance" and no call is made,
    leaving the user with an intention instead of an answer;
  - a compliance verdict ("within policy") is asserted without having
    retrieved BOTH the rule text and the actual figure.

The chat loop also uses this module to enforce two boundaries before a tool is
called:

  - a mixed data + policy question must obtain successful PeopleSoft evidence
    before the wiki can be queried;
  - a request scope selected by the user is injected into financial tools and
    cannot be silently changed by the model.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping

_PROMISE = re.compile(
    r"(?i)\b(?:i (?:will|'ll|am going to)|let me|next,? i(?:'ll| will)|"
    r"to (?:verify|confirm|check) this,? i(?:'ll| will)?)\b[^.]{0,80}\b"
    r"(?:call|use|check|query|run|look ?up|retrieve|fetch)\b"
)
_VERDICT = re.compile(
    r"(?i)(?:\b(?:with)?in (?:our |the )?polic\w*|\b(?:non-?)?compliant\b|"
    r"\bin compliance\b|\bviolat\w+|\bbreach\w*|"
    r"\bexceeds? (?:the |our )?(?:policy|threshold|limit)\w*|"
    # "does this $6,000 purchase qualify" / "should this be capitalized" —
    # applying a rule to a specific fact is a verdict and needs both halves.
    r"\bdoes (?:this|that|it) .{0,40}\bqualif\w+|"
    r"\bshould (?:this|that|it) .{0,40}\b(?:be capitali[sz]\w+|be expensed|"
    r"be accrued|be written off)|"
    r"\bare we (?:compliant|within|allowed)\b)"
)
POLICY_TOOLS = {"wiki_health", "wiki_lookup", "wiki_search", "wiki_get_page"}
POLICY_EVIDENCE_TOOLS = {"wiki_lookup", "wiki_get_page"}
_DATA_HINTS = ("balance", "trial", "aging", "journal", "report", "billing",
               "integrity", "rollup", "sql", "customer", "rate")

# Tools that return an actual financial fact suitable for the data side of a
# mixed policy/compliance decision. Discovery helpers are deliberately absent:
# knowing that a BU exists is not evidence that its balance is compliant.
FINANCIAL_EVIDENCE_TOOLS = {
    "get_trial_balance",
    "get_account_balance",
    "compare_trial_balance",
    "drill_to_journals",
    "tb_integrity_check",
    "rollup_trial_balance",
    "get_exchange_rate",
    "get_top_billing_customers",
    "get_ar_aging",
    "get_customer_ar",
    "search_customers",
    "get_billing_workbench",
    "run_report",
    "run_sql",
    "run_playbook",
    "get_open_payables",
    "get_vendor_payments",
    "get_asset_register",
    "get_project_costs",
    "get_coupa_invoices",
    "get_coupa_stuck_approvals",
    "get_coupa_rni",
    "get_coupa_supplier_spend",
    "coupa_to_ap_tie",
}

# Request-scope field -> tool argument. The right-hand value differs only where
# a tool calls its period "through_period". Tools not listed do not accept
# financial scope parameters and are left untouched.
_TOOL_SCOPE_ARGS = {
    # run_sql receives only the business unit, and only as context for the
    # disclosure note — the SQL text itself is never rewritten.
    "run_sql": {"business_unit": "business_unit"},
    "get_trial_balance": {
        "business_unit": "business_unit", "ledger": "ledger",
        "fiscal_year": "fiscal_year", "period": "period",
    },
    "get_account_balance": {
        "business_unit": "business_unit", "ledger": "ledger",
        "fiscal_year": "fiscal_year", "period": "through_period",
    },
    "compare_trial_balance": {
        "business_unit": "business_unit", "ledger": "ledger",
        "fiscal_year": "fiscal_year", "period": "period",
    },
    "drill_to_journals": {
        "business_unit": "business_unit", "ledger": "ledger",
        "fiscal_year": "fiscal_year", "period": "period",
    },
    "list_periods": {"fiscal_year": "fiscal_year"},
    "tb_integrity_check": {
        "business_unit": "business_unit", "ledger": "ledger",
        "fiscal_year": "fiscal_year", "period": "period",
    },
    "rollup_trial_balance": {
        "business_unit": "business_unit", "ledger": "ledger",
        "fiscal_year": "fiscal_year", "period": "period",
    },
    "get_top_billing_customers": {"business_unit": "business_unit"},
    "get_ar_aging": {"business_unit": "business_unit"},
    "get_customer_ar": {"business_unit": "business_unit"},
    "search_customers": {"business_unit": "business_unit"},
    "get_billing_workbench": {"business_unit": "business_unit"},
    "run_report": {
        "business_unit": "business_unit", "ledger": "ledger",
        "fiscal_year": "fiscal_year", "period": "period",
    },
    "run_playbook": {
        "business_unit": "business_unit", "ledger": "ledger",
        "fiscal_year": "fiscal_year", "period": "period",
    },
    "resolve_timespan": {
        "fiscal_year": "fiscal_year", "period": "period",
    },
    "list_ledgers": {"business_unit": "business_unit"},
    "search_accounts": {"business_unit": "business_unit"},
    "get_open_payables": {"business_unit": "business_unit"},
    "get_vendor_payments": {"business_unit": "business_unit"},
    "get_asset_register": {"business_unit": "business_unit"},
    "get_project_costs": {"business_unit": "business_unit"},
}

# WHICH scope fields are governance and which are convenience.
# business_unit/ledger are HARD: answering from the wrong company or ledger is
# the failure the scope bar exists to prevent, so the model may never change
# them. fiscal_year/period are SOFT defaults the user's question may override.
_SOFT_SCOPE_FIELDS = {"fiscal_year", "period"}

_SCOPE_ALIASES = {
    "business_unit": ("business_unit", "bu"),
    "ledger": ("ledger",),
    "fiscal_year": ("fiscal_year", "fy"),
    "period": ("period", "per"),
}

_POLICY_QUERY = re.compile(
    r"(?i)(?:\b(?:polic(?:y|ies)|procedure|process|rule|guideline|standard|"
    r"threshold|approv(?:e|ed|es|ing|al|als)|authori[sz](?:e|ed|es|ing|ation)|"
    r"allow(?:ed|ance)?|permission|compliance|compliant|"
    r"capitali[sz](?:e|ed|ation)|close checklist|documentation|"
    r"responsib\w+|sign-?off)\b|"
    # "who can post to 998", "when is period 998 used" — process questions
    # whose answer lives in the wiki, not the ledger.
    r"\bwho (?:can|may|must|should|is allowed to|owns|approves)\b|"
    r"\bwhen (?:is|are|should) .{0,40}\b(?:used|run|posted|closed)\b)"
)
_DATA_QUERY = re.compile(
    r"(?i)\b(?:amounts?|figures?|balances?|trial balances?|tb|ledgers?|accounts?|"
    r"activity|postings?|journals?|billing|invoices?|receivables?|aging|customers?|revenues?|"
    r"expenses?|variances?|budgets?|actuals?|financial statements?|reports?|"
    r"income statements?|balance sheets?|cash flow statements?|p\s*&\s*l|"
    r"profit and loss|profits?|earn(?:ed|ings?)?|sales|margins?|costs?|"
    r"owe[ds]?|owing|due|overdue|past[ -]due|collections?|"
    r"business units?|bu(?:s)?|periods?|fiscal years?|currenc(?:y|ies)|"
    r"exchange rates?|suspense|open items?|debits?|credits?)\b"
)
# A domain NOUN alone does not make a question a data question: "what is our
# travel EXPENSE policy" is pure policy. Requiring an anchor — a request to
# show/quantify, or a concrete scope like a year, period, account or amount —
# is what separates "what is the rule" from "what is the number".
_DATA_ANCHOR = re.compile(
    r"(?i)(?:\bhow (?:much|many)\b|\bshow\b|\blist\b|\bdisplay\b|\brun\b|"
    r"\bcalculat(?:e|ed|ion)\b|\bcomput(?:e|ed)\b|\btotals?\b|\bsum\b|"
    r"\brank\b|\btop \d+\b|\bcompare\b|\bdrill\b|\bbreak ?down\b|"
    r"\bwhat (?:is|are|was|were) (?:the|our|my)? ?(?:balance|total|amount|"
    r"aging|figure|number|variance|position)\b|"
    r"\bwho (?:owes|posted|paid)\b|\bas of\b|\bytd\b|\bmtd\b|\bqtd\b|"
    r"\bperiod \d+\b|\bfy ?\d{2,4}\b|\bfiscal year\b|\bquarter\b|"
    r"\baccount \d+\b|\b\d{4,}\b|[$€£₹]\s?\d|\b\d[\d,]*\.\d{2}\b|"
    r"\b\d{1,3}(?:,\d{3})+\b)"
)
# Naming a period/year/account is not, on its own, a request for a figure —
# "when is period 998 used" is a process question. Alongside policy wording,
# only an explicit ask for a quantity makes a question genuinely mixed.
_DATA_ANCHOR_STRONG = re.compile(
    r"(?i)(?:\bhow (?:much|many)\b|\bshow\b|\blist\b|\bdisplay\b|"
    r"\bcalculat(?:e|ed|ion)\b|\bcomput(?:e|ed)\b|\btotals?\b|\bsum\b|"
    r"\brank\b|\btop \d+\b|\bcompare\b|\bdrill\b|\bbreak ?down\b|"
    r"\bwhat (?:is|are|was|were) (?:the|our|my)? ?(?:balance|total|amount|"
    r"aging|figure|number|variance|position)\b|"
    r"\bwho (?:owes|posted|paid)\b|[$€£₹]\s?\d|\b\d[\d,]*\.\d{2}\b|"
    r"\b\d{1,3}(?:,\d{3})+\b|"
    # A concrete quantity noun ("...and the current suspense BALANCE") is a
    # real figure request; domain nouns like expense/journal/invoice are not,
    # because they occur naturally in policy wording.
    r"\b(?:balances?|amounts?|totals?|aging|open items?)\b)"
)
_QUESTION_DOMAINS = {
    "balance": re.compile(
        r"(?i)\b(?:balances?|trial balances?|tb|activity|postings?|suspense|"
        r"debits?|credits?)\b"
    ),
    "journal": re.compile(r"(?i)\bjournals?\b"),
    "billing": re.compile(r"(?i)\b(?:billing|invoices?)\b"),
    # "owe" is direction-sensitive: money owed TO US is receivables; money
    # WE owe is payables. The blanket owe[ds]? match sent "how much do we
    # owe our vendors" to the AR domain, where no AP tool could ever ground
    # it — the gate then discarded the answer get_open_payables had just
    # produced and told the user it had no PeopleSoft result.
    "ar": re.compile(
        r"(?i)\b(?:receivables?|aging|open items?|"
        r"owes?\s+us|owed\s+to\s+us|who\s+owes|"
        r"due|overdue|past[ -]due|collections?)\b"
    ),
    "ap": re.compile(
        r"(?i)\b(?:payables?|vouchers?|vendors?|suppliers?|"
        r"pay(?:ment)?\s+runs?)\b"
        r"|\b(?:we|do\s+we|should\s+we|how\s+much\s+do\s+we)\s+owe\b"
    ),
    "am": re.compile(
        r"(?i)\b(?:assets?|capitali[sz]\w+|depreciat\w+|"
        r"fixed\s+assets?|retire(?:d|ments?))\b"
    ),
    "pc": re.compile(r"(?i)\bprojects?\b"),
    "customer": re.compile(r"(?i)\bcustomers?\b"),
    "fx": re.compile(r"(?i)\b(?:currenc(?:y|ies)|exchange rates?)\b"),
    "variance": re.compile(
        r"(?i)\b(?:variances?|changed?|movers?|spikes?|drivers?|driving|"
        r"falls?|fell|drops?|declines?|increases?|decreases?)\b"
    ),
    "report": re.compile(
        r"(?i)\b(?:revenues?|expenses?|budgets?|actuals?|"
        r"financial statements?|income statements?|balance sheets?|"
        r"cash flow statements?|p\s*&\s*l|profit and loss|profits?|"
        r"earn(?:ed|ings?)?|sales|margins?|costs?|reports?)\b"
    ),
}
_TOOL_DOMAINS = {
    "get_trial_balance": {"balance", "report"},
    "get_account_balance": {"balance", "variance", "report"},
    "compare_trial_balance": {"balance", "variance", "report"},
    "drill_to_journals": {"journal", "balance", "variance"},
    "tb_integrity_check": {"balance", "journal"},
    "rollup_trial_balance": {"balance", "report"},
    "get_exchange_rate": {"fx"},
    "get_top_billing_customers": {"billing", "customer", "fx"},
    "get_ar_aging": {"ar", "balance", "customer", "fx"},
    "get_customer_ar": {"ar", "balance", "customer", "fx"},
    "search_customers": {"balance", "customer"},
    "get_billing_workbench": {"billing"},
    "run_report": {"report", "balance", "variance"},
    "run_playbook": {"balance", "journal", "ar", "billing", "report"},
    # Module packs and connectors. Domains are what a tool can GROUND, and
    # they are deliberately generous where the tool really computes the
    # fact: open payables reports overdue/due amounts, so it grounds those
    # words even though they also appear in AR questions. The gate is a
    # safety net — a false refusal costs the user the answer a tool just
    # produced; a false accept only means the model routed oddly and the
    # number guard still checks every figure.
    "get_open_payables": {"ap", "ar", "billing", "balance"},
    "get_vendor_payments": {"ap", "report"},
    "get_asset_register": {"am", "report", "balance"},
    "get_project_costs": {"pc", "report", "variance"},
    "get_coupa_invoices": {"ap", "billing"},
    "get_coupa_stuck_approvals": {"ap", "billing"},
    "get_coupa_rni": {"ap", "billing", "balance"},
    "get_coupa_supplier_spend": {"ap", "report"},
    "coupa_to_ap_tie": {"ap", "billing", "balance"},
}


class ScopeConflict(ValueError):
    """The model attempted to override a user-selected request scope."""


def is_policy_tool(tool_name: str) -> bool:
    """Whether a tool reads or inspects the policy/wiki source."""
    return tool_name in POLICY_TOOLS or tool_name.startswith("wiki_")


# Technical vocabulary: the question is about how something WORKS or how to DO
# something — an integration, an interface, a batch job, a setup step. The
# wiki is the PRIMARY source for these (that is where the specs and KB
# articles live), yet they routinely mention billing/customers/invoices,
# which are _DATA_QUERY nouns. Without this signal such questions classified
# as "data" and every wiki tool was hard-blocked — locking the agent out of
# the knowledge base precisely when it was asked to use it.
_TECHNICAL_QUERY = re.compile(
    r"(?i)\b(?:integrat\w+|interfaces?|feeds?|file layouts?|mappings?|"
    r"data ?marts?|stag(?:ing|ed)|extracts?|"
    r"app(?:lication)? ?engine|peoplecode|component interface|"
    r"process scheduler|run ?controls?|batch(?:es| jobs?)?|sqr|"
    r"spec(?:ification)?s?\b|knowledge ?base|\bkbs?\b|"
    r"set ?up|configur\w+|install\w*|implement\w*|"
    r"re-?runs?|re-?ran|restarts?|resubmits?|reprocess\w*|"
    r"troubleshoot\w*|debug\w*|fail(?:s|ed|ing|ure)?|error(?:s|ed)?\b)|"
    r"\bhow (?:does|do|to|is|are)\b|\bsteps? (?:to|for)\b"
)

# A direct request for a FIGURE. Deliberately narrower than
# _DATA_ANCHOR_STRONG: "show me how to rerun the feed" contains "show", but
# it asks for a procedure, not an amount. Only quantity words, money and
# formatted numbers count here.
_FIGURE_ASK = re.compile(
    r"(?i)(?:\bhow (?:much|many)\b|\btotals?\b|\bsum\b|"
    r"\bwhat (?:is|are|was|were)\b.{0,40}\b(?:balance|total|amount|aging|"
    r"figure|variance|position)\b|"
    r"[$€£₹]\s?\d|\b\d[\d,]*\.\d{2}\b|\b\d{1,3}(?:,\d{3})+\b)"
)


def evidence_intent(question: str) -> str:
    """Classify a question for deterministic evidence routing.

    Returns ``policy``, ``data``, ``mixed``, ``technical`` or ``general``.
    A compliance verdict is always mixed because it requires both a rule and
    an actual fact, even when the user did not explicitly say "balance" or
    "amount". ``technical`` marks how-does-it-work / how-do-I questions —
    integrations, interfaces, jobs, setup — whose answer lives in the wiki's
    specs and KB articles: they may read the wiki freely AND the database,
    and no consumer gates on them. The number guard still applies to any
    figure such an answer states.
    """
    text = question or ""
    policy = bool(_POLICY_QUERY.search(text))
    data = bool(_DATA_QUERY.search(text))
    technical = bool(_TECHNICAL_QUERY.search(text))
    if technical and data and not _FIGURE_ASK.search(text):
        # The billing/customer/invoice nouns are the SUBJECT of a technical
        # question ("how does the billing interface load work"), not a
        # request for a figure. Same principle as the policy demotion below.
        data = False
    if policy and data and not _DATA_ANCHOR_STRONG.search(text):
        # Domain vocabulary inside a policy question ("travel expense policy",
        # "who approves a journal over 50k") is not a request for a figure.
        # Treating it as mixed blocked the wiki and refused the answer.
        data = False
    if _VERDICT.search(text):
        # A compliance verdict genuinely needs both halves: rule and figure.
        policy = data = True
    if policy and data:
        return "mixed"
    if policy:
        return "policy"
    if data:
        return "data"
    if technical:
        return "technical"
    return "general"


def requires_financial_evidence(question: str) -> bool:
    """Whether a data question asserts a financial fact, not just metadata.

    Discovery helpers such as ``resolve_period`` can answer calendar questions,
    but they must never authorize a model-written balance, journal, billing, or
    receivables figure. Mixed policy questions already require financial
    evidence independently of this helper.
    """
    return bool(question_financial_domains(question))


def question_financial_domains(question: str) -> set[str]:
    """Financial fact domains explicitly present in a user question."""
    return {
        domain
        for domain, pattern in _QUESTION_DOMAINS.items()
        if pattern.search(question or "")
    }


def financial_tool_domains(tool_name: str) -> set[str]:
    """Fact domains a curated tool can directly ground."""
    return set(_TOOL_DOMAINS.get(tool_name, set()))


def financial_tool_is_relevant(tool_name: str, question: str) -> bool:
    """Whether a successful financial tool addresses the asked fact domain.

    Membership in a broad "financial tools" set is not sufficient evidence:
    an exchange-rate result must not authorize a model-written cash balance,
    and a billing workbench must not ground a journal-status answer.
    """
    tool_domains = financial_tool_domains(tool_name)
    question_domains = question_financial_domains(question)
    return bool(question_domains) and question_domains.issubset(tool_domains)


def _scope_value(field: str, value):
    if value in (None, ""):
        return None
    if field in ("business_unit", "ledger"):
        value = str(value).strip()
        return value or None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number, not a boolean")
    try:
        value = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field} must be an integer") from e
    if value < 0 or value > 9999:
        raise ValueError(f"{field} is outside the supported range")
    # Zero means "use the database default", so it is not a concrete request
    # constraint and should not overwrite a tool's normal default behavior.
    return value or None


def normalize_request_scope(scope: Mapping | None) -> dict:
    """Validate and canonicalize an optional GUI/API request scope.

    Short UI aliases (bu/fy/per) are accepted. Supplying both forms with
    different values is rejected rather than choosing one silently.
    """
    if scope is None:
        return {}
    if not isinstance(scope, Mapping):
        raise ValueError("scope must be an object")
    normalized = {}
    for field, aliases in _SCOPE_ALIASES.items():
        found = []
        for alias in aliases:
            if alias in scope:
                value = _scope_value(field, scope.get(alias))
                if value is not None:
                    found.append(value)
        if not found:
            continue
        first = found[0]
        if any(not _same_scope_value(field, first, other) for other in found[1:]):
            raise ValueError(f"scope supplies conflicting values for {field}")
        normalized[field] = first
    return normalized


def _same_scope_value(field: str, left, right) -> bool:
    if field in ("business_unit", "ledger"):
        return str(left).strip().upper() == str(right).strip().upper()
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return False


_ALL_BU_RE = re.compile(
    # "across BUs" means across BUs whether or not the user says "all" —
    # requiring the word "all" made the scope guard read "compare this
    # across business units" as a single-unit question and block the
    # override the user was explicitly asking for.
    r"(?i)\b(?:across|over)\s+(?:the\s+)?(?:all\s+)?"
    r"(?:bus?|business\s*units?|units?|companies)\b|"
    r"\b(?:for|in|from)\s+all\s+(?:the\s+)?"
    r"(?:bus?|business\s*units?|units?|companies)\b|"
    r"\ball\s+business\s*units?\b|\bevery\s+(?:bu|business\s*unit)\b|"
    r"\bcross[- ]bu\b|"
    r"\b(?:company|enterprise|organi[sz]ation)-?\s?wide\b")


def wants_all_business_units(question: str) -> bool:
    """The question itself asks to cross business units.

    The selected scope pins one BU so an answer can never silently mix
    units. But "across all business units" IS the user changing that scope,
    in words, for one question — and refusing it turned an explicit request
    into a per-unit crawl that ran out of model rounds before it ran out of
    units. Detection is textual and conservative: only phrasings that name
    the crossing unlock it.
    """
    return bool(_ALL_BU_RE.search(question or ""))


def apply_request_scope(tool_name: str, args: Mapping | None,
                        request_scope: Mapping | None,
                        allow_bu_override: bool = False) -> dict:
    """Return tool arguments with the validated request scope enforced.

    Empty model arguments are treated as omissions and receive the request
    value. An explicit conflicting value raises :class:`ScopeConflict`.
    ``list_financial_scopes`` is intentionally never constrained, so discovery
    questions such as "show all BUs" always see the full authorized catalog.
    """
    out = dict(args or {})
    if tool_name == "list_financial_scopes":
        return out
    scope = normalize_request_scope(request_scope)
    # run_sql is NOT refused under a scope. Refusing it made every ad-hoc and
    # custom-record question ("list the files configured in PS_TU_FILE_INTFC")
    # impossible in the GUI, where a scope is always active. Instead the
    # active business unit is passed down so the result can state plainly
    # whether the query was restricted to it — disclosure, not a blockade.
    supported = _TOOL_SCOPE_ARGS.get(tool_name, {})
    for field, tool_arg in supported.items():
        if field not in scope:
            continue
        requested = scope[field]
        current = out.get(tool_arg)
        current_value = _scope_value(field, current)
        if current_value is None:
            out[tool_arg] = requested
        elif not _same_scope_value(field, current_value, requested):
            if field in _SOFT_SCOPE_FIELDS:
                # Time is a DEFAULT, not a lock. "Show the trial balance for
                # period 3" while the chip reads P6 is a legitimate question,
                # and refusing it made the selected period a cage: no other
                # period or year could be asked about without changing the
                # scope first. The model's explicit value wins.
                continue
            if field == "business_unit" and allow_bu_override:
                # The user's own words crossed the units ("across all
                # business units"), so the chip's BU is their default, not
                # their answer. The model's explicit value — another unit,
                # or "ALL" — is honoured for this turn only; absent that
                # phrasing the conflict below still refuses.
                continue
            raise ScopeConflict(
                f"{tool_name}.{tool_arg}={current!r} conflicts with the "
                f"request scope {field}={requested!r}"
            )
    return out


def tool_result_status(tool_name: str, content: str) -> tuple[bool, str]:
    """Return whether a tool result is usable evidence and, if not, why."""
    raw = content or ""
    if raw.startswith("TOOL ERROR"):
        return False, raw[:240]
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # MCP may return a non-JSON text result. Absence of an explicit tool
        # error still means the call completed; the model can read the text.
        return True, ""
    if not isinstance(payload, dict):
        return True, ""
    if payload.get("error"):
        return False, str(payload["error"])[:240]
    status = payload.get("scope_status")
    if status and status != "ok":
        return False, str(payload.get("detail") or status)[:240]
    if payload.get("control_status") == "not_run":
        return False, str(payload.get("summary") or "control did not run")[:240]
    # Evidence is judged on STRUCTURED fields only. Scanning prose for "no
    # data" failed every successful run_report, whose note legitimately
    # explains that "'—' means the ledger has no data for that column's
    # scope" — the whole financial-statement pack became unanswerable.
    if payload.get("no_data") is True:
        return False, str(payload.get("detail") or "no data for this scope")[:240]
    if is_policy_tool(tool_name) and payload.get("demo_content_warning"):
        return False, str(payload["demo_content_warning"])[:240]
    if tool_name == "wiki_lookup":
        if not payload.get("passages"):
            return False, "wiki lookup returned no supporting passages"
    return True, ""


def promises_tool_call(text: str) -> bool:
    """Did the model say it would call a tool instead of calling one?"""
    return bool(_PROMISE.search(text or ""))


def unevidenced_verdict(answer: str, tools_used: set) -> str:
    """Return the missing evidence side when a compliance verdict is not backed
    by both a policy lookup and a data lookup; empty string when it is fine."""
    if not _VERDICT.search(answer or ""):
        return ""
    had_policy = bool(set(tools_used) & POLICY_EVIDENCE_TOOLS)
    had_data = any(any(h in t for h in _DATA_HINTS)
                   for t in set(tools_used) - POLICY_TOOLS)
    if had_policy and had_data:
        return ""
    return "the policy text" if not had_policy else "the actual figure from the ledger"

# --------------------------------------------------------- number grounding
# Money-shaped and other substantive figures the model states in prose. The
# prompt already forbids inventing them and the verdict guard catches
# unevidenced judgements, but neither MECHANICALLY prevents a fabricated
# amount from reaching the user. This does: every figure in the answer must
# appear in a tool payload from the same turn, or the answer is refused.
_FIGURE = re.compile(r"(?<![\w.])-?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w])"
                     r"|(?<![\w.])-?\d+\.\d{2,}(?![\w])")

# Values that are never "figures from the ledger": years, fiscal periods,
# account numbers, percentages and small counts the model may legitimately
# derive (how many rows it is describing).
_FIGURE_EXEMPT = re.compile(
    r"(?i)(?:FY\s*|fiscal year\s*|period\s*|P)\d{1,4}\b"
    r"|\b(?:19|20)\d{2}\b"
    r"|\d+(?:\.\d+)?\s*%"
)


def _numeric_key(text: str) -> str:
    """Canonical form so 1,234.50 / 1234.5 / -1234.500 compare equal."""
    cleaned = text.replace(",", "").lstrip("+")
    negative = cleaned.startswith("-")
    cleaned = cleaned.lstrip("-")
    try:
        value = float(cleaned)
    except ValueError:
        return text
    if value == int(value):
        body = str(int(value))
    else:
        body = ("%.6f" % value).rstrip("0").rstrip(".")
    return ("-" if negative and value else "") + body


def payload_numbers(payloads) -> set:
    """Every number appearing anywhere in this turn's tool results."""
    found: set = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            found.add(_numeric_key(str(node)))
        elif isinstance(node, str):
            for match in re.findall(r"-?\d[\d,]*(?:\.\d+)?", node):
                found.add(_numeric_key(match))

    for raw in payloads or []:
        if isinstance(raw, str):
            try:
                walk(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                for match in re.findall(r"-?\d[\d,]*(?:\.\d+)?", raw):
                    found.add(_numeric_key(match))
        else:
            walk(raw)
    # Ground unit-scaled RESTATEMENTS of payload figures. "$4.55M" for a
    # payload 4,548,123.45 is the same fact at a coarser unit, not an
    # invented number — and withholding a correct aging answer over it
    # taught the user that nothing works. For every payload figure large
    # enough to restate, the thousand/million/billion forms at one and two
    # decimals are grounded too.
    for key in list(found):
        try:
            value = float(key)
        except ValueError:
            continue
        for divisor in (1e3, 1e6, 1e9):
            if abs(value) >= divisor:
                for digits in (0, 1, 2):
                    found.add(_numeric_key(str(round(value / divisor, digits))))
    return found


def ungrounded_figures(answer: str, payloads) -> list:
    """Figures stated in the answer that no tool result supports.

    Rounding is tolerated in the direction a human would read it: a figure
    matches if it appears exactly, or if a payload number rounds to it at the
    stated precision (908,846.06 -> "908,846" or "908.85 thousand" style
    restatements are NOT invented, they are the same fact).
    """
    grounded = payload_numbers(payloads)
    if not grounded:
        return []
    exempt_spans = [m.span() for m in _FIGURE_EXEMPT.finditer(answer or "")]

    def inside_exempt(span) -> bool:
        return any(a <= span[0] and span[1] <= b for a, b in exempt_spans)

    missing: list = []
    for match in _FIGURE.finditer(answer or ""):
        if inside_exempt(match.span()):
            continue
        text = match.group(0)
        key = _numeric_key(text)
        if key in grounded:
            continue
        # Ledger amounts are SIGNED (credits negative) and the prompt tells
        # the model to present them the way accountants read them — positive
        # with a DR/CR side. So a payload's -23,400.00 legitimately appears as
        # "23,400.00 CR" in prose. Compare magnitudes too, or the guard
        # punishes the model for following its instructions.
        if key.lstrip("-") in {g.lstrip("-") for g in grounded}:
            continue
        # tolerate a rounded restatement of a grounded value
        try:
            stated = float(text.replace(",", ""))
        except ValueError:
            continue
        decimals = len(text.split(".")[1]) if "." in text else 0
        if any(round(abs(float(g)), decimals) == round(abs(stated), decimals)
               for g in grounded if _is_number(g)):
            continue
        missing.append(text)
    return missing


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------ rate grounding
# Percentages are the LEAST protected numbers here and the likeliest to be
# invented. _FIGURE_EXEMPT deliberately lets them past the withhold guard,
# and that exemption must stay: _FIGURE matches "25.00" inside "25.00%", and
# the prompt mandates two-decimal formatting, so deleting it would withhold
# every correctly formatted rate answer on day one. The cost of the
# exemption is that "the standard rate is 18%" — a figure recalled from
# training data and presented as this company's configured rate — reaches
# the user with nothing objecting.
#
# So rates get a SEPARATE scanner with its own grounding set, which appends
# a caveat and NEVER withholds. A wrong rate is a sentence the user can
# check; a withheld answer is a product that looks broken.
#
# Grounding is DECLARED-ONLY, and that restraint is the design. The obvious
# alternative -- derive percentages from ratios of payload numbers -- was
# implemented and measured while designing this: over one realistic
# 29-scalar finance payload it grounded 62 of the 101 integer percentages,
# including the fabricated 18 and the 25 it existed to question. A grounding
# rule that authorises the fabrication it was built to catch is worse than
# no rule, because it looks like protection.
_RATE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d+)?)\s*(?:%|percent\b)")

# A key whose NAME declares its value is a PERCENT. The producer asserts the
# unit; nothing is inferred.
#
# "pct"/"percent" may sit anywhere in the key (pct_used, percent_complete,
# share_pct, change_percent) — this repo produces both orders.
#
# A bare "rate" is deliberately NOT a percent, and that exclusion is the
# whole reason this is an allowlist. get_exchange_rate emits
# {"rate": 18.34567891} (engine.py) — an FX multiplier — and USD/MXN sitting
# near 18 grounded the exact "the standard rate is 18%" fabrication this
# layer exists to catch. Verified before the fix: a single FX call anywhere
# in the payload window silenced the caveat completely. Only the *_rate
# names that really carry percentages are admitted.
_RATE_KEY = re.compile(
    r"(?i)(?:^|_)(?:pct|percent)(?:_|$)"
    r"|(?:^|_)(?:tax|vat|gst|sales_tax|discount|withholding|interest"
    r"|growth|margin|utilization)_rate$")

_RHETORICAL_RATES = {"0", "100"}

PAYLOAD_DECLARED = "payload"
USER_STATED = "user_stated"


def payload_rates(payloads, question: str = "") -> dict:
    """Percentages the machinery DECLARED, plus the ones the user typed.

    Returns {canonical rate: source}. A payload declaration outranks the
    user's own figure, so a rate the ledger actually produced is never
    downgraded to "your number".

    Deliberately independent of payload_numbers(), which is left untouched:
    that set already carries thousand/million/billion restatements at 0-2
    decimals, so small values are dense in it and deriving rates from it
    would inherit exactly the pollution described above.
    """
    rates: dict = {}

    def note(value, source: str) -> None:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return
        # Key on MAGNITUDE, exactly as the figure guard compares signed
        # ledger amounts (see ungrounded_figures). A declared change_pct of
        # -21.84 is written in prose as "down 21.84%", and _RATE has no sign
        # group, so a signed key would false-caveat every DECLINING
        # percentage this repo computes.
        key = _numeric_key(str(num)).lstrip("-")
        if rates.get(key) != PAYLOAD_DECLARED:
            rates[key] = source

    def note_leaves(node) -> None:
        # A percent-named key may hold a MAP or a LIST of percentages, not
        # just one scalar: get_ar_aging declares bucket_share_pct as
        # {"current": 26.01, "1-30": 51.23, ...}. Requiring a scalar there
        # caveated a share the tool had just computed. The key still has to
        # name the unit — this widens what a declaration may CONTAIN, never
        # what counts as one.
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            note(node, PAYLOAD_DECLARED)
        elif isinstance(node, dict):
            for value in node.values():
                note_leaves(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                note_leaves(value)

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if _RATE_KEY.search(str(key)):
                    note_leaves(value)
                elif str(key) == "ratios" and isinstance(value, list):
                    # An explicit producer declaration: "this numerator over
                    # this denominator is this percent, on this basis". The
                    # tool did the arithmetic and owns the basis; the guard
                    # never divides two numbers itself.
                    for entry in value:
                        if not isinstance(entry, dict):
                            continue
                        if isinstance(entry.get("pct"), (int, float)):
                            note(entry["pct"], PAYLOAD_DECLARED)
                            continue
                        num = entry.get("numerator")
                        den = entry.get("denominator")
                        if (isinstance(num, (int, float))
                                and isinstance(den, (int, float)) and den):
                            note(num / den * 100.0, PAYLOAD_DECLARED)
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    for raw in payloads or []:
        if isinstance(raw, str):
            try:
                walk(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                continue
        else:
            walk(raw)

    # The user's own figure grounds a restatement of itself. Without this,
    # answering "our GST works out to 25% - is that right?" gets caveated
    # for repeating the number the controller just typed, which reads as
    # the system calling them a liar.
    for match in _RATE.finditer(question or ""):
        note(match.group(1), USER_STATED)
    return rates


def rate_findings(answer: str, payloads, question: str = "") -> list:
    """Rates stated in the answer that no tool result declared.

    Each finding is {"rate": as written, "source": None | "user_stated"}.
    A rate the machinery declared produces no finding at all.
    """
    if not answer:
        return []
    rates = payload_rates(payloads, question)
    findings: list = []
    seen: set = set()
    for match in _RATE.finditer(answer):
        stated_text = match.group(1)
        key = _numeric_key(stated_text)
        if key in seen:
            continue
        seen.add(key)
        # 0% and 100% are idiom far more often than measurement — "I am
        # 100% confident", "0% of invoices are disputed" — and no
        # fabricated benchmark ever looks like either. Flagging them was
        # pure noise, and noise is what teaches people to skip the caveat
        # that mattered.
        if key in _RHETORICAL_RATES:
            continue
        source = rates.get(key)
        if source is None:
            # Tolerate a rounded restatement of a declared rate, the same
            # way the figure guard does: a payload 8.05 reported as "8.1%"
            # is the same fact, not an invention.
            try:
                stated = float(stated_text)
            except ValueError:
                continue
            decimals = (len(stated_text.split(".")[1])
                        if "." in stated_text else 0)
            for grounded, grounded_source in rates.items():
                if not _is_number(grounded):
                    continue
                if (round(abs(float(grounded)), decimals)
                        == round(abs(stated), decimals)):
                    source = grounded_source
                    break
        if source == PAYLOAD_DECLARED:
            continue
        findings.append({"rate": match.group(0).strip(), "source": source})
    return findings


def rate_caveat(findings) -> str:
    """One bracketed clause covering every unverified rate in an answer.

    One clause, never several: a paragraph of warnings trains the reader to
    skip all of them, including the one that mattered.
    """
    if not findings:
        return ""
    unsourced = [f["rate"] for f in findings if not f.get("source")]
    from_user = [f["rate"] for f in findings
                 if f.get("source") == USER_STATED]
    def listed(rates: list) -> str:
        # Same shape as the money guard's message: naming three and
        # silently dropping the rest understates the problem.
        return ", ".join(rates[:3]) + (" and others" if len(rates) > 3 else "")

    parts: list = []
    if unsourced:
        parts.append(
            listed(unsourced)
            + " did not come from any tool result this turn — treat "
            + ("it" if len(unsourced) == 1 else "them")
            + " as unconfirmed rather than a rate retrieved from your data.")
    if from_user:
        parts.append(
            listed(from_user)
            + " is the figure you gave me; I did not retrieve or verify "
              "it against the ledger."
            if len(from_user) == 1 else
            listed(from_user)
            + " are figures you gave me; I did not retrieve or verify "
              "them against the ledger.")
    return "[" + " ".join(parts) + "]"
