"""Unit tests for slack_audit_report — the fleet-audit headline for Slack.

Run: python3 -m pytest agents/platform/scripts/test_slack_audit_report.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_audit_report as sar

LEDGER = "https://github.com/acme/fleet-config/issues/231"

REPORT = f"""## [audit] Security & RBAC Posture Audit — 7 findings (2 critical)

**New since last run:** 2 · **Resolved:** 1

- **[minor] seeded-a** — a namespace has no NetworkPolicy
- **[critical] seeded-b, seeded-c** — `cluster-admin` bound to the default service account
- **[major] seeded-a** — Workload Identity is off on one node pool
- **[critical] seeded-c** — a ClusterRole grants `*` on secrets

Ledger: {LEDGER}
"""


class HeadlineTest(unittest.TestCase):
    def test_mock_08_layout(self):
        self.assertEqual(
            sar.headline_message(REPORT),
            "**Security & RBAC Posture audit: 7 findings, 2 critical.** 2 are new since the last run.\n"
            ":red_circle: **critical**  seeded-b, seeded-c — `cluster-admin` bound to the default service account\n"
            ":red_circle: **critical**  seeded-c — a ClusterRole grants `*` on secrets\n"
            f"[Ledger issue #231 ↗]({LEDGER})\n"
            "all 7 findings: in the thread",
        )

    def test_rows_are_ranked_by_severity_then_order(self):
        report = REPORT.replace("[critical] seeded-c** — a ClusterRole", "[minor] seeded-c** — a ClusterRole")
        rows = sar.headline_message(report).splitlines()[1:3]
        self.assertTrue(rows[0].startswith(":red_circle: **critical**  seeded-b"))
        self.assertTrue(rows[1].startswith(":large_yellow_circle: **major**  seeded-a"))

    def test_no_new_count_leaves_the_headline_bare(self):
        report = REPORT.replace("**New since last run:** 2 · ", "")
        first = sar.headline_message(report).splitlines()[0]
        self.assertEqual(first, "**Security & RBAC Posture audit: 7 findings, 2 critical.**")

    def test_zero_new_is_not_said(self):
        report = REPORT.replace("**New since last run:** 2", "**New since last run:** 0")
        self.assertNotIn("new since", sar.headline_message(report).splitlines()[0])

    def test_one_new_is_singular(self):
        report = REPORT.replace("**New since last run:** 2", "**New since last run:** 1")
        self.assertIn(" 1 is new since the last run.", sar.headline_message(report))

    def test_singular_title(self):
        report = f"[audit] Cost Audit — 1 finding (0 critical)\n- [major] idle node pool\n{LEDGER}"
        self.assertEqual(
            sar.headline_message(report).splitlines()[0], "**Cost audit: 1 finding, 0 critical.**"
        )

    def test_clean_run_is_one_line(self):
        report = f"[audit] Security & RBAC Posture Audit — 0 findings (0 critical)\nClosed: {LEDGER}"
        self.assertEqual(
            sar.headline_message(report),
            f"Security & RBAC Posture audit: clean. [Ledger closed ↗]({LEDGER})",
        )

    def test_long_rows_are_clipped(self):
        report = REPORT.replace("bound to the default service account", "word " * 60)
        row = sar.headline_message(report).splitlines()[1]
        self.assertTrue(row.endswith("…"))
        self.assertLessEqual(len(row), len(":red_circle: **critical**  ") + sar.ROW_TEXT_MAX)

    def test_a_retyped_dash_still_matches(self):
        self.assertIsNotNone(sar.headline_message(REPORT.replace("—", "-")))


class NotAnAuditTest(unittest.TestCase):
    def test_no_title(self):
        self.assertIsNone(sar.headline_message(f"All clusters healthy. {LEDGER}"))

    def test_no_ledger_url(self):
        self.assertIsNone(sar.headline_message(REPORT.replace(LEDGER, "the ledger")))

    def test_coverage_incomplete_title(self):
        report = f"[audit] Cost Audit — coverage incomplete (2 gaps, 0 findings)\n{LEDGER}"
        self.assertIsNone(sar.headline_message(report))

    def test_pull_request_url_is_not_the_ledger(self):
        report = REPORT.replace("/issues/231", "/pull/231")
        self.assertIsNone(sar.headline_message(report))


if __name__ == "__main__":
    unittest.main()
