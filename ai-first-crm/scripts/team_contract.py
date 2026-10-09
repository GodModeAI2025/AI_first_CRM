#!/usr/bin/env python3
"""Pure contracts for Git-backed team work; no service or network access."""
from __future__ import annotations
import hashlib, json, re
from pathlib import Path, PurePosixPath
from typing import Any, Optional

CONFIG_PATH = "schema/team.json"
RECEIPTS_DIR = "meta/team-operations"
FORMAT = "ai-first-crm-team/1"
PREVIEW_FORMAT = "ai-first-crm-team-preview/1"
APPROVAL_FORMAT = "ai-first-crm-team-approval/1"
RECEIPT_FORMAT = "ai-first-crm-team-operation/1"
RUNTIME_REPOSITORY = "GodModeAI2025/AI_first_CRM"
CHECK_CONTEXT = "crm/team-integrity"
ACTIONS_APP_ID = 15368
ROLES = {"reader", "contributor", "maintainer", "owner"}
LOGIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?$", re.I)
SHA = re.compile(r"^[0-9a-f]{40}$")
OP_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
DERIVED = {
    "WIKI_VERSION",
    "meta/manifest.json",
    "meta/releases.jsonl",
    "meta/lint-report.json",
    "meta/quality-status.json",
}
ROOT_FILES = {"WIKI.md", "SOUL.md", "STANDARDS.md", "WIKI_VERSION"}
ALLOWED_DIRS = {"schema", "sources", "wiki", "records", "graph", "meta"}


class TeamError(Exception):
    def __init__(self, state: str, message: str = "", **details: Any):
        self.state = state
        self.message = message or state
        self.details = details
        super().__init__(self.message)

    def result(self) -> dict[str, Any]:
        return {"state": self.state, "message": self.message, **self.details}


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def login_for(value: str) -> str:
    if not isinstance(value, str) or not LOGIN.fullmatch(value):
        raise TeamError("invalid_identity", "A GitHub login is required.")
    return value.lower()


def actor_for(login: str, github_id: Optional[int] = None) -> str:
    name = login_for(login)
    if github_id is not None and (
        isinstance(github_id, bool) or not isinstance(github_id, int) or github_id <= 0
    ):
        raise TeamError("invalid_identity")
    return "human:github:" + str(github_id if github_id is not None else name)


def operation_id(value: str) -> str:
    if not OP_ID.fullmatch(value):
        raise TeamError(
            "invalid_operation", "Use one stable UUIDv4 operation identifier."
        )
    return value


def safe_path(value: str) -> bool:
    p = PurePosixPath(value)
    return (
        bool(value)
        and not p.is_absolute()
        and ".." not in p.parts
        and "." not in p.parts
        and "\\" not in value
        and p.as_posix() == value
        and not any(x.lower() == ".git" for x in p.parts)
    )


def validate_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise TeamError("invalid_team_config", "Unsupported team configuration.")
    repository = raw.get("repository", "")
    if (
        not isinstance(repository, str)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repository
        )
        or repository.endswith(".git")
        or ".." in repository
    ):
        raise TeamError(
            "invalid_team_config", "Use a credential-free owner/repository identity."
        )
    branch = raw.get("branch", "")
    if (
        not isinstance(branch, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_-]{0,100}", branch)
        or branch.startswith("refs/")
        or "// " in branch
        or "//" in branch
        or branch.endswith("/")
        or ".." in branch
    ):
        raise TeamError("invalid_team_config", "Use a safe branch name.")
    if not SHA.fullmatch(str(raw.get("runtime_revision", ""))):
        raise TeamError(
            "invalid_team_config",
            "Pin the trusted product runtime to a full Git commit.",
        )
    privacy = raw.get("privacy")
    if (
        not isinstance(privacy, dict)
        or privacy.get("profile") != "git-history"
        or privacy.get("acknowledged") is not True
    ):
        raise TeamError(
            "history_acknowledgement_required",
            "Git keeps historical data. A team must explicitly accept this storage profile; it is not complete erasure.",
        )
    members = raw.get("members")
    if not isinstance(members, dict) or not members:
        raise TeamError("invalid_team_config", "At least one owner is required.")
    normalized = {}
    for name, member in members.items():
        name = login_for(name)
        if (
            name in normalized
            or not isinstance(member, dict)
            or member.get("role") not in ROLES
        ):
            raise TeamError("invalid_team_config", "Invalid or duplicate team member.")
        if (
            isinstance(member.get("github_id"), bool)
            or not isinstance(member.get("github_id"), int)
            or member["github_id"] <= 0
        ):
            raise TeamError(
                "invalid_team_config",
                "Bind each member to their verified numeric GitHub user ID.",
            )
        if "exports" in member and not isinstance(member["exports"], bool):
            raise TeamError("invalid_team_config", "Export policy must be boolean.")
        if "objects" in member and (
            not isinstance(member["objects"], list)
            or any(
                not isinstance(o, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", o)
                for o in member["objects"]
            )
        ):
            raise TeamError(
                "invalid_team_config", "Object scopes must contain API names."
            )
        denied = member.get("denied_fields", {})
        if not isinstance(denied, dict) or any(
            not isinstance(v, list)
            or any(
                not isinstance(f, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9.]*", f)
                for f in v
            )
            for v in denied.values()
        ):
            raise TeamError("invalid_team_config", "Invalid field restrictions.")
        normalized[name] = member
    if not any(v["role"] == "owner" for v in normalized.values()):
        raise TeamError("invalid_team_config", "At least one owner is required.")
    result = dict(raw)
    result["members"] = normalized
    return result


def load_config(target: Path, required: bool = True) -> Optional[dict[str, Any]]:
    path = target / CONFIG_PATH
    if not path.is_file():
        if required:
            raise TeamError(
                "team_not_configured", "This workspace has no Git team configuration."
            )
        return None
    try:
        return validate_config(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError) as exc:
        raise TeamError(
            "invalid_team_config", "Team configuration cannot be read."
        ) from exc


def permission(
    cfg: dict[str, Any],
    login: str,
    area: str,
    object_name: Optional[str] = None,
    action: str = "update",
    field: Optional[str] = None,
) -> bool:
    member = cfg["members"].get(login_for(login))
    if not member:
        return False
    role = member["role"]
    if area == "read":
        return True
    if area == "export":
        return member.get("exports", role != "reader") is True
    if area == "team-config":
        return role == "owner"
    if area in {
        "schema",
        "knowledge",
        "workflows",
        "erase",
        "merge",
        "destroy",
        "history",
    }:
        return role in {"maintainer", "owner"}
    if area != "records" or role == "reader":
        return False
    if action in {"erase", "destroy", "merge"} and role not in {"maintainer", "owner"}:
        return False
    if object_name and "objects" in member and object_name not in member["objects"]:
        return False
    if field and (
        field in member.get("denied_fields", {}).get(object_name, [])
        or field.split(".")[0] in member.get("denied_fields", {}).get(object_name, [])
    ):
        return False
    return True


def native_roles(cfg: dict[str, Any]) -> dict[str, Any]:
    roles = {}
    assignments = {}
    for login, member in cfg["members"].items():
        objects = {}
        names = member.get("objects", ["*"])
        for name in names:
            elevated = member["role"] in {"maintainer", "owner"}
            objects[name] = {
                "read": True,
                "update": member["role"] != "reader",
                "delete": member["role"] != "reader",
                "restore": member["role"] != "reader",
                "destroy": elevated,
                "erase": elevated,
                "merge": elevated,
                "fields": {
                    f: {"update": False}
                    for f in member.get("denied_fields", {}).get(name, [])
                },
            }
        roles[login] = {"objects": objects}
        assignments[actor_for(login, member["github_id"])] = login
    return {"enforcement": "team", "roles": roles, "assignments": assignments}


def make_preview(
    op: str,
    base: str,
    actor: str,
    changes: list[dict[str, Any]],
    warnings: list[str],
    destructive: bool,
) -> dict[str, Any]:
    operation_id(op)
    if not SHA.fullmatch(base):
        raise TeamError("invalid_revision")
    payload = {
        "format": PREVIEW_FORMAT,
        "operation_id": op,
        "base_revision": base,
        "actor": login_for(actor),
        "changes": changes,
        "warnings": warnings,
        "destructive": bool(destructive),
        "retains_git_history": True,
    }
    return {**payload, "preview_sha256": digest(payload)}


def check_preview(preview: dict[str, Any]) -> None:
    if preview.get("format") != PREVIEW_FORMAT or preview.get(
        "preview_sha256"
    ) != digest({k: v for k, v in preview.items() if k != "preview_sha256"}):
        raise TeamError(
            "stale_preview", "The preview was changed; create and review a new preview."
        )


def approval_for(
    preview: dict[str, Any],
    actor: str,
    expected: str,
    confirm_destructive: bool,
    ack_history: bool,
) -> dict[str, Any]:
    check_preview(preview)
    if expected != preview["preview_sha256"] or login_for(actor) != preview["actor"]:
        raise TeamError(
            "stale_preview", "Approval must match the preview and authenticated user."
        )
    if preview["destructive"] and not confirm_destructive:
        raise TeamError(
            "confirmation_required",
            "Destructive changes require explicit approval of the preview.",
        )
    if preview["destructive"] and preview["retains_git_history"] and not ack_history:
        raise TeamError(
            "history_acknowledgement_required",
            "Current data may be removed, but previous Git versions remain.",
        )
    return {
        "format": APPROVAL_FORMAT,
        "operation_id": preview["operation_id"],
        "base_revision": preview["base_revision"],
        "actor": preview["actor"],
        "preview_sha256": expected,
        "confirm_destructive": bool(confirm_destructive),
        "acknowledge_history": bool(ack_history),
    }


def validate_approval(
    preview: dict[str, Any], approval: dict[str, Any], actor: str
) -> None:
    check_preview(preview)
    if approval.get("format") != APPROVAL_FORMAT:
        raise TeamError("approval_required")
    wanted = approval_for(
        preview,
        actor,
        approval.get("preview_sha256", ""),
        approval.get("confirm_destructive") is True,
        approval.get("acknowledge_history") is True,
    )
    if approval != wanted:
        raise TeamError(
            "stale_preview", "Approval no longer binds this operation and base."
        )


def payload_files(target: Path) -> dict[str, bytes]:
    """All tracked workspace files except Git metadata and the transient claim."""
    result = {}
    for p in sorted(target.rglob("*")):
        parts = p.relative_to(target).parts
        if (
            ".git" in parts
            or parts[0] == ".llmwiki.lock"
            or "__pycache__" in parts
            or p.name in {".DS_Store", "Thumbs.db"}
        ):
            continue
        if p.is_symlink():
            raise TeamError(
                "unsafe_workspace", "Symbolic links are not allowed in team snapshots."
            )
        if p.is_file():
            result[p.relative_to(target).as_posix()] = p.read_bytes()
    return result


def change_set(base: Path, candidate: Path) -> list[dict[str, Any]]:
    before = payload_files(base)
    after = payload_files(candidate)
    result = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        if (
            path in DERIVED
            or path.startswith("graph/")
            or path.startswith(RECEIPTS_DIR + "/")
        ):
            continue
        item = {
            "path": path,
            "action": (
                "create"
                if path not in before
                else ("remove" if path not in after else "update")
            ),
            "before_sha256": (
                hashlib.sha256(before[path]).hexdigest() if path in before else ""
            ),
            "after_sha256": (
                hashlib.sha256(after[path]).hexdigest() if path in after else ""
            ),
            "before_bytes": len(before.get(path, b"")),
            "after_bytes": len(after.get(path, b"")),
        }
        # Plain-text previews are private runtime files, never committed into the data repository.
        if path.endswith((".md", ".json", ".jsonl", ".yml", ".yaml", ".txt")):
            for label, source in [("before", before), ("after", after)]:
                if path in source:
                    try:
                        item[label] = source[path].decode("utf-8")
                    except UnicodeDecodeError:
                        pass
        result.append(item)
    return result


def derived_link_labels(candidate: Path, dm: Any, old: Any, new: Any) -> bool:
    """Native rename propagation changes labels, not relation identities or audit fields."""
    from crm_contract import parse_record_link, load_record, record_link

    if (
        old is None
        or new is None
        or old.object != new.object
        or old.richtext != new.richtext
    ):
        return False
    changed = [
        key
        for key in set(old.data) | set(new.data)
        if old.data.get(key) != new.data.get(key)
    ]
    if not changed:
        return False
    for field in changed:
        if dm.fields(new.object).get(field, {}).get("type") not in {
            "RELATION",
            "MORPH_RELATION",
        }:
            return False
        a = old.data.get(field)
        b = new.data.get(field)
        a = a if isinstance(a, list) else [a]
        b = b if isinstance(b, list) else [b]
        if len(a) != len(b):
            return False
        for before, after in zip(a, b):
            left = parse_record_link(before)
            right = parse_record_link(after)
            if left is None or right != left:
                return False
            obj = dm.object_for_directory(right[0])
            if obj is None:
                return False
            target = load_record(
                candidate,
                dm,
                obj,
                candidate / "records" / right[0] / (right[1] + ".md"),
            )
            if after != record_link(dm, obj, target.id, target.data["crm_title"]):
                return False
    return True


def authorize_changes(
    base: Path, candidate: Path, cfg: dict[str, Any], actor: str
) -> dict[str, Any]:
    """Authorize the complete data diff, independent of the client's claimed helper."""
    from crm_contract import load_datamodel, load_record, compose_record

    before = payload_files(base)
    after = payload_files(candidate)
    destructive = False
    changed_records = []
    if actor not in cfg["members"]:
        raise TeamError("permission_denied", "Unknown team identity.")
    dm = (
        load_datamodel(candidate)
        if (candidate / "schema/crm/datamodel.json").is_file()
        else None
    )
    old_dm = (
        load_datamodel(base) if (base / "schema/crm/datamodel.json").is_file() else None
    )
    model_changed = before.get("schema/crm/datamodel.json") != after.get(
        "schema/crm/datamodel.json"
    )
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        if not safe_path(path):
            raise TeamError("unsafe_workspace")
        if (
            path in DERIVED
            or path.startswith("graph/")
            or path.startswith(RECEIPTS_DIR + "/")
        ):
            continue
        parts = PurePosixPath(path).parts
        if (
            path == CONFIG_PATH
            or parts[0] == ".github"
            or path in {".gitignore", ".gitattributes"}
        ):
            if not permission(cfg, actor, "team-config"):
                raise TeamError(
                    "permission_denied",
                    "Only an owner can change team configuration or repository checks.",
                )
        elif parts[0] == "records":
            if parts[1] in {"_files", "_outbox"}:
                if not permission(cfg, actor, "records"):
                    raise TeamError("permission_denied")
                destructive |= path not in after
                continue
            if (
                dm is None
                or old_dm is None
                or len(parts) != 3
                or not path.endswith(".md")
            ):
                raise TeamError("invalid_record_change")
            old_obj = old_dm.object_for_directory(parts[1]) if path in before else None
            new_obj = dm.object_for_directory(parts[1]) if path in after else None
            obj = new_obj or old_obj
            if obj is None:
                raise TeamError("invalid_record_change")
            old = load_record(base, old_dm, old_obj, base / path) if old_obj else None
            new = (
                load_record(candidate, dm, new_obj, candidate / path)
                if new_obj
                else None
            )
            # An object rename moves a stable record UUID to a new folder.
            moved = False
            event_obj = obj
            if model_changed and (old is None or new is None):
                counterpart = []
                other = before if old is None else after
                for other_path in other:
                    bits = PurePosixPath(other_path).parts
                    if (
                        len(bits) == 3
                        and bits[0] == "records"
                        and bits[2] == parts[2]
                        and other_path != path
                        and other_path not in (after if old is None else before)
                    ):
                        counterpart.append(other_path)
                if len(counterpart) == 1:
                    other_path = counterpart[0]
                    other_dir = PurePosixPath(other_path).parts[1]
                    if old is None:
                        counterpart_obj = old_dm.object_for_directory(other_dir)
                        if counterpart_obj:
                            old = load_record(
                                base, old_dm, counterpart_obj, base / other_path
                            )
                            moved = True
                    else:
                        counterpart_obj = dm.object_for_directory(other_dir)
                        if counterpart_obj:
                            new = load_record(
                                candidate, dm, counterpart_obj, candidate / other_path
                            )
                            event_obj = counterpart_obj
                            moved = True
            action = (
                "update"
                if moved
                else (
                    "create"
                    if path not in before
                    else ("destroy" if path not in after else "update")
                )
            )
            derived = derived_link_labels(candidate, dm, old, new) if new else False
            if not derived and not permission(cfg, actor, "records", obj, action):
                raise TeamError(
                    "permission_denied",
                    "This member may not change this record.",
                    path=path,
                )
            migrating = bool(
                model_changed
                and old
                and new
                and permission(cfg, actor, "schema")
                and old.data.get("crm_updated_by") == new.data.get("crm_updated_by")
            )
            if new:
                if (
                    compose_record(dm, new).encode("utf-8") != after.get(path)
                    and path in after
                ):
                    raise TeamError(
                        "noncanonical_record",
                        "Changed records must use the native canonical format.",
                        path=path,
                    )
                if (
                    not derived
                    and not migrating
                    and new.data.get("crm_updated_by")
                    != actor_for(actor, cfg["members"][actor]["github_id"])
                ):
                    raise TeamError(
                        "actor_mismatch",
                        "The record actor must be the authenticated GitHub user.",
                        path=path,
                    )
                if old is None and new.data.get("crm_created_by") != actor_for(
                    actor, cfg["members"][actor]["github_id"]
                ):
                    raise TeamError(
                        "actor_mismatch",
                        "New records must identify their authenticated creator.",
                        path=path,
                    )
            old_data = old.data if old else {}
            new_data = new.data if new else {}
            if not derived:
                for field in set(old_data) | set(new_data):
                    if old_data.get(field) == new_data.get(field) or field.startswith(
                        "crm_"
                    ):
                        continue
                    if not permission(cfg, actor, "records", obj, action, field):
                        raise TeamError(
                            "permission_denied",
                            "This field is outside the member scope.",
                            path=path,
                            field=field,
                        )
                for field in set(old.richtext if old else {}) | set(
                    new.richtext if new else {}
                ):
                    if (old.richtext.get(field) if old else None) != (
                        new.richtext.get(field) if new else None
                    ) and not permission(cfg, actor, "records", obj, action, field):
                        raise TeamError(
                            "permission_denied",
                            "This rich-text field is outside the member scope.",
                            path=path,
                            field=field,
                        )
            if path not in after and not moved:
                destructive = True
            changed_records.append(
                {
                    "object": event_obj,
                    "id": (new or old).id,
                    "action": action,
                    "derived_labels": derived,
                }
            )
        elif parts[0] == "schema":
            if not permission(cfg, actor, "schema"):
                raise TeamError(
                    "permission_denied",
                    "Only maintainers may change the model, settings, views or workflows.",
                )
            destructive |= path not in after
        elif parts[0] in {"sources", "wiki"} or path in {
            "WIKI.md",
            "SOUL.md",
            "STANDARDS.md",
        }:
            if parts[0] == "wiki":
                from navigation import expected_indexes, _without_updated

                expected = expected_indexes(candidate, "1970-01-01")
                rendered = after.get(path, b"").decode("utf-8")
                if path in expected and _without_updated(rendered) == _without_updated(
                    expected[path]
                ):
                    continue
            if not permission(cfg, actor, "knowledge"):
                raise TeamError(
                    "permission_denied", "Knowledge maintenance requires a maintainer."
                )
            destructive |= path not in after
        elif parts[0] == "meta":
            if (
                path.startswith("meta/history/")
                or path.startswith("meta/crm-events/")
                or path.startswith("meta/crm-runs/")
                or path == "meta/crm-workflow-state.json"
            ):
                if not permission(cfg, actor, "records"):
                    raise TeamError("permission_denied")
            elif not permission(cfg, actor, "knowledge"):
                raise TeamError("permission_denied")
        else:
            raise TeamError(
                "unexpected_path",
                "Changes outside the CRM workspace contract are not accepted.",
                path=path,
            )
    return {"destructive": destructive, "changed_records": changed_records}
