"""The eval presubmit's change-based task selection must fail closed.

hack/eval_triggers.py's contract is that a mapping gap costs time, never
coverage: a path no bucket owns, a file type not on a bucket's extension
allowlist, a stack no active task declares, and an unreadable config must
all come out as the full matrix. These tests pin that direction, the
GitHub-Actions-style path semantics (`**` crosses slashes, `!` negates,
last match wins), and the shell block in hack/ci-eval-pr.sh that consumes
the verdict.
"""

import importlib.util
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE = REPO_ROOT / "hack" / "eval_triggers.py"
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"

spec = importlib.util.spec_from_file_location("eval_triggers", MODULE)
eval_triggers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_triggers)

# autoops-warning-event-triage declares stack prebuilt/autoops-incident in
# its task.yaml; cluster-provision-kanban (prebuilt/kind) is left out of
# ACTIVE on purpose, standing in for a stack no active task declares.
ACTIVE = [
    "gpu-stress-test-diagnosis",
    "cluster-agent-crashloop-debug",
    "autoops-warning-event-triage",
]


def select(changed):
    floor, buckets = eval_triggers.load_config(eval_triggers.CONFIG)
    return eval_triggers.Selector(buckets, floor).select(changed, ACTIVE)


class GlobTest(unittest.TestCase):
    def test_star_stays_inside_one_segment(self):
        rx = eval_triggers._compile("docs/*")
        self.assertTrue(rx.match("docs/a.md"))
        self.assertIsNone(rx.match("docs/a/b.md"))

    def test_double_star_crosses_segments(self):
        rx = eval_triggers._compile("docs/**")
        self.assertTrue(rx.match("docs/a/b/c.md"))

    def test_question_mark_does_not_cross_slash(self):
        rx = eval_triggers._compile("a?c")
        self.assertTrue(rx.match("abc"))
        self.assertIsNone(rx.match("a/c"))


class BucketOwnershipTest(unittest.TestCase):
    def test_extension_gate_applies_to_wildcards(self):
        b = eval_triggers.NoTasks("docs", ["docs/**"], ["md"])
        self.assertTrue(b.owns("docs/a.md"))
        self.assertFalse(b.owns("docs/build.ts"))
        self.assertFalse(b.owns("docs/Makefile"))  # no extension at all

    def test_literal_paths_bypass_the_gate(self):
        b = eval_triggers.NoTasks("docs", ["LICENSE"], ["md"])
        self.assertTrue(b.owns("LICENSE"))

    def test_negation_last_match_wins(self):
        b = eval_triggers.NoTasks("docs", ["docs/**", "!docs/keep/**"], ["md"])
        self.assertTrue(b.owns("docs/a.md"))
        self.assertFalse(b.owns("docs/keep/a.md"))

    def test_a_later_positive_reclaims_a_negated_path(self):
        b = eval_triggers.NoTasks(
            "docs", ["docs/**", "!docs/keep/**", "docs/keep/back/**"], ["md"]
        )
        self.assertTrue(b.owns("docs/keep/back/a.md"))


class SelectionTest(unittest.TestCase):
    """Against the real eval_triggers.yaml and real task files."""

    def test_docs_prose_selects_nothing(self):
        self.assertEqual(select(["docs/a.md", "README.md", "LICENSE"]), set())

    def test_task_change_selects_the_task_plus_floor(self):
        got = select(["bench/tasks/gpu-stress-test-diagnosis/task.yaml"])
        self.assertEqual(got, {"gpu-stress-test-diagnosis", "cluster-agent-crashloop-debug"})

    def test_stack_change_selects_its_declaring_task(self):
        got = select(["bench/tf/prebuilt/autoops-incident/main.tf"])
        self.assertEqual(got, {"autoops-warning-event-triage", "cluster-agent-crashloop-debug"})

    def test_inactive_task_change_selects_nothing(self):
        self.assertEqual(select(["bench/tasks/fleet-cost-idle-pool/task.yaml"]), set())

    def test_fail_closed_cases_all_widen_to_all(self):
        for path in [
            "charts/kube-agents/values.yaml",  # no bucket owns it
            "agents/platform/skills/x/SKILL.md",  # image input, deliberately unowned
            "docs/site/astro.config.mjs",  # docs bucket, extension not allowed
            ".github/workflows/validate.yml",  # same
            "bench/tasks/gpu-stress-test-diagnosis/helper.py",  # same, task bucket
            "bench/tf/prebuilt/kind/main.tf",  # stack with no active task
        ]:
            self.assertIs(select([path]), eval_triggers.ALL, path)

    def test_one_widening_file_overrides_a_narrow_one(self):
        got = select(["bench/tasks/gpu-stress-test-diagnosis/task.yaml", "charts/x.yaml"])
        self.assertIs(got, eval_triggers.ALL)


class ConfigTest(unittest.TestCase):
    def test_the_shipped_config_loads_and_its_floor_task_is_registered(self):
        floor, buckets = eval_triggers.load_config(eval_triggers.CONFIG)
        self.assertTrue(buckets)
        tasks_array = SCRIPT.read_text(encoding="utf-8")
        for task in floor:
            self.assertRegex(
                tasks_array,
                rf'(?m)^  "\./tasks/{re.escape(task)}/task\.yaml"$',
                f"floor task {task} must be an active TASKS entry",
            )

    def rejects(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(text)
        with self.assertRaises(Exception):
            eval_triggers.load_config(pathlib.Path(f.name))

    def test_bad_configs_are_rejected_not_guessed_at(self):
        self.rejects("floor: []\n")  # missing buckets
        self.rejects("floor: []\nbuckets: {}\nextra: 1\n")  # unknown top key
        self.rejects("floor: []\nbuckets:\n  d:\n    kind: nope\n    paths: [a]\n")
        self.rejects("floor: []\nbuckets:\n  d:\n    kind: no-tasks\n")  # no paths
        self.rejects(  # typo'd bucket key must not pass silently
            "floor: []\nbuckets:\n  d:\n    kind: no-tasks\n    pattern: [a]\n"
        )
        self.rejects(  # a bucket that only negates owns nothing on purpose
            "floor: []\nbuckets:\n  d:\n    kind: no-tasks\n    paths: ['!a/**']\n"
        )

    def test_a_broken_config_exits_nonzero_so_the_shell_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            shutil.copy(MODULE, pathlib.Path(td) / "eval_triggers.py")
            (pathlib.Path(td) / "eval_triggers.yaml").write_text("buckets: 3\n")
            result = subprocess.run(
                ["python3", str(pathlib.Path(td) / "eval_triggers.py"), "some-task"],
                input="docs/a.md\n",
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)


class ShellBlockTest(unittest.TestCase):
    """The selection block as written, lifted from hack/ci-eval-pr.sh."""

    @classmethod
    def block(cls):
        src = SCRIPT.read_text(encoding="utf-8")
        match = re.search(
            r"^# ─── Change-based task selection.*?^fi$", src, re.S | re.M
        )
        if match is None:  # pragma: no cover - a move should say so loudly
            raise AssertionError("selection block not found in ci-eval-pr.sh")
        return match.group(0)

    def run_block(self, env_mode, selector_body, git_ok=True):
        """Run the lifted block with a stub selector and a stub git."""
        with tempfile.TemporaryDirectory() as td:
            hack = pathlib.Path(td) / "hack"
            hack.mkdir()
            (hack / "eval_triggers.py").write_text(selector_body)
            bindir = pathlib.Path(td) / "bin"
            bindir.mkdir()
            (bindir / "git").write_text(
                "#!/bin/sh\n" + ("echo some/changed/file\n" if git_ok else "exit 1\n")
            )
            (bindir / "git").chmod(0o755)
            harness = "\n".join(
                [
                    "set -euo pipefail",
                    f'export PATH="{bindir}:$PATH"',
                    f'SCRIPT_DIR="{hack}"',
                    'TASKS=( "./tasks/task-a/task.yaml" "./tasks/task-b/task.yaml" )',
                    'PULL_BASE_SHA=base PULL_PULL_SHA=head',
                    f'EVAL_TASK_SELECTION="{env_mode}"',
                    self.block(),
                    'echo "FINAL:${TASKS[*]}"',
                ]
            )
            return subprocess.run(
                ["bash", "-c", harness], capture_output=True, text=True, check=False
            )

    SUBSET_STUB = "import sys\nsys.stdin.read()\nprint('SUBSET'); print('task-b')\n"

    def test_unknown_mode_is_refused_not_shadowed(self):
        result = self.run_block("Auto", self.SUBSET_STUB)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be shadow, auto or off", result.stderr)

    def test_shadow_logs_but_never_shrinks_tasks(self):
        result = self.run_block("shadow", self.SUBSET_STUB)
        self.assertEqual(result.returncode, 0)
        self.assertIn("1 of 2 tasks", result.stdout)
        self.assertIn("FINAL:./tasks/task-a/task.yaml ./tasks/task-b/task.yaml", result.stdout)

    def test_auto_filters_tasks_to_the_subset(self):
        result = self.run_block("auto", self.SUBSET_STUB)
        self.assertEqual(result.returncode, 0)
        self.assertIn("FINAL:./tasks/task-b/task.yaml", result.stdout)
        self.assertNotIn("task-a", result.stdout.split("FINAL:")[1])

    def test_none_keeps_the_full_matrix_even_under_auto(self):
        result = self.run_block("auto", "import sys\nsys.stdin.read()\nprint('NONE')\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("FINAL:./tasks/task-a/task.yaml ./tasks/task-b/task.yaml", result.stdout)

    def test_selector_failure_fails_closed_to_the_full_matrix(self):
        result = self.run_block("auto", "import sys; sys.exit(3)\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("fail closed", result.stdout)
        self.assertIn("FINAL:./tasks/task-a/task.yaml ./tasks/task-b/task.yaml", result.stdout)

    def test_git_failure_fails_closed_to_the_full_matrix(self):
        result = self.run_block("auto", self.SUBSET_STUB, git_ok=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("FINAL:./tasks/task-a/task.yaml ./tasks/task-b/task.yaml", result.stdout)


if __name__ == "__main__":
    unittest.main()
