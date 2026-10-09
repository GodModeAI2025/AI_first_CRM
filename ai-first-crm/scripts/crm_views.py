#!/usr/bin/env python3
"""Views and dashboards of the CRM layer: definitions, validation, computation.

Views live in schema/crm/views.json (lmwiki-crm-views/1) and follow common CRM conventions's
table, kanban and calendar views; dashboards live in schema/crm/dashboards.json
(lmwiki-crm-dashboards/1) and use widgets on a 12-column grid.
Validation checks every definition against the data model and the shared
filter engine. The compute functions return plain data (Decimal values, labels,
local dates) that crm_build.py renders and that queries reuse, so a number in a
view, a dashboard and an answer is always the same number: every aggregate is
crm_filters.aggregate over the same records, and amounts in different
currencies are never added up, they are reported per currency. A count or sum
over no records is 0; an empty sum of amounts is shown in the field's default
currency, else in the workspace currency of the settings.

A generated view freezes one moment and one time zone. Relative filters such as
THIS_1_MONTH are evaluated at the build moment, calendar days are taken in the
time zone of schema/crm/settings.json, and both are shown on every page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional

import crm_filters
from crm_contract import (
    COMPOSITE_SUBFIELDS, DASHBOARDS_PATH, RATING_VALUES, ROLES_PATH, SETTINGS_PATH, UUID_RE, VIEWS_PATH, DataModel,
    Record, RecordStore, format_instant, load_json_file, parse_instant, parse_record_link, read_events,
)
from crm_filters import AGGREGATES, SYSTEM_FIELDS, Context, FilterError

VIEWS_FORMAT = "lmwiki-crm-views/1"
DASHBOARDS_FORMAT = "lmwiki-crm-dashboards/1"
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

VIEW_TYPES = {"table": "table", "kanban": "kanban", "calendar": "calendar"}
WIDGET_TYPES = {
    "number": "number", "bar": "bar", "line": "line", "pie": "pie", "table": "table", "richtext": "richtext",
    "link": "link", "aggregate_chart": "number", "bar_chart": "bar", "line_chart": "line", "pie_chart": "pie",
    "record_table": "table", "standalone_rich_text": "richtext", "iframe": "link",
}
VIEW_KEYS = {
    "id", "object", "type", "label", "description", "icon", "fields", "filter", "sort", "groupBy", "dateGranularity",
    "hideEmptyGroups", "aggregate", "aggregates", "probabilities", "expectedAmountField", "dateField",
    "calendarMode", "visibility", "roles", "position", "me", "compact",
}
DASHBOARD_KEYS = {"id", "label", "description", "icon", "widgets", "position", "visibility", "roles"}
WIDGET_KEYS = {
    "id", "type", "label", "description", "object", "filter", "aggregate", "groupBy", "secondaryGroupBy",
    "dateGranularity", "secondaryDateGranularity", "cumulative", "orderBy", "limit", "prefix", "suffix", "view",
    "text", "url", "layout", "orientation", "stacked", "donut", "omitNullValues", "me",
}
LAYOUT_KEYS = {"column", "span", "row", "rowSpan"}
GRANULARITY_ALIASES = {
    "DAY_OF_THE_WEEK": "DAY_OF_WEEK", "MONTH_OF_THE_YEAR": "MONTH_OF_YEAR", "QUARTER_OF_THE_YEAR": "QUARTER_OF_YEAR",
}
DATE_GRANULARITIES = {"DAY", "WEEK", "MONTH", "QUARTER", "YEAR", "DAY_OF_WEEK", "MONTH_OF_YEAR", "QUARTER_OF_YEAR"}
GAP_FILLED = {"DAY", "WEEK", "MONTH", "QUARTER", "YEAR"}
ORDER_BY = {
    "value": "value", "value_desc": "value", "value_asc": "value_asc", "label": "label", "label_asc": "label",
    "label_desc": "label_desc", "position": "position", "position_desc": "position_desc",
    "field_asc": "label", "field_desc": "label_desc", "field_position_asc": "position",
    "field_position_desc": "position_desc",
}
# Chart display limits; groups beyond them are reported as not displayed.
CHART_LIMITS = {"bar": 100, "line": 100, "pie": 100}
SECONDARY_LIMIT = 50
# Eight categorical colours; further series fold into "other" instead of inventing colours.
MAX_SERIES = 8
OTHER_KEY = "\u0000other"
MONEY_FUNCTIONS = {"SUM", "AVG", "MIN", "MAX"}
NUMERIC_TYPES = {"NUMBER", "NUMERIC", "CURRENCY", "RATING"}
NOT_GROUPABLE = {"RICH_TEXT", "RAW_JSON", "FILES"}
DATE_TYPES = {"DATE", "DATE_TIME"}
RELATION_TYPES = {"RELATION", "MORPH_RELATION"}
VISIBILITY = {"workspace", "restricted"}
PERSONAL_VISIBILITY = {"private", "unlisted", "personal", "me"}

TEXT = {
    "de": {
        "empty": "Ohne Wert", "other": "Andere", "yes": "Ja", "no": "Nein", "no_currency": "ohne Währung",
        "months": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
                   "November", "Dezember"],
        "months_short": ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez."],
        "weekdays_short": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"],
        "week": "KW {week}/{year}", "and": " und ", "or": " oder ", "not": "nicht ({inner})",
        "percent": "{value} %", "relative": {"PAST": "in den letzten {n} {unit}", "NEXT": "in den nächsten {n} {unit}",
                                             "THIS": "in diesem {unit}"},
        "units": {"DAY": ("Tag", "Tagen"), "WEEK": ("Woche", "Wochen"), "MONTH": ("Monat", "Monaten"),
                  "QUARTER": ("Quartal", "Quartalen"), "YEAR": ("Jahr", "Jahren")},
        "this_units": {"DAY": "Tag", "WEEK": "Woche", "MONTH": "Monat", "QUARTER": "Quartal", "YEAR": "Jahr"},
        "operands": {
            "IS": "ist", "IS_NOT": "ist nicht", "IS_NOT_NULL": "ist gesetzt", "CONTAINS": "enthält",
            "DOES_NOT_CONTAIN": "enthält nicht", "GREATER_THAN_OR_EQUAL": "ist mindestens",
            "LESS_THAN_OR_EQUAL": "ist höchstens", "IS_BEFORE": "liegt vor", "IS_AFTER": "liegt nach",
            "IS_EMPTY": "ist leer", "IS_NOT_EMPTY": "ist nicht leer", "IS_RELATIVE": "liegt",
            "IS_IN_PAST": "liegt in der Vergangenheit", "IS_IN_FUTURE": "liegt in der Zukunft", "IS_TODAY": "ist heute",
        },
        "me": "ich ({name})",
    },
    "en": {
        "empty": "No value", "other": "Other", "yes": "Yes", "no": "No", "no_currency": "no currency",
        "months": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                   "November", "December"],
        "months_short": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        "weekdays_short": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "week": "W{week} {year}", "and": " and ", "or": " or ", "not": "not ({inner})",
        "percent": "{value}%", "relative": {"PAST": "in the past {n} {unit}", "NEXT": "in the next {n} {unit}",
                                            "THIS": "in this {unit}"},
        "units": {"DAY": ("day", "days"), "WEEK": ("week", "weeks"), "MONTH": ("month", "months"),
                  "QUARTER": ("quarter", "quarters"), "YEAR": ("year", "years")},
        "this_units": {"DAY": "day", "WEEK": "week", "MONTH": "month", "QUARTER": "quarter", "YEAR": "year"},
        "operands": {
            "IS": "is", "IS_NOT": "is not", "IS_NOT_NULL": "is set", "CONTAINS": "contains",
            "DOES_NOT_CONTAIN": "does not contain", "GREATER_THAN_OR_EQUAL": "is at least",
            "LESS_THAN_OR_EQUAL": "is at most", "IS_BEFORE": "is before", "IS_AFTER": "is after",
            "IS_EMPTY": "is empty", "IS_NOT_EMPTY": "is not empty", "IS_RELATIVE": "is",
            "IS_IN_PAST": "is in the past", "IS_IN_FUTURE": "is in the future", "IS_TODAY": "is today",
        },
        "me": "me ({name})",
    },
}


def ui_language(language: Optional[str]) -> str:
    return "de" if str(language or "").lower().startswith("de") else "en"


def texts(language: Optional[str]) -> dict[str, Any]:
    return TEXT[ui_language(language)]


# ---------------------------------------------------------------------------
# settings and context


def wiki_language(target: Path) -> str:
    from frontmatter_contract import FrontmatterError, parse_file

    path = target / "schema/WIKI_PROFILE.md"
    if not path.is_file():
        return "en"
    try:
        return str(parse_file(path).data.get("wiki_language") or "en")
    except (FrontmatterError, OSError, UnicodeDecodeError):
        return "en"


def load_settings(target: Path, language: Optional[str] = None) -> dict[str, Any]:
    from crm_standard import standard_settings

    defaults = standard_settings(language or wiki_language(target), "", "EUR")
    loaded = load_json_file(target, SETTINGS_PATH, {})
    settings = dict(defaults)
    if isinstance(loaded, dict):
        settings.update({key: value for key, value in loaded.items() if value not in (None, "")})
    return settings


def load_views(target: Path) -> dict[str, Any]:
    return load_json_file(target, VIEWS_PATH, {"format": VIEWS_FORMAT, "views": []})


def load_dashboards(target: Path) -> dict[str, Any]:
    return load_json_file(target, DASHBOARDS_PATH, {"format": DASHBOARDS_FORMAT, "dashboards": []})


def load_roles(target: Path) -> Optional[dict[str, Any]]:
    roles = load_json_file(target, ROLES_PATH, None)
    return roles if isinstance(roles, dict) else None


def week_start(settings: dict[str, Any]) -> int:
    try:
        value = int(settings.get("calendar_start_day") or 1)
    except (TypeError, ValueError):
        value = 1
    return value if 1 <= value <= 7 else 1


def make_context(settings: dict[str, Any], now: Optional[str] = None, me: Optional[str] = None) -> Context:
    return Context(now=now, time_zone=str(settings.get("time_zone") or "UTC"), me=me, week_start=week_start(settings))


def zone_problem(settings: dict[str, Any], context: Context) -> Optional[str]:
    """A configured zone that silently fell back to UTC would shift every calendar day."""
    name = str(settings.get("time_zone") or "UTC")
    if name not in ("UTC", "Etc/UTC") and context.tz is timezone.utc:
        return f"time zone {name!r} is not available on this system; days and buckets were computed in UTC"
    return None


# ---------------------------------------------------------------------------
# field references


@dataclass(frozen=True)
class FieldRef:
    key: str
    ftype: str
    base: str
    sub: str
    definition: Optional[dict[str, Any]]

    @property
    def system(self) -> bool:
        return self.key in SYSTEM_FIELDS

    @property
    def inverse(self) -> bool:
        return bool(self.definition) and self.ftype == "RELATION" and \
            self.definition.get("relation", {}).get("type") == "ONE_TO_MANY"

    @property
    def relation(self) -> bool:
        return self.ftype in RELATION_TYPES and not self.sub

    @property
    def date(self) -> bool:
        return self.ftype in DATE_TYPES and not self.sub

    @property
    def options(self) -> list[dict[str, Any]]:
        if not self.definition or self.ftype not in {"SELECT", "MULTI_SELECT"} or self.sub:
            return []
        options = [option for option in self.definition.get("options", []) if isinstance(option, dict)]
        return [option for _index, option in sorted(enumerate(options), key=lambda item: (_option_position(item[1], item[0]), item[0]))]


def _option_position(option: dict[str, Any], index: int) -> Any:
    position = option.get("position")
    return position if isinstance(position, (int, float)) and not isinstance(position, bool) else index


def resolve_field(datamodel: DataModel, object_name: str, key: Any) -> FieldRef:
    """Resolve a field key (name, composite subfield or crm_ system key); raise FilterError otherwise."""
    if not isinstance(key, str) or not key:
        raise FilterError(f"field of {object_name} must be a non-empty name")
    if key in SYSTEM_FIELDS:
        return FieldRef(key, SYSTEM_FIELDS[key], key, "", None)
    base, _, sub = key.partition(".")
    definition = datamodel.fields(object_name).get(base)
    if definition is None:
        raise FilterError(f"unknown field {object_name}.{key}")
    ftype = definition.get("type", "")
    if sub:
        allowed = set(COMPOSITE_SUBFIELDS.get(ftype, ()))
        if ftype == "CURRENCY":
            allowed.add("amount")
        if sub not in allowed:
            if ftype in RELATION_TYPES:
                raise FilterError(f"{object_name}.{key}: fields of related records cannot be used; use the relation itself")
            raise FilterError(f"{object_name}.{key}: {base} has no subfield {sub}")
    return FieldRef(key, ftype, base, sub, definition)


def field_label(datamodel: DataModel, object_name: str, key: str, language: Optional[str] = None) -> str:
    system = {
        "de": {"crm_created_at": "Angelegt am", "crm_updated_at": "Geändert am", "crm_deleted_at": "Gelöscht am",
               "crm_created_by": "Angelegt von", "crm_updated_by": "Geändert von", "crm_title": "Titel",
               "crm_id": "ID", "crm_position": "Position", "crm_created_source": "Quelle"},
        "en": {"crm_created_at": "Created", "crm_updated_at": "Last update", "crm_deleted_at": "Deleted",
               "crm_created_by": "Created by", "crm_updated_by": "Updated by", "crm_title": "Title",
               "crm_id": "ID", "crm_position": "Position", "crm_created_source": "Source"},
    }
    if key in SYSTEM_FIELDS:
        return system[ui_language(language)].get(key, key)
    base, _, sub = key.partition(".")
    definition = datamodel.fields(object_name).get(base, {})
    label = str(definition.get("label") or base)
    return f"{label} ({sub})" if sub else label


def object_label(datamodel: DataModel, object_name: str, plural: bool = True) -> str:
    definition = datamodel.objects.get(object_name, {})
    return str(definition.get("labelPlural" if plural else "labelSingular") or object_name)


# ---------------------------------------------------------------------------
# normalization


def normalize_function(value: Any) -> str:
    name = str(value or "").strip().upper()
    return {"AVERAGE": "AVG", "COUNT_ALL": "COUNT", "COUNT_UNIQUE": "COUNT_UNIQUE_VALUES"}.get(name, name)


def normalize_granularity(value: Any) -> str:
    name = str(value or "").strip().upper()
    return GRANULARITY_ALIASES.get(name, name)


def normalize_aggregate(spec: Any) -> Optional[dict[str, Any]]:
    if spec in (None, {}, ""):
        return None
    if isinstance(spec, str):
        return {"function": normalize_function(spec), "field": None}
    if not isinstance(spec, dict):
        return None
    field = spec.get("field")
    return {"function": normalize_function(spec.get("function") or spec.get("operation")), "field": field or None}


def normalize_sorts(spec: Any) -> list[dict[str, str]]:
    if spec in (None, [], {}):
        return []
    items = spec if isinstance(spec, list) else [spec]
    sorts = []
    for item in items:
        if isinstance(item, str):
            sorts.append({"field": item, "direction": "asc"})
        elif isinstance(item, dict):
            sorts.append({"field": item.get("field"), "direction": str(item.get("direction") or "asc").lower()})
    return sorts


def default_fields(datamodel: DataModel, object_name: str, view_type: str, group_by: Optional[str] = None) -> list[str]:
    label_field = datamodel.label_field(object_name)
    stored = [name for name, definition in datamodel.stored_fields(object_name) if definition.get("type") != "RICH_TEXT"
              and definition.get("active", True) is not False]
    if view_type == "table":
        return [label_field] + [name for name in stored if name != label_field]
    if view_type == "kanban":
        return [name for name in stored if name not in (label_field, group_by)][:3]
    return []


def normalize_view(datamodel: DataModel, view: dict[str, Any], index: int = 0) -> dict[str, Any]:
    """Canonical form of one valid view: lowercase type and directions, uppercase functions, defaults filled in."""
    view_type = VIEW_TYPES.get(str(view.get("type") or "table").lower(), "table")
    object_name = view.get("object")
    group_by = view.get("groupBy") or None
    granularity = normalize_granularity(view.get("dateGranularity")) or None
    if group_by and granularity is None:
        try:
            if resolve_field(datamodel, object_name, group_by).date:
                granularity = "DAY"
        except FilterError:
            pass
    aggregates = {}
    for key, function in (view.get("aggregates") or {}).items():
        aggregates[key] = normalize_function(function)
    probabilities = {}
    for key, value in (view.get("probabilities") or {}).items():
        probabilities[str(key)] = to_decimal(value)
    fields = view.get("fields")
    if not isinstance(fields, list):
        fields = default_fields(datamodel, object_name, view_type, group_by)
    aggregate = normalize_aggregate(view.get("aggregate"))
    expected_field = view.get("expectedAmountField")
    if probabilities and not expected_field and aggregate and aggregate.get("field"):
        expected_field = aggregate["field"]
    return {
        "id": view.get("id"),
        "object": object_name,
        "type": view_type,
        "label": str(view.get("label") or view.get("id")),
        "description": str(view.get("description") or ""),
        "fields": list(fields),
        "filter": view.get("filter"),
        "sort": normalize_sorts(view.get("sort")),
        "groupBy": group_by,
        "dateGranularity": granularity,
        "hideEmptyGroups": bool(view.get("hideEmptyGroups")),
        "aggregate": aggregate,
        "aggregates": aggregates,
        "probabilities": probabilities,
        "expectedAmountField": expected_field or None,
        "dateField": view.get("dateField"),
        "calendarMode": str(view.get("calendarMode") or "month").lower(),
        "visibility": str(view.get("visibility") or "workspace").lower(),
        "roles": [str(role) for role in view.get("roles") or []],
        "position": view.get("position") if isinstance(view.get("position"), (int, float)) else index,
        "me": _record_id(view.get("me")),
    }


def normalize_views(datamodel: DataModel, views_doc: Any) -> list[dict[str, Any]]:
    views = views_doc.get("views", []) if isinstance(views_doc, dict) else []
    normalized = [normalize_view(datamodel, view, index) for index, view in enumerate(views) if isinstance(view, dict)]
    return [view for _index, view in sorted(enumerate(normalized), key=lambda item: (item[1]["position"], item[0]))]


def normalize_widget(datamodel: DataModel, widget: dict[str, Any], index: int = 0) -> dict[str, Any]:
    widget_type = WIDGET_TYPES.get(str(widget.get("type") or "").lower(), str(widget.get("type") or "").lower())
    aggregate = normalize_aggregate(widget.get("aggregate")) or {"function": "COUNT", "field": None}
    group_by = widget.get("groupBy") or None
    granularity = normalize_granularity(widget.get("dateGranularity")) or None
    secondary = widget.get("secondaryGroupBy") or None
    secondary_granularity = normalize_granularity(widget.get("secondaryDateGranularity")) or None
    object_name = widget.get("object")
    for key, current, setter in ((group_by, granularity, "primary"), (secondary, secondary_granularity, "secondary")):
        if key and current is None and object_name in datamodel.objects:
            try:
                if resolve_field(datamodel, object_name, key).date:
                    if setter == "primary":
                        granularity = "DAY"
                    else:
                        secondary_granularity = "DAY"
            except FilterError:
                pass
    layout = widget.get("layout") if isinstance(widget.get("layout"), dict) else {}
    default_span = {"number": 3, "link": 3, "richtext": 6, "table": 12}.get(widget_type, 6)
    order_by = ORDER_BY.get(str(widget.get("orderBy") or "").lower()) if widget.get("orderBy") else None
    return {
        "id": widget.get("id") or f"widget-{index + 1}",
        "type": widget_type,
        "label": str(widget.get("label") or ""),
        "description": str(widget.get("description") or ""),
        "object": object_name,
        "filter": widget.get("filter"),
        "aggregate": aggregate,
        "groupBy": group_by,
        "dateGranularity": granularity,
        "secondaryGroupBy": secondary,
        "secondaryDateGranularity": secondary_granularity,
        "cumulative": bool(widget.get("cumulative")),
        "orderBy": order_by,
        "limit": widget.get("limit") if isinstance(widget.get("limit"), int) and not isinstance(widget.get("limit"), bool) else None,
        "prefix": str(widget.get("prefix") or ""),
        "suffix": str(widget.get("suffix") or ""),
        "view": widget.get("view"),
        "text": str(widget.get("text") or ""),
        "url": str(widget.get("url") or ""),
        "orientation": str(widget.get("orientation") or "vertical").lower(),
        "stacked": widget.get("stacked") is not False,
        "donut": widget.get("donut") is not False,
        "omitNullValues": bool(widget.get("omitNullValues")),
        "me": _record_id(widget.get("me")),
        "layout": {
            "column": layout.get("column") if isinstance(layout.get("column"), int) else None,
            "span": layout.get("span") if isinstance(layout.get("span"), int) else default_span,
            "row": layout.get("row") if isinstance(layout.get("row"), int) else None,
            "rowSpan": layout.get("rowSpan") if isinstance(layout.get("rowSpan"), int) else 1,
        },
    }


def normalize_dashboards(datamodel: DataModel, dashboards_doc: Any) -> list[dict[str, Any]]:
    dashboards = dashboards_doc.get("dashboards", []) if isinstance(dashboards_doc, dict) else []
    result = []
    for index, dashboard in enumerate(dashboards):
        if not isinstance(dashboard, dict):
            continue
        result.append({
            "id": dashboard.get("id"),
            "label": str(dashboard.get("label") or dashboard.get("id")),
            "description": str(dashboard.get("description") or ""),
            "position": dashboard.get("position") if isinstance(dashboard.get("position"), (int, float)) else index,
            "visibility": str(dashboard.get("visibility") or "workspace").lower(),
            "roles": [str(role) for role in dashboard.get("roles") or []],
            "widgets": [normalize_widget(datamodel, widget, number) for number, widget in enumerate(dashboard.get("widgets") or [])
                        if isinstance(widget, dict)],
        })
    return [item for _index, item in sorted(enumerate(result), key=lambda pair: (pair[1]["position"], pair[0]))]


def to_decimal(value: Any) -> Optional[Decimal]:
    """Exact decimal of a number or numeric text; None for empty, boolean, non-numeric or infinite values."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _record_id(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = parse_record_link(value)
    if parsed:
        return parsed[1]
    text = value.strip().lower()
    if ":" in text:
        text = text.split(":", 1)[1]
    return text


# ---------------------------------------------------------------------------
# validation


def _check_filter(datamodel: DataModel, object_name: str, spec: Any, where: str, me: Any) -> list[str]:
    errors = [f"{where}: filter: {problem}" for problem in crm_filters.validate_filter(datamodel, object_name, spec)]
    if errors:
        return errors
    try:
        group = crm_filters.normalize_filter(spec)
    except FilterError as exc:
        return [f"{where}: filter: {exc}"]

    def walk(node: Optional[dict[str, Any]]) -> None:
        if node is None:
            return
        if "conditions" in node:
            for child in node["conditions"]:
                walk(child)
            return
        try:
            ref = resolve_field(datamodel, object_name, node["field"])
        except FilterError as exc:
            errors.append(f"{where}: filter: {exc}")
            return
        if ref.inverse:
            target = ref.definition["relation"]
            errors.append(
                f"{where}: filter on {object_name}.{ref.base} is not possible: it is the computed inverse side; "
                f"filter {target.get('target')} by {target.get('inverse')} instead"
            )
        values = node.get("value") if isinstance(node.get("value"), list) else [node.get("value")]
        if "@me" in values and not me:
            errors.append(
                f"{where}: filter uses @me, but a generated page has no signed-in viewer; "
                "bind it to a named member with \"me\": \"<workspaceMember id>\""
            )

    walk(group)
    return errors


def _check_aggregate(datamodel: DataModel, object_name: str, spec: Any, where: str) -> list[str]:
    aggregate = normalize_aggregate(spec)
    if aggregate is None:
        return [f"{where}: aggregate must be {{\"function\": ..., \"field\": ...}}"]
    function, key = aggregate["function"], aggregate["field"]
    if function not in AGGREGATES:
        return [f"{where}: unknown aggregate function {function!r} (one of {', '.join(sorted(AGGREGATES))})"]
    if function == "COUNT":
        return []
    if not key:
        return [f"{where}: {function} needs a field"]
    try:
        ref = resolve_field(datamodel, object_name, key)
    except FilterError as exc:
        return [f"{where}: {exc}"]
    if ref.inverse:
        return [f"{where}: {object_name}.{key} is a computed inverse relation and cannot be aggregated"]
    if function in MONEY_FUNCTIONS:
        numeric = ref.ftype in NUMERIC_TYPES and (not ref.sub or ref.sub == "amount") or ref.key == "crm_position"
        if not numeric:
            return [f"{where}: {function} needs a NUMBER, NUMERIC, CURRENCY or RATING field, not {key} ({ref.ftype})"]
    if function in {"COUNT_TRUE", "COUNT_FALSE"} and ref.ftype != "BOOLEAN":
        return [f"{where}: {function} needs a BOOLEAN field, not {key} ({ref.ftype})"]
    return []


def _check_groupable(datamodel: DataModel, object_name: str, key: Any, where: str, what: str) -> list[str]:
    try:
        ref = resolve_field(datamodel, object_name, key)
    except FilterError as exc:
        return [f"{where}: {what}: {exc}"]
    if ref.inverse:
        return [f"{where}: {what} {key} is a computed inverse relation; group the other object by its relation instead"]
    if ref.ftype in NOT_GROUPABLE:
        return [f"{where}: {what} {key} ({ref.ftype}) cannot be grouped"]
    return []


def _check_granularity(datamodel: DataModel, object_name: str, key: Any, value: Any, where: str) -> list[str]:
    if value in (None, ""):
        return []
    granularity = normalize_granularity(value)
    if granularity not in DATE_GRANULARITIES and granularity != "NONE":
        return [f"{where}: unknown date granularity {value!r} (one of {', '.join(sorted(DATE_GRANULARITIES))})"]
    try:
        ref = resolve_field(datamodel, object_name, key) if key else None
    except FilterError:
        return []
    if ref is None or not ref.date:
        return [f"{where}: a date granularity needs a DATE or DATE_TIME grouping field"]
    return []


def _check_role_list(value: Any, where: str, roles: Optional[dict[str, Any]]) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(role, str) and role for role in value):
        return [f"{where}: visibility 'restricted' needs a non-empty list 'roles'"]
    if roles and isinstance(roles.get("roles"), dict):
        unknown = [role for role in value if role not in roles["roles"]]
        if unknown:
            return [f"{where}: unknown roles {unknown} (defined in {ROLES_PATH}: {sorted(roles['roles'])})"]
    return []


def _check_visibility(item: dict[str, Any], where: str, roles: Optional[dict[str, Any]]) -> list[str]:
    visibility = str(item.get("visibility") or "workspace").lower()
    if visibility in PERSONAL_VISIBILITY:
        return [
            f"{where}: visibility {item.get('visibility')!r} (only for me) cannot be represented in a released wiki: "
            "every file under graph/crm/ is part of the release and of every export, so all readers would see it; "
            "use 'workspace', or 'restricted' with roles, or keep the personal view outside the wiki"
        ]
    if visibility not in VISIBILITY:
        return [f"{where}: visibility must be 'workspace' or 'restricted', not {item.get('visibility')!r}"]
    if visibility == "restricted":
        return _check_role_list(item.get("roles"), where, roles)
    return []


def validate_views(datamodel: DataModel, views_doc: Any, *, roles: Optional[dict[str, Any]] = None) -> list[str]:
    """Check schema/crm/views.json against the data model and the filter engine."""
    if not isinstance(views_doc, dict) or views_doc.get("format") != VIEWS_FORMAT:
        return [f"{VIEWS_PATH}: format must be {VIEWS_FORMAT}"]
    views = views_doc.get("views")
    if not isinstance(views, list):
        return [f"{VIEWS_PATH}: views must be a list"]
    errors: list[str] = []
    seen: set[str] = set()
    for index, view in enumerate(views):
        where = f"{VIEWS_PATH}: view {index + 1}"
        if not isinstance(view, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        view_id = view.get("id")
        if not isinstance(view_id, str) or not ID_RE.fullmatch(view_id):
            errors.append(f"{where}: id must be kebab-case such as 'open-deals', not {view_id!r}")
        else:
            where = f"{VIEWS_PATH}: view {view_id}"
            if view_id in seen:
                errors.append(f"{where}: duplicate id")
            seen.add(view_id)
        errors.extend(_check_view(datamodel, view, where, roles))
    return errors


def _check_view(datamodel: DataModel, view: dict[str, Any], where: str, roles: Optional[dict[str, Any]]) -> list[str]:
    errors = [f"{where}: unknown key {key!r}" for key in sorted(set(view) - VIEW_KEYS)]
    object_name = view.get("object")
    if object_name not in datamodel.objects:
        return errors + [f"{where}: unknown object {object_name!r}"]
    view_type = str(view.get("type") or "").lower()
    if view_type not in VIEW_TYPES:
        errors.append(f"{where}: type must be table, kanban or calendar, not {view.get('type')!r}")
        view_type = "table"
    if not isinstance(view.get("label"), str) or not view["label"].strip():
        errors.append(f"{where}: label is required")
    fields = view.get("fields")
    if fields is not None:
        if not isinstance(fields, list):
            errors.append(f"{where}: fields must be a list of field names")
        else:
            seen_fields: set[str] = set()
            for key in fields:
                try:
                    resolve_field(datamodel, object_name, key)
                except FilterError as exc:
                    errors.append(f"{where}: fields: {exc}")
                    continue
                if key in seen_fields:
                    errors.append(f"{where}: fields: {key} is listed twice")
                seen_fields.add(key)
    errors.extend(_check_filter(datamodel, object_name, view.get("filter"), where, view.get("me")))
    if view.get("me") is not None and (_record_id(view.get("me")) is None or not UUID_RE.fullmatch(_record_id(view.get("me")) or "")):
        errors.append(f"{where}: me must be the id or record link of a workspaceMember")
    sort = view.get("sort")
    if sort is not None and not isinstance(sort, (list, dict, str)):
        errors.append(f"{where}: sort must be a list of {{field, direction}}")
    for item in normalize_sorts(sort):
        try:
            ref = resolve_field(datamodel, object_name, item["field"])
            if ref.inverse or ref.ftype in {"RICH_TEXT", "RAW_JSON", "FILES"}:
                errors.append(f"{where}: sort: {item['field']} ({ref.ftype}) cannot be sorted")
        except FilterError as exc:
            errors.append(f"{where}: sort: {exc}")
        if item["direction"] not in ("asc", "desc"):
            errors.append(f"{where}: sort direction must be asc or desc, not {item['direction']!r}")
    for flag in ("hideEmptyGroups", "compact"):
        if flag in view and not isinstance(view[flag], bool):
            errors.append(f"{where}: {flag} must be true or false")
    if "position" in view and (isinstance(view["position"], bool) or not isinstance(view["position"], (int, float))):
        errors.append(f"{where}: position must be a number")
    errors.extend(_check_visibility(view, where, roles))
    group_by = view.get("groupBy")
    if view_type == "kanban":
        if not group_by:
            errors.append(f"{where}: a kanban view needs groupBy with a SELECT field")
        else:
            try:
                ref = resolve_field(datamodel, object_name, group_by)
                if ref.ftype != "SELECT" or ref.sub:
                    errors.append(
                        f"{where}: kanban groupBy must be a SELECT field (columns are its options), "
                        f"not {group_by} ({ref.ftype})"
                    )
            except FilterError as exc:
                errors.append(f"{where}: groupBy: {exc}")
        if view.get("aggregate") not in (None, {}):
            errors.extend(_check_aggregate(datamodel, object_name, view.get("aggregate"), f"{where}: aggregate"))
        errors.extend(_check_probabilities(datamodel, view, where))
    elif group_by:
        if view_type == "calendar":
            errors.append(f"{where}: groupBy is not available for calendar views")
        else:
            errors.extend(_check_groupable(datamodel, object_name, group_by, where, "groupBy"))
            errors.extend(_check_granularity(datamodel, object_name, group_by, view.get("dateGranularity"), where))
    if view_type != "kanban":
        for key in ("aggregate", "probabilities", "expectedAmountField"):
            if view.get(key) not in (None, {}, ""):
                errors.append(f"{where}: {key} is only available for kanban views")
    if view_type != "calendar":
        for key in ("dateField", "calendarMode"):
            if view.get(key) not in (None, ""):
                errors.append(f"{where}: {key} is only available for calendar views")
    else:
        date_field = view.get("dateField")
        if not date_field:
            errors.append(f"{where}: a calendar view needs dateField (a DATE or DATE_TIME field)")
        else:
            try:
                ref = resolve_field(datamodel, object_name, date_field)
                if not ref.date:
                    errors.append(f"{where}: dateField must be a DATE or DATE_TIME field, not {date_field} ({ref.ftype})")
            except FilterError as exc:
                errors.append(f"{where}: dateField: {exc}")
        mode = str(view.get("calendarMode") or "month").lower()
        if mode not in ("month", "week"):
            errors.append(f"{where}: calendarMode must be month or week, not {view.get('calendarMode')!r}")
    aggregates = view.get("aggregates")
    if aggregates not in (None, {}):
        if view_type != "table" or not isinstance(aggregates, dict):
            errors.append(f"{where}: aggregates ({{field: FUNCTION}}) are only available for table views")
        else:
            for key, function in aggregates.items():
                errors.extend(_check_aggregate(datamodel, object_name, {"function": function, "field": key}, f"{where}: aggregates.{key}"))
    return errors


def _check_probabilities(datamodel: DataModel, view: dict[str, Any], where: str) -> list[str]:
    probabilities = view.get("probabilities")
    if probabilities in (None, {}):
        if view.get("expectedAmountField"):
            return [f"{where}: expectedAmountField needs probabilities"]
        return []
    if not isinstance(probabilities, dict):
        return [f"{where}: probabilities must map option values to shares between 0 and 1"]
    errors = []
    object_name = view["object"]
    try:
        options = [option.get("value") for option in resolve_field(datamodel, object_name, view.get("groupBy")).options]
    except FilterError:
        options = []
    for key, value in probabilities.items():
        if options and key not in options:
            errors.append(f"{where}: probabilities: {key!r} is not an option of {view.get('groupBy')} ({', '.join(options)})")
        number = to_decimal(value)
        if number is None or isinstance(value, bool):
            errors.append(f"{where}: probabilities.{key} must be a number between 0 and 1")
        elif number < 0 or number > 1:
            errors.append(
                f"{where}: probabilities.{key} is {value}; give the share between 0 and 1 (0.6 for 60 %), "
                "so the expected amount is amount times share"
            )
    amount_field = view.get("expectedAmountField") or (normalize_aggregate(view.get("aggregate")) or {}).get("field")
    if not amount_field:
        errors.append(f"{where}: probabilities need an amount: set expectedAmountField or an aggregate on an amount field")
    else:
        try:
            ref = resolve_field(datamodel, object_name, amount_field)
            if ref.ftype not in {"CURRENCY", "NUMBER", "NUMERIC"} or (ref.sub and ref.sub != "amount"):
                errors.append(f"{where}: the expected amount needs a CURRENCY, NUMBER or NUMERIC field, not {amount_field} ({ref.ftype})")
        except FilterError as exc:
            errors.append(f"{where}: expectedAmountField: {exc}")
    return errors


def validate_dashboards(datamodel: DataModel, dashboards_doc: Any, views_doc: Any = None, *, roles: Optional[dict[str, Any]] = None) -> list[str]:
    """Check schema/crm/dashboards.json; table widgets must name a view of views_doc."""
    if not isinstance(dashboards_doc, dict) or dashboards_doc.get("format") != DASHBOARDS_FORMAT:
        return [f"{DASHBOARDS_PATH}: format must be {DASHBOARDS_FORMAT}"]
    dashboards = dashboards_doc.get("dashboards")
    if not isinstance(dashboards, list):
        return [f"{DASHBOARDS_PATH}: dashboards must be a list"]
    view_ids = {
        view.get("id"): view for view in (views_doc.get("views", []) if isinstance(views_doc, dict) else [])
        if isinstance(view, dict)
    }
    errors: list[str] = []
    seen: set[str] = set()
    for index, dashboard in enumerate(dashboards):
        where = f"{DASHBOARDS_PATH}: dashboard {index + 1}"
        if not isinstance(dashboard, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        dashboard_id = dashboard.get("id")
        if not isinstance(dashboard_id, str) or not ID_RE.fullmatch(dashboard_id):
            errors.append(f"{where}: id must be kebab-case such as 'sales', not {dashboard_id!r}")
        else:
            where = f"{DASHBOARDS_PATH}: dashboard {dashboard_id}"
            if dashboard_id in seen:
                errors.append(f"{where}: duplicate id")
            seen.add(dashboard_id)
        errors.extend(f"{where}: unknown key {key!r}" for key in sorted(set(dashboard) - DASHBOARD_KEYS))
        if not isinstance(dashboard.get("label"), str) or not dashboard["label"].strip():
            errors.append(f"{where}: label is required")
        if "position" in dashboard and (isinstance(dashboard["position"], bool) or not isinstance(dashboard["position"], (int, float))):
            errors.append(f"{where}: position must be a number")
        errors.extend(_check_visibility(dashboard, where, roles))
        widgets = dashboard.get("widgets")
        if not isinstance(widgets, list):
            errors.append(f"{where}: widgets must be a list")
            continue
        widget_ids: set[str] = set()
        for number, widget in enumerate(widgets):
            widget_where = f"{where}: widget {number + 1}"
            if not isinstance(widget, dict):
                errors.append(f"{widget_where}: must be a mapping")
                continue
            widget_id = widget.get("id")
            if not isinstance(widget_id, str) or not ID_RE.fullmatch(widget_id):
                errors.append(f"{widget_where}: id must be kebab-case, not {widget_id!r}")
            else:
                widget_where = f"{where}: widget {widget_id}"
                if widget_id in widget_ids:
                    errors.append(f"{widget_where}: duplicate id")
                widget_ids.add(widget_id)
            errors.extend(_check_widget(datamodel, widget, widget_where, view_ids))
    return errors


def _check_widget(datamodel: DataModel, widget: dict[str, Any], where: str, views: dict[str, Any]) -> list[str]:
    errors = [f"{where}: unknown key {key!r}" for key in sorted(set(widget) - WIDGET_KEYS)]
    widget_type = WIDGET_TYPES.get(str(widget.get("type") or "").lower())
    if widget_type is None:
        return errors + [f"{where}: type must be number, bar, line, pie, table, richtext or link, not {widget.get('type')!r}"]
    if widget_type not in ("richtext", "link") and (not isinstance(widget.get("label"), str) or not widget["label"].strip()):
        errors.append(f"{where}: label is required")
    errors.extend(_check_layout(widget.get("layout"), where))
    for flag in ("cumulative", "stacked", "donut", "omitNullValues"):
        if flag in widget and not isinstance(widget[flag], bool):
            errors.append(f"{where}: {flag} must be true or false")
    for key in ("prefix", "suffix"):
        if key in widget and not isinstance(widget[key], str):
            errors.append(f"{where}: {key} must be text")
    if "limit" in widget and (isinstance(widget["limit"], bool) or not isinstance(widget["limit"], int) or widget["limit"] < 1):
        errors.append(f"{where}: limit must be a positive whole number")
    if widget_type == "richtext":
        if not isinstance(widget.get("text"), str) or not widget["text"].strip():
            errors.append(f"{where}: a richtext widget needs text (Markdown)")
        return errors
    if widget_type == "link":
        url = widget.get("url")
        if not isinstance(url, str) or not re.match(r"^https?://[^\s<>\"]+$", url.strip()):
            errors.append(f"{where}: a link widget needs an http or https url (an embedded page becomes a link)")
        return errors
    if widget_type == "table":
        view_id = widget.get("view")
        view = views.get(view_id)
        if not view_id:
            errors.append(f"{where}: a table widget needs view (the id of a view in {VIEWS_PATH})")
        elif view is None:
            errors.append(f"{where}: view {view_id!r} does not exist in {VIEWS_PATH}")
        elif widget.get("object") and widget.get("object") != view.get("object"):
            errors.append(f"{where}: object {widget.get('object')!r} differs from the object of view {view_id}")
        return errors
    object_name = widget.get("object")
    if object_name not in datamodel.objects:
        return errors + [f"{where}: unknown object {object_name!r}"]
    errors.extend(_check_filter(datamodel, object_name, widget.get("filter"), where, widget.get("me")))
    errors.extend(_check_aggregate(datamodel, object_name, widget.get("aggregate") or {"function": "COUNT"}, f"{where}: aggregate"))
    if widget_type in ("bar", "line", "pie"):
        group_by = widget.get("groupBy")
        if not group_by:
            errors.append(f"{where}: a {widget_type} chart needs groupBy")
        else:
            errors.extend(_check_groupable(datamodel, object_name, group_by, where, "groupBy"))
            errors.extend(_check_granularity(datamodel, object_name, group_by, widget.get("dateGranularity"), where))
        order_by = widget.get("orderBy")
        if order_by is not None and str(order_by).lower() not in ORDER_BY:
            errors.append(f"{where}: orderBy must be value, value_asc, label, label_desc or position, not {order_by!r}")
    else:
        for key in ("groupBy", "secondaryGroupBy", "dateGranularity", "orderBy", "cumulative"):
            if widget.get(key) not in (None, "", False):
                errors.append(f"{where}: {key} is not available for {widget_type} widgets")
    secondary = widget.get("secondaryGroupBy")
    if secondary:
        if widget_type not in ("bar", "line"):
            errors.append(f"{where}: secondaryGroupBy is only available for bar and line charts")
        else:
            errors.extend(_check_groupable(datamodel, object_name, secondary, where, "secondaryGroupBy"))
            errors.extend(_check_granularity(datamodel, object_name, secondary, widget.get("secondaryDateGranularity"), where))
    if widget.get("orientation") is not None:
        if widget_type != "bar":
            errors.append(f"{where}: orientation is only available for bar charts")
        elif str(widget["orientation"]).lower() not in ("vertical", "horizontal"):
            errors.append(f"{where}: orientation must be vertical or horizontal")
    if "donut" in widget and widget_type != "pie":
        errors.append(f"{where}: donut is only available for pie charts")
    if "cumulative" in widget and widget.get("cumulative") and widget_type not in ("bar", "line"):
        errors.append(f"{where}: cumulative is only available for bar and line charts")
    if widget.get("me") is not None and not UUID_RE.fullmatch(_record_id(widget.get("me")) or ""):
        errors.append(f"{where}: me must be the id or record link of a workspaceMember")
    return errors


def _check_layout(layout: Any, where: str) -> list[str]:
    if layout is None:
        return []
    if not isinstance(layout, dict):
        return [f"{where}: layout must be {{column, span, row}}"]
    errors = [f"{where}: layout: unknown key {key!r}" for key in sorted(set(layout) - LAYOUT_KEYS)]
    values = {}
    for key, low, high in (("column", 0, 11), ("span", 1, 12), ("row", 0, 999), ("rowSpan", 1, 12)):
        if key not in layout:
            continue
        value = layout[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            errors.append(f"{where}: layout.{key} must be a whole number from {low} to {high}")
        else:
            values[key] = value
    if "column" in values and values.get("column", 0) + values.get("span", 1) > 12:
        errors.append(f"{where}: layout column {values['column']} plus span {values.get('span', 1)} exceeds the 12-column grid")
    return errors


# ---------------------------------------------------------------------------
# records


class Dataset:
    """The records of one wiki, loaded once, with the lookups views and dashboards need."""

    def __init__(self, target: Path, datamodel: DataModel, *, settings: Optional[dict[str, Any]] = None,
                 language: Optional[str] = None, now: Optional[str] = None):
        self.target = target
        self.dm = datamodel
        self.language = language or wiki_language(target)
        self.lang = ui_language(self.language)
        self.t = texts(self.language)
        self.settings = settings if settings is not None else load_settings(target, self.language)
        self.number_format = str(self.settings.get("number_format") or "COMMAS_AND_DOT")
        self.default_currency = str(self.settings.get("default_currency") or "")
        self.store = RecordStore(target, datamodel)
        self.context = make_context(self.settings, now)
        self.now = self.context.now
        self._titles: Optional[dict[str, tuple[str, str]]] = None
        self._incoming: Optional[dict[str, list[tuple[str, str, str]]]] = None
        self._events: Optional[dict[str, list[dict[str, Any]]]] = None
        self._event_list: Optional[list[dict[str, Any]]] = None

    def records(self, object_name: str) -> list[Record]:
        return list(self.store.records(object_name).values())

    def active(self, object_name: str) -> list[Record]:
        return [record for record in self.records(object_name) if not record.deleted]

    def context_for(self, me: Optional[str] = None) -> Context:
        if not me:
            return self.context
        return make_context(self.settings, format_instant(self.context.now), me)

    @property
    def titles(self) -> dict[str, tuple[str, str]]:
        if self._titles is None:
            titles: dict[str, tuple[str, str]] = {}
            for object_name in self.dm.objects:
                for record in self.records(object_name):
                    titles[record.id] = (object_name, str(record.data.get("crm_title") or record.id[:8]))
            self._titles = titles
        return self._titles

    def title(self, record_id: str) -> str:
        found = self.titles.get(record_id)
        return found[1] if found else record_id[:8]

    def find(self, record_id: str) -> Optional[Record]:
        found = self.titles.get(record_id)
        return self.store.get(found[0], record_id) if found else None

    @property
    def incoming(self) -> dict[str, list[tuple[str, str, str]]]:
        """Target id -> [(source object, relation field, source id)] over every stored relation.

        Records in the trash do not count, as is usual in CRMs, until they are restored.
        """
        if self._incoming is None:
            index: dict[str, list[tuple[str, str, str]]] = {}
            for object_name in self.dm.objects:
                relation_fields = [name for name, definition in self.dm.stored_fields(object_name)
                                   if definition.get("type") in RELATION_TYPES]
                if not relation_fields:
                    continue
                for record in self.records(object_name):
                    if record.deleted:
                        continue
                    for name in relation_fields:
                        value = record.data.get(name)
                        for item in value if isinstance(value, list) else ([value] if value else []):
                            parsed = parse_record_link(item) if isinstance(item, str) else None
                            if parsed:
                                index.setdefault(parsed[1], []).append((object_name, name, record.id))
            self._incoming = index
        return self._incoming

    def events(self) -> list[dict[str, Any]]:
        if self._event_list is None:
            self._event_list = list(read_events(self.target))
        return self._event_list

    def events_for(self, record_id: str) -> list[dict[str, Any]]:
        if self._events is None:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for event in self.events():
                grouped.setdefault(str(event.get("record_id")), []).append(event)
            self._events = grouped
        return self._events.get(record_id, [])

    # -- formatting -------------------------------------------------------

    def local(self, moment: datetime) -> datetime:
        return self.context.local(moment)

    def format_date(self, day: date) -> str:
        style = str(self.settings.get("date_format") or "DAY_FIRST")
        if style == "MONTH_FIRST":
            return f"{day.month:02d}/{day.day:02d}/{day.year:04d}"
        if style == "YEAR_FIRST":
            return day.isoformat()
        separator = "." if self.lang == "de" else "/"
        return f"{day.day:02d}{separator}{day.month:02d}{separator}{day.year:04d}"

    def format_time(self, moment: datetime) -> str:
        if str(self.settings.get("time_format") or "HOUR_24") == "HOUR_12":
            hour = moment.hour % 12 or 12
            return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"
        return f"{moment.hour:02d}:{moment.minute:02d}"

    def format_instant(self, value: Any) -> str:
        moment = parse_instant(value) if isinstance(value, str) else None
        if moment is None:
            return "" if value in (None, "") else str(value)
        local = self.local(moment)
        return f"{self.format_date(local.date())}, {self.format_time(local)}"

    def format_number(self, value: Decimal, places: Optional[int] = None) -> str:
        return crm_filters.format_number(value, self.number_format, places)

    def format_money(self, value: Optional[Decimal], currency: Optional[str]) -> str:
        if value is None:
            return ""
        code = currency if currency else ""
        return f"{self.format_number(value, 2)} {code}".strip()

    def option_label(self, ref: FieldRef, value: str) -> str:
        for option in ref.options:
            if option.get("value") == value:
                return str(option.get("label") or value)
        return value

    def display(self, record: Record, key: str) -> str:
        """Readable text of one field in the wiki's formats (dates in the configured time zone)."""
        ref = resolve_field(self.dm, record.object, key)
        value = record.data.get(key) if ref.system else None
        if ref.system:
            if ref.ftype == "DATE_TIME":
                return self.format_instant(value)
            if key == "crm_id":
                return record.id
            return "" if value in (None, "") else str(value)
        data = record.data
        if ref.ftype == "DATE_TIME" and not ref.sub:
            return self.format_instant(data.get(ref.base))
        if ref.ftype == "DATE" and not ref.sub:
            raw = data.get(ref.base)
            try:
                return self.format_date(date.fromisoformat(str(raw))) if raw else ""
            except ValueError:
                return str(raw)
        if ref.ftype == "BOOLEAN":
            raw = data.get(ref.base)
            return "" if raw is None else (self.t["yes"] if raw else self.t["no"])
        if ref.ftype in ("NUMBER", "NUMERIC") and not ref.sub:
            number = to_decimal(data.get(ref.base))
            return "" if number is None else self.format_number(number)
        if ref.ftype == "CURRENCY" and ref.sub == "amount":
            amount = crm_filters.raw_value(self.dm, record, key)
            return "" if amount is None else self.format_number(amount, 2)
        if ref.ftype == "RICH_TEXT":
            text = re.sub(r"\s+", " ", re.sub(r"[*_`#>\[\]]", "", record.richtext.get(ref.base, ""))).strip()
            return text if len(text) <= 160 else text[:157].rstrip() + "..."
        if ref.ftype == "FILES":
            return ", ".join(str(item.get("name") or "") for item in json_list(data.get(ref.base)) if isinstance(item, dict))
        if ref.ftype == "RAW_JSON":
            raw = str(data.get(ref.base) or "")
            return raw if len(raw) <= 160 else raw[:157] + "..."
        if ref.ftype == "ARRAY" or (ref.ftype == "EMAILS" and ref.sub == "additionalEmails"):
            raw = data.get(key if ref.sub else ref.base)
            return ", ".join(str(item) for item in raw) if isinstance(raw, list) else str(raw or "")
        if ref.inverse:
            titles = [self.title(source_id) for source_object, field_name, source_id in self.incoming.get(record.id, [])
                      if source_object == ref.definition["relation"].get("target")
                      and field_name == ref.definition["relation"].get("inverse")]
            return ", ".join(titles)
        return crm_filters.display_value(self.dm, record, key, self.number_format)

    # -- aggregates -------------------------------------------------------

    def is_money(self, object_name: str, function: str, key: Optional[str]) -> bool:
        if function not in MONEY_FUNCTIONS or not key:
            return False
        try:
            ref = resolve_field(self.dm, object_name, key)
        except FilterError:
            return False
        return ref.ftype == "CURRENCY" and ref.sub in ("", "amount")

    def currency_partition(self, records: list[Record], key: str) -> list[tuple[str, list[Record]]]:
        """Records with an amount, split by currency code; the default currency comes first."""
        base = key.split(".", 1)[0]
        groups: dict[str, list[Record]] = {}
        for record in records:
            if record.data.get(f"{base}.amountMicros") is None:
                continue
            code = str(record.data.get(f"{base}.currencyCode") or "")
            groups.setdefault(code, []).append(record)
        return sorted(groups.items(), key=lambda item: (item[0] != self.default_currency, item[0] == "", item[0]))

    def empty_currency(self, object_name: str, key: Optional[str]) -> str:
        """Currency of an amount aggregate without any amount: the field's default, else the workspace default."""
        return crm_filters.default_currency(self.dm, object_name, key, self.default_currency)

    def aggregate_values(self, object_name: str, records: list[Record], function: str, key: Optional[str]) -> list[dict[str, Any]]:
        """[{currency, value}]: one entry, or one per currency when amounts are summed or compared."""
        function = normalize_function(function)
        field = key if function != "COUNT" else None
        if self.is_money(object_name, function, key):
            parts = self.currency_partition(records, key)
            if not parts:
                # No amount at all: SUM is 0 and AVG, MIN and MAX have no value, in the currency a new amount gets.
                return [{"currency": self.empty_currency(object_name, key) or None,
                         "value": crm_filters.aggregate(self.dm, records, function, key)}]
            return [{"currency": code or None, "value": crm_filters.aggregate(self.dm, part, function, key)} for code, part in parts]
        return [{"currency": None, "value": crm_filters.aggregate(self.dm, records, function, field)}]

    def format_aggregate(self, object_name: str, function: str, key: Optional[str], value: dict[str, Any]) -> str:
        function = normalize_function(function)
        number = value.get("value")
        if number is None:
            return "" if function not in {"COUNT", "COUNT_EMPTY", "COUNT_NOT_EMPTY", "COUNT_TRUE", "COUNT_FALSE",
                                          "COUNT_UNIQUE_VALUES"} else "0"
        if function.startswith("PERCENTAGE"):
            return self.t["percent"].format(value=self.format_number(number, 1))
        if function.startswith("COUNT"):
            return self.format_number(number)
        if self.is_money(object_name, function, key):
            currency = value.get("currency") or self.t["no_currency"]
            return self.format_money(number, currency)
        if function == "AVG":
            return self.format_number(number, 2)
        return self.format_number(number) if number == number.to_integral_value() else self.format_number(number, 2)

    def format_aggregates(self, object_name: str, function: str, key: Optional[str], values: list[dict[str, Any]]) -> str:
        texts_ = [self.format_aggregate(object_name, function, key, value) for value in values]
        return " · ".join(text for text in texts_ if text)


def json_list(value: Any) -> list[Any]:
    import json

    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


# ---------------------------------------------------------------------------
# selection, sorting and grouping


def select(ds: Dataset, object_name: str, filter_spec: Any = None, sorts: Optional[list[dict[str, str]]] = None,
           context: Optional[Context] = None, include_deleted: bool = False) -> list[Record]:
    """Records of one object matching a filter, in view order (missing values last)."""
    records = crm_filters.select_records(ds.dm, ds.records(object_name), filter_spec=filter_spec,
                                         context=context or ds.context, include_deleted=include_deleted)
    return sort_records(ds, records, sorts or [])


def sort_records(ds: Dataset, records: list[Record], sorts: list[dict[str, str]]) -> list[Record]:
    """crm_filters order, except that relations sort by the linked record's title instead of its id."""
    ordered = list(records)
    for sort in reversed(sorts):
        if not ordered:
            break
        key = sort.get("field")
        try:
            ref = resolve_field(ds.dm, ordered[0].object, key)
        except FilterError:
            continue
        descending = str(sort.get("direction", "asc")).lower() == "desc"
        if ref.relation:
            def title_key(record: Record, key=key) -> str:
                ids = crm_filters.raw_value(ds.dm, record, key)
                return ds.title(ids[0]).casefold() if ids else ""

            present = [record for record in ordered if title_key(record)]
            missing = [record for record in ordered if not title_key(record)]
            present.sort(key=title_key, reverse=descending)
            ordered = present + missing
        else:
            ordered = crm_filters.sort_records(ds.dm, ordered, [{"field": key, "direction": "desc" if descending else "asc"}])
    return ordered


def default_order(ds: Dataset, records: list[Record]) -> list[Record]:
    """Stable fallback order: position, then title, then id."""
    def key(record: Record) -> tuple:
        position = record.data.get("crm_position")
        number = position if isinstance(position, (int, float)) and not isinstance(position, bool) else None
        return (number is None, number or 0, str(record.data.get("crm_title") or "").casefold(), record.id)

    return sorted(records, key=key)


def group_keys(ds: Dataset, record: Record, ref: FieldRef, granularity: Optional[str], context: Context) -> list[str]:
    keys = crm_filters.group_key(ds.dm, record, ref.key, granularity or "NONE", context)
    unique = []
    for key in keys:
        if key not in unique:
            unique.append(key)
    return unique or [""]


def natural_sort_key(ds: Dataset, ref: FieldRef, granularity: Optional[str], key: str) -> tuple:
    """Order of group keys: option position, chronology, number or title; 'no value' last."""
    if key == "":
        return (2, 0, "")
    options = [option.get("value") for option in ref.options]
    if options:
        return (0, options.index(key), "") if key in options else (1, 0, key.casefold())
    if ref.ftype == "RATING" and key in RATING_VALUES:
        return (0, RATING_VALUES.index(key), "")
    if ref.ftype == "BOOLEAN":
        return (0, 0 if key == "true" else 1, "")
    if ref.date and granularity and granularity != "NONE":
        if granularity == "DAY_OF_WEEK" and key.isdigit():
            return (0, (int(key) - ds.context.week_start) % 7, "")
        return (0, 0, key)
    if ref.relation:
        return (0, 0, ds.title(key).casefold() + "\u0000" + key)
    number = to_decimal(key)
    if ref.ftype in NUMERIC_TYPES and number is not None:
        return (0, number, "")
    return (0, 0, key.casefold() + "\u0000" + key)


def group_label(ds: Dataset, ref: FieldRef, granularity: Optional[str], key: str) -> str:
    if key == "":
        return ds.t["empty"]
    if ref.options:
        return ds.option_label(ref, key)
    if ref.ftype == "RATING" and key in RATING_VALUES:
        return "★" * (RATING_VALUES.index(key) + 1)
    if ref.ftype == "BOOLEAN":
        return ds.t["yes"] if key == "true" else ds.t["no"]
    if ref.relation:
        return ds.title(key)
    if ref.date and granularity and granularity != "NONE":
        return bucket_label(ds, key, granularity)
    if ref.ftype == "DATE_TIME" and not ref.sub:
        return ds.format_instant(key)
    if ref.ftype == "DATE" and not ref.sub:
        try:
            return ds.format_date(date.fromisoformat(key))
        except ValueError:
            return key
    number = to_decimal(key)
    if ref.ftype == "CURRENCY" and number is not None:
        return ds.format_number(number, 2)
    if ref.ftype in ("NUMBER", "NUMERIC") and number is not None:
        return ds.format_number(number)
    return key


def bucket_label(ds: Dataset, key: str, granularity: str) -> str:
    t = ds.t
    try:
        if granularity == "DAY":
            return ds.format_date(date.fromisoformat(key))
        if granularity == "WEEK":
            year, week = key.split("-W")
            return t["week"].format(week=int(week), year=year)
        if granularity == "MONTH":
            year, month = key.split("-")
            return f"{t['months_short'][int(month) - 1]} {year}"
        if granularity == "QUARTER":
            year, quarter = key.split("-")
            return f"{quarter} {year}"
        if granularity == "DAY_OF_WEEK":
            return t["weekdays_short"][int(key) - 1]
        if granularity == "MONTH_OF_YEAR":
            return t["months"][int(key) - 1]
    except (ValueError, IndexError):
        return key
    return key


def fill_gaps(keys: list[str], granularity: str, limit: int = 1000) -> list[str]:
    """All date buckets between the first and last present bucket (charts fill these gaps)."""
    present = sorted(key for key in keys if key)
    if granularity not in GAP_FILLED or len(present) < 2:
        return keys
    first, last = present[0], present[-1]
    filled: list[str] = []
    try:
        if granularity == "DAY":
            day, end = date.fromisoformat(first), date.fromisoformat(last)
            while day <= end and len(filled) <= limit:
                filled.append(day.isoformat())
                day += timedelta(days=1)
        elif granularity == "WEEK":
            year, week = (int(part) for part in first.split("-W"))
            day = date.fromisocalendar(year, week, 1)
            while len(filled) <= limit:
                iso = day.isocalendar()
                key = f"{iso[0]}-W{iso[1]:02d}"
                filled.append(key)
                if key >= last:
                    break
                day += timedelta(days=7)
        elif granularity in ("MONTH", "QUARTER", "YEAR"):
            if granularity == "MONTH":
                year, month = (int(part) for part in first.split("-"))
                step = 1
            elif granularity == "QUARTER":
                year, quarter = first.split("-Q")
                year, month, step = int(year), (int(quarter) - 1) * 3 + 1, 3
            else:
                year, month, step = int(first), 1, 12
            while len(filled) <= limit:
                if granularity == "MONTH":
                    key = f"{year}-{month:02d}"
                elif granularity == "QUARTER":
                    key = f"{year}-Q{(month - 1) // 3 + 1}"
                else:
                    key = str(year)
                filled.append(key)
                if key >= last:
                    break
                month += step
                while month > 12:
                    month -= 12
                    year += 1
    except (ValueError, IndexError):
        return keys
    if len(filled) > limit:
        return keys
    return filled + ([""] if "" in keys else [])


@dataclass
class Group:
    key: str
    label: str
    records: list[Record]
    color: str = ""
    other: bool = False


def group_records(ds: Dataset, records: list[Record], ref: FieldRef, granularity: Optional[str], context: Context,
                  *, all_options: bool = False) -> list[Group]:
    """Records grouped by one field in natural order; a record with several values joins several groups."""
    buckets: dict[str, list[Record]] = {}
    for record in records:
        for key in group_keys(ds, record, ref, granularity, context):
            buckets.setdefault(key, []).append(record)
    if all_options:
        for option in ref.options:
            buckets.setdefault(str(option.get("value")), [])
    colors = {str(option.get("value")): str(option.get("color") or "") for option in ref.options}
    ordered = sorted(buckets, key=lambda key: natural_sort_key(ds, ref, granularity, key))
    return [Group(key, group_label(ds, ref, granularity, key), buckets[key], colors.get(key, "")) for key in ordered]


# ---------------------------------------------------------------------------
# views


def compute_view(ds: Dataset, view: dict[str, Any]) -> dict[str, Any]:
    view = view if "hideEmptyGroups" in view and "aggregates" in view else normalize_view(ds.dm, view)
    if view["type"] == "kanban":
        return compute_kanban(ds, view)
    if view["type"] == "calendar":
        return compute_calendar(ds, view)
    return compute_table(ds, view)


def _columns(ds: Dataset, view: dict[str, Any]) -> list[FieldRef]:
    return [resolve_field(ds.dm, view["object"], key) for key in view["fields"]]


def compute_table(ds: Dataset, view: dict[str, Any]) -> dict[str, Any]:
    """Rows of a table view in order, optionally grouped, with footer aggregates per group and overall."""
    object_name = view["object"]
    context = ds.context_for(view.get("me"))
    records = select(ds, object_name, view.get("filter"), view.get("sort"), context)
    if not view.get("sort"):
        records = default_order(ds, records)
    aggregates = view.get("aggregates") or {}

    def footer(rows: list[Record]) -> dict[str, list[dict[str, Any]]]:
        return {key: ds.aggregate_values(object_name, rows, function, key) for key, function in aggregates.items()}

    groups: list[dict[str, Any]] = []
    if view.get("groupBy"):
        ref = resolve_field(ds.dm, object_name, view["groupBy"])
        for group in group_records(ds, records, ref, view.get("dateGranularity"), context,
                                   all_options=not view.get("hideEmptyGroups")):
            if not group.records and (view.get("hideEmptyGroups") or group.key == ""):
                continue
            groups.append({"key": group.key, "label": group.label, "color": group.color, "records": group.records,
                           "count": len(group.records), "aggregates": footer(group.records)})
    else:
        groups.append({"key": None, "label": "", "color": "", "records": records, "count": len(records),
                       "aggregates": footer(records)})
    return {
        "view": view, "type": "table", "columns": _columns(ds, view), "groups": groups, "count": len(records),
        "aggregates": footer(records), "context": context,
    }


def stage_since(ds: Dataset, record: Record, key: str) -> Optional[str]:
    """When the record entered its current value of a field, from the event log; None if unknown."""
    since = None
    for event in ds.events_for(record.id):
        changes = event.get("changes") or {}
        if event.get("op") == "create":
            origin = event.get("origin") or {}
            since = event.get("at") if key in changes and origin.get("kind") not in ("import", "migration") else None
        elif key in changes and not event.get("erased"):
            since = event.get("at")
    return since


def compute_kanban(ds: Dataset, view: dict[str, Any]) -> dict[str, Any]:
    """Kanban columns in option order plus 'no value'; counts, aggregates and expected amounts per currency."""
    object_name = view["object"]
    context = ds.context_for(view.get("me"))
    ref = resolve_field(ds.dm, object_name, view["groupBy"])
    records = default_order(ds, ds.records(object_name))
    records = crm_filters.select_records(ds.dm, records, filter_spec=view.get("filter"), context=context)
    records = sort_records(ds, records, view.get("sort") or [])
    aggregate = view.get("aggregate") or {"function": "COUNT", "field": None}
    probabilities = view.get("probabilities") or {}
    amount_field = view.get("expectedAmountField")
    buckets: dict[str, list[Record]] = {}
    for record in records:
        value = record.data.get(ref.base)
        buckets.setdefault(str(value) if value not in (None, "") else "", []).append(record)
    option_values = [str(option.get("value")) for option in ref.options]
    order = option_values + sorted(key for key in buckets if key and key not in option_values) + [""]
    nullable = (ref.definition or {}).get("nullable") is not False
    columns = []
    for key in order:
        rows = buckets.get(key, [])
        if not rows and (view.get("hideEmptyGroups") or (key == "" and not nullable) or (key and key not in option_values)):
            continue
        probability = probabilities.get(key) if key else None
        expected = None
        if probabilities and amount_field:
            expected = expected_amounts(ds, object_name, rows, amount_field, probability)
        columns.append({
            "key": key,
            "label": ds.option_label(ref, key) if key else ds.t["empty"],
            "color": next((str(option.get("color") or "") for option in ref.options if option.get("value") == key), ""),
            "records": rows,
            "count": len(rows),
            "aggregate": ds.aggregate_values(object_name, rows, aggregate["function"], aggregate.get("field")),
            "probability": probability,
            "expected": expected,
        })
    total_expected = None
    if probabilities and amount_field:
        total_expected = _sum_by_currency([column["expected"] or [] for column in columns])
    return {
        "view": view, "type": "kanban", "columns": columns, "count": len(records), "group": ref,
        "aggregate": ds.aggregate_values(object_name, records, aggregate["function"], aggregate.get("field")),
        "expected": total_expected, "context": context, "cards": _columns(ds, view),
    }


def expected_amounts(ds: Dataset, object_name: str, records: list[Record], amount_field: str,
                     probability: Optional[Decimal]) -> list[dict[str, Any]]:
    """Amount times share per currency; a column without a share has no expected amount."""
    if probability is None:
        return []
    ref = resolve_field(ds.dm, object_name, amount_field)
    if ref.ftype == "CURRENCY":
        parts = ds.currency_partition(records, amount_field)
        return [{"currency": code or None, "value": (crm_filters.aggregate(ds.dm, part, "SUM", amount_field) or Decimal(0)) * probability}
                for code, part in parts]
    total = crm_filters.aggregate(ds.dm, records, "SUM", amount_field)
    return [{"currency": None, "value": (total or Decimal(0)) * probability}] if records else []


def _sum_by_currency(lists: Iterable[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    totals: dict[Optional[str], Decimal] = {}
    order: list[Optional[str]] = []
    for values in lists:
        for value in values:
            if value.get("value") is None:
                continue
            currency = value.get("currency")
            if currency not in totals:
                order.append(currency)
                totals[currency] = Decimal(0)
            totals[currency] += value["value"]
    return [{"currency": currency, "value": totals[currency]} for currency in sorted(order, key=lambda code: (code is None, code or ""))]


def local_day(ds: Dataset, ref: FieldRef, value: Any) -> tuple[str, str, str]:
    """(local date YYYY-MM-DD, local time text, sort key) of a DATE or DATE_TIME value.

    The sort key is the UTC instant, so the hour that repeats when daylight saving time
    ends still sorts in the order the moments happened; all-day dates sort first.
    """
    if value in (None, ""):
        return "", "", ""
    if ref.ftype == "DATE":
        text = str(value)[:10]
        return (text, "", "") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else ("", "", "")
    moment = parse_instant(value)
    if moment is None:
        return "", "", ""
    local = ds.local(moment)
    return local.date().isoformat(), ds.format_time(local), format_instant(moment)


def compute_calendar(ds: Dataset, view: dict[str, Any]) -> dict[str, Any]:
    """Calendar entries placed on their local day in the configured time zone, across DST changes."""
    object_name = view["object"]
    context = ds.context_for(view.get("me"))
    ref = resolve_field(ds.dm, object_name, view["dateField"])
    records = select(ds, object_name, view.get("filter"), view.get("sort"), context)
    entries, undated = [], []
    for record in records:
        day, time_text, sortable = local_day(ds, ref, record.data.get(ref.key))
        if not day:
            undated.append(record)
            continue
        entries.append({"record": record, "date": day, "time": time_text, "sort": sortable})
    entries.sort(key=lambda entry: (entry["date"], entry["sort"], str(entry["record"].data.get("crm_title") or "").casefold(), entry["record"].id))
    return {
        "view": view, "type": "calendar", "entries": entries, "undated": undated, "count": len(records),
        "today": context.today().isoformat(), "mode": view.get("calendarMode") or "month", "field": ref,
        "context": context, "cards": _columns(ds, view),
    }


# ---------------------------------------------------------------------------
# dashboards


def compute_widget(ds: Dataset, widget: dict[str, Any], views: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    widget = widget if "layout" in widget and "stacked" in widget else normalize_widget(ds.dm, widget)
    kind = widget["type"]
    if kind == "richtext":
        return {"widget": widget, "kind": kind, "text": widget["text"]}
    if kind == "link":
        return {"widget": widget, "kind": kind, "url": widget["url"]}
    if kind == "table":
        view = next((item for item in views or [] if item["id"] == widget["view"]), None)
        if view is None:
            return {"widget": widget, "kind": kind, "error": f"view {widget['view']} is missing"}
        context = ds.context_for(view.get("me"))
        records = select(ds, view["object"], view.get("filter"), view.get("sort"), context)
        if not view.get("sort"):
            records = default_order(ds, records)
        limit = widget.get("limit") or 10
        return {"widget": widget, "kind": kind, "view": view, "columns": _columns(ds, view), "rows": records[:limit],
                "count": len(records), "hidden": max(0, len(records) - limit)}
    object_name = widget["object"]
    context = ds.context_for(widget.get("me"))
    records = select(ds, object_name, widget.get("filter"), None, context)
    function = widget["aggregate"]["function"]
    field = widget["aggregate"].get("field")
    if kind == "number":
        return {"widget": widget, "kind": kind, "count": len(records),
                "values": ds.aggregate_values(object_name, records, function, field)}
    if ds.is_money(object_name, function, field):
        parts = ds.currency_partition(records, field)
        panels = [compute_panel(ds, widget, part, code or None, context) for code, part in parts]
        if not panels:
            panels = [compute_panel(ds, widget, [], ds.empty_currency(object_name, field) or None, context)]
    else:
        panels = [compute_panel(ds, widget, records, None, context)]
    return {"widget": widget, "kind": kind, "count": len(records), "panels": panels}


def _order_groups(ds: Dataset, groups: list[Group], values: dict[str, Optional[Decimal]], order_by: Optional[str],
                  ref: FieldRef, granularity: Optional[str]) -> list[Group]:
    natural = sorted(groups, key=lambda group: natural_sort_key(ds, ref, granularity, group.key))
    if order_by in (None, "position", "label"):
        if order_by == "label" and not ref.options and not (ref.date and granularity):
            return sorted(natural, key=lambda group: (group.key == "", group.label.casefold(), group.key))
        return natural
    if order_by in ("position_desc", "label_desc"):
        empty = [group for group in natural if group.key == ""]
        rest = [group for group in natural if group.key != ""]
        if order_by == "label_desc" and not ref.options and not (ref.date and granularity):
            rest = sorted(rest, key=lambda group: (group.label.casefold(), group.key))
        return list(reversed(rest)) + empty
    ranked = list(natural)
    descending = order_by == "value"
    present = [group for group in ranked if values.get(group.key) is not None]
    missing = [group for group in ranked if values.get(group.key) is None]
    present.sort(key=lambda group: values[group.key], reverse=descending)
    return present + missing


def compute_panel(ds: Dataset, widget: dict[str, Any], records: list[Record], currency: Optional[str],
                  context: Context) -> dict[str, Any]:
    """One chart: categories, series values (each crm_filters.aggregate of its records), hidden counts."""
    object_name = widget["object"]
    function = widget["aggregate"]["function"]
    field = widget["aggregate"].get("field") if function != "COUNT" else None
    kind = widget["type"]
    ref = resolve_field(ds.dm, object_name, widget["groupBy"])
    granularity = widget.get("dateGranularity") if ref.date else None
    groups = group_records(ds, records, ref, granularity, context)
    if widget.get("omitNullValues"):
        groups = [group for group in groups if group.key != ""]
    values = {group.key: crm_filters.aggregate(ds.dm, group.records, function, field) for group in groups}
    order_by = widget.get("orderBy")
    if kind == "pie":
        groups = [group for group in groups if values.get(group.key) is not None and values[group.key] > 0]
    groups = _order_groups(ds, groups, values, order_by, ref, granularity)
    if kind in ("bar", "line") and granularity in GAP_FILLED and order_by in (None, "position", "label"):
        present = {group.key: group for group in groups}
        filled_keys = fill_gaps([group.key for group in groups], granularity)
        groups = [present.get(key) or Group(key, group_label(ds, ref, granularity, key), []) for key in filled_keys]
        for group in groups:
            if group.key not in values:
                values[group.key] = crm_filters.aggregate(ds.dm, [], function, field)
    # A pie shows the whole: without an explicit limit every group beyond the colour slots folds into "other".
    default_limit = len(groups) if kind == "pie" else CHART_LIMITS[kind]
    limit = min(widget.get("limit") or default_limit, default_limit if kind == "pie" and not widget.get("limit") else CHART_LIMITS[kind])
    hidden = max(0, len(groups) - limit)
    shown = groups[:limit]
    series: list[dict[str, Any]] = []
    folded = 0
    if kind == "pie":
        if len(shown) > MAX_SERIES:
            keep, tail = shown[:MAX_SERIES - 1], shown[MAX_SERIES - 1:]
            tail_records = _union(tail)
            folded = len(tail)
            shown = keep + [Group(OTHER_KEY, ds.t["other"], tail_records, other=True)]
            values[OTHER_KEY] = crm_filters.aggregate(ds.dm, tail_records, function, field)
        series.append({"key": "", "label": widget.get("label") or "", "values": [values.get(group.key) for group in shown]})
    elif widget.get("secondaryGroupBy"):
        second = resolve_field(ds.dm, object_name, widget["secondaryGroupBy"])
        second_granularity = widget.get("secondaryDateGranularity") if second.date else None
        shown_records = _union(shown)
        second_groups = group_records(ds, shown_records, second, second_granularity, context)
        if len(second_groups) > MAX_SERIES:
            totals = {group.key: crm_filters.aggregate(ds.dm, group.records, function, field) for group in second_groups}
            ranked = sorted(range(len(second_groups)), key=lambda index: (
                totals[second_groups[index].key] is None, -(totals[second_groups[index].key] or 0), index))
            keep_indexes = sorted(ranked[:MAX_SERIES - 1])
            tail = [group for index, group in enumerate(second_groups) if index not in keep_indexes]
            folded = len(tail)
            second_groups = [second_groups[index] for index in keep_indexes] + [Group(OTHER_KEY, ds.t["other"], _union(tail), other=True)]
        members = {group.key: {record.id for record in group.records} for group in shown}
        for second_group in second_groups[:SECONDARY_LIMIT]:
            second_ids = {record.id for record in second_group.records}
            row = []
            for group in shown:
                subset = [record for record in group.records if record.id in second_ids and record.id in members[group.key]]
                row.append(crm_filters.aggregate(ds.dm, subset, function, field) if subset or function.startswith("COUNT") else None)
            series.append({"key": second_group.key, "label": second_group.label, "values": row, "other": second_group.other})
    else:
        series.append({"key": "", "label": widget.get("label") or "", "values": [values.get(group.key) for group in shown]})
    if widget.get("cumulative") and kind in ("bar", "line"):
        for item in series:
            running = Decimal(0)
            cumulative = []
            for value in item["values"]:
                running += value or Decimal(0)
                cumulative.append(running)
            item["raw"] = item["values"]
            item["values"] = cumulative
    total = crm_filters.aggregate(ds.dm, records, function, field)
    return {
        "currency": currency,
        "categories": [{"key": group.key, "label": group.label, "color": group.color, "other": group.other} for group in shown],
        "series": series, "hidden": hidden, "folded": folded, "total": total, "records": len(records),
        "groups": {group.key: group.records for group in shown},
    }


def _union(groups: list[Group]) -> list[Record]:
    seen: set[str] = set()
    result = []
    for group in groups:
        for record in group.records:
            if record.id not in seen:
                seen.add(record.id)
                result.append(record)
    return result


# ---------------------------------------------------------------------------
# describing filters


def describe_filter(ds: Dataset, object_name: str, spec: Any, me: Optional[str] = None) -> str:
    """A readable sentence for a filter group, in the wiki language."""
    try:
        group = crm_filters.normalize_filter(spec)
    except FilterError:
        return ""
    t = ds.t

    def value_text(ref: FieldRef, value: Any) -> str:
        if isinstance(value, list):
            return ", ".join(value_text(ref, item) for item in value)
        if value == "@me":
            return t["me"].format(name=ds.title(me) if me else "?")
        if value in (None, ""):
            return ""
        text = str(value)
        if ref.options:
            return f"„{ds.option_label(ref, text)}“" if ds.lang == "de" else f"\"{ds.option_label(ref, text)}\""
        if ref.relation:
            parsed = parse_record_link(text)
            return ds.title(parsed[1] if parsed else text.lower())
        if ref.date:
            match = crm_filters.RELATIVE_RE.fullmatch(text.upper())
            if match:
                direction, amount, unit = match.groups()
                if direction == "THIS":
                    return t["relative"]["THIS"].format(unit=t["this_units"][unit])
                singular, plural = t["units"][unit]
                return t["relative"][direction].format(n=amount, unit=singular if amount == "1" else plural)
            moment = parse_instant(text) if "T" in text else None
            if moment is not None:
                return ds.format_instant(text)
            try:
                return ds.format_date(date.fromisoformat(text[:10]))
            except ValueError:
                return text
        return f"„{text}“" if ds.lang == "de" else f"\"{text}\""

    def walk(node: Optional[dict[str, Any]], top: bool = False) -> str:
        if node is None:
            return ""
        if "conditions" in node:
            parts = [walk(child) for child in node["conditions"]]
            parts = [part for part in parts if part]
            if not parts:
                return ""
            joined = (t["or"] if node["op"] == "OR" else t["and"]).join(parts)
            if node["op"] == "NOT":
                return t["not"].format(inner=t["and"].join(parts))
            return joined if top or len(parts) == 1 else f"({joined})"
        try:
            ref = resolve_field(ds.dm, object_name, node["field"])
        except FilterError:
            return ""
        label = field_label(ds.dm, object_name, node["field"], ds.language)
        operand = node["operand"]
        phrase = t["operands"].get(operand, operand)
        if operand in {"IS_EMPTY", "IS_NOT_EMPTY", "IS_NOT_NULL", "IS_IN_PAST", "IS_IN_FUTURE", "IS_TODAY"}:
            return f"{label} {phrase}"
        return f"{label} {phrase} {value_text(ref, node.get('value'))}".strip()

    return walk(group, top=True)
