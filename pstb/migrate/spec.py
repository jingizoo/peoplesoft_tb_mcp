"""Shared vocabulary for the 9.1 -> 9.2 record-porting pipeline.

PeopleSoft "code" for records is rows in the PeopleTools tables (PSRECDEFN,
PSRECFIELD, PSDBFIELD ...), so the pipeline reads metadata with plain SELECTs
on both instances and never writes either database. Everything that changes
the 9.2 target goes through the delivered tools — Application Designer for
definitions and Build, Data Mover for data — from artifacts this pipeline
emits and reconciles afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class MigrateError(RuntimeError):
    pass


# PSRECDEFN.RECTYPE
RECTYPE_NAMES = {
    0: "SQL Table",
    1: "SQL View",
    2: "Derived/Work",
    3: "Subrecord",
    5: "Dynamic View",
    6: "Query View",
    7: "Temporary Table",
}

# PSDBFIELD.FIELDTYPE
FIELDTYPE_NAMES = {
    0: "Character",
    1: "Long Character",
    2: "Number",
    3: "Signed Number",
    4: "Date",
    5: "Time",
    6: "DateTime",
    8: "Image",
    9: "Image Reference",
}

# PSRECFIELD.USEEDIT is a bitmask; bit 1 (Key) is the only bit the pipeline
# interprets. The raw mask is carried through for humans and the model.
USEEDIT_KEY = 1

# Classifications — one per record in the plan.
BUILD_AND_LOAD = "build_and_load"          # custom table, absent in 9.2: build + copy data
BUILD_DEFINITION = "build_definition"      # custom view/subrecord/work/temp, absent in 9.2
LOAD_ONLY = "load_only"                    # custom table already in 9.2, same shape: copy data
ALREADY_PRESENT = "already_present"        # custom non-table already in 9.2, same shape
DRIFT_REVIEW = "drift_review"              # custom, in both, shapes differ: human/LLM merge
DELIVERED_OK = "delivered_ok"              # delivered dependency, present in 9.2: nothing to do
DELIVERED_MISSING = "delivered_missing"    # delivered dependency ABSENT in 9.2: resolve first
UNKNOWN_SOURCE = "unknown_source"          # referenced but not found in 9.1 metadata

# Data movement per record: straight Data Mover copy, a reviewed mapping
# script, or nothing (views/subrecords/work records carry no data; delivered
# records must NEVER have data copied across releases).
DATA_DMS = "dms"
DATA_MAPPED_SQL = "mapped_sql"
DATA_NONE = "none"

# Record lifecycle in the state db.
STATUSES = (
    "planned",
    "definitions_exported",   # in the App Designer project, copied to 9.2
    "built_verified",         # physical table confirmed in the 9.2 database
    "data_loaded",            # operator ran the import (or mapped SQL)
    "reconciled",             # counts/sums match between instances
    "blocked",
)


@dataclass
class FieldDef:
    fieldname: str
    fieldnum: int
    fieldtype: int | None      # None: field exists on the record but not in PSDBFIELD
    length: int
    decimalpos: int
    useedit: int
    edittable: str = ""
    from_subrecord: str = ""   # "" for a direct field, else the subrecord it came from

    @property
    def is_key(self) -> bool:
        return bool(self.useedit & USEEDIT_KEY)

    @property
    def type_name(self) -> str:
        return FIELDTYPE_NAMES.get(self.fieldtype, f"type {self.fieldtype}")

    @property
    def is_numeric(self) -> bool:
        return self.fieldtype in (2, 3)

    def shape(self) -> tuple:
        """What must match for two instances to count as the same shape."""
        return (self.fieldname, self.fieldtype, self.length,
                self.decimalpos, self.is_key)


@dataclass
class RecordDef:
    recname: str
    rectype: int
    sqltablename: str = ""
    rellang_recname: str = ""
    audit_recname: str = ""
    lastupdoprid: str = ""
    descr: str = ""
    fields: list = field(default_factory=list)        # FieldDef, subrecords expanded
    subrecords: list = field(default_factory=list)    # every subrecord seen (nested too)

    @property
    def rectype_name(self) -> str:
        return RECTYPE_NAMES.get(self.rectype, f"rectype {self.rectype}")

    @property
    def has_table(self) -> bool:
        """Physical data to copy — SQL tables only. Temporary tables (7) are
        transient batch scratch; their definitions port, their data does not."""
        return self.rectype == 0

    @property
    def table_name(self) -> str:
        if self.sqltablename.strip():
            return self.sqltablename.strip().upper()
        return f"PS_{self.recname}"

    def field_map(self) -> dict:
        return {f.fieldname.upper(): f for f in self.fields}

    def key_fields(self) -> list:
        return [f.fieldname for f in self.fields if f.is_key]

    def numeric_fields(self) -> list:
        return [f.fieldname for f in self.fields if f.is_numeric]


@dataclass
class ShapeDiff:
    source_only: list = field(default_factory=list)   # fieldnames only in 9.1
    target_only: list = field(default_factory=list)   # fieldnames only in 9.2
    changed: list = field(default_factory=list)       # "FIELD: length 10 -> 12"

    def empty(self) -> bool:
        return not (self.source_only or self.target_only or self.changed)

    def to_dict(self) -> dict:
        return {"source_only": self.source_only, "target_only": self.target_only,
                "changed": self.changed}


@dataclass
class PlanItem:
    recname: str
    rectype: int
    classification: str
    data_plan: str
    via: list = field(default_factory=list)     # provenance: how it entered the plan
    notes: list = field(default_factory=list)
    shape_diff: dict = field(default_factory=dict)
    row_count: int | None = None                # source rows, when it has a table

    @property
    def rectype_name(self) -> str:
        return RECTYPE_NAMES.get(self.rectype, f"rectype {self.rectype}")

    @property
    def needs_definition_port(self) -> bool:
        """Belongs in the App Designer project copied 9.1 -> 9.2."""
        return self.classification in (BUILD_AND_LOAD, BUILD_DEFINITION,
                                       DRIFT_REVIEW)

    def to_dict(self) -> dict:
        return {
            "recname": self.recname,
            "rectype": self.rectype,
            "rectype_name": self.rectype_name,
            "classification": self.classification,
            "data_plan": self.data_plan,
            "via": self.via,
            "notes": self.notes,
            "shape_diff": self.shape_diff,
            "row_count": self.row_count,
        }
