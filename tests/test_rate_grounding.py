"""Rate grounding: the one class of number the guard never protected.

A percentage passes the withhold guard untouched by design — removing that
exemption would withhold every correctly formatted rate answer, because
_FIGURE matches "25.00" inside "25.00%" and the prompt mandates two-decimal
formatting. The cost was that "the standard rate is 18%", recalled from
training data and presented as this company's configured rate, reached the
user with nothing objecting. This suite pins the new caveat layer AND pins
the untouched behaviour of everything around it, because the failure mode
of a guard change is not a crash — it is a working guard quietly widened.
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.client.chat import agent_turn  # noqa: E402
from pstb.client.llm_base import LLMResponse, ToolCall  # noqa: E402
from pstb.guards import (PAYLOAD_DECLARED, USER_STATED,  # noqa: E402
                         payload_numbers, payload_rates, rate_caveat,
                         rate_findings, ungrounded_figures)

SAMPLE_AS_OF = "2026-08-06"


class NothingMovedTests(unittest.TestCase):
    """The prove-nothing-moved pins. If these fail, the money guard changed
    and the blast radius is every answer, not just rate answers."""

    PAYLOADS = ['{"totals": {"total": 21334221.84, "count": 17},'
                ' "rows": [{"amt": -23400.0}, {"amt": 4550000}]}']

    def test_payload_numbers_output_is_unchanged(self) -> None:
        got = payload_numbers(self.PAYLOADS)
        # Exact payload values, plus the thousand/million restatements the
        # guard already grounds (it DIVIDES: 21,334,221.84 -> 21334.22 ->
        # 21.33), plus the magnitude of the signed credit.
        for expected in ("21334221.84", "17", "-23400", "4550000",
                         "21334.22", "21.33", "4.55", "4550"):
            self.assertIn(expected, got,
                          f"payload_numbers lost {expected} — the money "
                          "guard's grounding set moved")
        self.assertNotIn("25", got, "payload_numbers gained a value it never "
                                    "produced before")

    def test_money_figures_are_still_withheld(self) -> None:
        self.assertEqual(
            ungrounded_figures("total is 987,654.32", ['{"a": 1}']),
            ["987,654.32"])

    def test_percentages_still_bypass_the_withhold_guard(self) -> None:
        # This exemption must survive forever. Deleting it ships a day-one
        # full-answer withhold on every rate question — _FIGURE matches
        # "25.00" inside "25.00%" and only the exempt span saves it.
        for text in ("the rate is 18%", "tax is 25.00% of sales",
                     "margin ran at 42.50%"):
            self.assertEqual(ungrounded_figures(text, ['{"a": 1}']), [],
                             f"{text!r} reached the withhold guard")


class DeclaredOnlyGroundingTests(unittest.TestCase):
    """Grounding is declared-only, and the restraint is the design."""

    def test_percent_shaped_keys_ground_a_rate(self) -> None:
        # Both orders, because this repo produces both: share_pct and
        # change_percent, but also pct_used and percent_complete.
        for key in ("share_pct", "pct", "change_percent", "pct_used",
                    "percent_complete", "tax_rate", "effective_tax_rate",
                    "gst_rate"):
            rates = payload_rates([json.dumps({key: 18.0})])
            self.assertEqual(rates.get("18"), PAYLOAD_DECLARED,
                             f"{key} did not declare a rate")

    def test_a_bare_rate_key_is_a_multiplier_not_a_percent(self) -> None:
        # THE bypass this layer was built to stop, found by adversarial
        # review: get_exchange_rate emits {"rate": 18.34567891} and USD/MXN
        # sits near 18, so a single FX call anywhere in the payload window
        # silenced the caveat on "the standard rate is 18%" entirely.
        self.assertEqual(
            payload_rates([json.dumps({"rate": 18.34567891})]), {})
        self.assertEqual(
            [f["rate"] for f in rate_findings(
                "The standard GST rate is 18%.",
                ['{"rate": 18.34567891, "to_currency": "MXN"}'])],
            ["18%"])

    def test_rate_bearing_words_are_not_mistaken_for_rates(self) -> None:
        for key in ("rate_type", "corporate", "separate", "exchange_rate"):
            self.assertEqual(payload_rates([json.dumps({key: 18.0})]), {},
                             f"{key} was read as a declared percentage")

    def test_rates_are_never_derived_from_pairs_of_numbers(self) -> None:
        # The measured lesson: a pairwise a/b*100 closure over one realistic
        # payload grounded 62 of the 101 integer percentages, including the
        # very fabrication it existed to catch. Never again.
        rates = payload_rates([json.dumps(
            {"tax": 250000, "sales": 1000000, "period": 18, "count": 25})])
        self.assertEqual(rates, {},
                         "a rate was grounded by arithmetic the guard did "
                         "itself — this re-opens the fabrication hole")

    def test_a_producer_may_declare_a_ratio_explicitly(self) -> None:
        rates = payload_rates([json.dumps({"ratios": [
            {"numerator": 250000, "denominator": 1000000, "pct": 25.0,
             "basis": "net taxable base"}]})])
        self.assertEqual(rates.get("25"), PAYLOAD_DECLARED)

    def test_a_declared_ratio_without_pct_is_computed_from_its_own_pair(self) -> None:
        rates = payload_rates([json.dumps({"ratios": [
            {"numerator": 1, "denominator": 4}]})])
        self.assertEqual(rates.get("25"), PAYLOAD_DECLARED)

    def test_a_percent_key_may_hold_a_map_or_list(self) -> None:
        # get_ar_aging declares bucket_share_pct as a MAP of bucket ->
        # share. Requiring a scalar under the key caveated a share the tool
        # had just computed — found by sweeping real payloads, not by
        # reading the code.
        rates = payload_rates([json.dumps(
            {"bucket_share_pct": {"current": 26.01, "1-30": 51.23}})])
        self.assertEqual(rates.get("26.01"), PAYLOAD_DECLARED)
        self.assertEqual(rates.get("51.23"), PAYLOAD_DECLARED)
        self.assertEqual(
            payload_rates([json.dumps({"share_pct": [12.5, 87.5]})]).get("12.5"),
            PAYLOAD_DECLARED)

    def test_rhetorical_zero_and_hundred_are_never_flagged(self) -> None:
        for text in ("I am 100% confident in this reconciliation.",
                     "0% of invoices are disputed.",
                     "100 percent of vouchers posted."):
            self.assertEqual(rate_findings(text, ['{"a": 1}']), [],
                             f"idiom flagged as an unverified rate: {text!r}")

    def test_booleans_are_not_rates(self) -> None:
        self.assertEqual(payload_rates([json.dumps({"is_rate": True})]), {})


class FindingTests(unittest.TestCase):
    def test_the_fabricated_standard_rate_is_caught(self) -> None:
        found = rate_findings("the standard rate is 18%", ['{"total": 250000}'])
        self.assertEqual([f["rate"] for f in found], ["18%"])
        self.assertIsNone(found[0]["source"])

    def test_a_declared_rate_produces_no_finding(self) -> None:
        self.assertEqual(
            rate_findings("share is 42.5%", ['{"share_pct": 42.5}']), [])

    def test_the_users_own_figure_is_tagged_not_condemned(self) -> None:
        found = rate_findings("25% is high for GST", ['{"a": 1}'],
                              "our GST works out to 25% — is that right?")
        self.assertEqual(found[0]["source"], USER_STATED)
        self.assertIn("you gave me", rate_caveat(found))

    def test_a_payload_declaration_outranks_the_users_figure(self) -> None:
        found = rate_findings("it is 25%", ['{"tax_rate": 25.0}'],
                              "is 25% right?")
        self.assertEqual(found, [], "a rate the ledger produced was "
                                    "downgraded to the user's own number")

    def test_rounded_restatement_of_a_declared_rate_is_accepted(self) -> None:
        self.assertEqual(
            rate_findings("about 8.1%", ['{"share_pct": 8.05}']), [])

    def test_a_declining_percentage_is_not_false_caveated(self) -> None:
        # A declared change_pct of -21.84 is written in prose as "down
        # 21.84%" — _RATE has no sign group, so a signed grounding key
        # would caveat every falling percentage the repo computes.
        self.assertEqual(
            rate_findings("revenue fell 21.84%", ['{"change_pct": -21.84}']),
            [])
        self.assertEqual(
            rate_findings("down about 21.8%", ['{"change_pct": -21.84}']), [])

    def test_more_than_three_rates_says_and_others(self) -> None:
        caveat = rate_caveat(rate_findings(
            "18%, 9%, 3%, 1% and 7%", ['{"a": 1}']))
        self.assertIn("and others", caveat)

    def test_the_word_percent_counts(self) -> None:
        found = rate_findings("roughly 18 percent", ['{"a": 1}'])
        self.assertEqual(len(found), 1)

    def test_a_repeated_rate_is_reported_once(self) -> None:
        found = rate_findings("18% here and 18% there", ['{"a": 1}'])
        self.assertEqual(len(found), 1)

    def test_an_answer_with_no_rates_is_silent(self) -> None:
        self.assertEqual(rate_findings("total is 1,234.00", ['{"a": 1}']), [])
        self.assertEqual(rate_caveat([]), "")

    def test_one_clause_covers_everything(self) -> None:
        caveat = rate_caveat(rate_findings(
            "18% standard, 9% reduced, 3% cess", ['{"a": 1}']))
        self.assertEqual(caveat.count("["), 1)
        self.assertTrue(caveat.startswith("[") and caveat.endswith("]"))


class ScriptedProvider:
    name = "test"
    model = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)

    def _next(self):
        if not self.responses:
            raise AssertionError("scripted provider ran out of responses")
        return self.responses.pop(0)

    def send_user(self, text):
        return self._next()

    def send_tool_results(self, results):
        return self._next()

    def reset(self):
        pass


class FakeSession:
    def __init__(self, outputs):
        self.outputs = outputs

    async def call_tool(self, name, arguments):
        await asyncio.sleep(0)
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(self.outputs[name]))],
            is_error=False)


class TurnIntegrationTests(unittest.IsolatedAsyncioTestCase):
    SCOPE = {"business_unit": "US001", "ledger": "ACTUALS"}

    async def _turn(self, answer_text, payload, question="what is the rate?"):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[ToolCall(id="a", name="get_ar_aging",
                                             args={})]),
            LLMResponse(text=answer_text),
        ])
        session = FakeSession({"get_ar_aging": payload})
        with patch("pstb.client.chat.MAX_NUDGES", 0):
            return await agent_turn(provider, session, question,
                                    surface="gui", scope=self.SCOPE)

    async def test_an_unsourced_rate_is_caveated_not_withheld(self) -> None:
        answer = await self._turn(
            "Open AR is healthy. The standard GST rate is 18%.",
            {"scope_status": "ok", "totals": {"total": 100.0}})
        self.assertIn("Open AR is healthy", answer,
                      "the answer was withheld — rates must only ever be "
                      "caveated")
        self.assertNotIn("I withheld", answer)
        self.assertIn("18%", answer)
        self.assertIn("did not come from any tool result", answer)

    async def test_a_declared_rate_passes_clean(self) -> None:
        answer = await self._turn(
            "The current bucket is 42.5% of open AR.",
            {"scope_status": "ok", "totals": {"total": 100.0},
             "share_pct": 42.5})
        self.assertNotIn("did not come from", answer)
        self.assertNotIn("you gave me", answer)

    async def test_the_users_own_rate_is_acknowledged_not_scolded(self) -> None:
        answer = await self._turn(
            "25% would be unusual for GST.",
            {"scope_status": "ok", "totals": {"total": 100.0}},
            question="our GST works out to 25% — is that right?")
        self.assertIn("you gave me", answer)
        self.assertNotIn("did not come from any tool result", answer)


if __name__ == "__main__":
    unittest.main()


class ToolsDeclareTheirPercentagesTests(unittest.TestCase):
    """The guard is only quiet on legitimate answers if the tools DECLARE
    the percentages users ask for. Adversarial review measured that only 2
    of 7 curated tools did, which would have caveated correct answers —
    the noise failure that trains people to skip every warning."""

    def test_aging_declares_its_bucket_shares(self) -> None:
        import pstb.server as srv
        out = srv.get_ar_aging(as_of_date=SAMPLE_AS_OF)
        self.assertIsNotNone(out.get("overdue_pct"))
        self.assertTrue(out.get("bucket_share_pct"))
        self.assertAlmostEqual(
            sum(out["bucket_share_pct"].values()), 100.0, places=0,
            msg="bucket shares must cover the population")

    def test_a_real_aging_percentage_answer_is_not_caveated(self) -> None:
        import pstb.server as srv
        out = srv.get_ar_aging(as_of_date=SAMPLE_AS_OF)
        payloads = [json.dumps(out, default=str)]
        answer = f"{out['overdue_pct']}% of open AR is overdue."
        self.assertEqual(rate_caveat(rate_findings(answer, payloads)), "",
                         "a percentage the tool itself computed was flagged "
                         "as unverified")
        self.assertIn("did not come from",
                      rate_caveat(rate_findings(
                          "The standard GST rate is 18%.", payloads)),
                      "the fabrication must still be caught alongside it")


class NoFalsePositivesOnRealPayloadsTests(unittest.TestCase):
    """The question that matters in practice is not "does it catch the
    fabrication" but "does it stay quiet on correct answers". A sweep over
    REAL tool output caught three false positives that reading the code did
    not — including one introduced by the same PR. This pins the sweep."""

    def test_percentages_the_tools_declare_are_never_flagged(self) -> None:
        import pstb.server as srv
        aging = srv.get_ar_aging(as_of_date=SAMPLE_AS_OF)
        top = srv.get_top_billing_customers(display_currency="USD")
        proj = srv.get_project_costs()
        cases = [
            (aging, f"{aging['overdue_pct']}% of open AR is overdue."),
            (aging, "The current bucket is "
                    f"{aging['bucket_share_pct']['current']}% of the total."),
            (aging, f"About {round(aging['overdue_pct'])}% is past due."),
            (aging, "I am 100% confident in this reconciliation."),
            (top, "The largest customer is "
                  f"{top['customers'][0]['share_pct']}% of billings."),
            (proj, "Budget utilisation is "
                   f"{proj['projects'][0]['pct_used']}%."),
        ]
        for payload, sentence in cases:
            self.assertEqual(
                rate_caveat(rate_findings(sentence,
                                          [json.dumps(payload, default=str)])),
                "", f"false caveat on a correct answer: {sentence!r}")

    def test_the_fabrication_is_still_caught_beside_them(self) -> None:
        import pstb.server as srv
        for payload in (srv.get_ar_aging(as_of_date=SAMPLE_AS_OF),
                        srv.get_exchange_rate(from_currency="USD",
                                              to_currency="EUR")):
            self.assertIn("did not come from", rate_caveat(rate_findings(
                "The standard GST rate is 18%.",
                [json.dumps(payload, default=str)])))
