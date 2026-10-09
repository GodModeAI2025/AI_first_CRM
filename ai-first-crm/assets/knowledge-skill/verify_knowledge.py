#!/usr/bin/env python3
"""Verify this skill's bundled frozen wiki without modifying it.

An export made with --exclude-crm leaves the CRM record data out on purpose. It
declares the left-out prefixes in references/SNAPSHOT.json; only the prefixes in
EXCLUDABLE_PREFIXES may be declared, their manifest entries are reported as
excluded instead of missing, and a file that is present below one of them is an
error, because the export promised that it is not there.
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


ROOT_FILES = ("WIKI.md", "WIKI_VERSION", "SOUL.md", "STANDARDS.md")
# records/ and the CRM logs are released data of the optional CRM layer.
CONTROLLED_DIRS = ("schema", "sources", "wiki", "graph", "records", "meta/crm-events", "meta/crm-runs", "meta/team-operations")
META_FILES = (
    "meta/sources.jsonl",
    "meta/changes.md",
    "meta/questions.md",
    "meta/lint-report.json",
    "meta/releases.jsonl",
    "meta/quality-reviews.jsonl",
    "meta/quality-status.json",
    "meta/crm-workflow-state.json",
)
# The only release parts an export may leave out deliberately (personal CRM data).
EXCLUDABLE_PREFIXES = (
    "schema/team.json",
    "meta/team-operations/",
    "records/",
    "meta/crm-events/",
    "meta/crm-runs/",
    "meta/crm-workflow-state.json",
    "graph/crm/",
)


def knowledge_root() -> Path:
    return Path(__file__).resolve().parent.parent / "references" / "knowledge"


def snapshot_path() -> Path:
    return Path(__file__).resolve().parent.parent / "references" / "SNAPSHOT.json"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def is_excluded(relative: str, prefixes: list[str]) -> bool:
    return any(relative == prefix or (prefix.endswith("/") and relative.startswith(prefix)) for prefix in prefixes)


def declared_exclusions() -> tuple[list[str], list[str]]:
    """Prefixes this export left out on purpose, and problems with that declaration."""
    path = snapshot_path()
    if not path.is_file():
        return [], []
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [], [f"references/SNAPSHOT.json is unreadable: {exc}"]
    if not isinstance(snapshot, dict):
        return [], ["references/SNAPSHOT.json must be a JSON object"]
    prefixes = snapshot.get("excluded_prefixes", [])
    if not isinstance(prefixes, list) or any(not isinstance(item, str) for item in prefixes):
        return [], ["references/SNAPSHOT.json: excluded_prefixes must be a list of texts"]
    unknown = [item for item in prefixes if item not in EXCLUDABLE_PREFIXES]
    if unknown:
        return [], [f"references/SNAPSHOT.json excludes parts that may not be left out: {unknown}"]
    return sorted(set(prefixes)), []


def controlled_paths(target: Path) -> set[str]:
    paths: set[str] = set()
    for name in ROOT_FILES:
        if (target / name).is_file():
            paths.add(name)
    for directory in CONTROLLED_DIRS:
        root = target / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not path.name.startswith(".") and not path.name.endswith(".tmp"):
                paths.add(path.relative_to(target).as_posix())
    for name in META_FILES:
        if (target / name).is_file():
            paths.add(name)
    return paths


def verify() -> dict[str, Any]:
    target = knowledge_root()
    manifest_path = target / "meta/manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        return {"state": "invalid_snapshot", "reason": f"Release manifest is unavailable or invalid: {exc}"}
    if not isinstance(manifest, dict) or manifest.get("format") != "lmwiki-release/1":
        return {"state": "invalid_snapshot", "reason": "Unsupported release manifest format"}
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return {"state": "invalid_snapshot", "reason": "Release manifest has no files"}

    excluded, errors = declared_exclusions()
    excluded_count = 0
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            errors.append("Manifest contains a non-object entry")
            continue
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative:
            errors.append("Manifest contains an empty path")
            continue
        path_value = PurePosixPath(relative)
        windows = PureWindowsPath(relative)
        if (
            path_value.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or ".." in path_value.parts
            or ".." in windows.parts
            or "\\" in relative
            or path_value.as_posix() != relative
        ):
            errors.append(f"Manifest path is not portable: {relative}")
            continue
        if relative in seen:
            errors.append(f"Manifest path is duplicated: {relative}")
            continue
        seen.add(relative)
        path = target / relative
        if is_excluded(relative, excluded):
            excluded_count += 1
            if path.exists():
                errors.append(f"File below an excluded prefix is present: {relative}")
            continue
        try:
            content = path.read_bytes()
        except OSError:
            errors.append(f"Released file is missing: {relative}")
            continue
        if sha256_bytes(content) != entry.get("sha256"):
            errors.append(f"Released file hash differs: {relative}")
        if len(content) != entry.get("bytes"):
            errors.append(f"Released file size differs: {relative}")
    unexpected = sorted(controlled_paths(target) - seen)
    if unexpected:
        errors.append(f"Files exist outside the frozen manifest: {unexpected}")
    version_path = target / "WIKI_VERSION"
    version = version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else ""
    if version != manifest.get("version"):
        errors.append("WIKI_VERSION differs from the frozen manifest")
    if errors:
        return {
            "state": "invalid_snapshot",
            "version": version,
            "manifest_sha256": sha256_bytes(manifest_bytes),
            "errors": errors,
        }
    return {
        "state": "ready",
        "version": version,
        "release_id": manifest.get("release_id", ""),
        "released_at": manifest.get("released_at", ""),
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "files": len(files),
        "verified_files": len(files) - excluded_count,
        "excluded_files": excluded_count,
        "excluded_prefixes": excluded,
    }


def main() -> int:
    report = verify()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("state") == "ready" else 4


if __name__ == "__main__":
    raise SystemExit(main())
