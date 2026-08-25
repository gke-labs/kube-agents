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
  ]
  chat_apis = var.enable_google_chat ? [
    "pubsub.googleapis.com",
    "chat.googleapis.com",
    "gsuiteaddons.googleapis.com",
  ] : []

  use_vertex     = var.model_provider == "vertex_ai"
  vertex_project = var.vertex_project_id != "" ? var.vertex_project_id : var.project_id
  # Not var.location: a model is only callable from a location that serves it,
  # and the cluster's is often not one — on a zonal cluster it is not even a
  # valid Vertex location. Mirrors DEFAULT_VERTEX_LOCATION in
  # k8s-operator/scripts/installer_common.sh.
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

  required_apis = toset(concat(local.base_apis, local.chat_apis))

  # The agent's GCP IAM permission-set bundles, kept verbatim so the
  # two install paths hand the agent the same authority. Kubernetes RBAC is
  # read-only in both; see the security-and-iam reference.
  read_only_roles = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
    "roles/compute.viewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/iam.serviceAccountUser",
    "roles/iam.securityReviewer",
    "roles/mcp.toolUser",
  ]
  gke_admin_roles = [
    "roles/container.clusterAdmin",
    "roles/container.admin",
    "roles/compute.viewer",
    "roles/monitoring.admin",
    # The agent can query logs for diagnostics but must not administer the
    # audit-log sink.
    "roles/logging.viewer",
    "roles/iam.serviceAccountUser",
    "roles/iam.securityReviewer",
    "roles/mcp.toolUser",
  ]

  # An explicit project_roles list always wins, so an existing configuration
  # that set it keeps its roles regardless of permission_set.
  agent_project_roles = (
    var.project_roles != null
    ? var.project_roles
    : (var.permission_set == "gke-admin" ? local.gke_admin_roles : local.read_only_roles)
  )

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
      ANTHROPIC_API_KEY  = var.anthropic_api_key
      GEMINI_API_KEY     = var.gemini_api_key
      OPENAI_API_KEY     = var.openai_api_key
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
  "/")

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

  depends_on = [google_project_service.required]
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

module "kube_agents_iam" {
  source = "../../modules/kube-agents-iam"

  project_id    = var.project_id
  namespace     = var.namespace
  project_roles = local.agent_project_roles

  depends_on = [google_project_service.required]
}

# ─── Vertex AI gateway identity (model_provider = "vertex_ai") ────────────────
# Vertex has no API key: the LiteLLM gateway calls it as this GSA through
# Workload Identity. The GSA lives in project_id; the aiplatform.user grant and
# the API enablement go to the serving project, which may be a different one.
resource "google_project_service" "vertex_ai" {
  count = local.use_vertex ? 1 : 0

  project            = local.vertex_project
  service            = "aiplatform.googleapis.com"
  disable_on_destroy = false
}

module "litellm_vertex_iam" {
  source = "../../modules/kube-agents-iam"
  count  = local.use_vertex ? 1 : 0

  project_id         = var.project_id
  service_account_id = "kubeagents-litellm-gsa"
  display_name       = "Kube-Agents LiteLLM Vertex AI Service Account"
  namespace          = var.namespace
  ksa_name           = local.litellm_ksa
  # Granted below instead, so a cross-project vertex_project_id works.
  project_roles = []

  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "litellm_vertex_user" {
  #checkov:skip=CKV_GCP_41:LiteLLM gateway uses dedicated service account for Vertex AI inference
  #checkov:skip=CKV_GCP_42:Service account is granted non-admin aiplatform.user role
  #checkov:skip=CKV_GCP_46:Dedicated custom service account used for LiteLLM workload identity
  #checkov:skip=CKV_GCP_49:LiteLLM gateway uses dedicated service account for Vertex AI inference
  #checkov:skip=CKV_GCP_117:Vertex AI user role required for LiteLLM gateway inference access
  count = local.use_vertex ? 1 : 0

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

module "github_minter" {
  source = "../../modules/github-minter"
  count  = var.enable_github_minter ? 1 : 0

  project_id       = var.project_id
  location         = var.location
  namespace        = var.namespace
  kms_keyring_name = var.github_minter_kms_keyring
  kms_key_name     = var.github_minter_kms_key

  depends_on = [google_project_service.required]
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
    # Reaches every image this release pulls, including the two the chart does
    # not render itself — the agent Deployment and the fluent-bit sidecar the
    # operator resolves at reconcile time. See the chart README's
    # "Installing from a mirrored registry".
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
      } : {}
    )
    platformAgent = {
      harness = {
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
      }
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
        serviceAccountAnnotations = {
          "iam.gke.io/gcp-service-account" = module.kube_agents_iam.service_account_email
        }
      }
      credentials = {
        create = true
        data   = local.credentials
      }
      integration = merge(
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
        var.github_repo != "" ? {
          github = { gitRepo = var.github_repo }
        } : {}
      )
    }
    # The minter's Kubernetes half (Deployment, Service, NetworkPolicy, KSA,
    # minty rule ConfigMap, github-app-credentials Secret); the GCP half is
    # module.github_minter above. The App private key still has to be imported
    # into the module's KMS key before the Deployment goes Ready — see the
    # github-minter module README.
    githubMinter = {
      enabled = var.enable_github_minter
      org     = local.github_org
      repo    = local.github_repo_name
      appId   = var.github_app_id
      kms = {
        keyring = var.github_minter_kms_keyring
        key     = var.github_minter_kms_key
      }
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
  depends_on = [
    module.gke_cluster,
    google_project_service.vertex_ai,
    google_project_iam_member.litellm_vertex_user,
    helm_release.cert_manager,
  ]

  lifecycle {
    precondition {
      condition     = !var.enable_github_minter || (local.github_org != "" && local.github_repo_name != "")
      error_message = "enable_github_minter requires github_repo in owner/repo (or github.com URL) form — the minty rule ConfigMap is scoped to that repository."
    }
  }
}
