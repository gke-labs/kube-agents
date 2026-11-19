"""Tests for post_merge_check.notice_merges: one forge read per waiting record, with a fake forge and clock."""

import json
import re
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import post_merge_check

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
REPO = "https://github.com/acme/fleet"


def schema() -> str:
    """The table as session_kv_server.py creates it, read from its source so the two cannot drift."""
    source = (Path(__file__).resolve().parent / "session_kv_server.py").read_text(encoding="utf-8")
    return re.search(r"CREATE TABLE IF NOT EXISTS post_merge_records \((.*?)\)\s*\"\"\"", source, re.DOTALL).group(0)[:-3]


class FakeForge:
    """``view(repo, number)`` from a table of proposals; counts every read."""

    def __init__(self, proposals):
        self.proposals = proposals
        self.reads = []

    def __call__(self, repo, number):
        self.reads.append((repo, number))
        answer = self.proposals[number]
        if isinstance(answer, Exception):
            raise answer
        return answer


class NoticeMergesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(schema())

    def add(self, number, **columns):
        row = {"url": f"{REPO}/pull/{number}", "repo": REPO, "number": number, "kind": "no-pdb",
               "opened_at": (NOW - timedelta(hours=100 - number)).isoformat(), **columns}
        names = ", ".join(row)
        self.conn.execute(f"INSERT INTO post_merge_records ({names}) VALUES ({', '.join('?' * len(row))})",
                          tuple(row.values()))

    def row(self, number):
        self.conn.row_factory = sqlite3.Row
        found = dict(self.conn.execute("SELECT * FROM post_merge_records WHERE number = ?", (number,)).fetchone())
        self.conn.row_factory = None
        return found

    def test_a_merged_pull_request_records_its_merge_time_and_commit(self):
        self.add(1)
        forge = FakeForge({1: {"state": "merged", "mergeCommit": "abc123", "closed": "2026-11-12T08:30:00Z"}})
        seen = post_merge_check.notice_merges(self.conn, forge, NOW)
        self.assertEqual(seen["merged"], [f"{REPO}/pull/1"])
        row = self.row(1)
        self.assertEqual((row["merged_at"], row["merge_commit"], row["closed_at"], row["outcome"]),
                         ("2026-11-12T08:30:00Z", "abc123", None, None))
        self.assertEqual(forge.reads, [(REPO, 1)])

    def test_a_merge_with_no_commit_or_time_still_records_the_merge(self):
        self.add(1)
        post_merge_check.notice_merges(self.conn, FakeForge({1: {"state": "merged", "mergeCommit": None}}), NOW)
        row = self.row(1)
        self.assertEqual((row["merged_at"], row["merge_commit"]), (NOW.isoformat(), None))

    def test_closed_unmerged_ends_silently_after_the_grace(self):
        self.add(2)
        closed = {"state": "closed", "closed": "2026-11-12T07:00:00Z"}
        seen = post_merge_check.notice_merges(self.conn, FakeForge({2: closed}), NOW)
        self.assertEqual(seen["closed"], [f"{REPO}/pull/2"])
        row = self.row(2)
        self.assertEqual((row["closed_at"], row["merged_at"], row["outcome"]), ("2026-11-12T07:00:00Z", None, None))
        # Within the grace it is read again; past it, never.
        forge = FakeForge({2: closed})
        post_merge_check.notice_merges(self.conn, forge, NOW + timedelta(hours=12))
        self.assertEqual(forge.reads, [(REPO, 2)])
        forge.reads.clear()
        post_merge_check.notice_merges(self.conn, forge, NOW + timedelta(days=2))
        self.assertEqual(forge.reads, [])
        self.assertIsNone(self.row(2)["outcome"])

    def test_a_reopen_inside_the_grace_waits_again_and_a_later_merge_is_noticed(self):
        self.add(3)
        post_merge_check.notice_merges(self.conn, FakeForge({3: {"state": "closed", "closed": NOW.isoformat()}}), NOW)
        post_merge_check.notice_merges(self.conn, FakeForge({3: {"state": "open"}}), NOW + timedelta(hours=1))
        self.assertIsNone(self.row(3)["closed_at"])
        later = NOW + timedelta(days=3)
        post_merge_check.notice_merges(self.conn, FakeForge({3: {"state": "merged", "mergeCommit": "c3"}}), later)
        self.assertEqual(self.row(3)["merge_commit"], "c3")

    def test_an_open_pull_request_is_only_stamped_as_read(self):
        self.add(3)
        seen = post_merge_check.notice_merges(self.conn, FakeForge({3: {"state": "open"}}), NOW)
        self.assertEqual(seen["open"], [f"{REPO}/pull/3"])
        row = self.row(3)
        self.assertEqual((row["merged_at"], row["closed_at"], row["merge_checked_at"]), (None, None, NOW.isoformat()))

    def test_a_failed_read_is_logged_and_the_rest_still_run(self):
        self.add(4)
        self.add(5)
        forge = FakeForge({4: RuntimeError("broker down"), 5: {"state": "merged", "mergeCommit": "def"}})
        with self.assertLogs("post_merge_check", level="WARNING"):
            seen = post_merge_check.notice_merges(self.conn, forge, NOW)
        self.assertEqual((seen["failed"], seen["merged"]), ([f"{REPO}/pull/4"], [f"{REPO}/pull/5"]))
        self.assertIsNone(self.row(4)["merged_at"])
        self.assertEqual(self.row(4)["merge_checked_at"], NOW.isoformat(), "a failed read still rotates the queue")

    def test_a_database_error_on_one_record_does_not_stop_the_tick(self):
        self.add(10)
        self.add(11)
        real = self.conn

        class Flaky:
            calls = 0

            def execute(self, sql, params=()):
                if sql.startswith("UPDATE") and params[-1].endswith("/10"):
                    raise sqlite3.OperationalError("database is locked")
                return real.execute(sql, params)

            def __enter__(self):
                return real.__enter__()

            def __exit__(self, *exc):
                return real.__exit__(*exc)

        forge = FakeForge({10: {"state": "open"}, 11: {"state": "open"}})
        with self.assertLogs("post_merge_check", level="WARNING"):
            seen = post_merge_check.notice_merges(Flaky(), forge, NOW)
        self.assertEqual((seen["failed"], seen["open"]), ([f"{REPO}/pull/10"], [f"{REPO}/pull/11"]))

    def test_only_waiting_records_are_read(self):
        self.add(6, merged_at="2026-11-11T00:00:00Z")
        self.add(7, closed_at="2026-11-10T00:00:00Z")
        self.add(8, outcome="not_applied")
        self.add(9)
        forge = FakeForge({9: {"state": "open"}})
        post_merge_check.notice_merges(self.conn, forge, NOW)
        self.assertEqual(forge.reads, [(REPO, 9)])

    def test_at_most_twenty_reads_a_tick_and_every_record_gets_its_turn(self):
        for number in range(1, 31):
            self.add(number)
        forge = FakeForge({n: {"state": "open"} for n in range(1, 31)})
        post_merge_check.notice_merges(self.conn, forge, NOW)
        self.assertEqual(len(forge.reads), post_merge_check.READS_PER_TICK)
        first = {n for _, n in forge.reads}
        forge.reads.clear()
        post_merge_check.notice_merges(self.conn, forge, NOW + timedelta(minutes=5))
        second = {n for _, n in forge.reads}
        self.assertEqual(len(second), 20)
        self.assertTrue(set(range(1, 31)) - first <= second, "the ten not read last tick are read this one")

    def test_forge_view_reads_the_proposal_through_forge_call(self):
        import types
        from unittest import mock

        calls = []
        fake = types.ModuleType("forge")
        fake.call = lambda verb, payload, repo, **kw: calls.append((verb, payload, repo, kw)) or {
            "proposal": {"state": "merged"}
        }
        with mock.patch.dict(sys.modules, {"forge": fake}):
            self.assertEqual(post_merge_check.forge_view(REPO, 7), {"state": "merged"})
        self.assertEqual(calls[0][:3], ("proposal-view", {"number": 7}, REPO))
        self.assertTrue(calls[0][3]["retry_transient"])
        fake.call = lambda *a, **kw: {"forge": "github"}
        with mock.patch.dict(sys.modules, {"forge": fake}), self.assertRaises(TypeError):
            post_merge_check.forge_view(REPO, 7)


MERGE = "3d1a1b0bf1b13e4e40a0301114750aadf81bae38"
OLDER = "1" * 40


def argo_app(revision=MERGE, status="Synced", repo=REPO + ".git", namespace="shop"):
    return {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {"name": "shop", "namespace": "argocd"},
            "spec": {"source": {"repoURL": repo, "path": "clusters/prod/shop"}, "destination": {"namespace": namespace}},
            "status": {"sync": {"status": status, "revision": revision}, "operationState": {"phase": "Succeeded"}}}


def deployment(replicas=3, updated=3, available=3, ready=3, generation=2, observed=2):
    return {"kind": "Deployment", "metadata": {"name": "checkout", "generation": generation},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": "checkout"}},
                     "template": {"metadata": {"labels": {"app": "checkout", "tier": "web"}}}},
            "status": {"observedGeneration": observed, "updatedReplicas": updated, "availableReplicas": available,
                       "readyReplicas": ready}}


def pods(restarts=(0, 0, 0), ready=True, oom_at=None):
    items = []
    for n, count in enumerate(restarts):
        container = {"name": "web", "restartCount": count, "lastState": {}, "state": {"running": {}}}
        if oom_at and n == 0:
            container["lastState"] = {"terminated": {"reason": "OOMKilled", "finishedAt": oom_at}}
        items.append({"metadata": {"name": f"checkout-{n}", "uid": f"u{n}", "labels": {"pod-template-hash": "6d4f"},
                                   "ownerReferences": [{"kind": "ReplicaSet", "name": "checkout-6d4f"}]},
                      "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                                 "containerStatuses": [container]}})
    return {"items": items}


class FakeCluster:
    """``kubectl_json`` answered from a mutable table keyed by the resource asked for."""

    def __init__(self):
        self.apps = [argo_app(revision=OLDER, status="OutOfSync")]
        self.workload = deployment()
        self.pod_list = pods()
        self.calls = []
        self.fail = None
        self.budgets = []
        self.gone = False
        self.lists = {}

    def __call__(self, record, argv):
        self.calls.append(argv)
        if self.fail:
            raise self.fail
        resource = argv[1]
        if self.gone and resource in ("deployments.apps", "statefulsets.apps"):
            raise post_merge_check.WorkloadGone("deployments.apps \"checkout\" not found")
        if resource == "poddisruptionbudgets.policy":
            return {"items": self.budgets}
        if resource == post_merge_check.ARGO_APPLICATIONS:
            return {"items": self.apps}
        if resource in post_merge_check.RECONCILER_RESOURCES:
            return self.lists.get(resource, {"items": []})
        if resource in ("deployments.apps", "statefulsets.apps"):
            return self.workload
        if resource == "pods":
            self.selector = argv[argv.index("-l") + 1]
            return self.pod_list
        raise AssertionError(argv)


class WatchHealthTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(schema())
        self.conn.execute(
            "INSERT INTO post_merge_records (url, repo, number, kind, project, location, cluster, namespace, object, "
            "opened_at, merged_at, merge_commit) VALUES (?, ?, 1, 'no-pdb', 'p', 'l', 'prod', 'shop', "
            "'Deployment/checkout', ?, ?, ?)",
            (f"{REPO}/pull/1", REPO, NOW.isoformat(), NOW.isoformat(), MERGE),
        )
        self.cluster = FakeCluster()

    def row(self):
        self.conn.row_factory = sqlite3.Row
        found = dict(self.conn.execute("SELECT * FROM post_merge_records").fetchone())
        self.conn.row_factory = None
        return found

    def tick(self, minutes):
        return post_merge_check.watch_health(self.conn, self.cluster, NOW + timedelta(minutes=minutes))

    def test_waits_until_the_reconciler_applies_the_merge_commit(self):
        self.assertEqual(self.tick(1)["waiting"], [f"{REPO}/pull/1"])
        row = self.row()
        self.assertIsNone(row["applied_at"])
        self.assertTrue(row["sync_state"].startswith("pending"), row["sync_state"])

    def test_a_restart_from_before_the_apply_on_a_pod_still_starting_is_not_a_restart(self):
        # checkout-2 has one old restart and is not Ready at the apply reading; later all three are Ready
        # and no count has moved: the window passes, it does not fail on the old restart.
        self.cluster.apps = [argo_app()]
        starting = pods(restarts=(0, 0, 1))
        starting["items"][2]["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        self.cluster.pod_list = starting
        self.assertEqual(self.tick(1)["applied"], [f"{REPO}/pull/1"])
        baseline = json.loads(self.row()["health_baseline"])
        self.assertEqual(baseline, {"u0/web": 0, "u1/web": 0, "~u2/web": 1})
        self.cluster.pod_list = pods(restarts=(0, 0, 1))
        self.tick(4)
        self.assertEqual(self.tick(6)["passed"], [f"{REPO}/pull/1"])
        # A restart after the apply on that pod still fails it.
        self.conn.execute("UPDATE post_merge_records SET health_result = NULL, health_detail = NULL")
        self.cluster.pod_list = pods(restarts=(0, 0, 2))
        self.assertEqual(self.tick(8)["failed"], [f"{REPO}/pull/1"])

    def test_applied_records_the_baseline_then_passes_after_the_window(self):
        self.cluster.apps = [argo_app()]
        self.cluster.pod_list = pods(restarts=(2, 0, 0))
        self.assertEqual(self.tick(1)["applied"], [f"{REPO}/pull/1"])
        row = self.row()
        self.assertEqual(row["applied_at"], (NOW + timedelta(minutes=1)).isoformat())
        self.assertEqual(json.loads(row["health_baseline"]), {"u0/web": 2, "u1/web": 0, "u2/web": 0})
        self.assertEqual(self.cluster.selector, "app=checkout")
        # Inside the window nothing is judged; after it, a restart count at the baseline passes.
        self.assertEqual(self.tick(4)["waiting"], [f"{REPO}/pull/1"])
        self.assertEqual(self.tick(6)["passed"], [f"{REPO}/pull/1"])
        self.assertEqual(self.row()["health_result"], "passed")
        # Judged: not read again.
        self.cluster.calls.clear()
        self.tick(10)
        self.assertEqual(self.cluster.calls, [])

    def test_the_window_survives_a_restart_of_the_process(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        restarted = FakeCluster()
        restarted.apps = [argo_app()]
        post_merge_check.watch_health(self.conn, restarted, NOW + timedelta(minutes=3))
        restarted = FakeCluster()
        restarted.apps = [argo_app()]
        seen = post_merge_check.watch_health(self.conn, restarted, NOW + timedelta(minutes=7))
        self.assertEqual(seen["passed"], [f"{REPO}/pull/1"])
        self.assertNotIn(post_merge_check.ARGO_APPLICATIONS, [argv[1] for argv in restarted.calls])

    def test_each_failure_fails_the_window(self):
        for name, change in (
            ("restart", lambda c: setattr(c, "pod_list", pods(restarts=(1, 0, 0)))),
            ("not ready", lambda c: setattr(c, "pod_list", pods(ready=False))),
            ("oom", lambda c: setattr(c, "pod_list", pods(oom_at=(NOW + timedelta(minutes=3)).isoformat()))),
            ("no pods", lambda c: setattr(c, "pod_list", {"items": []})),
        ):
            with self.subTest(name):
                self.setUp()
                self.cluster.apps = [argo_app()]
                self.tick(1)
                change(self.cluster)
                self.assertEqual(self.tick(6)["failed"], [f"{REPO}/pull/1"])
                self.assertEqual(self.row()["health_result"], "failed")

    def test_healthy_then_red_after_the_sync_fails_the_window(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        self.assertEqual(self.tick(2)["waiting"], [f"{REPO}/pull/1"])
        self.assertTrue(self.row()["health_detail"].startswith("healthy samples: 1"), self.row()["health_detail"])
        # Argo still reads synced; a pod fails a minute after the apply.
        self.cluster.pod_list = pods(ready=False)
        self.assertEqual(self.tick(3)["failed"], [f"{REPO}/pull/1"])
        self.assertIn("not Ready", self.row()["health_detail"])
        # Recovering later does not undo it.
        self.cluster.pod_list = pods()
        self.cluster.calls.clear()
        self.tick(7)
        self.assertEqual(self.cluster.calls, [])

    def test_a_short_window_still_holds_sixty_seconds_after_the_sync_and_two_readings(self):
        from unittest import mock

        self.cluster.apps = [argo_app()]
        with mock.patch.dict(post_merge_check.HEALTH_WINDOW, {"no-pdb": timedelta(seconds=10)}):
            self.tick(1)
            at = NOW + timedelta(minutes=1)
            for seconds, bucket in ((20, "waiting"), (40, "waiting"), (61, "passed")):
                seen = post_merge_check.watch_health(self.conn, self.cluster, at + timedelta(seconds=seconds))
                self.assertEqual(seen[bucket], [f"{REPO}/pull/1"], seconds)

    def test_a_rollout_or_scale_under_way_waits_then_fails_only_past_the_settle_limit(self):
        for name, workload in (("rollout", deployment(updated=2)), ("generation", deployment(observed=1)),
                               ("scale-up", deployment(replicas=4))):
            with self.subTest(name):
                self.setUp()
                self.cluster.apps = [argo_app()]
                self.tick(1)
                self.cluster.workload = workload
                self.assertEqual(self.tick(6)["waiting"], [f"{REPO}/pull/1"])
                self.assertTrue(self.row()["health_detail"].startswith("not settled"), self.row()["health_detail"])
                with self.assertLogs("post_merge_check", level="WARNING"):
                    self.assertEqual(self.tick(40)["failed"], [f"{REPO}/pull/1"])
                self.assertIn("not healthy 35 minutes after the apply", self.row()["health_detail"])

    def test_an_evicted_pod_beside_three_ready_ones_passes(self):
        # Kept by the pod garbage collector with its owner and hash, but the ReplicaSet no longer counts it.
        evicted = pods(restarts=(0,), ready=False)["items"][0]
        evicted["metadata"].update(name="checkout-evicted", uid="ue")
        evicted["status"].update(phase="Failed", reason="Evicted")
        for name, with_statuses in (("in the baseline", True), ("no container statuses", False)):
            with self.subTest(name):
                self.setUp()
                pod = json.loads(json.dumps(evicted))
                if not with_statuses:
                    pod["status"].pop("containerStatuses")
                self.cluster.apps = [argo_app()]
                self.cluster.pod_list = {"items": pods()["items"] + [pod]}
                self.tick(1)
                self.assertNotIn("ue/web", json.loads(self.row()["health_baseline"]))
                self.tick(4)
                self.assertEqual(self.tick(6)["passed"], [f"{REPO}/pull/1"])

    def test_a_pod_starting_at_the_apply_is_waited_for_not_failed(self):
        self.cluster.apps = [argo_app()]
        starting = pods(restarts=(0,), ready=False)["items"][0]
        starting["metadata"].update(name="checkout-new", uid="un")
        self.cluster.pod_list = {"items": pods()["items"] + [starting]}
        self.cluster.workload = deployment(replicas=4, updated=4)
        self.tick(1)
        self.assertNotIn("un/web", json.loads(self.row()["health_baseline"]))
        self.assertEqual(self.tick(2)["waiting"], [f"{REPO}/pull/1"])
        self.assertTrue(self.row()["health_detail"].startswith("not settled"), self.row()["health_detail"])
        starting["status"]["conditions"][0]["status"] = "True"
        self.tick(4)
        self.assertEqual(self.tick(6)["passed"], [f"{REPO}/pull/1"])

    def test_one_failed_read_past_the_settle_limit_is_read_again_not_ended(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        self.cluster.workload = deployment(updated=2)
        self.tick(6)
        self.cluster.workload = deployment()
        self.tick(34)  # healthy, one sample
        self.cluster.fail = post_merge_check.ClusterReadError("i/o timeout")
        with self.assertLogs("post_merge_check", level="WARNING"):
            self.assertEqual(self.tick(35)["unreadable"], [f"{REPO}/pull/1"])
        self.cluster.fail = None
        self.assertEqual(self.tick(38)["waiting"], [f"{REPO}/pull/1"], "read, healthy again: counted, not ended")
        self.assertEqual(self.tick(40)["passed"], [f"{REPO}/pull/1"])

    def test_a_workload_scaled_to_zero_is_healthy(self):
        self.cluster.apps = [argo_app()]
        self.cluster.workload = deployment(replicas=0, updated=0, available=0, ready=0)
        self.cluster.pod_list = {"items": []}
        self.tick(1)
        self.tick(4)
        self.assertEqual(self.tick(6)["passed"], [f"{REPO}/pull/1"])
        self.assertIn("scaled to zero", self.row()["health_detail"])

    def test_a_rollout_that_settles_resets_the_count_then_passes(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        self.cluster.workload = deployment(updated=2)
        self.tick(2)
        self.cluster.workload = deployment()
        self.assertEqual(self.tick(6)["waiting"], [f"{REPO}/pull/1"])
        self.assertEqual(self.tick(7)["passed"], [f"{REPO}/pull/1"])

    def test_reads_failing_past_the_settle_limit_fail_with_the_last_reading(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        self.cluster.fail = post_merge_check.ClusterReadError("i/o timeout")
        with self.assertLogs("post_merge_check", level="WARNING"):
            self.assertEqual(self.tick(6)["unreadable"], [f"{REPO}/pull/1"])
            self.assertEqual(self.tick(40)["failed"], [f"{REPO}/pull/1"])
        self.assertIn("i/o timeout", self.row()["health_detail"])

    def test_a_pod_that_goes_not_ready_fails_at_once_though_the_controller_counts_drop(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        # What a real controller reports: one pod NotReady, ready and available counts down to 2.
        self.cluster.workload = deployment(ready=2, available=2)
        self.cluster.pod_list = pods()
        self.cluster.pod_list["items"][1]["status"]["conditions"][0]["status"] = "False"
        self.assertEqual(self.tick(2)["failed"], [f"{REPO}/pull/1"])
        self.assertIn("pod checkout-1 is not Ready", self.row()["health_detail"])

    def test_a_new_pod_still_starting_after_a_scale_up_waits(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        self.cluster.workload = deployment(replicas=4, updated=4, ready=3, available=3)
        starting = pods(restarts=(0,), ready=False)["items"][0]
        starting["metadata"].update(name="checkout-new", uid="new")
        self.cluster.pod_list = pods()
        self.cluster.pod_list["items"].append(starting)
        self.assertEqual(self.tick(2)["waiting"], [f"{REPO}/pull/1"])
        self.assertIn("pod checkout-new is starting", self.row()["health_detail"])

    def test_a_pod_from_a_scale_up_seen_ready_then_not_ready_fails(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        self.cluster.workload = deployment(replicas=4, updated=4, ready=4, available=4)
        scaled = pods(restarts=(0,))["items"][0]
        scaled["metadata"].update(name="checkout-new", uid="new")
        self.cluster.pod_list = pods()
        self.cluster.pod_list["items"].append(scaled)
        self.assertEqual(self.tick(2)["waiting"], [f"{REPO}/pull/1"])
        self.assertIn("new/web", json.loads(self.row()["health_baseline"]), "a pod seen Ready joins the baseline")
        scaled["status"]["conditions"][0]["status"] = "False"
        self.assertEqual(self.tick(3)["failed"], [f"{REPO}/pull/1"])
        self.assertIn("pod checkout-new is not Ready", self.row()["health_detail"])

    def test_a_known_pod_not_ready_fails_even_while_a_new_pod_is_starting(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        self.cluster.workload = deployment(replicas=4, updated=4, ready=2, available=2)
        starting = pods(restarts=(0,), ready=False)["items"][0]
        starting["metadata"].update(name="checkout-new", uid="new")
        self.cluster.pod_list = pods()
        self.cluster.pod_list["items"][1]["status"]["conditions"][0]["status"] = "False"
        self.cluster.pod_list["items"].append(starting)
        self.assertEqual(self.tick(2)["failed"], [f"{REPO}/pull/1"])
        self.assertIn("pod checkout-1 is not Ready", self.row()["health_detail"])

    def test_a_sidecar_restart_after_the_apply_fails_the_window(self):
        def with_sidecar(restarts):
            listed = pods()
            for item in listed["items"]:
                item["status"]["initContainerStatuses"] = [
                    {"name": "proxy", "restartCount": restarts, "lastState": {}, "state": {"running": {}}}]
            return listed

        self.cluster.apps = [argo_app()]
        self.cluster.pod_list = with_sidecar(1)
        self.tick(1)
        self.assertEqual(json.loads(self.row()["health_baseline"])["u0/proxy"], 1, "the baseline covers sidecars")
        self.cluster.pod_list = with_sidecar(2)
        self.assertEqual(self.tick(2)["failed"], [f"{REPO}/pull/1"])
        self.assertIn("container proxy in pod checkout-0 restarted", self.row()["health_detail"])

    def test_a_terminating_pod_is_not_judged(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        draining = pods(restarts=(0,), ready=False)["items"][0]
        draining["metadata"].update(name="checkout-old", uid="old", deletionTimestamp=NOW.isoformat())
        self.cluster.pod_list = pods()
        self.cluster.pod_list["items"].append(draining)
        self.tick(3)
        self.assertEqual(self.tick(6)["passed"], [f"{REPO}/pull/1"])

    def test_a_baseline_that_is_not_a_map_of_counts_fails_with_the_reason(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        for value in ("null", "[]", '{"u0/web": "x"}'):
            with self.subTest(value):
                self.conn.execute("UPDATE post_merge_records SET health_baseline = ?, health_result = NULL", (value,))
                with self.assertLogs("post_merge_check", level="WARNING"):
                    self.assertEqual(self.tick(3)["failed"], [f"{REPO}/pull/1"])
                self.assertIn("baseline is unreadable", self.row()["health_detail"])

    def test_an_oom_with_no_time_in_the_past_state_does_not_fail_it(self):
        listed = pods()
        listed["items"][0]["status"]["containerStatuses"][0]["lastState"] = {"terminated": {"reason": "OOMKilled"}}
        passed, _ = post_merge_check.judge_health(deployment(), listed, {}, NOW)
        self.assertTrue(passed)
        listed["items"][0]["status"]["containerStatuses"][0]["state"] = {"terminated": {"reason": "OOMKilled"}}
        passed, _ = post_merge_check.judge_health(deployment(), listed, {}, NOW)
        self.assertFalse(passed)

    def test_an_oom_before_the_apply_does_not_fail_it(self):
        self.cluster.apps = [argo_app()]
        self.cluster.pod_list = pods(oom_at=(NOW - timedelta(hours=1)).isoformat())
        self.tick(1)
        self.tick(3)
        self.assertEqual(self.tick(6)["passed"], [f"{REPO}/pull/1"])

    def test_an_unreadable_cluster_is_logged_and_retried(self):
        self.cluster.fail = post_merge_check.ClusterReadError("connection refused")
        with self.assertLogs("post_merge_check", level="WARNING"):
            self.assertEqual(self.tick(1)["unreadable"], [f"{REPO}/pull/1"])
        self.assertTrue(self.row()["sync_state"].startswith("unreadable"))
        self.cluster.fail = None
        self.cluster.apps = [argo_app()]
        self.assertEqual(self.tick(2)["applied"], [f"{REPO}/pull/1"])

    def test_a_qualified_cluster_fills_the_coordinates_the_record_left_empty(self):
        self.conn.execute("UPDATE post_merge_records SET project = '', location = '', cluster = 'p/l/prod'")
        self.cluster.apps = [argo_app()]
        self.assertEqual(self.tick(1)["applied"], [f"{REPO}/pull/1"])

    def test_a_record_with_no_cluster_coordinates_is_not_read(self):
        self.conn.execute("UPDATE post_merge_records SET project = ''")
        with self.assertLogs("post_merge_check", level="WARNING"):
            self.assertEqual(self.tick(1)["unreadable"], [f"{REPO}/pull/1"])
        self.assertEqual(self.cluster.calls, [])

    def test_a_later_applied_commit_that_contains_the_merge_is_applied(self):
        self.cluster.apps = [argo_app(revision=OLDER)]
        asked = []

        def contains(repo, ancestor, revision):
            asked.append((repo, ancestor, revision))
            return True

        seen = post_merge_check.watch_health(self.conn, self.cluster, NOW + timedelta(minutes=1), contains=contains)
        self.assertEqual(seen["applied"], [f"{REPO}/pull/1"])
        self.assertEqual(asked, [(REPO, MERGE, OLDER)])

    def test_a_later_commit_without_the_merge_or_unknown_ancestry_waits(self):
        self.cluster.apps = [argo_app(revision=OLDER)]
        for answer, state in ((False, "pending"), (None, "unconfirmed")):
            with self.subTest(answer=answer):
                post_merge_check.watch_health(self.conn, self.cluster, NOW, contains=lambda *a, answer=answer: answer)
                self.assertTrue(self.row()["sync_state"].startswith(state), self.row()["sync_state"])
                self.assertIsNone(self.row()["applied_at"])

    def test_unknown_ancestry_falls_back_to_observing_the_budget(self):
        self.cluster.apps = [argo_app(revision=OLDER)]
        self.cluster.budgets = [{"metadata": {"name": "checkout-pdb"}, "spec": {"selector": {"matchLabels": {"app": "checkout"}}}}]
        seen = post_merge_check.watch_health(self.conn, self.cluster, NOW, contains=lambda *a: None)
        self.assertEqual(seen["applied"], [f"{REPO}/pull/1"])
        state = self.row()["sync_state"]
        self.assertTrue(state.startswith("observed"), state)
        self.assertIn("could not be confirmed", state)
        self.assertNotIn("no reconciler targets", state, "a reconciler does target the namespace")

    def test_a_failed_reconciler_list_is_read_once_per_cluster(self):
        self.conn.execute(
            "INSERT INTO post_merge_records (url, repo, number, kind, project, location, cluster, namespace, object, "
            "opened_at, merged_at, merge_commit) SELECT ?, repo, 2, kind, project, location, cluster, namespace, "
            "object, opened_at, merged_at, merge_commit FROM post_merge_records WHERE number = 1",
            (f"{REPO}/pull/2",),
        )
        self.cluster.fail = post_merge_check.ClusterReadError("connection refused")
        with self.assertLogs("post_merge_check", level="WARNING"):
            seen = self.tick(1)
        self.assertEqual(sorted(seen["unreadable"]), [f"{REPO}/pull/1", f"{REPO}/pull/2"])
        self.assertEqual(len(self.cluster.calls), 1, self.cluster.calls)

    def test_a_record_whose_update_fails_is_reported_unreadable_not_applied(self):
        real = self.conn
        real.commit()  # the failed UPDATE rolls back; keep setUp's row

        class Locked:
            def execute(self, sql, params=()):
                if sql.startswith("UPDATE"):
                    raise sqlite3.OperationalError("database is locked")
                return real.execute(sql, params)

            def __enter__(self):
                return real.__enter__()

            def __exit__(self, *exc):
                return real.__exit__(*exc)

        self.cluster.apps = [argo_app()]
        with self.assertLogs("post_merge_check", level="WARNING"):
            seen = post_merge_check.watch_health(Locked(), self.cluster, NOW + timedelta(minutes=1))
        self.assertEqual((seen["applied"], seen["unreadable"]), ([], [f"{REPO}/pull/1"]))
        self.assertIsNone(self.row()["applied_at"])

    def test_a_root_sync_delivering_the_changed_file_is_read_and_others_are_not(self):
        root = {"apiVersion": "configsync.gke.io/v1beta1", "kind": "RootSync",
                "metadata": {"name": "root-sync", "namespace": "config-management-system"},
                "spec": {"git": {"repo": REPO, "dir": "clusters/prod"}}, "status": {"lastSyncedCommit": MERGE}}
        self.cluster.apps = []
        self.cluster.lists = {post_merge_check.CONFIGSYNC_ROOTSYNCS: {"items": [root]}}
        asked = []

        def paths(repo, number, name):
            asked.append((repo, number, name))
            return ["clusters/prod/shop/pdb.yaml"]

        seen = post_merge_check.watch_health(self.conn, self.cluster, NOW + timedelta(minutes=1), paths=paths)
        self.assertEqual(asked, [(REPO, 1, "checkout")])
        self.assertEqual(seen["applied"], [f"{REPO}/pull/1"])
        self.assertTrue(self.row()["sync_state"].startswith("applied"), self.row()["sync_state"])
        # No reconciler names the repository: the files are not read at all.
        self.conn.execute("UPDATE post_merge_records SET applied_at = NULL, sync_state = NULL")
        root["spec"]["git"]["repo"] = "https://github.com/acme/other"
        asked.clear()
        post_merge_check.watch_health(self.conn, self.cluster, NOW + timedelta(minutes=2), paths=paths)
        self.assertEqual(asked, [])

    def test_the_files_are_read_once_and_kept_on_the_row(self):
        root = {"kind": "RootSync", "metadata": {"name": "root-sync", "namespace": "config-management-system"},
                "spec": {"git": {"repo": REPO, "dir": "clusters/prod"}}, "status": {"lastSyncedCommit": OLDER}}
        self.cluster.apps = []
        self.cluster.lists = {post_merge_check.CONFIGSYNC_ROOTSYNCS: {"items": [root]}}
        asked = []

        def paths(repo, number, name):
            asked.append(number)
            return ["clusters/prod/shop/pdb.yaml"]

        for minutes in (1, 2, 3):
            post_merge_check.watch_health(self.conn, self.cluster, NOW + timedelta(minutes=minutes),
                                          contains=lambda *a: False, paths=paths)
        self.assertEqual(asked, [1])
        self.assertEqual(json.loads(self.row()["changed_paths"]), ["clusters/prod/shop/pdb.yaml"])
        self.assertNotIn("files unknown", self.row()["sync_state"])

    def test_unknown_files_are_said_in_the_reading_and_read_again(self):
        root = {"kind": "RootSync", "metadata": {"name": "root-sync", "namespace": "config-management-system"},
                "spec": {"git": {"repo": REPO, "dir": "clusters/prod"}}, "status": {"lastSyncedCommit": MERGE}}
        self.cluster.apps = []
        self.cluster.lists = {post_merge_check.CONFIGSYNC_ROOTSYNCS: {"items": [root]}}
        asked = []

        def unknown(repo, number, name):
            asked.append(number)

        for minutes in (1, 2):
            seen = post_merge_check.watch_health(self.conn, self.cluster, NOW + timedelta(minutes=minutes), paths=unknown)
            self.assertEqual(seen["waiting"], [f"{REPO}/pull/1"])
        self.assertEqual(asked, [1, 1], "an unknown answer is not kept")
        self.assertIsNone(self.row()["changed_paths"])
        self.assertIn("files unknown", self.row()["sync_state"])

    def test_a_root_sync_of_another_file_in_the_pull_request_never_marks_the_workload_applied(self):
        from unittest import mock

        # The pull request changes platform/x.yaml and the workload's own budget under apps/team; the
        # RootSync on this cluster delivers platform/ and has synced past the merge.
        diff = ("diff --git a/platform/x.yaml b/platform/x.yaml\n--- a/platform/x.yaml\n+++ b/platform/x.yaml\n"
                "@@ -1 +1 @@\n-replicas: 1\n+replicas: 2\n"
                "diff --git a/apps/team/pdb.yaml b/apps/team/pdb.yaml\n--- /dev/null\n+++ b/apps/team/pdb.yaml\n"
                "@@ -0,0 +2 @@\n+  selector: {matchLabels: {app: checkout}}\n+  maxUnavailable: 1\n")
        root = {"apiVersion": "configsync.gke.io/v1beta1", "kind": "RootSync",
                "metadata": {"name": "root-sync", "namespace": "config-management-system"},
                "spec": {"git": {"repo": REPO, "dir": "platform"}}, "status": {"lastSyncedCommit": MERGE}}
        self.cluster.apps = []
        self.cluster.lists = {post_merge_check.CONFIGSYNC_ROOTSYNCS: {"items": [root]}}
        forge = mock.Mock(call=lambda *a, **k: {"diff": diff})
        with mock.patch.dict(sys.modules, {"forge": forge}):
            seen = post_merge_check.watch_health(self.conn, self.cluster, NOW + timedelta(minutes=1))
        self.assertEqual(seen["waiting"], [f"{REPO}/pull/1"], "observed directly, and no budget is live yet")
        self.assertIn("no reconciler targets", self.row()["sync_state"])
        self.assertEqual(json.loads(self.row()["changed_paths"]), ["apps/team/pdb.yaml"])
        # Its own directory holding the workload's file does count.
        root["spec"]["git"]["dir"] = "apps"
        self.conn.execute("UPDATE post_merge_records SET sync_state = NULL")
        with mock.patch.dict(sys.modules, {"forge": forge}):
            seen = post_merge_check.watch_health(self.conn, self.cluster, NOW + timedelta(minutes=2))
        self.assertEqual(seen["applied"], [f"{REPO}/pull/1"], self.row()["sync_state"])

    def test_with_no_reconciler_for_the_namespace_the_budget_is_observed_directly(self):
        self.cluster.apps = [argo_app(namespace="elsewhere")]
        self.assertEqual(self.tick(1)["waiting"], [f"{REPO}/pull/1"])
        self.assertIn("no budget selects the workload yet", self.row()["sync_state"])
        self.cluster.budgets = [{"metadata": {"name": "checkout-pdb"}, "spec": {"selector": {"matchLabels": {"app": "checkout"}}}}]
        self.assertEqual(self.tick(2)["applied"], [f"{REPO}/pull/1"])
        self.assertTrue(self.row()["sync_state"].startswith("observed"), self.row()["sync_state"])

    def test_a_deleted_workload_fails_with_the_reason_and_is_not_read_again(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        self.cluster.gone = True
        with self.assertLogs("post_merge_check", level="WARNING"):
            self.assertEqual(self.tick(6)["failed"], [f"{REPO}/pull/1"])
        row = self.row()
        self.assertEqual(row["health_result"], "failed")
        self.assertIn("not found", row["health_detail"])
        self.cluster.calls.clear()
        self.tick(10)
        self.assertEqual(self.cluster.calls, [])

    def test_an_unreadable_apply_time_fails_rather_than_waiting_forever(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        self.conn.execute("UPDATE post_merge_records SET applied_at = 'garbage'")
        with self.assertLogs("post_merge_check", level="WARNING"):
            self.assertEqual(self.tick(2)["failed"], [f"{REPO}/pull/1"])
        self.assertIn("apply time", self.row()["health_detail"])

    def test_a_read_failure_after_the_apply_is_kept_and_retried(self):
        self.cluster.apps = [argo_app()]
        self.tick(1)
        self.cluster.fail = post_merge_check.ClusterReadError("i/o timeout")
        with self.assertLogs("post_merge_check", level="WARNING"):
            self.assertEqual(self.tick(6)["unreadable"], [f"{REPO}/pull/1"])
        self.assertIn("i/o timeout", self.row()["health_detail"])
        self.assertIsNone(self.row()["health_result"])
        self.cluster.fail = None
        # The failed read reset the count: one healthy reading is not enough.
        self.assertEqual(self.tick(7)["waiting"], [f"{REPO}/pull/1"])
        self.assertEqual(self.tick(8)["passed"], [f"{REPO}/pull/1"])

    def test_records_are_read_least_recently_checked_first_whatever_their_state(self):
        for n in range(2, 23):
            self.conn.execute(
                "INSERT INTO post_merge_records (url, repo, number, kind, project, location, cluster, namespace, "
                "object, opened_at, merged_at, merge_commit) SELECT ?, repo, ?, kind, project, location, cluster, "
                "namespace, object, opened_at, merged_at, merge_commit FROM post_merge_records WHERE number = 1",
                (f"{REPO}/pull/{n}", n),
            )
        first = set(self.tick(1)["waiting"])
        second = set(self.tick(2)["waiting"])
        self.assertEqual(len(first), 20)
        self.assertEqual(len(first | second), 22, "every record is read within two ticks")

    def test_unmerged_closed_or_finished_records_are_not_read(self):
        self.conn.execute("UPDATE post_merge_records SET outcome = 'verified_fixed'")
        self.tick(1)
        self.assertEqual(self.cluster.calls, [])


class SelectorAndRolloutTest(unittest.TestCase):
    def test_a_pod_belongs_to_the_deployment_only_through_its_own_replica_set(self):
        def pod(rs, template_hash):
            return {"metadata": {"labels": {"pod-template-hash": template_hash},
                                 "ownerReferences": [{"kind": "ReplicaSet", "name": rs}]}}

        web = {"kind": "Deployment", "metadata": {"name": "web"}}
        self.assertTrue(post_merge_check._owned_by(pod("web-6d4f", "6d4f"), web))
        self.assertFalse(post_merge_check._owned_by(pod("web-canary-7c9b", "7c9b"), web))
        self.assertFalse(post_merge_check._owned_by({"metadata": {"name": "bare"}}, web), "a pod with no owner")

    def test_label_selector_renders_match_expressions(self):
        selector = {"matchLabels": {"app": "web"}, "matchExpressions": [
            {"key": "tier", "operator": "In", "values": ["a", "b"]}, {"key": "canary", "operator": "DoesNotExist"},
            {"key": "zone", "operator": "Exists"}, {"key": "env", "operator": "NotIn", "values": ["dev"]}]}
        self.assertEqual(post_merge_check.label_selector(selector), "app=web,tier in (a,b),!canary,zone,env notin (dev)")
        with self.assertRaises(post_merge_check.WorkloadGone):
            post_merge_check.label_selector({"matchExpressions": [{"key": "x", "operator": "Gt"}]})

    def test_selects_matches_labels_and_expressions(self):
        selector = {"matchLabels": {"app": "web"}, "matchExpressions": [{"key": "tier", "operator": "In", "values": ["a"]}]}
        self.assertTrue(post_merge_check.selects(selector, {"app": "web", "tier": "a"}))
        self.assertFalse(post_merge_check.selects(selector, {"app": "web", "tier": "b"}))
        self.assertFalse(post_merge_check.selects({}, {"app": "web"}))

    def test_a_statefulset_is_rolled_out_only_at_its_update_revision(self):
        sts = {"kind": "StatefulSet", "metadata": {"generation": 1},
               "spec": {"replicas": 2}, "status": {"observedGeneration": 1, "readyReplicas": 2,
                                                   "currentRevision": "r1", "updateRevision": "r2"}}
        self.assertFalse(post_merge_check._rolled_out(sts))
        sts["status"]["currentRevision"] = "r2"
        self.assertTrue(post_merge_check._rolled_out(sts))

    def test_pods_of_another_workload_are_not_judged(self):
        foreign = pods(restarts=(5,))
        foreign["items"][0]["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "canary-77"}]
        mine = pods()
        mine["items"] += foreign["items"]
        passed, _ = post_merge_check.judge_health(deployment(), mine, {}, NOW)
        self.assertTrue(passed)


class SandboxKubectlTest(unittest.TestCase):
    def run_with(self, returncode, stdout="", stderr=""):
        import subprocess
        import types
        from unittest import mock

        fake = types.ModuleType("stall_watch")
        fake.fetch_credentials = lambda *key: "/home/hermes/.kubeconfigs/x.yaml"
        fake.run_sandbox = lambda argv, timeout, kubeconfig: subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        fake.stderr_excerpt = lambda text: text
        record = {"project": "p", "cluster": "c", "location": "l"}
        with mock.patch.dict(sys.modules, {"stall_watch": fake}):
            return post_merge_check.SandboxKubectl()(record, ["get", "x", "-o", "json"])

    def test_answers_parse_and_errors_are_typed(self):
        self.assertEqual(self.run_with(0, '{"items": [1]}'), {"items": [1]})
        self.assertEqual(self.run_with(1, stderr="error: the server doesn't have a resource type \"x\""), {"items": []})
        with self.assertRaises(post_merge_check.WorkloadGone):
            self.run_with(1, stderr='Error from server (NotFound): deployments.apps "x" not found')
        with self.assertRaises(post_merge_check.ClusterReadError):
            self.run_with(1, stderr="dial tcp: i/o timeout")

    def test_a_qualified_cluster_name_reaches_gcloud_as_its_parts(self):
        import subprocess
        import types
        from unittest import mock

        asked = []
        fake = types.ModuleType("stall_watch")
        fake.fetch_credentials = lambda *key: asked.append(key) or "/home/hermes/.kubeconfigs/x.yaml"
        fake.run_sandbox = lambda argv, timeout, kubeconfig: subprocess.CompletedProcess(argv, 0, '{"items": []}', "")
        fake.stderr_excerpt = lambda text: text
        with mock.patch.dict(sys.modules, {"stall_watch": fake}):
            post_merge_check.SandboxKubectl()({"project": "acme-prod", "cluster": "acme-prod/us-east1/shared",
                                               "location": "us-east1"}, ["get", "x", "-o", "json"])
            post_merge_check.SandboxKubectl()({"project": "", "cluster": "acme-dev/europe-west1/web",
                                               "location": ""}, ["get", "x", "-o", "json"])
            post_merge_check.SandboxKubectl()({"project": "p", "cluster": "bare", "location": "l"},
                                              ["get", "x", "-o", "json"])
        self.assertEqual(asked, [("acme-prod", "shared", "us-east1"), ("acme-dev", "web", "europe-west1"),
                                 ("p", "bare", "l")])

    def test_a_list_that_is_not_found_is_empty_not_a_gone_workload(self):
        import subprocess
        import types
        from unittest import mock

        fake = types.ModuleType("stall_watch")
        fake.fetch_credentials = lambda *key: "/home/hermes/.kubeconfigs/x.yaml"
        fake.run_sandbox = lambda argv, timeout, kubeconfig: subprocess.CompletedProcess(
            argv, 1, "", "Error from server (NotFound): the server could not find the requested resource")
        fake.stderr_excerpt = lambda text: text
        record = {"project": "p", "cluster": "c", "location": "l"}
        with mock.patch.dict(sys.modules, {"stall_watch": fake}):
            self.assertEqual(post_merge_check.SandboxKubectl()(record, ["get", "kustomizations.x", "-A", "-o", "json"]),
                             {"items": []})


class ReconcilersForTest(unittest.TestCase):
    def test_matches_the_repository_whatever_its_spelling(self):
        for repo in (REPO, REPO + ".git", REPO + "/", "git@github.com:acme/fleet.git", "HTTPS://GitHub.com/acme/fleet",
                     "ssh://git@github.com:22/acme/fleet.git", "github.com:acme/fleet.git", "https://www.github.com/acme/fleet"):
            with self.subTest(repo=repo):
                lists = {post_merge_check.ARGO_APPLICATIONS: {"items": [argo_app(repo=repo)]}}
                self.assertEqual(len(post_merge_check.reconcilers_for(REPO, "shop", lists)), 1)
        lists = {post_merge_check.ARGO_APPLICATIONS: {"items": [argo_app(repo="https://github.com/acme/other")]}}
        self.assertEqual(post_merge_check.reconcilers_for(REPO, "shop", lists), [])
        # A record with no repository matches nothing, not every source with a missing url.
        lists = {post_merge_check.ARGO_APPLICATIONS: {"items": [argo_app(repo="")]}}
        self.assertEqual(post_merge_check.reconcilers_for("", "shop", lists), [])

    def test_only_objects_delivering_into_the_namespace_on_this_cluster_count(self):
        elsewhere = argo_app()
        elsewhere["spec"]["destination"]["server"] = "https://10.0.0.1"
        lists = {post_merge_check.ARGO_APPLICATIONS: {"items": [
            argo_app(namespace="shop"), argo_app(namespace="other"), argo_app(namespace=""), elsewhere]}}
        found = post_merge_check.reconcilers_for(REPO, "shop", lists)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["spec"]["destination"], {"namespace": "shop"})

    def test_reads_flux_through_its_git_repository_and_config_sync(self):
        lists = {
            post_merge_check.FLUX_GIT_REPOSITORIES: {"items": [
                {"metadata": {"name": "fleet", "namespace": "flux-system"}, "spec": {"url": REPO}}]},
            post_merge_check.FLUX_KUSTOMIZATIONS: {"items": [
                {"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
                 "metadata": {"name": "shop", "namespace": "flux-system"},
                 "spec": {"sourceRef": {"kind": "GitRepository", "name": "fleet"}, "targetNamespace": "shop"}}]},
            post_merge_check.CONFIGSYNC_ROOTSYNCS: {"items": [
                {"kind": "RootSync", "metadata": {"name": "root-sync", "namespace": "config-management-system"},
                 "spec": {"git": {"repo": REPO + ".git"}}}]},
            post_merge_check.CONFIGSYNC_REPOSYNCS: {"items": [
                {"kind": "RepoSync", "metadata": {"name": "repo-sync", "namespace": "shop"},
                 "spec": {"git": {"repo": REPO + ".git"}}}]},
        }
        kinds = sorted(o["kind"] for o in post_merge_check.reconcilers_for(REPO, "shop", lists))
        self.assertEqual(kinds, ["Kustomization", "RepoSync"])

    def test_a_root_sync_counts_when_its_dir_holds_a_changed_file(self):
        root = {"kind": "RootSync", "metadata": {"name": "root-sync", "namespace": "config-management-system"},
                "spec": {"sourceType": "git", "git": {"repo": REPO + ".git", "dir": "clusters/dev"}}}
        other_repo = {"kind": "RootSync", "metadata": {"name": "apps", "namespace": "config-management-system"},
                      "spec": {"git": {"repo": "https://github.com/acme/apps", "dir": "/"}}}
        lists = {post_merge_check.CONFIGSYNC_ROOTSYNCS: {"items": [root, other_repo]}}
        self.assertEqual(post_merge_check.reconcilers_for(REPO, "shop", lists, ["clusters/dev/shop/pdb.yaml"]), [root])
        for paths in (["clusters/devx/shop/pdb.yaml"], ["clusters/prod/shop/pdb.yaml"], None, []):
            with self.subTest(paths=paths):
                self.assertEqual(post_merge_check.reconcilers_for(REPO, "shop", lists, paths), [])

    def test_a_root_sync_dir_is_read_however_it_is_written(self):
        for directory in ("clusters/dev", "/clusters/dev/", "./clusters/dev", "clusters/dev/", "/./clusters/dev",
                          "clusters/dev/.", "clusters//dev", "././clusters/./dev"):
            with self.subTest(dir=directory):
                root = {"kind": "RootSync", "spec": {"git": {"repo": REPO, "dir": directory}}}
                lists = {post_merge_check.CONFIGSYNC_ROOTSYNCS: {"items": [root]}}
                self.assertEqual(len(post_merge_check.reconcilers_for(REPO, "shop", lists, ["clusters/dev/a.yaml"])), 1)
        for directory in (None, "", "/", ".", "./"):
            with self.subTest(dir=directory):
                root = {"kind": "RootSync", "spec": {"git": {"repo": REPO, "dir": directory}}}
                lists = {post_merge_check.CONFIGSYNC_ROOTSYNCS: {"items": [root]}}
                self.assertEqual(len(post_merge_check.reconcilers_for(REPO, "shop", lists, ["any/where.yaml"])), 1)

    def test_a_root_sync_from_an_oci_or_helm_source_never_counts(self):
        root = {"kind": "RootSync", "spec": {"sourceType": "oci", "git": {"repo": REPO, "dir": "/"}}}
        lists = {post_merge_check.CONFIGSYNC_ROOTSYNCS: {"items": [root]}}
        self.assertEqual(post_merge_check.reconcilers_for(REPO, "shop", lists, ["a.yaml"]), [])

    def test_a_path_narrows_argo_and_flux_and_never_empties_the_match(self):
        dev, prod = argo_app(), argo_app()
        dev["spec"]["source"]["path"] = "overlays/dev"
        prod["spec"]["source"]["path"] = "overlays/prod"
        lists = {post_merge_check.ARGO_APPLICATIONS: {"items": [dev, prod]}}
        self.assertEqual(post_merge_check.reconcilers_for(REPO, "shop", lists, ["overlays/dev/pdb.yaml"]), [dev])
        # A file outside every path (a shared base a kustomization reaches) keeps them all.
        self.assertEqual(post_merge_check.reconcilers_for(REPO, "shop", lists, ["base/pdb.yaml"]), [dev, prod])
        self.assertEqual(post_merge_check.reconcilers_for(REPO, "shop", lists, None), [dev, prod])
        lists = {
            post_merge_check.FLUX_GIT_REPOSITORIES: {"items": [
                {"metadata": {"name": "fleet", "namespace": "flux-system"}, "spec": {"url": REPO}}]},
            post_merge_check.FLUX_KUSTOMIZATIONS: {"items": [
                {"kind": "Kustomization", "metadata": {"name": n, "namespace": "flux-system"},
                 "spec": {"sourceRef": {"kind": "GitRepository", "name": "fleet"}, "targetNamespace": "shop",
                          "path": f"./clusters/{n}"}} for n in ("dev", "prod")]},
        }
        found = post_merge_check.reconcilers_for(REPO, "shop", lists, ["clusters/prod/shop/pdb.yaml"])
        self.assertEqual([o["metadata"]["name"] for o in found], ["prod"])

    def test_diff_paths_reads_only_headers_and_unquotes(self):
        diff = ("diff --git a/notes.patch b/notes.patch\n--- a/notes.patch\n+++ b/notes.patch\n@@ -1 +1,2 @@\n"
                "--- a/clusters/prod/x.yaml\n+++ b/clusters/prod/x.yaml\n"
                'diff --git "a/caf\\303\\251/p.yaml" "b/caf\\303\\251/p.yaml"\n'
                '--- "a/caf\\303\\251/p.yaml"\n+++ "b/caf\\303\\251/p.yaml"\n@@ -1 +1 @@\n')
        self.assertEqual(post_merge_check.diff_paths(diff), ["notes.patch", "caf\u00e9/p.yaml"])

    def test_diff_paths_unquotes_escapes_beside_raw_utf8(self):
        diff = 'diff --git "a/caf\u00e9/\\"q\\".yaml" "b/caf\u00e9/\\"q\\".yaml"\n--- "a/caf\u00e9/\\"q\\".yaml"\n'
        self.assertEqual(post_merge_check.diff_paths(diff), ['caf\u00e9/"q".yaml'])
        # Raw Latin-1-range text that happens to spell valid UTF-8 is kept, never decoded a second time.
        mojibake = 'diff --git "a/\u00c3\u00a9/\\"q\\".yaml" "b/\u00c3\u00a9/\\"q\\".yaml"\n--- "a/\u00c3\u00a9/\\"q\\".yaml"\n'
        self.assertEqual(post_merge_check.diff_paths(mojibake), ['\u00c3\u00a9/"q".yaml'])

    def test_diff_paths_with_a_name_keeps_only_the_files_that_name_it(self):
        diff = ("diff --git a/platform/x.yaml b/platform/x.yaml\n--- a/platform/x.yaml\n+++ b/platform/x.yaml\n"
                "@@ -1 +1 @@\n context checkout\n+replicas: 2\n"
                "diff --git a/apps/checkout.yaml b/apps/checkout.yaml\n--- a/apps/checkout.yaml\n+++ b/apps/checkout.yaml\n"
                "@@ -1 +1 @@\n+x: 1\n"
                "diff --git a/apps/pdb.yaml b/apps/pdb.yaml\n--- /dev/null\n+++ b/apps/pdb.yaml\n@@ -0,0 +1 @@\n"
                "+  name: checkout-pdb\n"
                "diff --git a/apps/other.yaml b/apps/other.yaml\n--- a/apps/other.yaml\n+++ b/apps/other.yaml\n"
                "@@ -1 +1 @@\n+app: checkout2\n")
        self.assertEqual(post_merge_check.diff_paths(diff, "checkout"), ["apps/checkout.yaml", "apps/pdb.yaml"])
        self.assertEqual(len(post_merge_check.diff_paths(diff)), 4)

    def test_diff_paths_ends_lines_only_where_git_does(self):
        diff = ("diff --git a/a.yaml b/a.yaml\r\n--- a/a.yaml\r\n+++ b/a.yaml\r\n@@ -1 +1 @@\r\n"
                "+x\rdiff --git a/clusters/prod/p.yaml b/clusters/prod/p.yaml\n+y\u2028diff --git a/c/q b/c/q\n")
        self.assertEqual(post_merge_check.diff_paths(diff), ["a.yaml"])

    def test_an_escape_that_names_no_byte_is_kept_as_text_never_a_crash(self):
        from unittest import mock

        diff = 'diff --git "a/\\400/p.yaml" "b/\\400/p.yaml"\n--- "a/\\400/p.yaml"\n'
        forge = mock.Mock(call=lambda *a, **k: {"diff": diff})
        with mock.patch.dict(sys.modules, {"forge": forge}):
            self.assertEqual(post_merge_check.forge_paths(REPO, 7), ["\\400/p.yaml"])
        self.assertEqual(post_merge_check._unquote("\\\u20acx"), "\\\u20acx")

    def test_diff_paths_reads_renames_and_binary_files(self):
        diff = ("diff --git a/old/pdb.yaml b/clusters/dev/pdb.yaml\nsimilarity index 100%\n"
                "rename from old/pdb.yaml\nrename to clusters/dev/pdb.yaml\n"
                "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n")
        self.assertEqual(post_merge_check.diff_paths(diff), ["old/pdb.yaml", "clusters/dev/pdb.yaml", "logo.png"])

    def test_a_root_sync_with_no_git_section_never_breaks_the_match(self):
        lists = {post_merge_check.CONFIGSYNC_ROOTSYNCS: {"items": [{"kind": "RootSync", "spec": {}}]}}
        self.assertEqual(post_merge_check.reconcilers_for("", "shop", lists, ["a.yaml"]), [])
        self.assertEqual(post_merge_check.reconcilers_for(REPO, "shop", lists, ["a.yaml"]), [])

    def test_the_files_are_read_only_when_they_can_change_the_match(self):
        asked = []

        def paths():
            asked.append(1)
            return ["overlays/dev/pdb.yaml"]

        one = {post_merge_check.ARGO_APPLICATIONS: {"items": [argo_app()]}}
        self.assertEqual(len(post_merge_check.reconcilers_for(REPO, "shop", one, paths)), 1)
        self.assertEqual(asked, [])
        dev, prod = argo_app(), argo_app()
        dev["spec"]["source"]["path"], prod["spec"]["source"]["path"] = "overlays/dev", "overlays/prod"
        two = {post_merge_check.ARGO_APPLICATIONS: {"items": [dev, prod]}}
        self.assertEqual(post_merge_check.reconcilers_for(REPO, "shop", two, paths), [dev])
        self.assertEqual(asked, [1])

    def test_diff_paths_reads_old_and_new_names(self):
        diff = ("diff --git a/clusters/dev/pdb.yaml b/clusters/dev/pdb.yaml\nnew file mode 100644\n"
                "--- /dev/null\n+++ b/clusters/dev/pdb.yaml\n@@ -0,0 +1 @@\n+--- a/not/a/header\n"
                "diff --git a/old.yaml b/new.yaml\n--- a/old.yaml\n+++ b/new.yaml\n")
        self.assertEqual(post_merge_check.diff_paths(diff), ["clusters/dev/pdb.yaml", "old.yaml", "new.yaml"])

    def test_forge_paths_reads_the_diff_and_says_unknown_when_it_cannot(self):
        from unittest import mock

        calls = []

        def answering(answer):
            def call(op, args, repo, **kwargs):
                calls.append((op, args, repo, kwargs))
                if isinstance(answer, Exception):
                    raise answer
                return answer
            return mock.Mock(call=call)

        diff = "diff --git a/clusters/dev/pdb.yaml b/clusters/dev/pdb.yaml\n--- a/clusters/dev/pdb.yaml\n+++ b/clusters/dev/pdb.yaml\n"
        with mock.patch.dict(sys.modules, {"forge": answering({"diff": diff})}):
            self.assertEqual(post_merge_check.forge_paths(REPO, 7), ["clusters/dev/pdb.yaml"])
        self.assertEqual(calls[0][:3], ("proposal-view", {"number": 7, "diff": True}, REPO))
        self.assertTrue(calls[0][3]["retry_transient"])
        cut = {"diff": diff + "# diff cut short: only the first 1 files are shown\n"}
        maybe = {"diff": diff + "# diff may be cut short: GitLab sent page 3 again\n"}
        quoted = diff + "@@ -0,0 +1 @@\n+# diff cut short is just text in this file\n"
        with mock.patch.dict(sys.modules, {"forge": answering({"diff": quoted})}):
            self.assertEqual(post_merge_check.forge_paths(REPO, 7), ["clusters/dev/pdb.yaml"])
        for answer in ({"diff": ""}, {"diff": None}, {}, ["not", "a", "dict"], cut, maybe, RuntimeError("forge down")):
            with self.subTest(answer=answer), mock.patch.dict(sys.modules, {"forge": answering(answer)}):
                if isinstance(answer, Exception):
                    with self.assertLogs("post_merge_check", level="WARNING"):
                        self.assertIsNone(post_merge_check.forge_paths(REPO, 7))
                else:
                    self.assertIsNone(post_merge_check.forge_paths(REPO, 7))

    def test_combined_sync_is_applied_only_when_every_reconciler_is(self):
        state, _ = post_merge_check.combined_sync([argo_app(), argo_app(revision=OLDER, status="OutOfSync")], MERGE)
        self.assertNotEqual(state, "applied")
        self.assertEqual(post_merge_check.combined_sync([argo_app()], MERGE)[0], "applied")
        self.assertEqual(post_merge_check.combined_sync([], MERGE)[0], "unknown")


class MainTest(unittest.TestCase):
    def test_off_reads_nothing_and_on_runs_one_tick(self):
        import io
        import json
        import tempfile
        from contextlib import redirect_stdout
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kv.db")
            with sqlite3.connect(db) as conn:
                conn.execute(schema())
                conn.execute(
                    "INSERT INTO post_merge_records (url, repo, number, kind, opened_at) VALUES (?, ?, 1, 'no-pdb', ?)",
                    (f"{REPO}/pull/1", REPO, NOW.isoformat()),
                )
            forge = FakeForge({1: {"state": "merged", "mergeCommit": "abc"}})
            import kubeagents_switches

            for value, expected in (("off", {"switch": "off"}), ("silent", None)):
                out = io.StringIO()
                with mock.patch.dict("os.environ", {"KUBEAGENTS_POST_MERGE_CHECK": value, "SESSION_KV_DB_PATH": db}), \
                        mock.patch.object(kubeagents_switches, "MANAGED_DOTENV_PATH", Path(tmp) / "no-env"), \
                        mock.patch.object(kubeagents_switches, "SWITCH_DIR", Path(tmp) / "no-dir"), \
                        redirect_stdout(out):
                    self.assertEqual(post_merge_check.main(["notice"], view=forge), 0)
                answer = json.loads(out.getvalue())
                if expected is not None:
                    self.assertEqual(answer, expected)
                    self.assertEqual(forge.reads, [])
                else:
                    self.assertEqual(answer["merged"], [f"{REPO}/pull/1"])
        self.assertEqual(post_merge_check.main(["other"]), 2)

    def test_health_runs_one_tick_under_the_switch(self):
        import io
        import kubeagents_switches
        import tempfile
        from contextlib import redirect_stdout
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kv.db")
            with sqlite3.connect(db) as conn:
                conn.execute(schema())
            out = io.StringIO()
            with mock.patch.dict("os.environ", {"KUBEAGENTS_POST_MERGE_CHECK": "report", "SESSION_KV_DB_PATH": db}), \
                    mock.patch.object(kubeagents_switches, "MANAGED_DOTENV_PATH", Path(tmp) / "no-env"), \
                    mock.patch.object(kubeagents_switches, "SWITCH_DIR", Path(tmp) / "no-dir"), \
                    redirect_stdout(out):
                self.assertEqual(post_merge_check.main(["health"], kubectl_json=FakeCluster()), 0)
            self.assertEqual(json.loads(out.getvalue())["applied"], [])

    def test_the_install_sets_the_reads_per_tick(self):
        from unittest import mock

        for raw, expected in (("", 20), ("5", 5), ("25", 25), ("0", 20), ("-3", 20), ("26", 20), ("500", 20), ("ten", 20),
                              (" 7 ", 7), ("1_0", 20), ("\u0663", 20), ("+5", 20), ("0025", 25),
                              ("9" * 4301, 20)):
            with self.subTest(raw=raw), mock.patch.dict("os.environ", {post_merge_check.READS_PER_TICK_ENV: raw}):
                self.assertEqual(post_merge_check.reads_per_tick(), expected)

    def test_a_tick_reads_no_more_than_the_install_s_limit(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from unittest import mock

        import kubeagents_switches

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kv.db")
            with sqlite3.connect(db) as conn:
                conn.execute(schema())
                for number in range(1, 11):
                    conn.execute("INSERT INTO post_merge_records (url, repo, number, kind, opened_at) "
                                 "VALUES (?, ?, ?, 'no-pdb', ?)", (f"{REPO}/pull/{number}", REPO, number, NOW.isoformat()))
            forge = FakeForge({n: {"state": "open"} for n in range(1, 11)})
            with mock.patch.dict("os.environ", {"KUBEAGENTS_POST_MERGE_CHECK": "silent", "SESSION_KV_DB_PATH": db,
                                                post_merge_check.READS_PER_TICK_ENV: "3"}), \
                    mock.patch.object(kubeagents_switches, "MANAGED_DOTENV_PATH", Path(tmp) / "no-env"), \
                    mock.patch.object(kubeagents_switches, "SWITCH_DIR", Path(tmp) / "no-dir"), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(post_merge_check.main(["notice"], view=forge), 0)
            self.assertEqual(len(forge.reads), 3)


    def test_an_unreadable_scope_reads_no_cluster_and_exits_one(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from unittest import mock

        import kubeagents_switches
        import post_merge_scope

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kv.db")
            with sqlite3.connect(db) as conn:
                conn.execute(schema())
                conn.execute(
                    "INSERT INTO post_merge_records (url, repo, number, kind, project, location, cluster, namespace, "
                    "object, opened_at, merged_at, merge_commit) VALUES (?, ?, 1, 'no-pdb', 'p', 'l', 'prod', 'shop', "
                    "'Deployment/checkout', ?, ?, ?)", (f"{REPO}/pull/1", REPO, NOW.isoformat(), NOW.isoformat(), MERGE))
            cluster, out = FakeCluster(), io.StringIO()
            with mock.patch.dict("os.environ", {"KUBEAGENTS_POST_MERGE_CHECK": "report", "SESSION_KV_DB_PATH": db}), \
                    mock.patch.object(kubeagents_switches, "MANAGED_DOTENV_PATH", Path(tmp) / "no-env"), \
                    mock.patch.object(kubeagents_switches, "SWITCH_DIR", Path(tmp) / "no-dir"), \
                    mock.patch.object(post_merge_scope, "install_scope", side_effect=RuntimeError("render unreadable")), \
                    redirect_stdout(out):
                self.assertEqual(post_merge_check.main(["health"], kubectl_json=cluster), 1)
        self.assertEqual(cluster.calls, [], "an unreadable scope reads no cluster, never every cluster")
        self.assertEqual(json.loads(out.getvalue())["scope"], "unreadable")


if __name__ == "__main__":
    unittest.main()
