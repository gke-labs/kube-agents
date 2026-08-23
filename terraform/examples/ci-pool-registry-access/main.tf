# Lets ONE project of the Prow evaluation pool pull the warm build cache image
# out of the shared registry in kube-agents-prow.
#
# Why this is Terraform and not a line in a runbook: until now it was a bullet
# in docs/site/src/content/docs/deploy/ci-pool-projects.md, and on 2026-08-21
# kube-agents-evals-3 joined the Boskos pool without it. Every presubmit that
# leased that project lost the layer cache and cold-built all three images —
# roughly a third of every run in the repository — and it presented as a flake
# rather than as a missing grant, because the failure follows the leased
# project and not the pull request: a re-run that drew one of the other two
# pool projects passed unchanged. The bullet also named the wrong principal, so
# following it would not have fixed the project either.
#
# Why the grant has to be made in kube-agents-prow rather than in the pool
# project: hack/ci-deploy.sh submits the build with
# `gcloud builds submit --project="${PROJECT_ID}"`, where PROJECT_ID is the
# Boskos-LEASED project, and deploy/docker/cloudbuild-ci.yaml declares no
# `serviceAccount:`. The build therefore runs as the leased project's Compute
# Engine default service account, and the warm-cache `docker pull` is a
# cross-project read performed by that identity. No amount of IAM inside the
# pool project can authorise it; only the project that owns the repository can.

data "google_project" "pool" {
  project_id = var.project_id
}

locals {
  # Derived rather than written down, so onboarding kube-agents-evals-4 is a
  # tfvars value and never a hardcoded project number in this file — which is
  # the same class of mistake as the prose step this composition replaces.
  #
  # The legacy Cloud Build agent (${number}@cloudbuild.gserviceaccount.com) is
  # deliberately NOT granted. A build only runs as it when the build config
  # asks for it, and cloudbuild-ci.yaml does not; the entry for it in the live
  # policy is residue from the first pool project. Granting an identity that
  # never makes the call would leave a standing cross-project permission with
  # no caller behind it.
  build_identity = "serviceAccount:${data.google_project.pool.number}-compute@developer.gserviceaccount.com"
}

# ADDITIVE ON PURPOSE. `_iam_member` here, never `_iam_binding` and never
# `_iam_policy` — if you are about to change this, read the next paragraph
# first, because both of the alternatives take CI down.
#
# `us/kube-agents` in kube-agents-prow is a multi-tenant resource. Every pool
# project's build identity holds roles/artifactregistry.reader on it, an
# unrelated project holds the same role, and github-actions@kube-agents-prow
# holds roles/artifactregistry.writer — that last one being how the cache image
# gets published in the first place. This composition, by contrast, is applied
# once per pool project against that project's own state, and knows about
# exactly one of those members.
#
# google_artifact_registry_repository_iam_binding is authoritative for the
# whole role. Using it here would set the reader members to this project's
# service account and nothing else, revoking cache access from every other pool
# project on the next apply — turning a fix for one project into precisely the
# outage it was written to end, for the two projects that already work. With
# one state per project it would also never settle: each project's apply would
# evict the last one's, so only the most recently applied project would ever
# have access.
#
# google_artifact_registry_repository_iam_policy is authoritative for the
# entire policy, so it does all of the above and additionally drops the
# artifactregistry.writer grant — after which the cache image stops being
# published at all and no pool project has anything to pull.
#
# `_iam_member` touches only the one member named below and leaves the rest of
# the policy untouched. For a resource with independent tenants and one state
# per tenant it is the only variant that is correct.
resource "google_artifact_registry_repository_iam_member" "warm_cache_reader" {
  project    = var.cache_project_id
  location   = var.cache_location
  repository = var.cache_repository
  role       = "roles/artifactregistry.reader"
  member     = local.build_identity
}
