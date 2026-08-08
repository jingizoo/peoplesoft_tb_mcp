"""Executing an existing PSQuery through the Query Access Service.

The safety properties are structural, and these tests exist to prove
they stay that way: the transport physically cannot POST (so a
write-capable IB service is unreachable, not merely disallowed), the
operation is the one the discovery module classifies as read-only, and
the payload always says whose permission lists shaped the rows.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config  # noqa: E402
from pstb.connectors import ConnectorError, FIXTURE_DIR, FixtureTransport  # noqa: E402
from pstb.connectors.psquery_api import (EXECUTE_OPERATION,  # noqa: E402
                                         QasConnector, from_config,
                                         rest_base)
from pstb.psquery import READ_ONLY_OPERATIONS  # noqa: E402


def _qas(**kw):
    return QasConnector(
        transport=FixtureTransport(Path(FIXTURE_DIR) / "psquery.json"), **kw)


class ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.q = _qas()

    def test_it_runs_a_named_query_and_returns_rows(self) -> None:
        out = self.q.execute("AP_AGING_BY_VENDOR",
                             {"BIND1": "US001", "BIND2": "2026-08-06"})
        self.assertEqual(out["row_count"], 3)
        self.assertIn("VOUCHER_ID", out["columns"])
        self.assertEqual(out["prompts_supplied"]["BIND1"], "US001")

    def test_prompts_become_query_parameters(self) -> None:
        self.q.execute("AP_AGING_BY_VENDOR", {"BIND1": "US001"})
        called = " ".join(self.q.transport.calls)
        self.assertIn("QAS_EXECUTEQUERY.v1/PUBLIC/AP_AGING_BY_VENDOR",
                      called)

    def test_a_blank_name_refuses_toward_discovery(self) -> None:
        with self.assertRaises(ConnectorError) as ctx:
            self.q.execute("")
        self.assertIn("search_ps_queries", str(ctx.exception))

    def test_an_unrecognised_response_shape_refuses_clearly(self) -> None:
        with self.assertRaises(ConnectorError) as ctx:
            self.q.execute("UNKNOWN_SHAPE")
        self.assertIn("does not recognise", str(ctx.exception))
        self.assertIn("REST listening connector", str(ctx.exception))

    def test_the_row_cap_is_bounded_by_config_not_the_caller(self) -> None:
        q = _qas(max_rows=2)
        out = q.execute("AP_AGING_BY_VENDOR", max_rows=10_000)
        self.assertEqual(out["row_count"], 2)
        self.assertTrue(out["truncated"])
        self.assertIn("narrow the prompts", out["note"])


class SafetyTests(unittest.TestCase):
    def test_the_transport_cannot_post_at_all(self) -> None:
        # This is what makes a voucher-build service unreachable rather
        # than merely disallowed.
        q = _qas()
        with self.assertRaises(ConnectorError) as ctx:
            q._request("POST", "https://x.example/PSIGW/anything")
        self.assertIn("read-only", str(ctx.exception))

    def test_the_operation_is_the_shared_read_only_one(self) -> None:
        self.assertIn(EXECUTE_OPERATION, READ_ONLY_OPERATIONS,
                      "execution and discovery must classify the same "
                      "operation the same way, or they will drift")

    def test_security_divergence_is_always_disclosed(self) -> None:
        out = _qas().execute("AP_AGING_BY_VENDOR")
        self.assertIn("PERMISSION LISTS", out["note"])
        self.assertIn("bypasses row-level security", out["note"])
        self.assertIn("executed_as", out)

    def test_fixture_mode_is_disclosed(self) -> None:
        out = _qas().execute("AP_AGING_BY_VENDOR")
        self.assertEqual(out["mode"], "fixtures")
        self.assertIn("SAMPLE", out["note"])

    def test_credentials_never_appear_in_the_payload(self) -> None:
        import json
        q = QasConnector(
            "https://x.example", user="QASUSER", password="s3cr3t-value",
            transport=FixtureTransport(Path(FIXTURE_DIR) / "psquery.json"))
        blob = json.dumps(q.execute("AP_AGING_BY_VENDOR"))
        self.assertNotIn("s3cr3t-value", blob)
        self.assertIn("QASUSER", blob, "the USER is disclosed on purpose — "
                                       "it explains the row set")


class GatewayDiscoveryTests(unittest.TestCase):
    def test_the_rest_base_derives_from_the_sites_own_target(self) -> None:
        self.assertEqual(
            rest_base("http://host:8016/PSIGW/"
                      "PeopleSoftServiceListeningConnector"),
            "http://host:8016/PSIGW/RESTListeningConnector")

    def test_an_unknown_shape_is_passed_through_untouched(self) -> None:
        self.assertEqual(rest_base("http://host/custom"),
                         "http://host/custom")
        self.assertEqual(rest_base(""), "")

    def test_disabled_config_falls_back_to_fixtures(self) -> None:
        cfg = Config.sample(ROOT)
        self.assertFalse(cfg.ps_api.enabled)
        self.assertEqual(from_config(cfg, "http://host/PSIGW/x").mode,
                         "fixtures",
                         "execution must stay off until explicitly enabled")


if __name__ == "__main__":
    unittest.main()
