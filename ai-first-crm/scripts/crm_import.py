#!/usr/bin/env python3
"""Import CSV, XLSX and API JSON files into CRM records through one hash-bound plan.

inspect  reads a file and reports encoding, delimiter, headers, row count, sample
         rows (credentials masked), a proposed mapping of columns to field keys
         with type hints, and the columns nothing matches.
plan     maps, normalizes and validates every row, resolves the match key and
         every relation to existing records, and writes one transaction plan
         outside the wiki (crm_contract.plan_transaction). Nothing changes yet.
apply    applies exactly that plan, like crm_records.py apply.

Import semantics: exactly one match key per run (id or one unique field, for
example emails.primaryEmail or domainName); a row whose id and unique values
point to different records is a row error; soft-deleted records count and are
restored when matched; an empty cell clears a value (the report lists every
existing value that would go; --keep-empty-cells keeps them), a missing column
leaves it unchanged; a column mapped to a whole currency, e-mail, link or phone
field means its amount or primary value, so an empty cell keeps the currency
code and the additional values; multi-select values replace; select values by API name or
label; defaults apply on creation; relation columns must hit existing records,
so companies come first, then people, opportunities, notes and tasks. A row that
repeats an earlier row exactly is reported as duplicate-ignored and imported
once; rows sharing a match value with different values are errors. A
noteTargets or taskTargets file (note or task id plus targetPerson,
targetCompany or targetOpportunity) adds links to the targets field of
existing notes and tasks.

Mapping file (JSON): {"columns": {"<column header>": "<field key>" | null}}.
Field keys: id, crm_created_at, <field>, <field>.<subfield> (amount for decimal
currency amounts), <relation>.<field of the target> (company.domainName,
accountOwner.userEmail, company.id), <morph field>.<object>[.<field>]
(targets.person), and in junction files record[.<field>].
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import hashlib
import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import secret_screen
from crm_contract import (
    StalePlanError,
    COMPOSITE_SUBFIELDS, CURRENCY_CODE_RE, EMAIL_RE, INSTANT_RE, SETTINGS_PATH, UUID_RE, CrmError, DataModel,
    OperationError, RecordStore, amount_to_micros, apply_transaction, coerce_subfield, coerce_value, is_empty,
    DOMAIN_FIELDS, load_credential_acks, load_datamodel, load_json_file, normalize_email, normalize_url, option_values,
    plan_transaction, portable_reference, unique_fields, unique_key, verify_plan,
)
from crm_standard import L as STANDARD_LABELS
from crm_standard import OBJECT_LABELS
from crm_tabular import (
    DOT_DATE, GERMAN_SUBFIELD_LABELS, ISO_DATE, SEP_DATE, SYSTEM_IGNORED, DOC_SUBFIELD_LABELS,
    STANDARD_FIELD_LABELS, STANDARD_SUBFIELD_LABELS, CellError, Table, TabularError, ValueProblem, cell_text,
    choose_decimal, decimal_evidence, decimal_text, describe_value, header_key, humanize, infer_decimal, is_blank, moment_to_date,
    moment_to_instant, parse_bool, parse_decimal, parse_list, parse_money, parse_moment, read_table,
    strip_injection_guard,
)
from wiki_lock import require_lock

MAPPING_FORMAT = "lmwiki-crm-import-mapping/1"
IMPORT_FORMAT = "lmwiki-crm-import/1"
MODES = ("create", "upsert", "update-only")
JUNCTION_OBJECTS = {"noteTarget": "note", "noteTargets": "note", "taskTarget": "task", "taskTargets": "task"}
IMPORT_ORDER = "Zielsätze müssen vor dem Import existieren; Reihenfolge: Firmen, Personen, Verkaufschancen, dann Notizen und Aufgaben"
REDACTED = "[credential removed]"
NUMERIC_TYPES = {"NUMBER", "NUMERIC"}
URLISH = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?:www\.)?[^\s/@]+\.[a-z]{2,}(?:[/?#].*)?$", re.IGNORECASE)


class MappingError(ValueError):
    """The mapping cannot be applied to the data model or the file."""


class _Clear:
    def __repr__(self) -> str:
        return "CLEAR"


CLEAR = _Clear()


def outside(target: Path, path: Path) -> bool:
    return path != target and target not in path.parents


# ---------------------------------------------------------------------------
# mapping targets


@dataclass
class Target:
    text: str
    kind: str
    field: str = ""
    sub: str = ""
    ftype: str = ""
    lookup: str = ""
    morph_object: str = ""
    definition: dict[str, Any] = dataclass_field(default_factory=dict)

    @property
    def slot(self) -> str:
        """Two columns may not fill the same slot."""
        if self.kind in {"relation", "owner"}:
            return f"{self.kind}:{self.field}"
        if self.kind == "morph":
            return f"morph:{self.field}:{self.morph_object}"
        if self.kind == "value" and self.ftype == "CURRENCY" and self.sub in {"amount", "amountMicros"}:
            return f"value:{self.field}.amount"
        return f"{self.kind}:{self.text}"


def _check_lookup(datamodel: DataModel, object_name: str, lookup: str) -> None:
    if not lookup or lookup == "id":
        return
    fields = datamodel.fields(object_name)
    base, _, _sub = lookup.partition(".")
    if base not in fields:
        raise MappingError(f"{object_name} hat kein Feld {base!r} für die Zuordnung")
    if fields[base]["type"] in {"RELATION", "MORPH_RELATION", "RICH_TEXT"}:
        raise MappingError(f"{object_name}.{base} eignet sich nicht zur Zuordnung")


def parse_target(datamodel: DataModel, object_name: str, text: str, owner: Optional[str] = None) -> Target:
    text = str(text).strip()
    if text in {"id", "crm_id"}:
        return Target(text, "id")
    if text in {"crm_created_at", "createdAt"}:
        return Target(text, "created_at")
    if owner and (text == "record" or text.startswith("record.")):
        lookup = text.partition(".")[2]
        _check_lookup(datamodel, object_name, lookup)
        return Target(text, "owner", lookup=lookup)
    base, _, rest = text.partition(".")
    fields = datamodel.fields(object_name)
    if base not in fields:
        raise MappingError(f"{object_name} hat kein Feld {base!r}")
    definition = fields[base]
    if definition.get("active", True) is False:
        raise MappingError(f"das Feld {object_name}.{base} ist deaktiviert")
    ftype = definition["type"]
    if ftype == "RELATION":
        relation = definition["relation"]
        if relation.get("type") == "ONE_TO_MANY":
            raise MappingError(
                f"{object_name}.{base} ist die 1:n-Gegenseite; die Verknüpfung wird an der n-Seite importiert "
                f"({relation['target']}.{relation.get('inverse')})"
            )
        _check_lookup(datamodel, relation["target"], rest)
        return Target(text, "relation", base, ftype=ftype, lookup=rest, definition=definition)
    if ftype == "MORPH_RELATION":
        allowed = definition["relation"]["targets"]
        morph_object, _, lookup = rest.partition(".")
        if morph_object and morph_object not in allowed:
            raise MappingError(f"{object_name}.{base} verknüpft nur {', '.join(allowed)}, nicht {morph_object}")
        if lookup:
            _check_lookup(datamodel, morph_object, lookup)
        return Target(text, "morph", base, ftype=ftype, morph_object=morph_object, lookup=lookup, definition=definition)
    if ftype in COMPOSITE_SUBFIELDS:
        if rest and rest not in COMPOSITE_SUBFIELDS[ftype] and not (ftype == "CURRENCY" and rest == "amount"):
            raise MappingError(f"{object_name}.{base} hat kein Unterfeld {rest!r} (erlaubt: {', '.join(COMPOSITE_SUBFIELDS[ftype])})")
        return Target(text, "value", base, sub=rest, ftype=ftype, definition=definition)
    if ftype == "RICH_TEXT":
        if rest == "blocknote":
            raise MappingError("BlockNote-JSON wird nicht übernommen; bitte die Markdown-Spalte zuordnen")
        if rest not in {"", "markdown"}:
            raise MappingError(f"{object_name}.{base} hat kein Unterfeld {rest!r}")
        return Target(text, "value", base, ftype=ftype, definition=definition)
    if rest:
        raise MappingError(f"{object_name}.{base} hat keine Unterfelder")
    return Target(text, "value", base, ftype=ftype, definition=definition)


# ---------------------------------------------------------------------------
# automatic mapping


SYNONYMS: dict[str, dict[str, list[str]]] = {
    "person": {
        "name.firstName": ["Vorname", "First name", "Given name", "Firstname"],
        "name.lastName": ["Nachname", "Familienname", "Last name", "Surname", "Family name", "Lastname"],
        "jobTitle": ["Position", "Funktion", "Rolle", "Job title", "Title"],
        "company": ["Firma", "Unternehmen", "Organisation", "Organization", "Account", "Company"],
        "linkedinLink": ["LinkedIn", "LinkedIn URL", "LinkedIn-Profil"],
    },
    "company": {
        "name": ["Firmenname", "Firma", "Unternehmen", "Company", "Company name", "Organisation", "Organization"],
        "domainName": ["Website", "Webseite", "Domain", "URL", "Homepage", "Web", "Internet"],
        "annualRevenue": ["Umsatz", "Jahresumsatz", "Revenue", "Annual revenue"],
        "accountOwner": ["Betreuer", "Kundenbetreuer", "Account owner", "Owner", "Verantwortlich"],
        "linkedinLink": ["LinkedIn", "LinkedIn URL"],
    },
    "opportunity": {
        "name": ["Verkaufschance", "Deal", "Opportunity", "Bezeichnung"],
        "amount": ["Betrag", "Wert", "Volumen", "Auftragswert", "Value", "Deal value"],
        "closeDate": ["Abschlussdatum", "Abschluss", "Close date", "Closing date"],
        "stage": ["Phase", "Stufe", "Stage"],
        "company": ["Firma", "Unternehmen", "Kunde", "Company", "Account"],
        "pointOfContact": ["Ansprechpartner", "Kontakt", "Contact", "Point of contact"],
        "owner": ["Verantwortlich", "Owner", "Vertrieb"],
    },
    "task": {
        "title": ["Aufgabe", "Titel", "Betreff", "Task", "Title", "Subject"],
        "dueAt": ["Fällig", "Fällig am", "Fälligkeit", "Termin", "Due", "Due date"],
        "assignee": ["Zuständig", "Bearbeiter", "Assignee", "Assigned to", "assignedTo"],
        "bodyV2": ["Beschreibung", "Inhalt", "Text", "Body", "Details"],
    },
    "note": {
        "title": ["Titel", "Betreff", "Title", "Subject"],
        "bodyV2": ["Inhalt", "Text", "Notiz", "Body", "Note"],
    },
    "workspaceMember": {
        "name.firstName": ["Vorname", "First name"],
        "name.lastName": ["Nachname", "Last name"],
        "userEmail": ["E-Mail", "Email", "Mail", "Login-E-Mail"],
    },
}
TYPE_SYNONYMS: dict[str, dict[str, list[str]]] = {
    "ADDRESS": {
        "addressStreet1": ["Straße", "Strasse", "Straße und Hausnummer", "Street", "Address line 1", "Anschrift"],
        "addressStreet2": ["Adresszusatz", "Address line 2"],
        "addressCity": ["Ort", "Stadt", "City", "Town"],
        "addressPostcode": ["PLZ", "Postleitzahl", "Zip", "Zip code", "Postal code", "Postcode"],
        "addressState": ["Bundesland", "Region", "State", "Province"],
        "addressCountry": ["Land", "Country"],
    },
    "EMAILS": {"": ["E-Mail", "Email", "Mail", "E-Mail-Adresse", "Email address", "E-Mail Adresse"]},
    "PHONES": {"": ["Telefon", "Telefonnummer", "Tel", "Phone", "Phone number", "Mobil", "Mobile"]},
}
LOOKUP_SHORT = {"EMAILS": ["Email", "E-Mail", "Mail"], "LINKS": ["Domain", "Website", "URL"], "UUID": ["Id"]}
DOMAIN_HEADERS = ["Domain", "Domain Name", "Domainname", "Website", "Webseite", "Homepage", "Internetseite"]
NAME_PARTS = {
    "firstName": ("vorname", "firstname", "givenname"),
    "lastName": ("nachname", "lastname", "familienname", "familyname", "surname"),
}


def field_labels(object_name: str, field_name: str, definition: dict[str, Any]) -> list[str]:
    labels = [field_name, humanize(field_name), str(definition.get("label") or "")]
    standard = STANDARD_FIELD_LABELS.get(object_name, {}).get(field_name)
    if standard:
        labels.append(standard)
    if definition.get("standard"):
        for language in ("de", "en"):
            label = STANDARD_LABELS[language].get(field_name)
            if label:
                labels.append(label)
    return [label for label in dict.fromkeys(labels) if label]


def object_labels(object_name: str, definition: dict[str, Any]) -> list[str]:
    labels = [object_name, humanize(object_name), str(definition.get("labelSingular") or "")]
    for language in ("de", "en"):
        pair = OBJECT_LABELS.get(language, {}).get(object_name)
        if pair:
            labels.append(pair[0])
    return [label for label in dict.fromkeys(labels) if label]


class AliasTable:
    def __init__(self) -> None:
        self.entries: dict[str, list[tuple[int, Optional[str], str]]] = {}

    def add(self, text: str, target: Optional[str], priority: int, reason: str) -> None:
        key = header_key(text)
        if key:
            self.entries.setdefault(key, []).append((priority, target, reason))

    def lookup(self, header: str) -> list[tuple[int, Optional[str], str]]:
        return self.entries.get(header_key(header), [])


def build_aliases(datamodel: DataModel, object_name: str, owner: Optional[str] = None) -> AliasTable:
    table = AliasTable()
    fields = datamodel.fields(object_name)
    if owner:
        owner_definition = datamodel.object(object_name)
        for label in object_labels(object_name, owner_definition):
            for text in (f"{label}Id", f"{label} Id", f"{label}.id"):
                table.add(text, "record", 1, "Notiz oder Aufgabe der Verknüpfung")
            table.add(label, "record", 2, "Notiz oder Aufgabe der Verknüpfung")
        table.add("id", None, 1, "ID der Verknüpfung (Junction); wird nicht gebraucht")
        for morph_name, definition in fields.items():
            if definition.get("type") != "MORPH_RELATION" or not definition["relation"].get("multiple", True):
                continue
            for target_object in definition["relation"]["targets"]:
                capital = target_object[:1].upper() + target_object[1:]
                for text in (f"target{capital}Id", f"target{capital}", f"target{capital}.id"):
                    table.add(text, f"{morph_name}.{target_object}", 1, "Ziel der Verknüpfung")
                for label in object_labels(target_object, datamodel.object(target_object)) if target_object in datamodel.objects else [capital]:
                    table.add(f"{label} Id", f"{morph_name}.{target_object}", 2, "Ziel der Verknüpfung")
                    table.add(label, f"{morph_name}.{target_object}", 3, "Ziel der Verknüpfung")
            break
        return table
    for text in ("id", "Id", "ID", "crm_id", "uuid", "Record ID", "Datensatz-ID"):
        table.add(text, "id", 1, "Datensatz-ID")
    for text in ("createdAt", "crm_created_at", "Creation date", "Created at", "Erstellt am", "Erstellungsdatum", "Angelegt am"):
        table.add(text, "crm_created_at", 2, "Erstellungszeitpunkt (nur beim Anlegen)")
    for text in SYSTEM_IGNORED | {"Geändert am", "Gelöscht am", "Erstellt von", "Geändert von", "Updated at", "Deleted at", "Created by", "Updated by"}:
        table.add(text, None, 1, "Systemfeld; wird nicht importiert")
    type_counts = Counter(definition.get("type") for definition in fields.values())
    for field_name, definition in fields.items():
        ftype = definition.get("type")
        if definition.get("active", True) is False:
            continue
        labels = field_labels(object_name, field_name, definition)
        if ftype == "RELATION":
            relation = definition["relation"]
            if relation.get("type") == "ONE_TO_MANY":
                for label in labels:
                    table.add(label, None, 3, f"1:n-Gegenseite; Verknüpfung an {relation['target']}.{relation.get('inverse')} importieren")
                continue
            target_object = relation["target"]
            table.add(field_name, field_name, 1, "Relation (ID, Unique-Feld oder Name je Wert)")
            for text in (f"{field_name}Id", f"{field_name}.id"):
                table.add(text, f"{field_name}.id", 1, "Relations-ID")
            for label in labels:
                table.add(f"{label} Id", f"{field_name}.id", 2, "Relations-ID (Standard-Spaltenform)")
                table.add(label, field_name, 3, "Relation (ID, Unique-Feld oder Name je Wert)")
            target_fields = datamodel.fields(target_object)
            lookups = [name for name, item in target_fields.items() if item.get("unique")]
            label_field = datamodel.label_field(target_object)
            if label_field and label_field not in lookups:
                lookups.append(label_field)
            for lookup in lookups:
                lookup_definition = target_fields[lookup]
                shorts = field_labels(target_object, lookup, lookup_definition) + LOOKUP_SHORT.get(lookup_definition.get("type"), [])
                if "mail" in lookup.lower():
                    shorts += LOOKUP_SHORT["EMAILS"]
                table.add(f"{field_name}.{lookup}", f"{field_name}.{lookup}", 1, f"Relation über {target_object}.{lookup}")
                for label in labels:
                    for short in dict.fromkeys(shorts):
                        table.add(f"{label} {short}", f"{field_name}.{lookup}", 2, f"Relation über {target_object}.{lookup}")
            # A bare domain column in a file of people or deals names the company they belong to.
            for lookup in [name for name in lookups if target_fields[name].get("unique") and target_fields[name].get("type") == "LINKS"][:1]:
                for text in DOMAIN_HEADERS + field_labels(target_object, lookup, target_fields[lookup]):
                    table.add(text, f"{field_name}.{lookup}", 4, f"Domain der Relation {field_name}; vor dem Namen bevorzugt")
            continue
        if ftype == "MORPH_RELATION":
            table.add(field_name, field_name, 1, "Verknüpfungen als objekt:id")
            for target_object in definition["relation"]["targets"]:
                table.add(f"{field_name}.{target_object}", f"{field_name}.{target_object}", 1, f"Verknüpfung mit {target_object}")
            continue
        table.add(field_name, field_name, 1, "API-Name")
        for label in labels:
            table.add(label, field_name, 3, "Feldbezeichnung")
        if ftype == "RICH_TEXT":
            for label in labels:
                table.add(f"{label} / Markdown", field_name, 2, "Standard-Spaltenform")
                table.add(f"{label}.markdown", field_name, 1, "API-Pfad")
                table.add(f"{label} / BlockNote", None, 2, "BlockNote-JSON wird nicht übernommen")
            continue
        if ftype not in COMPOSITE_SUBFIELDS:
            continue
        for sub in COMPOSITE_SUBFIELDS[ftype]:
            target = f"{field_name}.amount" if (ftype == "CURRENCY" and sub == "amountMicros") else f"{field_name}.{sub}"
            table.add(f"{field_name}.{sub}", f"{field_name}.{sub}", 1, "API-Pfad")
            sub_labels = [STANDARD_SUBFIELD_LABELS[ftype][sub], humanize(sub), GERMAN_SUBFIELD_LABELS[ftype][sub]]
            sub_labels += DOC_SUBFIELD_LABELS.get(ftype, {}).get(sub, [])
            for label in labels:
                for sub_label in sub_labels:
                    table.add(f"{label} / {sub_label}", target, 2, "Standard-Spaltenform")
                    table.add(f"{label} {sub_label}", target, 3, "Feld und Unterfeld")
            if type_counts[ftype] == 1:
                for sub_label in sub_labels:
                    table.add(sub_label, target, 4, "Unterfeld-Bezeichnung")
                for synonym in TYPE_SYNONYMS.get(ftype, {}).get(sub, []):
                    table.add(synonym, target, 4, "Synonym")
        if type_counts[ftype] == 1:
            for synonym in TYPE_SYNONYMS.get(ftype, {}).get("", []):
                table.add(synonym, field_name, 4, "Synonym")
    for target, synonyms in SYNONYMS.get(object_name, {}).items():
        base = target.partition(".")[0]
        if base in fields and fields[base].get("active", True) is not False:
            for synonym in synonyms:
                table.add(synonym, target, 4, "Synonym")
    return table


PRIMARY_KEYS = {"EMAILS": "primaryEmail", "LINKS": "primaryLinkUrl", "PHONES": "primaryPhoneNumber", "CURRENCY": "amount"}


def _canonical_target(datamodel: DataModel, object_name: str, text: Optional[str]) -> Optional[str]:
    """A whole e-mail, link, phone or currency field means its primary subfield."""
    if not text or "." in text:
        return text
    definition = datamodel.fields(object_name).get(text)
    primary = PRIMARY_KEYS.get(definition.get("type")) if definition else None
    return f"{text}.{primary}" if primary else text


def _name_part(datamodel: DataModel, object_name: str, header: str) -> Optional[str]:
    """'Ansprechpartner Vorname' -> name.firstName when the object has exactly one full-name field."""
    full_names = [
        name for name, definition in datamodel.fields(object_name).items()
        if definition.get("type") == "FULL_NAME" and definition.get("active", True) is not False
    ]
    if len(full_names) != 1:
        return None
    key = header_key(header)
    parts = [part for part, words in NAME_PARTS.items() if any(word in key for word in words)]
    return f"{full_names[0]}.{parts[0]}" if len(parts) == 1 else None


def _lookup_rank(datamodel: DataModel, target: Target) -> int:
    """Within one relation: id first, then a unique field such as the domain, then a per-value guess, then a name."""
    if target.kind != "relation" or target.lookup == "id":
        return 0
    if not target.lookup:
        return 2
    lookup = datamodel.fields(target.definition["relation"]["target"]).get(target.lookup.partition(".")[0], {})
    return 1 if lookup.get("unique") else 3


def suggest_mapping(datamodel: DataModel, object_name: str, headers: list[str], owner: Optional[str] = None) -> tuple[dict[str, Optional[str]], dict[str, dict[str, Any]]]:
    """Proposed {header: target or None} plus details (reason, priority) per header."""
    aliases = build_aliases(datamodel, object_name, owner)
    details: dict[str, dict[str, Any]] = {}
    ranked: list[tuple[int, int, str, Target]] = []
    for position, header in enumerate(headers):
        candidates = list(aliases.lookup(header))
        try:
            explicit = parse_target(datamodel, object_name, header, owner)
            if not (owner and explicit.kind not in {"owner", "morph"}):
                candidates.insert(0, (0, explicit.text, "Feldschlüssel"))
        except (MappingError, OperationError):
            pass
        key = header_key(header)
        if not candidates and (key.startswith("createdby") or key.startswith("updatedby")):
            candidates = [(1, None, "Systemfeld; wird nicht importiert")]
        if not candidates and not owner:
            part = _name_part(datamodel, object_name, header)
            if part:
                candidates = [(5, part, "Namensteil in der Überschrift")]
        if not candidates:
            details[header] = {"target": None, "reason": "kein passendes Feld"}
            continue
        best = min(item[0] for item in candidates)
        targets = {item[1] for item in candidates if item[0] == best}
        reasons = [item[2] for item in candidates if item[0] == best]
        if len({_canonical_target(datamodel, object_name, item) for item in targets}) > 1:
            choices = ", ".join(sorted(str(item) for item in targets if item))
            details[header] = {"target": None, "reason": f"mehrdeutig ({choices}); bitte mit einer Mapping-Datei festlegen"}
            continue
        target_text = min(targets, key=lambda item: len(item or ""))
        if target_text is None:
            details[header] = {"target": None, "reason": reasons[0]}
            continue
        try:
            target = parse_target(datamodel, object_name, target_text, owner)
        except MappingError as exc:
            details[header] = {"target": None, "reason": str(exc)}
            continue
        details[header] = {"target": target_text, "reason": reasons[0]}
        ranked.append((best, position, header, target))
    taken: dict[str, str] = {}
    mapping: dict[str, Optional[str]] = {header: None for header in headers}
    wholes: dict[str, str] = {}
    for _priority, _position, header, target in sorted(ranked, key=lambda item: (_lookup_rank(datamodel, item[3]), item[0], item[1])):
        slot = target.slot
        if slot in taken:
            if target.kind == "relation":
                reason = f"Relation {target.field} schon über Spalte {taken[slot]!r} zugeordnet (ID vor Unique-Feld wie Domain vor Name)"
            else:
                reason = f"Ziel {target.text} schon durch Spalte {taken[slot]!r} belegt"
            details[header] = {"target": None, "reason": reason}
            continue
        taken[slot] = header
        mapping[header] = target.text
        if target.kind == "value" and target.ftype in COMPOSITE_SUBFIELDS and not target.sub:
            wholes[target.field] = header
    for field_name, header in wholes.items():
        if not any(mapping[other] and mapping[other].startswith(field_name + ".") for other in headers if other != header):
            continue
        primary = _canonical_target(datamodel, object_name, field_name)
        if primary != field_name and primary not in mapping.values():
            mapping[header] = primary
            details[header] = {"target": primary, "reason": details[header]["reason"]}
        else:
            mapping[header] = None
            details[header] = {"target": None, "reason": f"Unterfelder von {field_name} sind schon einzeln zugeordnet"}
    return mapping, details


# ---------------------------------------------------------------------------
# value analysis for inspect


def analyze_values(values: list[Any]) -> list[str]:
    counts: Counter = Counter()
    for value in values[:500]:
        if is_blank(value):
            counts["leer"] += 1
        elif isinstance(value, CellError):
            counts["Excel-Fehlerwert"] += 1
        elif isinstance(value, bool):
            counts["Wahrheitswert"] += 1
        elif isinstance(value, (int, Decimal)):
            counts["Zahl"] += 1
        elif isinstance(value, datetime):
            counts["Datum mit Uhrzeit (Excel)"] += 1
        elif isinstance(value, date):
            counts["Datum (Excel)"] += 1
        elif isinstance(value, dict):
            counts["Objekt (API-Form)"] += 1
        elif isinstance(value, list):
            counts["Liste"] += 1
        else:
            counts[_text_kind(cell_text(value).strip())] += 1
    return [f"{label} ({count})" for label, count in counts.most_common(4)]


def _text_kind(text: str) -> str:
    if "\n" in text:
        return "mehrzeiliger Text"
    if UUID_RE.fullmatch(text.lower()):
        return "UUID"
    if EMAIL_RE.fullmatch(text):
        return "E-Mail-Adresse"
    if ISO_DATE.match(text):
        return "Datum JJJJ-MM-TT"
    if INSTANT_RE.fullmatch(text):
        return "ISO-Zeitpunkt"
    if DOT_DATE.match(text):
        return "Datum TT.MM.JJJJ"
    if SEP_DATE.match(text):
        return "Datum mit / oder - (Reihenfolge mit --date-format angeben)"
    if text.casefold() in {"wahr", "falsch", "true", "false", "ja", "nein", "yes", "no", "x"}:
        return "Wahrheitswert"
    if text.startswith("[") and text.endswith("]"):
        return "JSON-Liste"
    if URLISH.match(text):
        return "Domain oder URL"
    evidence = decimal_evidence(text)
    if re.search(r"[€$£¥]|\b[A-Z]{3}\b", text) and re.search(r"\d", text):
        return "Betrag mit Währung"
    if evidence == "comma":
        return "Zahl mit Dezimalkomma"
    if evidence == "dot":
        return "Zahl mit Dezimalpunkt"
    if re.fullmatch(r"[+-]?[0-9][0-9.,' ]*", text):
        return "Zahl"
    return "Text"


# ---------------------------------------------------------------------------
# rows


@dataclass
class RowResult:
    number: int
    values: dict[str, Any] = dataclass_field(default_factory=dict)
    record_id: Optional[str] = None
    created_at: Optional[str] = None
    owner: Optional[str] = None
    morph: dict[str, list[str]] = dataclass_field(default_factory=dict)
    morph_seen: set[str] = dataclass_field(default_factory=set)
    money_codes: dict[str, str] = dataclass_field(default_factory=dict)
    errors: list[dict[str, Any]] = dataclass_field(default_factory=list)
    cells: dict[str, Any] = dataclass_field(default_factory=dict)
    columns: dict[str, str] = dataclass_field(default_factory=dict)
    duplicate_of: Optional[int] = None

    def error(self, column: Optional[str], rule: str, message: str) -> None:
        self.errors.append({"row": self.number, "column": column, "rule": rule, "message": message})


UNIT_LABELS = {"line": "Zeile", "row": "Zeile", "item": "Eintrag"}


class Importer:
    def __init__(self, target: Path, datamodel: DataModel, settings: dict[str, Any], table: Table, object_name: str,
                 mapping: dict[str, Target], *, mode: str, match_key: Optional[str], date_format: Optional[str],
                 decimal: Optional[str], time_zone: Optional[str], redact: bool, owner: Optional[str] = None,
                 keep_empty: bool = False):
        self.target = target
        self.datamodel = datamodel
        self.settings = settings
        self.table = table
        self.object = object_name
        self.mapping = mapping
        self.mode = mode
        self.date_format = date_format
        self.redact = redact
        self.owner = owner
        self.keep_empty = keep_empty
        self.duplicates: list[dict[str, Any]] = []
        self.unit = UNIT_LABELS.get(table.row_unit, "Zeile")
        self.time_zone = time_zone or settings.get("time_zone") or "UTC"
        self.store = RecordStore(target, datamodel)
        self.indexes: dict[tuple[str, str], dict[Any, list[str]]] = {}
        self.normalizations: dict[tuple[str, str], dict[str, Any]] = {}
        self.credentials: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.conventions: dict[str, tuple[str, str]] = {}
        try:
            acknowledgements = load_credential_acks(target)
        except CrmError as exc:
            self.warnings.append(f"Bestätigungen für Zugangsdaten nicht lesbar: {exc}")
            acknowledgements = []
        self.accepted = {(entry.get("kind"), entry.get("value_sha256")) for entry in acknowledgements if isinstance(entry, dict)}
        self.match_key = self._match_key(match_key) if not owner else None
        self._decimal_conventions(decimal)

    # -- setup ------------------------------------------------------------

    def _match_key(self, text: Optional[str]) -> tuple[str, str]:
        """(field or 'id', storage key) for the one match key of this run."""
        if not text:
            raise MappingError("--match-key fehlt")
        text = text.strip()
        if text in {"id", "crm_id"}:
            key = ("id", "id")
        else:
            base, _, sub = text.partition(".")
            definition = self.datamodel.fields(self.object).get(base)
            if definition is None:
                raise MappingError(f"{self.object} hat kein Feld {base!r} für --match-key")
            if not definition.get("unique"):
                unique = [name for name, _definition in unique_fields(self.datamodel, self.object)]
                raise MappingError(f"{self.object}.{base} ist kein Unique-Feld; erlaubt sind id{', ' if unique else ''}{', '.join(unique)}")
            ftype = definition["type"]
            primary = {"EMAILS": "primaryEmail", "LINKS": "primaryLinkUrl", "PHONES": "primaryPhoneNumber"}.get(ftype)
            if sub and sub != primary:
                raise MappingError(f"--match-key {text}: verglichen wird nur {base}.{primary}")
            key = (base, f"{base}.{primary}" if primary else base)
        covered = any(
            (key[0] == "id" and item.kind == "id")
            or (item.kind == "value" and item.field == key[0] and item.sub in {"", key[1].partition(".")[2]})
            for item in self.mapping.values()
        )
        if not covered:
            if self.mode == "update-only":
                raise MappingError(f"keine Spalte ist dem Match-Schlüssel {text} zugeordnet; update-only braucht ihn")
            self.warnings.append(f"Keine Spalte ist dem Match-Schlüssel {text} zugeordnet; jede Zeile legt einen neuen Datensatz an (wie in CRMs üblich).")
        return key

    def _decimal_conventions(self, explicit: Optional[str]) -> None:
        columns = []
        for column, target in self.mapping.items():
            if target.kind != "value":
                continue
            needs = target.ftype in NUMERIC_TYPES or (target.ftype == "CURRENCY" and target.sub in {"", "amount"}) or (target.ftype == "ADDRESS" and target.sub in {"addressLat", "addressLng"})
            if needs:
                columns.append(column)
        evidence = {column: infer_decimal(row.cells.get(column) for row in self.table.rows) for column in columns}
        file_values = [row.cells.get(column) for column in columns for row in self.table.rows]
        file_level = infer_decimal(file_values) if columns else None
        for column in columns:
            if evidence[column] == "mixed":
                self.warnings.append(f"Spalte {column!r} mischt Dezimalkomma und Dezimalpunkt; Werte, die nicht zur gewählten Konvention passen, werden als Zeilenfehler gemeldet.")
            convention, reason = choose_decimal(explicit, evidence[column], file_level, self.table.delimiter, self.settings.get("number_format"))
            self.conventions[column] = (convention, reason)

    # -- bookkeeping -------------------------------------------------------

    def normalized(self, column: str, rule: str, before: Any = None, after: Any = None, example: bool = True) -> None:
        entry = self.normalizations.setdefault((column, rule), {"column": column, "rule": rule, "count": 0})
        entry["count"] += 1
        if example and "example" not in entry and before is not None:
            entry["example"] = f"{describe_value(before)} -> {describe_value(after)}"

    def screen(self, value: Any, row: int, column: str) -> tuple[Any, list[str]]:
        if isinstance(value, str):
            return self._screen_text(value)
        if isinstance(value, list):
            kinds: list[str] = []
            cleaned = []
            for item in value:
                new, found = self.screen(item, row, column)
                cleaned.append(new)
                kinds += found
            return cleaned, sorted(set(kinds))
        if isinstance(value, dict):
            kinds = []
            cleaned_map = {}
            for key, item in value.items():
                new, found = self.screen(item, row, column)
                cleaned_map[key] = new
                kinds += found
            return cleaned_map, sorted(set(kinds))
        return value, []

    def _screen_text(self, text: str) -> tuple[str, list[str]]:
        spans = []
        for kind, pattern in secret_screen.PATTERNS:
            for match in pattern.finditer(text):
                token = match.group(0)
                if secret_screen.is_placeholder(kind, token):
                    continue
                if (kind, hashlib.sha256(token.encode("utf-8")).hexdigest()) in self.accepted:
                    continue
                spans.append((match.start(), match.end(), kind))
        if not spans:
            return text, []
        kinds = sorted({kind for _start, _end, kind in spans})
        if not self.redact:
            return text, kinds
        parts = []
        position = 0
        for start, end, _kind in sorted(spans):
            if end <= position:
                continue
            parts.append(text[position:max(start, position)])
            parts.append(REDACTED)
            position = end
        parts.append(text[position:])
        return "".join(parts), kinds

    # -- lookups -----------------------------------------------------------

    def _index_value(self, object_name: str, lookup: str, value: Any) -> Any:
        if value is None:
            return None
        if lookup == "id":
            return str(value).strip().lower() or None
        base, _, sub = lookup.partition(".")
        definition = self.datamodel.fields(object_name).get(base, {})
        ftype = definition.get("type")
        text = cell_text(value).strip() if not isinstance(value, list) else ""
        if not text:
            return None
        if ftype == "EMAILS" and sub in {"", "primaryEmail"}:
            return normalize_email(text)
        if ftype == "LINKS" and sub in {"", "primaryLinkUrl"}:
            return normalize_url(text, base in DOMAIN_FIELDS)
        if ftype == "PHONES" and sub in {"", "primaryPhoneNumber"}:
            return re.sub(r"\D", "", text) or None
        if ftype == "FULL_NAME" and not sub:
            return " ".join(text.split()).casefold()
        if ftype == "TEXT" and "mail" in base.lower() and "@" in text:
            return normalize_email(text)
        return text

    def _record_value(self, record: Any, lookup: str) -> Any:
        if lookup == "id":
            return record.id
        base, _, sub = lookup.partition(".")
        definition = self.datamodel.fields(record.object).get(base, {})
        ftype = definition.get("type")
        if ftype in COMPOSITE_SUBFIELDS and not sub:
            if ftype == "FULL_NAME":
                return " ".join(str(record.data.get(f"{base}.{part}") or "").strip() for part in ("firstName", "lastName")).strip()
            primary = {"EMAILS": "primaryEmail", "LINKS": "primaryLinkUrl", "PHONES": "primaryPhoneNumber", "ADDRESS": "addressCity", "CURRENCY": "amountMicros"}[ftype]
            return record.data.get(f"{base}.{primary}")
        return record.data.get(lookup)

    def index(self, object_name: str, lookup: str) -> dict[Any, list[str]]:
        key = (object_name, lookup)
        if key not in self.indexes:
            index: dict[Any, list[str]] = {}
            for record in self.store.records(object_name).values():
                value = self._index_value(object_name, lookup, self._record_value(record, lookup))
                if value is not None:
                    index.setdefault(value, []).append(record.id)
            self.indexes[key] = index
        return self.indexes[key]

    def auto_lookup(self, object_name: str, text: str) -> str:
        if UUID_RE.fullmatch(text.lower()):
            return "id"
        fields = self.datamodel.fields(object_name)
        unique = [name for name, definition in fields.items() if definition.get("unique")]
        if "@" in text:
            for candidates in (
                [name for name in unique if fields[name]["type"] == "EMAILS"],
                [name for name in unique if fields[name]["type"] == "TEXT" and "mail" in name.lower()],
                [name for name, definition in fields.items() if definition.get("type") == "EMAILS"],
            ):
                if candidates:
                    return candidates[0]
        if URLISH.match(text):
            for candidates in (
                [name for name in unique if fields[name]["type"] == "LINKS"],
                [name for name, definition in fields.items() if definition.get("type") == "LINKS"],
            ):
                if candidates:
                    return candidates[0]
        label_field = self.datamodel.label_field(object_name)
        if not label_field:
            raise ValueProblem("relation-not-found", f"{object_name} lässt sich nur über die ID zuordnen")
        return label_field

    def resolve(self, object_name: str, lookup: str, value: Any, purpose: str) -> str:
        if isinstance(value, dict):
            identifier = value.get("id")
            if not identifier:
                raise ValueProblem("relation-format", "verschachtelte Relation ohne id")
            value, lookup = identifier, "id"
        text = cell_text(value).strip()
        field_name = lookup or self.auto_lookup(object_name, text)
        normalized = self._index_value(object_name, field_name, text)
        ids = self.index(object_name, field_name).get(normalized, []) if normalized is not None else []
        records = self.store.records(object_name)
        live = [record_id for record_id in ids if not records[record_id].deleted]
        label = self.datamodel.object(object_name).get("labelSingular", object_name)
        if len(live) == 1:
            return live[0]
        if len(live) > 1:
            raise ValueProblem("relation-ambiguous", f"{len(live)} Datensätze {label} passen zu {field_name} {describe_value(text)!r}; bitte über ID oder ein Unique-Feld zuordnen")
        if ids:
            raise ValueProblem("relation-deleted", f"{label} mit {field_name} {describe_value(text)!r} liegt im Papierkorb; erst wiederherstellen ({purpose})")
        raise ValueProblem("relation-not-found", f"kein Datensatz {label} mit {field_name} {describe_value(text)!r} ({purpose}). {IMPORT_ORDER}")

    def morph_reference(self, target: Target, item: Any) -> str:
        if target.morph_object:
            return f"{target.morph_object}:{self.resolve(target.morph_object, target.lookup, item, target.text)}"
        text = cell_text(item).strip()
        match = re.match(r"^\[\[records/([a-z0-9-]+)/([0-9a-f-]{36})(?:\|[^\]]*)?\]\]$", text)
        if match:
            object_name = self.datamodel.object_for_directory(match.group(1)) or ""
            identifier = match.group(2)
        elif ":" in text:
            object_name, identifier = text.split(":", 1)
        else:
            raise ValueProblem("morph-format", f"{describe_value(text)!r}: bitte objekt:id angeben (zum Beispiel person:<id>) oder die Spalte {target.field}.person zuordnen")
        allowed = target.definition["relation"]["targets"]
        if object_name not in allowed:
            raise ValueProblem("morph-format", f"{target.field} verknüpft nur {', '.join(allowed)}, nicht {object_name!r}")
        return f"{object_name}:{self.resolve(object_name, 'id', identifier.strip(), target.text)}"

    # -- conversion --------------------------------------------------------

    def convert_row(self, row: Any) -> RowResult:
        result = RowResult(row.number, cells=row.cells)
        for column, target in self.mapping.items():
            if column not in row.cells:
                continue
            raw = strip_injection_guard(row.cells[column])
            if isinstance(raw, CellError):
                result.error(column, raw.rule, raw.message)
                continue
            raw, kinds = self.screen(raw, row.number, column)
            if kinds:
                self.credentials.append({"row": row.number, "column": column, "kinds": kinds, "action": "removed" if self.redact else "row-error"})
                if not self.redact:
                    result.error(column, "credential", f"mögliche Zugangsdaten ({', '.join(kinds)}); Wert entfernen oder mit --redact-credentials durch {REDACTED} ersetzen")
                    continue
            before = set(result.values) | result.morph_seen
            try:
                self.convert(target, column, raw, result)
            except ValueProblem as problem:
                result.error(column, problem.rule, str(problem))
            except OperationError as exc:
                result.error(column, "format", str(exc))
            for key in (set(result.values) | result.morph_seen) - before:
                result.columns.setdefault(key, column)
        self._finish_money(result)
        return result

    def convert(self, target: Target, column: str, raw: Any, result: RowResult) -> None:
        blank = is_blank(raw)
        if target.kind == "id":
            if not blank:
                text = cell_text(raw).strip().lower()
                if not UUID_RE.fullmatch(text):
                    raise ValueProblem("format", f"{describe_value(raw)!r} ist keine UUID")
                result.record_id = text
            return
        if target.kind == "created_at":
            if not blank:
                moment = parse_moment(raw, self.date_format)
                value, _rule = moment_to_instant(moment, self.time_zone)
                result.created_at = value if "T" in value else value + "T00:00:00Z"
            return
        if target.kind == "owner":
            if blank:
                raise ValueProblem("required", "die Notiz bzw. Aufgabe der Verknüpfung fehlt")
            result.owner = self.resolve(self.object, target.lookup or "id", raw, target.text)
            return
        if target.kind == "relation":
            if blank:
                result.values[target.field] = CLEAR
                return
            result.values[target.field] = self.resolve(target.definition["relation"]["target"], target.lookup, raw, target.text)
            return
        if target.kind == "morph":
            result.morph_seen.add(target.field)
            if blank:
                return
            items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else parse_list(raw))
            references = result.morph.setdefault(target.field, [])
            for item in items:
                reference = self.morph_reference(target, item)
                if reference not in references:
                    references.append(reference)
            return
        self.convert_value(target, column, raw, blank, result)

    def convert_value(self, target: Target, column: str, raw: Any, blank: bool, result: RowResult) -> None:
        ftype, base, definition = target.ftype, target.field, target.definition
        if ftype in COMPOSITE_SUBFIELDS:
            if target.sub:
                self.convert_sub(target, column, target.sub, raw, result)
                return
            if isinstance(raw, dict):
                for sub, value in raw.items():
                    if sub == "__typename":
                        continue
                    if sub not in COMPOSITE_SUBFIELDS[ftype]:
                        raise ValueProblem("format", f"{base} hat kein Unterfeld {sub!r}")
                    self.convert_sub(target, column, sub, value, result)
                return
            if blank:
                # An empty cell clears what a filled cell would set: the amount of a currency and the
                # primary value of e-mails, links and phones. Currency code and additional values stay.
                cleared = {
                    "CURRENCY": ("amount",), "EMAILS": ("primaryEmail",), "LINKS": ("primaryLinkUrl", "primaryLinkLabel"),
                    "PHONES": ("primaryPhoneNumber", "primaryPhoneCountryCode", "primaryPhoneCallingCode"),
                }.get(ftype)
                if cleared:
                    for sub in cleared:
                        result.values[f"{base}.{sub}"] = CLEAR
                else:
                    result.values[base] = CLEAR
                return
            if ftype == "FULL_NAME":
                first, last = split_name(cell_text(raw))
                result.values[f"{base}.firstName"] = first or CLEAR
                result.values[f"{base}.lastName"] = last or CLEAR
                self.normalized(column, "name-split", example=False)
                return
            if ftype == "CURRENCY":
                self.convert_sub(target, column, "amount", raw, result)
                return
            if ftype == "ADDRESS":
                raise ValueProblem("format", "eine Adresse braucht Unterfelder (Straße, PLZ, Ort, Land); bitte die Spalte einem Unterfeld zuordnen")
            primary = {"EMAILS": "primaryEmail", "LINKS": "primaryLinkUrl", "PHONES": "primaryPhoneNumber"}[ftype]
            self.convert_sub(target, column, primary, raw, result)
            return
        if ftype == "RICH_TEXT":
            if isinstance(raw, dict):
                raw = raw.get("markdown")
            result.values[base] = CLEAR if is_blank(raw) else cell_text(raw).replace("\r\n", "\n")
            return
        if blank:
            result.values[base] = CLEAR
            return
        value: Any
        if ftype == "TEXT":
            value = cell_text(raw).replace("\r\n", "\n").strip()
        elif ftype == "UUID":
            value = cell_text(raw).strip().lower()
        elif ftype in NUMERIC_TYPES:
            value = parse_decimal(raw, self.conventions[column][0])
            if isinstance(raw, str) and value != raw.strip():
                self.normalized(column, f"decimal-{self.conventions[column][0]}", raw, value)
        elif ftype == "BOOLEAN":
            value = parse_bool(raw)
            if isinstance(raw, str) and raw.strip().lower() not in {"true", "false"}:
                self.normalized(column, "boolean", raw, "true" if value else "false")
        elif ftype in {"DATE", "DATE_TIME"}:
            moment = parse_moment(raw, self.date_format)
            if ftype == "DATE":
                value = moment_to_date(moment)
            else:
                value, extra = moment_to_instant(moment, self.time_zone)
                if extra:
                    self.normalized(column, f"{extra}:{self.time_zone}", raw, value)
            for rule in moment.rules:
                if rule not in {"iso-date", "iso-instant", "iso-local"}:
                    self.normalized(column, rule, raw, value)
        elif ftype == "SELECT":
            value = self.option(column, definition, raw)
        elif ftype == "MULTI_SELECT":
            value = []
            for item in parse_list(raw):
                option = self.option(column, definition, item)
                if option not in value:
                    value.append(option)
        elif ftype == "RATING":
            text = cell_text(raw).strip()
            value = f"RATING_{len(text)}" if text and set(text) <= {"★", "*"} else text
        elif ftype == "ARRAY":
            value = [cell_text(item) for item in parse_list(raw)]
        elif ftype in {"RAW_JSON", "FILES"}:
            if isinstance(raw, str):
                try:
                    json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueProblem("json", f"ungültiges JSON: {exc.msg}") from exc
                value = raw
            else:
                value = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
        else:
            raise ValueProblem("format", f"Felder vom Typ {ftype} lassen sich nicht importieren")
        coerce_value(ftype, definition, base, value)
        result.values[base] = value

    def convert_sub(self, target: Target, column: str, sub: str, raw: Any, result: RowResult) -> None:
        ftype, base = target.ftype, target.field
        key = f"{base}.{sub}"
        if is_blank(raw):
            result.values[f"{base}.amount" if sub == "amountMicros" and ftype == "CURRENCY" else key] = CLEAR
            return
        value: Any
        if ftype == "CURRENCY" and sub == "amount":
            amount, code = parse_money(raw, self.conventions.get(column, ("dot", ""))[0])
            if isinstance(raw, str) and amount != raw.strip():
                self.normalized(column, "money", raw, f"{amount} {code or ''}".strip())
            amount_to_micros(amount)
            result.values[f"{base}.amount"] = amount
            if code:
                result.money_codes[base] = code
            return
        if ftype == "CURRENCY" and sub == "amountMicros":
            micros = parse_decimal(raw, "dot")
            if not re.fullmatch(r"-?[0-9]+", micros):
                raise ValueProblem("format", "amountMicros muss eine ganze Zahl sein (Betrag mal 1.000.000)")
            result.values[f"{base}.amount"] = decimal_text(Decimal(micros) / Decimal(1_000_000))
            return
        if ftype == "CURRENCY" and sub == "currencyCode":
            value = cell_text(raw).strip().upper()
            if not CURRENCY_CODE_RE.fullmatch(value):
                raise ValueProblem("currency", f"{describe_value(raw)!r} ist kein ISO-Währungscode (zum Beispiel EUR)")
        elif ftype == "ADDRESS" and sub in {"addressLat", "addressLng"}:
            value = parse_decimal(raw, self.conventions.get(column, ("dot", ""))[0])
        elif ftype == "EMAILS" and sub == "additionalEmails":
            value = [cell_text(item).strip() for item in parse_list(raw)]
        elif sub in {"secondaryLinks", "additionalPhones"}:
            if isinstance(raw, list):
                items = raw
            else:
                try:
                    items = json.loads(cell_text(raw).strip())
                except json.JSONDecodeError as exc:
                    raise ValueProblem("json", f"{sub} braucht eine JSON-Liste: {exc.msg}") from exc
            if not isinstance(items, list):
                raise ValueProblem("json", f"{sub} braucht eine JSON-Liste")
            if not items:
                result.values[key] = CLEAR
                return
            value = json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
        else:
            value = cell_text(raw).replace("\r\n", "\n").strip()
        coerce_subfield(ftype, sub, value)
        result.values[key] = value

    def option(self, column: str, definition: dict[str, Any], raw: Any) -> str:
        text = cell_text(raw).strip()
        values = option_values(definition)
        if text in values:
            return text
        for option in definition.get("options", []):
            if text.casefold() == str(option.get("label", "")).casefold():
                self.normalized(column, "select-label", text, option["value"])
                return option["value"]
        upper = re.sub(r"[\s-]+", "_", text).upper()
        if upper in values:
            self.normalized(column, "select-api-name", text, upper)
            return upper
        for language in ("de", "en"):
            for value in values:
                label = STANDARD_LABELS[language].get(value)
                if label and label.casefold() == text.casefold():
                    self.normalized(column, "select-label", text, value)
                    return value
        raise ValueProblem("select-option", f"{describe_value(text)!r} ist keine Option (erlaubt: {', '.join(values)}); neue Optionen legt der Import nicht an")

    def _finish_money(self, result: RowResult) -> None:
        for base, code in result.money_codes.items():
            key = f"{base}.currencyCode"
            given = result.values.get(key)
            if given is None or given is CLEAR:
                result.values[key] = code
            elif given != code:
                result.error(None, "currency-conflict", f"{base}: Betrag in {code}, Währungsspalte sagt {given}")
        if self.keep_empty:
            return
        for key, value in list(result.values.items()):
            if key.endswith(".amount") and value is not CLEAR:
                base = key[: -len(".amount")]
                if result.values.get(f"{base}.currencyCode") is CLEAR:
                    result.error(None, "currency-pair", f"{base}: Betrag ohne Währung; Betrag und Währung müssen beide gefüllt sein")

    # -- matching ----------------------------------------------------------

    def row_unique(self, result: RowResult, field_name: str) -> Optional[str]:
        """The normalized unique value a row gives a field, compared like crm_contract.unique_key."""
        if field_name == "id":
            return result.record_id
        definition = self.datamodel.fields(self.object)[field_name]
        pseudo = {
            key: value for key, value in result.values.items()
            if value is not CLEAR and (key == field_name or key.startswith(field_name + "."))
        }
        return unique_key(field_name, definition, pseudo)

    def existing_unique(self, field_name: str) -> dict[str, list[str]]:
        key = (self.object, f"unique:{field_name}")
        if key not in self.indexes:
            index: dict[str, list[str]] = {}
            definition = self.datamodel.fields(self.object)[field_name]
            for record in self.store.records(self.object).values():
                value = unique_key(field_name, definition, record.data)
                if value is not None:
                    index.setdefault(value, []).append(record.id)
            self.indexes[key] = index
        return self.indexes[key]

    def checked_fields(self) -> list[str]:
        names = ["id"] if any(target.kind == "id" for target in self.mapping.values()) else []
        mapped = {target.field for target in self.mapping.values() if target.kind == "value"}
        names += [name for name, _definition in unique_fields(self.datamodel, self.object) if name in mapped]
        return names

    def _signature(self, result: RowResult) -> str:
        """What a row would write; rows with the same signature are the same record twice."""
        if result.errors:
            cells = {column: cell_text(strip_injection_guard(result.cells.get(column))).strip() for column in self.mapping if column in result.cells}
            return json.dumps({"cells": cells}, ensure_ascii=False, sort_keys=True, default=str)
        values: dict[str, Any] = {}
        for key, value in result.values.items():
            if value is CLEAR:
                values[key] = None
            elif key.endswith(".primaryEmail") and isinstance(value, str):
                values[key] = normalize_email(value)
            elif key.endswith(".primaryLinkUrl") and isinstance(value, str):
                values[key] = normalize_url(value, key.split(".", 1)[0] in DOMAIN_FIELDS)
            else:
                values[key] = value
        return json.dumps({
            "id": result.record_id, "created_at": result.created_at, "values": values,
            "morph": {name: sorted(items) for name, items in result.morph.items()}, "morph_seen": sorted(result.morph_seen),
        }, ensure_ascii=False, sort_keys=True, default=str)

    def _differing_columns(self, group: list[RowResult]) -> list[str]:
        return [
            column for column in self.mapping
            if len({cell_text(strip_injection_guard(item.cells.get(column))).strip() for item in group}) > 1
        ]

    def match(self, results: list[RowResult]) -> list[tuple[RowResult, str, str]]:
        """(result, 'create' | 'update', record id) for every row without errors.

        Rows that repeat an earlier row exactly (same match value, same values) are
        set aside as duplicate-ignored; the first one is imported. Rows that share a
        unique value but differ elsewhere are row errors, because the file contradicts itself.
        """
        records = self.store.records(self.object)
        checked = self.checked_fields()
        match_field = self.match_key[0]
        if match_field not in checked:
            checked.insert(0, match_field)
        by_match: dict[str, list[RowResult]] = {}
        for result in results:
            value = self.row_unique(result, match_field)
            if value is not None:
                by_match.setdefault(value, []).append(result)
        for group in by_match.values():
            if len(group) > 1 and len({self._signature(item) for item in group}) == 1:
                first = group[0]
                for item in group[1:]:
                    item.duplicate_of = first.number
                    self.duplicates.append({
                        "row": item.number, "rule": "duplicate-ignored", "duplicate_of": first.number,
                        "message": f"identisch mit {self.unit} {first.number} (gleicher Match-Wert, gleiche Werte); nicht erneut importiert",
                    })
        active = [result for result in results if result.duplicate_of is None]
        seen: dict[tuple[str, str], list[RowResult]] = {}
        for result in active:
            for name in checked:
                value = self.row_unique(result, name)
                if value is not None:
                    seen.setdefault((name, value), []).append(result)
        for (name, value), group in seen.items():
            if len(group) > 1:
                rows = ", ".join(str(item.number) for item in group)
                differing = self._differing_columns(group)
                detail = f"; abweichend: {', '.join(differing)}" if differing else ""
                for item in group:
                    item.error(self._column_for(name), "duplicate-in-file",
                               f"{self._field_label(name)} {describe_value(value)!r} kommt in der Datei mehrfach mit abweichenden Werten vor ({self.unit} {rows}{detail})")
        planned = []
        for result in active:
            if result.errors:
                continue
            key_value = self.row_unique(result, match_field)
            existing = None
            if key_value is not None:
                if match_field == "id":
                    existing = key_value if key_value in records else None
                else:
                    owners = self.existing_unique(match_field).get(key_value, [])
                    existing = owners[0] if owners else None
            if self.mode == "create":
                if existing:
                    hint = " (liegt im Papierkorb; upsert stellt ihn wieder her)" if records[existing].deleted else ""
                    result.error(self._column_for(match_field), "exists", f"{records[existing].data.get('crm_title')!r} existiert schon ({existing}){hint}; Modus upsert oder update-only verwenden")
                    continue
                action, record_id = "create", result.record_id or str(uuid.uuid4())
            elif self.mode == "upsert":
                action, record_id = ("update", existing) if existing else ("create", result.record_id or str(uuid.uuid4()))
            else:
                if key_value is None:
                    result.error(self._column_for(match_field), "match-missing", f"der Match-Wert ({self._field_label(match_field)}) fehlt; update-only legt nichts an")
                    continue
                if not existing:
                    result.error(self._column_for(match_field), "not-found", f"kein Datensatz mit {self._field_label(match_field)} {describe_value(key_value)!r}; update-only legt nichts an")
                    continue
                action, record_id = "update", existing
            for name in checked:
                if name == match_field and action == "create":
                    continue
                value = self.row_unique(result, name)
                if value is None:
                    continue
                if name == "id":
                    owner = value if value in records else None
                    if action == "update" and value != record_id:
                        result.error(self._column_for("id"), "match-conflict", self._conflict_text(name, value, record_id, owner))
                    elif action == "create" and owner:
                        result.error(self._column_for("id"), "unique-conflict", self._conflict_text(name, value, record_id, owner))
                    continue
                for owner in self.existing_unique(name).get(value, []):
                    if owner != record_id:
                        result.error(self._column_for(name), "match-conflict" if action == "update" else "unique-conflict", self._conflict_text(name, value, record_id, owner))
                        break
            if not result.errors:
                planned.append((result, action, record_id))
        return planned

    def _conflict_text(self, name: str, value: str, record_id: str, owner: Optional[str]) -> str:
        records = self.store.records(self.object)
        own = records.get(record_id)
        own_text = f"{own.data.get('crm_title')!r} ({record_id})" if own else f"den neuen Datensatz {record_id}"
        if owner is None:
            return f"{self._field_label(name)} {describe_value(value)!r} gehört zu keinem vorhandenen Datensatz, die Zeile trifft aber {own_text}; IDs lassen sich nicht ändern"
        other = records[owner]
        deleted = " (im Papierkorb)" if other.deleted else ""
        return (f"widersprüchlicher Treffer: die Zeile trifft {own_text}, aber {self._field_label(name)} {describe_value(value)!r} "
                f"gehört zu {other.data.get('crm_title')!r} ({owner}){deleted}")

    def _column_for(self, field_name: str) -> Optional[str]:
        for column, target in self.mapping.items():
            if (field_name == "id" and target.kind == "id") or (target.kind == "value" and target.field == field_name):
                return column
        return None

    def _field_label(self, field_name: str) -> str:
        if field_name == "id":
            return "ID"
        return str(self.datamodel.fields(self.object)[field_name].get("label") or field_name)

    # -- operations --------------------------------------------------------

    def operation(self, result: RowResult, action: str, record_id: str) -> dict[str, Any]:
        """An empty cell clears the value on update (common CRM semantics), unless --keep-empty-cells keeps it."""
        values: dict[str, Any] = {}
        for key, value in result.values.items():
            if value is CLEAR:
                if action == "create" or self.keep_empty:
                    continue
                value = None
            values[key] = value
        for field_name in result.morph_seen:
            references = result.morph.get(field_name, [])
            if not references and (action == "create" or self.keep_empty):
                continue
            values[field_name] = references
        self._default_currency(record_id if action == "update" else None, values)
        if action == "update":
            return {"op": "update", "object": self.object, "record": record_id, "values": values, "row": result.number, "restore_if_deleted": True}
        operation = {"op": "create", "object": self.object, "id": record_id, "values": values, "row": result.number}
        if result.created_at:
            operation["created_at"] = result.created_at
        return operation

    def _has_value(self, record: Any, key: str) -> bool:
        base, _, sub = key.partition(".")
        definition = self.datamodel.fields(self.object).get(base, {})
        ftype = definition.get("type")
        if ftype == "RICH_TEXT":
            return bool(record.richtext.get(base, "").strip())
        if ftype in COMPOSITE_SUBFIELDS:
            if not sub:
                return any(not is_empty(record.data.get(f"{base}.{item}")) for item in COMPOSITE_SUBFIELDS[ftype])
            if ftype == "CURRENCY" and sub == "amount":
                sub = "amountMicros"
            return not is_empty(record.data.get(f"{base}.{sub}"))
        return not is_empty(record.data.get(base))

    def empty_cells(self, planned: list[tuple[RowResult, str, str]]) -> dict[str, Any]:
        """Which existing values the empty cells of update rows clear (or keep with --keep-empty-cells)."""
        records = self.store.records(self.object)
        columns: dict[str, dict[str, Any]] = {}
        for result, action, record_id in planned:
            record = records.get(record_id) if action == "update" else None
            if record is None:
                continue
            keys = [key for key, value in result.values.items() if value is CLEAR]
            keys += [name for name in sorted(result.morph_seen) if not result.morph.get(name)]
            seen: set[str] = set()
            for key in keys:
                column = result.columns.get(key, key)
                if column in seen or not self._has_value(record, key):
                    continue
                seen.add(column)
                entry = columns.setdefault(column, {"column": column, "count": 0, "examples": []})
                entry["count"] += 1
                if len(entry["examples"]) < 5:
                    entry["examples"].append({self.table.row_unit: result.number, "record": record.data.get("crm_title"), "field": key})
        listed = sorted(columns.values(), key=lambda item: (-item["count"], item["column"]))
        return {
            "mode": "keep" if self.keep_empty else "clear",
            "values": sum(item["count"] for item in listed),
            "columns": listed,
        }

    def _default_currency(self, record_id: Optional[str], values: dict[str, Any]) -> None:
        """An amount without currency gets the field default or the workspace currency, as on creation."""
        record = self.store.records(self.object).get(record_id) if record_id else None
        for key in [key for key, value in values.items() if key.endswith(".amount") and value not in (None, "")]:
            base = key[: -len(".amount")]
            field_default = (self.datamodel.fields(self.object)[base].get("default") or {}).get("currencyCode")
            if f"{base}.currencyCode" in values or (record and record.data.get(f"{base}.currencyCode")):
                continue
            if record is None and field_default:
                continue
            default = field_default or self.settings.get("default_currency")
            if default:
                values[f"{base}.currencyCode"] = default

    def junction_operations(self, results: list[RowResult]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        grouped: dict[str, dict[str, Any]] = {}
        for result in results:
            if result.errors:
                continue
            if not result.owner:
                result.error(None, "required", "die Notiz bzw. Aufgabe der Verknüpfung fehlt")
                continue
            references = [reference for field_name in result.morph for reference in result.morph[field_name]]
            if not references:
                result.error(None, "required", "die Zeile nennt kein Ziel (Person, Firma oder Verkaufschance)")
                continue
            entry = grouped.setdefault(result.owner, {"row": result.number, "fields": {}})
            for field_name, items in result.morph.items():
                bucket = entry["fields"].setdefault(field_name, [])
                bucket.extend(item for item in items if item not in bucket)
        operations = []
        for owner, entry in grouped.items():
            values = {field_name: {"add": items} for field_name, items in entry["fields"].items()}
            operations.append({"op": "update", "object": self.object, "record": owner, "values": values, "row": entry["row"]})
        return operations, {owner: entry["row"] for owner, entry in grouped.items()}


def split_name(text: str) -> tuple[str, str]:
    """'Max Müller' -> (Max, Müller); 'Müller, Max' -> (Max, Müller); a single word is a first name."""
    clean = " ".join(text.split())
    if "," in clean:
        last, _, first = clean.partition(",")
        return first.strip(), last.strip()
    if " " in clean:
        first, _, last = clean.rpartition(" ")
        return first, last
    return clean, ""


# ---------------------------------------------------------------------------
# commands


def load_mapping(path: Path, headers: list[str]) -> dict[str, Optional[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MappingError(f"Mapping-Datei nicht lesbar: {exc}") from exc
    columns = raw.get("columns", raw) if isinstance(raw, dict) else None
    if not isinstance(columns, dict):
        raise MappingError('die Mapping-Datei braucht {"columns": {"<Spalte>": "<Feldschlüssel>" | null}}')
    by_key = {header_key(header): header for header in headers}
    mapping: dict[str, Optional[str]] = {header: None for header in headers}
    for column, target in columns.items():
        if column == "format" or column == "object":
            continue
        header = column if column in mapping else by_key.get(header_key(column))
        if header is None:
            raise MappingError(f"die Spalte {column!r} aus der Mapping-Datei fehlt in der Datei")
        if target is not None and not isinstance(target, str):
            raise MappingError(f"Ziel der Spalte {column!r} muss ein Feldschlüssel oder null sein")
        mapping[header] = target.strip() if isinstance(target, str) and target.strip() else None
    return mapping


def resolve_object(datamodel: DataModel, name: str) -> tuple[str, Optional[str]]:
    """(object to change, junction owner or None)."""
    if name not in datamodel.objects and name not in JUNCTION_OBJECTS:
        name = object_for_root(datamodel, name) or name
    if name in JUNCTION_OBJECTS:
        owner = JUNCTION_OBJECTS[name]
        datamodel.object(owner)
        return owner, owner
    try:
        definition = datamodel.object(name)
    except OperationError as exc:
        raise MappingError(f"unbekanntes Objekt {name!r}; vorhanden: {', '.join(datamodel.objects)}") from exc
    if definition.get("active", True) is False:
        raise MappingError(f"das Objekt {name} ist deaktiviert")
    return name, None


def object_for_root(datamodel: DataModel, root: Optional[str]) -> Optional[str]:
    if not root:
        return None
    lowered = root.casefold()
    for name in list(datamodel.objects) + list(JUNCTION_OBJECTS):
        forms = {name, name + "s", name[:-1] + "ies" if name.endswith("y") else name + "s"}
        if name == "person":
            forms.add("people")
        if lowered in {form.casefold() for form in forms}:
            return name
    return None


def compile_mapping(datamodel: DataModel, object_name: str, mapping: dict[str, Optional[str]], owner: Optional[str]) -> dict[str, Target]:
    compiled: dict[str, Target] = {}
    slots: dict[str, str] = {}
    for column, text in mapping.items():
        if not text:
            continue
        try:
            target = parse_target(datamodel, object_name, text, owner)
        except OperationError as exc:
            raise MappingError(f"Spalte {column!r}: {exc}") from exc
        except MappingError as exc:
            raise MappingError(f"Spalte {column!r}: {exc}") from exc
        if owner and target.kind not in {"owner", "morph"}:
            raise MappingError(f"Spalte {column!r}: in einer Verknüpfungsdatei sind nur record und {', '.join('targets.' + item for item in ('person', 'company', 'opportunity'))} sinnvoll")
        if target.slot in slots:
            raise MappingError(f"die Spalten {slots[target.slot]!r} und {column!r} haben dasselbe Ziel {target.text}")
        slots[target.slot] = column
        compiled[column] = target
    wholes = {target.field for target in compiled.values() if target.kind == "value" and target.ftype in COMPOSITE_SUBFIELDS and not target.sub}
    for target in compiled.values():
        if target.kind == "value" and target.sub and target.field in wholes:
            raise MappingError(f"{target.field} ist zugleich als ganzes Feld und als Unterfeld {target.sub} zugeordnet")
    if owner:
        if not any(target.kind == "owner" for target in compiled.values()):
            raise MappingError("keine Spalte ist der Notiz bzw. Aufgabe zugeordnet (Ziel record)")
        if not any(target.kind == "morph" for target in compiled.values()):
            raise MappingError("keine Spalte ist einem Ziel zugeordnet (targets.person, targets.company, targets.opportunity)")
    if not compiled:
        raise MappingError("keine Spalte ist einem Feld zugeordnet")
    return compiled


def read_input(args: argparse.Namespace) -> Table:
    return read_table(Path(args.file).expanduser(), encoding=args.encoding, sheet=args.sheet, delimiter=args.delimiter)


def sample_rows(importer_screen, table: Table, limit: int = 5) -> list[dict[str, Any]]:
    samples = []
    for row in table.rows[:limit]:
        cells = {}
        for header in table.headers:
            value = row.cells.get(header)
            masked, kinds = importer_screen(value)
            cells[header] = describe_value(masked, 80) if not kinds else REDACTED
        samples.append({table.row_unit: row.number, "cells": cells})
    return samples


def credential_mask(value: Any) -> tuple[Any, list[str]]:
    if not isinstance(value, str):
        return value, []
    kinds = sorted({kind for kind, pattern in secret_screen.PATTERNS for match in pattern.finditer(value) if not secret_screen.is_placeholder(kind, match.group(0))})
    return value, kinds


def inspect(target: Path, args: argparse.Namespace) -> dict[str, Any]:
    datamodel = load_datamodel(target)
    table = read_input(args)
    report: dict[str, Any] = {
        "state": "inspected",
        "file": table.summary(),
        "headers": table.headers,
        "notes": table.notes,
        "reading_problems": table.problems[:50],
        "sample_rows": sample_rows(credential_mask, table),
    }
    findings = []
    for row in table.rows:
        for header in table.headers:
            _value, kinds = credential_mask(row.cells.get(header))
            if kinds:
                findings.append({table.row_unit: row.number, "column": header, "kinds": kinds})
    if findings:
        report["credentials"] = findings[:50]
        report["credential_count"] = len(findings)
    object_name = args.object or object_for_root(datamodel, table.json_root)
    if not object_name:
        scores = []
        candidates = [name for name, definition in datamodel.objects.items() if definition.get("active", True) is not False]
        candidates += [name for name in ("noteTarget", "taskTarget") if JUNCTION_OBJECTS[name] in datamodel.objects]
        for candidate in candidates:
            changed, owner = resolve_object(datamodel, candidate)
            mapping, _details = suggest_mapping(datamodel, changed, table.headers, owner)
            mapped = sum(1 for value in mapping.values() if value)
            if mapped:
                scores.append({"object": candidate, "mapped_columns": mapped})
        scores.sort(key=lambda item: -item["mapped_columns"])
        report["object_suggestions"] = scores[:5]
        report["next_step"] = "inspect erneut mit --object aufrufen, um das Mapping zu sehen"
        return report
    changed, owner = resolve_object(datamodel, object_name)
    mapping, details = suggest_mapping(datamodel, changed, table.headers, owner)
    columns = []
    for header in table.headers:
        entry: dict[str, Any] = {"column": header, "target": mapping[header], "reason": details.get(header, {}).get("reason")}
        if mapping[header]:
            parsed = parse_target(datamodel, changed, mapping[header], owner)
            entry["kind"] = parsed.kind
            if parsed.ftype:
                entry["field_type"] = parsed.ftype
        entry["values"] = analyze_values([row.cells.get(header) for row in table.rows])
        columns.append(entry)
    report["object"] = object_name
    report["junction"] = bool(owner)
    report["mapping"] = {header: mapping[header] for header in table.headers}
    report["columns"] = columns
    report["unmapped_columns"] = [{"column": header, "reason": details.get(header, {}).get("reason")} for header in table.headers if not mapping[header]]
    if not owner:
        keys = []
        for header, text in mapping.items():
            if not text:
                continue
            parsed = parse_target(datamodel, changed, text, owner)
            if parsed.kind == "id":
                keys.append("id")
            elif parsed.kind == "value" and datamodel.fields(changed)[parsed.field].get("unique"):
                primary = {"EMAILS": "primaryEmail", "LINKS": "primaryLinkUrl", "PHONES": "primaryPhoneNumber"}.get(parsed.ftype)
                keys.append(f"{parsed.field}.{primary}" if primary else parsed.field)
        report["match_key_candidates"] = list(dict.fromkeys(keys))
    if args.write_mapping:
        out = Path(args.write_mapping).expanduser().resolve()
        if not outside(target, out):
            raise MappingError("die Mapping-Datei muss außerhalb des Wikis liegen")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"format": MAPPING_FORMAT, "object": object_name, "columns": report["mapping"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["mapping_file"] = out.name
    report["next_step"] = "Mapping prüfen, dann plan mit --auto-mapping oder --mapping-file aufrufen"
    return report


def map_core_errors(errors: list[dict[str, Any]], row_by_record: dict[str, int], mapping: dict[str, Target]) -> list[dict[str, Any]]:
    mapped = []
    for error in errors:
        row = error.get("row")
        if row is None and error.get("record"):
            row = row_by_record.get(error["record"])
        message = str(error.get("error", ""))
        column = None
        for header, target in mapping.items():
            if target.field and re.search(rf"(?<![A-Za-z]){re.escape(target.field)}(?![A-Za-z])", message):
                column = header
                break
        mapped.append({"row": row, "column": column, "rule": "core", "message": message})
    return mapped


def count_results(plan: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, int]:
    by_record: dict[str, list[dict[str, Any]]] = {}
    for shard in plan.get("event_shards", []):
        for event in shard.get("append", []):
            by_record.setdefault(event.get("record_id"), []).append(event)
    counts = {"create": 0, "update": 0, "restore": 0, "unchanged": 0, "links_added": 0}
    for operation in operations:
        record_id = operation.get("id") or operation.get("record")
        events = by_record.get(record_id, [])
        if any(event["op"] == "create" for event in events):
            counts["create"] += 1
        elif any(event["op"] in {"update", "upsert"} for event in events):
            counts["update"] += 1
            if any("crm_deleted_at" in event.get("changes", {}) for event in events):
                counts["restore"] += 1
        else:
            counts["unchanged"] += 1
        for event in events:
            for change in event.get("changes", {}).values():
                if isinstance(change, list) and len(change) == 2 and isinstance(change[1], list):
                    before = change[0] if isinstance(change[0], list) else []
                    counts["links_added"] += max(0, len(change[1]) - len(before))
    return counts


def row_changes(plan: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
    """What each planned row does to which record: action, title and every changed field with old and new value."""
    def short(value: Any) -> Any:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        return text if value is None or len(text) <= 80 else text[:77] + "..."

    changes: list[dict[str, Any]] = []
    for shard in plan.get("event_shards", []):
        for event in shard.get("append", []):
            fields = {}
            for key, value in (event.get("changes") or {}).items():
                if key.startswith(("crm_", "richtext:")) or not isinstance(value, list) or len(value) != 2:
                    if key.startswith("richtext:"):
                        fields[key.split(":", 1)[1]] = "text changed"
                    continue
                fields[key] = [short(value[0]), short(value[1])]
            title = (event.get("changes") or {}).get("crm_title")
            changes.append({
                "row": (event.get("origin") or {}).get("row"), "action": event.get("op"), "object": event.get("object"),
                "record": title[1] if isinstance(title, list) and len(title) == 2 and title[1] else event.get("record_id"),
                "fields": fields,
            })
            if len(changes) >= limit:
                return changes
    return changes


def plan_import(target: Path, args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    datamodel = load_datamodel(target)
    settings = load_json_file(target, SETTINGS_PATH, {}) or {}
    output = Path(args.output).expanduser().resolve()
    if not outside(target, output):
        raise MappingError("der Plan muss außerhalb des Wikis liegen")
    reference = Path(str(args.origin_ref).replace("\\", "/")).name.strip()
    if not reference or not portable_reference(reference):
        raise MappingError("--origin-ref braucht einen portablen Dateinamen, zum Beispiel kontakte.csv")
    object_name, owner = resolve_object(datamodel, args.object)
    if owner and args.mode == "create":
        raise MappingError("eine Verknüpfungsdatei ergänzt vorhandene Notizen oder Aufgaben; bitte --mode upsert oder update-only")
    if owner and args.match_key not in {"id", "record"}:
        raise MappingError("in einer Verknüpfungsdatei ist der Match-Schlüssel die ID der Notiz bzw. Aufgabe (--match-key id)")
    table = read_input(args)
    if args.mapping_file:
        raw_mapping = load_mapping(Path(args.mapping_file).expanduser(), table.headers)
        ignored = [{"column": header, "reason": "nicht im Mapping"} for header, value in raw_mapping.items() if not value]
    else:
        raw_mapping, details = suggest_mapping(datamodel, object_name, table.headers, owner)
        ignored = [{"column": header, "reason": details.get(header, {}).get("reason")} for header, value in raw_mapping.items() if not value]
    mapping = compile_mapping(datamodel, object_name, raw_mapping, owner)
    importer = Importer(
        target, datamodel, settings, table, object_name, mapping, mode=args.mode, match_key=args.match_key,
        date_format=args.date_format, decimal=args.decimal, time_zone=args.time_zone, redact=args.redact_credentials,
        owner=owner, keep_empty=args.keep_empty_cells,
    )
    results = [importer.convert_row(row) for row in table.rows]
    by_row = {result.number: result for result in results}
    for problem in table.problems:
        if problem.get("row") in by_row:
            by_row[problem["row"]].error(problem.get("column"), problem["rule"], problem["message"])
    planned: list[tuple[RowResult, str, str]] = []
    if owner:
        operations, row_by_record = importer.junction_operations(results)
    else:
        planned = importer.match(results)
        operations = [importer.operation(result, action, record_id) for result, action, record_id in planned]
        row_by_record = {record_id: result.number for result, _action, record_id in planned}
    row_errors = [error for result in results if result.duplicate_of is None for error in result.errors]
    invalid_rows = sorted({error["row"] for error in row_errors})
    skip = args.skip_invalid_rows
    origin = {"kind": "import", "ref": reference, "sha256": table.sha256, "occasion": f"Import {args.object} ({args.mode})"}

    def empty_report() -> Optional[dict[str, Any]]:
        if owner:
            return None
        rows_left = {operation["row"] for operation in operations}
        return importer.empty_cells([item for item in planned if item[0].number in rows_left])

    def import_info() -> dict[str, Any]:
        return {
            "format": IMPORT_FORMAT,
            "file": table.summary(),
            "object": args.object,
            "mode": args.mode,
            "match_key": args.match_key,
            "keep_empty_cells": bool(args.keep_empty_cells),
            "mapping": {column: target.text for column, target in mapping.items()},
            "row_errors": row_errors,
            "skipped_rows": invalid_rows if skip else [],
            "duplicates_ignored": importer.duplicates,
            "empty_cells": empty_report(),
            "credentials": importer.credentials,
        }

    plan = None
    core_errors: list[dict[str, Any]] = []
    attempts = 0
    while operations:
        blocking = [] if skip else [
            {"row": error["row"], "column": error["column"], "rule": error["rule"], "error": error["message"]} for error in row_errors
        ]
        request = {"actor": args.actor, "origin": origin, "operations": operations, "blocking_errors": blocking, "annotations": {"import": import_info()}}
        plan = plan_transaction(target, request)
        core_errors = map_core_errors(plan["errors"][len(blocking):], row_by_record, mapping)
        if not core_errors or not skip:
            break
        # The core found what the importer could not see; drop those rows and plan again.
        failing = {error["row"] for error in core_errors if error["row"] is not None}
        attempts += 1
        if not failing or attempts > 5:
            break
        row_errors += [error for error in core_errors if error["row"] in failing]
        invalid_rows = sorted(set(invalid_rows) | failing)
        operations = [operation for operation in operations if operation["row"] not in failing]
        core_errors = [error for error in core_errors if error["row"] not in failing]
        plan = None
    rule_counts = Counter(error["rule"] for error in row_errors + core_errors)
    empty_cells = empty_report()
    warnings = list(importer.warnings)
    if empty_cells and empty_cells["values"] and not args.keep_empty_cells:
        warnings.append(
            f"{empty_cells['values']} vorhandene Werte würden durch leere Zellen gelöscht (übliche CRM-Semantik, Details unter empty_cells); "
            "mit --keep-empty-cells bleiben sie erhalten."
        )
    report: dict[str, Any] = {
        "object": args.object,
        "mode": args.mode,
        "match_key": args.match_key,
        "junction": bool(owner),
        "file": table.summary(),
        "notes": table.notes,
        "mapping": {column: target.text for column, target in mapping.items()},
        "ignored_columns": ignored,
        "decimal_conventions": {column: {"convention": value[0], "reason": value[1]} for column, value in importer.conventions.items()},
        "normalizations": sorted(importer.normalizations.values(), key=lambda item: (item["column"], item["rule"])),
        "warnings": warnings,
        "credentials": importer.credentials[:100],
        "row_errors": (row_errors + core_errors)[:200],
        "row_error_count": len(row_errors) + len(core_errors),
        "row_errors_by_rule": dict(rule_counts),
        "duplicates_ignored": importer.duplicates[:200],
    }
    if empty_cells is not None:
        report["empty_cells"] = empty_cells
    if skip:
        report["skipped_rows"] = [
            {table.row_unit: row, "rules": sorted({error["rule"] for error in row_errors if error["row"] == row})}
            for row in invalid_rows
        ][:200]
    counts = {
        "rows": len(table.rows), "empty_rows": table.empty_rows, "invalid_rows": len(invalid_rows),
        "skipped": len(invalid_rows) if skip else 0, "duplicates_ignored": len(importer.duplicates),
    }
    if plan is None:
        counts.update({"create": 0, "update": 0, "restore": 0, "unchanged": 0})
        report["counts"] = counts
        report["state"] = "invalid" if row_errors else "nothing_to_plan"
        report["applicable"] = False
        report["next_step"] = "Zeilenfehler beheben oder fehlerhafte Zeilen mit --skip-invalid-rows weglassen" if row_errors else "die Datei enthält keine Zeilen zum Import"
        return report, 1 if row_errors else 0
    final = plan
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(final, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    counts.update(count_results(final, operations))
    if not owner:
        counts.pop("links_added", None)
    report["counts"] = counts
    applicable = not final["errors"]
    report["state"] = "planned" if applicable else "invalid"
    report["applicable"] = applicable
    report["plan_file"] = output.name
    report["plan_sha256"] = final["plan_sha256"]
    report["files"] = len(final["files"])
    report["row_changes"] = row_changes(final)
    report["summary"] = final["summary"]
    report["plan_warnings"] = final["warnings"][:50]
    if applicable:
        report["next_step"] = "Bericht mit dem Nutzer prüfen; nach Zustimmung apply mit --expect-plan-sha256 aufrufen, danach Ansichten neu bauen und lint"
    else:
        report["next_step"] = "Zeilenfehler beheben und neu planen oder fehlerhafte Zeilen mit --skip-invalid-rows weglassen"
    return report, 0 if applicable else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def reading(command: argparse.ArgumentParser) -> None:
        command.add_argument("--target", required=True)
        command.add_argument("--lock-token", required=True)
        command.add_argument("--file", required=True, help="CSV, XLSX or JSON file to import")
        command.add_argument("--encoding", help="Force a text encoding, for example cp1252")
        command.add_argument("--sheet", help="XLSX sheet name or 1-based number (default: first sheet)")
        command.add_argument("--delimiter", help="Force the CSV delimiter: ; , | or tab")
        command.add_argument("--date-format", choices=["DMY", "MDY", "YMD"], help="Order of slash or dash dates such as 01/02/2024")
        command.add_argument("--decimal", choices=["comma", "dot"], help="Decimal separator of text numbers")
        command.add_argument("--time-zone", help="Zone for times without offset (default: CRM settings)")

    inspect_parser = sub.add_parser("inspect", help="Report how a file would be read and mapped")
    reading(inspect_parser)
    inspect_parser.add_argument("--object", help="Object to map to (company, person, ..., noteTarget, taskTarget)")
    inspect_parser.add_argument("--write-mapping", help="Write the proposed mapping as JSON (outside the wiki)")
    plan_parser = sub.add_parser("plan", help="Plan the import as one transaction")
    reading(plan_parser)
    plan_parser.add_argument("--object", required=True)
    group = plan_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mapping-file")
    group.add_argument("--auto-mapping", action="store_true")
    plan_parser.add_argument("--match-key", required=True, help="id or one unique field, for example emails.primaryEmail")
    plan_parser.add_argument("--mode", required=True, choices=MODES)
    plan_parser.add_argument("--actor", required=True)
    plan_parser.add_argument("--origin-ref", required=True, help="Portable name of the source file, stored in the event origin")
    plan_parser.add_argument("--output", required=True, help="Plan file outside the wiki")
    plan_parser.add_argument("--skip-invalid-rows", action="store_true")
    plan_parser.add_argument("--redact-credentials", action="store_true")
    plan_parser.add_argument("--keep-empty-cells", action="store_true",
                             help="Empty cells leave existing values unchanged (default: they clear them, as is usual in CRMs)")
    apply_parser = sub.add_parser("apply", help="Apply a reviewed plan")
    apply_parser.add_argument("--target", required=True)
    apply_parser.add_argument("--lock-token", required=True)
    apply_parser.add_argument("--plan-file", required=True)
    apply_parser.add_argument("--expect-plan-sha256", required=True)
    apply_parser.add_argument("--confirm-destructive", action="store_true")
    args = parser.parse_args()
    target = Path(args.target).expanduser().resolve()
    require_lock(target, args.lock_token)
    try:
        if args.command == "inspect":
            print(json.dumps(inspect(target, args), ensure_ascii=False, indent=2))
            return 0
        if args.command == "plan":
            report, code = plan_import(target, args)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return code
        plan = verify_plan(json.loads(Path(args.plan_file).expanduser().read_text(encoding="utf-8")), args.expect_plan_sha256)
        result, code = apply_transaction(target, args.lock_token, plan, confirm_destructive=args.confirm_destructive)
        info = (plan.get("annotations") or {}).get("import")
        if isinstance(info, dict):
            result["import"] = {key: info.get(key) for key in ("object", "mode", "match_key", "keep_empty_cells")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except StalePlanError as exc:
        print(json.dumps({"state": "stale_plan", "writes": 0, "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 3
    except (TabularError, MappingError, CrmError, ValueProblem, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"state": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
