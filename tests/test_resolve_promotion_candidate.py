"""Unit tests for scripts/release/resolve_promotion_candidate.sh.

The script decides two things the nightly pipeline branches on, and confusing
them is the failure worth guarding against:

  skip_pipeline   nothing to deploy at all
  skip_promotion  deploy and test, but nominate nothing and push no tag

Every skip is exit 0. The only exit 1 is a tag that does not resolve, or one the
RC pipeline never validated.

Since the release-candidate eval became a gate, the promotion path has two tags
rather than one — evalcand_<core> starts the eval, staging_<core> deploys on a
green verdict — and the skip keys on either being present. Both are checked
because they went out of step during the transition: candidates promoted before
the gate landed carry a staging_ tag and no evalcand_ one, and keying on the
nomination alone would re-nominate every one of them.
"""

import pathlib
import subprocess
import unittest

from tests.testing.common import (
    create_mock_git_repo,
    get_isolated_test_env,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release" / "resolve_promotion_candidate.sh"


class ResolvePromotionCandidateTest(unittest.TestCase):
    def _repo(self, with_pipeline_markers=True):
        temp_dir, repo_dir, git = create_mock_git_repo()
        self.addCleanup(temp_dir.cleanup)
        repo_dir = pathlib.Path(repo_dir)
        if with_pipeline_markers:
            self._seed_pipeline_markers(repo_dir, git)
        return repo_dir, git

    def _seed_pipeline_markers(self, repo_dir, git):
        """What candidate_supports_shared_pipeline looks for in a candidate's tree.

        The shared workflows run scripts out of the candidate's own checkout, so
        the resolver skips a candidate that predates them. Every case here that is
        not about that check needs a tree which passes it, and because git commits
        whole trees, seeding once covers the commits the tests stack on top.
        """
        release = repo_dir / "scripts" / "release"
        release.mkdir(parents=True, exist_ok=True)
        (release / "run_optional_e2e_suites.sh").write_text("#!/usr/bin/env bash\n")
        (release / "execute_e2e_tests.py").write_text('_SUITE_ENV_VAR = "E2E_SUITE"\n')
        (release / "reconcile_environment.sh").write_text("#!/usr/bin/env bash\n")
        git("add", "-A")
        git("commit", "-m", "chore: shared pipeline scripts")

    def _commit(self, repo_dir, git, name):
        (repo_dir / f"{name}.txt").write_text(f"{name}\n")
        git("add", f"{name}.txt")
        git("commit", "-m", f"chore: {name}")
        return git("rev-parse", "HEAD").stdout.strip()

    def _run(self, repo_dir, args=(), env=None):
        outputs = repo_dir / "outputs.txt"
        outputs.touch()
        overrides = {"GITHUB_OUTPUT": str(outputs)}
        if env:
            overrides.update(env)
        proc = subprocess.run(
            ["bash", str(_SCRIPT), *args],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=overrides),
            cwd=str(repo_dir),
        )
        parsed = {}
        for line in outputs.read_text().splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                parsed[key] = value
        return proc, parsed

    def test_picks_the_newest_validated_candidate(self):
        repo_dir, git = self._repo()
        older = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", "rc_2608181000_1111111_validated", "-m", "Older")
        newer = self._commit(repo_dir, git, "second")
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Newer")
        # An unvalidated candidate ahead of both must not win.
        self._commit(repo_dir, git, "third")
        git("tag", "-a", "rc_2608201300_3333333", "-m", "Unvalidated")

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["rc_tag"], "rc_2608191200_2222222_validated")
        self.assertEqual(out["commit_sha"], newer)
        self.assertNotEqual(out["commit_sha"], older)
        self.assertEqual(out["evalcand_tag"], "evalcand_2608191200_2222222")
        self.assertEqual(out["staging_tag"], "staging_2608191200_2222222")
        self.assertEqual(out["skip_pipeline"], "false")
        self.assertEqual(out["skip_promotion"], "false")

    def test_no_validated_tag_skips_the_pipeline_without_failing(self):
        repo_dir, _ = self._repo()

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["skip_pipeline"], "true")
        self.assertEqual(out["skip_promotion"], "true")
        self.assertEqual(out["commit_sha"], "")

    def test_already_promoted_skips_only_the_promotion(self):
        """The matrix still runs. Only the promotion path is gated on eligibility.

        This is the transition case: the commit carries a staging_ tag from before
        the eval gate existed, so there is no evalcand_ tag to key on. Without the
        staging_ check it would be nominated afresh and re-measured for hours.
        """
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Validated")
        git("tag", "-a", "staging_2608191200_2222222", "-m", "Already promoted")

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["commit_sha"], head)
        self.assertEqual(out["skip_pipeline"], "false", "an already-promoted night still deploys and tests")
        self.assertEqual(out["skip_promotion"], "true")

    def test_already_nominated_skips_the_promotion_path(self):
        """An evalcand_ tag means the eval has already been paid for.

        Re-nominating would push nothing — ensure_git_tag no-ops on a tag already
        at that commit — so no second eval would start and the pipeline would then
        poll for a verdict it had not asked for. Skipping states the same outcome
        up front.
        """
        repo_dir, git = self._repo()
        head = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Validated")
        git("tag", "-a", "evalcand_2608191200_2222222", "-m", "Already nominated")

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["commit_sha"], head)
        self.assertEqual(out["skip_pipeline"], "false", "an already-nominated night still deploys and tests")
        self.assertEqual(out["skip_promotion"], "true")
        self.assertIn("already nominated", out["skip_reason"])

    def test_an_evalcand_tag_on_another_commit_does_not_block_this_one(self):
        repo_dir, git = self._repo()
        git("tag", "-a", "evalcand_2608181000_1111111", "-m", "Nominated earlier")
        candidate = self._commit(repo_dir, git, "second")
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Validated")

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["commit_sha"], candidate)
        self.assertEqual(out["skip_promotion"], "false")

    def test_both_tags_are_emitted_and_share_a_core(self):
        """The nomination and the promotion have to name the same candidate."""
        repo_dir, git = self._repo()
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Validated")

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["evalcand_tag"], "evalcand_2608191200_2222222")
        self.assertEqual(out["staging_tag"], "staging_2608191200_2222222")

    def test_no_candidate_emits_an_empty_evalcand_tag_rather_than_a_malformed_one(self):
        """The tag is derived from an RC tag, so with no candidate there is nothing to derive from.

        No job reads it on this night — `skip_pipeline` is what step 4 gates on,
        and it is true here — so the assertion is about what the script emits
        rather than about what is pushed. Empty is the reading every consumer
        already has a branch for; `evalcand_` with an absent core is a shape that
        clears a prefix check, reaches whichever step later stops gating on
        `skip_pipeline`, and fires no eval job when it is pushed.
        """
        repo_dir, _ = self._repo()

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["evalcand_tag"], "")

    def test_a_staging_tag_on_another_commit_does_not_block_this_one(self):
        repo_dir, git = self._repo()
        git("tag", "-a", "staging_2608181000_1111111", "-m", "Promoted earlier")
        candidate = self._commit(repo_dir, git, "second")
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Validated")

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["commit_sha"], candidate)
        self.assertEqual(out["skip_promotion"], "false")

    def test_an_explicit_tag_is_honoured(self):
        repo_dir, git = self._repo()
        older = git("rev-parse", "HEAD").stdout.strip()
        git("tag", "-a", "rc_2608181000_1111111_validated", "-m", "Older")
        self._commit(repo_dir, git, "second")
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Newer")

        proc, out = self._run(repo_dir, args=("rc_2608181000_1111111_validated",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["commit_sha"], older)
        self.assertEqual(out["staging_tag"], "staging_2608181000_1111111")

    def test_an_unvalidated_commit_is_refused_even_when_named_by_hand(self):
        """The gate cannot be talked into promoting what the RC pipeline failed."""
        repo_dir, git = self._repo()
        git("tag", "-a", "rc_2608191200_2222222", "-m", "Never validated")

        proc, out = self._run(repo_dir, args=("rc_2608191200_2222222",))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("carries no rc_*_validated tag", proc.stderr)
        self.assertEqual(out, {})

    def test_an_unresolvable_tag_fails(self):
        repo_dir, _ = self._repo()

        proc, _ = self._run(repo_dir, args=("rc_does_not_exist_validated",))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Cannot resolve a commit", proc.stderr)

    def test_a_candidate_predating_the_restructure_skips_the_whole_pipeline(self):
        """No cluster, no matrix, no tag — and no failure either.

        The shared workflows run scripts out of the candidate's checkout. A
        candidate without them would be driven with a suite selector its runner
        ignores, so the gate would test the wrong suite and the optional step
        would contribute nothing, while the run reported a green matrix. Skipping
        whole is the only outcome that does not publish a misleading result.
        """
        repo_dir, git = self._repo(with_pipeline_markers=False)
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Validated")

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["skip_pipeline"], "true")
        self.assertEqual(out["skip_promotion"], "true")
        self.assertIn("predates the shared-pipeline restructure", out["skip_reason"])

    def test_a_hand_passed_ineligible_candidate_fails_rather_than_skipping(self):
        """Naming a candidate is a question about that candidate.

        Answering with a green run whose later jobs were all skipped says it was
        tested. The auto-resolved path skips because nothing is wrong there; this
        one errors, matching the rule the header states for a hand-passed tag the
        pipeline never validated.
        """
        repo_dir, git = self._repo(with_pipeline_markers=False)
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Validated")

        proc, out = self._run(repo_dir, args=("rc_2608191200_2222222_validated",))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("predates a restructure the workflows depend on", proc.stderr)
        self.assertIn("Omit rc_tag", proc.stderr)
        self.assertEqual(out, {}, "a refused candidate must not emit outputs a later job reads")

    def test_the_same_candidate_auto_resolved_skips_instead(self):
        """Same tree, same tag — the difference is only who chose it."""
        repo_dir, git = self._repo(with_pipeline_markers=False)
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Validated")

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["skip_pipeline"], "true")

    def test_a_candidate_missing_only_the_optional_suite_runner_is_skipped(self):
        """Both markers are checked, because they went missing independently."""
        repo_dir, git = self._repo()
        (repo_dir / "scripts" / "release" / "run_optional_e2e_suites.sh").unlink()
        git("add", "-A")
        git("commit", "-m", "chore: drop the optional runner")
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Validated")

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["skip_pipeline"], "true")

    def test_a_candidate_whose_runner_predates_the_selector_rename_is_skipped(self):
        repo_dir, git = self._repo()
        (repo_dir / "scripts" / "release" / "execute_e2e_tests.py").write_text('_ENV_VAR = "E2E_ENV"\n')
        git("add", "-A")
        git("commit", "-m", "chore: pre-rename runner")
        git("tag", "-a", "rc_2608191200_2222222_validated", "-m", "Validated")

        proc, out = self._run(repo_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out["skip_pipeline"], "true")


if __name__ == "__main__":
    unittest.main()
