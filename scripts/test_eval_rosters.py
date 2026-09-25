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
file for the nightly one that day) -- and the nightly file holds the
script's nightly array plus the nine cases the TASKS array held commented
out, which the same decision moved into the nightly (#1546, #1564), less
the cases promoted out of it since, plus the seven. A later roster change
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
    "pdb-remediation-pr",  # still here: its 2026-09-22 promotion was withdrawn, the record predates its #1780 grader
]
# The nine cases TASKS held commented out at the split, moved into the
# nightly by the same decision. The two commented-out cases NOT here --
# obtainability-declared-intent-no-finding (#1341) and vcs-history-only-fact
# (#1253) -- have no fixture at all and wait in the validator's
# FIXTURE_NOT_READY instead.
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
]
# Appended at the tail of the nightly file.
ADDED_AT_THE_TAIL = [
    "chat-routing-own-cluster-namespaces",
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
# reason beside its nightly line. The presubmit file and the roster have held
# the same cases since.
HELD_OUT_TO_NIGHTLY = [
    "security-overgrant-remediation-proposal",  # #1066, never admitted
    "obtainability-pdb-semantics",  # #1049, never admitted
    "obtainability-fleet-exposure-sweep",  # #1049, never admitted
    "obtainability-healthy-namespace-silence",  # #1049, never admitted
    "rca-remediation-pr",  # demoted 2026-09-02, #1189
    "compliance-rbac-overgrant",  # demoted 2026-09-02, #1171
    "cluster-agent-healthy-workload-no-finding",  # held out on #1010
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
        self.assertEqual(eval_rosters.presubmit_cases(), expected)

    def test_the_presubmit_runs_the_blocking_roster_and_nothing_else(self):
        # Decided 2026-09-22 (#1023): a case that cannot red a pull request
        # does not run on one. The script checks only that the roster is a
        # subset of the presubmit; the equality is policy, pinned here.
        self.assertEqual(eval_rosters.presubmit_cases(), eval_rosters.blocking_roster())
        for case in HELD_OUT_TO_NIGHTLY:
            with self.subTest(case=case):
                self.assertNotIn(case, eval_rosters.presubmit_cases())
                self.assertNotIn(case, eval_rosters.blocking_roster())
                self.assertIn(case, eval_rosters.nightly_cases())

    def test_the_blocking_roster_is_bootstrap_admitted_at_the_split_plus_the_admitted(self):
        self.assertEqual(eval_rosters.blocking_roster(), with_insertions(ROSTER_AT_SPLIT, ADMITTED_AFTER_THE_SPLIT))

    def test_the_nightly_file_is_the_nightly_array_plus_the_moved_cases_less_the_promoted_plus_the_held_out(self):
        promoted = {case for case, _ in PROMOTED_AFTER_THE_SPLIT}
        expected = [c for c in NIGHTLY_AT_SPLIT + ADDED_AFTER_THE_SPLIT + MOVED_TO_NIGHTLY if c not in promoted]
        self.assertEqual(eval_rosters.nightly_cases(), expected + HELD_OUT_TO_NIGHTLY + ADDED_AT_THE_TAIL)

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
    "agent-kanban-smoke",  # #2039: grades kanban_create by the front door; the inject door addresses platform directly
]


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

    def test_an_exclusion_is_not_a_demotion(self):
        # The api lane's roster is untouched by an entry here: the excluded
        # case still runs on every pull request and can still red one.
        for case in INJECT_LANE_EXCLUDED:
            with self.subTest(case=case):
                self.assertIn(case, eval_rosters.presubmit_cases())
                self.assertIn(case, eval_rosters.blocking_roster())


if __name__ == "__main__":
    unittest.main()
