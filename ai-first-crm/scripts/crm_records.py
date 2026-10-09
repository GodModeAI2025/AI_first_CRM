#!/usr/bin/env python3
"""Plan and apply hash-bound CRM record transactions; show records read-only.

plan   reads a request JSON (actor, origin, operations) and writes an immutable
       plan outside the wiki. Nothing in the wiki changes.
apply  applies exactly that plan after checking every precondition hash, takes
       a targeted snapshot first and appends the events to the event log.
show   prints one record with its outgoing and incoming relations and timeline.
revert writes a request that undoes one transaction (for example an import): records it
       created are destroyed, records it changed, deleted, merged away or cleared are
       restored from the transaction's snapshot. The request goes through plan and
       apply like any other; an erasure can never be reverted.
cleanup-files
       writes a request that removes stored files no record refers to any more
       (--orphans) or named stored files and outbox drafts (--path, a file or an
       outbox folder). It goes through plan and apply with --confirm-destructive;
       the snapshot keeps the removed files restorable.
acknowledge-credential
       records that a named person confirmed one credential-shaped value in CRM
       data as harmless (for example a company name shaped like a cloud key).
       Only the digest of the value is stored; the value never leaves the record.

Request format (all values are canonical: ISO dates, dot decimals, option API names).
A FILES value stores a file in the wiki with {"name": "Angebot.pdf", "upload": "<runtime path>"};
the plan records its sha256 and apply copies it to records/_files/. "artifacts" writes
outbox drafts ({"path": "records/_outbox/<folder>/<name>.eml", "content": "..."}) or
removes stored files and drafts ({"path": "...", "delete": true}).

{
  "actor": "human:anna@example.com" | "agent/<name>" | "process:<id>",
  "origin": {"kind": "manual|import|email|calendar|workflow|agent|api|migration",
             "ref": "<portable reference>", "occasion": "<what produced it>"},
  "operations": [
    {"op": "create", "object": "company", "ref": "acme", "values": {"name": "Acme GmbH",
       "domainName": "https://acme.example", "address": {"addressCity": "Berlin"}}},
    {"op": "create", "object": "person", "values": {"name": {"firstName": "Ada", "lastName": "Muster"},
       "emails": "ada@acme.example", "company": {"ref": "acme"}}},
    {"op": "update", "object": "opportunity", "record": {"match": {"name": "Relaunch"}},
       "values": {"stage": "PROPOSAL", "amount": {"amount": "12500.50", "currencyCode": "EUR"}}},
    {"op": "upsert", "object": "person", "match": {"emails.primaryEmail": "x@y.example"}, "values": {...}},
    {"op": "delete" | "restore" | "destroy" | "erase", "object": "task", "record": "<uuid>"},
    {"op": "merge", "object": "company", "into": "<uuid>", "from": ["<uuid>"], "prefer": {"name": "<uuid>"}}
  ]
}
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import json
import sys
from pathlib import Path

from crm_contract import (
    StalePlanError,
    CREDENTIAL_ACKS_FORMAT, CREDENTIAL_ACKS_PATH, CRM_DATA_PREFIXES, CrmError, RecordStore, apply_transaction,
    load_credential_acks, load_datamodel, parse_record_link, plan_transaction, read_events, utc_now, valid_actor,
    verify_plan,
)
from wiki_lock import require_lock


def outside(target: Path, path: Path) -> bool:
    return path != target and target not in path.parents


def show(target: Path, object_name: str, record_id: str) -> dict:
    datamodel = load_datamodel(target)
    store = RecordStore(target, datamodel)
    record = store.get(object_name, record_id)
    if record is None:
        raise CrmError(f"{object_name} record {record_id} does not exist")
    incoming = []
    for other_object, field_name, _definition in datamodel.relation_fields_targeting(object_name):
        for other in store.records(other_object).values():
            value = other.data.get(field_name)
            values = value if isinstance(value, list) else ([value] if value else [])
            if any(isinstance(item, str) and parse_record_link(item) and parse_record_link(item)[1] == record_id for item in values):
                incoming.append({"object": other_object, "field": field_name, "id": other.id, "title": other.data.get("crm_title"), "deleted": other.deleted})
    timeline = [
        {key: event.get(key) for key in ("at", "actor", "op", "changes", "origin")}
        for event in read_events(target)
        if event.get("record_id") == record_id
    ]
    return {
        "object": object_name,
        "id": record_id,
        "path": record.path,
        "deleted": record.deleted,
        "data": record.data,
        "richtext": record.richtext,
        "incoming": incoming,
        "timeline": timeline[-200:],
    }


def acknowledge(target: Path, token: str, relative: str, line: int, kind: str, confirmed_by: str, reason: str,
                value_sha256: str = "") -> dict:
    import re
    import secret_screen
    from snapshot_wiki import create_snapshot

    import portable_io

    if not confirmed_by.startswith("human:") or not valid_actor(confirmed_by):
        raise CrmError("--confirmed-by must name the person who confirmed it, as human:<id>")
    if not reason.strip():
        raise CrmError("--reason is required")
    if value_sha256:
        # From a plan's credential error: the value is not in the wiki yet, so it is named by its digest.
        if relative or line:
            raise CrmError("give either --path and --line, or --kind with --value-sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", value_sha256) or kind not in {name for name, _pattern in secret_screen.PATTERNS}:
            raise CrmError("--value-sha256 must be the 64-character digest from the plan and --kind a known credential kind")
        findings = [(0, kind, value_sha256)]
        relative = "plan"
    else:
        if not relative.startswith(CRM_DATA_PREFIXES):
            raise CrmError("acknowledgements apply to CRM data only (records/, meta/crm-events/, meta/crm-runs/)")
        path = target / relative
        if not path.is_file():
            raise CrmError(f"{relative} does not exist")
        findings = [item for item in secret_screen.scan_text_detailed(path.read_text(encoding="utf-8")) if item[0] == line and item[1] == kind]
        if not findings:
            raise CrmError(f"no {kind} finding on line {line} of {relative}")
    entries = load_credential_acks(target)
    known = {(entry.get("kind"), entry.get("value_sha256")) for entry in entries}
    added = []
    for _number, found_kind, digest in findings:
        if (found_kind, digest) in known:
            continue
        entry = {"kind": found_kind, "value_sha256": digest, "first_seen": relative, "confirmed_by": confirmed_by,
                 "confirmed_at": utc_now(), "reason": reason.strip()}
        entries.append(entry)
        added.append(entry)
    if added:
        ack_path = target / CREDENTIAL_ACKS_PATH
        if ack_path.is_file():
            create_snapshot(target, token, operation="crm-credential-acknowledgement", selected_files=[CREDENTIAL_ACKS_PATH])
        ack_path.parent.mkdir(parents=True, exist_ok=True)
        portable_io.atomic_write_text(ack_path, json.dumps({"format": CREDENTIAL_ACKS_FORMAT, "entries": entries}, ensure_ascii=False, indent=2) + "\n")
    return {"state": "acknowledged" if added else "already_acknowledged", "added": len(added), "path": CREDENTIAL_ACKS_PATH}


def cleanup_request(target: Path, actor: str, orphans: bool, paths: list[str]) -> dict:
    from crm_contract import FILES_DIR, artifact_files, stored_record_refs

    datamodel = load_datamodel(target)
    store = RecordStore(target, datamodel)
    records = [record for name in datamodel.objects for record in store.records(name).values()]
    referenced = stored_record_refs(records, datamodel)
    present = artifact_files(target)
    selected: list[str] = []
    if orphans:
        selected += [path for path in present if path.startswith(FILES_DIR + "/") and path not in referenced]
    for raw in paths:
        relative = raw.strip().replace("\\", "/").strip("/")
        folder = [path for path in present if path.startswith(relative + "/")]
        selected += folder if folder else [relative]
    still_used = sorted(path for path in set(selected) if path in referenced)
    if still_used:
        raise CrmError(f"still attached to records, remove the reference first: {', '.join(still_used[:5])}")
    chosen = sorted(set(selected))
    if not chosen:
        raise CrmError("nothing to clean up")
    return {
        "request": {"actor": actor, "origin": {"kind": "manual", "occasion": "cleanup of stored files and outbox drafts"},
                    "operations": [], "artifacts": [{"path": path, "delete": True} for path in chosen]},
        "files": chosen,
    }


def revert_request(target: Path, transaction: str, actor: str) -> dict:
    from crm_contract import SYSTEM_KEYS, parse_record_text

    datamodel = load_datamodel(target)
    events = [event for event in read_events(target) if event.get("txn") == transaction]
    if not events:
        raise CrmError(f"no events of transaction {transaction}")
    if any(event.get("op") == "erase" or event.get("erased") for event in events):
        raise CrmError("an erasure cannot be reverted; its data was removed on purpose")
    snapshot_root = None
    history = target / "meta/history"
    for manifest in sorted(history.glob("*/snapshot.json")) if history.is_dir() else []:
        try:
            if json.loads(manifest.read_text(encoding="utf-8")).get("operation") == f"crm-transaction:{transaction}":
                snapshot_root = manifest.parent
        except (OSError, json.JSONDecodeError):
            continue
    store = RecordStore(target, datamodel)
    touched: list[tuple[str, str]] = []
    created: set[tuple[str, str]] = set()
    for event in events:
        key = (event["object"], event["record_id"])
        if key not in touched:
            touched.append(key)
        if event.get("op") == "create":
            created.add(key)
    later = sorted({
        f"{event['object']} {event['record_id']}"
        for event in read_events(target)
        if (event.get("object"), event.get("record_id")) in set(touched) and event.get("txn") != transaction and event.get("at", "") > events[-1].get("at", "")
    })
    operations: list[dict] = []
    warnings = [f"changed again after the transaction, reverting discards those later changes: {item}" for item in later]
    for object_name, record_id in touched:
        current = store.get(object_name, record_id)
        relative = f"{datamodel.directory(object_name)}/{record_id}.md"
        if (object_name, record_id) in created:
            if current is not None:
                operations.append({"op": "destroy", "object": object_name, "record": record_id})
            continue
        source = snapshot_root / relative if snapshot_root is not None else None
        if source is None or not source.is_file():
            warnings.append(f"no snapshot copy of {object_name} {record_id}; it cannot be restored")
            continue
        before, before_richtext = parse_record_text(source.read_text(encoding="utf-8"), relative)
        values = {key: value for key, value in before.items() if key not in SYSTEM_KEYS}
        if current is not None:
            for key in current.data:
                if key not in SYSTEM_KEYS and key not in values:
                    values[key] = None
        for field_name, definition in datamodel.fields(object_name).items():
            if definition.get("type") == "RICH_TEXT":
                values[field_name] = before_richtext.get(field_name, "")
        was_deleted = bool(before.get("crm_deleted_at"))
        if current is None:
            operations.append({"op": "create", "object": object_name, "id": record_id, "values": values, "source": before.get("crm_created_source") or "MANUAL"})
            if was_deleted:
                operations.append({"op": "delete", "object": object_name, "record": record_id})
            continue
        if current.deleted:
            operations.append({"op": "restore", "object": object_name, "record": record_id})
        operations.append({"op": "update", "object": object_name, "record": record_id, "values": values})
        if was_deleted:
            operations.append({"op": "delete", "object": object_name, "record": record_id})
    if not operations:
        raise CrmError("nothing to revert: the records of this transaction are already gone or unchanged")
    return {
        "request": {"actor": actor, "origin": {"kind": "restore", "note": f"revert {transaction}"}, "operations": operations},
        "snapshot": snapshot_root.relative_to(target).as_posix() if snapshot_root is not None else None,
        "warnings": warnings,
        "summary": {
            "destroy": sum(1 for op in operations if op["op"] == "destroy"),
            "restore_from_snapshot": sum(1 for op in operations if op["op"] in {"update", "create"}),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--target", required=True)
    plan_parser.add_argument("--lock-token", required=True)
    plan_parser.add_argument("--request-file", required=True)
    plan_parser.add_argument("--output", required=True)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--target", required=True)
    apply_parser.add_argument("--lock-token", required=True)
    apply_parser.add_argument("--plan-file", required=True)
    apply_parser.add_argument("--expect-plan-sha256", required=True)
    apply_parser.add_argument("--confirm-destructive", action="store_true")
    show_parser = sub.add_parser("show")
    show_parser.add_argument("--target", required=True)
    show_parser.add_argument("--lock-token", required=True)
    show_parser.add_argument("--object", required=True)
    show_parser.add_argument("--id", required=True)
    revert_parser = sub.add_parser("revert")
    revert_parser.add_argument("--target", required=True)
    revert_parser.add_argument("--lock-token", required=True)
    revert_parser.add_argument("--transaction", required=True, help="Transaction id (txn-...) from the plan or the event log")
    revert_parser.add_argument("--actor", required=True)
    revert_parser.add_argument("--output-request", required=True, help="Request file to write, outside the wiki")
    cleanup_parser = sub.add_parser("cleanup-files")
    cleanup_parser.add_argument("--target", required=True)
    cleanup_parser.add_argument("--lock-token", required=True)
    cleanup_parser.add_argument("--orphans", action="store_true", help="Stored files no record refers to")
    cleanup_parser.add_argument("--path", action="append", default=[], help="A stored file, an outbox draft or an outbox folder (repeatable)")
    cleanup_parser.add_argument("--actor", required=True)
    cleanup_parser.add_argument("--output-request", required=True, help="Request file to write, outside the wiki")
    ack_parser = sub.add_parser("acknowledge-credential")
    ack_parser.add_argument("--target", required=True)
    ack_parser.add_argument("--lock-token", required=True)
    ack_parser.add_argument("--path", default="", help="Vault-relative file named in the lint finding")
    ack_parser.add_argument("--line", default=0, type=int)
    ack_parser.add_argument("--value-sha256", default="", help="Digest from a plan's credential error, instead of --path and --line")
    ack_parser.add_argument("--kind", required=True)
    ack_parser.add_argument("--confirmed-by", required=True, help="human:<id> of the person who confirmed it")
    ack_parser.add_argument("--reason", required=True)
    ack_parser.add_argument("--user-confirmed-harmless", action="store_true", help="Pass only after that person confirmed it")
    args = parser.parse_args()
    target = Path(args.target).expanduser().resolve()
    require_lock(target, args.lock_token)
    try:
        if args.command == "plan":
            output = Path(args.output).expanduser().resolve()
            if not outside(target, output):
                raise CrmError("the plan must be written outside the wiki")
            request = json.loads(Path(args.request_file).read_text(encoding="utf-8"))
            plan = plan_transaction(target, request, allow_uploads=True)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            report = {
                "state": "invalid" if plan["errors"] else "planned",
                "plan_sha256": plan["plan_sha256"],
                "plan_file": output.name,
                "summary": plan["summary"],
                "files": len(plan["files"]),
                "files_to_store": [{"name": Path(entry["source"]).name, "path": entry["path"], "size": entry["size"]} for entry in plan.get("stored_files", [])][:50],
                "artifacts": [{"path": entry["path"], "action": "remove" if entry["after"] is None else "write"} for entry in plan.get("artifacts", [])][:100],
                "destructive": plan["destructive"],
                "erases": plan["erases"],
                "erase_removes_files": plan.get("erase_paths", [])[:100],
                "erase_redacts_records": plan.get("erase_redacted", [])[:100],
                "history_files_to_clean": plan.get("history_purge", [])[:100],
                "errors": plan["errors"][:100],
                "error_count": len(plan["errors"]),
                "warnings": plan["warnings"][:50],
                "changes_preview": [
                    {"path": entry["path"], "action": "remove" if entry["after"] is None else ("update" if entry["before_exists"] else "create")}
                    for entry in plan["files"][:50]
                ],
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1 if plan["errors"] else 0
        if args.command == "apply":
            plan = verify_plan(json.loads(Path(args.plan_file).read_text(encoding="utf-8")), args.expect_plan_sha256)
            result, code = apply_transaction(target, args.lock_token, plan, confirm_destructive=args.confirm_destructive)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return code
        if args.command == "revert":
            output = Path(args.output_request).expanduser().resolve()
            if not outside(target, output):
                raise CrmError("the request must be written outside the wiki")
            result = revert_request(target, args.transaction.strip(), args.actor)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result["request"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"state": "request_written", "request_file": output.name, "snapshot": result["snapshot"],
                              "summary": result["summary"], "warnings": result["warnings"],
                              "next_step": "plan this request with crm_records.py plan, show it, apply after confirmation"}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "cleanup-files":
            output = Path(args.output_request).expanduser().resolve()
            if not outside(target, output):
                raise CrmError("the request must be written outside the wiki")
            if not args.orphans and not args.path:
                raise CrmError("name --orphans or at least one --path")
            result = cleanup_request(target, args.actor, args.orphans, args.path)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result["request"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"state": "request_written", "request_file": output.name, "files": result["files"][:100], "count": len(result["files"]),
                              "next_step": "plan this request with crm_records.py plan, show the list, apply with --confirm-destructive after confirmation"},
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == "acknowledge-credential":
            if not args.user_confirmed_harmless:
                raise CrmError("a person must confirm the value is harmless first; then pass --user-confirmed-harmless")
            if not args.value_sha256 and not (args.path and args.line):
                raise CrmError("give --path and --line from a lint finding, or --value-sha256 from a plan's credential error")
            print(json.dumps(acknowledge(target, args.lock_token, args.path, args.line, args.kind, args.confirmed_by, args.reason,
                                         args.value_sha256), ensure_ascii=False, indent=2))
            return 0
        print(json.dumps(show(target, args.object, args.id.strip().lower()), ensure_ascii=False, indent=2))
        return 0
    except StalePlanError as exc:
        print(json.dumps({"state": "stale_plan", "writes": 0, "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 3
    except (OSError, json.JSONDecodeError, CrmError) as exc:
        print(json.dumps({"state": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
