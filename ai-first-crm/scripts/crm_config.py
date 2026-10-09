#!/usr/bin/env python3
"""Plan and apply hash-bound changes of CRM configuration documents.

Views, dashboards, settings and roles are JSON documents under schema/crm/.
`plan` validates a complete new version of one document against the data model,
reports which entries it adds, changes and removes, and writes an immutable plan
outside the wiki. `apply` writes exactly that plan after checking that the
document did not change in between, with a snapshot first. Data model changes
use crm_schema.py and workflow definitions use crm_workflows.py instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse  # noqa: E402
import json  # noqa: E402
from typing import Any  # noqa: E402

from crm_contract import (  # noqa: E402
    DASHBOARDS_PATH, ROLES_PATH, SETTINGS_PATH, VIEWS_PATH, CrmError, canonical_json, load_datamodel, load_json_file,
    sha256_bytes, valid_actor,
)
from wiki_lock import require_lock  # noqa: E402

PLAN_FORMAT = "lmwiki-crm-config/1"
DOCUMENTS = {
    "views": (VIEWS_PATH, "lmwiki-crm-views/1", "views"),
    "dashboards": (DASHBOARDS_PATH, "lmwiki-crm-dashboards/1", "dashboards"),
    "settings": (SETTINGS_PATH, "lmwiki-crm-settings/1", None),
    "roles": (ROLES_PATH, "lmwiki-crm-roles/1", None),
}
ROLE_FLAGS = {"read", "update", "delete", "destroy"}


def validate_roles(document: dict[str, Any], object_names: set[str]) -> list[str]:
    errors: list[str] = []
    if document.get("enforcement") not in ("cooperative", "off"):
        errors.append("enforcement must be cooperative or off")
    roles = document.get("roles")
    if not isinstance(roles, dict) or not roles:
        return errors + ["roles must be a non-empty mapping"]
    for name, role in roles.items():
        objects = role.get("objects") if isinstance(role, dict) else None
        if not isinstance(objects, dict):
            errors.append(f"role {name}: objects must be a mapping")
            continue
        for object_name, rules in objects.items():
            if object_name != "*" and object_name not in object_names:
                errors.append(f"role {name}: unknown object {object_name}")
            if not isinstance(rules, dict):
                errors.append(f"role {name}.{object_name}: rules must be a mapping")
                continue
            for flag in set(rules) - ROLE_FLAGS - {"fields"}:
                errors.append(f"role {name}.{object_name}: unknown permission {flag}")
            for flag in ROLE_FLAGS & set(rules):
                if not isinstance(rules[flag], bool):
                    errors.append(f"role {name}.{object_name}.{flag} must be true or false")
    default = document.get("default_role")
    if default is not None and default not in roles:
        errors.append(f"default_role {default!r} is not a role")
    for actor, role in (document.get("assignments") or {}).items():
        pattern = actor[:-1] if actor.endswith("*") else actor
        if not (valid_actor(actor) or pattern in ("agent/", "human:", "process:")):
            errors.append(f"assignment {actor!r} is not an actor or an actor prefix ending in *")
        if role not in roles:
            errors.append(f"assignment {actor!r} names unknown role {role!r}")
    return errors


def validate_settings(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    code = str(document.get("default_currency") or "")
    if len(code) != 3 or not code.isalpha() or not code.isupper():
        errors.append("default_currency must be a three-letter ISO code")
    for key, allowed in (
        ("date_format", {"SYSTEM", "MONTH_FIRST", "DAY_FIRST", "YEAR_FIRST"}),
        ("time_format", {"SYSTEM", "HOUR_24", "HOUR_12"}),
        ("number_format", {"SYSTEM", "COMMAS_AND_DOT", "DOTS_AND_COMMA", "SPACES_AND_COMMA", "APOSTROPHE_AND_DOT"}),
    ):
        if key in document and document[key] not in allowed:
            errors.append(f"{key} must be one of {sorted(allowed)}")
    if "calendar_start_day" in document and document["calendar_start_day"] not in (1, 6, 7):
        errors.append("calendar_start_day must be 1 (Monday), 6 (Saturday) or 7 (Sunday)")
    email = document.get("email", {})
    if not isinstance(email, dict):
        errors.append("email must be a mapping")
    else:
        policy = str(email.get("contact_creation", "SENT"))
        if policy.upper() not in {"NONE", "SENT", "SENT_AND_RECEIVED", "WORK-DOMAINS"}:
            errors.append("email.contact_creation must be NONE, SENT, SENT_AND_RECEIVED or work-domains")
        for key in ("blocklist", "excluded_handles", "free_email_domains", "own_addresses", "own_domains"):
            if key in email and (not isinstance(email[key], list) or any(not isinstance(item, str) for item in email[key])):
                errors.append(f"email.{key} must be a list of texts")
    return errors


def storage_name_problems(document: dict[str, Any], list_key: str, folder: str) -> list[str]:
    """Ids become file names; refuse those OneDrive or SharePoint would reject or take for a conflict copy."""
    import sync_artifacts

    problems = []
    for item in document.get(list_key, []) if isinstance(document.get(list_key), list) else []:
        identifier = item.get("id") if isinstance(item, dict) else None
        if isinstance(identifier, str) and identifier:
            artifact = sync_artifacts.classify(f"{folder}/{identifier}.html")
            if artifact is not None:
                problems.append(f"{list_key} id {identifier!r} gives a file name the storage treats specially ({artifact.reason}); choose another id")
    return problems


def validate_document(target: Path, kind: str, document: Any) -> list[str]:
    relative, expected, _list_key = DOCUMENTS[kind]
    if not isinstance(document, dict) or document.get("format") != expected:
        return [f"{relative}: format must be {expected}"]
    datamodel = load_datamodel(target)
    if kind == "views":
        import crm_views

        return crm_views.validate_views(datamodel, document) + storage_name_problems(document, "views", "graph/crm/views")
    if kind == "dashboards":
        import crm_views

        views = load_json_file(target, VIEWS_PATH, {"format": "lmwiki-crm-views/1", "views": []})
        return crm_views.validate_dashboards(datamodel, document, views) + storage_name_problems(document, "dashboards", "graph/crm/dashboards")
    if kind == "roles":
        return validate_roles(document, set(datamodel.objects))
    return validate_settings(document)


def entry_changes(before: Any, after: dict[str, Any], list_key: str) -> dict[str, list[str]]:
    old = {item.get("id"): item for item in (before or {}).get(list_key, []) if isinstance(item, dict)}
    new = {item.get("id"): item for item in after.get(list_key, []) if isinstance(item, dict)}
    return {
        "added": sorted(str(key) for key in set(new) - set(old)),
        "removed": sorted(str(key) for key in set(old) - set(new)),
        "changed": sorted(str(key) for key in set(old) & set(new) if canonical_json(old[key]) != canonical_json(new[key])),
    }


def plan(target: Path, kind: str, document: Any) -> dict[str, Any]:
    relative, _expected, list_key = DOCUMENTS[kind]
    path = target / relative
    before_bytes = path.read_bytes() if path.is_file() else b""
    before = json.loads(before_bytes.decode("utf-8")) if before_bytes else None
    errors = validate_document(target, kind, document)
    after_text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    payload = {
        "format": PLAN_FORMAT,
        "document": kind,
        "path": relative,
        "before_exists": path.is_file(),
        "before_sha256": sha256_bytes(before_bytes) if path.is_file() else "",
        "after": after_text,
        "after_sha256": sha256_bytes(after_text.encode("utf-8")),
        "changes": entry_changes(before, document, list_key) if list_key else {},
        "removes_entries": bool(list_key and entry_changes(before, document, list_key)["removed"]),
        "errors": errors,
    }
    return {**payload, "plan_sha256": sha256_bytes(canonical_json(payload))}


def apply(target: Path, token: str, plan_doc: dict[str, Any], expected: str, confirm_removal: bool) -> tuple[dict[str, Any], int]:
    import portable_io
    from snapshot_wiki import create_snapshot

    payload = {key: value for key, value in plan_doc.items() if key != "plan_sha256"}
    if plan_doc.get("format") != PLAN_FORMAT or plan_doc.get("plan_sha256") != expected or sha256_bytes(canonical_json(payload)) != expected:
        return {"state": "stale_plan", "writes": 0, "reason": "the plan was modified or does not match the confirmed hash"}, 3
    if plan_doc.get("errors"):
        return {"state": "invalid_plan", "writes": 0, "errors": plan_doc["errors"]}, 4
    if plan_doc.get("removes_entries") and not confirm_removal:
        return {"state": "confirmation_required", "writes": 0, "reason": "the new document removes entries; confirm with --confirm-removal"}, 3
    path = target / plan_doc["path"]
    if path.is_file() != bool(plan_doc["before_exists"]) or (path.is_file() and sha256_bytes(path.read_bytes()) != plan_doc["before_sha256"]):
        return {"state": "stale_plan", "writes": 0, "reason": f"{plan_doc['path']} changed after planning"}, 3
    snapshot = create_snapshot(target, token, operation=f"crm-config-{plan_doc['document']}", selected_files=[plan_doc["path"]]) if path.is_file() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    portable_io.atomic_write_text(path, plan_doc["after"])
    return {"state": "applied", "path": plan_doc["path"], "changes": plan_doc.get("changes"), "snapshot": snapshot,
            "next_step": "rebuild the CRM views (crm_build.py), lint, and publish a minor release"}, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--target", required=True)
    plan_parser.add_argument("--lock-token", required=True)
    plan_parser.add_argument("--document", required=True, choices=sorted(DOCUMENTS))
    plan_parser.add_argument("--input", required=True, help="Complete new JSON document, outside the wiki")
    plan_parser.add_argument("--output", required=True)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--target", required=True)
    apply_parser.add_argument("--lock-token", required=True)
    apply_parser.add_argument("--plan-file", required=True)
    apply_parser.add_argument("--expect-plan-sha256", required=True)
    apply_parser.add_argument("--confirm-removal", action="store_true")
    args = parser.parse_args()
    target = Path(args.target).expanduser().resolve()
    require_lock(target, args.lock_token)
    try:
        if args.command == "plan":
            output = Path(args.output).expanduser().resolve()
            if output == target or target in output.parents:
                raise CrmError("the plan must be written outside the wiki")
            document = json.loads(Path(args.input).read_text(encoding="utf-8"))
            result = plan(target, args.document, document)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({key: result[key] for key in ("document", "path", "changes", "removes_entries", "errors", "plan_sha256")}, ensure_ascii=False, indent=2))
            return 1 if result["errors"] else 0
        plan_doc = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
        result, code = apply(target, args.lock_token, plan_doc, args.expect_plan_sha256, args.confirm_removal)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except (OSError, json.JSONDecodeError, CrmError) as exc:
        print(json.dumps({"state": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
