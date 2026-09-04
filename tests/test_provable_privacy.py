"""What the harness may keep: verdicts and counts, never words.

The summary is the PERSISTABLE artifact, so it must be structurally
unable to carry a question, an answer, or a party name; the detail
file is private evidence and must be born owner-only; and the
throwaway qlog each case runs through must not outlive its case --
pass or fail.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from pstb.evalharness import report
from pstb.evalharness.runner import _write_private_json

ROW = {"id": "tb-balances", "kind": "figure",
       "pstb_verdict": "proved", "raw_verdict": "abstained",
       "joint": "both_honest",
       "figure_counts": {"pstb": 2, "raw": 0}, "seconds": 3.1}
META = {"backend": "sqlite", "sample_db": True,
        "providers": {"pstb": {"name": "gemini", "model": ""},
                      "raw": {"name": "claude", "model": "",
                              "prompt_variant": "a"}}}


class SummaryPrivacyTests(unittest.TestCase):
    def test_the_summary_carries_no_text_channels(self):
        summary = report.build_summary(results=[dict(ROW)], meta=META)
        blob = json.dumps(summary)
        # KEY forms, not bare substrings: the harness version string
        # "provable_answers_v1" legitimately contains "answer".
        for banned in ('"question":', '"answer":', '"calls":',
                       "SENTINEL", "Kestrel Holloway"):
            self.assertNotIn(banned, blob)

    def test_a_row_smuggling_an_answer_is_refused(self):
        for key in ("answer", "question", "calls"):
            with self.subTest(key=key):
                row = dict(ROW)
                row[key] = "the ending balance is 9,999.99"
                with self.assertRaises(ValueError):
                    report.build_summary(results=[row], meta=META)

    def test_an_unknown_row_key_is_refused_not_forwarded(self):
        row = dict(ROW)
        row["notes"] = "vendor JANE DOE LLC still owes 1,234.56"
        with self.assertRaises(ValueError):
            report.build_summary(results=[row], meta=META)


class PrivateFileTests(unittest.TestCase):
    def test_detail_files_are_born_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "detail.json"
            _write_private_json(target, {"details": [{"answer": "x"}]})
            mode = os.stat(target).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class ThrowawayQlogTests(unittest.TestCase):
    def test_the_temp_qlog_dies_with_its_case(self):
        """run_pstb_arm deletes its qlog directory on BOTH paths --
        return and raise -- so no per-case question text survives in
        the report directory. The p2go silo scope keeps the prompt
        builder off the config object; the exploding provider fails
        the turn from INSIDE the try, which is the path that leaks."""
        import asyncio
        from types import SimpleNamespace
        from pstb.evalharness import arms

        class FakeSession:
            async def list_tools(self):
                return SimpleNamespace(tools=[])

        class ExplodingProvider:
            name = "boom"

            def send_user(self, _text):
                raise RuntimeError("scripted failure inside the turn")

        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            case = {"id": "boom", "question": "what is the balance?",
                    "scope": {"source": "p2go"}}
            try:
                asyncio.run(arms.run_pstb_arm(
                    FakeSession(), SimpleNamespace(), "ollama", case,
                    report_dir=report_dir,
                    provider_factory=lambda p, t, o:
                        ExplodingProvider()))
            except Exception:                     # noqa: BLE001
                pass
            leftovers = [p.name for p in report_dir.glob(".qlog-*")]
        self.assertEqual(leftovers, [],
                         "a case's throwaway qlog survived its case")


if __name__ == "__main__":
    unittest.main()
