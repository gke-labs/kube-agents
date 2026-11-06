"""Unit tests for reconciler_sync: has the reconciler applied a merge commit yet?"""

import copy
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import reconciler_sync as rs

MERGE = "3d1a1b0bf1b13e4e40a0301114750aadf81bae38"
BEFORE = "4a33bccb104c971749e0025bc0b454a09c119b83"
LATER = "9f0c2d6e1a7b4c3d2e1f0a9b8c7d6e5f4a3b2c1d"

# Trimmed from the live `argocd/mcp-demo` Application on jayantid-gkedemos-3,
# 2026-11-06, after the PDB fix merged as MERGE and Argo synced it.
ARGO_SYNCED_AT_MERGE = {
    "apiVersion": "argoproj.io/v1alpha1",
    "kind": "Application",
    "metadata": {"name": "mcp-demo", "namespace": "argocd"},
    "spec": {
        "source": {
            "path": "clusters/platform-agent-host/mcp-demo",
            "repoURL": "https://github.com/gke-agentic/jayantid-gkedemos-2-infra.git",
            "targetRevision": "main",
        },
        "syncPolicy": {"automated": {"prune": True, "selfHeal": True}},
    },
    "status": {
        "sync": {"revision": MERGE, "status": "Synced"},
        "health": {"status": "Healthy"},
        "operationState": {
            "phase": "Succeeded",
            "message": "successfully synced (all tasks run)",
            "syncResult": {"revision": MERGE},
        },
    },
}


def argo(sync_status="Synced", revision=MERGE, **status):
    obj = copy.deepcopy(ARGO_SYNCED_AT_MERGE)
    obj["status"]["sync"] = {"status": sync_status, "revision": revision}
    obj["status"].update(status)
    return obj


def flux(last_applied="", conditions=(), suspend=False):
    status = {"conditions": list(conditions)}
    if last_applied:
        status["lastAppliedRevision"] = last_applied
    return {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": "apps", "namespace": "flux-system"},
        "spec": {"suspend": suspend},
        "status": status,
    }


def configsync(last_synced="", conditions=(), kind="RootSync", **extra):
    status = {"conditions": list(conditions), **extra}
    if last_synced:
        status["lastSyncedCommit"] = last_synced
    return {
        "apiVersion": "configsync.gke.io/v1beta1",
        "kind": kind,
        "metadata": {"name": "root-sync", "namespace": "config-management-system"},
        "status": status,
    }


def ancestry(answer):
    """An is_ancestor that answers `answer` and records its calls."""
    return mock.Mock(return_value=answer)


class ArgoTest(unittest.TestCase):
    def test_synced_at_the_merge_commit_is_applied_and_carries_health(self):
        reading = rs.read_sync(ARGO_SYNCED_AT_MERGE, MERGE)
        self.assertEqual((reading.state, reading.reconciler, reading.revision, reading.health),
                         (rs.APPLIED, rs.ARGOCD, MERGE, "Healthy"))

    def test_synced_at_the_commit_before_the_merge_is_never_applied(self):
        # The 2026-11-05 failure: Argo still at the old commit must not read as the fix.
        self.assertEqual(rs.read_sync(argo(revision=BEFORE), MERGE, ancestry(False)).state, rs.PENDING)
        self.assertEqual(rs.read_sync(argo(revision=BEFORE), MERGE).state, rs.UNCONFIRMED)

    def test_a_later_commit_that_contains_the_merge_is_applied(self):
        check = ancestry(True)
        reading = rs.read_sync(argo(revision=LATER), MERGE, check)
        self.assertEqual((reading.state, reading.revision), (rs.APPLIED, LATER))
        check.assert_called_once_with(MERGE, LATER)

    def test_ancestry_is_not_asked_when_the_revision_is_the_merge_commit(self):
        check = ancestry(False)
        rs.read_sync(ARGO_SYNCED_AT_MERGE, MERGE, check)
        check.assert_not_called()

    def test_an_unanswered_ancestry_is_unconfirmed(self):
        self.assertEqual(rs.read_sync(argo(revision=LATER), MERGE, ancestry(None)).state, rs.UNCONFIRMED)

    def test_out_of_sync_at_the_merge_commit_is_pending(self):
        reading = rs.read_sync(argo(sync_status="OutOfSync"), MERGE)
        self.assertEqual(reading.state, rs.PENDING)
        self.assertIn("waiting for Argo CD", reading.detail)

    def test_manual_sync_says_someone_has_to_sync_it(self):
        obj = argo(sync_status="OutOfSync")
        del obj["spec"]["syncPolicy"]
        self.assertIn("automated sync is off", rs.read_sync(obj, MERGE).detail)
        obj["spec"]["syncPolicy"] = {"automated": {"enabled": False}}
        self.assertIn("automated sync is off", rs.read_sync(obj, MERGE).detail)

    def test_a_running_sync_is_pending(self):
        obj = argo(sync_status="OutOfSync", operationState={"phase": "Running", "operation": {"sync": {"revision": MERGE}}})
        reading = rs.read_sync(obj, MERGE)
        self.assertEqual(reading.state, rs.PENDING)
        self.assertIn("running", reading.detail)

    def test_a_failed_sync_of_the_merge_commit_is_an_error(self):
        obj = argo(sync_status="OutOfSync", operationState={
            "phase": "Failed", "message": "one or more objects failed to apply", "syncResult": {"revision": MERGE}})
        reading = rs.read_sync(obj, MERGE)
        self.assertEqual(reading.state, rs.ERROR)
        self.assertIn("failed to apply", reading.detail)

    def test_a_failed_sync_of_an_older_commit_is_not_this_merges_error(self):
        obj = argo(sync_status="OutOfSync", operationState={"phase": "Failed", "syncResult": {"revision": BEFORE}})
        self.assertEqual(rs.read_sync(obj, MERGE).state, rs.PENDING)

    def test_a_failed_sync_that_names_no_revision_is_an_error(self):
        for operation in ({}, {"operation": {"sync": {"revision": "HEAD"}}}):
            with self.subTest(operation=operation):
                obj = argo(sync_status="OutOfSync", revision=BEFORE,
                           operationState={"phase": "Failed", "message": "rpc error", **operation})
                reading = rs.read_sync(obj, MERGE, ancestry(False))
                self.assertEqual(reading.state, rs.ERROR)
                self.assertIn("names no revision", reading.detail)

    def test_a_comparison_error_condition_is_an_error_and_a_warning_is_not(self):
        err = argo(sync_status="Unknown", conditions=[{"type": "ComparisonError", "message": "repo not found"}])
        self.assertEqual(rs.read_sync(err, MERGE).state, rs.ERROR)
        warn = argo(sync_status="OutOfSync", conditions=[{"type": "SharedResourceWarning", "message": "x"}])
        self.assertEqual(rs.read_sync(warn, MERGE).state, rs.PENDING)

    def test_synced_at_the_merge_wins_over_a_stale_error_condition(self):
        obj = argo(conditions=[{"type": "SyncError", "message": "old"}])
        self.assertEqual(rs.read_sync(obj, MERGE).state, rs.APPLIED)

    def test_an_error_condition_during_a_running_sync_is_pending(self):
        obj = argo(sync_status="OutOfSync", revision=BEFORE,
                   conditions=[{"type": "SyncError", "message": f"Failed last sync attempt to {BEFORE}"}],
                   operationState={"phase": "Running", "operation": {"sync": {"revision": MERGE}}})
        self.assertEqual(rs.read_sync(obj, MERGE).state, rs.PENDING)

    def test_a_failed_multi_source_sync_of_the_merge_is_an_error(self):
        obj = argo(sync_status="OutOfSync",
                   operationState={"phase": "Failed", "message": "boom", "syncResult": {"revisions": ["1.4.2", MERGE]}})
        obj["status"]["sync"] = {"status": "OutOfSync", "revisions": ["1.4.2", BEFORE]}
        self.assertEqual(rs.read_sync(obj, MERGE).state, rs.ERROR)

    def test_multi_source_pending_names_the_git_revision(self):
        obj = argo()
        obj["status"]["sync"] = {"status": "OutOfSync", "revisions": ["1.4.2", BEFORE]}
        self.assertIn(BEFORE[:8], rs.read_sync(obj, MERGE).detail)

    def test_a_helm_chart_revision_is_unknown(self):
        self.assertEqual(rs.read_sync(argo(revision="1.4.2"), MERGE).state, rs.UNKNOWN)

    def test_multi_source_applies_when_any_source_is_at_the_merge(self):
        obj = argo()
        obj["status"]["sync"] = {"status": "Synced", "revisions": ["1.4.2", MERGE]}
        self.assertEqual(rs.read_sync(obj, MERGE).state, rs.APPLIED)

    def test_no_sync_status_is_unknown(self):
        obj = argo()
        obj["status"] = {}
        self.assertEqual(rs.read_sync(obj, MERGE).state, rs.UNKNOWN)


class FluxTest(unittest.TestCase):
    def test_both_revision_formats_at_the_merge_are_applied(self):
        for rev in (f"main@sha1:{MERGE}", f"main/{MERGE}", f"sha1:{MERGE}", MERGE.upper()):
            with self.subTest(rev=rev):
                self.assertEqual(rs.read_sync(flux(rev), MERGE).state, rs.APPLIED)

    def test_an_older_revision_needs_ancestry(self):
        self.assertEqual(rs.read_sync(flux(f"main@sha1:{BEFORE}"), MERGE).state, rs.UNCONFIRMED)
        self.assertEqual(rs.read_sync(flux(f"main@sha1:{BEFORE}"), MERGE, ancestry(False)).state, rs.PENDING)
        self.assertEqual(rs.read_sync(flux(f"main@sha1:{LATER}"), MERGE, ancestry(True)).state, rs.APPLIED)

    def test_a_short_or_prefixed_sha_is_not_a_commit(self):
        self.assertEqual(rs.read_sync(flux(f"main@sha1:{MERGE[:12]}"), MERGE).state, rs.UNKNOWN)
        self.assertEqual(rs.read_sync(flux(f"main@sha1:x{MERGE}"), MERGE).state, rs.UNKNOWN)

    def test_a_sha256_commit_from_a_named_repository_source_is_applied(self):
        sha256 = "c" * 64
        obj = flux(f"main@sha256:{sha256}")
        obj["spec"]["sourceRef"] = {"kind": "GitRepository", "name": "apps"}
        self.assertEqual(rs.read_sync(obj, sha256).state, rs.APPLIED)

    def test_an_oci_source_or_digest_is_unknown(self):
        self.assertEqual(rs.read_sync(flux("latest@sha256:" + "ab" * 32), MERGE).state, rs.UNKNOWN)
        obj = flux(f"main@sha1:{MERGE}")
        obj["spec"]["sourceRef"] = {"kind": "OCIRepository", "name": "apps"}
        self.assertEqual(rs.read_sync(obj, MERGE).state, rs.UNKNOWN)

    def test_a_failure_left_over_from_the_commit_before_the_merge_is_not_the_merges(self):
        cond = {"type": "Ready", "status": "False", "reason": "BuildFailed", "message": "old"}
        obj = flux(f"main@sha1:{BEFORE}", [cond])
        obj["status"]["lastAttemptedRevision"] = f"main@sha1:{BEFORE}"
        self.assertEqual(rs.read_sync(obj, MERGE, ancestry(False)).state, rs.PENDING)
        self.assertEqual(rs.read_sync(obj, MERGE).state, rs.UNCONFIRMED)
        obj["status"]["lastAttemptedRevision"] = f"main@sha1:{MERGE}"
        self.assertEqual(rs.read_sync(obj, MERGE, ancestry(False)).state, rs.ERROR)

    def test_a_failed_health_check_after_applying_the_merge_is_applied(self):
        cond = {"type": "Ready", "status": "False", "reason": "HealthCheckFailed", "message": "Deployment/x not ready"}
        obj = flux(f"main@sha1:{BEFORE}", [cond])
        obj["status"]["lastAttemptedRevision"] = f"main@sha1:{MERGE}"
        reading = rs.read_sync(obj, MERGE, ancestry(False))
        self.assertEqual((reading.state, reading.revision), (rs.APPLIED, MERGE))
        self.assertIn("health check failing", reading.detail)

    def test_suspended_wins_over_an_old_failure(self):
        cond = {"type": "Ready", "status": "False", "reason": "BuildFailed"}
        reading = rs.read_sync(flux(f"main@sha1:{BEFORE}", [cond], suspend=True), MERGE)
        self.assertEqual(reading.state, rs.PENDING)
        self.assertIn("suspended", reading.detail)

    def test_ready_false_with_a_failure_reason_is_an_error(self):
        cond = {"type": "Ready", "status": "False", "reason": "BuildFailed", "message": "kustomize build failed"}
        reading = rs.read_sync(flux(f"main@sha1:{BEFORE}", [cond]), MERGE)
        self.assertEqual(reading.state, rs.ERROR)
        self.assertIn("BuildFailed", reading.detail)

    def test_ready_false_while_progressing_is_not_an_error(self):
        cond = {"type": "Ready", "status": "False", "reason": "Progressing"}
        self.assertEqual(rs.read_sync(flux(f"main@sha1:{BEFORE}", [cond]), MERGE, ancestry(False)).state, rs.PENDING)

    def test_a_reconcile_of_the_merge_in_flight_is_pending_with_or_without_ancestry(self):
        cond = {"type": "Ready", "status": "Unknown", "reason": "Progressing"}
        obj = flux(f"main@sha1:{BEFORE}", [cond])
        obj["status"]["lastAttemptedRevision"] = f"main@sha1:{MERGE}"
        for check in (None, ancestry(False)):
            with self.subTest(ancestry=check):
                reading = rs.read_sync(obj, MERGE, check)
                self.assertEqual(reading.state, rs.PENDING)
                self.assertIn("reconcile", reading.detail)
                self.assertIn("running", reading.detail)

    def test_stalled_is_an_error(self):
        cond = {"type": "Stalled", "status": "True", "reason": "InvalidPath", "message": "path not found"}
        self.assertEqual(rs.read_sync(flux("", [cond]), MERGE).state, rs.ERROR)

    def test_suspended_is_pending_and_says_so(self):
        reading = rs.read_sync(flux(f"main@sha1:{BEFORE}", suspend=True), MERGE)
        self.assertEqual(reading.state, rs.PENDING)
        self.assertIn("suspended", reading.detail)

    def test_no_status_is_unknown(self):
        obj = flux()
        obj["status"] = {}
        self.assertEqual(rs.read_sync(obj, MERGE).state, rs.UNKNOWN)


class ConfigSyncTest(unittest.TestCase):
    def test_last_synced_commit_at_the_merge_is_applied_for_both_kinds(self):
        for kind in ("RootSync", "RepoSync"):
            with self.subTest(kind=kind):
                reading = rs.read_sync(configsync(MERGE, kind=kind), MERGE)
                self.assertEqual((reading.state, reading.reconciler), (rs.APPLIED, rs.CONFIGSYNC))

    def test_an_older_commit_is_not_applied(self):
        self.assertEqual(rs.read_sync(configsync(BEFORE), MERGE, ancestry(False)).state, rs.PENDING)
        self.assertEqual(rs.read_sync(configsync(BEFORE), MERGE).state, rs.UNCONFIRMED)

    def test_stalled_is_an_error(self):
        cond = {"type": "Stalled", "status": "True", "reason": "Deployment", "message": "reconciler not ready"}
        self.assertEqual(rs.read_sync(configsync(BEFORE, [cond]), MERGE).state, rs.ERROR)

    def test_syncing_with_a_real_error_is_an_error(self):
        cond = {"type": "Syncing", "status": "False", "commit": MERGE, "errorSourceRefs": ["status.sync.errors"]}
        obj = configsync(BEFORE, [cond], sync={"errors": [{"code": "1068", "errorMessage": "KNV1068: duplicate object"}]})
        reading = rs.read_sync(obj, MERGE)
        self.assertEqual(reading.state, rs.ERROR)
        self.assertIn("KNV1068", reading.detail)

    def test_syncing_with_only_a_transient_error_is_not_an_error(self):
        cond = {"type": "Syncing", "status": "True", "errorSourceRefs": ["status.rendering.errors"]}
        obj = configsync(BEFORE, [cond], rendering={"errors": [{"code": "2016", "errorMessage": "rendering in progress"}]})
        self.assertEqual(rs.read_sync(obj, MERGE, ancestry(False)).state, rs.PENDING)

    def test_an_error_on_an_older_commit_is_not_the_merges(self):
        cond = {"type": "Syncing", "status": "False", "commit": BEFORE, "errorSourceRefs": ["status.sync.errors"]}
        obj = configsync(BEFORE, [cond], sync={"errors": [{"code": "1068", "errorMessage": "old"}]})
        self.assertEqual(rs.read_sync(obj, MERGE, ancestry(False)).state, rs.PENDING)

    def test_an_error_ref_with_no_error_list_is_still_an_error(self):
        cond = {"type": "Syncing", "status": "False", "commit": MERGE, "message": "apply failed",
                "errorSourceRefs": ["status.sync.errors"]}
        obj = configsync(BEFORE, [cond], sync={"errorSummary": {"totalCount": 1}})
        self.assertEqual(rs.read_sync(obj, MERGE).state, rs.ERROR)

    def test_a_helm_or_oci_source_or_a_non_commit_is_unknown(self):
        obj = configsync(MERGE)
        obj["spec"] = {"sourceType": "helm"}
        self.assertEqual(rs.read_sync(obj, MERGE).state, rs.UNKNOWN)
        self.assertEqual(rs.read_sync(configsync("1.2.3"), MERGE).state, rs.UNKNOWN)

    def test_a_sync_of_the_merge_in_flight_is_pending_with_or_without_ancestry(self):
        cond = {"type": "Syncing", "status": "True", "commit": MERGE}
        for check in (None, ancestry(False)):
            with self.subTest(ancestry=check):
                reading = rs.read_sync(configsync(BEFORE, [cond]), MERGE, check)
                self.assertEqual(reading.state, rs.PENDING)
                self.assertIn("is running", reading.detail)

    def test_never_synced_is_pending(self):
        cond = {"type": "Syncing", "status": "True", "reason": "Source"}
        self.assertEqual(rs.read_sync(configsync("", [cond]), MERGE).state, rs.PENDING)


class InputTest(unittest.TestCase):
    def test_a_merge_commit_that_is_not_a_full_id_is_refused(self):
        for bad in ("", MERGE[:11], MERGE[:13], "main", MERGE + "0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                rs.read_sync(ARGO_SYNCED_AT_MERGE, bad)

    def test_bitbucket_clouds_abbreviated_merge_commit_matches_as_a_prefix(self):
        reading = rs.read_sync(ARGO_SYNCED_AT_MERGE, MERGE[:12].upper())
        self.assertEqual((reading.state, reading.revision), (rs.APPLIED, MERGE))
        self.assertIn("applied the merge commit", reading.detail)
        self.assertEqual(rs.read_sync(argo(revision=BEFORE), MERGE[:12], ancestry(False)).state, rs.PENDING)
        self.assertEqual(rs.read_sync(flux(f"main@sha1:{MERGE}"), MERGE[:12]).state, rs.APPLIED)

    def test_a_sha256_merge_commit_is_accepted(self):
        sha256 = "a" * 64
        self.assertEqual(rs.read_sync(argo(revision=sha256), sha256).state, rs.APPLIED)

    def test_helmrelease_and_other_kinds_are_refused(self):
        for api, kind in (("helm.toolkit.fluxcd.io/v2", "HelmRelease"), ("apps/v1", "Deployment"),
                          ("example.com/v1", "Application")):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                rs.read_sync({"apiVersion": api, "kind": kind, "status": {}}, MERGE)

    def test_a_non_object_is_refused(self):
        with self.assertRaises(TypeError):
            rs.read_sync([], MERGE)

    def test_a_long_message_is_clipped(self):
        obj = argo(sync_status="Unknown", conditions=[{"type": "ComparisonError", "message": "x" * 1000}])
        self.assertLessEqual(len(rs.read_sync(obj, MERGE).detail), rs.MAX_DETAIL_CHARS + len("ComparisonError: "))


class CliTest(unittest.TestCase):
    def run_main(self, stdin, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(out), redirect_stderr(err):
            code = rs.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_prints_the_reading_as_json(self):
        code, out, _ = self.run_main(json.dumps(ARGO_SYNCED_AT_MERGE), "--merge-commit", MERGE)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["state"], rs.APPLIED)

    def test_bad_input_exits_2(self):
        for stdin, merge in (("not json", MERGE), (json.dumps(ARGO_SYNCED_AT_MERGE), "abc"), ("[]", MERGE)):
            with self.subTest(stdin=stdin[:10], merge=merge):
                code, out, err = self.run_main(stdin, "--merge-commit", merge)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("reconciler_sync:", err)


if __name__ == "__main__":
    unittest.main()
