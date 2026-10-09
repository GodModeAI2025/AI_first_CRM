#!/usr/bin/env python3
"""Export CRM records as import-compatible CSV or JSON, as a sample file, or as note and task links.

csv        one object, optionally through a saved view (its filter, sort and visible
           fields). --format standard writes import-compatible columns: Id first,
           'Label / Sublabel' for composite subfields, 'Label Id' for relations on
           the many side, dot decimals, TRUE/FALSE, option API names, ISO dates,
           UTF-8 without BOM, comma separated. --format plain writes the wiki's
           labels and readable values for spreadsheets (UTF-8 with BOM, semicolon
           where the wiki uses decimal commas). More rows than --max-rows (default
           20,000, a common export limit) go into numbered files.
json       API form: one object per record with nested composite values,
           <relation>Id for relations, createdAt and updatedAt.
sample     a CSV template with every importable column and one example row, like
           a CRM's sample file for imports.
junctions  noteTargets or taskTargets rows (id, noteId or taskId, targetPersonId,
           targetCompanyId, targetOpportunityId) for the targets field, which other CRMs
           keeps as junction records.

The command only reads the wiki; every output file lies outside it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import csv
import hashlib
import io
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from crm_contract import (
    COMPOSITE_SUBFIELDS, SETTINGS_PATH, VIEWS_PATH, CrmError, DataModel, Record, RecordStore, load_datamodel,
    load_json_file, option_values, parse_instant, parse_record_link, utc_now,
)
from crm_filters import Context, FilterError, select_records, validate_filter
from crm_tabular import (
    GERMAN_SUBFIELD_LABELS, STANDARD_SUBFIELD_LABELS, STANDARD_SYSTEM_LABELS, decimal_text, humanize, injection_guard,
    standard_column, zone,
)
from wiki_lock import require_lock

EXPORT_FILE_LIMIT = 20000
REIMPORT_FILE_LIMIT = 10000
SYSTEM_KEYS = ("crm_created_at", "crm_updated_at", "crm_deleted_at", "crm_created_by", "crm_updated_by")
PLAIN_SYSTEM_LABELS = {
    "de": {"id": "ID", "crm_created_at": "Erstellt am", "crm_updated_at": "Geändert am", "crm_deleted_at": "Gelöscht am",
           "crm_created_by": "Erstellt von", "crm_updated_by": "Geändert von"},
    "en": {"id": "ID", "crm_created_at": "Created at", "crm_updated_at": "Updated at", "crm_deleted_at": "Deleted at",
           "crm_created_by": "Created by", "crm_updated_by": "Updated by"},
}
JUNCTION_NAMESPACE = uuid.UUID("6f1c2d6e-3b8a-5e4f-9a51-0c2b7d1e8f30")


class ExportError(ValueError):
    """The export cannot be produced as requested."""


@dataclass
class Column:
    header: str
    key: str
    field: str = ""
    sub: str = ""
    ftype: str = ""


def outside(target: Path, path: Path) -> bool:
    return path != target and target not in path.parents


def wiki_language(target: Path) -> str:
    try:
        from frontmatter_contract import parse_file

        language = str(parse_file(target / "schema/WIKI_PROFILE.md").data.get("wiki_language") or "en")
    except Exception:  # noqa: BLE001 - a missing profile only changes labels
        language = "en"
    return "de" if language.lower().startswith("de") else "en"


# ---------------------------------------------------------------------------
# columns


def default_keys(datamodel: DataModel, object_name: str, include_deleted: bool) -> list[str]:
    keys = [name for name, definition in datamodel.stored_fields(object_name) if definition.get("active", True) is not False]
    keys += ["crm_created_at", "crm_updated_at"]
    if include_deleted:
        keys.append("crm_deleted_at")
    return keys


def view_keys(view: dict[str, Any], include_deleted: bool) -> list[str]:
    keys = []
    for item in view.get("fields") or []:
        if isinstance(item, dict):
            if item.get("visible") is False or item.get("isVisible") is False:
                continue
            item = item.get("field") or item.get("key") or item.get("name")
        if isinstance(item, str) and item not in keys:
            keys.append(item)
    if include_deleted and "crm_deleted_at" not in keys:
        keys.append("crm_deleted_at")
    return keys


def columns_for(datamodel: DataModel, object_name: str, keys: list[str], fmt: str, language: str,
                *, with_id: bool = True) -> tuple[list[Column], list[dict[str, str]]]:
    standard = fmt == "standard"
    columns: list[Column] = []
    omitted: list[dict[str, str]] = []
    if with_id:
        columns.append(Column(STANDARD_SYSTEM_LABELS["id"] if standard else PLAIN_SYSTEM_LABELS[language]["id"], "id"))
    fields = datamodel.fields(object_name)
    for key in keys:
        if key == "id":
            continue
        if key in SYSTEM_KEYS:
            if standard and key in STANDARD_SYSTEM_LABELS:
                columns.append(Column(STANDARD_SYSTEM_LABELS[key], key))
            elif standard:
                omitted.append({"field": key, "reason": "Der Akteur ist ein eigenes Objekt; nicht exportiert"})
            else:
                columns.append(Column(PLAIN_SYSTEM_LABELS[language][key], key))
            continue
        base, _, sub = key.partition(".")
        definition = fields.get(base)
        if definition is None:
            omitted.append({"field": key, "reason": "nicht im Datenmodell"})
            continue
        if definition.get("active", True) is False:
            omitted.append({"field": key, "reason": "Feld deaktiviert"})
            continue
        ftype = definition["type"]
        label = str(definition.get("label") or humanize(base))
        if ftype == "RELATION":
            if definition["relation"].get("type") == "ONE_TO_MANY":
                omitted.append({"field": base, "reason": "1:n-Gegenseite; Relations-IDs stehen nur auf der n-Seite"})
                continue
            columns.append(Column(standard_column(object_name, base, definition) if standard else label, base, base, "", ftype))
            continue
        if ftype == "MORPH_RELATION":
            if standard:
                omitted.append({"field": base, "reason": "Verknüpfungen mit mehreren Objekttypen enthält der Standardexport nicht; dafür gibt es junctions"})
            else:
                columns.append(Column(label, base, base, "", ftype))
            continue
        if ftype in COMPOSITE_SUBFIELDS:
            subs = [sub] if sub else list(COMPOSITE_SUBFIELDS[ftype])
            for item in subs:
                if item not in COMPOSITE_SUBFIELDS[ftype]:
                    omitted.append({"field": key, "reason": "unbekanntes Unterfeld"})
                    continue
                if standard:
                    header = standard_column(object_name, base, definition, item)
                else:
                    sub_labels = GERMAN_SUBFIELD_LABELS if language == "de" else STANDARD_SUBFIELD_LABELS
                    header = f"{label} / {sub_labels[ftype][item]}"
                columns.append(Column(header, f"{base}.{item}", base, item, ftype))
            continue
        if ftype == "RICH_TEXT":
            columns.append(Column(standard_column(object_name, base, definition, "markdown") if standard else label, base, base, "", ftype))
            continue
        columns.append(Column(standard_column(object_name, base, definition) if standard else label, base, base, "", ftype))
    return columns, omitted


# ---------------------------------------------------------------------------
# values


def number_text(value: Any) -> str:
    if value is None or value == "" or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return decimal_text(Decimal(repr(value)))
    return str(value)


def micros_text(value: Any) -> str:
    if value is None or value == "" or isinstance(value, bool):
        return ""
    return decimal_text(Decimal(int(value)) / Decimal(1_000_000))


def link_id(value: Any) -> str:
    parsed = parse_record_link(value) if isinstance(value, str) else None
    return parsed[1] if parsed else ""


def link_title(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    parts = value.strip()[2:-2].split("|", 1)
    return parts[1] if len(parts) == 2 else link_id(value)


def standard_value(record: Record, column: Column) -> str:
    data = record.data
    if column.key == "id":
        return record.id
    if column.key in SYSTEM_KEYS:
        return str(data.get(column.key) or "")
    ftype = column.ftype
    if ftype == "RELATION":
        return link_id(data.get(column.field))
    if ftype == "RICH_TEXT":
        return injection_guard(record.richtext.get(column.field, "").strip("\n"))
    if ftype in COMPOSITE_SUBFIELDS:
        value = data.get(column.key)
        if ftype == "CURRENCY" and column.sub == "amountMicros":
            return micros_text(value)
        if ftype == "ADDRESS" and column.sub in {"addressLat", "addressLng"}:
            return number_text(value)
        if isinstance(value, list):
            return json.dumps(value, ensure_ascii=False) if value else ""
        if column.sub in {"secondaryLinks", "additionalPhones"}:
            return "" if value in (None, "", "[]") else str(value)
        return injection_guard(str(value or ""))
    value = data.get(column.field)
    if value is None:
        return ""
    if ftype == "BOOLEAN":
        return "TRUE" if value is True else ("FALSE" if value is False else "")
    if ftype == "NUMBER":
        return number_text(value)
    if ftype in {"MULTI_SELECT", "ARRAY"}:
        return json.dumps(value, ensure_ascii=False) if value else ""
    if ftype in {"TEXT", "UUID"}:
        return injection_guard(str(value))
    return str(value)


class PlainFormatter:
    """Readable values in the wiki's language and number, date and time formats."""

    def __init__(self, datamodel: DataModel, settings: dict[str, Any], language: str):
        self.datamodel = datamodel
        self.language = language
        self.comma = settings.get("number_format") in {"DOTS_AND_COMMA", "SPACES_AND_COMMA"}
        self.date_format = settings.get("date_format") or ("DAY_FIRST" if language == "de" else "MONTH_FIRST")
        self.zone = zone(settings.get("time_zone") or "UTC")

    def number(self, text: str) -> str:
        return text.replace(".", ",") if self.comma else text

    def day(self, iso: str) -> str:
        # DD.MM.YYYY is unambiguous; month-first dates would need --date-format on reimport, so they stay ISO.
        if len(iso) != 10 or self.date_format != "DAY_FIRST":
            return iso
        year, month, day = iso.split("-")
        return f"{day}.{month}.{year}"

    def instant(self, value: str) -> str:
        moment = parse_instant(value)
        if moment is None:
            return value
        local = moment.astimezone(self.zone) if self.zone is not None else moment
        return f"{self.day(local.date().isoformat())} {local.strftime('%H:%M')}"

    def value(self, record: Record, column: Column) -> str:
        data = record.data
        if column.key == "id":
            return record.id
        if column.key in {"crm_created_at", "crm_updated_at", "crm_deleted_at"}:
            return self.instant(str(data.get(column.key))) if data.get(column.key) else ""
        if column.key in SYSTEM_KEYS:
            return str(data.get(column.key) or "")
        ftype = column.ftype
        if ftype == "RELATION":
            return link_title(data.get(column.field))
        if ftype == "MORPH_RELATION":
            value = data.get(column.field)
            items = value if isinstance(value, list) else ([value] if value else [])
            return "; ".join(link_title(item) for item in items)
        if ftype == "RICH_TEXT":
            return injection_guard(record.richtext.get(column.field, "").strip("\n"))
        if ftype in COMPOSITE_SUBFIELDS:
            value = data.get(column.key)
            if ftype == "CURRENCY" and column.sub == "amountMicros":
                return self.number(micros_text(value))
            if ftype == "ADDRESS" and column.sub in {"addressLat", "addressLng"}:
                return self.number(number_text(value))
            if isinstance(value, list):
                return "; ".join(str(item) for item in value)
            return injection_guard(str(value or ""))
        value = data.get(column.field)
        if value is None:
            return ""
        definition = self.datamodel.fields(record.object)[column.field]
        if ftype == "BOOLEAN":
            return {True: "ja", False: "nein"}.get(value, "") if self.language == "de" else {True: "yes", False: "no"}.get(value, "")
        if ftype in {"NUMBER", "NUMERIC"}:
            return self.number(number_text(value))
        if ftype == "DATE":
            return self.day(str(value))
        if ftype == "DATE_TIME":
            return self.instant(str(value))
        if ftype in {"SELECT", "MULTI_SELECT"}:
            labels = {option["value"]: option.get("label") or option["value"] for option in definition.get("options", [])}
            items = value if isinstance(value, list) else [value]
            return "; ".join(str(labels.get(item, item)) for item in items)
        if ftype == "ARRAY":
            return "; ".join(str(item) for item in value)
        if ftype in {"TEXT", "UUID"}:
            return injection_guard(str(value))
        return str(value)


# ---------------------------------------------------------------------------
# writing


def csv_bytes(headers: list[str], rows: list[list[str]], *, delimiter: str, bom: bool) -> bytes:
    buffer = io.StringIO()
    # CRLF as line terminator makes the writer quote every cell with a line break or carriage return.
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)
    text = buffer.getvalue()
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def write_chunks(output: Path, headers: list[str], rows: list[list[str]], max_rows: int, *, delimiter: str, bom: bool) -> list[dict[str, Any]]:
    output.parent.mkdir(parents=True, exist_ok=True)
    chunks = [rows[start:start + max_rows] for start in range(0, len(rows), max_rows)] or [[]]
    files = []
    for number, chunk in enumerate(chunks, 1):
        path = output if len(chunks) == 1 else output.with_name(f"{output.stem}-{number}{output.suffix or '.csv'}")
        content = csv_bytes(headers, chunk, delimiter=delimiter, bom=bom)
        path.write_bytes(content)
        files.append({"file": path.name, "rows": len(chunk), "sha256": hashlib.sha256(content).hexdigest()})
    return files


def ordered_records(store: RecordStore, object_name: str) -> list[Record]:
    return sorted(store.records(object_name).values(), key=lambda record: (str(record.data.get("crm_created_at") or ""), record.id))


def find_view(target: Path, object_name: str, name: str) -> dict[str, Any]:
    views = load_json_file(target, VIEWS_PATH, {"views": []})
    for view in views.get("views", []) if isinstance(views, dict) else []:
        if isinstance(view, dict) and (view.get("id") == name or str(view.get("label", "")).casefold() == name.casefold()):
            if view.get("object") != object_name:
                raise ExportError(f"die Ansicht {name!r} gehört zum Objekt {view.get('object')}, nicht zu {object_name}")
            return view
    raise ExportError(f"Ansicht {name!r} nicht gefunden")


# ---------------------------------------------------------------------------
# commands


def export_csv(target: Path, args: argparse.Namespace) -> dict[str, Any]:
    datamodel = load_datamodel(target)
    datamodel.object(args.object)
    settings = load_json_file(target, SETTINGS_PATH, {}) or {}
    language = wiki_language(target)
    store = RecordStore(target, datamodel)
    records = ordered_records(store, args.object)
    report: dict[str, Any] = {"object": args.object, "format": args.format}
    notes: list[str] = []
    if args.view:
        view = find_view(target, args.object, args.view)
        spec = view.get("filter") if view.get("filter") is not None else view.get("filters")
        sorts = view.get("sort") or view.get("sorts")
        problems = validate_filter(datamodel, args.object, spec)
        if problems:
            raise ExportError("Filter der Ansicht ungültig: " + "; ".join(problems))
        context = Context(now=utc_now(), time_zone=settings.get("time_zone") or "UTC", me=args.me, week_start=int(settings.get("calendar_start_day") or 1))
        chosen = select_records(datamodel, records, filter_spec=spec, sorts=sorts, context=context, include_deleted=args.include_deleted)
        keys = view_keys(view, args.include_deleted) or default_keys(datamodel, args.object, args.include_deleted)
        report["view"] = view.get("id")
        if spec:
            report["evaluated_at"] = {"now": context.now.isoformat().replace("+00:00", "Z"), "time_zone": context.time_zone_name}
    else:
        chosen = select_records(datamodel, records, include_deleted=args.include_deleted)
        keys = default_keys(datamodel, args.object, args.include_deleted)
    columns, omitted = columns_for(datamodel, args.object, keys, args.format, language)
    if args.format == "standard":
        rows = [[standard_value(record, column) for column in columns] for record in chosen]
        delimiter, bom = ",", False
    else:
        formatter = PlainFormatter(datamodel, settings, language)
        rows = [[formatter.value(record, column) for column in columns] for record in chosen]
        delimiter, bom = (";" if formatter.comma else ","), True
    output = Path(args.output).expanduser().resolve()
    if not outside(target, output):
        raise ExportError("die Exportdatei muss außerhalb des Wikis liegen")
    max_rows = args.max_rows or EXPORT_FILE_LIMIT
    files = write_chunks(output, [column.header for column in columns], rows, max_rows, delimiter=delimiter, bom=bom)
    if len(files) > 1:
        notes.append(f"{len(rows)} Zeilen auf {len(files)} Dateien zu höchstens {max_rows} Zeilen verteilt.")
    if args.format == "standard" and any(item["rows"] > REIMPORT_FILE_LIMIT for item in files):
        notes.append(f"Viele CRM-Importe nehmen höchstens {REIMPORT_FILE_LIMIT} Datensätze je Datei; für den Reimport --max-rows {REIMPORT_FILE_LIMIT} verwenden.")
    if args.include_deleted:
        notes.append("Gelöschte Datensätze sind enthalten; ein Reimport stellt sie wieder her (übliches CRM-Verhalten).")
    report.update({
        "state": "exported",
        "rows": len(rows),
        "files": files,
        "columns": [column.header for column in columns],
        "omitted": omitted,
        "delimiter": "TAB" if delimiter == "\t" else delimiter,
        "encoding": "utf-8-sig" if bom else "utf-8",
        "notes": notes,
    })
    return report


def api_value(record: Record, field_name: str, definition: dict[str, Any]) -> tuple[Optional[str], Any]:
    data = record.data
    ftype = definition["type"]
    if ftype == "RELATION":
        if definition["relation"].get("type") == "ONE_TO_MANY":
            return None, None
        return f"{field_name}Id", link_id(data.get(field_name)) or None
    if ftype == "MORPH_RELATION":
        return None, None
    if ftype == "RICH_TEXT":
        text = record.richtext.get(field_name, "").strip("\n")
        return field_name, {"blocknote": None, "markdown": text or None}
    if ftype in COMPOSITE_SUBFIELDS:
        composite: dict[str, Any] = {}
        for sub in COMPOSITE_SUBFIELDS[ftype]:
            value = data.get(f"{field_name}.{sub}")
            if sub in {"secondaryLinks", "additionalPhones"}:
                composite[sub] = json.loads(value) if isinstance(value, str) and value else None
            elif sub == "additionalEmails":
                composite[sub] = list(value) if isinstance(value, list) and value else None
            elif (ftype == "CURRENCY" and sub == "amountMicros") or sub in {"addressLat", "addressLng"}:
                composite[sub] = value if value not in ("",) else None
            else:
                composite[sub] = value if value is not None else ""
        return field_name, composite
    value = data.get(field_name)
    if ftype in {"RAW_JSON", "FILES"}:
        return field_name, json.loads(value) if isinstance(value, str) and value else None
    if ftype in {"MULTI_SELECT", "ARRAY"}:
        return field_name, list(value) if isinstance(value, list) else None
    if ftype == "TEXT":
        return field_name, value if value is not None else ""
    return field_name, value


def api_record(datamodel: DataModel, record: Record) -> dict[str, Any]:
    item: dict[str, Any] = {"id": record.id}
    for field_name, definition in datamodel.stored_fields(record.object):
        if definition.get("active", True) is False:
            continue
        key, value = api_value(record, field_name, definition)
        if key:
            item[key] = value
    item["createdAt"] = record.data.get("crm_created_at")
    item["updatedAt"] = record.data.get("crm_updated_at")
    item["deletedAt"] = record.data.get("crm_deleted_at") or None
    if record.data.get("crm_position") is not None:
        item["position"] = record.data["crm_position"]
    return item


def export_json(target: Path, args: argparse.Namespace) -> dict[str, Any]:
    datamodel = load_datamodel(target)
    datamodel.object(args.object)
    store = RecordStore(target, datamodel)
    chosen = select_records(datamodel, ordered_records(store, args.object), include_deleted=args.include_deleted)
    items = [api_record(datamodel, record) for record in chosen]
    output = Path(args.output).expanduser().resolve()
    if not outside(target, output):
        raise ExportError("die Exportdatei muss außerhalb des Wikis liegen")
    output.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(items, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.write_bytes(content)
    omitted = [
        {"field": name, "reason": "Verknüpfungen mit mehreren Objekttypen liegen in vielen CRMs in Junction-Objekten; dafür gibt es junctions"}
        for name, definition in datamodel.stored_fields(args.object) if definition["type"] == "MORPH_RELATION"
    ]
    return {"state": "exported", "object": args.object, "format": "api-json", "rows": len(items),
            "files": [{"file": output.name, "rows": len(items), "sha256": hashlib.sha256(content).hexdigest()}], "omitted": omitted}


def example_value(datamodel: DataModel, object_name: str, column: Column, settings: dict[str, Any]) -> str:
    definition = datamodel.fields(object_name)[column.field]
    ftype = column.ftype
    if ftype == "RELATION":
        return "00000000-0000-4000-8000-000000000000"
    if ftype in COMPOSITE_SUBFIELDS:
        examples = {
            "firstName": "Max", "lastName": "Mustermann", "primaryEmail": "max.mustermann@example.com",
            "additionalEmails": '["max@example.org"]', "primaryLinkUrl": "https://example.com", "primaryLinkLabel": "example.com",
            "secondaryLinks": '[{"url":"https://example.org","label":"Example"}]', "primaryPhoneNumber": "301234567",
            "primaryPhoneCountryCode": "DE", "primaryPhoneCallingCode": "+49", "additionalPhones": "",
            "addressStreet1": "Musterstraße 1", "addressStreet2": "", "addressCity": "Berlin", "addressPostcode": "10115",
            "addressState": "Berlin", "addressCountry": "Germany", "addressLat": "52.52", "addressLng": "13.405",
            "amountMicros": "1234.56",
        }
        if column.sub == "currencyCode":
            return str((definition.get("default") or {}).get("currencyCode") or settings.get("default_currency") or "EUR")
        return examples.get(column.sub, "")
    options = option_values(definition)
    examples = {
        "TEXT": "Beispiel", "UUID": "00000000-0000-4000-8000-000000000001", "NUMBER": "42", "NUMERIC": "1234.56",
        "BOOLEAN": "TRUE", "DATE": "2026-01-31", "DATE_TIME": "2026-01-31T09:00:00Z", "RATING": "RATING_3",
        "SELECT": options[0] if options else "", "MULTI_SELECT": json.dumps(options[:1]) if options else "",
        "ARRAY": '["eins","zwei"]', "RAW_JSON": '{"key":"value"}', "FILES": '[{"name":"angebot.pdf","ref":"ablage/angebot.pdf"}]',
        "RICH_TEXT": "Text in **Markdown**",
    }
    return examples.get(ftype, "")


def export_sample(target: Path, args: argparse.Namespace) -> dict[str, Any]:
    datamodel = load_datamodel(target)
    datamodel.object(args.object)
    settings = load_json_file(target, SETTINGS_PATH, {}) or {}
    keys = [name for name, definition in datamodel.stored_fields(args.object) if definition.get("active", True) is not False]
    columns, omitted = columns_for(datamodel, args.object, keys, "standard", wiki_language(target), with_id=False)
    row = [example_value(datamodel, args.object, column, settings) for column in columns]
    output = Path(args.output).expanduser().resolve()
    if not outside(target, output):
        raise ExportError("die Vorlage muss außerhalb des Wikis liegen")
    files = write_chunks(output, [column.header for column in columns], [row], 1, delimiter=",", bom=False)
    return {
        "state": "exported", "object": args.object, "format": "sample", "rows": 1, "files": files,
        "columns": [column.header for column in columns], "omitted": omitted,
        "notes": ["Relationsspalten erwarten die ID eines vorhandenen Datensatzes; per Mapping auch Domain, E-Mail oder Name."],
    }


def export_junctions(target: Path, args: argparse.Namespace) -> dict[str, Any]:
    datamodel = load_datamodel(target)
    datamodel.object(args.object)
    morph = [
        (name, definition) for name, definition in datamodel.fields(args.object).items()
        if definition.get("type") == "MORPH_RELATION" and definition["relation"].get("multiple", True)
    ]
    if not morph:
        raise ExportError(f"{args.object} hat kein Mehrfach-Verknüpfungsfeld (targets)")
    field_name, definition = morph[0]
    allowed = list(definition["relation"]["targets"])
    owner_column = f"{args.object}Id"
    target_columns = {name: f"target{name[:1].upper()}{name[1:]}Id" for name in allowed}
    headers = ["id", owner_column] + [target_columns[name] for name in allowed]
    store = RecordStore(target, datamodel)
    rows = []
    for record in select_records(datamodel, ordered_records(store, args.object), include_deleted=args.include_deleted):
        value = record.data.get(field_name)
        for link in value if isinstance(value, list) else ([value] if value else []):
            parsed = parse_record_link(link) if isinstance(link, str) else None
            if not parsed:
                continue
            object_name = datamodel.object_for_directory(parsed[0])
            if object_name not in target_columns:
                continue
            junction_id = uuid.uuid5(JUNCTION_NAMESPACE, f"{args.object}:{record.id}:{object_name}:{parsed[1]}")
            row = {"id": str(junction_id), owner_column: record.id, target_columns[object_name]: parsed[1]}
            rows.append([row.get(header, "") for header in headers])
    output = Path(args.output).expanduser().resolve()
    if not outside(target, output):
        raise ExportError("die Exportdatei muss außerhalb des Wikis liegen")
    files = write_chunks(output, headers, rows, args.max_rows or EXPORT_FILE_LIMIT, delimiter=",", bom=False)
    plural = "noteTargets" if args.object == "note" else ("taskTargets" if args.object == "task" else f"{args.object}Targets")
    return {
        "state": "exported", "object": args.object, "format": "junctions", "junction_object": plural,
        "rows": len(rows), "files": files, "columns": headers,
        "notes": [f"Je Verknüpfung eine Zeile; die IDs sind aus Notiz bzw. Aufgabe und Ziel abgeleitet und bleiben bei jedem Export gleich. Reimport ins Wiki: crm_import.py plan --object {plural[:-1]}."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--target", required=True)
        command.add_argument("--lock-token", required=True)
        command.add_argument("--object", required=True)
        command.add_argument("--output", required=True, help="Output file outside the wiki")

    csv_parser = sub.add_parser("csv", help="Export one object as CSV")
    common(csv_parser)
    csv_parser.add_argument("--view", help="Saved view id or label: filter, sort and visible fields")
    csv_parser.add_argument("--format", choices=["standard", "plain"], default="standard")
    csv_parser.add_argument("--include-deleted", action="store_true")
    csv_parser.add_argument("--max-rows", type=int, help=f"Rows per file (default {EXPORT_FILE_LIMIT})")
    csv_parser.add_argument("--me", help="workspaceMember id that @me in a view filter stands for")
    json_parser = sub.add_parser("json", help="Export one object in API JSON form")
    common(json_parser)
    json_parser.add_argument("--include-deleted", action="store_true")
    sample_parser = sub.add_parser("sample", help="CSV template with one example row")
    common(sample_parser)
    junction_parser = sub.add_parser("junctions", help="noteTargets or taskTargets rows")
    common(junction_parser)
    junction_parser.add_argument("--include-deleted", action="store_true")
    junction_parser.add_argument("--max-rows", type=int)
    args = parser.parse_args()
    target = Path(args.target).expanduser().resolve()
    require_lock(target, args.lock_token)
    try:
        if getattr(args, "max_rows", None) is not None and args.max_rows < 1:
            raise ExportError("--max-rows muss mindestens 1 sein")
        handler = {"csv": export_csv, "json": export_json, "sample": export_sample, "junctions": export_junctions}[args.command]
        print(json.dumps(handler(target, args), ensure_ascii=False, indent=2))
        return 0
    except (ExportError, CrmError, FilterError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"state": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
