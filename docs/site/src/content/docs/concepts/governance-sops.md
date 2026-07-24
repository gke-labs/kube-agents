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

Fleet-wide security and architectural policy sweep. Verifies every namespace (outside `kube-system` and `cnrm-system`) has an active `NetworkPolicy`, flags any container running `privileged: true`, and audits RBAC for over-privileged bindings (non-system service accounts granted `cluster-admin` or wildcard `*` grants).

Invoked by the [`compliance-audit`](/kube-agents/concepts/autonomous-watchdogs/) watchdog weekly on Sunday 09:00.

### `fleet_wide_cost_analysis_sop.md`

Aggregates node instance types, pricing models (Spot vs. on-demand), and resource requests across the fleet. Surfaces right-sizing candidates, idle capacity (nodes whose aggregate Pod requests are below 40% of node capacity), and stateless workloads that could move to Spot VMs, and publishes a daily cost-delta report.

Invoked by the [`fleet-wide-cost-analysis`](/kube-agents/concepts/autonomous-watchdogs/) watchdog daily at 10:00.

### `global_capacity_orchestrator_sop.md`

Hourly cross-cluster utilization audit. Flags clusters above 85% aggregate CPU/memory utilization (exhaustion risk) and below 30% (waste), then recommends `ComputeClass` adjustments and cross-region workload shifts, delivered as a fleet resource-map report.

Invoked by the [`global-capacity-orchestrator`](/kube-agents/concepts/autonomous-watchdogs/) watchdog hourly.

### `lifecycle_deprecation_manager_sop.md`

Monthly scan for deprecated Kubernetes API versions in live manifests, ahead of the next GKE upgrade window. Emits notifications listing workloads whose manifests will break at the target API version.

Invoked by the [`lifecycle-deprecation-manager`](/kube-agents/concepts/autonomous-watchdogs/) watchdog monthly on the 1st at 09:00.

### `obtainability_audit_sop.md`

Daily audit for rigid capacity allocations — workloads that pin to a specific node hostname or zone via `nodeSelector`, and deployments with more than three replicas that lack a `HorizontalPodAutoscaler`. Auto-generates YAML patches that swap static selectors for `ComputeClass` tolerations and add the missing HPAs.

Invoked by the [`obtainability-audit`](/kube-agents/concepts/autonomous-watchdogs/) watchdog daily at 12:00.

### `policy_propagation_sop.md`

Hourly propagation of platform default policies. Reads the baseline `NetworkPolicy` and `ResourceQuota` templates from `/opt/defaults/templates/` and verifies they are active across clusters and namespaces, reconciling any drift where a namespace lost a required default.

Invoked by the [`policy-propagation`](/kube-agents/concepts/autonomous-watchdogs/) watchdog hourly.

### `security_patch_orchestrator_sop.md`

Daily audit of GKE control plane and node versions against the latest available security patches (queried via `gcloud container get-server-config`). When a critical upgrade is required it proposes a staggered rollout — dev/staging cluster first, then production once the dev change is merged and healthy — as GitHub PRs via `submit-suggestion`, never applying upgrades directly.

Invoked by the [`security-patch-orchestrator`](/kube-agents/concepts/autonomous-watchdogs/) watchdog daily at 11:00.

### `standardization_validator_sop.md`

Weekly deep-diff of live cluster configuration against corporate architectural patterns. Verifies that deployments and services carry the standard metadata labels (`app.kubernetes.io/name`, `owner`, `environment`), and flags any dev-namespace Service exposing a public external LoadBalancer IP without the `platform.harness.io/public-exposition-approved: "true"` annotation.

Invoked by the [`standardization-validator`](/kube-agents/concepts/autonomous-watchdogs/) watchdog weekly on Sunday 10:00.

## How SOPs work

Each SOP is a Markdown file that opens with a `**Purpose:**` line and then a single `## Execution Checklist` broken into numbered steps (loose convention, not enforced). The steps typically cover:

1. **Target selection** — which clusters, namespaces, or resource kinds the SOP audits (usually "retrieve the active GKE clusters list directly using native GKE monitoring and read-only tools").
2. **Audit rules** — the exact diagnostic queries the agent runs and the policy thresholds that constitute a violation.
3. **Remediation / reporting** — what to do with findings: file a PR via `submit-suggestion`, or emit a report or Chat alert.

The cron watchdog invokes the SOP by prompting the agent to "read `/opt/defaults/governance/<sop>.md` and execute". The SOP is loaded once, executed, and the run terminates when the SOP's completion criteria are met.

## SOPs vs. skills

- A **skill** is a reusable capability (how to onboard an app, how to submit a PR, how to query costs).
- An **SOP** composes skills into a fleet-wide procedure with a policy for when to act.

`blueprint_sync_sop.md` and `security_patch_orchestrator_sop.md` both call `submit-suggestion` to turn their findings into GitHub PRs. The remaining SOPs deliver their findings as reports or Chat alerts rather than invoking a named skill, and none preload skills via the cron job entry (every governance job ships with `"skills": []`).

## Where to go next

- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — the schedules that invoke SOPs.
- [Skill catalog](/kube-agents/skills/) — the capabilities SOPs compose.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — how SOP-generated remediations become PRs.
