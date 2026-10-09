#!/usr/bin/env python3
"""Standard CRM data model, settings, views and dashboards, following common CRM conventions.

Field API names, types, options and relation directions follow the common standard
objects so imports and exports stay compatible. Labels follow the wiki language;
German and English are built in, every other language receives English labels.
Two deliberate simplifications are documented in references/crm-contract.md:
note and task targets are one multi-target field instead of the noteTarget and
taskTarget junction objects, and e-mail and calendar participants are links on
the message or event itself.
"""

from __future__ import annotations

from typing import Any

from crm_contract import DATAMODEL_FORMAT
from crm_messaging_objects import messaging_objects

L = {
    "de": {
        "name": "Name", "domainName": "Domain", "address": "Adresse", "linkedinLink": "LinkedIn",
        "annualRevenue": "Jahresumsatz", "accountOwner": "Kundenbetreuer", "people": "Personen",
        "opportunities": "Verkaufschancen", "emails": "E-Mails", "jobTitle": "Position",
        "phones": "Telefon", "company": "Firma", "pointOfContactForOpportunities": "Ansprechpartner für",
        "amount": "Betrag", "closeDate": "Abschlussdatum", "stage": "Phase", "pointOfContact": "Ansprechpartner",
        "owner": "Verantwortlich", "title": "Titel", "bodyV2": "Inhalt", "dueAt": "Fällig am",
        "status": "Status", "assignee": "Zuständig", "targets": "Bezug", "userEmail": "Login-E-Mail",
        "accountOwnerForCompanies": "Betreut Firmen", "ownedOpportunities": "Verantwortet Chancen",
        "assignedTasks": "Zugewiesene Aufgaben", "file": "Datei", "target": "Gehört zu",
        "subject": "Betreff", "text": "Text", "receivedAt": "Empfangen am", "headerMessageId": "Message-ID",
        "messageThreadId": "Konversation", "direction": "Richtung", "fromHandle": "Von",
        "toHandles": "An", "ccHandles": "Cc", "participants": "Beteiligte", "visibility": "Sichtbarkeit",
        "startsAt": "Beginn", "endsAt": "Ende", "isFullDay": "Ganztägig", "location": "Ort",
        "description": "Beschreibung", "iCalUid": "iCal-UID", "recurrence": "Wiederholung",
        "conferenceLink": "Konferenz-Link", "isCanceled": "Abgesagt", "organizerHandle": "Organisator",
        "NEW": "Neu", "SCREENING": "Prüfung", "MEETING": "Termin", "PROPOSAL": "Angebot", "CUSTOMER": "Kunde",
        "TODO": "Offen", "IN_PROGRESS": "In Arbeit", "DONE": "Erledigt",
        "INCOMING": "Eingehend", "OUTGOING": "Ausgehend",
        "METADATA": "Nur Metadaten", "SUBJECT": "Betreff", "SHARE_EVERYTHING": "Alles",
        "listMemberships": "Listen", "messageCampaign": "Kampagne", "isDraft": "Entwurf", "messages": "E-Mails",
        "externalCreatedAt": "Erstellt (extern)", "externalUpdatedAt": "Geändert (extern)",
        "conferenceSolution": "Konferenzdienst", "iCalSequence": "iCal-Sequenz",
    },
    "en": {
        "name": "Name", "domainName": "Domain", "address": "Address", "linkedinLink": "LinkedIn",
        "annualRevenue": "Annual revenue", "accountOwner": "Account owner", "people": "People",
        "opportunities": "Opportunities", "emails": "Emails", "jobTitle": "Job title",
        "phones": "Phones", "company": "Company", "pointOfContactForOpportunities": "Point of contact for",
        "amount": "Amount", "closeDate": "Close date", "stage": "Stage", "pointOfContact": "Point of contact",
        "owner": "Owner", "title": "Title", "bodyV2": "Body", "dueAt": "Due date",
        "status": "Status", "assignee": "Assignee", "targets": "Relations", "userEmail": "Login email",
        "accountOwnerForCompanies": "Account owner for", "ownedOpportunities": "Owned opportunities",
        "assignedTasks": "Assigned tasks", "file": "File", "target": "Attached to",
        "subject": "Subject", "text": "Text", "receivedAt": "Received at", "headerMessageId": "Message-ID",
        "messageThreadId": "Thread", "direction": "Direction", "fromHandle": "From",
        "toHandles": "To", "ccHandles": "Cc", "participants": "Participants", "visibility": "Visibility",
        "startsAt": "Starts at", "endsAt": "Ends at", "isFullDay": "All day", "location": "Location",
        "description": "Description", "iCalUid": "iCal UID", "recurrence": "Recurrence",
        "conferenceLink": "Conference link", "isCanceled": "Canceled", "organizerHandle": "Organizer",
        "NEW": "New", "SCREENING": "Screening", "MEETING": "Meeting", "PROPOSAL": "Proposal", "CUSTOMER": "Customer",
        "TODO": "To do", "IN_PROGRESS": "In progress", "DONE": "Done",
        "INCOMING": "Incoming", "OUTGOING": "Outgoing",
        "METADATA": "Metadata only", "SUBJECT": "Subject", "SHARE_EVERYTHING": "Everything",
        "listMemberships": "Lists", "messageCampaign": "Campaign", "isDraft": "Draft", "messages": "Emails",
        "externalCreatedAt": "Created (external)", "externalUpdatedAt": "Updated (external)",
        "conferenceSolution": "Conference solution", "iCalSequence": "iCal sequence",
    },
}

OBJECT_LABELS = {
    "de": {
        "company": ("Firma", "Firmen"), "person": ("Person", "Personen"),
        "opportunity": ("Verkaufschance", "Verkaufschancen"), "task": ("Aufgabe", "Aufgaben"),
        "note": ("Notiz", "Notizen"), "workspaceMember": ("Mitglied", "Mitglieder"),
        "attachment": ("Anhang", "Anhänge"), "message": ("E-Mail", "E-Mails"),
        "calendarEvent": ("Termin", "Termine"),
    },
    "en": {
        "company": ("Company", "Companies"), "person": ("Person", "People"),
        "opportunity": ("Opportunity", "Opportunities"), "task": ("Task", "Tasks"),
        "note": ("Note", "Notes"), "workspaceMember": ("Member", "Members"),
        "attachment": ("Attachment", "Attachments"), "message": ("Email", "Emails"),
        "calendarEvent": ("Calendar event", "Calendar events"),
    },
}

COLORS = {"NEW": "red", "SCREENING": "purple", "MEETING": "sky", "PROPOSAL": "turquoise", "CUSTOMER": "yellow",
          "TODO": "sky", "IN_PROGRESS": "purple", "DONE": "green"}


def labels(language: str) -> dict[str, Any]:
    base = (language or "en").split("-", 1)[0].lower()
    return L.get(base, L["en"])


def object_labels(language: str) -> dict[str, tuple[str, str]]:
    base = (language or "en").split("-", 1)[0].lower()
    return OBJECT_LABELS.get(base, OBJECT_LABELS["en"])


def _field(t: dict[str, Any], name: str, ftype: str, **extra: Any) -> dict[str, Any]:
    definition = {"type": ftype, "label": t.get(name, name), "standard": True}
    definition.update(extra)
    return definition


def _options(t: dict[str, Any], values: list[str]) -> list[dict[str, Any]]:
    return [
        {"value": value, "label": t.get(value, value.replace("_", " ").title()), "color": COLORS.get(value, "gray"), "position": index}
        for index, value in enumerate(values)
    ]


def _object(t: dict[str, Any], name: str, label_field: str, fields: dict[str, Any], **extra: Any) -> dict[str, Any]:
    singular, plural = t["__objects__"][name]
    definition = {"labelSingular": singular, "labelPlural": plural, "labelField": label_field,
                  "standard": True, "active": True, "fields": fields}
    definition.update(extra)
    return definition


def standard_datamodel(language: str, currency: str = "EUR") -> dict[str, Any]:
    t = dict(labels(language))
    t["__objects__"] = object_labels(language)
    m2o = lambda target, inverse=None, on_delete="SET_NULL": {"type": "MANY_TO_ONE", "target": target, "inverse": inverse, "onDelete": on_delete}  # noqa: E731
    o2m = lambda target, inverse: {"type": "ONE_TO_MANY", "target": target, "inverse": inverse}  # noqa: E731
    objects = {
        "company": _object(t, "company", "name", {
            "name": _field(t, "name", "TEXT"),
            "domainName": _field(t, "domainName", "LINKS", unique=True),
            "address": _field(t, "address", "ADDRESS"),
            "linkedinLink": _field(t, "linkedinLink", "LINKS"),
            "annualRevenue": _field(t, "annualRevenue", "CURRENCY", default={"currencyCode": currency}),
            "accountOwner": _field(t, "accountOwner", "RELATION", relation=m2o("workspaceMember", "accountOwnerForCompanies")),
            "people": _field(t, "people", "RELATION", relation=o2m("person", "company")),
            "opportunities": _field(t, "opportunities", "RELATION", relation=o2m("opportunity", "company")),
        }),
        "person": _object(t, "person", "name", {
            "name": _field(t, "name", "FULL_NAME"),
            "emails": _field(t, "emails", "EMAILS", unique=True),
            "linkedinLink": _field(t, "linkedinLink", "LINKS"),
            "jobTitle": _field(t, "jobTitle", "TEXT"),
            "phones": _field(t, "phones", "PHONES"),
            "company": _field(t, "company", "RELATION", relation=m2o("company", "people")),
            "pointOfContactForOpportunities": _field(t, "pointOfContactForOpportunities", "RELATION", relation=o2m("opportunity", "pointOfContact")),
            "listMemberships": _field(t, "listMemberships", "RELATION", relation=o2m("messageListMember", "person")),
        }),
        "opportunity": _object(t, "opportunity", "name", {
            "name": _field(t, "name", "TEXT"),
            "amount": _field(t, "amount", "CURRENCY", default={"currencyCode": currency}),
            "closeDate": _field(t, "closeDate", "DATE_TIME"),
            "stage": _field(t, "stage", "SELECT", nullable=False, default="NEW",
                            options=_options(t, ["NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER"])),
            "pointOfContact": _field(t, "pointOfContact", "RELATION", relation=m2o("person", "pointOfContactForOpportunities")),
            "company": _field(t, "company", "RELATION", relation=m2o("company", "opportunities")),
            "owner": _field(t, "owner", "RELATION", relation=m2o("workspaceMember", "ownedOpportunities")),
        }),
        "task": _object(t, "task", "title", {
            "title": _field(t, "title", "TEXT"),
            "bodyV2": _field(t, "bodyV2", "RICH_TEXT"),
            "dueAt": _field(t, "dueAt", "DATE_TIME"),
            "status": _field(t, "status", "SELECT", default="TODO", options=_options(t, ["TODO", "IN_PROGRESS", "DONE"])),
            "assignee": _field(t, "assignee", "RELATION", relation=m2o("workspaceMember", "assignedTasks")),
            "targets": _field(t, "targets", "MORPH_RELATION", relation={"targets": ["person", "company", "opportunity"], "multiple": True, "onDelete": "SET_NULL"}),
        }),
        "note": _object(t, "note", "title", {
            "title": _field(t, "title", "TEXT"),
            "bodyV2": _field(t, "bodyV2", "RICH_TEXT"),
            "targets": _field(t, "targets", "MORPH_RELATION", relation={"targets": ["person", "company", "opportunity"], "multiple": True, "onDelete": "SET_NULL"}),
        }),
        "workspaceMember": _object(t, "workspaceMember", "name", {
            "name": _field(t, "name", "FULL_NAME", nullable=False),
            "userEmail": _field(t, "userEmail", "TEXT", unique=True),
            "accountOwnerForCompanies": _field(t, "accountOwnerForCompanies", "RELATION", relation=o2m("company", "accountOwner")),
            "ownedOpportunities": _field(t, "ownedOpportunities", "RELATION", relation=o2m("opportunity", "owner")),
            "assignedTasks": _field(t, "assignedTasks", "RELATION", relation=o2m("task", "assignee")),
        }),
        "attachment": _object(t, "attachment", "name", {
            "name": _field(t, "name", "TEXT"),
            "file": _field(t, "file", "FILES"),
            "target": _field(t, "target", "MORPH_RELATION", relation={"targets": ["person", "company", "opportunity", "task", "note"], "multiple": False, "onDelete": "CASCADE"}),
        }),
        "message": _object(t, "message", "subject", {
            "subject": _field(t, "subject", "TEXT"),
            "text": _field(t, "text", "RICH_TEXT"),
            "receivedAt": _field(t, "receivedAt", "DATE_TIME"),
            "headerMessageId": _field(t, "headerMessageId", "TEXT", unique=True),
            "messageThreadId": _field(t, "messageThreadId", "TEXT"),
            "direction": _field(t, "direction", "SELECT", options=_options(t, ["INCOMING", "OUTGOING"])),
            "fromHandle": _field(t, "fromHandle", "TEXT"),
            "toHandles": _field(t, "toHandles", "ARRAY"),
            "ccHandles": _field(t, "ccHandles", "ARRAY"),
            "visibility": _field(t, "visibility", "SELECT", default="SHARE_EVERYTHING",
                                 options=_options(t, ["METADATA", "SUBJECT", "SHARE_EVERYTHING"])),
            "participants": _field(t, "participants", "MORPH_RELATION", relation={"targets": ["person", "workspaceMember"], "multiple": True, "onDelete": "SET_NULL"}),
            "messageCampaign": _field(t, "messageCampaign", "RELATION", relation=m2o("messageCampaign", "messages")),
            "isDraft": _field(t, "isDraft", "BOOLEAN"),
        }),
        "calendarEvent": _object(t, "calendarEvent", "title", {
            "title": _field(t, "title", "TEXT"),
            "startsAt": _field(t, "startsAt", "DATE_TIME"),
            "endsAt": _field(t, "endsAt", "DATE_TIME"),
            "isFullDay": _field(t, "isFullDay", "BOOLEAN"),
            "location": _field(t, "location", "TEXT"),
            "description": _field(t, "description", "RICH_TEXT"),
            "iCalUid": _field(t, "iCalUid", "TEXT", unique=True),
            "recurrence": _field(t, "recurrence", "TEXT"),
            "conferenceLink": _field(t, "conferenceLink", "LINKS"),
            "isCanceled": _field(t, "isCanceled", "BOOLEAN"),
            "organizerHandle": _field(t, "organizerHandle", "TEXT"),
            "externalCreatedAt": _field(t, "externalCreatedAt", "DATE_TIME"),
            "externalUpdatedAt": _field(t, "externalUpdatedAt", "DATE_TIME"),
            "conferenceSolution": _field(t, "conferenceSolution", "TEXT"),
            "iCalSequence": _field(t, "iCalSequence", "NUMBER"),
            "visibility": _field(t, "visibility", "SELECT", default="SHARE_EVERYTHING",
                                 options=_options(t, ["METADATA", "SHARE_EVERYTHING"])),
            "participants": _field(t, "participants", "MORPH_RELATION", relation={"targets": ["person", "workspaceMember"], "multiple": True, "onDelete": "SET_NULL"}),
        }),
    }
    objects.update(messaging_objects(language))
    campaign_fields = objects["messageCampaign"]["fields"]
    campaign_fields["messages"] = _field(t, "messages", "RELATION", relation=o2m("message", "messageCampaign"))
    return {"format": DATAMODEL_FORMAT, "objects": objects}


def standard_settings(language: str, title: str, currency: str) -> dict[str, Any]:
    german = (language or "").lower().startswith("de")
    return {
        "format": "lmwiki-crm-settings/1",
        "workspace_name": title,
        "default_currency": currency,
        "date_format": "DAY_FIRST" if german else "MONTH_FIRST",
        "time_format": "HOUR_24" if german else "HOUR_12",
        "number_format": "DOTS_AND_COMMA" if german else "COMMAS_AND_DOT",
        "time_zone": "Europe/Berlin" if german else "UTC",
        "calendar_start_day": 1 if german else 7,
        "email": {
            "blocklist": [],
            "contact_creation": "work-domains",
            "exclude_group_emails": True,
            "excluded_handles": [],
            "free_email_domains": [
                "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
                "icloud.com", "me.com", "gmx.de", "gmx.net", "web.de", "t-online.de", "freenet.de", "posteo.de", "proton.me",
            ],
            "default_visibility": "SHARE_EVERYTHING",
        },
    }


def standard_views(language: str) -> dict[str, Any]:
    german = (language or "").lower().startswith("de")
    lbl = (lambda de, en: de if german else en)  # noqa: E731
    return {
        "format": "lmwiki-crm-views/1",
        "views": [
            {"id": "companies", "object": "company", "type": "table", "label": lbl("Alle Firmen", "All companies"),
             "fields": ["name", "domainName", "accountOwner", "address", "annualRevenue", "crm_created_at"],
             "sort": [{"field": "name", "direction": "asc"}], "visibility": "workspace", "position": 0},
            {"id": "people", "object": "person", "type": "table", "label": lbl("Alle Personen", "All people"),
             "fields": ["name", "emails", "company", "jobTitle", "phones", "crm_created_at"],
             "sort": [{"field": "name", "direction": "asc"}], "visibility": "workspace", "position": 1},
            {"id": "pipeline", "object": "opportunity", "type": "kanban", "label": lbl("Vertriebspipeline", "Sales pipeline"),
             "groupBy": "stage", "fields": ["amount", "company", "closeDate", "owner"],
             "aggregate": {"function": "SUM", "field": "amount"}, "visibility": "workspace", "position": 2},
            {"id": "opportunities", "object": "opportunity", "type": "table", "label": lbl("Alle Verkaufschancen", "All opportunities"),
             "fields": ["name", "amount", "stage", "closeDate", "company", "pointOfContact", "owner"],
             "sort": [{"field": "closeDate", "direction": "asc"}], "aggregates": {"amount": "sum"}, "visibility": "workspace", "position": 3},
            {"id": "tasks", "object": "task", "type": "table", "label": lbl("Alle Aufgaben", "All tasks"),
             "fields": ["title", "status", "dueAt", "assignee", "targets"], "groupBy": "status",
             "sort": [{"field": "dueAt", "direction": "asc"}], "visibility": "workspace", "position": 4},
            {"id": "tasks-calendar", "object": "task", "type": "calendar", "label": lbl("Aufgaben nach Fälligkeit", "Tasks by due date"),
             "dateField": "dueAt", "fields": ["status", "assignee"], "visibility": "workspace", "position": 5},
            {"id": "notes", "object": "note", "type": "table", "label": lbl("Alle Notizen", "All notes"),
             "fields": ["title", "targets", "crm_created_at"], "sort": [{"field": "crm_created_at", "direction": "desc"}],
             "visibility": "workspace", "position": 6},
        ],
    }


def standard_dashboards(language: str) -> dict[str, Any]:
    german = (language or "").lower().startswith("de")
    lbl = (lambda de, en: de if german else en)  # noqa: E731
    return {
        "format": "lmwiki-crm-dashboards/1",
        "dashboards": [
            {"id": "sales", "label": lbl("Vertrieb", "Sales"), "widgets": [
                {"id": "pipeline-value", "type": "number", "label": lbl("Offener Pipeline-Wert", "Open pipeline value"),
                 "object": "opportunity", "aggregate": {"function": "SUM", "field": "amount"},
                 "filter": {"op": "AND", "conditions": [{"field": "stage", "operand": "IS_NOT", "value": "CUSTOMER"}]}},
                {"id": "by-stage", "type": "bar", "label": lbl("Chancen je Phase", "Opportunities by stage"),
                 "object": "opportunity", "groupBy": "stage", "aggregate": {"function": "COUNT"}},
                {"id": "amount-by-month", "type": "line", "label": lbl("Betrag nach Abschlussmonat", "Amount by close month"),
                 "object": "opportunity", "groupBy": "closeDate", "dateGranularity": "month",
                 "aggregate": {"function": "SUM", "field": "amount"}},
                {"id": "open-tasks", "type": "number", "label": lbl("Offene Aufgaben", "Open tasks"),
                 "object": "task", "aggregate": {"function": "COUNT"},
                 "filter": {"op": "AND", "conditions": [{"field": "status", "operand": "IS_NOT", "value": "DONE"}]}},
            ]},
        ],
    }
