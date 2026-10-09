locals {
  # iam, monitoring, and logging are here because Terraform must enable every
  # API its own resources call, where gcloud enables them implicitly.
  base_apis = [
    "container.googleapis.com",
    "cloudkms.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    # Unconditional: the cluster is created with the Backup for GKE agent
    # enabled whether or not a BackupPlan follows, and the addon cannot be
    # enabled without the API.
    "gkebackup.googleapis.com",
    "developerknowledge.googleapis.com",
  ]
  pubsub_apis = (var.enable_google_chat || var.enable_pubsub_platform || var.enable_stockout_investigator || var.enable_drift_pubsub) ? [
    "pubsub.googleapis.com",
  ] : []
  chat_apis = var.enable_google_chat ? [
    "chat.googleapis.com",
    "gsuiteaddons.googleapis.com",
  ] : []
  # Only when a folder or organisation is declared: the reconcile resolves a
  # container's members with one Cloud Asset Inventory search, and an install
  # that names explicit projects alone never calls the API and must not fail
  # under an organisation policy that forbids it
  # (docs/designs/multi-project-scope.md §4).
  scope_apis = length(var.scope.folders) + length(var.scope.organizations) > 0 ? [
    "cloudasset.googleapis.com",
  ] : []
  # Only when a Shared VPC host is declared: the resolver's getXpnResources read
  # at plan time and the reconcile's lookup of the host's service projects use
  # the Compute API in project_id (a Metrics Scope's reads use the Monitoring
  # and Resource Manager APIs, in base_apis). A project that hosts a GKE
  # cluster has it on already; naming it here is what makes the composition own
  # every API the selectors' reads use, as the installer's pre-enablement and
  # the documents say it does.
  selector_apis = length(var.scope.shared_vpc_hosts) > 0 ? [
    "compute.googleapis.com",
  ] : []

  use_vertex     = var.model_provider == "vertex_ai"
  vertex_project = var.vertex_project_id != "" ? var.vertex_project_id : var.project_id
  # The two resources that live in vertex_project rather than project_id. A
  # cross-project serving project is often one the applying identity holds no
  # IAM on, and the composition cannot tell that apart from a project where the
  # grant simply has not happened yet — so the operator says which it is.
  manage_vertex_serving = local.use_vertex && var.vertex_manage_serving_project
  # Not var.location: a model is only callable from a location that serves it,
  # and the cluster's is often not one — on a zonal cluster it is not even a
  # valid Vertex location. Mirrors DEFAULT_VERTEX_LOCATION in
  # scripts/installer/installer_common.sh.
  vertex_location = var.vertex_location != "" ? var.vertex_location : "global"
  litellm_ksa     = "kubeagents-litellm"

  # The minter chart values need the GitOps repository split into owner and
  # name. Accepts the same forms integration.github.gitRepo takes: owner/repo,
  # or a github.com URL. Anything else leaves both parts empty, which the
  # helm_release precondition rejects when the minter is enabled.
  github_repo_path  = trimsuffix(trimprefix(trimprefix(trimprefix(var.github_repo, "https://"), "http://"), "github.com/"), ".git")
  github_repo_parts = split("/", local.github_repo_path)
  github_org        = length(local.github_repo_parts) == 2 ? local.github_repo_parts[0] : ""
  github_repo_name  = length(local.github_repo_parts) == 2 ? local.github_repo_parts[1] : ""

  # A GitLab GitOps repository is declared through the forges/repositories
  # lists, never the github alias: one gitlab forge, carrying the Secret the
  # installer creates, and the repository on it as the gitops role. The
  # operator derives the broker's allowedPaths from the repository's group.
  # Two lists, each merged in on its own: one conditional carrying both would
  # have no single type to unify with its empty arm.
  gitops_is_gitlab = var.gitops_forge == "gitlab"
  gitlab_forges = [merge(
    { name = "gitlab", provider = "gitlab", credentialsRef = { name = var.gitlab_token_secret_name } },
    var.gitops_host != "" ? { host = var.gitops_host } : {}
  )]
  gitlab_repositories = [{ forge = "gitlab", repository = var.gitlab_repo, role = "gitops" }]

  # The CR's spec.integration, as one local so the tests can read it.
  platform_agent_integration = merge(
    var.enable_google_chat ? {
      googleChat = {
        enabled          = true
        topicName        = module.chat_pubsub[0].topic_name
        subscriptionName = module.chat_pubsub[0].subscription_name
        allowedUsers     = var.google_chat_allowed_users
        homeChannel      = var.google_chat_home_channel
        mode             = var.google_chat_mode
      }
    } : {},
    var.enable_slack ? {
      slack = {
        enabled         = true
        allowedUsers    = var.slack_allowed_users
        homeChannel     = var.slack_home_channel
        homeChannelName = var.slack_home_channel_name
      }
    } : {},
    local.gitops_is_gitlab ? { forges = local.gitlab_forges } : {},
    local.gitops_is_gitlab ? { repositories = local.gitlab_repositories } : {},
    (!local.gitops_is_gitlab && (local.github_org != "" || var.github_repo != "")) ? {
      github = merge(
        local.github_org != "" ? { org = local.github_org } : {},
        var.github_repo != "" ? { gitRepo = var.github_repo } : {}
      )
    } : {}
  )

  required_apis = toset(concat(local.base_apis, local.pubsub_apis, local.chat_apis, local.scope_apis, local.selector_apis))

  # The agent's GCP IAM permission-set bundle, kept verbatim so the two install
  # paths hand the agent the same authority. Kubernetes RBAC is read-only
  # alongside it; see the security-and-iam reference.
  #
  # There is deliberately one bundle and no admin one. GKE authorizes an action
  # if either IAM or Kubernetes RBAC allows it, so a role like
  # roles/container.admin authorizes the agent through IAM regardless of how
  # narrow its KSA is, and the container.clusters.impersonate it carries applies
  # to every cluster in the project. A deployment that needs broader roles names
  # them in project_roles, which puts the grant in the caller's Terraform where
  # it is reviewed.
  read_only_roles = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
    "roles/compute.viewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/iam.serviceAccountUser",
    "roles/iam.securityReviewer",
    "roles/mcp.toolUser",
    "roles/serviceusage.serviceUsageConsumer",
  ]

  # An explicit project_roles list always wins, so an existing configuration
  # that set it keeps its roles regardless of permission_set.
  agent_project_roles = var.project_roles != null ? var.project_roles : local.read_only_roles

  # Only non-empty credential keys end up in the Secret, so an unset optional
  # provider key does not create an empty entry.
  optional_credentials = {
    for key, value in {
      API_SERVER_KEY = var.api_server_key
      # Generated rather than asked for: neither value means anything to an
      # operator, and both are scoped to the agent pod. Held in Terraform state
      # rather than left to the chart's own generation so that `terraform apply`
      # is idempotent without needing a cluster read — rotating the salt would
      # re-anonymise every user, breaking the link between their past sessions
      # and their future ones. The variables outrank the generation for the
      # one case state cannot cover: adopting a cluster whose Secret already
      # holds live values (the installer recovers them from it). With empty
      # state and empty variables, a fresh salt here would sever every user's
      # session history.
      SESSION_KV_API_KEY = var.session_kv_api_key != "" ? var.session_kv_api_key : random_password.session_kv_api_key.result
      SESSION_KV_SALT    = var.session_kv_salt != "" ? var.session_kv_salt : random_password.session_kv_salt.result
      # The agent's half of the shell sandbox keypair, generated for the same
      # reason: nobody has to choose its value. The chart copies the public
      # half into <name>-shell-authorized-keys for the sandbox to mount.
      SANDBOX_SSH_PRIVATE_KEY = tls_private_key.sandbox_ssh.private_key_openssh
      SANDBOX_SSH_PUBLIC_KEY  = tls_private_key.sandbox_ssh.public_key_openssh
      ANTHROPIC_API_KEY       = var.anthropic_api_key
      GEMINI_API_KEY          = var.gemini_api_key
      OPENAI_API_KEY          = var.openai_api_key
    } : key => value if value != ""
  }

  # Slack is the exception to that filter, and has to be: with the integration
  # enabled the CR names both keys in a secretKeyRef the operator passes
  # through verbatim (no `optional: true` — see defaultSecretRef in
  # manifest_helpers.go), so a key missing from the Secret does not disable
  # Slack, it holds the whole agent pod in CreateContainerConfigError. The
  # tokens legitimately arrive after the first apply, because creating the
  # Slack app is a manual step, so an empty value has to reach the Secret as
  # an empty value.
  slack_credentials = var.enable_slack ? {
    SLACK_BOT_TOKEN = var.slack_bot_token
    SLACK_APP_TOKEN = var.slack_app_token
  } : {}

  credentials = merge(local.optional_credentials, local.slack_credentials)

  # One resources block for all three cert-manager Deployments, kept as a
  # single local so the three copies cannot drift apart.
  cert_manager_resources = {
    requests = {
      cpu    = "10m"
      memory = "32Mi"
    }
    limits = {
      cpu    = "100m"
      memory = "128Mi"
    }
  }

  # The registry third-party images are pulled from on a mirrored install:
  # third_party_image_registry, falling back to image_registry, the same
  # precedence the chart's kube-agents.thirdPartyImageRegistry helper applies.
  # Empty means the upstream registries.
  third_party_registry = trimsuffix(
    var.third_party_image_registry != "" ? var.third_party_image_registry : var.image_registry,
    "/"
  )

  # Mirrored image overrides for helm_release.cert_manager below. Destination
  # names follow images.json (<prefix>/<name>:<tag>) — the contract
  # `make mirror-images` writes. The tag stays the chart's own appVersion,
  # which is what images.json pins for the cert-manager entries. Empty when not mirroring,
  # so a default install's release values are byte-identical.
  cert_manager_mirror_values = local.third_party_registry == "" ? [] : [yamlencode({
    image      = { repository = "${local.third_party_registry}/cert-manager-controller" }
    webhook    = { image = { repository = "${local.third_party_registry}/cert-manager-webhook" } }
    cainjector = { image = { repository = "${local.third_party_registry}/cert-manager-cainjector" } }
    acmesolver = { image = { repository = "${local.third_party_registry}/cert-manager-acmesolver" } }
    startupapicheck = {
      image = { repository = "${local.third_party_registry}/cert-manager-startupapicheck" }
    }
  })]

  # Whether the CR this composition renders will say the drift detector is on,
  # by either door that reaches the field. extra_helm_values is a second values
  # document Helm deep-merges over the computed one (helm_release.kube_agents
  # below), and its own description names the harness knobs as what it is for,
  # so platformAgent.harness.driftDetector.enabled set there starts the detector
  # without enable_drift_detector ever being read. The preconditions below are
  # about what the detector will then do, not about which variable asked for it,
  # so they test this rather than the flag.
  #
  # tobool inside the try, not outside: a leaf the type cannot hold ("yes", 1)
  # then fails the lookup and leaves this false, where an uncaught conversion
  # would abort the plan with a type error instead of the message the
  # precondition owes. Such a value reaches the CRD, which rejects it.
  #
  # `== true` inside the try for the same reason, against the one input that
  # is neither a boolean nor a conversion failure: null. tobool(null) does not
  # error -- it converts, and a null of type bool comes back -- so the try has
  # nothing to catch and hands a null to `||`, whose arguments may not be
  # null. The plan then aborts on this local, naming no variable, and it does
  # so whichever way enable_drift_detector is set: OpenTofu evaluates both
  # operands. That leaf is the chart's own idiom, not a typo --
  # charts/kube-agents/values.yaml ships `driftDetector.enabled: null`,
  # values.schema.json types it ["boolean","null"], and the chart README says
  # null is how a knob is omitted so the CRD's default applies -- so an
  # operator copying the block into extra_helm_values to set gitopsManagers,
  # which this composition does not expose, writes exactly that. `null == true`
  # is false rather than null, so the comparison turns the one value the
  # conversion accepts and the operator did not ask for into "not requested".
  drift_detector_requested = (
    var.enable_drift_detector ||
    try(tobool(var.extra_helm_values.platformAgent.harness.driftDetector.enabled) == true, false)
  )

  # The same leaf, read a second time, for what the local above cannot say:
  # extra_helm_values does not only ask for the detector, it can also
  # countermand enable_drift_detector. The block above renders `enabled: true`
  # into the computed document and extra_helm_values is the second one, so the
  # provider's later-document-wins merge hands whatever the leaf holds
  # straight to the CR. A null deletes the field -- Helm's coalesce drops a
  # nil user key and the chart's compactFields drops what is then missing --
  # and the CRD's default false applies; a false arrives as false. Either way
  # enable_drift_detector is true in the tfvars, the sink, topic and
  # subscription are provisioned and billing, the CR does not start the
  # detector, and nothing says so. That is the state the two preconditions
  # below already refuse, reached by a third door, and the only one of the
  # three where the operator did ask for the detector.
  #
  # `!= true` rather than a test for null, because false countermands exactly
  # as null does and arrives by the same paste. A leaf the type cannot hold
  # ("yes", "true") is refused here too, which is a better message than the
  # one values.schema.json gives for it further in and arrives before the
  # apply starts. Comparing across types is safe -- a string is not equal to a
  # bool, so it lands on `!= true` rather than raising.
  #
  # can() as well as try(), to tell a leaf that is present and null from one
  # that is absent. Absent is the ordinary case and must not be refused:
  # nothing overrides the computed document and `enabled: true` reaches the
  # CR.
  #
  # The second clause is the same door one level up: `driftDetector = null`
  # reaches the identical end state and the first clause cannot see it, because
  # attribute access on null raises and the can() that tolerates an absent leaf
  # swallows a present null parent with it. Coalesce deletes a nil-valued key
  # where the chart default is a table, and the template's `| default dict`
  # then renders no driftDetector block at all -- no enabled, and no
  # subscription either -- so the ingress bills and nothing reads it.
  #
  # The "absent" sentinel is what separates "the key is there and is null" from
  # "the key is not there": try returns the sentinel in the second case, and a
  # sentinel that is not null cannot collide with the value being tested for. A
  # non-null non-map parent is deliberately not caught here -- coalesce keeps a
  # scalar and values.schema.json types driftDetector as object, so the release
  # fails validation with a message of its own.
  extra_helm_values_countermands_detector = (
    var.enable_drift_detector && (
      (can(var.extra_helm_values.platformAgent.harness.driftDetector.enabled) &&
      try(var.extra_helm_values.platformAgent.harness.driftDetector.enabled, null) != true) ||
      try(var.extra_helm_values.platformAgent.harness.driftDetector, "absent") == null
    )
  )
}

# A warning rather than a precondition: an install that enables Slack before
# the Slack app exists is a legitimate order of operations, and the empty keys
# above keep the pod running until the tokens land. What is not legitimate is
# not being told.
check "slack_tokens_present" {
  assert {
    condition     = !var.enable_slack || (var.slack_bot_token != "" && var.slack_app_token != "")
    error_message = "enable_slack is true but slack_bot_token and/or slack_app_token is empty. The agent pod will start and Slack will stay silent until both tokens are set in the credentials Secret."
  }
}

# Bearer token for the pod-local Session KV server on 127.0.0.1:8699. Both the
# sandbox container (which serves and calls it) and the credential-proxy
# container (whose event watcher posts to it) read this one value.
resource "random_password" "session_kv_api_key" {
  length  = 48
  special = false
}

# HMAC salt for pseudonymising chat identities before they reach session
# metadata, audit logs, or OTel spans.
resource "random_password" "session_kv_salt" {
  length  = 48
  special = false
}

# The agent's SSH keypair for the shell sandbox (#737 Part B). The agent pod
# holds the private half and dials the sandbox with it; the sandbox authorises
# it and holds nothing else.
#
# tls_private_key rather than shelling out to ssh-keygen, which is what the
# installer scripts have to do: private_key_openssh and public_key_openssh give
# both halves in the exact encodings sshd and `ssh -i` want, with no local-exec
# and no provisioner. Held in Terraform state like the two passwords above, so
# `terraform apply` is idempotent without reading the cluster — and so a plan
# never proposes a new pair, which would lock the agent out of a running
# sandbox until that pod restarted.
resource "tls_private_key" "sandbox_ssh" {
  algorithm = "ED25519"
}

resource "google_project_service" "required" {
  for_each = local.required_apis

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

module "gke_cluster" {
  source = "../../modules/gke-cluster"

  project_id                 = var.project_id
  cluster_name               = var.cluster_name
  cluster_mode               = var.cluster_mode
  create_cluster             = var.create_cluster
  accept_no_network_policy   = var.accept_no_network_policy
  location                   = var.location
  deletion_protection        = var.deletion_protection
  release_channel            = var.release_channel
  enable_database_encryption = var.enable_database_encryption
  kms_keyring_name           = var.kms_keyring_name
  kms_key_name               = var.kms_key_name
  allow_external_dns_traffic = var.allow_external_dns_traffic
  enable_backup_agent        = var.enable_backup_agent
  enable_gvisor_node_pool    = var.enable_gvisor_node_pool
  gvisor_pool_name           = var.gvisor_pool_name

  resource_labels = {
    "kube-agents-host" = "true"
  }

  # module.drift_pubsub is here for the DESTROY order, not the apply one.
  # Terraform destroys dependents before dependencies, so naming the ingress
  # here is what puts the cluster's teardown ahead of the topic's deletion.
  # The cluster emits mutating audit records for as long as its control plane
  # is up -- measured at ~4 minutes past the point the topic used to be deleted
  # -- and each one that lands on a missing topic mails every project owner
  # (#2426). The module's sink_drain cannot cover that: it is a fixed timer and
  # the teardown's length varies with the cluster. This edge removes the race
  # instead of widening the margin, leaving the drain to do the job it is
  # actually sized for, the Log Router's own propagation lag once the cluster
  # has gone quiet -- which is also the whole of it when create_cluster is
  # false and no cluster is in this state's destroy graph at all.
  #
  # On apply it means the ingress is built before the cluster. That is correct
  # -- a sink with no cluster to watch exports nothing -- but it is not free,
  # in two ways worth knowing before anyone widens the edge.
  #
  # It serialises what used to run in parallel. The cluster is the install's
  # long pole, and it now waits for the whole ingress, including
  # time_sleep.logging_identity's own wait. A greenfield install with
  # enable_drift_pubsub on is longer by roughly the ingress's critical path.
  # The ingress's documented "Service account ... does not exist" failure also
  # now aborts before the cluster is requested rather than after it is built,
  # which makes the retry cheaper and the first failure earlier.
  #
  # And a module-level depends_on defers every data source inside the module
  # whenever a target has a planned change -- the mechanism the drift_pubsub
  # call below and the scope_resolver call document for themselves. Here that
  # reaches data.google_container_cluster.existing, which exists only when
  # create_cluster is false, so on a bring-your-own-cluster install an apply
  # whose only change is in the ingress defers that read to apply time and
  # with it the Workload Identity and NetworkPolicy postconditions, which are
  # written to refuse a plan rather than fail partway through one. Narrowing
  # the edge does not avoid it: Terraform resolves an indexed depends_on
  # reference to the whole resource, and a root depends_on cannot name
  # anything finer than a module. It is the price of the ordering rather than
  # an oversight, and it is confined to the case where the ordering buys
  # nothing anyway -- an external cluster outlives the destroy, so the drain
  # is doing the work there.
  depends_on = [google_project_service.required, module.drift_pubsub]
}

module "gke_backup_plan" {
  source = "../../modules/gke-backup-plan"
  count  = var.enable_gke_backup_plan ? 1 : 0

  project_id          = var.project_id
  cluster_name        = module.gke_cluster.cluster_name
  location            = module.gke_cluster.cluster_location
  selected_namespaces = [var.namespace]
  cron_schedule       = var.backup_cron_schedule
  backup_retain_days  = var.backup_retain_days
  encryption_key      = var.backup_encryption_key
}

# The two scope selectors that are not containers, resolved to projects at
# plan time, and -- while the scoped service account pool is armed -- each
# declared folder's and organisation's members, listed for the pool with the
# reconcile's own Asset Inventory search. Called with no depends_on and no
# input a managed resource produces, on purpose: module.kube_agents_iam below
# carries a module-level depends_on, which would defer a data source inside it
# to apply time on a first install or on any upgrade that enables an API, and
# the bindings keyed on the resolved projects would then fail the plan as
# unknown. Here the reads happen on every plan, and a read the planning
# identity cannot make fails the plan with the selector or container named,
# before anything is applied.
module "scope_resolver" {
  source = "../../modules/kube-agents-scope-resolver"

  shared_vpc_hosts = var.scope.shared_vpc_hosts
  metrics_scopes   = var.scope.metrics_scopes
  exclude_projects = var.scope.exclude.projects
  member_cap       = var.scope.max_projects
  # The containers are read for the pool alone (their grant is on the
  # container and inherited, so nothing is bound per member): listed only
  # while the pool is armed, and no container is read while it is off.
  folders                = var.scope.folders
  organizations          = var.scope.organizations
  list_container_members = var.scoped_pool_enabled
  # The consumer project of the reads: the management project, whose APIs
  # this composition enables (and install.sh pre-enables before a first apply,
  # since the reads run in the plan).
  quota_project = var.project_id
}

module "kube_agents_iam" {
  source = "../../modules/kube-agents-iam"

  project_id    = var.project_id
  namespace     = var.namespace
  project_roles = local.agent_project_roles
  scope         = var.scope
  # The scoped service account pool, derived in the module from the same
  # scope object: armed by the switch alone, never by the scope.
  scoped_pool_enabled      = var.scoped_pool_enabled
  scoped_pool_max_accounts = var.scoped_pool_max_accounts
  # What the selectors resolved to, from the module above; the IAM module
  # binds these and refuses a selector with no entry.
  scope_selector_members = module.scope_resolver.members
  # What each declared container held while the pool is armed, from the same
  # module; the IAM module gives each a pool account and nothing else, and
  # refuses an armed pool beside a container with no entry. Empty while the
  # pool is off.
  scope_container_members = module.scope_resolver.container_members
  service_account_id      = var.agent_service_account_id
  # The KSA half of the Workload Identity member; the same variable is the
  # chart's platformAgent.security.serviceAccountName below. The variable's
  # description in variables.tf says why it exists and what bounds it.
  ksa_name = var.agent_ksa_name

  # module.gke_cluster, and not only the API enablements, because the module's
  # workload_identity binding names the pool as an interpolated string
  # ("serviceAccount:${var.project_id}.svc.id.goog[...]") rather than as a
  # reference to the cluster. Terraform therefore sees no edge between them and
  # starts the binding as soon as the service account exists, roughly nine
  # minutes before an Autopilot cluster finishes. The pool does not exist until
  # the project's first Workload-Identity-enabled cluster does, so on a project
  # that has never had one the apply fails with "Identity Pool does not exist".
  # It survived this long because a pool outlives the cluster that created it:
  # every project the composition had been applied to already had one.
  depends_on = [google_project_service.required, module.gke_cluster]
}

# ─── Vertex AI gateway identity (model_provider = "vertex_ai") ────────────────
# Vertex has no API key: the LiteLLM gateway calls it as this GSA through
# Workload Identity. The GSA lives in project_id; the aiplatform.user grant and
# the API enablement go to the serving project, which may be a different one —
# and when it is one the applying identity cannot administer,
# vertex_manage_serving_project = false leaves both to the operator.
resource "google_project_service" "vertex_ai" {
  count = local.manage_vertex_serving ? 1 : 0

  project            = local.vertex_project
  service            = "aiplatform.googleapis.com"
  disable_on_destroy = false
}

module "litellm_vertex_iam" {
  source = "../../modules/kube-agents-iam"
  count  = local.use_vertex ? 1 : 0

  project_id         = var.project_id
  service_account_id = var.litellm_service_account_id
  display_name       = "Kube-Agents LiteLLM Vertex AI Service Account"
  namespace          = var.namespace
  ksa_name           = local.litellm_ksa
  # Granted below instead, so a cross-project vertex_project_id works.
  project_roles = []

  # module.gke_cluster for the reason module.kube_agents_iam's depends_on
  # states in full: the module's workload_identity binding names the pool as
  # an interpolated string, so Terraform sees no edge to the cluster whose
  # creation brings the pool into existence, and on a project that has never
  # held a Workload-Identity-enabled cluster the binding fails with "Identity
  # Pool does not exist". This instantiation went without the edge for as long
  # as the pool path never set model_provider = "vertex_ai" -- the first
  # onboarding run that does (provision_ci_pool_project.sh, Step 2.1 building
  # the project's first cluster) is exactly the case that races.
  depends_on = [google_project_service.required, module.gke_cluster]
}

resource "google_project_iam_member" "litellm_vertex_user" {
  #checkov:skip=CKV_GCP_41:LiteLLM gateway uses dedicated service account for Vertex AI inference
  #checkov:skip=CKV_GCP_42:Service account is granted non-admin aiplatform.user role
  #checkov:skip=CKV_GCP_46:Dedicated custom service account used for LiteLLM workload identity
  #checkov:skip=CKV_GCP_49:LiteLLM gateway uses dedicated service account for Vertex AI inference
  #checkov:skip=CKV_GCP_117:Vertex AI user role required for LiteLLM gateway inference access
  count = local.manage_vertex_serving ? 1 : 0

  project = local.vertex_project
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${module.litellm_vertex_iam[0].service_account_email}"

  depends_on = [google_project_service.vertex_ai]
}

module "chat_pubsub" {
  source = "../../modules/chat-pubsub"
  count  = var.enable_google_chat ? 1 : 0

  project_id                  = var.project_id
  agent_service_account_email = module.kube_agents_iam.service_account_email
  topic_name                  = var.chat_topic_name
  subscription_name           = var.chat_subscription_name

  depends_on = [google_project_service.required]
}

# The drift detector's audit-log ingress: Log Router sink, drift-audit topic
# and pull subscription, and the sink-writer and detector IAM. The three names
# are composition variables, as the stockout trio's are, because a second
# install in the project has to be able to name its own -- lifecycle.sh's
# guard_drift_adoption refuses an apply that would otherwise find all three
# there and import them; the module's defaults decide the rest (retention, backoff, and
# the cluster scope, every GKE cluster in the project). The consumer,
# k8s-operator/cmd/drift-detector, ships in the images and starts in the
# gateway pod when the PlatformAgent sets spec.harness.driftDetector.enabled;
# this flag provisions its input and passes the subscription's name into that
# block (the harness values below), and enable_drift_detector is what starts
# the consumer (docs/designs/drift-detection.md). The two are separate so that
# the ingress can be provisioned on its own; the other order is refused by a
# helm_release precondition below, because the harness block that carries
# enabled is written only when this flag is on, so the composition would
# otherwise accept the second variable and silently do nothing with it.
module "drift_pubsub" {
  source = "../../modules/drift-pubsub"
  count  = var.enable_drift_pubsub ? 1 : 0

  # No kube_agents_iam input here on purpose, and the detector's subscription
  # grants are below rather than inside the module. That one argument used to
  # make this module a dependent of kube_agents_iam, which depends on
  # gke_cluster -- so Terraform, destroying dependents first, tore the whole
  # ingress down ahead of the cluster. The topic went at t+124s while the
  # control plane kept emitting matching audit records until t+366s, and every
  # one of them mailed the project's owners (#2426's destroy half). Without the
  # argument this module depends on nothing cluster-side, so gke_cluster can
  # depend on IT instead and the topic is destroyed last of all.
  project_id        = var.project_id
  topic_name        = var.drift_pubsub_topic
  subscription_name = var.drift_pubsub_subscription
  sink_name         = var.drift_pubsub_sink
  topic_publishers  = var.drift_pubsub_topic_publishers

  # Exposed because the module's sink postcondition names them in its error:
  # a project whose sink reports some other writer identity fails that check on
  # every plan from then on, and the operator it tells to set the override has
  # only this composition to set it in.
  sink_writer_identity_override = var.drift_pubsub_sink_writer_identity_override
  sink_drain_duration           = var.drift_pubsub_sink_drain_duration

  # The module's two timers are both chosen margins against something GCP
  # documents no bound for, so both are exposed: an operator whose apply still
  # fails on an unbindable Logging agent, or whose teardown still mails, has
  # only this composition to lengthen them in.
  logging_identity_propagation_duration = var.drift_pubsub_logging_identity_propagation_duration

  # Defers data.google_project.this inside the module to apply time whenever
  # any member of required_apis has a planned change, which makes the sink's
  # publish grant "(known after apply)" and -- member being ForceNew -- plans
  # it for replacement while the sink stays live. See the note on the grant in
  # the module's main.tf; narrowing this list to the three APIs the module
  # needs does not help, because Terraform resolves an indexed depends_on
  # reference to the whole resource.
  depends_on = [google_project_service.required]
}

# The detector's access to its own subscription. These lived in the module
# until the destroy ordering above made them the wrong thing to keep there:
# they are the only part of the ingress that names the agent's GSA, so holding
# them here is what lets the module stay clear of kube_agents_iam.
#
# They are also the one part that genuinely cannot be reordered. Pub/Sub IAM
# validates that a principal exists -- a grant naming a service account that
# has not been created yet fails outright, measured on #2480 against the
# Logging service agent -- so these have to follow the GSA however the rest is
# arranged. Referencing module.kube_agents_iam is what puts them there.
#
# `moved` from the module rather than recreated: the bindings are identical, and
# an address change alone would otherwise plan a destroy/create of the
# detector's own access on every existing install.
moved {
  from = module.drift_pubsub[0].google_pubsub_subscription_iam_member.detector_subscriber
  to   = google_pubsub_subscription_iam_member.detector_subscriber[0]
}

moved {
  from = module.drift_pubsub[0].google_pubsub_subscription_iam_member.detector_viewer
  to   = google_pubsub_subscription_iam_member.detector_viewer[0]
}

resource "google_pubsub_subscription_iam_member" "detector_subscriber" {
  count = var.enable_drift_pubsub ? 1 : 0

  project      = var.project_id
  subscription = module.drift_pubsub[0].subscription_id
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${module.kube_agents_iam.service_account_email}"
}

# roles/pubsub.subscriber covers consuming messages but not reading the
# subscription's own metadata. It grants subscriptions.consume, snapshots.seek,
# and topics.attachSubscription -- notably not subscriptions.get. A client that
# confirms the subscription exists before pulling (the chat adapter's
# _check_subscription_exists) needs viewer as well, and without it fails with a
# PermissionDenied that reads nothing like a missing grant.
#
# The drift detector makes one: a startup subscriptions.get reading the
# configured ackDeadlineSeconds, so it can warn when --batch-join-budget would
# hold a batch past it. This grant is what keeps that call from failing. It is
# advisory on the detector's side -- a probe that is denied logs that the budget
# went unchecked and the loop pulls anyway -- so removing viewer degrades the
# warning rather than breaking ingestion. Viewer would stay regardless: `gcloud
# pubsub subscriptions describe` needs it, and that is the first command anyone
# runs against an empty topic.
resource "google_pubsub_subscription_iam_member" "detector_viewer" {
  count = var.enable_drift_pubsub ? 1 : 0

  project      = var.project_id
  subscription = module.drift_pubsub[0].subscription_id
  role         = "roles/pubsub.viewer"
  member       = "serviceAccount:${module.kube_agents_iam.service_account_email}"
}

module "github_minter" {
  source = "../../modules/github-minter"
  count  = var.enable_github_minter ? 1 : 0

  project_id         = var.project_id
  location           = var.location
  namespace          = var.namespace
  service_account_id = var.github_minter_service_account_id
  kms_keyring_name   = var.github_minter_kms_keyring
  kms_key_name       = var.github_minter_kms_key

  depends_on = [google_project_service.required]
}

# Stockout Investigator Pub/Sub Infrastructure (Topic, Subscription, Logging Sink, IAM).
# Declaratively provisioned by the composition when enable_stockout_investigator = true.
# Matches the resource names and filter configuration created imperatively by
# agentplugins/gke-stockout-investigator/install.sh.
resource "google_pubsub_topic" "stockout_alerts" {
  #checkov:skip=CKV_GCP_83:Stockout alert topic uses default Google-managed encryption keys
  count   = var.enable_stockout_investigator ? 1 : 0
  project = var.project_id
  name    = var.stockout_pubsub_topic

  depends_on = [google_project_service.required]
}

resource "google_pubsub_subscription" "stockout_alerts" {
  count   = var.enable_stockout_investigator ? 1 : 0
  project = var.project_id
  name    = var.stockout_pubsub_subscription
  topic   = google_pubsub_topic.stockout_alerts[0].id

  ack_deadline_seconds       = 60
  message_retention_duration = "604800s"

  expiration_policy {
    ttl = ""
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
}

resource "google_logging_project_sink" "stockout_alerts" {
  count       = var.enable_stockout_investigator ? 1 : 0
  project     = var.project_id
  name        = var.stockout_pubsub_sink
  destination = "pubsub.googleapis.com/${google_pubsub_topic.stockout_alerts[0].id}"
  filter      = "(log_id(\"test-stockout\") OR log_id(\"container.googleapis.com/cluster-autoscaler-visibility\")) AND (resource.labels.cluster_name=\"${module.gke_cluster.cluster_name}\" OR jsonPayload.resource.labels.cluster_name=\"${module.gke_cluster.cluster_name}\") AND (jsonPayload.messageId:(\"scale.up.error.out.of.resources\" OR \"scale.up.error.quota.exceeded\" OR \"scale.up.error.ip.space.exhausted\" OR \"scale.up.no.scale.up\") OR jsonPayload.noDecisionStatus.noScaleUp:* OR jsonPayload.resultInfo.results.errorMsg.messageId:(\"scale.up.error.out.of.resources\" OR \"scale.up.error.quota.exceeded\" OR \"scale.up.error.ip.space.exhausted\" OR \"scale.up.no.scale.up\"))"

  unique_writer_identity = true
}

resource "google_pubsub_topic_iam_member" "stockout_sink_writer" {
  count   = var.enable_stockout_investigator ? 1 : 0
  project = var.project_id
  topic   = google_pubsub_topic.stockout_alerts[0].name
  role    = "roles/pubsub.publisher"
  member  = google_logging_project_sink.stockout_alerts[0].writer_identity
}

resource "google_pubsub_subscription_iam_member" "stockout_agent_subscriber" {
  count        = var.enable_stockout_investigator ? 1 : 0
  project      = var.project_id
  subscription = google_pubsub_subscription.stockout_alerts[0].name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${module.kube_agents_iam.service_account_email}"
}

resource "google_pubsub_subscription_iam_member" "stockout_agent_viewer" {
  count        = var.enable_stockout_investigator ? 1 : 0
  project      = var.project_id
  subscription = google_pubsub_subscription.stockout_alerts[0].name
  role         = "roles/pubsub.viewer"
  member       = "serviceAccount:${module.kube_agents_iam.service_account_email}"
}

# cert-manager, the certificate source for the operator's admission webhooks.
#
# Two deliberate choices:
#   - leader election runs in the cert-manager namespace rather than its
#     kube-system default, which Autopilot restricts. Moving the lease clears
#     that restriction without giving up the lock.
#   - pointing this at a cluster that already runs cert-manager fails on the
#     existing CRDs rather than adopting them. Set enable_cert_manager = false
#     there.
resource "helm_release" "cert_manager" {
  count = var.enable_cert_manager ? 1 : 0

  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  version          = var.cert_manager_version
  namespace        = "cert-manager"
  create_namespace = true

  # Load-bearing, not hygiene: helm_release.kube_agents renders a Certificate
  # and an Issuer, and the API server rejects both unless cert-manager's own
  # webhook is already serving. wait blocks until the three Deployments report
  # Available, which is what makes the depends_on below mean anything.
  wait    = true
  timeout = 600

  # Helm deep-merges the docs in order, so the mirror overrides (second doc,
  # present only on a mirrored install) reach the image repositories without
  # disturbing the resource patches here.
  values = concat([yamlencode({
    # cert-manager 1.15+'s spelling; 1.14 and earlier called it installCRDs.
    # Dropping cert_manager_version below 1.15.x means changing this key too.
    crds = {
      enabled = true
    }

    global = {
      leaderElection = {
        namespace = "cert-manager"
      }
    }

    # Small explicit requests: Autopilot bills what is requested, and its
    # defaults are several times these.
    resources = local.cert_manager_resources
    cainjector = {
      resources = local.cert_manager_resources
    }
    webhook = {
      resources = local.cert_manager_resources
    }
  })], local.cert_manager_mirror_values)

  depends_on = [module.gke_cluster]
}

resource "helm_release" "kube_agents" {
  name             = "kube-agents"
  chart            = "${path.module}/../../../charts/kube-agents"
  namespace        = var.namespace
  create_namespace = true

  # This wait is the install's rollout gate, and 600 is not the provider
  # default (300) restated: hindsight-api budgets 300s of startupProbe for its
  # in-process model load on top of a 1.4 GB image pull, so the provider
  # default gives up on a cold node that is loading normally. Keep it above
  # the startup budget plus a slow pull (300+240) and below hindsight-api's
  # progressDeadlineSeconds (900) — past that the Deployment reports failure
  # and waiting longer buys nothing. tests/test_hindsight_probes.py asserts
  # the ordering.
  wait    = true
  timeout = 600

  values = [yamlencode({
    # Reaches every image this release pulls, including the three the chart
    # does not render itself — the agent Deployment, the shell sandbox
    # StatefulSet, and the fluent-bit sidecar the operator resolves at
    # reconcile time. See the chart README's "Installing from a mirrored
    # registry".
    #
    # It does NOT reach helm_release.cert_manager above: that is a separate
    # release of an upstream chart, and these values are not passed to it.
    # local.cert_manager_mirror_values carries the same registry to that
    # release's image repositories, so a mirrored install pulls every image —
    # cert-manager's included — from the mirror.
    global = {
      imageRegistry           = var.image_registry
      thirdPartyImageRegistry = var.third_party_image_registry
      # Secret names only. The Secrets themselves are created out of band, so
      # no registry credential is ever written to Terraform state.
      imagePullSecrets = var.image_pull_secrets
    }
    # The sandbox is built from this repository at the same commit as the
    # agent and the operator, so it takes image_tag with them. It needs its own
    # entry because the operator does not derive it: unlike the credential
    # broker, which comes from the agent image with the trailing name swapped,
    # the sandbox is a separate repository the chart names in AGENT_SANDBOX_IMAGE.
    # Leaving it out pins the sandbox to Chart.appVersion while everything
    # around it moves, which fails the pull rather than running the wrong code.
    agentSandbox = {
      image = {
        tag = var.image_tag
      }
    }
    operator = {
      image = {
        tag = var.image_tag
      }
      # The composition installs cert-manager, so unlike a bare `helm install`
      # it can turn the admission webhooks on. failurePolicy stays at the
      # chart's Ignore: this release creates the PlatformAgent CR too, and Helm
      # registers the webhooks before the operator holds a certificate.
      webhooks = {
        enabled = var.enable_webhooks
      }
    }
    litellm = merge(
      {
        modelProvider    = var.model_provider
        modelDefaultName = var.model_default_name
        maxTokens        = var.model_max_tokens
      },
      local.use_vertex ? {
        vertex = {
          serviceAccountName = local.litellm_ksa
          serviceAccountAnnotations = {
            "iam.gke.io/gcp-service-account" = module.litellm_vertex_iam[0].service_account_email
          }
          projectId = local.vertex_project
          location  = local.vertex_location
        }
      } : {},
      # Only while on, so an install that never turns redaction on renders the
      # gateway ConfigMap, and its checksum, exactly as before. Unset rule keys
      # are dropped: yamlencode writes them as null, which the chart refuses.
      var.litellm_redaction.enabled ? {
        redaction = {
          enabled = true
          ip = {
            action     = var.litellm_redaction.ip_action
            allowCidrs = var.litellm_redaction.allow_cidrs
          }
          rules = [for r in var.litellm_redaction.rules : { for k, v in r : k => v if v != null }]
        }
      } : {}
    )
    platformAgent = {
      # The durable record of an adoption that accepted a cluster with no
      # NetworkPolicy enforcement. Derived from what the module read, not from
      # the variable that admitted it: a cluster that later gains Dataplane V2
      # drops the annotation on the next apply, and one that never lacked
      # enforcement never carries it, whatever accept_no_network_policy says.
      annotations = module.gke_cluster.network_policy_enforced ? {} : {
        "kubeagents.x-k8s.io/network-policy-enforcement" = "absent-accepted"
      }
      harness = merge(
        {
          clusterName = module.gke_cluster.cluster_name
          location    = module.gke_cluster.cluster_location
          projectId   = var.project_id
          # null leaves a field out of the CR so the CRD default applies — the
          # chart's compactFields drops nulls and empty strings.
          hermes = {
            dashboardEnabled = var.hermes_dashboard_enabled
          }
          memory = {
            enabled            = var.memory_enabled
            provider           = var.memory_provider
            userProfileEnabled = var.user_profile_enabled
          }
        },
        # The subscription the drift detector pulls from, so a renamed
        # drift_pubsub_subscription is the one it reads: the detector's own
        # default is the module's default name and nothing else would carry a
        # rename to it. Only when the module exists -- the chart renders a
        # driftDetector block into the CR as soon as one field is set, and an
        # install that never asked for drift detection should not carry one
        # (the chart's platform-agent-cr.yaml says why).
        #
        # enabled is null rather than false when the detector is off, so the
        # CRD's own default applies and the rendered CR is byte-identical to
        # what an ingress-only install produced before this variable existed.
        # The chart's compactFields drops nulls; it does not drop false.
        var.enable_drift_pubsub ? {
          driftDetector = {
            subscription = module.drift_pubsub[0].subscription_name
            enabled      = var.enable_drift_detector ? true : null
          }
        } : {}
      )
      deployment = {
        image = {
          tag = var.image_tag
        }
        availability = {
          # The gVisor pool only exists to run the agent sandboxed, so the
          # pool and the runtimeClass move together. That derivation covers
          # Standard only: Autopilot has the gvisor RuntimeClass and no pool,
          # so enable_gvisor_node_pool is false there by force and
          # agent_runtime_class is what asks for the sandbox.
          runtimeClassName = var.agent_runtime_class != "" ? var.agent_runtime_class : (var.enable_gvisor_node_pool ? "gvisor" : "")
        }
      }
      security = {
        # With annotations set, the OPERATOR creates and manages the KSA (see
        # the chart README's ServiceAccount-ownership section); this one wires
        # Workload Identity to the GSA the kube-agents-iam module created.
        # The KSA module.kube_agents_iam bound above -- one variable, so the
        # binding cannot name a KSA the pod does not run as.
        serviceAccountName = var.agent_ksa_name
        serviceAccountAnnotations = {
          "iam.gke.io/gcp-service-account" = module.kube_agents_iam.service_account_email
        }
        # The mapping the credential broker selects from, one row per project
        # the module provisioned a member for. It has to reach the cluster as
        # data rather than being recomputed there: the broker refuses a
        # project it has no entry for, so a second implementation of the
        # naming rule would turn a mismatch into a refusal at request time
        # instead of a diff at plan time. `enabled` is the arming switch the
        # operator reads; the list is empty while it is false.
        scopedServiceAccountPool = {
          enabled = var.scoped_pool_enabled
          serviceAccounts = [
            for project_id in sort(keys(module.kube_agents_iam.scoped_service_accounts)) : {
              projectId           = project_id
              serviceAccountEmail = module.kube_agents_iam.scoped_service_accounts[project_id]
            }
          ]
        }
      }
      # The same object the IAM module bound above, so the CR declares no
      # project the module did not also bind. Always rendered, empty lists
      # included: the reconcile reads a present block with an empty projects
      # list as the declaration that drops projects, and an absent block as no
      # declaration at all (docs/designs/multi-project-scope.md §7), so
      # removing the last scoped project here has to reach the CR as an
      # emptied block, never as a missing one.
      scope = {
        projects       = var.scope.projects
        folders        = var.scope.folders
        organizations  = var.scope.organizations
        sharedVpcHosts = var.scope.shared_vpc_hosts
        metricsScopes  = var.scope.metrics_scopes
        maxProjects    = var.scope.max_projects
        exclude = {
          projects = var.scope.exclude.projects
          clusters = [
            for cluster in var.scope.exclude.clusters : {
              projectId   = cluster.project_id
              location    = cluster.location
              clusterName = cluster.cluster_name
            }
          ]
        }
      }
      credentials = {
        create = true
        data   = local.credentials
      }
      integration = local.platform_agent_integration
    }
    # The minter's Kubernetes half (Deployment, Service, NetworkPolicy, KSA,
    # minty rule ConfigMap, github-app-credentials Secret); the GCP half is
    # module.github_minter above. The App private key still has to be imported
    # into the module's KMS key before the Deployment goes Ready — see the
    # github-minter module README.
    githubMinter = merge(
      {
        enabled = var.enable_github_minter
        org     = local.github_org
        repo    = local.github_repo_name
        appId   = var.github_app_id
        # The minty rule's only gate on either scope is assertion.email
        # against this value; left unset the chart falls back to the fixed
        # kubeagents-platform-gsa name, so an install that overrides
        # agent_service_account_id would annotate one GSA and allowlist another.
        allowedServiceAccount = module.kube_agents_iam.service_account_email
        kms = {
          keyring = var.github_minter_kms_keyring
          key     = var.github_minter_kms_key
        }
      },
      # The chart annotates the minter KSA with gsaName and otherwise falls back
      # to the fixed kubeagents-github-minter-gsa, so an install that sets
      # github_minter_service_account_id would create one GSA and have the pod
      # impersonate another. Only when the module exists: a null here would
      # be a Helm null, which deletes the chart default instead of keeping it.
      var.enable_github_minter ? {
        gsaName = module.github_minter[0].service_account_id
      } : {}
    )
    plugins = {
      pubsubPlatform = merge(
        {
          enabled = var.enable_pubsub_platform || var.enable_stockout_investigator
        },
        var.image_tag != "" ? {
          image = {
            tag = var.image_tag
          }
        } : {}
      )
      stockoutInvestigator = merge(
        {
          enabled     = var.enable_stockout_investigator
          clusterName = module.gke_cluster.cluster_name
          pubsub = {
            topic        = var.stockout_pubsub_topic
            subscription = var.stockout_pubsub_subscription
            sink         = var.stockout_pubsub_sink
          }
        },
        var.image_tag != "" ? {
          image = {
            tag = var.image_tag
          }
        } : {}
      )
    }
    }),
    # Second document rather than a merge() into the first: Helm deep-merges
    # successive values documents, so a caller can reach a single leaf
    # (litellm.otel, one harness knob) without restating the block around it.
    # A merge() here would be one level deep and would silently drop the rest of
    # whichever top-level key was passed.
    yamlencode(var.extra_helm_values),
  ]

  # The Vertex entries are no-ops when model_provider is not "vertex_ai"; without
  # them the gateway can be serving before its API and role grant land.
  # cert_manager is listed even when enable_cert_manager is false — depends_on to
  # a resource with count = 0 is satisfied immediately, so it costs nothing in
  # that case and is the ordering guarantee in the case that matters.
  # module.kube_agents_iam, so the scope's per-project bindings exist before
  # the CR that declares those projects is written; the values above already
  # depend on the module's service account, not on its bindings.
  depends_on = [
    module.gke_cluster,
    module.kube_agents_iam,
    google_project_service.vertex_ai,
    google_project_iam_member.litellm_vertex_user,
    helm_release.cert_manager,
    google_pubsub_topic.stockout_alerts,
    google_pubsub_subscription.stockout_alerts,
    google_logging_project_sink.stockout_alerts,
    google_pubsub_topic_iam_member.stockout_sink_writer,
    google_pubsub_subscription_iam_member.stockout_agent_subscriber,
    google_pubsub_subscription_iam_member.stockout_agent_viewer,
  ]

  lifecycle {
    precondition {
      condition     = !var.enable_github_minter || (local.github_org != "" && local.github_repo_name != "")
      error_message = "enable_github_minter requires github_repo in owner/repo (or github.com URL) form — the minty rule ConfigMap is scoped to that repository."
    }

    # A gitlab install has no GitHub App: the minter would mint for a
    # repository the agent does not use, and github_repo would render the
    # github alias beside the gitlab lists, which the CRD refuses.
    precondition {
      condition     = !local.gitops_is_gitlab || (var.gitlab_repo != "" && var.github_repo == "" && !var.enable_github_minter)
      error_message = "gitops_forge = \"gitlab\" needs gitlab_repo set, and github_repo and enable_github_minter left unset: a GitLab install has no GitHub App."
    }

    # The reverse: GitLab inputs under the default forge would be dropped,
    # applying a CR with no GitOps repository and no error.
    precondition {
      condition     = local.gitops_is_gitlab || (var.gitlab_repo == "" && var.gitops_host == "")
      error_message = "gitlab_repo and gitops_host apply only with gitops_forge = \"gitlab\"; set it, or leave them unset."
    }

    # What this refuses is an install that asks for the detector without the
    # subscription it reads, which is silent whichever door asked. Through
    # enable_drift_detector: driftDetector, enabled included, is written inside
    # the enable_drift_pubsub ternary above, so with the ingress off the field
    # is never rendered, the apply succeeds, nothing is provisioned, nothing
    # starts, and the only evidence is a variable that did nothing. Through
    # extra_helm_values (local.drift_detector_requested above): the field is
    # rendered, the detector starts, and it retries a pull against a
    # subscription that was never created for the life of a pod that stays
    # Ready -- k8s-operator/cmd/drift-detector/README.md says the subscription
    # is not checked at startup. Refused rather than warned about, unlike
    # check "slack_tokens_present" above, because a Slack install with no
    # tokens says so in the pod log and neither of these leaves a trace
    # anywhere.
    #
    # The ternary is what keeps a driftDetector block out of the CR of an
    # install that never asked for drift detection, and this is what keeps
    # asking for it half-way from being accepted. Neither replaces the other.
    precondition {
      condition     = !local.drift_detector_requested || var.enable_drift_pubsub
      error_message = "the drift detector requires enable_drift_pubsub, whether it was asked for with enable_drift_detector or with platformAgent.harness.driftDetector.enabled through extra_helm_values: it reads the Pub/Sub subscription that flag provisions. Without it the flag renders no driftDetector block at all and does nothing, and extra_helm_values renders one this composition cannot point at a subscription, leaving the detector pulling one that does not exist with the pod Ready. Neither says anything. Through the installer, ENABLE_DRIFT_DETECTOR=true sets both."
    }

    # The operator's own gate, brought forward to the plan. A numeric
    # project_id works everywhere else in this composition -- gcloud resolves
    # a project number, and the credential proxy and the context name both
    # take one -- but driftDetectorEnabled() in the operator refuses it
    # (k8s-operator/internal/controller/platformagent_manifests.go), because
    # the join matches it against each audit record's project_id, which is
    # always the ID. Left to run, such an install provisions the whole ingress
    # -- a sink exporting every GKE cluster in the project into a subscription
    # that retains 31 days and never expires -- and starts no consumer, with a
    # Ready pod and no report to tell anyone.
    precondition {
      condition     = !local.drift_detector_requested || !can(regex("^[0-9]+$", var.project_id))
      error_message = "the drift detector requires project_id to be the project ID, not the project number: it matches it against each audit record's project_id, which is always the ID, so the operator refuses to start it and the ingress bills for a stream nothing reads."
    }

    # The third route to that same ingress-without-a-consumer, and the one the
    # other two cannot see: here the flag is set, the ingress is asked for by
    # the same variable that asks for the detector, and the CR is what
    # disagrees. local.extra_helm_values_countermands_detector above has the
    # merge that does it and why false is refused alongside null.
    #
    # Refused rather than stripped from the document. Silently dropping the
    # leaf would give this operator what they almost certainly meant, and it
    # would also make extra_helm_values the one values document this
    # composition edits before passing on -- so the next operator to set a
    # leaf it disagrees with would have no way to tell whether it arrived.
    # Saying which line to delete costs one apply and keeps that contract.
    precondition {
      condition     = !local.extra_helm_values_countermands_detector
      error_message = "enable_drift_detector is true, but extra_helm_values sets platformAgent.harness.driftDetector.enabled to something other than true, and Helm merges that document over the one this composition computes -- so the CR decides against the flag: a null leaf deletes the field and the CRD's default false applies, a false leaf arrives as false. The sink, topic and subscription are provisioned and bill either way, and no detector reads them. Remove the enabled leaf from extra_helm_values -- enable_drift_detector already writes it, and the rest of your driftDetector block is still merged in -- or set it to true."
    }
  }
}
