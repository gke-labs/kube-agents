"""Unit tests for slack_audit_report — the fleet-audit headline for Slack.

Run: python3 -m pytest agents/platform/scripts/test_slack_audit_report.py
"""

import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_audit_report as sar

LEDGER = "https://github.com/acme/fleet-config/issues/231"
REF = sar.LedgerRef(LEDGER, "acme/fleet-config", 231)

#: The longest a pathological input may take; the backtracking it guards against took minutes.
FAST_SECONDS = 1.0
LONG_INPUT = 20000
#: A finding heading padded with this many spaces took minutes on a cubic pattern.
HEADING_PADDING = 60000
#: A heading of this many "<!--finding:" runs (96 KB) took 15 seconds with only the whitespace runs fixed.
MARKER_REPEATS = 8000
#: A relayed line of this many unclosed "<!" (96 KB) took 2 seconds while a mention scanned past a "<".
MENTION_REPEATS = 48000
#: A title of this many "new" tags; matching the run as one group was quadratic in it.
TAG_REPEATS = 20000
#: The link pass of Hermes' ``SlackAdapter.format_message``, which every text post goes through:
#: whatever it matches reaches Slack as a live ``<url|label>`` link.
HERMES_TEXT_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
#: Links Hermes' text path reads that a narrower pattern leaves whole: a space
#: inside a parenthesised URL part, a space, tab or angle brackets in the URL,
#: a label ending in "[", and a label holding one.
HERMES_ONLY_LINKS = (
    "[click here](https://evil.example/x(a b))",
    "[x](https://evil.example/a b)",
    "[x]( https://evil.example )",
    "[x](<https://evil.example/a b>)",
    "[x](https://evil.example/\tz)",
    "[a[](https://evil.example/)",
    "[a [b](https://evil.example/x(a b))",
)

#: The SOP's relayed line (compliance_audit_sop.md), with its trailing ledger URL.
REPORT = f"Security & RBAC posture audit: 2 new, 1 resolved across 3 clusters — {LEDGER}"


def finding(title, fid, new=False):
    marker = "<!-- finding-new -->\n\n" if new else ""
    return f'<a id="finding-{fid}"></a>\n\n#### {title} <!-- finding:{fid} -->\n\n{marker}- **Where:** `seeded-a` — `x`\n'


BODY = (
    "7 findings: 2 critical, 1 major, 4 minor.\n\n"
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
HEADLINE = "**Security & RBAC Posture Audit: 2 critical findings**"
LINK = f"[Ledger issue #231 ↗]({LEDGER})"
#: Two critical findings and nothing else, so the card lists every one.
CRITICAL_BODY = (
    "### Critical (2)\n\n"
    + finding("seeded-b, seeded-c: `cluster-admin` bound to the default service account", "rbac-1")
    + finding("seeded-c: a ClusterRole grants `*` on secrets", "rbac-2")
)


def skipped(*names, more=0, reason="unreachable: dial tcp: i/o timeout"):
    """BODY with fleet-audit's Scope table of clusters it could not audit."""
    rows = "".join(f"| `{name}` | {reason} |\n" for name in names)
    overflow = f"| _…and {more} more_ |  |\n" if more else ""
    return (
        "## Scope\n\n### Skipped\n\n**Coverage is partial.** clusters could not be audited, so this report says nothing about them.\n\n"
        "| Cluster | Reason |\n| ------- | ------ |\n" + rows + overflow + "\n## Findings\n\n" + BODY
    )


def gap_line(text):
    return next((line for line in text.splitlines() if line.startswith(sar.GAP_MARK)), None)


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
            f"Security audit: 2 new across 3 of 5 clusters. [Ledger issue #231]({LEDGER})",
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

    def test_a_link_label_without_ledger_is_not_the_ledger(self):
        self.assertIsNone(sar.ledger_ref(f"Security audit: 2 new. [see this]({LEDGER})"))

    def test_bracket_runs_do_not_backtrack(self):
        for text in ("[ledger " * (LONG_INPUT // 8), "[" + "ledger " * (LONG_INPUT // 7), "— " * LONG_INPUT):
            with self.subTest(text=text[:16]):
                start = time.monotonic()
                self.assertIsNone(sar.ledger_ref(text))
                self.assertLess(time.monotonic() - start, FAST_SECONDS)
        start = time.monotonic()
        self.assertIsNone(sar.ledger_ref("[" * LONG_INPUT))
        self.assertEqual(sar._row_text("[" * LONG_INPUT), "[" * LONG_INPUT)
        self.assertLess(time.monotonic() - start, FAST_SECONDS)

    def test_a_long_report_still_names_its_ledger(self):
        self.assertEqual(sar.ledger_ref("[ledger " * (LONG_INPUT // 8) + "\n" + REPORT), REF)

    def test_a_dot_segment_repository_is_not_the_ledger(self):
        for repo in ("../..", "./x", "acme/.."):
            with self.subTest(repo=repo):
                self.assertIsNone(sar.ledger_ref(f"done — https://github.com/{repo}/issues/1"))

    def test_an_overlong_issue_number_is_not_the_ledger(self):
        self.assertIsNone(sar.ledger_ref("done — https://github.com/o/r/issues/" + "9" * 5000))


class HeadlineFromIssueTest(unittest.TestCase):
    def test_approved_layout(self):
        self.assertEqual(
            sar.headline_from_issue(ISSUE, REF, REPORT),
            f"{HEADLINE}\n"
            "`critical` seeded-b, seeded-c: `cluster-admin` bound to the default service account\n"
            "`critical` seeded-c: a ClusterRole grants `*` on secrets\n"
            "5 more (1 major, 4 minor) are in the ledger issue.\n"
            f"{LINK}",
        )

    def test_approved_layout_with_a_cluster_not_reached(self):
        body = skipped("seeded-c").replace("7 findings: 2 critical, 1 major, 4 minor.", "7 findings: 2 critical, 3 major, 2 minor.")
        self.assertEqual(
            sar.headline_from_issue(dict(ISSUE, body=body), REF, REPORT).splitlines()[3:],
            ["5 more (3 major, 2 minor) are in the ledger issue.", "⚠️ Couldn't reach seeded-c, so this run didn't check it.", LINK],
        )
    def test_a_finding_new_since_the_last_run_is_tagged(self):
        body = BODY.replace(
            finding("seeded-c: a ClusterRole grants `*` on secrets", "rbac-2"),
            finding("seeded-c: a ClusterRole grants `*` on secrets", "rbac-2", new=True),
        )
        lines = sar.headline_from_issue(dict(ISSUE, body=body), REF, REPORT).splitlines()
        self.assertEqual(lines[1], "`critical` seeded-b, seeded-c: `cluster-admin` bound to the default service account")
        self.assertEqual(lines[2], "`critical` seeded-c: a ClusterRole grants `*` on secrets · _new_")
        self.assertEqual(len(lines), 5)

    def test_no_finding_is_tagged_without_its_marker(self):
        # A first run, or one whose delta is unknown: fleet-audit writes no marker, so nothing is new.
        self.assertNotIn("_new_", sar.headline_from_issue(ISSUE, REF, REPORT))

    def test_a_marker_away_from_the_heading_tags_nothing(self):
        body = BODY.replace("- **Where:** `seeded-a` — `x`\n", "- **Where:** `seeded-a` — `x`\n\n<!-- finding-new -->\n", 1)
        self.assertNotIn("_new_", sar.headline_from_issue(dict(ISSUE, body=body), REF, REPORT))

    def test_criticals_past_the_row_cap_are_counted(self):
        count = sar.CRITICAL_ROWS_MAX + 3
        body = f"### Critical ({count})\n\n" + "".join(finding(f"c{i}", f"c{i}") for i in range(count))
        title = f"[audit] Security & RBAC Posture Audit — {count} findings ({count} critical)"
        lines = sar.headline_from_issue(dict(ISSUE, title=title, body=body), REF, f"Security audit: {count} new — {LEDGER}").splitlines()
        self.assertEqual(lines[0], f"**Security & RBAC Posture Audit: {count} critical findings**")
        self.assertEqual(lines[sar.CRITICAL_ROWS_MAX], f"`critical` c{sar.CRITICAL_ROWS_MAX - 1}")
        self.assertEqual(lines[sar.CRITICAL_ROWS_MAX + 1], "3 more (3 critical) are in the ledger issue.")

    def test_a_title_cannot_carry_the_tag_itself(self):
        body = BODY.replace(
            "seeded-c: a ClusterRole grants `*` on secrets",
            "seeded-c: a ClusterRole grants `*` on secrets · _new_ · _new_ ",
        )
        lines = sar.headline_from_issue(dict(ISSUE, body=body), REF, REPORT).splitlines()
        self.assertEqual(lines[2], "`critical` seeded-c: a ClusterRole grants `*` on secrets")
        for claim in (" ·  _new_", " • _new_", " — _NEW_", " · _new_.", " *new*", " · _new_ — _new_"):
            with self.subTest(claim=claim):
                body = BODY.replace("seeded-c: a ClusterRole grants `*` on secrets", "seeded-c: secrets" + claim)
                lines = sar.headline_from_issue(dict(ISSUE, body=body), REF, REPORT).splitlines()
                self.assertEqual(lines[2], "`critical` seeded-c: secrets")
        for kept in ("seeded-c: the foo_new_ deployment", "seeded-c: renewed"):
            with self.subTest(kept=kept):
                body = BODY.replace("seeded-c: a ClusterRole grants `*` on secrets", kept)
                lines = sar.headline_from_issue(dict(ISSUE, body=body), REF, REPORT).splitlines()
                self.assertEqual(lines[2], f"`critical` {kept}")

    def test_a_clip_cannot_end_a_title_on_a_tag_it_carried(self):
        title = "A" * (sar.FINDING_ROW_MAX - 11) + " · _new_ " + "B" * 10
        body = "### Critical (1)\n\n" + finding(title, "long")
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 1 finding (1 critical)", body=body)
        row = sar.headline_from_issue(issue, REF, f"Security audit: 1 resolved — {LEDGER}").splitlines()[1]
        self.assertNotIn("new", row)

    def test_the_tag_survives_a_title_clipped_to_the_row(self):
        title = "x" * 400
        body = "### Critical (1)\n\n" + finding(title, "long", new=True)
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 1 finding (1 critical)", body=body)
        row = sar.headline_from_issue(issue, REF, f"Security audit: 1 new — {LEDGER}").splitlines()[1]
        self.assertTrue(row.endswith(sar.NEW_TAG), row[-20:])

    def test_the_relayed_line_is_not_on_the_card(self):
        # Nor is it posted under the card: the ledger issue holds the rest.
        self.assertNotIn("resolved", sar.headline_from_issue(ISSUE, REF, REPORT))

    def test_no_critical_findings_lead_with_the_most_severe_present(self):
        body = (
            "5 findings: 0 critical, 3 major, 2 minor.\n\n### Major (3)\n\n"
            + finding("a", "a") + finding("b", "b") + finding("c", "c")
            + "\n### Minor (2)\n\n" + finding("d", "d") + finding("e", "e")
        )
        issue = dict(ISSUE, title="[audit] Workload Reliability Audit — 5 findings (0 critical)", body=body)
        self.assertEqual(
            sar.headline_from_issue(issue, REF).splitlines(),
            ["**Workload Reliability Audit: 3 major findings**", "`major` a", "`major` b", "3 more (1 major, 2 minor) are in the ledger issue.", LINK],
        )

    def test_a_summary_count_no_section_lists_does_not_lead(self):
        body = (
            "3 findings: 1 critical, 1 major, 1 minor.\n\n### Major (1)\n\n"
            + finding("a", "a") + "\n### Minor (1)\n\n" + finding("d", "d")
        )
        issue = dict(ISSUE, title="[audit] Workload Reliability Audit — 3 findings (0 critical)", body=body)
        lines = sar.headline_from_issue(issue, REF).splitlines()
        self.assertEqual(lines[:2], ["**Workload Reliability Audit: 1 major finding**", "`major` a"])

    def test_without_a_summary_the_sections_are_counted(self):
        body = BODY.replace("7 findings: 2 critical, 1 major, 4 minor.\n\n", "")
        lines = sar.headline_from_issue(dict(ISSUE, body=body), REF, REPORT).splitlines()
        self.assertEqual(lines[3], "2 more (1 major, 1 minor) are in the ledger issue.")

    def test_one_more_is_singular(self):
        body = "3 findings: 2 critical, 1 major, 0 minor.\n\n" + CRITICAL_BODY + "### Major (1)\n\n" + finding("idle", "c")
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 3 findings (2 critical)", body=body)
        self.assertEqual(sar.headline_from_issue(issue, REF).splitlines()[3], "1 more (1 major) is in the ledger issue.")

    def test_every_finding_listed_leaves_off_the_more_line(self):
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 2 findings (2 critical)", body=CRITICAL_BODY)
        lines = sar.headline_from_issue(issue, REF, REPORT).splitlines()
        self.assertEqual(lines[0], HEADLINE)
        self.assertEqual(lines[3:], [LINK])
    def test_singular_title(self):
        issue = dict(ISSUE, title="[audit] Cost Audit — 1 finding (0 critical)", body="### Major (1)\n\n" + finding("idle", "c"))
        headline = sar.headline_from_issue(issue, REF).splitlines()
        self.assertEqual(headline, ["**Cost Audit: 1 major finding**", "`major` idle", LINK])

    def test_a_body_listing_no_findings_does_not_parse(self):
        # The card would count findings that neither it nor its thread could show.
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, body="Summary."), REF, REPORT))
    def test_a_closed_ledger_is_the_clean_card(self):
        # Only a clean run closes the ledger, leaving the old title: its counts are the last run's.
        closed = dict(ISSUE, state="closed")
        for report in ("", f"Security & RBAC posture audit: clean across 3 clusters — {LEDGER}", f"Ledger: {LEDGER}"):
            with self.subTest(report=report):
                self.assertEqual(
                    sar.headline_from_issue(closed, REF, report), f"**Security & RBAC Posture Audit: clean. Ledger closed.**\n{LINK}"
                )
        coverage = dict(closed, title="[audit] Cost Audit — coverage incomplete (2 gaps, 0 findings)")
        self.assertTrue(sar.headline_from_issue(coverage, REF).startswith("**Cost Audit: clean. Ledger closed.**"))

    def test_a_closed_ledger_under_a_line_counting_findings_does_not_parse(self):
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, state="closed"), REF, REPORT))
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, state="closed"), REF, f"Audit: 7 findings — {LEDGER}"))
        self.assertIsNone(
            sar.headline_from_issue(dict(ISSUE, state="closed"), REF, f"Workload Reliability Audit: 2 critical — {LEDGER}")
        )
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, state=""), REF, ""))
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, state="closed", labels=["bug"]), REF, ""))
        self.assertIsNone(
            sar.headline_from_issue(dict(ISSUE, state="closed"), REF, f"Audit: 1 new critical, 2 new major — {LEDGER}")
        )

    def test_an_issue_without_the_ledger_label_does_not_parse(self):
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, labels=["bug"]), REF, REPORT))
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, labels=None), REF, REPORT))

    def test_labels_as_names_or_dicts(self):
        for labels in (["agent:audit"], [{"name": "agent:audit"}], ("agent:audit",)):
            with self.subTest(labels=labels):
                self.assertIsNotNone(sar.headline_from_issue(dict(ISSUE, labels=labels), REF, REPORT))

    def test_labels_of_any_other_shape_are_not_the_ledger(self):
        for labels in ("agent:audit-retired", "agent:audit", {"agent:audit": 1}, [{"label": "agent:audit"}], 7):
            with self.subTest(labels=labels):
                self.assertIsNone(sar.headline_from_issue(dict(ISSUE, labels=labels), REF, REPORT))

    def test_overlong_counts_do_not_parse(self):
        big = "9" * 5000
        for report in (f"{big} new findings — {LEDGER}", f"{big} resolved — {LEDGER}", f"{big} findings — {LEDGER}"):
            with self.subTest(report=report[-40:]):
                self.assertIsNone(sar.headline_from_issue(ISSUE, REF, report))
        title = f"[audit] Security & RBAC Posture Audit — {big} findings (2 critical)"
        self.assertIsNone(sar.headline_from_issue(dict(ISSUE, title=title), REF, REPORT))

    def test_the_relayed_line_cannot_post_a_link_or_a_mention(self):
        report = f"Security audit: 3 new <!channel> see <https://evil.example|Ledger issue #7 ↗> [x](https://evil.example) — {LEDGER}"
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 7 findings (2 critical)")
        texts = [
            sar.headline_from_issue(issue, REF, report),
            str(sar.blocks_from_issue(issue, REF, report)),
            sar.headline_fallback(report, REF),
        ]
        for text in texts:
            self.assertNotIn("evil.example", text)
            self.assertNotIn("<!", text)
        self.assertTrue(texts[0].startswith(HEADLINE + "\n"), texts[0])
        self.assertIn("3 new !channel see Ledger issue #7 ↗ x", texts[2])

    def test_the_state_is_read_in_any_case(self):
        self.assertIsNotNone(sar.headline_from_issue(dict(ISSUE, state="OPEN"), REF, REPORT))

    def test_a_zero_finding_title_does_not_parse(self):
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 0 findings (0 critical)")
        self.assertIsNone(sar.headline_from_issue(issue, REF, LEDGER))

    def test_the_card_does_not_restate_the_lines_counts(self):
        line = (
            "AI Workload Security Audit: 1 critical, 3 major, 2 minor across 2 of 7 clusters "
            "(2 new, 1 resolved, 1 remediation PR opened)"
        )
        text = sar.headline_from_issue(ISSUE, REF, f"Here's the audit.\n{line} — {LEDGER}")
        self.assertTrue(text.startswith(HEADLINE + "\n"), text)
        for phrase in ("across", "new", "resolved", "remediation"):
            self.assertNotIn(phrase, text)

    def test_a_gap_the_line_names_gets_the_warning_line(self):
        # Without a Skipped table, the line's own words for what it did not scan.
        for line, gap in (
            ("Security audit: 7 findings across 3 clusters (1 unreachable).", "1 cluster unreachable."),
            ("Security audit: 7 findings across 3 clusters (2 unreachable)", "2 clusters unreachable."),
            ("Security audit: 7 findings across 3 clusters; seeded-c could not be scanned.", "seeded-c could not be scanned."),
            ("Security audit: 2 new across 3 clusters, but seeded-c was not reached", "seeded-c was not reached."),
            ("Security audit: 7 findings across 3 clusters but seeded-c was not reached", "seeded-c was not reached."),
            (
                "Security audit: 7 findings across 3 clusters, but 1 of 3 clusters and seeded-c were unreachable",
                "1 of 3 clusters and seeded-c were unreachable.",
            ),
            (
                "Security audit: 7 findings across 3 clusters and 2 namespaces were denied and seeded-c was skipped.",
                "2 namespaces were denied and seeded-c was skipped.",
            ),
            ("Security audit: 7 findings across 3 clusters — seeded-b was unreachable.", "seeded-b was unreachable."),
            ("Security audit: 2 new across 3 clusters (see appendix) – seeded-b unreachable.", "seeded-b unreachable."),
        ):
            with self.subTest(line=line):
                text = sar.headline_from_issue(ISSUE, REF, f"{line} — {LEDGER}")
                self.assertEqual(text.splitlines()[0], HEADLINE)
                self.assertEqual(gap_line(text), sar.GAP_MARK + gap)
    def test_every_gap_is_kept_and_its_neighbouring_counts_are_not(self):
        for line, gap in (
            ("Security audit: 7 findings across 3 clusters (1 skipped, 2 unreachable).", "1 cluster skipped, 2 clusters unreachable."),
            ("Security audit: 7 findings across 3 clusters (2 new and 1 skipped).", "1 cluster skipped."),
            ("Security audit: 7 findings across 3 clusters, 1 skipped.", "1 cluster skipped."),
            ("Security audit: 7 findings across 3 clusters (2 new but 1 unreachable).", "1 cluster unreachable."),
            ("Security audit: 7 findings across 3 clusters (2 new and seeded-c skipped).", "seeded-c skipped."),
            ("Security audit: 7 findings across 3 clusters; skipped: seeded-d (no credentials).", "Skipped: seeded-d (no credentials)."),
            (
                "Security audit: 7 findings across 3 clusters; 2 clusters could not be scanned: seeded-d, seeded-e.",
                "2 clusters could not be scanned: seeded-d, seeded-e.",
            ),
            (
                "Security audit: 7 findings across 3 clusters (1 unreachable); seeded-c could not be scanned.",
                "1 cluster unreachable, seeded-c could not be scanned.",
            ),
            ("Security audit: 7 findings across 3 clusters (1 unreachable); 1 cluster unreachable.", "1 cluster unreachable."),
        ):
            with self.subTest(line=line):
                text = sar.headline_from_issue(ISSUE, REF, f"{line} — {LEDGER}")
                self.assertEqual(gap_line(text), sar.GAP_MARK + gap)
                self.assertNotIn("2 new", text)

    def test_a_gap_past_the_display_clip_still_gets_its_line(self):
        line = "Security audit: 7 findings across 3 clusters; " + "x" * sar.REPORT_LINE_MAX + "; seeded-c could not be scanned"
        text = sar.headline_from_issue(ISSUE, REF, f"{line} — {LEDGER}")
        self.assertTrue(gap_line(text).endswith("seeded-c could not be scanned."), gap_line(text))
    def test_the_gap_line_follows_the_more_line(self):
        line = "Security audit: 2 new, 1 resolved across 3 clusters (1 unreachable)"
        lines = sar.headline_from_issue(ISSUE, REF, f"{line} — {LEDGER}").splitlines()
        self.assertEqual(lines[3:], ["5 more (1 major, 4 minor) are in the ledger issue.", "⚠️ 1 cluster unreachable.", LINK])
    def test_a_gap_inside_a_parenthetical_of_counts_leaves_the_counts(self):
        # The example line in obtainability_audit_sop.md.
        line = (
            "Workload Reliability Audit: 2 critical, 6 major, 11 minor across 4 clusters "
            "(3 new, 1 resolved, 1 skipped, 1 remediation PR opened)"
        )
        text = sar.headline_from_issue(ISSUE, REF, f"{line} — {LEDGER}")
        self.assertEqual(gap_line(text), "⚠️ 1 cluster skipped.")
        for count in ("3 new,", "resolved", "remediation"):
            self.assertNotIn(count, text)

    def test_a_clause_saying_nothing_was_missed_is_not_a_gap(self):
        line = "Security audit: 2 new across 3 clusters, no clusters unreachable"
        text = sar.headline_from_issue(ISSUE, REF, f"{line} — {LEDGER}")
        self.assertIsNone(gap_line(text))
        self.assertNotIn("unreachable", text)
    def test_a_denied_or_forbidden_finding_is_not_a_gap(self):
        for line in (
            "Security audit: 2 new across 3 clusters; 5 pods forbidden from privileged mode",
            "Security audit: 2 new across 3 clusters; 3 service accounts denied by policy",
            "Security audit: 7 findings across 3 clusters, 4 access denied events",
            "Security audit: 7 findings across 3 clusters (2 new, 1 forbidden)",
        ):
            with self.subTest(line=line):
                text = sar.headline_from_issue(ISSUE, REF, f"{line} — {LEDGER}")
                self.assertIsNone(gap_line(text))
                self.assertNotRegex(text, "denied|forbidden")

    def test_a_gap_word_negated_after_it_or_inside_a_url_is_not_a_gap(self):
        for line in (
            "Security audit: 7 findings across 3 clusters; clusters skipped: none.",
            "Security audit: 7 findings across 3 clusters; unreachable: 0.",
            "Security audit: 7 findings across 3 clusters; see https://x.example/unreachable for details",
        ):
            with self.subTest(line=line):
                text = sar.headline_from_issue(ISSUE, REF, f"{line} — {LEDGER}")
                self.assertIsNone(gap_line(text))
                for gap in ("Clusters skipped.", "Unreachable.", "See https"):
                    self.assertNotIn(gap, text)

    def test_a_reason_opening_on_a_negation_is_still_a_gap(self):
        for part, gap in (
            ("seeded-b unreachable: no response from the control plane.", "seeded-b unreachable."),
            ("seeded-b skipped — no credentials.", "seeded-b skipped."),
            ("seeded-b skipped: nothing to read without RBAC.", "seeded-b skipped."),
            ("unreachable: 0 of 3 control planes answered.", "Unreachable."),
        ):
            with self.subTest(part=part):
                line = f"Security audit: 7 findings across 3 clusters; {part}"
                text = sar.headline_from_issue(ISSUE, REF, f"{line} — {LEDGER}")
                self.assertEqual(gap_line(text), sar.GAP_MARK + gap)

    def test_the_skipped_table_names_the_clusters_not_reached(self):
        for names, more, gap in (
            (["seeded-c"], 0, "Couldn't reach seeded-c, so this run didn't check it."),
            (["seeded-b", "seeded-c"], 0, "Couldn't reach seeded-b and seeded-c, so this run didn't check them."),
            (["seeded-a", "seeded-b", "seeded-c"], 0, "Couldn't reach seeded-a, seeded-b and seeded-c, so this run didn't check them."),
            (["a", "b", "c", "d"], 0, "Couldn't reach 4 clusters, so this run didn't check them."),
            # The rows past the cap give no reason, so nothing says they were unreachable.
            (["seeded-c"], 60, "Didn't check 61 clusters this run."),
        ):
            with self.subTest(names=names, more=more):
                text = sar.headline_from_issue(dict(ISSUE, body=skipped(*names, more=more)), REF, REPORT)
                self.assertEqual(gap_line(text), sar.GAP_MARK + gap)

    def test_the_skipped_table_outranks_the_lines_words(self):
        report = f"Security audit: 7 findings across 3 clusters (1 unreachable) — {LEDGER}"
        text = sar.headline_from_issue(dict(ISSUE, body=skipped("seeded-c")), REF, report)
        self.assertEqual(gap_line(text), "⚠️ Couldn't reach seeded-c, so this run didn't check it.")

    def test_a_cluster_skipped_for_another_reason_was_not_checked_rather_than_not_reached(self):
        for names, reason, gap in (
            (["seeded-c"], "no credentials for this cluster", "Didn't check seeded-c this run."),
            (["seeded-b", "seeded-c"], "excluded by the audit's scope", "Didn't check seeded-b and seeded-c this run."),
            (["a", "b", "c", "d"], "no credentials", "Didn't check 4 clusters this run."),
            (["seeded-c"], "control plane unreachable", "Couldn't reach seeded-c, so this run didn't check it."),
            (["seeded-c"], "dial tcp 10.0.0.1:443: connection refused", "Couldn't reach seeded-c, so this run didn't check it."),
        ):
            with self.subTest(reason=reason):
                text = sar.headline_from_issue(dict(ISSUE, body=skipped(*names, reason=reason)), REF, REPORT)
                self.assertEqual(gap_line(text), sar.GAP_MARK + gap)

    def test_one_cluster_skipped_for_another_reason_makes_the_whole_line_not_checked(self):
        body = skipped("seeded-c").replace(
            "| `seeded-c` | unreachable: dial tcp: i/o timeout |\n",
            "| `seeded-c` | unreachable: dial tcp: i/o timeout |\n| `seeded-d` | no credentials |\n",
        )
        text = sar.headline_from_issue(dict(ISSUE, body=body), REF, REPORT)
        self.assertEqual(gap_line(text), "⚠️ Didn't check seeded-c and seeded-d this run.")

    def test_a_skipped_table_outside_the_scope_section_is_not_read(self):
        # A finding's model-written text may carry a table of its own.
        body = BODY.replace("\n### Skipped\n\n", "\n### Skipped\n\n| `seeded-z` | unreachable |\n\n")
        # The second is a Scope with nothing skipped, so the finding's table is the first one after it.
        for body in (body, "## Scope\n\nEvery cluster was audited.\n\n## Findings\n\n" + body):
            with self.subTest(body=body[:20]):
                text = sar.headline_from_issue(dict(ISSUE, body=body), REF, REPORT)
                self.assertIsNone(gap_line(text))
                self.assertNotIn("seeded-z", text)

    def test_a_title_counting_fewer_criticals_than_listed_counts_the_listed(self):
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 7 findings (1 critical)")
        text = sar.headline_from_issue(issue, REF, REPORT)
        self.assertIn("2 critical findings", text)
        self.assertNotIn("-", text.splitlines()[0])

    def test_a_skipped_name_cannot_post_a_link_or_a_mention(self):
        text = sar.headline_from_issue(dict(ISSUE, body=skipped("<!channel>")), REF, REPORT)
        self.assertNotIn("<!", text)
    def test_a_report_that_is_only_the_link_adds_no_line(self):
        self.assertEqual(sar.headline_from_issue(ISSUE, REF, f"Ledger: {LEDGER}"), sar.headline_from_issue(ISSUE, REF, REPORT))
    def test_more_new_findings_than_the_ledger_lists_does_not_parse(self):
        self.assertIsNone(sar.headline_from_issue(ISSUE, REF, "8 new — x"))

    def test_a_zero_finding_run_over_an_open_ledger_does_not_parse(self):
        # A partial or held run with nothing found leaves the ledger open over last run's title.
        for line in (
            "Security & RBAC posture audit: 0 findings, coverage incomplete (2 gaps)",
            "Security & RBAC posture audit: no findings; 1 carried finding unaccounted",
            "Security & RBAC posture audit: zero findings across 3 of 5 clusters",
            # The SOPs leave the wording of a held or partial clean line to the model.
            "Upgrade & patch readiness: clean this run; 2 carried (up-1, up-2) held, ledger stays open",
            "Cost audit: clean across 3 of 5 clusters (2 coverage gaps)",
            "Security & RBAC posture audit: no new findings across 3 of 5 clusters (2 coverage gaps)",
            "Security & RBAC posture audit: nothing new across 3 of 5 clusters",
            "Security & RBAC posture audit: 0 new, 0 resolved across 3 of 5 clusters (2 gaps)",
            "Security & RBAC posture audit: nothing reproduced across 3 of 5 clusters",
            # A held run may count its carried findings by severity; that is not a change.
            "Workload Reliability Audit: held 2 carried findings (1 critical): pdb-a, pdb-b",
            "Workload Reliability Audit: clean, 2 carried findings held (1 critical, 1 major)",
        ):
            with self.subTest(line=line):
                self.assertIsNone(sar.headline_from_issue(ISSUE, REF, f"{line} — {LEDGER}"))

    def test_a_line_that_counts_a_change_parses(self):
        for line in (
            "Security & RBAC posture audit: 0 new, 2 resolved across 3 clusters",
            "Workload Reliability Audit: 2 critical, 6 major across 4 clusters",
            "Cost audit: 1 new critical, 2 new major",
        ):
            with self.subTest(line=line):
                self.assertIsNotNone(sar.headline_from_issue(ISSUE, REF, f"{line} — {LEDGER}"))

    def test_a_new_findings_count_is_not_a_total(self):
        report = f"Upgrade & patch readiness: 3 new findings (1 critical), 2 resolved — {LEDGER}"
        self.assertIsNotNone(sar.headline_from_issue(ISSUE, REF, report))

    def test_a_matching_total_parses(self):
        self.assertIsNotNone(sar.headline_from_issue(ISSUE, REF, f"Audit: 7 findings, 2 new — {LEDGER}"))

    def test_held_rows_are_not_findings(self):
        body = (
            "### Critical (1)\n\n"
            + finding("seeded-c: a ClusterRole grants `*` on secrets", "rbac-2")
            + "\n<!-- audit-held:begin -->\n## Held by the collector\n\n"
            + finding("seeded-a: held from an earlier run", "held-1")
            + "<!-- audit-held:end -->\n"
        )
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 1 finding (1 critical)", body=body)
        headline = sar.headline_from_issue(issue, REF)
        self.assertNotIn("held from an earlier run", headline)
        self.assertEqual(headline.count("`critical` "), 1)

    def test_a_crlf_body_keeps_its_rows(self):
        crlf = dict(ISSUE, body=BODY.replace("\n", "\r\n"))
        self.assertEqual(sar.headline_from_issue(crlf, REF, REPORT), sar.headline_from_issue(ISSUE, REF, REPORT))

    def test_a_title_cannot_post_a_link_or_a_mention(self):
        title = "[seeded-a docs](https://evil.example/x) and <https://evil.example/y|seeded-b> <!channel>"
        body = "### Critical (1)\n\n" + finding(title, "evil")
        row = sar.headline_from_issue(dict(ISSUE, body=body), REF).splitlines()[1]
        self.assertNotIn("evil.example", row)
        self.assertNotIn("<", row)
        self.assertIn("seeded-a docs and seeded-b !channel", row)

    def test_a_link_whose_url_holds_a_spaced_parenthetical_is_reduced(self):
        # Hermes' converter takes a space or tab inside the url's parentheses, so the reader must too.
        for link in ("[click](https://evil.example/x(a b))", "[click](https://evil.example/a(b\tc))"):
            with self.subTest(link=link):
                body = "### Critical (1)\n\n" + finding(link, "evil") + finding("second", "b")
                title = f"[audit] Fleet {link} — 7 findings (1 critical)"
                report = f"Fleet audit: 7 findings, 1 new {link} — {LEDGER}"
                issue = dict(ISSUE, title=title, body=body)
                texts = [
                    sar.headline_from_issue(issue, REF, report),
                    str(sar.blocks_from_issue(issue, REF, report)),
                    sar.headline_fallback(report, REF),
                ]
                for text in texts:
                    self.assertNotIn("evil.example", text)

    def test_a_nested_link_in_a_title_leaves_no_link_behind(self):
        for title in ("[[y](@U2)](!channel)", "[[[z](#C1)](@U2)](!here)", "<!channel|[y](@U2)>"):
            with self.subTest(title=title):
                text = sar._row_text(title)
                self.assertIsNone(sar.MD_LINK.search(text))
                self.assertIsNone(sar.SLACK_LINK.search(text))

    def test_two_rows_cannot_make_one_link(self):
        for first, second in (("see [the", "](https://evil.example/x) fix"), ("[a](https://evil.example/x", "y)")):
            with self.subTest(first=first):
                body = "### Critical (2)\n\n" + finding(first, "a") + finding(second, "b")
                issue = dict(ISSUE, title="[audit] Security Audit — 2 findings (2 critical)", body=body)
                headline = sar.headline_from_issue(issue, REF)
                self.assertEqual([m.group(2) for m in HERMES_TEXT_LINK.finditer(headline)], [LEDGER])

    def test_the_audit_name_cannot_post_a_link_or_a_mention(self):
        title = "[audit] Security & RBAC Posture Audit <!channel> [see](https://evil.example/x) — 7 findings (2 critical)"
        head = sar.headline_from_issue(dict(ISSUE, title=title), REF).splitlines()[0]
        self.assertNotIn("evil.example", head)
        self.assertNotIn("<", head)
        self.assertEqual(head, "**Security & RBAC Posture Audit !channel see: 2 critical findings**")

    def test_a_hash_line_in_fenced_evidence_ends_no_section(self):
        # fleet-audit's trim_command marks a long evidence command with a column-0 "# " line.
        command = "```bash\nkubectl get clusterrolebindings\n# … (command truncated by audit_report.py)\n"
        for close in ("```\n", ""):  # balanced, and a fence the model left open
            with self.subTest(balanced=bool(close)):
                body = (
                    "### Critical (2)\n\n"
                    + finding("seeded-b: first critical", "a")
                    + command
                    + close
                    + finding("seeded-c: second critical", "b")
                    + "\n### Major (1)\n\n"
                    + finding("seeded-a: a major", "x")
                )
                rows = sar.headline_from_issue(dict(ISSUE, body=body), REF).splitlines()[1:3]
                self.assertTrue(rows[0].endswith("seeded-b: first critical"), rows)
                self.assertTrue(rows[1].endswith("seeded-c: second critical"), rows)

    def test_an_unbalanced_fence_ends_at_the_next_severity_heading(self):
        # An impact note can carry a stray fence; the renderer's severity heading still closes it.
        body = (
            "### Critical (1)\n\n"
            + finding("crit one", "c")
            + "- **Impact:** see\n```\n"
            + "\n### Major (2)\n\n"
            + finding("maj one", "m1")
            + finding("maj two", "m2")
        )
        self.assertEqual(
            sar._severity_findings(body),
            [("critical", "crit one", False), ("major", "maj one", False), ("major", "maj two", False)],
        )

    def test_a_padded_title_parses_unchanged(self):
        body = "### Critical (1)\n\n#### \t  seeded-b: privileged pods \t  <!-- finding: p-1 -->\n"
        self.assertEqual(sar._severity_findings(body), [("critical", "seeded-b: privileged pods", False)])

    def test_unclosed_mentions_stay_linear(self):
        mentions = "<!" * MENTION_REPEATS
        start = time.monotonic()
        sar._row_text(mentions)
        self.assertLess(time.monotonic() - start, FAST_SECONDS)

    def test_a_title_of_repeated_tags_stays_linear(self):
        # A pattern matching the tag run as one group took 27 seconds on 20,000 tags before a word.
        for title in ("_new_ " * TAG_REPEATS + "x", " · _new_" * TAG_REPEATS):
            with self.subTest(title=title[-8:]):
                start = time.monotonic()
                sar._untagged(title)
                self.assertLess(time.monotonic() - start, FAST_SECONDS)

    def test_unclosed_mentions_survive_into_the_fallback_headline(self):
        # The headline reads only LINE_READ_MAX of the line, so this is fast
        # whatever MENTION does; the timing above is what pins the pattern.
        mentions = "<!" * MENTION_REPEATS
        headline = sar.headline_fallback(f"Fleet audit: 7 findings {mentions} — {LEDGER}", REF)
        self.assertTrue(headline.startswith("**Fleet audit: 7 findings <!"))

    def test_a_long_finding_heading_does_not_backtrack(self):
        for heading in ("#### " + " " * LONG_INPUT + "x", "#### " + "<!--finding:" * MARKER_REPEATS):
            with self.subTest(heading=heading[:16]):
                start = time.monotonic()
                self.assertEqual(sar._severity_findings(f"### Critical (1)\n\n{heading}\n"), [])
                self.assertLess(time.monotonic() - start, FAST_SECONDS)
        title, fid = "t" * 300, "i" * 100
        body = f"### Critical (1)\n\n####   {title}   <!-- finding: {fid} -->\n"
        self.assertEqual(sar._severity_findings(body), [("critical", title, False)])

    def test_a_heading_of_spaces_stays_linear(self):
        for tail in ("", "<!-- finding:", "<!-- finding: x --> y"):
            with self.subTest(tail=tail):
                body = "### Critical (1)\n\n#### " + " " * HEADING_PADDING + tail + "\n"
                start = time.monotonic()
                self.assertEqual(sar._severity_findings(body), [])
                self.assertLess(time.monotonic() - start, FAST_SECONDS)

    def test_a_heading_line_in_balanced_evidence_ends_no_fence(self):
        # Evidence output can print a column-0 "### " line; only a marked heading closes an open fence.
        evidence = "```\n### generated\n#### seeded-z: not a finding\n```\n"
        body = (
            "### Critical (2)\n\n"
            + finding("seeded-b: first critical", "a")
            + evidence
            + finding("seeded-c: second critical", "b")
        )
        rows = sar.headline_from_issue(dict(ISSUE, body=body), REF).splitlines()[1:3]
        self.assertTrue(rows[0].endswith("seeded-b: first critical"), rows)
        self.assertTrue(rows[1].endswith("seeded-c: second critical"), rows)

    def test_a_severity_heading_in_balanced_evidence_relabels_nothing(self):
        body = (
            "### Critical (2)\n\n"
            "#### First critical <!-- finding:a.1 -->\n\n"
            "```\nkubectl logs x\n### Minor (1)\n```\n\n"
            '<a id="b"></a>\n\n'
            "#### Second critical <!-- finding:a.2 -->\n"
        )
        self.assertEqual(
            sar._severity_findings(body), [("critical", "First critical", False), ("critical", "Second critical", False)]
        )

    def test_a_severity_heading_in_an_open_fence_still_ends_it(self):
        body = (
            "### Major (1)\n\n"
            "#### Real major <!-- finding:a.1 -->\n\n"
            "```\nlog\n### Minor (1)\n\n"
            "#### A minor <!-- finding:b.1 -->\n\n```\n"
        )
        self.assertEqual(sar._severity_findings(body), [("major", "Real major", False), ("minor", "A minor", False)])

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
        self.assertLessEqual(len(row), len("`critical` ") + sar.FINDING_ROW_MAX)

    def test_repro_a_a_clipped_row_closes_its_code_span(self):
        title = (
            "seeded-c: ClusterRoleBinding `default-sa-admin binds cluster-admin to "
            "system:serviceaccount:payments:default and grants every verb on every resource in the cluster` is live"
        )
        body = "### Critical (1)\n\n" + finding(title, "long")
        row = sar.headline_from_issue(dict(ISSUE, body=body), REF).splitlines()[1]
        self.assertTrue(row.endswith("`…"), row)
        self.assertEqual(row.count("`") % 2, 0)
        self.assertLessEqual(len(row), len("`critical` ") + sar.FINDING_ROW_MAX)


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

    def test_an_orienting_sentence_is_not_the_headline_over_a_bare_link(self):
        report = f"Here's this morning's security audit.\n{LINE}\nLedger: {LEDGER}"
        self.assertEqual(sar.headline_fallback(report, REF), sar.headline_fallback(REPORT, REF))

    def test_no_line_with_counts_over_a_bare_link_has_no_headline(self):
        report = f"Here's this morning's security audit.\n- seeded-a: 2 open findings\nLedger: {LEDGER}"
        self.assertIsNone(sar.headline_fallback(report, REF))

    def test_a_long_line_keeps_its_coverage(self):
        line = "Workload Reliability Audit: " + "2 critical, 6 major, 11 minor, " * 5 + "across 4 of 9 clusters"
        self.assertIn("across 4 of 9 clusters", sar.headline_fallback(f"{line} — {LEDGER}", REF))

    def test_a_bare_ledger_line_has_no_headline(self):
        self.assertIsNone(sar.headline_fallback(f"Ledger: {LEDGER}", REF))

    def test_no_link_hermes_would_post_survives(self):
        for link in HERMES_ONLY_LINKS:
            with self.subTest(link=link):
                headline = sar.headline_fallback(f"Security audit: 3 new, see {link} — {LEDGER}", REF)
                self.assertEqual([m.group(2) for m in HERMES_TEXT_LINK.finditer(headline)], [LEDGER])
                self.assertIsNone(HERMES_TEXT_LINK.search(sar._row_text(link)))

    def test_link_runs_do_not_backtrack(self):
        nested = "[" * LONG_INPUT + "x" + "](u)" * LONG_INPUT
        for text in ("[](" * LONG_INPUT, "[a](" + "(" * LONG_INPUT, "[" * LONG_INPUT + "](" + "x" * LONG_INPUT, nested):
            with self.subTest(text=text[:8]):
                start = time.monotonic()
                sar.headline_fallback(f"Security audit: 3 new {text} — {LEDGER}", REF)
                self.assertLess(time.monotonic() - start, FAST_SECONDS)


class LedgerLineTest(unittest.TestCase):
    def test_a_ledger_url_wrapped_onto_its_own_line_is_not_the_line(self):
        for report in (
            f"{LINE} —\n{LEDGER}",
            f"{LINE}\nLedger:\n<{LEDGER}>",
            f"{LINE} —\n[#231]({LEDGER})",
            f"Here's the 2026-10-02 security audit.\n{LINE}\nLedger: {LEDGER}",
            f"{LINE}\nRemediation PRs: #240, #241.\nLedger: {LEDGER}",
            f"{LINE}\n2 remediation pull requests are open.\nLedger: {LEDGER}",
            f"{LINE}\nNext run 2026-10-03 06:00 UTC.\nLedger: {LEDGER}",
        ):
            with self.subTest(report=report):
                self.assertEqual(sar.ledger_ref(report), REF)
                self.assertEqual(sar._ledger_line(report), LINE)
                self.assertTrue(sar.headline_fallback(report, REF).startswith(f"**{LINE}**"))

    def test_an_epilogue_shaped_like_an_audit_line_is_still_taken_for_it(self):
        # Known limit: a findings total or change count after the audit line wins.
        for epilogue in ("3 findings resolved since yesterday.", "1 critical finding is still awaiting a fix."):
            with self.subTest(epilogue=epilogue):
                self.assertEqual(sar._ledger_line(f"{LINE}\n{epilogue}\nLedger: {LEDGER}"), epilogue)


class BalancedClipTest(unittest.TestCase):
    def test_a_short_title_with_a_stray_backtick_is_left_alone(self):
        title = "seeded-a: `default SA is cluster-admin"
        self.assertEqual(sar._balanced_clip(title, sar.FINDING_ROW_MAX), title)


class LedgerLinkTest(unittest.TestCase):
    """Nothing posts the report under its headline, so every headline must link the ledger itself."""

    def test_every_headline_links_the_ledger(self):
        report = f"{LINE} — {LEDGER}"
        link = sar.LEDGER_LINK.format(number=REF.number, url=REF.url)
        closed = dict(ISSUE, state="closed")
        for name, text in (
            ("card", sar.headline_from_issue(ISSUE, REF, report)),
            ("clean", sar.headline_from_issue(closed, REF, f"Ledger: {LEDGER}")),
            ("fallback", sar.headline_fallback(report, REF)),
        ):
            with self.subTest(name=name):
                self.assertIn(link, text)

    def test_every_block_card_has_the_ledger_button(self):
        closed = dict(ISSUE, state="closed")
        for name, built in (
            ("card", sar.blocks_from_issue(ISSUE, REF, REPORT)),
            ("clean", sar.blocks_from_issue(closed, REF, f"Ledger: {LEDGER}")),
        ):
            with self.subTest(name=name):
                blocks, _ = built
                self.assertEqual(blocks[-1]["elements"][-1]["url"], REF.url)


class BlocksFromIssueTest(unittest.TestCase):
    def test_approved_layout(self):
        blocks, text = sar.blocks_from_issue(ISSUE, REF, REPORT)
        self.assertEqual([b["type"] for b in blocks], ["rich_text", "divider", "rich_text", "divider", "rich_text", "actions"])
        self.assertEqual(
            blocks[0]["elements"][0]["elements"],
            [{"type": "text", "text": "Security & RBAC Posture Audit: 2 critical findings", "style": {"bold": True}}],
        )
        self.assertEqual(len(blocks[2]["elements"]), 2)
        self.assertEqual(
            blocks[4]["elements"][0]["elements"], [{"type": "text", "text": "5 more (1 major, 4 minor) are in the ledger issue."}]
        )
        look, link = blocks[5]["elements"]
        self.assertEqual((look["text"]["text"], look["value"]), (sar.LOOK_FIRST, sar.LOOK_FIRST))
        self.assertEqual((look["action_id"], look["style"]), ("kage_audit.choice.0", "primary"))
        self.assertEqual((link["text"]["text"], link["url"]), ("Ledger issue #231 ↗", LEDGER))
        self.assertEqual(
            text,
            "*Security &amp; RBAC Posture Audit: 2 critical findings*\n"
            "`critical` seeded-b, seeded-c: `cluster-admin` bound to the default service account\n"
            "`critical` seeded-c: a ClusterRole grants `*` on secrets\n"
            "5 more (1 major, 4 minor) are in the ledger issue.\n"
            f"<{LEDGER}|Ledger issue #231 ↗>",
        )

    def test_the_gap_line_follows_the_more_line_in_blocks_and_text(self):
        blocks, text = sar.blocks_from_issue(dict(ISSUE, body=skipped("seeded-c")), REF, REPORT)
        gap = "⚠️ Couldn't reach seeded-c, so this run didn't check it."
        self.assertEqual([section["elements"][0]["text"] for section in blocks[4]["elements"]][1], gap)
        self.assertIn(f"in the ledger issue.\n{gap}\n<{LEDGER}|", text)

    def test_the_clean_card_is_one_line_and_the_ledger(self):
        blocks, text = sar.blocks_from_issue(dict(ISSUE, state="closed"), REF, "")
        self.assertEqual([b["type"] for b in blocks], ["rich_text", "actions"])
        self.assertEqual(blocks[0]["elements"][0]["elements"][0]["text"], "Security & RBAC Posture Audit: clean. Ledger closed.")
        self.assertEqual([e["text"]["text"] for e in blocks[1]["elements"]], ["Ledger issue #231 ↗"])
        self.assertEqual(text, f"*Security &amp; RBAC Posture Audit: clean. Ledger closed.*\n<{LEDGER}|Ledger issue #231 ↗>")

    def test_nothing_folds(self):
        for report in (REPORT, ""):
            with self.subTest(report=report):
                blocks, _ = sar.blocks_from_issue(ISSUE, REF, report)
                self.assertNotIn("container", [b["type"] for b in blocks])
                self.assertNotIn("Workload Identity", str(blocks))

    def test_the_fix_value_is_its_label_whatever_the_finding(self):
        for title in ("privileged pods", "word " * 60, "*seeded-b* admits _privileged_ ~pods~"):
            with self.subTest(title=title[:20]):
                body = "### Critical (2)\n\n" + finding(title, "a") + finding("second", "b")
                blocks, _ = sar.blocks_from_issue(dict(ISSUE, body=body), REF, REPORT)
                fix = blocks[4]["elements"][0]
                self.assertEqual(fix["value"], fix["text"]["text"])

    def test_every_finding_shown_has_no_more_line(self):
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 2 findings (2 critical)", body=CRITICAL_BODY)
        blocks, _ = sar.blocks_from_issue(issue, REF, REPORT)
        self.assertEqual([b["type"] for b in blocks], ["rich_text", "divider", "rich_text", "divider", "actions"])
        self.assertEqual([e["text"]["text"] for e in blocks[-1]["elements"]], [sar.LOOK_FIRST, "Ledger issue #231 ↗"])
    def test_the_title_sets_the_critical_count(self):
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 7 findings (3 critical)")
        lines = sar.headline_from_issue(issue, REF, REPORT).splitlines()
        self.assertEqual(lines[0], "**Security & RBAC Posture Audit: 3 critical findings**")
        self.assertEqual(lines[3], "6 more (1 critical, 1 major, 4 minor) are in the ledger issue.")
    def test_a_title_counting_criticals_the_body_does_not_list_leads_on_what_it_lists(self):
        body = (
            "5 findings: 0 critical, 1 major, 4 minor.\n\n### Major (1)\n\n"
            + finding("seeded-a: Workload Identity is off on one node pool", "wi-1")
            + "\n### Minor (4)\n\n"
            + finding("seeded-a: a namespace has no NetworkPolicy", "np-1")
        )
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 5 findings (2 critical)", body=body)
        blocks, text = sar.blocks_from_issue(issue, REF, "")
        self.assertIn("Audit: 1 major finding*", text)
        self.assertNotIn("critical", text)
        self.assertIn(sar.LOOK_FIRST, str(blocks))
    def test_the_relayed_line_is_not_in_the_blocks(self):
        blocks, _ = sar.blocks_from_issue(ISSUE, REF, REPORT)
        self.assertNotIn(LINE, str(blocks))
        self.assertNotIn("resolved", str(blocks))
        self.assertNotIn("new", str(blocks))

    def test_no_blocks_wherever_the_text_headline_falls_back(self):
        for label, issue, report in (
            ("closed", dict(ISSUE, state="closed"), REPORT),
            ("unlabelled", dict(ISSUE, labels=["bug"]), REPORT),
            ("stale", ISSUE, f"Security audit: 9 new — {LEDGER}"),
            ("stale by severity", ISSUE, f"Security audit: 5 new critical, 4 new major — {LEDGER}"),
            ("old total", ISSUE, f"Security audit: 0 findings across 2 of 3 clusters — {LEDGER}"),
            ("zero", dict(ISSUE, title="[audit] Cost Audit — 0 findings (0 critical)"), LEDGER),
            ("none listed", dict(ISSUE, body="Summary."), REPORT),
        ):
            with self.subTest(label):
                self.assertIsNone(sar.headline_from_issue(issue, REF, report))
                self.assertIsNone(sar.blocks_from_issue(issue, REF, report))

    def test_block_rows_cannot_post_a_link_or_a_mention(self):
        body = "### Critical (1)\n\n" + finding("see [the fix](https://evil.example) <!channel>", "l-1")
        blocks, text = sar.blocks_from_issue(dict(ISSUE, body=body), REF, REPORT)
        self.assertNotIn("evil.example", str(blocks) + text)
        self.assertNotIn("<!", str(blocks) + text)
        self.assertEqual(blocks[-1]["elements"][0]["value"], sar.LOOK_FIRST)

    def test_held_and_crlf_rows(self):
        body = (
            "### Critical (1)\r\n\r\n"
            + finding("seeded-c: a ClusterRole grants `*` on secrets", "rbac-2").replace("\n", "\r\n")
            + "\r\n<!-- audit-held:begin -->\r\n## Held by the collector\r\n\r\n"
            + finding("seeded-a: held from an earlier run", "held-1")
        )
        issue = dict(ISSUE, title="[audit] Security & RBAC Posture Audit — 1 finding (1 critical)", body=body)
        blocks, _ = sar.blocks_from_issue(issue, REF, f"Security audit: 1 new — {LEDGER}")
        self.assertIn("Security & RBAC Posture Audit: 1 critical finding'", str(blocks[0]))
        self.assertEqual([e["text"]["text"] for e in blocks[-1]["elements"]], [sar.LOOK_FIRST, "Ledger issue #231 ↗"])
        self.assertNotIn("held from an earlier run", str(blocks))

    def test_the_new_tag_is_italic_in_the_blocks(self):
        body = BODY.replace(
            finding("seeded-c: a ClusterRole grants `*` on secrets", "rbac-2"),
            finding("seeded-c: a ClusterRole grants `*` on secrets", "rbac-2", new=True),
        )
        blocks, text = sar.blocks_from_issue(dict(ISSUE, body=body), REF, REPORT)
        second = blocks[2]["elements"][1]["elements"]
        self.assertEqual(second[-1], {"type": "text", "text": "new", "style": {"italic": True}})
        self.assertIn("secrets · _new_", text)

    def test_a_held_run_over_an_open_ledger_and_an_unparsed_issue_have_no_blocks(self):
        # The open ledger still carries the last run's title; a line saying this run held it is not that run.
        held = "Security & RBAC posture audit: held, 2 critical carried — " + LEDGER
        self.assertIsNone(sar.blocks_from_issue(ISSUE, REF, held))
        self.assertIsNone(sar.blocks_from_issue({"title": "something else"}, REF, REPORT))


if __name__ == "__main__":
    unittest.main()
