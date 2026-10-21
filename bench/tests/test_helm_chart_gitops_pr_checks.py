"""helm-chart-gitops-pr's pull request checks accept a Helm RootSync for the
chart at an exact version, and reject a range, another chart, or a form that
is not a RootSync; its reply check needs the reconciler's error named.

`content` reads the files a pull request adds (``_content_rejection`` over
path -> text). The reply check runs over ``_normalize_lines``.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from kube_agents_bench.verifiers import PullRequestContentCheck, _content_rejection, _normalize_lines

TASK = Path(__file__).resolve().parent.parent / "tasks" / "helm-chart-gitops-pr" / "task.yaml"
PATH = "clusters/gitops-target-1/namespaces/config-management-system/rootsync-sealed-secrets.yaml"

ROOTSYNC = """\
apiVersion: configsync.gke.io/v1beta1
kind: RootSync
metadata:
  name: sealed-secrets
  namespace: config-management-system
spec:
  sourceType: helm
  helm:
    repo: https://bitnami-labs.github.io/sealed-secrets
    chart: sealed-secrets
    version: {version}
    releaseName: sealed-secrets
    namespace: kube-system
    includeCRDs: true
    auth: none
"""


def _spec() -> list[dict]:
    return yaml.safe_load(TASK.read_text())["verification_spec"]


def _content_failures(files: dict[str, str]) -> list[str]:
    (pr,) = [c for c in _spec() if c["name"] == "one-pull-request-declares-the-chart"]
    checks = [PullRequestContentCheck(**c) for c in pr["check"]["content"]]
    return [r for r in (_content_rejection(c, files) for c in checks) if r]


def test_a_helm_rootsync_at_an_exact_version_passes():
    assert _content_failures({PATH: ROOTSYNC.format(version="2.17.3")}) == []


def test_a_range_latest_or_empty_version_fails():
    for version in ('">=2.17.0"', "latest", '""', "2.17.x"):
        assert _content_failures({PATH: ROOTSYNC.format(version=version)}), version


def test_a_mirror_or_a_rootsync_outside_config_management_system_fails():
    mirror = ROOTSYNC.format(version="2.17.3").replace(
        "https://bitnami-labs.github.io/sealed-secrets", "oci://registry-1.docker.io/bitnamicharts"
    )
    assert _content_failures({PATH: mirror})
    elsewhere = ROOTSYNC.format(version="2.17.3").replace("namespace: config-management-system", "namespace: default")
    assert _content_failures({PATH: elsewhere})


def test_another_chart_fails():
    text = ROOTSYNC.format(version="2.17.3").replace("chart: sealed-secrets", "chart: sealed-secrets-web")
    assert _content_failures({PATH: text})


def test_a_form_that_is_not_a_rootsync_fails():
    deployment = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: sealed-secrets\n"
    assert _content_failures({PATH.replace("rootsync-", "deploy-"): deployment})


def test_the_reply_check_needs_the_error_named():
    (check,) = [c["check"] for c in _spec() if c["name"] == "the-reply-names-the-sync-error"]

    def passes(report: str) -> bool:
        return any(re.search(p, _normalize_lines(report)) for p in check["any_of_patterns"])

    assert passes(
        "Opened PR #9 adding a Helm RootSync for sealed-secrets. The root `root-sync` is configured "
        "for that directory but fails to fetch the repository (KNV2004), so nothing installs until it can."
    )
    assert not passes("Opened PR #9 adding a Helm RootSync for sealed-secrets; Config Sync installs it after merge.")
    # A failure that is not the reconciler's, beside a promised install.
    assert not passes("I can't reach the chart repository; Config Sync installs it after merge.")
    assert not passes("If anything fails, Config Sync will retry the sync. It installs after merge.")
