#!/usr/bin/env python3
"""Validate a whole proposed CRM revision using trusted runtime code, not PR code."""
from __future__ import annotations
import json, subprocess, sys, tempfile, shutil
from pathlib import Path
from typing import Any
from team_contract import (
    TeamError,
    CONFIG_PATH,
    RECEIPTS_DIR,
    RECEIPT_FORMAT,
    load_config,
    authorize_changes,
    change_set,
    make_preview,
    validate_approval,
    actor_for,
    payload_files,
    permission,
    digest,
)
from team_github import WORKFLOW_PATH, workflow_source


def verify_native(target: Path) -> dict[str, Any]:
    # Git has no empty directories. Recreate only the contract's empty source folder;
    # missing source files remain a manifest error and can never be synthesized.
    (target / "sources").mkdir(exist_ok=True)
    from verify_release import verify_snapshot

    result = verify_snapshot(target)
    if result.get("state") != "ready":
        raise TeamError(
            "invalid_release",
            "The complete workspace release is not verifiable.",
            release_state=result.get("state"),
        )
    scripts = Path(__file__).resolve().parent
    # Native lint intentionally requires a claim, including check-only mode.
    # The validation target is an isolated snapshot, so this never blocks shared readers.
    with tempfile.TemporaryDirectory(prefix="crm-team-validation-") as temporary:
        token = Path(temporary) / "token"
        claimed = subprocess.run(
            [
                sys.executable,
                str(scripts / "wiki_lock.py"),
                "acquire",
                "--target",
                str(target),
                "--owner",
                "trusted-team-validator",
                "--operation",
                "check-only",
                "--token-file",
                str(token),
            ],
            capture_output=True,
        )
        if claimed.returncode or not token.is_file():
            raise TeamError("workspace_busy")
        try:
            run = subprocess.run(
                [
                    sys.executable,
                    str(scripts / "run_locked.py"),
                    "--token-file",
                    str(token),
                    "--helper",
                    "lint_wiki.py",
                    "--target",
                    str(target),
                    "--check-only",
                ],
                capture_output=True,
            )
        finally:
            released = subprocess.run(
                [
                    sys.executable,
                    str(scripts / "wiki_lock.py"),
                    "release",
                    "--target",
                    str(target),
                    "--token-file",
                    str(token),
                    "--remove-token-file",
                ],
                capture_output=True,
            )
            if released.returncode or (target / ".llmwiki.lock").exists():
                raise TeamError("workspace_busy")
    if run.returncode:
        raise TeamError(
            "invalid_workspace",
            "The complete candidate failed strict CRM/wiki validation.",
        )
    return result


def validate_event_diff(
    base: Path,
    candidate: Path,
    cfg: dict[str, Any],
    actor: str,
    changed: list[dict[str, Any]],
) -> None:
    """Past logs are immutable except an explicitly privileged erase operation."""
    from crm_contract import read_events

    before = payload_files(base)
    after = payload_files(candidate)
    old_events = list(read_events(base))
    previous_ids = {e["event_id"] for e in old_events}
    erased = {
        (e.get("object"), e.get("record_id"))
        for e in old_events
        if e.get("op") == "erase" or e.get("erased") is True
    }
    new_events = [
        e for e in read_events(candidate) if e["event_id"] not in previous_ids
    ]
    for event in new_events:
        if event.get("actor") != actor_for(actor, cfg["members"][actor]["github_id"]):
            raise TeamError(
                "actor_mismatch",
                "New CRM events must identify the authenticated PR author.",
            )
        if event.get("op") in {"erase", "destroy", "merge"} and not permission(
            cfg, actor, event["op"]
        ):
            raise TeamError("permission_denied", "The operation requires a maintainer.")
    event_records = {(e.get("object"), e.get("record_id")) for e in new_events}
    for record in changed:
        if record["action"] == "create" and (record["object"], record["id"]) in erased:
            raise TeamError(
                "erased_record_restore",
                "Previously erased record identities cannot be restored from old Git copies.",
            )
        if (
            not record.get("derived_labels")
            and (record["object"], record["id"]) not in event_records
        ):
            raise TeamError(
                "missing_event",
                "Every changed CRM record needs its native transaction event.",
                record_id=record["id"],
            )
    erasure = any(e.get("op") == "erase" for e in new_events)
    migration = before.get("schema/crm/datamodel.json") != after.get(
        "schema/crm/datamodel.json"
    ) and any((e.get("origin") or {}).get("kind") == "migration" for e in new_events)
    for path in set(before) | set(after):
        if path.startswith(("meta/crm-events/", "meta/crm-runs/")):
            old = before.get(path, b"")
            new = after.get(path, b"")
            if not new.startswith(old) and not permission(cfg, actor, "erase"):
                raise TeamError(
                    "permission_denied",
                    "Rewriting historical event or run data requires a maintainer.",
                )
            if not new.startswith(old) and not (erasure or migration):
                raise TeamError(
                    "immutable_history",
                    "Historical event and run entries may only be rewritten by an erasure or model migration.",
                )
        if (
            path.startswith(RECEIPTS_DIR + "/")
            and path in before
            and before[path] != after.get(path)
        ):
            raise TeamError(
                "immutable_receipt",
                "Previously published operation receipts cannot be rewritten.",
            )


def validate_workflow_state(
    base: Path, candidate: Path, cfg: dict[str, Any], actor: str
) -> None:
    from crm_workflows import read_run_entries, event_line_counts

    path = "meta/crm-workflow-state.json"
    old = json.loads((base / path).read_text()) if (base / path).is_file() else {}
    new = (
        json.loads((candidate / path).read_text())
        if (candidate / path).is_file()
        else {}
    )
    if old == new:
        return
    old_entries = read_run_entries(base)
    entries = read_run_entries(candidate)
    old_signatures = {digest(e) for e in old_entries}
    added = [e for e in entries if digest(e) not in old_signatures]
    if not permission(cfg, actor, "workflows"):
        if not added:
            raise TeamError(
                "permission_denied",
                "Cursor-only workflow maintenance requires a maintainer.",
            )
        for entry in added:
            if entry.get("launched_by") != actor_for(
                actor, cfg["members"][actor]["github_id"]
            ):
                raise TeamError(
                    "permission_denied",
                    "A contributor may only execute or resume their own approved workflow runs.",
                )
    counts = event_line_counts(candidate)
    for workflow_id, previous in old.get("workflows", {}).items():
        definition = "schema/crm/workflows/" + workflow_id + ".json"
        if (
            (base / definition).is_file()
            and (candidate / definition).is_file()
            and (base / definition).read_bytes()
            == (candidate / definition).read_bytes()
        ):
            current = new.get("workflows", {}).get(workflow_id, {})
            for shard, value in (previous.get("event_cursor") or {}).items():
                if (current.get("event_cursor") or {}).get(shard, 0) < value:
                    raise TeamError(
                        "workflow_cursor_rollback",
                        "A restored state cannot replay already consumed events for an unchanged workflow.",
                    )
    for state in new.get("workflows", {}).values():
        for shard, value in (state.get("event_cursor") or {}).items():
            if not isinstance(value, int) or value < 0 or value > counts.get(shard, 0):
                raise TeamError("invalid_workflow_cursor")


def changed_operations(base: Path, candidate: Path) -> list[str]:
    from crm_contract import read_events

    ids = {e["event_id"] for e in read_events(base)}
    return [e.get("op", "") for e in read_events(candidate) if e["event_id"] not in ids]


def verify_rendered_views(candidate: Path) -> None:
    """Generated HTML is not trusted merely because a PR supplies matching hashes."""
    from team_git import native, acquire, release

    graph = json.loads((candidate / "graph/graph.json").read_text())
    graph_now = graph.get("generatedAt")
    if not isinstance(graph_now, str):
        raise TeamError("invalid_rendered_view")
    with tempfile.TemporaryDirectory(prefix="crm-team-render-check-") as temporary:
        root = Path(temporary)
        copy = root / "workspace"
        shutil.copytree(
            candidate,
            copy,
            ignore=shutil.ignore_patterns(".git", ".llmwiki.lock", "__pycache__"),
        )
        token = root / "token"
        acquire(copy, token, "trusted-team-validator", "render-check")
        try:
            if (copy / "schema/crm/datamodel.json").is_file():
                crm_now = json.loads(
                    (copy / "graph/crm/manifest.json").read_text()
                ).get("generated_at")
                if not isinstance(crm_now, str):
                    raise TeamError("invalid_rendered_view")
                native(copy, token, "crm_build.py", ["--now", crm_now])
            native(copy, token, "build_graph.py", ["--now", graph_now])
        finally:
            release(copy, token)
        submitted = {
            k: v for k, v in payload_files(candidate).items() if k.startswith("graph/")
        }
        expected = {
            k: v for k, v in payload_files(copy).items() if k.startswith("graph/")
        }
        if submitted != expected:
            raise TeamError(
                "untrusted_rendered_view",
                "Submitted browser views differ from the pinned trusted renderer.",
            )


def make_candidate_preview(
    base: Path, candidate: Path, cfg: dict[str, Any], actor: str, op: str, revision: str
) -> dict[str, Any]:
    allowed = authorize_changes(base, candidate, cfg, actor)
    operations = changed_operations(base, candidate)
    destructive = allowed["destructive"] or any(
        o in {"destroy", "merge", "erase"} for o in operations
    )
    # Model removals are destructive even when their records are retained.
    old_model = base / "schema/crm/datamodel.json"
    new_model = candidate / "schema/crm/datamodel.json"
    if old_model.is_file() and new_model.is_file():
        old = json.loads(old_model.read_text()).get("objects", {})
        new = json.loads(new_model.read_text()).get("objects", {})
        destructive |= bool(set(old) - set(new))
        for name in set(old) & set(new):
            destructive |= bool(
                set(old[name].get("fields", {})) - set(new[name].get("fields", {}))
            )
    warnings = (
        ["The private Git history and other clones/exports retain prior versions."]
        if destructive
        else []
    )
    return make_preview(
        op, revision, actor, change_set(base, candidate), warnings, destructive
    )


def validate_candidate(
    base: Path,
    candidate: Path,
    base_revision: str,
    actor: str,
    repository: str,
    head_revision: str = "",
    actor_id: Any = None,
) -> dict[str, Any]:
    """Authentication inputs come from GitHub, never from the proposed receipt."""
    cfg = load_config(base)
    if cfg["repository"].lower() != repository.lower():
        raise TeamError("repository_mismatch")
    if actor not in cfg["members"] or actor_id != cfg["members"][actor]["github_id"]:
        raise TeamError(
            "permission_denied",
            "The authenticated numeric GitHub identity does not match membership.",
        )
    verify_native(base)
    if (candidate / ".llmwiki.lock").exists():
        raise TeamError(
            "tracked_lock",
            "A candidate must not contain an active or tracked maintenance lock.",
        )
    for path in candidate.rglob("*"):
        parts = path.relative_to(candidate).parts
        if parts and parts[0] == ".git":
            continue
        if "__pycache__" in parts or path.suffix == ".pyc" or ".tokens" in parts:
            raise TeamError(
                "private_runtime_tracked",
                "Runtime state must stay outside the data repository.",
            )
    current = load_config(candidate)
    if (
        current["repository"].lower() != repository.lower()
        or current["branch"] != cfg["branch"]
    ):
        raise TeamError("repository_mismatch")
    if (candidate / WORKFLOW_PATH).read_text(encoding="utf-8") != workflow_source(
        current
    ):
        raise TeamError(
            "untrusted_workflow",
            "Team verification must use the exact pinned trusted runtime workflow.",
        )
    previous = payload_files(base)
    proposed = payload_files(candidate)
    receipts = [
        p for p in proposed if p.startswith(RECEIPTS_DIR + "/") and p not in previous
    ]
    if len(receipts) != 1:
        raise TeamError(
            "operation_receipt_required",
            "A proposal must add exactly one operation receipt.",
        )
    path = receipts[0]
    try:
        receipt = json.loads(proposed[path])
    except ValueError as exc:
        raise TeamError("invalid_receipt") from exc
    if (
        receipt.get("format") != RECEIPT_FORMAT
        or path != RECEIPTS_DIR + "/" + receipt.get("operation_id", "") + ".json"
    ):
        raise TeamError("invalid_receipt")
    if receipt.get("base_revision") != base_revision:
        raise TeamError(
            "stale_base",
            "The shared branch changed. Review a new plan before publishing.",
        )
    if receipt.get("actor") != actor or receipt.get("actor_id") != actor_id:
        raise TeamError(
            "actor_mismatch",
            "The operation actor must be the authenticated pull request author.",
        )
    preview = make_candidate_preview(
        base, candidate, cfg, actor, receipt["operation_id"], base_revision
    )
    if receipt.get("preview_sha256") != preview["preview_sha256"]:
        raise TeamError(
            "stale_preview", "Candidate data does not match the approved preview."
        )
    validate_approval(preview, receipt.get("approval", {}), actor)
    changes = authorize_changes(base, candidate, cfg, actor)
    validate_event_diff(base, candidate, cfg, actor, changes["changed_records"])
    validate_workflow_state(base, candidate, cfg, actor)
    verified = verify_native(candidate)
    verify_rendered_views(candidate)
    return {
        "state": "verified",
        "operation_id": receipt["operation_id"],
        "actor": actor,
        "base_revision": base_revision,
        "candidate_revision": head_revision,
        "preview_sha256": preview["preview_sha256"],
        "version": verified.get("version"),
        "records_changed": len(changes["changed_records"]),
        "git_history_retained": preview["retains_git_history"],
    }
