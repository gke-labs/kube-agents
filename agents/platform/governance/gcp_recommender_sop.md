# SOP: GCP Recommender & Cloud Notifications Ingest (Daily Governance)

**Purpose:** Ingest machine-learning insights and active recommendations from the GCP Recommender API across all fleet projects (`GCP_PROJECT_ID` and `MONITORED_PROJECT_IDS`), focused on GKE admission webhook latency and container security diagnosis advisories (`google.container.DiagnosisRecommender`). Cross-cutting recommenders for cost (`google.compute.*`), IAM least privilege (`google.iam.policy.*`), and GKE cluster upgrades (`google.container.*.UPGRADE`) are intentionally excluded from this standalone stream to avoid duplicate ledger issues and competing remediation PRs with `fleet_wide_cost_analysis_sop.md`, `compliance_audit_sop.md`, and `security_patch_orchestrator_sop.md`. Output is this stream's single GitHub ledger issue, rewritten in place on every run, plus narrow remediation Pull Requests for promoted findings.

**Cron:** id `gcp-recommender-audit`, schedule `10 8 * * *` (daily 08:10 UTC).

**Data sources:** `gcloud recommender recommendations ...` across target fleet projects (`GCP_PROJECT_ID` and `MONITORED_PROJECT_IDS`).

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit gcp-recommender-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"/opt/data/gitops/gcp-recommender-audit/org__repo", "findings_path":"/opt/data/scratch/findings_gcp-recommender-audit.json", "pending_remediation_requests":["<id>",...]}`.

### 1. Enumerate the target fleet

```bash
gcloud projects list --format=json
```

- Target every configured fleet project. Record `{name, location, project, checks_run}` into `scope.clusters`.
- **`checks_run` is mandatory on every scope entry:** Each entry is an object `{"check": "<slug>", "command": "<literal command>"}` naming the exact inspection command executed on that target.
- A project or target you cannot reach goes in `scope.skipped` with a reason string. If a target is partially readable, record the refusal in its `limitations` string. Declare structurally inapplicable checks in `checks_not_applicable`.

### 2. Diagnostic checks roster

#### 2.1 GKE admission webhook latency and readiness warnings (`gke-webhook-readiness`)

- **Severity**: `major`
- **Impact**: Failing admission webhooks degrade control plane reliability and block pod scheduling.
- **Command**: `gcloud recommender recommendations list --recommender=google.container.DiagnosisRecommender --location=$LOCATION --project=$PROJECT --format=json`
- **Condition**: GKE cluster diagnosis identifies failing or slow mutating/validating admission webhooks.
- **Do NOT flag**: Webhooks explicitly configured with `failurePolicy: Ignore` in test namespaces.
- **Remediation**: Optimize webhook latency or update webhook failurePolicy via `kind: manual`.

#### 2.2 GKE container diagnosis and security posture alerts (`gke-security-posture-cve`)

- **Severity**: `major`
- **Impact**: Unpatched container vulnerabilities and weak security contexts expose workloads to exploit.
- **Command**: `gcloud recommender recommendations list --recommender=google.container.DiagnosisRecommender --location=$LOCATION --project=$PROJECT --format=json`
- **Condition**: GKE cluster diagnosis identifies critical workload security configuration defects.
- **Do NOT flag**: Low-severity CVEs without available patch fixes in current container base images.
- **Remediation**: Remediate container security context settings in workload manifests via `kind: manual`.

### 3. Generate remediation artifacts

Write the manifest for every id in `pending_remediation_requests` from Step 0 whose finding still reproduces. A human has already asked for that fix via `/remediate` on the ledger; without the file the promotion fails.

For promoted findings requiring `kind: manifest` remediation, write the updated Terraform or manifest file to `remediation.path` resolved within the `workspace` GitOps repository:

- Discover the target configuration from existing repository paths (e.g., `terraform/modules/iam/bindings.tf`).
- Never invent phantom paths or write manifests to directories outside the reconciled GitOps hierarchy.

### 4. Emit findings.json

Write the whole document to `findings_path` in one shot, with `audit: "gcp-recommender-audit"`, `scope.clusters` listing every project you queried — each carrying the `checks_run` list §1 required and, where §1 recorded them, that target's `checks_not_applicable` entries and `limitations` string — and `scope.skipped` listing only the targets you could not read.

`command` in `checks_run` is the literal inspection command executed, and anything under eight characters is rejected.

Every finding must conform to the full findings schema:

```json
{
  "audit": "gcp-recommender-audit",
  "scope": {
    "clusters": [
      {
        "name": "gcp-fleet-prod",
        "location": "global",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "iam-least-privilege",
            "command": "gcloud recommender recommendations list --recommender=google.iam.policy.Recommender --location=global --project=proj-1 --format=json"
          }
        ]
      }
    ],
    "skipped": []
  },
  "findings": [
    {
      "check": "iam-least-privilege",
      "severity": "major",
      "title": "Over-privileged IAM role binding on app-sa",
      "cluster": "gcp-fleet-prod",
      "namespace": "default",
      "object": "IAMRole/app-sa",
      "impact": "Service account has roles/editor which exceeds required permissions.",
      "evidence": {
        "command": "gcloud recommender recommendations describe rec-123 --location=global --project=proj-1 --format=json",
        "excerpt": "Replace roles/editor with roles/storage.objectViewer"
      },
      "recommendation": {
        "action": "Downscope app-sa to specific required IAM roles.",
        "rationale": "Implements least-privilege security posture based on 90-day API call history.",
        "risk": "Ensure newly required API methods are included in target roles."
      },
      "remediation": {
        "kind": "manual",
        "path": ""
      }
    }
  ]
}
```

### 5. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit gcp-recommender-audit   --findings-file /opt/data/scratch/findings_gcp-recommender-audit.json
# -> {"status":"CLEAN"|"OPENED"|"UPDATED","issue_url":...,"new":n,"resolved":m,
#     "prs_opened":[...],"prs_closed":[...],"partial":false,"coverage_gaps":[],
#     "silent_ok":true}
```

- On a **scheduled** run, `silent_ok: true` -> your final response is exactly `[SILENT]`.
- **An on-demand run is never silent.** If a person dispatched this job, report the outcome and the ledger URL whatever `silent_ok` says.
- Repo writers can trigger remediation by commenting `/remediate <finding-id>` or `/remediate all` on the ledger issue.

---

## Red Lines

- **Read-only audit.** Never mutate live IAM policies, revoke permissions, or delete cloud resources directly.
- **No hand-written issues or PRs.** `audit_report.py` owns the entire git/GitHub write path.
- **Never print raw credentials.** Secret tokens, certificates, private keys, and authorization headers must never reach an excerpt.
- **No unstable finding identity.** Name the durable resource identifier (`IAMRole/<name>`, `Recommender/<id>`), never an ephemeral execution timestamp.
- **Never emit a manifest that directly deletes an active resource.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
