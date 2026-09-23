"""scripts/eval_rosters.py reads hack/eval/ the way the shell does, and the split lost nothing.

Three files replaced three bash arrays in hack/ci-eval-pr.sh on 2026-09-15
(#1546): TASKS, NIGHTLY_TASKS and BOOTSTRAP_ADMITTED's default. Two things
are pinned here. The parser: comments, blank lines, trailing notes and the
old script's default line all read as the shell reads them, and a malformed
entry raises rather than being skipped, because the shell stops the job on
it. The contents: the presubmit file and the blocking roster hold exactly the
sets the script carried at the split -- what runs on every pull request and
what blocks did not move -- and the nightly file holds the script's nightly
array plus the nine cases the TASKS array held commented out, which the
same decision moved into the nightly (#1546, #1564). A later roster change
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
    "pdb-remediation-pr",
]
# The nine cases TASKS held commented out at the split, moved into the
# nightly by the same decision. The two commented-out cases NOT here --
# obtainability-declared-intent-no-finding (#1341) and vcs-history-only-fact
# (#1253) -- have no fixture at all and wait in the validator's
# FIXTURE_NOT_READY instead.
# Registered in the nightly file after the split, in file order, each by the
# pull request that authored the case (a new case lands in the nightly first).
ADDED_AFTER_THE_SPLIT = [
    "incident-triage-oom-event-probe",  # #1023's incident-triage second case, PR #1625
    "ai-security-planted-model-audit",  # #1023's fleet-audits second case, PR #1103
    "autoops-crashloop-config-triage",  # #1023's other incident-triage second case, PR #1103
    "consistency-no-environment-label",  # the drift collector's §4.14 check, with fleet_drift.py
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
    def test_the_presubmit_file_is_the_tasks_array_at_the_split(self):
        self.assertEqual(eval_rosters.presubmit_cases(), PRESUBMIT_AT_SPLIT)

    def test_the_blocking_roster_is_bootstrap_admitted_at_the_split(self):
        self.assertEqual(eval_rosters.blocking_roster(), ROSTER_AT_SPLIT)

    def test_the_nightly_file_is_the_nightly_array_plus_the_moved_cases(self):
        self.assertEqual(eval_rosters.nightly_cases(), NIGHTLY_AT_SPLIT + ADDED_AFTER_THE_SPLIT + MOVED_TO_NIGHTLY)

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


if __name__ == "__main__":
    unittest.main()
