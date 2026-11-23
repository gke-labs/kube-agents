"""The rendered chart carries no mapping key twice, for the value sets below.

A label written by hand above `include "kube-agents.labels"` renders the
key twice in one mapping when the helper emits it too. PyYAML's default
loaders keep the last value and say nothing, so a render test that uses
`yaml.safe_load_all` passes. A strict YAML parser, and Argo CD, refuse the
manifest.

This test loads every rendered document with a loader that refuses a
duplicate key. It covers the value sets in `_VALUE_SETS` only: a template
behind a toggle that no set turns on is not checked.

Run: python3 -m unittest discover -s tests -p 'test_chart_no_duplicate_keys.py' -v
"""

import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_HELM = shutil.which("helm")
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_WEB_CONSOLE_ENVIRONMENT = [
    "--set", "webConsole.enabled=true",
    "--set", "webConsole.environment.enabled=true",
]
_BOTH_PROFILES = [
    "--set", "profiles.tse.enabled=true",
    "--set", "profiles.cluster.enabled=true",
]
_HINDSIGHT = ["--set", "hindsight.enabled=true"]
# Each entry is one render. The name appears in the failure message.
_VALUE_SETS = {
    "defaults": [],
    "web console with environment": _WEB_CONSOLE_ENVIRONMENT,
    "web console with environment and both profiles": [
        *_WEB_CONSOLE_ENVIRONMENT, *_BOTH_PROFILES,
    ],
    "web console with environment, both profiles and hindsight": [
        *_WEB_CONSOLE_ENVIRONMENT, *_BOTH_PROFILES, *_HINDSIGHT,
    ],
}
_RELEASE = "test-release"
# The five objects the web console environment adds. Each one carried
# `app.kubernetes.io/instance` twice.
_ENVIRONMENT_OBJECTS = [
    ("ClusterRole", f"{_RELEASE}-web-console-environment"),
    ("ClusterRoleBinding", f"{_RELEASE}-web-console-environment"),
    ("Role", f"{_RELEASE}-web-console-environment"),
    ("RoleBinding", f"{_RELEASE}-web-console-environment"),
    ("ServiceAccount", f"{_RELEASE}-web-console"),
]
_INSTANCE_LABEL = "app.kubernetes.io/instance"
_YAML_MERGE_TAG = "tag:yaml.org,2002:merge"
# helm writes this comment above each document. It names the template.
_SOURCE_PREFIX = "# Source: "
_UNKNOWN_SOURCE = "unknown template"


class DuplicateKeyError(yaml.YAMLError):
    """A mapping carries the same key twice. `line` counts from 1."""

    def __init__(self, key, line):
        super().__init__(f"duplicate key {key!r} at line {line} of the render")
        self.line = line


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with a repeated key."""

    def construct_mapping(self, node, deep=False):
        # Keys are compared as written (resolved tag and text). A merge key
        # or a composite key is left to SafeLoader.
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == _YAML_MERGE_TAG or not isinstance(key_node, yaml.ScalarNode):
                continue
            key = (key_node.tag, key_node.value)
            if key in seen:
                raise DuplicateKeyError(key_node.value, key_node.start_mark.line + 1)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _render(values):
    proc = subprocess.run(
        [_HELM, "template", _RELEASE, str(_CHART), *_REQUIRED, *values],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"helm template failed with {values}: {proc.stderr}")
    return proc.stdout


def _template_at(text, line):
    """The template that produced `line` of the render, from helm's Source comment."""
    for candidate in reversed(text.splitlines()[:line]):
        if candidate.startswith(_SOURCE_PREFIX):
            return candidate[len(_SOURCE_PREFIX):]
    return _UNKNOWN_SOURCE


def _load_strict(text):
    return [doc for doc in yaml.load_all(text, Loader=_UniqueKeyLoader) if doc]


class UniqueKeyLoaderTest(unittest.TestCase):
    """The loader itself, so the render test cannot pass on a loader that accepts everything."""

    def test_refuses_a_repeated_key(self):
        with self.assertRaises(DuplicateKeyError):
            _load_strict("labels:\n  a: x\n  a: x\n")

    def test_refuses_a_repeated_key_in_a_nested_mapping(self):
        with self.assertRaises(DuplicateKeyError):
            _load_strict("metadata:\n  labels:\n    a: x\n    b: y\n    a: z\n")

    def test_accepts_a_key_that_overrides_a_merged_one(self):
        docs = _load_strict("base: &base\n  a: x\nuse:\n  <<: *base\n  a: y\n")
        self.assertEqual(docs[0]["use"]["a"], "y")

    def test_accepts_the_same_key_in_two_mappings(self):
        docs = _load_strict("one:\n  a: x\ntwo:\n  a: x\n---\none:\n  a: x\n")
        self.assertEqual(len(docs), 2)


@unittest.skipUnless(_HELM, "helm not on PATH")
class ChartNoDuplicateKeysTest(unittest.TestCase):
    """Render the chart and load each document with the strict loader."""

    def test_no_rendered_document_repeats_a_key(self):
        for name, values in _VALUE_SETS.items():
            with self.subTest(values=name):
                rendered = _render(values)
                try:
                    docs = _load_strict(rendered)
                except DuplicateKeyError as err:
                    self.fail(f"render with {name}: {err}, in {_template_at(rendered, err.line)}")
                self.assertTrue(docs, f"render with {name} produced no documents")

    def test_environment_objects_keep_the_instance_label(self):
        docs = _load_strict(_render(_WEB_CONSOLE_ENVIRONMENT))
        labels = {
            (doc["kind"], doc["metadata"]["name"]): doc["metadata"].get("labels", {})
            for doc in docs
        }
        for obj in _ENVIRONMENT_OBJECTS:
            with self.subTest(obj=obj):
                self.assertIn(obj, labels)
                self.assertEqual(labels[obj].get(_INSTANCE_LABEL), _RELEASE)


if __name__ == "__main__":
    unittest.main()
