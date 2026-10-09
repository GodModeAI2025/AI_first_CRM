#!/usr/bin/env python3
"""Agent-facing Git team sessions. Ordinary skills, isolated files and protected PRs."""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, sys, tempfile, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from team_contract import (
    TeamError,
    CONFIG_PATH,
    RECEIPTS_DIR,
    RECEIPT_FORMAT,
    CHECK_CONTEXT,
    RUNTIME_REPOSITORY,
    load_config,
    validate_config,
    permission,
    actor_for,
    operation_id,
    canonical,
    digest,
    make_preview,
    approval_for,
    validate_approval,
    change_set,
    payload_files,
    login_for,
)
from team_github import (
    GitHub,
    WORKFLOW_PATH,
    workflow_source,
    git,
    git_text,
    repository_from_remote,
)
from team_integrity import (
    verify_native,
    validate_candidate,
    make_candidate_preview,
    validate_event_diff,
    validate_workflow_state,
)
from portable_io import atomic_write_bytes
from run_locked import HELPERS
from team_runtime import guarded, inventory, clear_dead_guard

SCRIPTS = Path(__file__).resolve().parent
SESSION_FORMAT = "ai-first-crm-team-session/1"
BUILD_HELPERS = {
    "crm_build.py",
    "build_graph.py",
    "lint_wiki.py",
    "verify_release.py",
    "describe_actions.py",
    "inventory_wiki.py",
}
RECORD_HELPERS = {"crm_records.py", "crm_import.py", "crm_ingest.py", "crm_campaign.py"}
READ_HELPERS = {
    "crm_query.py",
    "crm_export.py",
    "export_wiki_skill.py",
    "report_okf.py",
    "export_okf_bundle.py",
}
EXPORT_HELPERS = {"crm_export.py", "export_wiki_skill.py", "export_okf_bundle.py"}
OUTPUT_FLAGS = {
    "--output",
    "--output-request",
    "--write-mapping",
    "--outbox",
    "--staging",
    "--output-dir",
    "--destination",
    "--out-dir",
}


def utc() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical(value), mode=0o600)


def within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise TeamError(
            "unsafe_runtime", "Private runtime files must not be symbolic links."
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise TeamError("invalid_runtime", "A JSON object is required.")
        return value
    except (OSError, ValueError) as exc:
        raise TeamError("invalid_runtime") from exc


def native(
    target: Path,
    token_file: Optional[Path],
    helper: str,
    args: list[str],
    check: bool = True,
) -> dict[str, Any]:
    if helper not in HELPERS and helper not in {
        "export_wiki_skill.py",
        "export_okf_bundle.py",
        "wiki_lock.py",
    }:
        raise TeamError("unsupported_helper")
    if token_file:
        command = [
            sys.executable,
            str(SCRIPTS / "run_locked.py"),
            "--token-file",
            str(token_file),
            "--helper",
            helper,
            *args,
            "--target",
            str(target),
        ]
    else:
        command = [
            sys.executable,
            str(SCRIPTS / helper),
            *args,
            "--target",
            str(target),
        ]
    result = subprocess.run(
        command, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    )
    try:
        data = json.loads(result.stdout) if result.stdout else {}
    except ValueError:
        data = {"state": "helper_failed"}
    if check and result.returncode:
        raise TeamError(
            "helper_failed",
            "The trusted native helper rejected the staged operation.",
            helper=helper,
            helper_state=data.get("state", "error"),
        )
    return {**data, "exit_code": result.returncode}


def acquire(target: Path, token_file: Path, actor: str, op: str) -> None:
    result = native(
        target,
        None,
        "wiki_lock.py",
        [
            "acquire",
            "--owner",
            "github:" + actor,
            "--operation",
            "team-" + op,
            "--token-file",
            str(token_file),
        ],
    )
    if result.get("state") != "held":
        raise TeamError("workspace_busy")


def release(target: Path, token_file: Path) -> None:
    result = native(
        target,
        None,
        "wiki_lock.py",
        ["release", "--token-file", str(token_file), "--remove-token-file"],
    )
    if result.get("released") is not True or (target / ".llmwiki.lock").exists():
        raise TeamError("workspace_busy", "The candidate lock was not fully released.")


def root_for_session(session_file: Path) -> tuple[Path, dict[str, Any]]:
    s = read_json(session_file)
    root = session_file.parent.resolve()
    stat = root.stat()
    if (
        session_file.stat().st_mode & 0o077
        or s.get("format") != SESSION_FORMAT
        or s.get("root_identity") != [stat.st_dev, stat.st_ino]
    ):
        raise TeamError("unsafe_runtime", "The runtime directory identity changed.")
    if session_file.name != "session.json":
        raise TeamError("invalid_runtime")
    operation_id(s["operation_id"])
    return root, s


def provider_for(source: Path) -> GitHub:
    return GitHub(repository_from_remote(source))


def source_branch(source: Path) -> str:
    cfg = load_config(source, required=False)
    if cfg is not None:
        return cfg["branch"]
    return git_text(source, "symbolic-ref", "--short", "HEAD")


def published_receipt(
    provider: Any, cfg: dict[str, Any], op: str, actor: str, expected_preview: str = ""
) -> Optional[dict[str, Any]]:
    revision = provider.revision(cfg["branch"])
    with tempfile.TemporaryDirectory(prefix="crm-team-published-") as tmp:
        snapshot = Path(tmp)
        provider.archive(revision, snapshot)
        verify_native(snapshot)
        path = snapshot / RECEIPTS_DIR / (op + ".json")
        if not path.is_file():
            return None
        receipt = read_json(path)
        if receipt.get("format") != RECEIPT_FORMAT or receipt.get("operation_id") != op:
            raise TeamError("invalid_receipt")
        if receipt.get("actor_id") != cfg["members"].get(actor, {}).get(
            "github_id"
        ) or (expected_preview and receipt.get("preview_sha256") != expected_preview):
            raise TeamError(
                "operation_collision",
                "This operation identifier already belongs to another confirmed change.",
            )
        return {
            "state": "published",
            "operation_id": op,
            "actor": actor,
            "revision": revision,
            "version": receipt.get("version"),
            "preview_sha256": receipt.get("preview_sha256"),
            "git_history_retained": receipt.get("git_history_retained", False),
            "idempotent": True,
        }


def begin(
    source: Path,
    work_dir: Path,
    op: str,
    provider: Any,
    setup: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    op = operation_id(op)
    provider.assert_private()
    principal = provider.principal()
    actor = principal["login"]
    branch = setup["branch"] if setup is not None else source_branch(source)
    base_revision = provider.revision(branch)
    if work_dir.exists():
        raise TeamError(
            "runtime_exists",
            "Choose a new private runtime directory or resume its session.json.",
        )
    if within(work_dir, source):
        raise TeamError(
            "unsafe_runtime",
            "Keep runtime plans, tokens and candidate workspaces outside the shared clone.",
        )
    work_dir.mkdir(mode=0o700, parents=True)
    try:
        base = work_dir / "base"
        provider.archive(base_revision, base)
        verify_native(base)
        cfg = load_config(base, required=False)
        configuration_change = setup is not None
        bootstrap = configuration_change and cfg is None
        if configuration_change:
            if bootstrap and provider.permission_for(actor) != "admin":
                raise TeamError(
                    "permission_denied",
                    "Initial team setup requires repository administration rights.",
                )
            if cfg is not None and (
                not permission(cfg, actor, "team-config")
                or cfg["members"][actor]["github_id"] != principal["id"]
            ):
                raise TeamError("permission_denied")
            cfg = validate_config(setup)
            if (
                cfg["members"].get(actor, {}).get("role") != "owner"
                or cfg["members"][actor]["github_id"] != principal["id"]
            ):
                raise TeamError(
                    "permission_denied",
                    "The authenticated setup user must be an owner.",
                )
        else:
            if cfg is None:
                raise TeamError("team_not_configured")
            if (
                not permission(cfg, actor, "records")
                or cfg["members"][actor]["github_id"] != principal["id"]
            ):
                raise TeamError(
                    "permission_denied",
                    "This member can read, but cannot start a write session.",
                )
            if provider.permission_for(actor) not in {"write", "admin"}:
                raise TeamError(
                    "hosting_write_required",
                    "Contributors need GitHub write access to submit their own protected proposals.",
                )
            provider.assert_policy(branch)
            done = published_receipt(provider, cfg, op, actor)
            if done:
                return done
        if (
            cfg["repository"].lower() != provider.repository.lower()
            or cfg["branch"] != branch
        ):
            raise TeamError("repository_mismatch")
        if hasattr(provider, "assert_runtime"):
            provider.assert_runtime(cfg["runtime_revision"])
        proposal_branch = "crm/" + op + "/" + base_revision[:12]
        candidate = work_dir / "candidate"
        provider.clone(base_revision, candidate, proposal_branch)
        verify_native(candidate)
        if configuration_change:
            write_json(candidate / CONFIG_PATH, cfg)
            (candidate / WORKFLOW_PATH).parent.mkdir(parents=True, exist_ok=True)
            (candidate / WORKFLOW_PATH).write_text(
                workflow_source(cfg), encoding="utf-8"
            )
            ignore = candidate / ".gitignore"
            existing = ignore.read_text() if ignore.is_file() else ""
            for value in [
                ".llmwiki.lock/",
                "__pycache__/",
                "*.pyc",
                ".DS_Store",
                "Thumbs.db",
            ]:
                if value not in existing.splitlines():
                    existing += (
                        ("\n" if existing and not existing.endswith("\n") else "")
                        + value
                        + "\n"
                    )
            ignore.write_text(existing, encoding="utf-8")
        token = work_dir / "token"
        acquire(candidate, token, actor, op)
        stat = work_dir.stat()
        state = {
            "format": SESSION_FORMAT,
            "operation_id": op,
            "repository": provider.repository,
            "base_revision": base_revision,
            "branch": branch,
            "proposal_branch": proposal_branch,
            "principal": principal,
            "phase": "draft",
            "bootstrap": bootstrap,
            "configuration_change": configuration_change,
            "root_identity": [stat.st_dev, stat.st_ino],
            "created_at": utc(),
            "base_inventory": inventory(base),
        }
        write_json(work_dir / "session.json", state)
        return {
            "state": "draft",
            "operation_id": op,
            "base_revision": base_revision,
            "actor": actor,
            "session": "session.json",
            "workspace": "candidate",
            "private_runtime": True,
        }
    except Exception:
        # Only this helper-created, new directory may be cleaned up after a failed start.
        shutil.rmtree(work_dir)
        raise


def authenticated(root: Path, s: dict[str, Any], provider: Any) -> dict[str, Any]:
    provider.assert_private()
    principal = provider.principal()
    if principal != s["principal"]:
        raise TeamError(
            "identity_changed",
            "Resume this session with the GitHub identity that prepared it.",
        )
    cfg = load_config(root / "candidate" if s["bootstrap"] else root / "base")
    if (
        not permission(cfg, principal["login"], "read")
        or cfg["members"][principal["login"]]["github_id"] != principal["id"]
    ):
        raise TeamError("permission_denied")
    return cfg


def safe_arguments(
    root: Path, target: Path, helper: str, args: list[str], actor: str, actor_id: int
) -> list[str]:
    args = [
        piece
        for arg in args
        for piece in (
            arg.split("=", 1) if arg.startswith("--") and "=" in arg else [arg]
        )
    ]
    if args and args[0] == "--":
        args = args[1:]
    forbidden = {"--target", "--lock-token", "--token-file"}
    for arg in args:
        if arg.split("=", 1)[0] in forbidden:
            raise TeamError(
                "unsafe_arguments",
                "The team helper supplies the target and private lock itself.",
            )
    for i, arg in enumerate(args):
        key = arg.split("=", 1)[0]
        if key in OUTPUT_FLAGS:
            value = (
                arg.split("=", 1)[1]
                if "=" in arg
                else (args[i + 1] if i + 1 < len(args) else "")
            )
            output = Path(value).expanduser().resolve()
            if (
                not value
                or not within(output, root)
                or within(output, target)
                or within(output, root / "base")
            ):
                raise TeamError(
                    "unsafe_output",
                    "Plans and exports must stay in the selected private runtime directory, outside the candidate and baseline.",
                )
        if key == "--actor":
            value = (
                arg.split("=", 1)[1]
                if "=" in arg
                else (args[i + 1] if i + 1 < len(args) else "")
            )
            if value != actor_for(actor, actor_id):
                raise TeamError("actor_mismatch", "Use the authenticated team actor.")
    # Keep originals unchanged. Only the runtime copy of a request gets the actor binding.
    if "--request-file" in args:
        i = args.index("--request-file") + 1
        if i >= len(args):
            raise TeamError("unsafe_arguments")
        request = read_json(Path(args[i]).expanduser().resolve())
        request["actor"] = actor_for(actor, actor_id)
        copied = root / ("request-" + uuid.uuid4().hex + ".json")
        write_json(copied, request)
        args[i] = str(copied)
        state = read_json(root / "session.json")
        state.setdefault("owned_runtime_files", []).append(copied.name)
        write_json(root / "session.json", state)
    method = args[0] if args and not args[0].startswith("-") else None
    help_args = [method, "--help"] if method else ["--help"]
    help_run = subprocess.run(
        [sys.executable, str(SCRIPTS / helper), *help_args], capture_output=True
    )
    if b"--actor" in help_run.stdout and not any(
        a.split("=", 1)[0] == "--actor" for a in args
    ):
        args += ["--actor", actor_for(actor, actor_id)]
    return args


@guarded
def run_helper(
    session_file: Path, helper: str, args: list[str], provider: Any
) -> dict[str, Any]:
    root, s = root_for_session(session_file)
    cfg = authenticated(root, s, provider)
    actor = s["principal"]["login"]
    if s["phase"] != "draft":
        raise TeamError(
            "session_frozen",
            "Start a new preview before changing an approved candidate.",
        )
    if helper not in HELPERS or helper in {
        "release_wiki.py",
        "init_wiki.py",
        "initialize_wiki.py",
    }:
        raise TeamError(
            "unsupported_helper",
            "Initialize first; the team helper owns release publication.",
        )
    workflow_run = helper == "crm_workflows.py" and bool(args) and args[0] == "run"
    area = (
        "records"
        if helper in RECORD_HELPERS or helper in BUILD_HELPERS or workflow_run
        else (
            "export"
            if helper in EXPORT_HELPERS
            else (
                "read"
                if helper == "crm_query.py"
                else (
                    "schema"
                    if helper
                    in {
                        "crm_init.py",
                        "crm_schema.py",
                        "crm_config.py",
                        "crm_workflows.py",
                    }
                    else "knowledge"
                )
            )
        )
    )
    if not permission(cfg, actor, area):
        raise TeamError(
            "permission_denied", "This member may not invoke this class of helper."
        )
    actual = safe_arguments(
        root, root / "candidate", helper, args, actor, s["principal"]["id"]
    )
    data = native(root / "candidate", root / "token", helper, actual, check=False)
    s = read_json(session_file)
    s["last_helper"] = helper
    s["last_result"] = data.get("state", "unknown")
    write_json(session_file, s)
    return {**data, "operation_id": s["operation_id"], "shared_data_changed": False}


@guarded
def build_preview(session_file: Path, provider: Any) -> dict[str, Any]:
    root, s = root_for_session(session_file)
    cfg = authenticated(root, s, provider)
    if s["phase"] != "draft":
        raise TeamError("session_frozen")
    if provider.revision(s["branch"]) != s["base_revision"]:
        raise TeamError(
            "stale_base",
            "The shared branch changed. Prepare a new isolated session and review its preview.",
        )
    candidate = root / "candidate"
    token = root / "token"
    moment = utc()
    if (candidate / "schema/crm/datamodel.json").is_file():
        native(candidate, token, "crm_build.py", ["--now", moment])
    native(candidate, token, "build_graph.py", ["--now", moment])
    native(candidate, token, "lint_wiki.py", ["--fix-safe"])
    preview = make_candidate_preview(
        root / "base",
        candidate,
        cfg,
        s["principal"]["login"],
        s["operation_id"],
        s["base_revision"],
    )
    from team_contract import authorize_changes

    changes = authorize_changes(root / "base", candidate, cfg, s["principal"]["login"])
    validate_event_diff(
        root / "base",
        candidate,
        cfg,
        s["principal"]["login"],
        changes["changed_records"],
    )
    validate_workflow_state(root / "base", candidate, cfg, s["principal"]["login"])
    if not preview["changes"]:
        raise TeamError("no_changes", "There is no material change to publish.")
    write_json(root / "preview.json", preview)
    s["preview_sha256"] = preview["preview_sha256"]
    write_json(session_file, s)
    # Return all structured changes. The full private preview is never put in Git.
    return {**preview, "preview_file": "preview.json", "shared_data_changed": False}


def bump_for(base: Path, candidate: Path) -> str:
    changes = change_set(base, candidate)
    paths = {c["path"] for c in changes}
    if "schema/WIKI_PROFILE.md" in paths:
        from frontmatter_contract import parse_document

        old = parse_document((base / "schema/WIKI_PROFILE.md").read_text())[0]
        new = parse_document((candidate / "schema/WIKI_PROFILE.md").read_text())[0]
        if old.get("wiki_language") != new.get("wiki_language"):
            return "major"
    if any(p.startswith("schema/") or p in {"WIKI.md", "SOUL.md"} for p in paths):
        return "minor"
    return "patch"


@guarded
def approve(
    session_file: Path,
    expected: str,
    confirm_destructive: bool,
    ack_history: bool,
    provider: Any,
) -> dict[str, Any]:
    root, s = root_for_session(session_file)
    cfg = authenticated(root, s, provider)
    if s["phase"] != "draft":
        raise TeamError("session_frozen")
    if provider.revision(s["branch"]) != s["base_revision"]:
        raise TeamError("stale_base", "The shared branch changed after the preview.")
    actor = s["principal"]["login"]
    stored = read_json(root / "preview.json")
    current = make_candidate_preview(
        root / "base",
        root / "candidate",
        cfg,
        actor,
        s["operation_id"],
        s["base_revision"],
    )
    if current != stored or expected != s.get("preview_sha256"):
        raise TeamError("stale_preview", "The candidate changed after its preview.")
    approval = approval_for(current, actor, expected, confirm_destructive, ack_history)
    from release_wiki import bump_version, read_version

    bump = bump_for(root / "base", root / "candidate")
    version = bump_version(read_version(root / "base" / "WIKI_VERSION"), bump)
    receipt = {
        "format": RECEIPT_FORMAT,
        "operation_id": s["operation_id"],
        "base_revision": s["base_revision"],
        "actor": actor,
        "actor_id": s["principal"]["id"],
        "preview_sha256": expected,
        "approval": approval,
        "proposal_branch": s["proposal_branch"],
        "version": version,
        "git_history_retained": current["retains_git_history"],
        "created_at": utc(),
    }
    # Finalize a private copy. A failed release/check never damages the editable draft.
    finalizer = root / ("finalized-" + uuid.uuid4().hex)
    final_token = root / ("final-token-" + uuid.uuid4().hex)
    shutil.copytree(
        root / "candidate",
        finalizer,
        ignore=shutil.ignore_patterns(".llmwiki.lock", "__pycache__"),
    )
    try:
        copied = make_candidate_preview(
            root / "base", finalizer, cfg, actor, s["operation_id"], s["base_revision"]
        )
        if copied != stored:
            raise TeamError("stale_preview")
        acquire(finalizer, final_token, actor, s["operation_id"])
        write_json(finalizer / RECEIPTS_DIR / (s["operation_id"] + ".json"), receipt)
        native(
            finalizer,
            final_token,
            "release_wiki.py",
            [
                "--operation-id",
                "team-" + s["operation_id"],
                "--expect-current-version",
                (root / "base" / "WIKI_VERSION").read_text().strip(),
                "--bump",
                bump,
                "--summary",
                "Confirmed Git team operation " + s["operation_id"],
            ],
        )
        release(finalizer, final_token)
        verify_native(finalizer)
        if not s["bootstrap"]:
            validate_candidate(
                root / "base",
                finalizer,
                s["base_revision"],
                actor,
                s["repository"],
                actor_id=s["principal"]["id"],
            )
        git(finalizer, "config", "user.name", actor)
        git(
            finalizer,
            "config",
            "user.email",
            str(s["principal"]["id"]) + "+" + actor + "@users.noreply.github.com",
        )
        git(finalizer, "add", "--all")
        git(finalizer, "commit", "-m", "CRM operation " + s["operation_id"])
        head = git_text(finalizer, "rev-parse", "HEAD")
        tree = git_text(finalizer, "rev-parse", "HEAD^{tree}")
        if (
            make_candidate_preview(
                root / "base",
                root / "candidate",
                cfg,
                actor,
                s["operation_id"],
                s["base_revision"],
            )
            != stored
        ):
            raise TeamError(
                "stale_preview",
                "The editable draft changed while its approved copy was being finalized.",
            )
    except Exception:
        if final_token.is_file():
            try:
                release(finalizer, final_token)
            except TeamError:
                pass
        shutil.rmtree(finalizer)
        if final_token.exists():
            final_token.unlink()
        raise
    backup = "draft-" + uuid.uuid4().hex
    s.update(
        phase="finalizing",
        pending_candidate=finalizer.name,
        draft_backup=backup,
        head_revision=head,
        tree_revision=tree,
        preview_sha256=expected,
    )
    write_json(session_file, s)
    release(root / "candidate", root / "token")
    (root / "candidate").rename(root / backup)
    finalizer.rename(root / "candidate")
    s["phase"] = "approved"
    s.pop("pending_candidate", None)
    s["candidate_inventory"] = inventory(root / "candidate")
    write_json(session_file, s)
    return {
        "state": "approved",
        "operation_id": s["operation_id"],
        "preview_sha256": expected,
        "candidate_revision": head,
        "shared_data_changed": False,
    }


def frozen(root: Path, s: dict[str, Any]) -> None:
    if s["phase"] not in {"approved", "submitted", "published"}:
        raise TeamError("approval_required")
    if git_text(root / "candidate", "rev-parse", "HEAD") != s[
        "head_revision"
    ] or git_text(root / "candidate", "status", "--porcelain", "--untracked-files=all"):
        raise TeamError(
            "stale_preview", "The frozen candidate was changed after approval."
        )
    verify_native(root / "candidate")


@guarded
def submit(session_file: Path, provider: Any) -> dict[str, Any]:
    root, s = root_for_session(session_file)
    cfg = authenticated(root, s, provider)
    frozen(root, s)
    done = published_receipt(
        provider, cfg, s["operation_id"], s["principal"]["login"], s["preview_sha256"]
    )
    if done:
        return done
    if provider.revision(s["branch"]) != s["base_revision"]:
        raise TeamError("stale_base")
    if s["bootstrap"]:
        raise TeamError(
            "setup_requires_publish",
            "Approved owner setup uses publish; ordinary changes use protected pull requests.",
        )
    provider.assert_policy(s["branch"])
    provider.push(root / "candidate", s["proposal_branch"])
    pr = provider.find_pr(s["proposal_branch"], s["branch"]) or provider.create_pr(
        s["proposal_branch"], s["branch"], s["operation_id"]
    )
    if (
        pr.get("head", {}).get("sha") != s["head_revision"]
        or pr.get("user", {}).get("login", "").lower() != s["principal"]["login"]
    ):
        raise TeamError(
            "proposal_mismatch",
            "The existing proposal does not belong to this frozen operation.",
        )
    if pr.get("state") == "closed" and not pr.get("merged"):
        raise TeamError(
            "proposal_closed", "The proposal was closed without publication."
        )
    s["phase"] = "submitted"
    s["pull_request"] = pr["number"]
    write_json(session_file, s)
    return {
        "state": "submitted",
        "operation_id": s["operation_id"],
        "pull_request": pr["number"],
        "url": pr.get("html_url", ""),
        "candidate_revision": s["head_revision"],
        "published": False,
    }


@guarded
def publish(session_file: Path, provider: Any) -> dict[str, Any]:
    root, s = root_for_session(session_file)
    cfg = authenticated(root, s, provider)
    frozen(root, s)
    actor = s["principal"]["login"]
    done = published_receipt(
        provider, cfg, s["operation_id"], actor, s["preview_sha256"]
    )
    if done:
        if s["bootstrap"]:
            provider.protect(s["branch"])
        s["phase"] = "published"
        s["published_revision"] = done["revision"]
        write_json(session_file, s)
        return done
    if provider.revision(s["branch"]) != s["base_revision"]:
        raise TeamError(
            "stale_base",
            "The shared branch changed; this operation needs a new preview.",
        )
    if s["bootstrap"]:
        if provider.permission_for(actor) != "admin":
            raise TeamError("permission_denied")
        provider.push_bootstrap(root / "candidate", s["branch"], s["base_revision"])
        result = published_receipt(
            provider, cfg, s["operation_id"], actor, s["preview_sha256"]
        )
        if not result:
            raise TeamError(
                "publication_unconfirmed", "The bootstrap push could not be verified."
            )
        provider.protect(s["branch"])
    else:
        provider.assert_policy(s["branch"])
        if s["phase"] != "submitted":
            raise TeamError("proposal_required")
        pr = provider.pull(s["pull_request"])
        if (
            pr.get("head", {}).get("sha") != s["head_revision"]
            or pr.get("base", {}).get("ref") != s["branch"]
        ):
            raise TeamError("proposal_mismatch")
        if not provider.trusted_status(s["head_revision"]):
            return {
                "state": "checks_pending",
                "operation_id": s["operation_id"],
                "pull_request": s["pull_request"],
                "published": False,
            }
        # Revalidate the complete candidate immediately before invoking the protected merge.
        validate_candidate(
            root / "base",
            root / "candidate",
            s["base_revision"],
            actor,
            s["repository"],
            s["head_revision"],
            actor_id=s["principal"]["id"],
        )
        try:
            merge = provider.merge(s["pull_request"], s["head_revision"])
            if not merge.get("merged"):
                return {
                    "state": "merge_pending",
                    "operation_id": s["operation_id"],
                    "published": False,
                }
            merge_revision = merge["sha"]
            if provider.commit_tree(merge_revision) != s["tree_revision"]:
                raise TeamError(
                    "publication_mismatch",
                    "The published tree differs from the reviewed candidate.",
                )
        except TeamError:
            # A lost response is not a failed or successful publication claim. Inspect authoritative data.
            recovered = published_receipt(
                provider, cfg, s["operation_id"], actor, s["preview_sha256"]
            )
            if not recovered:
                raise
            pr = provider.pull(s["pull_request"])
            merge_revision = pr.get("merge_commit_sha", "")
            if (
                not merge_revision
                or provider.commit_tree(merge_revision) != s["tree_revision"]
            ):
                raise TeamError("publication_mismatch")
        result = published_receipt(
            provider, cfg, s["operation_id"], actor, s["preview_sha256"]
        )
        if not result:
            raise TeamError(
                "publication_unconfirmed",
                "The remote did not contain the published operation receipt.",
            )
        result["operation_revision"] = merge_revision
    s["phase"] = "published"
    s["published_revision"] = result["revision"]
    write_json(session_file, s)
    return {**result, "idempotent": False}


def read_snapshot(source: Path, work_dir: Path, provider: Any) -> dict[str, Any]:
    provider.assert_private()
    principal = provider.principal()
    revision = provider.revision(source_branch(source))
    if work_dir.exists() or within(work_dir, source):
        raise TeamError(
            "unsafe_runtime", "Use a new reader directory outside the shared clone."
        )
    work_dir.mkdir(mode=0o700, parents=True)
    snapshot = work_dir / "snapshot"
    provider.archive(revision, snapshot)
    cfg = load_config(snapshot)
    if (
        cfg["repository"].lower() != provider.repository.lower()
        or not permission(cfg, principal["login"], "read")
        or cfg["members"][principal["login"]]["github_id"] != principal["id"]
    ):
        raise TeamError("permission_denied")
    verified = verify_native(snapshot)
    stat = work_dir.stat()
    state = {
        "format": SESSION_FORMAT,
        "operation_id": str(uuid.uuid4()),
        "repository": provider.repository,
        "principal": principal,
        "branch": cfg["branch"],
        "base_revision": revision,
        "phase": "reader",
        "root_identity": [stat.st_dev, stat.st_ino],
        "snapshot_inventory": inventory(snapshot),
    }
    write_json(work_dir / "session.json", state)
    return {
        "state": "reader",
        "revision": revision,
        "version": verified["version"],
        "snapshot": "snapshot",
        "session": "session.json",
    }


@guarded
def read_helper(
    session_file: Path, helper: str, args: list[str], provider: Any
) -> dict[str, Any]:
    root, s = root_for_session(session_file)
    if s["phase"] != "reader" or helper not in READ_HELPERS:
        raise TeamError("unsupported_helper")
    provider.assert_private()
    principal = provider.principal()
    if principal != s["principal"]:
        raise TeamError("identity_changed")
    snapshot = root / "snapshot"
    area = "export" if helper in EXPORT_HELPERS else "read"
    with tempfile.TemporaryDirectory(
        prefix="crm-team-current-membership-"
    ) as temporary:
        current = Path(temporary)
        provider.archive(provider.revision(s["branch"]), current)
        verify_native(current)
        cfg = load_config(current)
    if cfg["members"].get(principal["login"], {}).get("github_id") != principal["id"]:
        raise TeamError("permission_denied")
    if not permission(cfg, principal["login"], area):
        raise TeamError(
            "permission_denied",
            "The configured skill export policy denies this operation. Repository readers can still copy data outside the skill.",
        )
    verify_native(snapshot)
    actual = safe_arguments(
        root, snapshot, helper, args, principal["login"], principal["id"]
    )
    if helper in {"crm_export.py", "report_okf.py"}:
        # This helper currently needs a local claim even for export. It owns only the private reader copy.
        token = root / "read-token"
        acquire(snapshot, token, principal["login"], s["operation_id"])
        try:
            result = native(snapshot, token, helper, actual)
        finally:
            release(snapshot, token)
    else:
        if helper == "crm_query.py":
            actual += ["--release"]
        result = native(snapshot, None, helper, actual)
    verify_native(snapshot)
    return {
        **result,
        "revision": s["base_revision"],
        "published_snapshot": True,
        "personal_data_warning": (
            "Exports may contain personal data, including free text and custom fields."
            if helper in EXPORT_HELPERS
            else ""
        ),
    }


def check_pr(
    repository: str, number: int, provider: Any, post_status: bool = False
) -> dict[str, Any]:
    provider.assert_private()
    pr = provider.pull(number)
    head = pr["head"]["sha"]
    if (
        pr.get("head", {}).get("repo", {}).get("full_name", "").lower()
        != repository.lower()
    ):
        raise TeamError(
            "foreign_proposal",
            "Data proposals must use branches in the private team repository, not forks.",
        )
    try:
        branch = pr["base"]["ref"]
        current = provider.revision(branch)
        if pr["base"]["sha"] != current:
            raise TeamError("stale_base")
        if post_status:
            provider.status(
                head, "pending", "Checking confirmed CRM data and release integrity"
            )
        with tempfile.TemporaryDirectory(prefix="crm-team-check-") as tmp:
            root = Path(tmp)
            provider.archive(current, root / "base")
            provider.archive(head, root / "candidate")
            if hasattr(provider, "assert_runtime"):
                provider.assert_runtime(
                    load_config(root / "candidate")["runtime_revision"]
                )
            result = validate_candidate(
                root / "base",
                root / "candidate",
                current,
                pr["user"]["login"].lower(),
                repository,
                head,
                actor_id=pr["user"]["id"],
            )
        if post_status:
            provider.status(
                head, "success", "Confirmed CRM candidate and complete release verified"
            )
        return result
    except Exception as exc:
        if post_status:
            provider.status(
                head,
                "failure",
                "Candidate blocked: review data, permissions, approval or base revision",
            )
        if isinstance(exc, TeamError):
            raise
        raise TeamError(
            "integrity_check_failed", "The complete candidate could not be verified."
        ) from exc


def status(source: Path, provider: Any) -> dict[str, Any]:
    provider.assert_private()
    principal = provider.principal()
    revision = provider.revision(source_branch(source))
    with tempfile.TemporaryDirectory(prefix="crm-team-status-") as tmp:
        root = Path(tmp)
        provider.archive(revision, root)
        cfg = load_config(root)
        verify_native(root)
        if (
            not permission(cfg, principal["login"], "read")
            or cfg["members"][principal["login"]]["github_id"] != principal["id"]
        ):
            raise TeamError("permission_denied")
        provider.assert_policy(cfg["branch"])
        return {
            "state": "ready",
            "repository": provider.repository,
            "revision": revision,
            "actor": principal["login"],
            "role": cfg["members"][principal["login"]]["role"],
            "storage_profile": "git-history",
            "no_separate_application": True,
        }


def history_audit(source: Path, provider: Any) -> dict[str, Any]:
    provider.assert_private()
    principal = provider.principal()
    with tempfile.TemporaryDirectory(prefix="crm-team-history-policy-") as temporary:
        current = Path(temporary)
        provider.archive(provider.revision(source_branch(source)), current)
        verify_native(current)
        cfg = load_config(current)
    if not permission(cfg, principal["login"], "history"):
        raise TeamError("permission_denied")
    # Report scope without printing identifiers/values from old customer records.
    refs = git_text(
        source,
        "for-each-ref",
        "--format=%(refname)",
        "refs/heads/",
        "refs/tags/",
        "refs/remotes/",
    ).splitlines()
    count = git_text(source, "rev-list", "--all", "--count")
    return {
        "state": "history_scope",
        "local_reachable_commits": int(count or 0),
        "local_refs": refs,
        "current_data_erasure_reaches_git_history": False,
        "external_clones_exports_backups": "not-enumerable-from-this-clone",
        "complete_erasure_verified": False,
        "next_action": "Coordinate repository/history, hosting caches, other clones and backup/restore retention separately. Never claim complete erasure from a normal CRM release.",
    }


def sync(source: Path, provider: Any) -> dict[str, Any]:
    provider.assert_private()
    principal = provider.principal()
    branch = source_branch(source)
    revision = provider.revision(branch)
    with tempfile.TemporaryDirectory(prefix="crm-team-sync-verify-") as temporary:
        target = Path(temporary)
        provider.archive(revision, target)
        verified = verify_native(target)
        cfg = load_config(target)
        if (
            cfg["members"].get(principal["login"], {}).get("github_id")
            != principal["id"]
        ):
            raise TeamError("permission_denied")
    if git_text(source, "status", "--porcelain", "--untracked-files=all"):
        raise TeamError(
            "local_changes_preserved",
            "The shared clone has local changes. Read an isolated published snapshot instead of overwriting them.",
        )
    if git_text(source, "symbolic-ref", "--short", "HEAD") != branch:
        raise TeamError("wrong_local_branch")
    provider.fetch(source, branch)
    if git_text(source, "rev-parse", "FETCH_HEAD") != revision:
        raise TeamError(
            "remote_changed",
            "The remote advanced during synchronization; retry from its new verified revision.",
        )
    git(source, "merge", "--ff-only", revision)
    return {
        "state": "synchronized",
        "revision": revision,
        "version": verified["version"],
        "local_changes_discarded": False,
    }


def recover(session_file: Path, provider: Any) -> dict[str, Any]:
    root, s = root_for_session(session_file)
    if provider.principal() != s["principal"]:
        raise TeamError("identity_changed")
    clear_dead_guard(session_file)
    if s["phase"] != "finalizing":
        return {
            "state": s["phase"],
            "operation_id": s["operation_id"],
            "private_session_recovered": True,
        }
    pending = s.get("pending_candidate", "")
    backup = s.get("draft_backup", "")
    import re

    if not re.fullmatch(r"finalized-[0-9a-f]{32}", pending) or not re.fullmatch(
        r"draft-[0-9a-f]{32}", backup
    ):
        raise TeamError("invalid_runtime")
    finalizer = root / pending
    candidate = root / "candidate"
    selected = finalizer if finalizer.exists() else candidate
    if (
        selected.is_symlink()
        or git_text(selected, "rev-parse", "HEAD") != s["head_revision"]
    ):
        raise TeamError(
            "recovery_required",
            "The finalized copy is not the recorded candidate. Preserve all private copies and replan.",
        )
    verify_native(selected)
    if finalizer.exists():
        if candidate.exists():
            if (root / "token").exists():
                release(candidate, root / "token")
            if (root / backup).exists():
                raise TeamError("recovery_required")
            candidate.rename(root / backup)
        finalizer.rename(candidate)
    s["phase"] = "approved"
    s.pop("pending_candidate", None)
    s["candidate_inventory"] = inventory(candidate)
    write_json(session_file, s)
    return {
        "state": "approved",
        "operation_id": s["operation_id"],
        "private_session_recovered": True,
        "shared_data_changed": False,
    }


@guarded
def supersede(session_file: Path, work_dir: Path, provider: Any) -> dict[str, Any]:
    root, s = root_for_session(session_file)
    authenticated(root, s, provider)
    if s["bootstrap"] or s["phase"] == "published":
        raise TeamError("cannot_supersede")
    # First preserve a new draft from the current canonical revision. Old private data stays intact.
    result = begin(root / "candidate", work_dir, s["operation_id"], provider)
    if s.get("pull_request"):
        pr = provider.pull(s["pull_request"])
        if pr.get("state") == "open":
            provider.close_pr(s["pull_request"])
    if (root / "token").is_file():
        release(root / "candidate", root / "token")
    s["candidate_inventory"] = inventory(root / "candidate")
    s["phase"] = "superseded"
    write_json(session_file, s)
    return {
        **result,
        "previous_private_draft_preserved": True,
        "request_replay": "Replan the original user request on this new snapshot; do not replay a stale technical plan.",
    }


@guarded
def cancel(session_file: Path, provider: Any) -> dict[str, Any]:
    root, s = root_for_session(session_file)
    cfg = authenticated(root, s, provider)
    if s["phase"] in {"finalizing", "cleaned"}:
        raise TeamError("recovery_required")
    done = published_receipt(
        provider,
        cfg,
        s["operation_id"],
        s["principal"]["login"],
        s.get("preview_sha256", ""),
    )
    if done:
        s["phase"] = "published"
        s["published_revision"] = done["revision"]
        write_json(session_file, s)
        return {
            **done,
            "cancelled": False,
            "next_action": "A published change needs a new reviewed revert, not cancellation.",
        }
    if s.get("pull_request"):
        pr = provider.pull(s["pull_request"])
        if pr.get("state") == "open":
            provider.close_pr(s["pull_request"])
    if (root / "token").is_file():
        release(root / "candidate", root / "token")
    s["phase"] = "cancelled"
    s["candidate_inventory"] = inventory(root / "candidate")
    write_json(session_file, s)
    return {
        "state": "cancelled",
        "operation_id": s["operation_id"],
        "private_draft_preserved": True,
        "shared_data_changed": False,
    }


@guarded
def cleanup(
    session_file: Path, provider: Any, discard_private_draft: bool = False
) -> dict[str, Any]:
    root, s = root_for_session(session_file)
    if provider.principal()["id"] != s["principal"]["id"]:
        raise TeamError("identity_changed")
    if s["phase"] not in {"published", "reader"} and not (
        s["phase"] in {"cancelled", "superseded"} and discard_private_draft
    ):
        raise TeamError(
            "private_draft_preserved",
            "Unpublished work is preserved. Cancel/supersede it and explicitly authorize discarding that private draft before cleanup.",
        )
    folders = []
    for name, key in [
        ("candidate", "candidate_inventory"),
        ("base", "base_inventory"),
        ("snapshot", "snapshot_inventory"),
    ]:
        folder = root / name
        if folder.exists():
            if folder.is_symlink() or s.get(key) != inventory(folder):
                raise TeamError(
                    "private_changes_present",
                    "A private workspace contains additional changes. It was preserved.",
                    workspace=name,
                )
            folders.append(folder)
    backup = s.get("draft_backup")
    if backup:
        import re

        if not re.fullmatch(r"draft-[0-9a-f]{32}", backup):
            raise TeamError("invalid_runtime")
        old = root / backup
        if old.exists():
            cfg = load_config(root / "base")
            expected = read_json(root / "preview.json")
            if (
                make_candidate_preview(
                    root / "base",
                    old,
                    cfg,
                    s["principal"]["login"],
                    s["operation_id"],
                    s["base_revision"],
                )
                != expected
            ):
                raise TeamError(
                    "private_changes_present",
                    "The earlier draft has additional data. It was preserved.",
                    workspace=backup,
                )
            folders.append(old)
    for folder in folders:
        shutil.rmtree(folder)
    owned = []
    import re

    for name in s.get("owned_runtime_files", []):
        if not isinstance(name, str) or not re.fullmatch(
            r"request-[0-9a-f]{32}\.json", name
        ):
            raise TeamError("invalid_runtime")
        owned.append(root / name)
    for path in [root / "preview.json", root / "token", *owned]:
        if path.is_file() and not path.is_symlink():
            path.unlink()
    s["phase"] = "cleaned"
    write_json(session_file, s)
    remaining = [
        p.name for p in root.iterdir() if p.name not in {"session.json", "session.lock"}
    ]
    return {
        "state": "cleaned",
        "operation_id": s["operation_id"],
        "original_inputs_and_exports_preserved": remaining,
        "remote_history_erased": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["status", "history-audit", "sync"]:
        p = sub.add_parser(name)
        p.add_argument("--target", required=True)
    p = sub.add_parser("start")
    p.add_argument("--target", required=True)
    p.add_argument("--work-dir", required=True)
    p.add_argument("--operation-id", required=True)
    p = sub.add_parser("setup")
    p.add_argument("--target", required=True)
    p.add_argument("--work-dir", required=True)
    p.add_argument("--operation-id", required=True)
    p.add_argument("--members-file", required=True)
    p.add_argument("--branch", default="main")
    p.add_argument("--runtime-revision")
    p.add_argument("--acknowledge-git-history", action="store_true")
    p = sub.add_parser("read")
    p.add_argument("--target", required=True)
    p.add_argument("--work-dir", required=True)
    for name in ["preview", "submit", "publish", "recover", "cancel"]:
        p = sub.add_parser(name)
        p.add_argument("--session", required=True)
    p = sub.add_parser("cleanup")
    p.add_argument("--session", required=True)
    p.add_argument("--discard-private-draft", action="store_true")
    p = sub.add_parser("supersede")
    p.add_argument("--session", required=True)
    p.add_argument("--work-dir", required=True)
    p = sub.add_parser("approve")
    p.add_argument("--session", required=True)
    p.add_argument("--expect-preview-sha256", required=True)
    p.add_argument("--confirm-destructive", action="store_true")
    p.add_argument("--acknowledge-git-history", action="store_true")
    for name in ["run", "read-run"]:
        p = sub.add_parser(name)
        p.add_argument("--session", required=True)
        p.add_argument("--helper", required=True)
        p.add_argument("arguments", nargs=argparse.REMAINDER)
    p = sub.add_parser("check-pr")
    p.add_argument("--repository", required=True)
    p.add_argument("--number", type=int, required=True)
    p.add_argument("--post-status", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "check-pr":
            result = check_pr(
                args.repository, args.number, GitHub(args.repository), args.post_status
            )
        elif hasattr(args, "session"):
            session = Path(args.session).expanduser().resolve()
            _, s = root_for_session(session)
            provider = GitHub(s["repository"])
            if args.command == "run":
                result = run_helper(session, args.helper, args.arguments, provider)
            elif args.command == "read-run":
                result = read_helper(session, args.helper, args.arguments, provider)
            elif args.command == "preview":
                result = build_preview(session, provider)
            elif args.command == "approve":
                result = approve(
                    session,
                    args.expect_preview_sha256,
                    args.confirm_destructive,
                    args.acknowledge_git_history,
                    provider,
                )
            elif args.command == "submit":
                result = submit(session, provider)
            elif args.command == "recover":
                result = recover(session, provider)
            elif args.command == "cleanup":
                result = cleanup(session, provider, args.discard_private_draft)
            elif args.command == "cancel":
                result = cancel(session, provider)
            elif args.command == "supersede":
                result = supersede(
                    session, Path(args.work_dir).expanduser().resolve(), provider
                )
            else:
                result = publish(session, provider)
        else:
            target = Path(args.target).expanduser().resolve()
            provider = provider_for(target)
            if args.command == "status":
                result = status(target, provider)
            elif args.command == "sync":
                result = sync(target, provider)
            elif args.command == "history-audit":
                result = history_audit(target, provider)
            elif args.command == "read":
                result = read_snapshot(
                    target, Path(args.work_dir).expanduser().resolve(), provider
                )
            elif args.command == "start":
                result = begin(
                    target,
                    Path(args.work_dir).expanduser().resolve(),
                    args.operation_id,
                    provider,
                )
            else:
                revision = args.runtime_revision or GitHub(RUNTIME_REPOSITORY).revision(
                    "main"
                )
                provider.assert_runtime(revision)
                members = read_json(Path(args.members_file).expanduser().resolve())
                for login, member in members.items():
                    if not isinstance(member, dict):
                        raise TeamError("invalid_team_config")
                    login_for(login)
                    member["github_id"] = int(provider.api("/users/" + login)["id"])
                cfg = {
                    "format": "ai-first-crm-team/1",
                    "repository": provider.repository,
                    "branch": args.branch,
                    "runtime_revision": revision,
                    "privacy": {
                        "profile": "git-history",
                        "acknowledged": args.acknowledge_git_history,
                    },
                    "members": members,
                }
                result = begin(
                    target,
                    Path(args.work_dir).expanduser().resolve(),
                    args.operation_id,
                    provider,
                    setup=cfg,
                )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except TeamError as exc:
        print(json.dumps(exc.result(), ensure_ascii=False, indent=2))
        return 3
    except (OSError, ValueError, KeyError) as exc:
        print(
            json.dumps(
                {
                    "state": "runtime_error",
                    "message": "The private team runtime could not complete this operation; no publication is claimed.",
                },
                indent=2,
            )
        )
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
