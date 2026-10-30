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
import findings_nudge
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
        # The profiles directory sits in the gateway home, where onboarding's markers are.
        self.profiles = Path(self.tmp.name) / "profiles"
        self.profiles.mkdir()
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
        board = self.snap()
        got = board["clusters"]
        self.assertEqual(board["cluster_count"], 3)
        self.assertEqual([c["name"] for c in got], ["seeded-a", "seeded-b", "seeded-c"])
        self.assertEqual(got[0], {"name": "seeded-a", "findings": 2, "severity": "critical", "top": "payments-api crashlooping"})
        self.assertEqual(got[1]["findings"], 1)
        self.assertEqual(got[2], {"name": "seeded-c", "findings": 0})

    def test_the_count_is_the_whole_fleet_past_the_listed_cap(self):
        for i in range(board_snapshot.CLUSTERS_MAX + 5):
            home = self.profiles / f"ca-extra-{i}"
            home.mkdir()
            (home / "config.yaml").write_text(f"cluster_identity:\n  project: p\n  cluster: extra-{i:02d}\n  location: l\n")
        board = self.snap()
        self.assertEqual(len(board["clusters"]), board_snapshot.CLUSTERS_MAX)
        self.assertEqual(board["cluster_count"], board_snapshot.CLUSTERS_MAX + 7)

    def test_a_cluster_name_two_projects_share_is_two_rows(self):
        for project in ("p1", "p2"):
            home = self.profiles / f"ca-{project}-prod"
            home.mkdir()
            (home / "config.yaml").write_text(f"cluster_identity:\n  project: {project}\n  cluster: prod\n  location: l\n")
        f1 = finding("prod", "payments-api crashlooping", "critical")
        f2 = dict(finding("prod", "no PDB on checkout", "minor"), project="p2")
        f1["project"] = "p1"
        findings_queue.register_findings(self.session, [f1, f2])
        board = self.snap()
        names = [c["name"] for c in board["clusters"]]
        self.assertEqual(names[:2], ["p1/prod", "p2/prod"])
        self.assertEqual(board["clusters"][0]["findings"], 1)
        self.assertEqual(board["cluster_count"], 4)
        self.assertIn("seeded-a", names)

    def test_a_closed_finding_is_not_open(self):
        findings_queue.register_findings(self.session, [finding("seeded-a", "gone", "critical")])
        self.session.execute("UPDATE findings SET state = 'resolved'")
        self.assertEqual(self.snap()["clusters"][0], {"name": "seeded-a", "findings": 0})

    def test_before_the_first_report_the_board_says_so(self):
        home = self.profiles.parent
        self.assertNotIn("first_report_pending", self.snap(), "no sweep filed: onboarding never ran")
        (home / findings_nudge.SCAN_FILED_MARKER).write_text(f"task_id=t_1\n{findings_nudge.FILED_AT_PREFIX}{int(NOW.timestamp()) - 60}\n")
        board = self.snap()
        self.assertTrue(board["first_report_pending"])
        self.assertTrue(board["first_message_pending"])
        (home / board_snapshot.FIRST_MESSAGE_MARKER).touch()
        board = self.snap()
        self.assertTrue(board["first_report_pending"])
        self.assertNotIn("first_message_pending", board)

    def test_a_row_before_delivery_keeps_the_other_clusters_pending(self):
        # One cluster's finding landed first; the rest are still unchecked.
        (self.profiles.parent / findings_nudge.SCAN_FILED_MARKER).touch()
        findings_queue.register_findings(self.session, [finding("seeded-a", "oom crash", "critical")])
        board = self.snap()
        self.assertTrue(board["first_report_pending"])
        self.assertEqual([c["findings"] for c in board["clusters"]], [1, 0])

    def test_a_delivered_report_ends_the_pending_state(self):
        home = self.profiles.parent
        (home / findings_nudge.SCAN_FILED_MARKER).touch()
        (home / findings_nudge.DELIVERED_MARKER).touch()
        board = self.snap()
        self.assertNotIn("first_report_pending", board)
        self.assertNotIn("first_message_pending", board)

    def test_a_conversation_claim_is_not_the_report_out(self):
        # Under spec.mode next the claim can be released; the recorded outcome ends the state.
        home = self.profiles.parent
        (home / findings_nudge.SCAN_FILED_MARKER).touch()
        (home / findings_nudge.CONVERSATION_MARKERS[0]).write_text("{}")
        (home / findings_nudge.DELIVERED_MARKER).touch()
        self.assertTrue(self.snap()["first_report_pending"])
        (home / findings_nudge.CONVERSATION_DONE_MARKER).write_text("posted\n")
        self.assertNotIn("first_report_pending", self.snap())

    def test_registered_findings_end_the_pending_state_without_a_delivery(self):
        # A conversation post that failed five times after registration: the fleet was checked.
        home = self.profiles.parent
        (home / findings_nudge.SCAN_FILED_MARKER).touch()
        (home / findings_nudge.CONVERSATION_MARKERS[0]).write_text("{}")
        findings_queue.register_findings(self.session, [finding("seeded-a", "oom crash", "critical")])
        self.assertTrue(self.snap()["first_report_pending"])
        (home / board_snapshot.FINDINGS_REGISTERED_MARKER).touch()
        board = self.snap()
        self.assertNotIn("first_report_pending", board)
        self.assertNotIn("first_message_pending", board)
        self.assertEqual([c["findings"] for c in board["clusters"]], [1, 0])

    def test_a_never_primed_install_stays_pending(self):
        # Nobody wrote within the plugin's week: delivery never ran, so nothing was registered.
        home = self.profiles.parent
        week_ago = int(NOW.timestamp()) - board_snapshot.FIRST_MESSAGE_MAX_AGE_SECONDS - 1
        (home / findings_nudge.SCAN_FILED_MARKER).write_text(f"task_id=t_1\n{findings_nudge.FILED_AT_PREFIX}{week_ago}\n")
        board = self.snap()
        self.assertTrue(board["first_report_pending"])
        self.assertNotIn("first_message_pending", board)

    def test_an_old_sweep_asks_for_no_message(self):
        # Past a week the onboarding plugin primes nothing, so the note would mislead.
        home = self.profiles.parent
        week_ago = int(NOW.timestamp()) - board_snapshot.FIRST_MESSAGE_MAX_AGE_SECONDS - 1
        (home / findings_nudge.SCAN_FILED_MARKER).write_text(f"task_id=t_1\n{findings_nudge.FILED_AT_PREFIX}{week_ago}\n")
        board = self.snap()
        self.assertTrue(board["first_report_pending"])
        self.assertNotIn("first_message_pending", board)

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

    def test_a_malformed_row_is_skipped_not_fatal(self):
        kb = kanban_db()
        kb.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
            [("t1", "Good", "platform", "running", STARTED, STARTED), ("t2", "Bad", "platform", "running", "noon", "noon")],
        )
        good = json.dumps({"url": "https://github.com/a/b/pull/1", "number": 1, "title": "ok"})
        kb.executemany(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            [
                ("t1", "pr_opened", good, STARTED),
                ("t1", "pr_opened", json.dumps({"url": ["x"], "number": 2}), STARTED + 1),
                ("t1", "pr_opened", json.dumps({"url": "https://github.com/a/b/pull/3", "number": "three"}), STARTED + 2),
                ("t1", "pr_opened", json.dumps({"url": "https://github.com/a/b/pull/4", "number": 4}), 1e30),
                ("t1", "pr_opened", json.dumps({"url": "https://github.com/a/b/pull/5", "number": 5}), 1.79e18),
                ("t1", "pr_opened", json.dumps({"url": "https://github.com/a/b/pull/6", "number": 1e30}), STARTED + 3),
                ("t1", "pr_opened", json.dumps({"url": "https://github.com/a/b/pull/7", "number": []}), STARTED + 4),
                ("t1", "pr_opened", json.dumps({"url": "https://github.com/a/b/pull/8", "number": 41.9}), STARTED + 5),
                ("t1", "pr_opened", json.dumps({"url": "https://github.com/a/b/pull/9", "number": "9"}), STARTED + 6),
            ],
        )
        board = self.snap(kb)
        self.assertEqual([c["title"] for c in board["running"]], ["Good"])
        self.assertEqual([p["number"] for p in board["prs"]], [1])

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

    def test_an_audit_run_the_wire_shape_cannot_hold_is_skipped(self):
        def stream(**latest):
            return {"repos": {"a/b": {"latest": dict({"findings": 1, "critical": 0}, **latest)}}}

        projection = {
            "streams": {
                "ring-stamp": stream(finished_at="20261016T102400.000000Z"),
                "zoneless": stream(finished_at="2026-10-17T10:24:00"),
                "string-issue": stream(finished_at="2026-10-18T10:24:00+00:00", issue_number="231x"),
                "good": stream(finished_at="2026-10-15T06:24:00-04:00", issue_number=231),
            }
        }
        got = board_snapshot.last_audit(projection, str)
        # The ring stamp parses with its zone and is re-emitted in RFC 3339; the newer
        # zoneless and non-numeric runs are skipped.
        self.assertEqual((got["name"], got["finished_at"], got["issue"]), ("ring-stamp", "2026-10-16T10:24:00+00:00", 0))

    def test_the_audit_lands_on_the_board(self):
        audit = {"name": "n", "findings": 0, "critical": 0, "issue_url": "", "issue": 0, "finished_at": "2026-10-16T10:24:00+00:00"}
        self.assertEqual(self.snap(audit=lambda: audit)["audit"], audit)


if __name__ == "__main__":
    unittest.main()
