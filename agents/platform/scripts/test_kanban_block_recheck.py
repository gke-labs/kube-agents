"""Unit tests for kanban_block_recheck.py: the re-check of a blocked card's condition.

Run: python3 -m unittest agents/platform/scripts/test_kanban_block_recheck.py

The board is a plain sqlite file with the columns the module reads; Hermes'
own writers (``unblock_task``, ``_append_event``) and the sandbox ``kubectl``
read are injected, so every test runs on the host.
"""

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import kanban_block_recheck as kbr  # noqa: E402

NOW = 1_800_000_000
HOUR = 3600

SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT NOT NULL,
    block_kind TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT,
    created_at INTEGER
);
"""

K8S = {
    "kind": kbr.CONDITION_K8S, "project": "p", "location": "us-central1", "cluster": "c",
    "namespace": "shop", "resource": "deployment/web",
}


def deployment(available: bool) -> dict:
    return {"kind": "Deployment", "status": {"conditions": [
        {"type": "Progressing", "status": "True"},
        {"type": "Available", "status": "True" if available else "False"},
    ]}}


def pod(ready: bool) -> dict:
    return {"kind": "Pod", "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


class Board(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = sqlite3.connect(str(Path(self.tmp) / "kanban.db"))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.addCleanup(self.conn.close)
        self.unblocked, self.events = [], []

    def card(self, tid, status="blocked", *, kind=None, title="Investigate web", event=None, at=NOW - 2 * HOUR):
        """A card, blocked by ``event``: a ``blocked`` event of ``kind``, or ``gave_up``."""
        self.conn.execute("INSERT INTO tasks (id, title, status, block_kind) VALUES (?, ?, ?, ?)",
                          (tid, title, status, kind))
        if event == "gave_up":
            self.event(tid, "gave_up", {"failures": 2, "error": "crashed"}, at)
        elif event == "blocked":
            self.event(tid, "blocked", {"reason": "429 from the provider", "kind": kind}, at)
        self.conn.commit()
        return tid

    def event(self, tid, kind, payload=None, at=NOW):
        self.conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                          (tid, kind, json.dumps(payload or {}), at))
        self.conn.commit()

    def record(self, tid):
        row = self.conn.execute(f"SELECT * FROM {kbr.TABLE} WHERE task_id = ?", (tid,)).fetchone()
        return dict(row) if row else None

    def run_recheck(self, now=NOW, kube=None, unblock_ok=True):
        def unblock(conn, tid):
            self.unblocked.append(tid)
            if unblock_ok:  # as Hermes' unblock_task does: the card leaves ``blocked``
                conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
                conn.commit()
            return unblock_ok

        def append_event(conn, tid, payload):
            self.events.append((tid, payload))

        return kbr.recheck(self.conn, now=now, kube=kube or (lambda condition: deployment(True)),
                           unblock=unblock, append_event=append_event)


class DeriveRecordsTests(Board):
    """The harness's own blocks get a time condition; a person's question does not."""

    def test_a_transient_block_retries_half_an_hour_after_the_block(self):
        self.card("t_1", kind="transient", event="blocked", at=NOW - 10)
        self.assertEqual(kbr.derive_records(self.conn, now=NOW), ["t_1"])
        rec = self.record("t_1")
        self.assertEqual(rec["kind"], "transient")
        self.assertEqual(json.loads(rec["condition"]), {"kind": kbr.CONDITION_TIME})
        self.assertEqual(rec["check_after"], NOW - 10 + kbr.RETRY_AFTER_SECONDS["transient"])
        self.assertEqual(rec["expires_at"], rec["check_after"])

    def test_a_breaker_trip_retries_an_hour_after_it(self):
        self.card("t_2", event="gave_up", at=NOW - 10)
        kbr.derive_records(self.conn, now=NOW)
        rec = self.record("t_2")
        self.assertEqual(rec["kind"], kbr.GAVE_UP_KIND)
        self.assertEqual(rec["check_after"], NOW - 10 + kbr.RETRY_AFTER_SECONDS[kbr.GAVE_UP_KIND])

    def test_blocks_that_wait_on_a_person_get_no_record(self):
        for tid, kind in (("t_n", "needs_input"), ("t_c", "capability"), ("t_u", None)):
            self.card(tid, kind=kind, event="blocked")
        self.card("t_none", kind="transient")  # blocked by a direct edit: no event at all
        self.card("t_done", status="done", kind="transient", event="blocked")
        self.assertEqual(kbr.derive_records(self.conn, now=NOW), [])
        self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {kbr.TABLE}").fetchone()[0], 0)

    def test_the_newest_block_event_decides(self):
        self.card("t_3", kind="transient", event="blocked", at=NOW - 5 * HOUR)
        self.event("t_3", "unblocked", {}, NOW - 4 * HOUR)
        self.event("t_3", "blocked", {"reason": "a question", "kind": "needs_input"}, NOW - 3 * HOUR)
        self.assertEqual(kbr.derive_records(self.conn, now=NOW), [])

    def test_a_record_for_an_older_block_is_replaced(self):
        self.card("t_4", kind="transient", event="blocked", at=NOW - 5 * HOUR)
        kbr.derive_records(self.conn, now=NOW)
        old = self.record("t_4")
        self.event("t_4", "unblocked", {}, NOW - 4 * HOUR)
        self.event("t_4", "gave_up", {"failures": 2}, NOW - HOUR)
        self.assertEqual(kbr.derive_records(self.conn, now=NOW), ["t_4"])
        new = self.record("t_4")
        self.assertNotEqual(new["block_event_id"], old["block_event_id"])
        self.assertEqual(new["kind"], kbr.GAVE_UP_KIND)

    def test_a_record_the_card_has_left_behind_is_dropped(self):
        self.card("t_5", kind="transient", event="blocked")
        kbr.derive_records(self.conn, now=NOW)
        self.conn.execute("UPDATE tasks SET status = 'done' WHERE id = 't_5'")
        self.conn.commit()
        kbr.derive_records(self.conn, now=NOW)
        self.assertIsNone(self.record("t_5"))


class RecordConditionTests(Board):
    def test_a_kubernetes_condition_is_recorded_with_a_default_expiry(self):
        self.card("t_k", kind="needs_input", event="blocked")
        rec = kbr.record_condition(self.conn, "t_k", K8S, now=NOW)
        self.assertEqual(json.loads(rec["condition"]), K8S)
        self.assertEqual(rec["check_after"], NOW)
        self.assertEqual(rec["expires_at"], NOW + kbr.DEFAULT_K8S_EXPIRY_SECONDS)
        self.assertEqual(rec["kind"], "needs_input")

    def test_an_unknown_resource_kind_or_a_missing_field_is_refused(self):
        self.card("t_k", kind="needs_input", event="blocked")
        with self.assertRaises(ValueError):
            kbr.record_condition(self.conn, "t_k", {**K8S, "resource": "configmap/x"}, now=NOW)
        with self.assertRaises(ValueError):
            kbr.record_condition(self.conn, "t_k", {k: v for k, v in K8S.items() if k != "namespace"}, now=NOW)
        with self.assertRaises(ValueError):
            kbr.record_condition(self.conn, "t_k", {**K8S, "resource": "deployment"}, now=NOW)

    def test_only_a_blocked_card_takes_a_condition(self):
        self.card("t_r", status="ready")
        with self.assertRaises(ValueError):
            kbr.record_condition(self.conn, "t_r", K8S, now=NOW)


class PredicateTests(unittest.TestCase):
    def test_a_deployment_clears_when_available(self):
        self.assertEqual(kbr.predicate_holds("deployment/web", deployment(True)), (True, "Available"))
        self.assertEqual(kbr.predicate_holds("deployment/web", deployment(False))[0], False)

    def test_a_pod_clears_when_ready(self):
        self.assertEqual(kbr.predicate_holds("pod/web-1", pod(True))[0], True)
        self.assertEqual(kbr.predicate_holds("pod/web-1", pod(False))[0], False)
        self.assertEqual(kbr.predicate_holds("pod/web-1", {"status": {}})[0], False)

    def test_an_object_that_is_gone_does_not_clear(self):
        held, detail = kbr.predicate_holds("pod/web-1", kbr.NOT_FOUND)
        self.assertFalse(held)
        self.assertIn("not found", detail)


class RecheckTests(Board):
    def test_nothing_is_due_before_check_after(self):
        self.card("t_1", kind="transient", event="blocked", at=NOW)
        self.assertEqual(self.run_recheck(now=NOW + 60), [])
        self.assertEqual(self.unblocked, [])

    def test_a_lapsed_time_condition_retries_the_card_once(self):
        self.card("t_1", kind="transient", event="blocked", at=NOW - HOUR)
        lines = self.run_recheck()
        self.assertEqual(self.unblocked, ["t_1"])
        self.assertEqual(self.events[0][0], "t_1")
        self.assertEqual(self.events[0][1]["outcome"], kbr.OUTCOME_RETRIED)
        self.assertIsNone(self.record("t_1"))
        self.assertEqual(len(lines), 1)
        self.assertIn("Investigate web", lines[0])
        self.assertNotIn("t_1", lines[0])

    def test_a_card_already_retried_automatically_waits_for_someone(self):
        self.card("t_1", kind="transient", event="blocked", at=NOW - 5 * HOUR)
        self.event("t_1", kbr.EVENT_KIND, {"outcome": kbr.OUTCOME_RETRIED}, NOW - 4 * HOUR)
        self.event("t_1", "unblocked", {}, NOW - 4 * HOUR)
        self.event("t_1", "gave_up", {"failures": 2}, NOW - 2 * HOUR)
        self.assertEqual(self.run_recheck(), [])
        self.assertEqual(self.unblocked, [])
        rec = self.record("t_1")
        self.assertEqual(rec["last_outcome"], kbr.OUTCOME_RETRY_LIMIT)
        self.assertEqual(rec["last_checked_at"], NOW)
        # Said once, then left alone.
        self.assertEqual(self.run_recheck(now=NOW + HOUR), [])
        self.assertEqual(self.record("t_1")["last_checked_at"], NOW)

    def test_retries_per_run_are_capped(self):
        for i in range(kbr.MAX_RETRIES_PER_RUN + 2):
            self.card(f"t_{i}", event="gave_up", at=NOW - 3 * HOUR)
        self.run_recheck()
        self.assertEqual(len(self.unblocked), kbr.MAX_RETRIES_PER_RUN)
        self.run_recheck(now=NOW + 60)
        self.assertEqual(len(self.unblocked), kbr.MAX_RETRIES_PER_RUN + 2)

    def test_a_card_that_moved_on_loses_its_record_without_an_event(self):
        self.card("t_1", kind="transient", event="blocked", at=NOW - HOUR)
        self.run_recheck(unblock_ok=False)
        self.assertEqual(self.events, [])
        self.assertIsNone(self.record("t_1"))

    def test_a_kubernetes_condition_that_holds_refreshes_the_record(self):
        self.card("t_k", kind="needs_input", event="blocked")
        kbr.record_condition(self.conn, "t_k", K8S, now=NOW - HOUR)
        seen = []

        def kube(condition):
            seen.append(condition)
            return deployment(False)

        self.assertEqual(self.run_recheck(kube=kube), [])
        self.assertEqual(seen, [K8S])
        self.assertEqual(self.unblocked, [])
        rec = self.record("t_k")
        self.assertEqual((rec["checks"], rec["last_checked_at"], rec["last_outcome"]), (1, NOW, kbr.OUTCOME_STILL_BLOCKED))
        self.assertEqual(self.events, [])

    def test_a_kubernetes_condition_that_cleared_retries_the_card(self):
        self.card("t_k", kind="needs_input", event="blocked", title="Roll out web")
        kbr.record_condition(self.conn, "t_k", K8S, now=NOW - HOUR)
        lines = self.run_recheck(kube=lambda condition: deployment(True))
        self.assertEqual(self.unblocked, ["t_k"])
        payload = self.events[0][1]
        self.assertEqual(payload["outcome"], kbr.OUTCOME_RETRIED)
        self.assertEqual(payload["condition"], K8S)
        self.assertIn("deployment/web", lines[0])
        self.assertIn("shop", lines[0])

    def test_a_cluster_that_cannot_be_read_is_recorded_not_retried(self):
        self.card("t_k", kind="needs_input", event="blocked")
        kbr.record_condition(self.conn, "t_k", K8S, now=NOW - HOUR)

        def kube(condition):
            raise RuntimeError("get-credentials exited 1")

        self.assertEqual(self.run_recheck(kube=kube), [])
        rec = self.record("t_k")
        self.assertEqual(rec["last_outcome"], kbr.OUTCOME_UNKNOWN)
        self.assertIn("get-credentials", rec["last_detail"])
        self.assertEqual(self.unblocked, [])

    def test_a_kubernetes_condition_stops_at_its_expiry(self):
        self.card("t_k", kind="needs_input", event="blocked")
        kbr.record_condition(self.conn, "t_k", K8S, now=NOW - 3 * HOUR, expires_at=NOW - HOUR)
        calls = []
        self.run_recheck(kube=lambda c: calls.append(c) or deployment(True))
        self.assertEqual(calls, [])
        self.assertEqual(self.unblocked, [])
        rec = self.record("t_k")
        self.assertEqual(rec["last_outcome"], kbr.OUTCOME_EXPIRED)
        self.assertEqual(self.events[0][1]["outcome"], kbr.OUTCOME_EXPIRED)
        self.run_recheck(now=NOW + HOUR, kube=lambda c: calls.append(c) or deployment(True))
        self.assertEqual(len(self.events), 1)

    def test_the_sandbox_read_is_the_cluster_s_kubectl_get(self):
        """The default ``kube``: one credential fetch and one ``kubectl get -o json``."""
        runs = []

        class Stall:
            @staticmethod
            def fetch_credentials(project, cluster, location):
                runs.append(("creds", project, cluster, location))
                return "/home/hermes/.kubeconfigs/x.yaml"

            @staticmethod
            def run_sandbox(argv, *, timeout, kubeconfig=None, stdin=None):
                runs.append(("run", tuple(argv), kubeconfig))
                return type("P", (), {"returncode": 0, "stdout": json.dumps(deployment(True)), "stderr": ""})()

        self.assertEqual(kbr.kube_get(K8S, stall=Stall), deployment(True))
        self.assertEqual(runs[0], ("creds", "p", "c", "us-central1"))
        self.assertEqual(runs[1], ("run", ("kubectl", "get", "deployment/web", "-n", "shop", "-o", "json"),
                                   "/home/hermes/.kubeconfigs/x.yaml"))

    def test_a_not_found_read_is_the_sentinel_and_another_failure_raises(self):
        class Stall:
            code, err = 1, 'Error from server (NotFound): deployments.apps "web" not found'

            @staticmethod
            def fetch_credentials(project, cluster, location):
                return "k"

            @classmethod
            def run_sandbox(cls, argv, *, timeout, kubeconfig=None, stdin=None):
                return type("P", (), {"returncode": cls.code, "stdout": "", "stderr": cls.err})()

        self.assertIs(kbr.kube_get(K8S, stall=Stall), kbr.NOT_FOUND)
        Stall.err = "Unable to connect to the server: dial tcp: i/o timeout"
        with self.assertRaises(RuntimeError):
            kbr.kube_get(K8S, stall=Stall)


class MainTests(Board):
    def test_a_quiet_run_prints_nothing(self):
        self.card("t_1", kind="needs_input", event="blocked")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = kbr.main([], conn=self.conn, now=NOW, kube=lambda c: deployment(True),
                            unblock=lambda conn, tid: True, append_event=lambda conn, tid, payload: None)
        self.assertEqual((code, out.getvalue()), (0, ""))

    def test_a_retry_is_said_in_one_line(self):
        self.card("t_1", event="gave_up", at=NOW - 2 * HOUR, title="Check the ingress")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kbr.main([], conn=self.conn, now=NOW, kube=lambda c: deployment(True),
                     unblock=lambda conn, tid: True, append_event=lambda conn, tid, payload: None)
        self.assertEqual(out.getvalue().count("\n"), 1)
        self.assertIn("Check the ingress", out.getvalue())

    def test_watch_records_a_kubernetes_condition(self):
        self.card("t_k", kind="needs_input", event="blocked")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = kbr.main(["--watch", "t_k", "--project", "p", "--location", "us-central1", "--cluster", "c",
                             "--namespace", "shop", "--resource", "deployment/web", "--expires-in-hours", "2"],
                            conn=self.conn, now=NOW)
        self.assertEqual(code, 0)
        rec = self.record("t_k")
        self.assertEqual(json.loads(rec["condition"]), K8S)
        self.assertEqual(rec["expires_at"], NOW + 2 * HOUR)
        self.assertIn("t_k", out.getvalue())


if __name__ == "__main__":
    unittest.main()
