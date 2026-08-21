---
title: CI pool project prerequisites
description: Prerequisites and infrastructure setup required to onboard a GCP project into the Prow Boskos evaluation pool.
sidebar:
  order: 7
---

Prow CI smoke tests lease dedicated GCP sandbox projects from a [Boskos](https://github.com/kubernetes-sigs/boskos) resource pool (`kube-agents-evals-project`) to isolate concurrent evaluation runs.

Every GCP project registered in the Boskos pool must be provisioned with the prerequisites below before registering it in `oss-test-infra`.

## 1. Enabled GCP APIs

The project must have the following Google Cloud APIs enabled:

```bash
gcloud services enable \
  container.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  iam.googleapis.com \
  --project="${PROJECT_ID}"
```

## 2. Host GKE Cluster (`platform-agent-host`)

A long-lived GKE cluster hosting the Platform Agent and evaluation infrastructure:

- **Cluster Name**: `platform-agent-host`
- **Location**: `us-central1` (regional or zonal, matching `hack/ci-env.sh`)
- **Database Encryption**: CMEK encryption enabled (`ALL_OBJECTS_ENCRYPTION_ENABLED`), required by `hack/ci-deploy.sh` when `ALLOW_UNENCRYPTED_SECRETS=false`.

The cluster can be provisioned using the Terraform modules in `terraform/examples/full-install`:

```bash
cd terraform/examples/full-install
terraform apply -var="project_id=${PROJECT_ID}"
```

## 3. Service accounts and IAM

- **Workload Identity**: Google Service Account `kubeagents-platform-gsa@${PROJECT_ID}.iam.gserviceaccount.com` bound to KSA `kubeagents-platform-ksa` in namespace `kubeagents-system`:
  ```bash
  gcloud iam service-accounts add-iam-policy-binding \
    kubeagents-platform-gsa@${PROJECT_ID}.iam.gserviceaccount.com \
    --role="roles/iam.workloadIdentityUser" \
    --member="serviceAccount:${PROJECT_ID}.svc.id.goog[kubeagents-system/kubeagents-platform-ksa]"
  ```
- **Cloud Build Service Account** (`${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com`):
  - `roles/artifactregistry.writer` in `${PROJECT_ID}` (to push PR build images).
  - `roles/artifactregistry.reader` in `kube-agents-prow` (to pull the warm `:latest` cache image).
- **GKE Node Service Account**:
  - `roles/artifactregistry.reader` in `${PROJECT_ID}` to pull operator and agent images.

## 4. Artifact Registry repository and cleanup policy

Each pool project maintains a regional Artifact Registry repository for PR images:

- **Repository**: `kube-agents`
- **Location**: `us-central1` (`us-central1-docker.pkg.dev/${PROJECT_ID}/kube-agents`)
- **Format**: Docker standard repository

### Cleanup policy

Configure a lifecycle policy to prevent unconstrained storage growth from presubmit builds:

```json
[
  {
    "name": "delete-pr-images-older-than-14-days",
    "action": { "type": "Delete" },
    "condition": {
      "tagState": "tagged",
      "tagPrefixes": ["pr-"],
      "olderThan": "14d"
    }
  },
  {
    "name": "delete-untagged-older-than-1-day",
    "action": { "type": "Delete" },
    "condition": {
      "tagState": "untagged",
      "olderThan": "1d"
    }
  },
  {
    "name": "keep-latest",
    "action": { "type": "Keep" },
    "condition": {
      "tagState": "tagged",
      "tagPrefixes": ["latest"]
    }
  }
]
```

Apply the policy:

```bash
gcloud artifacts repositories set-cleanup-policies kube-agents \
  --location=us-central1 \
  --project="${PROJECT_ID}" \
  --policy=policy.json
```

## 5. Boskos pool registration

Once the GCP project is provisioned with the prerequisites above, register the project ID under the `kube-agents-evals-project` resource type in the Prow Boskos deployment configuration:

```yaml
- type: kube-agents-evals-project
  state: free
  names:
    - kube-agents-evals
    - kube-agents-evals-2
    - <NEW_PROJECT_ID>
```

> **Important:** The Boskos janitor must be disabled for `kube-agents-evals-project` so that the long-lived `platform-agent-host` cluster and pre-warmed state are preserved across leases.
