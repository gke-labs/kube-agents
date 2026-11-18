output "cluster_name" {
  description = "Name of the provisioned GKE Autopilot cluster"
  value       = module.gke_cluster.cluster_name
}

# The image tag the last apply recorded, which is NOT the tag the cluster is
# serving: the redeploy workflows move the running tag with `helm upgrade` and
# never touch Terraform. A drift plan needs this one. Planning at the RUNNING
# tag instead makes every out-of-band redeploy show up as a pending change to
# helm_release.kube_agents, so the daily report opens on image lag and the
# infra-drift issue never reaches the clean plan that would close it.
#
# An output rather than reading the helm_release's values out of state, because
# outputs are what Terraform persists for exactly this purpose and `terraform
# output -raw` is a stable interface where digging through `show -json` is not.
output "image_tag" {
  description = "Image tag recorded by the last apply of this composition"
  value       = var.image_tag
}

# The profile the Standard pools were last applied under, for lifecycle.sh's
# guard_node_pool_shape. A root output persists in state where the module's
# does not, so the guard can tell a profile the operator changed, which asks
# for the resize it plans, from one kept beside a lost explicit TF_VAR_ line,
# which asks for nothing. Resolved by the module: small when sizing_profile is
# unset or "".
output "sizing_profile" {
  description = "Sizing profile the Standard cluster's node pools were last applied under (small, medium or fleet); small when sizing_profile is unset or \"\"."
  value       = module.gke_cluster.sizing_profile
}

# The shape inputs that were set at the last apply, for the same guard. The
# profile above says which profile the pools came from; this says which
# attributes an explicit input decided instead of it. A changed profile asks
# for the shape of every attribute a profile decides and nothing about one an
# explicit line decided, so the guard refuses an apply whose explicit line is
# gone whatever the profile says, until the line is back or the pool's input
# names the shape. Names rather than values: the shape each decided is in the
# pool's node_config in state, where the guard already reads it. One
# space-separated string, so `terraform output -raw` returns it whole and the
# guard matches a name without a JSON parser; empty when every attribute came
# from the profile. "" counts as unset, as the module reads it.
output "node_pool_shape_inputs" {
  description = "The standard_* and gvisor_* machine-type and disk inputs that were set (not null, not \"\") at the last apply, space-separated and sorted by name; empty when every pool shape came from sizing_profile."
  value = join(" ", [
    for name, value in {
      standard_machine_type = var.standard_machine_type
      standard_disk_size_gb = var.standard_disk_size_gb
      standard_disk_type    = var.standard_disk_type
      gvisor_machine_type   = var.gvisor_machine_type
      gvisor_disk_size_gb   = var.gvisor_disk_size_gb
      gvisor_disk_type      = var.gvisor_disk_type
    } : name if value != null && tostring(value) != ""
  ])
}

output "cluster_location" {
  description = "Region the cluster runs in"
  value       = module.gke_cluster.cluster_location
}

output "agent_service_account_email" {
  description = "Email of the Platform Agent's Google Service Account"
  value       = module.kube_agents_iam.service_account_email
}

output "agent_project_roles" {
  description = "Project-level IAM roles actually granted to the agent's service account"
  value       = local.agent_project_roles

  # permission_set = "custom" with no project_roles would otherwise fall
  # through to the read-only bundle — quietly granting something other than
  # what was asked for.
  precondition {
    condition     = !(var.permission_set == "custom" && var.project_roles == null)
    error_message = "permission_set = \"custom\" requires project_roles to be set explicitly (use [] to grant nothing)."
  }
}

output "backup_plan_name" {
  description = "Name of the scheduled BackupPlan (null when enable_gke_backup_plan is false)"
  value       = try(module.gke_backup_plan[0].backup_plan_name, null)

  # Both operands are input variables, so this is decided at plan time — before
  # the cluster exists. Without it the mismatch surfaces as a raw
  # FAILED_PRECONDITION from the Backup for GKE API partway through an apply
  # that has already built everything ahead of the plan.
  precondition {
    condition     = !var.enable_gke_backup_plan || var.enable_backup_agent
    error_message = "enable_gke_backup_plan = true requires enable_backup_agent = true: a BackupPlan cannot target a cluster whose Backup for GKE agent is off."
  }
}

output "chat_topic_name" {
  description = "Pub/Sub topic for Google Chat events (null when Chat is disabled); already wired into the PlatformAgent CR's googleChat section"
  value       = try(module.chat_pubsub[0].topic_name, null)
}

output "chat_subscription_name" {
  description = "Pub/Sub subscription for Google Chat events (null when Chat is disabled); already wired into the PlatformAgent CR's googleChat section"
  value       = try(module.chat_pubsub[0].subscription_name, null)
}

output "github_minter_service_account_email" {
  description = "Email of the GitHub token minter's service account (null when the minter is disabled)"
  value       = try(module.github_minter[0].service_account_email, null)
}

output "github_minter_kms_keyring" {
  description = "KMS key ring holding the GitHub App signing key (null when the minter is disabled)"
  value       = try(module.github_minter[0].kms_keyring, null)
}

output "github_minter_kms_key" {
  description = "KMS signing key to import the GitHub App PEM into (null when the minter is disabled)"
  value       = try(module.github_minter[0].kms_key, null)
}

output "stockout_pubsub_topic" {
  description = "Pub/Sub topic for GKE stockout alerts (null when enable_stockout_investigator is false)"
  value       = try(google_pubsub_topic.stockout_alerts[0].name, null)
}

output "stockout_pubsub_subscription" {
  description = "Pub/Sub subscription for GKE stockout alerts (null when enable_stockout_investigator is false)"
  value       = try(google_pubsub_subscription.stockout_alerts[0].name, null)
}

output "stockout_pubsub_sink" {
  description = "Cloud Logging sink for GKE stockout alerts (null when enable_stockout_investigator is false)"
  value       = try(google_logging_project_sink.stockout_alerts[0].name, null)
}

output "drift_pubsub_topic" {
  description = "Pub/Sub topic the drift audit-log sink publishes to (null when enable_drift_pubsub is false)"
  value       = try(module.drift_pubsub[0].topic_name, null)
}

output "drift_pubsub_subscription" {
  description = "Pub/Sub pull subscription the drift detector reads from (null when enable_drift_pubsub is false)"
  value       = try(module.drift_pubsub[0].subscription_name, null)
}

output "drift_pubsub_subscription_id" {
  description = "Fully-qualified drift subscription path, projects/<project>/subscriptions/<name>, the value the drift detector's --subscription flag takes (null when enable_drift_pubsub is false)"
  value       = try(module.drift_pubsub[0].subscription_id, null)
}

output "drift_pubsub_source_projects" {
  description = "The projects beyond project_id whose audit logs the drift topic also receives, one sink each named drift_pubsub_sink with project_id appended: the projects the declaration lists at plan time (the IAM module's scope_export_projects: scope.projects and the selectors' members, never a container's Asset-Inventory members) less a project a scope.exclude.projects glob matches and less drift_pubsub_source_exclude_projects. Empty with no declared scope; null when enable_drift_pubsub is false."
  value       = try(module.drift_pubsub[0].source_projects, null)
}

output "scoped_service_accounts" {
  description = "Map from project id to the pool member's service account for it, one per project the plan listed in scope while scoped_pool_enabled is true, and empty otherwise. The key is what the credential broker matches on, so the two are directly comparable. The accounts hold no IAM grant as of 2026-08-12; see scoped_pool.tf."
  value       = module.kube_agents_iam.scoped_service_accounts
}

output "scope_projects" {
  description = "The projects beyond project_id that scope.projects named and the IAM module bound scope_roles in. The host project is omitted even when named; the CR's projects list carries it as written."
  value       = module.kube_agents_iam.scope_projects
}

output "scope_roles" {
  description = "The roles every scope project carries: the IAM module's read allowlist intersected with the roles the host project got."
  value       = module.kube_agents_iam.scope_roles
}

output "scope_folders" {
  description = "The folders scope.folders named and the IAM module bound scope_container_roles on."
  value       = module.kube_agents_iam.scope_folders
}

output "scope_organizations" {
  description = "The organisations scope.organizations named and the IAM module bound scope_container_roles on."
  value       = module.kube_agents_iam.scope_organizations
}

output "scope_container_roles" {
  description = "The roles every folder and organisation in scope carries: scope_roles plus roles/cloudasset.viewer for the reconcile's container search."
  value       = module.kube_agents_iam.scope_container_roles
}

output "scope_shared_vpc_hosts" {
  description = "The Shared VPC host projects scope.shared_vpc_hosts named; each was resolved at plan time to its service projects (scope_selector_members) and, unless otherwise in scope or project_id itself, is bound with roles/compute.viewer alone for the reconcile's lookup (scope_lookup_only_hosts)."
  value       = module.kube_agents_iam.scope_shared_vpc_hosts
}

output "scope_metrics_scopes" {
  description = "The Metrics Scope scoping projects scope.metrics_scopes named; each was resolved at plan time to the projects it monitors (scope_selector_members) and, unless it is project_id, is bound with scope_roles itself for the reconcile's lookup."
  value       = module.kube_agents_iam.scope_metrics_scopes
}

output "scope_selector_members" {
  description = "What each Shared VPC host and Metrics Scope resolved to at plan time, by project ID, under the selector's snapshot name (sharedVpcHosts/<host>, metricsScopes/<scope>)."
  value       = module.scope_resolver.members
}

output "scope_bound_projects" {
  description = "Every project beyond project_id the IAM module bound scope_roles in: the explicit projects, the selectors' members less a Shared VPC service project an exclude entry names by ID (a monitored project excluded by number is never resolved, and one excluded by ID keeps its grant), and each Metrics Scope scoping project."
  value       = module.kube_agents_iam.scope_bound_projects
}

output "scope_lookup_only_hosts" {
  description = "The Shared VPC hosts bound with roles/compute.viewer alone, for the reconcile's lookup of their service projects: every declared host that is neither project_id nor otherwise in scope."
  value       = module.kube_agents_iam.scope_lookup_only_hosts
}

output "kube_agents_helm_release" {
  description = "How to install the kube-agents chart from a pipeline of your own when install_helm_releases is false: the release name, namespace, chart directory (relative to the root of this repository or a release bundle) and chart version, with the values in kube_agents_helm_values. Install cert-manager first (cert_manager_helm_release) when enable_cert_manager is true: the chart's Certificate and Issuer need its webhook serving."
  value = {
    name      = "kube-agents"
    namespace = var.namespace
    chart     = local.kube_agents_chart_repo_path
    version   = local.kube_agents_chart_version
  }
}

output "kube_agents_helm_values" {
  description = "The values documents helm_release.kube_agents is given, in order (later documents deep-merge over earlier ones, as `helm install -f a.yaml -f b.yaml` does). Sensitive: they carry every credential the install was given (the API server key, the session store's key and salt, the sandbox SSH key, any model provider API key, the Slack tokens), all of which the install's state already holds. Write each to a file for the other pipeline, for example: terraform output -json kube_agents_helm_values | jq -r '.[0]' > values-0.yaml."
  value       = local.kube_agents_helm_values
  sensitive   = true

  # The chart refuses to render openai_compatible without these, and a
  # render failure inside helm_release arrives after the cluster and IAM
  # have been built; a precondition fails the plan instead. They sit here, on
  # the output every plan evaluates, rather than on the release, so a plan
  # with install_helm_releases = false refuses the same values the chart would. The egress rules
  # (a target port for a Service, CIDRs otherwise) are the chart's to judge.
  precondition {
    condition     = !local.use_openai_compatible || var.model_default_name != ""
    error_message = "model_provider = \"openai_compatible\" requires model_default_name, the model the server serves; the provider has no default."
  }
  precondition {
    condition     = !local.use_openai_compatible || var.openai_compatible_api_base != ""
    error_message = "model_provider = \"openai_compatible\" requires openai_compatible_api_base, the server's base URL including /v1."
  }
  precondition {
    condition     = local.model_provider_has_default || local.use_openai_compatible || var.model_default_name != ""
    error_message = "model_provider = \"${var.model_provider}\" is a LiteLLM provider slug outside the chart's table, which has no default model: set model_default_name to the model it serves."
  }

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

  # A Bitbucket install has no GitHub App either, and names its repository
  # as workspace/slug (Cloud) or KEY/slug (Data Center), the grammars of the
  # operator's bitbucketWorkspaceRegex and bitbucketProjectKeyRegex.
  precondition {
    condition = !local.gitops_is_bitbucket || (var.github_repo == "" && !var.enable_github_minter && (
      var.gitops_forge == "bitbucket"
      ? can(regex("^[a-z0-9][a-z0-9_-]*/[A-Za-z0-9_.-]+$", var.gitops_repo_path))
      : can(regex("^[A-Za-z][A-Za-z0-9_]*/[A-Za-z0-9_.-]+$", var.gitops_repo_path))
    ))
    error_message = "gitops_forge = \"bitbucket\" needs gitops_repo_path as workspace/repo-slug, and \"bitbucket-datacenter\" as PROJECTKEY/repo-slug; github_repo and enable_github_minter stay unset: a Bitbucket install has no GitHub App."
  }

  # Bitbucket Cloud is bitbucket.org only, with the public CAs; a Data
  # Center instance names its host, which is never a hosted service's.
  precondition {
    condition     = var.gitops_forge != "bitbucket" || (var.gitops_host == "" && var.gitops_ca_secret_name == "")
    error_message = "gitops_forge = \"bitbucket\" is Bitbucket Cloud at bitbucket.org: leave gitops_host and gitops_ca_secret_name unset. For a Data Center instance, use gitops_forge = \"bitbucket-datacenter\"."
  }
  precondition {
    condition     = var.gitops_forge != "bitbucket-datacenter" || (var.gitops_host != "" && !can(regex("(^|\\.)(bitbucket\\.org|bitbucket\\.io|atlassian\\.com|atlassian\\.net|github\\.com|githubusercontent\\.com|githubassets\\.com|githubapp\\.com|github\\.io|github\\.dev|ghe\\.com|gitlab\\.com)$", var.gitops_host)))
    error_message = "gitops_forge = \"bitbucket-datacenter\" needs gitops_host: the instance's own hostname, not bitbucket.org or another hosted service's domain."
  }
  precondition {
    condition     = var.gitops_forge != "bitbucket" || var.gitops_identity == "" || can(regex("^(\\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\\}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})$", var.gitops_identity))
    error_message = "gitops_identity on Bitbucket Cloud is the account UUID the access token acts as, with or without braces."
  }
  # The CRD's grammar for identity, on both Bitbucket providers.
  precondition {
    condition     = var.gitops_identity == "" || can(regex("^[A-Za-z0-9._@+{}-]{1,255}$", var.gitops_identity))
    error_message = "gitops_identity is 1 to 255 letters, digits and . _ @ + { } -."
  }
  # The Bitbucket inputs under another forge would be dropped silently.
  precondition {
    condition     = local.gitops_is_bitbucket || (var.gitops_repo_path == "" && var.gitops_token_secret_name == "" && var.gitops_ca_secret_name == "" && var.gitops_identity == "")
    error_message = "gitops_repo_path, gitops_token_secret_name, gitops_ca_secret_name and gitops_identity apply only with gitops_forge = \"bitbucket\" or \"bitbucket-datacenter\"; set it, or leave them unset."
  }

  # The reverse: GitLab inputs under the default forge would be dropped,
  # applying a CR with no GitOps repository and no error. gitops_host is
  # the github forge's too: a GitHub Enterprise Server host.
  precondition {
    condition     = local.gitops_is_gitlab || (var.gitlab_repo == "" && var.gitlab_ca_secret_name == "")
    error_message = "gitlab_repo and gitlab_ca_secret_name apply only with gitops_forge = \"gitlab\"; set it, or leave them unset."
  }

  # A GitHub Enterprise Server's CA belongs to the github forge at a host:
  # github.com presents a certificate the public CAs sign, and a private CA
  # must never vouch for it.
  precondition {
    condition     = var.github_ca_secret_name == "" || local.gitops_is_ghes
    error_message = "github_ca_secret_name is for a GitHub Enterprise Server: set gitops_host to its hostname, with gitops_forge = \"github\". github.com uses the public CAs."
  }

  # The github forge at gitops_host is a GitHub Enterprise Server. A host
  # where GitHub runs its own services is refused, as the operator refuses
  # it; github.com is the default and needs no host; and a GitLab host is
  # not GitHub's.
  precondition {
    condition     = !local.gitops_is_ghes || !can(regex("(^|\\.)(github\\.com|githubusercontent\\.com|githubassets\\.com|githubapp\\.com|github\\.io|github\\.dev|ghe\\.com|gitlab\\.com|bitbucket\\.org|bitbucket\\.io|atlassian\\.com|atlassian\\.net)$", var.gitops_host))
    error_message = "gitops_host with gitops_forge = \"github\" is a GitHub Enterprise Server hostname. Leave it empty for github.com; a host that GitHub operates (github.com, githubusercontent.com, ghe.com and the like), gitlab.com, or a Bitbucket or Atlassian domain is not one."
  }

  # On a GitHub Enterprise Server, github_repo is required, as owner/name
  # on the instance: the forge is declared with the owner as its namespace,
  # and the composition reads a URL on github.com only.
  precondition {
    condition     = !local.gitops_is_ghes || can(regex("^[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]+$", var.github_repo))
    error_message = "With gitops_host set, github_repo is required, as owner/name on the GitHub Enterprise Server, not a URL."
  }

  # A private CA is for a self-managed GitLab only: gitlab.com presents a
  # certificate the public CAs sign, and a private CA must never vouch for it.
  precondition {
    condition     = var.gitlab_ca_secret_name == "" || !contains(["", "gitlab.com", "www.gitlab.com"], var.gitops_host)
    error_message = "gitlab_ca_secret_name is for a self-managed GitLab: set gitops_host to its hostname. gitlab.com uses the public CAs."
  }

  # What this refuses is an install that asks for the detector without the
  # subscription it reads, which is silent whichever door asked. Through
  # enable_drift_detector: driftDetector, enabled included, is written inside
  # the enable_drift_pubsub ternary in main.tf, so with the ingress off the field
  # is never rendered, the apply succeeds, nothing is provisioned, nothing
  # starts, and the only evidence is a variable that did nothing. Through
  # extra_helm_values (local.drift_detector_requested in main.tf): the field is
  # rendered, the detector starts, and it retries a pull against a
  # subscription that was never created for the life of a pod that stays
  # Ready -- k8s-operator/cmd/drift-detector/README.md says the subscription
  # is not checked at startup. Refused rather than warned about, unlike
  # check "slack_tokens_present" in main.tf, because a Slack install with no
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
  # disagrees. local.extra_helm_values_countermands_detector in main.tf has the
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

  # The web console needs NetworkPolicy enforced (see the local); the release
  # checks it too, for the case where it is only known at apply.
  precondition {
    condition     = local.web_console_network_policy_ok
    error_message = local.web_console_network_policy_error
  }
}

output "cert_manager_helm_release" {
  description = "How to install cert-manager from a pipeline of your own when install_helm_releases is false, or null when enable_cert_manager is false. Install it, and wait for its Deployments to be Available, before the kube-agents chart."
  value = var.enable_cert_manager ? {
    name       = "cert-manager"
    namespace  = "cert-manager"
    repository = "https://charts.jetstack.io"
    chart      = "cert-manager"
    version    = var.cert_manager_version
    values     = local.cert_manager_helm_values
  } : null
}
