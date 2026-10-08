"""The chart forwards `platformAgent.deployment.credentialProxy.resources` to the CR.

The value is the chart's route to `spec.deployment.credentialProxy.resources`, the
PlatformAgent field that sizes the credential-proxy container. Four things have to hold:
a default render writes no block at all (an empty one would be pruned by an API server
serving an older CRD and read as an override by everyone else); a set value reaches the
CR under the path the operator reads; the schema, which is closed under
`platformAgent.deployment`, declares the key and refuses one it does not know beneath it;
and an override the operator would refuse fails the render instead of installing Degraded.

The quota preflight's arithmetic over the same value is in test_quota_preflight.py.

Run: python3 -m unittest discover -s tests -p 'test_chart_credential_proxy_resources.py' -v
"""

import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_SCHEMA = _CHART / "values.schema.json"
_VALUES = _CHART / "values.yaml"
_CR_TEMPLATE = "templates/platform-agent-cr.yaml"
_HELM = shutil.which("helm")
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_VALUE_PATH = "platformAgent.deployment.credentialProxy.resources"
_CR_PATH = ("spec", "deployment", "credentialProxy", "resources")


def _proxy_values(resources):
    return {"platformAgent": {"deployment": {"credentialProxy": {"resources": resources}}}}


def _cr_resources(cr):
    node = cr
    for key in _CR_PATH:
        node = node[key]
    return node


def _schema_node(path):
    node = json.loads(_SCHEMA.read_text())
    for key in path:
        node = node["properties"][key]
    return node


class SchemaDeclaresTheKeyTest(unittest.TestCase):
    """Readable without helm: the value is declared, and closed below `credentialProxy`."""

    def test_values_yaml_carries_an_empty_default(self):
        values = yaml.safe_load(_VALUES.read_text())
        self.assertEqual(values["platformAgent"]["deployment"]["credentialProxy"], {"resources": {}})

    def test_credential_proxy_is_closed_and_resources_is_an_open_object(self):
        node = _schema_node(("platformAgent", "deployment", "credentialProxy"))
        self.assertIs(node["additionalProperties"], False)
        self.assertEqual(node["properties"]["resources"], {"type": "object"})


@unittest.skipUnless(_HELM, "helm is not installed")
class CredentialProxyResourcesRenderTest(unittest.TestCase):
    def _render_cr(self, sets=(), expect_failure=False, values=None):
        args = [_HELM, "template", "r", str(_CHART), "-s", _CR_TEMPLATE, *_REQUIRED]
        for item in sets:
            args += ["--set", item]
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as fh:
            # A values file, unlike `--set`, hands the chart YAML floats and nulls.
            yaml.safe_dump(values or {}, fh)
            fh.flush()
            res = subprocess.run([*args, "-f", fh.name], capture_output=True, text=True)
        if expect_failure:
            return res
        self.assertEqual(res.returncode, 0, res.stderr)
        docs = [d for d in yaml.safe_load_all(res.stdout) if d and d.get("kind") == "PlatformAgent"]
        self.assertEqual(len(docs), 1, res.stdout)
        return docs[0]

    def test_default_render_writes_no_credential_proxy_block(self):
        cr = self._render_cr()
        self.assertNotIn("credentialProxy", cr["spec"]["deployment"])

    def test_a_null_resources_value_writes_no_block_either(self):
        # `--set ...resources=null` is the documented Helm way to drop a key; the chart
        # has to read it as "nothing set", not render `resources: null` onto the CR.
        cr = self._render_cr([f"{_VALUE_PATH}=null"])
        self.assertNotIn("credentialProxy", cr["spec"]["deployment"])

    def test_a_nulled_leaf_alone_writes_no_block(self):
        # A non-empty map whose only leaf is null holds nothing to override.
        for values in (_proxy_values({"limits": {"memory": None}}),
                       _proxy_values({"limits": {"memory": None}, "requests": {"cpu": None}})):
            cr = self._render_cr(values=values)
            self.assertNotIn("credentialProxy", cr["spec"]["deployment"], values)

    def test_a_nulled_leaf_beside_a_set_one_is_dropped(self):
        cr = self._render_cr(values=_proxy_values({"limits": {"memory": None, "cpu": "2"}}))
        self.assertEqual(_cr_resources(cr), {"limits": {"cpu": "2"}})

    def test_an_empty_string_leaf_alone_writes_no_block(self):
        # The chart reads "" as unset, as kube-agents.compactFields does; rendered,
        # `memory: ""` is refused by the CRD's quantity pattern.
        for values in (_proxy_values({"limits": {"memory": ""}}),
                       _proxy_values({"limits": {"memory": ""}, "requests": {"cpu": ""}})):
            cr = self._render_cr(values=values)
            self.assertNotIn("credentialProxy", cr["spec"]["deployment"], values)
        cr = self._render_cr([f"{_VALUE_PATH}.limits.memory="])
        self.assertNotIn("credentialProxy", cr["spec"]["deployment"])

    def test_an_empty_string_leaf_beside_a_set_one_is_dropped(self):
        cr = self._render_cr(values=_proxy_values({"limits": {"memory": "", "cpu": 1}}))
        self.assertEqual(_cr_resources(cr), {"limits": {"cpu": "1"}})

    def test_a_float_cpu_from_a_values_file_reaches_the_cr_as_a_string(self):
        # The CRD types a quantity as int-or-string; a bare 1.5 is refused by the API server.
        cr = self._render_cr(values=_proxy_values({"limits": {"cpu": 1.5, "memory": "2Gi"}}))
        self.assertEqual(_cr_resources(cr), {"limits": {"cpu": "1.5", "memory": "2Gi"}})

    def test_a_memory_limit_alone_reaches_the_cr_as_written(self):
        cr = self._render_cr([f"{_VALUE_PATH}.limits.memory=2Gi"])
        node = cr
        for key in _CR_PATH:
            node = node[key]
        # Only the key set: the operator merges it over its defaults, so the chart
        # must not pad the block with defaults of its own.
        self.assertEqual(node, {"limits": {"memory": "2Gi"}})

    def test_requests_and_limits_both_reach_the_cr(self):
        cr = self._render_cr([
            f"{_VALUE_PATH}.requests.memory=1Gi",
            f"{_VALUE_PATH}.limits.memory=4Gi",
            f"{_VALUE_PATH}.limits.cpu=2",
        ])
        # `--set ...cpu=2` is an integer; the CR carries it quoted, which the CRD accepts.
        self.assertEqual(_cr_resources(cr), {"requests": {"memory": "1Gi"}, "limits": {"memory": "4Gi", "cpu": "2"}})

    def test_the_schema_refuses_an_unknown_key_under_credential_proxy(self):
        res = self._render_cr(["platformAgent.deployment.credentialProxy.replicas=2"], expect_failure=True)
        self.assertNotEqual(res.returncode, 0)
        # Helm 3 prints `platformAgent.deployment.credentialProxy: Additional property
        # replicas is not allowed`; Helm 4 prints the path as a JSON pointer and the
        # phrase in lower case. Both name the parent and the key.
        self.assertIn("credentialProxy", res.stderr)
        self.assertIn("replicas", res.stderr)


@unittest.skipUnless(_HELM, "helm is not installed")
class CredentialProxyResourcesRefusedAtRenderTest(unittest.TestCase):
    """What the operator refuses, the render refuses first: rendered, the release would
    install cleanly and the proxy would sit Degraded at its defaults with no Helm error."""

    _render_cr = CredentialProxyResourcesRenderTest._render_cr

    def _render_error(self, sets=(), values=None):
        res = self._render_cr(sets, expect_failure=True, values=values)
        self.assertNotEqual(res.returncode, 0, res.stdout)
        return res.stderr

    def test_a_misspelt_key_fails_naming_it_and_the_accepted_ones(self):
        # The CRD's resources object declares claims, limits and requests only, so
        # `limit:` would be pruned by the API server and the override lost.
        err = self._render_error([f"{_VALUE_PATH}.limit.memory=2Gi"])
        self.assertIn(f"{_VALUE_PATH} carries limit", err)
        self.assertIn("the accepted keys are requests, limits and claims", err)

    def test_a_scalar_or_list_side_fails_naming_the_key(self):
        # range cannot walk a string, and a list hands it integer names.
        err = self._render_error([f"{_VALUE_PATH}.limits=2Gi"])
        self.assertIn(f"{_VALUE_PATH}.limits is 2Gi, which is not a map of resource name to quantity", err)
        err = self._render_error(values=_proxy_values({"requests": ["2Gi"]}))
        self.assertIn(f"{_VALUE_PATH}.requests is [2Gi], which is not a map of resource name to quantity", err)

    def test_claims_fail(self):
        err = self._render_error([f"{_VALUE_PATH}.claims[0].name=gpu"])
        self.assertIn(f"{_VALUE_PATH}.claims is not supported", err)

    def test_a_resource_name_the_container_does_not_declare_fails(self):
        err = self._render_error([f"{_VALUE_PATH}.limits.hugepages-2Mi=1Gi"])
        self.assertIn(f"{_VALUE_PATH}.limits.hugepages-2Mi", err)

    def test_a_memory_limit_under_the_floor_fails_naming_it(self):
        err = self._render_error([f"{_VALUE_PATH}.limits.memory=512Mi"])
        self.assertIn(f"{_VALUE_PATH}.limits.memory is 512Mi, under the 672Mi floor", err)

    def test_a_memory_limit_a_fraction_under_the_floor_fails(self):
        # parseBytes ceils a milli-, micro- or nano-byte figure, which would round half a
        # byte under the 704,643,072-byte floor up onto it; the operator compares exactly.
        for value in ("704643071500m", "704643071500000u", "704643071500000000n"):
            err = self._render_error([f"{_VALUE_PATH}.limits.memory={value}"])
            self.assertIn(f"{_VALUE_PATH}.limits.memory is {value}, under the 672Mi floor", err)

    def test_a_memory_limit_exactly_at_the_floor_in_millibytes_renders(self):
        cr = self._render_cr([f"{_VALUE_PATH}.limits.memory=704643072000m"])
        self.assertEqual(_cr_resources(cr)["limits"]["memory"], "704643072000m")

    def test_a_request_above_the_default_limit_fails_naming_the_pair(self):
        err = self._render_error([f"{_VALUE_PATH}.requests.memory=2Gi"])
        self.assertIn(f"{_VALUE_PATH}.requests.memory (2Gi) exceeds the operator's default limits.memory (1Gi)", err)
        self.assertIn("set limits.memory as well", err)

    def test_a_limit_below_the_default_request_fails_naming_the_pair(self):
        err = self._render_error([f"{_VALUE_PATH}.limits.cpu=200m"])
        self.assertIn(f"{_VALUE_PATH}.limits.cpu (200m) is below the operator's default requests.cpu (500m)", err)

    def test_a_request_above_the_limit_set_beside_it_fails(self):
        err = self._render_error([f"{_VALUE_PATH}.requests.memory=4Gi", f"{_VALUE_PATH}.limits.memory=2Gi"])
        self.assertIn(f"{_VALUE_PATH}.requests.memory (4Gi) exceeds limits.memory (2Gi)", err)

    def test_a_crossed_pair_is_compared_exactly(self):
        # The operator compares exact quantities; parseCpuMillis and parseBytes truncate
        # or ceil, which would admit these or (499.5m) compare them the wrong way.
        # quantityExact scales in Sprig's decimal arithmetic, so a strict comparison
        # separates two 15-digit values one unit apart (the second case).
        cases = (
            ([f"{_VALUE_PATH}.requests.cpu=1.0004"], "requests.cpu (1.0004) exceeds the operator's default limits.cpu (1)"),
            ([f"{_VALUE_PATH}.requests.cpu=100000000000001m", f"{_VALUE_PATH}.limits.cpu=100000000000000m"],
             "requests.cpu (100000000000001m) exceeds limits.cpu (100000000000000m)"),
            ([f"{_VALUE_PATH}.requests.memory=1073741824.5"],
             "requests.memory (1073741824.5) exceeds the operator's default limits.memory (1Gi)"),
            ([f"{_VALUE_PATH}.limits.cpu=499.5m"], "limits.cpu (499.5m) is below the operator's default requests.cpu (500m)"),
            ([f"{_VALUE_PATH}.requests.cpu=1999u", f"{_VALUE_PATH}.limits.cpu=1001u"],
             "requests.cpu (1999u) exceeds limits.cpu (1001u)"),
        )
        for sets, want in cases:
            err = self._render_error(sets)
            self.assertIn(f"{_VALUE_PATH}.{want}", err, sets)

    def test_an_equal_pair_scaled_differently_renders(self):
        # A regression pin on Sprig's decimal mulf and divf: in float64 arithmetic
        # 1.005 × 1000 is 1004.9999999999999, which a strict comparison would refuse
        # against 1005m; 1.005G against 1005M likewise. The operator admits both pairs.
        cases = (
            {"requests": {"cpu": "1005m"}, "limits": {"cpu": "1.005"}},
            {"requests": {"memory": "1005M"}, "limits": {"memory": "1.005G"}},
        )
        for resources in cases:
            cr = self._render_cr(values=_proxy_values(resources))
            self.assertEqual(_cr_resources(cr), resources)

    def test_a_request_equal_to_the_default_limit_renders(self):
        for value in ("1", "1000m"):
            cr = self._render_cr([f"{_VALUE_PATH}.requests.cpu={value}"])
            self.assertEqual(_cr_resources(cr), {"requests": {"cpu": value}})
        cr = self._render_cr([f"{_VALUE_PATH}.requests.memory=1Gi"])
        self.assertEqual(_cr_resources(cr), {"requests": {"memory": "1Gi"}})

    def test_an_ephemeral_storage_request_above_the_default_limit_fails(self):
        err = self._render_error([f"{_VALUE_PATH}.requests.ephemeral-storage=3Gi"])
        self.assertIn("limits.ephemeral-storage (2Gi)", err)

    def test_a_negative_quantity_fails(self):
        err = self._render_error(values=_proxy_values({"requests": {"cpu": "-1"}}))
        self.assertIn(f"{_VALUE_PATH}.requests.cpu is -1", err)

    def test_a_zero_limit_fails(self):
        err = self._render_error([f"{_VALUE_PATH}.limits.cpu=0"])
        self.assertIn(f"{_VALUE_PATH}.limits.cpu is 0", err)

    def test_a_byte_count_past_int64_fails_naming_the_key(self):
        # parseBytes saturates at math.MaxInt64 for the quota sums; this check has to
        # see the overflow, or 10E would render and the operator refuse it.
        for value in ("10E", "8Ei"):
            err = self._render_error([f"{_VALUE_PATH}.limits.memory={value}"])
            self.assertIn(f"{_VALUE_PATH}.limits.memory is {value}, which is not a representable byte count", err)
        err = self._render_error([f"{_VALUE_PATH}.limits.ephemeral-storage=8Ei"])
        self.assertIn(f"{_VALUE_PATH}.limits.ephemeral-storage is 8Ei, which is not a representable byte count", err)
        # Past float64's range Sprig's float64 answers 0; read that way 1e400 would render,
        # or draw the zero-limit refusal, while the operator refuses it as unrepresentable.
        for key in ("requests.memory", "requests.cpu", "limits.memory"):
            err = self._render_error([f"{_VALUE_PATH}.{key}=1e400"])
            self.assertIn(f"{_VALUE_PATH}.{key} is 1e400, which is not a representable", err)
            self.assertNotIn("a limit of zero", err)

    def test_a_cpu_whose_millicores_overflow_fails_naming_the_key(self):
        # 1e308 is a finite float64, but in millicores it is +Inf, which toJson writes
        # as "" and the zero-limit check read as 0. 10E fits a float64 in millicores and
        # not an int64, where the operator's MilliValue wraps it.
        for key, value in (("requests.cpu", "1e308"), ("limits.cpu", "1e308"), ("requests.cpu", "10E")):
            err = self._render_error([f"{_VALUE_PATH}.{key}={value}"])
            self.assertIn(f"{_VALUE_PATH}.{key} is {value}, which is not a CPU count the scheduler can represent in millicores", err)
            self.assertNotIn("a limit of zero", err)

    def test_a_cpu_just_under_the_millicore_bound_renders(self):
        cr = self._render_cr([f"{_VALUE_PATH}.limits.cpu=9.22337203685477e15"])
        self.assertEqual(_cr_resources(cr), {"limits": {"cpu": "9.22337203685477e15"}})

    def test_a_suffixed_cpu_past_float64_fails_naming_the_key(self):
        # The number before the suffix is finite, so quantityOverflowsFloat64 passes it;
        # the suffix carries it past float64's range, and quantityExact refuses it.
        value = "1" + "0" * 306 + "k"
        err = self._render_error([f"{_VALUE_PATH}.requests.cpu={value}"])
        self.assertIn(f"{_VALUE_PATH}.requests.cpu is {value}, which is not a representable quantity", err)
        self.assertNotIn("a limit of zero", err)

    def test_more_than_fifteen_significant_digits_fail_naming_the_key(self):
        # float64 holds 15 significant digits exactly; with a 16th, each of these reads
        # as its bound (1Gi, the floor, 2^53) and would render while the operator,
        # comparing exactly, refuses it.
        cases = (
            ([f"{_VALUE_PATH}.requests.memory=1073741824.00000001"], "requests.memory", "1073741824.00000001"),
            ([f"{_VALUE_PATH}.limits.memory=704643071.99999999"], "limits.memory", "704643071.99999999"),
            ([f"{_VALUE_PATH}.requests.memory=9007199254740993", f"{_VALUE_PATH}.limits.memory=9007199254740992"],
             "limits.memory", "9007199254740992"),
            ([f"{_VALUE_PATH}.requests.memory=9007199254740993", f"{_VALUE_PATH}.limits.memory=16Pi"],
             "requests.memory", "9007199254740993"),
        )
        for sets, key, value in cases:
            err = self._render_error(sets)
            self.assertIn(f"{_VALUE_PATH}.{key} is {value}, which has more than 15 significant digits", err, sets)
            self.assertIn("larger unit or fewer digits", err)

    def test_fifteen_or_fewer_significant_digits_render(self):
        cr = self._render_cr([f"{_VALUE_PATH}.requests.memory=1073741824"])
        self.assertEqual(_cr_resources(cr), {"requests": {"memory": "1073741824"}})
        cr = self._render_cr([f"{_VALUE_PATH}.limits.memory=1.5Gi"])
        self.assertEqual(_cr_resources(cr), {"limits": {"memory": "1.5Gi"}})

    def test_an_integer_past_int64_from_a_values_file_fails_naming_the_key(self):
        # Helm reads an integer above 2^63 as a float64, written 1e+19.
        err = self._render_error(values=_proxy_values({"limits": {"memory": 10**19}}))
        self.assertIn(f"{_VALUE_PATH}.limits.memory is 1e+19, which is not a representable byte count", err)

    def test_a_byte_count_under_int64_renders(self):
        # 7Ei is under 2^63, so the operator reads it; as a limit it passes every check.
        cr = self._render_cr([f"{_VALUE_PATH}.limits.memory=7Ei"])
        self.assertEqual(_cr_resources(cr), {"limits": {"memory": "7Ei"}})

    def test_a_two_gi_limit_renders(self):
        cr = self._render_cr([f"{_VALUE_PATH}.limits.memory=2Gi"])
        self.assertEqual(_cr_resources(cr), {"limits": {"memory": "2Gi"}})

    def test_a_limit_at_the_floor_renders(self):
        cr = self._render_cr([f"{_VALUE_PATH}.limits.memory=672Mi"])
        self.assertEqual(_cr_resources(cr), {"limits": {"memory": "672Mi"}})

    # The CRD's grammar admits `.5Gi`, `1.Gi` and `500n`, and resource.ParseQuantity
    # reads them, so the render has to read them too: a form it dropped instead would
    # skip the floor and pair checks and install Degraded.
    def test_a_leading_dot_quantity_is_read_and_refused_by_the_floor(self):
        err = self._render_error([f"{_VALUE_PATH}.limits.memory=.5Gi"])
        self.assertIn(f"{_VALUE_PATH}.limits.memory is .5Gi, under the 672Mi floor", err)

    def test_a_trailing_dot_quantity_renders(self):
        cr = self._render_cr(values=_proxy_values({"limits": {"memory": "1.Gi"}}))
        self.assertEqual(_cr_resources(cr), {"limits": {"memory": "1.Gi"}})

    def test_a_nano_cpu_request_renders_as_the_crd_and_operator_read_it(self):
        # 500n is a positive quantity under the default 1-core limit: the CRD admits
        # it and the operator refuses nothing about it, so the render passes it on.
        cr = self._render_cr(values=_proxy_values({"requests": {"cpu": "500n"}}))
        self.assertEqual(_cr_resources(cr), {"requests": {"cpu": "500n"}})

    def test_a_quantity_outside_the_grammar_fails_naming_the_key(self):
        for value in ("abc", "1e.5", "2GB"):
            err = self._render_error(values=_proxy_values({"limits": {"memory": value}}))
            self.assertIn(f'{_VALUE_PATH}.limits.memory is "{value}", which is not a Kubernetes quantity', err)

    def test_a_padded_quantity_renders_trimmed(self):
        # The checks read the trimmed value; the CRD's anchored pattern refuses the padding.
        cr = self._render_cr(values=_proxy_values({"limits": {"memory": " 2Gi"}}))
        self.assertEqual(_cr_resources(cr), {"limits": {"memory": "2Gi"}})


class CrdGuardTest(unittest.TestCase):
    """`helm upgrade` does not apply crds/, so an override against a CRD from before
    the field would be pruned silently. `lookup` is empty under `helm template`, so
    the guard can only be pinned as text here; the render tests above show the block
    still renders when the lookup returns nothing. The failing branch is exercised
    only against a live install."""

    _LOOKUP = 'lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "platformagents.kubeagents.x-k8s.io"'

    def setUp(self):
        template = (_CHART / _CR_TEMPLATE).read_text()
        block = re.search(r"\n(\s*\{\{- if \$proxyQuantities \}\}\n\s*\{\{- \$crd := .*?)\n\s*credentialProxy:\n", template, re.DOTALL)
        self.assertIsNotNone(block, "the credentialProxy block is missing from the CR template")
        self.guard = block.group(1)

    def test_a_set_override_looks_up_the_installed_crd(self):
        gate = re.search(r"\{\{- if \$proxyQuantities \}\}\n\s*\{\{- \$crd := " + re.escape(self._LOOKUP), self.guard)
        self.assertIsNotNone(gate, "the CRD lookup is not gated on a set override")

    def test_the_guard_reads_the_storage_versions_deployment_properties(self):
        self.assertIn("{{- if .storage }}", self.guard)
        self.assertIn('dig "schema" "openAPIV3Schema" "properties" "spec" "properties" "deployment" "properties" (dict) .', self.guard)

    def test_the_guard_fails_naming_the_field_and_the_remedy(self):
        fail = re.search(r'\{\{- if not \(hasKey \$deploymentProps "credentialProxy"\) \}\}\n\s*\{\{- fail "([^"]*)" \}\}', self.guard)
        self.assertIsNotNone(fail, "the CRD guard does not fail on a missing key")
        for want in ("predates spec.deployment.credentialProxy", "helm upgrade does not update CRDs",
                     "charts/kube-agents/crds/", "upgrade.sh", "prunes the value silently", "release record keeps it"):
            self.assertIn(want, fail.group(1))


if __name__ == "__main__":
    unittest.main()
