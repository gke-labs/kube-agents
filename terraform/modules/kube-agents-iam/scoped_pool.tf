# The scoped service account pool.
#
# GCP has no way to hand out a weakened copy of a credential: Credential Access
# Boundaries are Cloud Storage only and the STS exchange has no actor_token and
# no `act` claim. Google's documented answer is to keep several service accounts
# with different role sets, so that is what this file provisions -- one account
# per GKE cluster.
#
# UPDATE 2026-08-12: the accounts hold no IAM grant. The IAM Condition that was
# supposed to scope them grants nothing for Kubernetes object operations, and
# removing the condition without removing the grant would have handed every
# member project-wide container.viewer. Both are gone; see the block below the
# service account resource for the measurement and the replacement.
#
# The seam is worth stating once at the top, because it will be tempting to try
# a third GCP mechanism here: **GCP-layer credential attenuation does not reach
# Kubernetes object authorization.** IAM Conditions are the second mechanism
# measured on that seam. The container.read-only OAuth scope was the first -- it
# gates the Container API control plane and a token carrying it still created a
# namespace. Assume the next one is on the same side of it.
#
# Provisioned here and never by the operator. A controller must not grant
# authority beyond its requester's, and `reconcileRBAC` already mints Kubernetes
# RBAC on every reconcile with no requester ceiling. Extending that habit to GCP
# identities would put the ability to create cloud principals inside the control
# loop the agent is supposed to be bounded by.

locals {
  # The GKE resource name, which is the key the credential broker looks the
  # account up by. One string, spelled once: every Critical this project has
  # found came from a checker and an enforcer parsing the same input
  # differently, and the cheapest defence is to give them nothing to disagree
  # about. It was also the IAM Condition's operand, which is the only part of
  # that mechanism worth keeping.
  #
  # The broker builds this in `scoped_sa_pool.scope_key` and
  # `tests/test_scoped_sa_pool_iam.py` compares the two renderings, so a change
  # to either spelling fails a test rather than silently filing an account under
  # a key no request will ever produce.
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
  # account is a principal; where its authority comes from is a separate
  # question, and as of 2026-08-12 the answer is "nowhere yet" -- see below.
  project      = var.project_id
  account_id   = each.value.account_id
  display_name = "Kube-Agents scoped reader: ${each.value.cluster_name} (${each.value.location})"
  description  = "Pool member for ${each.key}. Holds no IAM grant; authority arrives with per-cluster RBAC."
}

# REMOVED 2026-08-12: google_project_iam_member.scoped_container_viewer
#
# It granted roles/container.viewer in the cluster's project under an IAM
# Condition on resource.name. The condition grants nothing.
#
# Measured. Three accounts, one cluster, same role: unconditioned reads,
# conditioned does not, including the condition naming that exact cluster. Then
# four spellings, all refused, including
# resource.service == "container.googleapis.com" -- which asserts nothing beyond
# "this is a GKE call". Resource attributes are not populated on the path GKE
# uses to authorize Kubernetes object operations. Policy Troubleshooter reports
# MEMBERSHIP_INCLUDED and ROLE_PERMISSION_INCLUDED with the condition
# UNKNOWN_CONDITIONAL and an empty explanation: found, relevant, granting
# nothing.
#
# Deleting only the condition would have been worse than leaving it. That
# binding un-conditioned is project-wide container.viewer on every pool member,
# which is exactly the ceiling this file was written to remove. So the whole
# resource is gone and a member now holds nothing.
#
# The replacement is Kubernetes RBAC rather than IAM. GKE authorizes on IAM *or*
# RBAC, and RBAC is per-cluster natively -- a ClusterRoleBinding in one cluster
# says nothing about any other, so the scoping is structural instead of
# expressed. A service account with no usable IAM container permission was
# measured reading a cluster on the strength of a binding alone. Separate change.
#
# One trap from that measurement, repeated here because it costs an afternoon:
# the binding must name the service account by its **numeric unique ID**. A
# ClusterRoleBinding naming the email is accepted by the API server, shows up in
# `kubectl get clusterrolebinding`, and authorizes nobody. No diagnostic exists.
#
# Until that lands, CREDENTIAL_PROXY_SCOPED_SA_POOL defaults to 0 and the broker
# runs on the ambient credential. The accounts are still provisioned so the
# mapping, the selection and the token-minting path stay exercised.

resource "google_service_account_iam_member" "scoped_token_creator" {
  for_each = local.scoped_pool

  # Bound on the pool member as a *resource*, never at project level.
  #
  # This is the line the whole file turns on. A project-level grant of
  # roles/iam.serviceAccountTokenCreator would let the agent mint a token for
  # any service account in the project, which is a general escalation primitive
  # and would make the pool decorative -- the agent could simply become
  # something wider. Bound per account, the set of identities the agent can
  # become is exactly the pool, and every member of the pool is narrower than
  # the agent already is.
  #
  # `tests/test_scoped_sa_pool_iam.py` lists this role as forbidden for the
  # agent's project-level set, and that test must keep passing alongside this
  # binding. The two are not in tension: the role is dangerous at project scope
  # and bounded at resource scope, and that distinction is the whole design.
  #
  # A text sweep for forbidden roles that does not make the distinction will
  # flag the `role` line below. The resolution is to suppress inside a
  # `resource "google_service_account_iam_member"` block and nowhere else --
  # scope-aware, so a project-scoped reintroduction is still caught. Do not
  # resolve it by dropping the role from the forbidden set, and do not remove
  # the grant: impersonated_credentials needs tokenCreator on the target, so
  # without it the pool cannot mint at all.
  service_account_id = google_service_account.scoped[each.key].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.agent.email}"
}
