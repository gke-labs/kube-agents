"""The fixture behind the release-candidate eval driver's tests.

Builds a throwaway git repository carrying a real `hack/ci-eval-rc.sh` with its
three siblings stubbed, and runs the driver in it. Shared rather than inherited:
`tests/test_ci_eval_rc.py` uses it for the driver's own unit tests and
`tests/integration/test_seam_rc_eval_verdict.py` uses it to produce a real
summary artifact for the verdict poller to read. Subclassing the test case would
have run the first suite a second time inside the second tier.

Not a TestCase. The caller owns the lifecycle: construct it in `setUp` and
register `cleanup` with `addCleanup`.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap

from tests.testing.common import create_mock_git_repo

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CI_EVAL_RC = _REPO_ROOT / "hack" / "ci-eval-rc.sh"

RC_TAG = "rc_2609021231_fdba3a7"
JOB_NAME = "ci-kube-agents-eval-rc"
BUILD_ID = "2095176282760286208"
EXPECTED_DECK_URL = (
    f"https://oss.gprow.dev/view/gs/kube-agents-prow/logs/{JOB_NAME}/{BUILD_ID}"
)

# A stub ci-deploy.sh has to carry this for the driver's predates-the-RC-path
# guard to let it through; the real marker is the variable ci-deploy.sh reads.
DEPLOY_RC_MARKER = "RC_COMMIT_SHA"

# The tier switch's variable. The driver exports it and greps the candidate for
# it; nothing on main reads it yet.
EVAL_TIER_MARKER = "EVAL_TIER"

# bench-gate's per-case roll-up, which the real hack/ci-eval-pr.sh writes with
# --markdown-out. RC_VERDICT_FILE in hack/ci-eval-rc.sh is the other half of the
# pair: the driver reads its presence to tell a graded catalog from a preflight
# refusal, so the two names have to stay in step.
VERDICT_FILE = "eval-verdict.md"


def stub(trace: pathlib.Path, name: str, body: str = "", exit_code: int = 0) -> str:
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


class RcEvalDriverFixture:
    """Runs the real hack/ci-eval-rc.sh against a stubbed candidate tree."""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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
        eval_writes_verdict: bool = True,
        deploy_exit_code: int = 0,
        truncate_driver_from_deploy: bool = False,
    ) -> tuple[pathlib.Path, str]:
        """A repository with a candidate commit and a later HEAD to start from.

        Returns the repository root and the candidate's SHA. `hack/` is
        populated at the candidate commit, because the candidate's tree is what
        runs after the checkout — that is the whole point of doing one.

        `eval_writes_verdict` is what separates the two ways the eval can exit
        non-zero, and the driver now reads them differently: bench-gate's
        `--markdown-out` is written by the suite roll-up, so a failing eval that
        produced it graded a catalog and is RED, and one that did not stopped in
        its preflight and is NOT RUN. Default True, because a stub that grades
        nothing at all is the unusual case rather than the normal one.
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
                  echo "rc_tag={RC_TAG}" >> "${{RC_TARGET_OUTPUT}}"
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
            deploy_body = f'echo "  marker {DEPLOY_RC_MARKER}" >> "{self.trace}"'
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
            deploy_stub = stub(
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
        # bench-gate's roll-up artifact, written before the exit so that a
        # non-zero eval still leaves it behind: that ordering is what makes the
        # file mean "a catalog was graded" rather than "the eval succeeded".
        verdict_body = ""
        if eval_writes_verdict:
            verdict_body = (
                f'printf "# eval verdict\\n" > "{self.artifacts / VERDICT_FILE}"'
            )
        if eval_supports_tier:
            eval_stub = stub(
                self.trace, "eval", body=verdict_body, exit_code=eval_exit_code
            )
        else:
            eval_stub = textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                set -euo pipefail
                echo "STEP eval" >> "{self.trace}"
                {verdict_body}
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
            "JOB_NAME": JOB_NAME,
            "BUILD_ID": BUILD_ID,
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

    def cleanup(self):
        self._tmp.cleanup()
