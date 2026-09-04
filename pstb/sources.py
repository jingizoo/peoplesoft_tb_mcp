"""Named database sources for ask-anything beyond the PeopleSoft primary.

The primary connection (config `db:`) is the DEFAULT source — every curated
PeopleSoft tool uses it and nothing about it changes. Entries under
`sources:` are additional databases (a reporting mart, an extract schema,
another application's DB) reachable from the guarded ad-hoc tools.  Their
structure can also be indexed by the offline metadata catalog; curated
financial tools continue to use the PeopleSoft primary only.

Each source gets its own Database instance — its own credentials, session
pool, dialect branching, schema catalog, and error translation — built
lazily on first use. The guardrail stack is therefore identical everywhere:
SELECT-only, single statement, row caps, catalog-validated table names,
schema qualification, translated errors.
"""
from __future__ import annotations

import re
import threading

from .config import Config, normalize_db_schemas
from .db import Database, DbError


_PRIMARY_ALIASES = {"peoplesoft", "people soft", "ps", "psft", "primary",
                    "main", "finance", "erp", "gl"}


class SourceRegistry:
    def __init__(self, cfg: Config, primary: Database):
        normalize_db_schemas(cfg.db, section="db")
        for source_name, source_cfg in (cfg.sources or {}).items():
            normalize_db_schemas(
                source_cfg, section=f"sources.{source_name}")
            # Backfill for hand-assembled configs; the loader's stamp
            # wins when it ran.
            if not getattr(source_cfg, "source_name", ""):
                source_cfg.source_name = str(source_name)
        # Every later boundary comparison is case-insensitive.  Accepting
        # both ``P2Go`` and ``p2go`` here would therefore make two credentials
        # collapse to one guard identity even though dict lookup still treated
        # them as different databases.  Fail before either can be queried.
        seen = {"default": "the PeopleSoft primary"}
        for configured in cfg.sources or {}:
            name = str(configured).strip()
            key = name.casefold()
            if not name:
                raise DbError("Configured database source names cannot be blank")
            if key in seen:
                raise DbError(
                    f"Configured database source {configured!r} conflicts "
                    f"case-insensitively with {seen[key]!r}; source names "
                    "must be unique across the hard database boundary"
                )
            seen[key] = name
        self.cfg = cfg
        self._primary = primary
        self._dbs: dict[str, Database] = {}
        self._lock = threading.Lock()

    def names(self) -> list:
        """The default source first, then the configured extras."""
        return ["default"] + sorted(self.cfg.sources)

    def _configured_as(self, name: str) -> str:
        """The configured source matching this name, case-insensitively.

        __init__ already refuses two sources that differ only by case, so at
        most one can match. The alias tests below asked
        ``name not in cfg.sources`` -- an exact-case test guarding a
        case-insensitive alias list, so a site with a source literally named
        "gl" or "main" or "erp" had "GL" answered from the PeopleSoft
        primary instead. That is a silent cross-database misroute, and on
        the approvals path it would apply a decision to the wrong source's
        review queue.
        """
        if not name:
            return ""
        configured = self.cfg.sources or {}
        if name in configured:
            return name
        folded = name.casefold()
        for candidate in configured:
            if str(candidate).casefold() == folded:
                return str(candidate)
        return ""

    def resolve_name(self, source: str = "") -> str:
        """The canonical name get() would answer from, for provenance.

        A payload that says which database answered is only useful if it says
        the name the registry actually used: "peoplesoft" and "" and "default"
        are one connection, and labelling a result with whichever alias the
        caller happened to type would make two identical answers look like
        they came from two places. Unknown names are returned unchanged so
        the caller's own error path reports them.
        """
        name = (source or "").strip()
        if name in ("", "default"):
            return "default"
        configured = self._configured_as(name)
        if name.lower() in _PRIMARY_ALIASES and not configured:
            return "default"
        # Report the spelling the site configured, not the caller's casing:
        # provenance that varies by how the name was typed makes one source
        # look like several.
        return configured or name

    def get(self, source: str = "") -> Database:
        """Database for a named source; '' or 'default' is the primary.

        Unknown names fail with the full catalog of valid ones — the model
        (or user) should correct the name, not guess again.
        """
        name = (source or "").strip()
        if name in ("", "default"):
            return self._primary
        # The primary source IS the PeopleSoft database, so "peoplesoft" is
        # the name anyone would reach for first. Refusing it on a technicality
        # spends a whole turn on a correction that teaches nothing — accept
        # the obvious aliases, unless the site has configured a real source
        # under that name, which always wins.
        configured = self._configured_as(name)
        if name.lower() in _PRIMARY_ALIASES and not configured:
            return self._primary
        name = configured or name
        if name not in self.cfg.sources:
            raise DbError(
                f"Unknown source {name!r}. Configured sources: "
                f"{', '.join(self.names())}. Add new ones under 'sources:' "
                "in config.yaml."
            )
        with self._lock:
            if name not in self._dbs:
                # Each source resolves paths against the same deployment root
                # but carries its own connection settings.
                src_cfg = Config(root=self.cfg.root,
                                 defaults=self.cfg.defaults,
                                 db=self.cfg.sources[name],
                                 llm=self.cfg.llm, wiki=self.cfg.wiki,
                                 tools=self.cfg.tools)
                self._dbs[name] = Database(src_cfg)
            return self._dbs[name]

    def resolve_command(self, token: str) -> str:
        """Resolve a UI slash-workspace token to its exact registry source.

        The browser sends the command, not a caller-selected ``source``.  A
        dedicated source route can therefore derive its database boundary
        from the URL and reject a conflicting body scope.  Matching is
        case-insensitive for human input; the returned source preserves the
        configured key's exact case.
        """
        command = str(token or "").strip().lstrip("/").lower()
        for item in self.describe():
            accepted = {str(item["command"]).lower()}
            accepted.update(str(alias).lower()
                            for alias in item.get("aliases") or [])
            if command in accepted:
                return str(item["source"])
        available = ", ".join(
            f"/{item['command']}" for item in self.describe()
        )
        raise DbError(
            f"Unknown data workspace /{command or '?'}. Available: "
            f"{available}."
        )

    def describe(self) -> list:
        """Connection-free summary for the list_sources tool and the GUI.

        ``command`` is the stable, unambiguous slash workspace exposed by the
        browser.  It deliberately is not inferred in JavaScript: configured
        source names may contain spaces or punctuation, and a real secondary
        source may even be named ``finance``.  The primary always owns the
        human-facing ``/finance`` command; collisions receive a deterministic
        ``db-`` prefix while ``source`` remains the exact registry key used by
        the database boundary.
        """
        out = [{
            "source": "default",
            "command": "finance",
            "aliases": ["ps", "peoplesoft"],
            "label": "Finance",
            "backend": self.cfg.db.backend,
            "schema": self.cfg.db.schema or None,
            "schemas": list(self.cfg.db.schemas),
            "mode": "finance",
            "curated_tools": True,
            "role": "primary finance system — all curated tools answer from here",
        }]
        used_commands = {"finance", "ps", "peoplesoft"}
        for name in sorted(self.cfg.sources):
            src = self.cfg.sources[name]
            command = re.sub(
                r"[^a-z0-9_-]+", "-", str(name).strip().lower()
            ).strip("-_") or "source"
            if command in used_commands:
                command = f"db-{command}"
            base, suffix = command, 2
            while command in used_commands:
                command = f"{base}-{suffix}"
                suffix += 1
            used_commands.add(command)
            out.append({
                "source": name,
                "command": command,
                "aliases": [],
                "label": str(name),
                "backend": src.backend,
                "schema": src.schema or None,
                "schemas": list(src.schemas),
                "mode": "semantic_read_only",
                "curated_tools": False,
                "role": "read-only semantic and relationship discovery "
                        "with guarded queries; primary-system curated tools are "
                        "not available in this workspace",
            })
        return out
