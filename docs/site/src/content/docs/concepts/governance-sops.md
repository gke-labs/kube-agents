---
title: Governance SOPs
description: Standard operating procedures that codify how the fleet is audited, standardized, and kept in policy.
sidebar:
  order: 5
---

Governance SOPs are the fleet-wide playbooks the Platform Agent executes on schedule (via cron watchdogs) or on request. They codify **how** the agent audits, remediates, and standardises clusters — separating the strategy from the tactics (skills).

The SOPs live in [`agents/platform/governance/`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/governance).

## The five audit SOPs

Five SOPs back the enabled [fleet audits](/kube-agents/concepts/autonomous-watchdogs/). They share one shape: enumerate the fleet, run read-only checks, write a validated findings file, and hand it to the [`fleet-audit`](/kube-agents/skills/) skill, which owns the stream's ledger issue and any remediation pull requests it spawns. Each check in each SOP states its exact command, its flag-when predicate, an explicit **do NOT flag** list, a severity, an impact sentence, a recommendation, and a remediation kind — so a finding is either reproducible or it is dropped.

### `compliance_audit_sop.md`

Security & RBAC posture, daily. Eleven checks: privileged and `SYS_ADMIN` containers, host namespace sharing, `hostPath` mounts, `cluster-admin` and wildcard grants on **bound** roles only, namespaces with no enforcing `NetworkPolicy`, `default` ServiceAccount token automount, Workload Identity disabled, node pools exposing the legacy GCE metadata endpoint, public control planes with no authorized networks, and Pod Security `restricted` gaps.

Invoked by the `compliance-audit` watchdog.

### `obtainability_audit_sop.md`

Workload reliability, daily — the question "which workloads break when I upgrade a node pool, and which ones cannot scale?" Ten checks over workload **templates** (not live Pods): missing requests and memory limits, multi-replica workloads with no PodDisruptionBudget, drain-blocking PDBs, unscaled and unscalable Deployments, hostname and single-zone pinning, missing spreading, missing probes, and single-replica Service-backed Deployments.

Invoked by the `obtainability-audit` watchdog. The cron id predates the rename.

### `security_patch_orchestrator_sop.md`

Upgrade & patch readiness, weekly. Control-plane and node-pool versions compared against `gcloud container get-server-config` for each cluster's release channel, node skew against GKE's two-minor ceiling, fleet-wide minor spread, clusters on no release channel, `autoUpgrade`/`autoRepair` off, missing maintenance windows, upgrade-blocking maintenance exclusions, deprecated node image variants, and absent upgrade notifications.

The SOP forbids the words "vulnerable", "unpatched", and "CVE" in its findings: there is no vulnerability feed in this environment, so every finding is version currency or upgrade-policy hygiene. Invoked by the `security-patch-orchestrator` watchdog.

### `fleet_wide_cost_analysis_sop.md`

Fleet waste, weekly. Over-requested workloads (three `kubectl top` samples that must all agree), orphaned PersistentVolumes, unconsumed PVCs, unattached Compute Engine disks, idle reserved IPs, orphaned load-balancer resources, under-allocated node pools, the scale-down blockers pinning them, terminal-pod accumulation, and idle namespaces still holding billable objects.

Findings are reported in **resource units — GiB, vCPU, node and object counts — never dollars.** There is no billing export to price against, and the SOP treats a fabricated figure as worse than no figure. No remediation it emits may delete a PV, PVC, namespace, disk, snapshot, or address. Invoked by the `fleet-wide-cost-analysis` watchdog.

### `fleet_consistency_drift_sop.md`

Fleet consistency drift, weekly. For each configuration facet — release channel, Workload Identity, Shielded Nodes, logging and monitoring config, network policy, node auto-provisioning, Binary Authorization, required labels — it computes what the majority of _comparable_ clusters do and reports the outliers.

The baseline is derived from the live fleet and nowhere else. That is what makes this one runnable where `blueprint-sync` and `standardization-validator` are not: it needs no master blueprint, no CMDB, and no standards document. Invoked by the `fleet-consistency-drift` watchdog.

## The disabled SOPs

`blueprint_sync_sop.md`, `policy_propagation_sop.md`, `global_capacity_orchestrator_sop.md`, `standardization_validator_sop.md`, and `lifecycle_deprecation_manager_sop.md` are retained on disk, but their cron jobs ship with `enabled: false`. As written, each depends on an input a stock install does not provide — a master blueprint, a `/opt/defaults/templates/` directory, a corporate patterns document — or duplicates an audit above. [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/#the-disabled-jobs) has the reason for each. Rewrite the SOP before re-enabling its job, or the run will find nothing.

`inventory.md` is not a fleet audit. It is the first-boot environment discovery procedure behind [first-run onboarding](/kube-agents/concepts/chatops/#first-run-onboarding), which builds `/opt/data/INVENTORY.md` once and then returns `[SILENT]` forever after.

## How SOPs work

Each SOP is a Markdown file that opens with a `**Purpose:**` line and a `**Data sources:**` line naming exactly what the run may read, followed by a single `## Execution Checklist` broken into numbered steps (loose convention, not enforced). The five audit SOPs additionally close with a `## Red Lines` section — the things the run must never do, stated as prohibitions rather than guidance.

The cron watchdog invokes the SOP by prompting the agent to read `governance/<sop>.md` **relative to its profile home** and execute it. `profile_scaffold.py` overlays the baked `/opt/platform-template/governance/` directory there at container start; there is no `/opt/defaults/governance/`, so an absolute path of that shape does not resolve.

## SOPs vs. skills

- A **skill** is a reusable capability (how to onboard an app, how to submit a PR, how to open and close an audit run).
- An **SOP** composes skills into a fleet-wide procedure with a policy for when to act.

The division of labour in the five audits is deliberate: **the SOP decides what is true, the skill decides what happens to it.** The model reasons, runs read-only commands, and emits evidence; `fleet-audit`'s helper owns every `git` and `gh` call and renders every body itself — the stream's ledger issue and the remediation PRs promoted from it. The SOPs forbid hand-writing any of those bodies or invoking git directly, which is what keeps the five ledgers uniform and their run-to-run deltas computable.

The five audit jobs preload the skill through their cron entry (`"skills": ["fleet-audit"]`). The disabled governance jobs still ship with `"skills": []`.

## Where to go next

- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — the schedules that invoke SOPs.
- [Skill catalog](/kube-agents/skills/) — the capabilities SOPs compose.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — how SOP-generated remediations become PRs.
