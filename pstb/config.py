"""Configuration loading: config.yaml + .env overlays.

Stdlib-safe: PyYAML and python-dotenv are used when installed, but the module
imports (and Config.sample()) work without them so the smoke test can run on a
bare interpreter.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional


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
    aging_buckets: list = field(default_factory=lambda: [30, 60, 90])


@dataclass
class DbCfg:
    backend: str = "sqlite"  # sqlite | oracle | sqlserver
    sqlite_path: str = "sample_data/ps_sample.db"
    schema: str = ""
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
    process_graph: ProcessGraphCfg = field(default_factory=ProcessGraphCfg)
    anomalies: AnomalyCfg = field(default_factory=AnomalyCfg)
    security: SecurityCfg = field(default_factory=SecurityCfg)

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
        _apply_section(cfg.defaults, data.get("defaults"))
        _apply_section(cfg.db, data.get("db"))
        _apply_section(cfg.llm, data.get("llm"))
        _apply_section(cfg.wiki, data.get("wiki"))
        _apply_section(cfg.tools, data.get("tools"))
        _apply_section(cfg.process_graph, data.get("process_graph"))
        _apply_section(cfg.anomalies, data.get("anomalies"))
        _apply_section(cfg.ps_api, data.get("ps_api"))
        _apply_section(cfg.security, data.get("security"))
        if isinstance(data.get("semantics"), dict):
            cfg.semantics = data["semantics"]
        for name, block in (data.get("sources") or {}).items():
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
            cfg.sources[str(name)] = src

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
