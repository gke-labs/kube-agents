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


class DenialClassifierTest(unittest.TestCase):
    """_denial_reason separates "I was refused" from "it is not there".

    Every string below is what the tool actually prints, because the whole
    classifier is a claim about the wording of messages written elsewhere. A
    hand-invented denial string would only prove the regex matches itself.

    _unread_reason adds a third category between the two: TRANSIENTS are not
    refusals, and the resource was not read, so they must fail the first test
    and pass the third.
    """

    REFUSALS = (
        "ERROR: (gcloud.artifacts.repositories.describe) PERMISSION_DENIED: Permission "
        "'artifactregistry.repositories.get' denied on resource '...' (or it may not exist).",
        "ERROR: (gcloud.container.clusters.list) ResponseError: code=403, message=Required "
        '"container.clusters.list" permission(s) for "projects/kube-agents-evals-6".',
        "ERROR: (gcloud.storage.buckets.describe) HTTPError 403: x@google.com does not have "
        "storage.buckets.get access to the Google Cloud Storage bucket.",
        "ERROR: (gcloud.projects.describe) User [x] does not have permission to access projects "
        "instance [y] (or it may not exist)",
        # Observed on 2026-08-27 against an existing SA in kube-agents-evals-6.
        "ERROR: (gcloud.iam.service-accounts.get-iam-policy) PERMISSION_DENIED: Permission "
        "'iam.serviceAccounts.getIamPolicy' denied on resource "
        "'//iam.googleapis.com/projects/-/serviceAccounts/113405042032614536240' (or it may not exist).",
        "gh: Resource not accessible by integration (HTTP 403)",
        # gh wraps every API error as `gh: <message> (HTTP <code>)`; the entry
        # above is an observed instance of that wrapper, and the message half
        # varies with the endpoint and matches none of the other patterns. Both
        # of these reach check_github_repo_and_app on a token that is scoped but
        # not SSO-authorised for the org.
        "gh: Must have admin rights to Repository. (HTTP 403)",
        "gh: Resource protected by organization SAML enforcement. You must grant your OAuth "
        "token access to this organization. (HTTP 403)",
    )

    ABSENCES = (
        "ERROR: (gcloud.storage.buckets.describe) HTTPError 404: The specified bucket does not exist.",
        "ERROR: (gcloud.artifacts.repositories.describe) NOT_FOUND: Repository does not exist",
        "ERROR: (gcloud.kms.keys.describe) NOT_FOUND: CryptoKey not found",
        # Observed on 2026-08-27. An absent service account answers NOT_FOUND
        # even when the caller cannot read the project it would live in, so the
        # two SA checks may still report a genuinely missing GSA as missing.
        "ERROR: (gcloud.iam.service-accounts.get-iam-policy) NOT_FOUND: Unknown service account.",
        "boom",
        "",
    )

    # Read did not happen, and permissions were not the reason.
    TRANSIENTS = (
        "timed out after 120s: gcloud projects describe p",
        # Observed 2026-08-27 from a gcloud whose refresh token had lapsed. The
        # account still printed as ACTIVE under `gcloud auth list`, which is the
        # case check_toolchain's docstring says it cannot catch.
        "ERROR: (gcloud.projects.describe) There was a problem refreshing your current auth "
        "tokens: ('invalid_grant: Bad Request', {'error': 'invalid_grant', "
        "'error_description': 'Bad Request'})",
        "ERROR: (gcloud.container.clusters.list) Reauthentication required.",
    )

    def test_refusals_are_recognised(self):
        for err in self.REFUSALS:
            with self.subTest(err=err[:60]):
                self.assertIsNotNone(checker._denial_reason(err), err)

    def test_absences_are_not_mistaken_for_refusals(self):
        for err in self.ABSENCES + self.TRANSIENTS:
            with self.subTest(err=err[:60]):
                self.assertIsNone(checker._denial_reason(err), err)

    def test_a_read_that_did_not_happen_is_never_read_as_absence(self):
        for err in self.REFUSALS + self.TRANSIENTS:
            with self.subTest(err=err[:60]):
                self.assertIsNotNone(checker._unread_reason(err), err)

    def test_a_genuine_absence_stays_an_absence(self):
        for err in self.ABSENCES:
            with self.subTest(err=err[:60]):
                self.assertIsNone(checker._unread_reason(err), err)

    def test_a_timeout_does_not_report_the_resource_as_missing(self):
        # A 120s stall on a bucket that exists used to append "Missing Terraform
        # state bucket" and exit 1. A read that did not happen is not evidence.
        details, warnings = [], []
        self.assertTrue(
            checker._record_unreadable(
                "timed out after 120s: gcloud storage buckets describe gs://p-tf-state",
                "Missing Terraform state bucket",
                "state bucket not checked",
                details,
                warnings,
            )
        )
        self.assertEqual(details, [])
        self.assertEqual(len(warnings), 1)

    def test_a_project_number_containing_403_is_not_a_denial(self):
        # `403` as a bare substring appears in project numbers, bucket names and
        # image digests. Matching it would turn arbitrary absences into
        # "unverified" and quietly stop the script failing anything.
        self.assertIsNone(checker._denial_reason("NOT_FOUND: project 403829105 has no such bucket"))

    def test_record_routes_a_denial_to_warnings_and_keeps_the_check_passing(self):
        details, warnings = [], []
        denied = checker._record_unreadable(
            "PERMISSION_DENIED: nope", "Missing thing", "Thing not checked", details, warnings
        )
        self.assertTrue(denied)
        self.assertEqual([], details)
        self.assertEqual(1, len(warnings))
        self.assertIn("Thing not checked", warnings[0])

    def test_record_routes_an_absence_to_details_and_fails_the_check(self):
        details, warnings = [], []
        denied = checker._record_unreadable(
            "NOT_FOUND", "Missing thing", "Thing not checked", details, warnings
        )
        self.assertFalse(denied)
        self.assertEqual(["Missing thing"], details)
        self.assertEqual([], warnings)


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

    def test_denied_project_describe_is_unverified_not_failed(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _fail("ERROR: (gcloud.projects.describe) User [x] does not have permission to access "
                      "projects instance [kube-agents-evals-6] (or it may not exist)")
            ]
            number, result = checker.check_project_and_apis("kube-agents-evals-6")
        self.assertIsNone(number)
        self.assertTrue(result.passed, result.details)
        self.assertTrue(result.warnings)

    def test_absent_project_still_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_fail("ERROR: (gcloud.projects.describe) NOT_FOUND: project not found")]
            _, result = checker.check_project_and_apis("kube-agents-evals-99")
        self.assertFalse(result.passed)

    def test_denied_service_list_is_unverified_and_keeps_the_project_number(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"projectNumber": "123456"})),
                _fail("ERROR: (gcloud.services.list) PERMISSION_DENIED: Permission denied to list services"),
            ]
            number, result = checker.check_project_and_apis("kube-agents-evals-6")
        self.assertEqual("123456", number)
        self.assertTrue(result.passed, result.details)
        self.assertIn("not checked", result.message)

    def test_a_timed_out_project_describe_is_unverified_not_failed(self):
        # A read that did not happen, filed the same way whatever stopped it.
        # These two call sites classified with _denial_reason alone until the
        # #1008 review, so a timeout or a mid-run credential expiry reported
        # "Project describe failed" -- exit 1 for a project nothing was learned
        # about, plus two derived checks skipped behind it.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_fail("timed out after 120s: gcloud projects describe p")]
            number, result = checker.check_project_and_apis("kube-agents-evals-6")
        self.assertIsNone(number)
        self.assertTrue(result.passed, result.details)
        self.assertTrue(result.warnings)

    def test_a_lapsed_credential_on_the_service_list_is_unverified_not_failed(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"projectNumber": "123456"})),
                _fail("ERROR: (gcloud.services.list) There was a problem refreshing your current "
                      "auth tokens: ('invalid_grant: Bad Request', {'error': 'invalid_grant'})"),
            ]
            number, result = checker.check_project_and_apis("kube-agents-evals-6")
        self.assertEqual("123456", number)
        self.assertTrue(result.passed, result.details)
        self.assertIn("not checked", result.message)


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
            run.side_effect = [
                _ok(self._clusters("ENCRYPTED")),
                _fail("ERROR: (gcloud.storage.buckets.describe) HTTPError 404: The specified bucket "
                      "does not exist."),
            ]
            result = checker.check_gke_and_state("kube-agents-evals-3")
        self.assertFalse(result.passed)
        self.assertTrue(any("state bucket" in d for d in result.details), result.details)

    def test_denied_state_bucket_is_unverified_not_missing(self):
        # The finding that opened #1004. `buckets describe` needs
        # storage.buckets.get; `storage ls` does not. The account that produced
        # this could list three prefixes inside the bucket the script had just
        # called missing.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._clusters("ENCRYPTED")),
                _fail("ERROR: (gcloud.storage.buckets.describe) HTTPError 403: x@google.com does not "
                      "have storage.buckets.get access to the Google Cloud Storage bucket."),
            ]
            result = checker.check_gke_and_state("kube-agents-evals-6")
        self.assertTrue(result.passed, result.details)
        self.assertFalse(any("Missing Terraform state bucket" in d for d in result.details), result.details)
        self.assertTrue(any("not checked" in w for w in result.warnings), result.warnings)
        self.assertIn("not checked", result.message)
        self.assertNotIn("state bucket present", result.message)

    def test_denied_cluster_list_is_unverified_not_missing_clusters(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _fail('ERROR: (gcloud.container.clusters.list) ResponseError: code=403, message='
                      'Required "container.clusters.list" permission(s) for "projects/p".'),
                _ok("bucket"),
            ]
            result = checker.check_gke_and_state("kube-agents-evals-6")
        self.assertTrue(result.passed, result.details)
        self.assertFalse(any("Missing GKE cluster" in d for d in result.details), result.details)
        self.assertIn("not checked", result.message)

    def test_cluster_list_failing_for_another_reason_still_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _fail("ERROR: (gcloud.container.clusters.list) NOT_FOUND: Project "
                      "'kube-agents-evals-3' not found or deleted."),
                _ok("bucket"),
            ]
            result = checker.check_gke_and_state("kube-agents-evals-3")
        self.assertFalse(result.passed)

    def test_a_disabled_container_api_reports_unchecked_and_the_api_check_fails_it(self):
        # gcloud reports a disabled Kubernetes Engine API as `code=403`, so this
        # check cannot tell it from a caller who may not list clusters and says
        # "not checked" for both. That is the right answer here and the wrong
        # verdict overall, which is why it is check_project_and_apis that fails
        # the project: REQUIRED_APIS reads the enabled-services list directly.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _fail("ERROR: (gcloud.container.clusters.list) ResponseError: code=403, "
                      "message=Kubernetes Engine API has not been used in project 12345 before "
                      "or it is disabled."),
                _ok("bucket"),
            ]
            result = checker.check_gke_and_state("kube-agents-evals-3")
        self.assertTrue(result.passed, result.details)
        self.assertIn("not checked", result.message)
        self.assertIn("container.googleapis.com", checker.REQUIRED_APIS)


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

    def test_unresolved_cluster_is_unverified_not_failed(self):
        # Changed deliberately: this asserted that an unresolved cluster fails.
        # The script's own two counts already separate "could not reach" from
        # "looked and it was not there", and only the second is evidence about
        # the project. A credential without container.clusters.get fails every
        # resolve and arrives here as 0/7 written -- the exact shape a healthy
        # kube-agents-evals-6 produced while it was passing a 13-task presubmit.
        #
        # The warning lines matter and are not decoration: the count alone no
        # longer earns the excuse, because a count alone is also what a slot
        # that lost its labels produces. See the test below.
        stderr = "\n".join([
            f"WARNING: no credentials for seeded cluster seeded-{slot} in kube-agents-evals-5: "
            f'code=403, message=Required "container.clusters.get" permission(s).'
            for slot in ("a", "b", "c")
        ] + [self._summary(0, unresolved=self._roles())])
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("v1.30.0"), (0, "", stderr)]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertTrue(result.passed, result.details)
        self.assertTrue(result.warnings)
        self.assertIn("not checked", result.message)

    def test_unresolved_with_no_warning_at_all_still_fails(self):
        # The hole the excuse opened, and the reason it now wants positive
        # evidence rather than the absence of a contrary warning. A seeded
        # cluster that keeps its name and loses its labels never enters the
        # listing: no per-cluster warning names it (:215 fires only for a
        # LABELLED cluster), `labelled` stays non-zero so :406 is silent, the
        # other slots resolve so :411 is silent, and its roles increment
        # `unresolved` with nothing printed. check_gke_and_state matches by
        # name and passes it, so this check is the only one that can fail it.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok("v1.30.0"),
                (0, "", self._summary(self._roles() - 2, unresolved=2)),
            ]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertFalse(result.passed, result.message)

    def test_a_skipped_cluster_is_a_visibility_limit_too(self):
        # hack/fleet-kubeconfigs.sh:386. Not a refusal, but the slot ends up
        # with no kubeconfig for a reason that says nothing about the fleet.
        stderr = "\n".join([
            "WARNING: could not create a temporary file; skipping seeded cluster seeded-a",
            self._summary(self._roles() - 1, unresolved=1),
        ])
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("v1.30.0"), (0, "", stderr)]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertTrue(result.passed, result.details)

    def test_unlabelled_fleet_still_fails_though_every_role_is_unresolved(self):
        # The absent/misconfigured fleet, which arrives in the same "unresolved"
        # count as a refused one. check_gke_and_state cannot be the backstop
        # here: it matches EXPECTED_CLUSTERS by NAME, while the fleet script
        # discovers by the environment/managed-by labels, so a cluster that kept
        # its name and lost its label passes there and is unresolved here.
        stderr = "\n".join([
            "WARNING: project kube-agents-evals-5 carries no clusters labelled "
            "environment=seeded,managed-by=kube-agents-seeded-fleet.",
            self._summary(0, unresolved=self._roles()),
        ])
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("v1.30.0"), (0, "", stderr)]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertFalse(result.passed, result.message)

    def test_a_cluster_matching_no_catalog_slot_still_fails(self):
        stderr = "\n".join([
            "WARNING: seeded cluster seeded-z in kube-agents-evals-5 matches no slot the "
            "catalog declares; ignoring it",
            self._summary(0, unresolved=self._roles()),
        ])
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("v1.30.0"), (0, "", stderr)]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertFalse(result.passed, result.message)

    def test_unreachable_clusters_are_one_unverified_item_not_one_per_cluster(self):
        # report() counts warnings to fill in "N item(s) could not be checked".
        # Three unreachable clusters are evidence for a single item -- this
        # project's seeded fleet -- so folding them in keeps the banner honest.
        stderr = "\n".join([
            f"WARNING: no credentials for seeded cluster seeded-{slot} in kube-agents-evals-5: "
            f'code=403, message=Required "container.clusters.get" permission(s).'
            for slot in ("a", "b", "c")
        ] + [self._summary(0, unresolved=self._roles())])
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("v1.30.0"), (0, "", stderr)]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertTrue(result.passed, result.details)
        self.assertEqual(1, len(result.warnings), result.warnings)
        for slot in ("a", "b", "c"):
            self.assertIn(f"seeded-{slot}", result.warnings[0])

    def test_unplanted_alongside_unresolved_still_fails(self):
        # One role looked at and absent is a finding, whatever else went
        # unreached. The unverified path above must not swallow it.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok("v1.30.0"),
                (0, "", self._summary(self._roles() - 2, unresolved=1, unplanted=1)),
            ]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertFalse(result.passed)

    def test_script_refused_is_unverified_not_failed(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok("v1.30.0"),
                (1, "", 'ERROR: Required "container.clusters.get" permission(s) for "projects/p".'),
            ]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertTrue(result.passed, result.details)
        self.assertTrue(any("container.clusters.get" in w for w in result.warnings), result.warnings)

    def test_script_timing_out_is_unverified_not_failed(self):
        # A fleet script that never finished says nothing about the fleet, and
        # it is the likeliest non-zero exit here: it walks every seeded cluster
        # with a get-credentials each. Classified with _denial_reason alone it
        # was a hard failure, which is the same wrong answer this file was
        # opened to remove, arriving one exit code later.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok("v1.30.0"),
                (124, "", "timed out after 300s: hack/fleet-kubeconfigs.sh"),
            ]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertTrue(result.passed, result.details)
        self.assertTrue(any("timed out" in w for w in result.warnings), result.warnings)

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

    def test_the_fleet_warning_phrases_are_the_ones_the_script_prints(self):
        # Same standard as the test above, for the three regexes that decide
        # fail against unverified. A phrase reworded in hack/fleet-kubeconfigs.sh
        # and not here stops matching in silence, and it breaks both ways now:
        # a _FLEET_LOOKED_AND_FOUND_WRONG phrase that stops matching drops every
        # genuinely absent fleet from exit 1 to exit 2, and a _FLEET_UNREACHABLE
        # one that stops matching fails every project whose clusters were merely
        # refused -- the bug this file exists to remove.
        text = checker._FLEET_KUBECONFIGS.read_text(encoding="utf-8")
        wrong = checker._FLEET_LOOKED_AND_FOUND_WRONG.pattern.split("|")
        unreachable = checker._FLEET_UNREACHABLE.pattern.split("|")
        self.assertEqual(4, len(wrong))
        self.assertEqual(2, len(unreachable))
        for phrase in [*wrong, *unreachable, checker._FLEET_COULD_NOT_LOOK.pattern]:
            with self.subTest(phrase=phrase):
                self.assertRegex(text, phrase)

    def test_a_refused_cluster_listing_is_not_read_as_an_absent_fleet(self):
        # A refused `clusters list` leaves hack/fleet-kubeconfigs.sh with an
        # empty listing, so it goes on to print the same "carries no clusters
        # labelled" warning it prints for a project that genuinely has none.
        # Failing on that string alone reintroduces, in this check, the bug the
        # rest of this file exists to remove. Only the first line separates them.
        err = (
            "WARNING: could not list clusters in kube-agents-evals-5; every fleet check will "
            "report status=error\n"
            "WARNING: project kube-agents-evals-5 carries no clusters labelled "
            "environment=seeded,managed-by=kube-agents-seeded-fleet.\n"
            + self._summary(0, unresolved=self._roles())
        )
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok("v1.30.0"), (0, "", err)]
            result = checker.check_seeded_fleet_fixtures("kube-agents-evals-5")
        self.assertTrue(result.passed, result.details)
        self.assertIn("not checked", result.message)


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
        # A policy read that failed for a reason that is not permissions. There
        # is nothing for an operator to go and confirm by hand, so this stays a
        # failure -- the counterpart to the denial case below, which does not.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [_ok(json.dumps(self._REPO)), _fail("boom"), _fail("boom")]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("Could not read any IAM policy" in d for d in result.details), result.details)

    def test_one_policy_denied_and_the_other_empty_does_not_accuse(self):
        # The half-read case. `policy_read` is True because the repo policy came
        # back, but the grants provision_ci_pool_project.sh makes are
        # project-level, so the refused half is the half that holds them. An
        # empty repo policy plus a refused project policy looks exactly like a
        # project with no push rights, and reporting it as one is the accusation
        # this whole change exists to stop.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _fail("ERROR: (gcloud.projects.get-iam-policy) PERMISSION_DENIED: Permission "
                      "'resourcemanager.projects.getIamPolicy' denied on resource"),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)
        self.assertFalse(any("image push" in d for d in result.details), result.details)
        self.assertTrue(any("push rights were not checked" in w for w in result.warnings), result.warnings)

    def test_one_policy_denied_still_passes_on_a_grant_the_other_holds(self):
        # A binding found settles the question even from a partial read, so a
        # refusal must not turn a conclusive pass into an unchecked item.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _fail("ERROR: PERMISSION_DENIED: Permission 'resourcemanager.projects.getIamPolicy' denied"),
                _ok(self._push_and_pull()),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)
        self.assertFalse(any("push rights" in w for w in result.warnings), result.warnings)

    def test_one_policy_failing_for_another_reason_still_accuses(self):
        # Not a denial, so there is nothing for an operator to confirm by hand
        # and the absence stays a finding -- but the message says the read was
        # partial rather than presenting it as a complete picture.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _fail("boom"),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("partial policy" in d for d in result.details), result.details)

    def test_denied_policies_are_unverified_not_failed(self):
        # Both reads refused. Nothing is known about push rights either way, and
        # a caller who cannot read a project's IAM policy has learned nothing
        # about whether Cloud Build can push to it.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _fail("ERROR: PERMISSION_DENIED: Permission 'resourcemanager.projects.getIamPolicy' denied"),
                _fail("ERROR: PERMISSION_DENIED: Permission 'artifactregistry.repositories.getIamPolicy' denied"),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)
        self.assertTrue(any("not checked" in w for w in result.warnings), result.warnings)
        self.assertIn("not checked", result.message)
        self.assertNotIn("push rights, and node pull rights", result.message)

    def test_timed_out_policies_are_unverified_not_failed(self):
        # Same conclusion as the pair of refusals above, from the pair of reads
        # that never happened. This is the site where the two classifiers had to
        # stay apart -- policy_errors still feeds the "partial policy" wording --
        # so the fix routes unreads into policy_denials rather than widening
        # what "denied" means. Before it, two timeouts hit policy_errors and
        # printed "Could not read any IAM policy" as a hard failure.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _fail("timed out after 120s: gcloud projects get-iam-policy kube-agents-evals-3"),
                _fail("ERROR: (gcloud.artifacts.repositories.get-iam-policy) There was a problem "
                      "refreshing your current auth tokens: invalid_grant"),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)
        self.assertFalse(any("Could not read any IAM policy" in d for d in result.details), result.details)
        self.assertIn("not checked", result.message)

    def test_a_timed_out_policy_read_is_not_reported_as_a_partial_read(self):
        # The half-and-half case, and the reason the routing had to preserve the
        # policy_denials/policy_errors split rather than merge the buckets: the
        # "partial policy" wording is for a read that produced a usable answer
        # alongside one that broke, and a timeout produced no answer at all.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps(self._REPO)),
                _fail("timed out after 120s: gcloud projects get-iam-policy kube-agents-evals-3"),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)
        self.assertFalse(any("image push" in d for d in result.details), result.details)
        self.assertTrue(any("push rights were not checked" in w for w in result.warnings), result.warnings)

    def test_denied_repository_describe_is_unverified_not_missing(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _fail(
                    "ERROR: (gcloud.artifacts.repositories.describe) PERMISSION_DENIED: Permission "
                    "'artifactregistry.repositories.get' denied on resource (or it may not exist)."
                ),
                _ok(self._push_and_pull()),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)
        self.assertFalse(any("Missing Artifact Registry" in d for d in result.details), result.details)
        self.assertIn("not checked", result.message)

    def test_absent_repository_still_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _fail("ERROR: (gcloud.artifacts.repositories.describe) NOT_FOUND: Repository does not exist"),
                _ok(self._push_and_pull()),
                _ok(self._EMPTY),
                _ok(self._NODES),
            ]
            result = checker.check_artifact_registry("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("Missing Artifact Registry" in d for d in result.details), result.details)

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

    def test_uninstalled_app_still_fails(self):
        # An org with no installations answers 200 with an empty list, so this
        # really is "the App is not installed" and must keep failing.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"isPrivate": True, "name": "kube-agents-evals-3-infra"})),
                _ok(""),
            ]
            result = checker.check_github_repo_and_app("kube-agents-evals-3", self._APP_ID)
        self.assertFalse(result.passed)
        self.assertTrue(any("installation not found" in d for d in result.details), result.details)

    def test_token_without_admin_org_is_unverified_not_an_uninstalled_app(self):
        # GET /orgs/{org}/installations needs admin:org and answers 404 -- not
        # 403 -- to a PAT carrying repo,workflow. Reading that as "not
        # installed" names a correctly configured org as the defect.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"isPrivate": True, "name": "kube-agents-evals-6-infra"})),
                (1, "", "gh: Not Found (HTTP 404)"),
            ]
            result = checker.check_github_repo_and_app("kube-agents-evals-6", self._APP_ID)
        self.assertTrue(result.passed, result.details)
        self.assertFalse(any("installation not found" in d for d in result.details), result.details)
        self.assertTrue(any("admin:org" in w for w in result.warnings), result.warnings)
        self.assertIn("NOT verified", result.message)

    def test_a_timed_out_installations_lookup_is_unverified_not_an_uninstalled_app(self):
        # The same manufactured claim as the test above, reached by a different
        # road: this call site classified with _denial_reason alone until the
        # #1008 review, so a `gh api` that never answered produced the flat
        # assertion "GitHub App <id> installation not found on org gke-agentic"
        # about an org nothing had been read from.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"isPrivate": True, "name": "kube-agents-evals-6-infra"})),
                (124, "", "timed out after 120s: gh api /orgs/gke-agentic/installations"),
            ]
            result = checker.check_github_repo_and_app("kube-agents-evals-6", self._APP_ID)
        self.assertTrue(result.passed, result.details)
        self.assertFalse(any("installation not found" in d for d in result.details), result.details)
        self.assertIn("NOT verified", result.message)

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

    def test_denied_kms_reads_are_unverified_not_an_unprovisioned_minter(self):
        denied = _fail("ERROR: (gcloud.kms.keys.versions.list) PERMISSION_DENIED: Permission "
                       "'cloudkms.cryptoKeyVersions.list' denied on resource")
        result = self._run(versions=denied, key=denied, key_policy=denied, gsa_policy=denied)
        self.assertTrue(result.passed, result.details)
        self.assertEqual([], result.details)
        self.assertEqual(4, len(result.warnings))
        self.assertNotIn("ENABLED", result.message)
        # Every partial summary in this file reads the same way: what was
        # verified, then what was not, comma-separated within each half. Three
        # checks used to phrase it three ways and the operator had to work out
        # which of "verified", "present" and "partly verified" meant the same
        # thing. Nothing was verified here, so the first half is absent.
        self.assertEqual(
            "the imported key versions, the key's purpose, algorithm and import-only setting, "
            "the minter GSA's signing rights, the minter GSA's Workload Identity binding "
            "not checked",
            result.message,
        )

    def test_a_partial_summary_names_both_halves(self):
        self.assertEqual(
            "a, c verified; b not checked",
            checker._partial_summary([("a", True), ("b", False), ("c", True)]),
        )
        # Empty means "everything was checked": the caller says so in its own
        # words rather than printing a bare "verified" with nothing after it.
        self.assertEqual("", checker._partial_summary([("a", True), ("b", True)]))

    def test_absent_kms_key_still_fails(self):
        result = self._run(versions=_fail("ERROR: (gcloud.kms.keys.versions.list) NOT_FOUND: CryptoKey "
                                          "projects/p/locations/l/keyRings/r/cryptoKeys/k not found"))
        self.assertFalse(result.passed)
        self.assertTrue(any("not found or error" in d for d in result.details), result.details)

    def test_one_denied_read_does_not_hide_a_real_failure_in_another(self):
        # A partial denial must not turn a genuine finding into a pass.
        result = self._run(
            key_policy=_fail("PERMISSION_DENIED: cannot read key policy"),
            gsa_policy=_ok(self._gsa_policy(member="serviceAccount:wrong@example.iam.gserviceaccount.com")),
        )
        self.assertFalse(result.passed)
        self.assertTrue(any("Workload Identity" in d for d in result.details), result.details)

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


_FAKE_PEM = "-----BEGIN RSA PRIVATE KEY-----\nnot-a-key\n-----END RSA PRIVATE KEY-----\n"


class LedgerAppKeyReadTest(unittest.TestCase):
    """Reading the App's private key out of the build cluster.

    A None pem must always carry a reason, because the reason is the whole of
    what the operator is told when the check reports unverified.
    """

    def _read(self, *results):
        with mock.patch.object(checker, "run_cmd", side_effect=list(results)):
            return checker._read_ledger_app_key()

    def test_reads_and_decodes_the_secret(self):
        encoded = base64.b64encode(_FAKE_PEM.encode()).decode()
        pem, reason = self._read((0, "", ""), (0, encoded, ""))
        self.assertEqual(_FAKE_PEM, pem)
        self.assertEqual("", reason)

    def test_the_cluster_is_named_by_its_gke_name_not_the_prow_alias(self):
        # `build-kube-agents` is the prowjob's cluster: field. There is no GKE
        # cluster by that name, and get-credentials on it fails.
        calls = []

        def run_cmd(cmd, **kw):
            calls.append(cmd)
            return (0, base64.b64encode(_FAKE_PEM.encode()).decode(), "") if len(calls) > 1 else (0, "", "")

        with mock.patch.object(checker, "run_cmd", side_effect=run_cmd):
            checker._read_ledger_app_key()
        context = " ".join(calls[1])
        self.assertIn(checker.PROW_BUILD_CLUSTER, context)
        self.assertNotIn("build-kube-agents", context)

    def test_absent_kubectl_is_a_reason_not_a_crash(self):
        pem, reason = self._read((127, "", "no kubectl"))
        self.assertIsNone(pem)
        self.assertIn("kubectl", reason)

    def test_a_refused_read_says_so_rather_than_calling_the_secret_absent(self):
        # Real kubectl RBAC text, verbatim: it carries no status code, so the
        # invented `403 Forbidden` this used to assert on was matching a pattern
        # production never sees.
        pem, reason = self._read((0, "", ""), (1, "", (
            f'Error from server (Forbidden): secrets "{checker.LEDGER_KEY_SECRET}" is forbidden: '
            'User "operator@example.com" cannot get resource "secrets" in API group "" '
            'in the namespace "test-pods"')))
        self.assertIsNone(pem)
        self.assertIn("refused", reason)

    def test_a_missing_entry_reads_as_empty_not_as_an_error(self):
        # kubectl exits 0 with empty output when the jsonpath misses, so an
        # absent key.pem would otherwise decode to an empty PEM and sign nothing.
        pem, reason = self._read((0, "", ""), (0, "", ""))
        self.assertIsNone(pem)
        self.assertIn(checker.LEDGER_KEY_SECRET_ENTRY, reason)

    def test_a_missing_context_names_the_get_credentials_command(self):
        pem, reason = self._read((0, "", ""), (1, "", 'error: context "gke_x" does not exist'))
        self.assertIsNone(pem)
        self.assertIn("get-credentials", reason)

    def test_undecodable_material_is_a_reason_not_a_traceback(self):
        pem, reason = self._read((0, "", ""), (0, "!!!not base64!!!", ""))
        self.assertIsNone(pem)
        self.assertIn(checker.LEDGER_KEY_SECRET_ENTRY, reason)


class LedgerTokenMintTest(unittest.TestCase):
    """Trading the App key for an installation token."""

    def _mint(self, sign_rc=0, urlopen=None):
        def run_cmd(cmd, **kw):
            if sign_rc == 0:
                with open(cmd[cmd.index("-out") + 1], "wb") as fh:
                    fh.write(b"signature-bytes")
            return sign_rc, "", "openssl said no"

        opener = urlopen or (lambda *a, **kw: _Response({"token": "ghs_minted", "expires_at": "z"}))
        with mock.patch.object(checker, "run_cmd", side_effect=run_cmd), \
             mock.patch.object(checker.urllib.request, "urlopen", opener):
            return checker._mint_ledger_token(_FAKE_PEM)

    def _http_error(self, code, reason="err"):
        def raise_it(*a, **kw):
            raise urllib.error.HTTPError("u", code, reason, {}, None)

        return raise_it

    def test_returns_the_token_on_success(self):
        token, status, message = self._mint()
        self.assertEqual("ghs_minted", token)
        self.assertEqual("ok", status)
        self.assertEqual("", message)

    def test_posts_to_the_installation_this_script_names(self):
        seen = {}

        def urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            return _Response({"token": "ghs_minted"})

        self._mint(urlopen=urlopen)
        self.assertIn(str(checker.LEDGER_INSTALLATION_ID), seen["url"])
        self.assertEqual("POST", seen["method"])

    def test_the_jwt_is_issued_by_the_ledger_app(self):
        captured = {}

        def urlopen(request, timeout=None):
            jwt = request.get_header("Authorization").split()[1]
            captured["claims"] = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))
            return _Response({"token": "ghs_minted"})

        self._mint(urlopen=urlopen)
        self.assertEqual(str(checker.LEDGER_APP_ID), captured["claims"]["iss"])
        # GitHub rejects an App JWT more than ten minutes out.
        self.assertLess(captured["claims"]["exp"] - int(time.time()), 600)

    def test_a_wrong_key_fails_rather_than_reporting_unverified(self):
        token, status, message = self._mint(urlopen=self._http_error(401, "Unauthorized"))
        self.assertIsNone(token)
        self.assertEqual("failed", status)
        self.assertIn("401", message)

    def test_a_missing_installation_fails(self):
        _, status, message = self._mint(urlopen=self._http_error(404, "Not Found"))
        self.assertEqual("failed", status)
        self.assertIn(str(checker.LEDGER_INSTALLATION_ID), message)

    def test_an_installation_scoped_to_all_repositories_fails(self):
        # The read App's containment boundary. `all` reads this project's issues
        # fine, so every check below would pass while the scope was gone.
        token, status, message = self._mint(
            urlopen=lambda *a, **kw: _Response(
                {"token": "ghs_minted", "repository_selection": "all"}))
        self.assertIsNone(token)
        self.assertEqual("failed", status)
        self.assertIn("repository_selection", message)

    def test_only_all_trips_the_scope_guard(self):
        for selection in ("selected", None):
            body = {"token": "ghs_minted"}
            if selection:
                body["repository_selection"] = selection
            with self.subTest(selection=selection):
                token, status, _ = self._mint(urlopen=lambda *a, **kw: _Response(dict(body)))
                self.assertEqual("ghs_minted", token)
                self.assertEqual("ok", status)

    def test_a_server_error_is_unverified_not_failed(self):
        _, status, _ = self._mint(urlopen=self._http_error(503, "Service Unavailable"))
        self.assertEqual("unverified", status)

    def test_absent_openssl_is_unverified_not_a_bad_key(self):
        _, status, message = self._mint(sign_rc=127)
        self.assertEqual("unverified", status)
        self.assertIn("openssl", message)

    def test_a_key_openssl_rejects_fails(self):
        _, status, _ = self._mint(sign_rc=1)
        self.assertEqual("failed", status)

    def test_the_pem_never_reaches_a_message(self):
        _, _, message = self._mint(sign_rc=1)
        self.assertNotIn("not-a-key", message)


class LedgerReadCredentialTest(unittest.TestCase):
    """The grading credential, which is not the minter App and not the operator's own login."""

    def _check(self, urlopen, pem=_FAKE_PEM, key_reason="kubectl is not on PATH",
               mint=("ghs_fake", "ok", ""), project="kube-agents-evals-7"):
        with mock.patch.object(checker, "_read_ledger_app_key",
                               return_value=(pem, "" if pem else key_reason)), \
             mock.patch.object(checker, "_mint_ledger_token", return_value=mint), \
             mock.patch.object(checker.urllib.request, "urlopen", urlopen):
            return checker.check_ledger_read_credential(project)

    def _http_error(self, code, reason="err", headers=None):
        def raise_it(*a, **kw):
            raise urllib.error.HTTPError("u", code, reason, headers or {}, None)

        return raise_it

    def test_readable_issues_pass(self):
        seen = {}

        def urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            return _Response([])

        result = self._check(urlopen)
        self.assertTrue(result.passed)
        self.assertEqual([], result.warnings)
        self.assertIn("gke-agentic/kube-agents-evals-7-infra", seen["url"])
        self.assertEqual("Bearer ghs_fake", seen["auth"])

    def test_empty_issue_list_is_still_a_pass(self):
        # The question is whether the read is permitted. A pool repository has no
        # ledger issue until its first lease publishes one, so requiring content
        # would fail every project this check is run on.
        self.assertTrue(self._check(lambda *a, **kw: _Response([])).passed)

    def test_repo_outside_the_installation_fails(self):
        # kube-agents-evals-6's first lease, exactly: everything provisioned, the
        # ledger filed, and 404 on the read back.
        result = self._check(self._http_error(404, "Not Found"))
        self.assertFalse(result.passed)
        self.assertIn("404", " ".join(result.details))

    def test_repo_reachable_without_issues_read_fails(self):
        result = self._check(self._http_error(403, "Forbidden"))
        self.assertFalse(result.passed)
        self.assertIn("issues: read", " ".join(result.details))

    def test_rate_limited_403_is_unverified_not_failed(self):
        result = self._check(self._http_error(403, "rate limit exceeded", {"x-ratelimit-remaining": "0"}))
        self.assertTrue(result.passed)
        self.assertTrue(result.warnings)

    def test_server_error_is_unverified_not_failed(self):
        result = self._check(self._http_error(503, "Service Unavailable"))
        self.assertTrue(result.passed)
        self.assertTrue(result.warnings)

    def test_untrusted_ca_names_the_cert_bundle_not_a_firewall(self):
        def untrusted(*a, **kw):
            raise urllib.error.URLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

        result = self._check(untrusted)
        self.assertTrue(result.passed)
        self.assertIn("SSL_CERT_FILE", " ".join(result.warnings))

    def test_an_unreadable_key_is_unverified_and_reads_nothing(self):
        # The operator is an org member, so falling back to their own login would
        # answer 200 for a repository the CI credential cannot see.
        def fail_if_called(*a, **kw):
            raise AssertionError("no request may be made without the CI credential")

        result = self._check(fail_if_called, pem=None)
        self.assertTrue(result.passed)
        self.assertIn("kubectl is not on PATH", " ".join(result.warnings))
        self.assertIn(checker.LEDGER_KEY_SECRET, " ".join(result.warnings))

    def test_a_failed_mint_fails_the_check(self):
        def fail_if_called(*a, **kw):
            raise AssertionError("nothing may be read without a token")

        result = self._check(fail_if_called, mint=(None, "failed", "the stored key is wrong"))
        self.assertFalse(result.passed)
        self.assertIn("the stored key is wrong", " ".join(result.details))

    def test_an_unverified_mint_is_amber_not_a_pass_claim(self):
        def fail_if_called(*a, **kw):
            raise AssertionError("nothing may be read without a token")

        result = self._check(fail_if_called, mint=(None, "unverified", "GitHub answered HTTP 502"))
        self.assertTrue(result.passed)
        self.assertIn("502", " ".join(result.warnings))

    def test_no_environment_variable_can_stand_in_for_the_cluster_key(self):
        # bench's verifier falls back to GITHUB_TOKEN; this check must not. On a
        # laptop that variable is the operator's PAT, and a pass read with it says
        # nothing about what CI can read.
        def fail_if_called(*a, **kw):
            raise AssertionError("GITHUB_TOKEN must not be used here")

        env = {"GITHUB_TOKEN": "ghp_operator", "BENCH_GITHUB_TOKEN": "ghp_operator"}
        with mock.patch.dict(checker.os.environ, env, clear=True):
            result = self._check(fail_if_called, pem=None)
        self.assertTrue(result.warnings)

    def test_the_token_never_reaches_a_message(self):
        result = self._check(self._http_error(403, "Forbidden"), mint=("ghs_secret_value", "ok", ""))
        printed = " ".join([result.message] + result.details + result.warnings)
        self.assertNotIn("ghs_secret_value", printed)


class LedgerCredentialMatchesCiEvalPrTest(unittest.TestCase):
    """This check must attest the credential hack/ci-eval-pr.sh actually mints.

    The App, its installation, and the variable the token lands in are written
    in three files that do not read each other -- here, hack/ci-eval-pr.sh, and
    bench/kube_agents_bench/verifiers.py. Change one and this check goes on
    reporting a project healthy against a credential CI no longer uses. Parsed
    rather than imported: the verifier is deliberately dependency-free, bench is
    an installable package, and the third file is shell.
    """

    def setUp(self):
        self.script = (checker._ROOT / "hack" / "ci-eval-pr.sh").read_text()

    def _default(self, name):
        m = re.search(rf'^export {name}="\$\{{{name}:-([^}}]+)\}}"', self.script, re.M)
        self.assertIsNotNone(m, f"could not find the {name} default in hack/ci-eval-pr.sh")
        return m.group(1)

    def test_the_app_and_installation_match_the_script(self):
        self.assertEqual(str(checker.LEDGER_APP_ID), self._default("EVAL_LEDGER_APP_ID"))
        self.assertEqual(
            str(checker.LEDGER_INSTALLATION_ID), self._default("EVAL_LEDGER_INSTALLATION_ID")
        )

    def test_the_script_mints_into_the_variable_bench_reads_first(self):
        text = (checker._ROOT / "bench" / "kube_agents_bench" / "verifiers.py").read_text()
        block = re.search(r"^LEDGER_TOKEN_ENV_VARS\s*=\s*\((.*?)\)", text, re.S | re.M)
        self.assertIsNotNone(block, "could not find LEDGER_TOKEN_ENV_VARS in bench's verifiers.py")
        preferred = re.findall(r'"([^"]+)"', block.group(1))[0]
        self.assertIn(f"export {preferred}=", self.script)

    def _unit(self):
        unit = re.search(r"^run_one_unit\(\) \{.*?^\}", self.script, re.S | re.M)
        self.assertIsNotNone(unit, "could not find run_one_unit in hack/ci-eval-pr.sh")
        return unit.group(0)

    def test_a_failed_mint_does_not_fall_back_to_the_mounted_pat(self):
        # A fallback would let a smoke test pass while proving nothing about the
        # credential it was added to exercise.
        mint = re.search(r"^mint_ledger_token\(\) \{.*?^\}", self.script, re.S | re.M)
        self.assertIsNotNone(mint, "could not find mint_ledger_token in hack/ci-eval-pr.sh")
        body = mint.group(0)
        failure = re.search(r'^    if \[ "\$\{rc\}".*?^    fi', body, re.S | re.M)
        self.assertIsNotNone(failure, "could not find the branch that gives up on the mint")
        self.assertNotIn("BENCH_GITHUB_TOKEN", failure.group(0))
        # Non-zero rather than `exit`: the unit call site holds two locks by the
        # time it mints, and exiting there would strand them for lock_acquire's
        # full timeout. Each caller unwinds its own scope instead.
        self.assertIn("return 1", failure.group(0))
        self.assertIsNone(re.search(r"\bexit\b", body))
        # And the token is assigned once, below the retry loop rather than on
        # any path through it. tests/test_ci_eval_ledger_mint.py executes what
        # that loop does; this only pins where the assignment sits.
        self.assertEqual(1, body.count("export BENCH_GITHUB_TOKEN="))
        self.assertLess(body.index("\n  done"), body.index("export BENCH_GITHUB_TOKEN="))

    def test_the_preflight_mint_is_the_one_that_stops_the_run(self):
        # The other half of the rule above: a key that cannot mint at all is a
        # run-wide fault, and nothing is held here to strand.
        self.assertIn('mint_ledger_token "preflight" || exit 1', self.script)

    def test_the_unit_mints_after_it_has_taken_every_lock(self):
        # A unit can sit in lock_acquire for longer than the hour a token lasts:
        # repetitions of one task serialize on the task lock and EVAL_REPETITIONS
        # defaults to 3, so minting above the waiting hands devops-bench a token
        # that expired while the unit was queued.
        body = self._unit()
        self.assertLess(
            body.rindex("lock_acquire"),
            body.index("mint_ledger_token"),
            "run_one_unit must mint below its last lock_acquire, not above it",
        )

    def test_a_unit_that_cannot_mint_releases_what_it_holds(self):
        # Returning without releasing would park every sibling for lock_acquire's
        # timeout and grade their repetitions MISSING.
        branch = re.search(
            r"^  if ! mint_ledger_token .*?^  fi", self._unit(), re.S | re.M
        )
        self.assertIsNotNone(branch, "could not find the unit's mint-failure branch")
        self.assertEqual(2, branch.group(0).count("lock_release"))
        self.assertIn("return 0", branch.group(0))

    def test_every_bench_invocation_is_preceded_by_a_mint(self):
        # #1057 rewrote the serial repetition loop into a fan-out of background
        # subshells, and a call site left behind in the old loop would define a
        # mint nothing reaches: units would run on whatever token they inherited
        # and the check here would attest a credential CI does not use.
        body = self._unit()
        self.assertIn("mint_ledger_token", body)
        self.assertLess(
            body.index("mint_ledger_token"),
            body.index("uv run devops-bench"),
            "the unit must mint before it invokes devops-bench",
        )
        # And nowhere else runs one: a second invocation site would need its own
        # mint, and the fan-out is the only place the token is read. Counted over
        # command lines rather than the whole file, so a comment quoting the
        # command does not read as a second call site.
        sites = [
            line for line in self.script.splitlines()
            if "uv run devops-bench" in line and not line.lstrip().startswith("#")
        ]
        self.assertEqual(1, len(sites), sites)


class IamGrantsTest(unittest.TestCase):
    def _wi_policy(self, project_id):
        member = f"serviceAccount:{project_id}.svc.id.goog[kubeagents-system/kubeagents-platform-agent]"
        return json.dumps({"bindings": [{"role": "roles/iam.workloadIdentityUser", "members": [member]}]})

    def _litellm_wi_policy(self, project_id):
        member = f"serviceAccount:{project_id}.svc.id.goog[kubeagents-system/kubeagents-litellm]"
        return json.dumps({"bindings": [{"role": "roles/iam.workloadIdentityUser", "members": [member]}]})

    def _reader_policy(self, members):
        return json.dumps({"bindings": [{"role": "roles/artifactregistry.reader", "members": members}]})

    def _project_policy(
        self,
        project_id="kube-agents-evals-3",
        prow_roles=None,
        platform_roles=None,
        litellm_roles=None,
        conditional_roles=(),
        extra_bindings=(),
    ):
        """The project's own policy: all three identities holding exactly what they should."""
        prow = checker.PROW_RUNNER_ROLES if prow_roles is None else prow_roles
        platform = checker.PLATFORM_GSA_ROLES if platform_roles is None else platform_roles
        litellm = checker.LITELLM_GSA_ROLES if litellm_roles is None else litellm_roles
        platform_member = checker.PLATFORM_GSA_MEMBER_TEMPLATE.format(project_id=project_id)
        litellm_member = checker.LITELLM_GSA_MEMBER_TEMPLATE.format(project_id=project_id)
        bindings = [{"role": r, "members": [checker.PROW_RUNNER_MEMBER]} for r in sorted(prow)]
        bindings += [{"role": r, "members": [platform_member]} for r in sorted(platform)]
        bindings += [{"role": r, "members": [litellm_member]} for r in sorted(litellm)]
        bindings += [
            {
                "role": r,
                "members": [checker.PROW_RUNNER_MEMBER],
                "condition": {"title": "expires", "expression": "request.time < timestamp('2020-01-01T00:00:00Z')"},
            }
            for r in sorted(conditional_roles)
        ]
        bindings += list(extra_bindings)
        return json.dumps({"bindings": bindings})

    def _both_build_identities(self):
        return self._reader_policy(
            [
                "serviceAccount:123456@cloudbuild.gserviceaccount.com",
                "serviceAccount:123456-compute@developer.gserviceaccount.com",
            ]
        )

    def test_both_build_identities_granted_passes(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-3")),
                _ok(self._litellm_wi_policy("kube-agents-evals-3")),
                _ok(self._project_policy()),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)

    def test_missing_legacy_cloudbuild_reader_fails(self):
        # This is exactly the drift found on kube-agents-evals-2.
        project_number = "123456"
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-2")),
                _ok(self._litellm_wi_policy("kube-agents-evals-2")),
                _ok(self._project_policy("kube-agents-evals-2")),
                _ok(self._reader_policy([f"serviceAccount:{project_number}-compute@developer.gserviceaccount.com"])),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-2", project_number)
        self.assertFalse(result.passed)
        # The whole member the checker builds, not the domain it ends in. A
        # detail naming any cloudbuild SA -- another project's, or a
        # remediation hint quoting the domain -- satisfied the old spelling
        # without the reported identity being this project's. Matching a bare
        # host literal also reads to CodeQL as an incomplete URL check
        # (py/incomplete-url-substring-sanitization).
        cloudbuild_sa = f"serviceAccount:{project_number}@cloudbuild.gserviceaccount.com"
        self.assertTrue(any(cloudbuild_sa in d for d in result.details), result.details)

    def test_missing_workload_identity_binding_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(json.dumps({"bindings": []})),
                _ok(self._litellm_wi_policy("kube-agents-evals-3")),
                _ok(self._project_policy()),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("Workload Identity" in d for d in result.details), result.details)

    def test_missing_litellm_wi_binding_fails(self):
        # The gap every pool project provisioned before model_provider =
        # "vertex_ai" landed in provision_ci_pool_project.sh's tfvars carries:
        # the deploy annotates the kubeagents-litellm KSA, no binding backs
        # it, and every leased presubmit reds at the model-call gate (#1097).
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-3")),
                _ok(json.dumps({"bindings": []})),
                _ok(self._project_policy()),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(
            any("kubeagents-litellm-gsa" in d and "Workload Identity" in d for d in result.details),
            result.details,
        )

    def test_missing_litellm_gsa_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-3")),
                _fail("ERROR: (gcloud.iam.service-accounts.get-iam-policy) NOT_FOUND: Unknown "
                      "service account."),
                _ok(self._project_policy()),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(
            any("Missing GSA" in d and "kubeagents-litellm-gsa" in d for d in result.details),
            result.details,
        )

    def test_litellm_gsa_missing_role_fails(self):
        # The 2026-09-03 pool rollout's finding (#1208): the WI binding check
        # alone passed a project that would still fail at the model call for
        # want of aiplatform.user.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-3")),
                _ok(self._litellm_wi_policy("kube-agents-evals-3")),
                _ok(self._project_policy(litellm_roles=set())),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(
            any("roles/aiplatform.user" in d and "LiteLLM" in d for d in result.details),
            result.details,
        )

    def test_litellm_gsa_extra_role_fails(self):
        # Closed in both directions like the platform GSA: the gateway proxies
        # attacker-influenceable content and must hold aiplatform.user only.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-3")),
                _ok(self._litellm_wi_policy("kube-agents-evals-3")),
                _ok(self._project_policy(
                    litellm_roles=checker.LITELLM_GSA_ROLES | {"roles/container.viewer"})),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(
            any("roles/container.viewer" in d and "LiteLLM" in d for d in result.details),
            result.details,
        )

    def test_denied_litellm_policy_is_unverified_not_a_missing_gsa(self):
        # Same anti-enumeration shape as the platform GSA read below: a denied
        # read is "not checked", never "missing".
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-6")),
                _fail("ERROR: (gcloud.iam.service-accounts.get-iam-policy) PERMISSION_DENIED: Permission "
                      "iam.serviceAccounts.getIamPolicy is required to perform this operation"),
                _ok(self._project_policy("kube-agents-evals-6")),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-6", "123456")
        self.assertTrue(result.passed, result.details)
        self.assertFalse(any("Missing GSA" in d for d in result.details), result.details)

    def test_denied_gsa_policy_is_unverified_not_a_missing_gsa(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _fail("ERROR: (gcloud.iam.service-accounts.get-iam-policy) PERMISSION_DENIED: Permission "
                      "iam.serviceAccounts.getIamPolicy is required to perform this operation"),
                _ok(self._litellm_wi_policy("kube-agents-evals-6")),
                _ok(self._project_policy("kube-agents-evals-6")),
                _ok(
                    self._reader_policy(
                        [
                            "serviceAccount:123456@cloudbuild.gserviceaccount.com",
                            "serviceAccount:123456-compute@developer.gserviceaccount.com",
                        ]
                    )
                ),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-6", "123456")
        self.assertTrue(result.passed, result.details)
        self.assertFalse(any("Missing GSA" in d for d in result.details), result.details)
        self.assertIn("not checked", result.message)

    def test_denied_project_policy_is_unverified_not_missing_roles(self):
        # The read this PR adds is a third site for #1008's bug. An operator
        # without resourcemanager.projects.getIamPolicy would otherwise be told
        # both identities are missing every role and not to register the
        # project, on the strength of a read that never happened.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-6")),
                _ok(self._litellm_wi_policy("kube-agents-evals-6")),
                _fail("ERROR: (gcloud.projects.get-iam-policy) PERMISSION_DENIED: Permission "
                      "'resourcemanager.projects.getIamPolicy' denied on resource"),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-6", "123456")
        self.assertTrue(result.passed, result.details)
        self.assertFalse(any("roles/" in d for d in result.details), result.details)
        self.assertTrue(
            any("were not checked" in w for w in result.warnings), result.warnings)
        self.assertEqual(
            "the Workload Identity binding, the cross-project AR reader grants verified; "
            "the Prow runner and platform GSA project roles not checked",
            result.message,
        )

    def test_denied_cross_project_prow_policy_is_unverified(self):
        # kube-agents-prow is somebody else's project. Nobody outside Prow can
        # read its Artifact Registry policy, and that says nothing about the
        # pool project being onboarded.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-6")),
                _ok(self._litellm_wi_policy("kube-agents-evals-6")),
                _ok(self._project_policy("kube-agents-evals-6")),
                _fail("ERROR: PERMISSION_DENIED: Permission 'artifactregistry.repositories.getIamPolicy' "
                      "denied on resource"),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-6", "123456")
        self.assertTrue(result.passed, result.details)
        # Both halves, as check_artifact_registry does it. An operator reading
        # only the skipped half cannot tell whether the other one passed or was
        # skipped too, and goes and re-checks something this run already did.
        self.assertEqual(
            "the Workload Identity binding, the Prow runner and platform GSA project roles "
            "verified; the cross-project AR reader grants not checked",
            result.message,
        )

    def test_gsa_policy_failing_for_another_reason_still_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                # What IAM answers for an absent service account, observed
                # 2026-08-27; it is NOT_FOUND rather than the anti-enumeration
                # PERMISSION_DENIED, so a missing GSA is still reportable.
                _fail("ERROR: (gcloud.iam.service-accounts.get-iam-policy) NOT_FOUND: Unknown "
                      "service account."),
                _ok(self._litellm_wi_policy("kube-agents-evals-3")),
                _ok(self._project_policy("kube-agents-evals-3")),
                _ok(self._reader_policy(["serviceAccount:123456@cloudbuild.gserviceaccount.com"])),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("Missing GSA" in d for d in result.details), result.details)

    def test_prow_runner_missing_role_fails(self):
        # kube-agents-evals-6 as it stood on 2026-08-26: fully provisioned,
        # verified green, and its first lease died at get-credentials.
        without_container_admin = checker.PROW_RUNNER_ROLES - {"roles/container.admin"}
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-6")),
                _ok(self._litellm_wi_policy("kube-agents-evals-6")),
                _ok(self._project_policy("kube-agents-evals-6", prow_roles=without_container_admin)),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-6", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("roles/container.admin" in d for d in result.details), result.details)

    def test_prow_runner_conditional_binding_does_not_count(self):
        # A condition the presubmit does not satisfy grants nothing, so counting
        # the binding would pass a project the runner still cannot use.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-6")),
                _ok(self._litellm_wi_policy("kube-agents-evals-6")),
                _ok(
                    self._project_policy(
                        "kube-agents-evals-6",
                        prow_roles=checker.PROW_RUNNER_ROLES - {"roles/container.admin"},
                        conditional_roles={"roles/container.admin"},
                    )
                ),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-6", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("roles/container.admin" in d for d in result.details), result.details)

    def test_prow_runner_extra_role_passes(self):
        # The check reports absences only; a project holding more than the
        # measured set is not a misconfiguration this script has an opinion on.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-3")),
                _ok(self._litellm_wi_policy("kube-agents-evals-3")),
                _ok(self._project_policy(prow_roles=checker.PROW_RUNNER_ROLES | {"roles/artifactregistry.writer"})),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertTrue(result.passed, result.details)

    def test_platform_gsa_missing_role_fails(self):
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-3")),
                _ok(self._litellm_wi_policy("kube-agents-evals-3")),
                _ok(self._project_policy(
                    platform_roles=checker.PLATFORM_GSA_ROLES - {"roles/container.viewer"})),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("roles/container.viewer" in d for d in result.details), result.details)

    def test_platform_gsa_extra_admin_role_fails(self):
        # The drift the swap on 2026-08-26 cleared: projects provisioned before
        # the module narrowed kept container.admin, so the agent under test could
        # write to the shared fleet on half the pool.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-3")),
                _ok(self._litellm_wi_policy("kube-agents-evals-3")),
                _ok(self._project_policy(
                    platform_roles=checker.PLATFORM_GSA_ROLES | {"roles/container.admin"})),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("roles/container.admin" in d for d in result.details), result.details)

    def test_a_public_binding_fails(self):
        # Neither identity check would see this: both scan for one literal
        # member, and allUsers is not either of them.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-3")),
                _ok(self._litellm_wi_policy("kube-agents-evals-3")),
                _ok(self._project_policy(
                    extra_bindings=[{"role": "roles/storage.objectViewer", "members": ["allUsers"]}])),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("allUsers" in d for d in result.details), result.details)

    def test_a_conditional_public_binding_still_fails(self):
        # Unlike the two checks below it: a condition narrows when the grant
        # applies, not who holds it, so the exposure is still there.
        with mock.patch.object(checker, "run_cmd") as run:
            run.side_effect = [
                _ok(self._wi_policy("kube-agents-evals-3")),
                _ok(self._litellm_wi_policy("kube-agents-evals-3")),
                _ok(self._project_policy(extra_bindings=[{
                    "role": "roles/storage.objectViewer",
                    "members": ["allAuthenticatedUsers"],
                    "condition": {"title": "t", "expression": "request.time < timestamp('2030-01-01T00:00:00Z')"},
                }])),
                _ok(self._both_build_identities()),
            ]
            result = checker.check_iam_and_service_accounts("kube-agents-evals-3", "123456")
        self.assertFalse(result.passed)
        self.assertTrue(any("allAuthenticatedUsers" in d for d in result.details), result.details)


class PlatformGsaRolesMatchTerraformTest(unittest.TestCase):
    """PLATFORM_GSA_ROLES must equal the roles the install actually grants.

    Hardcoding the eight roles is what lets this check run without a Terraform
    toolchain, and it is also how the two drift apart. Without this test,
    narrowing the granted set would leave every correctly-provisioned project
    failing verification weeks later, with nothing pointing at Terraform as the
    cause.

    The set is written in three places, and only one of them is applied. Pool
    projects are installed through terraform/examples/full-install, whose
    `local.agent_project_roles` passes `local.read_only_roles` into the module
    (main.tf) -- so the module's own `project_roles` default is never read on
    that path and pinning it alone would leave this test green through exactly
    the drift it exists to catch. Both are asserted below: the composition
    because it is what runs, the module default because it is live for a caller
    invoking the module directly and nothing else joins the two.
    """

    def _roles(self, text, pattern, what):
        block = re.search(pattern, text, re.S | re.M)
        self.assertIsNotNone(block, f"could not find {what}")
        return set(re.findall(r'"(roles/[^"]+)"', block.group(1)))

    def test_matches_the_composition_the_install_applies(self):
        main = (checker._ROOT / "terraform" / "examples" / "full-install" / "main.tf").read_text()
        applied = self._roles(
            main, r"^[ \t]*read_only_roles[ \t]*=[ \t]*\[(.*?)\]", "local.read_only_roles in full-install/main.tf"
        )
        self.assertEqual(applied, checker.PLATFORM_GSA_ROLES)

    def test_the_module_default_matches_the_composition(self):
        tf = (checker._ROOT / "terraform" / "modules" / "kube-agents-iam" / "variables.tf").read_text()
        declared = self._roles(
            tf,
            r'variable\s+"project_roles".*?default\s*=\s*\[(.*?)\]',
            "the project_roles default in variables.tf",
        )
        self.assertEqual(declared, checker.PLATFORM_GSA_ROLES)


class ProwRunnerRolesMatchGrantersTest(unittest.TestCase):
    """PROW_RUNNER_ROLES must equal what the two granting sites grant.

    The twelve are written here, in the provisioning script's loop, and in the
    repair block on the prerequisites page, and none reads another. Drift is
    silent the worst way round: a role dropped from the script leaves a project
    the verifier still passes, registered, dying on its first lease as #966 did.
    """

    def _loop_roles(self, text, what):
        loops = [
            m.group(1)
            for m in re.finditer(r"for role in(.*?);\s*do(.*?)done", text, re.S)
            if re.search(r"prowjob-default-sa|PROW_RUNNER_SA", m.group(2))
        ]
        self.assertEqual(len(loops), 1, f"expected exactly one Prow runner grant loop in {what}")
        return set(re.findall(r"roles/[\w.]+", loops[0]))

    def test_matches_the_loop_the_provisioning_script_runs(self):
        script = (checker._ROOT / "scripts" / "provision_ci_pool_project.sh").read_text()
        granted = self._loop_roles(script, "provision_ci_pool_project.sh")
        self.assertEqual(granted, checker.PROW_RUNNER_ROLES)

    def test_matches_the_repair_block_on_the_prerequisites_page(self):
        page = (
            checker._ROOT / "docs" / "site" / "src" / "content" / "docs" / "deploy" / "ci-pool-projects.md"
        ).read_text()
        documented = self._loop_roles(page, "deploy/ci-pool-projects.md")
        self.assertEqual(documented, checker.PROW_RUNNER_ROLES)


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

    def test_a_bad_command_line_exits_usage_not_unverified(self):
        # argparse's own error() exits 2, which a caller would read as "nothing
        # failed, go confirm these by hand" -- so a typo would look like a run
        # that finished.
        with mock.patch("sys.argv", ["verify_ci_pool_project.py", "--no-such-flag"]), \
             mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as raised:
                checker.main()
        self.assertEqual(raised.exception.code, checker.EXIT_USAGE)
        self.assertNotEqual(checker.EXIT_USAGE, checker.EXIT_UNVERIFIED)

    def test_a_missing_required_argument_exits_usage(self):
        with mock.patch("sys.argv", ["verify_ci_pool_project.py"]), \
             mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as raised:
                checker.main()
        self.assertEqual(raised.exception.code, checker.EXIT_USAGE)


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

    def test_a_longer_project_id_does_not_satisfy_a_shorter_one(self):
        # Unanchored, `kube-agents-evals)` matches inside the -2 row, so
        # onboarding project 2 would read as already mapped.
        body = _ci_deploy_text("kube-agents-evals-2")
        self.assertFalse(checker._mapping_row_present(body, "kube-agents-evals"))
        self.assertTrue(checker._mapping_row_present(body, "kube-agents-evals-2"))

    def test_a_commented_out_row_is_not_a_row(self):
        # `case` ignores it, so the project deploys to whatever `*)` names.
        commented = (
            'gitops_repo_for_project() {\n  case "$1" in\n'
            '    # kube-agents-evals-6) echo "gke-agentic/kube-agents-evals-6-infra" ;;\n'
            "    *) return 1 ;;\n  esac\n}\n"
        )
        self.assertFalse(checker._mapping_row_present(commented, "kube-agents-evals-6"))

    def test_a_row_pointing_at_an_archived_repo_is_not_the_row(self):
        # `-infra` is a prefix of `-infra-old`.
        stale = (
            'gitops_repo_for_project() {\n  case "$1" in\n'
            '    kube-agents-evals-6) echo "gke-agentic/kube-agents-evals-6-infra-old" ;;\n'
            "    *) return 1 ;;\n  esac\n}\n"
        )
        self.assertFalse(checker._mapping_row_present(stale, "kube-agents-evals-6"))

    def test_an_unquoted_row_is_still_a_row(self):
        # Nothing forces the quotes, and an unquoted echo behaves identically.
        unquoted = (
            'gitops_repo_for_project() {\n  case "$1" in\n'
            "    kube-agents-evals-6) echo gke-agentic/kube-agents-evals-6-infra ;;\n"
            "    *) return 1 ;;\n  esac\n}\n"
        )
        self.assertTrue(checker._mapping_row_present(unquoted, "kube-agents-evals-6"))

    def test_an_unquoted_row_missing_its_space_is_not_a_row(self):
        # `echogke-agentic/...` is a command no shell resolves, so the arm is
        # dead and the project would fall through to `*)` at lease time.
        jammed = (
            'gitops_repo_for_project() {\n  case "$1" in\n'
            "    kube-agents-evals-6) echogke-agentic/kube-agents-evals-6-infra ;;\n"
            "    *) return 1 ;;\n  esac\n}\n"
        )
        self.assertFalse(checker._mapping_row_present(jammed, "kube-agents-evals-6"))

    def test_a_quoted_row_missing_its_space_is_not_a_row(self):
        # Quoting does not rescue it: word splitting runs before quote removal,
        # so `echo"gke-agentic/..."` is the same single dead token.
        jammed = (
            'gitops_repo_for_project() {\n  case "$1" in\n'
            '    kube-agents-evals-6) echo"gke-agentic/kube-agents-evals-6-infra" ;;\n'
            "    *) return 1 ;;\n  esac\n}\n"
        )
        self.assertFalse(checker._mapping_row_present(jammed, "kube-agents-evals-6"))

    def test_a_lookalike_owner_is_not_the_upstream_remote(self):
        # `not-gke-labs` ends with the real slug, and is a name anyone can take.
        impostor = "origin\tgit@github.com:not-gke-labs/kube-agents.git (fetch)\n"
        with _git(remotes=impostor):
            self.assertIsNone(checker._upstream_remote())

    def test_an_ssh_url_carrying_a_port_is_the_upstream_remote(self):
        # `git clone` accepts it, and the port must not be read as path.
        ported = "origin\tssh://git@github.com:22/gke-labs/kube-agents.git (fetch)\n"
        with _git(remotes=ported):
            self.assertEqual(checker._upstream_remote(), "origin")

    def test_the_owner_is_matched_case_insensitively(self):
        # GitHub resolves `GKE-Labs`; rejecting it loses the upstream comparison
        # silently, leaving the operator a warning instead of a verdict.
        shouted = "origin\thttps://github.com/GKE-Labs/kube-agents.git (fetch)\n"
        with _git(remotes=shouted):
            self.assertEqual(checker._upstream_remote(), "origin")

    def test_the_right_path_on_another_host_is_not_the_upstream_remote(self):
        mirror = "mirror\tgit@example.com:gke-labs/kube-agents.git (fetch)\n"
        with _git(remotes=mirror):
            self.assertIsNone(checker._upstream_remote())

    def test_the_upstream_remote_may_be_named_anything(self):
        archive = "archive\tgit@github.com:gke-labs/kube-agents.git (fetch)\n"
        with _git(remotes=archive):
            self.assertEqual(checker._upstream_remote(), "archive")


class RunChecksTest(unittest.TestCase):
    def test_missing_project_number_skips_dependent_checks_without_raising(self):
        with mock.patch.object(checker, "check_codebase_mapping", return_value=checker.CheckResult("m", True)), \
             mock.patch.object(checker, "check_project_and_apis", return_value=(None, checker.CheckResult("p", False))), \
             mock.patch.object(checker, "check_gke_and_state", return_value=checker.CheckResult("g", True)), \
             mock.patch.object(checker, "check_seeded_fleet_fixtures", return_value=checker.CheckResult("f", True)), \
             mock.patch.object(checker, "check_github_repo_and_app", return_value=checker.CheckResult("h", True)), \
             mock.patch.object(checker, "check_ledger_read_credential", return_value=checker.CheckResult("l", True)), \
             mock.patch.object(checker, "check_token_minter", return_value=checker.CheckResult("k", True)):
            results = checker.run_checks("kube-agents-evals-3")
        skipped = [c for c in results if c.message.startswith("Skipped")]
        self.assertEqual(len(skipped), 2)
        self.assertTrue(all(not c.passed for c in skipped))

    def test_denied_project_read_does_not_fail_the_checks_that_needed_it(self):
        # The project number is missing because the read was refused, not
        # because the project is wrong. Failing the two dependent checks would
        # put the conflation straight back one level up.
        unverified = checker.CheckResult("p", True, "Not checked", warnings=["could not describe"])
        with mock.patch.object(checker, "check_codebase_mapping", return_value=checker.CheckResult("m", True)), \
             mock.patch.object(checker, "check_project_and_apis", return_value=(None, unverified)), \
             mock.patch.object(checker, "check_gke_and_state", return_value=checker.CheckResult("g", True)), \
             mock.patch.object(checker, "check_seeded_fleet_fixtures", return_value=checker.CheckResult("f", True)), \
             mock.patch.object(checker, "check_github_repo_and_app", return_value=checker.CheckResult("h", True)), \
             mock.patch.object(checker, "check_ledger_read_credential", return_value=checker.CheckResult("l", True)), \
             mock.patch.object(checker, "check_token_minter", return_value=checker.CheckResult("k", True)):
            results = checker.run_checks("kube-agents-evals-6")
        dependent = [c for c in results if c.name in
                     ("Service Accounts & IAM Grants", "Artifact Registry Repository")]
        self.assertEqual(2, len(dependent))
        self.assertTrue(all(c.passed for c in dependent), [c.message for c in dependent])
        self.assertTrue(all(c.warnings for c in dependent))
        with mock.patch("builtins.print"):
            self.assertEqual(checker.EXIT_UNVERIFIED, checker.report("kube-agents-evals-6", results))


if __name__ == "__main__":
    unittest.main()
