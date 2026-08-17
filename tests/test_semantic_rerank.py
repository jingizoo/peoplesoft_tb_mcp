from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from pstb.config import Config
from pstb.rerank import HybridReranker, VertexTextEmbedder, metadata_document


class FakeEmbedder:
    name = "fake"
    model = "meaning-v1"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []

    def embed(self, texts, *, task_type):
        self.calls.append((list(texts), task_type))
        if self.fail:
            raise RuntimeError("offline")
        vectors = []
        for text in texts:
            lower = text.lower()
            vectors.append([1.0, 0.0] if "approval" in lower
                           or "which queue" in lower else [0.0, 1.0])
        return vectors


class PartialBadEmbedder(FakeEmbedder):
    def embed(self, texts, *, task_type):
        self.calls.append((list(texts), task_type))
        if task_type == "RETRIEVAL_QUERY":
            return [[1.0, 0.0]]
        return [[1.0, 0.0], [1.0]]


def _matches():
    return [
        {
            "object_id": "default:PS_BI_HDR", "source": "default",
            "kind": "table", "physical_object": "PS_BI_HDR",
            "logical_records": ["BI_HDR"], "label": "Billing header",
            "relevance": 100,
            "confidence": {"tier": "confirmed", "basis": "catalog"},
        },
        {
            "object_id": "default:CORP_AR_QUEUE", "source": "default",
            "kind": "table", "physical_object": "CORP_AR_QUEUE",
            "logical_records": ["Z_AR_QUEUE"], "label": "Receivable queue",
            "relevance": 60,
            "confidence": {"tier": "corroborated", "basis": "SQLTABLENAME"},
            "matched_metadata": [{
                "kind": "field", "name": "X_APPR_STAT",
                "label": "Approval Status", "facets": ["field label"],
            }],
        },
    ]


class HybridRerankerTests(unittest.TestCase):
    def test_semantics_may_reorder_but_not_change_candidates_or_confidence(self):
        embedder = FakeEmbedder()
        before = _matches()
        got = HybridReranker(embedder, enabled=True,
                             semantic_weight=0.8).rerank(
                                 "which queue has approval status", before)
        self.assertTrue(got["applied"])
        self.assertEqual([m["object_id"] for m in got["matches"]], [
            "default:CORP_AR_QUEUE", "default:PS_BI_HDR"])
        self.assertEqual(
            got["matches"][0]["confidence"], before[1]["confidence"])
        self.assertCountEqual(
            [m["object_id"] for m in got["matches"]],
            [m["object_id"] for m in before])
        self.assertIn("only reordered", got["boundary"])

    def test_disabled_path_is_exact_original_order_and_never_calls_provider(self):
        embedder = FakeEmbedder()
        got = HybridReranker(embedder, enabled=False).rerank("anything", _matches())
        self.assertEqual(got["status"], "disabled")
        self.assertEqual([m["object_id"] for m in got["matches"]], [
            "default:PS_BI_HDR", "default:CORP_AR_QUEUE"])
        self.assertEqual(embedder.calls, [])

    def test_provider_failure_preserves_deterministic_order(self):
        got = HybridReranker(FakeEmbedder(fail=True), enabled=True).rerank(
            "which queue", _matches())
        self.assertEqual(got["status"], "unavailable")
        self.assertFalse(got["applied"])
        self.assertEqual([m["object_id"] for m in got["matches"]], [
            "default:PS_BI_HDR", "default:CORP_AR_QUEUE"])

    def test_partial_scoring_failure_returns_pristine_fallback(self):
        got = HybridReranker(
            PartialBadEmbedder(), enabled=True).rerank("which queue", _matches())
        self.assertEqual(got["status"], "unavailable")
        self.assertFalse(got["applied"])
        self.assertEqual([m["object_id"] for m in got["matches"]], [
            "default:PS_BI_HDR", "default:CORP_AR_QUEUE"])
        self.assertTrue(all("semantic_rerank" not in row
                            for row in got["matches"]))

    def test_embedding_text_is_allow_listed_and_cannot_leak_attached_rows(self):
        match = _matches()[1]
        match["sample_rows"] = [{"CUSTOMER_NAME": "SECRET PERSON"}]
        match["attributes"] = {"tax_id": "999-99-9999"}
        match["logical_records"].append({
            "sample_rows": [{"CUSTOMER_NAME": "NESTED SECRET"}]})
        match["matched_metadata"][0]["facets"].append({
            "amount": 9876543.21})
        text = metadata_document(match)
        self.assertIn("CORP_AR_QUEUE", text)
        self.assertIn("Approval Status", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("999-99", text)
        self.assertNotIn("9876543", text)

    def test_config_is_off_by_default_and_bounds_weight_and_candidates(self):
        cfg = Config.sample(".")
        self.assertFalse(HybridReranker.from_config(cfg).enabled)
        cfg.semantic_retrieval.semantic_weight = 9
        cfg.semantic_retrieval.candidate_limit = 999
        got = HybridReranker.from_config(cfg)
        self.assertEqual(got.semantic_weight, 0.8)
        self.assertEqual(got.candidate_limit, 50)

    def test_only_literal_boolean_true_enables_cloud_egress(self):
        cfg = Config.sample(".")
        cfg.semantic_retrieval.enabled = "false"
        got = HybridReranker.from_config(cfg)
        self.assertFalse(got.enabled)
        self.assertIsNone(got.embedder)

    def test_vertex_client_receives_bounded_timeout_in_milliseconds(self):
        calls = {}
        google = types.ModuleType("google")
        genai = types.ModuleType("google.genai")

        class FakeHttpOptions:
            def __init__(self, *, timeout):
                self.timeout = timeout

        class FakeClient:
            def __init__(self, **kwargs):
                calls.update(kwargs)

            def close(self):
                pass

        genai.types = types.SimpleNamespace(HttpOptions=FakeHttpOptions)
        genai.Client = FakeClient
        google.genai = genai
        with mock.patch.dict(sys.modules, {
                "google": google, "google.genai": genai}):
            embedder = VertexTextEmbedder(
                project="prod-project", timeout_seconds=999)
            embedder._start()
        self.assertEqual(embedder.timeout_seconds, 60)
        self.assertEqual(calls["http_options"].timeout, 60_000)

    def test_enabled_without_project_degrades_at_query_time_not_server_start(self):
        cfg = Config.sample(".")
        cfg.semantic_retrieval.enabled = True
        cfg.llm.gemini_project = ""
        reranker = HybridReranker.from_config(cfg)
        self.assertTrue(reranker.enabled)
        got = reranker.rerank("approval queue", _matches())
        self.assertEqual(got["status"], "unavailable")
        self.assertIn("GOOGLE_CLOUD_PROJECT", got["detail"])


if __name__ == "__main__":
    unittest.main()
