"""Tests for scripts/check_quarantine.py, plus the offline check over the whole tree."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_quarantine as cq  # noqa: E402

NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)

GO_GATED = """package x

func TestFlaky(t *testing.T) {
	if os.Getenv("KA_RUN_QUARANTINED") == "" {
		t.Skip("quarantined: gke-labs/kube-agents#4357")
	}
}
"""

PY_GATED = """import os, unittest

@unittest.skipUnless(os.environ.get("KA_RUN_QUARANTINED"), "quarantined: gke-labs/kube-agents#4357")
class GitLabLaneTest(unittest.TestCase):
    pass
"""

PY_SPLIT = """@unittest.skipUnless(
    os.environ.get("KA_RUN_QUARANTINED"),
    "quarantined: gke-labs/kube-agents#12",
)
"""

GOOD_BODY = "Suspected cause: the fake clock races the poll loop.\nProduct race ruled out: yes, test-only fake.\n"


class FakeAPI:
    repo = cq.REPO

    def __init__(self, issues, comments=None, timelines=None, labelled=()):
        self.issues = issues
        self.comments = comments or {}
        self.timelines = timelines or {}
        self.labelled = list(labelled)

    def get(self, path):
        return self.issues[int(path.rsplit("/", 1)[1])]

    def get_all(self, path):
        if path.endswith("/comments"):
            return self.comments.get(int(path.split("/")[-2]), [])
        if path.endswith("/timeline"):
            return self.timelines.get(int(path.split("/")[-2]), [])
        return self.labelled


def issue(number, state="open", labels=(cq.LABEL,), assignees=("someone",), body=GOOD_BODY):
    return {
        "number": number,
        "state": state,
        "labels": [{"name": name} for name in labels],
        "assignees": [{"login": login} for login in assignees],
        "body": body,
    }


def labelled_event(days_ago):
    return {
        "event": "labeled",
        "label": {"name": cq.LABEL},
        "created_at": (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z"),
    }


def scanned(*files):
    report = cq.Report()
    for path, text in files:
        cq.scan_text(path, text, report)
    return report


class ScanTest(unittest.TestCase):
    def test_gated_go_and_python_quarantines_are_recorded(self):
        report = scanned(("a_test.go", GO_GATED), ("test_b.py", PY_GATED), ("test_c.py", PY_SPLIT))
        self.assertEqual([], report.failures)
        self.assertEqual(
            [("a_test.go", 5, 4357), ("test_b.py", 3, 4357), ("test_c.py", 3, 12)],
            [(q.path, q.line, q.issue) for q in report.quarantines],
        )

    def test_a_skip_without_the_override_fails(self):
        report = scanned(("a_test.go", '\tt.Skip("quarantined: gke-labs/kube-agents#9")\n'))
        self.assertEqual(1, len(report.failures))
        self.assertIn("KA_RUN_QUARANTINED", report.failures[0])

    def test_a_malformed_citation_fails(self):
        for text in ('t.Skip("quarantined: #9")', 't.Skip("quarantined: kube-agents#9")'):
            with self.subTest(text=text):
                report = scanned(("a_test.go", f'if os.Getenv("KA_RUN_QUARANTINED") == "" {{ {text} }}'))
                self.assertEqual([], report.quarantines)
                self.assertEqual(1, len(report.failures))
                self.assertIn("malformed", report.failures[0])

    def test_the_override_more_than_the_window_above_does_not_count(self):
        text = 'os.Getenv("KA_RUN_QUARANTINED")\n\n\n\tt.Skip("quarantined: gke-labs/kube-agents#9")\n'
        self.assertEqual(1, len(scanned(("a_test.go", text)).failures))


class TheTreeTest(unittest.TestCase):
    def test_every_quarantine_in_the_tree_is_well_formed_and_gated(self):
        report = cq.scan_tree()
        self.assertEqual([], report.failures)


class GitHubTest(unittest.TestCase):
    def run_check(self, api, files=(("test_b.py", PY_GATED),)):
        return cq.check_github(api, scanned(*files), NOW)

    def test_a_complete_issue_passes_and_reports_its_age(self):
        api = FakeAPI({4357: issue(4357)}, timelines={4357: [labelled_event(3)]}, labelled=[issue(4357)])
        report = self.run_check(api)
        self.assertEqual(([], []), (report.failures, report.warnings))
        self.assertEqual({4357: 3}, report.ages)
        self.assertIn("oldest 3 days; 0 over 14 days", cq.render(report, True))

    def test_a_closed_issue_fails(self):
        report = self.run_check(FakeAPI({4357: issue(4357, state="closed")}))
        self.assertEqual(1, len(report.failures))
        self.assertIn("closed", report.failures[0])

    def test_missing_label_owner_cause_and_race_each_fail(self):
        bare = issue(4357, labels=(), assignees=(), body="")
        report = self.run_check(FakeAPI({4357: bare}))
        joined = "\n".join(report.failures)
        for needle in (cq.LABEL, "assignee", "Suspected cause", "Product race ruled out"):
            self.assertIn(needle, joined)
        self.assertEqual(4, len(report.failures))

    def test_a_race_not_ruled_out_fails(self):
        body = "Suspected cause: unknown timing.\nProduct race ruled out: no\n"
        report = self.run_check(FakeAPI({4357: issue(4357, body=body)}))
        self.assertEqual(1, len(report.failures))
        self.assertIn("stays live", report.failures[0])

    def test_cause_and_race_may_arrive_in_a_comment(self):
        api = FakeAPI({4357: issue(4357, body="bot-filed table")}, comments={4357: [{"body": GOOD_BODY}]})
        self.assertEqual([], self.run_check(api).failures)

    def test_a_quarantine_past_the_limit_warns(self):
        api = FakeAPI({4357: issue(4357)}, timelines={4357: [labelled_event(cq.STALE_DAYS + 1)]})
        report = self.run_check(api)
        self.assertEqual([], report.failures)
        self.assertEqual(1, len(report.warnings))
        self.assertIn("15 days", report.warnings[0])

    def test_a_label_with_no_skip_in_the_tree_warns(self):
        api = FakeAPI({}, labelled=[issue(77)])
        report = self.run_check(api, files=())
        self.assertEqual(1, len(report.warnings))
        self.assertIn("#77", report.warnings[0])

    def test_one_issue_quarantining_two_tests_is_read_once(self):
        api = FakeAPI({4357: issue(4357)})
        report = self.run_check(api, files=(("test_b.py", PY_GATED), ("a_test.go", GO_GATED)))
        self.assertEqual([], report.failures)
        self.assertEqual(2, len(report.quarantines))


if __name__ == "__main__":
    unittest.main()
