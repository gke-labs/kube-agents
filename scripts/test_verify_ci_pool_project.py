#!/usr/bin/env python3
"""Unit tests for verify_ci_pool_project.py."""

import base64
import json
import re
import subprocess
import time
import unittest
import urllib.error
from unittest import mock

import verify_ci_pool_project as checker


def _ok(stdout: str):
    return (0, stdout, "")


def _fail(stderr: str = "boom"):
    return (1, "", stderr)


class RunCmdTest(unittest.TestCase):
    def test_timeout_reports_124_and_does_not_raise(self):
        with mock.patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd=["gcloud"], timeout=120)
        ):
            rc, out, err = checker.run_cmd(["gcloud", "projects", "describe", "p"])
        self.assertEqual(rc, 124)
        self.assertEqual(out, "")
        self.assertIn("timed out", err)

    def test_missing_binary_reports_127(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("no gcloud")):
            rc, _, err = checker.run_cmd(["gcloud"])
        self.assertEqual(rc, 127)
        self.assertIn("no gcloud", err)

    def test_passes_timeout_through_to_subprocess(self):
        completed = subprocess.CompletedProcess(args=["gcloud"], returncode=0, stdout="x", stderr="")
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            checker.run_cmd(["gcloud"])
        self.assertEqual(run.call_args.kwargs["timeout"], checker.DEFAULT_TIMEOUT_SECONDS)


class RequiredApisTest(unittest.TestCase):
    def test_compute_api_is_required(self):
        # bench/tf/fleet declares google_compute_disk directly.
        self.assertIn("compute.googleapis.com", checker.REQUIRED_APIS)

    def test_all_apis_enabled_passes(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"projectNumber": "123456"})),
                _ok("\n".join(sorted(checker.REQUIRED_APIS))),
            ]
            number, result = checker.check_project_and_apis("kube-agents-evals-3")
        self.assertEqual(number, "123456")
        self.assertTrue(result.passed, result.details)

    def test_missing_api_is_reported_by_name(self):
        enabled = checker.REQUIRED_APIS - {"compute.googleapis.com"}
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"projectNumber": "123456"})),
                _ok("\n".join(sorted(enabled))),
            ]
            _, result = checker.check_project_and_apis("kube-agents-evals-3")
        self.assertFalse(result.passed)
        self.assertIn("Missing API: compute.googleapis.com", result.details)

    def test_unparseable_project_json_fails_without_raising(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("not json at all")]
            number, result = checker.check_project_and_apis("kube-agents-evals-3")
        self.assertIsNone(number)
        self.assertFalse(result.passed)


class GkeAndCmekTest(unittest.TestCase):
    def _clusters(self, host_state: str) -> str:
        return "\n".join(
            [
                f"{checker.HOST_CLUSTER}\t{host_state}",
                "seeded-a\tENCRYPTED",
                "seeded-b\tENCRYPTED",
                "seeded-c\tENCRYPTED",
            ]
        )

    def test_encrypted_host_cluster_passes(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok(self._clusters("ENCRYPTED")), _ok("bucket")]
            result = checker.check_gke_and_state("kube-agents-evals-3")
        self.assertTrue(result.passed, result.details)

    def test_all_objects_encryption_enabled_also_passes(self):
        # installer_common.sh accepts both spellings; rejecting the second would
        # fail a correctly configured cluster.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok(self._clusters("ALL_OBJECTS_ENCRYPTION_ENABLED")), _ok("bucket")]
            result = checker.check_gke_and_state("kube-agents-evals-3")
        self.assertTrue(result.passed, result.details)

    def test_decrypted_host_cluster_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok(self._clusters("DECRYPTED")), _ok("bucket")]
            result = checker.check_gke_and_state("kube-agents-evals-3")
        self.assertFalse(result.passed)
        self.assertTrue(any("databaseEncryption.state" in d for d in result.details), result.details)

    def test_missing_encryption_column_fails(self):
        clusters = "\n".join([checker.HOST_CLUSTER, "seeded-a", "seeded-b", "seeded-c"])
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok(clusters), _ok("bucket")]
            result = checker.check_gke_and_state("kube-agents-evals-3")
        self.assertFalse(result.passed)
        self.assertTrue(any("unset" in d for d in result.details), result.details)

    def test_missing_seeded_cluster_fails(self):
        clusters = f"{checker.HOST_CLUSTER}\tENCRYPTED\nseeded-a\tENCRYPTED\nseeded-b\tENCRYPTED"
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok(clusters), _ok("bucket")]
            result = checker.check_gke_and_state("kube-agents-evals-3")
        self.assertFalse(result.passed)
        self.assertTrue(any("seeded-c" in d for d in result.details), result.details)

    def test_missing_state_bucket_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok(self._clusters("ENCRYPTED")), _fail("not found")]
            result = checker.check_gke_and_state("kube-agents-evals-3")
        self.assertFalse(result.passed)
        self.assertTrue(any("state bucket" in d for d in result.details), result.details)


class SeededFleetFixturesTest(unittest.TestCase):
    """check_seeded_fleet_fixtures shells out to hack/fleet-kubeconfigs.sh.

    Two calls: `kubectl version` to establish the probes can run at all, then
    the script itself. The script exits 0 whether it wrote every role file or
    none, so every assertion here is on the summary line it prints to stderr.
    """

    def _summary(self, written: int, unresolved: int = 0, unplanted: int = 0) -> str:
        return (
            f"Seeded-fleet kubeconfigs: {written} role(s) written to /tmp/x, "
            f"{unresolved} on clusters that could not be resolved or reached, "
            f"{unplanted} whose fixtures were not present (project kube-agents-evals-5)"
        )

    def _roles(self) -> int:
        return len(json.loads(checker._FLEET_CATALOG.read_text(encoding="utf-8"))["roles"])

    def test_every_role_written_passes(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("v1.30.0"), (0, "", self._summary(self._roles()))]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertTrue(result.passed, result.details)
        self.assertEqual([], result.warnings)

    def test_project_is_passed_to_the_script(self):
        # FLEET_PROJECT_ID is the only thing pointing the script at the project
        # under test. Without it the script falls back to PROJECT_ID from the
        # ambient environment and verifies whichever project the operator's
        # shell happens to name.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("v1.30.0"), (0, "", self._summary(self._roles()))]
            checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        env = run.call_args_list[1].kwargs["env"]
        self.assertEqual("kube-agents-evals-5", env["FLEET_PROJECT_ID"])
        self.assertTrue(env["BENCH_FLEET_KUBECONFIG_DIR"].startswith("/"))

    def test_unplanted_fixture_fails_and_names_the_role(self):
        # The clusters are up and labelled; the objects were never created.
        # This is the state check_gke_and_state passes and this check exists for.
        stderr = "\n".join([
            "WARNING: deployment/payments-api absent from b.kubeconfig in "
            "kube-agents-evals-5, so fixture role 'crashloop-workload' was never planted.",
            self._summary(self._roles() - 1, unplanted=1),
        ])
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("v1.30.0"), (0, "", stderr)]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertFalse(result.passed)
        self.assertTrue(any("crashloop-workload" in d for d in result.details), result.details)

    def test_unresolved_cluster_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("v1.30.0"), (0, "", self._summary(0, unresolved=self._roles()))]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertFalse(result.passed)

    def test_missing_kubectl_is_unverified_not_failed(self):
        # 127 is "could not look", and reporting it as an absent fleet would
        # block a project that is fine on a missing binary.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [(127, "", "not found")]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertTrue(result.passed)
        self.assertTrue(any("kubectl" in w for w in result.warnings), result.warnings)
        self.assertEqual(1, run.call_count)

    def test_missing_summary_on_exit_zero_is_unverified(self):
        # The wording lives in another file. If it moves, this check must stop
        # answering rather than start failing healthy projects.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("v1.30.0"), (0, "", "something else entirely")]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertTrue(result.passed)
        self.assertTrue(result.warnings)

    def test_script_error_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("v1.30.0"), (1, "", "ERROR: fleet fixture catalog not found")]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertFalse(result.passed)

    def test_summary_regex_matches_the_line_the_script_prints(self):
        # The counts are parsed out of prose in a file this test does not run.
        # Asserting against a hand-written copy of that prose only proves the
        # regex matches itself, so take the format string from the script.
        text = checker._FLEET_KUBECONFIGS.read_text(encoding="utf-8")
        line = next(
            l for l in text.splitlines() if "Seeded-fleet kubeconfigs:" in l and "echo" in l
        )
        rendered = re.sub(r"\$\{[^}]+\}", "7", line.split('"', 1)[1].rsplit('"', 1)[0])
        match = checker._FLEET_SUMMARY.search(rendered)
        self.assertIsNotNone(match, rendered)


class ArtifactRegistryTest(unittest.TestCase):
    """check_artifact_registry makes four calls: describe, project policy, repo policy, cluster list.

    The fourth resolves the account platform-agent-host's nodes run as, so that
    push rights and pull rights are asserted against the identities that
    actually need them rather than against whichever one happens to be granted.
    """

    _REPO = {
        "format": "DOCKER",
        "cleanupPolicies": {"delete-old": {"action": "DELETE"}},
    }
    _EMPTY = json.dumps({"bindings": []})

    # `gcloud container clusters list --format=value(...)` is tab-separated, and
    # "default" is what the API reports for a pool that was never given an
    # account. The seeded trio is in the listing on a real project and must not
    # influence the result.
    _NODES = "platform-agent-host\tdefault"
    _NODES_WITH_FLEET = (
        "platform-agent-host\tdefault\n"
        "seeded-a\tseeded-fleet-nodes@p.iam.gserviceaccount.com\n"
        "seeded-b\tseeded-fleet-nodes@p.iam.gserviceaccount.com"
    )

    def _policy(self, members, role="roles/artifactregistry.writer"):
        return json.dumps({"bindings": [{"role": role, "members": members}]})

    def _push_and_pull(self):
        """The good shape: the build can push, the node account can pull."""
        return json.dumps({
            "bindings": [
                {
                    "role": "roles/artifactregistry.writer",
                    "members": ["serviceAccount:123456@cloudbuild.gserviceaccount.com"],
                },
                {
                    "role": "roles/artifactregistry.reader",
                    "members": ["serviceAccount:123456-compute@developer.gserviceaccount.com"],
                },
            ]
        })

    def test_owner_is_not_an_accepted_writer_role(self):
        # A build identity holding owner is a finding, not a pass.
        self.assertNotIn("roles/owner", checker.AR_WRITER_ROLES)

    def test_repo_with_cleanup_policy_and_writer_passes(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _ok(self._push_and_pull()),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)

    def test_cloudbuild_builds_builder_confers_push(self):
        # What kube-agents-evals actually has; a literal writer check failed it.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _ok(
                    json.dumps({
                        "bindings": [
                            {
                                "role": "roles/cloudbuild.builds.builder",
                                "members": ["serviceAccount:123456@cloudbuild.gserviceaccount.com"],
                            },
                            {
                                "role": "roles/artifactregistry.reader",
                                "members": [
                                    "serviceAccount:123456-compute@developer.gserviceaccount.com"
                                ],
                            },
                        ]
                    })
                ),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)

    def test_editor_on_compute_sa_confers_push_and_pull(self):
        # The node account and the build account are the same identity here, and
        # editor covers both sides. This is the shape all four live pool
        # projects are in today.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _ok(
                    self._policy(
                        ["serviceAccount:123456-compute@developer.gserviceaccount.com"],
                        role="roles/editor",
                    )
                ),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)

    def test_grant_on_the_repository_alone_is_accepted(self):
        # The grant can sit on the repo instead of the project.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _ok(self._EMPTY),
                _ok(self._push_and_pull()),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)

    def test_missing_repository_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _fail("NOT_FOUND"),
                _ok(self._push_and_pull()),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("Missing Artifact Registry" in d for d in result.details), result.details)

    def test_missing_cleanup_policy_fails(self):
        repo = dict(self._REPO)
        repo.pop("cleanupPolicies")
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(repo)),
                _ok(self._push_and_pull()),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("no cleanup policy" in d for d in result.details), result.details)

    def test_dry_run_cleanup_policy_fails(self):
        repo = dict(self._REPO, cleanupPolicyDryRun=True)
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(repo)),
                _ok(self._push_and_pull()),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("dry-run" in d for d in result.details), result.details)

    def test_no_push_grant_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _ok(self._policy(["serviceAccount:someone-else@example.iam.gserviceaccount.com"])),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("image push" in d for d in result.details), result.details)

    def test_unreadable_policies_fail_rather_than_pass_silently(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok(json.dumps(self._REPO)), _fail("denied"), _fail("denied")]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("Could not read any IAM policy" in d for d in result.details), result.details)

    # ── Pull rights ───────────────────────────────────────────────────────────
    # The gap these cover: push and pull are different verbs held by different
    # identities, and a check that only asks about push passes a project whose
    # nodes cannot start a single pod.

    def test_build_can_push_but_node_cannot_pull_fails(self):
        # Cloud Build holds writer; the node account holds nothing. Every other
        # item on this check is satisfied, so before the pull assertion existed
        # this project was reported ready and died at ImagePullBackOff on its
        # first lease.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _ok(self._policy(["serviceAccount:123456@cloudbuild.gserviceaccount.com"])),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed, result.details)
        self.assertTrue(any("image pull" in d for d in result.details), result.details)
        self.assertTrue(
            any("123456-compute@developer.gserviceaccount.com" in d for d in result.details),
            result.details,
        )

    def test_custom_node_service_account_is_read_off_the_cluster(self):
        # A pool created with --service-account runs as that account. Asserting
        # the Compute default here would report a failure the project does not
        # have.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _ok(
                    json.dumps({
                        "bindings": [
                            {
                                "role": "roles/artifactregistry.writer",
                                "members": ["serviceAccount:123456@cloudbuild.gserviceaccount.com"],
                            },
                            {
                                "role": "roles/artifactregistry.reader",
                                "members": ["serviceAccount:nodes@p.iam.gserviceaccount.com"],
                            },
                        ]
                    })
                ),
                _ok(self._EMPTY),
                _ok("platform-agent-host\tnodes@p.iam.gserviceaccount.com"),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)

    def test_seeded_fleet_node_accounts_are_not_asserted(self):
        # The trio runs its own account and pulls no kube-agents image. Holding
        # it to the host cluster's requirement would fail every real project.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _ok(self._push_and_pull()),
                _ok(self._EMPTY),
                _ok(self._NODES_WITH_FLEET),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)

    def test_unreadable_cluster_warns_rather_than_failing(self):
        # "Could not look" is not "cannot pull". This is the same distinction
        # check_toolchain enforces, and it has to hold per check too.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _ok(self._push_and_pull()),
                _ok(self._EMPTY),
                _fail("PERMISSION_DENIED"),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)
        self.assertTrue(any("pull rights" in w for w in result.warnings), result.warnings)
        # The summary must not assert what the warning retracts.
        self.assertIn("not checked", result.message)
        self.assertNotIn("and node pull rights", result.message)

    def test_absent_host_cluster_warns_rather_than_failing(self):
        # An empty listing means the node account is unknown, not unprivileged.
        # check_gke_and_state is what fails a project with no host cluster.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _ok(self._push_and_pull()),
                _ok(self._EMPTY),
                _ok(""),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)
        self.assertTrue(any("no node pools" in w for w in result.warnings), result.warnings)
        self.assertIn("not checked", result.message)
        self.assertNotIn("and node pull rights", result.message)

    def test_checked_pull_rights_are_claimed_in_the_summary(self):
        # The other side of the same contract: when the check did run, the
        # summary says so, so the two states are distinguishable at a glance.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _ok(self._push_and_pull()),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)
        self.assertEqual([], result.warnings)
        self.assertIn("and node pull rights", result.message)

    def test_reader_alone_does_not_confer_push(self):
        # AR_PULLER_ROLES is a superset of AR_WRITER_ROLES; the containment must
        # not run the other way, or a reader-only project reports push-ready.
        self.assertIn("roles/artifactregistry.reader", checker.AR_PULLER_ROLES)
        self.assertNotIn("roles/artifactregistry.reader", checker.AR_WRITER_ROLES)
        self.assertTrue(checker.AR_WRITER_ROLES < checker.AR_PULLER_ROLES)


class GithubAppInstallationTest(unittest.TestCase):
    _APP_ID = checker.DEFAULT_GITHUB_APP_ID

    def test_repo_in_installation_passes(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"isPrivate": True, "name": "kube-agents-evals-3-infra"})),
                _ok(json.dumps({"id": 99, "repository_selection": "selected"})),
                _ok("gke-agentic/kube-agents-evals-3-infra\ngke-agentic/kube-agents-evals-infra"),
            ]
            result = checker.check_github_repo_and_app("kube-agents-evals-3", self._APP_ID)
        self.assertTrue(result.passed, result.details)

    def test_repo_absent_from_installation_fails(self):
        # The regression this check exists for: the installation is healthy and
        # repository_selection is 'selected', but this project's repo is not in it.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"isPrivate": True, "name": "kube-agents-evals-3-infra"})),
                _ok(json.dumps({"id": 99, "repository_selection": "selected"})),
                _ok("gke-agentic/kube-agents-evals-infra\ngke-agentic/kube-agents-evals-2-infra"),
            ]
            result = checker.check_github_repo_and_app("kube-agents-evals-3", self._APP_ID)
        self.assertFalse(result.passed)
        self.assertTrue(any("not in GitHub App" in d for d in result.details), result.details)

    def test_confirmation_flag_clears_the_warning_but_says_it_was_attested(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"isPrivate": True, "name": "kube-agents-evals-3-infra"})),
                _ok(json.dumps({"id": 99, "repository_selection": "selected"})),
                (1, "", "gh: HTTP 403"),
            ]
            result = checker.check_github_repo_and_app(
                "kube-agents-evals-3", self._APP_ID, repo_membership_confirmed=True
            )
        self.assertTrue(result.passed, result.details)
        self.assertEqual(result.warnings, [])
        self.assertIn("operator-confirmed", result.message)
        self.assertIn("not machine-checked", result.message)

    def test_confirmation_flag_does_not_excuse_a_real_failure(self):
        # The flag attests to membership only. A public repo still fails.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"isPrivate": False, "name": "kube-agents-evals-3-infra"})),
                _ok(json.dumps({"id": 99, "repository_selection": "selected"})),
                (1, "", "gh: HTTP 403"),
            ]
            result = checker.check_github_repo_and_app(
                "kube-agents-evals-3", self._APP_ID, repo_membership_confirmed=True
            )
        self.assertFalse(result.passed)

    def test_confirmation_flag_does_not_override_a_readable_absent_repo(self):
        # If the list IS readable and the repo is genuinely missing, the flag
        # must not turn that into a pass -- machine evidence beats attestation.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"isPrivate": True, "name": "kube-agents-evals-3-infra"})),
                _ok(json.dumps({"id": 99, "repository_selection": "selected"})),
                _ok("gke-agentic/some-other-repo"),
            ]
            result = checker.check_github_repo_and_app(
                "kube-agents-evals-3", self._APP_ID, repo_membership_confirmed=True
            )
        self.assertFalse(result.passed)
        self.assertTrue(any("not in GitHub App" in d for d in result.details), result.details)

    def test_unreadable_membership_warns_and_does_not_fail(self):
        # An operator PAT cannot read this list -- only a token authorized to the
        # App can. Failing the project over a limit in our own credentials would
        # be a false negative, so it warns instead.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"isPrivate": True, "name": "kube-agents-evals-3-infra"})),
                _ok(json.dumps({"id": 99, "repository_selection": "selected"})),
                (1, "", "gh: HTTP 403"),
            ]
            result = checker.check_github_repo_and_app("kube-agents-evals-3", self._APP_ID)
        self.assertTrue(result.passed, result.details)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("NOT verified", result.message)
        self.assertIn("settings/installations/99", result.warnings[0])

    def test_public_repo_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"isPrivate": False, "name": "kube-agents-evals-3-infra"})),
                _ok(json.dumps({"id": 99, "repository_selection": "selected"})),
                _ok("gke-agentic/kube-agents-evals-3-infra"),
            ]
            result = checker.check_github_repo_and_app("kube-agents-evals-3", self._APP_ID)
        self.assertFalse(result.passed)
        self.assertTrue(any("not private" in d for d in result.details), result.details)

    def test_repository_selection_all_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"isPrivate": True, "name": "kube-agents-evals-3-infra"})),
                _ok(json.dumps({"id": 99, "repository_selection": "all"})),
                _ok("gke-agentic/kube-agents-evals-3-infra"),
            ]
            result = checker.check_github_repo_and_app("kube-agents-evals-3", self._APP_ID)
        self.assertFalse(result.passed)
        self.assertTrue(any("repository_selection" in d for d in result.details), result.details)

    def test_no_installation_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"isPrivate": True, "name": "kube-agents-evals-3-infra"})),
                _ok(""),
            ]
            result = checker.check_github_repo_and_app("kube-agents-evals-3", self._APP_ID)
        self.assertFalse(result.passed)
        self.assertTrue(any("installation not found" in d for d in result.details), result.details)

    def test_multiple_jq_objects_do_not_raise(self):
        # `gh api --jq` emits one JSON value per match, newline-separated, which
        # is not a parseable document.
        two = json.dumps({"id": 99, "repository_selection": "selected"}) + "\n" + json.dumps({"id": 100})
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"isPrivate": True, "name": "kube-agents-evals-3-infra"})),
                _ok(two),
                _ok("gke-agentic/kube-agents-evals-3-infra"),
            ]
            result = checker.check_github_repo_and_app("kube-agents-evals-3", self._APP_ID)
        self.assertTrue(result.passed, result.details)


class TokenMinterTest(unittest.TestCase):
    """check_token_minter reads four things over gcloud, then probes GitHub.

    The live probe is stubbed here and exercised directly in GithubAppProbeTest;
    these cases are about how check_token_minter routes its three outcomes.
    """

    _GSA = "kubeagents-github-minter-gsa@kube-agents-evals-3.iam.gserviceaccount.com"

    def _versions(self, state="ENABLED", ids=(1,)):
        return json.dumps(
            [{"name": f"projects/p/.../cryptoKeyVersions/{i}", "state": state} for i in ids]
        )

    def _key(self, purpose=None, algorithm=None, import_only=True):
        return json.dumps(
            {
                "purpose": purpose or checker.KMS_KEY_PURPOSE,
                "versionTemplate": {"algorithm": algorithm or checker.KMS_KEY_ALGORITHM},
                "importOnly": import_only,
            }
        )

    def _key_policy(self, members=None):
        members = [f"serviceAccount:{self._GSA}"] if members is None else members
        return json.dumps({"bindings": [{"role": "roles/cloudkms.signerVerifier", "members": members}]})

    def _gsa_policy(self, member=None):
        member = member or f"serviceAccount:kube-agents-evals-3.svc.id.goog[{checker.MINTER_KSA}]"
        return json.dumps({"bindings": [{"role": "roles/iam.workloadIdentityUser", "members": [member]}]})

    def _run(self, versions=None, key=None, key_policy=None, gsa_policy=None, probe=("ok", "accepted as App 1")):
        with mock.patch.object(checker, "run_cmd") as run, \
             mock.patch.object(checker, "_probe_github_app_identity", return_value=probe) as probe_mock:
            run.side_effect = [
                versions if versions is not None else _ok(self._versions()),
                key if key is not None else _ok(self._key()),
                key_policy if key_policy is not None else _ok(self._key_policy()),
                gsa_policy if gsa_policy is not None else _ok(self._gsa_policy()),
            ]
            self.probe_mock = probe_mock
            return checker.check_token_minter("kube-agents-evals-3")

    def test_fully_provisioned_minter_passes(self):
        result = self._run()
        self.assertTrue(result.passed, result.details)

    def test_empty_import_only_key_fails(self):
        # Terraform creates the key import-only and empty; an empty version list
        # means the PEM was never imported with minty.
        result = self._run(versions=_ok("[]"))
        self.assertFalse(result.passed)
        self.assertTrue(any("no ENABLED version" in d for d in result.details), result.details)

    def test_destroyed_version_fails(self):
        result = self._run(versions=_ok(self._versions("DESTROYED")))
        self.assertFalse(result.passed)

    def test_unparseable_versions_fail_without_raising(self):
        result = self._run(versions=_ok("<html>error</html>"))
        self.assertFalse(result.passed)

    def test_wrong_key_purpose_fails(self):
        # A symmetric key holds an ENABLED version too, then fails at signing.
        result = self._run(key=_ok(self._key(purpose="ENCRYPT_DECRYPT")))
        self.assertFalse(result.passed)
        self.assertTrue(any("purpose is ENCRYPT_DECRYPT" in d for d in result.details), result.details)

    def test_wrong_algorithm_fails(self):
        result = self._run(key=_ok(self._key(algorithm="RSA_SIGN_PSS_2048_SHA256")))
        self.assertFalse(result.passed)
        self.assertTrue(any("algorithm is" in d for d in result.details), result.details)

    def test_key_not_import_only_fails(self):
        # Losing import_only means the PEM could be written from Terraform state.
        result = self._run(key=_ok(self._key(import_only=False)))
        self.assertFalse(result.passed)
        self.assertTrue(any("not import-only" in d for d in result.details), result.details)

    def test_missing_signer_verifier_fails(self):
        result = self._run(key_policy=_ok(self._key_policy(members=[])))
        self.assertFalse(result.passed)
        self.assertTrue(any("signerVerifier" in d for d in result.details), result.details)

    def test_missing_minter_gsa_fails(self):
        result = self._run(gsa_policy=_fail("NOT_FOUND"))
        self.assertFalse(result.passed)
        self.assertTrue(any("Minter GSA" in d for d in result.details), result.details)

    def test_missing_minter_workload_identity_binding_fails(self):
        # The minter KSA differs from the platform agent's; binding the wrong one
        # leaves a minter that can never authenticate.
        wrong = "serviceAccount:kube-agents-evals-3.svc.id.goog[kubeagents-system/kubeagents-platform-agent]"
        result = self._run(gsa_policy=_ok(self._gsa_policy(member=wrong)))
        self.assertFalse(result.passed)
        self.assertTrue(any("Workload Identity binding missing" in d for d in result.details), result.details)

    def test_wrong_app_key_fails_the_check(self):
        # The one thing no attribute check can see: correctly shaped material
        # that belongs to a different App.
        result = self._run(probe=("failed", "authenticated as GitHub App 999, not 4675512"))
        self.assertFalse(result.passed)
        self.assertTrue(any("not 4675512" in d for d in result.details), result.details)

    def test_unreachable_github_warns_and_does_not_fail(self):
        # gcloud reaches cloudkms.googleapis.com and the probe reaches
        # api.github.com. One being blocked says nothing about the project, so it
        # must not fail a configuration that is otherwise clean.
        result = self._run(probe=("unverified", "Could not reach https://api.github.com/app"))
        self.assertTrue(result.passed, result.details)
        self.assertTrue(any("Could not reach" in w for w in result.warnings), result.warnings)

    def test_no_enabled_version_skips_the_probe(self):
        self._run(versions=_ok("[]"))
        self.probe_mock.assert_not_called()

    def test_wrong_algorithm_skips_the_probe(self):
        # An RSA_SIGN_PSS key signs fine and yields a JWT GitHub cannot verify.
        # Probing it would spend a round trip to restate the failure just found.
        self._run(key=_ok(self._key(algorithm="RSA_SIGN_PSS_2048_SHA256")))
        self.probe_mock.assert_not_called()

    def test_probe_uses_the_version_the_chart_pins_not_the_highest(self):
        # The pool deploys through helm and the chart pins
        # githubMinter.kms.keyVersion, so probing the highest ENABLED version
        # would verify a key no lease ever loads.
        self._run(versions=_ok(self._versions(ids=(1, 2))))
        self.assertEqual(self.probe_mock.call_args.args[2], "1")

    def test_rotation_that_disables_the_pinned_version_fails(self):
        # import v2, disable v1 -- the rotation token-minter.md describes. The
        # old highest-ENABLED probe greened here while every lease deployed a
        # minter pinned to the disabled v1.
        versions = json.dumps([
            {"name": "projects/p/.../cryptoKeyVersions/1", "state": "DISABLED"},
            {"name": "projects/p/.../cryptoKeyVersions/2", "state": "ENABLED"},
        ])
        result = self._run(versions=_ok(versions))
        self.assertFalse(result.passed)
        self.assertTrue(
            any("cryptoKeyVersion 1" in d and "DISABLED" in d for d in result.details), result.details
        )
        self.probe_mock.assert_not_called()

    def test_pinned_version_that_does_not_exist_fails(self):
        result = self._run(versions=_ok(self._versions(ids=(2,))))
        self.assertFalse(result.passed)
        self.assertTrue(any("does not exist" in d for d in result.details), result.details)
        self.probe_mock.assert_not_called()

    def test_several_enabled_versions_warn_but_pass(self):
        result = self._run(versions=_ok(self._versions(ids=(1, 2))))
        self.assertTrue(result.passed, result.details)
        self.assertTrue(any("ENABLED versions" in w for w in result.warnings), result.warnings)
        self.assertEqual(self.probe_mock.call_args.args[2], "1")

    def test_unreadable_chart_pin_warns_and_falls_back(self):
        with mock.patch.object(
            checker, "_chart_pinned_key_version", return_value=(None, "missing values.yaml")
        ):
            result = self._run(versions=_ok(self._versions(ids=(1, 2))))
        self.assertTrue(result.passed, result.details)
        self.assertTrue(any("unconfirmed" in w for w in result.warnings), result.warnings)
        self.assertEqual(self.probe_mock.call_args.args[2], "2")

    def test_message_names_the_version_it_verified(self):
        self.assertIn("v1", self._run().message)


class ChartKeyVersionPinTest(unittest.TestCase):
    """The pin is only authoritative if it is read correctly and not overridden."""

    def _values(self, text):
        fake = mock.Mock()
        fake.exists.return_value = True
        fake.read_text.return_value = text
        return mock.patch.object(checker, "_CHART_VALUES", fake)

    def test_reads_the_pin_out_of_the_real_chart(self):
        version, detail = checker._chart_pinned_key_version()
        self.assertEqual(detail, "")
        self.assertTrue(version and version.isdigit(), f"unreadable pin {version!r}: {detail}")

    def test_ci_deploy_does_not_override_the_pin(self):
        # The chart's value is what the pool signs with only because nothing
        # overrides it at deploy time. An override added to GITHUB_MINTER_ARGS
        # later would make this whole check verify the wrong version again, so
        # it fails here rather than in a fifteen-minute helm timeout.
        self.assertNotIn("kms.keyVersion", checker._CI_DEPLOY.read_text(encoding="utf-8"))

    def test_a_pin_outside_the_githubminter_block_is_not_read(self):
        with self._values('other:\n  kms:\n    keyVersion: "9"\ngithubMinter:\n  kms:\n    keyVersion: "3"\n'):
            self.assertEqual(checker._chart_pinned_key_version()[0], "3")

    def test_unquoted_pin_is_read(self):
        with self._values("githubMinter:\n  kms:\n    keyVersion: 4\n"):
            self.assertEqual(checker._chart_pinned_key_version()[0], "4")

    def test_missing_values_file_is_reported_not_raised(self):
        fake = mock.Mock()
        fake.exists.return_value = False
        with mock.patch.object(checker, "_CHART_VALUES", fake):
            version, detail = checker._chart_pinned_key_version()
        self.assertIsNone(version)
        self.assertIn("missing", detail)

    def test_absent_pin_is_reported_not_guessed(self):
        with self._values("githubMinter:\n  kms:\n    key: github-token-minter-key\n"):
            version, detail = checker._chart_pinned_key_version()
        self.assertIsNone(version)
        self.assertIn("keyVersion", detail)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class GithubAppProbeTest(unittest.TestCase):
    """_probe_github_app_identity: only GitHub's own verdict may fail a project."""

    def setUp(self):
        self.signing_input = None

    def _sign(self, cmd, **kwargs):
        flags = dict(a.split("=", 1) for a in cmd if a.startswith("--") and "=" in a)
        with open(flags["--input-file"], "rb") as fh:
            self.signing_input = fh.read()
        with open(flags["--signature-file"], "wb") as fh:
            fh.write(b"\x01" * 256)
        return 0, "", ""

    def _probe(self, urlopen, sign=None, app_id=4675512):
        with mock.patch.object(checker, "run_cmd", side_effect=sign or self._sign), \
             mock.patch.object(checker.urllib.request, "urlopen", urlopen):
            return checker._probe_github_app_identity("p", "us-central1", "1", app_id)

    def _http_error(self, code, reason="err"):
        def raise_it(*a, **kw):
            raise urllib.error.HTTPError(checker.GITHUB_APP_URL, code, reason, {}, None)

        return raise_it

    def test_matching_app_id_passes(self):
        status, message = self._probe(lambda *a, **kw: _Response({"id": 4675512, "slug": "minter"}))
        self.assertEqual(status, "ok")
        self.assertIn("4675512", message)

    def test_key_from_another_app_fails(self):
        # A valid RSA key for the wrong App: signs, verifies, mints tokens for
        # somebody else's installation.
        status, message = self._probe(lambda *a, **kw: _Response({"id": 999}))
        self.assertEqual(status, "failed")
        self.assertIn("999", message)

    def test_rejected_signature_fails(self):
        status, message = self._probe(self._http_error(401, "Unauthorized"))
        self.assertEqual(status, "failed")
        self.assertIn("401", message)

    def test_server_error_is_unverified_not_failed(self):
        status, _ = self._probe(self._http_error(503, "Service Unavailable"))
        self.assertEqual(status, "unverified")

    def test_rate_limit_is_unverified_not_failed(self):
        status, _ = self._probe(self._http_error(403, "rate limit exceeded"))
        self.assertEqual(status, "unverified")

    def test_no_egress_is_unverified_not_failed(self):
        def blocked(*a, **kw):
            raise urllib.error.URLError("Name or service not known")

        status, message = self._probe(blocked)
        self.assertEqual(status, "unverified")
        self.assertIn("egress", message)

    def test_untrusted_ca_names_the_cert_bundle_not_a_firewall(self):
        # A python.org build with no CA bundle fails here while curl and gcloud
        # both succeed; "check your egress" would send the operator hunting for
        # a firewall that is not there.
        def untrusted(*a, **kw):
            raise urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

        status, message = self._probe(untrusted)
        self.assertEqual(status, "unverified")
        self.assertIn("SSL_CERT_FILE", message)
        self.assertNotIn("egress", message)

    def test_unsignable_key_is_unverified_and_names_the_permission(self):
        # A limit of this script's credentials, not a defect in the project. No
        # attestation flag is offered for this one the way it is for App
        # installation membership: there is nothing a human could look at.
        status, message = self._probe(
            lambda *a, **kw: _Response({"id": 4675512}), sign=lambda *a, **kw: (1, "", "PERMISSION_DENIED")
        )
        self.assertEqual(status, "unverified")
        self.assertIn("useToSign", message)

    def test_jwt_expiry_stays_inside_githubs_ten_minute_ceiling(self):
        # exp exactly 600s out lands on the boundary and 401s intermittently on
        # clock skew, which reads as a wrong key.
        before = int(time.time())
        self._probe(lambda *a, **kw: _Response({"id": 4675512}))
        claims = json.loads(base64.urlsafe_b64decode(self.signing_input.split(b".")[1] + b"=="))
        # GitHub measures exp against its own clock, so the margin that matters
        # is exp minus now -- not exp minus iat, which is 600 by GitHub's own
        # recommendation to backdate iat a minute for drift.
        self.assertLess(claims["exp"] - before, 600)
        self.assertLess(claims["iat"], before + 1)
        self.assertEqual(claims["iss"], "4675512")

    def test_signed_payload_is_a_bare_jwt_signing_input(self):
        # gcloud signs the file byte for byte; a trailing newline would change
        # the digest and produce a signature over something that is not the JWT.
        self._probe(lambda *a, **kw: _Response({"id": 4675512}))
        self.assertEqual(self.signing_input.count(b"."), 1)
        self.assertFalse(self.signing_input.endswith(b"\n"))
        header = json.loads(base64.urlsafe_b64decode(self.signing_input.split(b".")[0] + b"=="))
        self.assertEqual(header["alg"], "RS256")


class IamGrantsTest(unittest.TestCase):
    def _wi_policy(self, project_id):
        member = f"serviceAccount:{project_id}.svc.id.goog[kubeagents-system/kubeagents-platform-agent]"
        return json.dumps({"bindings": [{"role": "roles/iam.workloadIdentityUser", "members": [member]}]})

    def _reader_policy(self, members):
        return json.dumps({"bindings": [{"role": "roles/artifactregistry.reader", "members": members}]})

    def test_both_build_identities_granted_passes(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-3")),
                _ok(
                    self._reader_policy(
                        [
                            "serviceAccount:123456@cloudbuild.gserviceaccount.com",
                            "serviceAccount:123456-compute@developer.gserviceaccount.com",
                        ]
                    )
                ),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)

    def test_missing_legacy_cloudbuild_reader_fails(self):
        # This is exactly the drift found on kube-agents-evals-2.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-2")),
                _ok(self._reader_policy(["serviceAccount:123456-compute@developer.gserviceaccount.com"])),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-2", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("cloudbuild.gserviceaccount.com" in d for d in result.details), result.details)

    def test_missing_workload_identity_binding_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"bindings": []})),
                _ok(
                    self._reader_policy(
                        [
                            "serviceAccount:123456@cloudbuild.gserviceaccount.com",
                            "serviceAccount:123456-compute@developer.gserviceaccount.com",
                        ]
                    )
                ),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("Workload Identity" in d for d in result.details), result.details)


class ExitStatusTest(unittest.TestCase):
    """An unverified item must never share an exit code with a clean run."""

    def _report(self, checks):
        with mock.patch("builtins.print") as p:
            status = checker.report("kube-agents-evals-3", checks)
        return status, "\n".join(str(c.args[0]) for c in p.call_args_list if c.args)

    def test_all_clean_exits_zero_and_says_safe_to_register(self):
        status, out = self._report([checker.CheckResult("a", True), checker.CheckResult("b", True)])
        self.assertEqual(status, checker.EXIT_OK)
        self.assertIn("ALL CHECKS PASSED", out)

    def test_a_failure_exits_one(self):
        status, out = self._report([checker.CheckResult("a", True), checker.CheckResult("b", False)])
        self.assertEqual(status, checker.EXIT_FAILED)
        self.assertIn("PRE-FLIGHT CHECK FAILED", out)

    def test_a_warning_alone_exits_two_and_withholds_the_green(self):
        status, out = self._report(
            [checker.CheckResult("a", True), checker.CheckResult("b", True, warnings=["cannot read X"])]
        )
        self.assertEqual(status, checker.EXIT_UNVERIFIED)
        self.assertIn("MANUAL VERIFICATION REQUIRED", out)
        self.assertNotIn("ALL CHECKS PASSED", out)
        self.assertIn("cannot read X", out)

    def test_a_failure_outranks_a_warning(self):
        status, out = self._report(
            [checker.CheckResult("a", False), checker.CheckResult("b", True, warnings=["cannot read X"])]
        )
        self.assertEqual(status, checker.EXIT_FAILED)
        self.assertNotIn("MANUAL VERIFICATION REQUIRED", out)

    def test_missing_minter_is_a_failure_not_a_warning(self):
        # Every part of the minter is readable over gcloud, so it is never
        # downgraded to an unverified item the way App membership is.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("[]"), _fail("x"), _fail("x"), _fail("x")]
            minter = checker.check_token_minter("kube-agents-evals-3")
        self.assertFalse(minter.passed)
        self.assertEqual(minter.warnings, [])
        status, _ = self._report([minter])
        self.assertEqual(status, checker.EXIT_FAILED)


class ToolchainTest(unittest.TestCase):
    """A broken toolchain must not be reported as an unprovisioned project."""

    def _toolchain(self, gcloud, gh):
        with mock.patch.object(checker, "run_cmd", side_effect=[gcloud, gh]):
            return checker.check_toolchain()

    def test_both_authenticated_blocks_nothing(self):
        self.assertEqual(self._toolchain(_ok("me@example.com\n"), _ok("")), [])

    def test_logged_out_gcloud_exits_zero_with_no_accounts(self):
        # The case the return code cannot see: an empty active-account list is a
        # successful query, so every later GCP check would report absence.
        blockers = self._toolchain(_ok(""), _ok(""))
        self.assertEqual(len(blockers), 1)
        self.assertIn("no active credential", blockers[0])

    def test_missing_binaries_are_named_separately(self):
        blockers = self._toolchain((127, "", ""), (127, "", ""))
        self.assertEqual(len(blockers), 2)
        self.assertIn("gcloud is not on PATH", blockers[0])
        self.assertIn("gh is not on PATH", blockers[1])

    def test_unauthenticated_gh_blocks(self):
        blockers = self._toolchain(_ok("me@example.com\n"), _fail("not logged in"))
        self.assertEqual(len(blockers), 1)
        self.assertIn("gh is not authenticated", blockers[0])

    def test_a_blocker_exits_unverified_without_running_a_check(self):
        with mock.patch.object(checker, "check_toolchain", return_value=["gcloud is not on PATH"]), \
             mock.patch.object(checker, "run_checks") as run_checks, \
             mock.patch("builtins.print") as p:
            status = checker.verify_project("kube-agents-evals-3")
        run_checks.assert_not_called()
        self.assertEqual(status, checker.EXIT_UNVERIFIED)
        out = "\n".join(str(c.args[0]) for c in p.call_args_list if c.args)
        self.assertIn("Nothing was checked", out)


_REMOTES = (
    "origin\tgit@github.com:lapis2002/kube-agents.git (fetch)\n"
    "origin\tgit@github.com:lapis2002/kube-agents.git (push)\n"
    "gke-labs\tgit@github.com:gke-labs/kube-agents.git (fetch)\n"
    "gke-labs\tgit@github.com:gke-labs/kube-agents.git (push)\n"
    "upstream\tgit@github.com:gke-labs/devops-bench.git (fetch)\n"
    "upstream\tgit@github.com:gke-labs/devops-bench.git (push)\n"
)


def _ci_deploy_text(*projects):
    rows = "".join(f'    {p}) echo "gke-agentic/{p}-infra" ;;\n' for p in projects)
    return 'gitops_repo_for_project() {\n  case "$1" in\n' + rows + "    *) return 1 ;;\n  esac\n}\n"


def _local_ci_deploy(text):
    fake = mock.Mock()
    fake.exists.return_value = True
    fake.read_text.return_value = text
    return mock.patch.object(checker, "_CI_DEPLOY", fake)


def _git(remotes=_REMOTES, remotes_rc=0, show=None, show_rc=0, show_err="fatal: bad object",
         log="2026-08-19", log_rc=0):
    def responder(cmd, *_a, **_kw):
        if cmd[3] == "remote":
            return (remotes_rc, remotes if remotes_rc == 0 else "", "" if remotes_rc == 0 else "not a git repo")
        if cmd[3] == "show":
            return (show_rc, show or "", "" if show_rc == 0 else show_err)
        if cmd[3] == "log":
            return (log_rc, log + "\n" if log_rc == 0 else "", "" if log_rc == 0 else "fatal: bad revision")
        raise AssertionError(f"unexpected command {cmd}")

    return mock.patch.object(checker, "run_cmd", side_effect=responder)


class CodebaseMappingTest(unittest.TestCase):
    """The row a presubmit reads is main's, not this checkout's."""

    def test_row_on_upstream_main_passes_clean(self):
        with _local_ci_deploy(_ci_deploy_text("kube-agents-evals-6")), \
             _git(show=_ci_deploy_text("kube-agents-evals-6")):
            r = checker.check_codebase_mapping("kube-agents-evals-6")
        self.assertTrue(r.passed)
        self.assertEqual(r.warnings, [])
        self.assertIn("gke-labs/main", r.message)

    def test_row_only_in_this_checkout_is_unverified_not_green(self):
        with _local_ci_deploy(_ci_deploy_text("kube-agents-evals-6")), \
             _git(show=_ci_deploy_text("kube-agents-evals-3")):
            r = checker.check_codebase_mapping("kube-agents-evals-6")
        self.assertTrue(r.passed)
        self.assertEqual(len(r.warnings), 1)
        self.assertIn("not yet on gke-labs/main", r.message)
        self.assertIn("before registering", r.warnings[0])
        self.assertIn("git fetch gke-labs main", r.warnings[0])

    def test_row_only_in_this_checkout_withholds_the_safe_to_register_verdict(self):
        with _local_ci_deploy(_ci_deploy_text("kube-agents-evals-6")), \
             _git(show=_ci_deploy_text("kube-agents-evals-3")):
            r = checker.check_codebase_mapping("kube-agents-evals-6")
        with mock.patch("builtins.print") as p:
            status = checker.report("kube-agents-evals-6", [r])
        out = "\n".join(str(c.args[0]) for c in p.call_args_list if c.args)
        self.assertEqual(status, checker.EXIT_UNVERIFIED)
        self.assertNotIn("ALL CHECKS PASSED", out)

    def test_row_absent_locally_still_fails_without_consulting_git(self):
        with _local_ci_deploy(_ci_deploy_text("kube-agents-evals-3")), \
             mock.patch.object(checker, "run_cmd") as run:
            r = checker.check_codebase_mapping("kube-agents-evals-6")
        self.assertFalse(r.passed)
        run.assert_not_called()

    def test_no_remote_for_the_merge_target_is_unverified(self):
        only_fork = "origin\tgit@github.com:lapis2002/kube-agents.git (fetch)\n"
        with _local_ci_deploy(_ci_deploy_text("kube-agents-evals-6")), _git(remotes=only_fork):
            r = checker.check_codebase_mapping("kube-agents-evals-6")
        self.assertTrue(r.passed)
        self.assertIn("no git remote points at gke-labs/kube-agents", r.warnings[0])

    def test_unreadable_main_is_unverified_rather_than_absent(self):
        with _local_ci_deploy(_ci_deploy_text("kube-agents-evals-6")), \
             _git(show_rc=128, show_err="fatal: invalid object name 'gke-labs/main'"):
            r = checker.check_codebase_mapping("kube-agents-evals-6")
        self.assertTrue(r.passed)
        self.assertIn("could not read gke-labs/main:hack/ci-deploy.sh", r.warnings[0])
        self.assertNotIn("not yet on", r.message)

    def test_remote_is_resolved_by_url_not_by_name(self):
        # `origin` is the contributor's fork and `upstream` is a different
        # repository; neither name identifies the merge target.
        with _git():
            self.assertEqual(checker._upstream_remote(), "gke-labs")

    def test_remote_resolution_accepts_the_https_url_form(self):
        https = "fleet\thttps://github.com/gke-labs/kube-agents.git (fetch)\n"
        with _git(remotes=https):
            self.assertEqual(checker._upstream_remote(), "fleet")

    def test_no_git_at_all_is_unverified(self):
        with _local_ci_deploy(_ci_deploy_text("kube-agents-evals-6")), _git(remotes_rc=127):
            r = checker.check_codebase_mapping("kube-agents-evals-6")
        self.assertTrue(r.passed)
        self.assertIn("no git remote points at", r.warnings[0])

    def test_snapshot_predating_the_function_is_unverified_not_absent(self):
        # `git show <remote>/main` reads the last fetch, and a fetch older than
        # 2026-08-21 returns a ci-deploy.sh with no gitops_repo_for_project()
        # in it at all. Every project reads as unmapped there, including ones
        # mapped for months -- so the copy cannot answer, and saying "not yet
        # on main" about it is a claim this check has not earned.
        before_the_function = 'deploy_agent() {\n  echo "no mapping here"\n}\n'
        with _local_ci_deploy(_ci_deploy_text("kube-agents-evals")), \
             _git(show=before_the_function):
            r = checker.check_codebase_mapping("kube-agents-evals")
        self.assertTrue(r.passed)
        self.assertEqual(len(r.warnings), 1)
        self.assertNotIn("not yet on", r.message)
        self.assertIn("no gitops_repo_for_project()", r.warnings[0])

    def test_not_yet_on_main_dates_the_snapshot_it_read(self):
        with _local_ci_deploy(_ci_deploy_text("kube-agents-evals-6")), \
             _git(show=_ci_deploy_text("kube-agents-evals-3"), log="2026-08-19"):
            r = checker.check_codebase_mapping("kube-agents-evals-6")
        self.assertIn("gke-labs/main is dated 2026-08-19", r.warnings[0])

    def test_undatable_snapshot_still_reports_the_row_as_missing(self):
        with _local_ci_deploy(_ci_deploy_text("kube-agents-evals-6")), \
             _git(show=_ci_deploy_text("kube-agents-evals-3"), log_rc=128):
            r = checker.check_codebase_mapping("kube-agents-evals-6")
        self.assertNotIn("dated", r.warnings[0])
        self.assertIn("not yet on gke-labs/main", r.message)


class RunChecksTest(unittest.TestCase):
    def test_missing_project_number_skips_dependent_checks_without_raising(self):
        with mock.patch.object(checker, "check_codebase_mapping", return_value=checker.CheckResult("m", True)), \
             mock.patch.object(checker, "check_project_and_apis", return_value=(None, checker.CheckResult("p", False))), \
             mock.patch.object(checker, "check_gke_and_state", return_value=checker.CheckResult("g", True)), \
             mock.patch.object(checker, "check_github_repo_and_app", return_value=checker.CheckResult("h", True)), \
             mock.patch.object(checker, "check_token_minter", return_value=checker.CheckResult("k", True)):
            results = checker.run_checks("kube-agents-evals-3")
        skipped = [c for c in results if c.message.startswith("Skipped")]
        self.assertEqual(len(skipped), 2)
        self.assertTrue(all(not c.passed for c in skipped))


if __name__ == "__main__":
    unittest.main()
