"""The inject lane's safeguards step in hack/ci-eval-pr.sh (#2079).

Under AGENT_TRANSPORT=inject the step exports BENCH_GITOPS_REPO from the
leased project's mapping, materialises every task in the matrix as
`<scratch>/<case>/task.yaml` with hack/eval/inject-lane-safeguards.yaml's
entries appended, and `unit_task_path` hands devops-bench that copy; on any
other transport the step exports nothing, copies nothing and the helper is
the identity, so the api lane's matrix and task files are byte for byte what
they were. The step is lifted out of the shipped script and run under bash
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
    return lifted_block(r"^readonly EVAL_INJECT_LANE_SAFEGUARDS_FILE=[^\n]*\n") + lifted_block(
        r"^readonly EVAL_INJECT_TRANSPORT=[^\n]*\n"
    )


def safeguards_step() -> str:
    """The step and the helper after it, up to the correctness-floor comment."""
    return lifted_block(r"^# ─── The inject lane's safeguards.*?^unit_task_path\(\) \{.*?^\}$")


def presubmit_tasks() -> list[str]:
    excluded = set(eval_rosters.inject_lane_exclusions())
    return [f"./tasks/{c}/task.yaml" for c in eval_rosters.presubmit_cases() if c not in excluded]


def run_step(env: dict | None = None, tasks: list[str] | None = None, lane_file: pathlib.Path = LANE_FILE, prelude: str = "") -> subprocess.CompletedProcess:
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
            prelude,
            step,
            'echo "REPO=${BENCH_GITOPS_REPO-<unset>}"',
            'echo "DIR=${INJECT_LANE_TASKS_DIR-<unset>}"',
            'for t in "${TASKS[@]}"; do n="$(basename "$(dirname "${t}")")"; echo "PATH ${n} $(unit_task_path "${t}" "${n}")"; done',
        ]
    )
    # "Not set by the test" has to mean unset, not whatever the shell running
    # the tests exports: the transport switch and the two repository
    # variables, which a developer who drove the lane by hand has in theirs.
    clean = {k: v for k, v in os.environ.items() if k not in ("AGENT_TRANSPORT", "BENCH_GITOPS_REPO", "EVAL_GITOPS_REPO")}
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True, check=False, env={**clean, **(env or {})})


def tagged(result: subprocess.CompletedProcess, tag: str) -> list[str]:
    return [line[len(tag) + 1 :] for line in result.stdout.splitlines() if line.startswith(tag + " ")]


def value(result: subprocess.CompletedProcess, key: str) -> str:
    return next(line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith(key + "="))


def spec_names(task_yaml: pathlib.Path) -> list[str]:
    import yaml

    doc = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    return [e["name"] for e in doc.get("verification_spec") or []]


class ApiLaneUntouchedTest(unittest.TestCase):
    def test_no_export_no_copy_and_the_helper_is_the_identity(self):
        for env in ({}, {"AGENT_TRANSPORT": "api"}):
            with self.subTest(env=env):
                result = run_step(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(value(result, "REPO"), "<unset>")
                self.assertEqual(value(result, "DIR"), "")
                for line in tagged(result, "PATH"):
                    name, path = line.split(" ", 1)
                    self.assertEqual(path, f"./tasks/{name}/task.yaml")
                self.assertNotIn("carries the lane's safeguards", result.stdout)
                self.assertNotIn(str(LANE_FILE), result.stdout)

    def test_the_lane_file_is_not_read_on_the_api_lane(self):
        # A missing file is fine where the step does not run, which is what
        # "the api lane is untouched" has to mean for the file too.
        result = run_step({"AGENT_TRANSPORT": "api"}, lane_file=pathlib.Path("/nonexistent/lane.yaml"))
        self.assertEqual(result.returncode, 0, result.stderr)


class InjectLaneTest(unittest.TestCase):
    def test_every_task_gets_a_copy_with_the_lane_entries_appended(self):
        result = run_step({"AGENT_TRANSPORT": "inject", "EVAL_LEDGER_REPO_FOR_TEST": "gke-agentic/kube-agents-evals-21-infra"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(value(result, "REPO"), "gke-agentic/kube-agents-evals-21-infra")
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

    def test_the_task_files_under_bench_tasks_are_not_written(self):
        before = {p: p.read_bytes() for p in (BENCH_DIR / "tasks").glob("*/task.yaml")}
        result = run_step({"AGENT_TRANSPORT": "inject", "EVAL_LEDGER_REPO_FOR_TEST": "gke-agentic/kube-agents-evals-21-infra"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_a_local_runs_own_repository_stands_in_for_the_mapping(self):
        result = run_step({"AGENT_TRANSPORT": "inject", "EVAL_GITOPS_REPO": "someone/throwaway-infra"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(value(result, "REPO"), "someone/throwaway-infra")

    def test_the_mapping_wins_over_a_local_override(self):
        result = run_step(
            {"AGENT_TRANSPORT": "inject", "EVAL_LEDGER_REPO_FOR_TEST": "gke-agentic/kube-agents-evals-21-infra", "EVAL_GITOPS_REPO": "someone/throwaway-infra"}
        )
        self.assertEqual(value(result, "REPO"), "gke-agentic/kube-agents-evals-21-infra")

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
        # Named, not implied: the job closes nothing.
        self.assertIn("closes none of them", report)
        call = src.index("\nreport_github_leftovers\n")
        self.assertLess(src.index("EOF_UNIT_QUEUE\nwait\n"), call)
        self.assertLess(call, src.index("# ─── Per-case verdicts"))
        self.assertLess(src.index("EVAL_RUN_STARTED_AT=\"$(date"), src.index("# 2. Cluster Auth"))

    def test_the_lane_file_is_the_path_the_roster_module_names(self):
        self.assertIn(f'readonly EVAL_INJECT_LANE_SAFEGUARDS_FILE="eval/{eval_rosters.INJECT_LANE_SAFEGUARDS_FILE.name}"', constants())

    def test_the_script_parses(self):
        result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
