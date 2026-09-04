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

import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

from tests.testing.common import create_mock_git_repo

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_EVAL_RC = _REPO_ROOT / "hack" / "ci-eval-rc.sh"

_RC_TAG = "rc_2609021231_fdba3a7"
_JOB_NAME = "ci-kube-agents-eval-rc"
_BUILD_ID = "2095176282760286208"
_EXPECTED_DECK_URL = (
    f"https://oss.gprow.dev/view/gs/kube-agents-prow/logs/{_JOB_NAME}/{_BUILD_ID}"
)

# A stub ci-deploy.sh has to carry this for the driver's predates-the-RC-path
# guard to let it through; the real marker is the variable ci-deploy.sh reads.
_DEPLOY_RC_MARKER = "RC_COMMIT_SHA"

# The tier switch's variable. The driver exports it and greps the candidate for
# it; nothing on main reads it yet.
_EVAL_TIER_MARKER = "EVAL_TIER"


def _stub(trace: pathlib.Path, name: str, body: str = "", exit_code: int = 0) -> str:
    """A sibling script that records that it ran, with what, and in what order."""
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        {{
          echo "STEP {name}"
          echo "  RC_COMMIT_SHA=${{RC_COMMIT_SHA:-unset}}"
          echo "  TIER=${{EVAL_TIER:-unset}}"
          echo "  HEAD=$(git rev-parse HEAD)"
        }} >> "{trace}"
        {body}
        exit {exit_code}
        """
    )


class RcEvalDriverTestCase(unittest.TestCase):
    """Runs the real hack/ci-eval-rc.sh against a stubbed candidate tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.base = pathlib.Path(self._tmp.name)
        # Both live outside the repository: a checkout must not be able to
        # delete the record of what happened before it.
        self.trace = self.base / "trace.txt"
        self.trace.write_text("", encoding="utf-8")
        self.artifacts = self.base / "artifacts"
        self.artifacts.mkdir()

    def build_repo(
        self,
        *,
        driver_at_candidate: bool = True,
        deploy_supports_rc: bool = True,
        eval_supports_tier: bool = True,
        eval_exit_code: int = 0,
        deploy_exit_code: int = 0,
        truncate_driver_from_deploy: bool = False,
    ) -> tuple[pathlib.Path, str]:
        """A repository with a candidate commit and a later HEAD to start from.

        Returns the repository root and the candidate's SHA. `hack/` is
        populated at the candidate commit, because the candidate's tree is what
        runs after the checkout — that is the whole point of doing one.
        """
        # A str, not the Path: create_mock_git_repo treats anything with a
        # `.name` as a TemporaryDirectory, and Path.name is its basename.
        _, repo, git = create_mock_git_repo(str(self.base))
        root = pathlib.Path(repo)
        hack = root / "hack"
        hack.mkdir()

        shutil.copy(_CI_EVAL_RC, hack / "ci-eval-rc.sh")

        # resolve-rc-target.sh's real contract: the SHA on stdout, everything
        # else on stderr, the tag written through RC_TARGET_OUTPUT. The SHA it
        # must print is not known until the commit exists, so it is patched in
        # below once the tree has been committed.
        (hack / "resolve-rc-target.sh").write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                set -euo pipefail
                echo "STEP resolve" >> "{self.trace}"
                echo "resolving" >&2
                if [ -n "${{RC_TARGET_OUTPUT:-}}" ]; then
                  echo "rc_tag={_RC_TAG}" >> "${{RC_TARGET_OUTPUT}}"
                  echo "rc_commit_sha=__CANDIDATE_SHA__" >> "${{RC_TARGET_OUTPUT}}"
                fi
                echo "__CANDIDATE_SHA__"
                """
            ),
            encoding="utf-8",
        )

        # The guard greps the candidate's ci-deploy.sh for the marker, so a
        # stub standing in for a candidate that predates the path must not
        # mention it ANYWHERE — including in the trace lines _stub emits,
        # which is why that variant is written out longhand.
        if deploy_supports_rc:
            deploy_body = f'echo "  marker {_DEPLOY_RC_MARKER}" >> "{self.trace}"'
            if truncate_driver_from_deploy:
                # Truncate the driver IN PLACE, which is the mechanism the
                # wrapper exists for and the one a checkout does not perform:
                # git replaces a file by rename, leaving the running shell's
                # descriptor on the intact original inode, so no checkout can
                # reproduce this. `: >` keeps the inode and drops it to zero
                # bytes, so a shell still reading from disk hits EOF at its
                # offset and the remaining steps silently vanish.
                #
                # The path resolves to the same inode the driver is running
                # from only because the candidate's copy of it is byte-identical
                # to HEAD's, so the checkout left the file alone.
                deploy_body += f'\n: > "{hack / "ci-eval-rc.sh"}"'
            deploy_stub = _stub(
                self.trace, "deploy", body=deploy_body, exit_code=deploy_exit_code
            )
        else:
            deploy_stub = textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                set -euo pipefail
                echo "STEP deploy" >> "{self.trace}"
                echo "  building from source" >> "{self.trace}"
                """
            )
        (hack / "ci-deploy.sh").write_text(deploy_stub, encoding="utf-8")

        # The tier note keys off the string EVAL_TIER appearing in the
        # candidate's ci-eval-pr.sh, so the stub for a candidate that predates
        # the tier must not mention it anywhere — including in its trace lines.
        if eval_supports_tier:
            eval_stub = _stub(self.trace, "eval", exit_code=eval_exit_code)
        else:
            eval_stub = textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                set -euo pipefail
                echo "STEP eval" >> "{self.trace}"
                exit {eval_exit_code}
                """
            )
        (hack / "ci-eval-pr.sh").write_text(eval_stub, encoding="utf-8")

        for script in hack.iterdir():
            script.chmod(0o755)

        if not driver_at_candidate:
            (hack / "ci-eval-rc.sh").unlink()

        git("add", "-A")
        git("commit", "-m", "feat: the release candidate")
        candidate = git("rev-parse", "HEAD").stdout.strip()

        # The resolver can only name the candidate once it exists, and the
        # patched copy must be the one at HEAD rather than at the candidate:
        # the driver runs the resolver BEFORE the checkout.
        resolver = hack / "resolve-rc-target.sh"
        resolver.write_text(
            resolver.read_text(encoding="utf-8").replace(
                "__CANDIDATE_SHA__", candidate
            ),
            encoding="utf-8",
        )
        # HEAD moves past the candidate so the checkout is a real move, and
        # the driver is restored here whether or not the candidate carries it.
        shutil.copy(_CI_EVAL_RC, hack / "ci-eval-rc.sh")
        (hack / "ci-eval-rc.sh").chmod(0o755)
        resolver.chmod(0o755)
        (root / "later.txt").write_text("a commit after the candidate\n")
        git("add", "-A")
        git("commit", "-m", "chore: main has moved on")

        return root, candidate

    def run_driver(self, root: pathlib.Path, **env) -> subprocess.CompletedProcess:
        environ = {
            **os.environ,
            "RC_EVAL_ENABLED": "1",
            "ARTIFACTS": str(self.artifacts),
            "JOB_NAME": _JOB_NAME,
            "BUILD_ID": _BUILD_ID,
        }
        environ.pop("PULL_NUMBER", None)
        environ.pop("RC_TAG", None)
        for key, value in env.items():
            if value is None:
                environ.pop(key, None)
            else:
                environ[key] = value
        return subprocess.run(
            ["bash", str(root / "hack" / "ci-eval-rc.sh")],
            cwd=root,
            env=environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def steps(self) -> list[str]:
        return [
            line.split(" ", 1)[1]
            for line in self.trace.read_text(encoding="utf-8").splitlines()
            if line.startswith("STEP ")
        ]

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
        # `nightly`, not a value of this lane's own: #1175's switch exits 1 on
        # anything it does not know, so inventing `rc` here would break the day
        # the two land together.
        self.assertEqual(trace.count("TIER=nightly"), 2, trace)

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

    def test_notes_but_allows_a_candidate_without_the_tier_switch(self):
        """A smaller matrix than intended is a note; it is not a wrong verdict.

        This is the ordinary case, not a legacy one: the tier switch is not on
        main, so every candidate reaches here until it lands.
        """
        root, _ = self.build_repo(eval_supports_tier=False)
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("carries no EVAL_TIER switch", result.stdout)
        self.assertIn("presubmit matrix", result.stdout)
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

    def test_the_tier_marker_is_a_string_the_real_ci_eval_pr_reads(self):
        """The driver's export and the candidate's switch must be one string.

        The half that can be checked today is the driver's: it greps the
        candidate for the same name it exports, so a rename on one side alone
        makes the grep vacuous. The candidate's half waits on #1175, which is
        what adds the switch to hack/ci-eval-pr.sh.

        A skip rather than @unittest.expectedFailure, deliberately. An
        expected failure that starts passing is an unexpectedSuccess, which
        unittest counts as a failure and exits 1 — so #1175 landing would have
        reddened main's Python suite, with the failure naming this file rather
        than the change that caused it.
        """
        driver = _CI_EVAL_RC.read_text(encoding="utf-8")
        self.assertIn(
            _EVAL_TIER_MARKER,
            driver,
            "the driver must export the marker it greps the candidate for",
        )
        evaluator = (_REPO_ROOT / "hack" / "ci-eval-pr.sh").read_text(encoding="utf-8")
        if _EVAL_TIER_MARKER not in evaluator:
            self.skipTest(
                f"{_EVAL_TIER_MARKER} is not on main yet (#1175), so the "
                "driver's export is inert and its note fires on every "
                "candidate — the case above covers that state"
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
        """Non-gating is the job config's `|| true`, not a hidden exit 0 here."""
        root, _ = self.build_repo(eval_exit_code=1)
        result = self.run_driver(root)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("RED", result.stdout)
        summary = (self.artifacts / "rc-eval-summary.md").read_text(encoding="utf-8")
        self.assertIn("| Verdict | RED |", summary)
        self.assertIn("does not hold a release", summary)

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
