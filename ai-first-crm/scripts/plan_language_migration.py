#!/usr/bin/env python3
"""Preview the complete impact of changing AI First CRM's maintained language."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Bundled helpers import their siblings; keep that working under python -I, which drops the script directory.
_sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import argparse
import json
import re
from pathlib import Path
from typing import Any, Optional

from wiki_lock import require_lock


CLAIM_ID = re.compile(r"\bid:\s*(clm-[0-9a-f]{16})\b")


def profile_language(path: Path) -> tuple[str, str]:
    if not path.is_file():
        raise SystemExit("schema/WIKI_PROFILE.md is missing")
    text = path.read_text(encoding="utf-8")
    code = re.search(r'^wiki_language:\s*["\']?([^"\'\s]+)', text, re.MULTILINE)
    label = re.search(r'^wiki_language_label:\s*["\']?(.+?)["\']?\s*$', text, re.MULTILINE)
    if not code or not label:
        raise SystemExit("schema/WIKI_PROFILE.md has no readable wiki language")
    return code.group(1), label.group(1).strip().strip('"\'')


def crm_labels(target: Path) -> Optional[dict[str, Any]]:
    """Labels of the CRM layer that follow the wiki language; field values and API names never do."""
    from crm_contract import DASHBOARDS_PATH, DATAMODEL_PATH, VIEWS_PATH, CrmError, load_json_file

    if not (target / DATAMODEL_PATH).is_file():
        return None
    files = [relative for relative in (DATAMODEL_PATH, VIEWS_PATH, DASHBOARDS_PATH) if (target / relative).is_file()]
    try:
        datamodel = load_json_file(target, DATAMODEL_PATH, {})
        views = load_json_file(target, VIEWS_PATH, {})
        dashboards = load_json_file(target, DASHBOARDS_PATH, {})
    except CrmError as exc:
        return {"files": files, "error": f"CRM labels could not be counted: {exc}"}
    counts = {
        "objects": 0, "object_labels": 0, "field_labels": 0, "option_labels": 0, "descriptions": 0,
        "view_labels": 0, "dashboard_labels": 0, "widget_labels": 0,
    }
    objects = datamodel.get("objects") if isinstance(datamodel, dict) else None
    for definition in (objects or {}).values() if isinstance(objects, dict) else []:
        if not isinstance(definition, dict):
            continue
        counts["objects"] += 1
        counts["object_labels"] += sum(1 for key in ("labelSingular", "labelPlural") if definition.get(key))
        counts["descriptions"] += 1 if definition.get("description") else 0
        fields = definition.get("fields") if isinstance(definition.get("fields"), dict) else {}
        for field in fields.values():
            if not isinstance(field, dict):
                continue
            counts["field_labels"] += 1 if field.get("label") else 0
            counts["descriptions"] += 1 if field.get("description") else 0
            options = field.get("options") if isinstance(field.get("options"), list) else []
            counts["option_labels"] += sum(1 for option in options if isinstance(option, dict) and option.get("label"))
    for view in views.get("views", []) if isinstance(views, dict) else []:
        counts["view_labels"] += 1 if isinstance(view, dict) and view.get("label") else 0
    for dashboard in dashboards.get("dashboards", []) if isinstance(dashboards, dict) else []:
        if not isinstance(dashboard, dict):
            continue
        counts["dashboard_labels"] += 1 if dashboard.get("label") else 0
        widgets = dashboard.get("widgets") if isinstance(dashboard.get("widgets"), list) else []
        counts["widget_labels"] += sum(1 for widget in widgets if isinstance(widget, dict) and widget.get("label"))
    counts["total"] = sum(value for key, value in counts.items() if key != "objects")
    return {
        "files": files,
        "labels_to_translate": counts,
        "unchanged": (
            "Field values of every record, record titles, option API names (values), object and field API names, "
            "view and dashboard IDs and the event log stay exactly as they are; only labels and descriptions follow "
            "the new wiki language."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--lock-token", required=True, help="Token returned by wiki_lock.py acquire")
    parser.add_argument("--to-language", required=True)
    parser.add_argument("--to-label", required=True)
    args = parser.parse_args()

    target = Path(args.target).expanduser().resolve()
    require_lock(target, args.lock_token)
    to_language = args.to_language.strip()
    to_label = args.to_label.strip()
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", to_language):
        raise SystemExit("to-language must be a portable BCP-47-style language code")
    if not to_label:
        raise SystemExit("to-label must not be empty")
    current_language, current_label = profile_language(target / "schema/WIKI_PROFILE.md")
    if current_language.casefold() == to_language.casefold():
        raise SystemExit("The requested wiki language is already configured")

    wiki_pages = sorted((target / "wiki").rglob("*.md"))
    claim_count = sum(len(CLAIM_ID.findall(path.read_text(encoding="utf-8"))) for path in wiki_pages)
    schema_files = [
        relative
        for relative in ("schema/CLUSTERS.md", "schema/CONCEPTS.md")
        if (target / relative).is_file()
    ]
    affected = ["WIKI.md", "schema/WIKI_PROFILE.md", *schema_files]
    affected.extend(path.relative_to(target).as_posix() for path in wiki_pages)
    if (target / "SOUL.md").is_file():
        affected.append("SOUL.md")
    if (target / "schema/CONTENT_POLICY.md").is_file():
        affected.append("schema/CONTENT_POLICY.md")
    crm = crm_labels(target)
    crm_actions: list[str] = []
    crm_preserved: list[str] = []
    if crm is not None:
        affected.extend(crm["files"])
        crm_actions.append(
            "Translate CRM labels and descriptions (objects, fields, options, views, dashboards and widgets) in "
            "schema/crm/ with the CRM schema and view helpers; keep API names, option values and every record value, "
            "then rebuild the CRM views"
        )
        crm_preserved.append("CRM record field values, record titles, option API names and the CRM event log")

    print(
        json.dumps(
            {
                "mode": "preview",
                "from": {"code": current_language, "label": current_label},
                "to": {"code": to_language, "label": to_label},
                "affected_files": affected,
                "wiki_pages": len(wiki_pages),
                "claim_texts": claim_count,
                "unchanged_source_markdown": len(list((target / "sources").glob("*.md"))),
                "required_actions": [
                    "Obtain explicit user confirmation for the complete migration",
                    "Snapshot the current wiki-controlled files",
                    "Translate maintained page titles, descriptions, prose, and claim text",
                    "Translate cluster labels and descriptions plus preferred concept terms and definitions",
                    "Retain stable IDs, source IDs, source locators, paths, and useful old-language aliases",
                    "Set every wiki page language field and WIKI_PROFILE to the target language",
                    "Re-render SOUL.md and schema/CONTENT_POLICY.md with plan_identity.py for the target language, unchanged values, and apply the confirmed proposal with apply_identity.py --confirm-replace",
                    *crm_actions,
                    "Rebuild index, graph, reading views, and run strict lint",
                    "Publish one major release and verify its manifest before unlocking",
                ],
                "preserved": [
                    "sources/*.md content and source language",
                    "claim IDs and source locators",
                    "page IDs and default file paths",
                    "human:keep blocks",
                    "historical snapshots",
                    *crm_preserved,
                ],
                **({"crm": crm} if crm is not None else {}),
                "release_bump": "major",
                "applied": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
