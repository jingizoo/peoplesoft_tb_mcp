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
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config  # noqa: E402
from pstb.config import DbCfg  # noqa: E402
from pstb.db import Database, DbError  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
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

    def test_describe_exposes_slash_workspace_contract(self) -> None:
        self.cfg.sources = {"p2go": DbCfg(backend="sqlite")}
        described = self.registry.describe()
        finance, p2go = described
        self.assertEqual(
            {key: finance[key] for key in
            ("source", "command", "label", "mode", "curated_tools")},
            {"source": "default", "command": "finance",
             "label": "Finance", "mode": "finance",
             "curated_tools": True},
        )
        self.assertEqual(p2go["command"], "p2go")
        self.assertEqual(p2go["mode"], "semantic_read_only")
        self.assertFalse(p2go["curated_tools"])
        self.assertIn("semantic and relationship", p2go["role"])

    def test_command_resolution_is_case_insensitive_and_exact(self) -> None:
        self.cfg.sources = {"P2Go": DbCfg(backend="sqlite")}
        self.assertEqual(self.registry.resolve_command("finance"), "default")
        self.assertEqual(self.registry.resolve_command("/PS"), "default")
        self.assertEqual(self.registry.resolve_command("p2go"), "P2Go")
        with self.assertRaises(DbError) as ctx:
            self.registry.resolve_command("warehouse")
        self.assertIn("/finance", str(ctx.exception))
        self.assertIn("/p2go", str(ctx.exception))

    def test_reserved_and_normalized_commands_are_collision_safe(self) -> None:
        self.cfg.sources = {
            "finance": DbCfg(backend="sqlite"),
            "P2 Go": DbCfg(backend="sqlite"),
            "p2-go": DbCfg(backend="sqlite"),
        }
        by_source = {item["source"]: item for item in self.registry.describe()}
        self.assertEqual(by_source["default"]["command"], "finance")
        self.assertEqual(by_source["finance"]["command"], "db-finance")
        self.assertNotEqual(by_source["P2 Go"]["command"],
                            by_source["p2-go"]["command"])
        commands = [item["command"] for item in by_source.values()]
        self.assertEqual(len(commands), len(set(commands)))
        for source, item in by_source.items():
            self.assertEqual(self.registry.resolve_command(item["command"]),
                             source)

    def test_case_insensitive_source_name_collision_fails_at_init(self) -> None:
        cfg = Config.sample(ROOT)
        cfg.sources = {
            "P2Go": DbCfg(backend="sqlite"),
            "p2go": DbCfg(backend="sqlite"),
        }
        with self.assertRaises(DbError) as ctx:
            SourceRegistry(cfg, self.db)
        self.assertIn("case-insensitively", str(ctx.exception))
        self.assertIn("hard database boundary", str(ctx.exception))

    def test_secondary_named_finance_remains_valid_but_is_remapped(self) -> None:
        cfg = Config.sample(ROOT)
        cfg.sources = {"finance": DbCfg(backend="sqlite")}
        registry = SourceRegistry(cfg, self.db)
        described = {item["source"]: item for item in registry.describe()}
        self.assertEqual(described["default"]["command"], "finance")
        self.assertEqual(described["finance"]["command"], "db-finance")
        self.assertEqual(registry.resolve_command("finance"), "default")
        self.assertEqual(registry.resolve_command("db-finance"), "finance")


class SourceEngineConfigTests(unittest.TestCase):
    def test_secondary_oracle_catalog_checks_use_its_own_schema(self) -> None:
        primary_cfg = Config.sample(ROOT)
        primary_cfg.db.backend = "oracle"
        primary_cfg.db.schema = "PSOWNER"
        source_cfg = Config.sample(ROOT)
        source_cfg.db.backend = "oracle"
        source_cfg.db.schema = "P2OWNER"

        class FakeOracle:
            dialect = "oracle"

            def __init__(self, cfg):
                self.cfg = cfg
                self.calls = []

            def query(self, sql, params, max_rows=1):
                self.calls.append({"sql": sql, "params": dict(params)})
                return ([{"x": 1}], ["x"])

        primary = FakeOracle(primary_cfg)
        p2go = FakeOracle(source_cfg)
        engine = TBEngine(primary, primary_cfg)
        engine.registry = SimpleNamespace(get=lambda name: p2go)

        child = engine.for_source("p2go")
        self.assertIs(child.cfg, source_cfg)
        self.assertTrue(child._table_exists("P2_INVOICE"))
        self.assertEqual(p2go.calls[-1]["params"]["o"], "P2OWNER")
        self.assertNotEqual(p2go.calls[-1]["params"]["o"], "PSOWNER")


if __name__ == "__main__":
    unittest.main()
