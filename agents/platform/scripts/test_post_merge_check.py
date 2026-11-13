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


def pods(restarts=(0, 0, 0), ready=True):
    items = []
    for n, count in enumerate(restarts):
        container = {"name": "web", "restartCount": count, "lastState": {}, "state": {"running": {}}}
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
            return {"items": []}
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

    def test_applied_records_the_baseline_and_is_not_read_again(self):
        self.cluster.apps = [argo_app()]
        self.cluster.pod_list = pods(restarts=(2, 0, 0))
        self.assertEqual(self.tick(1)["applied"], [f"{REPO}/pull/1"])
        row = self.row()
        self.assertEqual(row["applied_at"], (NOW + timedelta(minutes=1)).isoformat())
        self.assertEqual(json.loads(row["health_baseline"]), {"u0/web": 2, "u1/web": 0, "u2/web": 0})
        self.assertEqual(self.cluster.selector, "app=checkout")
        # Applied: the apply step does not read it again.
        self.cluster.calls.clear()
        self.tick(10)
        self.assertEqual(self.cluster.calls, [])

    def test_an_unreadable_cluster_is_logged_and_retried(self):
        self.cluster.fail = post_merge_check.ClusterReadError("connection refused")
        with self.assertLogs("post_merge_check", level="WARNING"):
            self.assertEqual(self.tick(1)["unreadable"], [f"{REPO}/pull/1"])
        self.assertTrue(self.row()["sync_state"].startswith("unreadable"))
        self.cluster.fail = None
        self.cluster.apps = [argo_app()]
        self.assertEqual(self.tick(2)["applied"], [f"{REPO}/pull/1"])

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
        self.assertTrue(self.row()["sync_state"].startswith("observed"), self.row()["sync_state"])

    def test_with_no_reconciler_for_the_namespace_the_budget_is_observed_directly(self):
        self.cluster.apps = [argo_app(namespace="elsewhere")]
        self.assertEqual(self.tick(1)["waiting"], [f"{REPO}/pull/1"])
        self.assertIn("no budget selects the workload yet", self.row()["sync_state"])
        self.cluster.budgets = [{"metadata": {"name": "checkout-pdb"}, "spec": {"selector": {"matchLabels": {"app": "checkout"}}}}]
        self.assertEqual(self.tick(2)["applied"], [f"{REPO}/pull/1"])
        self.assertTrue(self.row()["sync_state"].startswith("observed"), self.row()["sync_state"])

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


class SelectorTest(unittest.TestCase):
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
        for repo in (REPO, REPO + ".git", REPO + "/", "git@github.com:acme/fleet.git", "HTTPS://GitHub.com/acme/fleet"):
            with self.subTest(repo=repo):
                lists = {post_merge_check.ARGO_APPLICATIONS: {"items": [argo_app(repo=repo)]}}
                self.assertEqual(len(post_merge_check.reconcilers_for(REPO, "shop", lists)), 1)
        lists = {post_merge_check.ARGO_APPLICATIONS: {"items": [argo_app(repo="https://github.com/acme/other")]}}
        self.assertEqual(post_merge_check.reconcilers_for(REPO, "shop", lists), [])

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

if __name__ == "__main__":
    unittest.main()
