---
title: Declarative workflow
description: All infrastructure changes route through Git. How submit-suggestion and Minty enforce it.
sidebar:
  order: 7
---

The Platform Agent's `SOUL.md` forbids direct infrastructure mutations. When the agent has a fix in mind — a policy update, a node pool tweak, a security patch, a namespace addition — it doesn't `kubectl apply`. It writes the change into your **GitOps repo** as a **pull request** via the `submit-suggestion` skill, using a short-lived GitHub token minted on demand by **Minty**.

## Why

- **Human review.** Every infrastructure change gets seen before it hits prod. The PR is the audit trail.
- **Rollback via revert.** A bad remediation is one revert away from undone.
- **Compatibility with your existing GitOps.** ArgoCD, Flux, RootSync — whichever reconciler you already run applies the merged change. The agent doesn't compete with your reconciler.
- **Least privilege on the cluster.** The agent's Kubernetes identity cannot mutate workloads or cluster state — its only write grant is a leader-election housekeeping Role confined to its own namespace — so even a misled persona cannot change a cluster through the Kubernetes API. Its GCP identity is a separate question, governed by the provisioning-time permission set (`read-only` by default, `gke-admin` as an opt-in). See [Security &amp; IAM](/kube-agents/reference/security-and-iam/#what-the-agent-can-and-cannot-do).

## The `submit-suggestion` skill

Source: [`agents/platform/skills/submit-suggestion/`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/skills/submit-suggestion).

The agent invokes this skill whenever an SOP or on-request task decides "propose a change". The pod holds no checkout of its own; the skill's helper makes one, from the repository URL the agent resolves on startup out of `/opt/data/SETTINGS.md` (per `SOUL.md §1`). The flow:

1. Runs `./skills/submit-suggestion/scripts/submit_suggestion.py prepare --branch platform-agent/<change_type>-<target_id>` (e.g. `platform-agent/upgrade-policy-baseline`). That leases a private clone, refreshes it, cuts the topic branch off `origin/main`, and prints the workspace path as JSON.
2. Applies the change **inside the printed workspace** (file writes, YAML patches), then stages **only** the specific files it edited — `git add .` / `git add -A` are explicitly forbidden — and commits using Conventional Commit messages.
3. Runs the same helper with `submit --workspace … --branch … --title … --body …`, which mints a fresh GitHub App token (via `github_token_refresh.py`), pushes the branch, and opens a PR against `main` with `gh pr create`.
4. The script prints the PR URL to stdout; the agent posts it to Chat.

The lease in step 1 is what keeps concurrent agents apart: a Pod runs five audit crons alongside every kanban worker, and they used to share one working tree. `submit` refuses outright if the workspace it is handed belongs to another lease, and the credential proxy refuses tree-mutating `git` anywhere outside a leased directory — see [Credential isolation](/kube-agents/reference/credential-isolation/), with [`docs/designs/gitops-workspace-leases.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/gitops-workspace-leases.md) canonical for the layout.

Safety red lines enforced by the skill: direct/manual cluster mutations are forbidden, blanket staging (`git add .`) is refused, and `submit_suggestion.py` hard-blocks pushes to the protected branches `main`, `master`, and `production`. The push is `--force-with-lease`, so re-submitting after review feedback updates the existing PR branch but will not overwrite one somebody else has moved.

## The `fleet-audit` skill

Source: [`agents/platform/skills/fleet-audit/`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/skills/fleet-audit).

`submit-suggestion` fits a one-off change: the agent decides what to propose, writes the body, and opens a PR. A recurring [fleet audit](/kube-agents/concepts/autonomous-watchdogs/) does not fit that shape. A daily audit using `submit-suggestion` would open a near-identical PR every morning; and an audit report is not a diff at all, so a PR is the wrong container for it — a reviewer would have to accept or reject every finding at once, a force-push would orphan their line comments each run, and closing the PR on a clean fleet would read as _rejected_ rather than _done_. `fleet-audit` is the second write path, and it inverts the division of labour: **the model produces evidence, the script produces the published artefacts.**

The agent's only output is a validated `findings.json` — one entry per deviation, each carrying the literal read-only command that proves it and a `recommendation` (action, rationale, risk). `audit_report.py` does the rest:

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit <audit-id>
# … the agent inspects the fleet read-only and writes findings.json …
./skills/fleet-audit/scripts/audit_report.py finish --audit <audit-id> --findings-file <path>
```

What it publishes has two tiers: a durable report that is always there, and — only where there is something to merge — narrow pull requests that carry an actual diff.

### Tier 1 — the ledger issue

Each audit stream owns exactly one **GitHub issue**, rewritten in place on every run and labelled `agent:audit`, `audit:<audit-id>`, and `severity:<highest severity present>`. Its title is generated (`[audit] <human name> — <n> findings (<c> critical)`), and its body carries the scope table, a findings table with a state column whose every row links to the finding it names, and per-finding detail: evidence, impact, the finding's own id, recommendation, remediation, and a link to that finding's remediation PR where one exists.

A run that finds nothing **closes the issue as completed**, and closes any remediation PRs still open for the stream. That is the point of the shape: a closed issue reads as _done_.

Unless the run could not see the whole fleet. A cluster that was skipped, or one that was read but where some checks could not run, makes the run **partial** — and a finding's absence from a cluster nobody looked at is not evidence it was fixed. Over a partial run the ledger stays open with a comment naming the gaps, nothing is announced as resolved, and no remediation PR is retired.

### Tier 2 — remediation pull requests

A finding whose remediation is a manifest can be promoted into its own pull request on `platform-agent/fix-<audit-id>-<slug>-<digest>`, based on `main`, carrying only that finding's manifest and linked back to the ledger with `Part of #<issue>`. It takes the ledger's labels plus `audit:remediation`. Findings whose `remediation.path` values overlap share a single PR, since separate branches touching the same file would conflict on merge.

The branch name is the only join key between a finding and its PR — nothing is stored outside GitHub — so the digest is taken over the group's sorted set of remediation **paths**, not over its finding ids. Ids are regenerated on every run; the files a fix touches are what actually persists.

Promotion is hybrid:

- **Automatic** when the finding is `critical`, its remediation is a `manifest`, and there is no _live_ pull request on its branch — capped at five auto-promotions per run, with any withheld findings named in the ledger as awaiting an explicit request.
- **On request** otherwise. Someone with write access on the repo comments `/remediate <finding-id>` — or `/remediate all` — on the ledger issue, and the next run opens the PRs and replies with the links. Only `manifest` findings are promotable: naming a `gcloud` or `manual` finding gets one reply explaining that its fix is a command to run, not a file to merge. A command from an author without write access gets one reply and nothing else.

Every request gets exactly one answer and the answer is never silence, because a command that vanished is indistinguishable from an audit that has not run yet. An acknowledgement names each target and what became of it — the PR's URL, already open and left untouched, superseded by a human close, or queued for a retry — rather than a count. A refusal says which of the reasons applies, and a request the parser cannot honour at all (the command mid-sentence rather than on its own line, or with no target after it) gets a reply carrying the correct syntax and the ids that would have worked. A `/remediate` inside a code span is prose about the command, not an attempt at it — which is how the harness avoids answering its own comments. Each answer is recorded behind a hidden marker keyed on the triggering comment, so a standing request is answered once rather than every morning.

Each finding's state is recomputed every run from two facts — whether it still reproduces, and what its remediation PR is doing — and never stored. The ledger renders exactly one of `open`, `fix proposed`, `⚠ fix merged, still reproduces`, `resolved (fix merged)`, `resolved`, `fix refused`, or `fix withdrawn, awaiting re-proposal`. Two of those act on the PR side, and the second is one the old PR-as-report model had no way to express:

- A remediation PR whose finding no longer reproduces is **closed automatically**, with a comment naming the command that no longer reproduces. The branch is deliberately **not** deleted, so any human fixup pushed to it survives and the PR can be reopened.
- A finding that still reproduces after its PR merged is rendered with a **"fix merged, still reproduces"** warning and the merged PR gets one comment. It is never reopened.

The last two states are the same event seen from opposite sides: a fix PR that is closed, unmerged, while the finding is still there. Who closed it decides everything, and the `audit:stale-closed` label is how the harness tells its own work apart from a person's. Its own stale-closes carry that label and render as **`fix withdrawn, awaiting re-proposal`** — the finding is treated as having no PR at all, so it is promotable again on the usual terms. A PR a **human** closed renders as **`fix refused`** and is final: re-proposing it would overrule a person every morning. The escape hatch there is `/remediate <finding-id>`, from someone with write access and written _after_ the close — an older command still sitting in the thread is reported as superseded rather than acted on, since comments are never edited away and one would otherwise re-open the same close forever.

### What the script owns

Three properties follow from the script owning the artefacts rather than the model:

- **One ledger per audit stream.** The `--audit` id is checked against a fixed allowlist, and the branch and label names are derived from it rather than passed in, so a typo cannot open a sixth stream. The agent never calls `gh issue create` itself.
- **A computable delta.** The issue body carries a hidden `<!-- audit-findings: [...] -->` block; the next run diffs finding ids against it. Stability is not asked of the model: the id is derived in code from `(check, cluster, namespace, object)` and any `id` in the findings file is discarded, so the same problem keeps the same id without anyone remembering to make it so. A second hidden line stamps which identity scheme minted the ids, and a run that reads a block from a different scheme withholds `resolved` for one run rather than reporting a renamed finding as a fixed one.
- **No invented output.** The model never writes the title, body, commit message, or any timestamp — so two runs against an unchanged fleet produce an unchanged ledger.

The `agent:audit` label is also what keeps the two issue-writing watchdogs apart: the `github-issue-resolver` job's poll query excludes it, so it never tries to "resolve" an audit ledger.

`fleet-audit` shares `submit-suggestion`'s guardrails: same Minty token path, the same refusal of `git add .` / `git add -A`, and the same hard block on force-pushing `main`, `master`, or `production`.

## Minty (GitHub Token Minter)

Source: [`k8s-operator/config/integrations/github/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github).

Minty is a small in-cluster service that brokers GitHub App installation tokens without any long-lived secret ever touching the agent's pod.

### How it works

1. A GitHub App is created (once, by you) with the needed permissions (`contents:write`, `pull_requests:write`, `issues:write` — `fleet-audit` publishes its ledger as an issue) and installed on the target repo.
2. The App's private key is imported into a **GCP KMS asymmetric signing key** (keyring `github-token-minter-keyring`, key `github-token-minter-key`, created by `provision_10_deploy_github_minter.sh`) — the raw key material never lives outside KMS.
3. When `submit-suggestion` needs a token, the credential broker calls Minty (default endpoint `http://github-token-minter.kubeagents-system.svc.cluster.local:8080/token`) using the agent's Workload Identity.
4. Minty asks KMS to sign a JWT with the imported private key.
5. Minty exchanges the JWT with GitHub for a **short-lived installation token scoped to the target repository**.
6. Minty returns the token to the caller.

### Recovery

If a git operation fails with an auth error (e.g. `fatal: Authentication failed`, `could not read Username`), `SOUL.md §3` requires the agent to run the packaged token refresher:

```bash
# outside a git repo
./scripts/github_token_refresh.py <owner>/<repo>
# inside a git repo (repo inferred from remote.origin.url)
./scripts/github_token_refresh.py
```

which triggers a fresh mint from Minty and caches it, then retries the command. The recovery ladder (`§4`) caps retries at **5 iterations or ~10 minutes per distinct blocker** before escalating.

## Complementary integrations

Alongside GitHub PR flows, the persona explicitly names other declarative pipelines it will use when they're the active workflow:

- **Config Connector** — for GCP resources modeled as Kubernetes CRs.
- **ArgoCD / Flux** — inspecting `RootSync` state and Application health as part of diagnostics.
- **GKE Hub fleet membership / Connect Gateway** — for multi-cluster targeting.

`SOUL.md §4` requires the agent to inspect these before manual intervention.

## Anti-patterns

Explicitly called out as forbidden in `SOUL.md`:

- Running raw `kubectl apply` against a live cluster for infrastructure changes.
- Configuring `git` credential helpers manually.
- Running ad-hoc `git clone` against the GitOps repo for change submission, or driving `git`/`gh` directly to open a PR or file an issue. `SOUL.md §3.2` names exactly two packaged skills that may own the write path: `submit-suggestion` for a one-off change, `fleet-audit` for a scheduled audit run.
- Outputting raw tool schemas, JSON payloads, or exit codes in user-facing messages.

## Where to go next

- [Deploy → Token minter](/kube-agents/deploy/token-minter/) — Minty install details.
- [Concepts → Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks that invoke `submit-suggestion`.
- [Reference → Attribution](/kube-agents/reference/attribution/) — how a PR ties back to the authenticated human who requested it.
