#!/usr/bin/env python3
"""Campaign audiences, lists, drafts and delivery results, modelled on a CRM e-mailing module.

audience        who would receive a campaign: the people of a list (--list) or of a
                filter or person view (--filter, --view), with counts per exclusion
                reason: deleted person, list member without status SUBSCRIBED,
                no primary e-mail, blocklisted address, suppression (bounce,
                complaint, unsubscribe; TRACKING does not exclude), and people created
                automatically from e-mail or calendar files without consent
                (filter audiences only; --include-auto-created keeps them).
plan-list       a CRM transaction plan that creates a list (or extends --list) with the
                people of a filter or view audience. Members get status SUBSCRIBED only
                with --consent-source (where the consent comes from); otherwise PENDING.
render          personalized .eml drafts of a campaign for its list, as one transaction
                plan that keeps them in the wiki under records/_outbox/campaigns/<campaign>/
                <time>/ (with manifest.json) and sets the campaign status RENDERED and
                skippedCount. --outbox names an optional extra copy outside the wiki,
                written when the plan is applied. Variables are person field paths such
                as {{person.name.firstName}} or the short form {{name.firstName}}, plus
                {{fullName}} and {{personId}}; unknown names and short forms such as
                {{firstName}} stop the rendering. List-Unsubscribe is a mailto
                placeholder that must be replaced before sending. Nothing is sent.
import-results  a CSV of delivery results (columns email,status with bounced,
                complained or unsubscribed) becomes suppression records as a plan.
                A bounce or complaint upgrades an unsubscribe; nothing is downgraded.
apply           applies a plan of this helper with its plan_sha256.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import csv
import io
import json
import re
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, formataddr
from typing import Any, Optional

import crm_mail
from crm_contract import (
    StalePlanError,
    OUTBOX_DIR, SETTINGS_PATH, VIEWS_PATH, CrmError, Record, RecordStore, apply_transaction, coerce_value, format_instant,
    load_datamodel, load_json_file, parse_record_link, plan_transaction, valid_actor, verify_plan,
)
from crm_filters import Context, FilterError, select_records, validate_filter
from crm_messaging_objects import HARD_SUPPRESSION_REASONS, OBJECT_NAMES
from wiki_lock import require_lock

CAMPAIGN_FORMAT = "lmwiki-crm-campaign/1"
VARIABLE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*)*)\s*\}\}")
VARIABLE_TYPES = {"TEXT", "NUMBER", "BOOLEAN", "DATE", "DATE_TIME", "SELECT", "RATING"}
COMPUTED_VARIABLES = ("fullName", "personId")
SHORT_FORMS = {"firstName": "name.firstName", "lastName": "name.lastName", "email": "emails.primaryEmail (not available as a variable)"}
RESULT_REASONS = {"bounced": "BOUNCE", "bounce": "BOUNCE", "complained": "COMPLAINT", "complaint": "COMPLAINT",
                  "unsubscribed": "UNSUBSCRIBE", "unsubscribe": "UNSUBSCRIBE"}
AUTO_SOURCES = {"EMAIL", "CALENDAR"}
RENDERABLE_STATUSES = {"DRAFT", "RENDERED"}
UNSUBSCRIBE_PLACEHOLDER = "abmelden@platzhalter.invalid"
MESSAGE_NAMESPACE = uuid.UUID("5d2f4c1e-8b3a-5f6d-9c2e-1a7b3e4f5d60")
SAMPLE = 50


def outside(target: Path, path: Path) -> bool:
    return path != target and target not in path.parents


def error_text(exc: Exception) -> str:
    """Error text without local paths: only the file name of an OSError is shown."""
    if isinstance(exc, OSError):
        name = Path(str(exc.filename)).name if getattr(exc, "filename", None) else ""
        return (exc.strerror or "file error") + (f": {name}" if name else "")
    return str(exc)


def linked_id(value: Any) -> Optional[str]:
    parsed = parse_record_link(value) if isinstance(value, str) else None
    return parsed[1] if parsed else None


class Campaigns:
    def __init__(self, target: Path, args: argparse.Namespace):
        self.target = target
        self.args = args
        self.datamodel = load_datamodel(target)
        missing = [name for name in OBJECT_NAMES + ("person",) if name not in self.datamodel.objects]
        if missing:
            raise CrmError(f"the data model lacks {', '.join(missing)}; merge crm_messaging_objects into the standard data model first")
        self.store = RecordStore(target, self.datamodel)
        settings = load_json_file(target, SETTINGS_PATH, {}) or {}
        self.settings = settings
        self.blocklist = [item for item in (settings.get("email") or {}).get("blocklist") or [] if isinstance(item, str)]
        self.time_zone = str(settings.get("time_zone") or "UTC")

    # -- audience ------------------------------------------------------------

    def filter_spec(self) -> tuple[Any, dict[str, Any]]:
        conditions = []
        source: dict[str, Any] = {}
        if getattr(self.args, "view", None):
            views = load_json_file(self.target, VIEWS_PATH, {}) or {}
            view = next((item for item in views.get("views", []) if item.get("id") == self.args.view), None)
            if view is None:
                raise CrmError(f"view {self.args.view!r} does not exist")
            if view.get("object") != "person":
                raise CrmError(f"view {self.args.view!r} shows {view.get('object')}, not people")
            view_filter = view.get("filter") or view.get("filters")
            if view_filter:
                conditions.append(view_filter)
            source["view"] = self.args.view
        if getattr(self.args, "filter", None):
            raw = self.args.filter
            path = Path(raw).expanduser()
            try:
                spec = json.loads(path.read_text(encoding="utf-8")) if not raw.lstrip().startswith(("{", "[")) and path.is_file() else json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                raise CrmError(f"--filter is neither JSON nor a readable JSON file: {exc}") from exc
            conditions.append(spec)
            source["filter"] = spec
        spec = None if not conditions else (conditions[0] if len(conditions) == 1 else {"op": "AND", "conditions": conditions})
        problems = validate_filter(self.datamodel, "person", spec) if spec is not None else []
        if problems:
            raise CrmError("invalid filter: " + "; ".join(problems))
        return spec, source

    def suppressions(self, topic: Optional[str]) -> tuple[dict[str, str], dict[str, Any]]:
        """Excluding suppression reason per address, and the state of the suppression list."""
        reasons: dict[str, str] = {}
        latest = ""
        count = 0
        for record in self.store.records("messageSuppression").values():
            if record.deleted:
                continue
            count += 1
            latest = max(latest, str(record.data.get("crm_updated_at") or ""))
            address = str(record.data.get("emailAddress") or "").strip().lower()
            reason = str(record.data.get("reason") or "")
            record_topic = record.data.get("unsubscribeTopicId")
            if not address or reason == "TRACKING":
                continue
            if record_topic and record_topic != topic:
                continue
            current = reasons.get(address)
            if current is None or (reason in HARD_SUPPRESSION_REASONS and current not in HARD_SUPPRESSION_REASONS):
                reasons[address] = reason if not record_topic else "UNSUBSCRIBE_TOPIC"
        return reasons, {"records": count, "latest_update": latest or None}

    def audience(self, *, list_id: Optional[str] = None, topic: Optional[str] = None, include_auto: bool = False) -> dict[str, Any]:
        people = self.store.records("person")
        excluded: dict[str, int] = {}
        not_subscribed: dict[str, int] = {}
        recipients: list[dict[str, Any]] = []
        entries: list[tuple[Optional[Record], Optional[Record]]] = []
        source: dict[str, Any]
        if list_id:
            message_list = self.store.get("messageList", list_id)
            if message_list is None or message_list.deleted:
                raise CrmError(f"list {list_id} does not exist or is in the trash")
            source = {"list": list_id, "title": message_list.data.get("crm_title")}
            for member in self.store.records("messageListMember").values():
                if member.deleted or linked_id(member.data.get("list")) != list_id:
                    continue
                entries.append((member, people.get(linked_id(member.data.get("person")) or "")))
        else:
            spec, source = self.filter_spec()
            if not source:
                raise CrmError("give --list, --filter or --view")
            context = Context(time_zone=self.time_zone)
            for person in select_records(self.datamodel, list(people.values()), filter_spec=spec, context=context):
                entries.append((None, person))
        suppressed, suppression_state = self.suppressions(topic)
        seen_people: set[str] = set()
        seen_addresses: set[str] = set()

        def exclude(reason: str) -> None:
            excluded[reason] = excluded.get(reason, 0) + 1

        for member, person in entries:
            if person is None or person.deleted:
                exclude("person_deleted")
                continue
            if person.id in seen_people:
                exclude("duplicate")
                continue
            seen_people.add(person.id)
            if member is not None:
                status = str(member.data.get("status") or "PENDING")
                if status != "SUBSCRIBED":
                    exclude("not_subscribed")
                    not_subscribed[status] = not_subscribed.get(status, 0) + 1
                    continue
            address = str(person.data.get("emails.primaryEmail") or "").strip().lower()
            if not address:
                exclude("no_email")
                continue
            if address in seen_addresses:
                exclude("duplicate")
                continue
            seen_addresses.add(address)
            if crm_mail.is_blocklisted(address, self.blocklist):
                exclude("blocklisted")
                continue
            if address in suppressed:
                exclude("suppressed_" + suppressed[address].lower())
                continue
            if member is None and not include_auto and person.data.get("crm_created_source") in AUTO_SOURCES:
                exclude("auto_created_without_consent")
                continue
            recipients.append({"id": person.id, "title": person.data.get("crm_title"), "email": address, "person": person})
        result = {
            "source": source,
            "candidates": len(entries),
            "recipients": len(recipients),
            "excluded": dict(sorted(excluded.items())),
            "suppression_list": suppression_state,
            "_recipients": recipients,
        }
        if not_subscribed:
            result["not_subscribed_by_status"] = not_subscribed
        return result

    # -- plans ---------------------------------------------------------------

    def write_plan(self, request: dict[str, Any], info: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        output = Path(self.args.output).expanduser().resolve()
        if not outside(self.target, output):
            raise CrmError("the plan must be written outside the wiki")
        plan = plan_transaction(self.target, {**request, "annotations": {"campaign": {"format": CAMPAIGN_FORMAT, **info}}})
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report = {
            "state": "invalid" if plan["errors"] else "planned",
            "plan_sha256": plan["plan_sha256"],
            "plan_file": output.name,
            "summary": plan["summary"],
            "files": len(plan["files"]),
            "outbox_files": sum(1 for entry in plan.get("artifacts", []) if entry["after"] is not None),
            "destructive": plan["destructive"],
            "errors": plan["errors"][:100],
            "warnings": plan["warnings"][:50],
        }
        return plan, report

    def plan_list(self) -> dict[str, Any]:
        if not valid_actor(self.args.actor):
            raise CrmError("--actor must be human:<id>, agent/<name> or process:<id>")
        audience = self.audience(include_auto=self.args.include_auto_created)
        operations: list[dict[str, Any]] = []
        if self.args.list:
            message_list = self.store.get("messageList", self.args.list)
            if message_list is None or message_list.deleted:
                raise CrmError(f"list {self.args.list} does not exist or is in the trash")
            list_ref: Any = message_list.id
            list_title = str(message_list.data.get("crm_title") or "")
        else:
            if not (self.args.name or "").strip():
                raise CrmError("--name is required to create a list")
            list_title = self.args.name.strip()
            operations.append({"op": "create", "object": "messageList", "ref": "list",
                               "values": {"name": list_title, "description": self.args.description or None}})
            list_ref = {"ref": "list"}
        existing = set()
        if self.args.list:
            for member in self.store.records("messageListMember").values():
                if not member.deleted and linked_id(member.data.get("list")) == self.args.list:
                    existing.add(linked_id(member.data.get("person")))
        consent_at = None
        if self.args.consent_at:
            consent_at = coerce_value("DATE_TIME", {}, "consentAt", self.args.consent_at)
        status = "SUBSCRIBED" if (self.args.consent_source or "").strip() else "PENDING"
        already = 0
        for recipient in audience["_recipients"]:
            if recipient["id"] in existing:
                already += 1
                continue
            operations.append({"op": "create", "object": "messageListMember", "values": {
                "name": f"{recipient['title']} · {list_title}",
                "list": list_ref,
                "person": recipient["id"],
                "status": status,
                "consentSource": (self.args.consent_source or "").strip() or None,
                "consentAt": consent_at,
            }})
        if not operations:
            return {"state": "nothing_to_plan", "audience": self.public(audience), "already_members": already}
        _plan, report = self.write_plan({"actor": self.args.actor, "origin": {"kind": "manual", "occasion": "campaign list"}, "operations": operations},
                                        {"command": "plan-list", "members": len(operations) - (0 if self.args.list else 1), "status": status})
        report.update({"audience": self.public(audience), "already_members": already, "member_status": status})
        if status == "PENDING":
            report["note"] = "without --consent-source the members are PENDING and campaigns skip them until their consent is recorded"
        return report

    @staticmethod
    def public(audience: dict[str, Any]) -> dict[str, Any]:
        result = {key: value for key, value in audience.items() if not key.startswith("_")}
        result["recipients_sample"] = [{key: value for key, value in item.items() if key != "person"} for item in audience["_recipients"][:SAMPLE]]
        return result

    # -- rendering -----------------------------------------------------------

    def person_variables(self) -> dict[str, tuple[str, Optional[str], dict[str, Any]]]:
        variables: dict[str, tuple[str, Optional[str], dict[str, Any]]] = {}
        for name, definition in self.datamodel.fields("person").items():
            if definition.get("active", True) is False:
                continue
            if definition["type"] in VARIABLE_TYPES:
                variables[name] = (name, None, definition)
            elif definition["type"] == "FULL_NAME":
                for sub in ("firstName", "lastName"):
                    variables[f"{name}.{sub}"] = (name, sub, definition)
        return variables

    def check_variables(self, texts: list[str]) -> tuple[dict[str, str], list[str]]:
        """Map each used variable to its field path; return problems for unknown names."""
        known = self.person_variables()
        used: dict[str, str] = {}
        problems = []
        for text in texts:
            for raw in VARIABLE_PATTERN.findall(text or ""):
                name = raw[len("person."):] if raw.startswith("person.") else raw
                if name in known or name in COMPUTED_VARIABLES:
                    used[raw] = name
                elif name in SHORT_FORMS:
                    problems.append(f"{{{{{raw}}}}} is a short form; use {{{{person.{SHORT_FORMS[name]}}}}}" if "not available" not in SHORT_FORMS[name]
                                    else f"{{{{{raw}}}}} is not a campaign variable; {SHORT_FORMS[name]}")
                else:
                    problems.append(f"{{{{{raw}}}}} does not match a field of People")
        if problems:
            available = sorted(set(known) | set(COMPUTED_VARIABLES))
            problems.append("available: " + ", ".join(f"{{{{person.{name}}}}}" for name in available[:40]))
        return used, list(dict.fromkeys(problems))

    def variable_value(self, person: Record, name: str) -> str:
        data = person.data
        if name == "personId":
            return person.id
        if name == "fullName":
            return " ".join(part for part in (str(data.get("name.firstName") or "").strip(), str(data.get("name.lastName") or "").strip()) if part)
        field_name, sub, definition = self.person_variables()[name]
        value = data.get(f"{field_name}.{sub}") if sub else data.get(field_name)
        if value in (None, ""):
            return ""
        ftype = definition["type"]
        if ftype in {"DATE", "DATE_TIME"}:
            return str(value)[:10]
        if ftype == "SELECT":
            return next((str(option.get("label")) for option in definition.get("options", []) if option.get("value") == value), str(value))
        if ftype == "RATING":
            return str(value).replace("RATING_", "")
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def render(self) -> dict[str, Any]:
        if not valid_actor(self.args.actor):
            raise CrmError("--actor must be human:<id>, agent/<name> or process:<id>")
        campaign = self.store.get("messageCampaign", self.args.campaign.strip().lower())
        if campaign is None or campaign.deleted:
            raise CrmError(f"campaign {self.args.campaign} does not exist or is in the trash")
        status = str(campaign.data.get("status") or "DRAFT")
        if status not in RENDERABLE_STATUSES:
            raise CrmError(f"campaign status is {status}; only DRAFT or RENDERED campaigns are rendered")
        subject = str(campaign.data.get("subject") or "")
        body = campaign.richtext.get("bodyTemplate", "")
        sender = str(campaign.data.get("fromAddress.primaryEmail") or "")
        list_id = linked_id(campaign.data.get("list"))
        missing = [name for name, value in (("subject", subject), ("bodyTemplate", body.strip()), ("fromAddress", sender), ("list", list_id)) if not value]
        if missing:
            raise CrmError(f"the campaign lacks {', '.join(missing)}")
        used, problems = self.check_variables([subject, body])
        if problems:
            return {"state": "invalid", "errors": problems, "note": "nothing was rendered"}
        copy_dir = Path(self.args.outbox).expanduser().resolve() if self.args.outbox else None
        if copy_dir is not None and not outside(self.target, copy_dir):
            raise CrmError("--outbox names an extra copy outside the wiki; the drafts themselves are kept in records/_outbox/")
        if copy_dir is not None and copy_dir.exists() and any(copy_dir.iterdir()):
            raise CrmError(f"the copy folder {copy_dir.name} is not empty; choose a new folder")
        topic = campaign.data.get("unsubscribeTopicId") or None
        audience = self.audience(list_id=list_id, topic=topic)
        if not audience["_recipients"]:
            return {"state": "nothing_to_render", "audience": self.public(audience)}
        unsubscribe = (self.args.unsubscribe_mailto or UNSUBSCRIBE_PLACEHOLDER).strip()
        if not crm_mail.EMAIL_RE.fullmatch(unsubscribe):
            raise CrmError("--unsubscribe-mailto must be an e-mail address")
        sender_domain = crm_mail.domain_of(sender) or "localhost"
        empty: dict[str, int] = {}
        manifest = []
        artifacts: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc).replace(microsecond=0)
        folder = f"{OUTBOX_DIR}/campaigns/{campaign.id}/{now.strftime('%Y%m%dT%H%M%SZ')}"

        def fill(template: str, person: Record) -> str:
            def replace(match: "re.Match[str]") -> str:
                value = self.variable_value(person, used[match.group(1)])
                if not value:
                    empty[match.group(1)] = empty.get(match.group(1), 0) + 1
                return value

            return VARIABLE_PATTERN.sub(replace, template)

        for index, recipient in enumerate(audience["_recipients"], 1):
            person = recipient["person"]
            message = EmailMessage()
            message["From"] = sender
            display = self.variable_value(person, "fullName")
            message["To"] = formataddr((display, recipient["email"])) if display else recipient["email"]
            message["Subject"] = crm_mail.single_line(fill(subject, person), 300)
            message["Date"] = format_datetime(now)
            message["Message-ID"] = f"<{uuid.uuid5(MESSAGE_NAMESPACE, campaign.id + ':' + person.id)}@{sender_domain}>"
            message["List-Unsubscribe"] = f"<mailto:{unsubscribe}?subject=unsubscribe>"
            message["X-Unsent"] = "1"
            message["X-LMWiki-Campaign"] = campaign.id
            message.set_content(fill(body, person).rstrip("\n") + "\n")
            name = f"{index:04d}-{person.id[:8]}.eml"
            artifacts.append({"path": f"{folder}/{name}", "content": bytes(message).decode("utf-8")})
            manifest.append({"file": name, "person": person.id, "email": recipient["email"]})
        skipped = audience["candidates"] - audience["recipients"]
        artifacts.append({"path": f"{folder}/manifest.json", "content": json.dumps({
            "format": "lmwiki-campaign-outbox/1", "campaign": campaign.id, "list": list_id, "rendered_at": format_instant(now),
            "drafts": manifest, "excluded": audience["excluded"], "list_unsubscribe": unsubscribe,
        }, ensure_ascii=False, indent=2) + "\n"})
        _plan, report = self.write_plan({
            "actor": self.args.actor, "origin": {"kind": "manual", "occasion": "campaign drafts rendered"},
            "operations": [{"op": "update", "object": "messageCampaign", "record": campaign.id,
                            "values": {"status": "RENDERED", "skippedCount": skipped}}],
            "artifacts": artifacts,
        }, {"command": "render", "campaign": campaign.id, "drafts": len(manifest), "outbox_folder": folder,
            "copy_dir": str(copy_dir) if copy_dir is not None else None})
        report.update({
            "drafts": len(manifest),
            "outbox": folder,
            "outbox_copy": copy_dir.name if copy_dir is not None else None,
            "audience": self.public(audience),
            "variables": sorted(set(used.values())),
            "empty_variables": empty,
            "list_unsubscribe": {"value": f"mailto:{unsubscribe}", "placeholder": unsubscribe == UNSUBSCRIBE_PLACEHOLDER,
                                 "note": "replace the placeholder by a monitored unsubscribe address before sending; one-click unsubscribe (RFC 8058) needs a web endpoint this skill does not provide"},
            "next_step": "apply the plan (it writes the drafts into the wiki outbox and sets the status); send the drafts with the mail program or sending service, never from here; import bounces, complaints and unsubscribes with import-results",
        })
        return report

    # -- results -------------------------------------------------------------

    def import_results(self) -> dict[str, Any]:
        if not valid_actor(self.args.actor):
            raise CrmError("--actor must be human:<id>, agent/<name> or process:<id>")
        path = Path(self.args.file).expanduser()
        raw = path.read_bytes()
        text, note = crm_mail.decode_bytes(raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw, None)
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        columns = {name.strip().lower(): name for name in (reader.fieldnames or []) if name}
        if "email" not in columns or "status" not in columns:
            raise CrmError("the CSV needs the columns email and status")
        event_column = columns.get("provider_event_id") or columns.get("providereventid")
        topic = (self.args.topic or "").strip().lower() or None
        if topic:
            coerce_value("UUID", {}, "unsubscribeTopicId", topic)
        wanted: dict[tuple[str, Optional[str]], dict[str, Any]] = {}
        errors = []
        rows = 0
        for row_number, row in enumerate(reader, 2):
            address = str(row.get(columns["email"]) or "").strip().lower()
            status = str(row.get(columns["status"]) or "").strip().lower()
            if not address and not status:
                continue
            rows += 1
            if not crm_mail.EMAIL_RE.fullmatch(address):
                errors.append({"row": row_number, "error": "not an e-mail address"})
                continue
            reason = RESULT_REASONS.get(status)
            if reason is None:
                errors.append({"row": row_number, "error": f"status {status!r} is not bounced, complained or unsubscribed"})
                continue
            key = (address, None if reason in HARD_SUPPRESSION_REASONS else topic)
            current = wanted.get(key)
            if current is None or (reason in HARD_SUPPRESSION_REASONS and current["reason"] not in HARD_SUPPRESSION_REASONS):
                wanted[key] = {"reason": reason, "row": row_number,
                               "providerEventId": (str(row.get(event_column) or "").strip() or None) if event_column else None}
        existing: dict[tuple[str, Optional[str]], Record] = {}
        for record in self.store.records("messageSuppression").values():
            if record.data.get("reason") == "TRACKING":
                continue
            key = (str(record.data.get("emailAddress") or "").strip().lower(), record.data.get("unsubscribeTopicId") or None)
            if key not in existing or existing[key].deleted:
                existing[key] = record
        operations = []
        counts = {"rows": rows, "created": 0, "upgraded": 0, "restored": 0, "unchanged": 0}
        for (address, row_topic), entry in sorted(wanted.items(), key=lambda item: item[1]["row"]):
            record = existing.get((address, row_topic))
            values = {"reason": entry["reason"], "source": "IMPORT", "providerEventId": entry["providerEventId"]}
            if record is None:
                values.update({"emailAddress": address, "unsubscribeTopicId": row_topic})
                operations.append({"op": "create", "object": "messageSuppression", "row": entry["row"], "values": values})
                counts["created"] += 1
                continue
            if record.deleted:
                operations.append({"op": "restore", "object": "messageSuppression", "record": record.id, "row": entry["row"]})
                operations.append({"op": "update", "object": "messageSuppression", "record": record.id, "row": entry["row"], "values": values})
                counts["restored"] += 1
                continue
            stored = str(record.data.get("reason") or "")
            if entry["reason"] in HARD_SUPPRESSION_REASONS and stored not in HARD_SUPPRESSION_REASONS:
                operations.append({"op": "update", "object": "messageSuppression", "record": record.id, "row": entry["row"], "values": values})
                counts["upgraded"] += 1
            else:
                counts["unchanged"] += 1
        result: dict[str, Any] = {"counts": counts, "row_errors": errors[:100]}
        if note:
            result["note"] = note
        if errors and not self.args.skip_failed:
            result.update({"state": "invalid", "next_step": "fix the rows or, after the user agreed, plan again with --skip-failed"})
            return result
        if not operations:
            result["state"] = "nothing_to_plan"
            return result
        _plan, report = self.write_plan({"actor": self.args.actor, "origin": {"kind": "import", "ref": path.name, "occasion": "campaign delivery results"},
                                         "operations": operations}, {"command": "import-results", "counts": counts, "row_errors": errors[:100]})
        report.update(result)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: str) -> argparse.ArgumentParser:
        child = sub.add_parser(command)
        child.add_argument("--target", required=True)
        child.add_argument("--lock-token", required=True)
        return child

    audience_parser = common("audience")
    audience_parser.add_argument("--list")
    audience_parser.add_argument("--filter", help="filter JSON (or a file holding it) on People")
    audience_parser.add_argument("--view", help="id of a person view in schema/crm/views.json")
    audience_parser.add_argument("--topic", help="unsubscribe topic id whose unsubscribes also apply")
    audience_parser.add_argument("--include-auto-created", action="store_true")
    list_parser = common("plan-list")
    list_parser.add_argument("--actor", required=True)
    list_parser.add_argument("--output", required=True)
    list_parser.add_argument("--filter")
    list_parser.add_argument("--view")
    list_parser.add_argument("--name", help="name of the new list")
    list_parser.add_argument("--list", help="add to this existing list instead")
    list_parser.add_argument("--description")
    list_parser.add_argument("--consent-source", help="where the consent of these people is documented")
    list_parser.add_argument("--consent-at", help="when the consent was given (ISO date or instant)")
    list_parser.add_argument("--include-auto-created", action="store_true")
    render_parser = common("render")
    render_parser.add_argument("--actor", required=True)
    render_parser.add_argument("--campaign", required=True)
    render_parser.add_argument("--outbox", help="optional extra copy of the drafts in a new folder outside the wiki")
    render_parser.add_argument("--output", required=True, help="status plan, outside the wiki")
    render_parser.add_argument("--unsubscribe-mailto", help="address for the List-Unsubscribe header")
    results_parser = common("import-results")
    results_parser.add_argument("--actor", required=True)
    results_parser.add_argument("--file", required=True)
    results_parser.add_argument("--output", required=True)
    results_parser.add_argument("--topic", help="unsubscribe topic id of unsubscribe rows")
    results_parser.add_argument("--skip-failed", action="store_true")
    apply_parser = common("apply")
    apply_parser.add_argument("--plan-file", required=True)
    apply_parser.add_argument("--expect-plan-sha256", required=True)
    args = parser.parse_args()
    target = Path(args.target).expanduser().resolve()
    require_lock(target, args.lock_token)
    try:
        if args.command == "apply":
            plan = verify_plan(json.loads(Path(args.plan_file).read_text(encoding="utf-8")), args.expect_plan_sha256)
            if ((plan.get("annotations") or {}).get("campaign") or {}).get("format") != CAMPAIGN_FORMAT:
                raise CrmError("this is not a campaign plan; apply it with crm_records.py")
            result, code = apply_transaction(target, args.lock_token, plan)
            info = (plan.get("annotations") or {}).get("campaign") or {}
            if code == 0 and info.get("copy_dir"):
                copy_dir = Path(info["copy_dir"])
                copy_dir.mkdir(parents=True, exist_ok=True)
                for entry in plan.get("artifacts", []):
                    if entry.get("after") is not None:
                        (copy_dir / Path(entry["path"]).name).write_text(entry["after"], encoding="utf-8")
                result["outbox_copy"] = copy_dir.name
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return code
        helper = Campaigns(target, args)
        if args.command == "audience":
            if args.list and (args.filter or args.view):
                raise CrmError("use either --list or --filter/--view")
            report = {"state": "ok", **helper.public(helper.audience(list_id=(args.list or "").strip().lower() or None,
                                                                         topic=args.topic, include_auto=args.include_auto_created))}
        elif args.command == "plan-list":
            report = helper.plan_list()
        elif args.command == "render":
            report = helper.render()
        else:
            report = helper.import_results()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report.get("state") == "invalid" else 0
    except StalePlanError as exc:
        print(json.dumps({"state": "stale_plan", "writes": 0, "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 3
    except (OSError, json.JSONDecodeError, CrmError, FilterError) as exc:
        print(json.dumps({"state": "error", "error": error_text(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
