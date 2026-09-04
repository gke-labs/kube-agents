---
title: CI pool project prerequisites
description: Prerequisites and infrastructure setup required to onboard a GCP project into the Prow Boskos evaluation pool.
sidebar:
  order: 7
---

Prow CI smoke tests lease dedicated GCP sandbox projects from a [Boskos](https://github.com/kubernetes-sigs/boskos) resource pool (`kube-agents-evals-project`) to isolate concurrent evaluation runs.

Every GCP project registered in the Boskos pool must be provisioned with the prerequisites below before its entry lands in the pool roster. That roster is in `gke-internal/test-infra`, not in `oss-test-infra` with the rest of the Prow config — section 8 covers the split, and section 9 covers the one thing that does live in `oss-test-infra`.

Sections 1 to 6 are what a leasable project must end up holding, and section 7 is how you check it. `scripts/provision_ci_pool_project.sh --project-id=<id>` does all of it — grouped differently from the section order, since it enables the APIs, IAM and registry together before building the host cluster. Run the script rather than the individual commands, which are here so a project provisioned by hand does not miss one. Its flags: `--pem-file=PATH` imports the App private key in section 5 (without it the key stays `PENDING_IMPORT` and the run ends amber); `--skip-host-cluster` and `--skip-fleet` skip sections 2 and 6 for a project that already has them; `--allow-unmapped` downgrades the mapping precondition below to a warning; `--app-id` overrides the App the run provisions and verifies against.

## 0. Preconditions

Everything below provisions _into_ a project that already exists and already has a billing account linked. The script checks both before it touches anything, because `gcloud services enable` against a project with no billing account fails with a message that does not mention billing. It stops on a project it cannot see, and on billing it can read and finds off; billing it _cannot_ read is a visibility limit, so it warns and continues. Which billing account, and how a project gets linked to it, is not recorded here.

The project must also be mapped in `gitops_repo_for_project()` in `hack/ci-deploy.sh`, to `gke-agentic/<project>-infra`, with the same pair in `_EXPECTED_MAPPING` in `tests/test_ci_gitops_repo.py`. That is a code change the script cannot make, so it checks for it first: an unmapped project fails every lease at that function's refusal, and section 7 would fail on it anyway. Land the mapping before provisioning.

## 1. Enabled GCP APIs

The project must have the following Google Cloud APIs enabled:

```bash
gcloud services enable \
  compute.googleapis.com \
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

`cloudkms.googleapis.com` is for the GitHub token minter's signing key (section 5); the `ci-pool-minter` composition enables it too, so it is listed here only so a project provisioned by hand does not miss it. `compute.googleapis.com` is for the seeded fleet (section 6), which declares the orphan `google_compute_disk` the cost audit looks for and so depends on Compute directly rather than only transitively through GKE.

This list and `REQUIRED_APIS` in `scripts/verify_ci_pool_project.py` must agree — the verifier fails a project for an API this block does not mention, and passes one that is missing an API only this block names.

## 2. Host GKE Cluster (`platform-agent-host`)

A long-lived GKE cluster hosting the Platform Agent and evaluation infrastructure:

- **Cluster Name**: `platform-agent-host`
- **Location**: `us-central1` (regional or zonal, matching `hack/ci-env.sh`)
- **Database Encryption**: CMEK encryption enabled (`ALL_OBJECTS_ENCRYPTION_ENABLED`). `full-install` creates the cluster this way, so any other state is drift.

The cluster is provisioned by the `terraform/examples/full-install` composition, through its `lifecycle.sh` rather than a bare `terraform apply` — `cluster_name`, `location`, and `api_server_key` have no defaults, so the bare form fails on the missing variables:

```bash
cd terraform/examples/full-install
cat > terraform.tfvars <<EOF
project_id     = "${PROJECT_ID}"
cluster_name   = "platform-agent-host"
location       = "us-central1"
api_server_key = "$(openssl rand -hex 16)"
EOF
KUBE_AGENTS_STATE_BUCKET="${PROJECT_ID}-tf-state" \
KUBE_AGENTS_STATE_PREFIX="full-install/platform-agent-host" \
  ./lifecycle.sh apply
```

`api_server_key` is generated the same way `hack/ci-deploy.sh` generates it when unset. It is regenerated on every apply, which is why section 8 forbids re-running the provisioning script after registration.

## 3. Service accounts and IAM

- **Workload Identity**: Google Service Account `kubeagents-platform-gsa@${PROJECT_ID}.iam.gserviceaccount.com` bound to KSA `kubeagents-platform-agent` in namespace `kubeagents-system` (the KSA name `hack/ci-deploy.sh` and `scripts/installer/common.sh` both use):
  ```bash
  gcloud iam service-accounts add-iam-policy-binding \
    kubeagents-platform-gsa@${PROJECT_ID}.iam.gserviceaccount.com \
    --role="roles/iam.workloadIdentityUser" \
    --member="serviceAccount:${PROJECT_ID}.svc.id.goog[kubeagents-system/kubeagents-platform-agent]"
  ```
- **Upload rights on the project's own registry**, so a presubmit can push its PR build images. `roles/artifactregistry.writer` is the grant to make explicitly, and `scripts/provision_ci_pool_project.sh` makes it. The pool projects that predate the script reach the same permission indirectly — Cloud Build through `roles/cloudbuild.builds.builder`, the Compute default SA through the `roles/editor` GCP grants it by default — which is why `AR_WRITER_ROLES` in `scripts/verify_ci_pool_project.py` accepts a set rather than the one role. `roles/owner` is deliberately not in it. Either build identity holding one of those roles satisfies the check.
- **Reader on the cache images**, in the `kube-agents` repository of `kube-agents-prow` — the repository at location `us`, not the project, and not `us-central1`, because that is where `hack/ci-deploy.sh`'s default `CACHE_IMAGE` and the `:buildcache` manifests beside it live. Both identities need `roles/artifactregistry.reader` there, and the verifier fails the project if either is missing:
  - `${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com`
  - `${PROJECT_NUMBER}-compute@developer.gserviceaccount.com`
- **The Prow runner's access to the project.** Every other grant on this page is for an identity inside the project. This one is not: the presubmit runs on the `build-kube-agents` cluster as `prowjob-default-sa@kube-agents-prow.iam.gserviceaccount.com`, leases the project, and reaches in to fetch cluster credentials, apply the chart, submit the build and read the logs back. Nothing in the project's own configuration implies it, which is how projects 4 to 6 were provisioned, verified green and registered without it — until a lease of `kube-agents-evals-6` died on `Required "container.clusters.get" permission(s)` ([#966](https://github.com/gke-labs/kube-agents/pull/966)). Grant all twelve:

  ```bash
  for role in roles/cloudbuild.builds.editor roles/cloudbuild.builds.viewer \
              roles/container.admin roles/container.developer \
              roles/iam.serviceAccountAdmin roles/iam.serviceAccountUser \
              roles/logging.logWriter roles/logging.viewer \
              roles/resourcemanager.projectIamAdmin \
              roles/serviceusage.serviceUsageConsumer \
              roles/storage.admin roles/viewer; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:prowjob-default-sa@kube-agents-prow.iam.gserviceaccount.com" \
      --role="${role}" --quiet >/dev/null
  done
  ```

  `scripts/provision_ci_pool_project.sh` makes this grant for any project it onboards; the block above is for repairing one provisioned before it did. The list is what `kube-agents-evals` holds, kept as measured rather than trimmed so a new project matches one a presubmit has passed on. It is not minimal — `container.admin` subsumes `container.developer`, `viewer` subsumes `logging.viewer` and `cloudbuild.builds.viewer`. No Artifact Registry role is in it: `hack/ci-deploy.sh` builds and pushes through `gcloud builds submit`, so Cloud Build holds the registry credentials and the runner never touches the registry itself.

- **The platform agent's project roles, checked in both directions.** The agent under test authenticates as `kubeagents-platform-gsa@${PROJECT_ID}`, so this is the one set on this page where an _extra_ role fails the project as well as a missing one. The eight read-only roles come from `local.read_only_roles` in [`terraform/examples/full-install`](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install), which is what the install passes to the IAM module — the module's own `project_roles` default is never read on that path. The verifier hardcodes the eight so it can run without a Terraform toolchain, and a unit test asserts both the composition and the module default match it, so narrowing either fails in CI rather than failing every project weeks later.

  Boskos leases at random, so a project that differs grades differently from the rest of the pool — a case can pass on the grant rather than on the agent, and only on the runs that happen to lease that project. Note that re-running the install does **not** strip roles it no longer grants; correcting an over-privileged project is the hand-swap in [Security and IAM](../reference/security-and-iam.md).

- **GKE Node Service Account**:
  - `roles/artifactregistry.reader` in `${PROJECT_ID}` to pull operator and agent images. The verifier checks this against the account the host cluster's nodes actually run as, read from `nodePools[].config.serviceAccount` — `default` meaning the Compute default SA. Any role in `AR_PULLER_ROLES` satisfies it, which is `AR_WRITER_ROLES` plus the reader role, since every role that confers push already confers read. Push and pull are separate assertions: a project where only Cloud Build can push fails on this one.

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

The evaluation scenarios that exercise the GitOps workflow — the six fleet-audit streams and both remediation cases — write to GitHub. Step 0 of a fleet-audit stream (`audit_report.py start`) mints a repository-scoped GitHub App token and clones the workspace named by the `Git Repo:` line of `/opt/data/SETTINGS.md`; `finish` rewrites a ledger issue and opens remediation pull requests.

**Every pool project needs its own private GitOps repository.** Two leases must not share a ledger issue or race on a remediation branch, and a token minted in one lease must not reach another lease's repository.

<!-- prettier-ignore -->
| Project | GitOps repository |
| --- | --- |
| `kube-agents-evals` | `gke-agentic/kube-agents-evals-infra` |
| `kube-agents-evals-2` | `gke-agentic/kube-agents-evals-2-infra` |
| `kube-agents-evals-3` | `gke-agentic/kube-agents-evals-3-infra` |
| `kube-agents-evals-4` | `gke-agentic/kube-agents-evals-4-infra` |
| `kube-agents-evals-5` | `gke-agentic/kube-agents-evals-5-infra` |
| `kube-agents-evals-6` | `gke-agentic/kube-agents-evals-6-infra` |
| `kube-agents-evals-7` | `gke-agentic/kube-agents-evals-7-infra` |
| `kube-agents-evals-8` | `gke-agentic/kube-agents-evals-8-infra` |
| `kube-agents-evals-9` | `gke-agentic/kube-agents-evals-9-infra` |
| `kube-agents-evals-10` | `gke-agentic/kube-agents-evals-10-infra` |
| `kube-agents-evals-11` | `gke-agentic/kube-agents-evals-11-infra` |
| `kube-agents-evals-12` | `gke-agentic/kube-agents-evals-12-infra` |
| `kube-agents-evals-13` | `gke-agentic/kube-agents-evals-13-infra` |
| `kube-agents-evals-14` | `gke-agentic/kube-agents-evals-14-infra` |
| `kube-agents-evals-15` | `gke-agentic/kube-agents-evals-15-infra` |
| `kube-agents-evals-16` | `gke-agentic/kube-agents-evals-16-infra` |
| `kube-agents-evals-17` | `gke-agentic/kube-agents-evals-17-infra` |
| `kube-agents-evals-18` | `gke-agentic/kube-agents-evals-18-infra` |
| `kube-agents-evals-19` | `gke-agentic/kube-agents-evals-19-infra` |
| `kube-agents-evals-20` | `gke-agentic/kube-agents-evals-20-infra` |
| `kube-agents-evals-21` | `gke-agentic/kube-agents-evals-21-infra` |
| `kube-agents-evals-22` | `gke-agentic/kube-agents-evals-22-infra` |
| `kube-agents-evals-23` | `gke-agentic/kube-agents-evals-23-infra` |
| `kube-agents-evals-24` | `gke-agentic/kube-agents-evals-24-infra` |
| `kube-agents-evals-25` | `gke-agentic/kube-agents-evals-25-infra` |
| `kube-agents-evals-26` | `gke-agentic/kube-agents-evals-26-infra` |
| `kube-agents-evals-27` | `gke-agentic/kube-agents-evals-27-infra` |
| `kube-agents-evals-28` | `gke-agentic/kube-agents-evals-28-infra` |
| `kube-agents-evals-29` | `gke-agentic/kube-agents-evals-29-infra` |
| `kube-agents-evals-30` | `gke-agentic/kube-agents-evals-30-infra` |

The repository is kept private: it is throwaway state a bot rewrites on every run. [`examples/gitops-repo`](https://github.com/gke-labs/kube-agents/tree/main/examples/gitops-repo) is the layout an audit expects to find, not a required seed — the current pool repositories carry only a LICENSE and a README, because an audit works against an empty tree and a `remediation.path` that does not exist degrades to a manual finding rather than failing the run.

> **A row above means the project is mapped, not that it is provisioned or leasable.** The mapping comes first by necessity: Step 0 of `scripts/provision_ci_pool_project.sh` refuses to run against a project `gitops_repo_for_project()` does not know, so the row is written before the applies are. Provisioning follows it and a Boskos entry follows that — so which projects a presubmit can actually lease is the roster in `gke-internal/test-infra`, not this page. Everything from `kube-agents-evals-4` on was provisioned by `scripts/provision_ci_pool_project.sh` and verified before any Boskos entry was made rather than after, the order this page prescribes — which says the order held, not that every row has an entry. Run `scripts/verify_ci_pool_project.py --project-id <project>` for the current state of any one of them; three of the things it checks are:>
>
> 1. The private GitOps repository exists and is mapped in the table above.
> 2. App `4675512` resolves to every pool repository, still `repository_selection: selected`, with `contents: write`, `issues: write`, `pull_requests: write`, `metadata: read`.
> 3. `terraform/examples/ci-pool-minter` is applied per project: each carries `kubeagents-github-minter-gsa@<project>.iam.gserviceaccount.com` and the key ring `github-token-minter-keyring` with key `github-token-minter-key` in `us-central1`, and the App PEM is imported — `gcloud kms keys versions list` shows exactly one `ENABLED` `RSA_SIGN_PKCS1_2048_SHA256` version in each.
>
> `kube-agents-evals-3` is the counter-example that order exists to prevent. It joined the Boskos pool on 2026-08-21 with only its GCP half provisioned, so for three days every presubmit that leased it stopped at `gitops_repo_for_project()`'s unmapped-project refusal, taking a share of every open pull request's smoke test with it. Its repository landed 2026-08-21, its place in the App installation 2026-08-23, and its minter — the `ci-pool-minter` apply plus the PEM import — on 2026-08-24.
>
> **Register a project last, because the switch is pool-wide.** `hack/ci-deploy.sh` enables the minter whenever `GITOPS_REPO` is non-empty and `EVAL_GITHUB_APP_ID` is set, so a project that is mapped but has no key ring renders a `github-token-minter` Deployment pointing at nothing. That pod fails its readiness probe, and the minter is part of the release `helm upgrade --install --wait --timeout 15m` gates on, so the run dies fifteen minutes into the chart-deployment step while leases of the other projects pass. `EVAL_GITHUB_APP_ID` is set in the Prow job environment as of 2026-08-25, so that hazard is live for the next project added rather than hypothetical: the variable means "the manual half is done", and it is only true of the pool when it is true of every project in it.
>
> Reverting the mapping is not the fix. It restores the immediate, named refusal at `gitops_repo_for_project()` — more legible than a fifteen-minute timeout, and the fast-fail this page exists to preserve — but a lease of the project still fails, so it buys diagnosability, not a working project. Finish the project or drop it from the pool. Section 7 is how you tell which case you are in without waiting for a presubmit to find out.

### 5.1 How CI resolves it

`hack/ci-deploy.sh` maps the leased project to its repository in `gitops_repo_for_project()` and passes the result as `--set-string platformAgent.integration.github.gitRepo=...`. The operator renders that field into the `platform-agent-settings` ConfigMap as the `Git Repo:` line.

CI supplies the value rather than relying on the chart default, and that is deliberate. A presubmit builds and deploys the pull request's own chart, operator, and agent, so a pull request that blanks `platformAgent.integration.github.gitRepo` in `values.yaml`, or breaks the CR-to-`SETTINGS.md` rendering, is exactly the regression the eval should surface as a failed scenario — which it can only do if the value the run is supposed to use comes from outside the artefacts under test. (This is a correctness argument, not the containment boundary; see 5.3.)

Adding a project is one line in `gitops_repo_for_project()`, one row in the table above, and one entry in `_EXPECTED_MAPPING` in [`tests/test_ci_gitops_repo.py`](https://github.com/gke-labs/kube-agents/blob/main/tests/test_ci_gitops_repo.py). The test entry is the one that is easy to skip: the suite iterates that dictionary rather than parsing the function for projects it does not know about, so a mapping added without it stays green and stays untested.

An unmapped project stops the deploy:

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

1. **Install the GitHub App on the repository** (org-admin on `gke-agentic`, plus App-manager rights). Grant `contents: write`, `pull_requests: write`, and `issues: write`, on that one repository. **Done for every project in the table above** — see the App below; each further project means adding its repository to the same installation, and that edit is the security review.
2. **Import the App's private key** into the project's KMS signing key with the Minty CLI. The PEM must never enter Terraform state, so the key is created import-only and empty; the command is in the [composition's README](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/ci-pool-minter). Confirm version 1 reaches `ENABLED`. This one is per project — the same PEM, imported into each project's own key.

The pool's write half is served by a single App, `kube-agents-evals-token-minter`, **App ID `4675512`**, installed on each project's `gke-agentic/<project>-infra` repository and nothing else. (The read half is a second App; see 5.4.) The query below is the list, rather than a copy of it kept here to go stale:

```bash
gh api /orgs/gke-agentic/installations \
  --jq '.installations[] | select(.app_id==4675512) |
        {app_slug, repository_selection, permissions}'
```

It is a dedicated App rather than the organisation's existing all-repositories minter, and that is a deliberate cost. Reusing the staging App would have copied its signing key into every pool project's KMS, added unreviewed presubmit code to the callers of an identity that otherwise only serves merged code, and coupled rotation — an eval incident forcing a key rotation would have taken staging and autopush with it.

Only then does `EVAL_GITHUB_APP_ID=4675512` belong in the Prow job environment. The value is the same for every pool project, and it is set there as of 2026-08-25 ([oss-test-infra#2661](https://github.com/GoogleCloudPlatform/oss-test-infra/pull/2661)). `hack/ci-deploy.sh` keeps `githubMinter.enabled=false` while it is unset, because the minter Deployment is part of the release `helm --wait` gates on: enabling it before the key import fails every presubmit instead of degrading quietly. Now that it is set, a project added to the pool before its key import fails that way — which is what section 7 is for.

### 5.3 What actually bounds where a run can write

The GitHub App's installation list, and nothing else. A presubmit runs the pull request's code, so a pull request can in principle edit the resolution table or the minty rule ConfigMap — but it cannot make the App mint a token for a repository the App is not installed on. Keep the installation scoped to the pool's GitOps repositories, and treat any change to that list as the security review.

### 5.4 The credential that reads the ledger back

Everything above is the write half. Publishing a ledger issue is only half of what the eval does with it: the scenarios that plant a defect then grade the issue with the `ledger_issue_contains` check, and that check reads GitHub as the Prow runner rather than as the agent. [Grading a fleet audit](https://github.com/gke-labs/kube-agents/blob/main/bench/CUSTOM-TASKS.md#grading-a-fleet-audit) is that check's design; this section is only its credential. Its credential is a second GitHub App, `kube-agents-evals-ledger-reader` (`4739812`), installed on `gke-agentic` with `issues: read` and `metadata: read` and nothing else. `hack/ci-eval-pr.sh` signs a JWT with the App's private key and trades it for a one-hour installation token, then puts that token in `BENCH_GITHUB_TOKEN` — the variable `ledger_issue_contains` reads. Once up front, and again inside each fan-out unit as late as it can be: units queue against each other, and one can wait long enough before it runs that a token minted at launch would already have expired. Each unit is its own subshell, so a token minted in one does not reach its siblings anyway.

The key is Kubernetes secret `kube-agents-evals-ledger-app-key` (entry `key.pem`) in namespace `test-pods` on cluster `kube-agents-prow`, zone `us-west1-b`, project `kube-agents-prow`. That is the GKE cluster; `build-kube-agents` is the alias the prowjob's `cluster:` field names, and `get-credentials` on it fails. The Prow job mounts the secret at `/etc/ledger-app-key/key.pem` and points `EVAL_LEDGER_APP_KEY_FILE` at it. Unset, the script leaves `BENCH_GITHUB_TOKEN` alone. Nothing falls back to the PAT: a fallback would let a smoke test pass while proving nothing about the credential. The script mints once up front, where a key that cannot mint at all kills the run in seconds; a mint that fails later kills only that unit, whose repetition then grades `MISSING`.

**Add each new repository to the App's installation before the project is registered.** The installation is `repository_selection: selected`, and a repository outside the list is a `404` — indistinguishable from one that does not exist. Any `gke-agentic` owner can add one, which is the difference from what this replaced: a fine-grained token on one person's account, extendable only by that person.

`selected` stays even though the App is read-only, for the same reason as 5.3: `gke-agentic` holds repositories that are not pool infrastructure, and the list is what keeps a bench run's credential off them. Widening it to `all` would pass every check here — the read the check makes still succeeds — so the mint response's `repository_selection` is checked directly and `all` fails the project.

Skipping this step fails nothing visible until a lease. `kube-agents-evals-6` was registered with its repository outside the then-credential's scope, and the first run to lease it filed its ledger issue correctly and then 404'd reading it back — a red on an unrelated pull request ([#994](https://github.com/gke-labs/kube-agents/issues/994)). Section 7's `Ledger Read Credential` check is what catches it beforehand.

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

The fleet owner creates `gs://${PROJECT_ID}-tf-state` once per project. Confirming the apply is section 7's job: `scripts/verify_ci_pool_project.py` runs `hack/fleet-kubeconfigs.sh` against the project and requires all seven fixture roles, so there is no separate command to remember here and no dated claim about which projects are planted to go stale. A non-zero count under _whose fixtures were not present_ is a project the stack needs re-applying in, and fails the check. A count only under _could not be resolved or reached_ usually is not: those clusters were never read, so the check reports their roles as unchecked rather than accusing a fleet it could not see, and the run exits `2` unless something else failed. The exception is what separates "I could not look" from "I looked and the fleet is wrong", since both land in that same count and only the script's own warnings tell them apart. Four warnings mean it read the cluster list and what came back is not what the catalog describes — the project carries no labelled clusters, none resolved to a catalog slot, a cluster matches no slot, or two clusters match one — and a role left unresolved alongside any of them fails the check. Unless the script also said it could not list the project's clusters: a refused listing leaves it with an empty list, from which it prints the no-labelled-clusters warning as well, and nothing it says after that is evidence about the fleet.

Nothing outside the fleet's own catalog addresses these clusters by name. `hack/fleet-kubeconfigs.sh` discovers them in the leased project by the labels the stack applies (`environment=seeded`, `managed-by=kube-agents-seeded-fleet`), so a project may use a different `cluster_prefix` or region without any scenario changing. The one other sanctioned consumer discovers by the same labels: `hack/ci-eval-pr.sh` §3b reuses the slot-c cluster as the presubmit's log-fixture subject instead of provisioning a per-run cluster, mutating nothing in it — the fleet's catalog (`bench/tf/fleet/fixtures.json`) records the exception.

A half-finished apply is the case to watch for. The stack's Kubernetes provider is configured against a cluster the same stack creates, so an apply that fails after the clusters and before the fixtures leaves a trio that carries the labels, answers every API call, and holds none of the planted objects. The runner therefore reads every object in the role's `probes` list before it publishes that role — the objects themselves, not just their namespaces, since four of the seven roles are cluster-scoped — and a role it cannot confirm reports `status: "error"` naming the role and the project, the same answer as no fleet at all, rather than a check that blames the agent for a fixture nobody planted. `tofu apply` again until it is clean.

### 6.1 A read-only credential for the checks

An eval run reads the fleet to confirm its fixtures survived; it has no business being able to change them, and a safeguard is worth less when the credential that checks it could also have caused what it is checking for. **This is not true today.** The Prow identity holds `roles/container.admin` in every eval project, and there are no in-cluster RoleBindings to narrow — GKE's IAM webhook is the whole authorization path.

The seam exists: the fleet stack provisions `seeded-fleet-reader@${PROJECT_ID}.iam.gserviceaccount.com` with `roles/container.viewer` and nothing else. To use it, per project:

1. Add the Prow identity to `fleet_reader_token_creators` and re-apply the stack, which binds it `roles/iam.serviceAccountTokenCreator` on that account alone.
2. Export `FLEET_READONLY_SA=seeded-fleet-reader@${PROJECT_ID}.iam.gserviceaccount.com` in the Prow job. Unset, `hack/fleet-kubeconfigs.sh` warns on every run and the kubeconfigs carry the runner's own identity.

## 7. Pre-flight verification

`scripts/verify_ci_pool_project.py` checks the live project against the sections above. Run it before section 8.

```bash
python3 scripts/verify_ci_pool_project.py --project-id kube-agents-evals-4 \
  --confirmed-repo-in-app-installation
```

It exits `0` when everything checked passed, `1` when a prerequisite failed, and **`2` when nothing failed but something could not be checked**. The third code exists because a script that prints "ALL CHECKS PASSED" over items it merely could not read gives the same false assurance that let `kube-agents-evals-3` into the pool. Treat `2` as "go and look", not as a pass.

A bad command line exits `64`, not `2`, so a mistyped flag cannot be mistaken for an unverified item. One case stays ambiguous and cannot be fixed inside the script: if the _path_ to the script is wrong, Python exits `2` before the file is read. A wrapper that branches on `2` should check the path exists first.

`scripts/provision_ci_pool_project.sh` runs it as its own last step, so a project provisioned by the script has been through this already.

Two checks need more than a read-only API call, and both need `kubectl`. `Seeded Fleet Fixtures` runs `hack/fleet-kubeconfigs.sh`, which fetches cluster credentials into a temporary directory it removes on the way out. `Ledger Read Credential` reads a secret out of the build cluster and POSTs for a one-hour installation token — nothing in the pool project, but not a read either. Without `kubectl` on `PATH` both report as unverified rather than failing the project.

**It is not a complete reading of this page.** Read the script's own check list rather than assuming a green run means every paragraph above is satisfied; an inventory copied here goes stale the first time a check is added, and it goes stale in the dangerous direction — a list of what _is_ checked, left behind, tells an operator to skip a verification that no longer happens. Three things running the script will not tell you: the platform agent GSA's eight read-only roles are the only set checked for extras as well as absences, the host cluster's node account's pull rights are read off `nodePools[].config.serviceAccount` rather than assumed to be the Compute Engine default, and the host cluster's location is not checked at all.

Some items report `2` for a reason other than a refused read. The first two below cannot be settled from a machine at all, so they report `2` until an operator settles them by hand — for the mapping that means landing the pull request, not attesting to anything. The last two report `2` only when the probe cannot run: the ledger read for want of the build cluster, signing for want of the permission or the network.

- **The mapping on `main`.** The check reads `hack/ci-deploy.sh` twice, from this checkout and from `gke-labs/main`, because a presubmit runs main's copy rather than your branch's. A row that exists only on the branch reports `2` with `not yet on gke-labs/main`. The project is provisioned; registering it before the row merges is the `kube-agents-evals-3` outage again. Land the pull request and re-run. What it reads for main is the remote-tracking ref — the last `git fetch`, not GitHub — so the warning names that snapshot's date, and a snapshot old enough to predate `gitops_repo_for_project()` itself reports `2` saying the copy could not be read rather than claiming any project is unmapped.
- **Installation membership.** Listing an App installation's selected repositories needs a token authorized to the App itself; an operator PAT is not one, and no OAuth scope makes it one. Open the installation's settings page on the `gke-agentic` org — `gh api /orgs/gke-agentic/installations --jq '.installations[] | select(.app_id==4675512) | .html_url'` prints the URL — confirm `gke-agentic/<project>-infra` is in the list, and pass `--confirmed-repo-in-app-installation`. The summary then reports the item as operator-confirmed rather than machine-checked. If the list ever does become readable and the repository is genuinely absent, the flag does not override that.
- **The ledger read credential.** `Ledger Read Credential` reads the ledger App's key out of the build cluster with `kubectl`, mints an installation token from it, and lists `gke-agentic/<project>-infra`'s issues with that. Not the call the grader makes — that fetches an issue by number, and a candidate project has none yet — but the same permission, which is what proves `issues: read` rather than mere visibility (5.4). It deliberately does not fall back to your own `gh` login: you are an org member, so your credential answers `200` for a repository the CI credential cannot see, which is a pass on exactly the question. It reports `2` when it cannot get to the key — no `kubectl`, no context for `kube-agents-prow`, or a refused read — and closing the item means `gcloud container clusters get-credentials kube-agents-prow --zone us-west1-b --project kube-agents-prow` and a re-run. A `403` or `404` from the CI credential itself is a failure rather than an unchecked item, and a `403` is only excused when GitHub says the credential is rate-limited.
- **Signing.** The script asks KMS to sign a GitHub App JWT and calls `GET /app` to see whether GitHub accepts it as App `4675512`. It signs with the version the chart deploys — `githubMinter.kms.keyVersion` in `charts/kube-agents/values.yaml`, which nothing in `hack/ci-deploy.sh` overrides — rather than the newest enabled one, and fails the project when that version is not `ENABLED`. A rotation that imports a new version and disables the pinned one would otherwise pass here and then fail every lease, because the deployed minter loads the pinned version and nothing else. This is the only check that proves the imported material is a private key of _this_ App: KMS stores opaque bytes, so a PEM from another App imports cleanly, reports `ENABLED`, satisfies every attribute check, and fails for the first time at a real push. Signing needs `cloudkms.cryptoKeyVersions.useToSign` on the key. Without it — or without egress to `api.github.com` — the run reports `2` rather than failing the project, because that is a limit of the operator's credentials and not a defect in the project. No attestation flag is offered for this one, unlike membership above: whether the bytes in KMS belong to this App is not something anyone can establish by looking at a console.

Elsewhere `2` means the read did not happen. `gcloud` answers a read the caller holds no IAM for with `PERMISSION_DENIED ... (or it may not exist)` and will not say which of the two it means, so neither does the verifier: it reports that item as unchecked rather than as a missing resource. It reads a call that timed out and a credential that expired mid-run the same way, for the same reason — a resource that was not looked at is not a resource that is absent. An account without project-level read therefore gets `2` and a list of things to go and confirm, rather than `1` and a list of resources that are all in fact present.

Two places depart from that, both deliberately. `Seeded Fleet Fixtures` reports `2` for causes wider than an unperformed read — `kubectl` absent, `hack/fleet-kubeconfigs.sh` returning no summary line, clusters it could not reach — and narrower in the case the paragraph in section 6 describes, where the script says the fleet is not what the catalog declares and the check fails the project. And inside `GitOps Repo & GitHub App Installation`, a failing `gh repo view gke-agentic/<project>-infra` is a failure rather than an unchecked item, even though GitHub answers `404` both for a repository that does not exist and for one the token cannot see: a GitOps repo that was never created is the onboarding gap the check exists to catch, so its message names both readings instead of going quiet on the first. The installation lookup in that same check reads its `404` the other way round, because `GET /orgs/{org}/installations` returns one for a token without `admin:org` and returns `200` with an empty list when an org genuinely has no installations — so only the `404` is a visibility limit.

A run that can answer every question needs project-level read on the pool project — section 3's grants go to the project's own service accounts, not to the operator. On `kube-agents-evals-6`, an operator holding neither was refused `iam.serviceAccounts.getIamPolicy`, `resourcemanager.projects.getIamPolicy`, `storage.buckets.get`, `artifactregistry.repositories.get`, `artifactregistry.repositories.getIamPolicy`, `container.clusters.get` and the three `cloudkms` reads, while `resourcemanager.projects.get`, `serviceusage.services.list` and `container.clusters.list` went through. Signing needs `cloudkms.cryptoKeyVersions.useToSign` on top of that, and `GET /orgs/{org}/installations` needs `admin:org` on the GitHub token, answering `404` without it. The ledger read needs `get` on Secrets in `test-pods` on the `kube-agents-prow` cluster: `get-credentials` yields a kubeconfig and not the RBAC, so an operator who runs it and re-runs still gets a `2` on that item.

## 8. Boskos pool registration

Once the GCP project is provisioned with the prerequisites above, register the project ID under the `kube-agents-evals-project` resource type in the Prow Boskos deployment configuration:

```yaml
- type: kube-agents-evals-project
  state: free
  names:
    # the projects already registered, left as they are
    - <NEW_PROJECT_ID>
```

This roster does not live in `oss-test-infra` with the rest of the Prow config — it is in `gke-internal/test-infra`, under `deployments/gke-agentic-tooling-team/boskos`. That split is why registration and onboarding can drift apart: this page is the only thing joining the two repositories, and nothing enforces the order between them.

**Prove it with one eval when the provisioning script or its Terraform changed** — not once per project. The failure that argued for a per-project rule was a credential nothing read, and section 7's `Ledger Read Credential` now reads it (5.4). What no check can tell you is whether a _changed_ provisioning path produces a working project, since every check reads the state that change just wrote; a smoke run costs upwards of two hours plus a commit to revert, so spend it on the change.

When you do spend it, pin a run to the project while it is still unregistered. On the pull request that maps the project, add an unconditional assignment to `hack/ci-env.sh` and revert that commit before merge:

```bash
export PROJECT_ID="kube-agents-evals-7"   # TEMPORARY PIN -- REVERT BEFORE MERGE
```

Unconditional, and after the `PROJECT_ID="${PROJECT_ID:-...}"` line rather than in place of it. The Prow job exports the leased project into the environment before sourcing this file, so a pin written with `:-` is silently ignored and the run tests whatever Boskos handed out. Boskos still leases a project and leaves it idle; deploy, eval and teardown all follow `PROJECT_ID`, and the idle lease is released as usual. **The pin must not merge** — on `main` it sends every presubmit to one project and serialises the pool behind it.

**Register the project last.** Everything above — the APIs, the cluster, the registry, the GitOps repository, the App installation, the key import, the `gitops_repo_for_project()` row, the seeded fleet — is a prerequisite of the entry in this list, not a follow-up to it. A project that becomes leasable before it is onboarded takes a share of every presubmit and fails it, which is how `kube-agents-evals-3` broke the smoke test for every open pull request on 2026-08-21.

**And do not re-run `scripts/provision_ci_pool_project.sh` after it.** Registration is the boundary in both directions. Before it, re-running is free and expected — a first run rarely reaches the end, and every section above is written to be repeatable. After it, the script is the wrong tool: step 2.1 generates a fresh `api_server_key` on each invocation and `terraform/examples/full-install` writes it into the agent's Secret, so a re-run rotates a credential out from under whatever run currently holds the lease, and the agent pod keeps serving the old value until something restarts it. Nothing in the script stops you, and nothing in the failure looks like its cause. Repairing a registered project is a different job from onboarding one, and there is no tooling for it yet.

> **Important:** The Boskos janitor must be disabled for `kube-agents-evals-project` so that the long-lived `platform-agent-host` cluster and pre-warmed state are preserved across leases.

## 9. Raise the presubmit's concurrency

Registration makes a project leasable; it does not make the presubmit use it. The evaluation job's `max_concurrency` in `oss-test-infra` caps how many runs are in flight at once, and the pool size is only a ceiling above it — add five projects without raising that number and the presubmit runs exactly as many evals as it did before, with the new projects idle.

Raise it to the number of leasable projects, not the number provisioned. A slot with no project to lease blocks on Boskos rather than running.

This is the one step in a different repository from section 8's roster: `oss-test-infra` holds the job config, `gke-internal/test-infra` holds the pool. Neither knows about the other, so a change to one is never a prompt to change the other.

## 10. Know when to onboard the next one

Onboard when the rolling p50 wait goes over 15 minutes or the rolling p95 over 45. `scripts/pool_pressure.py` measures that over a seven-day window and exits 1 when either trips; the two numbers are `DEFAULT_P50_THRESHOLD_MINUTES` and `DEFAULT_P95_THRESHOLD_MINUTES` at the top of that file, and this paragraph is what they cite. A day is judged on its own rather than blended into the window, so one bad day trips the check instead of being averaged away, and days under five runs are counted but not judged — a weekend here runs single digits, and two samples put any number at p95.

```bash
python3 scripts/pool_pressure.py                 # the last seven days
python3 scripts/pool_pressure.py --json          # the same run as data
python3 scripts/pool_pressure.py --as-of 2026-08-27 --window-days 1   # replay a past day
```

It exits 0 within threshold, 1 on a breach, and 2 when it could not measure — which is not a green run. Nothing it does provisions anything; `hack/pool_pressure_cron.sh` runs it on a schedule and hands a breach to a notifier, and it too only reports.

### What "wait" means here

Setup time: everything between Prow accepting the job and the test being able to start, in four segments the check reports separately.

| segment | from              | to                                |
| ------- | ----------------- | --------------------------------- |
| queue   | ProwJob created   | pod created                       |
| pod     | pod created       | container running                 |
| setup   | container running | the run asks Boskos for a project |
| lease   | the request       | the project is in hand            |

The split is the point. A run that waited three hours in Prow's queue and a run that waited three hours inside `boskosctl acquire` are the same number in a single total and two different problems, and only one of them is solved by buying a project. Each segment reports how many runs it was measured on, because a log that stops early or a banner that has been reworded upstream makes a segment go quiet, and a median over the few runs that still parse reads exactly like a healthy pool.

### Read the pool before you read the wait

A long wait with the pool full is demand. A long wait with projects sitting free is not, and it has been seen: [oss-test-infra#2666](https://github.com/GoogleCloudPlatform/oss-test-infra/issues/2666) was runs stuck in `triggered` against an idle build cluster, a Prow control-plane problem that onboarding a project would have cost money and fixed nothing. So the check reads the pool alongside the wait and reports one of four causes, and **only `CAPACITY` justifies onboarding**: `CONCURRENCY_CAP` means projects exist that the cap will not let anything reach, `CONTROL_PLANE` is the #2666 shape, and `UNKNOWN` means the pool could not be read and the question is still open.

The lease segment is the tiebreaker between the first and the third. A lease that took minutes is real contention; a prompt lease under a long total wait puts the delay before the pod, where Boskos has no say in it. That used to mean opening a slow build's `build-log.txt` by hand.

Since [oss-test-infra#2678](https://github.com/GoogleCloudPlatform/oss-test-infra/pull/2678) the cap equals the pool, so Prow admits enough pods to claim every project and none is held in reserve. One project cleaning, dirty, or holding a leaked lease is then a run that starts on time and blocks in `boskosctl acquire` until its ten-minute timeout. The check says so when it applies, and it is the case where the lease segment is the only place the delay shows.
