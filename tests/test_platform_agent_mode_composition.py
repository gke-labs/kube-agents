"""terraform/examples/full-install's platform_agent_mode reaches the chart's
platformAgent.mode, and its default reaches it as nothing at all.

"today" must add no key to the values the composition hands the chart, or the
helm_release of every existing install plans an update on its next apply for a
setting nobody changed; the chart then leaves spec.mode out of the CR, which
the operator reads as today. "next" adds exactly platformAgent.mode. The
variable refuses anything outside the CRD's enum at plan, rather than at the
helm_release apply with the cluster half built.

Read as text, as the other composition-wiring tests here are, because the
terraform binary is not a suite dependency. Where one is on PATH the variable
block and the merge's operand are also lifted verbatim out of the two files
into a scratch root with no providers and evaluated by `terraform apply`, so the
validation and the expression are exercised as Terraform reads them, not as a
regular expression does. The root has no resources, so its apply only writes
an output into the scratch directory's local state.

Run: python3 -m unittest discover -s tests -p 'test_platform_agent_mode_composition.py' -v
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
_COMPOSITION = _REPO_ROOT / "terraform" / "examples" / "full-install"
_CRD = _REPO_ROOT / "charts" / "kube-agents" / "crds" / "kubeagents.x-k8s.io_platformagents.yaml"
_DEFAULTS = _REPO_ROOT / "install.defaults.env"
_TFVARS_EXAMPLE = _COMPOSITION / "terraform.tfvars.example"
# The merge's operand: what platform_agent_mode adds to platformAgent.
_OPERAND_RE = re.compile(
    r"^      \}, (var\.platform_agent_mode == \"today\" \? \{\} : \{\n      mode = var\.platform_agent_mode\n    \})\)$",
    re.MULTILINE,
)
_TERRAFORM_TIMEOUT_SECONDS = 120


def _variable_block() -> str:
    variables = (_COMPOSITION / "variables.tf").read_text()
    start = variables.index('variable "platform_agent_mode" {')
    return variables[start : variables.index("\n}\n", start) + 3]


def _crd_enum() -> list:
    crd = yaml.safe_load(_CRD.read_text())
    version = next(v for v in crd["spec"]["versions"] if v["name"] == "v1alpha1")
    return version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["mode"]["enum"]


class CompositionTextTest(unittest.TestCase):
    def setUp(self):
        self.variable = _variable_block()
        self.main_tf = (_COMPOSITION / "main.tf").read_text()

    def test_the_variable_defaults_to_today_and_takes_the_crd_enum(self):
        self.assertIn('default     = "today"', self.variable)
        self.assertIn("nullable    = false", self.variable)
        enum = _crd_enum()
        self.assertIn(f"contains({json.dumps(enum)}, var.platform_agent_mode)".replace('","', '", "'), self.variable)
        # The front doors' default is the same word.
        self.assertIn('DEFAULT_PLATFORM_AGENT_MODE="today"', _DEFAULTS.read_text())

    def test_platform_agent_is_merged_with_the_mode_only_when_it_is_not_today(self):
        self.assertIn("    platformAgent = merge({\n", self.main_tf)
        self.assertRegex(self.main_tf, _OPERAND_RE)
        # One route from the variable to the values, and it is that one.
        self.assertEqual(self.main_tf.count("var.platform_agent_mode"), 2)

    def test_the_tfvars_example_shows_the_key(self):
        self.assertIn('# platform_agent_mode = "today"', _TFVARS_EXAMPLE.read_text())


@unittest.skipUnless(shutil.which("terraform"), "needs a terraform binary")
class CompositionEvaluatedTest(unittest.TestCase):
    """The verbatim block and operand, evaluated by Terraform itself."""

    @classmethod
    def setUpClass(cls):
        operand = _OPERAND_RE.search((_COMPOSITION / "main.tf").read_text())
        if operand is None:
            raise AssertionError("the platformAgent merge operand is not where CompositionTextTest pins it")
        cls.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.tmp.name)
        (root / "variables.tf").write_text(_variable_block())
        (root / "outputs.tf").write_text(
            "output \"platform_agent_values\" {\n"
            f"  value = jsonencode(merge({{ untouched = true }}, {operand.group(1)}))\n"
            "}\n"
        )
        init = cls._terraform(root, "init", "-input=false", "-no-color")
        if init.returncode != 0:
            raise AssertionError(init.stderr)
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def _terraform(root, *args):
        return subprocess.run(
            ["terraform", f"-chdir={root}", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_TERRAFORM_TIMEOUT_SECONDS,
        )

    def _apply(self, *var_args) -> subprocess.CompletedProcess:
        # An apply of a root with no resources only records its output in the
        # scratch directory's local state, which is then read back as JSON.
        return self._terraform(self.root, "apply", "-auto-approve", "-input=false", "-no-color", *var_args)

    def _values(self, *var_args) -> dict:
        applied = self._apply(*var_args)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        out = self._terraform(self.root, "output", "-raw", "platform_agent_values")
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_today_adds_no_key(self):
        for args in ((), ("-var", "platform_agent_mode=today")):
            with self.subTest(args=args):
                self.assertEqual(self._values(*args), {"untouched": True})

    def test_next_adds_the_mode(self):
        self.assertEqual(self._values("-var", "platform_agent_mode=next"), {"untouched": True, "mode": "next"})

    def test_anything_else_is_refused_by_the_validation(self):
        for value in ("Next", "", "later", "null"):
            with self.subTest(value=value):
                out = self._apply("-var", f"platform_agent_mode={value}")
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("platform_agent_mode must be", out.stderr)


if __name__ == "__main__":
    unittest.main()
