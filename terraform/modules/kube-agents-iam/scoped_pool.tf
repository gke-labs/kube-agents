# The scoped service account pool.
#
# GCP has no way to hand out a weakened copy of a credential: Credential Access
# Boundaries are Cloud Storage only and the STS exchange has no actor_token and
# no `act` claim. Google's documented answer is to keep several service accounts
# with different role sets, so that is what this file provisions -- one account
# per project in the scope.
#
# Per project, not per cluster. The estate this is written for runs one
# cluster per project, so a per-cluster pool and a per-project pool are the
# same size there, and the project is the IAM unit the declaration is written
# in: `spec.scope` names projects, the scope's read roles are bound per
# project (scope.tf), and the pool is derived from that same declaration
# rather than hand-listed beside it, so an explicit project gets its account
# when it gets its grant. Two clusters in one project share an account by
# design (docs/designs/multi-project-scope.md §6): the blast radius of a
# compromised sandbox is the project, which is the unit the customer's estate
# is cut in.
#
# What the plan can list is what gets an account: the host project, each
# `scope.projects` entry less an exact `exclude.projects` entry, and each
# selector's members (`local.scope_listed_projects`), plus each declared
# folder's and organisation's members, which the kube-agents-scope-resolver
# module lists at plan time with the same Asset Inventory search the reconcile
# runs, only while the pool is armed, and the composition hands in as
# `scope_container_members` (design §6). A container's member gets an account
# on that apply and nothing else: no per-project binding, because the
# container-level grant (scope.tf) is inherited, and no place in the
# resolved-set count, because containers come last in the reconcile's order
# and are not counted at plan. So the container grant and discovery stay
# zero-touch while pool membership under a container lags: a project created
# beneath it since the last apply is refused by the broker until the next
# apply lists it. The listing is one answer from an eventually consistent
# index, and the plan carries no state to grace it with, unlike the
# reconcile's day for a member the index omits: a project the index leaves
# out on one plan loses its account on that apply and gets a new one, under
# a new unique ID, on the next (the resolver warns when a container answers
# no member at all, the shape an index gap most often takes); a project
# pinned in `scope.projects` does not depend on the index. An exact
# `exclude.projects` entry drops a member from the pool; a glob is the
# reconcile's alone. The pool is armed by
# `scoped_pool_enabled` alone, off by default and independent of the scope, so
# declaring `projects` arms nothing.
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
  # The keys the resolver's container_members output uses, one per declared
  # container; the precondition in main.tf requires each in
  # scope_container_members while the pool is armed.
  scoped_pool_container_keys = concat(
    [for folder in local.scope_folders : "folders/${folder}"],
    [for organization in local.scope_organizations : "organizations/${organization}"],
  )
  scoped_pool_containers_listed = alltrue([for key in local.scoped_pool_container_keys : contains(keys(var.scope_container_members), key)])

  # The containers' members, less the host project (listed already) and an
  # exact exclude.projects entry. Only the declared containers' entries are
  # read, so a stale key in the input adds nothing.
  scoped_pool_container_members = toset([
    for project in flatten([for key in local.scoped_pool_container_keys : lookup(var.scope_container_members, key, [])]) : project
    if project != var.project_id && !contains(var.scope.exclude.projects, project)
  ])

  # The pool's set: what the plan lists for the resolved-set count plus the
  # containers' members, which join the pool and nothing else.
  # `tests/test_scoped_sa_pool_iam.py` pins this union, so a member cannot
  # reach the pool another way.
  scoped_pool_projects = setunion(
    local.scope_listed_projects,
    local.scoped_pool_container_members,
  )

  # Keyed on the bare project id, which is the key the credential broker looks
  # the account up by (`scoped_sa_pool.py` keys its members on `projectId`).
  # One string, spelled once: every Critical this project has found came from
  # a checker and an enforcer parsing the same input differently, and the
  # cheapest defence is to give them nothing to disagree about.
  # `tests/test_scoped_sa_pool_iam.py` pins that the for_each iterates the
  # pool's projects and keys on the id, so a change here fails a test rather
  # than silently filing an account under a key no request will ever produce.
  scoped_pool = var.scoped_pool_enabled ? {
    for project_id in local.scoped_pool_projects :
    project_id => {
      project_id = project_id

      # Service account ids are 6-30 characters and a project id alone can be
      # 30, so the readable part is cosmetic and the hash is what makes it
      # unique: eight hex characters of sha256 over the install's own
      # service_account_id and the project's resource name. The project keeps
      # two projects with the same first seventeen characters on two accounts;
      # the install's id keeps two installs in one host project (variables.tf:
      # "a second install in the same project must set its own") on two
      # members for a project both list, the host project above all, which
      # every armed install lists. Without it the second install's apply
      # would stop on a 409 creating the host's member. The ownership check
      # the installer runs before an apply covers the pool through this: two
      # installs can only derive the same member id by sharing the agent's
      # service_account_id, which that check already refuses. Trailing
      # hyphens are stripped because truncation can leave one and an id
      # ending in a hyphen is invalid.
      account_id = format(
        "ka-%s-%s",
        replace(
          substr(replace(lower(project_id), "/[^a-z0-9]/", "-"), 0, 17),
          "/-+$/",
          ""
        ),
        substr(sha256("${var.service_account_id}/projects/${project_id}"), 0, 8)
      )
    }
  } : {}
}

resource "google_service_account" "scoped" {
  for_each = local.scoped_pool

  # Created in the host project even when the member's project is elsewhere.
  # An account is a principal; where its authority comes from is a separate
  # question, and as of 2026-08-12 the answer is "nowhere yet" -- see below.
  # The cap on how many this creates, scoped_pool_max_accounts against the
  # host project's service-account quota, is a precondition on the agent's
  # account in main.tf, beside the scope's, so a pool past it is refused once
  # rather than once per member.
  project      = var.project_id
  account_id   = each.value.account_id
  display_name = "Kube-Agents scoped reader: ${each.value.project_id}"
  # The description is read by the installer's ownership check (installer_common.sh,
  # check_service_account_ownership): it lists members by this marker to refuse an apply
  # that would 409 on a member this install's state does not own.
  description = "Pool member of ${var.service_account_id} for projects/${each.value.project_id}. Holds no IAM grant; authority arrives with per-cluster RBAC."
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
# runs on the ambient credential. The accounts are provisioned only while
# scoped_pool_enabled arms the pool, which keeps the mapping, the selection
# and the token-minting path exercisable without creating an account in every
# install that declares a scope.

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
