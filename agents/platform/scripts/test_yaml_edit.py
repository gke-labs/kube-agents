"""Tests for yaml_edit: real manifests, hostile inputs, and a bounded differential fuzz.

PyYAML is the oracle here only: every accepted edit must re-parse, under a full
YAML parser, to the original document with exactly the target value changed.
"""

import copy
import os
import random
import sys
import unittest
from typing import ClassVar

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import automation_rules  # noqa: E402
from fix_recipes import yaml_edit  # noqa: E402
from fix_recipes.yaml_edit import RefusedEdit, set_scalar  # noqa: E402

PDB = """\
# Budget for the web tier. Keep minAvailable below replicas.
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: web   # matches the Deployment
  namespace: shop
  labels:
    app.kubernetes.io/name: web
spec:
  minAvailable: 1  # raised by the no-pdb recipe
  selector:
    matchLabels:
      app: web
"""

DEPLOYMENT = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: "shop"
  annotations:
    team: 'payments'   # owner
spec:
  replicas: 3
  revisionHistoryLimit: 5
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 25%
  selector:
    matchLabels: {app: api}
  template:
    metadata:
      labels:
        app: api
    spec:
      # Pinned for the PCI scope.
      serviceAccountName: api
      automountServiceAccountToken: false
      containers:
      - name: api
        image: "us-docker.pkg.dev/shop/api:1.4.2"
        args: ["--port", "8080"]
        ports:
        - containerPort: 8080
          name: http
        env:
          - name: LOG_LEVEL
            value: info
          - name: GREETING
            value: 'hello, world'
        resources:
          requests:
            cpu: 100m
            memory: 128Mi
        readinessProbe:
          httpGet: {path: /healthz, port: http}
      - name: sidecar
        image: busybox
        command:
          - sh
          - -c
        livenessProbe:
          exec:
            command: [
              "true",
            ]
"""

MULTI = """\
# Two objects in one file.
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
data:
  config.yaml: |
    key: value
    # not a YAML comment, part of the block

  mode: fast
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: settings-pdb
spec:
  maxUnavailable: 1
  selector:
    matchLabels:
      app: settings
"""

HAND_EDITED = """\
apiVersion: apps/v1
kind:   StatefulSet
metadata:
    name: db      # four-space indent
    labels:
        tier: data
spec:
    replicas:    1
    serviceName: db
    podManagementPolicy: 'OrderedReady'
    template:
        spec:
            terminationGracePeriodSeconds: 30
            containers:
              -   name: db
                  image: postgres:16
                  env:
                  -   name: PGDATA
                      value: "/var/lib/postgresql/data#x"
            nodeSelector:
                cloud.google.com/gke-nodepool: pool-a


# trailing comment, two blank lines above
"""

NO_TRAILING_NEWLINE = "apiVersion: v1\nkind: Service\nmetadata:\n  name: s\nspec:\n  type: ClusterIP"

FIXTURES = {
    "pdb": PDB,
    "deployment": DEPLOYMENT,
    "multi": MULTI,
    "hand-edited": HAND_EDITED,
    "no-trailing-newline": NO_TRAILING_NEWLINE,
}


def set_in(data, path, value):
    out = copy.deepcopy(data)
    node = out
    for seg in path[:-1]:
        node = node[seg]
    node[path[-1]] = value
    return out


def changed_lines(before, after):
    a, b = before.split("\n"), after.split("\n")
    if len(a) != len(b):
        return None
    return [i for i, (x, y) in enumerate(zip(a, b)) if x != y]


class RealManifests(unittest.TestCase):
    CASES: ClassVar[list] = [
        # (fixture, doc, path, value, expected changed line text)
        ("pdb", 0, ["spec", "minAvailable"], 2, "  minAvailable: 2  # raised by the no-pdb recipe"),
        ("pdb", 0, ["metadata", "labels", "app.kubernetes.io/name"], "web2", "    app.kubernetes.io/name: web2"),
        ("deployment", 0, ["spec", "replicas"], 5, "  replicas: 5"),
        ("deployment", 0, ["spec", "strategy", "rollingUpdate", "maxSurge"], "50%", '      maxSurge: "50%"'),
        ("deployment", 0, ["metadata", "annotations", "team"], "billing", "    team: 'billing'   # owner"),
        (
            "deployment",
            0,
            ["spec", "template", "spec", "containers", 0, "image"],
            "us-docker.pkg.dev/shop/api:1.4.3",
            '        image: "us-docker.pkg.dev/shop/api:1.4.3"',
        ),
        (
            "deployment",
            0,
            ["spec", "template", "spec", "containers", 0, "env", 1, "value"],
            "it's",
            "            value: 'it''s'",
        ),
        (
            "deployment",
            0,
            ["spec", "template", "spec", "automountServiceAccountToken"],
            True,
            "      automountServiceAccountToken: true",
        ),
        (
            "deployment",
            0,
            ["spec", "template", "spec", "containers", 1, "command", 1],
            "-ec",
            '          - "-ec"',
        ),
        ("multi", 1, ["spec", "maxUnavailable"], 2, "  maxUnavailable: 2"),
        ("multi", 0, ["data", "mode"], "slow", "  mode: slow"),
        ("hand-edited", 0, ["spec", "replicas"], 3, "    replicas:    3"),
        ("hand-edited", 0, ["spec", "podManagementPolicy"], "Parallel", "    podManagementPolicy: 'Parallel'"),
        (
            "hand-edited",
            0,
            ["spec", "template", "spec", "containers", 0, "image"],
            "postgres:17",
            '                  image: "postgres:17"',
        ),
        ("no-trailing-newline", 0, ["spec", "type"], "NodePort", "  type: NodePort"),
    ]

    def test_edits_change_only_the_target_line(self):
        for name, doc, path, value, want in self.CASES:
            with self.subTest(fixture=name, path=path):
                before = FIXTURES[name]
                after = set_scalar(before, doc, path, value)
                diff = changed_lines(before, after)
                self.assertEqual(len(diff), 1, diff)
                self.assertEqual(after.split("\n")[diff[0]], want)
                self.assertEqual(before.endswith("\n"), after.endswith("\n"))
                docs_before = list(yaml.safe_load_all(before))
                docs_after = list(yaml.safe_load_all(after))
                expected = list(docs_before)
                expected[doc] = set_in(docs_before[doc], path, value)
                self.assertEqual(docs_after, expected)

    def test_same_value_is_a_no_op(self):
        self.assertEqual(set_scalar(PDB, 0, ["spec", "minAvailable"], 1), PDB)

    def test_deterministic(self):
        a = set_scalar(DEPLOYMENT, 0, ["spec", "replicas"], 4)
        b = set_scalar(DEPLOYMENT, 0, ["spec", "replicas"], 4)
        self.assertEqual(a, b)

    def test_quoting_is_chosen_so_the_type_survives(self):
        cases = [("yes", '"yes"'), ("1.0", '"1.0"'), ("", '""'), ("a b", '"a b"'), ('say "hi"', '"say \\"hi\\""')]
        for value, rendered in cases:
            with self.subTest(value=value):
                out = set_scalar(PDB, 0, ["spec", "selector", "matchLabels", "app"], value)
                self.assertIn(f"      app: {rendered}\n", out)
                self.assertEqual(yaml.safe_load(out)["spec"]["selector"]["matchLabels"]["app"], value)

    def test_int_replaces_quoted_string(self):
        out = set_scalar(DEPLOYMENT, 0, ["metadata", "annotations", "team"], 7)
        self.assertIn("    team: 7   # owner\n", out)


class CreateLeaf(unittest.TestCase):
    def test_creates_at_end_of_existing_mapping(self):
        out = set_scalar(PDB, 0, ["spec", "unhealthyPodEvictionPolicy"], "AlwaysAllow", create_missing_leaf=True)
        self.assertIn("      app: web\n  unhealthyPodEvictionPolicy: AlwaysAllow\n", out)
        self.assertEqual(yaml.safe_load(out)["spec"]["unhealthyPodEvictionPolicy"], "AlwaysAllow")

    def test_creates_inside_sequence_item_mapping(self):
        path = ["spec", "template", "spec", "containers", 0, "imagePullPolicy"]
        out = set_scalar(DEPLOYMENT, 0, path, "Always", create_missing_leaf=True)
        self.assertIn("            memory: 128Mi\n        readinessProbe:", out)
        self.assertIn("          httpGet: {path: /healthz, port: http}\n        imagePullPolicy: Always\n", out)
        self.assertEqual(yaml.safe_load(out)["spec"]["template"]["spec"]["containers"][0]["imagePullPolicy"], "Always")

    def test_inserts_before_trailing_comments(self):
        out = set_scalar(HAND_EDITED, 0, ["spec", "minReadySeconds"], 10, create_missing_leaf=True)
        self.assertTrue(out.endswith("pool-a\n    minReadySeconds: 10\n\n\n# trailing comment, two blank lines above\n"))

    def test_file_without_trailing_newline(self):
        out = set_scalar(NO_TRAILING_NEWLINE, 0, ["spec", "sessionAffinity"], "None", create_missing_leaf=True)
        self.assertTrue(out.endswith("  type: ClusterIP\n  sessionAffinity: None"))

    def test_existing_key_is_set_not_duplicated(self):
        out = set_scalar(PDB, 0, ["spec", "minAvailable"], 2, create_missing_leaf=True)
        self.assertEqual(out, set_scalar(PDB, 0, ["spec", "minAvailable"], 2))

    def test_create_refusals(self):
        cases = [
            (PDB, ["spec", "missing", "leaf"], "path-missing"),  # no intermediate mappings
            (PDB, ["spec", "selector", "matchLabels", "yes"], "bad-path"),  # key YAML reads as bool
            (PDB, ["spec", "selector", "matchLabels", "a b"], "bad-path"),
            (MULTI, ["data", "extra"], None),  # last entry is plain; allowed
            ("a: 1\nb: |\n  text\n", ["c"], "block-scalar"),
            (DEPLOYMENT, ["spec", "selector", "matchLabels", "tier"], "flow-on-path"),
        ]
        for text, path, reason in cases:
            with self.subTest(path=path):
                if reason is None:
                    set_scalar(text, 0, path, "v", create_missing_leaf=True)
                    continue
                with self.assertRaises(RefusedEdit) as cm:
                    set_scalar(text, 0, path, "v", create_missing_leaf=True)
                self.assertEqual(cm.exception.reason, reason)

    def test_missing_leaf_refused_without_create(self):
        with self.assertRaises(RefusedEdit) as cm:
            set_scalar(PDB, 0, ["spec", "maxUnavailable"], 1)
        self.assertEqual(cm.exception.reason, "path-missing")


class Refusals(unittest.TestCase):
    # (name, text, doc, path, value, reason)
    CASES: ClassVar[list] = [
        ("anchor", "a: &x 1\nb: 2\n", 0, ["b"], 3, "anchor"),
        ("alias", "a: 1\nb: *x\n", 0, ["a"], 3, "alias"),
        ("tag", "a: !!str 1\nb: 2\n", 0, ["b"], 3, "tag"),
        ("anchor on item", "a:\n- &x 1\nb: 2\n", 0, ["b"], 3, "anchor"),
        ("anchor in flow", "a: [&x 1, *x]\nb: 2\n", 0, ["b"], 3, "anchor"),
        ("alias in flow", "a: {k: *x}\nb: 2\n", 0, ["b"], 3, "alias"),
        ("tag in flow", "a: [!!int 1]\nb: 2\n", 0, ["b"], 3, "tag"),
        ("merge key", "a:\n  <<: {x: 1}\n  x: 2\n", 0, ["a", "x"], 3, "merge-key"),
        ("flow on path", "a: {b: 1}\n", 0, ["a", "b"], 2, "flow-on-path"),
        ("flow seq on path", "a: [1, 2]\n", 0, ["a", 0], 2, "flow-on-path"),
        ("flow target", "a: [1, 2]\n", 0, ["a"], 2, "flow-on-path"),
        ("literal block", "a: |\n  x\n", 0, ["a"], "y", "block-scalar"),
        ("folded block", "a: >-\n  x\n", 0, ["a"], "y", "block-scalar"),
        ("multi-line plain", "a: one\n  two\nb: 1\n", 0, ["b"], 2, "multi-line-scalar"),
        ("multi-line quoted", 'a: "one\n  two"\nb: 1\n', 0, ["b"], 2, "multi-line-scalar"),
        ("scalar on own line", "a:\n  one\n", 0, ["a"], 2, "multi-line-scalar"),
        ("helm template", "a: {{ .Values.x }}\n", 0, ["a"], 2, "template"),
        ("helm in comment", "a: 1 # {{ x }}\n", 0, ["a"], 2, "template"),
        ("tab indent", "a:\n\tb: 1\n", 0, ["a", "b"], 2, "tab"),
        ("tab in value", "a: x\ty\n", 0, ["a"], 2, "tab"),
        ("crlf", "a: 1\r\nb: 2\r\n", 0, ["a"], 2, "crlf"),
        ("bom", "\ufeffa: 1\n", 0, ["a"], 2, "bom"),
        ("nel", "a: 1\x85b: 2\n", 0, ["a"], 2, "control-char"),
        ("line separator", "a: 1\u2028b: 2\n", 0, ["a"], 2, "control-char"),
        ("nul", "a: 1\x00\n", 0, ["a"], 2, "control-char"),
        ("lone surrogate", "a: \udc80\n", 0, ["a"], 2, "control-char"),
        ("duplicate key", "a: 1\nb: 2\na: 3\n", 0, ["b"], 3, "duplicate-key"),
        ("duplicate quoted key", "a: 1\n'a': 3\n", 0, ["a"], 3, "duplicate-key"),
        ("duplicate nested", "x:\n  a: 1\n  a: 2\ny: 1\n", 0, ["y"], 3, "duplicate-key"),
        ("non-string key", "m:\n  yes: 1\n  a: 2\n", 0, ["m", "a"], 3, "non-string-key"),
        ("numeric key", "m:\n  1: x\n  a: 2\n", 0, ["m", "a"], 3, "non-string-key"),
        ("complex key", "? a\n: 1\n", 0, ["a"], 2, "unsupported-syntax"),
        ("directive", "%YAML 1.1\n---\na: 1\n", 0, ["a"], 2, "directive"),
        ("doc end marker", "a: 1\n...\n", 0, ["a"], 2, "doc-end-marker"),
        ("content after marker", "--- a: 1\n", 0, ["a"], 2, "unsupported-syntax"),
        ("root sequence", "- a: 1\n", 0, [0, "a"], 2, "unsupported-syntax"),
        ("indented root", "  a: 1\n", 0, ["a"], 2, "unsupported-syntax"),
        ("bad indentation", "a:\n    b: 1\n  c: 2\n", 0, ["a", "b"], 2, "unsupported-syntax"),
        ("colon in plain value", "a: b: c\n", 0, ["a"], 2, "unsupported-syntax"),
        ("unclosed flow", "a: [1,\nb: 2\n", 0, ["b"], 2, "unsupported-syntax"),
        ("api version", PDB, 0, ["apiVersion"], "v2", "protected-path"),
        ("kind", PDB, 0, ["kind"], "Pod", "protected-path"),
        ("name", PDB, 0, ["metadata", "name"], "x", "protected-path"),
        ("namespace", PDB, 0, ["metadata", "namespace"], "x", "protected-path"),
        ("namespace as tuple", PDB, 0, ("metadata", "namespace"), "x", "protected-path"),
        ("missing path", PDB, 0, ["spec", "nope", "x"], 1, "path-missing"),
        ("index out of range", DEPLOYMENT, 0, ["spec", "template", "spec", "containers", 5, "name"], "x", "path-missing"),
        ("negative index", DEPLOYMENT, 0, ["spec", "template", "spec", "containers", -1, "name"], "x", "path-missing"),
        ("index into mapping", PDB, 0, ["spec", 0], 1, "path-missing"),
        ("bool as index", DEPLOYMENT, 0, ["spec", "template", "spec", "containers", False], "x", "bad-path"),
        ("empty path", PDB, 0, [], 1, "bad-path"),
        ("target is mapping", PDB, 0, ["spec"], 1, "not-scalar"),
        ("target is null", "a:\nb: 1\n", 0, ["a"], 1, "not-scalar"),
        ("doc out of range", PDB, 1, ["spec", "minAvailable"], 2, "doc-index"),
        ("negative doc", PDB, -1, ["spec", "minAvailable"], 2, "doc-index"),
        ("multi-line value", PDB, 0, ["spec", "minAvailable"], "a\nb", "bad-value"),
        ("templated value", PDB, 0, ["spec", "minAvailable"], "{{ x }}", "bad-value"),
        ("float value", PDB, 0, ["spec", "minAvailable"], 1.5, "bad-value"),
        ("none value", PDB, 0, ["spec", "minAvailable"], None, "bad-value"),
        ("timestamp keys", "d:\n  2024-1-1T00:00:00Z: a\n  2024-01-01T00:00:00Z: b\n", 0, ["d", "2024-1-1T00:00:00Z"], "x", "non-string-key"),
        ("date key", "d:\n  2024-01-01: old\n", 0, ["d", "2024-01-01"], "x", "non-string-key"),
        ("go octal key", "d:\n  0o17: a\n  k: b\n", 0, ["d", "k"], "x", "non-string-key"),
        ("c1 control", "b: x\u0086y\n", 0, ["b"], "z", "control-char"),
        ("noncharacter", "b: x\uffffy\n", 0, ["b"], "z", "control-char"),
        ("plain value indicator", "a: 1\nb: =\n", 0, ["a"], 2, "unsupported-syntax"),
        ("plain merge value", "a: 1\nb: <<\n", 0, ["a"], 2, "unsupported-syntax"),
        ("value indicator in flow", "a: {k: =}\nb: 1\n", 0, ["b"], 2, "unsupported-syntax"),
        ("merge in flow", "a: [<<]\nb: 1\n", 0, ["b"], 2, "unsupported-syntax"),
        ("explicit block indent", "a: |2\n  x\n b: 1\nc: 2\n", 0, ["c"], 3, "unsupported-syntax"),
        ("leading blank out-indents", "a: |\n      \n  x\nc: 2\n", 0, ["c"], 3, "unsupported-syntax"),
        ("list item kind", "apiVersion: v1\nkind: List\nitems:\n- apiVersion: v1\n  kind: ConfigMap\n  metadata:\n    name: a\n", 0, ["items", 0, "kind"], "Secret", "protected-path"),
        ("list item name", "apiVersion: v1\nkind: List\nitems:\n- apiVersion: v1\n  kind: ConfigMap\n  metadata:\n    name: a\n", 0, ["items", 0, "metadata", "name"], "b", "protected-path"),
        ("int over int64", PDB, 0, ["spec", "minAvailable"], 2**63, "bad-value"),
        ("huge int", PDB, 0, ["spec", "minAvailable"], -(10**5000), "bad-value"),
        ("block scalar dedent", "data:\n  c: |\n      key: value\n    bad: 1\n  mode: fast\n", 0, ["data", "mode"], "x", "unsupported-syntax"),
        ("unknown escape", 'a: "\\s"\nb: 1\n', 0, ["b"], 2, "unsupported-syntax"),
        ("windows path escape", 'a: "C:\\Users\\app"\nb: 1\n', 0, ["b"], 2, "unsupported-syntax"),
        ("truncated unicode escape", 'a: "\\u12"\nb: 1\n', 0, ["b"], 2, "unsupported-syntax"),
        ("long plain key", "k" * 1025 + ": 1\nb: 1\n", 0, ["b"], 2, "unsupported-syntax"),
        ("long quoted key", '"' + "k" * 1025 + '": 1\nb: 1\n', 0, ["b"], 2, "unsupported-syntax"),
        ("oversized", "a: 1\n# " + "x" * (64 * 1024) + "\n", 0, ["a"], 2, "too-large"),
    ]

    def test_refusals(self):
        for name, text, doc, path, value, reason in self.CASES:
            with self.subTest(name):
                with self.assertRaises(RefusedEdit) as cm:
                    set_scalar(text, doc, path, value)
                self.assertEqual(cm.exception.reason, reason, str(cm.exception))

    def test_template_metadata_is_not_an_identity_field(self):
        path = ["spec", "template", "metadata", "labels", "app"]
        self.assertIn("        app: api2\n", set_scalar(DEPLOYMENT, 0, path, "api2"))

    def test_int64_bounds_accepted(self):
        out = set_scalar(PDB, 0, ["spec", "minAvailable"], 2**63 - 1)
        self.assertEqual(yaml.safe_load(out)["spec"]["minAvailable"], 2**63 - 1)

    def test_flow_collections_as_sequence_items_are_skipped(self):
        text = 'ports:\n- {name: http, containerPort: 8080}\n- ["a: b"]\nb: 1\n'
        self.assertEqual(set_scalar(text, 0, ["b"], 2), text.replace("b: 1", "b: 2"))

    def test_valid_escapes_are_accepted(self):
        text = 'a: "tab\\t nl\\n \\x41 \\u00e9 \\\\ \\" \\/"\nb: 1\n'
        yaml.safe_load(text)
        self.assertEqual(set_scalar(text, 0, ["b"], 2), text.replace("b: 1", "b: 2"))

    def test_refusal_is_a_value_error(self):
        self.assertTrue(issubclass(RefusedEdit, ValueError))

    def test_size_cap_matches_rules_file(self):
        self.assertEqual(yaml_edit.MAX_FILE_BYTES, automation_rules.MAX_FILE_BYTES)

    def test_exactly_at_cap_is_accepted(self):
        head = "a: 1\n# "
        text = head + "x" * (yaml_edit.MAX_FILE_BYTES - len(head) - 1) + "\n"
        self.assertEqual(len(text.encode()), yaml_edit.MAX_FILE_BYTES)
        set_scalar(text, 0, ["a"], 2)

    def test_hostile_constructs_in_another_document_are_refused(self):
        with self.assertRaises(RefusedEdit) as cm:
            set_scalar("a: &x 1\nb: *x\n---\nc: 1\n", 1, ["c"], 2)
        self.assertEqual(cm.exception.reason, "anchor")

    def test_opaque_values_off_the_path_are_skipped(self):
        text = 'a: |\n  b: *notalias\n  &x\nc: ["*", "&", "!"]\nd: 1\n'
        out = set_scalar(text, 0, ["d"], 2)
        self.assertEqual(out, text.replace("d: 1", "d: 2"))


class Documents(unittest.TestCase):
    def test_document_count_matches_pyyaml(self):
        cases = ["a: 1\n---\n", "---\n", "# c\n---\na: 1\n", "a: 1\n---\n---\nb: 2\n", "---\na: 1", MULTI, PDB]
        for text in cases:
            with self.subTest(text=text):
                n = len(list(yaml.safe_load_all(text)))
                lines = text.split("\n")
                self.assertEqual(len(yaml_edit._documents(lines)), n)

    def test_only_the_named_document_changes(self):
        out = set_scalar(MULTI, 1, ["spec", "maxUnavailable"], 3)
        head, sep, _ = MULTI.rpartition("---\n")
        self.assertTrue(out.startswith(head + sep))


# --- bounded differential fuzz ----------------------------------------------

STRING_VALUES = [
    "web", "v1.2.3", "yes", "No", "null", "~", "0x1F", "1_000", "1:20", "1e3", ".inf", "", " ",
    "a b", "a: b", "#x", "x #y", "it's", 'say "hi"', "back\\slash", "-dash", "*star", "&amp", "!bang",
    "[x]", "{x}", "%p", "@at", "`tick`", "café", "\u00e9t\u00e9", "100m", "25%", "true", "False",
]
OTHER_VALUES = [0, 1, -1, 3, 65535, True, False]
HOSTILE = [
    "&a ", "*a", "!!str ", "!x ", "{", "}", "[", "]", ", ", "\t", "\r", "? ", "<<: ", "|", ">", "\ufeff",
    "{{ x }}", "---", "--- ", "...", "%YAML 1.2", "'", '"', " #", ": ", "- ", "-", "  ", "\\", "\x85", "\u2028",
]


def scalar_paths(node, prefix=()):
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str):
                yield from scalar_paths(v, prefix + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from scalar_paths(v, prefix + (i,))
    elif node is not None:
        yield prefix


def mapping_paths(node, prefix=()):
    if isinstance(node, dict):
        yield prefix
        for k, v in node.items():
            if isinstance(k, str):
                yield from mapping_paths(v, prefix + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from mapping_paths(v, prefix + (i,))


class DifferentialFuzz(unittest.TestCase):
    """Random edits: the editor either refuses, or PyYAML agrees with the result."""

    SEED = 20261119

    def assert_exact(self, before, after, doc, path, value, create):
        a, b = before.split("\n"), after.split("\n")
        if create:
            self.assertEqual(len(b), len(a) + 1)
            k = next(i for i in range(len(b)) if i >= len(a) or a[i] != b[i])
            self.assertEqual(b[:k] + b[k + 1 :], a)
        else:
            diff = changed_lines(before, after)
            self.assertIsNotNone(diff)
            self.assertLessEqual(len(diff), 1)
        docs_before = list(yaml.safe_load_all(before))
        docs_after = list(yaml.safe_load_all(after))
        expected = list(docs_before)
        expected[doc] = set_in(docs_before[doc], list(path), value)
        self.assertEqual(docs_after, expected)

    def test_random_edits_on_real_files(self):
        rng = random.Random(self.SEED)
        accepted = 0
        for _ in range(400):
            text = FIXTURES[rng.choice(sorted(FIXTURES))]
            docs = list(yaml.safe_load_all(text))
            doc = rng.randrange(len(docs))
            create = rng.random() < 0.25
            if create:
                paths = list(mapping_paths(docs[doc]))
                path = rng.choice(paths) + (rng.choice(["newKey", "extra.field", "x_1", "zz"]),)
            else:
                path = rng.choice(list(scalar_paths(docs[doc])))
            value = rng.choice(STRING_VALUES + OTHER_VALUES)
            try:
                out = set_scalar(text, doc, list(path), value, create_missing_leaf=create)
            except RefusedEdit:
                continue
            accepted += 1
            created = create and out.count("\n") != text.count("\n") or len(out.split("\n")) != len(text.split("\n"))
            with self.subTest(path=path, value=value):
                self.assert_exact(text, out, doc, path, value, created)
        self.assertGreater(accepted, 200, "fuzz accepted too few edits to mean anything")

    def test_hostile_mutations_refuse_or_stay_exact(self):
        rng = random.Random(self.SEED + 1)
        accepted = 0
        for _ in range(800):
            original = FIXTURES[rng.choice(sorted(FIXTURES))]
            docs = list(yaml.safe_load_all(original))
            doc = rng.randrange(len(docs))
            path = rng.choice(list(scalar_paths(docs[doc])))
            lines = original.split("\n")
            for _ in range(rng.randint(1, 3)):
                i = rng.randrange(len(lines))
                if rng.random() < 0.2:
                    lines.insert(i, lines[rng.randrange(len(lines))])  # duplicated line
                else:
                    j = rng.randint(0, len(lines[i]))
                    lines[i] = lines[i][:j] + rng.choice(HOSTILE) + lines[i][j:]
            text = "\n".join(lines)
            value = rng.choice(STRING_VALUES + OTHER_VALUES)
            try:
                out = set_scalar(text, doc, list(path), value)
            except RefusedEdit:
                continue
            accepted += 1
            with self.subTest(text=text, path=path, value=value):
                # Accepted means a full YAML parser must agree on the input and the output.
                self.assert_exact(text, out, doc, path, value, False)
        self.assertGreater(accepted, 50, "every mutation was refused; the fuzz is not exercising acceptance")


if __name__ == "__main__":
    unittest.main()
