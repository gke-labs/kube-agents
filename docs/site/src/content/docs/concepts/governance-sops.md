---
title: Governance SOPs
description: Standard operating procedures that codify how the fleet is audited, standardized, and kept in policy.
sidebar:
  order: 5
---

Governance SOPs are the fleet-wide playbooks the Platform Agent executes on schedule (via cron watchdogs) or on request. They codify **how** the agent audits, remediates, and standardises clusters — separating the strategy from the tactics (skills).

The SOPs live in [`agents/platform/governance/`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/governance).

## The seven audit SOPs

Seven SOPs back the enabled [fleet audits](/kube-agents/concepts/autonomous-watchdogs/). They share one shape: enumerate the fleet, run read-only checks, write a validated findings file, and hand it to the [`fleet-audit`](/kube-agents/skills/) skill, which owns the stream's ledger issue and any remediation pull requests it spawns. Each check in each SOP states its exact command, its flag-when predicate, an explicit **do NOT flag** list, a severity, an impact sentence, a recommendation, and a remediation kind — so a finding is either reproducible or it is dropped.

### `compliance_audit_sop.md`

Security & RBAC posture, daily. Eleven checks: privileged and `SYS_ADMIN` containers, host namespace sharing, `hostPath` mounts, `cluster-admin` and wildcard grants on **bound** roles only, namespaces with no enforcing `NetworkPolicy`, `default` ServiceAccount token automount, Workload Identity disabled, node pools exposing the legacy GCE metadata endpoint, public control planes with no authorized networks, and Pod Security `restricted` gaps.

Invoked by the `compliance-audit` watchdog.

### `obtainability_audit_sop.md`

Workload reliability, daily — the question "which workloads break when I upgrade a node pool, and which ones cannot scale?" Eleven checks over workload **templates** (not live Pods): missing requests and memory limits, multi-replica workloads with no PodDisruptionBudget, drain-blocking PDBs, unscaled and unscalable Deployments, hostname and single-zone pinning, missing spreading, missing readiness and liveness probes, and single-replica Service-backed Deployments.

Invoked by the `obtainability-audit` watchdog. The cron id predates the rename.

### `security_patch_orchestrator_sop.md`

Upgrade & patch readiness, weekly. Control-plane and node-pool versions compared against `gcloud container get-server-config` for each cluster's release channel, node skew against GKE's two-minor ceiling, fleet-wide minor spread, clusters on no release channel, `autoUpgrade`/`autoRepair` off, missing maintenance windows, upgrade-blocking maintenance exclusions, deprecated node image variants, and absent upgrade notifications.

The SOP forbids the words "vulnerable", "unpatched", and "CVE" in its findings: there is no vulnerability feed in this environment, so every finding is version currency or upgrade-policy hygiene. Invoked by the `security-patch-orchestrator` watchdog.

### `fleet_wide_cost_analysis_sop.md`

Fleet waste, weekly. Over-requested workloads (three `kubectl top` samples that must all agree), orphaned PersistentVolumes, unconsumed PVCs, unattached Compute Engine disks, idle reserved IPs, orphaned load-balancer resources, under-allocated node pools, the scale-down blockers pinning them, terminal-pod accumulation, and idle namespaces still holding billable objects.

Findings are reported in **resource units — GiB, vCPU, node and object counts — never dollars.** There is no billing export to price against, and the SOP treats a fabricated figure as worse than no figure. No remediation it emits may delete a PV, PVC, namespace, disk, snapshot, or address. Invoked by the `fleet-wide-cost-analysis` watchdog.

### `fleet_consistency_drift_sop.md`

Fleet consistency drift, weekly. For each configuration facet — release channel, Workload Identity, Shielded Nodes, logging and monitoring config, network policy, node auto-provisioning, Binary Authorization, required labels — it computes what the majority of _comparable_ clusters do and reports the outliers.

The baseline is derived from the live fleet and nowhere else. That is what makes this one runnable where the retired `blueprint_sync_sop.md` and `standardization_validator_sop.md` are not: it needs no master blueprint, no CMDB, and no standards document. Invoked by the `fleet-consistency-drift` watchdog.

### `stockout_prevention_sop.md`

Capacity obtainability & ComputeClass resilience, daily. Twelve checks over ComputeClasses, GCP reservations, workload affinities, and regional capacity: missing fallback machine families and dimension diversity, missing On-Demand floors for Spot priority lists, large-shape (>32 vCPU) obtainability risks, excessive priority rules causing autoscaler starvation, mixed disk generations on PV-attached ComputeClasses, Hyperdisk generation compatibility, regional quota saturation, Spot preemption and obtainability risks, single-zone standard node pools, GCP reservation bypasses or unallocated capacity mismatches, autoscaler out-of-resources visibility indicators, and dangling or invalid ComputeClass configurations.

Invoked by the `stockout-prevention` watchdog.

### `ai_security_audit_sop.md`

AI workload security, daily — "who can reach my models, what can rewrite them, and where did their weights come from?" Six checks over the workloads a two-pronged discriminator identifies as AI workloads (a container image naming a known inference runtime, **or** a container requesting an `nvidia.com/gpu` / `google.com/tpu`): inference endpoints on external LoadBalancers, model repositories trusted to execute their own code (`--trust-remote-code`), model weights mounted writable by the serving process, model artifacts pulled from an unpinned source, model-registry credentials in plaintext environment variables, and model-server images on floating tags.

It deliberately does **not** evaluate the model. Prompt-injection resistance, jailbreak susceptibility, output filtering, and training-data provenance are real AI risks that no `kubectl` read can decide, and the SOP treats an unfalsifiable finding in a public issue as worse than no finding. It also stays off the generic container-hardening surface — privileged containers, host namespaces, RBAC, NetworkPolicy, and Workload Identity on AI workloads all belong to `compliance_audit_sop.md`, which already audits them there, so one object never carries two verdicts in two ledgers. Invoked by the `ai-security-audit` watchdog.

## The unscheduled SOPs

`blueprint_sync_sop.md`, `policy_propagation_sop.md`, `global_capacity_orchestrator_sop.md`, `standardization_validator_sop.md`, and `lifecycle_deprecation_manager_sop.md` are retained on disk, but no cron job invokes them — their watchdogs were disabled and then [retired from the roster](/kube-agents/concepts/autonomous-watchdogs/#the-retired-jobs). As written, each depends on an input a stock install does not provide — a master blueprint, a `/opt/defaults/templates/` directory, a corporate patterns document — or duplicates an audit above. Rewrite the SOP before scheduling a job against it, or the run will find nothing.

`inventory.md` is not a fleet audit. It is the first-boot environment discovery procedure behind [first-run onboarding](/kube-agents/concepts/chatops/#first-run-onboarding), which builds `/opt/data/INVENTORY.raw.md` once and then returns `[SILENT]` forever after. Its companions run as separate cards on the same one-shot path: `cluster_inventory_audit_sop.md` is what each Cluster Agent follows on the one cluster it is pinned to, returning structured `metadata` the waiting sweep card merges, and `inventory_prioritize_sop.md` ranks the merged findings into the short `/opt/data/INVENTORY.md` that reaches the user.

`eod_event_watcher_daily_report_sop.md` is not a fleet audit either, and it is the one SOP here that no agent ever reads. It documents `eod_report_generator.py`, a `no_agent` script that renders the k8s-event-watcher recap deterministically from the session ledger; the file exists so the behaviour has an owner next to the SOPs it sits beside, not to instruct a model.

## How SOPs work

Each SOP is a Markdown file that opens with a `**Purpose:**` line and a `**Data sources:**` line naming exactly what the run may read, followed by a single `## Execution Checklist` broken into numbered steps (loose convention, not enforced). The seven audit SOPs additionally close with a `## Red Lines` section — the things the run must never do, stated as prohibitions rather than guidance.

The cron watchdog invokes the SOP by prompting the agent to read `governance/<sop>.md` **relative to its profile home** and execute it. `profile_scaffold.py` overlays the baked `/opt/platform-template/governance/` directory there at container start; there is no `/opt/defaults/governance/`, so an absolute path of that shape does not resolve.

## SOPs vs. skills

- A **skill** is a reusable capability (how to onboard an app, how to submit a PR, how to open and close an audit run).
- An **SOP** composes skills into a fleet-wide procedure with a policy for when to act.

The division of labour in the seven audits is deliberate: **the SOP decides what is true, the skill decides what happens to it.** The model reasons, runs read-only commands, and emits evidence; `fleet-audit`'s helper owns every `git` and `gh` call and renders every body itself — the stream's ledger issue and the remediation PRs promoted from it. The SOPs forbid hand-writing any of those bodies or invoking git directly, which is what keeps the seven ledgers uniform and their run-to-run deltas computable.

The seven audit jobs preload the skill through their cron entry (`"skills": ["fleet-audit"]`). An SOP that needs no preloaded skill can omit the key or leave it empty — the run loads what it needs.

## Where to go next

- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — the schedules that invoke SOPs.
- [Skill catalog](/kube-agents/skills/) — the capabilities SOPs compose.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — how SOP-generated remediations become PRs.
