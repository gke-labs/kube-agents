# SOP: FinOps & Cloud Waste Audit (Daily Governance)

**Purpose:** Sweep all monitored GCP projects and GKE clusters for gross cloud resource over-allocation, orphaned retained PersistentVolumes, unattached external static IPs, and unindexed Cloud Logging cost runaway. The question this audit answers for a platform admin is: _which workloads are requesting hundreds of unutilized CPU cores, which deleted PVCs left billable persistent disks behind, and where are unmetered debug logs causing runaway cloud spend?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying manifest and Terraform fixes for the findings that get promoted.

**Cron:** id `finops-cloud-waste-audit`, schedule `30 8 * * *` (daily 08:30 UTC).

**Data sources:** Google Cloud Billing & Asset Inventory APIs, `kubectl get pv,pvc -A`, Cloud Logging log volume metrics, and GCE static address descriptors.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit finops-cloud-waste-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"...", "findings_path":"/opt/data/scratch/findings_finops-cloud-waste-audit.json"}`.

### 1. Enumerate the target fleet

```bash
kubectl get pv -o json
```

Record all target clusters and cloud scopes into `scope.clusters` with mandatory `checks_run` objects.

### 2. Execute cloud waste inspection

```bash
./skills/finops-cloud-waste-audit/scripts/finops_waste_audit.py --output /opt/data/scratch/finops_raw.json
```

---

## Section 3: Diagnostic Checks Roster

#### `massive-overrequest`

- **Severity**: `critical`
- **Command**: `kubectl --context=$CLUSTER get deployments,statefulsets -A -o json`
- **Condition**: Workload requests `> 100` CPU cores or `> 500Gi` RAM with historical 30-day peak utilization `< 5%`.
- **Remediation**: Rightsize `resources.requests` in Deployment spec based on historical usage telemetry.

#### `orphan-retained-pvs`

- **Severity**: `major`
- **Command**: `kubectl --context=$CLUSTER get pv -o json`
- **Condition**: PersistentVolume has `persistentVolumeReclaimPolicy: Retain` and `Status: Released` with zero bound PVCs, incurring continuous storage expense.
- **Remediation**: Clean up released unattached disk storage via `kind: manual` or `kind: gcloud`.

#### `unattached-static-ips`

- **Severity**: `major`
- **Command**: `gcloud compute addresses list --filter="status=RESERVED" --format=json`
- **Condition**: Reserved external regional or global IP address has `status: RESERVED` without an attached forwarding rule or instance.
- **Remediation**: Release unused static IP reservation via `kind: manual` or `kind: gcloud`.

#### `cloud-logging-cost-runaway`

- **Severity**: `major`
- **Command**: `gcloud logging sinks list --format=json`
- **Condition**: Verbose application stdout log volume exceeds 10GB/day of unindexed debug entries.
- **Remediation**: Configure Cloud Logging sink exclusion filters in GCP telemetry exporter or Terraform baseline.

#### `idle-backend-services`

- **Severity**: `minor`
- **Command**: `gcloud compute backend-services list --format=json`
- **Condition**: Cloud Load Balancing backend service receives 0 requests over a 14-day window.
- **Remediation**: Prune idle forwarding rules and backend services via `kind: manual` or `kind: gcloud`.

---

### 4. Emit findings.json

Write the schema exactly as validated by the helper to `findings_path`:

```json
{
  "audit": "finops-cloud-waste-audit",
  "scope": {
    "clusters": [
      {
        "name": "finops-fleet-global",
        "location": "global",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "massive-overrequest",
            "command": "kubectl --context=cluster-1 get deployments,statefulsets -A -o json"
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
./skills/fleet-audit/scripts/audit_report.py finish --audit finops-cloud-waste-audit \
  --findings-file /opt/data/scratch/findings_finops-cloud-waste-audit.json
```

---

## Red Lines

- **Never mutate live cloud billing accounts or financial setups.** This audit is strictly read-only.
- **Never emit a manifest that deletes a PV, PVC, namespace, disk, snapshot, or address.** Deletion remediations are `kind: manual` or `kind: gcloud` only. A manifest is one merge away from `main` — a `critical` one opens its own pull request without a human asking.
- **Never export cloud billing account numbers or credit card identifiers to finding evidence.**
