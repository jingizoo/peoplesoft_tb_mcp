"""Orchestrates the record port: discover -> plan -> emit -> verify -> reconcile.

Connections come from the existing SourceRegistry, so the 9.1 source and the
9.2 target are ordinary `sources:` entries with the same guarded, SELECT-only
Database stack every other tool uses. The pipeline owns no credentials and
opens no write path.
"""
from __future__ import annotations

import json

from ..config import Config
from ..sources import SourceRegistry
from . import compare, preflight as pre, reconcile as recon
from .catalog import RecordCatalog
from .closure import expand
from .convert import build_mapping, load_overrides
from .emit import emit_all
from .spec import DATA_MAPPED_SQL, MigrateError
from .state import MigrateState


class MigratePipeline:
    def __init__(self, cfg: Config, registry: SourceRegistry):
        m = cfg.migrate
        if not m.source:
            raise MigrateError(
                "migrate.source is not set. Add the 9.1 database under "
                "`sources:` in config.yaml and point migrate.source at it "
                "(credentials via PSTB_SRC_<NAME>_DSN/USER/PASSWORD in .env).")
        if m.delivered_data not in ("skip", "convert"):
            raise MigrateError(
                f"migrate.delivered_data must be skip|convert, got "
                f"{m.delivered_data!r}")
        self.cfg = cfg
        prefixes = list(m.custom_prefixes)
        self.source = RecordCatalog(registry.get(m.source), prefixes)
        self.target = RecordCatalog(registry.get(m.target or "default"), prefixes)
        self.state = MigrateState(cfg.resolve_path(m.state_path))
        self.overrides_path = cfg.resolve_path(m.mapping_overrides)

    # ---- steps -----------------------------------------------------------
    def discover(self, mode: str = "", limit: int = 0,
                 delivered_like: str = "") -> dict:
        """Custom records by prefix/oprid, or — with delivered_like — the
        delivered records matching a name pattern, which is how a
        reimplementation names the delivered tables it has to carry."""
        if delivered_like:
            rows = self.source.discover_delivered(delivered_like, limit=limit)
            truncated = any(r.get("truncated") for r in rows)
            rows = [r for r in rows if r.get("recname")]
            return {"mode": "delivered", "pattern": delivered_like.upper(),
                    "count": len(rows), "truncated": truncated,
                    "records": rows,
                    "note": "Seed these into plan to convert their data. "
                            "Requires migrate.delivered_data = convert."
                            if self.cfg.migrate.delivered_data != "convert"
                            else "migrate.delivered_data = convert is active."}
        mode = mode or self.cfg.migrate.discovery
        rows = self.source.discover_custom(mode=mode, limit=limit)
        truncated = any(r.get("truncated") for r in rows)
        rows = [r for r in rows if r.get("recname")]
        return {"mode": mode, "count": len(rows), "truncated": truncated,
                "records": rows}

    def plan(self, seed_records: list | None = None, mode: str = "") -> dict:
        """Closure + classification, persisted to the state db. Seeds default
        to every discovered custom record; pass an explicit list to port a
        subsystem at a time."""
        if seed_records:
            seeds = [s.upper().strip() for s in seed_records if s.strip()]
        else:
            seeds = [r["recname"] for r in self.discover(mode=mode)["records"]]
        if not seeds:
            raise MigrateError("Nothing to plan: discovery found no custom "
                               "records and no seeds were given.")
        provenance = expand(self.source, seeds,
                            max_records=self.cfg.migrate.max_records)
        items = []
        for recname in sorted(provenance):
            item = compare.classify(self.source, self.target, recname,
                                    provenance[recname],
                                    delivered_data=self.cfg.migrate.delivered_data)
            if "seed" in item.via and not self.source.is_custom(recname):
                item.notes.append(
                    "Seeded (oprid discovery or explicit) but does not match "
                    "migrate.custom_prefixes, so it is classified as "
                    "delivered. If this is really a custom record with a "
                    "non-standard name, add its prefix and replan.")
            if item.data_plan != "none":
                rec = self.source.record(recname)
                if rec is not None and rec.has_table:
                    try:
                        item.row_count = self.source.table_row_count(rec.table_name)
                    except Exception as e:
                        item.notes.append(f"source row count unavailable: {e}")
            items.append(item)
        self.state.upsert_plan(items)
        summary: dict = {}
        for it in items:
            summary[it.classification] = summary.get(it.classification, 0) + 1
        return {"seeds": len(seeds), "records": len(items),
                "by_classification": dict(sorted(summary.items())),
                "items": [i.to_dict() for i in items]}

    def show_record(self, recname: str) -> dict:
        """Both shapes side by side — what the model (or a human) needs to
        reason about one record's drift or dependencies."""
        src = self.source.record(recname)
        tgt = self.target.record(recname)
        if src is None and tgt is None:
            raise MigrateError(f"{recname} not found in either instance.")

        def shape(rec):
            if rec is None:
                return None
            return {
                "rectype": rec.rectype_name,
                "table": rec.table_name if rec.has_table else None,
                "descr": rec.descr,
                "lastupdoprid": rec.lastupdoprid,
                "audit_recname": rec.audit_recname or None,
                "rellang_recname": rec.rellang_recname or None,
                "subrecords": rec.subrecords,
                "fields": [{
                    "name": f.fieldname, "type": f.type_name,
                    "length": f.length, "decimals": f.decimalpos,
                    "key": f.is_key, "edittable": f.edittable or None,
                    "from_subrecord": f.from_subrecord or None,
                } for f in rec.fields],
            }

        out = {"recname": recname.upper(), "source_91": shape(src),
               "target_92": shape(tgt)}
        if src is not None and tgt is not None:
            out["shape_diff"] = compare.shape_diff(src, tgt).to_dict()
        if src is not None and src.rectype in (1, 5, 6):
            text = self.source.view_sql(recname)
            if text:
                out["view_sql"] = text[:4000]
        try:
            out["state"] = self.state.get(recname)
        except MigrateError:
            pass
        return out

    # ---- column mapping --------------------------------------------------
    def _mappings(self, items: list, recnames: list | None = None) -> dict:
        """Resolved mappings for every record whose data needs reshaping.

        Rebuilt from live metadata plus the overrides file on each call, so an
        edited override takes effect without a replan — the plan records
        classification, the overrides file records intent.
        """
        overrides = load_overrides(self.overrides_path)
        wanted = {r.upper() for r in recnames} if recnames else None
        out: dict = {}
        for it in items:
            if it.data_plan != DATA_MAPPED_SQL:
                continue
            if wanted is not None and it.recname not in wanted:
                continue
            src = self.source.record(it.recname)
            tgt = self.target.record(it.recname)
            if src is None or tgt is None:
                continue
            out[it.recname] = build_mapping(it.recname, src, tgt, overrides)
        return out

    def mapping(self, recname: str = "") -> dict:
        """Column-by-column resolution for records whose shape differs:
        where every 9.2 column gets its value, what that costs, which 9.1
        columns are dropped, and rename candidates for unmapped columns."""
        items = self.state.items()
        if not items:
            raise MigrateError("Plan is empty — run plan before mapping.")
        maps = self._mappings(items, [recname] if recname else None)
        if recname and not maps:
            raise MigrateError(
                f"{recname} has no mapped-SQL data plan. Only records whose "
                "shape differs between releases are mapped (drift_review, or "
                "delivered_convert with a shape change).")
        return {"overrides_file": str(self.overrides_path),
                "overrides_present": self.overrides_path.exists(),
                "count": len(maps),
                "mappings": {k: v.to_dict() for k, v in sorted(maps.items())}}

    def mapping_template(self, recname: str = "", overwrite: bool = False) -> dict:
        """Write a starter overrides file listing every unresolved column, so
        a human (or the model) edits real decisions rather than inventing the
        file's shape. Never clobbers existing overrides unless asked."""
        items = self.state.items()
        maps = self._mappings(items, [recname] if recname else None)
        if not maps:
            raise MigrateError("No records need a mapping — nothing to template.")
        existing = load_overrides(self.overrides_path)
        if existing and not overwrite:
            raise MigrateError(
                f"{self.overrides_path} already exists. Edit it, or pass "
                "overwrite to regenerate (your edits would be lost).")
        template: dict = {}
        for name, m in sorted(maps.items()):
            cols: dict = {}
            for c in m.columns:
                if c.kind != "defaulted":
                    continue
                cands = m.rename_suggestions.get(c.target_column) or []
                hint = (f"9.2-only. Rename candidates: "
                        + ", ".join(x["source_column"] for x in cands)
                        if cands else
                        "9.2-only, no rename candidate — set a default or expr.")
                cols[c.target_column] = {"default": c.source_expr,
                                         "_comment": hint}
            entry: dict = {"_dropped_9_1_columns": m.dropped_source,
                           "where": ""}
            if cols:
                entry["columns"] = cols
            template[name] = entry
        self.overrides_path.parent.mkdir(parents=True, exist_ok=True)
        self.overrides_path.write_text(json.dumps(template, indent=2) + "\n")
        return {"written": str(self.overrides_path), "records": len(template),
                "next": "Edit the file: replace a 'default' with "
                        "{\"from\": \"SRC_COL\"} for renames, or "
                        "{\"expr\": \"...\"} for conversions; set 'where' to "
                        "filter rows. Then re-run mapping and preflight."}

    def preflight(self, recnames: list | None = None) -> dict:
        """Count, on the real 9.1 data, the rows each mapping would truncate,
        round, overflow, or collide on the 9.2 key. Read-only; run before any
        conversion script."""
        items = self.state.items()
        if not items:
            raise MigrateError("Plan is empty — run plan before preflight.")
        maps = self._mappings(items, recnames)
        if not maps:
            return {"checked": 0, "results": [],
                    "note": "No records need reshaping — nothing to pre-flight."}
        results = []
        for name, m in sorted(maps.items()):
            src, tgt = self.source.record(name), self.target.record(name)
            if src is None or tgt is None:
                results.append({"recname": name, "ok": False,
                                "error": "definition unavailable"})
                continue
            r = pre.run(m, src, tgt, self.source)
            r["mapping_risks"] = m.risk_counts()
            # A measurable risk that measured zero is cleared by the data;
            # only blockers the probes cannot settle still stop the load.
            unmeasured = m.unmeasured_blockers()
            if unmeasured:
                r["blocking"] = True
                r["unmeasured_blockers"] = [{"code": c, "message": msg}
                                            for _, c, msg in unmeasured]
                r["ok"] = False
            results.append(r)
        blocking = [r for r in results if r.get("blocking")]
        for r in results:
            if r.get("blocking"):
                self.state.set_status(
                    r["recname"], "blocked",
                    r.get("summary", "pre-flight found blocking risks"))
        return {"checked": len(results),
                "clean": len([r for r in results if r.get("ok")]),
                "blocking": len(blocking), "results": results}

    def emit(self, out_dir: str = "") -> dict:
        items = self.state.items()
        if not items:
            raise MigrateError("Plan is empty — run plan before emit.")
        target = self.cfg.resolve_path(out_dir or self.cfg.migrate.out_dir)
        m = self.cfg.migrate
        return emit_all(items, target, self.source,
                        mappings=self._mappings(items),
                        convert_via=m.convert_via, dblink=m.dblink_name,
                        staging_prefix=m.staging_prefix)

    def verify_build(self) -> dict:
        items = self.state.items()
        if not items:
            raise MigrateError("Plan is empty — run plan before verify-build.")
        results = recon.verify_build(items, self.source, self.target)
        for r in results:
            if r.get("ok") and r.get("table"):
                self.state.set_status(r["recname"], "built_verified")
        ok = [r for r in results if r.get("ok")]
        return {"checked": len(results), "ok": len(ok),
                "failed": len(results) - len(ok), "results": results}

    def reconcile(self, recnames: list | None = None) -> dict:
        items = self.state.items()
        if not items:
            raise MigrateError("Plan is empty — run plan before reconcile.")
        results = recon.reconcile(items, self.source, self.target, recnames,
                                  mappings=self._mappings(items, recnames))
        for r in results:
            if r.get("ok"):
                self.state.set_status(r["recname"], "reconciled")
        ok = [r for r in results if r.get("ok")]
        return {"checked": len(results), "ok": len(ok),
                "failed": len(results) - len(ok), "results": results}

    def mark(self, recname: str, status: str, note: str = "") -> dict:
        """Record a manual step (project copied, Build run, import executed)
        so state reflects work done in App Designer / Data Mover."""
        return self.state.set_status(recname, status, note)

    def status(self) -> dict:
        return self.state.summary()
