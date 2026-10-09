#!/usr/bin/env python3
"""Answer read-only questions about the CRM records bundled with this frozen skill.

The helper verifies the bundled release before and after reading, exactly like
verify_knowledge.py, never writes, and has no option to select another wiki.

  find       --object O [--filter JSON] [--sort JSON] [--fields a,b] [--limit N]
             [--offset N] [--include-deleted]
  aggregate  --object O --function F [--field X] [--group-by X] [--granularity G]
             [--filter JSON] [--include-deleted]
  get        --object O --id UUID
  search     --text T [--objects a,b] [--limit N] [--include-deleted]
  timeline   [--object O] [--id UUID] [--since ISO] [--limit N]
  options of every command: [--now ISO] [--me WORKSPACE-MEMBER-UUID]

Filters, sorting, grouping and aggregates are those of crm_filters.py, so a
filter means here exactly what it means in views and dashboards. Grouping by a
select field lists every option in option order, also options without records
(counts and sums 0, other functions without value), like the columns of a
kanban board. Every answer names generated_at, the moment relative dates were
evaluated at, and the time zone of the CRM settings. Values come twice: as
display text formatted by the CRM settings and as the raw stored value.

Exit codes: 0 answered, 2 invalid request or unknown record, 3 this snapshot
carries no CRM records, 4 the snapshot failed verification.

The functions below are also the query engine of the maintenance helper
crm_query.py, which binds them to a wiki under its own lock or release check.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":
    # Bound to the bundled snapshot: no bytecode files, siblings importable under python -I.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import json
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Optional

import crm_contract
import crm_filters
from crm_contract import (
    COMPOSITE_SUBFIELDS, DATAMODEL_PATH, SETTINGS_PATH, SYSTEM_KEYS, UUID_RE, CrmError, Record, RecordStore,
    format_instant, load_datamodel, load_json_file, normalize_email, normalize_url, parse_instant,
    parse_record_link, read_events, valid_date,
)
from crm_filters import Context, FilterError

NUMBER_FORMATS = ("COMMAS_AND_DOT", "DOTS_AND_COMMA", "SPACES_AND_COMMA", "APOSTROPHE_AND_DOT")
DATE_FORMATS = ("DAY_FIRST", "MONTH_FIRST", "YEAR_FIRST")
TIME_FORMATS = ("HOUR_24", "HOUR_12")
INSTANT_KEYS = ("crm_created_at", "crm_updated_at", "crm_deleted_at")
COUNT_FUNCTIONS = {"COUNT", "COUNT_UNIQUE_VALUES", "COUNT_EMPTY", "COUNT_NOT_EMPTY", "COUNT_TRUE", "COUNT_FALSE"}
AMOUNT_FUNCTIONS = {"SUM", "AVG", "MIN", "MAX"}
FUNCTION_ALIASES = {"AVERAGE": "AVG", "MINIMUM": "MIN", "MAXIMUM": "MAX", "COUNT_UNIQUE": "COUNT_UNIQUE_VALUES"}
SEARCH_WEIGHTS = {"title": 10.0, "EMAILS": 6.0, "LINKS": 5.0, "TEXT": 3.0, "RICH_TEXT": 1.0}
MICROS = Decimal(1_000_000)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
WORD = re.compile(r"[^\W_]+", re.UNICODE)
LINK_LABEL = re.compile(r"^\[\[[^|\]]+\|([^\]]*)\]\]$")
LIMITS = {"find": (50, 1000), "search": (20, 200), "timeline": (200, 5000)}


class QueryError(ValueError):
    """The request cannot be answered as asked: unknown object or field, malformed filter."""


class NotFound(QueryError):
    """The requested record does not exist."""


# ---------------------------------------------------------------------------
# small helpers


def decimal_text(value: Decimal) -> str:
    """Exact decimal as plain text without exponent; money never passes through float."""
    return format(value.normalize(), "f")


def rounded(value: Decimal, places: int = 6) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def words(value: str) -> list[str]:
    return WORD.findall(fold(value))


def as_links(value: Any) -> list[str]:
    values = value if isinstance(value, list) else ([value] if value else [])
    return [item for item in values if isinstance(item, str)]


def load_json_argument(text: str, option: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise QueryError(f"{option} is not valid JSON: {exc.msg}") from exc


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"not serializable: {type(value).__name__}")


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=json_default)


def _read_only(*_args: Any, **_kwargs: Any) -> Any:
    raise CrmError("CRM queries are read-only; writing is not possible here")


def seal_read_only() -> None:
    """Disable every writing entry point of the CRM core in this process."""
    for name in ("apply_transaction", "plan_transaction", "atomic_write", "purge_history"):
        if hasattr(crm_contract, name):
            setattr(crm_contract, name, _read_only)


def normalize_function(name: Any) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name or "").strip())
    text = re.sub(r"[\s-]+", "_", text).upper()
    text = FUNCTION_ALIASES.get(text, text)
    if text not in crm_filters.AGGREGATES:
        raise QueryError(f"unknown aggregate function {name!r}; use one of {', '.join(sorted(crm_filters.AGGREGATES))}")
    return text


# ---------------------------------------------------------------------------
# session


class Session:
    """One query against one wiki state: data model, records, settings and evaluation moment."""

    def __init__(self, target: Path, *, now: Optional[str] = None, me: Optional[str] = None, path_prefix: str = ""):
        self.target = target
        self.path_prefix = path_prefix
        self.datamodel = load_datamodel(target)
        settings = load_json_file(target, SETTINGS_PATH, {})
        self.settings = settings if isinstance(settings, dict) else {}
        self.warnings: list[str] = []
        moment = now or crm_contract.utc_now()
        if parse_instant(moment) is None:
            raise QueryError(f"--now must be an ISO-8601 instant such as 2026-10-06T12:00:00Z, not {moment!r}")
        member = me.strip().lower() if isinstance(me, str) and me.strip() else None
        if member is not None and not UUID_RE.fullmatch(member):
            raise QueryError("--me must be the UUID of a workspace member record")
        zone = str(self.settings.get("time_zone") or "UTC")
        week_start = self.settings.get("calendar_start_day", 1)
        if isinstance(week_start, bool) or not isinstance(week_start, int) or not 1 <= week_start <= 7:
            week_start = 1
        self.context = Context(now=moment, time_zone=zone, me=member, week_start=week_start)
        self.time_zone = zone
        if self.context.tz is timezone.utc and zone not in ("UTC", "Etc/UTC"):
            self.time_zone = "UTC"
            self.warnings.append(f"time zone {zone} is not available on this system; dates are evaluated and shown in UTC")
        self.number_format = self._choice("number_format", NUMBER_FORMATS, "COMMAS_AND_DOT")
        self.date_format = self._choice("date_format", DATE_FORMATS, "YEAR_FIRST")
        self.time_format = self._choice("time_format", TIME_FORMATS, "HOUR_24")
        self.store = RecordStore(target, self.datamodel)
        self._inverse: dict[tuple[str, str], dict[str, list[Record]]] = {}
        self._events: Optional[list[dict[str, Any]]] = None
        if member and "workspaceMember" in self.datamodel.objects and self.store.get("workspaceMember", member) is None:
            self.warnings.append(f"--me {member} names no workspaceMember record")

    def _choice(self, key: str, allowed: tuple[str, ...], default: str) -> str:
        value = self.settings.get(key)
        if value in allowed:
            return str(value)
        if value not in (None, "", "SYSTEM"):
            self.warnings.append(f"{SETTINGS_PATH}: {key} {value!r} is unknown; {default} is used")
        return default

    def envelope(self, command: str, **values: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "state": "ok",
            "command": command,
            "generated_at": format_instant(self.context.now),
            "time_zone": self.time_zone,
            "formats": {"date": self.date_format, "time": self.time_format, "number": self.number_format},
        }
        result.update(values)
        warnings = list(dict.fromkeys(self.warnings)) + [f"unreadable record skipped: {item}" for item in self.store.load_errors]
        if warnings:
            result["warnings"] = warnings
        return result

    # -- names and keys ---------------------------------------------------

    def object_name(self, name: Any) -> str:
        if not isinstance(name, str) or name not in self.datamodel.objects:
            known = ", ".join(sorted(self.datamodel.objects))
            raise QueryError(f"unknown object {name!r}; objects are: {known}")
        return name

    def field_type(self, object_name: str, key: str) -> tuple[str, str, str]:
        try:
            ftype, base, sub = crm_filters.field_type(self.datamodel, object_name, key)
        except FilterError as exc:
            raise QueryError(str(exc)) from exc
        if sub and sub not in COMPOSITE_SUBFIELDS.get(ftype, ()) and not (ftype == "CURRENCY" and sub == "amount"):
            raise QueryError(f"{object_name}.{base} has no subfield {sub}")
        return ftype, base, sub

    def is_inverse(self, object_name: str, key: str) -> bool:
        if key in crm_filters.SYSTEM_FIELDS:
            return False
        definition = self.datamodel.fields(object_name).get(key.partition(".")[0], {})
        return definition.get("type") == "RELATION" and definition.get("relation", {}).get("type") == "ONE_TO_MANY"

    def parse_filter(self, object_name: str, text: Optional[str]) -> Optional[dict[str, Any]]:
        if text in (None, ""):
            return None
        spec = load_json_argument(str(text), "--filter")
        errors = crm_filters.validate_filter(self.datamodel, object_name, spec)
        if errors:
            raise QueryError("invalid filter: " + "; ".join(errors))
        group = crm_filters.normalize_filter(spec)
        for condition in self._conditions(group):
            ftype, base, sub = self.field_type(object_name, condition["field"])
            if self.is_inverse(object_name, condition["field"]):
                raise QueryError(f"{object_name}.{base} is a computed inverse relation and cannot be filtered")
            values = condition.get("value") if isinstance(condition.get("value"), list) else [condition.get("value")]
            if "@me" in values and not self.context.me:
                self.warnings.append("the filter uses @me but --me was not given; @me matches nothing")
            if ftype in ("SELECT", "MULTI_SELECT") and not sub and condition["operand"] in ("IS", "IS_NOT"):
                options = crm_contract.option_values(self.datamodel.field(object_name, base))
                unknown = [value for value in values if isinstance(value, str) and value not in options and value != "@me"]
                if unknown:
                    self.warnings.append(
                        f"filter values {unknown} are not option values of {object_name}.{base} "
                        f"({', '.join(options)}); filters compare option API names, not labels"
                    )
        return group

    def _conditions(self, node: Optional[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        if node is None:
            return
        if "conditions" in node:
            for child in node["conditions"]:
                yield from self._conditions(child)
        else:
            yield node

    def parse_sorts(self, object_name: str, text: Optional[str]) -> list[dict[str, str]]:
        if text in (None, ""):
            return []
        spec = load_json_argument(str(text), "--sort")
        sorts = []
        for item in spec if isinstance(spec, list) else [spec]:
            if isinstance(item, str):
                item = {"field": item}
            if not isinstance(item, dict) or not isinstance(item.get("field"), str):
                raise QueryError('a sort is {"field": "<field>", "direction": "asc" or "desc"}')
            direction = str(item.get("direction") or "asc").lower()
            if direction not in ("asc", "desc"):
                raise QueryError(f"sort direction must be asc or desc, not {item.get('direction')!r}")
            self.field_type(object_name, item["field"])
            if self.is_inverse(object_name, item["field"]):
                raise QueryError(f"{object_name}.{item['field']} is a computed inverse relation and cannot be sorted")
            sorts.append({"field": item["field"], "direction": direction})
        return sorts

    def parse_fields(self, object_name: str, text: Optional[str]) -> list[str]:
        if not text:
            return self.default_fields(object_name)
        keys = list(dict.fromkeys(part.strip() for part in str(text).split(",") if part.strip()))
        for key in keys:
            self.field_type(object_name, key)
        return keys

    def default_fields(self, object_name: str) -> list[str]:
        label = self.datamodel.label_field(object_name)
        keys = [label] if label else []
        for name, definition in self.datamodel.fields(object_name).items():
            if name == label or definition.get("active", True) is False or definition.get("type") == "RICH_TEXT":
                continue
            if self.is_inverse(object_name, name):
                continue
            keys.append(name)
        return keys

    # -- values -----------------------------------------------------------

    def records(self, object_name: str) -> list[Record]:
        return list(self.store.records(object_name).values())

    def record_ref(self, record: Record) -> dict[str, Any]:
        reference = {
            "object": record.object,
            "id": record.id,
            "title": record.data.get("crm_title"),
            "path": self.path_prefix + record.path,
        }
        if record.deleted:
            reference["deleted"] = True
        return reference

    def find_record(self, object_names: Iterable[str], record_id: str) -> Optional[Record]:
        for object_name in object_names:
            if object_name in self.datamodel.objects:
                record = self.store.get(object_name, record_id)
                if record is not None:
                    return record
        return None

    def link_info(self, link: Any) -> Optional[dict[str, Any]]:
        if not isinstance(link, str) or not link:
            return None
        parsed = parse_record_link(link)
        if parsed is None:
            return {"link": link}
        object_name = self.datamodel.object_for_directory(parsed[0])
        label = LINK_LABEL.match(link.strip())
        target = self.store.get(object_name, parsed[1]) if object_name else None
        info: dict[str, Any] = {
            "object": object_name or parsed[0],
            "id": parsed[1],
            "title": (target.data.get("crm_title") if target else None) or (label.group(1) if label else parsed[1][:8]),
        }
        if target is None:
            info["exists"] = False
        else:
            info["path"] = self.path_prefix + target.path
            if target.deleted:
                info["deleted"] = True
        return info

    def inverse_records(self, object_name: str, field_name: str, record_id: str) -> list[Record]:
        """Records on the many side that point at record_id; deleted ones are left out."""
        relation = self.datamodel.field(object_name, field_name).get("relation", {})
        source, inverse = relation.get("target"), relation.get("inverse")
        if source not in self.datamodel.objects or not inverse:
            return []
        key = (source, inverse)
        if key not in self._inverse:
            index: dict[str, list[Record]] = {}
            for record in self.store.records(source).values():
                for link in as_links(record.data.get(inverse)):
                    parsed = parse_record_link(link)
                    if parsed:
                        index.setdefault(parsed[1], []).append(record)
            self._inverse[key] = index
        return [record for record in self._inverse[key].get(record_id, []) if not record.deleted]

    def raw(self, record: Record, key: str) -> Any:
        ftype, base, sub = self.field_type(record.object, key)
        data = record.data
        if key in crm_filters.SYSTEM_FIELDS:
            return record.id if key == "crm_id" else data.get(key)
        if ftype == "RICH_TEXT":
            return record.richtext.get(base) or None
        if ftype in COMPOSITE_SUBFIELDS:
            if ftype == "CURRENCY" and sub == "amount":
                micros = data.get(f"{base}.amountMicros")
                return None if not isinstance(micros, int) else decimal_text(Decimal(micros) / MICROS)
            if sub:
                return data.get(key)
            value = {
                part: data.get(f"{base}.{part}")
                for part in COMPOSITE_SUBFIELDS[ftype]
                if data.get(f"{base}.{part}") not in (None, "", [])
            }
            if ftype == "CURRENCY" and isinstance(value.get("amountMicros"), int):
                value["amount"] = decimal_text(Decimal(value["amountMicros"]) / MICROS)
            return value or None
        if ftype == "RELATION":
            if self.is_inverse(record.object, base):
                return [self.record_ref(other) for other in self.inverse_records(record.object, base, record.id)]
            return self.link_info(data.get(base))
        if ftype == "MORPH_RELATION":
            value = data.get(base)
            if isinstance(value, list):
                return [self.link_info(item) for item in value]
            return self.link_info(value)
        return data.get(base)

    def display(self, record: Record, key: str) -> str:
        ftype, base, sub = self.field_type(record.object, key)
        data = record.data
        if key in INSTANT_KEYS:
            return self.instant_text(data.get(key))
        if key in crm_filters.SYSTEM_FIELDS:
            value = record.id if key == "crm_id" else data.get(key)
            return "" if value is None else str(value)
        if ftype == "DATE_TIME" and not sub:
            return self.instant_text(data.get(base))
        if ftype == "DATE" and not sub:
            return self.date_text_of(data.get(base))
        if ftype == "RELATION" and self.is_inverse(record.object, base):
            return ", ".join(str(other.data.get("crm_title") or other.id) for other in self.inverse_records(record.object, base, record.id))
        if ftype in ("RELATION", "MORPH_RELATION"):
            infos = [self.link_info(link) for link in as_links(data.get(base))]
            return ", ".join(str(info.get("title") or info.get("link") or "") for info in infos if info)
        if ftype == "NUMBER" and not sub:
            value = data.get(base)
            return "" if value is None or isinstance(value, bool) else self.number_text(Decimal(str(value)))
        if ftype == "NUMERIC" and not sub:
            value = data.get(base)
            return "" if value in (None, "") else self.number_text(Decimal(str(value)))
        if ftype == "CURRENCY" and sub == "amount":
            micros = data.get(f"{base}.amountMicros")
            return "" if not isinstance(micros, int) else self.number_text(Decimal(micros) / MICROS, 2)
        return crm_filters.display_value(self.datamodel, record, key, self.number_format)

    def number_text(self, value: Decimal, places: Optional[int] = None) -> str:
        return crm_filters.format_number(value, self.number_format, places)

    def amount_text(self, value: Decimal, currency: Optional[str]) -> str:
        return f"{self.number_text(value, 2)} {currency or ''}".strip()

    def date_text(self, day: date) -> str:
        if self.date_format == "DAY_FIRST":
            return f"{day.day:02d}.{day.month:02d}.{day.year:04d}"
        if self.date_format == "MONTH_FIRST":
            return f"{day.month:02d}/{day.day:02d}/{day.year:04d}"
        return day.isoformat()

    def date_text_of(self, value: Any) -> str:
        if value in (None, ""):
            return ""
        if valid_date(value):
            return self.date_text(date.fromisoformat(value))
        return str(value)

    def time_text(self, moment: datetime) -> str:
        if self.time_format == "HOUR_12":
            return f"{(moment.hour % 12) or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"
        return f"{moment.hour:02d}:{moment.minute:02d}"

    def instant_text(self, value: Any) -> str:
        moment = parse_instant(value) if isinstance(value, str) else None
        if moment is None:
            return "" if value in (None, "") else str(value)
        local = self.context.local(moment)
        return f"{self.date_text(local.date())} {self.time_text(local)}"

    def row(self, record: Record, keys: list[str]) -> dict[str, Any]:
        return {
            "id": record.id,
            "object": record.object,
            "title": record.data.get("crm_title"),
            "path": self.path_prefix + record.path,
            "deleted": record.deleted,
            "values": {key: {"display": self.display(record, key), "raw": self.raw(record, key)} for key in keys},
        }

    # -- aggregates -------------------------------------------------------

    def aggregate_value(self, object_name: str, function: str, key: Optional[str], records: list[Record]) -> dict[str, Any]:
        if key and function in AMOUNT_FUNCTIONS:
            ftype, base, sub = self.field_type(object_name, key)
            if ftype == "CURRENCY" and sub in ("", "amount"):
                by_code: dict[str, list[Record]] = {}
                for record in records:
                    if isinstance(record.data.get(f"{base}.amountMicros"), int):
                        by_code.setdefault(str(record.data.get(f"{base}.currencyCode") or ""), []).append(record)
                if len(by_code) > 1:
                    return {
                        "value": None,
                        "display": "",
                        "by_currency": {code or "-": self._amount_result(function, key, items, code) for code, items in sorted(by_code.items())},
                        "note": "amounts in different currencies are not added up",
                    }
                code = next(iter(by_code), "")
                if not code and function == "SUM":
                    # A sum over no amount is 0 in the field's or the workspace's currency, as in the maintained wiki.
                    code = crm_filters.default_currency(self.datamodel, object_name, key, self.settings.get("default_currency"))
                return self._amount_result(function, key, records, code)
            if ftype in ("DATE", "DATE_TIME") and not sub and function in ("MIN", "MAX"):
                # crm_filters compares numbers only; the earliest and latest date are compared as ISO text.
                values = sorted(str(value) for value in (record.data.get(base) for record in records) if value not in (None, ""))
                if not values:
                    return {"value": None, "display": ""}
                chosen = values[0] if function == "MIN" else values[-1]
                return {"value": chosen, "display": self.instant_text(chosen) if ftype == "DATE_TIME" else self.date_text_of(chosen)}
        try:
            value = crm_filters.aggregate(self.datamodel, records, function, key)
        except FilterError as exc:
            raise QueryError(str(exc)) from exc
        if value is None:
            return {"value": None, "display": ""}
        if function in COUNT_FUNCTIONS:
            return {"value": int(value), "display": self.number_text(Decimal(int(value)))}
        if function.startswith("PERCENTAGE"):
            return {"value": decimal_text(rounded(value)), "display": f"{self.number_text(value, 1)} %"}
        if function == "AVG":
            return {"value": decimal_text(rounded(value)), "display": self.number_text(value, 2)}
        return {"value": decimal_text(value), "display": self.number_text(value)}

    def option_groups(self, object_name: str, key: str) -> list[str]:
        """Every option of a SELECT or MULTI_SELECT group field, in option order; other fields have none."""
        if key in crm_filters.SYSTEM_FIELDS:
            return []
        ftype, base, sub = self.field_type(object_name, key)
        if ftype in ("SELECT", "MULTI_SELECT") and not sub:
            return crm_contract.option_values(self.datamodel.field(object_name, base))
        return []

    def empty_value(self, object_name: str, function: str, key: Optional[str], records: list[Record]) -> dict[str, Any]:
        """The aggregate of a group without records: counts and sums are 0, other functions have no value."""
        if function in COUNT_FUNCTIONS:
            return {"value": 0, "display": self.number_text(Decimal(0))}
        if function != "SUM" or not key:
            return {"value": None, "display": ""}
        ftype, base, sub = self.field_type(object_name, key)
        if ftype == "CURRENCY" and sub in ("", "amount"):
            codes = {
                str(record.data.get(f"{base}.currencyCode") or "")
                for record in records if isinstance(record.data.get(f"{base}.amountMicros"), int)
            }
            code = next(iter(codes)) if len(codes) == 1 else ("" if codes else crm_filters.default_currency(
                self.datamodel, object_name, key, self.settings.get("default_currency")))
            return {"value": "0", "display": self.amount_text(Decimal(0), code or None), "currency": code or None}
        if ftype in ("NUMBER", "NUMERIC", "RATING") or (ftype in COMPOSITE_SUBFIELDS and sub in ("amountMicros", "addressLat", "addressLng")):
            return {"value": "0", "display": self.number_text(Decimal(0))}
        return {"value": None, "display": ""}

    def _amount_result(self, function: str, key: str, records: list[Record], currency: str) -> dict[str, Any]:
        try:
            value = crm_filters.aggregate(self.datamodel, records, function, key)
        except FilterError as exc:
            raise QueryError(str(exc)) from exc
        if value is None:
            return {"value": None, "display": "", "currency": currency or None}
        if function == "AVG":
            value = rounded(value)
        return {"value": decimal_text(value), "display": self.amount_text(value, currency), "currency": currency or None}

    def group_label(self, object_name: str, key: str, value: str, grain: Optional[str]) -> str:
        if value == "":
            return ""
        ftype, base, sub = self.field_type(object_name, key)
        if key in crm_filters.SYSTEM_FIELDS:
            return self.instant_text(value) if key in INSTANT_KEYS and grain in (None, "NONE") else value
        if ftype in ("SELECT", "MULTI_SELECT") and not sub:
            for option in self.datamodel.field(object_name, base).get("options", []):
                if option.get("value") == value:
                    return str(option.get("label") or value)
            return value
        if ftype in ("RELATION", "MORPH_RELATION"):
            relation = self.datamodel.field(object_name, base).get("relation", {})
            targets = [relation.get("target")] if ftype == "RELATION" else list(relation.get("targets", []))
            record = self.find_record([target for target in targets if target], value)
            return str(record.data.get("crm_title")) if record else value
        if ftype == "BOOLEAN":
            return "✓" if value == "true" else "✗"
        if ftype == "RATING" and value in crm_contract.RATING_VALUES:
            return "★" * (crm_contract.RATING_VALUES.index(value) + 1)
        if grain in (None, "NONE"):
            if ftype == "DATE_TIME" and not sub:
                return self.instant_text(value)
            if ftype == "DATE" and not sub:
                return self.date_text_of(value)
        return value

    def order_groups(self, object_name: str, key: str, grain: Optional[str], values: list[str]) -> list[str]:
        ftype, base, sub = self.field_type(object_name, key)
        present = [value for value in values if value != ""]
        if ftype in ("SELECT", "MULTI_SELECT") and not sub and key not in crm_filters.SYSTEM_FIELDS:
            options = crm_contract.option_values(self.datamodel.field(object_name, base))
            present.sort(key=lambda value: (options.index(value) if value in options else len(options), value))
        elif ftype in ("DATE", "DATE_TIME", "RATING"):
            present.sort()
        elif ftype in ("NUMBER", "NUMERIC", "CURRENCY"):
            def numeric(value: str) -> tuple[int, Any]:
                try:
                    return (0, Decimal(value))
                except Exception:  # noqa: BLE001 - non-numeric keys sort after numbers
                    return (1, value)

            present.sort(key=numeric)
        else:
            present.sort(key=lambda value: (fold(self.group_label(object_name, key, value, grain)), value))
        return present + [value for value in values if value == ""]

    # -- events -----------------------------------------------------------

    def events(self) -> list[dict[str, Any]]:
        if self._events is None:
            loaded = list(read_events(self.target))
            loaded.sort(key=lambda event: (parse_instant(event.get("at")) or EPOCH, event.get("_shard", ""), event.get("_line", 0)))
            self._events = loaded
        return self._events

    def timeline_entry(self, event: dict[str, Any]) -> dict[str, Any]:
        object_name = event.get("object")
        record = None
        if object_name in self.datamodel.objects and isinstance(event.get("record_id"), str):
            record = self.store.get(object_name, event["record_id"])
        changes = event.get("changes") if isinstance(event.get("changes"), dict) else {}
        title = record.data.get("crm_title") if record else None
        if title is None:
            change = changes.get("crm_title")
            if isinstance(change, list) and change:
                title = change[0] or change[-1]
            elif isinstance(change, str):
                title = change
        entry: dict[str, Any] = {
            "at": event.get("at"),
            "at_display": self.instant_text(event.get("at")),
            "actor": event.get("actor"),
            "op": event.get("op"),
            "object": object_name,
            "record_id": event.get("record_id"),
            "title": title,
            "path": self.path_prefix + record.path if record else None,
            "changes": changes,
            "origin": event.get("origin", {}),
        }
        for key in ("txn", "caused_by", "merged", "erased", "schema_change"):
            if key in event:
                entry[key] = event[key]
        return entry


# ---------------------------------------------------------------------------
# commands


def find(
    session: Session,
    object_name: str,
    *,
    filter_text: Optional[str] = None,
    sort_text: Optional[str] = None,
    fields_text: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    include_deleted: bool = False,
) -> dict[str, Any]:
    object_name = session.object_name(object_name)
    group = session.parse_filter(object_name, filter_text)
    sorts = session.parse_sorts(object_name, sort_text)
    keys = session.parse_fields(object_name, fields_text)
    try:
        chosen = crm_filters.select_records(
            session.datamodel, session.records(object_name), filter_spec=group, sorts=sorts,
            context=session.context, include_deleted=include_deleted,
        )
    except FilterError as exc:
        raise QueryError(str(exc)) from exc
    page = chosen[offset:offset + limit]
    return session.envelope(
        "find",
        object=object_name,
        filter=group,
        sort=sorts,
        fields=keys,
        include_deleted=include_deleted,
        total=len(chosen),
        offset=offset,
        limit=limit,
        returned=len(page),
        rows=[session.row(record, keys) for record in page],
    )


def aggregate(
    session: Session,
    object_name: str,
    function: str,
    *,
    field: Optional[str] = None,
    group_by: Optional[str] = None,
    granularity: Optional[str] = None,
    filter_text: Optional[str] = None,
    include_deleted: bool = False,
) -> dict[str, Any]:
    object_name = session.object_name(object_name)
    function = normalize_function(function)
    if function != "COUNT" and not field:
        raise QueryError(f"{function} needs --field")
    if field:
        session.field_type(object_name, field)
        if session.is_inverse(object_name, field):
            raise QueryError(f"{object_name}.{field} is a computed inverse relation; aggregate the other side instead")
    grain: Optional[str] = None
    if group_by:
        group_type, _base, group_sub = session.field_type(object_name, group_by)
        if session.is_inverse(object_name, group_by):
            raise QueryError(f"{object_name}.{group_by} is a computed inverse relation and cannot group records")
        if group_type in ("DATE", "DATE_TIME") and not group_sub:
            grain = str(granularity or "DAY").upper()
            if grain not in crm_filters.GRANULARITIES:
                raise QueryError(f"unknown granularity {granularity!r}; use one of {', '.join(sorted(crm_filters.GRANULARITIES))}")
        elif granularity:
            raise QueryError("--granularity applies only when grouping by a date field")
    elif granularity:
        raise QueryError("--granularity needs --group-by")
    group = session.parse_filter(object_name, filter_text)
    try:
        records = crm_filters.select_records(
            session.datamodel, session.records(object_name), filter_spec=group,
            context=session.context, include_deleted=include_deleted,
        )
        result: dict[str, Any] = {
            "object": object_name,
            "function": function,
            "field": field,
            "filter": group,
            "include_deleted": include_deleted,
            "records": len(records),
            **session.aggregate_value(object_name, function, field, records),
        }
        if group_by:
            buckets: dict[str, list[Record]] = {}
            for record in records:
                for key in crm_filters.group_key(session.datamodel, record, group_by, grain or "NONE", session.context):
                    buckets.setdefault(key, []).append(record)
            # Like a kanban board, a select field shows every option, also those without records.
            for option in session.option_groups(object_name, group_by):
                buckets.setdefault(option, [])
            groups = []
            for key in session.order_groups(object_name, group_by, grain, list(buckets)):
                items = buckets[key]
                groups.append({
                    "group": key,
                    "label": session.group_label(object_name, group_by, key, grain),
                    "records": len(items),
                    **(session.aggregate_value(object_name, function, field, items) if items else session.empty_value(object_name, function, field, records)),
                })
            result.update(group_by=group_by, granularity=grain, groups=groups)
    except FilterError as exc:
        raise QueryError(str(exc)) from exc
    return session.envelope("aggregate", **result)


def get(session: Session, object_name: str, record_id: str, *, timeline_limit: int = 200) -> dict[str, Any]:
    object_name = session.object_name(object_name)
    parsed = parse_record_link(record_id) if isinstance(record_id, str) else None
    record_id = parsed[1] if parsed else str(record_id or "").strip().lower()
    record = session.store.get(object_name, record_id)
    if record is None:
        raise NotFound(f"{object_name} record {record_id} does not exist")
    fields: dict[str, Any] = {}
    for name, definition in session.datamodel.fields(object_name).items():
        entry = {
            "label": definition.get("label", name),
            "type": definition.get("type"),
            "display": session.display(record, name),
            "raw": session.raw(record, name),
        }
        if definition.get("active", True) is False:
            entry["active"] = False
        fields[name] = entry
    known = set(SYSTEM_KEYS)
    for name, definition in session.datamodel.fields(object_name).items():
        known.update(crm_contract.frontmatter_keys(name, definition))
    undeclared = {key: value for key, value in record.data.items() if key not in known}
    incoming = []
    for other_object, field_name, definition in session.datamodel.relation_fields_targeting(object_name):
        for other in session.store.records(other_object).values():
            hits = [parse_record_link(link) for link in as_links(other.data.get(field_name))]
            if any(hit and hit[1] == record.id for hit in hits):
                incoming.append({
                    **session.record_ref(other),
                    "field": field_name,
                    "field_label": definition.get("label", field_name),
                    "deleted": other.deleted,
                })
    timeline = [session.timeline_entry(event) for event in session.events() if event.get("record_id") == record.id]
    values: dict[str, Any] = {
        "object": object_name,
        "id": record.id,
        "title": record.data.get("crm_title"),
        "path": session.path_prefix + record.path,
        "deleted": record.deleted,
        "system": {key: record.data[key] for key in SYSTEM_KEYS if key in record.data},
        "system_display": {key: session.instant_text(record.data[key]) for key in INSTANT_KEYS if key in record.data},
        "fields": fields,
        "incoming": incoming,
        "timeline_total": len(timeline),
        "timeline": timeline[-timeline_limit:],
    }
    if undeclared:
        values["undeclared_values"] = undeclared
    return session.envelope("get", **values)


def _search_parts(record: Record, fields: list[tuple[str, str]]) -> list[tuple[str, str, str, list[str]]]:
    """(field, kind, text, identity values) for every searchable value of a record."""
    parts = [("title", "title", str(record.data.get("crm_title") or ""), [])]
    data = record.data
    for name, ftype in fields:
        if ftype == "TEXT":
            text = data.get(name)
            parts.append((name, ftype, text if isinstance(text, str) else "", []))
        elif ftype == "EMAILS":
            addresses = [data.get(f"{name}.primaryEmail")] + list(data.get(f"{name}.additionalEmails") or [])
            addresses = [str(item) for item in addresses if isinstance(item, str) and item.strip()]
            parts.append((name, ftype, " ".join(addresses), [normalize_email(item) for item in addresses]))
        elif ftype == "LINKS":
            urls = [data.get(f"{name}.primaryLinkUrl")]
            try:
                secondary = json.loads(data.get(f"{name}.secondaryLinks") or "[]")
            except (TypeError, json.JSONDecodeError):
                secondary = []
            urls += [item.get("url") for item in secondary if isinstance(item, dict)] if isinstance(secondary, list) else []
            domains = [normalize_url(str(url)) for url in urls if isinstance(url, str) and url.strip()]
            parts.append((name, ftype, " ".join(domains), domains))
        elif ftype == "RICH_TEXT":
            parts.append((name, ftype, record.richtext.get(name, ""), []))
    return parts


def search(
    session: Session,
    text: str,
    *,
    objects_text: Optional[str] = None,
    limit: int = 20,
    include_deleted: bool = False,
) -> dict[str, Any]:
    """Rank records by title, text, e-mail, domain and rich-text matches."""
    query_words = list(dict.fromkeys(words(text or "")))
    if not query_words:
        raise QueryError("--text must contain at least one letter or digit")
    phrase = " ".join(words(text))
    identity = {normalize_email(text), normalize_url(text)}
    if objects_text:
        names = [session.object_name(part.strip()) for part in str(objects_text).split(",") if part.strip()]
    else:
        names = [name for name, definition in session.datamodel.objects.items() if definition.get("active", True) is not False]
    results = []
    for object_name in names:
        fields = [
            (name, definition.get("type"))
            for name, definition in session.datamodel.fields(object_name).items()
            if definition.get("type") in ("TEXT", "EMAILS", "LINKS", "RICH_TEXT") and definition.get("active", True) is not False
        ]
        for record in session.store.records(object_name).values():
            if record.deleted and not include_deleted:
                continue
            score = 0.0
            found: set[str] = set()
            matched: list[str] = []
            snippet = ""
            for name, kind, value, identities in _search_parts(record, fields):
                if not value:
                    continue
                weight = SEARCH_WEIGHTS[kind]
                counts = Counter(words(value))
                part_score = 0.0
                for word in query_words:
                    if counts.get(word):
                        part_score += weight * min(counts[word], 3)
                        found.add(word)
                    elif len(word) >= 2 and any(token.startswith(word) for token in counts):
                        part_score += weight * 0.5
                        found.add(word)
                if len(query_words) > 1 and phrase in " ".join(words(value)):
                    part_score += weight * 2
                if identities and identity & set(identities):
                    part_score += 20.0
                if part_score:
                    score += part_score
                    matched.append(name)
                    if not snippet and kind in ("TEXT", "RICH_TEXT"):
                        snippet = _snippet(value, query_words)
            if not score:
                continue
            if len(found) == len(query_words):
                score += 5.0
            results.append({
                **session.record_ref(record),
                "score": round(score, 2),
                "matched_fields": matched,
                "snippet": snippet,
            })
    results.sort(key=lambda item: (-item["score"], fold(str(item.get("title") or "")), item["id"]))
    return session.envelope(
        "search", text=text, objects=names, include_deleted=include_deleted,
        total=len(results), returned=min(limit, len(results)), results=results[:limit],
    )


def _snippet(text: str, query_words: list[str]) -> str:
    best = ("", 0)
    for line in text.splitlines():
        compact = " ".join(line.split())
        overlap = sum(1 for word in query_words if any(token.startswith(word) for token in words(compact)))
        if compact and overlap > best[1]:
            best = (compact, overlap)
    snippet = best[0]
    return snippet if len(snippet) <= 200 else snippet[:197].rstrip() + "..."


def parse_since(session: Session, value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    if valid_date(value):
        # A plain date means the start of that day in the CRM time zone.
        local = datetime.combine(date.fromisoformat(value), time(0, 0), tzinfo=session.context.tz)
        return local.astimezone(timezone.utc)
    moment = parse_instant(value)
    if moment is None:
        raise QueryError(f"--since must be a date or an ISO-8601 instant, not {value!r}")
    return moment


def timeline(
    session: Session,
    *,
    object_name: Optional[str] = None,
    record_id: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 200,
) -> dict[str, Any]:
    if object_name and object_name not in session.datamodel.objects:
        if not crm_contract.NAME_RE.fullmatch(object_name):
            raise QueryError(f"invalid object name {object_name!r}")
        session.warnings.append(f"{object_name} is not an object of the current data model; only its history is shown")
    if record_id:
        parsed = parse_record_link(record_id)
        record_id = parsed[1] if parsed else record_id.strip().lower()
    start = parse_since(session, since)
    selected = []
    for event in session.events():
        if object_name and event.get("object") != object_name:
            continue
        if record_id and event.get("record_id") != record_id:
            continue
        if start is not None and (parse_instant(event.get("at")) or EPOCH) < start:
            continue
        selected.append(event)
    entries = [session.timeline_entry(event) for event in selected[-limit:]]
    return session.envelope(
        "timeline",
        object=object_name,
        id=record_id,
        since=format_instant(start) if start else None,
        total=len(selected),
        returned=len(entries),
        events=entries,
    )


# ---------------------------------------------------------------------------
# command line


def common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--now", help="Evaluate relative dates at this ISO-8601 instant (default: the current time)")
    common.add_argument("--me", help="UUID of the workspace member that @me stands for in filters")
    return common


def add_query_commands(subparsers: Any, parents: list[argparse.ArgumentParser]) -> None:
    find_parser = subparsers.add_parser("find", parents=parents, help="list records that match a filter")
    find_parser.add_argument("--object", required=True)
    find_parser.add_argument("--filter", help='crm_filters filter as JSON, e.g. {"field": "stage", "operand": "IS", "value": "NEW"}')
    find_parser.add_argument("--sort", help='JSON list such as [{"field": "closeDate", "direction": "asc"}]')
    find_parser.add_argument("--fields", help="comma-separated field keys, e.g. name,address.addressCity,crm_created_at")
    find_parser.add_argument("--limit", type=int, default=LIMITS["find"][0])
    find_parser.add_argument("--offset", type=int, default=0)
    find_parser.add_argument("--include-deleted", action="store_true", help="include records in the trash")
    aggregate_parser = subparsers.add_parser("aggregate", parents=parents, help="COUNT, SUM, AVG, MIN, MAX and more, optionally grouped")
    aggregate_parser.add_argument("--object", required=True)
    aggregate_parser.add_argument("--function", required=True)
    aggregate_parser.add_argument("--field")
    aggregate_parser.add_argument("--group-by")
    aggregate_parser.add_argument("--granularity", help="for date groups: DAY, WEEK, MONTH, QUARTER, YEAR, DAY_OF_WEEK, MONTH_OF_YEAR, QUARTER_OF_YEAR, NONE")
    aggregate_parser.add_argument("--filter")
    aggregate_parser.add_argument("--include-deleted", action="store_true")
    get_parser = subparsers.add_parser("get", parents=parents, help="one record with incoming relations and timeline")
    get_parser.add_argument("--object", required=True)
    get_parser.add_argument("--id", required=True)
    search_parser = subparsers.add_parser("search", parents=parents, help="ranked full-text search")
    search_parser.add_argument("--text", required=True)
    search_parser.add_argument("--objects", help="comma-separated objects; default: every active object")
    search_parser.add_argument("--limit", type=int, default=LIMITS["search"][0])
    search_parser.add_argument("--include-deleted", action="store_true")
    timeline_parser = subparsers.add_parser("timeline", parents=parents, help="change history from the event log")
    timeline_parser.add_argument("--object")
    timeline_parser.add_argument("--id")
    timeline_parser.add_argument("--since", help="date or ISO-8601 instant")
    timeline_parser.add_argument("--limit", type=int, default=LIMITS["timeline"][0])


def check_paging(command: str, limit: int, offset: int = 0) -> None:
    maximum = LIMITS[command][1]
    if not 1 <= limit <= maximum:
        raise QueryError(f"--limit must be between 1 and {maximum}")
    if offset < 0:
        raise QueryError("--offset must not be negative")


def run_query(session: Session, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "find":
        check_paging("find", args.limit, args.offset)
        return find(
            session, args.object, filter_text=args.filter, sort_text=args.sort, fields_text=args.fields,
            limit=args.limit, offset=args.offset, include_deleted=args.include_deleted,
        )
    if args.command == "aggregate":
        return aggregate(
            session, args.object, args.function, field=args.field, group_by=args.group_by,
            granularity=args.granularity, filter_text=args.filter, include_deleted=args.include_deleted,
        )
    if args.command == "get":
        return get(session, args.object, args.id)
    if args.command == "search":
        check_paging("search", args.limit)
        return search(session, args.text, objects_text=args.objects, limit=args.limit, include_deleted=args.include_deleted)
    if args.command == "timeline":
        check_paging("timeline", args.limit)
        return timeline(session, object_name=args.object, record_id=args.id, since=args.since, limit=args.limit)
    raise QueryError(f"unknown command {args.command!r}")


def main(argv: Optional[list[str]] = None) -> int:
    from verify_knowledge import knowledge_root, verify

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_query_commands(subparsers, [common_parser()])
    args = parser.parse_args(argv)

    release = verify()
    if release.get("state") != "ready":
        print(dump({"state": release.get("state", "invalid_snapshot"), "release": release}))
        return 4
    target = knowledge_root()
    if "records/" in (release.get("excluded_prefixes") or []):
        print(dump({
            "state": "crm_excluded",
            "reason": "This snapshot was exported without CRM records (personal data). Questions about individual records cannot be answered from it.",
        }))
        return 3
    if not (target / DATAMODEL_PATH).is_file():
        print(dump({"state": "no_crm_layer", "reason": "This snapshot contains no CRM layer."}))
        return 3
    seal_read_only()
    try:
        result = run_query(Session(target, now=args.now, me=args.me, path_prefix="references/knowledge/"), args)
    except NotFound as exc:
        print(dump({"state": "not_found", "error": str(exc)}))
        return 2
    except QueryError as exc:
        print(dump({"state": "invalid_request", "error": str(exc)}))
        return 2
    except (CrmError, OSError, UnicodeDecodeError) as exc:
        print(dump({"state": "error", "error": str(exc)}))
        return 2
    final = verify()
    if final.get("state") != "ready" or final.get("manifest_sha256") != release.get("manifest_sha256"):
        print(dump({"state": "snapshot_changed", "release": final}))
        return 4
    result["mode"] = "frozen-snapshot"
    result["release"] = {key: release.get(key) for key in ("version", "release_id", "released_at", "manifest_sha256")}
    print(dump(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
