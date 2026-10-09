#!/usr/bin/env python3
"""Tests for CRM views and dashboards: crm_views.py, crm_build.py, crm_svg.py, assets/crm-template.html.

Usage: python3 tests/views/test_views.py
Creates test wikis under test-wikis/views/ (t-views, t-views-copy, t-views-en), builds graph/crm/ and
prints ALL OK or the list of failures. Needs no pytest and no network.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import traceback
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent.parent
sys.path.insert(0, str(WORKSPACE / "tools"))
from harness import SCRIPTS, SKILL, Wiki  # noqa: E402

sys.path.insert(0, str(SCRIPTS))
import crm_build  # noqa: E402
import crm_contract  # noqa: E402
import crm_filters  # noqa: E402
import crm_standard  # noqa: E402
import crm_svg  # noqa: E402
import crm_views  # noqa: E402

VIEWS_DIR = WORKSPACE / "test-wikis" / "views"
VIEWS_DIR.mkdir(parents=True, exist_ok=True)
BASE_DE = VIEWS_DIR / "base-de"
BASE_EN = VIEWS_DIR / "base-en"
TARGET = VIEWS_DIR / "t-views"
COPY = VIEWS_DIR / "t-views-copy"
TARGET_EN = VIEWS_DIR / "t-views-en"
TOKENS = VIEWS_DIR / ".tokens"
WORK = VIEWS_DIR / ".work-views"
NOW = "2026-10-06T15:00:00Z"
EVIL = 'Evil </script><img src="https://evil.example/x.png" onerror=alert(1)> GmbH'

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def ensure_base(path: Path, language: str) -> None:
    if (path / "WIKI.md").is_file():
        return
    completed = subprocess.run([sys.executable, str(WORKSPACE / "tools" / "make_test_wiki.py"), str(SKILL), str(path),
                                "--language", language], capture_output=True, text=True)
    if completed.returncode != 0:
        raise SystemExit(f"make_test_wiki failed: {completed.stdout}\n{completed.stderr}")


def fresh_wiki(base: Path, target: Path) -> Wiki:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(base, target)
    wiki = Wiki(target, TOKENS)
    if wiki.token_file.exists():
        wiki.token_file.unlink()
    wiki.acquire("test-views")
    return wiki


def token(wiki: Wiki) -> str:
    return wiki.token_file.read_text(encoding="utf-8").strip()


def build(wiki: Wiki, *extra: str, now: str = NOW) -> tuple[int, dict]:
    command = [sys.executable, str(SCRIPTS / "crm_build.py"), "--target", str(wiki.target), "--lock-token", token(wiki)]
    if now:
        command += ["--now", now]
    completed = subprocess.run(command + list(extra), capture_output=True, text=True)
    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError:
        report = {"_stdout": completed.stdout[-3000:], "_stderr": completed.stderr[-3000:]}
    return completed.returncode, report


def transact(wiki: Wiki, operations: list, name: str, confirm: bool = False) -> dict:
    report, result = wiki.transact({"actor": "agent/test-views", "origin": {"kind": "manual", "occasion": "Views-Test"},
                                    "operations": operations}, WORK / name, confirm=confirm)
    if report.get("state") != "planned" or not result or result.get("state") != "applied":
        raise SystemExit(f"transaction {name} failed: {json.dumps(report, ensure_ascii=False)[:2000]} {result}")
    return result


def ids_by_title(target: Path, directory: str) -> dict[str, str]:
    found = {}
    for path in sorted((target / "records" / directory).glob("*.md")):
        data, _rich = crm_contract.parse_record_text(path.read_text(encoding="utf-8"), path.name)
        found[data["crm_title"]] = data["crm_id"]
    return found


def page_data(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'<script type="application/json" id="crm-data">(.*?)</script>', text, re.S)
    return json.loads(match.group(1))


def output_hashes(target: Path) -> dict[str, str]:
    root = target / "graph" / "crm"
    return {path.relative_to(target).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def tree_hashes(target: Path, exclude: tuple[str, ...]) -> dict[str, str]:
    result = {}
    for path in sorted(target.rglob("*")):
        relative = path.relative_to(target).as_posix()
        if path.is_file() and not relative.startswith(exclude):
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


# ---------------------------------------------------------------------------
# data


def seed(wiki: Wiki) -> dict[str, dict[str, str]]:
    target = wiki.target
    transact(wiki, [
        {"op": "create", "object": "workspaceMember", "ref": "anna", "values": {"name": {"firstName": "Anna", "lastName": "Admin"}, "userEmail": "anna@firma.example"}},
        {"op": "create", "object": "workspaceMember", "ref": "ben", "values": {"name": {"firstName": "Ben", "lastName": "Berater"}, "userEmail": "ben@firma.example"}},
        {"op": "create", "object": "company", "ref": "acme", "values": {"name": "Acme GmbH", "domainName": "https://www.acme.example/", "address": {"addressCity": "Berlin"}, "accountOwner": {"ref": "anna"}, "annualRevenue": {"amount": "2000000", "currencyCode": "EUR"}}},
        {"op": "create", "object": "company", "ref": "beta", "values": {"name": "Beta AG", "domainName": "beta.example", "annualRevenue": {"amount": "500000", "currencyCode": "USD"}, "accountOwner": {"ref": "ben"}}},
        {"op": "create", "object": "company", "ref": "evil", "values": {"name": EVIL, "domainName": "javascript:alert(1)"}},
        {"op": "create", "object": "company", "ref": "gamma", "values": {"name": "Gamma KG", "domainName": "gamma.example"}},
        {"op": "create", "object": "person", "ref": "max", "values": {"name": {"firstName": "Max", "lastName": "Muster"}, "emails": "max@acme.example", "company": {"ref": "acme"}}},
        {"op": "create", "object": "person", "ref": "erika", "values": {"name": {"firstName": "Erika", "lastName": "Beispiel"}, "emails": "erika@beta.example", "company": {"ref": "beta"}}},
        {"op": "create", "object": "opportunity", "ref": "relaunch", "values": {"name": "Relaunch", "stage": "PROPOSAL", "amount": {"amount": "100000.00", "currencyCode": "EUR"}, "company": {"ref": "acme"}, "pointOfContact": {"ref": "max"}, "owner": {"ref": "anna"}, "closeDate": "2026-10-20T10:00:00Z"}},
        {"op": "create", "object": "opportunity", "values": {"name": "Shop", "stage": "PROPOSAL", "amount": {"amount": "50000.50", "currencyCode": "EUR"}, "company": {"ref": "beta"}, "owner": {"ref": "ben"}, "closeDate": "2026-11-05T10:00:00Z"}},
        {"op": "create", "object": "opportunity", "values": {"name": "Hosting", "stage": "PROPOSAL", "amount": {"amount": "20000", "currencyCode": "USD"}, "company": {"ref": "beta"}, "owner": {"ref": "ben"}, "closeDate": "2026-11-20T10:00:00Z"}},
        {"op": "create", "object": "opportunity", "values": {"name": "Beratung", "stage": "NEW", "amount": {"amount": "10000", "currencyCode": "EUR"}, "company": {"ref": "acme"}, "owner": {"ref": "anna"}, "closeDate": "2026-12-01T10:00:00Z"}},
        {"op": "create", "object": "opportunity", "values": {"name": "Wartung", "stage": "CUSTOMER", "amount": {"amount": "7500.25", "currencyCode": "EUR"}, "company": {"ref": "acme"}, "owner": {"ref": "anna"}, "closeDate": "2026-08-15T10:00:00Z"}},
        {"op": "create", "object": "opportunity", "values": {"name": "Lizenz", "stage": "MEETING", "amount": {"amount": "3000", "currencyCode": "USD"}, "company": {"ref": "beta"}, "owner": {"ref": "ben"}}},
        {"op": "create", "object": "task", "values": {"title": "DST vorher", "dueAt": "2026-10-24T22:30:00Z", "status": "TODO", "assignee": {"ref": "anna"}}},
        {"op": "create", "object": "task", "values": {"title": "DST Nacht", "dueAt": "2026-10-25T00:30:00Z", "status": "TODO"}},
        {"op": "create", "object": "task", "values": {"title": "DST danach", "dueAt": "2026-10-25T01:30:00Z", "status": "IN_PROGRESS"}},
        {"op": "create", "object": "task", "values": {"title": "DST Abend", "dueAt": "2026-10-25T22:30:00Z", "status": "DONE"}},
        {"op": "create", "object": "task", "values": {"title": "DST Folgetag", "dueAt": "2026-10-25T23:30:00Z", "status": "TODO"}},
        {"op": "create", "object": "task", "values": {"title": "Ohne Datum", "status": "TODO"}},
        {"op": "create", "object": "task", "values": {"title": "Angebot nachfassen", "dueAt": "2026-10-10T07:00:00Z", "status": "TODO",
                                                      "assignee": {"ref": "anna"},
                                                      "targets": [{"object": "company", "ref": "acme"}, {"object": "person", "ref": "max"}],
                                                      "bodyV2": "Bitte **Freitag** anrufen. <script>alert(1)</script> [Angebot](https://intranet.example/a) [böse](javascript:alert(1))"}},
        {"op": "create", "object": "note", "values": {"title": "Erstgespräch", "targets": [{"object": "company", "ref": "acme"}, {"object": "opportunity", "ref": "relaunch"}], "bodyV2": "Interesse an *Relaunch*."}},
        {"op": "create", "object": "message", "values": {"subject": "Angebot Relaunch", "receivedAt": "2026-10-01T08:00:00Z", "direction": "INCOMING", "participants": [{"object": "person", "ref": "max"}]}},
        {"op": "create", "object": "calendarEvent", "values": {"title": "Kickoff", "startsAt": "2026-10-12T09:00:00Z", "endsAt": "2026-10-12T10:00:00Z", "participants": [{"object": "person", "ref": "max"}, {"object": "workspaceMember", "ref": "anna"}]}},
    ], "seed")
    transact(wiki, [{"op": "delete", "object": "company", "record": {"match": {"name": "Gamma KG"}}}], "trash")
    transact(wiki, [{"op": "update", "object": "opportunity", "record": {"match": {"name": "Beratung"}}, "values": {"stage": "SCREENING"}},
                    {"op": "update", "object": "opportunity", "record": {"match": {"name": "Beratung"}}, "values": {"stage": "NEW"}}], "stage-move")
    transact(wiki, [{"op": "erase", "object": "person", "record": {"match": {"emails": "erika@beta.example"}}}], "erase", confirm=True)
    ids = {name: ids_by_title(target, directory) for name, directory in (
        ("company", "company"), ("person", "person"), ("opportunity", "opportunity"), ("task", "task"),
        ("workspaceMember", "workspace-member"), ("note", "note"))}
    acme = ids["company"]["Acme GmbH"]
    page = target / "wiki" / "kunden" / "acme.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntitle: \"Kunde Acme\"\n---\n# Kunde Acme\n\nSiehe [[records/company/" + acme + "|Acme GmbH]].\n", encoding="utf-8")
    workflows = target / "schema" / "crm" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "closed-won.json").write_text(json.dumps({"format": "lmwiki-crm-workflow/1", "name": "Closed Won Folgeaufgabe", "active": True}), encoding="utf-8")
    (workflows / "entwurf.json").write_text(json.dumps({"name": "Entwurf Begrüßung", "versions": [{"status": "DRAFT"}]}), encoding="utf-8")
    (workflows / "kaputt.json").write_text("{nicht json", encoding="utf-8")
    return ids


def definitions(target: Path) -> None:
    views = crm_standard.standard_views("de")
    for view in views["views"]:
        if view["id"] == "pipeline":
            view["probabilities"] = {"NEW": 0.1, "SCREENING": "0.25", "MEETING": 0.4, "PROPOSAL": 0.6, "CUSTOMER": 1}
    views["views"] += [
        {"id": "relaunch-only", "object": "opportunity", "type": "kanban", "label": "Nur Relaunch", "groupBy": "stage",
         "fields": ["amount"], "filter": {"op": "AND", "conditions": [{"field": "name", "operand": "IS", "value": "Relaunch"}]},
         "aggregate": {"function": "SUM", "field": "amount"}, "probabilities": {"PROPOSAL": 0.6}, "hideEmptyGroups": True},
        {"id": "deals-by-stage", "object": "opportunity", "type": "table", "label": "Chancen nach Phase", "groupBy": "stage",
         "fields": ["name", "amount", "company", "owner", "closeDate"], "aggregates": {"amount": "SUM", "name": "COUNT"},
         "sort": [{"field": "amount", "direction": "desc"}], "visibility": "restricted", "roles": ["vertrieb"]},
        {"id": "this-month", "object": "opportunity", "type": "table", "label": "Abschluss diesen Monat",
         "fields": ["name", "closeDate", "stage"], "filter": {"op": "AND", "conditions": [{"field": "closeDate", "operand": "IS_RELATIVE", "value": "THIS_1_MONTH"}]}},
        {"id": "my-tasks", "object": "task", "type": "table", "label": "Meine Aufgaben", "fields": ["title", "dueAt"],
         "filter": {"op": "AND", "conditions": [{"field": "assignee", "operand": "IS", "value": "@me"}]}, "me": "MEMBER"},
        {"id": "events-week", "object": "calendarEvent", "type": "calendar", "label": "Termine", "dateField": "startsAt", "calendarMode": "week", "fields": ["location"]},
    ]
    anna = ids_by_title(target, "workspace-member")["Anna Admin"]
    for view in views["views"]:
        if view.get("me") == "MEMBER":
            view["me"] = anna
    (target / "schema/crm/views.json").write_text(json.dumps(views, ensure_ascii=False, indent=2), encoding="utf-8")
    dashboards = crm_standard.standard_dashboards("de")
    dashboards["dashboards"].append({"id": "test", "label": "Testboard", "widgets": [
        {"id": "sum-amount", "type": "number", "label": "Summe Betrag", "object": "opportunity", "aggregate": {"function": "SUM", "field": "amount"}, "layout": {"column": 0, "span": 4, "row": 0}},
        {"id": "count-deals", "type": "number", "label": "Chancen", "object": "opportunity", "aggregate": {"function": "COUNT"}, "suffix": " Stück", "layout": {"column": 4, "span": 4, "row": 0}},
        {"id": "avg-eur", "type": "number", "label": "Mittel", "object": "opportunity", "aggregate": {"function": "avg", "field": "amount"}, "layout": {"column": 8, "span": 4, "row": 0}},
        {"id": "by-stage", "type": "bar", "label": "Je Phase", "object": "opportunity", "groupBy": "stage", "aggregate": {"function": "COUNT"}},
        {"id": "stage-owner", "type": "bar", "label": "Phase und Verantwortlich", "object": "opportunity", "groupBy": "stage", "secondaryGroupBy": "owner", "aggregate": {"function": "COUNT"}},
        {"id": "top-companies", "type": "bar", "label": "Top Firmen", "object": "opportunity", "groupBy": "company", "orientation": "horizontal", "aggregate": {"function": "SUM", "field": "amount"}, "orderBy": "value", "limit": 1},
        {"id": "per-month", "type": "line", "label": "Kumuliert je Monat", "object": "opportunity", "groupBy": "closeDate", "dateGranularity": "MONTH", "cumulative": True, "aggregate": {"function": "COUNT"}},
        {"id": "pie-stage", "type": "pie", "label": "Anteil je Phase", "object": "opportunity", "groupBy": "stage", "aggregate": {"function": "COUNT"}},
        {"id": "deal-table", "type": "table", "label": "Größte Chancen", "view": "deals-by-stage", "limit": 2},
        {"id": "hinweis", "type": "richtext", "text": "## Hinweis\n\nSiehe [[records/company/" + ids_by_title(target, "company")["Acme GmbH"] + "|Acme]] und <b>kein HTML</b>."},
        {"id": "extern", "type": "link", "label": "Externes Board", "url": "https://example.org/board"},
    ]})
    (target / "schema/crm/dashboards.json").write_text(json.dumps(dashboards, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# tests


def test_validation() -> None:
    dm = crm_contract.DataModel(crm_standard.standard_datamodel("de"), "x")
    for language in ("de", "en"):
        views = crm_standard.standard_views(language)
        check(crm_views.validate_views(dm, views) == [], f"standard views ({language}) must validate: {crm_views.validate_views(dm, views)}")
        dashboards = crm_standard.standard_dashboards(language)
        check(crm_views.validate_dashboards(dm, dashboards, views) == [], f"standard dashboards ({language}) must validate")

    def errors_for(view: dict) -> list[str]:
        return crm_views.validate_views(dm, {"format": crm_views.VIEWS_FORMAT, "views": [view]})

    base = {"id": "v", "object": "opportunity", "label": "V"}
    problems = errors_for({**base, "type": "kanban", "groupBy": "name"})
    check(any("SELECT" in problem for problem in problems), f"kanban without SELECT must fail: {problems}")
    problems = errors_for({**base, "type": "kanban"})
    check(any("groupBy" in problem for problem in problems), f"kanban without groupBy must fail: {problems}")
    problems = errors_for({**base, "type": "table", "fields": ["name", "doesNotExist"]})
    check(any("unknown field opportunity.doesNotExist" in problem for problem in problems), f"unknown field must fail: {problems}")
    problems = errors_for({**base, "type": "table", "sort": [{"field": "nope"}]})
    check(any("unknown field" in problem for problem in problems), f"unknown sort field must fail: {problems}")
    for visibility in ("unlisted", "private", "UNLISTED"):
        problems = errors_for({**base, "type": "table", "visibility": visibility})
        check(any("cannot be represented in a released wiki" in problem and "export" in problem for problem in problems),
              f"visibility {visibility} must fail with an explanation: {problems}")
    problems = errors_for({**base, "type": "table", "visibility": "restricted"})
    check(any("roles" in problem for problem in problems), f"restricted without roles must fail: {problems}")
    problems = crm_views.validate_views(dm, {"format": crm_views.VIEWS_FORMAT, "views": [{**base, "type": "table", "visibility": "restricted", "roles": ["chef"]}]},
                                        roles={"roles": {"vertrieb": {}}})
    check(any("unknown roles" in problem for problem in problems), f"unknown role must fail: {problems}")
    problems = errors_for({**base, "type": "kanban", "groupBy": "stage", "aggregate": {"function": "SUM", "field": "amount"}, "probabilities": {"PROPOSAL": 60}})
    check(any("between 0 and 1" in problem for problem in problems), f"probability 60 must fail: {problems}")
    problems = errors_for({**base, "type": "kanban", "groupBy": "stage", "probabilities": {"WON": 0.5}, "expectedAmountField": "amount"})
    check(any("not an option" in problem for problem in problems), f"unknown probability option must fail: {problems}")
    problems = errors_for({**base, "type": "calendar", "dateField": "name"})
    check(any("DATE or DATE_TIME" in problem for problem in problems), f"calendar on TEXT must fail: {problems}")
    problems = errors_for({**base, "type": "table", "filter": {"op": "AND", "conditions": [{"field": "owner", "operand": "IS", "value": "@me"}]}})
    check(any("@me" in problem for problem in problems), f"@me without me must fail: {problems}")
    problems = errors_for({**base, "type": "table", "filter": {"op": "AND", "conditions": [{"field": "company.name", "operand": "CONTAINS", "value": "x"}]}})
    check(any("related records" in problem for problem in problems), f"filter on related field must fail: {problems}")
    problems = crm_views.validate_views(dm, {"format": crm_views.VIEWS_FORMAT, "views": [
        {"id": "c", "object": "company", "label": "C", "type": "table", "filter": {"op": "AND", "conditions": [{"field": "people", "operand": "IS_EMPTY"}]}}]})
    check(any("inverse" in problem for problem in problems), f"filter on inverse relation must fail: {problems}")
    problems = errors_for({**base, "type": "table", "filtr": {}})
    check(any("unknown key 'filtr'" in problem for problem in problems), f"unknown key must fail: {problems}")
    views = {"format": crm_views.VIEWS_FORMAT, "views": [{**base, "type": "table"}]}
    widget_errors = crm_views.validate_dashboards(dm, {"format": crm_views.DASHBOARDS_FORMAT, "dashboards": [{"id": "d", "label": "D", "widgets": [
        {"id": "t", "type": "table", "label": "T", "view": "missing"},
        {"id": "b", "type": "bar", "label": "B", "object": "opportunity", "aggregate": {"function": "SUM", "field": "name"}, "groupBy": "stage"},
        {"id": "l", "type": "link", "url": "javascript:alert(1)"},
        {"id": "g", "type": "pie", "label": "G", "object": "opportunity", "groupBy": "stage", "layout": {"column": 10, "span": 4}},
        {"id": "x", "type": "gauge", "label": "X"},
    ]}]}, views)
    for expected in ("view 'missing' does not exist", "SUM needs a NUMBER", "http or https url", "exceeds the 12-column grid", "type must be number"):
        check(any(expected in problem for problem in widget_errors), f"dashboard validation must report {expected!r}: {widget_errors}")


def test_markdown_and_svg() -> None:
    rendered = crm_build.markdown_html("**fett** <script>x</script> [a](javascript:alert(1)) [b](https://ok.example/p?q=1&r=2)\n\n- eins\n- zwei")
    check("<script>" not in rendered and "&lt;script&gt;" in rendered, f"markdown must escape HTML: {rendered}")
    check('href="javascript' not in rendered, "markdown must drop javascript links")
    check('<a href="https://ok.example/p?q=1&amp;r=2" rel="noopener noreferrer" target="_blank">b</a>' in rendered, f"markdown links: {rendered}")
    check("<ul><li>eins</li><li>zwei</li></ul>" in rendered and "<strong>fett</strong>" in rendered, f"markdown lists/bold: {rendered}")
    check(crm_build.safe_url("javascript:alert(1)", bare_domains=True) is None, "safe_url must refuse javascript:")
    check(crm_build.safe_url("acme.example", bare_domains=True) == "https://acme.example", "bare domains become https")
    embedded = crm_build.embed_json({"x": "</script><!-- & \u2028"})
    check("</" not in embedded and "<!--" not in embedded and "&" not in embedded, f"embedded JSON must not contain markup: {embedded}")
    check(json.loads(embedded) == {"x": "</script><!-- & \u2028"}, "embedded JSON must round-trip")
    low, high, step = crm_svg.nice_scale(Decimal(0), Decimal("417"))
    check((low, high, step) == (Decimal(0), Decimal(500), Decimal(100)) or (low, high) == (Decimal(0), Decimal(600)), f"nice scale: {(low, high, step)}")
    svg = crm_svg.bar_chart("c", "T", "D", ["A", "B"], [{"label": "S", "values": [Decimal(3), None], "slot": 0}],
                            value_text=lambda value: "" if value is None else str(value), tick_text=str)
    check(svg.startswith('<svg class="chart"') and 'role="img"' in svg and "<title" in svg, "bar chart needs role and title")
    again = crm_svg.bar_chart("c", "T", "D", ["A", "B"], [{"label": "S", "values": [Decimal(3), None], "slot": 0}],
                              value_text=lambda value: "" if value is None else str(value), tick_text=str)
    check(svg == again, "SVG must be deterministic")


def test_build(wiki: Wiki, ids: dict[str, dict[str, str]]) -> None:
    target = wiki.target
    before_outside = tree_hashes(target, ("graph/crm/", ".llmwiki"))
    code, report = build(wiki)
    check(code == 0 and report.get("state") == "built", f"build must succeed: {code} {report}")
    if code != 0:
        return
    after_outside = tree_hashes(target, ("graph/crm/", ".llmwiki"))
    check(before_outside == after_outside, "the build must write only below graph/crm/")
    crm = target / "graph" / "crm"
    expected = ["index.html", "manifest.json", "objects/company.html", "objects/person.html", "objects/opportunity.html",
                "objects/task.html", "objects/note.html", "objects/workspace-member.html", "objects/message.html",
                "objects/calendar-event.html", "objects/attachment.html", "views/pipeline.html", "views/tasks-calendar.html",
                "views/deals-by-stage.html", "views/relaunch-only.html", "views/events-week.html", "dashboards/sales.html", "dashboards/test.html"]
    for name in expected:
        check((crm / name).is_file(), f"missing graph/crm/{name}")
    manifest = json.loads((crm / "manifest.json").read_text(encoding="utf-8"))
    check(manifest.get("format") == "lmwiki-crm-build/1" and manifest.get("generated_at") == NOW, f"manifest header: {manifest.get('format')} {manifest.get('generated_at')}")
    check(manifest.get("inputs_sha256") == crm_build.expected_inputs_sha256(target), "manifest inputs_sha256 must match expected_inputs_sha256")
    check(set(manifest["files"]) == {path for path in output_hashes(target) if not path.endswith("manifest.json")}, "manifest must list every generated file")
    check(crm_build.check_fresh(target) == [], f"fresh build must pass check_fresh: {crm_build.check_fresh(target)}")
    code, fresh = build(wiki, "--check")
    check(code == 0 and fresh.get("state") == "fresh", f"--check after build: {code} {fresh}")

    # kanban sums and expected amounts, per currency
    pipeline = page_data(crm / "views/pipeline.html")
    columns = {column["key"]: column for column in pipeline["columns"]}
    check(list(columns) == ["NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER"], f"kanban columns in option order: {list(columns)}")

    def amounts(values: list) -> dict:
        return {item["currency"]: Decimal(item["value"]) for item in values or [] if item["value"] is not None}

    check(amounts(columns["PROPOSAL"]["aggregate"]) == {"EUR": Decimal("150000.50"), "USD": Decimal("20000")}, f"PROPOSAL sum: {columns['PROPOSAL']['aggregate']}")
    check(columns["PROPOSAL"]["count"] == 3 and columns["SCREENING"]["count"] == 0, "kanban counts")
    check(amounts(columns["NEW"]["aggregate"]) == {"EUR": Decimal("10000")}, f"NEW sum: {columns['NEW']['aggregate']}")
    check(amounts(columns["PROPOSAL"]["expected"]) == {"EUR": Decimal("90000.300"), "USD": Decimal("12000")}, f"PROPOSAL expected: {columns['PROPOSAL']['expected']}")
    check(amounts(columns["CUSTOMER"]["expected"]) == {"EUR": Decimal("7500.25")}, f"CUSTOMER expected: {columns['CUSTOMER']['expected']}")
    check(amounts(pipeline["expected"]) == {"EUR": Decimal("90000.300") + Decimal("1000.0") + Decimal("7500.25"), "USD": Decimal("12000") + Decimal("1200.0")},
          f"expected total: {pipeline['expected']}")
    html_text = (crm / "views/pipeline.html").read_text(encoding="utf-8")
    check("150.000,50 EUR" in html_text and "20.000,00 USD" in html_text, "kanban shows sums per currency in German format")
    check("90.000,30 EUR" in html_text, "kanban shows the expected amount")
    relaunch = page_data(crm / "views/relaunch-only.html")
    check([column["key"] for column in relaunch["columns"]] == ["PROPOSAL"], f"hideEmptyGroups: {[c['key'] for c in relaunch['columns']]}")
    check(amounts(relaunch["columns"][0]["expected"]) == {"EUR": Decimal("60000")}, f"60 % of 100.000 EUR must be 60.000 EUR: {relaunch['columns'][0]['expected']}")
    beratung = ids["opportunity"]["Beratung"]
    check("in dieser Phase" in html_text and beratung in html_text, "time in stage comes from the event log")

    # calendar across the end of daylight saving time in Europe/Berlin (25.10.2026)
    calendar = page_data(crm / "views/tasks-calendar.html")
    days = {entry[3]: (entry[0], entry[1]) for entry in calendar["entries"]}
    check(days.get("DST vorher") == ("2026-10-25", "00:30"), f"22:30Z on 24.10. is 00:30 CEST on 25.10.: {days.get('DST vorher')}")
    check(days.get("DST Nacht") == ("2026-10-25", "02:30"), f"00:30Z is 02:30 CEST: {days.get('DST Nacht')}")
    check(days.get("DST danach") == ("2026-10-25", "02:30"), f"01:30Z is 02:30 CET: {days.get('DST danach')}")
    check(days.get("DST Abend") == ("2026-10-25", "23:30"), f"22:30Z on 25.10. stays on 25.10. (CET): {days.get('DST Abend')}")
    check(days.get("DST Folgetag") == ("2026-10-26", "00:30"), f"23:30Z on 25.10. is 00:30 on 26.10.: {days.get('DST Folgetag')}")
    check(calendar["undated"] == 1 and "Ohne Datum" not in days, "a task without due date is listed as undated")
    day25 = [entry[3] for entry in calendar["entries"] if entry[0] == "2026-10-25"]
    check(day25 == ["DST vorher", "DST Nacht", "DST danach", "DST Abend"], f"entries of 25.10. in the order they happen, the repeated hour included: {day25}")
    check(calendar["today"] == "2026-10-06" and calendar["timeZone"] == "Europe/Berlin" and calendar["ws"] == 1, "calendar context")
    week = page_data(crm / "views/events-week.html")
    check(week["mode"] == "week" and [entry[3] for entry in week["entries"]] == ["Kickoff"], "week calendar")

    # dashboards equal crm_filters.aggregate over the same records
    dm = crm_contract.load_datamodel(target)
    store = crm_contract.RecordStore(target, dm)
    context = crm_filters.Context(now=NOW, time_zone="Europe/Berlin", week_start=1)
    opportunities = list(store.records("opportunity").values())
    board = {widget["id"]: widget for widget in page_data(crm / "dashboards/test.html")["widgets"]}
    for currency in ("EUR", "USD"):
        part = [record for record in opportunities if not record.deleted and record.data.get("amount.currencyCode") == currency]
        value = next(item for item in board["sum-amount"]["values"] if item["currency"] == currency)
        check(Decimal(value["value"]) == crm_filters.aggregate(dm, part, "SUM", "amount"), f"number widget {currency} differs from crm_filters.aggregate")
        average = next(item for item in board["avg-eur"]["values"] if item["currency"] == currency)
        check(Decimal(average["value"]) == crm_filters.aggregate(dm, part, "AVG", "amount"), f"AVG {currency} differs from crm_filters.aggregate")
    active = crm_filters.select_records(dm, opportunities, context=context)
    check(Decimal(board["count-deals"]["values"][0]["value"]) == crm_filters.aggregate(dm, active, "COUNT"), "COUNT widget")
    panel = board["by-stage"]["panels"][0]
    for label, value in zip(panel["categories"], panel["series"][0]["values"]):
        stage = next(option["value"] for option in dm.fields("opportunity")["stage"]["options"] if option["label"] == label)
        group = [record for record in active if record.data.get("stage") == stage]
        check(Decimal(value) == crm_filters.aggregate(dm, group, "COUNT"), f"bar value for {label} differs from crm_filters.aggregate")
    check(panel["categories"] == ["Neu", "Termin", "Angebot", "Kunde"], f"bar categories in option order: {panel['categories']}")
    sales = {widget["id"]: widget for widget in page_data(crm / "dashboards/sales.html")["widgets"]}
    open_deals = crm_filters.select_records(dm, opportunities, filter_spec={"op": "and", "conditions": [{"field": "stage", "operator": "isNot", "value": "CUSTOMER"}]}, context=context)
    for item in sales["pipeline-value"]["values"]:
        part = [record for record in open_deals if record.data.get("amount.currencyCode") == item["currency"]]
        check(Decimal(item["value"]) == crm_filters.aggregate(dm, part, "SUM", "amount"), "standard pipeline-value widget")
    month_panels = {panel["currency"]: panel for panel in sales["amount-by-month"]["panels"]}
    check(set(month_panels) == {"EUR", "USD"}, f"sum by month is split by currency: {set(month_panels)}")
    stacked = board["stage-owner"]["panels"][0]
    check({series["label"] for series in stacked["series"]} == {"Anna Admin", "Ben Berater"}, f"stacked series: {[s['label'] for s in stacked['series']]}")
    top = board["top-companies"]["panels"]
    eur_top = next(panel for panel in top if panel["currency"] == "EUR")
    check(eur_top["hidden"] == 1 and eur_top["categories"] == ["Acme GmbH"] and all(len(panel["categories"]) == 1 for panel in top),
          f"limit shows top N per currency and reports hidden groups: {top}")
    dashboard_html = (crm / "dashboards/test.html").read_text(encoding="utf-8")
    check("nicht angezeigte Daten" in dashboard_html, "the limit note must say 'nicht angezeigte Daten'")
    cumulative = board["per-month"]["panels"][0]["series"][0]
    running, sums = Decimal(0), []
    for raw in cumulative["raw"]:
        running += Decimal(raw or 0)
        sums.append(running)
    check([Decimal(value) for value in cumulative["values"]] == sums, "cumulative values are running totals of the raw values")
    check(board["per-month"]["panels"][0]["categories"][:4] == ["Aug. 2026", "Sept. 2026", "Okt. 2026", "Nov. 2026"], f"month gaps are filled: {board['per-month']['panels'][0]['categories']}")
    check(board["deal-table"]["rows"] and len(board["deal-table"]["rows"]) == 2, "table widget shows the first rows of its view")
    check(board["extern"]["url"] == "https://example.org/board" and 'rel="noopener noreferrer"' in dashboard_html, "link widget")
    check("&lt;b&gt;kein HTML&lt;/b&gt;" in dashboard_html, "rich text widget escapes HTML")
    check(dashboard_html.count('role="img"') >= 5 and "<desc" in dashboard_html, "charts have accessible titles and descriptions")

    # object pages: fields, relations, timeline, backlinks, trash
    companies = page_data(crm / "objects/company.html")
    rows = {row[0]: row for row in companies["rows"]}
    acme = rows[ids["company"]["Acme GmbH"]]
    incoming = {companies["incg"][group] for group, _ref in acme[3]}
    check({"Personen", "Verkaufschancen"} <= incoming and any("Aufgaben" in label for label in incoming) and any("Notizen" in label for label in incoming),
          f"related records of Acme: {incoming}")
    check(any(companies["pages"][index][0].endswith("pages/wiki/kunden/acme.html") for index in acme[5]), "wiki backlink to the reading view")
    check(companies["raw"] == "../../../records/company/", f"raw link prefix: {companies['raw']}")
    check(rows[ids["company"]["Gamma KG"]][1] != "", "a deleted record carries its trash date")
    check(len(acme[4]) >= 1 and companies["ops"][acme[4][0][2]] == "angelegt", "timeline starts with the create event")
    people = page_data(crm / "objects/person.html")
    person_rows = {row[0]: row for row in people["rows"]}
    max_row = person_rows[ids["person"]["Max Muster"]]
    labels = {people["incg"][group] for group, _ref in max_row[3]}
    check(any("E-Mails" in label for label in labels) and any("Termine" in label for label in labels) and any("Aufgaben" in label for label in labels),
          f"participants and targets appear as related records of Max: {labels}")
    check(len(people["rows"]) == 1, "the erased person has no row")
    tasks = page_data(crm / "objects/task.html")
    body_index = next(index for index, column in enumerate(tasks["cols"]) if column[1] == "h")
    task_rows = {row[2][0]: row for row in tasks["rows"]}
    body = task_rows["Angebot nachfassen"][2][body_index]
    check("<strong>Freitag</strong>" in body and "&lt;script&gt;" in body and "<script" not in body and 'href="javascript' not in body.lower()
          and 'href="https://intranet.example/a" rel="noopener noreferrer"' in body, f"rich text body: {body}")
    index_html = (crm / "index.html").read_text(encoding="utf-8")
    check("Closed Won Folgeaufgabe" in index_html and "Entwurf Begrüßung" in index_html and "Datei nicht lesbar" in index_html, "workflows listed with name")
    check(re.search(r"Closed Won Folgeaufgabe <span class=\"badge on\">aktiv</span>", index_html) is not None, "active workflow status")
    check(re.search(r"Entwurf Begrüßung <span class=\"badge off\">inaktiv</span>", index_html) is not None, "inactive workflow status")
    check("gelöschter Datensatz" in index_html, "erased events show as deleted in recent activity")
    check("Stand: <strong>06.10.2026, 17:00</strong> (Europe/Berlin)" in index_html, "header shows the build moment in the configured zone")
    this_month = page_data(crm / "views/this-month.html")
    names = [row[2][0] for group in this_month["groups"] for row in group["rows"]]
    check(names == ["Relaunch"], f"relative filter THIS_1_MONTH evaluated at --now: {names}")
    mine = page_data(crm / "views/my-tasks.html")
    titles = sorted(row[2][0] for group in mine["groups"] for row in group["rows"])
    check(titles == ["Angebot nachfassen", "DST vorher"], f"@me bound to a named member: {titles}")
    deals = page_data(crm / "views/deals-by-stage.html")
    proposal = next(group for group in deals["groups"] if group["l"] == "Angebot")
    check(proposal["n"] == 3 and any("150.000,50 EUR · 20.000,00 USD" in text for _index, text in proposal["a"]), f"table group footer: {proposal['a']}")
    check([row[2][0] for row in proposal["rows"]] == ["Relaunch", "Shop", "Hosting"], f"table sort amount desc: {[row[2][0] for row in proposal['rows']]}")
    check("Eingeschränkt auf Rollen: vertrieb" in (crm / "views/deals-by-stage.html").read_text(encoding="utf-8"), "restricted view shows its roles")

    # every page: safe embedding, no external resources, relative links only
    user_home = str(Path.home())
    for path in sorted(crm.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        name = path.relative_to(target).as_posix()
        check(text.count("</script>") == 2, f"{name}: exactly two script elements may close")
        check("<img" not in text and not re.search(r"<[^>]*\sonerror\s*=", text), f"{name}: record text must stay escaped")
        check(str(target) not in text and user_home not in text, f"{name}: no absolute paths")
        check("Content-Security-Policy" in text and "default-src 'none'" in text, f"{name}: CSP missing")
        for tag in re.finditer(r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>", text):
            markup = tag.group(0)
            if re.search(r"""\s(?:src|href|action|poster|data|srcset)\s*=\s*["']?\s*(?:https?:)?//""", markup):
                if tag.group(1).lower() != "a" or 'rel="noopener noreferrer"' not in markup:
                    FAILURES.append(f"{name}: external resource {markup[:160]}")
            if re.search(r"""\shref\s*=\s*["']?\s*javascript:""", markup, re.I):
                FAILURES.append(f"{name}: javascript link {markup[:160]}")
        check(not re.search(r"<(link|iframe|img|object|embed|audio|video)\b", text), f"{name}: no embedded external elements")
        check("@import" not in text and not re.search(r"url\(\s*['\"]?https?:", text), f"{name}: no remote CSS")
        for link in re.findall(r'\shref="([^"]*)"', text):
            check(not link.startswith("/") and not link.lower().startswith("file:"), f"{name}: link must be relative: {link}")
    company_text = (crm / "objects/company.html").read_text(encoding="utf-8")
    check("javascript:alert" not in company_text.replace('"javascript:alert(1)"', ""), "javascript: domains are not links")
    check(EVIL not in company_text and "\\u003c/script\\u003e" in company_text, "the evil title is embedded only escaped")


def test_freshness_and_determinism(wiki: Wiki) -> None:
    target = wiki.target
    first = output_hashes(target)
    code, report = build(wiki)
    check(code == 0 and report.get("written") == 0, f"second build with the same --now writes nothing: {report.get('written')}")
    check(output_hashes(target) == first, "same inputs and --now must give identical bytes")
    transact(wiki, [{"op": "update", "object": "opportunity", "record": {"match": {"name": "Shop"}}, "values": {"stage": "CUSTOMER"}}], "fresh-update")
    stale = crm_build.check_fresh(target)
    check(any("stale" in problem for problem in stale), f"check_fresh must report stale pages after a record change: {stale}")
    code, _report = build(wiki, "--check")
    check(code == 3, "--check must exit 3 when stale")
    code, report = build(wiki)
    check(code == 0 and crm_build.check_fresh(target) == [], f"rebuild makes it fresh: {crm_build.check_fresh(target)}")
    page = target / "graph/crm/views/pipeline.html"
    page.write_bytes(page.read_bytes() + b"<!-- edited -->")
    problems = crm_build.check_fresh(target)
    check(any("differs" in problem for problem in problems), f"hand edits are detected: {problems}")
    extra = target / "graph/crm/views/alt.html"
    extra.write_text("x", encoding="utf-8")
    problems = crm_build.check_fresh(target)
    check(any("not in its manifest" in problem for problem in problems), f"extra files are detected: {problems}")
    wiki_page = target / "wiki" / "kunden" / "acme.md"
    code, report = build(wiki)
    check(code == 0 and not extra.exists() and "graph/crm/views/alt.html" in report.get("removed", []), "stale files are removed by the next build")
    check(crm_build.check_fresh(target) == [], "fresh after removing stale files")
    wiki_page.write_text(wiki_page.read_text(encoding="utf-8").replace("Kunde Acme", "Kunde Acme Holding"), encoding="utf-8")
    check(any("stale" in problem for problem in crm_build.check_fresh(target)), "a changed backlink title makes graph/crm stale")
    code, report = build(wiki)
    unrelated = target / "wiki" / "overview.md"
    unrelated.write_text(unrelated.read_text(encoding="utf-8") + "\nNeuer Absatz ohne Datensatzlink.\n", encoding="utf-8")
    check(crm_build.check_fresh(target) == [], "an unrelated wiki edit keeps graph/crm fresh")
    views_path = target / "schema/crm/views.json"
    original = views_path.read_text(encoding="utf-8")
    broken = json.loads(original)
    broken["views"].append({"id": "privat", "object": "task", "type": "table", "label": "Privat", "visibility": "unlisted"})
    views_path.write_text(json.dumps(broken), encoding="utf-8")
    before = output_hashes(target)
    code, report = build(wiki)
    check(code == 4 and report.get("state") == "invalid" and any("unlisted" in error for error in report.get("errors", [])), f"invalid views stop the build: {code} {report}")
    check(output_hashes(target) == before, "an invalid build leaves graph/crm untouched")
    views_path.write_text(original, encoding="utf-8")
    code, report = build(wiki, now="2026-13-01T00:00:00Z")
    check(code == 2 and report.get("state") == "error", f"invalid --now is an error: {code} {report}")
    code, report = build(wiki)
    check(code == 0, "build after restoring views")


def test_trash_hides_relations(wiki: Wiki, ids: dict[str, dict[str, str]]) -> None:
    target = wiki.target
    transact(wiki, [{"op": "delete", "object": "person", "record": {"match": {"emails": "max@acme.example"}}}], "trash-max")
    code, report = build(wiki)
    check(code == 0, f"build after trashing Max: {report}")
    crm = target / "graph" / "crm"
    companies = page_data(crm / "objects/company.html")
    acme = next(row for row in companies["rows"] if row[0] == ids["company"]["Acme GmbH"])
    groups = {companies["incg"][group] for group, _ref in acme[3]}
    check("Personen" not in groups, f"a person in the trash is not a related record of Acme: {groups}")
    people = page_data(crm / "objects/person.html")
    max_row = next(row for row in people["rows"] if row[0] == ids["person"]["Max Muster"])
    company_index = next(index for index, column in enumerate(people["cols"]) if column[0] == "Firma")
    check(max_row[1] != "" and max_row[2][company_index], "the trashed person keeps its own outgoing company link")
    transact(wiki, [{"op": "restore", "object": "person", "record": {"match": {"emails": "max@acme.example"}}}], "restore-max")
    code, report = build(wiki)
    companies = page_data(crm / "objects/company.html")
    acme = next(row for row in companies["rows"] if row[0] == ids["company"]["Acme GmbH"])
    check("Personen" in {companies["incg"][group] for group, _ref in acme[3]}, "a restored person is related again")


def test_path_independence(wiki: Wiki) -> None:
    original = output_hashes(wiki.target)
    wiki.release()
    if COPY.exists():
        shutil.rmtree(COPY)
    shutil.copytree(wiki.target, COPY)
    copy = Wiki(COPY, TOKENS)
    if copy.token_file.exists():
        copy.token_file.unlink()
    copy.acquire("test-views-copy")
    try:
        check(crm_build.check_fresh(COPY) == [], f"a copied wiki keeps a fresh graph/crm: {crm_build.check_fresh(COPY)}")
        code, report = build(copy)
        check(code == 0 and report.get("written") == 0, f"a copy at another path builds identical bytes: {report.get('written')}")
        check(output_hashes(COPY) == original, "outputs do not depend on the wiki location")
    finally:
        copy.release()


def test_english() -> None:
    ensure_base(BASE_EN, "en")
    wiki = fresh_wiki(BASE_EN, TARGET_EN)
    try:
        wiki.locked("crm_init.py", "--target", TARGET_EN)
        transact(wiki, [{"op": "create", "object": "company", "values": {"name": "Acme Inc", "annualRevenue": {"amount": "1234.5", "currencyCode": "USD"}}},
                        {"op": "create", "object": "task", "values": {"title": "Call", "dueAt": "2026-10-06T13:00:00Z"}}], "en-seed")
        code, report = build(wiki)
        check(code == 0, f"English build: {report}")
        index_html = (TARGET_EN / "graph/crm/index.html").read_text(encoding="utf-8")
        check('<html lang="en"' in index_html and "As of <strong>10/06/2026, 3:00 PM</strong> (UTC)" in index_html, "English texts and formats")
        companies = (TARGET_EN / "graph/crm/objects/company.html").read_text(encoding="utf-8")
        check("1,234.50 USD" in companies and "Show trash" in companies, "English number format and labels")
    finally:
        wiki.release()


def main() -> int:
    ensure_base(BASE_DE, "de")
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        test_validation()
        test_markdown_and_svg()
    except Exception:  # noqa: BLE001
        FAILURES.append("unit tests crashed:\n" + traceback.format_exc())
    wiki = fresh_wiki(BASE_DE, TARGET)
    released = False
    try:
        wiki.locked("crm_init.py", "--target", TARGET)
        ids = seed(wiki)
        definitions(TARGET)
        test_build(wiki, ids)
        test_freshness_and_determinism(wiki)
        test_trash_hides_relations(wiki, ids)
        test_path_independence(wiki)
        released = True
    except Exception:  # noqa: BLE001
        FAILURES.append("integration test crashed:\n" + traceback.format_exc())
    finally:
        if not released:
            wiki.release()
    try:
        test_english()
    except Exception:  # noqa: BLE001
        FAILURES.append("English test crashed:\n" + traceback.format_exc())
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES")
        for failure in FAILURES:
            print("- " + failure)
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
