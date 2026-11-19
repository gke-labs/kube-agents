"""Tests for post_merge_scope: the declared-scope and re-check adapters, with their edges faked."""

import sqlite3
import subprocess
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_scope_args
import post_merge_check
import post_merge_scope
from test_post_merge_check import MERGE, REPO, FakeCluster, argo_app, schema

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
RECORD = {"project": "p", "location": "l", "cluster": "c", "kind": "no-pdb", "namespace": "ns", "object": "Deployment/x"}


def declared(projects, unread=None, excluded=None):
    scope = fleet_scope_args.DeclaredScope()
    scope.set(projects, unread, excluded)
    return scope


def fake_stall_watch(fetched=None):
    stall = types.ModuleType("stall_watch")
    stall.fetch_credentials = lambda *key: (fetched.append(key) if fetched is not None else None) or "/k/" + "_".join(key)
    stall.run_sandbox = lambda argv, timeout, kubeconfig: subprocess.CompletedProcess(argv, 0, '{"items": []}', "")
    return stall


class InstallScopeTest(unittest.TestCase):
    def test_hands_the_collector_flags_to_declared_scope(self):
        targets = types.SimpleNamespace(collector_argv=lambda: (
            fleet_scope_args.SCOPE_PROJECTS_FLAG, "p1,p2",
            fleet_scope_args.SCOPE_EXCLUDE_CLUSTERS_FLAG, "p1/us-central1/skip"))
        fake = types.ModuleType("fleet_scope_targets")
        fake.declared_scope_targets = lambda: targets
        with mock.patch.dict(sys.modules, {"fleet_scope_targets": fake}):
            scope = post_merge_scope.install_scope()
        self.assertTrue(scope.declared)
        self.assertEqual(scope.projects, ["p1", "p2"])
        self.assertIn(("p1", "us-central1", "skip"), scope.excluded_clusters)
        fake.declared_scope_targets = lambda: None
        with mock.patch.dict(sys.modules, {"fleet_scope_targets": fake}), \
                mock.patch.object(fleet_scope_args, "sandbox_declares_scope", lambda: False):
            self.assertFalse(post_merge_scope.install_scope().declared)


class ScopeRefusalTest(unittest.TestCase):
    def test_uses_recheck_from_this_tree(self):
        refused = post_merge_scope.scope_refusal_for(declared("p1", None, "p1/l/skip"))
        self.assertIsNone(refused({"project": "p1", "location": "l", "cluster": "keep"}))
        self.assertIn("excluded", refused({"project": "p1", "location": "l", "cluster": "skip"}))
        self.assertTrue(refused({"project": "p9", "location": "l", "cluster": "x"}))

    def test_a_qualified_cluster_is_judged_by_its_parts(self):
        refused = post_merge_scope.scope_refusal_for(declared("p1", None, "p1/l/skip"))
        self.assertIn("excluded", refused({"project": "", "location": "", "cluster": "p1/l/skip"}))
        self.assertIsNone(refused({"project": "", "location": "", "cluster": "p1/l/keep"}))

    def test_a_project_outside_the_declared_scope_is_out_of_scope(self):
        reason = post_merge_scope.scope_refusal_for(declared("other"))({"project": "p", "location": "l", "cluster": "c"})
        self.assertFalse(reason.startswith(post_merge_scope.UNKNOWN_PREFIX), reason)

    def test_an_unreadable_project_is_unknown_never_out_of_scope(self):
        reason = post_merge_scope.scope_refusal_for(declared("", "p=unreachable"))(
            {"project": "p", "location": "l", "cluster": "c"})
        self.assertTrue(reason.startswith(post_merge_scope.UNKNOWN_PREFIX), reason)

    def test_a_scope_with_the_unresolved_row_never_calls_a_project_out_of_scope(self):
        scope = declared("ops-mgmt", f"{fleet_scope_args.UNRESOLVED_SCOPE_ROW}=unresolved")
        reason = post_merge_scope.scope_refusal_for(scope)({"project": "payments", "location": "l", "cluster": "c"})
        self.assertTrue(reason.startswith(post_merge_scope.UNKNOWN_PREFIX), reason)

    def test_an_excluded_cluster_in_an_unread_project_is_out_of_scope(self):
        reason = post_merge_scope.scope_refusal_for(declared("", "p=permission-denied", "p/l/c"))(
            {"project": "p", "location": "l", "cluster": "c"})
        self.assertFalse(reason.startswith(post_merge_scope.UNKNOWN_PREFIX), reason)
        self.assertIn("spec.scope.exclude.clusters", reason)

    def test_the_prefix_matches_the_one_post_merge_check_reads(self):
        self.assertEqual(post_merge_scope.UNKNOWN_PREFIX, post_merge_check.SCOPE_UNKNOWN_PREFIX)


class SandboxRecheckTest(unittest.TestCase):
    def test_targets_the_record_and_retries_an_unread_answer(self):
        calls = {}
        fake_recheck = types.ModuleType("recheck")
        fake_recheck.Target = lambda **kw: kw

        def recheck(target, run, kubeconfig, scope):
            calls.update(target=target, kubeconfig=kubeconfig)
            calls["run"] = run(["kubectl", "get", "x"], env={"KUBECONFIG": kubeconfig}, timeout=5)
            return calls.get("answer", {"outcome": "clean"})

        fake_recheck.recheck = recheck
        with mock.patch.dict(sys.modules, {"stall_watch": fake_stall_watch()}), \
                mock.patch.object(post_merge_scope, "_fleet_audit_module", lambda name: fake_recheck):
            run_one = post_merge_scope.sandbox_recheck(scope=None)
            self.assertEqual(run_one(RECORD), {"outcome": "clean"})
            self.assertEqual(calls["target"]["cluster"], "p/l/c")
            self.assertEqual((calls["run"].rc, calls["kubeconfig"]), (0, "/k/p_c_l"))
            calls["answer"] = {"outcome": "unchecked", "reason": "the read failed its gate (rc=1: x)"}
            with self.assertRaises(post_merge_scope.RecheckUnread):
                run_one(RECORD)
            calls["answer"] = {"outcome": "unchecked", "reason": "no on-demand re-check is registered"}
            self.assertEqual(run_one(RECORD)["outcome"], "unchecked")

    def test_failed_credentials_are_an_unread_answer(self):
        stall = fake_stall_watch()
        stall.fetch_credentials = mock.Mock(side_effect=RuntimeError("denied"))
        with mock.patch.dict(sys.modules, {"stall_watch": stall}), \
                mock.patch.object(post_merge_scope, "_fleet_audit_module", lambda name: types.ModuleType("recheck")), \
                self.assertRaises(post_merge_scope.RecheckUnread):
            post_merge_scope.sandbox_recheck(None)(RECORD)

    def test_health_and_recheck_fetch_each_cluster_s_credentials_once(self):
        fetched = []
        fake_recheck = types.ModuleType("recheck")
        fake_recheck.Target = lambda **kw: kw
        fake_recheck.recheck = lambda target, run, kubeconfig, scope: {"outcome": "clean"}
        shared = {}
        with mock.patch.dict(sys.modules, {"stall_watch": fake_stall_watch(fetched)}), \
                mock.patch.object(post_merge_scope, "_fleet_audit_module", lambda name: fake_recheck):
            post_merge_check.SandboxKubectl(shared)(RECORD, ["get", "pods", "-A"])
            post_merge_scope.sandbox_recheck(None, shared)(RECORD)
        self.assertEqual(fetched, [("p", "c", "l")])

    def test_recheck_is_imported_from_the_image_first_never_the_profile(self):
        dirs = post_merge_scope._fleet_audit_dirs()
        self.assertEqual(dirs[0], post_merge_scope.IMAGE_FLEET_AUDIT_DIR)
        self.assertFalse(any("profiles" in d for d in dirs))


class HealthScopeGateTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(schema())
        for number, cluster in ((1, "prod"), *((n, "elsewhere") for n in range(2, 25))):
            self.conn.execute(
                "INSERT INTO post_merge_records (url, repo, number, kind, project, location, cluster, namespace, "
                "object, opened_at, merged_at, merge_commit) VALUES (?, ?, ?, 'no-pdb', 'p', 'l', ?, 'shop', "
                "'Deployment/checkout', ?, ?, ?)",
                (f"{REPO}/pull/{number}", REPO, number, cluster, NOW.isoformat(), NOW.isoformat(), MERGE),
            )
        self.cluster = FakeCluster()
        self.cluster.apps = [argo_app()]

    def health(self, minutes, refusal):
        return post_merge_check.watch_health(self.conn, self.cluster, NOW + timedelta(minutes=minutes), limit=20,
                                             scope_refusal=refusal)

    def test_a_refused_cluster_is_never_read_and_never_holds_the_slots(self):
        refusals = {"elsewhere": "unknown: p is unreadable"}
        seen = self.health(1, lambda record: refusals.get(record["cluster"]))
        # 23 refused records, more than the cap of 20, and the one in scope is still read on the first tick.
        self.assertEqual((len(seen["scope_unknown"]), seen["applied"]), (23, [f"{REPO}/pull/1"]))
        state = self.conn.execute("SELECT sync_state FROM post_merge_records WHERE number = 2").fetchone()[0]
        self.assertEqual(state, "scope unknown: p is unreadable")

    def test_the_cap_counts_only_records_that_are_read(self):
        self.conn.execute("UPDATE post_merge_records SET cluster = 'prod'")
        seen = self.health(1, lambda record: None)
        self.assertEqual(len(seen["applied"]), 20)

    def test_a_record_naming_no_cluster_says_so_rather_than_out_of_scope(self):
        self.conn.execute("UPDATE post_merge_records SET project = '' WHERE number = 1")
        with self.assertLogs("post_merge_check", level="WARNING"):
            seen = self.health(1, lambda record: "outside the declared scope")
        self.assertIn(f"{REPO}/pull/1", seen["unreadable"])
        state = self.conn.execute("SELECT sync_state FROM post_merge_records WHERE number = 1").fetchone()[0]
        self.assertIn("names no cluster", state)

    def test_out_of_scope_and_unjudgeable_records_are_stamped_not_read(self):
        def broken(record):
            raise KeyError("project")

        for refusal, bucket in ((lambda r: "p/l/prod is excluded by spec.scope.exclude.clusters", "out_of_scope"),
                                (broken, "scope_unknown")):
            with self.subTest(bucket=bucket):
                self.cluster.calls.clear()
                if refusal is broken:
                    with self.assertLogs("post_merge_check", level="WARNING"):
                        seen = self.health(1, refusal)
                else:
                    seen = self.health(1, refusal)
                self.assertEqual((len(seen[bucket]), self.cluster.calls), (24, []))
                states = {row[0].split(":")[0] for row in self.conn.execute(
                    "SELECT sync_state FROM post_merge_records WHERE health_checked_at IS NOT NULL AND applied_at IS NULL")}
                self.assertEqual(states, {"out of scope" if bucket == "out_of_scope" else "scope unknown"})
                self.conn.execute("UPDATE post_merge_records SET health_checked_at = NULL, sync_state = NULL")


if __name__ == "__main__":
    unittest.main()
