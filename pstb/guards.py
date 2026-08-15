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
    "explain_balance_change",
    "drill_to_journals",
    "tb_integrity_check",
    "detect_transaction_anomalies",
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
    "get_budget_variance",
    "run_ps_query",
    "coupa_budget_variance",
    "get_coupa_budget_lines",
    "get_invoice_lifecycle",
    "get_dso_trend",
    "get_cash_outlook",
    "get_vendor_intelligence",
    "get_customer_intelligence",
    "get_customer_financial_360",
    "get_vendor_payables_network",
    "search_vendors",
    "get_match_exceptions",
    "get_procurement_chain",
    "get_entity_network",
    "get_concentration",
    "get_invoice_totals",
    "get_duplicate_payments",
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
    "explain_balance_change": {
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
    "detect_transaction_anomalies": {
        "business_unit": "business_unit", "as_of_date": "as_of_date",
    },
    "rollup_trial_balance": {
        "business_unit": "business_unit", "ledger": "ledger",
        "fiscal_year": "fiscal_year", "period": "period",
    },
    "get_top_billing_customers": {"business_unit": "business_unit",
                                "as_of_date": "as_of_date"},
    "get_ar_aging": {"business_unit": "business_unit",
                   "as_of_date": "as_of_date"},
    "get_customer_ar": {"business_unit": "business_unit",
                      "as_of_date": "as_of_date"},
    "search_customers": {"business_unit": "business_unit",
                       "as_of_date": "as_of_date"},
    "get_billing_workbench": {"business_unit": "business_unit",
                            "as_of_date": "as_of_date"},
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
    "get_budget_variance": {
        "business_unit": "business_unit", "ledger": "ledger",
        "fiscal_year": "fiscal_year", "period": "period",
    },
    "coupa_budget_variance": {
        "business_unit": "business_unit", "fiscal_year": "fiscal_year",
        "period": "period",
    },
    "get_invoice_lifecycle": {"business_unit": "business_unit",
                            "as_of_date": "as_of_date"},
    "get_dso_trend": {"business_unit": "business_unit",
                      "fiscal_year": "fiscal_year"},
    "get_cash_outlook": {"business_unit": "business_unit",
                       "as_of_date": "as_of_date"},
    "get_vendor_intelligence": {"business_unit": "business_unit",
                              "as_of_date": "as_of_date"},
    "get_customer_intelligence": {"business_unit": "business_unit",
                                "as_of_date": "as_of_date"},
    "get_customer_financial_360": {"business_unit": "business_unit",
                                 "as_of_date": "as_of_date"},
    "get_vendor_payables_network": {"business_unit": "business_unit",
                                  "as_of_date": "as_of_date"},
    "get_match_exceptions": {"business_unit": "business_unit",
                             "as_of_date": "as_of_date"},
    "get_entity_network": {"business_unit": "business_unit"},
    "get_concentration": {"business_unit": "business_unit"},
    "get_entity_connection": {"business_unit": "business_unit"},
    "get_procurement_chain": {"business_unit": "business_unit",
                              "as_of_date": "as_of_date"},
    "search_vendors": {"business_unit": "business_unit",
                     "as_of_date": "as_of_date"},
    "get_invoice_totals": {"business_unit": "business_unit",
                           "fiscal_year": "fiscal_year"},
    "get_duplicate_payments": {"business_unit": "business_unit",
                             "as_of_date": "as_of_date"},
    "get_open_payables": {"business_unit": "business_unit",
                        "as_of_date": "as_of_date"},
    "get_vendor_payments": {"business_unit": "business_unit",
                          "as_of_date": "as_of_date"},
    "get_asset_register": {"business_unit": "business_unit",
                         "as_of_date": "as_of_date"},
    "get_project_costs": {"business_unit": "business_unit",
                        "as_of_date": "as_of_date"},
}

# Tools that understand business_unit="ALL" as "every unit, each row
# labelled with its own". Only these may widen past the selected unit;
# anywhere else "ALL" would be taken for a literal unit name and quietly
# return nothing.
BU_ALL_TOOLS = {"get_top_billing_customers"}

# Tools that read PeopleSoft data without naming a business unit, so a
# restricted user reaching them would see every unit's rows. They are not
# refused outright — most are catalogs or shape lookups with no figures in
# them — but the ad-hoc ones are, below.
_UNSCOPED_DATA_TOOLS = {"run_sql", "run_ps_query"}
# Structure, never amounts: a process trace must not satisfy the
# grounding guard's demand for evidence behind a figure.
STRUCTURAL_TOOLS = {"trace_process", "describe_process_graph",
                    "get_record_map", "join_path",
                    "describe_metadata_catalog", "search_metadata",
                    "get_metadata_context"}


def filter_scope_payload(tool_name: str, payload: str, access) -> str:
    """Narrow a scope-catalog RESULT to the units this person holds.

    The catalog tool runs inside the MCP server, which has no identity —
    it is one shared subprocess answering every session — so the filtering
    has to happen here, on the way back, where the caller is known. Without
    it a restricted user's model is handed the full list of unit names and
    then blocked from every one it tries: the names leak and the turn
    burns its rounds discovering that.
    """
    if access is None or getattr(access, "all_units", True):
        return payload
    if tool_name != "list_financial_scopes" or not isinstance(payload, str):
        return payload
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return payload
    if not isinstance(data, dict) or "scopes" not in data:
        return payload
    kept = [s for s in (data.get("scopes") or [])
            if access.allows((s or {}).get("business_unit"))]
    dropped = len(data.get("scopes") or []) - len(kept)
    data["scopes"] = kept
    if dropped:
        # Disclosed, never silent: the model has to know the catalog it is
        # reasoning over is partial, or it will describe it as complete.
        data["note"] = ((data.get("note") or "") + f" Filtered to the "
                        f"{len(kept)} business unit(s) {access.oprid} is "
                        f"authorised for; {dropped} other(s) exist and are "
                        "not shown.").strip()
        data["access_filtered"] = True
    return json.dumps(data)


def unit_access_block(tool_name: str, args, access,
                      allow_raw_sql: bool = False) -> str:
    """Why this call must not run for this person, or "" to allow it.

    The second gate, behind the scope lock. The scope lock asks "does this
    match what the user selected"; this asks "is the user allowed to
    select it at all", and they are different questions: a person can type
    a unit they were never granted into a question, and the model will
    faithfully pass it through.

    Returns a REASON rather than raising, because the agent loop already
    has a blocked-with-remedy path that puts the refusal in front of the
    model — which then re-asks within the units the person does have,
    instead of the turn dying.
    """
    if access is None or getattr(access, "all_units", True):
        return ""
    if tool_name == "list_financial_scopes":
        return ""                       # the catalog is filtered at source
    if tool_name in _UNSCOPED_DATA_TOOLS and not allow_raw_sql:
        # Ad-hoc SQL and a saved PSQuery both choose their own WHERE
        # clause, so no argument check can bound them to a unit. Rather
        # than pretend otherwise, they are off for a restricted user and
        # the refusal says which curated tools answer the same question.
        return (
            f"{tool_name} is not available to {access.oprid}: it runs "
            "arbitrary SQL, which cannot be limited to the business units "
            f"PeopleSoft grants this user ({', '.join(sorted(access.units)) or 'none'}). "
            "Use the curated tools — they carry the unit and are filtered.")
    for key in ("business_unit", "bu"):
        value = (args or {}).get(key)
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        if text.upper() in _BU_ALL_VALUES:
            # "ALL" is not a unit anyone was denied, so this loop used to
            # wave it through as harmless. It is not harmless: it means
            # every unit that EXISTS, and a restricted caller got another
            # company's customers and amounts out of the cross-unit ranking.
            #
            # The in-process filter (pstb.security.allowed_units) narrows it
            # for the GUI's own endpoints. It cannot help HERE, because this
            # gate stands in front of an MCP server running in a SEPARATE
            # PROCESS that has no way to know who is asking — and passing
            # the grant as a tool argument would hand the model a value it
            # could widen. So on this path the answer is to refuse, and to
            # name the units that would work.
            mine = ", ".join(sorted(access.units))
            return (
                f"{tool_name} was asked for ALL business units, and "
                f"{access.oprid} is granted only {mine or 'none'}. A "
                "cross-unit ranking cannot be limited to those units on "
                "this path, so it is refused rather than quietly narrowed."
                + (f" Ask for one of: {mine}." if mine else ""))
        if not access.allows(text):
            return access.refusal(text)
    return ""
_BU_ALL_VALUES = {"ALL", "*"}

# WHICH scope fields are governance and which are convenience.
# business_unit/ledger are HARD: answering from the wrong company or ledger is
# the failure the scope bar exists to prevent, so the model may never change
# them. fiscal_year/period are SOFT defaults the user's question may override.
# Time is a DEFAULT, not a lock: "what does C1001 owe today" while the
# chip reads P6 is a legitimate question. An explicit tool argument wins,
# and the resolved date behaves the same way as the period it came from.
_SOFT_SCOPE_FIELDS = {"fiscal_year", "period", "as_of_date"}

_SCOPE_ALIASES = {
    "business_unit": ("business_unit", "bu"),
    "ledger": ("ledger",),
    "fiscal_year": ("fiscal_year", "fy"),
    "period": ("period", "per"),
    # The selected period, resolved to the DATE the subledger tools
    # actually take. Ledger tools filter by FISCAL_YEAR and ACCOUNTING_
    # PERIOD; AR, Billing and AP filter by a date, so the chip's period
    # reached the ledger and stopped at the boundary. Someone could select
    # FY2025 P12 and read this month's receivables beside it.
    "as_of_date": ("as_of_date", "as_of"),
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
    r"activity|postings?|journals?|general ledger|gl|billing|invoices?|"
    r"receivables?|ar|aging|customers?|revenues?|"
    r"payables?|accounts payable|ap|vouchers?|vendors?|suppliers?|"
    r"payments?|disbursements?|accru(?:e[ds]?|al|als|ed|ing)|grni|rni|"
    r"expenses?|variances?|budgets?|actuals?|financial statements?|reports?|"
    r"income statements?|balance sheets?|cash flow statements?|p\s*&\s*l|"
    r"profit and loss|profits?|earn(?:ed|ings?)?|sales|margins?|costs?|"
    r"owe[ds]?|owing|due|overdue|past[ -]due|collections?|"
    r"business units?|bu(?:s)?|periods?|fiscal years?|currenc(?:y|ies)|"
    r"exchange rates?|suspense|open items?|debits?|credits?|"
    r"close readiness|ready to close|month[ -]end close|year[ -]end close)\b"
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
        r"debits?|credits?|general ledger|gl|close readiness|"
        r"ready to close|month[ -]end close|year[ -]end close)\b"
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
        r"(?i)\b(?:payables?|accounts payable|ap|vouchers?|vendors?|suppliers?|"
        r"payments?|disbursements?|pay(?:ment)?\s+runs?|"
        r"accru(?:e[ds]?|al|als|ed|ing)|"
        r"grni|rni)\b"
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
    "explain_balance_change": {"balance", "variance", "report",
                               "ar", "ap", "am", "pc",
                               "customer", "fx", "journal"},
    "drill_to_journals": {"journal", "balance", "variance"},
    "tb_integrity_check": {"balance", "journal"},
    "detect_transaction_anomalies": {
        # Operational telemetry can ground an anomaly/variance statement, but
        # a clean broad scan is not evidence for an AR/AP/asset/project policy
        # conclusion about a specific balance or transaction.
        "variance"},
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
    "run_ps_query": {"balance", "report", "ar", "ap", "billing",
                     "journal", "variance", "customer"},
    "get_budget_variance": {"balance", "report", "variance"},
    "coupa_budget_variance": {"balance", "report", "variance", "ap"},
    "get_coupa_budget_lines": {"report", "ap"},
    "get_invoice_lifecycle": {"billing", "ar", "report"},
    "get_dso_trend": {"ar", "balance", "report", "variance"},
    "get_cash_outlook": {"ar", "ap", "balance", "report"},
    "get_vendor_intelligence": {"ap", "report"},
    "get_customer_intelligence": {"billing", "customer", "ar", "report",
                                  "fx"},
    "get_customer_financial_360": {"billing", "customer", "ar", "report",
                                   "balance", "fx"},
    "get_vendor_payables_network": {"ap", "report", "balance"},
    "get_match_exceptions": {"ap", "report", "balance"},
    "get_entity_network": {"billing", "customer", "ar", "ap", "report"},
    "get_concentration": {"billing", "customer", "ar", "ap", "report",
                          "balance"},
    "get_entity_connection": {"billing", "customer", "ap", "report"},
    "get_procurement_chain": {"ap", "report", "balance"},
    "trace_process": {"balance", "report", "ar", "ap", "billing",
                      "journal", "customer", "fx", "variance"},
    "search_vendors": {"ap", "balance"},
    "get_invoice_totals": {"billing", "report", "balance"},
    "get_duplicate_payments": {"ap", "report"},
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


# Reconciliation phrasings: "did X land in AP", "does the subledger tie",
# "matched against". These are DATA questions even when they contain
# "approved" — approving an invoice is a transaction event, not a policy —
# and classifying them as policy replaced a grounded tie-out answer with a
# wiki refusal. An explicit policy word still wins.
_RECON_QUERY = re.compile(
    r"(?i)\b(?:tie[sd]?\s+(?:out|to)|reconcil\w+|"
    r"land(?:ed)?\s+in|reach(?:ed)?\s+(?:ap|ar|gl)\b|"
    r"match(?:ed)?\s+(?:to|against)|make\s+it\s+(?:in)?to)")
_EXPLICIT_POLICY_WORD = re.compile(
    r"(?i)\b(?:polic(?:y|ies)|procedure|rule|guideline|threshold|"
    r"checklist|complian\w+)\b")

# Metadata discovery is an evidence-gathering task even when its subject is
# an approval/status concept.  Treating "which record has the approval status
# field?" as a policy question kept Gemini away from the catalog tools — a
# particularly costly failure for custom, company-prefixed records.
_CATALOG_QUERY = re.compile(
    r"(?i)(?:\b(?:find|search|locate|which|what|show|describe|compare|profile)\b"
    r".{0,80}\b(?:records?|tables?|fields?|columns?)\b|"
    r"\b(?:records?|tables?)\b.{0,80}\b(?:used|holds?|contains?|live|"
    r"staging|history|historical|physical)\b)"
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
    if _RECON_QUERY.search(text) and not _EXPLICIT_POLICY_WORD.search(text):
        return "data"
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
    # Metadata wording is technical only after the stronger reconciliation,
    # verdict and data rules have had their say.  Putting this first allowed
    # "which tables are out of balance?" to turn off the financial-evidence
    # gate; it also stole domain-free hard overrides such as "which records
    # reconcile?" and "which records show we are compliant?".  Explicit
    # custom-object asks still arrive here with data demoted by the technical
    # rule above, and approval/status field discovery remains technical.
    if (_CATALOG_QUERY.search(text)
            and not _FIGURE_ASK.search(text)
            and not data):
        return "technical"
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


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _scope_value(field: str, value):
    if value in (None, ""):
        return None
    if field == "as_of_date":
        value = str(value).strip()[:10]
        if not value:
            return None
        if not _ISO_DATE.match(value):
            raise ValueError("as_of_date must be YYYY-MM-DD")
        return value
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


def units_named_in(question: str, known_units) -> list:
    """Business units this question NAMES, matched against the real catalog.

    Matching against the catalog rather than a pattern, because a unit code
    is whatever the site made it — US001, CAN, 10500, EMEA_SHARED — and any
    regex for "looks like a business unit" is either blind to half of them
    or matches every account number in the sentence.
    """
    text = (question or "").upper()
    found = []
    for unit in known_units or ():
        code = str(unit or "").strip().upper()
        # Bounded so US001 does not match inside US0012, and so a two-letter
        # code does not match inside an ordinary word.
        if code and re.search(rf"(?<![A-Z0-9_]){re.escape(code)}(?![A-Z0-9_])",
                              text):
            found.append(code)
    return sorted(set(found))


def spans_business_units(question: str, known_units=(),
                         selected_unit: str = "") -> bool:
    """Does answering this question require crossing business units?

    Two ways it can, and only one was detected before:

      1. The user SAYS the crossing — "across all business units",
         "company-wide". That is wants_all_business_units().
      2. The user NAMES the units — "compare US001 and CA001", or names a
         single unit that is not the one selected in the chip.

    The second is the common shape and it silently failed: the scope lock
    pinned the selected unit, the model dutifully called a single-unit tool
    with it, and the answer covered one of the two units the person asked
    about while looking exactly as confident as a correct one.
    """
    if wants_all_business_units(question):
        return True
    named = units_named_in(question, known_units)
    if len(named) > 1:
        return True
    selected = (selected_unit or "").strip().upper()
    return bool(named and selected and named[0] != selected)


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
            if (field == "business_unit"
                    and tool_name in BU_ALL_TOOLS
                    and str(current).strip().upper() in _BU_ALL_VALUES):
                # "ALL" is not a different company — it is a SUPERSET that
                # contains the selected one, on a tool built to rank across
                # units and label every row with its own. The scope lock
                # exists to stop an answer about US200 being presented as
                # US001; widening to every unit, with the unit visible per
                # row, is not that failure.
                #
                # Refusing it was a live false positive: the tool's own
                # docstring tells the model that business_unit="ALL" ranks
                # across every unit, so on a larger top-N — where one unit
                # may not hold enough customers — the model took the advice
                # and the guard refused its own instruction.
                # Nothing extra is injected here: `out` becomes the tool's
                # arguments, and an unknown key would be rejected by MCP.
                # The disclosure is the payload's own business_unit="ALL"
                # plus the per-row unit, which the answer guards can see.
                out[tool_arg] = "ALL"
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


# ------------------------------------------------------- source attribution
# The number guard above asks "does this figure exist in a tool result". It
# does not ask WHICH tool, and that gap is a real one: a Coupa commitment
# quoted as a general-ledger balance is fully grounded today, and so is a
# figure typed into a Confluence page an AP clerk can edit, because wiki
# passages are tool payloads like any other.
#
# Both are the same defect — grounding is source-blind — so both are fixed
# by the same thing: carry the producing tool alongside each number and let
# a second scanner object when a sentence names one source and the figure
# came from another.
#
# This is a CAVEAT layer, never a withhold. The withhold path deals in
# figures that exist nowhere, which is unambiguous. Attribution reads
# English prose to decide what a sentence claimed, and prose is arguable, so
# the cost of being wrong has to stay at one bracketed clause the reader can
# dismiss — not a blanked answer.

# Which system's authority a tool's numbers carry. This mapping is a
# JUDGEMENT and belongs in code review, not in inference: run_sql and
# run_playbook are peoplesoft_gl because they read the PeopleSoft database
# THIS deployment points at, and that stops being true the day sources.py
# aims a named source at some other system. Revisit it then.
_SOURCE_OF_TOOL = {
    "peoplesoft_gl": {
        "get_trial_balance", "get_account_balance", "compare_trial_balance",
        "rollup_trial_balance", "drill_to_journals", "tb_integrity_check",
        "explain_balance_change",
        "run_report", "get_budget_variance", "get_exchange_rate",
        "get_tree_node_accounts", "search_accounts", "run_sql",
        "run_playbook",
    },
    "peoplesoft_operations": {"detect_transaction_anomalies"},
    "peoplesoft_ar": {
        "get_ar_aging", "get_customer_ar", "get_invoice_totals",
        "get_top_billing_customers", "get_billing_workbench",
        "get_dso_trend", "get_cash_outlook", "get_invoice_lifecycle",
        "get_customer_intelligence", "get_customer_financial_360",
        "search_customers",
    },
    "peoplesoft_ap": {
        "get_open_payables", "get_duplicate_payments", "get_vendor_payments",
        "get_vendor_intelligence", "get_asset_register", "get_project_costs",
        "get_vendor_payables_network", "search_vendors",
        "get_match_exceptions", "get_procurement_chain",
        "get_entity_network", "get_concentration",
        "get_entity_connection",
    },
    "peoplesoft_query": {"run_ps_query"},
    "coupa": {
        "get_coupa_invoices", "get_coupa_stuck_approvals", "get_coupa_rni",
        "get_coupa_supplier_spend", "get_coupa_budget_lines",
        "coupa_budget_variance", "coupa_to_ap_tie",
    },
    "wiki": set(POLICY_EVIDENCE_TOOLS),
}
_TOOL_SOURCE = {tool: label
                for label, tools in _SOURCE_OF_TOOL.items()
                for tool in tools}

# Which SYSTEM each source belongs to. Rule B fires across this boundary
# and never inside it, and that restraint was bought with a false positive:
# get_dso_trend correctly noted "DSO is computed from the ledger only", the
# word "ledger" read as a general-ledger claim, and every DSO figure was
# caveated as "receivables, not the ledger". Both are PeopleSoft. The
# sub-ledger/GL line is blurry in prose and a controller does not care
# where inside PeopleSoft a PeopleSoft number came from — what they cannot
# afford to miss is a Coupa commitment or a wiki page wearing the ledger's
# authority.
_SYSTEM_OF = {
    "peoplesoft_gl": "peoplesoft", "peoplesoft_ar": "peoplesoft",
    "peoplesoft_ap": "peoplesoft", "peoplesoft_query": "peoplesoft",
    "peoplesoft_operations": "peoplesoft",
    "coupa": "coupa", "wiki": "wiki",
}

# How each source reads in an answer, for the caveat text.
SOURCE_LABELS = {
    "peoplesoft_gl": "the PeopleSoft general ledger",
    "peoplesoft_ar": "PeopleSoft receivables and billing",
    "peoplesoft_ap": "PeopleSoft payables",
    "peoplesoft_query": "an existing PeopleSoft query (QAS)",
    "peoplesoft_operations": "PeopleSoft operational transaction/process telemetry",
    "coupa": "Coupa procurement",
    "wiki": "a policy wiki page",
}

# Prose that CLAIMS a source. Deliberately narrow: a phrase earns a place
# here only if a finance reader would take it as naming where the number
# came from. "the ledger shows" does; "spend" alone does not.
#
# There is deliberately NO wiki entry. "policy" and "threshold" are the only
# candidate words and they are far too weak — "the balance is within policy
# at 18,432.75" names no source at all, and reading it as a wiki claim
# flagged a correct ledger figure. The wiki boundary is enforced by rule A
# instead, which needs no prose.
_SOURCE_MENTION = (
    ("peoplesoft_gl", re.compile(
        r"(?i)\b(?:general ledger|the ledger|trial balance|GL balance|"
        r"in the GL|PS_LEDGER|the books|posted (?:to|in) the ledger)\b")),
    ("coupa", re.compile(
        r"(?i)\b(?:coupa|procurement|purchase order|requisition|"
        r"commitment[s]?|encumbrance)\b")),
    ("peoplesoft_query", re.compile(
        r"(?i)\b(?:PSQuery|PS Query|Query Access Service|\bQAS\b)\b")),
)

# A source that may state a POLICY but never a ledger amount. This is the
# trust boundary: a Confluence page is editable by anyone with the link, so
# a balance typed into one is an assertion, not a measurement.
UNTRUSTED_FOR_AMOUNTS = {"wiki"}

# Supplying thresholds is the wiki's JOB, so rule A has to tell a policy
# limit from a balance. It reads the words around the figure, and when they
# are ambiguous it stays silent: a caveat on "the threshold is 5,000.00" is
# not a smaller mistake than missing one, it is the mistake that teaches
# people to ignore the caveat that mattered.
_POLICY_CUE = re.compile(
    r"(?i)\b(?:threshold|limit|maximum|minimum|cap|tolerance|materiality|"
    r"policy|policies|allowed|permitted|exceed|exceeds|exceeding|"
    r"requires?|required|requirement|at least|no more than|up to|"
    r"greater than|less than|above|below|within)\b")
_MEASURED_CUE = re.compile(
    r"(?i)\b(?:balance|balances|total|totals|totall?ing|sum|amount|"
    r"outstanding|owed|owe|spend|spent|actual|actuals|posted|booked|"
    r"shows?|showing|reported|reports|stands? at|came to|"
    r"as of)\b")
_CUE_WINDOW = 45

_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _nearest_cue(pattern, before: str, after: str) -> int:
    """Characters from the figure to the closest cue on either side."""
    best = _CUE_WINDOW + 1
    for match in pattern.finditer(before):
        best = min(best, len(before) - match.end())
    for match in pattern.finditer(after):
        best = min(best, match.start())
    return best


def _reads_as_policy_value(sentence: str, start: int, end: int) -> bool:
    """Is this figure presented as a policy LIMIT rather than a measured
    amount?

    The NEAREST cue wins. A fixed window scored by presence alone cannot
    read "the balance is 0.00, which is within the 5,000.00 threshold" —
    one clause reaches into the other's window and both figures come out
    the same. Distance separates them: "balance" is four characters from
    the 0.00, "threshold" is one from the 5,000.00.

    Ties and figures with no cue at all count as policy, so rule A only
    fires on an unambiguous balance claim. A caveat on a correctly quoted
    threshold is not a smaller mistake than missing one — it is the mistake
    that teaches people to ignore the caveat that mattered.
    """
    before = sentence[max(0, start - _CUE_WINDOW):start]
    after = sentence[end:end + _CUE_WINDOW]
    return (_nearest_cue(_MEASURED_CUE, before, after)
            >= _nearest_cue(_POLICY_CUE, before, after))


def source_of_tool(tool_name: str) -> str:
    """Which system a tool's numbers come from. "" when unclassified, and
    unclassified numbers are never a finding."""
    return _TOOL_SOURCE.get((tool_name or "").strip(), "")


def untag_payload(raw) -> tuple:
    """Split a turn payload into (tool_name, payload).

    Every guard that walks payloads goes through here, because they do NOT
    all walk alike — payload_rates looks for percent-named KEYS and would
    silently find none inside an untupled pair, turning a declared rate into
    a caveat. One unwrapper means adding a consumer cannot reintroduce that.
    """
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[0], str):
        return raw[0], raw[1]
    return "", raw


def tagged_payload_numbers(payloads) -> dict:
    """Every number in this turn's results, mapped to the SOURCES that
    produced it.

    Same single traversal as the untagged form — a dict insert instead of a
    set insert — so this costs nothing at turn time.

    Accepts two element shapes on purpose. A bare payload (str or already
    parsed) carries no source and lands under "", which every consumer
    treats as "unknown, never a finding". A ``(tool_name, payload)`` pair
    carries one. Both shapes have to work: the GUI holds prior payloads in
    memory for 1800s, so a worker replaced mid-deploy will read tuples
    written by its predecessor and bare strings written before the upgrade.
    """
    found: dict = {}
    # Keys that exist ONLY because this code summed a column. A real figure
    # landing on one later takes it over outright — see add_sum.
    sum_only: set = set()

    def add(key: str, label: str) -> None:
        if key in sum_only:
            # A figure a tool actually printed outranks arithmetic that
            # happened to reach the same value. Without this the phantom
            # label survived alongside the real one and vouched for a
            # figure its system never produced.
            sum_only.discard(key)
            found[key] = {label}
            return
        found.setdefault(key, set()).add(label)

    def walk(node, label):
        if isinstance(node, dict):
            for value in node.values():
                walk(value, label)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value, label)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            add(_numeric_key(str(node)), label)
        elif isinstance(node, str):
            for match in re.findall(r"-?\d[\d,]*(?:\.\d+)?", node):
                add(_numeric_key(match), label)

    def add_sum(key: str, label: str) -> None:
        """Ground a COMPUTED total without widening anyone's attribution.

        A synthetic sum is weaker evidence than a figure a tool actually
        printed: it is arithmetic this code did, not a value the source
        system reported. Grounding it is right — the model computed it from
        real rows. Letting it add a SOURCE to a number some other system
        already produced is not, because the attribution guard would then
        accept "per the general ledger" for a figure only AP ever returned.

        Measured on the real sample: summing every numeric column made nine
        figures gain a source they had not earned, all of them small
        integers where a day-count total collided with a period number. So
        a sum may create a key, never widen one.
        """
        if key not in found:
            found[key] = {label}
            sum_only.add(key)

    # Columns whose sum is not a figure anybody reports. Adding them
    # grounded nothing a real answer would say and only widened the
    # collision surface — a "days late" column totalling 91 is how AR
    # ended up vouching for an AP number.
    non_additive = re.compile(
        r"pct|percent|rate|ratio|avg|average|median|days|age|score|"
        r"year|period|month|fiscal|_dt$|date", re.IGNORECASE)

    def walk_sums(node, label):
        """Ground whole-column TOTALS over row sets.

        "What is the revenue of this parent across all the children" is
        answered by summing a column the tool really returned — but the
        total itself appears in no payload, so the guard withheld a correct
        answer and told the user the numbers were invented. A caveat that
        fires on a correct answer is worse than a miss. For every list of
        row dicts, the per-column sums are grounded with the same sources
        as the rows they add up; asking the model to do arithmetic on tool
        data and then punishing the result is not a policy, it is a bug.
        """
        if isinstance(node, dict):
            for value in node.values():
                walk_sums(value, label)
            return
        if not isinstance(node, (list, tuple)):
            return
        by_field: dict = {}
        for item in node:
            if not isinstance(item, dict):
                continue
            for field_name, value in item.items():
                if isinstance(value, bool):
                    continue
                if non_additive.search(str(field_name)):
                    continue
                if isinstance(value, (int, float)):
                    by_field.setdefault(field_name, []).append(float(value))
                elif isinstance(value, str):
                    text = value.strip()
                    if re.fullmatch(r"-?\d[\d,]*(?:\.\d+)?", text):
                        by_field.setdefault(field_name, []).append(
                            float(text.replace(",", "")))
        for values in by_field.values():
            if len(values) >= 2:        # a single row's value is already in
                total = sum(values)
                add_sum(_numeric_key(str(total)), label)
                add_sum(_numeric_key(str(round(total, 2))), label)
        for item in node:               # nested row sets ground too
            walk_sums(item, label)

    for raw in payloads or []:
        tool, raw = untag_payload(raw)
        label = source_of_tool(tool)
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                for match in re.findall(r"-?\d[\d,]*(?:\.\d+)?", raw):
                    add(_numeric_key(match), label)
            else:
                walk(parsed, label)
                walk_sums(parsed, label)
        else:
            walk(raw, label)
            walk_sums(raw, label)
    # Ground unit-scaled RESTATEMENTS of payload figures. "$4.55M" for a
    # payload 4,548,123.45 is the same fact at a coarser unit, not an
    # invented number — and withholding a correct aging answer over it
    # taught the user that nothing works. For every payload figure large
    # enough to restate, the thousand/million/billion forms at one and two
    # decimals are grounded too. The restatement inherits the SOURCES of the
    # figure it restates, or "$4.5M" would lose the provenance of the
    # 4,548,123.45 that justifies it.
    for key, labels in list(found.items()):
        try:
            value = float(key)
        except ValueError:
            continue
        # A restatement of a COMPUTED sum is doubly derived: "$91.3K" for a
        # column this code added up. Still worth grounding — a person may
        # legitimately say it — but it must not lend its source to a "91.3"
        # some other system really reported. That is how a day-count total
        # from AR ended up vouching for an AP figure, which the real-payload
        # sweep caught after the first cut of this shipped.
        derived = key in sum_only
        for divisor in (1e3, 1e6, 1e9):
            if abs(value) >= divisor:
                for digits in (0, 1, 2):
                    restated = _numeric_key(str(round(value / divisor,
                                                      digits)))
                    if derived:
                        if restated not in found:
                            found[restated] = set(labels)
                            sum_only.add(restated)
                    else:
                        found.setdefault(restated, set()).update(labels)
    return found


def payload_numbers(payloads) -> set:
    """Every number appearing anywhere in this turn's tool results.

    Kept as the untagged view over the same walk. Six test modules and the
    smoke suite call this with plain lists of JSON strings and assert on the
    set it returns; provenance is additive, not a migration.
    """
    return set(tagged_payload_numbers(payloads))


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


def _labels_for(text: str, tagged: dict) -> set:
    """Sources that produced this figure, tolerating the same rounded
    restatement the withhold guard tolerates. Empty means the figure is not
    in any payload — that is the withhold guard's business, not ours."""
    key = _numeric_key(text)
    if key in tagged:
        return tagged[key]
    unsigned = key.lstrip("-")
    labels: set = set()
    for grounded, sources in tagged.items():
        if grounded.lstrip("-") == unsigned:
            labels |= sources
    if labels:
        return labels
    try:
        stated = float(text.replace(",", ""))
    except ValueError:
        return set()
    decimals = len(text.split(".")[1]) if "." in text else 0
    for grounded, sources in tagged.items():
        if not _is_number(grounded):
            continue
        if round(abs(float(grounded)), decimals) == round(abs(stated),
                                                          decimals):
            labels |= sources
    return labels


def misattributed_figures(answer: str, payloads, intent: str = "") -> list:
    """Figures whose stated source is not the source that produced them.

    Two rules, and the split is deliberate.

    RULE A is mechanical and does not read prose at all: on a question that
    wants data, a figure carried ONLY by a wiki passage is flagged. A
    Confluence page is editable by anyone in the building, so a balance
    typed into one is somebody's assertion — and today it grounds exactly
    like a number the ledger engine computed. This rule fires on the source
    set alone, so it is reproducible regardless of how the model phrases
    the sentence around it.

    RULE B reads prose: in a sentence that names exactly one source, a
    figure from a different source is flagged. This one is a heuristic, so
    it is fenced — one named source per sentence, labels are unioned so a
    value both systems carry never fires, and an unclassified figure is
    skipped. Its output is a caveat; being wrong costs a clause.
    """
    if not answer:
        return []
    tagged = tagged_payload_numbers(payloads)
    if not tagged:
        return []
    exempt = [m.span() for m in _FIGURE_EXEMPT.finditer(answer)]
    findings: list = []
    seen: set = set()
    wants_data = intent in ("data", "mixed")

    offset = 0
    for sentence in _SENTENCE.split(answer):
        start = answer.find(sentence, offset)
        start = offset if start < 0 else start
        offset = start + len(sentence)
        claimed = {label for label, pattern in _SOURCE_MENTION
                   if pattern.search(sentence)}
        for match in _FIGURE.finditer(sentence):
            span = (start + match.start(), start + match.end())
            if any(a <= span[0] and span[1] <= b for a, b in exempt):
                continue
            text = match.group(0)
            if text in seen:
                continue
            labels = {l for l in _labels_for(text, tagged) if l}
            if not labels:
                continue
            # Rule A — a BALANCE only a wiki page carries. A threshold only
            # a wiki page carries is the wiki doing its job.
            if wants_data and labels <= UNTRUSTED_FOR_AMOUNTS:
                if _reads_as_policy_value(sentence, match.start(),
                                          match.end()):
                    continue
                seen.add(text)
                findings.append({"figure": text, "rule": "untrusted_source",
                                 "actual": sorted(labels), "claimed": ""})
                continue
            # Rule B — the sentence named one system, the figure came from
            # another. Across systems only: see _SYSTEM_OF.
            if len(claimed) == 1:
                (claim,) = tuple(claimed)
                produced = {_SYSTEM_OF.get(l, l) for l in labels}
                if _SYSTEM_OF.get(claim, claim) not in produced:
                    seen.add(text)
                    findings.append({"figure": text, "rule": "cross_source",
                                     "actual": sorted(labels),
                                     "claimed": claim})
    return findings


def attribution_caveat(findings) -> str:
    """One bracketed clause naming where the figures actually came from.

    One clause, never several — the same restraint as rate_caveat, for the
    same reason: a stack of warnings teaches the reader to skip all of them.
    """
    if not findings:
        return ""

    def phrase(labels) -> str:
        named = [SOURCE_LABELS.get(l, l) for l in labels]
        if len(named) == 1:
            return named[0]
        return ", ".join(named[:-1]) + " and " + named[-1]

    parts: list = []
    untrusted = [f for f in findings if f["rule"] == "untrusted_source"]
    if untrusted:
        listed = ", ".join(f["figure"] for f in untrusted[:3])
        more = " and others" if len(untrusted) > 3 else ""
        parts.append(
            f"{listed}{more} " + ("is" if len(untrusted) == 1 else "are")
            + " text from a policy wiki page, which colleagues can edit — "
              "not a figure any PeopleSoft query returned. Wiki pages carry "
              "policy and thresholds; balances come from the ledger.")
    # Group by (what was claimed, what produced it) so five figures from one
    # mix-up read as one sentence. Repeating the same clause per figure was
    # a wall of text that said one thing.
    grouped: dict = {}
    for finding in findings:
        if finding["rule"] != "cross_source":
            continue
        key = (finding["claimed"], tuple(finding["actual"]))
        grouped.setdefault(key, []).append(finding["figure"])
    for (claimed, actual), figures in grouped.items():
        listed = ", ".join(figures[:3])
        more = f" and {len(figures) - 3} more" if len(figures) > 3 else ""
        parts.append(
            f"{listed}{more} came from {phrase(actual)}, not from "
            f"{SOURCE_LABELS.get(claimed, claimed)}.")
    return "[Attribution: " + " ".join(parts) + "]"


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
        _, raw = untag_payload(raw)
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
