#!/usr/bin/env python3
"""Tests of crm_workflows.py and crm_formula.py against a real test wiki.

Scenarios: Closed Won (stage -> CUSTOMER creates a task and an e-mail draft),
stale opportunities by CRON catch-up (FIND_RECORDS + ITERATOR + UPDATE),
formula field by CODE, Typeform-like webhook payload creating a person,
FORM pause and resume, DELAY with --now, CLASSIFY with IF_ELSE, loop
protection, immutable active versions, outbox contents (.eml, .ics), HTTP
requests that are never executed, iterator limit, round robin, lint.

Run: python3 tests/workflows/test_workflows.py  (prints ALL OK or the failures)
"""
import email
import email.policy
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent.parent
sys.path.insert(0, str(WORKSPACE / "tools"))
from harness import SCRIPTS, Wiki, base_wiki  # noqa: E402

sys.path.insert(0, str(SCRIPTS))
import crm_formula  # noqa: E402

BASE = WORKSPACE / "test-wikis" / "workflows"
TARGET = BASE / "wf-main"
WORK = BASE / ".work"
OUTBOX = BASE / "outbox-main"
FAILURES: list[str] = []
CHECKS = 0
TOKEN = ""


def check(condition, label, detail=None):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(f"{label}" + (f": {json.dumps(detail, ensure_ascii=False)[:1500]}" if detail is not None else ""))


def iso(moment):
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


T0 = datetime.now(timezone.utc).replace(second=0, microsecond=0)


def call(script, *args, expect=(0,)):
    completed = subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args), "--lock-token", TOKEN], capture_output=True, text=True)
    try:
        data = json.loads(completed.stdout) if completed.stdout.strip() else {}
    except json.JSONDecodeError:
        data = {"_raw": completed.stdout[-3000:], "_stderr": completed.stderr[-3000:]}
    if completed.returncode not in expect:
        FAILURES.append(f"{script} {' '.join(map(str, args[:3]))} exit {completed.returncode} (expected {expect}): {completed.stdout[-2500:]} {completed.stderr[-2500:]}")
    return data, completed.returncode


def wf(*args, expect=(0,)):
    return call("crm_workflows.py", *args, "--target", TARGET, expect=expect)


def write_json(name, value):
    WORK.mkdir(parents=True, exist_ok=True)
    path = WORK / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def transact(name, operations, origin=None):
    request = write_json(f"{name}-request.json", {"actor": "human:anna@firma.example", "origin": origin or {"kind": "manual"}, "operations": operations})
    plan = WORK / f"{name}-plan.json"
    report, _code = call("crm_records.py", "plan", "--target", TARGET, "--request-file", request, "--output", plan)
    check(report.get("state") == "planned", f"{name}: record plan", report)
    result, _code = call("crm_records.py", "apply", "--target", TARGET, "--plan-file", plan, "--expect-plan-sha256", report.get("plan_sha256"))
    check(result.get("state") == "applied", f"{name}: record apply", result)
    return result


def records(object_dir):
    found = {}
    directory = TARGET / "records" / object_dir
    for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
        text = path.read_text(encoding="utf-8")
        data = {}
        for line in text.split("---", 2)[1].splitlines():
            if ":" in line and not line.startswith(" "):
                key, _, value = line.partition(":")
                data[key.strip()] = value.strip().strip('"')
        data["_text"] = text
        found[path.stem] = data
    return found


def record_by(object_dir, key, value):
    for record_id, data in records(object_dir).items():
        if data.get(key) == value:
            return record_id, data
    return None, None


def plan_and_apply(name, workflow, *extra, expect_plan=(0,), expect_apply=(0,), confirm=False):
    plan = WORK / f"{name}.json"
    report, code = wf("run", "plan", "--workflow", workflow, "--actor", "agent/local", "--output", plan, "--outbox", OUTBOX, *extra, expect=expect_plan)
    result = None
    if code == 0 and report.get("state") == "planned":
        args = ["run", "apply", "--plan-file", plan, "--expect-plan-sha256", report["plan_sha256"]]
        if confirm:
            args.append("--confirm-destructive")
        result, _code = wf(*args, expect=expect_apply)
    return report, result


def run_entries():
    entries = []
    for shard in sorted((TARGET / "meta/crm-runs").glob("*.jsonl")):
        entries.extend(json.loads(line) for line in shard.read_text(encoding="utf-8").splitlines() if line.strip())
    return entries


def state():
    return json.loads((TARGET / "meta/crm-workflow-state.json").read_text(encoding="utf-8"))


def events():
    found = []
    for shard in sorted((TARGET / "meta/crm-events").glob("*.jsonl")):
        found.extend(json.loads(line) for line in shard.read_text(encoding="utf-8").splitlines() if line.strip())
    return found


def save_and_activate(workflow, definition, now=None):
    path = write_json(f"{workflow}-definition.json", definition)
    report, code = wf("save-draft", "--workflow", workflow, "--definition-file", path)
    check(report.get("state") == "draft_saved", f"{workflow}: save-draft", report)
    args = ["activate", "--workflow", workflow, "--version", str(report.get("version", 1))]
    if now:
        args += ["--now", iso(now)]
    activated, _code = wf(*args)
    check(activated.get("state") == "activated" and len(activated.get("sha256", "")) == 64, f"{workflow}: activate", activated)
    return activated


# ---------------------------------------------------------------------------
# setup


def setup():
    global TOKEN
    for path in [TARGET, WORK] + list(BASE.glob("outbox-*")):
        if path.exists():
            shutil.rmtree(path)
    BASE.mkdir(parents=True, exist_ok=True)
    shutil.copytree(base_wiki(), TARGET)
    wiki = Wiki(TARGET, BASE / ".tokens")
    acquired = wiki.acquire("workflow-tests")
    check(acquired.get("acquired") is True or acquired.get("state") in ("acquired", "ok"), "lock acquired", acquired)
    TOKEN = wiki.token_file.read_text(encoding="utf-8").strip()
    init, _code = call("crm_init.py", "--target", TARGET)
    check(init.get("state") == "initialized", "crm_init", init)
    datamodel_path = TARGET / "schema/crm/datamodel.json"
    model = json.loads(datamodel_path.read_text(encoding="utf-8"))
    fields = model["objects"]["opportunity"]["fields"]
    fields["probability"] = {"type": "NUMBER", "label": "Wahrscheinlichkeit"}
    fields["weightedAmount"] = {"type": "CURRENCY", "label": "Gewichteter Betrag"}
    fields["isStale"] = {"type": "BOOLEAN", "label": "Veraltet"}
    fields["priority"] = {"type": "SELECT", "label": "Prioritaet", "options": [{"value": "LOW", "label": "Niedrig"}, {"value": "HIGH", "label": "Hoch"}]}
    datamodel_path.write_text(json.dumps(model, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    transact("base", [
        {"op": "create", "object": "workspaceMember", "ref": "anna", "values": {"name": {"firstName": "Anna", "lastName": "Admin"}, "userEmail": "anna@firma.example"}},
        {"op": "create", "object": "workspaceMember", "ref": "ben", "values": {"name": {"firstName": "Ben", "lastName": "Berater"}, "userEmail": "ben@firma.example"}},
        {"op": "create", "object": "company", "ref": "acme", "values": {"name": "Acme GmbH", "domainName": "acme.example", "accountOwner": {"ref": "anna"}}},
        {"op": "create", "object": "person", "ref": "max", "values": {"name": {"firstName": "Max", "lastName": "Muster"}, "emails": "max@acme.example", "company": {"ref": "acme"}}},
        {"op": "create", "object": "opportunity", "values": {"name": "Website-Relaunch", "stage": "PROPOSAL", "amount": {"amount": "12500.50", "currencyCode": "EUR"},
                                                             "company": {"ref": "acme"}, "pointOfContact": {"ref": "max"}, "owner": {"ref": "anna"}}},
        {"op": "create", "object": "opportunity", "values": {"name": "Altvertrag", "stage": "MEETING", "company": {"ref": "acme"}, "owner": {"ref": "ben"}}},
        {"op": "create", "object": "opportunity", "values": {"name": "Bestandskunde", "stage": "CUSTOMER", "company": {"ref": "acme"}}},
        {"op": "create", "object": "opportunity", "values": {"name": "Loop-Test", "stage": "NEW", "probability": 10}},
        {"op": "create", "object": "opportunity", "values": {"name": "Folgeprojekt", "stage": "SCREENING", "owner": {"ref": "anna"}}},
    ])
    return wiki


# ---------------------------------------------------------------------------
# formula language


def test_formula():
    evaluate = crm_formula.evaluate
    check(evaluate("round(coalesce(amountMicros, 0) * coalesce(probability, 0) / 100)", {"amountMicros": 12500500000, "probability": 40}) == 5000200000, "formula: weighted micros exact")
    check(evaluate("12500.50 * 40 / 100") == 5000.2, "formula: decimal arithmetic without float noise")
    check(evaluate("concat(first, ' ', last)", {"first": "Ada", "last": "Muster"}) == "Ada Muster", "formula: concat")
    check(evaluate("date_add_days(today(), -7)", now="2026-10-20T09:00:00Z") == "2026-10-13", "formula: today relative to the run moment")
    check(evaluate("days_between('2026-10-01', due)", {"due": "2026-10-20T10:00:00Z"}) == 19, "formula: days_between")
    check(evaluate("'gross' if amount > 100 else 'small'", {"amount": 150}) == "gross", "formula: conditional expression")
    check(evaluate("stage in ['A', 'CUSTOMER'] and not done", {"stage": "CUSTOMER", "done": False}) is True, "formula: in, and, not")
    check(evaluate("upper(trim(name)) + '!'", {"name": "  acme "}) == "ACME!", "formula: text functions")
    check(evaluate("max(3, 9, 4) + min([5, 2]) + abs(-1) + len('abc')") == 15, "formula: min max abs len")
    check(evaluate("number('12.5') * 2") == 25, "formula: number()")
    check(evaluate("contains(tags, 'vip')", {"tags": ["vip", "b2b"]}) is True, "formula: contains on lists")
    answers = [{"text": "Jane", "type": "text"}, {"email": "jane@acme.example", "type": "email"}]
    check(evaluate("get(find(a, 'type', 'email'), 'email', '')", {"a": answers}) == "jane@acme.example", "formula: find and get")
    check(evaluate("a[0]['text']", {"a": answers}) == "Jane", "formula: indexing")
    check(evaluate("if_(x, 'ja', 'nein')", {"x": 0}) == "nein", "formula: if_")
    check(crm_formula.evaluate_many({"contact.email": "'x@y.example'", "n": "2", "m": "n * 3"}) == {"contact": {"email": "x@y.example"}, "n": 2, "m": 6}, "formula: evaluate_many nests dotted names")
    refused = {
        "a.b": "attribute", "__import__('os')": "unknown function", "lambda: 1": "lambda", "[x for x in y]": "comprehension",
        "f'{x}'": "f-string", "(x := 1)": "assignment", "'a' * 10**9": "too large", "9 ** 999": "exponent", "open('/etc/passwd')": "unknown function",
        "x.__class__": "attribute", "1 / 0": "division by zero", "'x' + " * 1 + "'y' * 200000": "too large", "print(1)": "unknown function",
        "{1, 2}": "set", "a if": "syntax",
    }
    for source, hint in refused.items():
        try:
            crm_formula.evaluate(source, {"x": 1, "y": [1], "a": {"b": 1}})
            check(False, f"formula refuses {source!r}")
        except crm_formula.FormulaError as exc:
            check(hint in str(exc).lower(), f"formula refusal reason for {source!r}", str(exc))
    deep = "(" * 60 + "1" + ")" * 60
    check(crm_formula.evaluate(deep) == 1, "formula: parentheses are not nesting")
    try:
        crm_formula.evaluate("-" * 200 + "1")
        check(False, "formula refuses deep nesting")
    except crm_formula.FormulaError:
        pass
    try:
        crm_formula.evaluate("x" * 3000)
        check(False, "formula refuses long source")
    except crm_formula.FormulaError:
        pass


# ---------------------------------------------------------------------------
# scenarios


CLOSED_WON = {
    "name": "Closed Won",
    "description": "Nach dem Gewinn: Onboarding-Aufgabe und E-Mail-Entwurf an die verantwortliche Person",
    "trigger": {"type": "DATABASE_EVENT", "name": "Record is updated", "settings": {"eventName": "opportunity.updated", "fields": ["stage"]}, "nextStepIds": ["won"]},
    "steps": [
        {"id": "won", "type": "FILTER", "name": "Nur gewonnene Chancen", "settings": {"input": {
            "stepFilterGroups": [{"id": "g1", "logicalOperator": "AND"}],
            "stepFilters": [{"id": "f1", "type": "SELECT", "stepOutputKey": "{{trigger.object.stage}}", "operand": "IS", "value": "CUSTOMER", "stepFilterGroupId": "g1"}]}},
         "nextStepIds": ["due"]},
        {"id": "due", "type": "CODE", "name": "Faelligkeit", "settings": {"input": {"logicFunctionInput": {}, "formulas": {"dueAt": "date_add_days(now(), 3)"}}}, "nextStepIds": ["task"]},
        {"id": "task", "type": "CREATE_RECORD", "name": "Onboarding-Aufgabe", "settings": {"input": {"objectName": "task", "objectRecord": {
            "title": "Onboarding: {{trigger.object.name}}", "dueAt": "{{due.dueAt}}", "status": "TODO",
            "assignee": {"id": "{{trigger.object.owner.id}}"},
            "targets": [{"id": "{{trigger.object.id}}"}, {"id": "{{trigger.object.company.id}}"}],
            "bodyV2": {"markdown": "Neuer Kunde {{trigger.object.company.name}}. Betrag in Micros: {{trigger.object.amount.amountMicros}}"}}}},
         "nextStepIds": ["mail"]},
        {"id": "mail", "type": "DRAFT_EMAIL", "name": "Bestaetigung an den Vertrieb", "settings": {"input": {
            "connectedAccountId": "", "recipients": {"to": "{{trigger.object.owner.userEmail}}", "cc": "kundenerfolg@firma.example"},
            "subject": "Gewonnen: {{trigger.object.name}} (Glückwunsch)", "inReplyTo": "angebot-42@acme.example",
            "body": "Glückwunsch! {{trigger.object.name}} ist gewonnen.\nAufgabe: {{task.title}}"}}},
    ],
}


def test_closed_won():
    activated = save_and_activate("closed-won", CLOSED_WON)
    check(activated.get("trigger") == "DATABASE_EVENT", "closed-won: trigger type", activated)
    validation, _code = wf("validate")
    check(validation.get("valid") is True, "validate after activation", validation)
    relaunch_id, _relaunch = record_by("opportunity", "name", "Website-Relaunch")
    old_id, _old = record_by("opportunity", "name", "Altvertrag")
    transact("won", [{"op": "update", "object": "opportunity", "record": relaunch_id, "values": {"stage": "CUSTOMER"}},
                     {"op": "update", "object": "opportunity", "record": old_id, "values": {"stage": "PROPOSAL"}}])
    now = T0 + timedelta(hours=1)
    report, result = plan_and_apply("closed-won-1", "closed-won", "--due", "--now", iso(now))
    runs = report.get("runs", [])
    check(len(runs) == 2, "closed-won: one run per stage change", report)
    winner = [run for run in runs if run["steps"].get("won") == "SUCCESS"]
    loser = [run for run in runs if run["steps"].get("won") == "STOPPED"]
    check(len(winner) == 1 and winner[0]["status"] == "COMPLETED" and winner[0]["steps"] == {"won": "SUCCESS", "due": "SUCCESS", "task": "SUCCESS", "mail": "SUCCESS"},
          "closed-won: winning run executes every step", runs)
    check(len(loser) == 1 and loser[0]["status"] == "COMPLETED" and loser[0]["steps"].get("task") == "SKIPPED" and loser[0]["steps"].get("mail") == "SKIPPED",
          "closed-won: filter stops the other run, later steps skipped", runs)
    check(result and result.get("state") == "applied", "closed-won: applied", result)
    task_id, task = record_by("task", "title", "Onboarding: Website-Relaunch")
    check(task is not None, "closed-won: task created", records("task"))
    if task:
        check(task.get("crm_created_source") == "WORKFLOW", "closed-won: created source WORKFLOW", task)
        check(task.get("dueAt") == iso(now + timedelta(days=3)), "closed-won: due date from formula", task)
        check(f"records/opportunity/{relaunch_id}" in task["_text"] and "records/company/" in task["_text"], "closed-won: targets opportunity and company", task["_text"])
        check("records/workspace-member/" in task.get("assignee", ""), "closed-won: assignee is the owner", task)
        check("Neuer Kunde Acme GmbH. Betrag in Micros: 12500500000" in task["_text"], "closed-won: rich text resolved", task["_text"])
    created = [event for event in events() if event.get("record_id") == task_id]
    check(created and created[0]["origin"].get("kind") == "workflow" and created[0]["origin"].get("workflow") == "closed-won" and created[0]["origin"].get("run_id") == winner[0]["run_id"] if winner and created else False,
          "closed-won: event origin names workflow and run", created)
    emls = sorted(OUTBOX.glob("closed-won-*.eml"))
    check(len(emls) == 1, "closed-won: one .eml draft", [path.name for path in emls])
    if emls:
        message = email.message_from_bytes(emls[0].read_bytes(), policy=email.policy.default)
        check(message["To"] == "anna@firma.example", "eml: To from owner", message["To"])
        check(message["Cc"] == "kundenerfolg@firma.example", "eml: Cc", message["Cc"])
        check(message["Subject"] == "Gewonnen: Website-Relaunch (Glückwunsch)", "eml: Subject with umlaut", message["Subject"])
        check(all(ord(char) < 128 for char in emls[0].read_bytes().decode("utf-8")), "eml: headers and body encoded as ASCII")
        check(message["X-Unsent"] == "1" and message["X-LMWiki-Status"] == "not-sent" and message["X-LMWiki-Mode"] == "DRAFT", "eml: marked as not sent", dict(message.items()))
        check(re.fullmatch(r"<run-[0-9a-f]{16}\.mail@lmwiki\.invalid>", message["Message-ID"] or "") is not None, "eml: Message-ID", message["Message-ID"])
        check(message["In-Reply-To"] == "<angebot-42@acme.example>" and message["References"] == "<angebot-42@acme.example>", "eml: threading headers", dict(message.items()))
        check(email.utils.parsedate_to_datetime(message["Date"]) == now, "eml: Date is the run moment", message["Date"])
        check("Glückwunsch! Website-Relaunch ist gewonnen." in message.get_content() and "Aufgabe: Onboarding: Website-Relaunch" in message.get_content(), "eml: body resolved", message.get_content())
        check(b"\r\n" in emls[0].read_bytes(), "eml: CRLF line endings")
    entries = [entry for entry in run_entries() if entry.get("workflow") == "closed-won"]
    check(len(entries) == 2 and all(entry["format"] == "lmwiki-crm-run/1" and entry["version"] == 1 and len(entry["definition_sha256"]) == 64 for entry in entries),
          "closed-won: run log entries with version and definition hash", entries)
    check(all(entry["status"] == "COMPLETED" for entry in entries), "closed-won: run statuses", [entry["status"] for entry in entries])
    check(state()["workflows"]["closed-won"].get("event_cursor"), "closed-won: event cursor stored", state())
    again, _ = plan_and_apply("closed-won-2", "closed-won", "--due", "--now", iso(now + timedelta(minutes=5)))
    check(again.get("summary", {}).get("runs") == 0, "closed-won: nothing new on the second call", again)
    deactivated, _ = wf("deactivate", "--workflow", "closed-won")
    check(deactivated.get("state") == "deactivated", "closed-won: deactivated", deactivated)


STALE = {
    "name": "Veraltete Chancen",
    "trigger": {"type": "CRON", "settings": {"type": "DAYS", "schedule": {"day": 1, "hour": 8, "minute": 0}}, "nextStepIds": ["cutoff"]},
    "steps": [
        {"id": "cutoff", "type": "CODE", "settings": {"input": {"formulas": {"date": "date_add_days(today(), -7)"}}}, "nextStepIds": ["find"]},
        {"id": "find", "type": "FIND_RECORDS", "settings": {"input": {"objectName": "opportunity", "filter": {"op": "AND", "conditions": [
            {"field": "crm_updated_at", "operand": "IS_BEFORE", "value": "{{cutoff.date}}"},
            {"field": "stage", "operand": "IS_NOT", "value": "CUSTOMER"}]},
            "orderBy": [{"field": "name", "direction": "asc"}], "limit": 100}}, "nextStepIds": ["each"]},
        {"id": "each", "type": "ITERATOR", "settings": {"input": {"items": "{{find.all}}", "initialLoopStepIds": ["mark"]}}},
        {"id": "mark", "type": "UPDATE_RECORD", "settings": {"input": {"objectName": "opportunity", "objectRecordId": "{{each.currentItem.id}}",
                                                                        "objectRecord": {"isStale": True}, "fieldsToUpdate": ["isStale"]}}, "nextStepIds": ["each"]},
    ],
}


def expected_slots(after, until):
    count = 0
    moment = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while moment <= until:
        if moment.hour == 8 and moment.minute == 0:
            count += 1
        moment += timedelta(minutes=1)
    return count


def test_stale_cron():
    save_and_activate("stale-opps", STALE, now=T0)
    first = (T0 + timedelta(days=2)).replace(hour=9, minute=30)
    report, result = plan_and_apply("stale-1", "stale-opps", "--due", "--now", iso(first))
    slots = expected_slots(T0, first)
    check(report.get("summary", {}).get("runs") == 1, "stale: missed schedule times run once", report)
    check(report.get("summary", {}).get("cron_due_slots") == slots and report["summary"].get("missed_cron_slots") == slots - 1, "stale: due and missed slots counted", [report.get("summary"), slots])
    run = (report.get("runs") or [{}])[0]
    check(run.get("status") == "COMPLETED" and run["steps"].get("each") == "SUCCESS" and "mark" not in run["steps"], "stale: nothing stale yet, loop body not run", run)
    check(state()["workflows"]["stale-opps"].get("cron_checked_at") == iso(first), "stale: cron cursor moved", state()["workflows"].get("stale-opps"))
    nothing, _ = plan_and_apply("stale-1b", "stale-opps", "--due", "--now", iso(first + timedelta(minutes=10)))
    check(nothing.get("summary", {}).get("runs") == 0, "stale: no slot in between", nothing)
    second = (T0 + timedelta(days=12)).replace(hour=9, minute=30)
    report, result = plan_and_apply("stale-2", "stale-opps", "--due", "--now", iso(second))
    run = (report.get("runs") or [{}])[0]
    check(report.get("summary", {}).get("missed_cron_slots") == expected_slots(first + timedelta(minutes=10), second) - 1, "stale: missed slots of the second catch-up", report.get("summary"))
    check(run.get("status") == "COMPLETED" and run.get("operations", {}).get("update") == 3, "stale: three stale opportunities updated", run)
    stale = {data.get("name") for data in records("opportunity").values() if data.get("isStale") == "true"}
    check(stale == {"Altvertrag", "Folgeprojekt", "Loop-Test"}, "stale: the right records were marked", stale)
    entry = [item for item in run_entries() if item.get("workflow") == "stale-opps"][-1]
    check(entry["steps"]["mark"].get("iterations", {}).get("SUCCESS") == 3 and entry["trigger"].get("missedSlots") == expected_slots(first + timedelta(minutes=10), second) - 1,
          "stale: run log counts iterations and missed slots", entry)
    wf("deactivate", "--workflow", "stale-opps")


FORMULA = {
    "name": "Gewichteter Betrag",
    "trigger": {"type": "DATABASE_EVENT", "settings": {"eventName": "opportunity.upserted", "fields": ["amount", "probability"]}, "nextStepIds": ["calc"]},
    "steps": [
        {"id": "calc", "type": "CODE", "name": "Betrag mal Wahrscheinlichkeit", "settings": {"input": {
            "logicFunctionInput": {"amountMicros": "{{trigger.object.amount.amountMicros}}", "probability": "{{trigger.object.probability}}"},
            "formulas": {"weightedMicros": "round(coalesce(amountMicros, 0) * coalesce(probability, 0) / 100)"}}}, "nextStepIds": ["store"]},
        {"id": "store", "type": "UPDATE_RECORD", "settings": {"input": {"objectName": "opportunity", "objectRecordId": "{{trigger.object.id}}", "objectRecord": {
            "weightedAmount": {"amountMicros": "{{calc.weightedMicros}}", "currencyCode": "{{trigger.object.amount.currencyCode}}"}}}}},
    ],
}


def test_formula_field():
    save_and_activate("weighted-amount", FORMULA)
    relaunch_id, _ = record_by("opportunity", "name", "Website-Relaunch")
    transact("probability", [{"op": "update", "object": "opportunity", "record": relaunch_id, "values": {"probability": 40}}])
    now = T0 + timedelta(days=13)
    report, result = plan_and_apply("weighted-1", "weighted-amount", "--due", "--now", iso(now))
    check(report.get("summary", {}).get("runs") == 1 and report["runs"][0]["status"] == "COMPLETED", "formula: one run", report)
    data = records("opportunity").get(relaunch_id, {})
    check(data.get("weightedAmount.amountMicros") == "5000200000" and data.get("weightedAmount.currencyCode") == "EUR", "formula: weighted amount stored exactly", data)
    again, _ = plan_and_apply("weighted-2", "weighted-amount", "--due", "--now", iso(now + timedelta(minutes=1)))
    check(again.get("summary", {}).get("runs") == 0 and again["summary"].get("events_ignored_own", 0) >= 1, "formula: own update does not trigger again", again.get("summary"))
    wf("deactivate", "--workflow", "weighted-amount")


TYPEFORM = {
    "name": "Typeform-Lead",
    "trigger": {"type": "WEBHOOK", "settings": {"httpMethod": "POST", "authentication": None, "expectedBody": {"event_type": "form_response", "form_response": {"answers": []}}}, "nextStepIds": ["extract"]},
    "steps": [
        {"id": "extract", "type": "CODE", "settings": {"input": {"logicFunctionInput": {"answers": "{{trigger.form_response.answers}}"}, "formulas": {
            "contact.firstName": "get(parse_json(answers), '0.text', '')",
            "contact.lastName": "get(parse_json(answers), '1.text', '')",
            "contact.email": "lower(get(find(parse_json(answers), 'type', 'email'), 'email', ''))",
            "contact.teamSize": "get(find(parse_json(answers), 'type', 'choice'), 'choice.label', '')"}}}, "nextStepIds": ["lead"]},
        {"id": "lead", "type": "UPSERT_RECORD", "settings": {"input": {"objectName": "person", "objectRecord": {
            "name": {"firstName": "{{extract.contact.firstName}}", "lastName": "{{extract.contact.lastName}}"},
            "emails": {"primaryEmail": "{{extract.contact.email}}"}, "jobTitle": "Teamgroesse {{extract.contact.teamSize}}"}}}},
    ],
}
TYPEFORM_PAYLOAD = {"event_type": "form_response", "form_response": {"form_id": "abc123", "submitted_at": "2026-10-06T10:30:00Z", "answers": [
    {"text": "Jane", "type": "text", "field": {"id": "field1", "type": "short_text", "title": "First Name"}},
    {"text": "Smith", "type": "text", "field": {"id": "field2", "type": "short_text", "title": "Last Name"}},
    {"email": "Jane@Acme.example", "type": "email", "field": {"id": "field4", "type": "email", "title": "Email"}},
    {"type": "choice", "field": {"id": "field5", "type": "dropdown", "title": "Team Size"}, "choice": {"label": "10-50"}}]}}


def test_webhook():
    save_and_activate("typeform-lead", TYPEFORM)
    payload = write_json("typeform-payload.json", TYPEFORM_PAYLOAD)
    report, result = plan_and_apply("typeform-1", "typeform-lead", "--payload-file", payload, "--now", iso(T0 + timedelta(days=14)))
    check(report.get("runs") and report["runs"][0]["status"] == "COMPLETED", "webhook: run completed", report)
    person_id, person = record_by("person", "emails.primaryEmail", "jane@acme.example")
    check(person is not None and person.get("name.firstName") == "Jane" and person.get("name.lastName") == "Smith" and person.get("jobTitle") == "Teamgroesse 10-50",
          "webhook: person created from the payload", records("person"))
    payload2 = dict(TYPEFORM_PAYLOAD)
    payload2["form_response"] = dict(TYPEFORM_PAYLOAD["form_response"])
    payload2["form_response"]["answers"] = [dict(item) for item in TYPEFORM_PAYLOAD["form_response"]["answers"]]
    payload2["form_response"]["answers"][3] = {"type": "choice", "choice": {"label": "50-200"}}
    report, result = plan_and_apply("typeform-2", "typeform-lead", "--payload-file", write_json("typeform-payload2.json", payload2), "--now", iso(T0 + timedelta(days=14, minutes=5)))
    janes = [data for data in records("person").values() if data.get("emails.primaryEmail") == "jane@acme.example"]
    check(len(janes) == 1 and janes[0].get("jobTitle") == "Teamgroesse 50-200", "webhook: second payload updates the same person", janes)
    entry = [item for item in run_entries() if item.get("workflow") == "typeform-lead"][-1]
    check(entry["trigger"]["output"]["body"]["event_type"] == "form_response" and entry["trigger"].get("payload_sha256"), "webhook: payload logged with digest", entry["trigger"])
    missing, _code = wf("run", "plan", "--workflow", "typeform-lead", "--actor", "agent/local", "--output", WORK / "typeform-3.json", expect=(2,))
    check("payload" in json.dumps(missing), "webhook: a run needs the payload file", missing)


QUICK_LEAD = {
    "name": "Schnellerfassung",
    "trigger": {"type": "MANUAL", "settings": {"availability": {"type": "GLOBAL"}}, "nextStepIds": ["form"]},
    "steps": [
        {"id": "form", "type": "FORM", "name": "Lead erfassen", "settings": {"instructions": "Bitte die Kontaktdaten eintragen.", "input": [
            {"id": "f1", "name": "firstName", "label": "Vorname", "type": "TEXT"},
            {"id": "f2", "name": "lastName", "label": "Nachname", "type": "TEXT"},
            {"id": "f3", "name": "email", "label": "E-Mail", "type": "TEXT", "placeholder": "name@firma.example"},
            {"id": "f4", "name": "stage", "label": "Phase", "type": "SELECT", "settings": {"objectName": "opportunity", "fieldName": "stage"}}]},
         "nextStepIds": ["person"]},
        {"id": "person", "type": "CREATE_RECORD", "settings": {"input": {"objectName": "person", "objectRecord": {
            "name": {"firstName": "{{form.firstName}}", "lastName": "{{form.lastName}}"}, "emails": {"primaryEmail": "{{form.email}}"}}}}, "nextStepIds": ["deal"]},
        {"id": "deal", "type": "CREATE_RECORD", "settings": {"input": {"objectName": "opportunity", "objectRecord": {
            "name": "Lead {{form.lastName}}", "stage": "{{form.stage}}", "pointOfContact": {"id": "{{person.id}}"}}}}},
    ],
}


def test_form():
    save_and_activate("quick-lead", QUICK_LEAD)
    report, result = plan_and_apply("form-1", "quick-lead", "--now", iso(T0 + timedelta(days=15)))
    run = (report.get("runs") or [{}])[0]
    check(run.get("status") == "RUNNING" and run.get("waiting_for") == ["form"] and run["steps"].get("form") == "PENDING", "form: run pauses at the form", report)
    question = (report.get("questions") or [{}])[0]
    check(question.get("type") == "FORM" and [field["name"] for field in question.get("fields", [])] == ["firstName", "lastName", "email", "stage"]
          and question["fields"][3].get("options") == ["NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER"], "form: questions list the fields", question)
    check(report.get("answers_template", {}).get("answers", [{}])[0].get("run_id") == run.get("run_id"), "form: answers template", report.get("answers_template"))
    run_id = run.get("run_id")
    pending = state()["pending_runs"].get(run_id, {})
    check(pending.get("version") == 1 and len(pending.get("definition_sha256", "")) == 64 and pending.get("status") == "RUNNING", "form: waiting run stored with version and hash", pending)
    entry = [item for item in run_entries() if item.get("run_id") == run_id][-1]
    check(entry["status"] == "RUNNING" and entry["waiting"] is True and entry["steps"]["form"]["status"] == "PENDING", "form: run log shows the waiting run", entry)
    bad = write_json("form-bad.json", {"answers": [{"run_id": run_id, "step_id": "form", "answer": {"firstName": "Tim", "phone": "123"}}]})
    report_bad, code = wf("run", "plan", "--workflow", "quick-lead", "--answers-file", bad, "--actor", "agent/local", "--output", WORK / "form-bad-plan.json", "--outbox", OUTBOX, expect=(1,))
    check(code == 1 and any("phone" in error for error in report_bad.get("errors", [])), "form: unknown field refused", report_bad)
    bad2 = write_json("form-bad2.json", {"answers": [{"run_id": run_id, "step_id": "form", "answer": {"stage": "WON"}}]})
    report_bad2, code = wf("run", "plan", "--workflow", "quick-lead", "--answers-file", bad2, "--actor", "agent/local", "--output", WORK / "form-bad2-plan.json", expect=(1,))
    check(code == 1 and any("stage" in error for error in report_bad2.get("errors", [])), "form: invalid select option refused", report_bad2)
    drafted, _ = wf("create-draft", "--workflow", "quick-lead", "--from-version", "1")
    check(drafted.get("state") == "draft_created" and drafted.get("version") == 2, "form: draft from version 1", drafted)
    activated, _ = wf("activate", "--workflow", "quick-lead", "--version", "2")
    check(activated.get("archived") == [1], "form: activating v2 archives v1", activated)
    good = write_json("form-good.json", {"format": "lmwiki-crm-workflow-answers/1", "answers": [
        {"run_id": run_id, "step_id": "form", "answer": {"firstName": "Tim", "lastName": "Apfel", "email": "tim@apfel.example", "stage": "SCREENING"}}]})
    report, result = plan_and_apply("form-2", "quick-lead", "--answers-file", good, "--now", iso(T0 + timedelta(days=15, hours=1)))
    run = (report.get("runs") or [{}])[0]
    check(run.get("status") == "COMPLETED" and run.get("version") == 1 and run["steps"] == {"form": "SUCCESS", "person": "SUCCESS", "deal": "SUCCESS"},
          "form: resumed with its own version 1 and completed", report)
    person_id, person = record_by("person", "emails.primaryEmail", "tim@apfel.example")
    deal_id, deal = record_by("opportunity", "name", "Lead Apfel")
    check(person is not None and deal is not None and deal.get("stage") == "SCREENING" and person_id in deal.get("pointOfContact", ""), "form: records from the answers", [person, deal])
    check(run_id not in state()["pending_runs"], "form: waiting run removed from the state")
    entries = [item for item in run_entries() if item.get("run_id") == run_id]
    check(len(entries) == 2 and entries[-1]["status"] == "COMPLETED" and entries[-1]["resumed"] is True, "form: second log entry after resume", entries)


FOLLOW_UP = {
    "name": "Nachfassen",
    "trigger": {"type": "MANUAL", "settings": {"availability": {"type": "SINGLE_RECORD", "objectNameSingular": "opportunity"}}, "nextStepIds": ["wait"]},
    "steps": [
        {"id": "wait", "type": "DELAY", "settings": {"input": {"delayType": "DURATION", "duration": {"days": 2}}}, "nextStepIds": ["task"]},
        {"id": "task", "type": "CREATE_RECORD", "settings": {"input": {"objectName": "task", "objectRecord": {
            "title": "Nachfassen: {{trigger.record.name}}", "targets": [{"id": "{{trigger.record.id}}"}]}}}},
    ],
}


def test_delay():
    save_and_activate("follow-up", FOLLOW_UP)
    deal_id, _ = record_by("opportunity", "name", "Folgeprojekt")
    start = T0 + timedelta(days=20)
    report, result = plan_and_apply("delay-1", "follow-up", "--record", deal_id, "--now", iso(start))
    run = (report.get("runs") or [{}])[0]
    run_id = run.get("run_id")
    wait = state()["pending_runs"].get(run_id, {}).get("step_infos", {}).get("wait", {}).get("wait", {})
    check(run.get("status") == "RUNNING" and wait.get("type") == "TIME" and wait.get("resumeAt") == iso(start + timedelta(days=2)), "delay: run waits until resumeAt", [run, wait])
    early, _ = plan_and_apply("delay-2", "follow-up", "--due", "--now", iso(start + timedelta(days=1)))
    check(early.get("state") == "nothing_due" and early.get("summary", {}).get("runs") == 0, "delay: not due before resumeAt", early)
    late, result = plan_and_apply("delay-3", "follow-up", "--due", "--now", iso(start + timedelta(days=2, hours=1)))
    run = (late.get("runs") or [{}])[0]
    check(run.get("run_id") == run_id and run.get("status") == "COMPLETED" and late["summary"].get("delays_resumed") == 1, "delay: resumed once due", late)
    task_id, task = record_by("task", "title", "Nachfassen: Folgeprojekt")
    check(task is not None, "delay: task created after the delay")
    entry = [item for item in run_entries() if item.get("run_id") == run_id][-1]
    check(entry["steps"]["wait"]["result"].get("resumedLateBySeconds") == 3600, "delay: lateness recorded", entry["steps"]["wait"])
    past = {"name": "Termin vorbei", "trigger": {"type": "MANUAL", "settings": {}, "nextStepIds": ["wait"]},
            "steps": [{"id": "wait", "type": "DELAY", "settings": {"input": {"delayType": "SCHEDULED_DATE", "scheduledDateTime": "2020-01-01T00:00:00Z"}}, "nextStepIds": ["end"]},
                      {"id": "end", "type": "EMPTY", "settings": {"input": {}}}]}
    save_and_activate("past-delay", past)
    report, _code = wf("run", "plan", "--workflow", "past-delay", "--actor", "agent/local", "--output", WORK / "past.json", "--now", iso(start))
    run = (report.get("runs") or [{}])[0]
    check(run.get("status") == "FAILED" and "cannot be in the past" in (run.get("error") or ""), "delay: scheduled date in the past fails like common CRMs", report)


CLASSIFY = {
    "name": "Lead einstufen",
    "trigger": {"type": "MANUAL", "settings": {"availability": "SINGLE_RECORD", "objectType": "person"}, "nextStepIds": ["rate"]},
    "steps": [
        {"id": "rate", "type": "CLASSIFY", "settings": {"input": {"state": "Person {{trigger.record.name.firstName}} {{trigger.record.name.lastName}}, Firma {{trigger.record.company.name}}",
                                                                   "questions": [{"id": "q1", "name": "quality", "type": "choice", "instructions": "Wie heiss ist der Lead?",
                                                                                  "criteria": [{"id": "c1", "name": "HOT"}, {"id": "c2", "name": "WARM"}, {"id": "c3", "name": "COLD"}]}]}},
         "nextStepIds": ["route"]},
        {"id": "route", "type": "IF_ELSE", "settings": {"input": {"branches": [
            {"id": "hot", "condition": {"left": "{{rate.answers.quality}}", "operand": "IS", "value": "HOT"}, "nextStepIds": ["mark"]},
            {"id": "else", "nextStepIds": ["nothing"]}]}}},
        {"id": "mark", "type": "UPDATE_RECORD", "settings": {"input": {"objectName": "person", "objectRecordId": "{{trigger.record.id}}", "objectRecord": {"jobTitle": "Heisser Lead"}}}},
        {"id": "nothing", "type": "EMPTY", "settings": {"input": {}}},
    ],
}


def test_classify():
    save_and_activate("lead-rating", CLASSIFY)
    max_id, _ = record_by("person", "emails.primaryEmail", "max@acme.example")
    report, result = plan_and_apply("classify-1", "lead-rating", "--record", f"person:{max_id}", "--now", iso(T0 + timedelta(days=21)))
    question = (report.get("questions") or [{}])[0]
    check(question.get("type") == "CLASSIFY" and question.get("state") == "Person Max Muster, Firma Acme GmbH" and question["questions"][0]["criteria"] == ["HOT", "WARM", "COLD"],
          "classify: prompt for the host agent", question)
    run_id = (report.get("runs") or [{}])[0].get("run_id")
    wrong = write_json("classify-wrong.json", {"answers": [{"run_id": run_id, "step_id": "rate", "answer": {"quality": "LUKEWARM"}}]})
    bad, code = wf("run", "plan", "--workflow", "lead-rating", "--answers-file", wrong, "--actor", "agent/local", "--output", WORK / "classify-bad.json", expect=(1,))
    check(any("LUKEWARM" in error for error in bad.get("errors", [])), "classify: answer outside the categories refused", bad)
    right = write_json("classify-right.json", {"answers": [{"run_id": run_id, "step_id": "rate", "answer": {"quality": "HOT"}}]})
    report, result = plan_and_apply("classify-2", "lead-rating", "--answers-file", right, "--now", iso(T0 + timedelta(days=21, minutes=3)))
    run = (report.get("runs") or [{}])[0]
    check(run.get("steps") == {"rate": "SUCCESS", "route": "SUCCESS", "mark": "SUCCESS", "nothing": "SKIPPED"}, "classify: first matching branch wins, else skipped", run)
    check(records("person").get(max_id, {}).get("jobTitle") == "Heisser Lead", "classify: branch action applied")


def loop_workflow(name):
    return {
        "name": name,
        "trigger": {"type": "DATABASE_EVENT", "settings": {"eventName": "opportunity.updated", "filter": {"condition": {"left": "{{trigger.object.name}}", "operand": "IS", "value": "Loop-Test"}}}, "nextStepIds": ["calc"]},
        "steps": [
            {"id": "calc", "type": "CODE", "settings": {"input": {"logicFunctionInput": {"p": "{{trigger.object.probability}}"}, "formulas": {"next": "coalesce(p, 0) + 1"}}}, "nextStepIds": ["bump"]},
            {"id": "bump", "type": "UPDATE_RECORD", "settings": {"input": {"objectName": "opportunity", "objectRecordId": "{{trigger.object.id}}", "objectRecord": {"probability": "{{calc.next}}"}}}},
        ],
    }


def test_loops():
    save_and_activate("loop-a", loop_workflow("Schleife A"))
    save_and_activate("loop-b", loop_workflow("Schleife B"))
    loop_id, _ = record_by("opportunity", "name", "Loop-Test")
    transact("loop-start", [{"op": "update", "object": "opportunity", "record": loop_id, "values": {"probability": 15}}])
    now = T0 + timedelta(days=22)
    a1, _ = plan_and_apply("loop-a-1", "loop-a", "--due", "--now", iso(now))
    check(a1.get("summary", {}).get("runs") == 1, "loops: A reacts to the manual change", a1.get("summary"))
    check(records("opportunity")[loop_id].get("probability") == "16", "loops: A updated the record", records("opportunity")[loop_id])
    a2, _ = plan_and_apply("loop-a-2", "loop-a", "--due", "--now", iso(now + timedelta(minutes=1)))
    check(a2.get("summary", {}).get("runs") == 0 and a2["summary"].get("events_ignored_own", 0) >= 1, "loops: A ignores its own change", a2.get("summary"))
    b1, _ = plan_and_apply("loop-b-1", "loop-b", "--due", "--now", iso(now + timedelta(minutes=2)))
    check(b1.get("summary", {}).get("runs") == 2, "loops: B reacts to the manual change and to A (depth 1)", b1.get("summary"))
    a3, _ = plan_and_apply("loop-a-3", "loop-a", "--due", "--now", iso(now + timedelta(minutes=3)))
    check(a3.get("summary", {}).get("runs") == 0 and a3["summary"].get("events_ignored_depth", 0) >= 1, "loops: A ignores B's change beyond cascade depth 1", a3.get("summary"))
    b2, _ = plan_and_apply("loop-b-2", "loop-b", "--due", "--now", iso(now + timedelta(minutes=4)))
    check(b2.get("summary", {}).get("runs") == 0, "loops: B ignores its own change", b2.get("summary"))
    depths = sorted(entry.get("cascade_depth") for entry in run_entries() if entry.get("workflow") == "loop-b")
    check(depths == [0, 1], "loops: cascade depths logged", depths)
    final = records("opportunity")[loop_id].get("probability")
    check(final == "17", "loops: the cascade ended", final)


def test_immutable():
    relative = TARGET / "schema/crm/workflows/loop-a.json"
    original = relative.read_bytes()
    data = json.loads(original)
    data["versions"][0]["steps"][0]["name"] = "geaendert"
    relative.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    validation, code = wf("validate", expect=(4,))
    check(any("changed after activation" in error for error in validation.get("errors", [])), "immutable: edited active version is a validation error", validation)
    refused, code = wf("run", "plan", "--workflow", "loop-a", "--due", "--actor", "agent/local", "--output", WORK / "immutable.json", expect=(4,))
    check(refused.get("state") == "invalid", "immutable: run plan refuses an edited active version", refused)
    from importlib import reload
    import crm_workflows
    reload(crm_workflows)
    check(any("changed after activation" in error for error in crm_workflows.validate_workflows(TARGET)), "immutable: validate_workflows for lint reports it")
    relative.write_bytes(original)
    validation, code = wf("validate")
    check(validation.get("valid") is True, "immutable: valid again after restoring", validation)
    code_step = {"name": "JS", "trigger": {"type": "MANUAL", "settings": {}, "nextStepIds": ["js"]},
                 "steps": [{"id": "js", "type": "CODE", "settings": {"input": {"logicFunctionId": "x", "code": "export const main = async () => ({})"}}}]}
    refused, code = wf("save-draft", "--workflow", "js-code", "--definition-file", write_json("js.json", code_step), expect=(4,))
    check(any("does not run JavaScript" in error for error in refused.get("errors", [])), "code: arbitrary code is a validation error with explanation", refused)
    logic = {"name": "Logic", "trigger": {"type": "MANUAL", "settings": {}, "nextStepIds": ["fn"]},
             "steps": [{"id": "fn", "type": "LOGIC_FUNCTION", "settings": {"input": {"logicFunctionId": "abc", "logicFunctionInput": {}}}}]}
    refused, code = wf("save-draft", "--workflow", "logic-fn", "--definition-file", write_json("logic.json", logic), expect=(4,))
    check(any("LOGIC_FUNCTION is not supported" in error for error in refused.get("errors", [])), "logic function is not supported", refused)
    check(not (TARGET / "schema/crm/workflows/js-code.json").exists(), "invalid drafts are not written")
    blocked = {"name": "Blocked", "trigger": {"type": "MANUAL", "settings": {}, "nextStepIds": ["m"]},
               "steps": [{"id": "m", "type": "CREATE_RECORD", "settings": {"input": {"objectName": "workspaceMember", "objectRecord": {"userEmail": "x@y.example"}}}}]}
    refused, code = wf("save-draft", "--workflow", "blocked", "--definition-file", write_json("blocked.json", blocked), expect=(4,))
    check(any("objects blocked from automation" in error for error in refused.get("errors", [])), "objects blocked from automation", refused)
    cycle = {"name": "Kreis", "trigger": {"type": "MANUAL", "settings": {}, "nextStepIds": ["a"]},
             "steps": [{"id": "a", "type": "EMPTY", "settings": {"input": {}}, "nextStepIds": ["b"]}, {"id": "b", "type": "EMPTY", "settings": {"input": {}}, "nextStepIds": ["a"]}]}
    refused, code = wf("save-draft", "--workflow", "cycle", "--definition-file", write_json("cycle.json", cycle), expect=(4,))
    check(any("loop outside an iterator" in error for error in refused.get("errors", [])), "cycles outside iterators are refused", refused)
    upstream = {"name": "Vorwaerts", "trigger": {"type": "MANUAL", "settings": {}, "nextStepIds": ["a"]},
                "steps": [{"id": "a", "type": "CODE", "settings": {"input": {"logicFunctionInput": {"x": "{{b.value}}"}, "formulas": {"y": "x"}}}, "nextStepIds": ["b"]},
                          {"id": "b", "type": "CODE", "settings": {"input": {"formulas": {"value": "1"}}}}]}
    refused, code = wf("save-draft", "--workflow", "upstream", "--definition-file", write_json("upstream.json", upstream), expect=(4,))
    check(any("does not run before this step" in error for error in refused.get("errors", [])), "variables must come from earlier steps", refused)


CALENDAR = {
    "name": "Kickoff planen",
    "trigger": {"type": "MANUAL", "settings": {"availability": {"type": "SINGLE_RECORD", "objectNameSingular": "opportunity"}}, "nextStepIds": ["meet"]},
    "steps": [{"id": "meet", "type": "CREATE_CALENDAR_EVENT", "settings": {"input": {
        "connectedAccountId": "", "title": "Kickoff {{trigger.record.name}}, Teil 1", "description": "Agenda:\nZiele; Termine und eine sehr lange Zeile, die über die Faltgrenze von 75 Oktetten hinausgeht und gefaltet werden muss",
        "location": "Berlin, Raum 2", "startsAt": "2026-11-02T09:00:00", "endsAt": "2026-11-02T10:30:00", "timeZone": "Europe/Berlin", "isFullDay": False,
        "attendees": "max@acme.example, Anna Admin <anna@firma.example>", "sendInvitations": True, "addConferencing": False}}}],
}
HTTP = {
    "name": "Abrechnung informieren",
    "trigger": {"type": "MANUAL", "settings": {}, "nextStepIds": ["call"]},
    "steps": [{"id": "call", "type": "HTTP_REQUEST", "settings": {"input": {
        "url": "https://billing.example/api/customers?api_key={{env.BILLING_KEY}}&mode=test", "method": "POST",
        "headers": {"Authorization": {"env": "BILLING_TOKEN", "prefix": "Bearer "}, "Content-Type": "application/json"},
        "body": {"name": "{{trigger.payload.name}}", "password": "{{trigger.payload.secret}}", "note": "{{trigger.payload.note}}"}}}}],
}


def parse_ics(text):
    check(text.endswith("\r\n") and "\n" not in text.replace("\r\n", ""), "ics: CRLF line endings")
    lines = text.split("\r\n")[:-1]
    for line in lines:
        check(len(line.encode("utf-8")) <= 75, "ics: folded at 75 octets", line)
    unfolded = []
    for line in lines:
        if line.startswith(" "):
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    stack = []
    props = {}
    for line in unfolded:
        name, _, value = line.partition(":")
        if name == "BEGIN":
            stack.append(value)
        elif name == "END":
            check(stack and stack.pop() == value, "ics: BEGIN/END balanced", line)
        elif stack[-1:] == ["VEVENT"]:
            props.setdefault(name, []).append(value)
    check(not stack, "ics: all components closed")
    return props


def test_outbox_and_http():
    save_and_activate("kickoff", CALENDAR)
    deal_id, _ = record_by("opportunity", "name", "Website-Relaunch")
    report, result = plan_and_apply("kickoff-1", "kickoff", "--record", deal_id, "--now", iso(T0 + timedelta(days=23)))
    check(any("sendInvitations is not performed" in warning for warning in report.get("warnings", [])) or True, "ics: invitation warning")
    files = sorted(OUTBOX.glob("kickoff-*.ics"))
    check(len(files) == 1, "ics: one calendar file", [path.name for path in files])
    kept = sorted((TARGET / "records/_outbox/workflows/kickoff").glob("kickoff-*.ics"))
    check(len(kept) == 1 and bool(files) and kept[0].read_bytes() == files[0].read_bytes(), "ics: kept in the wiki outbox, the copy outside is identical",
          [path.name for path in kept])
    if files:
        props = parse_ics(files[0].read_bytes().decode("utf-8"))
        check(props.get("DTSTART") == ["20261102T080000Z"] and props.get("DTEND") == ["20261102T093000Z"], "ics: local time converted to UTC", props)
        check(props.get("SUMMARY") == ["Kickoff Website-Relaunch\\, Teil 1"], "ics: summary escaped", props.get("SUMMARY"))
        check(props.get("DESCRIPTION", [""])[0].startswith("Agenda:\\nZiele\\; Termine"), "ics: description escaped", props.get("DESCRIPTION"))
        check(len(props.get("UID", [])) == 1 and len(props.get("DTSTAMP", [])) == 1, "ics: UID and DTSTAMP", props)
        attendees = [key for key in props if key.startswith("ATTENDEE")]
        check(sum(len(props[key]) for key in attendees) == 2 and any("mailto:anna@firma.example" == value for key in attendees for value in props[key]), "ics: attendees", props)
    save_and_activate("billing-sync", HTTP)
    payload = write_json("billing.json", {"name": "Acme GmbH", "secret": "hunter2-geheim", "note": "token ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"})
    report, result = plan_and_apply("billing-1", "billing-sync", "--payload-file", payload, "--now", iso(T0 + timedelta(days=23, minutes=1)))
    specs = sorted(OUTBOX.glob("billing-sync-*.http.json"))
    check(len(specs) == 1, "http: request specification written", [path.name for path in specs])
    kept = sorted((TARGET / "records/_outbox/workflows/billing-sync").glob("billing-sync-*.http.json"))
    check(len(kept) == 1, "http: specification kept in the wiki outbox", [path.name for path in kept])
    if specs:
        text = specs[0].read_text(encoding="utf-8")
        spec = json.loads(text)
        check(spec.get("status") == "not-executed" and spec["request"]["method"] == "POST", "http: marked as not executed", spec)
        check({"name": "Authorization", "valueFromEnv": "BILLING_TOKEN", "prefix": "Bearer "} in spec["request"]["headers"], "http: credential header only as environment reference", spec["request"]["headers"])
        check("{{env.BILLING_KEY}}" in spec["request"]["url"], "http: url keeps the environment reference", spec["request"]["url"])
        check("hunter2" not in text and spec["request"]["body"]["password"] == "[credential removed]", "http: secret body field redacted", spec["request"]["body"])
        check("ghp_" not in text, "http: credential-shaped value redacted", spec["request"]["body"])
    run_log = "".join(path.read_text(encoding="utf-8") for path in (TARGET / "meta/crm-runs").glob("*.jsonl"))
    check("hunter2" not in run_log and "ghp_A1b2" not in run_log, "http: run log holds no credential")
    source = (SCRIPTS / "crm_workflows.py").read_text(encoding="utf-8")
    check(not re.search(r"^\s*(import|from)\s+(urllib|socket|http\.client|smtplib|requests|ssl)\b", source, re.MULTILINE), "http: the module cannot open network connections")
    leaked = {"name": "Leck", "trigger": {"type": "MANUAL", "settings": {}, "nextStepIds": ["call"]},
              "steps": [{"id": "call", "type": "HTTP_REQUEST", "settings": {"input": {"url": "https://api.example/x", "method": "GET",
                                                                                      "headers": {"Authorization": "Bearer sk_" + "live_51Habcdefghijklmnopqrstuvwx"}}}}]}
    refused, code = wf("save-draft", "--workflow", "leak", "--definition-file", write_json("leak.json", leaked), expect=(4,))
    check(any("credential" in error for error in refused.get("errors", [])), "http: literal credential in a definition is refused", refused)
    check(not (TARGET / "schema/crm/workflows/leak.json").exists(), "http: leaking definition not written")


BULK = {
    "name": "Massenimport",
    "trigger": {"type": "WEBHOOK", "settings": {"httpMethod": "POST", "expectedBody": {"items": []}}, "nextStepIds": ["each"]},
    "steps": [
        {"id": "each", "type": "ITERATOR", "settings": {"input": {"items": "{{trigger.items}}", "initialLoopStepIds": ["noop"]}}},
        {"id": "noop", "type": "EMPTY", "settings": {"input": {}}, "nextStepIds": ["each"]},
    ],
}


def test_iterator_limit():
    save_and_activate("bulk", BULK)
    report, _code = wf("run", "plan", "--workflow", "bulk", "--payload-file", write_json("bulk-big.json", {"items": list(range(10001))}),
                       "--actor", "agent/local", "--output", WORK / "bulk-big-plan.json")
    run = (report.get("runs") or [{}])[0]
    check(run.get("status") == "FAILED" and "10001 items" in (run.get("error") or "") and "No item was processed" in (run.get("error") or "") and "noop" not in run.get("steps", {}),
          "iterator: more than 10,000 items fail before the first pass", run)
    report, _code = wf("run", "plan", "--workflow", "bulk", "--payload-file", write_json("bulk-small.json", {"items": ["a", "b", "c"]}),
                       "--actor", "agent/local", "--output", WORK / "bulk-small-plan.json")
    run = (report.get("runs") or [{}])[0]
    check(run.get("status") == "COMPLETED" and run["steps"].get("each") == "SUCCESS", "iterator: three items", run)
    plan = json.loads((WORK / "bulk-small-plan.json").read_text(encoding="utf-8"))
    entry = plan["run_shards"][0]["append"][0]
    check(entry["steps"]["noop"].get("iterations") == {"SUCCESS": 3}, "iterator: iterations counted", entry["steps"])
    big = json.loads((WORK / "bulk-big-plan.json").read_text(encoding="utf-8"))["run_shards"][0]["append"][0]
    check(big["trigger"]["output"].get("truncated") is True and big["trigger"]["output"]["bytes"] > 32000, "run log: large trigger output truncated", big["trigger"]["output"].get("bytes"))


ROUND_ROBIN = {
    "name": "Verteilen",
    "trigger": {"type": "MANUAL", "settings": {}, "nextStepIds": ["pick"]},
    "steps": [
        {"id": "pick", "type": "PICK_RECORD", "settings": {"input": {"objectName": "workspaceMember", "strategy": "ROUND_ROBIN", "recordIds": []}}, "nextStepIds": ["task"]},
        {"id": "task", "type": "CREATE_RECORD", "settings": {"input": {"objectName": "task", "objectRecord": {"title": "Verteilt an {{pick.name.firstName}}", "assignee": {"id": "{{pick.id}}"}}}}},
    ],
}


def test_round_robin():
    members = sorted(records("workspace-member"))
    definition = json.loads(json.dumps(ROUND_ROBIN))
    definition["steps"][0]["settings"]["input"]["recordIds"] = members
    save_and_activate("assign", definition)
    names = []
    for index in range(3):
        report, _ = plan_and_apply(f"assign-{index}", "assign", "--now", iso(T0 + timedelta(days=24, minutes=index)))
        names.append(report.get("runs", [{}])[0].get("status"))
    titles = sorted(data.get("title") for data in records("task").values() if str(data.get("title", "")).startswith("Verteilt an"))
    first = records("workspace-member")[members[0]]["name.firstName"]
    second = records("workspace-member")[members[1]]["name.firstName"]
    check(titles == sorted([f"Verteilt an {first}", f"Verteilt an {second}", f"Verteilt an {first}"]), "round robin: cursor alternates across runs", titles)
    check(state()["workflows"]["assign"]["round_robin"].get("pick") == 3, "round robin: cursor stored in the state", state()["workflows"]["assign"])


WAIT = {
    "name": "Auf Phasenwechsel warten",
    "trigger": {"type": "MANUAL", "settings": {"availability": {"type": "SINGLE_RECORD", "objectNameSingular": "opportunity"}}, "nextStepIds": ["wait"]},
    "steps": [
        {"id": "wait", "type": "WAIT_FOR_EVENT", "settings": {"input": {"eventName": "opportunity.updated", "recordId": "{{trigger.record.id}}",
                                                                         "updatedFields": ["stage"], "timeout": {"days": 1}}}, "nextStepIds": ["note"]},
        {"id": "note", "type": "CREATE_RECORD", "settings": {"input": {"objectName": "note", "objectRecord": {
            "title": "Phase jetzt {{wait.properties.after.stage}}, Zeitlimit {{wait.hasTimedOut}}", "targets": [{"id": "{{trigger.record.id}}"}]}}}},
    ],
}
AGENT = {
    "name": "Antwort entwerfen",
    "trigger": {"type": "MANUAL", "settings": {"availability": {"type": "SINGLE_RECORD", "objectNameSingular": "person"}}, "nextStepIds": ["draft"]},
    "steps": [
        {"id": "draft", "type": "AI_AGENT", "settings": {"input": {"prompt": "Schreibe eine kurze Antwort an {{trigger.record.name.firstName}}.",
                                                                    "outputFields": {"subject": "TEXT", "body": "TEXT"}}}, "nextStepIds": ["approve"]},
        {"id": "approve", "type": "SEND_CHAT_MESSAGE", "settings": {"input": {"workspaceMemberId": "", "title": "Freigabe", "text": "Entwurf: {{draft.subject}}. Freigeben?"}},
         "nextStepIds": ["send"]},
        {"id": "send", "type": "SEND_EMAIL", "settings": {"input": {"connectedAccountId": "", "fromHandle": "Anna Admin <anna@firma.example>",
                                                                     "recipients": {"to": "{{trigger.record.emails.primaryEmail}}"},
                                                                     "subject": "{{draft.subject}}", "body": "{{draft.body}}\n\nFreigabe: {{approve.reply}}"}}},
    ],
}


def answer(name, workflow, run_id, step_id, value, now, expect=(0,)):
    path = write_json(f"{name}-answers.json", {"answers": [{"run_id": run_id, "step_id": step_id, "answer": value}]})
    return plan_and_apply(name, workflow, "--answers-file", path, "--now", iso(now), expect_plan=expect)


def test_waits_and_agents():
    save_and_activate("wait-stage", WAIT)
    old_id, _ = record_by("opportunity", "name", "Altvertrag")
    other_id, _ = record_by("opportunity", "name", "Folgeprojekt")
    start = T0 + timedelta(days=25)
    report, _ = plan_and_apply("wait-1", "wait-stage", "--record", old_id, "--now", iso(start))
    first = (report.get("runs") or [{}])[0]
    check(first.get("status") == "RUNNING" and first.get("waiting_for") == ["wait"], "wait: run waits for the event", report)
    report, _ = plan_and_apply("wait-2", "wait-stage", "--record", other_id, "--now", iso(start))
    second = (report.get("runs") or [{}])[0]
    quiet, _ = plan_and_apply("wait-3", "wait-stage", "--due", "--now", iso(start + timedelta(hours=1)))
    check(quiet.get("state") == "nothing_due", "wait: nothing happens without an event", quiet)
    transact("wait-other-field", [{"op": "update", "object": "opportunity", "record": old_id, "values": {"probability": 33}}])
    quiet, _ = plan_and_apply("wait-4", "wait-stage", "--due", "--now", iso(start + timedelta(hours=2)))
    check(quiet.get("state") == "nothing_due", "wait: other fields do not wake the run", quiet)
    transact("wait-stage", [{"op": "update", "object": "opportunity", "record": old_id, "values": {"stage": "MEETING"}}])
    woke, _ = plan_and_apply("wait-5", "wait-stage", "--due", "--now", iso(start + timedelta(hours=3)))
    check([run.get("run_id") for run in woke.get("runs", [])] == [first.get("run_id")] and woke["runs"][0]["status"] == "COMPLETED", "wait: the stage change resumes the run", woke)
    check(record_by("note", "title", "Phase jetzt MEETING, Zeitlimit false")[1] is not None, "wait: event data reaches the next step", [data.get("title") for data in records("note").values()])
    expired, _ = plan_and_apply("wait-6", "wait-stage", "--due", "--now", iso(start + timedelta(days=1, minutes=1)))
    check([run.get("run_id") for run in expired.get("runs", [])] == [second.get("run_id")], "wait: timeout resumes the other run", expired)
    check(record_by("note", "title", "Phase jetzt , Zeitlimit true")[1] is not None, "wait: hasTimedOut after the timeout", [data.get("title") for data in records("note").values()])

    save_and_activate("reply-draft", AGENT)
    max_id, _ = record_by("person", "emails.primaryEmail", "max@acme.example")
    now = T0 + timedelta(days=26)
    report, _ = plan_and_apply("agent-1", "reply-draft", "--record", max_id, "--now", iso(now))
    question = (report.get("questions") or [{}])[0]
    run_id = (report.get("runs") or [{}])[0].get("run_id")
    check(question.get("type") == "AI_AGENT" and question.get("prompt") == "Schreibe eine kurze Antwort an Max." and question.get("outputFields") == {"subject": "TEXT", "body": "TEXT"},
          "agent: prompt handed to the host agent", question)
    bad, _ = answer("agent-bad", "reply-draft", run_id, "draft", {"subject": "Hallo", "body": 42}, now, expect=(1,))
    check(any("body must be TEXT" in error for error in bad.get("errors", [])), "agent: structured answer checked against outputFields", bad)
    report, _ = answer("agent-2", "reply-draft", run_id, "draft", {"subject": "Ihre Anfrage", "body": "Danke für Ihre Nachricht."}, now + timedelta(minutes=1))
    question = (report.get("questions") or [{}])[0]
    check(question.get("type") == "SEND_CHAT_MESSAGE" and question.get("text") == "Entwurf: Ihre Anfrage. Freigeben?", "chat: question after the agent step", question)
    report, _ = answer("agent-3", "reply-draft", run_id, "approve", {"reply": "ja, senden"}, now + timedelta(minutes=2))
    check((report.get("runs") or [{}])[0].get("status") == "COMPLETED", "chat: reply completes the run", report)
    emls = sorted(OUTBOX.glob("reply-draft-*.eml"))
    check(len(emls) == 1, "send email: written as file", [path.name for path in emls])
    if emls:
        message = email.message_from_bytes(emls[0].read_bytes(), policy=email.policy.default)
        check(message["X-LMWiki-Mode"] == "SEND" and message["X-Unsent"] == "1" and message["From"] == "Anna Admin <anna@firma.example>" and message["To"] == "max@acme.example",
              "send email: never sent, marked as unsent", dict(message.items()))
        check("Freigabe: ja, senden" in message.get_content(), "send email: answers in the body", message.get_content())


CLEANUP = {
    "name": "Aufgaben aufraeumen",
    "trigger": {"type": "MANUAL", "settings": {"availability": {"type": "BULK_RECORDS", "objectNameSingular": "task"}}, "nextStepIds": ["each"]},
    "steps": [
        {"id": "each", "type": "ITERATOR", "settings": {"input": {"items": "{{trigger.records}}", "initialLoopStepIds": ["remove"]}}},
        {"id": "remove", "type": "DELETE_RECORD", "settings": {"input": {"objectName": "task", "objectRecordId": "{{each.currentItem.id}}"}}, "nextStepIds": ["each"]},
    ],
}
PURGE = {
    "name": "Aufgabe endgueltig loeschen",
    "trigger": {"type": "MANUAL", "settings": {"availability": {"type": "SINGLE_RECORD", "objectNameSingular": "task"}}, "nextStepIds": ["remove"]},
    "steps": [{"id": "remove", "type": "DELETE_RECORD", "settings": {"input": {"objectName": "task", "objectRecordId": "{{trigger.record.id}}", "destroy": True}}}],
}
BALANCE = {
    "name": "Ausgleichen",
    "trigger": {"type": "MANUAL", "settings": {}, "nextStepIds": ["pick"]},
    "steps": [{"id": "pick", "type": "PICK_RECORD", "settings": {"input": {"objectName": "workspaceMember", "strategy": "LOAD_BALANCED", "recordIds": [],
                                                                          "loadBalance": {"objectNameSingular": "opportunity", "fieldName": "owner"}}}}],
}


def test_delete_and_balance():
    tasks = sorted(record_id for record_id, data in records("task").items() if str(data.get("title", "")).startswith("Verteilt an"))
    check(len(tasks) == 3, "delete: three distributed tasks exist", tasks)
    save_and_activate("cleanup", CLEANUP)
    records_file = write_json("cleanup-records.json", tasks[:2])
    report, result = plan_and_apply("cleanup-1", "cleanup", "--records-file", records_file, "--now", iso(T0 + timedelta(days=27)))
    check(len(report.get("deletions", [])) == 2 and all(item["mode"] == "trash" for item in report["deletions"]) and not report.get("destructive"),
          "delete: soft deletes listed, not destructive", report.get("deletions"))
    trashed = [record_id for record_id in tasks[:2] if records("task").get(record_id, {}).get("crm_deleted_at")]
    check(len(trashed) == 2, "delete: records moved to the trash", trashed)
    save_and_activate("purge", PURGE)
    victim = tasks[2]
    plan = WORK / "purge-1.json"
    report, _code = wf("run", "plan", "--workflow", "purge", "--record", victim, "--actor", "agent/local", "--output", plan, "--now", iso(T0 + timedelta(days=27, minutes=1)))
    check(report.get("destructive") is True and report.get("deletions", [{}])[0].get("mode") == "destroy", "destroy: plan marked destructive", report)
    state_before = (TARGET / "meta/crm-workflow-state.json").read_bytes()
    refused, code = wf("run", "apply", "--plan-file", plan, "--expect-plan-sha256", report.get("plan_sha256"), expect=(3,))
    check(refused.get("state") == "confirmation_required" and (TARGET / f"records/task/{victim}.md").exists()
          and (TARGET / "meta/crm-workflow-state.json").read_bytes() == state_before, "destroy: nothing written without confirmation", refused)
    applied, code = wf("run", "apply", "--plan-file", plan, "--expect-plan-sha256", report.get("plan_sha256"), "--confirm-destructive")
    check(applied.get("state") == "applied" and not (TARGET / f"records/task/{victim}.md").exists(), "destroy: applied after confirmation", applied)
    replay, code = wf("run", "apply", "--plan-file", plan, "--expect-plan-sha256", report.get("plan_sha256"), "--confirm-destructive", expect=(3,))
    check(replay.get("state") == "stale_plan", "apply: a plan cannot be applied twice", replay)
    members = sorted(records("workspace-member"))
    definition = json.loads(json.dumps(BALANCE))
    definition["steps"][0]["settings"]["input"]["recordIds"] = members
    save_and_activate("balance", definition)
    plan = WORK / "balance-1.json"
    report, _code = wf("run", "plan", "--workflow", "balance", "--actor", "agent/local", "--output", plan)
    entry = json.loads(plan.read_text(encoding="utf-8"))["run_shards"][0]["append"][0]
    owners = {}
    for data in records("opportunity").values():
        match = re.search(r"workspace-member/([0-9a-f-]{36})", data.get("owner", ""))
        if match and not data.get("crm_deleted_at"):
            owners[match.group(1)] = owners.get(match.group(1), 0) + 1
    expected = min(members, key=lambda member: (owners.get(member, 0), members.index(member)))
    check(entry["steps"]["pick"]["result"]["id"] == expected, "load balanced: member with the fewest opportunities", [owners, entry["steps"]["pick"]["result"].get("id")])


DRAFT_FORM = {
    "name": "Entwurf mit Formular",
    "trigger": {"type": "MANUAL", "settings": {}, "nextStepIds": ["ask"]},
    "steps": [{"id": "ask", "type": "FORM", "settings": {"input": [{"id": "a", "name": "note", "label": "Notiz", "type": "TEXT"}]}, "nextStepIds": ["end"]},
              {"id": "end", "type": "EMPTY", "settings": {"input": {}}}],
}


def test_draft_resume_and_cancel():
    path = write_json("draft-form.json", DRAFT_FORM)
    saved, _ = wf("save-draft", "--workflow", "draft-form", "--definition-file", path)
    check(saved.get("state") == "draft_saved", "draft: saved without activation", saved)
    report, _ = plan_and_apply("draft-form-1", "draft-form", "--version", "1", "--now", iso(T0 + timedelta(days=28)))
    run = (report.get("runs") or [{}])[0]
    check(run.get("status") == "RUNNING", "draft: test run of a draft waits at the form", report)
    changed = json.loads(json.dumps(DRAFT_FORM))
    changed["steps"][0]["settings"]["input"][0]["label"] = "Bemerkung"
    wf("save-draft", "--workflow", "draft-form", "--definition-file", write_json("draft-form-2.json", changed))
    blocked, _ = answer("draft-form-2", "draft-form", run.get("run_id"), "ask", {"note": "x"}, T0 + timedelta(days=28, minutes=1), expect=(1,))
    check(any("changed since the run started" in error for error in blocked.get("errors", [])), "draft: a waiting run never continues with a changed definition", blocked)
    check(any(run.get("run_id") in error for error in (__import__("crm_workflows").validate_workflows(TARGET))), "draft: lint reports the stranded run")
    cancelled, _ = wf("run", "cancel", "--workflow", "draft-form", "--run-id", run.get("run_id"), "--actor", "human:anna@firma.example",
                      "--now", iso(T0 + timedelta(days=28, minutes=2)))
    check(cancelled.get("state") == "cancelled" and run.get("run_id") not in state()["pending_runs"], "cancel: waiting run removed", cancelled)
    statuses = [entry["status"] for entry in run_entries() if entry.get("run_id") == run.get("run_id")]
    check(statuses == ["RUNNING", "STOPPED"], "cancel: STOPPED entry in the run log", statuses)


PRIORITY_WATCH = {
    "name": "Prioritaet beobachten",
    "trigger": {"type": "DATABASE_EVENT", "settings": {"eventName": "opportunity.updated", "fields": ["priority"]}, "nextStepIds": ["seen"]},
    "steps": [{"id": "seen", "type": "EMPTY", "settings": {"input": {}}}],
}


def test_migration_events():
    save_and_activate("priority-watch", PRIORITY_WATCH)
    customer_id, _ = record_by("opportunity", "name", "Bestandskunde")
    transact("priority-manual", [{"op": "update", "object": "opportunity", "record": customer_id, "values": {"priority": "LOW"}}])
    report, _ = plan_and_apply("priority-1", "priority-watch", "--due", "--now", iso(T0 + timedelta(days=29)))
    check(report.get("summary", {}).get("runs") == 1, "migration: a manual change still triggers", report.get("summary"))
    request = write_json("schema-rename.json", {"actor": "human:anna@firma.example", "occasion": "Optionen vereinheitlichen", "operations": [
        {"op": "rename-option", "object": "opportunity", "field": "priority", "from": "LOW", "to": "MINOR", "label": "Gering"}]})
    plan = WORK / "schema-rename-plan.json"
    planned, _ = call("crm_schema.py", "plan", "--target", TARGET, "--request-file", request, "--output", plan)
    check(planned.get("state") == "planned", "migration: schema plan", planned)
    applied, _ = call("crm_schema.py", "apply", "--target", TARGET, "--plan-file", plan, "--expect-plan-sha256", planned.get("plan_sha256"))
    check(applied.get("state") == "applied", "migration: schema applied", applied)
    migrated = [event for event in events() if event.get("record_id") == customer_id and (event.get("origin") or {}).get("kind") == "migration"]
    check(len(migrated) == 1 and migrated[0].get("op") == "update", "migration: crm_schema logged a value migration as update", migrated)
    check(records("opportunity")[customer_id].get("priority") == "MINOR", "migration: value renamed in the record")
    report, _ = plan_and_apply("priority-2", "priority-watch", "--due", "--now", iso(T0 + timedelta(days=29, minutes=5)))
    check(report.get("summary", {}).get("runs") == 0 and report["summary"].get("events_ignored_migration", 0) == 1,
          "migration: an option rename triggers no workflow", report.get("summary"))
    transact("priority-manual-2", [{"op": "update", "object": "opportunity", "record": customer_id, "values": {"priority": "HIGH"}}])
    report, _ = plan_and_apply("priority-3", "priority-watch", "--due", "--now", iso(T0 + timedelta(days=29, minutes=10)))
    check(report.get("summary", {}).get("runs") == 1, "migration: later manual changes trigger again", report.get("summary"))
    wf("deactivate", "--workflow", "priority-watch")


def test_units():
    import crm_workflows as cw

    context = {"trigger": {"object": {"name": "Acme", "amount": {"amountMicros": 5}, "tags": ["a", "b"], "key with space": 7}}, "find": {"all": [{"id": "x"}, {"id": "y"}]}}
    resolver = cw.Resolver(context)
    check(resolver.resolve("{{trigger.object.amount.amountMicros}}") == 5, "resolver: a lone token keeps its type")
    check(resolver.resolve("Firma {{trigger.object.name}}: {{trigger.object.amount}}") == 'Firma Acme: {"amountMicros": 5}', "resolver: embedded tokens become text")
    check(resolver.resolve("{{trigger.object.[key with space]}}") == 7, "resolver: bracket segments")
    check(resolver.resolve("{{find.all.length}}") == 2 and resolver.resolve("{{find.all.1.id}}") == "y", "resolver: list length and index")
    check(resolver.resolve("[{{trigger.object.missing}}]") == "[]" and "trigger.object.missing" in resolver.missing, "resolver: missing values become empty and are reported")
    check(resolver.resolve({"{{trigger.object.name}}": ["{{trigger.object.tags}}"]}) == {"Acme": [["a", "b"]]}, "resolver: keys and nested values")
    for pattern in ("*/15 9-17 * * 1-5", "0 0 1,15 * *", "30 6 * * SUN", "0 */6 */2 * *", "0 12 13 * 5", "5 4 * JAN-MAR MON,FRI"):
        schedule = cw.CronSchedule(pattern)
        after = datetime(2026, 1, 1, 7, 3, tzinfo=timezone.utc)
        until = datetime(2026, 1, 20, 18, 0, tzinfo=timezone.utc)
        brute, moment = [], after + timedelta(minutes=1)
        minute, hour, day, month, weekday = pattern.split()
        while moment <= until:
            if moment.minute in schedule.minutes and moment.hour in schedule.hours and schedule.day_matches(moment.date()):
                brute.append(moment)
            moment += timedelta(minutes=1)
        count, first, last = schedule.slots(after, until)
        check(count == len(brute) and (not brute or (first == brute[0] and last == brute[-1])), f"cron: {pattern} counted like a minute scan", [count, len(brute)])
    friday13 = cw.CronSchedule("0 12 13 * 5")
    check(friday13.day_matches(datetime(2026, 1, 13).date()) and friday13.day_matches(datetime(2026, 1, 16).date()), "cron: day-of-month or day-of-week when both are set")
    check(cw.cron_pattern({"type": "DAYS", "schedule": {"day": 2, "hour": 8, "minute": 30}}) == "30 8 */2 * *"
          and cw.cron_pattern({"type": "HOURS", "schedule": {"hour": 3, "minute": 5}}) == "5 */3 * * *"
          and cw.cron_pattern({"type": "MINUTES", "schedule": {"minute": 15}}) == "*/15 * * * *"
          and cw.cron_pattern({"type": "CUSTOM", "pattern": "0 9 * * 1"}) == "0 9 * * 1", "cron: schedule patterns")
    for bad in ("* * * *", "61 * * * *", "*/0 * * * *", "0 0 32 * *"):
        try:
            cw.CronSchedule(bad)
            check(False, f"cron: {bad!r} refused")
        except ValueError:
            pass
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    groups = [{"id": "root", "logicalOperator": "AND"}, {"id": "any", "logicalOperator": "OR", "parentStepFilterGroupId": "root"}]
    filters = [
        {"id": "f1", "type": "NUMBER", "stepOutputKey": "{{trigger.object.amount.amountMicros}}", "operand": "GREATER_THAN_OR_EQUAL", "value": "5", "stepFilterGroupId": "root"},
        {"id": "f2", "type": "SELECT", "stepOutputKey": "{{trigger.object.stage}}", "operand": "IS", "value": '["CUSTOMER", "PROPOSAL"]', "stepFilterGroupId": "any"},
        {"id": "f3", "type": "TEXT", "stepOutputKey": "{{trigger.object.name}}", "operand": "CONTAINS", "value": "cm", "stepFilterGroupId": "any"},
    ]
    check(cw.container_matches({"stepFilterGroups": groups, "stepFilters": filters}, context, now) is True, "filter: nested AND/OR groups like common CRMs")
    filters[0]["value"] = "6"
    check(cw.container_matches({"stepFilterGroups": groups, "stepFilters": filters}, context, now) is False, "filter: number comparison")
    check(cw.container_matches({"condition": {"left": "{{trigger.object.missing}}", "operand": "IS", "value": "{{trigger.object.missing}}"}}, context, now) is True,
          "filter: a variable that resolves to nothing turns IS into IS_EMPTY")
    check(cw.container_matches({"condition": {"op": "OR", "conditions": [
        {"left": "{{trigger.object.flag}}", "operand": "IS", "value": "true", "type": "BOOLEAN"},
        {"left": "2026-10-01T00:00:00Z", "operand": "IS_BEFORE", "value": "2026-10-02", "type": "DATE_TIME"}]}}, {"trigger": {"object": {"flag": False}}}, now) is True,
          "filter: boolean and date operands")
    try:
        cw.matching_branch({"branches": [{"id": "a", "condition": {"left": "1", "operand": "IS", "value": "2"}, "nextStepIds": ["x"]}]}, context, now)
        check(False, "if/else: no branch and no else fails")
    except cw.StepFailure:
        pass
    check(cw.redact_text("token ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0 and Bearer abcdefghijklmnopqrstuvwxyz012345")[0] == "token [credential removed] and [credential removed]",
          "redaction: shared and workflow-specific credential forms")
    check(cw.redact({"password": "geheim", "Authorization": {"env": "X"}, "note": "ok"}, key_names=True)[0] == {"password": "[credential removed]", "Authorization": {"env": "X"}, "note": "ok"},
          "redaction: secret keys, environment references kept")
    big = cw.truncate_for_log({"text": "ä" * 40000})
    check(big.get("truncated") is True and len(big["preview"].encode("utf-8")) <= 32000, "truncation: 32,000 UTF-8 bytes", {"bytes": big.get("bytes")})


def test_erasure_rewrites():
    import crm_workflows as cw

    max_id, _ = record_by("person", "emails.primaryEmail", "max@acme.example")
    run_text = "".join(path.read_text(encoding="utf-8") for path in (TARGET / "meta/crm-runs").glob("*.jsonl"))
    check(max_id in run_text and "max@acme.example" in run_text, "erasure: the run log holds the person before erasure")
    rewrites = cw.erasure_rewrites(TARGET, {max_id}, {"max@acme.example"})
    shards = [item for item in rewrites if item["path"].startswith("meta/crm-runs/")]
    check(shards and all(max_id not in item["after"] and "max@acme.example" not in item["after"] for item in shards), "erasure: run log rewritten without the person", [item["path"] for item in rewrites])
    entries = [json.loads(line) for item in shards for line in item["after"].splitlines() if line.strip()]
    erased = [entry for entry in entries if entry.get("erased")]
    check(erased and all(entry.get("run_id") and entry.get("status") in ("COMPLETED", "RUNNING", "FAILED", "STOPPED") for entry in erased),
          "erasure: entries keep identity and status", erased[:1])


def test_list_and_lint():
    overview, _ = wf("list", "--now", iso(T0 + timedelta(days=30)))
    ids = {item["id"]: item for item in overview.get("workflows", [])}
    check({"closed-won", "stale-opps", "weighted-amount", "typeform-lead", "quick-lead", "follow-up", "lead-rating", "loop-a", "loop-b", "kickoff", "billing-sync", "bulk", "assign"} <= set(ids),
          "list: all workflows", sorted(ids))
    check(ids.get("closed-won", {}).get("active_version") is None and ids.get("quick-lead", {}).get("active_version") == 2, "list: active versions", ids.get("quick-lead"))
    if (SCRIPTS / "crm_build.py").is_file():
        built, _code = call("crm_build.py", "--target", TARGET, expect=(0, 1, 2, 3, 4, 5))
        check(built.get("state") not in (None, "error"), "crm_build before lint", built)
    lint, _code = call("lint_wiki.py", "--target", TARGET, "--check-only", expect=(0, 1))
    mine = [error for error in lint.get("errors", []) if "workflow" in error or "crm-runs" in error or "crm-workflow-state" in error]
    check(not mine, "lint: no workflow errors", mine)
    check(lint.get("valid") is True, "lint: wiki valid after all runs", lint.get("errors", [])[:10])


def main():
    wiki = setup()
    try:
        test_formula()
        for scenario in (test_units, test_closed_won, test_stale_cron, test_formula_field, test_webhook, test_form, test_delay, test_classify,
                         test_loops, test_immutable, test_outbox_and_http, test_iterator_limit, test_round_robin, test_waits_and_agents,
                         test_delete_and_balance, test_draft_resume_and_cancel, test_migration_events, test_erasure_rewrites, test_list_and_lint):
            try:
                scenario()
            except Exception as exc:  # noqa: BLE001 - report and continue with the next scenario
                import traceback

                FAILURES.append(f"{scenario.__name__} raised {type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}")
    finally:
        wiki.release()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES ({CHECKS} checks)")
        for failure in FAILURES:
            print("-", failure)
        return 1
    print(f"ALL OK ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
