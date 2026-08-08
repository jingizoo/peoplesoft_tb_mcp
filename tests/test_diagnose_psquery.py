"""The preflight must name the unmet precondition, and never leak a secret.

run_ps_query falls back to FIXTURES when anything is missing rather than
failing — deliberate, so the demo works with no credentials, but it means
a half-configured site gets sample rows that look real. The only tell is
`mode: fixtures` buried in the payload. This script exists so the tell is
a sentence instead.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "diagnose_psquery.py"


def _run(env_extra=None):
    import os
    env = dict(os.environ)
    env.pop("PSFT_QAS_USER", None)
    env.pop("PSFT_QAS_PASSWORD", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=300,
                          env=env)


class PreflightTests(unittest.TestCase):
    def test_unconfigured_reports_not_live_and_exits_nonzero(self) -> None:
        out = _run()
        self.assertEqual(out.returncode, 1, out.stdout[-800:])
        self.assertIn("NOT LIVE", out.stdout)
        self.assertIn("fixtures", out.stdout)

    def test_it_names_every_missing_precondition(self) -> None:
        text = _run().stdout
        for expected in ("ps_api.enabled", "PSFT_QAS_USER",
                         "PSFT_QAS_PASSWORD"):
            self.assertIn(expected, text,
                          f"{expected} missing from the checklist")

    def test_it_says_the_curated_tools_still_work(self) -> None:
        # Otherwise "not live" reads as "the product is broken".
        self.assertIn("curated database tools", _run().stdout)

    def test_the_password_is_never_printed(self) -> None:
        out = _run({"PSFT_QAS_USER": "QASUSER",
                    "PSFT_QAS_PASSWORD": "hunter2-should-never-appear"})
        self.assertNotIn("hunter2", out.stdout + out.stderr)
        self.assertIn("PSFT_QAS_PASSWORD: set", out.stdout)
        self.assertIn("QASUSER", out.stdout,
                      "the USER is shown on purpose — it explains whose "
                      "permission lists shaped the rows")

    def test_it_names_the_node_default_and_how_to_change_it(self) -> None:
        text = _run().stdout
        self.assertIn("PSFT_QAS_NODE", text)
        self.assertIn("404", text, "a wrong node is the usual first failure")


class DocumentationTests(unittest.TestCase):
    def test_env_example_carries_the_qas_settings(self) -> None:
        text = (ROOT / ".env.example").read_text()
        for key in ("PSFT_QAS_USER", "PSFT_QAS_PASSWORD", "PSFT_QAS_NODE"):
            self.assertIn(key, text,
                          f"{key} is read at runtime but absent from the "
                          ".env template, so nobody can find it")

    def test_no_real_secret_is_committed_in_the_template(self) -> None:
        for line in (ROOT / ".env.example").read_text().splitlines():
            if line.startswith("PSFT_QAS_PASSWORD"):
                self.assertEqual(line.strip(), "PSFT_QAS_PASSWORD=")

    def test_config_points_at_the_preflight(self) -> None:
        self.assertIn("diagnose_psquery",
                      (ROOT / "config.yaml").read_text())


if __name__ == "__main__":
    unittest.main()
