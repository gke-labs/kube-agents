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
| `kube-agents-evals-3` | `gke-agentic/kube-agents-evals-3-infra` |

The repository is kept private: it is throwaway state a bot rewrites on every run. [`examples/gitops-repo`](https://github.com/gke-labs/kube-agents/tree/main/examples/gitops-repo) is the layout an audit expects to find, not a required seed — the current pool repositories carry only a LICENSE and a README, because an audit works against an empty tree and a `remediation.path` that does not exist degrades to a manual finding rather than failing the run.

> **The pool's three projects are all provisioned, as of 2026-08-24.** `kube-agents-evals-3` was added to the Boskos pool on 2026-08-21 with only its GCP half done, so every presubmit that leased it stopped at `gitops_repo_for_project()`'s unmapped-project refusal. That is closed. Verified in all three projects:
>
> 1. The private GitOps repository exists and is mapped in the table above.
> 2. App `4675512` resolves to all three pool repositories, still `repository_selection: selected`, with `contents: write`, `issues: write`, `pull_requests: write`, `metadata: read`.
> 3. `terraform/examples/ci-pool-minter` is applied per project: each carries `kubeagents-github-minter-gsa@<project>.iam.gserviceaccount.com` and the key ring `github-token-minter-keyring` with key `github-token-minter-key` in `us-central1`, and the App PEM is imported — `gcloud kms keys versions list` shows exactly one `ENABLED` `RSA_SIGN_PKCS1_2048_SHA256` version in each.
>
> One step remains and it is not in this repository: `EVAL_GITHUB_APP_ID=4675512` is still unset in the Prow job environment, so `hack/ci-deploy.sh` renders `githubMinter.enabled=false` for every lease and an audit stream still fails at `audit_report.py start` for want of a token. Setting it — together with mounting the ledger-read token the bench verifiers need — is [`GoogleCloudPlatform/oss-test-infra#2661`](https://github.com/GoogleCloudPlatform/oss-test-infra/pull/2661), open and unmerged. Because the switch is pool-wide, it is only safe to set once the manual half is true of every project in the pool, which it now is.

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
terraform init
terraform workspace new "${PROJECT_ID}"        # or a per-project backend prefix
cp terraform.tfvars.example terraform.tfvars   # set project_id and gitops_repo
terraform plan                                 # must be create-only
terraform apply
terraform output manual_steps
```

**Each project needs its own state.** `project_id` is force-new on the minter's GSA, so re-pointing this composition at a second pool project and applying over the first project's state destroys the first project's minter rather than adding a second — and the KMS key ring cannot simply be re-created afterwards. The workspace above (or a `backend_override.tf` prefix, as in `terraform/examples/full-install`) is what keeps them apart; the create-only plan is what catches it if they are not. The composition's README covers both and the recovery.

That provisions the minter GSA, its Workload Identity binding to `kubeagents-system/kubeagents-github-minter`, and the import-only KMS signing key. The chart renders the Kubernetes half and derives both `githubMinter.gsaName` and `githubMinter.allowedServiceAccount` from `platformAgent.harness.projectId`, so the minty rule comes out scoped to this project's repository and keyed on this project's `kubeagents-platform-gsa` with no per-project values.

Two steps have no Terraform equivalent and must be done by a human with the corresponding rights:

1. **Install the GitHub App on the repository** (org-admin on `gke-agentic`, plus App-manager rights). Grant `contents: write`, `pull_requests: write`, and `issues: write`, on that one repository. **Done for all three current pool projects** — see the App below; a fourth project means adding its repository to the same installation.
2. **Import the App's private key** into the project's KMS signing key with the Minty CLI. The PEM must never enter Terraform state, so the key is created import-only and empty; the command is in the [composition's README](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/ci-pool-minter). Confirm version 1 reaches `ENABLED`. This one is per project — the same PEM, imported into each project's own key.

The pool is served by a single App, `kube-agents-evals-token-minter`, **App ID `4675512`**, installed on `gke-agentic/kube-agents-evals-infra`, `gke-agentic/kube-agents-evals-2-infra`, and `gke-agentic/kube-agents-evals-3-infra`:

```bash
gh api /orgs/gke-agentic/installations \
  --jq '.installations[] | select(.app_id==4675512) |
        {app_slug, repository_selection, permissions}'
```

It is a dedicated App rather than the organisation's existing all-repositories minter, and that is a deliberate cost. Reusing the staging App would have copied its signing key into every pool project's KMS, added unreviewed presubmit code to the callers of an identity that otherwise only serves merged code, and coupled rotation — an eval incident forcing a key rotation would have taken staging and autopush with it.

Only then set `EVAL_GITHUB_APP_ID=4675512` in the Prow job environment. The value is the same for every pool project. `hack/ci-deploy.sh` keeps `githubMinter.enabled=false` until it is set, because the minter Deployment is part of the release `helm --wait` gates on: enabling it before the key import fails every presubmit instead of degrading quietly.

### 5.3 What actually bounds where a run can write

The GitHub App's installation list, and nothing else. A presubmit runs the pull request's code, so a pull request can in principle edit the resolution table or the minty rule ConfigMap — but it cannot make the App mint a token for a repository the App is not installed on. Keep the installation scoped to the pool's GitOps repositories, and treat any change to that list as the security review.

## 6. The seeded dirty fleet

Six of the evaluation scenarios assert on defects that were planted on purpose — a crashlooping `payments-api`, a workload with no PodDisruptionBudget, an idle node pool, a control plane held a minor behind, a cluster missing master authorized networks. Those fixtures are not provisioned per run. They live on three small standing GKE clusters, `seeded-a`, `seeded-b` and `seeded-c`, and **each pool project needs its own trio**: Boskos leases at random, so a project without them is a project where every fleet check reports `status: "error"` and `VerificationCoverage` drops below 1.0 for that run.

Apply [`bench/tf/fleet`](https://github.com/gke-labs/kube-agents/tree/main/bench/tf/fleet) once per pool project, each with its own remote state:

```bash
cd bench/tf/fleet
tofu init -reconfigure \
          -backend-config="bucket=${PROJECT_ID}-tf-state" \
          -backend-config="prefix=seeded-fleet"
tofu apply -var="project_id=${PROJECT_ID}"
```

The fleet owner creates `gs://${PROJECT_ID}-tf-state` once per project. All three pool projects are applied as of 2026-08-24, and `hack/fleet-kubeconfigs.sh` run against each of them confirms all seven fixture roles — `7 role(s) written, 0 on clusters that could not be resolved or reached, 0 whose fixtures were not present`. That command is the check to re-run before believing this paragraph; a project it reports anything else for is a project the stack needs re-applying in.

Nothing outside the fleet's own catalog addresses these clusters by name. `hack/fleet-kubeconfigs.sh` discovers them in the leased project by the labels the stack applies (`environment=seeded`, `managed-by=kube-agents-seeded-fleet`), so a project may use a different `cluster_prefix` or region without any scenario changing. The one other sanctioned consumer discovers by the same labels: `hack/ci-eval-pr.sh` §3b reuses the slot-c cluster as the presubmit's log-fixture subject instead of provisioning a per-run cluster, mutating nothing in it — the fleet's catalog (`bench/tf/fleet/fixtures.json`) records the exception.

A half-finished apply is the case to watch for. The stack's Kubernetes provider is configured against a cluster the same stack creates, so an apply that fails after the clusters and before the fixtures leaves a trio that carries the labels, answers every API call, and holds none of the planted objects. The runner therefore reads every object in the role's `probes` list before it publishes that role — the objects themselves, not just their namespaces, since four of the seven roles are cluster-scoped — and a role it cannot confirm reports `status: "error"` naming the role and the project, the same answer as no fleet at all, rather than a check that blames the agent for a fixture nobody planted. `tofu apply` again until it is clean.

### 6.1 A read-only credential for the checks

An eval run reads the fleet to confirm its fixtures survived; it has no business being able to change them, and a safeguard is worth less when the credential that checks it could also have caused what it is checking for. **This is not true today.** The Prow identity holds `roles/container.admin` in every eval project, and there are no in-cluster RoleBindings to narrow — GKE's IAM webhook is the whole authorization path.

The seam exists: the fleet stack provisions `seeded-fleet-reader@${PROJECT_ID}.iam.gserviceaccount.com` with `roles/container.viewer` and nothing else. To use it, per project:

1. Add the Prow identity to `fleet_reader_token_creators` and re-apply the stack, which binds it `roles/iam.serviceAccountTokenCreator` on that account alone.
2. Export `FLEET_READONLY_SA=seeded-fleet-reader@${PROJECT_ID}.iam.gserviceaccount.com` in the Prow job. Unset, `hack/fleet-kubeconfigs.sh` warns on every run and the kubeconfigs carry the runner's own identity.

## 7. Boskos pool registration

Once the GCP project is provisioned with the prerequisites above, register the project ID under the `kube-agents-evals-project` resource type in the Prow Boskos deployment configuration:

```yaml
- type: kube-agents-evals-project
  state: free
  names:
    - kube-agents-evals
    - kube-agents-evals-2
    - kube-agents-evals-3
    - <NEW_PROJECT_ID>
```

This roster does not live in `oss-test-infra` with the rest of the Prow config — it is in `gke-internal/test-infra`, under `deployments/gke-agentic-tooling-team/boskos`. That split is why registration and onboarding can drift apart: this page is the only thing joining the two repositories, and nothing enforces the order between them.

**Register the project last.** Everything above — the APIs, the cluster, the registry, the GitOps repository, the App installation, the key import, the `gitops_repo_for_project()` row, the seeded fleet — is a prerequisite of the entry in this list, not a follow-up to it. A project that becomes leasable before it is onboarded takes a share of every presubmit and fails it, which is how `kube-agents-evals-3` broke the smoke test for every open pull request on 2026-08-21.

> **Important:** The Boskos janitor must be disabled for `kube-agents-evals-project` so that the long-lived `platform-agent-host` cluster and pre-warmed state are preserved across leases.
