#!/usr/bin/env python3
"""Run crm_build.py on a test wiki under its own lock: run_build.py <wiki> [--now ISO] [--check]."""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent.parent
sys.path.insert(0, str(WORKSPACE / "tools"))
from harness import SCRIPTS, Wiki  # noqa: E402

target = Path(sys.argv[1]).resolve()
extra = sys.argv[2:]
wiki = Wiki(target, WORKSPACE / "test-wikis" / "views" / ".tokens")
if wiki.token_file.exists():
    wiki.token_file.unlink()
wiki.acquire("crm-build")
token = wiki.token_file.read_text(encoding="utf-8").strip()
try:
    started = time.time()
    completed = subprocess.run([sys.executable, str(SCRIPTS / "crm_build.py"), "--target", str(target), "--lock-token", token, *extra],
                               capture_output=True, text=True)
    print(completed.stdout[-6000:])
    print(completed.stderr[-6000:])
    print("exit", completed.returncode, "wall", round(time.time() - started, 2))
finally:
    wiki.release()
