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

import threading

from .config import Config
from .db import Database, DbError


_PRIMARY_ALIASES = {"peoplesoft", "people soft", "ps", "psft", "primary",
                    "main", "finance", "erp", "gl"}


class SourceRegistry:
    def __init__(self, cfg: Config, primary: Database):
        self.cfg = cfg
        self._primary = primary
        self._dbs: dict[str, Database] = {}
        self._lock = threading.Lock()

    def names(self) -> list:
        """The default source first, then the configured extras."""
        return ["default"] + sorted(self.cfg.sources)

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
        if (name.lower() in _PRIMARY_ALIASES
                and name not in (self.cfg.sources or {})):
            return self._primary
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

    def describe(self) -> list:
        """Connection-free summary for the list_sources tool and the GUI."""
        out = [{
            "source": "default",
            "backend": self.cfg.db.backend,
            "schema": self.cfg.db.schema or None,
            "role": "PeopleSoft primary — all curated tools answer from here",
        }]
        for name in sorted(self.cfg.sources):
            src = self.cfg.sources[name]
            out.append({
                "source": name,
                "backend": src.backend,
                "schema": src.schema or None,
                "role": "guarded discovery/SQL; structure can be included "
                        "in the offline metadata catalog (curated financial "
                        "tools remain on the PeopleSoft primary)",
            })
        return out
