# CRM data: records, imports, exports, data model changes

Read `crm-contract.md` first. This reference describes how the agent turns everyday CRM requests into transactions. Every helper below runs through `run_locked.py` under the maintenance lock; plans are written outside the wiki.

## Contents

- [From a request to a transaction](#from-a-request-to-a-transaction)
- [Everyday record changes](#everyday-record-changes)
- [Undoing a transaction](#undoing-a-transaction)
- [Stored files and sent drafts](#stored-files-and-sent-drafts)
- [Duplicates and merging](#duplicates-and-merging)
- [Trash, destruction, GDPR erasure](#trash-destruction-gdpr-erasure)
- [Importing CSV, XLSX, JSON](#importing-csv-xlsx-json)
- [Exporting](#exporting)
- [Changing the data model](#changing-the-data-model)
- [Credential findings](#credential-findings)

## From a request to a transaction

1. Acquire the lock and run the baseline lint (`lint_wiki.py --check-only`); report pre-existing problems separately.
2. Identify the records involved. When the user names a record by its title, find it with `crm_query.py find` or `search`; when several records match, show the candidates and ask, never guess.
3. Write the request JSON (format in `crm-contract.md`) into a temporary directory outside the wiki and run `crm_records.py plan --target <wiki> --request-file <request> --output <plan>`.
4. Show the user the summary: what is created, changed, moved to the trash, merged or destroyed, files to store, every error with its record and field, warnings, and the destructive flag. Errors make the plan inapplicable.
5. Apply with `crm_records.py apply --target <wiki> --plan-file <plan> --expect-plan-sha256 <hash>` once confirmed (see the confirmation policy in `SKILL.md`), adding `--confirm-destructive` for every plan the helper reports as destructive, after the user confirmed it.
6. Collect further changes of the session the same way. At the end: `crm_build.py`, `build_graph.py`, `lint_wiki.py --fix-safe`, one `release_wiki.py`, `verify_release.py` (through `run_locked.py`, so the owner may verify before releasing), lock release. Then delete the session's temporary requests and plans: they may hold runtime paths of attached files and personal data.

Every apply reports its transaction id (`txn-…`); quote it when the user may want to undo the change.

Use the user's own words for values but store canonical values: option API names (`PROPOSAL`, not "Angebot"; the planner also accepts the exact option label), ISO dates, decimal points. Convert relative dates ("next Friday") to an explicit date and say which date you used. When a value is missing, leave the field empty rather than inventing one.

## Everyday record changes

| Request | Operation |
|---|---|
| "Leg die Firma Acme GmbH mit acme.de an" | `create` company with `name`, `domainName` |
| "Neuer Kontakt Ada Muster bei Acme, ada@acme.de" | `create` person with `name.firstName`, `name.lastName`, `emails`, `company` by `{"match": {"domainName": "acme.de"}}` |
| "Der Deal Relaunch ist jetzt im Angebot, 48.000 Euro" | `update` opportunity selected by match on name: `stage`, `amount` with `{"amount": "48000", "currencyCode": "EUR"}` |
| "Notiz zum Gespräch mit Ada" | `create` note with `title`, `bodyV2`, `targets` |
| "Aufgabe für Ben: Angebot bis Freitag nachfassen" | `create` task with `title`, `dueAt`, `assignee` matched on the member's `userEmail` or name, `targets` |
| "Ben übernimmt die Stadtwerke" | `update` company `accountOwner` |
| "Verschiebe die Karte Relaunch nach Kunde" (kanban) | `update` opportunity `stage` |
| "Hänge das Angebot an" (a file the user gives) | `create` attachment with `name`, `file` `[{"name": "Angebot.pdf", "upload": "<path of the file>"}]` and `target` `{"object": "company", "match": {...}}`; the file is copied into the wiki on apply; without `name` the attachment takes the file name |
| "Hänge den Link zum Angebot an" | `create` attachment with `file` `[{"name": "...", "ref": "<URL or portable reference>"}]` and `target` |

The planner refreshes the cached titles and link labels after a rename, applies defaults on creation, and rejects values that do not fit the field type, unknown options, links to missing or wrongly typed records, and duplicates of unique values.

## Undoing a transaction

`crm_records.py revert --target <wiki> --transaction <txn-…> --actor <actor> --output-request <file>` writes a request that undoes one transaction, for example an import: records it created are destroyed, records it changed, moved to the trash, merged away, or cleared are restored from the snapshot that the transaction's apply took. Plan and apply that request like any other; it is destructive whenever it destroys records, so show it and apply with `--confirm-destructive` after confirmation. The command warns when a record was changed again later, because the revert discards those later changes. An erasure can never be reverted.

## Stored files and sent drafts

A file the user attaches is stored in the wiki, not referenced on their disk: the plan names it with size and SHA-256 under `files_to_store`, apply copies it to `records/_files/`, and the record browser links it. Say which files will be stored. A file above 25 MB stays outside the wiki; offer to store a link instead. A text file with something credential-shaped is refused until the user removes it.

Destroying an attachment keeps its file until the user cleans up, so a revert still works. When lint warns about stored files no record uses, or the user has sent drafts from the outbox and wants them gone, write the request with `crm_records.py cleanup-files --orphans` or `--path <file or draft folder>`, plan it, show the list, and apply with `--confirm-destructive` after confirmation.

## Duplicates and merging

To find duplicates, compare records with `crm_query.py find` on the identifying fields (e-mail, domain, name) and show the groups. Merging needs the user's choice of the surviving record and, where values differ, which record's value wins:

```json
{"op": "merge", "object": "company", "into": "<uuid of the survivor>", "from": ["<uuid>", "<uuid>"], "prefer": {"name": "<uuid>", "address": "<uuid>"}}
```

Empty fields of the survivor are filled from the merged records in order, multi-target links and rich text are joined, every relation pointing at a merged record is moved to the survivor, and the merged records are destroyed. Merge is destructive: show the plan and apply with `--confirm-destructive` after confirmation.

## Trash, destruction, GDPR erasure

- "Lösch die Aufgabe" means the trash (`delete`); say that it can be restored. `restore` brings it back.
- "Endgültig löschen" means `destroy`, after confirmation. Explain the effect on related records (cleared links, cascaded junction records) from the plan.
- A request based on the right to erasure (Art. 17 GDPR) means `erase`. Plan it first without applying and show: the record, every record that links to it, `erase_redacts_records` (records whose copies of the person's e-mail or phone number are replaced), `erase_removes_files` (stored files only the erased records used and outbox drafts that mention the person), `history_files_to_clean`, and the warnings about records that still mention the name. Ask whether those mentions and the stored text files that mention the person should go too; if yes, plan again with `{"op": "erase", ..., "redact_mentions": true}`, so the redaction happens inside the erasure. Never clean such mentions afterwards with an ordinary update: it would write the name into the event log and the history again. Remind the user that copies outside the wiki (earlier exports, bundles, other devices, backups, outbox copies made with `--outbox`, mails already sent) are not reached, and that a later import or e-mail can create the same address again. Knowledge pages and registered sources that mention the person are listed as warnings and changed only through their own procedures (page batch, withdrawn source) after the user decides. Apply with `--confirm-destructive` only after explicit confirmation; report `history_still_mentions` from the result if it is not empty; then rebuild, lint and release so the erased state becomes the published one. Finally delete every temporary plan and request of the session: they hold the person's identifying values.

## Importing CSV, XLSX, JSON

Import order follows the relations: members, companies, people, opportunities, then notes and tasks. Each object is one import run with exactly one match key.

1. `crm_import.py inspect --target <wiki> --file <runtime path> --object <object> --write-mapping <mapping.json>` reports encoding, delimiter, headers, row count, sample rows, and a suggested mapping, and writes that mapping as a file outside the wiki. Show the mapping and ask about columns that have no field (offer to create a custom field with `crm_schema.py`, or to leave the column out) and about ambiguous date formats. Reading options, valid for `inspect` and `plan`: `--encoding` (for example `cp1252`), `--delimiter` (`;`, `,`, `|` or `tab`), `--sheet` (XLSX sheet name or number), `--date-format DMY|MDY|YMD` for dates like `01/02/2024`, `--decimal comma|dot` for text numbers, `--time-zone` for times without offset.
2. Edit the mapping if needed. Its format: `{"format": "lmwiki-crm-import-mapping/1", "object": "person", "columns": {"<column header>": "<field key>" | null}}`. Field keys are `id`, `crm_created_at`, `<field>`, `<field>.<subfield>` (`amount` for a currency amount), `<relation>.<field of the target>` (`company.domainName`, `accountOwner.userEmail`), and `<morph field>.<object>[.<field>]` (`targets.person`); `null` leaves a column out.
3. `crm_import.py plan --target <wiki> --file <file> --object <object> --mapping-file <mapping> --match-key <key> --mode upsert --actor <actor> --origin-ref <portable file name> --output <plan>` builds the transaction (`--auto-mapping` instead of `--mapping-file` takes the suggestion as is). The match key is `id` or one unique field such as `domainName` or `emails.primaryEmail`; `--mode` is `create`, `upsert` or `update-only`. Show the row report: rows to create, to update, unchanged, `row_changes` (row, record, every changed field with old and new value), and every error with row, column, and rule.
4. Fix the file or, with the user's consent, re-plan with `--skip-invalid-rows`; then `crm_import.py apply --target <wiki> --plan-file <plan> --expect-plan-sha256 <hash>`.

Links of notes and tasks from another CRM come as junction files: import them with `--object noteTarget` or `--object taskTarget` (columns: the note or task id plus `targetPerson`, `targetCompany` or `targetOpportunity`), match key `id`, mode `upsert` or `update-only`, after the notes, tasks and their targets exist.

Semantics follow the common CRM import rules: an empty cell clears the field (a column mapped to a whole currency, e-mail, link, or phone field stands for its amount or primary value, so the currency code and additional values stay), a missing column leaves it unchanged, a multi-select cell replaces the list, select values are matched by API name or label, defaults apply to new records, a match against a record in the trash restores it, and a value that would duplicate a unique field is an error. The plan lists under `empty_cells` every existing value an empty cell would clear; when the user wants to keep those values, re-plan with `--keep-empty-cells`. A row that repeats an earlier row of the file exactly is imported once and reported under `duplicates_ignored`; two rows with the same match value but different contents are row errors, because the file does not say which one is right. Relation columns must name existing records (by ID, domain, e-mail, or exact name), which is why the order matters. The original file stays outside the wiki; the events record its portable name, its SHA-256, and the row of every change. An import never copies files into the wiki: a FILES column may hold links, but a cell that names a local file to upload is a row error. Attach files through a `crm_records.py` request instead.

## Exporting

`crm_export.py` writes outside the wiki: `csv --object <object> | --view <view id or label>` (`--format standard` gives import-compatible headers with subfield labels, `--format plain` one column per stored key; `--include-deleted` adds the trash; files split at `--max-rows`, default 20,000), `json` in the API shape, `sample` for an import template, and `junctions` for the note and task targets in junction format. Before handing an export to the user, say how many records with personal data it contains.

## Changing the data model

`crm_schema.py plan --target <wiki> --request-file <request> --output <plan>` and `apply ... --expect-plan-sha256 <hash>` change `schema/crm/datamodel.json` and, where needed, the records in one confirmed step: add custom objects and fields (a relation also creates its inverse), rename an object or change its description or icon (`update-object`, standard objects included), change field labels, descriptions, defaults, required and unique flags (refused while records violate them, with the list), add, relabel, recolor, reorder, rename (with record migration), or remove select options, deactivate or reactivate fields and objects, and delete custom fields or objects (destructive). Standard fields cannot be deleted, only deactivated. The plan lists `dependent_references`: views, dashboards, roles and active or draft workflow versions that use what changes; show them before the user confirms. A data model change is followed by a rebuild of the views and a minor release. The request:

```json
{"actor": "human:anna@example.com", "occasion": "Projekte erfassen", "operations": [
  {"op": "add-object", "name": "project", "labelSingular": "Projekt", "labelPlural": "Projekte", "labelField": "name",
   "fields": {"name": {"type": "TEXT", "label": "Name"}}},
  {"op": "add-field", "object": "project", "name": "company", "type": "RELATION", "label": "Firma",
   "relation": {"type": "MANY_TO_ONE", "target": "company", "inverse": "projects", "inverseLabel": "Projekte", "onDelete": "SET_NULL"}},
  {"op": "add-field", "object": "project", "label": "Status", "type": "SELECT", "default": "OPEN",
   "options": [{"value": "OPEN", "label": "Offen", "color": "sky"}, {"value": "DONE", "label": "Erledigt", "color": "green"}]},
  {"op": "update-object", "object": "opportunity", "labelSingular": "Chance", "labelPlural": "Chancen"},
  {"op": "rename-option", "object": "opportunity", "field": "stage", "from": "SCREENING", "to": "QUALIFIED", "label": "Qualifiziert"},
  {"op": "remove-option", "object": "opportunity", "field": "stage", "value": "MEETING", "map_to": "PROPOSAL"}
]}
```

Further operations: `update-field` (label, description, nullable, unique, default), `add-option`, `update-option`, `deactivate` and `activate` (with `field`, or without it for the whole object), `delete-field`, `delete-object`. The full list with examples is in the docstring of `crm_schema.py`.

## Credential findings

When a planned value or an existing record matches the credential screen, tell the user the record, field, and kind, never the value. The planner refuses such a value before it reaches the event log. A real credential is removed from the request or replaced with `[credential removed]` (imports offer `--redact-credentials`) and the user is advised to rotate it. A harmless look-alike, such as a company name in the shape of a cloud key, may be acknowledged by a named person after that person confirmed it: from a plan error with `crm_records.py acknowledge-credential --kind <kind> --value-sha256 <digest> --confirmed-by human:<id> --reason <text> --user-confirmed-harmless` (kind and digest are in the error message), or from a lint finding with `--path <file> --line <n>` instead of `--value-sha256`. Then plan again.
