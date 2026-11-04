"""operator.resources.limits.memory has a floor, and the render refuses what is under it.

Releases up to 0.16.0 shipped the Kubebuilder scaffold's 128Mi limit for the manager, and
`helm upgrade --reuse-values` renders a new chart against the old release's values, chart
defaults included, so an install upgraded that way kept a limit the manager's cache outgrows.
The chart now fails that render naming the key (kube-agents.operatorResourcesCheck in
templates/_helpers.tpl). These tests pin the floor to the default in values.yaml, the message,
and the inputs the check must leave alone.

Run: python3 -m unittest discover -s tests -p 'test_chart_operator_resources.py' -v
"""

import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_VALUES = _CHART / "values.yaml"
_HELPERS = _CHART / "templates" / "_helpers.tpl"
_OPERATOR_TEMPLATE = "templates/operator-deployment.yaml"
_HELM = shutil.which("helm")
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_VALUE_PATH = "operator.resources"
_FLOOR_HELPER = re.compile(
    r'define "kube-agents\.operatorMemoryFloorBytes" -\}\}\s*(\d+)\s*\{\{- end', re.S
)
_MANAGER_CONTAINER = "manager"
_MIB = 1024**2
# What releases up to 0.16.0 shipped for the manager, the pair --reuse-values carries forward.
_LEGACY_LIMIT = "128Mi"
_LEGACY_REQUEST = "64Mi"


def _floor_bytes():
    match = _FLOOR_HELPER.search(_HELPERS.read_text())
    assert match, f"kube-agents.operatorMemoryFloorBytes is not defined in {_HELPERS}"
    return int(match.group(1))


def _default_limit():
    return yaml.safe_load(_VALUES.read_text())["operator"]["resources"]["limits"]["memory"]


class FloorIsTheDefaultTest(unittest.TestCase):
    """Readable without helm: the floor is the chart's own default, so a stock install
    sits on it and the knob can only be raised."""

    def test_the_floor_equals_the_values_default(self):
        default = _default_limit()
        self.assertTrue(default.endswith("Mi"), default)
        self.assertEqual(_floor_bytes(), int(default[:-2]) * _MIB)

    def test_the_floor_is_above_the_legacy_limit(self):
        self.assertGreater(_floor_bytes(), int(_LEGACY_LIMIT[:-2]) * _MIB)


@unittest.skipUnless(_HELM, "helm is not installed")
class OperatorMemoryLimitRenderTest(unittest.TestCase):
    def _render(self, sets=(), expect_failure=False, values=None):
        args = [_HELM, "template", "r", str(_CHART), "-s", _OPERATOR_TEMPLATE, *_REQUIRED]
        for item in sets:
            args += ["--set", item]
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as fh:
            # A values file, unlike `--set`, hands the chart YAML floats and nulls.
            yaml.safe_dump(values or {}, fh)
            fh.flush()
            res = subprocess.run([*args, "-f", fh.name], capture_output=True, text=True)
        if expect_failure:
            self.assertNotEqual(res.returncode, 0, res.stdout)
            return res.stderr
        self.assertEqual(res.returncode, 0, res.stderr)
        docs = [d for d in yaml.safe_load_all(res.stdout) if d and d.get("kind") == "Deployment"]
        self.assertEqual(len(docs), 1, res.stdout)
        return docs[0]

    def _manager_resources(self, deployment):
        for container in deployment["spec"]["template"]["spec"]["containers"]:
            if container["name"] == _MANAGER_CONTAINER:
                return container["resources"]
        raise AssertionError(f"no {_MANAGER_CONTAINER} container in the rendered Deployment")

    def test_the_default_render_carries_the_floor(self):
        resources = self._manager_resources(self._render())
        self.assertEqual(resources["limits"]["memory"], _default_limit())

    def test_the_legacy_limit_fails_naming_the_key_and_the_way_out(self):
        err = self._render([f"{_VALUE_PATH}.limits.memory={_LEGACY_LIMIT}"], expect_failure=True)
        floor_mi = _floor_bytes() // _MIB
        self.assertIn(f"{_VALUE_PATH}.limits.memory is {_LEGACY_LIMIT}, under the {floor_mi}Mi floor", err)
        self.assertIn("--reuse-values", err)
        self.assertIn("--reset-then-reuse-values", err)

    def test_the_legacy_pair_fails_on_the_limit(self):
        # The whole scaffold block a --reuse-values upgrade from 0.16.0 or earlier carries.
        err = self._render(
            [
                f"{_VALUE_PATH}.limits.memory={_LEGACY_LIMIT}",
                f"{_VALUE_PATH}.requests.memory={_LEGACY_REQUEST}",
            ],
            expect_failure=True,
        )
        self.assertIn(f"{_VALUE_PATH}.limits.memory is {_LEGACY_LIMIT}, under the", err)

    def test_one_byte_under_the_floor_fails(self):
        value = f"{_floor_bytes() - 1}"
        err = self._render([f"{_VALUE_PATH}.limits.memory={value}"], expect_failure=True)
        self.assertIn(f"{_VALUE_PATH}.limits.memory is {value}, under the", err)

    def test_a_fraction_of_a_byte_under_the_floor_fails(self):
        # quantityExact compares without rounding; parseBytes would ceil this onto the floor.
        value = f"{_floor_bytes() * 1000 - 500}m"
        err = self._render([f"{_VALUE_PATH}.limits.memory={value}"], expect_failure=True)
        self.assertIn(f"{_VALUE_PATH}.limits.memory is {value}, under the", err)

    def test_exactly_the_floor_renders_in_every_spelling(self):
        floor = _floor_bytes()
        for value in (f"{floor}", f"{floor // _MIB}Mi", f"{floor * 1000}m", f"{floor / 1024**3}Gi"):
            with self.subTest(value=value):
                resources = self._manager_resources(self._render([f"{_VALUE_PATH}.limits.memory={value}"]))
                # `--set` hands a bare integer to the chart as a number, which renders unquoted.
                self.assertEqual(str(resources["limits"]["memory"]), value)

    def test_a_raised_limit_renders(self):
        resources = self._manager_resources(self._render([f"{_VALUE_PATH}.limits.memory=2Gi"]))
        self.assertEqual(resources["limits"]["memory"], "2Gi")

    def test_a_float_from_a_values_file_is_read_as_bytes(self):
        # 0.5 bytes, not 0.5 of anything larger: the check reads it and refuses it.
        err = self._render(
            values={"operator": {"resources": {"limits": {"memory": 0.5}}}}, expect_failure=True
        )
        self.assertIn(f"{_VALUE_PATH}.limits.memory is 0.5, under the", err)

    def test_an_absent_memory_limit_is_left_alone(self):
        # No limit is not a small limit; the floor says nothing about it.
        resources = self._manager_resources(self._render([f"{_VALUE_PATH}.limits.memory=null"]))
        self.assertNotIn("memory", resources.get("limits") or {})
        resources = self._manager_resources(self._render([f"{_VALUE_PATH}.limits=null"]))
        self.assertNotIn("limits", resources)

    def test_a_value_that_is_not_a_quantity_fails_naming_the_key(self):
        err = self._render([f"{_VALUE_PATH}.limits.memory=lots"], expect_failure=True)
        self.assertIn(f'{_VALUE_PATH}.limits.memory is "lots", which is not a Kubernetes quantity', err)

    def test_a_quantity_past_float64_fails_as_unrepresentable_not_as_under_the_floor(self):
        # Sprig's float64 answers 0 on a range error, which the floor compare would
        # otherwise report as "under the 512Mi floor".
        err = self._render([f"{_VALUE_PATH}.limits.memory=1e400"], expect_failure=True)
        self.assertIn(f"{_VALUE_PATH}.limits.memory is 1e400, which is not a representable byte count", err)
        self.assertNotIn("under the", err)

    def test_a_quantity_past_int64_fails_naming_the_downward_api_bound(self):
        # 9Ei is a float64 but not an int64; the Downward API cannot hand it to the manager.
        err = self._render([f"{_VALUE_PATH}.limits.memory=9Ei"], expect_failure=True)
        self.assertIn(f"{_VALUE_PATH}.limits.memory is 9Ei, which exceeds the 9223372036854775807 bytes an int64 holds", err)
        self.assertNotIn("under the", err)

    def test_a_negative_quantity_fails_naming_the_key(self):
        err = self._render([f"{_VALUE_PATH}.limits.memory=-1Gi"], expect_failure=True)
        self.assertIn(f"{_VALUE_PATH}.limits.memory is -1Gi; a quantity must not be negative", err)

    def test_a_scalar_limits_side_fails_naming_the_key(self):
        err = self._render([f"{_VALUE_PATH}.limits=1Gi"], expect_failure=True)
        self.assertIn(f"{_VALUE_PATH}.limits is 1Gi, which is not a map of resource name to quantity", err)


if __name__ == "__main__":
    unittest.main()
