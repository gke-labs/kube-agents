# SOP: GCE Compute Engine and MIG Fleet Audit (Daily Governance)

**Purpose:** Sweep all managed GCE Compute Engine instances and Managed Instance Groups (MIGs) across target GCP projects for failed startup scripts, flapping autoscaler recommendations, failing Ops Agent health metrics, sole-tenant headroom exhaustion, and orphaned storage snapshots. The question this audit answers for a platform admin is: _which standalone VMs or MIG instances have failed startup scripts, where are MIG autoscalers stuck in flapping resize loops, and which storage snapshots belong to deleted disks?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying Terraform or manifest fixes for the findings that get promoted.

**Cron:** id `gce-compute-fleet-audit`, schedule `45 7 * * *` (daily 07:45 UTC).

**Data sources:** `gcloud compute instances ...`, `gcloud compute instance-groups ...`, `gcloud compute resource-policies ...`, and `gcloud compute snapshots ...`, run once per project in the resolved project scope (§1).

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit gce-compute-fleet-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/gce-compute-fleet-audit/org__repo", "findings_path":"/opt/data/scratch/findings_gce-compute-fleet-audit.json", "pending_remediation_requests": [<finding_id>, ...]}`.

If `pending_remediation_requests` is non-empty, inspect each requested finding in the open issue and write the updated manifest or Terraform file to `workspace` at `remediation.path` before proceeding to step 3 (`finish`).

### 1. Enumerate the target fleet

**Resolve the project scope first.** The scope is the host project (`gcloud config get-value project`) plus every project `gcloud projects list --format="value(projectId)"` returns. Run every collection command once per project, passing `--project` explicitly — the ambient default silently audits one project and reports the result as a fleet sweep. The scope is what the agent's identity can read, so an operator narrows it by narrowing the IAM grant. A listing that exits non-zero, or that returns without the host project, cannot say how many other projects exist: sweep the projects you have and add one `scope.skipped` entry, `{"cluster": "project/UNENUMERATED_PROJECTS", "reason": "<the listing's rc and stderr excerpt, or the host project it omitted>"}`, so the run publishes as partial rather than as the whole fleet. A project where the API this audit reads is disabled (`SERVICE_DISABLED`, `accessNotConfigured`, `has not been used in project`) holds nothing to audit and counts as empty, not skipped: recording it as a loss would pin every run partial for as long as the project exists.

- **Pass `--project` explicitly on every collection command.** Never rely on the ambient default: it silently audits one project and reports the result as a fleet sweep.
- Record each resolved project as `{name: "project-" + project_id, location: "global", project: project_id, checks_run: [...]}` into `scope.clusters`.
- **`checks_run` is mandatory on every scope entry:** Each entry is an object `{"check": "<slug>", "command": "<literal command>"}` naming the exact inspection command executed on that project target.
- A project or target you cannot reach goes in `scope.skipped` with a reason string, **and the sweep continues** — one project's permission error never decides the outcome for the rest of the fleet. If a target is partially readable, record the refusal in its `limitations` string. Declare structurally inapplicable checks in `checks_not_applicable`.

### 2. Diagnostic checks roster

#### 2.1 Instance startup script failures in serial port output (`gce-startup-script-status`)

- **Severity**: `critical`
- **Command**: `gcloud compute instances get-serial-port-output $VM --zone=$ZONE --port=1`
- **Condition**: VM serial port console output contains fatal startup script errors (`startup-script exit status 1` or `Finished running startup scripts with error`).
- **Do NOT flag**: GKE node pool instances managed directly by GKE control plane or instances cleanly completing boot without errors.
- **Remediation**: Correct boot metadata or deployment configuration in instance template or Terraform definition.

#### 2.2 Managed Instance Group autoscaler flapping and resizing loops (`mig-autoscaler-flapping`)

- **Severity**: `major`
- **Command**: `gcloud compute instance-groups managed describe $MIG --region=$REGION --format=json`
- **Condition**: MIG autoscaler repeatedly scales instances up and down within 15 minutes due to contradictory target metric thresholds.
- **Do NOT flag**: GKE cluster autoscaler managed node pools (`k8s-` or `gke-` prefix) undergoing standard pod-driven scale events.
- **Remediation**: Adjust autoscaling cool-down period and utilization targets in MIG specification.

#### 2.3 Compute Engine Ops Agent guest telemetry and health check failures (`ops-agent-guest-health`)

- **Severity**: `major`
- **Command**: `gcloud compute instances describe $VM --zone=$ZONE --format=json`
- **Condition**: Standalone production VM instance lacks active Google Cloud Ops Agent telemetry reporting or guest health check failures are present.
- **Do NOT flag**: GKE node instances, short-lived ephemeral batch VMs, or non-production test instances explicitly labeled for dev/test.
- **Remediation**: Install or restart Google Cloud Ops Agent service on target VM instance.

#### 2.4 Sole-tenant node group reservation headroom exhaustion (`sole-tenant-headroom`)

- **Severity**: `minor`
- **Command**: `gcloud compute sole-tenancy node-groups list --format=json`
- **Condition**: Sole-tenant node group utilization exceeds 90% allocated vCPU/memory capacity without failover host headroom.
- **Do NOT flag**: Node groups with active autoscaling enabled or planned maintenance windows.
- **Remediation**: Add capacity or expand sole-tenant node group reservation.

#### 2.5 Orphaned Persistent Disk snapshots from deleted source disks (`orphaned-snapshots`)

- **Severity**: `minor`
- **Command**: `gcloud compute snapshots list --format=json`
- **Condition**: Snapshot references source disk that has been deleted > 90 days ago and is not retained by any active backup policy.
- **Do NOT flag**: Snapshots retained under explicit long-term legal hold or active compliance backup schedules.
- **Remediation**: Clean up obsolete orphaned snapshot via `kind: gcloud`.

### 3. Generate remediation artifacts

For promoted findings requiring `kind: manifest` remediation, write the updated Terraform or manifest file to `remediation.path` resolved within the `workspace` GitOps repository:

- Discover the target configuration from existing repository paths (e.g., `terraform/modules/compute/vm.tf`).
- Never invent phantom paths or write manifests to directories outside the reconciled GitOps hierarchy.

### 4. Emit findings.json

Write the whole document to `findings_path` in one shot, with `audit: "gce-compute-fleet-audit"`, `scope.clusters` listing every target you queried — each carrying the `checks_run` list §1 required and, where §1 recorded them, that target's `checks_not_applicable` entries and `limitations` string — and `scope.skipped` listing only the targets you could not read.

`command` in `checks_run` is the literal inspection command executed, and anything under eight characters is rejected.

Every finding must conform to the full findings schema:

```json
{
  "audit": "gce-compute-fleet-audit",
  "scope": {
    "clusters": [
      {
        "name": "project-proj-1",
        "location": "global",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "gce-startup-script-status",
            "command": "gcloud compute instances get-serial-port-output vm-1 --zone=us-central1-a --port=1 --project=proj-1"
          }
        ]
      }
    ],
    "skipped": []
  },
  "findings": [
    {
      "check": "gce-startup-script-status",
      "severity": "critical",
      "title": "Startup script failure on standalone instance vm-1",
      "cluster": "project-proj-1",
      "namespace": "",
      "object": "ComputeInstance/vm-1",
      "impact": "Instance vm-1 failed initialization and is unable to serve production traffic.",
      "evidence": {
        "command": "gcloud compute instances get-serial-port-output vm-1 --zone=us-central1-a --port=1 --project=proj-1",
        "excerpt": "startup-script exit status 1"
      },
      "recommendation": {
        "action": "Fix failing package dependencies in instance startup-script metadata.",
        "rationale": "Prevents boot failure and restores automated instance recovery.",
        "risk": "Requires instance reboot to apply updated startup script."
      },
      "remediation": {
        "kind": "gcloud",
        "path": "",
        "note": "gcloud compute instances reset vm-1 --zone=us-central1-a --project=proj-1"
      }
    }
  ]
}
```

### 5. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit gce-compute-fleet-audit   --findings-file /opt/data/scratch/findings_gce-compute-fleet-audit.json
# -> {"status":"CLEAN"|"HELD"|"OPENED"|"UPDATED","issue_url":...,"new":n,"resolved":m,
#     "prs_opened":[...],"prs_closed":[...],"partial":false,"coverage_gaps":[],
#     "silent_ok":true}
```

- On a **scheduled** run, `silent_ok: true` -> your final response is exactly `[SILENT]`.
- **An on-demand run is never silent.** If a person dispatched this job, report the outcome and the ledger URL whatever `silent_ok` says.
- Repo writers can trigger remediation by commenting `/remediate <finding-id>` or `/remediate all` on the ledger issue.

---

## Red Lines

- **Read-only audit.** Never terminate Compute Engine instances, delete Persistent Disks, or modify live firewall rules.
- **No hand-written issues or PRs.** `audit_report.py` owns the entire git/GitHub write path.
- **Never print raw credentials.** Secret tokens, certificates, private keys, or credentials in serial port output must never reach an excerpt.
- **No unstable finding identity.** Name the durable resource identifier (`ComputeInstance/<name>`, `ManagedInstanceGroup/<name>`), never an ephemeral instance ID.
- **Never emit a manifest that directly deletes a VM or disk.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
- **Never export internal VM secrets or private keys in issue bodies.**
