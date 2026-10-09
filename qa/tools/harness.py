#!/usr/bin/env python3
"""Small test harness: run skill helpers under the wiki lock and return parsed JSON."""
import json
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
SKILL = WORKSPACE.parent / "ai-first-crm"
SCRIPTS = SKILL / "scripts"


def base_wiki() -> Path:
    """The initialized German test wiki the suites copy from (test-wikis/t1), created on first use."""
    target = WORKSPACE / "test-wikis" / "t1"
    if not (target / "WIKI.md").is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        made = subprocess.run([sys.executable, str(WORKSPACE / "tools" / "make_test_wiki.py"), str(SKILL), str(target)], capture_output=True, text=True)
        if made.returncode != 0:
            raise RuntimeError(f"make_test_wiki failed: {made.stdout} {made.stderr}")
    return target


class Wiki:
    def __init__(self, target: Path, token_dir: Path):
        self.target = Path(target).resolve()
        token_dir.mkdir(parents=True, exist_ok=True)
        self.token_file = token_dir / (self.target.name + ".token")

    def raw(self, script, *args, check=True):
        completed = subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)], capture_output=True, text=True)
        if check and completed.returncode not in (0,):
            raise RuntimeError(f"{script} {args} failed ({completed.returncode}):\n{completed.stdout}\n{completed.stderr}")
        return completed

    def acquire(self, operation="test"):
        out = self.raw("wiki_lock.py", "acquire", "--target", self.target, "--owner", "test-harness",
                       "--operation", operation, "--token-file", self.token_file)
        return json.loads(out.stdout)

    def release(self):
        out = self.raw("wiki_lock.py", "release", "--target", self.target, "--token-file", self.token_file,
                       "--remove-token-file", check=False)
        return out.stdout

    def locked(self, helper, *args, expect=(0,)):
        completed = subprocess.run([sys.executable, str(SCRIPTS / "run_locked.py"), "--token-file", str(self.token_file),
                                    "--helper", helper, *map(str, args)], capture_output=True, text=True)
        if completed.returncode not in expect:
            raise RuntimeError(f"{helper} {args} exit {completed.returncode}:\n{completed.stdout[-4000:]}\n{completed.stderr[-4000:]}")
        try:
            return json.loads(completed.stdout) if completed.stdout.strip() else {}
        except json.JSONDecodeError:
            return {"_raw": completed.stdout, "_stderr": completed.stderr, "_code": completed.returncode}

    def transact(self, request, workdir: Path, confirm=False, expect_plan=(0,), expect_apply=(0,)):
        workdir.mkdir(parents=True, exist_ok=True)
        req = workdir / "request.json"
        req.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        plan_file = workdir / "plan.json"
        report = self.locked("crm_records.py", "plan", "--target", self.target, "--request-file", req, "--output", plan_file, expect=expect_plan)
        if report.get("state") != "planned":
            return report, None
        args = ["apply", "--target", self.target, "--plan-file", plan_file, "--expect-plan-sha256", report["plan_sha256"]]
        if confirm:
            args.append("--confirm-destructive")
        result = self.locked("crm_records.py", *args, expect=expect_apply)
        return report, result
