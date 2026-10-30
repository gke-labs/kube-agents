"""The broker's profile role (#3889): task credentials, binding and confinement.

A profile worker's kubectl and gcloud reach the shared broker with a projected
ServiceAccount token (the profile audience) and the dispatcher's task
credential in X-Kube-Agents-Task-Credential. These tests hold the broker to:
the credential is verified and bound to the attested pod, UID, profile and
tier; without one only local-only commands run; with one every call is held
to its target cluster and project.
"""

from __future__ import annotations

import contextlib
import dataclasses
import http.client
import json
import logging
import os
import re
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import command_policy
import credential_proxy
import task_confinement
import task_credential
import vcs_broker
from credential_proxy import CredentialProxyHandler, Policy
from credential_proxy_client import ClusterTarget
from task_confinement import ConfinementRefusal, confine

FIXTURES = Path(__file__).resolve().parents[3] / "a2a" / "taskcred" / "testdata" / "credential_proxy_fixtures.json"
DOC = json.loads(FIXTURES.read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in DOC["cases"]}
VALID = CASES["valid"]

TARGET = ClusterTarget(project="proj-a", location="us-central1", cluster="cluster-a")
CONTEXT = TARGET.context_name
OTHER_CONTEXT = "gke_proj-b_us-central1_cluster-b"
PROFILE_SA = "system:serviceaccount:kubeagents-profiles:agentprofile-auditor-read"
POD = "auditor-read-x7k2p"
POD_UID = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"


class KubectlConfinementTest(unittest.TestCase):
    """Every way kubectl names a cluster resolves to the target or is refused."""

    def refused(self, argv, kubeconfig_context=None, target=TARGET):
        with self.assertRaises(ConfinementRefusal) as caught:
            confine(argv, kubeconfig_context, target)
        return str(caught.exception)

    def test_naming_no_cluster_gets_the_target_not_the_host(self):
        confined = confine(["kubectl", "get", "pods"], None, TARGET)
        self.assertEqual(CONTEXT, confined.kubeconfig_context)
        self.assertEqual(["kubectl", "get", "pods", f"--context={CONTEXT}"], confined.argv)
        self.assertFalse(confined.isolated)

    def test_the_context_is_named_on_every_call(self):
        # Unconditional: whatever the kubeconfig's current-context says,
        # kubectl selects the target's entry.
        for argv, context in (
            (["kubectl", "get", "pods"], CONTEXT),
            (["kubectl", f"--kubeconfig={CONTEXT}", "get", "pods"], None),
        ):
            with self.subTest(argv=argv):
                confined = confine(argv, context, TARGET)
                self.assertIn(f"--context={CONTEXT}", confined.argv)
        given = ["kubectl", "get", "pods", f"--context={CONTEXT}"]
        self.assertEqual(given, confine(given, None, TARGET).argv)

    def test_the_target_named_every_way_passes(self):
        for argv, context in (
            (["kubectl", "get", "pods", f"--context={CONTEXT}"], None),
            (["kubectl", "--context", CONTEXT, "get", "pods"], None),
            (["kubectl", "get", "pods"], CONTEXT),
            (["kubectl", f"--kubeconfig={CONTEXT}", "get", "pods"], None),
        ):
            with self.subTest(argv=argv, context=context):
                confine(argv, context, TARGET)

    def test_a_kubeconfig_flag_resolves_to_the_target(self):
        # Checked equal to the target and dropped: the broker runs the
        # target's kubeconfig from the structure, with no flag to reroute.
        confined = confine(["kubectl", f"--kubeconfig={CONTEXT}", "get", "pods"], None, TARGET)
        self.assertEqual(CONTEXT, confined.kubeconfig_context)
        self.assertEqual(["kubectl", "get", "pods", f"--context={CONTEXT}"], confined.argv)

    def test_another_context_flag_is_refused(self):
        self.assertIn("--context", self.refused(["kubectl", "get", "pods", f"--context={OTHER_CONTEXT}"]))
        self.refused(["kubectl", "--context", OTHER_CONTEXT, "get", "pods"])
        # kubectl reads the last; every one is checked, so a target first does
        # not cover another after it.
        self.refused(["kubectl", f"--context={CONTEXT}", "get", "pods", f"--context={OTHER_CONTEXT}"])
        self.refused(["kubectl", "get", "pods", "--context="])

    def test_another_kubeconfig_flag_is_refused(self):
        self.assertIn("--kubeconfig", self.refused(["kubectl", f"--kubeconfig={OTHER_CONTEXT}", "get", "pods"]))
        self.refused(["kubectl", "--kubeconfig", "/etc/credential-proxy/kubeconfig", "get", "pods"])
        self.refused(["kubectl", f"--kubeconfig={CONTEXT}", f"--kubeconfig={OTHER_CONTEXT}", "get", "pods"])

    def test_another_kubeconfig_context_is_refused(self):
        self.assertIn("context", self.refused(["kubectl", "get", "pods"], kubeconfig_context=OTHER_CONTEXT))

    # Refused by name, for the redirect itself: the message is the explicit
    # check's, not the unlisted-flag fallback's, so the explicit check is what
    # these pin (the allowlist stands behind it on its own test).
    REDIRECT = "is refused for a profile task: the cluster and the credential are the task's"

    def redirect_refused(self, argv, flag):
        message = self.refused(argv)
        self.assertIn(flag, message)
        self.assertIn(self.REDIRECT, message)

    def test_the_cluster_flag_is_refused(self):
        self.redirect_refused(["kubectl", "get", "pods", "--cluster=gke_proj-b"], "--cluster")

    def test_the_server_flag_is_refused(self):
        self.redirect_refused(["kubectl", "get", "pods", "--server", "https://10.0.0.1"], "--server")
        self.redirect_refused(["kubectl", "get", "pods", "-s", "https://10.0.0.1"], "-s")
        self.redirect_refused(["kubectl", "get", "pods", "-As", "https://10.0.0.1"], "-s")

    def test_the_user_flag_is_refused(self):
        self.redirect_refused(["kubectl", "get", "pods", "--user=admin"], "--user")

    def test_insecure_skip_tls_verify_is_refused(self):
        self.redirect_refused(["kubectl", "get", "pods", "--insecure-skip-tls-verify"], "--insecure-skip-tls-verify")

    def test_the_other_credential_flags_are_refused(self):
        for flag in ("--token=x", "--as=admin", "--certificate-authority=/x", "--client-key=/x"):
            with self.subTest(flag=flag):
                self.redirect_refused(["kubectl", "get", "pods", flag], flag.split("=")[0])

    def test_an_unlisted_flag_is_refused(self):
        self.refused(["kubectl", "get", "pods", "--raw", "/api"])

    def test_a_flag_after_double_dash_is_the_remote_commands(self):
        # After `--` the words are the command kubectl exec runs in the pod.
        confine(["kubectl", "exec", "pod/x", "--", "tool", f"--context={OTHER_CONTEXT}"], None, TARGET)


class GcloudConfinementTest(unittest.TestCase):
    """--project, locations, identity and project-less reads."""

    def refused(self, argv, target=TARGET):
        with self.assertRaises(ConfinementRefusal) as caught:
            confine(argv, None, target)
        return str(caught.exception)

    def test_a_missing_project_is_the_targets(self):
        confined = confine(["gcloud", "compute", "instances", "list"], None, TARGET)
        self.assertEqual(["gcloud", "compute", "instances", "list", "--project=proj-a"], confined.argv)
        self.assertIsNone(confined.kubeconfig_context)

    def test_the_target_project_is_kept_as_given(self):
        for argv in (
            ["gcloud", "compute", "instances", "list", "--project=proj-a"],
            ["gcloud", "--project", "proj-a", "compute", "instances", "list"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(
                    ["gcloud", "compute", "instances", "list", "--project=proj-a"], confine(argv, None, TARGET).argv
                )

    def test_a_gke_call_gets_the_target_location_and_cluster(self):
        # Unconditional: gcloud would otherwise take them from compute/region,
        # compute/zone and container/cluster.
        for argv, want in (
            (["gcloud", "container", "clusters", "list"],
             ["gcloud", "container", "clusters", "list", "--project=proj-a", "--location=us-central1"]),
            (["gcloud", "container", "node-pools", "list"],
             ["gcloud", "container", "node-pools", "list", "--project=proj-a", "--location=us-central1", "--cluster=cluster-a"]),
            (["gcloud", "container", "operations", "list", "--location=us-central1"],
             ["gcloud", "container", "operations", "list", "--project=proj-a", "--location=us-central1"]),
            (["gcloud", "container", "clusters", "describe", "cluster-a"],
             ["gcloud", "container", "clusters", "describe", "--project=proj-a", "--location=us-central1", "cluster-a"]),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(want, confine(argv, None, TARGET).argv)

    def test_another_project_is_refused(self):
        self.assertIn("--project", self.refused(["gcloud", "container", "clusters", "list", "--project=proj-b"]))
        self.refused(["gcloud", "--project", "proj-b", "container", "clusters", "list"])
        self.refused(["gcloud", "container", "clusters", "list", "--project=proj-a", "--project=proj-b"])
        self.refused(["gcloud", "logging", "read", "x", "--billing-project=proj-b"])

    def test_impersonation_is_refused(self):
        self.assertIn(
            "--impersonate-service-account",
            self.refused(["gcloud", "container", "clusters", "list", "--impersonate-service-account=sa@proj-a.iam.gserviceaccount.com"]),
        )

    def test_another_account_is_refused(self):
        self.assertIn("--account", self.refused(["gcloud", "container", "clusters", "list", "--account", "someone@example.com"]))

    def test_the_other_identity_flags_are_refused(self):
        for flag in ("--configuration=other", "--access-token-file=/x", "--flags-file=/x.yaml"):
            with self.subTest(flag=flag):
                self.refused(["gcloud", "container", "clusters", "list", flag])

    def test_another_location_or_cluster_is_refused(self):
        self.assertIn("--location", self.refused(["gcloud", "container", "node-pools", "list", "--cluster=cluster-a", "--location=europe-west1"]))
        self.assertIn("--zone", self.refused(["gcloud", "container", "operations", "list", "--zone", "us-central1-a"]))
        self.refused(["gcloud", "container", "operations", "list", "--region=europe-west1"])
        self.assertIn("--cluster", self.refused(["gcloud", "container", "node-pools", "list", "--cluster=cluster-b", "--location=us-central1"]))
        confine(["gcloud", "container", "node-pools", "list", "--cluster=cluster-a", "--location=us-central1"], None, TARGET)

    def test_project_less_reads_are_refused(self):
        for argv in (
            ["gcloud", "projects", "list"],
            ["gcloud", "beta", "projects", "list"],
            ["gcloud", "projects", "search", "--query=x"],
            ["gcloud", "organizations", "list"],
            ["gcloud", "resource-manager", "folders", "list"],
            ["gcloud", "billing", "accounts", "list"],
            ["gcloud", "asset", "search-all-resources", "--scope=organizations/123"],
            ["gcloud", "asset", "search-all-resources"],
            ["gcloud", "asset", "search-all-iam-policies", "--scope=folders/9"],
            ["gcloud", "logging", "read", "x", "--organization=123"],
            ["gcloud", "storage", "ls", "gs://some-bucket"],
            ["gcloud", "auth", "print-access-token"],
            ["gcloud", "config", "set", "project", "proj-b"],
            ["gcloud", "info"],
        ):
            with self.subTest(argv=argv):
                self.refused(argv)

    def test_project_less_reads_are_named_as_such(self):
        # The allowlist refuses these too; the explicit lists are what tell
        # the model why, so the message is pinned.
        for argv in (
            ["gcloud", "projects", "list"],
            ["gcloud", "beta", "projects", "list"],
            ["gcloud", "projects", "search"],
            ["gcloud", "organizations", "list"],
            ["gcloud", "resource-manager", "folders", "list"],
            ["gcloud", "billing", "budgets", "list"],
            ["gcloud", "storage", "ls", "gs://some-bucket"],
            ["gcloud", "info"],
        ):
            with self.subTest(argv=argv):
                self.assertIn("reads outside any one project", self.refused(argv))

    def test_a_project_scoped_asset_search_passes(self):
        confine(["gcloud", "asset", "search-all-resources", "--scope=projects/proj-a"], None, TARGET)

    def test_projects_names_only_the_target(self):
        confine(["gcloud", "projects", "describe", "proj-a"], None, TARGET)
        self.refused(["gcloud", "projects", "describe", "proj-b"])

    def test_embedded_projects_are_held_to_the_target(self):
        for argv in (
            ["gcloud", "logging", "read", 'logName="projects/proj-b/logs/x"'],
            ["gcloud", "container", "clusters", "describe", "https://container.googleapis.com/v1/projects/proj-b/locations/us-central1/clusters/c"],
            ["gcloud", "container", "clusters", "describe", "projects/proj-a/locations/us-central1/clusters/cluster-b"],
            ["gcloud", "iam", "service-accounts", "list", "--filter=name:projects/proj-b/serviceAccounts/x"],
            ["gcloud", "projects", "get-iam-policy", "projects/123456789"],
        ):
            with self.subTest(argv=argv):
                self.refused(argv)
        confine(["gcloud", "logging", "read", 'logName="projects/proj-a/logs/x"'], None, TARGET)
        confine(["gcloud", "container", "clusters", "describe", "projects/proj-a/locations/us-central1/clusters/cluster-a"], None, TARGET)

    def test_get_credentials_must_name_the_target_exactly(self):
        ok = ["gcloud", "container", "clusters", "get-credentials", "cluster-a", "--location=us-central1"]
        want = ["gcloud", "container", "clusters", "get-credentials", "--project=proj-a", "--location=us-central1", "cluster-a"]
        self.assertEqual(want, confine(ok, None, TARGET).argv)
        # A location gcloud would default is the target's, injected.
        self.assertEqual(
            want,
            confine(["gcloud", "container", "clusters", "get-credentials", "cluster-a"], None, TARGET).argv,
        )
        for argv in (
            ["gcloud", "container", "clusters", "get-credentials", "cluster-b", "--location=us-central1"],
            ["gcloud", "container", "clusters", "get-credentials", "cluster-a", "--location=europe-west1"],
            ["gcloud", "container", "clusters", "get-credentials", "cluster-a", "--location=us-central1", "--project=proj-b"],
            # No "run as given": a cluster gcloud would default is one this
            # side can't see.
            ["gcloud", "container", "clusters", "get-credentials", "--location=us-central1"],
        ):
            with self.subTest(argv=argv):
                self.refused(argv)

    def test_another_cluster_by_positional_is_refused(self):
        self.refused(["gcloud", "container", "clusters", "describe", "cluster-b", "--location=us-central1"])

    def test_an_unknown_flag_is_refused(self):
        self.refused(["gcloud", "container", "clusters", "list", "--some-new-flag", "x"])


class ConfinedCommandTableTest(unittest.TestCase):
    """gcloud reads are confined only where the table says how they name a project."""

    def test_every_table_entry_is_a_read_the_policy_admits(self):
        self.assertLessEqual(set(task_confinement.GCLOUD_CONFINED_COMMANDS), set(command_policy.GCLOUD_READ_COMMANDS))

    def test_a_read_missing_from_the_table_is_refused(self):
        # `version` is the one read outside the table that runs: it is
        # local-only (ruling (d)) and names no project at all.
        missing = set(command_policy.GCLOUD_READ_COMMANDS) - set(task_confinement.GCLOUD_CONFINED_COMMANDS) - {("version",)}
        self.assertTrue(missing)
        for command in sorted(missing):
            with self.subTest(command=command):
                with self.assertRaises(ConfinementRefusal):
                    confine(["gcloud", *command], None, TARGET)

    def test_a_read_added_to_the_policy_later_fails_closed(self):
        added = ("compute", "new-surface", "list")
        with mock.patch.object(command_policy, "GCLOUD_READ_COMMANDS", command_policy.GCLOUD_READ_COMMANDS | {added}):
            with self.assertRaisesRegex(ConfinementRefusal, "GCLOUD_CONFINED_COMMANDS"):
                confine(["gcloud", *added], None, TARGET)

    def test_shared_vpc_names_only_the_target_host_project(self):
        with self.assertRaisesRegex(ConfinementRefusal, "another project"):
            confine(["gcloud", "compute", "shared-vpc", "list-associated-resources", "other-proj"], None, TARGET)
        confine(["gcloud", "compute", "shared-vpc", "list-associated-resources", "proj-a"], None, TARGET)

    def test_a_metrics_scope_names_only_the_target(self):
        for scope in ("other-proj", "locations/global/metricsScopes/other-proj", "locations/global/metricsScopes/123"):
            with self.subTest(scope=scope):
                with self.assertRaisesRegex(ConfinementRefusal, "metrics scope"):
                    confine(["gcloud", "beta", "monitoring", "metrics-scopes", "describe", scope], None, TARGET)
        for scope in ("proj-a", "locations/global/metricsScopes/proj-a"):
            with self.subTest(scope=scope):
                confine(["gcloud", "beta", "monitoring", "metrics-scopes", "describe", scope], None, TARGET)

    def test_an_image_names_only_the_target_project(self):
        for image in (
            "us-docker.pkg.dev/other/repo/img:1",
            "europe-west1-docker.pkg.dev/other/repo/img@sha256:abc",
            "gcr.io/other/img",
            "eu.gcr.io/other/img",
            "registry.example.com/proj-a/img",
            "img",
        ):
            with self.subTest(image=image):
                with self.assertRaisesRegex(ConfinementRefusal, "image"):
                    confine(["gcloud", "artifacts", "docker", "images", "describe", image], None, TARGET)
        for image in ("us-docker.pkg.dev/proj-a/repo/img:1", "gcr.io/proj-a/img", "us.gcr.io/proj-a/img@sha256:abc"):
            with self.subTest(image=image):
                confine(["gcloud", "artifacts", "docker", "images", "describe", image], None, TARGET)

    def test_a_kubeconfig_context_for_another_cluster_refuses_a_gcloud_call(self):
        with self.assertRaisesRegex(ConfinementRefusal, "kubeconfig's context"):
            confine(["gcloud", "container", "clusters", "list"], OTHER_CONTEXT, TARGET)
        confine(["gcloud", "container", "clusters", "list"], CONTEXT, TARGET)


class FlagValueTest(unittest.TestCase):
    """A flag's value is read the way the CLI reads it."""

    def test_help_false_is_not_local_only(self):
        for argv in (["kubectl", "get", "pods", "--help=false"], ["gcloud", "container", "clusters", "list", "--help=false"]):
            with self.subTest(argv=argv):
                with self.assertRaisesRegex(ConfinementRefusal, "no target"):
                    confine(argv, None, None)
        confine(["kubectl", "get", "pods", "--help=true"], None, None)

    def test_the_walk_gives_a_scope_flag_its_value(self):
        flags, words = task_confinement._gcloud_walk(["gcloud", "--billing-account", "help", "billing", "budgets", "list"])
        self.assertEqual([("--billing-account", "help")], flags)
        self.assertEqual(["billing", "budgets", "list"], words)
        flags, words = task_confinement._gcloud_walk(["gcloud", "--account=x", "version"])
        self.assertEqual(([("--account", "x")], ["version"]), (flags, words))

    def test_a_scope_or_identity_flag_consumes_its_value(self):
        # Without the value consumed, `help` is the first word and the command
        # reads as local-only help.
        for argv in (
            ["gcloud", "--billing-account", "help", "billing", "budgets", "list"],
            ["gcloud", "--organization", "help", "projects", "list"],
            ["gcloud", "--impersonate-service-account", "help", "compute", "instances", "list"],
            ["gcloud", "--account", "version"],
        ):
            for target in (TARGET, None):
                with self.subTest(argv=argv, target=target):
                    with self.assertRaisesRegex(ConfinementRefusal, argv[1]):
                        confine(argv, None, target)

    def test_an_identity_flag_beside_a_local_only_command_is_refused(self):
        # Local-only runs isolated, but gcloud still holds the broker's own
        # credential there: an identity or scope flag is refused before the
        # local-only check, however local the command is.
        for argv in (
            ["gcloud", "version", "--account=someone@example.com"],
            ["gcloud", "container", "clusters", "list", "--help", "--impersonate-service-account=sa@proj-b.iam.gserviceaccount.com"],
            ["gcloud", "version", "--organization=123"],
        ):
            for target in (TARGET, None):
                with self.subTest(argv=argv, target=target):
                    with self.assertRaisesRegex(ConfinementRefusal, argv[-1].split("=")[0]):
                        confine(argv, None, target)


class StoredDefaultTest(unittest.TestCase):
    """Nothing a profile runs may change what a later bare command targets."""

    def test_gcloud_config_writes_are_refused(self):
        for argv in (
            ["gcloud", "config", "set", "project", "other"],
            ["gcloud", "config", "set", "account", "someone@example.com"],
            ["gcloud", "config", "set", "auth/impersonate_service_account", "sa@other.iam.gserviceaccount.com"],
            ["gcloud", "config", "set", "compute/region", "europe-west1"],
            ["gcloud", "config", "set", "container/cluster", "cluster-b"],
            ["gcloud", "config", "unset", "project"],
            ["gcloud", "config", "configurations", "activate", "other"],
            ["gcloud", "config", "configurations", "create", "other"],
            ["gcloud", "config", "configurations", "delete", "other"],
            ["gcloud", "beta", "config", "set", "project", "other"],
            ["gcloud", "config", "set", "project", "other", "--help"],
        ):
            for target in (TARGET, None):
                with self.subTest(argv=argv, target=target):
                    with self.assertRaisesRegex(ConfinementRefusal, "changes the configuration"):
                        confine(argv, None, target)

    def test_kubectl_config_writes_are_refused(self):
        for argv in (
            ["kubectl", "config", "use-context", OTHER_CONTEXT],
            ["kubectl", "config", "set-context", "--current", "--namespace=x"],
            ["kubectl", "config", "set-cluster", "x"],
            ["kubectl", "config", "set-credentials", "x"],
            ["kubectl", "config", "set", "current-context", OTHER_CONTEXT],
            ["kubectl", "config", "unset", "current-context"],
            ["kubectl", "config", "delete-context", CONTEXT],
            ["kubectl", "config", "rename-context", CONTEXT, "x"],
            ["kubectl", "config", "use-context", OTHER_CONTEXT, "--help"],
        ):
            for target in (TARGET, None):
                with self.subTest(argv=argv, target=target):
                    try:
                        confine(argv, None, target)
                    except ConfinementRefusal as exc:
                        # Refused for the write, or (--current, --namespace
                        # are outside the allowlist) for a flag: either way,
                        # never run.
                        self.assertRegex(str(exc), "changes the kubeconfig|not a flag")
                    else:
                        self.fail(f"{argv} ran")

    def test_the_brokers_own_gcloud_state_is_not_shown(self):
        # config list, config get-value and auth list print the broker's own
        # account and default project: refused with or without a credential,
        # --help or not.
        for argv in (
            ["gcloud", "config", "list"],
            ["gcloud", "config", "get", "project"],
            ["gcloud", "config", "get-value", "account"],
            ["gcloud", "auth", "list"],
            ["gcloud", "beta", "auth", "list"],
            ["gcloud", "auth", "list", "--help"],
        ):
            for target in (TARGET, None):
                with self.subTest(argv=argv, target=target):
                    with self.assertRaisesRegex(ConfinementRefusal, "broker's own account"):
                        confine(argv, None, target)

    def test_the_kubectl_config_reads_still_run(self):
        for argv in (
            ["kubectl", "config", "get-contexts"],
            ["kubectl", "config", "current-context"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(confine(argv, None, None).isolated)
                self.assertTrue(confine(argv, None, TARGET).isolated)


class LocalOnlyTest(unittest.TestCase):
    """Ruling (d): local-only commands run without a target; nothing else does."""

    def test_local_only_commands_run_without_a_target(self):
        for argv in (
            ["kubectl", "version", "--client"],
            ["kubectl", "get", "pods", "--help"],
            ["gcloud", "version"],
            ["gcloud", "--help"],
            ["gcloud", "container", "clusters", "list", "--help"],
        ):
            with self.subTest(argv=argv):
                confined = confine(argv, None, None)
                self.assertTrue(confined.isolated)
                self.assertIsNone(confined.kubeconfig_context)
                self.assertEqual(argv, confined.argv)

    def test_without_a_target_nothing_else_runs(self):
        for argv, context in (
            (["kubectl", "get", "pods"], None),
            (["kubectl", "version"], None),
            (["kubectl", "version", "--client", f"--context={CONTEXT}"], None),
            (["kubectl", "version", "--client"], CONTEXT),
            (["gcloud", "container", "clusters", "list"], None),
            (["gcloud", "container", "clusters", "list", "--project=proj-a"], None),
            (["gcloud", "projects", "list"], None),
        ):
            with self.subTest(argv=argv, context=context):
                with self.assertRaisesRegex(ConfinementRefusal, "no target|may not name"):
                    confine(argv, context, None)

    def test_the_no_target_refusal_promises_only_what_the_policy_admits(self):
        # `kubectl apply --help` is refused by the policy (#4298), so the
        # message may not promise "--help on a command" runs.
        for argv in (["kubectl", "get", "pods"], ["gcloud", "container", "clusters", "list"]):
            with self.subTest(argv=argv):
                with self.assertRaises(ConfinementRefusal) as caught:
                    confine(argv, None, None)
                self.assertNotRegex(str(caught.exception), r"--help on a command(?! it admits)")
                self.assertIn("the read-only policy admits", str(caught.exception))

    def test_a_local_shaped_gcloud_argv_with_a_separator_is_refused_for_the_separator(self):
        # The `--` refusal precedes the policy call: the policy would judge a
        # reordered argv in which the post-`--` word has become a flag.
        argv = ["gcloud", "container", "clusters", "list", "--help", "--", "--flags-file=/tmp/x"]
        for target in (None, TARGET):
            with self.subTest(target=target):
                with self.assertRaises(ConfinementRefusal) as caught:
                    confine(argv, None, target)
                self.assertEqual(task_confinement.RULE_TASK_TARGET, caught.exception.rule)
                self.assertIsNone(caught.exception.policy_argv)
                self.assertIn("takes no --", str(caught.exception))

    def test_a_target_that_is_not_a_gke_name_is_no_target(self):
        self.assertIsNone(task_confinement.target_from_claims("proj_a", "us-central1", "c"))
        self.assertEqual(TARGET, task_confinement.target_from_claims("proj-a", "us-central1", "cluster-a"))


class _StubAuthenticator:
    authenticates = True

    def __init__(self, principal):
        self.principal = principal

    def authenticate(self, headers):
        return self.principal


class _RecordingExecutor:
    ALLOWED_EXECUTABLES = credential_proxy.CommandExecutor.ALLOWED_EXECUTABLES

    def __init__(self):
        self.calls = []

    def git_lease_violation(self, argv, cwd):
        return None

    def execute(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return credential_proxy.ExecutionResult(
            exit_code=0, stdout="ran", stderr="", duration_ms=0, truncated=False, timed_out=False,
        )


class ProfileRouteTest(unittest.TestCase):
    """The broker end to end: header in, binding, confinement, executor out."""

    def setUp(self):
        for attribute in (
            "policy", "executor", "enforce_read_only", "max_request_bytes", "authenticator", "profile_gate",
        ):
            self.addCleanup(
                self._restore, attribute, attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )
        self.executor = _RecordingExecutor()
        CredentialProxyHandler.executor = self.executor
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.max_request_bytes = 1 << 20
        CredentialProxyHandler.enforce_read_only = True
        self.principal = credential_proxy.Principal(
            workload=PROFILE_SA, uid="sa-uid", role=credential_proxy.CALLER_ROLE_PROFILE, pod=POD, pod_uid=POD_UID,
        )
        CredentialProxyHandler.authenticator = _StubAuthenticator(self.principal)
        self.now = VALID["now"]
        self.identities = {PROFILE_SA: task_credential.ProfileIdentity("auditor", "read")}
        self.set_gate()

        self.records = []
        level = credential_proxy.LOGGER.level
        credential_proxy.LOGGER.setLevel(logging.INFO)
        self.addCleanup(credential_proxy.LOGGER.setLevel, level)
        test = self

        class Capture(logging.Handler):
            def emit(self, record):
                test.records.append(record.getMessage() + " " + json.dumps(getattr(record, "audit", {}), default=str))

        previous = list(credential_proxy.LOGGER.handlers)
        propagate = credential_proxy.LOGGER.propagate
        credential_proxy.LOGGER.handlers = [Capture()]
        credential_proxy.LOGGER.propagate = False
        self.addCleanup(setattr, credential_proxy.LOGGER, "propagate", propagate)
        self.addCleanup(setattr, credential_proxy.LOGGER, "handlers", previous)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    @staticmethod
    def _restore(attribute, was_set, original):
        if was_set:
            setattr(CredentialProxyHandler, attribute, original)
        elif attribute in CredentialProxyHandler.__dict__:
            delattr(CredentialProxyHandler, attribute)

    def set_gate(self, trusted=None, identities=None, lifetime=None):
        CredentialProxyHandler.profile_gate = credential_proxy.ProfileTaskGate(
            trusted=tuple(trusted or DOC["trusted"]),
            identities=self.identities if identities is None else identities,
            max_lifetime_raw=lifetime or f"{DOC['max_lifetime_seconds']}s",
            clock=lambda: self.now,
        )

    def post(self, payload, token=VALID["token"], path="/v1/exec"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers[credential_proxy.TASK_CREDENTIAL_HEADER] = token
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def assert_refused(self, status, body, rule):
        self.assertEqual(403, status, body)
        self.assertEqual("SECURITY_POLICY_BLOCKED", body.get("code"), body)
        self.assertEqual(rule, body["rule"])
        self.assertEqual([], self.executor.calls, "a refused call reached the executor")

    # -- a valid credential ------------------------------------------------

    def test_a_bound_credential_runs_kubectl_on_the_target(self):
        status, body = self.post({"argv": ["kubectl", "get", "pods"]})
        self.assertEqual(200, status, body)
        (argv, kwargs), = self.executor.calls
        self.assertEqual(["kubectl", "get", "pods", f"--context={CONTEXT}"], argv)
        self.assertEqual(CONTEXT, kwargs["kubeconfig_context"])
        self.assertFalse(kwargs["confined"].isolated)
        self.assertEqual(argv, kwargs["confined"].argv)

    def test_a_bound_credential_gives_gcloud_the_target_project(self):
        status, body = self.post({"argv": ["gcloud", "container", "clusters", "list"]})
        self.assertEqual(200, status, body)
        (argv, _), = self.executor.calls
        self.assertEqual(["gcloud", "container", "clusters", "list", "--project=proj-a", "--location=us-central1"], argv)

    def test_changing_a_default_is_refused_at_the_proxy(self):
        for argv in (["gcloud", "config", "set", "project", "other"], ["kubectl", "config", "use-context", OTHER_CONTEXT]):
            with self.subTest(argv=argv):
                status, body = self.post({"argv": argv})
                self.assert_refused(status, body, task_confinement.RULE_TASK_TARGET)

    def test_a_call_outside_the_target_is_refused_at_the_proxy(self):
        for argv in (
            ["kubectl", "get", "pods", f"--context={OTHER_CONTEXT}"],
            ["gcloud", "container", "clusters", "list", "--project=proj-b"],
            ["gcloud", "container", "clusters", "get-credentials", "cluster-b", "--location=us-central1"],
        ):
            with self.subTest(argv=argv):
                status, body = self.post({"argv": argv})
                self.assert_refused(status, body, task_confinement.RULE_TASK_TARGET)

    def test_a_kubeconfig_context_outside_the_target_is_refused(self):
        status, body = self.post({"argv": ["kubectl", "get", "pods"], "kubeconfigContext": OTHER_CONTEXT})
        self.assert_refused(status, body, task_confinement.RULE_TASK_TARGET)

    def test_the_token_is_never_logged_and_its_jti_is(self):
        self.post({"argv": ["kubectl", "get", "pods"]})
        self.post({"argv": ["kubectl", "get", "pods"]}, token=CASES["the nats audience"]["token"])
        joined = "\n".join(self.records)
        self.assertNotIn(VALID["token"], joined)
        self.assertNotIn(VALID["token"].split(".")[2], joined)
        self.assertNotIn(CASES["the nats audience"]["token"].split(".")[2], joined)
        self.assertIn("jti FIXTUREJTI", joined)

    # -- the mandatory credential -------------------------------------------

    def test_no_credential_runs_no_cluster_call(self):
        for argv in (["kubectl", "get", "pods"], ["gcloud", "container", "clusters", "list"]):
            with self.subTest(argv=argv):
                status, body = self.post({"argv": argv}, token=None)
                self.assert_refused(status, body, task_confinement.RULE_TASK_TARGET)

    def test_no_credential_still_runs_local_only_commands_isolated(self):
        status, body = self.post({"argv": ["kubectl", "version", "--client"]}, token=None)
        self.assertEqual(200, status, body)
        (_, kwargs), = self.executor.calls
        self.assertTrue(kwargs["confined"].isolated)
        self.assertIsNone(kwargs["kubeconfig_context"])

    # -- --help parity with the read-only policy (#4298) ------------------------

    HELP_THE_POLICY_REFUSES = (
        ["kubectl", "apply", "--help"],
        ["kubectl", "delete", "--help"],
        ["kubectl", "rollout", "restart", "--help"],
        ["kubectl", "delete", "pods", "-h"],
        ["gcloud", "components", "list", "--help"],
        ["gcloud", "init", "--help"],
    )

    def test_help_on_a_command_the_policy_refuses_gets_the_policys_refusal(self):
        # `--help` makes a command local-only only when command_policy admits
        # it: a write's --help is refused with the policy's own rule and
        # message, with or without a task, not a confinement refusal that
        # promised "--help on a command" runs.
        for argv in self.HELP_THE_POLICY_REFUSES:
            decision = command_policy.evaluate(argv)
            self.assertFalse(decision.allowed, argv)
            for token in (VALID["token"], None):
                with self.subTest(argv=argv, task=token is not None):
                    status, body = self.post({"argv": argv}, token=token)
                    self.assert_refused(status, body, decision.rule_id)
                    # The whole body the policy gate gives (message, verb,
                    # local_render), so the shim's refusal stop reads it alike.
                    self.assertEqual(credential_proxy.read_only_refusal(argv)[0], body)

    def test_help_on_a_command_the_policy_admits_still_runs_isolated(self):
        for argv in (
            ["kubectl", "get", "pods", "--help"],
            ["kubectl", "describe", "pods", "--help"],
            ["gcloud", "container", "clusters", "list", "--help"],
        ):
            for token in (VALID["token"], None):
                with self.subTest(argv=argv, task=token is not None):
                    self.executor.calls.clear()
                    status, body = self.post({"argv": argv}, token=token)
                    self.assertEqual(200, status, body)
                    (ran, kwargs), = self.executor.calls
                    self.assertEqual(argv, ran)
                    self.assertTrue(kwargs["confined"].isolated)
                    self.assertIsNone(kwargs["kubeconfig_context"])

    def test_no_gate_is_no_task(self):
        CredentialProxyHandler.profile_gate = None
        status, body = self.post({"argv": ["kubectl", "get", "pods"]}, token=None)
        self.assert_refused(status, body, task_confinement.RULE_TASK_TARGET)
        status, body = self.post({"argv": ["kubectl", "get", "pods"]})
        self.assert_refused(status, body, credential_proxy.RULE_TASK_CREDENTIAL)

    def test_two_credential_headers_are_refused(self):
        # urllib folds a repeated header into one; http.client sends both.
        body = json.dumps({"argv": ["kubectl", "get", "pods"]}).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/v1/exec")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
        connection.putheader(credential_proxy.TASK_CREDENTIAL_HEADER, VALID["token"])
        connection.putheader(credential_proxy.TASK_CREDENTIAL_HEADER, CASES["the nats audience"]["token"])
        connection.endheaders(body)
        response = connection.getresponse()
        self.assert_refused(response.status, json.loads(response.read()), credential_proxy.RULE_TASK_CREDENTIAL)

    # -- the binding ----------------------------------------------------------

    def refused_credential(self):
        status, body = self.post({"argv": ["kubectl", "get", "pods"]})
        self.assert_refused(status, body, credential_proxy.RULE_TASK_CREDENTIAL)
        return body["message"]

    def test_another_pod_is_refused(self):
        CredentialProxyHandler.authenticator = _StubAuthenticator(
            credential_proxy.Principal(workload=PROFILE_SA, role=credential_proxy.CALLER_ROLE_PROFILE, pod="auditor-read-other", pod_uid=POD_UID)
        )
        self.assertIn("another pod", self.refused_credential())

    def test_another_pod_uid_is_refused(self):
        CredentialProxyHandler.authenticator = _StubAuthenticator(
            credential_proxy.Principal(workload=PROFILE_SA, role=credential_proxy.CALLER_ROLE_PROFILE, pod=POD, pod_uid="99999999-0000-0000-0000-000000000000")
        )
        self.assertIn("pod UID", self.refused_credential())

    def test_another_profile_is_refused(self):
        self.set_gate(identities={PROFILE_SA: task_credential.ProfileIdentity("other-profile", "read")})
        self.assertIn("profile", self.refused_credential())

    def test_another_tier_is_refused(self):
        self.set_gate(identities={PROFILE_SA: task_credential.ProfileIdentity("auditor", "unattended")})
        self.assertIn("tier", self.refused_credential())

    def test_an_unmapped_service_account_is_refused(self):
        self.set_gate(identities={})
        self.assertIn("no profile identity", self.refused_credential())

    def test_an_expired_credential_is_refused(self):
        self.now = VALID["claims"]["exp"] + 60
        self.assertIn("expired", self.refused_credential())

    def test_the_wrong_audience_is_refused(self):
        status, body = self.post({"argv": ["kubectl", "get", "pods"]}, token=CASES["the nats audience"]["token"])
        self.assert_refused(status, body, credential_proxy.RULE_TASK_CREDENTIAL)

    def test_an_untrusted_key_is_refused(self):
        self.set_gate(trusted=[DOC["untrusted"]])
        self.assertIn("not a trusted", self.refused_credential())

    def test_an_unusable_lifetime_setting_refuses_every_credential(self):
        self.set_gate(lifetime="forever")
        self.assertIn("A2A_TASK_CREDENTIAL_MAX_LIFETIME", self.refused_credential())

    def test_a_refused_credential_is_refused_even_for_a_local_command(self):
        # A presented credential that fails is loud, not a quiet downgrade to
        # local-only.
        self.set_gate(trusted=[DOC["untrusted"]])
        status, body = self.post({"argv": ["kubectl", "version", "--client"]})
        self.assert_refused(status, body, credential_proxy.RULE_TASK_CREDENTIAL)

    def test_the_route_answers_busy_past_the_profile_share(self):
        slots = credential_proxy.SessionSlots(
            1, role=credential_proxy.CALLER_ROLE_PROFILE, env_name=credential_proxy.ENV_PROFILE_SLOTS,
            bound=credential_proxy.ADMISSION_BOUND_PROFILE,
        )
        CredentialProxyHandler.profile_slots = slots
        self.addCleanup(setattr, CredentialProxyHandler, "profile_slots", None)
        with slots.acquire(credential_proxy.CALLER_ROLE_PROFILE):
            status, body = self.post({"argv": ["kubectl", "get", "pods"]})
            self.assertNotEqual(200, status, body)
            self.assertIn(credential_proxy.ENV_PROFILE_SLOTS, json.dumps(body))
            self.assertEqual([], self.executor.calls)
        status, body = self.post({"argv": ["kubectl", "get", "pods"]})
        self.assertEqual(200, status, body)

    def test_other_roles_ignore_the_header(self):
        shell = credential_proxy.Principal(workload="system:serviceaccount:ns:shell", role=credential_proxy.CALLER_ROLE_SHELL)
        CredentialProxyHandler.authenticator = _StubAuthenticator(shell)
        self.set_gate(trusted=[DOC["untrusted"]])
        status, body = self.post({"argv": ["kubectl", "get", "pods"]})
        self.assertEqual(200, status, body)
        (_, kwargs), = self.executor.calls
        self.assertIsNone(kwargs["kubeconfig_context"])
        self.assertNotIn("confined", kwargs)

    def test_the_profile_flag_allowlist_is_the_sessions(self):
        status, body = self.post({"argv": ["kubectl", "get", "-f", "/etc/credential-proxy/policy.json"]})
        self.assert_refused(status, body, credential_proxy.RULE_CALLER_KUBECTL_FLAG)

    def test_git_is_not_a_profile_executable(self):
        routed = (*credential_proxy.EXEC_ROUTE_EXECUTABLES, "git")
        with mock.patch.object(credential_proxy, "EXEC_ROUTE_EXECUTABLES", routed):
            status, body = self.post({"argv": ["git", "status"]})
        self.assert_refused(status, body, credential_proxy.RULE_CALLER_EXECUTABLE)


class ProfileRouteDenylistTest(ProfileRouteTest):
    """ProfileRouteTest with the shipped credential denylist installed.

    The two rules and the blocked message are copied from the shipped policy
    (k8s-operator/internal/testing/testdata/platform/expected/platformagent.yaml);
    neither rule carries a message of its own there. Every inherited test runs
    again with them in place.
    """

    BLOCKED_MESSAGE = "Command blocked for security reasons."
    RULES = (
        (
            "kubernetes.token-disclosure",
            r"\bkubectl\b(?:\s+\S+)*?\s+create\b(?:\s+\S+)*?\s+token\b"
            r"|\bkubectl\b(?:\s+\S+)*?\s+config\b(?:\s+\S+)*?\s+view\b(?:\s+\S+)*?\s+--raw\b",
        ),
        (
            "tool.self-modification",
            r"\bgcloud\b(?:\s+\S+)*?\s+components\b(?:\s+\S+)*?\s+(?:install|update|remove)\b"
            r"|\bgh\b(?:\s+\S+)*?\s+extension\b(?:\s+\S+)*?\s+(?:install|upgrade|remove)\b"
            r"|\bgh\b(?:\s+\S+)*?\s+(?:alias|config)\b(?:\s+\S+)*?\s+(?:set|import|delete)\b",
        ),
    )

    def setUp(self):
        super().setUp()
        CredentialProxyHandler.policy = Policy(
            rules=[
                credential_proxy.Rule(rule_id=rule_id, pattern=re.compile(pattern, re.IGNORECASE | re.MULTILINE),
                                      message=self.BLOCKED_MESSAGE)
                for rule_id, pattern in self.RULES
            ],
            blocked_message=self.BLOCKED_MESSAGE,
        )

    def test_help_on_a_denylisted_command_keeps_the_denylist_rule(self):
        # The help-shaped argv the read-only policy refuses is asked of the
        # denylist first, as the normal path asks it before the read-only
        # gate: its specific rule and message, not kubernetes.read-only.
        for argv, rule_id in (
            (["kubectl", "create", "token", "sa", "--help"], "kubernetes.token-disclosure"),
            (["gcloud", "components", "install", "--help"], "tool.self-modification"),
        ):
            self.assertFalse(command_policy.evaluate(argv).allowed, argv)
            for token in (VALID["token"], None):
                with self.subTest(argv=argv, task=token is not None):
                    self.records.clear()
                    status, body = self.post({"argv": argv}, token=token)
                    self.assert_refused(status, body, rule_id)
                    self.assertEqual(
                        {"status": "blocked", "code": "SECURITY_POLICY_BLOCKED", "rule": rule_id,
                         "message": self.BLOCKED_MESSAGE},
                        body,
                    )
                    blocked = [record for record in self.records if '"status": "blocked"' in record]
                    self.assertTrue(blocked, self.records)
                    for record in blocked:
                        self.assertIn(f'"rule": "{rule_id}"', record)

    def test_a_separator_beside_help_is_refused_for_the_separator(self):
        argv = ["gcloud", "container", "clusters", "list", "--help", "--", "--flags-file=/tmp/x"]
        for token in (VALID["token"], None):
            with self.subTest(task=token is not None):
                status, body = self.post({"argv": argv}, token=token)
                self.assert_refused(status, body, task_confinement.RULE_TASK_TARGET)
                self.assertIn("takes no --", body["message"])


class ChangedDefaultTest(unittest.TestCase):
    """A stored default that changed anyway does not move a profile's call.

    The real executor runs stub CLIs that resolve their target the way the
    real ones do: an explicit flag first, then the stored default. The
    defaults here point elsewhere (a gcloud configuration naming another
    project, region and cluster; a kubeconfig whose current-context is
    another cluster), as if a write had got past the refusals. The call still
    reaches the token's target, because the injected flags beat the defaults.
    """

    GCLOUD_STUB = """#!{python}
import sys
args = sys.argv[1:]
def flag(name, default):
    value = default
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            value = args[i + 1]
        elif a.startswith(name + "="):
            value = a.split("=", 1)[1]
    return value
# The stored configuration, changed to point elsewhere.
print("project=" + flag("--project", "other-project"))
print("location=" + flag("--location", "europe-west1"))
print("cluster=" + flag("--cluster", "cluster-b"))
"""

    KUBECTL_STUB = """#!{python}
import os, sys
args = sys.argv[1:]
context = None
for i, a in enumerate(args):
    if a == "--context" and i + 1 < len(args):
        context = args[i + 1]
    elif a.startswith("--context="):
        context = a.split("=", 1)[1]
if context is None:
    for line in open(os.environ["KUBECONFIG"]):
        if line.startswith("current-context:"):
            context = line.split(":", 1)[1].strip()
print("context=" + context)
"""

    def setUp(self):
        import sys
        import tempfile

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for attribute in ("policy", "executor", "enforce_read_only", "max_request_bytes", "authenticator", "profile_gate"):
            self.addCleanup(
                ProfileRouteTest._restore, attribute, attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps({"blockedMessage": "blocked", "rules": []}), encoding="utf-8")
        CredentialProxyHandler.policy = Policy.load(str(policy_path))
        executor = credential_proxy.CommandExecutor(
            timeout_seconds=10, max_output_bytes=4096, state_dir=str(root / "state"), scoped_pool=None,
        )
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, body in (("gcloud", self.GCLOUD_STUB), ("kubectl", self.KUBECTL_STUB)):
            stub = bin_dir / name
            stub.write_text(body.format(python=sys.executable), encoding="utf-8")
            stub.chmod(0o755)
            executor.executables[name] = str(stub)
        # The kubeconfig the broker resolves for the target, with its
        # current-context moved to another cluster; and the host's, which a
        # call naming no cluster must never reach.
        self.resolved = []
        target_file = root / "target.yaml"
        target_file.write_text(f"apiVersion: v1\nkind: Config\ncurrent-context: {OTHER_CONTEXT}\n", encoding="utf-8")

        def resolve(context, scoped=True):
            self.resolved.append(context)
            return target_file

        executor._resolve_kubeconfig = resolve
        executor._default_kubeconfig = mock.Mock(side_effect=AssertionError("the host kubeconfig was resolved"))
        CredentialProxyHandler.executor = executor
        CredentialProxyHandler.max_request_bytes = 1 << 20
        CredentialProxyHandler.enforce_read_only = True
        CredentialProxyHandler.authenticator = _StubAuthenticator(credential_proxy.Principal(
            workload=PROFILE_SA, role=credential_proxy.CALLER_ROLE_PROFILE, pod=POD, pod_uid=POD_UID,
        ))
        CredentialProxyHandler.profile_gate = credential_proxy.ProfileTaskGate(
            trusted=tuple(DOC["trusted"]),
            identities={PROFILE_SA: task_credential.ProfileIdentity("auditor", "read")},
            max_lifetime_raw="1h",
            clock=lambda: VALID["now"],
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def run_bare(self, argv):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/exec",
            data=json.dumps({"argv": argv}).encode("utf-8"),
            headers={"Content-Type": "application/json", credential_proxy.TASK_CREDENTIAL_HEADER: VALID["token"]},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            body = json.loads(response.read())
        self.assertEqual(0, body["exitCode"], body)
        return dict(line.split("=", 1) for line in body["stdout"].splitlines())

    def test_a_bare_gcloud_call_reaches_the_target_whatever_the_stored_default(self):
        seen = self.run_bare(["gcloud", "container", "node-pools", "list"])
        self.assertEqual({"project": "proj-a", "location": "us-central1", "cluster": "cluster-a"}, seen)
        seen = self.run_bare(["gcloud", "container", "clusters", "list"])
        self.assertEqual(("proj-a", "us-central1"), (seen["project"], seen["location"]))

    def test_a_bare_kubectl_call_reaches_the_target_whatever_the_current_context(self):
        seen = self.run_bare(["kubectl", "get", "pods"])
        self.assertEqual({"context": CONTEXT}, seen)
        self.assertEqual([CONTEXT], self.resolved)

    def post(self, argv, token=VALID["token"]):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers[credential_proxy.TASK_CREDENTIAL_HEADER] = token
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/exec",
            data=json.dumps({"argv": argv}).encode("utf-8"), headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_a_kubeconfig_inside_a_flag_value_never_resolves(self):
        # The confinement and the executor read argv once, the same way: a
        # `--kubeconfig` or `--context` sitting where a value goes is refused,
        # never resolved by a second, arity-blind scan.
        for argv in (
            ["kubectl", "config", "get-contexts", "-n", f"--kubeconfig={OTHER_CONTEXT}"],
            ["kubectl", "get", "pods", "-l", "--kubeconfig", OTHER_CONTEXT],
            ["kubectl", "get", "pods", "--field-selector", f"--kubeconfig={OTHER_CONTEXT}"],
            ["kubectl", "get", "pods", "--namespace", f"--context={OTHER_CONTEXT}"],
        ):
            with self.subTest(argv=argv):
                self.resolved.clear()
                status, body = self.post(argv)
                self.assertNotEqual(200, status, body)
                self.assertNotIn(OTHER_CONTEXT, self.resolved)

    def test_get_credentials_with_help_is_an_isolated_help_read(self):
        # Local-only (--help) runs isolated: no get-credentials handling, so
        # no endpoint describe of the cluster the argv names and no
        # kubeconfig, but the synopsis does run (#4298: it ended as a 400).
        executor = CredentialProxyHandler.executor
        describe = mock.Mock(side_effect=AssertionError("a GKE describe ran"))
        executor._endpoint_decision = describe
        executor._execute_get_credentials = mock.Mock(side_effect=AssertionError("get-credentials handling ran"))
        plain = executor._execute
        kubeconfigs = []

        def run(command, **kwargs):
            kubeconfigs.append(kwargs.get("kubeconfig_path"))
            return plain(command, **kwargs)

        executor._execute = run
        for argv in (
            ["gcloud", "container", "clusters", "get-credentials", "cluster-b", "--location=europe-west1",
             "--project=proj-b", "--help"],
            ["gcloud", "container", "clusters", "get-credentials", "cluster-a", "--help"],
            ["gcloud", "container", "clusters", "get-credentials", "c", "--help"],
        ):
            for token in (VALID["token"], None):
                with self.subTest(argv=argv, task=token is not None):
                    kubeconfigs.clear()
                    self.resolved.clear()
                    status, body = self.post(argv, token=token)
                    self.assertEqual(200, status, body)
                    self.assertEqual(0, body["exitCode"], body)
                    self.assertEqual([Path(os.devnull)], kubeconfigs)
                    self.assertEqual([], self.resolved)
        describe.assert_not_called()
        executor._execute_get_credentials.assert_not_called()

    def test_get_credentials_with_flags_between_its_words_is_still_get_credentials(self):
        # Spelt with a flag between the command words, it still reaches the
        # one path that writes a kubeconfig into a scratch file, never a plain
        # run against the broker's own kubeconfig.
        fetched = []

        def fetch(command, stdin, cwd, wants_kubeconfig, caller=None):
            fetched.append(command[1:])
            return credential_proxy.ExecutionResult(
                exit_code=0, stdout="", stderr="", duration_ms=0, truncated=False, timed_out=False,
            )

        CredentialProxyHandler.executor._execute_get_credentials = fetch
        plain = mock.Mock(side_effect=AssertionError("get-credentials ran as a plain command"))
        CredentialProxyHandler.executor._execute = plain
        for argv in (
            ["gcloud", "container", "clusters", "--location=us-central1", "get-credentials", "cluster-a"],
            ["gcloud", "container", "--project=proj-a", "clusters", "get-credentials", "cluster-a"],
            ["gcloud", "--project", "proj-a", "container", "clusters", "get-credentials", "--location",
             "us-central1", "cluster-a"],
        ):
            with self.subTest(argv=argv):
                fetched.clear()
                status, body = self.post(argv)
                self.assertEqual(200, status, body)
                self.assertEqual(1, len(fetched))
                self.assertEqual(["container", "clusters", "get-credentials"], fetched[0][:3])
        plain.assert_not_called()


class ParseOnceTest(unittest.TestCase):
    """One arity-aware parse, one canonical argv, one structure the executor reads."""

    def test_a_flag_shaped_value_is_refused(self):
        for argv in (
            ["kubectl", "config", "get-contexts", "-n", f"--kubeconfig={OTHER_CONTEXT}"],
            ["kubectl", "get", "pods", "-l", "--kubeconfig", OTHER_CONTEXT],
            ["kubectl", "get", "pods", "-n", "--context", OTHER_CONTEXT],
            ["kubectl", "get", "pods", "--namespace=--kubeconfig=x"],
            ["kubectl", "get", "pods", "-n"],
            ["gcloud", "container", "clusters", "list", "--format", "--project=proj-b"],
        ):
            for target in (TARGET, None):
                with self.subTest(argv=argv, target=target):
                    with self.assertRaises(ConfinementRefusal):
                        confine(argv, None, target)

    def test_the_kubectl_argv_is_canonical(self):
        confined = confine(
            ["kubectl", "--namespace", "ns", "get", "-l", "app=x", f"--kubeconfig={CONTEXT}", "pods", "-A"], None, TARGET,
        )
        self.assertEqual(
            ["kubectl", "get", "pods", f"--context={CONTEXT}", "--namespace=ns", "--selector=app=x", "--all-namespaces"],
            confined.argv,
        )
        self.assertEqual(CONTEXT, confined.kubeconfig_context)
        self.assertEqual(("get", "pods"), confined.words)
        # A shorthand takes its value with pflag's arity and is written long.
        self.assertEqual(
            ["kubectl", "get", "pods", f"--context={CONTEXT}", "--namespace=ns", "--output=json"],
            confine(["kubectl", "get", "pods", "-n", "ns", "-ojson"], None, TARGET).argv,
        )
        # Nothing the caller wrote is a --kubeconfig or --context any more:
        # only the one --context the confinement injects.
        for argv in (
            ["kubectl", "get", "pods", "-n", "ns", f"--context={CONTEXT}"],
            ["kubectl", "--kubeconfig", CONTEXT, "get", "pods"],
        ):
            with self.subTest(argv=argv):
                got = confine(argv, None, TARGET).argv
                self.assertEqual([f"--context={CONTEXT}"], [a for a in got if a.startswith(("--context", "--kubeconfig"))])

    def test_the_gcloud_argv_is_canonical_command_words_first(self):
        for argv in (
            ["gcloud", "container", "clusters", "--location=us-central1", "get-credentials", "cluster-a"],
            ["gcloud", "container", "--project=proj-a", "clusters", "get-credentials", "cluster-a"],
            ["gcloud", "--project", "proj-a", "container", "clusters", "get-credentials", "-z", "us-central1", "cluster-a"],
        ):
            with self.subTest(argv=argv):
                confined = confine(argv, None, TARGET)
                self.assertEqual(["gcloud", "container", "clusters", "get-credentials"], confined.argv[:4])
                self.assertEqual("cluster-a", confined.argv[-1])
                self.assertTrue(confined.get_credentials)
                self.assertTrue(credential_proxy._is_get_credentials(confined.argv))
                self.assertEqual(TARGET, credential_proxy._get_credentials_target(confined.argv))
        self.assertEqual(
            ["gcloud", "container", "clusters", "get-credentials", "--project=proj-a", "--zone=us-central1", "cluster-a"],
            confine(["gcloud", "container", "clusters", "get-credentials", "-z", "us-central1", "cluster-a"],
                    None, TARGET).argv,
        )

    def test_a_local_only_command_runs_isolated_with_a_target_too(self):
        for argv in (
            ["gcloud", "container", "clusters", "get-credentials", "cluster-b", "--location=europe-west1",
             "--project=proj-b", "--help"],
            ["gcloud", "container", "clusters", "list", "--help"],
            ["gcloud", "version"],
            ["kubectl", "version", "--client"],
            ["kubectl", "get", "pods", "--help"],
            ["kubectl", "config", "current-context"],
        ):
            for target in (TARGET, None):
                with self.subTest(argv=argv, target=target):
                    confined = confine(argv, None, target)
                    self.assertTrue(confined.isolated)
                    self.assertIsNone(confined.kubeconfig_context)
                    # Nothing injected: no context, no project of the target's.
                    self.assertNotIn(f"--context={CONTEXT}", confined.argv)
                    self.assertNotIn(f"--project={TARGET.project}", confined.argv)

    def test_every_local_only_command_passes_the_read_only_policy(self):
        # What the confinement admits as local-only must be what command_policy
        # admits downstream; otherwise the refusal text promises a command the
        # broker then refuses under another rule.
        for argv in (
            ["kubectl", "version", "--client"],
            ["kubectl", "get", "pods", "--help"],
            ["kubectl", "config", "get-contexts"],
            ["kubectl", "config", "current-context"],
            ["gcloud", "version"],
            ["gcloud", "--help"],
            ["gcloud", "container", "clusters", "list", "--help"],
        ):
            with self.subTest(argv=argv):
                confined = confine(argv, None, None)
                self.assertTrue(command_policy.evaluate(confined.argv).allowed, confined.argv)
        for argv in (
            ["kubectl", "config", "view"],
            ["kubectl", "config", "view", "--flatten"],
            ["kubectl", "config", "get-clusters"],
            ["kubectl", "config", "get-users"],
            ["kubectl", "help"],
            ["kubectl", "options"],
            ["gcloud", "help"],
            ["gcloud", "topic"],
            ["gcloud", "--version"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(ConfinementRefusal):
                    confine(argv, None, None)
        # --help is local-only only where the policy admits the command: the
        # dynamic path, not just the static lists above (#4298). A refused
        # one carries the policy's rule and message, with or without a task.
        for argv in (
            ["kubectl", "apply", "--help"],
            ["kubectl", "delete", "--help"],
            ["kubectl", "rollout", "restart", "--help"],
            ["gcloud", "components", "list", "--help"],
            ["gcloud", "init", "--help"],
        ):
            decision = command_policy.evaluate(argv)
            for target in (TARGET, None):
                with self.subTest(argv=argv, target=target):
                    with self.assertRaises(ConfinementRefusal) as refused:
                        confine(argv, None, target)
                    self.assertEqual(decision.rule_id, refused.exception.rule)
                    self.assertEqual(decision.message, str(refused.exception))
        for argv in (
            ["kubectl", "rollout", "status", "--help"],
            ["gcloud", "container", "clusters", "get-credentials", "c", "--help"],
        ):
            for target in (TARGET, None):
                with self.subTest(argv=argv, target=target):
                    confined = confine(argv, None, target)
                    self.assertTrue(confined.isolated)
                    self.assertFalse(confined.get_credentials)
                    self.assertTrue(command_policy.evaluate(confined.argv).allowed, confined.argv)
        # --version is not local beside a command: it is confined like any read.
        with self.assertRaisesRegex(ConfinementRefusal, "--project"):
            confine(["gcloud", "container", "clusters", "list", "--project=proj-b", "--version"], None, TARGET)


class ConfinedExecutionTest(unittest.TestCase):
    """The executor runs a profile call from the confinement's structure, not a re-parse."""

    def setUp(self):
        executor = credential_proxy.CommandExecutor.__new__(credential_proxy.CommandExecutor)
        executor.executables = {"kubectl": "/bin/kubectl", "gcloud": "/bin/gcloud"}
        executor.kubectl_timeout_seconds = 60
        executor.scoped_pool = None
        self.seen = {}

        def fake_execute(command, **kwargs):
            self.seen["command"], self.seen["kwargs"] = command, kwargs
            return credential_proxy.ExecutionResult(exit_code=0, stdout="", stderr="", duration_ms=0, truncated=False, timed_out=False)

        executor._execute = fake_execute
        executor._default_kubeconfig = mock.Mock(side_effect=AssertionError("the host kubeconfig was resolved"))
        self.resolved = []
        executor._resolve_kubeconfig = lambda context, scoped=True: self.resolved.append(context) or Path("/k")
        executor._reroute_kubeconfig_flags = mock.Mock(side_effect=AssertionError("argv was re-parsed for --kubeconfig"))
        executor._execute_get_credentials = mock.Mock(side_effect=AssertionError("get-credentials handling ran"))
        self.executor = executor

    def test_the_executor_reads_the_confined_structure_not_the_argv(self):
        confined = confine(["kubectl", "get", "pods"], None, TARGET)
        self.executor.execute(confined.argv, confined=confined)
        self.assertEqual([CONTEXT], self.resolved)
        self.assertEqual(Path("/k"), self.seen["kwargs"]["kubeconfig_path"])

    def test_an_isolated_structure_runs_against_no_kubeconfig(self):
        confined = confine(["gcloud", "container", "clusters", "list", "--help"], None, TARGET)
        self.executor.execute(confined.argv, confined=confined)
        self.assertEqual(Path(os.devnull), self.seen["kwargs"]["kubeconfig_path"])
        self.assertEqual([], self.resolved)
        # get-credentials --help is an isolated help read (#4298): no
        # get-credentials handling, no kubeconfig.
        confined = confine(["gcloud", "container", "clusters", "get-credentials", "cluster-b", "--help"], None, TARGET)
        self.assertFalse(confined.get_credentials)
        self.executor.execute(confined.argv, confined=confined)
        self.assertEqual(Path(os.devnull), self.seen["kwargs"]["kubeconfig_path"])
        self.assertEqual([], self.resolved)
        # The executor still refuses a structure that is isolated and
        # get-credentials both: it would describe a cluster an isolated call
        # reaches none of.
        with self.assertRaises(ValueError):
            self.executor.execute(confined.argv, confined=dataclasses.replace(confined, get_credentials=True))

    def test_an_argv_that_is_not_the_confined_one_is_refused(self):
        confined = confine(["kubectl", "get", "pods"], None, TARGET)
        with self.assertRaises(ValueError):
            self.executor.execute(["kubectl", "get", "pods", f"--kubeconfig={OTHER_CONTEXT}"], confined=confined)
        self.assertEqual([], self.resolved)


class ProfileSlotShareTest(unittest.TestCase):
    """Profile work holds a bounded share of the pool, and the shell keeps a slot."""

    def test_the_share_is_named_defaulted_and_clamped_below_the_pool(self):
        with mock.patch.dict(os.environ, {credential_proxy.ENV_PROFILE_SLOTS: ""}):
            self.assertEqual(4, credential_proxy.profile_slot_limit_from_env(8, 2))
        for raw, pool, session, want, warns in (
            ("3", 8, 2, 3, False),
            ("10", 8, 2, 5, True),  # clamped: 8 - 2 - 1, so the shell keeps a slot
            ("x", 8, 2, 4, True),
            ("0", 8, 2, 4, True),
            ("4", 3, 2, 1, True),  # a pool too small to share still serves one
        ):
            with self.subTest(raw=raw, pool=pool):
                with mock.patch.dict(os.environ, {credential_proxy.ENV_PROFILE_SLOTS: raw}):
                    with self.assertLogs(credential_proxy.LOGGER, level="WARNING") if warns else contextlib.nullcontext():
                        self.assertEqual(want, credential_proxy.profile_slot_limit_from_env(pool, session))

    def test_a_profile_past_its_share_is_busy_and_the_shell_is_not(self):
        slots = credential_proxy.SessionSlots(
            1, role=credential_proxy.CALLER_ROLE_PROFILE, env_name=credential_proxy.ENV_PROFILE_SLOTS,
            bound=credential_proxy.ADMISSION_BOUND_PROFILE,
        )
        with slots.acquire(credential_proxy.CALLER_ROLE_PROFILE):
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as raised:
                with slots.acquire(credential_proxy.CALLER_ROLE_PROFILE):
                    pass
            self.assertEqual(credential_proxy.ADMISSION_BOUND_PROFILE, raised.exception.bound)
            self.assertIn(credential_proxy.ENV_PROFILE_SLOTS, str(raised.exception))
            for role in (credential_proxy.CALLER_ROLE_SHELL, credential_proxy.CALLER_ROLE_SESSION):
                with slots.acquire(role):
                    pass
        with slots.acquire(credential_proxy.CALLER_ROLE_PROFILE):
            pass


class ProfileRouteTableTest(unittest.TestCase):
    def test_the_profile_role_reaches_the_exec_and_vcs_routes_alone(self):
        for prefix, roles in credential_proxy.ROUTE_ROLES:
            with self.subTest(prefix=prefix):
                self.assertEqual(prefix in ("/v1/exec", "/v1/vcs/"), credential_proxy.CALLER_ROLE_PROFILE in roles)

    def test_the_broker_and_the_verifier_name_one_permit(self):
        self.assertEqual(task_credential.PERMIT_VCS_PROPOSAL_CREATE, credential_proxy.VCS_PROPOSAL_CREATE_PERMIT)
        self.assertIn(credential_proxy.VCS_PROPOSAL_CREATE_PERMIT, task_credential.KNOWN_PERMITS)

    def test_the_profile_vcs_verbs_are_the_reads_and_the_three_gated_writes(self):
        self.assertEqual(PROFILE_VCS_READS | PROFILE_VCS_WRITES, credential_proxy.ROLE_VCS_VERBS[credential_proxy.CALLER_ROLE_PROFILE])
        self.assertEqual(PROFILE_VCS_WRITES, credential_proxy.PROFILE_PERMIT_GATED_VCS_VERBS)
        self.assertEqual(frozenset(), PROFILE_VCS_READS & vcs_broker.WRITE_VERBS)

    def test_an_ungated_profile_write_is_refused_at_import(self):
        for verb in ("branch-delete", "proposal-comment", "issue-create"):
            with self.subTest(verb=verb), self.assertRaisesRegex(ValueError, "without the task credential's permit"):
                credential_proxy._validate_role_vcs_verbs(
                    {credential_proxy.CALLER_ROLE_PROFILE: frozenset({"proposal-view", verb})}
                )
        for verb in ("snapshot", "capabilities"):
            with self.subTest(verb=verb), self.assertRaisesRegex(ValueError, "does not manage"):
                credential_proxy._validate_role_vcs_verbs(
                    {credential_proxy.CALLER_ROLE_PROFILE: frozenset({"proposal-view", verb})}
                )
        credential_proxy._validate_role_vcs_verbs(credential_proxy.ROLE_VCS_VERBS)

    def test_the_shim_and_the_broker_name_one_header(self):
        import credential_proxy_client

        self.assertEqual(credential_proxy.TASK_CREDENTIAL_HEADER, credential_proxy_client.TASK_CREDENTIAL_HEADER)

    def test_kubectl_and_gcloud_only(self):
        self.assertEqual(frozenset({"kubectl", "gcloud"}), credential_proxy.ROLE_EXECUTABLES[credential_proxy.CALLER_ROLE_PROFILE])


# The install's own forge login, and the repository every ownership case is on.
AGENT_LOGIN = "kube-agents[bot]"
OWNED_REPO = "acme/infra"


def _proposal(number, source, author=AGENT_LOGIN, source_repo=OWNED_REPO, state="open"):
    """A proposal as the providers translate one (the neutral shape)."""
    return {
        "number": number, "source": source, "sourceRepo": source_repo, "author": author,
        "state": state, "url": f"https://github.com/{OWNED_REPO}/pull/{number}",
    }


class _OwnershipForge:
    """The two reads require_agent_proposal makes: proposal-view and proposal-list."""

    verbs = ("proposal-view", "proposal-list")

    def __init__(self, proposals):
        self.proposals = dict(proposals)

    def proposal_view(self, api, repo, payload):
        return {"proposal": self.proposals[payload["number"]]}

    def proposal_list(self, api, repo, query):
        return {
            "proposals": [
                item for item in self.proposals.values()
                if item["source"] == query.get("source") and item["state"] == query.get("state")
            ]
        }


def _ownership_broker(forge, viewer):
    """What VcsBroker.require_agent_proposal reads off self: _bind and _viewer."""
    bound = type("Bound", (), {"forge": forge, "repo": OWNED_REPO, "api": staticmethod(lambda *a, **k: None)})()
    return type(
        "OwnershipBroker", (), {"_bind": lambda self, payload: bound, "_viewer": lambda self, b: viewer}
    )()


# The profile role's version-control verbs (#3993), spelled out here rather
# than read off the broker's table, so a change to either side is a red test.
PROFILE_VCS_READS = frozenset(
    {"clone", "proposal-view", "proposal-list", "proposal-commits", "branch-view", "identity", "issue-list", "issue-view"}
)
PROFILE_VCS_WRITES = frozenset({"publish", "proposal-create", "proposal-update"})
PERMIT = CASES["a permit"]


class ProfileVcsRouteTest(unittest.TestCase):
    """POST /v1/vcs/<verb> for a profile caller, end to end over HTTP.

    Real fixture credentials through the real gate: "valid" carries no permit,
    "a permit" carries vcs:proposal-create, both bound to the same pod. The
    forge is a stand-in: every verb's route is a recording mock, and the
    managed list answers what each test says.
    """

    PAYLOAD = {
        "repository": "https://github.com/acme/infra", "number": 7, "branch": "platform-agent/fix",
        "title": "t", "body": "b",
    }

    setUp_base = ProfileRouteTest.setUp
    _restore = ProfileRouteTest.__dict__["_restore"]
    set_gate = ProfileRouteTest.set_gate
    post = ProfileRouteTest.post

    def setUp(self):
        self.addCleanup(
            ProfileRouteTest._restore, "vcs", "vcs" in CredentialProxyHandler.__dict__,
            CredentialProxyHandler.__dict__.get("vcs"),
        )
        self.setUp_base()
        vcs = mock.Mock()
        vcs.snapshot_source.side_effect = lambda repository: (repository, None)
        forge = mock.Mock()
        forge.name = "github"
        vcs.registry.resolve.return_value = (forge, "acme/infra")
        CredentialProxyHandler.vcs = vcs
        self.vcs = vcs
        self.routes = {verb: mock.Mock(return_value={"ok": verb}) for verb in vcs_broker.VCS_VERBS}
        patcher = mock.patch.object(credential_proxy.vcs_broker, "route_table", return_value=self.routes)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The ownership bar runs the real VcsBroker.require_agent_proposal
        # against a stand-in forge: proposal 7, on platform-agent/fix, is the
        # install's own.
        self.forge = _OwnershipForge({7: _proposal(7, "platform-agent/fix")})
        self.ownership = _ownership_broker(self.forge, viewer=AGENT_LOGIN)
        vcs.require_agent_proposal = mock.Mock(
            side_effect=lambda verb, payload: vcs_broker.VcsBroker.require_agent_proposal(self.ownership, verb, payload)
        )
        self.managed = mock.Mock(return_value=True)
        patcher = mock.patch.object(credential_proxy, "repository_is_managed", self.managed)
        patcher.start()
        self.addCleanup(patcher.stop)

    # proposal-create from a profile task carries only the fields every adapter
    # reads, so it gets a payload of its own.
    CREATE_PAYLOAD = {
        "repository": "https://github.com/acme/infra", "title": "t", "body": "b",
        "source": "platform-agent/new-change", "target": "main",
    }

    def payload_for(self, verb):
        return self.CREATE_PAYLOAD if verb == "proposal-create" else self.PAYLOAD

    def vcs_post(self, verb, token):
        return self.post(self.payload_for(verb), token=token, path=f"/v1/vcs/{verb}")

    def assert_ran(self, verb, status, body):
        self.assertEqual(200, status, body)
        self.assertEqual({"ok": verb}, body)
        self.routes[verb].assert_called_once_with(self.payload_for(verb))

    def assert_permit_refused(self, verb, status, body):
        self.assertEqual(403, status, body)
        self.assertEqual("CALLER_PERMIT_REQUIRED", body.get("code"), body)
        self.assertEqual(credential_proxy.RULE_CALLER_VCS_PERMIT, body["rule"])
        self.routes[verb].assert_not_called()
        # Refused before the managed list or the forge were asked anything.
        self.vcs.registry.resolve.assert_not_called()
        self.managed.assert_not_called()

    def reset(self):
        for route in self.routes.values():
            route.reset_mock()
        self.vcs.registry.resolve.reset_mock()
        self.managed.reset_mock()

    # -- reads -----------------------------------------------------------------

    def test_every_read_verb_runs_with_a_credential_and_no_permit(self):
        for verb in sorted(PROFILE_VCS_READS):
            with self.subTest(verb=verb):
                self.reset()
                status, body = self.vcs_post(verb, VALID["token"])
                self.assert_ran(verb, status, body)

    def test_every_read_verb_runs_with_the_permit_too(self):
        for verb in sorted(PROFILE_VCS_READS):
            with self.subTest(verb=verb):
                self.reset()
                status, body = self.vcs_post(verb, PERMIT["token"])
                self.assert_ran(verb, status, body)

    def test_a_read_still_meets_the_managed_list(self):
        self.managed.return_value = False
        status, body = self.vcs_post("proposal-view", VALID["token"])
        self.assertEqual(403, status, body)
        self.assertEqual("REPOSITORY_NOT_MANAGED", body["code"])
        self.routes["proposal-view"].assert_not_called()

    # -- no credential -----------------------------------------------------------

    def test_no_credential_is_refused_for_every_admitted_verb(self):
        for verb in sorted(PROFILE_VCS_READS | PROFILE_VCS_WRITES):
            with self.subTest(verb=verb):
                self.reset()
                status, body = self.vcs_post(verb, None)
                self.assert_permit_refused(verb, status, body)
                self.assertIn("needs a task credential", body["error"])

    def test_a_refused_credential_is_refused_whole(self):
        status, body = self.vcs_post("publish", CASES["permits on the nats audience"]["token"])
        self.assertEqual(403, status, body)
        self.assertEqual(credential_proxy.RULE_TASK_CREDENTIAL, body["rule"])
        self.routes["publish"].assert_not_called()

    # -- writes ------------------------------------------------------------------

    def test_every_write_verb_is_refused_without_the_permit(self):
        for verb in sorted(PROFILE_VCS_WRITES):
            for spelling in (verb, verb.replace("-", "_")):
                with self.subTest(spelling=spelling):
                    self.reset()
                    status, body = self.vcs_post(spelling, VALID["token"])
                    self.assert_permit_refused(verb, status, body)
                    self.assertIn(credential_proxy.VCS_PROPOSAL_CREATE_PERMIT, body["error"])

    def test_every_write_verb_runs_with_the_permit(self):
        for verb in sorted(PROFILE_VCS_WRITES):
            with self.subTest(verb=verb):
                self.reset()
                status, body = self.vcs_post(verb, PERMIT["token"])
                self.assert_ran(verb, status, body)
                self.managed.assert_called_once()

    def test_a_permitted_write_to_an_unmanaged_repository_is_refused(self):
        # The permit names no repository; the managed list is what bounds
        # where a permitted write may land.
        self.managed.return_value = False
        for verb in sorted(PROFILE_VCS_WRITES):
            with self.subTest(verb=verb):
                self.reset()
                status, body = self.vcs_post(verb, PERMIT["token"])
                self.assertEqual(403, status, body)
                self.assertEqual("REPOSITORY_NOT_MANAGED", body["code"])
                self.routes[verb].assert_not_called()
                self.managed.assert_called_once()

    def test_every_other_verb_is_refused_even_with_the_permit(self):
        others = sorted(vcs_broker.VCS_VERBS - PROFILE_VCS_READS - PROFILE_VCS_WRITES)
        self.assertIn("branch-delete", others)
        self.assertIn("snapshot", others)
        self.assertIn("capabilities", others)
        for verb in others:
            with self.subTest(verb=verb):
                self.reset()
                status, body = self.vcs_post(verb, PERMIT["token"])
                self.assertEqual(403, status, body)
                self.assertEqual("CALLER_VERB_FORBIDDEN", body["code"])
                self.routes[verb].assert_not_called()

    # -- ownership (the permit does not reach a person's proposal) -----------------

    def assert_ownership_refused(self, verb, status, body):
        self.assertEqual(403, status, body)
        self.assertEqual("PROPOSAL_NOT_OURS", body.get("code"), body)
        self.assertEqual(credential_proxy.RULE_CALLER_VCS_OWNERSHIP, body["rule"])
        self.routes[verb].assert_not_called()

    def post_with(self, verb, **fields):
        return self.post({**self.payload_for(verb), **fields}, token=PERMIT["token"], path=f"/v1/vcs/{verb}")

    def test_proposal_update_runs_on_the_installs_own_proposal(self):
        status, body = self.post_with("proposal-update")
        self.assertEqual(200, status, body)
        self.routes["proposal-update"].assert_called_once()

    def test_proposal_update_is_refused_on_a_proposal_that_is_not_the_installs(self):
        cases = {
            "a person's proposal": _proposal(8, "platform-agent/fix-2", author="alice"),
            "the install's login, off the branch namespace": _proposal(8, "release-1.2"),
            # A fork's head ref can read platform-agent/... and its author be
            # the install's login; its head repository is still the fork.
            "a fork, with the install's login and a platform-agent/ branch": _proposal(
                8, "platform-agent/fix", source_repo="kube-agents/infra"
            ),
            "a same-name repository under another owner": _proposal(
                8, "platform-agent/fix", source_repo="other-owner/infra"
            ),
            "a deleted fork (no head repository)": _proposal(8, "platform-agent/fix", source_repo=""),
        }
        for name, proposal in cases.items():
            with self.subTest(case=name):
                self.reset()
                self.forge.proposals[8] = proposal
                status, body = self.post_with("proposal-update", number=8)
                self.assert_ownership_refused("proposal-update", status, body)

    def test_proposal_update_is_refused_when_the_credential_cannot_name_itself(self):
        self.ownership = _ownership_broker(self.forge, viewer="")
        status, body = self.post_with("proposal-update")
        self.assert_ownership_refused("proposal-update", status, body)

    def test_publish_is_refused_off_the_branch_namespace(self):
        for branch in ("fix", "main", "release-1.2", "platform-agentx/fix"):
            with self.subTest(branch=branch):
                self.reset()
                status, body = self.post_with("publish", branch=branch)
                self.assert_ownership_refused("publish", status, body)

    def test_publish_runs_under_the_branch_namespace(self):
        status, body = self.post_with("publish", branch="platform-agent/new-change")
        self.assertEqual(200, status, body)
        self.routes["publish"].assert_called_once()

    def test_advance_runs_onto_the_installs_own_proposal_branch(self):
        status, body = self.post_with("publish", advance=True)
        self.assertEqual(200, status, body)
        self.routes["publish"].assert_called_once()

    def test_advance_is_refused_onto_a_branch_whose_proposal_is_not_the_installs(self):
        cases = {
            "a person's proposal": _proposal(9, "platform-agent/theirs", author="alice"),
            "a fork's proposal": _proposal(9, "platform-agent/theirs", source_repo="kube-agents/infra"),
            "another owner's same-name repository": _proposal(9, "platform-agent/theirs", source_repo="other-owner/infra"),
        }
        for name, proposal in cases.items():
            with self.subTest(case=name):
                self.reset()
                self.forge.proposals[9] = proposal
                status, body = self.post_with("publish", branch="platform-agent/theirs", advance=True)
                self.assert_ownership_refused("publish", status, body)
        # And a branch with the install's proposal beside a person's.
        self.reset()
        self.forge.proposals[9] = _proposal(9, "platform-agent/fix", author="alice")
        status, body = self.post_with("publish", advance=True)
        self.assert_ownership_refused("publish", status, body)

    def test_advance_is_refused_onto_a_branch_with_no_open_proposal(self):
        status, body = self.post_with("publish", branch="platform-agent/nothing-open", advance=True)
        self.assert_ownership_refused("publish", status, body)

    def test_publish_without_advance_is_refused_onto_a_branch_carrying_anothers_proposal(self):
        # A publish without `advance` still fast-forwards an existing remote
        # branch, so the open proposals on it are checked either way.
        cases = {
            "a person's proposal": _proposal(9, "platform-agent/theirs", author="alice"),
            "a fork's proposal": _proposal(9, "platform-agent/theirs", source_repo="kube-agents/infra"),
            "another owner's same-name repository": _proposal(9, "platform-agent/theirs", source_repo="other-owner/infra"),
        }
        for name, proposal in cases.items():
            with self.subTest(case=name):
                self.reset()
                self.forge.proposals[9] = proposal
                status, body = self.post_with("publish", branch="platform-agent/theirs")
                self.assert_ownership_refused("publish", status, body)
        self.reset()
        self.forge.proposals[9] = _proposal(9, "platform-agent/fix", author="alice")
        status, body = self.post_with("publish")
        self.assert_ownership_refused("publish", status, body)

    def test_publish_without_advance_runs_onto_the_installs_own_proposal_branch(self):
        status, body = self.post_with("publish")  # platform-agent/fix carries proposal 7, the install's
        self.assertEqual(200, status, body)
        self.routes["publish"].assert_called_once()

    def test_publish_is_refused_when_the_open_proposals_fill_a_page(self):
        for number in range(20, 20 + vcs_broker.OPEN_PROPOSALS_ON_A_BRANCH):
            self.forge.proposals[number] = _proposal(number, "platform-agent/busy")
        for advance in (False, True):
            with self.subTest(advance=advance):
                self.reset()
                status, body = self.post_with("publish", branch="platform-agent/busy", advance=advance)
                self.assert_ownership_refused("publish", status, body)

    def test_proposal_create_runs_from_the_installs_own_branch(self):
        for source in ("platform-agent/new-change", "refs/heads/platform-agent/new-change"):
            with self.subTest(source=source):
                self.reset()
                status, body = self.post_with("proposal-create", source=source)
                self.assertEqual(200, status, body)
                self.routes["proposal-create"].assert_called_once()

    def test_proposal_create_is_refused_off_the_branch_namespace(self):
        for source in ("fix", "main", "refs/heads/main", "release-1.2", "platform-agentx/fix"):
            with self.subTest(source=source):
                self.reset()
                status, body = self.post_with("proposal-create", source=source)
                self.assert_ownership_refused("proposal-create", status, body)

    def test_proposal_create_is_refused_from_a_fork_or_another_repository(self):
        # The cross-repository head form, and every field that could name a
        # source repository on some adapter: none is read today, and none is
        # left for a later adapter to honour.
        for fields in (
            {"source": "kube-agents:platform-agent/fix"},
            {"source": "other-owner:platform-agent/fix"},
            {"sourceRepo": "kube-agents/infra"},
            {"sourceRepo": "other-owner/infra"},
            {"head": "other-owner:platform-agent/fix"},
            {"headRepo": "other-owner/infra"},
            {"head_repo": "other-owner/infra"},
            {"source_project_id": 42},
            {"sourceProject": "other-owner/infra"},
            {"fork": True},
        ):
            with self.subTest(fields=fields):
                self.reset()
                status, body = self.post_with("proposal-create", **fields)
                self.assert_ownership_refused("proposal-create", status, body)

    # -- the platform agent is unchanged ------------------------------------------

    def test_the_shell_is_not_held_to_the_ownership_bar(self):
        CredentialProxyHandler.authenticator = _StubAuthenticator(
            credential_proxy.Principal(workload="system:serviceaccount:ns:agent", uid="u", role=credential_proxy.CALLER_ROLE_SHELL)
        )
        self.forge.proposals[8] = _proposal(8, "release-1.2", author="alice")
        self.forge.proposals[9] = _proposal(9, "platform-agent/theirs", author="alice")
        for verb, fields in (
            ("proposal-update", {"number": 8}),
            ("publish", {"branch": "feature/person"}),
            ("publish", {"branch": "release-1.2", "advance": True}),
            ("publish", {"branch": "platform-agent/theirs"}),
            ("proposal-create", {"source": "feature/person"}),
            ("proposal-create", {"source": "platform-agent/x", "sourceRepo": "other-owner/infra"}),
        ):
            with self.subTest(verb=verb, fields=fields):
                self.reset()
                sent = {**self.payload_for(verb), **fields}
                status, body = self.post(sent, token=None, path=f"/v1/vcs/{verb}")
                self.assertEqual(200, status, body)
                self.routes[verb].assert_called_once_with(sent)
        CredentialProxyHandler.vcs.require_agent_proposal.assert_not_called()

    def test_the_shell_reaches_every_verb_with_no_task_credential(self):
        CredentialProxyHandler.authenticator = _StubAuthenticator(
            credential_proxy.Principal(workload="system:serviceaccount:ns:agent", uid="u", role=credential_proxy.CALLER_ROLE_SHELL)
        )
        for verb in sorted(vcs_broker.VCS_VERBS):
            with self.subTest(verb=verb):
                self.reset()
                status, body = self.vcs_post(verb, None)
                self.assert_ran(verb, status, body)


class ProfileAuthenticatorTest(unittest.TestCase):
    """The profile audience and the identity map's ServiceAccounts go together."""

    SHELL = "kubeagents-credential-proxy"
    CHAT = "kubeagents-credential-proxy-chat"
    PROFILE = "kubeagents-credential-proxy-profile"
    AGENT = "system:serviceaccount:kubeagents-system:agent"

    def _authenticator(self, identities=None):
        return credential_proxy.ServiceAccountAuthenticator(
            audience_roles={
                self.SHELL: credential_proxy.CALLER_ROLE_SHELL,
                self.CHAT: credential_proxy.CALLER_ROLE_CHAT,
                self.PROFILE: credential_proxy.CALLER_ROLE_PROFILE,
            },
            allowed_callers=frozenset({self.AGENT, PROFILE_SA}),
            profile_identities={PROFILE_SA: task_credential.ProfileIdentity("auditor", "read")} if identities is None else identities,
            api_host="10.0.0.1", api_port="443", ca_file="", token_file="/nonexistent", cache_seconds=0.0,
        )

    def _review(self, username, audience, extra=None):
        user = {"username": username, "uid": "sa-uid", "groups": []}
        if extra is not None:
            user["extra"] = extra
        return {"status": {"authenticated": True, "audiences": [audience], "user": user}}

    def test_a_profile_caller_gets_the_profile_role_with_its_pod_and_uid(self):
        principal = self._authenticator()._principal_from(self._review(
            PROFILE_SA, self.PROFILE,
            {credential_proxy.TOKEN_REVIEW_POD_NAME_EXTRA: [POD], credential_proxy.TOKEN_REVIEW_POD_UID_EXTRA: [POD_UID]},
        ))
        self.assertEqual(credential_proxy.CALLER_ROLE_PROFILE, principal.role)
        self.assertEqual(POD, principal.pod)
        self.assertEqual(POD_UID, principal.pod_uid)

    def test_a_profile_caller_presenting_another_audience_is_refused(self):
        with self.assertRaisesRegex(credential_proxy.AuthenticationError, "only the profile audience"):
            self._authenticator()._principal_from(self._review(PROFILE_SA, self.SHELL))

    def test_another_caller_presenting_the_profile_audience_is_refused(self):
        with self.assertRaisesRegex(credential_proxy.AuthenticationError, "only for profile callers"):
            self._authenticator()._principal_from(self._review(self.AGENT, self.PROFILE))

    def test_an_empty_identity_map_confers_the_role_on_nobody(self):
        with self.assertRaisesRegex(credential_proxy.AuthenticationError, "only for profile callers"):
            self._authenticator(identities={})._principal_from(self._review(PROFILE_SA, self.PROFILE))


class ProfileEnvironmentTest(unittest.TestCase):
    """build_authenticator and build_profile_gate read the operator's rendering."""

    BASE = {
        "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
        "CREDENTIAL_PROXY_ALLOWED_CALLERS": f"system:serviceaccount:ns:agent,{PROFILE_SA}",
        "CREDENTIAL_PROXY_AUDIENCE": "aud-shell",
        "CREDENTIAL_PROXY_CHAT_AUDIENCE": "aud-chat",
        "KUBERNETES_SERVICE_HOST": "10.0.0.1",
    }

    def build(self, **env):
        with mock.patch.dict(os.environ, {**self.BASE, **env}, clear=True):
            authenticator = credential_proxy.build_authenticator()
            return authenticator, credential_proxy.build_profile_gate(authenticator)

    def test_the_profile_role_is_conferred_with_its_map_and_keys(self):
        authenticator, gate = self.build(
            CREDENTIAL_PROXY_PROFILE_AUDIENCE="aud-profile",
            CREDENTIAL_PROXY_PROFILE_IDENTITIES=json.dumps({PROFILE_SA: {"profile": "auditor", "tier": "read"}}),
            A2A_TASK_SIGNING_PUBLIC_KEYS=DOC["trusted"][0],
        )
        self.assertEqual(credential_proxy.CALLER_ROLE_PROFILE, authenticator.audience_roles["aud-profile"])
        self.assertEqual(frozenset({PROFILE_SA}), authenticator.profile_callers)
        self.assertEqual(tuple(DOC["trusted"]), gate.trusted)
        self.assertEqual(task_credential.DEFAULT_MAX_LIFETIME_SECONDS, gate.max_lifetime)

    def test_no_profile_audience_is_no_role_and_no_gate(self):
        authenticator, gate = self.build()
        self.assertNotIn(credential_proxy.CALLER_ROLE_PROFILE, authenticator.audience_roles.values())
        self.assertIsNone(gate)

    def test_a_malformed_identity_map_stops_the_broker(self):
        with self.assertRaisesRegex(RuntimeError, "CREDENTIAL_PROXY_PROFILE_IDENTITIES"):
            self.build(CREDENTIAL_PROXY_PROFILE_AUDIENCE="aud-profile", CREDENTIAL_PROXY_PROFILE_IDENTITIES="[]")

    def test_malformed_keys_stop_the_broker(self):
        identities = json.dumps({PROFILE_SA: {"profile": "auditor", "tier": "read"}})
        for keys in ("not-a-key", ",".join([DOC["trusted"][0], DOC["untrusted"], DOC["trusted"][0]])):
            with self.subTest(keys=keys[:20]):
                with self.assertRaisesRegex(RuntimeError, "A2A_TASK_SIGNING_PUBLIC_KEYS"):
                    self.build(
                        CREDENTIAL_PROXY_PROFILE_AUDIENCE="aud-profile",
                        CREDENTIAL_PROXY_PROFILE_IDENTITIES=identities,
                        A2A_TASK_SIGNING_PUBLIC_KEYS=keys,
                    )

    def test_a_colliding_profile_audience_is_not_conferred(self):
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            authenticator, gate = self.build(CREDENTIAL_PROXY_PROFILE_AUDIENCE="aud-shell")
        self.assertNotIn(credential_proxy.CALLER_ROLE_PROFILE, authenticator.audience_roles.values())
        self.assertIsNone(gate)


class IsolatedExecutionTest(unittest.TestCase):
    """An isolated command runs against an empty kubeconfig, never the host's."""

    def test_isolated_kubectl_reads_no_kubeconfig(self):
        executor = credential_proxy.CommandExecutor.__new__(credential_proxy.CommandExecutor)
        executor.executables = {"kubectl": "/bin/kubectl", "gcloud": "/bin/gcloud"}
        executor.kubectl_timeout_seconds = 60
        seen = {}

        def fake_execute(command, **kwargs):
            seen["command"], seen["kwargs"] = command, kwargs
            return credential_proxy.ExecutionResult(exit_code=0, stdout="", stderr="", duration_ms=0, truncated=False, timed_out=False)

        executor._execute = fake_execute
        executor._default_kubeconfig = mock.Mock(side_effect=AssertionError("the host kubeconfig was resolved"))
        executor._resolve_kubeconfig = mock.Mock(side_effect=AssertionError("a kubeconfig was resolved"))
        confined = confine(["kubectl", "version", "--client"], None, None)
        executor.execute(confined.argv, confined=confined)
        self.assertEqual(Path(os.devnull), seen["kwargs"]["kubeconfig_path"])
        # get-credentials --help with no task is an isolated help read too
        # (#4298); a structure isolated and get-credentials both is refused.
        seen.clear()
        confined = confine(["gcloud", "container", "clusters", "get-credentials", "c", "--help"], None, None)
        executor.execute(confined.argv, confined=confined)
        self.assertEqual(Path(os.devnull), seen["kwargs"]["kubeconfig_path"])
        with self.assertRaises(ValueError):
            executor.execute(confined.argv, confined=dataclasses.replace(confined, get_credentials=True))


if __name__ == "__main__":
    unittest.main()
