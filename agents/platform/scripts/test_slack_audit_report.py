"""Unit tests for slack_audit_report — the fleet-audit headline for Slack.

Run: python3 -m pytest agents/platform/scripts/test_slack_audit_report.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_audit_report as sar

LEDGER = "https://github.com/acme/fleet-config/issues/231"
REF = sar.LedgerRef(LEDGER, "acme/fleet-config", 231)

#: The SOP's relayed line (compliance_audit_sop.md), with its trailing ledger URL.
REPORT = f"Security & RBAC posture audit: 2 new, 1 resolved across 3 clusters — {LEDGER}"


def finding(title, fid):
    return f'<a id="finding-{fid}"></a>\n\n#### {title} <!-- finding:{fid} -->\n\n- **Where:** `seeded-a` — `x`\n'


BODY = (
    "Summary line.\n\n"
    "### Critical (2)\n\n"
    + finding("seeded-b, seeded-c: `cluster-admin` bound to the default service account", "rbac-1")
    + finding("seeded-c: a ClusterRole grants `*` on secrets", "rbac-2")
    + "\n### Major (1)\n\n"
    + finding("seeded-a: Workload Identity is off on one node pool", "wi-1")
    + "\n### Minor (4)\n\n"
    + finding("seeded-a: a namespace has no NetworkPolicy", "np-1")
    + "\n### Skipped\n\n"
    + finding("not a finding", "skip-1")
)
ISSUE = {
    "title": "[audit] Security & RBAC Posture Audit — 7 findings (2 critical)",
    "body": BODY,
    "state": "open",
    "labels": ["agent:audit", "severity:critical"],
}
LINE = "Security & RBAC posture audit: 2 new, 1 resolved across 3 clusters"


class LedgerRefTest(unittest.TestCase):
    def test_the_sop_line_names_its_ledger(self):
        self.assertEqual(sar.ledger_ref(REPORT), REF)

    def test_every_sop_shape(self):
        for line in (
            (
                f"AI Workload Security Audit: 1 critical, 3 major, 2 minor across 2 of 7 clusters "
                f"(2 new, 1 resolved, 1 remediation PR opened) — {LEDGER}"
            ),
            f"Upgrade & patch readiness: 3 new findings (1 critical), 2 resolved, across 11 clusters — {LEDGER}",
            f"Workload Reliability Audit: 2 critical — {LEDGER}.",
            f"Cost audit: 2 new – <{LEDGER}>",
            f"Cost audit: 2 new — [#231]({LEDGER})",
            f"Cost audit: 2 new - {LEDGER}\n",
            f"## Security audit\n\n- a finding\n\nLedger: {LEDGER}\n",
        ):
            with self.subTest(line=line):
                self.assertEqual(sar.ledger_ref(line), REF)

    def test_repro_b_a_finding_url_is_not_the_ledger(self):
        report = (
            "- **[critical] seeded-a** — affected by https://github.com/kubernetes/kubernetes/issues/124000\n"
            f"Ledger: {LEDGER}\n"
        )
        self.assertEqual(sar.ledger_ref(report), REF)

    def test_a_url_that_does_not_end_the_report_is_not_the_ledger(self):
        self.assertIsNone(sar.ledger_ref(f"See {LEDGER} for the details of this."))

    def test_a_url_without_a_dash_or_label_is_not_the_ledger(self):
        self.assertIsNone(sar.ledger_ref(f"affected by {LEDGER}"))

    def test_a_pull_request_is_not_the_ledger(self):
        self.assertIsNone(sar.ledger_ref(REPORT.replace("/issues/", "/pull/")))


class HeadlineFromIssueTest(unittest.TestCase):
    def test_mock_08_layout(self):
        self.assertEqual(
            sar.headline_from_issue(ISSUE, REF, REPORT),
            "**Security & RBAC Posture audit: 7 findings, 2 critical.** 2 are new since the last run.\n"
            f"{LINE}\n"
            ":red_circle: **critical**  seeded-b, seeded-c: `cluster-admin` bound to the default service account\n"
            ":red_circle: **critical**  seeded-c: a ClusterRole grants `*` on secrets\n"
            f"[Ledger issue #231 ↗]({LEDGER}): all 7 findings",
        )

    def test_rows_follow_the_severity_sections(self):
        body = BODY.replace("### Critical (2)", "### Nothing").replace(
            "### Minor (4)", "### Minor (4)\n\n" + finding("minor one", "m-0")
        )
        rows = sar.headline_from_issue(dict(ISSUE, body=body), REF, REPORT).splitlines()[2:4]
        self.assertEqual(rows[0], ":large_yellow_circle: **major**  seeded-a: Workload Identity is off on one node pool")
        self.assertTrue(rows[1].startswith(":white_circle: **minor**  minor one"))

    def test_no_new_count_leaves_the_headline_bare(self):
        first = sar.headline_from_issue(ISSUE, REF, "").splitlines()[0]
        self.assertEqual(first, "**Security & RBAC Posture audit: 7 findings, 2 critical.**")

    def test_one_new_is_singular(self):
        self.assertIn(" 1 is new since the last run.", sar.headline_from_issue(ISSUE, REF, "1 new — x"))

    def test_singular_title(self):
        issue = dict(ISSUE, title="[audit] Cost Audit — 1 finding (0 critical)", body="### Major (1)\n\n" + finding("idle", "c"))
        self.assertEqual(sar.headline_from_issue(issue, REF).splitlines()[0], "**Cost audit: 1 finding, 0 critical.**")

    def test_a_closed_ledger_does_not_parse(self):
        # A clean run closes the ledger over its old title: 7 findings is the last run's count.
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, state="closed"), REF, REPORT))
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, state=""), REF, REPORT))

    def test_an_issue_without_the_ledger_label_does_not_parse(self):
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, labels=["bug"]), REF, REPORT))
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, labels=None), REF, REPORT))

    def test_the_state_is_read_in_any_case(self):
        self.assertIsNotNone(sar.headline_from_issue(dict(ISSUE, state="OPEN"), REF, REPORT))

    def test_a_zero_finding_title_does_not_parse(self):
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 0 findings (0 critical)", body="All clear.")
        self.assertIsNone(sar.headline_from_issue(issue, REF, REPORT))

    def test_the_relayed_line_keeps_coverage_resolved_and_prs(self):
        line = (
            "AI Workload Security Audit: 1 critical, 3 major, 2 minor across 2 of 7 clusters "
            "(2 new, 1 resolved, 1 remediation PR opened)"
        )
        lines = sar.headline_from_issue(ISSUE, REF, f"Here's the audit.\n{line} — {LEDGER}").splitlines()
        self.assertEqual(lines[1], line)
        self.assertNotIn(LEDGER, lines[1])

    def test_a_report_that_is_only_the_link_adds_no_line(self):
        lines = sar.headline_from_issue(ISSUE, REF, f"Ledger: {LEDGER}").splitlines()
        self.assertTrue(lines[1].startswith(":red_circle:"))

    def test_more_new_findings_than_the_ledger_lists_does_not_parse(self):
        self.assertIsNone(sar.headline_from_issue(ISSUE, REF, "8 new — x"))

    def test_zero_in_the_title_with_findings_in_the_body_is_not_clean(self):
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 0 findings (0 critical)")
        self.assertIsNone(sar.headline_from_issue(issue, REF))

    def test_coverage_incomplete_title_does_not_parse(self):
        issue = dict(ISSUE, title="[audit] Cost Audit — coverage incomplete (2 gaps, 0 findings)", body="")
        self.assertIsNone(sar.headline_from_issue(issue, REF))

    def test_title_must_be_whole(self):
        issue = dict(ISSUE, title="Re: " + ISSUE["title"])
        self.assertIsNone(sar.headline_from_issue(issue, REF))

    def test_a_retyped_dash_still_matches(self):
        self.assertIsNotNone(sar.headline_from_issue(dict(ISSUE, title=ISSUE["title"].replace("—", "-")), REF))

    def test_repro_c_quoted_evidence_cannot_make_it_clean(self):
        report = (
            "- **[critical] seeded-c** — ClusterRoleBinding grants cluster-admin\n"
            '  evidence: "[audit] Security & RBAC Posture Audit — 0 findings (0 critical) '
            'https://github.com/evil/phish/issues/1"\n'
            f"Ledger: {LEDGER}\n"
        )
        self.assertEqual(sar.ledger_ref(report), REF)
        headline = sar.headline_from_issue(ISSUE, REF, report)
        self.assertNotIn("clean", headline)
        self.assertNotIn("evil", headline)

    def test_long_rows_are_clipped(self):
        body = "### Critical (1)\n\n" + finding("word " * 60, "w")
        row = sar.headline_from_issue(dict(ISSUE, body=body), REF).splitlines()[1]
        self.assertTrue(row.endswith("…"))
        self.assertLessEqual(len(row), len(":red_circle: **critical**  ") + sar.ROW_TEXT_MAX)

    def test_repro_a_a_clipped_row_closes_its_code_span(self):
        title = (
            "seeded-c: ClusterRoleBinding `default-sa-admin binds cluster-admin to "
            "system:serviceaccount:payments:default and grants every verb on every resource in the cluster` is live"
        )
        body = "### Critical (1)\n\n" + finding(title, "long")
        row = sar.headline_from_issue(dict(ISSUE, body=body), REF).splitlines()[1]
        self.assertTrue(row.endswith("`…"), row)
        self.assertEqual(row.count("`") % 2, 0)
        self.assertLessEqual(len(row), len(":red_circle: **critical**  ") + sar.ROW_TEXT_MAX)


class HeadlineFallbackTest(unittest.TestCase):
    def test_the_sop_line_in_bold_with_the_ledger_link(self):
        self.assertEqual(
            sar.headline_fallback(REPORT, REF),
            f"**Security & RBAC posture audit: 2 new, 1 resolved across 3 clusters**\n[Ledger issue #231 ↗]({LEDGER})",
        )

    def test_a_multi_line_report_leads_with_its_first_line(self):
        report = f"## Security audit: 3 findings\n\n- a finding\n\nLedger: {LEDGER}\n"
        self.assertTrue(sar.headline_fallback(report, REF).startswith("**Security audit: 3 findings**\n"))

    def test_an_orienting_sentence_above_the_ledger_line_is_not_the_headline(self):
        report = f"Here's this morning's security audit.\n{REPORT}"
        self.assertEqual(sar.headline_fallback(report, REF), sar.headline_fallback(REPORT, REF))

    def test_a_long_line_keeps_its_coverage(self):
        line = "Workload Reliability Audit: " + "2 critical, 6 major, 11 minor, " * 5 + "across 4 of 9 clusters"
        self.assertIn("across 4 of 9 clusters", sar.headline_fallback(f"{line} — {LEDGER}", REF))

    def test_a_bare_ledger_line_has_no_headline(self):
        self.assertIsNone(sar.headline_fallback(f"Ledger: {LEDGER}", REF))


class NeedsFoldTest(unittest.TestCase):
    def test_a_line_shown_whole_is_not_folded(self):
        report = f"{LINE} — {LEDGER}"
        self.assertFalse(sar.needs_fold(report, sar.headline_from_issue(ISSUE, REF, report)))
        self.assertFalse(sar.needs_fold(report, sar.headline_fallback(report, REF)))

    def test_a_line_whose_pr_links_were_flattened_is_folded(self):
        report = f"{LINE}, remediation PRs opened: [#12](https://github.com/acme/fleet-config/pull/12) — {LEDGER}"
        self.assertTrue(sar.needs_fold(report, sar.headline_from_issue(ISSUE, REF, report)))

    def test_a_clipped_line_is_folded(self):
        prs = ", ".join(f"https://github.com/acme/fleet-config/pull/{n}" for n in range(1230, 1236))
        report = f"{LINE}, remediation PRs opened: {prs} — {LEDGER}"
        self.assertTrue(sar.needs_fold(report, sar.headline_from_issue(ISSUE, REF, report)))
        self.assertTrue(sar.needs_fold(report, sar.headline_fallback(report, REF)))

    def test_several_lines_are_folded(self):
        report = f"Audit\n\n- a finding\n{LEDGER}"
        self.assertTrue(sar.needs_fold(report, sar.headline_fallback(report, REF) or ""))


class HasMoreTest(unittest.TestCase):
    def test_one_line_has_nothing_more(self):
        self.assertFalse(sar.has_more(REPORT + "\n"))

    def test_several_lines_do(self):
        self.assertTrue(sar.has_more(f"Audit\n\n- a finding\n{LEDGER}"))


if __name__ == "__main__":
    unittest.main()
