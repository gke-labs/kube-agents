"""`make shellcheck` still gates: the `validate` job runs it, unconditionally.

The shell lint is a merge gate only because a step of the `validate` job in
validate.yml runs `make shellcheck` -- that job is on main's required
status checks and a new job would not be. Four places describe it that way
(the Makefile comment above the target, `.agents/rules/core_engineering.md`,
`docs/pull-request-workflow.md`, and the docstring of
tests/test_make_help_targets.py), and none of them notices when the step is
removed, renamed, or put behind an `if:`. This does: it reads the workflow
and asserts the step is there, runs the Makefile target rather than its own
shellcheck invocation, and that the install step ahead of it pins a release
by version and SHA-256. `PreinstalledShellcheckTest` runs that install step
against stubs: it takes the runner image's apt package when that package is
already the pinned release, and downloads the pin otherwise.
tests/test_minter_secret_wiring.py pins workflow wiring the same way.
"""

import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "validate.yml"

#: The job on main's required-status-checks list; the gate holds only there.
_JOB_ID = "validate"

#: The recipe the gating step must run, so it and a contributor's local run
#: share the Makefile's pathspec, severity and exclude list.
_GATE_COMMAND = "make shellcheck"

#: The install step's pin, as `env:` keys: a release tag and its archive's SHA-256.
_VERSION_ENV = "SHELLCHECK_VERSION"
_SHA256_ENV = "SHELLCHECK_SHA256"
_VERSION_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

#: The step that puts the pinned shellcheck on PATH, found by its name.
_INSTALL_STEP = "Install shellcheck"

#: What `shellcheck --version` prints; the step matches the version line whole.
_VERSION_OUTPUT = """ShellCheck - shell script analysis tool
version: {version}
license: GNU General Public License, version 3
website: https://www.shellcheck.net
"""

#: A release other than the pin: what Ubuntu 24.04's apt package reports.
_OTHER_RELEASE = "0.9.0"

#: Stands in for the runner image's shellcheck, printing `--version` output.
_SHELLCHECK_STUB = "#!/bin/sh\ncat <<'EOF'\n{output}EOF\n"

#: bash's exit status for a command it cannot find.
_COMMAND_NOT_FOUND = 127

#: Stands in for a runner image with no shellcheck on PATH.
_ABSENT_STUB = f"#!/bin/sh\nexit {_COMMAND_NOT_FOUND}\n"

#: curl's exit status for an HTTP error, which the release download's 504 gave.
_CURL_HTTP_ERROR = 22

#: Stands in for curl: records each call and fails as the 504 did.
_CURL_STUB = '#!/bin/sh\necho "$*" >> "{calls}"\nexit {status}\n'

#: Ceiling on one run of the step, which reaches nothing but the stubs.
_STEP_TIMEOUT_SECONDS = 30


def _steps() -> list:
    return yaml.safe_load(_WORKFLOW.read_text())["jobs"][_JOB_ID]["steps"]


def _run_lines(step: dict) -> list:
    return [line.strip() for line in str(step.get("run", "")).splitlines()]


def _executable(path: pathlib.Path, script: str) -> None:
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class ShellcheckGateWiringTest(unittest.TestCase):
    def test_the_validate_job_runs_make_shellcheck_unconditionally(self):
        gate = [s for s in _steps() if _GATE_COMMAND in _run_lines(s)]
        self.assertEqual(
            len(gate),
            1,
            f"the `{_JOB_ID}` job in {_WORKFLOW.name} must run `{_GATE_COMMAND}` in "
            "exactly one step; it is the only required check that reaches the shell "
            "lint, and the Makefile, the rules file and the workflow doc all say it does.",
        )
        self.assertNotIn(
            "if",
            gate[0],
            f"the `{_GATE_COMMAND}` step must not carry an `if:`; a conditional gate "
            "is a gate the pull requests it skips never meet.",
        )

    def test_a_pinned_release_is_installed_before_the_gate(self):
        steps = _steps()
        gate_index = next(i for i, s in enumerate(steps) if _GATE_COMMAND in _run_lines(s))
        pinned = [
            s for s in steps[:gate_index]
            if _VERSION_ENV in s.get("env", {}) and _SHA256_ENV in s.get("env", {})
        ]
        self.assertEqual(
            len(pinned),
            1,
            f"a step before `{_GATE_COMMAND}` must install shellcheck with both "
            f"{_VERSION_ENV} and {_SHA256_ENV} in its env: the runner image's apt "
            "package is whatever release its Ubuntu ships, and an unpinned download "
            "turns a new upstream rule into a red check on a pull request that "
            "changed no shell.",
        )
        env = pinned[0]["env"]
        self.assertRegex(str(env[_VERSION_ENV]), _VERSION_TAG)
        self.assertRegex(str(env[_SHA256_ENV]), _SHA256_HEX)
        self.assertIn(
            "sha256sum -c",
            str(pinned[0]["run"]),
            "the pinned SHA-256 has to be checked, not just declared.",
        )


class PreinstalledShellcheckTest(unittest.TestCase):
    """The install step downloads the pin only when PATH does not already hold it.

    When the runner image's apt package is the pinned release, as on the
    `ubuntu-26.04` image, the step skips the download, the part a GitHub
    outage fails: on 2026-11-05 the release URL returned 504 for longer than
    curl retries, in four runs within 70 minutes. The step is run here, not
    read: its `run:` block executes under the shell GitHub gives it, with a
    stub `shellcheck` that reports a chosen version and a stub `curl` that
    fails as the 504 did.
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="shellcheck-install-")).resolve()
        self.stubs = self.tmp / "bin"
        self.stubs.mkdir()
        self.curl_calls = self.tmp / "curl-calls"
        _executable(self.stubs / "curl", _CURL_STUB.format(calls=self.curl_calls, status=_CURL_HTTP_ERROR))
        self.step = next(s for s in _steps() if s.get("name") == _INSTALL_STEP)
        self.tag = str(self.step["env"][_VERSION_ENV])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, shellcheck_stub: str) -> subprocess.CompletedProcess:
        _executable(self.stubs / "shellcheck", shellcheck_stub)
        self.curl_calls.write_text("")
        env = dict(os.environ, **{key: str(value) for key, value in self.step["env"].items()})
        env.update(PATH=f"{self.stubs}{os.pathsep}{os.environ['PATH']}", RUNNER_TEMP=str(self.tmp))
        return subprocess.run(
            ["bash", "-e", "-c", self.step["run"]],
            capture_output=True,
            text=True,
            env=env,
            timeout=_STEP_TIMEOUT_SECONDS,
        )

    def _reporting(self, version: str) -> str:
        return _SHELLCHECK_STUB.format(output=_VERSION_OUTPUT.format(version=version))

    def test_the_pinned_release_on_path_skips_the_download(self):
        result = self._run(self._reporting(self.tag.removeprefix("v")))
        self.assertEqual(
            self.curl_calls.read_text(),
            "",
            "the binary on PATH is already the pinned release, so the step must not "
            "download it: that download is what a GitHub release outage fails.",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_any_other_shellcheck_still_downloads_the_pin(self):
        for label, stub in (
            ("another release", self._reporting(_OTHER_RELEASE)),
            ("none on PATH", _ABSENT_STUB),
        ):
            with self.subTest(label):
                result = self._run(stub)
                self.assertEqual(result.returncode, _CURL_HTTP_ERROR, result.stdout + result.stderr)
                calls = self.curl_calls.read_text().splitlines()
                self.assertEqual(len(calls), 1, "the step must fall through to the pinned download")
                self.assertIn(f"/releases/download/{self.tag}/", calls[0])


if __name__ == "__main__":
    unittest.main()
