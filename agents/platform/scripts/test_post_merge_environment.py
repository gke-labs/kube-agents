"""Tests for post_merge_environment.py: a checked fix's cluster environment, with a fake reader and clock."""

import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kubeagents_switches
import post_merge_environment as pme
from test_post_merge_check import schema

NOW = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)
REPO = "https://github.com/acme/fleet"


class FillTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(schema())
        self.reads = []

    def add(self, number, cluster="p1/us-central1/c1", outcome=pme.VERIFIED_FIXED, age=timedelta(minutes=5), **columns):
        row = {"url": f"{REPO}/pull/{number}", "repo": REPO, "number": number, "kind": "no-pdb",
               "opened_at": (NOW - timedelta(hours=3)).isoformat(), "cluster": cluster, "outcome": outcome,
               "outcome_at": (NOW - age).isoformat(), **columns}
        self.conn.execute(f"INSERT INTO post_merge_records ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                          tuple(row.values()))

    def env(self, number):
        return self.conn.execute("SELECT environment FROM post_merge_records WHERE number = ?", (number,)).fetchone()[0]

    def reader(self, answers):
        def read(record):
            self.reads.append(record["cluster"])
            answer = answers[record["cluster"]]
            if isinstance(answer, Exception):
                raise answer
            return answer
        return read

    def test_checked_records_get_their_clusters_environment(self):
        self.add(1)
        self.add(2, outcome=pme.STILL_BROKEN, cluster="p1/us-central1/c2")
        seen = pme.fill_environments(self.conn, self.reader({"p1/us-central1/c1": "dev", "p1/us-central1/c2": ""}), NOW)
        self.assertEqual(seen["filled"], [f"{REPO}/pull/1", f"{REPO}/pull/2"])
        self.assertEqual((self.env(1), self.env(2)), ("dev", ""))

    def test_only_checked_records_without_an_environment_are_read(self):
        self.add(1, outcome=None)
        self.add(2, outcome="not_applied")
        self.add(3, environment="prod")
        self.add(4, cluster="")
        seen = pme.fill_environments(self.conn, self.reader({}), NOW)
        self.assertEqual((self.reads, sum(map(len, seen.values()))), ([], 0))

    def test_each_cluster_is_read_once_and_the_limit_caps_clusters(self):
        self.add(1, cluster="p1/us-central1/a", age=timedelta(minutes=1))
        self.add(2, cluster="p1/us-central1/a", age=timedelta(minutes=2))
        self.add(3, cluster="p1/us-central1/b", age=timedelta(minutes=3))
        answers = {"p1/us-central1/a": "dev", "p1/us-central1/b": "prod"}
        seen = pme.fill_environments(self.conn, self.reader(answers), NOW, limit=1)
        self.assertEqual((self.reads, seen["filled"]), (["p1/us-central1/a"], [f"{REPO}/pull/1", f"{REPO}/pull/2"]))
        self.assertIsNone(self.env(3))
        pme.fill_environments(self.conn, self.reader(answers), NOW, limit=1)
        self.assertEqual(self.env(3), "prod")

    def test_an_unnamed_cluster_costs_no_read(self):
        self.add(1, cluster="bare", age=timedelta(minutes=1))
        self.add(2, cluster="p1/us-central1/a", age=timedelta(minutes=2))
        seen = pme.fill_environments(self.conn, self.reader({"p1/us-central1/a": "dev"}), NOW, limit=1)
        self.assertEqual((self.reads, seen["unreadable"], seen["filled"]),
                         (["p1/us-central1/a"], [f"{REPO}/pull/1"], [f"{REPO}/pull/2"]))

    def test_a_failed_read_is_retried_then_given_up(self):
        self.add(1)
        boom = {"p1/us-central1/c1": RuntimeError("denied")}
        self.assertEqual(pme.fill_environments(self.conn, self.reader(boom), NOW)["unreadable"], [f"{REPO}/pull/1"])
        self.assertIsNone(self.env(1))
        later = NOW + pme.GIVE_UP_AFTER
        self.assertEqual(pme.fill_environments(self.conn, self.reader(boom), later)["given-up"], [f"{REPO}/pull/1"])
        self.assertEqual(self.env(1), "")

    def test_an_unparseable_outcome_time_is_given_up_at_once(self):
        self.add(1, outcome_at="not a time")
        boom = {"p1/us-central1/c1": RuntimeError("denied")}
        self.assertEqual(pme.fill_environments(self.conn, self.reader(boom), NOW)["given-up"], [f"{REPO}/pull/1"])
        self.assertEqual(self.env(1), "")


class SandboxReadTest(unittest.TestCase):
    def run_with(self, done, record):
        fake = types.ModuleType("stall_watch")
        fake.calls = []
        fake.run_sandbox = lambda argv, **kw: fake.calls.append(argv) or done
        fake.stderr_excerpt = lambda text: text or ""
        with mock.patch.dict(sys.modules, {"stall_watch": fake}):
            return pme.sandbox_environment_of(record), fake.calls

    def test_reads_the_labels_by_the_rules_files_rule(self):
        done = subprocess.CompletedProcess([], 0, json.dumps({"resourceLabels": {"stage": "qa", "team": "x"}}), "")
        env, calls = self.run_with(done, {"cluster": "p1/us-central1-a/c1"})
        self.assertEqual(env, "qa")
        self.assertEqual(calls[0][:5], ["gcloud", "container", "clusters", "describe", "c1"])
        self.assertIn("--project=p1", calls[0])
        self.assertIn("--location=us-central1-a", calls[0])

    def test_a_failed_or_unnamed_read_raises(self):
        with self.assertRaises(pme.post_merge_check.ClusterReadError):
            self.run_with(subprocess.CompletedProcess([], 1, "", "denied"), {"cluster": "p1/us-central1/c1"})
        with self.assertRaises(pme.post_merge_check.ClusterReadError):
            self.run_with(subprocess.CompletedProcess([], 0, "{}", ""), {"cluster": "c1"})


class MainTest(unittest.TestCase):
    def test_off_reads_nothing_and_on_runs_one_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kv.db")
            with sqlite3.connect(db) as conn:
                conn.execute(schema())
                conn.execute("INSERT INTO post_merge_records (url, repo, number, kind, opened_at, cluster, outcome, "
                             "outcome_at) VALUES (?, ?, 1, 'no-pdb', ?, 'p/l/c', 'verified_fixed', ?)",
                             (f"{REPO}/pull/1", REPO, NOW.isoformat(), NOW.isoformat()))
            reads = []
            for value, expected in (("off", {"switch": "off"}), ("silent", None)):
                out = io.StringIO()
                with mock.patch.dict("os.environ", {"KUBEAGENTS_POST_MERGE_CHECK": value, "SESSION_KV_DB_PATH": db}), \
                        mock.patch.object(kubeagents_switches, "MANAGED_DOTENV_PATH", Path(tmp) / "no-env"), \
                        mock.patch.object(kubeagents_switches, "SWITCH_DIR", Path(tmp) / "no-dir"), \
                        redirect_stdout(out):
                    self.assertEqual(pme.main(["fill"], environment_of=lambda r: reads.append(1) or "dev"), 0)
                answer = json.loads(out.getvalue())
                if expected is not None:
                    self.assertEqual((answer, reads), (expected, []))
                else:
                    self.assertEqual((answer["filled"], reads), ([f"{REPO}/pull/1"], [1]))
            self.assertEqual(pme.main(["bogus"]), 2)


if __name__ == "__main__":
    unittest.main()
