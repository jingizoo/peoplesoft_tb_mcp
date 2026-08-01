"""Orchestrates the record port: discover -> plan -> emit -> verify -> reconcile.

Connections come from the existing SourceRegistry, so the 9.1 source and the
9.2 target are ordinary `sources:` entries with the same guarded, SELECT-only
Database stack every other tool uses. The pipeline owns no credentials and
opens no write path.
"""
from __future__ import annotations

from ..config import Config
from ..sources import SourceRegistry
from . import compare, reconcile as recon
from .catalog import RecordCatalog
from .closure import expand
from .emit import emit_all
from .spec import MigrateError
from .state import MigrateState


class MigratePipeline:
    def __init__(self, cfg: Config, registry: SourceRegistry):
        m = cfg.migrate
        if not m.source:
            raise MigrateError(
                "migrate.source is not set. Add the 9.1 database under "
                "`sources:` in config.yaml and point migrate.source at it "
                "(credentials via PSTB_SRC_<NAME>_DSN/USER/PASSWORD in .env).")
        self.cfg = cfg
        prefixes = list(m.custom_prefixes)
        self.source = RecordCatalog(registry.get(m.source), prefixes)
        self.target = RecordCatalog(registry.get(m.target or "default"), prefixes)
        self.state = MigrateState(cfg.resolve_path(m.state_path))

    # ---- steps -----------------------------------------------------------
    def discover(self, mode: str = "", limit: int = 0) -> dict:
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
                                    provenance[recname])
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

    def emit(self, out_dir: str = "") -> dict:
        items = self.state.items()
        if not items:
            raise MigrateError("Plan is empty — run plan before emit.")
        target = self.cfg.resolve_path(out_dir or self.cfg.migrate.out_dir)
        return emit_all(items, target, self.source)

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
        results = recon.reconcile(items, self.source, self.target, recnames)
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
