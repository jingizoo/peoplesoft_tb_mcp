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
    mssql_conn_str: str = ""
    query_timeout_seconds: int = 120


@dataclass
class LlmCfg:
    provider: str = "ollama"  # ollama | gemini
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    gemini_model: str = "gemini-2.5-pro"
    gemini_project: str = ""
    gemini_location: str = "us-central1"
    # -1 = model default (dynamic thinking); 0 disables thinking (flash only —
    # 2.5-pro enforces a minimum); >0 caps the thinking token budget.
    gemini_thinking_budget: int = -1
    temperature: float = 0.2
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


@dataclass
class Config:
    root: Path = field(default_factory=Path.cwd)
    defaults: Defaults = field(default_factory=Defaults)
    db: DbCfg = field(default_factory=DbCfg)
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


def load_config(path: Optional[str] = None) -> Config:
    """Load config.yaml (path arg > $PSTB_CONFIG > ./config.yaml) then overlay env vars."""
    cfg_path = Path(path or os.environ.get("PSTB_CONFIG") or "config.yaml")
    if not cfg_path.is_absolute():
        cfg_path = Path.cwd() / cfg_path
    cfg = Config(root=cfg_path.parent if cfg_path.exists() else Path.cwd())

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

    d, l, w = cfg.db, cfg.llm, cfg.wiki
    d.oracle_dsn = _env("ORACLE_DSN", d.oracle_dsn)
    d.oracle_user = _env("ORACLE_USER", d.oracle_user)
    d.oracle_password = _env("ORACLE_PASSWORD", d.oracle_password)
    d.mssql_conn_str = _env("MSSQL_CONN_STR", d.mssql_conn_str)
    l.provider = _env("PSTB_LLM_PROVIDER", l.provider)
    l.ollama_host = _env("OLLAMA_HOST", l.ollama_host)
    l.gemini_project = _env("GOOGLE_CLOUD_PROJECT", l.gemini_project)
    l.gemini_location = _env("GOOGLE_CLOUD_LOCATION", l.gemini_location)
    w.confluence_base_url = _env("CONFLUENCE_BASE_URL", w.confluence_base_url)
    w.confluence_labels = _env("CONFLUENCE_LABELS", w.confluence_labels)
    w.confluence_email = _env("CONFLUENCE_EMAIL", w.confluence_email)
    w.confluence_api_token = _env("CONFLUENCE_API_TOKEN", w.confluence_api_token)
    return cfg
