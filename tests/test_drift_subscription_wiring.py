"""The drift subscription's name reaches both of its consumers, and the copies agree.

full-install composes the drift-pubsub module behind `enable_drift_pubsub` and
lets a caller rename the trio it creates (`drift_pubsub_topic`,
`drift_pubsub_subscription`, `drift_pubsub_sink`). Two things have to hold for
the rename to be safe, and each is asserted against source rather than against
another document:

  - one subscription name reaches both consumers: the module's
    `subscription_name` (what gets created) and the chart's
    `platformAgent.harness.driftDetector.subscription` (what the detector
    pulls from). The detector's compiled-in default is the module's default
    name, so an install that renames the subscription and does not carry the
    rename into the CR has a detector that pulls a subscription that does not
    exist. Nothing fails closed on that: the detector never exits, it retries
    the pull for the life of the pod, the entrypoint's short-exit ALERT cannot
    fire, and the pod stays Ready (the package comment on
    k8s-operator/cmd/drift-detector/main.go says so), which is what makes the
    failure silent.
  - the chart block is written only when the module exists. The chart renders
    a `driftDetector` block into the CR as soon as one field is set, and the
    CR template says an install that never asked for drift detection should
    not carry one.
  - the composition's three defaults equal the module's, and the detector's
    `defaultSubscriptionName` equals the subscription's, so an install that
    never sets a name gets the resource the detector looks for. docs/README.md
    names this file as what holds those copies together.

Terraform is not a dependency of this suite; the HCL is read as text.

Run:
  python3 -m unittest discover -s tests -p 'test_drift_subscription_wiring.py' -v
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
MODULE_VARIABLES = REPO_ROOT / "terraform" / "modules" / "drift-pubsub" / "variables.tf"
DETECTOR_MAIN = REPO_ROOT / "k8s-operator" / "cmd" / "drift-detector" / "main.go"
CHART_VALUES = REPO_ROOT / "charts" / "kube-agents" / "values.yaml"
CHART_CR_TEMPLATE = REPO_ROOT / "charts" / "kube-agents" / "templates" / "platform-agent-cr.yaml"

FLAG_VARIABLE = "enable_drift_pubsub"
MODULE_CALL = "drift_pubsub"
# Composition variable -> module variable, for the three names the module
# takes and lifecycle.sh adopts by.
NAME_VARIABLES = {
    "drift_pubsub_topic": "topic_name",
    "drift_pubsub_subscription": "subscription_name",
    "drift_pubsub_sink": "sink_name",
}
SUBSCRIPTION_VARIABLE = "drift_pubsub_subscription"
DETECTOR_DEFAULT_CONSTANT = "defaultSubscriptionName"
CHART_VALUE_PATH = ("platformAgent", "harness", "driftDetector", "subscription")
CHART_TEMPLATE_FIELD = '"subscription" $drift.subscription'

# A Terraform `variable "<name>" { ... }` block, up to its closing brace at
# column zero. Blocks here are separated by a blank line and the next
# `variable`, so the lazy match ends at the right brace.
VARIABLE_BLOCK_RE = r'variable "{name}" \{{\n(?P<body>.*?)\n\}}\n'
DEFAULT_RE = re.compile(r'^\s*default\s*=\s*"(?P<value>[^"]*)"\s*$', re.M)
MODULE_CALL_RE = re.compile(r'module "' + MODULE_CALL + r'" \{\n(?P<body>.*?)\n\}\n', re.S)
MODULE_ARG_RE = r"^\s*{module_var}\s*=\s*var\.{root_var}\s*$"
GO_CONST_RE = re.compile(r"^\s*" + DETECTOR_DEFAULT_CONSTANT + r'\s*=\s*"(?P<value>[^"]*)"', re.M)
# The platformAgent.harness value inside the helm_release values. It is a
# merge() of the always-present map and the conditional drift block, bounded
# by its own closing paren at its own indentation (six spaces).
CHART_HARNESS_BLOCK_RE = re.compile(r"\n      harness = merge\(\n(?P<body>.*?)\n      \)\n", re.S)
# The conditional as written: the flag, then a driftDetector map whose only
# field is the subscription, read from the module instance rather than from
# the variable so the Helm release waits for the subscription to exist.
CHART_DRIFT_BLOCK_RE = re.compile(
    r"var\." + FLAG_VARIABLE + r"\s*\?\s*\{\s*"
    r"driftDetector\s*=\s*\{\s*"
    r"subscription\s*=\s*module\." + MODULE_CALL + r"\[0\]\.subscription_name\s*"
    r"\}\s*\}\s*:\s*\{\}",
    re.S,
)


def variable_block(text: str, name: str) -> str:
    match = re.search(VARIABLE_BLOCK_RE.format(name=name), text, re.S)
    if match is None:
        raise AssertionError(f'no variable "{name}" block found')
    return match.group("body")


def single(pattern: re.Pattern, text: str, what: str) -> re.Match:
    found = list(pattern.finditer(text))
    if len(found) != 1:
        raise AssertionError(f"expected exactly one {what}, found {len(found)}")
    return found[0]


def root_default(name: str) -> str:
    return single(DEFAULT_RE, variable_block(ROOT_VARIABLES.read_text(encoding="utf-8"), name), f"{name} default").group("value")


def module_default(name: str) -> str:
    return single(DEFAULT_RE, variable_block(MODULE_VARIABLES.read_text(encoding="utf-8"), name), f"{name} default").group("value")


class DefaultsAgreeTest(unittest.TestCase):
    def test_root_defaults_equal_module_defaults(self):
        for root_var, module_var in NAME_VARIABLES.items():
            with self.subTest(variable=root_var):
                self.assertEqual(
                    module_default(module_var),
                    root_default(root_var),
                    f"the composition's {root_var} default differs from the module's {module_var}; "
                    "an install that never sets it would adopt one name and create another",
                )

    def test_detector_default_equals_subscription_default(self):
        detector_default = single(GO_CONST_RE, DETECTOR_MAIN.read_text(encoding="utf-8"), "detector default").group("value")
        self.assertEqual(
            root_default(SUBSCRIPTION_VARIABLE),
            detector_default,
            "the detector's compiled-in subscription name differs from the one the composition "
            "creates by default; an install that sets neither would pull a subscription that does not exist",
        )


class OneNameReachesBothConsumersTest(unittest.TestCase):
    def setUp(self):
        self.main = ROOT_MAIN.read_text(encoding="utf-8")

    def test_module_is_passed_the_root_variables(self):
        body = single(MODULE_CALL_RE, self.main, f'module "{MODULE_CALL}" call').group("body")
        for root_var, module_var in NAME_VARIABLES.items():
            with self.subTest(variable=root_var):
                pattern = re.compile(MODULE_ARG_RE.format(module_var=module_var, root_var=root_var), re.M)
                single(pattern, body, f"{module_var} = var.{root_var} in the module call")

    def test_chart_is_passed_the_module_subscription_behind_the_flag(self):
        body = single(CHART_HARNESS_BLOCK_RE, self.main, "platformAgent.harness merge").group("body")
        single(CHART_DRIFT_BLOCK_RE, body, "driftDetector.subscription conditional on the flag")

    def test_chart_exposes_the_value_path_the_composition_writes(self):
        values = yaml.safe_load(CHART_VALUES.read_text(encoding="utf-8"))
        node = values
        for key in CHART_VALUE_PATH:
            self.assertIn(key, node, f"chart values have no {'.'.join(CHART_VALUE_PATH)}; the composition writes it")
            node = node[key]
        self.assertIn(
            CHART_TEMPLATE_FIELD,
            CHART_CR_TEMPLATE.read_text(encoding="utf-8"),
            "the CR template no longer renders driftDetector.subscription from the values path the composition writes",
        )


if __name__ == "__main__":
    unittest.main()
