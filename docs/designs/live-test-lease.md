# Live-Test Lease

> **STATUS — design of record; implemented.** `scripts/live_test_lease.py` and the hook wiring in
> `.claude/settings.json.example` are what the repository ships.

**Scope:** How several agents share one running kube-agents installation without overwriting each
other's live validation.
**Owns:** the `live-test-lease` ConfigMap, `scripts/live_test_lease.py`, and the command
classification that decides which Bash commands need the lease. The live-validation requirement
itself belongs to [`AGENTS.md`](../../AGENTS.md), "Pull Request Hygiene". Leasing GitOps clones
inside the agent pod is the same idea one layer down and belongs to
[`gitops-workspace-leases.md`](gitops-workspace-leases.md).

---

## The problem

Every pull request here must be exercised against a real installation, and standing up a GKE
cluster per contributor is not realistic — so a team shares one. Then two agents work at once. One
patches the `PlatformAgent` env while the other rolls a new image tag into the same Deployment;
each reads back a pod that has both changes, or neither, and writes a **Live validation** section
describing an install that no longer exists. The loser's evidence is not wrong-looking. It is a
successful `kubectl get` against state somebody else produced.

Nothing in the toolchain notices. The unit tests pass, `make docs-check` passes, and the failure
surfaces days later as a change that never worked.

## The lease

A mutating command takes a lease first. The lease is a ConfigMap named `live-test-lease` in the
install's own namespace. Its `data` holds the token that proves ownership, the session id behind
it, and the fields a blocked agent needs in order to decide what to do: `holder` (`user@host`),
working directory, branch, PR number and note if given, acquisition time, TTL, and expiry.

It lives **in the cluster it protects** rather than on the workstation that took it. That is what
makes it a lease rather than a local mutex: it is visible to anyone with a kubeconfig for that
cluster — a second checkout, a colleague's laptop, CI — which is exactly the population that can
collide.

Acquisition is a compare-and-swap. A free install is claimed with `kubectl create`, which fails
`AlreadyExists` if another agent won the race; an expired lease is taken over with `kubectl replace`
carrying the `resourceVersion` read a moment earlier, which fails `Conflict` for the same reason.
Two agents racing the same expired lease cannot both come away believing they hold it.

Holder identity is a random token minted at acquire time and stored locally under
`$XDG_STATE_HOME/kube-agents/live-test-lease/` (`~/.local/state` when that is unset), keyed on the
session id: `KUBE_AGENTS_LEASE_SESSION` if the harness sets it, otherwise `CLAUDE_PID`, otherwise
the parent process id. A Claude Code session shares `CLAUDE_PID` with its subagents and not with a
second concurrent CLI, so a subagent inherits its parent's claim while a separate session is a
separate holder. The parent-pid fallback is a last resort — it makes the key stable within one
shell rather than across a session — which is why another harness should set
`KUBE_AGENTS_LEASE_SESSION` explicitly.

**Not every holder is a person.** The nightly reconcile of `autopush` and `staging`
(`scripts/release/reconcile_environment.sh`) takes the same lease before it applies, under
`KUBE_AGENTS_LEASE_SESSION=gha-<run_id>`, and defers to the next night rather than overwriting an
agent's evidence mid-run. So a lease held by `gha-…` is a scheduled infrastructure apply, not a
colleague: it releases itself when the run ends, and the run it names is readable in the Actions
tab. Do not `steal` one — the apply it is protecting is a `terraform apply` against a live cluster.

Losing the local token does not lose the install. The record also carries the session id, so the
hook can tell "another agent holds this" from "you hold this, under a token this machine no longer
has" and send you to `steal` rather than to a wait that will never end.

The local file records the context, namespace, and kubeconfig the lease was taken against, not just
the token. Those files are how `release --all` and `SessionEnd` decide what to release: the set of
installs a directory can discover is a different set from the ones a session actually claimed. `cd
../other-checkout && ./upgrade.sh` takes a lease on that checkout's install, and by the time the
session ends the working directory is back somewhere that cannot see it. Releasing what the exit
directory resolves to would drop nothing and leave the real lease standing for the rest of its hour.

Leases expire after 60 minutes and renew lazily past the halfway mark, so an active session keeps
its claim and an abandoned one does not hold the install hostage. `SessionEnd` releases whatever
the session holds — except when its `reason` is `clear` or `resume`, which Claude Code reports on a
process that carries straight on under the same holder key. Releasing there would hand the install
to the next agent mid-test, and silently: the session that gave it up sees nothing.

## Why a hook, not an instruction

The lease could have been a line in `AGENTS.md` telling agents to run `acquire` first. That does
not work, and the reason is not carelessness: advisory exclusion holds only when every participant
remembers, and the participants are LLM sessions and their subagents, each deep in a task, reading
a rule written hundreds of lines away from the command they are about to run.

So enforcement sits in a `PreToolUse` hook, which is the only place a mutating command can be
stopped before it runs. The hook denies the command while another agent holds the lease — naming
the holder, their branch, and the expiry, so the blocked agent knows whether to wait or to
coordinate — and **auto-acquires when the lease is free**, so nobody has to know the protocol
exists.

The wiring ships as an example you copy into place, once per checkout:

```bash
cp .claude/settings.json.example .claude/settings.json
```

`.claude/settings.json` is gitignored, so the copy is yours to edit and never turns up in a diff.
It is not committed for the reason under [Limits](#limits) — a tracked settings file is branch
content that Claude Code runs unprompted, and this repository's own review workflow checks out fork
branches. Merge the two `hooks` entries by hand if you already keep a `settings.json`. Doing the
lease by hand stays available for the cases the classifier cannot see:

```bash
python3 scripts/live_test_lease.py status                    # every protected install
python3 scripts/live_test_lease.py acquire --pr 123 --note "operator env plumbing"
python3 scripts/live_test_lease.py release
```

`--env <name>` picks the install when more than one resolves, and is required there; with a single
install it is optional, and `status` reports all of them either way.

## What counts as a mutation

Read-only commands are never blocked and never take the lease — `get`, `describe`, `logs`, `top`,
`events` and their kin run untouched, which keeps the common case free. A command is a mutation
only if the classifier recognises its command word, and it recognises the families that reach a
running install:

- **`kubectl`**, by verb. `exec`, `cp`, and `port-forward` count as mutations — they reach inside
  the running install — and `auth` is a read apart from `auth reconcile`, which writes RBAC. The
  exception runs the other way too: `rollout status` and `rollout history` are how you watch a
  deploy somebody else started, and any `--dry-run` short of `=none` persists nothing. A verb in
  neither list is treated as mutating: a plugin nobody anticipated is more likely to write than to
  read, and the cost of being wrong is asymmetric.
- **`helm`**, the state-changing **`terraform`** subcommands, and the installer and redeploy entry
  points — `install.sh`, `uninstall.sh`, `upgrade.sh`, `lifecycle.sh`, `dev_rebuild_agent.sh`, and
  the `make` targets that redeploy. The root installers **ignore your current kubectl context
  entirely** and act on the install its `install.env` names, so the checkout is
  what resolves their target when nothing more direct is on the line. Asking one of them for
  `--help`, a `--dry-run` or a `--plan` is not running it — `upgrade.sh --plan` reads the install's
  Terraform state but takes no lock and changes nothing on the cluster, and what it does write
  (`terraform.tfvars`, `backend_override.tf`) is local to the checkout. An `agentplugins/*/install.sh` shares that basename
  and nothing else: it applies an AgentPlugin CR through your current context and reads no
  install configuration, so it resolves the way a `kubectl` does.
- **`gcloud`** cluster mutations, and `pubsub publish`, which drives a real agent turn. The match
  is on the install's markers rather than on a hardcoded topic: a publish that reaches an install
  names its project, or a topic the install records.
- **`docker push`**, and the `--push` that `docker buildx build` does instead of one, to the
  registry prefix the install records. The push itself touches no cluster, but overwriting a tag
  the install is running is how one agent's image became another's, which is the incident this
  exists to prevent. A stock install records the public
  [`DEFAULT_REGISTRY_PREFIX`](../../scripts/installer/installer_common.sh) rather than a private
  one, so this guard is only as specific as the prefix the install was given. A push whose image ref
  did not expand asks rather than passing, because an unreadable ref and an absent one are different
  answers. That covers the ref that says nothing at all — `docker push $(cat last-image)` — and the
  half-spelled one, where what the ref does say is still on the path to a protected registry:
  The configuration carries `PROJECT_ID`, so `docker push $REGION-docker.pkg.dev/$PROJECT_ID/platform:dev`
  and `docker push us-central1-docker.pkg.dev/$PROJECT_ID/platform:dev` both ask, while
  `us-central1-docker.pkg.dev/some-other-project/x:$TAG` has already answered the question and stays
  silent.

`scripts/live_test_lease.py` is the source of truth for the membership of each family — the
constants at the top of it, not this list. Its tests re-derive the `make` targets from the two
Makefiles, so a `deploy-*` target added later cannot ship unguarded past a green suite.

Classification is by **command word**, not by tokens present on the line. `grep -rn kubectl
deploy/` and `cat upgrade.sh` are reads, and a matcher that scans for a substring calls both of
them mutations — which would take an hour-long lease on a shared cluster for a command that touched
nothing, and would deny the same `grep` while somebody else holds it. Finding the command word
means following the shell rather than reading the line: `&&`, `|`, `;`, and a lone `&` separate
segments, but not inside quotes or a heredoc body, where they are text — `AGENTS.md` requires a
**Live validation** section naming what was run, so this repository's own `gh pr create` carries a
`kubectl patch` on a line of its own. Wrappers (`sudo`, `env`, `timeout`, `xargs`) and the shell
keywords that stand in front of a command word (`do`, `then`, a subshell's parenthesis) are
stripped, so the `kubectl delete` in a cleanup loop is still a `kubectl delete`. `bash -c "…"`
payloads, `eval`'s arguments, and `$(…)` substitutions are classified as the shell will actually run
them, and a function definition's `f() {` is stepped over the way a wrapper is. And a `cd`
changes the checkout, and so the install configuration, that the segments after it resolve against — including
when the install it lands on is one this session had not otherwise heard of.

It does not follow the shell everywhere. A `case` arm hides the command word behind its pattern
label — `case $E in prod) kubectl delete ns x;;` classifies as nothing — because the parsing that
would find it is the same widening of "skip tokens until something looks like a command" that makes
a real argument readable as one, and a false mutation denies an agent the cluster for an hour. The
gap is a silent pass on a form nobody writes by accident; if you are scripting one against a shared
install, take the lease by hand first.

Which install a segment acts on is settled by the most direct evidence available. For a command
that takes them — `kubectl`, `helm`, a plugin installer — an explicit `--context`, or the
`--kube-context` helm spells it as, wins outright, because naming an unprotected cluster is a
definite answer rather than a missing one, and `--kubeconfig` is next, since the live-test workflow
keeps a dedicated kubeconfig per install and a marker is only a substring that happened to appear
on the line. Everything else falls back to the
markers, then to the checkout's install configuration where that is the honest signal, then to the ambient
context. A context renamed locally is reached through an `aliases` entry, not by guessing.

## Which installs are protected

Discovered, never hardcoded. The checkout's `install.env` records the install it is pointed at —
as does a legacy `k8s-operator/scripts/vars.sh`, still read so a checkout from before that change
stays protected. Both are taken from the first directory up the tree that has either, and where
both exist their keys are merged with `install.env` winning, matching the order every shell front
door loads them in. `PROJECT_ID`, `CLUSTER_NAME`, and `REGION` compose the context
(`gke_<project>_<location>_<cluster>`, the name `installer_common.sh` itself reconstructs);
`REGISTRY_PREFIX` gives the prefix `docker push` is matched against — the installer always records
one, so `<REGION>-docker.pkg.dev/<PROJECT_ID>` is a fallback for a hand-written configuration that
omits it. `CHAT_TOPIC_NAME` becomes a marker, because a `gcloud pubsub publish` often names the
topic and nothing else on the line. The ConfigMap lives in `kubeagents-system` unless the install
records a `NAMESPACE`, which a stock configuration does not. An install with no `REGION` recorded
is not protected rather than guessed at.

Those keys are matched with a regex against an allowlist and the file is never sourced — it is
mode-600 install state that holds credentials, and sourcing a file to read a handful of variables
out of it executes everything else in there.

An install you protect but have no checkout of goes in `$KUBE_AGENTS_LIVE_TEST_ENVS` (default
`$XDG_CONFIG_HOME/kube-agents/live-test-envs.json`, and `~/.config` when that is unset); see
[`scripts/live_test_envs.example.json`](../../scripts/live_test_envs.example.json). With neither
present, nothing is protected and the hook exits before it can cost anything — which is the state
every contributor who has not opted in is in.

## Failure modes it takes a position on

- **The cluster cannot be consulted.** The hook asks rather than deciding. Treating an unreachable
  cluster as an unheld lease is how the mechanism fails open at exactly the wrong moment; treating
  it as held blocks work over a flaky network. Only one error means the lease is free — the
  ConfigMap's own `not found` — and the read matches that message rather than the phrase, which a
  missing namespace, an absent auth plugin, and a `kubectl` that is not on `PATH` all also say.
- **The target cannot be resolved.** For an installer, a `helm upgrade`, or a `terraform apply` —
  commands that reconfigure an install wholesale — the hook asks. For an ordinary `kubectl` it does
  not: protected installs are in the kubeconfig by construction, `kubectl` runs constantly, and a
  prompt on every unresolvable invocation is noise that gets the hook turned off.
- **The hook itself fails.** Claude Code treats a non-zero hook exit other than 2 as a hook error
  and lets the command through, so an unhandled exception would be a silent fail-open. Anything
  unexpected is caught and turned into the same `ask` an unreachable cluster gets.
- **The hook runs out of time.** A killed hook is a hook error, and therefore the same fail-open,
  so `PreToolUse` declares a budget with room for its worst case: three `kubectl` calls plus one
  context probe, cached per classification so a compound line pays for one rather than one per
  segment. `SessionEnd` declares one for the opposite reason — Claude Code gives that phase 1.5
  seconds unless a hook asks for more, which is not enough to reach a cluster, and a release that
  never runs strands the lease for its full TTL. Both budgets are asserted against
  `KUBECTL_TIMEOUT` in the tests, because neither failure says anything at the time.
- **The holder is gone.** Nothing detects that. `steal` goes by the clock alone — it takes over an
  expired lease, and refuses an unexpired one without `--force`, so a session that died a minute
  after acquiring holds the install for the rest of its hour unless someone forces it. That is the
  deliberate side to err on: a live holder is an agent mid-test, not a stale lock, and `--force` is
  the point at which you have coordinated and know the session is gone.

## Limits

Enforcement is Claude Code-specific — the CLI is harness-agnostic and can be run from any agent or
a plain shell, but only the hook makes it mandatory. An agent working through another harness, or a
human running `kubectl` directly, is on the honour system.

**The hook is opt-in, and that is a real gap.** A contributor who never runs the `cp` above has no
enforcement at all — which is the failure mode a committed `.claude/settings.json` would have
closed, and closing it is not worth what it costs. A tracked settings file is branch content that
Claude Code executes without being asked. A pull request can change both it and the script it runs,
so checking out a fork's branch into an already-trusted checkout — or into a worktree of one, which
is how `.claude/commands/pr-review-batch.md` reviews every open pull request — would arm whatever
that branch says on the reviewer's next Bash call, with their kubeconfigs and their mode-600
install configuration in reach. Workspace trust is granted per directory and is not re-asked when the branch
changes, so the reviewing case is exactly the one it does not cover. Shipping an example keeps the
reviewer's execution path free of fork content; the cost is that the protection has to be asked
for.

Once copied, the file is repository-controlled code running on a contributor's machine on every
Bash tool call, and the script is written for that: it reads the install configuration with a
regex over an allowlist rather than sourcing it — accepting both `K=V` and `export K=V`, since
`install.env` is a dotenv and `vars.sh` was generated with `printf %q` — exits before any cluster round-trip when nothing is configured,
and shells out to nothing but `kubectl` and `git`. Read the diff of `scripts/live_test_lease.py`
before pulling a branch you do not trust, since the copy points at the working tree's script.
Personal hooks unrelated to the lease belong in `.claude/settings.local.json`, which stays ignored
either way.

The lease renews only when another Bash call passes through the hook, so a single unattended run
longer than the TTL — `install.sh` against a fresh GKE cluster — expires mid-run and can legitimately
be taken over. Take a longer one by hand first: `acquire --ttl 180`.

A context renamed locally is invisible to discovery: the configuration records what the install
_is_, and
`gke_<project>_<location>_<cluster>` is the name `gcloud` writes, so a `kubectl config
rename-context` leaves commands naming the short form unresolvable. Add the new name to that
install's `aliases` in the config file, keyed on the **canonical** context. Aliases are further
names the classifier recognises and nothing more: every `kubectl` the tool runs itself goes through
`--context <install.context>`, so keying the entry on the renamed name instead yields two installs
for one cluster and still leases under the name the rename removed. That is also the limit of the
remedy — it restores classification, not the lease. For the lease to work the canonical name has to
be back in the kubeconfig, which `gcloud container clusters get-credentials` does, adding it
alongside the renamed one; without it every lease call answers `Unreachable` and the hook asks
about every mutating command. Nothing detects the rename for you.

The lease record is readable by anyone with `get configmap` in the install's namespace, and it
names the holder's `user@host`, their branch, and their working directory — enough to identify a
contributor and the path their checkout sits at. That is the same population that can already read
the install's pods and logs, and the alternative is a blocked agent being told only "held", which
is what makes the wait-or-coordinate decision impossible. Do not put anything else in `--note`.

The ConfigMap is contributor tooling that lives in the install's namespace; `release` removes it,
and an abandoned one expires rather than lingering as a live claim.
