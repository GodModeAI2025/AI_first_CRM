#!/usr/bin/env python3
"""Read iCalendar files (RFC 5545) for the CRM import with the standard library only.

Server CRMs read calendars through a library and let the provider expand recurring
events. This module covers what a file import needs: line unfolding, property
parameters, VEVENT, VTIMEZONE, time zone resolution, all-day events, UID and
SEQUENCE, cancellations, and a recurrence expander for a documented subset.

Time zones resolve in this order: the TZID through zoneinfo, then the Windows
name through assets/crm-windows-zones.json, then the offsets of the VTIMEZONE
block embedded in the file, and finally local time of the wiki's time zone with
a note. Ambiguous and non-existent local times (daylight saving changes) follow
RFC 5545 (first occurrence, offset before the gap) and are reported.

Recurrence: ``expand_rrule(event, start, end)`` expands FREQ DAILY, WEEKLY,
MONTHLY and YEARLY with INTERVAL, COUNT, UNTIL, BYDAY (weekdays, with an
ordinal for MONTHLY and YEARLY), BYMONTHDAY, BYMONTH, WKST, plus RDATE and
EXDATE. Other rule parts (BYSETPOS, BYWEEKNO, BYYEARDAY, BYHOUR, BYMINUTE,
BYSECOND, HOURLY and finer) are kept as text and reported as not expandable.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Union

from crm_contract import parse_instant

WINDOWS_ZONES_ASSET = Path(__file__).resolve().parent.parent / "assets" / "crm-windows-zones.json"
UTC = timezone.utc
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
WEEKDAY_NAMES = {value: key for key, value in WEEKDAYS.items()}
EXPANDABLE_FREQS = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}
EXPANDABLE_PARTS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "WKST"}
MAX_GENERATED = 100_000
CONFERENCE_PROPERTIES = (
    "X-GOOGLE-CONFERENCE", "X-MICROSOFT-SKYPETEAMSMEETINGURL", "X-MICROSOFT-ONLINEMEETINGCONFLINK",
    "X-MICROSOFT-ONLINEMEETINGEXTERNALLINK", "URL",
)
# Conference providers as CRMs store them (Google conferenceSolution.key.type, Microsoft onlineMeetingProvider).
CONFERENCE_SOLUTIONS = {
    "X-GOOGLE-CONFERENCE": "hangoutsMeet",
    "X-MICROSOFT-SKYPETEAMSMEETINGURL": "teamsForBusiness",
    "X-MICROSOFT-ONLINEMEETINGCONFLINK": "teamsForBusiness",
    "X-MICROSOFT-ONLINEMEETINGEXTERNALLINK": "teamsForBusiness",
}
CONFERENCE_HOSTS = (
    ("meet.google.com", "hangoutsMeet"), ("teams.microsoft.com", "teamsForBusiness"), ("teams.live.com", "teams"),
    ("zoom.us", "zoom"), ("webex.com", "webex"),
)
CONFERENCE_URL_RE = re.compile(
    r"https://(?:teams\.microsoft\.com/l/meetup-join/|teams\.live\.com/meet/|[a-z0-9.-]*zoom\.us/[jw]/|meet\.google\.com/|[a-z0-9.-]*webex\.com/)[^\s<>\")\]]+",
    re.IGNORECASE,
)

Moment = Union[datetime, date]


class IcalError(ValueError):
    """An iCalendar text or value cannot be read."""


# ---------------------------------------------------------------------------
# content lines and components


@dataclass
class Prop:
    name: str
    params: dict[str, list[str]]
    value: str

    def param(self, key: str, default: Optional[str] = None) -> Optional[str]:
        values = self.params.get(key.upper())
        return values[0] if values else default


@dataclass
class Component:
    name: str
    props: list[Prop] = field(default_factory=list)
    children: list["Component"] = field(default_factory=list)

    def get(self, name: str) -> Optional[Prop]:
        name = name.upper()
        for prop in self.props:
            if prop.name == name:
                return prop
        return None

    def get_all(self, name: str) -> list[Prop]:
        name = name.upper()
        return [prop for prop in self.props if prop.name == name]

    def text(self, name: str) -> str:
        prop = self.get(name)
        return unescape_text(prop.value) if prop else ""


def decode_ics_bytes(data: bytes) -> tuple[str, Optional[str]]:
    """Decode a calendar file strictly; fall back to cp1252, then latin-1, with a note."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        note = None if encoding == "utf-8" else f"calendar file is not valid UTF-8; read as {encoding}"
        return text, note
    raise IcalError("calendar file cannot be decoded")  # pragma: no cover - latin-1 never fails


def unfold_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in re.split(r"\r\n|\r|\n", text):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def parse_line(line: str) -> Prop:
    match = re.match(r"[A-Za-z0-9-]+", line)
    if not match:
        raise IcalError(f"invalid content line: {line[:40]!r}")
    name = match.group(0).upper()
    index = match.end()
    params: dict[str, list[str]] = {}
    while index < len(line) and line[index] == ";":
        index += 1
        param_match = re.match(r"([A-Za-z0-9-]+)=", line[index:])
        if not param_match:
            raise IcalError(f"invalid parameter in {name}")
        param_name = param_match.group(1).upper()
        index += param_match.end()
        values: list[str] = []
        while True:
            if index < len(line) and line[index] == '"':
                closing = line.find('"', index + 1)
                if closing < 0:
                    raise IcalError(f"unterminated quoted parameter in {name}")
                values.append(line[index + 1:closing])
                index = closing + 1
            else:
                start = index
                while index < len(line) and line[index] not in ",;:":
                    index += 1
                values.append(line[start:index])
            if index < len(line) and line[index] == ",":
                index += 1
                continue
            break
        params.setdefault(param_name, []).extend(values)
    if index >= len(line) or line[index] != ":":
        raise IcalError(f"property {name} has no value")
    return Prop(name, params, line[index + 1:])


def parse_components(text: str) -> tuple[list[Component], list[str]]:
    """Top-level components of a calendar text and notes about skipped lines."""
    roots: list[Component] = []
    stack: list[Component] = []
    notes: list[str] = []
    for line in unfold_lines(text):
        try:
            prop = parse_line(line)
        except IcalError as exc:
            notes.append(str(exc))
            continue
        if prop.name == "BEGIN":
            component = Component(prop.value.strip().upper())
            if stack:
                stack[-1].children.append(component)
            else:
                roots.append(component)
            stack.append(component)
        elif prop.name == "END":
            name = prop.value.strip().upper()
            while stack:
                closed = stack.pop()
                if closed.name == name:
                    break
        elif stack:
            stack[-1].props.append(prop)
    if stack:
        notes.append(f"component {stack[-1].name} is not closed")
    return roots, notes


def unescape_text(value: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda match: "\n" if match.group(1) in "nN" else match.group(1), value)


# ---------------------------------------------------------------------------
# values


DATE_VALUE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
DATETIME_VALUE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$")
DURATION_VALUE = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")
OFFSET_VALUE = re.compile(r"^([+-])(\d{2})(\d{2})(\d{2})?$")


def parse_value(text: str) -> tuple[Moment, bool]:
    """A DATE or DATE-TIME value: (naive datetime or date, is_utc)."""
    value = text.strip()
    match = DATETIME_VALUE.match(value)
    if match:
        year, month, day, hour, minute, second, zulu = match.groups()
        second_value = min(int(second), 59)  # leap seconds are clamped
        try:
            return datetime(int(year), int(month), int(day), int(hour), int(minute), second_value), bool(zulu)
        except ValueError as exc:
            raise IcalError(f"invalid date-time {value!r}") from exc
    match = DATE_VALUE.match(value)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))), False
        except ValueError as exc:
            raise IcalError(f"invalid date {value!r}") from exc
    raise IcalError(f"invalid date or date-time {value!r}")


def parse_duration(text: str) -> timedelta:
    match = DURATION_VALUE.match(text.strip().upper())
    if not match or text.strip().upper() in {"P", "-P", "+P", "PT"}:
        raise IcalError(f"invalid duration {text!r}")
    sign, weeks, days, hours, minutes, seconds = match.groups()
    delta = timedelta(weeks=int(weeks or 0), days=int(days or 0), hours=int(hours or 0),
                      minutes=int(minutes or 0), seconds=int(seconds or 0))
    return -delta if sign == "-" else delta


def parse_offset(text: str) -> timedelta:
    match = OFFSET_VALUE.match(text.strip())
    if not match:
        raise IcalError(f"invalid UTC offset {text!r}")
    sign, hours, minutes, seconds = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes), seconds=int(seconds or 0))
    return -delta if sign == "-" else delta


def format_basic(moment: Moment, utc: bool = False) -> str:
    if isinstance(moment, datetime):
        return moment.strftime("%Y%m%dT%H%M%S") + ("Z" if utc else "")
    return moment.strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# time zones


class Zone:
    """Converts naive local wall times of one zone to UTC and back."""

    name = "UTC"
    iana: Optional[str] = "UTC"

    def to_utc(self, local: datetime) -> tuple[datetime, Optional[str]]:
        return local.replace(tzinfo=UTC), None

    def from_utc(self, moment: datetime) -> datetime:
        return moment.astimezone(UTC).replace(tzinfo=None)


class IanaZone(Zone):
    def __init__(self, name: str, info: Any):
        self.name = name
        self.iana = name
        self.info = info

    def to_utc(self, local: datetime) -> tuple[datetime, Optional[str]]:
        first = local.replace(tzinfo=self.info, fold=0)
        second = local.replace(tzinfo=self.info, fold=1)
        if first.utcoffset() == second.utcoffset():
            return first.astimezone(UTC), None
        round_trip = first.astimezone(UTC).astimezone(self.info).replace(tzinfo=None)
        if round_trip == local:
            return first.astimezone(UTC), "ambiguous"
        # fold=0 in a gap uses the offset before the transition, as RFC 5545 asks.
        return first.astimezone(UTC), "nonexistent"

    def from_utc(self, moment: datetime) -> datetime:
        return moment.astimezone(self.info).replace(tzinfo=None)


@dataclass
class Observance:
    kind: str
    start: datetime
    offset_from: timedelta
    offset_to: timedelta
    rule: Optional["RRule"]
    rdates: list[datetime]


class VTimezoneZone(Zone):
    """A zone defined only by a VTIMEZONE block of the file."""

    def __init__(self, tzid: str, observances: list[Observance]):
        self.name = tzid
        self.iana = None
        self.observances = observances
        self._until_year = 0
        self._transitions: list[tuple[datetime, timedelta]] = []
        self.offsets = sorted({item.offset_to for item in observances} | {item.offset_from for item in observances})

    def _ensure(self, year: int) -> None:
        wanted = min(max(year + 2, 1970), 2200)
        if wanted <= self._until_year:
            return
        transitions = []
        limit = datetime(wanted, 12, 31, 23, 59, 59)
        for observance in self.observances:
            onsets = [observance.start] + list(observance.rdates)
            if observance.rule is not None:
                for onset in iter_rule_starts(observance.rule, observance.start, lambda value, o=observance: value - o.offset_from):
                    if onset > limit:
                        break
                    onsets.append(onset)
            for onset in onsets:
                if isinstance(onset, datetime) and onset <= limit:
                    transitions.append((onset - observance.offset_from, observance.offset_to))
        transitions.sort(key=lambda item: item[0])
        self._transitions = transitions
        self._until_year = wanted

    def offset_at(self, utc_naive: datetime) -> timedelta:
        self._ensure(utc_naive.year)
        current = None
        for instant, offset in self._transitions:
            if instant <= utc_naive:
                current = offset
            else:
                break
        if current is None:
            earliest = min(self.observances, key=lambda item: item.start)
            return earliest.offset_from
        return current

    def to_utc(self, local: datetime) -> tuple[datetime, Optional[str]]:
        if len({item.offset_to for item in self.observances}) == 1:
            return (local - self.observances[0].offset_to).replace(tzinfo=UTC), None
        valid = []
        for offset in self.offsets:
            candidate = local - offset
            if self.offset_at(candidate) == offset and candidate not in valid:
                valid.append(candidate)
        if valid:
            return min(valid).replace(tzinfo=UTC), ("ambiguous" if len(valid) > 1 else None)
        before = self.offset_at(local - max(self.offsets))
        return (local - before).replace(tzinfo=UTC), "nonexistent"

    def from_utc(self, moment: datetime) -> datetime:
        naive = moment.astimezone(UTC).replace(tzinfo=None)
        return naive + self.offset_at(naive)


def load_zoneinfo(name: str) -> Optional[Any]:
    if not name or name.strip() != name or ".." in name:
        return None
    if name in {"UTC", "Etc/UTC", "GMT", "Z"}:
        return UTC
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+, needs system data or the tzdata package

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown names and missing time zone data are expected
        return None


def zone_from_name(name: str) -> Optional[Zone]:
    info = load_zoneinfo(name)
    if info is None:
        return None
    if info is UTC:
        return Zone()
    return IanaZone(name, info)


def load_windows_zones(path: Optional[Path] = None) -> dict[str, str]:
    """Windows zone IDs and Outlook display names mapped to IANA names (read once per path)."""
    return dict(_windows_zones(str(path or WINDOWS_ZONES_ASSET)))


@lru_cache(maxsize=4)
def _windows_zones(location: str) -> tuple[tuple[str, str], ...]:
    path = Path(location)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    mapping: dict[str, str] = {}
    for section in ("zones", "display_names"):
        for key, value in (raw.get(section) or {}).items():
            if isinstance(key, str) and isinstance(value, str):
                mapping[key.strip().casefold()] = value
    return tuple(mapping.items())


def vtimezone_from_component(component: Component) -> Optional[VTimezoneZone]:
    tzid = (component.get("TZID").value.strip() if component.get("TZID") else "")
    observances = []
    for child in component.children:
        if child.name not in {"STANDARD", "DAYLIGHT"}:
            continue
        try:
            start_prop = child.get("DTSTART")
            start, _utc = parse_value(start_prop.value if start_prop else "19700101T000000")
            if not isinstance(start, datetime):
                start = datetime.combine(start, time())
            offset_to = parse_offset(child.get("TZOFFSETTO").value)
            offset_from = parse_offset(child.get("TZOFFSETFROM").value) if child.get("TZOFFSETFROM") else offset_to
            rule = parse_rrule(child.get("RRULE").value) if child.get("RRULE") else None
            rdates = []
            for prop in child.get_all("RDATE"):
                for part in prop.value.split(","):
                    value, _ = parse_value(part)
                    rdates.append(value if isinstance(value, datetime) else datetime.combine(value, time()))
        except (IcalError, AttributeError):
            continue
        observances.append(Observance(child.name, start, offset_from, offset_to, rule, rdates))
    if not tzid or not observances:
        return None
    return VTimezoneZone(tzid, observances)


class ZoneResolver:
    """Resolve TZID values: zoneinfo, Windows names, embedded VTIMEZONE, then local time."""

    def __init__(self, vtimezones: Optional[dict[str, VTimezoneZone]] = None, windows: Optional[dict[str, str]] = None, default_zone: str = "UTC"):
        self.vtimezones = vtimezones or {}
        self.windows = windows if windows is not None else load_windows_zones()
        self.default_name = default_zone or "UTC"
        self.default = zone_from_name(self.default_name)
        self._cache: dict[Optional[str], tuple[Zone, Optional[str]]] = {}

    def fallback(self, reason: str) -> tuple[Zone, str]:
        if self.default is not None:
            return self.default, f"{reason}; read as local time of {self.default_name}"
        return Zone(), f"{reason}; the wiki time zone {self.default_name} is not available, read as UTC"

    def resolve(self, tzid: Optional[str]) -> tuple[Zone, Optional[str]]:
        if tzid in self._cache:
            return self._cache[tzid]
        result = self._resolve(tzid)
        self._cache[tzid] = result
        return result

    def _resolve(self, tzid: Optional[str]) -> tuple[Zone, Optional[str]]:
        if tzid is None:
            return self.fallback("time without time zone (floating)")
        name = tzid.strip().strip('"')
        candidates = [name, name.lstrip("/")]
        tail = re.search(r"([A-Za-z_]+/[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)?)$", name)
        if tail:
            candidates.append(tail.group(1))
        for candidate in candidates:
            zone = zone_from_name(candidate)
            if zone is not None:
                return zone, None
        mapped = self.windows.get(name.casefold())
        if mapped:
            zone = zone_from_name(mapped)
            if zone is not None:
                return zone, f"time zone {name} mapped to {mapped}"
        embedded = self.vtimezones.get(name)
        if embedded is not None:
            return embedded, f"time zone {name} is not an IANA zone; offsets taken from the embedded VTIMEZONE"
        return self.fallback(f"time zone {name} is unknown")


def local_to_utc(local: datetime, zone: Zone) -> tuple[datetime, Optional[str]]:
    return zone.to_utc(local)


# ---------------------------------------------------------------------------
# recurrence rules


@dataclass
class RRule:
    freq: str
    interval: int = 1
    count: Optional[int] = None
    until: Optional[Moment] = None
    until_utc: bool = False
    byday: list[tuple[Optional[int], int]] = field(default_factory=list)
    bymonthday: list[int] = field(default_factory=list)
    bymonth: list[int] = field(default_factory=list)
    wkst: int = 0
    unsupported: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def expandable(self) -> bool:
        return self.freq in EXPANDABLE_FREQS and not self.unsupported


def parse_rrule(text: str) -> RRule:
    raw = text.strip()
    if raw.upper().startswith("RRULE:"):
        raw = raw[6:]
    parts: dict[str, str] = {}
    for item in raw.split(";"):
        if not item.strip():
            continue
        if "=" not in item:
            raise IcalError(f"invalid RRULE part {item!r}")
        key, value = item.split("=", 1)
        parts[key.strip().upper()] = value.strip()
    freq = parts.get("FREQ", "").upper()
    if not freq:
        raise IcalError("RRULE without FREQ")
    rule = RRule(freq=freq, raw=raw)
    if freq not in EXPANDABLE_FREQS:
        rule.unsupported.append(f"FREQ={freq}")
    try:
        if "INTERVAL" in parts:
            rule.interval = max(1, int(parts["INTERVAL"]))
        if "COUNT" in parts:
            rule.count = max(0, int(parts["COUNT"]))
        if "UNTIL" in parts:
            rule.until, rule.until_utc = parse_value(parts["UNTIL"])
        for item in filter(None, parts.get("BYDAY", "").upper().split(",")):
            match = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", item.strip())
            if not match:
                raise IcalError(f"invalid BYDAY value {item!r}")
            ordinal = int(match.group(1)) if match.group(1) else None
            rule.byday.append((ordinal, WEEKDAYS[match.group(2)]))
        rule.bymonthday = [int(item) for item in filter(None, parts.get("BYMONTHDAY", "").split(","))]
        rule.bymonth = [int(item) for item in filter(None, parts.get("BYMONTH", "").split(","))]
        if "WKST" in parts:
            rule.wkst = WEEKDAYS.get(parts["WKST"].upper(), 0)
    except ValueError as exc:
        raise IcalError(f"invalid RRULE {raw!r}: {exc}") from exc
    if any(not 1 <= month <= 12 for month in rule.bymonth) or any(day == 0 or abs(day) > 31 for day in rule.bymonthday):
        raise IcalError(f"invalid RRULE {raw!r}")
    for key in parts:
        if key not in EXPANDABLE_PARTS:
            rule.unsupported.append(key)
    if rule.freq in {"DAILY", "WEEKLY"} and any(ordinal for ordinal, _ in rule.byday):
        rule.unsupported.append("BYDAY with ordinal")
    return rule


def _month_days(year: int, month: int, rule: RRule, default_day: int) -> list[int]:
    last = calendar.monthrange(year, month)[1]
    by_monthday = set()
    for value in rule.bymonthday:
        day = value if value > 0 else last + value + 1
        if 1 <= day <= last:
            by_monthday.add(day)
    by_weekday = set()
    for ordinal, weekday in rule.byday:
        days = [day for day in range(1, last + 1) if date(year, month, day).weekday() == weekday]
        if ordinal is None:
            by_weekday.update(days)
        elif 1 <= abs(ordinal) <= len(days):
            by_weekday.add(days[ordinal - 1] if ordinal > 0 else days[ordinal])
    if rule.bymonthday and rule.byday:
        return sorted(by_monthday & by_weekday)
    if rule.bymonthday:
        return sorted(by_monthday)
    if rule.byday:
        return sorted(by_weekday)
    return [default_day] if default_day <= last else []


def _year_days(year: int, rule: RRule, start: date) -> list[date]:
    if rule.bymonth:
        months = sorted(set(rule.bymonth))
    elif rule.byday and not rule.bymonthday:
        # BYDAY without BYMONTH counts weekdays within the whole year.
        result = set()
        all_days = [date(year, 1, 1) + timedelta(days=offset) for offset in range(366 if calendar.isleap(year) else 365)]
        for ordinal, weekday in rule.byday:
            days = [day for day in all_days if day.weekday() == weekday]
            if ordinal is None:
                result.update(days)
            elif 1 <= abs(ordinal) <= len(days):
                result.add(days[ordinal - 1] if ordinal > 0 else days[ordinal])
        return sorted(result)
    else:
        months = [start.month]
    result = []
    for month in months:
        for day in _month_days(year, month, rule, start.day):
            result.append(date(year, month, day))
    return result


def _combine(day: date, template: Moment) -> Moment:
    if isinstance(template, datetime):
        return datetime.combine(day, template.time())
    return day


def _period_candidates(rule: RRule, start: Moment, index: int) -> list[Moment]:
    start_day = start.date() if isinstance(start, datetime) else start
    step = index * rule.interval
    if rule.freq == "DAILY":
        day = start_day + timedelta(days=step)
        if rule.bymonth and day.month not in rule.bymonth:
            return []
        if rule.bymonthday and day.day not in _month_days(day.year, day.month, RRule("MONTHLY", bymonthday=rule.bymonthday), day.day):
            return []
        if rule.byday and day.weekday() not in {weekday for _, weekday in rule.byday}:
            return []
        return [_combine(day, start)]
    if rule.freq == "WEEKLY":
        week_start = start_day - timedelta(days=(start_day.weekday() - rule.wkst) % 7) + timedelta(weeks=step)
        weekdays = sorted({weekday for _, weekday in rule.byday}) or [start_day.weekday()]
        days = sorted(week_start + timedelta(days=(weekday - rule.wkst) % 7) for weekday in weekdays)
        return [_combine(day, start) for day in days if not rule.bymonth or day.month in rule.bymonth]
    if rule.freq == "MONTHLY":
        month_index = start_day.year * 12 + start_day.month - 1 + step
        year, month = divmod(month_index, 12)
        month += 1
        if year > 9999 or (rule.bymonth and month not in rule.bymonth):
            return []
        return [_combine(date(year, month, day), start) for day in _month_days(year, month, rule, start_day.day)]
    if rule.freq == "YEARLY":
        year = start_day.year + step
        if year > 9999:
            return []
        return [_combine(day, start) for day in _year_days(year, rule, start_day)]
    return []


def iter_rule_starts(rule: RRule, start: Moment, to_utc: Optional[Callable[[datetime], datetime]] = None) -> Iterator[Moment]:
    """Starts produced by an RRULE in chronological order, DTSTART first (RFC 5545 3.8.5.3).

    ``to_utc`` converts a naive local start to naive UTC; it is needed when
    UNTIL is given in UTC and the series runs in a local time zone.
    """
    if not rule.expandable:
        return
    produced = 0
    generated = 0

    def beyond_until(candidate: Moment) -> bool:
        if rule.until is None:
            return False
        if isinstance(rule.until, datetime) and isinstance(candidate, datetime):
            if rule.until_utc and to_utc is not None:
                return to_utc(candidate) > rule.until
            return candidate > rule.until
        until_day = rule.until.date() if isinstance(rule.until, datetime) else rule.until
        candidate_day = candidate.date() if isinstance(candidate, datetime) else candidate
        return candidate_day > until_day

    if rule.count == 0 or beyond_until(start):
        return
    yield start
    produced = 1
    index = 0
    empty_periods = 0
    while generated < MAX_GENERATED:
        candidates = _period_candidates(rule, start, index)
        index += 1
        if not candidates:
            empty_periods += 1
            if empty_periods > 2000:
                return
            continue
        empty_periods = 0
        for candidate in candidates:
            generated += 1
            if candidate <= start:
                continue
            if beyond_until(candidate):
                return
            if rule.count is not None and produced >= rule.count:
                return
            produced += 1
            yield candidate


# ---------------------------------------------------------------------------
# events


@dataclass
class Attendee:
    email: str
    name: str = ""
    partstat: str = ""
    role: str = ""
    cutype: str = ""


@dataclass
class IcalEvent:
    uid: str
    sequence: int = 0
    dtstamp: Optional[datetime] = None
    created: Optional[datetime] = None
    last_modified: Optional[datetime] = None
    method: str = ""
    status: str = ""
    klass: str = ""
    summary: str = ""
    description: str = ""
    description_html: str = ""
    location: str = ""
    conference_url: str = ""
    conference_solution: str = ""
    organizer: Optional[Attendee] = None
    attendees: list[Attendee] = field(default_factory=list)
    all_day: bool = False
    start_local: Optional[Moment] = None
    end_local: Optional[Moment] = None
    zone: Optional[Zone] = None
    tzid: str = ""
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    rrule: str = ""
    rdates: list[Moment] = field(default_factory=list)
    exdates: list[Moment] = field(default_factory=list)
    recurrence_id: str = ""
    notes: list[str] = field(default_factory=list)
    uid_generated: bool = False

    @property
    def is_canceled(self) -> bool:
        return self.status.upper() == "CANCELLED" or self.method.upper() == "CANCEL"

    @property
    def record_uid(self) -> str:
        """The iCalUid of the record: an exception of a series gets its own key."""
        return f"{self.uid}#{self.recurrence_id}" if self.recurrence_id else self.uid

    def recurrence_text(self) -> str:
        """RFC 5545 recurrence set as text, with DTSTART so that it can be expanded later."""
        if self.recurrence_id:
            return f"RECURRENCE-ID:{self.recurrence_id}"
        if not self.rrule and not self.rdates:
            return ""
        lines = [_moment_line("DTSTART", self.start_local, self.zone, self.all_day)]
        if self.rrule:
            lines.append(f"RRULE:{self.rrule}")
        for value in self.rdates:
            lines.append(_moment_line("RDATE", value, self.zone, self.all_day))
        for value in self.exdates:
            lines.append(_moment_line("EXDATE", value, self.zone, self.all_day))
        return "\n".join(lines)


def _moment_line(name: str, value: Optional[Moment], zone: Optional[Zone], all_day: bool) -> str:
    if value is None:
        return f"{name}:"
    if all_day or not isinstance(value, datetime):
        day = value.date() if isinstance(value, datetime) else value
        return f"{name};VALUE=DATE:{format_basic(day)}"
    if zone is not None and zone.iana and zone.iana not in {"UTC", "Etc/UTC"}:
        return f"{name};TZID={zone.iana}:{format_basic(value)}"
    utc_value, _ = (zone or Zone()).to_utc(value)
    return f"{name}:{format_basic(utc_value.replace(tzinfo=None), utc=True)}"


@dataclass
class IcalDocument:
    method: str
    events: list[IcalEvent]
    notes: list[str]


def _attendee(prop: Prop) -> Optional[Attendee]:
    raw = prop.param("EMAIL") or prop.value
    raw = re.sub(r"^mailto:", "", raw.strip(), flags=re.IGNORECASE)
    if "@" not in raw:
        return None
    return Attendee(
        email=raw.strip().lower(),
        name=(prop.param("CN") or "").strip().strip('"'),
        partstat=(prop.param("PARTSTAT") or "").upper(),
        role=(prop.param("ROLE") or "").upper(),
        cutype=(prop.param("CUTYPE") or "").upper(),
    )


def _value_list(prop: Prop) -> list[str]:
    return [part for part in prop.value.split(",") if part.strip()]


def _instant(prop: Prop, resolver: ZoneResolver, notes: list[str], label: str) -> tuple[Moment, Optional[Zone], bool]:
    """(local value, zone, is_date) of a DTSTART-like property."""
    value, is_utc = parse_value(prop.value)
    if not isinstance(value, datetime) or (prop.param("VALUE") or "").upper() == "DATE":
        day = value.date() if isinstance(value, datetime) else value
        return day, None, True
    if is_utc:
        return value, Zone(), False
    zone, note = resolver.resolve(prop.param("TZID"))
    if note:
        notes.append(f"{label}: {note}")
    return value, zone, False


def _to_utc_noted(local: datetime, zone: Zone, notes: list[str], label: str) -> datetime:
    moment, flag = zone.to_utc(local)
    if flag == "ambiguous":
        notes.append(f"{label}: local time {local.isoformat()} occurs twice in {zone.name}; the first occurrence was used")
    elif flag == "nonexistent":
        notes.append(f"{label}: local time {local.isoformat()} does not exist in {zone.name}; the offset before the change was used")
    return moment


def _stamp(component: Component, name: str, resolver: ZoneResolver, notes: list[str]) -> Optional[datetime]:
    """A UTC timestamp property such as DTSTAMP, CREATED or LAST-MODIFIED, or None."""
    prop = component.get(name)
    if prop is None:
        return None
    try:
        value, is_utc = parse_value(prop.value)
    except IcalError:
        notes.append(f"{name} is invalid")
        return None
    if not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if is_utc:
        return value.replace(tzinfo=UTC)
    return resolver.resolve(prop.param("TZID"))[0].to_utc(value)[0]


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def event_from_component(component: Component, method: str, resolver: ZoneResolver) -> IcalEvent:
    notes: list[str] = []
    uid = component.text("UID").strip()
    generated = False
    if not uid:
        seed = component.text("SUMMARY") + "|" + (component.get("DTSTART").value if component.get("DTSTART") else "")
        uid = "generated-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
        generated = True
        notes.append("the event has no UID; a stable UID was derived from summary and start")
    event = IcalEvent(uid=uid, method=method.upper(), uid_generated=generated)
    try:
        event.sequence = int(component.text("SEQUENCE") or 0)
    except ValueError:
        notes.append("SEQUENCE is not a number; 0 was used")
    event.dtstamp = _stamp(component, "DTSTAMP", resolver, notes)
    event.created = _stamp(component, "CREATED", resolver, notes)
    event.last_modified = _stamp(component, "LAST-MODIFIED", resolver, notes)
    event.status = component.text("STATUS").strip().upper()
    event.klass = component.text("CLASS").strip().upper()
    event.summary = component.text("SUMMARY").strip()
    event.description = component.text("DESCRIPTION").strip()
    alt = component.get("X-ALT-DESC")
    if alt is not None and (alt.param("FMTTYPE") or "").lower() == "text/html":
        event.description_html = unescape_text(alt.value)
    event.location = component.text("LOCATION").strip()
    for name in CONFERENCE_PROPERTIES:
        prop = component.get(name)
        if prop is not None and prop.value.strip().lower().startswith(("https://", "http://")):
            event.conference_url = prop.value.strip()
            event.conference_solution = CONFERENCE_SOLUTIONS.get(name, "")
            break
    if not event.conference_url:
        found = CONFERENCE_URL_RE.search(event.location + "\n" + event.description)
        if found:
            event.conference_url = found.group(0)
    if event.conference_url and not event.conference_solution:
        host = re.sub(r"^https?://", "", event.conference_url.lower()).split("/", 1)[0]
        event.conference_solution = next((solution for suffix, solution in CONFERENCE_HOSTS
                                          if host == suffix or host.endswith("." + suffix)), "")
    organizer = component.get("ORGANIZER")
    event.organizer = _attendee(organizer) if organizer is not None else None
    event.attendees = [attendee for attendee in (_attendee(prop) for prop in component.get_all("ATTENDEE")) if attendee]

    start_prop = component.get("DTSTART")
    if start_prop is None:
        raise IcalError(f"event {uid} has no DTSTART")
    start_value, zone, is_date = _instant(start_prop, resolver, notes, "DTSTART")
    all_day_flag = (component.text("X-MICROSOFT-CDO-ALLDAYEVENT").strip().upper() == "TRUE")
    if not is_date and all_day_flag and isinstance(start_value, datetime) and start_value.time() == time():
        start_value, zone, is_date = start_value.date(), None, True
    event.all_day = is_date
    event.zone = zone
    event.tzid = start_prop.param("TZID") or ""
    event.start_local = start_value
    end_prop = component.get("DTEND")
    end_value: Optional[Moment] = None
    if end_prop is not None:
        try:
            end_value, end_zone, end_is_date = _instant(end_prop, resolver, notes, "DTEND")
            if is_date and not end_is_date:
                end_value = end_value.date() if isinstance(end_value, datetime) else end_value
            elif not is_date and isinstance(end_value, datetime) and end_zone is not None and zone is not None and end_zone is not zone:
                end_utc, _ = end_zone.to_utc(end_value)
                end_value = zone.from_utc(end_utc)
            elif not is_date and not isinstance(end_value, datetime):
                end_value = datetime.combine(end_value, time())
        except IcalError:
            notes.append("DTEND is invalid; the duration was ignored")
            end_value = None
    if end_value is None and component.get("DURATION") is not None:
        try:
            end_value = start_value + parse_duration(component.get("DURATION").value)
        except IcalError:
            notes.append("DURATION is invalid")
    if end_value is None:
        end_value = start_value + timedelta(days=1) if is_date else start_value
    event.end_local = end_value
    if is_date:
        event.starts_at = _midnight(start_value)
        event.ends_at = _midnight(end_value if isinstance(end_value, date) else start_value)
    else:
        event.starts_at = _to_utc_noted(start_value, zone, notes, "DTSTART")
        if isinstance(end_value, datetime) and end_value != start_value:
            event.ends_at = _to_utc_noted(end_value, zone, notes, "DTEND")
        else:
            event.ends_at = event.starts_at
    if event.ends_at < event.starts_at:
        notes.append("the event ends before it starts; the end was set to the start")
        event.ends_at = event.starts_at

    rrule = component.get("RRULE")
    if rrule is not None:
        event.rrule = rrule.value.strip()
        try:
            rule = parse_rrule(event.rrule)
            if rule.unsupported:
                notes.append(f"recurrence kept as text; not expandable: {', '.join(rule.unsupported)}")
        except IcalError as exc:
            notes.append(f"recurrence kept as text; {exc}")
    for prop in component.get_all("RDATE"):
        if (prop.param("VALUE") or "").upper() == "PERIOD":
            notes.append("RDATE periods are not supported and were ignored")
            continue
        event.rdates.extend(_series_values(prop, resolver, zone, is_date, notes))
    for prop in component.get_all("EXDATE"):
        event.exdates.extend(_series_values(prop, resolver, zone, is_date, notes))
    recurrence_id = component.get("RECURRENCE-ID")
    if recurrence_id is not None:
        try:
            value, rid_zone, rid_is_date = _instant(recurrence_id, resolver, notes, "RECURRENCE-ID")
            if rid_is_date:
                event.recurrence_id = format_basic(value)
            else:
                moment, _ = (rid_zone or Zone()).to_utc(value)
                event.recurrence_id = format_basic(moment.replace(tzinfo=None), utc=True)
        except IcalError:
            notes.append("RECURRENCE-ID is invalid")
    event.notes = notes
    return event


def _series_values(prop: Prop, resolver: ZoneResolver, zone: Optional[Zone], is_date: bool, notes: list[str]) -> list[Moment]:
    """RDATE/EXDATE values converted to the local time of the series zone."""
    values: list[Moment] = []
    for part in _value_list(prop):
        try:
            value, is_utc = parse_value(part)
        except IcalError:
            notes.append(f"{prop.name} value {part!r} is invalid")
            continue
        if is_date or not isinstance(value, datetime):
            values.append(value.date() if isinstance(value, datetime) else value)
            continue
        if is_utc:
            source_zone: Zone = Zone()
        else:
            source_zone, note = resolver.resolve(prop.param("TZID")) if prop.param("TZID") else ((zone or Zone()), None)
            if note:
                notes.append(f"{prop.name}: {note}")
        moment, _ = source_zone.to_utc(value)
        values.append((zone or Zone()).from_utc(moment))
    return values


def parse_ics(text: str, *, default_zone: str = "UTC", windows: Optional[dict[str, str]] = None) -> IcalDocument:
    roots, notes = parse_components(text)
    calendars = [root for root in roots if root.name == "VCALENDAR"] or [Component("VCALENDAR", [], roots)]
    events: list[IcalEvent] = []
    method = ""
    for calendar_component in calendars:
        cal_method = calendar_component.text("METHOD").strip().upper()
        method = method or cal_method
        vtimezones = {}
        for child in calendar_component.children:
            if child.name == "VTIMEZONE":
                zone = vtimezone_from_component(child)
                if zone is not None:
                    vtimezones[zone.name] = zone
        resolver = ZoneResolver(vtimezones, windows, default_zone)
        for child in calendar_component.children:
            if child.name != "VEVENT":
                continue
            try:
                events.append(event_from_component(child, cal_method, resolver))
            except IcalError as exc:
                notes.append(str(exc))
    if not events:
        notes.append("the calendar contains no readable VEVENT")
    return IcalDocument(method, events, notes)


# ---------------------------------------------------------------------------
# expansion


def _window_bound(value: Moment) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return _midnight(value)


def _series_from_text(text: str, resolver: ZoneResolver) -> tuple[Optional[Moment], Optional[Zone], bool, str, list[Moment], list[Moment]]:
    start = None
    zone: Optional[Zone] = None
    is_date = False
    rrule = ""
    rdates: list[Moment] = []
    exdates: list[Moment] = []
    notes: list[str] = []
    for line in unfold_lines(text):
        try:
            prop = parse_line(line)
        except IcalError:
            continue
        if prop.name == "DTSTART":
            start, zone, is_date = _instant(prop, resolver, notes, "DTSTART")
        elif prop.name == "RRULE":
            rrule = prop.value.strip()
    for line in unfold_lines(text):
        try:
            prop = parse_line(line)
        except IcalError:
            continue
        if prop.name == "RDATE":
            rdates.extend(_series_values(prop, resolver, zone, is_date, notes))
        elif prop.name == "EXDATE":
            exdates.extend(_series_values(prop, resolver, zone, is_date, notes))
    return start, zone, is_date, rrule, rdates, exdates


def expand_rrule(event: Any, start: Moment, end: Moment, overrides: Optional[list[str]] = None, *, default_zone: str = "UTC") -> list[tuple[datetime, datetime]]:
    """Occurrences of a recurring event that overlap the window [start, end).

    ``event`` is an IcalEvent or a CRM calendarEvent record (its ``data``
    mapping or the Record itself) with ``recurrence``, ``startsAt``, ``endsAt``
    and ``isFullDay``. Returns (start, end) pairs as UTC datetimes; all-day
    occurrences start at 00:00 UTC of their date. ``overrides`` lists
    RECURRENCE-ID values (as in IcalEvent.recurrence_id) whose occurrences are
    represented by records of their own and are left out. A rule part outside
    the supported subset raises IcalError.
    """
    window_start, window_end = _window_bound(start), _window_bound(end)
    if isinstance(event, IcalEvent):
        series_start, zone, is_date = event.start_local, event.zone, event.all_day
        rrule_text, rdates, exdates = event.rrule, list(event.rdates), list(event.exdates)
        duration_utc = (event.ends_at - event.starts_at) if event.starts_at and event.ends_at else timedelta(0)
    else:
        data = getattr(event, "data", event)
        text = str(data.get("recurrence") or "")
        resolver = ZoneResolver(default_zone=default_zone)
        series_start, zone, is_date, rrule_text, rdates, exdates = _series_from_text(text, resolver)
        starts_at = _parse_utc(data.get("startsAt"))
        ends_at = _parse_utc(data.get("endsAt"))
        if series_start is None and starts_at is not None:
            series_start, zone, is_date = (starts_at.date(), None, True) if data.get("isFullDay") else (starts_at.replace(tzinfo=None), Zone(), False)
        duration_utc = (ends_at - starts_at) if starts_at and ends_at else timedelta(0)
    if series_start is None:
        raise IcalError("the event has no start")
    if is_date and isinstance(series_start, datetime):
        series_start = series_start.date()
    zone = zone or Zone()
    rule = parse_rrule(rrule_text) if rrule_text else None
    if rule is not None and not rule.expandable:
        raise IcalError(f"recurrence rule not expandable: {', '.join(rule.unsupported)}")

    def occurrence(local: Moment) -> tuple[datetime, datetime]:
        if is_date:
            day = local.date() if isinstance(local, datetime) else local
            begin = _midnight(day)
            return begin, begin + (duration_utc if duration_utc > timedelta(0) else timedelta(days=1))
        begin, _ = zone.to_utc(local)
        return begin, begin + duration_utc

    def to_utc_naive(local: datetime) -> datetime:
        return zone.to_utc(local)[0].replace(tzinfo=None)

    starts: list[Moment] = []
    if rule is not None:
        for local in iter_rule_starts(rule, series_start, to_utc_naive):
            if occurrence(local)[0] >= window_end:
                break
            starts.append(local)
    else:
        starts.append(series_start)
    starts.extend(rdates)
    excluded = set()
    for value in exdates:
        excluded.add(occurrence(value)[0])
    for value in overrides or []:
        try:
            parsed, is_utc = parse_value(value)
        except IcalError:
            continue
        if isinstance(parsed, datetime):
            excluded.add(parsed.replace(tzinfo=UTC) if is_utc else zone.to_utc(parsed)[0])
        else:
            excluded.add(_midnight(parsed))
    result = []
    seen = set()
    for local in starts:
        begin, finish = occurrence(local)
        if begin in excluded or begin in seen:
            continue
        seen.add(begin)
        if begin < window_end and (finish > window_start or (finish == begin and begin >= window_start)):
            result.append((begin, finish))
    return sorted(result)


def _parse_utc(value: Any) -> Optional[datetime]:
    """A stored DATE_TIME value (UTC with Z) as an aware datetime."""
    return parse_instant(value) if isinstance(value, str) and value else None


def occurrences_for_records(records: list[Any], start: Moment, end: Moment, *, default_zone: str = "UTC",
                            include_canceled: bool = False) -> list[dict[str, Any]]:
    """Occurrences of calendarEvent records (``data`` mappings or Records) in [start, end).

    A series is expanded without the instances that have an exception record of
    their own ("<UID>#<RECURRENCE-ID>"); exceptions and single events appear as
    they are. Canceled events and exceptions are left out unless
    ``include_canceled``. A series whose rule cannot be expanded yields its
    first occurrence and ``"expanded": False``. Each entry holds ``record``
    (the data mapping), ``start`` and ``end`` (UTC datetimes) and ``expanded``.
    """
    datas = [getattr(record, "data", record) for record in records]
    overrides: dict[str, list[str]] = {}
    for data in datas:
        uid = str(data.get("iCalUid") or "")
        if "#" in uid:
            master, recurrence_id = uid.split("#", 1)
            overrides.setdefault(master, []).append(recurrence_id)
    window_start, window_end = _window_bound(start), _window_bound(end)
    result: list[dict[str, Any]] = []
    for data in datas:
        if data.get("isCanceled") and not include_canceled:
            continue
        starts_at = _parse_utc(data.get("startsAt"))
        ends_at = _parse_utc(data.get("endsAt")) or starts_at
        if starts_at is None:
            continue
        recurrence = str(data.get("recurrence") or "")
        if "RRULE" in recurrence or "RDATE" in recurrence:
            try:
                for begin, finish in expand_rrule(data, start, end, overrides.get(str(data.get("iCalUid") or ""), []), default_zone=default_zone):
                    result.append({"record": data, "start": begin, "end": finish, "expanded": True})
                continue
            except IcalError:
                if starts_at < window_end and ends_at >= window_start:
                    result.append({"record": data, "start": starts_at, "end": ends_at, "expanded": False})
                continue
        if starts_at < window_end and (ends_at > window_start or (ends_at == starts_at and starts_at >= window_start)):
            result.append({"record": data, "start": starts_at, "end": ends_at, "expanded": False})
    return sorted(result, key=lambda item: (item["start"], str(item["record"].get("iCalUid") or "")))
