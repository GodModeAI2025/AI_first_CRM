#!/usr/bin/env python3
"""Campaign objects of the CRM layer, modelled on a CRM e-mailing module.

``messaging_objects(language)`` returns the objects messageList,
messageListMember, messageSuppression and messageCampaign in the format of
crm_standard.standard_datamodel, ready to be merged into the standard data
model. Field API names, types and options follow common CRM conventions (compute-message-list*, compute-message-list-member*, compute-message-suppression*,
compute-message-campaign* and compute-campaign-delivery*).

Deliberate differences, all marked ``"standard": false`` where they are fields:

- messageListMember gets ``name`` (label, otherwise members show only an id), ``status``
  (SUBSCRIBED, PENDING, UNSUBSCRIBED; default PENDING) and ``consentSource`` /
  ``consentAt`` so that a list member carries the evidence of consent. The usual
  unique index on (person, list) cannot be expressed by the core; crm_campaign
  enforces it when planning list members.
- messageSuppression.reason and .source are often TEXT enums and are SELECT
  fields here; source has the extra value IMPORT for result files. The usual
  unique index on (emailAddress, unsubscribeTopicId) is enforced by
  crm_campaign, as a sending service does.
- messageCampaign.bodyTemplate is RICH_TEXT (Markdown body block) instead of
  TEXT, and status has the extra option RENDERED: drafts were written as .eml
  files, nothing was sent. An export for another CRM maps RENDERED to DRAFT.
- One-to-many inverses whose many-to-one side lives on other standard objects
  (messageCampaign.messages and .recipients, person.listMemberships) are left
  out; message.messageCampaign does not exist in the standard model yet.
- campaignDelivery is not an object: rendering writes drafts, and delivery
  results come back as suppression records through import-results.
"""

from __future__ import annotations

from typing import Any

from crm_contract import validate_datamodel

LABELS = {
    "de": {
        "objects": {
            "messageList": ("Liste", "Listen"),
            "messageListMember": ("Listenmitglied", "Listenmitglieder"),
            "messageSuppression": ("Versandsperre", "Versandsperren"),
            "messageCampaign": ("Kampagne", "Kampagnen"),
        },
        "fields": {
            "name": "Name", "description": "Beschreibung", "members": "Mitglieder", "campaigns": "Kampagnen",
            "memberName": "Bezeichnung", "list": "Liste", "person": "Person", "status": "Status",
            "consentSource": "Einwilligung: Herkunft", "consentAt": "Einwilligung am",
            "emailAddress": "E-Mail-Adresse", "reason": "Grund", "source": "Herkunft",
            "providerEventId": "Ereignis-ID des Anbieters", "unsubscribeTopicId": "Abmeldethema",
            "subject": "Betreff", "bodyTemplate": "Inhalt", "fromAddress": "Absender",
            "scheduledAt": "Geplant für", "sentAt": "Gesendet am", "sentCount": "Gesendet",
            "deliveredCount": "Zugestellt", "failedCount": "Fehlgeschlagen", "bouncedCount": "Unzustellbar",
            "complainedCount": "Spam-Beschwerden", "skippedCount": "Übersprungen",
        },
        "options": {
            "SUBSCRIBED": "Angemeldet", "PENDING": "Einwilligung offen", "UNSUBSCRIBED": "Abgemeldet",
            "UNSUBSCRIBE": "Abgemeldet", "BOUNCE": "Unzustellbar", "COMPLAINT": "Spam-Beschwerde",
            "TRACKING": "Kein Tracking", "SYSTEM": "System", "WEBHOOK": "Versandanbieter", "IMPORT": "Import",
            "DRAFT": "Entwurf", "SCHEDULED": "Geplant", "SENDING": "Wird gesendet", "SENT": "Gesendet",
            "SENT_WITH_ERRORS": "Gesendet mit Fehlern", "CANCELED": "Abgebrochen", "RENDERED": "Entwürfe erzeugt",
        },
    },
    "en": {
        "objects": {
            "messageList": ("List", "Lists"),
            "messageListMember": ("List member", "List members"),
            "messageSuppression": ("Message suppression", "Message suppressions"),
            "messageCampaign": ("Campaign", "Campaigns"),
        },
        "fields": {
            "name": "Name", "description": "Description", "members": "Members", "campaigns": "Campaigns",
            "memberName": "Label", "list": "List", "person": "Person", "status": "Status",
            "consentSource": "Consent source", "consentAt": "Consent given at",
            "emailAddress": "Email address", "reason": "Reason", "source": "Source",
            "providerEventId": "Provider event ID", "unsubscribeTopicId": "Unsubscribe topic",
            "subject": "Subject", "bodyTemplate": "Body", "fromAddress": "From address",
            "scheduledAt": "Scheduled at", "sentAt": "Sent at", "sentCount": "Sent count",
            "deliveredCount": "Delivered count", "failedCount": "Failed count", "bouncedCount": "Bounced count",
            "complainedCount": "Complained count", "skippedCount": "Skipped count",
        },
        "options": {
            "SUBSCRIBED": "Subscribed", "PENDING": "Consent pending", "UNSUBSCRIBED": "Unsubscribed",
            "UNSUBSCRIBE": "Unsubscribed", "BOUNCE": "Bounce", "COMPLAINT": "Complaint",
            "TRACKING": "No tracking", "SYSTEM": "System", "WEBHOOK": "Sending provider", "IMPORT": "Import",
            "DRAFT": "Draft", "SCHEDULED": "Scheduled", "SENDING": "Sending", "SENT": "Sent",
            "SENT_WITH_ERRORS": "Sent with errors", "CANCELED": "Canceled", "RENDERED": "Drafts rendered",
        },
    },
}

COLORS = {
    "SUBSCRIBED": "green", "PENDING": "yellow", "UNSUBSCRIBED": "gray",
    "UNSUBSCRIBE": "gray", "BOUNCE": "red", "COMPLAINT": "orange", "TRACKING": "blue",
    "SYSTEM": "gray", "WEBHOOK": "purple", "IMPORT": "sky",
    "DRAFT": "gray", "SCHEDULED": "sky", "SENDING": "purple", "SENT": "green",
    "SENT_WITH_ERRORS": "orange", "CANCELED": "red", "RENDERED": "turquoise",
}

MEMBER_STATUSES = ("SUBSCRIBED", "PENDING", "UNSUBSCRIBED")
SUPPRESSION_REASONS = ("UNSUBSCRIBE", "BOUNCE", "COMPLAINT", "TRACKING")
HARD_SUPPRESSION_REASONS = ("BOUNCE", "COMPLAINT")
SUPPRESSION_SOURCES = ("SYSTEM", "WEBHOOK", "IMPORT")
CAMPAIGN_STATUSES = ("DRAFT", "SCHEDULED", "SENDING", "SENT", "SENT_WITH_ERRORS", "CANCELED", "RENDERED")
OBJECT_NAMES = ("messageList", "messageListMember", "messageSuppression", "messageCampaign")


def _labels(language: str) -> dict[str, Any]:
    base = (language or "en").split("-", 1)[0].lower()
    return LABELS.get(base, LABELS["en"])


def messaging_objects(language: str) -> dict[str, dict[str, Any]]:
    t = _labels(language)
    fields_t = t["fields"]

    def field(name: str, ftype: str, *, label_key: str = "", standard: bool = True, **extra: Any) -> dict[str, Any]:
        definition = {"type": ftype, "label": fields_t.get(label_key or name, name), "standard": standard}
        definition.update(extra)
        return definition

    def options(values: tuple[str, ...]) -> list[dict[str, Any]]:
        return [
            {"value": value, "label": t["options"].get(value, value), "color": COLORS.get(value, "gray"), "position": index}
            for index, value in enumerate(values)
        ]

    def obj(name: str, label_field: str, fields: dict[str, Any]) -> dict[str, Any]:
        singular, plural = t["objects"][name]
        return {"labelSingular": singular, "labelPlural": plural, "labelField": label_field,
                "standard": True, "active": True, "fields": fields}

    m2o = lambda target, inverse, on_delete: {"type": "MANY_TO_ONE", "target": target, "inverse": inverse, "onDelete": on_delete}  # noqa: E731
    o2m = lambda target, inverse: {"type": "ONE_TO_MANY", "target": target, "inverse": inverse}  # noqa: E731
    return {
        "messageList": obj("messageList", "name", {
            "name": field("name", "TEXT"),
            "description": field("description", "TEXT"),
            "members": field("members", "RELATION", relation=o2m("messageListMember", "list")),
            "campaigns": field("campaigns", "RELATION", relation=o2m("messageCampaign", "list")),
        }),
        "messageListMember": obj("messageListMember", "name", {
            "name": field("name", "TEXT", label_key="memberName", standard=False),
            "list": field("list", "RELATION", nullable=False, relation=m2o("messageList", "members", "CASCADE")),
            "person": field("person", "RELATION", nullable=False, relation=m2o("person", "listMemberships", "CASCADE")),
            "status": field("status", "SELECT", standard=False, nullable=False, default="PENDING", options=options(MEMBER_STATUSES)),
            "consentSource": field("consentSource", "TEXT", standard=False),
            "consentAt": field("consentAt", "DATE_TIME", standard=False),
        }),
        "messageSuppression": obj("messageSuppression", "emailAddress", {
            "emailAddress": field("emailAddress", "TEXT", nullable=False),
            "reason": field("reason", "SELECT", nullable=False, options=options(SUPPRESSION_REASONS)),
            "source": field("source", "SELECT", nullable=False, options=options(SUPPRESSION_SOURCES)),
            "providerEventId": field("providerEventId", "TEXT"),
            "unsubscribeTopicId": field("unsubscribeTopicId", "UUID"),
        }),
        "messageCampaign": obj("messageCampaign", "name", {
            "name": field("name", "TEXT", nullable=False),
            "subject": field("subject", "TEXT"),
            "bodyTemplate": field("bodyTemplate", "RICH_TEXT"),
            "fromAddress": field("fromAddress", "EMAILS"),
            "status": field("status", "SELECT", nullable=False, default="DRAFT", options=options(CAMPAIGN_STATUSES)),
            "scheduledAt": field("scheduledAt", "DATE_TIME"),
            "sentAt": field("sentAt", "DATE_TIME"),
            "sentCount": field("sentCount", "NUMBER", nullable=False, default=0),
            "deliveredCount": field("deliveredCount", "NUMBER", nullable=False, default=0),
            "failedCount": field("failedCount", "NUMBER", nullable=False, default=0),
            "bouncedCount": field("bouncedCount", "NUMBER", nullable=False, default=0),
            "complainedCount": field("complainedCount", "NUMBER", nullable=False, default=0),
            "skippedCount": field("skippedCount", "NUMBER", nullable=False, default=0),
            "unsubscribeTopicId": field("unsubscribeTopicId", "UUID"),
            "list": field("list", "RELATION", relation=m2o("messageList", "campaigns", "SET_NULL")),
        }),
    }


def merged_datamodel(datamodel: dict[str, Any], language: str) -> tuple[dict[str, Any], list[str]]:
    """Add the campaign objects that a data model lacks; existing definitions stay untouched."""
    result = {"format": datamodel.get("format"), "objects": dict(datamodel.get("objects", {}))}
    added = []
    for name, definition in messaging_objects(language).items():
        if name not in result["objects"]:
            result["objects"][name] = definition
            added.append(name)
    errors = validate_datamodel(result)
    if errors:
        raise ValueError("; ".join(errors[:10]))
    return result, added
