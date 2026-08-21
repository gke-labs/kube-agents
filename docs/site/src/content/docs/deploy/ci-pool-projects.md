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
  cloudkms.googleapis.com \
  --project="${PROJECT_ID}"
```

`cloudkms.googleapis.com` is for the GitHub token minter's signing key (section 5); the `ci-pool-minter` composition enables it too, so it is listed here only so a project provisioned by hand does not miss it.

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

- **Workload Identity**: Google Service Account `kubeagents-platform-gsa@${PROJECT_ID}.iam.gserviceaccount.com` bound to KSA `kubeagents-platform-agent` in namespace `kubeagents-system` (the KSA name `hack/ci-deploy.sh` and `k8s-operator/scripts/common.sh` both use):
  ```bash
  gcloud iam service-accounts add-iam-policy-binding \
    kubeagents-platform-gsa@${PROJECT_ID}.iam.gserviceaccount.com \
    --role="roles/iam.workloadIdentityUser" \
    --member="serviceAccount:${PROJECT_ID}.svc.id.goog[kubeagents-system/kubeagents-platform-agent]"
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

## 5. GitOps repository and GitHub token minter

The evaluation scenarios that exercise the GitOps workflow — the six fleet-audit streams and `rca-remediation-pr` — write to GitHub. Step 0 of a fleet-audit stream (`audit_report.py start`) mints a repository-scoped GitHub App token and clones the workspace named by the `Git Repo:` line of `/opt/data/SETTINGS.md`; `finish` rewrites a ledger issue and opens remediation pull requests.

**Every pool project needs its own private GitOps repository.** Two leases must not share a ledger issue or race on a remediation branch, and a token minted in one lease must not reach another lease's repository.

<!-- prettier-ignore -->
| Project | GitOps repository |
| --- | --- |
| `kube-agents-evals` | `gke-agentic/kube-agents-evals-infra` |
| `kube-agents-evals-2` | `gke-agentic/kube-agents-evals-2-infra` |

The repository is seeded from the layout in [`examples/gitops-repo`](https://github.com/gke-labs/kube-agents/tree/main/examples/gitops-repo) and kept private: it is throwaway state a bot rewrites on every run.

### 5.1 How CI resolves it

`hack/ci-deploy.sh` maps the leased project to its repository in `gitops_repo_for_project()` and passes the result as `--set-string platformAgent.integration.github.gitRepo=...`. The operator renders that field into the `platform-agent-settings` ConfigMap as the `Git Repo:` line.

CI supplies the value rather than relying on the chart default, and that is deliberate. A presubmit builds and deploys the pull request's own chart, operator, and agent, so a pull request that blanks `platformAgent.integration.github.gitRepo` in `values.yaml`, or breaks the CR-to-`SETTINGS.md` rendering, is exactly the regression the eval should surface as a failed scenario — which it can only do if the value the run is supposed to use comes from outside the artefacts under test. (This is a correctness argument, not the containment boundary; see 5.3.)

Adding a project is one line in `gitops_repo_for_project()` and one row in the table above. An unmapped project stops the deploy:

- **In a Prow run** (`PULL_NUMBER` or `JOB_NAME` set) the script exits non-zero and names the function to edit. It also refuses an `EVAL_GITOPS_REPO` override, because under Boskos the project is leased per run and a value pinned in the job environment would eventually point one project's run at another project's repository.
- **On a laptop** the script exits non-zero too, and prints the two ways to say where the run writes: `EVAL_GITOPS_REPO=owner/repo` for your own throwaway repository, or `EVAL_GITOPS_REPO=none` to deploy with the GitHub integration off. Neither path is a default — an empty `gitRepo` is only ever reached by asking for it.

### 5.2 The token minter

`gitRepo` only tells the agent where to clone. Writing needs a token, and the only source of one is the in-cluster [GitHub token minter](/kube-agents/deploy/token-minter/) — the agent's refresher deletes any inherited `GITHUB_TOKEN`. Provision its GCP half with the [`terraform/examples/ci-pool-minter`](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/ci-pool-minter) composition, once per pool project:

```bash
cd terraform/examples/ci-pool-minter
cp terraform.tfvars.example terraform.tfvars   # set project_id and gitops_repo
terraform init && terraform apply
terraform output manual_steps
```

That provisions the minter GSA, its Workload Identity binding to `kubeagents-system/kubeagents-github-minter`, and the import-only KMS signing key. The chart renders the Kubernetes half and derives both `githubMinter.gsaName` and `githubMinter.allowedServiceAccount` from `platformAgent.harness.projectId`, so the minty rule comes out scoped to this project's repository and keyed on this project's `kubeagents-platform-gsa` with no per-project values.

Two steps have no Terraform equivalent and must be done by a human with the corresponding rights:

1. **Install the GitHub App on the repository** (org-admin on `gke-agentic`, plus App-manager rights). Grant `contents: write`, `pull_requests: write`, and `issues: write`, on that one repository.
2. **Import the App's private key** into the project's KMS signing key with the Minty CLI. The PEM must never enter Terraform state, so the key is created import-only and empty; the command is in the [composition's README](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/ci-pool-minter). Confirm version 1 reaches `ENABLED`.

Only then set `EVAL_GITHUB_APP_ID` to the App's numeric ID in the Prow job environment. `hack/ci-deploy.sh` keeps `githubMinter.enabled=false` until it is set, because the minter Deployment is part of the release `helm --wait` gates on: enabling it before the key import fails every presubmit instead of degrading quietly.

### 5.3 What actually bounds where a run can write

The GitHub App's installation list, and nothing else. A presubmit runs the pull request's code, so a pull request can in principle edit the resolution table or the minty rule ConfigMap — but it cannot make the App mint a token for a repository the App is not installed on. Keep the installation scoped to the pool's GitOps repositories, and treat any change to that list as the security review.

## 6. Boskos pool registration

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
