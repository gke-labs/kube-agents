"""The chart forwards `platformAgent.harness.experimental.shellSandbox.resources` to the CR.

The value is the chart's route to `spec.harness.experimental.shellSandbox.resources`, the
PlatformAgent field that sizes the shell container. Four things have to hold: a default
render writes no block at all (an empty one would be pruned by an API server serving an
older CRD and read as an override by everyone else); a set value reaches the CR under the
path the operator reads, beside the sandbox's other fields; the schema, which is closed
under `shellSandbox`, declares the key; the shape check the operator's refusals need fails
the render naming the key; and the quota preflight counts the override as the pod the
operator will write.

Run: python3 -m unittest discover -s tests -p 'test_chart_shell_sandbox_resources.py' -v
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_SCHEMA = _CHART / "values.schema.json"
_VALUES = _CHART / "values.yaml"
_FOOTPRINT = _CHART / "files" / "footprint.yaml"
_CR_TEMPLATE = "templates/platform-agent-cr.yaml"
_HELM = shutil.which("helm")
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_VALUE_PATH = "platformAgent.harness.experimental.shellSandbox.resources"
_CR_PATH = ("spec", "harness", "experimental", "shellSandbox", "resources")
_SANDBOX_VALUES_PATH = ("platformAgent", "harness", "experimental", "shellSandbox")
_GIB = 1024**3
_MIB = 1024**2
_PROBE_TEMPLATE = """
{{- if .Values.probe.emitRequirements }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: probe-requirements
data:
  requirements: |
    {{ include "kube-agents.quotaRequirements" . }}
{{- end }}
"""


def _sandbox_values(resources):
    return {"platformAgent": {"harness": {"experimental": {"shellSandbox": {"resources": resources}}}}}


def _cr_sandbox(cr):
    return cr["spec"]["harness"]["experimental"]["shellSandbox"]


def _schema_node(path):
    node = json.loads(_SCHEMA.read_text())
    for key in path:
        node = node["properties"][key]
    return node


class SchemaDeclaresTheKeyTest(unittest.TestCase):
    """Readable without helm: the value is declared, and closed below `shellSandbox`."""

    def test_values_yaml_carries_an_empty_default(self):
        values = yaml.safe_load(_VALUES.read_text())
        for key in _SANDBOX_VALUES_PATH:
            values = values[key]
        self.assertEqual(values["resources"], {})

    def test_shell_sandbox_is_closed_and_resources_is_an_open_object(self):
        node = _schema_node(_SANDBOX_VALUES_PATH)
        self.assertIs(node["additionalProperties"], False)
        self.assertEqual(node["properties"]["resources"], {"type": "object"})

    def test_the_crd_guard_is_called_on_the_field(self):
        # `lookup` is empty under `helm template`, so the guard against a CRD that
        # predates the field cannot fire here; the call is pinned, and the shared
        # kube-agents.crdFieldGuard body is pinned by test_chart_credential_proxy_resources.
        text = (_CHART / _CR_TEMPLATE).read_text()
        self.assertIn(
            '{{- include "kube-agents.crdFieldGuard" (dict "value" "' + _VALUE_PATH + '" '
            '"path" (list "harness" "experimental" "shellSandbox") "field" "resources") }}', text)


@unittest.skipUnless(_HELM, "helm is not installed")
class ShellSandboxResourcesRenderTest(unittest.TestCase):
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

    def test_default_render_writes_no_experimental_block(self):
        cr = self._render_cr()
        self.assertNotIn("experimental", cr["spec"]["harness"])

    def test_a_nulled_leaf_alone_writes_no_block(self):
        cr = self._render_cr(values=_sandbox_values({"limits": {"memory": None}}))
        self.assertNotIn("experimental", cr["spec"]["harness"])

    def test_an_empty_string_leaf_alone_writes_no_block(self):
        cr = self._render_cr([f"{_VALUE_PATH}.limits.memory="])
        self.assertNotIn("experimental", cr["spec"]["harness"])

    def test_the_memory_pair_reaches_the_cr_as_written_and_nothing_else(self):
        cr = self._render_cr([f"{_VALUE_PATH}.requests.memory=1Gi", f"{_VALUE_PATH}.limits.memory=3Gi"])
        # Only the keys set: the operator merges them over its defaults, so the chart
        # must not pad the block with defaults of its own, nor with the sandbox's other
        # fields when they are unset.
        self.assertEqual(_cr_sandbox(cr), {"resources": {"requests": {"memory": "1Gi"}, "limits": {"memory": "3Gi"}}})

    def test_a_float_cpu_from_a_values_file_reaches_the_cr_as_a_string(self):
        # The CRD types a quantity as int-or-string; a bare 1.5 is refused by the API server.
        cr = self._render_cr(values=_sandbox_values({"limits": {"cpu": 1.5}}))
        self.assertEqual(_cr_sandbox(cr)["resources"], {"limits": {"cpu": "1.5"}})

    def test_the_override_sits_beside_the_sandbox_image(self):
        cr = self._render_cr([
            "platformAgent.harness.experimental.shellSandbox.image=example.invalid/sandbox:v1",
            f"{_VALUE_PATH}.limits.cpu=4",
        ])
        self.assertEqual(_cr_sandbox(cr), {"image": "example.invalid/sandbox:v1", "resources": {"limits": {"cpu": "4"}}})

    def test_the_sandbox_image_alone_still_renders_without_a_resources_block(self):
        cr = self._render_cr(["platformAgent.harness.experimental.shellSandbox.image=example.invalid/sandbox:v1"])
        self.assertEqual(_cr_sandbox(cr), {"image": "example.invalid/sandbox:v1"})

    def test_the_schema_refuses_an_unknown_key_under_shell_sandbox(self):
        res = self._render_cr(["platformAgent.harness.experimental.shellSandbox.replicas=2"], expect_failure=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("shellSandbox", res.stderr)
        self.assertIn("replicas", res.stderr)


@unittest.skipUnless(_HELM, "helm is not installed")
class ShellSandboxResourcesRefusedAtRenderTest(unittest.TestCase):
    """The shape the operator refuses on sight, the render refuses first, naming the key."""

    _render_cr = ShellSandboxResourcesRenderTest._render_cr

    def _render_error(self, sets=(), values=None):
        res = self._render_cr(sets, expect_failure=True, values=values)
        self.assertNotEqual(res.returncode, 0, res.stdout)
        return res.stderr

    def test_a_misspelt_key_fails_naming_it_and_the_accepted_ones(self):
        err = self._render_error([f"{_VALUE_PATH}.limit.memory=2Gi"])
        self.assertIn(f"{_VALUE_PATH} carries limit", err)
        self.assertIn("the accepted keys are requests, limits and claims", err)

    def test_claims_fail_naming_the_sandbox_pod(self):
        err = self._render_error([f"{_VALUE_PATH}.claims[0].name=gpu"])
        self.assertIn(f"{_VALUE_PATH}.claims is not supported -- the shell sandbox pod declares no resourceClaims", err)

    def test_a_resource_name_the_container_does_not_declare_fails_naming_the_shell(self):
        err = self._render_error([f"{_VALUE_PATH}.limits.hugepages-2Mi=1Gi"])
        self.assertIn(f"{_VALUE_PATH}.limits.hugepages-2Mi: the shell container declares cpu, memory and ephemeral-storage only", err)

    def test_a_negative_quantity_fails(self):
        err = self._render_error([f"{_VALUE_PATH}.requests.memory=-1Gi"])
        self.assertIn(f"{_VALUE_PATH}.requests.memory is -1Gi; a quantity must not be negative", err)

    def test_an_unparseable_quantity_fails(self):
        err = self._render_error([f"{_VALUE_PATH}.limits.memory=2GB"])
        self.assertIn(f'{_VALUE_PATH}.limits.memory is "2GB", which is not a Kubernetes quantity', err)


@unittest.skipUnless(_HELM, "helm is not installed")
class ShellSandboxFootprintTest(unittest.TestCase):
    """The quota preflight counts the override as the pod the operator will write."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.chart = pathlib.Path(cls._tmp.name) / "kube-agents"
        shutil.copytree(_CHART, cls.chart)
        # The probe adds a value the shipped schema does not declare, and the schema is
        # closed. Dropping it from the throwaway copy keeps the probe out of the real chart.
        (cls.chart / "values.schema.json").unlink(missing_ok=True)
        (cls.chart / "templates" / "zz-probe.yaml").write_text(_PROBE_TEMPLATE)
        cls.footprint = yaml.safe_load(_FOOTPRINT.read_text())["operatorRendered"]["shellSandbox"]

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _requirements(self, sets=(), values=None):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as fh:
            yaml.safe_dump({"probe": {"emitRequirements": True}, **(values or {})}, fh)
            fh.flush()
            args = [_HELM, "template", "r", str(self.chart), "-f", fh.name, *_REQUIRED]
            for item in sets:
                args += ["--set", item]
            res = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        for doc in yaml.safe_load_all(res.stdout):
            if doc and doc.get("metadata", {}).get("name") == "probe-requirements":
                return json.loads(doc["data"]["requirements"])
        self.fail(f"probe ConfigMap not found in output:\n{res.stdout}")

    def test_the_footprint_carries_the_operators_defaults(self):
        # The operator's shellSandbox* constants, which the footprint is summed from.
        self.assertEqual(self.footprint["cpuMillisRequest"], 200)
        self.assertEqual(self.footprint["memoryBytesRequest"], 512 * _MIB)
        self.assertEqual(self.footprint["cpuMillisLimit"], 2000)
        self.assertEqual(self.footprint["memoryBytesLimit"], 2 * _GIB)
        self.assertEqual(self.footprint["ephemeralStorageBytesRequest"], 0)
        self.assertEqual(self.footprint["ephemeralStorageBytesLimit"], 0)

    def test_the_memory_pair_moves_the_memory_totals_and_nothing_else(self):
        base = self._requirements()
        raised = self._requirements([f"{_VALUE_PATH}.requests.memory=1Gi", f"{_VALUE_PATH}.limits.memory=3Gi"])
        self.assertEqual(raised["requestsMemory"] - base["requestsMemory"], 1 * _GIB - self.footprint["memoryBytesRequest"])
        self.assertEqual(raised["limitsMemory"] - base["limitsMemory"], 3 * _GIB - self.footprint["memoryBytesLimit"])
        for key in ("pods", "requestsCpu", "limitsCpu", "requestsEphemeral", "limitsEphemeral", "persistentVolumeClaims"):
            self.assertEqual(raised[key], base[key], key)

    def test_a_cpu_limit_alone_moves_only_the_cpu_limit_total(self):
        base = self._requirements()
        raised = self._requirements([f"{_VALUE_PATH}.limits.cpu=4"])
        self.assertEqual(raised["limitsCpu"] - base["limitsCpu"], 4000 - self.footprint["cpuMillisLimit"])
        for key in ("pods", "requestsCpu", "requestsMemory", "limitsMemory", "requestsEphemeral", "limitsEphemeral"):
            self.assertEqual(raised[key], base[key], key)

    def test_an_ephemeral_limit_alone_is_counted_as_a_request_too(self):
        # The shell declares no ephemeral-storage, so the API server defaults an absent
        # request to the limit; counting 0 would pass a quota the pod is then refused on.
        base = self._requirements()
        raised = self._requirements([f"{_VALUE_PATH}.limits.ephemeral-storage=10Gi"])
        self.assertEqual(raised["limitsEphemeral"] - base["limitsEphemeral"], 10 * _GIB)
        self.assertEqual(raised["requestsEphemeral"] - base["requestsEphemeral"], 10 * _GIB)
        explicit = self._requirements([f"{_VALUE_PATH}.limits.ephemeral-storage=10Gi", f"{_VALUE_PATH}.requests.ephemeral-storage=4Gi"])
        self.assertEqual(explicit["requestsEphemeral"] - base["requestsEphemeral"], 4 * _GIB)

    def test_a_null_override_key_keeps_the_default(self):
        base = self._requirements()
        nulled = self._requirements([f"{_VALUE_PATH}.limits.memory=null"])
        self.assertEqual(nulled, base)

    def test_a_bad_quantity_fails_the_preflight_naming_the_key(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as fh:
            yaml.safe_dump({"probe": {"emitRequirements": True}}, fh)
            fh.flush()
            res = subprocess.run(
                [_HELM, "template", "r", str(self.chart), "-f", fh.name, *_REQUIRED,
                 "--set", f"{_VALUE_PATH}.limits.memory=2GB"],
                capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn(f"{_VALUE_PATH}.limits.memory", res.stderr)


if __name__ == "__main__":
    unittest.main()
