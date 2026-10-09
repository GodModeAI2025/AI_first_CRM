# Git team mode

Use this reference when a user wants a shared Git workspace, several agents/users working together, or when `schema/team.json` exists in the selected wiki. The interface is still the user's AI system and this installed skill. There is no separate CRM application, server process, or coordinator to operate.

The first supported hosting provider is **GitHub.com**. GitHub provides authenticated repositories, durable branches/pull requests and integrity checks. Other Git hosts are not advertised as supported by this implementation.

## Boundaries that change your decisions

- Keep the public skill/code/landing repository separate from the **private team data repository**. `team_git.py` refuses a public data repository, credential-bearing remote URLs and unsupported hosts.
- Repository writers and administrators are trusted to use this skill and preserve its checks. Skill roles are not independent GitHub access roles. A writer who can install another GitHub Actions workflow can manufacture an Actions status with the same context; the expected app alone does not identify one workflow. This beta therefore does not claim hostile-writer isolation or platform-wide role enforcement. For an untrusted-writer deployment, require a separately governed mandatory workflow or reviewer policy before enabling it. See [GitHub status-check rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets) and [workflow token permissions](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token).
- All people with repository read access can copy its data and reachable history. This mode supports a team with the same data visibility and different mutation permissions. Object/field restrictions govern this skill's writes, not the confidentiality of files already cloned. The skill's export policy cannot revoke raw Git copying.
- Git retains earlier contact values, messages, event logs and attachments. Setup needs an explicit `git-history` acknowledgement. Erasure removes current released data and the native wiki copies it covers; it does not purge old Git objects, other clones, hosting caches, exports or backups. Destructive publication additionally requires acknowledgement of this limitation. Never call it complete GDPR erasure.
- The local standalone mode still uses local files without networking. The optional team helper uses authenticated Git/GitHub access for sharing and verification. CRM workflows still run on invocation and still write drafts instead of sending messages.
- GitHub must support enforced protected-branch rules for this private repository. The helper checks the real policy and refuses ordinary publication if it is missing. This can depend on the account's hosting plan and permissions. No Enterprise merge queue or separate service is required.
- A skill host needs Python 3.9+, Git, authenticated `gh` and local file execution. A cloud host that cannot run these tools cannot maintain the shared workspace. Any compatible AI host can run the same installable skill.

## What a user experiences

The user describes the task in conversation. The agent reads a verified published snapshot, prepares the work in a private isolated copy, shows its concrete preview and follows the ordinary confirmation rules. After confirmation it submits a protected Git request and waits for the trusted integrity check. Only a verified remote operation receipt is success.

Multiple agents can prepare independently. An updated shared base blocks an older proposal; explain the affected change and replan on the new published state. Never resolve this with `--force`, a line-union merge, or by silently applying a different plan.

Use plain messages such as:

- "The task and its linked contact are prepared. These are the values that will change."
- "The shared state changed while you reviewed this. I will show an updated preview."
- "The request is submitted and its checks are still running. It has not been published."
- "The operation is published in this verified version."

The user must not manage Python commands, Git branches, private tokens or CI identifiers. Commands below are implementation details for the agent.

## Setup on an existing released workspace

1. Initialize the wiki using the existing identity interview and confirmed initialization. Optionally initialize its CRM layer using the existing workflow. Publish and verify it. Put these resulting files in a **private** GitHub repository and obtain its initial branch commit. The data repository is a user's workspace, never this public product repository.
2. Use the user's existing GitHub authentication. Do not ask them to reveal a token. `gh` authentication and GitHub user IDs determine the identity; an arbitrary actor string does not. Contributors need actual GitHub write access; readers need repository read access. Setup does not invite people or grant GitHub repository access.
3. Obtain the member roles and an explicit decision accepting historical Git storage. Use a private runtime file containing a mapping such as `{"alice": {"role": "owner"}, "bob": {"role": "contributor"}, "charlie": {"role": "reader"}}`. The setup command resolves immutable numeric GitHub user IDs and stores them with the roles, so a reused username cannot inherit membership.
4. Generate a new UUIDv4 for this setup intent, then invoke:

   ```text
   python <skill-root>/scripts/team_git.py setup --target <git-wiki> --work-dir <new-private-runtime> --operation-id <uuid> --members-file <private-members.json> --acknowledge-git-history
   ```

   Do not pass the history acknowledgement until the user accepted it. Setup selects an exact released commit of the trusted product's `main` branch; `--runtime-revision <40-character-commit>` can choose an older supported commit in that history. Unmerged forks/PR code is refused.
5. `preview --session <runtime>/session.json` builds and validates the staged views and returns the full material diff plus `preview_sha256`. Show the actual changed settings and membership. The original shared clone is unchanged.
6. After the user authorizes this exact setup preview, use `approve --session ... --expect-preview-sha256 <hash>`, then `publish --session ...`. Initial setup requires GitHub repository administration rights. It publishes the reviewed bootstrap with a normal, expected-base Git update, installs/verifies protected-branch requirements and returns success only when both are confirmed.
7. The required rule is an up-to-date pull request with `crm/team-integrity` from the **GitHub Actions app**, enforced for admins, with force pushes and deletions disabled. Existing protections are never weakened. If an already-protected repository does not satisfy the policy, preserve the work and have its owner add the required policy without removing existing checks; report `repository_policy_required`, never "ready".

`schema/team.json` contains the repository identity, branch, pinned runtime revision, acknowledged storage profile and numeric member IDs. Owners can use the setup flow again to prepare a membership/configuration update. Once configured, this is an ordinary protected proposal rather than an unprotected bootstrap.

## Roles and scope

| Role | Skill operations |
|---|---|
| `reader` | Read verified released files and queries; exports are off by default |
| `contributor` | Record creation/update/trash/restore; imports, communication and drafts within object/field scope; execute own approved workflow runs |
| `maintainer` | Contributor operations plus model/configuration/knowledge/workflow maintenance, merge/destroy/erase and history-scope review |
| `owner` | Maintainer operations plus team membership and trusted check configuration |

An optional `objects` array limits mutation to named object API names. `denied_fields` maps object names to fields that cannot be changed. `exports: true|false` controls exports through this helper, with the raw repository-read limitation above. Unknown users are denied. Routine record actors are bound to `human:github:<numeric-user-id>` automatically.

GitHub membership is independent of CRM `workspaceMember` records. Assignees remain ordinary CRM relations. For `@me` queries, identify the actual CRM member and pass its UUID; do not invent a mapping from a GitHub login to a business contact.

## Prepare and submit a change

Choose a new private runtime directory outside the shared clone and outside synchronized storage. It contains plans, tokens and personal data; never put it in Git or paste its contents into a public issue. Keep the original uploaded files unchanged.

```text
python <skill-root>/scripts/team_git.py start --target <git-wiki> --work-dir <new-private-runtime> --operation-id <uuid>
```

This reads the actual remote base and creates a separate `candidate/`, `base/` and private `session.json`. The helper owns the candidate's local wiki claim and hides its token. Never invoke an ordinary mutating helper against the shared clone while in team mode.

Use the existing task-specific references. Invoke native maintenance helpers through the session wrapper:

```text
python <skill-root>/scripts/team_git.py run --session <runtime>/session.json --helper crm_records.py -- plan --request-file <selected-request.json> --output <runtime>/record-plan.json
python <skill-root>/scripts/team_git.py run --session <runtime>/session.json --helper crm_records.py -- apply --plan-file <runtime>/record-plan.json --expect-plan-sha256 <confirmed-native-plan-hash>
```

The wrapper supplies the target and claim, binds request/direct actors to the authenticated identity, and restricts outputs to the private runtime. It copies requests before binding actors; selected originals are not modified. Existing import/schema/config/ingest/campaign/workflow/page/source helpers use their usual arguments. Do not pass a target or token yourself. Schema/configuration and knowledge operations need the corresponding role. Workflow definition changes need a maintainer; execution of an existing approved workflow uses the caller's record permissions.

All ordinary native confirmation rules still apply. Applying a native plan here changes only the private candidate, not the shared data. Invalid plans and helper failures are not successful changes.

```text
python <skill-root>/scripts/team_git.py preview --session <runtime>/session.json
python <skill-root>/scripts/team_git.py approve --session <runtime>/session.json --expect-preview-sha256 <shown-hash>
```

Show the complete material preview before approving. If the user's original request already authorized the exact non-destructive shared effect, that authorization can cover it after showing the preview. Imports, bulk/model changes and destructive operations retain their explicit confirmation requirements. `approve` needs `--confirm-destructive --acknowledge-git-history` for destructive changes; these flags are never inferred from a stale confirmation.

Approval finalizes a separate copy, preserving the editable draft if final validation fails. It publishes one native release, includes an operation receipt, releases its private claim and freezes a commit. Any later candidate modification invalidates approval.

```text
python <skill-root>/scripts/team_git.py submit --session <runtime>/session.json
python <skill-root>/scripts/team_git.py publish --session <runtime>/session.json
```

Submission creates a branch and PR with an opaque operation identifier. It stores no private technical preview, token or machine path in Git. The branches/PRs are the durable pending requests; newer submissions do not replace unrelated pending requests.

The trusted GitHub workflow uses `pull_request_target` only to obtain authenticated PR metadata and data archives. It checks out the pinned public product runtime, never a PR's program or workflow. It validates the complete candidate, numeric author identity, role/field scope, native events, approval, immutable receipts, current base and release. It regenerates browser views with trusted templates and pinned times instead of trusting a submitted HTML file's own checksum. It posts the required GitHub Actions status.

`checks_pending` or `merge_pending` is **not publication**. Preserve the session and retry with bounded waits while giving the user relevant progress. Do not repeatedly create new branches or operations. A normal protected merge is followed by remote receipt and reviewed-tree verification. No force push is used.

## Conflicts, retries and private runtime recovery

- `stale_base`: the canonical Git base advanced. Use `supersede --session <old-session> --work-dir <new-private-runtime>` to preserve a fresh draft from the current revision and close the older pending PR. Old private inputs and drafts remain available. Replan the original request; never replay a stale technical plan or overwrite a newer value automatically. Show the new effect and obtain the required fresh approval.
- `stale_preview`: the draft or approval changed. Rebuild and review the actual preview. Do not edit its hash or receipt.
- `checks_pending`: wait for this PR's check; do not report it merged. The user's host may retry `publish` when the check changes.
- Lost response after a merge: retry `publish` using the same operation ID. The verified remote receipt returns the original operation without duplicate records, tasks or workflow runs.
- `session_busy`: another process is operating on this private session. If it actually ended, `recover --session ...` clears only a guard from a dead process on the same machine/OS user and resumes a recorded finalized copy. It never steals a live process or forces a wiki claim.
- `repository_policy_required`: the actual host protections are insufficient or unavailable. Stop publication and report the hosting/admin requirement; a passing local test never replaces host enforcement.

On an explicit cancellation request, `cancel --session ...` closes the pending proposal and releases only this private draft's claim. It preserves the draft. A previously published receipt instead reports publication and requires a new approved revert. After cancellation/supersession, `cleanup --discard-private-draft` may remove the preserved copy only when the user explicitly authorized discarding it.

Use a new operation ID for a new user intent. Reuse it only for retries of that intent. A reused ID belonging to another user/effect is rejected.

## Read and export published data

A reader uses an immutable, verified snapshot, not an in-progress candidate:

```text
python <skill-root>/scripts/team_git.py read --target <git-wiki> --work-dir <new-private-reader-runtime>
python <skill-root>/scripts/team_git.py read-run --session <reader>/session.json --helper crm_query.py -- find --object company
```

The response names its revision. After verified publication, `sync --target <git-wiki>` can update a clean shared clone with a verified fast-forward. It preserves local modifications and refuses a wrong local branch or changed remote instead of resetting files. Reader snapshots are unaffected by another agent's private maintenance lock. Open their generated HTML or read their Markdown normally.

Exports use `read-run` with `crm_export.py`, `export_wiki_skill.py` or `export_okf_bundle.py` and a new output under the private reader runtime. Check the export permission, show/disclose the number and types of records with personal data as required by the existing export procedure, and transfer only the requested result to the user's selected destination. The helper checks current membership/export policy even when reading an earlier released snapshot.

Frozen `--exclude-crm` exports also omit team membership configuration and operation receipts. A frozen export remains read-only and never becomes a team writer.

## History and cleanup

`history-audit --target <git-wiki>` reports local reachable commits/refs and the explicitly unverified scope of other clones, PR caches, exports and backups. It does not purge history or claim to have enumerated external copies. Keep operational retention, hosting support and backup-restoration procedures separate from routine publication.

Previously erased record identities cannot be restored from an older Git copy. Event cursor rollback for an unchanged workflow is blocked, so restoring an old state cannot silently replay already consumed events. Reimporting information as a genuinely new record is a separate request, not proof that earlier external copies were removed.

After verified publication, use `cleanup --session ...` to remove helper-owned private workspace copies, previews and bound request copies. Additional private changes are preserved rather than discarded. Original selected inputs and user exports remain untouched and are named in the cleanup result. The agent must separately remove its own temporary native plans/requests when no longer needed, while preserving the user's originals and deliverables. Cleaning a private runtime does not erase Git history.
