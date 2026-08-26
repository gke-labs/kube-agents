# GitHub token minter for a CI evaluation-pool project

Root composition that provisions the [GitHub token minter](../../modules/github-minter/README.md)'s GCP half in **one** project of the Prow evaluation pool (`kube-agents-evals-project`), so that the presubmit eval's GitHub-writing scenarios — the fleet-audit streams and `rca-remediation-pr` — can mint a token scoped to that project's GitOps repository and no other.

This is not an installer. The pool projects already have their `platform-agent-host` cluster, their agent GSA, and their Artifact Registry repository; those were provisioned before this composition existed and stay outside its state. It adds only the three things the minter needs and nothing else:

- the minter GSA `kubeagents-github-minter-gsa@<project>.iam.gserviceaccount.com`,
- its Workload Identity binding to `kubeagents-system/kubeagents-github-minter`,
- the import-only KMS signing key `github-token-minter-key` in `github-token-minter-keyring`, plus the `roles/cloudkms.signerVerifier` grant on it.

Use [`terraform/examples/full-install`](../full-install/README.md) instead for a whole environment; that composition already includes the same module behind `enable_github_minter`.

## Why one apply per project

Each pool project gets its **own** private GitOps repository, and the minty rule the chart renders scopes tokens to exactly that one repository, keyed on that project's agent GSA. Two leases therefore cannot reach each other's repository, cannot share a ledger issue, and cannot race on a remediation branch. The resources are per-project (a KMS key ring is not shareable across projects), so the composition is applied once per project with its own state.

The project-to-repository mapping has exactly one home: `gitops_repo_for_project()` in `hack/ci-deploy.sh`, documented in [CI pool project prerequisites](../../../docs/site/src/content/docs/deploy/ci-pool-projects.md). Read the `gitops_repo` for the project being onboarded out of there. Onboarding a project is one line in that function plus one row on that page, so a copy of the table here would go stale on the first onboarding.

## Usage

**One state per project, chosen before the first apply.** This composition ships no backend block, so a bare `terraform apply` writes `terraform.tfstate` into this directory. Re-pointing `project_id` at a second pool project and re-applying against that same state does not add a second minter — `project_id` is force-new on the module's GSA, so the plan **destroys** the first project's `kubeagents-github-minter-gsa`, its `roles/iam.workloadIdentityUser` binding, and its `roles/cloudkms.signerVerifier` grant before creating the second project's. Re-applying does not undo that: by the KMS warning below, the first project's key ring and key then have to be `terraform import`ed back before its GSA can be recreated.

Pick one of these once, per project:

```bash
cd terraform/examples/ci-pool-minter

# a) Workspaces — one local state file per project under terraform.tfstate.d/,
#    no extra files to write. Fine when the checkout is long-lived.
terraform init
terraform workspace new kube-agents-evals    # later runs: workspace select

# b) Remote state, the same pattern terraform/examples/full-install uses: a
#    gitignored backend_override.tf (*_override.tf is in .gitignore) with a
#    per-project prefix. Prefer this if the state must outlive the checkout.
cat > backend_override.tf <<'EOF'
terraform {
  backend "gcs" {
    bucket = "<your-tfstate-bucket>"
    prefix = "ci-pool-minter/kube-agents-evals"
  }
}
EOF
terraform init
```

Then, for that project:

```bash
cp terraform.tfvars.example terraform.tfvars   # set project_id and gitops_repo
terraform plan                                 # must be create-only
terraform apply
terraform output manual_steps
```

`terraform plan` before every apply is the check that matters. Onboarding a project that has not been onboarded yet is a create-only plan; **any `destroy` line means the wrong state is loaded** — stop and fix the workspace or the backend prefix rather than confirming.

**Applied for all five pool projects as of 2026-08-26** — `kube-agents-evals` through `kube-agents-evals-5`, each in its own workspace, each with the App PEM imported (`gcloud kms keys versions list --location us-central1 --keyring github-token-minter-keyring --key github-token-minter-key --project <project>` shows one `ENABLED` `RSA_SIGN_PKCS1_2048_SHA256` version in each). A sixth pool project is the create-only case above, in a workspace of its own, and step 2 below has no key to import into until that apply lands.

`location` and `namespace` default to the values `hack/ci-env.sh` uses (`us-central1`, `kubeagents-system`) and should only be overridden if that file changes: the chart derives the KMS key path from `platformAgent.harness.location`, which `hack/ci-deploy.sh` sets from `REGION`.

> **KMS resources cannot be deleted.** `terraform destroy` removes the key ring and key from state only; re-applying with the same names fails with a 409. The [module README](../../modules/github-minter/README.md) covers the recovery.

## What Terraform cannot do

Two steps are human-only, and the minter does not work until both are done. `terraform output manual_steps` prints them with this project's values substituted.

**1. Install the GitHub App on the project's GitOps repository.** A GitHub App installation is not a GCP resource, and creating one needs org-admin rights on `gke-agentic`. Grant `contents: write`, `pull_requests: write` and `issues: write` on that repository only.

**This is already done for every project onboarded so far.** The pool is served by one App, `kube-agents-evals-token-minter`, **App ID `4675512`**, installed on each project's `gke-agentic/<project>-infra` repository and nothing else. The query below is the list, rather than a copy of it kept here to go stale:

```bash
gh api /orgs/gke-agentic/installations \
  --jq '.installations[] | select(.app_id==4675512) |
        {app_slug, repository_selection, permissions}'
```

`repository_selection` must read `selected`. If it ever reads `all`, a presubmit can mint for every repository in the organisation and the boundary below is gone.

This installation — not the minty rule, and not `hack/ci-deploy.sh` — is what actually bounds where an eval run can write. A presubmit builds and deploys the pull request's own chart, operator, and agent, so a pull request can in principle rewrite the rule ConfigMap or the resolution table; it cannot make the App mint a token for a repository the App is not installed on. Keep the installation list to the pool's GitOps repositories, and treat adding a repository to it as the security review.

A dedicated App rather than an existing one, deliberately. `gke-agentic` already hosts an all-repositories minter App used by the staging deployment, and pointing the pool at it would have meant three things: the staging App's signing key copied into every pool project's KMS, unreviewed presubmit code joining merged code as a caller of the same identity, and any rotation forced by an eval incident taking staging and autopush down with it. The pool's own App costs one creation and removes all three.

**2. Import the App's private key into the signing key.** The PEM must never enter Terraform state, so the key is created import-only and empty. The Minty CLI does the cryptographic wrapping:

```bash
git clone --depth 1 --branch v2.7.1 \
  https://github.com/abcxyz/github-token-minter.git /tmp/minty
cd /tmp/minty && go run ./cmd/minty tools import-pk \
  -project-id="${PROJECT_ID}" -location=us-central1 \
  -key-ring=github-token-minter-keyring -key=github-token-minter-key \
  -private-key=@/path/to/app-private-key.pem
```

Confirm afterwards that version 1 is `ENABLED`:

```bash
gcloud kms keys versions list --key=github-token-minter-key \
  --keyring=github-token-minter-keyring --location=us-central1 \
  --project="${PROJECT_ID}"
```

## Turning the minter on in CI

`hack/ci-deploy.sh` renders `githubMinter.enabled=false` until `EVAL_GITHUB_APP_ID` is set in the job environment, and that variable is the switch meaning "the two manual steps above are done for this project". Its value is **`4675512`** and is the same for every pool project — one App serves the pool. What differs per project is the KMS key its PEM was imported into. The minter Deployment is part of the release `helm --wait` gates on, so enabling it before the key import fails every presubmit rather than degrading quietly.

The chart needs nothing else per project. `githubMinter.gsaName` and `githubMinter.allowedServiceAccount` both derive from `platformAgent.harness.projectId`, which `hack/ci-deploy.sh` sets to the leased project, so the rule comes out keyed on that project's `kubeagents-platform-gsa` automatically.
