"""`charts/kube-agents/values.schema.json` admits every key the chart's callers set.

Helm validates a chart's values against its schema on `lint`, `template`,
`install` and `upgrade`, and the Terraform helm provider goes through the same
path, so a key the schema does not know fails every one of them. That is the
point of the file -- a misspelt `platformAgent.harness.clustername` used to render
silently -- but it also means a key added to `values.yaml`, to the composition,
or to a CI `--set` without a matching schema entry turns the next `helm lint`
red. These tests find that before Helm does, and say which key.

Each caller's keys are resolved through the schema's `properties`. A path either
reaches a node the schema types, or stops at one it deliberately leaves open (an
object with no property list, or an array whose items are untyped); anything
else is a missing entry. The callers covered: `values.yaml` itself, the
`helm_release` values block in the full-install composition, the `--set` flags
in `hack/ci-deploy.sh`, `hack/check-image-inventory.sh`, `validate.yml` and
`scripts/release/publish_helm_chart.sh`, and the keys `upgrade.sh` retags. Where
a `helm` binary is present, the last class also asks Helm itself to refuse a
misspelt key, so the README's claim has a test that goes red when the schema
stops being enforced.
"""

import json
import pathlib
import re
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_SCHEMA_PATH = _CHART / "values.schema.json"
_VALUES_PATH = _CHART / "values.yaml"
_COMPOSITION_MAIN = _REPO_ROOT / "terraform" / "examples" / "full-install" / "main.tf"
_SET_FLAG_SOURCES = (
    _REPO_ROOT / "hack" / "ci-deploy.sh",
    _REPO_ROOT / "hack" / "check-image-inventory.sh",
    _REPO_ROOT / ".github" / "workflows" / "validate.yml",
    _REPO_ROOT / "scripts" / "release" / "publish_helm_chart.sh",
)
_UPGRADE_SCRIPT = _REPO_ROOT / "upgrade.sh"
# `helm_retag "a.b.tag" "c.d.tag"` in upgrade.sh: each argument becomes a
# `--set <key>=<tag>` the regex below cannot see, so the call sites are read instead.
_RETAG_CALL_RE = re.compile(r'helm_retag((?:\s+"[A-Za-z][A-Za-z0-9_.]*")+)')
_RETAG_KEY_RE = re.compile(r'"([^"]+)"')

# The three values the chart requires, as validate.yml passes them.
_REQUIRED_SET_FLAGS = (
    "--set",
    "platformAgent.harness.clusterName=ci-cluster",
    "--set",
    "platformAgent.harness.location=us-central1",
    "--set",
    "platformAgent.harness.projectId=ci-project",
)
_HELM_TIMEOUT_SECONDS = 120

_DRAFT_07 = "http://json-schema.org/draft-07/schema#"
# The marker a list index becomes in a dotted path, so `--set a[0].b` and a
# `for` expression in HCL resolve through the schema's `items` the same way.
_ITEM = "[]"

# `--set`, `--set-string` and `--set-file` flags, with or without a quote
# before the key. `\.` is an escaped dot inside a key, which Helm keeps as part
# of one segment (the annotation keys `iam\.gke\.io/...`).
_SET_FLAG_RE = re.compile(r"--set(?:-string|-file)?\s+[\"']?([A-Za-z](?:\\\.|[A-Za-z0-9_.\[\]/-])*)=")
_SET_SEGMENT_RE = re.compile(r"(?:\\\.|[^.])+")
_SET_INDEX_RE = re.compile(r"\[\d+\]")
_COMMENT_LINE_RE = re.compile(r"(?m)^\s*#.*$")

# HCL, as much of it as the composition's values block uses. An identifier
# followed by `=` (not `==`, `!=`, `<=`, `>=`, nor the `=>` of a `for` map
# expression) is a map key; `{` opens an object, `[` a list or an index, `(` a
# call or a ternary's parentheses.
_HCL_COMPOSITION_RESOURCE = 'resource "helm_release" "kube_agents"'
_HCL_VALUES_OPENER = "yamlencode({"
_HCL_KEY_RE = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z_][A-Za-z0-9_]*)\s*=(?![=>])")
_HCL_OPENERS = "{[("
_HCL_CLOSERS = "}])"

# Python's YAML scalar types against the JSON Schema type names a leaf may carry.
_YAML_TO_SCHEMA_TYPES = {
    bool: {"boolean"},
    int: {"integer", "number"},
    float: {"number"},
    str: {"string"},
    type(None): {"null"},
}


def _load_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text())


def _resolve(schema: dict, path: tuple[str, ...]) -> dict | None:
    """The schema node at `path`, or None where the schema stops typing.

    Raises KeyError naming the offending segment when a closed object has no
    property for it, which is the failure these tests exist to report.
    """
    node = schema
    for depth, segment in enumerate(path):
        if segment == _ITEM:
            items = node.get("items")
            if not items:
                return None
            node = items
            continue
        properties = node.get("properties")
        if properties is None:
            return None
        if segment in properties:
            node = properties[segment]
            continue
        if node.get("additionalProperties", True) is False:
            raise KeyError(".".join(path[: depth + 1]))
        return None
    return node


def _values_leaves(value, prefix: tuple[str, ...] = ()):
    """Every (path, scalar) pair in a values tree; an empty map or list is a leaf."""
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _values_leaves(child, prefix + (str(key),))
    elif isinstance(value, list) and value:
        for child in value:
            yield from _values_leaves(child, prefix + (_ITEM,))
    else:
        yield prefix, value


def _set_flag_paths(text: str) -> set[tuple[str, ...]]:
    paths = set()
    for match in _SET_FLAG_RE.finditer(_COMMENT_LINE_RE.sub("", text)):
        key = _SET_INDEX_RE.sub("." + _ITEM, match.group(1))
        segments = tuple(seg.replace("\\.", ".") for seg in _SET_SEGMENT_RE.findall(key))
        paths.add(segments)
    return paths


def _strip_hcl_comments_and_strings(text: str) -> str:
    """Blank out `#` comments and string literals, keeping every other character.

    Lengths are preserved so brace depth and key positions are unaffected. A
    quoted map key (`"iam.gke.io/gcp-service-account" = ...`) therefore
    disappears, which is what leaves it out of the key walk: those sit under
    objects the schema leaves open.
    """
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "#":
            end = text.find("\n", i)
            end = len(text) if end == -1 else end
            out.append(" " * (end - i))
            i = end
        elif ch == '"':
            j = i + 1
            while j < len(text) and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(" " * (j + 1 - i))
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _composition_value_paths(text: str) -> set[tuple[str, ...]]:
    """Every dotted key the composition's first values document assigns."""
    start = text.index(_HCL_COMPOSITION_RESOURCE)
    start = text.index(_HCL_VALUES_OPENER, start) + len(_HCL_VALUES_OPENER) - 1
    body = _strip_hcl_comments_and_strings(text[start:])
    # Each frame: [opener, path component or None, last key assigned inside it].
    # An object takes the key it is assigned to (through `merge(` and a
    # ternary); an object inside a list literal is an item; a list literal
    # takes its key; an index expression like `x[key]` takes nothing.
    frames: list[list] = []
    paths: set[tuple[str, ...]] = set()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch in _HCL_OPENERS:
            name = None
            if ch == "{":
                enclosing = next((f for f in reversed(frames) if f[0] != "("), None)
                if enclosing is None:
                    name = None
                elif enclosing[0] == "[":
                    name = _ITEM
                else:
                    name = _pending_key(frames)
            elif ch == "[" and body[:i].rstrip().endswith("="):
                name = _pending_key(frames)
            frames.append([ch, name, None])
            i += 1
            continue
        if ch in _HCL_CLOSERS:
            frames.pop()
            if not frames:
                break
            i += 1
            continue
        match = _HCL_KEY_RE.match(body, i)
        if match and frames and frames[-1][0] == "{":
            frames[-1][2] = match.group(1)
            paths.add(tuple(f[1] for f in frames if f[1]) + (match.group(1),))
            i = match.end()
            continue
        i += 1
    return paths


def _pending_key(frames: list) -> str | None:
    """The key whose value the next `{` belongs to, looking through call parentheses."""
    for frame in reversed(frames):
        if frame[0] == "{":
            return frame[2]
    return None


class SchemaShapeTest(unittest.TestCase):
    def test_schema_is_draft_07_and_closed_at_the_top(self) -> None:
        schema = _load_schema()
        self.assertEqual(schema.get("$schema"), _DRAFT_07)
        self.assertEqual(schema.get("type"), "object")
        self.assertIs(schema.get("additionalProperties"), False)

    def test_top_level_keys_match_values_yaml_exactly(self) -> None:
        schema = _load_schema()
        values = yaml.safe_load(_VALUES_PATH.read_text())
        self.assertEqual(set(schema["properties"]), set(values))

    def test_every_typed_object_is_closed(self) -> None:
        """An object with a property list also refuses unknown keys.

        Typing some keys and leaving the object open would catch a wrong type
        but wave a misspelling through, which is the failure the schema exists
        to stop.
        """
        open_typed = []

        def walk(node, path):
            if not isinstance(node, dict):
                return
            properties = node.get("properties")
            if properties is not None and node.get("additionalProperties", True) is not False:
                open_typed.append(".".join(path) or "<root>")
            for key, child in (properties or {}).items():
                walk(child, path + (key,))
            if isinstance(node.get("items"), dict):
                walk(node["items"], path + (_ITEM,))

        walk(_load_schema(), ())
        self.assertEqual(open_typed, [])


class ValuesYamlTest(unittest.TestCase):
    def test_every_default_resolves_and_matches_its_type(self) -> None:
        schema = _load_schema()
        values = yaml.safe_load(_VALUES_PATH.read_text())
        for path, default in _values_leaves(values):
            with self.subTest(path=".".join(path)):
                node = _resolve(schema, path)
                if node is None or "type" not in node:
                    continue
                declared = node["type"]
                declared = {declared} if isinstance(declared, str) else set(declared)
                if isinstance(default, (dict, list)):
                    expected = {"object"} if isinstance(default, dict) else {"array"}
                else:
                    expected = _YAML_TO_SCHEMA_TYPES[type(default)]
                self.assertTrue(
                    declared & expected,
                    f"values.yaml default {default!r} is not one of the schema types {sorted(declared)}",
                )


class CallerKeysTest(unittest.TestCase):
    def test_composition_values_resolve(self) -> None:
        paths = _composition_value_paths(_COMPOSITION_MAIN.read_text())
        # A guard against the walker silently matching nothing.
        self.assertIn(("platformAgent", "harness", "clusterName"), paths)
        self.assertIn(("global", "imagePullSecrets"), paths)
        self.assertIn(("platformAgent", "security", "scopedServiceAccounts", _ITEM, "projectId"), paths)
        schema = _load_schema()
        for path in sorted(paths):
            with self.subTest(path=".".join(path)):
                _resolve(schema, path)

    def test_set_flags_in_ci_and_release_scripts_resolve(self) -> None:
        schema = _load_schema()
        for source in _SET_FLAG_SOURCES:
            paths = _set_flag_paths(source.read_text())
            self.assertIn(("platformAgent", "harness", "clusterName"), paths, source.name)
            for path in sorted(paths):
                with self.subTest(source=source.name, path=".".join(path)):
                    _resolve(schema, path)


    def test_upgrade_retag_keys_resolve(self) -> None:
        schema = _load_schema()
        keys = set()
        for call in _RETAG_CALL_RE.finditer(_UPGRADE_SCRIPT.read_text()):
            keys.update(_RETAG_KEY_RE.findall(call.group(1)))
        self.assertIn("operator.image.tag", keys)
        for key in sorted(keys):
            with self.subTest(key=key):
                _resolve(schema, tuple(key.split(".")))


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class HelmEnforcesSchemaTest(unittest.TestCase):
    """Helm itself refuses what the schema refuses.

    The shape tests above read the file; this one proves Helm loads it, which is
    the README's claim. A `.helmignore` line or a rename that stopped the schema
    shipping would leave every other test here green.
    """

    @staticmethod
    def _template(*extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED_SET_FLAGS, *extra],
            capture_output=True,
            text=True,
            check=False,
            timeout=_HELM_TIMEOUT_SECONDS,
        )

    def test_the_required_values_alone_render(self) -> None:
        proc = self._template()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_misspelt_key_fails_with_its_path(self) -> None:
        proc = self._template("--set", "platformAgent.harness.clustername=x")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("clustername", proc.stderr)
        self.assertIn("/platformAgent/harness", proc.stderr)

    def test_a_wrong_type_fails_with_its_path(self) -> None:
        proc = self._template("--set", "operator.replicaCount=two")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("/operator/replicaCount", proc.stderr)

    def test_a_numeric_image_tag_renders(self) -> None:
        # Helm's --set reads an all-digit value as an integer; a date-stamped
        # tag must not be refused on type.
        proc = self._template("--set", "operator.image.tag=20260913")
        self.assertEqual(proc.returncode, 0, proc.stderr)


class ResolverTest(unittest.TestCase):
    """The resolver itself refuses what the schema refuses, so a green run means something."""

    def test_unknown_key_under_a_closed_object_is_reported(self) -> None:
        with self.assertRaises(KeyError) as ctx:
            _resolve(_load_schema(), ("platformAgent", "harness", "clustername"))
        self.assertIn("platformAgent.harness.clustername", str(ctx.exception))

    def test_open_objects_and_untyped_items_stop_the_walk(self) -> None:
        schema = _load_schema()
        self.assertIsNone(_resolve(schema, ("platformAgent", "annotations", "anything")))
        self.assertIsNone(_resolve(schema, ("global", "imagePullSecrets", _ITEM, "name")))
        self.assertIsNone(_resolve(schema, ("platformAgent", "harness", "tuning", "platform", "maxTurns")))

    def test_hcl_walker_skips_for_expression_arrows(self) -> None:
        paths = _composition_value_paths(
            'resource "helm_release" "kube_agents" {\n  values = [yamlencode({\n'
            "    platformAgent = { integration = { slack = { for k, v in var.s : k => v } } }\n"
            "  })]\n}\n"
        )
        self.assertEqual(
            paths,
            {("platformAgent",), ("platformAgent", "integration"), ("platformAgent", "integration", "slack")},
        )

    def test_set_flag_parser_keeps_escaped_dots_in_one_segment(self) -> None:
        paths = _set_flag_paths(
            '--set-string "platformAgent.security.serviceAccountAnnotations.iam\\.gke\\.io/x=y" '
            "--set global.imagePullSecrets[0].name=regcred"
        )
        self.assertIn(
            ("platformAgent", "security", "serviceAccountAnnotations", "iam.gke.io/x"), paths
        )
        self.assertIn(("global", "imagePullSecrets", _ITEM, "name"), paths)


if __name__ == "__main__":
    unittest.main()
