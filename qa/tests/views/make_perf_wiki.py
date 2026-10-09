#!/usr/bin/env python3
"""Create the load-test wiki for CRM views: 5,000 companies, 5,000 people,
2,000 opportunities and 3,000 tasks, written through the transaction planner.

Usage: make_perf_wiki.py [--force]
The wiki lands in test-wikis/views/perf-de. Batches use distinct planning
moments so every batch writes a fresh monthly event shard.
"""
from __future__ import annotations

import json
import random
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent.parent
sys.path.insert(0, str(WORKSPACE / "tools"))
from harness import SCRIPTS, Wiki  # noqa: E402

sys.path.insert(0, str(SCRIPTS))
import crm_contract  # noqa: E402

BASE = WORKSPACE / "test-wikis" / "views" / "base-de"
TARGET = WORKSPACE / "test-wikis" / "views" / "perf-de"
TOKENS = WORKSPACE / "test-wikis" / "views" / ".tokens"

FIRST = ["Anna", "Ben", "Clara", "David", "Eva", "Felix", "Greta", "Hannes", "Ida", "Jonas", "Klara", "Lukas",
         "Mia", "Noah", "Olga", "Paul", "Quirin", "Rosa", "Sven", "Tara", "Ute", "Viktor", "Wanda", "Yusuf", "Zoe"]
LAST = ["Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker", "Schulz", "Hoffmann",
        "Koch", "Richter", "Klein", "Wolf", "Schröder", "Neumann", "Schwarz", "Zimmermann", "Braun", "Krüger"]
CITIES = ["Berlin", "Hamburg", "München", "Köln", "Frankfurt am Main", "Stuttgart", "Düsseldorf", "Leipzig",
          "Dortmund", "Essen", "Bremen", "Dresden", "Hannover", "Nürnberg", "Freiburg"]
STAGES = ["NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER"]
STATUSES = ["TODO", "IN_PROGRESS", "DONE"]


def batch_moment(index: int) -> str:
    year = 2025 + index // 12
    month = index % 12 + 1
    return f"{year}-{month:02d}-15T10:00:00Z"


def run_batch(target: Path, token: str, operations: list, index: int) -> None:
    started = time.time()
    plan = crm_contract.plan_transaction(target, {
        "actor": "agent/loadtest",
        "origin": {"kind": "import", "ref": f"loadtest/batch-{index:02d}.csv"},
        "operations": operations,
    }, now=batch_moment(index))
    if plan["errors"]:
        raise SystemExit(json.dumps(plan["errors"][:5], ensure_ascii=False))
    planned = time.time()
    result, code = crm_contract.apply_transaction(target, token, plan)
    if code != 0:
        raise SystemExit(json.dumps(result, ensure_ascii=False)[:2000])
    print(f"batch {index:02d}: {len(operations)} ops, plan {planned - started:.1f}s, apply {time.time() - planned:.1f}s", flush=True)


def ids(target: Path, directory: str) -> list[str]:
    return sorted(path.stem for path in (target / "records" / directory).glob("*.md"))


def main() -> int:
    force = "--force" in sys.argv
    if TARGET.exists():
        if not force and len(list((TARGET / "records" / "task").glob("*.md"))) >= 3000:
            print("perf wiki exists; use --force to rebuild")
            return 0
        shutil.rmtree(TARGET)
    shutil.copytree(BASE, TARGET)
    wiki = Wiki(TARGET, TOKENS)
    if wiki.token_file.exists():
        wiki.token_file.unlink()
    wiki.acquire("perf-data")
    token = wiki.token_file.read_text(encoding="utf-8").strip()
    rng = random.Random(20261006)
    started = time.time()
    try:
        wiki.locked("crm_init.py", "--target", TARGET)
        batch = 0
        members = [{"op": "create", "object": "workspaceMember", "values": {
            "name": {"firstName": FIRST[i], "lastName": LAST[i]}, "userEmail": f"{FIRST[i].lower()}@firma.example"}}
            for i in range(10)]
        run_batch(TARGET, token, members, batch)
        batch += 1
        member_ids = ids(TARGET, "workspace-member")
        for chunk in range(5):
            operations = []
            for n in range(chunk * 1000, chunk * 1000 + 1000):
                currency = "USD" if n % 7 == 0 else "EUR"
                operations.append({"op": "create", "object": "company", "values": {
                    "name": f"Firma {n:05d} {rng.choice(LAST)} GmbH",
                    "domainName": f"https://firma{n:05d}.example",
                    "address": {"addressCity": rng.choice(CITIES), "addressStreet1": f"Hauptstraße {rng.randint(1, 200)}",
                                "addressPostcode": f"{rng.randint(10000, 99999)}", "addressCountry": "Deutschland"},
                    "annualRevenue": {"amount": f"{rng.randint(1, 5000) * 10000}.00", "currencyCode": currency},
                    "accountOwner": rng.choice(member_ids),
                }})
            run_batch(TARGET, token, operations, batch)
            batch += 1
        company_ids = ids(TARGET, "company")
        for chunk in range(5):
            operations = []
            for n in range(chunk * 1000, chunk * 1000 + 1000):
                first, last = rng.choice(FIRST), rng.choice(LAST)
                operations.append({"op": "create", "object": "person", "values": {
                    "name": {"firstName": first, "lastName": last},
                    "emails": f"{first.lower()}.{n:05d}@kunde.example",
                    "jobTitle": rng.choice(["Einkauf", "Geschäftsführung", "Vertrieb", "IT-Leitung", "Marketing"]),
                    "phones": {"primaryPhoneNumber": f"30{rng.randint(1000000, 9999999)}", "primaryPhoneCallingCode": "+49"},
                    "company": rng.choice(company_ids),
                }})
            run_batch(TARGET, token, operations, batch)
            batch += 1
        person_ids = ids(TARGET, "person")
        for chunk in range(2):
            operations = []
            for n in range(chunk * 1000, chunk * 1000 + 1000):
                currency = "USD" if n % 9 == 0 else "EUR"
                month = rng.randint(1, 18)
                year, month = (2026, month) if month <= 12 else (2027, month - 12)
                operations.append({"op": "create", "object": "opportunity", "values": {
                    "name": f"Projekt {n:04d}",
                    "amount": {"amount": f"{rng.randint(5, 900) * 1000}.{rng.randint(0, 99):02d}", "currencyCode": currency},
                    "stage": rng.choice(STAGES),
                    "closeDate": f"{year}-{month:02d}-{rng.randint(1, 28):02d}T{rng.randint(6, 18):02d}:00:00Z",
                    "company": rng.choice(company_ids),
                    "pointOfContact": rng.choice(person_ids),
                    "owner": rng.choice(member_ids),
                }})
            run_batch(TARGET, token, operations, batch)
            batch += 1
        opportunity_ids = ids(TARGET, "opportunity")
        for chunk in range(3):
            operations = []
            for n in range(chunk * 1000, chunk * 1000 + 1000):
                targets = [f"company:{rng.choice(company_ids)}"]
                if n % 2 == 0:
                    targets.append(f"opportunity:{rng.choice(opportunity_ids)}")
                if n % 3 == 0:
                    targets.append(f"person:{rng.choice(person_ids)}")
                operations.append({"op": "create", "object": "task", "values": {
                    "title": f"Aufgabe {n:04d}: {rng.choice(['Angebot nachfassen', 'Rückruf', 'Vertrag prüfen', 'Termin vorbereiten'])}",
                    "status": rng.choice(STATUSES),
                    "dueAt": f"2026-{rng.randint(9, 12):02d}-{rng.randint(1, 28):02d}T{rng.randint(6, 18):02d}:30:00Z",
                    "assignee": rng.choice(member_ids),
                    "targets": targets,
                    "bodyV2": "Bitte **bis Freitag** erledigen. Details im [Angebot](https://intranet.example/angebot).",
                }})
            run_batch(TARGET, token, operations, batch)
            batch += 1
    finally:
        print("release:", wiki.release()[:120])
    print(f"total {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
