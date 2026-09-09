# Fleet observability and anomaly detection checks for a large GKE fleet

## Purpose

A platform team running GKE at scale — millions of cores, dozens of cluster families (fleets of
clusters built to one template), and multi-tenant clusters holding thousands of namespaces each —
finds that local problems hide in fleet-wide dashboards. With that many clusters nobody watches any
one family or region, so a problem confined to one of them (their example: zonal skew in a single
region) is discovered only when it becomes an outage.

This document lists the checks such an operator would realistically ask the Kube-Agent to run on a
schedule, as candidate criteria for scheduled audits. The operator's own framing is "guardrails,
usage, and costs", and the three sections follow it. Each check is read-only: it inspects the
fleet and reports a finding and never changes a cluster.

## How it is delivered

The checks ship as one skill with pre-defined cron jobs — one per section, at its own cadence —
so they run on a schedule without being asked; the same skill can be triggered from chat at any
time, for the whole fleet or for one family or region; and the criteria below — thresholds,
family grouping, exclusions, report shape — are held apart from the procedure so the operator can
revise them after deployment by describing the change to the agent, agreeing the revision, and
having the agent write it for every later run. The mechanics, and what a customization does and
does not survive, are in [`customizable-scheduled-skills.md`](customizable-scheduled-skills.md).

## Guardrails

Configuration that must hold everywhere, and the places it has quietly stopped holding.

- **Tenant isolation.** Namespaces with no ResourceQuota or LimitRange; pods without limits on
  shared node pools; workloads missing `topologySpreadConstraints` or a PodDisruptionBudget;
  node-pressure evictions concentrated on a few tenants.
- **Security posture.** NetworkPolicy default-deny coverage per namespace; Pod Security Admission
  level per namespace; RBAC wildcards and cluster-admin bindings; privileged containers,
  `hostPath` and `hostNetwork` use; secrets passed as plain environment variables; images pulled
  from registries outside the allowlist or without Binary Authorization attestation.
- **Cluster hardening.** Public control-plane endpoints on clusters meant to be private; legacy
  metadata endpoints; Workload Identity not enabled; shielded nodes off; audit logging or
  Cloud Logging disabled; backup plans missing on clusters holding stateful workloads.
- **Configuration drift within a family.** A baseline derived per family (by label or naming
  convention) rather than fleet-wide, so a family that is deliberately different is not reported
  as a fleet of drifted clusters. Node pool version, image, and taint drift; feature-flag drift
  (Dataplane V2, Workload Identity, logging and monitoring configuration); add-on version drift.
- **Capacity guardrails.** ComputeClasses without fallback machine families; Spot-only classes
  with no on-demand floor; single-zone node pools on clusters meant to be regional; regional
  quota within a set margin of exhaustion.
- **Expiry and rotation.** Cluster CA rotation due; service account keys older than policy
  allows; certificates in Secrets near expiry; Workload Identity bindings pointing at deleted
  service accounts.
- **Telemetry coverage.** A cluster whose logs or metrics stopped arriving, or whose monitoring
  agent is crash-looping, is the "issue nobody noticed" case in its purest form; report ingestion
  gaps as findings, not as missing data.

## Usage

How the fleet is actually being used, and where a family, cluster, or zone has drifted from its
own norm.

- **Zonal and topology balance.** Node and pod distribution per zone per cluster, with the reason
  once skew crosses a threshold: a stockout (autoscaler out-of-resources events), a single-zone
  node pool, a misconfigured `topologySpreadConstraints`, or zone-pinned PersistentVolumes
  dragging pods into one zone. Zonal capacity headroom (autoscaler maximum against current size
  per zone) and load-balancer backend distribution per zone, for single-zone blast radius.
- **Utilization per family.** Requested-versus-used CPU and memory per family, flagging the
  cluster that diverges from its siblings; bin-packing efficiency trend per family; per-namespace
  usage shifts week over week inside multi-tenant clusters.
- **Autoscaler and scheduling health.** Cluster Autoscaler thrash (scale-up/scale-down loops);
  nodes stuck `NotReady` or unregistered; a long tail of pending pods with requests nothing can
  satisfy; HPAs pinned at maximum for days; VPA recommendations far below current requests;
  event-driven scalers in error.
- **Workload health trends.** Restart-count growth per namespace; OOMKilled concentration; image
  pull latency outliers; PersistentVolumeClaims near full; Jobs whose run time is climbing.
- **Control-plane load.** API server latency and error-rate outliers per cluster; etcd
  object-count growth, which thousands of namespaces stress; admission webhook p99; request
  volume by user agent, to catch a controller in a hot loop.
- **Accelerator utilization.** GPU and TPU allocation versus measured utilization, and idle
  accelerator nodes held by pending or completed Jobs, where the fleet runs them.

## Costs

Where money leaks, and why a bill moved.

- **Idle and orphaned resources.** Unattached disks, orphaned PersistentVolumes, bound PVCs with
  no consuming pod, reserved addresses not in use, load balancers with no backends, TTL-less
  completed Jobs, terminal pods, namespaces holding billable objects but no running workload.
- **Scale-down blockers.** Nodes kept alive by pods with `safe-to-evict: false`, local storage,
  or unsatisfiable PDBs; autoscaler floors set above what the family's siblings need.
- **Purchase-model drift.** Committed-use discount coverage per family and region; Spot share
  falling back to on-demand; reservations unused or bypassed; machine-family generations that
  are dearer than an equivalent newer one.
- **Cost anomaly attribution.** A regional spend jump decomposed into new node pools, Spot to
  on-demand fallback, a raised HPA maximum, cross-region registry pulls, or a drop in
  committed-use coverage. Detection is a dashboard; the attribution across billing, GKE
  configuration, and workload changes is the manual work.
- **Network and egress.** Cross-zone traffic from unbalanced backends; cross-region image pulls;
  Cloud NAT and external load balancer counts growing faster than workloads.
- **Observability spend.** Cloud Logging and Monitoring ingestion volume per cluster and
  namespace, and the workloads or metric cardinality driving it; at this scale telemetry is a
  line item, and the agent's own footprint belongs in the same report.
- **Storage tiering.** Disk types and sizes against measured IOPS and fill level; snapshots and
  backups past retention; regional disks where zonal would do.
- **Chargeback.** Namespace-level cost allocation drift inside multi-tenant clusters, and tenants
  whose allocated share no longer matches their quota or label.
