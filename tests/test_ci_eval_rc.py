"""The release-candidate eval driver survives checking out the candidate.

`hack/ci-eval-rc.sh` runs four steps — resolve the candidate, check it out,
deploy its published images, evaluate them — and the second one moves the
ground under the first. Bash reads a script incrementally as it executes, so a
script that checks out a revision where its own file differs can resume at the
same byte offset in different content, and when that goes wrong it goes wrong
silently: a step vanishes and the shell still exits 0. That is why the whole
driver lives inside `main()`, which bash parses in full before running any of
it, and exits rather than returning.

Two tests hold that down from opposite ends.
`test_survives_a_candidate_whose_tree_lacks_the_driver` runs the real script
through a real checkout that deletes it mid-run, and
`test_a_wrapped_body_runs_every_step_after_rewriting_its_own_file` pins the
wrapper property on the bare mechanism. Neither asserts that the *unwrapped*
form breaks, deliberately — `MainWrapperTestCase` explains why.

Everything else here runs the real script — copied into a throwaway git
repository with its three siblings stubbed — rather than grepping it, because a
guard that greps passes for a section someone has commented out. The stubs
append to a trace file OUTSIDE the repository, so the checkout cannot erase the
evidence of what ran before it.
"""

import pathlib
import subprocess
import tempfile
import textwrap
import unittest

from tests.testing.rc_eval_driver import (
    DEPLOY_RC_MARKER as _DEPLOY_RC_MARKER,
    EXPECTED_DECK_URL as _EXPECTED_DECK_URL,
    EXPECTED_RC_EVAL_TIER as _EXPECTED_RC_EVAL_TIER,
    RC_TAG as _RC_TAG,
    RcEvalDriverFixture,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_EVAL_RC = _REPO_ROOT / "hack" / "ci-eval-rc.sh"


class RcEvalDriverTestCase(unittest.TestCase):
    """Runs the real hack/ci-eval-rc.sh against a stubbed candidate tree.

    The fixture itself lives in tests/testing/rc_eval_driver.py, because
    tests/integration/test_seam_rc_eval_verdict.py needs the same driver run to
    produce a real summary artifact for the verdict poller to read, and
    inheriting it from here would have re-run every case below inside that tier.
    """

    def setUp(self):
        self.driver = RcEvalDriverFixture()
        self.addCleanup(self.driver.cleanup)
        self.base = self.driver.base
        self.trace = self.driver.trace
        self.artifacts = self.driver.artifacts

    def build_repo(self, **kwargs) -> tuple[pathlib.Path, str]:
        return self.driver.build_repo(**kwargs)

    def run_driver(self, root: pathlib.Path, **env) -> subprocess.CompletedProcess:
        return self.driver.run_driver(root, **env)

    def steps(self) -> list[str]:
        return self.driver.steps()

    # ─── Dormancy and the trust boundary ────────────────────────────────────

    def test_dormant_until_the_job_config_arms_it(self):
        """Unset RC_EVAL_ENABLED is every context that is not the RC periodic."""
        root, _ = self.build_repo()
        result = self.run_driver(root, RC_EVAL_ENABLED=None)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("RC_EVAL_ENABLED is not set", result.stdout)
        self.assertEqual(self.steps(), [], "a dormant run must not resolve or deploy")

    def test_a_pull_request_never_measures_a_candidate(self):
        """PULL_NUMBER set is a presubmit, which grades itself, not a release."""
        root, _ = self.build_repo()
        result = self.run_driver(root, PULL_NUMBER="1170")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PULL_NUMBER=1170", result.stdout)
        self.assertEqual(self.steps(), [])

    # ─── The four steps ─────────────────────────────────────────────────────

    def test_runs_resolve_checkout_deploy_eval_in_that_order(self):
        root, candidate = self.build_repo()
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.steps(), ["resolve", "deploy", "eval"])
        # The checkout is the step with no stub of its own: both later steps
        # report the HEAD they ran at, and it has to be the candidate's.
        trace = self.trace.read_text(encoding="utf-8")
        self.assertEqual(
            trace.count(f"HEAD={candidate}"),
            2,
            f"deploy and eval must both run at the candidate:\n{trace}",
        )

    def test_deploy_and_eval_receive_the_candidate_and_the_tier(self):
        """RC_COMMIT_SHA is what keeps the candidate out of main's baseline."""
        root, candidate = self.build_repo()
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 0, result.stdout)
        trace = self.trace.read_text(encoding="utf-8")
        self.assertEqual(trace.count(f"RC_COMMIT_SHA={candidate}"), 2, trace)
        # One of #1175's two values and not a name of this lane's own: its
        # switch exits 1 on anything it does not know, and it exits 1 after the
        # pool lease rather than before it.
        self.assertEqual(
            trace.count(f"TIER={_EXPECTED_RC_EVAL_TIER}"), 2, trace
        )

    def test_survives_a_candidate_whose_tree_lacks_the_driver(self):
        """The checkout deletes the running script; the run must finish anyway.

        This is the case the `main()` wrapper exists for, and it is not
        hypothetical: every candidate cut before this file merged is one.
        """
        root, candidate = self.build_repo(driver_at_candidate=False)
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self.steps(),
            ["resolve", "deploy", "eval"],
            "steps after the checkout were dropped -- the driver is being "
            f"re-read from disk after it moves:\n{result.stdout}",
        )
        self.assertFalse((root / "hack" / "ci-eval-rc.sh").exists())
        self.assertIn(candidate[:7], result.stdout)

    def test_refuses_a_dirty_tree_before_touching_the_checkout(self):
        """git's own abort names the file but not why a CI job is holding one."""
        root, _ = self.build_repo()
        (root / "hack" / "ci-deploy.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
        )
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("modified tracked files", result.stdout)
        self.assertIn("hack/ci-deploy.sh", result.stdout)
        self.assertEqual(self.steps(), ["resolve"])

    # ─── Guards on what the candidate's own tree can do ─────────────────────

    def test_refuses_a_candidate_predating_the_rc_deploy_path(self):
        """Without it, ci-deploy.sh builds and the run grades an unshipped build."""
        root, _ = self.build_repo(deploy_supports_rc=False)
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("predates the release-candidate deploy path", result.stdout)
        self.assertEqual(
            self.steps(), ["resolve"], "the refusal must land before deploying"
        )

    def test_grades_a_candidate_that_predates_the_tier_switch_without_a_note(self):
        """The two agree on the matrix, so there is nothing left to warn about.

        A candidate cut before #1175 has no switch to read and falls back to
        the presubmit matrix, which is what RC_EVAL_TIER exports anyway. The
        driver used to print a note here saying the run measured something
        narrower than the lane intended; that stopped being true when the lane
        settled on the presubmit matrix, and a note nobody can act on is how
        the comment this change removed survived nineteen days.
        """
        root, _ = self.build_repo(eval_supports_tier=False)
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("carries no EVAL_TIER switch", result.stdout)
        self.assertEqual(self.steps(), ["resolve", "deploy", "eval"])

    def test_finishes_the_run_after_its_own_file_is_emptied_mid_step(self):
        """The negative control for main(): flatten the wrapper and this fails.

        Every other test here passes against an unwrapped driver, because they
        all move the file by checkout and a checkout cannot hurt a running
        shell — git renames, so the descriptor keeps the original inode. This
        one truncates the live inode from inside the deploy step, which is the
        hazard the header actually describes. Wrapped, the body is already in
        memory and the eval still runs; unwrapped, bash reads EOF at its offset
        and the run ends after deploy having reported nothing and exited 0.
        """
        root, _ = self.build_repo(truncate_driver_from_deploy=True)
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(
            self.steps(),
            ["resolve", "deploy", "eval"],
            "the eval must run after the driver's own file is emptied",
        )
        self.assertTrue(
            (self.artifacts / "rc-eval-summary.md").is_file(),
            "a run that loses its tail exits 0 with no summary: the silent failure",
        )

    # ─── The markers are contracts with real files, not with the stubs ──────

    def test_the_deploy_marker_is_a_string_the_real_ci_deploy_reads(self):
        """The guard greps for it, so a rename there makes this grep vacuous."""
        deploy = (_REPO_ROOT / "hack" / "ci-deploy.sh").read_text(encoding="utf-8")
        self.assertIn(_DEPLOY_RC_MARKER, deploy)

    def test_the_exported_tier_is_the_presubmit_matrix(self):
        """The lane grades the merge-blocking matrix, not the full catalog.

        Step 5 of staging-promotion-pipeline.yml withdraws the nomination if no
        verdict has arrived in 330 minutes, and it cannot wait longer: a
        GitHub-hosted job is killed at 360. The nightly tier does not finish in
        that window — ci-kube-agents-eval-nightly grades the same 126 units and
        took 357 and 401 minutes on the two runs that finished in the week to
        2026-09-23. The presubmit matrix does: the same fan-out took 83, 110 and
        143 minutes on the three runs that reached a verdict on 2026-09-24.

        This pins the value rather than only checking the evaluator accepts it,
        because `nightly` is also accepted. It was `nightly` from #1230 until
        the wall-clock measurements on #1842, and the comment that made that
        look harmless said #1175's switch was not on main — 29 minutes after it
        was. A silent flip back reds here rather than on the next deploy that
        does not happen.
        """
        driver = _CI_EVAL_RC.read_text(encoding="utf-8")
        self.assertIn(
            f'readonly RC_EVAL_TIER="{_EXPECTED_RC_EVAL_TIER}"',
            driver,
            "widening the tier needs the verdict to arrive somewhere that is "
            "not a GitHub-hosted job holding a connection open for it",
        )
        evaluator = (_REPO_ROOT / "hack" / "ci-eval-pr.sh").read_text(encoding="utf-8")
        self.assertIn(
            f"  {_EXPECTED_RC_EVAL_TIER})",
            evaluator,
            "ci-eval-pr.sh's tier case must accept what the driver exports, or "
            "the run exits 1 after taking a pool project",
        )

    # ─── Reporting ──────────────────────────────────────────────────────────

    def test_a_failed_deploy_is_reported_and_is_not_a_verdict(self):
        """Found live: a real ci-deploy.sh exiting non-zero wrote no artifact.

        The deploy was invoked bare, so errexit aborted the run before the
        summary — which left the summary's own "did not reach the verdict step"
        branch unreachable and a Prow run with nothing but a log to read. It
        must not report RED either: nothing was measured, and RED is a
        judgement on the candidate that this run never formed.
        """
        root, _ = self.build_repo(deploy_exit_code=3)
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 3, "the deploy's own status survives")
        self.assertEqual(self.steps(), ["resolve", "deploy"], "the eval must not run")
        self.assertIn("NOT RUN", result.stdout)
        self.assertNotIn("RED", result.stdout)
        summary = (self.artifacts / "rc-eval-summary.md").read_text(encoding="utf-8")
        self.assertIn("| Verdict | NOT RUN |", summary)
        self.assertIn("never evaluated", summary)

    def test_a_red_verdict_is_reported_not_swallowed(self):
        """A red eval has to be red here, because the promotion reads it.

        Both halves matter and they are read by different audiences. The
        non-zero status is what makes the Prow job red for a human looking at
        Deck; the RED row in the summary is what step 5 of staging-promotion-pipeline.yml
        polls for, and what holds the candidate back from staging.
        """
        root, _ = self.build_repo(eval_exit_code=1, eval_writes_verdict=True)
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("RED", result.stdout)
        summary = (self.artifacts / "rc-eval-summary.md").read_text(encoding="utf-8")
        self.assertIn("| Verdict | RED |", summary)
        self.assertIn("RED holds", summary)

    def test_an_eval_that_graded_nothing_is_not_a_red_candidate(self):
        """A preflight refusal must not retire the candidate it never measured.

        hack/ci-eval-pr.sh exits 1 for a red catalog and also for the guards it
        runs before the first case — a ledger token that would not mint, a
        runner image short of `uv`, an EVAL_REPETITIONS that is not a positive
        integer. Reading both as RED matters here in a way it does not in the
        presubmit: RED is settled, so step 5b leaves the evalcand_ tag in place
        and resolve_promotion_candidate.sh skips that commit for good. A broken
        runner would retire a candidate nobody measured.

        bench-gate's --markdown-out is the separator. The roll-up writes it, so
        its absence after a non-zero exit means no case was graded.
        """
        root, _ = self.build_repo(eval_exit_code=1, eval_writes_verdict=False)
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 1, "the eval's own status still survives")
        self.assertEqual(self.steps(), ["resolve", "deploy", "eval"])
        summary = (self.artifacts / "rc-eval-summary.md").read_text(encoding="utf-8")
        self.assertIn("| Verdict | NOT RUN |", summary)
        self.assertNotIn("| Verdict | RED |", summary)
        self.assertIn("before grading a case", summary)

    def test_a_not_evaluated_eval_is_not_run_rather_than_red(self):
        """ci-eval-pr.sh exits 2 when `bench-gate suite` could not evaluate the
        run (an admitted case lost every repetition to infrastructure), and
        bench-gate has written `outcome: not_evaluated` beside the markdown.
        That formed no judgement on the candidate, so it is the failed
        deploy's NOT RUN and not a RED somebody then investigates; the status
        itself still survives, as every other one does.
        """
        root, _ = self.build_repo(
            eval_exit_code=2,
            eval_body='printf \'{"outcome": "not_evaluated"}\' > "${ARTIFACTS}/eval-verdict.json"',
        )
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(self.steps(), ["resolve", "deploy", "eval"])
        self.assertIn("NOT RUN", result.stdout)
        self.assertNotIn("RED", result.stdout)
        self.assertIn("could not certify a verdict", result.stdout)
        summary = (self.artifacts / "rc-eval-summary.md").read_text(encoding="utf-8")
        self.assertIn("| Verdict | NOT RUN |", summary)
        self.assertIn("Nothing here is a judgement on the candidate", summary)

    def test_an_exit_2_the_verdict_json_does_not_confirm_is_red(self):
        """The status is not the proof. `bench-gate case` exits 2 when it could
        not grade at all (a store that will not load, a bad VERSIONS.json), and
        ci-eval-pr.sh dies with that status; for a candidate cut before the
        not-evaluated verdict existed it is the only meaning 2 has. Calling
        that NOT RUN would tell the reader to rerun when the weather clears,
        for as long as the store stays broken. Without the JSON's word, RED.
        """
        root, _ = self.build_repo(eval_exit_code=2)
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("RED", result.stdout)
        self.assertNotIn("NOT RUN", result.stdout)
        summary = (self.artifacts / "rc-eval-summary.md").read_text(encoding="utf-8")
        self.assertIn("| Verdict | RED |", summary)
        # The summary keys its "not measured" paragraph on the verdict, not on
        # the status: a RED row followed by "rerun when the environment is
        # healthy" would tell the reader the opposite of the row.
        self.assertNotIn("could not certify", summary)
        self.assertNotIn("rerun when the environment is healthy", summary)

    def test_an_exit_2_whose_verdict_json_says_red_is_red(self):
        root, _ = self.build_repo(
            eval_exit_code=2,
            eval_body='printf \'{"outcome": "red"}\' > "${ARTIFACTS}/eval-verdict.json"',
        )
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 2, result.stdout)
        summary = (self.artifacts / "rc-eval-summary.md").read_text(encoding="utf-8")
        self.assertIn("| Verdict | RED |", summary)
        self.assertNotIn("could not certify", summary)
        self.assertNotIn("rerun when the environment is healthy", summary)

    def test_an_exit_2_whose_verdict_json_is_the_traps_partial_table_is_red(self):
        """ci-eval-pr.sh's EXIT trap tables the cases graded so far when the
        run ends after its fan-out began and before its own suite step, in
        the same two files `bench-gate suite` would have written, with
        `partial: true` in the JSON. A `bench-gate case` that could not grade
        in the loop after the fan-out lands there with status 2, and the
        subset's `outcome` can read `not_evaluated` when one graded case lost
        every repetition to infrastructure. That is not the run's word for
        it: the run died on a grading error, and before the trap existed it
        left no JSON at all and was RED. It stays RED, and the summary says
        the verdict step was not reached rather than pointing at the table
        as the run's per-case detail.
        """
        root, _ = self.build_repo(
            eval_exit_code=2,
            eval_body=(
                'printf \'{"outcome": "not_evaluated", "partial": true}\' > "${ARTIFACTS}/eval-verdict.json"; '
                'printf \'# PARTIAL\\n\' > "${ARTIFACTS}/eval-verdict.md"'
            ),
        )
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("RED", result.stdout)
        self.assertNotIn("NOT RUN", result.stdout)
        summary = (self.artifacts / "rc-eval-summary.md").read_text(encoding="utf-8")
        self.assertIn("| Verdict | RED |", summary)
        self.assertNotIn("could not certify", summary)
        self.assertNotIn("rerun when the environment is healthy", summary)
        self.assertIn("did not reach its verdict step", summary)
        self.assertIn("under\na PARTIAL banner", summary)
        self.assertNotIn("Per-case detail is in", summary)

    def test_a_deadline_kill_with_a_partial_table_says_the_verdict_step_was_not_reached(self):
        """The same table after Prow's deadline (143): RED as before, and the
        summary must not present the trap's partial table as the run's
        per-case detail beside that RED.
        """
        root, _ = self.build_repo(
            eval_exit_code=143,
            eval_body=(
                'printf \'{"outcome": "green", "partial": true}\' > "${ARTIFACTS}/eval-verdict.json"; '
                'printf \'# PARTIAL\\n\' > "${ARTIFACTS}/eval-verdict.md"'
            ),
        )
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 143, result.stdout)
        summary = (self.artifacts / "rc-eval-summary.md").read_text(encoding="utf-8")
        self.assertIn("| Verdict | RED |", summary)
        self.assertIn("did not reach its verdict step", summary)
        self.assertNotIn("Per-case detail is in", summary)

    def test_a_preflight_refusal_exiting_2_is_not_described_as_weather(self):
        """The two NOT RUN paths must not both claim the same run.

        A preflight refusal exits whatever it exits, and 2 is a status it can
        reach without bench-gate: argparse takes it on a bad flag, and `uv run`
        can exit it before bench-gate loads. Such a run wrote neither verdict
        file, so it is the roll-up-never-reached branch and its summary should
        say the eval stopped before grading a case.

        What it must not also say is that an admitted case lost every
        repetition to infrastructure. That paragraph belonged to a condition
        written when a 2 the JSON did not confirm was still RED, so nothing
        NOT RUN could reach it; once a preflight refusal could, re-deriving the
        branch from the status alone printed both explanations for one run and
        told the reader to wait for weather that was never the problem.
        """
        root, _ = self.build_repo(eval_exit_code=2, eval_writes_verdict=False)
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 2, result.stdout)
        summary = (self.artifacts / "rc-eval-summary.md").read_text(encoding="utf-8")
        self.assertIn("| Verdict | NOT RUN |", summary)
        self.assertIn("before grading a case", summary)
        self.assertNotIn("could not certify", summary)
        self.assertNotIn("rerun when the environment is healthy", summary)
        self.assertNotIn("lost every", summary)

    def test_writes_the_target_and_summary_artifacts(self):
        root, candidate = self.build_repo()
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 0, result.stdout)

        target = (self.artifacts / "rc-target.env").read_text(encoding="utf-8")
        self.assertIn(f"rc_tag={_RC_TAG}", target)
        self.assertIn(f"rc_commit_sha={candidate}", target)

        summary = (self.artifacts / "rc-eval-summary.md").read_text(encoding="utf-8")
        self.assertIn(_RC_TAG, summary)
        self.assertIn(candidate, summary)
        self.assertIn("| Verdict | GREEN |", summary)
        # The link is the whole of "results anyone can find" this lane is
        # allowed to ship: no credential is widened to post it anywhere.
        self.assertIn(_EXPECTED_DECK_URL, summary)

    def test_no_deck_link_when_prow_did_not_supply_one(self):
        """A laptop run gets a summary without a fabricated URL in it."""
        root, _ = self.build_repo()
        result = self.run_driver(root, JOB_NAME=None, BUILD_ID=None)
        self.assertEqual(result.returncode, 0, result.stdout)
        summary = (self.artifacts / "rc-eval-summary.md").read_text(encoding="utf-8")
        self.assertNotIn("| Run |", summary)
        self.assertNotIn("oss.gprow.dev", summary)


class MainWrapperTestCase(unittest.TestCase):
    """Why hack/ci-eval-rc.sh puts its whole body inside main().

    Not a test of this repository's code: it pins the property the driver's
    shape buys, on the bare mechanism, for a reader who finds the wrapper
    ornamental.

    There is deliberately no companion test asserting that the UNWRAPPED form
    breaks. It does, sometimes — a step vanishing from an otherwise green run
    is the shape it takes — but whether it does on any given script depends on
    the file's size and where bash's read buffer lands, and measurements on
    this repository produced both outcomes from the same construct. A test
    asserting the failure would be asserting a coincidence. The wrapper is
    here because that coin is not worth flipping, and what is testable is that
    the wrapped form does not flip it at all.
    """

    # In-place truncation, which is the hostile case: it keeps the inode bash
    # has open, so unlike a git checkout it cannot leave the original content
    # readable behind the descriptor. printf rather than a heredoc because
    # this body gets indented into main() below, and an indented heredoc
    # terminator ends nothing.
    _REWRITE = (
        "printf '%s\\n'"
        " '#!/usr/bin/env bash'"
        " 'echo \"a different file entirely\"'"
        ' > "$0"\n'
    )

    def test_a_wrapped_body_runs_every_step_after_rewriting_its_own_file(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            script = pathlib.Path(tmp) / "s.sh"
            # Padded past bash's read buffer: an unpadded script is small
            # enough to be buffered whole, which makes the rewrite a no-op for
            # the interpreter and the test vacuous.
            padding = "".join(f'p{i}="filler filler filler"\n' for i in range(400))
            script.write_text(
                "#!/usr/bin/env bash\nset -euo pipefail\n"
                + padding
                + "main() {\n"
                + '  echo "step 1"\n'
                + textwrap.indent(self._REWRITE, "  ")
                + '  echo "step 2"\n  echo "step 3"\n  exit 0\n}\nmain "$@"\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                ["bash", str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stdout)
        for step in ("step 1", "step 2", "step 3"):
            self.assertIn(step, result.stdout, result.stdout)


if __name__ == "__main__":
    unittest.main()
