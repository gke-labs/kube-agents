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
    not carry one. The same block carries `enabled` from
    `enable_drift_detector`, and a precondition refuses the other order. What
    that precondition prevents is quieter than the silent failure above:
    because the block is written only when the ingress is on, an
    `enable_drift_detector` set without `enable_drift_pubsub` renders no
    `driftDetector` block at all, so the apply succeeds, provisions nothing,
    starts nothing, and leaves a variable that did nothing as the only
    evidence. The precondition tests `local.drift_detector_requested` rather
    than the flag, because `extra_helm_values` reaches the same leaf: Helm
    deep-merges it over the computed document, so a caller can turn the
    detector on there and land the first failure above instead of this one.
  - the composition's three defaults equal the module's, and the detector's
    `defaultSubscriptionName` equals the subscription's, so an install that
    never sets a name gets the resource the detector looks for. docs/README.md
    names this file as what holds those copies together.

Terraform is not a dependency of this suite; the HCL is read as text.

Run:
  python3 -m unittest discover -s tests -p 'test_drift_subscription_wiring.py' -v
"""

from __future__ import annotations

import json
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
CHART_SCHEMA = REPO_ROOT / "charts" / "kube-agents" / "values.schema.json"

FLAG_VARIABLE = "enable_drift_pubsub"
DETECTOR_FLAG_VARIABLE = "enable_drift_detector"
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
DRIFT_VALUE_PREFIX = ("platformAgent", "harness", "driftDetector")
# Each leaf the composition writes, and the CR template line that renders it.
CHART_LEAVES = {
    "subscription": '"subscription" $drift.subscription',
    "enabled": '"enabled" $drift.enabled',
}

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
# The conditional as written: the ingress flag, then a driftDetector map
# carrying the subscription -- read from the module instance rather than from
# the variable, so the Helm release waits for the subscription to exist -- and
# enabled, true or null rather than true or false so that the chart's
# compactFields drops the field and the CRD default applies.
CHART_DRIFT_BLOCK_RE = re.compile(
    r"var\." + FLAG_VARIABLE + r"\s*\?\s*\{\s*"
    r"driftDetector\s*=\s*\{\s*"
    r"subscription\s*=\s*module\." + MODULE_CALL + r"\[0\]\.subscription_name\s*"
    r"enabled\s*=\s*var\." + DETECTOR_FLAG_VARIABLE + r"\s*\?\s*true\s*:\s*null\s*"
    r"\}\s*\}\s*:\s*\{\}",
    re.S,
)
# The precondition that refuses the consumer without the ingress, and the local
# it tests. Read as HCL text rather than by planning: Terraform is not a
# dependency here.
#
# Both doors to the field have to be in that local. extra_helm_values is a
# second values document Helm deep-merges over the one the composition
# computes, and its own description names the harness knobs as what it is for,
# so the same leaf set there starts the detector without
# enable_drift_detector ever being read. A precondition naming the flag alone
# accepts that apply, and what it renders is the failure the detector's README
# calls silent: a pull against a subscription that was never created, retried
# for the life of a pod that stays Ready.
REQUESTED_LOCAL = "drift_detector_requested"
PRECONDITION_RE = re.compile(
    r"condition\s*=\s*!local\." + REQUESTED_LOCAL + r"\s*\|\|\s*var\." + FLAG_VARIABLE + r"\s*$",
    re.M,
)
# The `== true` is pinned, not incidental. tobool(null) converts rather than
# failing, so the try catches nothing and a null of type bool reaches `||`,
# whose arguments may not be null: the plan aborts on the local itself, for
# every apply passing the leaf as null, which is what the chart's values.yaml
# and values.schema.json teach. `null == true` is false, so the comparison
# is what turns that into "not requested".
REQUESTED_LOCAL_RE = re.compile(
    REQUESTED_LOCAL + r"\s*=\s*\(\s*"
    r"var\." + DETECTOR_FLAG_VARIABLE + r"\s*\|\|\s*"
    r"try\(tobool\(var\.extra_helm_values\."
    + r"\.".join(DRIFT_VALUE_PREFIX)
    + r"\.enabled\)\s*==\s*true\s*,\s*false\)",
    re.S,
)
# The other direction through the same leaf: extra_helm_values countermanding
# the flag rather than standing in for it. Both halves are pinned because
# either alone is wrong. Without can() an absent leaf reads as null and every
# ordinary install is refused; with `== null` in place of `!= true` a false
# leaf passes, and false silences the detector exactly as null does.
COUNTERMAND_LOCAL = "extra_helm_values_countermands_detector"
LEAF_PATH = r"var\.extra_helm_values\." + r"\.".join(DRIFT_VALUE_PREFIX) + r"\.enabled"
COUNTERMAND_LOCAL_RE = re.compile(
    COUNTERMAND_LOCAL + r"\s*=\s*\(\s*"
    r"var\." + DETECTOR_FLAG_VARIABLE + r"\s*&&\s*"
    r"can\(" + LEAF_PATH + r"\)\s*&&\s*"
    r"try\(" + LEAF_PATH + r",\s*null\)\s*!=\s*true",
    re.S,
)
COUNTERMAND_PRECONDITION_RE = re.compile(
    r"condition\s*=\s*!local\." + COUNTERMAND_LOCAL + r"\s*$",
    re.M,
)
# The document extra_helm_values contributes, and the fact the countermand
# rests on: it is passed later in the same list than the computed one, and
# Helm's merge gives the later document the key.
EXTRA_VALUES_DOCUMENT = "yamlencode(var.extra_helm_values)"


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
        single(CHART_DRIFT_BLOCK_RE, body, "driftDetector block conditional on the flag")

    def test_the_detector_cannot_be_enabled_without_the_ingress(self):
        single(
            PRECONDITION_RE,
            self.main,
            f"precondition refusing the detector without {FLAG_VARIABLE}",
        )

    def test_the_refusal_covers_the_values_document_a_caller_can_pass(self):
        single(
            REQUESTED_LOCAL_RE,
            self.main,
            f"local.{REQUESTED_LOCAL} reading both enable_drift_detector and the extra_helm_values leaf",
        )

    def test_the_leaf_the_refusal_reads_is_one_the_chart_teaches_as_null(self):
        """Why `local.drift_detector_requested` compares rather than converts.

        `tobool` fails on "yes" and on 1, and the `try` catches both. It does
        not fail on null: the conversion succeeds and returns a null of type
        bool, so the `try` has nothing to catch and the null reaches `||`,
        whose arguments may not be null. The plan aborts on the local, naming
        no variable, for every apply that passes the leaf that way — including
        `enable_drift_detector = true`, since OpenTofu evaluates both operands.

        That input is not a typo, which is what makes it worth a test rather
        than a comment: the chart ships the leaf as null and its schema types
        it that way, because null is how a knob is omitted so the CRD's own
        default applies. An operator copying the `driftDetector` block into
        `extra_helm_values` to set `gitopsManagers`, which the composition
        does not expose, writes exactly that. Asserted against the chart
        rather than restated, so that a chart that stopped teaching null would
        take the reasoning with it instead of leaving it stale here.

        The `== true` itself is pinned by REQUESTED_LOCAL_RE, which
        test_the_refusal_covers_the_values_document_a_caller_can_pass runs.
        """
        values = yaml.safe_load(CHART_VALUES.read_text(encoding="utf-8"))
        node = values
        for key in (*DRIFT_VALUE_PREFIX, "enabled"):
            self.assertIn(key, node, f"chart values have no {'.'.join((*DRIFT_VALUE_PREFIX, 'enabled'))}")
            node = node[key]
        self.assertIsNone(node, "the chart no longer ships the detector leaf as null")
        schema = json.loads(CHART_SCHEMA.read_text(encoding="utf-8"))
        node = schema
        for key in (*DRIFT_VALUE_PREFIX, "enabled"):
            node = node["properties"][key]
        self.assertIn("null", node.get("type", []), f"values.schema.json no longer admits null there: {node}")

    def test_the_composition_refuses_a_leaf_that_countermands_the_flag(self):
        """The same leaf read the other way round.

        `local.drift_detector_requested` covers `extra_helm_values` asking for
        the detector without the flag. This covers the flag asking for it and
        `extra_helm_values` taking it away: the leaf lands in the second
        values document, Helm gives the later document the key, and a null
        deletes the field (the CRD default `false` then applies) while a
        `false` arrives as `false`. The tfvars say `enable_drift_detector =
        true`, the sink, topic and subscription are provisioned and bill, and
        the CR never starts a consumer — the state the other two
        preconditions exist to refuse, reached where neither of them looks.
        """
        single(
            COUNTERMAND_LOCAL_RE,
            self.main,
            f"local.{COUNTERMAND_LOCAL} reading the extra_helm_values leaf against the flag",
        )
        single(
            COUNTERMAND_PRECONDITION_RE,
            self.main,
            f"precondition refusing local.{COUNTERMAND_LOCAL}",
        )

    def test_extra_helm_values_is_the_later_document(self):
        """Why the countermand happens at all.

        Helm merges successive values documents with the later one winning the
        key, so the refusal above is only needed while `extra_helm_values` is
        passed after the document this composition computes. Reordering them
        would make the flag win and the precondition pointless — a change that
        should have to come past this test rather than leave a refusal nobody
        can trigger.
        """
        computed = self.main.find("driftDetector = {")
        self.assertNotEqual(computed, -1, "the computed document no longer writes a driftDetector block")
        passed = self.main.find(EXTRA_VALUES_DOCUMENT)
        self.assertNotEqual(passed, -1, f"the composition no longer passes {EXTRA_VALUES_DOCUMENT}")
        self.assertLess(
            computed,
            passed,
            f"{EXTRA_VALUES_DOCUMENT} is no longer the later document, so the leaf no longer wins the key",
        )

    def test_chart_exposes_the_value_paths_the_composition_writes(self):
        values = yaml.safe_load(CHART_VALUES.read_text(encoding="utf-8"))
        template = CHART_CR_TEMPLATE.read_text(encoding="utf-8")
        for leaf, rendered in CHART_LEAVES.items():
            with self.subTest(leaf=leaf):
                path = (*DRIFT_VALUE_PREFIX, leaf)
                node = values
                for key in path:
                    self.assertIn(key, node, f"chart values have no {'.'.join(path)}; the composition writes it")
                    node = node[key]
                self.assertIn(
                    rendered,
                    template,
                    f"the CR template no longer renders driftDetector.{leaf} from the values path the composition writes",
                )


if __name__ == "__main__":
    unittest.main()
