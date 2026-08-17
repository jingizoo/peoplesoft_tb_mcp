"""Optional semantic re-ranking behind deterministic metadata retrieval.

The important boundary is architectural, not mathematical: the metadata
catalog chooses a bounded candidate set using names, labels and declared
relationships.  This module may only reorder that set.  It cannot add an
object, change the catalog's confidence, or turn structural metadata into
financial evidence.

Vertex is optional and lazy.  A normal server import performs no cloud call;
when the feature is disabled (the default), the original ranking is returned
byte-for-byte.  If the provider is unavailable or errors, retrieval fails open
to the deterministic ranking and discloses why semantic ordering was skipped.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol, Sequence


MAX_CANDIDATES = 50
MAX_TEXT_CHARS = 1_500


class Embedder(Protocol):
    """Small provider boundary so ranking tests never need cloud access."""

    name: str
    model: str

    def embed(self, texts: Sequence[str], *, task_type: str) -> list[list[float]]:
        ...


class VertexTextEmbedder:
    """Vertex AI text embeddings through the same google-genai dependency.

    ``gemini-embedding-001`` is called one input at a time.  This is slightly
    slower than batching, but it follows the model's strictest documented
    request shape and candidate counts are deliberately small.
    """

    name = "vertex"

    def __init__(self, *, project: str, location: str = "global",
                 model: str = "gemini-embedding-001",
                 output_dimensionality: int = 768,
                 timeout_seconds: int = 15):
        if not (project or "").strip():
            raise RuntimeError(
                "semantic retrieval on Vertex needs GOOGLE_CLOUD_PROJECT")
        self.project = project.strip()
        self.location = (location or "global").strip()
        self.model = (model or "gemini-embedding-001").strip()
        self.output_dimensionality = min(
            max(int(output_dimensionality or 768), 32), 3072)
        self.timeout_seconds = min(
            max(int(timeout_seconds or 15), 1), 60)
        self._client: Any = None
        self._types: Any = None

    def _start(self) -> None:
        if self._client is not None:
            return
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError(
                "semantic retrieval needs google-genai; install the llm extra"
            ) from exc
        self._types = types
        self._client = genai.Client(
            vertexai=True, project=self.project, location=self.location,
            http_options=types.HttpOptions(
                timeout=self.timeout_seconds * 1000))

    def embed(self, texts: Sequence[str], *, task_type: str) -> list[list[float]]:
        self._start()
        vectors: list[list[float]] = []
        for text in texts:
            response = self._client.models.embed_content(
                model=self.model,
                contents=str(text)[:MAX_TEXT_CHARS],
                config=self._types.EmbedContentConfig(
                    task_type=task_type,
                    output_dimensionality=self.output_dimensionality,
                ),
            )
            embeddings = getattr(response, "embeddings", None) or []
            values = (getattr(embeddings[0], "values", None)
                      if embeddings else None)
            if not values:
                raise RuntimeError("Vertex returned no embedding values")
            vectors.append([float(value) for value in values])
        return vectors

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        raise ValueError("embedding dimensions do not match")
    dot = sum(a * b for a, b in zip(left, right))
    ln = math.sqrt(sum(value * value for value in left))
    rn = math.sqrt(sum(value * value for value in right))
    if not ln or not rn:
        return 0.0
    return dot / (ln * rn)


def metadata_document(match: dict) -> str:
    """Build a bounded STRUCTURE-only text representation.

    Deliberately allow-list keys.  A caller accidentally attaching samples,
    amounts, party details or arbitrary attributes to a match cannot send
    those values to an embedding provider.
    """
    parts: list[str] = []

    def add(label: str, value: Any) -> None:
        if value is None or value == "":
            return
        if isinstance(value, (list, tuple, set)):
            # Never stringify nested structures.  A future metadata payload
            # may accidentally attach samples or arbitrary dictionaries under
            # an otherwise allow-listed key; those values must not cross the
            # cloud-provider boundary.
            clean = [item[:160] for item in value
                     if isinstance(item, str) and item]
            if clean:
                parts.append(f"{label}: " + ", ".join(clean[:20]))
        elif isinstance(value, str):
            parts.append(f"{label}: {value[:300]}")

    add("source", match.get("source"))
    add("schema", match.get("schema"))
    add("kind", match.get("kind"))
    add("physical object", match.get("physical_object") or match.get("name"))
    add("logical records", match.get("logical_records"))
    add("label", match.get("label"))
    add("match reasons", match.get("match_reasons"))
    for item in (match.get("matched_metadata") or [])[:10]:
        if not isinstance(item, dict):
            continue
        add("metadata kind", item.get("kind"))
        add("metadata name", item.get("name"))
        add("metadata label", item.get("label"))
        add("metadata facets", item.get("facets"))
    return "\n".join(parts)[:MAX_TEXT_CHARS]


@dataclass
class HybridReranker:
    embedder: Embedder | None
    enabled: bool = False
    semantic_weight: float = 0.35
    candidate_limit: int = 20

    @classmethod
    def from_config(cls, cfg) -> "HybridReranker":
        section = getattr(cfg, "semantic_retrieval", None)
        # This is an explicit data-egress control.  YAML strings such as
        # "false" are truthy in Python but must never enable a cloud call.
        enabled = getattr(section, "enabled", False) is True
        weight = min(max(float(getattr(
            section, "semantic_weight", 0.35) or 0.35), 0.0), 0.8)
        limit = min(max(int(getattr(
            section, "candidate_limit", 20) or 20), 1), MAX_CANDIDATES)
        if not enabled:
            return cls(None, enabled=False, semantic_weight=weight,
                       candidate_limit=limit)
        provider = str(getattr(section, "provider", "vertex") or "vertex")
        if provider.lower() != "vertex":
            # Keep provider validation at request time through the disclosed
            # fallback path; a typo must not prevent the finance server from
            # starting.
            return cls(_UnavailableEmbedder(
                provider, f"unsupported semantic provider {provider!r}"),
                enabled=True, semantic_weight=weight, candidate_limit=limit)
        project = str(getattr(cfg.llm, "gemini_project", "") or "").strip()
        if not project:
            embedder = _UnavailableEmbedder(
                "vertex", "GOOGLE_CLOUD_PROJECT is not configured")
        else:
            embedder = VertexTextEmbedder(
                project=project,
                location=getattr(section, "location", "global"),
                model=getattr(section, "model", "gemini-embedding-001"),
                output_dimensionality=getattr(
                    section, "output_dimensionality", 768),
                timeout_seconds=getattr(section, "timeout_seconds", 15),
            )
        return cls(embedder, enabled=True, semantic_weight=weight,
                   candidate_limit=limit)

    def rerank(self, query: str, matches: Sequence[dict]) -> dict:
        """Return candidates in hybrid order plus an honest status payload."""
        original = [dict(item) for item in matches]
        if not self.enabled or not self.embedder or len(original) < 2:
            return {
                "matches": original,
                "applied": False,
                "status": "disabled" if not self.enabled else "not_needed",
            }
        considered = min(len(original), self.candidate_limit, MAX_CANDIDATES)
        head, tail = original[:considered], original[considered:]
        documents = [metadata_document(item) for item in head]
        try:
            query_vector = self.embedder.embed(
                [str(query)[:MAX_TEXT_CHARS]], task_type="RETRIEVAL_QUERY")[0]
            document_vectors = self.embedder.embed(
                documents, task_type="RETRIEVAL_DOCUMENT")
            if len(document_vectors) != len(head):
                raise RuntimeError("embedding provider returned the wrong count")
            lexical = [float(item.get("relevance") or 0.0) for item in head]
            lo, hi = min(lexical), max(lexical)
            span = hi - lo
            scores = []
            for position, (item, vector, lexical_score) in enumerate(
                    zip(head, document_vectors, lexical)):
                semantic = max(min(_cosine(query_vector, vector), 1.0), -1.0)
                semantic_unit = (semantic + 1.0) / 2.0
                lexical_unit = ((lexical_score - lo) / span if span else 1.0)
                hybrid = ((1.0 - self.semantic_weight) * lexical_unit
                          + self.semantic_weight * semantic_unit)
                annotation = {
                    "semantic_similarity": round(semantic, 6),
                    "hybrid_score": round(hybrid, 6),
                    "original_position": position + 1,
                }
                # Validate every vector before mutating any candidate.  If a
                # later vector is malformed, fallback remains pristine.
                scores.append((hybrid, -position, item, annotation))
            scored = []
            for hybrid, negative_position, item, annotation in scores:
                ranked = dict(item)
                ranked["semantic_rerank"] = annotation
                scored.append((hybrid, negative_position, ranked))
            scored.sort(key=lambda row: (-row[0], -row[1]))
            return {
                "matches": [row[2] for row in scored] + tail,
                "applied": True,
                "status": "applied",
                "provider": self.embedder.name,
                "model": self.embedder.model,
                "candidate_count": considered,
                "weight": self.semantic_weight,
                "boundary": (
                    "Semantic scoring only reordered candidates selected by "
                    "the deterministic metadata catalog; confidence and "
                    "financial-evidence status are unchanged."),
            }
        except Exception as exc:
            return {
                "matches": original,
                "applied": False,
                "status": "unavailable",
                "detail": f"{type(exc).__name__}: {str(exc)[:240]}",
                "boundary": (
                    "Deterministic metadata order was preserved because "
                    "semantic re-ranking was unavailable."),
            }


class _UnavailableEmbedder:
    def __init__(self, name: str, detail: str):
        self.name = name
        self.model = ""
        self.detail = detail

    def embed(self, texts: Sequence[str], *, task_type: str) -> list[list[float]]:
        raise RuntimeError(self.detail)
