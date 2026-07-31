"""Source naming: accept what a caller would obviously say.

The primary source IS the PeopleSoft database, but it is registered as
"default". A model asked to query PeopleSoft naturally passes
source="PeopleSoft", got an error, and spent a turn recovering — a correction
that teaches nothing and sometimes was not recovered from at all.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config  # noqa: E402
from pstb.db import Database, DbError  # noqa: E402
from pstb.sources import SourceRegistry  # noqa: E402


class SourceAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = Config.sample(ROOT)
        self.db = Database(self.cfg)
        self.registry = SourceRegistry(self.cfg, self.db)

    def tearDown(self) -> None:
        self.db.close()

    def test_the_obvious_names_reach_the_primary(self) -> None:
        for name in ("", "default", "PeopleSoft", "peoplesoft", "PS", "psft",
                     "primary", "main", "ERP", "gl"):
            self.assertIs(self.registry.get(name), self.db,
                          f"source={name!r} did not reach the primary")

    def test_a_genuinely_unknown_name_still_refuses(self) -> None:
        # Aliasing must not become "any string means the primary" — that would
        # silently answer from PeopleSoft when the user asked about a warehouse.
        with self.assertRaises(DbError) as ctx:
            self.registry.get("warehouse")
        self.assertIn("Unknown source", str(ctx.exception))
        self.assertIn("default", str(ctx.exception))

    def test_a_configured_source_beats_the_alias(self) -> None:
        # If a site really names a source "finance", that is what they get.
        self.cfg.sources = {"finance": object()}
        with self.assertRaises(Exception) as ctx:
            self.registry.get("finance")
        self.assertNotIn("Unknown source", str(ctx.exception),
                         "a configured source was shadowed by the alias list")


if __name__ == "__main__":
    unittest.main()
