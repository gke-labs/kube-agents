#!/usr/bin/env python3
"""The broker's /v1/exec route holds GCP metadata reads to the install's exclusions
for every caller: a describe of an excluded cluster never reaches the executor,
and a `logging read` reaches it with the exclusion clause on its filter."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import credential_proxy
import scope_exclude
from credential_proxy import CredentialProxyHandler
import test_credential_proxy_profile as profile_tests
from test_credential_proxy_profile import POD, POD_UID, TARGET, _StubAuthenticator

_Route = profile_tests.ProfileRouteTest


class _RouteHarness(unittest.TestCase):
    """ProfileRouteTest's live server, recording executor and helpers, without its
    test methods."""

    setUp = _Route.setUp
    _restore = staticmethod(_Route._restore)
    set_gate = _Route.set_gate
    post = _Route.post
    assert_refused = _Route.assert_refused


# Not left at module level, where the runner would collect it and its tests again.
del _Route

SHELL_SA = "system:serviceaccount:kubeagents-system:platform-agent"
OTHER = "other-cluster"
CLAUSE = f'NOT resource.labels.cluster_name=~"^({OTHER})$"'


class MetadataScopeRouteTest(_RouteHarness):
    """Run as the platform agent's shell caller, which no task confines."""

    def setUp(self):
        super().setUp()
        self.addCleanup(
            self._restore, "exclusions", "exclusions" in CredentialProxyHandler.__dict__,
            CredentialProxyHandler.__dict__.get("exclusions"),
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        scope_file = Path(directory.name) / "scope.json"
        scope_file.write_text(json.dumps({"exclude": {"projects": [], "clusters": [
            {"projectId": TARGET.project, "location": TARGET.location, "clusterName": OTHER}]}}), encoding="utf-8")
        CredentialProxyHandler.exclusions = scope_exclude.Exclusions(
            path=lambda: str(scope_file), holds=scope_exclude.Holds(path=lambda: ""),
        )
        self.principal = credential_proxy.Principal(
            workload=SHELL_SA, uid="sa-uid", role=credential_proxy.CALLER_ROLE_SHELL, pod=POD, pod_uid=POD_UID,
        )
        CredentialProxyHandler.authenticator = _StubAuthenticator(self.principal)

    def test_describing_an_excluded_cluster_never_reaches_the_executor(self):
        for argv in (
            ["gcloud", "container", "clusters", "describe", OTHER, f"--location={TARGET.location}",
             f"--project={TARGET.project}"],
            ["gcloud", "container", "node-pools", "list", f"--cluster={OTHER}", f"--location={TARGET.location}",
             f"--project={TARGET.project}"],
        ):
            with self.subTest(argv=argv):
                status, body = self.post({"argv": argv}, token=None)
                self.assert_refused(status, body, scope_exclude.RULE_EXCLUDED_CLUSTER)
                self.assertIn(OTHER, body["message"])

    def test_an_in_scope_describe_runs_unchanged(self):
        argv = ["gcloud", "container", "clusters", "describe", TARGET.cluster, f"--location={TARGET.location}",
                f"--project={TARGET.project}"]
        status, body = self.post({"argv": argv}, token=None)
        self.assertEqual(200, status, body)
        (ran, _), = self.executor.calls
        self.assertEqual(argv, ran)

    def test_a_logging_read_runs_with_the_exclusion_on_its_filter(self):
        argv = ["gcloud", "logging", "read", 'resource.type="k8s_container"', "--limit=5",
                f"--project={TARGET.project}"]
        status, body = self.post({"argv": argv}, token=None)
        self.assertEqual(200, status, body)
        (ran, _), = self.executor.calls
        self.assertEqual(
            ["gcloud", "logging", "read", f'(\nresource.type="k8s_container"\n) AND {CLAUSE}', "--limit=5",
             f"--project={TARGET.project}"],
            ran,
        )

    def test_a_prohibited_flag_gets_its_own_rule_not_the_scope_rule(self):
        argv = ["gcloud", "container", "clusters", "describe", OTHER, f"--location={TARGET.location}",
                "--impersonate-service-account=x@y.iam.gserviceaccount.com"]
        status, body = self.post({"argv": argv}, token=None)
        self.assertEqual(403, status, body)
        self.assertNotEqual(scope_exclude.RULE_EXCLUDED_CLUSTER, body.get("rule"), body)
        self.assertEqual([], self.executor.calls)

    def test_help_on_a_describe_runs(self):
        status, body = self.post({"argv": ["gcloud", "container", "clusters", "describe", "--help"]}, token=None)
        self.assertEqual(200, status, body)

    def test_a_filter_that_could_swallow_the_clause_is_refused(self):
        argv = ["gcloud", "logging", "read", 'severity>=ERROR) OR (x="y', f"--project={TARGET.project}"]
        status, body = self.post({"argv": argv}, token=None)
        self.assert_refused(status, body, scope_exclude.RULE_EXCLUDED_CLUSTER)


class ProfileMetadataScopeRouteTest(_RouteHarness):
    """A profile caller's logging read: the clause goes on before confinement, so the
    confined call carries it and the executor's argv check still holds."""

    def setUp(self):
        super().setUp()
        self.addCleanup(
            self._restore, "exclusions", "exclusions" in CredentialProxyHandler.__dict__,
            CredentialProxyHandler.__dict__.get("exclusions"),
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        scope_file = Path(directory.name) / "scope.json"
        scope_file.write_text(json.dumps({"exclude": {"projects": [], "clusters": [
            {"projectId": TARGET.project, "location": TARGET.location, "clusterName": OTHER}]}}), encoding="utf-8")
        CredentialProxyHandler.exclusions = scope_exclude.Exclusions(
            path=lambda: str(scope_file), holds=scope_exclude.Holds(path=lambda: ""),
        )

    def test_a_profile_s_bare_reads_of_its_own_target_run(self):
        # Confinement pins a bare node-pools read and a describe to the task's
        # target; the cluster check runs after it, on that argv, so an install
        # that excludes another cluster doesn't refuse them.
        for argv in (
            ["gcloud", "container", "node-pools", "list"],
            ["gcloud", "container", "clusters", "describe", TARGET.cluster],
        ):
            with self.subTest(argv=argv):
                self.executor.calls.clear()
                status, body = self.post({"argv": argv})
                self.assertEqual(200, status, body)

    def test_a_profile_s_read_of_an_excluded_cluster_is_refused(self):
        status, body = self.post({"argv": ["gcloud", "container", "clusters", "describe", OTHER]})
        self.assertEqual(403, status, body)
        self.assertEqual([], self.executor.calls)

    def test_a_profile_logging_read_carries_the_clause_through_confinement(self):
        status, body = self.post({"argv": ["gcloud", "logging", "read", "severity>=ERROR"]})
        self.assertEqual(200, status, body)
        (ran, kwargs), = self.executor.calls
        self.assertIn(f"(\nseverity>=ERROR\n) AND {CLAUSE}", ran)
        self.assertEqual(ran, kwargs["confined"].argv)


if __name__ == "__main__":
    unittest.main()


class ListingRouteTest(_RouteHarness):
    """#7031: a session's and a profile task's project-wide listings drop excluded
    clusters; the platform agent's own shell keeps the full list."""

    def setUp(self):
        _RouteHarness.setUp(self)
        self.addCleanup(
            self._restore, "exclusions", "exclusions" in CredentialProxyHandler.__dict__,
            CredentialProxyHandler.__dict__.get("exclusions"),
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        scope_file = Path(directory.name) / "scope.json"
        scope_file.write_text(json.dumps({"exclude": {"projects": [], "clusters": [
            {"projectId": TARGET.project, "location": TARGET.location, "clusterName": OTHER}]}}), encoding="utf-8")
        CredentialProxyHandler.exclusions = scope_exclude.Exclusions(
            path=lambda: str(scope_file), holds=scope_exclude.Holds(path=lambda: ""),
        )

    def as_role(self, role):
        principal = credential_proxy.Principal(workload=SHELL_SA, uid="sa-uid", role=role, pod=POD, pod_uid=POD_UID)
        CredentialProxyHandler.authenticator = _StubAuthenticator(principal)

    def test_a_profile_listing_carries_the_filter_through_confinement(self):
        status, body = self.post({"argv": ["gcloud", "container", "clusters", "list"]})
        self.assertEqual(200, status, body)
        (ran, kwargs), = self.executor.calls
        self.assertIn(f'--filter=NOT name~"^({OTHER})$"', ran)
        self.assertEqual(ran, kwargs["confined"].argv)

    def test_the_platform_shell_keeps_the_full_listing(self):
        self.as_role(credential_proxy.CALLER_ROLE_SHELL)
        argv = ["gcloud", "container", "clusters", "list", f"--project={TARGET.project}"]
        status, body = self.post({"argv": argv}, token=None)
        self.assertEqual(200, status, body)
        (ran, _), = self.executor.calls
        self.assertEqual(argv, ran)
