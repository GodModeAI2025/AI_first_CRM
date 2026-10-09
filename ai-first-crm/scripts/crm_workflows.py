#!/usr/bin/env python3
"""CRM workflows following common CRM conventions: versioned definitions, runs on request, file artifacts.

A workflow lives in schema/crm/workflows/<id>.json (format lmwiki-crm-workflow/1)
with numbered versions (DRAFT, ACTIVE, DEACTIVATED, ARCHIVED). Only the ACTIVE
version reacts to its trigger. A version that was ever activated is frozen by
the sha256 of its trigger and steps; changing it is a validation error.

Nothing runs in the background. A run starts when the agent asks for it, and
`run plan --due` catches up what became due since the previous call: scheduled
(CRON) times in UTC, finished delays, and record events from meta/crm-events/
for DATABASE_EVENT triggers. Missed schedule times run once and are counted.
Event triggers ignore their own workflow's writes, data model migrations
(origin kind "migration") and writes more than one workflow level deep.

Every run is planned first. One plan holds one CRM transaction for all record
operations of its runs (crm_contract.plan_transaction, origin kind workflow),
the entries for meta/crm-runs/YYYY-MM.jsonl, the new meta/crm-workflow-state.json
(cursors, waiting runs, round-robin positions) and the outbox files: .eml
drafts, .ics events and HTTP request specifications. They are kept in the wiki
under records/_outbox/workflows/<workflow>/ (lint checks them, an erasure reaches
them); --outbox additionally copies them to a folder outside the wiki.
`run apply` writes exactly that plan. The skill never sends an
e-mail, never calls a URL and never runs code; CODE steps evaluate crm_formula
expressions only.

Commands (all need --target and --lock-token):
  validate | list
  save-draft --workflow W --definition-file F
  create-draft --workflow W --from-version N
  activate --workflow W --version N | deactivate --workflow W
  delete --workflow W --confirm-destructive
  run plan --workflow W [--record SELECTOR ... | --records-file F] [--payload-file F]
           [--answers-file F] [--due] [--now ISO] [--version N] --actor A --output PLAN [--outbox DIR]
  run apply --plan-file P --expect-plan-sha256 H [--confirm-destructive]
  run cancel --workflow W --run-id R --actor A

Output is JSON on stdout. Exit codes: 0 ok, 1 plan with errors, 2 error,
3 confirmation needed or stale plan, 4 invalid definition or plan, 5 partial failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import copy
import email.policy
import email.utils
import json
import random
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Optional

import crm_filters
import crm_formula
import portable_io
import secret_screen
from crm_contract import (
    COMPOSITE_SUBFIELDS, EMAIL_RE, EVENTS_DIR, INSTANT_RE, OUTBOX_DIR, OUTBOX_PATH_RE, ROLES_PATH, UUID_RE, WORKFLOWS_DIR,
    CrmError, DataModel, Planner, Record, apply_transaction, canonical_json, format_instant, load_datamodel, load_json_file,
    parse_instant, parse_record_link, plan_transaction, read_events, record_credential_findings, sha256_bytes, sha256_text,
    valid_actor, verify_plan,
)
from wiki_lock import require_lock

WORKFLOW_FORMAT = "lmwiki-crm-workflow/1"
RUN_FORMAT = "lmwiki-crm-run/1"
STATE_FORMAT = "lmwiki-crm-workflow-state/1"
PLAN_FORMAT = "lmwiki-crm-workflow-plan/1"
ANSWERS_FORMAT = "lmwiki-crm-workflow-answers/1"
REQUEST_FORMAT = "lmwiki-crm-http-request/1"
STATE_PATH = "meta/crm-workflow-state.json"
RUNS_DIR = "meta/crm-runs"

VERSION_STATUSES = ("DRAFT", "ACTIVE", "DEACTIVATED", "ARCHIVED")
TRIGGER_TYPES = ("DATABASE_EVENT", "MANUAL", "CRON", "WEBHOOK")
# Run statuses and step statuses of the common workflow model
RUN_STATUSES = ("NOT_STARTED", "RUNNING", "COMPLETED", "FAILED", "ENQUEUED", "STOPPING", "STOPPED")
STEP_STATUSES = ("NOT_STARTED", "RUNNING", "SUCCESS", "STOPPED", "FAILED", "FAILED_SAFELY", "PENDING", "SKIPPED")
# Action types of the common workflow model
ACTION_TYPES = (
    "CODE", "LOGIC_FUNCTION", "SEND_EMAIL", "SEND_CHAT_MESSAGE", "DRAFT_EMAIL", "CREATE_CALENDAR_EVENT",
    "CREATE_RECORD", "UPDATE_RECORD", "DELETE_RECORD", "UPSERT_RECORD", "FIND_RECORDS", "PICK_RECORD", "FORM",
    "FILTER", "IF_ELSE", "HTTP_REQUEST", "AI_AGENT", "CLASSIFY", "ITERATOR", "EMPTY", "DELAY", "WAIT_FOR_EVENT",
)
WRITE_ACTIONS = {"CREATE_RECORD", "UPDATE_RECORD", "DELETE_RECORD", "UPSERT_RECORD"}
# Objects the CRM maintains itself; workflows may read them but never write them.
OBJECTS_BLOCKED_FROM_AUTOMATION = (
    "workflow", "workflowVersion", "workflowRun", "workflowAutomatedTrigger", "workspaceMember", "dashboard",
    "message", "messageThread", "messageChannelMessageAssociation", "messageParticipant", "calendarEvent",
    "calendarEventParticipant", "calendarChannelEventAssociation",
)
# Event log operation -> database event actions it stands for ("upserted" means "created or updated").
EVENT_ACTIONS = {
    "create": {"created", "upserted"},
    "update": {"updated", "upserted"},
    "upsert": {"updated", "upserted"},
    "cascade-null": {"updated", "upserted"},
    "merge": {"updated", "upserted"},
    "delete": {"deleted"},
}
FORM_FIELD_TYPES = ("TEXT", "NUMBER", "DATE", "SELECT", "MULTI_SELECT", "RECORD")
AI_OUTPUT_TYPES = ("TEXT", "NUMBER", "BOOLEAN", "DATE", "LIST", "OBJECT")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
QUERY_MAX_RECORDS = 200  # maximum of a record search step
MAX_ITERATIONS = 10_000  # maximum items of an iterator
MAX_CASCADE_DEPTH = 1
MAX_RUNS_PER_PLAN = 500
MAX_EXECUTED_STEPS = 200_000
MAX_STEP_LOG_BYTES = 32_000  # bytes of a step output kept in the run log
MAX_PENDING_RUN_BYTES = 2_000_000
MAX_CRON_DAYS = 3700
REDACTED = "[credential removed]"

ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
STEP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
EVENT_NAME_RE = re.compile(r"^([a-z][a-zA-Z0-9_]*)\.(created|updated|deleted|upserted)$")
CLASSIFY_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
VARIABLE_RE = re.compile(r"\{\{([^{}]+)\}\}")
NUMBER_LITERAL_RE = re.compile(r"^-?\d+(\.\d+)?$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
SHARD_RE = re.compile(r"^\d{4}-\d{2}\.jsonl$")
RESERVED_STEP_IDS = {"trigger", "env"}

# Header names whose values are never logged
SENSITIVE_HEADER_NAMES = {
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token", "x-csrf-token",
    "x-amz-security-token", "x-goog-api-key", "api-key",
}
SENSITIVE_URL_PARAMS = {
    "api_key", "apikey", "api-key", "token", "access_token", "refresh_token", "id_token", "auth", "auth_token",
    "authentication", "secret", "client_secret", "private_key", "key", "sig", "signature", "password", "passwd", "pwd",
}
SENSITIVE_KEY_RE = re.compile(
    r"^(password|passwd|pwd|.*_?token|.*_?secret|authorization|api[_-]?key|private[_-]?key|client[_-]?secret|"
    r"x-?api-?key|x-?auth-?token|access[_-]?key)$",
    re.IGNORECASE,
)
# Credential forms the shared screen does not know and that workflow how-tos put into definitions.
EXTRA_CREDENTIAL_PATTERNS = (
    ("payment-api-key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9_/-]{20,}")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("basic-auth", re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/]{16,}={0,2}")),
)
URL_QUERY_RE = re.compile(r"([?&])([^=&#]+)=([^&#]*)")


class WorkflowError(CrmError):
    """A workflow request cannot be carried out."""


class InvalidDefinition(WorkflowError):
    """The workflow file does not validate; nothing may run from it."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


class StepFailure(Exception):
    """One step failed; the run fails or continues according to the step settings."""


# ---------------------------------------------------------------------------
# small utilities


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def deep(value: Any) -> Any:
    return copy.deepcopy(value)


def outside(target: Path, path: Path) -> bool:
    return path != target and target not in path.parents


def now_moment(value: Optional[str]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    moment = parse_instant(value)
    if moment is None:
        raise WorkflowError(f"--now {value!r} is not an ISO instant such as 2026-10-06T08:00:00Z")
    return moment.replace(microsecond=0)


def is_variable(value: Any) -> bool:
    return isinstance(value, str) and VARIABLE_RE.search(value) is not None


def version_hash(version: dict[str, Any]) -> str:
    """The frozen identity of a version: sha256 of its trigger and steps."""
    return sha256_bytes(canonical_json({"trigger": version.get("trigger"), "steps": version.get("steps")}))


def new_run_id() -> str:
    return "run-" + uuid.uuid4().hex[:16]


def shard_for(moment: datetime) -> str:
    return f"{RUNS_DIR}/{moment.strftime('%Y-%m')}.jsonl"


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:180]


# ---------------------------------------------------------------------------
# variables: {{trigger.object.name}}, {{stepId.field}}, [bracket segments]


def _first_param(expression: str) -> Optional[str]:
    param = ""
    index = 0
    while index < len(expression):
        char = expression[index]
        if char == "[":
            closing = expression.find("]", index + 1)
            if closing == -1:
                return None
            param += expression[index:closing + 1]
            index = closing + 1
            continue
        if char.isspace():
            break
        param += char
        index += 1
    return param


def parse_path(expression: str) -> tuple[bool, Any]:
    """(is_literal, value) for a literal token, else (False, [segments]) or (False, None)."""
    param = _first_param(expression.strip())
    if not param:
        return False, None
    literals = {"true": True, "false": False, "null": None, "undefined": None}
    if param in literals:
        return True, literals[param]
    if NUMBER_LITERAL_RE.fullmatch(param):
        return True, float(param) if "." in param else int(param)
    if len(param) > 1 and param[0] == param[-1] and param[0] in "\"'":
        return True, param[1:-1]
    for prefix in ("this.", "@root."):
        if param.startswith(prefix):
            param = param[len(prefix):]
    segments: list[str] = []
    current = ""
    index = 0
    while index < len(param):
        char = param[index]
        if char == "[":
            closing = param.find("]", index + 1)
            if closing == -1:
                return False, None
            if current:
                segments.append(current)
                current = ""
            segments.append(param[index + 1:closing])
            index = closing + 1
            if index < len(param) and param[index] == ".":
                index += 1
            continue
        if char == ".":
            if current:
                segments.append(current)
                current = ""
            index += 1
            continue
        current += char
        index += 1
    if current:
        segments.append(current)
    return False, segments or None


def token_root(expression: str) -> Optional[str]:
    literal, parsed = parse_path(expression)
    if literal or not parsed:
        return None
    return parsed[0]


def lookup(context: Any, segments: list[str]) -> Any:
    current = context
    for segment in segments:
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list):
            if segment == "length":
                current = len(current)
            elif segment.isdigit():
                position = int(segment)
                current = current[position] if position < len(current) else None
            else:
                return None
        elif isinstance(current, str) and segment == "length":
            current = len(current)
        else:
            return None
    return current


def template_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return dumps(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class Resolver:
    """Resolve {{...}} like common CRMs: a lone token keeps its type, embedded tokens become text."""

    def __init__(self, context: dict[str, Any], *, keep_env: bool = False):
        self.context = context
        self.keep_env = keep_env
        self.missing: list[str] = []

    def token(self, expression: str, raw: str) -> Any:
        literal, parsed = parse_path(expression)
        if literal:
            return parsed
        if not parsed:
            self.missing.append(expression.strip())
            return None
        if parsed[0] == "env":
            return raw if self.keep_env else None
        value = lookup(self.context, parsed)
        if value is None:
            self.missing.append(expression.strip())
        return value

    def resolve(self, value: Any) -> Any:
        if isinstance(value, str):
            matches = list(VARIABLE_RE.finditer(value))
            if not matches:
                return value
            if len(matches) == 1 and matches[0].group(0) == value:
                return deep(self.token(matches[0].group(1), value))

            def replace(match: re.Match) -> str:
                if self.keep_env and token_root(match.group(1)) == "env":
                    return match.group(0)
                return template_text(self.token(match.group(1), match.group(0)))

            return VARIABLE_RE.sub(replace, value)
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if isinstance(value, dict):
            resolved: dict[str, Any] = {}
            for key, item in value.items():
                new_key = self.resolve(key) if isinstance(key, str) else key
                resolved[template_text(new_key)] = self.resolve(item)
            return resolved
        return value


def variable_roots(value: Any) -> list[tuple[str, Optional[str]]]:
    """(token, root) for every non-literal variable in a nested value."""
    found: list[tuple[str, Optional[str]]] = []

    def walk(item: Any) -> None:
        if isinstance(item, str):
            for match in VARIABLE_RE.finditer(item):
                literal, parsed = parse_path(match.group(1))
                if not literal:
                    found.append((match.group(0), parsed[0] if parsed else None))
        elif isinstance(item, list):
            for element in item:
                walk(element)
        elif isinstance(item, dict):
            for key, element in item.items():
                walk(key)
                walk(element)

    walk(value)
    return found


# ---------------------------------------------------------------------------
# credentials and log size


def _credential_spans(text: str) -> list[tuple[int, int, str]]:
    spans = []
    for kind, pattern in tuple(secret_screen.PATTERNS) + EXTRA_CREDENTIAL_PATTERNS:
        for match in pattern.finditer(text):
            if not secret_screen.is_placeholder(kind, match.group(0)):
                spans.append((match.start(), match.end(), kind))
    return spans


def credential_kinds(text: str) -> list[str]:
    return sorted({kind for _start, _end, kind in _credential_spans(text)})


def redact_text(text: str) -> tuple[str, int]:
    spans = _credential_spans(text)
    if not spans:
        return text, 0
    merged: list[list[int]] = []
    for start, end, _kind in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    parts = []
    last = 0
    for start, end in merged:
        parts.append(text[last:start])
        parts.append(REDACTED)
        last = end
    parts.append(text[last:])
    return "".join(parts), len(merged)


def is_sensitive_key(name: str) -> bool:
    return name.lower() in SENSITIVE_HEADER_NAMES or SENSITIVE_KEY_RE.match(name) is not None


def is_env_reference(value: Any) -> bool:
    if isinstance(value, dict):
        return set(value) <= {"env", "prefix"} and isinstance(value.get("env"), str) and ENV_NAME_RE.fullmatch(value["env"]) is not None
    if isinstance(value, str):
        tokens = VARIABLE_RE.findall(value)
        return bool(tokens) and all(token_root(token) == "env" for token in tokens)
    return False


def redact(value: Any, *, key_names: bool) -> tuple[Any, int]:
    """Replace credential-shaped values; with key_names also values under secret-looking keys."""
    count = 0

    def walk(item: Any) -> Any:
        nonlocal count
        if isinstance(item, str):
            text, found = redact_text(item)
            count += found
            return text
        if isinstance(item, list):
            return [walk(element) for element in item]
        if isinstance(item, dict):
            result = {}
            for key, element in item.items():
                if (
                    key_names and isinstance(key, str) and is_sensitive_key(key)
                    and element not in (None, "", REDACTED) and not is_env_reference(element)
                    and not isinstance(element, (dict, list, bool))
                ):
                    result[key] = REDACTED
                    count += 1
                else:
                    result[key] = walk(element)
            return result
        return item

    return walk(value), count


def truncate_for_log(value: Any, limit: int = MAX_STEP_LOG_BYTES) -> Any:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    if len(encoded) <= limit:
        return value
    return {"truncated": True, "bytes": len(encoded), "preview": encoded[:limit].decode("utf-8", errors="ignore")}


def redact_url(url: str) -> str:
    def replace(match: re.Match) -> str:
        prefix, name, value = match.groups()
        if name.lower() in SENSITIVE_URL_PARAMS and value and not is_env_reference(value):
            return f"{prefix}{name}={REDACTED}"
        return match.group(0)

    return redact_text(URL_QUERY_RE.sub(replace, url))[0]


# ---------------------------------------------------------------------------
# cron schedules (UTC, five fields, Vixie day matching)

MONTH_NAMES = {name: index for index, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
WEEKDAY_NAMES = {name: index for index, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))}


class CronSchedule:
    def __init__(self, pattern: str):
        parts = pattern.split()
        if len(parts) != 5:
            raise ValueError("a cron pattern has five fields: minute hour day-of-month month day-of-week (UTC)")
        self.pattern = " ".join(parts)
        self.minutes = sorted(self._field(parts[0], 0, 59, {}))
        self.hours = sorted(self._field(parts[1], 0, 23, {}))
        self.days = self._field(parts[2], 1, 31, {})
        self.months = self._field(parts[3], 1, 12, MONTH_NAMES)
        self.weekdays = {0 if value == 7 else value for value in self._field(parts[4], 0, 7, WEEKDAY_NAMES)}
        self.days_star = parts[2].startswith("*")
        self.weekdays_star = parts[4].startswith("*")

    @staticmethod
    def _value(text: str, names: dict[str, int]) -> int:
        upper = text.upper()
        if upper in names:
            return names[upper]
        if not text.isdigit():
            raise ValueError(f"invalid cron value {text!r}")
        return int(text)

    def _field(self, text: str, low: int, high: int, names: dict[str, int]) -> set[int]:
        values: set[int] = set()
        for part in text.split(","):
            if not part:
                raise ValueError(f"empty part in cron field {text!r}")
            base, _, step_text = part.partition("/")
            step = 1
            if step_text:
                if not step_text.isdigit() or int(step_text) < 1:
                    raise ValueError(f"invalid cron step in {part!r}")
                step = int(step_text)
            if base == "*":
                start, end = low, high
            elif "-" in base:
                first, second = base.split("-", 1)
                start, end = self._value(first, names), self._value(second, names)
            else:
                start = self._value(base, names)
                end = high if step_text else start
            if not low <= start <= end <= high:
                raise ValueError(f"cron value out of range in {part!r} ({low}-{high})")
            values.update(range(start, end + 1, step))
        return values

    def day_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        in_days = day.day in self.days
        in_weekdays = (day.isoweekday() % 7) in self.weekdays
        if self.days_star or self.weekdays_star:
            return in_days and in_weekdays
        return in_days or in_weekdays

    def slots(self, after: datetime, until: datetime) -> tuple[int, Optional[datetime], Optional[datetime]]:
        """Count schedule times t with after < t <= until; return (count, first, last)."""
        if until <= after:
            return 0, None, None
        first_day = after.date()
        last_day = until.date()
        if (last_day - first_day).days > MAX_CRON_DAYS:
            first_day = last_day - timedelta(days=MAX_CRON_DAYS)
        count = 0
        first: Optional[datetime] = None
        last: Optional[datetime] = None
        per_day = len(self.hours) * len(self.minutes)
        day = first_day
        while day <= last_day:
            if self.day_matches(day):
                if after.date() < day < last_day:
                    count += per_day
                    start = datetime(day.year, day.month, day.day, self.hours[0], self.minutes[0], tzinfo=timezone.utc)
                    first = first or start
                    last = datetime(day.year, day.month, day.day, self.hours[-1], self.minutes[-1], tzinfo=timezone.utc)
                else:
                    for hour in self.hours:
                        for minute in self.minutes:
                            moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)
                            if after < moment <= until:
                                count += 1
                                first = first or moment
                                last = moment
            day += timedelta(days=1)
        return count, first, last


def cron_pattern(settings: dict[str, Any]) -> str:
    """Cron pattern of a schedule trigger."""
    kind = settings.get("type")
    if kind in (None, "CUSTOM") and isinstance(settings.get("pattern"), str):
        return settings["pattern"]
    schedule = settings.get("schedule") if isinstance(settings.get("schedule"), dict) else {}

    def number(key: str, low: int, high: Optional[int]) -> int:
        value = schedule.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
            limit = f"{low} to {high}" if high is not None else f"at least {low}"
            raise ValueError(f"schedule.{key} must be a whole number {limit}")
        return value

    if kind == "DAYS":
        return f"{number('minute', 0, 59)} {number('hour', 0, 23)} */{number('day', 1, None)} * *"
    if kind == "HOURS":
        return f"{number('minute', 0, 59)} */{number('hour', 1, None)} * * *"
    if kind == "MINUTES":
        return f"*/{number('minute', 1, 60)} * * * *"
    raise ValueError("CRON needs settings.pattern or settings.type DAYS, HOURS, MINUTES or CUSTOM with a schedule")


# ---------------------------------------------------------------------------
# flow graph


class FlowGraph:
    def __init__(self, trigger: dict[str, Any], steps: list[dict[str, Any]]):
        self.trigger = trigger if isinstance(trigger, dict) else {}
        self.order = [step["id"] for step in steps if isinstance(step, dict) and isinstance(step.get("id"), str)]
        self.steps = {step["id"]: step for step in steps if isinstance(step, dict) and isinstance(step.get("id"), str)}
        self.trigger_next = [item for item in (self.trigger.get("nextStepIds") or []) if isinstance(item, str)]
        self._bodies: dict[str, list[str]] = {}
        self._parents: dict[str, list[str]] = {}

    def input(self, step_id: str) -> Any:
        settings = self.steps.get(step_id, {}).get("settings")
        return settings.get("input") if isinstance(settings, dict) else None

    def next_ids(self, step_id: str) -> list[str]:
        value = self.steps.get(step_id, {}).get("nextStepIds") or []
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    def branches(self, step_id: str) -> list[dict[str, Any]]:
        if self.steps.get(step_id, {}).get("type") != "IF_ELSE":
            return []
        inp = self.input(step_id)
        value = inp.get("branches") if isinstance(inp, dict) else None
        return [branch for branch in value if isinstance(branch, dict)] if isinstance(value, list) else []

    def branch_ids(self, step_id: str) -> list[str]:
        ids: list[str] = []
        for branch in self.branches(step_id):
            for item in branch.get("nextStepIds") or []:
                if isinstance(item, str) and item not in ids:
                    ids.append(item)
        return ids

    def loop_ids(self, step_id: str) -> list[str]:
        if self.steps.get(step_id, {}).get("type") != "ITERATOR":
            return []
        inp = self.input(step_id)
        value = inp.get("initialLoopStepIds") if isinstance(inp, dict) else None
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    def outgoing(self, step_id: str) -> list[str]:
        result: list[str] = []
        for item in self.next_ids(step_id) + self.branch_ids(step_id) + self.loop_ids(step_id):
            if item not in result:
                result.append(item)
        return result

    def loop_body(self, iterator_id: str, _stack: Optional[set[str]] = None) -> list[str]:
        """Steps of an iterator's loop, as getAllStepIdsInLoop finds them."""
        if iterator_id in self._bodies:
            return self._bodies[iterator_id]
        stack = set(_stack or set()) | {iterator_id}
        body: list[str] = []
        seen: set[str] = set()

        def walk(ids: list[str]) -> None:
            for step_id in ids:
                if step_id in seen or step_id == iterator_id or step_id not in self.steps:
                    continue
                seen.add(step_id)
                body.append(step_id)
                if self.steps[step_id].get("type") == "ITERATOR" and step_id not in stack:
                    for nested in self.loop_body(step_id, stack):
                        if nested not in seen and nested != iterator_id:
                            seen.add(nested)
                            body.append(nested)
                walk(self.branch_ids(step_id))
                following = self.next_ids(step_id)
                if iterator_id in following:
                    continue
                walk(following)

        walk(self.loop_ids(iterator_id))
        self._bodies[iterator_id] = body
        return body

    def parents(self, step_id: str) -> list[str]:
        """Parent steps as findParentSteps sees them; an iterator ignores the edges closing its loop."""
        if step_id not in self._parents:
            found = []
            body = set(self.loop_body(step_id)) if self.steps.get(step_id, {}).get("type") == "ITERATOR" else set()
            for candidate in self.order:
                if candidate in body:
                    continue
                if step_id in self.next_ids(candidate) or step_id in self.branch_ids(candidate):
                    found.append(candidate)
            self._parents[step_id] = found
        return self._parents[step_id]

    def reachable(self) -> set[str]:
        seen: set[str] = set()
        queue = list(self.trigger_next)
        while queue:
            step_id = queue.pop(0)
            if step_id in seen or step_id not in self.steps:
                continue
            seen.add(step_id)
            queue.extend(self.outgoing(step_id))
        return seen

    def ancestors(self, step_id: str) -> set[str]:
        reverse: dict[str, set[str]] = {}
        for source in self.order:
            for child in self.outgoing(source):
                reverse.setdefault(child, set()).add(source)
        seen: set[str] = set()
        queue = list(reverse.get(step_id, ()))
        while queue:
            item = queue.pop()
            if item in seen:
                continue
            seen.add(item)
            queue.extend(reverse.get(item, ()))
        return seen

    def cycle(self) -> Optional[list[str]]:
        """A cycle that does not close an iterator loop, or None."""
        back_edges = set()
        for step_id in self.order:
            if self.steps[step_id].get("type") == "ITERATOR":
                for member in self.loop_body(step_id):
                    if step_id in self.next_ids(member):
                        back_edges.add((member, step_id))
        state: dict[str, int] = {}
        path: list[str] = []

        def visit(node: str) -> Optional[list[str]]:
            state[node] = 1
            path.append(node)
            for child in self.outgoing(node):
                if (node, child) in back_edges or child not in self.steps:
                    continue
                if state.get(child) == 1:
                    return path[path.index(child):] + [child]
                if state.get(child) is None:
                    found = visit(child)
                    if found:
                        return found
            path.pop()
            state[node] = 2
            return None

        for node in self.order:
            if state.get(node) is None:
                found = visit(node)
                if found:
                    return found
        return None


# ---------------------------------------------------------------------------
# definition validation


TOP_KEYS = {"format", "id", "name", "description", "versions"}
VERSION_KEYS = {"version", "status", "created_at", "activated_at", "sha256", "trigger", "steps", "note"}
TRIGGER_KEYS = {"type", "name", "settings", "nextStepIds", "position"}
STEP_KEYS = {"id", "type", "name", "settings", "nextStepIds", "valid", "position"}
CODE_FORBIDDEN_KEYS = ("code", "source", "script", "javascript", "typescript", "logicFunctionId", "function")


def manual_availability(settings: dict[str, Any]) -> tuple[str, Optional[str]]:
    availability = settings.get("availability")
    if availability is None:
        return "GLOBAL", settings.get("objectType")
    if isinstance(availability, str):
        return availability, settings.get("objectType")
    if isinstance(availability, dict):
        return str(availability.get("type") or ""), availability.get("objectNameSingular") or settings.get("objectType")
    return "", None


def normalize_operand(operand: Any) -> Optional[str]:
    if not isinstance(operand, str):
        return None
    upper = operand.upper()
    if upper in crm_filters.OPERANDS:
        return upper
    if upper in ("GREATER_THAN", "LESS_THAN"):
        return upper + "_OR_EQUAL"
    return crm_filters.LEGACY_OPERANDS.get(operand.replace("_", "").lower())


def compact_to_groups(condition: Any, groups: list[dict[str, Any]], filters: list[dict[str, Any]], parent: Optional[str] = None) -> str:
    """Translate a compact condition {op, conditions:[{left, operand, value, type}]} into step filters."""
    group_id = f"g{len(groups) + 1}"
    if isinstance(condition, dict) and "conditions" in condition:
        operator = str(condition.get("op") or condition.get("logicalOperator") or "AND").upper()
        groups.append({"id": group_id, "logicalOperator": operator, **({"parentStepFilterGroupId": parent} if parent else {})})
        for item in condition.get("conditions") or []:
            if isinstance(item, dict) and "conditions" in item:
                compact_to_groups(item, groups, filters, group_id)
            else:
                filters.append(_compact_filter(item, group_id, len(filters)))
        return group_id
    groups.append({"id": group_id, "logicalOperator": "AND", **({"parentStepFilterGroupId": parent} if parent else {})})
    filters.append(_compact_filter(condition, group_id, len(filters)))
    return group_id


def _compact_filter(item: Any, group_id: str, index: int) -> dict[str, Any]:
    item = item if isinstance(item, dict) else {}
    left = item.get("left", item.get("stepOutputKey"))
    return {
        "id": f"f{index + 1}", "type": item.get("type") or "", "stepOutputKey": left, "operand": item.get("operand"),
        "value": item.get("value"), "stepFilterGroupId": group_id,
    }


def _check_filters(container: dict[str, Any], where: str, errors: list[str]) -> None:
    if "condition" in container:
        groups: list[dict[str, Any]] = []
        filters: list[dict[str, Any]] = []
        condition = container["condition"]
        if not isinstance(condition, dict):
            errors.append(f"{where}: condition must be an object")
            return
        compact_to_groups(condition, groups, filters)
    else:
        groups = container.get("stepFilterGroups") or []
        filters = container.get("stepFilters") or []
    if not isinstance(groups, list) or not isinstance(filters, list):
        errors.append(f"{where}: stepFilterGroups and stepFilters must be lists")
        return
    group_ids = set()
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("id"), str):
            errors.append(f"{where}: every filter group needs an id")
            continue
        if str(group.get("logicalOperator") or "").upper() not in ("AND", "OR"):
            errors.append(f"{where}: filter group {group.get('id')} needs logicalOperator AND or OR")
        group_ids.add(group["id"])
    for group in groups:
        if isinstance(group, dict) and group.get("parentStepFilterGroupId") not in (None, "") and group.get("parentStepFilterGroupId") not in group_ids:
            errors.append(f"{where}: filter group {group.get('id')} names an unknown parent group")
    for item in filters:
        if not isinstance(item, dict):
            errors.append(f"{where}: every filter must be an object")
            continue
        if not isinstance(item.get("stepOutputKey"), str) or not item.get("stepOutputKey"):
            errors.append(f"{where}: filter {item.get('id')} needs the value to test (stepOutputKey or left), for example {{{{trigger.object.stage}}}}")
        if normalize_operand(item.get("operand")) is None:
            errors.append(f"{where}: filter {item.get('id')} has an unknown operand {item.get('operand')!r}")
        if group_ids and item.get("stepFilterGroupId") not in group_ids:
            errors.append(f"{where}: filter {item.get('id')} names an unknown filter group")


def _check_object(dm: DataModel, value: Any, where: str, errors: list[str], *, write: bool = False, label: str = "objectName") -> Optional[str]:
    if is_variable(value):
        return None
    if not isinstance(value, str) or not value:
        errors.append(f"{where}: {label} is required")
        return None
    if value not in dm.objects:
        errors.append(f"{where}: unknown object {value!r}")
        return None
    if write and value in OBJECTS_BLOCKED_FROM_AUTOMATION:
        errors.append(f"{where}: workflows may read {value} records but never create, update or delete them (objects blocked from automation)")
    return value


def _field_key_problem(dm: DataModel, object_name: str, key: str) -> Optional[str]:
    if key in ("id",) or is_variable(key):
        return None
    fields = dm.fields(object_name)
    base = key.split(".", 1)[0]
    if base not in fields and base.endswith("Id") and fields.get(base[:-2], {}).get("type") in ("RELATION", "MORPH_RELATION"):
        return None
    if base not in fields:
        return f"{object_name} has no field {base}"
    definition = fields[base]
    if definition.get("type") == "RELATION" and definition.get("relation", {}).get("type") == "ONE_TO_MANY":
        return f"{object_name}.{base} is the inverse side of a relation; set the field on the other object"
    return None


def _check_record(dm: DataModel, object_name: Optional[str], record: Any, where: str, errors: list[str]) -> None:
    if not isinstance(record, dict):
        errors.append(f"{where}: objectRecord must be an object of field values")
        return
    if object_name is None:
        return
    for key in record:
        problem = _field_key_problem(dm, object_name, key)
        if problem:
            errors.append(f"{where}: {problem}")


def _check_number_parts(value: Any, keys: tuple[str, ...], where: str, label: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append(f"{where}: {label} must be an object with {', '.join(keys)}")
        return
    for key, part in value.items():
        if key not in keys:
            errors.append(f"{where}: {label}.{key} is not supported ({', '.join(keys)})")
        elif not is_variable(part) and part not in (None, "") and (isinstance(part, bool) or not isinstance(part, (int, float)) or part < 0):
            errors.append(f"{where}: {label}.{key} must be a non-negative number")


def _validate_step(dm: DataModel, graph: FlowGraph, step: dict[str, Any], where: str, errors: list[str], warnings: list[str]) -> None:
    kind = step.get("type")
    settings = step.get("settings")
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        errors.append(f"{where}: settings must be an object")
        return
    handling = settings.get("errorHandlingOptions")
    if handling is not None:
        if not isinstance(handling, dict):
            errors.append(f"{where}: errorHandlingOptions must be an object")
        else:
            retry = (handling.get("retryOnFailure") or {}).get("value", 0) if isinstance(handling.get("retryOnFailure"), dict) else 0
            if isinstance(retry, bool):
                retry = int(retry)
            if not isinstance(retry, int) or not 0 <= retry <= 3:
                errors.append(f"{where}: retryOnFailure.value must be 0 to 3")
            elif retry:
                warnings.append(f"{where}: retryOnFailure is accepted but has no effect; steps run deterministically on request")
            proceed = handling.get("continueOnFailure")
            if proceed is not None and (not isinstance(proceed, dict) or not isinstance(proceed.get("value"), bool)):
                errors.append(f"{where}: continueOnFailure must be {{\"value\": true|false}}")
    inp = settings.get("input")
    if kind in ("FORM",):
        if not isinstance(inp, list) or not inp:
            errors.append(f"{where}: a FORM needs input: a non-empty list of fields")
            return
    elif kind == "EMPTY":
        if inp not in (None, {}):
            errors.append(f"{where}: EMPTY takes no input")
        return
    else:
        if not isinstance(inp, dict):
            errors.append(f"{where}: settings.input must be an object")
            return

    if kind == "LOGIC_FUNCTION":
        errors.append(f"{where}: LOGIC_FUNCTION is not supported: app logic functions need a server runtime; use CODE with formulas or do the step by hand")
    elif kind == "CODE":
        for key in CODE_FORBIDDEN_KEYS:
            if key in inp:
                errors.append(
                    f"{where}: CODE does not run JavaScript, Python or any other code ({key!r} found); the skill has no sandbox. "
                    "Write the logic as crm_formula expressions in settings.input.formulas, for example "
                    "{\"weighted\": \"round(amount * probability / 100)\"}"
                )
        params = inp.get("logicFunctionInput") or {}
        if not isinstance(params, dict):
            errors.append(f"{where}: logicFunctionInput must be an object of named inputs")
            params = {}
        for name in params:
            if not isinstance(name, str) or not IDENTIFIER_RE.fullmatch(name):
                errors.append(f"{where}: input name {name!r} must be a simple name (letters, digits, underscore)")
        formulas = inp.get("formulas")
        if not isinstance(formulas, dict) or not formulas:
            errors.append(f"{where}: CODE needs settings.input.formulas: an object of named crm_formula expressions")
        else:
            known = set(params)
            for name, source in formulas.items():
                if not isinstance(name, str) or not all(IDENTIFIER_RE.fullmatch(part) for part in name.split(".")):
                    errors.append(f"{where}: formula name {name!r} must be a name or dotted names such as contact.email")
                    continue
                for problem in crm_formula.check_formula(source, known):
                    errors.append(f"{where}: formula {name}: {problem}")
                if "." not in name:
                    known.add(name)
    elif kind in WRITE_ACTIONS:
        object_name = _check_object(dm, inp.get("objectName"), where, errors, write=True)
        if kind in ("CREATE_RECORD", "UPDATE_RECORD", "UPSERT_RECORD"):
            _check_record(dm, object_name, inp.get("objectRecord", {}), where, errors)
        if kind in ("UPDATE_RECORD", "DELETE_RECORD") and not inp.get("objectRecordId"):
            errors.append(f"{where}: objectRecordId is required (a record id or a variable such as {{{{trigger.object.id}}}})")
        if kind == "UPDATE_RECORD":
            update_keys = inp.get("fieldsToUpdate")
            if update_keys is not None:
                record = inp.get("objectRecord") if isinstance(inp.get("objectRecord"), dict) else {}
                if not isinstance(update_keys, list) or not update_keys:
                    errors.append(f"{where}: fieldsToUpdate must be a non-empty list")
                else:
                    for name in update_keys:
                        if name not in record:
                            errors.append(f"{where}: fieldsToUpdate names {name!r}, which objectRecord does not set")
        if kind == "UPSERT_RECORD" and object_name:
            record = inp.get("objectRecord") if isinstance(inp.get("objectRecord"), dict) else {}
            match_fields = inp.get("matchFields")
            unique = [name for name, definition in dm.fields(object_name).items() if definition.get("unique")]
            if match_fields is not None:
                if not isinstance(match_fields, list) or not match_fields:
                    errors.append(f"{where}: matchFields must be a non-empty list of field names")
                else:
                    for name in match_fields:
                        problem = _field_key_problem(dm, object_name, str(name))
                        if problem:
                            errors.append(f"{where}: matchFields: {problem}")
            elif "id" not in record and not any(name in record or any(key.startswith(name + ".") for key in record) for name in unique):
                errors.append(f"{where}: UPSERT_RECORD needs id or a value for a unique field of {object_name} ({', '.join(unique) or 'none'}) or matchFields")
        if kind == "DELETE_RECORD" and inp.get("destroy") not in (None, True, False):
            errors.append(f"{where}: destroy must be true or false")
    elif kind == "FIND_RECORDS":
        object_name = _check_object(dm, inp.get("objectName"), where, errors)
        spec = inp.get("filter")
        if isinstance(spec, dict) and ("recordFilters" in spec or "recordFilterGroups" in spec):
            errors.append(f"{where}: record filters with field metadata ids are not supported; write the filter as {{\"op\": \"AND\", \"conditions\": [{{\"field\": ..., \"operand\": ..., \"value\": ...}}]}}")
        elif spec is not None and object_name:
            probe = deep(spec)

            def neutral(node: Any) -> None:
                if isinstance(node, dict):
                    if "conditions" in node:
                        for child in node.get("conditions") or []:
                            neutral(child)
                    elif is_variable(node.get("value")) and normalize_operand(node.get("operand") or node.get("operator")) == "IS_RELATIVE":
                        node["value"] = "PAST_1_DAY"
                elif isinstance(node, list):
                    for child in node:
                        neutral(child)

            neutral(probe)
            for problem in crm_filters.validate_filter(dm, object_name, probe):
                errors.append(f"{where}: filter: {problem}")
        order = inp.get("orderBy")
        if order is not None and not isinstance(order, (list, dict)):
            errors.append(f"{where}: orderBy must be a list of {{\"field\": ..., \"direction\": \"asc\" or \"desc\"}}")
        elif order is not None and object_name:
            for sort in _sorts(order):
                try:
                    crm_filters.field_type(dm, object_name, sort.get("field") or "")
                except crm_filters.FilterError as exc:
                    errors.append(f"{where}: orderBy: {exc}")
        for key in ("limit", "offset"):
            value = inp.get(key)
            if value is not None and not is_variable(value) and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                errors.append(f"{where}: {key} must be a whole number")
        if isinstance(inp.get("limit"), int) and not isinstance(inp.get("limit"), bool) and inp["limit"] > QUERY_MAX_RECORDS:
            warnings.append(f"{where}: limit {inp['limit']} is reduced to {QUERY_MAX_RECORDS}, the maximum for Search Records")
    elif kind == "PICK_RECORD":
        object_name = _check_object(dm, inp.get("objectName"), where, errors)
        if inp.get("strategy") not in ("RANDOM", "ROUND_ROBIN", "LOAD_BALANCED"):
            errors.append(f"{where}: strategy must be RANDOM, ROUND_ROBIN or LOAD_BALANCED")
        ids = inp.get("recordIds")
        if not isinstance(ids, list) or not ids:
            errors.append(f"{where}: recordIds must list the candidate records")
        else:
            for item in ids:
                if not is_variable(item) and (not isinstance(item, str) or not UUID_RE.fullmatch(item.lower())):
                    errors.append(f"{where}: recordIds must be record UUIDs, not {item!r}")
        if inp.get("strategy") == "LOAD_BALANCED":
            balance = inp.get("loadBalance")
            if not isinstance(balance, dict):
                errors.append(f"{where}: loadBalance is required when strategy is LOAD_BALANCED")
            else:
                counted = _check_object(dm, balance.get("objectNameSingular"), where, errors, label="loadBalance.objectNameSingular")
                if counted:
                    definition = dm.fields(counted).get(str(balance.get("fieldName")))
                    targets = []
                    if definition and definition.get("type") == "RELATION" and definition["relation"].get("type") == "MANY_TO_ONE":
                        targets = [definition["relation"].get("target")]
                    elif definition and definition.get("type") == "MORPH_RELATION":
                        targets = list(definition["relation"].get("targets") or [])
                    if not targets:
                        errors.append(f"{where}: loadBalance.fieldName must be a relation field of {counted}")
                    elif object_name and object_name not in targets:
                        errors.append(f"{where}: {counted}.{balance.get('fieldName')} does not point to {object_name}")
    elif kind == "FORM":
        names: set[str] = set()
        for field in inp:
            if not isinstance(field, dict):
                errors.append(f"{where}: every form field must be an object")
                continue
            name = field.get("name")
            if not isinstance(name, str) or not IDENTIFIER_RE.fullmatch(name):
                errors.append(f"{where}: form field name {name!r} must be a simple name")
            elif name in names:
                errors.append(f"{where}: form field names must be unique ({name})")
            else:
                names.add(name)
            if not isinstance(field.get("label"), str) or not field.get("label", "").strip():
                errors.append(f"{where}: form field {name} needs a label")
            ftype = field.get("type")
            if ftype not in FORM_FIELD_TYPES:
                errors.append(f"{where}: form field {name} has type {ftype!r}; allowed: {', '.join(FORM_FIELD_TYPES)}")
            extra = field.get("settings") if isinstance(field.get("settings"), dict) else {}
            if ftype in ("SELECT", "MULTI_SELECT") and not _form_options(dm, extra):
                errors.append(f"{where}: form field {name} needs settings.options or settings.objectName with fieldName of a select field")
            if ftype == "RECORD":
                _check_object(dm, extra.get("objectName"), where, errors, label=f"form field {name} settings.objectName")
    elif kind == "FILTER":
        _check_filters(inp, where, errors)
    elif kind == "IF_ELSE":
        branches = inp.get("branches")
        if not isinstance(branches, list) or len(branches) < 2:
            errors.append(f"{where}: IF_ELSE needs at least two branches (a condition branch and an else branch)")
            branches = branches if isinstance(branches, list) else []
        groups = {group.get("id") for group in inp.get("stepFilterGroups") or [] if isinstance(group, dict)}
        if inp.get("stepFilterGroups") is not None or inp.get("stepFilters") is not None:
            _check_filters(inp, where, errors)
        ids: set[str] = set()
        else_positions = []
        for position, branch in enumerate(branches):
            if not isinstance(branch, dict) or not isinstance(branch.get("id"), str):
                errors.append(f"{where}: every branch needs an id")
                continue
            if branch["id"] in ids:
                errors.append(f"{where}: branch ids must be unique ({branch['id']})")
            ids.add(branch["id"])
            if not isinstance(branch.get("nextStepIds"), list) or not branch.get("nextStepIds"):
                errors.append(f"{where}: branch {branch['id']} is not connected to any step")
            if "condition" in branch:
                _check_filters({"condition": branch["condition"]}, f"{where}: branch {branch['id']}", errors)
            elif branch.get("filterGroupId") not in (None, ""):
                if branch["filterGroupId"] not in groups:
                    errors.append(f"{where}: branch {branch['id']} references filter group {branch['filterGroupId']!r}, which does not exist")
            else:
                else_positions.append(position)
        if len(else_positions) > 1 or (else_positions and else_positions[0] != len(branches) - 1):
            errors.append(f"{where}: only the last branch may be the else branch without a condition")
    elif kind == "ITERATOR":
        items = inp.get("items")
        if items is None or (isinstance(items, (str, list)) and len(items) == 0):
            errors.append(f"{where}: ITERATOR needs items: a list or a variable such as {{{{find.all}}}}")
        elif not isinstance(items, (str, list)):
            errors.append(f"{where}: items must be a list or a variable")
        loop = inp.get("initialLoopStepIds")
        if not isinstance(loop, list) or not loop:
            errors.append(f"{where}: ITERATOR has items but no steps inside the loop (initialLoopStepIds)")
        if inp.get("shouldContinueOnIterationFailure") not in (None, True, False):
            errors.append(f"{where}: shouldContinueOnIterationFailure must be true or false")
    elif kind == "DELAY":
        delay_type = inp.get("delayType")
        if delay_type == "DURATION":
            if not isinstance(inp.get("duration"), dict) or not inp["duration"]:
                errors.append(f"{where}: a DURATION delay needs duration with days, hours, minutes or seconds")
            _check_number_parts(inp.get("duration"), ("days", "hours", "minutes", "seconds"), where, "duration", errors)
        elif delay_type == "SCHEDULED_DATE":
            value = inp.get("scheduledDateTime")
            if not isinstance(value, str) or not value:
                errors.append(f"{where}: a SCHEDULED_DATE delay needs scheduledDateTime")
            elif not is_variable(value) and parse_instant(value if "T" in value else value + "T00:00:00Z") is None:
                errors.append(f"{where}: scheduledDateTime {value!r} is not an ISO instant")
        else:
            errors.append(f"{where}: delayType must be DURATION or SCHEDULED_DATE")
    elif kind == "WAIT_FOR_EVENT":
        name = inp.get("eventName")
        match = EVENT_NAME_RE.fullmatch(name) if isinstance(name, str) else None
        if not match:
            errors.append(f"{where}: eventName must look like opportunity.updated (created, updated, deleted or upserted)")
        else:
            _check_object(dm, match.group(1), where, errors, label="eventName object")
        fields = inp.get("updatedFields")
        if fields is not None and (not isinstance(fields, list) or any(not isinstance(item, str) for item in fields)):
            errors.append(f"{where}: updatedFields must be a list of field names")
        _check_number_parts(inp.get("timeout"), ("days", "hours", "minutes"), where, "timeout", errors)
    elif kind in ("SEND_EMAIL", "DRAFT_EMAIL"):
        recipients = inp.get("recipients")
        if not isinstance(recipients, dict) or not recipients.get("to"):
            errors.append(f"{where}: recipients.to is required")
        else:
            for key, value in recipients.items():
                if key not in ("to", "cc", "bcc"):
                    errors.append(f"{where}: recipients.{key} is not supported (to, cc, bcc)")
                elif not isinstance(value, (str, list)):
                    errors.append(f"{where}: recipients.{key} must be text or a list")
        for key in ("subject", "body", "fromHandle", "inReplyTo"):
            if inp.get(key) is not None and not isinstance(inp.get(key), str):
                errors.append(f"{where}: {key} must be text")
        if inp.get("files"):
            warnings.append(f"{where}: attachments are not written into .eml drafts; attach them by hand")
    elif kind == "CREATE_CALENDAR_EVENT":
        for key in ("title", "startsAt", "endsAt"):
            if not isinstance(inp.get(key), str) or not inp.get(key):
                errors.append(f"{where}: {key} is required")
        if inp.get("isFullDay") not in (None, True, False) and not is_variable(inp.get("isFullDay")):
            errors.append(f"{where}: isFullDay must be true or false")
        zone = inp.get("timeZone")
        if zone not in (None, "") and not is_variable(zone) and not _valid_zone(zone):
            errors.append(f"{where}: unknown time zone {zone!r}")
        for key in ("sendInvitations", "addConferencing"):
            if inp.get(key) is True:
                warnings.append(f"{where}: {key} is not performed; the .ics file carries the event only")
    elif kind == "HTTP_REQUEST":
        url = inp.get("url")
        if not isinstance(url, str) or not url:
            errors.append(f"{where}: url is required")
        elif not url.startswith("{{") and not re.match(r"^https?://", url):
            errors.append(f"{where}: url must start with http:// or https://")
        else:
            for match in URL_QUERY_RE.finditer(url):
                if match.group(2).lower() in SENSITIVE_URL_PARAMS and match.group(3) and not is_variable(match.group(3)):
                    errors.append(f"{where}: url parameter {match.group(2)} carries a credential; reference an environment variable as {{{{env.NAME}}}}")
        if str(inp.get("method") or "").upper() not in HTTP_METHODS:
            errors.append(f"{where}: method must be one of {', '.join(HTTP_METHODS)}")
        headers = inp.get("headers") or {}
        if not isinstance(headers, dict):
            errors.append(f"{where}: headers must be an object")
        else:
            for name, value in headers.items():
                if is_env_reference(value):
                    continue
                if isinstance(value, dict):
                    errors.append(f"{where}: header {name} must be text or {{\"env\": \"NAME\", \"prefix\": \"Bearer \"}}")
                elif not isinstance(value, str):
                    errors.append(f"{where}: header {name} must be text")
                elif is_sensitive_key(str(name)) and value.strip():
                    errors.append(
                        f"{where}: header {name} carries a credential; never write it into the definition. "
                        "Reference an environment variable: {\"env\": \"NAME\", \"prefix\": \"Bearer \"}"
                    )
        body = inp.get("body")
        if body is not None and not isinstance(body, (dict, str)):
            errors.append(f"{where}: body must be an object or text")
        if isinstance(body, dict):
            for key, value in body.items():
                if is_sensitive_key(str(key)) and isinstance(value, str) and value and not is_variable(value):
                    errors.append(f"{where}: body field {key} carries a credential; reference an environment variable as {{{{env.NAME}}}}")
    elif kind == "AI_AGENT":
        if not isinstance(inp.get("prompt"), str) or not inp.get("prompt", "").strip():
            errors.append(f"{where}: AI_AGENT needs a prompt")
        fields = inp.get("outputFields")
        if fields is not None:
            if not isinstance(fields, dict) or not fields:
                errors.append(f"{where}: outputFields must map names to {', '.join(AI_OUTPUT_TYPES)}")
            else:
                for name, ftype in fields.items():
                    if not IDENTIFIER_RE.fullmatch(str(name)) or ftype not in AI_OUTPUT_TYPES:
                        errors.append(f"{where}: outputFields.{name} must be one of {', '.join(AI_OUTPUT_TYPES)}")
        if inp.get("agentId"):
            warnings.append(f"{where}: agentId is ignored; the host agent answers the prompt in the conversation")
    elif kind == "CLASSIFY":
        if not isinstance(inp.get("state"), str) or not inp.get("state", "").strip():
            errors.append(f"{where}: CLASSIFY needs state: the text every question is asked about")
        questions = inp.get("questions")
        if not isinstance(questions, list) or not questions:
            errors.append(f"{where}: CLASSIFY needs at least one question")
        else:
            seen: set[str] = set()
            for question in questions:
                if not isinstance(question, dict):
                    errors.append(f"{where}: every question must be an object")
                    continue
                name = question.get("name")
                if not isinstance(name, str) or not CLASSIFY_NAME_RE.fullmatch(name):
                    errors.append(f"{where}: question name {name!r} must have 1 to 64 letters, digits, underscores or dashes")
                elif name in seen:
                    errors.append(f"{where}: two questions are named {name}")
                else:
                    seen.add(name)
                qtype = question.get("type")
                if qtype not in ("choice", "score", "boolean"):
                    errors.append(f"{where}: question {name} needs type choice, score or boolean")
                criteria = question.get("criteria") or []
                if qtype in ("choice", "score"):
                    names = [item.get("name") if isinstance(item, dict) else item for item in criteria]
                    if not names or any(not isinstance(item, str) or not item or "." in item for item in names):
                        errors.append(f"{where}: question {name} needs criteria with names (no dots)")
                    elif len(set(names)) != len(names):
                        errors.append(f"{where}: question {name} lists an option twice")
                elif qtype == "boolean" and criteria:
                    errors.append(f"{where}: a boolean question takes no criteria")
    elif kind == "SEND_CHAT_MESSAGE":
        if not isinstance(inp.get("text"), str) or not inp.get("text", "").strip():
            errors.append(f"{where}: SEND_CHAT_MESSAGE needs text")
        if inp.get("toolCall") is not None:
            errors.append(f"{where}: toolCall is not supported: the skill never executes tool calls; ask in text and branch on the reply")


def _valid_zone(name: str) -> bool:
    if name in ("UTC", "Etc/UTC"):
        return True
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001 - every failure means the zone cannot be used
        return False


def _form_options(dm: DataModel, settings: dict[str, Any]) -> list[str]:
    options = settings.get("options")
    if isinstance(options, list) and options:
        return [str(item.get("value")) if isinstance(item, dict) else str(item) for item in options]
    object_name, field_name = settings.get("objectName"), settings.get("fieldName")
    if object_name in dm.objects and field_name in dm.fields(object_name):
        definition = dm.fields(object_name)[field_name]
        if definition.get("type") in ("SELECT", "MULTI_SELECT"):
            return [str(option.get("value")) for option in definition.get("options", []) if isinstance(option, dict)]
    return []


def _sorts(order: Any) -> list[dict[str, Any]]:
    """orderBy as [{field, direction}] or the GraphQL form {gqlOperationOrderBy: [{field: AscNullsLast}]}."""
    if isinstance(order, dict):
        if isinstance(order.get("gqlOperationOrderBy"), list):
            sorts = []
            for item in order["gqlOperationOrderBy"]:
                if isinstance(item, dict):
                    for name, direction in item.items():
                        sorts.append({"field": name, "direction": "desc" if str(direction).lower().startswith("desc") else "asc"})
            return sorts
        order = order.get("recordSorts") or []
    if isinstance(order, list):
        return [
            {"field": item.get("field") or item.get("fieldName"), "direction": str(item.get("direction") or "asc").lower()}
            for item in order if isinstance(item, dict)
        ]
    return []


def _validate_trigger(dm: DataModel, trigger: Any, step_ids: set[str], where: str, errors: list[str], warnings: list[str]) -> None:
    if not isinstance(trigger, dict):
        errors.append(f"{where}: trigger must be an object")
        return
    for key in trigger:
        if key not in TRIGGER_KEYS:
            errors.append(f"{where}: unknown trigger key {key!r}")
    kind = trigger.get("type")
    if kind not in TRIGGER_TYPES:
        errors.append(f"{where}: trigger type must be one of {', '.join(TRIGGER_TYPES)}")
        return
    settings = trigger.get("settings") or {}
    if not isinstance(settings, dict):
        errors.append(f"{where}: trigger settings must be an object")
        return
    nexts = trigger.get("nextStepIds")
    if not isinstance(nexts, list) or not nexts:
        errors.append(f"{where}: the trigger is not connected to any step (nextStepIds)")
    else:
        for item in nexts:
            if item not in step_ids:
                errors.append(f"{where}: the trigger references a non-existent step {item!r}")
    if kind == "DATABASE_EVENT":
        name = settings.get("eventName")
        match = EVENT_NAME_RE.fullmatch(name) if isinstance(name, str) else None
        if not match:
            errors.append(f"{where}: eventName must follow objectName.action, for example opportunity.updated")
            return
        object_name = _check_object(dm, match.group(1), where, errors, label="eventName object")
        fields = settings.get("fields")
        if fields is not None:
            if not isinstance(fields, list) or any(not isinstance(item, str) for item in fields):
                errors.append(f"{where}: fields must be a list of field names")
            elif fields and match.group(2) not in ("updated", "upserted"):
                errors.append(f"{where}: fields filter only applies to updated and upserted events")
            elif object_name:
                for item in fields:
                    if item not in dm.fields(object_name):
                        errors.append(f"{where}: {object_name} has no field {item}")
        if settings.get("filter") is not None:
            if not isinstance(settings["filter"], dict):
                errors.append(f"{where}: trigger filter must be an object")
            else:
                _check_filters(settings["filter"], f"{where}: trigger filter", errors)
                for token, root in variable_roots(settings["filter"]):
                    if root != "trigger":
                        errors.append(f"{where}: trigger filter may only read {{{{trigger...}}}}, not {token}")
    elif kind == "MANUAL":
        availability, object_name = manual_availability(settings)
        if availability not in ("GLOBAL", "SINGLE_RECORD", "BULK_RECORDS"):
            errors.append(f"{where}: availability must be GLOBAL, SINGLE_RECORD or BULK_RECORDS")
        elif availability != "GLOBAL":
            _check_object(dm, object_name, where, errors, label="availability.objectNameSingular")
    elif kind == "CRON":
        try:
            CronSchedule(cron_pattern(settings))
        except ValueError as exc:
            errors.append(f"{where}: {exc}")
        if settings.get("timeZone") not in (None, "UTC", "Etc/UTC"):
            errors.append(f"{where}: schedules run in UTC as is usual in CRMs; convert the times to UTC")
    elif kind == "WEBHOOK":
        method = settings.get("httpMethod")
        if method not in ("GET", "POST"):
            errors.append(f"{where}: httpMethod must be GET or POST")
        if method == "POST" and not isinstance(settings.get("expectedBody"), dict):
            errors.append(f"{where}: a POST webhook needs expectedBody, a sample of the JSON body")
        if settings.get("authentication") not in (None, "API_KEY"):
            errors.append(f"{where}: authentication must be null or API_KEY")
        elif settings.get("authentication") == "API_KEY":
            warnings.append(f"{where}: authentication API_KEY cannot be checked for a payload file; check the sender yourself")


def validate_flow(dm: DataModel, trigger: Any, steps: Any, where: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(steps, list) or not steps:
        errors.append(f"{where}: the workflow has no steps")
        steps = []
    step_ids: set[str] = set()
    valid_steps = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"{where}: step {index + 1} must be an object")
            continue
        step_id = step.get("id")
        if not isinstance(step_id, str) or not STEP_ID_RE.fullmatch(step_id) or step_id in RESERVED_STEP_IDS:
            errors.append(f"{where}: step {index + 1} needs an id of letters, digits, '-' or '_' (not trigger or env)")
            continue
        if step_id in step_ids:
            errors.append(f"{where}: duplicate step id {step_id!r}")
            continue
        step_ids.add(step_id)
        for key in step:
            if key not in STEP_KEYS:
                errors.append(f"{where}: step {step_id}: unknown key {key!r}")
        if step.get("type") not in ACTION_TYPES:
            errors.append(f"{where}: step {step_id}: unknown type {step.get('type')!r}")
            continue
        if step.get("name") is not None and not isinstance(step.get("name"), str):
            errors.append(f"{where}: step {step_id}: name must be text")
        if step.get("nextStepIds") is not None and (not isinstance(step["nextStepIds"], list) or any(not isinstance(item, str) for item in step["nextStepIds"])):
            errors.append(f"{where}: step {step_id}: nextStepIds must be a list of step ids")
            continue
        valid_steps.append(step)
    _validate_trigger(dm, trigger, step_ids, where, errors, warnings)
    graph = FlowGraph(trigger if isinstance(trigger, dict) else {}, valid_steps)
    for step_id in graph.order:
        for child in graph.outgoing(step_id):
            if child not in graph.steps:
                errors.append(f"{where}: step {step_id} references a non-existent step {child!r}")
    reachable = graph.reachable()
    for step_id in graph.order:
        if step_id not in reachable:
            errors.append(f"{where}: step {step_id} is not reachable from the trigger")
    cycle = graph.cycle()
    if cycle:
        errors.append(f"{where}: steps {' -> '.join(cycle)} form a loop outside an iterator")
    settings = trigger.get("settings") if isinstance(trigger, dict) and isinstance(trigger.get("settings"), dict) else {}
    watched = EVENT_NAME_RE.fullmatch(str(settings.get("eventName"))) if isinstance(trigger, dict) and trigger.get("type") == "DATABASE_EVENT" else None
    if watched and watched.group(2) in ("updated", "upserted") and not settings.get("fields"):
        writes_same = any(
            graph.steps[step_id].get("type") in ("UPDATE_RECORD", "UPSERT_RECORD")
            and isinstance(graph.input(step_id), dict) and graph.input(step_id).get("objectName") == watched.group(1)
            for step_id in graph.order
        )
        if writes_same:
            warnings.append(
                f"{where}: the workflow changes the {watched.group(1)} records it watches; its own writes never trigger it again, "
                "but a fields filter keeps runs to the changes that matter"
            )
    for step_id in graph.order:
        step = graph.steps[step_id]
        step_where = f"{where}: step {step_id}"
        _validate_step(dm, graph, step, step_where, errors, warnings)
        ancestors = graph.ancestors(step_id)
        settings = step.get("settings") if isinstance(step.get("settings"), dict) else {}
        for token, root in variable_roots(settings):
            if root == "trigger":
                continue
            if root == "env":
                if step.get("type") != "HTTP_REQUEST":
                    errors.append(f"{step_where}: {token} is allowed only in HTTP_REQUEST steps; values from the environment are never stored")
                continue
            if root is None:
                errors.append(f"{step_where}: {token} is not a valid variable path")
            elif root not in graph.steps:
                errors.append(f"{step_where}: {token} reads unknown step {root!r}")
            elif root not in ancestors:
                errors.append(f"{step_where}: {token} reads step {root!r}, which does not run before this step")
    return errors, warnings


def validate_workflow_raw(dm: Optional[DataModel], raw: Any, relative: str, stem: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return [f"{relative}: a workflow must be a JSON object"], []
    if raw.get("format") != WORKFLOW_FORMAT:
        errors.append(f"{relative}: format must be {WORKFLOW_FORMAT}")
    for key in raw:
        if key not in TOP_KEYS:
            errors.append(f"{relative}: unknown key {key!r}")
    workflow_id = raw.get("id")
    if not isinstance(workflow_id, str) or not ID_RE.fullmatch(workflow_id) or len(workflow_id) > 64:
        errors.append(f"{relative}: id must be kebab-case (letters, digits, hyphens)")
    elif workflow_id != stem:
        errors.append(f"{relative}: the file name must be {workflow_id}.json")
    else:
        import sync_artifacts

        artifact = sync_artifacts.classify(f"{WORKFLOWS_DIR}/{workflow_id}.json")
        if artifact is not None:
            errors.append(f"{relative}: the id {workflow_id!r} gives a file name the storage treats specially ({artifact.reason}); choose another id")
    if not isinstance(raw.get("name"), str) or not raw.get("name", "").strip():
        errors.append(f"{relative}: name is required")
    if raw.get("description") is not None and not isinstance(raw.get("description"), str):
        errors.append(f"{relative}: description must be text")
    versions = raw.get("versions")
    if not isinstance(versions, list) or not versions:
        errors.append(f"{relative}: versions must be a non-empty list")
        return errors, warnings
    numbers: set[int] = set()
    counts = {status: 0 for status in VERSION_STATUSES}
    for index, version in enumerate(versions):
        where = f"{relative}: version {index + 1}"
        if not isinstance(version, dict):
            errors.append(f"{where}: must be an object")
            continue
        number = version.get("version")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            errors.append(f"{where}: version must be a whole number from 1")
        elif number in numbers:
            errors.append(f"{where}: version number {number} is used twice")
        else:
            numbers.add(number)
            where = f"{relative}: v{number}"
        for key in version:
            if key not in VERSION_KEYS:
                errors.append(f"{where}: unknown key {key!r}")
        status = version.get("status")
        if status not in VERSION_STATUSES:
            errors.append(f"{where}: status must be one of {', '.join(VERSION_STATUSES)}")
            continue
        counts[status] += 1
        if parse_instant(version.get("created_at")) is None:
            errors.append(f"{where}: created_at must be an ISO instant")
        if status == "DRAFT":
            if version.get("sha256") not in (None, ""):
                errors.append(f"{where}: a DRAFT has no sha256; it is set when the version is activated")
            if version.get("activated_at") not in (None, ""):
                errors.append(f"{where}: a DRAFT has no activated_at")
        else:
            if status in ("ACTIVE", "DEACTIVATED") and parse_instant(version.get("activated_at")) is None:
                errors.append(f"{where}: activated_at must be an ISO instant")
            expected = version.get("sha256")
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                errors.append(
                    f"{where}: {status} versions carry the sha256 of their trigger and steps; write a new version as DRAFT "
                    "and activate it with crm_workflows.py activate"
                )
            elif version_hash(version) != expected:
                errors.append(
                    f"{where}: the {status} version was changed after activation (sha256 mismatch); "
                    "versions are immutable once activated. Restore it from meta/history or undo the edit, "
                    "and make changes in a new draft (create-draft)"
                )
        if dm is not None:
            flow_errors, flow_warnings = validate_flow(dm, version.get("trigger"), version.get("steps"), where)
            errors.extend(flow_errors)
            warnings.extend(flow_warnings)
    if counts["ACTIVE"] > 1:
        errors.append(f"{relative}: at most one version may be ACTIVE")
    if counts["DRAFT"] > 1:
        errors.append(f"{relative}: at most one version may be a DRAFT")
    return errors, warnings


def scan_definition_text(text: str, relative: str) -> list[str]:
    errors = []
    for number, line in enumerate(text.splitlines(), 1):
        kinds = credential_kinds(line)
        if kinds:
            errors.append(
                f"{relative}:{number}: possible credential ({', '.join(kinds)}); never store it in a workflow. "
                "Reference an environment variable instead ({\"env\": \"NAME\"} or {{env.NAME}})"
            )
    return errors


def workflow_relative(workflow_id: str) -> str:
    return f"{WORKFLOWS_DIR}/{workflow_id}.json"


def read_workflow(target: Path, workflow_id: str) -> tuple[dict[str, Any], str, str]:
    if not isinstance(workflow_id, str) or not ID_RE.fullmatch(workflow_id):
        raise WorkflowError(f"workflow id {workflow_id!r} must be kebab-case")
    relative = workflow_relative(workflow_id)
    path = target / relative
    if not path.is_file():
        raise WorkflowError(f"{relative} does not exist")
    content = path.read_bytes()
    try:
        raw = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidDefinition([f"{relative}: invalid JSON: {exc}"]) from exc
    return raw, relative, sha256_bytes(content)


def check_workflow(target: Path, dm: DataModel, raw: dict[str, Any], relative: str) -> tuple[list[str], list[str]]:
    errors, warnings = validate_workflow_raw(dm, raw, relative, Path(relative).stem)
    path = target / relative
    if path.is_file():
        errors.extend(scan_definition_text(path.read_text(encoding="utf-8"), relative))
    return errors, warnings


def find_version(raw: dict[str, Any], number: int) -> dict[str, Any]:
    for version in raw.get("versions") or []:
        if isinstance(version, dict) and version.get("version") == number:
            return version
    raise WorkflowError(f"workflow {raw.get('id')} has no version {number}")


def active_version(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    for version in raw.get("versions") or []:
        if isinstance(version, dict) and version.get("status") == "ACTIVE":
            return version
    return None


# ---------------------------------------------------------------------------
# state and run log files


def empty_state() -> dict[str, Any]:
    return {"format": STATE_FORMAT, "workflows": {}, "pending_runs": {}}


def read_state(target: Path) -> tuple[dict[str, Any], bool, str]:
    path = target / STATE_PATH
    if not path.is_file():
        return empty_state(), False, ""
    content = path.read_bytes()
    try:
        state = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"{STATE_PATH}: invalid JSON: {exc}") from exc
    if not isinstance(state, dict) or state.get("format") != STATE_FORMAT:
        raise WorkflowError(f"{STATE_PATH}: format must be {STATE_FORMAT}")
    state.setdefault("workflows", {})
    state.setdefault("pending_runs", {})
    return state, True, sha256_bytes(content)


def read_run_entries(target: Path) -> list[dict[str, Any]]:
    root = target / RUNS_DIR
    entries = []
    if not root.is_dir():
        return entries
    for shard in sorted(root.glob("*.jsonl")):
        for line in shard.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    return entries


def run_depth_index(target: Path) -> dict[str, int]:
    """Cascade depth of every logged run, reachable by run id and by the transaction's origin run id."""
    index: dict[str, int] = {}
    for entry in read_run_entries(target):
        depth = entry.get("cascade_depth")
        if not isinstance(depth, int):
            continue
        for key in (entry.get("run_id"), entry.get("origin_run_id")):
            if isinstance(key, str) and key:
                index[key] = max(index.get(key, 0), depth)
    return index


def event_line_counts(target: Path) -> dict[str, int]:
    root = target / EVENTS_DIR
    counts: dict[str, int] = {}
    if root.is_dir():
        for shard in sorted(root.glob("*.jsonl")):
            counts[shard.relative_to(target).as_posix()] = len(shard.read_text(encoding="utf-8").splitlines())
    return counts


def is_migration_event(event: dict[str, Any]) -> bool:
    """A data model migration (crm_schema.py) rewrites values; it is no record change and triggers nothing."""
    origin = event.get("origin") if isinstance(event.get("origin"), dict) else {}
    return origin.get("kind") == "migration" or "schema_change" in event or "schema_change" in (event.get("changes") or {})


def changed_fields(event: dict[str, Any]) -> list[str]:
    names = []
    for key in (event.get("changes") or {}):
        base = key[len("richtext:"):] if key.startswith("richtext:") else key.split(".", 1)[0]
        if base.startswith("crm_"):
            continue
        if base not in names:
            names.append(base)
    return names


# ---------------------------------------------------------------------------
# record view and API-shaped record objects


class RecordView:
    """Records on disk plus the operations planned so far in this plan.

    The view simulates every operation with crm_contract.Planner, so a later
    step sees what an earlier step created or changed, and a failing operation
    fails exactly the step that requested it.
    """

    def __init__(self, target: Path, dm: DataModel, actor: str, workflow_id: str, now: str):
        roles = load_json_file(target, ROLES_PATH, None)
        cooperative = roles if isinstance(roles, dict) and roles.get("enforcement") == "cooperative" else None
        from team_contract import load_config, native_roles
        team = load_config(target, required=False)
        if team is not None:
            cooperative = native_roles(team)
        self.dm = dm
        self.planner = Planner(target, dm, actor, {"kind": "workflow", "workflow": workflow_id, "run_id": "simulation"}, now, cooperative)

    def apply(self, operation: dict[str, Any]) -> Optional[str]:
        before = len(self.planner.errors)
        try:
            self.planner.run([deep(operation)])
        except Exception as exc:  # noqa: BLE001 - an unexpected core error fails the step, not the plan
            return f"{type(exc).__name__}: {exc}"
        if len(self.planner.errors) > before:
            return str(self.planner.errors[-1].get("error"))
        return None

    def get(self, object_name: str, record_id: str) -> Optional[Record]:
        return self.planner.current(object_name, record_id)

    def records(self, object_name: str) -> list[Record]:
        return self.planner.all_records(object_name)

    def match(self, object_name: str, criteria: dict[str, Any]) -> list[Record]:
        return self.planner.match_records(object_name, criteria, include_deleted=True)

    def object_of(self, record_id: str, candidates: list[str]) -> Optional[str]:
        for object_name in candidates:
            if object_name in self.dm.objects and self.get(object_name, record_id) is not None:
                return object_name
        return None


def _link_object(dm: DataModel, link: str) -> Optional[dict[str, Any]]:
    parsed = parse_record_link(link) if isinstance(link, str) else None
    if not parsed:
        return None
    label = re.match(r"^\[\[[^|\]]+\|([^\]]*)\]\]$", link.strip())
    return {"id": parsed[1], "object": dm.object_for_directory(parsed[0]), "title": label.group(1) if label else None}


def record_to_object(dm: DataModel, view: Optional[RecordView], record: Record, depth: int = 1) -> dict[str, Any]:
    """A record as workflows see it: composites nested, relations as objects."""
    data = record.data
    result: dict[str, Any] = {
        "id": record.id,
        "createdAt": data.get("crm_created_at"),
        "updatedAt": data.get("crm_updated_at"),
        "deletedAt": data.get("crm_deleted_at"),
        "createdBy": data.get("crm_created_by"),
        "updatedBy": data.get("crm_updated_by"),
    }
    if data.get("crm_position") is not None:
        result["position"] = data["crm_position"]
    for name, definition in dm.fields(record.object).items():
        kind = definition.get("type")
        if kind in COMPOSITE_SUBFIELDS:
            result[name] = {sub: data.get(f"{name}.{sub}") for sub in COMPOSITE_SUBFIELDS[kind]}
        elif kind == "RICH_TEXT":
            result[name] = {"markdown": record.richtext.get(name, "")}
        elif kind == "RELATION":
            if definition.get("relation", {}).get("type") == "ONE_TO_MANY":
                continue
            linked = _link_object(dm, data.get(name)) if data.get(name) else None
            result[name + "Id"] = linked["id"] if linked else None
            result[name] = _expand(dm, view, linked, depth)
        elif kind == "MORPH_RELATION":
            value = data.get(name)
            links = value if isinstance(value, list) else ([value] if value else [])
            objects = [item for item in (_link_object(dm, link) for link in links) if item]
            if definition.get("relation", {}).get("multiple", True):
                result[name] = objects
            else:
                result[name] = _expand(dm, view, objects[0] if objects else None, depth)
                result[name + "Id"] = objects[0]["id"] if objects else None
        else:
            result[name] = deep(data.get(name))
    return result


def _expand(dm: DataModel, view: Optional[RecordView], linked: Optional[dict[str, Any]], depth: int) -> Optional[dict[str, Any]]:
    if linked is None:
        return None
    if depth <= 0 or view is None or not linked.get("object"):
        return linked
    target = view.get(linked["object"], linked["id"])
    if target is None:
        return linked
    expanded = record_to_object(dm, view, target, depth - 1)
    expanded["object"] = linked["object"]
    expanded["title"] = target.data.get("crm_title")
    return expanded


def normalize_values(dm: DataModel, view: RecordView, object_name: str, values: dict[str, Any]) -> dict[str, Any]:
    """API-shaped field values (relations as {id}, rich text as {markdown}) -> core request values."""
    fields = dm.fields(object_name)
    result: dict[str, Any] = {}
    for key, value in values.items():
        if key == "id":
            continue
        base, _, sub = key.partition(".")
        if base not in fields and base.endswith("Id") and fields.get(base[:-2], {}).get("type") in ("RELATION", "MORPH_RELATION"):
            base = base[:-2]
            key = base
            sub = ""
        definition = fields.get(base)
        if definition is None:
            raise StepFailure(f"{object_name} has no field {base}")
        kind = definition.get("type")
        if kind == "RICH_TEXT":
            value = value.get("markdown", "") if isinstance(value, dict) else value
        elif kind in ("RELATION", "MORPH_RELATION") and not sub:
            value = _normalize_relation(dm, view, definition, value)
        elif kind in COMPOSITE_SUBFIELDS and isinstance(value, dict) and not sub:
            allowed = set(COMPOSITE_SUBFIELDS[kind]) | ({"amount"} if kind == "CURRENCY" else set())
            value = {part: item for part, item in value.items() if part in allowed}
        result[key] = value
    return result


def _normalize_relation(dm: DataModel, view: RecordView, definition: dict[str, Any], value: Any) -> Any:
    relation = definition.get("relation", {})
    allowed = [relation.get("target")] if definition.get("type") == "RELATION" else list(relation.get("targets") or [])
    multiple = definition.get("type") == "MORPH_RELATION" and relation.get("multiple", True)

    def one(item: Any) -> Any:
        if item is None or item == "":
            return None
        if isinstance(item, dict):
            if "match" in item or "ref" in item:
                return item
            record_id = item.get("id")
            if record_id in (None, ""):
                return None
            object_name = item.get("object") or item.get("objectName")
            if not object_name and len(allowed) > 1 and isinstance(record_id, str):
                object_name = view.object_of(record_id.lower(), allowed)
            return {"object": object_name, "id": record_id} if object_name else record_id
        if isinstance(item, str):
            text = item.strip()
            if parse_record_link(text) or (":" in text and text.split(":", 1)[0] in dm.objects):
                return text
            if UUID_RE.fullmatch(text.lower()) and len(allowed) > 1:
                object_name = view.object_of(text.lower(), allowed)
                return f"{object_name}:{text.lower()}" if object_name else text
            return text
        return item

    if isinstance(value, dict) and ("add" in value or "remove" in value) and multiple:
        return {key: [entry for entry in (one(item) for item in (value.get(key) or [])) if entry is not None] for key in ("add", "remove") if key in value}
    if multiple:
        items = value if isinstance(value, list) else ([] if value in (None, "") else [value])
        return [entry for entry in (one(item) for item in items) if entry is not None]
    if isinstance(value, list):
        value = value[0] if len(value) == 1 else value
    return one(value)


# ---------------------------------------------------------------------------
# step filters (port of a full-featured CRM evaluate-filter-conditions.util.ts)


def _parse_bool(value: Any) -> Any:
    if value == "true":
        return True
    if value == "false":
        return False
    return value


def _to_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _non_empty(value: Any) -> bool:
    return (isinstance(value, str) and value != "") or (isinstance(value, list) and len(value) > 0)


def _filter_contains(left: Any, right: Any) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        return any(item in right for item in left)
    if isinstance(right, str) and isinstance(left, (list, str)):
        try:
            parsed = json.loads(right)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            return any(item in left for item in parsed)
        return right in left
    return str(right) in str(left)


def _select_options(right: Any) -> list[str]:
    if isinstance(right, list):
        return [item for item in right if isinstance(item, str)]
    if isinstance(right, str):
        try:
            parsed = json.loads(right)
        except (json.JSONDecodeError, ValueError):
            return [right]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, str)]
        return [right]
    return []


def _matches_option(left: Any, options: list[str]) -> bool:
    non_empty = [option for option in options if option != ""]
    has_empty = len(non_empty) != len(options)
    if isinstance(left, list):
        return any(item in non_empty for item in left) or (has_empty and not left)
    value = left if isinstance(left, str) else None
    return (value in non_empty) or (has_empty and value is None)


def _relation_id(value: Any) -> Any:
    if isinstance(value, dict) and "id" in value:
        return value.get("id")
    if isinstance(value, str):
        parsed = parse_record_link(value)
        if parsed:
            return parsed[1]
    return value


def _rating(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        match = re.fullmatch(r"RATING_(\d+)", value)
        if match:
            return float(match.group(1))
        return _to_number(value)
    return None


def _date_filter(operand: str, left: Any, right: Any, now: datetime) -> bool:
    if operand == "IS_EMPTY":
        return left is None or left == ""
    if operand == "IS_NOT_EMPTY":
        return left is not None and left != ""

    def instant(value: Any) -> Optional[datetime]:
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
            return parse_instant(value.strip() + "T00:00:00Z")
        return parse_instant(value) if isinstance(value, str) else None

    moment = instant(left)
    if moment is None:
        return False
    if operand == "IS":
        other = instant(right)
        return other is not None and moment.date() == other.date()
    if operand == "IS_NOT":
        other = instant(right)
        return other is None or moment.date() != other.date()
    if operand == "IS_IN_PAST":
        return moment < now
    if operand == "IS_IN_FUTURE":
        return moment > now
    if operand == "IS_TODAY":
        return moment.date() == now.date()
    if operand in ("IS_BEFORE", "IS_AFTER", "GREATER_THAN_OR_EQUAL", "LESS_THAN_OR_EQUAL"):
        other = instant(right)
        if other is None:
            return False
        return {
            "IS_BEFORE": moment < other, "IS_AFTER": moment > other,
            "GREATER_THAN_OR_EQUAL": moment >= other, "LESS_THAN_OR_EQUAL": moment <= other,
        }[operand]
    if operand == "IS_RELATIVE":
        start, end = crm_filters.relative_range(str(right or ""), crm_filters.Context(now=format_instant(now)))
        return start <= moment.date() <= end
    raise StepFailure(f"operand {operand} is not supported for date filters")


def _number_filter(operand: str, left: Any, right: Any) -> bool:
    empty = left is None or left == ""
    a, b = _to_number(left), _to_number(right)
    if operand == "IS_EMPTY":
        return empty
    if operand == "IS_NOT_EMPTY":
        return not empty
    if operand in ("GREATER_THAN_OR_EQUAL", "LESS_THAN_OR_EQUAL", "IS"):
        if empty or a is None or b is None:
            return False
        return a >= b if operand == "GREATER_THAN_OR_EQUAL" else (a <= b if operand == "LESS_THAN_OR_EQUAL" else a == b)
    if operand == "IS_NOT":
        return empty or a is None or b is None or a != b
    raise StepFailure(f"operand {operand} is not supported for number filters")


def _loose_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return str(left).lower() == str(right).lower()
    a, b = _to_number(left), _to_number(right)
    if a is not None and b is not None and (not isinstance(left, str) or not isinstance(right, str)):
        return a == b
    return str(left) == str(right)


def evaluate_filter(item: dict[str, Any], now: datetime) -> bool:
    operand = item["operand"]
    left, right = item["left"], item["right"]
    kind = str(item.get("type") or "")
    sub = item.get("compositeFieldSubFieldName")
    if not kind:
        if operand in ("IS_BEFORE", "IS_AFTER", "IS_IN_PAST", "IS_IN_FUTURE", "IS_TODAY", "IS_RELATIVE"):
            kind = "DATE_TIME"
        elif operand in ("GREATER_THAN_OR_EQUAL", "LESS_THAN_OR_EQUAL"):
            kind = "NUMBER"
    if kind in ("NUMBER", "NUMERIC", "number"):
        return _number_filter(operand, left, right)
    if kind in ("DATE", "DATE_TIME"):
        return _date_filter(operand, left, right, now)
    if kind in ("TEXT", "MULTI_SELECT", "FULL_NAME", "EMAILS", "PHONES", "ADDRESS", "LINKS", "ARRAY", "array", "RAW_JSON"):
        if operand == "CONTAINS":
            return _filter_contains(left, right)
        if operand == "DOES_NOT_CONTAIN":
            return not _filter_contains(left, right)
        if operand == "IS":
            return left == right
        if operand == "IS_NOT":
            return left != right
        if operand == "IS_EMPTY":
            return not _non_empty(left)
        if operand == "IS_NOT_EMPTY":
            return _non_empty(left)
        raise StepFailure(f"operand {operand} is not supported for {kind} filters")
    if kind == "SELECT":
        if operand == "IS":
            return _matches_option(left, _select_options(right))
        if operand == "IS_NOT":
            return not _matches_option(left, _select_options(right))
        if operand == "IS_EMPTY":
            return not _non_empty(left)
        if operand == "IS_NOT_EMPTY":
            return _non_empty(left)
        raise StepFailure(f"operand {operand} is not supported for select filters")
    if kind in ("BOOLEAN", "boolean"):
        if operand == "IS":
            return _parse_bool(left) == _parse_bool(right)
        if operand == "IS_NOT":
            return _parse_bool(left) != _parse_bool(right)
        raise StepFailure(f"operand {operand} is not supported for boolean filters")
    if kind == "UUID":
        if operand in ("IS", "IS_NOT"):
            same = left == right
            return same if operand == "IS" else not same
        if operand in ("IS_EMPTY", "IS_NOT_EMPTY"):
            empty = not (isinstance(left, str) and left)
            return empty if operand == "IS_EMPTY" else not empty
        raise StepFailure(f"operand {operand} is not supported for uuid filters")
    if kind == "RATING":
        a, b = _rating(left), _rating(right)
        return {
            "IS": a is not None and a == b, "IS_NOT": a is None or a != b,
            "GREATER_THAN_OR_EQUAL": a is not None and b is not None and a >= b,
            "LESS_THAN_OR_EQUAL": a is not None and b is not None and a <= b,
            "IS_EMPTY": a is None, "IS_NOT_EMPTY": a is not None,
        }.get(operand, False)
    if kind == "RELATION":
        a, b = _relation_id(left), _relation_id(right)
        if operand in ("IS", "IS_NOT"):
            return (a == b) if operand == "IS" else (a != b)
        empty = not (isinstance(a, str) and a)
        if operand in ("IS_EMPTY", "IS_NOT_EMPTY"):
            return empty if operand == "IS_EMPTY" else not empty
        raise StepFailure(f"operand {operand} is not supported for relation filters")
    if kind == "CURRENCY":
        if sub == "currencyCode":
            return evaluate_filter({**item, "type": "TEXT"}, now) if operand not in ("IS", "IS_NOT") else (
                _filter_contains(left, right) if operand == "IS" else not _filter_contains(left, right)
            )
        return _number_filter(operand, left, right)
    if operand == "IS":
        return _loose_equal(left, right)
    if operand == "IS_NOT":
        return not _loose_equal(left, right)
    if operand == "IS_EMPTY":
        return not _non_empty(left) and left not in (0, False) if left is not None else True
    if operand == "IS_NOT_EMPTY":
        return left is not None and left != "" and left != []
    if operand == "CONTAINS":
        return _filter_contains(left, right)
    if operand == "DOES_NOT_CONTAIN":
        return not _filter_contains(left, right)
    if operand in ("GREATER_THAN_OR_EQUAL", "LESS_THAN_OR_EQUAL"):
        return _number_filter(operand, left, right)
    if operand in ("IS_BEFORE", "IS_AFTER", "IS_IN_PAST", "IS_IN_FUTURE", "IS_TODAY", "IS_RELATIVE"):
        return _date_filter(operand, left, right, now)
    raise StepFailure(f"operand {operand} is not supported")


EMPTINESS_OPERAND = {"IS": "IS_EMPTY", "IS_NOT": "IS_NOT_EMPTY", "CONTAINS": "IS_EMPTY", "DOES_NOT_CONTAIN": "IS_NOT_EMPTY"}


def resolve_filters(filters: list[dict[str, Any]], context: dict[str, Any]) -> list[dict[str, Any]]:
    resolved = []
    for item in filters:
        operand = normalize_operand(item.get("operand"))
        if operand is None:
            raise StepFailure(f"unknown filter operand {item.get('operand')!r}")
        resolver = Resolver(context)
        left = resolver.resolve(item.get("stepOutputKey"))
        raw_value = item.get("value")
        right = resolver.resolve(raw_value)
        if is_variable(raw_value) and right is None:
            operand = EMPTINESS_OPERAND.get(operand, operand)
        resolved.append({**item, "operand": operand, "left": left, "right": right})
    return resolved


def evaluate_conditions(groups: list[dict[str, Any]], filters: list[dict[str, Any]], now: datetime) -> bool:
    if not groups and not filters:
        return True
    group_ids = {group.get("id") for group in groups}
    if groups:
        for item in filters:
            if item.get("stepFilterGroupId") not in group_ids:
                raise StepFailure(f"filter group {item.get('stepFilterGroupId')} not found")
    roots = [group for group in groups if not group.get("parentStepFilterGroupId")]
    if not roots:
        return all(evaluate_filter(item, now) for item in filters)

    def group_result(group: dict[str, Any]) -> bool:
        children = sorted(
            (child for child in groups if child.get("parentStepFilterGroupId") == group.get("id")),
            key=lambda child: child.get("positionInStepFilterGroup") or 0,
        )
        results = [evaluate_filter(item, now) for item in filters if item.get("stepFilterGroupId") == group.get("id")]
        results += [group_result(child) for child in children]
        if not results:
            return True
        operator = str(group.get("logicalOperator") or "AND").upper()
        return all(results) if operator == "AND" else any(results)

    return all(group_result(root) for root in roots)


def filter_spec(container: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if "condition" in container:
        groups: list[dict[str, Any]] = []
        filters: list[dict[str, Any]] = []
        compact_to_groups(container["condition"], groups, filters)
        return groups, filters
    return list(container.get("stepFilterGroups") or []), list(container.get("stepFilters") or [])


def container_matches(container: dict[str, Any], context: dict[str, Any], now: datetime) -> bool:
    groups, filters = filter_spec(container)
    return evaluate_conditions(groups, resolve_filters(filters, context), now)


def matching_branch(inp: dict[str, Any], context: dict[str, Any], now: datetime) -> dict[str, Any]:
    groups = list(inp.get("stepFilterGroups") or [])
    filters = resolve_filters(list(inp.get("stepFilters") or []), context)
    for branch in inp.get("branches") or []:
        if "condition" in branch:
            if container_matches({"condition": branch["condition"]}, context, now):
                return branch
            continue
        group_id = branch.get("filterGroupId")
        if group_id in (None, ""):
            return branch
        selected: dict[str, dict[str, Any]] = {}

        def collect(identifier: str) -> None:
            for group in groups:
                if group.get("id") == identifier and identifier not in selected:
                    selected[identifier] = {**group, "parentStepFilterGroupId": None} if identifier == group_id else group
                    for child in groups:
                        if child.get("parentStepFilterGroupId") == identifier:
                            collect(child["id"])

        collect(group_id)
        if not selected:
            raise StepFailure(f"branch {branch.get('id')} references filter group {group_id}, which does not exist")
        chosen = [item for item in filters if item.get("stepFilterGroupId") in selected]
        if evaluate_conditions(list(selected.values()), chosen, now):
            return branch
    raise StepFailure("no matching branch found in the If/Else step")


# ---------------------------------------------------------------------------
# outbox files


def parse_addresses(value: Any) -> list[tuple[str, str]]:
    if value in (None, ""):
        return []
    texts = value if isinstance(value, list) else [value]
    pairs = email.utils.getaddresses([str(item).replace("\r", " ").replace("\n", " ") for item in texts if item not in (None, "")])
    result = []
    for name, address in pairs:
        if not address:
            continue
        if not EMAIL_RE.fullmatch(address):
            raise StepFailure(f"{address!r} is not an e-mail address")
        result.append((name, address))
    return result


def _header(value: Any) -> str:
    return re.sub(r"[\r\n]+", " ", template_text(value)).strip()


def build_eml(*, sender: Optional[str], to: list[tuple[str, str]], cc: list[tuple[str, str]], bcc: list[tuple[str, str]],
              subject: str, body: str, moment: datetime, message_id: str, in_reply_to: Optional[str], mode: str,
              marker: str) -> str:
    message = EmailMessage(policy=email.policy.SMTP)
    if sender:
        message["From"] = sender
    message["To"] = ", ".join(email.utils.formataddr(pair) for pair in to)
    if cc:
        message["Cc"] = ", ".join(email.utils.formataddr(pair) for pair in cc)
    if bcc:
        message["Bcc"] = ", ".join(email.utils.formataddr(pair) for pair in bcc)
    message["Subject"] = subject
    message["Date"] = email.utils.format_datetime(moment)
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message["X-Unsent"] = "1"
    message["X-LMWiki-Status"] = "not-sent"
    message["X-LMWiki-Mode"] = mode
    message["X-LMWiki-Workflow"] = marker
    # Quoted-printable keeps the file 7-bit clean for every mail client that opens it.
    message.set_content(body or "", subtype="plain", charset="utf-8", cte="quoted-printable")
    return message.as_bytes().decode("utf-8")


def ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
        .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    )


def ics_fold(line: str) -> str:
    parts = []
    current = ""
    size = 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > 75:
            parts.append(current)
            current = " " + char
            size = 1 + width
        else:
            current += char
            size += width
    parts.append(current)
    return "\r\n".join(parts)


def ics_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def zoned_instant(value: Any, zone: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00"
    match = INSTANT_RE.fullmatch(text)
    if not match:
        return None
    if match.group(8) is None and zone and zone not in ("UTC", "Etc/UTC"):
        from zoneinfo import ZoneInfo

        year, month, day, hour, minute, second = (int(match.group(index) or 0) for index in range(1, 7))
        return datetime(year, month, day, hour, minute, second, tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
    return parse_instant(text)


# ---------------------------------------------------------------------------
# the engine: executes runs of one version


class Outcome:
    def __init__(self, result: Any = None, *, stopped: bool = False, wait: Optional[dict[str, Any]] = None, error: Optional[str] = None):
        self.result = result
        self.stopped = stopped
        self.wait = wait
        self.error = error


class Engine:
    """Runs of one workflow version, step by step, against the plan's record view."""

    def __init__(self, builder: "PlanBuilder", version: dict[str, Any]):
        self.b = builder
        self.version = version
        self.graph = FlowGraph(version["trigger"], version["steps"])

    # -- control flow -------------------------------------------------------

    def start(self, run: dict[str, Any]) -> None:
        run["frames"] = [{"kind": "main", "queue": list(self.graph.trigger_next), "pending": []}]
        self.execute(run)

    def execute(self, run: dict[str, Any]) -> None:
        while run["frames"] and run["status"] == "RUNNING":
            frame = run["frames"][-1]
            if frame["queue"]:
                self.visit(run, frame, frame["queue"].pop(0))
                continue
            if frame["pending"]:
                return
            if frame["kind"] == "loop":
                self.next_iteration(run, frame)
            else:
                run["frames"].pop()
        if run["status"] == "RUNNING" and not run["frames"]:
            run["status"] = "COMPLETED"
            run["ended_at"] = self.b.now_iso

    def info(self, run: dict[str, Any], step_id: str) -> dict[str, Any]:
        return run["step_infos"].get(step_id) or {"status": "NOT_STARTED"}

    def continues_on_failure(self, step_id: str) -> bool:
        step = self.graph.steps.get(step_id, {})
        if step.get("type") == "IF_ELSE":
            return False
        options = (step.get("settings") or {}).get("errorHandlingOptions") or {}
        proceed = options.get("continueOnFailure") if isinstance(options, dict) else None
        return isinstance(proceed, dict) and proceed.get("value") is True

    def effective_status(self, run: dict[str, Any], parent: str, child: str) -> str:
        info = self.info(run, parent)
        status = info.get("status", "NOT_STARTED")
        if self.graph.steps.get(parent, {}).get("type") != "IF_ELSE" or child in self.graph.next_ids(parent):
            return status
        chosen = (info.get("result") or {}).get("matchingBranchId") if isinstance(info.get("result"), dict) else None
        if chosen is None:
            return status
        for branch in self.graph.branches(parent):
            if branch.get("id") == chosen:
                return status if child in (branch.get("nextStepIds") or []) else "SKIPPED"
        return "SKIPPED"

    def readiness(self, run: dict[str, Any], step_id: str) -> Optional[str]:
        parents = self.graph.parents(step_id)
        if not parents:
            return "EXECUTE"
        outcomes = []
        for parent in parents:
            status = self.effective_status(run, parent, step_id)
            info = self.info(run, parent)
            continued = status == "FAILED_SAFELY" and bool(info.get("error")) and self.continues_on_failure(parent)
            outcomes.append((status, continued))
        if not all(status in ("SUCCESS", "STOPPED", "SKIPPED", "FAILED_SAFELY") for status, _continued in outcomes):
            return None
        if any(status == "SUCCESS" or continued for status, continued in outcomes):
            return "EXECUTE"
        if any(status == "FAILED_SAFELY" for status, _continued in outcomes):
            return "FAILED_SAFELY"
        return "SKIPPED"

    def queue_children(self, run: dict[str, Any], frame: dict[str, Any], step_id: str) -> None:
        children = self.graph.next_ids(step_id) + self.graph.branch_ids(step_id)
        for child in children:
            if frame["kind"] == "loop" and child == frame["iterator"]:
                continue
            if child not in frame["queue"]:
                frame["queue"].append(child)

    def set_info(self, run: dict[str, Any], step_id: str, info: dict[str, Any]) -> None:
        previous = run["step_infos"].get(step_id) or {}
        for key in ("iterations", "last"):
            if key in previous:
                info[key] = previous[key]
        run["step_infos"][step_id] = info
        if info.get("status") in ("SUCCESS", "STOPPED") and "result" in info:
            run["context"][step_id] = info["result"]
        else:
            run["context"].pop(step_id, None)

    def visit(self, run: dict[str, Any], frame: dict[str, Any], step_id: str) -> None:
        if step_id not in self.graph.steps or self.info(run, step_id).get("status") != "NOT_STARTED":
            return
        decision = self.readiness(run, step_id)
        if decision is None:
            return
        if decision in ("SKIPPED", "FAILED_SAFELY"):
            self.set_info(run, step_id, {"status": decision})
            self.queue_children(run, frame, step_id)
            return
        segment = self.b.segment(run)
        segment["executed"] = segment.get("executed", 0) + 1
        if segment["executed"] > MAX_EXECUTED_STEPS:
            self.fail_run(run, f"the run executed more than {MAX_EXECUTED_STEPS} steps in this plan and was stopped")
            return
        step = self.graph.steps[step_id]
        if step["type"] == "ITERATOR":
            self.start_iterator(run, frame, step)
            return
        try:
            outcome = self.execute_step(run, step)
        except StepFailure as exc:
            outcome = Outcome(error=str(exc))
        except crm_formula.FormulaError as exc:
            outcome = Outcome(error=str(exc))
        except (CrmError, crm_filters.FilterError, ValueError, TypeError, KeyError, AttributeError) as exc:
            outcome = Outcome(error=f"{type(exc).__name__}: {exc}")
        self.finish(run, frame, step, outcome)

    def finish(self, run: dict[str, Any], frame: dict[str, Any], step: dict[str, Any], outcome: Outcome) -> None:
        step_id = step["id"]
        if outcome.wait is not None:
            self.set_info(run, step_id, {"status": "PENDING", "wait": outcome.wait})
            frame["pending"].append(step_id)
            return
        if outcome.error is not None:
            if self.continues_on_failure(step_id):
                self.set_info(run, step_id, {"status": "FAILED_SAFELY", "error": outcome.error})
                self.queue_children(run, frame, step_id)
                return
            self.set_info(run, step_id, {"status": "FAILED", "error": outcome.error})
            self.fail_scope(run, frame, step_id, outcome.error)
            return
        status = "STOPPED" if outcome.stopped else "SUCCESS"
        self.set_info(run, step_id, {"status": status, "result": outcome.result})
        self.queue_children(run, frame, step_id)

    def fail_scope(self, run: dict[str, Any], frame: dict[str, Any], step_id: str, error: str) -> None:
        if frame["kind"] == "loop":
            iterator = self.graph.steps.get(frame["iterator"], {})
            inp = (iterator.get("settings") or {}).get("input") or {}
            if inp.get("shouldContinueOnIterationFailure") is True:
                frame["failed"] = frame.get("failed", 0) + 1
                frame["queue"] = []
                frame["pending"] = []
                return
        self.fail_run(run, f"step {step_id}: {error}")

    def fail_run(self, run: dict[str, Any], error: str) -> None:
        run["status"] = "FAILED"
        run["error"] = error
        run["ended_at"] = self.b.now_iso
        run["frames"] = []

    # -- iterator -----------------------------------------------------------

    def start_iterator(self, run: dict[str, Any], frame: dict[str, Any], step: dict[str, Any]) -> None:
        step_id = step["id"]
        inp = (step.get("settings") or {}).get("input") or {}
        items = Resolver(run["context"]).resolve(inp.get("items"))
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except json.JSONDecodeError:
                items = None
        if not isinstance(items, list):
            self.finish(run, frame, step, Outcome(error=f"Iterator input items must be an array, not {template_text(items)[:80] or 'null'}"))
            return
        loop_ids = self.graph.loop_ids(step_id)
        if not loop_ids or not items:
            self.finish(run, frame, step, Outcome({"currentItemIndex": 0, "currentItem": None, "hasProcessedAllItems": True, "index": 0, "items": 0}))
            return
        if len(items) > MAX_ITERATIONS:
            self.finish(run, frame, step, Outcome(error=(
                f"the iterator received {len(items)} items; at most {MAX_ITERATIONS} are allowed. "
                "No item was processed. Narrow the search or split the work into several runs"
            )))
            return
        body = self.graph.loop_body(step_id)
        self.reset_body(run, body)
        result = {"currentItemIndex": 0, "currentItem": items[0], "hasProcessedAllItems": False, "index": 0, "items": len(items)}
        run["step_infos"][step_id] = {"status": "RUNNING", "result": result}
        run["context"][step_id] = result
        run["frames"].append({"kind": "loop", "iterator": step_id, "items": items, "index": 0, "queue": list(loop_ids), "pending": [], "body": body, "failed": 0})

    def reset_body(self, run: dict[str, Any], body: list[str]) -> None:
        for step_id in body:
            info = run["step_infos"].get(step_id)
            if info is None:
                continue
            kept = {"status": "NOT_STARTED"}
            if "iterations" in info:
                kept["iterations"] = info["iterations"]
            if info.get("status") not in (None, "NOT_STARTED"):
                kept["last"] = {key: info[key] for key in ("status", "result", "error") if key in info}
            elif "last" in info:
                kept["last"] = info["last"]
            run["step_infos"][step_id] = kept
            run["context"].pop(step_id, None)

    def next_iteration(self, run: dict[str, Any], frame: dict[str, Any]) -> None:
        for step_id in frame["body"]:
            info = run["step_infos"].get(step_id)
            if info and info.get("status") not in (None, "NOT_STARTED"):
                counts = info.setdefault("iterations", {})
                counts[info["status"]] = counts.get(info["status"], 0) + 1
        iterator = frame["iterator"]
        index = frame["index"] + 1
        items = frame["items"]
        if index < len(items):
            frame["index"] = index
            self.reset_body(run, frame["body"])
            result = {"currentItemIndex": index, "currentItem": items[index], "hasProcessedAllItems": False, "index": index, "items": len(items)}
            run["step_infos"][iterator]["result"] = result
            run["context"][iterator] = result
            frame["queue"] = list(self.graph.loop_ids(iterator))
            return
        run["frames"].pop()
        result = {"currentItemIndex": len(items), "currentItem": None, "hasProcessedAllItems": True, "index": len(items),
                  "items": len(items), "failedIterations": frame.get("failed", 0)}
        run["step_infos"][iterator] = {"status": "SUCCESS", "result": result}
        run["context"][iterator] = result
        if run["frames"]:
            parent = run["frames"][-1]
            for child in self.graph.next_ids(iterator):
                if not (parent["kind"] == "loop" and child == parent["iterator"]) and child not in parent["queue"]:
                    parent["queue"].append(child)

    def resume(self, run: dict[str, Any], step_id: str, result: Any) -> None:
        for frame in reversed(run["frames"]):
            if step_id in frame["pending"]:
                frame["pending"].remove(step_id)
                self.set_info(run, step_id, {"status": "SUCCESS", "result": result})
                self.queue_children(run, frame, step_id)
                break
        else:
            raise WorkflowError(f"run {run['run_id']} has no step {step_id} waiting")
        self.execute(run)

    def iteration_suffix(self, run: dict[str, Any]) -> str:
        indices = [str(frame["index"]) for frame in run["frames"] if frame["kind"] == "loop"]
        return ("-i" + "-".join(indices)) if indices else ""

    # -- steps --------------------------------------------------------------

    def execute_step(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        handler = getattr(self, "step_" + step["type"].lower(), None)
        if handler is None:
            raise StepFailure(f"{step['type']} is not supported")
        return handler(run, step)

    def raw_input(self, step: dict[str, Any]) -> Any:
        return (step.get("settings") or {}).get("input")

    def resolved_input(self, run: dict[str, Any], step: dict[str, Any], *, keep_env: bool = False) -> Any:
        resolver = Resolver(run["context"], keep_env=keep_env)
        value = resolver.resolve(deep(self.raw_input(step)))
        for token in resolver.missing:
            note = f"step {step['id']}: {{{{{token}}}}} resolved to nothing"
            notes = self.b.segment(run)["notes"]
            if note not in notes and len(notes) < 50:
                notes.append(note)
        return value

    def record_operation(self, run: dict[str, Any], step: dict[str, Any], operation: dict[str, Any]) -> None:
        operation["row"] = f"{run['run_id']}:{step['id']}{self.iteration_suffix(run)}"
        error = self.b.view.apply(operation)
        if error:
            raise StepFailure(error)
        self.b.ops.append(operation)
        counts = self.b.segment(run)["operations"]
        counts[operation["op"]] = counts.get(operation["op"], 0) + 1
        if operation["op"] in ("destroy", "erase", "merge"):
            self.b.destructive = True

    def object_name(self, inp: dict[str, Any], *, write: bool) -> str:
        name = inp.get("objectName")
        if not isinstance(name, str) or name not in self.b.dm.objects:
            raise StepFailure(f"unknown object {name!r}")
        if write and name in OBJECTS_BLOCKED_FROM_AUTOMATION:
            raise StepFailure(f"workflows may not change {name} records (blocked from automation)")
        return name

    def record_id(self, value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("id")
        if isinstance(value, str):
            parsed = parse_record_link(value)
            text = parsed[1] if parsed else value.strip().lower()
            if ":" in text and not UUID_RE.fullmatch(text):
                text = text.split(":", 1)[1]
            if UUID_RE.fullmatch(text):
                return text
        raise StepFailure(f"Object record ID is required and must be a UUID, not {template_text(value)[:80] or 'empty'}")

    def as_object(self, record: Optional[Record]) -> Optional[dict[str, Any]]:
        return record_to_object(self.b.dm, self.b.view, record) if record is not None else None

    def step_create_record(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.resolved_input(run, step)
        object_name = self.object_name(inp, write=True)
        values = dict(inp.get("objectRecord") or {})
        record_id = self.record_id(values["id"]) if values.get("id") else str(uuid.uuid4())
        self.record_operation(run, step, {"op": "create", "object": object_name, "id": record_id,
                                          "values": normalize_values(self.b.dm, self.b.view, object_name, values)})
        return Outcome(self.as_object(self.b.view.get(object_name, record_id)))

    def step_update_record(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.resolved_input(run, step)
        object_name = self.object_name(inp, write=True)
        record_id = self.record_id(inp.get("objectRecordId"))
        if self.b.view.get(object_name, record_id) is None:
            raise StepFailure(f"{object_name} record {record_id} does not exist")
        values = dict(inp.get("objectRecord") or {})
        wanted = inp.get("fieldsToUpdate")
        if isinstance(wanted, list):
            values = {key: value for key, value in values.items() if key in wanted}
        values.pop("id", None)
        if not values:
            raise StepFailure("Failed to update: No fields to update")
        self.record_operation(run, step, {"op": "update", "object": object_name, "record": record_id,
                                          "values": normalize_values(self.b.dm, self.b.view, object_name, values)})
        return Outcome(self.as_object(self.b.view.get(object_name, record_id)))

    def step_delete_record(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.resolved_input(run, step)
        object_name = self.object_name(inp, write=True)
        record_id = self.record_id(inp.get("objectRecordId"))
        existing = self.b.view.get(object_name, record_id)
        if existing is None:
            raise StepFailure(f"{object_name} record {record_id} does not exist")
        before = self.as_object(existing)
        destroy = inp.get("destroy") is True
        self.record_operation(run, step, {"op": "destroy" if destroy else "delete", "object": object_name, "record": record_id})
        self.b.deletions.append({"object": object_name, "id": record_id, "title": existing.data.get("crm_title"),
                                 "mode": "destroy" if destroy else "trash", "run_id": run["run_id"]})
        return Outcome(before)

    def upsert_match(self, object_name: str, values: dict[str, Any], match_fields: Any) -> dict[str, Any]:
        fields = self.b.dm.fields(object_name)

        def key_value(name: str) -> Optional[tuple[str, Any]]:
            definition = fields.get(name.split(".", 1)[0])
            if definition is None:
                return None
            if "." in name:
                value = values.get(name)
                if value is None:
                    base, sub = name.split(".", 1)
                    value = values.get(base, {}).get(sub) if isinstance(values.get(base), dict) else None
                return (name, value) if value not in (None, "") else None
            value = values.get(name)
            primary = {"EMAILS": "primaryEmail", "LINKS": "primaryLinkUrl", "PHONES": "primaryPhoneNumber"}.get(definition.get("type"))
            if primary:
                if isinstance(value, dict):
                    value = value.get(primary)
                if value in (None, "") and values.get(f"{name}.{primary}") not in (None, ""):
                    value = values.get(f"{name}.{primary}")
                return (f"{name}.{primary}", value) if value not in (None, "") else None
            return (name, value) if value not in (None, "") and not isinstance(value, (dict, list)) else None

        if values.get("id"):
            return {"id": self.record_id(values["id"])}
        candidates = [str(name) for name in match_fields] if isinstance(match_fields, list) else [
            name for name, definition in fields.items() if definition.get("unique")
        ]
        for name in candidates:
            found = key_value(name)
            if found:
                return {found[0]: found[1]}
        raise StepFailure(f"Failed to upsert: no id and no value for a unique field of {object_name} ({', '.join(candidates) or 'none'})")

    def step_upsert_record(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.resolved_input(run, step)
        object_name = self.object_name(inp, write=True)
        values = dict(inp.get("objectRecord") or {})
        match = self.upsert_match(object_name, values, inp.get("matchFields"))
        try:
            found = self.b.view.match(object_name, match)
        except CrmError as exc:
            raise StepFailure(str(exc)) from exc
        if len(found) > 1:
            raise StepFailure(f"{len(found)} {object_name} records match {match}; the match must be unique")
        record_id = found[0].id if found else (match.get("id") or str(uuid.uuid4()))
        self.record_operation(run, step, {"op": "upsert", "object": object_name, "match": match, "id": record_id,
                                          "values": normalize_values(self.b.dm, self.b.view, object_name, values)})
        return Outcome(self.as_object(self.b.view.get(object_name, record_id)))

    def step_find_records(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.resolved_input(run, step)
        object_name = self.object_name(inp, write=False)
        spec = inp.get("filter")

        def check(node: Any) -> None:
            if isinstance(node, dict):
                if "conditions" in node:
                    for child in node.get("conditions") or []:
                        check(child)
                elif node.get("value") is None and normalize_operand(node.get("operand") or node.get("operator")) not in (
                        "IS_EMPTY", "IS_NOT_EMPTY", "IS_NOT_NULL", "IS_IN_PAST", "IS_IN_FUTURE", "IS_TODAY"):
                    raise StepFailure(f"filter condition on {node.get('field')} has an empty value after variable resolution")
            elif isinstance(node, list):
                for child in node:
                    check(child)

        check(spec)
        limit = inp.get("limit", QUERY_MAX_RECORDS)
        limit_number = _to_number(limit)
        limit = QUERY_MAX_RECORDS if limit_number is None else min(max(int(limit_number), 1), QUERY_MAX_RECORDS)
        offset_number = _to_number(inp.get("offset", 0))
        offset = 0 if offset_number is None else max(0, int(offset_number))
        context = crm_filters.Context(now=self.b.now_iso, time_zone="UTC", me=self.b.me)
        try:
            matched = crm_filters.select_records(self.b.dm, self.b.view.records(object_name), filter_spec=spec,
                                                 sorts=_sorts(inp.get("orderBy")), context=context)
        except crm_filters.FilterError as exc:
            raise StepFailure(f"filter could not be evaluated: {exc}") from exc
        page =[self.as_object(record) for record in matched[offset:offset + limit]]
        return Outcome({"first": page[0] if page else None, "all": page, "totalCount": len(matched), "length": len(page)})

    def step_pick_record(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.resolved_input(run, step)
        object_name = self.object_name(inp, write=False)
        ids = [self.record_id(item) for item in inp.get("recordIds") or [] if item not in (None, "")]
        if not ids:
            raise StepFailure("Pick record action has no candidate records")
        candidates = sorted((record for record in (self.b.view.get(object_name, item) for item in ids) if record is not None and not record.deleted), key=lambda record: record.id)
        if not candidates:
            raise StepFailure("Pick record action could not find any of the candidate records")
        strategy = inp.get("strategy")
        if strategy == "LOAD_BALANCED":
            balance = inp.get("loadBalance") or {}
            counted, field_name = balance.get("objectNameSingular"), balance.get("fieldName")
            if counted not in self.b.dm.objects:
                raise StepFailure("Pick record action is missing its load balancing configuration")
            load = {record.id: 0 for record in candidates}
            for other in self.b.view.records(counted):
                if other.deleted:
                    continue
                value = other.data.get(field_name)
                for link in (value if isinstance(value, list) else [value]):
                    parsed = parse_record_link(link) if isinstance(link, str) else None
                    if parsed and parsed[1] in load:
                        load[parsed[1]] += 1
            index = min(range(len(candidates)), key=lambda position: (load[candidates[position].id], position))
        elif strategy == "ROUND_ROBIN":
            cursors = self.b.workflow_state.setdefault("round_robin", {})
            cursor = int(cursors.get(step["id"], 0))
            index = cursor % len(candidates)
            cursors[step["id"]] = cursor + 1
        else:
            seed = f"{run['run_id']}:{step['id']}{self.iteration_suffix(run)}"
            index = random.Random(seed).randrange(len(candidates))
        return Outcome(self.as_object(candidates[index]))

    def step_filter(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.raw_input(step) or {}
        if "condition" not in inp and not inp.get("stepFilters") and not inp.get("stepFilterGroups"):
            return Outcome({"matchesFilter": True})
        matches = container_matches(inp, run["context"], self.b.now)
        return Outcome({"matchesFilter": matches}, stopped=not matches)

    def step_if_else(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        branch = matching_branch(self.raw_input(step) or {}, run["context"], self.b.now)
        return Outcome({"matchingBranchId": branch.get("id")})

    def step_empty(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        return Outcome({})

    def step_code(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.raw_input(step) or {}
        params = Resolver(run["context"]).resolve(deep(inp.get("logicFunctionInput") or {}))
        return Outcome(crm_formula.evaluate_many(inp.get("formulas") or {}, params, now=self.b.now))

    def step_delay(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.resolved_input(run, step)
        if inp.get("delayType") == "SCHEDULED_DATE":
            value = inp.get("scheduledDateTime")
            moment = parse_instant(value if isinstance(value, str) and "T" in value else f"{value}T00:00:00Z")
            if moment is None:
                raise StepFailure("Scheduled date time is required for scheduled date delay")
            if moment < self.b.now:
                raise StepFailure("Scheduled date cannot be in the past")
        else:
            duration = inp.get("duration") or {}
            parts = {}
            for key in ("days", "hours", "minutes", "seconds"):
                number = _to_number(duration.get(key, 0) if duration.get(key) not in ("", None) else 0)
                if number is None or number < 0:
                    raise StepFailure(f"duration.{key} must be a non-negative number")
                parts[key] = number
            moment = self.b.now + timedelta(**parts)
        if moment <= self.b.now:
            return Outcome({"success": True, "resumedLateBySeconds": 0})
        return Outcome(wait={"type": "TIME", "resumeAt": format_instant(moment)})

    def step_wait_for_event(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        raw = self.raw_input(step) or {}
        inp = self.resolved_input(run, step)
        name = inp.get("eventName")
        if not isinstance(name, str) or not EVENT_NAME_RE.fullmatch(name):
            raise StepFailure(f"Invalid event to wait for: {name!r}")
        record_id = inp.get("recordId")
        if raw.get("recordId") and not record_id:
            raise StepFailure("The record to wait for resolved to no record")
        wait: dict[str, Any] = {"type": "EVENT", "eventName": name, "armedCursor": event_line_counts(self.b.target)}
        if record_id:
            wait["recordId"] = self.record_id(record_id)
        if inp.get("updatedFields"):
            wait["updatedFields"] = [str(item) for item in inp["updatedFields"]]
        timeout = inp.get("timeout") or {}
        if timeout:
            total = 0.0
            for key, factor in (("days", 1440), ("hours", 60), ("minutes", 1)):
                number = _to_number(timeout.get(key) or 0)
                if number is None or number < 0:
                    raise StepFailure("Wait timeout must be made of non-negative numbers")
                total += number * factor
            if total > 365 * 1440:
                raise StepFailure("Wait timeout cannot exceed one year")
            if total > 0:
                wait["expiresAt"] = format_instant(self.b.now + timedelta(minutes=total))
        return Outcome(wait=wait)

    def ask(self, run: dict[str, Any], step: dict[str, Any], request: dict[str, Any]) -> Outcome:
        request = {"type": step["type"], "title": step.get("name") or step["id"], **request}
        return Outcome(wait={"type": "ANSWER", "request": request})

    def step_form(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        resolver = Resolver(run["context"])
        fields = []
        for field in self.raw_input(step) or []:
            extra = field.get("settings") if isinstance(field.get("settings"), dict) else {}
            entry = {"name": field.get("name"), "label": resolver.resolve(field.get("label")), "type": field.get("type")}
            if field.get("placeholder") not in (None, ""):
                entry["placeholder"] = resolver.resolve(field.get("placeholder"))
            if field.get("value") not in (None, ""):
                entry["default"] = resolver.resolve(field.get("value"))
            if field.get("type") in ("SELECT", "MULTI_SELECT"):
                entry["options"] = _form_options(self.b.dm, extra)
            if field.get("type") == "RECORD":
                entry["objectName"] = extra.get("objectName")
            fields.append(entry)
        instructions = resolver.resolve((step.get("settings") or {}).get("instructions") or "")
        return self.ask(run, step, {"instructions": instructions, "fields": fields})

    def step_ai_agent(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.resolved_input(run, step)
        request = {"prompt": inp.get("prompt"), "outputFields": inp.get("outputFields") or {"response": "TEXT"}}
        if inp.get("humanInputInstructions"):
            request["humanInputInstructions"] = inp["humanInputInstructions"]
        return self.ask(run, step, request)

    def step_classify(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.resolved_input(run, step)
        questions = []
        for question in inp.get("questions") or []:
            criteria = [item.get("name") if isinstance(item, dict) else item for item in question.get("criteria") or []]
            questions.append({"name": question.get("name"), "type": question.get("type"), "instructions": question.get("instructions"), "criteria": criteria})
        return self.ask(run, step, {"state": inp.get("state"), "questions": questions})

    def step_send_chat_message(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.resolved_input(run, step)
        return self.ask(run, step, {"text": inp.get("text"), "messageTitle": inp.get("title"), "workspaceMemberId": inp.get("workspaceMemberId")})

    def outbox_file(self, run: dict[str, Any], step: dict[str, Any], extension: str, content: str, kind: str) -> str:
        name = safe_name(f"{self.b.workflow_id}-{run['run_id']}-{step['id']}{self.iteration_suffix(run)}") + extension
        findings = record_credential_findings(self.b.target, f"{self.b.outbox_folder}/{name}", content)
        if findings:
            raise StepFailure(f"the {kind} file would contain credential-like values ({', '.join(sorted({found for _line, found in findings}))}); "
                              "keep secrets out of workflow texts")
        self.b.outbox.append({"name": name, "kind": kind, "content": content, "sha256": sha256_text(content)})
        self.b.segment(run)["outbox"].append(name)
        return name

    def step_send_email(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        return self.email(run, step, "SEND")

    def step_draft_email(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        return self.email(run, step, "DRAFT")

    def email(self, run: dict[str, Any], step: dict[str, Any], mode: str) -> Outcome:
        inp = self.resolved_input(run, step)
        recipients = inp.get("recipients") or {}
        to = parse_addresses(recipients.get("to"))
        if not to:
            raise StepFailure("the e-mail has no recipient (recipients.to resolved to nothing)")
        cc = parse_addresses(recipients.get("cc"))
        bcc = parse_addresses(recipients.get("bcc"))
        sender = _header(inp.get("fromHandle") or "") or self.b.actor_email or ""
        if sender:
            parse_addresses(sender)
        domain = sender.rsplit("@", 1)[-1].strip(">") if "@" in sender else "lmwiki.invalid"
        message_id = f"<{run['run_id']}.{step['id']}{self.iteration_suffix(run)}@{domain}>"
        reply_to = _header(inp.get("inReplyTo") or "")
        if reply_to and not reply_to.startswith("<"):
            reply_to = f"<{reply_to}>"
        subject = _header(inp.get("subject") or "")
        body = template_text(inp.get("body") or "")
        content = build_eml(sender=sender or None, to=to, cc=cc, bcc=bcc, subject=subject, body=body, moment=self.b.now,
                            message_id=message_id, in_reply_to=reply_to or None, mode=mode,
                            marker=f"{self.b.workflow_id} v{self.version['version']} {run['run_id']}")
        name = self.outbox_file(run, step, ".eml", content, "eml")
        result = {"emlFile": name, "sent": False, "mode": mode, "messageId": message_id, "subject": subject, "from": sender or None,
                  "recipients": {"to": [address for _name, address in to], "cc": [address for _name, address in cc], "bcc": [address for _name, address in bcc]}}
        if inp.get("files"):
            result["attachmentsSkipped"] = len(inp["files"]) if isinstance(inp["files"], list) else 1
        return Outcome(result)

    def step_create_calendar_event(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.resolved_input(run, step)
        title = _header(inp.get("title") or "")
        if not title:
            raise StepFailure("the event needs a title")
        zone = inp.get("timeZone") or None
        if zone and not _valid_zone(zone):
            raise StepFailure(f"unknown time zone {zone!r}")
        full_day = inp.get("isFullDay") is True or inp.get("isFullDay") == "true"
        start = zoned_instant(inp.get("startsAt"), zone)
        end = zoned_instant(inp.get("endsAt"), zone)
        if start is None or end is None:
            raise StepFailure("startsAt and endsAt must be ISO dates or instants")
        uid = f"{run['run_id']}-{step['id']}{self.iteration_suffix(run)}@lmwiki.invalid"
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AI First CRM//ai-first-crm CRM//EN", "CALSCALE:GREGORIAN",
                 "METHOD:PUBLISH", "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{ics_time(self.b.now)}"]
        if full_day:
            first = str(inp.get("startsAt"))[:10]
            last = str(inp.get("endsAt"))[:10]
            first_day = date.fromisoformat(first)
            last_day = max(date.fromisoformat(last), first_day)
            lines += [f"DTSTART;VALUE=DATE:{first_day.strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{(last_day + timedelta(days=1)).strftime('%Y%m%d')}"]
        else:
            if end <= start:
                raise StepFailure("endsAt must be after startsAt")
            lines += [f"DTSTART:{ics_time(start)}", f"DTEND:{ics_time(end)}"]
        lines.append(f"SUMMARY:{ics_escape(title)}")
        if inp.get("description"):
            lines.append(f"DESCRIPTION:{ics_escape(template_text(inp['description']))}")
        if inp.get("location"):
            lines.append(f"LOCATION:{ics_escape(template_text(inp['location']))}")
        attendees = parse_addresses(inp.get("attendees"))
        for name, address in attendees:
            plain = name.replace('"', "")
            params = (f';CN="{plain}"' if re.search(r"[:;,]", plain) else f";CN={plain}") if plain else ""
            lines.append(f"ATTENDEE{params};RSVP=FALSE:mailto:{address}")
        lines += ["STATUS:CONFIRMED", "X-LMWIKI-STATUS:not-sent", "END:VEVENT", "END:VCALENDAR"]
        content = "\r\n".join(ics_fold(line) for line in lines) + "\r\n"
        name = self.outbox_file(run, step, ".ics", content, "ics")
        return Outcome({"icsFile": name, "iCalUid": uid, "title": title, "startsAt": format_instant(start), "endsAt": format_instant(end),
                        "isFullDay": full_day, "attendees": [address for _name, address in attendees], "invitationsSent": False, "conferenceLink": None})

    def step_http_request(self, run: dict[str, Any], step: dict[str, Any]) -> Outcome:
        inp = self.resolved_input(run, step, keep_env=True)
        url = template_text(inp.get("url") or "")
        if not re.match(r"^https?://", url):
            raise StepFailure(f"url {url[:80]!r} must start with http:// or https://")
        method = str(inp.get("method") or "GET").upper()
        headers = []
        for name, value in (inp.get("headers") or {}).items():
            if isinstance(value, dict) and "env" in value:
                headers.append({"name": name, "valueFromEnv": value["env"], "prefix": value.get("prefix", "")})
            elif is_env_reference(value):
                headers.append({"name": name, "template": value})
            elif is_sensitive_key(str(name)):
                headers.append({"name": name, "value": REDACTED})
            else:
                headers.append({"name": name, "value": redact_text(template_text(value))[0]})
        body, _count = redact(inp.get("body"), key_names=True)
        spec = {
            "format": REQUEST_FORMAT,
            "status": "not-executed",
            "note": ("The skill never sends this request. Review it and send it with a tool you trust; "
                     "credentials come from the named environment variables at that moment and are never stored."),
            "workflow": self.b.workflow_id, "version": self.version["version"], "run_id": run["run_id"], "step_id": step["id"],
            "created_at": self.b.now_iso,
            "request": {"method": method, "url": redact_url(url), "headers": headers, "body": body},
        }
        content = json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        name = self.outbox_file(run, step, ".http.json", content, "http-request")
        return Outcome({"executed": False, "requestFile": name, "method": method, "url": redact_url(url)})

    # -- answers ------------------------------------------------------------

    def answer_result(self, run: dict[str, Any], step_id: str, answer: Any) -> Any:
        info = self.info(run, step_id)
        wait = info.get("wait") or {}
        if info.get("status") != "PENDING" or wait.get("type") != "ANSWER":
            raise WorkflowError(f"run {run['run_id']} has no step {step_id} waiting for an answer")
        request = wait.get("request") or {}
        kind = request.get("type")
        where = f"answer for run {run['run_id']} step {step_id}"
        if kind == "FORM":
            if not isinstance(answer, dict):
                raise WorkflowError(f"{where}: a form answer is an object of field values")
            fields = {field["name"]: field for field in request.get("fields") or []}
            for key in answer:
                if key not in fields:
                    raise WorkflowError(f"{where}: the form has no field {key!r}")
            result = {}
            for name, field in fields.items():
                result[name] = self.form_value(field, answer.get(name, field.get("default")), where)
            return result
        if kind == "AI_AGENT":
            output = request.get("outputFields") or {"response": "TEXT"}
            if isinstance(answer, str) and list(output) == ["response"]:
                answer = {"response": answer}
            if not isinstance(answer, dict):
                raise WorkflowError(f"{where}: the agent answer is an object with {', '.join(output)}")
            for key in answer:
                if key not in output:
                    raise WorkflowError(f"{where}: unexpected output field {key!r}")
            return {key: self.typed_value(answer.get(key), output[key], f"{where}: {key}") for key in output}
        if kind == "CLASSIFY":
            values = answer.get("answers", answer) if isinstance(answer, dict) else None
            if not isinstance(values, dict):
                raise WorkflowError(f"{where}: a classification answer maps question names to answers")
            answers, scores = {}, {}
            for question in request.get("questions") or []:
                name = question["name"]
                if name not in values:
                    raise WorkflowError(f"{where}: question {name} is not answered")
                value = values[name]
                criteria = question.get("criteria") or []
                if question.get("type") == "boolean":
                    if isinstance(value, bool):
                        answers[name] = value
                    elif isinstance(value, (int, float)) and 0 <= value <= 1:
                        answers[name] = value >= 0.5
                        scores[name] = value
                    else:
                        raise WorkflowError(f"{where}: {name} needs true, false or a probability from 0 to 1")
                elif question.get("type") == "score" and isinstance(value, int) and not isinstance(value, bool):
                    if not 0 <= value < len(criteria):
                        raise WorkflowError(f"{where}: {name} score must be 0 to {len(criteria) - 1}")
                    answers[name] = criteria[value]
                    scores[name] = value
                elif value in criteria:
                    answers[name] = value
                    if question.get("type") == "score":
                        scores[name] = criteria.index(value)
                else:
                    raise WorkflowError(f"{where}: {name} must be one of {', '.join(map(str, criteria))}, not {value!r}")
            extra = set(values) - {question["name"] for question in request.get("questions") or []}
            if extra:
                raise WorkflowError(f"{where}: unknown questions {', '.join(sorted(extra))}")
            return {"answers": answers, "scores": scores, "modelId": "host-agent"}
        if kind == "SEND_CHAT_MESSAGE":
            reply = answer.get("reply") if isinstance(answer, dict) else answer
            if not isinstance(reply, str) or not reply.strip():
                raise WorkflowError(f"{where}: the reply must be a non-empty text")
            return {"reply": reply, "outcome": "answered", "threadId": run["run_id"]}
        raise WorkflowError(f"{where}: step type {kind} does not take answers")

    def typed_value(self, value: Any, kind: str, where: str) -> Any:
        if value is None:
            return None
        checks = {
            "TEXT": isinstance(value, str), "NUMBER": isinstance(value, (int, float)) and not isinstance(value, bool),
            "BOOLEAN": isinstance(value, bool), "LIST": isinstance(value, list), "OBJECT": isinstance(value, dict),
            "DATE": isinstance(value, str) and parse_instant(value if "T" in value else value + "T00:00:00Z") is not None,
        }
        if not checks.get(kind, False):
            raise WorkflowError(f"{where} must be {kind}")
        return value

    def form_value(self, field: dict[str, Any], value: Any, where: str) -> Any:
        name = field["name"]
        kind = field.get("type")
        if value in (None, ""):
            return None
        if kind == "TEXT":
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise WorkflowError(f"{where}: {name} must be text")
            return str(value)
        if kind == "NUMBER":
            number = _to_number(value)
            if number is None or isinstance(value, bool):
                raise WorkflowError(f"{where}: {name} must be a number")
            return int(number) if number.is_integer() else number
        if kind == "DATE":
            text = str(value)
            if parse_instant(text if "T" in text else text + "T00:00:00Z") is None:
                raise WorkflowError(f"{where}: {name} must be a date YYYY-MM-DD")
            return text
        options = field.get("options") or []
        if kind == "SELECT":
            if value not in options:
                raise WorkflowError(f"{where}: {name} must be one of {', '.join(options)}")
            return value
        if kind == "MULTI_SELECT":
            values = value if isinstance(value, list) else [value]
            for item in values:
                if item not in options:
                    raise WorkflowError(f"{where}: {name} must use options {', '.join(options)}")
            return values
        if kind == "RECORD":
            object_name = field.get("objectName")
            try:
                record_id = self.record_id(value)
            except StepFailure as exc:
                raise WorkflowError(f"{where}: {name}: {exc}") from exc
            record = self.b.view.get(object_name, record_id) if object_name in self.b.dm.objects else None
            if record is None:
                raise WorkflowError(f"{where}: {name}: {object_name} record {record_id} does not exist")
            return record_to_object(self.b.dm, self.b.view, record, 0)
        raise WorkflowError(f"{where}: {name} has an unsupported type {kind}")


# ---------------------------------------------------------------------------
# planning


class PlanBuilder:
    """Collects the runs of one plan: their record operations, log entries, state and outbox files."""

    def __init__(self, target: Path, workflow_id: str, actor: str, now: datetime, outbox_dir: Optional[Path]):
        self.target = target
        self.dm = load_datamodel(target)
        self.raw, self.relative, self.file_sha = read_workflow(target, workflow_id)
        errors, self.definition_warnings = check_workflow(target, self.dm, self.raw, self.relative)
        if errors:
            raise InvalidDefinition(errors)
        self.workflow_id = workflow_id
        self.actor = actor
        self.actor_email = actor.split(":", 1)[1] if actor.startswith("human:") and EMAIL_RE.fullmatch(actor.split(":", 1)[1]) else None
        self.now = now
        self.now_iso = format_instant(now)
        self.outbox_dir = outbox_dir
        self.outbox_folder = f"{OUTBOX_DIR}/workflows/{safe_name(workflow_id)}"
        self.state_before, self.state_exists, self.state_sha = read_state(target)
        self.state = deep(self.state_before)
        self.workflow_state = self.state["workflows"].setdefault(workflow_id, {})
        self.view = RecordView(target, self.dm, actor, workflow_id, self.now_iso)
        self.me = self.member_for_actor()
        self.ops: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.segments: dict[str, dict[str, Any]] = {}
        self.outbox: list[dict[str, Any]] = []
        self.deletions: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.warnings: list[str] = list(self.definition_warnings)
        self.engines: dict[int, Engine] = {}
        self.destructive = False
        self.stats: dict[str, int] = {}
        self.depths: Optional[dict[str, int]] = None
        self._events: Optional[list[dict[str, Any]]] = None

    def events_after(self, cursor: dict[str, int]) -> list[dict[str, Any]]:
        if self._events is None:
            self._events = list(read_events(self.target))
        return [event for event in self._events if event["_line"] > int(cursor.get(event["_shard"], 0))]

    def count(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount

    def member_for_actor(self) -> Optional[str]:
        if not self.actor_email or "workspaceMember" not in self.dm.objects:
            return None
        for record in self.view.records("workspaceMember"):
            if str(record.data.get("userEmail") or "").casefold() == self.actor_email.casefold():
                return record.id
        return None

    def segment(self, run: dict[str, Any]) -> dict[str, Any]:
        return self.segments.setdefault(run["run_id"], {"operations": {}, "outbox": [], "notes": [], "resumed": False, "executed": 0})

    def engine(self, number: int) -> Engine:
        if number not in self.engines:
            self.engines[number] = Engine(self, find_version(self.raw, number))
        return self.engines[number]

    def run_version(self, explicit: Optional[int]) -> dict[str, Any]:
        if explicit is not None:
            return find_version(self.raw, explicit)
        version = active_version(self.raw)
        if version is None:
            raise WorkflowError(f"workflow {self.workflow_id} has no ACTIVE version; activate one, or pass --version N for a test run")
        return version

    def new_run(self, version: dict[str, Any], trigger_output: dict[str, Any], info: dict[str, Any], *, depth: int = 0, test: bool = False) -> dict[str, Any]:
        if len(self.runs) >= MAX_RUNS_PER_PLAN:
            raise WorkflowError(f"a plan holds at most {MAX_RUNS_PER_PLAN} runs; split the work")
        run = {
            "run_id": new_run_id(), "workflow": self.workflow_id, "version": version["version"],
            "definition_sha256": version_hash(version), "trigger": {"type": version["trigger"]["type"], **info},
            "context": {"trigger": trigger_output}, "step_infos": {}, "frames": [], "status": "RUNNING",
            "started_at": self.now_iso, "ended_at": None, "cascade_depth": depth, "launched_by": self.actor, "test": test,
        }
        self.runs.append(run)
        self.segment(run)
        self.engine(version["version"]).start(run)
        self.count("runs_started")
        return run

    # -- pending runs -------------------------------------------------------

    def pending_run(self, run_id: str) -> tuple[dict[str, Any], Engine]:
        for run in self.runs:
            if run["run_id"] == run_id:
                return run, self.engine(run["version"])
        stored = self.state["pending_runs"].get(run_id)
        if not isinstance(stored, dict) or stored.get("workflow") != self.workflow_id:
            raise WorkflowError(f"run {run_id} of workflow {self.workflow_id} is not waiting")
        try:
            version = find_version(self.raw, stored["version"])
        except WorkflowError as exc:
            raise WorkflowError(f"run {run_id} cannot be resumed: version {stored.get('version')} no longer exists") from exc
        if version_hash(version) != stored.get("definition_sha256"):
            raise WorkflowError(
                f"run {run_id} cannot be resumed: version {stored['version']} changed since the run started; "
                "a waiting run continues only with the version it started with (cancel it with run cancel)"
            )
        run = deep(stored)
        self.runs.append(run)
        self.segment(run)["resumed"] = True
        self.count("runs_resumed")
        return run, self.engine(run["version"])

    def pending_steps(self, run: dict[str, Any]) -> list[str]:
        return [step_id for frame in run.get("frames", []) for step_id in frame.get("pending", [])]

    def apply_answers(self, answers: list[dict[str, Any]]) -> None:
        for entry in answers:
            run_id, step_id = entry.get("run_id"), entry.get("step_id")
            if not isinstance(run_id, str) or not isinstance(step_id, str) or "answer" not in entry:
                self.errors.append("every answer needs run_id, step_id and answer")
                continue
            try:
                stored = self.state["pending_runs"].get(run_id)
                if isinstance(stored, dict) and stored.get("workflow") != self.workflow_id:
                    raise WorkflowError(f"run {run_id} belongs to workflow {stored.get('workflow')}, not {self.workflow_id}")
                run, engine = self.pending_run(run_id)
                result = engine.answer_result(run, step_id, entry["answer"])
                engine.resume(run, step_id, result)
            except WorkflowError as exc:
                self.errors.append(str(exc))

    def due_waits(self, run: dict[str, Any]) -> list[tuple[str, Any]]:
        due: list[tuple[str, Any]] = []
        for step_id in self.pending_steps(run):
            wait = (run["step_infos"].get(step_id) or {}).get("wait") or {}
            if wait.get("type") == "TIME":
                moment = parse_instant(wait.get("resumeAt"))
                if moment is not None and moment <= self.now:
                    due.append((step_id, {"success": True, "resumedLateBySeconds": int((self.now - moment).total_seconds())}))
            elif wait.get("type") == "EVENT":
                outcome = self.event_wait_outcome(run["run_id"], wait)
                if outcome is not None:
                    due.append((step_id, outcome))
        return due

    def resume_due(self) -> None:
        for run_id, stored in sorted(self.state["pending_runs"].items()):
            if not isinstance(stored, dict) or stored.get("workflow") != self.workflow_id:
                continue
            if not self.due_waits(next((run for run in self.runs if run["run_id"] == run_id), stored)):
                continue
            try:
                run, engine = self.pending_run(run_id)
                for step_id, result in self.due_waits(run):
                    engine.resume(run, step_id, result)
                    self.count("delays_resumed" if "success" in result else "event_waits_resumed")
            except WorkflowError as exc:
                self.errors.append(str(exc))

    def event_wait_outcome(self, run_id: str, wait: dict[str, Any]) -> Optional[dict[str, Any]]:
        match = EVENT_NAME_RE.fullmatch(str(wait.get("eventName")))
        if not match:
            return None
        object_name, action = match.groups()
        own = {item for item in (run_id, wait.get("ownOriginRunId")) if item}
        for event in self.events_after(wait.get("armedCursor") or {}):
            origin = event.get("origin") or {}
            if (origin.get("run_id") and origin.get("run_id") in own) or event.get("object") != object_name or is_migration_event(event):
                continue
            if action not in EVENT_ACTIONS.get(event.get("op"), set()):
                continue
            if wait.get("recordId") and event.get("record_id") != wait["recordId"]:
                continue
            if wait.get("updatedFields") and not set(wait["updatedFields"]) & set(changed_fields(event)):
                continue
            output = self.event_output(event, action)
            return {"hasTimedOut": False, **output}
        expires = parse_instant(wait.get("expiresAt"))
        if expires is not None and expires <= self.now:
            return {"hasTimedOut": True}
        return None

    # -- triggers -----------------------------------------------------------

    def event_output(self, event: dict[str, Any], action: str) -> dict[str, Any]:
        object_name, record_id = event["object"], event["record_id"]
        record = self.view.get(object_name, record_id)
        current = record_to_object(self.dm, self.view, record) if record is not None else {"id": record_id}
        after, before = deep(current), deep(current)
        diff: dict[str, Any] = {}
        fields = self.dm.fields(object_name) if object_name in self.dm.objects else {}
        for key, change in (event.get("changes") or {}).items():
            if key.startswith("richtext:") or not isinstance(change, list) or len(change) != 2:
                continue
            old, new = change
            base, _, sub = key.partition(".")
            if base == "crm_deleted_at":
                before["deletedAt"], after["deletedAt"] = old, new
                continue
            if base.startswith("crm_"):
                continue
            definition = fields.get(base, {})
            if definition.get("type") in ("RELATION", "MORPH_RELATION"):
                old, new = self.project_link(old), self.project_link(new)
                if definition.get("type") == "RELATION":
                    before[base + "Id"] = old["id"] if isinstance(old, dict) else None
                    after[base + "Id"] = new["id"] if isinstance(new, dict) else None
            if sub:
                for side, value in ((before, old), (after, new)):
                    if not isinstance(side.get(base), dict):
                        side[base] = {}
                    side[base][sub] = value
                entry = diff.setdefault(base, {"before": {}, "after": {}})
                entry["before"][sub], entry["after"][sub] = old, new
            else:
                before[base], after[base] = old, new
                diff[base] = {"before": old, "after": new}
        if action == "created" or event.get("op") == "create":
            before_value: Any = None
        else:
            before_value = before
        subject = before if action == "deleted" else after
        return {
            "recordId": record_id, "objectName": object_name, "eventName": f"{object_name}.{action}",
            "object": subject, "properties": {"before": before_value, "after": None if action == "deleted" else after,
                                              "updatedFields": changed_fields(event), "diff": diff},
            "event": {"id": event.get("event_id"), "at": event.get("at"), "op": event.get("op"), "actor": event.get("actor"),
                      "origin": (event.get("origin") or {}).get("kind")},
        }

    def project_link(self, value: Any) -> Any:
        if isinstance(value, list):
            return [_link_object(self.dm, item) for item in value]
        return _link_object(self.dm, value) if value else None

    def plan_events(self, version: dict[str, Any]) -> None:
        settings = version["trigger"].get("settings") or {}
        object_name, action = EVENT_NAME_RE.fullmatch(settings["eventName"]).groups()
        watched = set(settings.get("fields") or [])
        cursor = self.workflow_state.get("event_cursor")
        if not isinstance(cursor, dict):
            activated = parse_instant(version.get("activated_at")) or self.now
            events = [event for event in self.events_after({}) if (parse_instant(event.get("at")) or self.now) >= activated]
            cursor = {}
            self.warnings.append("no event cursor was stored for this workflow; events since its activation were read")
        else:
            events = self.events_after(cursor)
        new_cursor = dict(cursor)
        visited: set[tuple[str, str, str]] = set()
        for event in events:
            if len(self.runs) >= MAX_RUNS_PER_PLAN:
                self.count("events_deferred")
                continue
            new_cursor[event["_shard"]] = max(int(new_cursor.get(event["_shard"], 0)), event["_line"])
            self.count("events_scanned")
            if event.get("object") != object_name or action not in EVENT_ACTIONS.get(event.get("op"), set()):
                continue
            if is_migration_event(event):
                self.count("events_ignored_migration")
                continue
            # Loop protection comes first: a workflow never reacts to its own writes, and a write
            # caused by another workflow triggers at most one further level.
            origin = event.get("origin") or {}
            depth = 0
            if origin.get("kind") == "workflow":
                if origin.get("workflow") == self.workflow_id:
                    self.count("events_ignored_own")
                    continue
                if self.depths is None:
                    self.depths = run_depth_index(self.target)
                parent = self.depths.get(str(origin.get("run_id")))
                depth = (parent if parent is not None else MAX_CASCADE_DEPTH) + 1
                if depth > MAX_CASCADE_DEPTH:
                    self.count("events_ignored_depth")
                    continue
            if watched and action in ("updated", "upserted") and not watched & set(changed_fields(event)):
                self.count("events_skipped_fields")
                continue
            output = self.event_output(event, action)
            if isinstance(settings.get("filter"), dict):
                try:
                    if not container_matches(settings["filter"], {"trigger": output}, self.now):
                        self.count("events_skipped_filter")
                        continue
                except StepFailure as exc:
                    self.warnings.append(f"trigger filter could not be evaluated for event {event.get('event_id')}: {exc}")
                    continue
            key = (self.workflow_id, str(event.get("record_id")), sha256_bytes(canonical_json(event.get("changes") or {})))
            if key in visited:
                self.count("events_skipped_duplicate")
                continue
            visited.add(key)
            self.new_run(version, output, {"event_id": event.get("event_id"), "event_at": event.get("at"),
                                           "record": f"{object_name}:{event.get('record_id')}", "eventName": output["eventName"]}, depth=depth)
        if not self.stats.get("events_deferred"):
            # Everything was read: the cursor moves to the end of every shard, blank lines included.
            for shard, lines in event_line_counts(self.target).items():
                new_cursor[shard] = max(int(new_cursor.get(shard, 0)), lines)
        else:
            self.warnings.append(f"{self.stats['events_deferred']} events wait for the next run plan --due (at most {MAX_RUNS_PER_PLAN} runs per plan)")
        self.workflow_state["event_cursor"] = new_cursor

    def plan_cron(self, version: dict[str, Any]) -> None:
        schedule = CronSchedule(cron_pattern(version["trigger"].get("settings") or {}))
        checked = parse_instant(self.workflow_state.get("cron_checked_at")) or parse_instant(version.get("activated_at")) or self.now
        count, first, last = schedule.slots(checked, self.now)
        if not count:
            return
        self.workflow_state["cron_checked_at"] = self.now_iso
        self.count("cron_due_slots", count)
        self.count("missed_cron_slots", count - 1)
        output = {"scheduledAt": format_instant(last), "dueSlots": count, "missedSlots": count - 1,
                  "firstDueAt": format_instant(first), "pattern": schedule.pattern}
        self.new_run(version, output, {"scheduledAt": output["scheduledAt"], "dueSlots": count, "missedSlots": count - 1})

    def plan_due(self) -> None:
        self.resume_due()
        version = active_version(self.raw)
        if version is None:
            return
        kind = version["trigger"]["type"]
        if kind == "CRON":
            self.plan_cron(version)
        elif kind == "DATABASE_EVENT":
            self.plan_events(version)

    def resolve_selector(self, object_name: str, selector: Any) -> Record:
        if isinstance(selector, str):
            text = selector.strip()
            if ":" in text and not text.startswith("[[") and text.split(":", 1)[0] in self.dm.objects:
                named, text = text.split(":", 1)
                if named != object_name:
                    raise WorkflowError(f"record {selector} is not a {object_name}")
            selector = text
        try:
            record = self.view.planner.resolve_record(object_name, selector, include_deleted=False)
        except CrmError as exc:
            raise WorkflowError(str(exc)) from exc
        if record.deleted:
            raise WorkflowError(f"{object_name} record {record.id} is in the trash")
        return record

    def plan_explicit(self, version: dict[str, Any], selectors: list[Any], payload: Optional[Any], test: bool) -> None:
        trigger = version["trigger"]
        settings = trigger.get("settings") or {}
        kind = trigger["type"]
        metadata = {"launchedBy": self.actor, "workspaceMemberId": self.me}
        if kind == "MANUAL":
            availability, object_name = manual_availability(settings)
            if availability == "GLOBAL":
                if selectors:
                    raise WorkflowError("this workflow is GLOBAL and takes no records")
                data = payload if isinstance(payload, dict) else {}
                self.new_run(version, {**data, "payload": data, "metadata": metadata}, {"availability": "GLOBAL"}, test=test)
                return
            if not selectors:
                raise WorkflowError(f"this workflow runs on {object_name} records; pass --record or --records-file")
            records = [record_to_object(self.dm, self.view, self.resolve_selector(object_name, item)) for item in selectors]
            if availability == "SINGLE_RECORD":
                for record in records:
                    self.new_run(version, {"record": record, "selectedRecord": record, "metadata": metadata},
                                 {"availability": availability, "record": f"{object_name}:{record['id']}"}, test=test)
            else:
                self.new_run(version, {"records": records, "selectedRecords": records, "metadata": metadata},
                             {"availability": availability, "records": len(records)}, test=test)
            return
        if kind == "WEBHOOK":
            if payload is None:
                raise WorkflowError("a webhook workflow needs the received body as --payload-file")
            if settings.get("httpMethod") == "POST" and not isinstance(payload, dict):
                raise WorkflowError("the webhook payload must be a JSON object")
            expected = settings.get("expectedBody") if isinstance(settings.get("expectedBody"), dict) else {}
            for key, sample in expected.items():
                if not isinstance(payload, dict) or key not in payload:
                    self.warnings.append(f"the payload has no {key!r}, which expectedBody describes")
                elif isinstance(sample, (dict, list)) and not isinstance(payload[key], type(sample)) and not isinstance(payload[key], str):
                    self.warnings.append(f"payload field {key!r} has another shape than in expectedBody")
            body = payload if isinstance(payload, dict) else {"value": payload}
            # The body itself is handed to the run ({{trigger.field}}); {{trigger.body.field}} works as well.
            output = {"body": body, "payload": body, "httpMethod": settings.get("httpMethod")}
            output.update(body)
            self.new_run(version, output, {"payload_sha256": sha256_bytes(canonical_json(payload)), "httpMethod": settings.get("httpMethod")}, test=test)
            return
        if kind == "CRON":
            schedule = CronSchedule(cron_pattern(settings))
            self.new_run(version, {"scheduledAt": self.now_iso, "dueSlots": 1, "missedSlots": 0, "pattern": schedule.pattern, "test": True},
                         {"scheduledAt": self.now_iso, "explicit": True}, test=True)
            return
        object_name, action = EVENT_NAME_RE.fullmatch(settings["eventName"]).groups()
        if not selectors:
            raise WorkflowError(f"a database event workflow runs by itself with --due; for a test run pass --record of {object_name}")
        for item in selectors:
            record = record_to_object(self.dm, self.view, self.resolve_selector(object_name, item))
            output = {"recordId": record["id"], "objectName": object_name, "eventName": f"{object_name}.{action}", "object": record,
                      "properties": {"before": record if action != "created" else None, "after": record, "updatedFields": [], "diff": {}},
                      "event": {"simulated": True}}
            self.new_run(version, output, {"record": f"{object_name}:{record['id']}", "simulated": True}, test=True)

    # -- result -------------------------------------------------------------

    def log_entry(self, run: dict[str, Any], transaction: Optional[dict[str, Any]], origin_run_id: Optional[str]) -> dict[str, Any]:
        engine = self.engine(run["version"])
        segment = self.segment(run)
        steps = {}
        for step_id in engine.graph.order:
            info = run["step_infos"].get(step_id)
            if not info:
                continue
            entry: dict[str, Any] = {"type": engine.graph.steps[step_id]["type"], "status": info.get("status")}
            if "result" in info:
                entry["result"] = truncate_for_log(info["result"])
            if info.get("error"):
                entry["error"] = info["error"]
            if info.get("iterations"):
                entry["iterations"] = info["iterations"]
            if info.get("last") and info.get("status") == "NOT_STARTED":
                entry["last"] = truncate_for_log(info["last"])
            if info.get("wait"):
                wait = {key: value for key, value in info["wait"].items() if key not in ("request", "armedCursor")}
                if "request" in info["wait"]:
                    wait["request"] = info["wait"]["request"].get("type")
                entry["wait"] = wait
            steps[step_id] = entry
        waiting = self.pending_steps(run)
        entry = {
            "format": RUN_FORMAT,
            "run_id": run["run_id"], "workflow": self.workflow_id, "workflow_name": self.raw.get("name"),
            "version": run["version"], "definition_sha256": run["definition_sha256"],
            "status": run["status"], "waiting": bool(waiting) and run["status"] == "RUNNING", "test": run.get("test", False),
            "trigger": {**run["trigger"], "output": truncate_for_log(run["context"].get("trigger"))},
            "cascade_depth": run.get("cascade_depth", 0), "launched_by": run.get("launched_by"),
            "started_at": run.get("started_at"), "ended_at": run.get("ended_at"), "logged_at": self.now_iso,
            "resumed": segment["resumed"], "steps": steps, "operations": segment["operations"],
            "transaction": transaction.get("transaction") if transaction and segment["operations"] else None,
            "origin_run_id": origin_run_id if segment["operations"] else None,
            "outbox": segment["outbox"], "notes": segment["notes"],
        }
        if run.get("error"):
            entry["error"] = run["error"]
        if waiting and run["status"] == "RUNNING":
            entry["waiting_for"] = [{"step": step_id, **{key: value for key, value in (run["step_infos"][step_id].get("wait") or {}).items() if key in ("type", "resumeAt", "eventName", "expiresAt")}} for step_id in waiting]
        return redact(entry, key_names=True)[0]

    def persisted_run(self, run: dict[str, Any]) -> dict[str, Any]:
        keep = ("run_id", "workflow", "version", "definition_sha256", "trigger", "context", "step_infos", "frames", "status",
                "started_at", "ended_at", "cascade_depth", "launched_by", "test")
        stored, found = redact({key: run.get(key) for key in keep}, key_names=False)
        if found:
            self.warnings.append(f"run {run['run_id']}: {found} credential-shaped value(s) were removed from the stored run state")
        size = len(canonical_json(stored))
        if size > MAX_PENDING_RUN_BYTES:
            self.errors.append(f"run {run['run_id']} waits, but its state ({size} bytes) is larger than {MAX_PENDING_RUN_BYTES} bytes; narrow the data the run carries")
        return stored

    def questions(self) -> list[dict[str, Any]]:
        asked = []
        for run in self.runs:
            if run["status"] != "RUNNING":
                continue
            for step_id in self.pending_steps(run):
                wait = run["step_infos"][step_id].get("wait") or {}
                if wait.get("type") == "ANSWER":
                    asked.append({"run_id": run["run_id"], "step_id": step_id, **(wait.get("request") or {})})
        return asked

    def finalize(self) -> dict[str, Any]:
        transaction = None
        runs_with_ops = [run for run in self.runs if self.segment(run)["operations"]]
        origin_run_id = None
        if self.ops:
            origin_run_id = runs_with_ops[0]["run_id"] if len(runs_with_ops) == 1 else "batch-" + uuid.uuid4().hex[:16]
            versions = sorted({run["version"] for run in runs_with_ops})
            origin = {"kind": "workflow", "workflow": self.workflow_id, "run_id": origin_run_id,
                      "occasion": f"workflow {self.workflow_id} v{','.join(map(str, versions))}"}
            transaction = plan_transaction(self.target, {"actor": self.actor, "origin": origin, "operations": self.ops}, now=self.now_iso)
            for problem in transaction.get("errors", []):
                self.errors.append(f"CRM transaction: {problem.get('error')} ({problem.get('object') or ''} {problem.get('record') or problem.get('row') or ''})".strip())
            self.warnings.extend(f"CRM transaction: {item}" for item in transaction.get("warnings", []))
            for entry in transaction.get("files", []):
                if entry.get("after"):
                    kinds = credential_kinds(entry["after"])
                    if kinds:
                        self.warnings.append(f"{entry['path']} would contain a credential-shaped value ({', '.join(kinds)}); lint blocks the release until it is removed or acknowledged")
        entries = [self.log_entry(run, transaction, origin_run_id) for run in self.runs]
        shards = []
        if entries:
            relative = shard_for(self.now)
            path = self.target / relative
            shards.append({"path": relative, "before_exists": path.is_file(),
                           "before_sha256": sha256_bytes(path.read_bytes()) if path.is_file() else "", "append": entries})
        pending = self.state["pending_runs"]
        for run in self.runs:
            if run["status"] == "RUNNING" and self.pending_steps(run):
                if origin_run_id and self.segment(run)["operations"]:
                    # A run waiting for an event must not wake up on the writes it made itself.
                    for step_id in self.pending_steps(run):
                        wait = run["step_infos"][step_id].get("wait") or {}
                        if wait.get("type") == "EVENT":
                            wait["ownOriginRunId"] = origin_run_id
                pending[run["run_id"]] = self.persisted_run(run)
            else:
                pending.pop(run["run_id"], None)
        if self.runs:
            self.workflow_state["last_run_at"] = self.now_iso
            self.workflow_state["runs_started"] = int(self.workflow_state.get("runs_started", 0)) + self.stats.get("runs_started", 0)
        state_changed = canonical_json(self.state) != canonical_json(self.state_before) or (not self.state_exists and bool(self.runs))
        state_entry = {
            "path": STATE_PATH, "before_exists": self.state_exists, "before_sha256": self.state_sha,
            "after": self.state if state_changed else None,
        }
        report_runs = []
        for run in self.runs:
            report_runs.append({
                "run_id": run["run_id"], "version": run["version"], "status": run["status"],
                "waiting_for": self.pending_steps(run) if run["status"] == "RUNNING" else [],
                "trigger": run["trigger"].get("type"), "error": run.get("error"),
                "steps": {step_id: info.get("status") for step_id, info in run["step_infos"].items()},
                "outbox": self.segment(run)["outbox"], "operations": self.segment(run)["operations"],
            })
        summary = {
            "runs": len(self.runs),
            "completed": sum(1 for run in self.runs if run["status"] == "COMPLETED"),
            "failed": sum(1 for run in self.runs if run["status"] == "FAILED"),
            "waiting": sum(1 for run in self.runs if run["status"] == "RUNNING"),
            "operations": transaction.get("summary", {}) if transaction else {},
            "record_files": len(transaction.get("files", [])) if transaction else 0,
            "outbox_files": len(self.outbox),
            **self.stats,
        }
        payload = {
            "format": PLAN_FORMAT,
            "created_at": self.now_iso, "actor": self.actor, "workflow": self.workflow_id,
            "workflow_file": self.relative, "workflow_file_sha256": self.file_sha,
            "crm_transaction": transaction,
            "run_shards": shards,
            "state": state_entry,
            "outbox": {"folder": self.outbox_folder, "copy_dir": str(self.outbox_dir) if self.outbox_dir else None, "files": self.outbox},
            "runs": report_runs, "summary": summary, "questions": self.questions(), "deletions": self.deletions,
            "destructive": bool(transaction and transaction.get("destructive")),
            "errors": self.errors, "warnings": self.warnings,
        }
        return {**payload, "plan_sha256": sha256_bytes(canonical_json(payload))}


# ---------------------------------------------------------------------------
# applying a plan


def apply_plan(target: Path, token: str, plan: dict[str, Any], *, confirm_destructive: bool) -> tuple[dict[str, Any], int]:
    from snapshot_wiki import create_snapshot

    require_lock(target, token)
    if plan.get("errors"):
        return {"state": "invalid_plan", "writes": 0, "reason": "the plan contains errors; fix them and plan again", "errors": plan["errors"][:50]}, 4
    transaction = plan.get("crm_transaction")
    if transaction and transaction.get("destructive") and not confirm_destructive:
        return {"state": "confirmation_required", "writes": 0, "reason": "the runs destroy records; apply only after the user confirmed (--confirm-destructive)",
                "deletions": plan.get("deletions", [])}, 3
    workflow_path = target / plan["workflow_file"]
    if not workflow_path.is_file() or sha256_bytes(workflow_path.read_bytes()) != plan["workflow_file_sha256"]:
        return {"state": "stale_plan", "writes": 0, "reason": f"{plan['workflow_file']} changed after planning"}, 3
    state = plan["state"]
    state_path = target / state["path"]
    if state_path.is_file() != bool(state["before_exists"]) or (state_path.is_file() and sha256_bytes(state_path.read_bytes()) != state["before_sha256"]):
        return {"state": "stale_plan", "writes": 0, "reason": f"{state['path']} changed after planning"}, 3
    for shard in plan.get("run_shards", []):
        path = target / shard["path"]
        if path.is_file() != bool(shard["before_exists"]) or (path.is_file() and sha256_bytes(path.read_bytes()) != shard["before_sha256"]):
            return {"state": "stale_plan", "writes": 0, "reason": f"{shard['path']} changed after planning"}, 3
    outbox = plan.get("outbox") or {}
    files = outbox.get("files") or []
    folder = str(outbox.get("folder") or "")
    copy_dir = Path(outbox["copy_dir"]).resolve() if outbox.get("copy_dir") else None
    skipped: list[str] = []
    copies_skipped: list[str] = []
    if files:
        if not folder.startswith(OUTBOX_DIR + "/"):
            return {"state": "invalid_plan", "writes": 0, "reason": f"outbox files belong in {OUTBOX_DIR}/ in the wiki"}, 4
        if copy_dir is not None and not outside(target, copy_dir):
            return {"state": "invalid_plan", "writes": 0, "reason": "the outbox copy directory must lie outside the wiki"}, 4
        for item in files:
            if sha256_text(item["content"]) != item["sha256"]:
                return {"state": "stale_plan", "writes": 0, "reason": f"outbox content of {item['name']} was modified"}, 3
            relative = f"{folder}/{item['name']}"
            if not OUTBOX_PATH_RE.fullmatch(relative):
                return {"state": "invalid_plan", "writes": 0, "reason": f"{relative} is not a valid outbox path"}, 4
            for path, seen in ((target / relative, skipped), (copy_dir / item["name"] if copy_dir is not None else None, copies_skipped)):
                if path is None or not path.exists():
                    continue
                if path.is_file() and sha256_bytes(path.read_bytes()) == item["sha256"]:
                    seen.append(item["name"])
                    continue
                return {"state": "stale_plan", "writes": 0, "reason": f"outbox file {item['name']} already exists with other content"}, 3
    crm_result = None
    if transaction:
        try:
            verify_plan(transaction, transaction.get("plan_sha256", ""))
        except CrmError as exc:
            return {"state": "stale_plan", "writes": 0, "reason": str(exc)}, 3
        crm_result, code = apply_transaction(target, token, transaction, confirm_destructive=confirm_destructive)
        if code != 0:
            return {"state": crm_result.get("state"), "writes": 0, "reason": "the CRM transaction was not applied; nothing else was written", "crm": crm_result}, code
    written: list[str] = []
    snapshot = None
    try:
        existing = ([state["path"]] if state["before_exists"] and state.get("after") is not None else [])
        # Run shards only grow; their length before the append is reported instead of a full copy.
        shard_lengths = [{"path": shard["path"], "bytes": (target / shard["path"]).stat().st_size if (target / shard["path"]).is_file() else 0}
                         for shard in plan.get("run_shards", [])]
        if existing:
            snapshot = create_snapshot(target, token, operation="crm-workflow-run", selected_files=sorted(set(existing)))
        for shard in plan.get("run_shards", []):
            path = target / shard["path"]
            base = path.read_text(encoding="utf-8") if path.is_file() else ""
            if base and not base.endswith("\n"):
                base += "\n"
            lines = "".join(dumps(entry) + "\n" for entry in shard["append"])
            portable_io.atomic_write_bytes(path, (base + lines).encode("utf-8"))
            written.append(shard["path"])
        if state.get("after") is not None:
            portable_io.atomic_write_bytes(state_path, (json.dumps(state["after"], ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            written.append(state["path"])
        for item in files:
            if item["name"] not in skipped:
                path = target / folder / item["name"]
                path.parent.mkdir(parents=True, exist_ok=True)
                portable_io.atomic_write_bytes(path, item["content"].encode("utf-8"))
                written.append(f"{folder}/{item['name']}")
            if copy_dir is not None and item["name"] not in copies_skipped:
                copy_dir.mkdir(parents=True, exist_ok=True)
                portable_io.atomic_write_bytes(copy_dir / item["name"], item["content"].encode("utf-8"))
                written.append("copy:" + item["name"])
    except OSError as exc:
        return {"state": "partial_failure", "reason": f"writing failed after the CRM transaction: {exc}", "written": written,
                "crm": crm_result, "snapshot": snapshot, "run_shards_before": shard_lengths, "recovery_required": True}, 5
    summary = plan.get("summary") or {}
    return {
        "state": "applied", "plan_sha256": plan.get("plan_sha256"), "workflow": plan.get("workflow"),
        "runs": summary.get("runs", 0), "completed": summary.get("completed", 0), "failed": summary.get("failed", 0),
        "waiting": summary.get("waiting", 0), "crm": crm_result, "written": written,
        "outbox_folder": folder or None, "outbox_files": [item["name"] for item in files], "outbox_already_present": skipped,
        "outbox_copy": copy_dir.name if copy_dir is not None else None, "snapshot": snapshot,
        "next_step": "rebuild CRM views (crm_build.py), lint and release; the drafts are in the wiki's outbox folder, nothing was sent",
    }, 0


# ---------------------------------------------------------------------------
# definition commands


def write_workflow(target: Path, token: str, relative: str, raw: dict[str, Any], state: Optional[dict[str, Any]], operation: str) -> Optional[dict[str, Any]]:
    from snapshot_wiki import create_snapshot

    existing = [item for item in ([relative] + ([STATE_PATH] if state is not None else [])) if (target / item).is_file()]
    snapshot = create_snapshot(target, token, operation=operation, selected_files=existing) if existing else None
    portable_io.atomic_write_bytes(target / relative, (json.dumps(raw, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    if state is not None:
        portable_io.atomic_write_bytes(target / STATE_PATH, (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return snapshot


def activate(target: Path, token: str, workflow_id: str, number: int, now: datetime) -> tuple[dict[str, Any], int]:
    dm = load_datamodel(target)
    raw, relative, _sha = read_workflow(target, workflow_id)
    errors, warnings = check_workflow(target, dm, raw, relative)
    if errors:
        return {"state": "invalid", "errors": errors, "warnings": warnings}, 4
    version = find_version(raw, number)
    if version["status"] == "ACTIVE":
        return {"state": "already_active", "workflow": workflow_id, "version": number}, 0
    published = max((item.get("version", 0) for item in raw["versions"] if item.get("activated_at")), default=None)
    if version["status"] != "DRAFT" and not (version["status"] == "DEACTIVATED" and version["version"] == published):
        return {"state": "invalid", "errors": [f"version {number} is {version['status']}; only a DRAFT or the last published DEACTIVATED version can be activated (use create-draft --from-version {number})"]}, 4
    now_iso = format_instant(now)
    archived = []
    for item in raw["versions"]:
        if item.get("status") == "ACTIVE":
            item["status"] = "ARCHIVED"
            archived.append(item["version"])
    version["status"] = "ACTIVE"
    version["activated_at"] = now_iso
    version["sha256"] = version_hash(version)
    state, _exists, _state_sha = read_state(target)
    workflow_state = state["workflows"].setdefault(workflow_id, {})
    kind = version["trigger"]["type"]
    if kind == "DATABASE_EVENT":
        workflow_state["event_cursor"] = event_line_counts(target)
    if kind == "CRON":
        workflow_state["cron_checked_at"] = now_iso
    workflow_state["activated_at"] = now_iso
    workflow_state["active_version"] = number
    snapshot = write_workflow(target, token, relative, raw, state, "crm-workflow-activate")
    return {
        "state": "activated", "workflow": workflow_id, "version": number, "sha256": version["sha256"], "archived": archived,
        "trigger": kind, "activated_at": now_iso, "snapshot": snapshot, "warnings": warnings,
        "note": "events and schedule times before this moment do not trigger the workflow",
        "next_step": "lint and release (minor); due work runs with run plan --due",
    }, 0


def deactivate(target: Path, token: str, workflow_id: str) -> tuple[dict[str, Any], int]:
    raw, relative, _sha = read_workflow(target, workflow_id)
    version = active_version(raw)
    if version is None:
        return {"state": "not_active", "workflow": workflow_id}, 0
    version["status"] = "DEACTIVATED"
    state, _exists, _sha2 = read_state(target)
    state["workflows"].setdefault(workflow_id, {})["active_version"] = None
    snapshot = write_workflow(target, token, relative, raw, state, "crm-workflow-deactivate")
    return {"state": "deactivated", "workflow": workflow_id, "version": version["version"], "snapshot": snapshot,
            "note": "waiting runs continue on request; new triggers are ignored until a version is active again"}, 0


def delete_workflow(target: Path, token: str, workflow_id: str, confirmed: bool) -> tuple[dict[str, Any], int]:
    """Remove a workflow definition with all its versions; the run log stays as history."""
    from snapshot_wiki import create_snapshot

    _raw, relative, _sha = read_workflow(target, workflow_id)
    state, state_exists, _state_sha = read_state(target)
    waiting = sorted(run_id for run_id, run in state.get("pending_runs", {}).items() if isinstance(run, dict) and run.get("workflow") == workflow_id)
    if waiting:
        return {"state": "waiting_runs", "workflow": workflow_id, "runs": waiting,
                "reason": "cancel the waiting runs first with run cancel, then delete"}, 3
    if not confirmed:
        return {"state": "confirmation_required", "workflow": workflow_id,
                "reason": "deleting removes the definition and every version; confirm with --confirm-destructive (the run log stays)"}, 3
    snapshot = create_snapshot(target, token, operation="crm-workflow-delete",
                               selected_files=[relative] + ([STATE_PATH] if state_exists else []))
    (target / relative).unlink()
    if workflow_id in state.get("workflows", {}):
        del state["workflows"][workflow_id]
        portable_io.atomic_write_bytes(target / STATE_PATH, (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return {"state": "deleted", "workflow": workflow_id, "snapshot": snapshot,
            "next_step": "rebuild CRM views (crm_build.py), lint and publish a minor release"}, 0


def create_draft(target: Path, token: str, workflow_id: str, number: int, now: datetime, replace: bool) -> tuple[dict[str, Any], int]:
    raw, relative, _sha = read_workflow(target, workflow_id)
    source = find_version(raw, number)
    drafts = [item for item in raw["versions"] if item.get("status") == "DRAFT"]
    if drafts and not replace:
        return {"state": "draft_exists", "workflow": workflow_id, "draft": drafts[0]["version"],
                "reason": "edit the existing draft or pass --replace-draft"}, 3
    raw["versions"] = [item for item in raw["versions"] if item.get("status") != "DRAFT"]
    new_number = max(item.get("version", 0) for item in raw["versions"]) + 1 if raw["versions"] else 1
    draft = {"version": new_number, "status": "DRAFT", "created_at": format_instant(now),
             "trigger": deep(source["trigger"]), "steps": deep(source["steps"])}
    raw["versions"].append(draft)
    snapshot = write_workflow(target, token, relative, raw, None, "crm-workflow-draft")
    return {"state": "draft_created", "workflow": workflow_id, "version": new_number, "from_version": number, "snapshot": snapshot}, 0


def save_draft(target: Path, token: str, workflow_id: str, definition: dict[str, Any], now: datetime) -> tuple[dict[str, Any], int]:
    dm = load_datamodel(target)
    relative = workflow_relative(workflow_id)
    if not isinstance(definition, dict) or "trigger" not in definition or "steps" not in definition:
        return {"state": "invalid", "errors": ["the definition file needs trigger and steps (and name for a new workflow)"]}, 4
    if (target / relative).is_file():
        raw, relative, _sha = read_workflow(target, workflow_id)
    else:
        raw = {"format": WORKFLOW_FORMAT, "id": workflow_id, "name": definition.get("name") or "", "versions": []}
        if definition.get("description"):
            raw["description"] = definition["description"]
    for key in ("name", "description"):
        if definition.get(key):
            raw[key] = definition[key]
    drafts = [item for item in raw["versions"] if isinstance(item, dict) and item.get("status") == "DRAFT"]
    if drafts:
        draft = drafts[0]
    else:
        draft = {"version": max((item.get("version", 0) for item in raw["versions"] if isinstance(item, dict)), default=0) + 1,
                 "status": "DRAFT", "created_at": format_instant(now)}
        raw["versions"].append(draft)
    draft["trigger"] = definition["trigger"]
    draft["steps"] = definition["steps"]
    errors, warnings = validate_workflow_raw(dm, raw, relative, workflow_id)
    text = json.dumps(raw, ensure_ascii=False, indent=2)
    errors.extend(scan_definition_text(text, relative))
    if errors:
        return {"state": "invalid", "errors": errors, "warnings": warnings, "writes": 0}, 4
    (target / WORKFLOWS_DIR).mkdir(parents=True, exist_ok=True)
    snapshot = write_workflow(target, token, relative, raw, None, "crm-workflow-draft")
    return {"state": "draft_saved", "workflow": workflow_id, "version": draft["version"], "path": relative,
            "snapshot": snapshot, "warnings": warnings, "next_step": f"activate --workflow {workflow_id} --version {draft['version']}"}, 0


def cancel_run(target: Path, token: str, workflow_id: str, run_id: str, actor: str, now: datetime) -> tuple[dict[str, Any], int]:
    from snapshot_wiki import create_snapshot

    state, exists, _sha = read_state(target)
    stored = state["pending_runs"].get(run_id)
    if not exists or not isinstance(stored, dict) or stored.get("workflow") != workflow_id:
        return {"state": "error", "error": f"run {run_id} of workflow {workflow_id} is not waiting"}, 2
    del state["pending_runs"][run_id]
    now_iso = format_instant(now)
    entry = {
        "format": RUN_FORMAT, "run_id": run_id, "workflow": workflow_id, "version": stored.get("version"),
        "definition_sha256": stored.get("definition_sha256"), "status": "STOPPED", "waiting": False, "test": stored.get("test", False),
        "trigger": {key: value for key, value in (stored.get("trigger") or {}).items()}, "cascade_depth": stored.get("cascade_depth", 0),
        "launched_by": stored.get("launched_by"), "stopped_by": actor, "started_at": stored.get("started_at"), "ended_at": now_iso,
        "logged_at": now_iso, "resumed": False, "steps": {}, "operations": {}, "transaction": None, "origin_run_id": None,
        "outbox": [], "notes": ["cancelled while waiting"],
    }
    relative = shard_for(now)
    path = target / relative
    existing = [STATE_PATH] + ([relative] if path.is_file() else [])
    snapshot = create_snapshot(target, token, operation="crm-workflow-cancel", selected_files=existing)
    base = path.read_text(encoding="utf-8") if path.is_file() else ""
    if base and not base.endswith("\n"):
        base += "\n"
    portable_io.atomic_write_bytes(path, (base + dumps(redact(entry, key_names=True)[0]) + "\n").encode("utf-8"))
    portable_io.atomic_write_bytes(target / STATE_PATH, (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    return {"state": "cancelled", "run_id": run_id, "status": "STOPPED", "snapshot": snapshot}, 0


# ---------------------------------------------------------------------------
# validation for lint and the list overview


def _validate_all(target: Path) -> tuple[list[str], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    ids: list[str] = []
    root = target / WORKFLOWS_DIR
    files = sorted(root.iterdir()) if root.is_dir() else []
    state_present = (target / STATE_PATH).is_file()
    runs_present = (target / RUNS_DIR).is_dir()
    if not files and not state_present and not runs_present:
        return errors, warnings, ids
    dm: Optional[DataModel]
    try:
        dm = load_datamodel(target)
    except CrmError as exc:
        return [f"{WORKFLOWS_DIR}: workflows need a valid CRM data model: {exc}"], warnings, ids
    raws: dict[str, dict[str, Any]] = {}
    for path in files:
        relative = path.relative_to(target).as_posix()
        if path.name in (".DS_Store", "Thumbs.db", "desktop.ini"):
            continue
        if path.is_dir() or path.suffix != ".json":
            errors.append(f"{relative}: only <id>.json workflow files belong in {WORKFLOWS_DIR}/")
            continue
        try:
            text = path.read_text(encoding="utf-8")
            raw = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"{relative}: invalid JSON: {exc}")
            continue
        file_errors, file_warnings = validate_workflow_raw(dm, raw, relative, path.stem)
        errors.extend(file_errors)
        warnings.extend(file_warnings)
        errors.extend(scan_definition_text(text, relative))
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            raws[raw["id"]] = raw
            ids.append(raw["id"])
    if state_present:
        try:
            state, _exists, _sha = read_state(target)
        except WorkflowError as exc:
            errors.append(str(exc))
            state = None
        if state is not None:
            if not isinstance(state.get("workflows"), dict) or not isinstance(state.get("pending_runs"), dict):
                errors.append(f"{STATE_PATH}: workflows and pending_runs must be objects")
            else:
                for workflow_id in state["workflows"]:
                    if workflow_id not in raws:
                        warnings.append(f"{STATE_PATH}: state for unknown workflow {workflow_id}")
                for run_id, stored in state["pending_runs"].items():
                    where = f"{STATE_PATH}: waiting run {run_id}"
                    if not isinstance(stored, dict):
                        errors.append(f"{where}: must be an object")
                        continue
                    raw = raws.get(str(stored.get("workflow")))
                    if raw is None:
                        errors.append(f"{where}: workflow {stored.get('workflow')} does not exist; cancel the run (run cancel)")
                        continue
                    try:
                        version = find_version(raw, stored.get("version"))
                    except WorkflowError:
                        errors.append(f"{where}: version {stored.get('version')} does not exist; cancel the run")
                        continue
                    if version_hash(version) != stored.get("definition_sha256"):
                        errors.append(f"{where}: version {stored.get('version')} changed since the run started; cancel the run")
                    if stored.get("status") != "RUNNING":
                        errors.append(f"{where}: a waiting run has status RUNNING")
    if runs_present:
        for path in sorted((target / RUNS_DIR).iterdir()):
            relative = path.relative_to(target).as_posix()
            if path.name in (".DS_Store", "Thumbs.db", "desktop.ini"):
                continue
            if not path.is_file() or not SHARD_RE.fullmatch(path.name):
                errors.append(f"{relative}: only monthly YYYY-MM.jsonl shards belong in {RUNS_DIR}/")
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"{relative}:{number}: invalid JSON: {exc.msg}")
                    continue
                if not isinstance(entry, dict) or entry.get("format") != RUN_FORMAT:
                    errors.append(f"{relative}:{number}: run format must be {RUN_FORMAT}")
                elif entry.get("status") not in RUN_STATUSES or not isinstance(entry.get("run_id"), str):
                    errors.append(f"{relative}:{number}: run_id and a status of {', '.join(RUN_STATUSES)} are required")
    return errors, warnings, ids


ERASED = "[erased]"


def erasure_rewrites(target: Path, erase_ids: set[str], needles: Optional[set[str]] = None) -> list[dict[str, Any]]:
    """Run log shards and workflow state without the content of erased records (GDPR erasure).

    A run entry that mentions an erased record id (or one of the needles, such as its
    e-mail address) keeps its identity, version, statuses and counts; its trigger data,
    step results, errors and notes become "[erased]". A waiting run that mentions it is
    dropped from the state, because it cannot continue without the data. Returns
    [{path, before_sha256, after}] for the caller's erase transaction; nothing is written here.
    """
    markers = {item for item in set(erase_ids) | set(needles or set()) if item and len(item) >= 4}
    rewrites: list[dict[str, Any]] = []
    if not markers:
        return rewrites

    def mentions(text: str) -> bool:
        return any(marker in text for marker in markers)

    root = target / RUNS_DIR
    for shard in sorted(root.glob("*.jsonl")) if root.is_dir() else []:
        text = shard.read_text(encoding="utf-8")
        lines = []
        changed = False
        for line in text.splitlines():
            if line.strip() and mentions(line):
                entry = json.loads(line)
                if isinstance(entry.get("trigger"), dict):
                    entry["trigger"] = {key: (ERASED if key in ("output", "record", "records") else value) for key, value in entry["trigger"].items()}
                for info in (entry.get("steps") or {}).values():
                    for key in ("result", "last", "error", "wait"):
                        if key in info:
                            info[key] = ERASED
                entry["notes"] = []
                entry.pop("error", None)
                entry["erased"] = True
                line = dumps(entry)
                changed = True
            lines.append(line)
        if changed:
            rewrites.append({"path": shard.relative_to(target).as_posix(), "before_sha256": sha256_bytes(shard.read_bytes()),
                             "after": "\n".join(lines) + "\n"})
    path = target / STATE_PATH
    if path.is_file():
        state = json.loads(path.read_text(encoding="utf-8"))
        dropped = [run_id for run_id, stored in (state.get("pending_runs") or {}).items() if mentions(dumps(stored))]
        if dropped:
            for run_id in dropped:
                del state["pending_runs"][run_id]
            state.setdefault("erased_runs", []).extend(sorted(dropped))
            rewrites.append({"path": STATE_PATH, "before_sha256": sha256_bytes(path.read_bytes()),
                             "after": json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"})
    return rewrites


def validate_workflows(target: Path) -> list[str]:
    """Errors of all workflow definitions, the workflow state and the run log; used by lint. Never raises."""
    try:
        return _validate_all(Path(target))[0]
    except Exception as exc:  # noqa: BLE001 - lint must report, not crash, on any broken workflow file
        return [f"{WORKFLOWS_DIR}: workflow validation failed: {type(exc).__name__}: {exc}"]


def overview(target: Path, now: datetime) -> dict[str, Any]:
    errors, warnings, ids = _validate_all(target)
    state, _exists, _sha = read_state(target)
    items = []
    counts = event_line_counts(target)
    for workflow_id in ids:
        raw, relative, _file_sha = read_workflow(target, workflow_id)
        version = active_version(raw)
        workflow_state = state["workflows"].get(workflow_id, {})
        pending = [{"run_id": run_id, "version": stored.get("version"),
                    "waiting_for": [{"step": step_id, **{key: value for key, value in ((stored.get("step_infos") or {}).get(step_id, {}).get("wait") or {}).items() if key in ("type", "resumeAt", "expiresAt", "eventName")}}
                                    for frame in stored.get("frames", []) for step_id in frame.get("pending", [])]}
                   for run_id, stored in state["pending_runs"].items() if isinstance(stored, dict) and stored.get("workflow") == workflow_id]
        due: dict[str, Any] = {}
        for item in pending:
            for wait in item["waiting_for"]:
                moment = parse_instant(wait.get("resumeAt"))
                if moment is not None and moment <= now:
                    due["delays"] = due.get("delays", 0) + 1
        trigger = version["trigger"] if version else None
        if version and trigger.get("type") == "CRON":
            try:
                schedule = CronSchedule(cron_pattern(trigger.get("settings") or {}))
                checked = parse_instant(workflow_state.get("cron_checked_at")) or parse_instant(version.get("activated_at")) or now
                due["cron_slots"] = schedule.slots(checked, now)[0]
            except ValueError:
                pass
        if version and trigger.get("type") == "DATABASE_EVENT":
            cursor = workflow_state.get("event_cursor") or {}
            due["new_events"] = sum(max(0, lines - int(cursor.get(shard, 0))) for shard, lines in counts.items())
        items.append({
            "id": workflow_id, "name": raw.get("name"), "description": raw.get("description"), "path": relative,
            "active_version": version["version"] if version else None,
            "trigger": {"type": trigger.get("type"), **({"eventName": trigger["settings"].get("eventName")} if trigger.get("type") == "DATABASE_EVENT" else {}),
                        **({"pattern": cron_pattern(trigger["settings"])} if trigger.get("type") == "CRON" else {})} if trigger else None,
            "versions": [{"version": item.get("version"), "status": item.get("status"), "activated_at": item.get("activated_at")}
                         for item in raw.get("versions", []) if isinstance(item, dict)],
            "waiting_runs": pending, "due": due,
            "last_run_at": workflow_state.get("last_run_at"),
        })
    return {"now": format_instant(now), "workflows": items, "valid": not errors, "errors": errors, "warnings": warnings}


# ---------------------------------------------------------------------------
# command line


def load_json_argument(path: str) -> Any:
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def answers_from(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and isinstance(value.get("answers"), list):
        return value["answers"]
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        entries = []
        for run_id, steps in value.items():
            if run_id == "format":
                continue
            if isinstance(steps, dict):
                for step_id, answer in steps.items():
                    entries.append({"run_id": run_id, "step_id": step_id, "answer": answer})
        return entries
    raise WorkflowError("the answers file must hold {\"answers\": [{\"run_id\", \"step_id\", \"answer\"}]}")


def selectors_from(records: Optional[list[str]], records_file: Optional[str]) -> list[Any]:
    selectors: list[Any] = list(records or [])
    if records_file:
        value = load_json_argument(records_file)
        if isinstance(value, dict):
            value = value.get("records")
        if not isinstance(value, list):
            raise WorkflowError("the records file must hold a JSON list of record ids, links or {\"match\": {...}}")
        selectors.extend(value)
    return selectors


def command_run_plan(target: Path, args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    output = Path(args.output).expanduser().resolve()
    if not outside(target, output):
        raise WorkflowError("the plan must be written outside the wiki")
    outbox = Path(args.outbox).expanduser().resolve() if args.outbox else None
    if outbox is not None and not outside(target, outbox):
        raise WorkflowError("--outbox names an additional copy outside the wiki; the drafts themselves are kept in records/_outbox/")
    if not valid_actor(args.actor):
        raise WorkflowError("--actor must be human:<id>, agent/<name> or process:<id>")
    now = now_moment(args.now)
    try:
        builder = PlanBuilder(target, args.workflow, args.actor, now, outbox)
    except InvalidDefinition as exc:
        return {"state": "invalid", "errors": exc.errors, "reason": "the workflow file does not validate; nothing runs from it"}, 4
    if args.now and abs((now - datetime.now(timezone.utc)).total_seconds()) > 86400:
        builder.warnings.append("--now differs from the system clock by more than a day; due times and timestamps follow --now")
    if args.answers_file:
        builder.apply_answers(answers_from(load_json_argument(args.answers_file)))
    if args.due:
        builder.plan_due()
    selectors = selectors_from(args.record, args.records_file)
    payload = load_json_argument(args.payload_file) if args.payload_file else None
    explicit = bool(selectors) or payload is not None or (not args.due and not args.answers_file)
    if explicit:
        version = builder.run_version(args.version)
        builder.plan_explicit(version, selectors, payload, test=args.version is not None and version.get("status") != "ACTIVE")
    plan = builder.finalize()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    nothing = not plan["runs"] and plan["state"]["after"] is None
    report = {
        "state": "invalid" if plan["errors"] else ("nothing_due" if nothing else "planned"),
        "plan_sha256": plan["plan_sha256"], "plan_file": output.name, "workflow": builder.workflow_id,
        "runs": plan["runs"], "summary": plan["summary"], "questions": plan["questions"],
        "deletions": plan["deletions"], "destructive": plan["destructive"],
        "outbox": [item["name"] for item in plan["outbox"]["files"]],
        "outbox_folder": plan["outbox"]["folder"] if plan["outbox"]["files"] else None,
        "record_changes": [
            {"path": entry["path"], "action": "remove" if entry["after"] is None else ("update" if entry["before_exists"] else "create")}
            for entry in (plan["crm_transaction"] or {}).get("files", [])[:50]
        ],
        "errors": plan["errors"][:100], "warnings": plan["warnings"][:50],
    }
    if plan["questions"]:
        report["answers_template"] = {"format": ANSWERS_FORMAT, "answers": [
            {"run_id": item["run_id"], "step_id": item["step_id"], "answer": {}} for item in plan["questions"]]}
    if not plan["errors"] and not nothing:
        report["next_step"] = "show the plan to the user, then run apply with --expect-plan-sha256 " + plan["plan_sha256"]
    return report, 1 if plan["errors"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> argparse.ArgumentParser:
        command.add_argument("--target", required=True)
        command.add_argument("--lock-token", required=True)
        return command

    common(sub.add_parser("validate"))
    common(sub.add_parser("list")).add_argument("--now")
    save_parser = common(sub.add_parser("save-draft"))
    save_parser.add_argument("--workflow", required=True)
    save_parser.add_argument("--definition-file", required=True)
    save_parser.add_argument("--now")
    draft_parser = common(sub.add_parser("create-draft"))
    draft_parser.add_argument("--workflow", required=True)
    draft_parser.add_argument("--from-version", required=True, type=int)
    draft_parser.add_argument("--replace-draft", action="store_true")
    draft_parser.add_argument("--now")
    activate_parser = common(sub.add_parser("activate"))
    activate_parser.add_argument("--workflow", required=True)
    activate_parser.add_argument("--version", required=True, type=int)
    activate_parser.add_argument("--now")
    deactivate_parser = common(sub.add_parser("deactivate"))
    deactivate_parser.add_argument("--workflow", required=True)
    delete_parser = common(sub.add_parser("delete"))
    delete_parser.add_argument("--workflow", required=True)
    delete_parser.add_argument("--confirm-destructive", action="store_true")
    run_parser = sub.add_parser("run")
    run_sub = run_parser.add_subparsers(dest="run_command", required=True)
    plan_parser = common(run_sub.add_parser("plan"))
    plan_parser.add_argument("--workflow", required=True)
    plan_parser.add_argument("--record", action="append")
    plan_parser.add_argument("--records-file")
    plan_parser.add_argument("--payload-file")
    plan_parser.add_argument("--answers-file")
    plan_parser.add_argument("--due", action="store_true")
    plan_parser.add_argument("--now")
    plan_parser.add_argument("--version", type=int, help="run this version as a test run instead of the active one")
    plan_parser.add_argument("--outbox", help="Optional extra copy of the drafts in a folder outside the wiki")
    plan_parser.add_argument("--actor", required=True)
    plan_parser.add_argument("--output", required=True)
    apply_parser = common(run_sub.add_parser("apply"))
    apply_parser.add_argument("--plan-file", required=True)
    apply_parser.add_argument("--expect-plan-sha256", required=True)
    apply_parser.add_argument("--confirm-destructive", action="store_true")
    cancel_parser = common(run_sub.add_parser("cancel"))
    cancel_parser.add_argument("--workflow", required=True)
    cancel_parser.add_argument("--run-id", required=True)
    cancel_parser.add_argument("--actor", required=True)
    cancel_parser.add_argument("--now")
    args = parser.parse_args()
    target = Path(args.target).expanduser().resolve()
    require_lock(target, args.lock_token)
    try:
        if args.command == "validate":
            errors, warnings, ids = _validate_all(target)
            report, code = {"valid": not errors, "workflows": ids, "errors": errors, "warnings": warnings}, (0 if not errors else 4)
        elif args.command == "list":
            report, code = overview(target, now_moment(args.now)), 0
        elif args.command == "save-draft":
            report, code = save_draft(target, args.lock_token, args.workflow, load_json_argument(args.definition_file), now_moment(args.now))
        elif args.command == "create-draft":
            report, code = create_draft(target, args.lock_token, args.workflow, args.from_version, now_moment(args.now), args.replace_draft)
        elif args.command == "activate":
            report, code = activate(target, args.lock_token, args.workflow, args.version, now_moment(args.now))
        elif args.command == "deactivate":
            report, code = deactivate(target, args.lock_token, args.workflow)
        elif args.command == "delete":
            report, code = delete_workflow(target, args.lock_token, args.workflow, args.confirm_destructive)
        elif args.run_command == "plan":
            report, code = command_run_plan(target, args)
        elif args.run_command == "apply":
            plan_text = Path(args.plan_file).expanduser().read_text(encoding="utf-8")
            plan = json.loads(plan_text)
            if not isinstance(plan, dict) or plan.get("format") != PLAN_FORMAT:
                raise WorkflowError("unsupported workflow plan")
            payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
            if plan.get("plan_sha256") != args.expect_plan_sha256 or sha256_bytes(canonical_json(payload)) != args.expect_plan_sha256:
                report, code = {"state": "stale_plan", "writes": 0, "reason": "the workflow plan is stale or was modified"}, 3
            else:
                report, code = apply_plan(target, args.lock_token, plan, confirm_destructive=args.confirm_destructive)
        else:
            report, code = cancel_run(target, args.lock_token, args.workflow, args.run_id, args.actor, now_moment(args.now))
    except InvalidDefinition as exc:
        report, code = {"state": "invalid", "errors": exc.errors}, 4
    except (OSError, json.JSONDecodeError, CrmError, ValueError) as exc:
        report, code = {"state": "error", "error": str(exc)}, 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
