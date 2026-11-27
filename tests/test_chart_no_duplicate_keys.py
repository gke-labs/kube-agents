"""The rendered chart carries no mapping key twice, across its feature toggles.

A label written by hand above `include "kube-agents.labels"` renders the
key twice in one mapping when the helper emits it too. PyYAML's default
loaders keep the last value and say nothing, so a render test that uses
`yaml.safe_load_all` passes. A strict YAML parser (such as Flux) refuses the
manifest.

This test loads every rendered document with a loader that refuses a
duplicate key, renders `_VALUE_SETS` covering every document-rendering
template in `charts/kube-agents/templates/`, and reports both the template
path and `<kind>/<metadata.name>` on any duplicate key.

Run: python3 -m unittest discover -s tests -p 'test_chart_no_duplicate_keys.py' -v
"""

import functools
import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_TEMPLATES_DIR = _CHART / "templates"
_MAKEFILE = _REPO_ROOT / "Makefile"
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
_WEBHOOKS_CERT_MANAGER = [
    "--set", "operator.webhooks.enabled=true",
    "--set", "operator.webhooks.certManager.enabled=true",
]
_WEBHOOKS_EXTERNAL_CERT = [
    "--set", "operator.webhooks.enabled=true",
    "--set", "operator.webhooks.certManager.enabled=false",
]
_GITHUB_MINTER_GHES = [
    "--set", "githubMinter.enabled=true",
    "--set", "githubMinter.org=acme",
    "--set", "githubMinter.repo=acme/infra",
    "--set-string", "githubMinter.appId=12345",
    "--set", "platformAgent.integration.forges[0].name=github",
    "--set", "platformAgent.integration.forges[0].provider=github",
    "--set", "platformAgent.integration.forges[0].host=ghe.example.com",
    "--set", "platformAgent.integration.forges[0].namespace=acme",
    "--set", "platformAgent.integration.forges[0].caBundleRef.name=ghe-ca",
    "--set", "platformAgent.integration.forges[0].caBundleRef.key=ca.crt",
    "--set", "platformAgent.integration.repositories[0].forge=github",
    "--set", "platformAgent.integration.repositories[0].repository=infra",
    "--set", "platformAgent.integration.repositories[0].role=gitops",
]
_FULL_INTEGRATIONS_AND_MONITORING = [
    *_WEB_CONSOLE_ENVIRONMENT,
    *_BOTH_PROFILES,
    "--set", "profiles.fleetAudit.enabled=true",
    *_HINDSIGHT,
    "--set", "plugins.pubsubPlatform.enabled=true",
    "--set", "plugins.stockoutInvestigator.enabled=true",
    "--set", "platformAgent.credentials.create=true",
    "--set", "platformAgent.credentials.data.API_SERVER_KEY=test-api-key",
    "--set", "platformAgent.credentials.data.SANDBOX_SSH_PUBLIC_KEY=ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest",
    "--set", "platformAgent.deployment.credentialProxy.exec.replicas=2",
    "--set", "platformAgent.healthRules.enabled=true",
    "--set", "platformAgent.podMonitoring=true",
    "--set", "platformAgent.serviceMonitor=true",
    "--set", "platformAgent.serviceMonitorLabels.release=prometheus",
    "--set", "platformAgent.vmPodScrape=true",
    "--set", "platformAgent.vmPodScrapeLabels.release=victoria",
    "--set", "platformAgent.monitoringScraperNamespace=monitoring",
    "--set", "operator.metrics.podMonitoring=true",
    "--set", "litellm.podMonitoring=true",
    "--set", "hindsight.podMonitoring=true",
    "--set", "platformAgent.integration.forges[0].name=gitlab",
    "--set", "platformAgent.integration.forges[0].provider=gitlab",
    "--set", "platformAgent.integration.forges[0].host=gitlab.example.com",
    "--set", "platformAgent.integration.forges[0].namespace=acme/platform",
    "--set", "platformAgent.integration.forges[0].credentialsRef.name=gitlab-token",
    "--set", "platformAgent.integration.forges[0].caBundleRef.name=gitlab-ca",
    "--set", "platformAgent.integration.forges[0].caBundleRef.key=ca.crt",
    "--set", "platformAgent.integration.repositories[0].forge=gitlab",
    "--set", "platformAgent.integration.repositories[0].repository=infra",
    "--set", "platformAgent.integration.repositories[0].role=gitops",
    "--set", "platformAgent.integration.slack.enabled=true",
    "--set", "platformAgent.integration.googleChat.enabled=true",
    "--set", "platformAgent.integration.teams.enabled=true",
    "--set", "platformAgent.integration.teams.tenantId=00000000-0000-0000-0000-000000000000",
    "--set", "platformAgent.integration.teams.allowAllUsers=true",
    "--set", "platformAgent.integration.teams.inbound.enabled=true",
    "--api-versions", "kubeagents.x-k8s.io/v1alpha1/Tier",
    "--api-versions", "monitoring.googleapis.com/v1/PodMonitoring",
    "--api-versions", "monitoring.googleapis.com/v1/Rules",
    "--api-versions", "monitoring.coreos.com/v1/ServiceMonitor",
    "--api-versions", "operator.victoriametrics.com/v1beta1/VMPodScrape",
]
_OPENAI_COMPATIBLE_AND_REPLAY_SHADOW = [
    "--set", "litellm.modelProvider=openai_compatible",
    "--set", "litellm.modelDefaultName=meta-llama/Llama-3.3-70B-Instruct",
    "--set", "litellm.openaiCompatible.apiBase=https://llm.internal.example.com:8443/v1",
    "--set", "litellm.openaiCompatible.egressCIDRs[0]=10.200.0.0/16",
    "--set", "litellm.openaiCompatible.caBundleSecretRef.name=llm-ca",
    "--set", "litellm.openaiCompatible.caBundleSecretRef.key=ca.crt",
    "--set", "litellm.envFromSecret=litellm-oauth",
    "--set", "litellm.redaction.enabled=true",
    "--set", "litellm.traceLogEnabled=true",
    "--set", "litellm.otel=true",
    "--set", "litellm.replayShadow.enabled=true",
    "--set", "litellm.replayShadow.image.repository=us-central1-docker.pkg.dev/pool/kube-agents/replay-proxy-shadow",
    "--set", "litellm.replayShadow.image.tag=v1",
    "--set", "litellm.replayShadow.job.kind=presubmit",
]
_VERTEX_WITHOUT_PLATFORM_AGENT = [
    "--set", "platformAgent.enabled=false",
    "--set", "litellm.modelProvider=vertex_ai",
    "--set", "litellm.vertex.credentialsSecretRef.name=vertex-sa",
    "--set", "litellm.vertex.credentialsSecretRef.key=sa.json",
    "--set", "litellm.vertex.projectSecretRef.name=vertex-project",
    "--set", "litellm.vertex.projectSecretRef.key=project-id",
    "--set", "litellm.vertex.sessionAffinity.enabled=true",
]
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
    "webhooks with cert-manager": _WEBHOOKS_CERT_MANAGER,
    "webhooks with external certificates": _WEBHOOKS_EXTERNAL_CERT,
    "github minter on GitHub Enterprise Server with CA": _GITHUB_MINTER_GHES,
    "gitlab, teams, plugins, profiles, tiers, health rules and monitoring": _FULL_INTEGRATIONS_AND_MONITORING,
    "openai_compatible with custom CA, redaction and replay shadow": _OPENAI_COMPATIBLE_AND_REPLAY_SHADOW,
    "vertex_ai without platformAgent (a2a-quota-chart-pods and static litellm-policy)": _VERTEX_WITHOUT_PLATFORM_AGENT,
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
_UNKNOWN_RESOURCE = "unknown resource"
_DOCUMENT_SEPARATOR = "---"
_KIND_PREFIX = "kind:"
_METADATA_KEY = "metadata:"
_NAME_PREFIX = "name:"
_METADATA_INDENT = "  "
# quota-preflight.yaml runs lookup-based ResourceQuota validation at install
# time and never emits a YAML document.
_NON_RENDERING_TEMPLATES = frozenset({"quota-preflight.yaml"})
_CHART_TEMPLATES_PREFIX = "kube-agents/templates/"
_MAKEFILE_TARGET_HEADER = "chart-check:"
_TEST_FILE_NAME = "test_chart_no_duplicate_keys.py"


class DuplicateKeyError(yaml.YAMLError):
    """A mapping carries the same key twice. `line` counts from 1."""

    def __init__(self, key, line):
        super().__init__(f"duplicate key {key!r} at line {line} of the render")
        self.key = key
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


@functools.lru_cache(maxsize=None)
def _render_cached(values):
    proc = subprocess.run(
        [_HELM, "template", _RELEASE, str(_CHART), *_REQUIRED, *values],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"helm template failed with {list(values)}: {proc.stderr}")
    return proc.stdout


def _render(values):
    return _render_cached(tuple(values))


def _template_at(text, line):
    """The template that produced `line` of the render, from helm's Source comment."""
    for candidate in reversed(text.splitlines()[:line]):
        if candidate.startswith(_SOURCE_PREFIX):
            return candidate[len(_SOURCE_PREFIX):]
    return _UNKNOWN_SOURCE


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _resource_at(text, line):
    """The `<kind>/<metadata.name>` of the document containing `line` (1-indexed)."""
    lines = text.splitlines()
    start = 0
    for idx in range(min(line, len(lines)) - 1, -1, -1):
        if lines[idx].rstrip() == _DOCUMENT_SEPARATOR:
            start = idx + 1
            break
    end = len(lines)
    for idx in range(start, len(lines)):
        if idx >= line and lines[idx].rstrip() == _DOCUMENT_SEPARATOR:
            end = idx
            break
    kind = None
    name = None
    in_metadata = False
    for raw in lines[start:end]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw.startswith(" "):
            in_metadata = False
            if stripped.startswith(_KIND_PREFIX):
                kind = _unquote(stripped[len(_KIND_PREFIX):])
            elif stripped == _METADATA_KEY:
                in_metadata = True
        elif in_metadata and raw.startswith(_METADATA_INDENT) and not raw.startswith(_METADATA_INDENT * 2):
            if stripped.startswith(_NAME_PREFIX):
                name = _unquote(stripped[len(_NAME_PREFIX):])
    if kind and name:
        return f"{kind}/{name}"
    if kind:
        return kind
    return _UNKNOWN_RESOURCE


def _format_failure(name, err, rendered):
    template = _template_at(rendered, err.line)
    resource = _resource_at(rendered, err.line)
    return f"render with {name}: {err}, in {template} ({resource})"


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

    def test_identifies_template_resource_and_duplicate_key_on_failure(self):
        manifest = (
            "---\n"
            "# Source: kube-agents/templates/web-console.yaml\n"
            "apiVersion: v1\n"
            "kind: Service\n"
            "metadata:\n"
            "  name: test-release-web-console\n"
            "---\n"
            "# Source: kube-agents/templates/web-console.yaml\n"
            "apiVersion: rbac.authorization.k8s.io/v1\n"
            "kind: ClusterRole\n"
            "metadata:\n"
            '  name: "test-release-web-console-environment"\n'
            "  labels:\n"
            "    app.kubernetes.io/instance: test-release\n"
            "    app.kubernetes.io/name: web-console\n"
            "    app.kubernetes.io/instance: test-release\n"
        )
        with self.assertRaises(DuplicateKeyError) as ctx:
            _load_strict(manifest)
        self.assertEqual(ctx.exception.key, _INSTANCE_LABEL)
        self.assertEqual(
            _template_at(manifest, ctx.exception.line),
            "kube-agents/templates/web-console.yaml",
        )
        self.assertEqual(
            _resource_at(manifest, ctx.exception.line),
            "ClusterRole/test-release-web-console-environment",
        )
        msg = _format_failure("web console", ctx.exception, manifest)
        self.assertIn("kube-agents/templates/web-console.yaml", msg)
        self.assertIn("ClusterRole/test-release-web-console-environment", msg)
        self.assertIn(_INSTANCE_LABEL, msg)

    def test_resource_at_ignores_indented_separator_inside_block_scalar(self):
        manifest = (
            "---\n"
            "# Source: kube-agents/templates/github-minter.yaml\n"
            "apiVersion: v1\n"
            "kind: ConfigMap\n"
            "metadata:\n"
            "  name: github-token-minter-config\n"
            "data:\n"
            "  default.yaml: |\n"
            "    ---\n"
            "    version: v2\n"
            "  duplicate: one\n"
            "  duplicate: two\n"
        )
        with self.assertRaises(DuplicateKeyError) as ctx:
            _load_strict(manifest)
        self.assertEqual(
            _resource_at(manifest, ctx.exception.line),
            "ConfigMap/github-token-minter-config",
        )

    def test_chart_check_target_runs_duplicate_key_suite(self):
        makefile = _MAKEFILE.read_text(encoding="utf-8")
        self.assertIn(_MAKEFILE_TARGET_HEADER, makefile)
        after = makefile.split(_MAKEFILE_TARGET_HEADER, 1)[1]
        recipe_lines = []
        for line in after.splitlines()[1:]:
            if line.startswith("\t"):
                recipe_lines.append(line)
            elif line.strip():
                break
        self.assertTrue(
            any(_TEST_FILE_NAME in line for line in recipe_lines),
            f"chart-check target in Makefile must run {_TEST_FILE_NAME}: {recipe_lines}",
        )


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
                    self.fail(_format_failure(name, err, rendered))
                self.assertTrue(docs, f"render with {name} produced no documents")

    def test_value_sets_cover_every_document_rendering_template(self):
        expected = {
            path.name
            for path in _TEMPLATES_DIR.glob("*.yaml")
            if path.name not in _NON_RENDERING_TEMPLATES
        }
        rendered_templates = set()
        for values in _VALUE_SETS.values():
            rendered = _render(values)
            for line in rendered.splitlines():
                if line.startswith(_SOURCE_PREFIX):
                    source = line[len(_SOURCE_PREFIX):].strip()
                    if source.startswith(_CHART_TEMPLATES_PREFIX):
                        rendered_templates.add(source[len(_CHART_TEMPLATES_PREFIX):])
        self.assertEqual(
            expected - rendered_templates,
            set(),
            f"chart templates not rendered by any _VALUE_SETS entry: {sorted(expected - rendered_templates)}",
        )

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
