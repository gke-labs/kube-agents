# Warm build-cache access for a CI evaluation-pool project

Root composition that grants **one** project of the Prow evaluation pool (`kube-agents-evals-project`) `roles/artifactregistry.reader` on `us/kube-agents` in `kube-agents-prow`, the repository holding the warm `:latest` image that every presubmit build seeds its layer cache from.

It manages exactly one resource — a single additive IAM member. That is the whole composition, and it is a composition rather than a bullet on a checklist because the bullet was skipped once and the cost was high.

## The incident this exists to prevent

`kube-agents-evals-3` was registered in the Boskos pool on 2026-08-21 without this grant. Every presubmit that leased it failed the `warm-cache` step of [`deploy/docker/cloudbuild-ci.yaml`](../../../deploy/docker/cloudbuild-ci.yaml) with `Permission 'artifactregistry.repositories.downloadArtifacts' denied`, three times, and then cold-built all three images. Roughly a third of every run in the repository drew that project.

What made it expensive was not the failure but its shape. The lease is random, so the same pull request passed on one run and struggled on the next, which reads as flake; and the grant is invisible from inside the pool project, so nothing in the project's own configuration is missing. The [pool-project prerequisites page](../../../docs/site/src/content/docs/deploy/ci-pool-projects.md) did document the step, but named `${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com` — an identity the build does not run as — so following it would not have fixed the project.

## Why the grant lives in the cache project

`hack/ci-deploy.sh` submits the build with `gcloud builds submit --project="${PROJECT_ID}"`, and `PROJECT_ID` is the **leased** pool project, not `kube-agents-prow`. `cloudbuild-ci.yaml` declares no `serviceAccount:`, so the build runs as the leased project's Compute Engine default service account, `${PROJECT_NUMBER}-compute@developer.gserviceaccount.com`. Pulling the cache image is therefore a cross-project read by the pool project's identity, and only the project that owns the repository can authorise it.

The project number is read at plan time from `data.google_project`, so onboarding a further pool project is one `project_id` in `terraform.tfvars` and no edit to the HCL.

## Who can apply this, and why it is not part of `ci-pool-minter`

**Applying this needs `roles/artifactregistry.admin` (or another `setIamPolicy` holder) on `kube-agents-prow`, plus `resourcemanager.projects.get` on the pool project.** That is a materially different right from the rest of pool-project onboarding, all of which is confined to the pool project itself. Note it before you apply: the plan writes to a shared, production-adjacent project that serves every pool project and the postsubmit publisher.

That escalation is the first reason this is not a resource inside [`ci-pool-minter`](../ci-pool-minter/README.md). Folding it in would quietly require every future minter apply — a per-project, pool-project-scoped operation today — to also carry IAM-admin on the shared cache project, or to fail part-way through with the minter GSA created and the grant missing.

The second reason is that `ci-pool-minter`'s state is hazardous in a way this resource must not inherit. `project_id` is force-new on that composition's minter GSA, so an apply against the wrong workspace destroys another project's minter. If the cache grant shared that state, the same mistake would additionally destroy and recreate the IAM member — that is, **revoke a working project's cache access** as a side effect of a mis-selected workspace, converting a one-project outage into a two-project one. Keeping the grant in a state that holds nothing else means the worst a wrong workspace can do here is move one member.

They are also simply different concerns: `ci-pool-minter` is about the GitHub App's signing key, this is about registry reads. What joins them is the onboarding checklist on the [prerequisites page](../../../docs/site/src/content/docs/deploy/ci-pool-projects.md), which names both.

## Usage

**One state per project, chosen before the first apply**, for the same reason `ci-pool-minter` insists on it — this composition ships no backend block, so a bare `terraform apply` writes `terraform.tfstate` into this directory. Re-pointing `project_id` at a second pool project and applying over the first project's state does not add a second member; the plan removes the first project's and adds the second's, and that project's presubmits go back to cold builds until someone notices.

```bash
cd terraform/examples/ci-pool-registry-access

terraform init
terraform workspace new kube-agents-evals-3    # later runs: workspace select

cp terraform.tfvars.example terraform.tfvars   # set project_id
terraform plan                                 # one create, nothing destroyed
terraform apply
```

A `backend_override.tf` with a per-project `prefix`, as in [`full-install`](../full-install/README.md), works equally well and is the better choice if the state must outlive the checkout.

**Read the plan.** Onboarding a new project is a single `google_artifact_registry_repository_iam_member` create. Any `destroy` line means the wrong state is loaded — stop rather than confirming.

Confirm afterwards that the apply _added_ a member rather than replacing the set:

```bash
gcloud artifacts repositories get-iam-policy kube-agents \
  --location=us --project=kube-agents-prow
```

Every previously onboarded pool project must still be listed under `roles/artifactregistry.reader`, and `github-actions@kube-agents-prow` must still hold `roles/artifactregistry.writer`.

## Additive, not authoritative

The resource is `google_artifact_registry_repository_iam_member`, and the choice is load-bearing. `us/kube-agents` has several independent tenants — one member per pool project, one unrelated project, and the postsubmit publisher's `artifactregistry.writer` grant — while this composition is applied per project and knows about exactly one of them.

`google_artifact_registry_repository_iam_binding` is authoritative for the whole role: it would set the reader members to the applying project's service account alone and revoke every other pool project's cache access, which is the outage this composition exists to end, inflicted on the projects that already work. With one state per project it would never converge either — each apply would evict the previous one.

`google_artifact_registry_repository_iam_policy` is authoritative for the entire policy, so it would do all of that and also drop `artifactregistry.writer`, after which the cache image stops being published and there is nothing left for anyone to pull.

The same comment lives in `main.tf`, next to the resource, because that is where the temptation is.

## Projects already granted by hand

`kube-agents-evals` and `kube-agents-evals-2` were granted before this composition existed. A first apply for either is a create in Terraform's plan and a no-op at the API: `setIamPolicy` with a member already present in the binding leaves the policy unchanged. **No `terraform import` is required** — importing is merely tidier if you want the plan for a second run to be empty, and the resource's import ID is `projects/kube-agents-prow/locations/us/repositories/kube-agents roles/artifactregistry.reader serviceAccount:<number>-compute@developer.gserviceaccount.com`.

What a `_iam_member` state does **not** claim is exclusivity, so bringing those two projects under management cannot disturb each other or the writer grant.
