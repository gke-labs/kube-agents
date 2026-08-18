# SOP: GKE Runtime Telemetry & Container OS Health Audit (Daily Governance)

**Purpose:** Sweep all managed GKE clusters for container runtime health regressions, cgroup CFS quota throttling, silent conntrack table drops, missing preStop sleep hooks on load-balanced services, rapid ephemeral storage growth, and open file descriptor exhaustion. The question this audit answers for a platform admin is: _which workloads are silently dropping ingress traffic during rolling updates due to missing preStop drain hooks, which containers are suffering tail-latency spikes from CFS quota throttling, and which nodes are running out of conntrack capacity?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying manifest fixes for the findings that get promoted.

**Cron:** id `gke-runtime-telemetry-audit`, schedule `30 7 * * *` (daily 07:30 UTC).

**Data sources:** `kubectl` read verbs and `gcloud container clusters ...` across all managed fleet clusters (`GCP_PROJECT_ID` and `MONITORED_PROJECT_IDS`).

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit gke-runtime-telemetry-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/gke-runtime-telemetry-audit/org__repo", "findings_path":"/opt/data/scratch/findings_gke-runtime-telemetry-audit.json", "pending_remediation_requests": [<finding_id>, ...]}`.

If `pending_remediation_requests` is non-empty, inspect each requested finding in the open issue and write the updated workload manifest file to `workspace` at `remediation.path` before proceeding to step 3 (`finish`).

### 1. Enumerate the target fleet

```bash
gcloud container clusters list --format=json
```

- Target every cluster with `status == "RUNNING"`. Record `{name, location, project, checks_run}` into `scope.clusters`.
- Obtain per-cluster credentials into an isolated kubeconfig so clusters cannot bleed into each other:
  ```bash
  export KC="${HERMES_HOME:-/opt/data}/.kubeconfigs/kubeconfig_<project>_<cluster>_<location>.yaml"
  KUBECONFIG=$KC gcloud container clusters get-credentials <cluster> --location=<location> --project=<project>
  ```
- **`checks_run` is mandatory on every cluster:** Each entry is an object `{"check": "<slug>", "command": "<literal command>"}` naming the exact inspection command executed on that cluster.
- A cluster you cannot reach goes in `scope.skipped` with a reason string. If a cluster is partially readable, record the refusal in its `limitations` string. Declare structurally inapplicable checks in `checks_not_applicable`.

### 2. Diagnostic checks roster

- **S1 — system namespace:** `kube-system`, `kube-public`, `kube-node-lease`, `gmp-system`, `gmp-public`, `gke-gmp-system`, `cnrm-system`, `configconnector-operator-system`, `krmapihosting-system`, `istio-system`, `asm-system`, `anthos-identity-service`, `gatekeeper-system`, `composer-system`, or any namespace matching `gke-*`, `gke-managed-*`, or `config-management-*`.
- **S2 (Managed addons)**: Exclude resources annotated with `addonmanager.kubernetes.io/mode`.
- **S3 (Controller-managed)**: Exclude child Pods carrying `ownerReferences`. Target parent Deployments, StatefulSets, or DaemonSets.
- **S4 (Scaled-to-zero)**: Exclude workloads where `spec.replicas == 0`.

#### 2.1 Severe cgroup CFS CPU quota throttling (`cfs-quota-throttling`)

- **Severity**: `major`
- **Command**: `kubectl --context=$CLUSTER get pods,deployments -A -o json`
- **Condition**: Workload container specifies restrictive fractional CPU limits (`limits.cpu < 500m` with `requests.cpu == limits.cpu`) without CPU burst support.
- **Do NOT flag**: Multi-core workloads or containers with unconstrained CPU ceilings.
- **Remediation**: `kind: manifest` at the workload declaration's path, adjusting container CPU limits or enabling CPU burst. When the manifest is not found in the repo, `kind: manual`.

#### 2.2 Kernel conntrack table saturation and silent drop risk (`conntrack-saturation`)

- **Severity**: `major`
- **Command**: `kubectl --context=$CLUSTER get daemonsets,nodes -n kube-system -o json`
- **Condition**: Cluster nodes or system tuning DaemonSets configure sub-optimal `nf_conntrack_max` thresholds for high-throughput packet routing.
- **Do NOT flag**: Autopilot managed clusters where node sysctls are managed by GKE.
- **Remediation**: `kind: manifest` at the configuration file path (DaemonSet or ConfigMap), configuring `net.netfilter.nf_conntrack_max`. When the manifest is not found, `kind: manual`.

#### 2.3 Missing graceful shutdown preStop hooks on load-balanced services (`ingress-502-drain`)

- **Severity**: `major`
- **Command**: `kubectl --context=$CLUSTER get svc,deployments -A -o json`
- **Condition**: Service-exposed workload behind Ingress or Gateway lacks `lifecycle.preStop.exec` sleep hook (e.g. `sleep 15`) and `terminationGracePeriodSeconds` is default 30s.
- **Do NOT flag**: Workloads not exposed by a Service or background batch/queue workers.
- **Remediation**: `kind: manifest` at the workload declaration's path, adding `lifecycle.preStop.exec.command: ["/bin/sh", "-c", "sleep 15"]` to the container manifest. When the manifest is not found, `kind: manual`.

#### 2.4 Unbounded container ephemeral-storage growth (`ephemeral-growth-rate`)

- **Severity**: `minor`
- **Command**: `kubectl --context=$CLUSTER get pods,deployments -A -o json`
- **Condition**: Container writes ephemeral logs/scratch data without specifying `resources.limits.ephemeral-storage`.
- **Do NOT flag**: Workloads mounting dedicated `emptyDir` or PersistentVolumeClaims for scratch data.
- **Remediation**: `kind: manifest` at the workload declaration's path, adding explicit `resources.requests.ephemeral-storage` and `limits.ephemeral-storage`. When the manifest is not found, `kind: manual`.

#### 2.5 Container runtime file descriptor limit exhaustion (`ulimit-exhaustion`)

- **Severity**: `minor`
- **Command**: `kubectl --context=$CLUSTER get deployments,statefulsets -A -o json`
- **Condition**: High-concurrency reverse proxy or database workload runs with default low file descriptor limits without explicit initContainer system tuning.
- **Do NOT flag**: Batch workloads and low-concurrency microservices.
- **Remediation**: `kind: manifest` at the workload declaration's path, setting appropriate container system parameters or initContainer ulimits. When the manifest is not found, `kind: manual`.

### 3. Generate remediation artifacts

- Write every `kind: manifest` file into the `workspace` clone §0 named, **before** calling `finish`. A path with no file behind it no longer kills the run: that one finding degrades to `kind: manual`, keeps its evidence and recommendation, and says in the ledger that the audit named the fix but never wrote it — the report still publishes. Treat a degrade as a defect in your own work, not a fallback. This includes every finding named in `pending_remediation_requests` from §0 — a `/remediate` request with no manifest on disk cannot be promoted.
- **Where the file goes depends on whether the object already exists. Both branches discover a directory that is already there; neither invents one.**
- **Changing an object that already exists** — 2.1, 2.3, 2.4, 2.5 workload edits, and 2.2 node system tuning — goes to that object's **existing declaration in the GitOps repo**: locate it (`grep -rl "name: <object>" --include='*.yaml' .`), name that file as `remediation.path`, and rewrite it as the object's complete desired manifest. Never write a patch fragment: a file carrying `metadata.name` and a partial `spec` is not valid `kubectl apply` input, and a second file claiming an object the repo already declares is a duplicate resource id that both Config Sync and Argo reject.
- **Never create a new top-level directory, and never write to a path whose parent directory does not already exist in the clone.**
- **If the object already exists and you cannot find its declaration, the finding is `kind: manual`.** Describe the change in `recommendation.action`, write no file, and omit `remediation.path`. Never invent a new path for it.
- `remediation.path` is relative to the repository root — which is `workspace`, not the directory you happen to be in — and must match the file you wrote exactly. No `..`, no glob metacharacter (`*`, `?`, `[`, `]`), no leading `:` — the helper rejects all of them.
- For `kind: manual`, write no file and **omit `remediation.path` entirely** — the helper rejects a path on a non-manifest remediation. Put the ordered human steps in `remediation.note`. Both are never promotable to a PR; a `/remediate` request naming one is refused.
- Head each file with a comment naming the cluster, the check, and the finding id.
- Copy selectors and labels verbatim from the live object. Never invent a resource quantity, replica count, or utilization target.
- Manifests are proposals. Never `kubectl apply` them and never embed a live `resourceVersion`.

### 4. Emit findings.json

Write the whole document to `findings_path` in one shot, with `audit: "gke-runtime-telemetry-audit"`, `scope.clusters` listing every cluster you queried — each carrying the `checks_run` list §1 required and, where §1 recorded them, that cluster's `checks_not_applicable` entries and `limitations` string — and `scope.skipped` listing only the targets you could not read.

`command` in `checks_run` is the literal inspection command executed, and anything under eight characters is rejected.

Every finding must conform to the full findings schema:

```json
{
  "audit": "gke-runtime-telemetry-audit",
  "scope": {
    "clusters": [
      {
        "name": "cluster-1",
        "location": "us-central1-c",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "ingress-502-drain",
            "command": "kubectl --context=cluster-1 get svc,deployments -A -o json"
          }
        ]
      }
    ],
    "skipped": []
  },
  "findings": [
    {
      "check": "ingress-502-drain",
      "severity": "major",
      "title": "Service-exposed deployment frontend-app lacks preStop hook for graceful drain",
      "cluster": "cluster-1",
      "namespace": "production",
      "object": "Deployment/frontend-app",
      "impact": "In-flight client connections receive HTTP 502 Bad Gateway during rolling updates.",
      "evidence": {
        "command": "kubectl --context=cluster-1 get deployment frontend-app -n production -o json",
        "excerpt": "name: frontend-container, lifecycle: null"
      },
      "recommendation": {
        "action": "Add lifecycle.preStop sleep 15 hook to frontend-app container in GitOps manifest.",
        "rationale": "Allows ingress proxies and iptables 15s to deregister the dying pod before SIGTERM.",
        "risk": "Increases rolling deployment duration by 15s per pod."
      },
      "remediation": {
        "kind": "manifest",
        "path": "clusters/cluster-1/workloads/frontend-app.yaml"
      }
    }
  ]
}
```

### 5. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit gke-runtime-telemetry-audit   --findings-file /opt/data/scratch/findings_gke-runtime-telemetry-audit.json
# -> {"status":"CLEAN"|"OPENED"|"UPDATED","issue_url":...,"new":n,"resolved":m,
#     "prs_opened":[...],"prs_closed":[...],"partial":false,"coverage_gaps":[],
#     "silent_ok":true}
```

- On a **scheduled** run, `silent_ok: true` -> your final response is exactly `[SILENT]`.
- **An on-demand run is never silent.** If a person dispatched this job, report the outcome and the ledger URL whatever `silent_ok` says.
- Repo writers can trigger remediation by commenting `/remediate <finding-id>` or `/remediate all` on the ledger issue.

---

## Red Lines

- **Read-only audit.** Never mutate live deployments, restart pods, or alter node sysctl parameters directly.
- **No hand-written issues or PRs.** `audit_report.py` owns the entire git/GitHub write path.
- **Never print raw credentials.** Secret tokens, certificates, and private keys must never reach an excerpt.
- **No unstable finding identity.** Name the durable resource identifier (`Deployment/<name>`), never an ephemeral pod name.
- **Never emit a manifest that directly deletes a workload.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
