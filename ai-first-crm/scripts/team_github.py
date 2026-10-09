#!/usr/bin/env python3
"""Authenticated GitHub transport for the Git team skill. No long-running service."""
from __future__ import annotations
import io, json, os, re, subprocess, tarfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote
from team_contract import (
    TeamError,
    CHECK_CONTEXT,
    ACTIONS_APP_ID,
    RUNTIME_REPOSITORY,
    SHA,
    safe_path,
    login_for,
)

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
WORKFLOW_PATH = ".github/workflows/crm-team-integrity.yml"


def extract_archive(data: bytes, target: Path) -> None:
    """Extract only ordinary files/directories; never links, devices or traversal."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise TeamError("workspace_too_large")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            members = archive.getmembers()
            seen = set()
            total = 0
            root = None
            for member in members:
                parts = member.name.split("/")
                if root is None:
                    root = parts[0]
                if not parts[0] or parts[0] != root:
                    raise TeamError("unsafe_archive")
                relative = "/".join(parts[1:]).rstrip("/")
                if not relative:
                    continue
                if not safe_path(relative) or relative.casefold() in seen:
                    raise TeamError("unsafe_archive")
                seen.add(relative.casefold())
                total += member.size
                if total > MAX_ARCHIVE_BYTES or not (member.isdir() or member.isfile()):
                    raise TeamError("unsafe_archive")
                destination = target / relative
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise TeamError("unsafe_archive")
                    destination.write_bytes(source.read())
                    destination.chmod(0o600)
    except (tarfile.TarError, OSError) as exc:
        raise TeamError(
            "unsafe_archive", "The workspace archive could not be extracted safely."
        ) from exc


def git(target: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(target), *map(str, args)],
        capture_output=True,
        env=env,
    )
    if check and result.returncode:
        # Git errors may contain credential URLs. Never echo them into an agent or CI log.
        raise TeamError(
            "git_failed",
            "Git could not complete this operation.",
            command=args[0] if args else "git",
        )
    return result


def git_text(target: Path, *args: str) -> str:
    return git(target, *args).stdout.decode("utf-8").strip()


def repository_from_remote(target: Path) -> str:
    remote = git_text(target, "remote", "get-url", "origin")
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
        remote,
    )
    if not match or ".." in match.group(1):
        raise TeamError(
            "unsupported_remote", "Use a credential-free GitHub origin URL."
        )
    return match.group(1)


def workflow_source(cfg: dict[str, Any]) -> str:
    """The workflow never checks out or runs code from a data pull request."""
    template = (
        Path(__file__).resolve().parent.parent / "assets/team-integrity-workflow.yml"
    )
    return template.read_text(encoding="utf-8").replace(
        "__RUNTIME_REVISION__", cfg["runtime_revision"]
    )


class GitHub:
    def __init__(self, repository: str):
        if (
            not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repository)
            or ".." in repository
        ):
            raise TeamError("invalid_repository")
        self.repository = repository

    def api(
        self,
        path: str,
        method: str = "GET",
        body: Optional[dict[str, Any]] = None,
        binary: bool = False,
    ) -> Any:
        args = ["gh", "api", "--hostname", "github.com", path, "--method", method]
        if body is not None:
            args += ["--input", "-"]
        try:
            result = subprocess.run(
                args,
                input=json.dumps(body).encode() if body is not None else None,
                capture_output=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TeamError(
                "hosting_unavailable",
                "Authenticated GitHub access is unavailable; the operation has not been confirmed published.",
            ) from exc
        if result.returncode:
            raise TeamError(
                "hosting_request_failed",
                "GitHub rejected the request. Check authentication, repository access and the hosting policy.",
                method=method,
            )
        if binary:
            return result.stdout
        try:
            return json.loads(result.stdout) if result.stdout else {}
        except ValueError as exc:
            raise TeamError("hosting_invalid_response") from exc

    def principal(self) -> dict[str, Any]:
        user = self.api("/user")
        return {"login": login_for(user["login"]), "id": int(user["id"])}

    def metadata(self) -> dict[str, Any]:
        return self.api("/repos/" + self.repository)

    def assert_private(self) -> None:
        meta = self.metadata()
        if meta.get("private") is not True:
            raise TeamError(
                "private_repository_required",
                "Team CRM data must use a private repository, separate from the public product.",
            )

    def revision(self, branch: str) -> str:
        sha = self.api(
            "/repos/" + self.repository + "/git/ref/heads/" + quote(branch, safe="")
        )["object"]["sha"]
        if not SHA.fullmatch(sha):
            raise TeamError("invalid_revision")
        return sha

    def archive(self, revision: str, target: Path) -> None:
        if not SHA.fullmatch(revision):
            raise TeamError("invalid_revision")
        target.mkdir(parents=True, exist_ok=True)
        extract_archive(
            self.api("/repos/" + self.repository + "/tarball/" + revision, binary=True),
            target,
        )

    def clone(self, revision: str, target: Path, branch: str) -> None:
        # No checkout: obtain Git objects first, then safely extract the pinned archive.
        target.mkdir(parents=True, exist_ok=True)
        git(target, "init")
        git(
            target,
            "remote",
            "add",
            "origin",
            "https://github.com/" + self.repository + ".git",
        )
        git(
            target,
            "-c",
            "credential.helper=!gh auth git-credential",
            "fetch",
            "--no-tags",
            "origin",
            revision,
        )
        self.archive(revision, target)
        git(target, "read-tree", revision)
        git(target, "update-ref", "refs/heads/" + branch, revision)
        git(target, "symbolic-ref", "HEAD", "refs/heads/" + branch)

    def permission_for(self, login: str) -> str:
        return self.api(
            "/repos/"
            + self.repository
            + "/collaborators/"
            + quote(login_for(login), safe="")
            + "/permission"
        ).get("permission", "none")

    def assert_runtime(self, revision: str) -> None:
        if not SHA.fullmatch(revision):
            raise TeamError("runtime_unavailable")
        trusted = GitHub(RUNTIME_REPOSITORY)
        comparison = trusted.api(
            "/repos/" + RUNTIME_REPOSITORY + "/compare/" + revision + "...main"
        )
        if comparison.get("status") not in {"ahead", "identical"}:
            raise TeamError(
                "runtime_not_released",
                "Pin a runtime revision contained in the trusted product main branch, not an unmerged pull request or fork.",
            )
        content = trusted.api(
            "/repos/"
            + RUNTIME_REPOSITORY
            + "/contents/ai-first-crm/scripts/team_git.py?ref="
            + revision
        )
        if content.get("type") != "file":
            raise TeamError("runtime_unavailable")

    def assert_policy(self, branch: str) -> dict[str, Any]:
        try:
            p = self.api(
                "/repos/"
                + self.repository
                + "/branches/"
                + quote(branch, safe="")
                + "/protection"
            )
        except TeamError as exc:
            raise TeamError(
                "repository_policy_required",
                "The team branch needs enforced PRs and the trusted integrity check. Hosting-plan support and admin access may be required.",
            ) from exc
        required = p.get("required_status_checks") or {}
        checks = required.get("checks") or []
        trusted = any(
            c.get("context") == CHECK_CONTEXT and c.get("app_id") == ACTIONS_APP_ID
            for c in checks
        )
        if not (
            required.get("strict") is True
            and trusted
            and p.get("required_pull_request_reviews") is not None
            and (p.get("enforce_admins") or {}).get("enabled") is True
            and (p.get("allow_force_pushes") or {}).get("enabled") is not True
            and (p.get("allow_deletions") or {}).get("enabled") is not True
        ):
            raise TeamError(
                "repository_policy_required",
                "Require up-to-date PRs, the GitHub Actions integrity check, admin enforcement and no force pushes/deletions.",
            )
        return p

    def protect(self, branch: str) -> None:
        # Existing protections are never weakened by a setup helper.
        if self._has_protection(branch):
            self.assert_policy(branch)
            return
        body = {
            "required_status_checks": {
                "strict": True,
                "checks": [{"context": CHECK_CONTEXT, "app_id": ACTIONS_APP_ID}],
            },
            "enforce_admins": True,
            "required_pull_request_reviews": {"required_approving_review_count": 0},
            "restrictions": None,
            "allow_force_pushes": False,
            "allow_deletions": False,
        }
        self.api(
            "/repos/"
            + self.repository
            + "/branches/"
            + quote(branch, safe="")
            + "/protection",
            "PUT",
            body,
        )
        self.assert_policy(branch)

    def _has_protection(self, branch: str) -> bool:
        return (
            self.api(
                "/repos/" + self.repository + "/branches/" + quote(branch, safe="")
            ).get("protected")
            is True
        )

    def push(self, workspace: Path, branch: str) -> None:
        if repository_from_remote(workspace).lower() != self.repository.lower():
            raise TeamError("repository_mismatch")
        # An explicit destination prevents a changed remote.pushurl from exporting CRM data elsewhere.
        git(
            workspace,
            "-c",
            "credential.helper=!gh auth git-credential",
            "push",
            "https://github.com/" + self.repository + ".git",
            "HEAD:refs/heads/" + branch,
        )

    def fetch(self, workspace: Path, branch: str) -> None:
        if repository_from_remote(workspace).lower() != self.repository.lower():
            raise TeamError("repository_mismatch")
        git(
            workspace,
            "-c",
            "credential.helper=!gh auth git-credential",
            "fetch",
            "--no-tags",
            "https://github.com/" + self.repository + ".git",
            "refs/heads/" + branch,
        )

    def push_bootstrap(self, workspace: Path, branch: str, base: str) -> None:
        if self.revision(branch) != base:
            raise TeamError(
                "stale_base", "The shared branch changed; setup needs a new preview."
            )
        self.push(workspace, branch)

    def find_pr(self, head: str, base: str) -> Optional[dict[str, Any]]:
        owner = self.repository.split("/")[0]
        prs = self.api(
            "/repos/"
            + self.repository
            + "/pulls?state=all&head="
            + quote(owner + ":" + head, safe="")
            + "&base="
            + quote(base, safe="")
        )
        return prs[0] if prs else None

    def create_pr(self, head: str, base: str, op: str) -> dict[str, Any]:
        return self.api(
            "/repos/" + self.repository + "/pulls",
            "POST",
            {
                "head": head,
                "base": base,
                "title": "CRM change " + op,
                "body": "A confirmed skill change. The trusted CRM integrity check validates the complete candidate and its operation receipt.",
            },
        )

    def close_pr(self, number: int) -> None:
        self.api(
            "/repos/" + self.repository + "/pulls/" + str(number),
            "PATCH",
            {"state": "closed"},
        )

    def pull(self, number: int) -> dict[str, Any]:
        return self.api("/repos/" + self.repository + "/pulls/" + str(number))

    def status(self, sha: str, state: str, description: str) -> None:
        self.api(
            "/repos/" + self.repository + "/statuses/" + sha,
            "POST",
            {"state": state, "context": CHECK_CONTEXT, "description": description},
        )

    def merge(self, number: int, head: str) -> dict[str, Any]:
        meta = self.metadata()
        pr = self.pull(number)
        protection = self.assert_policy(pr["base"]["ref"])
        linear = (protection.get("required_linear_history") or {}).get(
            "enabled"
        ) is True
        method = (
            "squash"
            if meta.get("allow_squash_merge") and linear
            else (
                "merge"
                if meta.get("allow_merge_commit") and not linear
                else ("squash" if meta.get("allow_squash_merge") else "rebase")
            )
        )
        if method == "rebase" and not meta.get("allow_rebase_merge"):
            raise TeamError("merge_method_unavailable")
        return self.api(
            "/repos/" + self.repository + "/pulls/" + str(number) + "/merge",
            "PUT",
            {
                "sha": head,
                "merge_method": method,
                "commit_title": "Publish verified CRM operation",
            },
        )

    def commit_tree(self, revision: str) -> str:
        return self.api("/repos/" + self.repository + "/git/commits/" + revision)[
            "tree"
        ]["sha"]

    def trusted_status(self, head: str) -> bool:
        statuses = self.api(
            "/repos/" + self.repository + "/commits/" + head + "/status"
        ).get("statuses", [])
        matching = [s for s in statuses if s.get("context") == CHECK_CONTEXT]
        return (
            bool(matching)
            and matching[0].get("state") == "success"
            and matching[0].get("creator", {}).get("login") == "github-actions[bot]"
        )
