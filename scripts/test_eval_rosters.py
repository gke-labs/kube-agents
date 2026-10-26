"""scripts/eval_rosters.py reads hack/eval/ the way the shell does, and the split lost nothing.

Three files replaced three bash arrays in hack/ci-eval-pr.sh on 2026-09-15
(#1546): TASKS, NIGHTLY_TASKS and BOOTSTRAP_ADMITTED's default. Two things
are pinned here. The parser: comments, blank lines, trailing notes and the
old script's default line all read as the shell reads them, and a malformed
entry raises rather than being skipped, because the shell stops the job on
it. The contents: the presubmit file and the blocking roster held exactly the
sets the script carried at the split -- what runs on every pull request and
what blocks did not move that day; the admissions and promotions since are
pinned beside them (ADMITTED_AFTER_THE_SPLIT, PROMOTED_AFTER_THE_SPLIT), and
so is the 2026-09-22 decision that the presubmit runs the blocking roster
only (HELD_OUT_TO_NIGHTLY: the seven held-out cases that left the presubmit
file for the nightly one that day) and the held-out seats a coverage tracker
puts back in the presubmit file without a roster line (HELD_OUT_IN_PRESUBMIT,
the documented exception: #2013; #2016's seat took its roster line 2026-10-14) and the
cases demoted since (DEMOTED_AFTER_THE_SPLIT) -- and the nightly file holds the
script's nightly array plus the nine cases the TASKS array held commented
out, which the same decision moved into the nightly (#1546, #1564), plus
whatever landed there since (ADDED_AFTER_THE_SPLIT, ADDED_AFTER_THE_MOVED_BLOCK),
less the cases promoted out of it since, plus the seven, less any held-out
seat. A later roster change
edits the expected sets here in the same pull request; that is the point of
pinning them, since the files are what the eval-crew rule in hack/OWNERS
guards. Since #3495 that pin covers the nightly file's frozen part only: a
case registered since, or a line that comes back to the nightly (a demotion,
a withdrawn seat), goes in its sorted section, which NightlySortedSectionTest
holds by its properties, so a case pull request edits no list here.

scripts/test_ci_eval_nightly.py runs the real shell over the real files and
asserts it builds the arrays this module reads.

The seat bar (#3968, section 5; docs/eval-gate-roster.md, "The five rules a
seat meets") is held here too: SeatBarTest reads every blocking-roster case's
task.yaml for rules (c), a fixture the hourly seeded-fleet scan asserts, and
(d), a deterministic objective, with SEAT_BAR_EXEMPT the seats that break one
today, each on its issue; PresubmitWallTimeTest holds rule (e), the estimated
presubmit wall time under eval_rosters.PRESUBMIT_WALL_BUDGET_MINUTES.
"""

import contextlib
import io
import pathlib
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import eval_rosters

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"

# The TASKS array's uncommented entries at the split (main at 8263e7fd), in
# order: the presubmit matrix, eighteen cases.
PRESUBMIT_AT_SPLIT = [
    "reliability-pdb-probe",
    "capacity-pinned-pool-probe",
    "security-overgrant-probe",
    "upgrades-lagging-master-probe",
    "consistency-authorized-networks-probe",
    "cost-idle-pool-probe",
    "security-overgrant-remediation-proposal",
    "obtainability-pdb-semantics",
    "obtainability-fleet-exposure-sweep",
    "obtainability-healthy-namespace-silence",
    "obtainability-remediation-proposal",
    "rca-remediation-pr",
    "compliance-rbac-overgrant",
    "cluster-agent-crashloop-debug",
    "cluster-agent-crashloop-misleading-symptom",
    "cluster-agent-crashloop-evidence-chain",
    "cluster-agent-healthy-workload-no-finding",
    "agent-kanban-smoke",
]
# BOOTSTRAP_ADMITTED's default at the split, in order: ten cases.
ROSTER_AT_SPLIT = [
    "reliability-pdb-probe",
    "security-overgrant-probe",
    "upgrades-lagging-master-probe",
    "consistency-authorized-networks-probe",
    "cost-idle-pool-probe",
    "obtainability-remediation-proposal",
    "cluster-agent-crashloop-debug",
    "cluster-agent-crashloop-misleading-symptom",
    "cluster-agent-crashloop-evidence-chain",
    "agent-kanban-smoke",
]
# NIGHTLY_TASKS at the split, in order: eleven cases.
NIGHTLY_AT_SPLIT = [
    "obtainability-planted-pdb",
    "stockout-pinned-pool",
    "upgrade-readiness-lagging-cluster",
    "consistency-drift-outlier",
    "obtainability-direct-query",
    "gpu-stress-test-diagnosis",
    "autoops-warning-event-triage",
    "knowledge-grounding-sources-probe",
    "cluster-agent-stalled-controller-healthy-silence",
    "chat-routing-board-read",
    "pdb-remediation-pr",  # its 2026-09-22 promotion was withdrawn (the record predated its #1780 grader); seated held out 2026-09-28, promoted 2026-10-14 (#2016), demoted 2026-10-17 (#3120), back 2026-10-26
]
# The nine cases TASKS held commented out at the split, moved into the
# nightly by the same decision. The two commented-out cases NOT here had no
# fixture at all and waited in the validator's FIXTURE_NOT_READY instead:
# vcs-history-only-fact (#1253) still does, and
# obtainability-declared-intent-no-finding (#1341) joined this file on
# 2026-09-29 with its fixture (#1409, ADDED_AFTER_THE_SPLIT below).
# Registered in the nightly file after the split, in file order, each by the
# pull request that authored the case (a new case lands in the nightly first).
ADDED_AFTER_THE_SPLIT = [
    "incident-triage-oom-event-probe",  # #1023's incident-triage second case, PR #1625; promoted 2026-09-22
    "ai-security-planted-model-audit",  # #1023's fleet-audits second case, PR #1103
    "autoops-crashloop-config-triage",  # #1023's other incident-triage second case, PR #1103
    "consistency-no-environment-label",  # the drift collector's §4.14 check, with fleet_drift.py
    "gitops-drift-out-of-band-triage",  # the drift half of incident-triage, PR #1827
    "cluster-agent-delegation-profile-lookup",  # #1840's delegation route, PR #1917
    "upgrades-master-behind-offered-elsewhere",  # the patch collector's §3.1 route check, with patch_readiness.py
    "obtainability-planted-orphan-service",  # the obtainability collector's §3.16 check, with collect.py
    "obtainability-declared-intent-no-finding",  # #1341 criterion 4, activated with its declared-no-pdb-workload fixture (#1409)
    "compliance-declared-intent-no-finding",  # the compliance stream's declared-intent step over the same fixture (#1408)
    "compliance-declared-token-shields-siblings",  # the compliance stream's other posture, over the declared-token-workload fixture (#1408)
    "patch-declared-intent-no-finding",  # the patch stream's declared-intent step, over seeded-c's missing upgrade notifications (#1408)
    "cost-declared-intent-no-finding",  # the waste stream's declared-intent step, over the declared-overrequest-workload fixture (#1408)
]
# Appended at the tail of the nightly file.
ADDED_AT_THE_TAIL = [
    "chat-routing-own-cluster-namespaces",
    "chat-reset-history-names-the-command",  # the front door's reset-request reply, #2189
]
MOVED_TO_NIGHTLY = [
    "cluster-agent-pending-replicas-capped-pool",
    "obtainability-refusal-direct-mutation",
    "chat-routing-fleet-question",
    "fleet-cost-idle-pool",
    "upgrades-fleet-version-table",
    "upgrades-fleet-rollout-stall",
    "upgrades-fleet-readiness-exclusion",
    "upgrades-api-deprecation-clean-repo",
    "cluster-agent-crashloop-fix-request",
]
# Registered after the moved-in nine, which is why this is a second list and
# not more entries in ADDED_AFTER_THE_SPLIT: those nine sit between the two in
# the file, so file order is not registration order. Newest last. The consumer
# migration's two cases (#1246 PR-2): a full issue-resolver run and a
# read-back on an existing proposal's branch, both writing to the eval GitOps
# repository, both nightly on measured cost.
ADDED_AFTER_THE_MOVED_BLOCK = [
    "vcs-issue-resolver-triage",
    "vcs-review-feedback-read-back",
    # #1918: a second proposal under a branch name its first proposal spent.
    "vcs-spent-branch-reuse",
    # #2201: a request for gh answered through the verbs.
    "vcs-forge-cli-request-uses-the-verbs",
]
# Registered after the moved block, in file order, by the pull request that
# authored each case. Frozen with the nightly file's frozen part (#3495): a
# case registered since goes in that file's sorted section, which
# NightlySortedSectionTest holds by its properties, so nothing is appended here.
ADDED_AFTER_THE_MOVE = [
    "obtainability-design-quota-vs-capacity",  # the two obtainability-journey probes, PR #1841
    "obtainability-window-planning-probe",
    "bootstrap-discovery-fanout",  # the onboarding discovery fan-out, PR #2085
    "first-install-hello-running",  # the first-install hello, both variants
    "first-install-hello-done",
    "fleet-audit-reports-past-run",  # the report store's reader, PR #2115
    "platform-worker-refuses-shipped-skill-edit",  # skill governance, #1848
    "chat-voice-ack-names-target",  # the front door's delegation ack
    "chat-sample-report-is-not-cluster-state",  # nothing filed on a sample report's say-so, #3080
    "chat-sample-report-is-not-cluster-state-labelled",  # the same, with the patched adapter's label
    "chat-real-report-fix-files-confirm-first",  # a real report still files at once, confirm-first
    "bootstrap-inventory-ranking-delivery",  # the onboarding prioritization stage, #2143
    "chat-question-wake-stays-silent",  # SOUL §2 step 5's already-posted rule
    "chat-question-typed-answer-fresh-session",  # a typed answer from a new thread session
    "chat-question-click-answer-stays-silent",  # SOUL §1.5 Unblock: silent after a button answer
    "platform-worker-shell-edit-of-shipped-skill",  # the sandbox's image trees, #2096
    "autoops-controller-stall-triage",  # the stall watch on the inject path
    "chat-voice-retry-says-it-is-retried",  # the front door's reply to a crashed card
    "chat-voice-final-attempt-is-not-retried",  # the front door's reply to a card's last attempt
    "chat-voice-failure-leads-with-fact",  # the front door's reply to a blocked card
    "upgrades-zonal-control-plane-outage-warned",  # upgrade-failure catalogue entry 11, the first scenario case
    "oobe-first-run-audits",  # the oobe job's first-run audits stage
    "chat-voice-answer-first",  # a delegated answer opens on its verdict
    "observability-watcher-scrape-state",  # the event watcher's scrape state, #2141
    "platform-security-secrets-encryption-currentstate-probe",  # the gke-basics/security skills sync
    "chat-fanout-fleet-restarts-rows",  # a fleet question fanned out one titled card per cluster
    "backup-dr-cmek-selected-namespaces-probe",  # the gke-backup-dr skill sync
    "findings-decision-covers-item",  # the findings queue's item-wide decision
    "upgrades-fleet-readiness-failclosed-webhook",  # the readiness report's fail-closed webhook source
    "upgrades-freeze-runbook-probe",  # the gke-upgrades skill sync
    "fleet-audit-clean-close-reads-clean",  # the clean close's banner, with close_ledger_reads_clean
    "session-crashloop-question-loads-a-skill",  # the session persona's skills, #2858
    "fleet-audit-unchanged-finding-keeps-wording",  # finish keeps the wording of an unchanged finding
    "upgrades-audit-reports-upgrade-blocked",  # the weekly upgrade audit's check 3.11, graded in the ledger
    "scale-request-proposes-pr",  # the mirrored gke-* skills' pull request path, #2937
    "session-cluster-question-wake-answers-once",  # a session's delegated question ends in a wake's answer
    "gitops-drift-noise-filtered-triage",  # the drift detector's principal classifier, #2739's fixture
    "chat-question-asked-to-choose-pauses",  # an asked-for choice pauses the card on a needs_input question
    "manage-cluster-exclusion-installer-route-probe",  # the exclusion route on an installer-made install
    "upgrades-readiness-zonal-control-plane",  # catalogue entry 11 through the readiness report's per-entry rules
    "stop-on-permission-refusal",  # retry after a command-policy refusal, #1948
    "not-found-target-stops-as-failure",  # a missing target stops its card on a hard wall, mock 06
    "alert-config-terraform-promql-probe",  # the gke-alert-configuration skill sync
    "cluster-agent-observation-time-read",  # an audit trail states no time it did not read, #3256
    "security-chat-request-runs-collector",  # a plain security-posture request runs the compliance collector, #2386
    "compute-class-canary-active-migration-pdb-probe",  # the gke-compute-classes skill sync
    "observability-trace-latency-brokered",  # the trace latency helper through the broker, unparked 2026-10-16 (#2244)
    "cost-chat-request-runs-collector",  # a plain cost request runs the waste collector, #2386
    "reliability-chat-request-runs-collector",  # a plain workload-reliability request runs the obtainability collector, #2386
    "stockout-chat-request-runs-collector",  # a plain stockout-risk request runs the stockout collector, #2386
    "upgrade-chat-request-runs-collector",  # a plain upgrade-readiness request runs the patch collector, #2386
    "drift-chat-request-runs-collector",  # a plain peer-consistency request runs the drift collector, #2386
    "ai-security-chat-request-runs-collector",  # a plain AI-security request runs the AI security collector, #2386
    "provisioning-cluster-request-never-creates",  # a cluster request ends in a PR or a stop, #2937
    "provisioning-kcc-cluster-without-crds-stops",  # the KCC skill's no-CRDs stop, #2937
    "cluster-agent-security-audit-script",  # a Cluster Agent runs its audit script from the path its skill names, #3402
    "cluster-setting-request-proposes-pr",  # the gke-platform-security skill's pull request path, #2937
    "fanout-absent-cluster-cards-complete",  # a fleet search's per-cluster cards complete, #2978
    "chat-failure-reply-declares-next-step",  # a blocked card's reply declares the one next step, mock 06
    "chat-answer-declares-next-step",  # a card's answer declares its one obvious next step, mock 07
    "chat-answer-tangent-offer-declares-no-step",  # an offer about an unasked workload declares no step, mock 03
    "fleet-zonal-cluster-named-by-region",  # a zonal cluster named by its region, #3371
    "helm-chart-no-reconciler-stops",  # gitops-helm-chart stops where nothing renders charts, #2938
    "delete-namespace-unowned-stops",  # a delete the repository cannot carry ends in a stated stop, #3077
]
# Taken back out of the nightly file after registering there, each with a
# KNOWN_UNREGISTERED entry in scripts/validate_bench_cases.py that says why.
PARKED_AFTER_THE_MOVE: list[str] = []

# Admitted after the split, each by a pull request that cited the record
# (docs/eval-gate-roster.md, "Admitted on the record since the split"), as
# (case, the roster line it follows): the file keeps the presubmit file's
# reporting order.
ADMITTED_AFTER_THE_SPLIT = [
    # 2026-09-22 (#1023): 529/570 graded presubmit repetitions since #1626, no collapse.
    ("capacity-pinned-pool-probe", "reliability-pdb-probe"),
    # 2026-09-22 (#1023): 10/12 on the same four nights, both misses platform bugs (#1840, #1874).
    ("incident-triage-oom-event-probe", "cluster-agent-crashloop-evidence-chain"),
    # 2026-10-14 (#2016): 3/3 on the three writers nightlies 10-12 to 10-14, the store at would-admit, 90.2% of 2,086 graded presubmit repetitions since #2263 (10-03).
    # Demoted 2026-10-17 (#3120); back 2026-10-26 after #3145: 21/21 on the writers nightlies since the claude-opus-5-5 key of 10-19, 9/9 since #3145, the store at collecting.
    ("pdb-remediation-pr", "obtainability-remediation-proposal"),
    # 2026-10-23: 42/42 on the fourteen writers nightlies 10-09 to 10-22 (12/12 since the claude-opus-5-5 key of 10-19), the store at collecting.
    ("vcs-spent-branch-reuse", "agent-kanban-smoke"),
]
# Moved from the nightly file into the presubmit one after the split, as
# (case, the presubmit line it follows); the same case leaves the nightly
# expectation (NIGHTLY_AT_SPLIT or ADDED_AFTER_THE_SPLIT, whichever registered
# it), since the nightly runs both files and lists no case twice.
PROMOTED_AFTER_THE_SPLIT = [
    ("incident-triage-oom-event-probe", "cluster-agent-healthy-workload-no-finding"),  # 2026-09-22 (#1023)
    ("pdb-remediation-pr", "obtainability-remediation-proposal"),  # 2026-10-14 (#2016), from its held-out seat; again 2026-10-26, from the nightly file (#3120)
    ("vcs-spent-branch-reuse", "agent-kanban-smoke"),  # 2026-10-23, from the nightly file's frozen part
]
# Moved from the presubmit file to the end of the nightly one on 2026-09-22
# (#1023), when the eval crew decided the presubmit runs the blocking roster
# and nothing else: the seven cases the presubmit had run without letting
# them block, in the presubmit file's reporting order, each with its hold-out
# reason beside its nightly line. The presubmit file and the roster held the
# same cases from then until the first held-out seat (HELD_OUT_IN_PRESUBMIT).
HELD_OUT_TO_NIGHTLY = [
    "security-overgrant-remediation-proposal",  # #1066, never admitted
    "obtainability-pdb-semantics",  # #1049, never admitted
    "obtainability-fleet-exposure-sweep",  # #1049, never admitted
    "obtainability-healthy-namespace-silence",  # #1049, never admitted
    "rca-remediation-pr",  # demoted 2026-09-02, #1189
    "compliance-rbac-overgrant",  # demoted 2026-09-02, #1171; seated back in the presubmit 2026-09-29 (HELD_OUT_IN_PRESUBMIT)
    "cluster-agent-healthy-workload-no-finding",  # held out on #1010
]

# Seated in the presubmit file WITHOUT a roster line: the documented exception
# to the 2026-09-22 rule, one case per coverage tracker, as (case, the
# presubmit line it follows). A held-out seat runs on every pull request and
# cannot red one on a graded failure -- rungs 4 and 6 are scoped to the
# roster; rungs 1-3 still block for it as for every case -- and earns its
# record at presubmit volume instead of one nightly a night. Each case here is
# also in a nightly expectation above (its nightly line is what moved), and
# the nightly still runs it through the presubmit file. The tracker's
# roster-line step deletes the entry here and adds the name to
# blocking-roster.txt. roster <= presubmit holds; presubmit == roster holds
# less exactly this list.
HELD_OUT_IN_PRESUBMIT = [
    ("compliance-rbac-overgrant", "vcs-spent-branch-reuse"),  # #2013 step 2, seated 2026-09-29 after agent-kanban-smoke; the roster line is step 4
]


# Demoted after the split under docs/eval-gate-roster.md "Demoting a flaky
# case": the line leaves blocking-roster.txt and presubmit-cases.txt and lands
# in the nightly file with its issue above it, as (case, the nightly line it
# follows). Restoring a case deletes its entry here.
# The second element is the frozen-part line the demoted case follows, or None
# when its line went in the nightly file's sorted section (#3495), which is
# where a demotion lands from now on.
DEMOTED_AFTER_THE_SPLIT = [
    # upgrades-lagging-master-probe sat here 2026-10-16 to 2026-10-23 (#3269; its line followed
    # fleet-cost-idle-pool) and took its roster line back on three clean nightlies after #3338.
    # pdb-remediation-pr sat here 2026-10-17 to 2026-10-26 (#3120; its line followed
    # fleet-cost-idle-pool) and took its roster line back after #3145 seeded the manifest it needs.
    ("consistency-authorized-networks-probe", None),  # 2026-10-21, #3598 (Gemini reads seeded-c's leftover authorized-networks block as configured; #3448 for the 429s)
]
DEMOTED = {case for case, _ in DEMOTED_AFTER_THE_SPLIT}
# The demotions recorded before the freeze (#3495), whose lines sit in the
# frozen part after a named predecessor. Written out and not to grow: every
# later demotion goes in the sorted section, recorded with None.
# Empty since pdb-remediation-pr, the last of them, returned to the roster on
# 2026-10-26.
DEMOTED_INTO_THE_FROZEN_PART: frozenset[tuple[str, str]] = frozenset()
# Held-out seats withdrawn since #3495: a withdrawal moves the line to the
# nightly file's sorted section and records the case here, in the same edit
# that deletes its HELD_OUT_IN_PRESUBMIT entry.
WITHDRAWN_TO_SORTED: list[str] = []
# The pinned cases whose lines may sit in the sorted section rather than the
# frozen part. Nothing else may leave the frozen part.
ALLOWED_IN_THE_SORTED_SECTION = (
    {case for case, follows in DEMOTED_AFTER_THE_SPLIT if follows is None} | set(WITHDRAWN_TO_SORTED))
# Every case that was ever in the presubmit file before the nightly file was
# frozen (#3495): the split's TASKS array plus the two promoted since. Written
# out, so it doesn't grow with later promotions. A held-out seat among these
# must have a pinned nightly line; a seat for a case registered later can't.
EVER_IN_THE_PRESUBMIT_BEFORE_3495 = frozenset(PRESUBMIT_AT_SPLIT) | {
    "incident-triage-oom-event-probe",
    "pdb-remediation-pr",
}


def with_insertions(base, insertions):
    """``base`` with each (case, follows) pair inserted after its predecessor."""
    out = list(base)
    for case, follows in insertions:
        if follows is None:  # its line is in the nightly file's sorted section
            continue
        out.insert(out.index(follows) + 1, case)
    return out


OLD_SCRIPT_LINE = 'export BOOTSTRAP_ADMITTED="${BOOTSTRAP_ADMITTED:-a-probe,b-probe,c-probe}"\n'


class ParserTest(unittest.TestCase):
    def test_comments_blank_lines_and_whitespace_are_dropped(self):
        text = "# header\n\n  ./tasks/a-case/task.yaml  \n./tasks/b-case/task.yaml # trailing note\n#./tasks/c-case/task.yaml\n"
        self.assertEqual(eval_rosters.entries(text), ["./tasks/a-case/task.yaml", "./tasks/b-case/task.yaml"])
        self.assertEqual(eval_rosters.case_names(text), ["a-case", "b-case"])

    def test_a_malformed_entry_raises_rather_than_being_skipped(self):
        for bad in ("a-case", "tasks/a-case/task.yaml", "./tasks/a-case/task.yml", "./tasks/a case/task.yaml"):
            with self.subTest(entry=bad), self.assertRaises(ValueError):
                eval_rosters.case_names(f"./tasks/ok-case/task.yaml\n{bad}\n")

    def test_a_commented_out_case_path_is_reported(self):
        text = "./tasks/a-case/task.yaml\n# ./tasks/parked-case/task.yaml\n# see ./tasks/a-case/task.yaml above\n"
        self.assertEqual(eval_rosters.commented_out_cases(text), ["parked-case"])

    def test_the_blocking_roster_reads_the_file_shape(self):
        text = "# roster\na-probe\nb-probe  # since 09-01\n\nc-probe\n"
        self.assertEqual(eval_rosters.parse_blocking_roster(text), ["a-probe", "b-probe", "c-probe"])

    def test_the_old_script_shape_has_its_own_parser(self):
        # An era before 2026-09-15 comes from `git show <commit>:hack/ci-eval-pr.sh`.
        self.assertEqual(eval_rosters.parse_script_roster("#!/bin/bash\n" + OLD_SCRIPT_LINE), ["a-probe", "b-probe", "c-probe"])
        self.assertEqual(
            eval_rosters.parse_script_roster('BOOTSTRAP_ADMITTED="${BOOTSTRAP_ADMITTED:-a-probe b-probe}"'),
            ["a-probe", "b-probe"],
            "bench-gate accepts whitespace separators too",
        )
        with self.assertRaises(ValueError):
            eval_rosters.parse_script_roster("a-probe\nb-probe\n")

    def test_a_comment_quoting_the_override_syntax_is_not_the_roster(self):
        # The file parser never guesses: a header comment that shows the
        # BOOTSTRAP_ADMITTED override form is a comment, and the shell,
        # which strips comments first, must agree with it.
        text = "# a laptop run may set " + OLD_SCRIPT_LINE + "real-probe\n"
        self.assertEqual(eval_rosters.parse_blocking_roster(text), ["real-probe"])

    def test_the_real_files_parse(self):
        self.assertTrue(eval_rosters.presubmit_cases())
        self.assertTrue(eval_rosters.nightly_cases())
        self.assertTrue(eval_rosters.blocking_roster())


class NightlySortedSectionTest(unittest.TestCase):
    """The nightly file's last section, where new cases go (#3495).

    Properties instead of a pinned list: a case pull request adds one line at
    its place in case-id order, with its reason above it, and edits nothing a
    second such pull request edits.
    """

    def setUp(self):
        self.text = eval_rosters.NIGHTLY_CASES_FILE.read_text(encoding="utf-8")
        self.frozen, self.sorted_section, self.tail = eval_rosters.split_nightly(self.text)

    def test_the_section_is_in_case_id_order(self):
        # Python's order, the same as `LC_ALL=C sort`; a UTF-8 locale's sort
        # ignores the hyphens and can disagree.
        self.assertEqual(self.sorted_section, sorted(self.sorted_section))

    def test_no_case_is_listed_twice(self):
        everything = self.frozen + self.sorted_section
        self.assertEqual(len(everything), len(set(everything)))

    def test_every_case_in_the_section_has_its_reason_above_it(self):
        # The parser's own boundary rule keeps the marker's stanza from being
        # anyone's reason: a comment separated from the next entry by a blank
        # line isn't a reason (test_a_blank_line_follows_the_marker_stanza).
        # It keys a reason by its entry, here a ./tasks/<id>/task.yaml path.
        reasons = {
            eval_rosters.CASE_ENTRY_RE.match(entry).group(1): reason
            for entry, reason in eval_rosters.parse_lane_exclusions(self.tail).items()
            if eval_rosters.CASE_ENTRY_RE.match(entry)
        }
        for case in self.sorted_section:
            with self.subTest(case=case):
                self.assertTrue(
                    reasons.get(case),
                    "each case in the sorted section needs its own # lines directly above its entry; "
                    "a reason shared with the entry above it doesn't count")

    def test_a_blank_line_follows_the_marker_stanza(self):
        lines = self.tail.splitlines()
        i = 0
        while i < len(lines) and lines[i].lstrip().startswith("#"):
            i += 1
        if i < len(lines):  # an empty section ends at the stanza
            self.assertEqual(
                lines[i].strip(), "",
                "leave a blank line between the sorted-section marker's comment block and the first "
                "case's reason, so the marker's text isn't read as that case's reason")

    def test_the_marker_must_appear_exactly_once(self):
        entry = "./tasks/a-case/task.yaml\n"
        marker = eval_rosters.NIGHTLY_SORTED_SECTION_MARKER + " ──\n"
        with self.assertRaises(ValueError):
            eval_rosters.split_nightly(entry)
        with self.assertRaises(ValueError):
            eval_rosters.split_nightly(marker + entry + marker)
        self.assertEqual(eval_rosters.split_nightly(entry + marker + "# why\n" + entry)[:2],
                         (["a-case"], ["a-case"]))

    def test_the_command_prints_the_summary(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(eval_rosters.main(["--summary", "--repetitions", "2"]), 0)
        got = dict(line.split(": ", 1) for line in out.getvalue().splitlines())
        matrix = len(eval_rosters.presubmit_cases()) + len(eval_rosters.nightly_cases())
        self.assertEqual(got["nightly matrix cases"], str(matrix))
        self.assertEqual(got["nightly matrix units"], str(matrix * 2))

    def test_the_command_refuses_no_flag_and_a_repetitions_below_one(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(eval_rosters.main([]), 2)
        for bad in ("0", "-1", "x"):
            with self.subTest(repetitions=bad), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    eval_rosters.main(["--summary", "--repetitions", bad])

    def test_the_summary_counts_the_files(self):
        got = eval_rosters.summary(repetitions=3)
        matrix = len(eval_rosters.presubmit_cases()) + len(eval_rosters.nightly_cases())
        self.assertEqual(got["nightly matrix cases"], matrix)
        self.assertEqual(got["nightly matrix units"], matrix * 3)
        self.assertEqual(got["nightly-only cases"], len(self.frozen) + len(self.sorted_section))


class SplitLostNothingTest(unittest.TestCase):
    def test_the_presubmit_file_is_the_tasks_array_at_the_split_plus_the_promoted_less_the_held_out(self):
        expected = [
            c for c in with_insertions(PRESUBMIT_AT_SPLIT, PROMOTED_AFTER_THE_SPLIT)
            if c not in HELD_OUT_TO_NIGHTLY and c not in DEMOTED
        ]
        self.assertEqual(eval_rosters.presubmit_cases(), with_insertions(expected, HELD_OUT_IN_PRESUBMIT))

    def test_the_presubmit_runs_the_blocking_roster_plus_the_held_out_seats_and_nothing_else(self):
        # Decided 2026-09-22 (#1023): a case that cannot red a pull request
        # does not run on one -- less the documented exception, one seat per
        # coverage tracker (HELD_OUT_IN_PRESUBMIT), which runs without
        # blocking until its roster line lands. The script checks only that
        # the roster is a subset of the presubmit; the rest is policy, pinned here.
        seated = {case for case, _ in HELD_OUT_IN_PRESUBMIT}
        self.assertEqual([c for c in eval_rosters.presubmit_cases() if c not in seated], eval_rosters.blocking_roster())
        for case in seated:
            with self.subTest(case=case):
                self.assertIn(case, eval_rosters.presubmit_cases())
                self.assertNotIn(case, eval_rosters.blocking_roster())
                self.assertNotIn(case, eval_rosters.nightly_cases())
                pinned_nightly = (
                    NIGHTLY_AT_SPLIT + ADDED_AFTER_THE_SPLIT + MOVED_TO_NIGHTLY + ADDED_AFTER_THE_MOVED_BLOCK
                    + HELD_OUT_TO_NIGHTLY + ADDED_AT_THE_TAIL + ADDED_AFTER_THE_MOVE
                )
                if case in EVER_IN_THE_PRESUBMIT_BEFORE_3495:
                    self.assertIn(case, pinned_nightly, "a pre-freeze seat's nightly line is what moved")
                # A seat for a case registered after the freeze came from the
                # sorted section, which pins no history; its seat cites its record.
        for case in HELD_OUT_TO_NIGHTLY:
            if case in seated:
                continue
            with self.subTest(case=case):
                self.assertNotIn(case, eval_rosters.presubmit_cases())
                self.assertNotIn(case, eval_rosters.blocking_roster())
                self.assertIn(case, eval_rosters.nightly_cases())

    def test_the_blocking_roster_is_bootstrap_admitted_at_the_split_plus_the_admitted(self):
        self.assertEqual(
            eval_rosters.blocking_roster(),
            [c for c in with_insertions(ROSTER_AT_SPLIT, ADMITTED_AFTER_THE_SPLIT) if c not in DEMOTED],
        )

    def test_the_nightly_file_is_the_nightly_array_plus_the_moved_cases_less_the_promoted_plus_the_held_out(self):
        # The frozen part only: the sorted section below the marker is held by
        # NightlySortedSectionTest (#3495).
        frozen, sorted_section, _ = eval_rosters.split_nightly(
            eval_rosters.NIGHTLY_CASES_FILE.read_text(encoding="utf-8"))
        promoted = {case for case, _ in PROMOTED_AFTER_THE_SPLIT}
        seated = {case for case, _ in HELD_OUT_IN_PRESUBMIT}
        expected = [
            c
            for c in NIGHTLY_AT_SPLIT + ADDED_AFTER_THE_SPLIT + MOVED_TO_NIGHTLY + ADDED_AFTER_THE_MOVED_BLOCK
            if c not in promoted
        ]
        pinned = with_insertions(
            [
                c
                for c in expected + HELD_OUT_TO_NIGHTLY + ADDED_AT_THE_TAIL + ADDED_AFTER_THE_MOVE
                if c not in seated and c not in PARKED_AFTER_THE_MOVE
            ],
            DEMOTED_AFTER_THE_SPLIT,
        )
        # A demotion recorded with None, or a withdrawn seat, is held in the
        # sorted section instead; nothing else may leave the frozen part.
        self.assertEqual(
            frozen,
            [c for c in pinned if c not in ALLOWED_IN_THE_SORTED_SECTION],
            "the nightly file's frozen part (every entry above the sorted-section marker) changed: "
            "a new case goes in the sorted section at the foot of the file; a demotion is recorded "
            "in DEMOTED_AFTER_THE_SPLIT with None and a withdrawn seat in WITHDRAWN_TO_SORTED",
        )

    def test_a_demotion_since_the_freeze_goes_in_the_sorted_section(self):
        named = {(case, follows) for case, follows in DEMOTED_AFTER_THE_SPLIT if follows is not None}
        self.assertLessEqual(
            named, DEMOTED_INTO_THE_FROZEN_PART,
            "a demotion since #3495 puts its line in the nightly file's sorted section and is "
            "recorded in DEMOTED_AFTER_THE_SPLIT with None, not after a frozen-part predecessor")

    def test_a_demoted_case_is_in_the_nightly_only(self):
        for case in DEMOTED:
            with self.subTest(case=case):
                self.assertIn(case, eval_rosters.nightly_cases())
                self.assertNotIn(case, eval_rosters.presubmit_cases())
                self.assertNotIn(case, eval_rosters.blocking_roster())

    def test_a_promoted_case_is_in_the_presubmit_and_on_the_roster_and_not_in_the_nightly(self):
        for case, _ in PROMOTED_AFTER_THE_SPLIT:
            if case in DEMOTED:
                continue  # demoted since its promotion; test_a_demoted_case_is_in_the_nightly_only holds it
            with self.subTest(case=case):
                self.assertIn(case, eval_rosters.presubmit_cases())
                self.assertIn(case, eval_rosters.blocking_roster())
                self.assertNotIn(case, eval_rosters.nightly_cases())

    def test_the_script_no_longer_carries_the_arrays(self):
        # A literal array creeping back in would be a second source of truth
        # that OWNERS cannot see.
        src = SCRIPT.read_text(encoding="utf-8")
        for literal in ('\nTASKS=(\n  "', '\nNIGHTLY_TASKS=(\n  "', "BOOTSTRAP_ADMITTED:-reliability-pdb-probe"):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, src)

    def test_the_script_parses(self):
        result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


# The inject lane's exclusions at their introduction (#2039, 2026-09-25): the
# one presubmit case whose premise needs the chat front door. An edit to the
# file edits this set in the same pull request, for the reason the sets above
# are pinned.
INJECT_LANE_EXCLUDED = [
    "agent-kanban-smoke",  # #2039: grades kanban_create by the front door and the specialist's answer folded back, which the door's thread never gets
    "chat-voice-retry-says-it-is-retried",  # #2039: grades the front door's reply to a crashed card's wake; a card-wake replay runs on the api transport only
    "chat-voice-final-attempt-is-not-retried",  # the same for a card's last-attempt wake
    "chat-voice-failure-leads-with-fact",  # #2039: grades the front door's reply to a blocked card's wake; same reason
    "chat-question-wake-stays-silent",  # #2039: grades the front door's silence on a posted question's wake; same reason
    "chat-question-typed-answer-fresh-session",  # the same for a typed answer in a session the wake never reached
    "chat-question-click-answer-stays-silent",  # the same for an answer given by clicking the question's button
    "chat-voice-ack-names-target",  # #2039: grades the front door's delegation ack with the specialist's answer after it, which the door's thread never gets
    "chat-reset-history-names-the-command",  # #2039: grades the front door's answer to a reset request; excluded under cli, not yet graded on api
    "chat-routing-board-read",  # the same for the front door reading the board instead of filing a card
    "chat-routing-fleet-question",  # the same as the ack: the answer is the specialist's, which the door's thread never gets
    "first-install-hello-running",  # #2039: grades the chat profile's onboarding greeting; excluded under cli, not yet graded on api
    "first-install-hello-done",  # the same once the first-look scan has finished
    "chat-voice-answer-first",  # #2039: grades the card result the front door delivers; the inject door files no card
    "chat-fanout-fleet-restarts-rows",  # #2039: grades the front door's ack and the fan-out under its one card; same door
    "chat-question-asked-to-choose-pauses",  # #2039: grades the worker's needs_input block under the door's one card; same door
    "chat-failure-reply-declares-next-step",  # #2039: grades the front door's reply to a blocked card's wake; a card-wake replay
    "chat-answer-declares-next-step",  # #2039: grades the worker's answer under the door's one card; same door
    "chat-answer-tangent-offer-declares-no-step",  # the same
    "fleet-audit-request-line",  # #2619: grades the worker's answer the api lane folds into the reply; the door's thread never gets it
]
# The directives a case's prompt opens with to replay a wake into the chat
# front door (bench/kube_agents_bench/card_wake.py); the harness errors such
# a run on any transport but api.
FRONT_DOOR_WAKE_DIRECTIVES = ("[bench:card-failure-wake]", "[bench:slack-question-wake]")
# Check types that read what only the chat front door's card round trip
# delivers; on the inject lane there is no card, so the check fails rather
# than being set aside (bench/README.md, the transport table).
FRONT_DOOR_DELIVERY_CHECK_TYPES = frozenset({"answer_first", "report_store_reply"})
# Each exclusion's api-lane tier, pinned beside it: an entry is not a
# demotion, so a case that leaves its tier's file while still excluded reds.
INJECT_LANE_EXCLUDED_TIER = {
    "agent-kanban-smoke": "presubmit",
    "chat-voice-retry-says-it-is-retried": "nightly",
    "chat-voice-final-attempt-is-not-retried": "nightly",
    "chat-voice-failure-leads-with-fact": "nightly",
    "chat-question-wake-stays-silent": "nightly",
    "chat-question-typed-answer-fresh-session": "nightly",
    "chat-question-click-answer-stays-silent": "nightly",
    "chat-voice-ack-names-target": "nightly",
    "chat-reset-history-names-the-command": "nightly",
    "chat-routing-board-read": "nightly",
    "chat-routing-fleet-question": "nightly",
    "first-install-hello-running": "nightly",
    "first-install-hello-done": "nightly",
    "chat-voice-answer-first": "nightly",
    "chat-fanout-fleet-restarts-rows": "nightly",
    "chat-question-asked-to-choose-pauses": "nightly",
    "chat-failure-reply-declares-next-step": "nightly",
    "chat-answer-declares-next-step": "nightly",
    "chat-answer-tangent-offer-declares-no-step": "nightly",
    "fleet-audit-request-line": "nightly",
}


def positive_check_types(node, negations: int = 0) -> set:
    """The check types a task REQUIRES to pass: every `type:` under an even
    number of negating `type: none` compounds (parity, as the bench's
    `_negates_every_leaf` reads them) and under no `any` compound, where a
    leaf is one alternative and not required. The other compounds
    (`sequence`, `parallel`, `all`) are conjunctive and transparent."""
    if isinstance(node, dict):
        if node.get("type") == "any":
            return set()
        if node.get("type") == "none":
            found = set()
            for value in node.values():
                found |= positive_check_types(value, negations + 1)
            return found
        found = {node["type"]} if isinstance(node.get("type"), str) and negations % 2 == 0 else set()
        for value in node.values():
            found |= positive_check_types(value, negations)
        return found
    if isinstance(node, list):
        found = set()
        for item in node:
            found |= positive_check_types(item, negations)
        return found
    return set()


class GitLabLaneTest(unittest.TestCase):
    """hack/eval/gitlab-cases.txt: what the GitLab lane's presubmit
    runs (kube-agents#2394). Registered cases only, every entry a case that
    grades the forge."""

    FORGE_CHECKS = {"pull_request_opened", "pull_request_diff_contains", "ledger_issue_contains"}
    PINNED = ["vcs-spent-branch-reuse", "vcs-review-feedback-read-back", "pdb-remediation-pr"]

    def test_the_file_is_the_pinned_set(self):
        self.assertEqual(eval_rosters.gitlab_cases(), self.PINNED)

    def test_every_lane_case_is_registered(self):
        registered = eval_rosters.presubmit_cases() + eval_rosters.nightly_cases()
        for case in eval_rosters.gitlab_cases():
            with self.subTest(case=case):
                self.assertIn(case, registered, "the lane picks from the catalogue, it does not register")

    def test_every_lane_case_grades_the_forge(self):
        import yaml

        for case in eval_rosters.gitlab_cases():
            with self.subTest(case=case):
                doc = yaml.safe_load((REPO_ROOT / "bench" / "tasks" / case / "task.yaml").read_text(encoding="utf-8"))
                self.assertTrue(positive_check_types(doc) & self.FORGE_CHECKS, f"{case} grades nothing on the forge; a chat probe proves nothing about GitLab")

    def test_a_negated_forge_check_does_not_count(self):
        # "The agent did not open a merge request" proves nothing on GitLab.
        negated = {"verification_spec": [{"type": "none", "checks": [{"type": "pull_request_opened"}]}]}
        self.assertFalse(positive_check_types(negated) & self.FORGE_CHECKS)
        self.assertTrue(positive_check_types({"verification_spec": [{"type": "pull_request_opened"}]}) & self.FORGE_CHECKS)
        # A none under a none undoes it, as the bench reads it (bench/tests/test_cases.py, doubly-negated).
        double = {"verification_spec": [{"type": "none", "checks": [{"type": "none", "checks": [{"type": "pull_request_opened"}]}]}]}
        self.assertTrue(positive_check_types(double) & self.FORGE_CHECKS)
        # Under `any` the forge check is one alternative, not a requirement: a chat answer could pass the case instead.
        either = {"verification_spec": [{"type": "any", "checks": [{"type": "pull_request_opened"}, {"type": "report_contains"}]}]}
        self.assertFalse(positive_check_types(either) & self.FORGE_CHECKS)
        self.assertTrue(positive_check_types({"verification_spec": [{"type": "all", "checks": [{"type": "ledger_issue_contains"}]}]}) & self.FORGE_CHECKS)

    def test_no_commented_out_case_path(self):
        self.assertEqual(eval_rosters.commented_out_cases(eval_rosters.GITLAB_CASES_FILE.read_text(encoding="utf-8")), [])


class InjectLaneExclusionsTest(unittest.TestCase):
    """hack/eval/inject-lane-exclusions.txt: the lane-level list, checked here.

    A per-case marker would change what the api lane does to the case; a
    list read only under AGENT_TRANSPORT=inject changes nothing there. The
    shape is FIXTURE_NOT_READY's -- an exclusion with a reason -- and every
    entry must name a registered case, carry a reason, and cite the issue
    that decides when the entry goes.
    """

    def test_the_parser_reads_the_reason_block_above_each_entry(self):
        text = (
            "# header, not a reason\n"
            "\n"
            "# #1: first line\n"
            "# second line\n"
            "a-case\n"
            "b-case  # trailing note is not the reason\n"
            "\n"
            "c-case\n"
            "\n"
            "##2: marker with no space keeps its issue number\n"
            "d-case\n"
        )
        self.assertEqual(
            eval_rosters.parse_lane_exclusions(text),
            {"a-case": "#1: first line second line", "b-case": "", "c-case": "", "d-case": "#2: marker with no space keeps its issue number"},
        )
        # The shell reads the same file with the plain entry parser.
        self.assertEqual(eval_rosters.entries(text), ["a-case", "b-case", "c-case", "d-case"])

    def test_the_file_is_the_pinned_set(self):
        self.assertEqual(list(eval_rosters.inject_lane_exclusions()), INJECT_LANE_EXCLUDED)

    def test_the_blocking_roster_cases_it_excludes_are_the_pinned_set(self):
        # The merge-blocking presubmit runs on the inject lane, so a roster
        # case listed here leaves what can red a pull request: hack/ci-eval-pr.sh
        # drops it from the matrix and from BOOTSTRAP_ADMITTED there. It keeps
        # its seat on the today lane, which grades over the api transport but
        # gates nothing. Adding another is that decision, made here and in
        # docs/eval-gate-roster.md ("The inject lane"), not a side effect of an
        # exclusion entry.
        roster = set(eval_rosters.blocking_roster())
        self.assertEqual([case for case in eval_rosters.inject_lane_exclusions() if case in roster], ["agent-kanban-smoke"])

    def test_every_exclusion_names_a_registered_case(self):
        registered = set(eval_rosters.presubmit_cases()) | set(eval_rosters.nightly_cases())
        for case in eval_rosters.inject_lane_exclusions():
            with self.subTest(case=case):
                self.assertTrue((REPO_ROOT / "bench" / "tasks" / case / "task.yaml").is_file(), f"{case} has no task.yaml")
                self.assertIn(case, registered, f"{case} runs on no lane, so there is nothing to exclude it from")

    def test_every_exclusion_carries_a_reason_that_names_an_issue(self):
        for case, reason in eval_rosters.inject_lane_exclusions().items():
            with self.subTest(case=case):
                self.assertTrue(reason, f"{case}: no reason in the comment block above it")
                self.assertRegex(reason, eval_rosters.ISSUE_REFERENCE_RE, f"{case}: the reason names no issue")

    def test_every_registered_wake_replay_is_excluded(self):
        # A replay errors on the inject lane, so an unlisted one reds there
        # every repetition; the docs' rule is enforced here, on the loaded
        # prompt's first line as card_wake.parse reads it.
        import yaml

        excluded = eval_rosters.inject_lane_exclusions()
        for case in set(eval_rosters.presubmit_cases()) | set(eval_rosters.nightly_cases()):
            doc = yaml.safe_load((REPO_ROOT / "bench" / "tasks" / case / "task.yaml").read_text(encoding="utf-8"))
            lines = str(doc.get("prompt") or "").strip().splitlines()
            if lines and lines[0].strip() in FRONT_DOOR_WAKE_DIRECTIVES:
                with self.subTest(case=case):
                    self.assertIn(case, excluded, f"{case} replays a wake but is not in {eval_rosters.INJECT_LANE_EXCLUSIONS_FILE.name}")

    def test_every_registered_front_door_delivery_case_is_excluded(self):
        # answer_first fails on the inject lane rather than being set aside,
        # so an unlisted case carrying one reds there every repetition.
        import yaml

        def check_types(node):
            if isinstance(node, dict):
                if isinstance(node.get("type"), str):
                    yield node["type"]
                for value in node.values():
                    yield from check_types(value)
            elif isinstance(node, list):
                for value in node:
                    yield from check_types(value)

        excluded = eval_rosters.inject_lane_exclusions()
        for case in set(eval_rosters.presubmit_cases()) | set(eval_rosters.nightly_cases()):
            doc = yaml.safe_load((REPO_ROOT / "bench" / "tasks" / case / "task.yaml").read_text(encoding="utf-8"))
            if FRONT_DOOR_DELIVERY_CHECK_TYPES & set(check_types(doc.get("verification_spec"))):
                with self.subTest(case=case):
                    self.assertIn(case, excluded, f"{case} reads a delivered card result but is not in {eval_rosters.INJECT_LANE_EXCLUSIONS_FILE.name}")

    def test_an_exclusion_is_not_a_demotion(self):
        # The api lane's roster is untouched by an entry here: an excluded
        # presubmit case still runs on every pull request and can still red
        # one, and an excluded nightly case still runs every night.
        self.assertEqual(sorted(INJECT_LANE_EXCLUDED_TIER), sorted(INJECT_LANE_EXCLUDED))
        for case, tier in INJECT_LANE_EXCLUDED_TIER.items():
            with self.subTest(case=case, tier=tier):
                if tier == "presubmit":
                    self.assertIn(case, eval_rosters.presubmit_cases())
                    self.assertIn(case, eval_rosters.blocking_roster())
                else:
                    self.assertIn(case, eval_rosters.nightly_cases())


# The api lane's exclusions at their introduction (#3263): the cases only the
# inject door can carry. An edit to the file edits this set.
API_LANE_EXCLUDED = [
    "chat-stop-halts-delegated-remediation",  # #3263: the harness types `stop` through the door
]


class ApiLaneExclusionsTest(unittest.TestCase):
    """hack/eval/api-lane-exclusions.txt: the inject lane's mirror, checked the same way."""

    def test_the_file_is_the_pinned_set(self):
        self.assertEqual(list(eval_rosters.api_lane_exclusions()), API_LANE_EXCLUDED)

    def test_every_exclusion_names_a_registered_nightly_case(self):
        # Not a presubmit case: the presubmit matrix also runs on the today
        # lane over api, where a case excluded here would hold a seat it
        # never fills, and a case new to the inject door earns its record in
        # the nightly first.
        for case in eval_rosters.api_lane_exclusions():
            with self.subTest(case=case):
                self.assertTrue((REPO_ROOT / "bench" / "tasks" / case / "task.yaml").is_file(), f"{case} has no task.yaml")
                self.assertIn(case, eval_rosters.nightly_cases())
                self.assertNotIn(case, eval_rosters.presubmit_cases())
                self.assertNotIn(case, eval_rosters.blocking_roster())

    def test_every_exclusion_carries_a_reason_that_names_an_issue(self):
        for case, reason in eval_rosters.api_lane_exclusions().items():
            with self.subTest(case=case):
                self.assertTrue(reason, f"{case}: no reason in the comment block above it")
                self.assertRegex(reason, eval_rosters.ISSUE_REFERENCE_RE, f"{case}: the reason names no issue")

    def test_no_case_is_excluded_on_both_lanes(self):
        self.assertFalse(set(eval_rosters.api_lane_exclusions()) & set(eval_rosters.inject_lane_exclusions()))

    def test_every_registered_stopped_ask_is_excluded(self):
        # A stopped ask errors on the api transport, so an unlisted one would
        # error there every repetition. The directive is read from the module
        # the harness reads it from, so the two cannot disagree.
        import yaml

        sys.path.insert(0, str(REPO_ROOT / "bench"))
        from kube_agents_bench.stop_hook import is_stopped_ask

        excluded = eval_rosters.api_lane_exclusions()
        for case in set(eval_rosters.presubmit_cases()) | set(eval_rosters.nightly_cases()):
            doc = yaml.safe_load((REPO_ROOT / "bench" / "tasks" / case / "task.yaml").read_text(encoding="utf-8"))
            if is_stopped_ask(str(doc.get("prompt") or "")):
                with self.subTest(case=case):
                    self.assertIn(case, excluded, f"{case} is a stopped ask but is not in {eval_rosters.API_LANE_EXCLUSIONS_FILE.name}")


# The inject lane's safeguards at their introduction (#2079, 2026-09-28): the
# one entry every case on the lane carries beside its own. An edit to the
# file edits this set in the same pull request, for the reason the sets
# above are pinned.
INJECT_LANE_SAFEGUARDS = [
    "no-github-writes-the-case-did-not-request",  # #2079: a none-wrapped github_writes, catastrophic
]
# The registered cases whose checks request a pull request, at the
# safeguard's introduction: what hack/ci-eval-pr.sh runs in the fan-out's
# second phase on the inject lane, after every other unit has finished. A
# new requesting case edits this set in the same pull request; the check
# types that count are lane.REQUESTING_CHECK_TYPES.
INJECT_LANE_REQUESTING = [
    "cluster-agent-crashloop-fix-request",
    # cluster-setting-request-proposes-pr: asked to turn on Binary Authorization, it
    # may propose the change as a pull request; its objective's
    # pull_request_opened leaf allows one.
    "cluster-setting-request-proposes-pr",
    # obtainability-remediation-proposal was listed in the safeguards file's
    # `requesting:` while the persona answered its "propose a manifest" prompt
    # with a pull request; the persona's rule now puts that manifest in the
    # reply (SOUL.md §3, item 3), so the allowance is gone and the lane's
    # github_writes safeguard grades the case at zero like any other.
    # obtainability-refusal-direct-mutation, below: refused the direct apply,
    # it may propose the change as a pull request (#2610, 2026-10-10); its
    # objective's pull_request_opened leaf allows one.
    "obtainability-refusal-direct-mutation",
    "pdb-remediation-pr",
    "rca-remediation-pr",
    # scale-request-proposes-pr: asked to scale a Deployment, it may propose
    # the change as a pull request; its objective's pull_request_opened leaf
    # allows one.
    "scale-request-proposes-pr",
    "vcs-review-feedback-read-back",
    # Also listed in `requesting:`, for the pull request it opens and closes
    # beside the one its check grades.
    "vcs-spent-branch-reuse",
]
LANE_SAFEGUARD_LEAF_TYPE = "github_writes"


def _leaf_types(node) -> list[str]:
    if isinstance(node, dict):
        children = [node.get(k) for k in ("checks", "check") if node.get(k) is not None]
        if not children:
            return [str(node.get("type") or "")]
        return [t for child in children for t in _leaf_types(child)]
    if isinstance(node, list):
        return [t for item in node for t in _leaf_types(item)]
    return []


class InjectLaneSafeguardsTest(unittest.TestCase):
    """hack/eval/inject-lane-safeguards.yaml: the entries hack/ci-eval-pr.sh
    appends to every case it runs on the inject lane (#2079).

    A lane-level list rather than fifty per-case edits, for the reason the
    exclusions are: a per-case entry would change what the api lane grades.
    What has to hold: the file is the pinned set; every entry is a
    none-wrapped `github_writes` safeguard at catastrophic severity (a leaf
    that PASSES on a write, so `none` is what makes it a safeguard); no
    registered case declares an entry of the same name, since devops-bench
    refuses a duplicate name as a parse error on every repetition of that
    case; and the set of cases that request a pull request is pinned,
    because the fan-out runs them in a phase of their own after every other
    unit (the check dates writes and cannot tell a concurrent sibling's pull
    request from this repetition's).
    """

    @staticmethod
    def entries() -> list[dict]:
        import yaml

        doc = yaml.safe_load(eval_rosters.INJECT_LANE_SAFEGUARDS_FILE.read_text(encoding="utf-8"))
        return doc["safeguards"]

    @staticmethod
    def task_spec(case: str) -> list[dict]:
        import yaml

        doc = yaml.safe_load((REPO_ROOT / "bench" / "tasks" / case / "task.yaml").read_text(encoding="utf-8"))
        return doc.get("verification_spec") or []

    def lane_cases(self) -> list[str]:
        """Every case the lane can run: the presubmit matrix on the presubmit
        tier, plus the nightly file when EVAL_TIER=nightly, less the
        exclusions -- the safeguards step runs over TASKS after the tier
        switch, so both tiers carry the entry."""
        excluded = set(eval_rosters.inject_lane_exclusions())
        return [c for c in eval_rosters.presubmit_cases() + eval_rosters.nightly_cases() if c not in excluded]

    def test_the_file_is_the_pinned_set(self):
        self.assertEqual([e["name"] for e in self.entries()], INJECT_LANE_SAFEGUARDS)

    def test_every_entry_is_a_none_wrapped_github_writes_catastrophic_safeguard(self):
        for entry in self.entries():
            with self.subTest(entry=entry["name"]):
                self.assertEqual(entry["role"], "safeguard")
                self.assertEqual(entry["severity"], "catastrophic")
                self.assertEqual(entry["check"]["type"], "none")
                self.assertEqual(_leaf_types(entry["check"]), [LANE_SAFEGUARD_LEAF_TYPE])

    def test_no_registered_case_reuses_a_lane_entry_name(self):
        names = set(INJECT_LANE_SAFEGUARDS)
        registered = set(eval_rosters.presubmit_cases()) | set(eval_rosters.nightly_cases())
        for case in sorted(registered):
            with self.subTest(case=case):
                declared = {str(e.get("name")) for e in self.task_spec(case) if isinstance(e, dict)}
                self.assertFalse(declared & names, f"{case} declares a lane safeguard's name")

    def test_the_requesting_cases_are_the_pinned_set(self):
        """A `github_writes` check dates writes, and the fan-out runs cases
        side by side against one repository, so a case that requests a pull
        request has to be kept away from the others: the script runs the set
        the lane module computes from the specs in a second phase, after
        every other unit has finished. The set is pinned here so a new
        requesting case is a reviewed edit, and derived from the same module
        the script runs so the two cannot disagree."""
        sys.path.insert(0, str(REPO_ROOT / "bench"))
        from kube_agents_bench import lane

        listed = lane.load_lane_requesting(eval_rosters.INJECT_LANE_SAFEGUARDS_FILE)
        requesting = [c for c in self.lane_cases() if lane.requested_pull_requests(self.task_spec(c)) > 0 or c in listed]
        # A listed case is registered and on the lane, and its own checks do
        # not yet request as many as it is allowed: once they do, the entry
        # is a leftover.
        for case, count in listed.items():
            with self.subTest(listed=case):
                self.assertIn(case, self.lane_cases())
                requested = lane.requested_pull_requests(self.task_spec(case))
                self.assertLess(requested, count, f"{case}'s own checks request {requested} pull request(s) now, at least the {count} `requesting:` allows it; drop the entry")
        self.assertEqual(sorted(requesting), INJECT_LANE_REQUESTING)
        # The plain leaf walk here agrees with the module's on every lane case.
        for case in self.lane_cases():
            with self.subTest(case=case):
                types = [t for e in self.task_spec(case) if isinstance(e, dict) for t in _leaf_types(e.get("check"))]
                self.assertEqual(
                    sum(1 for t in types if t in lane.REQUESTING_CHECK_TYPES),
                    lane.requested_pull_requests(self.task_spec(case)),
                )

    def test_the_spent_branch_case_is_allowed_both_its_pull_requests(self):
        # It opens one pull request and closes it to spend the branch name,
        # and its reply names both; its one `pull_request_opened` leaf alone
        # would allow the second and fail the safeguard on the first.
        sys.path.insert(0, str(REPO_ROOT / "bench"))
        from kube_agents_bench import lane

        listed = lane.load_lane_requesting(eval_rosters.INJECT_LANE_SAFEGUARDS_FILE)
        case = "vcs-spent-branch-reuse"
        self.assertEqual(max(lane.requested_pull_requests(self.task_spec(case)), listed.get(case, 0)), 2)

    def test_the_lane_still_runs_a_case_that_requests_nothing(self):
        # The safeguard changes what a case is graded on, not whether it
        # runs: the lane's matrix is the presubmit file less the exclusions,
        # exactly as before this file existed.
        self.assertTrue(self.lane_cases())
        self.assertNotIn("agent-kanban-smoke", self.lane_cases())



# ─── The seat bar (#3968, section 5) ──────────────────────────────────────────
# docs/eval-gate-roster.md, "The five rules a seat meets". Rules (a)
# and (b) are judgments on the record, asked for by the pull request
# template; (c), (d) and (e) are read from the files and held here.
#
# The blocking-roster seats that break rule (c) or (d), as (case, rule, issue,
# what is missing). The issue says what would clear the rule; the edit that
# clears it deletes the entry here, and SeatBarTest fails an entry whose case
# no longer breaks its rule. Kept the way HELD_OUT_IN_PRESUBMIT is: a short
# written list, each line with its issue, never a way to seat a new case.
SEAT_BAR_EXEMPT = [
    ("agent-kanban-smoke", "c-slot", 3984, "names seeded-a, seeded-b and seeded-c in its check and declares no fixture role for any of them"),
    ("vcs-spent-branch-reuse", "c-repository", 3985, "opens pull requests in the pool's GitOps repository, and the scan asserts nothing about that repository"),
    ("pdb-remediation-pr", "c-repository", 3994, "opens a pull request in the pool's GitOps repository and needs checkout-gateway's manifest there, and the scan asserts nothing about that repository"),
]
FLEET_FIXTURE_STATE_SCRIPT = REPO_ROOT / "hack" / "fleet-fixture-state.py"
FLEET_CATALOG = REPO_ROOT / "bench" / "tf" / "fleet" / "fixtures.json"
FLEET_OVERLAY = REPO_ROOT / "docs" / "designs" / "fleet-fixtures.yaml"
# A seeded cluster named in a check's text, `seeded-a`, or by placeholder,
# `{cluster:a}`: a read of that slot whatever the case's `fixtures:` says.
# Bounded so seeded-a-canary and the seeded-debug namespace are not slot a or
# d; only a slot the overlay declares counts, so a deliberately absent
# `seeded-z` is not a read.
SEEDED_CLUSTER_NAME_RE = re.compile(r"(?<![\w-])seeded-([a-z])(?![\w-])")
CLUSTER_PLACEHOLDER_RE = re.compile(r"\{cluster:([a-z])\}")
# A pattern that names several slots at once -- `{cluster:any}`, or a regex
# spelling such as `seeded-(b|c)`, `seeded-[bc]` or `seeded-\w` -- reads every
# slot the overlay declares, so the case lists a role on each (the case
# format's rule for a case that depends on every slot).
EVERY_SLOT_RE = re.compile(r"\{cluster:any\}|(?<![\w-])seeded-[(\[\\]")
# The rule ids a finding carries: rule (c)'s three mechanisms apart, so an
# exemption covers the one its issue is about and nothing else.
SEAT_BAR_RULES = frozenset({"c-role", "c-slot", "c-repository", "d"})
# The subject constant hack/fleet-fixture-state.py defines once it reads a
# pool `*-infra` repository, the way CLUSTER_SUBJECT names the GKE cluster.
# Until the scan defines it, no role has a repository assertion, so nothing a
# catalog edit alone writes can clear that part of rule (c): the scan has to
# run it. The one place that lookup lives, so the pool-repository assertions
# the scan learns (the checkout-gateway manifest every pool repository must
# hold, #3120) clear rule (c) for the cases on that role the day they land.
SCAN_REPOSITORY_SUBJECT_ATTR = "REPOSITORY_SUBJECT"


def load_scan():
    """hack/fleet-fixture-state.py as a module: the hourly scan's own code."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("fleet_fixture_state", FLEET_FIXTURE_STATE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_scan_assertions(scan) -> dict[str, list]:
    """{role: its validated `state` entries}, read by the scan's own loader:
    what the hourly scan (scripts/eval_dashboard/fixture_state.py) asserts,
    and nothing it would refuse."""
    return {role: entry["state"] for role, entry in scan.load_assertions(FLEET_CATALOG).items()}


def repository_asserted_roles(assertions: dict[str, list], repository_subject: str | None) -> set[str]:
    """The roles with a `state` entry on the scan's repository subject; none
    while the scan defines no such subject."""
    if not repository_subject:
        return set()
    return {role for role, state in assertions.items()
            if any(str(e.get("subject", "")).split("/", 1)[0].split("?", 1)[0] == repository_subject for e in state)}


def overlay_role_slots() -> dict[str, str | None]:
    """{role: slot} from docs/designs/fleet-fixtures.yaml: every seeded-fleet
    role, the project-scoped ones (slot None) included."""
    import yaml

    doc = yaml.safe_load(FLEET_OVERLAY.read_text(encoding="utf-8"))
    return {f["role"]: f.get("slot") for f in doc["fixtures"]}


def deterministic_leaves(node, negations: int = 0) -> list[str]:
    """The check leaves that grade something the run must show: every leaf
    under an even number of `none` compounds. Unlike positive_check_types, an
    `any` counts, since each alternative is still an exact check."""
    if isinstance(node, list):
        return [t for item in node for t in deterministic_leaves(item, negations)]
    if not isinstance(node, dict):
        return []
    children = [node.get(k) for k in ("checks", "check") if node.get(k) is not None]
    if not children:
        return [str(node["type"])] if node.get("type") and negations % 2 == 0 else []
    inner = negations + (1 if node.get("type") == "none" else 0)
    return [t for child in children for t in deterministic_leaves(child, inner)]


def seat_bar_findings(doc: dict, *, requests_pull_request: bool, scan: dict[str, list],
                      repository_roles: set[str], role_slots: dict[str, str | None]) -> list[tuple[str, str]]:
    """What a case's task.yaml breaks of rules (c) and (d), as (rule id, why);
    the ids are SEAT_BAR_RULES."""
    found = []
    spec = doc.get("verification_spec") or []
    objectives = [e for e in spec if isinstance(e, dict) and e.get("role") == "objective"]
    if not any(deterministic_leaves(e.get("check")) for e in objectives):
        found.append(("d", "no objective carries a deterministic check: graded by the judge alone"))
    roles = list(doc.get("fixtures") or [])
    for role in roles:
        if not scan.get(role):
            found.append(("c-role", f"fixture role {role} has no `state` assertion the seeded-fleet scan runs"))
    declared_slots = {role_slots.get(r) for r in roles}
    known_slots = {slot for slot in role_slots.values() if slot}
    spec_text = repr(spec)
    named = set(SEEDED_CLUSTER_NAME_RE.findall(spec_text)) | set(CLUSTER_PLACEHOLDER_RE.findall(spec_text))
    if EVERY_SLOT_RE.search(spec_text):
        named |= known_slots
    for slot in sorted((named & known_slots) - declared_slots):
        found.append(("c-slot", f"its checks read seeded slot {slot} and it declares no fixture role there"))
    # A case with roles writes about them (pdb-remediation-pr's manifest for
    # checkout-gateway), so the assertion sits on one of its roles; a case
    # with none needs the scan to read the pool repository at all.
    if requests_pull_request and not (set(roles) & repository_roles if roles else repository_roles):
        found.append(("c-repository", "it writes to the pool's GitOps repository, and the scan asserts nothing there it depends on"))
    return found


class SeatBarTest(unittest.TestCase):
    """Rules (c) and (d) of docs/eval-gate-roster.md, "The admission bar", on
    every blocking-roster case, read from its task.yaml."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(REPO_ROOT / "bench"))
        from kube_agents_bench import lane

        scan = load_scan()
        cls.scan = load_scan_assertions(scan)
        cls.repository_roles = repository_asserted_roles(cls.scan, getattr(scan, SCAN_REPOSITORY_SUBJECT_ATTR, None))
        cls.role_slots = overlay_role_slots()
        cls.lane_requesting = eval_rosters.parse_lane_requesting(eval_rosters.INJECT_LANE_SAFEGUARDS_FILE.read_text(encoding="utf-8"))
        cls.lane = lane

    def findings(self, case: str) -> list[tuple[str, str]]:
        import yaml

        doc = yaml.safe_load((REPO_ROOT / "bench" / "tasks" / case / "task.yaml").read_text(encoding="utf-8"))
        return self.findings_for(doc, eval_rosters.requests_a_pull_request(case, self.lane_requesting))

    def findings_for(self, doc: dict, requests: bool = False, **override) -> list[tuple[str, str]]:
        kwargs = {"scan": self.scan, "repository_roles": self.repository_roles, "role_slots": self.role_slots}
        kwargs.update(override)
        return seat_bar_findings(doc, requests_pull_request=requests, **kwargs)

    def test_every_roster_case_clears_c_and_d_or_is_exempt(self):
        exempt = {(case, rule) for case, rule, _, _ in SEAT_BAR_EXEMPT}
        for case in eval_rosters.blocking_roster():
            with self.subTest(case=case):
                broken = [(rule, why) for rule, why in self.findings(case) if (case, rule) not in exempt]
                self.assertEqual(broken, [], f"{case} does not clear the seat bar (docs/eval-gate-roster.md, \"The five rules a seat meets\")")

    def test_every_exemption_is_a_roster_seat_that_still_breaks_its_rule(self):
        roster = eval_rosters.blocking_roster()
        for case, rule, issue, why in SEAT_BAR_EXEMPT:
            with self.subTest(case=case, rule=rule):
                self.assertIn(rule, SEAT_BAR_RULES)
                self.assertIn(case, roster, "an exemption is for a seat; a case off the roster drops its entry")
                self.assertGreater(issue, 0)
                self.assertTrue(why)
                self.assertIn(rule, {r for r, _ in self.findings(case)}, f"{case} clears rule ({rule}) now; delete its exemption (#{issue})")

    def test_the_exemption_list_names_each_seat_and_rule_once(self):
        pairs = [(case, rule) for case, rule, _, _ in SEAT_BAR_EXEMPT]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_a_judge_only_spec_breaks_rule_d(self):
        report = {"type": "report_contains", "required_phrases": ["debug-binding"]}
        self.assertIn("d", {r for r, _ in self.findings_for({"verification_spec": []})})
        # Safeguards alone grade what must not happen, never the defect.
        self.assertIn("d", {r for r, _ in self.findings_for({"verification_spec": [
            {"name": "s", "role": "safeguard", "severity": "catastrophic", "check": report}]})})
        # An objective whose only leaf sits under `none` names nothing the run found.
        self.assertIn("d", {r for r, _ in self.findings_for({"verification_spec": [
            {"name": "o", "role": "objective", "check": {"type": "none", "checks": [report]}}]})})
        for check in (report, {"type": "any", "checks": [report, {"type": "tool_called"}]},
                      {"type": "none", "checks": [{"type": "none", "checks": [report]}]}):
            with self.subTest(check=check):
                self.assertNotIn("d", {r for r, _ in self.findings_for({"verification_spec": [
                    {"name": "o", "role": "objective", "check": check}]})})

    def test_an_unasserted_seeded_role_breaks_rule_c(self):
        objective = [{"name": "o", "role": "objective", "check": {"type": "report_contains"}}]
        doc = {"fixtures": ["version-laggard"], "verification_spec": objective}
        self.assertEqual(self.findings_for(doc), [])
        unasserted = dict(self.scan, **{"version-laggard": []})
        self.assertEqual([r for r, _ in self.findings_for(doc, scan=unasserted)], ["c-role"])
        # A project-scoped role is not on a cluster the scan reads at all.
        self.assertEqual([r for r, _ in self.findings_for({"fixtures": ["orphan-disks"], "verification_spec": objective})], ["c-role"])

    def test_a_seeded_cluster_named_without_its_role_breaks_rule_c(self):
        def doc(phrase, fixtures=None):
            return {"fixtures": fixtures, "verification_spec": [
                {"name": "o", "role": "objective", "check": {"type": "report_contains", "required_phrases": [phrase]}}]}

        self.assertEqual([r for r, _ in self.findings_for(doc("seeded-b"))], ["c-slot"])
        self.assertEqual([r for r, _ in self.findings_for(doc("{cluster:b}"))], ["c-slot"])
        self.assertEqual(self.findings_for(doc("seeded-b", ["version-laggard"])), [])
        self.assertEqual(self.findings_for(doc("seeded-debug")), [], "a namespace is not slot d")
        self.assertEqual(self.findings_for(doc("seeded-a-canary")), [])
        self.assertEqual(self.findings_for(doc("seeded-z")), [], "no slot z: a cluster named as absent is not a read")
        # A pattern naming several slots at once reads every declared slot.
        every = {slot for slot in self.role_slots.values() if slot}
        for phrase in ("{cluster:any}", "seeded-(b|c)", "seeded-[bc]", r"seeded-\w"):
            with self.subTest(phrase=phrase):
                self.assertEqual(sorted(why.split("slot ")[1].split()[0] for _, why in self.findings_for(doc(phrase))), sorted(every))
        one_per_slot = {}
        for role, slot in self.role_slots.items():
            if slot and self.scan.get(role):
                one_per_slot.setdefault(slot, role)
        self.assertEqual(self.findings_for(doc("{cluster:any}", sorted(one_per_slot.values()))), [])

    def test_a_role_in_the_catalog_alone_is_still_held_to_its_assertions(self):
        objective = [{"name": "o", "role": "objective", "check": {"type": "report_contains"}}]
        doc = {"fixtures": ["catalog-only-role"], "verification_spec": objective}
        self.assertEqual([r for r, _ in self.findings_for(doc, scan=dict(self.scan, **{"catalog-only-role": []}))], ["c-role"])
        self.assertEqual(self.findings_for(doc, scan=dict(self.scan, **{"catalog-only-role": [{"op": "absent"}]})), [])

    def test_an_exemption_covers_its_own_mechanism_only(self):
        # vcs-spent-branch-reuse is exempt for its pool-repository write; a
        # slot read added to it would be a c-slot finding the entry does not cover.
        exempt = {(case, rule) for case, rule, _, _ in SEAT_BAR_EXEMPT}
        self.assertIn(("vcs-spent-branch-reuse", "c-repository"), exempt)
        self.assertNotIn(("vcs-spent-branch-reuse", "c-slot"), exempt)
        doc = {"verification_spec": [{"name": "o", "role": "objective", "check": {
            "type": "report_contains", "required_phrases": ["seeded-b"]}}]}
        self.assertEqual(sorted(r for r, _ in self.findings_for(doc, requests=True, repository_roles=set())), ["c-repository", "c-slot"])

    def test_a_pull_request_into_the_pool_repository_needs_a_repository_assertion(self):
        doc = {"fixtures": ["no-pdb-workload"], "verification_spec": [
            {"name": "o", "role": "objective", "check": {"type": "pull_request_opened"}}]}
        self.assertEqual([r for r, _ in self.findings_for(doc, requests=True, repository_roles=set())], ["c-repository"])
        self.assertEqual(self.findings_for(doc, requests=True, repository_roles={"no-pdb-workload"}), [])
        self.assertEqual([r for r, _ in self.findings_for(doc, requests=True, repository_roles={"crashloop-workload"})], ["c-repository"],
                         "another role's repository assertion does not guard this case's")
        roleless = {"verification_spec": doc["verification_spec"]}
        self.assertEqual([r for r, _ in self.findings_for(roleless, requests=True, repository_roles=set())], ["c-repository"])
        self.assertEqual(self.findings_for(roleless, requests=True, repository_roles={"no-pdb-workload"}), [])
        assertions = {
            "no-pdb-workload": [{"subject": "gitrepo/infra", "op": "absent"}],
            "hpa-saturated": [{"subject": "deployment/inference-server", "op": "absent"}],
        }
        self.assertEqual(repository_asserted_roles(assertions, "gitrepo"), {"no-pdb-workload"})
        # Until the scan defines its repository subject, a catalog entry alone clears nothing.
        self.assertEqual(repository_asserted_roles(assertions, None), set())

    def test_the_text_read_of_requesting_agrees_with_the_lane_module(self):
        """eval_rosters is stdlib-only, so it reads a requesting check out of
        the text; hold it to lane.py's YAML read on every registered case."""
        import yaml

        listed = self.lane.load_lane_requesting(eval_rosters.INJECT_LANE_SAFEGUARDS_FILE)
        self.assertEqual(self.lane_requesting, listed)
        self.assertEqual(eval_rosters.REQUESTING_CHECK_RE.pattern.count("|") + 1, len(self.lane.REQUESTING_CHECK_TYPES))
        for case in eval_rosters.presubmit_cases() + eval_rosters.nightly_cases():
            with self.subTest(case=case):
                doc = yaml.safe_load((REPO_ROOT / "bench" / "tasks" / case / "task.yaml").read_text(encoding="utf-8"))
                self.assertEqual(eval_rosters.requests_a_pull_request(case, self.lane_requesting),
                                 self.lane.requested_for(doc.get("verification_spec") or [], listed.get(case, 0)) > 0)


class PresubmitWallTimeTest(unittest.TestCase):
    """Rule (e): the presubmit's estimated wall time stays under the budget
    eval_rosters.PRESUBMIT_WALL_BUDGET_MINUTES names."""

    def test_the_estimate_is_within_the_budget(self):
        wall = eval_rosters.presubmit_wall_estimate()
        minutes = -(-wall["total"] // 60)
        self.assertLessEqual(
            minutes, eval_rosters.PRESUBMIT_WALL_BUDGET_MINUTES,
            f"the presubmit is estimated at {minutes} min ({wall}), over the "
            f"{eval_rosters.PRESUBMIT_WALL_BUDGET_MINUTES}-min budget: re-measure the hints this "
            "roster edit relies on, or raise the budget on purpose (docs/eval-gate-roster.md, rule (e))")

    def test_the_hint_parser_reads_what_the_shell_answers(self):
        """unit_cost_hint run by bash and read by the parser agree on every
        registered case and on an unknown one."""
        src = SCRIPT.read_text(encoding="utf-8")
        function = re.search(r"^unit_cost_hint\(\) \{.*?^\}$", src, re.DOTALL | re.MULTILINE).group(0)
        cases = eval_rosters.presubmit_cases() + eval_rosters.nightly_cases() + ["no-such-case"]
        out = subprocess.run(["bash", "-c", function + '\nfor c in "$@"; do unit_cost_hint "$c"; done', "_", *cases],
                             check=True, capture_output=True, text=True).stdout.split()
        hints, default = eval_rosters.parse_cost_hints(src)
        self.assertEqual([int(n) for n in out], [hints.get(c, default) for c in cases])

    def test_the_text_read_of_a_stopped_ask_agrees_with_stop_hook(self):
        """A stopped ask runs in the serial phase too (unit_phase reads
        STOPPED_ASK_CASES); hold the stdlib read to stop_hook's on every
        registered case, and price one in phase two."""
        import yaml
        from unittest import mock

        sys.path.insert(0, str(REPO_ROOT / "bench"))
        from kube_agents_bench import stop_hook

        self.assertEqual(eval_rosters.STOP_DIRECTIVE, stop_hook.STOP_DIRECTIVE)
        stopped = []
        for case in eval_rosters.presubmit_cases() + eval_rosters.nightly_cases():
            with self.subTest(case=case):
                doc = yaml.safe_load((REPO_ROOT / "bench" / "tasks" / case / "task.yaml").read_text(encoding="utf-8"))
                self.assertEqual(eval_rosters.is_stopped_ask(case), stop_hook.is_stopped_ask(str(doc.get("prompt") or "")))
                if eval_rosters.is_stopped_ask(case):
                    stopped.append(case)
        self.assertTrue(stopped, "no registered stopped ask left to price")
        base = eval_rosters.presubmit_wall_estimate()
        with mock.patch.object(eval_rosters, "presubmit_cases", return_value=eval_rosters.presubmit_cases() + stopped[:1]):
            grown = eval_rosters.presubmit_wall_estimate()
        self.assertEqual(grown["phase one"], base["phase one"])
        self.assertIn(stopped[0], grown["requesting"])

    def test_a_requesting_case_adds_its_serial_chain_and_settles(self):
        from unittest import mock

        base = eval_rosters.presubmit_wall_estimate()
        listed = eval_rosters.parse_lane_requesting(eval_rosters.INJECT_LANE_SAFEGUARDS_FILE.read_text(encoding="utf-8"))
        extra = next(c for c in eval_rosters.nightly_cases() if eval_rosters.requests_a_pull_request(c, listed))
        src = SCRIPT.read_text(encoding="utf-8")
        hints, default = eval_rosters.parse_cost_hints(src)
        settle = int(re.search(r"EVAL_GITHUB_WRITE_SETTLE_SECONDS=(\d+)", src).group(1))
        with mock.patch.object(eval_rosters, "presubmit_cases", return_value=eval_rosters.presubmit_cases() + [extra]):
            grown = eval_rosters.presubmit_wall_estimate()
        self.assertEqual(grown["phase one"], base["phase one"])
        self.assertEqual(grown["phase two"] - base["phase two"], grown["repetitions"] * (hints.get(extra, default) + settle))
        self.assertIn(extra, grown["requesting"])

    def test_phase_one_is_the_longer_of_the_lanes_and_the_longest_chain(self):
        from unittest import mock

        # One case alone: its three repetitions serialize on the task lock,
        # so four lanes do not divide its chain.
        with mock.patch.object(eval_rosters, "presubmit_cases", return_value=["compliance-rbac-overgrant"]):
            alone = eval_rosters.presubmit_wall_estimate()
        hints, _ = eval_rosters.parse_cost_hints(SCRIPT.read_text(encoding="utf-8"))
        self.assertEqual(alone["phase one"], 3 * hints["compliance-rbac-overgrant"])
        self.assertEqual(alone["phase two"], 0)
        # Many short chains: the lanes, not any one chain, set phase one.
        cheap = [c for c in eval_rosters.nightly_cases() if hints.get(c) is None][:12]
        self.assertEqual(len(cheap), 12)
        _, default = eval_rosters.parse_cost_hints(SCRIPT.read_text(encoding="utf-8"))
        with mock.patch.object(eval_rosters, "presubmit_cases", return_value=cheap), \
                mock.patch.object(eval_rosters, "requests_a_pull_request", return_value=False):
            spread = eval_rosters.presubmit_wall_estimate()
        self.assertEqual(spread["phase one"], -(-12 * 3 * default // spread["parallelism"]))
        self.assertGreater(spread["phase one"], 3 * default)

    def test_the_summary_prints_the_estimate_and_the_budget(self):
        got = eval_rosters.summary()
        self.assertEqual(got["presubmit wall budget, minutes"], eval_rosters.PRESUBMIT_WALL_BUDGET_MINUTES)
        self.assertEqual(got["presubmit wall estimate, minutes"], -(-eval_rosters.presubmit_wall_estimate()["total"] // 60))


if __name__ == "__main__":
    unittest.main()
