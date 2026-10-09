# Maintenance operations

These sections belong to `SKILL.md` and are binding in the same way; they live here so the skill body stays short enough to survive context compaction. Read the section that matches the request completely before acting on it. Relative helper paths mean `<skill-root>/scripts/...`.

## Contents

- [Frozen read-only knowledge-skill export](#frozen-read-only-knowledge-skill-export)
- [Deterministic metadata transactions](#deterministic-metadata-transactions)
- [Synchronized storage](#synchronized-storage)
- [Navigation indexes](#navigation-indexes)
- [Trust tiers](#trust-tiers)
- [Page expiry](#page-expiry)
- [Observations](#observations)
- [Adopting an existing collection](#adopting-an-existing-collection)
- [Open Knowledge Format](#open-knowledge-format)
- [Quality and optional cleaning review cycle](#quality-and-optional-cleaning-review-cycle)
- [Conversion routing](#conversion-routing)
- [Complete wiki-language migration](#complete-wiki-language-migration)

## Frozen read-only knowledge-skill export

Use this mode when the user wants one released wiki state as an explicit Claude Cowork or Codex knowledge skill. It is a read-only operation on the canonical wiki and therefore takes no maintenance claim. It must nevertheless refuse an export while `.llmwiki.lock` exists, while the release manifest is missing or invalid, or when any released file differs from the manifest.

The active agent invokes the bundled exporter internally; this command is an implementation contract for the agent, never a step delegated to the user:

```text
<python> <skill-root>/scripts/export_wiki_skill.py --target <wiki> --skill-name <lowercase-hyphenated-name> --dry-run [--output-dir <destination>] [--description <trigger-description>] [--exclude-crm] [--allow-large]
<python> <skill-root>/scripts/export_wiki_skill.py --target <wiki> --output-dir <destination> --skill-name <lowercase-hyphenated-name> [--description <trigger-description>] [--exclude-crm] [--allow-large]
```

Run the first form before exporting. `--dry-run` verifies the release exactly as the export does and reports what the package would hold (version, release, file count, bytes per area, CRM counts, the personal data warning, the size check, and the description) without writing anything, not even the output directory, so `--output-dir` is optional with it. A package above the upload limit is reported there with `export_would_be_refused` and exit code 0. Without `--description`, the exporter builds the trigger description from the wiki title, the Purpose section of `WIKI.md` (or the confirmed purpose in `SOUL.md`), and the maintained language; the dry run and the export both report the description they used.

The exporter verifies the same manifest before and after copying. It creates a skill folder and a deterministic `.skill` package whose archive root is that folder, with the installable skill directory as its single archive root. The package contains:

- `SKILL.md` with only `name` and `description` in frontmatter and a strict knowledge-only workflow;
- the complete released wiki under `references/knowledge/`, including its original `meta/manifest.json`;
- `references/SNAPSHOT.json` with the release identity and immutable/read-only flags;
- only the entrypoints `scripts/verify_knowledge.py`, `scripts/search_knowledge.py`, `scripts/assess_quality.py`, and `scripts/identity_status.py` plus the non-executable `frontmatter_contract.py`, `wiki_filters.py`, and `trust_contract.py` library modules; when the release has a CRM layer, also `scripts/query_records.py` with the library modules `crm_contract.py` and `crm_filters.py`. All are bound to their own bundled snapshot, perform no writes, and cannot select or modify another wiki. The search entrypoint supports validated read-only metadata filters, the quality helper distinguishes technical validity, confirmed identity/content policy, quality-review age, cleaning-review age, open questions, and frozen-snapshot age, and the record query answers find, aggregate, get, search, and timeline questions about the bundled records.

A release with a CRM layer contains personal data. Before exporting, tell the user how many records of which objects the package will carry; the dry run and the export report them with a `personal_data_warning`. When the recipients may not see those records, export with `--exclude-crm`: `records/`, `meta/crm-events/`, `meta/crm-runs/`, `meta/crm-workflow-state.json`, and `graph/crm/` stay out, the original manifest is bundled unchanged, `references/SNAPSHOT.json` lists the excluded prefixes, and the bundled verifier treats exactly those as deliberately absent. `schema/crm/` stays in the package even then, and its settings and roles may still name members or e-mail addresses. The count and the exclusion cover CRM data only: personal data inside registered sources and knowledge pages is neither counted nor excluded. No export log is kept either. When a later erasure may have to be followed up, tell the user to note where each export went, because an erasure in the canonical wiki never reaches a copy that was already handed out.

Skill uploads to claude.ai, Claude Cowork, and the API accept at most 30 MB of files, uncompressed and combined; the exporter refuses a larger package (exit 6) unless `--allow-large` confirms a host that installs skill folders directly, such as Claude Code or Codex. Before packaging, every bundled entrypoint runs once inside the new skill folder in isolated mode.

Never copy the maintenance, ingest, migration, cleaning, release, export, or lock helpers into the exported skill. Never put an absolute source path, lock token, secret, or transient lock file into it. Do not export an unreleased working state. The canonical wiki remains the only maintainable source; changes require maintaining and releasing that wiki and generating a new skill snapshot. Do not patch an exported snapshot in place.

The result of this mode is always a complete skill folder plus its uploadable `.skill` file and checksum, never merely a loose data directory, ordinary `.zip`, or standalone Python program. After installation, the user asks the exported skill normal knowledge questions; that skill runs its verification and retrieval helpers internally and must never ask the user to run them.

After export, report the wiki version, release ID, original manifest SHA-256, `.skill` SHA-256, skill folder, package path, and that self-maintenance is disabled. The `.skill` file is the installation artifact for Claude Cowork; the folder is useful for local inspection or hosts that install unpacked skills.

## Deterministic metadata transactions

The existing curation workflow remains authoritative. Use the new metadata helpers only when the request involves frontmatter inspection, bulk schema work, value normalization, wikilink cleanup, or recovery:

- Run locked `scripts/inventory_wiki.py` before any broad frontmatter migration or cleaning proposal. Discuss missing fields, type drift, noncanonical names, and compatible extensions; parser errors block planning.
- Use `scripts/frontmatter_actions.py plan` with a validated selector and action and write its plan only to the active temporary workspace. Show the exact selected paths, material before/after differences, changed/skipped counts, destructive flag, and `plan_sha256`.
- Obtain explicit confirmation for deletion, overwrite, rename, merge, value normalization, wikilink deduplication, or source-metadata changes. Apply only the same plan with `--expect-plan-sha256`; pass `--confirm-destructive` only after that approval. `stale_plan` means zero writes and requires inspection plus a new plan, never a bypass.
- Treat the targeted snapshot created by a successful frontmatter apply as the required pre-change snapshot for that action. On `partial_failure`, retain the lock and repair or run a confirmed restore before release.
- Use `scripts/restore_wiki.py list`, then `plan`, then `apply` for recovery. Show selected paths and hashes, require confirmation, and apply with both the expected plan hash and `--confirm-restore`. A restore never deletes files absent from an older snapshot and always creates a recovery snapshot first. The command synopses are in `references/frontmatter-operations.md` under snapshots and targeted restore.
- Use `normalize_values` only on fields whose semantics permit deterministic normalization. Do not normalize stable IDs, claim text, source titles or locators, hashes, `original_ref`, or protected human material. Use `dedupe_wikilinks` for alias- and subpath-preserving link cleanup.

Run `scripts/describe_actions.py` when a host needs a machine-readable description of the complete maintenance surface. It is descriptive and grants no mutation authority.

## Synchronized storage

Treat a OneDrive or SharePoint folder as a second writer on the wiki, never as a passive transport. The contract section on synchronized storage is binding; the operational rules are:

- A `sync_artifacts_present` state means a conflict copy exists. Do not curate both files and do not delete either. Run locked `scripts/resolve_conflict_copy.py plan`, show the user both sides with their hashes, sizes and modification times, take one decision per copy, then apply the confirmed plan hash with one `--decision` per copy (`run_locked.py` appends `--lock-token` itself):

  ```text
  <python> <skill-root>/scripts/run_locked.py --token-file <private-runtime-file> --helper resolve_conflict_copy.py plan --target <wiki> --output <temporary-plan-file>
  <python> <skill-root>/scripts/run_locked.py --token-file <private-runtime-file> --helper resolve_conflict_copy.py apply --target <wiki> --plan-file <temporary-plan-file> --expect-plan-sha256 <approved-hash> --decision <copy-path>=<keep-original|keep-copy|keep-both> [--decision <copy-path>=<choice> ...]
  ```

  Use the copy's path exactly as the plan lists it. An undecided copy stops apply with `decision_required`, and nothing is written. Rebuild the graph, lint, and release afterwards; a copy kept with `keep-both` has to be curated first, or lint refuses the release. The details are in `references/frontmatter-operations.md`.
- An `invalid_wiki` state that lists `storage_refused` names a file whose name the storage layer will not accept, for example `wiki/.lock`. It exists on this disk and would never reach the storage, so the wiki is not repaired by re-releasing it. Rename or remove the file after checking what it holds, then rebuild, lint, and release. `.lock` has no relation to the maintenance lock `.llmwiki.lock`, which lives at the wiki root and is not a refused name.
- A `sync_in_progress` state is a transfer still running. Wait and re-verify; never repair it, and never report it as a damaged wiki.
- A `hydration_required` state means released files hold no local content. Report the file count and estimated download volume and ask before proceeding. Do not pass `--allow-hydration` on your own initiative.
- Operating-system artifacts are expected noise. Report them once if useful and otherwise ignore them; never propose deleting a user's `.DS_Store` as if it were smuggled source material.
- After a release on an apparently synchronized folder, repeat the reported persistence statement. The release is durable locally; whether the client has uploaded it is unknown, so never tell the user that colleagues can already see it.
- When acquiring the claim reports a storage advisory, pass it on once. Two machines that are not yet reconciled each see only their own claim, so do not maintain the same wiki from two machines at the same time, and never force-override a claim held by another machine on age alone.

## Navigation indexes

Each populated subdirectory of `wiki/` carries a generated `index.md`. Never hand-write or hand-edit one: `scripts/build_graph.py` produces them, and an edit is overwritten on the next build. When the linter reports a stale, missing, or orphaned directory index, rebuild the graph rather than repairing the file.

A page counts as listed when the index responsible for it lists it, so a root index may link to directory indexes instead of to every page. Prefer that shape for a growing wiki and say why when you change it: the root then stops growing with the wiki. A root that still lists everything remains valid; do not migrate one without asking.

## Trust tiers

Record who produced and who confirmed individual pages, so a reader can tell a reviewed page from an unreviewed one. Include `generated_by` and `generated_at` on pages you author, using your own agent identifier in the form `agent/<name>`.

Use locked `scripts/verify_pages.py plan` to show which pages a confirmation would cover and the tier it would record, then apply the confirmed plan hash. Pass `--user-confirmed-human-review` only after the named person has actually confirmed they reviewed those pages; recording agent work as `human:` is prohibited regardless of how the request is phrased. Report the resulting distribution rather than implying the wiki as a whole was reviewed.

## Page expiry

A page whose content has an actual end date may carry the optional frontmatter field `stale_after`, an ISO date or instant: a price list valid to year end, a certification, a regulation superseded on a known day. Set it only when the source itself establishes that date. Do not invent one, and do not add the field to a page merely because it feels old - the wiki-wide quality policy governs the review rhythm, and inventing an expiry is worse than having none.

A passed date is a disclosure, not a verdict. Report it, never act on it: expired pages stay in the release, stay in indexes, stay findable, and are never deleted, hidden, or rewritten on that basis. The linter counts pages with and past their expiry, the release publishes those counts, and the reading skill names them in an answer. When a user asks about an expired page, offer to re-check the source, not to remove the page.

## Observations

Not all evidence is a document. Register knowledge that arose during the work - a decision in a meeting, the outcome of a run, something the user established - with `scripts/register_source.py --source-type observation`, adding `--observed-by <actor>` and `--occasion "<what produced it>"`. Both are required and are refused for any other source type. The actor uses the trust-tier notation: `human:<id>`, `agent/<name>`, `process:<id>`.

Write the observation's Markdown as faithfully as any extraction: what was established, by whom, on what occasion, and nothing beyond it. An observation is ordinary evidence and a page built on it cites it like a PDF. It is not a licence to record what you believe: register it because a named actor observed something on a named occasion, and when nobody can be named, there is no observation to register.

## Adopting an existing collection

When a user already keeps an Obsidian vault, a documentation folder, or an older wiki, use locked `scripts/adopt_directory.py plan` against that directory rather than asking them to hand over files one at a time. It reads only; it reports each Markdown file with its own title, hash, blockers, and notes, plus the non-Markdown files it skipped.

Show that report, let the user choose, then apply the confirmed plan hash with one `--accept <path>` per chosen file. Any file that changed since the plan stops the whole selection, and a recovery snapshot precedes the first write.

Say plainly what adoption does and does not do: it registers evidence, not knowledge. Each accepted file becomes a registered source of type `adopted`; nothing is created under `wiki/`, and nothing is asserted. Building pages from that material is the same curation as always, with claims and source locators. Never present a completed adoption as a populated wiki.

## Open Knowledge Format

Run locked `scripts/report_okf.py` only when the user asks about interoperability or an OKF migration. Never make OKF the native contract or invent its recommended metadata.

An OKF bundle is a first-class deliverable of this skill, not a footnote. Run `scripts/export_okf_bundle.py` whenever the user wants their knowledge in an interoperable form. It takes no lock token, requires an empty destination outside the wiki, refuses to export anything but a verified release, and validates the bundle it wrote before reporting success.

The bundle carries the registered source extractions by default so it can answer its own citations; pass `--no-sources` only when the user asks for concepts alone and say what that costs. Before exporting, tell the user what the bundle cannot carry: claim locators, the release manifest, snapshots, `SOUL.md`, concept worlds, and clusters. After exporting, name the release version it was bound to, report the concept and source counts, list the recommended fields that had no basis in the wiki, and repeat that editing the bundle does not change the wiki. Never fill a missing field to make a bundle look complete.

## Quality and optional cleaning review cycle

Treat technical validation, semantic quality review, and cleaning review as separate events:

- strict lint checks deterministic structure and runs for every release;
- a quality review checks semantic duplication, claim support and applicability, stale or inconsistent statuses, unresolved questions, partial extraction, wiki-language consistency, page boundaries, cluster fitness, and controlled-concept quality;
- a cleaning review examines removable duplication, obsolete generated material, superseded content, unused concepts or clusters, and reorganization opportunities. It is optional and begins with a preview. Never delete sources, pages, claims, history, or protected human content merely because cleaning is due.

Use `schema/QUALITY_POLICY.md` as the user-controlled reminder policy. Changing an interval is a maintained wiki change: verify the lock, snapshot first, update the policy, lint, release, and report the new schedule. A due date is advisory, not permission to mutate content.

When an older compliant wiki lacks the current quality or identity files, treat this as a compatible schema upgrade. Preserve its existing title, topic boundary, and maintained language. Confirm the three review intervals and conduct the full identity/content-policy interview. Apply the hash-bound identity proposal, add other missing files without overwriting compatible content, update root navigation, rebuild the graph, lint, and publish one minor release. Until that release exists, readers report the missing quality or identity state rather than inventing it.

Record a review only after the active agent actually completed it. Invoke `scripts/record_quality_review.py` internally with the owned lock, `--kind quality|cleaning`, `--outcome passed|attention-needed|not-applicable`, and a concise factual `--summary`. Do not record semantic quality merely because lint passed, and do not record cleaning merely because candidates were listed. If findings require changes, discuss destructive or meaning-changing proposals with the user, implement only approved changes, then record the truthful outcome and publish a new release so readers receive the updated status.

## Conversion routing

Use the most reliable local extraction capability available for the format. Plain text and Markdown need no model-based transformation. For PDF, Office, HTML, images, audio, or scanned material, use an available trusted reader or converter such as MarkItDown or Docling. Use OCR only when necessary and record extraction limitations.

The converter may read only the exact attachment or user-selected path. Never search protected or unrelated filesystem areas to recover a guessed filename. Perform conversion in a temporary workspace and pass the resulting `.md` file to the registrar. The canonical `sources/` directory is flat and contains only `src-<hash>-<slug>.md`; a `sources/raw/` directory and original PDF, Office, image, audio, or archive files are invalid.

When a previously registered Markdown extraction is incomplete, use the content that is actually present and disclose the limitation. If the missing pages require the original, ask the user to reattach or select that original. Do not manufacture a likely original path from `original_ref`, the source title, a prior machine path, or the wiki directory. Do not register a source as successfully extracted when important content could not be read. Preserve partial output only with `status: partial` and a clear extraction note.

## Complete wiki-language migration

A later language change is allowed but always expensive and explicit. It affects the maintained wiki, not the faithful source layer.

1. Acquire the maintenance lock and run `scripts/plan_language_migration.py` through `run_locked.py` with the private runtime token file, target, destination language code, and label.
2. Show its full page and claim impact and obtain explicit confirmation. Keep the lock while the user decides.
3. Verify the lock and snapshot the current wiki. Do not change `sources/*.md`, source IDs, claim IDs, locators, page IDs, or default file paths.
4. Translate every maintained page title, description, body, and claim text; translate index labels, cluster labels and descriptions, preferred concept terms and definitions, and user-facing root navigation. Preserve `human:keep` blocks exactly and report any resulting deliberate language exception. Retain useful old-language terms as aliases.
5. Update every page's `language` field and finally `schema/WIKI_PROFILE.md`. Do not change only the profile or publish a partly translated active wiki.
6. Render the identity files again: run `scripts/plan_identity.py` with the unchanged confirmed identity and content-policy values and the new `--wiki-language`, show the proposal, and after confirmation apply it with `scripts/apply_identity.py --confirm-replace` under the lock. Only the generated headings and explanatory prose change. The profile must already carry the new language, or the apply helper refuses the proposal.
7. Rebuild index, graph, and reading views; the generated directory indexes take their language and prose from the updated profile. Run the normal release workflow with `--bump major`; verify the released manifest; then release the lock.
