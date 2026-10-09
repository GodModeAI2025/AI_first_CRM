# CRM workflows

Read `crm-contract.md` first. Workflows follow the common model: a trigger and a chain of steps, kept as numbered versions. The difference is time: nothing runs in the background. A workflow runs when the agent starts it, and every invocation can catch up what became due in the meantime.

## Contents

- [Lifecycle](#lifecycle)
- [Definition](#definition)
- [Triggers](#triggers)
- [Steps](#steps)
- [Running](#running)
- [Pauses: forms, delays, AI steps](#pauses-forms-delays-ai-steps)
- [Outbox](#outbox)
- [Limits](#limits)

## Lifecycle

```text
crm_workflows.py save-draft   --workflow <id> --definition-file <file>
crm_workflows.py create-draft --workflow <id> --from-version <n> [--replace-draft]
crm_workflows.py activate     --workflow <id> --version <n>
crm_workflows.py deactivate   --workflow <id>
crm_workflows.py delete       --workflow <id> --confirm-destructive
crm_workflows.py validate | list
```

Every command also takes `--target <wiki>` and `--lock-token`; call it through `run_locked.py`, which adds the token. `delete` removes the definition with all versions after the user confirmed it (waiting runs must be cancelled first); the run log stays as history. A workflow id becomes a file name, so ids the storage treats specially (`con`, `aux`, or a name ending like `-laptop-repair`, which OneDrive reads as a conflict copy) are refused.

A workflow lives in `schema/crm/workflows/<id>.json` with versions DRAFT, ACTIVE, DEACTIVATED, and ARCHIVED; at most one version is ACTIVE. Activating a version freezes it by the hash of its trigger and steps, and the previously active version becomes ARCHIVED, as is usual in CRMs. To change an active workflow, create a draft from it, edit, and activate the draft. Show the user the definition in words before activating; activation, like every definition change, ends with a rebuild, lint, and a minor release.

## Definition

`save-draft` takes one version body:

```json
{
  "name": "Closed Won",
  "description": "Nach dem Gewinn: Onboarding-Aufgabe und E-Mail-Entwurf",
  "trigger": {"type": "DATABASE_EVENT", "settings": {"eventName": "opportunity.updated", "fields": ["stage"]}, "nextStepIds": ["won"]},
  "steps": [
    {"id": "won", "type": "FILTER", "settings": {"input": {
      "stepFilterGroups": [{"id": "g1", "logicalOperator": "AND"}],
      "stepFilters": [{"id": "f1", "type": "SELECT", "stepOutputKey": "{{trigger.object.stage}}", "operand": "IS", "value": "CUSTOMER", "stepFilterGroupId": "g1"}]}},
     "nextStepIds": ["due"]},
    {"id": "due", "type": "CODE", "settings": {"input": {"logicFunctionInput": {}, "formulas": {"dueAt": "date_add_days(now(), 3)"}}}, "nextStepIds": ["task"]},
    {"id": "task", "type": "CREATE_RECORD", "settings": {"input": {"objectName": "task", "objectRecord": {
      "title": "Onboarding: {{trigger.object.name}}", "dueAt": "{{due.dueAt}}", "status": "TODO",
      "assignee": {"id": "{{trigger.object.owner.id}}"},
      "targets": [{"id": "{{trigger.object.id}}"}, {"id": "{{trigger.object.company.id}}"}]}}},
     "nextStepIds": ["mail"]},
    {"id": "mail", "type": "DRAFT_EMAIL", "settings": {"input": {
      "recipients": {"to": "{{trigger.object.pointOfContact.emails.primaryEmail}}"},
      "subject": "Willkommen", "body": "Hallo {{trigger.object.pointOfContact.name.firstName}}, ..."}}}
  ]
}
```

The `due` step only illustrates a CODE step for a user who asked for a deadline three days after winning; never add such a rule the user did not ask for.

Variables use `{{...}}` paths: `trigger.object.<field>` (relations resolve to the linked record, so `trigger.object.company.name` works), `<stepId>.<field>` for earlier step results, and the iterator's current item. Conditions accept the usual `stepFilterGroups` and `stepFilters` or a compact `condition` in the `crm_filters` format. Secrets never appear in a definition: an HTTP header refers to an environment variable by name (`{"env": "NAME"}`) and is never resolved or stored.

## Triggers

| Trigger | Here |
|---|---|
| Manual (global, one record, several records) | `run plan --workflow <id>` with `--record <selector>` (repeatable) or `--records-file` |
| Record created, updated, deleted, upserted (`eventName` `<object>.<action>`, optional `fields`, `filter`) | `run plan --due` reads `meta/crm-events/` since the workflow's cursor; one run per matching event |
| Schedule (`DAYS`, `HOURS`, `MINUTES`, or a cron pattern, in UTC) | `run plan --due` starts one run for all times missed since the last check and reports their number (`missedSlots`); a daily digest missed for five days runs once |
| Webhook (`httpMethod`, `expectedBody`) | the payload is given as a file with `--payload-file`; differences from `expectedBody` are warnings; an API key cannot be checked |

Event triggers ignore the workflow's own writes, data model migrations, and anything deeper than one workflow level, so a workflow cannot loop. Changes made outside the skill (by hand, by a synchronization client) produce no events and trigger nothing. A large import can trigger many runs; at most 500 runs are planned at once and the rest follow on the next `--due`.

At the start of a CRM session, check whether active workflows have due work (`run plan --due` for each active workflow, or `list`) and tell the user what would run before applying it.

## Steps

All 22 common workflow action types are recognised:

- full: CREATE_RECORD, UPDATE_RECORD, UPSERT_RECORD, FIND_RECORDS (filter, sort, limit 200 by default, offset), FILTER, IF_ELSE (first matching branch wins), ITERATOR (up to 10,000 items, checked before the first pass), EMPTY, PICK_RECORD (RANDOM reproducible per run, ROUND_ROBIN with a stored cursor, LOAD_BALANCED), FORM;
- partial: DELETE_RECORD (to the trash; `destroy: true` needs `--confirm-destructive`), DELAY and WAIT_FOR_EVENT (resume on the next `--due` after their time), CODE (only formulas of `crm_formula.py`: arithmetic, comparisons and the functions `abs`, `ceil`, `coalesce`, `concat`, `contains`, `date_add_days`, `days_between`, `find`, `floor`, `get`, `if_`, `join`, `len`, `lower`, `max`, `min`, `now`, `number`, `parse_json`, `round`, `split`, `sum`, `text`, `today`, `trim`, `upper`; arbitrary JavaScript is a validation error), AI_AGENT, CLASSIFY, SEND_CHAT_MESSAGE (pause for the host agent's answer), SEND_EMAIL and DRAFT_EMAIL (`.eml` file, never sent), CREATE_CALENDAR_EVENT (`.ics` file), HTTP_REQUEST (request specification file, never executed);
- not supported: LOGIC_FUNCTION.

Steps cannot create, change or delete records of the objects the skill maintains itself: workspace members, workflows and their runs, dashboards, messages, message threads and participants, calendar events and their participants. Use the e-mail and calendar import for messages and events, and the config and workflow helpers for the rest.

`continueOnFailure` is honoured; `retryOnFailure` is accepted but has no effect. Record operations of one run that happened before a failing step stay in the run's transaction, as is usual in CRMs.

## Running

```text
crm_workflows.py run plan  --workflow <id> [--record <selector> ... | --records-file <file>] [--payload-file <file>]
                           [--answers-file <file>] [--due] [--now <instant>] [--version <n>]
                           --actor <actor> --output <plan> [--outbox <extra copy outside the wiki>]
crm_workflows.py run apply --plan-file <plan> --expect-plan-sha256 <hash> [--confirm-destructive]
crm_workflows.py run cancel --workflow <id> --run-id <id> --actor <actor>
```

`run plan` answers `planned` (show and apply), `nothing_due` (no run and no state change: nothing to apply, the plan file can be deleted) or `invalid`. One plan holds one CRM transaction for all record operations of its runs, the run entries for `meta/crm-runs/YYYY-MM.jsonl`, the new `meta/crm-workflow-state.json`, and the outbox files. Show the user the runs with their step statuses, the record changes, and the outbox files before applying; then apply, rebuild, lint, and release with the session. A plan that only advances the event cursor (zero runs) still has to be applied so the same events are not looked at again. Run entries redact credential-like values and cut step outputs at 32,000 bytes. A run records its workflow, version, and definition hash; a waiting run continues only with the same version.

## Pauses: forms, delays, AI steps

FORM, AI_AGENT, CLASSIFY, and SEND_CHAT_MESSAGE pause the run (status waiting) with their question or prompt. Ask the user the form fields in the conversation, or answer an AI step yourself as the host agent, write the answers file (`lmwiki-crm-workflow-answers/1`), and continue with `run plan --answers-file`. CLASSIFY answers must be one of the defined categories. DELAY and WAIT_FOR_EVENT keep the run waiting until a later `--due` after their time; report the delay when it resumes late. A waiting run can be cancelled with `run cancel`.

## Outbox

E-mail drafts (`.eml` with `X-Unsent: 1`), calendar files (`.ics`), and HTTP request specifications (`lmwiki-crm-http-request/1`) are kept in the wiki under `records/_outbox/workflows/<workflow>/`, named after the run and step. They are released with the wiki, listed on the CRM start page, checked for credentials (a draft with a secret fails its step), and removed by an erasure that concerns a person they mention. `--outbox <folder>` additionally copies them to a folder outside the wiki, for example the user's mail drafts folder; that copy is outside the erasure's reach, so say so. Tell the user plainly that nothing was sent or called and where the drafts are. After sending, `crm_records.py cleanup-files --path records/_outbox/workflows/<workflow>` removes them on request.

## Limits

No background execution, no instantaneous triggers, no outgoing network calls, no arbitrary code, no app logic functions, no credits. Parallel branches run one after another; a pause inside an iterator pauses the other branches too. Past runs with their status and step outputs are listed by `crm_query.py runs` (filters `--workflow`, `--status`, `--since`; one run with `--run-id`). Run entries are kept without a retention deadline; removing them follows the removal policy (preview and confirmation), and a GDPR erasure replaces the erased person's data in them and in waiting runs.
