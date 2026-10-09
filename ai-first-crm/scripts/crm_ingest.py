#!/usr/bin/env python3
"""Plan and apply the import of e-mail and calendar files into CRM records.

plan   reads .eml, .mbox and .ics files (or directories holding them), applies the
       e-mail rules of schema/crm/settings.json and writes one hash-bound CRM
       transaction plan outside the wiki. Nothing in the wiki changes.
apply  applies exactly that plan (same plan_sha256) through the CRM core.

the common import rules, in this order: invitations with a calendar part become
calendar events (the mail text becomes the description), mails to unsubscribe
addresses are skipped, then every mail or event with a blocklisted participant
(exact address or @domain including subdomains), internal mails (only own
domains, --include-internal imports them), and bulk or group mails (List-Unsubscribe,
List-Id, Precedence bulk/list/junk, Auto-Submitted, senders like info@ or noreply;
--include-bulk imports them). Private events (CLASS PRIVATE/CONFIDENTIAL) need
--include-private. Every skipped file or message is reported with its reason.

Messages are keyed by headerMessageId: the same mail from a second mailbox only
fills empty fields and adds participants. X-Unsent marks a draft (isDraft); a draft
rendered by crm_campaign.py is linked to its campaign (messageCampaign). Events are
keyed by iCalUid (an exception of a series by "<UID>#<RECURRENCE-ID>") and carry
iCalSequence, externalCreatedAt (CREATED), externalUpdatedAt (LAST-MODIFIED) and
conferenceSolution: a higher SEQUENCE than the stored iCalSequence (or, at equal
SEQUENCE, a later LAST-MODIFIED) updates the event, an older one is ignored and
reported, STATUS:CANCELLED or METHOD:CANCEL sets isCanceled and keeps the details.
Every event in the log names its source: origin kind email with ref
message-id:<id>, or kind calendar with ref ical-uid:<uid>.

Visibility is applied at import: METADATA stores neither subject nor text,
SUBJECT stores the subject, SHARE_EVERYTHING stores the text with a list of
attachment names and sizes. Calendar events know only METADATA (no title,
description, location or conference link) and SHARE_EVERYTHING; SUBJECT is
applied as METADATA. Credentials are redacted before planning; a record in which
secret_screen still finds something is reported as an error.

Unknown participants become contact proposals (person, and a company for the
registrable domain), never for free e-mail domains, blocklisted, excluded,
group or internal addresses. --create-contacts puts them into the plan. Every
participant without a person or member is listed in unmatched_participants with
its display name, the number of messages and events, and the reason why no
proposal was made (proposed, policy, own_address, free_email, blocklist,
excluded_handle, group_address, internal, person_in_trash, room_or_resource).
The report is part of the plan (annotations.ingest) and so of what is confirmed.
Own addresses (direction, internal mails, contact policy) come from --mailbox-owner
and --own-addresses (argument) and settings.email.own_addresses (settings); when
neither --own-addresses nor the setting is given, the login e-mails of all
workspace members outside the trash count as own addresses (members). The report
names the sources in settings.own_addresses_from.
Contact creation follows settings.email.contact_creation: NONE, SENT (participants
of mails sent from own addresses), SENT_AND_RECEIVED, or "work-domains" (the
default: SENT without free e-mail addresses). Calendar events use all
participants unless the policy is NONE (the common calendar default).

A plan with errors (unreadable files, remaining credentials, failing records) is
not applicable; after the user agreed, --skip-failed leaves the failing items out.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import crm_ical
import crm_mail
from crm_contract import (
    StalePlanError,
    SETTINGS_PATH, UUID_RE, CrmError, Record, RecordStore, apply_transaction, format_instant, load_datamodel,
    load_json_file, normalize_url, parse_record_link, plan_transaction, portable_reference, valid_actor, verify_plan,
)
from wiki_lock import require_lock

INGEST_FORMAT = "lmwiki-crm-ingest/1"
VISIBILITY_ORDER = {"METADATA": 0, "SUBJECT": 1, "SHARE_EVERYTHING": 2}
POLICIES = {"NONE", "SENT", "SENT_AND_RECEIVED", "WORK-DOMAINS"}
ITIP_SKIPPED = {"REPLY", "COUNTER", "REFRESH", "DECLINECOUNTER"}
ID_NAMESPACE = uuid.UUID("6f1c8f8e-2b7a-5d43-9a3e-6c2b1f4e8a10")
INPUT_SUFFIXES = {".eml", ".mbox", ".mbx", ".ics", ".ical", ".msg"}
REPORT_LIMIT = 200
DRAFT_HEADERS = {"x-unsent": "1"}
ATTACHMENT_HEADING = {"de": "Anhänge", "en": "Attachments"}
REQUIRED_FIELDS = {
    "message": ("subject", "text", "receivedAt", "headerMessageId", "messageThreadId", "direction", "fromHandle",
                "toHandles", "ccHandles", "visibility", "participants"),
    "calendarEvent": ("title", "startsAt", "endsAt", "isFullDay", "location", "description", "iCalUid", "recurrence",
                      "conferenceLink", "isCanceled", "organizerHandle", "visibility", "participants"),
    "person": ("name", "emails", "company"),
    "company": ("name", "domainName"),
    "workspaceMember": ("userEmail",),
}


def outside(target: Path, path: Path) -> bool:
    return path != target and target not in path.parents


def error_text(exc: Exception) -> str:
    """Error text without local paths: only the file name of an OSError is shown."""
    if isinstance(exc, OSError):
        name = Path(str(exc.filename)).name if getattr(exc, "filename", None) else ""
        return (exc.strerror or "file error") + (f": {name}" if name else "")
    return str(exc)


def record_uuid(kind: str, key: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, f"{kind}:{key}"))


def portable_ref(prefix: str, value: str) -> str:
    ref = f"{prefix}:{value}"
    if len(ref) <= 300 and portable_reference(ref) and not re.search(r"\s", ref):
        return ref
    return f"{prefix}-sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def size_text(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def normalized_words(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


@dataclass
class Item:
    """One message or calendar event that may become a record."""

    kind: str  # message | calendarEvent
    label: str
    sha256: str
    key: str  # headerMessageId or record iCalUid
    mail: Optional[crm_mail.ParsedMail] = None
    event: Optional[crm_ical.IcalEvent] = None
    via: str = ""
    description: str = ""
    existing: Optional[Record] = None
    record_id: str = ""
    mode: str = "create"  # create | update | fill
    values: dict[str, Any] = field(default_factory=dict)
    links: list[str] = field(default_factory=list)
    extra: list[crm_mail.Address] = field(default_factory=list)
    title: str = ""
    visible: bool = True

    def addresses(self) -> list[str]:
        if self.kind == "message":
            seen = [address.email for address in self.mail.addresses()]
            return seen + [address.email for address in self.extra if address.email not in seen]
        people = ([self.event.organizer] if self.event.organizer else []) + self.event.attendees
        return list(dict.fromkeys(person.email for person in people))

    @property
    def origin(self) -> dict[str, Any]:
        """Origin of the events of this record: the core merges it into the request origin."""
        if self.kind == "message":
            return {"kind": "email", "ref": portable_ref("message-id", self.key.strip("<>")), "sha256": self.sha256}
        origin = {"kind": "calendar", "ref": portable_ref("ical-uid", self.key), "sha256": self.sha256}
        if self.via:
            origin["note"] = f"invitation {portable_ref('message-id', self.via.strip('<>'))}"
        return origin

    @property
    def draft(self) -> bool:
        return self.kind == "message" and any(self.mail.header(name).strip() == value for name, value in DRAFT_HEADERS.items())


class Report:
    def __init__(self) -> None:
        self.lists: dict[str, list[dict[str, Any]]] = {name: [] for name in (
            "imported", "updated", "unchanged", "skipped", "errors", "redactions", "merged", "notes")}

    def add(self, name: str, entry: dict[str, Any]) -> None:
        self.lists[name].append(entry)

    def skip(self, label: str, reason: str, detail: str = "") -> None:
        self.add("skipped", {"item": label, "reason": reason, **({"detail": detail} if detail else {})})

    def error(self, label: str, error: str) -> None:
        self.add("errors", {"item": label, "error": error})

    def note(self, label: str, notes: list[str]) -> None:
        for text in notes:
            self.add("notes", {"item": label, "note": text})

    def render(self) -> dict[str, Any]:
        result = {}
        for name, entries in self.lists.items():
            result[name] = entries[:REPORT_LIMIT]
            if len(entries) > REPORT_LIMIT:
                result[f"{name}_truncated"] = len(entries) - REPORT_LIMIT
        return result


def wiki_language(target: Path) -> str:
    try:
        from frontmatter_contract import parse_file

        language = str(parse_file(target / "schema/WIKI_PROFILE.md").data.get("wiki_language") or "en")
    except Exception:  # noqa: BLE001 - a missing profile falls back to English labels
        language = "en"
    return language.split("-", 1)[0].lower()


class Ingest:
    def __init__(self, target: Path, args: argparse.Namespace):
        self.target = target
        self.args = args
        self.datamodel = load_datamodel(target)
        for object_name, fields in REQUIRED_FIELDS.items():
            if object_name not in self.datamodel.objects:
                raise CrmError(f"the data model has no {object_name} object; add the standard objects with crm_init.py --add-missing-standard-objects")
            missing = [name for name in fields if name not in self.datamodel.fields(object_name)]
            if missing:
                raise CrmError(f"{object_name} lacks the fields {', '.join(missing)}")
        self.store = RecordStore(target, self.datamodel)
        settings = load_json_file(target, SETTINGS_PATH, {}) or {}
        mail_settings = settings.get("email") or {}
        self.time_zone = str(settings.get("time_zone") or "UTC")
        self.language = wiki_language(target)
        self.blocklist = [item for item in mail_settings.get("blocklist") or [] if isinstance(item, str)]
        self.free_domains = [item for item in mail_settings.get("free_email_domains") or [] if isinstance(item, str)]
        self.excluded_handles = [item for item in mail_settings.get("excluded_handles") or [] if isinstance(item, str)]
        policy = str(mail_settings.get("contact_creation") or "SENT").strip().upper()
        if policy not in POLICIES:
            raise CrmError(f"settings.email.contact_creation {policy!r} is not one of NONE, SENT, SENT_AND_RECEIVED, work-domains")
        self.policy = policy
        self.exclude_group = bool(mail_settings.get("exclude_group_emails", True))
        self.include_bulk = bool(args.include_bulk)
        self.include_internal = bool(args.include_internal or mail_settings.get("import_internal"))
        self.include_private = bool(args.include_private)
        visibility = (args.visibility or mail_settings.get("default_visibility") or "SHARE_EVERYTHING").strip().upper()
        if visibility not in VISIBILITY_ORDER:
            raise CrmError(f"visibility {visibility!r} must be METADATA, SUBJECT or SHARE_EVERYTHING")
        self.visibility = visibility
        self.windows = crm_ical.load_windows_zones()
        self.report = Report()
        self._index()
        self._own_addresses(mail_settings)
        self.direction_noted = False
        self.batch_series: set[str] = set()
        self.missing_noted: set[str] = set()
        self.unmatched_participants: list[dict[str, Any]] = []

    # -- indexes -------------------------------------------------------------

    def _index(self) -> None:
        self.people: dict[str, list[tuple[int, Record]]] = {}
        for record in sorted(self.store.records("person").values(), key=lambda item: str(item.data.get("crm_created_at") or "")):
            primary = str(record.data.get("emails.primaryEmail") or "").strip().lower()
            if primary:
                self.people.setdefault(primary, []).append((0, record))
            for extra in record.data.get("emails.additionalEmails") or []:
                if isinstance(extra, str) and extra.strip():
                    self.people.setdefault(extra.strip().lower(), []).append((1, record))
        self.members: dict[str, Record] = {}
        for record in self.store.records("workspaceMember").values():
            address = str(record.data.get("userEmail") or "").strip().lower()
            if address and not record.deleted:
                self.members.setdefault(address, record)
        self.companies: dict[str, Record] = {}
        for record in self.store.records("company").values():
            url = record.data.get("domainName.primaryLinkUrl")
            if isinstance(url, str) and url.strip():
                self.companies.setdefault(normalize_url(url, True), record)
            secondary = record.data.get("domainName.secondaryLinks")
            if isinstance(secondary, str) and secondary.strip():
                try:
                    for link in json.loads(secondary):
                        if isinstance(link, dict) and isinstance(link.get("url"), str):
                            self.companies.setdefault(normalize_url(link["url"], True), record)
                except ValueError:
                    pass
        self.messages = {str(record.data.get("headerMessageId")): record for record in self.store.records("message").values() if record.data.get("headerMessageId")}
        self.events = {str(record.data.get("iCalUid")): record for record in self.store.records("calendarEvent").values() if record.data.get("iCalUid")}

    def _own_addresses(self, mail_settings: dict[str, Any]) -> None:
        own: list[str] = []
        sources: list[str] = []
        if self.args.mailbox_owner:
            owner = self.args.mailbox_owner.strip().lower()
            if owner not in self.members:
                trashed = any(str(record.data.get("userEmail") or "").strip().lower() == owner and record.deleted
                              for record in self.store.records("workspaceMember").values())
                if trashed:
                    raise CrmError(f"the workspace member with the login e-mail {owner} is in the trash; restore the member first")
                raise CrmError(f"no workspace member has the login e-mail {owner}; create the member first or use --own-addresses")
            own.append(owner)
            sources.append("argument")
        given = [raw.strip().lower() for raw in (self.args.own_addresses or "").split(",") if raw.strip()]
        configured = [item.strip().lower() for item in mail_settings.get("own_addresses") or [] if isinstance(item, str) and item.strip()]
        for address in given + configured:
            if not crm_mail.EMAIL_RE.fullmatch(address):
                raise CrmError(f"{address!r} is not an e-mail address")
            if address not in own:
                own.append(address)
        if given and "argument" not in sources:
            sources.append("argument")
        if configured:
            sources.append("settings")
        if not given and not configured:
            # Without explicit own addresses the login e-mails of all members (not in the trash) are the own addresses.
            members = [address for address in sorted(self.members) if crm_mail.EMAIL_RE.fullmatch(address)]
            own.extend(address for address in members if address not in own)
            if members:
                sources.append("members")
        self.own = set(own)
        self.own_sources = sources
        domains = {crm_mail.domain_of(address) for address in own if not crm_mail.is_free_email(address, self.free_domains)}
        domains |= {str(item).strip().lower().lstrip("@") for item in mail_settings.get("own_domains") or [] if isinstance(item, str) and item.strip()}
        self.own_domains = {domain for domain in domains if domain}

    def declared(self, object_name: str, values: dict[str, Any]) -> dict[str, Any]:
        """Values of fields the data model has; newer standard fields are optional for older wikis."""
        fields = self.datamodel.fields(object_name)
        missing = sorted(name for name in values if name not in fields and name not in self.missing_noted)
        if missing:
            self.missing_noted.update(missing)
            self.report.add("notes", {"item": "*", "note": f"the data model of {object_name} lacks {', '.join(missing)}; these values are not stored (add the fields with crm_schema.py)"})
        return {name: value for name, value in values.items() if name in fields}

    # -- lookups -------------------------------------------------------------

    def person_for(self, address: str, *, deleted: bool = False) -> Optional[Record]:
        matches = self.people.get(address.lower(), [])
        for priority in (0, 1):
            for rank, record in matches:
                if rank == priority and record.deleted == deleted:
                    return record
        return None

    def participant_links(self, address: str) -> list[str]:
        links = []
        person = self.person_for(address)
        if person is not None:
            links.append(f"person:{person.id}")
        member = self.members.get(address.lower())
        if member is not None:
            links.append(f"workspaceMember:{member.id}")
        return links

    def is_internal_address(self, address: str) -> bool:
        domain = crm_mail.domain_of(address)
        return any(domain == own or domain.endswith("." + own) for own in self.own_domains)

    # -- reading files ---------------------------------------------------------

    def inputs(self) -> list[tuple[str, Path]]:
        found: list[tuple[str, Path]] = []
        for raw in self.args.files:
            path = Path(raw).expanduser().resolve()
            if path.is_dir():
                for entry in sorted(path.rglob("*")):
                    if entry.is_file() and not entry.name.startswith(".") and entry.suffix.lower() in INPUT_SUFFIXES:
                        found.append((f"{path.name}/{entry.relative_to(path).as_posix()}", entry))
            elif path.is_file():
                found.append((path.name, path))
            else:
                self.report.error(path.name, "file or directory not found")
        return found

    def read(self) -> list[Item]:
        items: list[Item] = []
        for label, path in self.inputs():
            try:
                with path.open("rb") as handle:
                    head = handle.read(4096)
            except OSError as exc:
                self.report.error(label, f"cannot read the file: {error_text(exc)}")
                continue
            kind = crm_mail.detect_kind(path, head)
            if kind == "msg":
                self.report.skip(label, "outlook_msg", "Outlook .msg files are not read; save the message as .eml (or drag it out of Outlook as .eml) and import that file")
            elif kind == "unknown":
                self.report.skip(label, "unknown_format", "not an e-mail (.eml, .mbox) or calendar (.ics) file")
            elif kind == "ics":
                data = path.read_bytes()
                text, note = crm_ical.decode_ics_bytes(data)
                if note:
                    self.report.note(label, [note])
                document = crm_ical.parse_ics(text, default_zone=self.time_zone, windows=self.windows)
                self.report.note(label, document.notes)
                if not document.events:
                    self.report.error(label, "the calendar file contains no readable event")
                digest = hashlib.sha256(data).hexdigest()
                for index, event in enumerate(document.events, 1):
                    event_label = label if len(document.events) == 1 else f"{label}#{index}"
                    items.extend(self.event_item(event_label, event, digest, document.method))
            elif kind == "mbox":
                found = 0
                try:
                    for position, data, envelope in crm_mail.iter_mbox(path):
                        found += 1
                        items.extend(self.mail_items(f"{label}#{position}", data, envelope))
                except (OSError, ValueError) as exc:
                    self.report.error(label, f"cannot read the mbox file: {error_text(exc)}")
                else:
                    if not found:
                        self.report.error(label, "the mbox file contains no message (no 'From ' separator lines)")
            else:
                items.extend(self.mail_items(label, path.read_bytes(), None))
        seen: dict[str, int] = {}
        for item in items:
            count = seen.get(item.label, 0) + 1
            seen[item.label] = count
            if count > 1:
                item.label = f"{item.label}~{count}"
        return items

    def mail_items(self, label: str, data: bytes, envelope: Optional[datetime]) -> list[Item]:
        if crm_mail.is_outlook_msg(data):
            self.report.skip(label, "outlook_msg", "Outlook .msg data; export the message as .eml")
            return []
        try:
            mail = crm_mail.parse_mail(data, label, envelope)
        except crm_mail.MailError as exc:
            self.report.error(label, str(exc))
            return []
        self.report.note(label, mail.notes)
        if mail.calendars:
            events: list[Item] = []
            for text in mail.calendars:
                document = crm_ical.parse_ics(text, default_zone=self.time_zone, windows=self.windows)
                self.report.note(label, document.notes)
                if document.method in ITIP_SKIPPED:
                    self.report.skip(label, "invitation_reply", f"METHOD:{document.method} answers an invitation and changes no event")
                    return []
                for event in document.events:
                    description = self.merge_description(mail.text, event)
                    for item in self.event_item(label, event, mail.sha256, document.method, via=mail.message_id, description=description):
                        events.append(item)
                        self.report.add("merged", {"item": label, "message_id": mail.message_id, "into": "calendarEvent",
                                                   "iCalUid": item.key, "detail": "the invitation becomes the event; its text is the event description"})
            if events or any(entry["item"] == label for entry in self.report.lists["skipped"]):
                return events
            self.report.note(label, ["the calendar part cannot be read; the mail is imported as a message"])
        return [Item("message", label, mail.sha256, mail.message_id, mail=mail, title=mail.subject)]

    def merge_description(self, mail_text: str, event: crm_ical.IcalEvent) -> str:
        ics_text = event.description or (crm_mail.html_to_text(event.description_html) if event.description_html else "")
        if not mail_text or not ics_text:
            return mail_text or ics_text
        if normalized_words(ics_text) in normalized_words(mail_text):
            return mail_text
        if normalized_words(mail_text) in normalized_words(ics_text):
            return ics_text
        return mail_text + "\n\n" + ics_text

    def event_item(self, label: str, event: crm_ical.IcalEvent, sha: str, method: str, *, via: str = "", description: Optional[str] = None) -> list[Item]:
        self.report.note(label, event.notes)
        event.method = event.method or method.upper()
        if event.method in ITIP_SKIPPED:
            self.report.skip(label, "invitation_reply", f"METHOD:{event.method} answers an invitation and changes no event")
            return []
        text = event.description or (crm_mail.html_to_text(event.description_html) if event.description_html else "")
        return [Item("calendarEvent", label, sha, event.record_uid, event=event, via=via,
                     description=description if description is not None else text, title=event.summary)]

    # -- filters ---------------------------------------------------------------

    def keep_message(self, item: Item) -> bool:
        mail = item.mail
        addresses = [address.email for address in mail.addresses()]
        counterparts = [address for address in addresses if address not in self.own]
        if counterparts and all(crm_mail.is_unsubscribe_address(address) for address in counterparts):
            self.report.skip(item.label, "unsubscribe_request", "every counterpart is an unsubscribe address")
            return False
        blocked = [address for address in addresses if crm_mail.is_blocklisted(address, self.blocklist, self.own)]
        if blocked:
            self.report.skip(item.label, "blocklist", f"{len(blocked)} participant(s) on the blocklist")
            return False
        if self.own_domains and not self.include_internal and addresses and all(self.is_internal_address(address) for address in addresses):
            self.report.skip(item.label, "internal", "only own domains take part; use --include-internal to import internal mails")
            return False
        if self.exclude_group and not self.include_bulk and mail.from_address not in self.own:
            markers = crm_mail.bulk_headers(mail)
            if markers:
                self.report.skip(item.label, "bulk", f"mailing list or automated mail ({', '.join(markers)}); use --include-bulk to import it")
                return False
            if mail.from_address and crm_mail.is_group_address(mail.from_address):
                self.report.skip(item.label, "group_sender", "sent from a group or no-reply address; use --include-bulk to import it")
                return False
        return True

    def event_addresses(self, event: crm_ical.IcalEvent) -> list[crm_ical.Attendee]:
        people = ([event.organizer] if event.organizer else []) + event.attendees
        seen: set[str] = set()
        result = []
        for person in people:
            if person.email not in seen:
                seen.add(person.email)
                result.append(person)
        return result

    def keep_event(self, item: Item) -> bool:
        event = item.event
        addresses = [person.email for person in self.event_addresses(event)]
        blocked = [address for address in addresses if crm_mail.is_blocklisted(address, self.blocklist, self.own)]
        if blocked:
            self.report.skip(item.label, "blocklist", f"{len(blocked)} participant(s) on the blocklist")
            return False
        # Cancellations and exceptions often list only the organizer; a known series is never internal for them.
        known = event.uid in self.batch_series or any(uid == event.uid or uid.startswith(event.uid + "#") for uid in self.events)
        follow_up = bool(event.recurrence_id) or event.is_canceled
        if self.own_domains and not self.include_internal and addresses and all(self.is_internal_address(address) for address in addresses) and not (follow_up and known):
            self.report.skip(item.label, "internal", "only own domains take part; use --include-internal to import internal events")
            return False
        if event.klass in {"PRIVATE", "CONFIDENTIAL"} and not self.include_private:
            self.report.skip(item.label, "private_event", f"CLASS:{event.klass}; use --include-private to import it")
            return False
        return True

    # -- versions and duplicates ---------------------------------------------

    def resolve_messages(self, items: list[Item]) -> list[Item]:
        first: dict[str, Item] = {}
        result = []
        for item in items:
            if item.key in first:
                kept = first[item.key]
                known = set(kept.addresses())
                kept.extra.extend(address for address in item.mail.addresses() if address.email not in known)
                self.report.add("merged", {"item": item.label, "into_item": kept.label, "message_id": item.key,
                                           "detail": "same Message-ID in this import (for example from a second mailbox); one record"})
                continue
            first[item.key] = item
            existing = self.messages.get(item.key)
            if existing is not None and existing.deleted:
                self.report.skip(item.label, "in_trash", f"message {existing.id} is in the trash; restore it with crm_records.py first")
                continue
            item.existing = existing
            item.record_id = existing.id if existing else record_uuid("message", item.key)
            item.mode = "fill" if existing else "create"
            result.append(item)
        return result

    def resolve_events(self, items: list[Item]) -> list[Item]:
        def version(item: Item) -> tuple[int, str]:
            modified = item.event.last_modified
            return item.event.sequence, (format_instant(modified) if modified else "")

        def batch_order(item: Item) -> tuple[int, str, str]:
            stamp = item.event.dtstamp
            return version(item) + ((format_instant(stamp) if stamp else ""),)

        groups: dict[str, list[Item]] = {}
        for item in items:
            groups.setdefault(item.key, []).append(item)
        result = []
        for key, group in groups.items():
            group.sort(key=batch_order)
            newest = group[-1]
            for older in group[:-1]:
                self.report.skip(older.label, "older_version", f"SEQUENCE {older.event.sequence} is older than SEQUENCE {newest.event.sequence} in {newest.label}")
            existing = self.events.get(key)
            if existing is not None and existing.deleted:
                self.report.skip(newest.label, "in_trash", f"event {existing.id} is in the trash; restore it with crm_records.py first")
                continue
            if existing is None:
                if newest.event.is_canceled and not newest.event.recurrence_id:
                    self.report.skip(newest.label, "cancellation_unknown_event", "the canceled event is not in the CRM")
                    continue
                newest.mode, newest.record_id = "create", record_uuid("calendarEvent", key)
                result.append(newest)
                continue
            newest.existing, newest.record_id = existing, existing.id
            stored_sequence = existing.data.get("iCalSequence")
            known = None
            if isinstance(stored_sequence, (int, float)) and not isinstance(stored_sequence, bool):
                known = (int(stored_sequence), str(existing.data.get("externalUpdatedAt") or ""))
            current = version(newest)
            if known is None:
                newest.mode = "update"
                self.report.note(newest.label, ["the stored version of this event is unknown; the imported version replaces it"])
            elif current > known:
                newest.mode = "update"
            elif current == known:
                newest.mode = "fill"
            else:
                self.report.skip(newest.label, "older_version", f"SEQUENCE {current[0]} is older than the stored SEQUENCE {known[0]}")
                continue
            result.append(newest)
        return result

    # -- record values -------------------------------------------------------

    def redact(self, item: Item, name: str, value: str) -> str:
        cleaned, findings = crm_mail.redact(value)
        for finding in findings:
            self.report.add("redactions", {"item": item.label, "field": name, "line": finding.line, "kind": finding.kind})
        return cleaned

    def message_values(self, item: Item) -> None:
        mail = item.mail
        visibility = self.visibility
        if item.existing is not None:
            stored = str(item.existing.data.get("visibility") or "SHARE_EVERYTHING")
            if VISIBILITY_ORDER.get(stored, 2) < VISIBILITY_ORDER[visibility]:
                visibility = stored
        item.visible = VISIBILITY_ORDER[visibility] >= VISIBILITY_ORDER["SUBJECT"]
        direction = None
        if self.own:
            direction = "OUTGOING" if mail.from_address in self.own else "INCOMING"
        values: dict[str, Any] = {
            "headerMessageId": item.key,
            "messageThreadId": crm_mail.thread_root(mail),
            "receivedAt": format_instant(mail.date) if mail.date else None,
            "direction": direction,
            "fromHandle": mail.from_address or None,
            "toHandles": [address.email for address in mail.to] or None,
            "ccHandles": [address.email for address in mail.cc] or None,
            "visibility": visibility,
            "isDraft": item.draft,
        }
        campaign_id = mail.header("x-lmwiki-campaign").strip().lower()
        if UUID_RE.fullmatch(campaign_id) and "messageCampaign" in self.datamodel.objects:
            campaign = self.store.get("messageCampaign", campaign_id)
            if campaign is not None and not campaign.deleted:
                values["messageCampaign"] = f"messageCampaign:{campaign_id}"
        if VISIBILITY_ORDER[visibility] >= VISIBILITY_ORDER["SUBJECT"] and mail.subject:
            values["subject"] = self.redact(item, "subject", mail.subject)
        if visibility == "SHARE_EVERYTHING":
            text = mail.text
            if mail.attachments:
                heading = ATTACHMENT_HEADING.get(self.language, ATTACHMENT_HEADING["en"])
                listing = "\n".join(f"- {attachment.name} ({size_text(attachment.size)})" for attachment in mail.attachments)
                text = (text + "\n\n" if text else "") + f"{heading}:\n{listing}"
            if text:
                values["text"] = self.redact(item, "text", text)
        links = []
        for address in item.addresses():
            for link in self.participant_links(address):
                if link not in links:
                    links.append(link)
        item.links = links
        item.values = self.declared("message", values)
        item.title = values.get("subject") or ""
        if direction is None and not self.direction_noted:
            self.direction_noted = True
            self.report.add("notes", {"item": "*", "note": "direction is not set: no own addresses are known (--mailbox-owner, --own-addresses or settings.email.own_addresses)"})

    def event_values(self, item: Item) -> None:
        event = item.event
        visibility = "METADATA" if self.visibility != "SHARE_EVERYTHING" else "SHARE_EVERYTHING"
        if self.visibility == "SUBJECT":
            self.report.note(item.label, ["calendar events know only METADATA and SHARE_EVERYTHING; SUBJECT was applied as METADATA"])
        if item.existing is not None and str(item.existing.data.get("visibility") or "SHARE_EVERYTHING") == "METADATA":
            visibility = "METADATA"
        elif item.existing is not None and visibility == "METADATA" and item.mode == "update":
            self.report.note(item.label, ["the newer version is imported with visibility METADATA; title, description, location and conference link of the stored event are removed"])
        item.visible = visibility == "SHARE_EVERYTHING"
        values: dict[str, Any] = {
            "iCalUid": item.key,
            "startsAt": format_instant(event.starts_at) if event.starts_at else None,
            "endsAt": format_instant(event.ends_at) if event.ends_at else None,
            "isFullDay": event.all_day,
            "isCanceled": event.is_canceled,
            "organizerHandle": event.organizer.email if event.organizer else None,
            "recurrence": event.recurrence_text() or None,
            "visibility": visibility,
            "iCalSequence": event.sequence,
            "externalCreatedAt": format_instant(event.created) if event.created else None,
            "externalUpdatedAt": format_instant(event.last_modified) if event.last_modified else None,
            "conferenceSolution": event.conference_solution or None,
        }
        values.update({"title": None, "location": None, "conferenceLink": None, "description": ""})
        if visibility == "SHARE_EVERYTHING":
            values["title"] = self.redact(item, "title", event.summary) if event.summary else None
            values["location"] = self.redact(item, "location", event.location) if event.location else None
            conference = self.redact(item, "conferenceLink", event.conference_url) if event.conference_url else ""
            values["conferenceLink"] = {"primaryLinkUrl": conference} if conference else None
            description, note = crm_mail.clean_text(item.description)
            if note:
                self.report.note(item.label, [note])
            values["description"] = self.redact(item, "description", description) if description else ""
        links = []
        for address in item.addresses():
            for link in self.participant_links(address):
                if link not in links:
                    links.append(link)
        item.links = links
        item.values = self.declared("calendarEvent", values)
        item.title = values.get("title") or ""

    def screen(self, item: Item) -> bool:
        """secret_screen must find nothing in what would be written."""
        texts: list[tuple[str, str]] = []
        for name, value in item.values.items():
            if isinstance(value, str):
                texts.append((name, value))
            elif isinstance(value, list):
                texts.extend((name, entry) for entry in value if isinstance(entry, str))
            elif isinstance(value, dict):
                texts.extend((f"{name}.{sub}", entry) for sub, entry in value.items() if isinstance(entry, str))
        problems = []
        for name, text in texts:
            for line, kind in crm_mail.screen(text):
                problems.append(f"{name} line {line}: {kind}")
        if problems:
            self.report.error(item.label, "possible credential remains after redaction (" + "; ".join(problems[:5]) + "); the record is not planned")
            return False
        return True

    def gap_values(self, item: Item) -> dict[str, Any]:
        """Values for an existing record: only empty fields, plus new participants."""
        record = item.existing
        result: dict[str, Any] = {}
        for name, value in item.values.items():
            if value in (None, "", []) or name in {"visibility", "headerMessageId", "iCalUid"}:
                continue
            definition = self.datamodel.fields(record.object)[name]
            ftype = definition["type"]
            if ftype == "RICH_TEXT":
                empty = not record.richtext.get(name, "").strip()
            elif ftype == "LINKS":
                empty = not record.data.get(f"{name}.primaryLinkUrl")
            else:
                empty = record.data.get(name) in (None, "", [])
            if empty:
                result[name] = value
        current = record.data.get("participants") or []
        current_ids = {parse_record_link(link)[1] for link in current if isinstance(link, str) and parse_record_link(link)}
        new_links = [link for link in item.links if link.split(":", 1)[1] not in current_ids]
        if new_links:
            result["participants"] = {"add": new_links}
        return result

    # -- contact proposals ---------------------------------------------------

    def proposal_candidates(self, item: Item) -> list[crm_mail.Address]:
        if self.policy == "NONE":
            return []
        if item.kind == "message":
            mail = item.mail
            if item.draft:
                return []
            if self.policy in {"SENT", "WORK-DOMAINS"} and mail.from_address not in self.own:
                return []
            return [address for address in mail.addresses() + item.extra if address.email not in self.own]
        return [crm_mail.Address(person.email, person.name) for person in self.event_addresses(item.event)
                if person.email not in self.own and person.cutype not in {"ROOM", "RESOURCE"}]

    def address_reason(self, address: str) -> str:
        """Why an address never becomes a contact proposal, or "" when it may."""
        if address in self.own:
            return "own_address"
        if self.person_for(address, deleted=True) is not None:
            return "person_in_trash"
        if crm_mail.is_free_email(address, self.free_domains):
            return "free_email"
        if crm_mail.is_blocklisted(address, self.blocklist, self.own):
            return "blocklist"
        if crm_mail.is_blocklisted(address, self.excluded_handles):
            return "excluded_handle"
        if self.exclude_group and crm_mail.is_group_address(address):
            return "group_address"
        if self.is_internal_address(address) and not self.include_internal:
            return "internal"
        return ""

    def policy_detail(self, item: Item) -> str:
        if item.draft:
            return "drafts never create contacts"
        if self.policy == "NONE":
            return "settings.email.contact_creation is NONE"
        if item.kind == "message" and self.policy in {"SENT", "WORK-DOMAINS"}:
            return f"contact_creation {self.policy.lower() if self.policy == 'WORK-DOMAINS' else self.policy} proposes only participants of mails sent from own addresses"
        return ""

    def unmatched(self, items: list[Item], people: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Every participant without a person or member, so that the user can still be offered a contact."""
        found: dict[str, dict[str, Any]] = {}
        for item in items:
            if item.kind == "message":
                participants = [(address.email, address.name, "") for address in item.mail.addresses() + item.extra]
            else:
                participants = [(person.email, person.name, person.cutype) for person in self.event_addresses(item.event)]
            seen_here: set[str] = set()
            for email_address, name, cutype in participants:
                if email_address in seen_here or email_address in self.members or self.person_for(email_address) is not None:
                    continue
                seen_here.add(email_address)
                entry = found.setdefault(email_address, {"email": email_address, "name": "", "messages": 0, "events": 0,
                                                         "reason": "", "detail": "", "items": []})
                entry["name"] = entry["name"] or name
                entry["messages" if item.kind == "message" else "events"] += 1
                if len(entry["items"]) < 5:
                    entry["items"].append(item.label)
                if email_address in people:
                    entry["reason"], entry["detail"] = "proposed", "see contact_suggestions"
                    continue
                if entry["reason"]:
                    continue
                reason = self.address_reason(email_address)
                if not reason and cutype in {"ROOM", "RESOURCE"}:
                    reason = "room_or_resource"
                if reason:
                    entry["reason"] = reason
                else:
                    entry["reason"], entry["detail"] = "policy", self.policy_detail(item)
        return sorted(found.values(), key=lambda entry: (-(entry["messages"] + entry["events"]), entry["email"]))

    def proposals(self, items: list[Item]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        people: dict[str, dict[str, Any]] = {}
        companies: dict[str, dict[str, Any]] = {}
        declined: dict[str, dict[str, Any]] = {}
        for item in items:
            for address in self.proposal_candidates(item):
                email_address = address.email
                if email_address in people:
                    people[email_address]["items"].append(item.label)
                    continue
                if email_address in declined or email_address in self.members or self.person_for(email_address) is not None:
                    continue
                reason = self.address_reason(email_address)
                if reason:
                    declined[email_address] = {"email": email_address, "reason": reason}
                    continue
                first, last = crm_mail.person_name(address.name, email_address)
                proposal: dict[str, Any] = {
                    "email": email_address, "firstName": first, "lastName": last, "items": [item.label],
                    "source": "EMAIL" if item.kind == "message" else "CALENDAR", "origin": item.origin,
                }
                domain = crm_mail.registrable_domain(crm_mail.domain_of(email_address))
                if domain:
                    company = self.companies.get(normalize_url(domain, True))
                    if company is not None and not company.deleted:
                        proposal["company"] = {"id": company.id, "name": company.data.get("crm_title")}
                    elif company is not None:
                        proposal["company_note"] = f"company {company.id} for {domain} is in the trash"
                    elif not crm_mail.is_blocklisted("x@" + domain, self.blocklist):
                        proposal["company"] = {"domain": domain, "name": crm_mail.company_name_from_domain(domain)}
                        companies.setdefault(domain, {"domain": domain, "name": crm_mail.company_name_from_domain(domain),
                                                      "source": proposal["source"], "origin": item.origin, "people": []})
                        companies[domain]["people"].append(email_address)
                people[email_address] = proposal
        return people, companies, list(declined.values())

    # -- operations ----------------------------------------------------------

    def operations(self, items: list[Item], people: dict[str, dict[str, Any]], companies: dict[str, dict[str, Any]],
                   excluded: set[str]) -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
        ops: list[dict[str, Any]] = []
        owners: list[str] = []
        record_owner: dict[str, str] = {}
        created_people: dict[str, str] = {}
        if self.args.create_contacts:
            for domain, company in sorted(companies.items()):
                owner = f"company:{domain}"
                if owner in excluded:
                    continue
                record_id = record_uuid("company", domain)
                ops.append({"op": "create", "object": "company", "id": record_id, "ref": owner, "origin": company["origin"],
                            "values": {"name": company["name"], "domainName": {"primaryLinkUrl": f"https://{domain}"}}})
                owners.append(owner)
                record_owner[record_id] = owner
            for email_address, person in sorted(people.items()):
                owner = f"person:{email_address}"
                if owner in excluded:
                    continue
                values: dict[str, Any] = {"name": {"firstName": person["firstName"], "lastName": person["lastName"]},
                                          "emails": {"primaryEmail": email_address}}
                company = person.get("company") or {}
                if company.get("id"):
                    values["company"] = f"company:{company['id']}"
                elif company.get("domain") and f"company:{company['domain']}" not in excluded:
                    values["company"] = {"ref": f"company:{company['domain']}"}
                record_id = record_uuid("person", email_address)
                ops.append({"op": "create", "object": "person", "id": record_id, "ref": owner, "origin": person["origin"], "values": values})
                owners.append(owner)
                record_owner[record_id] = owner
                created_people[email_address] = owner
        for item in items:
            owner = f"item:{item.label}"
            if owner in excluded:
                continue
            links = list(item.links)
            for address in item.addresses():
                if address in created_people:
                    links.append({"object": "person", "ref": created_people[address]})
            if item.mode == "create":
                values = {key: value for key, value in item.values.items() if value not in (None, "", [])}
                if links:
                    values["participants"] = links
                ops.append({"op": "create", "object": item.kind, "id": item.record_id, "origin": item.origin, "values": values})
            elif item.mode == "update" and item.event is not None and item.event.is_canceled:
                # A cancellation identifies the event; its other properties do not replace the stored details.
                values = {key: item.values[key] for key in ("isCanceled", "iCalSequence", "externalUpdatedAt") if item.values.get(key) is not None}
                ops.append({"op": "update", "object": item.kind, "record": item.record_id, "origin": item.origin, "values": values})
            elif item.mode == "update":
                values = dict(item.values)
                values["participants"] = links
                ops.append({"op": "update", "object": item.kind, "record": item.record_id, "origin": item.origin, "values": values})
            else:
                item.links = [link for link in links if isinstance(link, str)]
                values = self.gap_values(item)
                extra = [link for link in links if isinstance(link, dict)]
                if extra:
                    values.setdefault("participants", {"add": []})["add"].extend(extra)
                if not values:
                    continue
                ops.append({"op": "update", "object": item.kind, "record": item.record_id, "origin": item.origin, "values": values})
            owners.append(owner)
            record_owner[item.record_id] = owner
        return ops, owners, record_owner

    def request(self, actor: str, ops: list[dict[str, Any]], accepted: list[Item], *,
                blocking: Optional[list[dict[str, Any]]] = None, annotations: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """A core request; every operation carries the origin of its file, the request only the occasion."""
        request: dict[str, Any] = {
            "actor": actor,
            "origin": {"kind": "email" if any(item.kind == "message" for item in accepted) else "calendar",
                       "occasion": "e-mail and calendar import"},
            "operations": ops,
        }
        if blocking:
            request["blocking_errors"] = blocking
        if annotations:
            request["annotations"] = annotations
        return request

    def plan(self, actor: str) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
        items = self.read()
        self.batch_series = {item.event.uid for item in items if item.kind == "calendarEvent" and not item.event.recurrence_id and not item.event.is_canceled}
        messages = [item for item in items if item.kind == "message" and self.keep_message(item)]
        events = [item for item in items if item.kind == "calendarEvent" and self.keep_event(item)]
        planned = self.resolve_messages(messages) + self.resolve_events(events)
        accepted = []
        for item in planned:
            if item.kind == "message":
                self.message_values(item)
            else:
                self.event_values(item)
            if self.screen(item):
                accepted.append(item)
        people, companies, declined = self.proposals(accepted)
        self.unmatched_participants = self.unmatched(accepted, people)
        excluded: set[str] = set()
        plan = None
        ops: list[dict[str, Any]] = []
        for _attempt in range(5):
            ops, owners, record_owner = self.operations(accepted, people, companies, excluded)
            if not ops:
                plan = None
                break
            plan = plan_transaction(self.target, self.request(actor, ops, accepted))
            if not plan["errors"]:
                break
            # A failing operation is attributed to its file (or proposed contact) and left out of the next round.
            failed: set[str] = set()
            for error in plan["errors"]:
                owner = owners[error["operation"]] if isinstance(error.get("operation"), int) and error["operation"] < len(owners) else record_owner.get(str(error.get("record")))
                if owner is None:
                    failed = set()
                    break
                failed.add(owner)
                label = owner.split(":", 1)[1]
                self.report.error(label, f"{error.get('object') or ''}: {error.get('error')}".strip(": "))
            if not failed:
                break
            excluded |= failed
        self.classify(plan, accepted)
        sections = self.sections(people, companies, declined)
        errors = self.report.lists["errors"]
        blocking = bool(errors) and not self.args.skip_failed
        if plan is not None:
            # The final plan carries the report and the blocking errors inside its hash: the user confirms both.
            blocking_errors = [{"error": entry["error"], "row": entry["item"]} for entry in errors] if blocking else []
            plan = plan_transaction(self.target, self.request(actor, ops, accepted, blocking=blocking_errors, annotations={"ingest": sections}))
            state = "invalid" if plan["errors"] else "planned"
        else:
            state = "invalid" if blocking else "nothing_to_import"
        report: dict[str, Any] = {"state": state, **{key: value for key, value in sections.items() if key != "format"}}
        if plan is not None:
            report.update({
                "plan_sha256": plan["plan_sha256"],
                "summary": plan["summary"],
                "files": len(plan["files"]),
                "destructive": plan["destructive"],
                "plan_errors": plan["errors"][:100],
                "warnings": plan["warnings"][:50],
            })
        if blocking:
            report["next_step"] = "fix the failing files or, after the user agreed, plan again with --skip-failed to leave them out"
        elif plan is not None:
            report["next_step"] = "show the summary; after confirmation apply with crm_ingest.py apply --expect-plan-sha256, then rebuild the CRM views"
        return plan, report

    def classify(self, plan: Optional[dict[str, Any]], items: list[Item]) -> None:
        """Imported, updated or unchanged, read from the events the plan would write."""
        touched: dict[str, list[str]] = {}
        for shard in (plan or {}).get("event_shards", []):
            for event in shard["append"]:
                touched.setdefault(event["record_id"], []).append(event["op"])
        failed_labels = {entry["item"] for entry in self.report.lists["errors"]}
        for item in items:
            if item.label in failed_labels:
                continue
            entry = {"item": item.label, "object": item.kind, "id": item.record_id, "key": item.key,
                     "title": (item.title or "") if item.visible else "(hidden by visibility)"}
            if item.kind == "calendarEvent" and item.event.is_canceled:
                entry["canceled"] = True
            if item.draft:
                entry["draft"] = True
            ops = touched.get(item.record_id, [])
            if "create" in ops:
                self.report.add("imported", entry)
            elif ops:
                self.report.add("updated", entry)
            else:
                self.report.add("unchanged", entry)

    def sections(self, people: dict[str, dict[str, Any]], companies: dict[str, dict[str, Any]], declined: list[dict[str, Any]]) -> dict[str, Any]:
        sections: dict[str, Any] = {
            "format": INGEST_FORMAT,
            "counts": self.counts(people, companies),
            **self.report.render(),
            "contact_suggestions": {
                "included_in_plan": bool(self.args.create_contacts),
                "policy": self.policy.lower() if self.policy == "WORK-DOMAINS" else self.policy,
                "people": [{key: value for key, value in person.items() if key != "origin"} for person in list(people.values())[:REPORT_LIMIT]],
                "companies": [{key: value for key, value in company.items() if key != "origin"} for company in list(companies.values())[:REPORT_LIMIT]],
                "not_suggested": declined[:REPORT_LIMIT],
            },
            "unmatched_participants": [{key: value for key, value in entry.items() if value not in ("", [])}
                                       for entry in self.unmatched_participants[:REPORT_LIMIT]],
            "settings": {
                "visibility": self.visibility, "own_addresses": len(self.own), "own_addresses_from": self.own_sources,
                "own_address_list": sorted(self.own)[:50], "own_domains": sorted(self.own_domains),
                "include_bulk": self.include_bulk, "include_internal": self.include_internal, "include_private": self.include_private,
                "exclude_group_emails": self.exclude_group, "blocklist_entries": len(self.blocklist), "time_zone": self.time_zone,
            },
        }
        if not self.args.create_contacts and people:
            sections["contact_suggestions"]["next_step"] = "plan again with --create-contacts to add the proposed people and companies"
        return sections

    def counts(self, people: dict[str, Any], companies: dict[str, Any]) -> dict[str, int]:
        lists = self.report.lists
        return {
            "imported": len(lists["imported"]), "updated": len(lists["updated"]), "unchanged": len(lists["unchanged"]),
            "skipped": len(lists["skipped"]), "errors": len(lists["errors"]), "merged": len(lists["merged"]),
            "redactions": len(lists["redactions"]), "contact_suggestions": len(people), "company_suggestions": len(companies),
            "unmatched_participants": len(self.unmatched_participants),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--target", required=True)
    plan_parser.add_argument("--lock-token", required=True)
    plan_parser.add_argument("--files", required=True, nargs="+", help=".eml, .mbox and .ics files or directories")
    plan_parser.add_argument("--actor", required=True)
    plan_parser.add_argument("--output", required=True)
    plan_parser.add_argument("--mailbox-owner", help="login e-mail of the workspace member who owns the mailbox")
    plan_parser.add_argument("--own-addresses", default="", help="comma-separated own addresses and aliases")
    plan_parser.add_argument("--include-bulk", action="store_true", help="import mailing-list, automated and group mails")
    plan_parser.add_argument("--include-internal", action="store_true", help="import mails and events between own domains only")
    plan_parser.add_argument("--include-private", action="store_true", help="import events marked private or confidential")
    plan_parser.add_argument("--create-contacts", action="store_true", help="add the proposed people and companies to the plan")
    plan_parser.add_argument("--visibility", choices=sorted(VISIBILITY_ORDER), help="default: settings.email.default_visibility")
    plan_parser.add_argument("--skip-failed", action="store_true", help="leave failing items out instead of blocking the plan")
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--target", required=True)
    apply_parser.add_argument("--lock-token", required=True)
    apply_parser.add_argument("--plan-file", required=True)
    apply_parser.add_argument("--expect-plan-sha256", required=True)
    args = parser.parse_args()
    target = Path(args.target).expanduser().resolve()
    require_lock(target, args.lock_token)
    try:
        if args.command == "plan":
            if not valid_actor(args.actor):
                raise CrmError("--actor must be human:<id>, agent/<name> or process:<id>")
            output = Path(args.output).expanduser().resolve()
            if not outside(target, output):
                raise CrmError("the plan must be written outside the wiki")
            plan, report = Ingest(target, args).plan(args.actor)
            if plan is not None:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                report["plan_file"] = output.name
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1 if report["state"] == "invalid" else 0
        plan = verify_plan(json.loads(Path(args.plan_file).read_text(encoding="utf-8")), args.expect_plan_sha256)
        ingest = (plan.get("annotations") or {}).get("ingest") or {}
        if ingest.get("format") != INGEST_FORMAT:
            raise CrmError("this is not an e-mail and calendar import plan; apply it with crm_records.py")
        result, code = apply_transaction(target, args.lock_token, plan)
        result["ingest_counts"] = ingest.get("counts", {})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except StalePlanError as exc:
        print(json.dumps({"state": "stale_plan", "writes": 0, "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 3
    except (OSError, json.JSONDecodeError, CrmError, crm_ical.IcalError) as exc:
        print(json.dumps({"state": "error", "error": error_text(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
