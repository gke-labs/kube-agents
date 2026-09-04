# Single-Cluster Inventory Audit (`bootstrap-inventory-cluster-<cluster-name>-<location>`)

**Purpose:** The per-cluster half of first-time environment discovery, performed by a Cluster Agent
on the one cluster it is pinned to. The Platform Agent fans this out during
`bootstrap-inventory-scan` (`inventory.md`) and aggregates the results; this SOP covers only what
one Cluster Agent does with its own cluster.

You are one of several agents auditing in parallel. **Report your findings on your card and write
nothing to shared state.** Specifically: do not write `/opt/data/INVENTORY.raw.md` or
`/opt/data/INVENTORY.md`, do not create kanban cards, and do not check whether either file already
exists. Those steps belong to the Platform Agent, and running them here means several agents
writing one path at once.

**Never block this card, whatever fails** — not for a failed `cluster_preflight.sh`, denied
permissions, a cluster in `ERROR`, credentials that will not mint, or an MCP tool that errors. This
overrides `SOUL.md` §6 step 2, your own `AGENTS.md` ("Fail loud, never silent"), and `SOUL.md` §2.
The platform card that fanned this one out waits for it to reach `done` or `archived` before it
compiles the fleet report (and a pre-#1010 aggregation card, where one is still in flight, lists
this card in its `parents`, which `claim_task` enforces the same way). A block is not `done`, nothing re-arms
`.bootstrap_scan_filed`, and the notifier reaches nobody on a card the cron gate filed — so one
blocked card costs the fleet report permanently and silently. Record what failed in `gaps`, and
complete.

**A preflight failure other than check 5 means you audit nothing.** Complete with the failure in
`gaps` and every other field empty. The script stops at the first failure, so a failure in checks
1–4 means you have not established which cluster you are — you have only failed to check. Decide
from the `check` field the script reports, not the remediation text: a missing `USER.md` and a
missing kubeconfig both say "Re-scaffold the profile", and both leave you as unidentified as a
context mismatch does. An
unpinned `kubectl` resolves to the credential proxy's own context, the management cluster, so an
audit run anyway files another cluster's workloads under your name. Aggregation copies `metadata`
verbatim and the Platform Agent is forbidden to re-audit, so nothing downstream catches it.

Check `5`, "Cannot reach the target cluster's API server", is the exception: your identity is
established and the cluster is simply unreachable, so record it in `gaps` and complete like any
other data-cost failure.

---

## Step 1: Confirm which cluster you are

Read `USER.md` in your own profile home — the absolute path is in the `Active Hermes profile` line
of your system prompt, as `<profile home>/USER.md`. It names the project, cluster, and location you
are pinned to.

**Do not derive your cluster from the environment.** `GKE_CLUSTER_NAME`, `GKE_LOCATION`,
`GKE_PROJECT_ID`, and `GCP_PROJECT_ID` are all set by the pod and name the management cluster the
harness itself runs on, not yours. An agent that trusts them audits the wrong cluster and reports
the result as if it were right.

If `USER.md` is missing, incomplete, or does not parse, complete the card with that as the finding
and audit nothing. Guessing your own identity from a profile name or a cluster list is how a report
ends up describing somebody else's cluster.

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
`projects/<project>/locations/<location>/clusters/<cluster>` parent — build it from `USER.md`, not
from the environment.

---

## Step 3: Workload & Service SRE audit

Across all namespaces on this cluster:

1. **Multi-Tenancy & Governance:** List all non-system namespaces (`kubectl get ns`). Verify whether
   ResourceQuotas, LimitRanges, and NetworkPolicies are configured to enforce boundary defense.
2. **Workload Health & QoS:**
   - List all Deployments, StatefulSets, DaemonSets, and Jobs
     (`kubectl get deployments,statefulsets,daemonsets,jobs -A`).
   - **Probes:** Verify that every workload has `livenessProbe`, `readinessProbe`, and
     `startupProbe` configured.
   - **Resource Management:** Verify that containers define explicit `requests` and `limits` (record
     the Quality of Service class: `Guaranteed`, `Burstable`, or `BestEffort`).
   - **Scaling:** Audit Horizontal Pod Autoscaler settings (`minReplicas`, `maxReplicas`, metrics
     targets).
   - **Security Context:** Verify whether workloads run as non-root (`runAsNonRoot: true`) and use
     read-only root filesystems (`readOnlyRootFilesystem: true`). Check `privileged`, `hostPID`,
     `hostNetwork`, `hostIPC`, and added `capabilities` in the same pass, and record any workload
     that sets one as a **high** severity finding naming that workload. Those five grant escape from
     the container to the node, which is a different class of problem from a writable root
     filesystem; scored alongside missing probes they read as one more hardening gap and the report
     ranks them out of sight.
3. **Core Infrastructure Addons:** Check for ingress controllers (GKE Gateway API, NGINX),
   cert-manager, OpenTelemetry collectors (`gke-managed-otel`), and identity integration endpoints
   (`github-token-minter` / `minty`). Record which deployment toolchain manages the cluster —
   Config Sync (`config-management-system`), Argo CD, Flux, plain Helm releases, or none observed:
   the fleet report has a column for it and no other stage can see your cluster.

**A pod list is not this audit.** `kubectl get pods` reports phase, and every gap above sits in a
workload's spec rather than its phase — a Deployment with no probes, no resource requests, and no
security context runs perfectly and reports `Running`. Read the specs
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
   keys. On Standard clusters, evaluate Shielded GKE Nodes, Dataplane V2, node auto-upgrades, and
   Pod Security Admission.

---

## Step 5: Report on your card

Complete the card with `kanban_complete`, supplying **both** a human-readable `result` and a
structured `metadata` object. The Platform Agent's waiting sweep card reads `metadata` verbatim off
this card and builds the fleet tables from it, so a finding that appears only in `result` prose is a
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
      "probes": { "liveness": false, "readiness": false, "startup": false },
      "resources": { "requests": false, "limits": false, "qos": "BestEffort" },
      "hpa": false,
      "telemetry": "otlp | prometheus-scrape | none",
      "security_context": {
        "run_as_non_root": false,
        "read_only_root_fs": false,
        "privileged": false,
        "host_pid": false,
        "host_network": false,
        "added_capabilities": []
      }
    }
  ],
  "namespace_governance": [
    {
      "namespace": "…",
      "resource_quota": false,
      "limit_range": false,
      "network_policy": false
    }
  ],
  "findings": [
    {
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

One `findings` entry names one workload. `"workload": "multiple workloads"`, a comma-separated list,
or `"e.g. networking-dra-driver"` all collapse on the way through aggregation into a line the user
cannot act on; file one entry per affected workload instead, even when the issue and the
recommendation repeat verbatim.

`workloads` must hold one entry per workload Step 3 listed — every Deployment, StatefulSet,
DaemonSet, and Job, including the ones with no findings. The aggregation stage has no way to go back
for the rest, and a summary row (`"workload": "multiple"`) is not one of them: the report cannot name
what to fix from it. Before you complete the card, compare `len(workloads)` against the count Step 3
produced; if it is short, either finish the enumeration or record the difference and the reason in
`gaps`. An empty `gaps` alongside a short `workloads` reads downstream as a clean, complete result.

Keep `result` to a short summary for a human reader: the cluster, how many workloads you audited,
and the headline findings. The detail belongs in `metadata`.
