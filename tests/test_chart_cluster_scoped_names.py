"""Every cluster-scoped RBAC and admission object the chart renders is named after the release.

The drift detector sends an upgrade's own writes to one digest instead of a
card each (`ownRelease.wrote`, k8s-operator/cmd/drift-detector/ownrelease.go).
It cannot read cluster-scoped RBAC or admission objects, so it tells the
release's own by name: the release name, then a hyphen. A chart object in those
two API groups without that prefix is carded on every upgrade, and each card
spends one of the day's drift alerts.

Rendered under the release name the installer uses, because the admission
policy names are literals that start with it.

Run:
  python3 -m unittest discover -s tests -p 'test_chart_cluster_scoped_names.py' -v
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _go_constants import go_string  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
_CHART = REPO_ROOT / "charts" / "kube-agents"
_INSTALLER = REPO_ROOT / "scripts" / "installer" / "installer_common.sh"
_RELEASE_CONSTANT = 'readonly KUBE_AGENTS_HELM_RELEASE="'
_NAMESPACE = "kubeagents-system"
_SETS = (
    "platformAgent.harness.clusterName=ci-cluster",
    "platformAgent.harness.location=us-central1",
    "platformAgent.harness.projectId=ci-project",
    # Every opt-in workload on, so its objects are in the render.
    "webConsole.enabled=true",
    "operator.webhooks.enabled=true",
    "hindsight.enabled=true",
    "githubMinter.enabled=true",
    "githubMinter.org=example-org",
    "githubMinter.repo=example-repo",
)
_DETECTOR_RULE = REPO_ROOT / "k8s-operator" / "cmd" / "drift-detector" / "ownrelease.go"
# Read from the detector, so that a group it starts or stops matching by name
# changes what this test holds the chart to.
_GROUPS = (go_string(_DETECTOR_RULE, "rbacGroup"), go_string(_DETECTOR_RULE, "admissionGroup"))
# The namespaced kinds of those groups; the detector matches these by namespace.
_NAMESPACED_KINDS = ("Role", "RoleBinding")
_SEPARATOR = go_string(_DETECTOR_RULE, "releaseNameSeparator")


def _installer_release_name() -> str:
    for line in _INSTALLER.read_text().splitlines():
        if line.startswith(_RELEASE_CONSTANT):
            return line[len(_RELEASE_CONSTANT) :].rstrip('"')
    raise AssertionError(f"{_INSTALLER} no longer declares KUBE_AGENTS_HELM_RELEASE")


def _cluster_scoped(release: str) -> list[tuple[str, str]]:
    """(kind, name) of every rendered cluster-scoped object in the two groups."""
    args = ["helm", "template", release, str(_CHART), "--namespace", _NAMESPACE, "--kube-version", "1.33.0"]
    for setting in _SETS:
        args += ["--set", setting]
    rendered = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    found = []
    for doc in yaml.safe_load_all(rendered):
        if not isinstance(doc, dict):
            continue
        group = str(doc.get("apiVersion", "")).split("/")[0]
        if group in _GROUPS and doc.get("kind") not in _NAMESPACED_KINDS:
            found.append((doc["kind"], doc["metadata"]["name"]))
    return found


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class ClusterScopedNamesTest(unittest.TestCase):
    def test_every_cluster_scoped_rbac_and_admission_object_starts_with_the_release_name(self):
        release = _installer_release_name()
        objects = _cluster_scoped(release)
        kinds = {kind for kind, _ in objects}
        # Not vacuous: the render holds the kinds an upgrade was seen to write.
        for kind in ("ClusterRole", "ClusterRoleBinding", "ValidatingAdmissionPolicy", "ValidatingWebhookConfiguration"):
            self.assertIn(kind, kinds, f"the render holds no {kind}; this test no longer covers what the chart ships")
        unnamed = [f"{kind}/{name}" for kind, name in objects if not name.startswith(release + _SEPARATOR)]
        self.assertEqual(
            unnamed,
            [],
            f"cluster-scoped objects not named '{release}{_SEPARATOR}...': the drift detector cannot tell them "
            "as the release's own, so each upgrade gets a drift card for each",
        )


if __name__ == "__main__":
    unittest.main()
