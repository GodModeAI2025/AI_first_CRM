#!/usr/bin/env python3
"""Actual native CRM helpers and Git branches; only GitHub HTTP is simulated."""
from pathlib import Path
import json, os, subprocess, sys, tempfile, unittest, uuid

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "ai-first-crm/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "qa/tools"))
from harness import Wiki
from team_contract import TeamError, CONFIG_PATH, RECEIPTS_DIR, CHECK_CONTEXT, actor_for
from team_github import extract_archive, workflow_source, git, git_text
from team_git import (
    begin,
    run_helper,
    build_preview,
    approve,
    submit,
    publish,
    read_snapshot,
    read_helper,
    check_pr,
    cleanup,
    sync,
    supersede,
    cancel,
    history_audit,
)
from team_integrity import verify_native, validate_candidate


def cfg():
    return {
        "format": "ai-first-crm-team/1",
        "repository": "example/crm-data",
        "branch": "main",
        "runtime_revision": "a" * 40,
        "privacy": {"profile": "git-history", "acknowledged": True},
        "members": {
            "alice": {"role": "owner", "github_id": 1},
            "bob": {"role": "contributor", "github_id": 2},
            "eve": {"role": "reader", "github_id": 3},
        },
    }


class Backend:
    def __init__(self, root, baseline):
        self.root = root
        self.remote = root / "remote.git"
        subprocess.run(
            ["git", "clone", "--bare", str(baseline), str(self.remote)],
            check=True,
            capture_output=True,
        )
        self.prs = {}
        self.statuses = {}
        self.policy = True
        self.private = True
        self.fail_push = False
        self.lose_merge = False
        self.merge_calls = 0

    def command(self, *args, check=True):
        r = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fake GitHub",
                "-c",
                "user.email=server@example.invalid",
                "--git-dir",
                str(self.remote),
                *args,
            ],
            capture_output=True,
        )
        if check and r.returncode:
            raise RuntimeError(r.stderr.decode())
        return r

    def text(self, *args):
        return self.command(*args).stdout.decode().strip()


class FakeGitHub:
    repository = "example/crm-data"

    def __init__(self, backend, login="alice"):
        self.backend = backend
        self.login = login

    def principal(self):
        return {
            "login": self.login,
            "id": {"alice": 1, "bob": 2, "eve": 3}.get(self.login, 4),
        }

    def permission_for(self, login):
        return "admin" if login == "alice" else "write"

    def assert_private(self):
        if not self.backend.private:
            raise TeamError("private_repository_required")

    def revision(self, branch):
        return self.backend.text("rev-parse", "refs/heads/" + branch)

    def archive(self, revision, target):
        target.mkdir(parents=True, exist_ok=True)
        extract_archive(
            self.backend.command(
                "archive", "--format=tar", "--prefix=workspace/", revision
            ).stdout,
            target,
        )

    def clone(self, revision, target, branch):
        subprocess.run(
            ["git", "clone", "--no-checkout", str(self.backend.remote), str(target)],
            check=True,
            capture_output=True,
        )
        self.archive(revision, target)
        git(target, "read-tree", revision)
        git(target, "update-ref", "refs/heads/" + branch, revision)
        git(target, "symbolic-ref", "HEAD", "refs/heads/" + branch)

    def assert_policy(self, branch):
        if not self.backend.policy:
            raise TeamError("repository_policy_required")
        return {}

    def protect(self, branch):
        self.backend.policy = True

    def push(self, workspace, branch):
        if self.backend.fail_push:
            raise TeamError("hosting_unavailable")
        git(workspace, "push", "origin", "HEAD:refs/heads/" + branch)

    def fetch(self, workspace, branch):
        git(workspace, "fetch", "--no-tags", "origin", "refs/heads/" + branch)

    def push_bootstrap(self, workspace, branch, base):
        if self.revision(branch) != base:
            raise TeamError("stale_base")
        self.push(workspace, branch)

    def find_pr(self, head, base):
        return next(
            (
                p
                for p in self.backend.prs.values()
                if p["head"]["ref"] == head and p["base"]["ref"] == base
            ),
            None,
        )

    def create_pr(self, head, base, op):
        p = {
            "number": len(self.backend.prs) + 1,
            "state": "open",
            "user": {"login": self.login, "id": self.principal()["id"]},
            "base": {"ref": base, "sha": self.revision(base)},
            "head": {
                "ref": head,
                "sha": self.revision(head),
                "repo": {"full_name": self.repository},
            },
            "html_url": "https://example.invalid/pr",
        }
        self.backend.prs[p["number"]] = p
        return p

    def close_pr(self, number):
        self.backend.prs[number]["state"] = "closed"

    def pull(self, number):
        p = dict(self.backend.prs[number])
        p["base"] = {**p["base"], "sha": self.revision(p["base"]["ref"])}
        return p

    def status(self, head, state, description):
        self.backend.statuses[head] = state

    def trusted_status(self, head):
        return self.backend.statuses.get(head) == "success"

    def commit_tree(self, revision):
        return self.backend.text("rev-parse", revision + "^{tree}")

    def merge(self, number, head):
        b = self.backend
        p = b.prs[number]
        base = self.revision(p["base"]["ref"])
        if p["head"]["sha"] != head or not self.trusted_status(head) or not b.policy:
            raise TeamError("merge_blocked")
        # Simulate protected up-to-date GitHub merging with an actual expected-old ref update.
        if b.command("merge-base", "--is-ancestor", base, head, check=False).returncode:
            raise TeamError("stale_base")
        tree = self.commit_tree(head)
        r = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fake GitHub",
                "-c",
                "user.email=server@example.invalid",
                "--git-dir",
                str(b.remote),
                "commit-tree",
                tree,
                "-p",
                base,
                "-p",
                head,
            ],
            input=b"Publish verified operation\n",
            capture_output=True,
            check=True,
        )
        sha = r.stdout.decode().strip()
        b.command("update-ref", "refs/heads/" + p["base"]["ref"], sha, base)
        p["state"] = "closed"
        p["merged"] = True
        p["merge_commit_sha"] = sha
        b.merge_calls += 1
        if b.lose_merge:
            b.lose_merge = False
            raise TeamError("hosting_unavailable")
        return {"merged": True, "sha": sha}


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="crm-team-suite-")
        cls.root = Path(cls.tmp.name)
        cls.baseline = cls.root / "baseline"
        run = subprocess.run(
            [
                sys.executable,
                str(ROOT / "qa/tools/make_test_wiki.py"),
                str(ROOT / "ai-first-crm"),
                str(cls.baseline),
            ],
            capture_output=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if run.returncode:
            raise RuntimeError(run.stdout.decode() + run.stderr.decode())
        w = Wiki(cls.baseline, cls.root / "tokens")
        w.acquire("team-fixture")
        w.locked("crm_init.py", "--target", cls.baseline)
        (cls.baseline / CONFIG_PATH).write_text(json.dumps(cfg()))
        workflow = cls.baseline / ".github/workflows/crm-team-integrity.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(workflow_source(cfg()))
        (cls.baseline / ".gitignore").write_text(
            ".llmwiki.lock/\n__pycache__/\n*.pyc\n.DS_Store\n"
        )
        w.locked("crm_build.py", "--target", cls.baseline)
        w.locked("build_graph.py", "--target", cls.baseline)
        w.locked(
            "release_wiki.py",
            "--target",
            cls.baseline,
            "--operation-id",
            "fixture",
            "--expect-current-version",
            "0.1.0",
            "--bump",
            "minor",
            "--summary",
            "Team fixture",
        )
        w.release()
        verify_native(cls.baseline)
        git(cls.baseline, "init", "-b", "main")
        git(cls.baseline, "config", "user.name", "Fixture")
        git(cls.baseline, "config", "user.email", "fixture@example.invalid")
        git(cls.baseline, "add", ".")
        git(cls.baseline, "commit", "-m", "Synthetic fixture")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.case = Path(tempfile.mkdtemp(dir=self.root, prefix="case-"))
        self.backend = Backend(self.case, self.baseline)
        self.provider = FakeGitHub(self.backend)
        self.source = self.case / "source"
        subprocess.run(
            ["git", "clone", str(self.backend.remote), str(self.source)],
            check=True,
            capture_output=True,
        )

    def start(self, actor="alice", op=None):
        provider = FakeGitHub(self.backend, actor)
        root = self.case / ("session-" + uuid.uuid4().hex)
        result = begin(self.source, root, op or str(uuid.uuid4()), provider)
        self.assertEqual(result["state"], "draft")
        return root, provider

    def change(self, root, provider, name="Example", domain="https://example.invalid"):
        request = root / "input.json"
        request.write_text(
            json.dumps(
                {
                    "actor": "human:forged",
                    "operations": [
                        {
                            "op": "create",
                            "object": "company",
                            "values": {"name": name, "domainName": domain},
                        }
                    ],
                }
            )
        )
        plan = run_helper(
            root / "session.json",
            "crm_records.py",
            [
                "plan",
                "--request-file",
                str(request),
                "--output",
                str(root / "record-plan.json"),
            ],
            provider,
        )
        self.assertEqual(plan["state"], "planned", plan)
        applied = run_helper(
            root / "session.json",
            "crm_records.py",
            [
                "apply",
                "--plan-file",
                str(root / "record-plan.json"),
                "--expect-plan-sha256",
                plan["plan_sha256"],
            ],
            provider,
        )
        self.assertEqual(applied["state"], "applied", applied)

    def candidate(self, actor="alice", domain="https://example.invalid"):
        root, provider = self.start(actor)
        self.change(root, provider, actor + " Company", domain)
        preview = build_preview(root / "session.json", provider)
        approve(
            root / "session.json", preview["preview_sha256"], False, False, provider
        )
        pr = submit(root / "session.json", provider)
        check_pr(provider.repository, pr["pull_request"], provider, True)
        return root, provider, pr

    def test_fr03_preparation_does_not_change_shared_clone_or_remote(self):
        old = self.provider.revision("main")
        root, p = self.start()
        self.change(root, p)
        preview = build_preview(root / "session.json", p)
        self.assertTrue(preview["changes"])
        self.assertEqual(p.revision("main"), old)
        self.assertEqual(git_text(self.source, "status", "--porcelain"), "")
        self.assertEqual(
            json.loads((root / "record-plan.json").read_text())["actor"],
            actor_for("alice", 1),
        )

    def test_fr04_edited_preview_and_candidate_are_refused(self):
        root, p = self.start()
        self.change(root, p)
        preview = build_preview(root / "session.json", p)
        file = next((root / "candidate/records/company").glob("*.md"))
        file.write_text(file.read_text().replace("Example", "Other"))
        with self.assertRaises(TeamError):
            approve(root / "session.json", preview["preview_sha256"], False, False, p)
        self.assertFalse((root / "candidate" / RECEIPTS_DIR).exists())

    def test_fr06_parallel_duplicate_creation_is_not_published(self):
        a, pa, pra = self.candidate("alice")
        b, pb, prb = self.candidate("bob")
        published = publish(a / "session.json", pa)
        self.assertEqual(published["state"], "published")
        with self.assertRaises(TeamError) as rejected:
            publish(b / "session.json", pb)
        self.assertEqual(rejected.exception.state, "stale_base")
        with self.assertRaises(TeamError):
            check_pr(pb.repository, prb["pull_request"], pb, True)
        self.assertEqual(self.backend.merge_calls, 1)

    def test_fr07_failed_push_is_not_acknowledged(self):
        root, p = self.start()
        self.change(root, p)
        preview = build_preview(root / "session.json", p)
        approve(root / "session.json", preview["preview_sha256"], False, False, p)
        old = p.revision("main")
        self.backend.fail_push = True
        with self.assertRaises(TeamError):
            submit(root / "session.json", p)
        self.assertEqual(old, p.revision("main"))
        self.assertEqual(
            json.loads((root / "session.json").read_text())["phase"], "approved"
        )

    def test_fr08_lost_merge_response_and_retry_create_one_operation(self):
        root, p, pr = self.candidate()
        self.backend.lose_merge = True
        result = publish(root / "session.json", p)
        self.assertEqual(result["state"], "published")
        again = publish(root / "session.json", p)
        self.assertTrue(again["idempotent"])
        self.assertEqual(self.backend.merge_calls, 1)

    def test_fr09_reader_uses_published_state_while_writer_has_lock(self):
        root, p = self.start()
        self.change(root, p)
        reader = self.case / "reader"
        rp = FakeGitHub(self.backend, "eve")
        read_snapshot(self.source, reader, rp)
        result = read_helper(
            reader / "session.json", "crm_query.py", ["find", "--object", "company"], rp
        )
        self.assertTrue(result["published_snapshot"])
        self.assertEqual(result.get("total", result.get("records", [])), 0)
        with self.assertRaises(TeamError):
            read_helper(
                reader / "session.json",
                "crm_export.py",
                [
                    "json",
                    "--object",
                    "company",
                    "--output",
                    str(reader / "contacts.json"),
                ],
                rp,
            )
        self.assertFalse((reader / "contacts.json").exists())

    def test_fr02_private_repository_policy_and_unknown_users_are_required(self):
        self.backend.private = False
        with self.assertRaises(TeamError):
            self.start()
        self.backend.private = True
        self.backend.policy = False
        with self.assertRaises(TeamError):
            self.start()
        self.backend.policy = True
        with self.assertRaises(TeamError):
            self.start("nobody")

    def test_fr10_forged_pull_request_actor_is_refused(self):
        root, p, pr = self.candidate()
        self.backend.prs[pr["pull_request"]]["user"] = {"login": "bob", "id": 2}
        with self.assertRaises(TeamError) as refused:
            check_pr(p.repository, pr["pull_request"], p, True)
        self.assertEqual(refused.exception.state, "actor_mismatch")

    def test_fr02_contributor_cannot_change_team_membership(self):
        root, p = self.start("bob")
        self.change(root, p)
        c = json.loads((root / "candidate" / CONFIG_PATH).read_text())
        c["members"]["bob"]["role"] = "owner"
        (root / "candidate" / CONFIG_PATH).write_text(json.dumps(c))
        with self.assertRaises(TeamError) as refused:
            build_preview(root / "session.json", p)
        self.assertEqual(refused.exception.state, "permission_denied")

    def test_fr02_navigation_marker_does_not_grant_knowledge_permission(self):
        root, provider = self.start("bob")
        self.change(root, provider)
        page = root / "candidate/wiki/index.md"
        page.write_text(
            page.read_text()
            + '\n<!-- generated_by: "process:skillsafewerkstatt-navigation" -->\nUnauthorized knowledge.\n'
        )
        with self.assertRaises(TeamError) as denied:
            build_preview(root / "session.json", provider)
        self.assertEqual(denied.exception.state, "permission_denied")

    def test_fr02_username_reuse_does_not_inherit_membership(self):
        provider = FakeGitHub(self.backend, "alice")
        provider.principal = lambda: {"login": "alice", "id": 99}
        with self.assertRaises(TeamError) as denied:
            begin(self.source, self.case / "wrong-account", str(uuid.uuid4()), provider)
        self.assertEqual(denied.exception.state, "permission_denied")
        with self.assertRaises(TeamError) as denied_history:
            history_audit(self.source, provider)
        self.assertEqual(denied_history.exception.state, "permission_denied")

    def test_fr05_company_rename_updates_derived_contact_labels(self):
        root, p = self.start()
        self.change(root, p)
        company = next((root / "candidate/records/company").glob("*.md")).stem
        request = root / "person.json"
        request.write_text(
            json.dumps(
                {
                    "operations": [
                        {
                            "op": "create",
                            "object": "person",
                            "values": {
                                "name": {"firstName": "Ada", "lastName": "Example"},
                                "company": {"id": company},
                            },
                        }
                    ]
                }
            )
        )
        plan = run_helper(
            root / "session.json",
            "crm_records.py",
            [
                "plan",
                "--request-file",
                str(request),
                "--output",
                str(root / "person-plan.json"),
            ],
            p,
        )
        self.assertEqual(plan["state"], "planned", plan)
        run_helper(
            root / "session.json",
            "crm_records.py",
            [
                "apply",
                "--plan-file",
                str(root / "person-plan.json"),
                "--expect-plan-sha256",
                plan["plan_sha256"],
            ],
            p,
        )
        preview = build_preview(root / "session.json", p)
        approve(root / "session.json", preview["preview_sha256"], False, False, p)
        pr = submit(root / "session.json", p)
        check_pr(p.repository, pr["pull_request"], p, True)
        publish(root / "session.json", p)
        second, bob = self.start("bob")
        change = second / "rename.json"
        change.write_text(
            json.dumps(
                {
                    "operations": [
                        {
                            "op": "update",
                            "object": "company",
                            "record": company,
                            "values": {"name": "Renamed Company"},
                        }
                    ]
                }
            )
        )
        plan = run_helper(
            second / "session.json",
            "crm_records.py",
            [
                "plan",
                "--request-file",
                str(change),
                "--output",
                str(second / "rename-plan.json"),
            ],
            bob,
        )
        self.assertEqual(plan["state"], "planned", plan)
        run_helper(
            second / "session.json",
            "crm_records.py",
            [
                "apply",
                "--plan-file",
                str(second / "rename-plan.json"),
                "--expect-plan-sha256",
                plan["plan_sha256"],
            ],
            bob,
        )
        preview = build_preview(second / "session.json", bob)
        approve(second / "session.json", preview["preview_sha256"], False, False, bob)
        pr = submit(second / "session.json", bob)
        self.assertEqual(
            check_pr(bob.repository, pr["pull_request"], bob, True)["state"], "verified"
        )

    def test_fr10_changed_browser_code_is_not_trusted_as_derived_data(self):
        root, p, pr = self.candidate()
        candidate = root / "candidate"
        html = candidate / "graph/crm/index.html"
        html.write_text(
            html.read_text().replace(
                "</body>", '<script>fetch("https://example.invalid")</script></body>'
            )
        )
        import hashlib

        build = candidate / "graph/crm/manifest.json"
        b = json.loads(build.read_text())
        b["files"]["graph/crm/index.html"] = hashlib.sha256(
            html.read_bytes()
        ).hexdigest()
        build.write_text(json.dumps(b))
        manifest = candidate / "meta/manifest.json"
        m = json.loads(manifest.read_text())
        for entry in m["files"]:
            if entry["path"] in {"graph/crm/index.html", "graph/crm/manifest.json"}:
                content = (candidate / entry["path"]).read_bytes()
                entry["sha256"] = hashlib.sha256(content).hexdigest()
                entry["bytes"] = len(content)
        manifest.write_text(json.dumps(m))
        with self.assertRaises(TeamError) as rejected:
            validate_candidate(
                root / "base",
                candidate,
                json.loads((root / "session.json").read_text())["base_revision"],
                "alice",
                p.repository,
                actor_id=1,
            )
        self.assertEqual(rejected.exception.state, "untrusted_rendered_view")

    def test_fr14_cleanup_preserves_original_inputs_and_user_exports(self):
        root, p, pr = self.candidate()
        publish(root / "session.json", p)
        original = root / "request-original.json"
        original.write_text("original user input")
        export = root / "contacts.csv"
        export.write_text("user export")
        result = cleanup(root / "session.json", p)
        self.assertEqual(result["state"], "cleaned")
        self.assertTrue(original.exists())
        self.assertTrue(export.exists())
        self.assertFalse((root / "candidate").exists())
        self.assertFalse((root / "preview.json").exists())

    def test_fr01_existing_workspace_setup_publishes_without_an_application(self):
        import shutil

        legacy = self.case / "legacy"
        shutil.copytree(self.baseline, legacy, ignore=shutil.ignore_patterns(".git"))
        (legacy / CONFIG_PATH).unlink()
        (legacy / ".github/workflows/crm-team-integrity.yml").unlink()
        w = Wiki(legacy, self.case / "legacy-tokens")
        w.acquire("legacy")
        w.locked("crm_build.py", "--target", legacy)
        w.locked("build_graph.py", "--target", legacy)
        w.locked(
            "release_wiki.py",
            "--target",
            legacy,
            "--operation-id",
            "legacy",
            "--expect-current-version",
            "0.2.0",
            "--bump",
            "minor",
            "--summary",
            "Synthetic pre-team data",
        )
        w.release()
        git(legacy, "init", "-b", "main")
        git(legacy, "config", "user.name", "Fixture")
        git(legacy, "config", "user.email", "fixture@example.invalid")
        git(legacy, "add", ".")
        git(legacy, "commit", "-m", "Pre-team fixture")
        container = self.case / "bootstrap"
        container.mkdir()
        backend = Backend(container, legacy)
        provider = FakeGitHub(backend)
        backend.policy = False
        source = container / "source"
        subprocess.run(
            ["git", "clone", str(backend.remote), str(source)],
            capture_output=True,
            check=True,
        )
        root = container / "setup"
        started = begin(source, root, str(uuid.uuid4()), provider, setup=cfg())
        self.assertEqual(started["state"], "draft")
        preview = build_preview(root / "session.json", provider)
        approve(
            root / "session.json", preview["preview_sha256"], False, False, provider
        )
        result = publish(root / "session.json", provider)
        self.assertEqual(result["state"], "published")
        self.assertTrue(backend.policy)
        self.assertFalse((source / CONFIG_PATH).exists())
        sync(source, provider)
        self.assertTrue((source / CONFIG_PATH).exists())

    def test_fr07_sync_preserves_local_modifications(self):
        root, p, pr = self.candidate()
        publish(root / "session.json", p)
        old = git_text(self.source, "rev-parse", "HEAD")
        (self.source / "my-unsaved-notes.txt").write_text("retain this")
        with self.assertRaises(TeamError) as denied:
            sync(self.source, p)
        self.assertEqual(denied.exception.state, "local_changes_preserved")
        self.assertEqual(git_text(self.source, "rev-parse", "HEAD"), old)
        self.assertEqual(
            (self.source / "my-unsaved-notes.txt").read_text(), "retain this"
        )

    def test_fr04_replan_preserves_draft_and_closes_stale_proposal(self):
        first, alice, pra = self.candidate("alice", "https://first.invalid")
        second, bob, prb = self.candidate("bob", "https://second.invalid")
        publish(first / "session.json", alice)
        new = self.case / "replanned"
        result = supersede(second / "session.json", new, bob)
        self.assertEqual(result["state"], "draft")
        self.assertTrue((second / "input.json").exists())
        self.assertEqual(self.backend.prs[prb["pull_request"]]["state"], "closed")
        self.assertEqual(result["base_revision"], bob.revision("main"))

    def test_fr11_erased_record_identity_cannot_be_restored(self):
        root, p, pr = self.candidate()
        identifier = next((root / "candidate/records/company").glob("*.md")).stem
        publish(root / "session.json", p)
        erased, p = self.start()
        req = erased / "erase.json"
        req.write_text(
            json.dumps(
                {
                    "operations": [
                        {"op": "erase", "object": "company", "record": identifier}
                    ]
                }
            )
        )
        plan = run_helper(
            erased / "session.json",
            "crm_records.py",
            [
                "plan",
                "--request-file",
                str(req),
                "--output",
                str(erased / "erase-plan.json"),
            ],
            p,
        )
        self.assertEqual(plan["state"], "planned", plan)
        run_helper(
            erased / "session.json",
            "crm_records.py",
            [
                "apply",
                "--plan-file",
                str(erased / "erase-plan.json"),
                "--expect-plan-sha256",
                plan["plan_sha256"],
                "--confirm-destructive",
            ],
            p,
        )
        preview = build_preview(erased / "session.json", p)
        with self.assertRaises(TeamError):
            approve(erased / "session.json", preview["preview_sha256"], True, False, p)
        approve(erased / "session.json", preview["preview_sha256"], True, True, p)
        pr = submit(erased / "session.json", p)
        check_pr(p.repository, pr["pull_request"], p, True)
        publish(erased / "session.json", p)
        restored, p = self.start()
        req = restored / "restore.json"
        req.write_text(
            json.dumps(
                {
                    "operations": [
                        {
                            "op": "create",
                            "object": "company",
                            "id": identifier,
                            "values": {
                                "name": "Restored old copy",
                                "domainName": "https://example.invalid",
                            },
                        }
                    ]
                }
            )
        )
        plan = run_helper(
            restored / "session.json",
            "crm_records.py",
            [
                "plan",
                "--request-file",
                str(req),
                "--output",
                str(restored / "restore-plan.json"),
            ],
            p,
        )
        self.assertEqual(plan["state"], "planned", plan)
        run_helper(
            restored / "session.json",
            "crm_records.py",
            [
                "apply",
                "--plan-file",
                str(restored / "restore-plan.json"),
                "--expect-plan-sha256",
                plan["plan_sha256"],
            ],
            p,
        )
        with self.assertRaises(TeamError) as blocked:
            build_preview(restored / "session.json", p)
        self.assertEqual(blocked.exception.state, "erased_record_restore")

    def test_fr14_frozen_export_without_crm_omits_membership_and_receipts(self):
        reader = self.case / "export-reader"
        read_snapshot(self.source, reader, self.provider)
        result = read_helper(
            reader / "session.json",
            "export_wiki_skill.py",
            [
                "--skill-name",
                "team-knowledge",
                "--output-dir",
                str(reader / "export"),
                "--exclude-crm",
            ],
            self.provider,
        )
        self.assertEqual(result["state"], "exported", result)
        files = list((reader / "export").rglob("team.json"))
        self.assertEqual(files, [])
        self.assertEqual(list((reader / "export").rglob("team-operations")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
