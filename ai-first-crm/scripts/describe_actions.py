#!/usr/bin/env python3
"""Return the machine-readable action catalog of the maintenance skill."""

from __future__ import annotations

import sys as _sys

_sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders

import json


# Each entry mirrors the argparse definition of its helper. A parameter's name is the
# argparse dest, its flag is the option that sets it, required is what argparse enforces,
# and default is the argparse default where one exists. Conditions that a helper checks
# only at run time are stated in the parameter's type or description.


SELECTOR = {
    "parameter": "selector_json",
    "kind": "all | paths | filter",
    "forms": [
        {"kind": "all"},
        {"kind": "paths", "paths": ["wiki/topics/example.md"]},
        {
            "kind": "filter",
            "combinator": "AND",
            "conditions": [{"property": "status", "operator": "equals", "value": "active", "case_sensitive": False}],
        },
    ],
    "kind_when_omitted": "filter",
    "condition_fields": ["property", "operator", "value", "case_sensitive"],
    "conditions_per_filter": "1 to 100",
    "operators": ["exists", "not_exists", "equals", "not_equals", "contains", "not_contains", "starts_with", "ends_with", "is_empty", "is_not_empty", "is_list", "is_string", "in_path"],
    "virtual_properties": ["__path", "__folder", "__filename", "__extension", "__trust_tier"],
    "combinators": ["AND", "OR"],
}

INVOCATION = {
    "run_locked": "<python> <skill-root>/scripts/run_locked.py --token-file <private-runtime-file> --helper <script> <subcommands> <flags>",
    "direct": "<python> <skill-root>/scripts/<script> <subcommands> <flags>",
    "either": "run_locked while the agent holds its own claim and direct otherwise, as the action description says",
    "lock_token": "run_locked.py appends --lock-token from the private token file, so run_locked actions do not list lock_token and the agent never reads, prints, or passes the token",
}

FIELDS = {
    "writes": "true when the action can create, change, or remove files inside the target wiki, including the claim files of the lock actions, while files written outside the wiki and the lease refresh that run_locked calls make on the side do not count",
    "destructive": "true when the action can delete or overwrite user data in the wiki",
    "preview": "true when the result is a preview or plan to show the user before an apply step",
    "snapshot": "true when the helper itself writes a recovery snapshot under meta/history/ before it changes existing files",
    "confirmation_required": "true when the action may run only after the user explicitly confirmed exactly what the preceding plan or preview showed",
    "lock_required": "true when the agent must hold its own claim on the target wiki",
    "invocation": "run_locked, direct, or either, as described under invocation",
}


def param(name: str, type_: str, required: bool, description: str = "", *, flag: str = "", default: object = None, repeatable: bool = False) -> dict[str, object]:
    entry: dict[str, object] = {"name": name, "flag": flag or "--" + name.replace("_", "-"), "type": type_, "required": required}
    if default is not None:
        entry["default"] = default
    if repeatable:
        entry["repeatable"] = True
    if description:
        entry["description"] = description
    return entry


def action(
    action_id: str,
    helper: str,
    description: str,
    parameters: list[dict[str, object]],
    *,
    invocation: str,
    writes: bool,
    destructive: bool,
    preview: bool,
    snapshot: bool,
    confirmation_required: bool,
    lock_required: bool,
) -> dict[str, object]:
    return {
        "id": action_id,
        "helper": helper,
        "description": description,
        "parameters": parameters,
        "writes": writes,
        "destructive": destructive,
        "preview": preview,
        "snapshot": snapshot,
        "confirmation_required": confirmation_required,
        "lock_required": lock_required,
        "invocation": invocation,
    }


def target(description: str = "the wiki directory") -> dict[str, object]:
    return param("target", "runtime path", True, description)


def plan_file(description: str = "the plan written by the matching plan step, kept outside the wiki") -> dict[str, object]:
    return param("plan_file", "runtime path", True, description)


def expect_plan_sha256() -> dict[str, object]:
    return param("expect_plan_sha256", "sha256", True, "plan_sha256 of exactly the plan the user confirmed")


def output(description: str = "plan file to write outside the wiki", required: bool = True) -> dict[str, object]:
    return param("output", "runtime path outside the wiki", required, description)


def token_pair() -> list[dict[str, object]]:
    return [
        param("token_file", "runtime path", False, "private token file written by acquire; one of token_file or lock_token is required, and the agent always passes token_file"),
        param("lock_token", "lock token", False, "the other member of the pair of which one of token_file or lock_token is required, never passed by the agent because it never handles the token itself"),
    ]


def query(*specific: dict[str, object]) -> list[dict[str, object]]:
    return [
        target(),
        param("release", "boolean", False, "read the verified published release instead of the locked working state; the helper then runs directly, not through run_locked.py, because it accepts exactly one of lock_token and release"),
        param("allow_hydration", "boolean", False, "with release, verify even when released files must be downloaded first, only after the user agreed"),
        param("now", "ISO-8601 instant", False, "moment at which relative dates are evaluated, the current time when omitted"),
        param("me", "workspace member UUID", False, "the member that @me stands for in filters"),
        *specific,
    ]


IMPORT_READING = [
    param("encoding", "string", False, "force a text encoding such as cp1252"),
    param("sheet", "string", False, "XLSX sheet name or 1-based number, the first sheet when omitted"),
    param("delimiter", "one character or tab", False, "force the CSV delimiter, such as a semicolon, comma, pipe, or tab"),
    param("date_format", "DMY | MDY | YMD", False, "order of slash or dash dates such as 01/02/2024"),
    param("decimal", "comma | dot", False, "decimal separator of numbers written as text"),
    param("time_zone", "IANA zone name", False, "zone for times without offset, the CRM settings zone when omitted"),
]


CATALOG = {
    "format": "lmwiki-action-catalog/1",
    "skill": "ai-first-crm",
    "invocation": INVOCATION,
    "fields": FIELDS,
    "selector": SELECTOR,
    "actions": [
        # Identity and initialization
        action(
            "plan-identity",
            "plan_identity.py",
            "Render the hash-bound SOUL.md and content-policy proposal from the confirmed interview answers without touching any wiki.",
            [
                param("input", "runtime path", True, "temporary JSON holding identity and content_policy"),
                param("wiki_language", "BCP-47 code", True, "maintained wiki language that the generated headings and prose follow and that the proposal hash binds"),
                param("output", "runtime path", False, "temporary path for the immutable proposal"),
            ],
            invocation="direct", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=False,
        ),
        action(
            "apply-identity",
            "apply_identity.py",
            "Apply exactly the confirmed identity proposal to an initialized wiki after snapshotting SOUL.md and schema/CONTENT_POLICY.md.",
            [
                target(),
                param("plan_file", "runtime path", True, "the confirmed identity proposal written by plan-identity"),
                param("expect_proposal_sha256", "sha256", True, "proposal_sha256 the user confirmed"),
                param("confirm_replace", "boolean", False, "required when SOUL.md or schema/CONTENT_POLICY.md already exists, passed only after the user confirmed the replacement"),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        action(
            "initialize",
            "initialize_wiki.py",
            "Build, lint, and release a complete new wiki in external staging and commit it to an empty target under a claim it takes and releases itself, so it never runs while the agent holds a claim.",
            [
                target("an empty or new wiki directory"),
                param("title", "string", False, "human-readable title, always passed explicitly even though the helper derives one from the topic or target when it is missing"),
                param("topic", "string", True, "short topic and scope boundary"),
                param("wiki_language", "BCP-47 code", True, "maintained wiki language, the same code the identity proposal was rendered for"),
                param("wiki_language_label", "string", False, "human-readable language name such as Deutsch"),
                param("storage_path_prefix", "string", False, "decoded OneDrive or SharePoint library path that enables the storage path checks"),
                param("quality_review_days", "integer", False, default=30),
                param("cleaning_review_days", "integer", False, default=90),
                param("snapshot_warning_days", "integer", False, default=60),
                param("identity_plan", "runtime path", True, "the confirmed identity proposal"),
                param("expect_identity_sha256", "sha256", True, "proposal_sha256 the user confirmed"),
                param("owner", "string", False, "public agent or run label of the internal claim", default="initialize-wiki"),
                param("summary", "string", False, "summary of the first release", default="Initialize portable AI First CRM"),
            ],
            invocation="direct", writes=True, destructive=False, preview=False, snapshot=False, confirmation_required=True, lock_required=False,
        ),
        action(
            "init-wiki",
            "init_wiki.py",
            "Create the missing files of a new wiki from the confirmed identity proposal under the agent's own claim, without overwriting anything, when curation continues under that claim.",
            [
                target(),
                param("title", "string", False, "human-readable title, always passed explicitly even though the helper derives one from the topic or target when it is missing"),
                param("wiki_language", "BCP-47 code", True, "maintained wiki language, the same code the identity proposal was rendered for"),
                param("wiki_language_label", "string", False, "human-readable language name such as Deutsch"),
                param("storage_path_prefix", "string", False, "decoded OneDrive or SharePoint library path that enables the storage path checks"),
                param("topic", "string", True, "short topic and scope boundary; --description is an alias of --topic", flag="--topic"),
                param("quality_review_days", "integer", False, default=30),
                param("cleaning_review_days", "integer", False, default=90),
                param("snapshot_warning_days", "integer", False, default=60),
                param("identity_plan", "runtime path", True, "the confirmed identity proposal"),
                param("expect_identity_sha256", "sha256", True, "proposal_sha256 the user confirmed"),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=False, confirmation_required=True, lock_required=True,
        ),
        # Sources and claims
        action(
            "validate-extraction",
            "validate_extraction.py",
            "Assess a staged Markdown extraction for UTF-8, flattened lines, page markers, headings, table structure, and credential-shaped content before it may be registered.",
            [
                param("markdown_file", "runtime path", True, "staged Markdown extraction outside the wiki"),
                param("source_type", "string", False, default="unknown"),
            ],
            invocation="direct", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=False,
        ),
        action(
            "register-source",
            "register_source.py",
            "Register a faithful Markdown extraction as a source of the wiki while the original stays outside and only a portable reference is stored.",
            [
                target(),
                param("markdown_file", "runtime path", True, "completed .md extraction outside the wiki"),
                param("title", "string", True),
                param("original_ref", "portable reference", True, "relative or logical reference, URL, or remote item ID, never an absolute local path"),
                param("original_file", "runtime path", False, "the exact original, read only for hashing and never stored"),
                param("original_version", "string", False),
                param("source_type", "string", False, "observation requires observed_by and occasion", default="unknown"),
                param("observed_by", "actor", False, "required with source_type observation, as human:<id>, agent/<name>, or process:<id>"),
                param("occasion", "string", False, "required with source_type observation: the meeting, decision, or run that produced it"),
                param("content_language", "BCP-47 code", False, "language of the extracted source, und when unknown", default="und"),
                param("extractor", "string", False, default="codex"),
                param("status", "active | partial", False, default="active"),
                param("notes", "string", False),
                param("confirm_extraction_warnings", "boolean", False, "allows an active registration of an extraction with warnings, only after the user reviewed them"),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "claim-id",
            "claim_id.py",
            "Generate stable claim IDs that no page of the wiki uses yet, without writing anything.",
            [target(), param("count", "integer from 1 to 1000", False, default=1)],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        # Frontmatter, snapshots, restore, moves, page batches
        action(
            "inventory-frontmatter",
            "inventory_wiki.py",
            "Report frontmatter property counts, observed types, samples, missing fields, naming drift, extensions, and parser errors without changing the wiki.",
            [target()],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "plan-frontmatter",
            "frontmatter_actions.py plan",
            "Resolve a selector and write an immutable before and after plan with file hashes for one frontmatter action.",
            [
                target(),
                param("selector_json", "selector JSON text", False, "the files to change, as described under selector", default='{"kind":"all"}'),
                param("action_json", "frontmatter action JSON text", True, "one action of type set, delete, rename, copy, merge, normalize_values, or dedupe_wikilinks"),
                output("temporary plan file outside the wiki that the apply step reads"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "apply-frontmatter",
            "frontmatter_actions.py apply",
            "Apply exactly a confirmed frontmatter plan when every file still matches its precondition hash, after a targeted snapshot.",
            [
                target(),
                plan_file(),
                expect_plan_sha256(),
                param("confirm_destructive", "boolean", False, "required when the plan is destructive, passed only after the user approved it"),
            ],
            invocation="run_locked", writes=True, destructive=True, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        action(
            "snapshot",
            "snapshot_wiki.py",
            "Create a portable pre-change snapshot of the wiki-controlled files with its own manifest before a change that no hash-bound helper protects.",
            [
                target(),
                param("run_id", "string", False, "filesystem-safe snapshot identifier, generated when omitted"),
                param("operation", "string", False, "short description stored in the snapshot", default="maintenance"),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=False, lock_required=True,
        ),
        action(
            "list-snapshots",
            "restore_wiki.py list",
            "List the valid and invalid recovery snapshots of the wiki.",
            [target()],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "plan-restore",
            "restore_wiki.py plan",
            "Preview a targeted restore from one snapshot with the current and snapshot hashes of every selected file.",
            [
                target(),
                param("snapshot_id", "string", True),
                param("paths_json", "JSON text array of vault-relative paths", False, "the files to restore, every file of the snapshot when omitted"),
                param("include_missing", "boolean", False, "also restore files the current wiki no longer has, only after the user approved it"),
                output("temporary restore plan file"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "apply-restore",
            "restore_wiki.py apply",
            "Restore exactly a confirmed restore plan after a recovery snapshot of the current state, never deleting files that the older snapshot lacks.",
            [
                target(),
                plan_file(),
                expect_plan_sha256(),
                param("confirm_restore", "boolean", False, "the helper refuses without it, so it is passed only after the user confirmed the restore plan"),
            ],
            invocation="run_locked", writes=True, destructive=True, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        action(
            "move-page",
            "move_wiki_page.py",
            "Preview a wiki page move with its affected links and preview hash or, with apply, move the page and rewrite internal wikilinks after a targeted snapshot.",
            [
                target(),
                param("from_path", "wiki path", True, "existing vault-relative Markdown path"),
                param("to_path", "wiki path", True, "new vault-relative Markdown path, which must not exist yet"),
                param("apply", "boolean", False, "perform the move instead of the preview, only after the user confirmed the preview"),
                param("expect_preview_sha256", "sha256", False, "required with apply: the preview_sha256 the user approved"),
            ],
            invocation="run_locked", writes=True, destructive=True, preview=True, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        action(
            "plan-language-migration",
            "plan_language_migration.py",
            "Report the complete page and claim impact of migrating the maintained wiki language without changing anything.",
            [
                target(),
                param("to_language", "BCP-47 code", True),
                param("to_label", "string", True, "human-readable name of the new language"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "plan-page-batch",
            "page_batch.py plan",
            "Check a staged batch of wiki pages and write a plan with the staged hashes and target preconditions outside the wiki.",
            [
                target(),
                param("staging_dir", "runtime path outside the wiki", True, "external staging directory holding the drafted pages under their wiki paths"),
                param("paths_json", "JSON text array of wiki paths", True, "the staged wiki/*.md paths of the batch"),
                output(),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "apply-page-batch",
            "page_batch.py apply",
            "Validate the confirmed page batch in a complete temporary mirror and commit it with a targeted snapshot and atomic per-file replacement.",
            [
                target(),
                param("staging_dir", "runtime path outside the wiki", True, "the staging directory the plan was made from"),
                plan_file(),
                expect_plan_sha256(),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        # Validation, graph, reviews, release
        action(
            "lint",
            "lint_wiki.py",
            "Validate the structure and traceability of the wiki, writing meta/lint-report.json and only deterministic safe repairs unless check_only is set.",
            [
                target(),
                param("fix_safe", "boolean", False, "request every deterministic safe repair"),
                param("check_only", "boolean", False, "baseline audit without repairs and without writing meta/lint-report.json, not combinable with fix_safe"),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "build-graph",
            "build_graph.py",
            "Regenerate the offline graph, the directory indexes, and the relative HTML reading views from the Markdown files and their wikilinks.",
            [target(), param("no_tags", "boolean", False, "do not render tags as graph nodes")],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "quality-review-record",
            "record_quality_review.py",
            "Append one quality or cleaning review record, only after that review was actually performed.",
            [
                target(),
                param("kind", "quality | cleaning", True),
                param("outcome", "passed | attention-needed | not-applicable", True),
                param("summary", "string", True, "one-line factual summary of the review, at most 500 characters"),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "release",
            "release_wiki.py",
            "Rerun strict lint, bump the version, record the quality status, hash the controlled files, and publish meta/manifest.json last, idempotently per operation ID.",
            [
                target(),
                param("bump", "patch | minor | major", False, default="patch"),
                param("summary", "string", False, default="Validated wiki maintenance release"),
                param("operation_id", "portable idempotency key", True, "stable key of this release intent, never reused after a later release"),
                param("expect_current_version", "semantic version", True, "the WIKI_VERSION observed before the release"),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "verify-release",
            "verify_release.py",
            "Verify the published release read-only, through run_locked.py while the agent still holds its claim because the helper otherwise reports wiki_busy, and directly once no claim exists.",
            [
                target("the released wiki directory"),
                param("expect_manifest_sha256", "sha256", False, "manifest hash the verified release must have"),
                param("allow_hydration", "boolean", False, "verify even when released files must be downloaded first, only after the user agreed"),
            ],
            invocation="either", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=False,
        ),
        # Exports and interoperability
        action(
            "export-frozen-skill",
            "export_wiki_skill.py",
            "Export one verified release as an immutable knowledge-only skill folder with its .skill package and checksum outside the wiki, refusing while any claim exists.",
            [
                target("the released wiki directory"),
                param("output_dir", "runtime path outside the wiki", False, "receives the skill folder, the .skill package, and its checksum, and is required unless dry_run is set"),
                param("skill_name", "lowercase-hyphenated name", True),
                param("description", "string of at most 1024 characters", False, "trigger description, built from the wiki title, purpose, and language when omitted"),
                param("exclude_crm", "boolean", False, "leave CRM records, their history, workflow runs, and generated CRM views out"),
                param("allow_large", "boolean", False, "export above the upload limit of 30,000,000 bytes for hosts that install skill folders directly"),
                param("dry_run", "boolean", False, "verify the release and report what the export would hold without writing anything"),
            ],
            invocation="direct", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=False,
        ),
        action(
            "export-okf-bundle",
            "export_okf_bundle.py",
            "Write one verified release as an Open Knowledge Format v0.2 bundle into a separate empty destination without a claim and without modifying the wiki.",
            [
                target("the released wiki directory"),
                param("destination", "runtime path outside the wiki", True, "empty or new output directory"),
                param("no_sources", "boolean", False, "omit the registered source extractions, so the bundle cannot answer its own citations"),
                param("allow_hydration", "boolean", False, "verify even when released files must be downloaded first, only after the user agreed"),
            ],
            invocation="direct", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=False,
        ),
        action(
            "report-okf",
            "report_okf.py",
            "Report the Open Knowledge Format v0.2 field compatibility of the wiki pages, and optionally of the sources, without changing anything.",
            [target(), param("include_sources", "boolean", False, "also check the registered sources")],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        # Synchronized storage, trust tiers, adoption
        action(
            "resolve-conflict-copy-plan",
            "resolve_conflict_copy.py plan",
            "Report every synchronization conflict copy beside its original with hashes, sizes, and modification times without changing anything.",
            [target(), output("plan file to write outside the wiki", required=False)],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "resolve-conflict-copy-apply",
            "resolve_conflict_copy.py apply",
            "Apply the confirmed decision for every conflict copy of a hash-bound plan after an automatic snapshot.",
            [
                target(),
                plan_file(),
                expect_plan_sha256(),
                param("decision", "PATH=keep-original | PATH=keep-copy | PATH=keep-both", False, "one per conflict copy of the plan, since the helper refuses with decision_required while any copy is undecided", repeatable=True),
            ],
            invocation="run_locked", writes=True, destructive=True, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        action(
            "verify-pages-plan",
            "verify_pages.py plan",
            "Show which pages a review confirmation would cover and the trust tier it would record, without changing anything.",
            [
                target(),
                param("actor", "actor", True, "agent/<name>, human:<id>, or process:<id>"),
                param("pages", "wiki path", True, "one page per flag", flag="--page", repeatable=True),
                output("plan file to write outside the wiki", required=False),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "verify-pages-apply",
            "verify_pages.py apply",
            "Record a confirmed page review from a hash-bound plan after an automatic snapshot.",
            [
                target(),
                plan_file(),
                expect_plan_sha256(),
                param("user_confirmed_human_review", "boolean", False, "required when the plan records a human: actor, passed only after that person confirmed the review"),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        action(
            "adopt-directory-plan",
            "adopt_directory.py plan",
            "Report which Markdown files of an existing collection outside the wiki could be registered as sources, with blockers and notes, without changing anything.",
            [
                target(),
                param("source_dir", "runtime path outside the wiki", True, "the existing Markdown collection"),
                param("content_language", "BCP-47 code", False, default="und"),
                output("plan file to write outside the wiki", required=False),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "adopt-directory-apply",
            "adopt_directory.py apply",
            "Register exactly the confirmed files of a hash-bound adoption plan as sources after an automatic snapshot, creating no wiki page.",
            [
                target(),
                param("source_dir", "runtime path outside the wiki", True, "the collection the plan was made from"),
                plan_file(),
                expect_plan_sha256(),
                param("accept", "path relative to source_dir", False, "one per confirmed file, and nothing is registered without any", repeatable=True),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        # Maintenance lock
        action(
            "lock-acquire",
            "wiki_lock.py acquire",
            "Claim the wiki for this maintainer by writing its claim file into .llmwiki.lock/ and the private token into a runtime file outside the wiki.",
            [
                target(),
                param("owner", "string", True, "agent or run identifier"),
                param("operation", "string", False, "short description of the run", default="maintain"),
                param("token_file", "runtime path outside the wiki", True, "private file that receives the token"),
                param("maintainer", "string", False, "team slot, user@host when omitted, set only when the user names another slot"),
                param("lease_seconds", "integer", False, "how long the claim stays valid without a heartbeat", default=3600),
                param("take_over", "boolean", False, "declare, and with the identical second call complete, the recorded handover of an expired claim"),
                param("settle_seconds", "integer", False, "how long a declared takeover stays visible before it takes effect", default=900),
                param("force", "boolean", False, "override every claim at once, only after the user explicitly approved it"),
                param("reason", "string", False, "required with force and take_over"),
            ],
            invocation="direct", writes=True, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=False,
        ),
        action(
            "lock-status",
            "wiki_lock.py status",
            "Show every visible claim on the wiki without changing anything.",
            [target()],
            invocation="direct", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=False,
        ),
        action(
            "lock-verify",
            "wiki_lock.py verify",
            "Verify with the private token file that the agent still solely owns the wiki and refresh the lease of its claim.",
            [target(), *token_pair()],
            invocation="direct", writes=True, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "lock-heartbeat",
            "wiki_lock.py heartbeat",
            "Extend the lease of the agent's own claim after a long pause.",
            [target(), *token_pair()],
            invocation="direct", writes=True, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "lock-release",
            "wiki_lock.py release",
            "Release the agent's own claim with its private token file at the end of a run.",
            [target(), *token_pair(), param("remove_token_file", "boolean", False, "also delete the private token file")],
            invocation="direct", writes=True, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "lock-withdraw",
            "wiki_lock.py withdraw",
            "Remove this machine's own claim and its conflict copies after an abandoned run whose token file is gone.",
            [
                target(),
                param("maintainer", "string", False, "team slot of the claim, user@host when omitted"),
                param("reason", "string", True),
            ],
            invocation="direct", writes=True, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=False,
        ),
        # CRM setup and records
        action(
            "crm-init",
            "crm_init.py",
            "Add the CRM layer to an initialized wiki, or with add_missing_standard_objects add missing standard objects after snapshotting the data model, never overwriting existing CRM files.",
            [
                target(),
                param("currency", "ISO 4217 code", False, "default currency of amounts", default="EUR"),
                param("add_missing_standard_objects", "boolean", False, "add standard objects that an existing data model lacks"),
                param("dry_run", "boolean", False, "report what would be created without writing"),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-plan-records",
            "crm_records.py plan",
            "Validate a CRM request of create, update, upsert, delete, restore, destroy, merge, and erase operations with file uploads and outbox artifacts and write an immutable transaction plan outside the wiki.",
            [
                target(),
                param("request_file", "runtime path", True, "request JSON with actor, origin, operations, and optional artifacts, kept outside the wiki"),
                output(),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-apply-records",
            "crm_records.py apply",
            "Apply exactly a confirmed CRM transaction plan after checking every precondition hash, snapshotting the touched files, storing the uploads, and appending the events.",
            [
                target(),
                plan_file(),
                expect_plan_sha256(),
                param("confirm_destructive", "boolean", False, "required when the plan is destructive (destroy, merge, erase, or removed files), passed only after the user confirmed"),
            ],
            invocation="run_locked", writes=True, destructive=True, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        action(
            "crm-show-record",
            "crm_records.py show",
            "Show one record with its relations in both directions and its timeline.",
            [target(), param("object", "object API name", True), param("id", "uuid", True)],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-revert-transaction",
            "crm_records.py revert",
            "Write a request outside the wiki that undoes one transaction from its snapshot, to be planned and applied like any other request.",
            [
                target(),
                param("transaction", "transaction id", True, "txn-... from the plan or the event log"),
                param("actor", "actor", True),
                param("output_request", "runtime path outside the wiki", True, "request file to write"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-cleanup-files",
            "crm_records.py cleanup-files",
            "Write a request outside the wiki that removes orphaned stored files or named stored files and outbox drafts, to be planned and applied as a destructive transaction.",
            [
                target(),
                param("orphans", "boolean", False, "stored files that no record refers to; one of orphans or path is required"),
                param("path", "vault path under records/_files/ or records/_outbox/", False, "a stored file, an outbox draft, or an outbox folder; one of orphans or path is required", repeatable=True),
                param("actor", "actor", True),
                param("output_request", "runtime path outside the wiki", True, "request file to write"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-acknowledge-credential",
            "crm_records.py acknowledge-credential",
            "Record that a named person confirmed one credential-shaped value in CRM data as harmless, storing only its digest.",
            [
                target(),
                param("path", "vault path", False, "CRM file named in the lint finding, given together with line unless value_sha256 is used"),
                param("line", "integer", False, "line of the lint finding, given together with path"),
                param("value_sha256", "sha256", False, "digest from a plan's credential error, used instead of path and line"),
                param("kind", "credential kind", True, "kind named in the finding"),
                param("confirmed_by", "human actor", True, "human:<id> of the person who confirmed that the value is harmless"),
                param("reason", "string", True),
                param("user_confirmed_harmless", "boolean", False, "the helper refuses without it, so it is passed only after that person confirmed"),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        # CRM import and export
        action(
            "crm-import-inspect",
            "crm_import.py inspect",
            "Read a CSV, XLSX, or JSON file and report its encoding, delimiter, headers, samples, a proposed field mapping, and the columns nothing matches.",
            [
                target(),
                param("file", "runtime path", True, "CSV, XLSX, or JSON file to import"),
                *IMPORT_READING,
                param("object", "object API name", False, "object to map to"),
                param("write_mapping", "runtime path outside the wiki", False, "write the proposed mapping as JSON"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-import-plan",
            "crm_import.py plan",
            "Map, normalize, validate, and match the rows of an import file and write one transaction plan with its row report outside the wiki.",
            [
                target(),
                param("file", "runtime path", True, "CSV, XLSX, or JSON file to import"),
                *IMPORT_READING,
                param("object", "object API name", True),
                param("mapping_file", "runtime path (one of mapping_file or auto_mapping is required)", False, "mapping JSON {\"columns\": {header: field key or null}}"),
                param("auto_mapping", "boolean (one of mapping_file or auto_mapping is required)", False, "use the proposed mapping"),
                param("match_key", "field key", True, "id or one unique field such as emails.primaryEmail"),
                param("mode", "create | upsert | update-only", True),
                param("actor", "actor", True),
                param("origin_ref", "portable file name", True, "portable name of the source file, stored in the event origin"),
                output(),
                param("skip_invalid_rows", "boolean", False, "leave invalid rows out, only with the user's consent"),
                param("redact_credentials", "boolean", False, "replace credential-shaped values with [credential removed]"),
                param("keep_empty_cells", "boolean", False, "empty cells leave existing values unchanged instead of clearing them"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-import-apply",
            "crm_import.py apply",
            "Apply exactly a confirmed import plan through the CRM transaction core after snapshotting the records it changes.",
            [
                target(),
                plan_file(),
                expect_plan_sha256(),
                param("confirm_destructive", "boolean", False, "needed only for a plan whose destructive flag is true, which an import plan of create and update operations never is"),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        action(
            "crm-export-csv",
            "crm_export.py csv",
            "Write the records of one object, optionally through a saved view, as import-compatible or plain CSV outside the wiki, split into numbered files above max_rows rows.",
            [
                target(),
                param("object", "object API name", True),
                param("output", "runtime path outside the wiki", True, "output file"),
                param("view", "view id or label", False, "saved view of that object whose filter, sort, and visible fields apply"),
                param("format", "standard | plain", False, default="standard"),
                param("include_deleted", "boolean", False, "include records in the trash"),
                param("max_rows", "integer", False, "rows per file, 20000 when omitted"),
                param("me", "workspace member id", False, "the member that @me in a view filter stands for"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-export-json",
            "crm_export.py json",
            "Write the records of one object in the API JSON shape to a file outside the wiki.",
            [
                target(),
                param("object", "object API name", True),
                param("output", "runtime path outside the wiki", True, "output file"),
                param("include_deleted", "boolean", False, "include records in the trash"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-export-sample",
            "crm_export.py sample",
            "Write a CSV import template with every importable column of one object and one example row outside the wiki.",
            [
                target(),
                param("object", "object API name", True),
                param("output", "runtime path outside the wiki", True, "output file"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-export-junctions",
            "crm_export.py junctions",
            "Write the targets links of notes or tasks as noteTargets or taskTargets junction rows to CSV outside the wiki.",
            [
                target(),
                param("object", "object API name", True, "note, task, or another object with a multi-target targets field"),
                param("output", "runtime path outside the wiki", True, "output file"),
                param("include_deleted", "boolean", False, "include records in the trash"),
                param("max_rows", "integer", False, "rows per file, 20000 when omitted"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        # CRM data model
        action(
            "crm-schema-plan",
            "crm_schema.py plan",
            "Plan data model changes with the record migration they need from a request whose operations are add-object, add-field, update-field, add-option, update-option, rename-option, remove-option, deactivate, activate, delete-field, and delete-object.",
            [
                target(),
                param("request_file", "runtime path", True, "request JSON with actor, occasion, and operations, kept outside the wiki"),
                output(),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-schema-apply",
            "crm_schema.py apply",
            "Apply exactly a confirmed data model plan, migrating the records first and replacing schema/crm/datamodel.json last, each after its own snapshot.",
            [
                target(),
                plan_file(),
                expect_plan_sha256(),
                param("confirm_destructive", "boolean", False, "required when the plan deletes fields or objects that hold data, passed only after the user confirmed"),
            ],
            invocation="run_locked", writes=True, destructive=True, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        # CRM queries
        action(
            "crm-query-find",
            "crm_query.py find",
            "List the records of one object that match a filter, sorted and paged, read-only on the locked working state or, with release, on the verified release.",
            query(
                param("object", "object API name", True),
                param("filter", "filter JSON text", False, "crm_filters filter such as {\"field\": \"stage\", \"operand\": \"IS\", \"value\": \"NEW\"}"),
                param("sort", "JSON text list", False, "sort levels such as [{\"field\": \"closeDate\", \"direction\": \"asc\"}]"),
                param("fields", "comma-separated field keys", False),
                param("limit", "integer from 1 to 1000", False, default=50),
                param("offset", "integer", False, default=0),
                param("include_deleted", "boolean", False, "include records in the trash"),
            ),
            invocation="either", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-query-aggregate",
            "crm_query.py aggregate",
            "Compute one aggregate over the records of an object, optionally filtered and grouped, read-only on the locked working state or, with release, on the verified release.",
            query(
                param("object", "object API name", True),
                param("function", "COUNT | COUNT_UNIQUE_VALUES | COUNT_EMPTY | COUNT_NOT_EMPTY | COUNT_TRUE | COUNT_FALSE | PERCENTAGE_EMPTY | PERCENTAGE_NOT_EMPTY | SUM | AVG | MIN | MAX", True),
                param("field", "field key", False),
                param("group_by", "field key", False),
                param("granularity", "DAY | WEEK | MONTH | QUARTER | YEAR | DAY_OF_WEEK | MONTH_OF_YEAR | QUARTER_OF_YEAR | NONE", False, "grouping of date fields"),
                param("filter", "filter JSON text", False),
                param("include_deleted", "boolean", False, "include records in the trash"),
            ),
            invocation="either", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-query-get",
            "crm_query.py get",
            "Show one record with its fields, linked records, and timeline, read-only on the locked working state or, with release, on the verified release.",
            query(param("object", "object API name", True), param("id", "uuid", True)),
            invocation="either", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-query-search",
            "crm_query.py search",
            "Run a ranked full-text search across records, read-only on the locked working state or, with release, on the verified release.",
            query(
                param("text", "string", True),
                param("objects", "comma-separated object API names", False, "every active object when omitted"),
                param("limit", "integer from 1 to 200", False, default=20),
                param("include_deleted", "boolean", False, "include records in the trash"),
            ),
            invocation="either", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-query-timeline",
            "crm_query.py timeline",
            "Show the change history from the event log, optionally for one object or record and since a moment, read-only on the locked working state or, with release, on the verified release.",
            query(
                param("object", "object API name", False),
                param("id", "uuid", False),
                param("since", "date or ISO-8601 instant", False),
                param("limit", "integer from 1 to 5000", False, default=200),
            ),
            invocation="either", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-query-time-in-stage",
            "crm_query.py time-in-stage",
            "Report how long records spent in each stage according to the event log, read-only on the locked working state or, with release, on the verified release.",
            query(
                param("object", "object API name", True),
                param("field", "select field key", False, default="stage"),
                param("id", "uuid", False),
                param("include_deleted", "boolean", False, "include records in the trash"),
                param("limit", "integer from 1 to 1000", False, default=100),
            ),
            invocation="either", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-query-expected-amount",
            "crm_query.py expected-amount",
            "Weight each amount with the probability of its stage and total the expected amount per currency, read-only on the locked working state or, with release, on the verified release.",
            query(
                param("object", "object API name", True),
                param("probabilities", "JSON text object", True, "share from 0 to 1 per stage option, such as {\"NEW\": 0.1, \"PROPOSAL\": 0.6}"),
                param("field", "currency field key", False, default="amount"),
                param("stage_field", "select field key", False, default="stage"),
                param("group_by", "field key or none", False, "the stage field when omitted, none for totals only"),
                param("granularity", "DAY | WEEK | MONTH | QUARTER | YEAR | DAY_OF_WEEK | MONTH_OF_YEAR | QUARTER_OF_YEAR | NONE", False, "grouping of date fields"),
                param("filter", "filter JSON text", False),
                param("include_deleted", "boolean", False, "include records in the trash"),
            ),
            invocation="either", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-query-runs",
            "crm_query.py runs",
            "List workflow runs from the run log newest first or show one run completely, read-only on the locked working state or, with release, on the verified release.",
            query(
                param("workflow", "workflow id", False, "only runs of this workflow"),
                param("status", "COMPLETED | FAILED | RUNNING | STOPPED", False, "only runs with this logged status, where CANCELLED is read as STOPPED"),
                param("since", "date or ISO-8601 instant", False, "only runs started at or after this moment"),
                param("limit", "integer from 1 to 1000", False, "at most this many runs, 50 when omitted"),
                param("run_id", "run id", False, "show this one run with every log entry and its step outputs, not combinable with status, since, or limit"),
            ),
            invocation="either", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        # CRM e-mail and calendar files
        action(
            "crm-ingest-plan",
            "crm_ingest.py plan",
            "Read e-mail and calendar files, apply the blocklist, bulk, visibility, and redaction rules, match the participants, and write one transaction plan with contact proposals outside the wiki.",
            [
                target(),
                param("files", "runtime paths, one or more values after a single --files", True, ".eml, .mbox, and .ics files or directories"),
                param("actor", "actor", True),
                output(),
                param("mailbox_owner", "e-mail address", False, "login e-mail of the workspace member who owns the mailbox"),
                param("own_addresses", "comma-separated e-mail addresses", False, "own addresses and aliases"),
                param("include_bulk", "boolean", False, "import mailing-list, automated, and group mails"),
                param("include_internal", "boolean", False, "import mails and events between own domains only"),
                param("include_private", "boolean", False, "import events marked private or confidential"),
                param("create_contacts", "boolean", False, "add the proposed people and companies to the plan, only after the user agreed"),
                param("visibility", "METADATA | SHARE_EVERYTHING | SUBJECT", False, "settings.email.default_visibility when omitted"),
                param("skip_failed", "boolean", False, "leave failing items out instead of blocking the plan, only after the user agreed"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-ingest-apply",
            "crm_ingest.py apply",
            "Apply exactly a confirmed e-mail and calendar import plan through the CRM transaction core.",
            [target(), plan_file(), expect_plan_sha256()],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        # CRM campaigns
        action(
            "crm-campaign-audience",
            "crm_campaign.py audience",
            "Count who would receive a campaign from a list or from a person filter or view, with the number excluded per reason, without writing anything.",
            [
                target(),
                param("list", "message list id", False, "one of list, filter, or view is required, and list excludes filter and view"),
                param("filter", "filter JSON text or a file holding it", False, "filter on people"),
                param("view", "view id", False, "a person view in schema/crm/views.json"),
                param("topic", "unsubscribe topic id", False, "topic whose unsubscribes also apply"),
                param("include_auto_created", "boolean", False, "keep people created automatically from e-mail or calendar files"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-campaign-plan-list",
            "crm_campaign.py plan-list",
            "Write a transaction plan outside the wiki that creates a message list, or extends one, with the people of a filter or person view.",
            [
                target(),
                param("actor", "actor", True),
                output(),
                param("filter", "filter JSON text or a file holding it", False, "one of filter or view is required"),
                param("view", "view id", False, "a person view; one of filter or view is required"),
                param("name", "string", False, "name of a new list, required unless list is given"),
                param("list", "message list id", False, "existing list to extend instead"),
                param("description", "string", False, "description of a new list"),
                param("consent_source", "string", False, "where the consent of these people is documented, without which members stay PENDING"),
                param("consent_at", "ISO date or instant", False, "when the consent was given"),
                param("include_auto_created", "boolean", False, "keep people created automatically from e-mail or calendar files"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-campaign-render",
            "crm_campaign.py render",
            "Plan one personalized .eml draft per recipient of a campaign in records/_outbox/campaigns/ together with the RENDERED status, writing only the plan outside the wiki and never sending anything.",
            [
                target(),
                param("actor", "actor", True),
                param("campaign", "campaign id", True),
                param("outbox", "runtime path outside the wiki", False, "new empty folder for an extra copy of the drafts, written when the plan is applied"),
                param("output", "runtime path outside the wiki", True, "status plan file to write"),
                param("unsubscribe_mailto", "e-mail address", False, "address for the List-Unsubscribe header"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-campaign-import-results",
            "crm_campaign.py import-results",
            "Turn a CSV of delivery results into a plan of suppression records written outside the wiki.",
            [
                target(),
                param("actor", "actor", True),
                param("file", "runtime path", True, "CSV with the columns email and status (bounced, complained, or unsubscribed)"),
                output(),
                param("topic", "unsubscribe topic id", False, "topic of the unsubscribe rows"),
                param("skip_failed", "boolean", False, "leave invalid rows out instead of blocking the plan"),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-campaign-apply",
            "crm_campaign.py apply",
            "Apply exactly a confirmed campaign plan, writing its records and outbox drafts into the wiki and the optional draft copy outside it.",
            [target(), plan_file(), expect_plan_sha256()],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        # CRM views, dashboards, configuration
        action(
            "crm-build",
            "crm_build.py",
            "Regenerate the CRM record browser, views, and dashboards under graph/crm/ deterministically, or with check only report whether they are stale.",
            [
                target(),
                param("now", "ISO-8601 instant", False, "build moment used by relative filters and the as-of line"),
                param("check", "boolean", False, "only report whether graph/crm/ is current, exiting with 3 when stale and writing nothing"),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-config-plan",
            "crm_config.py plan",
            "Validate a complete new views, dashboards, settings, or roles document against the data model and plan its replacement outside the wiki.",
            [
                target(),
                param("document", "dashboards | roles | settings | views", True),
                param("input", "runtime path outside the wiki", True, "the complete new JSON document"),
                output(),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-config-apply",
            "crm_config.py apply",
            "Replace one CRM configuration document with exactly the confirmed plan after a snapshot.",
            [
                target(),
                plan_file(),
                expect_plan_sha256(),
                param("confirm_removal", "boolean", False, "required when the new document removes entries, passed only after the user agreed"),
            ],
            invocation="run_locked", writes=True, destructive=True, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        # CRM workflows
        action(
            "crm-workflow-validate",
            "crm_workflows.py validate",
            "Validate every workflow definition against the data model without writing anything.",
            [target()],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-workflow-list",
            "crm_workflows.py list",
            "List the workflows with their versions, triggers, waiting runs, and due work without writing anything.",
            [target(), param("now", "ISO-8601 instant", False, "moment against which due work is computed")],
            invocation="run_locked", writes=False, destructive=False, preview=False, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-workflow-save-draft",
            "crm_workflows.py save-draft",
            "Save a definition as the draft version of a workflow, creating the workflow when it is new, after snapshotting the existing workflow file.",
            [
                target(),
                param("workflow", "workflow id", True),
                param("definition_file", "runtime path", True, "JSON file with one version body: trigger, steps, and optionally name and description"),
                param("now", "ISO-8601 instant", False),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-workflow-create-draft",
            "crm_workflows.py create-draft",
            "Create a new draft version of a workflow as a copy of an existing version after snapshotting the workflow file.",
            [
                target(),
                param("workflow", "workflow id", True),
                param("from_version", "integer", True),
                param("replace_draft", "boolean", False, "replace an existing draft instead of refusing"),
                param("now", "ISO-8601 instant", False),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-workflow-activate",
            "crm_workflows.py activate",
            "Activate a draft or the last published deactivated version of a workflow, freezing it by hash and archiving the previously active version, after a snapshot.",
            [
                target(),
                param("workflow", "workflow id", True),
                param("version", "integer", True),
                param("now", "ISO-8601 instant", False),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        action(
            "crm-workflow-deactivate",
            "crm_workflows.py deactivate",
            "Deactivate the active version of a workflow so that new triggers are ignored, after a snapshot.",
            [target(), param("workflow", "workflow id", True)],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        action(
            "crm-workflow-delete",
            "crm_workflows.py delete",
            "Delete a workflow definition with all its versions after a snapshot while keeping its run log, once no run of it is waiting.",
            [
                target(),
                param("workflow", "workflow id", True),
                param("confirm_destructive", "boolean", False, "the helper refuses without it, so it is passed only after the user confirmed the deletion"),
            ],
            invocation="run_locked", writes=True, destructive=True, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        action(
            "crm-workflow-run-plan",
            "crm_workflows.py run plan",
            "Plan manual, webhook, resumed, or due workflow runs as one CRM transaction with run log, state, and outbox drafts, writing only the plan outside the wiki.",
            [
                target(),
                param("workflow", "workflow id", True),
                param("record", "record selector", False, "UUID, record link, or match JSON of a record for a manual run", repeatable=True),
                param("records_file", "runtime path", False, "JSON list of record selectors"),
                param("payload_file", "runtime path", False, "webhook payload JSON"),
                param("answers_file", "runtime path", False, "lmwiki-crm-workflow-answers/1 file that continues waiting runs"),
                param("due", "boolean", False, "catch up the schedules, delays, and record events that became due"),
                param("now", "ISO-8601 instant", False),
                param("version", "integer", False, "run this version as a test run instead of the active one"),
                param("outbox", "runtime path outside the wiki", False, "extra copy folder for the drafts"),
                param("actor", "actor", True),
                output(),
            ],
            invocation="run_locked", writes=False, destructive=False, preview=True, snapshot=False, confirmation_required=False, lock_required=True,
        ),
        action(
            "crm-workflow-run-apply",
            "crm_workflows.py run apply",
            "Apply exactly a confirmed run plan, writing its records, run log entries, workflow state, and outbox drafts plus the optional copy outside the wiki.",
            [
                target(),
                plan_file(),
                expect_plan_sha256(),
                param("confirm_destructive", "boolean", False, "required when the runs destroy records, passed only after the user confirmed"),
            ],
            invocation="run_locked", writes=True, destructive=True, preview=False, snapshot=True, confirmation_required=True, lock_required=True,
        ),
        action(
            "crm-workflow-run-cancel",
            "crm_workflows.py run cancel",
            "Stop one waiting run of a workflow and log it as cancelled after snapshotting the workflow state.",
            [
                target(),
                param("workflow", "workflow id", True),
                param("run_id", "run id", True),
                param("actor", "actor", True),
                param("now", "ISO-8601 instant", False),
            ],
            invocation="run_locked", writes=True, destructive=False, preview=False, snapshot=True, confirmation_required=False, lock_required=True,
        ),
    ],
}


if __name__ == "__main__":
    print(json.dumps(CATALOG, ensure_ascii=False, indent=2))
