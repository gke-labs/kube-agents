# Single-Cluster Inventory Audit (`bootstrap-inventory-cluster-<profile>`)

**Purpose:** The per-cluster half of first-time environment discovery, performed by a Cluster Agent
on the one cluster it is pinned to. The onboarding gate files this card, one per Cluster Agent up to the first-run cap (the sweep audits the rest), during
`bootstrap-inventory-scan`, and collects the results from every card; this SOP covers only what one
Cluster Agent does with its own cluster.

You are one of several agents auditing in parallel. **Report your findings on your card and write
nothing to shared state.** Specifically: do not write `/opt/data/INVENTORY.raw.md` or
`/opt/data/INVENTORY.md`, do not create kanban cards, and do not check whether either file already
exists. Those steps belong to the hand-off, and running them here means several writers on one
path at once.

**Never block this card, whatever fails** — not for a failed `cluster_preflight.sh`, denied
permissions, a cluster in `ERROR`, credentials that will not mint, or an MCP tool that errors. This
overrides `SOUL.md` §6 step 2, your own `AGENTS.md` ("Fail loud, never silent"), and `SOUL.md` §2.
The hand-off waits for this card to finish (`done`, `blocked`, `triage`, `failed` or `cancelled`) and then reads its `metadata`. A blocked
card has none, so the report names its cluster as a gap carrying only the block reason, and nothing
re-arms `.bootstrap_scan_filed` to try again — onboarding runs once. Record what failed in `gaps`,
and complete: the report then shows what you did find and what you could not do.

**A preflight failure other than check 5 means you audit nothing.** Complete with the failure in
`gaps` and every other field empty. The script stops at the first failure, so a failure in checks
1–4 means you have not established which cluster you are — you have only failed to check. Decide
from the `check` field the script reports, not the remediation text: a missing `USER.md` and a
missing kubeconfig both say "Re-scaffold the profile", and both leave you as unidentified as a
context mismatch does. An
unpinned `kubectl` resolves to the credential proxy's own context, the management cluster, so an
audit run anyway files another cluster's workloads under your name. The hand-off copies your
`metadata` into the report verbatim and the ranking stage runs no tooling, so nothing downstream
catches it.

Check `5`, "Cannot reach the target cluster's API server", is the exception: your identity is
established and the cluster is simply unreachable, so record it in `gaps` and complete like any
other data-cost failure.

---

## Step 1: Confirm which cluster you are

Check the `## Target Cluster Identity` section in your `SOUL.md` (already loaded in your system
prompt — do not call `read_file` or `search_files` to look up `USER.md`). It names the `project`,
`cluster`, and `location` you are pinned to.

**Do not derive your cluster from the environment.** `GKE_CLUSTER_NAME`, `GKE_LOCATION`,
`GKE_PROJECT_ID`, and `GCP_PROJECT_ID` are all set by the pod and name the management cluster the
harness itself runs on, not yours. An agent that trusts them audits the wrong cluster and reports
the result as if it were right.

If the `## Target Cluster Identity` section is missing or incomplete, complete the card with that as
the finding and audit nothing. Guessing your own identity from a profile name or a cluster list is
how a report ends up describing somebody else's cluster.

If the audit cannot be done at all for one of the data-cost reasons above, record what failed in
`gaps`, leave the other fields empty, and complete.

---

## Step 2: Cluster topology

Inspect your cluster's configuration: Kubernetes version, control plane region/zone, node pools
(machine types, node counts, autoscaling boundaries), network configuration (VPC-native, Dataplane
V2 / eBPF), and enabled GKE features (Workload Identity, Managed Prometheus, OpenTelemetry
collection).

Your `KUBECONFIG` is pinned to this cluster, so plain `kubectl` reaches it and nothing else. For
control-plane settings that `kubectl` cannot see, the GKE MCP tools take a
`projects/<project>/locations/<location>/clusters/<cluster>` parent — build it from the
`## Target Cluster Identity` section of `SOUL.md`, not from the environment.

---

## Step 3: Workload & Service SRE audit

Across all namespaces on this cluster:

1. **Multi-Tenancy & Governance:** List all non-system namespaces (`kubectl get ns`). Verify whether
   ResourceQuotas and LimitRanges are configured to enforce boundary defense.
2. **Workloads:**
   - List all Deployments, StatefulSets, DaemonSets, and Jobs
     (`kubectl get deployments,statefulsets,daemonsets,jobs -A`).
   - **Startup probes:** Verify whether every workload has a `startupProbe` configured.
   - **Security Context:** Verify whether workloads use read-only root filesystems
     (`readOnlyRootFilesystem: true`). Check added `capabilities` in the same pass, and record any
     workload that adds one other than `SYS_ADMIN` as a **high** severity finding naming that
     workload and the capabilities it adds. An added capability widens what the container can do to
     the node, which is a different class of problem from a writable root filesystem; scored
     alongside missing probes it reads as one more hardening gap and the report ranks it out of
     sight. An added `SYS_ADMIN` is not this check: it counts as a privileged container, which
     Step 5 leaves to the first-run audits.
3. **Core Infrastructure Addons:** Check for ingress controllers (GKE Gateway API, NGINX),
   cert-manager, OpenTelemetry collectors (`gke-managed-otel`), and identity integration endpoints
   (`github-token-minter` / `minty`). Record which deployment toolchain manages the cluster —
   Config Sync (`config-management-system`), Argo CD, Flux, plain Helm releases, or none observed:
   the fleet report has a column for it and no other stage can see your cluster.

**A pod list is not this audit.** `kubectl get pods` reports phase, and every gap above sits in a
workload's spec rather than its phase — a Deployment with no startup probe and a writable root
filesystem runs perfectly and reports `Running`. Read the specs
(`kubectl get deployments -A -o json` and friends), or the audit will report a clean cluster it
never checked.

---

## Step 4: Improvement analysis

Evaluate this cluster against modern GKE patterns. Use the `developer_knowledge` tool for current
Google Cloud and GKE best practices where it helps.

1. **Observability & Telemetry:** Check whether an OpenTelemetry collector is deployed and actively
   receiving workload traces/metrics. `gke-managed-otel` is the default, but a self-hosted collector
   (commonly `otel-collector.otel-collector`) is equally valid — do not infer from the namespace list
   alone. Raise enabling OTel collection (`OTLP` / Telemetry API) as a finding **only if no collector
   is reachable at all** — a cluster exporting to one you did not expect is not a gap. Check whether
   Managed Service for Prometheus (`gmp-system` / PodMonitoring CRDs) is enabled.
2. **Alerting Hygiene:** Identify missing standard SRE health alerts — `CrashLoopBackOff` /
   `OOMKilled` events, control plane API latency, PersistentVolumeClaim exhaustion, probe failures —
   and whether alerting is driven by SLOs and error budgets rather than raw infrastructure
   thresholds.
3. **Security Hardening:** Verify whether pods reaching Google Cloud APIs use Workload Identity
   (`serviceAccountName` with an `iam.gke.io/gcp-service-account` annotation) rather than static
   keys. On Standard clusters, evaluate Shielded GKE Nodes, Dataplane V2, and node auto-upgrades.

---

## Step 5: Report on your card

Complete the card with `kanban_complete`, supplying **both** a human-readable `result` and a
structured `metadata` object. The onboarding hand-off reads `metadata` verbatim off this card and
builds the fleet tables and the findings block from it, so a finding that appears only in `result` prose is a
finding the report loses.

`metadata` must have this shape:

```json
{
  "cluster": "<cluster name>",
  "location": "<region or zone>",
  "project": "<project id>",
  "topology": {
    "k8s_version": "…",
    "node_pools": [
      { "name": "…", "machine_type": "…", "nodes": 0, "autoscaling": "…" }
    ],
    "workload_identity": true,
    "dataplane_v2": true,
    "observability": "gke-managed-otel | self-hosted | none",
    "deployment_toolchain": "<Config Sync, Argo CD, Flux, Helm releases, or none observed>"
  },
  "workloads": [
    {
      "namespace": "…",
      "name": "…",
      "kind": "Deployment",
      "replicas": "1/1",
      "probes": { "startup": false },
      "telemetry": "otlp | prometheus-scrape | none",
      "security_context": {
        "read_only_root_fs": false,
        "added_capabilities": []
      }
    }
  ],
  "namespace_governance": [
    {
      "namespace": "…",
      "resource_quota": false,
      "limit_range": false
    }
  ],
  "findings": [
    {
      "check": "probes-startup",
      "severity": "high|medium|low",
      "area": "security|reliability|observability",
      "namespace": "…",
      "workload": "…",
      "issue": "…",
      "recommendation": "…"
    }
  ],
  "gaps": ["<any step that could not be completed, and why>"]
}
```

`check` is a lowercase hyphenated slug naming the condition, stable across sweeps: with the project,
cluster, namespace and workload it is the finding's identity in the findings queue. These are the
audit streams' own slugs where a stream has one (`probes-startup`, `readonly-root-fs`, `no-resourcequota`, `static-sa-key` and `added-capabilities` have none yet); use one where it fits. Using them means the same
problem carries one identity whichever source found it, and a finding promoted out of the queue
routes to the stream that owns the check.

| what you found                   | check slug           |
| -------------------------------- | -------------------- |
| missing `startupProbe`           | `probes-startup`     |
| ResourceQuota and LimitRange     | `no-resourcequota`   |
| missing `readOnlyRootFilesystem` | `readonly-root-fs`   |
| Shielded Nodes                   | `shielded-nodes`     |
| Dataplane V2                     | `datapath-provider`  |
| Managed Service for Prometheus   | `managed-prometheus` |
| node auto-upgrade                | `no-autoupgrade`     |
| pods on static Google Cloud keys | `static-sa-key`      |
| added capabilities               | `added-capabilities` |

Checks the first-run audits own (cost, security, reliability and capacity: liveness and readiness
probes, requests and limits, HPA, NetworkPolicy, the cluster's Workload Identity setting
(`workload-identity-off`; a pod's static keys are `static-sa-key` above), pod security, privileged
and host-namespace workloads, and an added `SYS_ADMIN` among them) are not reported here, under any
slug. Those audits report them with their own evidence,
and a second copy from this card reaches the user as a separate problem.

For anything else, write a lowercase hyphenated slug naming the condition, and keep it stable: it
is the row's identity across every later sweep.

One `findings` entry names one workload. `"workload": "multiple workloads"`, a comma-separated list,
or `"e.g. networking-dra-driver"` all collapse on the way into the report into a line the user
cannot act on; file one entry per affected workload instead, even when the issue and the
recommendation repeat verbatim.

`workloads` must hold one entry per workload Step 3 listed — every Deployment, StatefulSet,
DaemonSet, and Job, including the ones with no findings. The hand-off has no way to go back
for the rest, and a summary row (`"workload": "multiple"`) is not one of them: the report cannot name
what to fix from it. Before you complete the card, compare `len(workloads)` against the count Step 3
produced; if it is short, either finish the enumeration or record the difference and the reason in
`gaps`. An empty `gaps` alongside a short `workloads` reads downstream as a clean, complete result.

Keep `result` to a short summary for a human reader: the cluster, how many workloads you audited,
and the headline findings. The detail belongs in `metadata`.
