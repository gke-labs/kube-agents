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
by version and SHA-256 rather than taking the runner image's apt package.
tests/test_minter_secret_wiring.py pins workflow wiring the same way.
"""

import pathlib
import re
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


def _steps() -> list:
    return yaml.safe_load(_WORKFLOW.read_text())["jobs"][_JOB_ID]["steps"]


def _run_lines(step: dict) -> list:
    return [line.strip() for line in str(step.get("run", "")).splitlines()]


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
            "package is releases behind what the tree is cleared against, and an "
            "unpinned download turns a new upstream rule into a red check on a pull "
            "request that changed no shell.",
        )
        env = pinned[0]["env"]
        self.assertRegex(str(env[_VERSION_ENV]), _VERSION_TAG)
        self.assertRegex(str(env[_SHA256_ENV]), _SHA256_HEX)
        self.assertIn(
            "sha256sum -c",
            str(pinned[0]["run"]),
            "the pinned SHA-256 has to be checked, not just declared.",
        )


if __name__ == "__main__":
    unittest.main()
