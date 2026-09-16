"""agent_ksa_name keeps a second install's agent KSA inside the admission policy's selector.

full-install exposes `agent_ksa_name` so a second install in one project binds its
own Workload Identity principal (#1239: the principal is namespace/KSA with no
cluster in it, so two installs with the default KSA name bind the same one).
Three things have to hold for the variable to be safe to set, and each is
asserted against source rather than against another document:

  - one value reaches both consumers: the kube-agents-iam module's `ksa_name`
    (the Workload Identity binding) and the chart's
    `platformAgent.security.serviceAccountName` (the pod). A mismatch points the
    binding at a KSA that does not exist.
  - the default equals what both consumers defaulted to before the variable
    existed, so an install that never sets it does not move.
  - the validation's suffix is the one the `kube-agents-agent-binding-scope`
    ValidatingAdmissionPolicy selects on. Its `binds-agent-sa` matchCondition is
    `s.name.endsWith('-agent')`, and a matchCondition that evaluates false
    removes the object from the policy rather than failing it, so a KSA outside
    the suffix leaves every binding to it unselected. Today the policy's one
    validation names `developer-team-agent` and the operator's reconcile is
    exempt, so the suffix changes no admission decision on current main; it
    keeps the install inside the selector for what the policy gains. If the
    policy's suffix moves, this fails until the validation follows.

Terraform is not a dependency of this suite. The DNS-1123 regex is exercised
through Python's engine (the class is plain enough that RE2 and `re` agree on it)
and the suffix check through the same comparison Terraform's endswith() makes.

Run:
  python3 -m unittest discover -s tests -p 'test_agent_ksa_name_guard.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

FULL_INSTALL = REPO_ROOT / "terraform" / "examples" / "full-install"
ROOT_VARIABLES = FULL_INSTALL / "variables.tf"
ROOT_MAIN = FULL_INSTALL / "main.tf"
IAM_MODULE_VARIABLES = REPO_ROOT / "terraform" / "modules" / "kube-agents-iam" / "variables.tf"
CHART_VALUES = REPO_ROOT / "charts" / "kube-agents" / "values.yaml"
POLICY_SRC = REPO_ROOT / "k8s-operator" / "config" / "admission" / "agent-rbac-policy.yaml"
CHART_POLICY = REPO_ROOT / "charts" / "kube-agents" / "templates" / "agent-rbac-admission-policy.yaml"

ROOT_VARIABLE = "agent_ksa_name"
MODULE_VARIABLE = "ksa_name"
MODULE_CALL = "kube_agents_iam"
CHART_VALUE_PATH = ("platformAgent", "security", "serviceAccountName")
POLICY_NAME = "kube-agents-agent-binding-scope"
POLICY_MATCH_CONDITION = "binds-agent-sa"

# DNS-1123 label ceiling. Kubernetes allows a ServiceAccount name to be a DNS
# subdomain; the variable narrows it to a label because the value is
# interpolated into the Workload Identity member and system:serviceaccount:
# principals, and this is the ceiling its regex declares.
DNS_LABEL_MAX = 63

# A Terraform `variable "<name>" { ... }` block, up to the next top-level block
# or end of file. Blocks here are separated by a blank line and the next
# `variable`, so the lazy match ends at the right brace.
VARIABLE_BLOCK_RE = r'variable "{name}" \{{\n(?P<body>.*?)\n\}}\n'
DEFAULT_RE = re.compile(r'^\s*default\s*=\s*"(?P<value>[^"]*)"\s*$', re.M)
REGEX_CONDITION_RE = re.compile(
    r'condition\s*=\s*can\(regex\("(?P<pattern>[^"]+)",\s*var\.' + ROOT_VARIABLE + r"\)\)"
)
SUFFIX_CONDITION_RE = re.compile(
    r'condition\s*=\s*endswith\(var\.' + ROOT_VARIABLE + r',\s*"(?P<suffix>[^"]+)"\)'
)
ERROR_MESSAGE_RE = re.compile(r'error_message\s*=\s*"(?P<message>(?:[^"\\]|\\.)*)"')
# The selector, in CEL, read out of the named matchCondition's expression rather
# than grepped from the file, so a header comment quoting it cannot confuse this.
POLICY_SUFFIX_RE = re.compile(r"s\.name\.endsWith\('(?P<suffix>[^']+)'\)")
# The chart copy is the source wrapped in one Go-template gate line at each end;
# stripping those leaves loadable YAML. tests/test_admission_policy_shipped.py
# asserts the wrapping stays exactly that.
CHART_TEMPLATE_PREFIX = "{{- if "
CHART_TEMPLATE_SUFFIX = "{{- end }}"
MODULE_CALL_RE = re.compile(r'module "' + MODULE_CALL + r'" \{\n(?P<body>.*?)\n\}\n', re.S)
MODULE_KSA_ARG_RE = re.compile(r"^\s*" + MODULE_VARIABLE + r"\s*=\s*var\." + ROOT_VARIABLE + r"\s*$", re.M)
# The platformAgent.security map inside the helm_release values, bounded by its
# own closing brace at its own indentation (six spaces): nested maps close
# deeper, so the lazy match cannot run past the block into a later one.
CHART_SECURITY_BLOCK_RE = re.compile(r"\n      security = \{\n(?P<body>.*?)\n      \}\n", re.S)
CHART_KSA_ARG_RE = re.compile(r"^\s*serviceAccountName\s*=\s*var\." + ROOT_VARIABLE + r"\s*$", re.M)

ACCEPTED_NAMES = [
    "kubeagents-platform-agent",
    "a-agent",
    "kubeagents-platform-2-agent",
    "x" * (DNS_LABEL_MAX - len("-agent")) + "-agent",
]
REJECTED_BY_LABEL_CHECK = [
    "",
    "Foo-agent",
    "-agent",
    "x_y-agent",
    "x.y-agent",
    "x" * (DNS_LABEL_MAX - len("-agent") + 1) + "-agent",
    "ends-with-hyphen-agent-",
]
# Valid labels the suffix check alone has to refuse.
REJECTED_BY_SUFFIX_CHECK = ["foo", "agent", "kubeagents-platform", "agent-kubeagents"]


def variable_block(text: str, name: str) -> str:
    match = re.search(VARIABLE_BLOCK_RE.format(name=name), text, re.S)
    if match is None:
        raise AssertionError(f'no variable "{name}" block found')
    return match.group("body")


def root_block() -> str:
    return variable_block(ROOT_VARIABLES.read_text(encoding="utf-8"), ROOT_VARIABLE)


def single(pattern: re.Pattern, text: str, what: str) -> re.Match:
    found = list(pattern.finditer(text))
    if len(found) != 1:
        raise AssertionError(f"expected exactly one {what}, found {len(found)}")
    return found[0]


class DefaultDoesNotMoveAnyInstallTest(unittest.TestCase):
    def test_root_default_equals_module_default(self):
        root_default = single(DEFAULT_RE, root_block(), "root default").group("value")
        module_default = single(
            DEFAULT_RE,
            variable_block(IAM_MODULE_VARIABLES.read_text(encoding="utf-8"), MODULE_VARIABLE),
            "module default",
        ).group("value")
        self.assertEqual(
            module_default,
            root_default,
            "the composition's default KSA name differs from the module's; an install "
            "that never set agent_ksa_name would have its Workload Identity binding move",
        )

    def test_root_default_equals_chart_default(self):
        root_default = single(DEFAULT_RE, root_block(), "root default").group("value")
        values = yaml.safe_load(CHART_VALUES.read_text(encoding="utf-8"))
        chart_default = values
        for key in CHART_VALUE_PATH:
            chart_default = chart_default[key]
        self.assertEqual(
            chart_default,
            root_default,
            "the composition's default KSA name differs from the chart's; an install "
            "that never set agent_ksa_name would have its agent pod re-created under "
            "a different ServiceAccount",
        )

    def test_root_default_passes_its_own_validations(self):
        block = root_block()
        default = single(DEFAULT_RE, block, "root default").group("value")
        pattern = single(REGEX_CONDITION_RE, block, "regex validation").group("pattern")
        suffix = single(SUFFIX_CONDITION_RE, block, "suffix validation").group("suffix")
        self.assertRegex(default, pattern)
        self.assertTrue(default.endswith(suffix))


class OneVariableFeedsBothConsumersTest(unittest.TestCase):
    def test_module_call_passes_the_variable_as_ksa_name(self):
        main = ROOT_MAIN.read_text(encoding="utf-8")
        body = single(MODULE_CALL_RE, main, f'module "{MODULE_CALL}" block').group("body")
        self.assertRegex(
            body,
            MODULE_KSA_ARG_RE,
            f"module.{MODULE_CALL} does not pass {MODULE_VARIABLE} = var.{ROOT_VARIABLE}; "
            "the Workload Identity binding falls back to the module default while the "
            "pod runs as whatever the chart was told",
        )

    def test_chart_values_pass_the_variable_as_service_account_name(self):
        main = ROOT_MAIN.read_text(encoding="utf-8")
        body = single(CHART_SECURITY_BLOCK_RE, main, "platformAgent.security block").group("body")
        self.assertRegex(
            body,
            CHART_KSA_ARG_RE,
            f"the helm_release values do not set platformAgent.security.serviceAccountName "
            f"= var.{ROOT_VARIABLE}; the pod would run as the chart default while the "
            "binding names the variable",
        )

    def test_nothing_else_names_the_module_default_for_the_agent(self):
        """The composition should hold the name in exactly one place.

        A second literal of the default in main.tf is the seam a future edit
        moves one consumer without the other through.
        """
        main = ROOT_MAIN.read_text(encoding="utf-8")
        default = single(DEFAULT_RE, root_block(), "root default").group("value")
        self.assertNotIn(default, main)


def policy_documents(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if lines[0].startswith(CHART_TEMPLATE_PREFIX) and lines[-1] == CHART_TEMPLATE_SUFFIX:
        text = "\n".join(lines[1:-1])
    return [d for d in yaml.safe_load_all(text) if d]


class SuffixIsThePolicySelectorTest(unittest.TestCase):
    def policy_suffix(self, path: Path) -> str:
        policies = [
            d for d in policy_documents(path)
            if d["kind"] == "ValidatingAdmissionPolicy" and d["metadata"]["name"] == POLICY_NAME
        ]
        self.assertEqual(1, len(policies), f"{path.name} does not hold exactly one {POLICY_NAME}")
        by_name = {c["name"]: c["expression"] for c in policies[0]["spec"]["matchConditions"]}
        self.assertIn(POLICY_MATCH_CONDITION, by_name, f"{path.name}: matchCondition renamed or gone")
        return single(POLICY_SUFFIX_RE, by_name[POLICY_MATCH_CONDITION], f"selector in {path.name}").group("suffix")

    def test_validation_suffix_equals_policy_source_suffix(self):
        suffix = single(SUFFIX_CONDITION_RE, root_block(), "suffix validation").group("suffix")
        self.assertEqual(
            self.policy_suffix(POLICY_SRC),
            suffix,
            "the -agent validation and the admission policy's binds-agent-sa selector "
            "disagree; a KSA the composition accepts would fall outside the policy",
        )

    def test_validation_suffix_equals_chart_copy_suffix(self):
        suffix = single(SUFFIX_CONDITION_RE, root_block(), "suffix validation").group("suffix")
        self.assertEqual(self.policy_suffix(CHART_POLICY), suffix)

    def test_suffix_error_message_names_the_policy(self):
        block = root_block()
        messages = [m.group("message") for m in ERROR_MESSAGE_RE.finditer(block)]
        self.assertTrue(
            any(POLICY_NAME in m and POLICY_MATCH_CONDITION in m for m in messages),
            "the suffix validation's error message has to say which admission policy "
            "and which matchCondition the suffix exists for; a bare 'must end in -agent' "
            "reads as a naming convention, which is what gets relaxed",
        )


class ValidationsAcceptAndRejectTest(unittest.TestCase):
    def setUp(self):
        block = root_block()
        self.pattern = re.compile(single(REGEX_CONDITION_RE, block, "regex validation").group("pattern"))
        self.suffix = single(SUFFIX_CONDITION_RE, block, "suffix validation").group("suffix")

    def accepted(self, name: str) -> bool:
        return bool(self.pattern.search(name)) and name.endswith(self.suffix)

    def test_accepts_labels_ending_in_the_suffix(self):
        for name in ACCEPTED_NAMES:
            with self.subTest(name=name):
                self.assertTrue(self.accepted(name))

    def test_label_check_rejects_non_labels(self):
        for name in REJECTED_BY_LABEL_CHECK:
            with self.subTest(name=name):
                self.assertIsNone(self.pattern.search(name))

    def test_suffix_check_rejects_labels_outside_the_suffix(self):
        for name in REJECTED_BY_SUFFIX_CHECK:
            with self.subTest(name=name):
                self.assertIsNotNone(self.pattern.search(name), "fixture is not a valid label")
                self.assertFalse(name.endswith(self.suffix))


if __name__ == "__main__":
    unittest.main()
