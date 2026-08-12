# SOP: FinOps and Cloud Resource Waste Audit (Daily Governance)

**Purpose:** Sweep all managed GCP projects for high-volume un-filtered Cloud Logging sinks and idle Cloud Load Balancing backend services. The question this audit answers for a platform admin is: _which cloud assets are generating ongoing waste without serving traffic, and where can resource footprint be safely rightsized?_ Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests carrying Terraform or manifest fixes for the findings that get promoted.

**Cron:** id `finops-cloud-waste-audit`, schedule `30 8 * * *` (daily 08:30 UTC).

**Data sources:** `gcloud compute backend-services ...` and `gcloud logging sinks ...` across all managed fleet projects (`GCP_PROJECT_ID` and `MONITORED_PROJECT_IDS`).

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit finops-cloud-waste-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/finops-cloud-waste-audit/org__repo", "findings_path":"/opt/data/scratch/findings_finops-cloud-waste-audit.json"}`.

### 1. Enumerate the target fleet

```bash
gcloud projects list --format=json
```

- Target every configured fleet project and GKE cluster. Record `{name, location, project, checks_run}` into `scope.clusters`.
- **`checks_run` is mandatory on every scope entry:** Each entry is an object `{"check": "<slug>", "command": "<literal command>"}` naming the exact inspection command executed on that target.
- A project or target you cannot reach goes in `scope.skipped` with a reason string. If a target is partially readable, record the refusal in its `limitations` string. Declare structurally inapplicable checks in `checks_not_applicable`.

### 2. Diagnostic checks roster

#### 2.1 Unfiltered high-throughput Cloud Logging export sinks (`cloud-logging-cost-runaway`)

- **Severity**: `minor`
- **Command**: `gcloud logging sinks list --project=$PROJECT --format=json`
- **Condition**: Log sink exports unfiltered noisy container stdout/stderr logs directly to BigQuery or Cloud Storage without exclusion filters.
- **Do NOT flag**: Compliance audit log sinks or sinks with existing explicit exclusion filters.
- **Remediation**: Add exclusion filters for health check and debug log streams in Terraform logging sink definition.

#### 2.2 Idle Cloud Load Balancing backend services (`idle-backend-services`)

- **Severity**: `minor`
- **Command**: `gcloud compute backend-services list --project=$PROJECT --format=json`
- **Condition**: Backend service has no attached backends or receives 0 requests over a sustained billing period.
- **Do NOT flag**: Backend services associated with active Kubernetes GKE Ingress/Gateway resources undergoing rolling deployments.
- **Remediation**: Remove unused backend service in Terraform configuration.

### 3. Generate remediation artifacts

For promoted findings requiring `kind: manifest` remediation, write the updated Terraform or manifest file to `remediation.path` resolved within the `workspace` GitOps repository:

- Discover the target configuration from existing repository paths (e.g., `terraform/modules/networking/sinks.tf`).
- Never invent phantom paths or write manifests to directories outside the reconciled GitOps hierarchy.

### 4. Emit findings.json

Write the whole document to `findings_path` in one shot, with `audit: "finops-cloud-waste-audit"`, `scope.clusters` listing every target you queried — each carrying the `checks_run` list §1 required and, where §1 recorded them, that target's `checks_not_applicable` entries and `limitations` string — and `scope.skipped` listing only the targets you could not read.

`command` in `checks_run` is the literal inspection command executed, and anything under eight characters is rejected.

Every finding must conform to the full findings schema:

```json
{
  "audit": "finops-cloud-waste-audit",
  "scope": {
    "clusters": [
      {
        "name": "project/proj-1",
        "location": "global",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "idle-backend-services",
            "command": "gcloud compute backend-services list --project=proj-1 --format=json"
          }
        ]
      }
    ],
    "skipped": []
  },
  "findings": [
    {
      "check": "idle-backend-services",
      "severity": "minor",
      "title": "Backend service svc-unused has no configured backends",
      "cluster": "project/proj-1",
      "namespace": "",
      "object": "BackendService/svc-unused",
      "impact": "Unused backend service adds configuration clutter.",
      "evidence": {
        "command": "gcloud compute backend-services list --project=proj-1 --format=json",
        "excerpt": "name: svc-unused, backends: []"
      },
      "recommendation": {
        "action": "Delete unused backend service svc-unused via gcloud or Terraform.",
        "rationale": "Backend service has no instance groups or NEGs attached.",
        "risk": "Verify no URL maps reference this backend service."
      },
      "remediation": {
        "kind": "gcloud",
        "path": ""
      }
    }
  ]
}
```

### 5. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit finops-cloud-waste-audit   --findings-file /opt/data/scratch/findings_finops-cloud-waste-audit.json
# -> {"status":"CLEAN"|"OPENED"|"UPDATED","issue_url":...,"new":n,"resolved":m,
#     "prs_opened":[...],"prs_closed":[...],"partial":false,"coverage_gaps":[],
#     "silent_ok":true}
```

- On a **scheduled** run, `silent_ok: true` -> your final response is exactly `[SILENT]`.
- **An on-demand run is never silent.** If a person dispatched this job, report the outcome and the ledger URL whatever `silent_ok` says.
- Repo writers can trigger remediation by commenting `/remediate <finding-id>` or `/remediate all` on the ledger issue.

---

## Red Lines

- **Read-only audit.** Never release static IPs, delete PersistentVolumes, or delete backend services directly.
- **No hand-written issues or PRs.** `audit_report.py` owns the entire git/GitHub write path.
- **Never print raw credentials.** Secret tokens, certificates, and private keys must never reach an excerpt.
- **No unstable finding identity.** Name the durable resource identifier (`Address/<name>`, `PersistentVolume/<name>`), never an ephemeral timestamp.
- **Never emit a manifest that directly deletes a resource.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
