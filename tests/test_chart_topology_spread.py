"""Every multi-replica workload the chart owns keeps its replicas off one node.

The chart shipped a PodDisruptionBudget and a default replicaCount of 2 for
litellm and github-token-minter with nothing spreading their pods, and the
Workload Reliability Audit in this repository found both: "replicas=2, no
topologySpreadConstraints or podAntiAffinity". The PDB does not cover that gap.
maxUnavailable: 1 stalls a *drain* that would take every replica; a node that
fails takes whatever is on it, and two replicas on one node go together.

What is guarded here is the shape of the next workload, not the two that were
fixed. A Deployment whose replica count comes from a value can be scaled past
one by any install, so each one needs the constraint; a Deployment pinned to a
literal `replicas: 1` (both Hindsight workloads) cannot, and a call site there
would render nothing while reading as coverage. The rule below is therefore
"templated replicas implies a call site", which a new workload trips by
existing rather than by anyone remembering this file.

The selector check is the other half. A labelSelector that does not match the
pods the constraint is attached to counts some other population and skews
against it, and the field is immutable once the Deployment exists -- so the
call site's selector is compared against its own Deployment's
spec.selector.matchLabels, resolving `include` on either side.

`helm` is not a dependency of this suite (it is not installed on the runner
that executes it), so this reads the templates rather than a real render, the
way test_admission_policy_shipped.py does for the same reason.

Run:
  python3 -m unittest discover -s tests -p 'test_chart_topology_spread.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = REPO_ROOT / "charts" / "kube-agents"
TEMPLATE_DIR = CHART_DIR / "templates"
HELPERS = TEMPLATE_DIR / "_helpers.tpl"
VALUES = CHART_DIR / "values.yaml"
# The kustomize dev path (INSTALL.md Method 2), which applies these by hand
# instead of rendering the chart.
KUSTOMIZE_DIR = REPO_ROOT / "k8s-operator" / "config"

HELPER_NAME = "kube-agents.topologySpreadConstraints"

# The constraint the helper must render, per
# agents/platform/governance/obtainability_audit_sop.md 3.8. ScheduleAnyway is
# the one the SOP calls mandatory: DoNotSchedule leaves the second replica
# Pending indefinitely on a pool that cannot satisfy the skew, which is worse
# than the co-location the constraint exists to avoid. The hostname key rather
# than the zone key because the loss guarded against is a node going away, and
# a zonal cluster has one zone to spread across.
EXPECTED_MAX_SKEW = "1"
EXPECTED_TOPOLOGY_KEY = "kubernetes.io/hostname"
EXPECTED_WHEN_UNSATISFIABLE = "ScheduleAnyway"
# The per-revision label the skew is scoped by, so a rollout counts the new
# ReplicaSet's pods alone; without it old and new pods are counted together and
# two nodes with one replica each can end the rollout with both new replicas on
# one node.
EXPECTED_MATCH_LABEL_KEY = "pod-template-hash"

# Below this the helper renders nothing: a constraint over a single pod is
# satisfied by construction.
MIN_REPLICAS_RENDERED = 1

# The values key each call site's `enabled` argument must resolve to, appended
# to the workload's own values path.
ENABLED_SUFFIX = ".topologySpread.enabled"

# How a call site has to read the flag: through `| default dict`, so a release
# installed before the chart gained `topologySpread` -- whose stored values
# `helm upgrade --reuse-values` renders against -- gets no constraint rather
# than a nil-pointer abort before anything is applied. Same shape the chart
# already uses for `agentSandbox` in operator-deployment.yaml.
_GUARDED_ENABLED = re.compile(r"^\((?P<path>\$?[\w.]+)\s*\|\s*default dict\)\.enabled$")

# Where a template assigns .Values.<x> to a local, so `$m.replicaCount` can be
# read back as `.Values.githubMinter.replicaCount`.
_LOCAL_ASSIGNMENT = re.compile(r"\{\{-?\s*(\$[A-Za-z_][\w]*)\s*:=\s*(\.Values\.[\w.]+)\s*\}\}")

_DEFINE_START = re.compile(r'\{\{-?\s*define\s+"([^"]+)"\s*-?\}\}')
_DEFINE_END = re.compile(r"\{\{-?\s*end\s*-?\}\}")

_INCLUDE_CALL = re.compile(r'include\s+"([^"]+)"')

# Read as a field rather than grepping the text for "DoNotSchedule": the
# comments explaining why ScheduleAnyway is the right answer name the wrong
# answer, and a substring search cannot tell the two apart.
_WHEN_UNSATISFIABLE = re.compile(r"^\s*whenUnsatisfiable:\s*(\S+)\s*$", re.MULTILINE)


def _templates():
    """Every rendered template in the chart, keyed by file name."""
    return {p.name: p.read_text() for p in sorted(TEMPLATE_DIR.glob("*.yaml"))}


def _defines():
    """Every named template in _helpers.tpl, keyed by name.

    The file nests no defines, so a define closes at the first `end` that is
    not closing an `if`/`range`/`with` opened inside it. Opens and ends are
    counted per line rather than per line-kind: `_helpers.tpl` carries
    one-line conditionals (`{{- if .x }}...{{- end }}`), and treating such a
    line as an open with no close mis-sliced every define after it.
    """
    lines = HELPERS.read_text().splitlines()
    bodies = {}
    name = None
    start = 0
    depth = 0
    for index, line in enumerate(lines):
        if name is None:
            match = _DEFINE_START.search(line)
            if match:
                name, start, depth = match.group(1), index + 1, 0
            continue
        depth += len(re.findall(r"\{\{-?\s*(?:if|range|with)\s", line))
        for _ in range(len(_DEFINE_END.findall(line))):
            if depth == 0:
                bodies[name] = "\n".join(lines[start:index])
                name = None
                break
            depth -= 1
    return bodies


def _balanced_suffix(text):
    """The first parenthesised expression, quoted string, or bare token.

    Call arguments are read out of a Helm pipeline, where the enclosing
    `(include ...)` and `{{- with ... }}` supply trailing parens and braces
    that a greedy match would swallow. Counting depth is what separates
    `(include "x" .)` from the `))` that closes the call around it, and the
    quoted case is separate because a selector like `"app: litellm"` carries a
    space that splitting on whitespace would cut in half.
    """
    text = text.strip()
    if text.startswith('"'):
        closing = text.index('"', 1)
        return text[: closing + 1]
    if not text.startswith("("):
        return text.split()[0].strip()
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                # A field read off the group, `(... | default dict).enabled`,
                # belongs to the same argument.
                trailing = re.match(r"(?:\.\w+)*", text[index + 1 :]).group(0)
                return text[: index + 1] + trailing
    raise AssertionError(f"unbalanced parentheses in call argument: {text!r}")


def _call_arguments(chunk):
    """The dict passed to the topologySpread helper in one manifest, or None."""
    marker = f'include "{HELPER_NAME}"'
    position = chunk.find(marker)
    if position < 0:
        return None
    tail = chunk[position + len(marker) :]
    arguments = {}
    for key in ("enabled", "replicas", "selectorLabels"):
        at = tail.find(f'"{key}"')
        assert at >= 0, f'the {HELPER_NAME} call passes no "{key}"'
        arguments[key] = _balanced_suffix(tail[at + len(key) + 2 :])
    return arguments


def _labels_from_yaml(text):
    """`key: value` lines read as a dict, ignoring blank lines."""
    labels = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        key, _, value = line.partition(":")
        labels[key.strip()] = value.strip()
    return labels


def _resolve_labels(expression, defines):
    """A selector expression -- literal YAML or an `include` -- as a dict.

    Both sides of the comparison go through this, so an operator selector
    written as `include "kube-agents.operatorSelectorLabels" .` on one side and
    the same include on the other compare equal without either being expanded
    into a second copy of those labels.
    """
    expression = expression.strip()
    include = _INCLUDE_CALL.search(expression)
    if include:
        name = include.group(1)
        assert name in defines, f"call references an undefined template {name}"
        return _labels_from_yaml(defines[name])
    return _labels_from_yaml(expression.strip('"'))


def _manifests(text):
    """A template's documents, split on its own `---` separators."""
    return re.split(r"^---\s*$", text, flags=re.MULTILINE)


def _selector_labels(chunk):
    """A Deployment's spec.selector.matchLabels, as written."""
    lines = chunk.splitlines()
    for index, line in enumerate(lines):
        if line.rstrip() != "  selector:":
            continue
        assert lines[index + 1].rstrip() == "    matchLabels:", (
            "spec.selector holds something other than matchLabels; "
            "the topologySpread call cannot be checked against it"
        )
        collected = []
        for follower in lines[index + 2 :]:
            if follower.strip() and not follower.startswith("      "):
                break
            collected.append(follower.strip().lstrip("{-").strip())
        # Strip the Helm pipeline off an `include ... | nindent N`.
        return "\n".join(part.split("|")[0].strip() for part in collected if part)
    raise AssertionError("Deployment has no spec.selector")


def _templated_replica_deployments():
    """Deployments whose replica count comes from a value, not a literal.

    Yields (file name, manifest text, the replicas expression).
    """
    for name, text in _templates().items():
        for chunk in _manifests(text):
            if not re.search(r"^kind: Deployment\s*$", chunk, flags=re.MULTILINE):
                continue
            match = re.search(r"^  replicas:\s*(.+)$", chunk, flags=re.MULTILINE)
            if not match:
                continue
            expression = match.group(1).strip()
            if "{{" not in expression:
                continue
            yield name, chunk, expression


def _values_path(expression, template_text):
    """A `.Values.x.y` or `$local.y` expression as a dotted values path.

    A guarded read, `(.Values.x.y | default dict).enabled`, resolves to the same
    path as the bare `.Values.x.y.enabled` it protects.
    """
    expression = expression.strip().strip("{}").strip()
    guarded = _GUARDED_ENABLED.match(expression)
    if guarded:
        expression = guarded.group("path") + ".enabled"
    for local, target in _LOCAL_ASSIGNMENT.findall(template_text):
        if expression.startswith(local + "."):
            return target + expression[len(local) :]
    return expression


class TopologySpreadHelperTest(unittest.TestCase):
    def setUp(self):
        self.defines = _defines()
        self.assertIn(HELPER_NAME, self.defines, f"{HELPERS.name} defines no {HELPER_NAME}")
        self.body = self.defines[HELPER_NAME]

    def test_renders_the_constraint_the_audit_sop_prescribes(self):
        self.assertIn(f"maxSkew: {EXPECTED_MAX_SKEW}", self.body)
        self.assertIn(f"topologyKey: {EXPECTED_TOPOLOGY_KEY}", self.body)
        self.assertIn(f"whenUnsatisfiable: {EXPECTED_WHEN_UNSATISFIABLE}", self.body)

    def test_scopes_the_skew_to_one_revision(self):
        self.assertRegex(
            self.body,
            rf"matchLabelKeys:\s*\n\s*-\s*{EXPECTED_MATCH_LABEL_KEY}\b",
            "without matchLabelKeys a rollout counts old and new pods together and "
            "can leave both new replicas on one node",
        )

    def test_never_renders_donotschedule(self):
        # The failure mode is a second replica Pending forever, which looks
        # like a scheduling problem rather than a chart change.
        self.assertEqual(
            set(_WHEN_UNSATISFIABLE.findall(self.body)), {EXPECTED_WHEN_UNSATISFIABLE}
        )

    def test_renders_nothing_for_a_single_replica(self):
        self.assertRegex(
            self.body,
            rf"gt\s+\(int\s+\.replicas\)\s+{MIN_REPLICAS_RENDERED}\b",
            "the helper must render nothing at one replica, where a spread "
            "constraint is satisfied by construction",
        )

    def test_is_gated_on_an_enabled_flag(self):
        self.assertIn(".enabled", self.body)


class TopologySpreadCallSiteTest(unittest.TestCase):
    def setUp(self):
        self.defines = _defines()
        self.values = yaml.safe_load(VALUES.read_text())
        self.deployments = list(_templated_replica_deployments())
        self.assertTrue(self.deployments, "found no Deployment with a templated replica count")

    def test_every_scalable_deployment_carries_the_constraint(self):
        for name, chunk, expression in self.deployments:
            with self.subTest(template=name):
                self.assertIsNotNone(
                    _call_arguments(chunk),
                    f"{name} renders a Deployment with replicas: {expression} and no "
                    f"{HELPER_NAME} call. Any install can scale it past one replica, and "
                    "nothing then keeps the replicas off one node.",
                )

    def test_each_call_selects_its_own_deployments_pods(self):
        for name, chunk, _ in self.deployments:
            arguments = _call_arguments(chunk)
            if arguments is None:
                continue
            with self.subTest(template=name):
                self.assertEqual(
                    _resolve_labels(arguments["selectorLabels"], self.defines),
                    _resolve_labels(_selector_labels(chunk), self.defines),
                    f"{name}'s topologySpread selector does not match the Deployment's own "
                    "spec.selector, so the constraint counts some other population",
                )

    def test_each_call_measures_its_own_deployments_replica_count(self):
        for name, chunk, expression in self.deployments:
            arguments = _call_arguments(chunk)
            if arguments is None:
                continue
            with self.subTest(template=name):
                self.assertEqual(
                    _values_path(arguments["replicas"], _templates()[name]),
                    _values_path(expression, _templates()[name]),
                    f"{name} passes a replica count the Deployment does not use, so the "
                    "constraint can vanish at two replicas or appear at one",
                )

    def test_each_enabled_flag_survives_a_release_that_predates_it(self):
        # `helm upgrade --reuse-values` renders the new chart against the stored
        # values of the old release, which have no `topologySpread` key, so a
        # bare `.Values.<x>.topologySpread.enabled` is a nil-pointer abort on
        # every upgrade from before this chart version. INSTALL.md's "on an
        # existing release" recipe is exactly that command.
        for name, chunk, _ in self.deployments:
            arguments = _call_arguments(chunk)
            if arguments is None:
                continue
            with self.subTest(template=name):
                self.assertRegex(
                    arguments["enabled"].strip(),
                    _GUARDED_ENABLED,
                    f"{name} reads the flag without `| default dict`, so an upgrade with "
                    "--reuse-values from a release that predates the key aborts",
                )

    def test_each_enabled_flag_has_a_default_in_values(self):
        for name, chunk, _ in self.deployments:
            arguments = _call_arguments(chunk)
            if arguments is None:
                continue
            path = _values_path(arguments["enabled"], _templates()[name])
            with self.subTest(template=name):
                self.assertTrue(
                    path.endswith(ENABLED_SUFFIX),
                    f"{name} gates the constraint on {path}, not on a {ENABLED_SUFFIX} key",
                )
                cursor = self.values
                for key in path[len(".Values.") :].split("."):
                    self.assertIsInstance(
                        cursor,
                        dict,
                        f"values.yaml has no {path}, so rendering the chart fails on a "
                        "nil lookup rather than on a missing knob",
                    )
                    self.assertIn(
                        key,
                        cursor,
                        f"values.yaml has no {path}, so rendering the chart fails on a "
                        "nil lookup rather than on a missing knob",
                    )
                    cursor = cursor[key]
                self.assertIsInstance(
                    cursor, bool, f"{path} defaults to {cursor!r}, which is not a boolean"
                )


class KustomizeMirrorTest(unittest.TestCase):
    """The dev install path carries the same constraint the chart renders.

    `make deploy` applies k8s-operator/config rather than the chart, and those
    manifests already mirror the chart's PDBs and its rollingUpdate fenceposts
    by hand. A spread constraint on one path and not the other means the audit
    finds on a dev install exactly what it found on a Helm one.
    """

    def _multi_replica_deployments(self):
        for path in sorted(KUSTOMIZE_DIR.rglob("*.yaml*")):
            for chunk in _manifests(path.read_text()):
                if not re.search(r"^kind: Deployment\s*$", chunk, flags=re.MULTILINE):
                    continue
                match = re.search(r"^  replicas:\s*(\d+)\s*$", chunk, flags=re.MULTILINE)
                if not match or int(match.group(1)) <= MIN_REPLICAS_RENDERED:
                    continue
                yield path.relative_to(REPO_ROOT), chunk

    def test_every_multi_replica_deployment_spreads_across_nodes(self):
        found = list(self._multi_replica_deployments())
        self.assertTrue(found, f"found no multi-replica Deployment under {KUSTOMIZE_DIR.name}")
        for path, chunk in found:
            with self.subTest(manifest=str(path)):
                self.assertIn(
                    "topologySpreadConstraints:",
                    chunk,
                    f"{path} runs more than one replica with nothing keeping them off one "
                    "node, while the chart's copy of the same workload spreads them",
                )
                self.assertIn(f"maxSkew: {EXPECTED_MAX_SKEW}", chunk)
                self.assertIn(f"topologyKey: {EXPECTED_TOPOLOGY_KEY}", chunk)
                self.assertEqual(
                    set(_WHEN_UNSATISFIABLE.findall(chunk)), {EXPECTED_WHEN_UNSATISFIABLE}
                )
                self.assertRegex(chunk, rf"matchLabelKeys:\s*\n\s*-\s*{EXPECTED_MATCH_LABEL_KEY}\b")

    def test_each_constraint_selects_its_own_deployments_pods(self):
        for path, chunk in self._multi_replica_deployments():
            constraint = re.search(
                r"^      topologySpreadConstraints:\n(?:.*\n)*?"
                r"          labelSelector:\n            matchLabels:\n((?:              .*\n)+)",
                chunk,
                flags=re.MULTILINE,
            )
            with self.subTest(manifest=str(path)):
                self.assertIsNotNone(
                    constraint, f"could not read {path}'s topologySpread labelSelector"
                )
                self.assertEqual(
                    _labels_from_yaml(constraint.group(1)),
                    _labels_from_yaml(_selector_labels(chunk)),
                    f"{path}'s topologySpread selector does not match the Deployment's own "
                    "spec.selector, so the constraint counts some other population",
                )


if __name__ == "__main__":
    unittest.main()
