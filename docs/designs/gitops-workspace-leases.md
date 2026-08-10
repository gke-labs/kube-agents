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
in the Pod. A PlatformAgent Pod runs five audit crons on colliding schedules, a Chat Agent, and one
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
closed enum, so it is a safe directory name by construction. `submit-suggestion` resolves `--lease` →
`$HERMES_KANBAN_TASK` (pinned into every dispatcher-spawned worker) → `$HERMES_SESSION_ID` → a
generated `adhoc-<8 hex>`. The identifier must be stable across invocations, because the agent runs
each shell command in a fresh process: a pid would hand `git commit` and the `submit` that follows it
two different clones. Every id is reduced to `[A-Za-z0-9._-]{1,64}`; one that sanitises to nothing is
refused rather than defaulted, because a shared default is the bug.

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

**fleet-audit** threads `lease=<audit-id>` through `start`, `remediate`, and `finish`. Five streams
that used to share one tree now hold five, so `finish`'s forced checkout and the untracked manifests
`start` left behind are no longer racing anyone.

**submit-suggestion** grows two subcommands. `prepare --branch <name>` leases a clone, resets it,
cuts the branch off the repository's default branch — `origin/HEAD`, overridable with
`GITOPS_BASE_BRANCH`, falling back to `main` — or off `origin/<name>` when that branch already
exists, so a second round of review feedback builds on the open pull request rather than replacing
it. It prints `{"workspace", "lease", "branch", "repo"}`; the agent
works inside the printed `workspace`. `submit --workspace <path> --branch --title --body` asserts
ownership first, verifies HEAD is on the named branch, then pushes and opens the pull request with
`cwd` set on every subprocess. The pre-`prepare` bare-flag call shape is still accepted as an alias
for `submit`, so a session already in flight does not die on "invalid choice".

`git push -f` becomes `git push --force-with-lease`. The force was there for a real reason — a card
that comes back for another round of review feedback has to update the branch its pull request already
points at — and `--force-with-lease` keeps that case while refusing to destroy a branch someone else
pushed. Deliberately **without** a `git fetch` first: fetching immediately before a force-with-lease
is the classic way to defeat it, because the fetch moves the remote-tracking ref onto whatever the
other agent just pushed and the lease then compares that value against itself. The ref the push is
leased against has to be the one `prepare` fetched.

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
