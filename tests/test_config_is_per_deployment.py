"""A pull must never overwrite the settings on a deployed machine.

config.yaml carries what is true of ONE installation — which database, which
schema, which business unit, which model provider. It used to be tracked, so
every `git pull` on the box either clobbered those values or stopped on a
merge conflict in a file nobody meant to edit in git.

The shape that fixes it has three layers, and each one is pinned here:

    config.example.yaml   tracked, shipped, replaced on every upgrade
    config.yaml           yours, git-ignored, wins when present
    config.local.yaml     the console's, git-ignored, wins key by key

The load order is what makes it safe, and the fallback is what keeps it
cheap: with no config.yaml at all — a fresh clone, or CI — the app reads the
example and runs, so nothing has to remember a copy step.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import (  # noqa: E402
    CONFIG_NAME, EXAMPLE_NAME, base_config_path, load_config,
    resolve_config_path,
)


def _git(*args: str) -> str:
    out = subprocess.run(("git",) + args, cwd=str(ROOT),
                         capture_output=True, text=True)
    return out.stdout


class WhatGitTracksTests(unittest.TestCase):
    """The tracked file is the example. Never the deployment's own."""

    def test_the_example_is_tracked(self) -> None:
        self.assertTrue((ROOT / EXAMPLE_NAME).exists())
        self.assertIn(EXAMPLE_NAME, _git("ls-files", EXAMPLE_NAME))

    def test_the_deployments_own_config_is_not_tracked(self) -> None:
        self.assertEqual(_git("ls-files", CONFIG_NAME).strip(), "",
                         "config.yaml is tracked again — a pull on the "
                         "target box will overwrite that machine's settings")

    def test_every_per_deployment_file_is_ignored(self) -> None:
        # Named individually rather than as a glob: each of these is a file
        # someone edits on ONE machine, and a pull must not touch any of them.
        for name in (CONFIG_NAME, "config.local.yaml", ".env",
                     "site_memory.json", "BUILD_INFO.json"):
            with self.subTest(name):
                out = _git("check-ignore", "-v", name).strip()
                self.assertTrue(out, f"{name} is not ignored")

    def test_the_example_is_not_ignored(self) -> None:
        # The mirror image: an ignore rule wide enough to swallow the example
        # would leave a fresh clone with no config at all.
        self.assertEqual(_git("check-ignore", EXAMPLE_NAME).strip(), "")


class FallbackTests(unittest.TestCase):
    """No config.yaml is a working state, not a broken one."""

    def _tree(self, *, real: bool) -> Path:
        d = Path(tempfile.mkdtemp())
        (d / EXAMPLE_NAME).write_text(
            "defaults:\n  business_unit: EXAMPLE\n  ledger: ACTUALS\n")
        if real:
            (d / CONFIG_NAME).write_text("defaults:\n  business_unit: MINE\n")
        return d

    def test_a_clone_with_only_the_example_loads(self) -> None:
        d = self._tree(real=False)
        self.assertEqual(base_config_path(d).name, EXAMPLE_NAME)
        self.assertEqual(load_config(str(d / CONFIG_NAME))
                         .defaults.business_unit, "EXAMPLE")

    def test_a_deployments_own_config_wins(self) -> None:
        d = self._tree(real=True)
        self.assertEqual(base_config_path(d).name, CONFIG_NAME)
        self.assertEqual(load_config(str(d / CONFIG_NAME))
                         .defaults.business_unit, "MINE")

    def test_an_upgrade_cannot_revert_a_deployment_to_sample_values(self) -> None:
        # The fallback runs one way only. If it preferred a NEWER example,
        # shipping a release would silently repoint a live box at the sample
        # ledger — which is the failure this whole change exists to prevent.
        d = self._tree(real=True)
        (d / EXAMPLE_NAME).write_text(
            "defaults:\n  business_unit: BRAND_NEW\ndb:\n  backend: sqlite\n")
        cfg = load_config(str(d / CONFIG_NAME))
        self.assertEqual(cfg.defaults.business_unit, "MINE")

    def test_the_console_overlay_still_applies_over_the_example(self) -> None:
        # A box that never created a config.yaml can still be configured from
        # /console: the overlay sits beside whichever base is in use.
        d = self._tree(real=False)
        (d / "config.local.yaml").write_text(
            "defaults:\n  business_unit: FROM_CONSOLE\n")
        cfg = load_config(str(d / CONFIG_NAME))
        self.assertEqual(cfg.defaults.business_unit, "FROM_CONSOLE")
        self.assertEqual(cfg.defaults.ledger, "ACTUALS",
                         "the overlay names one key and must leave the rest "
                         "of its section alone")

    def test_a_mistyped_path_is_not_answered_with_some_other_tree(self) -> None:
        d = self._tree(real=False)
        asked = d / "typo.yaml"
        self.assertEqual(resolve_config_path(str(asked)), asked,
                         "only config.yaml falls back to the example beside "
                         "it; anything else reports the path asked for")


class ConsoleTests(unittest.TestCase):
    """The console validates against the base actually in use."""

    def test_it_does_not_assume_a_config_yaml_exists(self) -> None:
        source = (ROOT / "pstb" / "gui" / "console.py").read_text()
        self.assertNotIn("src / 'config.yaml'", source,
                         "a hard-coded name refuses every save on a box "
                         "that runs from the example")
        self.assertIn("base_config_path", source)

    def test_a_save_validates_on_a_tree_with_no_config_yaml(self) -> None:
        from pstb.gui.console import _probe_config
        d = Path(tempfile.mkdtemp())
        (d / EXAMPLE_NAME).write_text("defaults:\n  business_unit: US001\n")
        overlay = d / "config.local.yaml"
        overlay.write_text("llm:\n  temperature: 0.3\n")
        ok, why = _probe_config(d, overlay)
        self.assertTrue(ok, why)

    def test_a_bad_overlay_is_still_refused_there(self) -> None:
        from pstb.gui.console import _probe_config
        d = Path(tempfile.mkdtemp())
        (d / EXAMPLE_NAME).write_text("defaults:\n  business_unit: US001\n")
        overlay = d / "config.local.yaml"
        overlay.write_text("llm:\n  : : :\n")
        ok, _ = _probe_config(d, overlay)
        self.assertFalse(ok, "the fallback must not weaken validation")


if __name__ == "__main__":
    unittest.main()
