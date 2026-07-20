---
title: Governance SOPs
description: Standard operating procedures that codify how the fleet is audited, standardized, and kept in policy.
sidebar:
  order: 4
---

Governance SOPs are the fleet-wide playbooks the Platform Agent executes on schedule (via cron watchdogs) or on request. They codify **how** the agent audits, remediates, and standardises clusters — separating the strategy from the tactics (skills).

The SOPs live in [`agents/platform/governance/`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/governance).

## The shipping SOPs

### `blueprint_sync_sop.md`

Daily audit of clusters against master blueprint definitions. Flags drift (a cluster running with a different node pool spec than its blueprint) and — via the declarative workflow — proposes reconciliation.

Invoked by the [`blueprint-sync`](/kube-agents/concepts/autonomous-watchdogs/) watchdog at 09:00 daily.

### `compliance_audit_sop.md`

Fleet-wide security and architectural policy sweep. Verifies namespaces have `NetworkPolicy`, `ResourceQuota`, and workload-identity bindings; checks pods for privileged escalation, disallowed capabilities, and unpatched CVEs.

Invoked by the [`compliance-audit`](/kube-agents/concepts/autonomous-watchdogs/) watchdog weekly on Sunday 09:00.

### `fleet_wide_cost_analysis_sop.md`

Aggregate cost data across clusters (via BigQuery cost export). Surface right-sizing candidates, idle allocations, and expensive Spot fallback events. The `gke-cost-analysis` skill provides the query surface.

Invoked by the [`fleet-wide-cost-analysis`](/kube-agents/concepts/autonomous-watchdogs/) watchdog daily at 10:00.

### `global_capacity_orchestrator_sop.md`

Hourly cross-cluster utilization audit. Identifies hot regions and cold regions; proposes rebalancing (moving workloads, adjusting HPA, changing compute classes) via the declarative workflow.

Invoked by the [`global-capacity-orchestrator`](/kube-agents/concepts/autonomous-watchdogs/) watchdog hourly.

### `lifecycle_deprecation_manager_sop.md`

Monthly scan for deprecated Kubernetes API versions in live manifests, ahead of the next GKE upgrade window. Emits notifications listing workloads whose manifests will break at the target API version.

Invoked by the [`lifecycle-deprecation-manager`](/kube-agents/concepts/autonomous-watchdogs/) watchdog monthly on the 1st at 09:00.

### `obtainability_audit_sop.md`

Daily audit for rigid capacity allocations — pods that pin to a specific machine type when a `ComputeClass` would give them flexibility. Auto-generates YAML patches to migrate workloads onto flexible capacity pools.

Invoked by the [`obtainability-audit`](/kube-agents/concepts/autonomous-watchdogs/) watchdog daily at 12:00.

### `policy_propagation_sop.md`

Hourly push of updated platform default policies (`NetworkPolicy`, `PodSecurityStandards`, `ResourceQuota`) across clusters and namespaces. Reconciles any drift where a namespace lost a required default.

Invoked by the [`policy-propagation`](/kube-agents/concepts/autonomous-watchdogs/) watchdog hourly.

### `security_patch_orchestrator_sop.md`

Daily CVE scan against node OS and workload images. Coordinates staggered emergency GKE upgrade rollouts (canary, then rolling) when critical CVEs land. Escalates for human confirmation before triggering any upgrade.

Invoked by the [`security-patch-orchestrator`](/kube-agents/concepts/autonomous-watchdogs/) watchdog daily at 11:00.

### `standardization_validator_sop.md`

Weekly deep-diff of live cluster configuration against corporate architectural patterns. Beyond compliance (which is pass/fail on discrete rules), this looks at higher-order shape: are workloads structured consistently, are namespaces named per convention, are IAM patterns uniform.

Invoked by the [`standardization-validator`](/kube-agents/concepts/autonomous-watchdogs/) watchdog weekly on Sunday 10:00.

## How SOPs work

Each SOP is a Markdown file with a small number of sections (loose convention, not enforced):

1. **Scope** — which clusters, namespaces, or resource kinds the SOP covers.
2. **Procedure** — the exact diagnostic queries the agent should run and what to look for.
3. **Remediation policy** — what to do with findings (open a PR, post to Chat, both).

The cron watchdog invokes the SOP by prompting the agent to "read `/opt/defaults/governance/<sop>.md` and execute". The SOP is loaded once, executed, and the run terminates when the SOP's completion criteria are met.

## SOPs vs. skills

- A **skill** is a reusable capability (how to onboard an app, how to submit a PR, how to query costs).
- An **SOP** composes skills into a fleet-wide procedure with a policy for when to act.

`fleet_wide_cost_analysis_sop.md` uses the `gke-cost-analysis` skill; `security_patch_orchestrator_sop.md` uses `gke-cluster-lifecycle`; `blueprint_sync_sop.md` uses `submit-suggestion` for its remediation step.

## Where to go next

- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — the schedules that invoke SOPs.
- [Skill catalog](/kube-agents/skills/) — the capabilities SOPs compose.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — how SOP-generated remediations become PRs.
