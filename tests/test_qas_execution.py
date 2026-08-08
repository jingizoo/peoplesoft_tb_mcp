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


class FailureRemedyTests(unittest.TestCase):
    """A failed API call must leave the operator knowing what to check and
    the USER still able to get an answer. 'HTTP 404' satisfies neither."""

    def _fail(self, status, **kw):
        q = QasConnector("https://ps.example/PSIGW/RESTListeningConnector",
                         user="QASUSER", password="s3cret",
                         transport=lambda m, u, h, b=None, timeout=30:
                             (status, ""), **kw)
        with self.assertRaises(ConnectorError) as ctx:
            q.execute("AP_AGING_BY_VENDOR")
        return str(ctx.exception)

    def test_401_names_the_credentials_this_connector_actually_reads(self):
        msg = self._fail(401)
        self.assertIn("PSFT_QAS_USER", msg)
        self.assertIn("PSFT_QAS_PASSWORD", msg)
        self.assertNotIn("client id", msg,
                         "the inherited message sends an operator hunting "
                         "for settings this connector never reads")
        self.assertIn("permission list", msg,
                       "right password + wrong permission list is the "
                       "second-likeliest cause and must be named")

    def test_404_names_all_three_usual_causes(self):
        msg = self._fail(404)
        self.assertIn("PSFT_QAS_NODE", msg)
        self.assertIn("REST listening connector", msg)
        self.assertIn("search_ps_queries", msg)

    def test_service_down_is_distinguished_from_a_bad_request(self):
        self.assertIn("unavailable", self._fail(503))

    def test_every_failure_says_the_curated_tools_still_work(self):
        for status in (401, 404, 503):
            self.assertIn("curated database tools", self._fail(status),
                          f"HTTP {status} must not read as 'the product is "
                          "down' — the database path is independent")

    def test_a_credential_never_leaks_into_a_failure_message(self):
        for status in (401, 404, 500):
            self.assertNotIn("s3cret", self._fail(status))

    def test_the_configured_timeout_reaches_the_socket(self):
        seen = {}

        def spy(m, u, h, b=None, timeout=30):
            seen["timeout"] = timeout
            return (200, '{"data": {"rows": []}}')

        QasConnector("https://x", user="u", timeout_seconds=120,
                     transport=spy).execute("Q")
        self.assertEqual(seen["timeout"], 120,
                         "a configured timeout that never reaches the "
                         "socket is a lie discovered only under load")

    def test_unreachable_host_names_what_to_check(self):
        def refused(m, u, h, b=None, timeout=30):
            raise ConnectionRefusedError("refused")

        q = QasConnector("https://ps.example/PSIGW/RESTListeningConnector",
                         user="u", transport=refused)
        with self.assertRaises(ConnectorError) as ctx:
            q.execute("AP_AGING_BY_VENDOR")
        msg = str(ctx.exception)
        self.assertIn("unreachable", msg)
        self.assertIn("network access", msg)


class DegradedModeTests(unittest.TestCase):
    """The REST service is the OPTIONAL path. When it is down the product
    must degrade to the curated database tools, not fail the turn."""

    def test_a_connector_failure_reaches_the_caller_as_a_remedy(self) -> None:
        # Not as "ConnectorError: ...". The prefix reads as a crash and
        # buries the sentence that tells the operator what to fix.
        from pstb import server

        def dead(*a, **kw):
            raise ConnectorError("The Query Access Service is unavailable "
                                 "(HTTP 503).")

        out = server._safe(dead)
        self.assertEqual(out["error"],
                         "The Query Access Service is unavailable (HTTP 503).")
        self.assertNotIn("ConnectorError", out["error"])

    def test_the_database_path_does_not_share_the_rest_dependency(self) -> None:
        # Structural, not behavioural: if a curated tool ever imported the
        # QAS connector, a dead gateway would take the whole product down.
        import inspect

        from pstb import engine
        for mod in (engine, ):
            src = inspect.getsource(mod)
            self.assertNotIn("psquery_api", src,
                             f"{mod.__name__} must not depend on the REST "
                             "connector — the database answer has to survive "
                             "a dead gateway")


if __name__ == "__main__":
    unittest.main()
