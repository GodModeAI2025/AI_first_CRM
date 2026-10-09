#!/usr/bin/env python3
"""Tests for crm_query.py, crm_schema.py, the frozen knowledge-skill export and the CRM notes of
the OKF export and the language-migration preview.

Run: python3 tests/query-schema-export/test_query_schema_export.py   (prints ALL OK or the failures)
Test wikis live under test-wikis/query-schema-export/ and are rebuilt on every run.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import traceback
import zipfile
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
SKILL = WORKSPACE.parent / "ai-first-crm"
SCRIPTS = SKILL / "scripts"
BASE = WORKSPACE / "test-wikis" / "query-schema-export"
WORK = BASE / ".work"
EXPORTS = BASE / ".exports"
PY = sys.executable
NOW = "2026-08-25T08:00:00Z"

sys.path.insert(0, str(WORKSPACE / "tools"))
sys.path.insert(0, str(SCRIPTS))
from harness import Wiki  # noqa: E402

import crm_contract  # noqa: E402
import crm_filters  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, message: str) -> bool:
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(message)
        print(f"  FAIL {message}")
    return bool(condition)


def run(script: Path, *args: object, cwd: Path | None = None, flags: tuple[str, ...] = ()) -> tuple[int, dict, str]:
    completed = subprocess.run([PY, *flags, str(script), *map(str, args)], capture_output=True, text=True, cwd=str(cwd) if cwd else None)
    try:
        report = json.loads(completed.stdout) if completed.stdout.strip() else {}
    except json.JSONDecodeError:
        report = {"_stdout": completed.stdout[-2000:]}
    if completed.returncode not in (0, 1, 2, 3, 4, 5, 6) or (not report and completed.stderr):
        report.setdefault("_stderr", completed.stderr[-2000:])
    return completed.returncode, report, completed.stdout


def make_base() -> Path:
    target = BASE / "base"
    if target.exists():
        shutil.rmtree(target)
    completed = subprocess.run([PY, str(WORKSPACE / "tools/make_test_wiki.py"), str(SKILL), str(target)], capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"make_test_wiki failed: {completed.stdout}{completed.stderr}")
    return target


def fresh_copy(source: Path, name: str) -> Path:
    target = BASE / name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    return target


def tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


class Session:
    """A locked maintenance session on one test wiki."""

    def __init__(self, target: Path, operation: str):
        self.target = target
        self.wiki = Wiki(target, BASE / ".tokens")
        state = self.wiki.acquire(operation).get("state")
        if state not in ("acquired", "held"):
            raise RuntimeError(f"lock not acquired: {state}")
        self.token = self.wiki.token_file.read_text(encoding="utf-8").strip()

    def close(self) -> None:
        self.wiki.release()

    def txn(self, operations: list[dict], now: str, origin: dict | None = None, confirm: bool = False) -> dict:
        request = {"actor": "agent/test", "origin": origin or {"kind": "manual"}, "operations": operations}
        plan = crm_contract.plan_transaction(self.target, request, now=now)
        if plan["errors"]:
            raise RuntimeError(f"transaction plan failed: {plan['errors']}")
        result, code = crm_contract.apply_transaction(self.target, self.token, plan, confirm_destructive=confirm)
        if code != 0:
            raise RuntimeError(f"transaction apply failed: {result}")
        return plan

    def query(self, *args: object) -> tuple[int, dict, str]:
        return run(SCRIPTS / "crm_query.py", args[0], "--target", self.target, "--lock-token", self.token, *args[1:])

    def schema(self, name: str, operations: list[dict], *, confirm: bool = False, apply: bool = True) -> tuple[int, dict, dict]:
        WORK.mkdir(parents=True, exist_ok=True)
        request = WORK / f"{name}.request.json"
        request.write_text(json.dumps({"actor": "agent/test", "occasion": name, "operations": operations}), encoding="utf-8")
        plan_file = WORK / f"{name}.plan.json"
        code, report, stdout = run(SCRIPTS / "crm_schema.py", "plan", "--target", self.target, "--lock-token", self.token,
                                   "--request-file", request, "--output", plan_file)
        check(str(self.target) not in stdout, f"{name}: schema plan report contains no absolute wiki path")
        if code != 0 or not apply:
            return code, report, {}
        args = ["apply", "--target", self.target, "--lock-token", self.token, "--plan-file", plan_file,
                "--expect-plan-sha256", report["plan_sha256"]]
        if confirm:
            args.append("--confirm-destructive")
        apply_code, result, _stdout = run(SCRIPTS / "crm_schema.py", *args)
        return apply_code, report, result

    def build_lint_release(self, operation_id: str, expected_version: str) -> dict:
        built = self.wiki.locked("crm_build.py", "--target", self.target, expect=(0,))
        check(built.get("state") in ("built", "fresh"), f"{operation_id}: crm_build ran ({built.get('state')})")
        lint = self.wiki.locked("lint_wiki.py", "--target", self.target, "--check-only", expect=(0, 1))
        check(lint.get("valid") is True, f"{operation_id}: lint valid before release: {lint.get('errors', [])[:5]}")
        return self.wiki.locked("release_wiki.py", "--target", self.target, "--bump", "minor", "--operation-id",
                                operation_id, "--expect-current-version", expected_version, expect=(0,))


def records_of(target: Path, object_name: str) -> dict:
    datamodel = crm_contract.load_datamodel(target)
    return crm_contract.RecordStore(target, datamodel).records(object_name)


def context(target: Path, me: str | None = None) -> crm_filters.Context:
    settings = crm_contract.load_json_file(target, crm_contract.SETTINGS_PATH, {})
    return crm_filters.Context(now=NOW, time_zone=settings.get("time_zone", "UTC"), me=me, week_start=settings.get("calendar_start_day", 1))


def by_title(target: Path, object_name: str, title: str):
    return next(record for record in records_of(target, object_name).values() if record.data.get("crm_title") == title)


# ---------------------------------------------------------------------------
# scenario: CRM data with synthetic history


def seed_crm(session: Session) -> dict[str, str]:
    session.wiki.locked("crm_init.py", "--target", session.target, expect=(0,))
    session.txn([
        {"op": "create", "object": "workspaceMember", "values": {"name": {"firstName": "Anna", "lastName": "Admin"}, "userEmail": "anna@firma.example"}},
        {"op": "create", "object": "workspaceMember", "values": {"name": {"firstName": "Ben", "lastName": "Berater"}, "userEmail": "ben@firma.example"}},
        {"op": "create", "object": "company", "ref": "acme", "values": {"name": "Acme GmbH", "domainName": "https://www.acme.example/", "address": {"addressCity": "Berlin"}}},
        {"op": "create", "object": "company", "values": {"name": "Beta AG", "domainName": "beta.example", "address": {"addressCity": "Hamburg"}}},
        {"op": "create", "object": "company", "values": {"name": "Gamma KG", "domainName": "gamma.example", "address": {"addressCity": "Berlin"}}},
        {"op": "create", "object": "person", "values": {"name": {"firstName": "Max", "lastName": "Müller"}, "emails": "max@acme.example", "jobTitle": "Einkauf", "company": {"ref": "acme"}}},
        {"op": "create", "object": "person", "values": {"name": {"firstName": "Erika", "lastName": "Muster"}, "emails": "erika@beta.example", "jobTitle": "Einkauf"}},
        {"op": "create", "object": "opportunity", "values": {"name": "Relaunch", "amount": {"amount": "12500.50", "currencyCode": "EUR"}, "company": {"ref": "acme"}, "closeDate": "2026-09-30T10:00:00Z"}},
    ], now="2026-08-01T08:00:00Z")
    session.txn([{"op": "update", "object": "opportunity", "record": {"match": {"name": "Relaunch"}}, "values": {"stage": "SCREENING"}}], now="2026-08-05T08:00:00Z")
    session.txn([
        {"op": "create", "object": "opportunity", "values": {"name": "Wartung", "amount": {"amount": "3000", "currencyCode": "EUR"}, "company": {"match": {"name": "Beta AG"}}, "closeDate": "2026-10-15T10:00:00Z"}},
    ], now="2026-08-10T08:00:00Z")
    session.txn([{"op": "update", "object": "opportunity", "record": {"match": {"name": "Relaunch"}}, "values": {"stage": "PROPOSAL"}}], now="2026-08-15T08:00:00Z")
    session.txn([{"op": "update", "object": "opportunity", "record": {"match": {"name": "Wartung"}}, "values": {"stage": "MEETING"}}], now="2026-08-20T08:00:00Z")
    anna = by_title(session.target, "workspaceMember", "Anna Admin").id
    session.txn([
        {"op": "create", "object": "opportunity", "values": {"name": "Lizenz", "stage": "PROPOSAL", "amount": {"amount": "1000", "currencyCode": "USD"}, "company": {"match": {"name": "Acme GmbH"}}}},
        {"op": "create", "object": "opportunity", "ref": "old", "values": {"name": "Altvertrag", "stage": "SCREENING", "amount": {"amount": "500", "currencyCode": "EUR"}, "company": {"match": {"name": "Gamma KG"}}}},
        {"op": "create", "object": "opportunity", "values": {"name": "Pilot", "stage": "SCREENING", "amount": {"amount": "200", "currencyCode": "EUR"}}},
        {"op": "create", "object": "task", "values": {"title": "Angebot nachfassen", "dueAt": "2026-08-25T09:00:00Z", "assignee": anna, "targets": [{"object": "company", "match": {"name": "Acme GmbH"}}]}},
        {"op": "create", "object": "task", "values": {"title": "Vertrag prüfen", "status": "DONE", "assignee": {"match": {"userEmail": "ben@firma.example"}}}},
        {"op": "create", "object": "note", "values": {"title": "Erstgespräch Acme", "bodyV2": "Kunde wünscht Rabatt für den Rahmenvertrag.\nNächster Termin im September.", "targets": [{"object": "company", "match": {"name": "Acme GmbH"}}]}},
    ], now="2026-08-22T08:00:00Z")
    session.txn([
        {"op": "delete", "object": "opportunity", "record": {"match": {"name": "Altvertrag"}}},
        {"op": "delete", "object": "person", "record": {"match": {"emails": "erika@beta.example"}}},
    ], now="2026-08-23T08:00:00Z")
    return {"anna": anna}


def test_queries(session: Session, ids: dict[str, str]) -> None:
    print("queries")
    target = session.target
    dm = crm_contract.load_datamodel(target)
    opportunities = list(records_of(target, "opportunity").values())
    ctx = context(target, ids["anna"])

    filters = [
        None,
        {"field": "stage", "operand": "IS_NOT", "value": "NEW"},
        {"op": "OR", "conditions": [{"field": "amount", "operand": "GREATER_THAN_OR_EQUAL", "value": 3000}, {"field": "stage", "operand": "IS", "value": "SCREENING"}]},
        {"field": "company", "operand": "IS", "value": by_title(target, "company", "Acme GmbH").id},
        {"field": "closeDate", "operand": "IS_RELATIVE", "value": "NEXT_2_MONTH"},
    ]
    sorts = [{"field": "amount", "direction": "desc"}]
    for spec in filters:
        args = ["find", "--object", "opportunity", "--sort", json.dumps(sorts), "--now", NOW, "--me", ids["anna"]]
        if spec:
            args += ["--filter", json.dumps(spec)]
        code, report, stdout = session.query(*args)
        expected = crm_filters.select_records(dm, opportunities, filter_spec=spec, sorts=sorts, context=ctx)
        check(code == 0 and report.get("state") == "ok", f"find {spec}: answered ({code}, {report.get('error')})")
        check([row["id"] for row in report.get("rows", [])] == [record.id for record in expected], f"find {spec}: same records and order as crm_filters")
        check(report.get("total") == len(expected), f"find {spec}: total matches crm_filters")
        check(str(target) not in stdout, "find output contains no absolute path")
    code, report, _ = session.query("find", "--object", "opportunity", "--include-deleted", "--limit", "2", "--offset", "1", "--now", NOW)
    every = crm_filters.select_records(dm, opportunities, context=ctx, include_deleted=True)
    check(report.get("total") == len(every) == 5 and [row["id"] for row in report["rows"]] == [record.id for record in every[1:3]], "find --include-deleted with offset and limit")
    check(report.get("generated_at") == NOW and report.get("time_zone") == "Europe/Berlin", "find names generated_at and the settings time zone")
    row = report["rows"][0]
    check(row["path"].startswith("records/opportunity/") and row["path"].endswith(".md"), "rows carry relative record paths")

    tasks = list(records_of(target, "task").values())
    me_filter = {"field": "assignee", "operand": "IS", "value": "@me"}
    code, report, _ = session.query("find", "--object", "task", "--filter", json.dumps(me_filter), "--me", ids["anna"], "--now", NOW)
    expected = crm_filters.select_records(dm, tasks, filter_spec=me_filter, context=ctx)
    check([row["id"] for row in report.get("rows", [])] == [record.id for record in expected] and len(expected) == 1, "find with @me resolves --me like crm_filters")
    code, report, _ = session.query("find", "--object", "task", "--filter", json.dumps({"field": "dueAt", "operand": "IS_TODAY"}), "--now", NOW, "--fields", "title,dueAt,assignee")
    check(report.get("total") == 1 and report["rows"][0]["values"]["dueAt"]["display"] == "25.08.2026 11:00", "IS_TODAY and date display follow the settings (Europe/Berlin, DAY_FIRST, HOUR_24)")

    code, report, _ = session.query("find", "--object", "opportunity", "--filter", '{"field": "nope", "operand": "IS", "value": 1}')
    check(code == 4 and report.get("state") == "invalid_request", "an unknown filter field is an invalid request (exit 4)")
    code, report, _ = session.query("find", "--object", "opportunity", "--filter", '{"field": "stage", "operand": "IS", "value": "Neu"}')
    check(code == 0 and any("not option values" in item for item in report.get("warnings", [])), "a label instead of an option value is reported as a warning")

    # aggregates
    live = [record for record in opportunities if not record.deleted]
    code, report, _ = session.query("aggregate", "--object", "opportunity", "--function", "count", "--group-by", "stage", "--now", NOW)
    groups = {group["group"]: group["value"] for group in report.get("groups", [])}
    expected_groups: dict[str, int] = {}
    for record in live:
        for key in crm_filters.group_key(dm, record, "stage", "NONE", ctx):
            expected_groups[key] = expected_groups.get(key, 0) + 1
    stage_options = crm_contract.option_values(dm.field("opportunity", "stage"))
    check(code == 0 and {key: value for key, value in groups.items() if value} == expected_groups and report.get("value") == len(live),
          f"COUNT grouped by stage equals crm_filters ({groups})")
    check([group["group"] for group in report["groups"]] == stage_options and groups["NEW"] == 0 and groups["CUSTOMER"] == 0,
          "grouping by a select field lists every option in option order, empty ones with 0")
    check(report["groups"][0]["label"] == "Neu" and report["groups"][-1]["label"] == "Kunde", "group labels are option labels")
    code, report, _ = session.query("aggregate", "--object", "opportunity", "--function", "SUM", "--field", "amount", "--group-by", "stage",
                                    "--filter", json.dumps({"field": "amount.currencyCode", "operand": "IS", "value": "EUR"}), "--now", NOW)
    customer = next(group for group in report.get("groups", []) if group["group"] == "CUSTOMER")
    check(customer["records"] == 0 and customer["value"] == "0" and customer["display"] == "0,00 EUR", f"an empty stage sums to 0 in the group currency ({customer})")
    code, report, _ = session.query("aggregate", "--object", "opportunity", "--function", "AVG", "--field", "amount", "--group-by", "stage", "--now", NOW)
    customer = next(group for group in report.get("groups", []) if group["group"] == "CUSTOMER")
    check(customer["records"] == 0 and customer["value"] is None, "an average over no records has no value")
    code, report, _ = session.query("aggregate", "--object", "opportunity", "--function", "SUM", "--field", "amount", "--now", NOW)
    eur = [record for record in live if record.data.get("amount.currencyCode") == "EUR"]
    usd = [record for record in live if record.data.get("amount.currencyCode") == "USD"]
    check(report.get("value") is None and set(report.get("by_currency", {})) == {"EUR", "USD"}, "SUM over several currencies is split, never added up")
    check(Decimal(report["by_currency"]["EUR"]["value"]) == crm_filters.aggregate(dm, eur, "SUM", "amount") == Decimal("15700.5"), "EUR sum equals crm_filters")
    check(Decimal(report["by_currency"]["USD"]["value"]) == crm_filters.aggregate(dm, usd, "SUM", "amount"), "USD sum equals crm_filters")
    check(report["by_currency"]["EUR"]["display"] == "15.700,50 EUR", "currency display uses DOTS_AND_COMMA")
    eur_filter = json.dumps({"field": "amount.currencyCode", "operand": "IS", "value": "EUR"})
    for function in ("AVG", "MIN", "MAX", "COUNT_EMPTY", "PERCENTAGE_NOT_EMPTY"):
        field = "closeDate" if function in ("COUNT_EMPTY", "PERCENTAGE_NOT_EMPTY") else "amount"
        code, report, _ = session.query("aggregate", "--object", "opportunity", "--function", function, "--field", field, "--filter", eur_filter, "--now", NOW)
        expected_value = crm_filters.aggregate(dm, crm_filters.select_records(dm, opportunities, filter_spec=json.loads(eur_filter), context=ctx), function, field)
        if function in ("AVG", "PERCENTAGE_NOT_EMPTY"):
            expected_value = expected_value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        check(code == 0 and Decimal(str(report.get("value"))) == expected_value, f"{function} equals crm_filters ({report.get('value')} vs {expected_value})")
    code, report, _ = session.query("aggregate", "--object", "opportunity", "--function", "countNotEmpty", "--field", "closeDate", "--group-by", "closeDate", "--granularity", "MONTH", "--now", NOW)
    check(code == 0 and [group["group"] for group in report["groups"]] == ["2026-09", "2026-10", ""], f"date groups by month, empty group last ({[group['group'] for group in report.get('groups', [])]})")
    code, report, _ = session.query("aggregate", "--object", "opportunity", "--function", "MAX", "--field", "closeDate", "--now", NOW)
    check(report.get("value") == "2026-10-15T10:00:00Z", "MAX of a date field returns the latest date")

    # get, search, timeline
    acme = by_title(target, "company", "Acme GmbH")
    code, report, stdout = session.query("get", "--object", "company", "--id", acme.id, "--now", NOW)
    incoming = {(item["object"], item["field"], item["title"]) for item in report.get("incoming", [])}
    check(code == 0 and {("person", "company", "Max Müller"), ("opportunity", "company", "Relaunch"), ("opportunity", "company", "Lizenz"),
                         ("task", "targets", "Angebot nachfassen"), ("note", "targets", "Erstgespräch Acme")} <= incoming, f"get lists incoming relations ({incoming})")
    check({item["title"] for item in report["fields"]["opportunities"]["raw"]} == {"Relaunch", "Lizenz"}, "the computed ONE_TO_MANY side lists live records")
    check(report["fields"]["domainName"]["raw"]["primaryLinkUrl"] == "https://www.acme.example/" and report["path"] == acme.path, "get returns raw values and the relative path")
    check(len(report.get("timeline", [])) == sum(1 for event in crm_contract.read_events(target) if event.get("record_id") == acme.id), "get returns the record timeline")
    code, report, _ = session.query("get", "--object", "company", "--id", "00000000-0000-4000-8000-000000000000")
    check(code == 2 and report.get("state") == "not_found", "an unknown record is not_found (exit 2)")
    code, report, _ = session.query("search", "--text", "Rabatt Rahmenvertrag", "--now", NOW)
    check(code == 0 and report["results"] and report["results"][0]["title"] == "Erstgespräch Acme" and "Rabatt" in report["results"][0]["snippet"], "search finds rich text with a snippet")
    code, report, _ = session.query("search", "--text", "acme.example")
    check(report["results"][0]["object"] == "company" and report["results"][0]["title"] == "Acme GmbH", "search ranks the exact domain first")
    code, report, _ = session.query("search", "--text", "max@acme.example")
    check(report["results"][0]["title"] == "Max Müller", "search ranks the exact e-mail first")
    code, report, _ = session.query("search", "--text", "muell")
    check(not report["results"], "search does not invent matches")
    code, report, _ = session.query("search", "--text", "Mül", "--objects", "person")
    check([item["title"] for item in report["results"]] == ["Max Müller"], "search matches prefixes and leaves the trash out")
    code, report, _ = session.query("timeline", "--since", "2026-08-15", "--now", NOW)
    since = crm_contract.parse_instant("2026-08-14T22:00:00Z")
    expected_count = sum(1 for event in crm_contract.read_events(target) if crm_contract.parse_instant(event["at"]) >= since)
    check(code == 0 and report.get("total") == expected_count and report["since"] == "2026-08-14T22:00:00Z", "timeline --since uses the start of the day in the CRM time zone")

    # time in stage
    relaunch = by_title(target, "opportunity", "Relaunch")
    code, report, _ = session.query("time-in-stage", "--object", "opportunity", "--id", relaunch.id, "--now", NOW)
    intervals = report["rows"][0]["intervals"] if code == 0 else []
    check([(item["stage"], item["days"], item["current"]) for item in intervals] == [("NEW", "4.00", False), ("SCREENING", "10.00", False), ("PROPOSAL", "10.00", True)],
          f"time in stage from the event log ({[(item['stage'], item['days']) for item in intervals]})")
    check(report["rows"][0]["current_since"] == "2026-08-15T08:00:00Z" and intervals[0]["basis"] == "event", "current stage since and basis")
    code, report, _ = session.query("time-in-stage", "--object", "opportunity", "--now", NOW)
    summary = {item["stage"]: item for item in report.get("summary", [])}
    check(summary["SCREENING"]["completed_intervals"] == 1 and summary["SCREENING"]["average_days_completed"] == "10.00", "summary averages completed intervals")
    check(summary["PROPOSAL"]["records_in_stage"] == 2 and summary["NEW"]["completed_intervals"] == 2, "summary counts current and completed intervals")
    code, report, _ = session.query("time-in-stage", "--object", "opportunity", "--field", "amount")
    check(code == 4, "time in stage refuses a non-SELECT field")

    # expected amount: probabilities are shares from 0 to 1, "40%" is read as 0.4
    probabilities = {"NEW": 0.1, "SCREENING": 0.25, "MEETING": "40%", "Angebot": 0.6, "CUSTOMER": 1}
    code, report, _ = session.query("expected-amount", "--object", "opportunity", "--probabilities", json.dumps(probabilities), "--now", NOW)
    currencies = {item["currency"]: item for item in report.get("currencies", [])}
    check(code == 0 and currencies["EUR"]["expected"]["value"] == "8750.3" and currencies["USD"]["expected"]["value"] == "600",
          f"expected amount is exact per currency ({ {code: item['expected']['value'] for code, item in currencies.items()} })")
    check([(group["group"], group["records"], group["expected"]["value"]) for group in currencies["EUR"]["groups"]]
          == [("NEW", 0, "0"), ("SCREENING", 1, "50"), ("MEETING", 1, "1200"), ("PROPOSAL", 1, "7500.3"), ("CUSTOMER", 0, "0")],
          "expected amount per stage lists every stage in option order, empty ones with 0")
    customer = currencies["EUR"]["groups"][-1]
    check(customer["label"] == "Kunde" and customer["amount"]["display"] == "0,00 EUR" and customer["probability"] == "1", "the empty stage Kunde appears with 0")
    check(currencies["EUR"]["expected"]["display"] == "8.750,30 EUR" and currencies["EUR"]["groups"][2]["probability_percent"] == "40",
          "expected amount display and a probability given with a percent sign")
    code, report, _ = session.query("expected-amount", "--object", "opportunity", "--probabilities", '{"NEW": 0.1}', "--now", NOW)
    eur = next(item for item in report.get("currencies", []) if item["currency"] == "EUR")
    meeting = next(group for group in eur["groups"] if group["group"] == "MEETING")
    check(report.get("stages_without_probability") and eur["unweighted"]["records"] == 3 and eur["records"] == 3,
          "stages without probability are listed, not guessed")
    check(meeting["records"] == 1 and meeting["expected"]["value"] is None and meeting["records_without_probability"] == 1 and meeting["probability"] is None,
          "a stage without probability keeps its records but has no expected amount")
    code, report, _ = session.query("expected-amount", "--object", "opportunity", "--probabilities", '{"NEW": 60}')
    check(code == 4, "a probability above 1 is refused as ambiguous (shares, as in kanban views)")
    code, report, _ = session.query("expected-amount", "--object", "opportunity", "--probabilities", '{"NEW": 0.1}',
                                    "--filter", json.dumps({"field": "name", "operand": "IS", "value": "nicht vorhanden"}), "--now", NOW)
    check(code == 0 and [group["group"] for group in report["currencies"][0]["groups"]] == stage_options and report["currencies"][0]["currency"] == "EUR",
          "without any matching record every stage still appears, in the default currency")

    code, report, _ = run(SCRIPTS / "crm_query.py", "find", "--target", target, "--release", "--object", "company")
    check(code == 3 and report.get("state") == "wiki_busy", "--release is refused while the maintenance lock exists")
    check("--lock-token" in report.get("message", ""), "the refusal tells to use --lock-token inside the own maintenance session")
    code, report, _ = run(SCRIPTS / "crm_query.py", "find", "--target", target, "--object", "company")
    check(code == 4, "a query without --lock-token or --release is refused")


# ---------------------------------------------------------------------------
# scenario: schema changes


def test_schema(session: Session) -> None:
    print("schema")
    target = session.target
    code, report, result = session.schema("add-project", [
        {"op": "add-object", "labelSingular": "Projekt", "labelPlural": "Projekte", "fields": {
            "budget": {"type": "CURRENCY", "label": "Budget", "default": {"currencyCode": "EUR"}},
            "company": {"type": "RELATION", "label": "Firma", "relation": {"type": "MANY_TO_ONE", "target": "company"}},
            "phase": {"type": "SELECT", "label": "Phase", "options": ["Planung", "Umsetzung"], "default": "PLANUNG"},
        }},
        {"op": "add-field", "object": "company", "label": "Mutterfirma", "type": "RELATION",
         "relation": {"type": "MANY_TO_ONE", "target": "company", "inverseLabel": "Tochterfirmen"}},
        {"op": "add-field", "object": "company", "label": "Type", "type": "TEXT"},
    ])
    check(code == 0 and result.get("state") == "applied", f"add-object with relation applied ({report.get('errors')}, {result.get('state')})")
    dm = crm_contract.load_datamodel(target)
    project = dm.objects.get("projekt", {})
    check(project.get("labelField") == "name" and project["fields"]["name"]["type"] == "TEXT" and project.get("standard") is False, "new object gets the label field name")
    check(dm.field("projekt", "company")["relation"] == {"type": "MANY_TO_ONE", "target": "company", "inverse": "projekte", "onDelete": "SET_NULL"}, "MANY_TO_ONE side stored")
    check(dm.field("company", "projekte")["relation"] == {"type": "ONE_TO_MANY", "target": "projekt", "inverse": "company"}, "ONE_TO_MANY side created automatically")
    check(dm.field("company", "tochterfirmen")["relation"]["inverse"] == "mutterfirma", "self relation gets both sides")
    check("typeCustom" in dm.fields("company"), "a reserved name derived from a label gets the Custom suffix")
    check([option["value"] for option in dm.field("projekt", "phase")["options"]] == ["PLANUNG", "UMSETZUNG"], "option values derived from labels")
    check(not crm_contract.validate_datamodel(dm.raw), "the new data model validates")
    session.txn([{"op": "create", "object": "projekt", "values": {"name": "Website", "company": {"match": {"name": "Acme GmbH"}}, "budget": {"amount": "5000"}}}], now="2026-08-24T08:00:00Z")
    code, report, _ = session.query("get", "--object", "company", "--id", by_title(target, "company", "Acme GmbH").id)
    check(any(item["object"] == "projekt" and item["title"] == "Website" for item in report.get("incoming", [])) and report["fields"]["projekte"]["display"] == "Website",
          "records of the new object link through the new relation")

    code, report, _ = session.schema("reserved", [{"op": "add-field", "object": "company", "name": "type", "label": "Typ", "type": "TEXT"}])
    check(code == 1 and "reserved" in json.dumps(report.get("errors")), "an explicit reserved name is refused")
    code, report, _ = session.schema("crm-prefix", [{"op": "add-field", "object": "company", "name": "crmScore", "label": "Score", "type": "NUMBER"}])
    check(code == 1, "a name starting with crm is refused")

    # rename-option rewrites records, the trash included, with migration events
    before = {record.id: record for record in records_of(target, "opportunity").values()}
    screening = [record for record in before.values() if record.data.get("stage") == "SCREENING"]
    events_before = sum(1 for _ in crm_contract.read_events(target))
    code, report, result = session.schema("rename-option", [
        {"op": "rename-option", "object": "opportunity", "field": "stage", "from": "SCREENING", "to": "QUALIFIED", "label": "Qualifiziert"}])
    check(code == 0 and result.get("state") == "applied", f"rename-option applied ({report.get('errors')})")
    after = records_of(target, "opportunity")
    check(len(screening) == 2 and all(after[record.id].data.get("stage") == "QUALIFIED" for record in screening), "every SCREENING record now holds QUALIFIED")
    check(any(record.deleted for record in screening), "the rename reached a record in the trash")
    check(all(after[record.id].data.get("crm_updated_at") == record.data.get("crm_updated_at") for record in screening), "a migration leaves crm_updated_at untouched")
    new_events = list(crm_contract.read_events(target))[events_before:]
    check(len(new_events) == 2 and all(event["origin"]["kind"] == "migration" and event["schema_change"]["op"] == "rename-option"
                                       and event["changes"]["stage"] == ["SCREENING", "QUALIFIED"] for event in new_events), "rename-option appends migration events")
    stage = crm_contract.load_datamodel(target).field("opportunity", "stage")
    check(any(option["value"] == "QUALIFIED" and option["label"] == "Qualifiziert" for option in stage["options"]) and "SCREENING" not in crm_contract.option_values(stage),
          "the option is renamed in the data model")
    relaunch = by_title(target, "opportunity", "Relaunch")
    code, report, _ = session.query("time-in-stage", "--object", "opportunity", "--id", relaunch.id, "--now", NOW)
    check([(item["stage"], item["days"]) for item in report["rows"][0]["intervals"]] == [("NEW", "4.00"), ("QUALIFIED", "10.00"), ("PROPOSAL", "10.00")],
          "time in stage treats the rename as a relabel, not as a move")

    # unique and nullable refusals list the records
    code, report, _ = session.schema("unique", [{"op": "update-field", "object": "person", "field": "jobTitle", "unique": True}])
    details = report.get("errors", [{}])[0].get("details", [])
    check(code == 1 and details and len(details[0]["records"]) == 2 and any(item["deleted"] for item in details[0]["records"]),
          "unique is refused for duplicates, the trash included, with the list of records")
    code, report, _ = session.schema("nullable", [{"op": "update-field", "object": "company", "field": "linkedinLink", "nullable": False}])
    check(code == 1 and "no value" in json.dumps(report.get("errors")) and len(report["errors"][0].get("details", [])) == 3,
          "nullable false is refused while live records are empty, with the list of records")
    code, report, result = session.schema("nullable-ok", [{"op": "update-field", "object": "company", "field": "address", "nullable": False}])
    check(code == 0 and result.get("state") == "applied", "nullable false is accepted when every live record has a value")
    code, report, result = session.schema("labels", [{"op": "update-field", "object": "company", "field": "typeCustom", "label": "Firmentyp", "unique": True}])
    check(code == 0 and result.get("state") == "applied", "update-field label and unique without duplicates")

    # options
    code, report, result = session.schema("options", [
        {"op": "add-option", "object": "opportunity", "field": "stage", "label": "Verhandlung", "color": "orange", "position": 3},
        {"op": "update-option", "object": "opportunity", "field": "stage", "value": "CUSTOMER", "color": "green", "position": 0},
    ])
    stage = crm_contract.load_datamodel(target).field("opportunity", "stage")
    check(code == 0 and crm_contract.option_values(stage) == ["CUSTOMER", "NEW", "QUALIFIED", "MEETING", "VERHANDLUNG", "PROPOSAL"]
          and [option["position"] for option in stage["options"]] == list(range(len(stage["options"]))), f"add-option and update-option keep positions ({crm_contract.option_values(stage)})")
    code, report, _ = session.schema("bad-color", [{"op": "add-option", "object": "opportunity", "field": "stage", "label": "X", "color": "neon"}])
    check(code == 1, "an unknown color is refused")
    code, report, _ = session.schema("reuse", [{"op": "add-option", "object": "opportunity", "field": "stage", "value": "SCREENING", "label": "Prüfung neu"}])
    check(code == 1 and "earlier value" in json.dumps(report.get("errors")), "an earlier option value cannot be given to a new option")
    code, report, _ = session.schema("remove-used", [{"op": "remove-option", "object": "opportunity", "field": "stage", "value": "MEETING"}])
    check(code == 1 and report["errors"][0].get("details"), "removing a used option without map_to is refused with the records")
    code, report, result = session.schema("remove-mapped", [{"op": "remove-option", "object": "opportunity", "field": "stage", "value": "MEETING", "map_to": "VERHANDLUNG"}])
    check(code == 0 and by_title(target, "opportunity", "Wartung").data.get("stage") == "VERHANDLUNG", "remove-option with map_to moves the records")
    code, report, _ = session.schema("rename-customer", [{"op": "rename-option", "object": "opportunity", "field": "stage", "from": "CUSTOMER", "to": "WON"}], apply=False)
    check(code == 0 and any("dashboards.json" in item and "CUSTOMER" in item for item in report.get("dependent_references", [])),
          f"dependent dashboards are reported ({report.get('dependent_references')})")

    # deactivate and activate
    code, report, result = session.schema("deactivate", [{"op": "deactivate", "object": "company", "field": "linkedinLink"}])
    check(code == 0 and crm_contract.load_datamodel(target).field("company", "linkedinLink").get("active") is False, "deactivate a field")
    code, report, result = session.schema("activate", [{"op": "activate", "object": "company", "field": "linkedinLink"}])
    check(code == 0 and crm_contract.load_datamodel(target).field("company", "linkedinLink").get("active") is True, "activate the field again")
    code, report, _ = session.schema("deactivate-label", [{"op": "deactivate", "object": "company", "field": "name"}])
    check(code == 1, "the label field cannot be deactivated")

    # delete-field: destructive, confirmation required, values removed
    code, report, _ = session.schema("delete-standard", [{"op": "delete-field", "object": "company", "field": "address"}])
    check(code == 1 and "standard" in json.dumps(report.get("errors")), "standard fields cannot be deleted")
    datamodel_before = (target / crm_contract.DATAMODEL_PATH).read_bytes()
    code, report, result = session.schema("delete-budget", [{"op": "delete-field", "object": "projekt", "field": "budget"}])
    check(code == 3 and result.get("state") == "confirmation_required" and (target / crm_contract.DATAMODEL_PATH).read_bytes() == datamodel_before,
          "delete-field needs --confirm-destructive and writes nothing without it")
    plan = json.loads((WORK / "delete-budget.plan.json").read_text(encoding="utf-8"))
    code, result, _ = run(SCRIPTS / "crm_schema.py", "apply", "--target", target, "--lock-token", session.token, "--plan-file", WORK / "delete-budget.plan.json",
                          "--expect-plan-sha256", plan["plan_sha256"], "--confirm-destructive")
    website = by_title(target, "projekt", "Website")
    check(code == 0 and "budget.amountMicros" not in website.data and "budget.currencyCode" not in website.data and "budget" not in crm_contract.load_datamodel(target).fields("projekt"),
          "delete-field removes the definition and every value")

    # stale plans are refused with zero writes
    code, report, _ = session.schema("stale", [{"op": "update-field", "object": "company", "field": "typeCustom", "label": "Art"}], apply=False)
    session.schema("intervening", [{"op": "update-field", "object": "company", "field": "typeCustom", "description": "Art der Firma"}])
    plan = json.loads((WORK / "stale.plan.json").read_text(encoding="utf-8"))
    code, result, _ = run(SCRIPTS / "crm_schema.py", "apply", "--target", target, "--lock-token", session.token, "--plan-file", WORK / "stale.plan.json",
                          "--expect-plan-sha256", plan["plan_sha256"])
    check(code == 3 and result.get("state") == "stale_plan", "a plan for an older data model is stale")
    code, result, _ = run(SCRIPTS / "crm_schema.py", "apply", "--target", target, "--lock-token", session.token, "--plan-file", WORK / "stale.plan.json",
                          "--expect-plan-sha256", "0" * 64)
    check(code == 3 and result.get("state") == "stale_plan" and "stale or was modified" in result.get("reason", ""), "a wrong plan hash is refused as stale_plan")

    # delete-object: an object without history goes; one with records needs core support for tombstones
    code, report, result = session.schema("scratch-object", [{"op": "add-object", "name": "scratchItem", "labelSingular": "Entwurf", "labelPlural": "Entwürfe"}])
    code, report, result = session.schema("delete-scratch", [{"op": "delete-object", "object": "scratchItem"}])
    check(code == 0 and "scratchItem" not in crm_contract.load_datamodel(target).objects, "delete-object without records or history")
    # Without confirmation nothing is deleted; the confirmed write path is covered by test_delete_object_with_records.
    code, report, result = session.schema("delete-project", [{"op": "delete-object", "object": "projekt"}])
    import crm_schema  # noqa: E402

    if crm_schema.core_accepts_tombstones():
        check(code == 3 and "projekt" in crm_contract.load_datamodel(target).objects, "delete-object with records waits for confirmation")
    else:
        check(code == 1 and "deletedObjects" in json.dumps(report.get("errors")), "delete-object with history is refused until the core accepts tombstones")
    code, report, result = session.schema("deactivate-project", [{"op": "deactivate", "object": "projekt"}])
    check(code == 0 and crm_contract.load_datamodel(target).objects["projekt"].get("active") is False, "the object can be deactivated instead")
    code, report, _ = session.schema("delete-standard-object", [{"op": "delete-object", "object": "company"}])
    check(code == 1, "standard objects cannot be deleted")
    code, report, _ = session.schema("nothing", [{"op": "deactivate", "object": "company", "field": "linkedinLink"},
                                                 {"op": "activate", "object": "company", "field": "linkedinLink"}])
    check(code == 1 and "change nothing" in json.dumps(report.get("errors")), "a plan that changes nothing is refused")


def test_delete_object_with_records(source: Path) -> None:
    """The write path of delete-object with records, with tombstone support simulated where the core lacks it."""
    print("delete-object with records")
    import crm_schema  # noqa: E402

    target = fresh_copy(source, "schema-delete")
    session = Session(target, "delete-object")
    supported = crm_schema.core_accepts_tombstones()
    original = crm_schema.core_accepts_tombstones
    crm_schema.core_accepts_tombstones = lambda: True
    try:
        plan = crm_schema.plan_schema(target, {"actor": "agent/test", "operations": [
            {"op": "activate", "object": "projekt"},
            {"op": "add-field", "object": "note", "label": "Bezug Projekt", "type": "MORPH_RELATION", "relation": {"targets": ["projekt", "company"]}},
            {"op": "add-field", "object": "task", "label": "Projekt", "type": "RELATION", "relation": {"type": "MANY_TO_ONE", "target": "projekt", "inverseLabel": "Aufgaben"}},
        ]})
        check(not plan["errors"] and crm_schema.apply_schema(target, session.token, plan)[1] == 0, "preparing relations to the object")
        project = by_title(target, "projekt", "Website")
        session.txn([
            {"op": "update", "object": "note", "record": {"match": {"title": "Erstgespräch Acme"}}, "values": {"bezugProjekt": [f"projekt:{project.id}", {"object": "company", "match": {"name": "Acme GmbH"}}]}},
            {"op": "update", "object": "task", "record": {"match": {"title": "Angebot nachfassen"}}, "values": {"projekt": project.id}},
        ], now="2026-10-01T08:00:00Z")
        plan = crm_schema.plan_schema(target, {"actor": "agent/test", "operations": [{"op": "delete-object", "object": "projekt"}]})
        check(not plan["errors"] and plan["destructive"] and plan["summary"]["records_removed"] == 1 and plan["summary"]["records_changed"] == 2,
              f"delete-object plans the record removal and the cleared links ({plan['errors']}, {plan['summary']})")
        result, code = crm_schema.apply_schema(target, session.token, plan)
        check(code == 3 and result["state"] == "confirmation_required", "delete-object with records needs confirmation")
        result, code = crm_schema.apply_schema(target, session.token, plan, confirm_destructive=True)
        dm = crm_contract.load_datamodel(target)
        check(code == 0 and "projekt" not in dm.objects and "projekte" not in dm.fields("company") and "projekt" not in dm.fields("task")
              and dm.field("note", "bezugProjekt")["relation"]["targets"] == ["company"], "the object, both relation sides and the morph target are gone")
        check(not (target / "records/projekt").exists() and result.get("removed_directories") == ["records/projekt"], "the empty record directory is removed")
        check(dm.raw.get("deletedObjects", {}).get("projekt", {}).get("directory") == "records/projekt", "a tombstone keeps the history readable")
        note = by_title(target, "note", "Erstgespräch Acme")
        task = by_title(target, "task", "Angebot nachfassen")
        check(len(note.data.get("bezugProjekt", [])) == 1 and "projekt" not in task.data, "links to the deleted records are removed")
        session.wiki.locked("crm_build.py", "--target", target, expect=(0,))
        lint = session.wiki.locked("lint_wiki.py", "--target", target, "--check-only", expect=(0, 1))
        unknown = [error for error in lint.get("errors", []) if "unknown object 'projekt'" in error]
        if supported:
            check(lint.get("valid") is True, f"lint is valid after delete-object ({lint.get('errors', [])[:5]})")
        else:
            check(unknown and len(unknown) == len(lint.get("errors", [])),
                  f"without core support lint reports only the events of the deleted object ({lint.get('errors', [])[:5]})")
    finally:
        crm_schema.core_accepts_tombstones = original
        session.close()
    shutil.rmtree(target, ignore_errors=True)


# ---------------------------------------------------------------------------
# scenario: exports


def export(target: Path, name: str, *extra: str) -> tuple[int, dict, Path]:
    EXPORTS.mkdir(parents=True, exist_ok=True)
    for path in (EXPORTS / name, EXPORTS / f"{name}.skill", EXPORTS / f"{name}.skill.sha256"):
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    code, report, stdout = run(SCRIPTS / "export_wiki_skill.py", "--target", target, "--output-dir", EXPORTS, "--skill-name", name, *extra)
    check(str(target) not in stdout and str(EXPORTS) not in stdout, f"{name}: export report holds no absolute path")
    return code, report, EXPORTS / name


def entrypoint_runs(skill: Path, name: str, crm_object: str | None = None) -> None:
    runs = [("verify_knowledge.py", []), ("search_knowledge.py", ["--query", "Testwiki"]), ("assess_quality.py", []), ("identity_status.py", [])]
    if crm_object:
        runs += [("query_records.py", ["find", "--object", crm_object]), ("query_records.py", ["search", "--text", "Acme"])]
    before = tree_digest(skill)
    for script, args in runs:
        for flags in (("-I",), ()):
            code, report, _ = run(skill / "scripts" / script, *args, cwd=skill.parent, flags=flags)
            check(code == 0, f"{name}: scripts/{script} {' '.join(flags)} runs in the package ({report.get('_stderr', '')[-300:]})")
    check(tree_digest(skill) == before, f"{name}: running the entry points wrote nothing into the skill")


def test_export_plain(base: Path) -> None:
    print("export without CRM")
    code, report, skill = export(base, "plain-wissen")
    check(code == 0 and report.get("state") == "exported", f"plain export ({report})")
    scripts = sorted(path.name for path in (skill / "scripts").iterdir())
    check(scripts == sorted(["assess_quality.py", "frontmatter_contract.py", "identity_status.py", "search_knowledge.py", "trust_contract.py",
                             "verify_knowledge.py", "wiki_filters.py"]), f"plain export bundles trust_contract.py and no CRM helper ({scripts})")
    check(report.get("personal_data_warning") is None and report["crm"]["layer"] is False and report.get("excluded_prefixes") == [], "no CRM layer, no warning")
    check([item["script"] for item in report.get("self_check", [])] == ["verify_knowledge.py", "identity_status.py", "assess_quality.py", "search_knowledge.py"],
          "the exporter's self-check ran every entry point")
    check("CRM records" not in (skill / "SKILL.md").read_text(encoding="utf-8"), "SKILL.md of a wiki without CRM mentions no CRM query")
    entrypoint_runs(skill, "plain")
    with zipfile.ZipFile(EXPORTS / "plain-wissen.skill") as archive:
        names = archive.namelist()
    check(all(item.startswith("plain-wissen/") for item in names) and "plain-wissen/scripts/trust_contract.py" in names
          and not any("__pycache__" in item for item in names), "the package has one root and no bytecode")
    code, report, skill = export(base, "plain-ohne-crm", "--exclude-crm")
    check(code == 0 and json.loads((skill / "references/SNAPSHOT.json").read_text())["excluded_prefixes"], "--exclude-crm works on a wiki without CRM")
    code, verify_report, _ = run(skill / "scripts/verify_knowledge.py", flags=("-I",))
    check(code == 0 and verify_report.get("excluded_files") == 0, "verify accepts declared exclusions with nothing excluded")


def test_export_crm(target: Path) -> None:
    print("export with CRM")
    manifest = (target / "meta/manifest.json").read_bytes()
    code, report, skill = export(target, "crm-wissen")
    check(code == 0, f"CRM export ({report})")
    check(report["crm"]["included"] and report["crm"]["records_by_object"].get("opportunity") == 5 and report["crm"]["records"] >= 14,
          f"the result counts records per object ({report['crm']})")
    check(isinstance(report.get("personal_data_warning"), str) and "personal data" in report["personal_data_warning"], "the result carries a personal data warning")
    check("query_records.py" in report.get("entrypoints", []) and (skill / "scripts/crm_contract.py").is_file() and (skill / "scripts/crm_filters.py").is_file(),
          "query_records.py ships with crm_contract.py and crm_filters.py")
    check(not any((skill / "scripts" / name).exists() for name in ("portable_io.py", "wiki_lock.py", "snapshot_wiki.py", "crm_query.py", "crm_records.py")),
          "no writing helper is bundled")
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    check("query_records.py" in text and "## CRM records" in text, "SKILL.md explains the CRM query")
    entrypoint_runs(skill, "crm", "opportunity")
    # The frozen answers equal the release answers of crm_query.py.
    for args in (["find", "--object", "opportunity", "--filter", '{"field": "stage", "operand": "IS", "value": "PROPOSAL"}'],
                 ["aggregate", "--object", "opportunity", "--function", "SUM", "--field", "amount", "--group-by", "stage"],
                 ["search", "--text", "Acme"], ["timeline", "--object", "opportunity"]):
        code, frozen, _ = run(skill / "scripts/query_records.py", *args, "--now", NOW, flags=("-I",))
        code_release, released, _ = run(SCRIPTS / "crm_query.py", args[0], "--target", target, "--release", *args[1:], "--now", NOW)
        strip = lambda value: json.loads(json.dumps(value).replace("references/knowledge/", ""))  # noqa: E731
        keys = ("rows", "groups", "results", "events", "total", "value", "by_currency")
        check(code == 0 and code_release == 0 and {key: strip(frozen.get(key)) for key in keys} == {key: released.get(key) for key in keys},
              f"frozen query_records {args[0]} equals crm_query --release")
        check(frozen.get("mode") == "frozen-snapshot" and released.get("mode") == "release" and frozen["release"]["manifest_sha256"] == released["release"]["manifest_sha256"],
              f"{args[0]}: both answers name the same release")
    code, record, _ = run(skill / "scripts/query_records.py", "get", "--object", "company", "--id", by_title(target, "company", "Acme GmbH").id, flags=("-I",))
    check(code == 0 and record["path"].startswith("references/knowledge/records/company/") and record["incoming"], "get works in the package with bundled paths")
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); import query_records, crm_contract; query_records.seal_read_only()\n"
             "try:\n    crm_contract.apply_transaction(None, '', {})\nexcept crm_contract.CrmError as exc:\n    print('sealed')")
    completed = subprocess.run([PY, "-I", "-B", "-c", probe, str(skill / "scripts")], capture_output=True, text=True)
    check(completed.stdout.strip() == "sealed", f"the bundled core cannot write after import ({completed.stderr[-300:]})")
    completed = subprocess.run([PY, "-I", "-B", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import crm_contract; crm_contract.atomic_write(None, b'')", str(skill / "scripts")],
                               capture_output=True, text=True)
    check(completed.returncode != 0 and "portable_io" in completed.stderr, "without the maintenance modules the core has no write path")
    (skill / "references/knowledge/records/company/extra.md").write_text("---\ncrm_id: x\n---\n", encoding="utf-8")
    code, report_verify, _ = run(skill / "scripts/query_records.py", "find", "--object", "company", flags=("-I",))
    check(code == 4 and report_verify.get("state") == "invalid_snapshot", "query_records refuses a tampered snapshot")
    (skill / "references/knowledge/records/company/extra.md").unlink()

    code, report, skill = export(target, "crm-ohne-daten", "--exclude-crm")
    check(code == 0 and report["crm"]["excluded"] and report.get("personal_data_warning") is None, f"--exclude-crm export ({report.get('state')})")
    knowledge = skill / "references/knowledge"
    check(not (knowledge / "records").exists() and not (knowledge / "meta/crm-events").exists() and not (knowledge / "graph/crm").exists()
          and (knowledge / "schema/crm/datamodel.json").is_file(), "records, events and CRM views are left out, the data model stays")
    check((knowledge / "meta/manifest.json").read_bytes() == manifest, "the original manifest is bundled unchanged")
    snapshot = json.loads((skill / "references/SNAPSHOT.json").read_text(encoding="utf-8"))
    check(snapshot["excluded_prefixes"] == ["schema/team.json", "meta/team-operations/", "graph/crm/", "meta/crm-events/", "meta/crm-runs/", "meta/crm-workflow-state.json", "records/"], "SNAPSHOT.json lists the excluded prefixes")
    code, verify_report, _ = run(skill / "scripts/verify_knowledge.py", flags=("-I",))
    check(code == 0 and verify_report["excluded_files"] > 0 and verify_report["verified_files"] + verify_report["excluded_files"] == verify_report["files"],
          f"verify_knowledge accepts exactly the excluded prefixes ({verify_report.get('errors')})")
    check(not (skill / "scripts/query_records.py").exists() and "without its CRM records" in (skill / "SKILL.md").read_text(encoding="utf-8"),
          "the excluded export ships no CRM query and says why")
    entrypoint_runs(skill, "crm-ohne-daten")
    tampered = EXPORTS / "tampered"
    released_record = sorted((EXPORTS / "crm-wissen/references/knowledge/records/company").glob("*.md"))[0]
    for case in ("present", "restored", "missing", "prefix"):
        if tampered.exists():
            shutil.rmtree(tampered)
        shutil.copytree(skill, tampered)
        if case == "present":
            (tampered / "references/knowledge/records/company").mkdir(parents=True)
            (tampered / "references/knowledge/records/company/x.md").write_text("x", encoding="utf-8")
        elif case == "restored":
            # A genuine released record put back below an excluded prefix breaks the export's promise.
            restored = tampered / "references/knowledge/records/company" / released_record.name
            restored.parent.mkdir(parents=True)
            shutil.copyfile(released_record, restored)
        elif case == "missing":
            (tampered / "references/knowledge/wiki/overview.md").unlink()
        else:
            data = json.loads((tampered / "references/SNAPSHOT.json").read_text(encoding="utf-8"))
            data["excluded_prefixes"].append("wiki/")
            (tampered / "references/SNAPSHOT.json").write_text(json.dumps(data), encoding="utf-8")
        code, verify_report, _ = run(tampered / "scripts/verify_knowledge.py", flags=("-I",))
        check(code == 4 and verify_report.get("state") == "invalid_snapshot", f"verify_knowledge rejects a tampered excluded export ({case})")
        if case == "restored":
            check(any("below an excluded prefix is present" in error for error in verify_report.get("errors", [])),
                  "a released file below an excluded prefix is named as present")
    shutil.rmtree(tampered)


def test_size_guard(source: Path) -> None:
    print("size guard")
    target = fresh_copy(source, "big")
    session = Session(target, "size")
    try:
        body = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 560_000
        session.txn([{"op": "create", "object": "note", "values": {"title": "Langes Protokoll", "bodyV2": body}}], now="2026-08-24T09:00:00Z")
        session.build_lint_release("size-guard", "0.2.0")
    finally:
        session.close()
    code, report, skill = export(target, "gross")
    check(code == 6 and report.get("reason") == "size_limit" and report["uncompressed_bytes"] > 30_000_000 and not skill.exists()
          and not (EXPORTS / "gross.skill").exists(), f"an export above 30 MB is refused before writing ({code}, {report.get('uncompressed_bytes')})")
    check(report.get("bytes_by_area", {}).get("records", 0) > 30_000_000, "the refusal shows where the bytes are")
    code, report, skill = export(target, "gross", "--allow-large")
    check(code == 0 and isinstance(report.get("size_warning"), str) and report["uncompressed_bytes"] > 30_000_000, "--allow-large exports with a size warning")
    code, report, skill = export(target, "gross-ohne-crm", "--exclude-crm")
    check(code == 0 and report["uncompressed_bytes"] < 30_000_000 and report.get("size_warning") is None, "--exclude-crm brings the export under the limit")
    for path in (EXPORTS / "gross", EXPORTS / "gross-ohne-crm"):
        shutil.rmtree(path, ignore_errors=True)
    shutil.rmtree(target, ignore_errors=True)


def test_okf_and_language(base: Path, crm_target: Path) -> None:
    print("OKF and language migration")
    for target, name, expect_note in ((base, "okf-plain", False), (crm_target, "okf-crm", True)):
        destination = EXPORTS / name
        shutil.rmtree(destination, ignore_errors=True)
        code, report, _ = run(SCRIPTS / "export_okf_bundle.py", "--target", target, "--destination", destination)
        readme = (destination / "README.md").read_text(encoding="utf-8") if destination.is_dir() else ""
        check(code == 0 and ("CRM records (" in readme) == expect_note, f"{name}: README names the CRM records only when there are some")
        check((report.get("crm_records_exported") is False) == expect_note and not (destination / "records").exists(), f"{name}: no record is exported")
    session = Session(crm_target, "language")
    try:
        code, report, _ = run(SCRIPTS / "plan_language_migration.py", "--target", crm_target, "--lock-token", session.token, "--to-language", "en", "--to-label", "English")
        labels = report.get("crm", {}).get("labels_to_translate", {})
        check(code == 0 and labels.get("objects", 0) >= 9 and labels.get("field_labels", 0) > 50 and labels.get("option_labels", 0) > 5
              and labels.get("view_labels", 0) >= 7 and labels.get("dashboard_labels", 0) >= 1 and labels.get("widget_labels", 0) >= 4, f"language preview counts CRM labels ({labels})")
        check("schema/crm/datamodel.json" in report.get("affected_files", []) and "Field values" in report["crm"]["unchanged"]
              and any("CRM record field values" in item for item in report.get("preserved", [])), "language preview says field values stay unchanged")
    finally:
        session.close()
    session = Session(base, "language-plain")
    try:
        code, report, _ = run(SCRIPTS / "plan_language_migration.py", "--target", base, "--lock-token", session.token, "--to-language", "en", "--to-label", "English")
        check(code == 0 and "crm" not in report, "language preview of a wiki without CRM is unchanged")
    finally:
        session.close()


def main() -> int:
    BASE.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree(EXPORTS, ignore_errors=True)
    base = make_base()
    steps = []
    try:
        test_export_plain(base)
    except Exception:  # noqa: BLE001 - every scenario reports and the others still run
        FAILURES.append("export without CRM raised:\n" + traceback.format_exc())
    crm_target = fresh_copy(base, "crm")
    session = Session(crm_target, "query-tests")
    try:
        ids = seed_crm(session)
        test_queries(session, ids)
        release = session.build_lint_release("query-release", "0.1.0")
        steps.append(release.get("version"))
    except Exception:  # noqa: BLE001
        FAILURES.append("queries raised:\n" + traceback.format_exc())
    finally:
        session.close()
    try:
        code, report, _ = run(SCRIPTS / "crm_query.py", "find", "--target", crm_target, "--release", "--object", "company", "--now", NOW)
        check(code == 0 and report.get("mode") == "release" and report["release"]["version"] == "0.2.0" and report["total"] == 3, f"--release reads the published release ({report.get('state')})")
        test_export_crm(crm_target)
        test_okf_and_language(base, crm_target)
        test_size_guard(crm_target)
    except Exception:  # noqa: BLE001
        FAILURES.append("exports raised:\n" + traceback.format_exc())
    schema_target = fresh_copy(crm_target, "schema")
    session = Session(schema_target, "schema-tests")
    try:
        test_schema(session)
        lint = session.wiki.locked("crm_build.py", "--target", schema_target, expect=(0,))
        lint = session.wiki.locked("lint_wiki.py", "--target", schema_target, "--check-only", expect=(0, 1))
        check(lint.get("valid") is True, f"lint is valid after all schema changes ({lint.get('errors', [])[:5]})")
    except Exception:  # noqa: BLE001
        FAILURES.append("schema raised:\n" + traceback.format_exc())
    finally:
        session.close()
    try:
        test_delete_object_with_records(schema_target)
    except Exception:  # noqa: BLE001
        FAILURES.append("delete-object raised:\n" + traceback.format_exc())
    if FAILURES:
        print(f"\n{len(FAILURES)} of {CHECKS} checks failed:")
        for failure in FAILURES:
            print(f"- {failure}")
        return 1
    print(f"\nALL OK ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
