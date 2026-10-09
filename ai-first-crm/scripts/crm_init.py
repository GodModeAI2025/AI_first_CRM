#!/usr/bin/env python3
"""Add the CRM record layer to an initialized wiki, or add missing standard objects.

Creates schema/crm/ (data model, settings, views, dashboards, workflows), the
records/ directory and the event log directory. Existing CRM files are never
overwritten; --add-missing-standard-objects adds standard objects that a data
model does not have yet and leaves every existing definition untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import json
from pathlib import Path

import portable_io
from crm_contract import (
    DASHBOARDS_PATH, DATAMODEL_PATH, EVENTS_DIR, RECORDS_DIR, SETTINGS_PATH, VIEWS_PATH, WORKFLOWS_DIR,
    CrmError, validate_datamodel,
)
from crm_standard import standard_dashboards, standard_datamodel, standard_settings, standard_views
from wiki_lock import require_lock


def wiki_profile(target: Path) -> tuple[str, str]:
    from frontmatter_contract import parse_file

    profile = parse_file(target / "schema/WIKI_PROFILE.md").data
    language = str(profile.get("wiki_language") or "en")
    title = ""
    wiki_md = target / "WIKI.md"
    if wiki_md.is_file():
        for line in wiki_md.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
    return language, title or target.name


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--lock-token", required=True)
    parser.add_argument("--currency", default="EUR", help="Default currency code for amounts, e.g. EUR")
    parser.add_argument("--add-missing-standard-objects", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be created without writing")
    args = parser.parse_args()
    target = Path(args.target).expanduser().resolve()
    require_lock(target, args.lock_token)
    if not (target / "WIKI.md").is_file() or not (target / "schema/WIKI_PROFILE.md").is_file():
        raise SystemExit("Target is not an initialized AI First CRM; initialize the wiki first")
    currency = args.currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise SystemExit("--currency must be a three-letter ISO code")
    language, title = wiki_profile(target)
    created: list[str] = []
    kept: list[str] = []
    added_objects: list[str] = []

    datamodel_path = target / DATAMODEL_PATH
    if datamodel_path.is_file():
        if args.add_missing_standard_objects:
            current = json.loads(datamodel_path.read_text(encoding="utf-8"))
            standard = standard_datamodel(language, currency)
            for name, definition in standard["objects"].items():
                if name not in current.get("objects", {}):
                    current["objects"][name] = definition
                    added_objects.append(name)
            errors = validate_datamodel(current)
            if errors:
                raise SystemExit("; ".join(errors[:10]))
            if added_objects and not args.dry_run:
                from snapshot_wiki import create_snapshot

                create_snapshot(target, args.lock_token, operation="crm-add-standard-objects", selected_files=[DATAMODEL_PATH])
                portable_io.atomic_write_text(datamodel_path, dump(current))
        else:
            kept.append(DATAMODEL_PATH)
    else:
        model = standard_datamodel(language, currency)
        errors = validate_datamodel(model)
        if errors:
            raise CrmError("; ".join(errors))
        if not args.dry_run:
            datamodel_path.parent.mkdir(parents=True, exist_ok=True)
            portable_io.atomic_write_text(datamodel_path, dump(model))
        created.append(DATAMODEL_PATH)

    defaults = {
        SETTINGS_PATH: standard_settings(language, title, currency),
        VIEWS_PATH: standard_views(language),
        DASHBOARDS_PATH: standard_dashboards(language),
    }
    for relative, value in defaults.items():
        path = target / relative
        if path.exists():
            kept.append(relative)
            continue
        if not args.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            portable_io.atomic_write_text(path, dump(value))
        created.append(relative)
    for directory in (WORKFLOWS_DIR, RECORDS_DIR, EVENTS_DIR):
        path = target / directory
        if not path.exists():
            if not args.dry_run:
                path.mkdir(parents=True, exist_ok=True)
            created.append(directory + "/")
    print(json.dumps({
        "state": "dry_run" if args.dry_run else "initialized",
        "wiki_language": language,
        "currency": currency,
        "created": created,
        "kept_existing": kept,
        "added_standard_objects": added_objects,
        "next_steps": [
            "build the CRM views (crm_build.py)",
            "lint (lint_wiki.py) and publish a minor release (release_wiki.py --bump minor)",
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
