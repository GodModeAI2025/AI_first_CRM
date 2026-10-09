#!/usr/bin/env python3
"""Plan and apply hash-bound changes to the CRM data model (schema/crm/datamodel.json).

plan   reads a request JSON and writes an immutable plan outside the wiki. The plan holds
       the complete new datamodel.json (checked with crm_contract.validate_datamodel), the
       hash of the current file and, when records must follow the change, an embedded CRM
       transaction plan in the format of crm_records.py. Nothing in the wiki changes.
apply  applies exactly that plan with --expect-plan-sha256; destructive plans (delete-field,
       delete-object with records) also need --confirm-destructive.

Apply order and why. The embedded record transaction is bound to the hash of the CURRENT
datamodel.json and runs first through crm_contract.apply_transaction. That function checks
every precondition (data model hash, each record file, each event shard) before its first
write and takes its own targeted snapshot, so a stale plan stops the whole schema change
with zero writes. Only afterwards is datamodel.json snapshotted and replaced atomically as
the last write. The planned record contents were validated against the NEW model during
planning, so once both steps are done every touched record is valid. Between the two
writes the maintenance lock keeps every reader away; if the final write fails, the result
is partial_failure and names both snapshots for a restore. Records in the trash are
migrated like all others: their values must stay valid and unique values count there too.

Value migrations are not record edits: they append "update" events with origin kind
"migration" and a "schema_change" marker, and leave crm_updated_at untouched, as a column
migration in a CRM does. time-in-stage and workflow triggers can tell them apart.
rename-option and remove-option with map_to keep the earlier value in previousValues of
the surviving option, so history logged under it still reads as that option; such a value
cannot be given to another option.

Request:
{
  "actor": "agent/<name>" | "human:<id>" | "process:<id>",
  "occasion": "<why the model changes>",
  "operations": [
    {"op": "add-object", "name": "project", "labelSingular": "Projekt", "labelPlural": "Projekte",
     "labelField": "name", "description": "...", "fields": {"name": {"type": "TEXT", "label": "Name"}}},
    {"op": "update-object", "object": "opportunity", "labelSingular": "Chance", "labelPlural": "Chancen",
     "description": "...", "icon": "IconTargetArrow"},
    {"op": "add-field", "object": "project", "name": "company", "type": "RELATION", "label": "Firma",
     "relation": {"type": "MANY_TO_ONE", "target": "company", "inverse": "projects",
                  "inverseLabel": "Projekte", "onDelete": "SET_NULL"}},
    {"op": "add-field", "object": "note", "label": "Bezug", "type": "MORPH_RELATION",
     "relation": {"targets": ["person", "company"], "multiple": true}},
    {"op": "add-field", "object": "project", "label": "Status", "type": "SELECT",
     "options": [{"value": "OPEN", "label": "Offen", "color": "sky"}], "default": "OPEN"},
    {"op": "update-field", "object": "company", "field": "x", "label": "...", "description": "...",
     "nullable": false, "unique": true, "default": "..."},
    {"op": "add-option", "object": "opportunity", "field": "stage", "value": "NEGOTIATION",
     "label": "Verhandlung", "color": "orange", "position": 4},
    {"op": "update-option", "object": "opportunity", "field": "stage", "value": "NEW", "label": "...",
     "color": "red", "position": 0},
    {"op": "rename-option", "object": "opportunity", "field": "stage", "from": "SCREENING",
     "to": "QUALIFIED", "label": "Qualifiziert"},
    {"op": "remove-option", "object": "opportunity", "field": "stage", "value": "MEETING", "map_to": "PROPOSAL"},
    {"op": "deactivate" | "activate", "object": "company", "field": "linkedinLink"},
    {"op": "delete-field", "object": "project", "field": "budget"},
    {"op": "delete-object", "object": "project"}
  ]
}
deactivate and activate without "field" act on the object. Names of new objects and fields
may be left out; they are derived from the label as CRMs usually do (camelCase, "Custom" suffix
for reserved names). A RELATION creates both sides; MANY_TO_ONE on the object stores the
link, the ONE_TO_MANY side on the target is computed.

update-object renames any object, standard objects included, or changes its description or
icon; it needs at least one of these four keys. The API name, the record directory and the
label field never change, so no record is touched, but the generated views show the old
labels until crm_build.py runs again.

The plan lists dependent references without rewriting them: views, dashboards and roles that
name the object, field or option, and the active or draft workflow versions that use it. A
workflow uses a field through its trigger (eventName "<object>.<action>" with a "fields"
list), through the inputs of steps whose objectName is the object (objectRecord keys,
fieldsToUpdate, matchFields, filter and orderBy fields) or through variables that read a
record of the object, such as {{trigger.object.<field>}} or {{<stepId>.<field>}}.

Exit codes: plan 0 planned, 1 plan with errors, 2 error; apply 0 applied, 2 error,
3 confirmation required or stale plan, 4 invalid plan, 5 partial failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import copy
import json
import re
import unicodedata
from typing import Any, Optional

import portable_io
from crm_contract import (
    StalePlanError,
    COMPOSITE_SUBFIELDS, DASHBOARDS_PATH, DATAMODEL_FORMAT, DATAMODEL_PATH, EVENT_FORMAT, FIELD_TYPES, NAME_RE,
    ON_DELETE, OPTION_VALUE_RE, PLAN_FORMAT, RECORDS_DIR, RELATION_TYPES, ROLES_PATH, SYSTEM_ONLY_TYPES, VIEWS_PATH,
    WORKFLOWS_DIR, CrmError, DataModel, OperationError, Record, RecordStore, amount_to_micros, apply_transaction,
    canonical_json, coerce_subfield, coerce_value, compose_record, event_shard, field_is_empty, frontmatter_keys,
    is_empty, kebab, load_datamodel, load_json_file, new_event_id, new_transaction_id, option_values,
    parse_record_link, read_events, record_relative_path, sha256_bytes, sha256_text, unique_fields, unique_key,
    utc_now, valid_actor, validate_datamodel, validate_event, validate_record, verify_plan,
)
from wiki_lock import require_lock

SCHEMA_PLAN_FORMAT = "lmwiki-crm-schema-plan/1"
OPERATIONS = (
    "add-object", "update-object", "add-field", "update-field", "add-option", "update-option", "rename-option",
    "remove-option", "deactivate", "activate", "delete-field", "delete-object",
)
# Names reserved by the metadata model
RESERVED_NAMES = frozenset({
    "approvedAccessDomain", "approvedAccessDomains", "appToken", "appTokens", "billingCustomer", "billingCustomers",
    "billingEntitlement", "billingEntitlements", "billingMeter", "billingMeters", "billingProduct", "billingProducts",
    "billingSubscription", "billingSubscriptions", "billingSubscriptionItem", "billingSubscriptionItems",
    "featureFlag", "featureFlags", "job", "jobs", "keyValuePair", "keyValuePairs", "pageLayout", "pageLayouts",
    "pageLayoutTab", "pageLayoutTabs", "pageLayoutWidget", "pageLayoutWidgets", "twoFactorMethod", "twoFactorMethods",
    "user", "users", "userWorkspace", "userWorkspaces", "workspace", "workspaces", "role", "roles",
    "userWorkspaceRole", "userWorkspaceRoles", "plan", "plans", "event", "events", "field", "fields", "link", "links",
    "currency", "currencies", "fullNames", "address", "addresses", "type", "types", "object", "objects", "index",
    "relation", "relations", "aggregate", "connect", "create", "disconnect", "search", "searches",
})
# Option colors
TAG_COLORS = (
    "red", "ruby", "crimson", "tomato", "orange", "amber", "yellow", "lime", "grass", "green", "jade", "mint",
    "turquoise", "cyan", "sky", "blue", "iris", "violet", "purple", "plum", "pink", "bronze", "gold", "brown", "gray",
)
IDENTIFIER_MAX = 63  # PostgreSQL identifier length
STORAGE_RESERVED = frozenset({"con", "prn", "aux", "nul"} | {f"com{n}" for n in range(10)} | {f"lpt{n}" for n in range(10)})
OS_ARTIFACTS = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
FIELD_KEYS = {"op", "object", "name", "type", "label", "description", "icon", "nullable", "unique", "default", "options", "relation"}
UPDATE_KEYS = {"label", "description", "icon", "nullable", "unique", "default"}
OBJECT_KEYS = {"op", "name", "labelSingular", "labelPlural", "labelField", "description", "icon", "fields"}
OBJECT_UPDATE_KEYS = {"labelSingular", "labelPlural", "description", "icon"}
LABEL_FIELD_TYPES = {"TEXT", "FULL_NAME", "EMAILS", "LINKS", "UUID", "NUMBER"}
TOMBSTONES = "deletedObjects"
# Workflow versions the reference scan reads: the one that runs and the draft that may replace it.
SCANNED_VERSIONS = ("ACTIVE", "DRAFT")
# Steps whose output is one record of their objectName ({{stepId.<field>}}).
RECORD_OUTPUT_STEPS = {"CREATE_RECORD", "UPDATE_RECORD", "UPSERT_RECORD", "DELETE_RECORD", "PICK_RECORD"}
# Where a database event output holds the record: {{trigger.object.x}}, {{trigger.properties.after.x}}, ...
EVENT_RECORD_PATHS = (("object",), ("properties", "after"), ("properties", "before"), ("properties", "diff"))
VARIABLE_RE = re.compile(r"\{\{([^{}]+)\}\}")
PATH_SEGMENT_RE = re.compile(r"\[([^\]]*)\]|([^.\[\]]+)")


class SchemaError(CrmError):
    """One requested schema operation cannot be planned; details name the affected records."""

    def __init__(self, message: str, details: Optional[list[Any]] = None):
        super().__init__(message)
        self.details = details or []


# ---------------------------------------------------------------------------
# names, labels, options


def name_from_label(label: str) -> str:
    """API name derived from a label as CRMs usually do: transliterated, camelCase, Custom suffix if reserved."""
    text = unicodedata.normalize("NFKD", label.replace("ß", "ss").replace("ẞ", "SS"))
    text = "".join(character for character in text if not unicodedata.combining(character))
    parts = re.findall(r"[A-Za-z0-9]+", text)
    if not parts:
        raise SchemaError(f"no API name can be derived from the label {label!r}; give the name explicitly")
    name = parts[0].lower() + "".join(part[:1].upper() + part[1:].lower() for part in parts[1:])
    if name[0].isdigit():
        name = "n" + name
    if name in RESERVED_NAMES:
        name += "Custom"
    return name


def option_value_from_label(label: str) -> str:
    text = unicodedata.normalize("NFKD", label.replace("ß", "ss").replace("ẞ", "SS"))
    text = "".join(character for character in text if not unicodedata.combining(character))
    parts = re.findall(r"[A-Za-z0-9]+", text)
    if not parts:
        raise SchemaError(f"no option value can be derived from the label {label!r}; give the value explicitly")
    return "_".join(part.upper() for part in parts)


def check_label(value: Any, what: str = "label") -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{what} is required")
    text = " ".join(value.split())
    if len(text) > IDENTIFIER_MAX:
        raise SchemaError(f"{what} {text!r} is longer than {IDENTIFIER_MAX} characters")
    return text


def check_option_label(value: Any) -> str:
    text = check_label(value, "option label")
    if "," in text:
        raise SchemaError(f"option label {text!r} must not contain a comma")
    return text


def check_option_value(value: Any) -> str:
    if not isinstance(value, str) or not OPTION_VALUE_RE.fullmatch(value):
        raise SchemaError(f"option value {value!r} must be UPPER_SNAKE_CASE (A-Z, 0-9, _) of at most {IDENTIFIER_MAX} characters")
    return value


def check_color(value: Any) -> str:
    if value not in TAG_COLORS:
        raise SchemaError(f"color {value!r} is not supported; use one of {', '.join(TAG_COLORS)}")
    return str(value)


def renumber(options: list[dict[str, Any]]) -> None:
    for index, option in enumerate(options):
        option["position"] = index


def check_name(name: Any, what: str, *, explicit: bool) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name.startswith("crm"):
        raise SchemaError(
            f"{what} name {name!r} must be camelCase ASCII letters and digits, start with a lowercase letter, "
            f"have at most {IDENTIFIER_MAX} characters and not start with 'crm'"
        )
    if explicit and name in RESERVED_NAMES:
        raise SchemaError(f"{what} name {name!r} is reserved; choose another name")
    return name


def dump_datamodel(raw: dict[str, Any]) -> str:
    return json.dumps(raw, ensure_ascii=False, indent=2) + "\n"


def core_accepts_tombstones() -> bool:
    """True once crm_contract.validate_event accepts events of objects listed in deletedObjects."""
    probe = DataModel({
        "format": DATAMODEL_FORMAT,
        "objects": {"probeKept": {"labelSingular": "A", "labelPlural": "B", "labelField": "name", "fields": {"name": {"type": "TEXT", "label": "N"}}}},
        TOMBSTONES: {"probeGone": {"directory": "records/probe-gone"}},
    }, "")
    event = {
        "format": EVENT_FORMAT, "event_id": "evt-0000000000000000", "at": "2026-01-01T00:00:00Z", "actor": "agent/probe",
        "op": "destroy", "object": "probeGone", "record_id": "00000000-0000-4000-8000-000000000000",
        "changes": {}, "origin": {"kind": "migration"},
    }
    return not any("unknown object" in problem for problem in validate_event(event, probe))


# ---------------------------------------------------------------------------
# workflow references


def nested(value: Any) -> list[Any]:
    """The value and everything inside it; mapping keys count as values."""
    found: list[Any] = []
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        found.append(item)
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


def variable_paths(value: Any) -> list[list[str]]:
    """Path segments of every {{...}} variable in a nested value; dots and [brackets] separate them."""
    paths = []
    for text in nested(value):
        if not isinstance(text, str):
            continue
        for match in VARIABLE_RE.finditer(text):
            words = match.group(1).split()
            expression = words[0] if words else ""
            for prefix in ("this.", "@root."):
                if expression.startswith(prefix):
                    expression = expression[len(prefix):]
            paths.append([bracket or plain for bracket, plain in PATH_SEGMENT_RE.findall(expression)])
    return paths


def event_object(name: Any) -> Optional[str]:
    """The object of an event name such as opportunity.updated."""
    return name.split(".", 1)[0] if isinstance(name, str) and "." in name else None


def field_base(key: Any) -> Optional[str]:
    """The field of a key such as amount.amountMicros."""
    return key.split(".", 1)[0] if isinstance(key, str) else None


def as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def manual_object(trigger: dict[str, Any]) -> Optional[str]:
    """The object a MANUAL trigger runs on (SINGLE_RECORD or BULK_RECORDS), else None."""
    settings = as_mapping(trigger.get("settings"))
    availability = settings.get("availability")
    if isinstance(availability, dict):
        kind, object_name = availability.get("type"), availability.get("objectNameSingular") or settings.get("objectType")
    else:
        kind, object_name = availability, settings.get("objectType")
    return object_name if trigger.get("type") == "MANUAL" and kind in ("SINGLE_RECORD", "BULK_RECORDS") else None


def record_roots(trigger: dict[str, Any], steps: list[dict[str, Any]], object_name: str) -> list[tuple[str, ...]]:
    """Variable paths that hold a record of the object in one workflow version; "*" stands for a list position."""
    settings = as_mapping(trigger.get("settings"))
    roots: list[tuple[str, ...]] = []
    lists: set[tuple[str, ...]] = set()
    if trigger.get("type") == "DATABASE_EVENT" and event_object(settings.get("eventName")) == object_name:
        roots += [("trigger",) + path for path in EVENT_RECORD_PATHS]
    if manual_object(trigger) == object_name:
        roots += [("trigger", "record"), ("trigger", "selectedRecord"), ("trigger", "records", "*"), ("trigger", "selectedRecords", "*")]
        lists |= {("trigger", "records"), ("trigger", "selectedRecords")}
    for step in steps:
        step_id, kind, inp = str(step.get("id")), step.get("type"), as_mapping(as_mapping(step.get("settings")).get("input"))
        if kind in RECORD_OUTPUT_STEPS and inp.get("objectName") == object_name:
            roots.append((step_id,))
        elif kind == "FIND_RECORDS" and inp.get("objectName") == object_name:
            roots += [(step_id, "first"), (step_id, "all", "*")]
            lists.add((step_id, "all"))
        elif kind == "WAIT_FOR_EVENT" and event_object(inp.get("eventName")) == object_name:
            roots += [(step_id,) + path for path in EVENT_RECORD_PATHS]
    for step in steps:
        items = as_mapping(as_mapping(step.get("settings")).get("input")).get("items")
        if step.get("type") == "ITERATOR" and any(tuple(path) in lists for path in variable_paths(items)):
            roots.append((str(step.get("id")), "currentItem"))
    return roots


def reads_field(value: Any, roots: list[tuple[str, ...]], keys: set[str]) -> bool:
    """True when a {{variable}} in the value reads one of the keys from a record held at one of the roots."""
    for path in variable_paths(value):
        for root in roots:
            if len(path) > len(root) and path[len(root)] in keys and all(
                    part == segment or (part == "*" and segment.isdigit()) for part, segment in zip(root, path)):
                return True
    return False


def names_object(value: Any, object_name: str) -> bool:
    """True when a nested value names the object: objectName, objectNameSingular, object or an event name."""
    return any(
        isinstance(item, dict) and (
            object_name in (item.get("objectName"), item.get("objectNameSingular"), item.get("object"))
            or event_object(item.get("eventName")) == object_name
        )
        for item in nested(value)
    )


def named_fields(settings: Any, object_name: str) -> list[str]:
    """Field keys a step names for the object: inputs of a step on the object and objectName/fieldName pairs."""
    inp = as_mapping(as_mapping(settings).get("input"))
    names: list[Any] = []
    if object_name in (inp.get("objectName"), event_object(inp.get("eventName"))):
        record = inp.get("objectRecord")
        names += list(record) if isinstance(record, dict) else []
        for key in ("fieldsToUpdate", "matchFields", "updatedFields"):
            names += inp[key] if isinstance(inp.get(key), list) else []
        conditions = [item for item in nested([inp.get("filter"), inp.get("orderBy")]) if isinstance(item, dict)]
        names += [item.get(key) for item in conditions for key in ("field", "fieldName")]
    names += [
        item.get("fieldName") for item in nested(settings)
        if isinstance(item, dict) and object_name in (item.get("objectName"), item.get("objectNameSingular"))
    ]
    return [name for name in names if isinstance(name, str)]


def trigger_uses(trigger: dict[str, Any], object_name: str, keys: Optional[set[str]], roots: list[tuple[str, ...]]) -> bool:
    settings = as_mapping(trigger.get("settings"))
    on_object = event_object(settings.get("eventName")) == object_name or manual_object(trigger) == object_name
    if keys is None:
        return on_object
    watched = settings.get("fields") if isinstance(settings.get("fields"), list) else []
    return (on_object and any(field_base(name) in keys for name in watched)) or reads_field(settings, roots, keys)


def step_uses(step: dict[str, Any], object_name: str, keys: Optional[set[str]], roots: list[tuple[str, ...]]) -> bool:
    settings = step.get("settings")
    if keys is None:
        return names_object(settings, object_name)
    return any(field_base(name) in keys for name in named_fields(settings, object_name)) or reads_field(settings, roots, keys)


def workflow_places(version: dict[str, Any], object_name: str, keys: Optional[set[str]]) -> list[str]:
    """Where one workflow version uses the object, or one of its fields when keys names it: trigger, step <id>."""
    trigger = as_mapping(version.get("trigger"))
    listed = version.get("steps")
    steps = [step for step in listed if isinstance(step, dict)] if isinstance(listed, list) else []
    roots = record_roots(trigger, steps, object_name) if keys is not None else []
    places = ["trigger"] if trigger_uses(trigger, object_name, keys, roots) else []
    return places + [f"step {step.get('id')}" for step in steps if step_uses(step, object_name, keys, roots)]


def mentions_word(value: Any, word: str) -> bool:
    """True when a text in the nested value holds the word on its own, as an option value or inside a formula."""
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])")
    return any(isinstance(item, str) and pattern.search(item) is not None for item in nested(value))


# ---------------------------------------------------------------------------
# planning


class SchemaPlanner:
    def __init__(self, target: Path, actor: str, occasion: str, now: Optional[str] = None):
        self.target = target
        path = target / DATAMODEL_PATH
        self.before_bytes = path.read_bytes() if path.is_file() else b""
        self.old = load_datamodel(target)
        self.raw: dict[str, Any] = copy.deepcopy(self.old.raw)
        self.actor = actor
        self.occasion = occasion
        self.now = now or utc_now()
        self.txn = new_transaction_id()
        self.store = RecordStore(target, self.old)
        self.working: dict[tuple[str, str], Record] = {}
        self.destroyed: set[tuple[str, str]] = set()
        self.events: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.changes: list[str] = []
        self.references: list[str] = []
        self.remove_dirs: list[str] = []
        self.destructive = False
        self.destructive_records = False

    # -- model access -----------------------------------------------------

    @property
    def objects(self) -> dict[str, Any]:
        return self.raw["objects"]

    def object_def(self, name: Any) -> dict[str, Any]:
        if not isinstance(name, str) or name not in self.objects:
            raise SchemaError(f"unknown object {name!r}")
        return self.objects[name]

    def field_def(self, object_name: Any, field_name: Any) -> dict[str, Any]:
        fields = self.object_def(object_name)["fields"]
        if not isinstance(field_name, str) or field_name not in fields:
            raise SchemaError(f"unknown field {object_name}.{field_name}")
        return fields[field_name]

    def select_field(self, op: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        object_name, field_name = op.get("object"), op.get("field")
        definition = self.field_def(object_name, field_name)
        if definition.get("type") not in ("SELECT", "MULTI_SELECT"):
            raise SchemaError(f"{object_name}.{field_name} is {definition.get('type')}, not SELECT or MULTI_SELECT")
        return object_name, field_name, definition

    @staticmethod
    def only_keys(op: dict[str, Any], allowed: set[str]) -> None:
        unknown = sorted(set(op) - allowed)
        if unknown:
            raise SchemaError(f"unknown keys for {op.get('op')}: {', '.join(unknown)}")

    # -- records ----------------------------------------------------------

    def current_records(self, object_name: str) -> list[Record]:
        """Every record of an object in its planned state, records in the trash included."""
        result = dict(self.store.records(object_name))
        for (obj, record_id), record in self.working.items():
            if obj == object_name:
                result[record_id] = record
        for obj, record_id in self.destroyed:
            if obj == object_name:
                result.pop(record_id, None)
        prefix = f"{RECORDS_DIR}/{kebab(object_name)}/"
        unreadable = [problem for problem in self.store.load_errors if problem.startswith(prefix)]
        if unreadable:
            raise SchemaError(f"{object_name} has unreadable records; repair them before changing the data model", unreadable[:20])
        return list(result.values())

    def touch(self, record: Record) -> Record:
        key = (record.object, record.id)
        if key not in self.working:
            self.working[key] = record.copy()
        return self.working[key]

    def set_value(self, record: Record, key: str, value: Any, changes: dict[str, Any]) -> None:
        before = record.data.get(key)
        if is_empty(before) and is_empty(value):
            record.data.pop(key, None)
            return
        if before == value:
            return
        if is_empty(value):
            record.data.pop(key, None)
        else:
            record.data[key] = value
        changes[key] = [before, None if is_empty(value) else value]

    def emit(self, op: str, record: Record, changes: dict[str, Any], schema_change: dict[str, Any]) -> None:
        if not changes:
            return
        self.events.append({
            "format": EVENT_FORMAT,
            "event_id": new_event_id(),
            "at": self.now,
            "actor": self.actor,
            "op": op,
            "object": record.object,
            "record_id": record.id,
            "changes": changes,
            "origin": {"kind": "migration", "occasion": self.occasion},
            "txn": self.txn,
            "schema_change": schema_change,
        })

    @staticmethod
    def ref(record: Record) -> dict[str, Any]:
        return {"id": record.id, "title": record.data.get("crm_title"), "path": record.path, "deleted": record.deleted}

    def clear_field(self, object_name: str, field_name: str, definition: dict[str, Any], schema_change: dict[str, Any]) -> int:
        """Remove every stored value of one field from all records; return how many records changed."""
        touched = 0
        for record in self.current_records(object_name):
            keys = [key for key in frontmatter_keys(field_name, definition) if not is_empty(record.data.get(key))]
            text = record.richtext.get(field_name, "") if definition.get("type") == "RICH_TEXT" else ""
            if not keys and not text:
                continue
            record = self.touch(record)
            changes: dict[str, Any] = {}
            for key in keys:
                self.set_value(record, key, None, changes)
            if definition.get("type") == "RICH_TEXT" and field_name in record.richtext:
                before = record.richtext.pop(field_name)
                if before.strip():
                    changes[f"richtext:{field_name}"] = {"before_sha256": sha256_text(before), "after_sha256": "", "after_chars": 0}
            self.emit("update", record, changes, schema_change)
            touched += 1 if changes else 0
        return touched

    def require_values(self, object_name: str, field_name: str, definition: dict[str, Any]) -> None:
        missing = [
            record for record in self.current_records(object_name)
            if not record.deleted and field_is_empty(record.data, record.richtext, field_name, definition)
        ]
        if missing:
            raise SchemaError(
                f"{object_name}.{field_name} cannot become required: {len(missing)} records have no value",
                [self.ref(record) for record in missing[:50]],
            )

    def require_unique(self, object_name: str, field_name: str, definition: dict[str, Any]) -> None:
        groups: dict[str, list[Record]] = {}
        for record in self.current_records(object_name):
            key = unique_key(field_name, definition, record.data)
            if key is not None:
                groups.setdefault(key, []).append(record)
        duplicates = [(key, records) for key, records in sorted(groups.items()) if len(records) > 1]
        if duplicates:
            details = []
            for key, records in duplicates[:50]:
                try:
                    value: Any = json.loads(key)
                except ValueError:
                    value = key
                details.append({"value": value, "records": [self.ref(record) for record in records]})
            raise SchemaError(
                f"{object_name}.{field_name} cannot become unique: {len(duplicates)} values are used more than once "
                "(records in the trash count as well)",
                details,
            )

    def records_with_option(self, object_name: str, field_name: str, value: str) -> list[Record]:
        found = []
        for record in self.current_records(object_name):
            stored = record.data.get(field_name)
            if stored == value or (isinstance(stored, list) and value in stored):
                found.append(record)
        return found

    def replace_option(self, object_name: str, field_name: str, old: str, new: str, schema_change: dict[str, Any]) -> tuple[int, int]:
        changed = deleted = 0
        for record in self.records_with_option(object_name, field_name, old):
            record = self.touch(record)
            stored = record.data.get(field_name)
            if isinstance(stored, list):
                value: Any = list(dict.fromkeys(new if item == old else item for item in stored))
            else:
                value = new
            changes: dict[str, Any] = {}
            self.set_value(record, field_name, value, changes)
            self.emit("update", record, changes, schema_change)
            changed += 1
            deleted += 1 if record.deleted else 0
        return changed, deleted

    # -- dependent configuration -------------------------------------------

    def note_references(self, object_name: str, field_name: Optional[str] = None, option: Optional[str] = None) -> None:
        """List views, dashboards, workflows and roles that name what changes; they are not rewritten here."""
        subject = f"{object_name}.{field_name}" if field_name else object_name
        if option:
            subject += f" option {option}"

        def mentions(value: Any) -> bool:
            if isinstance(value, str):
                if option:
                    return value == option
                return value == field_name or value.split(".", 1)[0] == field_name
            if isinstance(value, list):
                return any(mentions(item) for item in value)
            if isinstance(value, dict):
                return any(mentions(key) or mentions(item) for key, item in value.items())
            return False

        def check(item: Any, where: str) -> None:
            if isinstance(item, dict) and item.get("object") == object_name and (field_name is None or mentions(item)):
                self.references.append(f"{where} references {subject}")

        try:
            views = load_json_file(self.target, VIEWS_PATH, {})
            for view in views.get("views", []) if isinstance(views, dict) else []:
                check(view, f"{VIEWS_PATH}: view {view.get('id') if isinstance(view, dict) else '?'}")
            dashboards = load_json_file(self.target, DASHBOARDS_PATH, {})
            for dashboard in dashboards.get("dashboards", []) if isinstance(dashboards, dict) else []:
                for widget in dashboard.get("widgets", []) if isinstance(dashboard, dict) else []:
                    check(widget, f"{DASHBOARDS_PATH}: dashboard {dashboard.get('id')} widget {widget.get('id') if isinstance(widget, dict) else '?'}")
            roles = load_json_file(self.target, ROLES_PATH, {})
            for role_name, role in (roles.get("roles", {}) if isinstance(roles, dict) else {}).items():
                rules = role.get("objects", {}).get(object_name) if isinstance(role, dict) else None
                if isinstance(rules, dict) and (field_name is None or field_name in rules.get("fields", {})):
                    self.references.append(f"{ROLES_PATH}: role {role_name} references {subject}")
        except CrmError as exc:
            self.warnings.append(f"dependent configuration could not be checked: {exc}")
        self.note_workflow_references(object_name, field_name, option, subject)

    def field_keys(self, object_name: str, field_name: str) -> set[str]:
        """Names a workflow may use for a field: the field itself and, for a relation, <field>Id."""
        fields = self.old.objects.get(object_name, {}).get("fields", {})
        keys = {field_name}
        if fields.get(field_name, {}).get("type") in ("RELATION", "MORPH_RELATION") and f"{field_name}Id" not in fields:
            keys.add(f"{field_name}Id")
        return keys

    def note_workflow_references(self, object_name: str, field_name: Optional[str], option: Optional[str], subject: str) -> None:
        """List the active and draft workflow versions that use the object, field or option (see workflow_places)."""
        keys = self.field_keys(object_name, field_name) if field_name else None
        workflows = self.target / WORKFLOWS_DIR
        for path in sorted(workflows.glob("*.json")) if workflows.is_dir() else []:
            relative = path.relative_to(self.target).as_posix()
            try:
                content = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                self.warnings.append(f"{relative} could not be read; check it by hand for references to {subject}")
                continue
            versions = content.get("versions") if isinstance(content, dict) else None
            for version in versions if isinstance(versions, list) else []:
                if not isinstance(version, dict) or version.get("status") not in SCANNED_VERSIONS:
                    continue
                places = workflow_places(version, object_name, keys)
                # An option counts when the version uses its field and names the value, in a filter, a record or a formula.
                if not places or (option and not mentions_word([version.get("trigger"), version.get("steps")], option)):
                    continue
                verb = "probably references" if option else "references"
                self.references.append(
                    f"{relative}: v{version.get('version')} ({version.get('status')}) {verb} {subject} in {', '.join(places)}"
                )

    # -- operations ---------------------------------------------------------

    def op_add_object(self, op: dict[str, Any]) -> None:
        self.only_keys(op, OBJECT_KEYS)
        singular = check_label(op.get("labelSingular"), "labelSingular")
        plural = check_label(op.get("labelPlural"), "labelPlural")
        if singular.casefold() == plural.casefold():
            raise SchemaError("labelSingular and labelPlural must differ, as is usual in CRMs")
        explicit = bool(op.get("name"))
        name = check_name(op.get("name") or name_from_label(singular), "object", explicit=explicit)
        if name in self.objects:
            raise SchemaError(f"object {name} already exists")
        directory = kebab(name)
        if directory in STORAGE_RESERVED:
            raise SchemaError(f"object {name} would use the directory records/{directory}, a name SharePoint and Windows refuse")
        for other in self.objects:
            if kebab(other) == directory:
                raise SchemaError(f"object {name} would share the directory records/{directory} with {other}")
        if (self.target / RECORDS_DIR / directory).exists():
            raise SchemaError(f"records/{directory} already exists; move it away before adding object {name}")
        fields_spec = op.get("fields") or {}
        if not isinstance(fields_spec, dict):
            raise SchemaError("fields must be a mapping of field name to definition")
        label_field = op.get("labelField") or "name"
        if label_field not in fields_spec:
            if label_field != "name":
                raise SchemaError(f"labelField {label_field!r} must be one of the fields given")
            fields_spec = {"name": {"type": "TEXT", "label": "Name"}, **fields_spec}
        label_type = fields_spec[label_field].get("type") if isinstance(fields_spec[label_field], dict) else None
        if label_type not in LABEL_FIELD_TYPES:
            raise SchemaError(f"labelField must be one of {', '.join(sorted(LABEL_FIELD_TYPES))}, not {label_type}")
        definition: dict[str, Any] = {
            "labelSingular": singular, "labelPlural": plural, "labelField": label_field,
            "standard": False, "active": True, "fields": {},
        }
        for key in ("description", "icon"):
            if isinstance(op.get(key), str) and op[key].strip():
                definition[key] = op[key].strip()
        self.objects[name] = definition
        tombstones = self.raw.get(TOMBSTONES)
        if isinstance(tombstones, dict) and name in tombstones:
            tombstones.pop(name)
            if not tombstones:
                self.raw.pop(TOMBSTONES)
            self.warnings.append(f"object {name} existed before; its earlier history in the event log now belongs to the new object")
        for field_name, spec in fields_spec.items():
            if not isinstance(spec, dict):
                raise SchemaError(f"field {field_name} must be a mapping")
            self.add_field(name, {**spec, "name": field_name}, announce=False)
        self.changes.append(f"add object {name} ({singular} / {plural}) with fields {', '.join(definition['fields'])}")

    def op_update_object(self, op: dict[str, Any]) -> None:
        if "name" in op or "labelField" in op:
            raise SchemaError(
                "update-object keeps the API name, the record directory and the label field; "
                f"it changes only {', '.join(sorted(OBJECT_UPDATE_KEYS))}"
            )
        self.only_keys(op, OBJECT_UPDATE_KEYS | {"op", "object"})
        object_name = op.get("object")
        definition = self.object_def(object_name)
        if not any(key in op for key in OBJECT_UPDATE_KEYS):
            raise SchemaError(f"update-object needs at least one of {', '.join(sorted(OBJECT_UPDATE_KEYS))}")
        changed = []
        for key in ("labelSingular", "labelPlural"):
            if key in op:
                definition[key] = check_label(op[key], key)
                changed.append(key)
        if str(definition.get("labelSingular", "")).casefold() == str(definition.get("labelPlural", "")).casefold():
            raise SchemaError("labelSingular and labelPlural must differ, as is usual in CRMs")
        for key in ("description", "icon"):
            if key not in op:
                continue
            value = op[key]
            if value is not None and not isinstance(value, str):
                raise SchemaError(f"{key} must be text")
            if value is None or not value.strip():
                definition.pop(key, None)
            else:
                definition[key] = value.strip()
            changed.append(key)
        self.changes.append(f"update object {object_name}: {', '.join(changed)}; API name, records and label field stay")

    def op_add_field(self, op: dict[str, Any]) -> None:
        self.add_field(op.get("object"), op)

    def add_field(self, object_name: Any, spec: dict[str, Any], *, announce: bool = True) -> None:
        self.only_keys(spec, FIELD_KEYS)
        obj = self.object_def(object_name)
        ftype = spec.get("type")
        if ftype not in FIELD_TYPES:
            raise SchemaError(f"unknown field type {ftype!r}")
        if ftype in SYSTEM_ONLY_TYPES:
            raise SchemaError(f"type {ftype} is kept as a system key and cannot be a field")
        label = check_label(spec.get("label"))
        explicit = bool(spec.get("name"))
        name = check_name(spec.get("name") or name_from_label(label), "field", explicit=explicit)
        if name in obj["fields"]:
            raise SchemaError(f"field {object_name}.{name} already exists")
        definition: dict[str, Any] = {"type": ftype, "label": label, "standard": False}
        for key in ("description", "icon"):
            if isinstance(spec.get(key), str) and spec[key].strip():
                definition[key] = spec[key].strip()
        for flag in ("nullable", "unique"):
            if flag in spec:
                if not isinstance(spec[flag], bool):
                    raise SchemaError(f"{flag} must be true or false")
                definition[flag] = spec[flag]
        if ftype in ("SELECT", "MULTI_SELECT"):
            definition["options"] = self.new_options(spec.get("options"))
        elif "options" in spec:
            raise SchemaError(f"options apply to SELECT and MULTI_SELECT fields, not {ftype}")
        if ftype not in ("RELATION", "MORPH_RELATION") and "relation" in spec:
            raise SchemaError(f"relation settings apply to RELATION and MORPH_RELATION fields, not {ftype}")
        if spec.get("default") is not None:
            default = self.coerce_default(ftype, definition, spec["default"])
            if default is not None:
                definition["default"] = default
        summary = f"add field {object_name}.{name} ({ftype})"
        if ftype == "RELATION":
            summary = self.add_relation(object_name, name, definition, spec.get("relation"))
        elif ftype == "MORPH_RELATION":
            definition["relation"] = self.morph_settings(spec.get("relation"))
            obj["fields"][name] = definition
            summary = f"add field {object_name}.{name} (MORPH_RELATION to {', '.join(definition['relation']['targets'])})"
        else:
            obj["fields"][name] = definition
        stored = not (ftype == "RELATION" and definition["relation"]["type"] == "ONE_TO_MANY")
        if definition.get("nullable") is False and stored:
            # A new field is empty everywhere; it can only be required on an object without live records.
            self.require_values(object_name, name, definition)
        if announce:
            self.changes.append(summary)

    def add_relation(self, object_name: str, name: str, definition: dict[str, Any], relation: Any) -> str:
        if not isinstance(relation, dict):
            raise SchemaError("a RELATION field needs relation settings with type and target")
        unknown = sorted(set(relation) - {"type", "target", "inverse", "inverseLabel", "onDelete"})
        if unknown:
            raise SchemaError(f"unknown relation settings: {', '.join(unknown)}")
        rtype = relation.get("type", "MANY_TO_ONE")
        if rtype not in RELATION_TYPES:
            raise SchemaError("relation type must be MANY_TO_ONE or ONE_TO_MANY")
        target = relation.get("target")
        target_def = self.object_def(target)
        on_delete = relation.get("onDelete", "SET_NULL")
        if on_delete not in ON_DELETE:
            raise SchemaError("onDelete must be SET_NULL, CASCADE or RESTRICT")
        source = self.objects[object_name]
        inverse_label = check_label(
            relation.get("inverseLabel") or (source["labelPlural"] if rtype == "MANY_TO_ONE" else source["labelSingular"]),
            "inverseLabel",
        )
        explicit = bool(relation.get("inverse"))
        inverse = check_name(relation.get("inverse") or name_from_label(inverse_label), "inverse field", explicit=explicit)
        if inverse in target_def["fields"] or (target == object_name and inverse == name):
            raise SchemaError(f"field {target}.{inverse} already exists; give another inverse name")
        if rtype == "MANY_TO_ONE":
            definition["relation"] = {"type": "MANY_TO_ONE", "target": target, "inverse": inverse, "onDelete": on_delete}
            partner = {"type": "RELATION", "label": inverse_label, "standard": False,
                       "relation": {"type": "ONE_TO_MANY", "target": object_name, "inverse": name}}
        else:
            if definition.get("nullable") is False or definition.get("unique"):
                raise SchemaError("the computed ONE_TO_MANY side cannot be required or unique")
            definition["relation"] = {"type": "ONE_TO_MANY", "target": target, "inverse": inverse}
            partner = {"type": "RELATION", "label": inverse_label, "standard": False,
                       "relation": {"type": "MANY_TO_ONE", "target": object_name, "inverse": name, "onDelete": on_delete}}
        source["fields"][name] = definition
        target_def["fields"][inverse] = partner
        if rtype == "MANY_TO_ONE":
            return f"add field {object_name}.{name} (RELATION MANY_TO_ONE to {target}) with inverse {target}.{inverse}"
        return f"add field {object_name}.{name} (RELATION ONE_TO_MANY from {target}) with stored side {target}.{inverse}"

    def morph_settings(self, relation: Any) -> dict[str, Any]:
        if not isinstance(relation, dict) or not isinstance(relation.get("targets"), list) or not relation["targets"]:
            raise SchemaError("a MORPH_RELATION field needs relation.targets, a list of objects")
        unknown = sorted(set(relation) - {"targets", "multiple", "onDelete"})
        if unknown:
            raise SchemaError(f"unknown relation settings: {', '.join(unknown)}")
        targets = list(dict.fromkeys(relation["targets"]))
        for target in targets:
            self.object_def(target)
        multiple = relation.get("multiple", True)
        if not isinstance(multiple, bool):
            raise SchemaError("relation.multiple must be true or false")
        on_delete = relation.get("onDelete", "SET_NULL")
        if on_delete not in ON_DELETE:
            raise SchemaError("onDelete must be SET_NULL, CASCADE or RESTRICT")
        return {"targets": targets, "multiple": multiple, "onDelete": on_delete}

    def new_options(self, options: Any) -> list[dict[str, Any]]:
        if not isinstance(options, list) or not options:
            raise SchemaError("options must be a non-empty list")
        result = []
        for item in options:
            option = {"label": item} if isinstance(item, str) else item
            if not isinstance(option, dict):
                raise SchemaError("each option is a mapping with value, label and color")
            label = check_option_label(option.get("label"))
            value = check_option_value(option.get("value") or option_value_from_label(label))
            position = option.get("position", len(result))
            if isinstance(position, bool) or not isinstance(position, (int, float)):
                raise SchemaError(f"option {value}: position must be a number")
            result.append({"value": value, "label": label, "color": check_color(option.get("color", "gray")), "position": position})
        values = [option["value"] for option in result]
        duplicates = sorted({value for value in values if values.count(value) > 1})
        if duplicates:
            raise SchemaError(f"duplicate option values: {', '.join(duplicates)}")
        result.sort(key=lambda option: option["position"])
        renumber(result)
        return result

    def coerce_default(self, ftype: str, definition: dict[str, Any], value: Any) -> Any:
        try:
            if ftype in COMPOSITE_SUBFIELDS:
                if not isinstance(value, dict):
                    raise SchemaError(f"the default of a {ftype} field is a mapping of its subfields")
                result: dict[str, Any] = {}
                for sub, sub_value in value.items():
                    if ftype == "CURRENCY" and sub == "amount":
                        if sub_value not in (None, ""):
                            result["amountMicros"] = amount_to_micros(sub_value)
                        continue
                    if sub not in COMPOSITE_SUBFIELDS[ftype]:
                        raise SchemaError(f"{ftype} has no subfield {sub}")
                    coerced = coerce_subfield(ftype, sub, sub_value)
                    if coerced is not None:
                        result[sub] = coerced
                return result or None
            if ftype in ("RELATION", "MORPH_RELATION", "RICH_TEXT"):
                raise SchemaError(f"{ftype} fields have no default value")
            return coerce_value(ftype, definition, "default", value)
        except OperationError as exc:
            raise SchemaError(f"invalid default: {exc}") from exc

    def op_update_field(self, op: dict[str, Any]) -> None:
        self.only_keys(op, UPDATE_KEYS | {"op", "object", "field"})
        object_name, field_name = op.get("object"), op.get("field")
        definition = self.field_def(object_name, field_name)
        if not any(key in op for key in UPDATE_KEYS):
            raise SchemaError(f"update-field needs at least one of {', '.join(sorted(UPDATE_KEYS))}")
        is_inverse = definition.get("type") == "RELATION" and definition.get("relation", {}).get("type") == "ONE_TO_MANY"
        changed = []
        if "label" in op:
            definition["label"] = check_label(op["label"])
            changed.append("label")
        for key in ("description", "icon"):
            if key in op:
                if op[key] in (None, ""):
                    definition.pop(key, None)
                elif isinstance(op[key], str):
                    definition[key] = op[key].strip()
                else:
                    raise SchemaError(f"{key} must be text")
                changed.append(key)
        if "nullable" in op:
            if not isinstance(op["nullable"], bool):
                raise SchemaError("nullable must be true or false")
            if op["nullable"] is False:
                if is_inverse:
                    raise SchemaError("the computed ONE_TO_MANY side cannot be required")
                self.require_values(object_name, field_name, definition)
            definition["nullable"] = op["nullable"]
            changed.append("nullable")
        if "unique" in op:
            if not isinstance(op["unique"], bool):
                raise SchemaError("unique must be true or false")
            if op["unique"]:
                self.require_unique(object_name, field_name, definition)
            definition["unique"] = op["unique"]
            changed.append("unique")
        if "default" in op:
            if op["default"] is None:
                definition.pop("default", None)
            else:
                coerced = self.coerce_default(definition.get("type"), definition, op["default"])
                if coerced is None:
                    definition.pop("default", None)
                else:
                    definition["default"] = coerced
            changed.append("default")
        self.changes.append(f"update field {object_name}.{field_name}: {', '.join(changed)}")

    def op_add_option(self, op: dict[str, Any]) -> None:
        self.only_keys(op, {"op", "object", "field", "value", "label", "color", "position"})
        object_name, field_name, definition = self.select_field(op)
        label = check_option_label(op.get("label"))
        value = check_option_value(op.get("value") or option_value_from_label(label))
        options = definition["options"]
        if value in option_values(definition):
            raise SchemaError(f"option {value} already exists in {object_name}.{field_name}")
        self.check_reuse(definition, value)
        option = {"value": value, "label": label, "color": check_color(op.get("color", "gray"))}
        position = op.get("position")
        if position is None:
            options.append(option)
        elif isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise SchemaError("position must be a whole number from 0")
        else:
            options.insert(min(position, len(options)), option)
        renumber(options)
        self.changes.append(f"add option {object_name}.{field_name} {value} ({label}) at position {option['position']}")

    def op_update_option(self, op: dict[str, Any]) -> None:
        self.only_keys(op, {"op", "object", "field", "value", "label", "color", "position"})
        object_name, field_name, definition = self.select_field(op)
        options = definition["options"]
        option = next((item for item in options if item.get("value") == op.get("value")), None)
        if option is None:
            raise SchemaError(f"{object_name}.{field_name} has no option {op.get('value')!r}")
        if not any(key in op for key in ("label", "color", "position")):
            raise SchemaError("update-option needs label, color or position")
        if "label" in op:
            option["label"] = check_option_label(op["label"])
        if "color" in op:
            option["color"] = check_color(op["color"])
        if "position" in op:
            position = op["position"]
            if isinstance(position, bool) or not isinstance(position, int) or position < 0:
                raise SchemaError("position must be a whole number from 0")
            options.remove(option)
            options.insert(min(position, len(options)), option)
        renumber(options)
        self.changes.append(f"update option {object_name}.{field_name} {option['value']}")

    def op_rename_option(self, op: dict[str, Any]) -> None:
        self.only_keys(op, {"op", "object", "field", "from", "to", "label"})
        object_name, field_name, definition = self.select_field(op)
        old, new = op.get("from"), check_option_value(op.get("to"))
        option = next((item for item in definition["options"] if item.get("value") == old), None)
        if option is None:
            raise SchemaError(f"{object_name}.{field_name} has no option {old!r}")
        if new == old or new in option_values(definition):
            raise SchemaError(f"option {new} already exists in {object_name}.{field_name}")
        self.check_reuse(definition, new, own=option)
        option["value"] = new
        # Earlier values stay on the option, so history recorded under them still reads as this option.
        previous = [value for value in option.get("previousValues", []) if value != new]
        option["previousValues"] = list(dict.fromkeys(previous + [old]))
        if "label" in op:
            option["label"] = check_option_label(op["label"])
        self.replace_default(definition, old, new)
        change = {"op": "rename-option", "object": object_name, "field": field_name, "from": old, "to": new}
        records, in_trash = self.replace_option(object_name, field_name, old, new, change)
        self.changes.append(f"rename option {object_name}.{field_name} {old} -> {new} ({records} records, {in_trash} of them in the trash)")
        self.note_references(object_name, field_name, old)

    def op_remove_option(self, op: dict[str, Any]) -> None:
        self.only_keys(op, {"op", "object", "field", "value", "map_to"})
        object_name, field_name, definition = self.select_field(op)
        value, target = op.get("value"), op.get("map_to")
        option = next((item for item in definition["options"] if item.get("value") == value), None)
        if option is None:
            raise SchemaError(f"{object_name}.{field_name} has no option {value!r}")
        if len(definition["options"]) == 1:
            raise SchemaError("a select field keeps at least one option")
        if target is not None and (target == value or target not in option_values(definition)):
            raise SchemaError(f"map_to {target!r} must be another option of {object_name}.{field_name}")
        users = self.records_with_option(object_name, field_name, value)
        if users and target is None:
            raise SchemaError(
                f"option {value} is used by {len(users)} records (trash included); give map_to with the option they move to",
                [self.ref(record) for record in users[:50]],
            )
        default = definition.get("default")
        if (default == value or (isinstance(default, list) and value in default)) and target is None:
            raise SchemaError(f"option {value} is the default of {object_name}.{field_name}; give map_to or change the default first")
        definition["options"].remove(option)
        renumber(definition["options"])
        if target is not None:
            survivor = next(item for item in definition["options"] if item.get("value") == target)
            merged = list(survivor.get("previousValues", [])) + [value] + list(option.get("previousValues", []))
            survivor["previousValues"] = list(dict.fromkeys(item for item in merged if item != target))
            self.replace_default(definition, value, target)
            change = {"op": "remove-option", "object": object_name, "field": field_name, "from": value, "to": target}
            records, in_trash = self.replace_option(object_name, field_name, value, target, change)
            self.changes.append(f"remove option {object_name}.{field_name} {value}, {records} records moved to {target} ({in_trash} in the trash)")
        else:
            self.changes.append(f"remove unused option {object_name}.{field_name} {value}")
        self.note_references(object_name, field_name, value)

    @staticmethod
    def check_reuse(definition: dict[str, Any], value: str, own: Optional[dict[str, Any]] = None) -> None:
        for option in definition.get("options", []):
            if option is not own and value in (option.get("previousValues") or []):
                raise SchemaError(
                    f"{value} is an earlier value of option {option.get('value')}; reusing it would mix both histories, choose another value"
                )

    @staticmethod
    def replace_default(definition: dict[str, Any], old: str, new: str) -> None:
        default = definition.get("default")
        if default == old:
            definition["default"] = new
        elif isinstance(default, list) and old in default:
            definition["default"] = list(dict.fromkeys(new if item == old else item for item in default))

    def op_set_active(self, op: dict[str, Any], active: bool) -> None:
        self.only_keys(op, {"op", "object", "field"})
        object_name = op.get("object")
        word = "activate" if active else "deactivate"
        if op.get("field") is None:
            definition = self.object_def(object_name)
            if definition.get("active", True) is active:
                self.warnings.append(f"object {object_name} is already {'active' if active else 'deactivated'}")
                return
            definition["active"] = active
            self.changes.append(f"{word} object {object_name}; its records stay unchanged")
            if not active:
                self.note_references(object_name)
            return
        field_name = op["field"]
        definition = self.field_def(object_name, field_name)
        if definition.get("active", True) is active:
            self.warnings.append(f"field {object_name}.{field_name} is already {'active' if active else 'deactivated'}")
            return
        if not active and self.objects[object_name].get("labelField") == field_name:
            raise SchemaError(f"{object_name}.{field_name} is the label field and cannot be deactivated")
        if active and definition.get("nullable") is False:
            self.require_values(object_name, field_name, definition)
        definition["active"] = active
        partner = None
        if definition.get("type") == "RELATION":
            relation = definition.get("relation", {})
            partner_def = self.objects.get(relation.get("target"), {}).get("fields", {}).get(relation.get("inverse"))
            if isinstance(partner_def, dict):
                partner_def["active"] = active
                partner = f"{relation.get('target')}.{relation.get('inverse')}"
        self.changes.append(f"{word} field {object_name}.{field_name}" + (f" and its other side {partner}" if partner else "") + "; values stay unchanged")
        if not active:
            self.note_references(object_name, field_name)

    def op_delete_field(self, op: dict[str, Any]) -> None:
        self.only_keys(op, {"op", "object", "field"})
        object_name, field_name = op.get("object"), op.get("field")
        definition = self.field_def(object_name, field_name)
        if definition.get("standard") is True:
            raise SchemaError(f"{object_name}.{field_name} is a standard field; deactivate it instead of deleting it")
        if self.objects[object_name].get("labelField") == field_name:
            raise SchemaError(f"{object_name}.{field_name} is the label field and cannot be deleted")
        change = {"op": "delete-field", "object": object_name, "field": field_name}
        removed = [f"{object_name}.{field_name}"]
        if definition.get("type") == "RELATION":
            relation = definition["relation"]
            partner_object, partner_name = relation.get("target"), relation.get("inverse")
            partner = self.objects.get(partner_object, {}).get("fields", {}).get(partner_name)
            if isinstance(partner, dict) and partner.get("standard") is True:
                raise SchemaError(f"the other side {partner_object}.{partner_name} is a standard field; deactivate the relation instead")
            if relation.get("type") == "MANY_TO_ONE":
                cleared = self.clear_field(object_name, field_name, definition, change)
            else:
                cleared = self.clear_field(partner_object, partner_name, partner, change) if isinstance(partner, dict) else 0
            if isinstance(partner, dict):
                del self.objects[partner_object]["fields"][partner_name]
                removed.append(f"{partner_object}.{partner_name}")
        else:
            cleared = self.clear_field(object_name, field_name, definition, change)
        del self.objects[object_name]["fields"][field_name]
        self.destructive = True
        self.destructive_records = self.destructive_records or bool(cleared)
        self.changes.append(f"delete field {', '.join(removed)} ({definition.get('type')}); values removed from {cleared} records")
        self.note_references(object_name, field_name)

    def op_delete_object(self, op: dict[str, Any]) -> None:
        self.only_keys(op, {"op", "object"})
        object_name = op.get("object")
        definition = self.object_def(object_name)
        if definition.get("standard") is True:
            raise SchemaError(f"{object_name} is a standard object; deactivate it instead of deleting it")
        records = self.current_records(object_name)
        history = sum(1 for event in read_events(self.target) if event.get("object") == object_name)
        if (records or history) and not core_accepts_tombstones():
            raise SchemaError(
                f"{object_name} has {len(records)} records and {history} events. Its history stays in the event log, which "
                "crm_contract.validate_event would then report as an unknown object on every lint. Deleting an object "
                f"with records or history needs the core to accept datamodel.{TOMBSTONES}; deactivate the object meanwhile"
            )
        change = {"op": "delete-object", "object": object_name}
        cleared_fields = []
        for other_name, other in self.objects.items():
            if other_name == object_name:
                continue
            for field_name, field_def in list(other["fields"].items()):
                ftype = field_def.get("type")
                relation = field_def.get("relation", {})
                if ftype == "RELATION" and relation.get("target") == object_name:
                    if relation.get("type") == "MANY_TO_ONE":
                        self.clear_field(other_name, field_name, field_def, {**change, "field": f"{other_name}.{field_name}"})
                    del other["fields"][field_name]
                    cleared_fields.append(f"{other_name}.{field_name}")
                elif ftype == "MORPH_RELATION" and object_name in relation.get("targets", []):
                    if relation["targets"] == [object_name]:
                        self.clear_field(other_name, field_name, field_def, {**change, "field": f"{other_name}.{field_name}"})
                        del other["fields"][field_name]
                        cleared_fields.append(f"{other_name}.{field_name}")
                    else:
                        relation["targets"] = [target for target in relation["targets"] if target != object_name]
                        self.drop_links(other_name, field_name, object_name, {**change, "field": f"{other_name}.{field_name}"})
                        cleared_fields.append(f"{other_name}.{field_name} (target {object_name} removed)")
        for record in records:
            key = (record.object, record.id)
            self.working.pop(key, None)
            self.destroyed.add(key)
            self.emit("destroy", record, {"crm_title": [record.data.get("crm_title"), None]}, change)
        del self.objects[object_name]
        if records or history:
            tombstones = self.raw.setdefault(TOMBSTONES, {})
            tombstones[object_name] = {
                "directory": f"{RECORDS_DIR}/{kebab(object_name)}",
                "labelSingular": definition.get("labelSingular"),
                "labelPlural": definition.get("labelPlural"),
                "deletedAt": self.now,
            }
        self.remove_dirs.append(f"{RECORDS_DIR}/{kebab(object_name)}")
        if records:
            self.destructive = True
            self.destructive_records = True
        detail = f"; relation fields removed: {', '.join(cleared_fields)}" if cleared_fields else ""
        self.changes.append(f"delete object {object_name} with {len(records)} records{detail}")
        self.note_references(object_name)

    def drop_links(self, object_name: str, field_name: str, removed_object: str, schema_change: dict[str, Any]) -> None:
        directory = kebab(removed_object)
        for record in self.current_records(object_name):
            value = record.data.get(field_name)
            links = value if isinstance(value, list) else ([value] if value else [])
            kept = [link for link in links if not (isinstance(link, str) and (parse_record_link(link) or ("", ""))[0] == directory)]
            if len(kept) == len(links):
                continue
            record = self.touch(record)
            changes: dict[str, Any] = {}
            self.set_value(record, field_name, kept if isinstance(value, list) else (kept[0] if kept else None), changes)
            self.emit("update", record, changes, schema_change)

    # -- run ------------------------------------------------------------------

    def run(self, operations: list[Any]) -> None:
        handlers = {
            "add-object": self.op_add_object,
            "update-object": self.op_update_object,
            "add-field": self.op_add_field,
            "update-field": self.op_update_field,
            "add-option": self.op_add_option,
            "update-option": self.op_update_option,
            "rename-option": self.op_rename_option,
            "remove-option": self.op_remove_option,
            "deactivate": lambda op: self.op_set_active(op, False),
            "activate": lambda op: self.op_set_active(op, True),
            "delete-field": self.op_delete_field,
            "delete-object": self.op_delete_object,
        }
        for index, op in enumerate(operations):
            name = op.get("op") if isinstance(op, dict) else None
            if name not in handlers:
                self.errors.append({"operation": index, "error": f"unknown op {name!r}; use one of {', '.join(OPERATIONS)}"})
                continue
            checkpoint = (
                copy.deepcopy(self.raw), {key: record.copy() for key, record in self.working.items()}, set(self.destroyed),
                len(self.events), len(self.changes), len(self.references), list(self.remove_dirs),
                self.destructive, self.destructive_records,
            )
            try:
                handlers[name](op)
            except (SchemaError, OperationError, CrmError) as exc:
                (self.raw, self.working, self.destroyed, events, changes, references, self.remove_dirs,
                 self.destructive, self.destructive_records) = checkpoint
                del self.events[events:]
                del self.changes[changes:]
                del self.references[references:]
                entry: dict[str, Any] = {"operation": index, "op": name, "error": str(exc)}
                if isinstance(exc, SchemaError) and exc.details:
                    entry["details"] = exc.details
                self.errors.append(entry)

    def validate(self, after: DataModel) -> None:
        for problem in validate_datamodel(after.raw):
            self.errors.append({"operation": None, "error": problem})
        if self.errors:
            return
        existing: dict[tuple[str, str], str] = {}
        for object_name in after.objects:
            directory = self.target / after.directory(object_name)
            if directory.is_dir():
                for path in directory.glob("*.md"):
                    existing[(kebab(object_name), path.stem)] = object_name

        def exists(object_dir: str, record_id: str) -> Optional[str]:
            name = existing.get((object_dir, record_id))
            return None if name is None or (name, record_id) in self.destroyed else name

        for key, record in sorted(self.working.items()):
            if key in self.destroyed:
                continue
            for problem in validate_record(after, record, exists_check=exists):
                self.errors.append({"operation": None, "record": record.id, "error": problem})
        for object_name in sorted({key[0] for key in self.working if key[0] in after.objects}):
            for field_name, definition in unique_fields(after, object_name):
                try:
                    self.require_unique(object_name, field_name, definition)
                except SchemaError as exc:
                    self.errors.append({"operation": None, "error": str(exc), "details": exc.details})

    def transaction_plan(self, after: DataModel) -> Optional[dict[str, Any]]:
        """The record half of the change as a crm_contract transaction plan bound to the current model."""
        files = []
        for key, record in sorted(self.working.items()):
            if key in self.destroyed:
                continue
            content = compose_record(after, record)
            path = self.target / record.path
            before = path.read_bytes() if path.is_file() else b""
            if before.decode("utf-8", errors="replace") == content:
                continue
            files.append({
                "path": record.path, "before_exists": path.is_file(),
                "before_sha256": sha256_bytes(before) if path.is_file() else "",
                "after": content, "after_sha256": sha256_text(content),
            })
        for object_name, record_id in sorted(self.destroyed):
            relative = record_relative_path(self.old, object_name, record_id)
            path = self.target / relative
            if path.is_file():
                files.append({"path": relative, "before_exists": True, "before_sha256": sha256_bytes(path.read_bytes()), "after": None, "after_sha256": ""})
        if not files and not self.events:
            return None
        shards: dict[str, list[dict[str, Any]]] = {}
        for event in self.events:
            shards.setdefault(event_shard(event["at"]), []).append(event)
        event_shards = []
        for shard, appended in sorted(shards.items()):
            path = self.target / shard
            event_shards.append({
                "path": shard, "before_exists": path.is_file(),
                "before_sha256": sha256_bytes(path.read_bytes()) if path.is_file() else "", "append": appended,
            })
        summary: dict[str, int] = {}
        for event in self.events:
            summary[event["op"]] = summary.get(event["op"], 0) + 1
        payload = {
            "format": PLAN_FORMAT,
            "datamodel_sha256": sha256_bytes(self.before_bytes),
            "actor": self.actor,
            "origin": {"kind": "migration", "occasion": self.occasion},
            "transaction": self.txn,
            "created_at": self.now,
            "files": files,
            "event_shards": event_shards,
            "event_rewrites": [],
            "summary": summary,
            "destructive": self.destructive_records,
            "erases": [],
            "history_purge": [],
            "errors": [],
            "warnings": [],
        }
        return {**payload, "plan_sha256": sha256_bytes(canonical_json(payload))}


def plan_schema(target: Path, request: Any, *, now: Optional[str] = None) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise CrmError("the request must be a JSON object with actor and operations")
    actor = request.get("actor")
    if not valid_actor(actor):
        raise CrmError("request.actor must be human:<id>, agent/<name> or process:<id>")
    operations = request.get("operations")
    if not isinstance(operations, list) or not operations:
        raise CrmError("request.operations must be a non-empty list")
    occasion = str(request.get("occasion") or "data model change").strip()[:200]
    planner = SchemaPlanner(target, actor, occasion, now)
    planner.run(operations)
    text = dump_datamodel(planner.raw)
    after = DataModel(planner.raw, sha256_text(text))
    if not planner.errors:
        planner.validate(after)
    if not planner.errors and text.encode("utf-8") == planner.before_bytes and not planner.events:
        planner.errors.append({"operation": None, "error": "the requested operations change nothing"})
    transaction = planner.transaction_plan(after) if not planner.errors else None
    if planner.references:
        planner.warnings.append(
            "views, dashboards, workflows or roles name what changes; update them before the next release, lint reports what stays invalid"
        )
    payload = {
        "format": SCHEMA_PLAN_FORMAT,
        "actor": actor,
        "occasion": occasion,
        "created_at": planner.now,
        "operations": operations,
        "changes": planner.changes,
        "datamodel_path": DATAMODEL_PATH,
        "datamodel_before_sha256": sha256_bytes(planner.before_bytes),
        "datamodel_after": text,
        "datamodel_after_sha256": sha256_text(text),
        "record_transaction": transaction,
        "remove_directories": sorted(set(planner.remove_dirs)),
        "destructive": planner.destructive,
        "summary": {
            "operations": len(operations),
            "records_changed": sum(1 for entry in (transaction or {}).get("files", []) if entry["after"] is not None),
            "records_removed": sum(1 for entry in (transaction or {}).get("files", []) if entry["after"] is None),
            "events": len(planner.events) if transaction else 0,
        },
        "dependent_references": sorted(set(planner.references)),
        "errors": planner.errors,
        "warnings": planner.warnings,
    }
    return {**payload, "plan_sha256": sha256_bytes(canonical_json(payload))}


def verify_schema_plan(plan: Any, expected: str) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("format") != SCHEMA_PLAN_FORMAT:
        raise CrmError("unsupported CRM schema plan")
    payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
    actual = sha256_bytes(canonical_json(payload))
    if not expected or plan.get("plan_sha256") != expected or actual != expected:
        raise StalePlanError("CRM schema plan is stale or was modified")
    return plan


def remove_empty_directories(target: Path, directories: list[str]) -> tuple[list[str], list[str]]:
    removed, kept = [], []
    for relative in directories:
        path = target / relative
        if not relative.startswith(f"{RECORDS_DIR}/") or not path.is_dir() or path.is_symlink():
            continue
        entries = list(path.iterdir())
        if any(entry.name not in OS_ARTIFACTS or not entry.is_file() for entry in entries):
            kept.append(relative)
            continue
        for entry in entries:
            entry.unlink()
        path.rmdir()
        removed.append(relative)
    return removed, kept


def apply_schema(target: Path, token: str, plan: dict[str, Any], *, confirm_destructive: bool = False) -> tuple[dict[str, Any], int]:
    from snapshot_wiki import create_snapshot

    require_lock(target, token)
    if plan.get("errors"):
        return {"state": "invalid_plan", "writes": 0, "reason": "the plan contains errors; correct the request and plan again", "errors": plan["errors"][:50]}, 4
    if plan.get("destructive") and not confirm_destructive:
        return {
            "state": "confirmation_required", "writes": 0,
            "reason": "deleting fields or objects removes data and needs explicit user confirmation (--confirm-destructive)",
        }, 3
    path = target / DATAMODEL_PATH
    if not path.is_file() or sha256_bytes(path.read_bytes()) != plan.get("datamodel_before_sha256"):
        return {"state": "stale_plan", "writes": 0, "reason": "the data model changed after planning"}, 3
    text = plan.get("datamodel_after")
    if not isinstance(text, str) or sha256_text(text) != plan.get("datamodel_after_sha256"):
        return {"state": "stale_plan", "writes": 0, "reason": "the planned data model was modified"}, 3
    problems = validate_datamodel(json.loads(text))
    if problems:
        return {"state": "invalid_plan", "writes": 0, "errors": problems[:50]}, 4
    transaction = plan.get("record_transaction")
    record_result: Optional[dict[str, Any]] = None
    if transaction:
        try:
            verify_plan(transaction, str(transaction.get("plan_sha256") or ""))
        except CrmError as exc:
            return {"state": "invalid_plan", "writes": 0, "reason": str(exc)}, 4
        if transaction.get("datamodel_sha256") != plan.get("datamodel_before_sha256"):
            return {"state": "invalid_plan", "writes": 0, "reason": "the record transaction is bound to another data model"}, 4
        # Records first: apply_transaction checks every precondition before its first write.
        record_result, code = apply_transaction(target, token, transaction, confirm_destructive=confirm_destructive)
        if code != 0:
            return {"state": record_result.get("state"), "writes": record_result.get("writes", 0), "record_transaction": record_result}, code
    snapshot = create_snapshot(target, token, operation="crm-schema", selected_files=[DATAMODEL_PATH])
    try:
        portable_io.atomic_write_bytes(path, text.encode("utf-8"))
    except OSError as exc:
        return {
            "state": "partial_failure",
            "reason": f"the records were migrated but writing {DATAMODEL_PATH} failed: {exc}",
            "record_transaction": record_result,
            "snapshot": snapshot,
            "recovery_required": True,
        }, 5
    removed, kept = remove_empty_directories(target, list(plan.get("remove_directories") or []))
    result: dict[str, Any] = {
        "state": "applied",
        "plan_sha256": plan.get("plan_sha256"),
        "changes": plan.get("changes", []),
        "datamodel": DATAMODEL_PATH,
        "datamodel_sha256": plan.get("datamodel_after_sha256"),
        "snapshot": snapshot,
        "record_transaction": record_result,
        "removed_directories": removed,
        "dependent_references": plan.get("dependent_references", []),
        "views_stale": True,
        "next_step": "update dependent views, dashboards and workflows, rebuild the CRM views (crm_build.py), lint, and publish a minor release",
    }
    if kept:
        result["warnings"] = [f"{relative} still holds files and was kept" for relative in kept]
    return result, 0


def outside(target: Path, path: Path) -> bool:
    return path != target and target not in path.parents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--target", required=True)
    plan_parser.add_argument("--lock-token", required=True)
    plan_parser.add_argument("--request-file", required=True)
    plan_parser.add_argument("--output", required=True)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--target", required=True)
    apply_parser.add_argument("--lock-token", required=True)
    apply_parser.add_argument("--plan-file", required=True)
    apply_parser.add_argument("--expect-plan-sha256", required=True)
    apply_parser.add_argument("--confirm-destructive", action="store_true")
    args = parser.parse_args()
    target = Path(args.target).expanduser().resolve()
    require_lock(target, args.lock_token)
    try:
        if args.command == "plan":
            output = Path(args.output).expanduser().resolve()
            if not outside(target, output):
                raise CrmError("the plan must be written outside the wiki")
            request = json.loads(Path(args.request_file).read_text(encoding="utf-8"))
            plan = plan_schema(target, request)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            report = {
                "state": "invalid" if plan["errors"] else "planned",
                "plan_sha256": plan["plan_sha256"],
                "plan_file": output.name,
                "changes": plan["changes"],
                "summary": plan["summary"],
                "destructive": plan["destructive"],
                "dependent_references": plan["dependent_references"],
                "errors": plan["errors"][:100],
                "error_count": len(plan["errors"]),
                "warnings": plan["warnings"][:50],
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1 if plan["errors"] else 0
        plan = verify_schema_plan(json.loads(Path(args.plan_file).read_text(encoding="utf-8")), args.expect_plan_sha256)
        result, code = apply_schema(target, args.lock_token, plan, confirm_destructive=args.confirm_destructive)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except StalePlanError as exc:
        print(json.dumps({"state": "stale_plan", "writes": 0, "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 3
    except (OSError, json.JSONDecodeError, CrmError) as exc:
        print(json.dumps({"state": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
