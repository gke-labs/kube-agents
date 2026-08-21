# The GitHub token minter's GCP half for ONE project in the Prow evaluation
# pool. The pool projects predate this composition — their cluster, agent GSA,
# and Artifact Registry repository were provisioned before it existed and are
# not managed here — so this is deliberately additive: it provisions only what
# the minter needs and touches nothing else in the project.
#
# Apply it once per pool project, with its own state. What it does NOT do, and
# cannot, is in README.md: creating the project's private GitOps repository,
# installing the GitHub App on it, and importing the App's private key into the
# signing key below. The minter pod fails its readiness probe until that import
# has happened.

locals {
  # Cloud KMS is the only API the module needs that a pool project is not
  # already guaranteed to have on — the list in the pool-project doc predates
  # the minter. iam.googleapis.com is on that list, so it is not repeated here.
  required_apis = toset(["cloudkms.googleapis.com"])
}

resource "google_project_service" "required" {
  for_each = local.required_apis

  project = var.project_id
  service = each.value
  # Matches the full-install composition: the pool project is shared and
  # long-lived, so a destroy here must not turn an API off under whatever else
  # in the project depends on it.
  disable_on_destroy = false
}

module "github_minter" {
  source = "../../modules/github-minter"

  project_id = var.project_id
  location   = var.location
  namespace  = var.namespace

  depends_on = [google_project_service.required]
}
