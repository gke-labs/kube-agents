---
title: Governance SOPs
description: Standard operating procedures that codify how the fleet is audited, standardized, and kept in policy.
sidebar:
  order: 5
---

Governance SOPs are the fleet-wide playbooks the Platform Agent executes on schedule (via cron watchdogs) or on request. They codify **how** the agent audits, remediates, and standardises clusters — separating the strategy from the tactics (skills).

The SOPs live in [`agents/platform/governance/`](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/governance).

## The ten audit SOPs

Ten SOPs back the enabled [fleet audits](/kube-agents/concepts/autonomous-watchdogs/). They share one shape: enumerate the fleet, run read-only checks, write a validated findings file, and hand it to the [`fleet-audit`](/kube-agents/skills/) skill, which owns the stream's ledger issue and any remediation pull requests it spawns. Each check in each SOP states how it is read (its exact command, or the collector script that runs it), its flag-when predicate, an explicit **do NOT flag** list, a severity, an impact sentence, a recommendation, and a remediation kind, so a finding is either reproducible or it is dropped.

<!-- BEGIN GENERATED: governance-audits -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

| SOP file | Watchdog | Cadence | Audit |
| -------- | -------- | ------- | ----- |
| `compliance_audit_sop.md` | `compliance-audit` | Daily 06:20 | Security & RBAC Posture Audit |
| `obtainability_audit_sop.md` | `obtainability-audit` | Daily 06:50 | Workload Reliability Audit |
| `gce_compute_fleet_sop.md` | `gce-compute-fleet-audit` | Daily 07:45 | GCE Compute Engine and MIG Fleet Audit |
| `gcp_networking_fabric_sop.md` | `gcp-networking-fabric-audit` | Daily 08:00 | GCP Networking Fabric & VPC IPAM Audit |
| `ai_security_audit_sop.md` | `ai-security-audit` | Daily 08:50 | AI Workload Security Audit |
| `stockout_prevention_sop.md` | `stockout-prevention` | Daily 09:20 | Fleet Stockout Prevention & Capacity Audit |
| `security_patch_orchestrator_sop.md` | `security-patch-orchestrator` | Weekly, Monday 07:20 | Upgrade & Patch Readiness Audit |
| `fleet_wide_cost_analysis_sop.md` | `fleet-wide-cost-analysis` | Weekly, Monday 07:50 | Fleet Waste Audit |
| `fleet_consistency_drift_sop.md` | `fleet-consistency-drift` | Weekly, Monday 08:20 | Fleet Consistency Drift Audit |
| `upgrade_retrospective_sop.md` | `upgrade-retrospective` | Weekly, Sunday 18:00 | Upgrade Retrospective |

<!-- prettier-ignore-end -->
<!-- END GENERATED: governance-audits -->

The exact cron expressions are on [Reference → Cron jobs](/kube-agents/reference/cron-jobs/). `upgrade-retrospective` also runs once at onboarding, through `oobe`. Each bullet below names what the SOP audits, how many checks it carries, and what they are.

### What each SOP checks

- **`compliance_audit_sop.md`**, security and RBAC posture. Seventeen checks: privileged and `SYS_ADMIN` containers, host namespace sharing, `hostPath` mounts, `cluster-admin` and wildcard grants on **bound** roles only, namespaces with no enforcing `NetworkPolicy`, `default` ServiceAccount token automount, Workload Identity disabled, node pools exposing the legacy GCE metadata endpoint, public control planes with no authorized networks, Pod Security `restricted` gaps, stalled Config Connector objects, Config Sync reconcilers that have stopped applying their repository, image references that name no specific bytes, tokens mounted for ServiceAccounts nothing has granted, LoadBalancer Services publishing a management port to the internet, and bindings whose subject is everyone.
- **`obtainability_audit_sop.md`**, workload reliability: which workloads break when a node pool is upgraded, and which cannot scale (the cron id predates the rename).

  Twenty-four checks, over workload **templates** (not live Pods) and the Services, CronJobs, autoscalers and volumes around them: missing requests and memory limits, multi-replica workloads with no PodDisruptionBudget, drain-blocking and overlapping PDBs, unscaled and unscalable Deployments, autoscalers allowed to reach one replica, hostname and single-zone pinning, missing and unachieved spreading, missing readiness and liveness probes and liveness probes that fire first, single-replica Service-backed Deployments, rollouts and update strategies that drop traffic, `preStop` hooks the grace period cuts short, Services that select no pod or name a port no container declares, CronJobs that never succeed or pile up, single-node volumes claimed from two nodes, and workloads on a ComputeClass cluster that omit the compute-class selector.

- **`security_patch_orchestrator_sop.md`**, upgrade and patch readiness. Eleven checks: control-plane and node-pool versions compared against every version `gcloud container get-server-config` still offers at the cluster's location, and against its release channel's default; node skew against GKE's two-minor ceiling; fleet-wide minor spread; clusters on no release channel; `autoUpgrade`/`autoRepair` off; missing maintenance windows; upgrade-blocking maintenance exclusions; deprecated node image variants; and absent upgrade notifications.

  For a cluster found behind, it also runs the `fleet-upgrade-verification` readiness grade and reports the upgrade as blocked when a drain-blocking PodDisruptionBudget would hold it (GKE holds each node's drain for up to an hour and then evicts the workload anyway), when node-pool skew would stop it from completing, or when one of the readiness report's per-entry rules blocks it: a load balancer draining longer than GKE waits per node, a Node whose instance is gone, a cluster held by an operation, or a withdrawn target patch.

- **`fleet_wide_cost_analysis_sop.md`**, fleet waste. Fourteen checks, run by a collector script the agent starts: over-requested, under-requested, unsized and idle workloads, judged against a week of Cloud Monitoring usage; orphaned PersistentVolumes, unconsumed PVCs, unattached Compute Engine disks, idle reserved IPs, orphaned load-balancer resources, under-allocated node pools, the scale-down blockers pinning them, terminal-pod accumulation, idle namespaces still holding billable objects, and Artifact Registry repositories with no cleanup policy.
- **`fleet_consistency_drift_sop.md`**, clusters diverging from what the majority of comparable clusters do. Twenty checks: nineteen comparative settings, plus one that runs without a cohort.

  For each setting it computes what the majority of _comparable_ clusters do and reports the outliers: release channel; Shielded Nodes, secure boot and integrity monitoring; network policy enforcement; private nodes, private endpoint and authorized networks; logging and monitoring component sets and managed Prometheus; Binary Authorization; cluster autoscaling and node auto-provisioning; intra-node visibility and dataplane provider; resource label key set; node image type; and database encryption (etcd CMEK). The twentieth check reports a cluster whose missing environment label keeps it out of every comparison. Workload Identity and the maintenance window are owned by other streams and are not compared here.

- **`stockout_prevention_sop.md`**, capacity obtainability and ComputeClass resilience. Twelve checks over ComputeClasses, GCP reservations, workload affinities, and regional capacity: missing fallback machine families and dimension diversity, missing On-Demand floors for Spot priority lists, large-shape (>32 vCPU) obtainability risks, excessive priority rules causing autoscaler starvation, mixed disk generations on PV-attached ComputeClasses, Hyperdisk generation compatibility, regional quota saturation, Spot preemption and obtainability risks, single-zone standard node pools, GCP reservation bypasses or unallocated capacity mismatches, autoscaler out-of-resources visibility indicators, and dangling or invalid ComputeClass configurations.
- **`ai_security_audit_sop.md`**, AI workload security: who can reach the models, what can rewrite them, and where their weights came from. Six checks over the workloads a three-pronged discriminator identifies as AI workloads (a container image naming a known inference runtime, a container requesting an `nvidia.com/gpu` / `google.com/tpu`, **or** a container declaring a named model-provider credential such as `OPENAI_API_KEY` or `HF_TOKEN`): inference endpoints on external LoadBalancers, model repositories trusted to execute their own code (`--trust-remote-code`), model weights mounted writable by the serving process, model artifacts pulled from an unpinned source, model-registry credentials in plaintext environment variables, and model-server images on floating tags.
- **`gcp_networking_fabric_sop.md`**, GCP networking fabric and VPC IPAM. Six checks: VPC subnet IP exhaustion risks, Cloud NAT port exhaustion, PSC routing deadlocks, MTU mismatches across VPC peerings, Cloud Armor policy anomalies, and management ports open to the whole internet.
- **`gce_compute_fleet_sop.md`**, the GCE Compute Engine and MIG fleet. Four checks over instances, Managed Instance Groups, sole-tenant node groups and snapshots, for startup-script failures in serial port output, MIGs that cannot converge on their target size, sole-tenant headroom exhaustion, and snapshots orphaned by a deleted source disk. Ops Agent guest telemetry is deliberately not audited by this stream.
- **`upgrade_retrospective_sop.md`**, what the last upgrades broke, classified against the upgrade failure catalogue. It looks back rather than ahead. The collector reviews every cluster that is new or was upgraded since the last run, records which `UPGRADE_MASTER` and `UPGRADE_NODES` operations ran and from which version to which, reads pods, nodes, events and budgets for what stayed down, stuck or restarting afterwards, classifies each symptom against the twenty entries of the upgrade failure catalogue with a confidence and its evidence, and lists the catalogue shapes a clean cluster carries today as risks for its next upgrade.

  Seven checks grade the result. An upgrade that broke a workload, an operation GKE reported failed, and a node broken after its pool's rebuild file as Errors. A tentative match, an unclassified symptom, a symptom that predates the upgrade, and a container whose Cloud Logging ingestion rose past the thresholds in the seven days after the upgrade file as Warnings. Each reviewed cluster also carries a gate (`clear`, `risk` or `pending`) that the rollout orchestration SOP reads before the rest of a family follows. A failure still present from an earlier review is re-filed under its original check, as a Warning that says since when it has persisted. The SOP copies the collector's grade and never moves it.

  The report is written to the shell volume with the latest at a fixed path, chat gets `finish`'s rendering (the headline with the fleet's counts and the most severe findings by object), and a quiet weekend posts nothing. The same SOP answers a generic question about the last upgrades from the saved report when it is newer than the last upgrade operation, and runs the collector first when it is not.

### Five stream bounds

1. **No CVE claims.** `security_patch_orchestrator_sop.md` forbids the words "vulnerable", "unpatched", and "CVE" in its findings: there is no vulnerability feed in this environment, so every finding is version currency or upgrade-policy hygiene, or, for the one check that asks how the upgrade would go, the held drain, the refused move or the rule's block above.
2. **Resource units rather than dollars.** `fleet_wide_cost_analysis_sop.md` reports in GiB, vCPU, node and object counts, never dollars. There is no billing export to price against, and the SOP treats a fabricated figure as worse than no figure. No remediation it emits may delete a PV, PVC, namespace, disk, snapshot, or address.
3. **A live-fleet drift baseline.** `fleet_consistency_drift_sop.md` derives its baseline from the live fleet and nowhere else. That is what makes it runnable where the retired blueprint-sync and standardization-validator SOPs were not: it needs no master blueprint, no CMDB, and no standards document.
4. **AI and compliance scopes do not overlap.** `ai_security_audit_sop.md` deliberately does **not** evaluate the model. Prompt-injection resistance, jailbreak susceptibility, output filtering, and training-data provenance are real AI risks that no `kubectl` read can decide, and the SOP treats an unfalsifiable finding in a public issue as worse than no finding. It also stays off the generic container-hardening surface: privileged containers, host namespaces, RBAC, NetworkPolicy, and Workload Identity on AI workloads all belong to `compliance_audit_sop.md`, which already audits them there, so one object never carries two verdicts in two ledgers.
5. **The Sunday upgrade review is manual-only.** In `upgrade_retrospective_sop.md`, Errors file at `major` and Warnings at `minor`, never `critical`, and every remediation is `manual` naming the manifest and the change, so the stream opens no pull request on its own; a fix is a user's "apply". The ledger issue is the one thing a GitHub repository adds: without one `start` and `finish` still run, with no forge, and `finish` stores the document in the report store in place of an issue; chat receives `finish`'s rendering either way.

## The retired SOPs

The SOPs behind the five watchdogs [retired from the roster](/kube-agents/concepts/autonomous-watchdogs/#the-retired-jobs) — blueprint sync, policy propagation, global capacity orchestration, standardization validation, and lifecycle deprecation — are no longer in the tree or the image; git history has them. As written, each depended on an input a stock install does not provide — a master blueprint, a `/opt/defaults/templates/` directory, a corporate patterns document — or duplicated an audit above, so reviving one means writing a new SOP against what an install actually has, not restoring the old file.

## SOPs that are not fleet audits

`inventory.md` is not a fleet audit. It is the first-boot environment discovery procedure behind [first-run onboarding](/kube-agents/concepts/chatops/#first-run-onboarding), which lists the fleet once and audits the clusters no Cluster Agent card covers; the onboarding gate files the per-cluster audits itself, up to a first-run cap. Its companions run as separate cards on the same one-shot path: `cluster_inventory_audit_sop.md` is what each Cluster Agent follows on the one cluster it is pinned to, returning structured `metadata` that the onboarding gate's hand-off script merges into the raw file, and `inventory_prioritize_sop.md` scores the merged findings against a fixed rubric, registers all of them in the findings queue, and renders the short `/opt/data/INVENTORY.md` that reaches the user from the top of that order.

`upgrade_readiness_brief_sop.md` is not a fleet audit either. It is the procedure behind `upgrade-readiness-weekly-brief`, the Saturday model turn that composes the week's upgrade readiness brief from the weekly review's summary and the findings queue: what needs a decision, what else the week found, and where the full report is. It registers nothing and opens no ledger; the queue the review writes to is the durable record, and the review itself is a `no_agent` script with no SOP.

`upgrade_rollout_orchestration_sop.md` is not a fleet audit either, and no cron job runs it. It is the procedure behind a request to plan or run an upgrade across cluster families, or with a RolloutSequence: the precheck of the fleet host project's existing sequences (the platform allows one per host project), one cluster per family first, a seven-day soak, the upgrade retrospective's scoped diff on that cluster with its gate (`clear`, `risk` or `pending`), then the rest; and the same order per cluster through the `gke-upgrades` skill's runbook steps when the sequence cannot be created. It registers nothing and writes nothing: the operator creates the sequence and runs every upgrade, and each cluster change is a pull request.

The k8s-event-watcher daily recap has no SOP here: it is a `no_agent` script, `eod_report_generator.py`, that renders the recap deterministically from the session ledger and prompts no model, so its description is a design note rather than agent material — [`docs/designs/eod-event-watcher-daily-report.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/eod-event-watcher-daily-report.md).

## How SOPs work

Each SOP is a Markdown file that opens with a `**Purpose:**` line and a `**Data sources:**` line naming exactly what the run may read, followed by a single `## Execution Checklist` broken into numbered steps (loose convention, not enforced). The audit SOPs additionally close with a `## Red Lines` section — the things the run must never do, stated as prohibitions rather than guidance.

The cron watchdog invokes the SOP by prompting the agent to read `governance/<sop>.md` **relative to its profile home** and execute it. `profile_scaffold.py` overlays the baked `/opt/platform-template/governance/` directory there at container start; there is no `/opt/defaults/governance/`, so an absolute path of that shape does not resolve.

## SOPs vs. skills

- A **skill** is a reusable capability (how to onboard an app, how to submit a PR, how to open and close an audit run).
- An **SOP** composes skills into a fleet-wide procedure with a policy for when to act.

The division of labour in the audit streams is deliberate: **the SOP decides what is true, the skill decides what happens to it.** The model reasons, runs read-only commands, and emits evidence; `fleet-audit`'s helper owns every forge call, through the version-control broker, and renders every body itself — the stream's ledger issue and the remediation PRs promoted from it. The SOPs forbid hand-writing any of those bodies or invoking git directly, which is what keeps the ten ledgers uniform and their run-to-run deltas computable.

The audit jobs preload the skill through their cron entry (`"skills": ["fleet-audit"]`). An SOP that needs no preloaded skill can omit the key or leave it empty — the run loads what it needs.

## Where to go next

- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — the schedules that invoke SOPs.
- [Skill catalog](/kube-agents/skills/) — the capabilities SOPs compose.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — how SOP-generated remediations become PRs.
