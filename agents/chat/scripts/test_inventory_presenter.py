"""Tests for inventory_presenter and its flag gate in bootstrap_delivery."""

import contextlib
import io
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "platform" / "scripts"))

import bootstrap_delivery
import inventory_presenter

#: A paragraph of this many "1 more clusters" runs (96 KB) took 60 seconds with
#: the scope-noun lookahead unbounded.
SCOPE_REPEATS = 6000
#: A roll-up paragraph of this many "1 items x" runs (192 KB) with no colon took
#: 11 seconds while the findings count's scan for a colon was unbounded.
ROLLUP_REPEATS = 19200
#: A headline holding a run of this many spaces took 2 seconds while the severity
#: tag's leading whitespace was tried from every space in the run.
SPACE_REPEATS = 20_000
#: A roll-up word, then this many spaces before a count (40 KB), took 8 to 10
#: seconds while the space around a label's colon was two runs.
LABEL_SPACES = 40_000
#: A posture of this many "skipped: " parts (1.4 MB) took 11 seconds while each
#: gap was cut out of the clause by rereading it.
GAP_PARTS_REPEATS = 160_000
#: A roll-up of this many "these 4 high, those 14 low, " terms (400 KB) took 7
#: seconds while each term was looked up in a list of the restated ones.
RESTATED_REPEATS = 15_000
FAST_SECONDS = 5
RELAY_URL = "http://127.0.0.1:8765"

REPORT = """# GKE Environment Scan

I scanned 3 clusters and 41 workloads. Posture is mostly healthy.

1. **[critical] seeded-b and seeded-c admit privileged pods**
   Any workload can escape to the node; enforce baseline Pod Security.
2. **Default service account is cluster-admin on seeded-c**
   A compromised pod owns the cluster; remove the binding.
3. **Workload Identity is off on seeded-a (major)**
   Pods fall back to the node SA; enable it on the node pool.
4. **payments-api has no PodDisruptionBudget**
   An upgrade can take every replica down; add a PDB.

Also found: 18 more items, tracked in the findings queue — ask for the full list.

The full inventory is available — just ask.
"""

PRESENTED = """**I scanned 3 clusters and 41 workloads and found 22 things to look at.** Two are worth fixing first:

`critical` seeded-b and seeded-c admit privileged pods
Any workload can escape to the node; enforce baseline Pod Security.
**Default service account is cluster-admin on seeded-c**
A compromised pod owns the cluster; remove the binding.

The full inventory is available — just ask.
"""


class PresentTest(unittest.TestCase):
    def test_the_card_headline_top_two_and_the_closing_line(self):
        self.assertEqual(inventory_presenter.present(REPORT), PRESENTED)

    def test_the_rest_and_the_roll_up_are_left_out(self):
        out = inventory_presenter.present(REPORT)
        for absent in ("Workload Identity", "payments-api", "Also found", "18 more", "more worth a look", "mostly healthy"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, out)

    def test_two_items_have_no_lead(self):
        report = "# Scan\n\nAll quiet. One thing stands out.\n\n1. **A (minor)**\n   Fix it.\n2. **B**\n   Fix that.\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 2 things to look at:**\n\n`minor` A\nFix it.\n**B**\nFix that.\n",
        )

    def test_three_items_show_two_under_the_neutral_lead(self):
        report = "Posture.\n\n1. **A**\n   x\n2. **B**\n   y\n3. **C**\n   z\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 3 things to look at.** Start with these two:\n\n**A**\nx\n**B**\ny\n",
        )

    def test_severity_in_the_sentence_is_not_a_label(self):
        report = "Posture.\n\n1. **A**\n   This is not critical.\n"
        self.assertEqual(
            inventory_presenter.present(report), "**I found 1 thing to look at:**\n\n**A**\nThis is not critical.\n"
        )

    def test_a_partial_bold_headline_keeps_the_whole_line(self):
        report = (
            "Posture.\n\n"
            "1. **Critical:** seeded-b and seeded-c admit privileged pods.\n   Enforce baseline.\n"
            "2. **seeded-c:** the default service account is cluster-admin.\n   Remove the binding.\n"
            "3. **No PDB** on payments-api\n   Add one.\n"
        )
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 3 things to look at.** Two are worth fixing first:\n\n"
            "`critical` seeded-b and seeded-c admit privileged pods.\nEnforce baseline.\n"
            "**seeded-c: the default service account is cluster-admin.**\nRemove the binding.\n",
        )

    def test_a_bold_sentence_is_the_headline_and_the_rest_its_sentence(self):
        report = "Posture.\n\n1. **Default SA is cluster-admin on seeded-c.** A compromised pod owns it.\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 1 thing to look at:**\n\n**Default SA is cluster-admin on seeded-c.**\nA compromised pod owns it.\n",
        )

    def test_a_lazy_continuation_stays_in_its_item(self):
        report = (
            "Posture.\n\n"
            "1. **A on seeded-b**\nAny workload can escape.\n"
            "2. **B on seeded-c**\nA compromised pod owns it.\n"
            "3. **C on payments-api**\nAn upgrade takes it down.\n\n"
            "The full inventory is available.\n"
        )
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 3 things to look at.** Start with these two:\n\n**A on seeded-b**\nAny workload can escape.\n"
            "**B on seeded-c**\nA compromised pod owns it.\n\n"
            "The full inventory is available.\n",
        )

    def test_criticals_are_never_rolled_up(self):
        report = "Posture.\n\n" + "".join(f"{i}. **[critical] problem {i}**\n   x\n" for i in range(1, 4))
        report += "4. **other**\n   y\n"
        out = inventory_presenter.present(report)
        rows = "\n".join(f"`critical` problem {i}\nx" for i in range(1, 4))
        self.assertIn("**I found 4 things to look at.** Three are worth fixing first:\n\n" + rows, out)
        self.assertNotIn("other", out)
        self.assertNotIn("[critical]", out)

    def test_more_of_a_scope_noun_is_not_more_findings(self):
        report = "# Scan\n\nI scanned 3 clusters.\n\n1. **A.** b\n2. **B.** c\n\n"
        report += "There are 2 more clusters with 9 findings between them.\n"
        self.assertIn("I scanned 3 clusters and found 11 things to look at.", inventory_presenter.present(report))

    def test_more_across_a_counted_scope_is_more_findings(self):
        report = "# Scan\n\nI scanned 3 clusters.\n\n1. **A.** b\n2. **B.** c\n\n"
        report += "18 more across 3 clusters, including 9 minor findings.\n"
        self.assertIn("I scanned 3 clusters and found 20 things to look at.", inventory_presenter.present(report))

    def test_more_of_what_was_not_scanned_is_not_more_findings(self):
        report = "# Scan\n\nI scanned 3 clusters.\n\n1. **A.** b\n2. **B.** c\n\n"
        for closing in (
            "2 more clusters could not be scanned (permission denied).",
            "2 more clusters are still being set up; ask me later.",
            "1 more namespace is still syncing.",
        ):
            with self.subTest(closing=closing):
                out = inventory_presenter.present(report + closing + "\n")
                self.assertIn("I scanned 3 clusters and found 2 things to look at:", out)
                self.assertTrue(out.endswith("\n\n" + closing + "\n"), out)

    def test_a_long_roll_up_with_no_colon_stays_fast(self):
        report = "# Scan\n\nI scanned 3 clusters.\n\n1. **A.** b\n2. **B.** c\n\n" + "1 items x " * ROLLUP_REPEATS + "\n"
        start = time.monotonic()
        inventory_presenter.present(report)
        self.assertLess(time.monotonic() - start, FAST_SECONDS)

    def test_a_paragraph_of_repeated_scope_nouns_stays_fast(self):
        report = "# Scan\n\nI scanned 3 clusters.\n\n1. **A.** b\n2. **B.** c\n\n" + "1 more clusters " * SCOPE_REPEATS + "\n"
        start = time.monotonic()
        inventory_presenter.present(report)
        self.assertLess(time.monotonic() - start, FAST_SECONDS)

    def test_a_headline_with_a_long_run_of_spaces_stays_fast(self):
        report = "Posture.\n\n" + "".join(f"{i}. **problem {i}" + " " * SPACE_REPEATS + "x**\n   x\n" for i in range(1, 4))
        start = time.monotonic()
        inventory_presenter.present(report)
        self.assertLess(time.monotonic() - start, 0.5)

    def test_a_roll_up_word_then_a_run_of_spaces_stays_fast(self):
        for word in ("also", "also found", "Other findings"):
            with self.subTest(word=word):
                report = TWO_ITEMS + word + " " * LABEL_SPACES + "x\n"
                start = time.monotonic()
                inventory_presenter.present(report)
                self.assertLess(time.monotonic() - start, FAST_SECONDS)

    def test_a_posture_of_many_gaps_stays_fast(self):
        report = "# Scan\n\nI scanned 3 clusters; " + "skipped: " * GAP_PARTS_REPEATS + "\n\n1. **A.** b\n2. **B.** c\n"
        start = time.monotonic()
        inventory_presenter.present(report)
        self.assertLess(time.monotonic() - start, FAST_SECONDS)

    def test_a_roll_up_of_many_restated_terms_stays_fast(self):
        report = TWO_ITEMS + "Other findings: " + "these 4 high, those 14 low, " * RESTATED_REPEATS + "\n"
        start = time.monotonic()
        inventory_presenter.present(report)
        self.assertLess(time.monotonic() - start, FAST_SECONDS)

    def test_a_nul_inside_a_gap_keeps_the_scan_in_the_headline(self):
        report = "# Scan\n\nI scanned 3 clusters; 2 clusters could not\x00 be scanned.\n\n1. **A.** b\n2. **B.** c\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I scanned 3 clusters and found 2 things to look at.**\n2 clusters could not be scanned.\n\n**A.**\nb\n**B.**\nc\n",
        )
        blocks, _ = inventory_presenter.blocks(report)
        texts = [part["text"] for element in blocks[0]["elements"] for part in element["elements"]]
        self.assertEqual(texts, ["I scanned 3 clusters and found 2 things to look at.", "2 clusters could not be scanned."])

    def test_a_gap_is_cut_where_it_was_read(self):
        clause = "skipped: seeded-d, seeded-e (0 unreachable 2 clusters skipped) (permission denied); 2 clusters skipped"
        gaps, rest = inventory_presenter._split_gap(clause)
        self.assertEqual(gaps, ["skipped: seeded-d", "permission denied", "2 clusters skipped"])
        self.assertEqual(rest, " , seeded-e (0 unreachable 2 clusters skipped) ( );  ")

    def test_a_gap_not_found_ahead_of_the_last_falls_back_to_cutting_each(self):
        with mock.patch.object(inventory_presenter, "gap_parts", return_value=["b", "a"]):
            self.assertEqual(inventory_presenter._split_gap("a b"), (["b", "a"], "   "))

    def test_a_dashed_critical_label_is_read(self):
        rows = "\n".join(f"`critical` problem {i}\nx" for i in range(1, 4))
        for separator in (" \u2014 ", "\u2014", " \u2013 ", " - "):
            with self.subTest(separator=separator):
                report = "Posture.\n\n"
                report += "".join(f"{i}. **Critical{separator}problem {i}**\n   x\n" for i in range(1, 4))
                report += "4. **other**\n   y\n"
                out = inventory_presenter.present(report)
                self.assertIn("Three are worth fixing first:\n\n" + rows, out)
                self.assertNotIn("other", out)

    def test_a_hyphenated_severity_word_is_not_a_label(self):
        report = "Posture.\n\n1. **Critical-path job lags on prod**\n   Scale it.\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 1 thing to look at:**\n\n**Critical-path job lags on prod**\nScale it.\n",
        )

    def test_a_list_past_the_sops_cap_is_kept_whole(self):
        # The SOP lists more than five only when every one is critical, labelled or not.
        report = "Posture.\n\n" + "".join(f"{i}. **problem {i} on c{i}**\n   x\n" for i in range(1, 7))
        out = inventory_presenter.present(report)
        for i in range(1, 7):
            self.assertIn(f"problem {i} on c{i}", out)
        self.assertNotIn("Start with these two", out)

    def test_a_quiet_cluster_gets_the_neutral_lead(self):
        report = "No critical or major findings.\n\n"
        report += "".join(f"{i}. **item {i} (minor)**\n   x\n" for i in range(1, 4))
        out = inventory_presenter.present(report)
        self.assertIn("Start with these two:", out)
        self.assertNotIn("worth fixing", out)

    def test_a_severity_word_inside_a_name_is_not_a_label(self):
        report = "Posture.\n\n1. **major-version skew on node pool `minor-pool`**\n   Upgrade.\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 1 thing to look at:**\n\n**major-version skew on node pool `minor-pool`**\nUpgrade.\n",
        )

    def test_a_gap_is_its_own_line_under_the_bold_headline(self):
        report = "2 clusters were unreachable. Scanned 3 clusters.\n\n1. **A (major)**\n   x\n2. **B**\n   y\n3. **C**\n   z\n"
        out = inventory_presenter.present(report)
        self.assertTrue(
            out.startswith(
                "**I scanned 3 clusters and found 3 things to look at.**\n"
                "2 clusters were unreachable. Two are worth fixing first:\n\n"
            ),
            out,
        )

    def test_only_the_counts_the_posture_states_are_named(self):
        report = "Scanned 2 clusters, e.g. prod and staging. Posture is weak.\n\n1. **A**\n   x\n"
        self.assertEqual(
            inventory_presenter.present(report), "**I scanned 2 clusters and found 1 thing to look at:**\n\n**A**\nx\n"
        )

    def test_a_bold_title_line_is_dropped_like_a_heading(self):
        report = "**GKE Environment Scan**\n\nScanned 2 clusters and 41 workloads. Mostly healthy.\n\n1. **A thing**\n   Fix it.\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I scanned 2 clusters and 41 workloads and found 1 thing to look at:**\n\n**A thing**\nFix it.\n",
        )

    def test_a_bold_posture_sentence_is_not_a_title(self):
        report = "**Two problems need attention.**\n\n1. **A thing**\n   Fix it.\n"
        self.assertEqual(
            inventory_presenter.present(report), "**I found 1 thing to look at:**\n\n**A thing**\nFix it.\n"
        )

    def test_a_multi_line_sentence_is_joined_under_its_headline(self):
        report = "Posture.\n\n1. **A**\n   Pods fall back to the node SA;\n   enable it on the pool.\n"
        self.assertEqual(
            inventory_presenter.present(report),
            "**I found 1 thing to look at:**\n\n**A**\nPods fall back to the node SA; enable it on the pool.\n",
        )

    def test_an_item_with_no_sentence_is_its_headline_alone(self):
        report = "Posture.\n\n1. **A**\n2. **B**\n   Fix it.\n"
        self.assertEqual(inventory_presenter.present(report), "**I found 2 things to look at:**\n\n**A**\n**B**\nFix it.\n")

    def test_a_closing_line_with_a_count_is_kept(self):
        report = TWO_ITEMS + (
            "Also found: 18 more items, tracked in the findings queue.\n\n"
            "The full inventory covers 41 workloads and 22 findings; ask for it.\n"
        )
        out = inventory_presenter.present(report)
        self.assertTrue(out.endswith("\n\nThe full inventory covers 41 workloads and 22 findings; ask for it.\n"), out)
        self.assertNotIn("Also found", out)
        self.assertNotIn("Ask me to see all", out)

    def test_a_roll_up_that_was_the_last_line_becomes_the_ask(self):
        for tail, total in (
            ("Reply 'list' to see the other 3 findings in the full inventory.", 5),
            ("Also found: 18 more items.", 20),
        ):
            with self.subTest(tail=tail):
                out = inventory_presenter.present(TWO_ITEMS + tail + "\n")
                self.assertIn(f"found {total} things to look at", out)
                self.assertTrue(out.endswith(f"\n\nAsk me to see all {total}.\n"), out)
                self.assertNotIn(tail, out)

    def test_a_roll_up_after_a_closing_line_becomes_the_ask(self):
        out = inventory_presenter.present(TWO_ITEMS + "Fixing the first two clears most alerts.\n\nAlso found: 18 more items.\n")
        self.assertTrue(out.endswith("\n\nFixing the first two clears most alerts.\n\nAsk me to see all 20.\n"), out)

    def test_unparseable_reports_are_unchanged(self):
        for report in (
            "",
            "# Report\n\n| Cluster | ... |\n",
            "# Scan\n\n1. **A**\n   x\n",  # no posture
            "Posture.\n\n1. plain item, no bold headline\n",
            "Posture.\n\n1. **A**\n   x\n\n## Another section\n",
            "Posture.\n\n1. **Critical:**\n   x\n",  # a label and nothing to label
            "Posture.\n\n1. **A**\n   x\n\nSee 2. **B** and 3. **C** too.\n",  # items inside a paragraph
        ):
            with self.subTest(report=report):
                self.assertEqual(inventory_presenter.present(report), report)



#: tm-7's probe: two listed items, the first critical, so both show.
TWO_ITEMS = """# Scan

I scanned 3 clusters and 41 workloads. Mostly healthy.

1. **critical: seeded-b admits privileged pods.** Enforce baseline.
2. **Default SA is cluster-admin on seeded-c.** Remove the binding.

"""

PROBE_ITEMS = """1. **[HIGH] No PDB on payments/api in prod**
   A node upgrade drains every replica at once; add a PodDisruptionBudget.

2. **[MEDIUM] No readinessProbe on web in dev**
   Traffic reaches pods before they are ready; add a readinessProbe.

3. **[LOW] Unpinned image tag on batch/cron in dev**
   A re-pull can change the code; pin the digest.
"""

GAP_POSTURE = (
    "2 clusters could not be scanned (permission denied); across the other 5 clusters and 41 workloads posture is fair."
)


#: Every roll-up line a review has labelled, with the card's total under two items.
CARD_TOTALS = (
    ('These 2 findings are the only ones; ask me for more detail.', 2),
    ('Fixing these 2 issues first will also clear most alerts.', 2),
    ('Both 2 findings are in prod; the other clusters are clean.', 2),
    ('I found 2 issues; ask me for more detail.', 2),
    ('Those are 2 findings worth fixing; I can also open a PR.', 2),
    ('2 issues remain open from last week.', 2),
    ('Ask me for more detail on either of the 2 findings.', 2),
    ('Each of the 2 findings also has a runbook.', 2),
    ('4 more nodes: failed readiness probes.', 6),
    ('3 more projects: failed org policy checks.', 5),
    ('Also found: 4 nodes: forbidden hostPath mounts.', 6),
    ('Other findings: 6 projects: no credentials rotated in 90 days.', 8),
    ('Other findings: 3 nodes failed health checks.', 5),
    ('4 more nodes failed readiness probes.', 6),
    ('12 more: 4 timed out jobs', 14),
    ('I also flagged 18 issues, all low.', 20),
    ('18 issues also need attention.', 20),
    ('Plus, 18 issues of low severity.', 20),
    ('18 findings remain, mostly low; see the full report for more.', 20),
    ('18 high-priority findings remain: 2 critical, 16 high.', 20),
    ('Also found: 18 high-priority findings: 2 critical, 16 high.', 20),
    ('There are 4 issues in 2 namespaces: 3 high, 1 low.', 6),
    ('18 more in prod clusters, all low.', 20),
    ('18 more node pool findings, all low.', 20),
    ('12 more cluster-wide issues, all low.', 14),
    ('Other findings: 18 items, mostly low.', 20),
    ('Lower-priority findings: 18.', 20),
    ('18 more across the clusters, including 9 minor findings.', 20),
    ('18 more in other clusters, including 9 minor findings.', 20),
    ('Also found: 18 across the clusters, including 9 minor findings.', 20),
    ('Other issues: 2 clusters could not be scanned (permission denied).', 2),
    ('Also found: 2 clusters could not be scanned (permission denied).', 2),
    ('2 more clusters: permission denied.', 2),
    ('2 more namespaces: unreachable.', 2),
    ('Also found: 2 projects unreachable.', 2),
    ('2 more clusters timed out after 60s.', 2),
    ('2 more clusters failed with a 403.', 2),
    ('2 more clusters failed during the scan.', 2),
    ('2 more clusters failed - permission denied.', 2),
    ('2 more clusters failed: no credentials.', 2),
    ('2 more clusters timed out and were skipped.', 2),
    ('2 more clusters: permission denied for the scanner SA.', 2),
    ('Also found: 18 more items; seeded-d was skipped (no credentials).', 20),
    ('Also: 2 clusters could not be scanned (permission denied).', 2),
    ('2 more clusters could not be scanned (permission denied).', 2),
    ('Also found: 18 more items: 2 high, 16 lower-priority findings, tracked in the findings queue.', 20),
    ('Also found: 18 more items (2 high, 16 medium)', 20),
    ('Also found: 18 (2 high, 16 medium).', 20),
    ('Plus 18 lower-priority findings: 2 high, 16 medium.', 20),
    ('Also found: 2 high, 1 medium and 1 low finding.', 6),
    ('Also found: 3 high, 1 medium, 2 low.', 8),
    ('Also found: 18 items: 2 high, 16 low.', 20),
    ('I also found 18 findings remain: 2 high, 16 low.', 20),
    ('Also found: 2 high-priority and 5 low findings.', 9),
    ('Also found: 2 high-priority findings and 5 low.', 9),
    ('Plus 2 high-severity, 3 medium-severity and 4 low-severity findings.', 11),
    ('4 more nodes are not ready.', 6),
    ('4 more nodes are still on 1.27.', 6),
    ('4 more nodes remain unpatched.', 6),
    ('4 more nodes failed to drain.', 6),
    ('2 more projects are still using default service accounts.', 4),
    ('2 more clusters can be upgraded.', 4),
    ('2 more clusters cannot enforce network policy.', 4),
    ('3 more namespaces can be reached from the internet.', 5),
    ("4 more nodes aren't on the latest patch.", 6),
    ("2 more clusters don't enforce Binary Authorization.", 4),
    ('2 more clusters (prod, staging) allow privileged pods.', 4),
    ('4 more nodes returned errors.', 6),
    ('4 more nodes errored.', 6),
    ('2 more clusters returned errors on 3 workloads.', 4),
    ('2 more clusters skipped node auto-repair.', 4),
    ("2 more clusters weren't scanned.", 2),
    ("2 more clusters didn't respond.", 2),
    ('2 more clusters did not respond.', 2),
    ("2 more clusters aren't reachable.", 2),
    ("2 more clusters can't be reached.", 2),
    ('2 more clusters cannot be scanned.', 2),
    ('1 more namespace is still syncing.', 2),
    ('2 more clusters remain unreachable.', 2),
    ('2 more clusters (seeded-d, seeded-e) timed out.', 2),
    ('2 more clusters, seeded-d and seeded-e, timed out.', 2),
    ('2 more clusters (seeded-d, seeded-e) could not be scanned.', 2),
    ('2 more clusters returned errors.', 2),
    ('2 more projects errored.', 2),
    ('Other findings: 3 failed pods.', 5),
    ('Also found: 5 skipped checks.', 7),
    ('12 more: 4 timed out jobs.', 14),
    ('Lower-priority findings: 2 failed probes.', 4),
    ('Other findings: 3 nodes: failed health checks.', 5),
    ('4 more nodes: unreachable kubelet.', 6),
    ('Other findings: 18.', 20),
    ('Other findings: 18 (3 clusters: permission denied).', 20),
    ('Also found: 7, and 2 clusters: unreachable.', 9),
    ('5 more clusters: forbidden.', 2),
    ('5 more clusters : skipped', 2),
    ('Additional findings: 3 clusters: failed', 2),
    ('4 more nodes failed.', 2),
    ('2 more clusters failed to scan.', 2),
    ('2 more clusters: permission denied (missing roles/container.viewer).', 2),
    ('2 more clusters: unreachable, skipped.', 2),
    ('Also found: 2 clusters failed.', 2),
    ('Other findings: 2 clusters timed out.', 2),
    ('2 more clusters: timed out after 60s.', 2),
    ('18 more across 3 clusters, including 9 minor findings.', 20),
    ("2 more clusters couldn't be reached.", 2),
    ('2 more clusters could not connect.', 2),
    ('2 more clusters were not reachable.', 2),
    ('2 more clusters are still being set up; ask me later.', 2),
    ('2 more clusters were skipped.', 2),
    ('2 more clusters skipped.', 2),
    ('2 more clusters are offline.', 2),
    ('2 more clusters are pending.', 2),
    ('2 more clusters still syncing.', 2),
    ('2 more clusters failed to authenticate.', 2),
    ('Also found: 2 clusters could not be scanned.', 2),
    ('Other issues: 2 namespaces were skipped.', 2),
    ('4 more nodes are not patched.', 6),
    ('2 more clusters are not running the latest version.', 4),
    ('The 2 issues above are the most urgent; the rest can wait.', 2),
    ('Ask me for more detail on either finding.', 2),
    ('I found 22 findings in total.', 22),
    ('Want more? See all 22 findings in the thread.', 22),
    ('There are 18 lower-priority issues remaining.', 20),
    ('I found 2 issues; ask me for more on either.', 2),
    ('I found 2 findings, and I can also open a PR for either.', 2),
    ('Fix 2 issues now and the remaining alerts should clear.', 2),
    ('These are the 2 issues that remain.', 2),
    ('I can also explain any of the 2 findings above.', 2),
    ('Want more? I found 2 findings across 3 clusters.', 2),
    ('I scanned 3 clusters and found 2 issues; reply for more.', 2),
    ('Those 2 issues also affect staging.', 2),
    ('2 more clusters errored.', 2),
    ('2 more projects can impersonate the owner SA.', 4),
    ('4 more nodes are not running the latest image.', 6),
    ('2 more clusters are still on a deprecated release channel.', 4),
    ('2 more clusters do not have Workload Identity.', 4),
    ('2 more clusters are not using private nodes.', 4),
    ('3 more namespaces have no network policy.', 5),
    ('2 more clusters still allow legacy ABAC.', 4),
    ('4 more nodes remain on containerd 1.6.', 6),
    ('2 more projects cannot rotate keys automatically.', 4),
    ('These 2 findings are the only ones; there are no other findings.', 2),
    ('Both findings above need a fix; ask me for detail.', 2),
    ('The 2 findings above are all I found.', 2),
    ('2 more clusters are not reachable.', 2),
    ('2 more clusters could not be scanned.', 2),
    ('2 more clusters cannot be reached.', 2),
    ('2 more clusters were not scanned.', 2),
    ('2 more clusters are still syncing.', 2),
    ('2 more clusters are still provisioning.', 2),
    ('2 more clusters remain unscanned.', 2),
    ('2 more clusters are not accessible.', 2),
    ('2 more clusters were still unreachable.', 2),
    ('2 more clusters could not be reached (permission denied).', 2),
    ('2 more clusters are unreachable.', 2),
    ('2 more clusters are still pending.', 2),
    ('Plus, 18 low-severity issues.', 20),
    ('2 more clusters are not reachable from the internet, as intended.', 2),
    ('Plus, I checked 3 clusters.', 2),
    ('2 more clusters are not yet scanned.', 2),
    ('2 more clusters could not yet be scanned.', 2),
    ('2 more clusters could not currently be reached.', 2),
    ('2 more clusters were not able to be scanned.', 2),
    ('2 more clusters could not be contacted.', 2),
    ('2 more clusters could not be read.', 2),
    ('2 more clusters remain to be scanned.', 2),
    ('2 more clusters still need to be scanned.', 2),
    ('2 more clusters failed to complete the scan.', 2),
    ('2 more clusters failed to return results.', 2),
    ('2 more clusters are still initializing.', 2),
    ('2 more clusters were not checked.', 2),
    ('Your top 2 issues also affect prod.', 2),
    ('These top 2 issues also block the upgrade.', 2),
    ('The same 2 issues also appear in staging.', 2),
    ('The above 2 findings also apply to staging.', 2),
    ('I can also fix 2 issues in one PR.', 2),
    ('After these fixes, 2 issues remain.', 2),
    ('Those 2 issues also need a fix.', 2),
    ('All 2 issues also apply to prod.', 2),
    ('Both of the 2 issues also block upgrades.', 2),
    ('I also found 18 issues worth a look.', 20),
    ('4 more nodes are unavailable for scheduling.', 6),
    ('4 more nodes are still not able to schedule pods.', 6),
    ('2 more clusters are not yet upgraded.', 4),
    ('2 more clusters are not yet on 1.30.', 4),
    ('2 more clusters still need to be upgraded.', 4),
    ('2 more clusters remain to be patched.', 4),
    ('4 more nodes could not currently schedule GPU pods.', 6),
    ('2 more clusters failed to complete the upgrade.', 4),
    ('I can also fix 18 more issues if you want.', 20),
    ('Your 18 other issues also need a look.', 20),
    ('Fixing these 2 issues also clears most alerts.', 2),
    ('Fixing those 2 issues also unblocks the upgrade.', 2),
    ('Both 2 issues also affect prod.', 2),
    ('Fixing the 2 issues also clears most alerts.', 2),
    ('Your 2 issues also affect prod.', 2),
    ('My top 2 issues also affect prod.', 2),
    ('My first 2 issues also block the upgrade.', 2),
    ('Same 2 issues also show up in staging.', 2),
    ('Above 2 findings also apply to staging.', 2),
    ('With these fixes in, 2 issues remain.', 2),
    ('With those fixes in, 2 issues remain.', 2),
    ('After the fixes land, 2 issues remain.', 2),
    ('Once you apply them, 2 issues remain.', 2),
    ('I could also fix 2 issues in one PR.', 2),
    ('I will also fix 2 issues in one PR.', 2),
    ('I would also fix 2 issues in one PR.', 2),
    ('I may also fix 2 issues in one PR.', 2),
    ('I might also fix 2 issues in one PR.', 2),
    ('I should also fix 2 issues in one PR.', 2),
    ('In addition to these, I also flagged 18 issues.', 20),
    ('In addition to those, I also flagged 18 issues.', 20),
    ('On top of these, I also flagged 18 issues.', 20),
    ('Besides these, the scan also flagged 18 issues.', 20),
    ('Besides these, 18 issues also need attention.', 20),
    ('Besides these, 18 findings remain.', 20),
    ('Outside of these, I also noted 18 issues.', 20),
    ('After scanning the fleet, I also flagged 18 issues of lower severity.', 20),
    ('After the scan, I also flagged 18 issues of lower severity.', 20),
    ('Above and beyond these, there are 18 issues remaining.', 20),
    ('I will also mention 18 issues in staging.', 20),
    ('I would also highlight 18 issues in staging.', 20),
    ('Once you apply these, 2 issues remain.', 2),
    ('These 2 issues also block the upgrade.', 2),
    ('2 more clusters returned permission errors.', 2),
    ('2 more clusters returned 403 errors.', 2),
    ('2 more clusters were excluded from the scan.', 2),
    ('2 more clusters were not in scope.', 2),
    ('Plus, I found 18 issues.', 20),
    ('Plus, the scan surfaced 18 issues.', 20),
    ('The scan also turned up 18 issues.', 20),
    ('Should I also fix 2 issues now?', 2),
    ('I also want to fix 2 issues in one PR.', 2),
    ('We could also open 2 issues for tracking.', 2),
    ('I may also patch 2 issues tonight.', 2),
    ('Shall I also resolve 2 issues now?', 2),
    ('Plus, I also found 18 issues.', 20),
    ('I will also flag 18 issues in staging.', 20),
    ('4 more nodes returned 403 errors.', 6),
    ('2 more clusters returned 403 errors on 3 workloads.', 4),
    ('4 more nodes were excluded from the scan.', 2),
    ('2 more namespaces were out of scope.', 2),
    ('After these fixes, I also flagged 18 issues.', 20),
    ('Apart from these, 18 issues also need a look.', 20),
    ('Once the scan finished, I also flagged 18 issues.', 20),
    ('With the scan done, I also flagged 18 issues.', 20),
    ('Those 2 issues also affect staging.', 2),
    ('Both of these 2 issues also affect staging.', 2),
    ('After you fix these, 2 issues remain.', 2),
    ('I also flagged 18 issues (low severity).', 20),
    ('The scan also turned up these 2 issues.', 2),
    ('Should I also address 2 issues now?', 2),
    ('I would also open 2 issues for tracking.', 2),
    ('I might also remediate 2 issues tonight.', 2),
    ('I will also handle 2 issues in one PR.', 2),
    ('I can also mention 2 issues in the summary.', 2),
    ('After patching these, 2 issues remain.', 2),
    ('Once these are resolved, 2 issues remain.', 2),
    ('After remediating these, 2 issues remain.', 2),
    ('Once these are merged, 2 issues remain.', 2),
    ('I also recommend fixing 2 issues before the upgrade.', 2),
    ("I'd also prioritize fixing 2 issues first.", 2),
    ('If you want, I can open 2 issues: one per cluster.', 2),
    ('I can also file 2 problems: one for each namespace.', 2),
    ('Shall I raise 2 items: the quota and the PDB?', 2),
    ('Would you like me to go ahead and open 2 issues: one per cluster?', 2),
    ('I can open up 2 issues: one per cluster.', 2),
    ('If you want I can file tickets for 2 issues: one per cluster.', 2),
    ('I can see 18 issues: the rest are in the ledger.', 20),
    ('You can find 18 issues: in the ledger.', 20),
    ('I can fix that and 18 issues: will clear.', 20),
    ('I can patch it but 18 issues: still remain.', 20),
    ('I can fix this so 18 issues: clear at once.', 20),
    ('Also note that 2 issues block the upgrade.', 2),
    ('I also suggest fixing 2 issues first.', 2),
    ('I also recommend fixing 18 more issues.', 20),
    ('Plus, I spotted 18 issues in staging.', 20),
    ('Of these, 2 issues also block the upgrade.', 2),
    ('Among these, 2 issues also affect prod.', 2),
    ('Of those, 2 issues also need a fix.', 2),
    ('When these are fixed, 2 issues remain.', 2),
    ('If you apply these, 2 issues remain.', 2),
    ('Once these are fixed 2 issues remain.', 2),
    ("I'll also fix 2 issues in one PR.", 2),
    ("I'd also fix 2 issues first.", 2),
    ('Want me to also fix 2 issues?', 2),
    ('Would you like me to also patch 2 issues?', 2),
    ('2 more clusters returned 500 errors.', 2),
    ('2 more clusters returned PERMISSION_DENIED errors.', 2),
    ('2 more clusters returned 403 Forbidden errors.', 2),
    ('2 more clusters returned server errors.', 2),
    ('2 more clusters were not part of this scan.', 2),
    ('Besides those 2 issues, 18 findings remain.', 20),
    ('4 more nodes returned 503 errors.', 6),
    ('When you have time, I also flagged 18 issues.', 20),
    ("I'll also open PRs for 18 issues.", 20),
    ('I can also fix 18 issues tonight.', 20),
    ('Want me to also fix 18 issues?', 20),
    ('2 more clusters returned 500 internal server errors.', 2),
    ('4 more nodes returned 500 errors.', 6),
    ('If these are patched 2 issues remain.', 2),
    ('The scan also turned up 2 issues in staging.', 4),
    ('Plus, the scan found 2 issues in staging.', 4),
    ('Plus, the scan flagged 2 issues in staging.', 4),
    ('Plus, the scan surfaced 2 issues in staging.', 4),
    ('Plus, the scan spotted 2 issues in staging.', 4),
    ('Plus, the scan noticed 2 issues in staging.', 4),
    ('Plus, the scan noted 2 issues in staging.', 4),
    ('Plus, the scan saw 2 issues in staging.', 4),
    ('Plus, the scan detected 2 issues in staging.', 4),
    ('Plus, the scan identified 2 issues in staging.', 4),
    ('Plus, the scan uncovered 2 issues in staging.', 4),
    ('On top of these, 18 issues also need attention.', 20),
    ('On top of these, 3 issues also need attention.', 5),
    ('Outside of these, 18 issues also need attention.', 20),
    ('Regardless of those, 18 issues also need attention.', 20),
    ('Also, I discovered 2 issues in staging.', 4),
    ('Also, the scan reported 2 issues in staging.', 4),
    ('I also just discovered 2 issues in dev.', 4),
    ('Plus, the scan reported 2 issues in dev.', 4),
    ('Also, the scan revealed 2 issues in dev.', 4),
    ('Also, I observed 2 issues in dev.', 4),
    ('Also, there are 2 issues in staging.', 4),
    ('Also, there were 2 issues in dev.', 4),
    ('Also, staging has 2 issues.', 4),
    ('Plus, staging has 2 issues.', 4),
    ('We also ran into 2 issues on staging.', 4),
    ('We also picked up 2 issues in staging.', 4),
    ('I also came across 2 issues.', 4),
    ('When I patched the nodes, 18 issues also surfaced.', 20),
    ('If you fix these, 18 issues remain.', 20),
    ('2 more clusters returned 500 errors to clients.', 4),
    ('2 more clusters returned 502 errors during the canary.', 4),
    ('Also found: those 4 high and 14 low.', 20),
    ('Also found: 4 high and those 14 low.', 20),
    ('2 more clusters returned 403 errors during the scan.', 2),
    ('Plus, the scan caught 2 issues in dev.', 4),
    ('Plus, I counted 2 issues in dev.', 4),
    ('Plus, the scan shows 2 issues in dev.', 4),
    ('Plus, the scan showed 2 issues in dev.', 4),
    ('Also, I see 2 issues in dev.', 4),
    ('Also, we hit 2 issues in dev.', 4),
    ('Also, there is 1 issue in dev.', 3),
    ('Also, we have 2 issues in dev.', 4),
    ('On top of these, 2 issues also need attention.', 4),
    ('Also, fixing these closes 18 issues downstream.', 2),
    ('Once these are fixed, 18 issues also close.', 2),
    ('After these fixes, 18 findings also clear in staging.', 2),
    ('With these patches in, 18 issues also go away.', 2),
    ('After merging these, 18 issues also resolve.', 2),
    ('Also, the 18 issues in the raw output collapse into these 2.', 2),
    ('Also, these 18 findings roll up into the 2 above.', 2),
    ("Also, the 30 issues from last week's scan are all closed.", 2),
    ('Plus, your 40 open issues in Jira are untouched.', 2),
    ('Also, the top 10 issues from the last scan are fixed.', 2),
    ('Also, the same 18 issues showed up last week.', 2),
    ('2 more clusters returned 403 Forbidden.', 2),
    ('2 more clusters returned 403.', 2),
    ('2 more clusters returned 401 Unauthorized.', 2),
    ('2 more clusters returned 404 Not Found.', 2),
    ('2 more clusters returned PERMISSION_DENIED.', 2),
    ('2 more clusters returned a 403 Forbidden.', 2),
    ('2 more clusters returned 503 Service Unavailable.', 2),
    ('2 more clusters returned 500 Internal Server Error.', 2),
    ('2 more clusters returned 403 Forbidden, so I skipped them.', 2),
    ('2 more clusters returned 403 Forbidden (missing container.viewer).', 2),
    ('2 more clusters returned 403 Forbidden; grant the scanner SA container.viewer.', 2),
    ('2 more projects returned PERMISSION_DENIED.', 2),
    ('2 more namespaces returned 403 Forbidden.', 2),
    ('2 more clusters (seeded-d, seeded-e) returned 403 Forbidden.', 2),
    ('2 more clusters return 403 Forbidden to anonymous users.', 4),
    ('2 more clusters returned 403 Forbidden to anonymous users.', 4),
    ('2 more clusters returned 403 Forbidden on /metrics.', 4),
    ('2 more clusters returned 503 to their clients.', 4),
    ('2 more clusters return 401 for unauthenticated requests.', 4),
    ('2 more clusters returned 403 for the kubelet read-only port.', 4),
    ('2 more clusters expose a 403 Forbidden page publicly.', 4),
    ('4 more nodes returned 403 Forbidden.', 6),
    ('2 more clusters returned 404 for the health endpoint.', 4),
    ('2 more clusters returned 403 Forbidden for the dashboard.', 4),
    ('2 more clusters returned 403 Forbidden while serving the dashboard.', 4),
    ('2 more clusters returned 403 to anonymous users.', 4),
    ('2 more clusters returned 403 to their clients.', 4),
    ('2 more clusters returned 401 from the metadata server.', 4),
    ('2 more clusters returned 404 in the ingress logs.', 4),
    ('2 more clusters returned 403 via the public endpoint.', 4),
    ('2 more clusters returned PERMISSION_DENIED to workloads.', 4),
    ('2 more clusters returned 403 Forbidden — skipped.', 2),
    ('Also found: 2 clusters returned 403 Forbidden.', 2),
)

#: Labelled lines the card still totals wrongly: (line, today's total, the right one).
#: A fix moves the line to :data:`CARD_TOTALS` with the right total.
KNOWN_WRONG_TOTALS = (
    ("Also, across the fleet, I flagged 18 issues.", 2, 20),
    # An offer's modal ("would you like me to") rules out any count the list can hold.
    ("Would you like me to also look at 2 issues in staging?", 2, 4),
    # Only a modal of "fix" or the like makes an offer, so "share" leaves the 20 a roll-up.
    ("I can also share 20 findings in a CSV.", 22, 2),
    # An "after", "once" or "with" fix clause makes any count the listed ones, as before edc23cf6.
    ("After these fixes, 18 issues remain.", 2, 20),
    ("Once I applied the policy, 18 issues also surfaced.", 2, 20),
    ("With patches pending, 18 issues also need attention.", 2, 20),
    ("After merging the config, 18 findings also appeared.", 2, 20),
    # A status is a gap only where its clause ends, so "for lack of" keeps it a finding.
    ("2 more clusters returned 403 Forbidden for lack of credentials.", 4, 2),
    # A server's error is a gap only where its clause ends, so "during the scan" keeps it a finding.
    ("2 more clusters returned 500 errors during the scan.", 4, 2),
    # An offer before a colon is read only from OFFER_LEAD's phrasings and filler words,
    # so these read as a roll-up and add to the listed findings.
    ("I'd like to open 2 issues: one per cluster.", 4, 2),
    ("I could also go ahead and open 2 issues: one per cluster.", 4, 2),
    ("I can open tickets for each of the 2 issues: one per cluster.", 4, 2),
    ("I can fix both of these 2 issues: one PR each.", 4, 2),
    ("I can fix the remaining 2 issues: one PR each.", 4, 2),
    ("I can open an issue for these 2 issues: one per cluster.", 4, 2),
    # Only clusters, namespaces and projects return a scan's errors, so a node's are a finding.
    ("2 more nodes returned 500 errors.", 4, 2),
    # "there are" and "shows" after "also" count as found whatever the count.
    ("Also, there are 2 issues I'd fix first.", 4, 2),
    ("Also, the dashboard shows 2 issues as critical.", 4, 2),
    # One word after "also" needs no verb, so "see" counts as found.
    ("I also see 2 issues in the list.", 4, 2),
    # "Of these," picks out the listed ones only at a clause's start after a comma or full stop.
    ("The scan is done \u2014 of these, 2 issues also block the upgrade.", 4, 2),
    ("Fix the list first, and of these, 2 issues also block the upgrade.", 4, 2),
    ("Out of these, 2 issues also block the upgrade.", 4, 2),
)


class BlocksTest(unittest.TestCase):
    def _types(self, blocks):
        return [block["type"] for block in blocks]

    def _buttons(self, blocks):
        return [b["text"]["text"] for b in blocks[-1]["elements"]]

    def _headline(self, report):
        # (headline, note), or (headline, note, detail) when the posture names a gap.
        headline, note, detail = inventory_presenter.card_headline(inventory_presenter._shape(report))
        return (headline, note, detail) if detail else (headline, note)

    def _probe(self, posture, rollup="Also found: 19 more."):
        # tm-7's probe: three listed items under a model-plausible posture and roll-up.
        return f"# GKE Environment Scan\n\n{posture}\n\n{PROBE_ITEMS}\n{rollup}\n\nAsk me for the full inventory.\n"

    def test_a_nul_in_the_posture_neither_raises_nor_swaps_a_parenthetical(self):
        for posture, gap in (
            ("I scanned 3 clusters and seeded-a (x) was \x005\x00 unreachable.", "seeded-a (x) was 5 unreachable."),
            (
                "I scanned 3 clusters; seeded-c \x000\x00 could not be scanned (permission denied).",
                "seeded-c 0 could not be scanned (permission denied).",
            ),
        ):
            with self.subTest(posture=posture):
                report = "# Scan\n\n" + posture + "\n\n1. **A.** b\n2. **B.** c\n"
                self.assertEqual(
                    inventory_presenter.present(report),
                    "**I scanned 3 clusters and found 2 things to look at.**\n" + gap + "\n\n**A.**\nb\n**B.**\nc\n",
                )
                blocks, _ = inventory_presenter.blocks(report)
                texts = [part["text"] for part in blocks[0]["elements"][1]["elements"]]
                self.assertEqual(texts, [gap])

    def test_mock_v3a_shape(self):
        blocks, text = inventory_presenter.blocks(REPORT)
        self.assertEqual(self._types(blocks), ["rich_text", "divider", "rich_text", "divider", "actions"])
        head = blocks[0]["elements"][0]["elements"]
        self.assertEqual(
            head[0],
            {
                "type": "text",
                "text": "I scanned 3 clusters and 41 workloads and found 22 things to look at.",
                "style": {"bold": True},
            },
        )
        self.assertEqual(head[1]["text"], " Two are worth fixing first:")
        rows = blocks[2]["elements"]
        # No count above the rows: the first section is the first finding.
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["elements"][0], {"type": "text", "text": "critical", "style": {"code": True}})
        buttons = blocks[4]["elements"]
        self.assertEqual([b["action_id"] for b in buttons], ["kage_inventory.choice.0", "kage_inventory.choice.1"])
        self.assertEqual(buttons[0]["style"], "primary")
        # Four listed plus the roll-up's "18 more".
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 22"])
        self.assertTrue(
            text.startswith(
                "*I scanned 3 clusters and 41 workloads and found 22 things to look at. Two are worth fixing first:*\n"
                "`critical` seeded-b"
            )
        )

    def test_the_total_is_the_only_count_on_the_card(self):
        blocks, _ = inventory_presenter.blocks(REPORT)
        card = str(blocks)
        for gone in ("1 critical", "more worth a look", "Also found", "18", "Posture is mostly healthy", "full inventory"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, card)
        self.assertNotIn("container", self._types(blocks))

    def test_a_posture_total_counts_only_with_no_roll_up(self):
        report = REPORT.replace("41 workloads.", "41 workloads, 23 findings.")
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 22"])
        report = report.replace("Also found: 18 more items, tracked in the findings queue — ask for the full list.\n\n", "")
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 23"])
        self.assertIn("found 23 things", self._headline(report)[0])

    def test_a_roll_ups_stated_total_is_not_added_to_its_breakdown(self):
        for rollup in (
            "Also found: 18 more items: 2 high, 16 lower-priority findings, tracked in the findings queue.",
            "Also found: 18 more items (2 high, 16 medium)",
            "Also found: 18 (2 high, 16 medium).",
            "Plus 18 lower-priority findings: 2 high, 16 medium.",
        ):
            with self.subTest(rollup=rollup):
                report = TWO_ITEMS + rollup + "\n"
                blocks, text = inventory_presenter.blocks(report)
                self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 20"])
                self.assertIn("found 20 things to look at", text)
                self.assertIn("found 20 things to look at", inventory_presenter.present(report))

    def test_a_counted_closing_line_with_no_roll_up_is_kept_and_not_counted(self):
        for closing in (
            "The full inventory covers 41 workloads and 22 findings; ask for it.",
            "Most of the 20 findings are low risk; ask for the full list.",
            "The full inventory covers 41 workloads and 22 findings; high availability is fine.",
        ):
            with self.subTest(closing=closing):
                report = TWO_ITEMS.replace("Mostly healthy.", "Mostly healthy; 20 findings.") + closing + "\n"
                blocks, _ = inventory_presenter.blocks(report)
                self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 20"])
                presented = inventory_presenter.present(report)
                self.assertIn("found 20 things to look at", presented)
                self.assertIn(closing, presented)
                self.assertNotIn("Ask me to see all", presented)

    def test_a_closing_line_restating_the_shown_findings_is_not_a_roll_up(self):
        for closing in (
            "These 2 findings are the only ones; ask me for more detail.",
            "Fixing these 2 issues first will also clear most alerts.",
            "Both 2 findings are in prod; the other clusters are clean.",
            "I found 2 issues; ask me for more detail.",
            "Those are 2 findings worth fixing; I can also open a PR.",
            "2 issues remain open from last week.",
            "Ask me for more detail on either of the 2 findings.",
            "Each of the 2 findings also has a runbook.",
        ):
            with self.subTest(closing=closing):
                report = TWO_ITEMS + closing + "\n"
                blocks, _ = inventory_presenter.blocks(report)
                self.assertEqual(self._buttons(blocks), ["Fix the first one"])
                presented = inventory_presenter.present(report)
                self.assertIn("found 2 things to look at", presented)
                self.assertTrue(presented.endswith("\n\n" + closing + "\n"), presented)

    def test_a_free_form_roll_up_keeps_its_count(self):
        for rollup in (
            "18 more in prod clusters, all low.",
            "18 more node pool findings, all low.",
            "12 more cluster-wide issues, all low.",
            "Other findings: 18 items, mostly low.",
            "Lower-priority findings: 18.",
            "18 more across the clusters, including 9 minor findings.",
            "18 more in other clusters, including 9 minor findings.",
            "Also found: 18 across the clusters, including 9 minor findings.",
        ):
            with self.subTest(rollup=rollup):
                blocks, _ = inventory_presenter.blocks(TWO_ITEMS + rollup + "\n")
                total = 14 if rollup.startswith("12") else 20
                self.assertEqual(self._buttons(blocks), ["Fix the first one", f"See all {total}"])

    def test_every_labelled_roll_up_line_gives_its_total(self):
        for line, total in CARD_TOTALS:
            with self.subTest(line=line):
                self.assertEqual(inventory_presenter._shape(TWO_ITEMS + line + "\n").total, total)

    def test_a_known_wrong_total_is_still_wrong(self):
        # A line here that starts giving the right total belongs in CARD_TOTALS.
        for line, today, right in KNOWN_WRONG_TOTALS:
            with self.subTest(line=line):
                self.assertNotEqual(today, right)
                self.assertEqual(inventory_presenter._shape(TWO_ITEMS + line + "\n").total, today)

    def test_a_gap_after_a_roll_up_word_counts_nothing(self):
        for closing in (
            "Other issues: 2 clusters could not be scanned (permission denied).",
            "Also found: 2 clusters could not be scanned (permission denied).",
            "2 more clusters: permission denied.",
            "2 more namespaces: unreachable.",
            "Also found: 2 projects unreachable.",
            "2 more clusters timed out after 60s.",
            "2 more clusters failed with a 403.",
            "2 more clusters failed during the scan.",
            "2 more clusters failed - permission denied.",
            "2 more clusters failed: no credentials.",
            "2 more clusters timed out and were skipped.",
            "2 more clusters: permission denied for the scanner SA.",
        ):
            with self.subTest(closing=closing):
                blocks, _ = inventory_presenter.blocks(TWO_ITEMS + closing + "\n")
                self.assertEqual(self._buttons(blocks), ["Fix the first one"])

    def test_a_failure_that_starts_a_finding_is_counted(self):
        for rollup, total in (
            ("4 more nodes: failed readiness probes.", 6),
            ("3 more projects: failed org policy checks.", 5),
            ("Also found: 4 nodes: forbidden hostPath mounts.", 6),
            ("Other findings: 6 projects: no credentials rotated in 90 days.", 8),
            ("Other findings: 3 nodes failed health checks.", 5),
            ("4 more nodes failed readiness probes.", 6),
            ("12 more: 4 timed out jobs", 14),
        ):
            with self.subTest(rollup=rollup):
                blocks, _ = inventory_presenter.blocks(TWO_ITEMS + rollup + "\n")
                self.assertEqual(self._buttons(blocks), ["Fix the first one", f"See all {total}"])

    def test_a_state_that_is_a_finding_is_counted(self):
        for rollup, total in (
            ("4 more nodes are not ready.", 6),
            ("4 more nodes are still on 1.27.", 6),
            ("4 more nodes remain unpatched.", 6),
            ("4 more nodes failed to drain.", 6),
            ("2 more projects are still using default service accounts.", 4),
            ("2 more clusters can be upgraded.", 4),
            ("2 more clusters cannot enforce network policy.", 4),
            ("3 more namespaces can be reached from the internet.", 5),
            ("4 more nodes aren't on the latest patch.", 6),
            ("2 more clusters don't enforce Binary Authorization.", 4),
            ("2 more clusters (prod, staging) allow privileged pods.", 4),
            ("4 more nodes returned errors.", 6),
            ("4 more nodes errored.", 6),
            ("2 more clusters returned errors on 3 workloads.", 4),
            ("2 more clusters skipped node auto-repair.", 4),
        ):
            with self.subTest(rollup=rollup):
                blocks, _ = inventory_presenter.blocks(TWO_ITEMS + rollup + "\n")
                self.assertEqual(self._buttons(blocks), ["Fix the first one", f"See all {total}"])

    def test_a_state_that_says_the_scan_missed_it_counts_nothing(self):
        for closing in (
            "2 more clusters weren't scanned.",
            "2 more clusters didn't respond.",
            "2 more clusters did not respond.",
            "2 more clusters aren't reachable.",
            "2 more clusters can't be reached.",
            "2 more clusters cannot be scanned.",
            "1 more namespace is still syncing.",
            "2 more clusters remain unreachable.",
            "2 more clusters (seeded-d, seeded-e) timed out.",
            "2 more clusters, seeded-d and seeded-e, timed out.",
            "2 more clusters (seeded-d, seeded-e) could not be scanned.",
            "2 more clusters returned errors.",
            "2 more projects errored.",
        ):
            with self.subTest(closing=closing):
                blocks, _ = inventory_presenter.blocks(TWO_ITEMS + closing + "\n")
                self.assertEqual(self._buttons(blocks), ["Fix the first one"])

    def test_a_findings_count_beside_a_loose_more_word_is_a_roll_up(self):
        for rollup in (
            "I also flagged 18 issues, all low.",
            "18 issues also need attention.",
            "Plus, 18 issues of low severity.",
            "18 findings remain, mostly low; see the full report for more.",
        ):
            with self.subTest(rollup=rollup):
                blocks, _ = inventory_presenter.blocks(TWO_ITEMS + rollup + "\n")
                self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 20"])

    def test_a_first_term_heading_its_breakdown_is_the_roll_ups_count(self):
        for rollup, total in (
            ("18 high-priority findings remain: 2 critical, 16 high.", 20),
            ("Also found: 18 high-priority findings: 2 critical, 16 high.", 20),
            ("There are 4 issues in 2 namespaces: 3 high, 1 low.", 6),
        ):
            with self.subTest(rollup=rollup):
                blocks, _ = inventory_presenter.blocks(TWO_ITEMS + rollup + "\n")
                self.assertEqual(self._buttons(blocks), ["Fix the first one", f"See all {total}"])

    def test_a_severity_breakdown_is_summed_even_when_its_terms_coincide(self):
        for rollup, total in (
            ("Also found: 2 high, 1 medium and 1 low finding.", 6),
            ("Also found: 3 high, 1 medium, 2 low.", 8),
            ("Also found: 18 items: 2 high, 16 low.", 20),
            ("I also found 18 findings remain: 2 high, 16 low.", 20),
            ("Also found: 2 high-priority and 5 low findings.", 9),
            ("Also found: 2 high-priority findings and 5 low.", 9),
            ("Plus 2 high-severity, 3 medium-severity and 4 low-severity findings.", 11),
        ):
            with self.subTest(rollup=rollup):
                blocks, _ = inventory_presenter.blocks(TWO_ITEMS + rollup + "\n")
                self.assertEqual(self._buttons(blocks), ["Fix the first one", f"See all {total}"])

    def test_the_roll_up_wins_over_an_earlier_counting_paragraph(self):
        report = TWO_ITEMS + "Most need attention: 6 findings touch prod.\n\nAlso found: 12 more.\n"
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 14"])
        presented = inventory_presenter.present(report)
        self.assertIn("Most need attention: 6 findings touch prod.", presented)
        self.assertNotIn("Also found: 12 more.", presented)

    def test_a_larger_roll_up_total_beats_the_posture(self):
        # "3 findings need attention now" counts the shown ones, not the 14 behind them.
        report = "Scanned 2 clusters; 3 findings need attention now.\n\n" + "".join(
            f"{i}. **[critical] problem {i}**\n   x\n" for i in range(1, 4)
        )
        report += "\nAlso found: 14 more items, tracked in the findings queue.\n"
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 17"])
        self.assertEqual(
            self._headline(report),
            ("I scanned 2 clusters and found 17 things to look at.", "Three are worth fixing first:"),
        )

    def test_a_roll_up_without_more_is_counted(self):
        report = REPORT.replace("18 more items", "14 items")
        self.assertNotEqual(report, REPORT)
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 18"])

    def test_every_row_carries_its_sentence_as_a_second_line(self):
        blocks, text = inventory_presenter.blocks(REPORT)
        rows = blocks[2]["elements"]
        self.assertEqual(rows[0]["elements"][-1]["text"], "Any workload can escape to the node; enforce baseline Pod Security.")
        self.assertIn("\nA compromised pod owns the cluster; remove the binding.", text)

    def test_everything_shown_has_no_lead_and_no_see_all(self):
        report = "# Scan\n\nI scanned 2 clusters and 9 workloads.\n\n1. **A (major)**\n   Fix it.\n2. **B**\n   Fix that.\n"
        blocks, text = inventory_presenter.blocks(report)
        self.assertEqual(self._headline(report), ("I scanned 2 clusters and 9 workloads and found 2 things to look at:", ""))
        self.assertEqual(self._buttons(blocks), ["Fix the first one"])
        self.assertTrue(text.startswith("*I scanned 2 clusters and 9 workloads and found 2 things to look at:*\n"))

    def test_one_of_each_is_singular(self):
        report = "I scanned 1 cluster and 1 workload.\n\n1. **A (major)**\n   Fix it.\n"
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._headline(report), ("I scanned 1 cluster and 1 workload and found 1 thing to look at:", ""))
        self.assertEqual(self._buttons(blocks), ["Fix it"])

    def test_one_top_row_with_more_behind_it(self):
        report = "I scanned 4 GKE clusters.\n\n1. **A (critical)**\n   Fix it.\n\nAlso found: 6 more items.\n"
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(
            self._headline(report), ("I scanned 4 clusters and found 7 things to look at.", "One is worth fixing first:")
        )
        self.assertEqual(self._buttons(blocks), ["Fix it", "See all 7"])

    def test_a_quiet_cluster_gets_the_neutral_lead(self):
        report = "I scanned three clusters and 12 workloads. All quiet.\n\n1. **A (minor)**\n   x\n2. **B**\n   y\n"
        report += "\nAlso found: 3 more items.\n"
        self.assertEqual(
            self._headline(report),
            ("I scanned 3 clusters and 12 workloads and found 5 things to look at.", "Start with these two:"),
        )

    def test_a_posture_with_no_counts_says_only_what_was_found(self):
        report = "Posture.\n\n1. **A (major)**\n   x\n2. **B**\n   y\n3. **C**\n   z\n"
        self.assertEqual(self._headline(report), ("I found 3 things to look at.", "Two are worth fixing first:"))

    def test_the_primary_button_names_the_first_finding_in_its_value(self):
        # The label is what the card shows; the value is the turn a session with no thread context reads.
        blocks, _ = inventory_presenter.blocks(REPORT)
        primary = blocks[-1]["elements"][0]
        self.assertEqual(
            (primary["text"]["text"], primary["value"]),
            ("Fix the first one", "Fix the first one: seeded-b and seeded-c admit privileged pods"),
        )
        self.assertIn("seeded-b and seeded-c admit privileged pods", str(blocks[2]))

    def test_a_single_row_names_it_under_fix_it(self):
        report = "# Scan\n\nI scanned 1 cluster.\n\n1. **`kube-system` has no **NetworkPolicy****\n   Add one.\n"
        blocks, _ = inventory_presenter.blocks(report)
        primary = blocks[-1]["elements"][0]
        self.assertEqual((primary["text"]["text"], primary["value"]), ("Fix it", "Fix it: kube-system has no NetworkPolicy"))

    def test_the_value_names_the_row_as_the_card_shows_it(self):
        report = "# Scan\n\nI scanned 1 cluster.\n\n1. **On *seeded-b*, _privileged_ ~pods~ run**\n   Fix.\n"
        blocks, _ = inventory_presenter.blocks(report)
        value = blocks[-1]["elements"][0]["value"]
        self.assertEqual(value, "Fix it: On seeded-b, privileged pods run")
        shown = ["".join(e["text"] for e in section["elements"]) for section in blocks[2]["elements"]]
        self.assertTrue(any(value.removeprefix("Fix it: ") in line for line in shown), shown)

    def test_the_value_is_the_first_row_exactly_as_the_card_shows_it(self):
        long_title = "seeded-b admits privileged pods " + "word " * 70
        cases = {
            "severity": REPORT,
            "markup": "Scan.\n\n1. **`kube-system` on *seeded-b* has ~no~ [policy](https://example.com/p) (major)**\n   x\n"
            "2. **B**\n   y\n",
            "clipped": f"Scan.\n\n1. **{long_title}(critical)**\n   x\n2. **B**\n   y\n",
            # Clipped as markdown, so the link's URL counts against the clip; clipping
            # the plain text instead leaves more of the title than the card shows.
            "clipped link": f"Scan.\n\n1. **[seeded-b](https://example.com/{'p' * 60}) admits {long_title}**\n   x\n"
            "2. **B**\n   y\n",
        }
        for name, report in cases.items():
            with self.subTest(name):
                blocks, _ = inventory_presenter.blocks(report)
                primary = blocks[-1]["elements"][0]
                elements = blocks[2]["elements"][0]["elements"]
                if elements[0].get("style") == {"code": True} and elements[1] == {"type": "text", "text": " "}:
                    elements = elements[2:]
                first_line = "".join(e["text"] for e in elements).split("\n", 1)[0]
                self.assertEqual(primary["value"], f"{primary['text']['text']}: {first_line}")
                if name.startswith("clipped"):
                    self.assertTrue(first_line.endswith("…"), first_line)

    def test_a_gap_the_posture_names_is_its_own_line_under_the_headline(self):
        report = self._probe(GAP_POSTURE)
        self.assertEqual(
            self._headline(report),
            (
                "I scanned 5 clusters and 41 workloads and found 22 things to look at.",
                "",
                "2 clusters could not be scanned (permission denied). Start with these two:",
            ),
        )
        blocks, text = inventory_presenter.blocks(report)
        self.assertIn("2 clusters could not be scanned (permission denied).", str(blocks[0]))
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 22"])
        self.assertIn("2 clusters could not be scanned (permission denied).", text)

    def test_a_gap_written_after_the_list_is_on_the_card(self):
        gap = "2 clusters could not be scanned (permission denied)."
        for tail in ("Also: " + gap, "2 more clusters could not be scanned (permission denied)."):
            with self.subTest(tail=tail):
                report = TWO_ITEMS + tail + "\n"
                blocks, text = inventory_presenter.blocks(report)
                self.assertIn(tail.removeprefix("Also: "), str(blocks[0]))
                self.assertIn(tail.removeprefix("Also: "), text)
                # The text layout keeps the closing line itself, so the gap is not repeated above it.
                presented = inventory_presenter.present(report)
                self.assertTrue(presented.endswith("\n\n" + tail + "\n"), presented)
                self.assertEqual(presented.count("could not be scanned"), 1, presented)

    def test_a_gap_written_twice_is_on_the_card_once(self):
        gap = "2 clusters could not be scanned (permission denied)."
        for report in (
            "# Scan\n\nI scanned 3 clusters; " + gap + "\n\n1. **A.** b\n2. **B.** c\n\nAlso found: 18 more items.\n\nNote: " + gap + "\n",
            TWO_ITEMS + "Also found: 18 more items; " + gap + "\n\n" + gap + "\n",
            TWO_ITEMS + "Also found: 18 more items.\n\n" + gap + "\n\n" + gap.upper() + "\n",
        ):
            with self.subTest(report=report[-60:]):
                blocks, text = inventory_presenter.blocks(report)
                for out in (str(blocks[0]), text):
                    self.assertEqual(out.lower().count("could not be scanned"), 1, out)

    def test_a_gap_in_the_roll_up_outlives_it(self):
        report = TWO_ITEMS + "Also found: 18 more items; seeded-d was skipped (no credentials).\n"
        blocks, text = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 20"])
        for out in (str(blocks[0]), text, inventory_presenter.present(report)):
            with self.subTest(out=out):
                self.assertIn("seeded-d was skipped (no credentials).", out)
                self.assertNotIn("Also found", out)

    def test_a_clause_saying_nothing_was_missed_is_not_a_gap(self):
        for posture in (
            "I scanned 3 clusters and 41 workloads; no clusters were unreachable.",
            "I scanned 3 clusters and 41 workloads; 0 clusters unreachable.",
            "I scanned 3 clusters and 41 workloads; none were skipped.",
            "I scanned 3 clusters with no unreachable nodes and 41 workloads.",
            "I scanned 3 clusters and 41 workloads; there were 0 clusters that were unreachable.",
            "I scanned 3 clusters and 41 workloads; not a single cluster was unreachable.",
        ):
            with self.subTest(posture=posture):
                self.assertEqual(
                    self._headline(self._probe(posture)),
                    ("I scanned 3 clusters and 41 workloads and found 22 things to look at.", "Start with these two:"),
                )

    def test_a_denied_or_forbidden_finding_is_not_a_gap(self):
        for posture, headline in (
            ("I scanned 3 clusters and 12 requests were denied by the admission policy.", "I scanned 3 clusters"),
            ("I scanned 3 clusters and 41 workloads and 2 images are forbidden.", "I scanned 3 clusters and 41 workloads"),
            ("I scanned 3 clusters and found 5 denied requests in the audit log.", "I scanned 3 clusters"),
            ("I scanned 3 clusters and 41 workloads, with 2 forbidden images.", "I scanned 3 clusters and 41 workloads"),
        ):
            with self.subTest(posture=posture):
                self.assertEqual(
                    self._headline(self._probe(posture)),
                    (f"{headline} and found 22 things to look at.", "Start with these two:"),
                )

    def test_a_gap_after_a_clean_part_of_its_clause_is_kept(self):
        for posture, gap in (
            ("No drift, but 2 clusters could not be scanned (permission denied).", "2 clusters could not be scanned (permission denied)."),
            ("I scanned 3 clusters; 2 clusters with no credentials could not be scanned.", "2 clusters with no credentials could not be scanned."),
        ):
            with self.subTest(posture=posture):
                self.assertEqual(self._headline(self._probe(posture))[2], f"{gap} Start with these two:")

    def test_every_gap_is_kept_with_the_names_its_colon_lists(self):
        for posture, gap in (
            ("I scanned 3 clusters, but seeded-d was unreachable, and seeded-e was skipped.", "seeded-d was unreachable, seeded-e was skipped."),
            ("I scanned 3 clusters (1 skipped, 2 unreachable).", "1 skipped, 2 unreachable."),
            ("I scanned 3 clusters (2 new and 1 skipped).", "1 skipped."),
            ("I scanned 3 clusters (2 new but 1 unreachable).", "1 unreachable."),
            ("I scanned 3 clusters and seeded-e was unreachable.", "seeded-e was unreachable."),
            (
                "I scanned 3 clusters, and 2 clusters in us-east1 and seeded-c were unreachable.",
                "2 clusters in us-east1 and seeded-c were unreachable.",
            ),
            (
                "I scanned 3 clusters and 2 namespaces were denied and seeded-c was skipped.",
                "2 namespaces were denied and seeded-c was skipped.",
            ),
            ("I scanned 3 clusters; skipped: seeded-d (no credentials).", "Skipped: seeded-d (no credentials)."),
            ("I scanned 3 clusters; 2 clusters could not be scanned: seeded-d, seeded-e.", "2 clusters could not be scanned: seeded-d, seeded-e."),
        ):
            with self.subTest(posture=posture):
                self.assertEqual(
                    self._headline(self._probe(posture)),
                    ("I scanned 3 clusters and found 22 things to look at.", "", f"{gap} Start with these two:"),
                )

    def test_a_sentence_opening_on_a_number_is_a_clause_of_its_own(self):
        # Its count is not the scan's: only the clause with the scan verb is counted.
        report = self._probe("I scanned 3 clusters. 41 workloads run as root.")
        self.assertEqual(self._headline(report)[0], "I scanned 3 clusters and found 22 things to look at.")

    def test_a_gap_sharing_a_sentence_with_the_scan_leaves_the_counts(self):
        for posture, gap in (
            ("I scanned 3 clusters and 41 workloads, but 2 clusters could not be scanned.", "2 clusters could not be scanned."),
            ("I scanned 3 clusters and 41 workloads but seeded-c was not reached.", "seeded-c was not reached."),
            ("I scanned 41 workloads across 3 clusters. 1 of 4 clusters was not scanned.", "1 of 4 clusters was not scanned."),
            (
                "I scanned 3 clusters and 41 workloads (seeded-d unreachable, 5 namespaces denied).",
                "seeded-d unreachable, 5 namespaces denied.",
            ),
        ):
            with self.subTest(posture=posture):
                self.assertEqual(
                    self._headline(self._probe(posture)),
                    (
                        "I scanned 3 clusters and 41 workloads and found 22 things to look at.",
                        "",
                        f"{gap} Start with these two:",
                    ),
                )

    def test_a_version_number_is_not_a_cluster_count(self):
        report = self._probe("I scanned two GKE 1.30 clusters and 41 workloads.")
        self.assertEqual(self._headline(report)[0], "I scanned 41 workloads and found 22 things to look at.")

    def test_a_count_outside_a_scan_clause_is_not_named(self):
        posture = "One cluster is past end of support; the fleet of 4 clusters and 60 workloads is otherwise healthy."
        self.assertEqual(self._headline(self._probe(posture))[0], "I found 22 things to look at.")

    def test_a_partial_scan_names_what_was_scanned_not_the_fleet(self):
        for posture, scanned in (
            ("I scanned 3 of 5 clusters; the other 2 could not be scanned (permission denied).", "3 clusters"),
            ("I scanned 3 out of 5 clusters and 41 of 60 workloads.", "3 clusters and 41 workloads"),
            ("I scanned three of five GKE clusters.", "3 clusters"),
        ):
            with self.subTest(posture=posture):
                headline = self._headline(self._probe(posture))[0]
                self.assertEqual(headline, f"I scanned {scanned} and found 22 things to look at.")

    def test_scan_clauses_that_disagree_name_no_count(self):
        posture = "I scanned 3 clusters and 41 workloads. I reviewed 2 clusters in depth."
        self.assertEqual(self._headline(self._probe(posture))[0], "I scanned 41 workloads and found 22 things to look at.")

    def test_a_roll_up_split_by_severity_is_summed(self):
        report = self._probe("I scanned 3 clusters and 41 workloads.", "Also found: 2 high, 5 medium and 12 low findings.")
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 22"])
        self.assertNotIn("Also found", str(blocks))

    def test_a_roll_up_in_other_words_is_counted(self):
        report = self._probe("I scanned 3 clusters and 41 workloads.", "Plus 19 lower-priority findings in the full inventory.")
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 22"])

    def test_a_scope_count_in_a_roll_up_is_not_its_count(self):
        for rollup, total in (
            ("Also found 3 namespaces with 19 issues.", 22),
            ("Also found: 2 clusters with 19 lower-priority findings.", 22),
            ("Plus 3 namespaces carry 19 more issues.", 22),
            ("19 issues in 2 namespaces: x, y.", 22),
        ):
            with self.subTest(rollup=rollup):
                blocks, _ = inventory_presenter.blocks(self._probe("I scanned 3 clusters.", rollup))
                self.assertEqual(self._buttons(blocks)[-1], f"See all {total}")

    def test_a_roll_up_counts_whatever_it_calls_its_findings(self):
        for rollup in (
            "Also found 19 more.",
            "Also found 19 warnings.",
            "Also found 19 misconfigured workloads.",
            "Also found 19 best-practice gaps in dev.",
            "Also found 19 lower priority best-practice issues.",
            "Also found 19 workloads without resource limits in 3 namespaces.",
            "Also found 19 misconfigured workloads across 3 clusters.",
            "Also found 19 workload misconfigurations across 3 clusters.",
        ):
            with self.subTest(rollup=rollup):
                report = self._probe("I scanned 3 clusters.", rollup)
                self.assertEqual(self._headline(report)[0], "I scanned 3 clusters and found 22 things to look at.")
                self.assertEqual(self._buttons(inventory_presenter.blocks(report)[0])[-1], "See all 22")

    def test_a_closing_total_counts_and_all_is_not_more(self):
        report = self._probe("I scanned 3 clusters.", "Ask to see all 22 findings.")
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 22"])

    def test_the_posture_total_outranks_a_closing_in_total(self):
        for line in (
            "seeded-a has 9 findings in total.",
            "Fixing the first one clears 5 findings in total.",
            "seeded-a has 4 findings in total and seeded-b has 18 findings in total.",
        ):
            with self.subTest(line=line):
                report = self._probe("I scanned 3 clusters and found 22 findings.", line)
                self.assertEqual(inventory_presenter._shape(report).total, 22)
        report = self._probe("I scanned 3 clusters and found 22 findings.", "Ask to see all 30 findings.")
        self.assertEqual(inventory_presenter._shape(report).total, 30)

    def test_a_closing_in_total_outranks_a_posture_count_with_no_scan_verb_before_it(self):
        for posture in (
            "I scanned 3 clusters; seeded-a has 9 findings.",
            "I scanned 3 clusters; seeded-a has 9 findings across 41 workloads.",
        ):
            with self.subTest(posture=posture):
                report = self._probe(posture, "I found 22 findings in total.")
                self.assertEqual(inventory_presenter._shape(report).total, 22)
        report = self._probe("seeded-a has 9 findings.", "")
        self.assertEqual(inventory_presenter._shape(report).total, 9)

    def test_the_largest_closing_in_total_counts(self):
        for line in (
            "seeded-a has 4 findings in total. I found 22 findings in total.",
            "I found 22 findings in total. seeded-a has 4 findings in total.",
        ):
            with self.subTest(line=line):
                self.assertEqual(inventory_presenter._shape(self._probe("I scanned 3 clusters.", line)).total, 22)

    def test_all_before_a_sentences_in_total_is_not_the_total(self):
        report = self._probe("I scanned 3 clusters.", "Fixing all 3 findings clears 22 findings in total.")
        self.assertEqual(inventory_presenter._shape(report).total, 22)

    def test_a_decimal_is_not_a_posture_total(self):
        report = self._probe("I scanned 3 clusters running 1.30 and 4.5 findings per cluster on average.", "")
        blocks, _ = inventory_presenter.blocks(report)
        self.assertEqual(self._buttons(blocks), ["Fix the first one", "See all 3"])

    def test_unparseable_is_none(self):
        self.assertIsNone(inventory_presenter.blocks("# Report\n\n| Cluster | ... |\n"))

class OriginPlatformTest(unittest.TestCase):
    def _with_jobs(self, get_job):
        cron = types.ModuleType("cron")
        jobs = types.ModuleType("cron.jobs")
        jobs.get_job = get_job
        return mock.patch.dict(sys.modules, {"cron": cron, "cron.jobs": jobs})

    def test_reads_the_bound_platform(self):
        with self._with_jobs(lambda _id: {"origin": {"platform": "slack", "chat_id": "C1"}}):
            self.assertEqual(bootstrap_delivery._origin_platform(), "slack")

    def test_a_missing_job_or_origin_is_none(self):
        for job in (None, {}, {"origin": None}):
            with self.subTest(job=job), self._with_jobs(lambda _id, job=job: job):
                self.assertIsNone(bootstrap_delivery._origin_platform())

    def test_a_get_job_error_is_none(self):
        def boom(_id):
            raise OSError("jobs.json unreadable")

        with self._with_jobs(boom), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(bootstrap_delivery._origin_platform())

    def test_no_cron_module_is_none(self):
        with mock.patch.dict(sys.modules, {"cron": None, "cron.jobs": None}), contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(bootstrap_delivery._origin_platform())


class DeliveryFlagTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        (self.d / "INVENTORY.md").write_text(REPORT, encoding="utf-8")
        (self.d / ".user_aligned").touch()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, flag, platform="slack", relay=None):
        env = {k: v for k, v in os.environ.items() if k not in ("KAGE_SLACK_UX", "SLACK_RELAY_URL")}
        if flag is not None:
            env["KAGE_SLACK_UX"] = flag
        if relay is not None:
            env["SLACK_RELAY_URL"] = relay
        origin = {"platform": platform, "chat_id": "C1"} if platform else {}
        buf = io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(bootstrap_delivery, "_origin", lambda: dict(origin)),
            contextlib.redirect_stdout(buf),
        ):
            rc = bootstrap_delivery.main(self.d)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_flag_unset_delivers_verbatim(self):
        self.assertEqual(self._run(None), REPORT)

    def test_flag_off_delivers_verbatim(self):
        self.assertEqual(self._run("0"), REPORT)

    def test_flag_on_delivers_presented_and_archives_original(self):
        self.assertEqual(self._run("1"), PRESENTED)
        self.assertEqual((self.d / "INVENTORY.delivered.md").read_text(encoding="utf-8"), REPORT)

    def test_flag_on_without_a_relay_prints_the_text(self):
        # A Slack origin with a chat id, so only the missing relay stops the blocks.
        import slack_blocks_post

        with mock.patch.object(slack_blocks_post, "post") as poster:
            self.assertEqual(self._run("1"), PRESENTED)
        poster.assert_not_called()

    def test_google_chat_is_verbatim_with_the_flag_on(self):
        # A relay is set, so only the platform keeps the blocks off a Google Chat origin.
        import slack_blocks_post

        with mock.patch.object(slack_blocks_post, "post") as poster:
            self.assertEqual(self._run("1", platform="google_chat", relay=RELAY_URL), REPORT)
        poster.assert_not_called()

    def test_a_missing_origin_is_verbatim_with_the_flag_on(self):
        import slack_blocks_post

        with mock.patch.object(slack_blocks_post, "post") as poster:
            self.assertEqual(self._run("1", platform=None, relay=RELAY_URL), REPORT)
        poster.assert_not_called()

    def test_presenter_failure_delivers_verbatim(self):
        boom = mock.patch.object(inventory_presenter, "present", side_effect=ValueError("boom"))
        with boom, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self._run("1"), REPORT)



class BlocksDeliveryTest(unittest.TestCase):
    ORIGIN = {"platform": "slack", "chat_id": "C1", "thread_id": "1.5"}

    def setUp(self):
        import slack_blocks_post

        self.sbp = slack_blocks_post
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        (self.d / "INVENTORY.md").write_text(REPORT, encoding="utf-8")
        (self.d / ".user_aligned").touch()

    def _run(self, post, flag="1", origin=ORIGIN):
        env = {k: v for k, v in os.environ.items() if k != "KAGE_SLACK_UX"}
        env.update({"KAGE_SLACK_UX": flag, "SLACK_RELAY_URL": RELAY_URL})
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(bootstrap_delivery, "_origin", lambda: dict(origin)),
            mock.patch.object(self.sbp, "post", side_effect=post) as poster,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(bootstrap_delivery.main(self.d), 0)
        self.posts = poster.call_args_list
        return out.getvalue()

    def test_posts_blocks_into_the_origin_and_prints_nothing(self):
        self.assertEqual(self._run(lambda *a: "9.9"), "")
        (post,) = self.posts
        channel, text, blocks, thread_ts = post.args
        self.assertEqual((channel, thread_ts), ("C1", "1.5"))
        self.assertEqual(blocks[-1]["elements"][0]["text"]["text"], "Fix the first one")
        self.assertTrue(text.startswith("*I scanned 3 clusters and 41 workloads and found 22 things to look at."))
        self.assertTrue((self.d / ".bootstrap_completed").exists())
        self.assertEqual((self.d / "INVENTORY.delivered.md").read_text(encoding="utf-8"), REPORT)

    def test_flag_off_is_byte_identical(self):
        self.assertEqual(self._run(lambda *a: "9.9", flag="0"), REPORT)
        self.assertEqual(self.posts, [])

    def test_a_refusal_that_is_not_about_blocks_is_not_retried(self):
        self.assertEqual(self._run(self.sbp.Refused("channel_not_found")), PRESENTED)
        self.assertEqual(len(self.posts), 1)

    def test_refused_blocks_are_not_retried(self):
        # The card has no fold to leave out, so the same blocks would be refused again.
        self.assertEqual(self._run(self.sbp.Refused("invalid_blocks")), PRESENTED)
        self.assertEqual(len(self.posts), 1)

    def test_a_relay_failure_prints_the_text(self):
        self.assertEqual(self._run(OSError("connection refused")), PRESENTED)
        self.assertEqual(len(self.posts), 1)

    def test_a_post_that_may_have_landed_still_prints_the_text(self):
        # The first inventory is sent once, so a second copy beats none.
        self.assertEqual(self._run(TimeoutError("timed out")), PRESENTED)
        self.assertEqual(len(self.posts), 1)

    def test_no_chat_id_prints_the_text(self):
        self.assertEqual(self._run(lambda *a: "9.9", origin={"platform": "slack"}), PRESENTED)
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()
