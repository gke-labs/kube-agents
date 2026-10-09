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
the documented exception: #2013, #2016) -- and the nightly file holds the
script's nightly array plus the nine cases the TASKS array held commented
out, which the same decision moved into the nightly (#1546, #1564), plus
whatever landed there since (ADDED_AFTER_THE_SPLIT, ADDED_AFTER_THE_MOVED_BLOCK),
less the cases promoted out of it since, plus the seven, less any held-out
seat. A later roster change
edits the expected sets here in the same pull request; that is the point of
pinning them, since the files are what the eval-crew rule in hack/OWNERS
guards.

scripts/test_ci_eval_nightly.py runs the real shell over the real files and
asserts it builds the arrays this module reads.
"""

import pathlib
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
    "pdb-remediation-pr",  # its 2026-09-22 promotion was withdrawn (the record predated its #1780 grader); its held-out presubmit seat opened 2026-09-28 (HELD_OUT_IN_PRESUBMIT)
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
# authored each case.
ADDED_AFTER_THE_MOVE = [
    "obtainability-design-quota-vs-capacity",  # the two obtainability-journey probes, PR #1841
    "obtainability-window-planning-probe",
    "bootstrap-discovery-fanout",  # the onboarding discovery fan-out, PR #2085
    "first-install-hello-running",  # the first-install hello, both variants
    "first-install-hello-done",
    "fleet-audit-reports-past-run",  # the report store's reader, PR #2115
    "platform-worker-refuses-shipped-skill-edit",  # skill governance, #1848
    "chat-voice-ack-names-target",  # the front door's delegation ack
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
    "observability-watcher-scrape-state",  # the event watcher's scrape state, #2141
    "platform-security-secrets-encryption-currentstate-probe",  # the gke-basics/security skills sync
    "chat-fanout-fleet-restarts-rows",  # a fleet question fanned out one titled card per cluster
    "backup-dr-cmek-selected-namespaces-probe",  # the gke-backup-dr skill sync
    "findings-decision-covers-item",  # the findings queue's item-wide decision
    "upgrades-freeze-runbook-probe",  # the gke-upgrades skill sync
    "stop-on-permission-refusal",  # retry after a command-policy refusal, #1948
]
# Taken back out of the nightly file after registering there, each with a
# KNOWN_UNREGISTERED entry in scripts/validate_bench_cases.py that says why.
PARKED_AFTER_THE_MOVE = [
    "oobe-first-run-audits",  # 2026-10-09: truncated the main nightly, graded blocked
]

# Admitted after the split, each by a pull request that cited the record
# (docs/eval-gate-roster.md, "Admitted on the record since the split"), as
# (case, the roster line it follows): the file keeps the presubmit file's
# reporting order.
ADMITTED_AFTER_THE_SPLIT = [
    # 2026-09-22 (#1023): 529/570 graded presubmit repetitions since #1626, no collapse.
    ("capacity-pinned-pool-probe", "reliability-pdb-probe"),
    # 2026-09-22 (#1023): 10/12 on the same four nights, both misses platform bugs (#1840, #1874).
    ("incident-triage-oom-event-probe", "cluster-agent-crashloop-evidence-chain"),
]
# Moved from the nightly file into the presubmit one after the split, as
# (case, the presubmit line it follows); the same case leaves the nightly
# expectation (NIGHTLY_AT_SPLIT or ADDED_AFTER_THE_SPLIT, whichever registered
# it), since the nightly runs both files and lists no case twice.
PROMOTED_AFTER_THE_SPLIT = [
    ("incident-triage-oom-event-probe", "cluster-agent-healthy-workload-no-finding"),  # 2026-09-22 (#1023)
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
    ("compliance-rbac-overgrant", "agent-kanban-smoke"),  # #2013 step 2, seated 2026-09-29; the roster line is step 4
    ("pdb-remediation-pr", "compliance-rbac-overgrant"),  # #2016 step 2, seat opened 2026-09-28; the roster line is step 4
]


def with_insertions(base, insertions):
    """``base`` with each (case, follows) pair inserted after its predecessor."""
    out = list(base)
    for case, follows in insertions:
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


class SplitLostNothingTest(unittest.TestCase):
    def test_the_presubmit_file_is_the_tasks_array_at_the_split_plus_the_promoted_less_the_held_out(self):
        expected = [c for c in with_insertions(PRESUBMIT_AT_SPLIT, PROMOTED_AFTER_THE_SPLIT) if c not in HELD_OUT_TO_NIGHTLY]
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
                self.assertIn(
                    case,
                    NIGHTLY_AT_SPLIT + ADDED_AFTER_THE_SPLIT + MOVED_TO_NIGHTLY + ADDED_AFTER_THE_MOVED_BLOCK
                    + HELD_OUT_TO_NIGHTLY + ADDED_AT_THE_TAIL + ADDED_AFTER_THE_MOVE,
                    "a held-out seat's nightly line is what moved",
                )
        for case in HELD_OUT_TO_NIGHTLY:
            if case in seated:
                continue
            with self.subTest(case=case):
                self.assertNotIn(case, eval_rosters.presubmit_cases())
                self.assertNotIn(case, eval_rosters.blocking_roster())
                self.assertIn(case, eval_rosters.nightly_cases())

    def test_the_blocking_roster_is_bootstrap_admitted_at_the_split_plus_the_admitted(self):
        self.assertEqual(eval_rosters.blocking_roster(), with_insertions(ROSTER_AT_SPLIT, ADMITTED_AFTER_THE_SPLIT))

    def test_the_nightly_file_is_the_nightly_array_plus_the_moved_cases_less_the_promoted_plus_the_held_out(self):
        promoted = {case for case, _ in PROMOTED_AFTER_THE_SPLIT}
        seated = {case for case, _ in HELD_OUT_IN_PRESUBMIT}
        expected = [
            c
            for c in NIGHTLY_AT_SPLIT + ADDED_AFTER_THE_SPLIT + MOVED_TO_NIGHTLY + ADDED_AFTER_THE_MOVED_BLOCK
            if c not in promoted
        ]
        self.assertEqual(
            eval_rosters.nightly_cases(),
            [
                c
                for c in expected + HELD_OUT_TO_NIGHTLY + ADDED_AT_THE_TAIL + ADDED_AFTER_THE_MOVE
                if c not in seated and c not in PARKED_AFTER_THE_MOVE
            ],
        )

    def test_a_promoted_case_is_in_the_presubmit_and_on_the_roster_and_not_in_the_nightly(self):
        for case, _ in PROMOTED_AFTER_THE_SPLIT:
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
    "chat-fanout-fleet-restarts-rows",  # #2039: grades the front door's ack and the fan-out under its one card; same door
]
# The directives a case's prompt opens with to replay a wake into the chat
# front door (bench/kube_agents_bench/card_wake.py); the harness errors such
# a run on any transport but api.
FRONT_DOOR_WAKE_DIRECTIVES = ("[bench:card-failure-wake]", "[bench:slack-question-wake]")
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
    "chat-fanout-fleet-restarts-rows": "nightly",
}


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
    # obtainability-remediation-proposal was listed in the safeguards file's
    # `requesting:` while the persona answered its "propose a manifest" prompt
    # with a pull request; the persona's rule now puts that manifest in the
    # reply (SOUL.md §3, item 3), so the allowance is gone and the lane's
    # github_writes safeguard grades the case at zero like any other.
    "pdb-remediation-pr",
    "rca-remediation-pr",
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


if __name__ == "__main__":
    unittest.main()
