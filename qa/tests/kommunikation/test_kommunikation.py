#!/usr/bin/env python3
"""Tests for e-mail, calendar and campaign helpers: crm_mail, crm_ical, crm_ingest, crm_campaign.

Run: /usr/bin/python3 tests/kommunikation/test_kommunikation.py
Creates a fresh test wiki below test-wikis/kommunikation/, generates the corpus in
code, and prints ALL OK or the failures. No network, standard library only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import traceback
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[2]
SKILL = WORKSPACE.parent / "ai-first-crm"
SCRIPTS = SKILL / "scripts"
sys.path.insert(0, str(WORKSPACE / "tools"))
sys.path.insert(0, str(SCRIPTS))

from harness import Wiki  # noqa: E402

import crm_ical  # noqa: E402
import crm_mail  # noqa: E402
from crm_messaging_objects import merged_datamodel  # noqa: E402

BASE = WORKSPACE / "test-wikis" / "kommunikation"
TARGET = BASE / "wiki"
WORK = BASE / ".work"
CORPUS = WORK / "corpus"
ACTOR = "agent/local"
FAILURES: list[str] = []
PASSED: list[str] = []


# ---------------------------------------------------------------------------
# helpers


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def run(script: str, *args, expect=(0,), token: str = "", isolated: bool = False) -> dict:
    command = [sys.executable] + (["-I"] if isolated else []) + [str(SCRIPTS / script), *map(str, args)]
    if token:
        command += ["--lock-token", token]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode not in expect:
        raise AssertionError(f"{script} {' '.join(map(str, args[:3]))} exit {completed.returncode}:\n{completed.stdout[-3000:]}\n{completed.stderr[-3000:]}")
    try:
        result = json.loads(completed.stdout) if completed.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{script}: no JSON output: {completed.stdout[-1000:]} {completed.stderr[-1000:]}") from exc
    result["_exit"] = completed.returncode
    return result


class Env:
    wiki: Wiki
    token: str
    ids: dict[str, str] = {}

    def ingest(self, name: str, *files, expect=(0,), **options) -> tuple[dict, Path]:
        plan = WORK / f"{name}.plan.json"
        args = ["plan", "--target", TARGET, "--actor", ACTOR, "--output", plan, "--files", *files]
        for key, value in options.items():
            flag = "--" + key.replace("_", "-")
            if value is True:
                args.append(flag)
            elif value not in (None, False):
                args += [flag, value]
        return run("crm_ingest.py", *args, expect=expect, token=self.token), plan

    def apply_ingest(self, report: dict, plan: Path, expect=(0,)) -> dict:
        return run("crm_ingest.py", "apply", "--target", TARGET, "--plan-file", plan, "--expect-plan-sha256", report["plan_sha256"], expect=expect, token=self.token)

    def transact(self, name: str, operations: list, origin=None) -> dict:
        report, result = self.wiki.transact({"actor": ACTOR, "origin": origin or {"kind": "manual"}, "operations": operations}, WORK / name)
        check(report.get("state") == "planned", f"transaction {name} not planned: {report}")
        check(result and result.get("state") == "applied", f"transaction {name} not applied: {result}")
        return result

    def records(self, object_dir: str) -> dict[str, dict]:
        from crm_contract import parse_record_text

        found = {}
        directory = TARGET / "records" / object_dir
        for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
            data, richtext = parse_record_text(path.read_text(encoding="utf-8"), path.name)
            data["_richtext"] = richtext
            found[path.stem] = data
        return found

    def find(self, object_dir: str, key: str, value) -> dict:
        hits = [data for data in self.records(object_dir).values() if data.get(key) == value]
        check(len(hits) == 1, f"expected one {object_dir} with {key}={value!r}, found {len(hits)}")
        return hits[0]


ENV = Env()


def mail(sender, to, subject, body="", *, message_id=None, cc=None, bcc=None, date_value=None, html=None,
         headers=None, attachments=None, calendar=None, in_reply_to=None) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = subject
    message["Date"] = format_datetime(date_value or datetime(2026, 10, 6, 9, 30, tzinfo=timezone.utc))
    if message_id:
        message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    for key, value in (headers or {}).items():
        message[key] = value
    if html is not None and not body:
        message.set_content(html, subtype="html")
    else:
        message.set_content(body)
        if html is not None:
            message.add_alternative(html, subtype="html")
    if calendar is not None:
        message.add_attachment(calendar.encode("utf-8"), maintype="text", subtype="calendar",
                               filename="invite.ics", params={"method": "REQUEST"})
    for name, data in attachments or []:
        message.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return message.as_bytes()


def write(name: str, data) -> Path:
    path = CORPUS / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhbm5hQGZpcm1hLmV4YW1wbGUiLCJsaXN0IjoibmV3cyJ9.Q2hlY2tTdW1tZVNpZ25hdHVyZTEyMzQ1Ng"

VTIMEZONE_WEST = """BEGIN:VTIMEZONE
TZID:W. Europe Standard Time
BEGIN:STANDARD
DTSTART:16010101T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE"""


def ics(body: str, method: str = "PUBLISH", extra: str = "") -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Test//Kommunikation//DE", f"METHOD:{method}"]
    if extra:
        lines.append(extra)
    lines += [body, "END:VCALENDAR"]
    return "\r\n".join("\r\n".join(lines).split("\n")) + "\r\n"


def kickoff(sequence: int, *, status: str = "CONFIRMED", description: str = "") -> str:
    lines = [
        "BEGIN:VEVENT", "UID:kickoff-1@firma.example", f"SEQUENCE:{sequence}", f"DTSTAMP:2026100{1 + sequence}T080000Z",
        "DTSTART;TZID=Europe/Berlin:20261020T140000", "DTEND;TZID=Europe/Berlin:20261020T150000",
        "SUMMARY:Kickoff Relaunch", "ORGANIZER;CN=Anna Admin:mailto:anna@firma.example",
        "ATTENDEE;CN=Eva Kunde;PARTSTAT=NEEDS-ACTION:mailto:eva@kunde.example", f"STATUS:{status}",
    ]
    if description:
        lines.append("DESCRIPTION:" + description)
    lines.append("END:VEVENT")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# setup


def setup() -> None:
    if BASE.exists():
        shutil.rmtree(BASE)
    BASE.mkdir(parents=True)
    CORPUS.mkdir(parents=True)
    made = subprocess.run([sys.executable, str(WORKSPACE / "tools" / "make_test_wiki.py"), str(SKILL), str(TARGET)], capture_output=True, text=True)
    check(made.returncode == 0, f"make_test_wiki failed: {made.stdout} {made.stderr}")
    ENV.wiki = Wiki(TARGET, BASE / ".tokens")
    acquired = ENV.wiki.acquire("kommunikation-test")
    check(acquired.get("acquired") is True, f"lock not acquired: {acquired}")
    ENV.token = ENV.wiki.token_file.read_text(encoding="utf-8").strip()
    init = ENV.wiki.locked("crm_init.py", "--target", TARGET)
    check(init.get("state") == "initialized", f"crm_init failed: {init}")
    datamodel_path = TARGET / "schema/crm/datamodel.json"
    merged, added = merged_datamodel(json.loads(datamodel_path.read_text(encoding="utf-8")), "de")
    # The standard model now ships the campaign objects; merging adds them only to older data models.
    check(all(name in merged["objects"] for name in ("messageList", "messageListMember", "messageSuppression", "messageCampaign")),
          f"campaign objects missing after merge: {added}")
    datamodel_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    check({"iCalSequence", "externalCreatedAt", "externalUpdatedAt", "conferenceSolution"} <= set(merged["objects"]["calendarEvent"]["fields"]),
          "calendarEvent lacks the fields of the current standard model")
    settings_path = TARGET / "schema/crm/settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["email"]["blocklist"] = ["@wettbewerber.example", "spam@irgendwo.example"]
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ENV.transact("base", [
        {"op": "create", "object": "workspaceMember", "ref": "anna", "values": {"name": {"firstName": "Anna", "lastName": "Admin"}, "userEmail": "anna@firma.example"}},
        {"op": "create", "object": "workspaceMember", "ref": "ben", "values": {"name": {"firstName": "Ben", "lastName": "Berater"}, "userEmail": "ben@firma.example"}},
        {"op": "create", "object": "company", "ref": "kunde", "values": {"name": "Kunde GmbH", "domainName": "https://kunde.example"}},
        {"op": "create", "object": "person", "ref": "eva", "values": {"name": {"firstName": "Eva", "lastName": "Kunde"}, "emails": {"primaryEmail": "Eva@Kunde.example", "additionalEmails": ["eva.privat@kunde.example"]}, "company": {"ref": "kunde"}}},
        {"op": "create", "object": "person", "ref": "max", "values": {"name": {"firstName": "Max", "lastName": "Muster"}, "emails": "max@kunde.example", "company": {"ref": "kunde"}, "jobTitle": "Einkauf"}},
        {"op": "create", "object": "person", "ref": "ohne", "values": {"name": {"firstName": "Otto", "lastName": "Ohnemail"}}},
        {"op": "create", "object": "person", "ref": "bounce", "values": {"name": {"firstName": "Bea", "lastName": "Bounce"}, "emails": "bounce@kunde.example"}},
        {"op": "create", "object": "person", "ref": "unsub", "values": {"name": {"firstName": "Uwe", "lastName": "Ab"}, "emails": "uwe@kunde.example"}},
        {"op": "create", "object": "person", "ref": "spion", "values": {"name": {"firstName": "Sven", "lastName": "Spion"}, "emails": "sven@wettbewerber.example"}},
        {"op": "create", "object": "person", "ref": "geloescht", "values": {"name": {"firstName": "Gerd", "lastName": "Geloescht"}, "emails": "gerd@altkunde.example"}},
    ])
    gerd = ENV.find("person", "emails.primaryEmail", "gerd@altkunde.example")
    ENV.transact("trash", [{"op": "delete", "object": "person", "record": gerd["crm_id"]}])
    ENV.ids = {
        "anna": ENV.find("workspace-member", "userEmail", "anna@firma.example")["crm_id"],
        "ben": ENV.find("workspace-member", "userEmail", "ben@firma.example")["crm_id"],
        "eva": ENV.find("person", "emails.primaryEmail", "Eva@Kunde.example")["crm_id"],
        "max": ENV.find("person", "emails.primaryEmail", "max@kunde.example")["crm_id"],
    }


# ---------------------------------------------------------------------------
# library tests


def test_library_charset_and_html() -> None:
    raw = (b"From: Eva Kunde <eva@kunde.example>\r\nTo: anna@firma.example\r\nSubject: Termine\r\n"
           b"Message-ID: <lib1@kunde.example>\r\nContent-Type: text/plain; charset=utf-8\r\n"
           b"Content-Transfer-Encoding: 8bit\r\n\r\nTermine f\xfcr M\xe4rz\r\n")
    parsed = crm_mail.parse_mail(raw, "lib.eml")
    check(parsed.text == "Termine für März", f"charset fallback wrong: {parsed.text!r}")
    check("�" not in parsed.text, "replacement character in text")
    check(any("cp1252" in note for note in parsed.notes), f"fallback not noted: {parsed.notes}")
    html = ("<html><body><div style='display:none'>Vorschau</div><table><tr><th>Produkt</th><th>Preis</th></tr>"
            "<tr><td>A|B</td><td>12 &euro;</td></tr></table><p>Hallo<br>Welt <a href='https://x.example/a'>hier</a></p>"
            "<ul><li>eins<li>zwei</ul></body></html>")
    text = crm_mail.html_to_text(html)
    check("| Produkt | Preis |" in text and "| --- | --- |" in text and "| A\\|B | 12 € |" in text, f"table wrong: {text}")
    check("hier (https://x.example/a)" in text and "Hallo\nWelt" in text and "- eins\n- zwei" in text, f"html text wrong: {text}")
    check("Vorschau" not in text, "hidden preheader rendered")


def test_library_redaction() -> None:
    text = ("Link: https://portal.example/reset?token=abc123&lang=de\nZoom: https://zoom.us/j/123?pwd=XyZ987\n"
            f"Abmelden: https://news.example/u/{JWT}\nPasswort: Sommer2024\nIhr Kennwort:\n Geheim!42\n"
            "DB: postgres://admin:S3cr3tPw@db.example/x")
    cleaned, findings = crm_mail.redact(text)
    for secret in ("abc123", "XyZ987", JWT, "Sommer2024", "Geheim!42", "S3cr3tPw"):
        check(secret not in cleaned, f"{secret} not redacted: {cleaned}")
    check(cleaned.count(crm_mail.REDACTED) == 6, f"unexpected redactions: {cleaned}")
    kinds = {finding.kind for finding in findings}
    check({"url-parameter:token", "url-parameter:pwd", "json-web-token", "credential-line", "url-credentials"} <= kinds, f"kinds {kinds}")
    check(crm_mail.screen(cleaned) == [], "secret_screen still finds something")
    again, more = crm_mail.redact(cleaned)
    check(again == cleaned and not more, "redaction is not idempotent")


def test_library_domains_and_names() -> None:
    check(crm_mail.registrable_domain("mail.firma.co.uk") == "firma.co.uk", "co.uk")
    check(crm_mail.company_name_from_domain("vertrieb.firma.co.uk") == "Firma", "company name")
    check(crm_mail.registrable_domain("sales.acme.de") == "acme.de", "de")
    check(crm_mail.is_blocklisted("x@vertrieb.wettbewerber.example", ["@wettbewerber.example"]), "subdomain blocklist")
    check(not crm_mail.is_blocklisted("x@wettbewerber.example.org", ["@wettbewerber.example"]), "suffix confusion")
    check(not crm_mail.is_blocklisted("anna@wettbewerber.example", ["@wettbewerber.example"], own=["anna@wettbewerber.example"]), "own address blocklisted")
    check(crm_mail.person_name("Müller, Hans", "h@x.example") == ("Hans", "Müller"), "comma name")
    check(crm_mail.person_name("", "lena.neu+crm@neukunde.example") == ("Lena", "Neu"), "local part name")
    check(crm_mail.normalize_subject("AW: WG: Re:  Angebot") == "angebot", "subject normalization")
    check(crm_mail.is_free_email("a@gmx.de", ["gmx.de"]) and not crm_mail.is_free_email("a@kunde.example", ["gmx.de"]), "free mail")


def test_library_rrule() -> None:
    text = ics("\n".join([
        "BEGIN:VEVENT", "UID:serie-lib", "DTSTART;TZID=Europe/Berlin:20261005T100000", "DTEND;TZID=Europe/Berlin:20261005T110000",
        "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=6", "EXDATE;TZID=Europe/Berlin:20261012T100000", "SUMMARY:Jour fixe", "END:VEVENT"]))
    event = crm_ical.parse_ics(text, default_zone="Europe/Berlin").events[0]
    occurrences = crm_ical.expand_rrule(event, datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc))
    starts = [start.strftime("%m-%dT%H:%M") for start, _ in occurrences]
    check(starts == ["10-05T08:00", "10-19T08:00", "10-26T09:00", "11-02T09:00", "11-09T09:00"], f"weekly expansion {starts}")
    monthly = crm_ical.parse_rrule("FREQ=MONTHLY;INTERVAL=2;BYDAY=-1FR;UNTIL=20270401T000000Z")
    days = [value.date().isoformat() for value in crm_ical.iter_rule_starts(monthly, datetime(2026, 10, 30, 9), lambda value: value)]
    check(days == ["2026-10-30", "2026-12-25", "2027-02-26"], f"monthly {days}")
    yearly = crm_ical.parse_rrule("FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=31;COUNT=2")
    check([value.isoformat() for value in crm_ical.iter_rule_starts(yearly, date(2026, 3, 31))] == ["2026-03-31", "2027-03-31"], "yearly")
    daily = crm_ical.parse_rrule("FREQ=DAILY;INTERVAL=3;UNTIL=20261010")
    check(len(list(crm_ical.iter_rule_starts(daily, date(2026, 10, 1)))) == 4, "daily until")
    record = {"recurrence": event.recurrence_text(), "startsAt": "2026-10-05T08:00:00Z", "endsAt": "2026-10-05T09:00:00Z"}
    from_record = [start.strftime("%m-%d") for start, _ in crm_ical.expand_rrule(record, date(2026, 10, 1), date(2026, 11, 1))]
    check(from_record == ["10-05", "10-19", "10-26"], f"record expansion {from_record}")
    check(crm_ical.parse_rrule("FREQ=MONTHLY;BYSETPOS=1;BYDAY=MO").unsupported == ["BYSETPOS"], "unsupported part")


def test_library_time_zones() -> None:
    outlook = ics("\n".join([
        "BEGIN:VEVENT", "UID:tz-1", "DTSTART;TZID=W. Europe Standard Time:20261103T090000",
        "DTEND;TZID=W. Europe Standard Time:20261103T100000", "SUMMARY:Outlook", "END:VEVENT"]), extra=VTIMEZONE_WEST)
    event = crm_ical.parse_ics(outlook, default_zone="UTC").events[0]
    check(event.starts_at == datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc), f"windows zone {event.starts_at}")
    check(any("mapped to Europe/Berlin" in note for note in event.notes), f"mapping not noted {event.notes}")
    custom = outlook.replace("W. Europe Standard Time", "Mitteleuropa (eigene Zone)")
    event = crm_ical.parse_ics(custom, default_zone="UTC").events[0]
    check(event.starts_at == datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc), f"vtimezone offsets {event.starts_at}")
    check(any("embedded VTIMEZONE" in note for note in event.notes), "vtimezone not noted")
    unknown = ics("BEGIN:VEVENT\nUID:tz-3\nDTSTART;TZID=Nirgendwo:20260710T120000\nSUMMARY:x\nEND:VEVENT")
    event = crm_ical.parse_ics(unknown, default_zone="Europe/Berlin").events[0]
    check(event.starts_at == datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc) and any("unknown" in note for note in event.notes), "local time fallback")
    ambiguous = ics("BEGIN:VEVENT\nUID:tz-4\nDTSTART;TZID=Europe/Berlin:20261025T023000\nSUMMARY:x\nEND:VEVENT")
    event = crm_ical.parse_ics(ambiguous, default_zone="UTC").events[0]
    check(event.starts_at == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc) and any("occurs twice" in note for note in event.notes), "ambiguous time")
    allday = ics("BEGIN:VEVENT\nUID:tz-5\nDTSTART;VALUE=DATE:20261010\nDTEND;VALUE=DATE:20261012\nSUMMARY:Messe\nEND:VEVENT")
    event = crm_ical.parse_ics(allday).events[0]
    check(event.all_day and event.starts_at == datetime(2026, 10, 10, tzinfo=timezone.utc) and event.ends_at == datetime(2026, 10, 12, tzinfo=timezone.utc), "all day")


# ---------------------------------------------------------------------------
# ingest tests


def test_ingest_text_html_charset() -> None:
    text_mail = write("mails/01-text.eml", mail(
        "Anna Admin <anna@firma.example>", "Eva Kunde <eva@kunde.example>", "Angebot Website-Relaunch",
        "Hallo Eva,\n\nanbei das Angebot.\n\nGruß\nAnna", message_id="<m1@firma.example>",
        attachments=[("Angebot.pdf", b"%PDF-1.4 " + b"x" * 3000)]))
    html_mail = write("mails/02-html.eml", mail(
        "Eva Kunde <eva@kunde.example>", "anna@firma.example", "AW: Angebot Website-Relaunch",
        html="<html><body><p>Danke!</p><table><tr><th>Posten</th><th>Betrag</th></tr><tr><td>Design</td><td>4.000 &euro;</td></tr></table>"
             "<p>Details: <a href='https://kunde.example/projekt'>Projektseite</a></p></body></html>",
        message_id="<m2@kunde.example>", in_reply_to="<m1@firma.example>"))
    charset_mail = write("mails/03-charset.eml",
                         b"From: Eva Kunde <eva.privat@kunde.example>\r\nTo: anna@firma.example\r\nSubject: Termine\r\n"
                         b"Date: Tue, 06 Oct 2026 11:00:00 +0200\r\nMessage-ID: <m3@kunde.example>\r\n"
                         b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\nTermine f\xfcr M\xe4rz\r\n")
    report, plan = ENV.ingest("t1", text_mail, html_mail, charset_mail, mailbox_owner="anna@firma.example")
    check(report["state"] == "planned", f"state {report['state']}: {report.get('errors')} {report.get('plan_errors')}")
    check(report["counts"]["imported"] == 3, f"imported {report['counts']}")
    applied = ENV.apply_ingest(report, plan)
    check(applied["state"] == "applied", f"apply {applied}")
    first = ENV.find("message", "headerMessageId", "<m1@firma.example>")
    check(first["direction"] == "OUTGOING" and first["subject"] == "Angebot Website-Relaunch", f"first {first}")
    check(first["messageThreadId"] == "<m1@firma.example>" and first["receivedAt"] == "2026-10-06T09:30:00Z", "thread/received")
    check(f"records/person/{ENV.ids['eva']}|" in " ".join(first["participants"]), "eva not linked")
    check(f"records/workspace-member/{ENV.ids['anna']}|" in " ".join(first["participants"]), "anna not linked")
    check("Anhänge:\n- Angebot.pdf (3 KB)" in first["_richtext"]["text"], f"attachment list {first['_richtext']}")
    second = ENV.find("message", "headerMessageId", "<m2@kunde.example>")
    check(second["direction"] == "INCOMING" and second["messageThreadId"] == "<m1@firma.example>", "reply thread")
    check("| Posten | Betrag |" in second["_richtext"]["text"] and "Projektseite (https://kunde.example/projekt)" in second["_richtext"]["text"], second["_richtext"])
    third = ENV.find("message", "headerMessageId", "<m3@kunde.example>")
    check(third["_richtext"]["text"] == "Termine für März", f"charset {third['_richtext']}")
    check(f"records/person/{ENV.ids['eva']}|" in " ".join(third["participants"]), "additional email not matched")
    check(any("cp1252" in entry["note"] for entry in report["notes"]), "charset note missing")
    events = [json.loads(line) for line in (TARGET / "meta/crm-events").glob("*.jsonl").__next__().read_text(encoding="utf-8").splitlines()]
    origins = [event["origin"] for event in events if event["record_id"] == first["crm_id"]]
    check(origins and origins[0]["kind"] == "email" and origins[0]["ref"] == "message-id:m1@firma.example", f"origin {origins}")
    check(first["crm_created_source"] == "EMAIL" and first.get("isDraft") is False, "created source or draft flag")
    annotations = json.loads(plan.read_text(encoding="utf-8")).get("annotations", {})
    check(annotations.get("ingest", {}).get("format") == "lmwiki-crm-ingest/1" and annotations["ingest"]["counts"]["imported"] == 3, "ingest report not in the plan")


def test_ingest_bulk_and_redaction() -> None:
    newsletter = write("mails/04-newsletter.eml", mail(
        "Shop Newsletter <newsletter@shop.example>", "anna@firma.example", "Herbstangebote",
        html=f"<p>Neu im Shop</p><p><a href='https://shop.example/u/{JWT}'>Abmelden</a> <a href='https://shop.example/l?token=t0k3n'>Konto</a></p>",
        message_id="<news1@shop.example>",
        headers={"List-Unsubscribe": f"<https://shop.example/u/{JWT}>", "List-Id": "Shop News <news.shop.example>"}))
    password = write("mails/05-passwort.eml", mail(
        "Bernd IT <bernd@dienstleister.example>", "anna@firma.example", "Ihr Zugang",
        "Hallo Anna,\n\nBenutzer: anna\nPasswort: Sommer2024\n\nBitte nach der Anmeldung ändern.", message_id="<pw1@dienstleister.example>"))
    report, plan = ENV.ingest("t2", newsletter, password, mailbox_owner="anna@firma.example")
    check(report["state"] == "planned", f"state {report}")
    skipped = {entry["item"]: entry["reason"] for entry in report["skipped"]}
    check(skipped.get("04-newsletter.eml") == "bulk", f"newsletter not skipped as bulk: {skipped}")
    check(report["counts"]["imported"] == 1, "password mail not planned")
    plan_text = plan.read_text(encoding="utf-8")
    check("Sommer2024" not in plan_text, "password in plan")
    check(any(entry["kind"] == "credential-line" and entry["field"] == "text" for entry in report["redactions"]), f"redaction not reported {report['redactions']}")
    check("Sommer2024" not in json.dumps(report), "password in report")
    ENV.apply_ingest(report, plan)
    record = ENV.find("message", "headerMessageId", "<pw1@dienstleister.example>")
    check("Passwort: [credential removed]" in record["_richtext"]["text"], record["_richtext"])
    report, plan = ENV.ingest("t2b", newsletter, mailbox_owner="anna@firma.example", include_bulk=True)
    check(report["counts"]["imported"] == 1, f"newsletter with --include-bulk {report['counts']} {report['errors']}")
    plan_text = plan.read_text(encoding="utf-8")
    check(JWT not in plan_text and "t0k3n" not in plan_text, "JWT or token left in plan")
    kinds = {entry["kind"] for entry in report["redactions"]}
    check({"json-web-token", "url-parameter:token"} <= kinds, f"kinds {kinds}")
    ENV.apply_ingest(report, plan)
    news = ENV.find("message", "headerMessageId", "<news1@shop.example>")
    check("Abmelden (https://shop.example/u/[credential removed])" in news["_richtext"]["text"], news["_richtext"])


def test_ingest_two_mailboxes() -> None:
    def copy(received: str, bcc=None) -> bytes:
        data = mail("Anna Admin <anna@firma.example>", "Eva Kunde <eva@kunde.example>, Ben Berater <ben@firma.example>",
                    "Projektplan", "Der Projektplan im Überblick.", message_id="<m6@firma.example>", bcc=bcc)
        return f"Received: from {received} by mx.firma.example; Tue, 06 Oct 2026 09:31:00 +0000\r\n".encode() + data

    anna_box = write("mailboxes/anna.mbox", b"From anna@firma.example Tue Oct  6 09:30:00 2026\n" + copy("anna-pc", bcc="max@kunde.example").replace(b"\r\n", b"\n") + b"\n")
    ben_copy = write("mailboxes/ben-kopie.eml", copy("relay"))
    report, plan = ENV.ingest("t3", anna_box, ben_copy, mailbox_owner="anna@firma.example")
    check(report["counts"]["imported"] == 1 and report["counts"]["merged"] == 1, f"two copies in one run {report['counts']}")
    check(any(entry.get("into_item") == "anna.mbox#1" for entry in report["merged"]), f"merge not reported {report['merged']}")
    ENV.apply_ingest(report, plan)
    record = ENV.find("message", "headerMessageId", "<m6@firma.example>")
    joined = " ".join(record["participants"])
    check(f"records/person/{ENV.ids['max']}|" in joined and f"records/workspace-member/{ENV.ids['ben']}|" in joined, "bcc or member not linked")
    report, plan = ENV.ingest("t3b", ben_copy, mailbox_owner="ben@firma.example")
    check(report["state"] == "nothing_to_import" and report["counts"]["unchanged"] == 1 and "summary" not in report,
          f"second mailbox {report['state']} {report['counts']}")
    check(record == ENV.find("message", "headerMessageId", "<m6@firma.example>"), "second mailbox changed the record")
    check(len(ENV.records("message")) == 6, "message duplicated")


def test_ingest_invitation_and_calendar() -> None:
    invitation = write("mails/07-einladung.eml", mail(
        "Anna Admin <anna@firma.example>", "Eva Kunde <eva@kunde.example>", "Einladung: Kickoff Relaunch",
        "Hallo Eva,\n\nwie besprochen die Einladung zum Kickoff.\nAgenda: Ziele, Zeitplan.", message_id="<inv1@firma.example>",
        calendar=ics(kickoff(0, description="Agenda: Ziele\\, Zeitplan."), method="REQUEST")))
    outlook = write("kalender/outlook.ics", ics("\n".join([
        "BEGIN:VEVENT", "UID:040000008200E00074C5B7101A82E00800000000OUTLOOK1", "SEQUENCE:0", "DTSTAMP:20261001T080000Z",
        "CREATED:20260920T101500Z", "LAST-MODIFIED:20260921T090000Z",
        "DTSTART;TZID=W. Europe Standard Time:20261103T090000", "DTEND;TZID=W. Europe Standard Time:20261103T100000",
        "SUMMARY;LANGUAGE=de-DE:Workshop", "LOCATION:Raum 1", "ORGANIZER;CN=\"Kunde, Eva\":mailto:eva@kunde.example",
        "ATTENDEE;CN=Anna Admin:mailto:anna@firma.example",
        "DESCRIPTION:Teams: https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc\\nKenncode: hX7kQ2", "END:VEVENT"]),
        method="REQUEST", extra=VTIMEZONE_WEST))
    series = write("kalender/serie.ics", ics("\n".join([
        "BEGIN:VEVENT", "UID:serie-1@firma.example", "DTSTAMP:20261001T080000Z", "DTSTART;TZID=Europe/Berlin:20261005T100000",
        "DTEND;TZID=Europe/Berlin:20261005T110000", "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=6",
        "EXDATE;TZID=Europe/Berlin:20261012T100000", "SUMMARY:Jour fixe Kunde",
        "ORGANIZER:mailto:anna@firma.example", "ATTENDEE:mailto:max@kunde.example", "END:VEVENT"])))
    report, plan = ENV.ingest("t4", invitation, outlook, series, mailbox_owner="anna@firma.example")
    check(report["state"] == "planned" and report["counts"]["imported"] == 3, f"calendar import {report['counts']} {report['errors']}")
    check(any(entry.get("into") == "calendarEvent" and entry["iCalUid"] == "kickoff-1@firma.example" for entry in report["merged"]), "invitation merge not reported")
    check(not any(entry["key"] == "<inv1@firma.example>" for entry in report["imported"]), "invitation imported as message")
    ENV.apply_ingest(report, plan)
    kick = ENV.find("calendar-event", "iCalUid", "kickoff-1@firma.example")
    check(kick["startsAt"] == "2026-10-20T12:00:00Z" and kick["endsAt"] == "2026-10-20T13:00:00Z", f"kickoff times {kick}")
    check(kick["_richtext"]["description"].startswith("Hallo Eva,") and kick.get("isCanceled") is False, f"description {kick['_richtext']}")
    check(kick["organizerHandle"] == "anna@firma.example" and f"records/person/{ENV.ids['eva']}|" in " ".join(kick["participants"]), "kickoff participants")
    work = ENV.find("calendar-event", "iCalUid", "040000008200E00074C5B7101A82E00800000000OUTLOOK1")
    check(work["startsAt"] == "2026-11-03T08:00:00Z", f"outlook tz {work['startsAt']}")
    check(work["conferenceLink.primaryLinkUrl"].startswith("https://teams.microsoft.com/") and work["conferenceSolution"] == "teamsForBusiness", "conference link")
    check(work["iCalSequence"] == 0 and work["externalCreatedAt"] == "2026-09-20T10:15:00Z" and work["externalUpdatedAt"] == "2026-09-21T09:00:00Z", f"external times {work}")
    events = [json.loads(line) for shard in (TARGET / "meta/crm-events").glob("*.jsonl") for line in shard.read_text(encoding="utf-8").splitlines()]
    kick_origin = next(event["origin"] for event in events if event["record_id"] == kick["crm_id"])
    check(kick_origin["kind"] == "calendar" and kick_origin["ref"] == "ical-uid:kickoff-1@firma.example"
          and kick_origin.get("note") == "invitation message-id:inv1@firma.example", f"calendar origin {kick_origin}")
    check(not any("sequence=" in str(event.get("origin", {}).get("note", "")) for event in events), "version still kept in origin notes")
    check("hX7kQ2" not in work["_richtext"]["description"] and "Kenncode: [credential removed]" in work["_richtext"]["description"], "teams passcode not redacted")
    weekly = ENV.find("calendar-event", "iCalUid", "serie-1@firma.example")
    check(weekly["recurrence"].splitlines() == ["DTSTART;TZID=Europe/Berlin:20261005T100000", "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=6",
                                                 "EXDATE;TZID=Europe/Berlin:20261012T100000"], f"recurrence {weekly['recurrence']}")
    occurrences = crm_ical.expand_rrule(weekly, date(2026, 10, 1), date(2026, 12, 31))
    check(len(occurrences) == 5 and occurrences[2][0] == datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc), f"series {occurrences}")
    # A manual correction survives a re-import of the same version: fill mode only writes empty fields.
    ENV.transact("manual-fix", [{"op": "update", "object": "calendarEvent", "record": work["crm_id"],
                                 "values": {"isFullDay": True, "location": "Raum 2"}}])
    report, _ = ENV.ingest("t4b", outlook, mailbox_owner="anna@firma.example")
    check(report["state"] == "nothing_to_import" and report["counts"]["unchanged"] == 1, f"same version changed the event {report['counts']}")
    work = ENV.find("calendar-event", "iCalUid", "040000008200E00074C5B7101A82E00800000000OUTLOOK1")
    check(work["isFullDay"] is True and work["location"] == "Raum 2", "manual correction overwritten")


def test_ingest_cancel_and_older_sequence() -> None:
    # A cancellation often names only the organizer; it must still reach the known event.
    cancel = write("kalender/absage.ics", ics(kickoff(1, status="CANCELLED").replace(
        "ATTENDEE;CN=Eva Kunde;PARTSTAT=NEEDS-ACTION:mailto:eva@kunde.example\n", ""), method="CANCEL"))
    report, plan = ENV.ingest("t5", cancel, mailbox_owner="anna@firma.example")
    check(report["counts"]["updated"] == 1, f"cancel not an update {report['counts']} {report['skipped']}")
    ENV.apply_ingest(report, plan)
    kick = ENV.find("calendar-event", "iCalUid", "kickoff-1@firma.example")
    check(kick["isCanceled"] is True and kick["iCalSequence"] == 1, f"not canceled or sequence not stored {kick}")
    check(kick["_richtext"].get("description", "").startswith("Hallo Eva,") and kick["title"] == "Kickoff Relaunch", "cancellation erased details")
    check(len([data for data in ENV.records("calendar-event").values() if data.get("iCalUid") == "kickoff-1@firma.example"]) == 1, "event duplicated")
    older = write("kalender/alt.ics", ics(kickoff(0), method="REQUEST"))
    report, plan = ENV.ingest("t5b", older, mailbox_owner="anna@firma.example")
    check(report["state"] == "nothing_to_import", f"older version planned {report['state']}")
    check(any(entry["reason"] == "older_version" for entry in report["skipped"]), f"older not reported {report['skipped']}")
    unknown_cancel = write("kalender/unbekannt.ics", ics("BEGIN:VEVENT\nUID:nie-da\nSEQUENCE:3\nDTSTART:20261101T100000Z\nSTATUS:CANCELLED\nSUMMARY:x\nEND:VEVENT"))
    reply = write("kalender/antwort.ics", ics(kickoff(1).replace("NEEDS-ACTION", "ACCEPTED"), method="REPLY"))
    report, _ = ENV.ingest("t5c", unknown_cancel, reply, mailbox_owner="anna@firma.example")
    reasons = {entry["item"]: entry["reason"] for entry in report["skipped"]}
    check(reasons.get("unbekannt.ics") == "cancellation_unknown_event" and reasons.get("antwort.ics") == "invitation_reply", f"reasons {reasons}")


def test_ingest_msg_blocklist_internal_private() -> None:
    msg = write("mails/outlook.msg", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
    spion = write("mails/08-wettbewerber.eml", mail("Spion <spion@vertrieb.wettbewerber.example>", "anna@firma.example",
                                                     "Anfrage", "Hallo", message_id="<sp1@wettbewerber.example>"))
    intern = write("mails/09-intern.eml", mail("anna@firma.example", "ben@firma.example", "Mittag?", "12 Uhr?", message_id="<int1@firma.example>"))
    info = write("mails/10-info.eml", mail("info@lieferant.example", "anna@firma.example", "Katalog", "Unser Katalog", message_id="<info1@lieferant.example>"))
    private = write("kalender/privat.ics", ics("BEGIN:VEVENT\nUID:privat-1\nCLASS:PRIVATE\nDTSTART:20261101T100000Z\nSUMMARY:Arzt\nATTENDEE:mailto:eva@kunde.example\nEND:VEVENT"))
    report, _ = ENV.ingest("t6", msg, spion, intern, info, private, mailbox_owner="anna@firma.example")
    reasons = {entry["item"]: entry["reason"] for entry in report["skipped"]}
    check(reasons == {"outlook.msg": "outlook_msg", "08-wettbewerber.eml": "blocklist", "09-intern.eml": "internal",
                      "10-info.eml": "group_sender", "privat.ics": "private_event"}, f"reasons {reasons}")
    check(".eml" in next(entry["detail"] for entry in report["skipped"] if entry["item"] == "outlook.msg"), "msg hint missing")
    report, _ = ENV.ingest("t6b", intern, info, private, mailbox_owner="anna@firma.example", include_internal=True, include_bulk=True, include_private=True)
    check(report["counts"]["imported"] == 3 and not report["skipped"], f"switches {report['counts']} {report['skipped']}")


def test_ingest_contacts_and_visibility() -> None:
    contacts = write("mails/11-kontakte.eml", mail(
        "Anna Admin <anna@firma.example>", "Lena Neu <lena.neu@neukunde.example>",
        "Erstkontakt", "Hallo Frau Neu,\n\nwie telefonisch besprochen.", message_id="<c1@firma.example>",
        cc="Privat Person <privat.person@gmx.de>, info@neukunde.example, Gerd <gerd@altkunde.example>, Kollege <k.ollege@sub.firma.example>"))
    report, plan = ENV.ingest("t7", contacts, mailbox_owner="anna@firma.example", own_addresses="anna.admin@firma.example")
    suggestions = report["contact_suggestions"]
    people = {entry["email"]: entry for entry in suggestions["people"]}
    declined = {entry["email"]: entry["reason"] for entry in suggestions["not_suggested"]}
    check(set(people) == {"lena.neu@neukunde.example"}, f"people {people}")
    check(people["lena.neu@neukunde.example"]["firstName"] == "Lena" and people["lena.neu@neukunde.example"]["company"]["domain"] == "neukunde.example", "proposal content")
    check([entry["domain"] for entry in suggestions["companies"]] == ["neukunde.example"], f"companies {suggestions['companies']}")
    check(declined == {"privat.person@gmx.de": "free_email", "info@neukunde.example": "group_address",
                       "gerd@altkunde.example": "person_in_trash", "k.ollege@sub.firma.example": "internal"}, f"declined {declined}")
    check(report["summary"] == {"create": 1}, f"contacts planned without --create-contacts {report['summary']}")
    incoming = write("mails/11b-eingehend.eml", mail(
        "Lena Kraus <lena.kraus@neukunde2.example>", "Anna Admin <anna@firma.example>", "Anfrage Workshop",
        "Guten Tag, wir interessieren uns für einen Workshop.", message_id="<in1@neukunde2.example>", cc="anna.admin@firma.example"))
    report, _ = ENV.ingest("t7a", contacts, incoming, mailbox_owner="anna@firma.example", own_addresses="anna.admin@firma.example")
    unmatched = {entry["email"]: entry for entry in report["unmatched_participants"]}
    check(unmatched["lena.kraus@neukunde2.example"]["reason"] == "policy" and unmatched["lena.kraus@neukunde2.example"]["name"] == "Lena Kraus"
          and unmatched["lena.kraus@neukunde2.example"]["messages"] == 1 and "sent from own addresses" in unmatched["lena.kraus@neukunde2.example"]["detail"],
          f"incoming unknown sender {unmatched.get('lena.kraus@neukunde2.example')}")
    reasons = {email: entry["reason"] for email, entry in unmatched.items()}
    check(reasons == {"lena.kraus@neukunde2.example": "policy", "anna.admin@firma.example": "own_address", "lena.neu@neukunde.example": "proposed",
                      "privat.person@gmx.de": "free_email", "info@neukunde.example": "group_address", "gerd@altkunde.example": "person_in_trash",
                      "k.ollege@sub.firma.example": "internal"}, f"unmatched reasons {reasons}")
    check(report["counts"]["unmatched_participants"] == 7, "unmatched count")
    annotated = json.loads((WORK / "t7a.plan.json").read_text(encoding="utf-8"))["annotations"]["ingest"]
    check(len(annotated["unmatched_participants"]) == 7, "unmatched participants not in the plan annotations")
    report, plan = ENV.ingest("t7b", contacts, mailbox_owner="anna@firma.example", create_contacts=True)
    check(report["summary"] == {"create": 3}, f"summary with contacts {report['summary']}")
    ENV.apply_ingest(report, plan)
    lena = ENV.find("person", "emails.primaryEmail", "lena.neu@neukunde.example")
    company = ENV.find("company", "domainName.primaryLinkUrl", "https://neukunde.example")
    check(lena["crm_created_source"] == "EMAIL" and company["name"] == "Neukunde" and f"records/company/{company['crm_id']}|" in lena["company"], "contact records")
    message = ENV.find("message", "headerMessageId", "<c1@firma.example>")
    check(f"records/person/{lena['crm_id']}|" in " ".join(message["participants"]), "new person not linked")
    hidden = write("mails/12-metadata.eml", mail("Eva Kunde <eva@kunde.example>", "anna@firma.example", "Vertraulich: Budget",
                                                  "Budget 2027 ist freigegeben.", message_id="<meta1@kunde.example>"))
    report, plan = ENV.ingest("t7c", hidden, mailbox_owner="anna@firma.example", visibility="METADATA")
    check(report["imported"][0]["title"] == "(hidden by visibility)", "title shown despite METADATA")
    ENV.apply_ingest(report, plan)
    record = ENV.find("message", "headerMessageId", "<meta1@kunde.example>")
    check("subject" not in record and not record["_richtext"] and record["visibility"] == "METADATA", f"metadata record {record}")
    check("Budget" not in (TARGET / "records/message" / f"{record['crm_id']}.md").read_text(encoding="utf-8"), "content leaked")
    report, _ = ENV.ingest("t7d", hidden, mailbox_owner="anna@firma.example", visibility="SHARE_EVERYTHING")
    check(report["state"] == "nothing_to_import", "re-import widened the visibility")


def test_ingest_isolated_and_errors() -> None:
    broken = write("kalender/kaputt.ics", "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:ohne Start\nEND:VEVENT\nEND:VCALENDAR\n")
    good = write("mails/13-gut.eml", mail("Eva Kunde <eva@kunde.example>", "anna@firma.example", "Kurz", "Kurze Frage.", message_id="<ok1@kunde.example>"))
    report, plan = ENV.ingest("t8", broken, good, mailbox_owner="anna@firma.example", expect=(1,))
    check(report["state"] == "invalid" and report["errors"], f"broken file not blocking {report['state']}")
    applied = ENV.apply_ingest(report, plan, expect=(4,))
    check(applied["state"] == "invalid_plan", f"invalid plan applied {applied}")
    report, plan = ENV.ingest("t8b", broken, good, mailbox_owner="anna@firma.example", skip_failed=True)
    check(report["state"] == "planned" and report["counts"]["imported"] == 1 and report["counts"]["errors"] == 1, f"skip failed {report['counts']}")
    isolated = run("crm_ingest.py", "plan", "--target", TARGET, "--actor", ACTOR, "--output", WORK / "t8c.plan.json",
                   "--files", good, "--mailbox-owner", "anna@firma.example", token=ENV.token, isolated=True)
    check(isolated["state"] == "planned", "python -I run failed")
    inside = run("crm_ingest.py", "plan", "--target", TARGET, "--actor", ACTOR, "--output", TARGET / "plan.json", "--files", good,
                 token=ENV.token, expect=(2,))
    check("outside the wiki" in inside["error"], "plan inside wiki accepted")


def test_ingest_series_exceptions() -> None:
    series = write("kalender/serie-ausnahmen.ics", ics("\n".join([
        "BEGIN:VEVENT", "UID:serie-2@firma.example", "DTSTAMP:20261001T080000Z", "DTSTART;TZID=Europe/Berlin:20261103T150000",
        "DTEND;TZID=Europe/Berlin:20261103T160000", "RRULE:FREQ=WEEKLY;COUNT=4", "SUMMARY:Status Kunde",
        "ORGANIZER:mailto:anna@firma.example", "ATTENDEE:mailto:eva@kunde.example", "END:VEVENT",
        "BEGIN:VEVENT", "UID:serie-2@firma.example", "RECURRENCE-ID;TZID=Europe/Berlin:20261110T150000", "DTSTAMP:20261001T080000Z",
        "DTSTART;TZID=Europe/Berlin:20261110T160000", "DTEND;TZID=Europe/Berlin:20261110T170000", "SUMMARY:Status Kunde (verschoben)",
        "ORGANIZER:mailto:anna@firma.example", "ATTENDEE:mailto:eva@kunde.example", "END:VEVENT",
        "BEGIN:VEVENT", "UID:serie-2@firma.example", "RECURRENCE-ID;TZID=Europe/Berlin:20261117T150000", "DTSTAMP:20261001T080000Z",
        "DTSTART;TZID=Europe/Berlin:20261117T150000", "STATUS:CANCELLED", "SUMMARY:Status Kunde",
        "ORGANIZER:mailto:anna@firma.example", "END:VEVENT"])))
    report, plan = ENV.ingest("t9", series, mailbox_owner="anna@firma.example")
    check(report["counts"]["imported"] == 3, f"series with exceptions {report['counts']} {report['skipped']}")
    ENV.apply_ingest(report, plan)
    moved = ENV.find("calendar-event", "iCalUid", "serie-2@firma.example#20261110T140000Z")
    check(moved["startsAt"] == "2026-11-10T15:00:00Z" and moved["recurrence"] == "RECURRENCE-ID:20261110T140000Z", f"exception {moved}")
    canceled = ENV.find("calendar-event", "iCalUid", "serie-2@firma.example#20261117T140000Z")
    check(canceled["isCanceled"] is True, "canceled exception")
    records = [data for data in ENV.records("calendar-event").values() if str(data.get("iCalUid", "")).startswith("serie-2@")]
    occurrences = crm_ical.occurrences_for_records(records, date(2026, 11, 1), date(2026, 12, 1))
    starts = [(entry["start"].strftime("%m-%dT%H:%M"), entry["record"]["title"]) for entry in occurrences]
    check(starts == [("11-03T14:00", "Status Kunde"), ("11-10T15:00", "Status Kunde (verschoben)"), ("11-24T14:00", "Status Kunde")], f"occurrences {starts}")
    audience = run("crm_campaign.py", "audience", "--target", TARGET, "--filter", json.dumps({"field": "jobTitle", "operand": "IS", "value": "Einkauf"}),
                   token=ENV.token, isolated=True)
    check(audience["recipients"] == 1, f"python -I campaign run {audience}")


# ---------------------------------------------------------------------------
# campaign tests


def campaign(*args, expect=(0,)) -> dict:
    return run("crm_campaign.py", args[0], "--target", TARGET, *args[1:], expect=expect, token=ENV.token)


def apply_campaign(report: dict, plan: Path) -> dict:
    result = campaign("apply", "--plan-file", plan, "--expect-plan-sha256", report["plan_sha256"])
    check(result["state"] == "applied", f"campaign plan not applied: {result}")
    return result


AUDIENCE_FILTER = json.dumps({"op": "OR", "conditions": [
    {"field": "emails", "operand": "CONTAINS", "value": "kunde.example"},
    {"field": "name", "operand": "CONTAINS", "value": "Otto"},
    {"field": "emails", "operand": "CONTAINS", "value": "wettbewerber"},
]})


def test_campaign_results_and_audience() -> None:
    results = write("kampagne/ergebnisse.csv", "email;status\nbounce@kunde.example;bounced\nuwe@kunde.example;unsubscribed\n"
                    "UWE@kunde.example;complained\nkaputt;bounced\n")
    report = campaign("import-results", "--actor", ACTOR, "--file", results, "--output", WORK / "c1.plan.json", expect=(1,))
    check(report["state"] == "invalid" and report["row_errors"][0]["row"] == 5, f"row error not blocking {report}")
    report = campaign("import-results", "--actor", ACTOR, "--file", results, "--output", WORK / "c1.plan.json", "--skip-failed")
    check(report["state"] == "planned" and report["counts"]["created"] == 2 and report["counts"]["rows"] == 4, f"suppressions {report}")
    apply_campaign(report, WORK / "c1.plan.json")
    uwe = ENV.find("message-suppression", "emailAddress", "uwe@kunde.example")
    check(uwe["reason"] == "COMPLAINT" and uwe["source"] == "IMPORT", f"complaint must win over unsubscribe: {uwe}")
    events = [json.loads(line) for shard in (TARGET / "meta/crm-events").glob("*.jsonl") for line in shard.read_text(encoding="utf-8").splitlines()]
    origin = next(event["origin"] for event in events if event["record_id"] == uwe["crm_id"])
    check(origin["kind"] == "import" and origin["ref"] == "ergebnisse.csv" and origin["row"] == 4, f"suppression origin {origin}")
    again = write("kampagne/ergebnisse2.csv", "email,status\nuwe@kunde.example,unsubscribed\nbounce@kunde.example,complained\n")
    report = campaign("import-results", "--actor", ACTOR, "--file", again, "--output", WORK / "c1b.plan.json")
    check(report["state"] == "nothing_to_plan" and report["counts"]["unchanged"] == 2, f"downgrade or duplicate {report}")
    audience = campaign("audience", "--filter", AUDIENCE_FILTER)
    check(audience["candidates"] == 7 and audience["recipients"] == 2, f"audience counts {audience}")
    check(audience["excluded"] == {"auto_created_without_consent": 1, "blocklisted": 1, "no_email": 1,
                                   "suppressed_bounce": 1, "suppressed_complaint": 1}, f"reasons {audience['excluded']}")
    check(sorted(item["email"] for item in audience["recipients_sample"]) == ["eva@kunde.example", "max@kunde.example"], "recipients")
    check(audience["suppression_list"]["records"] == 2, "suppression state")
    with_auto = campaign("audience", "--filter", AUDIENCE_FILTER, "--include-auto-created")
    check(with_auto["recipients"] == 3, "auto-created switch")


def test_campaign_list_render() -> None:
    report = campaign("plan-list", "--actor", ACTOR, "--output", WORK / "c2.plan.json", "--filter", AUDIENCE_FILTER,
                      "--name", "Newsletter Kunden", "--consent-source", "Messe-Formular 2026", "--consent-at", "2026-09-01")
    check(report["state"] == "planned" and report["summary"] == {"create": 3} and report["member_status"] == "SUBSCRIBED", f"plan-list {report}")
    apply_campaign(report, WORK / "c2.plan.json")
    newsletter = ENV.find("message-list", "name", "Newsletter Kunden")
    members = [data for data in ENV.records("message-list-member").values()]
    check(len(members) == 2 and all(data["status"] == "SUBSCRIBED" and data["consentAt"] == "2026-09-01T00:00:00Z" for data in members), f"members {members}")
    pending = report_pending = campaign("plan-list", "--actor", ACTOR, "--output", WORK / "c3.plan.json", "--list", newsletter["crm_id"],
                                        "--filter", json.dumps({"field": "emails", "operand": "CONTAINS", "value": "neukunde"}), "--include-auto-created")
    check(pending["member_status"] == "PENDING" and pending["summary"] == {"create": 1} and "note" in pending, f"pending {pending}")
    apply_campaign(report_pending, WORK / "c3.plan.json")
    again = campaign("plan-list", "--actor", ACTOR, "--output", WORK / "c4.plan.json", "--list", newsletter["crm_id"], "--filter", AUDIENCE_FILTER)
    check(again["state"] == "nothing_to_plan" and again["already_members"] == 2, f"duplicate members {again}")
    bounce = ENV.find("person", "emails.primaryEmail", "bounce@kunde.example")
    ENV.transact("member-bounce", [{"op": "create", "object": "messageListMember", "values": {
        "name": "Bea Bounce", "list": newsletter["crm_id"], "person": bounce["crm_id"], "status": "SUBSCRIBED", "consentSource": "Altvertrag"}}])
    audience = campaign("audience", "--list", newsletter["crm_id"])
    check(audience["recipients"] == 2 and audience["excluded"] == {"not_subscribed": 1, "suppressed_bounce": 1}, f"list audience {audience}")
    check(audience["not_subscribed_by_status"] == {"PENDING": 1}, "status breakdown")
    ENV.transact("campaign", [{"op": "create", "object": "messageCampaign", "values": {
        "name": "Herbst-Newsletter", "subject": "Hallo {{person.name.firstName}}, Neues von uns",
        "bodyTemplate": "Hallo {{name.firstName}} {{ person.name.lastName }},\n\nals {{jobTitle}} interessiert Sie das.\n\nIhr Team (Ref {{personId}})",
        "fromAddress": "anna@firma.example", "list": newsletter["crm_id"]}}])
    record = ENV.find("message-campaign", "name", "Herbst-Newsletter")
    outbox = WORK / "outbox"
    report = campaign("render", "--actor", ACTOR, "--campaign", record["crm_id"], "--outbox", outbox, "--output", WORK / "c5.plan.json")
    check(report["state"] == "planned" and report["drafts"] == 2 and report["outbox_files"] == 3, f"render {report}")
    check(report["empty_variables"] == {"jobTitle": 1} and report["list_unsubscribe"]["placeholder"] is True, f"render details {report}")
    folder = TARGET / report["outbox"]
    check(report["outbox"].startswith(f"records/_outbox/campaigns/{record['crm_id']}/") and not folder.exists() and not outbox.exists(),
          "drafts written before the plan was applied")
    apply_campaign(report, WORK / "c5.plan.json")
    drafts = sorted(folder.glob("*.eml"))
    check(len(drafts) == 2 and (folder / "manifest.json").is_file(), "outbox content in the wiki")
    check(sorted(path.name for path in outbox.iterdir()) == sorted([path.name for path in drafts] + ["manifest.json"]), "copy outside the wiki")
    texts = [path.read_text(encoding="utf-8") for path in drafts]
    eva = next(text for text in texts if "eva@kunde.example" in text)
    check("Subject: Hallo Eva, Neues von uns" in eva and "Hallo Eva Kunde," in eva and "als  interessiert" in eva, f"personalization {eva}")
    check("List-Unsubscribe: <mailto:abmelden@platzhalter.invalid?subject=unsubscribe>" in eva and "X-Unsent: 1" in eva, "headers")
    check(f"Ref {ENV.ids['eva']}" in eva, "personId variable")
    max_draft = next(text for text in texts if "max@kunde.example" in text)
    check("als Einkauf interessiert" in max_draft, "jobTitle variable")
    record = ENV.find("message-campaign", "name", "Herbst-Newsletter")
    check(record["status"] == "RENDERED" and record["skippedCount"] == 2 and record["sentCount"] == 0, f"status {record}")
    full = campaign("render", "--actor", ACTOR, "--campaign", record["crm_id"], "--outbox", outbox, "--output", WORK / "c6.plan.json", expect=(2,))
    check("not empty" in full["error"], "non-empty outbox accepted")
    ENV.transact("campaign-short", [{"op": "update", "object": "messageCampaign", "record": record["crm_id"],
                                      "values": {"subject": "Hallo {{firstName}} {{unbekannt.feld}}"}}])
    invalid = campaign("render", "--actor", ACTOR, "--campaign", record["crm_id"], "--outbox", WORK / "outbox2", "--output", WORK / "c7.plan.json", expect=(1,))
    check(invalid["state"] == "invalid" and any("short form" in error and "name.firstName" in error for error in invalid["errors"]), f"short form {invalid}")
    check(any("unbekannt.feld" in error for error in invalid["errors"]) and not (WORK / "outbox2").exists(), "unknown variable")


def test_ingest_drafts_and_campaign_link() -> None:
    record = ENV.find("message-campaign", "name", "Herbst-Newsletter")
    draft = sorted((WORK / "outbox").glob("*.eml"))[0]
    unsent = write("mails/14-entwurf.eml", mail("Anna Admin <anna@firma.example>", "Neu <neu@unbekannt-firma.example>", "Entwurf",
                                                 "Noch nicht gesendet.", message_id="<draft1@firma.example>", headers={"X-Unsent": "1"}))
    report, plan = ENV.ingest("t10", draft, unsent, mailbox_owner="anna@firma.example")
    check(report["counts"]["imported"] == 2 and all(entry.get("draft") for entry in report["imported"]), f"drafts {report['imported']}")
    check(report["contact_suggestions"]["people"] == [], "drafts proposed contacts")
    neu = next(entry for entry in report["unmatched_participants"] if entry["email"] == "neu@unbekannt-firma.example")
    check(neu["reason"] == "policy" and neu["detail"] == "drafts never create contacts", f"draft participant {neu}")
    ENV.apply_ingest(report, plan)
    linked = [data for data in ENV.records("message").values() if data.get("messageCampaign")]
    check(len(linked) == 1 and f"records/message-campaign/{record['crm_id']}|" in linked[0]["messageCampaign"] and linked[0]["isDraft"] is True,
          f"campaign link {linked}")


def test_ingest_own_addresses_from_members() -> None:
    incoming = write("mails/15-an-mitglied.eml", mail("Klara Kunde <klara@neukunde3.example>", "Ben Berater <ben@firma.example>",
                                                       "Frage zum Angebot", "Hallo Herr Berater, eine Frage.", message_id="<own1@neukunde3.example>"))
    outgoing = write("mails/16-von-mitglied.eml", mail("Ben Berater <ben@firma.example>", "Kai Kontakt <kai@neukunde3.example>",
                                                        "Unterlagen", "Anbei die Unterlagen.", message_id="<own2@firma.example>"))
    report, plan = ENV.ingest("t11", incoming, outgoing)
    check(report["settings"]["own_addresses_from"] == ["members"] and report["settings"]["own_addresses"] == 2
          and report["settings"]["own_address_list"] == ["anna@firma.example", "ben@firma.example"], f"own address source {report['settings']}")
    check(not any(entry.get("note", "").startswith("direction is not set") for entry in report["notes"]), "direction note despite members")
    check([entry["email"] for entry in report["contact_suggestions"]["people"]] == ["kai@neukunde3.example"], "policy SENT without arguments")
    ENV.apply_ingest(report, plan)
    check(ENV.find("message", "headerMessageId", "<own1@neukunde3.example>")["direction"] == "INCOMING", "incoming mail to a member")
    check(ENV.find("message", "headerMessageId", "<own2@firma.example>")["direction"] == "OUTGOING", "outgoing mail of a member")
    explicit, _ = ENV.ingest("t11b", incoming, mailbox_owner="anna@firma.example", own_addresses="anna.admin@firma.example")
    check(explicit["settings"]["own_addresses_from"] == ["argument"] and explicit["settings"]["own_addresses"] == 2, f"argument source {explicit['settings']}")


def page_data(object_dir: str) -> dict:
    import re

    text = (TARGET / "graph/crm/objects" / f"{object_dir}.html").read_text(encoding="utf-8")
    found = re.search(r'<script type="application/json" id="crm-data">(.*?)</script>', text, re.S)
    check(found is not None, f"no data block in {object_dir}.html")
    return json.loads(found.group(1))


def related(data: dict, record_id: str) -> dict[str, set[str]]:
    row = next((row for row in data["rows"] if row[0] == record_id), None)
    check(row is not None, f"record {record_id} missing on the object page")
    groups: dict[str, set[str]] = {}
    for group, ref in row[3]:
        groups.setdefault(data["incg"][group], set()).add(data["refs"][ref][1])
    return groups


def test_activity_on_company_and_opportunity() -> None:
    # the usual Emails and Calendar tabs: a company shows what its people took part in, an opportunity what its company did.
    ENV.transact("deal", [{"op": "create", "object": "opportunity", "values": {"name": "Relaunch Kunde", "company": {"match": {"domainName": "kunde.example"}}}},
                          {"op": "create", "object": "opportunity", "values": {"name": "Ohne Firma"}}])
    built = ENV.wiki.locked("crm_build.py", "--target", TARGET)
    check(built.get("state") in ("built", "fresh"), f"crm_build {built}")
    company = ENV.find("company", "name", "Kunde GmbH")["crm_id"]
    mail_ids = {data["crm_id"] for data in ENV.records("message").values()
                if any(f"records/person/{ENV.ids[name]}|" in link for name in ("eva", "max") for link in data.get("participants") or [])}
    event_ids = {data["crm_id"] for data in ENV.records("calendar-event").values()
                 if any(f"records/person/{ENV.ids[name]}|" in link for name in ("eva", "max") for link in data.get("participants") or [])}
    check(mail_ids and event_ids, "the corpus links no mail or meeting to Eva or Max")
    groups = related(page_data("company"), company)
    check(groups.get("E-Mails der Personen") == mail_ids, f"company mails {len(groups.get('E-Mails der Personen', ()))} of {len(mail_ids)}")
    check(groups.get("Termine der Personen") == event_ids, f"company meetings {len(groups.get('Termine der Personen', ()))} of {len(event_ids)}")
    deals = page_data("opportunity")
    deal = ENV.find("opportunity", "name", "Relaunch Kunde")["crm_id"]
    lone = ENV.find("opportunity", "name", "Ohne Firma")["crm_id"]
    check(related(deals, deal).get("E-Mails zur Verkaufschance") == mail_ids and related(deals, deal).get("Termine zur Verkaufschance") == event_ids,
          "opportunity does not show the mails and meetings of its company")
    check(not related(deals, lone), "an opportunity without company shows activity")


TESTS = [
    test_library_charset_and_html,
    test_library_redaction,
    test_library_domains_and_names,
    test_library_rrule,
    test_library_time_zones,
    test_ingest_text_html_charset,
    test_ingest_bulk_and_redaction,
    test_ingest_two_mailboxes,
    test_ingest_invitation_and_calendar,
    test_ingest_cancel_and_older_sequence,
    test_ingest_msg_blocklist_internal_private,
    test_ingest_contacts_and_visibility,
    test_ingest_isolated_and_errors,
    test_ingest_series_exceptions,
    test_campaign_results_and_audience,
    test_campaign_list_render,
    test_ingest_drafts_and_campaign_link,
    test_ingest_own_addresses_from_members,
    test_activity_on_company_and_opportunity,
]


def main() -> int:
    try:
        setup()
    except Exception:  # noqa: BLE001
        print("SETUP FAILED\n" + traceback.format_exc())
        return 1
    try:
        for test in TESTS:
            try:
                test()
                PASSED.append(test.__name__)
            except Exception as exc:  # noqa: BLE001
                FAILURES.append(f"{test.__name__}: {exc}" if isinstance(exc, AssertionError) else f"{test.__name__}: {traceback.format_exc()}")
        lint = ENV.wiki.locked("lint_wiki.py", "--target", TARGET, "--check-only", expect=(0, 1))
        crm_errors = [error for error in lint.get("errors", []) if "records/" in error or "crm-events" in error or "credential" in error]
        if crm_errors:
            FAILURES.append(f"lint reports CRM errors: {crm_errors[:5]}")
        else:
            PASSED.append("lint_records")
    finally:
        ENV.wiki.release()
    for name in PASSED:
        print(f"ok   {name}")
    for failure in FAILURES:
        print(f"FAIL {failure}")
    print("ALL OK" if not FAILURES else f"{len(FAILURES)} FAILED")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
