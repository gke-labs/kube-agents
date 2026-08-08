# SOP: GKE Runtime Telemetry & Linux Kernel Audit (Daily Governance)

**Purpose:** Sweep every managed GKE cluster for Linux cgroup, kernel conntrack, load-balancer draining, and OS-level runtime failure modes that pass declarative manifest validation but cause severe real-time latency spikes, connection drops, or pod evictions. The question this audit answers for a platform admin is: _which workloads are suffering invisible CPU throttling, conntrack saturation, or abrupt 502 drops during rolling updates?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying generated manifests for the findings that get promoted.

**Cron:** id `gke-runtime-telemetry-audit`, schedule `30 7 * * *` (daily 07:30 UTC).

**Data sources:** Google Cloud Monitoring API (`gcloud monitoring`), Prometheus / Managed Prometheus metrics (`container_cpu_cfs_throttled_periods_total`, `node_nf_conntrack_entries`), `kubectl` read verbs, and Cloud Logging HTTP ingress logs.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit gke-runtime-telemetry-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"...", "findings_path":"/opt/data/scratch/findings_gke-runtime-telemetry-audit.json"}`.

### 1. Enumerate the target fleet

```bash
gcloud container clusters list --format=json
```

Record all running clusters into `scope.clusters` with mandatory `checks_run` objects.

### 2. Execute runtime telemetry queries

```bash
./skills/gke-runtime-telemetry-audit/scripts/telemetry_audit.py --output /opt/data/scratch/telemetry_raw.json
```

---

## Section 3: Diagnostic Checks Roster

#### `cfs-quota-throttling`

- **Severity**: `major`
- **Command**: `kubectl --context=$CLUSTER top pods -A --containers`
- **Condition**: Container experiences `container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total > 0.20` over a 1-hour window.
- **Remediation**: Remove rigid CPU limits or adjust CPU burst requests in workload Deployment spec.

#### `conntrack-saturation`

- **Severity**: `critical`
- **Command**: `kubectl --context=$CLUSTER get nodes -o json`
- **Condition**: GKE Node conntrack entry utilization exceeds 75% (`node_nf_conntrack_entries / node_nf_conntrack_limit > 0.75`).
- **Remediation**: Inject `dnsConfig.options: [{name: ndots, value: "1"}]` and deploy NodeLocal DNSCache in workload manifest.

#### `ingress-502-drain`

- **Severity**: `major`
- **Command**: `kubectl --context=$CLUSTER get deployments,services,ingresses -A -o json`
- **Condition**: Edge-facing HTTP ingress gateways (`Deployment` with associated `Service`/`Ingress`) lack a `lifecycle.preStop` sleep hook.
- **Remediation**: Inject `lifecycle.preStop.exec.command: ["/bin/sh", "-c", "sleep 15"]` to allow Cloud Load Balancer EndpointSlice drainage.

#### `ephemeral-growth-rate`

- **Severity**: `major`
- **Command**: `kubectl --context=$CLUSTER get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.spec.containers[*].resources}{"\n"}{end}'`
- **Condition**: Workload writes unmetered files to local storage without declaring `resources.limits.ephemeral-storage`.
- **Remediation**: Set `resources.requests.ephemeral-storage` and `resources.limits.ephemeral-storage` in container spec.

#### `ulimit-exhaustion`

- **Severity**: `minor`
- **Command**: `kubectl --context=$CLUSTER get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.spec.securityContext}{"\n"}{end}'`
- **Condition**: High-concurrency network proxies exceed 80% of container file descriptor ceiling.
- **Remediation**: Configure explicit `securityContext` sysctls or increase container ulimit allocations.

---

### 4. Emit findings.json

Write the schema exactly as validated by the helper to `findings_path`:

```json
{
  "audit": "gke-runtime-telemetry-audit",
  "scope": {
    "clusters": [
      {
        "name": "cluster-1",
        "location": "us-central1-a",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "cfs-quota-throttling",
            "command": "kubectl --context=cluster-1 top pods -A --containers"
          }
        ]
      }
    ],
    "skipped": []
  },
  "findings": []
}
```

### 5. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit gke-runtime-telemetry-audit \
  --findings-file /opt/data/scratch/findings_gke-runtime-telemetry-audit.json
```

---

## Red Lines

- **Never mutate live GKE clusters or nodes directly.** This audit is strictly read-only.
- **Never emit a manifest that directly deletes a namespace, pod, or cluster resource.**
- **Never export authorization tokens, API keys, or credentials into finding evidence.**
