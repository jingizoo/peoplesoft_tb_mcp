"""Which objects are worth asking a human to name, and which are not.

Phase 7's own design review answered "should the model draft a meaning"
with a measurement, not an opinion: the vocabulary harvest yields zero
terms on most objects, and a drafted sentence with no grounded wording
behind it cannot be told apart from a hallucination by anything in the
approval store. So this module does not draft. It reads the same evidence
a human would (metadata.object_evidence), and sorts every object into a
bucket: worth a person's attention now, or refused with the reason named.

The refusal buckets ARE the deliverable. An object with rich view
vocabulary is one a human can already decode by reading the view -- this
worklist exists to point at the objects that cannot be decoded that way,
so a person spends their time where the catalog cannot help them alone.

Founding vs corroborating, deliberately kept separate: a declared foreign
key or a mined join tells you a RELATIONSHIP exists, never what either
side of it MEANS. Only three things found a meaning -- a record label
that says something the physical name did not already say, real view
vocabulary, or a neighbour whose own meaning is already approved. Nothing
else earns a worklist row, however much relationship evidence surrounds
it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

_DECASE = re.compile(r"[^A-Z0-9]")
MIN_VOCABULARY_TERMS = 3

BUCKETS = frozenset({
    "eligible", "no_human_wording", "empty", "profiler_silent",
    "already_spoken_for", "not_found", "ambiguous", "wrong_source",
    "source_error", "not_an_object",
})


def _decased_echo(label: str, object_name: str) -> bool:
    """True when a 'label' says nothing a de-cased physical name didn't.

    App Designer requires a record description, so many are a rubber
    stamp: TU_X7 described as "Tu X7". That satisfies no founding signal
    -- it is the same fact spelled differently, not new information.
    """
    norm_label = _DECASE.sub("", str(label or "").upper())
    norm_object = _DECASE.sub("", str(object_name or "").upper())
    return bool(norm_label) and norm_label == norm_object


@dataclass
class Classification:
    bucket: str
    founding_signals: set = field(default_factory=set)
    corroborating_signals: set = field(default_factory=set)
    detail: str = ""
    evidence: dict | None = None


def founding_signals(evidence: dict) -> set:
    """S1 (label) and S2 (vocabulary) -- the two signals visible on the
    object's OWN evidence, without consulting the approval store."""
    signals = set()
    label = evidence.get("label")
    object_name = str(evidence.get("object") or "")
    if label and not _decased_echo(label, object_name):
        signals.add("S1_record_label")
    vocabulary = evidence.get("view_vocabulary") or []
    distinct_terms = {str(v.get("means") or "") for v in vocabulary
                      if v.get("means")}
    if len(distinct_terms) >= MIN_VOCABULARY_TERMS:
        signals.add("S2_view_vocabulary")
    return signals


def neighbour_ids(evidence: dict) -> set:
    """Object ids of every foreign-key/view-declared-join neighbour --
    the only two relationship classes that carry INTENT, and therefore
    the only ones checked for S3. A mined join is a measurement, not
    someone's assertion that these objects are related; it corroborates
    nothing about what either side means."""
    ids = set()
    for hop in (evidence.get("declared_foreign_keys") or ()):
        if hop.get("with_object_id"):
            ids.add(str(hop["with_object_id"]))
    for hop in (evidence.get("view_declared_joins") or ()):
        if hop.get("with_object_id"):
            ids.add(str(hop["with_object_id"]))
    return ids


def classify_object(evidence: dict, *, already_proposed: bool,
                    approved_neighbour: Callable[[str], bool]) -> Classification:
    """One object's evidence packet -> which bucket it belongs in.

    ``already_proposed``: True if ANY proposal exists for this object_id
    in this source, in ANY status -- approved, excluded, pending,
    rejected, revoked. There is no per-object uniqueness in the store and
    no bulk-decide in the UI, so re-listing a decided object trains an
    operator to stop reading the worklist.
    ``approved_neighbour``: called with a neighbour's object_id; True if
    that neighbour already has an approved meaning (S3).
    """
    if not evidence.get("available"):
        return Classification(bucket="source_error",
                              detail=evidence.get("detail") or "")
    if not evidence.get("found"):
        bucket = evidence.get("bucket") or "not_found"
        if bucket not in BUCKETS:
            bucket = "not_found"
        return Classification(bucket=bucket,
                              detail=evidence.get("detail") or "")
    if already_proposed:
        return Classification(bucket="already_spoken_for", evidence=evidence)

    liveness = evidence.get("liveness")
    branch = evidence.get("caveat_branch")
    if liveness == "empty" and branch == "verified_empty_current":
        # Measured current and current-emptiness confirmed: nothing to
        # name. `demand.coverage_gaps` refuses the same objects for the
        # same reason.
        return Classification(bucket="empty", evidence=evidence)
    if evidence.get("profiler_status") == "silent":
        return Classification(bucket="profiler_silent", evidence=evidence)

    founding = founding_signals(evidence)
    corroborating = set()
    if evidence.get("declared_foreign_keys"):
        corroborating.add("declared_foreign_key")
    if evidence.get("mined_joins"):
        corroborating.add("mined_join")
    for neighbour_id in neighbour_ids(evidence):
        if approved_neighbour(neighbour_id):
            founding.add("S3_approved_neighbour")
            break

    if not founding:
        return Classification(bucket="no_human_wording",
                              corroborating_signals=corroborating,
                              evidence=evidence)
    return Classification(bucket="eligible", founding_signals=founding,
                          corroborating_signals=corroborating,
                          evidence=evidence)


def iter_catalog_objects(con, source: str):
    """(node_id, schema, name, kind) for every table/view in one source,
    in a stable order -- the worklist must be reproducible run to run."""
    for row in con.execute(
            "SELECT id, schema_name, name, kind FROM nodes "
            "WHERE source=? AND kind IN ('table','view') "
            "ORDER BY schema_name, name, kind, id", (source,)):
        yield row["id"], row["schema_name"], row["name"], row["kind"]


def build_worklist(catalog, store, source: str) -> dict:
    """The full run: every object in one source, bucketed, with counts.

    Read-only end to end. No proposal is ever written here -- that is
    the entire point of shipping this before any drafter exists.
    """
    con = catalog._open()  # noqa: SLF001 -- this module is the catalog's
    try:                   # own worklist, not an external consumer.
        has_profiles = bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='object_profiles'").fetchone())
        objects = list(iter_catalog_objects(con, source))
    finally:
        con.close()

    if not has_profiles and objects:
        # Every object would otherwise report profiler_status='silent'
        # one at a time, which reads as "this schema has no profiling" --
        # a fact about the artifact, not the schema. A missing table is
        # a build defect and must fail the run, not degrade quietly into
        # a census that looks complete.
        raise RuntimeError(
            "the metadata catalog has no object_profiles table; rebuild "
            "it before running the worklist -- every object would "
            "otherwise be reported profiler_silent, which would look "
            "like a real finding instead of a broken artifact")

    proposed_ids: set = set()
    approved_ids: set = set()
    for row in (store.list_proposals("") or ()):
        object_id = str(row.get("object_id") or "")
        if object_id:
            proposed_ids.add(object_id)
            if row.get("status") == "approved":
                approved_ids.add(object_id)

    rows = []
    counts: dict = {}
    for node_id, schema, name, kind in objects:
        identifier = f"{schema}.{name}" if schema else name
        evidence = catalog.object_evidence(identifier, source=source)
        classification = classify_object(
            evidence, already_proposed=node_id in proposed_ids,
            approved_neighbour=lambda oid: oid in approved_ids)
        counts[classification.bucket] = counts.get(
            classification.bucket, 0) + 1
        rows.append({
            "object_id": node_id, "schema": schema, "object": name,
            "kind": kind, "bucket": classification.bucket,
            "founding_signals": sorted(classification.founding_signals),
            "corroborating_signals": sorted(
                classification.corroborating_signals),
            "detail": classification.detail,
        })

    return {
        "source": source, "total": len(objects), "counts": counts,
        "rows": rows,
        "notes": catalog._evidence_notes(source),  # noqa: SLF001
        "coverage_note": (
            "A refusal here is not an error -- it is the finding. An "
            "object in no_human_wording has no view, no meaningful "
            "label and no approved neighbour; a person is the only "
            "remaining source of its meaning."),
    }


def _format_report(result: dict) -> str:
    lines = [
        f"source: {result['source']}",
        f"objects: {result['total']}",
        "",
        "buckets:",
    ]
    for bucket in sorted(result["counts"]):
        lines.append(f"  {bucket:<20} {result['counts'][bucket]}")
    lines.append("")
    for layer in ("view_vocabulary", "value_joins"):
        note = result["notes"].get(layer)
        lines.append(f"{layer}: {note or '(no note recorded)'}")
    lines.append("")
    eligible = [r for r in result["rows"] if r["bucket"] == "eligible"]
    lines.append(f"eligible for a person to write a meaning: "
                f"{len(eligible)}")
    for row in eligible[:200]:
        identifier = (f"{row['schema']}.{row['object']}" if row["schema"]
                     else row["object"])
        signals = ",".join(row["founding_signals"]) or "(none)"
        lines.append(f"  {identifier:<40} founded by: {signals}")
    if len(eligible) > 200:
        lines.append(f"  ... and {len(eligible) - 200} more")
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    import sys
    from pathlib import Path

    from .config import load_config
    from .metadata import MetadataCatalog, source_catalog_path, source_fingerprint
    from .source_knowledge import SourceKnowledge, source_knowledge_path
    from .sources import SourceRegistry
    from .db import Database

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[2])
    parser.add_argument("--source", default="default")
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    cfg = load_config(args.config) if args.config else load_config()
    registry = SourceRegistry(cfg, Database(cfg))
    canonical = registry.resolve_name(args.source)
    if canonical not in registry.names():
        print(f"unknown source {args.source!r}; choose one of "
              f"{', '.join(registry.names())}", file=sys.stderr)
        return 2

    catalog = MetadataCatalog(
        source_catalog_path(cfg, canonical), source=canonical,
        expected_fingerprint=source_fingerprint(cfg, canonical))
    if not catalog.available():
        print(f"no readable metadata catalog for {canonical!r}; build it "
              "first with scripts/build_metadata_catalog.py",
              file=sys.stderr)
        return 2
    store = SourceKnowledge(
        source_knowledge_path(cfg, canonical), source=canonical,
        source_fingerprint=source_fingerprint(cfg, canonical))

    try:
        result = build_worklist(catalog, store, canonical)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(_format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
