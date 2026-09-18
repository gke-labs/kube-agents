# Fleet observability and anomaly detection checks for a large GKE fleet

**Status:** requirements. Nothing in this document is built beyond what the Scope table credits to
an existing audit.

## Scope

Scheduled audits already cover part of this ground. The table maps each area below to the job that
runs it today; the SOP behind that job is canonical for exactly what it checks and at what
threshold, and this document does not restate those checks; each job's cadence is in the
[cron jobs reference](../site/src/content/docs/reference/cron-jobs.md). What a builder picks up from
here is the third column.

| Area                                    | Already runs on `main`                                                                                                                                                                                                                                                                                                                                                                                       | New in this document                                                                                                                                                                                        |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Guardrails — tenant isolation           | `obtainability-audit` (`agents/platform/governance/obtainability_audit_sop.md`): missing requests and memory limits, missing PodDisruptionBudgets, missing spreading, on workload templates.                                                                                                                                                                                                                 | ResourceQuota and LimitRange coverage per namespace; eviction concentration by tenant.                                                                                                                      |
| Guardrails — security posture           | `compliance-audit` (`agents/platform/governance/compliance_audit_sop.md`): privileged containers, host namespaces, `hostPath`, cluster-admin and wildcard RBAC, namespaces without NetworkPolicy, default ServiceAccount automount, Workload Identity off, legacy metadata endpoint, public control plane, Pod Security gaps. `ai-security-audit` flags plaintext credentials in AI workloads' environments. | Registry allowlist and Binary Authorization attestation on running pods; plaintext secrets in the environment of every workload, not only AI ones.                                                          |
| Guardrails — cluster hardening          | `fleet-consistency-drift` (`agents/platform/governance/fleet_consistency_drift_sop.md`) compares shielded nodes, private nodes and endpoints, Binary Authorization, logging and monitoring components, and database encryption against the cohort majority — a fleet uniformly lacking one raises nothing.                                                                                                   | Backup plans on clusters holding stateful workloads; an absolute rule for a facet where the cohort comparison is silent.                                                                                    |
| Guardrails — drift within a family      | `fleet-consistency-drift`: a live-derived baseline per environment or project cohort across its facets (release channel, image type, dataplane, logging and monitoring, labels, and the rest). Node-pool version drift is `security-patch-orchestrator`'s; Workload Identity is `compliance-audit`'s by the drift SOP's own choice.                                                                          | Grouping by cluster family (label or naming convention) instead of environment; taint drift; add-on version drift.                                                                                          |
| Guardrails — capacity                   | `stockout-prevention` (`agents/platform/governance/stockout_prevention_sop.md`): ComputeClass fallbacks, Spot floors, quota exhaustion risk, single-zone pools, autoscaler out-of-resources indicators, autoscaler backoff loops from priority rules, reservation bypass and unallocated reservations.                                                                                                       | Nothing material.                                                                                                                                                                                           |
| Guardrails — expiry, telemetry coverage | Nothing.                                                                                                                                                                                                                                                                                                                                                                                                     | Everything in those sections.                                                                                                                                                                               |
| Usage                                   | Nothing scheduled beyond what capacity and cost already report (single-zone pools, out-of-resources events, gross over-request). Several usage checks need a metrics source the audits currently forbid, and the obtainability SOP records HPA-at-maximum, VPA gaps and OOMKill history as dropped for that reason.                                                                                          | Zonal skew with its cause, per-family utilization baselines, autoscaler and scaling health, workload health trends, control-plane load, accelerator utilization — and the metrics source they need.         |
| Costs                                   | `fleet-wide-cost-analysis` (`agents/platform/governance/fleet_wide_cost_analysis_sop.md`): over-request, orphaned volumes and disks, unconsumed claims, idle addresses, orphaned load balancers, under-allocated pools, scale-down blockers, terminal pods, idle namespaces. `gce-compute-fleet-audit` reports orphaned snapshots; `gcp-networking-fabric-audit` reports Cloud NAT saturation.               | Committed-use coverage, Spot-share drift and machine-generation cost; cost anomaly attribution; cross-zone and cross-region traffic; observability spend; storage tiering against measured use; chargeback. |

## Purpose

A platform team running GKE at scale — millions of cores, dozens of cluster families (fleets of
clusters built to one template), and multi-tenant clusters holding thousands of namespaces each —
finds that local problems hide in fleet-wide dashboards. With that many clusters nobody watches any
one family or region, so a problem confined to one of them (their example: zonal skew in a single
region) is discovered only when it becomes an outage.

This document lists the checks such an operator would realistically ask the Platform Agent to run on
a schedule, as candidate criteria for scheduled audits. The operator's own framing is "guardrails,
usage, and costs", and the three sections follow it. Each check is read-only: it inspects the
fleet and reports a finding and never changes a cluster.

## How it is delivered

These checks are delivered as one audit capability on the
[capability delivery vehicle](capability-delivery-vehicle.md), which gives them five properties,
none of them built here: it ships **pre-defined** with the agent; it runs **scheduled** as cron
jobs, one per section at its own cadence; it is **triggerable** from chat at any time, for the
whole fleet or for one family or region; they are **customizable**, in that the criteria below are held apart from the procedure so an
operator can revise them by describing the change to the agent and agreeing the edit; and they are
**self-learning**, in that the agent refines those criteria from what it learns in those
conversations, within limits the operator sets. This document lists only the checks; the delivery
requirements are a separate design.

Every check reports per cluster family and region — at dozens of multi-tenant clusters a single
fleet-wide report is unreadable — and every finding carries an owner (team label) and a
week-over-week delta. The families themselves are the natural baseline: a cluster is anomalous when
it diverges from its siblings, not from a fleet-wide average.

## Guardrails

Configuration that must hold everywhere, and the places it has quietly stopped holding. Where the
Scope table names a job, that job's checks are not repeated here.

- **Tenant isolation.** Namespaces with no ResourceQuota or LimitRange; node-pressure evictions
  concentrated on a few tenants.
- **Security posture.** Images pulled from registries outside the allowlist or without Binary
  Authorization attestation; secrets passed as plain environment variables in any workload.
- **Cluster hardening.** Backup plans missing on clusters holding stateful workloads; an absolute
  rule for a hardening facet the operator requires everywhere, where the cohort comparison would
  stay silent on a uniformly weak fleet.
- **Drift within a family.** A baseline derived per family (by label or naming convention) rather
  than per environment, so a family that is deliberately different is not reported as a fleet of
  drifted clusters; taint drift; add-on version drift.
- **Expiry and rotation.** Cluster CA rotation due; service account keys older than policy
  allows; certificates in Secrets near expiry; Workload Identity bindings pointing at deleted
  service accounts.
- **Telemetry coverage.** A cluster whose logs or metrics stopped arriving, or whose monitoring
  agent is crash-looping, is the "issue nobody noticed" case in its purest form; report ingestion
  gaps as findings, not as missing data.

## Usage

How the fleet is actually being used, and where a family, cluster, or zone has drifted from its
own norm. Most of these need a metrics source — Cloud Monitoring, managed Prometheus, or VPA
recommendations — that the scheduled audits currently do without; the source is part of the
requirement.

- **Zonal and topology balance.** The acute case in this document — the operator's motivating
  outage began as undetected skew — so it does not wait for a weekly report: it runs daily and is
  re-evaluated on the event trigger the
  [capability delivery vehicle](capability-delivery-vehicle.md) defines, when an autoscaler
  out-of-resources event arrives. Node and pod distribution
  per zone per cluster, with the cause named once skew crosses a threshold: a capacity stockout
  (autoscaler out-of-resources events), a single-zone node pool, a `topologySpreadConstraints`
  misconfiguration or a `whenUnsatisfiable: ScheduleAnyway` that let the scheduler give up, or
  zone-pinned PersistentVolumes dragging a StatefulSet into one zone. Each finding quantifies the
  blast radius — the share of the workload lost if the crowded zone fails, and the cross-zone
  traffic the imbalance pays for — alongside zonal capacity headroom (autoscaler maximum against
  current size per zone) and load-balancer backend distribution per zone.
- **Utilization per family.** Requested-versus-used CPU and memory per family, flagging the
  cluster that diverges from its siblings; bin-packing efficiency trend per family; per-namespace
  usage shifts week over week inside multi-tenant clusters.
- **Autoscaler and scheduling health.** Cluster Autoscaler scale-up/scale-down loops beyond the
  priority-rule case capacity already covers; nodes stuck `NotReady` or unregistered; a long tail
  of pending pods with requests nothing can satisfy; HPAs pinned at maximum for days; VPA
  recommendations far below current requests; event-driven scalers in error.
- **Workload health trends.** Restart-count growth per namespace; OOMKilled concentration; image
  pull latency outliers; PersistentVolumeClaims near full; Jobs whose run time is climbing.
- **Control-plane load.** API server latency and error-rate outliers per cluster; etcd
  object-count growth, which thousands of namespaces stress; admission webhook p99; request
  volume by user agent, to catch a controller in a hot loop.
- **Accelerator utilization.** GPU and TPU allocation versus measured utilization, and idle
  accelerator nodes held by pending or completed Jobs, where the fleet runs them.

## Costs

Where money leaks, and why a bill moved. The idle-resource, over-request, scale-down-blocker,
orphaned-snapshot and NAT-saturation checks are served today (see Scope).

- **Purchase-model drift.** Committed-use discount coverage per family and region; Spot share
  falling back to on-demand; machine-family generations that are dearer than an equivalent newer
  one.
- **Cost anomaly attribution.** A regional spend jump decomposed into new node pools, Spot to
  on-demand fallback, a raised HPA maximum, cross-region registry pulls, or a drop in
  committed-use coverage. Detection is a dashboard; the attribution across billing, GKE
  configuration, and workload changes is the manual work.
- **Network and egress.** Cross-zone traffic from unbalanced backends; cross-region image pulls;
  external load balancer counts growing faster than workloads.
- **Observability spend.** Cloud Logging and Monitoring ingestion volume per cluster and
  namespace, and the workloads or metric cardinality driving it; at this scale telemetry is a
  line item, and the agent's own footprint belongs in the same report.
- **Storage tiering.** Disk types and sizes against measured IOPS and fill level; backups past
  retention; regional disks where zonal would do.
- **Chargeback.** Namespace-level cost allocation drift inside multi-tenant clusters, and tenants
  whose allocated share no longer matches their quota or label.
