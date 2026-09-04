"""Configuration loading: config.yaml + .env overlays.

Stdlib-safe: PyYAML and python-dotenv are used when installed, but the module
imports (and Config.sample()) work without them so the smoke test can run on a
bare interpreter.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass
class Defaults:
    business_unit: str = "US001"
    ledger: str = "ACTUALS"
    setid: str = "SHARE"
    calendar_id: str = "01"
    base_currency: str = "USD"
    rate_type: str = "CRRNT"
    adjustment_periods: list = field(default_factory=lambda: [998])
    suspense_accounts: list = field(default_factory=lambda: ["1999"])
    retained_earnings_account: str = "3500"
    account_tree: str = "ACCOUNT"
    ar_control_accounts: list = field(default_factory=lambda: ["1100"])
    # Site-governed AP liability accounts. Empty is deliberate: unlike the
    # bundled AR example, no AP account number is portable enough to assume.
    # reconcile_ap_to_gl fails closed until Finance configures this list or
    # the caller explicitly supplies the approved accounts.
    ap_control_accounts: list = field(default_factory=list)
    aging_buckets: list = field(default_factory=lambda: [30, 60, 90])


@dataclass
class DbCfg:
    backend: str = "sqlite"  # sqlite | oracle | sqlserver
    sqlite_path: str = "sample_data/ps_sample.db"
    # ``schema`` is the default namespace used for unqualified object names.
    # ``schemas`` is the complete read boundary for a source that deliberately
    # combines more than one namespace in one semantic catalog/graph.  The
    # loader also accepts ``schema: [DEFAULT, EXTRA]`` as a shorthand, but
    # normalizes it immediately so downstream code always sees a scalar here.
    schema: str = ""
    schemas: list = field(default_factory=list)
    use_views: bool = False
    oracle_dsn: str = ""
    oracle_user: str = ""
    oracle_password: str = ""
    # Thin-mode extras: a tnsnames/sqlnet directory and (optionally) a wallet,
    # for sites that hand out a TNS alias or require mTLS.
    oracle_config_dir: str = ""
    oracle_wallet_dir: str = ""
    oracle_wallet_password: str = ""
    mssql_conn_str: str = ""
    # BigQuery silo (sources: only). The project is where jobs run and
    # BILL; the dataset rides ``schema`` so every schema-boundary guard
    # stays armed. The byte cap is per QUERY, enforced server-side on
    # the client's default job config -- it is not a daily budget.
    bigquery_project: str = ""
    bigquery_location: str = ""
    bigquery_max_bytes_billed: int = 1_073_741_824   # 1 GiB ~= $0.006
    bigquery_warn_bytes: int = 268_435_456           # dry-run disclosure
    query_timeout_seconds: int = 120
    # Concurrent chat channels each need their own session; this caps them.
    pool_max: int = 8


@dataclass
class LlmCfg:
    provider: str = "ollama"  # ollama | gemini | claude
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    # Ollama defaults to a 2048-token context. This system prompt alone is
    # ~5,500 tokens, so the default SILENTLY TRUNCATED it — the record map,
    # the worked examples and half the doctrine never reached the model,
    # and every local-model routing failure was measured against a prompt
    # it could not see. Sized for prompt + tool results + history.
    # 16384 is MEASURED, not guessed: at Ollama's 2048 default the prompt
    # is silently cut; 8192 still truncates and fails the eval suite;
    # 32768 passes but runs ~28% slower for no benefit (23.3s vs 18.0s on
    # the same case). Re-measure before changing.
    ollama_num_ctx: int = 16384
    gemini_model: str = "gemini-2.5-pro"
    gemini_project: str = ""
    gemini_location: str = "us-central1"
    # -1 = model default (dynamic thinking); 0 disables thinking (flash only —
    # 2.5-pro enforces a minimum); >0 caps the thinking token budget.
    gemini_thinking_budget: int = -1
    temperature: float = 0.2
    # Routing discipline (Gemini): on the user turn of a question that needs
    # tools, force a function call (tool_config mode ANY) so the model cannot
    # skip straight to prose, and decode greedily — tool selection is a
    # decision, not a writing task. Chained/prose turns keep `temperature`.
    gemini_force_tool_round: bool = True
    gemini_routing_temperature: float = 0.0
    # Claude on the Anthropic API. The credential is never a config value:
    # the SDK reads ANTHROPIC_API_KEY from .env, or a signed-in CLI
    # profile, on its own.
    claude_model: str = "claude-opus-5"
    # Thinking and answer text share this budget, and thinking is on by
    # default on Opus 5 — a cap sized for the answer alone truncates.
    claude_max_tokens: int = 32000
    # low | medium | high | xhigh | max. `high` is the API default and a
    # deliberate starting point, not a measured one: run
    # scripts/eval.py --provider claude at two levels before changing it.
    claude_effort: str = "high"
    # The same routing discipline as gemini_force_tool_round, by the only
    # mechanism this API has: Opus 5 rejects temperature outright, so a
    # forced tool_choice replaces greedy decoding rather than joining it.
    claude_force_tool_round: bool = True
    # Re-run a request Anthropic's safety classifiers decline on the
    # recommended substitute model, instead of returning nothing.
    claude_fallbacks: bool = True
    # 0 = auto: 24k chars for local models, 120k for the 1M-token models.
    max_tool_result_chars: int = 0


@dataclass
class WikiCfg:
    provider: str = "auto"  # confluence | localdocs | auto
    localdocs_path: str = "sample_wiki"
    confluence_base_url: str = ""
    confluence_email: str = ""
    confluence_api_token: str = ""
    confluence_space: str = ""
    confluence_labels: str = ""


@dataclass
class ToolsCfg:
    allow_raw_sql: bool = True
    # Ad-hoc SQL on a bind the whole network can reach, with no row
    # security, is off unless an operator says otherwise here. The default
    # stays True because the machine-local and the secured deployments both
    # want it; what needs a deliberate hand is the third case.
    raw_sql_on_shared_bind: bool = False
    max_rows: int = 200
    # Period AP/GL activity is read into bounded in-memory key maps. This cap
    # applies independently to each side; runtime also enforces a 100k hard
    # ceiling even if configuration is higher.
    ap_reconciliation_line_cap: int = 50_000
    reports_path: str = "reports"
    question_log: str = "logs/questions.jsonl"
    # Facts about THIS installation, approved by an operator. A plain
    # reviewable file so a human can see exactly what the agent believes.
    site_memory: str = "site_memory.json"
    txn_row_threshold: int = 1000
    # Rows returned by profile_record/compare_records. These reach the
    # configured model, and on the Gemini path that means they leave the
    # network — sensitive columns are masked first (see pstb/profiles.py).
    # Set to 0 to send no rows at all and keep only shape and value counts.
    sample_rows: int = 3


@dataclass
class BatchExportCfg:
    """Large-result delivery without putting the population in chat.

    The browser only receives ``inline_rows`` records.  A user can then start
    a bounded background CSV export; the database cursor is fetched in small
    batches and written directly to disk, so ``max_rows`` is a governance
    ceiling rather than a memory allocation.
    """
    enabled: bool = True
    inline_rows: int = 100
    max_rows: int = 1_000_000
    max_file_mb: int = 1_024
    fetch_size: int = 2_000
    workers: int = 2
    max_queued: int = 8
    ttl_minutes: int = 60
    directory: str = "logs/batch_exports"


@dataclass
class ProcessGraphCfg:
    """Offline PeopleTools process-graph build ceilings.

    The graph reader remains intentionally small per question; these limits
    govern only the rare metadata harvest and atomic SQLite rebuild.
    """
    max_records: int = 100_000
    max_pages: int = 100_000
    max_page_fields: int = 100_000
    max_components: int = 100_000
    max_navigation: int = 100_000
    max_queries: int = 100_000
    query_page_size: int = 5_000
    max_nodes: int = 100_000
    max_edges: int = 100_000
    memory_budget_mb: int = 512
    write_batch_size: int = 2_000


@dataclass
class MetadataCatalogCfg:
    """Offline, read-only catalog of structure across configured databases.

    The artifact contains names, definitions and relationships only.  These
    ceilings bound one refresh without turning the catalog into a copy of any
    source database.
    """
    max_objects: int = 100_000
    max_fields: int = 500_000
    max_indexes: int = 250_000
    max_constraints: int = 250_000
    max_constraint_columns: int = 1_000_000
    max_dependencies: int = 250_000
    max_peopletools_rows: int = 500_000
    query_page_size: int = 5_000
    stale_after_hours: int = 168
    # Value-overlap join mining: bounded probes that measure undeclared
    # relationships from data containment. Off only if a site objects to
    # any sampled reads during a catalog build.
    mine_value_joins: bool = True
    mine_max_tables: int = 40
    mine_max_pairs: int = 120
    mine_sample_rows: int = 100
    mine_max_probes: int = 240
    # Failed-question demand steering of the miner working set; 0 = off.
    mine_demand_terms: int = 12
    # View-definition harvesting (#178). These were documented as config
    # keys from the start but existed only as CLI flags -- a config.yaml
    # entry was silently ignored, which the docs flatly contradicted.
    harvest_view_vocabulary: bool = True
    max_view_definitions: int = 5_000


@dataclass
class SemanticRetrievalCfg:
    """Optional semantic re-ranking for already-safe metadata candidates.

    This is deliberately a re-ranker, not a vector database or an alternate
    search path.  The deterministic catalog remains responsible for choosing
    candidates and explaining relationship confidence; an embedding model may
    only change their display order.  Disabled by default because enabling the
    Vertex provider sends the query and structural metadata names to Google.
    """
    enabled: bool = False
    provider: str = "vertex"       # currently: vertex
    model: str = "gemini-embedding-001"
    location: str = "global"
    output_dimensionality: int = 768
    candidate_limit: int = 20
    semantic_weight: float = 0.35
    # Per embedding request. Vertex receives one bounded candidate at a time,
    # so an explicit timeout is required for deterministic fallback.
    timeout_seconds: int = 15


@dataclass
class AnomalyCfg:
    """Bounded, read-only transaction/process anomaly settings.

    Rules stay as dictionaries because record names and shapes are inherently
    deployment-specific.  The detector validates every configured identifier
    against the live catalog before placing it in SQL.
    """
    infer_tables: bool = True
    infer_processes: bool = True
    table_rules: list = field(default_factory=list)
    relationship_rules: list = field(default_factory=list)
    process_rules: list = field(default_factory=list)
    candidate_limit: int = 20
    process_candidate_limit: int = 8
    max_inferred_relations: int = 20
    catalog_object_cap: int = 5000
    catalog_column_cap: int = 50000
    metadata_row_cap: int = 20000
    metadata_cache_seconds: int = 900
    process_result_cap: int = 5000
    max_unindexed_rows: int = 50000
    min_history_days: int = 28
    min_active_days: int = 12
    # A zero on the as-of date is not evidence of a miss until this many
    # whole days have elapsed.  Sites with intraday-complete feeds can set 0;
    # overnight/batch feeds normally keep the one-day default.  Individual
    # table and relationship sides may override this in their rules.
    freshness_lag_days: int = 1
    min_relation_days: int = 8
    min_process_history_days: int = 8
    min_process_runs: int = 3
    material_count: int = 10
    material_pct: float = 0.5
    process_material_pct: float = 0.5
    min_duration_increase_seconds: float = 30.0
    success_rate_drop: float = 0.2
    z_threshold: float = 3.5
    min_relation_confidence: float = 0.55


@dataclass
class CoupaCfg:
    """Non-secret Coupa procurement semantics for this deployment.

    Credentials remain in ``.env``.  These values describe where purchasing
    truth lives and how a Coupa receipt is scoped to a PeopleSoft business
    unit.  There is no portable Coupa account segment for business unit, so a
    blank path deliberately makes the scoped RNI control incomplete rather
    than scanning every company.
    """
    po_receipt_authority: bool = False
    # IANA timezone of the Coupa company/calendar used for API date cutoffs.
    # Required when Coupa is the PO/receipt authority so "today" does not
    # silently follow the application host around midnight.
    business_timezone: str = ""
    # Dotted JSON path on a receiving transaction, for example
    # ``account.segment-1`` or a tenant custom field.  A split allocation is
    # not silently collapsed to this field; the control reports it incomplete
    # unless its unit can be established unambiguously.
    business_unit_path: str = ""
    # Exact tenant-tested Coupa query keys required for live evaluation.
    # Query spelling varies by resource/release and is therefore never
    # derived from the JSON response path. These are key names, never values.
    receipt_business_unit_filter: str = ""
    invoice_business_unit_filter: str = ""
    # Required assertion for evaluated live RNI. True means the configured
    # invoice filter is tenant-tested to return every invoice header whose
    # line references an in-scope PO/order-line, even after distribution-
    # account reassignment. A generic invoice-account BU filter is not enough.
    invoice_scope_order_line_invariant: bool = False
    # Optional PeopleSoft BU -> Coupa value translation when the two systems
    # use different codes, e.g. {US001: US_CORP}.
    business_unit_map: dict = field(default_factory=dict)
    # Only these current Coupa invoice-header states reduce a receipt
    # candidate.  Pending/draft invoices remain visible as exceptions because
    # they have not reached the approved outbound-to-ERP population.
    invoice_eligible_statuses: list = field(
        default_factory=lambda: ["approved"])
    # Coupa defines receipt status as tenant-extensible text. Only reviewed
    # values enter the event population; any other observed value fails the
    # candidate control closed.
    receipt_eligible_statuses: list = field(
        default_factory=lambda: ["created"])
    rni_max_rows: int = 50_000


@dataclass
class PsApiCfg:
    """Query Access Service credentials and limits.

    Execution runs as this PeopleSoft USER, so results respect that
    user's permission lists — a real difference from the direct database
    account, and a governance improvement worth stating in answers. The
    gateway URL is deliberately absent: it is discovered from the site's
    own IB catalog (PSIBSVCSETUP) unless overridden here.
    """
    enabled: bool = False
    user: str = ""          # PSFT_QAS_USER in .env, never in config.yaml
    password: str = ""      # PSFT_QAS_PASSWORD in .env
    target_location: str = ""   # blank = discover from PSIBSVCSETUP
    timeout_seconds: int = 60
    max_rows: int = 5000


@dataclass
class TickerCfg:
    """The continuous exception ticker. OFF by default, like everything
    here that costs the database or exposes state: a standing loop
    against a read-only reporting account is enabled by a person who has
    read what it runs, never by a default."""
    enabled: bool = False
    cadence_minutes: int = 30
    ledger: str = ""
    # Adds the AP invoice-pipeline check (stuck vouchers) to each tick.
    watch_invoicing: bool = False
    business_units: list = field(default_factory=list)
    max_queries_per_tick: int = 40
    max_seconds_per_tick: int = 600
    history_per_check: int = 200
    events_kept: int = 500
    failure_trip: int = 3
    # How long an operator acknowledgment can quiet an UNCHANGED
    # exception before it re-demands attention (bounds live with the
    # other budgets in TickerLimits).
    ack_ttl_hours: int = 24


@dataclass
class SecurityCfg:
    """Business-unit row security, read from PeopleSoft's own configuration.

    NOT authentication while the sign-in takes a user ID and no password:
    anyone can type any ID. It mirrors PeopleSoft's rules so an honest user
    sees their own units and the model cannot wander across them; it stops
    nobody who is trying. See pstb/security.py.
    """
    enabled: bool = False
    # OPRIDs that see every unit whatever the security records say. Named
    # here rather than derived, so the account used to FIX a broken grant
    # does not depend on that grant being readable.
    privileged_users: list = field(default_factory=list)
    # Blank = probe the stock FSCM records (PS_SEC_BU_OPR, then
    # PS_SEC_BU_CLS). Set both when this site keeps unit security in a
    # custom record; unit_key is OPRID for user-level, or the permission
    # list field (e.g. OPRCLASS) for class-level.
    unit_record: str = ""
    unit_key: str = ""
    # What to do when security is enabled and unreadable. "refuse" shows
    # nothing and says which grant is missing; "allow" degrades to full
    # access, which is a decision a site should have to type.
    on_unavailable: str = "refuse"
    # Ad-hoc SQL bypasses every curated tool's scope handling, so for a
    # restricted user it is off by default. A privileged user is unaffected.
    raw_sql_for_restricted: bool = False
    # Emergency review path for deployments that intentionally use the
    # passwordless OPRID selector over a trusted network.  This is NOT
    # authentication: anyone who can reach the page can type a configured
    # privileged ID.  In explicit testing mode there is deliberately no Host
    # allowlist or timeout; it remains active until set false and restarted.
    allow_unauthenticated_remote_approvals: bool = False


@dataclass
class Config:
    root: Path = field(default_factory=Path.cwd)
    defaults: Defaults = field(default_factory=Defaults)
    db: DbCfg = field(default_factory=DbCfg)
    # Additional named databases for ask-anything beyond PeopleSoft.
    # The db: block above is always the default source (the PeopleSoft one
    # the curated tools use); entries here are reachable from run_sql /
    # list_tables / describe_table / search_records via source=<name>.
    sources: dict = field(default_factory=dict)
    # Concept overrides: which codes define a named population at THIS site,
    # e.g. semantics: {billing_invoiced: {values: [INV, PRO]}}. Approved
    # site-memory facts outrank this; the built-in seed backstops both.
    semantics: dict = field(default_factory=dict)
    ps_api: PsApiCfg = field(default_factory=PsApiCfg)
    llm: LlmCfg = field(default_factory=LlmCfg)
    wiki: WikiCfg = field(default_factory=WikiCfg)
    tools: ToolsCfg = field(default_factory=ToolsCfg)
    batch_exports: BatchExportCfg = field(default_factory=BatchExportCfg)
    process_graph: ProcessGraphCfg = field(default_factory=ProcessGraphCfg)
    metadata_catalog: MetadataCatalogCfg = field(
        default_factory=MetadataCatalogCfg)
    semantic_retrieval: SemanticRetrievalCfg = field(
        default_factory=SemanticRetrievalCfg)
    anomalies: AnomalyCfg = field(default_factory=AnomalyCfg)
    coupa: CoupaCfg = field(default_factory=CoupaCfg)
    security: SecurityCfg = field(default_factory=SecurityCfg)
    ticker: TickerCfg = field(default_factory=TickerCfg)

    @classmethod
    def sample(cls, root: Path | str) -> "Config":
        """Config pointing at the bundled SQLite sample, no yaml/env needed."""
        return cls(root=Path(root))

    def resolve_path(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else self.root / path


def _apply_section(obj: Any, data: Optional[dict]) -> None:
    if not isinstance(data, dict):
        return
    names = {f.name for f in fields(obj)}
    for k, v in data.items():
        if k in names and v is not None:
            setattr(obj, k, v)


def _validate_ticker(cfg: TickerCfg) -> None:
    """The off-switch must be a literal boolean, never a truthy value.

    The ticker is a standing loop against a production database; the
    quoted string "false" being truthy in Python must not be what turns
    it on. Numeric budgets are validated with floors AND ceilings where
    they are spent (ticker.TickerLimits) so a config typo cannot buy an
    unbounded loop either.
    """
    for name in ("enabled", "watch_invoicing"):
        value = getattr(cfg, name, False)
        if type(value) is not bool:  # bool only; integers are not accepted
            raise RuntimeError(
                f"ticker.{name} must be the YAML boolean true or false "
                "(without quotes)")
    units = getattr(cfg, "business_units", [])
    if not isinstance(units, (list, tuple)):
        raise RuntimeError("ticker.business_units must be a list")
    tables = getattr(cfg, "watch_tables", [])
    if isinstance(tables, str):
        raise RuntimeError(
            "ticker.watch_tables must be a YAML list -- a bare string "
            "iterates one character at a time")
    if not isinstance(tables, (list, tuple)):
        raise RuntimeError("ticker.watch_tables must be a list")
    # $/# are legal Oracle identifier characters, but the ticker's
    # check-id grammar refuses them -- refused HERE with the reason,
    # not mid-tick where the failure would become three _fail calls
    # and a tripped breaker.
    ident = re.compile(
        r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
    seen_fold: dict = {}
    for entry in tables:
        if type(entry) is not str or not entry.strip():
            raise RuntimeError(
                "ticker.watch_tables entries must be non-empty table "
                "names")
        name = entry.strip()
        if len(name) > 60 or not ident.fullmatch(name):
            raise RuntimeError(
                f"ticker.watch_tables entry {entry!r} is not a plain "
                "TABLE or SCHEMA.TABLE identifier of letters, digits "
                "and underscores (up to 60 characters); $ and # are "
                "refused because the ticker's check-id grammar cannot "
                "carry them")
        fold = name.casefold()
        if fold in seen_fold:
            raise RuntimeError(
                f"ticker.watch_tables lists {seen_fold[fold]!r} and "
                f"{entry!r}, which differ only by case: two spellings "
                "of one table would double-spend the query budget and "
                "split its history across two baselines")
        seen_fold[fold] = entry
    if len(tables) > 20:
        raise RuntimeError(
            "ticker.watch_tables is capped at 20 tables: 20 reserve 40 "
            "queries -- the entire default max_queries_per_tick -- and "
            "tb_integrity is charged first, so trailing tables would "
            "never run; raise the budget or shorten the list")


def _validate_security(cfg: SecurityCfg) -> None:
    """Fail closed for the deliberately unsafe remote-approval escape hatch.

    ``_apply_section`` is intentionally permissive for legacy settings, but
    a quoted ``"false"`` is truthy in Python.  That cannot be allowed to turn
    on a passwordless governance write path by accident.
    """
    enabled = getattr(cfg, "allow_unauthenticated_remote_approvals", False)
    if type(enabled) is not bool:  # bool only; integers are not accepted
        raise RuntimeError(
            "security.allow_unauthenticated_remote_approvals must be the "
            "YAML boolean true or false (without quotes)")
    if not enabled:
        return
    if getattr(cfg, "enabled", False) is not True:
        raise RuntimeError(
            "security.allow_unauthenticated_remote_approvals requires "
            "security.enabled: true so the browser must select a configured "
            "privileged user")
    users = getattr(cfg, "privileged_users", None)
    if not isinstance(users, (list, tuple)) or not users:
        raise RuntimeError(
            "security.allow_unauthenticated_remote_approvals requires a "
            "non-empty security.privileged_users list")
    for value in users:
        if type(value) is not str:
            raise RuntimeError(
                "security.privileged_users entries must be text PeopleSoft "
                "user IDs when unauthenticated remote approvals are on")
        candidate = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,30}", candidate):
            raise RuntimeError(
                "security.privileged_users entries must be PeopleSoft user "
                "IDs using letters, numbers, '_', '.', or '-' (up to 30 "
                "characters) when unauthenticated remote approvals are on")


_SCHEMA_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")


def normalize_db_schemas(cfg: DbCfg, *, section: str = "db") -> list[str]:
    """Canonicalize one database's default schema and schema allowlist.

    ``schema`` remains backward-compatible and scalar after this function.
    The explicit ``schemas`` list augments it; a list supplied in ``schema``
    is accepted as a user-friendly shorthand whose first entry is the
    default.  Values are ordinary unquoted database identifiers because the
    same allowlist is later used as a hard query/catalog boundary.  Blank or
    quoted/dotted values therefore fail closed instead of broadening access.

    The returned list is ordered with the default first and otherwise keeps
    configuration order.  Case-insensitive duplicates are removed.
    """
    raw_default = getattr(cfg, "schema", "")
    raw_allowed = getattr(cfg, "schemas", [])
    if isinstance(raw_default, (list, tuple)):
        default_values = list(raw_default)
        if not default_values:
            raise RuntimeError(
                f"{section}.schema list must contain at least one schema name")
        if raw_allowed not in (None, [], ()):
            if not isinstance(raw_allowed, (list, tuple)):
                raise RuntimeError(f"{section}.schemas must be a list of schema names")
            default_values.extend(raw_allowed)
        raw_values = default_values
        raw_default = default_values[0] if default_values else ""
    else:
        if raw_default is None:
            raw_default = ""
        if not isinstance(raw_default, str):
            raise RuntimeError(
                f"{section}.schema must be a schema name or a list of names")
        if raw_allowed is None:
            raw_allowed = []
        if not isinstance(raw_allowed, (list, tuple)):
            raise RuntimeError(f"{section}.schemas must be a list of schema names")
        raw_values = ([raw_default] if raw_default.strip() else []) + list(raw_allowed)

    # BigQuery datasets are case-sensitive: validation elsewhere folds
    # to uppercase (comparisons stay internally consistent), but the
    # stored value must remain verbatim or executed SQL would name a
    # dataset that does not exist. Idempotent across the loader's
    # repeated normalization passes.
    keep_case = str(getattr(cfg, "backend", "")).lower() == "bigquery"

    def _fold(value: str) -> str:
        return value.strip() if keep_case else value.strip().upper()

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        if not isinstance(raw, str):
            raise RuntimeError(f"{section}.schemas must contain only schema names")
        value = _fold(raw)
        if not value:
            raise RuntimeError(f"{section}.schemas cannot contain blank schema names")
        if not _SCHEMA_IDENT.fullmatch(value):
            raise RuntimeError(
                f"{section}.schemas contains unsafe schema name {raw!r}; "
                "use an unquoted database identifier without dots")
        if value not in seen:
            normalized.append(value)
            seen.add(value)

    default = _fold(str(raw_default))
    if default:
        if not _SCHEMA_IDENT.fullmatch(default):
            raise RuntimeError(
                f"{section}.schema contains unsafe schema name {raw_default!r}; "
                "use an unquoted database identifier without dots")
        if default in normalized:
            normalized.remove(default)
        normalized.insert(0, default)
    elif normalized:
        default = normalized[0]

    cfg.schema = default
    cfg.schemas = normalized
    return list(normalized)


def _validate_bigquery_source(block: Any, *, section: str) -> None:
    """Refuse a BigQuery block this release cannot honour.

    Silo-only: the curated PeopleSoft toolchain, the ticker and the
    profiles were never designed for this backend, so the PRIMARY db
    refuses it outright. The budgets get validate()-style floors and
    ceilings -- the 10MB floor is BigQuery's own billing minimum, below
    which a cap can never be satisfied.
    """
    if not isinstance(block, dict):
        return
    if str(block.get("backend") or "").strip().lower() != "bigquery":
        return
    if section == "db":
        raise RuntimeError(
            "BigQuery is supported as a sources: silo only in this "
            "release; the primary db must stay sqlite/oracle/sqlserver")
    if not str(block.get("bigquery_project") or "").strip():
        raise RuntimeError(
            f"{section}: a BigQuery source needs bigquery_project "
            "(where jobs run and bill)")
    raw_schema = block.get("schema", "")
    if isinstance(raw_schema, (list, tuple)) and len(raw_schema) != 1:
        raise RuntimeError(
            f"{section}: a BigQuery source is exactly one dataset")
    if not str(raw_schema[0] if isinstance(raw_schema, (list, tuple))
               else raw_schema or "").strip():
        raise RuntimeError(
            f"{section}: a BigQuery source needs schema: <dataset>")
    for name, floor, ceiling in (
        ("bigquery_max_bytes_billed", 10 * 1024 * 1024, 2**40),
        ("bigquery_warn_bytes", 10 * 1024 * 1024, 2**40),
    ):
        if name not in block:
            continue
        value = block[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"{section}.{name} must be an integer")
        if not floor <= value <= ceiling:
            raise RuntimeError(
                f"{section}.{name} must be between {floor} and {ceiling}")
    warn = block.get("bigquery_warn_bytes")
    cap = block.get("bigquery_max_bytes_billed", 1_073_741_824)
    if (isinstance(warn, int) and isinstance(cap, int)
            and not isinstance(warn, bool) and warn > cap):
        raise RuntimeError(
            f"{section}.bigquery_warn_bytes cannot exceed "
            "bigquery_max_bytes_billed")


def _validate_multi_schema_backend(block: Any, *, section: str) -> None:
    """Reject an explicitly multi-schema non-Oracle YAML source.

    Validation uses the unmodified input block. Runtime/fingerprint helpers
    normalize programmatically constructed configs more than once, so they
    cannot distinguish a user allowlist from the single default they derived
    on an earlier pass.
    """
    if not isinstance(block, dict):
        return
    raw_schema = block.get("schema", "")
    raw_schemas = block.get("schemas", [])
    values = list(raw_schema) if isinstance(raw_schema, (list, tuple)) else (
        [raw_schema] if isinstance(raw_schema, str) and raw_schema.strip()
        else [])
    if isinstance(raw_schemas, (list, tuple)):
        values.extend(raw_schemas)
    distinct = {
        value.strip().casefold() for value in values
        if isinstance(value, str) and value.strip()
    }
    backend = str(block.get("backend", "sqlite") or "").strip().casefold()
    if len(distinct) > 1 and backend != "oracle":
        raise RuntimeError(
            f"{section}.schemas supports multiple schema names only for "
            "Oracle in this release; configure other databases as separate "
            "sources instead"
        )


def _validate_coupa(cfg: CoupaCfg) -> None:
    """Fail closed on malformed procurement-authority configuration.

    Python truthiness would turn YAML ``"false"`` into true, which is not an
    acceptable failure mode for choosing the system of record or an egress
    population. Mapping/status shapes also reach row-security arithmetic and
    therefore must not be silently coerced.
    """
    if type(cfg.po_receipt_authority) is not bool:
        raise RuntimeError("coupa.po_receipt_authority must be true or false")
    if not isinstance(cfg.business_timezone, str):
        raise RuntimeError("coupa.business_timezone must be an IANA timezone")
    cfg.business_timezone = cfg.business_timezone.strip()
    if cfg.po_receipt_authority and not cfg.business_timezone:
        raise RuntimeError(
            "coupa.business_timezone is required when Coupa is the "
            "PO/receipt authority")
    if cfg.business_timezone:
        try:
            ZoneInfo(cfg.business_timezone)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError(
                "coupa.business_timezone must be a valid IANA timezone"
            ) from exc
    if type(cfg.invoice_scope_order_line_invariant) is not bool:
        raise RuntimeError(
            "coupa.invoice_scope_order_line_invariant must be true or false")
    if not isinstance(cfg.business_unit_path, str):
        raise RuntimeError("coupa.business_unit_path must be a string")
    for field_name in (
            "receipt_business_unit_filter",
            "invoice_business_unit_filter"):
        value = getattr(cfg, field_name)
        if not isinstance(value, str):
            raise RuntimeError(f"coupa.{field_name} must be a string")
        value = value.strip()
        if value and not re.fullmatch(r"[A-Za-z0-9_\-\[\]]+", value):
            raise RuntimeError(
                f"coupa.{field_name} must be a safe Coupa scalar query key")
        setattr(cfg, field_name, value)
    if not isinstance(cfg.business_unit_map, dict):
        raise RuntimeError("coupa.business_unit_map must be a mapping")
    normalized_map = {}
    for raw_key, raw_value in cfg.business_unit_map.items():
        if (not isinstance(raw_key, (str, int)) or isinstance(raw_key, bool)
                or not isinstance(raw_value, (str, int))
                or isinstance(raw_value, bool)):
            raise RuntimeError(
                "coupa.business_unit_map keys and values must be text")
        key, value = str(raw_key).strip(), str(raw_value).strip()
        if not key or not value:
            raise RuntimeError(
                "coupa.business_unit_map keys and values cannot be blank")
        normalized_map[key] = value
    cfg.business_unit_map = normalized_map
    for field_name in ("invoice_eligible_statuses",
                       "receipt_eligible_statuses"):
        configured = getattr(cfg, field_name)
        if (not isinstance(configured, list) or not configured
                or any(not isinstance(value, str) or not value.strip()
                       for value in configured)):
            raise RuntimeError(
                f"coupa.{field_name} must be a non-empty list of text")
        normalized = [value.strip().lower() for value in configured]
        if len(normalized) != len(set(normalized)):
            raise RuntimeError(
                f"coupa.{field_name} must not contain duplicates")
        if field_name == "invoice_eligible_statuses":
            delivered_ineligible = {
                "new", "ap_hold", "draft", "on_hold", "pending_receipt",
                "rejected", "abandoned", "disputed", "pending_approval",
                "booking_hold", "save_as_draft", "pending_action", "voided",
                "processing", "invalid", "payable_adjustment",
            }
            unsafe = sorted(set(normalized) & delivered_ineligible)
            if unsafe:
                raise RuntimeError(
                    "coupa.invoice_eligible_statuses contains delivered "
                    "non-approved status value(s): " + ", ".join(unsafe))
            if "paid" in normalized and "approved" not in normalized:
                raise RuntimeError(
                    "coupa.invoice_eligible_statuses cannot use paid without "
                    "approved; Coupa paid is a boolean, not a header status")
        setattr(cfg, field_name, normalized)
    if (not isinstance(cfg.rni_max_rows, int)
            or isinstance(cfg.rni_max_rows, bool)
            or not 1 <= cfg.rni_max_rows <= 100_000):
        raise RuntimeError("coupa.rni_max_rows must be between 1 and 100000")


def _env(name: str, current: str) -> str:
    v = os.environ.get(name, "").strip()
    return v or current


# The directory that contains the pstb package — i.e. the repo/deployment
# root, wherever it was unpacked.
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


CONFIG_NAME = "config.yaml"
EXAMPLE_NAME = "config.example.yaml"


def base_config_path(root: Path | str) -> Path:
    """The base config to read from a deployment root: that deployment's own
    config.yaml, or the shipped config.example.yaml when it has none yet.

    config.yaml is per-deployment and deliberately NOT tracked by git, so
    `git pull` on a deployed box can never overwrite the settings someone
    tuned there. The cost of that is a fresh clone — and CI — starts with no
    config.yaml at all, and this fallback is what keeps them working with no
    copy step, on exactly the values the example ships.

    Note which way the fallback runs: an existing config.yaml always wins,
    so a deployment never silently reverts to sample values because an
    upgrade shipped a newer example.
    """
    cfg_path = Path(root) / CONFIG_NAME
    if cfg_path.exists():
        return cfg_path
    example = cfg_path.parent / EXAMPLE_NAME
    return example if example.exists() else cfg_path


def _or_example(cfg_path: Path) -> Path:
    """config.example.yaml beside cfg_path, when cfg_path itself is absent.

    Scoped to the SAME directory: a mistyped --config still reports the path
    the caller asked for rather than quietly loading some other tree's
    example.
    """
    return base_config_path(cfg_path.parent) if cfg_path.name == CONFIG_NAME \
        else cfg_path


def resolve_config_path(path: Optional[str] = None) -> Path:
    """Where the config lives: path arg > $PSTB_CONFIG > ./config.yaml >
    <package root>/config.yaml — and beside each of those, config.example.yaml
    when the deployment has not created its own config.yaml yet. Pure path
    logic (no yaml import), so tests can verify it under a bare interpreter."""
    explicit = path or os.environ.get("PSTB_CONFIG")
    if explicit:
        cfg_path = Path(explicit)
        if not cfg_path.is_absolute():
            cfg_path = Path.cwd() / cfg_path
        return _or_example(cfg_path)
    cfg_path = base_config_path(Path.cwd())
    if not cfg_path.exists():
        packaged = base_config_path(_PACKAGE_ROOT)
        if packaged.exists():
            return packaged
    return cfg_path


def _tighten(path) -> None:
    """Repair a secret file that is already group- or world-readable.

    setup.py creates .env at 0600, but a file written before that rule — or
    copied, or restored from a backup — keeps its old mode forever, because
    creating at 0600 does nothing to a file that already exists. Checking on
    every load is cheap and is the only moment we reliably touch it.

    Repair is announced rather than silent: a mode that changed under an
    operator without a word is its own small surprise.
    """
    import os
    import stat as _stat
    import sys as _sys
    try:
        mode = _stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return
    if mode & 0o077:
        try:
            os.chmod(path, 0o600)
            print(f"[pstb] tightened {path} from {mode:04o} to 0600 — it "
                  "holds credentials", file=_sys.stderr)
        except OSError:
            print(f"[pstb] WARNING: {path} is mode {mode:04o} and readable "
                  "by other accounts on this host; could not chmod it",
                  file=_sys.stderr)


def load_config(path: Optional[str] = None) -> Config:
    """Load config.yaml, searched as: path arg > $PSTB_CONFIG > ./config.yaml
    > <package root>/config.yaml. Then overlay env vars.

    The package-root fallback is what makes every entry point safe to launch
    from ANY working directory. Without it, `cd scripts && python -m pstb.gui`
    found no config in the cwd and silently served the built-in sqlite
    defaults — on a real deployment that surfaced as every API call failing
    with "SQLite sample database not found" while a correct config.yaml sat
    one directory up.
    """
    cfg_path = resolve_config_path(path)
    cfg = Config(root=cfg_path.parent if cfg_path.exists() else _PACKAGE_ROOT)

    # .env sits next to the config file (or cwd); load before reading env overlays.
    try:
        from dotenv import load_dotenv

        # interpolate=False, and it is not cosmetic: dotenv expands ${...}
        # in BOTH quote styles, so an Oracle or QAS password containing a
        # literal ${ is silently rewritten before anything sees it.
        # Measured: ORACLE_PASSWORD='Pa${ss}w0rd!2026' reads back as
        # 'Paw0rd!2026' and authentication fails with a correct password in
        # the file. Nothing in this deployment substitutes variables into
        # .env, so the expansion has no upside to trade against.
        _tighten(cfg.root / ".env")
        load_dotenv(cfg.root / ".env", interpolate=False)
    except ImportError:
        pass

    if cfg_path.exists():
        try:
            import yaml
        except ImportError as e:
            raise RuntimeError(
                "PyYAML is required to read config.yaml — install with: pip install -e ."
            ) from e
        data = yaml.safe_load(cfg_path.read_text()) or {}
        # The configuration console writes config.local.yaml rather than
        # editing config.yaml, whose comments carry the reasoning for values
        # that took measurement to find. Merged one section deep: the
        # overlay names individual keys, never whole blocks, so an entry
        # here replaces one setting and leaves the rest of its section
        # alone. Malformed overlay is reported, never silently ignored —
        # a console save that appears to work and changes nothing is worse
        # than an error.
        overlay_path = cfg_path.parent / "config.local.yaml"
        if overlay_path.exists():
            try:
                overlay = yaml.safe_load(overlay_path.read_text()) or {}
            except yaml.YAMLError as e:
                raise RuntimeError(
                    f"{overlay_path.name} is not valid YAML ({e}). It is "
                    "written by the configuration console and is safe to "
                    "delete — doing so reverts to config.yaml.") from e
            for section, block in (overlay or {}).items():
                if isinstance(block, dict) and isinstance(
                        data.get(section), dict):
                    data[section] = {**data[section], **block}
                elif isinstance(block, dict):
                    data[section] = block
        _validate_multi_schema_backend(data.get("db"), section="db")
        _validate_bigquery_source(data.get("db"), section="db")
        _apply_section(cfg.defaults, data.get("defaults"))
        _apply_section(cfg.db, data.get("db"))
        _apply_section(cfg.llm, data.get("llm"))
        _apply_section(cfg.wiki, data.get("wiki"))
        _apply_section(cfg.tools, data.get("tools"))
        _apply_section(cfg.process_graph, data.get("process_graph"))
        _apply_section(cfg.metadata_catalog, data.get("metadata_catalog"))
        _apply_section(cfg.semantic_retrieval, data.get("semantic_retrieval"))
        _apply_section(cfg.anomalies, data.get("anomalies"))
        _apply_section(cfg.coupa, data.get("coupa"))
        _validate_coupa(cfg.coupa)
        _apply_section(cfg.ps_api, data.get("ps_api"))
        _apply_section(cfg.security, data.get("security"))
        _apply_section(cfg.ticker, data.get("ticker"))
        _validate_ticker(cfg.ticker)
        _validate_security(cfg.security)
        if isinstance(data.get("semantics"), dict):
            cfg.semantics = data["semantics"]
        for name, block in (data.get("sources") or {}).items():
            _validate_multi_schema_backend(
                block, section=f"sources.{name}")
            _validate_bigquery_source(
                block, section=f"sources.{name}")
            src = DbCfg()
            _apply_section(src, block)
            # Per-source credentials come from env vars named after the
            # source (PSTB_SRC_<NAME>_DSN/USER/PASSWORD), so secrets stay in
            # .env with the same handling as the primary connection.
            key = str(name).upper().replace("-", "_")
            src.oracle_dsn = _env(f"PSTB_SRC_{key}_DSN", src.oracle_dsn)
            src.oracle_user = _env(f"PSTB_SRC_{key}_USER", src.oracle_user)
            src.oracle_password = _env(f"PSTB_SRC_{key}_PASSWORD",
                                       src.oracle_password)
            src.bigquery_project = _env(f"PSTB_SRC_{key}_BQ_PROJECT",
                                        src.bigquery_project)
            cfg.sources[str(name)] = src

    # Do this after both YAML layers have been applied.  It turns the accepted
    # list shorthand back into the scalar contract every database consumer
    # historically expects, while retaining the full hard allowlist.
    normalize_db_schemas(cfg.db, section="db")
    for name, src in cfg.sources.items():
        normalize_db_schemas(src, section=f"sources.{name}")

    d, l, w = cfg.db, cfg.llm, cfg.wiki
    cfg.ps_api.user = _env("PSFT_QAS_USER", cfg.ps_api.user)
    cfg.ps_api.password = _env("PSFT_QAS_PASSWORD", cfg.ps_api.password)
    d.oracle_dsn = _env("ORACLE_DSN", d.oracle_dsn)
    d.oracle_user = _env("ORACLE_USER", d.oracle_user)
    d.oracle_password = _env("ORACLE_PASSWORD", d.oracle_password)
    d.oracle_config_dir = _env("TNS_ADMIN", d.oracle_config_dir)
    d.oracle_wallet_dir = _env("ORACLE_WALLET_DIR", d.oracle_wallet_dir)
    d.oracle_wallet_password = _env("ORACLE_WALLET_PASSWORD",
                                    d.oracle_wallet_password)
    d.mssql_conn_str = _env("MSSQL_CONN_STR", d.mssql_conn_str)
    l.provider = _env("PSTB_LLM_PROVIDER", l.provider)
    l.ollama_host = _env("OLLAMA_HOST", l.ollama_host)
    l.gemini_project = _env("GOOGLE_CLOUD_PROJECT", l.gemini_project)
    l.gemini_location = _env("GOOGLE_CLOUD_LOCATION", l.gemini_location)
    w.confluence_base_url = _env("CONFLUENCE_BASE_URL", w.confluence_base_url)
    w.confluence_labels = _env("CONFLUENCE_LABELS", w.confluence_labels)
    w.confluence_email = _env("CONFLUENCE_EMAIL", w.confluence_email)
    w.confluence_api_token = _env("CONFLUENCE_API_TOKEN", w.confluence_api_token)
    # Lets a harness point the wiki somewhere else without editing config.yaml
    # — the eval uses it to test policy answers against ordinary documents
    # rather than the bundled demo pages, which the evidence gate refuses.
    w.provider = _env("PSTB_WIKI_PROVIDER", w.provider)
    w.localdocs_path = _env("PSTB_WIKI_LOCALDOCS_PATH", w.localdocs_path)
    return cfg
