"""A missing cloud credential must read as a work order, not a doc link.

Google's own message is true and useless here: "Your default credentials
were not found, see <generic page>". It cannot know this deployment runs
headless over SSH, where `gcloud auth application-default login` tries to
open a browser that does not exist and appears to hang. Naming the
--no-launch-browser flag is the difference between a two-minute fix and
an afternoon.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.client.llm_gemini import adc_remedy  # noqa: E402


class RemedyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.msg = adc_remedy("pserp-dev-1234", "credentials were not found")

    def test_it_names_the_headless_flag(self) -> None:
        self.assertIn("--no-launch-browser", self.msg,
                      "without the flag the command hangs on a box with no "
                      "browser, which is every box this ships to")

    def test_it_offers_a_key_file_alternative(self) -> None:
        self.assertIn("GOOGLE_APPLICATION_CREDENTIALS", self.msg)
        self.assertIn("chmod 600", self.msg)

    def test_it_offers_a_way_to_keep_working_now(self) -> None:
        # A blocked user with no path forward stops using the product.
        self.assertIn("ollama", self.msg)

    def test_it_names_the_project_so_the_wrong_one_is_visible(self) -> None:
        self.assertIn("pserp-dev-1234", self.msg)
        self.assertIn("(unset)", adc_remedy(""))

    def test_it_says_the_app_never_holds_the_credential(self) -> None:
        self.assertIn("stored or logged", self.msg)

    def test_it_does_not_just_forward_googles_doc_link(self) -> None:
        body = self.msg.split("Pick one:")[0]
        self.assertLess(body.count("http"), 2,
                        "the remedy must lead with commands, not links")


class FailFastTests(unittest.TestCase):
    def test_credentials_are_checked_before_the_first_question(self) -> None:
        # Resolving at construction turns "the answer failed" into "this
        # provider could not start", which the surface can attribute.
        import inspect

        from pstb.client import llm_gemini
        src = inspect.getsource(llm_gemini.GeminiVertexProvider.__init__)
        self.assertIn("_require_adc(cfg)", src)
        self.assertLess(src.index("_require_adc(cfg)"), src.index("genai.Client"),
                        "check credentials before building the client")

    def test_a_mid_session_expiry_gets_the_same_remedy(self) -> None:
        import inspect

        from pstb.client import llm_gemini
        src = inspect.getsource(llm_gemini.GeminiVertexProvider._generate)
        self.assertIn("RefreshError", src,
                      "a token revoked mid-session is not transient and "
                      "must not be retried into the generic message")
        self.assertIn("adc_remedy", src)


if __name__ == "__main__":
    unittest.main()
