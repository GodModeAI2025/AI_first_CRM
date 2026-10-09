#!/usr/bin/env python3
"""CRM record layer of AI First CRM: data model, record files, events, transactions.

The CRM layer keeps structured records (companies, people, opportunities, tasks,
notes, and every custom object) next to the evidence-bound knowledge wiki. It is
following the data model of common CRM systems so that imports and exports stay compatible,
but it lives in plain files: one Markdown file per record below ``records/``,
an append-only event log below ``meta/crm-events/``, and its schema below
``schema/crm/``. Records are data, not claims: they carry no claim blocks and
are never translated. Their provenance is the event log, where every change
names its actor and origin.

Every write goes through a hash-bound transaction plan, exactly like the other
maintenance helpers: plan first, apply only the identical plan, snapshot the
touched files before writing.
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from frontmatter_contract import FrontmatterError, dump_frontmatter, parse_document

DATAMODEL_FORMAT = "lmwiki-crm-datamodel/1"
EVENT_FORMAT = "lmwiki-crm-event/1"
PLAN_FORMAT = "lmwiki-crm-transaction/1"
DATAMODEL_PATH = "schema/crm/datamodel.json"
SETTINGS_PATH = "schema/crm/settings.json"
ROLES_PATH = "schema/crm/roles.json"
VIEWS_PATH = "schema/crm/views.json"
DASHBOARDS_PATH = "schema/crm/dashboards.json"
WORKFLOWS_DIR = "schema/crm/workflows"
RECORDS_DIR = "records"
EVENTS_DIR = "meta/crm-events"

NAME_RE = re.compile(r"^[a-z][a-zA-Z0-9]{0,62}$")
OPTION_VALUE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_]{0,62}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INSTANT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?(Z|[+-]\d{2}:?\d{2})?$"
)
DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
EMAIL_RE = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$")
CURRENCY_CODE_RE = re.compile(r"^[A-Z]{3}$")
WIKILINK_RE = re.compile(r"^\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]$")
RECORD_LINK_RE = re.compile(r"^records/([a-z0-9]+(?:-[a-z0-9]+)*)/([0-9a-f-]{36})$")
ACTOR_RE = re.compile(r"^(?:agent/[A-Za-z0-9][A-Za-z0-9._-]{0,63}|human:[^\s]{1,128}|process:[^\s]{1,128})$")
RICHTEXT_BLOCK = re.compile(
    r"<!--\s*crm:richtext\s+([a-z][a-zA-Z0-9]*)\s*-->\n?(.*?)\n?<!--\s*/crm:richtext\s*-->",
    re.DOTALL,
)

FIELD_TYPES = {
    "ACTOR", "ADDRESS", "ARRAY", "BOOLEAN", "CURRENCY", "DATE", "DATE_TIME", "EMAILS", "FILES",
    "FULL_NAME", "LINKS", "MORPH_RELATION", "MULTI_SELECT", "NUMBER", "NUMERIC", "PHONES",
    "POSITION", "RATING", "RAW_JSON", "RELATION", "RICH_TEXT", "SELECT", "TEXT", "TS_VECTOR", "UUID",
}
# Field types the layer keeps as system keys or does not store as user fields.
SYSTEM_ONLY_TYPES = {"ACTOR", "POSITION", "TS_VECTOR"}
COMPOSITE_SUBFIELDS: dict[str, tuple[str, ...]] = {
    "FULL_NAME": ("firstName", "lastName"),
    "ADDRESS": (
        "addressStreet1", "addressStreet2", "addressCity", "addressPostcode",
        "addressState", "addressCountry", "addressLat", "addressLng",
    ),
    "CURRENCY": ("amountMicros", "currencyCode"),
    "EMAILS": ("primaryEmail", "additionalEmails"),
    "LINKS": ("primaryLinkUrl", "primaryLinkLabel", "secondaryLinks"),
    "PHONES": ("primaryPhoneNumber", "primaryPhoneCountryCode", "primaryPhoneCallingCode", "additionalPhones"),
}
# The subfield that identifies a composite value for display, emptiness and uniqueness.
PRIMARY_SUBFIELD = {
    "FULL_NAME": None,
    "ADDRESS": "addressCity",
    "CURRENCY": "amountMicros",
    "EMAILS": "primaryEmail",
    "LINKS": "primaryLinkUrl",
    "PHONES": "primaryPhoneNumber",
}
RATING_VALUES = ("RATING_1", "RATING_2", "RATING_3", "RATING_4", "RATING_5")
RELATION_TYPES = {"MANY_TO_ONE", "ONE_TO_MANY"}
ON_DELETE = {"SET_NULL", "CASCADE", "RESTRICT"}
SYSTEM_KEYS = (
    "crm_id", "crm_object", "crm_title", "crm_created_at", "crm_updated_at",
    "crm_created_by", "crm_updated_by", "crm_created_source", "crm_deleted_at", "crm_position",
)
CREATED_SOURCES = {"MANUAL", "IMPORT", "API", "EMAIL", "CALENDAR", "WORKFLOW", "AGENT", "MERGE"}
ORIGIN_KINDS = {"manual", "import", "email", "calendar", "workflow", "merge", "restore", "agent", "api", "migration"}
OPERATIONS = {"create", "update", "upsert", "delete", "restore", "destroy", "merge", "erase"}
DESTRUCTIVE_OPERATIONS = {"destroy", "merge", "erase"}
ERASED = "[erased]"


class CrmError(ValueError):
    """A CRM contract violation that stops the current operation."""


class OperationError(CrmError):
    """A single requested operation cannot be planned."""


# ---------------------------------------------------------------------------
# small utilities


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def kebab(name: str) -> str:
    """workspaceMember -> workspace-member; the record directory of an object."""
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


def new_record_id() -> str:
    return str(uuid.uuid4())


def new_event_id() -> str:
    return "evt-" + uuid.uuid4().hex[:16]


def new_transaction_id() -> str:
    return "txn-" + uuid.uuid4().hex[:16]


def parse_instant(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 instant on every supported Python, including a trailing Z.

    A value without an offset is read as UTC, because a CRM export without a
    zone carries no better information and a guess per machine would differ.
    """
    if not isinstance(value, str):
        return None
    match = INSTANT_RE.fullmatch(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    try:
        micro = int((fraction or "0")[:6].ljust(6, "0"))
        moment = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0), micro)
    except ValueError:
        return None
    if offset and offset != "Z":
        sign = 1 if offset[0] == "+" else -1
        digits = offset[1:].replace(":", "")
        delta_minutes = sign * (int(digits[:2]) * 60 + int(digits[2:]))
        from datetime import timedelta

        moment = moment - timedelta(minutes=delta_minutes)
    return moment.replace(tzinfo=timezone.utc)


def format_instant(moment: datetime) -> str:
    moment = moment.astimezone(timezone.utc).replace(microsecond=0)
    return moment.isoformat().replace("+00:00", "Z")


def valid_date(value: Any) -> bool:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def valid_actor(value: Any) -> bool:
    return isinstance(value, str) and bool(ACTOR_RE.fullmatch(value))


def normalize_email(value: str) -> str:
    return value.strip().lower()


# Fields that hold a company's web domain: their identity is the host alone, as a domain is.
DOMAIN_FIELDS = frozenset({"domainName"})


def _host_key(host: str) -> str:
    """Host identity: no user info, port or trailing dot, lowercase, without www., IDN labels in xn-- form."""
    import unicodedata

    host = host.rsplit("@", 1)[-1]
    if not host.startswith("["):
        host = host.split(":", 1)[0]
    host = host.strip().rstrip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    labels = []
    for label in host.split("."):
        label = unicodedata.normalize("NFC", label)
        if any(ord(character) > 127 for character in label):
            try:
                label = "xn--" + label.encode("punycode").decode("ascii")
            except UnicodeError:
                pass
        labels.append(label)
    return ".".join(labels)


def normalize_url(value: str, host_only: bool = False) -> str:
    """URL identity used for uniqueness and matching: scheme, user info, port, www., case, a trailing dot or
    slash and the fragment do not count, and an internationalized host matches its xn-- form. For a
    domain field (host_only) path and query do not count either, so acme.de/kontakt is acme.de."""
    text = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", value.strip()).split("#", 1)[0]
    match = re.match(r"([^/?]*)(.*)", text, re.DOTALL)
    host = _host_key(match.group(1) if match else text)
    if host_only:
        return host
    rest = (match.group(2) if match else "").lower()
    return host + (rest.rstrip("/") if rest.strip("/") else "")


def portable_reference(value: Any) -> bool:
    """A reference stored in the wiki must not reveal or depend on a local machine path."""
    if value in (None, ""):
        return True
    if not isinstance(value, str):
        return False
    text = value.strip()
    lowered = text.lower()
    if lowered.startswith("file:") or text.startswith(("/", "~", "\\")):
        return False
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return False
    if ".." in PurePosixPath(text.replace("\\", "/")).parts:
        return False
    return True


# ---------------------------------------------------------------------------
# artifacts the knowledge space keeps next to the records: stored files and the outbox

FILES_DIR = "records/_files"
OUTBOX_DIR = "records/_outbox"
ARTIFACT_DIRS = ("_files", "_outbox")
FILE_STORE_LIMIT = 25 * 1024 * 1024
STORED_REF_IN_TEXT = re.compile(r"records/_files/[0-9a-f]{2}/[0-9a-f]{64}(?:\.[a-z0-9]{1,10}){0,2}")
STORED_FILE_RE = re.compile(r"^records/_files/([0-9a-f]{2})/([0-9a-f]{64})((?:\.[a-z0-9]{1,10}){0,2})$")
OUTBOX_PATH_RE = re.compile(
    r"^records/_outbox/(?:[A-Za-z0-9][A-Za-z0-9._-]{0,120}/){1,4}[A-Za-z0-9][A-Za-z0-9._-]{0,220}\.(?:eml|ics|json|txt)$"
)
# Suffixes that wiki tools read as pages or data, or that a browser would run, are kept inert with .txt.
INERT_SUFFIXES = {".md", ".markdown", ".json", ".jsonl", ".html", ".htm", ".xhtml", ".svg", ".js", ".mjs", ".xml", ".py", ".sh", ".bat", ".ps1"}
TEXT_SUFFIXES = {".txt", ".csv", ".tsv", ".md", ".markdown", ".json", ".jsonl", ".eml", ".ics", ".vcf", ".xml", ".html", ".htm",
                 ".log", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".env", ".py", ".sh", ".js"}


def clean_file_name(name: str) -> str:
    """The display name of a stored file: the last path part, no control characters, at most 180 characters."""
    text = PurePosixPath(str(name).replace("\\", "/")).name
    text = re.sub(r"[\x00-\x1f\x7f]", "", text).strip()
    return (text[:180].strip() or "datei")


def stored_file_path(sha256: str, name: str) -> str:
    """Content-addressed place of a stored file; the original suffix stays so the file opens normally."""
    suffix = PurePosixPath(clean_file_name(name)).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ""
    if suffix in INERT_SUFFIXES:
        suffix += ".txt"
    elif suffix == ".tmp":
        suffix += ".bin"  # releases skip names ending in .tmp
    return f"{FILES_DIR}/{sha256[:2]}/{sha256}{suffix}"


def files_items(value: Any) -> list[dict[str, Any]]:
    """The items of a FILES value given as JSON text or as a list."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else []
        except json.JSONDecodeError:
            return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def stored_refs(value: Any) -> set[str]:
    return {str(item.get("ref")) for item in files_items(value) if str(item.get("ref") or "").startswith(FILES_DIR + "/")}


def stored_file_problems(item: dict[str, Any]) -> list[str]:
    ref = str(item.get("ref") or "")
    if not ref.startswith(FILES_DIR + "/"):
        return []
    match = STORED_FILE_RE.fullmatch(ref)
    if not match or match.group(1) != match.group(2)[:2]:
        return [f"stored file reference {ref!r} must have the form {FILES_DIR}/<first two hex digits>/<sha256>[.suffix]"]
    if item.get("sha256") != match.group(2):
        return [f"stored file reference {ref!r} needs its sha256 {match.group(2)}"]
    return []


def is_text_artifact(name: str, data: bytes) -> bool:
    """Whether a file can be read as text and therefore screened for credentials."""
    if any(suffix.lower() in TEXT_SUFFIXES for suffix in PurePosixPath(name).suffixes):
        return True
    if len(data) > 2 * 1024 * 1024 or b"\0" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stored_record_refs(records: Iterable["Record"], datamodel: "DataModel") -> dict[str, set[str]]:
    """Stored file path -> ids of the records whose FILES fields refer to it."""
    found: dict[str, set[str]] = {}
    for record in records:
        for name, definition in datamodel.fields(record.object).items():
            if definition.get("type") == "FILES":
                for ref in stored_refs(record.data.get(name)):
                    found.setdefault(ref, set()).add(record.id)
    return found


def artifact_files(target: Path) -> list[str]:
    """Every file below the stored-file and outbox directories, wiki-relative and sorted."""
    found = []
    for folder in (FILES_DIR, OUTBOX_DIR):
        root = target / folder
        if root.is_dir():
            found.extend(path.relative_to(target).as_posix() for path in root.rglob("*") if path.is_file()
                         and path.name not in {".DS_Store", "Thumbs.db", "desktop.ini"})
    return sorted(found)


# ---------------------------------------------------------------------------
# data model


@dataclass
class DataModel:
    raw: dict[str, Any]
    sha256: str

    @property
    def objects(self) -> dict[str, dict[str, Any]]:
        return self.raw["objects"]

    def object(self, name: str) -> dict[str, Any]:
        if name not in self.objects:
            raise OperationError(f"unknown object {name!r}")
        return self.objects[name]

    def fields(self, object_name: str) -> dict[str, dict[str, Any]]:
        return self.object(object_name)["fields"]

    def field(self, object_name: str, field_name: str) -> dict[str, Any]:
        fields = self.fields(object_name)
        if field_name not in fields:
            raise OperationError(f"unknown field {object_name}.{field_name}")
        return fields[field_name]

    def directory(self, object_name: str) -> str:
        return f"{RECORDS_DIR}/{kebab(object_name)}"

    def object_for_directory(self, directory: str) -> Optional[str]:
        for name in self.objects:
            if kebab(name) == directory:
                return name
        return None

    def label_field(self, object_name: str) -> str:
        return str(self.object(object_name).get("labelField") or "")

    def stored_fields(self, object_name: str) -> list[tuple[str, dict[str, Any]]]:
        """Fields whose values live in the record file (not computed inverses)."""
        result = []
        for name, definition in self.fields(object_name).items():
            ftype = definition.get("type")
            if ftype == "RELATION" and definition.get("relation", {}).get("type") == "ONE_TO_MANY":
                continue
            result.append((name, definition))
        return result

    def relation_fields_targeting(self, target_object: str) -> list[tuple[str, str, dict[str, Any]]]:
        """(object, field, definition) of every stored relation that may point at target_object."""
        found = []
        for object_name in self.objects:
            for field_name, definition in self.stored_fields(object_name):
                if definition.get("type") == "RELATION":
                    if definition["relation"].get("target") == target_object:
                        found.append((object_name, field_name, definition))
                elif definition.get("type") == "MORPH_RELATION":
                    if target_object in definition["relation"].get("targets", []):
                        found.append((object_name, field_name, definition))
        return found


def frontmatter_keys(field_name: str, definition: dict[str, Any]) -> list[str]:
    ftype = definition.get("type")
    if ftype in COMPOSITE_SUBFIELDS:
        return [f"{field_name}.{sub}" for sub in COMPOSITE_SUBFIELDS[ftype]]
    if ftype == "RICH_TEXT":
        return []
    if ftype == "RELATION" and definition.get("relation", {}).get("type") == "ONE_TO_MANY":
        return []
    return [field_name]


def validate_datamodel(raw: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(raw, dict) or raw.get("format") != DATAMODEL_FORMAT:
        return [f"{DATAMODEL_PATH}: format must be {DATAMODEL_FORMAT}"]
    objects = raw.get("objects")
    if not isinstance(objects, dict) or not objects:
        return [f"{DATAMODEL_PATH}: objects must be a non-empty mapping"]
    deleted = raw.get("deletedObjects", {})
    if not isinstance(deleted, dict):
        errors.append(f"{DATAMODEL_PATH}: deletedObjects must be a mapping of object name to tombstone")
        deleted = {}
    for name, tombstone in deleted.items():
        if name in objects:
            errors.append(f"{DATAMODEL_PATH}: deletedObjects.{name} is also an active object")
        if not isinstance(tombstone, dict) or not isinstance(tombstone.get("deletedAt"), str):
            errors.append(f"{DATAMODEL_PATH}: deletedObjects.{name} needs at least deletedAt")
    directories: dict[str, str] = {}
    for object_name, definition in objects.items():
        where = f"{DATAMODEL_PATH}: object {object_name}"
        if not isinstance(object_name, str) or not NAME_RE.fullmatch(object_name) or object_name.startswith("crm"):
            errors.append(f"{where}: name must be camelCase ASCII and must not start with 'crm'")
            continue
        directory = kebab(object_name)
        if directory in directories:
            errors.append(f"{where}: directory {directory} collides with object {directories[directory]}")
        directories[directory] = object_name
        if not isinstance(definition, dict):
            errors.append(f"{where}: definition must be a mapping")
            continue
        for key in ("labelSingular", "labelPlural"):
            if not isinstance(definition.get(key), str) or not definition.get(key, "").strip():
                errors.append(f"{where}: {key} is required")
        fields = definition.get("fields")
        if not isinstance(fields, dict) or not fields:
            errors.append(f"{where}: fields must be a non-empty mapping")
            continue
        label_field = definition.get("labelField")
        if label_field not in fields:
            errors.append(f"{where}: labelField {label_field!r} is not a field of the object")
        elif fields[label_field].get("type") not in {"TEXT", "FULL_NAME", "EMAILS", "LINKS", "UUID", "NUMBER"}:
            errors.append(f"{where}: labelField must be TEXT, FULL_NAME, EMAILS, LINKS, UUID or NUMBER")
        for flag in ("active", "standard"):
            if flag in definition and not isinstance(definition[flag], bool):
                errors.append(f"{where}: {flag} must be a boolean")
        for field_name, field_def in fields.items():
            errors.extend(_validate_field(object_name, field_name, field_def, objects))
    return errors


def _validate_field(object_name: str, field_name: str, definition: Any, objects: dict[str, Any]) -> list[str]:
    where = f"{DATAMODEL_PATH}: field {object_name}.{field_name}"
    errors: list[str] = []
    if not isinstance(field_name, str) or not NAME_RE.fullmatch(field_name) or field_name.startswith("crm"):
        return [f"{where}: name must be camelCase ASCII and must not start with 'crm'"]
    if not isinstance(definition, dict):
        return [f"{where}: definition must be a mapping"]
    ftype = definition.get("type")
    if ftype not in FIELD_TYPES:
        return [f"{where}: unknown type {ftype!r}"]
    if ftype in SYSTEM_ONLY_TYPES:
        return [f"{where}: type {ftype} is kept as a system key and cannot be a user field"]
    if not isinstance(definition.get("label"), str) or not definition.get("label", "").strip():
        errors.append(f"{where}: label is required")
    for flag in ("nullable", "unique", "active", "standard"):
        if flag in definition and not isinstance(definition[flag], bool):
            errors.append(f"{where}: {flag} must be a boolean")
    if definition.get("unique") and ftype in {"RICH_TEXT", "RELATION", "MORPH_RELATION", "MULTI_SELECT", "ARRAY", "FILES", "RAW_JSON", "BOOLEAN", "FULL_NAME", "ADDRESS", "CURRENCY"}:
        errors.append(f"{where}: unique is not supported for type {ftype}")
    if ftype in {"SELECT", "MULTI_SELECT"}:
        options = definition.get("options")
        if not isinstance(options, list) or not options:
            errors.append(f"{where}: options must be a non-empty list")
        else:
            seen: set[str] = set()
            for option in options:
                value = option.get("value") if isinstance(option, dict) else None
                if not isinstance(value, str) or not OPTION_VALUE_RE.fullmatch(value):
                    errors.append(f"{where}: option value {value!r} must be UPPER_SNAKE_CASE")
                elif value in seen:
                    errors.append(f"{where}: duplicate option value {value}")
                else:
                    seen.add(value)
                if isinstance(option, dict) and not str(option.get("label") or "").strip():
                    errors.append(f"{where}: option {value} needs a label")
    if ftype == "RELATION":
        relation = definition.get("relation")
        if not isinstance(relation, dict):
            errors.append(f"{where}: relation settings are required")
        else:
            if relation.get("type") not in RELATION_TYPES:
                errors.append(f"{where}: relation type must be MANY_TO_ONE or ONE_TO_MANY")
            target = relation.get("target")
            if target not in objects:
                errors.append(f"{where}: relation target {target!r} is not an object")
            if relation.get("type") == "MANY_TO_ONE" and relation.get("onDelete", "SET_NULL") not in ON_DELETE:
                errors.append(f"{where}: onDelete must be SET_NULL, CASCADE or RESTRICT")
            inverse = relation.get("inverse")
            if relation.get("type") == "ONE_TO_MANY":
                target_fields = objects.get(target, {}).get("fields", {}) if isinstance(objects.get(target), dict) else {}
                partner = target_fields.get(inverse) if isinstance(target_fields, dict) else None
                if not isinstance(partner, dict) or partner.get("type") != "RELATION" or partner.get("relation", {}).get("type") != "MANY_TO_ONE" or partner.get("relation", {}).get("target") != object_name:
                    errors.append(f"{where}: ONE_TO_MANY needs a MANY_TO_ONE inverse {target}.{inverse} pointing back")
    if ftype == "MORPH_RELATION":
        relation = definition.get("relation")
        if not isinstance(relation, dict) or not isinstance(relation.get("targets"), list) or not relation.get("targets"):
            errors.append(f"{where}: MORPH_RELATION needs relation.targets")
        else:
            for target in relation["targets"]:
                if target not in objects:
                    errors.append(f"{where}: morph target {target!r} is not an object")
            if "multiple" in relation and not isinstance(relation["multiple"], bool):
                errors.append(f"{where}: relation.multiple must be a boolean")
            if relation.get("onDelete", "SET_NULL") not in ON_DELETE:
                errors.append(f"{where}: onDelete must be SET_NULL, CASCADE or RESTRICT")
    if "default" in definition and definition["default"] is not None:
        try:
            _check_stored_value(ftype, definition, field_name, definition["default"], composite_default=True)
        except CrmError as exc:
            errors.append(f"{where}: invalid default: {exc}")
    return errors


def load_datamodel(target: Path) -> DataModel:
    path = target / DATAMODEL_PATH
    if not path.is_file():
        raise CrmError("this wiki has no CRM layer yet (schema/crm/datamodel.json is missing)")
    content = path.read_bytes()
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CrmError(f"{DATAMODEL_PATH}: invalid JSON: {exc}") from exc
    errors = validate_datamodel(raw)
    if errors:
        raise CrmError("; ".join(errors[:20]))
    return DataModel(raw, sha256_bytes(content))


def has_crm(target: Path) -> bool:
    return (target / DATAMODEL_PATH).is_file() or (target / RECORDS_DIR).exists()


def load_json_file(target: Path, relative: str, default: Any) -> Any:
    path = target / relative
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CrmError(f"{relative}: invalid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# value checking


def _check_stored_value(ftype: str, definition: dict[str, Any], key: str, value: Any, *, composite_default: bool = False) -> None:
    """Raise CrmError when a stored (canonical) value does not fit the field type."""
    if value is None:
        return
    if composite_default and ftype in COMPOSITE_SUBFIELDS:
        if not isinstance(value, dict):
            raise CrmError("composite default must be a mapping of subfields")
        for sub, sub_value in value.items():
            if sub not in COMPOSITE_SUBFIELDS[ftype]:
                raise CrmError(f"unknown subfield {sub}")
            _check_subfield(ftype, sub, sub_value)
        return
    if ftype in {"TEXT", "UUID"}:
        if not isinstance(value, str):
            raise CrmError("expected text")
        if ftype == "UUID" and value and not UUID_RE.fullmatch(value):
            raise CrmError("expected a lowercase UUID")
    elif ftype == "NUMBER":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
            raise CrmError("expected a number")
    elif ftype == "NUMERIC":
        if not isinstance(value, str) or (value and not DECIMAL_RE.fullmatch(value)):
            raise CrmError("expected a decimal number written as text with a dot")
    elif ftype == "BOOLEAN":
        if not isinstance(value, bool):
            raise CrmError("expected true or false")
    elif ftype == "DATE":
        if value != "" and not valid_date(value):
            raise CrmError("expected a date YYYY-MM-DD")
    elif ftype == "DATE_TIME":
        if value != "" and (not isinstance(value, str) or not value.endswith("Z") or parse_instant(value) is None):
            raise CrmError("expected a UTC instant like 2026-10-06T14:00:00Z")
    elif ftype == "SELECT":
        if not isinstance(value, str) or (value and value not in option_values(definition)):
            raise CrmError(f"{value!r} is not an option of the field")
    elif ftype == "MULTI_SELECT":
        if not isinstance(value, list) or any(item not in option_values(definition) for item in value):
            raise CrmError("expected a list of option values of the field")
        if len(set(value)) != len(value):
            raise CrmError("multi-select values must not repeat")
    elif ftype == "RATING":
        if not isinstance(value, str) or (value and value not in RATING_VALUES):
            raise CrmError("expected RATING_1 to RATING_5")
    elif ftype in {"ARRAY"}:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise CrmError("expected a list of texts")
    elif ftype in {"RAW_JSON", "FILES"}:
        if not isinstance(value, str):
            raise CrmError("expected JSON written as text")
        if value:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise CrmError(f"invalid JSON: {exc.msg}") from exc
            if ftype == "FILES":
                if not isinstance(parsed, list) or any(not isinstance(item, dict) or not portable_reference(item.get("ref")) for item in parsed):
                    raise CrmError("FILES must be a JSON list of {name, ref} with portable references, never local paths")
                problems = [problem for item in parsed for problem in stored_file_problems(item)]
                if problems:
                    raise CrmError(problems[0])
    elif ftype == "RELATION":
        if not isinstance(value, str) or (value and parse_record_link(value) is None):
            raise CrmError("expected a record wikilink [[records/<object>/<id>|title]]")
    elif ftype == "MORPH_RELATION":
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, str) or (item and parse_record_link(item) is None):
                raise CrmError("expected record wikilinks [[records/<object>/<id>|title]]")
    else:
        raise CrmError(f"type {ftype} has no stored value")


def _check_subfield(ftype: str, sub: str, value: Any) -> None:
    if value is None:
        return
    if ftype == "ADDRESS" and sub in {"addressLat", "addressLng"}:
        if value == "":
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CrmError(f"{sub} must be a number")
        return
    if ftype == "CURRENCY" and sub == "amountMicros":
        if value == "":
            return
        if isinstance(value, bool) or not isinstance(value, int):
            raise CrmError("amountMicros must be an integer (amount times 1,000,000)")
        return
    if ftype == "CURRENCY" and sub == "currencyCode":
        if not isinstance(value, str) or (value and not CURRENCY_CODE_RE.fullmatch(value)):
            raise CrmError("currencyCode must be a three-letter ISO code such as EUR")
        return
    if ftype == "EMAILS" and sub == "additionalEmails":
        if not isinstance(value, list) or any(not isinstance(item, str) or not EMAIL_RE.fullmatch(item) for item in value):
            raise CrmError("additionalEmails must be a list of e-mail addresses")
        return
    if ftype == "EMAILS" and sub == "primaryEmail":
        if not isinstance(value, str) or (value and not EMAIL_RE.fullmatch(value)):
            raise CrmError("primaryEmail must be an e-mail address")
        return
    if sub in {"secondaryLinks", "additionalPhones"}:
        if not isinstance(value, str):
            raise CrmError(f"{sub} must be JSON written as text")
        if value:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise CrmError(f"{sub}: invalid JSON: {exc.msg}") from exc
            if not isinstance(parsed, list):
                raise CrmError(f"{sub} must be a JSON list")
        return
    if not isinstance(value, str):
        raise CrmError(f"{sub} must be text")


def option_values(definition: dict[str, Any]) -> list[str]:
    return [str(option.get("value")) for option in definition.get("options", []) if isinstance(option, dict)]


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


# ---------------------------------------------------------------------------
# record links


def record_link(datamodel: DataModel, object_name: str, record_id: str, title: str) -> str:
    label = re.sub(r"[\[\]|\n\r]", " ", title).strip() or record_id[:8]
    return f"[[{datamodel.directory(object_name)}/{record_id}|{label}]]"


def parse_record_link(value: str) -> Optional[tuple[str, str]]:
    """Return (object directory, record id) for a record wikilink, otherwise None."""
    match = WIKILINK_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    if not match:
        return None
    link = RECORD_LINK_RE.fullmatch(match.group(1).strip())
    if not link or not UUID_RE.fullmatch(link.group(2)):
        return None
    return link.group(1), link.group(2)


# ---------------------------------------------------------------------------
# record files


@dataclass
class Record:
    object: str
    id: str
    data: dict[str, Any]
    richtext: dict[str, str]
    path: str
    sha256: str = ""
    exists: bool = True

    @property
    def deleted(self) -> bool:
        return bool(self.data.get("crm_deleted_at"))

    def copy(self) -> "Record":
        return Record(self.object, self.id, dict(self.data), dict(self.richtext), self.path, self.sha256, self.exists)


def record_relative_path(datamodel: DataModel, object_name: str, record_id: str) -> str:
    return f"{datamodel.directory(object_name)}/{record_id}.md"


def parse_record_text(text: str, source: str) -> tuple[dict[str, Any], dict[str, str]]:
    try:
        document = parse_document(text, source, require_frontmatter=True)
    except FrontmatterError as exc:
        raise CrmError(str(exc)) from exc
    richtext = {name: content for name, content in RICHTEXT_BLOCK.findall(document.body)}
    return document.data, richtext


def record_title(datamodel: DataModel, object_name: str, data: dict[str, Any]) -> str:
    label_field = datamodel.label_field(object_name)
    definition = datamodel.fields(object_name).get(label_field, {})
    ftype = definition.get("type")
    title = ""
    if ftype == "FULL_NAME":
        title = " ".join(str(data.get(f"{label_field}.{sub}") or "").strip() for sub in ("firstName", "lastName")).strip()
    elif ftype in PRIMARY_SUBFIELD and PRIMARY_SUBFIELD[ftype]:
        title = str(data.get(f"{label_field}.{PRIMARY_SUBFIELD[ftype]}") or "").strip()
    else:
        raw = data.get(label_field)
        title = "" if raw is None else str(raw).strip()
    if not title:
        title = f"{datamodel.object(object_name).get('labelSingular', object_name)} {str(data.get('crm_id') or '')[:8]}".strip()
    return title


def compose_record(datamodel: DataModel, record: Record) -> str:
    """Write a record in canonical key order so equal content yields identical bytes."""
    data = record.data
    ordered: dict[str, Any] = {}
    for key in SYSTEM_KEYS:
        if key in data and not (key in {"crm_deleted_at", "crm_position", "crm_created_source"} and is_empty(data[key])):
            ordered[key] = data[key]
    for field_name, definition in datamodel.stored_fields(record.object):
        for key in frontmatter_keys(field_name, definition):
            if key in data and not is_empty(data[key]):
                ordered[key] = data[key]
    # Values of fields that were removed from the data model are kept, not dropped silently.
    for key, value in data.items():
        if key not in ordered and not is_empty(value) and key not in SYSTEM_KEYS:
            ordered[key] = value
    header = dump_frontmatter(ordered)
    lines = [f"# {data.get('crm_title') or record.id}", ""]
    for field_name, definition in datamodel.fields(record.object).items():
        if definition.get("type") != "RICH_TEXT":
            continue
        content = record.richtext.get(field_name, "")
        if not content.strip():
            continue
        lines.extend([f"<!-- crm:richtext {field_name} -->", content.strip("\n"), "<!-- /crm:richtext -->", ""])
    for field_name, content in record.richtext.items():
        if field_name not in datamodel.fields(record.object) and content.strip():
            lines.extend([f"<!-- crm:richtext {field_name} -->", content.strip("\n"), "<!-- /crm:richtext -->", ""])
    return header + "\n".join(lines).rstrip("\n") + "\n"


def iter_record_files(target: Path, datamodel: Optional[DataModel] = None, objects: Optional[Iterable[str]] = None) -> Iterable[tuple[str, Path]]:
    root = target / RECORDS_DIR
    if not root.is_dir():
        return
    wanted = None
    if datamodel is not None and objects is not None:
        wanted = {kebab(name) for name in objects}
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or (wanted is not None and directory.name not in wanted):
            continue
        for path in sorted(directory.glob("*.md")):
            yield directory.name, path


def load_record(target: Path, datamodel: DataModel, object_name: str, path: Path) -> Record:
    content = path.read_bytes()
    text = content.decode("utf-8")
    relative = path.relative_to(target).as_posix()
    data, richtext = parse_record_text(text, relative)
    return Record(object_name, path.stem, data, richtext, relative, sha256_bytes(content), True)


class RecordStore:
    """Lazy, per-object cache of the records currently on disk."""

    def __init__(self, target: Path, datamodel: DataModel):
        self.target = target
        self.datamodel = datamodel
        self._objects: dict[str, dict[str, Record]] = {}
        self.load_errors: list[str] = []

    def records(self, object_name: str) -> dict[str, Record]:
        if object_name not in self._objects:
            loaded: dict[str, Record] = {}
            directory = self.target / self.datamodel.directory(object_name)
            if directory.is_dir():
                for path in sorted(directory.glob("*.md")):
                    try:
                        record = load_record(self.target, self.datamodel, object_name, path)
                    except (CrmError, UnicodeDecodeError) as exc:
                        self.load_errors.append(f"{path.relative_to(self.target).as_posix()}: {exc}")
                        continue
                    loaded[record.id] = record
            self._objects[object_name] = loaded
        return self._objects[object_name]

    def get(self, object_name: str, record_id: str) -> Optional[Record]:
        return self.records(object_name).get(record_id)


# ---------------------------------------------------------------------------
# record validation


def validate_record(datamodel: DataModel, record: Record, *, exists_check=None) -> list[str]:
    """Check one record against the data model.

    ``exists_check(object_dir, record_id)`` returns the object name of an
    existing record or None; relation targets are verified through it.
    """
    errors: list[str] = []
    where = record.path
    data = record.data
    if data.get("crm_id") != record.id or not UUID_RE.fullmatch(record.id):
        errors.append(f"{where}: crm_id must equal the file name and be a lowercase UUID")
    if data.get("crm_object") != record.object:
        errors.append(f"{where}: crm_object must be {record.object}")
    for key in ("crm_created_at", "crm_updated_at"):
        value = data.get(key)
        if not isinstance(value, str) or not value.endswith("Z") or parse_instant(value) is None:
            errors.append(f"{where}: {key} must be a UTC instant")
    for key in ("crm_created_by", "crm_updated_by"):
        if not valid_actor(data.get(key)):
            errors.append(f"{where}: {key} must be an actor such as human:<id>, agent/<name> or process:<id>")
    if data.get("crm_deleted_at") not in (None, "") and parse_instant(data.get("crm_deleted_at")) is None:
        errors.append(f"{where}: crm_deleted_at must be a UTC instant or absent")
    if data.get("crm_created_source") not in (None, "") and data.get("crm_created_source") not in CREATED_SOURCES:
        errors.append(f"{where}: crm_created_source {data.get('crm_created_source')!r} is unknown")
    position = data.get("crm_position")
    if position not in (None, "") and (isinstance(position, bool) or not isinstance(position, (int, float))):
        errors.append(f"{where}: crm_position must be a number")
    if not isinstance(data.get("crm_title"), str) or not data.get("crm_title"):
        errors.append(f"{where}: crm_title is required")
    known_keys = set(SYSTEM_KEYS)
    for field_name, definition in datamodel.stored_fields(record.object):
        ftype = definition["type"]
        keys = frontmatter_keys(field_name, definition)
        known_keys.update(keys)
        if ftype in COMPOSITE_SUBFIELDS:
            for key in keys:
                if key in data:
                    try:
                        _check_subfield(ftype, key.split(".", 1)[1], data[key])
                    except CrmError as exc:
                        errors.append(f"{where}: {key}: {exc}")
        elif keys:
            if field_name in data:
                try:
                    _check_stored_value(ftype, definition, field_name, data[field_name])
                except CrmError as exc:
                    errors.append(f"{where}: {field_name}: {exc}")
                if exists_check is not None and ftype in {"RELATION", "MORPH_RELATION"} and not is_empty(data[field_name]):
                    values = data[field_name] if isinstance(data[field_name], list) else [data[field_name]]
                    if ftype == "MORPH_RELATION" and not definition["relation"].get("multiple", True) and isinstance(data[field_name], list):
                        errors.append(f"{where}: {field_name} accepts one record only")
                    allowed = [definition["relation"]["target"]] if ftype == "RELATION" else definition["relation"]["targets"]
                    for value in values:
                        parsed = parse_record_link(value) if isinstance(value, str) else None
                        if parsed is None:
                            continue
                        found = exists_check(parsed[0], parsed[1])
                        if found is None:
                            errors.append(f"{where}: {field_name} points to a missing record {parsed[0]}/{parsed[1]}")
                        elif found not in allowed:
                            errors.append(f"{where}: {field_name} may point to {', '.join(allowed)}, not {found}")
        if definition.get("nullable") is False and not record.deleted and definition.get("active", True):
            if field_is_empty(data, record.richtext, field_name, definition):
                errors.append(f"{where}: {field_name} is required and must not be empty")
    for key in data:
        if key not in known_keys:
            base = key.split(".", 1)[0]
            if base in datamodel.fields(record.object):
                errors.append(f"{where}: {key} is not a valid key of field {base}")
            else:
                # A value of a field that left the data model stays readable but is flagged.
                errors.append(f"{where}: {key} is not declared in the data model")
    for name in record.richtext:
        definition = datamodel.fields(record.object).get(name)
        if not definition or definition.get("type") != "RICH_TEXT":
            errors.append(f"{where}: rich-text block {name} is not a RICH_TEXT field")
    return errors


def field_is_empty(data: dict[str, Any], richtext: dict[str, str], field_name: str, definition: dict[str, Any]) -> bool:
    ftype = definition.get("type")
    if ftype == "RICH_TEXT":
        return not richtext.get(field_name, "").strip()
    if ftype in COMPOSITE_SUBFIELDS:
        return all(is_empty(data.get(key)) for key in frontmatter_keys(field_name, definition))
    return is_empty(data.get(field_name))


def unique_key(field_name: str, definition: dict[str, Any], data: dict[str, Any]) -> Optional[str]:
    """Normalized identity value of a unique field, or None when empty."""
    ftype = definition.get("type")
    if ftype == "EMAILS":
        value = data.get(f"{field_name}.primaryEmail")
        return normalize_email(value) if isinstance(value, str) and value.strip() else None
    if ftype == "LINKS":
        value = data.get(f"{field_name}.primaryLinkUrl")
        return normalize_url(value, field_name in DOMAIN_FIELDS) if isinstance(value, str) and value.strip() else None
    if ftype == "PHONES":
        value = data.get(f"{field_name}.primaryPhoneNumber")
        if not isinstance(value, str) or not value.strip():
            return None
        code = str(data.get(f"{field_name}.primaryPhoneCallingCode") or "")
        return re.sub(r"\D", "", code + value)
    value = data.get(field_name)
    if is_empty(value):
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def unique_fields(datamodel: DataModel, object_name: str) -> list[tuple[str, dict[str, Any]]]:
    return [(name, definition) for name, definition in datamodel.fields(object_name).items() if definition.get("unique")]


# ---------------------------------------------------------------------------
# events


def event_shard(at: str) -> str:
    moment = parse_instant(at) or datetime.now(timezone.utc)
    return f"{EVENTS_DIR}/{moment.strftime('%Y-%m')}.jsonl"


def read_events(target: Path) -> Iterable[dict[str, Any]]:
    root = target / EVENTS_DIR
    if not root.is_dir():
        return
    for shard in sorted(root.glob("*.jsonl")):
        for number, line in enumerate(shard.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CrmError(f"{shard.relative_to(target).as_posix()}:{number}: invalid JSON: {exc.msg}") from exc
            event["_shard"] = shard.relative_to(target).as_posix()
            event["_line"] = number
            yield event


def validate_event(event: Any, datamodel: Optional[DataModel]) -> list[str]:
    where = f"{event.get('_shard', EVENTS_DIR)}:{event.get('_line', '?')}" if isinstance(event, dict) else EVENTS_DIR
    if not isinstance(event, dict) or event.get("format") != EVENT_FORMAT:
        return [f"{where}: event format must be {EVENT_FORMAT}"]
    errors = []
    if not re.fullmatch(r"evt-[0-9a-f]{16}", str(event.get("event_id") or "")):
        errors.append(f"{where}: invalid event_id")
    if parse_instant(event.get("at")) is None:
        errors.append(f"{where}: invalid at")
    if not valid_actor(event.get("actor")):
        errors.append(f"{where}: invalid actor")
    if event.get("op") not in OPERATIONS | {"cascade-null"}:
        errors.append(f"{where}: invalid op {event.get('op')!r}")
    if datamodel is not None and event.get("object") not in datamodel.objects and event.get("object") not in datamodel.raw.get("deletedObjects", {}):
        errors.append(f"{where}: unknown object {event.get('object')!r}")
    if not UUID_RE.fullmatch(str(event.get("record_id") or "")):
        errors.append(f"{where}: invalid record_id")
    origin = event.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") not in ORIGIN_KINDS:
        errors.append(f"{where}: origin.kind must be one of {sorted(ORIGIN_KINDS)}")
    elif not portable_reference(origin.get("ref")):
        errors.append(f"{where}: origin.ref is not a portable reference")
    if not isinstance(event.get("changes"), dict):
        errors.append(f"{where}: changes must be a mapping")
    return errors


# ---------------------------------------------------------------------------
# value coercion for requested changes


def coerce_value(ftype: str, definition: dict[str, Any], key: str, value: Any) -> Any:
    """Turn a requested value into its canonical stored form or raise OperationError.

    Inputs are canonical, language-neutral values: ISO dates, dots as decimal
    separator, true/false, option API names. Locale-specific spreadsheets are
    normalized by the importer before they reach this function.
    """
    if value is None or (isinstance(value, str) and value.strip() == "" and ftype not in {"TEXT"}):
        return None
    try:
        if ftype in {"TEXT", "UUID"}:
            text = value if isinstance(value, str) else str(value)
            if ftype == "UUID":
                text = text.strip().lower()
                if not UUID_RE.fullmatch(text):
                    raise OperationError("expected a UUID")
            return text
        if ftype == "NUMBER":
            if isinstance(value, bool):
                raise OperationError("expected a number")
            if isinstance(value, (int, float)):
                number = value
            else:
                number = Decimal(str(value).strip())
                number = int(number) if number == number.to_integral_value() else float(number)
            if isinstance(number, float) and not math.isfinite(number):
                raise OperationError("expected a finite number")
            return number
        if ftype == "NUMERIC":
            text = str(value).strip()
            if not DECIMAL_RE.fullmatch(text):
                Decimal(text)  # raises for garbage
                text = format(Decimal(text).normalize(), "f")
            return text
        if ftype == "BOOLEAN":
            if isinstance(value, bool):
                return value
            lowered = str(value).strip().lower()
            if lowered in {"true", "1", "yes"}:
                return True
            if lowered in {"false", "0", "no"}:
                return False
            raise OperationError("expected true or false")
        if ftype == "DATE":
            text = str(value).strip()
            if "T" in text:
                text = text.split("T", 1)[0]
            if not valid_date(text):
                raise OperationError("expected a date YYYY-MM-DD")
            return text
        if ftype == "DATE_TIME":
            text = str(value).strip()
            if valid_date(text):
                text = text + "T00:00:00Z"
            moment = parse_instant(text)
            if moment is None:
                raise OperationError("expected an ISO-8601 instant such as 2026-10-06T14:00:00Z")
            return format_instant(moment)
        if ftype == "SELECT":
            text = str(value).strip()
            options = definition.get("options", [])
            for option in options:
                if text == option.get("value"):
                    return text
            for option in options:
                if text.casefold() == str(option.get("label", "")).casefold():
                    return option["value"]
            raise OperationError(f"{text!r} is not an option ({', '.join(option_values(definition))})")
        if ftype == "MULTI_SELECT":
            items = value if isinstance(value, list) else [part for part in re.split(r"[,;|]", str(value)) if part.strip()]
            result = []
            for item in items:
                coerced = coerce_value("SELECT", definition, key, item)
                if coerced and coerced not in result:
                    result.append(coerced)
            return result
        if ftype == "RATING":
            text = str(value).strip().upper()
            if text.isdigit():
                text = f"RATING_{text}"
            if text not in RATING_VALUES:
                raise OperationError("expected a rating from 1 to 5")
            return text
        if ftype == "ARRAY":
            items = value if isinstance(value, list) else [part.strip() for part in re.split(r"[,;|]", str(value)) if part.strip()]
            return [str(item) for item in items]
        if ftype in {"RAW_JSON", "FILES"}:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
            parsed = json.loads(text)
            if ftype == "FILES":
                if not isinstance(parsed, list) or any(not isinstance(item, dict) or not portable_reference(item.get("ref")) for item in parsed):
                    raise OperationError("FILES needs a list of {name, ref} or {name, upload}; local file paths are not allowed as references")
                problems = [problem for item in parsed for problem in stored_file_problems(item)]
                if problems:
                    raise OperationError(problems[0])
            return json.dumps(parsed, ensure_ascii=False, sort_keys=True)
    except (InvalidOperation, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, OperationError):
            raise
        raise OperationError(f"invalid value {value!r}: {exc}") from exc
    raise OperationError(f"values of type {ftype} cannot be set directly")


def coerce_subfield(ftype: str, sub: str, value: Any) -> Any:
    if value is None or (isinstance(value, str) and value.strip() == "" and sub not in {"additionalEmails"}):
        return None
    try:
        if ftype == "ADDRESS" and sub in {"addressLat", "addressLng"}:
            number = float(Decimal(str(value).strip()))
            return number
        if ftype == "CURRENCY" and sub == "amountMicros":
            if isinstance(value, bool):
                raise OperationError("amountMicros must be an integer")
            micros = Decimal(str(value).strip())
            if micros != micros.to_integral_value():
                raise OperationError("amountMicros must be an integer; use 'amount' to give a decimal amount")
            return int(micros)
        if ftype == "CURRENCY" and sub == "currencyCode":
            code = str(value).strip().upper()
            if not CURRENCY_CODE_RE.fullmatch(code):
                raise OperationError("currencyCode must be a three-letter ISO code")
            return code
        if ftype == "EMAILS" and sub == "primaryEmail":
            text = str(value).strip()
            if not EMAIL_RE.fullmatch(text):
                raise OperationError(f"{text!r} is not an e-mail address")
            return text
        if ftype == "EMAILS" and sub == "additionalEmails":
            items = value if isinstance(value, list) else [part.strip() for part in re.split(r"[,;\s]+", str(value)) if part.strip()]
            for item in items:
                if not EMAIL_RE.fullmatch(str(item)):
                    raise OperationError(f"{item!r} is not an e-mail address")
            return [str(item) for item in items]
        if sub in {"secondaryLinks", "additionalPhones"}:
            parsed = json.loads(value) if isinstance(value, str) else value
            if not isinstance(parsed, list):
                raise OperationError(f"{sub} must be a list")
            return json.dumps(parsed, ensure_ascii=False, sort_keys=True)
    except (InvalidOperation, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, OperationError):
            raise
        raise OperationError(f"invalid value {value!r} for {sub}: {exc}") from exc
    return value if isinstance(value, str) else str(value)


def amount_to_micros(amount: Any) -> int:
    """Convert a decimal amount (text with a dot, int, or Decimal) to integer micros without float error."""
    try:
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount).strip())
    except InvalidOperation as exc:
        raise OperationError(f"invalid amount {amount!r}") from exc
    micros = value * 1_000_000
    if micros != micros.to_integral_value():
        raise OperationError(f"amount {amount!r} has more than six decimal places")
    return int(micros)


# ---------------------------------------------------------------------------
# transactions


@dataclass
class Working:
    """Planned state of the records a transaction touches."""

    records: dict[tuple[str, str], Record] = field(default_factory=dict)
    destroyed: set[tuple[str, str]] = field(default_factory=set)
    created: set[tuple[str, str]] = field(default_factory=set)
    refs: dict[str, tuple[str, str]] = field(default_factory=dict)


class Planner:
    """Plans CRM operations against the records on disk plus everything planned so far.

    Stable interface for other helpers (crm_workflows simulates runs with it):
    Planner(target, datamodel, actor, origin, now=None, roles=None), run(operations),
    current(object, id), all_records(object), match_records(object, criteria),
    resolve_record(object, selector), and the attributes events, errors, warnings, working.
    Each operation is atomic: a failing one is rolled back through an undo journal and
    reported with its index, row, and object; the others stay planned.
    """
    def __init__(self, target: Path, datamodel: DataModel, actor: str, origin: dict[str, Any], now: Optional[str] = None, roles: Optional[dict[str, Any]] = None,
                 allow_uploads: bool = False):
        self.target = target
        # Only a request the agent wrote for crm_records.py may copy a local file into the wiki;
        # values from imports, e-mail files, or workflow payloads must never name a file to read.
        self.allow_uploads = allow_uploads
        self.datamodel = datamodel
        self.store = RecordStore(target, datamodel)
        self.actor = actor
        self.origin = origin
        self.now = now or utc_now()
        self.working = Working()
        self.events: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.txn = new_transaction_id()
        self.roles = roles
        # Lookup indexes {(object, storage key): {normalized value: {record ids}}}, kept current on every write.
        self._index: dict[tuple[str, str], dict[Any, set[str]]] = {}
        # Undo journal of the operation being planned: prior states only of what it touches.
        self._undo: Optional[dict[str, Any]] = None
        self._op_origin: dict[str, Any] = origin
        # Files to copy into the file store when the plan is applied: {stored path: {source, sha256, size}}.
        self.uploads: dict[str, dict[str, Any]] = {}
        # Records whose mentions of an erased person were redacted, and stored files dropped with them.
        self.redacted_ids: set[str] = set()
        self.identifier_redacted_ids: set[str] = set()
        self._acks: Optional[set[tuple[Any, Any]]] = None
        self.redacted_files: set[str] = set()

    # -- lookup ------------------------------------------------------------

    def current(self, object_name: str, record_id: str) -> Optional[Record]:
        key = (object_name, record_id)
        if key in self.working.destroyed:
            return None
        if key in self.working.records:
            return self.working.records[key]
        return self.store.get(object_name, record_id)

    def all_records(self, object_name: str) -> list[Record]:
        result = {}
        for record_id, record in self.store.records(object_name).items():
            result[record_id] = record
        for (obj, record_id), record in self.working.records.items():
            if obj == object_name:
                result[record_id] = record
        for obj, record_id in self.working.destroyed:
            if obj == object_name:
                result.pop(record_id, None)
        return list(result.values())

    def exists(self, object_dir: str, record_id: str) -> Optional[str]:
        object_name = self.datamodel.object_for_directory(object_dir)
        if object_name is None:
            return None
        return object_name if self.current(object_name, record_id) is not None else None

    def match_records(self, object_name: str, criteria: dict[str, Any], *, include_deleted: bool = True) -> list[Record]:
        if not isinstance(criteria, dict) or not criteria:
            raise OperationError("match needs at least one field")
        normalized = {}
        for key, value in criteria.items():
            key = self._storage_key(object_name, key)
            normalized[key] = self._normalize_match_value(object_name, key, value)
        if any(value is None for value in normalized.values()):
            raise OperationError(f"match values must not be empty: {criteria}")
        candidates: Optional[set[str]] = None
        for key, value in normalized.items():
            ids = self._lookup(object_name, key, value)
            candidates = set(ids) if candidates is None else candidates & ids
        found = []
        for record_id in sorted(candidates or set()):
            record = self.current(object_name, record_id)
            if record is None or (record.deleted and not include_deleted):
                continue
            found.append(record)
        return found

    def _hashable(self, value: Any) -> Any:
        return json.dumps(value, sort_keys=True, ensure_ascii=False) if isinstance(value, (list, dict)) else value

    def _lookup(self, object_name: str, key: str, value: Any) -> set[str]:
        index_key = (object_name, key)
        if index_key not in self._index:
            index: dict[Any, set[str]] = {}
            for record in self.all_records(object_name):
                normalized = self._normalize_match_value(object_name, key, self._record_value(record, key), stored=True)
                if normalized is not None:
                    index.setdefault(self._hashable(normalized), set()).add(record.id)
            self._index[index_key] = index
        return self._index[index_key].get(self._hashable(value), set())

    def _reindex(self, record: Record, before: Optional[dict[str, Any]]) -> None:
        """Move one record between index buckets after its data changed (or vanished)."""
        for (object_name, key), index in self._index.items():
            if object_name != record.object:
                continue
            old = self._normalize_match_value(object_name, key, self._value_from(before, record.id, key), stored=True) if before is not None else None
            new = self._normalize_match_value(object_name, key, self._record_value(record, key), stored=True) if record.data is not None else None
            if old == new and before is not None:
                continue
            if old is not None:
                bucket = index.get(self._hashable(old))
                if bucket is not None:
                    bucket.discard(record.id)
            if new is not None:
                index.setdefault(self._hashable(new), set()).add(record.id)

    def _value_from(self, data: dict[str, Any], record_id: str, key: str) -> Any:
        probe = Record("", record_id, data, {}, "")
        return self._record_value(probe, key)

    def _unindex(self, record: Record) -> None:
        for (object_name, key), index in self._index.items():
            if object_name != record.object:
                continue
            for bucket in index.values():
                bucket.discard(record.id)

    def _storage_key(self, object_name: str, key: str) -> str:
        """A composite field named without subfield matches on its identifying subfield."""
        if key in ("id", "crm_id") or "." in key:
            return key
        definition = self.datamodel.fields(object_name).get(key)
        if definition is None:
            raise OperationError(f"cannot match on unknown field {object_name}.{key}")
        ftype = definition["type"]
        if ftype in COMPOSITE_SUBFIELDS:
            primary = PRIMARY_SUBFIELD.get(ftype)
            return f"{key}.{primary}" if primary else f"{key}.*"
        return key

    def _record_value(self, record: Record, key: str) -> Any:
        if key in ("id", "crm_id"):
            return record.id
        if key.endswith(".*"):
            base = key[:-2]
            return " ".join(str(record.data.get(f"{base}.{sub}") or "").strip() for sub in ("firstName", "lastName")).strip()
        return record.data.get(key)

    def _normalize_match_value(self, object_name: str, key: str, value: Any, stored: bool = False) -> Any:
        if key in ("id", "crm_id"):
            return str(value).strip().lower() if value is not None else None
        base, _, sub = key.partition(".")
        definition = self.datamodel.fields(object_name).get(base)
        if definition is None:
            raise OperationError(f"cannot match on unknown field {object_name}.{key}")
        ftype = definition["type"]
        if value is None or value == "":
            return None
        if sub == "*":
            return " ".join(str(value).split()).casefold()
        if ftype == "EMAILS" and sub in ("", "primaryEmail"):
            return normalize_email(str(value)) if str(value).strip() else None
        if ftype == "LINKS" and sub in ("", "primaryLinkUrl"):
            return normalize_url(str(value), base in DOMAIN_FIELDS) if str(value).strip() else None
        if isinstance(value, str):
            return value.strip()
        return value

    def resolve_record(self, object_name: str, selector: Any, *, include_deleted: bool = True) -> Record:
        if isinstance(selector, str):
            parsed = parse_record_link(selector)
            record_id = parsed[1] if parsed else selector.strip().lower()
            record = self.current(object_name, record_id)
            if record is None:
                raise OperationError(f"{object_name} record {record_id} does not exist")
            return record
        if isinstance(selector, dict) and "ref" in selector:
            key = self.working.refs.get(str(selector["ref"]))
            if key is None or key[0] != object_name:
                raise OperationError(f"unknown reference {selector['ref']!r} for {object_name}")
            record = self.current(*key)
            if record is None:
                raise OperationError(f"reference {selector['ref']!r} was destroyed in this transaction")
            return record
        if isinstance(selector, dict) and "match" in selector:
            found = self.match_records(object_name, selector["match"], include_deleted=include_deleted)
            if not found:
                raise OperationError(f"no {object_name} record matches {selector['match']}")
            if len(found) > 1:
                raise OperationError(f"{len(found)} {object_name} records match {selector['match']}; the selection must be unique")
            return found[0]
        raise OperationError("record selector must be an id, a record link, {'ref': ...} or {'match': {...}}")

    # -- values -----------------------------------------------------------

    def apply_values(self, record: Record, values: dict[str, Any], changes: dict[str, Any]) -> None:
        if not isinstance(values, dict):
            raise OperationError("values must be a mapping")
        flat: dict[str, Any] = {}
        for key, value in values.items():
            if isinstance(value, dict) and key in self.datamodel.fields(record.object) and self.datamodel.fields(record.object)[key].get("type") in COMPOSITE_SUBFIELDS:
                for sub, sub_value in value.items():
                    flat[f"{key}.{sub}"] = sub_value
            else:
                flat[key] = value
        for key, value in flat.items():
            base, _, sub = key.partition(".")
            definition = self.datamodel.fields(record.object).get(base)
            if definition is None:
                raise OperationError(f"unknown field {record.object}.{base}")
            if definition.get("active", True) is False:
                raise OperationError(f"field {record.object}.{base} is deactivated")
            self._check_permission(record.object, "update", base)
            ftype = definition["type"]
            if ftype == "RELATION" and definition["relation"]["type"] == "ONE_TO_MANY":
                raise OperationError(f"{record.object}.{base} is the inverse side; set {definition['relation']['target']}.{definition['relation']['inverse']} instead")
            if ftype == "RICH_TEXT":
                text = "" if value is None else str(value)
                before = record.richtext.get(base, "")
                if text.strip() != before.strip():
                    record.richtext[base] = text
                    changes[f"richtext:{base}"] = {"before_sha256": sha256_text(before) if before else "", "after_sha256": sha256_text(text) if text else "", "after_chars": len(text)}
                continue
            if ftype in COMPOSITE_SUBFIELDS:
                if ftype == "CURRENCY" and sub == "amount":
                    new = None if value in (None, "") else amount_to_micros(value)
                    self._set(record, f"{base}.amountMicros", new, changes)
                    continue
                if sub == "":
                    if value in (None, ""):
                        for composite_key in frontmatter_keys(base, definition):
                            self._set(record, composite_key, None, changes)
                        continue
                    primary = PRIMARY_SUBFIELD.get(ftype)
                    if not primary:
                        raise OperationError(f"{record.object}.{base} needs subfields such as {COMPOSITE_SUBFIELDS[ftype][0]}")
                    sub = primary
                if sub not in COMPOSITE_SUBFIELDS[ftype]:
                    raise OperationError(f"{record.object}.{base} has no subfield {sub}")
                coerced = coerce_subfield(ftype, sub, value)
                stored = record.data.get(f"{base}.{sub}")
                if isinstance(coerced, str) and isinstance(stored, str) and stored and (
                    (ftype == "LINKS" and sub == "primaryLinkUrl" and normalize_url(coerced, base in DOMAIN_FIELDS) == normalize_url(stored, base in DOMAIN_FIELDS))
                    or (ftype == "EMAILS" and sub == "primaryEmail" and normalize_email(coerced) == normalize_email(stored))
                ):
                    # The same domain or address in another spelling is not a change.
                    continue
                self._set(record, f"{base}.{sub}", coerced, changes)
                continue
            if sub:
                raise OperationError(f"{record.object}.{base} has no subfields")
            if ftype in {"RELATION", "MORPH_RELATION"}:
                self._set(record, base, self._coerce_relation(record, base, definition, value), changes)
                continue
            if ftype == "FILES":
                value = self._stage_files(value)
            self._set(record, base, coerce_value(ftype, definition, base, value), changes)

    def _stage_files(self, value: Any) -> Any:
        """Replace {name, upload} items by stored-file items; the copy happens when the plan is applied."""
        items = value
        if isinstance(value, str):
            try:
                items = json.loads(value) if value.strip() else []
            except json.JSONDecodeError:
                return value
        if not isinstance(items, list) or not any(isinstance(item, dict) and "upload" in item for item in items):
            return value
        if not self.allow_uploads:
            raise OperationError("files can be stored only through a request planned with crm_records.py; imports, e-mail files and workflows cannot name a file to copy")
        return [self._stage_upload(item) if isinstance(item, dict) and "upload" in item else item for item in items]

    def _stage_upload(self, item: dict[str, Any]) -> dict[str, Any]:
        raw = item.get("upload")
        if not isinstance(raw, str) or not raw.strip():
            raise OperationError("upload needs the path of the file to store")
        source = Path(raw.strip()).expanduser()
        source = (source if source.is_absolute() else Path.cwd() / source).resolve()
        if not source.is_file():
            raise OperationError(f"the file to store does not exist: {source.name}")
        size = source.stat().st_size
        if size > FILE_STORE_LIMIT:
            raise OperationError(f"{source.name} has {size} bytes; files above {FILE_STORE_LIMIT // (1024 * 1024)} MB stay outside the wiki, store a link to them instead")
        data = source.read_bytes()
        digest = sha256_bytes(data)
        name = clean_file_name(str(item.get("name") or source.name))
        ref = stored_file_path(digest, name)
        if is_text_artifact(name, data):
            findings = record_credential_findings(self.target, ref, data.decode("utf-8", errors="replace"))
            if findings:
                kinds = ", ".join(sorted({kind for _line, kind in findings}))
                raise OperationError(f"{name} contains credential-like values ({kinds}); remove them before storing the file")
        else:
            note = f"{name} is stored without a credential screen because it is not a text file"
            if note not in self.warnings:
                self.warnings.append(note)
        existing = self.target / ref
        if not (existing.is_file() and sha256_file(existing) == digest):
            self.uploads[ref] = {"path": ref, "source": str(source), "sha256": digest, "size": size}
        return {"name": name, "ref": ref, "sha256": digest, "size": size,
                "type": mimetypes.guess_type(name)[0] or "application/octet-stream"}

    def stored_files(self) -> list[dict[str, Any]]:
        """The staged uploads that surviving records still refer to."""
        referenced: set[str] = set()
        for record in self.working.records.values():
            for name, definition in self.datamodel.fields(record.object).items():
                if definition.get("type") == "FILES":
                    referenced |= stored_refs(record.data.get(name))
        return [self.uploads[ref] for ref in sorted(referenced) if ref in self.uploads]

    def _set(self, record: Record, key: str, value: Any, changes: dict[str, Any]) -> None:
        before = record.data.get(key)
        if is_empty(before) and is_empty(value):
            record.data.pop(key, None)
            return
        if before == value:
            return
        previous = dict(record.data) if self._index else None
        if is_empty(value):
            record.data.pop(key, None)
        else:
            record.data[key] = value
        changes[key] = [before, value]
        if previous is not None:
            self._reindex(record, previous)

    def _coerce_relation(self, record: Record, base: str, definition: dict[str, Any], value: Any) -> Any:
        ftype = definition["type"]
        relation = definition["relation"]
        multiple = ftype == "MORPH_RELATION" and relation.get("multiple", True)
        if ftype == "RELATION":
            allowed = [relation["target"]]
        else:
            allowed = list(relation["targets"])
        current = record.data.get(base)
        current_links = current if isinstance(current, list) else ([current] if current else [])
        if isinstance(value, dict) and ("add" in value or "remove" in value) and multiple:
            links = list(current_links)
            for item in value.get("remove", []) or []:
                target = self._resolve_link_target(item, allowed)
                links = [link for link in links if parse_record_link(link) and parse_record_link(link)[1] != target[1]]
            for item in value.get("add", []) or []:
                target = self._resolve_link_target(item, allowed)
                if not any(parse_record_link(link) and parse_record_link(link)[1] == target[1] for link in links):
                    links.append(self._link(target))
            return links
        if value is None or value == "" or value == []:
            return [] if multiple else None
        if multiple:
            items = value if isinstance(value, list) else [value]
            links = []
            for item in items:
                link = self._link(self._resolve_link_target(item, allowed))
                if link not in links:
                    links.append(link)
            return links
        if isinstance(value, list):
            if len(value) != 1:
                raise OperationError(f"{record.object}.{base} accepts exactly one record")
            value = value[0]
        return self._link(self._resolve_link_target(value, allowed))

    def _resolve_link_target(self, item: Any, allowed: list[str]) -> tuple[str, str]:
        if isinstance(item, str):
            parsed = parse_record_link(item)
            if parsed:
                object_name = self.datamodel.object_for_directory(parsed[0])
                if object_name not in allowed:
                    raise OperationError(f"link target {parsed[0]} is not allowed here ({', '.join(allowed)})")
                if self.current(object_name, parsed[1]) is None:
                    raise OperationError(f"linked record {parsed[0]}/{parsed[1]} does not exist")
                return object_name, parsed[1]
            if ":" in item and item.split(":", 1)[0] in self.datamodel.objects:
                object_name, record_id = item.split(":", 1)
                if object_name not in allowed:
                    raise OperationError(f"link target {object_name} is not allowed here ({', '.join(allowed)})")
                if self.current(object_name, record_id.strip().lower()) is None:
                    raise OperationError(f"linked record {object_name}/{record_id} does not exist")
                return object_name, record_id.strip().lower()
            if len(allowed) == 1:
                record = self.resolve_record(allowed[0], item)
                return record.object, record.id
            raise OperationError("a link to one of several object types needs 'object:id', a record link, or {'object': ..., 'match': {...}}")
        if isinstance(item, dict):
            object_name = item.get("object")
            if object_name is None and len(allowed) == 1:
                object_name = allowed[0]
            if object_name not in allowed:
                raise OperationError(f"link target {object_name!r} is not allowed here ({', '.join(allowed)})")
            selector = {key: value for key, value in item.items() if key != "object"}
            if "id" in selector:
                selector = selector["id"]
            record = self.resolve_record(object_name, selector)
            return record.object, record.id
        raise OperationError("invalid relation value")

    def _link(self, target: tuple[str, str]) -> str:
        record = self.current(*target)
        title = record.data.get("crm_title", "") if record else ""
        return record_link(self.datamodel, target[0], target[1], str(title))

    def _check_permission(self, object_name: str, action: str, field_name: Optional[str] = None) -> None:
        if not self.roles:
            return
        allowed, reason = permission_allows(self.roles, self.actor, object_name, action, field_name)
        if not allowed:
            raise OperationError(reason)

    # -- operations -------------------------------------------------------

    def _journal_record(self, key: tuple[str, str]) -> None:
        if self._undo is not None and key not in self._undo["records"]:
            current = self.working.records.get(key)
            self._undo["records"][key] = current.copy() if current is not None else None

    def _journal_member(self, name: str, key: tuple[str, str]) -> None:
        if self._undo is not None and (name, key) not in self._undo["sets"]:
            self._undo["sets"][(name, key)] = key in getattr(self.working, name)

    def _journal_ref(self, ref: str) -> None:
        if self._undo is not None and ref not in self._undo["refs"]:
            self._undo["refs"][ref] = self.working.refs.get(ref, _MISSING)

    def touch(self, record: Record) -> Record:
        key = (record.object, record.id)
        self._journal_record(key)
        if key not in self.working.records:
            record = record.copy()
            self.working.records[key] = record
        return self.working.records[key]

    def emit(self, op: str, record: Record, changes: dict[str, Any], extra: Optional[dict[str, Any]] = None) -> None:
        if not changes and op in {"update", "upsert"}:
            return
        event = {
            "format": EVENT_FORMAT,
            "event_id": new_event_id(),
            "at": self.now,
            "actor": self.actor,
            "op": op,
            "object": record.object,
            "record_id": record.id,
            "changes": changes,
            "origin": self._op_origin,
            "txn": self.txn,
        }
        if extra:
            event.update(extra)
        self.events.append(event)

    def op_create(self, op: dict[str, Any]) -> None:
        object_name = op.get("object")
        self.datamodel.object(object_name)
        if self.datamodel.object(object_name).get("active", True) is False:
            raise OperationError(f"object {object_name} is deactivated")
        self._check_permission(object_name, "create")
        record_id = str(op.get("id") or new_record_id()).strip().lower()
        if not UUID_RE.fullmatch(record_id):
            raise OperationError("id must be a lowercase UUID")
        if self.current(object_name, record_id) is not None or (object_name, record_id) in self.working.destroyed:
            raise OperationError(f"{object_name} record {record_id} already exists")
        record = Record(object_name, record_id, {}, {}, record_relative_path(self.datamodel, object_name, record_id), "", False)
        record.data.update({
            "crm_id": record_id,
            "crm_object": object_name,
            "crm_title": "",
            "crm_created_at": op.get("created_at") and coerce_value("DATE_TIME", {}, "created_at", op["created_at"]) or self.now,
            "crm_updated_at": self.now,
            "crm_created_by": self.actor,
            "crm_updated_by": self.actor,
        })
        source = op.get("source") or ORIGIN_TO_SOURCE.get(self._op_origin.get("kind", ""), "MANUAL")
        if source not in CREATED_SOURCES:
            raise OperationError(f"unknown created source {source!r}")
        record.data["crm_created_source"] = source
        if op.get("position") is not None:
            record.data["crm_position"] = op["position"]
        self._journal_record((object_name, record_id))
        self._journal_member("created", (object_name, record_id))
        self.working.records[(object_name, record_id)] = record
        self.working.created.add((object_name, record_id))
        if self._index:
            self._reindex(record, {})
        if op.get("ref"):
            ref = str(op["ref"])
            if ref in self.working.refs:
                raise OperationError(f"reference {ref!r} is used twice")
            self._journal_ref(ref)
            self.working.refs[ref] = (object_name, record_id)
        changes: dict[str, Any] = {}
        values = dict(self._defaults(object_name))
        values.update(op.get("values") or {})
        self.apply_values(record, values, changes)
        self._name_from_file(record, changes)
        record.data["crm_title"] = record_title(self.datamodel, object_name, record.data)
        self.emit("create", record, changes)

    def _name_from_file(self, record: Record, changes: dict[str, Any]) -> None:
        """An attachment created without a name is titled after its first file, not after its id."""
        label = self.datamodel.label_field(record.object)
        fields = self.datamodel.fields(record.object)
        if record.data.get(label) or (fields.get(label) or {}).get("type") != "TEXT":
            return
        for name, definition in fields.items():
            if definition.get("type") == "FILES":
                items = files_items(record.data.get(name))
                if items and str(items[0].get("name") or "").strip():
                    self._set(record, label, str(items[0]["name"]).strip(), changes)
                    return

    def _defaults(self, object_name: str) -> dict[str, Any]:
        defaults: dict[str, Any] = {}
        for name, definition in self.datamodel.fields(object_name).items():
            if definition.get("default") is None or definition.get("active", True) is False:
                continue
            if definition["type"] in COMPOSITE_SUBFIELDS and isinstance(definition["default"], dict):
                for sub, value in definition["default"].items():
                    defaults[f"{name}.{sub}"] = value
            else:
                defaults[name] = definition["default"]
        return defaults

    def op_update(self, op: dict[str, Any], *, op_name: str = "update") -> Record:
        object_name = op.get("object")
        self.datamodel.object(object_name)
        record = self.touch(self.resolve_record(object_name, op.get("record")))
        if record.deleted and not op.get("restore_if_deleted"):
            raise OperationError(f"{object_name} record {record.id} is deleted; restore it first")
        changes: dict[str, Any] = {}
        if record.deleted and op.get("restore_if_deleted"):
            changes["crm_deleted_at"] = [record.data.pop("crm_deleted_at"), None]
        self.apply_values(record, op.get("values") or {}, changes)
        self._finish_update(record, changes)
        self.emit(op_name, record, changes)
        return record

    def _finish_update(self, record: Record, changes: dict[str, Any]) -> None:
        if not changes:
            return
        title = record_title(self.datamodel, record.object, record.data)
        if title != record.data.get("crm_title"):
            record.data["crm_title"] = title
            self._refresh_inbound_titles(record)
        record.data["crm_updated_at"] = self.now
        record.data["crm_updated_by"] = self.actor

    def _refresh_inbound_titles(self, record: Record) -> None:
        """Keep link labels readable after a rename; the link identity never changes."""
        new_link = record_link(self.datamodel, record.object, record.id, record.data["crm_title"])
        for object_name, field_name, definition in self.datamodel.relation_fields_targeting(record.object):
            for other in self.all_records(object_name):
                value = other.data.get(field_name)
                values = value if isinstance(value, list) else ([value] if value else [])
                if not any(parse_record_link(item) and parse_record_link(item)[1] == record.id for item in values if isinstance(item, str)):
                    continue
                touched = self.touch(other)
                if isinstance(value, list):
                    touched.data[field_name] = [new_link if parse_record_link(item) and parse_record_link(item)[1] == record.id else item for item in value]
                else:
                    touched.data[field_name] = new_link

    def op_upsert(self, op: dict[str, Any]) -> None:
        object_name = op.get("object")
        match = op.get("match")
        found = self.match_records(object_name, match, include_deleted=True) if match else []
        if len(found) > 1:
            raise OperationError(f"{len(found)} {object_name} records match {match}; the selection must be unique")
        if found:
            self.op_update({**op, "record": found[0].id, "restore_if_deleted": True}, op_name="upsert")
            if op.get("ref"):
                self._journal_ref(str(op["ref"]))
                self.working.refs[str(op["ref"])] = (object_name, found[0].id)
        else:
            values = dict(op.get("values") or {})
            for key, value in (match or {}).items():
                if key not in ("id", "crm_id"):
                    values.setdefault(key, value)
            create = {**op, "values": values}
            if match and ("id" in match or "crm_id" in match):
                create["id"] = match.get("id") or match.get("crm_id")
            self.op_create(create)

    def op_delete(self, op: dict[str, Any]) -> None:
        object_name = op.get("object")
        self._check_permission(object_name, "delete")
        record = self.touch(self.resolve_record(object_name, op.get("record")))
        if record.deleted:
            self.warnings.append(f"{object_name} {record.id} was already deleted")
            return
        record.data["crm_deleted_at"] = self.now
        record.data["crm_updated_at"] = self.now
        record.data["crm_updated_by"] = self.actor
        self.emit("delete", record, {"crm_deleted_at": [None, self.now]})

    def op_restore(self, op: dict[str, Any]) -> None:
        object_name = op.get("object")
        self._check_permission(object_name, "delete")
        record = self.touch(self.resolve_record(object_name, op.get("record")))
        if not record.deleted:
            self.warnings.append(f"{object_name} {record.id} was not deleted")
            return
        before = record.data.pop("crm_deleted_at")
        record.data["crm_updated_at"] = self.now
        record.data["crm_updated_by"] = self.actor
        self.emit("restore", record, {"crm_deleted_at": [before, None]})

    def op_destroy(self, op: dict[str, Any], *, op_name: str = "destroy", visited: Optional[set] = None) -> None:
        object_name = op.get("object")
        self._check_permission(object_name, "destroy")
        record = self.resolve_record(object_name, op.get("record"))
        identifiers = erasure_identifiers(record) if op_name == "erase" else set()
        names = erasure_names(record) if op_name == "erase" else set()
        self._destroy(record, op_name, visited or set())
        if op_name == "erase":
            # E-mail addresses and phone numbers identify the person, so they go from every record.
            # The name is not unique; it and the stored documents that mention the person go only on request.
            if op.get("redact_mentions"):
                self._redact_mentions(identifiers | names, files=True)
            else:
                self._redact_mentions(identifiers, files=False)

    def _redact_mentions(self, needles: set[str], *, files: bool) -> None:
        """Replace the erased person's identifying values in every other record inside the erasure, so the
        events and snapshots of this redaction are cleaned with it. With files, a stored text file that
        mentions the person is also detached from its records and removed with the erasure."""
        if not needles:
            return
        pattern = re.compile("|".join(re.escape(needle) for needle in sorted(needles, key=len, reverse=True)), re.IGNORECASE)
        for object_name in self.datamodel.objects:
            fields = self.datamodel.fields(object_name)
            for other in self.all_records(object_name):
                plan: dict[str, Any] = {}
                for key, value in other.data.items():
                    if key in SYSTEM_KEYS or key == "crm_title":
                        continue
                    base = key.split(".", 1)[0]
                    definition = fields.get(base) or {}
                    if definition.get("type") in {"RELATION", "MORPH_RELATION"}:
                        continue
                    if definition.get("type") == "FILES":
                        if not files:
                            continue
                        kept = []
                        for item in files_items(value):
                            ref = str(item.get("ref") or "")
                            if self._stored_text_mentions(ref, pattern) or pattern.search(str(item.get("name") or "")):
                                if ref.startswith(FILES_DIR + "/"):
                                    self.redacted_files.add(ref)
                                continue
                            kept.append(item)
                        if len(kept) != len(files_items(value)):
                            plan[key] = json.dumps(kept, ensure_ascii=False, sort_keys=True) if kept else None
                        continue
                    if isinstance(value, str) and pattern.search(value):
                        plan[key] = redact_text(value, needles)
                    elif isinstance(value, list) and any(isinstance(item, str) and pattern.search(item) for item in value):
                        plan[key] = [redact_text(item, needles) if isinstance(item, str) else item for item in value]
                texts = {name: text for name, text in other.richtext.items() if pattern.search(text)}
                if not plan and not texts:
                    continue
                touched = self.touch(other)
                changes: dict[str, Any] = {}
                for key, value in plan.items():
                    self._set(touched, key, value, changes)
                for name, text in texts.items():
                    redacted = redact_text(text, needles)
                    touched.richtext[name] = redacted
                    changes[f"richtext:{name}"] = {"before_sha256": "", "after_sha256": sha256_text(redacted), "after_chars": len(redacted)}
                self._finish_update(touched, changes)
                (self.redacted_ids if files else self.identifier_redacted_ids).add(touched.id)
                self.emit("update", touched, changes, {"caused_by": {"erasure": True}})

    def _stored_text_mentions(self, ref: str, pattern: "re.Pattern[str]") -> bool:
        if not ref.startswith(FILES_DIR + "/"):
            return False
        path = self.target / ref
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024 or not any(suffix in TEXT_SUFFIXES for suffix in path.suffixes):
            return False
        return bool(pattern.search(path.read_bytes().decode("utf-8", errors="replace")))

    def _destroy(self, record: Record, op_name: str, visited: set) -> None:
        key = (record.object, record.id)
        if key in visited:
            return
        visited.add(key)
        # Apply onDelete to every stored relation that points at this record.
        for object_name, field_name, definition in self.datamodel.relation_fields_targeting(record.object):
            on_delete = definition["relation"].get("onDelete", "SET_NULL")
            for other in self.all_records(object_name):
                if (other.object, other.id) == key or (other.object, other.id) in visited:
                    continue
                value = other.data.get(field_name)
                values = value if isinstance(value, list) else ([value] if value else [])
                hits = [item for item in values if isinstance(item, str) and parse_record_link(item) and parse_record_link(item)[1] == record.id]
                if not hits:
                    continue
                if on_delete == "RESTRICT":
                    raise OperationError(f"{other.object} {other.id} still references {record.object} {record.id} through {field_name} (onDelete RESTRICT)")
                if on_delete == "CASCADE" and not isinstance(value, list):
                    self._destroy(other, "destroy", visited)
                    continue
                touched = self.touch(other)
                changes: dict[str, Any] = {}
                remaining = [item for item in values if item not in hits]
                new_value: Any = remaining if isinstance(value, list) else None
                self._set(touched, field_name, new_value, changes)
                if changes:
                    touched.data["crm_updated_at"] = self.now
                    touched.data["crm_updated_by"] = self.actor
                    self.emit("cascade-null", touched, changes, {"caused_by": {"object": record.object, "record_id": record.id}})
        existing = self.current(*key)
        snapshot_changes = {k: [v, None] for k, v in (existing.data.items() if existing else []) if k in ("crm_title",)}
        if existing is not None:
            self._unindex(existing)
        self._journal_record(key)
        self._journal_member("created", key)
        self._journal_member("destroyed", key)
        self.working.records.pop(key, None)
        if key in self.working.created:
            self.working.created.discard(key)
        else:
            self.working.destroyed.add(key)
        self.emit(op_name, record, snapshot_changes)

    def op_merge(self, op: dict[str, Any]) -> None:
        object_name = op.get("object")
        self._check_permission(object_name, "destroy")
        into = self.touch(self.resolve_record(object_name, op.get("into"), include_deleted=False))
        sources = [self.resolve_record(object_name, item) for item in (op.get("from") or [])]
        if not sources:
            raise OperationError("merge needs at least one record in 'from'")
        if len(sources) > 8:
            raise OperationError("merge accepts at most nine records in total, as is usual in CRMs")
        if any(source.id == into.id for source in sources):
            raise OperationError("a record cannot be merged into itself")
        prefer = op.get("prefer") or {}
        changes: dict[str, Any] = {}
        # Fill each field from the preferred record, otherwise keep the target and fill gaps in order.
        for field_name, definition in self.datamodel.stored_fields(object_name):
            keys = frontmatter_keys(field_name, definition)
            chosen = prefer.get(field_name)
            if chosen:
                donor = into if chosen == into.id else next((s for s in sources if s.id == chosen), None)
                if donor is None:
                    raise OperationError(f"prefer.{field_name} names a record outside the merge")
                if definition["type"] == "RICH_TEXT":
                    pass
                else:
                    for key in keys:
                        self._set(into, key, donor.data.get(key), changes)
                continue
            if definition["type"] == "MORPH_RELATION" and definition["relation"].get("multiple", True):
                merged = list(into.data.get(field_name) or [])
                for source in sources:
                    for link in source.data.get(field_name) or []:
                        if link not in merged:
                            merged.append(link)
                self._set(into, field_name, merged, changes)
                continue
            if all(is_empty(into.data.get(key)) for key in keys):
                for source in sources:
                    if any(not is_empty(source.data.get(key)) for key in keys):
                        for key in keys:
                            self._set(into, key, source.data.get(key), changes)
                        break
        for field_name, definition in self.datamodel.fields(object_name).items():
            if definition["type"] != "RICH_TEXT":
                continue
            chosen = prefer.get(field_name)
            parts = []
            for record in ([into] + sources) if not chosen else [r for r in [into] + sources if r.id == chosen]:
                text = record.richtext.get(field_name, "").strip()
                if text and text not in parts:
                    parts.append(text)
            merged_text = "\n\n".join(parts)
            if merged_text.strip() != into.richtext.get(field_name, "").strip():
                before = into.richtext.get(field_name, "")
                into.richtext[field_name] = merged_text
                changes[f"richtext:{field_name}"] = {"before_sha256": sha256_text(before) if before else "", "after_sha256": sha256_text(merged_text), "after_chars": len(merged_text)}
        # Re-point every inbound relation from the merged records to the surviving one.
        new_link_title = record_title(self.datamodel, object_name, into.data)
        for other_object, field_name, definition in self.datamodel.relation_fields_targeting(object_name):
            for other in self.all_records(other_object):
                if other.id == into.id or any(other.id == source.id for source in sources):
                    continue
                value = other.data.get(field_name)
                values = value if isinstance(value, list) else ([value] if value else [])
                source_ids = {source.id for source in sources}
                if not any(isinstance(item, str) and parse_record_link(item) and parse_record_link(item)[1] in source_ids for item in values):
                    continue
                touched = self.touch(other)
                relink: dict[str, Any] = {}
                target_link = record_link(self.datamodel, object_name, into.id, new_link_title)
                if isinstance(value, list):
                    replaced = []
                    for item in value:
                        parsed = parse_record_link(item) if isinstance(item, str) else None
                        new_item = target_link if parsed and parsed[1] in source_ids else item
                        if new_item not in replaced:
                            replaced.append(new_item)
                    self._set(touched, field_name, replaced, relink)
                else:
                    self._set(touched, field_name, target_link, relink)
                if relink:
                    touched.data["crm_updated_at"] = self.now
                    touched.data["crm_updated_by"] = self.actor
                    self.emit("update", touched, relink, {"caused_by": {"op": "merge", "into": into.id}})
        self._finish_update(into, changes)
        self.emit("merge", into, changes, {"merged": [source.id for source in sources]})
        for source in sources:
            # The merged records are gone; their unique values now belong to the survivor.
            key = (source.object, source.id)
            self._unindex(source)
            self._journal_record(key)
            self._journal_member("destroyed", key)
            self.working.records.pop(key, None)
            self.working.destroyed.add(key)
            self.emit("destroy", source, {"crm_title": [source.data.get("crm_title"), None]}, {"caused_by": {"op": "merge", "into": into.id}})

    # -- planning ---------------------------------------------------------

    def run(self, operations: list[dict[str, Any]]) -> None:
        handlers = {
            "create": self.op_create,
            "update": self.op_update,
            "upsert": self.op_upsert,
            "delete": self.op_delete,
            "restore": self.op_restore,
            "destroy": self.op_destroy,
            "merge": self.op_merge,
            "erase": lambda op: self.op_destroy(op, op_name="erase"),
        }
        for index, op in enumerate(operations):
            name = op.get("op") if isinstance(op, dict) else None
            if name not in handlers:
                self.errors.append({"operation": index, "row": op.get("row") if isinstance(op, dict) else None, "error": f"unknown op {name!r}"})
                continue
            self._undo = {"records": {}, "sets": {}, "refs": {}}
            event_count = len(self.events)
            try:
                self._op_origin = self.origin
                if isinstance(op.get("origin"), dict):
                    self._op_origin = clean_origin({**self.origin, **op["origin"]})
                if op.get("row") is not None:
                    self._op_origin = {**self._op_origin, "row": op["row"]}
                handlers[name](op)
            except (OperationError, CrmError) as exc:
                self._rollback(event_count)
                self.errors.append({"operation": index, "row": op.get("row"), "object": op.get("object"), "error": str(exc)})
            finally:
                self._undo = None
                self._op_origin = self.origin

    def _rollback(self, event_count: int) -> None:
        undo = self._undo or {"records": {}, "sets": {}, "refs": {}}
        for key, previous in undo["records"].items():
            if previous is None:
                self.working.records.pop(key, None)
            else:
                self.working.records[key] = previous
        for (name, key), was_member in undo["sets"].items():
            members = getattr(self.working, name)
            if was_member:
                members.add(key)
            else:
                members.discard(key)
        for ref, previous in undo["refs"].items():
            if previous is _MISSING:
                self.working.refs.pop(ref, None)
            else:
                self.working.refs[ref] = previous
        del self.events[event_count:]
        self._index.clear()

    def credential_findings(self, record: Record) -> list[tuple[str, str]]:
        """(kind, value sha256) of credential-shaped values in a planned record that nobody acknowledged."""
        import secret_screen

        if self._acks is None:
            try:
                self._acks = {(entry.get("kind"), entry.get("value_sha256")) for entry in load_credential_acks(self.target) if isinstance(entry, dict)}
            except CrmError:
                self._acks = set()
        text = compose_record(self.datamodel, record)
        return sorted({(kind, digest) for _line, kind, digest in secret_screen.scan_text_detailed(text) if (kind, digest) not in self._acks})

    def validate_working(self) -> None:
        """Validate every touched record and every unique constraint it participates in."""
        checked: dict[str, bool] = {}
        for key, record in sorted(self.working.records.items()):
            problems = validate_record(self.datamodel, record, exists_check=self.exists)
            for kind, digest in self.credential_findings(record):
                # Caught before the value reaches the append-only event log, where a later correction could not remove it.
                problems.append(f"a value looks like a credential ({kind}); remove it, or, if it is a harmless look-alike, have a named person "
                                f"acknowledge it with crm_records.py acknowledge-credential --kind {kind} --value-sha256 {digest}")
            for name, definition in self.datamodel.fields(record.object).items():
                if definition.get("type") != "FILES":
                    continue
                for item in files_items(record.data.get(name)):
                    ref = str(item.get("ref") or "")
                    if not ref.startswith(FILES_DIR + "/") or ref in self.uploads or stored_file_problems(item):
                        continue
                    if ref not in checked:
                        path = self.target / ref
                        checked[ref] = path.is_file() and sha256_file(path) == item.get("sha256")
                    if not checked[ref]:
                        problems.append(f"{name}: the stored file {ref} is missing or damaged; store it again with an upload")
            for problem in problems:
                self.errors.append({"operation": None, "object": record.object, "record": record.id, "error": problem})
        touched_objects = {key[0] for key in self.working.records} | {key[0] for key in self.working.destroyed}
        for object_name in sorted(touched_objects):
            for field_name, definition in unique_fields(self.datamodel, object_name):
                seen: dict[str, str] = {}
                for record in self.all_records(object_name):
                    value = unique_key(field_name, definition, record.data)
                    if value is None:
                        continue
                    if value in seen and ((object_name, record.id) in self.working.records or (object_name, seen[value]) in self.working.records):
                        self.errors.append({
                            "operation": None, "object": object_name, "record": record.id,
                            "error": f"{field_name} {value!r} is already used by {object_name} {seen[value]} (deleted records count as well)",
                        })
                    else:
                        seen.setdefault(value, record.id)

    def file_changes(self) -> list[dict[str, Any]]:
        changes = []
        for key, record in sorted(self.working.records.items()):
            content = compose_record(self.datamodel, record)
            existing = self.store.get(*key)
            path = record_relative_path(self.datamodel, *key)
            before = (self.target / path).read_bytes() if (self.target / path).is_file() else b""
            if existing is not None and before.decode("utf-8") == content:
                continue
            changes.append({
                "path": path,
                "before_exists": bool(before) or (self.target / path).is_file(),
                "before_sha256": sha256_bytes(before) if (self.target / path).is_file() else "",
                "after": content,
                "after_sha256": sha256_text(content),
            })
        for key in sorted(self.working.destroyed):
            path = record_relative_path(self.datamodel, *key)
            if (self.target / path).is_file():
                changes.append({
                    "path": path,
                    "before_exists": True,
                    "before_sha256": sha256_bytes((self.target / path).read_bytes()),
                    "after": None,
                    "after_sha256": "",
                })
        return changes


_MISSING = object()
ORIGIN_KEYS = {"kind", "ref", "sha256", "row", "occasion", "run_id", "workflow", "note"}


def clean_origin(origin: Any) -> dict[str, Any]:
    """Validate an origin and keep only its known keys; refs must be portable."""
    if not isinstance(origin, dict) or origin.get("kind") not in ORIGIN_KINDS:
        raise CrmError(f"origin.kind must be one of {sorted(ORIGIN_KINDS)}")
    if not portable_reference(origin.get("ref")):
        raise CrmError("origin.ref must be a portable reference, never a local path")
    return {key: value for key, value in origin.items() if key in ORIGIN_KEYS and value not in (None, "")}


ORIGIN_TO_SOURCE = {
    "manual": "MANUAL", "import": "IMPORT", "email": "EMAIL", "calendar": "CALENDAR",
    "workflow": "WORKFLOW", "agent": "AGENT", "api": "API", "merge": "MERGE", "migration": "IMPORT", "restore": "MANUAL",
}


ERASED_LINK = re.compile(r"\[\[(records/[a-z0-9-]+/)([0-9a-f-]{36})(?:\|[^\]]*)?\]\]")


def _scrub_value(value: Any, erase_ids: set[str]) -> Any:
    if isinstance(value, str):
        return ERASED_LINK.sub(
            lambda match: f"[[{match.group(1)}{match.group(2)}|{ERASED}]]" if match.group(2) in erase_ids else match.group(0),
            value,
        )
    if isinstance(value, list):
        return [_scrub_value(item, erase_ids) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_value(item, erase_ids) for key, item in value.items()}
    return value


def scrub_event(event: dict[str, Any], erase_ids: set[str], identifiers: Iterable[str] = (),
                names: Iterable[str] = (), redacted_ids: Optional[set[str]] = None) -> bool:
    """Remove the content of an erased record from one event; return whether it changed.

    Events of other records lose the erased record's link labels and its e-mail addresses and phone
    numbers; events of records whose mentions were redacted in the erasure also lose the name.
    """
    before = json.dumps(event, sort_keys=True, ensure_ascii=False)
    if event.get("record_id") in erase_ids:
        event["changes"] = {key: ERASED for key in event.get("changes", {})}
        event["erased"] = True
    else:
        needles = set(identifiers)
        if redacted_ids and event.get("record_id") in redacted_ids:
            needles |= set(names)
        event["changes"] = redact_value(_scrub_value(event.get("changes", {}), erase_ids), needles)
        for key in ("caused_by",):
            if isinstance(event.get(key), dict):
                event[key] = redact_value(event[key], needles)
    return json.dumps(event, sort_keys=True, ensure_ascii=False) != before


def scrub_shard_text(text: str, erase_ids: set[str], identifiers: Iterable[str] = (), names: Iterable[str] = (),
                     redacted_ids: Optional[set[str]] = None) -> tuple[str, bool]:
    lines = []
    changed = False
    for line in text.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if scrub_event(event, erase_ids, identifiers, names, redacted_ids):
            changed = True
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        lines.append(line)
    return ("\n".join(lines) + "\n") if lines else "", changed


def erasure_identifiers(record: Record) -> set[str]:
    """E-mail addresses and phone numbers of an erased record. They identify the person wherever they
    appear, so an erasure removes them from every event, run and snapshot, whichever record holds them."""
    found: set[str] = set()
    for key, value in record.data.items():
        if key.endswith(("primaryEmail", "primaryPhoneNumber", "userEmail")) and isinstance(value, str):
            found.add(value.strip())
        if key.endswith("additionalEmails") and isinstance(value, list):
            found.update(str(item).strip() for item in value)
    return {needle for needle in found if len(needle) >= 4}


def erasure_names(record: Record) -> set[str]:
    """The record title. A name is not unique, so other records keep it unless the user chose to redact mentions."""
    title = str(record.data.get("crm_title") or "").strip()
    return {title} if len(title) >= 4 else set()


def erasure_needles(record: Record) -> set[str]:
    """Identifying values of an erased record that copies elsewhere may still contain."""
    return erasure_identifiers(record) | erasure_names(record)


def redact_text(text: str, needles: Iterable[str]) -> str:
    """Replace every occurrence of the needles (case-insensitive) with the erasure marker."""
    for needle in sorted(set(needles), key=len, reverse=True):
        if needle:
            text = re.sub(re.escape(needle), ERASED, text, flags=re.IGNORECASE)
    return text


def redact_value(value: Any, needles: Iterable[str]) -> Any:
    needles = set(needles)
    if not needles:
        return value
    if isinstance(value, str):
        return redact_text(value, needles)
    if isinstance(value, list):
        return [redact_value(item, needles) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item, needles) for key, item in value.items()}
    return value


def history_files_mentioning(target: Path, erase_ids: set[str], needles: Optional[set[str]] = None,
                             paths: Optional[set[str]] = None) -> list[str]:
    """Snapshot files under meta/history that still hold content of the erased records,
    including copies of the stored files and outbox drafts the erasure removes."""
    root = target / "meta/history"
    hits = []
    if not root.is_dir():
        return hits
    markers = set(erase_ids) | set(needles or set())
    paths = set(paths or set())
    # Record copies that link the erased record name the stored files they carried (an attachment
    # destroyed and cleaned up earlier); their copies in other snapshots go as well.
    for path in sorted(root.rglob("*.md")):
        if path.is_file() and path.parent.name != "_files":
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(record_id in text for record_id in erase_ids):
                paths.update(STORED_REF_IN_TEXT.findall(text))
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "snapshot.json":
            continue
        relative = path.relative_to(target).as_posix()
        parts = PurePosixPath(relative).parts
        inner = "/".join(parts[3:]) if len(parts) > 3 else ""
        if inner in paths:
            hits.append(relative)
            continue
        if path.suffix == ".md" and path.stem in erase_ids:
            hits.append(relative)
            continue
        if inner.startswith((FILES_DIR + "/", OUTBOX_DIR + "/")):
            if any(suffix in TEXT_SUFFIXES for suffix in path.suffixes) and path.stat().st_size <= 2 * 1024 * 1024:
                if any(marker in path.read_bytes().decode("utf-8", errors="replace") for marker in markers):
                    hits.append(relative)
            continue
        if path.suffix in {".jsonl", ".md", ".json"}:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(marker in text for marker in markers):
                hits.append(relative)
    return hits


def purge_history(target: Path, erase_ids: set[str], needles: Optional[set[str]] = None, paths: Optional[set[str]] = None,
                  identifiers: Optional[set[str]] = None, redacted_ids: Optional[set[str]] = None) -> list[str]:
    """Remove erased records from recovery snapshots after the user confirmed the erasure.

    History is otherwise never pruned. An erasure is the one confirmed exception,
    because a snapshot that keeps the data would undo the erasure it protects.
    """
    touched = []
    workflow_parts: set[str] = set()
    for relative in history_files_mentioning(target, erase_ids, needles, paths):
        path = target / relative
        parts = PurePosixPath(relative).parts
        inner = "/".join(parts[3:]) if len(parts) > 3 else ""
        if inner.startswith((FILES_DIR + "/", OUTBOX_DIR + "/")):
            # Copies of stored files and drafts cannot be cleaned line by line; they go entirely.
            path.unlink()
            touched.append(relative)
            continue
        if inner.startswith("meta/crm-runs/") or inner == "meta/crm-workflow-state.json":
            # Workflow copies have their own format; crm_workflows knows how to empty them.
            workflow_parts.add(parts[2])
            continue
        names = set(needles or set()) - set(identifiers or set())
        if path.suffix == ".md" and path.stem in erase_ids:
            path.unlink()
            touched.append(relative)
        elif path.suffix == ".jsonl":
            new_text, changed = scrub_shard_text(path.read_text(encoding="utf-8"), erase_ids, identifiers or set(), names, redacted_ids)
            if changed:
                atomic_write(path, new_text.encode("utf-8"))
                touched.append(relative)
        else:
            text = path.read_text(encoding="utf-8")
            new_text = redact_text(_scrub_value(text, erase_ids), identifiers or set())
            if redacted_ids and path.stem in redacted_ids:
                new_text = redact_text(new_text, names)
            if new_text != text:
                atomic_write(path, new_text.encode("utf-8"))
                touched.append(relative)
    if workflow_parts:
        try:
            import crm_workflows

            for snapshot_id in sorted(workflow_parts):
                snapshot_root = target / "meta/history" / snapshot_id
                for rewrite in crm_workflows.erasure_rewrites(snapshot_root, set(erase_ids), set(needles or set())):
                    atomic_write(snapshot_root / rewrite["path"], rewrite["after"].encode("utf-8"))
                    touched.append(f"meta/history/{snapshot_id}/{rewrite['path']}")
        except ImportError:
            pass
    # Keep every touched snapshot manifest truthful about what it now holds.
    snapshots = sorted({PurePosixPath(item).parts[2] for item in touched if len(PurePosixPath(item).parts) > 3})
    for snapshot_id in snapshots:
        manifest_path = target / "meta/history" / snapshot_id / "snapshot.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = []
        for entry in manifest.get("files", []):
            file_path = target / "meta/history" / snapshot_id / entry["path"]
            if not file_path.is_file():
                continue
            entry["bytes"] = file_path.stat().st_size
            entry["sha256"] = sha256_bytes(file_path.read_bytes())
            entries.append(entry)
        manifest["files"] = entries
        manifest["erased_records"] = sorted(set(manifest.get("erased_records", [])) | set(erase_ids))
        atomic_write(manifest_path, (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return touched


def history_mentions(target: Path, needles: Iterable[str]) -> list[str]:
    """Snapshot text files that still contain one of the needles after an erasure (names left in other
    records the user chose not to redact), so the result can say so instead of claiming a clean history."""
    root = target / "meta/history"
    lowered = [needle.lower() for needle in needles if needle]
    found = []
    for path in sorted(root.rglob("*")) if root.is_dir() and lowered else []:
        if not path.is_file() or path.name == "snapshot.json" or path.stat().st_size > 4 * 1024 * 1024:
            continue
        if path.suffix not in {".md", ".json", ".jsonl", ".eml", ".ics", ".txt", ".csv"}:
            continue
        text = path.read_bytes().decode("utf-8", errors="replace").lower()
        if any(needle in text for needle in lowered):
            found.append(path.relative_to(target).as_posix())
    return found


def permission_allows(roles: dict[str, Any], actor: str, object_name: str, action: str, field_name: Optional[str] = None) -> tuple[bool, str]:
    """Cooperative role check; the wiki files themselves cannot enforce access."""
    role_name = role_for_actor(roles, actor)
    if role_name is None:
        return True, ""
    role = roles.get("roles", {}).get(role_name, {})
    objects = role.get("objects", {})
    rules = objects.get(object_name, objects.get("*", {}))
    flags = {"create": "update", "update": "update", "delete": "delete", "destroy": "destroy", "read": "read"}
    flag = flags.get(action, action)
    if rules.get(flag, False) is not True:
        return False, f"role {role_name} of {actor} may not {action} {object_name}"
    if field_name:
        field_rules = rules.get("fields", {}).get(field_name, {})
        if action in {"update", "create"} and field_rules.get("update") is False:
            return False, f"role {role_name} of {actor} may not change {object_name}.{field_name}"
    return True, ""


def role_for_actor(roles: Optional[dict[str, Any]], actor: str) -> Optional[str]:
    if not roles or not isinstance(roles.get("roles"), dict):
        return None
    assignments = roles.get("assignments", {}) or {}
    if actor in assignments:
        return assignments[actor]
    for pattern, role in assignments.items():
        if pattern.endswith("*") and actor.startswith(pattern[:-1]):
            return role
    return roles.get("default_role")


def plan_artifacts(target: Path, requested: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Outbox files to write and stored or outbox files to remove, with the state they must still have."""
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(requested):
        path = str(item.get("path") or "") if isinstance(item, dict) else ""
        problem = None
        if not isinstance(item, dict) or not path:
            problem = "an artifact needs a path"
        elif path in seen:
            problem = f"{path} is named twice"
        elif item.get("delete") is True:
            file = target / path
            if not path.startswith((FILES_DIR + "/", OUTBOX_DIR + "/")) or ".." in PurePosixPath(path).parts:
                problem = f"only stored files and outbox files can be removed here, not {path}"
            elif not file.is_file():
                problem = f"{path} does not exist"
            else:
                entries.append({"path": path, "before_exists": True, "before_sha256": sha256_file(file), "after": None, "after_sha256": ""})
        else:
            content = item.get("content")
            file = target / path
            if not OUTBOX_PATH_RE.fullmatch(path):
                problem = f"{path}: outbox files belong in {OUTBOX_DIR}/<folder>/<name>.eml|.ics|.json|.txt"
            elif not isinstance(content, str):
                problem = f"{path}: content must be text"
            else:
                findings = record_credential_findings(target, path, content)
                if findings:
                    problem = f"{path}: credential-like values ({', '.join(sorted({kind for _line, kind in findings}))}); remove them first"
                elif file.is_file():
                    if sha256_file(file) != sha256_text(content):
                        problem = f"{path} already exists with other content"
                else:
                    entries.append({"path": path, "before_exists": False, "before_sha256": "", "after": content, "after_sha256": sha256_text(content)})
        if problem:
            errors.append({"operation": None, "artifact": index, "error": problem})
        seen.add(path)
    return entries, errors


def erasure_artifacts(target: Path, planner: "Planner", datamodel: DataModel, erase_ids: set[str], needles: set[str],
                      artifacts: list[dict[str, Any]]) -> list[str]:
    """Remove what an erasure must reach beyond the records: stored files only the removed records
    referred to, and outbox drafts that mention the erased person. Text files other records still
    refer to are reported, since they belong to those records."""
    removed: list[str] = []
    destroyed = [planner.store.get(*key) for key in planner.working.destroyed]
    before = stored_record_refs([record for record in destroyed if record is not None], datamodel)
    for ref in planner.redacted_files:
        before.setdefault(ref, set())
    remaining = stored_record_refs([record for name in datamodel.objects for record in planner.all_records(name)], datamodel)
    planned = {entry["path"] for entry in artifacts}
    markers = set(needles) | set(erase_ids)
    for ref in sorted(set(before) - set(remaining)):
        if (target / ref).is_file() and ref not in planned:
            artifacts.append({"path": ref, "before_exists": True, "before_sha256": sha256_file(target / ref), "after": None, "after_sha256": ""})
            removed.append(ref)
    for ref in sorted(remaining):
        path = target / ref
        if path.is_file() and any(suffix in TEXT_SUFFIXES for suffix in PurePosixPath(ref).suffixes) and path.stat().st_size <= 2 * 1024 * 1024:
            text = path.read_bytes().decode("utf-8", errors="replace")
            if any(marker in text for marker in markers):
                planner.warnings.append(f"the stored file {ref} mentions the erased record and is still attached to other records; review it by hand")
    root = target / OUTBOX_DIR
    for path in sorted(root.rglob("*")) if root.is_dir() else []:
        relative = path.relative_to(target).as_posix()
        if not path.is_file() or relative in planned or relative in removed:
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        if any(marker in text for marker in markers):
            artifacts.append({"path": relative, "before_exists": True, "before_sha256": sha256_file(path), "after": None, "after_sha256": ""})
            removed.append(relative)
    return removed


def plan_transaction(target: Path, request: dict[str, Any], *, now: Optional[str] = None, allow_uploads: bool = False) -> dict[str, Any]:
    datamodel = load_datamodel(target)
    actor = request.get("actor")
    if not valid_actor(actor):
        raise CrmError("request.actor must be human:<id>, agent/<name> or process:<id>")
    request_origin = clean_origin(request.get("origin") or {"kind": "manual"})
    operations = request.get("operations")
    artifact_requests = request.get("artifacts") or []
    if not isinstance(artifact_requests, list):
        raise CrmError("request.artifacts must be a list of {path, content} or {path, delete: true}")
    if not isinstance(operations, list) or (not operations and not artifact_requests):
        raise CrmError("request.operations must be a non-empty list (it may be empty only when the request writes or removes artifacts)")
    blocking = request.get("blocking_errors") or []
    if not isinstance(blocking, list) or any(not isinstance(item, dict) or not item.get("error") for item in blocking):
        raise CrmError("request.blocking_errors must be a list of {error, ...} objects")
    roles = load_json_file(target, ROLES_PATH, None)
    planner = Planner(target, datamodel, actor, request_origin, now, roles if isinstance(roles, dict) and roles.get("enforcement") == "cooperative" else None,
                      allow_uploads=allow_uploads)
    planner.run(operations)
    planner.validate_working()
    for problem in planner.store.load_errors:
        planner.warnings.append(f"unreadable record skipped: {problem}")
    files = planner.file_changes()
    events = planner.events
    erase_ids = {event["record_id"] for event in events if event["op"] == "erase"}
    artifacts, artifact_errors = plan_artifacts(target, artifact_requests)
    planner.errors.extend(artifact_errors)
    erase_paths: list[str] = []
    shard_appends: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        shard_appends.setdefault(event_shard(event["at"]), []).append(event)
    shards = []
    for shard, appended in sorted(shard_appends.items()):
        path = target / shard
        shards.append({
            "path": shard,
            "before_exists": path.is_file(),
            "before_sha256": sha256_bytes(path.read_bytes()) if path.is_file() else "",
            "append": appended,
        })
    rewrites = []
    history_hits: list[str] = []
    erase_needles: set[str] = set()
    erase_identifiers: set[str] = set()
    erase_names: set[str] = set()
    if erase_ids:
        for record_key in [key for key in planner.working.destroyed if key[1] in erase_ids]:
            erased = planner.store.get(*record_key)
            if erased is not None:
                erase_identifiers.update(erasure_identifiers(erased))
                erase_names.update(erasure_names(erased))
        erase_needles = erase_identifiers | erase_names
        redacted = set(planner.redacted_ids)
        for event in events:
            scrub_event(event, erase_ids, erase_identifiers, erase_names, redacted)
        root = target / EVENTS_DIR
        for shard_path in sorted(root.glob("*.jsonl")) if root.is_dir() else []:
            text = shard_path.read_text(encoding="utf-8")
            new_text, changed = scrub_shard_text(text, erase_ids, erase_identifiers, erase_names, redacted)
            if changed:
                rewrites.append({
                    "path": shard_path.relative_to(target).as_posix(),
                    "before_sha256": sha256_bytes(shard_path.read_bytes()),
                    "after": new_text,
                })
        lowered = [needle.lower() for needle in erase_needles]
        for object_name in datamodel.objects:
            for other in planner.all_records(object_name):
                if other.id in redacted:
                    continue
                blob = json.dumps(other.data, ensure_ascii=False) + "\n".join(other.richtext.values())
                blob = re.sub(r"\[\[records/[^\]]+\]\]", "", blob).lower()
                if any(needle in blob for needle in lowered):
                    planner.warnings.append(
                        f"{other.object} {other.id} still mentions the erased record; plan the erase again with "
                        "\"redact_mentions\": true to remove such mentions inside the erasure, or leave them after the user decided"
                    )
        if erase_needles:
            for folder in ("wiki", "sources"):
                root = target / folder
                for page in sorted(root.rglob("*.md")) if root.is_dir() else []:
                    try:
                        text = page.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        continue
                    if any(needle in text for needle in erase_needles) or any(record_id in text for record_id in erase_ids):
                        kind = "knowledge page" if folder == "wiki" else "registered source"
                        planner.warnings.append(
                            f"{kind} {page.relative_to(target).as_posix()} mentions the erased record; it is not changed by this "
                            "transaction. Revise the page through a page batch or withdraw the source after the user decides"
                        )
        try:
            import crm_workflows

            # Workflow runs and waiting runs may hold copies of the erased data.
            rewrites.extend(crm_workflows.erasure_rewrites(target, set(erase_ids), erase_needles))
        except ImportError:
            pass
        erase_paths = erasure_artifacts(target, planner, datamodel, erase_ids, erase_needles, artifacts)
        history_hits = history_files_mentioning(target, erase_ids, erase_needles, set(erase_paths))
    ops_used = {op.get("op") for op in operations if isinstance(op, dict)}
    summary: dict[str, int] = {}
    for event in events:
        summary[event["op"]] = summary.get(event["op"], 0) + 1
    removed_artifacts = [entry["path"] for entry in artifacts if entry["after"] is None]
    payload = {
        "format": PLAN_FORMAT,
        "datamodel_sha256": datamodel.sha256,
        "actor": actor,
        "origin": request_origin,
        "transaction": planner.txn,
        "created_at": planner.now,
        "files": files,
        "event_shards": shards,
        "event_rewrites": rewrites,
        "summary": summary,
        "stored_files": planner.stored_files(),
        "artifacts": artifacts,
        "destructive": bool(ops_used & DESTRUCTIVE_OPERATIONS) or any(event["op"] in DESTRUCTIVE_OPERATIONS for event in events) or bool(removed_artifacts),
        "erases": sorted(erase_ids),
        "erase_needles": sorted(erase_needles),
        "erase_identifiers": sorted(erase_identifiers),
        "erase_redacted": sorted(planner.redacted_ids),
        "erase_paths": sorted(erase_paths),
        "history_purge": history_hits,
        "errors": blocking + planner.errors,
        "warnings": planner.warnings,
    }
    if isinstance(request.get("annotations"), dict):
        # Helper-specific report sections (import row report, ingest summary) are part of what the user confirms.
        payload["annotations"] = request["annotations"]
    return {**payload, "plan_sha256": sha256_bytes(canonical_json(payload))}


class StalePlanError(CrmError):
    """The plan does not match the confirmed hash; every helper answers with state stale_plan and exit 3."""


def verify_plan(plan: Any, expected: str) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("format") != PLAN_FORMAT:
        raise CrmError("unsupported CRM transaction plan")
    payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
    actual = sha256_bytes(canonical_json(payload))
    if not expected or plan.get("plan_sha256") != expected or actual != expected:
        raise StalePlanError("CRM transaction plan is stale or was modified")
    return plan


def atomic_write(path: Path, content: bytes) -> None:
    import portable_io

    portable_io.atomic_write_bytes(path, content)


def prune_empty_dirs(directory: Path, target: Path) -> None:
    """Remove directories an artifact removal left empty, up to records/_files or records/_outbox."""
    stops = {target / FILES_DIR, target / OUTBOX_DIR, target / RECORDS_DIR, target}
    while directory not in stops and target in directory.parents:
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


def apply_transaction(target: Path, token: str, plan: dict[str, Any], *, confirm_destructive: bool = False) -> tuple[dict[str, Any], int]:
    from snapshot_wiki import create_snapshot
    from wiki_lock import require_lock

    require_lock(target, token)
    if plan.get("errors"):
        return {"state": "invalid_plan", "writes": 0, "reason": "the plan contains errors; fix or drop the failing operations and plan again", "errors": plan["errors"][:50]}, 4
    if plan.get("destructive") and not confirm_destructive:
        return {"state": "confirmation_required", "writes": 0, "reason": "destroy, merge or erase needs explicit user confirmation (--confirm-destructive)"}, 3
    datamodel_path = target / DATAMODEL_PATH
    if not datamodel_path.is_file() or sha256_bytes(datamodel_path.read_bytes()) != plan.get("datamodel_sha256"):
        return {"state": "stale_plan", "writes": 0, "reason": "the data model changed after planning"}, 3
    for entry in plan.get("files", []):
        path = target / entry["path"]
        exists = path.is_file()
        if exists != bool(entry["before_exists"]):
            return {"state": "stale_plan", "writes": 0, "reason": f"existence changed: {entry['path']}"}, 3
        if exists and sha256_bytes(path.read_bytes()) != entry["before_sha256"]:
            return {"state": "stale_plan", "writes": 0, "reason": f"record changed after planning: {entry['path']}"}, 3
        if entry["after"] is not None and sha256_text(entry["after"]) != entry["after_sha256"]:
            return {"state": "stale_plan", "writes": 0, "reason": f"planned content was modified: {entry['path']}"}, 3
    for shard in plan.get("event_shards", []):
        path = target / shard["path"]
        if path.is_file() != bool(shard["before_exists"]) or (path.is_file() and sha256_bytes(path.read_bytes()) != shard["before_sha256"]):
            return {"state": "stale_plan", "writes": 0, "reason": f"event log changed after planning: {shard['path']}"}, 3
    for rewrite in plan.get("event_rewrites", []):
        path = target / rewrite["path"]
        if not path.is_file() or sha256_bytes(path.read_bytes()) != rewrite["before_sha256"]:
            return {"state": "stale_plan", "writes": 0, "reason": f"event log changed after planning: {rewrite['path']}"}, 3
    copies: list[dict[str, Any]] = []
    for entry in plan.get("stored_files", []):
        destination = target / entry["path"]
        if destination.is_file():
            if sha256_file(destination) != entry["sha256"]:
                return {"state": "stale_plan", "writes": 0, "reason": f"the file store holds other content at {entry['path']}"}, 3
            continue
        source = Path(entry["source"])
        if not source.is_file() or source.stat().st_size != entry["size"] or sha256_file(source) != entry["sha256"]:
            return {"state": "stale_plan", "writes": 0, "reason": f"the file to store changed or vanished after planning: {source.name}"}, 3
        copies.append(entry)
    for entry in plan.get("artifacts", []):
        path = target / entry["path"]
        exists = path.is_file()
        if exists != bool(entry["before_exists"]) or (exists and sha256_file(path) != entry["before_sha256"]):
            return {"state": "stale_plan", "writes": 0, "reason": f"{entry['path']} changed after planning"}, 3
        if entry["after"] is not None and sha256_text(entry["after"]) != entry["after_sha256"]:
            return {"state": "stale_plan", "writes": 0, "reason": f"planned content was modified: {entry['path']}"}, 3
    erase_paths = set(plan.get("erase_paths") or [])
    existing = [entry["path"] for entry in plan.get("files", []) if entry["before_exists"]]
    # Event shards only grow: instead of copying a whole monthly shard into every snapshot, the apply
    # notes its length so a failed write can be cut back. Rewritten shards (an erasure) are copied.
    shard_lengths = [{"path": shard["path"], "bytes": (target / shard["path"]).stat().st_size if (target / shard["path"]).is_file() else 0}
                     for shard in plan.get("event_shards", [])]
    existing += [rewrite["path"] for rewrite in plan.get("event_rewrites", []) if rewrite["path"] not in existing]
    # Removed artifacts are kept in the snapshot so they can be restored, except what an erasure removes.
    existing += [entry["path"] for entry in plan.get("artifacts", []) if entry["before_exists"] and entry["path"] not in erase_paths]
    snapshot = create_snapshot(target, token, operation=f"crm-transaction:{plan.get('transaction', '')}", selected_files=sorted(set(existing))) if existing else None
    written: list[str] = []
    try:
        for entry in copies:
            data = Path(entry["source"]).read_bytes()
            if sha256_bytes(data) != entry["sha256"]:
                raise OSError(f"the file to store changed while it was copied: {Path(entry['source']).name}")
            destination = target / entry["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(destination, data)
            written.append(entry["path"])
        rewritten = {}
        for rewrite in plan.get("event_rewrites", []):
            rewritten[rewrite["path"]] = rewrite["after"]
        for shard in plan.get("event_shards", []):
            path = target / shard["path"]
            base = rewritten.pop(shard["path"], None)
            if base is None:
                base = path.read_text(encoding="utf-8") if path.is_file() else ""
            if base and not base.endswith("\n"):
                base += "\n"
            lines = "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in shard["append"])
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, (base + lines).encode("utf-8"))
            written.append(shard["path"])
        for relative, content in rewritten.items():
            atomic_write(target / relative, content.encode("utf-8"))
            written.append(relative)
        for entry in plan.get("files", []):
            path = target / entry["path"]
            if entry["after"] is None:
                if path.is_file():
                    path.unlink()
                written.append(entry["path"])
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, entry["after"].encode("utf-8"))
            written.append(entry["path"])
        for entry in plan.get("artifacts", []):
            path = target / entry["path"]
            if entry["after"] is None:
                if path.is_file():
                    path.unlink()
                prune_empty_dirs(path.parent, target)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(path, entry["after"].encode("utf-8"))
            written.append(entry["path"])
    except OSError as exc:
        return {
            "state": "partial_failure",
            "reason": f"writing failed after validation: {exc}",
            "written": written,
            "snapshot": snapshot,
            "event_shards_before": shard_lengths,
            "recovery_required": True,
        }, 5
    purged: list[str] = []
    remaining: list[str] = []
    if plan.get("erases"):
        if snapshot and snapshot.get("snapshot"):
            # The erasure's own safety copy holds exactly the data it removes; once applied it goes too.
            import shutil

            shutil.rmtree(target / snapshot["snapshot"], ignore_errors=True)
            snapshot = {**snapshot, "removed_after_erasure": True}
        purged = purge_history(target, set(plan["erases"]), set(plan.get("erase_needles") or []), erase_paths,
                               set(plan.get("erase_identifiers") or []), set(plan.get("erase_redacted") or []))
        remaining = history_mentions(target, set(plan.get("erase_needles") or []))
    return {
        "history_purged": purged,
        "history_still_mentions": remaining,
        "state": "applied",
        "writes": len(written),
        "records_written": sum(1 for entry in plan.get("files", []) if entry["after"] is not None),
        "records_removed": sum(1 for entry in plan.get("files", []) if entry["after"] is None),
        "files_stored": [entry["path"] for entry in copies],
        "artifacts_written": [entry["path"] for entry in plan.get("artifacts", []) if entry["after"] is not None],
        "artifacts_removed": [entry["path"] for entry in plan.get("artifacts", []) if entry["after"] is None],
        "events": sum(len(shard["append"]) for shard in plan.get("event_shards", [])),
        "summary": plan.get("summary", {}),
        "snapshot": snapshot,
        "plan_sha256": plan.get("plan_sha256"),
        "transaction": plan.get("transaction"),
        "views_stale": True,
        "next_step": "rebuild CRM views (crm_build.py) and lint before the next release",
    }, 0


# ---------------------------------------------------------------------------
# credential screening of records

CREDENTIAL_ACKS_PATH = "schema/crm/credential-acknowledgements.json"
CREDENTIAL_ACKS_FORMAT = "lmwiki-crm-credential-acks/1"


def load_credential_acks(target: Path) -> list[dict[str, Any]]:
    raw = load_json_file(target, CREDENTIAL_ACKS_PATH, {"format": CREDENTIAL_ACKS_FORMAT, "entries": []})
    if not isinstance(raw, dict) or raw.get("format") != CREDENTIAL_ACKS_FORMAT or not isinstance(raw.get("entries"), list):
        raise CrmError(f"{CREDENTIAL_ACKS_PATH}: format must be {CREDENTIAL_ACKS_FORMAT} with an entries list")
    return raw["entries"]


def record_credential_findings(target: Path, relative: str, text: str, acks: Optional[list[dict[str, Any]]] = None) -> list[tuple[int, str]]:
    """Credential-shaped values in CRM data that no named person acknowledged as harmless.

    An acknowledgement covers one kind and the digest of one exact value, wherever
    that value appears in CRM data (the record, links to it, its events); any other
    or changed value is screened again. Knowledge pages and sources never use it.
    """
    import secret_screen

    acks = load_credential_acks(target) if acks is None else acks
    accepted = {(entry.get("kind"), entry.get("value_sha256")) for entry in acks if isinstance(entry, dict)}
    findings = []
    for number, kind, digest in secret_screen.scan_text_detailed(text):
        if (kind, digest) not in accepted:
            findings.append((number, kind))
    return sorted(set(findings))


CRM_DATA_PREFIXES = ("records/", "meta/crm-events/", "meta/crm-runs/")


# ---------------------------------------------------------------------------
# whole-store validation used by lint


def validate_artifacts(target: Path, datamodel: DataModel, records: list[Record]) -> tuple[list[str], list[str], dict[str, Any]]:
    """Stored files and outbox drafts: names, integrity, references, and the credential screen."""
    errors: list[str] = []
    warnings: list[str] = []
    stats = {"stored_files": 0, "stored_bytes": 0, "outbox_files": 0}
    references = stored_record_refs(records, datamodel)
    stored: set[str] = set()
    try:
        acks = load_credential_acks(target)
    except CrmError:
        acks = []
    for relative in artifact_files(target):
        path = target / relative
        if relative.startswith(FILES_DIR + "/"):
            match = STORED_FILE_RE.fullmatch(relative)
            if not match or match.group(1) != match.group(2)[:2]:
                errors.append(f"{relative}: only files named by their sha256 belong in {FILES_DIR}/ (store files through a FILES upload)")
                continue
            if sha256_file(path) != match.group(2):
                errors.append(f"{relative}: the stored file is damaged (its content no longer matches its sha256 name)")
            stored.add(relative)
            stats["stored_files"] += 1
            stats["stored_bytes"] += path.stat().st_size
            if relative not in references:
                warnings.append(f"{relative}: no record refers to this stored file; remove it with crm_records.py cleanup-files")
        else:
            if not OUTBOX_PATH_RE.fullmatch(relative):
                errors.append(f"{relative}: outbox files belong in {OUTBOX_DIR}/<folder>/<name>.eml|.ics|.json|.txt")
                continue
            stats["outbox_files"] += 1
        if path.suffix in {".md", ".json", ".jsonl"}:
            continue  # lint screens these suffixes in CRM data already
        if any(suffix in TEXT_SUFFIXES for suffix in path.suffixes) and path.stat().st_size <= 2 * 1024 * 1024:
            text = path.read_bytes().decode("utf-8", errors="replace")
            for number, kind in record_credential_findings(target, relative, text, acks):
                errors.append(f"{relative}:{number}: credential-like value ({kind}); remove it or acknowledge a harmless look-alike")
    for ref, record_ids in sorted(references.items()):
        if ref not in stored:
            errors.append(f"{ref}: refers to a stored file that is missing (records {', '.join(sorted(record_ids)[:3])})")
    return errors, warnings, stats


def validate_store(target: Path) -> tuple[list[str], list[str], dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    stats: dict[str, Any] = {"objects": 0, "records": 0, "deleted_records": 0, "events": 0}
    if not (target / DATAMODEL_PATH).is_file():
        if (target / RECORDS_DIR).exists():
            errors.append(f"{RECORDS_DIR}/ exists but {DATAMODEL_PATH} is missing")
        return errors, warnings, stats
    try:
        datamodel = load_datamodel(target)
    except CrmError as exc:
        return [str(exc)], warnings, stats
    stats["objects"] = len(datamodel.objects)
    root = target / RECORDS_DIR
    index: dict[tuple[str, str], str] = {}
    records: list[Record] = []
    seen_casefold: dict[str, str] = {}
    if root.is_dir():
        for entry in sorted(root.rglob("*")):
            relative = entry.relative_to(target).as_posix()
            parts = entry.relative_to(root).parts
            if entry.is_symlink():
                errors.append(f"{relative}: symbolic links are not allowed in records/")
                continue
            if parts[0] in ARTIFACT_DIRS:
                continue
            if entry.is_dir():
                if len(parts) > 1:
                    errors.append(f"{relative}: records/<object>/ must be flat")
                elif datamodel.object_for_directory(entry.name) is None:
                    errors.append(f"{relative}: no object of the data model uses this directory")
                continue
            if len(parts) != 2:
                errors.append(f"{relative}: record files belong in records/<object>/<id>.md")
                continue
            if entry.suffix != ".md":
                if entry.name in {".DS_Store", "Thumbs.db", "desktop.ini"}:
                    continue
                errors.append(f"{relative}: only record Markdown files belong in records/")
                continue
            folded = relative.casefold()
            if folded in seen_casefold:
                errors.append(f"{relative}: differs only in case from {seen_casefold[folded]}")
            seen_casefold[folded] = relative
            object_name = datamodel.object_for_directory(parts[0])
            if object_name is None:
                continue
            try:
                record = load_record(target, datamodel, object_name, entry)
            except (CrmError, UnicodeDecodeError) as exc:
                errors.append(f"{relative}: {exc}")
                continue
            records.append(record)
            index[(parts[0], record.id)] = object_name
    stats["records"] = len(records)
    stats["deleted_records"] = sum(1 for record in records if record.deleted)
    by_object: dict[str, int] = {}

    def exists(object_dir: str, record_id: str) -> Optional[str]:
        return index.get((object_dir, record_id))

    unique_seen: dict[tuple[str, str], dict[str, str]] = {}
    for record in records:
        by_object[record.object] = by_object.get(record.object, 0) + 1
        errors.extend(validate_record(datamodel, record, exists_check=exists))
        expected_title = record_title(datamodel, record.object, record.data)
        if record.data.get("crm_title") != expected_title:
            warnings.append(f"{record.path}: crm_title is out of date ({record.data.get('crm_title')!r} instead of {expected_title!r})")
        for field_name, definition in unique_fields(datamodel, record.object):
            value = unique_key(field_name, definition, record.data)
            if value is None:
                continue
            seen = unique_seen.setdefault((record.object, field_name), {})
            if value in seen:
                errors.append(f"{record.path}: {field_name} {value!r} is also used by {record.object} {seen[value]} (unique, deleted records included)")
            else:
                seen[value] = record.id
    stats["records_by_object"] = by_object
    artifact_errors, artifact_warnings, artifact_stats = validate_artifacts(target, datamodel, records)
    errors.extend(artifact_errors)
    warnings.extend(artifact_warnings)
    stats.update(artifact_stats)
    try:
        event_ids: set[str] = set()
        count = 0
        for event in read_events(target):
            count += 1
            problems = validate_event(event, datamodel)
            errors.extend(problems)
            if event.get("event_id") in event_ids:
                errors.append(f"{event['_shard']}:{event['_line']}: duplicate event_id {event.get('event_id')}")
            event_ids.add(str(event.get("event_id")))
        stats["events"] = count
    except CrmError as exc:
        errors.append(str(exc))
    events_root = target / EVENTS_DIR
    if events_root.is_dir():
        for entry in events_root.iterdir():
            if entry.name in {".DS_Store", "Thumbs.db", "desktop.ini"}:
                continue
            if not re.fullmatch(r"\d{4}-\d{2}\.jsonl", entry.name):
                errors.append(f"{entry.relative_to(target).as_posix()}: only monthly YYYY-MM.jsonl shards belong in {EVENTS_DIR}/")
    return errors, warnings, stats
