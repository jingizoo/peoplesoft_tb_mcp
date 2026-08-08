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
    provider: str = "ollama"  # ollama | gemini
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
    # 0 = auto: 24k chars for local models, 120k for Gemini (1M-token context).
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


def resolve_config_path(path: Optional[str] = None) -> Path:
    """Where the config lives: path arg > $PSTB_CONFIG > ./config.yaml >
    <package root>/config.yaml. Pure path logic (no yaml import), so tests
    can verify it under a bare interpreter."""
    explicit = path or os.environ.get("PSTB_CONFIG")
    if explicit:
        cfg_path = Path(explicit)
        return cfg_path if cfg_path.is_absolute() else Path.cwd() / cfg_path
    cfg_path = Path.cwd() / "config.yaml"
    if not cfg_path.exists() and (_PACKAGE_ROOT / "config.yaml").exists():
        return _PACKAGE_ROOT / "config.yaml"
    return cfg_path


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

        load_dotenv(cfg.root / ".env")
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
        _apply_section(cfg.defaults, data.get("defaults"))
        _apply_section(cfg.db, data.get("db"))
        _apply_section(cfg.llm, data.get("llm"))
        _apply_section(cfg.wiki, data.get("wiki"))
        _apply_section(cfg.tools, data.get("tools"))
        _apply_section(cfg.ps_api, data.get("ps_api"))
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
