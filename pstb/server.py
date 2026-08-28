"""MCP server exposing PeopleSoft trial-balance tools over stdio.

Run:  python -m pstb.server          (config from $PSTB_CONFIG or ./config.yaml)

Any MCP-compatible client can use this server (this repo's chat client, an IDE
or desktop MCP host, the MCP Inspector). Never print to stdout here — stdio
carries the protocol; diagnostics go to stderr.
"""
from __future__ import annotations

import os
import shlex
import sys

try:  # mcp SDK >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP

from typing import Optional

from .config import load_config
from .db import Database, DbError
from .engine import EngineError, TBEngine
from .journal_controls import JournalControlError, JournalStatusControl
from .grni import GRNIControl
from .ar import ARBilling, ARError
from .anomalies import AnomalyDetector, AnomalyError
from .metadata import (MetadataCatalog, MetadataError,
                       source_catalog_path as _metadata_path,
                       source_fingerprint as _metadata_fingerprint)
from .memory import MemoryError_, SiteMemory
from .modules import ModuleError, ModulePacks
from .playbooks import PlaybookError, PlaybookRunner
from .rerank import HybridReranker
from .relationships import Relationships
from .source_knowledge import (
    SourceExclusionIndex,
    SourceKnowledge,
    SourceKnowledgeError,
    normalize_aliases,
    source_knowledge_path as _source_knowledge_path,
    validate_catalog_aliases,
)
from .vendors import VendorNetwork
from .report import ReportError, ReportRunner
from . import wiki as wiki_mod
from .wiki import WikiError, make_wiki

cfg = load_config(os.environ.get("PSTB_CONFIG"))
db = Database(cfg)
engine = TBEngine(db, cfg)
from .sources import SourceRegistry
engine.registry = SourceRegistry(cfg, db)
journal_status = JournalStatusControl(engine)
anomaly_detector = AnomalyDetector(db, cfg)
report_runner = ReportRunner(engine)
ar = ARBilling(engine)
relationships = Relationships(ar)
from .connectors import ConnectorError
from .connectors import coupa as _coupa_mod
coupa = _coupa_mod.from_env(cfg=cfg)
playbooks = PlaybookRunner(engine, ar, modules=None, coupa=coupa)
modules = ModulePacks(engine)
grni = GRNIControl(modules)
vendor_network = VendorNetwork(modules)
from .procurement import Procurement
procurement = Procurement(modules)
memory = SiteMemory(cfg.resolve_path(
    getattr(cfg.tools, 'site_memory', 'site_memory.json')))
record_exclusions = SourceExclusionIndex(cfg)
from .entitygraph import EntityGraph, graph_path as _eg_path
entity_graph = EntityGraph(_eg_path(cfg))
from .procgraph import ProcessGraph, graph_path
# Opened per call against a local file, so a graph built or rebuilt after
# this process started is picked up without a restart — the build script is
# run by an administrator, not by the server.
process_graph = ProcessGraph(graph_path(cfg))
_default_metadata_fingerprint = ""
_default_metadata_binding_error = ""
if cfg.sources:
    try:
        _default_metadata_fingerprint = _metadata_fingerprint(cfg, "default")
    except MetadataError as exc:
        # Endpoint indirection that cannot be bound safely (for example a
        # missing tnsnames.ora or a DSN without an explicit server/database)
        # disables this derived artifact.  It must not stop unrelated MCP
        # tools or other independently-bound source artifacts from starting.
        _default_metadata_binding_error = str(exc)
metadata_catalog = MetadataCatalog(
    _metadata_path(cfg, "default"),
    stale_after_hours=getattr(cfg.metadata_catalog, "stale_after_hours", 168),
    source="default",
    # A historical one-database deployment may already have a v2 catalog
    # without an endpoint binding. Keep it readable until the next rebuild.
    # Multi-source deployments fail closed because alias reuse can cross a
    # real data boundary.
    expected_fingerprint=_default_metadata_fingerprint,
    binding_error=_default_metadata_binding_error)
# Additional readers are tiny immutable bindings over local paths and open a
# read-only SQLite connection per call. Keep the long-standing singular name
# above as the primary alias for compatibility with extensions/tests, while
# every secondary source gets a physically distinct artifact.
_metadata_catalogs: dict[str, MetadataCatalog] = {}
metadata_reranker = HybridReranker.from_config(cfg)
from .psquery import QueryCatalog
query_catalog = QueryCatalog(engine)
from .connectors import psquery_api as _qas_mod


class _LazyQas:
    """Resolve the QAS gateway on FIRST USE, never at import.

    This used to call query_catalog.integration_endpoints() in module scope,
    which put an Oracle round trip on the import path: every consumer of
    pstb.server — the MCP server, the GUI, the probe, the eval harness —
    paid it before doing anything, and a failure there took the whole
    process down rather than one tool. On a real instance it did: the IB
    catalog's gateway column is not named the same across tools releases,
    the query raised ORA-00904, and the server never finished starting.

    Discovery is also worthless to the 69 tools that never touch QAS.
    """

    def __init__(self):
        self._real = None

    def _resolve(self):
        if self._real is None:
            target = ""
            try:
                target = ((query_catalog.integration_endpoints() or {})
                          .get("target_location") or "")
            except Exception as e:  # discovery is optional, never fatal
                print(f"[pstb] IB gateway discovery skipped: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
            self._real = _qas_mod.from_config(cfg, target)
        return self._real

    def __getattr__(self, name):
        return getattr(self._resolve(), name)


qas = _LazyQas()
try:
    wiki = make_wiki(cfg)
except WikiError as e:
    print(f"[pstb] wiki disabled: {e}", file=sys.stderr)
    wiki = None
# Let the engine resolve policy figures into query binds. Approved site memory
# outranks the wiki, so both are handed over together.
engine.attach_policy(wiki, memory)
# Record discovery uses what people here have taught about their own tables.
engine.attach_memory(memory)
# A local, mtime-cached index makes an approved "do not use" decision a hard
# guard on discovery, profiling and SQL without adding a database round trip.
engine.attach_record_exclusions(record_exclusions)

mcp = FastMCP("peoplesoft-tb")


def _safe(fn, /, **kw) -> dict:
    try:
        return fn(**kw)
    except (EngineError, DbError, WikiError, ReportError, ARError,
            PlaybookError, MemoryError_, ModuleError, ConnectorError,
            AnomalyError, MetadataError, JournalControlError,
            SourceKnowledgeError) as e:
        # These carry a remedy written for the reader. Passing the message
        # through UNPREFIXED is the point: "ConnectorError: check
        # PSFT_QAS_NODE" reads as a crash, "check PSFT_QAS_NODE" reads as
        # an instruction.
        return {"error": str(e)}
    except Exception as e:  # keep the agent loop alive on unexpected failures
        return {"error": f"{type(e).__name__}: {e}"}


def _metadata_for_source(source: str = "") -> tuple[str, MetadataCatalog]:
    """Canonical source name and its source-bound knowledge artifact.

    Resolution happens before opening SQLite, and the reader independently
    verifies the file contains exactly that source.  These are two separate
    boundaries on purpose: a bad tool argument gets a useful configured-name
    error, while a copied or stale file cannot leak a different database's
    catalog even if routing selected the right name.
    """
    name = engine.registry.resolve_name(source)
    known = engine.registry.names()
    if name not in known:
        raise MetadataError(
            f"Unknown metadata source {source!r}. Available sources: "
            f"{', '.join(known)}")
    if name == "default":
        # Read the public alias each time so controlled test/extension
        # replacement of the primary reader continues to work.
        return name, metadata_catalog
    catalog = _metadata_catalogs.get(name)
    if catalog is None:
        fingerprint = ""
        binding_error = ""
        try:
            fingerprint = _metadata_fingerprint(cfg, name)
        except MetadataError as exc:
            # Keep source-independent tools available while this one source's
            # endpoint binding is incomplete.  Every catalog operation will
            # return the precise fail-closed remedy through binding_error.
            binding_error = str(exc)
        catalog = MetadataCatalog(
            _metadata_path(cfg, name),
            stale_after_hours=getattr(
                cfg.metadata_catalog, "stale_after_hours", 168),
            source=name,
            expected_fingerprint=fingerprint,
            binding_error=binding_error)
        _metadata_catalogs[name] = catalog
    return name, catalog


def _source_knowledge_for_source(source: str) -> SourceKnowledge:
    """One local governance overlay bound to the selected DB endpoint.

    Construction is intentionally cheap and repeated per call.  The reader
    opens its private SQLite file read-only, while a proposal opens a separate
    short transaction.  No mutable connection or annotation set is shared
    across source silos.
    """
    fingerprint = _metadata_fingerprint(cfg, source)
    return SourceKnowledge(
        _source_knowledge_path(cfg, source),
        source=source,
        source_fingerprint=fingerprint,
    )


def _exact_metadata_subject(result: dict, source: str) -> dict:
    """Return one proven physical table/view or refuse the proposal target."""
    if not isinstance(result, dict) or result.get("found") is not True:
        detail = (result or {}).get("detail") if isinstance(result, dict) else ""
        raise SourceKnowledgeError(
            str(detail or "the metadata catalog did not resolve one exact object"))
    subject = result.get("subject")
    if not isinstance(subject, dict):
        raise SourceKnowledgeError(
            "the metadata catalog returned no exact physical object")
    if (str(result.get("source_database") or "") != source
            or str(subject.get("source") or "") != source):
        raise SourceKnowledgeError(
            "the metadata result did not come from the selected source")
    if (str(subject.get("kind") or "").lower() not in {"table", "view"}
            or not subject.get("object_id")
            or not subject.get("schema")
            or not subject.get("physical_object")):
        raise SourceKnowledgeError(
            "metadata learning needs one exact physical table/view; use a "
            "schema-qualified identifier when the name is ambiguous")
    return subject


def _knowledge_annotation(row: dict) -> dict:
    """Bounded approved meaning shown as data, never prompt instructions."""
    return {
        "proposal_id": row.get("id"),
        "status": "approved",
        "meaning": row.get("meaning"),
        "aliases": list(row.get("aliases") or []),
        **({"matched_on": row.get("matched_on"),
            "matched_terms": list(row.get("matched_terms") or [])}
           if row.get("matched_on") else {}),
        "proposed_at": row.get("proposed_at"),
        **({"approved_at": row.get("decided_at")}
           if row.get("decided_at") else {}),
        "authority": "host-operator-approved source knowledge",
        "effect": (
            "object-selection pointer only; structural confidence, rows and "
            "relationships are unchanged"),
    }


def _exclusion_annotation(row: dict) -> dict:
    """Bounded operator veto shown separately from selectable candidates."""
    return {
        "proposal_id": row.get("id"),
        "status": "excluded",
        "selection_effect": "exclude",
        "reason": row.get("meaning"),
        "proposed_at": row.get("proposed_at"),
        **({"approved_at": row.get("decided_at")}
           if row.get("decided_at") else {}),
        "authority": "host-operator-approved record exclusion",
        "effect": (
            "hard veto: do not recommend, profile, compare, or query this "
            "object for answers"),
    }


def _approved_search_candidates(
    catalog: MetadataCatalog, source: str, query: str
) -> tuple[dict[str, list[dict]], list[dict], dict[str, list[dict]], dict]:
    """Approved meaning hits plus exact current-catalog object summaries.

    Annotation prose is deliberately returned separately.  The structural
    summaries may enter the optional semantic candidate set, but approved
    free text is attached only after Vertex scoring so enabling embeddings
    does not create a new annotation-egress path.
    """
    try:
        store = _source_knowledge_for_source(source)
        if not store.path.exists():
            return {}, [], {}, {}
        hits, exclusions = store.search_with_exclusions(query)
    except (MetadataError, SourceKnowledgeError) as exc:
        return {}, [], {}, {
            "available": False,
            "detail": str(exc),
            "effect": "no source annotations were used",
        }
    grouped: dict[str, list[dict]] = {}
    excluded = {}
    subjects: dict[str, dict] = {}
    ignored = 0
    for hit in hits:
        identifier = f"{hit.get('schema')}.{hit.get('object')}"
        context = catalog.context(identifier, source=source, limit=1)
        try:
            subject = _exact_metadata_subject(context, source)
        except SourceKnowledgeError:
            ignored += 1
            continue
        object_id = str(subject["object_id"])
        if object_id != str(hit.get("object_id") or ""):
            ignored += 1
            continue
        try:
            validate_catalog_aliases(
                catalog, source, object_id, hit.get("aliases") or ())
        except SourceKnowledgeError:
            # A newly native identifier outranks an older approved alias.
            # Quarantine the proposal at use time until it is replaced.
            ignored += 1
            continue
        subjects[object_id] = dict(subject)
        grouped.setdefault(object_id, []).append(hit)
    for row in exclusions:
        object_id = str(row.get("object_id") or "")
        if object_id:
            excluded.setdefault(object_id, []).append(row)
    # A veto wins over a positive pointer for the same object. This also
    # handles two operators approving opposite historical proposals without
    # silently depending on proposal order.
    for object_id in excluded:
        grouped.pop(object_id, None)
        subjects.pop(object_id, None)
    info = {
        "available": True,
        "approved_matches": sum(len(rows) for rows in grouped.values()),
        "matched_objects": len(grouped),
        "active_exclusions": len(excluded),
        "ignored_stale_targets": ignored,
        "effect": (
            "approved meanings may select/rank an exact catalog object; "
            "approved exclusions veto an object; structural confidence and "
            "relationship evidence are unchanged"),
    }
    return grouped, list(subjects.values()), excluded, info


def _attach_approved_context(
    result: dict, catalog: MetadataCatalog, store: SourceKnowledge, source: str
) -> dict:
    """Attach approved meanings for the result's exact current object."""
    if not isinstance(result, dict) or result.get("found") is not True:
        return result
    subject = _exact_metadata_subject(result, source)
    active_rules = store.active_rules(str(subject["object_id"]))
    exclusions = [row for row in active_rules if row["status"] == "excluded"]
    if exclusions:
        result["selection_status"] = "excluded_by_operator"
        result["selection_allowed"] = False
        result["operator_exclusions"] = [
            _exclusion_annotation(row) for row in exclusions
        ]
        result["source_knowledge_note"] = (
            "This object may be inspected structurally, but it is excluded "
            "from record profiling, comparison, recommendation, and SQL "
            "answers until an operator revokes the exclusion.")
        return result
    approved = []
    for row in active_rules:
        if row["status"] != "approved":
            continue
        try:
            validate_catalog_aliases(
                catalog, source, str(subject["object_id"]),
                row.get("aliases") or ())
        except SourceKnowledgeError:
            continue
        approved.append(row)
    if approved:
        result["approved_source_meanings"] = [
            _knowledge_annotation(row) for row in approved
        ]
        result["source_knowledge_note"] = (
            "Approved meanings help select this object; they do not change "
            "its catalog confidence or prove a join, row, status, or amount.")
    return result


def _sourced(source: str, method: str, /, **kw) -> dict:
    """Run an ad-hoc tool against a named source, and SAY which one answered.

    Two things this fixes, both discovered on a two-database deployment.

    Provenance. Only search_records named its source, so a table list from a
    reporting mart and one from PeopleSoft were indistinguishable in the
    payload, in the card, and to the model. "Which database is this?" is the
    first question anyone asks of a multi-source answer, and it had no
    answer. The name is the one the REGISTRY resolved, not the alias the
    caller typed, so "peoplesoft" and "" and "default" all report default
    rather than looking like three databases.

    Resolution errors. The call used to read
    ``_safe(engine.for_source(source).list_tables, ...)``, which evaluates
    for_source OUTSIDE _safe — so a mistyped source raised through the
    handler and reached the model as a crash-shaped "TOOL ERROR:" instead of
    the clean, remedied {"error": ...} the message was written to be.
    Resolving inside the try puts it back on the intended path.
    """
    try:
        bound = engine.for_source(source)
        label = (engine.registry.resolve_name(source)
                 if engine.registry is not None else "default")
    except (EngineError, DbError) as e:
        return {"error": str(e)}
    out = _safe(getattr(bound, method), **kw)
    if isinstance(out, dict):
        out.setdefault("source_database", label)
    return out


def _table_health(table: str, source: str) -> dict:
    """The health engine behind get_table_health; see the tool docstring."""
    from . import relmine

    name, catalog = _metadata_for_source(source)
    # limit=60, not the default: the shared context budget also feeds
    # dependencies and mappings, and a well-connected table starved the
    # column list to empty -- which silently skipped every check while
    # looking like a clean bill (review finding).
    context = catalog.context(table, source=name, limit=60)
    if not isinstance(context, dict) or context.get("found") is not True \
            or context.get("ambiguous"):
        return {
            "source_database": name,
            "error": str((context or {}).get("detail")
                         or "the catalog did not resolve one exact object"),
            "candidates": (context or {}).get("candidates") or [],
        }
    subject = context.get("subject") or {}
    schema = str(subject.get("schema") or "")
    physical = str(subject.get("physical_object") or "")
    kind = str(subject.get("kind") or "").lower()
    useful = context.get("usefulness") or {}
    qualified = f"{schema}.{physical}" if schema else physical

    for part in (schema, physical):
        if part and not relmine._SAFE_IDENT.fullmatch(part):
            return {"source_database": name,
                    "error": f"unsafe identifier {part[:40]!r}"}

    # Operator record exclusions bind here exactly as they bind join_path:
    # a health probe reads the record's DATA, which is precisely what an
    # exclusion forbids (review finding).
    excluded_ids = {str(row.get("object_id") or "")
                    for row in record_exclusions.for_source(name)}
    excluded_names = {str(row.get("object") or "").upper()
                      for row in record_exclusions.for_source(name)}
    if (str(subject.get("object_id") or "") in excluded_ids
            or physical.upper() in excluded_names):
        return {"source_database": name,
                "error": (f"{physical} is excluded from this source by an "
                          "operator decision; health checks read its data "
                          "and are refused for the same reason as queries")}

    bound = engine.for_source(source)
    db = bound.db
    dialect = str(getattr(db, "dialect", "")).lower()

    def bounded(inner: str, cap: int) -> str:
        return relmine.bounded_select(dialect, inner, cap)

    caveats: list[str] = []
    estimate = useful.get("row_estimate")
    # FAIL CLOSED on size: the duplicate probe is a whole-table GROUP BY,
    # and an unknown estimate is not permission -- it is the absence of
    # permission. On the target deployment most tables have never been
    # analyzed, so treating "unknown" as "small" would have made the 2M
    # gate decorative (review blocker).
    measured_size = (isinstance(estimate, (int, float))
                     and not isinstance(estimate, bool)
                     and estimate >= 0)
    exact_ok = measured_size and estimate <= 2_000_000
    if not exact_ok:
        caveats.append(
            (f"the profiler estimates {int(estimate):,} rows; "
             if measured_size else
             "the table's size is not established by any statistic; ")
            + "exact duplicate counting was skipped to avoid a full "
            "scan — null rates and orphan checks are sampled either way")

    # ---- columns worth checking: key-shaped first, then declared order
    columns = []
    for column in context.get("columns") or []:
        cname = str(column.get("name") or "")
        if cname and relmine._SAFE_IDENT.fullmatch(cname):
            columns.append(cname)
    key_first = sorted(
        columns, key=lambda c: (not relmine.is_key_shaped(c),
                                columns.index(c)))[:12]

    # ---- null rates over a bounded sample (one query, never a scan)
    null_rates: list[dict] = []
    sampled_rows = 0
    if not key_first:
        caveats.append(
            "the catalog returned no readable columns for this object, so "
            "null rates and duplicate checks were skipped -- not passed")
    if key_first and kind in ("table", "view"):
        selects = ", ".join(
            f"COUNT({c}) AS nn_{i}" for i, c in enumerate(key_first))
        try:
            rows, _ = db.query(
                f"SELECT COUNT(*) AS n, {selects} FROM "
                f"({bounded(f'SELECT * FROM {qualified}', 5000)}) t",
                {}, max_rows=1)
            row = rows[0] if rows else {}
            sampled_rows = int(row.get("n") or 0)
            for i, column in enumerate(key_first):
                non_null = int(row.get(f"nn_{i}") or 0)
                nulls = max(sampled_rows - non_null, 0)
                null_rates.append({
                    "column": column, "nulls": nulls,
                    "null_pct": (round(nulls / sampled_rows, 4)
                                 if sampled_rows else None)})
        except DbError as exc:
            caveats.append(f"null-rate sample failed: {exc}")
    if sampled_rows >= 5000:
        caveats.append(
            f"null rates are measured over {sampled_rows:,} rows the "
            "database happened to return first (no ordering is imposed), "
            "not the full population")
    elif sampled_rows:
        caveats.append(
            f"null rates are measured over all {sampled_rows:,} rows -- "
            "the sample covered the whole table")

    # ---- duplicate candidate keys (exact but bounded; skipped when large)
    duplicates: dict = {"checked": False}
    unique_cols: list[str] = []
    basis = ""
    for index in context.get("indexes") or []:
        if index.get("unique") and index.get("columns"):
            declared = [str(c) for c in index["columns"]]
            if len(declared) > 8:
                # Never truncate a declared key: grouping by a PREFIX of a
                # unique index manufactures duplicate alarms on data the
                # database itself enforces as unique (review blocker --
                # PS_JRNL_LN's five-column key read as thousands of
                # "duplicates"). An unusually wide key is skipped and
                # named instead.
                duplicates = {
                    "checked": False,
                    "key_columns": declared,
                    "basis": f"declared unique index {index.get('name')}",
                    "skipped": (f"the unique key has {len(declared)} "
                                "columns; grouping by fewer would report "
                                "false duplicates"),
                }
                break
            unique_cols = declared
            basis = f"declared unique index {index.get('name')}"
            break
    if not unique_cols and not duplicates.get("skipped"):
        unique_cols = [c for c in key_first
                       if relmine.is_key_shaped(c)][:3]
        basis = "heuristic key-shaped columns"
    unique_cols = [c for c in unique_cols
                   if relmine._SAFE_IDENT.fullmatch(c)]
    if basis.startswith("declared") and len(unique_cols) != len(
            [str(c) for i2 in (context.get("indexes") or [])
             if i2.get("name") == basis.split()[-1]
             for c in (i2.get("columns") or [])]):
        # An expression column failed the identifier gate: the remaining
        # prefix is NOT the key. Skip rather than group by part of it.
        duplicates = {"checked": False, "key_columns": unique_cols,
                      "basis": basis,
                      "skipped": "the unique index contains expression "
                                 "columns; a partial grouping would report "
                                 "false duplicates"}
        unique_cols = []
    if unique_cols and kind == "table" and exact_ok:
        group = ", ".join(unique_cols)
        try:
            rows, _ = db.query(
                "SELECT COUNT(*) AS g FROM ("
                + bounded(
                    f"SELECT 1 AS x FROM {qualified} GROUP BY {group} "
                    "HAVING COUNT(*) > 1", 51)
                + ") d", {}, max_rows=1)
            groups = int((rows[0].get("g") if rows else 0) or 0)
            duplicates = {
                "checked": True, "key_columns": unique_cols,
                "basis": basis,
                "duplicate_groups": ("50+" if groups > 50 else groups)}
        except DbError as exc:
            duplicates = {"checked": False, "key_columns": unique_cols,
                          "basis": basis, "error": str(exc)}

    # ---- referential integrity along known relationships
    relationships: list[dict] = []
    ring = catalog.relationships_of(qualified, source=name)
    for edge in (ring.get("relationships") or []):
        if len(relationships) >= 3:
            break
        if edge.get("direction") != "references":
            continue
        pairs = edge.get("column_pairs") or []
        if not pairs:
            continue
        target = edge.get("next") or {}
        relationship = edge.get("relationship")
        entry = {
            "parent": ".".join(p for p in (target.get("schema"),
                                           target.get("object")) if p),
            "relationship": relationship,
            "confidence": edge.get("confidence"),
        }
        # A multi-pair edge used to be silently skipped -- which was every
        # merged mined edge and every composite declared key, i.e. most of
        # the graph this tool exists to spend (review finding). Mined
        # pairs are independent single-column containments, so the
        # best-measured one is probed and named. A composite declared key
        # is NOT independent per column -- probing one column undercounts
        # real orphans -- so it is listed and skipped with the reason.
        if relationship == "value_overlap":
            measurements = edge.get("measurements") or []
            best = max(measurements,
                       key=lambda m: m.get("overlap_pct") or 0)                 if measurements else None
            probe_pair = ({"left": best.get("column"),
                           "right": best.get("referenced_column")}
                          if best else
                          {"left": pairs[0].get("left_column"),
                           "right": pairs[0].get("right_column")})
            if len(pairs) > 1:
                entry["probed_pair"] = (
                    f"{probe_pair['left']} -> {probe_pair['right']} "
                    f"(best-measured of {len(pairs)} pairs)")
        elif len(pairs) != 1:
            entry["via"] = ", ".join(
                f"{pair.get('left_column')} -> {pair.get('right_column')}"
                for pair in pairs)
            entry["skipped"] = (
                "composite declared key: probing one column of a "
                f"{len(pairs)}-column key would undercount real orphans, "
                "so nothing is claimed")
            relationships.append(entry)
            continue
        else:
            probe_pair = {"left": pairs[0].get("left_column"),
                          "right": pairs[0].get("right_column")}
        left = str(probe_pair["left"] or "")
        right = str(probe_pair["right"] or "")
        entry.setdefault("via", f"{left} -> {right}")
        if relationship == "value_overlap":
            entry["caveat"] = (
                "this relationship is MEASURED from value containment, "
                "not declared; treat orphan counts as evidence to "
                "investigate, not as a broken constraint")
        parent_id = str((target.get("object") or "")).upper()
        if (str(target.get("id") or "") in excluded_ids
                or parent_id in excluded_names):
            entry["skipped"] = (
                "the parent record is excluded from this source by an "
                "operator decision; its data is not probed")
            relationships.append(entry)
            continue
        try:
            probe = relmine.probe_containment(
                db,
                {"schema": schema, "table": physical, "column": left},
                {"schema": target.get("schema"),
                 "table": target.get("object"), "column": right},
                sample_rows=200)
            sampled = int(probe.get("sampled") or 0)
            contained = int(probe.get("contained") or 0)
            # DISTINCT-VALUE vocabulary, deliberately: the probe samples
            # distinct values of the child column, so 7/200 is a share of
            # VALUES, not of rows -- one orphaned value may be one row or
            # ten thousand (review finding).
            entry["sampled_distinct"] = sampled
            entry["orphaned_distinct"] = max(sampled - contained, 0)
            entry["orphan_value_pct"] = (
                round((sampled - contained) / sampled, 4)
                if sampled else None)
            entry["rate_note"] = (
                "rates are over distinct sampled values of the child "
                "column, not rows")
        except DbError as exc:
            entry["error"] = str(exc)
        relationships.append(entry)

    return {
        "source_database": name,
        "object": {"schema": schema, "name": physical, "kind": kind},
        "profile": {
            "liveness": useful.get("liveness"),
            "row_estimate": useful.get("row_estimate"),
            "value_score": useful.get("value_score"),
            "caveat": useful.get("caveat") or "",
        },
        "null_rates": null_rates,
        "sampled_rows": sampled_rows,
        "duplicate_keys": duplicates,
        "relationships": relationships,
        "caveats": caveats,
        "note": (
            "Counts and percentages only — no row values leave the "
            "database. Sampled figures are labeled; they support an "
            "investigation, never a completeness conclusion."),
    }


@mcp.tool()
def get_table_health(table: str, source: str = "") -> dict:
    """Data-quality and referential-integrity evidence for ONE table/view,
    in any workspace. Use for reconciliation- and audit-shaped questions:
    "do the stage rows all reach the header", "are there duplicate
    vouchers", "how complete is this feed table". Returns the profiler's
    liveness/row evidence, sampled null rates for key-shaped columns, a
    bounded duplicate-key check, and orphan counts along KNOWN
    relationships -- declared foreign keys first, then joins MEASURED from
    value containment (labeled, with their overlap evidence; measurement
    is not declaration). Counts and percentages only: no row values are
    returned, sampled figures are labeled sampled, and nothing here is a
    completeness conclusion on its own. table: exact name or schema.name.
    source: which database; required scope rules match describe_table."""
    return _safe(_table_health, table=table, source=source)


@mcp.tool()
def ask_user(question: str, options: str = "") -> dict:
    """END this turn by asking the user ONE question with concrete choices.

    Use ONLY when a required choice is genuinely the user's: a parameter is
    ambiguous and tools in THIS conversation (or the user's own words)
    produced 2-6 concrete candidates -- which business unit of the several
    they can see, which fiscal year, which of the near-duplicate tables.
    options is comma-separated and every option must come from data or the
    user's message, never invented. Never use it to stall, never twice in a
    turn, never when one more discovery call would answer it yourself, and
    the question must contain no financial figures. After calling, STOP:
    the turn ends and the user replies.
    """
    text = " ".join(str(question or "").split())
    if not text:
        raise ValueError("a clarification needs the question itself")
    if len(text) > 300:
        raise ValueError(
            "a clarification question is one sentence, not a briefing; "
            "state the ambiguity in under 300 characters")
    parts = [" ".join(part.split()) for part in
             str(options or "").split(",")]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        raise ValueError(
            "a clarification offers a real choice: give 2-6 comma-separated "
            "options drawn from tool results or the user's own words")
    if len(parts) > 6:
        raise ValueError(
            "more than 6 options is a listing, not a question; narrow it "
            "or show the candidates in a normal answer instead")
    overlong = [part for part in parts if len(part) > 80]
    if overlong:
        raise ValueError(
            f"option {overlong[0][:40]!r}... is longer than 80 characters; "
            "options are labels, not explanations")
    return {
        "clarification": {"question": text, "options": parts},
        "turn_ends": True,
        "note": ("The turn ends here. Do not call further tools and do not "
                 "answer the original question; the user will choose."),
    }


@mcp.tool()
def get_trial_balance(
    business_unit: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    ledger: str = "",
    group_by: str = "",
    dept: str = "",
    account: str = "",
    currency: str = "",
    include_adjustments: bool = False,
    max_rows: int = 0,
    amount_basis: str = "base",
) -> dict:
    """    Trial balance by account: beginning balance, period activity, ending balance, DR/CR.
    Amounts are signed: debits positive, credits negative.

    fiscal_year / period: 0 = current. business_unit / ledger: empty = the
    default. Leave them alone unless the user named one — never invent a year.
    account: exact "1000", range "6000-6999", list "1000,1100", or prefix "60%".
    group_by: extra chartfields, comma-separated (e.g. "DEPTID" or "DEPTID,PROJECT_ID").
    currency: filter to one currency code, or "detail" to break out by currency.
    include_adjustments: fold adjustment-period (998) amounts into ending balances.
    amount_basis: "base" (default) reports POSTED_TOTAL_AMT — already in the
    unit's base currency on every row, including rows a journal entered in
    EUR produced, which is why it ties. "transaction" reports POSTED_TRAN_AMT
    as entered; it is keyed by CURRENCY_CD, reported in totals.by_currency,
    and NO grand total is produced because amounts in different currencies
    cannot be added. Use it for "what did we actually spend in EUR", never to
    tie out. Read the `currency` block before quoting any figure."""
    return _safe(
        engine.trial_balance,
        business_unit=business_unit, fiscal_year=fiscal_year, period=period,
        ledger=ledger, group_by=group_by, dept=dept, account=account,
        currency=currency, include_adjustments=include_adjustments,
        max_rows=max_rows, amount_basis=amount_basis,
    )


@mcp.tool()
def get_account_balance(
    account: str,
    business_unit: str = "",
    fiscal_year: int = 0,
    through_period: int = 0,
    ledger: str = "",
    dept: str = "",
) -> dict:
    """One account's beginning-of-year balance, month-by-month activity trend, and
    ending balance through a period (plus adjustment-period amounts). Use for
    'what is the balance of X' and 'show the monthly trend of X' questions."""
    return _safe(
        engine.account_balance,
        account=account, business_unit=business_unit, fiscal_year=fiscal_year,
        through_period=through_period, ledger=ledger, dept=dept,
    )


@mcp.tool()
def compare_trial_balance(
    business_unit: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    vs_fiscal_year: int = 0,
    vs_period: int = 0,
    ledger: str = "",
    dept: str = "",
    account: str = "",
    min_abs_change: float = 0.0,
    top: int = 25,
) -> dict:
    """Compare ending balances between two fiscal year/period snapshots; returns the
    largest movers with change and % change. Defaults: current period vs the prior
    period (pass vs_fiscal_year for a prior-year comparison, e.g. same period last year).
    Flags accounts that are new or that dropped away."""
    return _safe(
        engine.compare_trial_balance,
        business_unit=business_unit, fiscal_year=fiscal_year, period=period,
        vs_fiscal_year=vs_fiscal_year, vs_period=vs_period, ledger=ledger,
        dept=dept, account=account, min_abs_change=min_abs_change, top=top,
    )


@mcp.tool()
def explain_balance_change(
    account: str,
    by: str = "",
    business_unit: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    vs_fiscal_year: int = 0,
    vs_period: int = 0,
    ledger: str = "",
    dept: str = "",
    min_abs_contribution: float = 0.0,
    top: int = 10,
) -> dict:
    """    WHY DID IT CHANGE / WHAT DROVE IT / BREAK DOWN THE MOVEMENT / WHICH
    DEPARTMENT CAUSED IT / EXPLAIN THE VARIANCE. Returns an exact additive
    BRIDGE of the movement across one dimension, with per-member
    contribution and share, plus the unexplained residual. Quote that
    residual — it is the point.

    Requires an account filter: the whole trial balance nets to zero, so
    "the total change" has no subject. One dimension per call — by=ACCOUNT
    for a range, or a chartfield such as DEPTID. Defaults are disclosed in
    by_source. No price/volume/mix: PS_LEDGER has no quantity column.
    Chartable."""
    return _safe(
        engine.explain_balance_change,
        account=account, by=by, business_unit=business_unit,
        fiscal_year=fiscal_year, period=period,
        vs_fiscal_year=vs_fiscal_year, vs_period=vs_period, ledger=ledger,
        dept=dept, min_abs_contribution=min_abs_contribution, top=top,
    )


@mcp.tool()
def get_budget_variance(
    business_unit: str = "", fiscal_year: int = 0, period: int = 0,
    ledger: str = "", budget_ledger: str = "", account: str = "",
    dept: str = "", top: int = 25, min_abs_variance: float = 0.0,
    include_balance_sheet: bool = False,
) -> dict:
    """Actual vs BUDGET year-to-date by account, with variance, % and
    FAVOURABILITY. Use for "budget vs actual / are we over budget / where
    are we overspending / variance to plan". Profit-and-loss accounts only
    by default (budgets are set on the P&L; the payload discloses this and
    include_balance_sheet=true overrides). Favourability comes from the
    account TYPE, never the sign — under-spending is favourable,
    under-earning is not. Totals are split revenue vs expense because one
    merged total of signed amounts means nothing. For period-over-period
    movement use compare_trial_balance instead."""
    return _safe(
        engine.budget_variance, business_unit=business_unit,
        fiscal_year=fiscal_year, period=period, ledger=ledger,
        budget_ledger=budget_ledger, account=account, dept=dept, top=top,
        min_abs_variance=min_abs_variance,
        include_balance_sheet=include_balance_sheet)


@mcp.tool()
def drill_to_journals(
    account: str,
    period: int,
    business_unit: str = "",
    fiscal_year: int = 0,
    ledger: str = "",
    dept: str = "",
    limit: int = 100,
) -> dict:
    """Posted journal lines behind one account's activity in one period (journal id,
    date, source, operator, line amount, description), plus a tie-out of journal
    total vs ledger activity. Use to answer 'what makes up / who posted X'."""
    return _safe(
        engine.drill_to_journals,
        account=account, period=period, business_unit=business_unit,
        fiscal_year=fiscal_year, ledger=ledger, dept=dept, limit=limit,
    )


@mcp.tool()
def get_journal_status(
    journal_id: str = "",
    business_unit: str = "",
    ledger: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    as_of_date: str = "",
    limit: int = 500,
) -> dict:
    """Exact CURRENT PeopleSoft journal status for one journal or a scoped
    period population. Keeps every BUSINESS_UNIT/JOURNAL_ID/JOURNAL_DATE/
    UNPOST_SEQ version and reports the delivered status code and meaning;
    it never treats every non-P status as the same kind of "unposted" item.

    Use journal_id for "what is journal J0001's status?" and leave it blank
    for "which journals still need action before close?". business_unit,
    ledger, fiscal_year and period are caller scope. as_of_date limits the
    JOURNAL_DATE population; PS_JRNL_HEADER is current state, so this does
    NOT prove what the status was historically at that date. A posted-by-
    cutoff claim additionally requires POSTED_DATE. Read status/evaluated,
    evidence_completeness and truncated before drawing a conclusion."""
    return _safe(
        journal_status.evaluate,
        journal_id=journal_id,
        business_unit=business_unit,
        ledger=ledger,
        fiscal_year=fiscal_year,
        period=period,
        through_date=as_of_date,
        limit=limit,
    )


@mcp.tool()
def search_accounts(query: str = "", account_type: str = "", limit: int = 50,
                    business_unit: str = "") -> dict:
    """Find GL accounts by description text or account-number prefix.
    Uses the business unit's own SetID, so pass the active scope's BU.
    account_type filter: A=Asset, L=Liability, Q=Equity, R=Revenue, E=Expense.
    Empty query lists accounts (up to limit)."""
    return _safe(engine.search_accounts, query=query, account_type=account_type,
                 limit=limit, business_unit=business_unit)


@mcp.tool()
def resolve_period(date: str = "") -> dict:
    """Convert a calendar date (YYYY-MM-DD, or empty for today) into fiscal year and
    accounting period using the GL detail calendar. Call this first whenever the
    user gives a date or says 'current/last month'."""
    return _safe(engine.resolve_period, date=date)


@mcp.tool()
def list_periods(fiscal_year: int = 0) -> dict:
    """Accounting-period calendar (begin/end dates per period) for a fiscal year."""
    return _safe(engine.list_periods, fiscal_year=fiscal_year)


@mcp.tool()
def tb_integrity_check(
    business_unit: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    ledger: str = "",
) -> dict:
    """    Trial-balance health check through a period: does the TB net to zero,
    plus total debits and credits, suspense account balances, accounts
    missing chartfield definitions, inactive accounts with balances,
    unposted journals, posted journals that don't balance, and whether
    beginning balances roll correctly from the prior-year close (retained
    earnings). Use for "does it balance / is it clean / out of balance / TB
    does not match / ready to close".
    Read "balanced" for whether the books balance and "control_status" for
    whether exceptions were found; they are different verdicts."""
    return _safe(
        engine.tb_integrity_check,
        business_unit=business_unit, fiscal_year=fiscal_year, period=period, ledger=ledger,
    )


@mcp.tool()
def detect_transaction_anomalies(
    as_of_date: str = "",
    history_months: int = 3,
    business_unit: str = "",
    include_inferred: bool = True,
) -> dict:
    """Detect TODAY'S TRANSACTION/INTERFACE/PROCESS ANOMALIES for a selected
    as-of date. Checks daily table volumes against a 3- or 6-calendar-month
    robust baseline, related-table counterpart mismatches, and operational
    process duration/success deterioration. Returns observed values, expected
    baseline/relationship, severity, confidence, and discovery evidence.

    Table names are discovered from the live database/PeopleTools metadata and
    observed co-activity — no PS_ prefix is assumed. Site rules in the
    ``anomalies`` config section override inference and are the right choice for
    custom interfaces or header/line relationships whose semantics metadata
    cannot prove. Missing/sparse history is disclosed, never called clean.
    As-of zeroes inside a configurable feed-freshness window are inconclusive,
    not critical. history_months must be 3 or 6. A blank business_unit resolves
    to one configured/authorized unit; ALL must be explicit and unrestricted.
    Scoped runs skip any table without a validated scope column."""
    return _safe(
        anomaly_detector.detect,
        as_of_date=as_of_date,
        history_months=history_months,
        business_unit=business_unit,
        include_inferred=include_inferred,
    )


@mcp.tool()
def rollup_trial_balance(
    business_unit: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    tree_name: str = "",
    level: int = 2,
    ledger: str = "",
) -> dict:
    """Trial balance summarized by PeopleSoft tree nodes (e.g. Total Assets,
    Liabilities, Revenue) using PSTREENODE/PSTREELEAF ranges. tree_name defaults to
    the configured account tree; level 2 = top summary captions."""
    return _safe(
        engine.rollup_trial_balance,
        business_unit=business_unit, fiscal_year=fiscal_year, period=period,
        tree_name=tree_name, level=level, ledger=ledger,
    )


@mcp.tool()
def get_record_map() -> dict:
    """The semantic map of PeopleSoft records by domain (general ledger,
    billing, receivables, currency, setup), with live row counts from THIS
    database. Transaction records (they carry amounts) are flagged when
    suspiciously small; reference records are legitimately small. Each entry
    names the curated tool to prefer over raw SQL. CALL THIS BEFORE run_sql
    whenever unsure which record answers a question."""
    return _safe(engine.get_record_map)


@mcp.tool()
def describe_metadata_catalog(source: str = "") -> dict:
    """Coverage and freshness of the offline metadata intelligence catalog.
    source chooses one configured database (default: the PeopleSoft primary).
    Each database has a physically separate artifact; this reports that one
    source's harvested layers, build limits/errors, and snapshot freshness.
    This is STRUCTURE only: it contains no transaction rows or balances and
    cannot support a financial conclusion. If unavailable, return the exact
    offline build command to the user."""
    def _describe() -> dict:
        name, catalog = _metadata_for_source(source)
        result = catalog.describe()
        result["source_database"] = name
        try:
            # This is deliberately a bounded operational summary.  Proposal
            # prose is available only through the private operator review
            # command, never through this model-facing catalog description.
            result["source_knowledge"] = (
                _source_knowledge_for_source(name).summary())
        except (MetadataError, SourceKnowledgeError) as exc:
            result["source_knowledge"] = {
                "available": False,
                "detail": str(exc),
                "effect": "no source annotations were used",
            }
        return result

    return _safe(_describe)


@mcp.tool()
def search_metadata(query: str, source: str = "", kinds: str = "",
                    limit: int = 20) -> dict:
    """Find the database object behind natural business/custom terminology.
    Searches the selected source's physical tables/views, fields, native keys
    and view relationships. The default Finance artifact may additionally
    contain logical PeopleTools records, labels, translate values, pages and
    public saved-query use. NEVER add PS_ or another prefix yourself. Results
    explain relevance, confidence and provenance. source selects exactly one
    physical artifact; kinds is an optional comma list such as
    table,view,record,field. Call get_metadata_context before querying an
    unfamiliar result. Metadata is structural, not row/financial evidence."""
    def _search() -> dict:
        name, catalog = _metadata_for_source(source)
        result = catalog.search(
            query=query, source=name, kinds=kinds, limit=limit)
        result["source_database"] = name
        matches = result.get("matches") if isinstance(result, dict) else None
        if not isinstance(matches, list):
            return result
        requested = str(query or "").strip().casefold()
        requested_forms = {
            requested,
            requested.replace(" ", "_"),
        }
        native_exact_ids = set()
        for item in matches:
            if not isinstance(item, dict):
                continue
            physical = str(item.get("physical_object") or "").casefold()
            schema = str(item.get("schema") or "").casefold()
            logical = [
                str(value or "").casefold()
                for value in (item.get("logical_records") or [])
            ]
            native_forms = {physical, f"{schema}.{physical}", *logical}
            if requested_forms & native_forms:
                native_exact_ids.add(str(item.get("object_id") or ""))
        approved, extra_subjects, excluded, knowledge_info = (
            _approved_search_candidates(catalog, name, query))
        wanted_kinds = {
            token.lower() for token in str(kinds or "").replace(",", " ").split()
            if token
        }
        if wanted_kinds:
            extra_subjects = [
                subject for subject in extra_subjects
                if str(subject.get("kind") or "").lower() in wanted_kinds
            ]
            allowed_ids = {str(subject.get("object_id") or "")
                           for subject in extra_subjects}
            approved = {object_id: rows for object_id, rows in approved.items()
                        if object_id in allowed_ids}
            if knowledge_info.get("available") is True:
                knowledge_info["approved_matches"] = sum(
                    len(rows) for rows in approved.values())
                knowledge_info["matched_objects"] = len(approved)
        exact_alias_ids = {
            object_id for object_id, rows in approved.items()
            if any(row.get("matched_on") == "approved alias" for row in rows)
        }
        if len(exact_alias_ids) > 1:
            # An approved alias is governance evidence for vocabulary, not a
            # licence to choose between two current physical objects.  Make
            # the collision explicit before any stable/lexical ordering can
            # look like a recommendation to the model.
            alias_candidates = [
                {
                    "object_id": str(subject.get("object_id") or ""),
                    "source": name,
                    "schema": subject.get("schema"),
                    "kind": subject.get("kind"),
                    "physical_object": subject.get("physical_object"),
                }
                for subject in extra_subjects
                if str(subject.get("object_id") or "") in exact_alias_ids
            ]
            alias_candidates.sort(key=lambda item: (
                str(item.get("schema") or ""),
                str(item.get("physical_object") or ""),
                str(item.get("object_id") or ""),
            ))
            knowledge_info["approved_alias_ambiguous"] = True
            knowledge_info["approved_alias_candidates"] = alias_candidates
            result["ambiguous"] = True
            result["detail"] = (
                "That exact approved source alias names more than one current "
                "object. Pass schema.object to get_metadata_context; no "
                "target was selected.")
        known_ids = {str(item.get("object_id") or "") for item in matches
                     if isinstance(item, dict)}
        for subject in extra_subjects:
            object_id = str(subject.get("object_id") or "")
            if object_id not in known_ids:
                matches.append(subject)
                known_ids.add(object_id)

        suppressed = []
        selectable = []
        for item in matches:
            object_id = str(item.get("object_id") or "") \
                if isinstance(item, dict) else ""
            rules = excluded.get(object_id, [])
            if rules:
                suppressed.append({
                    "object_id": object_id,
                    "source": name,
                    "schema": item.get("schema"),
                    "kind": item.get("kind"),
                    "physical_object": item.get("physical_object"),
                    "operator_exclusions": [
                        _exclusion_annotation(row) for row in rules
                    ],
                })
                approved.pop(object_id, None)
                continue
            selectable.append(item)
        matches = selectable
        if suppressed:
            knowledge_info["suppressed_matches"] = len(suppressed)
            knowledge_info["selection_rule"] = (
                "suppressed objects are not candidates and may not be queried "
                "for answers")
            result["excluded_by_operator"] = suppressed
            if not matches:
                result["detail"] = (
                    "Matching catalog objects were explicitly excluded by an "
                    "operator. No selectable record was returned; search for "
                    "an alternative or revoke the exclusion if it is obsolete.")

        if metadata_reranker.enabled:
            ranked = metadata_reranker.rerank(query, matches)
            matches = ranked.pop("matches")
            result["semantic_rerank"] = ranked

        # Approved vocabulary wins after optional model similarity. Its prose
        # is attached only now, after the embedding boundary saw structure.
        original_position = {
            str(item.get("object_id") or ""): position
            for position, item in enumerate(matches)
            if isinstance(item, dict)
        }
        approved_score = {
            object_id: max(int(row.get("semantic_score") or 0)
                           for row in rows)
            for object_id, rows in approved.items()
        }
        for item in matches:
            if not isinstance(item, dict):
                continue
            rows = approved.get(str(item.get("object_id") or ""), [])
            if rows:
                item["approved_source_meanings"] = [
                    _knowledge_annotation(row) for row in rows
                ]
        matches.sort(key=lambda item: (
            0 if str(item.get("object_id") or "") in native_exact_ids else
            1 if str(item.get("object_id") or "") in approved else 2,
            -approved_score.get(str(item.get("object_id") or ""), 0),
            original_position.get(str(item.get("object_id") or ""), 10**9),
        ))
        try:
            cap = min(max(int(limit or 20), 1), 100)
        except (TypeError, ValueError):
            cap = 20  # catalog.search already reports invalid limits
        result["matches"] = matches[:cap]
        result["count"] = len(result["matches"])
        result["truncated"] = bool(
            result.get("truncated") or len(matches) > cap)
        if knowledge_info:
            result["source_knowledge"] = knowledge_info
        return result

    return _safe(_search)


@mcp.tool()
def get_metadata_context(identifier: str, source: str = "",
                         limit: int = 40) -> dict:
    """Explain one object/field from exactly one source's offline catalog.
    Returns observed columns, ordered indexes, native constraints and view
    relationships with confidence/provenance. Finance may also return proven
    PeopleTools logical mappings, labels and translate codes. Ambiguous names
    return bounded candidates instead of guessing. Use the returned qualified
    physical object exactly, then obtain live evidence from a tool available in
    the same workspace. This snapshot contains no transaction values."""
    def _context() -> dict:
        name, catalog = _metadata_for_source(source)
        result = catalog.context(
            identifier=identifier, source=name, limit=limit)
        result["source_database"] = name
        try:
            store = _source_knowledge_for_source(name)
            if not store.path.exists():
                return result
            if result.get("found") is True:
                return _attach_approved_context(
                    result, catalog, store, name)
            # Approved aliases fill missing business vocabulary, but never
            # override an ambiguous catalog identifier.
            if result.get("ambiguous"):
                return result
            aliases = []
            ignored_aliases = 0
            for row in store.resolve_alias(identifier):
                try:
                    validate_catalog_aliases(
                        catalog, name, str(row.get("object_id") or ""),
                        row.get("aliases") or ())
                except SourceKnowledgeError:
                    ignored_aliases += 1
                    continue
                current = catalog.context(
                    identifier=f"{row['schema']}.{row['object']}",
                    source=name, limit=1)
                try:
                    current_subject = _exact_metadata_subject(current, name)
                except SourceKnowledgeError:
                    ignored_aliases += 1
                    continue
                if (str(current_subject["object_id"])
                        != str(row.get("object_id") or "")):
                    ignored_aliases += 1
                    continue
                aliases.append(row)
            object_ids = {str(row.get("object_id") or "") for row in aliases}
            if len(object_ids) > 1:
                result["ambiguous"] = True
                result["approved_alias_candidates"] = [
                    {
                        "object_id": row.get("object_id"),
                        "source": name,
                        "schema": row.get("schema"),
                        "kind": row.get("kind"),
                        "physical_object": row.get("object"),
                        "approved_source_meaning": _knowledge_annotation(row),
                    }
                    for row in aliases
                ]
                result["detail"] = (
                    "That approved source alias names more than one object. "
                    "Pass schema.object explicitly; no target was chosen.")
                return result
            if len(object_ids) == 1:
                row = aliases[0]
                resolved = catalog.context(
                    identifier=f"{row['schema']}.{row['object']}",
                    source=name, limit=limit)
                resolved["source_database"] = name
                subject = _exact_metadata_subject(resolved, name)
                if str(subject["object_id"]) != str(row["object_id"]):
                    raise SourceKnowledgeError(
                        "the approved alias target no longer matches the "
                        "current source catalog; no annotation was used")
                resolved["requested_identifier"] = identifier
                resolved["identifier_resolution"] = {
                    "via": "approved source alias",
                    "proposal_id": row.get("id"),
                    "status": "approved",
                }
                return _attach_approved_context(
                    resolved, catalog, store, name)
            if ignored_aliases:
                result["source_knowledge"] = {
                    "available": True,
                    "ignored_stale_targets": ignored_aliases,
                    "effect": "no stale approved alias was used",
                }
        except (MetadataError, SourceKnowledgeError) as exc:
            result["source_knowledge"] = {
                "available": False,
                "detail": str(exc),
                "effect": "no source annotations were used",
            }
        return result

    return _safe(_context)


@mcp.tool()
def propose_metadata_meaning(
    identifier: str,
    meaning: str,
    aliases: str = "",
    source: str = "",
    selection: str = "prefer",
) -> dict:
    """PROPOSE a durable selection rule for one exact table/view.

    Use only when the user explicitly names and teaches/corrects the object.
    First call get_metadata_context, then pass its exact schema.object as
    identifier. aliases is optional comma-separated business wording.
    selection=prefer makes the approved meaning a discovery pointer;
    selection=exclude makes approval a hard "do not use for answers" veto.
    Use exclude for junk, obsolete, duplicate, or non-reporting staging
    objects, but not merely because a valid interface table is called staging.
    This
    writes only a PENDING entry to the selected source's private local review
    queue; it does not change that database, search ranking, context, joins or
    the prompt until a host operator approves it. Never use it for inferred
    meanings, relationships, SQL, status rules, row values or financial facts.
    """
    def _propose() -> dict:
        name, catalog = _metadata_for_source(source)
        context = catalog.context(identifier, source=name, limit=10)
        subject = _exact_metadata_subject(context, name)
        alias_list = normalize_aliases(aliases)
        validate_catalog_aliases(
            catalog, name, str(subject["object_id"]), alias_list)
        store = _source_knowledge_for_source(name)
        proposal = store.propose(
            object_id=str(subject["object_id"]),
            schema=str(subject["schema"]),
            object_name=str(subject["physical_object"]),
            object_kind=str(subject["kind"]),
            meaning=meaning,
            aliases=alias_list,
            origin="conversation",
            selection=selection,
        )
        status = str(proposal.get("status") or "pending")
        proposal_id = str(proposal.get("id") or "")
        return {
            **proposal,
            "proposal_id": proposal_id,
            "source_database": name,
            "retrieval_active": status in {"approved", "excluded"},
            "review_command": (
                "python -m pstb.source_knowledge --source "
                f"{shlex.quote(name)} --approve {proposal_id}"),
            "note": (
                ("This object is already operator-excluded from answers."
                 if status == "excluded" else
                 "This exact proposal is already operator-approved and active.")
                if status in {"approved", "excluded"} else
                "Submitted for operator review. It is pending and has no "
                "effect on retrieval, context, prompts, relationships or SQL."),
        }

    return _safe(_propose)


@mcp.tool()
def trace_process(question: str, hops: int = 3, limit: int = 40) -> dict:
    """HOW something is DONE here, end to end — the answer to "how do we do
    invoicing", "how do we pay a supplier", "what is our close process",
    "where is customer credit maintained". Returns the chain this
    installation actually has, in the order a person meets it: the menu
    NAVIGATION, the COMPONENT and PAGES a user opens, the RECORDS those
    pages write, the setup tables that govern them, the tools here that can
    query them, and the written procedure that describes them. A qualifier
    like "for India" or "for US001" is resolved to business units and
    reported in scope_applied — pass those units to the financial tools
    afterwards. This holds NO amounts: it explains structure, and every
    figure still comes from a financial tool. Read from a graph built
    offline by scripts/build_process_graph.py; if it reports available:
    false, say the graph has not been built and name that script."""
    return _safe(process_graph.trace, question=question, hops=hops,
                 limit=limit)


@mcp.tool()
def get_entity_network(entity: str = "", kind: str = "",
                       business_unit: str = "", limit: int = 40) -> dict:
    """ONE actor and everything it trades with: a customer's products and
    units, a product's customers, a supplier's units, a unit's customers.
    entity takes an id OR a name (ambiguity comes back as a question).
    Use for "which customers buy LIC-SAAS", "what does ACME actually buy",
    "who supplies us in US001". Amounts are a DERIVED WEIGHT stamped as_of
    the graph build, not the ledger — quote them as of that date and use
    the tool named in next_steps for a live figure. Only flows in business
    units this user is granted are returned, and the payload says so when
    it withheld any."""
    return _safe(entity_graph.neighbourhood, entity=entity, kind=kind,
                 business_unit=business_unit, limit=limit)


@mcp.tool()
def get_concentration(kind: str = "customer", by: str = "",
                      business_unit: str = "", limit: int = 10) -> dict:
    """Who carries the weight, and what depends on a single partner. kind
    is customer, product, supplier or business_unit. Returns a ranked list
    with each actor's SHARE of the visible population, the top-N share, and
    single_partner — actors with exactly one counterparty, which is a
    dependency rather than a statistic (a product with one customer
    disappears with that customer). Use for "customer concentration", "how
    exposed are we to one product", "revenue concentration risk". Shares are
    of what THIS caller can see, stated in the payload; amounts are as of
    the graph build."""
    return _safe(entity_graph.concentration, kind=kind, by=by,
                 business_unit=business_unit, limit=limit)


@mcp.tool()
def get_entity_connection(source: str = "", target: str = "",
                          business_unit: str = "", hops: int = 3) -> dict:
    """How two actors are connected, with the evidence on every hop. Use for
    "is this supplier anything to do with that customer", "how is ACME
    connected to Summit Machining". Each hop is labelled 'recorded
    hierarchy' (the system says they are related) or 'shared transactions'
    (they merely trade in the same place) — and every hop carries a `reads`
    sentence stating the relationship the way the SYSTEM records it, which
    is not always the direction the path was walked. A path is a CONNECTION,
    never a conclusion: two actors sharing a business unit share it with
    everyone else in that unit."""
    return _safe(entity_graph.connection, source=source, target=target,
                 business_unit=business_unit, hops=hops)


@mcp.tool()
def describe_entity_graph() -> dict:
    """What the entity graph covers, when it was built, over what window,
    and which business units this caller can see in it. Call when the actor
    tools return less than expected, or to say how fresh their amounts are."""
    return _safe(entity_graph.describe)


@mcp.tool()
def describe_process_graph() -> dict:
    """What the process graph covers and when it was built: which sources
    were harvested, which were DEGRADED (a metadata table this account
    cannot read leaves a hole), how many nodes of each kind, and the build
    notes. Call this when trace_process returns less than expected, or when
    asked how the agent knows the process — the answer is "from this
    instance's own metadata at build time", and the notes say what was
    missing."""
    return _safe(process_graph.describe)


@mcp.tool()
def get_exchange_rate(
    from_currency: str,
    to_currency: str,
    as_of_date: str = "",
    rate_type: str = "",
    amounts: str = "",
) -> dict:
    """Effective-dated exchange rate from PS_RT_RATE_TBL (direct, inverse, or
    triangulated via the base currency). Pass amounts as a comma list
    ("185196.06, 96000") to have them converted SERVER-SIDE — never multiply
    amounts yourself; copy the returned conversions verbatim."""
    return _safe(
        engine.exchange_rate, from_currency=from_currency,
        to_currency=to_currency, as_of_date=as_of_date, rate_type=rate_type,
        amounts=amounts,
    )


@mcp.tool()
def get_top_billing_customers(
    business_unit: str = "",
    n: int = 10,
    months: int = 12,
    as_of_date: str = "",
    display_currency: str = "",
    active_within_months: int = 0,
) -> dict:
    """    Top customers by FINALIZED billing volume (PS_BI_HDR, status INV) over a
    trailing window, with invoice counts and share of total. Use for "top N
    billing customers / who do we bill the most". This is billing volume;
    open balances are get_ar_aging.

    business_unit="ALL" ranks across EVERY business unit in ONE call.
    active_within_months=N keeps only customers with an invoice in the last
    N months — the tool-level meaning of "still buying / active".
    Mixed currencies are never summed — pass display_currency to rank on
    converted totals.
    The window is CALENDAR months, closed at both ends: invoices dated after
    as_of_date are outside it. Check ranking_complete — when it is false the
    grouped read hit its row cap, customers past the cut-off were never
    counted, and the list must NOT be presented as "the top N"."""
    return _safe(
        ar.top_billing_customers, business_unit=business_unit, n=n,
        months=months, as_of_date=as_of_date, display_currency=display_currency,
        active_within_months=active_within_months,
    )


@mcp.tool()
def get_invoice_lifecycle(business_unit: str = "",
                          as_of_date: str = "") -> dict:
    """Where is the billing delay: the pipeline from the billing interface
    to AR as STAGES — interface waiting/error, bills by status with amounts
    and ages, finalized-but-not-in-AR orphans, open in AR — with the
    bottleneck named. Use for "where is the delay / how does an invoice
    flow / what is stuck between order and cash". Cycle times appear only
    where the site stores creation timestamps; otherwise current stage
    ages are reported and the limitation is disclosed."""
    return _safe(ar.invoice_lifecycle, business_unit=business_unit,
                 as_of_date=as_of_date)


@mcp.tool()
def get_customer_financial_360(cust_id: str = "", business_unit: str = "",
                               include_family: bool = True,
                               months: int = 12,
                               as_of_date: str = "") -> dict:
    """ONE customer, everything connected: the corporate family, current
    billing (finalized and still in flight), open receivables with aging
    and disputes, cash received and how much of it was actually applied,
    credit/rebill chains and what they netted to, plus a needs_attention
    list and a node/edge relationship graph. THIS IS ALSO THE REVENUE TOOL
    FOR ONE CUSTOMER — billing.by_status carries their invoiced revenue
    over the window; PS_LEDGER has no customer column, so a trial balance
    cannot answer it. Use for "revenue for customer X", "how much did we
    bill X", "give me the complete picture for X", "what is stuck",
    "which subsidiaries drive the parent's overdue balance". cust_id takes
    an ID **or a name** — the server resolves it and reports what it read
    the name as; several matches come back as scope_status
    ambiguous_customer with multiple_matches to ask about, and none as
    customer_not_found, which is NO DATA and never a zero. Family
    membership comes from the corporate hierarchy recorded in the system —
    customers are NEVER grouped by name. Totals are per currency and are
    not added across currencies. For a ranked list of many customers use
    get_customer_intelligence instead."""
    return _safe(relationships.customer_financial_360, cust_id=cust_id,
                 business_unit=business_unit, include_family=include_family,
                 months=months, as_of_date=as_of_date)


@mcp.tool()
def search_vendors(query: str = "", limit: int = 25,
                   business_unit: str = "", as_of_date: str = "") -> dict:
    """Find a supplier by ID or name substring, with its open payables and
    whether it belongs to a corporate supplier group. The AP counterpart of
    search_customers. Use it FIRST when the question names a supplier by
    name rather than by id. If the payload reports
    belongs_to_a_corporate_family or heads_a_corporate_family, the balance
    shown is that legal entity ALONE — say so, and use
    get_vendor_payables_network for the group."""
    # as_of_date is accepted so the scope guard can inject it uniformly;
    # a supplier's identity does not change with the date, so it is not
    # used. Refusing it would raise a TypeError on every scoped call.
    return _safe(modules.search_vendors, query=query, limit=limit,
                 business_unit=business_unit)


@mcp.tool()
def get_vendor_payables_network(vendor_id: str = "", business_unit: str = "",
                                include_family: bool = True,
                                months: int = 12,
                                as_of_date: str = "") -> dict:
    """ONE supplier, everything connected: the corporate supplier family,
    open payables with overdue and stuck vouchers, cash paid, duplicate
    vouchers, and IDENTITY LINKS — other suppliers sharing the same remit
    bank account or the same taxpayer id. Use for "the full picture for
    supplier X", "who else banks where X banks", "are we paying two
    suppliers into one account", "which subsidiaries owe the group's
    balance", and for what we SPEND with one supplier — PS_LEDGER has no
    supplier column, so a trial balance cannot answer that. vendor_id
    takes an ID **or a name**, resolved server-side; several matches come
    back as scope_status ambiguous_supplier with multiple_matches to ask
    about, and none as supplier_not_found, which is NO DATA and never a
    zero. Bank accounts and taxpayer ids are NEVER returned — a shared key
    appears only as a keyed hash token, and a shared key is a reason to
    investigate, NOT evidence that two suppliers are the same company.
    Only the recorded corporate hierarchy says that; never group suppliers
    by name yourself."""
    return _safe(vendor_network.vendor_payables_network, vendor_id=vendor_id,
                 business_unit=business_unit, include_family=include_family,
                 months=months, as_of_date=as_of_date)


@mcp.tool()
def get_match_exceptions(business_unit: str = "", months: int = 12,
                         as_of_date: str = "") -> dict:
    """Every break in the purchase-to-pay tie, computed from the documents
    and ranked by money: vouchered OVER the order price, invoiced more than
    was RECEIVED, vouchered with NO receipt at all, received and NEVER
    invoiced (a document-review candidate whose booking status is unknown),
    and orders still awaiting receipt
    — with canceled orders excluded, never counted as late. Use for "any
    match exceptions", "did we get what we paid for", "receipts not
    invoiced", "three-way match problems". The system's own
    MATCH_STATUS_VCHR flags are reported BESIDE the recomputed arithmetic,
    never merged — when they disagree, say so, because the disagreement is
    an override or a tolerance and worth reading. Amounts are per currency
    and never summed across."""
    return _safe(procurement.match_exceptions, business_unit=business_unit,
                 months=months, as_of_date=as_of_date)


@mcp.tool()
def get_po_grni_candidates(
    business_unit: str = "",
    as_of_date: str = "",
    materiality: float = 0.0,
    aging_days: int = 30,
    max_rows: int = 50000,
) -> dict:
    """PO-LINKED PeopleSoft receipt-schedule GRNI REVIEW CANDIDATES at one cut-off.
    Computes accepted receipt value less cutoff-eligible voucher-line value
    on the same PO line/schedule, with currencies kept separate. Use for
    "which received-not-invoiced items should we review/accrue at close?".

    Coverage is limited to same-business-unit, PO-linked documents; non-PO
    receipt accruals need the delivered accounting source. This is document-
    level candidate evidence only. It does NOT prove that
    PO_RECVACCR generated an RAC receipt-accrual accounting line, that Journal
    Generator distributed it, or that GL posted it; read booked_status and
    candidate_basis and call the result candidates, not a booked liability.
    Missing dates/keys/status/currency, ambiguous custom records, historical
    availability uncertainty, and row caps fail closed. materiality is
    applied separately within each transaction currency; no cross-currency
    total is produced."""
    return _safe(
        grni.period_end_accrual,
        business_unit=business_unit,
        as_of_date=as_of_date,
        materiality=materiality,
        aging_days=aging_days,
        max_rows=max_rows,
    )


@mcp.tool()
def get_procurement_chain(reference: str = "", business_unit: str = "",
                          as_of_date: str = "") -> dict:
    """ONE purchase-to-pay chain, tied out end to end: the order and its
    schedules, what was received against them, the vouchers that billed
    them, and the payments that settled those — with every break named and
    both figures quoted. reference takes a PO id, a receiver id, a voucher
    id, OR a supplier (id or name, resolved like every other party tool —
    ambiguity comes back as a question, not a guess). Use for "why is this
    voucher stuck", "was this PO received", "show the chain for PO2003",
    "what is open on Summit Machining's orders". A canceled order is
    labeled canceled and never reported as awaiting receipt."""
    return _safe(procurement.procurement_chain, reference=reference,
                 business_unit=business_unit, as_of_date=as_of_date)


@mcp.tool()
def get_dso_trend(business_unit: str = "", fiscal_year: int = 0) -> dict:
    """Monthly DSO (days sales outstanding) computed from the ledger:
    ending AR control balance / period revenue x days in period, revenue
    from ACCOUNT_TYPE='R' postings. Formula disclosed in the payload. Use
    for "DSO trend / are we collecting faster or slower". Chartable."""
    return _safe(ar.dso_trend, business_unit=business_unit,
                 fiscal_year=fiscal_year)


@mcp.tool()
def get_cash_outlook(business_unit: str = "", weeks: int = 8,
                     as_of_date: str = "") -> dict:
    """Expected cash by week from DUE DATES: inflows from open AR items,
    outflows from open vouchers, net per currency, overdue in its own
    bucket. This is due-date arithmetic — the starting point a treasurer
    refines — NOT a payment-behavior forecast, and the payload says so.
    Use for "cash outlook / what is due in and out over the next weeks"."""
    return _safe(ar.cash_outlook, business_unit=business_unit, weeks=weeks,
                 as_of_date=as_of_date)


@mcp.tool()
def get_vendor_intelligence(business_unit: str = "", months: int = 12,
                            n: int = 20, as_of_date: str = "") -> dict:
    """Top vendors with HOW WE PAY them: payment totals and share, plus
    amount-weighted days early/late versus voucher due dates, and computed
    observations (paying early = cash handed over sooner than required;
    late = relationship risk; concentration). The AP mirror of customer
    intelligence. Use for "top vendors / do we pay early or late / vendor
    concentration". Copy observations verbatim — they are arithmetic."""
    return _safe(modules.vendor_intelligence, business_unit=business_unit,
                 months=months, n=n, as_of_date=as_of_date)


@mcp.tool()
def get_customer_intelligence(business_unit: str = "", n: int = 20,
                              months: int = 12,
                              display_currency: str = "",
                              as_of_date: str = "") -> dict:
    """Top customers WITH context: where they are based (city/state/
    country), what they buy (top products from bill lines), and how they
    pay (open AR, overdue, average days late, disputes) — plus computed
    observations: concentration risk, late payers with the working capital
    at stake, disputes, and lapsed top billers. Use for "who are my top
    customers and where are they based / what do they buy / what can I
    optimize". Copy observations verbatim — they are arithmetic over
    records, and every figure is in the payload. Plain ranking with no
    context is get_top_billing_customers."""
    return _safe(ar.customer_intelligence, business_unit=business_unit,
                 n=n, months=months, display_currency=display_currency,
                 as_of_date=as_of_date)


@mcp.tool()
def get_invoice_totals(business_unit: str = "", fiscal_year: int = 0) -> dict:
    """    Total FINALIZED invoice amount (BILL_STATUS='INV'), with a population
    block naming every applied default and everything excluded. Use for
    "total invoice amount / how much have we invoiced / total billed".
    Totals are per currency, never summed across. Pipeline detail by status
    is get_billing_workbench; open balances are get_ar_aging."""
    return _safe(ar.invoice_totals, business_unit=business_unit,
                 fiscal_year=fiscal_year)


@mcp.tool()
def get_ar_aging(
    business_unit: str = "",
    as_of_date: str = "",
    customer_id: str = "",
    detail: bool = False,
    display_currency: str = "",
) -> dict:
    """    AR aging by customer — "who owes us", "overdue", "collections", "aging",
    "does AR tie to the GL": current / 1-30 / 31-60 / 61-90 / over-90 buckets
    from open items (PS_ITEM), credits and disputes broken out, PLUS a tie-out
    of the subledger total to the GL AR control account.
    Positive = owed by customer; negative = credit memo / on-account receipt.

    as_of_date: YYYY-MM-DD, empty = today. detail=true adds item-level rows.
    display_currency: ISO code ("USD", "INR"...) — items are converted
    server-side at the effective rate before bucketing and ranking; empty =
    the BU's base currency. Never convert amounts yourself."""
    return _safe(
        ar.aging, business_unit=business_unit, as_of_date=as_of_date,
        customer_id=customer_id, detail=detail, display_currency=display_currency,
    )


@mcp.tool()
def get_customer_ar(
    customer: str, business_unit: str = "", as_of_date: str = "",
    display_currency: str = "",
) -> dict:
    """One customer's open AR: every open item with days past due and bucket,
    credit memos, disputes, and the customer's aging summary. customer can be an
    ID (C1001) or a name fragment; ambiguous names return the candidates to ask
    the user about. display_currency converts every item server-side (empty =
    BU base currency); converted items keep original/original_currency."""
    return _safe(
        ar.customer, customer=customer, business_unit=business_unit,
        as_of_date=as_of_date, display_currency=display_currency,
    )


@mcp.tool()
def search_customers(
    query: str = "", limit: int = 25, business_unit: str = "",
    display_currency: str = "", as_of_date: str = ""
) -> dict:
    """Find AR customers by name or ID; returns id, name, active/inactive
    status, and open balance CONVERTED to one currency (the unit's base
    unless display_currency says otherwise), with balances_by_currency
    beside it when the customer bills in more than one. Empty query lists
    customers. If the payload reports belongs_to_a_corporate_family or
    heads_a_corporate_family, the balance shown is that legal entity
    ALONE — say so, and use get_customer_financial_360 for the group."""
    return _safe(
        ar.search_customers,
        query=query,
        limit=limit,
        business_unit=business_unit,
        display_currency=display_currency,
        as_of_date=as_of_date,
    )


@mcp.tool()
def get_billing_workbench(
    business_unit: str = "", days_stuck: int = 5, as_of_date: str = ""
) -> dict:
    """Billing pipeline health: invoice counts/amounts by status (NEW, HLD, RDY,
    INV=finalized, CAN), invoices pending longer than days_stuck, billing
    interface lines in error, and finalized invoices that never reached AR.
    Use for "stuck invoices", "billing errors", "revenue not billed",
    "did every invoice make it to AR"."""
    return _safe(
        ar.billing_workbench, business_unit=business_unit,
        days_stuck=days_stuck, as_of_date=as_of_date,
    )


@mcp.tool()
def list_reports() -> dict:
    """Named financial reports (PS/nVision equivalents): income_statement,
    balance_sheet, quarterly_expenses, plus any added to reports/. Shows each
    report's columns and the available timespans. Use before run_report when
    the user asks for a statement, budget comparison, or quarterly view."""
    return _safe(report_runner.list_reports)


@mcp.tool()
def run_report(
    report: str = "",
    business_unit: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    ledger: str = "",
    rows: str = "",
    columns: str = "",
    include_adjustments: bool = False,
) -> dict:
    """Run a financial-statement style report grid (the PS/nVision pattern):
    rows from account-tree nodes or account ranges, columns as
    ledger + timespan, amounts summed from the ledger in base currency.

    report: a name from list_reports (e.g. income_statement). fiscal_year and
    period set the context — LEAVE AS 0 for current unless the user named them.
    Timespans: PER, YTD, BAL (includes balance forward), YR, QTD, Q1-Q4,
    ROLL12, PER-1Y, YTD-1Y, BAL-1Y, YR-1Y, or a period range like "4-6".
    Ad-hoc without a saved report: rows="node:REVENUE:flip,acct:5000-5999",
    columns="ACTUALS:YTD,BUDGET:YTD" (:flip shows credit balances positive)."""
    return _safe(
        report_runner.run,
        report=report, business_unit=business_unit, fiscal_year=fiscal_year,
        period=period, ledger=ledger, rows=rows, columns=columns,
        include_adjustments=include_adjustments,
    )


@mcp.tool()
def resolve_timespan(timespan: str, fiscal_year: int = 0, period: int = 0) -> dict:
    """Show exactly which fiscal periods a timespan covers in context (YTD, BAL,
    QTD, Q1-Q4, ROLL12, the -1Y prior-year variants, or "4-6"). Use when the
    user asks what a report basis means or wants period math confirmed."""
    def _res(timespan: str, fiscal_year: int, period: int) -> dict:
        from .report import resolve_timespan as rts

        _, fy, per, _ = engine._defaults("", fiscal_year, period, "")
        maxreg = engine._max_regular_period(fy)
        clamped = per > maxreg
        out = rts(timespan, fy, min(per, maxreg), maxreg)
        if clamped:
            out["context_note"] = (
                f"Period {per} is an adjustment period; resolved at the "
                f"post-adjustment year-end context (period {maxreg})."
            )
        return out
    return _safe(_res, timespan=timespan, fiscal_year=fiscal_year, period=period)


@mcp.tool()
def list_trees() -> dict:
    """List available PeopleSoft trees (name, setid, latest effective date)."""
    return _safe(engine.list_trees)


@mcp.tool()
def list_financial_scopes(include_activity: bool = False) -> dict:
    """Business units, their ledgers, base currency, and which fiscal years hold
    data — all in ONE call. Use this first when you don't know the scope.
    The default is a fast BU/ledger inventory. Set include_activity=true only
    when the user also asks for fiscal-year ranges or latest posted periods.
    Do not call list_business_units and list_ledgers together to work this out:
    both run in the same turn, so the second cannot use the first's result."""
    return _safe(
        engine.list_financial_scopes, include_activity=include_activity
    )


@mcp.tool()
def list_business_units() -> dict:
    """List GL business units with descriptions and base currency."""
    return _safe(engine.list_business_units)


@mcp.tool()
def list_ledgers(business_unit: str) -> dict:
    """Ledgers holding data for ONE business unit (e.g. ACTUALS, BUDGET).
    business_unit is required — prefer list_financial_scopes, which returns
    business units and ledgers together."""
    return _safe(engine.list_ledgers, business_unit=business_unit)


@mcp.tool()
def recall_site_facts() -> dict:
    """Facts an operator approved about THIS installation — naming aliases,
    calendar conventions, exclusions. They are already in your context; call
    this only when the user asks what the agent has been told, or to check
    whether something is remembered."""
    return _safe(memory.list_facts, status="approved")


@mcp.tool()
def remember_site_fact(fact: str, kind: str = "context") -> dict:
    """    PROPOSE a durable fact about this INSTALLATION for operator approval —
    a naming alias, a calendar convention, a standing exclusion ("exclude
    intercompany from revenue totals"). One table's purpose goes to
    remember_record_fact instead.
    kind: alias | convention | exclusion | context.
    Stored PENDING until a human approves it, so say it was noted for
    review — never that you have learned it."""
    return _safe(memory.propose, text=fact, kind=kind, source="conversation")


@mcp.tool()
def list_playbooks() -> dict:
    """    The playbooks run_playbook can execute — close_readiness,
    receivables_health, daily_brief, post_close_watch, ap_completeness —
    with their steps. Lists only; the verdict comes from run_playbook."""
    return _safe(playbooks.list_playbooks)


@mcp.tool()
def run_playbook(playbook: str = "", business_unit: str = "", ledger: str = "",
                 fiscal_year: int = 0, period: int = 0) -> dict:
    """    Run a review workflow end to end and return a composed verdict.

    playbook: close_readiness (default), receivables_health,
    daily_brief ("what needs my attention today" — exceptions only:
    billing errors, orphans, duplicates, stuck vouchers, late posts,
    two-week cash pressure), post_close_watch, or ap_completeness ("is AP
    complete for month-end / did everything approved reach AP / what should
    we accrue" as ONE composed check) — see list_playbooks.
    Read "verdict": passed = every step ran and found nothing;
    exceptions_found = every step ran and some found something;
    incomplete = a step COULD NOT RUN and must never be reported as a pass.
    Report the verdict and the steps needing attention."""
    return _safe(playbooks.run, playbook=playbook, business_unit=business_unit,
                 ledger=ledger, fiscal_year=fiscal_year, period=period)


@mcp.tool()
def list_sources() -> dict:
    """Configured database workspaces. 'default' is the Finance/PeopleSoft
    primary with curated controls. Each extra source is an isolated semantic,
    native-relationship and guarded read-only query workspace; it does not
    inherit PeopleSoft, Coupa, wiki, memory or curated business tools."""
    return _safe(lambda: {"sources": engine.registry.describe()})


@mcp.tool()
def join_path(from_record: str, to_record: str, source: str = "") -> dict:
    """How to JOIN two records in exactly one configured database.

    The default PeopleSoft source uses its live curated/index-aware
    relationship service. A secondary source uses only native foreign-key and
    view-dependency evidence from that source's physically isolated metadata
    artifact. Secondary results never infer relationships from matching column
    names; when native evidence is absent, they say no path was found. This
    structural tool remains available when live raw SQL is disabled.

    Declared foreign keys and view lineage are searched FIRST; only when
    no declared path exists are measured value-overlap joins admitted,
    each hop labeled MEASURED with its containment numbers, and no join
    SQL is compiled from a measured hop.
    """
    def _join() -> dict:
        name, catalog = _metadata_for_source(source)
        if name == "default":
            return _sourced(
                name, "join_path", from_record=from_record,
                to_record=to_record)
        exclusions = record_exclusions.for_source(name)
        result = catalog.relationship_path(
            from_object=from_record, to_object=to_record, source=name,
            excluded_object_ids=[row.get("object_id") for row in exclusions])
        result["source_database"] = name
        return result

    return _safe(_join)


if cfg.tools.allow_raw_sql:

    @mcp.tool()
    def run_sql(sql: str, max_rows: int = 100, business_unit: str = "",
                source: str = "", policy_binds: Optional[dict] = None,
                pivot: Optional[dict] = None,
                list_binds: Optional[dict] = None,
                partition: Optional[dict] = None) -> dict:
        """Run a read-only SQL SELECT against exactly the selected database.
                source is a hard boundary. Guarded: one SELECT/WITH statement, no
                DML/DDL, rows capped at 500, every table validated against the catalog
                and unqualified names schema-qualified automatically (write
                OTHER_OWNER.TBL only for an approved same-database schema). Database
                links, external rowsets and three/four-part remote names are refused.
                NEVER invent a table or column. On Finance only, PS_ accounting tables
                use signed amounts (credits negative).
                business_unit: reported back as scope_filtered / scope_note — the SQL is
                never rewritten. Finance only; forbidden in a secondary workspace.
                policy_binds: when the question refers to a POLICY figure ("above the
                capitalization threshold", "over the approval limit"), do NOT type the
                number. Write the bind in the SQL and name the policy here — e.g.
                sql="... WHERE COST >= :threshold", policy_binds={"threshold":
                "capitalization_threshold"}. The value is looked up in the wiki and
                bound server-side, and the result carries policy_basis with the exact
                sentence and page it came from — quote that when you answer. Call
                list_policy_terms for the available names. Finance only; policy/wiki
                evidence is forbidden in a secondary database workspace.
                pivot: USE THIS for any "trend", "by month", "over the last N periods",
                "compare X across Y" question. Return three columns — row label, column
                label, value — grouped by the first two, and name them:
                pivot={"row_field":"customer","column_field":"period","value_field":"amt"}.
                Totals, change and percentage change come back computed — quote them,
                never recompute.
                partition: BREAK a slow grouped aggregate into indexed slices and
                merge — the fix for a query that TIMES OUT. Write the query for ONE
                slice with a :partition bind (e.g. AND BUSINESS_UNIT = :partition) and
                pass partition={"values": "business_units"}. ORDER BY / FETCH FIRST
                apply ONCE after the merge, so a top-20 is the top-20 of the WHOLE, not
                of each slice. AVG must be rewritten as SUM + COUNT. Partition on an
                INDEXED column; slicing an unindexed column is N full scans and worse
                than the direct query.
                list_binds: CHAIN a set produced by an earlier tool into this query
                WITHOUT retyping the values. Write `IN (:accts)` in the SQL and pass
                list_binds={"accts": {"from_result": "r2", "field": "accounts"}} — r2
                is the result_id of the earlier result, field is a dot path into it
                ("accounts", "rows[].account", "customers[].cust_id")."""
        return _sourced(source, "run_sql", sql=sql, max_rows=max_rows,
                     business_unit=business_unit, policy_binds=policy_binds,
                     pivot=pivot, list_binds=list_binds, partition=partition)


    @mcp.tool()
    def explain_query(sql: str, source: str = "") -> dict:
        """Ask the optimizer how it WOULD run a SELECT — WITHOUT running it.
                source selects exactly one configured database. USE THIS before any
                join or aggregate over large/custom transaction tables, and immediately
                after a timeout. Returns row estimates, indexes/column order, planned
                full scans and leading columns the WHERE/JOIN should include."""
        return _sourced(source, "explain_query", sql=sql)

    @mcp.tool()
    def search_records(query: str = "", limit: int = 25, source: str = "") -> dict:
        """Find the right PeopleSoft record for a question by searching
                PeopleTools metadata — record DESCRIPTIONS and field names, not just
                table names. Use whenever the question names a module, a document, or
                a custom record you have not seen ("file interface", "voucher",
                "asset profile", "TU_FILE"). Returns the PeopleTools record name, the
                physical table to query, the description, and approximate row counts,
                most-populated first."""
        return _sourced(source, "search_records", query=query, limit=limit)

    @mcp.tool()
    def describe_record(record: str) -> dict:
        """Fields of one PeopleSoft record from PeopleTools (PSRECFIELD) plus
        the physical column list, so you can see both the record definition and
        what the database actually has. Accepts a record name (TU_FILE_INTFC)
        or a table name (PS_TU_FILE_INTFC)."""
        return _safe(engine.describe_record, record=record)

    @mcp.tool()
    def profile_record(table: str, sample_rows: int = -1, source: str = "") -> dict:
        """See what a record actually CONTAINS before querying it: every column,
                how many sampled rows populate each, and a few sample rows. Use it when
                more than one record could answer a question, when a record is custom or
                unfamiliar, or when a query returned nothing and you need to know
                whether the record is empty or your predicate was wrong. value_counts is
                the strongest signal — seeing ITEM_STATUS holds 'O' and 'C' turns a
                guessed predicate into a correct one; always_null names columns this
                site never populates whatever the catalog claims.
                Personal columns (names, addresses, tax and bank identifiers) are
                masked to a shape-preserving placeholder. fill_percent and value_counts
                are measured over the rows sampled, so never report them as totals."""
        return _sourced(source, "profile_record",
                     table=table, sample_rows=sample_rows)

    @mcp.tool()
    def compare_records(tables: list, sample_rows: int = -1,
                        source: str = "") -> dict:
        """Profile up to 6 candidate records side by side. The intended sequence
                for any unfamiliar question is search_records -> compare_records ->
                run_sql: search_records proposes candidates by description, this shows
                which are readable, populated, and carry the columns and status codes
                the question implies. Sites keep live vs history vs staging vs custom
                copies of one record; names cannot separate those and contents can."""
        return _sourced(source, "compare_records",
                     tables=tables, sample_rows=sample_rows)

    @mcp.tool()
    def list_tables(pattern: str = "", source: str = "") -> dict:
        """List tables/views matching a pattern (e.g. 'JRNL' or 'PS_LEDGER%')."""
        return _sourced(source, "list_tables", pattern=pattern)

    @mcp.tool()
    def describe_table(table_name: str, source: str = "") -> dict:
        """Live column names/types for one table/view in the selected source."""
        return _sourced(source, "describe_table", table_name=table_name)


@mcp.tool()
def remember_record_fact(table: str, fact: str) -> dict:
    """    Record what the user tells you a table HOLDS, so the next question finds
    it — "the interface file info is in TU_FILE_INTFC", "PS_XX_REBATE is our
    custom rebate accrual table". Table-scoped; installation-wide facts go to
    remember_site_fact.
    table: the record or table name exactly as it exists (TU_FILE_INTFC).
    fact: what it holds, in one sentence, in the user's own words.
    Both are required. This proposes the fact for approval — say it was NOTED
    FOR REVIEW; never say you have learned or will remember it. Do NOT use
    this for figures, policies or rules — those belong in the wiki, via
    resolve_policy_value."""
    name = (table or "").strip().upper()
    body = (fact or "").strip()
    if not name or not body:
        return {"error": "remember_record_fact needs both a table and a fact"}
    return _safe(memory.propose, text=f"{name}: {body}", kind="record",
                 source="taught in conversation")


@mcp.tool()
def what_do_we_know_about(table: str) -> dict:
    """What this site has TAUGHT the agent about a record, if anything. Use
    alongside describe_record and profile_record when a table is unfamiliar:
    the catalog says what columns exist, this says what the record is FOR."""
    return _safe(lambda: {
        "table": (table or "").strip().upper(),
        "taught": memory.record_facts(table),
        "note": ("Taught facts are pointers, not authority. Verify the shape "
                 "with describe_record and the contents with profile_record; "
                 "the live catalog wins any disagreement."),
    })


@mcp.tool()
def list_policy_terms() -> dict:
    """Which POLICY figures this agent can resolve from the wiki and bind into
    a query (capitalization threshold, approval limit, write-off limit,
    materiality, suspense clearing days, payment terms). Use with
    resolve_policy_value, or pass policy_binds to run_sql."""
    from .policy import list_terms

    return _safe(list_terms)


@mcp.tool()
def resolve_policy_value(policy: str) -> dict:
    """Look up a POLICY figure in the company wiki and return it with the exact
    sentence and page it came from. USE THIS instead of recalling a threshold
    from memory — a paraphrased figure silently changes which rows an answer
    contains. Returns status: 'resolved' (one figure, cite the source),
    'ambiguous' (the wiki disagrees with itself — quote every value and ask
    which applies), 'not_found' (say so; do not supply one yourself), or
    'demo_content' (bundled fictional pages — never filter real data with it).
    To filter a query by the value, prefer run_sql's policy_binds so the number
    is bound server-side rather than retyped."""
    # Reuse the engine's resolver rather than building one per call — a
    # fresh instance starts with an empty cache, so every ask would pay the
    # full wiki cost again.
    return _safe(engine._policy.resolve, policy=policy)


@mcp.tool()
def get_tree_node_accounts(node: str, tree_name: str = "",
                           business_unit: str = "") -> dict:
    """    Flatten a PeopleSoft TREE NODE into its concrete account list — the SET
    PRODUCER for cross-module chains. Trees attach account RANGES to nodes,
    so "the accounts in node X" cannot be answered by a join; this resolves
    the ranges against the account master. Use THIS when the account set
    feeds something else: journals for those accounts, budget rows, any
    run_sql — pass its result_id into run_sql's list_binds instead of
    retyping account numbers. For a trial balance BY node use
    rollup_trial_balance."""
    return _safe(report_runner.node_accounts, business_unit=business_unit,
                 tree_name=tree_name, node=node)


@mcp.tool()
def get_open_payables(business_unit: str = "", as_of_date: str = "") -> dict:
    """AP: what do we OWE. Open vouchers by vendor with totals, overdue and
    due-within-7-days amounts, oldest due date per vendor, and pipeline
    exceptions (vouchers in recycle or unposted — owed but invisible to a
    payment run). Use for "what do we owe / open payables / overdue AP /
    anything stuck in AP". This is the payables mirror of get_ar_aging."""
    return _safe(modules.open_payables, business_unit=business_unit,
                 as_of_date=as_of_date)


@mcp.tool()
def reconcile_ap_to_gl(
    control_accounts: str = "",
    business_unit: str = "",
    ledger: str = "",
    fiscal_year: int = 0,
    period: int = 0,
    as_of_date: str = "",
) -> dict:
    """AP/GL ACTIVITY RECONCILIATION: compare account-attributed AP
    accounting activity to posted GL journal activity for one business unit.

    control_accounts is a comma-separated Finance-approved list. If omitted,
    defaults.ap_control_accounts must be configured; no account is guessed.
    Empty fiscal_year/period resolves from as_of_date, or uses the latest
    posted GL period when no date is supplied.

    Read status/evaluated/ties before narrating a result. Evaluation requires
    an AP accounting-line source with account, GL business unit, base amount,
    posting/distribution status, and complete Journal Generator keys; those
    keys are matched to posted PS_JRNL_HEADER/PS_JRNL_LN lines. Missing fields,
    incomplete keys, mixed currencies, ambiguous rows, and safety caps fail
    closed. Voucher gross/payment arithmetic is never substituted. The result
    is signed selected-period activity (debits positive, credits negative),
    not the ending open-liability balance; use delivered APY1400/APY1405 for
    that separate control, and APY1410/APY1420 for delivered activity detail.
    Observed categories identify exceptions without inventing their cause."""
    return _safe(
        modules.reconcile_ap_to_gl,
        control_accounts=control_accounts,
        business_unit=business_unit,
        ledger=ledger,
        fiscal_year=fiscal_year,
        period=period,
        as_of_date=as_of_date,
    )


@mcp.tool()
def get_vendor_payments(business_unit: str = "", vendor: str = "",
                        months: int = 12, n: int = 20,
                        as_of_date: str = "") -> dict:
    """AP: whom did we PAY. Payments over a trailing window ranked by vendor
    — count, total, last payment date; voids excluded where the record
    carries a status. vendor filters by id or name substring; empty ranks
    all. Use for "how much did we pay X / top vendors by spend / when did we
    last pay X". A payment is made by a pay cycle, not by a business unit,
    so the unit is applied through the voucher cross-reference: check
    scoped_to_business_unit, and if it is false the totals cover the whole
    installation — say so rather than naming the unit."""
    return _safe(modules.vendor_payments, business_unit=business_unit,
                 vendor=vendor, months=months, n=n, as_of_date=as_of_date)


@mcp.tool()
def coupa_health() -> dict:
    """Coupa connectivity: mode (live vs bundled sample fixtures),
    reachability, latency. Call when Coupa data looks wrong or missing —
    fixture mode means COUPA_BASE_URL is not configured on this host."""
    return _safe(coupa.health)


@mcp.tool()
def get_coupa_invoices(status: str = "", supplier: str = "",
                       days: int = 30, max_rows: int = 50) -> dict:
    """Coupa invoices over a trailing window with per-currency totals.
    status filters the Coupa invoice-header status exactly (for example,
    approved, pending_approval, or draft). Coupa `paid` is a separate boolean,
    not a delivered header status;
    supplier matches by normalized name. Use for "what invoices are in
    Coupa / what did supplier X invoice us / Coupa invoice status". This
    is the PROCUREMENT side; the ledger's AP view is get_open_payables."""
    return _safe(coupa.invoices, status=status, supplier=supplier,
                 days=days, max_rows=max_rows)


@mcp.tool()
def get_coupa_stuck_approvals(days_pending: int = 3) -> dict:
    """Coupa invoices sitting in pending approval past a threshold, with
    the current approver and days pending, slowest first. Use for "what is
    stuck in approvals / why hasn't X been booked" — the close cannot book
    what approval is still holding."""
    return _safe(coupa.stuck_approvals, days_pending=days_pending)


@mcp.tool()
def get_coupa_rni(business_unit: str = "", as_of_date: str = "",
                  min_amount: float = 0.0, max_rows: int = 0) -> dict:
    """Coupa PO-line received-not-invoiced REVIEW CANDIDATES for one
    caller-authorized business unit. Reads fully paged receiving events and
    eligible invoice lines, keeps currencies separate, and discloses matching,
    scope, threshold, pagination and display limits. Current API status cannot
    reconstruct a historical cut-off, so that case returns incomplete. It also
    reports the bounded current Coupa ``exported`` flag and a governed export
    timestamp when valid. Those source flags do NOT prove export delivery or
    success, PeopleSoft receipt or booking, Journal Generator, posted GL
    activity, an ending GRNI liability, or what should be booked."""
    return _safe(
        coupa.received_not_invoiced,
        min_amount=min_amount,
        business_unit=business_unit,
        as_of_date=as_of_date,
        max_rows=(max_rows or None),
    )


@mcp.tool()
def get_coupa_supplier_spend(months: int = 12, top_n: int = 10) -> dict:
    """Coupa invoice spend ranked by supplier and currency over a trailing
    window. Use for "top suppliers by spend / how much have we bought from
    X". Procurement view; cash actually PAID is get_vendor_payments."""
    return _safe(coupa.supplier_spend, months=months, top_n=top_n)


@mcp.tool()
def get_coupa_budget_lines(period: str = "") -> dict:
    """Budget lines as the procurement system holds them: account
    segments, period, amount. Use when asked what the budget IS. period
    filters like "FY2026". Segment meanings are per-tenant configuration
    and are reported, never assumed."""
    return _safe(coupa.budget_lines, period=period)


@mcp.tool()
def coupa_budget_variance(business_unit: str = "", fiscal_year: int = 0,
                          period: int = 0, top: int = 25) -> dict:
    """RECONCILIATION: procurement-system BUDGET vs PeopleSoft ACTUALS,
    matched on natural account, year-to-date. Use for "budget vs actual /
    are we over budget / where are we overspending" AT SITES THAT BUDGET
    IN PROCUREMENT rather than in a ledger. Reports compared lines with
    variance and favourability, plus two separate lists that mean
    different things: unbudgeted spend (expense with no budget line) and
    unspent budget. Revenue is excluded — procurement budgets cover
    spend. If budgets live in a PeopleSoft ledger instead, use
    get_budget_variance."""
    return _safe(coupa.budget_variance, engine=engine,
                 business_unit=business_unit, fiscal_year=fiscal_year,
                 period=period, top=top)


@mcp.tool()
def coupa_to_ap_tie(days: int = 90) -> dict:
    """CURRENT DIAGNOSTIC: approved/paid Coupa invoices vs PS vouchers, matched
    server-side on invoice number with the supplier verified against the
    vendor master. Reports matched pairs, amount breaks (same invoice,
    different amount — the dangerous kind), and invoices that never reached
    AP. This path is not fully paged, same-BU proven, or selected-as-of
    complete, so it cannot certify period-end AP completeness or that every
    Coupa invoice reached PeopleSoft. Every displayed figure comes from this
    payload, but its conclusion remains incomplete until those controls are
    implemented."""
    return _safe(coupa.ap_tie, db=db, days=days)


@mcp.tool()
def get_duplicate_payments(business_unit: str = "", months: int = 12,
                           tolerance_days: int = 7,
                           as_of_date: str = "") -> dict:
    """AP audit: duplicate-voucher candidates and payment confirmation.
    Candidate lists cover the same invoice vouchered twice and same vendor +
    amount within a few days. `confirmed_duplicate_payments` is narrower: it
    requires two distinct non-void payment headers linked to two vouchers as
    of the requested date. Never call a voucher candidate "paid twice" unless
    that confirmed list says so. An empty result is a real answer."""
    return _safe(modules.duplicate_payments, business_unit=business_unit,
                 months=months, tolerance_days=tolerance_days,
                 as_of_date=as_of_date)


@mcp.tool()
def get_asset_register(business_unit: str = "", months: int = 12,
                       as_of_date: str = "") -> dict:
    """AM: what do we OWN. Asset cost by category with counts, plus this
    window's additions and retirements. COST basis — net book value needs
    the site's depreciation record and is never approximated. Use for "what
    assets do we have / what was added or retired / asset register"."""
    return _safe(modules.asset_register, business_unit=business_unit,
                 months=months, as_of_date=as_of_date)


@mcp.tool()
def run_ps_query(query: str, prompts: Optional[dict] = None,
                 private_owner: str = "", max_rows: int = 0) -> dict:
    """EXECUTE an existing PeopleSoft query (PSQuery) through the Query
    Access Service and return its rows. Use after search_ps_queries and
    describe_ps_query: pass the query name and a prompts map keyed by the
    BIND NAMES that describe_ps_query listed. Cite the query name in your
    answer — this is logic the site built and validated, which is stronger
    evidence than SQL we write. Results reflect the executing PeopleSoft
    user's permission lists and may legitimately differ from the curated
    tools, which read through a database account; say so if the figures
    disagree rather than treating it as an error."""
    return _safe(qas.execute, query=query, prompts=prompts or {},
                 private_owner=private_owner, max_rows=max_rows)


@mcp.tool()
def search_ps_queries(text: str = "", record: str = "",
                      include_private: bool = False, limit: int = 25) -> dict:
    """    Find EXISTING PeopleSoft queries (PSQuery) this site already built:
    "is there already a query for AP aging?", "what queries read
    PS_VOUCHER?" (pass record=). Returns names, descriptions,
    public/private, and run counts. Catalog read only — run_ps_query
    executes."""
    return _safe(query_catalog.search_queries, text=text, record=record,
                 include_private=include_private, limit=limit)


@mcp.tool()
def describe_ps_query(query: str, owner: str = "") -> dict:
    """One existing PSQuery in detail: its runtime PROMPTS in order with
    their types, and the records it reads. Use after search_ps_queries to
    judge whether a query answers the question and what values it would
    need. This reads the catalog only — it cannot execute the query."""
    return _safe(query_catalog.query_detail, query=query, owner=owner)


@mcp.tool()
def list_integration_endpoints() -> dict:
    """Discover this site's Integration Broker target location and its
    service operations from the IB catalog — so the gateway URL comes
    from the database rather than from configuration. Every operation is
    listed with whether it is CALLABLE: only read-only query operations
    are on the invocation allowlist, because IB also carries voucher
    builds and journal loads. Use for "what APIs does this instance
    expose / can we call PeopleSoft directly"."""
    return _safe(query_catalog.integration_endpoints)


@mcp.tool()
def get_project_costs(business_unit: str = "", project: str = "",
                      stale_after_months: int = 3,
                      as_of_date: str = "") -> dict:
    """PC: every project's ACTUALS against its BUDGET with the two flags a
    reviewer scans for first — over_budget and stale (budget remaining but
    no recent activity). Handles the PS_PROJ_RESOURCE ANALYSIS_TYPE split
    (BUD* rows are budget, the rest actuals) server-side. pct_used is null
    when a project has no budget row — unbudgeted, not 0%. Use for "project
    spend / budget vs actual / which projects are over or dormant"."""
    return _safe(modules.project_costs, business_unit=business_unit,
                 project=project, stale_after_months=stale_after_months,
                 as_of_date=as_of_date)

if wiki is not None:

    @mcp.tool()
    def wiki_health() -> dict:
        """Is the company wiki actually connected, and is it serving REAL pages?
        Reports the active provider, auth mode, space/label scoping, and — most
        importantly — whether the bundled fictional demo pages are being served
        instead of Confluence. Call this whenever the user doubts a policy
        answer, and NEVER cite a policy figure as authoritative when
        is_bundled_demo_content is true."""
        try:
            return wiki.health()
        except Exception as e:
            return {"error": f"wiki_health failed: {e}"}

    @mcp.tool()
    def wiki_lookup(question: str, max_pages: int = 3,
                    max_passages: int = 6) -> dict:
        """READ the wiki: searches, fetches the top pages, and returns the actual
                PASSAGES that answer the question. USE THIS for any
                policy/process/threshold/"why" question — wiki_search alone returns
                links, and an answer built from titles is a guess."""
        try:
            return wiki_mod.lookup(wiki, question, max_pages=max_pages,
                                   max_passages=max_passages)
        except Exception as e:
            return {"error": f"wiki_lookup failed: {e}"}

    @mcp.tool()
    def wiki_search(query: str, limit: int = 5) -> dict:
        """Find candidate wiki pages (title, snippet, URL). This returns
        POINTERS, not full content — for an actual answer call wiki_lookup,
        which returns the passages. Never answer a policy question from these
        titles/links alone."""
        try:
            hits = wiki.search(query, limit)
            for h in hits:
                h.pop("text", None)  # keep search light; wiki_lookup reads pages
            out = {"provider": wiki.provider_name, "results": hits,
                   "next_step": "Call wiki_lookup(question=...) to read the "
                                "actual passages before answering."}
            if getattr(wiki, "provider_name", "") == "localdocs":
                h = wiki.health()
                if h.get("is_bundled_demo_content"):
                    out["demo_content_warning"] = (
                        "These are the repo's FICTIONAL sample policy pages, not "
                        "your company wiki. Do not present any figure from them "
                        "as company policy — say the wiki is not connected."
                    )
            return out
        except Exception as e:
            return {"error": f"wiki_search failed: {e}"}

    @mcp.tool()
    def wiki_get_page(page_id: str, offset: int = 0, max_chars: int = 0) -> dict:
        """READ a whole wiki page — the tool for TECHNICAL specs and KB articles.
                For "how does X work" / "how do I" questions, find the page
                (wiki_search or wiki_lookup sources) and read it ALL before acting.
                page_id: the id from wiki_search results or wiki_lookup sources.
                Long pages arrive in slices: when the result carries next_offset, call
                again with offset=next_offset until it is absent."""
        try:
            page = wiki.get_page(page_id)
        except Exception as e:
            return {"error": f"wiki_get_page failed: {e}"}
        text = str(page.get("text") or "")
        # Default slice stays inside the chat client's tool-result budget
        # (24k chars on the local provider) with room for the JSON wrapper —
        # otherwise a long spec is truncated mid-sentence with no way to ask
        # for the rest.
        cap = max(500, min(int(max_chars or 18_000), 100_000))
        start = max(0, int(offset or 0))
        if start >= len(text) and start:
            return {"error": (
                f"offset {start} is past the end of this page "
                f"({len(text)} chars). Re-read from offset 0.")}
        page["text"] = text[start:start + cap]
        page["length"] = len(text)
        page["offset"] = start
        if start + cap < len(text):
            page["next_offset"] = start + cap
            page["note"] = (
                f"PARTIAL: characters {start}-{start + cap} of {len(text)}. "
                f"Call wiki_get_page again with offset={start + cap} to "
                "continue — do not act on a spec you have only partly read.")
        return page


def main() -> None:
    print(
        f"[pstb] MCP server starting — db={cfg.db.backend}"
        f"{' (views)' if cfg.db.use_views else ''}, "
        f"wiki={getattr(wiki, 'provider_name', 'off')}, "
        f"raw_sql={'on' if cfg.tools.allow_raw_sql else 'off'}",
        file=sys.stderr,
    )
    mcp.run()


if __name__ == "__main__":
    main()
