---
title: Proactive autonomy
description: The background watchdogs that make kube-agents more than a chatbot — audit, remediate, PR, alert.
---

Most agent products are reactive: you ask, they answer. `kube-agents` is designed to _also_ act on its own. Cron-scheduled jobs, defined in [`agents/platform/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/cron/jobs.json), fire the Platform Agent at governance SOPs on a rolling schedule. Findings become proposed pull requests against your GitOps repo and proactive Chat messages.

## The hands-free loop

```text
Cron tick  →  Governance SOP  →  Platform Agent investigates  →  submit-suggestion skill
                                                              →  Minty mints GitHub token
                                                              →  Pull request opened
                                                              →  Proactive Chat alert
```

Every step is real code shipping in the repo. The SOPs live in [`agents/platform/governance/`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/governance); the [`submit-suggestion`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/skills/submit-suggestion) skill wraps the git flow; [Minty](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github) brokers short-lived tokens; the Chat integration is Google Chat by default with Slack as an opt-in.

## What runs on its own

The shipping schedule at time of writing:

| Job                             | Schedule             | What it does                                                                                 |
| ------------------------------- | -------------------- | -------------------------------------------------------------------------------------------- |
| `blueprint-sync`                | Daily 09:00          | Audit clusters against master blueprints; reconcile drift declaratively.                     |
| `policy-propagation`            | Hourly               | Push updated security, network, and resource policies across clusters and namespaces.        |
| `global-capacity-orchestrator`  | Hourly               | Fleet-wide utilization audit; propose rebalancing when regions are hot or cold.              |
| `fleet-wide-cost-analysis`      | Daily 10:00          | Aggregate cost usage; surface saving opportunities and right-sizing candidates.              |
| `security-patch-orchestrator`   | Daily 11:00          | CVE scan; coordinate staggered emergency GKE upgrades.                                       |
| `obtainability-audit`           | Daily 12:00          | Find rigid capacity allocations; emit YAML patches to move workloads onto flexible capacity. |
| `compliance-audit`              | Weekly Sun 09:00     | Fleet-wide security/architectural policy compliance sweep.                                   |
| `standardization-validator`     | Weekly Sun 10:00     | Deep-diff of live cluster configs vs. corporate architectural patterns.                      |
| `lifecycle-deprecation-manager` | Monthly (1st, 09:00) | Track deprecated Kubernetes API versions ahead of the next GKE upgrade window.               |
| `github-issue-resolver`         | Every 30 min         | Poll the target repo; triage and (within tight guardrails) resolve open issues.              |

Schedules are literal `cron` expressions from `jobs.json`. See [Reference → Cron jobs](/kube-agents/reference/cron-jobs/) for the full table with cron expressions and prompts.

## Why this matters

The alternative for each of these is a person on a rotation, a static Terraform module, or an alert that pages someone in the middle of the night. `kube-agents` closes the loop:

- **Audit → PR** — the agent doesn't just detect drift, it proposes the fix as a PR you review.
- **Read-only elevated for the fleet, write-narrow for its own identity** — the Platform Agent holds fleet-wide read via the GKE MCP server but has to route mutations through Git.
- **Recovery ladder before escalation** — `SOUL.md §5` caps recovery attempts at 5 iterations / ~10 minutes per blocker before asking a human.

The design goal: fleet issues stop rotting silently while the on-call queue is quiet.

## Safety rails

- **Declarative-only for infra changes.** `SOUL.md §1` forbids direct `kubectl apply` for GKE infrastructure. Everything routes through the GitOps PR flow (`submit-suggestion`).
- **Destructive operations always ask.** Cluster deletion, tenant offboarding, broad IAM revocation — the persona explicitly gates these on human confirmation, no matter how many "just do it" phrases are in the user's message.
- **Bounded retries.** The recovery ladder in `SOUL.md §5` bounds each blocker at 5 attempts / 10 minutes before escalating.

## Where to go next

- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — how cron ticks become tool calls.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — the `submit-suggestion` + Minty PR path.
- [Governance SOPs](/kube-agents/concepts/governance-sops/) — the playbooks the watchdogs execute.
