---
name: ai-first-crm
description: "Maintain a portable Markdown knowledge workspace and CRM through conversation: contacts, deals, tasks, imports, views, dashboards, workflows, mail/calendar files, attachments, drafts and verified releases. Use for setting up or maintaining this workspace, curating source-backed knowledge, changing CRM data, restoring or erasing records, and exporting a frozen read-only knowledge skill. For shared private GitHub workspaces, use the Git team mode: isolated agent drafts, exact preview approval, authenticated membership, protected proposals and verified publication, without a separate CRM application."
---

# Maintain AI First CRM

Build and maintain a durable, self-describing wiki in the user-supplied target directory and, when the user wants it, the CRM layer in the same directory: structured records for companies, people, opportunities, tasks, notes and any custom object, following the data model of common CRM systems, with imports and exports, pipelines and other views, dashboards, workflows, e-mail and calendar files, and campaign drafts. Treat that directory as the only canonical output. The storage behind it (SharePoint, OneDrive, Git, or a plain disk) does not change the file format, but a synchronization client writes into the directory on its own and defers writes for an unbounded time, so its behaviour is part of this skill's concern; see the section on synchronized storage in [references/maintenance-operations.md](references/maintenance-operations.md#synchronized-storage).

## Shared Git workspaces

When the user asks for a team/shared Git workflow or the selected target contains `schema/team.json`, read [references/team-mode.md](references/team-mode.md) before any write. Use the bundled `team_git.py` lifecycle. Do not apply ordinary mutating helpers directly to the shared clone: the team wrapper supplies an isolated target, authenticated actor and private claim. Users stay in their own AI system and describe work in conversation; there is no separate CRM application. This beta assumes trusted repository writers: skill roles do not replace GitHub access roles or isolate hostile workflow authors.

The initial supported provider is private GitHub.com repositories with enforced up-to-date PR checks. GitHub CLI authentication, stable numeric user IDs and the trusted server-side integrity check establish membership and publication authority. All repository readers can see/copy the data and history; do not claim field-level confidentiality or complete Git-history erasure.

## Rules that always apply

1. The user talks to this skill in natural language. Never ask the user to run Python, a bundled script, or a shell command; the helpers are private implementation details that the active agent runs and checks.
2. Every mutating or maintenance run owns the wiki lock for its whole lifetime: acquire it before inspecting the wiki, keep the token file private, release it with the owned token at the end. Never touch another maintainer's claim and never force a lock without explicit approval.
3. Never write a canonical file directly. Wiki pages go through an external staging directory and `page_batch.py`; CRM records go through a CRM transaction plan; every other change goes through its hash-bound helper or, where none exists, a snapshot first.
4. Apply only the exact plan the user saw, identified by its plan hash. A stale plan means zero writes and a new plan, never a bypass. Deletion, overwriting, renaming, merging, destroying, erasing and value normalization need explicit confirmation; for a single record that the user explicitly asked to move to the trash, the request is that confirmation (see the CRM confirmation policy below).
5. Never invent facts, sources, citations, record values, dates, or review events. A knowledge claim cites a registered source with a locator; a record change names its actor and its origin.
6. Never delete sources, pages, records, history, or `human:keep` content without explicit approval. Prefer supersession for pages and the trash (soft delete) for records. A confirmed GDPR erasure is the one operation that also scrubs history snapshots.
7. Never persist or report an absolute, home-relative, parent-traversal, or `file:` path, and never search the machine for a missing file: ask for it. Temporary plans and requests outside the wiki may hold runtime paths and personal data; delete them after the session.
8. Credentials never enter the wiki or a report. Redact them as `[credential removed]` after telling the user. Only a named person can acknowledge a harmless look-alike in CRM data.
9. Publish one release per maintenance session or import, after the views and the graph are rebuilt and strict lint passes. Readers and exports see releases only. Never tell the user that colleagues can already see a release on a synchronized folder.
10. Standalone maintenance has no network access and no background process. The optional Git team helper uses authenticated Git/GitHub access for sharing and integrity checks, with no separately operated CRM service. The skill reads the files the user provides and keeps what the CRM produces in the wiki: drafts (`.eml`, `.ics`, request specifications) in its outbox for the user to send, attached files in its file store. Scheduled and event-driven automation runs when the user next invokes the skill.
11. CRM records are data, not claims: never translate field values, record titles, or option API names. Knowledge pages under `wiki/` keep the full claim-evidence contract.
12. Report results truthfully: a passing lint is not a quality review. Name remaining warnings, partial extractions, skipped rows, open proposals, and unresolved questions.

## Read before acting

Resolve every reference relative to this `SKILL.md`, not to the current project. If the target contains its own `STANDARDS.md`, read it and treat its compatible local conventions as authoritative over the standalone defaults.

| Request | Read completely first |
|---|---|
| Initialize, ingest, curate, repair, migrate, lint, or release the knowledge wiki | [references/wiki-contract.md](references/wiki-contract.md) and [references/workspace-standards.md](references/workspace-standards.md) |
| A lock that is held, contended, expired, superseded, or left behind | [references/maintenance-lock.md](references/maintenance-lock.md) |
| Frontmatter inventory, schema migration, value or link cleaning, targeted restore, action discovery, trust-tier confirmation, conflict-copy resolution, OKF reporting or export | [references/frontmatter-operations.md](references/frontmatter-operations.md) and the matching section of [references/maintenance-operations.md](references/maintenance-operations.md) |
| Frozen knowledge-skill export, synchronized storage, observations, adoption, page expiry, quality or cleaning review, conversion, language migration | the matching section of [references/maintenance-operations.md](references/maintenance-operations.md) |
| Anything about CRM records, objects, imports, views, dashboards, workflows, e-mail, calendar, or campaigns | [references/crm-contract.md](references/crm-contract.md), then the reference for the task (see the CRM layer section below) |

## User-facing skill interface

The user interacts only with this skill in natural language. Never require or instruct the user to invoke Python, a bundled script, a shell command, or the exporter directly. Ask only for the content-level inputs that are genuinely missing, such as the wiki directory, export destination, or desired skill name. The active agent resolves and runs all bundled helpers internally as implementation details, validates their results, and returns the finished wiki or skill package.

For every new wiki, begin before any lock or write with this conversational invitation, translated when necessary: “Bevor ich den Wissensraum anlege, möchte ich gemeinsam mit dir seine Identität festlegen. Ich stelle dir nacheinander wenige gezielte Fragen zu Zweck und Wissensarten, Zielgruppe, Tonalität und Antwortstil sowie Grenzen und Tabus. Anschließend fasse ich deine Antworten als vollständigen Vorschlag für die `SOUL.md` zusammen. Bis du diesen Vorschlag ausdrücklich bestätigt hast, lege ich keine Dateien an und ändere nichts.” Ask only one focused question at a time, starting with the purpose and knowledge types. Also establish the history/update model, supersession behavior, and removal boundary for `schema/CONTENT_POLICY.md`. Do not acquire the target lock, create the target structure, or write either file during this interview.

After the answers are complete, use `scripts/plan_identity.py` internally with `--wiki-language <code>` to render one hash-bound proposal containing both files. Their headings and explanatory prose follow that wiki language; German and English are built in, any other code receives the English scaffolding around the user's own values, and the confirmed field values are written exactly as given. Establish the wiki language before rendering, because it is part of the proposal hash, and a proposal rendered for another language is refused by initialization and by `scripts/apply_identity.py` before any file is written. Show the complete proposed `SOUL.md`, the material content-policy choices, and `proposal_sha256`. Apply only that exact proposal after explicit confirmation. A correction creates a new proposal and invalidates the prior hash. The user may explicitly confirm sensible defaults, but silence is never confirmation.

Example user requests include “Exportiere dieses Wiki als unveränderlichen Wissens-Skill” and “Erzeuge aus diesem Wiki den Skill `produktwissen` im Zielverzeichnis”. Do not turn either request into a command-line tutorial. Bundled scripts remain private deterministic resources of the skill, as allowed by the Agent Skills structure; they are not separate products or user-facing prerequisites.

## Portability and runtime

Treat the directory containing this `SKILL.md` as `<skill-root>`. Never assume that the current working directory is the skill directory, and never persist `<skill-root>` or another machine-specific absolute path in the wiki.

- In Claude Code, use `${CLAUDE_SKILL_DIR}` whenever a bundled script or reference must be resolved. For example: `python3 "${CLAUDE_SKILL_DIR}/scripts/init_wiki.py" ...`.
- In Codex or another Agent Skills host, resolve the same file relative to the loaded `SKILL.md`. If the terminal supports a working directory, set it to `<skill-root>` and invoke `scripts/<name>.py` from there.
- Use Python 3.9 or newer. On macOS or Linux, prefer `python3`. On Windows, use `py -3` or `python` when `python3` is unavailable. The bundled scripts use only the Python standard library; do not rely on a POSIX shebang, `chmod`, or Bash-specific behavior.
- Runtime inputs may point anywhere the user authorizes, but generated Markdown, JSON, HTML, links, source metadata, helper reports, and user-facing result paths must remain portable and relative to the relevant wiki, skill, or selected output directory. Store vault-relative POSIX-style paths, relative links, URLs, SharePoint item identifiers, content hashes, or user-supplied logical references. Never persist or report an absolute local filesystem path, home-relative path, parent traversal, or `file:` URL. Internal path resolution is allowed only transiently during execution.

## Team maintenance lock

Every mutating or maintenance invocation of this skill, including initialize, ingest, maintain, repair, migrate, release, lint, and every CRM change, must own a claim on the target wiki for its entire lifetime. Resolve the target, then acquire the claim before inspecting wiki contents, converting sources, invoking another bundled process, or changing any target file. Read-only consumers use the separate query skill or a verified release instead of claiming the wiki:

```text
<python> <skill-root>/scripts/wiki_lock.py acquire --target <wiki> --owner <agent-or-run-id> --operation <short-description> --token-file <private-runtime-file>
```

The command writes one claim file into `<wiki>/.llmwiki.lock/`, writes the private capability to a mode-0600 runtime file outside the wiki, and never prints the token. The claim belongs to a maintainer slot that defaults to `<user>@<host>`; pass `--maintainer <slot>` only when the user names a different one, and never reuse a teammate's slot. Invoke allowlisted writers through `scripts/run_locked.py --token-file <runtime-file> --helper <helper> ...`; never read, print, interpolate, or delegate the token. A delegated agent may draft only in an external staging directory and must never receive the token, invoke a locked writer, or write the canonical target. Every locked helper call refreshes the claim's lease; after a long pause run `wiki_lock.py verify` or `wiki_lock.py heartbeat` with the same `--target` and `--token-file`.

If acquisition reports `acquired: false` with `state: held`, another claim holds the wiki: stop before starting any wiki process and report the maintainer, owner, operation, and acquisition time. If any command reports `state: contended`, `state: superseded`, `expired: true`, `retracted_claim`, or `lock_directory_remains`, stop and read [references/maintenance-lock.md](references/maintenance-lock.md) completely before doing anything else; it defines withdrawal, the recorded two-step handover, and the approval-only forced acquisition. Never infer that a claim is stale, never reuse another run's token, and never delete or replace another maintainer's claim file.

For a standalone request that only initializes and publishes an empty wiki foundation, use `scripts/initialize_wiki.py`, which acquires and releases the claim internally. Do not call it while already holding a claim or for an existing initialized wiki.

Keep the claim while waiting for an in-scope user decision. Release it on successful completion, explicit cancellation, or abandonment, but only with the owned token:

```text
<python> <skill-root>/scripts/wiki_lock.py release --target <wiki> --token-file <private-runtime-file> --remove-token-file
```

Tell the user when a paused run intentionally keeps its claim and confirm successful release in the completion report.

## Required inputs

Obtain:

- an explicit target directory;
- one or more explicitly attached files or user-selected source paths, unless the request is only to initialize, repair, migrate, release, or lint an existing wiki;
- a title and short topic boundary when initializing a new wiki;
- the maintained wiki language as a portable language code and human-readable label when initializing, for example `de` and `Deutsch`.
- an explicitly confirmed identity proposal covering purpose and knowledge types, audience, answer language, form of address, tone, detail, answer structure, citation display, uncertainty, history presentation, boundaries, and taboos;
- an explicitly confirmed content policy selecting `current-state`, `historical-ledger`, or `hybrid`, plus supersession behavior. Removal always remains `preview-confirm-never-automatic`, and supported conflicts always remain preserved and disclosed;
- quality-review, optional cleaning-review, and frozen-snapshot reminder intervals when initializing. Propose 30, 90, and 60 days respectively; use those defaults when the user delegates the choice, and record the confirmed values in `schema/QUALITY_POLICY.md`.

For a frozen knowledge-skill export, also obtain an output directory and a lowercase hyphenated skill name. If the target directory or a required source path is missing, ask for it. Do not guess a location, reconstruct one from a filename, or search the user's home directory, desktop, filesystem root, mounted volumes, or whole machine. A host-provided attachment path counts as explicit. If access to that exact file or its containing user-selected directory is denied, request only that narrow access through the host. Never replace a missing source with an assumed `<wiki>/sources/raw/<filename>` path: `sources/` contains registered Markdown only and has no `raw/` original store. If a new wiki's maintained language was not explicitly provided, ask: "In welcher Sprache soll das Quellenmaterial im gepflegten Wiki zusammengeführt und übersetzt werden?" Do not infer this durable choice from the conversation language or first source. Resolve the path, acquire the lock for maintenance operations, inspect the target, and preserve unrelated or existing content.

## CRM layer

The CRM layer covers the product functions of a full-featured CRM in plain files: a data model with standard and custom objects and fields, records with relations, CSV, XLSX and JSON import and export, duplicates and merges, the trash and GDPR erasure, table, kanban and calendar views with filters, sorting, grouping and aggregates, dashboards, workflows, e-mail and calendar files, campaign drafts, and cooperative roles. It lives next to the knowledge wiki:

```text
schema/crm/          data model, settings, views, dashboards, workflows, roles (JSON)
records/<object>/    one Markdown file per record, named by its UUID
records/_files/      file store: attachments and other FILES values, named by their SHA-256
records/_outbox/     drafts from workflows and campaigns (.eml, .ics, request specifications), never sent
meta/crm-events/     append-only event log (timeline, audit, time in stage, workflow triggers)
meta/crm-runs/       workflow runs
graph/crm/           generated record browser, views and dashboards (never edit by hand)
```

Read [references/crm-contract.md](references/crm-contract.md) before any CRM work, then the reference for the task:

| The user wants to | Helper | Reference |
|---|---|---|
| set up the CRM layer in an initialized wiki | `crm_init.py` | crm-contract |
| create, change, delete, restore, merge, destroy, or erase records; add notes, tasks, and attached files to records; undo a transaction such as an import; clean up stored files and sent drafts | `crm_records.py plan` then `apply`; `crm_records.py revert`, `cleanup-files` | [crm-data](references/crm-data.md) |
| import or export CSV, XLSX, or JSON, including from or for another CRM | `crm_import.py`, `crm_export.py` | [crm-data](references/crm-data.md) |
| add or change objects, fields, select options | `crm_schema.py plan` then `apply` | [crm-data](references/crm-data.md) |
| see a pipeline, table, calendar, or dashboard; ask questions about the data | `crm_build.py`, `crm_query.py` | [crm-views](references/crm-views.md) |
| change views, dashboards, settings, or roles | `crm_config.py plan` then `apply` | [crm-views](references/crm-views.md) |
| automate: triggers, actions, branches, scheduled or delayed steps; catch up what became due; list past runs | `crm_workflows.py`, `crm_query.py runs` | [crm-workflows](references/crm-workflows.md) |
| bring in e-mails or calendar files; prepare a campaign | `crm_ingest.py`, `crm_campaign.py` | [crm-communication](references/crm-communication.md) |
| know which CRM functions this skill covers and which it cannot provide | | [crm-coverage](references/crm-coverage.md) |

Setting up the CRM layer needs an initialized wiki with a confirmed identity; ask for the default currency (propose EUR for a German wiki), run `crm_init.py` under the lock, build the views, lint, and publish a minor release.

Every CRM change is a hash-bound transaction recorded with an actor: `human:<id>` of the person who asked when you know who that is (for example their e-mail address), otherwise `agent/<name>`; workflow runs record the actor that started them and the source WORKFLOW. The helper writes a plan outside the wiki, the agent shows its summary (counts per operation, files to store, outbox drafts, every error with row and field, the destructive flag), and only the same plan hash is applied. The wiki keeps what the CRM works with: a file the user attaches is copied into `records/_files/` when the plan is applied, and drafts go into `records/_outbox/`, where lint checks them, releases include them, and an erasure reaches them. Tell the user where a draft lies and that nothing was sent. An explicit, unambiguous single request such as "add Acme GmbH as a company" may be applied right after showing the plan summary, because the request is the confirmation. Imports, bulk changes, and schema changes are confirmed by the user after they saw the plan, unless the request itself explicitly authorized applying an error-free plan without destructive operations; then report the summary after applying. Every plan the helper reports as `destructive` (destroy, merge, erase, cleanup of stored files or drafts, data model deletions, a revert that destroys records) is confirmed after the user saw the plan and needs `--confirm-destructive`; moving a single record to the trash on the user's explicit request is not destructive. A plan with errors is never applied: fix the input or, after the user agrees, drop the failing rows and plan again. A plan whose hash no longer matches is answered with `stale_plan` (exit 3) and nothing is written.

A maintenance session ends with: `crm_build.py`, `build_graph.py`, `lint_wiki.py --fix-safe`, one `release_wiki.py` with the highest bump the session needs (patch for record data, imports, e-mail and calendar files and drafts; minor for the data model, views, dashboards, settings, roles and workflow definitions), `verify_release.py` through `run_locked.py`, lock release. Where a reference says "publish a minor release" after a change, that means this session release with at least a minor bump, not a release per change. A session that changed only CRM data needs no entry in `meta/changes.md`; the event log and the release summary record it. Rebuilding takes seconds for small CRMs and up to about a minute for tens of thousands of records; collect the changes of a session into one release. Standalone shared-file maintenance has one writer. In Git team mode agents prepare isolated candidates in parallel; protected proposals publish against a current verified base. See the team-mode reference.

Maintaining the CRM needs the wiki folder on the machine where the helpers run (Claude Code or Codex with local files). A cloud sandbox that only receives copies of single files cannot hold the lock and must not be used to maintain the canonical wiki.

## Workflow

For initialize, ingest, maintain, repair, migrate, release, or lint operations:

1. For a new wiki, complete the identity/content-policy interview and obtain confirmation of the hash-bound proposal before taking a lock or writing any target file. For an existing wiki whose identity files are missing or invalid, take the lock, run a baseline audit, conduct the same no-write proposal discussion, and apply the confirmed proposal with `scripts/apply_identity.py`; replacement of existing identity files requires explicit confirmation.
2. Acquire the exclusive wiki lock as specified above and keep only its private runtime token file until release. For a standalone initialization-only request, use the internal-lock wrapper described above and skip the remaining workflow after its successful released result.
3. Run `scripts/lint_wiki.py --check-only` before the first maintenance change. This baseline mode performs no repair and writes no lint report. Report pre-existing blockers separately from planned changes; never describe the whole wiki as releasable merely because the current batch is valid.
4. Inspect the locked target and classify the request as initialize, ingest, maintain, repair, migrate, quality-review, cleaning-review, release, lint, or CRM. A CRM request follows the CRM layer section and its references; steps 6 to 16 apply to knowledge pages and sources.
5. Before invoking initialization, construct and check one complete argument map containing target, explicit title, topic boundary, wiki-language code, language label, all three review intervals, the confirmed identity-plan path and hash, and either the private runtime token file or the standalone wrapper's public owner label. For a new wiki that will be curated further under the same lock, invoke `scripts/init_wiki.py` through `run_locked.py` and explicitly pass `--title <title>`, `--topic <topic>`, `--wiki-language <code>`, `--wiki-language-label <label>`, `--quality-review-days <days>`, `--cleaning-review-days <days>`, `--snapshot-warning-days <days>`, `--identity-plan <plan>`, and `--expect-identity-sha256 <confirmed-hash>`, plus `--storage-path-prefix <prefix>` when the wiki lives in OneDrive or SharePoint (it enables the storage path-length checks). Do not start the helper with a partial argument map. The identity plan must have been rendered with the same `--wiki-language` code; a mismatch is refused before any file is written. `--description` is an alias for `--topic`. As defensive recovery for a faulty caller, the script derives a title from the topic or target instead of failing when `--title` is omitted, reports `title_source: derived-from-topic-or-target`, and requires that derived title to be reviewed before further curation. The script creates only missing files and records the confirmed identity, content policy, language, and reminder intervals.

   For a standalone initialization-only request, invoke `scripts/initialize_wiki.py` internally with the same complete content arguments, identity plan and expected hash plus `--owner <agent-or-run-id>` and no lock token. It constructs and validates the full wiki outside the target and commits only the complete released result. A successful result must report `state: initialized`, `lint_valid: true`, version, release ID, manifest hash, and `lock_released: true`; a failure leaves no partial wiki content. Never expose or ask the user to handle an internal token.
6. Resolve every new source only from its current host attachment path or an exact path/directory explicitly selected by the user. Check that exact path before conversion. If it is missing, inaccessible, or no longer attached, return a concise `source_missing` or `source_access_denied` result and ask the user to reattach it or select its location; do not run a broad `find`, recursive home-directory scan, Spotlight/system search, or filename hunt. Convert the verified source to faithful Markdown in a temporary workspace outside the target wiki. Preserve its original language, headings, lists, tables, quotations, dates, identifiers, and page or slide markers when recoverable. Do not translate, summarize, or improve the source during extraction.
7. Run `scripts/validate_extraction.py` on the staged Markdown before registration. A rejected extraction never enters the wiki. An extraction with warnings is registered with `register_source.py --status partial` unless the user reviews the concrete warnings and explicitly confirms active use, which is passed as `--confirm-extraction-warnings`; without either option registration stops with `extraction_review_required`. Capability probing for optional converters must return structured availability instead of a traceback; absence of one optional converter is not a failure when a suitable fallback exists.
8. Register each converted Markdown file with the bundled `scripts/register_source.py` through `run_locked.py`, including `--content-language <source-language-or-und>`. The original remains outside the wiki. Use `--original-ref` for a portable relative or logical reference, URL, SharePoint item identifier, or other stable identifier. Use `--original-file` only at runtime when the exact verified original is still available for hashing; that machine path must never be stored. Never create or read `<wiki>/sources/raw/`, never copy an original binary below `sources/`, and never assume that a registered Markdown source implies continued access to its original file.
9. Process batches sequentially. For each non-duplicate source, read the wiki rules, content policy, index, relevant existing pages, and the new source before planning updates.
10. Read both `schema/CLUSTERS.md` and `schema/CONCEPTS.md` before synthesis. Treat them as separate contracts:
   - clusters control navigation, graph grouping, curation boundaries, and optional directories;
   - controlled concepts provide preferred terminology, multilingual aliases, and deterministic retrieval expansion;
   - a concept assignment never moves a page, and a cluster label never becomes a search synonym merely because it exists.
11. When a new wiki has no confirmed clusters, or maintenance reveals material overlap, overloaded clusters, or unassigned durable topics, present a concise cluster proposal before reorganizing the synthesis. For each proposed cluster include a stable ID, label, purpose, inclusion and exclusion boundary, representative pages, overlaps, and one color from the palette declared in `schema/CLUSTERS.md`. Separately propose new or changed concepts when sources introduce durable terminology, ambiguity, synonyms, translations, or concept relationships. For each concept include its stable ID, preferred label in the wiki language, definition, aliases, and optional broader or related concepts. Invite the user to confirm, rename, merge, split, recolor, relate, or reject proposals. Do not activate proposals until the user confirms them unless the user explicitly delegates the decision.
12. After confirmation, verify lock ownership and update the applicable schema plus affected page frontmatter. A page may have several `clusters` and `concepts`; use `primary_cluster` only when a cluster controls its directory. A confirmed cluster or category change may move a wiki page to the cluster's declared `Directory`. Run the bundled `scripts/move_wiki_page.py` through `run_locked.py` without `--apply` to produce the impact plan containing the old and new vault-relative paths, affected inbound links and index entries, per-file precondition hashes, `preview_sha256`, and possible external-link breakage. Show that preview and obtain user confirmation. After confirmation, verify the lock again and rerun with `--apply --expect-preview-sha256 <approved-hash>`; the helper creates its own targeted recovery snapshot before moving. Then rebuild the graph, run the linter, and record the migration. A mismatched preview hash is stale and must be regenerated. The helper refuses overwrites, preserves history, and rewrites internal wikilinks; it cannot repair links stored outside the wiki. Never move a page because only its concept assignment changed. Never move source Markdown under `sources/` as a side effect of categorization. Preserve stable cluster and concept IDs during ordinary maintenance and propose migrations rather than silently renaming them.
13. Never let an agent or delegated worker write a new or revised page directly into the canonical target. Draft every affected `wiki/*.md` file under one external temporary staging directory using the same relative path it will have in the wiki. Run `scripts/page_batch.py plan` through `run_locked.py`; it checks UTF-8, extreme line flattening, frontmatter, protected `human:keep` blocks, staged hashes, and target preconditions. Apply only the same plan hash with `page_batch.py apply`. The apply helper constructs a complete temporary wiki mirror, inserts the full batch there, builds the graph, and runs no-write lint. Any invalid staged page causes `validation_failed` with zero canonical writes. Only a fully valid batch receives a targeted snapshot and atomic per-file commit. A delegated agent may return drafts or populate this staging directory, but cannot receive the lock capability or canonical target as a write destination.
14. For non-page changes not already protected by a hash-bound apply helper, run `scripts/snapshot_wiki.py` through `run_locked.py` before changing the existing wiki. A confirmed `frontmatter_actions.py apply`, `restore_wiki.py apply`, `move_wiki_page.py --apply`, `apply_identity.py`, or `page_batch.py apply` creates its own targeted pre-change snapshot and satisfies this invariant for those exact files. Never snapshot or copy original input files into the wiki unless the user explicitly asks; a file the user asks to attach to a record is such a request and goes into the file store through the transaction.
15. Update the staged wiki batch incrementally:
   - extend or revise existing pages when the concept already exists and translate new synthesis into the language declared by `schema/WIKI_PROFILE.md`;
   - create a new page only when it has a distinct durable subject;
   - preserve all required frontmatter fields when editing a page; an index must retain `sources: []` even though it does not cite sources itself;
   - add vault-relative `[[wikilinks]]`, source IDs, and confirmed concept IDs;
   - generate globally unused claim IDs through the locked helper (`claim_id.py --count <n>` for several at once) and wrap every material assertion in the exact HTML-comment claim-block grammar from the wiki contract; never substitute an Obsidian callout;
   - give every active, disputed, or superseded claim one or more `source_id@locator` entries and ensure those source IDs also appear in page frontmatter;
   - preserve a claim ID only while the assertion keeps the same meaning; connect replacements and contradictions explicitly rather than silently rewriting history;
   - make contradictions, uncertainty, scope, and dates explicit;
   - preserve blocks enclosed by `<!-- human:keep -->` and `<!-- /human:keep -->` exactly;
   - never delete sources or pages without explicit approval; mark superseded pages instead.
16. Update `wiki/index.md`, `meta/changes.md`, and `meta/questions.md` as needed through an applicable validated batch or transaction. Record confirmed identity, content-policy, cluster, and concept changes and affected pages in the change log.
17. When the wiki has a CRM layer, first run `scripts/crm_build.py` through `run_locked.py` to regenerate `graph/crm/`. Run `scripts/build_graph.py` through `run_locked.py` to regenerate `graph/index.html`, `graph/graph.json`, and the static reading views below `graph/pages/`. It stages the complete output and publishes the graph index last. The graph uses Markdown files as nodes, wikilinks as edges, and confirmed navigation clusters as visible structure nodes and filters. Controlled concepts stay retrieval metadata rather than graph-layout instructions. A normal node click opens a generated HTML reading view through a URL relative to `graph/index.html`. Source views preserve uncertain line boundaries, render page/slide markers as anchors instead of raw comments, fall back to preformatted fragments for malformed tables, and identify layout limitations. Wiki claim views retain claim ID, status, and relative links to source locators. Every view links the canonical Markdown file and graph using only relative routes and is never a second editable source of truth.
18. Run `scripts/lint_wiki.py --fix-safe` through `run_locked.py` for the final validation pass. The linter uses the canonical flat frontmatter parser and validates the confirmed SOUL/content-policy schema. It fails clearly on unsupported YAML rather than guessing. It may repair the mandatory missing `sources: []` only on a page of type `index`; neither mode may invent sources, clusters, concepts, identity, policy, or content metadata. Repair all remaining errors introduced by the current run and regenerate the graph when links or pages changed.
19. Publish exactly one release through `run_locked.py` with a new stable `--operation-id`, `--expect-current-version <observed-version>`, one bump, and summary. The release helper reruns strict lint, enforces the version precondition, updates `WIKI_VERSION`, appends the release log, generates quality status, hashes controlled files, and writes `meta/manifest.json` last. Repeating the same operation ID returns the same current release without another version bump; reusing it after a later release is refused. Never rerun the release helper to display omitted fields: inspect its complete result, then use the read-only verifier. Use patch for ordinary content, minor for a compatible schema expansion, and major for an intentionally breaking contract or complete wiki-language migration. If release fails, retain the lock while repairing or restoring the snapshot.
20. Verify the published manifest read-only while still holding the claim (`verify_release.py` through `run_locked.py`, which passes the owning token; without it the verifier reports `wiki_busy`). Only after successful verification release the lock using its private runtime token file and remove that token file. Report sources added or skipped, pages created or changed, confirmed identity/content policy, cluster and concept decisions, contradictions or open questions, graph statistics, release ID, `WIKI_VERSION`, manifest SHA-256, final lint result, quality-review and cleaning-review state with next due dates, and successful lock release.

## Other maintenance operations

Each operation below is specified completely in [references/maintenance-operations.md](references/maintenance-operations.md); read its section before acting on it.

| Operation | Rule to keep in mind |
|---|---|
| Frozen read-only knowledge-skill export (`export_wiki_skill.py`) | Read-only and without a lock; refuse while `.llmwiki.lock` exists or the release does not verify; run it first with `--dry-run` to tell the user the record counts, stored files, size and the description it would use (built from the wiki title and topic unless the user gives `--description`), then export; the result is a skill folder plus `.skill` file and checksum; never copy maintenance helpers; disclose personal data in CRM records before exporting and offer `--exclude-crm`. |
| Deterministic metadata transactions (`inventory_wiki.py`, `frontmatter_actions.py`, `restore_wiki.py`, `describe_actions.py`) | Inventory first; plan, show paths and plan hash, confirm, apply with `--expect-plan-sha256`. |
| Synchronized storage (`resolve_conflict_copy.py`) | A conflict copy is resolved by plan and confirmed decision; `sync_in_progress` means wait; `hydration_required` means ask before downloading. |
| Navigation indexes | Generated by `build_graph.py`; never hand-edit one. |
| Trust tiers (`verify_pages.py`) | Record a `human:` review only after the named person confirmed it. |
| Page expiry (`stale_after`) | Set only when the source states an end date; a passed date is disclosed, never acted on. |
| Observations (`register_source.py --source-type observation`) | Needs a named actor and occasion; never a licence to record beliefs. |
| Adopting an existing collection (`adopt_directory.py`) | Registers evidence, not knowledge. |
| Open Knowledge Format (`report_okf.py`, `export_okf_bundle.py`) | An interoperability view of a verified release; CRM records are not carried. |
| Quality and cleaning reviews (`record_quality_review.py`) | Record only reviews actually performed; a due date never authorizes changes. |
| Conversion routing | Faithful Markdown from the exact attached file; OCR only when necessary. |
| Complete wiki-language migration (`plan_language_migration.py`) | A full, confirmed migration with a major release; CRM labels are translated, CRM values never. |

## Safety and quality boundaries

- Never modify an original source.
- Never search an entire home directory or machine for a missing source and never invent a local source path.
- Never create `sources/raw/` or store original/binary input below `sources/`; register only faithful Markdown.
- Never invent missing facts, citations, document versions, or source identifiers.
- Do not silently replace a conflicting claim with the newest wording. Record the conflict and its sources.
- Keep source extraction separate from wiki synthesis.
- Prefer exact source traceability over polished but unsupported prose.
- Never apply a stale frontmatter, move, or restore plan and never substitute a newly computed hash for the hash the user approved.
- Preserve compatible frontmatter extensions unless their removal or migration was explicitly approved.
- Do not describe the wiki as perfect merely because lint passes. State remaining warnings, partial extractions, and unresolved questions.
- Never write a record file by hand, never edit `graph/crm/`, and never append to the CRM event log outside a transaction.
- Never sum amounts in different currencies, never convert money through floating point, and never translate CRM values.
- Never send e-mail, call an external system, or run arbitrary code from a workflow; write the draft or request file instead and say so.
- Before a frozen export or any copy that leaves the wiki, tell the user how many CRM records with personal data it contains.

The operation is complete only when the target remains readable as ordinary Markdown, the index reflects the pages, active claims cite registered sources, no broken wikilinks remain, the interactive graph represents the current files and links, the linter reports zero errors, and `meta/manifest.json` is the last successfully published release boundary.
