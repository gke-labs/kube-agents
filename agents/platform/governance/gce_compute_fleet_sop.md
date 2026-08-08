# SOP: GCE Compute Engine & MIG Fleet Audit (Daily Governance)

**Purpose:** Sweep all monitored GCP projects for unmanaged GCE virtual machines, Managed Instance Groups (MIGs), serial console boot failures, and guest OS daemon health. The question this audit answers for a platform admin is: _which GCE VMs failed startup initialization, which MIGs are caught in autohealing death loops, and where are guest OS monitoring agents failing?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying Terraform fixes for the findings that get promoted.

**Cron:** id `gce-compute-fleet-audit`, schedule `45 7 * * *` (daily 07:45 UTC).

**Data sources:** Google Compute Engine API (`gcloud compute instances`, `gcloud compute instance-groups`), serial port console logs (`get-serial-port-output`), and Cloud Monitoring guest metrics.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit gce-compute-fleet-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"...", "findings_path":"/opt/data/scratch/findings_gce-compute-fleet-audit.json"}`.

### 1. Enumerate the target fleet

```bash
gcloud compute instances list --format=json
```

Record all running instances into `scope.clusters` with mandatory `checks_run` objects.

### 2. Execute compute fleet inspection

```bash
./skills/gce-compute-fleet-audit/scripts/compute_fleet_audit.py --output /opt/data/scratch/compute_raw.json
```

---

## Section 3: Diagnostic Checks Roster

#### `gce-startup-script-status`

- **Severity**: `critical`
- **Command**: `gcloud compute instances get-serial-port-output $VM --zone=$ZONE`
- **Condition**: GCE instance serial console log reveals `startup-script exit status 1` or systemd service failure during boot.
- **Remediation**: Correct Terraform `metadata_startup_script` or inject proper systemd unit definitions.

#### `mig-autoscaler-flapping`

- **Severity**: `major`
- **Command**: `gcloud compute instance-groups managed list-instances $MIG --region=$REGION --format=json`
- **Condition**: GCE Managed Instance Group (MIG) experiences continuous auto-healing recreation cycles due to health check path/port mismatch.
- **Remediation**: Align Terraform `google_compute_health_check` endpoint with application listening port.

#### `ops-agent-guest-health`

- **Severity**: `major`
- **Command**: `gcloud compute instances list --filter="status=RUNNING" --format=json`
- **Condition**: GCE VM has not emitted Google Cloud Ops Agent heartbeat metrics for > 2 hours.
- **Remediation**: Re-provision Ops Agent configuration via OS Config policy or Terraform module.

#### `sole-tenant-headroom`

- **Severity**: `major`
- **Command**: `gcloud compute sole-tenancy node-groups list --format=json`
- **Condition**: Sole-tenant node group reaches > 90% CPU/memory allocation without autoscaling node group policies.
- **Remediation**: Configure `google_compute_node_group` autoscaling policy in Terraform.

#### `orphaned-snapshots`

- **Severity**: `minor`
- **Command**: `gcloud compute snapshots list --format=json`
- **Condition**: GCE Persistent Disk snapshots older than 90 days with zero associated active instances or restore schedules.
- **Remediation**: Prune stale disk snapshots in Terraform baseline (`kind: manual` or `kind: gcloud`).

---

### 4. Emit findings.json

Write the schema exactly as validated by the helper to `findings_path`:

```json
{
  "audit": "gce-compute-fleet-audit",
  "scope": {
    "clusters": [
      {
        "name": "gce-fleet-global",
        "location": "global",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "gce-startup-script-status",
            "command": "gcloud compute instances get-serial-port-output vm-1 --zone=us-central1-a"
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
./skills/fleet-audit/scripts/audit_report.py finish --audit gce-compute-fleet-audit \
  --findings-file /opt/data/scratch/findings_gce-compute-fleet-audit.json
```

---

## Red Lines

- **Never delete or stop live GCE VM instances during this audit.**
- **Never emit a manifest that directly deletes a Persistent Disk, snapshot, or VM instance.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
- **Never export serial console boot authentication logs containing sensitive credentials.**
