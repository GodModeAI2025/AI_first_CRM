#!/usr/bin/env python3
"""Behavioral contracts for skill-only Git team collaboration."""
from pathlib import Path
import sys, unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ai-first-crm/scripts"))
from team_contract import (
    TeamError,
    validate_config,
    permission,
    actor_for,
    make_preview,
    approval_for,
    validate_approval,
)


def config():
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
            "limited": {
                "role": "contributor",
                "github_id": 4,
                "objects": ["task"],
                "denied_fields": {"task": ["assignee"]},
            },
        },
    }


class ContractTests(unittest.TestCase):
    def test_fr02_unknown_identity_is_not_authorized(self):
        c = validate_config(config())
        self.assertFalse(permission(c, "nobody", "records", "company", "update"))
        self.assertFalse(permission(c, "eve", "records", "company", "update"))
        self.assertTrue(permission(c, "bob", "records", "company", "create"))
        self.assertEqual(actor_for("Alice"), "human:github:alice")

    def test_fr02_member_cannot_elevate_to_configuration_owner(self):
        c = validate_config(config())
        self.assertFalse(permission(c, "bob", "team-config"))
        self.assertTrue(permission(c, "alice", "team-config"))
        self.assertFalse(permission(c, "limited", "records", "company", "update"))
        self.assertFalse(
            permission(c, "limited", "records", "task", "update", "assignee")
        )

    def test_fr04_confirmation_binds_actor_base_and_material_preview(self):
        p = make_preview(
            "12345678-1234-4234-9234-123456789abc",
            "a" * 40,
            "alice",
            [
                {
                    "path": "records/company/abc.md",
                    "before_sha256": "a" * 64,
                    "after_sha256": "b" * 64,
                    "action": "update",
                }
            ],
            [],
            False,
        )
        approval = approval_for(p, "alice", p["preview_sha256"], False, False)
        validate_approval(p, approval, "alice")
        changed = dict(p)
        changed["base_revision"] = "b" * 40
        with self.assertRaises(TeamError):
            validate_approval(changed, approval, "alice")
        with self.assertRaises(TeamError):
            validate_approval(p, approval, "bob")
        with self.assertRaises(TeamError):
            approval_for(p, "alice", "c" * 64, False, False)

    def test_fr04_destructive_and_history_scopes_need_explicit_approval(self):
        p = make_preview(
            "12345678-1234-4234-9234-123456789abc", "a" * 40, "alice", [], [], True
        )
        with self.assertRaises(TeamError):
            approval_for(p, "alice", p["preview_sha256"], False, False)
        p["retains_git_history"] = True
        with self.assertRaises(TeamError):
            approval_for(p, "alice", p["preview_sha256"], True, False)

    def test_fr11_history_policy_is_explicit_and_no_complete_erasure_claim(self):
        c = config()
        c["privacy"]["acknowledged"] = False
        with self.assertRaises(TeamError):
            validate_config(c)
        c = config()
        c["privacy"]["profile"] = "fully-erased-by-git"
        with self.assertRaises(TeamError):
            validate_config(c)

    def test_fr10_configuration_rejects_credential_urls_and_unsafe_refs(self):
        for value in [
            "https://token@github.com/example/data",
            "../other",
            "owner/data.git;echo-secret",
        ]:
            c = config()
            c["repository"] = value
            with self.assertRaises(TeamError):
                validate_config(c)
        for value in ["../main", "main:other", "-main", "main.lock", "refs/heads/main"]:
            c = config()
            c["branch"] = value
            with self.assertRaises(TeamError):
                validate_config(c)


if __name__ == "__main__":
    unittest.main(verbosity=2)
