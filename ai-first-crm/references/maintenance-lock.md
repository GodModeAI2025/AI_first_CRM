# Team maintenance lock

This is the complete lock procedure from `SKILL.md`. Read it completely whenever acquisition does not simply succeed, when a claim is reported as held, contended, expired, superseded, or left behind, and before any takeover or forced acquisition.

Every mutating or maintenance invocation of this skill, including initialize, ingest, maintain, repair, migrate, release, lint, and every CRM change (records, imports, the data model, views, workflows, e-mail import, and campaigns), must own a claim on the target wiki for its entire lifetime. Resolve the target, then acquire the claim before inspecting wiki contents, converting sources, invoking another bundled process, or changing any target file. Read-only consumers use the separate query skill and verify a released manifest instead of claiming the wiki:

```text
<python> <skill-root>/scripts/wiki_lock.py acquire --target <wiki> --owner <agent-or-run-id> --operation <short-description> --token-file <private-runtime-file>
```

The command writes one claim file into `<wiki>/.llmwiki.lock/`, writes the private capability to a mode-0600 runtime file outside the wiki, and never prints the token. The wiki may be curated by a team, so the claim belongs to a maintainer slot that defaults to `<user>@<host>`; pass `--maintainer <slot>` only when the user names a different one, and never reuse a teammate's slot. Invoke allowlisted writers through `scripts/run_locked.py --token-file <runtime-file> --helper <helper> ...`; never read, print, interpolate, or delegate the token. A delegated agent may draft only in an external staging directory and must never receive the token, invoke a locked writer, or write the canonical target. Every locked helper call refreshes the claim's lease; refresh it explicitly after a long pause:

```text
<python> <skill-root>/scripts/wiki_lock.py verify --target <wiki> --token-file <private-runtime-file>
<python> <skill-root>/scripts/wiki_lock.py heartbeat --target <wiki> --token-file <private-runtime-file>
```

If acquisition reports `acquired: false` with `state: held`, another claim holds the wiki: stop before starting any wiki process and report the maintainer, owner, operation, and acquisition time. Do not infer that a claim is stale, do not reuse another run's token, and do not delete or replace another maintainer's claim file. If it reports `state: contended` with a `retracted_claim`, two maintainers acquired in the same moment; this run's claim was taken back again and nothing was written, so agree who continues and acquire once more.

If any command reports `state: contended`, stop and change nothing. Two maintainers hold effective claims, a synchronization client made a conflict copy of a claim, or a claim is unreadable or timestamped in the future. Show the reported problems and holders, and tell the user that the team must agree who continues and that everybody else runs `wiki_lock.py withdraw --target <wiki> --reason <reason>` on their own machine. Never resolve contention by forcing or by deleting a file yourself.

If a run on this machine was abandoned and its runtime token is gone, withdraw that own claim rather than overriding:

```text
<python> <skill-root>/scripts/wiki_lock.py withdraw --target <wiki> --reason <reason>
```

For a standalone request that only initializes and publishes an empty wiki foundation, use `scripts/initialize_wiki.py` instead of acquiring the claim separately. That deterministic wrapper acquires the claim internally, invokes initialization, builds the graph, lints, publishes version `0.1.0`, and releases it on success or failure. Its top-level invocation contains no lock token. Do not call it while already holding a claim or for an existing initialized wiki.

When a teammate's claim is reported as `expired: true`, the supported way to continue is the recorded two-step handover, not an override. Run it once to declare the takeover, and tell the user when it becomes effective. Tell them as well that the other maintainer can cancel it simply by working again: any movement of that claim voids the declaration, and the other maintainer learns about it from the locked call they run anyway. Run the identical command a second time after that moment:

```text
<python> <skill-root>/scripts/wiki_lock.py acquire --target <wiki> --owner <agent-or-run-id> --operation <short-description> --token-file <private-runtime-file> --take-over --reason <reason>
```

An explicit emergency override exists in addition, but use it only after the user confirms that the prior run may be displaced:

```text
<python> <skill-root>/scripts/wiki_lock.py acquire --target <wiki> --owner <agent-or-run-id> --operation <short-description> --token-file <private-runtime-file> --force --reason <approved-reason>
```

Force acquisition supersedes every current claim and invalidates its token for future bundled processes; it cannot terminate an already running external process. It is also the only way to clear a conflict copy of the lock itself, which otherwise keeps the wiki contended; report every entry of `cleared_conflict_copies` to the user and record it in the change log, because that file was the evidence. Explain that residual risk before requesting approval. Never invoke `--force` merely because a claim is old or inconvenient, and never as a shortcut past a takeover window or a contended lock.

Keep the claim while waiting for an in-scope user decision. Release it on successful completion, explicit cancellation, or abandonment of the run, but only with the owned token:

```text
<python> <skill-root>/scripts/wiki_lock.py release --target <wiki> --token-file <private-runtime-file> --remove-token-file
```

If a helper reports `state: superseded`, another maintainer completed a handover or an approved override. Stop writing immediately, report what this run had already changed, and re-acquire before continuing. A completed handover and an approved override both remove the claim they replace, so the evicted run stays evicted even after the new holder releases. If release reports a token mismatch, do not remove the current claim. If release reports `lock_directory_remains`, say so: no claim is held any more, but readers keep reporting `wiki_busy` until whatever is left inside `.llmwiki.lock` has been inspected and removed. Tell the user when a paused run intentionally keeps its claim and confirm successful release in the completion report.
