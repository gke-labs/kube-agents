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


_OPERATOR_CRD = _REPO_ROOT / "k8s-operator" / "config" / "crd" / "bases" / "kubeagents.x-k8s.io_platformagents.yaml"
_MODE_GO = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "mode.go"


class ExistingInstallsKeepTodayTest(unittest.TestCase):
    """A new install defaults to next through install.sh alone. The CRD carries
    no default for spec.mode, and the operator reads an absent one as today.

    The API server applies a structural-schema default every time it reads an
    object from etcd, not only when the object is created. The composition and
    the chart render no mode field for today, so every existing today install
    has none. A CRD default of next would move all of them to next on their
    next reconcile, with no upgrade step to stop it.
    """

    def test_neither_crd_copy_defaults_the_mode(self):
        for path in (_CRD, _OPERATOR_CRD):
            with self.subTest(crd=path.relative_to(_REPO_ROOT).as_posix()):
                crd = yaml.safe_load(path.read_text())
                version = next(v for v in crd["spec"]["versions"] if v["name"] == "v1alpha1")
                mode = version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]["mode"]
                self.assertNotIn("default", mode)

    def test_the_example_leaves_the_mode_to_the_cluster(self):
        # INSTALL.md copies install.env.example onto running installs too. A
        # live key there would switch a today install to next; commented, the
        # cluster decides (adopt_live_platform_agent_mode).
        example = (_REPO_ROOT / "install.env.example").read_text()
        self.assertNotRegex(example, r"(?m)^\s*PLATFORM_AGENT_MODE=")
        self.assertIn("\n# PLATFORM_AGENT_MODE=next\n", example)

    def test_only_install_shs_first_install_resolver_reads_the_first_install_default(self):
        # A front door that falls back to the first-install default for a mode
        # install.env did not record treats an existing today install as next.
        reads = {
            "install.sh": [
                '  PLATFORM_AGENT_MODE_FALLBACK="${DEFAULT_FIRST_INSTALL_PLATFORM_AGENT_MODE:-${DEFAULT_PLATFORM_AGENT_MODE:-}}"'
            ],
        }
        for path in sorted(_REPO_ROOT.glob("*.sh")) + sorted((_REPO_ROOT / "scripts").rglob("*.sh")):
            rel = path.relative_to(_REPO_ROOT).as_posix()
            with self.subTest(path=rel):
                lines = [
                    line
                    for line in path.read_text().splitlines()
                    if "$DEFAULT_FIRST_INSTALL_PLATFORM_AGENT_MODE" in line
                    or "${DEFAULT_FIRST_INSTALL_PLATFORM_AGENT_MODE" in line
                ]
                self.assertEqual(lines, reads.get(rel, []))

    def test_released_scripts_still_read_today_as_an_existing_installs_mode(self):
        # install.sh and upgrade.sh source the target tree's
        # install.defaults.env, so a released script installing this tree reads
        # DEFAULT_PLATFORM_AGENT_MODE as it always has: the mode of an install
        # whose install.env records none. Sourced, under a strict shell, with
        # the expression those scripts use.
        script = (
            "set -euo pipefail\n"
            f". {_DEFAULTS}\n"
            "unset PLATFORM_AGENT_MODE PARAM_PLATFORM_AGENT_MODE\n"
            'echo "INSTALL=[${PARAM_PLATFORM_AGENT_MODE:-$DEFAULT_PLATFORM_AGENT_MODE}] '
            'UPGRADE=[${PLATFORM_AGENT_MODE:-$DEFAULT_PLATFORM_AGENT_MODE}]"\n'
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("INSTALL=[today] UPGRADE=[today]", proc.stdout)
        # And upgrade.sh in this tree is such a reader.
        self.assertIn(
            '"${PLATFORM_AGENT_MODE:-$DEFAULT_PLATFORM_AGENT_MODE}"', (_REPO_ROOT / "upgrade.sh").read_text()
        )

    def test_the_operator_reads_an_absent_mode_as_today(self):
        text = _MODE_GO.read_text()
        self.assertIn("if agent == nil || agent.Spec.Mode == nil {\n\t\treturn ModeToday, nil", text)


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
