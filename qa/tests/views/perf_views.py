#!/usr/bin/env python3
"""Load test for crm_build.py: 5,000 companies, 5,000 people, 2,000 opportunities, 3,000 tasks.

Usage: python3 tests/views/perf_views.py
Creates test-wikis/views/perf-de once (make_perf_wiki.py), then measures a cold build (graph/crm removed),
a warm build (nothing changed), check_fresh, and the size of graph/crm in total and per page.
Prints a JSON report; exit 1 when the targets (under 60 s, under 30 MB) are missed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent.parent
sys.path.insert(0, str(WORKSPACE / "tools"))
from harness import SCRIPTS, Wiki  # noqa: E402

sys.path.insert(0, str(SCRIPTS))
import crm_build  # noqa: E402

TARGET = WORKSPACE / "test-wikis" / "views" / "perf-de"
NOW = "2026-10-06T15:00:00Z"


def main() -> int:
    if not (WORKSPACE / "test-wikis" / "views" / "base-de" / "WIKI.md").is_file():
        subprocess.run([sys.executable, str(WORKSPACE / "tools" / "make_test_wiki.py"), str(SCRIPTS.parent),
                        str(WORKSPACE / "test-wikis" / "views" / "base-de"), "--language", "de"], check=True, capture_output=True)
    subprocess.run([sys.executable, str(HERE / "make_perf_wiki.py")], check=True)
    if (TARGET / "graph" / "crm").exists():
        shutil.rmtree(TARGET / "graph" / "crm")
    wiki = Wiki(TARGET, WORKSPACE / "test-wikis" / "views" / ".tokens")
    if wiki.token_file.exists():
        wiki.token_file.unlink()
    wiki.acquire("perf-views")
    token = wiki.token_file.read_text(encoding="utf-8").strip()
    command = [sys.executable, str(SCRIPTS / "crm_build.py"), "--target", str(TARGET), "--lock-token", token, "--now", NOW]
    try:
        started = time.time()
        cold = subprocess.run(command, capture_output=True, text=True)
        cold_seconds = time.time() - started
        started = time.time()
        warm = subprocess.run(command, capture_output=True, text=True)
        warm_seconds = time.time() - started
    finally:
        wiki.release()
    if cold.returncode != 0:
        print(cold.stdout, cold.stderr)
        return 1
    started = time.time()
    problems = crm_build.check_fresh(TARGET)
    fresh_seconds = time.time() - started
    root = TARGET / "graph" / "crm"
    sizes = {path.relative_to(root).as_posix(): path.stat().st_size for path in sorted(root.rglob("*")) if path.is_file()}
    total = sum(sizes.values())
    records = {directory.name: len(list(directory.glob("*.md"))) for directory in sorted((TARGET / "records").iterdir()) if directory.is_dir()}
    report = {
        "records": records,
        "events": sum(1 for shard in (TARGET / "meta/crm-events").glob("*.jsonl") for _line in shard.open(encoding="utf-8")),
        "cold_build_seconds": round(cold_seconds, 2),
        "cold_build_reported_seconds": json.loads(cold.stdout).get("seconds"),
        "warm_build_seconds": round(warm_seconds, 2),
        "warm_build_written": json.loads(warm.stdout).get("written"),
        "check_fresh_seconds": round(fresh_seconds, 2),
        "check_fresh_errors": problems,
        "total_bytes": total,
        "total_mb": round(total / 1_000_000, 2),
        "pages": dict(sorted(sizes.items(), key=lambda item: -item[1])),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    ok = cold_seconds < 60 and total < 30_000_000 and not problems and report["warm_build_written"] == 0
    print("TARGETS MET" if ok else "TARGETS MISSED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
