# CRM function coverage

This reference maps the product functions of a full-featured CRM (data model, data migration, views, dashboards, workflows, AI, e-mail and calendar, campaigns, permissions, settings) to what this skill does. Use it when the user asks whether something a CRM can do is possible here, and say plainly when it is not.

Every status cell starts with one of five words:

- **full**: works as in a server CRM, within the three file-based differences named under [Why some functions differ](#why-some-functions-differ) (nothing runs on its own, nothing leaves the machine, the interface is the conversation plus generated read-only HTML). Any further difference makes a row partial.
- **partial**: exists, with the difference named in the row.
- **documentary**: recorded but not enforced or executed.
- **not possible**: a file-based skill without network access and without a background process cannot provide it.
- **additional**: offered here beyond what a CRM usually offers.

## Contents

- [Why some functions differ](#why-some-functions-differ)
- [Data model](#data-model)
- [Data migration](#data-migration)
- [Layout, views, pipelines](#layout-views-pipelines)
- [Dashboards](#dashboards)
- [Workflows](#workflows)
- [AI](#ai)
- [Calendar and e-mail](#calendar-and-e-mail)
- [E-mail campaigns](#e-mail-campaigns)
- [Permissions and access](#permissions-and-access)
- [Settings, billing, legal](#settings-billing-legal)

## Why some functions differ

A server CRM runs a database, background workers, mailbox connections, and a web interface. This skill keeps the same data in files and runs only when the user asks. Three consequences follow everywhere:

1. Nothing happens on its own. Event triggers, schedules, delays, and incoming e-mail are processed when the user next invokes the skill; `crm_workflows.py run plan --due` reports and catches up what became due.
2. Nothing leaves the machine. E-mails, calendar invitations, HTTP requests, and campaign messages are written as drafts into the wiki's outbox (`records/_outbox/`) for the user to send or execute with their own tools; attached files are kept in the wiki's file store.
3. The interface is the conversation plus generated read-only HTML. Dragging a kanban card or editing a cell becomes a request to the agent, which applies it as a transaction.

## Data model

| CRM function | Skill | Status |
|---|---|---|
| Standard objects company, person, opportunity, task, note, workspace member, attachment, message, calendar event, campaign objects | `crm_standard.py`, created by `crm_init.py` | partial: note and task targets are one multi-target field, mapped to the common junction format on export. Message and calendar participants are stored on the message or event (`participants`, `fromHandle`, `toHandles`, `ccHandles`, `organizerHandle`) instead of separate participant, thread and channel objects, so recipes that search Message Participants use these fields |
| Custom objects | `crm_schema.py` add-object | partial: icons are stored but not rendered |
| Rename an object's labels, change its description or icon after creation | `crm_schema.py` update-object (labelSingular, labelPlural, description, icon; the API name and the label field stay) | full |
| All 25 field types | see the value table in `crm-contract.md` | partial: rich text keeps Markdown only, ACTOR/POSITION/TS_VECTOR are system keys; files up to 25 MB are stored in the wiki, larger ones as links |
| Attachments on records (upload, list, open, delete) | attachment records with a FILES value; the file is copied into `records/_files/`, the record browser links it, `cleanup-files` removes files no record uses | partial: no preview, images in rich text stay links |
| Relation fields one-to-many and many-to-one, inverse side created automatically | `crm_schema.py` add-field | full |
| Many-to-many through junction objects | custom junction object with two relations | partial: the record browser shows the junction records; there is no junction display setting |
| Select options with labels, colors, order; rename an option with data migration | `crm_schema.py` | full |
| Deactivate fields and objects without data loss; delete custom fields and custom objects | `crm_schema.py`; a deletion that removes data needs `--confirm-destructive` | full |
| Unique fields, including deleted records | data model `unique`, lint, import | partial: stricter than a raw comparison on purpose; e-mails compare without case, links without scheme or `www.`, phones by their digits |
| Required custom fields | `nullable: false` | additional; use sparingly, imports from other CRMs may leave such fields empty |
| Record merge (up to nine records) | `crm_records.py` merge | full; with confirmation |
| Trash, restore, permanent deletion | delete, restore, destroy | partial: no automatic purge after 14 days, because removal is never automatic |
| Timeline of changes per record | `meta/crm-events/`, record browser, `crm_query.py timeline` | partial: only changes made through the skill; edits made outside the skill have no events |
| Custom database indexes | none; records are files | not possible; file storage has no database indexes |

## Data migration

| CRM function | Skill | Status |
|---|---|---|
| CSV import with field mapping, uniqueness, relations, error report, update of existing records | `crm_import.py inspect`, `plan`, `apply` | partial: no per-value mapping of select values; a value must match an option's API name or label, and the import never creates options. Errors are shown as a row report and fixed in the file or by dropping rows, not in an inline editor |
| XLSX import | `crm_import.py` | partial: the differences of the CSV import apply |
| XLS (old binary Excel) | refused with a request for CSV or XLSX | not possible; reading XLS needs a parser outside Python's standard library |
| German spreadsheet conventions: decimal comma, `15.03.2024`, `WAHR` (CRM imports usually expect decimal points, `2024-03-15` and `TRUE`) | normalized with a report, as are a BOM and semicolons; ambiguous dates need a stated format | additional |
| Export of a view or object to CSV, 20,000-row limit | `crm_export.py csv`, split into several files | full |
| Import and export through the API | `crm_import.py` and `crm_export.py json` read and write files in the REST API shape | partial: there is no API; data moves as files |
| Migrating from other CRMs | CSV or XLSX export of the other system, mapped on import | full; as is usual in CRMs, views, workflows and permissions are recreated by hand |
| Migrating from self-hosted to cloud | not applicable | not possible |
| Sample file per object | `crm_export.py sample` | full |
| Undoing an import | `crm_records.py revert` restores the state before one transaction from its snapshot | additional |

## Layout, views, pipelines

| CRM function | Skill | Status |
|---|---|---|
| Table views with visible fields, order, filters (AND, OR, NOT groups and all operands), multi-level sorting, footer aggregates | `schema/crm/views.json`, `crm_build.py` | full; read-only HTML |
| Grouping a table view by a select field, group order, hidden groups | `groupBy` in a table view | partial: groups follow the option order and `hideEmptyGroups` hides empty ones; no alphabetical or manual group order, single groups cannot be hidden |
| Kanban views, sales pipeline, sums per column | kanban view grouped by a select field | partial: read-only, grouping by select fields only (server CRMs also group by some relations); moving a card is a request to the agent |
| Expected amount in the pipeline | `probabilities` in the view, `crm_query.py expected-amount` | full |
| Time in stage | derived from the event log, `crm_query.py time-in-stage` | partial: only changes made through the skill count |
| Calendar views | calendar view on a date field, month or week | partial: read-only, no day view, no end-date field |
| Record pages with fields, relations, notes, tasks, e-mails, timeline | record browser `graph/crm/objects/` | partial: no per-object page layout editor |
| Sidebar with folders, custom links, favorites, reordering, hidden objects | CRM start page `graph/crm/index.html` lists all objects, views and dashboards | partial: no folders, no custom links, no personal favorites; objects cannot be reordered or hidden |
| Rename, change the icon of, reorder, delete and favorite a view | `crm_config.py` on `schema/crm/views.json` (`label`, `icon`, `position`; removing a view needs `--confirm-removal`) | partial: icons are stored but not shown; no favorites |
| Unlisted views (visible only to their creator) | refused | not possible; everything released is visible to everyone who can read the folder |
| Views restricted to roles | `visibility: restricted` with a role list, shown as a label | additional; documentary only, the view stays visible to everyone who can read the folder |
| Relative date filters ("last 7 days") | evaluated when the views are built, with the moment shown | full; the page shows the result at build time |

## Dashboards

| CRM function | Skill | Status |
|---|---|---|
| Dashboards with a grid of widgets | `schema/crm/dashboards.json`, `crm_build.py` | full; read-only HTML |
| Dashboard tabs | none | partial: no tabs inside a dashboard; use one dashboard per tab |
| Number, bar (stacked), line (cumulative), pie charts with the twelve aggregations, date granularity, top N | inline SVG | partial: no ratio widget, no manual sort, no axis ranges, no short number format. Colors come from a fixed palette of eight series, not from the option colors; palette, axis names, data labels and the legend cannot be set; the pie center value shows only on donut charts. MIN and MAX of dates only through `crm_query.py` |
| Table, rich-text widgets | from a view; Markdown | full |
| iFrame widget, editing in the dashboard, live refresh | link placeholder; rebuild on the next session | not possible |

## Workflows

| CRM function | Skill | Status |
|---|---|---|
| Versions (draft, active, deactivated, archived) | `schema/crm/workflows/` | full |
| Runs with status, trigger data, step outputs and errors; run list filtered by workflow, status and date | `meta/crm-runs/`, `crm_query.py runs` (`--workflow`, `--status`, `--since`; `--run-id` shows one run with its step outputs) | full |
| Manual trigger (global, one record, several records) | `crm_workflows.py run plan` | full |
| Record created, updated, deleted, upserted | processed from the event log on the next `--due` run | partial: not instantaneous; changes made outside the skill do not trigger |
| Schedule (cron) | checked on every `--due` run | partial: all times missed since the last check collapse into one run per `--due`, which reports the number of missed slots |
| Webhook | the received body is given as a file with `--payload-file` | partial: no webhook URL; an API key cannot be checked |
| Create, update, upsert, find records; filter; if/else branches; iterator; empty step; pick record; form | engine in `crm_workflows.py` | full; forms are asked in the conversation |
| Delete record, delay, wait for event, send chat message | engine in `crm_workflows.py` | partial: deleting moves records to the trash, permanent deletion needs `--confirm-destructive`; delay and wait for event resume on the next `--due` after their time; the host agent answers chat messages |
| Send e-mail, draft e-mail, create calendar event | `.eml` and `.ics` drafts in the wiki outbox `records/_outbox/workflows/<workflow>/` | partial: never sent; static attachments are not written into the `.eml` draft |
| HTTP request | request specification file in the outbox | partial: never executed, so later steps get no response |
| Code action | safe formula language (`crm_formula.py`) for formula fields and calculations | partial: no arbitrary JavaScript |
| AI agent, classify | the host agent answers during the run | partial: only while the user is in the conversation |
| Logic functions from apps | not supported | not possible; they need a server runtime |
| Credits and rate limits | not applicable | not possible |

## AI

| CRM function | Skill | Status |
|---|---|---|
| Chatbot with access to all records | the host agent with `crm_query.py` on the current maintenance state or a verified release | full |
| AI agents with roles | the host agent acts as `agent/<name>`, which `schema/crm/roles.json` can assign a role | partial: the same cooperative checks as for members, on record writes only; the files are not protected |
| AI campaign writing | the host agent drafts, `crm_campaign.py render` writes drafts | partial: the body is plain text, see the campaign editor row |

## Calendar and e-mail

| CRM function | Skill | Status |
|---|---|---|
| Mailbox and calendar sync (Google, Microsoft, IMAP, CalDAV) every few minutes | `.eml`, `.mbox`, `.ics` files the user exports, imported with `crm_ingest.py` | partial: no mailbox connection; on request, from files |
| Several mailboxes per user, one message stored once | deduplication by Message-ID | partial: a message does not record which mailbox it came from; e-mail settings are workspace-wide, not per mailbox |
| Shared inbox forwarding | export of the shared mailbox as `.mbox` | partial: no forwarding address; the export is imported on request |
| Visibility levels (metadata, subject, everything) | applied when importing | partial: hidden parts are not stored, so the mailbox owner cannot see them either, and a later import never widens the visibility |
| Blocklist, excluding group and bulk e-mails, internal e-mails | `schema/crm/settings.json` email rules | partial: the blocklist is workspace-wide and a new entry does not remove earlier imports |
| Automatic contact and company creation from participants | proposals that the user confirms | partial: never silent |
| E-mail activity on people, companies, opportunities | participants link to people; the record browser lists a company's mails and meetings through its people and an opportunity's through its company, as the Emails and Calendar tabs do | partial: no thread grouping; `messageThreadId` is stored but not used for display |
| Sending e-mail, booking meetings | `.eml` and `.ics` drafts in the outbox | partial: drafts only, never sent; no video-call link is created; a sent message reaches the CRM only through a later import |
| Recurring events, time zones, cancellations, updates | RFC 5545 parser with time-zone mapping, sequence-based updates | partial: unusual recurrence rules (for example BYSETPOS or hourly) are kept as text and not expanded |
| Outlook `.msg` files | refused with a request for `.eml` | not possible; reading `.msg` needs a parser outside Python's standard library |

## E-mail campaigns

| CRM function | Skill | Status |
|---|---|---|
| Lists, list members, suppression after bounce, complaint or unsubscribe | campaign objects, `crm_campaign.py` | partial: a member receives drafts only with status SUBSCRIBED, which needs recorded consent; there is no unsubscribe page, unsubscribes come back through `import-results` |
| Unsubscribe topics | topic ids on campaigns and suppressions | partial: topic ids only; no topic definitions (name, description, visibility, unsubscribe page) |
| Audience from filters or views | `crm_campaign.py audience` | partial: people created automatically from e-mail or calendar files are left out unless `--include-auto-created` |
| Campaign editor and test send | Markdown body rendered by `crm_campaign.py render` as one `text/plain` `.eml` per recipient | partial: no images, buttons or HTML blocks; no test send |
| Sending and scheduling | personalized `.eml` drafts for the user's own mail or newsletter tool | partial: never sent by the skill; scheduling is left to the sending tool |
| Delivery statistics | `crm_campaign.py import-results` turns bounces, complaints and unsubscribes reported by the sending tool into suppressions | partial: no sent or delivered counts |
| Sending domain verification (SPF, DKIM, DMARC) | not applicable | not possible |

## Permissions and access

| CRM function | Skill | Status |
|---|---|---|
| Roles with object, field, settings and action permissions | `schema/crm/roles.json`, checked on record writes through the shared planner in `crm_contract.py` (`crm_records.py`, `crm_import.py`, `crm_workflows.py`, `crm_ingest.py`, `crm_campaign.py`) | partial: cooperative, the files are not protected. The update, delete and destroy flags and field edit rules are checked; the read flag exists but is not checked. `crm_schema.py`, `crm_config.py`, `crm_export.py`, `crm_query.py` and `crm_build.py` do not check roles. There are no settings or action permissions (send e-mail, import, export), no field visibility and no read restrictions |
| Audit log (what each member did) | `crm_query.py timeline` over `meta/crm-events/`, each change with its actor | partial: only record changes made through the skill; no filter by member |
| Row-level permissions, API keys, SSO, two-factor authentication | access control of the storage (separate wikis, SharePoint permissions) | not possible; access control belongs to the storage |

## Settings, billing, legal

| CRM function | Skill | Status |
|---|---|---|
| Workspace name | `workspace_name` in `schema/crm/settings.json`, set from the wiki title | full |
| Members and profiles | `workspaceMember` records; maintainer slots of the lock | partial: no invitations, no login |
| Experience: language, theme, date, time and number format, time zone, start of week | `schema/crm/settings.json`; the language is the wiki language in `schema/WIKI_PROFILE.md` | partial: workspace-wide, no per-member preferences, no theme; labels exist in German and English only |
| Domains, custom domain, community, early access | not applicable | not possible |
| Plans, credits, usage limits, invoices | not applicable | not possible |
| Legal pages | not applicable | not possible |
