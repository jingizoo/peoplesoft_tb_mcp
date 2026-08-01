"""Classify each record in the closure against the 9.2 target.

The classification decides everything downstream: whether the definition goes
into the App Designer project, whether data moves by Data Mover or by a
reviewed mapping script, and what an operator must resolve by hand. Shape
comparison uses the EXPANDED field list (subrecords spliced in), because that
is what the physical table is built from — two records can differ at the
subrecord level while every direct field matches.
"""
from __future__ import annotations

from .catalog import RecordCatalog
from .spec import (ALREADY_PRESENT, BUILD_AND_LOAD, BUILD_DEFINITION,
                   DATA_DMS, DATA_MAPPED_SQL, DATA_NONE, DELIVERED_MISSING,
                   DELIVERED_OK, DRIFT_REVIEW, LOAD_ONLY, PlanItem, RecordDef,
                   ShapeDiff, UNKNOWN_SOURCE)


def shape_diff(src: RecordDef, tgt: RecordDef) -> ShapeDiff:
    d = ShapeDiff()
    smap, tmap = src.field_map(), tgt.field_map()
    for name in smap:
        if name not in tmap:
            d.source_only.append(name)
    for name in tmap:
        if name not in smap:
            d.target_only.append(name)
    for name, sf in smap.items():
        tf = tmap.get(name)
        if tf is None or sf.shape() == tf.shape():
            continue
        bits = []
        if sf.fieldtype != tf.fieldtype:
            bits.append(f"type {sf.type_name} -> {tf.type_name}")
        if sf.length != tf.length:
            bits.append(f"length {sf.length} -> {tf.length}")
        if sf.decimalpos != tf.decimalpos:
            bits.append(f"decimals {sf.decimalpos} -> {tf.decimalpos}")
        if sf.is_key != tf.is_key:
            bits.append(f"key {sf.is_key} -> {tf.is_key}")
        d.changed.append(f"{name}: " + ", ".join(bits))
    d.source_only.sort()
    d.target_only.sort()
    d.changed.sort()
    return d


def classify(source: RecordCatalog, target: RecordCatalog, recname: str,
             via: list) -> PlanItem:
    src = source.record(recname)
    if src is None:
        return PlanItem(recname=recname, rectype=-1,
                        classification=UNKNOWN_SOURCE, data_plan=DATA_NONE,
                        via=via,
                        notes=["Referenced but not found in the 9.1 metadata "
                               "— likely a dynamic prompt or a dropped "
                               "record. Resolve or ignore explicitly."])
    custom = source.is_custom(recname)
    tgt = target.record(recname)

    if not custom:
        if tgt is not None:
            return PlanItem(recname=recname, rectype=src.rectype,
                            classification=DELIVERED_OK, data_plan=DATA_NONE,
                            via=via)
        return PlanItem(recname=recname, rectype=src.rectype,
                        classification=DELIVERED_MISSING, data_plan=DATA_NONE,
                        via=via,
                        notes=["Delivered in 9.1 but absent in 9.2 — renamed, "
                               "consolidated, or its module is not installed. "
                               "The custom object depending on it must be "
                               "retargeted; never copy delivered objects "
                               "forward."])

    if tgt is None:
        if src.has_table:
            item = PlanItem(recname=recname, rectype=src.rectype,
                            classification=BUILD_AND_LOAD, data_plan=DATA_DMS,
                            via=via)
        else:
            item = PlanItem(recname=recname, rectype=src.rectype,
                            classification=BUILD_DEFINITION,
                            data_plan=DATA_NONE, via=via)
        if src.rectype == 7:
            item.notes.append("Temporary table: port the definition; instance "
                              "data is batch scratch and is not copied.")
        return item

    diff = shape_diff(src, tgt)
    if diff.empty():
        if src.has_table:
            return PlanItem(recname=recname, rectype=src.rectype,
                            classification=LOAD_ONLY, data_plan=DATA_DMS,
                            via=via)
        return PlanItem(recname=recname, rectype=src.rectype,
                        classification=ALREADY_PRESENT, data_plan=DATA_NONE,
                        via=via)
    return PlanItem(
        recname=recname, rectype=src.rectype, classification=DRIFT_REVIEW,
        data_plan=DATA_MAPPED_SQL if src.has_table else DATA_NONE,
        via=via, shape_diff=diff.to_dict(),
        notes=["Exists in both instances with a different shape. Merge the "
               "definition in App Designer, then load data with the emitted "
               "mapping script after review — a straight Data Mover import "
               "would fail or silently misalign."])
