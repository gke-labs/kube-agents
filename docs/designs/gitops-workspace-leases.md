# GitOps Workspace Leases

> **STATUS — design of record; implemented.** The layout, the reaper, and the credential-proxy gate
> described here are what the harness ships.

**Scope:** How concurrent agents in one Pod write git without corrupting each other's working trees.
**Owns:** the `/opt/data/gitops` layout, the `.lease` marker, `gitops_workspace.py`, and the proxy's
`git.workspace.lease` rule. The proxy's other containment rules belong to
[`credential-isolation-design.md`](../credential-isolation-design.md).

---

## 1. The problem

`gitops_workspace.workspace_path()` used to derive the clone location as a pure function of the
repository name: `/opt/data/gitops/<owner>__<name>`. One repository, one clone, shared by every agent
in the Pod. A PlatformAgent Pod runs six audit crons on colliding schedules, a Chat Agent, and one
kanban worker per dispatched card, all against the same PersistentVolumeClaim. Three consequences
followed.

**`submit_suggestion.py` had no working directory at all.** It ran `git push -f` in whatever
directory the agent's shell happened to be in, and its SKILL.md told the agent to `git checkout -b …`
without naming a directory either. In practice that meant branching inside the fleet-audit clone —
switching branches under a running audit — and force-pushing from it. The blind `-f` would also
discard another agent's branch of the same name without a word.

**The audit's lock covered the wrong window.** `audit_report.ensure_workspace` took a `flock` and
released it as soon as the clone was refreshed: a few milliseconds. The window that needs protecting
is the roughly ten minutes between `start` and `finish`, during which the agent writes untracked
remediation manifests into the tree and `finish` runs `git checkout --force -B <branch>`.

**`flock` cannot cover that window anyway.** `start` and `finish` are separate processes and the
file descriptor dies with each one. No advisory lock can span them.

## 2. Isolation, not serialisation

There will always be multiple operations happening by different agents, so nobody may wait on anybody
else. Each concurrent operation gets its own clone, keyed by a lease it owns.

```
/opt/data/gitops/
├── .lock                                  # short root flock: reap + mkdir + write .lease
├── compliance-audit/
│   ├── .lease                             # {"lease","owner","repo","created_at","refreshed_at","pid"}
│   └── acme__fleet/                       # the clone; every git and gh call runs here
├── t_751ffb70/                            # a kanban worker's submit-suggestion lease
│   ├── .lease
│   └── acme__fleet/
└── adhoc-9f3c1e07/
    ├── .lease
    └── acme__fleet/
```

**Path.** `<root>/<lease>/<owner>__<name>`. Deterministic given the lease, so `start` and `finish` —
separate processes — find the same tree with no lookup state between them.

**Lease key.** The fleet audit uses the audit id, which `validate_audit_id` already constrains to a
closed enum, so it is a safe directory name by construction. It is the only caller whose key is
constrained that way, not the only caller that leases a clone: two read-only scans lease here too —
`api_deprecation_scan.py`'s directory mode and `inspect-repository`'s `clone`, on the branch it
takes when content mode is unavailable — and
neither has an id of its own. The write skills are the ones that do not appear here at all; they
take their working copies through the version-control verbs instead, which key a copy on the
repository and the branch and need no lease to keep two of them apart — see §4. What the generic
path offers the callers with no id is `lease_id`: an explicit `--lease` → `$HERMES_KANBAN_TASK`
(pinned into every dispatcher-spawned worker) → `$HERMES_SESSION_ID` → a generated
`adhoc-<8 hex>`. The identifier must be stable across
invocations, because the agent runs each shell command in a fresh process: a pid would hand
`git commit` and the submit that follows it two different clones. Every id is reduced to
`[A-Za-z0-9._-]{1,64}`; one that sanitises to nothing is refused rather than defaulted, because a
shared default is the bug.

**Lease file.** `.lease` is written before the clone and its mtime refreshed on every
`ensure_workspace`. It is three things at once: the reaper's TTL anchor, the marker the proxy looks
for, and the ownership record clients check.

**Root lock.** `workspace_lock` survives, shrunk to what a lock can actually cover — reaping, `mkdir`,
and writing `.lease`. It is held for milliseconds and never spans a clone, a fetch, or an audit. It
remains best-effort: a read-only or absent volume costs a retry, not the day's audit.

**Reaper.** Under the root lock, lease directories whose `.lease` mtime is older than
`GITOPS_LEASE_TTL_HOURS` (default 24) are deleted and the removal logged. Only directories containing
a `.lease` are ever considered, so the legacy flat `<root>/<owner>__<name>` clone — and anything else
an operator left under the root — is safe by construction. The caller's own lease is always spared,
so a run straddling the TTL cannot delete the tree it is about to use.

### Why a full clone per lease

The GitOps repository is roughly 366 KB against 9.6 GB free on the volume, so a clone per lease is
cheap. `git worktree` would share the object store, but a shared `.git` is exactly the kind of common
mutable state this design exists to remove. Revisit only if a repository large enough to make clone
time hurt shows up.

## 3. Two layers of enforcement

**Proxy — the floor.** The credential proxy refuses tree-mutating `git` when the resolved working
directory is not inside a lease directory. It catches "an agent ran `git push` from its profile
directory" and any future skill that skips the convention entirely.

The rule uses an explicit **mutating-verb denylist** — `add`, `am`, `apply`, `branch`, `checkout`,
`cherry-pick`, `clean`, `commit`, `merge`, `mv`, `pull`, `push`, `rebase`, `reset`, `restore`,
`revert`, `rm`, `sparse-checkout`, `stash`, `submodule`, `switch`, `tag`, `update-ref`, `worktree` —
rather than a read-only allowlist. The set of
verbs that can stomp a working tree is closed and well known; the set of read verbs is not, and a new
one silently failing closed would be a worse failure than the race being fixed. `clone` is absent on
purpose: it runs at the lease root, one directory above a tree that does not exist yet. `fetch`,
`config`, `remote`, and every read verb are untouched. The last three in the list are there because
each is a tree write wearing another word: `pull` is `fetch` plus the `merge` or `rebase` beside it,
`submodule update` checks out whole directories, and `sparse-checkout set` adds and removes files
across the entire tree.

`-C` is applied the way git applies it — cumulatively, before the subcommand runs — so
`git -C /elsewhere commit` is checked against `/elsewhere` and not against the directory the caller
reported. Refusal comes back through the existing `SECURITY_POLICY_BLOCKED` path with rule
`git.workspace.lease`, so the sandbox wrapper already renders it, and the message names the skill step
to run. `CREDENTIAL_PROXY_REQUIRE_GIT_LEASE=0` disables the gate for a skill that has not been
migrated, without shipping a new image.

The proxy checks **presence only** — not expiry, not ownership. Expiry is the reaper's job, and
keeping the proxy ignorant of the lease format avoids coupling it to the client.

**Client — ownership.** `gitops_workspace.assert_lease_owner` reads `<workspace>/../.lease` and
refuses if the recorded lease is not the caller's own. This is the layer that stops the original
incident: one agent writing inside another agent's tree. The proxy cannot do it. The sandbox wrapper
sends an argument array and `os.getcwd()` and no caller identity, so the sidecar can tell that a push
is happening inside _some_ lease but never whose.

## 4. Consequences for the two skills

**fleet-audit** threads `lease=<audit-id>` through `start`, `remediate`, and `finish`. Each of the nine
audit streams holds a tree of its own, so `finish`'s forced checkout and the untracked manifests
`start` left behind race nobody.

**submit-suggestion** grew the two subcommands this section gave it — `prepare` and `submit` — and
then left the lease behind. It works over the version-control verbs now, and the verbs hand out one
working copy per repository _and branch_, under a scratch root that is the container's alone. The
branch is in the name because the scratch root is not per card: two cards suggesting changes to one
GitOps repository are ordinary, and a copy named for the repository alone would make the second
card's `prepare` either refuse or, with `--force`, delete the first card's unpublished work. Two
cards, two copies, and no lease between them — so there is nothing to lease and no
`assert_lease_owner` for `submit` to call: `--workspace` and `--lease` are both retired flags that
warn and are ignored. The lease's reaper went with the lease, and `clone` took its job: before
bringing a copy down it deletes every other copy whose record, index and HEAD log are all older than
`KUBE_AGENTS_VCS_TTL_HOURS` (default 24, 0 turns it off). Nothing else would — a landed `submit`
leaves its copy behind, and a second round re-clones the branch anyway. Where a repository is cloned twice, `--repo` no longer identifies a copy on
its own; the directory the caller is standing in does, and a caller that is standing nowhere is
refused with the paths. `prepare --branch <name>` brings the repository down and
prints `{"workspace", "repo", "branch", "base", "started_from", "proposal"}`; the agent works inside
the printed `workspace`. Which branch the copy is taken of is decided by asking the forge rather than
by inspecting refs: a branch carrying an **open proposal** is one this run is adding to, so the copy
is of that branch and its revisions come with it, while a branch with no open proposal is one this
run is starting, so the copy is of the default branch and the branch is cut from it. That is the same
outcome the `origin/<name>`-exists test reached for, decided on the question that actually matters —
a branch reused after its proposal merged must not be added to.

The protection survives, taken from the thing being protected rather than from a file beside it.
`prepare` refuses to replace a copy that holds work which was never sent up; `--force` is the way
past, and it discards that work. Publishing is fast-forward only, which retires
`--force-with-lease` and the whole argument under it: there is no remote-tracking ref on this side to
lease against and no fetch that could defeat one, because the comparison is the forge's own and is
made at the moment of the push. A branch somebody else moved is refused by name.

[`version-control-support.md`](version-control-support.md) is canonical for the verbs.

## 5. Limits

- The proxy gate is a floor, not an ownership check. Two agents that both hold valid leases can still
  write in each other's trees if one of them passes the other's path to a helper that skips
  `assert_lease_owner`.
- An `adhoc-<hex>` lease is isolated but not recoverable: a later process that did not keep the path
  cannot find the tree again. It is reaped on the TTL like any other.
- A crashed run leaves its clone on disk for up to `GITOPS_LEASE_TTL_HOURS`. That is a disk-space
  trade, taken because reaping a live lease would be far worse than keeping a dead one.
- `github-issue-resolver/scripts/resolver.py` still carries its own copy of the repository-resolution
  logic. Folding it in was out of scope for this change.
