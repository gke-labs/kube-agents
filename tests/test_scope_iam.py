"""The IAM half of `spec.scope`, read from the Terraform as text.

docs/designs/multi-project-scope.md §6: a scoped project gets a fixed read
allowlist intersected with project_roles, never project_roles itself; every
allowlist entry is one the agent holds at home; the host project is never bound
twice; and the composition feeds the module and the chart from one value. The
terraform binary is not a suite dependency, so this reads the HCL the way
tests/test_scoped_sa_pool_iam.py does.

Run: python3 -m unittest discover -s tests -p 'test_scope_iam.py' -v
"""

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
        self.assertIn("setproduct(sort(tolist(local.scope_projects)), local.scope_roles)", self.scope_tf)

    def test_an_unmanageable_scope_fails_the_plan(self):
        agent = _resource(self.main_tf, "google_service_account", "agent")
        self.assertIn("condition     = length(local.scope_projects) == 0 || local.scope_can_manage", agent)
        self.assertIn("PLATFORM_AGENT_CUSTOM_ROLES", agent)
        self.assertIn("scope_can_manage = anytrue([for role in local.scope_roles : contains(local.scope_managing_roles, role)])",
                      self.scope_tf)


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
        for line in ("projects = optional(list(string), [])",
                     "clusters = optional(list(object({",
                     "nullable = false",
                     "default  = {}"):
            with self.subTest(line=line):
                self.assertIn(line, self.variable)

    def test_each_list_carries_the_crds_cap(self):
        lists = {
            "var.scope.projects": self.crd["projects"],
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
        self.assertEqual(self.crd["exclude"]["properties"]["projects"]["x-kubernetes-list-type"], "set")
        self.assertEqual(self.crd["exclude"]["properties"]["clusters"]["x-kubernetes-list-type"], "map")
        for rule in ("length(distinct(var.scope.projects)) == length(var.scope.projects)",
                     "length(distinct(var.scope.exclude.projects)) == length(var.scope.exclude.projects)",
                     'length(distinct([for c in var.scope.exclude.clusters : "${c.project_id}/${c.location}/${c.cluster_name}"])) == length(var.scope.exclude.clusters)'):
            with self.subTest(rule=rule[:50]):
                self.assertIn(rule, self.variable)


class ScopeReachesBothHalvesTest(unittest.TestCase):
    """One variable feeds the module's bindings and the chart's CR block."""

    def setUp(self):
        self.main_tf = (_COMPOSITION / "main.tf").read_text()
        self.variables = (_COMPOSITION / "variables.tf").read_text()

    def test_the_composition_declares_the_variable_like_the_module(self):
        variable = _block(self.variables, "variable", "scope")
        self.assertIn("projects = optional(list(string), [])", variable)
        self.assertIn("nullable = false", variable)
        self.assertIn("default  = {}", variable)

    def test_the_module_gets_the_variable(self):
        module = re.search(r'module "kube_agents_iam" \{(.*?)\n\}', self.main_tf, re.DOTALL).group(1)
        self.assertIn("scope              = var.scope", module)

    def test_the_chart_gets_the_same_object_with_the_crds_keys(self):
        values = re.search(r"\n      scope = \{\n(?P<body>.*?)\n      \}\n", self.main_tf, re.DOTALL)
        self.assertIsNotNone(values, "platformAgent.scope is not in the helm values")
        body = values.group("body")
        self.assertIn("projects = var.scope.projects", body)
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
        self.assertIn("value       = module.kube_agents_iam.scope_projects", outputs)
        self.assertIn("value       = module.kube_agents_iam.scope_roles", outputs)


if __name__ == "__main__":
    unittest.main()
