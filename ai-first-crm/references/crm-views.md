# CRM views, dashboards, and questions about the data

Read `crm-contract.md` first. Views and dashboards are definitions under `schema/crm/`; `crm_build.py` renders them, together with a record browser per object, into read-only offline HTML under `graph/crm/`. Questions about the data are answered with `crm_query.py`, never from memory.

## Contents

- [Answering questions](#answering-questions)
- [The generated pages](#the-generated-pages)
- [Views](#views)
- [Dashboards](#dashboards)
- [Changing views, dashboards, settings, roles](#changing-views-dashboards-settings-roles)
- [Building and freshness](#building-and-freshness)
- [Limits](#limits)

## Answering questions

Use `crm_query.py` for every figure, list, or record detail in an answer:

| Question | Command |
|---|---|
| "Welche Deals sind im Angebot?" | `find --object opportunity --filter '{"field": "stage", "operand": "IS", "value": "PROPOSAL"}'` |
| "Wie viel steckt in der Pipeline?" | `aggregate --object opportunity --function SUM --field amount --group-by stage` |
| "Gewichteter Pipeline-Wert?" | `expected-amount --object opportunity --probabilities '{"NEW": 0.1, ...}' --group-by stage` |
| "Was wissen wir über Acme?" | `search --text Acme`, then `get --object company --id <uuid>` (fields, linked records, timeline) |
| "Wie lange liegen die Deals schon in ihrer Phase?" | `time-in-stage --object opportunity` |
| "Was hat sich diese Woche getan?" | `timeline --since <ISO instant>` |

During a maintenance session pass the lock token; outside one, query the last release with `--release`, which verifies the manifest before and after reading and refuses while a lock exists. Quote amounts with their currency and never add different currencies. Probabilities are shares between 0 and 1 (60 % is `0.6`); ask when the user's numbers are ambiguous. Say which moment and time zone a time-dependent answer refers to; the output names both. When records changed in the current session, answers reflect the unreleased state; say so.

## The generated pages

`graph/crm/index.html` is the CRM start page: objects with their record counts, views, dashboards, workflows, and recent activity. `graph/crm/objects/<object-directory>.html` (the kebab-case directory name, for example `workspace-member.html`) is the record browser of one object with search, sorting, paging, and the trash; `#<uuid>` opens a record with all fields, its linked records in both directions (a company also lists the mails and meetings of its people, an opportunity those of its point of contact and its company), stored files as links, its rich text, its timeline, the raw Markdown file, and the wiki pages that link to it. Wikilinks from knowledge pages to records open this browser. `graph/crm/views/<id>.html` and `graph/crm/dashboards/<id>.html` render the definitions below. Every page shows when it was built and in which time zone, works offline by double-click, loads nothing from the network, and is regenerated, never edited.

## Views

`schema/crm/views.json` (`lmwiki-crm-views/1`) holds a list of views. Keys:

| Key | Meaning |
|---|---|
| `id`, `label`, `description`, `object`, `position` | identity and order; `id` is kebab-case |
| `type` | `table`, `kanban`, or `calendar` |
| `fields` | visible fields in order; system fields such as `crm_created_at` are allowed |
| `filter` | filter group: `{"op": "AND" or "OR" or "NOT", "conditions": [...]}`, conditions `{"field", "operand", "value"}` with the standard operands (IS, IS_NOT, CONTAINS, DOES_NOT_CONTAIN, GREATER_THAN_OR_EQUAL, LESS_THAN_OR_EQUAL, IS_BEFORE, IS_AFTER, IS_EMPTY, IS_NOT_EMPTY, IS_NOT_NULL, IS_RELATIVE such as `PAST_7_DAY` or `NEXT_1_MONTH`, IS_IN_PAST, IS_IN_FUTURE, IS_TODAY); `@me` needs `me` |
| `sort` | `[{"field": ..., "direction": "asc" or "desc"}]`, several levels |
| `groupBy`, `dateGranularity`, `hideEmptyGroups` | table grouping; kanban columns (a SELECT field, columns in option order plus one for empty values) |
| `aggregates` | table footers, `{"amount": "SUM"}` with the twelve standard aggregate functions |
| `aggregate` | kanban column figure, `{"function": "SUM", "field": "amount"}` |
| `probabilities`, `expectedAmountField` | expected amount per kanban column, shares 0 to 1 per option |
| `dateField`, `calendarMode` | calendar on a DATE or DATE_TIME field, `month` or `week` |
| `visibility`, `roles` | `workspace` or `restricted` (shown as a note; files are not protected) |
| `me`, `icon`, `compact` | the member that `@me` means, display hints |

Private or unlisted views are refused: a released wiki is visible to everyone who can read the folder. Filters reach the fields of the viewed object, not fields of linked records; filter on the link itself instead.

## Dashboards

`schema/crm/dashboards.json` (`lmwiki-crm-dashboards/1`) holds dashboards with `id`, `label`, and `widgets`. A widget has `id`, `type`, `label`, a `layout` (`column` 0 to 11, `span` 1 to 12, `row`, `rowSpan`), and per type:

- `number`: `object`, `aggregate`, optional `filter`, `prefix`, `suffix`; the aggregate is `{"function": "SUM", "field": "amount"}` (`COUNT` needs no field), for example `{"id": "offen", "type": "number", "label": "Offene Pipeline", "object": "opportunity", "aggregate": {"function": "SUM", "field": "amount"}, "filter": {"op": "AND", "conditions": [{"field": "stage", "operand": "IS_NOT", "value": "CUSTOMER"}]}}`; a sum or count over no record shows 0 (with the currency for a currency field);
- `bar`, `line`, `pie`: `object`, `aggregate`, `groupBy`, optional `dateGranularity`, `secondaryGroupBy` (stacked bars), `secondaryDateGranularity`, `cumulative` (line), `orderBy` (`value`, `value_asc`, `label`, `label_desc`, `position`), `limit` (the usual display limits apply and hidden groups are reported), `orientation`, `stacked`, `donut`, `omitNullValues`;
- `table`: `view` (the id of a view);
- `richtext`: `text` in Markdown;
- `link`: `url` (an embedded iFrame widget becomes a link).

Charts are inline SVG; at most eight colored series, the rest is grouped. Amounts in different currencies are shown separately.

## Changing views, dashboards, settings, roles

Write the complete new document into a temporary file outside the wiki, then:

```text
crm_config.py plan  --target <wiki> --document views|dashboards|settings|roles --input <file> --output <plan>
crm_config.py apply --target <wiki> --plan-file <plan> --expect-plan-sha256 <hash> [--confirm-removal]
```

The plan validates the document against the data model and lists added, changed, and removed entries; a document that removes views or dashboards needs `--confirm-removal` after the user agreed. Afterwards rebuild, lint, and publish a minor release. "Show the pipeline with weighted amounts" is answered with `crm_query.py`; ask before saving probabilities into the view.

## Building and freshness

`crm_build.py --target <wiki>` (under the lock) rebuilds `graph/crm/` completely in a staging directory and replaces changed files atomically; `--check` only reports whether the pages are current (exit 3 when stale). The build is deterministic: the same inputs and `--now` give identical bytes. `graph/crm/manifest.json` records a hash over every input (data model, settings, views, dashboards, roles, workflows, records, events, wiki backlinks to records, and the renderer version); lint reports the pages as stale whenever an input changed, so the build runs before every release of a wiki with a CRM layer. Measured: about 5 seconds and 13 MB for 15,000 records.

## Limits

Pages are read-only: moving a kanban card, editing a cell, or changing a dashboard happens through a request to the agent. Kanban columns come from select fields only. The calendar shows months and weeks. Relative date filters and time-in-stage figures are evaluated when the pages are built, in the time zone from `schema/crm/settings.json`, not the viewer's. Record pages have a fixed layout without tabs. MIN and MAX of date fields are available in `crm_query.py`, not in dashboards.
