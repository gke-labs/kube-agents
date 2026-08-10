---
title: Proactive autonomy
description: The background watchdogs that make kube-agents more than a chatbot — audit, remediate, PR, alert.
---

Most agent products are reactive: you ask, they answer. `kube-agents` is designed to _also_ act on its own. Cron-scheduled jobs, defined in [`agents/chat/defaults/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/cron/jobs.json) and ticked by the Chat Agent profile's gateway ([how](/kube-agents/concepts/autonomous-watchdogs/#how-a-watchdog-fires)), fire the Platform Agent at governance SOPs on a rolling schedule. Findings become a standing report issue on your GitOps repo, proposed pull requests against it, and proactive Chat messages.

## The hands-free loop

```text
Cron tick  →  Governance SOP  →  Platform Agent investigates  →  fleet-audit / submit-suggestion
                                                              →  Minty mints GitHub token
                                                              →  Ledger issue or pull request opened
                                                              →  Proactive Chat alert
```

Every step is real code shipping in the repo. The SOPs live in [`agents/platform/governance/`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/governance); the [`submit-suggestion`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/skills/submit-suggestion) skill wraps the git flow; [Minty](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github) brokers short-lived tokens; the Chat integration is Google Chat by default with Slack as an opt-in.

## What runs on its own

Five fleet audits run enabled, each on its own schedule and each maintaining a single GitHub issue as its standing report:

- **Security & RBAC posture** (daily) — privileged and host-namespace containers, over-privileged RBAC bindings, namespaces with no `NetworkPolicy`, Workload Identity and metadata-concealment gaps.
- **Workload reliability** (daily) — missing resource requests, drain-blocking or absent PodDisruptionBudgets, unscalable Deployments, zone-pinned scheduling, missing probes.
- **Upgrade & patch readiness** (weekly) — control-plane and node versions against the cluster's release channel, version skew, `autoUpgrade`/`autoRepair` off, missing maintenance windows.
- **Fleet waste** (weekly) — over-provisioned requests, orphaned PersistentVolumes and disks, idle reserved IPs, near-empty node pools. Reported in resource units, not dollars: there is no billing export to price against.
- **Fleet consistency drift** (weekly) — clusters that diverge from the rest of the fleet on release channel, Workload Identity, Shielded Nodes, logging config and similar facets. The baseline is derived from the fleet itself, so it needs no blueprint to compare against.

Alongside them, `github-issue-resolver` polls the target repo every 30 minutes and triages open issues within tight guardrails — audit ledgers, which carry `agent:audit`, are excluded from its poll.

Each audit calls the [`fleet-audit`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/skills/fleet-audit) skill, whose helper owns every git and `gh` operation and renders every body from a validated findings file. The stream's ledger issue is rewritten in place each run; findings with a mergeable manifest are promoted into narrow remediation PRs that link back to it — automatically for critical ones, on request for the rest ([Declarative workflow](/kube-agents/concepts/declarative-workflow/#the-fleet-audit-skill) has the mechanism). A finding with no reproducible command is dropped, not softened; a clean run closes the ledger as completed and says nothing at all — unless it could not read the whole fleet, in which case it leaves the ledger open and reports the gaps rather than passing a partial look off as an all-clear, or it resolved findings on the way there, in which case it reports what closed rather than letting the good news be the only thing it swallows.

Those six are the whole roster; five further watchdogs shipped disabled for a time and have since been [retired](/kube-agents/concepts/autonomous-watchdogs/#the-retired-jobs). [Reference → Cron jobs](/kube-agents/reference/cron-jobs/) has the full table, generated from `jobs.json`, with exact cron expressions and prompts.

## Why this matters

The alternative for each of these is a person on a rotation, a static Terraform module, or an alert that pages someone in the middle of the night. `kube-agents` closes the loop:

- **Audit → issue → PR** — the agent doesn't just detect drift, it keeps a standing report of it and proposes the mergeable fixes as PRs you review.
- **Fleet-wide read, mutations through Git** — the Platform Agent reads the fleet via the GKE MCP server and is designed to route every change through a pull request. Which parts of that are _enforced_ rather than _intended_ is set out in [Security &amp; IAM](/kube-agents/reference/security-and-iam/#what-the-agent-can-and-cannot-do).
- **Recovery ladder before escalation** — `SOUL.md §4` caps recovery attempts at 5 iterations / ~10 minutes per blocker before asking a human.

The design goal: fleet issues stop rotting silently while the on-call queue is quiet.

## Safety rails

- **Declarative-only for infra changes.** `SOUL.md §1` forbids direct `kubectl apply` for GKE infrastructure. Everything routes through the GitOps write path — `submit-suggestion` for a one-off change, `fleet-audit` for a scheduled audit run, and nothing else (`SOUL.md §3.2`).
- **Destructive operations always ask.** Cluster deletion, tenant offboarding, broad IAM revocation — the persona explicitly gates these on human confirmation, no matter how many "just do it" phrases are in the user's message.
- **Bounded retries.** The recovery ladder in `SOUL.md §4` bounds each blocker at 5 attempts / 10 minutes before escalating.

## Where to go next

- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — how cron ticks become tool calls.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — the `submit-suggestion` + Minty PR path.
- [Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks the watchdogs execute.
