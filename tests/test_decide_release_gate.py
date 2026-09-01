"""Unit tests for scripts/release/decide_release_gate.sh.

The script picks between "a human decided" and "the resolver decides", and the
distinction is the whole safety story of putting release-publish.yml on a cron:
a dispatch has to keep behaving exactly as it did before the gate existed, and a
scheduled run has to be unable to opt out of the gate.

`dry-run` is the third mode, and it exists because the schedule is not on yet.
It is the only way to watch the resolver decide against the real tag graph
without publishing a GA release, so its one hard requirement is that a verdict
of "release" still publishes nothing.
"""

import pathlib
import subprocess
import unittest

from tests.testing.common import (
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import (
    MOCK_COMMIT_MSG_BREAKING_PRE_1_0,
    MOCK_COMMIT_MSG_FEAT,
    MOCK_LATEST_STAGING_TAG,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "decide_release_gate.sh"

_GA_TAG = "0.1.0"
# The resolver frames every verdict it reaches — release, skip, halt — in this
# banner, and the bypass path prints no banner at all. Its presence in stdout is
# how these tests tell "the resolver ran" from "the gate was short-circuited".
_RESOLVER_MARKER = "=" * 20


class DecideReleaseGateTest(unittest.TestCase):
    def _run(self, repo_dir, env=None):
        output_file = pathlib.Path(repo_dir) / "github_output.txt"
        output_file.write_text("")
        summary_file = pathlib.Path(repo_dir) / "step_summary.md"
        summary_file.write_text("")
        overrides = {
            "GITHUB_OUTPUT": str(output_file),
            "GITHUB_STEP_SUMMARY": str(summary_file),
        }
        if env:
            overrides.update(env)

        proc = subprocess.run(
            ["bash", str(_SCRIPT)],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=overrides),
            cwd=repo_dir,
        )

        outputs = {}
        for line in output_file.read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                outputs[key] = value
        return proc, outputs, summary_file.read_text()

    def _repo(self, new_commit_msg=MOCK_COMMIT_MSG_FEAT):
        """A repository the resolver would say yes to."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        git("tag", "-a", _GA_TAG, "-m", f"release {_GA_TAG}")
        (pathlib.Path(repo_dir) / "second.txt").write_text("second\n")
        git("add", "second.txt")
        git("commit", "-m", new_commit_msg)
        head = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", MOCK_LATEST_STAGING_TAG, "-m", "promoted to staging")
        return temp_dir, repo_dir, git, head

    # ── bypass: the human who dispatched is the gate ─────────────────────────

    def test_a_dispatch_defaults_to_bypassing_the_gate(self):
        """No schedule_gate input at all — an unset variable must not evaluate."""
        temp_dir, repo_dir, _, _ = self._repo()
        try:
            proc, outputs, summary = self._run(repo_dir, env={"EVENT_NAME": "workflow_dispatch"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "true")
            self.assertEqual(outputs["release_commit"], "")
            self.assertNotIn(_RESOLVER_MARKER, proc.stdout)
            self.assertIn("manual dispatch", summary.lower())
        finally:
            temp_dir.cleanup()

    def test_bypass_publishes_even_where_the_resolver_would_halt(self):
        """The emergency path has to stay reachable past a breaking change."""
        temp_dir, repo_dir, _, _ = self._repo(new_commit_msg=MOCK_COMMIT_MSG_BREAKING_PRE_1_0)
        try:
            proc, outputs, _ = self._run(
                repo_dir, env={"EVENT_NAME": "workflow_dispatch", "SCHEDULE_GATE": "bypass"}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "true")
        finally:
            temp_dir.cleanup()

    def test_bypass_leaves_release_commit_empty_for_the_workflow_fallback(self):
        """The publish job ORs this with inputs.target_commit; a value here would win."""
        temp_dir, repo_dir, _, _ = self._repo()
        try:
            _, outputs, _ = self._run(repo_dir, env={"SCHEDULE_GATE": "bypass"})
            self.assertEqual(outputs["release_commit"], "")
        finally:
            temp_dir.cleanup()

    # ── evaluate: exactly a cron tick ────────────────────────────────────────

    def test_evaluate_on_a_dispatch_honours_the_resolver(self):
        temp_dir, repo_dir, _, head = self._repo()
        try:
            proc, outputs, _ = self._run(
                repo_dir, env={"EVENT_NAME": "workflow_dispatch", "SCHEDULE_GATE": "evaluate"}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "true")
            self.assertEqual(outputs["release_commit"], head)
            self.assertIn(_RESOLVER_MARKER, proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_evaluate_passes_a_halt_through_as_a_failure(self):
        temp_dir, repo_dir, _, _ = self._repo(new_commit_msg=MOCK_COMMIT_MSG_BREAKING_PRE_1_0)
        try:
            proc, outputs, _ = self._run(repo_dir, env={"SCHEDULE_GATE": "evaluate"})
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(outputs["should_release"], "false")
        finally:
            temp_dir.cleanup()

    # ── schedule: the gate is not optional ───────────────────────────────────

    def test_a_schedule_event_evaluates_whatever_the_input_says(self):
        """A cron tick cannot inherit a bypassing default, now or after an edit."""
        temp_dir, repo_dir, _, head = self._repo()
        try:
            proc, outputs, _ = self._run(
                repo_dir, env={"EVENT_NAME": "schedule", "SCHEDULE_GATE": "bypass"}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(_RESOLVER_MARKER, proc.stdout)
            self.assertEqual(outputs["release_commit"], head)
        finally:
            temp_dir.cleanup()

    def test_a_schedule_event_with_no_input_at_all_still_evaluates(self):
        """Inputs are empty on a schedule event, which must not read as bypass."""
        temp_dir, repo_dir, _, head = self._repo()
        try:
            proc, outputs, _ = self._run(repo_dir, env={"EVENT_NAME": "schedule", "SCHEDULE_GATE": ""})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(_RESOLVER_MARKER, proc.stdout)
            self.assertEqual(outputs["release_commit"], head)
        finally:
            temp_dir.cleanup()

    # ── dry-run: the verdict without the consequences ────────────────────────

    def test_dry_run_reports_a_release_verdict_but_publishes_nothing(self):
        temp_dir, repo_dir, _, head = self._repo()
        try:
            proc, outputs, summary = self._run(repo_dir, env={"SCHEDULE_GATE": "dry-run"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "false")
            # The verdict is still reported, so the run is readable.
            self.assertEqual(outputs["release_commit"], head)
            self.assertIn("should_release=true", proc.stdout)
            self.assertIn("DRY RUN", proc.stdout)
            # The caveat has to precede the resolver's "Releasing <sha>" heading,
            # or a reader meets the verdict before learning it was not acted on.
            self.assertLess(summary.index("Dry run"), summary.index("Releasing"))
        finally:
            temp_dir.cleanup()

    def test_dry_run_preserves_a_halt_exit_code(self):
        """What the cron would do, including going red."""
        temp_dir, repo_dir, _, _ = self._repo(new_commit_msg=MOCK_COMMIT_MSG_BREAKING_PRE_1_0)
        try:
            proc, outputs, _ = self._run(repo_dir, env={"SCHEDULE_GATE": "dry-run"})
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(outputs["should_release"], "false")
        finally:
            temp_dir.cleanup()

    # ── a named commit and a gate that picks its own are incompatible ────────

    def test_evaluate_refuses_a_dispatch_that_also_names_a_commit(self):
        """The gate would judge one commit and the publish job would ship another.

        release-publish.yml resolves `TARGET_COMMIT` as
        `inputs.target_commit || needs.evaluate-schedule.outputs.release_commit`,
        so the input wins. Left to run, `evaluate` scans the range behind the
        newest staging tag — breaking-change halt included — and then publishes
        whatever commit was typed into the form instead.
        """
        temp_dir, repo_dir, _, _ = self._repo()
        try:
            proc, outputs, _ = self._run(
                repo_dir,
                env={"SCHEDULE_GATE": "evaluate", "TARGET_COMMIT": "deadbeef"},
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("should_release", outputs)
            self.assertIn("target_commit", proc.stderr)
            self.assertNotIn(_RESOLVER_MARKER, proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_dry_run_refuses_a_dispatch_that_also_names_a_commit(self):
        """Publishes nothing either way, but would report a verdict about the wrong commit."""
        temp_dir, repo_dir, _, _ = self._repo()
        try:
            proc, outputs, _ = self._run(
                repo_dir,
                env={"SCHEDULE_GATE": "dry-run", "TARGET_COMMIT": "deadbeef"},
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("should_release", outputs)
            self.assertIn("target_commit", proc.stderr)
        finally:
            temp_dir.cleanup()

    def test_bypass_still_accepts_a_named_commit(self):
        """The emergency path names a commit; refusing it there would break the release."""
        temp_dir, repo_dir, _, _ = self._repo()
        try:
            proc, outputs, _ = self._run(
                repo_dir,
                env={"SCHEDULE_GATE": "bypass", "TARGET_COMMIT": "deadbeef"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "true")
            self.assertEqual(outputs["release_commit"], "")
        finally:
            temp_dir.cleanup()

    # ── an unrecognised mode is not silently a bypass ────────────────────────

    def test_an_unknown_mode_fails_rather_than_defaulting_to_publish(self):
        temp_dir, repo_dir, _, _ = self._repo()
        try:
            proc, outputs, _ = self._run(repo_dir, env={"SCHEDULE_GATE": "yes-please"})
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("should_release", outputs)
            self.assertIn("Unknown schedule_gate mode", proc.stderr)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
