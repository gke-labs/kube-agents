# Resolves the two `spec.scope` selectors that are not Resource Manager
# containers, a Shared VPC host and a Cloud Monitoring Metrics Scope, to the
# projects they reach, at plan time.
#
# Nothing is inherited through either (docs/designs/multi-project-scope.md
# §6), so every project they reach needs its own binding; the bindings are
# Terraform's (the kube-agents-iam module's `scope_selector_members` input)
# and Terraform cannot read the runtime snapshot, so this module makes the
# same three reads the reconcile makes each tick (§10 step 3): the Compute
# API's getXpnResources for a host's service projects, the Monitoring API's
# metricsScopes.get for a scope's monitored projects, and Resource Manager to
# name each monitored project, which the Monitoring API returns by number.
# The reads are made with the google provider's own access token
# (data.google_client_config), so they are answered for the identity that
# applies and not for whatever gcloud's active account happens to be; the
# provider offers no data source for either listing, hence hashicorp/http.
#
# A separate module, and not part of kube-agents-iam, because the composition
# calls that module with a module-level depends_on (the Workload Identity
# pool has to exist before its binding), and a module-level depends_on defers
# every data source inside the module to apply time whenever a target has a
# planned change -- a first install, or an upgrade that enables an API -- at
# which point a for_each keyed on the read fails the plan as unknown. This
# module is called with no depends_on and no input a managed resource
# produces, so its reads always happen at plan time.
#
# A read that fails fails the plan, before anything is applied, with the
# selector, the status and the API's message in the error; the postconditions
# below are the whole of that reporting, so a lookup this identity cannot make
# is a refused plan and never a silently smaller set, which would retire the
# members it missed on the reconcile's next two clean runs.


locals {
  scope_shared_vpc_hosts   = toset(var.shared_vpc_hosts)
  scope_metrics_scopes     = toset(var.metrics_scopes)
  scope_resolves_selectors = length(local.scope_shared_vpc_hosts) + length(local.scope_metrics_scopes) > 0

  # The three reads, as the reconcile makes them (agents/platform/scripts/
  # cluster_agent_reconcile.py): the Compute API names a host's service
  # projects by ID with a type; the Monitoring API names a scope's monitored
  # projects by project number, under locations/global/metricsScopes/<scope>/
  # projects/<number>; Resource Manager v3 names a project behind a number.
  scope_compute_api_url          = "https://compute.googleapis.com/compute/v1"
  scope_monitoring_api_url       = "https://monitoring.googleapis.com/v1"
  scope_resource_manager_api_url = "https://cloudresourcemanager.googleapis.com/v3"
  # getXpnResources is paged; one page of the API's maximum holds five times
  # the cap the CRD puts on any scope list, so a second page is refused rather
  # than followed, which HCL cannot do.
  scope_xpn_page_size = 500
  # The most projects one selector may resolve to. The reconcile lists at
  # most RESOLVED_SET_CAP (cluster_agent_reconcile.py; 100) projects of the
  # whole resolved set, the management project included, and a project past
  # that reads `over-cap` with nothing created under it; the cap on the whole
  # set is kube-agents-iam's precondition, which counts the management
  # project, the explicit projects and every selector's members together, as
  # the reconcile does. This bound is per selector and the same number: a
  # single selector past it cannot fit whatever else the scope declares, and
  # refusing it at the read it came from spares the naming reads (one
  # Resource Manager call per monitored project; a Metrics Scope may monitor
  # 375) the whole-set check would otherwise wait for. Counted as the
  # reconcile counts: less the members an exclude_projects entry names
  # exactly, by number for a monitored project and by ID for a service
  # project, since an excluded member is neither listed nor bound.
  scope_selector_member_cap       = 100
  scope_xpn_resource_type_project = "PROJECT"
  # What the Compute API answers, with HTTP 400, for a project that is not a
  # Shared VPC host. It has no service projects, which is a fact about the
  # estate and not a failed lookup; the reconcile reads it the same way, so a
  # misdeclared host neither fails the plan nor holds the scope prune.
  scope_not_xpn_host_marker            = "is not a shared VPC host project"
  scope_monitored_project_name_pattern = "^locations/global/metricsScopes/[^/]+/projects/(?P<project>[^/]+)$"
  scope_project_number_pattern         = "^[0-9]+$"
  # The CRD's project ID pattern: a monitored project whose ID does not match
  # it (a legacy domain-scoped `example.com:name`) cannot be declared, excluded
  # by ID or given a profile, so it is refused by number rather than bound.
  scope_project_id_pattern    = "^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
  scope_lookup_timeout_ms     = 20000
  scope_lookup_retry_attempts = 2
  # How much of an API's error body an error message carries.
  scope_lookup_error_excerpt_chars = 300
  # What a 403 says when the cause is not a grant on the project read: the
  # four reasons the installer's container preflight reads as not a permission
  # answer, in two pairs with a remedy each. The API being off in the consumer
  # project (SERVICE_DISABLED, "has not been used in project"): enable it in
  # quota_project, which install.sh does before a first install. The identity
  # refused the consumer project itself (USER_PROJECT_DENIED, "quota project"):
  # the API is on and the grant on the target is beside the point; the
  # identity needs serviceusage.services.use on quota_project, and enabling
  # the API changes nothing. Read in that order: a credential with no
  # consumer project of its own is answered with both a disabled API and a
  # quota-project sentence, and the consumer project is the cause.
  scope_api_off_markers              = ["SERVICE_DISABLED", "has not been used in project"]
  scope_consumer_denied_markers      = ["USER_PROJECT_DENIED", "quota project"]
  scope_consumer_role                = "roles/serviceusage.serviceUsageConsumer"
  scope_compute_api_service          = "compute.googleapis.com"
  scope_monitoring_api_service       = "monitoring.googleapis.com"
  scope_resource_manager_api_service = "cloudresourcemanager.googleapis.com"
}

# The identity the google provider plans and applies with, so every read
# below is answered for it: a lookup that passes here passes for the apply,
# and one that fails names the principal an administrator has to grant.
data "google_client_config" "scope_resolver" {
  count = local.scope_resolves_selectors ? 1 : 0
}

locals {
  # The bearer, and the consumer project every read is billed to, so the
  # answer is the same whichever credential type the provider holds.
  scope_resolver_headers = local.scope_resolves_selectors ? {
    Authorization         = "Bearer ${data.google_client_config.scope_resolver[0].access_token}"
    "x-goog-user-project" = var.quota_project
  } : {}

  scope_resolver_identity = "the identity the google provider plans with (its configured credentials, impersonation included)"

  # The sentence a refusal appends when the 403 is one of the two pairs
  # above, by API, so each postcondition picks by marker and the text lives
  # once.
  scope_api_off_clause = {
    for api in [local.scope_compute_api_service, local.scope_monitoring_api_service, local.scope_resource_manager_api_service] :
    api => " That answer names a disabled API rather than a grant: ${api} is off in ${var.quota_project}, the project these reads are billed to (install.sh enables it before a first install; by hand: gcloud services enable ${api} --project=${var.quota_project})."
  }
  scope_consumer_denied_clause = " That answer refuses the consumer project rather than a grant on the project read: ${local.scope_resolver_identity} may not bill reads to ${var.quota_project}, the project these reads name in x-goog-user-project. It needs serviceusage.services.use there (${local.scope_consumer_role} carries it, as does any role that applies the composition); the API is on, and enabling it changes nothing."
}

data "http" "scope_shared_vpc_host" {
  for_each = local.scope_shared_vpc_hosts

  url                = "${local.scope_compute_api_url}/projects/${each.key}/getXpnResources?maxResults=${local.scope_xpn_page_size}"
  request_headers    = local.scope_resolver_headers
  request_timeout_ms = local.scope_lookup_timeout_ms

  retry {
    attempts = local.scope_lookup_retry_attempts
  }

  lifecycle {
    postcondition {
      condition     = self.status_code == 200 || (self.status_code == 400 && strcontains(self.response_body, local.scope_not_xpn_host_marker))
      error_message = "shared_vpc_hosts: the service projects of ${each.key} could not be listed by ${local.scope_resolver_identity}; the Compute API answered HTTP ${self.status_code}: ${substr(try(jsondecode(self.response_body).error.message, self.response_body), 0, local.scope_lookup_error_excerpt_chars)}. That identity needs compute.projects.get on the host project (roles/compute.viewer carries it), and the Compute API enabled there; or drop the host from shared_vpc_hosts.${anytrue([for marker in local.scope_consumer_denied_markers : strcontains(self.response_body, marker)]) ? local.scope_consumer_denied_clause : anytrue([for marker in local.scope_api_off_markers : strcontains(self.response_body, marker)]) ? local.scope_api_off_clause[local.scope_compute_api_service] : ""} Nothing was applied."
    }
    postcondition {
      # A 200 whose body does not decode to an object (a list, a string, null
      # decode too, and `.resources` on them is what try() would swallow), or
      # whose resources lack an id or a type, is refused rather than read as a
      # host with no service projects, which the next apply would turn into
      # revoked bindings. An absent `resources` key stays legal: a host with
      # nothing attached answers so.
      condition     = self.status_code != 200 || (can(keys(jsondecode(self.response_body))) && can([for resource in try(jsondecode(self.response_body).resources, []) : "${resource.id}/${resource.type}"]))
      error_message = "shared_vpc_hosts: the Compute API's answer for ${each.key} is not the getXpnResources document this module reads (a JSON object whose resources each carry an id and a type); refusing to resolve the host from it rather than bind a set that may be short. Nothing was applied."
    }
    postcondition {
      # Counted less an exact exclude entry, as the reconcile counts and as
      # kube-agents-iam binds: an excluded service project is neither.
      condition     = self.status_code != 200 || length(distinct([for resource in try(jsondecode(self.response_body).resources, []) : try(resource.id, "") if try(resource.type, "") == local.scope_xpn_resource_type_project && !contains(var.exclude_projects, try(resource.id, ""))])) <= local.scope_selector_member_cap
      error_message = "shared_vpc_hosts: ${each.key} has more than ${local.scope_selector_member_cap} attached service projects not named in exclude_projects, more than the reconcile lists of the whole resolved set (RESOLVED_SET_CAP, the management project included), so the host cannot fit whatever else the scope declares; the members past the cap would read over-cap with nothing created under them, and their read roles would be reach the agent never uses. Name the service projects not wanted in exclude_projects (the scope's exclude.projects) by ID, or declare the ones wanted in the scope's projects, or a folder that holds them, instead. Nothing was applied."
    }
    postcondition {
      condition     = !can(jsondecode(self.response_body).nextPageToken)
      error_message = "shared_vpc_hosts: ${each.key} has more than ${local.scope_xpn_page_size} attached service projects, more than one page of the Compute API's answer holds and far past the scope cap; declare the service projects wanted in the scope's projects, or a folder that holds them, instead. Nothing was applied."
    }
  }
}

data "http" "scope_metrics_scope" {
  for_each = local.scope_metrics_scopes

  url                = "${local.scope_monitoring_api_url}/locations/global/metricsScopes/${each.key}"
  request_headers    = local.scope_resolver_headers
  request_timeout_ms = local.scope_lookup_timeout_ms

  retry {
    attempts = local.scope_lookup_retry_attempts
  }

  lifecycle {
    postcondition {
      condition     = self.status_code == 200
      error_message = "metrics_scopes: the Metrics Scope of ${each.key} could not be read by ${local.scope_resolver_identity}; the Monitoring API answered HTTP ${self.status_code}: ${substr(try(jsondecode(self.response_body).error.message, self.response_body), 0, local.scope_lookup_error_excerpt_chars)}. That identity needs to read the scope in its scoping project (roles/monitoring.metricsScopesViewer is the narrowest role), and monitoring.googleapis.com enabled there; or drop it from metrics_scopes.${anytrue([for marker in local.scope_consumer_denied_markers : strcontains(self.response_body, marker)]) ? local.scope_consumer_denied_clause : anytrue([for marker in local.scope_api_off_markers : strcontains(self.response_body, marker)]) ? local.scope_api_off_clause[local.scope_monitoring_api_service] : ""} Nothing was applied."
    }
    postcondition {
      # A scope always monitors its own scoping project, so a 200 that
      # decodes to no monitored project is not a document this module reads
      # either, and is refused rather than resolved to nothing.
      condition     = self.status_code != 200 || (can(keys(jsondecode(self.response_body))) && length(try(jsondecode(self.response_body).monitoredProjects, [])) > 0 && can([for row in try(jsondecode(self.response_body).monitoredProjects, []) : regex(local.scope_monitored_project_name_pattern, row.name)]))
      error_message = "metrics_scopes: the Monitoring API's answer for ${each.key} is not the metricsScopes.get document this module reads (a JSON object with at least one monitored project, each named locations/global/metricsScopes/<scope>/projects/<number>); refusing to resolve the scope from it rather than bind a set that may be short. Nothing was applied."
    }
    postcondition {
      # Counted less the monitored projects an exclude entry names by number,
      # the filter scope_monitored_numbers applies below, so the remedy the
      # message offers lowers the count it is tested against.
      condition     = self.status_code != 200 || length(distinct([for row in try(jsondecode(self.response_body).monitoredProjects, []) : try(regex(local.scope_monitored_project_name_pattern, row.name)["project"], "") if !contains(var.exclude_projects, try(regex(local.scope_monitored_project_name_pattern, row.name)["project"], ""))])) <= local.scope_selector_member_cap
      error_message = "metrics_scopes: the Metrics Scope of ${each.key} monitors more than ${local.scope_selector_member_cap} projects not named by number in exclude_projects, more than the reconcile lists of the whole resolved set (RESOLVED_SET_CAP, the management project included), so the scope cannot fit whatever else the declaration holds; the members past the cap would read over-cap with nothing created under them, and their read roles would be reach the agent never uses. Declare a narrower scope, name the numbers not wanted in exclude_projects (the scope's exclude.projects), or declare the projects wanted in the scope's projects instead. Nothing was applied."
    }
  }
}

locals {
  # Every service project the API named, by ID, and the ones the scope can
  # carry. A legacy domain-scoped ID (`example.com:name`) cannot be declared,
  # bound under a state key the CRD's patterns accept, excluded (the exclude
  # pattern carries no `:`) or given a profile, and unlike a monitored project
  # it has no number to be excluded by, so it is left out of `members` and
  # reported in `uncarriable_members` and by the check below, rather than
  # refusing the whole host over one project nothing in the scope model can
  # hold.
  scope_shared_vpc_named = {
    for host, response in data.http.scope_shared_vpc_host :
    host => response.status_code == 200 && can(keys(jsondecode(response.response_body))) ? sort(distinct(compact([
      for resource in try(jsondecode(response.response_body).resources, []) :
      try(resource.type, "") == local.scope_xpn_resource_type_project ? try(resource.id, "") : ""
    ]))) : []
  }
  scope_shared_vpc_members = {
    for host, named in local.scope_shared_vpc_named :
    host => [for member in named : member if can(regex(local.scope_project_id_pattern, member))]
  }
  scope_shared_vpc_uncarriable = {
    for host, named in local.scope_shared_vpc_named :
    "sharedVpcHosts/${host}" => [for member in named : member if !can(regex(local.scope_project_id_pattern, member))]
    if length([for member in named : member if !can(regex(local.scope_project_id_pattern, member))]) > 0
  }

  # A scope's monitored projects as the API named them: by number, or, should
  # the API ever name one by ID, as it came; an ID that came that way is
  # filtered below like a service project's, since it has no number to be
  # excluded by and the by-number postcondition never saw it.
  scope_monitored_projects = {
    for scope, response in data.http.scope_metrics_scope :
    scope => distinct(compact([
      for row in try(jsondecode(response.response_body).monitoredProjects, []) :
      try(regex(local.scope_monitored_project_name_pattern, row.name)["project"], "")
    ]))
  }

  # The numbers to name: every monitored project the Monitoring API returned by
  # number, less the ones an exclude entry names by that number (the runtime's
  # own escape for a project the account cannot name, design §10 step 3), so
  # an excluded number is neither read nor bound.
  scope_monitored_numbers = toset([
    for member in flatten(values(local.scope_monitored_projects)) : member
    if can(regex(local.scope_project_number_pattern, member)) && !contains(var.exclude_projects, member)
  ])
}

data "http" "scope_monitored_project" {
  for_each = local.scope_monitored_numbers

  url                = "${local.scope_resource_manager_api_url}/projects/${each.key}"
  request_headers    = local.scope_resolver_headers
  request_timeout_ms = local.scope_lookup_timeout_ms

  retry {
    attempts = local.scope_lookup_retry_attempts
  }

  lifecycle {
    postcondition {
      condition     = self.status_code == 200
      error_message = "metrics_scopes: monitored project ${each.key} could not be named by ${local.scope_resolver_identity}; Resource Manager answered HTTP ${self.status_code}: ${substr(try(jsondecode(self.response_body).error.message, self.response_body), 0, local.scope_lookup_error_excerpt_chars)}. A project this identity cannot name it cannot bind, and the agent would read it denied. Ask for resourcemanager.projects.get on projects/${each.key} for that identity, or name the number in exclude_projects (the scope's exclude.projects) to leave it out.${anytrue([for marker in local.scope_consumer_denied_markers : strcontains(self.response_body, marker)]) ? local.scope_consumer_denied_clause : anytrue([for marker in local.scope_api_off_markers : strcontains(self.response_body, marker)]) ? local.scope_api_off_clause[local.scope_resource_manager_api_service] : ""} Nothing was applied."
    }
    postcondition {
      condition     = self.status_code != 200 || can(regex(local.scope_project_id_pattern, jsondecode(self.response_body).projectId))
      error_message = "metrics_scopes: monitored project ${each.key} is named ${try(jsondecode(self.response_body).projectId, "<unreadable>")}, a project ID the scope cannot carry (the CRD accepts ${local.scope_project_id_pattern}; a legacy domain-scoped ID does not match); the reconcile reports it denied by number. Name the number in exclude_projects (the scope's exclude.projects) to leave it out. Nothing was applied."
    }
  }
}

locals {
  scope_project_id_by_number = {
    for number, response in data.http.scope_monitored_project :
    number => try(jsondecode(response.response_body).projectId, "")
  }

  scope_metrics_scope_named = {
    for scope, members in local.scope_monitored_projects :
    scope => sort(distinct(compact([
      for member in members :
      can(regex(local.scope_project_number_pattern, member)) ? lookup(local.scope_project_id_by_number, member, "") : member
    ])))
  }
  # A number the postcondition above let through names an ID the scope can
  # carry, so this filter only ever holds back a member the API named by an
  # ID it cannot; such a member is reported, not bound, and never refuses the
  # scope, the rule the host path applies.
  scope_metrics_scope_members = {
    for scope, named in local.scope_metrics_scope_named :
    scope => [for member in named : member if can(regex(local.scope_project_id_pattern, member))]
  }
  scope_metrics_scope_uncarriable = {
    for scope, named in local.scope_metrics_scope_named :
    "metricsScopes/${scope}" => [for member in named : member if !can(regex(local.scope_project_id_pattern, member))]
    if length([for member in named : member if !can(regex(local.scope_project_id_pattern, member))]) > 0
  }
  scope_selector_uncarriable = merge(local.scope_shared_vpc_uncarriable, local.scope_metrics_scope_uncarriable)

  # Each selector's members under the name the snapshot's `containers` array
  # gives it, so the output is comparable with fleet_scope.json line by line.
  scope_selector_members = merge(
    { for host, members in local.scope_shared_vpc_members : "sharedVpcHosts/${host}" => members },
    { for scope, members in local.scope_metrics_scope_members : "metricsScopes/${scope}" => members },
  )
}

# A warning, not a refusal: the member is not in the set, and the plan says so
# here and in `uncarriable_members`, while the rest of the selector's members
# are bound as usual.
check "selector_members_the_scope_can_carry" {
  assert {
    condition     = length(local.scope_selector_uncarriable) == 0
    error_message = "shared_vpc_hosts / metrics_scopes: ${join("; ", [for selector, members in local.scope_selector_uncarriable : "${selector} names project(s) ${join(", ", members)}"])} with an ID the scope cannot carry (the CRD accepts ${local.scope_project_id_pattern}; a legacy domain-scoped ID does not match). Left out of the bindings; the reconcile reports such a project on its own."
  }
}

