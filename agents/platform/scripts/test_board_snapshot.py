"""Tests for board_snapshot: the Slack Home tab's read of the board, the findings queue and the audit store."""

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_snapshot
import findings_queue

NOW = datetime(2026, 10, 16, 13, 2, tzinfo=timezone.utc)
STARTED = 1792155000.0


RUBRICS = {
    "critical": {"B": 3, "L": 10, "detect": 1, "recover": 2, "C": 1.0},
    "major": {"B": 3, "L": 6, "detect": 2, "recover": 2, "C": 1.0},
    "minor": {"B": 1, "L": 1, "detect": 2, "recover": 1, "C": 1.0},
}


def finding(cluster, title, severity):
    return {
        "source": "inventory",
        "check": "check-" + title.split()[0],
        "project": "p",
        "cluster": cluster,
        "namespace": "ns",
        "object": "Deployment/" + title.split()[0],
        "title": title,
        "rubric": RUBRICS[severity],
        "recommendation": {"action": "a", "rationale": "r", "risk": "k"},
        "remediation": {"kind": "manifest", "path": "x.yaml", "note": "n"},
        "verification": {"kind": "kubectl", "command": "kubectl get x", "still_failing_when": "w"},
    }


def kanban_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                            created_at REAL, started_at REAL);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
                                  kind TEXT, payload TEXT, created_at REAL);
        """
    )
    return conn


class BoardSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profiles = Path(self.tmp.name)
        for profile, cluster in (("platform", None), ("ca-seeded-a", "seeded-a"), ("ca-seeded-c", "seeded-c")):
            home = self.profiles / profile
            home.mkdir()
            body = f"cluster_identity:\n  project: p\n  cluster: {cluster}\n  location: us-central1\n" if cluster else "model: x\n"
            (home / "config.yaml").write_text(body)
        self.session = sqlite3.connect(":memory:")
        findings_queue.init_findings_schema(self.session)

    def tearDown(self):
        self.tmp.cleanup()

    def snap(self, kanban=None, audit=None):
        return board_snapshot.snapshot(self.session, kanban, NOW, self.profiles, audit or (lambda: None))

    def test_clusters_are_the_fleet_with_their_open_findings_worst_first(self):
        findings_queue.register_findings(
            self.session,
            [
                finding("seeded-b", "control plane one minor behind REGULAR", "major"),
                finding("seeded-a", "payments-api crashlooping", "critical"),
                finding("seeded-a", "checkout-gateway has no PDB", "major"),
            ],
        )
        got = self.snap()["clusters"]
        self.assertEqual([c["name"] for c in got], ["seeded-a", "seeded-b", "seeded-c"])
        self.assertEqual(got[0], {"name": "seeded-a", "findings": 2, "severity": "critical", "top": "payments-api crashlooping"})
        self.assertEqual(got[1]["findings"], 1)
        self.assertEqual(got[2], {"name": "seeded-c", "findings": 0})

    def test_a_closed_finding_is_not_open(self):
        findings_queue.register_findings(self.session, [finding("seeded-a", "gone", "critical")])
        self.session.execute("UPDATE findings SET state = 'resolved'")
        self.assertEqual(self.snap()["clusters"][0], {"name": "seeded-a", "findings": 0})

    def test_running_cards_carry_their_assignee_cluster(self):
        kb = kanban_db()
        kb.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("t1", "Diagnose the payments-api crashloop", "ca-seeded-a", "running", STARTED - 60, STARTED),
                ("t2", "Plan the rollout", "platform", "running", STARTED, None),
                ("t3", "Done already", "ca-seeded-a", "done", STARTED, STARTED),
            ],
        )
        got = self.snap(kb)["running"]
        self.assertEqual(
            got,
            [
                {"title": "Diagnose the payments-api crashloop", "started_at": "2026-10-16T12:50:00+00:00", "cluster": "seeded-a"},
                {"title": "Plan the rollout", "started_at": "2026-10-16T12:50:00+00:00"},
            ],
        )

    def test_prs_are_the_newest_pr_opened_events_once_each(self):
        kb = kanban_db()
        url = "https://github.com/acme/fleet-config/pull/41"
        rows = [
            ("t1", "pr_opened", json.dumps({"url": url, "number": 41, "title": "Raise the limit"}), STARTED),
            ("t2", "pr_opened", json.dumps({"url": url, "number": 41, "title": "Raise the limit"}), STARTED - 5),
            ("t2", "completed", "{}", STARTED + 5),
            ("t3", "pr_opened", "not json", STARTED + 9),
        ]
        rows += [("t9", "pr_opened", json.dumps({"url": f"{url}0{i}", "number": i, "title": "x"}), STARTED - 100 - i) for i in range(9)]
        kb.executemany("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)", rows)
        got = self.snap(kb)["prs"]
        self.assertEqual(len(got), board_snapshot.PRS_MAX)
        self.assertEqual(got[0], {"url": url, "number": 41, "title": "Raise the limit", "opened_at": "2026-10-16T12:50:00+00:00"})
        self.assertEqual(len({p["url"] for p in got}), len(got))

    def test_no_kanban_board_is_an_empty_board_not_an_error(self):
        got = self.snap(None)
        self.assertEqual((got["running"], got["prs"]), ([], []))
        self.assertNotIn("audit", got)

    def test_an_unreadable_findings_queue_is_an_error(self):
        self.session.execute("DROP TABLE findings")
        with self.assertRaises(board_snapshot.BoardError):
            self.snap()

    def test_last_audit_is_the_newest_finished_run(self):
        projection = {
            "streams": {
                "compliance-audit": {
                    "repos": {
                        "acme/fleet-config": {
                            "latest": {
                                "finished_at": "2026-10-16T10:24:00+00:00",
                                "findings": 7,
                                "critical": 2,
                                "issue_url": "https://github.com/acme/fleet-config/issues/231",
                                "issue_number": 231,
                            }
                        }
                    }
                },
                "fleet-waste": {"repos": {"acme/fleet-config": {"latest": {"finished_at": "2026-10-15T10:00:00+00:00", "findings": 1}}}},
                "broken": {"repos": {"x/y": {"latest": None, "error": "unreadable"}}},
            }
        }
        names = {"compliance-audit": "Security & RBAC Posture Audit"}
        got = board_snapshot.last_audit(projection, lambda i: names.get(i, i))
        self.assertEqual(
            got,
            {
                "name": "Security & RBAC Posture Audit",
                "findings": 7,
                "critical": 2,
                "issue_url": "https://github.com/acme/fleet-config/issues/231",
                "issue": 231,
                "finished_at": "2026-10-16T10:24:00+00:00",
            },
        )
        self.assertIsNone(board_snapshot.last_audit({"streams": {}}, str))

    def test_the_audit_lands_on_the_board(self):
        audit = {"name": "n", "findings": 0, "critical": 0, "issue_url": "", "issue": 0, "finished_at": "2026-10-16T10:24:00+00:00"}
        self.assertEqual(self.snap(audit=lambda: audit)["audit"], audit)


if __name__ == "__main__":
    unittest.main()
