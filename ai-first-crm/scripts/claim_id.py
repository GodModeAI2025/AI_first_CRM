#!/usr/bin/env python3
"""Generate unused stable claim IDs for one locked AI First CRM."""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Bundled helpers import their siblings; keep that working under python -I, which drops the script directory.
_sys.dont_write_bytecode = True  # no __pycache__ in the skill or the user's cache folders
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import argparse
import json
import re
import secrets
from pathlib import Path

from wiki_lock import require_lock


CLAIM_ID = re.compile(r"\bclm-[0-9a-f]{16}\b")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--lock-token", required=True, help="Token returned by wiki_lock.py acquire")
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args()

    target = Path(args.target).expanduser().resolve()
    require_lock(target, args.lock_token)
    if args.count < 1 or args.count > 1000:
        raise SystemExit("count must be between 1 and 1000")
    if not (target / "wiki").is_dir():
        raise SystemExit("Target is not an initialized AI First CRM")

    existing: set[str] = set()
    for path in (target / "wiki").rglob("*.md"):
        existing.update(CLAIM_ID.findall(path.read_text(encoding="utf-8")))
    generated: list[str] = []
    while len(generated) < args.count:
        claim_id = "clm-" + secrets.token_hex(8)
        if claim_id not in existing and claim_id not in generated:
            generated.append(claim_id)
    print(json.dumps({"claim_ids": generated}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
