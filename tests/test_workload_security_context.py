"""Tests that the LiteLLM gateway and the Hindsight store run unprivileged (#1510).

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching the other suites in this directory.

The pinned LiteLLM image declares `User: root`, and the pgvector image's
entrypoint starts as root before dropping to `postgres` itself, so a Deployment
or StatefulSet that sets no `securityContext` runs them as root on a writable
root filesystem. The operator, github-minter and the Hindsight API already
carry the house set — `runAsNonRoot`, a `RuntimeDefault` seccomp profile,
`allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`,
`capabilities.drop: [ALL]` — and nothing checks that the two workloads this
suite covers keep it. Each of them exists twice, in the chart template and in
the kustomize dev path `AGENTS.md` keeps in step with it, and the pair can
drift apart without any gate noticing:

    charts/kube-agents/templates/litellm.yaml
    k8s-operator/config/integrations/litellm/base/deployment.yaml
    charts/kube-agents/templates/hindsight.yaml
    k8s-operator/config/integrations/hindsight/postgresql.yaml
    k8s-operator/config/integrations/hindsight/api.yaml

The kustomize manifests are plain YAML and are parsed. The chart templates are
Go templates, so they are matched as text against the same expected blocks
rendered at the template's indentation, and — where `helm` is on PATH, as in
`test_litellm_redaction.py` — rendered with Hindsight enabled and checked as
objects, which is what ties the two halves together.

The read-only root is only safe with a writable path for what the process has
to write, so the volumes are part of the contract: an `emptyDir` at `/tmp` for
LiteLLM (with `HOME` pointing there, because uid 1000 has no passwd entry in
the image), and `emptyDir`s at `/var/run/postgresql` and `/tmp` for Postgres.
The Hindsight API container keeps `readOnlyRootFilesystem: false` on purpose;
only its pod-level fields are asserted here.
"""

import pathlib
import shutil
import subprocess
import unittest

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _ROOT / "charts" / "kube-agents"
_CHART_LITELLM = _CHART / "templates" / "litellm.yaml"
_CHART_HINDSIGHT = _CHART / "templates" / "hindsight.yaml"
_INTEGRATIONS = _ROOT / "k8s-operator" / "config" / "integrations"
_KUSTOMIZE_LITELLM = _INTEGRATIONS / "litellm" / "base" / "deployment.yaml"
_KUSTOMIZE_POSTGRESQL = _INTEGRATIONS / "hindsight" / "postgresql.yaml"
_KUSTOMIZE_API = _INTEGRATIONS / "hindsight" / "api.yaml"

_SECCOMP = {"type": "RuntimeDefault"}
_LITELLM_POD_CONTEXT = {
    "runAsNonRoot": True,
    "runAsUser": 1000,
    "runAsGroup": 1000,
    "fsGroup": 1000,
    "seccompProfile": _SECCOMP,
}
_POSTGRESQL_POD_CONTEXT = {
    "runAsNonRoot": True,
    "runAsUser": 999,
    "runAsGroup": 999,
    "fsGroup": 999,
    "fsGroupChangePolicy": "OnRootMismatch",
    "seccompProfile": _SECCOMP,
}
_HINDSIGHT_API_POD_CONTEXT = {
    "runAsNonRoot": True,
    "fsGroup": 1000,
    "seccompProfile": _SECCOMP,
}
_HARDENED_CONTAINER_CONTEXT = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "capabilities": {"drop": ["ALL"]},
}
# Volume name -> mount path of every emptyDir the container must be given.
_LITELLM_WRITABLE = {"tmp": "/tmp"}
_POSTGRESQL_WRITABLE = {"run": "/var/run/postgresql", "tmp": "/tmp"}
_LITELLM_HOME = "/tmp"

_LITELLM_CONTAINER = "litellm-container"
_POSTGRESQL_CONTAINER = "postgresql"
_HINDSIGHT_API_CONTAINER = "api"

# Where a block sits in the chart templates: the pod spec's children at six
# spaces, the container's at ten.
_POD_INDENT = 6
_CONTAINER_INDENT = 10

_HELM_ARGS = [
    "helm",
    "template",
    "test-release",
    str(_CHART),
    "--set-string",
    "platformAgent.harness.clusterName=test-cluster",
    "--set-string",
    "platformAgent.harness.location=us-central1",
    "--set-string",
    "platformAgent.harness.projectId=test-project",
    "--set",
    "hindsight.enabled=true",
    "-s",
    "templates/litellm.yaml",
    "-s",
    "templates/hindsight.yaml",
]


def _workload(docs, kind, name):
    """The one document of `kind` named `name`, or an AssertionError naming both."""
    matches = [
        d
        for d in docs
        if isinstance(d, dict)
        and d.get("kind") == kind
        and (d.get("metadata") or {}).get("name") == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one {kind} {name!r}, found {len(matches)}")
    return matches[0]


def _pod_spec(doc):
    return doc["spec"]["template"]["spec"]


def _container(pod_spec, name):
    for container in pod_spec.get("containers") or []:
        if container.get("name") == name:
            return container
    raise AssertionError(f"no container named {name!r}")


def _empty_dir_mounts(pod_spec, container):
    """Volume name -> mount path, for the container's mounts backed by an emptyDir.

    Both halves have to hold: a mount whose volume is a ConfigMap or a PVC is
    not a scratch directory, and a declared emptyDir nobody mounts is inert.
    """
    empty_dirs = {
        v["name"] for v in pod_spec.get("volumes") or [] if isinstance(v, dict) and "emptyDir" in v
    }
    return {
        m["name"]: m["mountPath"]
        for m in container.get("volumeMounts") or []
        if m.get("name") in empty_dirs
    }


def _env(container):
    return {e["name"]: e.get("value") for e in container.get("env") or [] if isinstance(e, dict)}


class _PrettierDumper(yaml.SafeDumper):
    """Indent sequence items under their key, as prettier formats the templates."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _indented(mapping, indent):
    """`mapping` as the YAML block the chart templates carry, at `indent` spaces.

    Key order is the insertion order of the dicts above, so the templates have
    to list the fields in that order too; a reordering is a test failure
    rather than a false pass, which is the cheaper of the two mistakes.
    """
    text = yaml.dump(mapping, Dumper=_PrettierDumper, sort_keys=False, default_flow_style=False)
    return "".join(" " * indent + line + "\n" for line in text.splitlines())


class _Assertions:
    """The per-workload checks, shared by the kustomize and the helm-render cases."""

    def assert_litellm(self, doc):
        pod_spec = _pod_spec(doc)
        container = _container(pod_spec, _LITELLM_CONTAINER)
        self.assertEqual(_LITELLM_POD_CONTEXT, pod_spec.get("securityContext"))
        self.assertEqual(_HARDENED_CONTAINER_CONTEXT, container.get("securityContext"))
        self.assertEqual(_LITELLM_WRITABLE, _empty_dir_mounts(pod_spec, container))
        self.assertEqual(
            _LITELLM_HOME,
            _env(container).get("HOME"),
            "HOME must point at the tmp emptyDir: uid 1000 has no passwd entry in "
            "the LiteLLM image, and the tokenizer and library caches follow HOME",
        )

    def assert_postgresql(self, doc):
        pod_spec = _pod_spec(doc)
        container = _container(pod_spec, _POSTGRESQL_CONTAINER)
        self.assertEqual(_POSTGRESQL_POD_CONTEXT, pod_spec.get("securityContext"))
        self.assertEqual(_HARDENED_CONTAINER_CONTEXT, container.get("securityContext"))
        self.assertEqual(_POSTGRESQL_WRITABLE, _empty_dir_mounts(pod_spec, container))

    def assert_hindsight_api(self, doc):
        pod_spec = _pod_spec(doc)
        # The container's own block is out of scope here; it must still exist
        # and still keep the api out of root, since the pod-level
        # runAsNonRoot below is what refuses an image that would run as 0.
        container = _container(pod_spec, _HINDSIGHT_API_CONTAINER)
        self.assertEqual(_HINDSIGHT_API_POD_CONTEXT, pod_spec.get("securityContext"))
        self.assertTrue((container.get("securityContext") or {}).get("runAsNonRoot"))


class KustomizeManifestsRunUnprivileged(_Assertions, unittest.TestCase):
    def test_litellm_base(self):
        docs = list(yaml.safe_load_all(_KUSTOMIZE_LITELLM.read_text()))
        self.assert_litellm(_workload(docs, "Deployment", "litellm"))

    def test_hindsight_postgresql(self):
        docs = list(yaml.safe_load_all(_KUSTOMIZE_POSTGRESQL.read_text()))
        self.assert_postgresql(_workload(docs, "StatefulSet", "hindsight-postgresql"))

    def test_hindsight_api(self):
        docs = list(yaml.safe_load_all(_KUSTOMIZE_API.read_text()))
        self.assert_hindsight_api(_workload(docs, "Deployment", "hindsight-api"))


class ChartTemplatesCarryTheSameBlocks(unittest.TestCase):
    """Text checks that run without helm, so the python-tests job covers the chart too."""

    def test_litellm_template(self):
        template = _CHART_LITELLM.read_text()
        self.assertIn(_indented({"securityContext": _LITELLM_POD_CONTEXT}, _POD_INDENT), template)
        self.assertIn(
            _indented({"securityContext": _HARDENED_CONTAINER_CONTEXT}, _CONTAINER_INDENT),
            template,
        )
        for name, path in _LITELLM_WRITABLE.items():
            self.assertIn(f"- name: {name}\n              mountPath: {path}\n", template)
            self.assertIn(f"- name: {name}\n          emptyDir: {{}}\n", template)
        self.assertIn(f"- name: HOME\n              value: {_LITELLM_HOME}\n", template)

    def test_hindsight_template(self):
        template = _CHART_HINDSIGHT.read_text()
        self.assertIn(
            _indented({"securityContext": _POSTGRESQL_POD_CONTEXT}, _POD_INDENT), template
        )
        self.assertIn(
            _indented({"securityContext": _HINDSIGHT_API_POD_CONTEXT}, _POD_INDENT), template
        )
        self.assertIn(
            _indented({"securityContext": _HARDENED_CONTAINER_CONTEXT}, _CONTAINER_INDENT),
            template,
        )
        for name, path in _POSTGRESQL_WRITABLE.items():
            self.assertIn(f"- name: {name}\n              mountPath: {path}\n", template)
            self.assertIn(f"- name: {name}\n          emptyDir: {{}}\n", template)


@unittest.skipUnless(shutil.which("helm"), "helm is not on PATH")
class RenderedChartRunsUnprivileged(_Assertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rendered = subprocess.run(_HELM_ARGS, capture_output=True, text=True, check=True)
        cls.docs = [d for d in yaml.safe_load_all(rendered.stdout) if d]

    def test_litellm(self):
        self.assert_litellm(_workload(self.docs, "Deployment", "litellm"))

    def test_hindsight_postgresql(self):
        self.assert_postgresql(_workload(self.docs, "StatefulSet", "hindsight-postgresql"))

    def test_hindsight_api(self):
        self.assert_hindsight_api(_workload(self.docs, "Deployment", "hindsight-api"))


if __name__ == "__main__":
    unittest.main()
