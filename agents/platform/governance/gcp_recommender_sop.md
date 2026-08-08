# SOP: GCP Recommender & Cloud Notifications Audit (Fleet Governance)

**Purpose:** A read-only, multi-project sweep ingesting Google Cloud Recommender API insights, GKE Security Posture CVEs, cluster condition notifications, and available GKE version upgrades across all managed fleet projects (`GCP_PROJECT_ID` and `MONITORED_PROJECT_IDS`). Translates cloud-level recommendations into reproducible findings in a dedicated ledger issue with narrow GitOps/Terraform remediation Pull Requests. Cron id `gcp-recommender-audit`, schedule `10 8 * * *` (daily 08:10 UTC).

**Data sources:** `gcloud recommender recommendations list`, `gcloud container get-server-config`, `gcloud container clusters list|describe`, and `kubectl` read verbs.

---

## Execution Checklist

### 0. Open the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py start --audit gcp-recommender-audit
```

Returns `{"issue": <int|null>, "repo":"org/repo", "workspace":"...", "findings_path":"/opt/data/scratch/findings_gcp-recommender-audit.json"}`.

### 1. Enumerate target fleet scope

```bash
gcloud container clusters list --format=json
```

Record all running clusters into `scope.clusters` with mandatory `checks_run` objects.

### 2. Execute cloud recommender queries

Execute `./skills/gcp-recommender-ingest/scripts/recommender_ingest.py` across all monitored projects.

---

## Checks Roster

#### `iam-least-privilege`

- **What it checks:** Unused or over-permissioned IAM role bindings identified by `google.iam.policy.Recommender`.
- **Severity:** `major`
- **Command:** `gcloud recommender recommendations list --recommender=google.iam.policy.Recommender --project=$PROJECT --location=global --format=json`
- **Remediation:** Remove or replace over-broad IAM role bindings with recommended least-privilege roles in IAM Terraform configuration.

#### `gke-upgrade-available`

- **What it checks:** Control plane or node pool upgrades available from GKE release channels or `google.container.UpgradeRecommendation`.
- **Severity:** `major`
- **Command:** `gcloud container get-server-config --project=$PROJECT --zone=$ZONE --format=json`
- **Remediation:** Update target GKE version in cluster manifest or Terraform module to the recommended stable release.

#### `idle-compute-instance`

- **What it checks:** Idle or under-utilized VM instances identified by `google.compute.instance.IdleResourceRecommendation`.
- **Severity:** `major`
- **Command:** `gcloud recommender recommendations list --recommender=google.compute.instance.IdleResourceRecommendation --project=$PROJECT --location=$ZONE --format=json`
- **Remediation:** Stop or delete idle instances or scale down static node pools in GitOps IaC (`kind: manual` or `kind: gcloud`).

#### `unattached-persistent-disk`

- **What it checks:** Orphaned Persistent Disks flagged by `google.compute.disk.IdleResourceRecommendation`.
- **Severity:** `major`
- **Command:** `gcloud recommender recommendations list --recommender=google.compute.disk.IdleResourceRecommendation --project=$PROJECT --location=$ZONE --format=json`
- **Remediation:** Declare disk snapshot and cleanup via `kind: manual` or `kind: gcloud`.

#### `gke-webhook-readiness`

- **What it checks:** Admission webhooks (`ValidatingWebhookConfiguration`, `MutatingWebhookConfiguration`) with timeouts `> 10s`, unreachable endpoints, or deprecated API versions that block cluster upgrades.
- **Severity:** `major`
- **Command:** `kubectl --context=$CLUSTER get validatingwebhookconfigurations,mutatingwebhookconfigurations -o json`
- **Remediation:** Update webhook timeout to `5s` or remove stale admission webhook configurations in manifest.

#### `gke-security-posture-cve`

- **What it checks:** High/Critical severity CVE container vulnerabilities flagged by Container Analysis or GKE Security Posture Dashboard.
- **Severity:** `major`
- **Command:** `kubectl --context=$CLUSTER get pods -A -o jsonpath='{..image}'`
- **Remediation:** Bump base container image tag to patched release.

#### `idle-ip-address`

- **What it checks:** Unused external static IP reservations flagged by `google.compute.address.IdleResourceRecommendation`.
- **Severity:** `minor`
- **Command:** `gcloud recommender recommendations list --recommender=google.compute.address.IdleResourceRecommendation --project=$PROJECT --location=$ZONE --format=json`
- **Remediation:** Release unused static IP reservation via `kind: manual` or `kind: gcloud`.

#### `cost-optimization-rightsizing`

- **What it checks:** Over-provisioned CPU and memory requests identified by `google.container.CostOptimizationRecommendation`.
- **Severity:** `major`
- **Command:** `gcloud recommender recommendations list --recommender=google.container.CostOptimizationRecommendation --project=$PROJECT --location=$ZONE --format=json`
- **Remediation:** Right-size workload container CPU/memory requests in deployment manifest.

---

### 3. Emit findings.json

Write the schema exactly as validated by the helper to `findings_path`:

```json
{
  "audit": "gcp-recommender-audit",
  "scope": {
    "clusters": [
      {
        "name": "cluster-1",
        "location": "us-central1-a",
        "project": "proj-1",
        "checks_run": [
          {
            "check": "iam-least-privilege",
            "command": "gcloud recommender recommendations list ..."
          }
        ]
      }
    ],
    "skipped": []
  },
  "findings": []
}
```

### 4. Close the audit run

```bash
./skills/fleet-audit/scripts/audit_report.py finish --audit gcp-recommender-audit \
  --findings-file /opt/data/scratch/findings_gcp-recommender-audit.json
```

---

## Red Lines

- **Never mutate live GCP infrastructure during this run.** This audit is strictly read-only.
- **Never emit a manifest that directly deletes a Persistent Disk, IP address, VM instance, or namespace.** Deletion remediations are `kind: manual` or `kind: gcloud` only.
- **Never export cloud credentials or auth tokens to finding evidence excerpts or issue bodies.**
