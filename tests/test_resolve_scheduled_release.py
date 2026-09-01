"""Unit tests for scripts/release/resolve_scheduled_release.sh.

Covers the three conditions an unattended release has to satisfy — a candidate
that passed the gate, something new to ship, and no breaking change in the range
— plus the outputs the publish job reads.

The exit codes carry as much of the design as the verdicts do, so they are
asserted everywhere rather than only where a test is about them. Conditions 1
and 2 failing must exit 0: a quiet week is not a broken pipeline, and a workflow
that goes red on quiet weeks is one nobody reads, which costs more than the
automation gains. Condition 3 must exit non-zero, because a breaking change does
not clear itself — every later run takes the same branch and GA releases stop
until somebody publishes by hand.
"""

import pathlib
import subprocess
import unittest

from tests.testing.common import (
    create_mock_git_repo,
    get_isolated_test_env,
)
from tests.testing.release import (
    MOCK_COMMIT_MSG_BREAKING_BODY,
    MOCK_COMMIT_MSG_BREAKING_PRE_1_0,
    MOCK_COMMIT_MSG_FEAT,
    MOCK_COMMIT_MSG_FIX,
    MOCK_HANDMADE_STAGING_TAG,
    MOCK_LATEST_STAGING_TAG,
    MOCK_LATEST_VALIDATED_RC_TAG,
    MOCK_OLDER_STAGING_TAG,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "resolve_scheduled_release.sh"

_GA_TAG = "0.1.0"


class ResolveScheduledReleaseTest(unittest.TestCase):
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

    def _repo(self, ga_tag=_GA_TAG, new_commit_msg=MOCK_COMMIT_MSG_FEAT, validated=True):
        """GA tag on the first commit, a second commit, gate tag on the second."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        if ga_tag is not None:
            git("tag", "-a", ga_tag, "-m", f"release {ga_tag}")

        (pathlib.Path(repo_dir) / "second.txt").write_text("second\n")
        git("add", "second.txt")
        git("commit", "-m", new_commit_msg)
        head = git("rev-parse", "HEAD").stdout.strip()

        if validated:
            git("tag", "-a", MOCK_LATEST_STAGING_TAG, "-m", "promoted to staging")
        return temp_dir, repo_dir, git, head

    # ── Condition 1: has anything passed the gate? ───────────────────────────

    def test_no_promoted_candidate_is_a_skip_not_an_error(self):
        temp_dir, repo_dir, _, _ = self._repo(validated=False)
        try:
            proc, outputs, summary = self._run(repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "false")
            self.assertEqual(outputs["gate_tag"], "")
            self.assertIn("staging_<ts>_<sha>", outputs["skip_reason"])
            self.assertIn("No release this run", summary)
        finally:
            temp_dir.cleanup()

    def test_an_rc_validated_tag_alone_is_not_enough_to_release(self):
        """The gate moved a rung up the ladder: staging, not the three-hourly suite.

        An `rc_*_validated` tag means the narrow RC suite passed. Reading it here
        would release commits the full nightly matrix has never seen, which is the
        whole thing the staging rung exists to prevent.
        """
        temp_dir, repo_dir, git, _ = self._repo(validated=False)
        try:
            git("tag", "-a", MOCK_LATEST_VALIDATED_RC_TAG, "-m", "validated candidate")

            proc, outputs, _ = self._run(repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "false")
            self.assertEqual(outputs["gate_tag"], "")
        finally:
            temp_dir.cleanup()

    def test_a_hand_made_staging_tag_is_not_gate_evidence(self):
        """`staging_*` is a deploy trigger anyone can push; the shape is the gate.

        `staging-redeploy-*.yml` fires on the bare prefix, so a `staging_hotfix`
        pushed by hand is a supported way to redeploy staging. It must not also
        read back to the release gate as "the nightly matrix passed here".
        """
        temp_dir, repo_dir, git, _ = self._repo(validated=False)
        try:
            git("tag", "-a", MOCK_HANDMADE_STAGING_TAG, "-m", "hand-made trigger")

            proc, outputs, _ = self._run(repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "false")
            self.assertEqual(outputs["gate_tag"], "")
            self.assertIn("staging_<ts>_<sha>", outputs["skip_reason"])
        finally:
            temp_dir.cleanup()

    def test_the_newest_validated_candidate_is_the_one_released(self):
        """Two validated candidates: the release targets the newer tag's commit."""
        temp_dir, repo_dir, git, newest = self._repo()
        try:
            # An older candidate on the first commit. get_latest_validated_rc_tag
            # sorts by refname, so the timestamps in the tag names decide.
            git("tag", "-a", MOCK_OLDER_STAGING_TAG, "-m", "older promotion", "HEAD~1")

            proc, outputs, _ = self._run(repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "true")
            self.assertEqual(outputs["gate_tag"], MOCK_LATEST_STAGING_TAG)
            self.assertEqual(outputs["release_commit"], newest)
        finally:
            temp_dir.cleanup()

    def test_a_green_gate_with_something_new_releases(self):
        temp_dir, repo_dir, _, head = self._repo()
        try:
            proc, outputs, summary = self._run(repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "true")
            self.assertEqual(outputs["release_commit"], head)
            self.assertEqual(outputs["gate_tag"], MOCK_LATEST_STAGING_TAG)
            self.assertEqual(outputs["skip_reason"], "")
            self.assertIn("Releasing", summary)
        finally:
            temp_dir.cleanup()

    def test_first_ever_release_needs_no_previous_ga_tag(self):
        temp_dir, repo_dir, _, head = self._repo(ga_tag=None)
        try:
            proc, outputs, _ = self._run(repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "true")
            self.assertEqual(outputs["release_commit"], head)
        finally:
            temp_dir.cleanup()

    def test_a_breaking_change_does_not_wedge_a_repository_with_no_ga_tag(self):
        """With no GA tag there is no range, so the halt must not scan all history.

        It would match some long-shipped `feat!:` and then never stop matching
        it, because there is no range left to shrink — one halt, permanently, on
        every run. `calculate_next_version.sh` takes the opposite branch in the
        same state and publishes the initial version without scanning, so a halt
        here would also make the two disagree about a never-released repository.
        """
        temp_dir, repo_dir, _, head = self._repo(
            ga_tag=None, new_commit_msg=MOCK_COMMIT_MSG_BREAKING_PRE_1_0
        )
        try:
            proc, outputs, _ = self._run(repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertEqual(outputs["should_release"], "true")
            self.assertEqual(outputs["release_commit"], head)
        finally:
            temp_dir.cleanup()

    # ── Condition 2: is there anything new? ──────────────────────────────────

    def test_nothing_new_since_the_last_ga_tag_is_a_skip(self):
        """GA tag and gate tag on the same commit — the already-released case.

        This is the condition that makes a separate "have we released this tag
        already?" check unnecessary: the state lives in the tags, so an empty
        range answers it.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            git("tag", "-a", _GA_TAG, "-m", f"release {_GA_TAG}")
            git("tag", "-a", MOCK_LATEST_STAGING_TAG, "-m", "promoted to staging")

            proc, outputs, _ = self._run(repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "false")
            self.assertIn("No commits between", outputs["skip_reason"])
        finally:
            temp_dir.cleanup()

    def test_a_ga_tag_ahead_of_the_gated_commit_is_a_skip_not_a_collision(self):
        """The emergency-release shape: GA tagged on a commit newer than the gate's."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            git("tag", "-a", MOCK_LATEST_STAGING_TAG, "-m", "promoted to staging")
            (pathlib.Path(repo_dir) / "hotfix.txt").write_text("hotfix\n")
            git("add", "hotfix.txt")
            git("commit", "-m", "fix: emergency hotfix")
            git("tag", "-a", _GA_TAG, "-m", f"release {_GA_TAG}")

            proc, outputs, _ = self._run(repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "false")
            self.assertIn("No commits between", outputs["skip_reason"])
        finally:
            temp_dir.cleanup()

    def test_a_quiet_run_leaves_the_next_one_free_to_release(self):
        """A skip must not record anything; the state is only ever the tags.

        Running twice over a repository that gains a candidate in between has to
        release on the second run. If a skip ever persisted a "we looked" marker,
        this is the test that catches it.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            git("tag", "-a", _GA_TAG, "-m", f"release {_GA_TAG}")
            proc, outputs, _ = self._run(repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "false")

            (pathlib.Path(repo_dir) / "second.txt").write_text("second\n")
            git("add", "second.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_FIX)
            head = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", MOCK_LATEST_STAGING_TAG, "-m", "promoted to staging")

            proc, outputs, _ = self._run(repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "true")
            self.assertEqual(outputs["release_commit"], head)
        finally:
            temp_dir.cleanup()

    def test_no_wall_clock_reasoning_gates_the_decision(self):
        """Two releasable runs back to back both say yes — the cron is the cadence.

        The resolver deliberately has no weekday anchor and no "one release per
        week" limiter, so nothing here rations releases by elapsed time. A
        limiter reintroduced later fails this.
        """
        temp_dir, repo_dir, _, head = self._repo()
        try:
            for _ in range(2):
                proc, outputs, _ = self._run(repo_dir)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(outputs["should_release"], "true")
                self.assertEqual(outputs["release_commit"], head)
        finally:
            temp_dir.cleanup()

    # ── Condition 3: a breaking change halts, and halting is red ─────────────

    def test_breaking_change_in_the_subject_halts_and_fails_the_job(self):
        temp_dir, repo_dir, _, _ = self._repo(new_commit_msg=MOCK_COMMIT_MSG_BREAKING_PRE_1_0)
        try:
            proc, outputs, summary = self._run(repo_dir)
            self.assertNotEqual(proc.returncode, 0, "a halt has to fail the job, not skip green")
            self.assertEqual(outputs["should_release"], "false")
            self.assertIn("breaking change", outputs["skip_reason"].lower())
            self.assertIn("published by a human", outputs["skip_reason"])
            self.assertIn("Release halted", summary)
            self.assertIn("::error", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_breaking_change_in_the_body_halts_and_fails_the_job(self):
        temp_dir, repo_dir, _, _ = self._repo(new_commit_msg=MOCK_COMMIT_MSG_BREAKING_BODY)
        try:
            proc, outputs, _ = self._run(repo_dir)
            self.assertNotEqual(proc.returncode, 0, "a halt has to fail the job, not skip green")
            self.assertEqual(outputs["should_release"], "false")
            self.assertIn("breaking change", outputs["skip_reason"].lower())
        finally:
            temp_dir.cleanup()

    def test_a_breaking_change_already_released_does_not_halt(self):
        """The halt reads the range, not the whole history.

        A breaking change behind the last GA tag has already shipped. Halting on
        it would wedge every later run permanently, which is the failure mode
        that turns a safety gate into an outage.
        """
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            (pathlib.Path(repo_dir) / "breaking.txt").write_text("breaking\n")
            git("add", "breaking.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_BREAKING_PRE_1_0)
            git("tag", "-a", _GA_TAG, "-m", f"release {_GA_TAG}")

            (pathlib.Path(repo_dir) / "after.txt").write_text("after\n")
            git("add", "after.txt")
            git("commit", "-m", MOCK_COMMIT_MSG_FIX)
            head = git("rev-parse", "HEAD").stdout.strip()
            git("tag", "-a", MOCK_LATEST_STAGING_TAG, "-m", "promoted to staging")

            proc, outputs, _ = self._run(repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(outputs["should_release"], "true")
            self.assertEqual(outputs["release_commit"], head)
        finally:
            temp_dir.cleanup()

    def test_the_halt_uses_the_same_breaking_definition_as_the_version_calculator(self):
        """A scoped `fix(x)!:` is breaking too, and 0.y.z hides it from MAJOR.

        calculate_next_version.sh bumps MINOR for this on 0.y.z, so a guard
        written against the MAJOR digit would wave it straight through. The
        resolver has to catch it.
        """
        temp_dir, repo_dir, _, _ = self._repo(new_commit_msg="fix(operator)!: drop the v1alpha1 field")
        try:
            proc, outputs, _ = self._run(repo_dir)
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("breaking change", outputs["skip_reason"].lower())
        finally:
            temp_dir.cleanup()

    # ── Genuine machinery failures stay red ──────────────────────────────────

    def test_a_gate_tag_that_names_no_commit_is_an_error(self):
        """Not a skip: the newest gate tag failing to resolve means the graph is broken.

        Skipping green here would hide it — and it would hide it every run,
        because the tag stays newest until somebody deletes it.

        It fails through the same exit as every verdict, so the outputs the gate
        job declares are written. A bare `exit 1` resolves `gate_tag` and
        `skip_reason` to empty for the reader who most needs them: the one asking
        why the release did not happen.
        """
        temp_dir, repo_dir, git, _ = self._repo()
        try:
            # A tag on a blob rather than a commit. `git tag -l` lists it, so it
            # is selected as the newest candidate and then fails to peel.
            blob = git("hash-object", "-w", "init.txt").stdout.strip()
            git("tag", "-d", MOCK_LATEST_STAGING_TAG)
            git("tag", MOCK_LATEST_STAGING_TAG, blob)

            proc, outputs, summary = self._run(repo_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does not resolve to a commit", proc.stdout)
            self.assertEqual(outputs["should_release"], "false")
            self.assertEqual(outputs["gate_tag"], MOCK_LATEST_STAGING_TAG)
            self.assertIn("does not resolve to a commit", outputs["skip_reason"])
            self.assertIn("::error", proc.stdout)
            self.assertIn("gate failed", summary.lower())
        finally:
            temp_dir.cleanup()

    def test_it_runs_outside_actions_without_a_github_output(self):
        """Usable by hand for a dry read of the verdict."""
        temp_dir, repo_dir, _, head = self._repo()
        try:
            proc = subprocess.run(
                ["bash", str(_SCRIPT)],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(),
                cwd=repo_dir,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("RELEASING", proc.stdout)
            self.assertIn(head, proc.stdout)
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
