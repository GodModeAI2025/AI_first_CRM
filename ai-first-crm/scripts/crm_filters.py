#!/usr/bin/env python3
"""Filters, sorting, grouping and aggregates over CRM records, following common CRM conventions.

One engine serves views, dashboards, queries, workflows and campaign audiences,
so a filter means the same thing everywhere. Filter groups nest with AND, OR and
NOT; conditions use the standard operands (IS, IS_NOT, CONTAINS, DOES_NOT_CONTAIN,
GREATER_THAN_OR_EQUAL, LESS_THAN_OR_EQUAL, IS_BEFORE, IS_AFTER, IS_EMPTY,
IS_NOT_EMPTY, IS_NOT_NULL, IS_RELATIVE, IS_IN_PAST, IS_IN_FUTURE, IS_TODAY).
Aggregates use the standard operations (COUNT, COUNT_UNIQUE_VALUES, COUNT_EMPTY,
COUNT_NOT_EMPTY, COUNT_TRUE, COUNT_FALSE, PERCENTAGE_EMPTY,
PERCENTAGE_NOT_EMPTY, SUM, AVG, MIN, MAX).

Time-dependent operands are evaluated against an explicit ``now`` and time
zone, which every caller reports next to its result, because a generated view
freezes the moment it was built.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from crm_contract import (
    COMPOSITE_SUBFIELDS, PRIMARY_SUBFIELD, RATING_VALUES, DataModel, Record, parse_instant, parse_record_link,
)

OPERANDS = {
    "IS", "IS_NOT", "IS_NOT_NULL", "LESS_THAN_OR_EQUAL", "GREATER_THAN_OR_EQUAL", "IS_BEFORE", "IS_AFTER",
    "CONTAINS", "DOES_NOT_CONTAIN", "IS_EMPTY", "IS_NOT_EMPTY", "IS_RELATIVE", "IS_IN_PAST", "IS_IN_FUTURE", "IS_TODAY",
}
LEGACY_OPERANDS = {
    "is": "IS", "isnot": "IS_NOT", "isnotnull": "IS_NOT_NULL", "lessthan": "LESS_THAN_OR_EQUAL",
    "greaterthan": "GREATER_THAN_OR_EQUAL", "lessthanorequal": "LESS_THAN_OR_EQUAL",
    "greaterthanorequal": "GREATER_THAN_OR_EQUAL", "isbefore": "IS_BEFORE", "isafter": "IS_AFTER",
    "contains": "CONTAINS", "doesnotcontain": "DOES_NOT_CONTAIN", "isempty": "IS_EMPTY",
    "isnotempty": "IS_NOT_EMPTY", "isrelative": "IS_RELATIVE", "isinpast": "IS_IN_PAST",
    "isinfuture": "IS_IN_FUTURE", "istoday": "IS_TODAY",
}
AGGREGATES = {
    "COUNT", "COUNT_UNIQUE_VALUES", "COUNT_EMPTY", "COUNT_NOT_EMPTY", "COUNT_TRUE", "COUNT_FALSE",
    "PERCENTAGE_EMPTY", "PERCENTAGE_NOT_EMPTY", "SUM", "AVG", "MIN", "MAX",
}
SYSTEM_FIELDS = {
    "crm_created_at": "DATE_TIME", "crm_updated_at": "DATE_TIME", "crm_deleted_at": "DATE_TIME",
    "crm_created_by": "TEXT", "crm_updated_by": "TEXT", "crm_title": "TEXT", "crm_id": "UUID",
    "crm_position": "NUMBER", "crm_created_source": "TEXT",
}
RELATIVE_RE = re.compile(r"^(PAST|NEXT|THIS)_(\d+)_(DAY|WEEK|MONTH|QUARTER|YEAR)S?$")
GRANULARITIES = {"DAY", "WEEK", "MONTH", "QUARTER", "YEAR", "DAY_OF_WEEK", "MONTH_OF_YEAR", "QUARTER_OF_YEAR", "NONE"}


class FilterError(ValueError):
    """A filter, sort, grouping or aggregate definition is invalid."""


class Context:
    """Evaluation context: the moment, the time zone, and who 'me' is."""

    def __init__(self, now: Optional[str] = None, time_zone: str = "UTC", me: Optional[str] = None, week_start: int = 1):
        self.now = parse_instant(now) if now else datetime.now(timezone.utc)
        if self.now is None:
            raise FilterError(f"invalid now {now!r}")
        self.time_zone_name = time_zone or "UTC"
        self.tz = _zone(self.time_zone_name)
        self.me = me
        self.week_start = week_start

    def local(self, moment: datetime) -> datetime:
        return moment.astimezone(self.tz)

    def today(self) -> date:
        return self.local(self.now).date()


def _zone(name: str):
    if name in ("", "UTC", "Etc/UTC"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+, needs the system time zone database

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - an unknown or unavailable zone falls back to UTC visibly
        return timezone.utc


# ---------------------------------------------------------------------------
# value access


def field_type(datamodel: DataModel, object_name: str, key: str) -> tuple[str, str, str]:
    """(field type, base field, subfield) for a field key such as amount or address.addressCity."""
    if key in SYSTEM_FIELDS:
        return SYSTEM_FIELDS[key], key, ""
    base, _, sub = key.partition(".")
    definition = datamodel.fields(object_name).get(base)
    if definition is None:
        raise FilterError(f"unknown field {object_name}.{key}")
    return definition["type"], base, sub


def raw_value(datamodel: DataModel, record: Record, key: str) -> Any:
    """The comparable value of a field: composite fields resolve to their identifying part."""
    ftype, base, sub = field_type(datamodel, record.object, key)
    data = record.data
    if key in SYSTEM_FIELDS:
        return record.id if key == "crm_id" else data.get(key)
    if ftype == "RICH_TEXT":
        return record.richtext.get(base, "")
    if ftype in COMPOSITE_SUBFIELDS:
        if sub:
            if ftype == "CURRENCY" and sub == "amount":
                micros = data.get(f"{base}.amountMicros")
                return None if micros is None else Decimal(micros) / Decimal(1_000_000)
            return data.get(f"{base}.{sub}")
        if ftype == "FULL_NAME":
            name = " ".join(str(data.get(f"{base}.{part}") or "").strip() for part in ("firstName", "lastName")).strip()
            return name or None
        if ftype == "CURRENCY":
            micros = data.get(f"{base}.amountMicros")
            return None if micros is None else Decimal(micros) / Decimal(1_000_000)
        return data.get(f"{base}.{PRIMARY_SUBFIELD[ftype]}")
    if ftype in {"RELATION", "MORPH_RELATION"}:
        value = data.get(base)
        values = value if isinstance(value, list) else ([value] if value else [])
        ids = []
        for item in values:
            parsed = parse_record_link(item) if isinstance(item, str) else None
            if parsed:
                ids.append(parsed[1])
        return ids
    return data.get(base)


def text_blob(datamodel: DataModel, record: Record, key: str) -> str:
    """All text a CONTAINS search on this field should see, including subfields and link labels."""
    ftype, base, sub = field_type(datamodel, record.object, key)
    if ftype in COMPOSITE_SUBFIELDS and not sub:
        parts = [str(record.data.get(f"{base}.{part}") or "") for part in COMPOSITE_SUBFIELDS[ftype]]
        return " ".join(parts)
    if ftype in {"RELATION", "MORPH_RELATION"}:
        value = record.data.get(base)
        return " ".join(value) if isinstance(value, list) else str(value or "")
    value = raw_value(datamodel, record, key)
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return "" if value is None else str(value)


def is_empty_value(value: Any) -> bool:
    return value is None or value == "" or value == []


def _as_moment(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return parse_instant(value + "T00:00:00Z")
        return parse_instant(value)
    return None


def _as_number(value: Any) -> Optional[Decimal]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str) and value in RATING_VALUES:
        return Decimal(RATING_VALUES.index(value) + 1)
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# filters


def normalize_filter(spec: Any) -> Optional[dict[str, Any]]:
    """Accept {op, conditions} groups, single conditions, or None; return a canonical group."""
    if spec in (None, {}, []):
        return None
    if isinstance(spec, list):
        return {"op": "AND", "conditions": [normalize_filter(item) for item in spec]}
    if not isinstance(spec, dict):
        raise FilterError("a filter must be a mapping")
    if "conditions" in spec:
        op = str(spec.get("op") or spec.get("logicalOperator") or "AND").upper()
        if op not in {"AND", "OR", "NOT"}:
            raise FilterError(f"unknown filter group operator {op}")
        conditions = [normalize_filter(item) for item in spec.get("conditions") or []]
        return {"op": op, "conditions": [item for item in conditions if item is not None]}
    operand = spec.get("operand") or spec.get("operator")
    if not isinstance(operand, str):
        raise FilterError("a condition needs field and operand")
    canonical = operand.upper() if operand.upper() in OPERANDS else LEGACY_OPERANDS.get(operand.replace("_", "").lower())
    if canonical not in OPERANDS:
        raise FilterError(f"unknown operand {operand!r}")
    if not isinstance(spec.get("field"), str):
        raise FilterError("a condition needs a field")
    return {"field": spec["field"], "operand": canonical, "value": spec.get("value")}


def validate_filter(datamodel: DataModel, object_name: str, spec: Any) -> list[str]:
    errors: list[str] = []
    try:
        group = normalize_filter(spec)
    except FilterError as exc:
        return [str(exc)]

    def walk(node: Optional[dict[str, Any]]) -> None:
        if node is None:
            return
        if "conditions" in node:
            for child in node["conditions"]:
                walk(child)
            return
        try:
            ftype, base, sub = field_type(datamodel, object_name, node["field"])
        except FilterError as exc:
            errors.append(str(exc))
            return
        if ftype in {"RELATION", "MORPH_RELATION"} and sub:
            errors.append(f"{node['field']}: filters cannot reach fields of related records; filter on the relation itself (the linked record)")
            return
        if ftype == "RELATION" and datamodel.fields(object_name)[base].get("relation", {}).get("type") == "ONE_TO_MANY":
            errors.append(f"{node['field']} is the computed inverse side and cannot be filtered; filter the other object instead")
            return
        if node["operand"] == "IS_RELATIVE" and not RELATIVE_RE.fullmatch(str(node.get("value") or "").upper()):
            errors.append(f"IS_RELATIVE needs a value like PAST_7_DAY, NEXT_2_WEEK or THIS_1_MONTH, not {node.get('value')!r}")
        if node["operand"] in {"IS_BEFORE", "IS_AFTER", "IS_RELATIVE", "IS_IN_PAST", "IS_IN_FUTURE", "IS_TODAY"} and ftype not in {"DATE", "DATE_TIME"}:
            errors.append(f"{node['operand']} applies to date fields, not {node['field']} ({ftype})")

    walk(group)
    return errors


def matches(datamodel: DataModel, record: Record, spec: Any, context: Context) -> bool:
    group = normalize_filter(spec) if not (isinstance(spec, dict) and spec.get("_normalized")) else spec
    if group is None:
        return True
    return _match(datamodel, record, group, context)


def _match(datamodel: DataModel, record: Record, node: dict[str, Any], context: Context) -> bool:
    if "conditions" in node:
        results = (_match(datamodel, record, child, context) for child in node["conditions"])
        if node["op"] == "AND":
            return all(results)
        if node["op"] == "OR":
            conditions = node["conditions"]
            return any(results) if conditions else True
        return not all(results)
    return _condition(datamodel, record, node, context)


def _resolve_me(value: Any, context: Context) -> Any:
    if value in ("@me", "ME", "me") and context.me:
        return context.me
    if isinstance(value, list):
        return [_resolve_me(item, context) for item in value]
    return value


def _condition(datamodel: DataModel, record: Record, node: dict[str, Any], context: Context) -> bool:
    key = node["field"]
    operand = node["operand"]
    expected = _resolve_me(node.get("value"), context)
    ftype, _base, sub = field_type(datamodel, record.object, key)
    value = raw_value(datamodel, record, key)
    if operand in {"IS_EMPTY", "IS_NOT_EMPTY", "IS_NOT_NULL"}:
        empty = is_empty_value(value)
        return empty if operand == "IS_EMPTY" else not empty
    if operand in {"CONTAINS", "DOES_NOT_CONTAIN"}:
        needle = str(expected or "").casefold()
        if isinstance(expected, list):
            haystack_values = value if isinstance(value, list) else [value]
            hit = any(str(item).casefold() in {str(e).casefold() for e in expected} for item in haystack_values if item is not None)
        else:
            hit = needle in text_blob(datamodel, record, key).casefold()
        return hit if operand == "CONTAINS" else not hit
    if ftype in {"DATE", "DATE_TIME"} and not sub:
        moment = _as_moment(value)
        if operand in {"IS_IN_PAST", "IS_IN_FUTURE", "IS_TODAY", "IS_RELATIVE", "IS_BEFORE", "IS_AFTER", "IS", "IS_NOT", "GREATER_THAN_OR_EQUAL", "LESS_THAN_OR_EQUAL"}:
            return _date_condition(operand, moment, expected, context, date_only=ftype == "DATE")
    if operand in {"IS", "IS_NOT"}:
        expected_values = expected if isinstance(expected, list) else [expected]
        if isinstance(value, list):
            hit = any(_equal(item, wanted) for item in value for wanted in expected_values)
        else:
            hit = any(_equal(value, wanted) for wanted in expected_values)
        return hit if operand == "IS" else not hit
    if operand in {"GREATER_THAN_OR_EQUAL", "LESS_THAN_OR_EQUAL"}:
        left, right = _as_number(value), _as_number(expected)
        if left is None or right is None:
            return False
        return left >= right if operand == "GREATER_THAN_OR_EQUAL" else left <= right
    if operand in {"IS_BEFORE", "IS_AFTER"}:
        return _date_condition(operand, _as_moment(value), expected, context, date_only=False)
    return False


def _equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return str(left).lower() == str(right).lower()
    left_number, right_number = _as_number(left), _as_number(right)
    if left_number is not None and right_number is not None and not (isinstance(left, str) and isinstance(right, str)):
        return left_number == right_number
    if isinstance(right, str):
        parsed = parse_record_link(right)
        if parsed:
            right = parsed[1]
    return str(left).casefold() == str(right).casefold()


def _date_condition(operand: str, moment: Optional[datetime], expected: Any, context: Context, *, date_only: bool) -> bool:
    if moment is None:
        return False
    local_day = moment.date() if date_only else context.local(moment).date()
    today = context.today()
    if operand == "IS_IN_PAST":
        return local_day < today if date_only else moment < context.now
    if operand == "IS_IN_FUTURE":
        return local_day > today if date_only else moment > context.now
    if operand == "IS_TODAY":
        return local_day == today
    if operand == "IS_RELATIVE":
        start, end = relative_range(str(expected or ""), context)
        return start <= local_day <= end
    expected_moment = _as_moment(expected) if not isinstance(expected, datetime) else expected
    if expected_moment is None:
        return False
    expected_day = expected_moment.date() if (isinstance(expected, str) and len(expected) == 10) else context.local(expected_moment).date()
    if operand == "IS":
        return local_day == expected_day
    if operand == "IS_NOT":
        return local_day != expected_day
    if operand in {"IS_BEFORE"}:
        return local_day < expected_day
    if operand in {"IS_AFTER"}:
        return local_day > expected_day
    if operand == "GREATER_THAN_OR_EQUAL":
        return local_day >= expected_day
    if operand == "LESS_THAN_OR_EQUAL":
        return local_day <= expected_day
    return False


def relative_range(value: str, context: Context) -> tuple[date, date]:
    match = RELATIVE_RE.fullmatch(value.upper())
    if not match:
        raise FilterError(f"invalid relative date {value!r}")
    direction, amount_text, unit = match.groups()
    amount = int(amount_text)
    today = context.today()
    if direction == "THIS":
        return period_bounds(today, unit, context)
    if unit == "DAY":
        delta = timedelta(days=amount)
        return (today - delta, today - timedelta(days=1)) if direction == "PAST" else (today + timedelta(days=1), today + delta)
    if unit == "WEEK":
        delta = timedelta(days=7 * amount)
        return (today - delta, today - timedelta(days=1)) if direction == "PAST" else (today + timedelta(days=1), today + delta)
    months = {"MONTH": 1, "QUARTER": 3, "YEAR": 12}[unit] * amount
    if direction == "PAST":
        return add_months(today, -months), today - timedelta(days=1)
    return today + timedelta(days=1), add_months(today, months)


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    for candidate in (day.day, 30, 29, 28):
        try:
            return date(year, month, min(day.day, candidate))
        except ValueError:
            continue
    return date(year, month, 1)


def period_bounds(day: date, unit: str, context: Context) -> tuple[date, date]:
    if unit == "DAY":
        return day, day
    if unit == "WEEK":
        start = day - timedelta(days=(day.isoweekday() - context.week_start) % 7)
        return start, start + timedelta(days=6)
    if unit == "MONTH":
        start = day.replace(day=1)
        return start, add_months(start, 1) - timedelta(days=1)
    if unit == "QUARTER":
        start = date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)
        return start, add_months(start, 3) - timedelta(days=1)
    start = date(day.year, 1, 1)
    return start, date(day.year, 12, 31)


# ---------------------------------------------------------------------------
# sorting, grouping, aggregates


def sort_records(datamodel: DataModel, records: list[Record], sorts: Optional[list[dict[str, Any]]]) -> list[Record]:
    ordered = list(records)
    for sort in reversed(sorts or []):
        key = sort.get("field")
        descending = str(sort.get("direction", "asc")).lower() == "desc"
        object_name = ordered[0].object if ordered else None

        def sort_key(record: Record, key=key):
            if key and object_name and key not in SYSTEM_FIELDS and field_type(datamodel, object_name, key)[0] in {"RELATION", "MORPH_RELATION"}:
                # Links sort by the linked record's title, as people read them, not by UUID.
                label = display_value(datamodel, record, key)
                return (1, "") if not label else (0, label.casefold())
            value = raw_value(datamodel, record, key)
            if isinstance(value, list):
                value = value[0] if value else None
            if value is None or value == "":
                return (1, "")
            number = _as_number(value) if not isinstance(value, str) or value in RATING_VALUES else None
            if number is not None:
                return (0, number)
            if key and object_name:
                ftype, base, _sub = field_type(datamodel, object_name, key)
                if ftype == "SELECT":
                    options = [option["value"] for option in datamodel.fields(object_name)[base].get("options", [])]
                    if value in options:
                        return (0, Decimal(options.index(value)))
            return (0, str(value).casefold())

        present = [record for record in ordered if sort_key(record)[0] == 0]
        missing = [record for record in ordered if sort_key(record)[0] == 1]
        present.sort(key=sort_key, reverse=descending)
        ordered = present + missing
    return ordered


def group_key(datamodel: DataModel, record: Record, key: str, granularity: str = "NONE", context: Optional[Context] = None) -> list[str]:
    """Group labels for a record; relations and multi-selects may yield several groups."""
    ftype, _base, _sub = field_type(datamodel, record.object, key)
    value = raw_value(datamodel, record, key)
    if ftype in {"DATE", "DATE_TIME"} and granularity not in ("", "NONE"):
        moment = _as_moment(value)
        if moment is None:
            return [""]
        day = moment.date() if ftype == "DATE" or context is None else context.local(moment).date()
        return [date_bucket(day, granularity)]
    if isinstance(value, list):
        return [str(item) for item in value] or [""]
    if value is None:
        return [""]
    if isinstance(value, bool):
        return ["true" if value else "false"]
    return [str(value)]


def date_bucket(day: date, granularity: str) -> str:
    granularity = granularity.upper()
    if granularity == "DAY":
        return day.isoformat()
    if granularity == "WEEK":
        iso = day.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if granularity == "MONTH":
        return f"{day.year}-{day.month:02d}"
    if granularity == "QUARTER":
        return f"{day.year}-Q{(day.month - 1) // 3 + 1}"
    if granularity == "YEAR":
        return str(day.year)
    if granularity == "DAY_OF_WEEK":
        return str(day.isoweekday())
    if granularity == "MONTH_OF_YEAR":
        return f"{day.month:02d}"
    if granularity == "QUARTER_OF_YEAR":
        return f"Q{(day.month - 1) // 3 + 1}"
    raise FilterError(f"unknown date granularity {granularity}")


def aggregate(datamodel: DataModel, records: Iterable[Record], function: str, key: Optional[str] = None) -> Optional[Decimal]:
    """One aggregate over records. Counts and SUM of no values are 0; AVG, MIN, MAX and the percentages have no value then."""
    function = function.upper()
    if function not in AGGREGATES:
        raise FilterError(f"unknown aggregate {function}")
    records = list(records)
    if function == "COUNT":
        return Decimal(len(records))
    if not key:
        raise FilterError(f"{function} needs a field")
    values = [raw_value(datamodel, record, key) for record in records]
    if function == "COUNT_EMPTY":
        return Decimal(sum(1 for value in values if is_empty_value(value)))
    if function == "COUNT_NOT_EMPTY":
        return Decimal(sum(1 for value in values if not is_empty_value(value)))
    if function == "PERCENTAGE_EMPTY":
        return (Decimal(sum(1 for value in values if is_empty_value(value))) * 100 / len(values)) if values else None
    if function == "PERCENTAGE_NOT_EMPTY":
        return (Decimal(sum(1 for value in values if not is_empty_value(value))) * 100 / len(values)) if values else None
    if function == "COUNT_TRUE":
        return Decimal(sum(1 for value in values if value is True))
    if function == "COUNT_FALSE":
        return Decimal(sum(1 for value in values if value is False))
    if function == "COUNT_UNIQUE_VALUES":
        flat = set()
        for value in values:
            for item in (value if isinstance(value, list) else [value]):
                if not is_empty_value(item):
                    flat.add(str(item).casefold())
        return Decimal(len(flat))
    numbers = [number for number in (_as_number(value) for value in values) if number is not None]
    if function == "SUM":
        # An empty sum is 0, as in a group without records, never "no value".
        return sum(numbers, Decimal(0))
    if not numbers:
        return None
    if function == "AVG":
        return sum(numbers, Decimal(0)) / len(numbers)
    if function == "MIN":
        return min(numbers)
    return max(numbers)


def default_currency(datamodel: DataModel, object_name: str, key: Optional[str], workspace_default: Any = "") -> str:
    """Currency of an amount that names none: the CURRENCY field's default, else the workspace default.

    An empty sum is shown in it ("0,00 EUR"), the currency a new amount without code gets as well.
    Fields of other types have no currency ("").
    """
    if not key or key in SYSTEM_FIELDS or object_name not in datamodel.objects:
        return ""
    definition = datamodel.fields(object_name).get(key.partition(".")[0])
    if not isinstance(definition, dict) or definition.get("type") != "CURRENCY":
        return ""
    default = definition.get("default")
    code = default.get("currencyCode") if isinstance(default, dict) else None
    return str(code or workspace_default or "")


def currency_codes(datamodel: DataModel, records: Iterable[Record], key: str) -> set[str]:
    """Currencies present in a CURRENCY field; sums across several currencies are refused by callers."""
    if not records:
        return set()
    records = list(records)
    if not records:
        return set()
    ftype, base, _sub = field_type(datamodel, records[0].object, key)
    if ftype != "CURRENCY":
        return set()
    return {str(record.data.get(f"{base}.currencyCode") or "") for record in records if record.data.get(f"{base}.amountMicros") is not None}


def select_records(
    datamodel: DataModel,
    records: Iterable[Record],
    *,
    filter_spec: Any = None,
    sorts: Optional[list[dict[str, Any]]] = None,
    context: Optional[Context] = None,
    include_deleted: bool = False,
    limit: Optional[int] = None,
    offset: int = 0,
) -> list[Record]:
    context = context or Context()
    group = normalize_filter(filter_spec)
    chosen = [
        record for record in records
        if (include_deleted or not record.deleted) and (group is None or _match(datamodel, record, group, context))
    ]
    chosen = sort_records(datamodel, chosen, sorts)
    if offset:
        chosen = chosen[offset:]
    if limit is not None:
        chosen = chosen[:limit]
    return chosen


def display_value(datamodel: DataModel, record: Record, key: str, number_format: str = "COMMAS_AND_DOT") -> str:
    """Readable text for one field, used by views, exports and answers."""
    ftype, base, sub = field_type(datamodel, record.object, key)
    data = record.data
    if key in SYSTEM_FIELDS:
        return str(data.get(key) or "")
    if ftype == "CURRENCY" and not sub:
        micros = data.get(f"{base}.amountMicros")
        if micros is None:
            return ""
        return f"{format_number(Decimal(micros) / Decimal(1_000_000), number_format, 2)} {data.get(f'{base}.currencyCode') or ''}".strip()
    if ftype == "ADDRESS" and not sub:
        parts = [data.get(f"{base}.{part}") for part in ("addressStreet1", "addressStreet2", "addressPostcode", "addressCity", "addressState", "addressCountry")]
        return ", ".join(str(part) for part in parts if part)
    if ftype == "PHONES" and not sub:
        number = data.get(f"{base}.primaryPhoneNumber")
        if not number:
            return ""
        return f"{data.get(f'{base}.primaryPhoneCallingCode') or ''} {number}".strip()
    if ftype in {"RELATION", "MORPH_RELATION"}:
        value = data.get(base)
        values = value if isinstance(value, list) else ([value] if value else [])
        labels = []
        for item in values:
            match = re.match(r"^\[\[[^|\]]+\|([^\]]*)\]\]$", str(item))
            labels.append(match.group(1) if match else str(item))
        return ", ".join(labels)
    if ftype in {"SELECT", "MULTI_SELECT"}:
        options = {option["value"]: option.get("label", option["value"]) for option in datamodel.fields(record.object)[base].get("options", [])}
        value = data.get(base)
        values = value if isinstance(value, list) else ([value] if value else [])
        return ", ".join(options.get(item, item) for item in values)
    if ftype == "RATING":
        value = data.get(base)
        return "★" * (RATING_VALUES.index(value) + 1) if value in RATING_VALUES else ""
    if ftype == "BOOLEAN":
        value = data.get(base)
        return "" if value is None else ("✓" if value else "✗")
    value = raw_value(datamodel, record, key)
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, Decimal):
        return format_number(value, number_format)
    if ftype in {"NUMBER", "NUMERIC"} and value not in (None, "") and not isinstance(value, bool):
        number = _as_number(value)
        if number is not None:
            return format_number(number, number_format)
    return "" if value is None else str(value)


def format_number(value: Decimal, number_format: str = "COMMAS_AND_DOT", places: Optional[int] = None) -> str:
    if places is not None:
        quantized = value.quantize(Decimal(1).scaleb(-places))
        text = f"{quantized:,.{places}f}"
    else:
        normalized = value.normalize()
        text = f"{normalized:,f}" if normalized == normalized.to_integral() else f"{normalized:,}"
    if number_format == "DOTS_AND_COMMA":
        return text.replace(",", "\u0000").replace(".", ",").replace("\u0000", ".")
    if number_format == "SPACES_AND_COMMA":
        return text.replace(",", " ").replace(".", ",")
    if number_format == "APOSTROPHE_AND_DOT":
        return text.replace(",", "'")
    return text
