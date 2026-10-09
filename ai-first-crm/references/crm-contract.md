# CRM layer contract

Read this contract before any CRM work. It defines the files, the data model, the record format, the event log, and the transaction rules that every CRM helper enforces. Task-specific procedures live in `crm-data.md`, `crm-views.md`, `crm-workflows.md`, and `crm-communication.md`; `crm-coverage.md` lists the functions of a full-featured CRM and how far this skill covers them.

## Contents

- [Purpose and boundaries](#purpose-and-boundaries)
- [Layout](#layout)
- [Data model](#data-model)
- [Record files](#record-files)
- [Values by field type](#values-by-field-type)
- [Uniqueness, required fields, defaults](#uniqueness-required-fields-defaults)
- [Event log](#event-log)
- [Transactions](#transactions)
- [Trash, destruction, merge, erasure](#trash-destruction-merge-erasure)
- [Stored files and outbox](#stored-files-and-outbox)
- [Credentials in CRM data](#credentials-in-crm-data)
- [Roles](#roles)
- [Lint, views, and release](#lint-views-and-release)
- [Language](#language)
- [Limits](#limits)

## Purpose and boundaries

The CRM layer reproduces the product functions of a full-featured CRM with files: a data model of standard and custom objects, records and their relations, imports and exports, views, dashboards, workflows, e-mail and calendar data, campaign drafts, and roles. It is optional; a wiki without `schema/crm/datamodel.json` and without `records/` has no CRM layer and nothing in this contract applies to it.

Records are data, not knowledge. They carry no claim blocks, no clusters or concepts, and are never translated. Their provenance is the event log: every change names its actor, its moment, and its origin. The claim-evidence contract of `wiki/` pages is unchanged; a knowledge page may link to a record with an ordinary wikilink such as `[[records/company/<uuid>|Acme GmbH]]`, but a record never substitutes for a registered source of a claim.

A typical CRM is a server application; this skill is not. Everything that needs a running server, a mailbox connection, or outgoing network traffic is replaced by files the user provides or receives, or by work that happens when the user next invokes the skill. `crm-coverage.md` names every such boundary.

## Layout

```text
schema/crm/datamodel.json                  lmwiki-crm-datamodel/1
schema/crm/settings.json                   lmwiki-crm-settings/1
schema/crm/roles.json                      lmwiki-crm-roles/1 (optional)
schema/crm/views.json                      lmwiki-crm-views/1
schema/crm/dashboards.json                 lmwiki-crm-dashboards/1
schema/crm/workflows/<id>.json             lmwiki-crm-workflow/1
schema/crm/credential-acknowledgements.json lmwiki-crm-credential-acks/1 (optional)
records/<object-directory>/<uuid>.md       one file per record
records/_files/<ab>/<sha256>[.suffix]      file store for FILES values (attachments)
records/_outbox/<folder>/<name>            drafts: .eml, .ics, request specifications, campaign manifests
meta/crm-events/YYYY-MM.jsonl              event log, monthly shards
meta/crm-runs/YYYY-MM.jsonl                workflow runs, monthly shards
meta/crm-workflow-state.json               cursors, pending runs, round-robin state
graph/crm/                                 generated record browser, views, dashboards
```

The object directory is the kebab-case form of the object's API name (`workspaceMember` becomes `records/workspace-member/`). `records/` is flat per object: no nested directories, no other file types, no symbolic links. The two directories starting with an underscore are not objects: `records/_files/` and `records/_outbox/` hold the artifacts described below. The file name is always the record's lowercase UUID, which keeps every record path storage-safe whatever the company or person is called: no reserved names, no case collisions, no characters a synchronization client refuses, and no change of path when a record is renamed.

All of these paths are released and hashed into `meta/manifest.json`, except the transient lock. `graph/crm/` is generated and must never be edited by hand. Snapshots of records are taken per transaction, not wholesale: a transaction copies the record files it changes, while the append-only event and run logs are not copied (apply reports their length before the append, so a failed write can be cut back).

## Data model

`schema/crm/datamodel.json` holds every object and field. Its shape:

```json
{
  "format": "lmwiki-crm-datamodel/1",
  "objects": {
    "company": {
      "labelSingular": "Firma", "labelPlural": "Firmen", "labelField": "name",
      "standard": true, "active": true,
      "fields": {
        "name": {"type": "TEXT", "label": "Name", "standard": true},
        "domainName": {"type": "LINKS", "label": "Domain", "unique": true},
        "accountOwner": {"type": "RELATION", "label": "Kundenbetreuer",
          "relation": {"type": "MANY_TO_ONE", "target": "workspaceMember", "inverse": "accountOwnerForCompanies", "onDelete": "SET_NULL"}}
      }
    }
  }
}
```

Object and field API names are camelCase ASCII, as is usual in CRMs, and must not start with `crm`. Each object names a `labelField` that provides the record title. A field has `type` (one of the 25 field types in the table below), `label`, and optionally `description`, `nullable` (default true), `unique`, `default`, `active` (default true), `standard`, `options` (SELECT and MULTI_SELECT: `value` in UPPER_SNAKE_CASE, `label`, `color`, `position`), and `relation`.

Relations: a `RELATION` field is either `MANY_TO_ONE` (stored on this record, pointing at one record of `target`, with `onDelete` SET_NULL, CASCADE, or RESTRICT) or `ONE_TO_MANY` (the computed inverse; it names the `MANY_TO_ONE` field on the target in `inverse` and stores nothing). A `MORPH_RELATION` field points at one or, with `multiple: true`, several records of the objects listed in `relation.targets`. Many-to-many relations follow the junction pattern: a custom object with two `MANY_TO_ONE` fields.

The standard model has the usual objects and fields of a CRM (company, person, opportunity, task, note, workspaceMember, attachment, message, calendarEvent, and the campaign objects). Two deliberate simplifications keep everyday requests short and are mapped back to the common junction shape on export: notes and tasks link to people, companies, and opportunities through one multi-target field `targets` instead of the `noteTarget` and `taskTarget` junction objects, and e-mails and calendar events list their `participants` directly. A data model change goes through `crm_schema.py` (see `crm-data.md`); never edit the file by hand.

## Record files

```markdown
---
crm_id: "0d5b33f5-b06d-4454-a820-ab8a4390860e"
crm_object: "task"
crm_title: "Angebot nachfassen"
crm_created_at: "2026-10-06T14:59:58Z"
crm_updated_at: "2026-10-06T14:59:58Z"
crm_created_by: "agent/claude"
crm_updated_by: "agent/claude"
crm_created_source: "MANUAL"
title: "Angebot nachfassen"
dueAt: "2026-10-10T07:00:00Z"
status: "TODO"
assignee: "[[records/workspace-member/57f057d5-431e-490a-9aee-984a166d9b62|Anna Admin]]"
targets:
  - "[[records/company/61e37838-3154-4acc-b2c9-62d6ea20ab04|Acme Holding GmbH]]"
---
# Angebot nachfassen

<!-- crm:richtext bodyV2 -->
Bitte **Freitag** anrufen.
<!-- /crm:richtext -->
```

System keys start with `crm_`: `crm_id` (equals the file name), `crm_object`, `crm_title` (cached display title from the label field), `crm_created_at`, `crm_updated_at` (UTC instants), `crm_created_by`, `crm_updated_by` (actors: `human:<id>`, `agent/<name>`, `process:<id>`), `crm_created_source` (MANUAL, IMPORT, API, EMAIL, CALENDAR, WORKFLOW, AGENT, MERGE), `crm_deleted_at` (only while in the trash), and `crm_position` (manual order). All other keys are field API names, composite fields as `field.subfield`. The frontmatter uses the wiki's flat subset; values are always written by the helpers in canonical order, so equal content gives identical bytes. The body starts with the title as a heading and holds rich-text fields as marked blocks.

Relation values are record wikilinks with the target's title as label. A rename refreshes the labels of every link to the record in the same transaction; the link identity, the UUID, never changes.

## Values by field type

| Type | Stored as |
|---|---|
| TEXT, UUID | text |
| NUMBER | number; NUMERIC as text with a dot |
| BOOLEAN | `true` or `false` |
| DATE | `YYYY-MM-DD`; DATE_TIME as a UTC instant ending in `Z` |
| SELECT, MULTI_SELECT | option API name; flat list of option API names |
| RATING | `RATING_1` to `RATING_5` |
| CURRENCY | `field.amountMicros` (integer, amount times one million), `field.currencyCode` (ISO 4217) |
| FULL_NAME | `field.firstName`, `field.lastName` |
| ADDRESS | `field.addressStreet1`, `addressStreet2`, `addressCity`, `addressPostcode`, `addressState`, `addressCountry` (text), `addressLat`, `addressLng` (numbers) |
| EMAILS | `field.primaryEmail`, `field.additionalEmails` (list) |
| LINKS | `field.primaryLinkUrl`, `field.primaryLinkLabel`, `field.secondaryLinks` (JSON text) |
| PHONES | `field.primaryPhoneNumber`, `primaryPhoneCountryCode`, `primaryPhoneCallingCode`, `field.additionalPhones` (JSON text) |
| RAW_JSON | JSON text |
| FILES | JSON text of `{name, ref}` entries. `ref` is a URL or another portable reference, or a stored file `records/_files/<ab>/<sha256>[.suffix]` with `sha256`, `size`, and `type`. To store a file, the request gives `{"name": "Angebot.pdf", "upload": "<runtime path>"}`; never a local path in the record |
| ARRAY | list of texts |
| RICH_TEXT | body block `<!-- crm:richtext <field> -->` with Markdown; block-editor JSON is not kept |
| RELATION | `MANY_TO_ONE`: one record wikilink; `ONE_TO_MANY`: nothing, computed |
| MORPH_RELATION | one record wikilink or a list of them |
| ACTOR, POSITION, TS_VECTOR | not user fields: `crm_created_by`, `crm_position`, nothing |

Requests always use canonical, language-neutral values: ISO dates and instants (an instant without offset is read as UTC), a dot as decimal separator, `true`/`false`, option API names or their exact labels. Amounts are given as `{"amount": "12500.50", "currencyCode": "EUR"}` and converted with decimal arithmetic; money never passes through floating point. Spreadsheet conventions such as decimal commas, `15.03.2024`, or `WAHR` are normalized by the importer before a request is built.

## Uniqueness, required fields, defaults

A unique field may hold each value only once across all records of its object, records in the trash included, as is usual in CRMs. EMAILS compare the primary e-mail case-insensitively; LINKS compare the primary URL without scheme, user info, port, `www.`, case, fragment, trailing dot and trailing slash, with an internationalized host in its `xn--` form; a domain field (`domainName`) compares the host alone, so `https://www.acme.de/kontakt` and `acme.de` are the same company; PHONES compare the digits of calling code and number. This is stricter than a raw comparison on purpose: it prevents the duplicates that case variants of the same address would create.

A field with `nullable: false` must have a value on every record that is not in the trash. Defaults (for example opportunity stage `NEW`, task status `TODO`, the default currency code) are applied when a record is created and a value is not given.

## Event log

Every applied change appends one event per touched record to the monthly shard of `meta/crm-events/`:

```json
{"format": "lmwiki-crm-event/1", "event_id": "evt-…", "at": "2026-10-06T14:59:58Z", "actor": "agent/claude",
 "op": "update", "object": "opportunity", "record_id": "<uuid>", "txn": "txn-…",
 "changes": {"stage": ["PROPOSAL", "CUSTOMER"]},
 "origin": {"kind": "import", "ref": "kunden-2026-10.csv", "sha256": "…", "row": 17}}
```

Operations are `create`, `update`, `upsert`, `delete` (to the trash), `restore`, `destroy`, `merge`, `erase`, and `cascade-null` (a relation cleared because its target was destroyed). Origin kinds are `manual`, `import`, `email`, `calendar`, `workflow`, `merge`, `restore`, `agent`, `api`, and `migration`; `ref` is a portable reference such as a file name, `message-id:<id>`, or `ical-uid:<uid>`, never a local path. Rich-text changes record digests and lengths, not the text.

The log is the record timeline, the source of time-in-stage figures, the audit trail, and the input of event-triggered workflows. It is only ever appended by a transaction; an erasure is the single operation that rewrites it.

## Transactions

Every write is a transaction with two steps, under the lock:

1. `plan`: the helper builds a request (or receives one), validates every operation against the data model and the current records, and writes an immutable plan outside the wiki. The plan holds the full new content of every touched record, the events to append, a summary per operation, the destructive flag, all errors (with operation index, row, object, record, and message), warnings, and `plan_sha256`. Nothing in the wiki changes.
2. `apply`: only the identical plan, given by its hash, is applied (a changed or foreign plan is `stale_plan`, exit 3). Apply refuses with zero writes when the plan has errors, when a destructive plan lacks `--confirm-destructive`, when the data model changed, or when any record or event shard differs from the plan's precondition hash. It snapshots every touched existing file into `meta/history/`, writes atomically, and appends the events.

The request format of `crm_records.py`:

```json
{
  "actor": "human:anna@example.com",
  "origin": {"kind": "manual", "occasion": "Telefonat vom 06.10.2026"},
  "operations": [
    {"op": "create", "object": "company", "ref": "acme", "values": {"name": "Acme GmbH", "domainName": "acme.example", "address": {"addressCity": "Berlin"}}},
    {"op": "create", "object": "person", "values": {"name": {"firstName": "Ada", "lastName": "Muster"}, "emails": "ada@acme.example", "company": {"ref": "acme"}}},
    {"op": "update", "object": "opportunity", "record": {"match": {"name": "Relaunch"}}, "values": {"stage": "PROPOSAL"}},
    {"op": "upsert", "object": "person", "match": {"emails.primaryEmail": "x@y.example"}, "values": {"jobTitle": "Einkauf"}},
    {"op": "create", "object": "note", "values": {"title": "Erstgespräch", "bodyV2": "…", "targets": [{"object": "company", "match": {"domainName": "acme.example"}}]}},
    {"op": "delete", "object": "task", "record": "<uuid>"}
  ]
}
```

A record is selected by UUID, by record link, by `{"ref": …}` for a record created earlier in the same transaction, or by `{"match": {field: value}}`, which must hit exactly one record (a composite field without subfield matches on its identifying part: the primary e-mail, the primary link, the full name). Values follow the table above; composite fields may be given as a nested mapping or as `field.subfield` keys; relation values take the same selectors, with `"object"` added for a morph relation, and multi-target fields also accept `{"add": […], "remove": […]}`. `upsert` matches deleted records too and restores them, as a CRM import usually does.

An operation that fails is reported and rolled back without affecting the others in the plan; the whole plan is still not applicable until the failing operations are fixed or, with the user's consent, left out. Exit codes of the CRM helpers: 0 success, 1 plan with errors, 2 error, 3 confirmation required or stale plan, 4 invalid plan, 5 partial failure after validation (keep the lock, restore the snapshot or repair, then release).

## Trash, destruction, merge, erasure

- `delete` moves a record to the trash: `crm_deleted_at` is set, the record keeps its file, its relations, and its unique values, and views hide it. `restore` takes it back. There is no automatic purge after a deadline; the content policy forbids automatic removal.
- `destroy` removes the record file. Every relation pointing at it is handled by its `onDelete`: SET_NULL clears it (`cascade-null` events), CASCADE destroys the dependent record, RESTRICT stops the plan. The removed file remains in the transaction's snapshot.
- `merge` keeps one record (`into`), fills its empty fields from the others or takes the field values the user chose in `prefer`, joins multi-target links and rich text, re-points every relation from the merged records to the survivor, and destroys the merged records. At most nine records merge at once, as is usual in CRMs.
- `erase` is the GDPR erasure. It destroys the record like `destroy` and additionally:
  - replaces the content of every event of that record with `[erased]` and the record's title in links held by other events with `[erased]`;
  - replaces the person's e-mail addresses and phone numbers with `[erased]` wherever they occur: in other records' fields and texts (a calendar organizer, a task text), in every event, in workflow runs and waiting runs (`meta/crm-runs/`, `meta/crm-workflow-state.json`), and in every snapshot under `meta/history/`;
  - removes the record's own copies, its stored files and the outbox drafts that mention the person from the history, and deletes the transaction's own safety snapshot once applied;
  - with `"redact_mentions": true` on the erase operation, also replaces the person's name in other records and removes stored text files that mention the person, inside the same transaction so its events and snapshots are cleaned too. Without it, the plan lists the records that still mention the name; never clean them afterwards with an ordinary update, which would write new copies into the event log and the history. Plan the erase again with `redact_mentions` instead.

  The plan shows the records it redacts, the files it removes and the history files it cleans; the apply result lists under `history_still_mentions` every history file that still holds the name (only possible without `redact_mentions`). Tell the user that copies already outside the wiki (earlier frozen exports, OKF bundles, synchronized devices, backups, copies made with `--outbox`, mails already sent) are not reached and must be handled separately, and that the skill does not stop the same address from being created again later by an import or an e-mail.

destroy, merge, erase, and every removal of stored files or drafts are destructive: show the plan, get explicit confirmation, apply with `--confirm-destructive`.

## Stored files and outbox

The wiki keeps the artifacts the CRM works with, so they are versioned with the records, released, checked by lint, and reached by an erasure.

- **File store.** An upload in a FILES value is planned with the file's SHA-256 and size; apply copies it to `records/_files/<first two hex digits>/<sha256><suffix>` after checking that the source is unchanged (otherwise the plan is stale and nothing is written). Identical content is stored once. Files above 25 MB stay outside; store a link to them instead. Text files are screened for credentials like records; binary files are stored without that screen, and the plan says so. Suffixes that wiki tools or a browser would interpret (`.md`, `.json`, `.html`, `.svg`, scripts) get `.txt` appended, so a stored file never becomes a page or runs. The record browser links each stored file.
- **Outbox.** Workflow steps (send or draft e-mail, calendar event, HTTP request) and campaign renders write their drafts to `records/_outbox/workflows/<workflow>/` and `records/_outbox/campaigns/<campaign>/<time>/`. A request may also write a draft itself with `"artifacts": [{"path": "records/_outbox/<folder>/<name>.eml", "content": "..."}]`. Drafts are never overwritten with other content and never sent; the CRM start page lists them. `--outbox <folder>` of the workflow and campaign helpers only adds a copy outside the wiki, which an erasure cannot reach.
- **Cleanup.** Destroying a record keeps its stored files, so a revert can restore it; lint then warns about files no record refers to. `crm_records.py cleanup-files --orphans` (or `--path` for a file, a draft, or a draft folder after sending) writes a request whose plan removes them; it is destructive and keeps the removed files in the snapshot.
- **Erasure.** An erase removes the stored files that only the erased records (and records removed with them) referred to, every outbox draft that mentions the erased person, and their copies in `meta/history/`, including copies of attachments that were cleaned up earlier. A stored text file still attached to other records that mentions the person is reported for a human decision.

## Credentials in CRM data

Imports and e-mail ingestion redact credential-shaped values as `[credential removed]` before planning and tell the user; every record a plan writes is screened again, so a key, token or "Passwort lautet …" typed into a field is a plan error and never reaches the append-only event log, where a later correction could not remove it. Lint screens `records/`, `meta/crm-events/`, `meta/crm-runs/`, stored text files and drafts like every other file. Because CRM data contains names and codes that merely look like keys (a company called `ASIAPACIFICLOGISTICS GmbH` matches the AWS key pattern), a named person may acknowledge one exact value as harmless: `crm_records.py acknowledge-credential` stores the kind and the SHA-256 of that value in `schema/crm/credential-acknowledgements.json`, never the value. Before the value is in the wiki, take kind and digest from the plan error (`--kind <kind> --value-sha256 <digest>`); for a lint finding give `--path` and `--line`. It applies only to CRM data and only after the person confirmed it; pass `--user-confirmed-harmless` only then, and plan again.

## Roles

`schema/crm/roles.json` may define roles with object and field permissions and assign actors to roles:

```json
{"format": "lmwiki-crm-roles/1", "enforcement": "cooperative", "default_role": "member",
 "roles": {"admin": {"objects": {"*": {"read": true, "update": true, "delete": true, "destroy": true}}},
           "member": {"objects": {"*": {"read": true, "update": true, "delete": true, "destroy": false},
                                  "company": {"read": true, "update": true, "delete": false, "destroy": false,
                                              "fields": {"annualRevenue": {"update": false}}}}}},
 "assignments": {"human:anna@example.com": "admin", "agent/*": "member"}}
```

With `"enforcement": "cooperative"` the CRM helpers refuse operations the acting actor's role does not allow. The files themselves protect nothing: anyone with access to the folder can read and change them. Real access control belongs to the storage (separate wikis, SharePoint permissions). Single sign-on, two-factor authentication, and invitation flows are not part of a file-based skill.

## Lint, views, and release

Lint validates the CRM layer whenever it exists: the data model; every record against it (keys, types, options, relation targets and their object types, required fields, case-insensitive path uniqueness, file names); unique constraints across all records; the event log (format, actors, origins, unique event IDs, shard names); settings, roles, views, dashboards, and workflows; credential findings; and the freshness of `graph/crm/` against its inputs. Records are not graph nodes and have no reading view under `graph/pages/`; links from wiki pages to records resolve to the record browser.

A session that changed CRM data ends with the sequence in `SKILL.md`: `crm_build.py`, `build_graph.py`, `lint_wiki.py --fix-safe`, one release with the highest bump the session needs (patch for record data; minor for the data model, views, dashboards, settings, roles, or workflow definitions; major only for an intentionally breaking change), `verify_release.py` through `run_locked.py`, and the lock release.

## Language

Labels of objects, fields, options, views, and dashboards follow the wiki language and are translated in a wiki-language migration. Field values, record titles, option API names, and e-mail or note texts are data in whatever language they were written and are never translated.

## Limits

- One maintainer writes at a time; everybody else reads the last release. Teammates propose changes, the maintainer applies them in one session.
- No network access and no background process: mailboxes, calendars, webhooks, schedules, and outgoing e-mail work through files and on the next invocation, as described in the task references.
- Sizes measured with the bundled helpers: rebuilding views and graph and releasing take seconds for a few thousand records and up to about a minute for tens of thousands. A frozen knowledge-skill export above 30 MB cannot be uploaded to Claude's skill upload; exclude the CRM layer from such an export or use a host that installs skill folders directly.
