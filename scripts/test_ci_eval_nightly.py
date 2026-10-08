"""The roster loading and the nightly tier switch are model-free shell, so they are testable here.

`hack/ci-eval-pr.sh` builds its task matrix from three files under hack/eval/
(#1546): presubmit-cases.txt into TASKS, nightly-cases.txt into
NIGHTLY_TASKS, blocking-roster.txt into BOOTSTRAP_ADMITTED's default. Then
EVAL_TIER selects the matrix: unset or "presubmit" runs exactly TASKS,
"nightly" appends NIGHTLY_TASKS, and anything else stops the job before it
spends a cluster. The properties below are exercised against the REAL text
lifted out of the script, run over the REAL files, in the same style as
test_ci_eval_fanout.py:

  - the shell and scripts/eval_rosters.py read the files to the same arrays,
    so the lints and the dashboard cannot disagree with the job about what
    runs and what blocks;
  - dormancy: with EVAL_TIER unset the matrix is byte-for-byte the presubmit
    file, so every existing job is untouched by the tier existing;
  - the superset: EVAL_TIER=nightly is the presubmit file then the nightly
    file, in order -- presubmit cases run in the nightly identically, and the
    appended tail is what the nightly adds, nothing reordered, nothing
    dropped;
  - a typo'd tier fails loudly rather than silently running the wrong matrix;
  - EVAL_NIGHTLY_PART splits the nightly into the cases that request a pull
    request ("writers") and the rest ("main"), unset or "all" changes
    nothing, and a typo, a part outside the nightly or an empty part stops
    the job;
  - a missing file, a malformed line, a path with no case behind it, a
    nightly entry that is also a presubmit one, a roster name outside the
    presubmit, and an empty presubmit file each stop the job with a message
    naming the file, rather than running a wrong matrix and reporting green;
  - the arrays are disjoint and every entry resolves to a real case
    directory -- an entry in both would run one task's repetitions twice per
    nightly (double-counted by the gate and racing its own task lock across
    two queue slots), and a path typo would grade MISSING every night while
    looking registered to the lint.

scripts/test_task_registration.py owns the registration half (a nightly entry
counts as registered; the retired commented-out state is a finding);
scripts/test_eval_rosters.py pins the arrays to the sets the script carried
before the split.
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import eval_rosters
from test_eval_rosters import INJECT_LANE_REQUESTING

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
HACK_DIR = REPO_ROOT / "hack"
BENCH_DIR = REPO_ROOT / "bench"
TASKS_DIR = BENCH_DIR / "tasks"


def lifted_block(pattern: str) -> str:
    """A block of the script as written, lifted by regex."""
    src = SCRIPT.read_text(encoding="utf-8")
    match = re.search(pattern, src, re.DOTALL | re.MULTILINE)
    if match is None:  # pragma: no cover - a reshape should say so loudly
        raise AssertionError(f"pattern {pattern!r} not found in {SCRIPT}")
    return match.group(0)


def roster_constants() -> str:
    return lifted_block(r"^readonly EVAL_PRESUBMIT_CASES_FILE=[^\n]*\n(?:readonly EVAL_[A-Z_]+_FILE=[^\n]*\n)*")


def matrix_section() -> str:
    """Section 6 through the tier switch: the reader functions, both case
    arrays, the presubmit name list and EVAL_TIER's case statement."""
    return lifted_block(r"^# 6\. Task Matrix Execution Loop\n.*?^esac$")


def roster_block() -> str:
    """The blocking roster's read, subset check and export."""
    return lifted_block(r"^BLOCKING_ROSTER_ENTRIES=.*?^export BOOTSTRAP_ADMITTED=[^\n]*")


def run_bash(body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + body],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


PRINT_ARRAYS = (
    'printf "TASK %s\\n" "${TASKS[@]}"\n'
    'printf "NIGHTLY %s\\n" "${NIGHTLY_TASKS[@]}"\n'
    'printf "ROSTER %s\\n" "${BOOTSTRAP_ADMITTED}"\n'
)


def load_matrix(env: dict | None = None, hack_dir: pathlib.Path = HACK_DIR) -> subprocess.CompletedProcess:
    """The real loading code over the files under `hack_dir`/eval, BENCH_DIR
    pinned to the real bench/ so entries resolve against real cases."""
    section = matrix_section().replace('BENCH_DIR="${SCRIPT_DIR}/../bench"', f'BENCH_DIR="{BENCH_DIR}"')
    body = "\n".join([f'SCRIPT_DIR="{hack_dir}"', roster_constants(), section, roster_block(), PRINT_ARRAYS])
    return run_bash(body, env)


def lines_tagged(result: subprocess.CompletedProcess, tag: str) -> list[str]:
    return [line[len(tag) + 1 :] for line in result.stdout.splitlines() if line.startswith(tag + " ")]


def presubmit_entries() -> list[str]:
    return [f"./tasks/{name}/task.yaml" for name in eval_rosters.presubmit_cases()]


def nightly_entries() -> list[str]:
    return [f"./tasks/{name}/task.yaml" for name in eval_rosters.nightly_cases()]


class ShellAndPythonAgreeTest(unittest.TestCase):
    def test_the_shell_builds_the_arrays_the_python_parse_reads(self):
        result = load_matrix({"EVAL_TIER": "presubmit"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "TASK"), presubmit_entries())
        self.assertEqual(lines_tagged(result, "NIGHTLY"), nightly_entries())
        self.assertEqual(lines_tagged(result, "ROSTER"), [",".join(eval_rosters.blocking_roster())])

    def test_the_env_override_of_the_roster_still_wins(self):
        result = load_matrix({"BOOTSTRAP_ADMITTED": "x-probe,y-probe"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "ROSTER"), ["x-probe,y-probe"])


class TierSwitchTest(unittest.TestCase):
    def test_unset_tier_is_exactly_the_presubmit_matrix(self):
        result = load_matrix()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            lines_tagged(result, "TASK"),
            presubmit_entries(),
            "with EVAL_TIER unset the matrix must be the presubmit file -- every existing job runs this configuration",
        )

    def test_presubmit_tier_is_exactly_the_presubmit_matrix(self):
        result = load_matrix({"EVAL_TIER": "presubmit"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "TASK"), presubmit_entries())

    def test_nightly_tier_appends_the_nightly_tail_in_order(self):
        result = load_matrix({"EVAL_TIER": "nightly"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            lines_tagged(result, "TASK"),
            presubmit_entries() + nightly_entries(),
            "nightly must be presubmit-then-nightly, in file order: the gate's reporting order is the array's order",
        )
        self.assertIn(f"EVAL_TIER=nightly: {len(nightly_entries())} nightly-only task(s) join the matrix", result.stdout)

    def test_an_unknown_tier_fails_loudly(self):
        result = load_matrix({"EVAL_TIER": "nigthly"})
        self.assertNotEqual(result.returncode, 0, "a typo'd EVAL_TIER must stop the job, not silently run the presubmit matrix under a nightly's name")
        self.assertIn("EVAL_TIER must be", result.stderr)
        self.assertIn("nigthly", result.stderr)

    def test_the_nightly_file_is_nonempty(self):
        # A tier that appends nothing is a job that costs a Boskos lease to
        # rerun the presubmit at midnight; if every nightly case graduates
        # or is retired, delete the tier rather than leaving it vacuous.
        self.assertTrue(nightly_entries(), "hack/eval/nightly-cases.txt parsed to no entries")


class RefusalTest(unittest.TestCase):
    """Each way a roster file can be wrong stops the job, naming the file."""

    def scratch(self, mutate) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            hack = pathlib.Path(tmp) / "hack"
            shutil.copytree(HACK_DIR / "eval", hack / "eval")
            mutate(hack / "eval")
            return load_matrix({"EVAL_TIER": "nightly"}, hack_dir=hack)

    def assert_refused(self, result, *needles):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("TASK ", result.stdout, "the matrix must not be built past a bad file")
        for needle in needles:
            self.assertIn(needle, result.stderr)

    def test_a_missing_file_stops_the_job(self):
        result = self.scratch(lambda d: (d / "nightly-cases.txt").unlink())
        self.assert_refused(result, "nightly-cases.txt is missing", "#1546")

    def test_a_line_that_is_not_a_case_path_stops_the_job(self):
        result = self.scratch(lambda d: (d / "nightly-cases.txt").write_text("agent-kanban-smoke\n"))
        self.assert_refused(result, "nightly-cases.txt", "is not a ./tasks/<id>/task.yaml path")

    def test_a_path_with_no_case_behind_it_stops_the_job(self):
        result = self.scratch(lambda d: (d / "presubmit-cases.txt").write_text("./tasks/no-such-case/task.yaml\n"))
        self.assert_refused(result, "presubmit-cases.txt", "no-such-case", "names no case under bench/tasks/")

    def test_a_nightly_entry_that_is_also_presubmit_stops_the_job(self):
        result = self.scratch(lambda d: (d / "nightly-cases.txt").write_text("./tasks/agent-kanban-smoke/task.yaml\n"))
        self.assert_refused(result, "agent-kanban-smoke", "would run it twice")

    def test_a_roster_name_outside_the_presubmit_stops_the_job(self):
        result = self.scratch(lambda d: (d / "blocking-roster.txt").write_text("obtainability-planted-pdb\n"))
        self.assert_refused(result, "blocking-roster.txt", "obtainability-planted-pdb", "subset of the presubmit")

    def test_an_empty_presubmit_file_stops_the_job(self):
        result = self.scratch(lambda d: (d / "presubmit-cases.txt").write_text("# every line a comment\n\n"))
        self.assert_refused(result, "presubmit-cases.txt names no case")

    def test_an_empty_blocking_roster_stops_the_job(self):
        # An empty roster under EVAL_ADMISSION_MODE=roster disarms rung 4 for
        # every pull request while the job reports green; the explicit
        # BOOTSTRAP_ADMITTED override is the way to mean it.
        result = self.scratch(lambda d: (d / "blocking-roster.txt").write_text("# nobody blocks\n"))
        self.assert_refused(result, "blocking-roster.txt names no case", "BOOTSTRAP_ADMITTED")

    def test_comments_and_whitespace_are_ignored(self):
        def mutate(d):
            text = (d / "nightly-cases.txt").read_text()
            (d / "nightly-cases.txt").write_text("  # leading comment\n\n" + text.replace(
                "./tasks/obtainability-planted-pdb/task.yaml",
                "   ./tasks/obtainability-planted-pdb/task.yaml   # trailing note",
            ))

        result = self.scratch(mutate)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "NIGHTLY"), nightly_entries())


class GitLabLaneTest(unittest.TestCase):
    """Under EVAL_FORGE=gitlab a presubmit runs the GitLab lane's file, a
    nightly the whole catalogue; the roster export keeps only the lane's
    cases, and an empty export is said, not stopped (kube-agents#2394)."""

    def lane_entries(self) -> list[str]:
        return [f"./tasks/{name}/task.yaml" for name in eval_rosters.gitlab_presubmit_cases()]

    def test_a_gitlab_presubmit_runs_the_lanes_file_and_says_what_it_left_out(self):
        result = load_matrix({"EVAL_FORGE": "gitlab", "EVAL_TIER": "presubmit"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "TASK"), self.lane_entries())
        self.assertIn("EVAL_FORGE=gitlab: the GitLab lane runs 2 of the presubmit's cases", result.stdout)
        self.assertIn("left out: reliability-pdb-probe,", result.stdout)
        # Both seats are held out, so no roster case is in the lane: the
        # export is empty by design and the log says rung 4 is disarmed.
        self.assertEqual(lines_tagged(result, "ROSTER"), [""])
        self.assertIn("rung 4 is disarmed on this lane", result.stdout)

    def test_a_roster_case_in_the_lane_arms_the_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            hack = pathlib.Path(tmp) / "hack"
            shutil.copytree(HACK_DIR / "eval", hack / "eval")
            (hack / "eval" / "gitlab-presubmit-cases.txt").write_text("./tasks/agent-kanban-smoke/task.yaml\n./tasks/pdb-remediation-pr/task.yaml\n")
            result = load_matrix({"EVAL_FORGE": "gitlab"}, hack_dir=hack)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "ROSTER"), ["agent-kanban-smoke"], "the roster intersected with the lane")
        self.assertNotIn("rung 4 is disarmed", result.stdout)

    def test_a_gitlab_nightly_is_the_whole_catalogue(self):
        result = load_matrix({"EVAL_FORGE": "gitlab", "EVAL_TIER": "nightly"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "TASK"), presubmit_entries() + nightly_entries())
        self.assertEqual(lines_tagged(result, "ROSTER"), [",".join(eval_rosters.blocking_roster())])

    def test_the_github_presubmit_is_untouched(self):
        result = load_matrix({"EVAL_FORGE": "github"})
        self.assertEqual(lines_tagged(result, "TASK"), presubmit_entries())
        self.assertNotIn("GitLab lane", result.stdout)

    def test_the_env_override_of_the_roster_still_wins_on_the_lane(self):
        result = load_matrix({"EVAL_FORGE": "gitlab", "BOOTSTRAP_ADMITTED": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("rung 4 is disarmed", result.stdout, "an explicit empty override is the stated way to mean it")

    def refused(self, text: str, *needles):
        with tempfile.TemporaryDirectory() as tmp:
            hack = pathlib.Path(tmp) / "hack"
            shutil.copytree(HACK_DIR / "eval", hack / "eval")
            if text is None:
                (hack / "eval" / "gitlab-presubmit-cases.txt").unlink()
            else:
                (hack / "eval" / "gitlab-presubmit-cases.txt").write_text(text)
            result = load_matrix({"EVAL_FORGE": "gitlab"}, hack_dir=hack)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("TASK ", result.stdout)
        for needle in needles:
            self.assertIn(needle, result.stderr)

    def test_an_entry_outside_the_presubmit_stops_the_job(self):
        self.refused("./tasks/obtainability-planted-pdb/task.yaml\n", "gitlab-presubmit-cases.txt", "obtainability-planted-pdb", "subset of the presubmit")

    def test_an_empty_file_a_missing_file_and_a_bad_path_stop_the_job(self):
        self.refused("# nothing\n", "gitlab-presubmit-cases.txt names no case")
        self.refused(None, "gitlab-presubmit-cases.txt is missing")
        self.refused("./tasks/no-such-case/task.yaml\n", "gitlab-presubmit-cases.txt", "names no case under bench/tasks/")

    def test_the_lane_file_does_not_change_the_github_run_when_broken(self):
        # The file is read only under gitlab, so a GitHub presubmit is not
        # stopped by it; the roster test pins the file's content on main.
        with tempfile.TemporaryDirectory() as tmp:
            hack = pathlib.Path(tmp) / "hack"
            shutil.copytree(HACK_DIR / "eval", hack / "eval")
            (hack / "eval" / "gitlab-presubmit-cases.txt").unlink()
            result = load_matrix({"EVAL_FORGE": "github"}, hack_dir=hack)
        self.assertEqual(result.returncode, 0, result.stderr)


class ArrayHygieneTest(unittest.TestCase):
    def test_the_files_are_disjoint(self):
        # An entry in both runs twice per nightly: six repetitions graded as
        # two three-repetition cases of the same name, with the second's
        # per-task lock directory colliding with the first's.
        overlap = sorted(set(presubmit_entries()) & set(nightly_entries()))
        self.assertEqual(overlap, [], "\n\nThese cases are in both roster files, so a nightly would run them twice:\n  " + "\n  ".join(overlap))

    def test_every_entry_resolves_to_a_case_directory(self):
        # Registration keeps the lint green; only existence keeps the run
        # green -- a moved directory would grade MISSING every night.
        missing = [
            name
            for name in eval_rosters.presubmit_cases() + eval_rosters.nightly_cases()
            if not (TASKS_DIR / name / "task.yaml").is_file()
        ]
        self.assertEqual(missing, [], "\n\nThese roster entries name no bench/tasks/ directory:\n  " + "\n  ".join(missing))

    def test_no_entry_repeats_within_a_file(self):
        for label, names in (("presubmit", eval_rosters.presubmit_cases()), ("nightly", eval_rosters.nightly_cases())):
            with self.subTest(file=label):
                self.assertEqual(len(names), len(set(names)), f"duplicate entries in the {label} file")

    def test_every_audit_shaped_nightly_case_has_a_cost_hint_in_the_audit_band(self):
        # Not every one: the audit-shaped ones. unit_cost_hint's default 200
        # fits the probe-shaped cases; what must not happen is a 600-1300s
        # audit priced at 200, parking it at the queue's tail where a deadline
        # kill eats all three of its repetitions every night.
        hint_fn = lifted_block(r"^unit_cost_hint\(\) \{.*?^\}$")
        for audit in (
            "obtainability-planted-pdb",
            "stockout-pinned-pool",
            "upgrade-readiness-lagging-cluster",
            "consistency-drift-outlier",
            "fleet-cost-idle-pool",
        ):
            with self.subTest(audit=audit):
                result = run_bash(f"{hint_fn}\nunit_cost_hint {audit}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertGreaterEqual(
                    int(result.stdout.strip()),
                    600,
                    f"{audit} is audit-shaped and must carry an explicit unit_cost_hint in the 600-1300s band, not the 200s default",
                )

    def test_the_version_control_cases_are_priced_above_the_default(self):
        # None is audit-shaped, so the check above does not reach them, and
        # all are far above the 200s default (each price's comment in
        # ci-eval-pr.sh has its measurement): measured on
        # `dev-vcs2-20260915a`, the resolver runs 1424s a repetition and is the
        # single most expensive unit in the nightly. Left at the default it
        # launched in the last cost tier of each repetition, which is exactly
        # what the deadline kill truncates first.
        hint_fn = lifted_block(r"^unit_cost_hint\(\) \{.*?^\}$")
        for case, floor in (
            ("vcs-issue-resolver-triage", 1300),
            ("vcs-review-feedback-read-back", 600),
            ("vcs-spent-branch-reuse", 900),
        ):
            with self.subTest(case=case):
                result = run_bash(f"{hint_fn}\nunit_cost_hint {case}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertGreaterEqual(
                    int(result.stdout.strip()),
                    floor,
                    f"{case} is measured well above the 200s default and must carry an explicit unit_cost_hint",
                )


def exclusions_block() -> str:
    """The inject lane's exclusion step (#2039): the read, the existence
    check on every entry, and the AGENT_TRANSPORT-gated drop from TASKS."""
    return lifted_block(r"^# ─── The inject lane's exclusions.*?^fi$")


def lane_constants() -> str:
    """The exclusion file's path (declared beside the roster files, after a
    comment `roster_constants` stops at) and the transport name the
    EVAL_MODE_NEXT export uses, which the step compares against."""
    return lifted_block(r"^readonly EVAL_INJECT_LANE_EXCLUSIONS_FILE=[^\n]*\n") + lifted_block(
        r"^readonly EVAL_INJECT_TRANSPORT=[^\n]*\n"
    )


def load_matrix_through_the_lane_step(env: dict | None = None, hack_dir: pathlib.Path = HACK_DIR) -> subprocess.CompletedProcess:
    """`load_matrix` with the exclusion step appended after the tier switch,
    where the script runs it."""
    section = matrix_section().replace('BENCH_DIR="${SCRIPT_DIR}/../bench"', f'BENCH_DIR="{BENCH_DIR}"')
    body = "\n".join(
        [f'SCRIPT_DIR="{hack_dir}"', roster_constants(), lane_constants(), section, exclusions_block(), roster_block(), PRINT_ARRAYS]
    )
    return run_bash(body, env)


class InjectLaneExclusionTest(unittest.TestCase):
    """Under AGENT_TRANSPORT=inject the excluded cases leave TASKS; on any
    other transport the matrix is byte for byte the presubmit file."""

    def excluded_entries(self) -> list[str]:
        return [f"./tasks/{name}/task.yaml" for name in eval_rosters.inject_lane_exclusions()]

    def test_the_api_lane_is_untouched(self):
        for env in ({}, {"AGENT_TRANSPORT": "api"}):
            with self.subTest(env=env):
                result = load_matrix_through_the_lane_step(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(lines_tagged(result, "TASK"), presubmit_entries())
                self.assertNotIn("leaves the matrix", result.stdout)

    def test_the_inject_lane_drops_the_excluded_cases_in_order(self):
        result = load_matrix_through_the_lane_step({"AGENT_TRANSPORT": "inject"})
        self.assertEqual(result.returncode, 0, result.stderr)
        excluded = self.excluded_entries()
        self.assertTrue(excluded, "the exclusions file parsed to no entries")
        self.assertEqual(lines_tagged(result, "TASK"), [e for e in presubmit_entries() if e not in excluded])
        # A nightly-only exclusion has nothing to leave on the presubmit tier.
        for name in (n for n in eval_rosters.inject_lane_exclusions() if n in eval_rosters.presubmit_cases()):
            self.assertIn(f"AGENT_TRANSPORT=inject: {name} leaves the matrix", result.stdout)
        # The roster FILE is untouched (an exclusion is not a demotion), but
        # the export leaves the dropped names out: a roster name the suite
        # never grades would trip bench-gate's misspelled-roster banner on
        # every run of the lane.
        excluded_names = set(eval_rosters.inject_lane_exclusions())
        self.assertTrue(excluded_names & set(eval_rosters.blocking_roster()), "the pinned exclusion is a roster case, so the export must differ from the file")
        self.assertEqual(
            lines_tagged(result, "ROSTER"),
            [",".join(c for c in eval_rosters.blocking_roster() if c not in excluded_names)],
        )

    def test_the_api_lane_exports_the_whole_roster(self):
        result = load_matrix_through_the_lane_step({"AGENT_TRANSPORT": "api"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "ROSTER"), [",".join(eval_rosters.blocking_roster())])

    def test_the_nightly_tier_is_filtered_too(self):
        result = load_matrix_through_the_lane_step({"AGENT_TRANSPORT": "inject", "EVAL_TIER": "nightly"})
        self.assertEqual(result.returncode, 0, result.stderr)
        excluded = self.excluded_entries()
        self.assertEqual(lines_tagged(result, "TASK"), [e for e in presubmit_entries() + nightly_entries() if e not in excluded])
        for name in eval_rosters.inject_lane_exclusions():
            self.assertIn(f"AGENT_TRANSPORT=inject: {name} leaves the matrix", result.stdout)

    def scratch(self, mutate, env) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            hack = pathlib.Path(tmp) / "hack"
            shutil.copytree(HACK_DIR / "eval", hack / "eval")
            mutate(hack / "eval")
            return load_matrix_through_the_lane_step(env, hack_dir=hack)

    def test_an_entry_naming_no_case_stops_the_job_on_every_lane(self):
        # A typo, and the path variants a filesystem test would accept while
        # the exact-match drop would not: each must stop the job, on both lanes.
        for bad in ("agent-kanban-smok", "agent-kanban-smoke/", "./agent-kanban-smoke"):
            for env in ({}, {"AGENT_TRANSPORT": "inject"}):
                with self.subTest(entry=bad, env=env):
                    result = self.scratch(lambda d, bad=bad: (d / "inject-lane-exclusions.txt").write_text(f"# #1: typo\n{bad}\n"), env)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("inject-lane-exclusions.txt", result.stderr)
                    self.assertIn(bad, result.stderr)
                    self.assertNotIn("TASK ", result.stdout, "the matrix must not be built past a bad entry")

    def test_a_nightly_only_case_may_be_excluded(self):
        # Registered means either file: an inject nightly may leave out a
        # nightly-only case, and the presubmit lane must accept the entry too.
        nightly_case = eval_rosters.nightly_cases()[0]
        for env in ({}, {"AGENT_TRANSPORT": "inject"}, {"AGENT_TRANSPORT": "inject", "EVAL_TIER": "nightly"}):
            with self.subTest(env=env):
                result = self.scratch(
                    lambda d: (d / "inject-lane-exclusions.txt").write_text(f"# #1: nightly-only\n{nightly_case}\n"), env
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                if env.get("EVAL_TIER") == "nightly":
                    self.assertNotIn(f"./tasks/{nightly_case}/task.yaml", lines_tagged(result, "TASK"))

    def test_a_missing_file_stops_the_job(self):
        result = self.scratch(lambda d: (d / "inject-lane-exclusions.txt").unlink(), {})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inject-lane-exclusions.txt is missing", result.stderr)

    def test_a_file_with_only_comments_excludes_nothing(self):
        result = self.scratch(lambda d: (d / "inject-lane-exclusions.txt").write_text("# nobody\n"), {"AGENT_TRANSPORT": "inject"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "TASK"), presubmit_entries())

    def test_excluding_every_case_stops_the_lane(self):
        everything = "".join(f"{name}\n" for name in eval_rosters.presubmit_cases())
        result = self.scratch(lambda d: (d / "inject-lane-exclusions.txt").write_text("# #1: all\n" + everything), {"AGENT_TRANSPORT": "inject"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("would run nothing", result.stderr)

    def test_excluding_every_roster_case_stops_the_nightly_lane_too(self):
        # On the nightly tier the matrix keeps the nightly cases past the
        # every-case-excluded stop, so the roster export is the guard that
        # has to fire: an empty BOOTSTRAP_ADMITTED there would disarm rung 4
        # for every case in the matrix with no banner, from a file that
        # needs only the normal approvers.
        everything = "".join(f"{name}\n" for name in eval_rosters.blocking_roster())
        result = self.scratch(
            lambda d: (d / "inject-lane-exclusions.txt").write_text("# #1: all\n" + everything),
            {"AGENT_TRANSPORT": "inject", "EVAL_TIER": "nightly"},
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("rung 4 disarmed", result.stderr)
        self.assertNotIn("ROSTER ", result.stdout, "the roster must not be exported past the guard")

    def test_an_explicit_roster_override_gets_past_the_guard(self):
        # The guard's message offers BOOTSTRAP_ADMITTED as the way to mean
        # it, so an explicit value -- empty included -- must reach the export.
        everything = "".join(f"{name}\n" for name in eval_rosters.blocking_roster())
        for override in ("x-probe,y-probe", ""):
            with self.subTest(override=override):
                result = self.scratch(
                    lambda d: (d / "inject-lane-exclusions.txt").write_text("# #1: all\n" + everything),
                    {"AGENT_TRANSPORT": "inject", "EVAL_TIER": "nightly", "BOOTSTRAP_ADMITTED": override},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(lines_tagged(result, "ROSTER"), [override])

    def test_excluding_one_roster_case_on_the_nightly_lane_keeps_the_rest(self):
        result = load_matrix_through_the_lane_step({"AGENT_TRANSPORT": "inject", "EVAL_TIER": "nightly"})
        self.assertEqual(result.returncode, 0, result.stderr)
        excluded_names = set(eval_rosters.inject_lane_exclusions())
        self.assertEqual(
            lines_tagged(result, "ROSTER"),
            [",".join(c for c in eval_rosters.blocking_roster() if c not in excluded_names)],
        )

    def test_the_step_runs_after_the_tier_switch_and_before_the_fan_out(self):
        src = SCRIPT.read_text(encoding="utf-8")
        tier = src.index('EVAL_TIER="${EVAL_TIER:-presubmit}"')
        step = src.index("# ─── The inject lane's exclusions")
        names = src.index("\nTASK_NAMES=()")
        self.assertLess(tier, step)
        self.assertLess(step, names, "TASK_NAMES is built from TASKS; the drop must come first")


def part_check() -> str:
    """EVAL_NIGHTLY_PART's validation, right after the tier switch."""
    return lifted_block(r"^# Which part of the nightly matrix this run takes.*?^esac$")


def part_step() -> str:
    """EVAL_NIGHTLY_PART applied to TASKS, after the lane step names the
    cases that request a pull request."""
    return lifted_block(r"^# ─── The nightly part.*?^fi$")


def load_matrix_through_the_part(
    env: dict | None = None, requesting: list[str] | None = None, hack_dir: pathlib.Path = HACK_DIR
) -> subprocess.CompletedProcess:
    """The tier switch, the part check, the lane's exclusions, the part step
    and the roster export, in the script's order. The lane step between the
    exclusions and the part step runs the bench's lane module, so the test
    sets what it computes: the pinned set of cases that request a pull
    request, which scripts/test_eval_rosters.py holds to the module."""
    requesting = INJECT_LANE_REQUESTING if requesting is None else requesting
    section = matrix_section().replace('BENCH_DIR="${SCRIPT_DIR}/../bench"', f'BENCH_DIR="{BENCH_DIR}"')
    body = "\n".join(
        [
            f'SCRIPT_DIR="{hack_dir}"',
            roster_constants(),
            lane_constants(),
            section,
            part_check(),
            exclusions_block(),
            f'INJECT_LANE_REQUESTING="{",".join(requesting)}"',
            part_step(),
            roster_block(),
            PRINT_ARRAYS,
        ]
    )
    clean = {k: v for k, v in os.environ.items() if k not in ("AGENT_TRANSPORT", "EVAL_TIER", "EVAL_NIGHTLY_PART", "BOOTSTRAP_ADMITTED")}
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + body], capture_output=True, text=True, check=False, env={**clean, **(env or {})}
    )


class NightlyPartTest(unittest.TestCase):
    """EVAL_NIGHTLY_PART: the nightly as two periodics, one for the cases
    that request a pull request and one for the rest."""

    def nightly_matrix(self) -> list[str]:
        return presubmit_entries() + nightly_entries()

    def is_writer(self, entry: str) -> bool:
        return pathlib.Path(entry).parent.name in INJECT_LANE_REQUESTING

    def test_unset_or_all_is_the_whole_nightly(self):
        for env in ({"EVAL_TIER": "nightly"}, {"EVAL_TIER": "nightly", "EVAL_NIGHTLY_PART": "all"}, {"EVAL_TIER": "nightly", "EVAL_NIGHTLY_PART": ""}):
            with self.subTest(env=env):
                result = load_matrix_through_the_part(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(lines_tagged(result, "TASK"), self.nightly_matrix())
                self.assertEqual(lines_tagged(result, "ROSTER"), [",".join(eval_rosters.blocking_roster())])
                n = len(self.nightly_matrix())
                self.assertIn(f"Nightly part: all ({n} of {n} cases)\n", result.stdout)

    def test_unset_or_all_outside_the_nightly_changes_nothing(self):
        for env in ({}, {"EVAL_NIGHTLY_PART": "all"}):
            with self.subTest(env=env):
                result = load_matrix_through_the_part(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(lines_tagged(result, "TASK"), presubmit_entries())
                self.assertNotIn("Nightly part:", result.stdout)

    def test_main_is_the_nightly_less_exactly_the_requesting_cases(self):
        result = load_matrix_through_the_part({"EVAL_TIER": "nightly", "EVAL_NIGHTLY_PART": "main"})
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = [e for e in self.nightly_matrix() if not self.is_writer(e)]
        self.assertEqual(lines_tagged(result, "TASK"), expected)
        self.assertEqual(len(self.nightly_matrix()) - len(expected), len(INJECT_LANE_REQUESTING))
        left_out = ",".join(pathlib.Path(e).parent.name for e in self.nightly_matrix() if self.is_writer(e))
        self.assertIn(f"Nightly part: main ({len(expected)} of {len(self.nightly_matrix())} cases); left out: {left_out}\n", result.stdout)
        # Every roster case is in the main part, so its export is the file's.
        self.assertEqual(lines_tagged(result, "ROSTER"), [",".join(eval_rosters.blocking_roster())])

    def test_writers_is_exactly_the_requesting_cases_in_matrix_order(self):
        result = load_matrix_through_the_part({"EVAL_TIER": "nightly", "EVAL_NIGHTLY_PART": "writers"})
        self.assertEqual(result.returncode, 0, result.stderr)
        tasks = lines_tagged(result, "TASK")
        self.assertEqual(tasks, [e for e in self.nightly_matrix() if self.is_writer(e)])
        self.assertEqual(sorted(pathlib.Path(t).parent.name for t in tasks), INJECT_LANE_REQUESTING)
        self.assertIn(f"Nightly part: writers ({len(tasks)} of {len(self.nightly_matrix())} cases); left out: ", result.stdout)
        # No roster case runs, so the export names none: a roster name the
        # suite never grades would trip bench-gate's misspelled-roster banner.
        self.assertEqual(lines_tagged(result, "ROSTER"), [""])

    def test_the_two_parts_cover_the_nightly_once(self):
        main = load_matrix_through_the_part({"EVAL_TIER": "nightly", "EVAL_NIGHTLY_PART": "main"})
        writers = load_matrix_through_the_part({"EVAL_TIER": "nightly", "EVAL_NIGHTLY_PART": "writers"})
        both = lines_tagged(main, "TASK") + lines_tagged(writers, "TASK")
        self.assertEqual(sorted(both), sorted(self.nightly_matrix()))

    def test_writers_on_the_inject_lane_does_not_trip_the_lanes_roster_guard(self):
        # The guard stops a lane whose exclusions empty the export; a
        # writers night empties it by design.
        result = load_matrix_through_the_part({"EVAL_TIER": "nightly", "EVAL_NIGHTLY_PART": "writers", "AGENT_TRANSPORT": "inject"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "ROSTER"), [""])

    def test_an_unknown_part_is_refused(self):
        for tier in ("nightly", "presubmit"):
            with self.subTest(tier=tier):
                result = load_matrix_through_the_part({"EVAL_TIER": tier, "EVAL_NIGHTLY_PART": "writer"})
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("EVAL_NIGHTLY_PART must be 'all', 'main' or 'writers', got 'writer'", result.stderr)
                self.assertNotIn("TASK ", result.stdout)

    def test_a_part_outside_the_nightly_is_refused(self):
        for part in ("main", "writers"):
            for env in ({}, {"EVAL_TIER": "presubmit"}):
                with self.subTest(part=part, env=env):
                    result = load_matrix_through_the_part({**env, "EVAL_NIGHTLY_PART": part})
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(f"EVAL_NIGHTLY_PART={part} selects part of the nightly matrix, but EVAL_TIER=presubmit", result.stderr)
                    self.assertNotIn("TASK ", result.stdout)

    def test_an_empty_part_is_refused(self):
        result = load_matrix_through_the_part({"EVAL_TIER": "nightly", "EVAL_NIGHTLY_PART": "writers"}, requesting=[])
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("EVAL_NIGHTLY_PART=writers selects no case", result.stderr)
        self.assertNotIn("TASK ", result.stdout)
        every_case = [pathlib.Path(e).parent.name for e in self.nightly_matrix()]
        result = load_matrix_through_the_part({"EVAL_TIER": "nightly", "EVAL_NIGHTLY_PART": "main"}, requesting=every_case)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("EVAL_NIGHTLY_PART=main selects no case", result.stderr)

    def test_the_part_is_applied_after_the_lane_step_and_before_the_fan_out(self):
        src = SCRIPT.read_text(encoding="utf-8")
        tier = src.index('EVAL_TIER="${EVAL_TIER:-presubmit}"')
        check = src.index('EVAL_NIGHTLY_PART="${EVAL_NIGHTLY_PART:-all}"')
        lane = src.index("# ─── The inject lane's safeguards")
        step = src.index("# ─── The nightly part")
        roster = src.index("\nBLOCKING_ROSTER_ENTRIES=")
        names = src.index("\nTASK_NAMES=()")
        self.assertLess(tier, check)
        self.assertLess(check, lane)
        self.assertLess(lane, step, "the lane step computes INJECT_LANE_REQUESTING, which the part reads")
        self.assertLess(step, roster)
        self.assertLess(step, names, "TASK_NAMES is built from TASKS; the part must come first")


if __name__ == "__main__":
    unittest.main()
