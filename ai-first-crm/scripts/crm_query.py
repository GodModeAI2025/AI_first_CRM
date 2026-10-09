#!/usr/bin/env python3
"""Read-only CRM queries for maintainers; nothing in the wiki is ever written.

Exactly one of two bindings is required:
  --lock-token TOKEN  inside a maintenance session; reads the current working state.
  --release           reads the published release. The manifest is verified like
                      verify_release.py before and after reading, and the query is
                      refused while a maintenance lock exists.

Commands (each also takes --target, --now ISO and --me WORKSPACE-MEMBER-UUID):
  find --object O [--filter JSON] [--sort JSON] [--limit N] [--offset N] [--fields a,b]
       [--include-deleted]
  aggregate --object O --function F [--field X] [--group-by X] [--granularity G] [--filter JSON]
  get --object O --id ID
  search --text T [--objects a,b] [--limit N]
  timeline [--object O] [--id ID] [--since ISO] [--limit N]
  time-in-stage --object opportunity [--field stage] [--id ID] [--include-deleted] [--limit N]
  expected-amount --object opportunity --probabilities JSON [--field amount] [--stage-field stage]
       [--group-by stage|none|<field>] [--granularity G] [--filter JSON] [--include-deleted]
  runs [--workflow W] [--status S] [--since ISO] [--limit N]
  runs --run-id R [--workflow W]

Filters, sorting, grouping and aggregates come from crm_filters.py. Every answer names
generated_at and the time zone of schema/crm/settings.json; numbers and dates carry a
display form following those settings next to the raw value. The query engine is shared
with the frozen knowledge skill (assets/knowledge-skill/query_records.py). COUNT and SUM
over no records are 0, like a group without records; a sum of amounts without any amount
is shown in the field's default currency, else in the default currency of the settings.

time-in-stage reads the event log: a stage is entered when an event records the change.
Schema migrations (origin.kind "migration", e.g. a renamed option) relabel a stage and
never count as a move. expected-amount weights each amount with the probability of its
stage, given as a share from 0 to 1 like the probabilities of a kanban view (60 % is 0.6;
"60%" with a percent sign is read as 0.6), exactly in decimals, and never adds up
different currencies. Grouped by a select field (the stage by default), every option
appears in option order, also without records; records of a stage without probability
count there and in the totals but add nothing to the expected amount.

runs reads the workflow run log meta/crm-runs/YYYY-MM.jsonl that crm_workflows.py writes
(format lmwiki-crm-run/1). It lists one entry per run, newest start first: run_id,
workflow, version, status, trigger type, start and end, error, the status of every step
and the outbox file names. A run that waited and went on later has several log entries;
the list shows its latest state and the outbox files of all of them. --status compares
the status as it is logged: COMPLETED, FAILED, RUNNING for a run that waits, STOPPED for
a cancelled run (CANCELLED is read as STOPPED). --since keeps runs started at or
after that moment; a plain date is the start of that day in the CRM time zone. With
--run-id the answer is that one run completely: every log entry as written, step outputs
included. The log is redacted when it is written; runs shows it as it is.

Exit codes: 0 answered, 2 error (no CRM layer, unknown record or run, unreadable data),
3 the release is busy, changed or not verifiable (--release), 4 invalid request.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import importlib.util
import json
import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import ModuleType
from typing import Any, Optional

import crm_filters
from crm_contract import CrmError, format_instant, option_values, parse_instant
from crm_filters import FilterError

ENGINE_PATH = Path(__file__).resolve().parent.parent / "assets" / "knowledge-skill" / "query_records.py"
DAY_SECONDS = Decimal(86400)
BASIS_NOTE = (
    "basis event: entered when the change was logged; import: entered at or before that import; "
    "crm_created_at: no logged entry, measured from record creation; unknown: no start time available"
)
# The run log of crm_workflows.py; lint allows only monthly shards in it.
RUNS_DIR = "meta/crm-runs"
RUN_FORMAT = "lmwiki-crm-run/1"
RUN_SHARD_RE = re.compile(r"^\d{4}-\d{2}\.jsonl$")
# Run statuses as crm_workflows.py logs them; a cancelled run is logged as STOPPED.
RUN_STATUSES = ("NOT_STARTED", "RUNNING", "COMPLETED", "FAILED", "ENQUEUED", "STOPPING", "STOPPED")
STATUS_ALIASES = {"CANCELLED": "STOPPED", "CANCELED": "STOPPED"}
RUNS_LIMIT = (50, 1000)


def load_engine() -> ModuleType:
    """Load the shared read-only query engine; sys.dont_write_bytecode above keeps it free of bytecode files."""
    spec = importlib.util.spec_from_file_location("crm_query_engine", ENGINE_PATH)
    if spec is None or spec.loader is None or not ENGINE_PATH.is_file():
        raise SystemExit("the CRM query engine assets/knowledge-skill/query_records.py is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def session_class(engine: ModuleType) -> type:
    """The engine's Session, with a sum of amounts over no amount at all shown in the currency it would carry."""

    class QuerySession(engine.Session):
        def zero_amount(self, object_name: str, function: str, key: Optional[str], records: list[Any]) -> Optional[dict[str, Any]]:
            """SUM of a CURRENCY field when no record carries an amount: 0 in the field's default currency."""
            if function != "SUM" or not key:
                return None
            ftype, base, sub = self.field_type(object_name, key)
            if ftype != "CURRENCY" or sub not in ("", "amount"):
                return None
            if any(isinstance(record.data.get(f"{base}.amountMicros"), int) for record in records):
                return None
            code = crm_filters.default_currency(self.datamodel, object_name, key, self.settings.get("default_currency"))
            return {"value": "0", "display": self.amount_text(Decimal(0), code or None), "currency": code or None}

        def aggregate_value(self, object_name: str, function: str, key: Optional[str], records: list[Any]) -> dict[str, Any]:
            return self.zero_amount(object_name, function, key, records) or super().aggregate_value(object_name, function, key, records)

        def empty_value(self, object_name: str, function: str, key: Optional[str], records: list[Any]) -> dict[str, Any]:
            return self.zero_amount(object_name, function, key, records) or super().empty_value(object_name, function, key, records)

    return QuerySession


def days_text(seconds: Optional[float]) -> Optional[str]:
    if seconds is None:
        return None
    return format((Decimal(int(seconds)) / DAY_SECONDS).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


# ---------------------------------------------------------------------------
# time in stage


def stage_intervals(record: Any, field: str, events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Intervals [stage, start, end) of one record, oldest first, from its logged changes."""
    intervals: list[dict[str, Any]] = []
    notes: list[str] = []
    current: Optional[dict[str, Any]] = None
    for event in events:
        before, after = event["changes"][field]
        at = parse_instant(event.get("at"))
        if at is None:
            continue
        origin = event.get("origin") if isinstance(event.get("origin"), dict) else {}
        if origin.get("kind") == "migration" or "schema_change" in event:
            # A schema migration renames or maps the stored value; the record did not move.
            for interval in intervals:
                if interval["stage"] == before:
                    interval["stage"] = after
            if after in (None, "") and current is not None:
                current["end"] = at
                current = None
            continue
        if not intervals and before not in (None, ""):
            created = parse_instant(record.data.get("crm_created_at"))
            start = created if created is not None and created <= at else None
            intervals.append({"stage": before, "start": start, "basis": "crm_created_at" if start else "unknown", "end": at})
        if current is not None:
            current["end"] = at
        current = None
        if after not in (None, ""):
            current = {"stage": after, "start": at, "basis": "import" if origin.get("kind") == "import" else "event", "end": None}
            intervals.append(current)
    value = record.data.get(field)
    if not intervals and value not in (None, ""):
        created = parse_instant(record.data.get("crm_created_at"))
        current = {"stage": value, "start": created, "basis": "crm_created_at" if created else "unknown", "end": None}
        intervals.append(current)
    elif current is not None and current["stage"] != value:
        notes.append(f"the event log ends in {current['stage']} but the record holds {value!r}")
    if current is not None and record.deleted:
        deleted_at = parse_instant(record.data.get("crm_deleted_at"))
        if deleted_at is not None:
            current["end"] = deleted_at
            current["ended_by"] = "deleted"
    return intervals, notes


def time_in_stage(
    engine: ModuleType,
    session: Any,
    object_name: str,
    *,
    field: str = "stage",
    record_id: Optional[str] = None,
    include_deleted: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    object_name = session.object_name(object_name)
    ftype, base, sub = session.field_type(object_name, field)
    if ftype != "SELECT" or sub or field in crm_filters.SYSTEM_FIELDS:
        raise engine.QueryError(f"time in stage needs a SELECT field; {object_name}.{field} is {ftype}")
    definition = session.datamodel.field(object_name, field)
    options = option_values(definition)
    labels = {str(option.get("value")): str(option.get("label") or option.get("value")) for option in definition.get("options", [])}
    # History logged under a renamed or merged value belongs to the option that carries it in previousValues.
    aliases = {
        str(previous): str(option.get("value"))
        for option in definition.get("options", [])
        for previous in (option.get("previousValues") if isinstance(option.get("previousValues"), list) else [])
        if previous not in options
    }
    records = session.store.records(object_name)
    if record_id:
        wanted_id = record_id.strip().lower()
        if wanted_id not in records:
            raise engine.NotFound(f"{object_name} record {wanted_id} does not exist")
        selected = [records[wanted_id]]
    else:
        selected = [record for record in records.values() if include_deleted or not record.deleted]
    history: dict[str, list[dict[str, Any]]] = {record.id: [] for record in selected}
    for event in session.events():
        if event.get("object") != object_name or event.get("record_id") not in history:
            continue
        change = (event.get("changes") or {}).get(field) if isinstance(event.get("changes"), dict) else None
        if isinstance(change, list) and len(change) == 2:
            history[event["record_id"]].append(event)
    now = session.context.now
    stats: dict[str, dict[str, Any]] = {}
    rows = []
    for record in selected:
        intervals, notes = stage_intervals(record, field, history[record.id])
        for interval in intervals:
            interval["stage"] = aliases.get(interval["stage"], interval["stage"])
        shown = []
        for interval in intervals:
            start, end = interval["start"], interval["end"]
            seconds = max(0, int(((end or now) - start).total_seconds())) if start is not None else None
            entry = {
                "stage": interval["stage"],
                "label": labels.get(str(interval["stage"]), str(interval["stage"])),
                "entered_at": format_instant(start) if start else None,
                "entered_display": session.instant_text(format_instant(start)) if start else "",
                "left_at": format_instant(end) if end else None,
                "left_display": session.instant_text(format_instant(end)) if end else "",
                "seconds": seconds,
                "days": days_text(seconds),
                "days_rounded_up": math.ceil(seconds / 86400) if seconds is not None else None,
                "basis": interval["basis"],
                "current": end is None,
            }
            if interval.get("ended_by"):
                entry["ended_by"] = interval["ended_by"]
            shown.append(entry)
            bucket = stats.setdefault(str(interval["stage"]), {"completed": [], "current": [], "unknown_start": 0})
            if seconds is None:
                bucket["unknown_start"] += 1
            elif end is None:
                bucket["current"].append(seconds)
            else:
                bucket["completed"].append(seconds)
        current = shown[-1] if shown and shown[-1]["current"] else None
        row = {
            **session.record_ref(record),
            "current_stage": current["stage"] if current else record.data.get(field),
            "current_label": current["label"] if current else labels.get(str(record.data.get(field)), record.data.get(field)),
            "current_since": current["entered_at"] if current else None,
            "current_since_display": current["entered_display"] if current else "",
            "current_days": current["days"] if current else None,
            "current_seconds": current["seconds"] if current else None,
            "intervals": shown,
        }
        if notes:
            row["notes"] = notes
        rows.append(row)
    rows.sort(key=lambda item: (item["current_seconds"] is None, -(item["current_seconds"] or 0), item["id"]))
    order = options + sorted(stage for stage in stats if stage not in options)
    summary = []
    for stage in order:
        bucket = stats.get(stage)
        if bucket is None:
            continue
        completed, current = bucket["completed"], bucket["current"]
        summary.append({
            "stage": stage,
            "label": labels.get(stage, stage),
            "completed_intervals": len(completed),
            "average_days_completed": days_text(sum(completed) / len(completed)) if completed else None,
            "records_in_stage": len(current),
            "average_days_in_stage_now": days_text(sum(current) / len(current)) if current else None,
            "intervals_without_start": bucket["unknown_start"],
        })
    return session.envelope(
        "time-in-stage",
        object=object_name,
        field=field,
        include_deleted=include_deleted,
        records=len(selected),
        summary=summary,
        returned=min(limit, len(rows)),
        rows=rows[:limit],
        basis=BASIS_NOTE,
    )


# ---------------------------------------------------------------------------
# expected amount


def parse_probabilities(engine: ModuleType, session: Any, object_name: str, stage_field: str, text: str) -> dict[str, Decimal]:
    spec = engine.load_json_argument(text, "--probabilities")
    if not isinstance(spec, dict) or not spec:
        raise engine.QueryError('--probabilities must be a JSON object such as {"NEW": 0.1, "PROPOSAL": 0.6}')
    definition = session.datamodel.field(object_name, stage_field)
    result: dict[str, Decimal] = {}
    for key, raw in spec.items():
        value = None
        for option in definition.get("options", []):
            if key == option.get("value") or str(key).casefold() == str(option.get("label") or "").casefold():
                value = str(option.get("value"))
                break
        if value is None:
            raise engine.QueryError(f"{key!r} is not an option of {object_name}.{stage_field} ({', '.join(option_values(definition))})")
        if isinstance(raw, bool):
            raise engine.QueryError(f"probability of {key!r} must be a share from 0 to 1")
        text = str(raw).strip()
        try:
            share = Decimal(text.rstrip("%").strip()) / (Decimal(100) if text.endswith("%") else Decimal(1))
        except InvalidOperation as exc:
            raise engine.QueryError(f"probability of {key!r} must be a share from 0 to 1") from exc
        if not share.is_finite() or share < 0 or share > 1:
            raise engine.QueryError(
                f"probability of {key!r} must be a share from 0 to 1 (60 % is 0.6 or \"60%\"); {raw!r} would be ambiguous"
            )
        result[value] = share
    return result


def expected_amount(
    engine: ModuleType,
    session: Any,
    object_name: str,
    probabilities_text: str,
    *,
    field: str = "amount",
    stage_field: str = "stage",
    group_by: Optional[str] = None,
    granularity: Optional[str] = None,
    filter_text: Optional[str] = None,
    include_deleted: bool = False,
) -> dict[str, Any]:
    object_name = session.object_name(object_name)
    stage_type, stage_base, stage_sub = session.field_type(object_name, stage_field)
    if stage_type != "SELECT" or stage_sub or stage_field in crm_filters.SYSTEM_FIELDS:
        raise engine.QueryError(f"--stage-field must be a SELECT field; {object_name}.{stage_field} is {stage_type}")
    amount_type, amount_base, amount_sub = session.field_type(object_name, field)
    if not ((amount_type == "CURRENCY" and amount_sub in ("", "amount")) or (amount_type in ("NUMBER", "NUMERIC") and not amount_sub)):
        raise engine.QueryError(f"--field must be a CURRENCY, NUMBER or NUMERIC field; {object_name}.{field} is {amount_type}")
    probabilities = parse_probabilities(engine, session, object_name, stage_field, probabilities_text)
    group_field = None if str(group_by or "").lower() == "none" else (group_by or stage_field)
    grain = None
    if group_field:
        group_type, _base, group_sub = session.field_type(object_name, group_field)
        if session.is_inverse(object_name, group_field):
            raise engine.QueryError(f"{object_name}.{group_field} is a computed inverse relation and cannot group records")
        if group_type in ("DATE", "DATE_TIME") and not group_sub:
            grain = str(granularity or "DAY").upper()
            if grain not in crm_filters.GRANULARITIES:
                raise engine.QueryError(f"unknown granularity {granularity!r}")
        elif granularity:
            raise engine.QueryError("--granularity applies only when grouping by a date field")
    group = session.parse_filter(object_name, filter_text)
    try:
        records = crm_filters.select_records(
            session.datamodel, session.records(object_name), filter_spec=group,
            context=session.context, include_deleted=include_deleted,
        )
    except FilterError as exc:
        raise engine.QueryError(str(exc)) from exc
    labels = {str(option.get("value")): str(option.get("label") or option.get("value")) for option in session.datamodel.field(object_name, stage_field).get("options", [])}
    currencies: dict[str, dict[str, Any]] = {}
    without_amount = 0
    missing: dict[str, int] = {}
    zero = Decimal(0)

    def new_entry() -> dict[str, Any]:
        return {"records": 0, "weighted": 0, "amount": zero, "expected": zero}

    def new_bucket() -> dict[str, Any]:
        return {"groups": {}, **new_entry(), "unweighted": {"records": 0, "amount": zero}}

    for record in records:
        if amount_type == "CURRENCY":
            micros = record.data.get(f"{amount_base}.amountMicros")
            amount = Decimal(micros) / Decimal(1_000_000) if isinstance(micros, int) else None
            code = str(record.data.get(f"{amount_base}.currencyCode") or "")
        else:
            raw = record.data.get(amount_base)
            amount = Decimal(str(raw)) if raw not in (None, "") and not isinstance(raw, bool) else None
            code = ""
        if amount is None:
            without_amount += 1
            continue
        bucket = currencies.setdefault(code, new_bucket())
        stage = record.data.get(stage_field)
        share = probabilities.get(stage) if isinstance(stage, str) else None
        expected = amount * share if share is not None else None
        keys = crm_filters.group_key(session.datamodel, record, group_field, grain or "NONE", session.context) if group_field else ["*"]
        # Records without a probability count in their group and the totals, but add nothing to the expected amount.
        for entry in [bucket] + [bucket["groups"].setdefault(key, new_entry()) for key in keys]:
            entry["records"] += 1
            entry["amount"] += amount
            if expected is not None:
                entry["weighted"] += 1
                entry["expected"] += expected
        if expected is None:
            bucket["unweighted"]["records"] += 1
            bucket["unweighted"]["amount"] += amount
            missing[str(stage or "")] = missing.get(str(stage or ""), 0) + 1

    # Like a kanban board, a select field shows every option, also those without records.
    option_groups = session.option_groups(object_name, group_field) if group_field else []
    if option_groups and not currencies:
        currencies[crm_filters.default_currency(session.datamodel, object_name, field, session.settings.get("default_currency"))] = new_bucket()
    for bucket in currencies.values():
        for option in option_groups:
            bucket["groups"].setdefault(option, new_entry())

    def money(value: Decimal, code: str) -> dict[str, Any]:
        return {"value": engine.decimal_text(value), "display": session.amount_text(value, code or None)}

    def expected_of(entry: dict[str, Any], code: str) -> dict[str, Any]:
        if entry["records"] and not entry["weighted"]:
            return {"value": None, "display": ""}
        return money(entry["expected"], code)

    result_currencies = []
    for code in sorted(currencies):
        bucket = currencies[code]
        groups = []
        if group_field:
            for key in session.order_groups(object_name, group_field, grain, list(bucket["groups"])):
                entry = bucket["groups"][key]
                item = {
                    "group": key,
                    "label": session.group_label(object_name, group_field, key, grain),
                    "records": entry["records"],
                    "amount": money(entry["amount"], code),
                    "expected": expected_of(entry, code),
                }
                if entry["records"] > entry["weighted"]:
                    item["records_without_probability"] = entry["records"] - entry["weighted"]
                if group_field == stage_field:
                    share = probabilities.get(key)
                    item["probability"] = engine.decimal_text(share) if share is not None else None
                    item["probability_percent"] = engine.decimal_text(share * 100) if share is not None else None
                groups.append(item)
        result_currencies.append({
            "currency": code or None,
            "records": bucket["records"],
            "weighted_records": bucket["weighted"],
            "amount": money(bucket["amount"], code),
            "expected": money(bucket["expected"], code),
            "groups": groups,
            "unweighted": {"records": bucket["unweighted"]["records"], "amount": money(bucket["unweighted"]["amount"], code)},
        })
    return session.envelope(
        "expected-amount",
        object=object_name,
        field=field,
        stage_field=stage_field,
        probabilities={value: {"share": engine.decimal_text(share), "percent": engine.decimal_text(share * 100), "label": labels.get(value, value)} for value, share in probabilities.items()},
        group_by=group_field,
        granularity=grain,
        filter=group,
        include_deleted=include_deleted,
        records=len(records),
        records_without_amount=without_amount,
        stages_without_probability=[
            {"stage": stage or None, "label": labels.get(stage, stage), "records": count} for stage, count in sorted(missing.items())
        ],
        currencies=result_currencies,
    )


# ---------------------------------------------------------------------------
# workflow runs


def run_log(session: Any) -> list[dict[str, Any]]:
    """Every entry of the workflow run log in file order, with the shard and line it stands in."""
    root = session.target / RUNS_DIR
    entries: list[dict[str, Any]] = []
    if not root.is_dir():
        return entries
    for shard in sorted(root.glob("*.jsonl")):
        relative = shard.relative_to(session.target).as_posix()
        if shard.name.startswith(".") or not shard.is_file():
            continue
        if not RUN_SHARD_RE.fullmatch(shard.name):
            session.warnings.append(f"{relative} is no monthly YYYY-MM.jsonl shard of the run log and was not read")
            continue
        for number, line in enumerate(shard.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CrmError(f"{relative}:{number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(entry, dict) or entry.get("format") != RUN_FORMAT or not isinstance(entry.get("run_id"), str) or not entry["run_id"]:
                raise CrmError(f"{relative}:{number}: not a run entry of format {RUN_FORMAT} with a run_id")
            entries.append({"entry": entry, "path": relative, "line": number})
    return entries


def run_summary(session: Any, items: list[dict[str, Any]]) -> dict[str, Any]:
    """One run from its log entries (oldest first): the latest state, every step's status, all outbox files."""
    entries = [item["entry"] for item in items]
    latest = entries[-1]
    steps: dict[str, Any] = {}
    outbox: list[str] = []
    for entry in entries:
        # Each entry holds the step infos of the whole run so far; a later one is newer.
        steps.update(entry["steps"] if isinstance(entry.get("steps"), dict) else {})
        for name in entry.get("outbox") or []:
            if isinstance(name, str) and name not in outbox:
                outbox.append(name)
    trigger = next((entry["trigger"] for entry in entries if isinstance(entry.get("trigger"), dict)), {})
    started = next((entry["started_at"] for entry in entries if entry.get("started_at")), None)
    summary = {
        "run_id": latest["run_id"],
        "workflow": latest.get("workflow"),
        "workflow_name": next((entry["workflow_name"] for entry in reversed(entries) if entry.get("workflow_name")), None),
        "version": latest.get("version"),
        "status": latest.get("status"),
        "test": bool(latest.get("test")),
        "trigger": trigger.get("type"),
        "started_at": started,
        "started_display": session.instant_text(started),
        "ended_at": latest.get("ended_at"),
        "ended_display": session.instant_text(latest.get("ended_at")),
        "error": latest.get("error"),
        "steps": {step_id: info.get("status") if isinstance(info, dict) else None for step_id, info in steps.items()},
        "outbox": outbox,
        "log_entries": len(entries),
    }
    if any(entry.get("erased") for entry in entries):
        summary["erased"] = True
    return summary


def workflow_runs(
    engine: ModuleType,
    session: Any,
    *,
    workflow: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    limit: Optional[int] = None,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    if run_id is not None and (status or since or limit is not None):
        raise engine.QueryError("--run-id shows one run; --status, --since and --limit select runs for the list")
    if limit is not None and not 1 <= limit <= RUNS_LIMIT[1]:
        raise engine.QueryError(f"--limit must be between 1 and {RUNS_LIMIT[1]}")
    by_run: dict[str, list[dict[str, Any]]] = {}
    for item in run_log(session):
        by_run.setdefault(item["entry"]["run_id"], []).append(item)

    def logged(item: dict[str, Any]) -> tuple[Any, str, int]:
        return (parse_instant(item["entry"].get("logged_at")) or engine.EPOCH, item["path"], item["line"])

    for items in by_run.values():
        items.sort(key=logged)
    if run_id is not None:
        wanted = run_id.strip()
        items = by_run.get(wanted)
        if not items or (workflow and items[-1]["entry"].get("workflow") != workflow):
            raise engine.NotFound(f"run {wanted} is not in the run log" + (f" of workflow {workflow}" if workflow else ""))
        return session.envelope(
            "runs",
            run_id=wanted,
            run=run_summary(session, items),
            entries=[item["entry"] for item in items],
            log=[{"path": item["path"], "line": item["line"]} for item in items],
        )
    wanted_statuses: set[str] = set()
    if status:
        name = status.strip().upper()
        logged_statuses = {str(items[-1]["entry"].get("status")) for items in by_run.values()}
        if name not in RUN_STATUSES and name not in STATUS_ALIASES and name not in logged_statuses:
            known = ", ".join(dict.fromkeys(list(RUN_STATUSES) + sorted(logged_statuses - set(RUN_STATUSES))))
            raise engine.QueryError(f"unknown run status {status!r}; runs are logged with {known} (CANCELLED is read as STOPPED)")
        wanted_statuses = {name, STATUS_ALIASES.get(name, name)}
    start = engine.parse_since(session, since)
    chosen = []
    for items in by_run.values():
        summary = run_summary(session, items)
        if workflow and summary["workflow"] != workflow:
            continue
        if wanted_statuses and summary["status"] not in wanted_statuses:
            continue
        moment = parse_instant(summary["started_at"]) or logged(items[0])[0]
        if start is not None and moment < start:
            continue
        chosen.append(((moment, *logged(items[-1])), summary))
    chosen.sort(key=lambda pair: pair[0], reverse=True)
    count = RUNS_LIMIT[0] if limit is None else limit
    return session.envelope(
        "runs",
        workflow=workflow,
        status=status.strip().upper() if status else None,
        since=format_instant(start) if start else None,
        limit=count,
        total=len(chosen),
        returned=min(count, len(chosen)),
        runs=[summary for _key, summary in chosen[:count]],
    )


# ---------------------------------------------------------------------------
# command line


LOCK_HINT = (
    "The wiki is locked for maintenance, so --release reads nothing. Inside your own maintenance session query the "
    "working state with --lock-token instead of --release; otherwise wait until the maintainer has released the wiki."
)


def release_refusal(release: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {"state": release.get("state"), "release": release}
    if release.get("state") == "wiki_busy":
        report["message"] = LOCK_HINT
    return report


def emit(report: dict[str, Any], code: int, engine: Optional[ModuleType] = None) -> int:
    if engine is not None:
        print(engine.dump(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return code


def build_parser(engine: ModuleType) -> argparse.ArgumentParser:
    binding = argparse.ArgumentParser(add_help=False)
    binding.add_argument("--target", required=True)
    binding.add_argument("--lock-token", help="token of the current maintenance session")
    binding.add_argument("--release", action="store_true", help="read the verified published release")
    binding.add_argument("--allow-hydration", action="store_true", help="with --release: verify even if files must be downloaded first")
    parents = [binding, engine.common_parser()]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)
    engine.add_query_commands(subparsers, parents)
    stage_parser = subparsers.add_parser("time-in-stage", parents=parents, help="time per stage from the event log")
    stage_parser.add_argument("--object", required=True)
    stage_parser.add_argument("--field", default="stage")
    stage_parser.add_argument("--id")
    stage_parser.add_argument("--include-deleted", action="store_true")
    stage_parser.add_argument("--limit", type=int, default=100)
    amount_parser = subparsers.add_parser("expected-amount", parents=parents, help="amount weighted with stage probabilities")
    amount_parser.add_argument("--object", required=True)
    amount_parser.add_argument("--probabilities", required=True, help='JSON object of stage option to share, e.g. {"NEW": 0.1, "PROPOSAL": 0.6}')
    amount_parser.add_argument("--field", default="amount")
    amount_parser.add_argument("--stage-field", default="stage")
    amount_parser.add_argument("--group-by", help="default: the stage field; none for totals only")
    amount_parser.add_argument("--granularity")
    amount_parser.add_argument("--filter")
    amount_parser.add_argument("--include-deleted", action="store_true")
    runs_parser = subparsers.add_parser("runs", parents=parents, help="workflow runs from the run log, newest first")
    runs_parser.add_argument("--workflow", help="only runs of this workflow id")
    runs_parser.add_argument("--status", help="only runs with this logged status, e.g. COMPLETED, FAILED, RUNNING or STOPPED (CANCELLED is read as STOPPED)")
    runs_parser.add_argument("--since", help="only runs started at or after this date or ISO-8601 instant")
    runs_parser.add_argument("--limit", type=int, help=f"at most this many runs (default {RUNS_LIMIT[0]})")
    runs_parser.add_argument("--run-id", help="this one run completely: every log entry with its step outputs")
    return parser


def run(engine: ModuleType, session: Any, args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "runs":
        return workflow_runs(
            engine, session, workflow=args.workflow, status=args.status, since=args.since, limit=args.limit,
            run_id=args.run_id,
        )
    if args.command == "time-in-stage":
        if not 1 <= args.limit <= 1000:
            raise engine.QueryError("--limit must be between 1 and 1000")
        return time_in_stage(
            engine, session, args.object, field=args.field, record_id=args.id,
            include_deleted=args.include_deleted, limit=args.limit,
        )
    if args.command == "expected-amount":
        return expected_amount(
            engine, session, args.object, args.probabilities, field=args.field, stage_field=args.stage_field,
            group_by=args.group_by, granularity=args.granularity, filter_text=args.filter,
            include_deleted=args.include_deleted,
        )
    return engine.run_query(session, args)


def main(argv: Optional[list[str]] = None) -> int:
    engine = load_engine()
    args = build_parser(engine).parse_args(argv)
    if bool(args.lock_token) == bool(args.release):
        return emit({
            "state": "invalid_request",
            "error": "use exactly one of --lock-token (maintenance session) and --release (published release)",
        }, 4)
    target = Path(args.target).expanduser().resolve()
    release: Optional[dict[str, Any]] = None
    if args.release:
        from verify_release import verify_snapshot

        release = verify_snapshot(target, allow_hydration=bool(args.allow_hydration))
        if release.get("state") != "ready":
            return emit(release_refusal(release), 3)
    else:
        from wiki_lock import require_lock

        require_lock(target, args.lock_token)
    try:
        session = session_class(engine)(target, now=args.now, me=args.me)
        engine.seal_read_only()
        result = run(engine, session, args)
    except engine.NotFound as exc:
        return emit({"state": "not_found", "error": str(exc)}, 2)
    except engine.QueryError as exc:
        return emit({"state": "invalid_request", "error": str(exc)}, 4)
    except (CrmError, OSError, UnicodeDecodeError) as exc:
        return emit({"state": "error", "error": str(exc)}, 2)
    if release is not None:
        from verify_release import verify_snapshot

        final = verify_snapshot(target, str(release.get("manifest_sha256") or ""), allow_hydration=bool(args.allow_hydration))
        if final.get("state") != "ready":
            # The answer was read from a state that is no longer the verified release.
            return emit({**release_refusal(final), "discarded": args.command}, 3)
        result["mode"] = "release"
        result["release"] = {key: release.get(key) for key in ("version", "release_id", "released_at", "manifest_sha256")}
    else:
        result["mode"] = "maintenance"
    return emit(result, 0, engine)


if __name__ == "__main__":
    raise SystemExit(main())
