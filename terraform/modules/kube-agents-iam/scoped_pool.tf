# The scoped service account pool.
#
# GCP has no way to hand out a weakened copy of a credential: Credential Access
# Boundaries are Cloud Storage only and the STS exchange has no actor_token and
# no `act` claim. Google's documented answer is to keep several service accounts
# with different role sets, so that is what this file provisions -- one account
# per GKE cluster, each holding roles/container.viewer under an IAM Condition on
# that one cluster's resource.name.
#
# That the condition reaches Kubernetes *object* operations, and not merely the
# Container API control plane, was measured rather than assumed: conditioned on
# the target cluster `kubectl get pods` succeeds; conditioned on another cluster
# name the same command is refused by Cloud IAM. Note the contrast with the
# container.read-only OAuth scope, which was tested at the same time and does
# NOT constrain object operations -- scopes are not a substitute for this.
#
# Provisioned here and never by the operator. C5 forbids a controller granting
# authority beyond its requester's, and `reconcileRBAC` already mints Kubernetes
# RBAC on every reconcile with no requester ceiling. Extending that habit to GCP
# identities would put the ability to create cloud principals inside the control
# loop the agent is supposed to be bounded by.

locals {
  # The GKE resource name, which is simultaneously the IAM Condition's operand
  # and the key the credential broker looks the account up by. One string, used
  # by both halves, deliberately: every Critical this project has found came
  # from a checker and an enforcer parsing the same input differently, and the
  # cheapest defence is to give them nothing to disagree about.
  #
  # The broker builds this in `scoped_sa_pool.scope_key` and asserts the
  # rendering matches, so a change to either spelling fails a test rather than
  # silently conditioning a grant on a cluster that does not exist.
  scoped_pool = {
    for cluster in var.scoped_clusters :
    "projects/${cluster.project_id}/locations/${cluster.location}/clusters/${cluster.cluster_name}" => {
      project_id   = cluster.project_id
      location     = cluster.location
      cluster_name = cluster.cluster_name

      # Service account ids are 6-30 characters. A project id alone can be 30
      # and a cluster name 40, so the tuple does not fit and truncating it
      # collides -- two clusters of the same name in different projects would
      # land on one account and silently share a credential.
      #
      # So the readable part is cosmetic and the hash is what makes it unique.
      # The hash covers the whole key, project and location included, which is
      # what keeps a second project safe to add later. Trailing hyphens are
      # stripped because truncation can leave one and `ka-foo--<hash>` is merely
      # ugly, while an id ending in a hyphen is invalid.
      account_id = format(
        "ka-%s-%s",
        replace(
          substr(replace(lower(cluster.cluster_name), "/[^a-z0-9]/", "-"), 0, 17),
          "/-+$/",
          ""
        ),
        substr(sha256("projects/${cluster.project_id}/locations/${cluster.location}/clusters/${cluster.cluster_name}"), 0, 8)
      )
    }
  }
}

resource "google_service_account" "scoped" {
  for_each = local.scoped_pool

  # Created in the host project even when the cluster lives elsewhere. An
  # account is a principal; the *grant* is what belongs to the cluster's
  # project, and that is bound below.
  project      = var.project_id
  account_id   = each.value.account_id
  display_name = "Kube-Agents scoped reader: ${each.value.cluster_name} (${each.value.location})"
  description  = "Reads Kubernetes objects in ${each.key} and in no other cluster."
}

resource "google_project_iam_member" "scoped_container_viewer" {
  for_each = local.scoped_pool

  # The cluster's project, not the host project. A GKE cluster's IAM policy
  # lives where the cluster does, so binding here is what lets one pool reach
  # clusters in several projects -- which is why adding a project is a row in a
  # list rather than a redesign.
  project = each.value.project_id
  role    = "roles/container.viewer"
  member  = "serviceAccount:${google_service_account.scoped[each.key].email}"

  condition {
    title       = "scoped-to-${each.value.cluster_name}"
    description = "Limits this grant to ${each.key}. Without it the account would read every cluster in the project, which is the ceiling the pool exists to remove."
    expression  = "resource.name == \"${each.key}\""
  }
}

resource "google_service_account_iam_member" "scoped_token_creator" {
  for_each = local.scoped_pool

  # Bound on the pool member as a *resource*, never at project level.
  #
  # This is the load-bearing line in the file. A project-level grant of
  # roles/iam.serviceAccountTokenCreator would let the agent mint a token for
  # any service account in the project, which is a general escalation primitive
  # and would make the pool decorative -- the agent could simply become
  # something wider. Bound per account, the set of identities the agent can
  # become is exactly the pool, and every member of the pool is narrower than
  # the agent already is.
  #
  # `tests/test_agent_iam_ceiling.py` lists this role as forbidden for the
  # agent's project-level set, and that test must keep passing alongside this
  # binding. The two are not in tension: the role is dangerous at project scope
  # and bounded at resource scope, and that distinction is the whole design.
  service_account_id = google_service_account.scoped[each.key].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.agent.email}"
}
