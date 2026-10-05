"""The inject lane's safeguards step in hack/ci-eval-pr.sh (#2079).

Under AGENT_TRANSPORT=inject the step exports BENCH_GITOPS_REPO from the
leased project's mapping, materialises every task in the matrix as
`<scratch>/<case>/task.yaml` with hack/eval/inject-lane-safeguards.yaml's
entries appended, and `unit_task_path` hands devops-bench that copy; on any
other transport the step exports nothing, copies nothing and the helper is
the identity, so the api lane's matrix and task files are byte for byte what
they were -- it only reads the same file for which cases request a pull
request, since #2260 runs those in the second phase on both lanes for the
repository reset's sake. The step is lifted out of the shipped script and run under bash
over the real files, with `uv run python -m kube_agents_bench.lane` answered
by the real module under python3, so the assertions are against the code
that ships rather than a copy of it.
"""

import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import eval_rosters
from test_eval_rosters import INJECT_LANE_REQUESTING

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
HACK_DIR = REPO_ROOT / "hack"
BENCH_DIR = REPO_ROOT / "bench"
LANE_FILE = HACK_DIR / "eval" / "inject-lane-safeguards.yaml"
LANE_ENTRY = "no-github-writes-the-case-did-not-request"

# `uv run python -m ...` answered by python3 with bench/ on the path: the
# real lane module, without the virtualenv the presubmit has. Anything else
# `uv` is asked for is a test error, said loudly.
UV_STUB = textwrap.dedent(
    f"""\
    uv() {{
      if [ "$1" = "run" ] && [ "$2" = "python" ]; then
        shift 2
        PYTHONPATH="{BENCH_DIR}" python3 "$@"
        return $?
      fi
      echo "unexpected uv call: $*" >&2
      return 1
    }}
    """
)


def lifted_block(pattern: str) -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    match = re.search(pattern, src, re.DOTALL | re.MULTILINE)
    if match is None:  # pragma: no cover - a reshape should say so loudly
        raise AssertionError(f"pattern {pattern!r} not found in {SCRIPT}")
    return match.group(0)


def constants() -> str:
    # The two pacing constants as well: the step picks the writer phase's
    # launch pause from them per lane (WRITER_LAUNCH_PAUSE).
    return "".join(
        lifted_block(pattern)
        for pattern in (
            r"^readonly EVAL_INJECT_LANE_SAFEGUARDS_FILE=[^\n]*\n",
            r"^readonly EVAL_INJECT_TRANSPORT=[^\n]*\n",
            r"^readonly EVAL_UNIT_LAUNCH_STAGGER_SECONDS=[^\n]*\n",
            r"^readonly EVAL_GITHUB_WRITE_SETTLE_SECONDS=[^\n]*\n",
        )
    )


def safeguards_step() -> str:
    """The step and the helper after it, up to the correctness-floor comment."""
    return lifted_block(r"^# ─── The inject lane's safeguards.*?^unit_task_path\(\) \{.*?^\}$")


def presubmit_tasks() -> list[str]:
    excluded = set(eval_rosters.inject_lane_exclusions())
    return [f"./tasks/{c}/task.yaml" for c in eval_rosters.presubmit_cases() if c not in excluded]


def requesting_among(tasks: list[str]) -> str:
    """The cases of `tasks` that request a pull request, in matrix order, as
    the step's INJECT_LANE_REQUESTING spells them. The set is the one
    scripts/test_eval_rosters.py pins, so seating or demoting a requesting
    case edits that pin and nothing here."""
    return ",".join(n for n in (pathlib.Path(t).parent.name for t in tasks) if n in INJECT_LANE_REQUESTING)


def run_step(env: dict | None = None, tasks: list[str] | None = None, lane_file: pathlib.Path = LANE_FILE) -> subprocess.CompletedProcess:
    tasks = presubmit_tasks() if tasks is None else tasks
    tasks_array = "TASKS=(" + " ".join(f'"{t}"' for t in tasks) + ")"
    step = safeguards_step().replace(
        '"${SCRIPT_DIR}/${EVAL_INJECT_LANE_SAFEGUARDS_FILE}"', f'"{lane_file}"'
    )
    body = "\n".join(
        [
            "set -euo pipefail",
            f'SCRIPT_DIR="{HACK_DIR}"; BENCH_DIR="{BENCH_DIR}"; cd "{BENCH_DIR}"',
            'EVAL_LEDGER_REPO="${EVAL_LEDGER_REPO_FOR_TEST:-}"',
            'PROJECT_ID="${PROJECT_ID_FOR_TEST:-kube-agents-evals-21}"',
            UV_STUB,
            constants(),
            tasks_array,
            step,
            'echo "REPO=${BENCH_GITOPS_REPO-<unset>}"',
            'echo "REQUESTING=${INJECT_LANE_REQUESTING-<unset>}"',
            'echo "DIR=${INJECT_LANE_TASKS_DIR-<unset>}"',
            'echo "PAUSE=${WRITER_LAUNCH_PAUSE-<unset>}"',
            'for t in "${TASKS[@]}"; do n="$(basename "$(dirname "${t}")")"; echo "PATH ${n} $(unit_task_path "${t}" "${n}")"; done',
        ]
    )
    # "Not set by the test" has to mean unset, not whatever the shell running
    # the tests exports: the transport switch and the two repository
    # variables, which a developer who drove the lane by hand has in theirs.
    # The step's `mktemp -d` lands under TMPDIR, which is a directory of the
    # test's own so nothing is left behind.
    clean = {k: v for k, v in os.environ.items() if k not in ("AGENT_TRANSPORT", "BENCH_GITOPS_REPO", "EVAL_GITOPS_REPO")}
    # Kept on the result, not in a `with`: the tests read the copies the step
    # wrote, so the directory lives until the result is dropped.
    scratch = tempfile.TemporaryDirectory()
    result = subprocess.run(
        ["bash", "-c", body], capture_output=True, text=True, check=False, env={**clean, "TMPDIR": scratch.name, **(env or {})}
    )
    result.scratch = scratch  # type: ignore[attr-defined]
    return result


def tagged(result: subprocess.CompletedProcess, tag: str) -> list[str]:
    return [line[len(tag) + 1 :] for line in result.stdout.splitlines() if line.startswith(tag + " ")]


def value(result: subprocess.CompletedProcess, key: str) -> str:
    return next(line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith(key + "="))


def spec_names(task_yaml: pathlib.Path) -> list[str]:
    import yaml

    doc = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    return [e["name"] for e in doc.get("verification_spec") or []]


class ApiLaneUntouchedTest(unittest.TestCase):
    """The api lane's task files and matrix are what they were; what it
    shares with the inject lane since #2260 is the second phase, so it reads
    the lane file for the requesting list and nothing else."""

    def test_no_export_no_copy_and_the_helper_is_the_identity(self):
        for env in ({}, {"AGENT_TRANSPORT": "api"}):
            with self.subTest(env=env):
                result = run_step(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(value(result, "REPO"), "<unset>")
                self.assertEqual(value(result, "REQUESTING"), requesting_among(presubmit_tasks()))
                self.assertEqual(value(result, "DIR"), "")
                for line in tagged(result, "PATH"):
                    name, path = line.split(" ", 1)
                    self.assertEqual(path, f"./tasks/{name}/task.yaml")
                self.assertNotIn("carries the lane's safeguards", result.stdout)
                self.assertIn("each after the repository is reset", result.stdout)
                # The writer phase on this lane grades no window, so its units
                # keep the launch stagger, not the inject lane's settle.
                self.assertEqual(value(result, "PAUSE"), "5")
                self.assertEqual(value(result, "PAUSE"), lifted_block(r"^readonly EVAL_UNIT_LAUNCH_STAGGER_SECONDS=(\d+)\n").split("=")[1].strip())

    def test_a_lane_file_that_cannot_be_read_stops_the_api_lane_too(self):
        # The requesting list is what orders the second phase, and the reset
        # before a writer unit is only safe inside it: no list, no run.
        result = run_step({"AGENT_TRANSPORT": "api"}, lane_file=pathlib.Path("/nonexistent/lane.yaml"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not read which cases request a pull request", result.stderr)


class InjectLaneTest(unittest.TestCase):
    def test_every_task_gets_a_copy_with_the_lane_entries_appended(self):
        result = run_step({"AGENT_TRANSPORT": "inject", "EVAL_LEDGER_REPO_FOR_TEST": "gke-agentic/kube-agents-evals-21-infra"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(value(result, "REPO"), "gke-agentic/kube-agents-evals-21-infra")
        # This lane's writer units wait the safeguard's settle, not the stagger.
        self.assertEqual(value(result, "PAUSE"), "120")
        scratch = pathlib.Path(value(result, "DIR"))
        self.assertTrue(scratch.is_dir())
        paths = dict(line.split(" ", 1) for line in tagged(result, "PATH"))
        self.assertEqual(sorted(paths), sorted(c for c in eval_rosters.presubmit_cases() if c not in eval_rosters.inject_lane_exclusions()))
        for name, path in paths.items():
            with self.subTest(case=name):
                self.assertEqual(path, str(scratch / name / "task.yaml"))
                original = spec_names(BENCH_DIR / "tasks" / name / "task.yaml")
                self.assertEqual(spec_names(pathlib.Path(path)), original + [LANE_ENTRY])
        self.assertIn("every task in the matrix carries the lane's safeguards", result.stdout)
        self.assertIn("BENCH_GITOPS_REPO=gke-agentic/kube-agents-evals-21-infra", result.stdout)
        # The presubmit cases that request a pull request, by their own
        # checks or the file's `requesting:` list: the second phase holds
        # them and the log says so. obtainability-remediation-proposal left
        # the list when the persona's rule put its manifest in the reply
        # (#2037), so it is graded at zero writes with the rest of the first
        # phase; pdb-remediation-pr asks for its pull request by its own check.
        requesting = requesting_among(presubmit_tasks())
        self.assertNotIn("obtainability-remediation-proposal", requesting.split(","))
        self.assertIn("pdb-remediation-pr", requesting.split(","))
        self.assertEqual(value(result, "REQUESTING"), requesting)
        self.assertIn(f"run after every other unit: {requesting}\n", result.stdout)

    def test_the_requesting_cases_are_named_for_the_second_phase(self):
        # Every pinned requesting case the presubmit matrix does not seat,
        # added to it: each is named once, in matrix order.
        seated = {pathlib.Path(t).parent.name for t in presubmit_tasks()}
        tasks = presubmit_tasks() + [f"./tasks/{c}/task.yaml" for c in INJECT_LANE_REQUESTING if c not in seated]
        result = run_step({"AGENT_TRANSPORT": "inject", "EVAL_LEDGER_REPO_FOR_TEST": "gke-agentic/kube-agents-evals-21-infra"}, tasks=tasks)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(value(result, "REQUESTING").split(",")), sorted(INJECT_LANE_REQUESTING))
        self.assertEqual(value(result, "REQUESTING"), requesting_among(tasks))
        self.assertIn(f"run after every other unit: {requesting_among(tasks)}\n", result.stdout)

    def test_the_task_files_under_bench_tasks_are_not_written(self):
        before = {p: p.read_bytes() for p in (BENCH_DIR / "tasks").glob("*/task.yaml")}
        result = run_step({"AGENT_TRANSPORT": "inject", "EVAL_LEDGER_REPO_FOR_TEST": "gke-agentic/kube-agents-evals-21-infra"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_a_local_runs_own_repository_stands_in_for_the_mapping(self):
        result = run_step({"AGENT_TRANSPORT": "inject", "EVAL_GITOPS_REPO": "gke-agentic/throwaway-infra"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(value(result, "REPO"), "gke-agentic/throwaway-infra")

    def test_a_repository_the_lane_entry_pins_another_owner_for_stops_the_lane(self):
        """The safeguard's `owner: gke-agentic` would error on every repetition
        over a repository elsewhere; the step refuses before the lease."""
        result = run_step({"AGENT_TRANSPORT": "inject", "EVAL_GITOPS_REPO": "someone/throwaway-infra"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not under gke-agentic", result.stderr)
        self.assertIn("BENCH_GITOPS_REPO=someone/throwaway-infra is not a repository they can grade", result.stderr)
        self.assertNotIn("REPO=", result.stdout)

    def test_a_local_override_wins_over_the_mapping_as_it_does_at_deploy(self):
        """hack/ci-deploy.sh points the agent at EVAL_GITOPS_REPO when it is
        set, so that is the repository the safeguard has to read; Prow
        refuses the override at deploy time, so in CI this is the mapping."""
        result = run_step(
            {"AGENT_TRANSPORT": "inject", "EVAL_LEDGER_REPO_FOR_TEST": "gke-agentic/kube-agents-evals-21-infra", "EVAL_GITOPS_REPO": "gke-agentic/throwaway-infra"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(value(result, "REPO"), "gke-agentic/throwaway-infra")

    def test_no_repository_stops_the_lane_before_anything_runs(self):
        for env in ({"AGENT_TRANSPORT": "inject"}, {"AGENT_TRANSPORT": "inject", "EVAL_GITOPS_REPO": "none"}):
            with self.subTest(env=env):
                result = run_step(env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("no GitOps repository is known for PROJECT_ID=kube-agents-evals-21", result.stderr)
                self.assertIn("inject-lane-safeguards.yaml", result.stderr)
                self.assertNotIn("REPO=", result.stdout)

    def test_a_lane_entry_a_case_already_names_stops_the_lane(self):
        with tempfile.TemporaryDirectory() as scratch:
            task_dir = pathlib.Path(scratch) / "tasks" / "clash"
            task_dir.mkdir(parents=True)
            (task_dir / "task.yaml").write_text(
                f"id: clash\nprompt: hi\nverification_spec:\n  - name: {LANE_ENTRY}\n    role: objective\n    check:\n      type: report_contains\n      required_phrases: [x]\n"
            )
            result = run_step(
                {"AGENT_TRANSPORT": "inject", "EVAL_LEDGER_REPO_FOR_TEST": "gke-agentic/kube-agents-evals-21-infra"},
                tasks=[str(task_dir / "task.yaml")],
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lane safeguard's name", result.stderr)
        self.assertIn("could not append the inject lane's safeguards", result.stderr)

    def test_a_missing_or_malformed_lane_file_stops_the_lane(self):
        with tempfile.TemporaryDirectory() as scratch:
            bad = pathlib.Path(scratch) / "lane.yaml"
            bad.write_text("safeguards: {}\n")
            for lane_file in (pathlib.Path(scratch) / "missing.yaml", bad):
                with self.subTest(lane_file=lane_file.name):
                    result = run_step({"AGENT_TRANSPORT": "inject", "EVAL_LEDGER_REPO_FOR_TEST": "gke-agentic/kube-agents-evals-21-infra"}, lane_file=lane_file)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("could not append the inject lane's safeguards", result.stderr)


class TwoPhaseFanOutTest(unittest.TestCase):
    """The cases that request a pull request launch only after every other
    unit has finished; with none named, one phase runs as before."""

    # The two pacing constants are `readonly` at the top of the script and are
    # not lifted; the test sets them so the stub units run in seconds.
    STUBS = textwrap.dedent(
        """\
        EVAL_REPETITIONS=2
        EVAL_TASK_PARALLELISM=4
        EVAL_UNIT_LAUNCH_STAGGER_SECONDS=0
        EVAL_GITHUB_WRITE_SETTLE_SECONDS=1
        TASKS=("./tasks/a/task.yaml" "./tasks/b/task.yaml" "./tasks/w/task.yaml" "./tasks/v/task.yaml")
        TASK_NAMES=(a b w v)
        TASK_REUSE=("" "" "" "")
        TASK_HAS_STACK=("" "" "" "")
        unit_cost_hint() { echo 200; }
        profile_begin() { :; }
        run_one_unit() { echo "START $2 rep $3"; sleep 1; echo "END $2 rep $3"; }
        """
    )

    def run_fanout(self, requesting: str) -> list[str]:
        body = "\n".join(
            [
                "set -euo pipefail",
                self.STUBS,
                f'INJECT_LANE_REQUESTING="{requesting}"',
                lifted_block(r"^# Two phases on the inject lane.*?^fi$"),
                'echo "TOTAL=$((UNIT_TOTAL + WRITER_TOTAL))"',
            ]
        )
        result = subprocess.run(["bash", "-c", body], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_requesting_units_launch_after_every_other_unit_has_ended_and_one_at_a_time(self):
        lines = self.run_fanout("w,v")
        self.assertIn("TOTAL=8", lines)
        writers = [i for i, line in enumerate(lines) if line.startswith(("START w", "START v", "END w", "END v"))]
        first_writer = min(writers)
        others_ended = [i for i, line in enumerate(lines) if line.startswith("END ") and not line.startswith(("END w", "END v"))]
        self.assertEqual(len(others_ended), 4)
        self.assertLess(max(others_ended), first_writer)
        self.assertTrue(any("every other unit is done; launching the 4 unit(s)" in line and "one at a time, each after a 1s pause" in line for line in lines))
        # The second phase is serial: every writer unit ends before the next
        # starts, whichever case it belongs to.
        writer_lines = [lines[i] for i in writers]
        self.assertEqual(len(writer_lines), 8)
        for start, end in zip(writer_lines[0::2], writer_lines[1::2]):
            self.assertTrue(start.startswith("START ") and end.startswith("END "), writer_lines)
            self.assertEqual(start.split(" ", 1)[1], end.split(" ", 1)[1], writer_lines)

    def test_with_no_requesting_case_there_is_one_phase(self):
        lines = self.run_fanout("")
        self.assertIn("TOTAL=8", lines)
        self.assertFalse(any("every other unit is done" in line for line in lines))
        self.assertEqual(sum(1 for line in lines if line.startswith("END ")), 8)

    def test_the_settle_equals_the_safeguards_clock_skew_default(self):
        """The settle exists so a write in the last seconds of the unit before
        is outside the next window, whose lower edge is the check's default
        skew; the two numbers have to agree."""
        script = SCRIPT.read_text(encoding="utf-8")
        settle = re.search(r"^readonly EVAL_GITHUB_WRITE_SETTLE_SECONDS=(\d+)$", script, re.MULTILINE).group(1)
        verifiers = (REPO_ROOT / "bench" / "kube_agents_bench" / "verifiers.py").read_text(encoding="utf-8")
        body = verifiers[verifiers.index("class GitHubWritesVerifier") :]
        default = re.search(r"max_clock_skew_sec: float = Field\(default=(\d+)\.0", body).group(1)
        self.assertEqual(settle, default)
        self.assertIsNotNone(re.search(r"^readonly EVAL_UNIT_LAUNCH_STAGGER_SECONDS=\d+$", script, re.MULTILINE))


class WiringTest(unittest.TestCase):
    def test_the_step_sits_after_the_exclusions_and_before_the_task_names(self):
        src = SCRIPT.read_text(encoding="utf-8")
        exclusions = src.index("# ─── The inject lane's exclusions")
        step = src.index("# ─── The inject lane's safeguards")
        names = src.index("TASK_NAMES=()")
        self.assertLess(exclusions, step)
        self.assertLess(step, names)

    def test_the_unit_hands_the_bench_the_resolved_path(self):
        src = SCRIPT.read_text(encoding="utf-8")
        unit = re.search(r"^run_one_unit\(\) \{.*?^\}$", src, re.DOTALL | re.MULTILINE).group(0)
        self.assertIn('run_task="$(unit_task_path "${task}" "${name}")"', unit)
        self.assertIn('uv run devops-bench "${run_task}"', unit)
        # Grading still reads the file under bench/tasks/: the scorer's
        # CaseSpec comes from there, and the lane entry reaches it through
        # the record's report.
        self.assertIn('finish_case "${task}" "${name}"', unit)

    def test_the_leftovers_report_runs_after_the_fanout_on_the_inject_lane_only(self):
        src = SCRIPT.read_text(encoding="utf-8")
        report = re.search(r"^report_github_leftovers\(\) \{.*?^\}$", src, re.DOTALL | re.MULTILINE).group(0)
        self.assertIn('[ "${AGENT_TRANSPORT:-}" != "${EVAL_INJECT_TRANSPORT}" ]', report)
        self.assertIn("python -m kube_agents_bench.github_writes", report)
        self.assertIn('--since "${EVAL_RUN_STARTED_AT}"', report)
        self.assertIn('mint_ledger_token "leftovers"', report)
        # Named, not implied: the listing closes nothing.
        self.assertIn("closes none of them", report)
        call = src.index("\nreport_github_leftovers\n")
        # After both phases of the fan-out have been waited for.
        self.assertLess(src.index('launch_units "${UNIT_QUEUE_WRITERS}" 1 "${WRITER_LAUNCH_PAUSE:-${EVAL_GITHUB_WRITE_SETTLE_SECONDS}}"\n  wait\nfi\n'), call)
        self.assertLess(call, src.index("# ─── Per-case verdicts"))
        self.assertLess(src.index("EVAL_RUN_STARTED_AT=\"$(date"), src.index("# 2. Cluster Auth"))

    def test_the_lane_file_is_the_path_the_roster_module_names(self):
        self.assertIn(f'readonly EVAL_INJECT_LANE_SAFEGUARDS_FILE="eval/{eval_rosters.INJECT_LANE_SAFEGUARDS_FILE.name}"', constants())

    def test_the_script_parses(self):
        result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
