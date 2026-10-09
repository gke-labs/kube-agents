"""The IAM half of `spec.scope`, read from the Terraform as text.

docs/designs/multi-project-scope.md §6: a scoped project gets a fixed read
allowlist intersected with project_roles, never project_roles itself; every
allowlist entry is one the agent holds at home; the host project is never bound
twice; and the composition feeds the module and the chart from one value. The
terraform binary is not a suite dependency, so this reads the HCL the way
tests/test_scoped_sa_pool_iam.py does. What the rules evaluate to is under
terraform/modules/*/tests/ (`make terraform-test`), against mocked providers.

Run: python3 -m unittest discover -s tests -p 'test_scope_iam.py' -v
"""

import json
import pathlib
import re
import unittest

import yaml

try:
    from tests.test_scoped_sa_pool_iam import _hcl_string_list, _hcl_variable_default_list
except ImportError:  # run from inside tests/
    from test_scoped_sa_pool_iam import _hcl_string_list, _hcl_variable_default_list

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODULE = _REPO_ROOT / "terraform" / "modules" / "kube-agents-iam"
_COMPOSITION = _REPO_ROOT / "terraform" / "examples" / "full-install"
_RESOLVER = _REPO_ROOT / "terraform" / "modules" / "kube-agents-scope-resolver"
# The chart's copy of the CRD, which make chart-check holds byte-identical to
# the operator's generated one; the constraints below are read from it.
_CRD = _REPO_ROOT / "charts" / "kube-agents" / "crds" / "kubeagents.x-k8s.io_platformagents.yaml"

# docs/designs/multi-project-scope.md §6, verbatim.
DESIGN_ALLOWLIST = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
    "roles/compute.viewer",
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/iam.securityReviewer",
]
# Kept in the host project on purpose (§6): actAs, the MCP server's check, quota.
HOST_ONLY_ROLES = [
    "roles/iam.serviceAccountUser",
    "roles/mcp.toolUser",
    "roles/serviceusage.serviceUsageConsumer",
]
# The allowlist entries that carry container.clusters.list AND .get; a scoped
# project bound with neither cannot be listed or have a profile created.
# roles/iam.securityReviewer lists but cannot get, so it is not one of them.
MANAGING_ROLES = [
    "roles/container.clusterViewer",
    "roles/container.viewer",
]


def _block(source, kind, name):
    match = re.search(rf'^{kind}\s+"{name}"(?:\s+"[^"]+")?\s*\{{(.*?)^\}}', source, re.MULTILINE | re.DOTALL)
    assert match, f"{kind} {name} not found"
    return match.group(1)


def _resource(source, kind, name):
    match = re.search(rf'^resource\s+"{kind}"\s+"{name}"\s*\{{(.*?)^\}}', source, re.MULTILINE | re.DOTALL)
    assert match, f"resource {kind}.{name} not found"
    return match.group(1)


class ScopeAllowlistTest(unittest.TestCase):
    def setUp(self):
        self.scope_tf = (_MODULE / "scope.tf").read_text()
        self.variables = (_MODULE / "variables.tf").read_text()
        self.main_tf = (_MODULE / "main.tf").read_text()

    def test_the_allowlist_is_the_designs(self):
        self.assertEqual(_hcl_string_list(self.scope_tf, "scope_role_allowlist"), DESIGN_ALLOWLIST)

    def test_every_allowlisted_role_is_one_the_agent_holds_at_home(self):
        defaults = _hcl_variable_default_list(self.variables, "project_roles")
        for role in DESIGN_ALLOWLIST:
            with self.subTest(role=role):
                self.assertIn(role, defaults)

    def test_the_host_only_roles_stay_out(self):
        allowlist = _hcl_string_list(self.scope_tf, "scope_role_allowlist")
        for role in HOST_ONLY_ROLES:
            with self.subTest(role=role):
                self.assertNotIn(role, allowlist)

    def test_the_managing_roles_are_the_two_that_list_and_get(self):
        managing = _hcl_string_list(self.scope_tf, "scope_managing_roles")
        self.assertEqual(managing, MANAGING_ROLES)
        self.assertNotIn("roles/iam.securityReviewer", managing)
        for role in managing:
            self.assertIn(role, DESIGN_ALLOWLIST)

    def test_the_binding_reads_the_intersection_and_never_project_roles(self):
        self.assertIn("scope_roles = [for role in local.scope_role_allowlist : role if contains(var.project_roles, role)]",
                      self.scope_tf)
        binding = _resource(self.scope_tf, "google_project_iam_member", "scope_roles")
        self.assertIn("for_each = local.scope_bindings", binding)
        self.assertIn("role    = each.value.role", binding)
        self.assertNotIn("var.project_roles", binding)
        self.assertIn('member  = "serviceAccount:${google_service_account.agent.email}"', binding)

    def test_the_host_project_is_never_bound_twice(self):
        # The filter, and the wire from it to the bindings: a for_each fed from
        # var.scope.projects directly would bind the host twice and leave this
        # local unused.
        self.assertIn("if project != var.project_id]", self.scope_tf)
        self.assertIn("scope_bound_projects = setunion(local.scope_projects, local.scope_selector_projects)", self.scope_tf)
        self.assertIn("setproduct(sort(tolist(local.scope_bound_projects)), local.scope_roles)", self.scope_tf)

    def test_an_unmanageable_scope_fails_the_plan(self):
        # For a container as for a project: a folder bound with no role that
        # lists and gets clusters would read ok and fail every profile create.
        agent = _resource(self.main_tf, "google_service_account", "agent")
        self.assertIn("condition     = !local.scope_declares_anything || local.scope_can_manage", agent)
        self.assertIn("PLATFORM_AGENT_CUSTOM_ROLES", agent)
        self.assertIn("scope_can_manage = anytrue([for role in local.scope_roles : contains(local.scope_managing_roles, role)])",
                      self.scope_tf)
        self.assertIn("scope_declares_anything = length(local.scope_projects) + length(local.scope_folders) + length(local.scope_organizations) + length(local.scope_shared_vpc_hosts) + length(local.scope_metrics_scopes) > 0",
                      self.scope_tf)


class ScopeContainerBindingsTest(unittest.TestCase):
    """A folder or organisation is bound on the container itself with the
    intersected allowlist plus roles/cloudasset.viewer, the one role the
    reconcile's container search needs there (design §6), and nothing else."""

    def setUp(self):
        self.scope_tf = (_MODULE / "scope.tf").read_text()

    def test_the_container_roles_are_the_allowlist_plus_the_asset_viewer(self):
        self.assertIn('scope_container_asset_role = "roles/cloudasset.viewer"', self.scope_tf)
        self.assertIn("scope_container_roles = concat(local.scope_roles, [local.scope_container_asset_role])", self.scope_tf)
        # Not in the project allowlist: the host project is listed with
        # `clusters list`, never searched.
        self.assertNotIn("roles/cloudasset.viewer", _hcl_string_list(self.scope_tf, "scope_role_allowlist"))

    def test_folders_bind_on_the_folder_with_the_container_roles(self):
        binding = _resource(self.scope_tf, "google_folder_iam_member", "scope_roles")
        self.assertIn("for_each = local.scope_folder_bindings", binding)
        self.assertIn('folder = "folders/${each.value.folder}"', binding)
        self.assertIn("role   = each.value.role", binding)
        self.assertIn('member = "serviceAccount:${google_service_account.agent.email}"', binding)
        self.assertNotIn("var.project_roles", binding)
        self.assertIn("setproduct(sort(tolist(local.scope_folders)), local.scope_container_roles)", self.scope_tf)

    def test_organisations_bind_on_the_organisation_with_the_container_roles(self):
        binding = _resource(self.scope_tf, "google_organization_iam_member", "scope_roles")
        self.assertIn("for_each = local.scope_organization_bindings", binding)
        self.assertIn("org_id = each.value.organization", binding)
        self.assertIn("role   = each.value.role", binding)
        self.assertIn('member = "serviceAccount:${google_service_account.agent.email}"', binding)
        self.assertNotIn("var.project_roles", binding)
        self.assertIn("setproduct(sort(tolist(local.scope_organizations)), local.scope_container_roles)", self.scope_tf)

    def test_the_outputs_surface_the_containers_and_their_roles(self):
        outputs = (_MODULE / "outputs.tf").read_text()
        self.assertIn("value       = sort(tolist(local.scope_folders))", outputs)
        self.assertIn("value       = sort(tolist(local.scope_organizations))", outputs)
        self.assertIn("value       = local.scope_container_roles", outputs)


class ScopeSelectorResolutionTest(unittest.TestCase):
    """A Shared VPC host or a Metrics Scope inherits nothing, so it is resolved
    to projects at plan time, as the identity the provider applies with, by the
    kube-agents-scope-resolver module, and the IAM module binds the same
    allowlist in each (design §6, §10 step 3). Read as text: the reads, whose
    token they carry, what fails the plan, and that the members reach the one
    project binding."""

    def setUp(self):
        self.resolver_tf = (_RESOLVER / "main.tf").read_text()
        self.scope_tf = (_MODULE / "scope.tf").read_text()
        self.main_tf = (_MODULE / "main.tf").read_text()
        self.versions = (_RESOLVER / "versions.tf").read_text()

    def _data(self, kind, name):
        match = re.search(rf'^data\s+"{kind}"\s+"{name}"\s*\{{(.*?)^\}}', self.resolver_tf, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, f"data {kind}.{name} not found in the resolver")
        return match.group(1)

    def test_the_resolution_is_not_inside_the_module_the_composition_orders(self):
        # The composition calls kube-agents-iam with a module-level depends_on,
        # which defers every data source in it to apply time on a first install
        # and fails a for_each keyed on the read; so the IAM module reads
        # nothing and takes the members as an input, and refuses a declared
        # selector the input does not carry.
        # Any data source, in any of the module's files: the hazard is a for_each
        # keyed on a deferred read, whichever provider answers it.
        for tf in sorted(_MODULE.glob("*.tf")):
            with self.subTest(file=tf.name):
                self.assertNotRegex(tf.read_text(), r'(?m)^data\s+"', "the IAM module reads nothing")
        self.assertNotIn("hashicorp/http", (_MODULE / "versions.tf").read_text())
        self.assertIn('variable "scope_selector_members"', (_MODULE / "variables.tf").read_text())
        self.assertIn("condition     = local.scope_selectors_resolved", self.main_tf)
        self.assertIn("kube-agents-scope-resolver", self.main_tf)

    def test_the_reads_are_the_reconciles_against_the_apis_it_calls(self):
        # Three selector reads, and the container listing for the pool, each
        # the call the reconcile makes against the same API.
        host = self._data("http", "scope_shared_vpc_host")
        self.assertIn("for_each = local.scope_shared_vpc_hosts", host)
        self.assertIn('url                = "${local.scope_compute_api_url}/projects/${each.key}/getXpnResources?maxResults=${local.scope_xpn_page_size}"', host)
        scope = self._data("http", "scope_metrics_scope")
        self.assertIn("for_each = local.scope_metrics_scopes", scope)
        self.assertIn('url                = "${local.scope_monitoring_api_url}/locations/global/metricsScopes/${each.key}"', scope)
        named = self._data("http", "scope_monitored_project")
        self.assertIn("for_each = local.scope_monitored_numbers", named)
        self.assertIn('url                = "${local.scope_resource_manager_api_url}/projects/${each.key}"', named)
        container = self._data("http", "scope_container")
        self.assertIn("for_each = local.scope_listed_containers", container)
        self.assertIn("local.scope_container_url[each.key]", container)
        self.assertIn('scope_asset_api_url', self.resolver_tf)
        self.assertIn('scope_compute_api_url          = "https://compute.googleapis.com/compute/v1"', self.resolver_tf)
        self.assertIn('scope_monitoring_api_url       = "https://monitoring.googleapis.com/v1"', self.resolver_tf)
        self.assertIn('scope_resource_manager_api_url = "https://cloudresourcemanager.googleapis.com/v3"', self.resolver_tf)

    def test_every_read_carries_the_providers_own_token(self):
        # google_client_config is the provider's configured identity, impersonation
        # included: the lookup passes or fails for the principal that applies.
        self.assertIn('data "google_client_config" "scope_resolver"', self.resolver_tf)
        self.assertIn('Authorization         = "Bearer ${data.google_client_config.scope_resolver[0].access_token}"', self.resolver_tf)
        # And the management project as the consumer project, so the APIs the
        # reads use are the ones the composition enables there, whichever
        # credential type the provider holds. A 403 that is not a grant on the
        # project read is named for what it is, with its own remedy: the API
        # off in the consumer project gets the enable command, the identity
        # refused the consumer project (USER_PROJECT_DENIED) gets the
        # serviceusage.services.use grant it needs there, and the consumer
        # clause is read first, since a credential without a consumer project
        # is answered with both sentences. Enabling an API that is on fixes
        # nothing, so the two must not share a remedy.
        self.assertIn('"x-goog-user-project" = var.quota_project', self.resolver_tf)
        self.assertIn('variable "quota_project"', (_RESOLVER / "variables.tf").read_text())
        # The management project is whatever the install has, a legacy
        # domain-scoped ID included (install.sh's is_valid_project_id admits
        # one), and the module is in every plan whether or not a selector is
        # declared, so its rule is the installer's two-sided one, not the CRD's.
        quota = re.search(r'variable "quota_project" \{.*?regex\("([^"]+)", var\.quota_project\)', (_RESOLVER / "variables.tf").read_text(), re.DOTALL)
        self.assertIsNotNone(quota, "quota_project has no regex validation")
        pattern = re.compile(quota.group(1))
        for accepted in ("my-project", "example.com:my-project", "sub.example.com:my-project"):
            self.assertRegex(accepted, pattern)
        for refused in ("My_Project", "abc", ":my-project", "example.com:"):
            self.assertNotRegex(refused, pattern)
        self.assertIn('scope_api_off_markers              = ["SERVICE_DISABLED", "has not been used in project"]', self.resolver_tf)
        self.assertIn('scope_consumer_denied_markers      = ["USER_PROJECT_DENIED", "quota project"]', self.resolver_tf)
        self.assertIn('scope_consumer_role                = "roles/serviceusage.serviceUsageConsumer"', self.resolver_tf)
        self.assertEqual(self.resolver_tf.count("is off in ${var.quota_project}, the project these reads are billed to"), 1)
        self.assertEqual(self.resolver_tf.count("It needs serviceusage.services.use there (${local.scope_consumer_role} carries it"), 1)
        self.assertIn("the API is on, and enabling it changes nothing.", self.resolver_tf)
        consumer_first = ('${anytrue([for marker in local.scope_consumer_denied_markers : strcontains(self.response_body, marker)]) ? local.scope_consumer_denied_clause : '
                          'anytrue([for marker in local.scope_api_off_markers : strcontains(self.response_body, marker)]) ? local.scope_api_off_clause[local.')
        for name, api in (("scope_shared_vpc_host", "scope_compute_api_service"),
                          ("scope_metrics_scope", "scope_monitoring_api_service"),
                          ("scope_monitored_project", "scope_resource_manager_api_service"),
                          ("scope_container", "scope_asset_api_service")):
            with self.subTest(read=name):
                self.assertIn("request_headers    = local.scope_resolver_headers", self._data("http", name))
                self.assertIn(consumer_first + api + '] : ""}', self._data("http", name))
        # No read shells out: an external program would answer for gcloud's
        # active account, which need not be the provider's identity.
        self.assertNotIn('data "external"', self.resolver_tf)
        self.assertNotIn("local-exec", self.resolver_tf)

    def test_the_http_provider_is_required_and_no_lookup_is_made_without_a_selector(self):
        self.assertIn('source  = "hashicorp/http"', self.versions)
        # The token is fetched when a selector is declared or a container is
        # listed for the pool, and never otherwise.
        self.assertIn("count = local.scope_reads_apis ? 1 : 0", self._data("google_client_config", "scope_resolver"))
        self.assertIn("scope_resolves_selectors = length(local.scope_shared_vpc_hosts) + length(local.scope_metrics_scopes) > 0", self.resolver_tf)
        self.assertIn("scope_reads_apis = local.scope_resolves_selectors || length(local.scope_listed_containers) > 0", self.resolver_tf)

    def test_a_failed_read_fails_the_plan_and_a_non_host_resolves_to_nothing(self):
        host = self._data("http", "scope_shared_vpc_host")
        self.assertIn("self.status_code == 200 || (self.status_code == 400 && strcontains(self.response_body, local.scope_not_xpn_host_marker))", host)
        self.assertIn('scope_not_xpn_host_marker            = "is not a shared VPC host project"', self.resolver_tf)
        self.assertIn("Nothing was applied.", host)
        self.assertIn("!can(jsondecode(self.response_body).nextPageToken)", host)
        for name in ("scope_metrics_scope", "scope_monitored_project"):
            with self.subTest(read=name):
                self.assertIn("condition     = self.status_code == 200\n", self._data("http", name))
        # A 200 whose body does not decode, or is not the document the module
        # reads, is refused, never read as an empty selector: try(..., [])
        # alone would pass it and the next apply would revoke every member.
        # An object, not merely JSON: a list, a string or null decode too, and
        # `.resources` on them is what try() would swallow into an empty host.
        self.assertIn('(can(keys(jsondecode(self.response_body))) && can([for resource in try(jsondecode(self.response_body).resources, []) : "${resource.id}/${resource.type}"]))', host)
        self.assertIn("response.status_code == 200 && can(keys(jsondecode(response.response_body)))", self.resolver_tf)
        scope = self._data("http", "scope_metrics_scope")
        self.assertIn("can(keys(jsondecode(self.response_body))) && length(try(jsondecode(self.response_body).monitoredProjects, [])) > 0", scope)
        # A selector past the cap is refused rather than bound in full, counted
        # as the reconcile counts: less the members an exclude entry names
        # exactly (by ID for a service project, by number for a monitored
        # project), so the exclusion the message offers as a remedy lowers the
        # count it is tested against.
        self.assertIn("scope_selector_member_cap       = var.member_cap", self.resolver_tf)
        self.assertIn('try(resource.type, "") == local.scope_xpn_resource_type_project && !contains(var.exclude_projects, try(resource.id, ""))])) <= local.scope_selector_member_cap', host)
        self.assertIn('if !contains(var.exclude_projects, try(regex(local.scope_monitored_project_name_pattern, row.name)["project"], ""))])) <= local.scope_selector_member_cap', scope)
        self.assertNotIn("length(try(jsondecode(self.response_body).monitoredProjects, [])) <= local.scope_selector_member_cap", scope)
        self.assertIn("not named by number in exclude_projects", scope)
        self.assertIn("not named in exclude_projects", host)
        self.assertIn("can([for row in try(jsondecode(self.response_body).monitoredProjects, []) : regex(local.scope_monitored_project_name_pattern, row.name)])", scope)
        # A legacy domain-scoped ID the scope cannot carry is refused by number
        # for a monitored project, which has a number to be excluded by; a
        # service project has none, so it is left out, listed and warned about
        # rather than refusing the host.
        self.assertIn("can(regex(local.scope_project_id_pattern, jsondecode(self.response_body).projectId))", self._data("http", "scope_monitored_project"))
        self.assertIn("Name the number in exclude_projects", self._data("http", "scope_monitored_project"))
        self.assertIn("host => [for member in named : member if can(regex(local.scope_project_id_pattern, member))]", self.resolver_tf)
        # And a monitored project the API should ever name by such an ID, which
        # the by-number postcondition never saw, goes through the same filter
        # rather than reaching kube-agents-iam's variable validation unnamed.
        self.assertIn("scope => [for member in named : member if can(regex(local.scope_project_id_pattern, member))]", self.resolver_tf)
        self.assertIn("scope_selector_uncarriable = merge(local.scope_shared_vpc_uncarriable, local.scope_metrics_scope_uncarriable)", self.resolver_tf)
        self.assertIn('check "selector_members_the_scope_can_carry"', self.resolver_tf)
        self.assertIn("condition     = length(local.scope_uncarriable) == 0", self.resolver_tf)
        self.assertIn("value       = local.scope_uncarriable", (_RESOLVER / "outputs.tf").read_text())

    def test_the_members_reach_the_one_project_binding_with_the_lookup_projects_and_less_an_exact_exclude(self):
        self.assertIn("scope_bound_projects = setunion(local.scope_projects, local.scope_selector_projects)", self.scope_tf)
        self.assertIn("for pair in setproduct(sort(tolist(local.scope_bound_projects)), local.scope_roles) :", self.scope_tf)
        selector = re.search(r"scope_selector_projects = toset\(concat\((.*?)\n  \)\)", self.scope_tf, re.DOTALL).group(1)
        # Only the declared selectors' entries are read from the input.
        self.assertIn("lookup(var.scope_selector_members, name, [])", selector)
        # An ID exclusion withholds a Shared VPC service project's grant only:
        # a monitored project excluded by ID still needs the grant for the
        # reconcile's naming call, or it is reported unnamed and holds the
        # scope prune every tick; one excluded by number never arrives here.
        self.assertIn('if pair.project != var.project_id && !(startswith(pair.name, "sharedVpcHosts/") && contains(var.scope.exclude.projects, pair.project))', selector)
        # The scoping project is bound with the allowlist whatever exclude
        # says, and a host not otherwise in scope with the lookup role alone:
        # the reconcile's lookups read them, and an unbound one freezes the
        # selector every tick, while the rest of the allowlist has no consumer
        # in a host whose clusters are never listed.
        self.assertIn("[for scope in local.scope_metrics_scopes : scope if scope != var.project_id]", selector)
        self.assertNotIn("local.scope_shared_vpc_hosts", selector)
        self.assertIn("if host != var.project_id && !contains(local.scope_bound_projects, host)", self.scope_tf)
        self.assertIn('"${host}/${local.scope_shared_vpc_lookup_role}" => { project = host, role = local.scope_shared_vpc_lookup_role }', self.scope_tf)
        # An excluded number is neither named nor bound, in the resolver.
        self.assertIn("if can(regex(local.scope_project_number_pattern, member)) && !contains(var.exclude_projects, member)", self.resolver_tf)
        # The snapshot's names, so the output reads beside fleet_scope.json.
        self.assertIn('"sharedVpcHosts/${host}" => members', self.resolver_tf)
        self.assertIn('"metricsScopes/${scope}" => members', self.resolver_tf)
        self.assertIn("value       = local.scope_selector_members", (_RESOLVER / "outputs.tf").read_text())

    def test_a_host_without_the_lookup_role_fails_the_plan(self):
        self.assertIn('scope_shared_vpc_lookup_role = "roles/compute.viewer"', self.scope_tf)
        self.assertIn("roles/compute.viewer", DESIGN_ALLOWLIST)
        # No carve-out for a host that is the management project: a custom
        # set with the permission in a custom role and one with no such
        # permission look alike here, and the second would freeze the selector.
        precondition = re.search(r"length\(local\.scope_shared_vpc_hosts\) == 0 \|\| contains\(local\.scope_roles, local\.scope_shared_vpc_lookup_role\)", self.main_tf)
        self.assertIsNotNone(precondition, "main.tf carries no precondition for the host lookup role")
        self.assertIn("scope_declares_anything = length(local.scope_projects) + length(local.scope_folders) + length(local.scope_organizations) + length(local.scope_shared_vpc_hosts) + length(local.scope_metrics_scopes) > 0",
                      self.scope_tf)

    def test_the_whole_resolved_set_past_the_reconciles_cap_fails_the_plan(self):
        # The resolver's bound is per selector; the reconcile's cap is on the
        # whole set (management project, explicit projects, selector members,
        # containers, in that order), so two selectors of 60, or one of 60
        # beside 50 explicit projects, would pass the resolver and bind the
        # read roles in members the reconcile reads over-cap. The IAM module
        # counts the three groups a plan can know, as the reconcile counts
        # them: once each, less an exact exclude entry, the management project
        # always in.
        reconcile = (_REPO_ROOT / "agents" / "platform" / "scripts" / "cluster_agent_reconcile.py").read_text()
        cap = re.search(r"^RESOLVED_SET_CAP = (\d+)$", reconcile, re.MULTILINE)
        self.assertIsNotNone(cap, "cluster_agent_reconcile.py has no RESOLVED_SET_CAP")
        # The cap is the declaration's (scope.max_projects, spec.scope.maxProjects); its
        # default in both modules is the reconcile's constant, so an install that declares
        # none is planned against the number the reconcile lists.
        self.assertIn("scope_resolved_set_cap = var.scope.max_projects", self.scope_tf)
        iam_variables = (_REPO_ROOT / "terraform" / "modules" / "kube-agents-iam" / "variables.tf").read_text()
        self.assertIn(f"max_projects     = optional(number, {cap.group(1)})", iam_variables)
        self.assertIn("scope_selector_member_cap       = var.member_cap", self.resolver_tf)
        resolver_variables = (_REPO_ROOT / "terraform" / "modules" / "kube-agents-scope-resolver" / "variables.tf").read_text()
        member_cap = re.search(r'variable "member_cap" \{.*?default     = (\d+)', resolver_variables, re.DOTALL)
        self.assertIsNotNone(member_cap, "the resolver has no member_cap variable with a default")
        self.assertEqual(member_cap.group(1), cap.group(1))
        self.assertIn("toset([var.project_id]),", self.scope_tf)
        self.assertIn("toset([for project in var.scope.projects : project if !contains(var.scope.exclude.projects, project)]),", self.scope_tf)
        self.assertIn("for project in flatten([for name in local.scope_selector_names : lookup(var.scope_selector_members, name, [])]) : project\n      if !contains(var.scope.exclude.projects, project)\n", self.scope_tf)
        # Held while a selector is declared or the cap is below its default: an
        # install with the CRD's hundred explicit projects, no selector and the
        # default cap planned before this precondition existed and must not start
        # being refused; a cap the operator declared below the default is theirs
        # to be held to with or without a selector.
        self.assertIn("condition     = (length(local.scope_selector_names) == 0 && var.scope.max_projects == local.scope_default_cap) || length(local.scope_listed_projects) <= local.scope_resolved_set_cap", self.main_tf)
        self.assertIn(f"scope_default_cap = {cap.group(1)}", self.scope_tf)
        self.assertIn("A glob is applied by the reconcile alone", self.main_tf)
        # The by-number form lowers the count on the selector leg only: an explicit
        # project the reconcile drops by its number is counted, and the message says so
        # and names the remedy rather than promising the entry lowers the count.
        self.assertIn("by ID for an entry in scope.projects or any selector member", self.main_tf)
        self.assertIn("is dropped by the reconcile but counted here", self.main_tf)
        self.assertIn("drop it from scope.projects", self.main_tf)
        # Containers are not in the count: their members, listed at plan time
        # only for an armed pool, come last in the reconcile's order and their
        # binding is on the container.
        listed = re.search(r"scope_listed_projects = setunion\((.*?)\n  \)", self.scope_tf, re.DOTALL)
        self.assertIsNotNone(listed)
        self.assertNotIn("folders", listed.group(1))
        self.assertNotIn("organizations", listed.group(1))

    def test_the_outputs_surface_the_selectors_and_what_they_resolved_to(self):
        outputs = (_MODULE / "outputs.tf").read_text()
        self.assertIn("value       = sort(tolist(local.scope_shared_vpc_hosts))", outputs)
        self.assertIn("value       = sort(tolist(local.scope_metrics_scopes))", outputs)
        self.assertIn("value       = sort(tolist(local.scope_bound_projects))", outputs)
        self.assertIn("value       = sort(tolist(local.scope_lookup_only_hosts))", outputs)


def _crd_scope_schema():
    crd = yaml.safe_load(_CRD.read_text())
    spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
    return spec["properties"]["scope"]["properties"]


def _hcl_regex(pattern):
    """The CRD's pattern as it has to be spelled inside an HCL string."""
    return pattern.replace("\\", "\\\\")


class ScopeVariableMirrorsTheCrdTest(unittest.TestCase):
    """The module refuses at plan time what the CRD would refuse at admission,
    after IAM had been applied: every cap, pattern and list type is read from
    the CRD here and looked for in the variable's validations, so a change to
    the kubebuilder markers without a matching edit to variables.tf fails."""

    def setUp(self):
        self.variable = _block((_MODULE / "variables.tf").read_text(), "variable", "scope")
        self.crd = _crd_scope_schema()

    def test_the_shape_and_defaults(self):
        for line in ("projects         = optional(list(string), [])",
                     "folders          = optional(list(string), [])",
                     "organizations    = optional(list(string), [])",
                     "shared_vpc_hosts = optional(list(string), [])",
                     "metrics_scopes   = optional(list(string), [])",
                     "max_projects     = optional(number, 100)",
                     "clusters = optional(list(object({",
                     "nullable = false",
                     "default  = {}"):
            with self.subTest(line=line):
                self.assertIn(line, self.variable)

    def test_each_list_carries_the_crds_cap(self):
        lists = {
            "var.scope.projects": self.crd["projects"],
            "var.scope.folders": self.crd["folders"],
            "var.scope.organizations": self.crd["organizations"],
            "var.scope.shared_vpc_hosts": self.crd["sharedVpcHosts"],
            "var.scope.metrics_scopes": self.crd["metricsScopes"],
            "var.scope.exclude.projects": self.crd["exclude"]["properties"]["projects"],
            "var.scope.exclude.clusters": self.crd["exclude"]["properties"]["clusters"],
        }
        for name, schema in lists.items():
            with self.subTest(list=name):
                self.assertIn(f"length({name}) <= {schema['maxItems']}", self.variable)

    def test_the_project_and_glob_patterns_are_the_crds(self):
        projects = self.crd["projects"]["items"]["pattern"]
        globs = self.crd["exclude"]["properties"]["projects"]["items"]["pattern"]
        self.assertIn(f'regex("{_hcl_regex(projects)}", project)', self.variable)
        self.assertIn(f'regex("{_hcl_regex(globs)}", entry)', self.variable)

    def test_the_selector_pattern_is_the_crds_project_id_for_both_lists(self):
        # A Shared VPC host and a Metrics Scope's scoping project are project IDs.
        hosts = self.crd["sharedVpcHosts"]["items"]["pattern"]
        self.assertEqual(hosts, self.crd["metricsScopes"]["items"]["pattern"])
        self.assertEqual(hosts, self.crd["projects"]["items"]["pattern"])
        self.assertIn(f'for selector in concat(var.scope.shared_vpc_hosts, var.scope.metrics_scopes) : can(regex("{_hcl_regex(hosts)}", selector))',
                      self.variable)

    def test_the_container_id_pattern_is_the_crds_for_both_lists(self):
        folders = self.crd["folders"]["items"]["pattern"]
        self.assertEqual(folders, self.crd["organizations"]["items"]["pattern"])
        self.assertIn(f'for container in concat(var.scope.folders, var.scope.organizations) : can(regex("{_hcl_regex(folders)}", container))',
                      self.variable)

    def test_the_cluster_triple_pattern_and_length_are_the_crds(self):
        # The CRD states the triple's parts as a pattern plus maxLength; the
        # module folds the length into the pattern's quantifier, which is only
        # right while the CRD pattern is the unbounded form asserted here.
        parts = self.crd["exclude"]["properties"]["clusters"]["items"]["properties"]
        for crd_key, tf_key in (("projectId", "project_id"), ("location", "location"), ("clusterName", "cluster_name")):
            with self.subTest(part=crd_key):
                schema = parts[crd_key]
                self.assertEqual(schema["pattern"], "^[a-z0-9][a-z0-9-]*$")
                bounded = f"^[a-z0-9][a-z0-9-]{{0,{schema['maxLength'] - 1}}}$"
                self.assertIn(f'regex("{bounded}", cluster.{tf_key})', self.variable)

    def test_the_crds_set_and_map_lists_are_checked_for_repeats(self):
        self.assertEqual(self.crd["projects"]["x-kubernetes-list-type"], "set")
        self.assertEqual(self.crd["folders"]["x-kubernetes-list-type"], "set")
        self.assertEqual(self.crd["organizations"]["x-kubernetes-list-type"], "set")
        self.assertEqual(self.crd["sharedVpcHosts"]["x-kubernetes-list-type"], "set")
        self.assertEqual(self.crd["metricsScopes"]["x-kubernetes-list-type"], "set")
        self.assertEqual(self.crd["exclude"]["properties"]["projects"]["x-kubernetes-list-type"], "set")
        self.assertEqual(self.crd["exclude"]["properties"]["clusters"]["x-kubernetes-list-type"], "map")
        for rule in ("length(distinct(var.scope.projects)) == length(var.scope.projects)",
                     "length(distinct(var.scope.folders)) == length(var.scope.folders)",
                     "length(distinct(var.scope.organizations)) == length(var.scope.organizations)",
                     "length(distinct(var.scope.shared_vpc_hosts)) == length(var.scope.shared_vpc_hosts)",
                     "length(distinct(var.scope.metrics_scopes)) == length(var.scope.metrics_scopes)",
                     "length(distinct(var.scope.exclude.projects)) == length(var.scope.exclude.projects)",
                     'length(distinct([for c in var.scope.exclude.clusters : "${c.project_id}/${c.location}/${c.cluster_name}"])) == length(var.scope.exclude.clusters)'):
            with self.subTest(rule=rule[:50]):
                self.assertIn(rule, self.variable)


class ScopeCapBoundsMirrorTheCrdTest(unittest.TestCase):
    """spec.scope.maxProjects's bounds and default are written in six places; the CRD is
    the source, and each copy is read against it so a bound moved on the CRD alone reds."""

    def setUp(self):
        crd = yaml.safe_load((_REPO_ROOT / "charts" / "kube-agents" / "crds" / "kubeagents.x-k8s.io_platformagents.yaml").read_text())
        scope = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["scope"]["properties"]
        self.minimum, self.maximum, self.default = scope["maxProjects"]["minimum"], scope["maxProjects"]["maximum"], scope["maxProjects"]["default"]

    def test_the_chart_schema_carries_the_crds_bounds(self):
        schema = json.loads((_REPO_ROOT / "charts" / "kube-agents" / "values.schema.json").read_text())
        cap = schema["properties"]["platformAgent"]["properties"]["scope"]["properties"]["maxProjects"]
        self.assertEqual((cap["minimum"], cap["maximum"]), (self.minimum, self.maximum))

    def test_both_modules_validate_the_crds_bounds_and_default_to_its_default(self):
        iam = (_MODULE / "variables.tf").read_text()
        resolver = (_REPO_ROOT / "terraform" / "modules" / "kube-agents-scope-resolver" / "variables.tf").read_text()
        self.assertIn(f"var.scope.max_projects >= {self.minimum} && var.scope.max_projects <= {self.maximum}", iam)
        self.assertIn(f"max_projects     = optional(number, {self.default})", iam)
        self.assertIn(f"var.member_cap >= {self.minimum} && var.member_cap <= {self.maximum}", resolver)
        self.assertIn(f"default     = {self.default}", resolver[resolver.index('variable "member_cap"'):])

    def test_the_installer_the_operator_and_the_reconcile_carry_the_same_numbers(self):
        common = (_REPO_ROOT / "scripts" / "installer" / "installer_common.sh").read_text()
        self.assertIn(f"readonly SCOPE_MAX_PROJECTS_MIN={self.minimum}\n", common)
        self.assertIn(f"readonly SCOPE_MAX_PROJECTS_MAX={self.maximum}\n", common)
        self.assertIn(f"readonly SCOPE_MAX_PROJECTS_DEFAULT={self.default}\n", common)
        self.assertIn(f"{self.minimum} to {self.maximum}, {self.default} when", (_REPO_ROOT / "install.sh").read_text())
        self.assertIn(f"const defaultScopeMaxProjects int32 = {self.default}",
                      (_REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go").read_text())
        reconcile = (_REPO_ROOT / "agents" / "platform" / "scripts" / "cluster_agent_reconcile.py").read_text()
        self.assertIn(f"RESOLVED_SET_CAP = {self.default}\n", reconcile)


class ScopeReachesBothHalvesTest(unittest.TestCase):
    """One variable feeds the module's bindings and the chart's CR block."""

    def setUp(self):
        self.main_tf = (_COMPOSITION / "main.tf").read_text()
        self.variables = (_COMPOSITION / "variables.tf").read_text()

    def test_the_composition_declares_the_variable_like_the_module(self):
        variable = _block(self.variables, "variable", "scope")
        for line in ("projects         = optional(list(string), [])",
                     "folders          = optional(list(string), [])",
                     "organizations    = optional(list(string), [])",
                     "shared_vpc_hosts = optional(list(string), [])",
                     "metrics_scopes   = optional(list(string), [])",
                     "max_projects     = optional(number, 100)"):
            with self.subTest(line=line):
                self.assertIn(line, variable)
        self.assertIn("nullable = false", variable)
        self.assertIn("default  = {}", variable)

    def test_the_module_gets_the_variable(self):
        module = re.search(r'module "kube_agents_iam" \{(.*?)\n\}', self.main_tf, re.DOTALL).group(1)
        self.assertRegex(module, r"\n  scope +=  *var\.scope\n")

    def test_the_selectors_are_resolved_beside_the_module_and_outside_its_depends_on(self):
        # The resolver is called with no depends_on and no managed-resource
        # input, so its reads happen at plan time on a first install too; the
        # IAM module, which carries the depends_on, gets the members as data.
        resolver = re.search(r'module "scope_resolver" \{(.*?)\n\}', self.main_tf, re.DOTALL)
        self.assertIsNotNone(resolver, "module.scope_resolver is not in the composition")
        body = resolver.group(1)
        self.assertIn('source = "../../modules/kube-agents-scope-resolver"', body)
        self.assertIn("shared_vpc_hosts = var.scope.shared_vpc_hosts", body)
        self.assertIn("metrics_scopes   = var.scope.metrics_scopes", body)
        self.assertIn("exclude_projects = var.scope.exclude.projects", body)
        self.assertIn("quota_project = var.project_id", body)
        self.assertNotIn("depends_on", body)
        # Every input is a var. reference and nothing else: a managed value, a
        # module output or a local derived from either makes an input unknown
        # on a first install, and the for_each keyed on it fails the plan.
        inputs = re.findall(r"^\s*(\w+)\s*=\s*(.+?)\s*$", body, re.MULTILINE)
        self.assertEqual({key for key, _ in inputs}, {"source", "shared_vpc_hosts", "metrics_scopes", "folders", "organizations", "list_container_members", "exclude_projects", "quota_project", "member_cap"})
        self.assertIn(("member_cap", "var.scope.max_projects"), inputs)
        # The containers' members are listed for the scoped service account
        # pool alone (design §6): the resolver reads a container only while
        # the pool is armed, and the IAM module takes what it listed as data,
        # like the selectors' members.
        self.assertIn(("folders", "var.scope.folders"), inputs)
        self.assertIn(("organizations", "var.scope.organizations"), inputs)
        self.assertIn(("list_container_members", "var.scoped_pool_enabled"), inputs)
        for key, value in inputs:
            if key != "source":
                with self.subTest(input=key):
                    self.assertRegex(value, r"^var\.[A-Za-z0-9_.]+$", f"{key} is fed {value}, not a variable")
        iam = re.search(r'module "kube_agents_iam" \{(.*?)\n\}', self.main_tf, re.DOTALL).group(1)
        self.assertIn("scope_selector_members = module.scope_resolver.members", iam)
        self.assertIn("scope_container_members = module.scope_resolver.container_members", iam)
        self.assertIn("depends_on = [google_project_service.required, module.gke_cluster]", iam)

    def test_the_chart_gets_the_same_object_with_the_crds_keys(self):
        values = re.search(r"\n      scope = \{\n(?P<body>.*?)\n      \}\n", self.main_tf, re.DOTALL)
        self.assertIsNotNone(values, "platformAgent.scope is not in the helm values")
        body = values.group("body")
        self.assertIn("projects       = var.scope.projects", body)
        self.assertIn("folders        = var.scope.folders", body)
        self.assertIn("organizations  = var.scope.organizations", body)
        self.assertIn("sharedVpcHosts = var.scope.shared_vpc_hosts", body)
        self.assertIn("metricsScopes  = var.scope.metrics_scopes", body)
        self.assertIn("maxProjects    = var.scope.max_projects", body)
        self.assertIn("projects = var.scope.exclude.projects", body)
        for key in ("projectId   = cluster.project_id",
                    "location    = cluster.location",
                    "clusterName = cluster.cluster_name"):
            with self.subTest(key=key):
                self.assertIn(key, body)

    def test_the_release_waits_for_the_bindings(self):
        release = _resource(self.main_tf, "helm_release", "kube_agents")
        depends = re.search(r"depends_on = \[(.*?)\]", release, re.DOTALL).group(1)
        self.assertIn("module.kube_agents_iam", depends)

    def test_the_outputs_are_surfaced(self):
        outputs = (_COMPOSITION / "outputs.tf").read_text()
        for name in ("scope_projects", "scope_roles", "scope_folders", "scope_organizations", "scope_container_roles",
                     "scope_shared_vpc_hosts", "scope_metrics_scopes", "scope_bound_projects", "scope_lookup_only_hosts"):
            with self.subTest(output=name):
                self.assertIn(f"value       = module.kube_agents_iam.{name}", outputs)
        self.assertIn("value       = module.scope_resolver.members", outputs)

    def test_the_asset_api_is_enabled_only_when_a_container_is_declared(self):
        # An install that names explicit projects alone never calls the Asset
        # API and must not fail under an organisation policy that forbids it
        # (design §4); one that names a folder or organisation needs it on.
        self.assertIn('scope_apis = length(var.scope.folders) + length(var.scope.organizations) > 0 ? [\n    "cloudasset.googleapis.com",\n  ] : []',
                      self.main_tf)
        self.assertIn("required_apis = toset(concat(local.base_apis, local.pubsub_apis, local.chat_apis, local.scope_apis, local.selector_apis))",
                      self.main_tf)
        self.assertNotIn('"cloudasset.googleapis.com"', re.search(r"base_apis = \[(.*?)\]", self.main_tf, re.DOTALL).group(1))
        # The Compute API, which the resolver's getXpnResources read and the reconcile's host
        # lookup use, is enabled when a Shared VPC host is declared, so the composition owns
        # every API the selectors' reads use, as the installer's pre-enablement assumes.
        self.assertIn('selector_apis = length(var.scope.shared_vpc_hosts) > 0 ? [\n    "compute.googleapis.com",\n  ] : []', self.main_tf)
        self.assertIn("local.scope_apis, local.selector_apis))", self.main_tf)
        self.assertNotIn('"compute.googleapis.com"', re.search(r"base_apis = \[(.*?)\]", self.main_tf, re.DOTALL).group(1))


class DriftSinksFollowTheScopeTest(unittest.TestCase):
    """The drift ingress exports every project the declaration lists (design §8, the
    drift-pubsub row): the composition feeds the module's source_projects from one IAM-module
    output, scope_export_projects, which is scope.projects and the selectors' members less the
    host and never a container's Asset-Inventory members (an index gap would otherwise destroy
    a sink and lose the records until the next plan), less the operator's per-project
    exclusions, the way to keep reading a project without exporting its logs."""

    def setUp(self):
        self.main_tf = (_COMPOSITION / "main.tf").read_text()
        self.iam_outputs = (_MODULE / "outputs.tf").read_text()

    def test_the_composition_feeds_the_sinks_from_the_iam_modules_listing(self):
        call = _block(self.main_tf, "module", "drift_pubsub")
        self.assertIn("source_projects                       = local.drift_pubsub_source_projects", call,
                      "the drift module's source_projects must come from the composition's export local")
        self.assertRegex(self.main_tf, r"drift_pubsub_source_projects\s*=\s*sort\(tolist\(setsubtract\(toset\(module\.kube_agents_iam\.scope_export_projects\), local\.drift_pubsub_source_exclude_projects\)\)\)",
                         "the export local must be the IAM module's scope_export_projects less the operator's exclusions; the pool's scope_discovered_projects carries Asset-Inventory members an index gap can drop")
        exclude = _block((_COMPOSITION / "variables.tf").read_text(), "variable", "drift_pubsub_source_exclude_projects")
        self.assertIn("type        = string", exclude, "the exclusion list is a string so a TF_VAR_ line can carry it and a blanked line reads as none")
        self.assertIn('default     = ""', exclude)
        self.assertIn("source_sink_writer_identity_overrides = local.drift_pubsub_source_sink_writer_identity_overrides", call)
        # A JSON string, decoded here, because the front doors reach it through a TF_VAR_ line
        # and a blanked line exports "", which a map-typed variable refuses as HCL.
        self.assertIn('drift_pubsub_source_sink_writer_identity_overrides = trimspace(var.drift_pubsub_source_sink_writer_identity_overrides) == "" ? {} : tomap(jsondecode(var.drift_pubsub_source_sink_writer_identity_overrides))', self.main_tf)
        variable = _block((_COMPOSITION / "variables.tf").read_text(), "variable", "drift_pubsub_source_sink_writer_identity_overrides")
        self.assertIn("type        = string", variable, "the composition's override is a string so a blanked TF_VAR_ line reads as no override")

    def test_the_listing_is_the_pools_set_less_the_host(self):
        output = _block(self.iam_outputs, "output", "scope_discovered_projects")
        self.assertIn("value       = sort(tolist(setsubtract(local.scoped_pool_projects, toset([var.project_id]))))", output,
                      "scope_discovered_projects is no longer the pool's set less the host; tests/test_scoped_sa_pool_iam.py pins what that set is")

    def test_the_export_set_is_the_declaration_less_the_host_and_no_index_member(self):
        output = _block(self.iam_outputs, "output", "scope_export_projects")
        self.assertIn("value       = sort(tolist(setsubtract(local.scope_listed_projects, toset([var.project_id]))))", output,
                      "scope_export_projects must be scope_listed_projects (scope.projects and the selectors' members) less the host, and never "
                      "scoped_pool_projects: a container member the Asset index omits for one plan would lose its sink and its records")
        self.assertNotIn("scoped_pool", output)

    def test_the_source_projects_are_surfaced(self):
        outputs = (_COMPOSITION / "outputs.tf").read_text()
        self.assertIn("value       = try(module.drift_pubsub[0].source_projects, null)", outputs)


if __name__ == "__main__":
    unittest.main()
