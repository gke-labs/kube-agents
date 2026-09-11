"""Unit tests for hack/check-tfvar-console.sh.

The script exists because the lifecycle.sh unit tests stub `terraform` and so
never see what the binary really prints (#1309 shipped against a stub that
answered `null` where Terraform answers `tostring(null)`; #1350 fixed it after
the autopush deploy failed). These tests stub `terraform` too -- deliberately:
they check that the script recognises a leaked console shape and reports which
variable leaked it, not what Terraform prints. validate.yml runs the script
against the real binary; that run is the check, this is the check's own test.
"""

import pathlib
import re
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "hack" / "check-tfvar-console.sh"

# What the stub answers for any variable the test does not single out.
_PLAIN_ANSWER = '"plain-value"'


class CheckTfvarConsoleTest(unittest.TestCase):
    def _run(self, answers=None, console_exit=0):
        """Run the script with a stub `terraform` whose console answers per variable.

        `answers` maps a variable name to the line the stub prints for it; every
        other variable gets a plain quoted string, which is what Terraform prints
        for a string variable with a value.
        """
        answers = answers or {}
        cases = "\n".join(
            f'        *"{name}"*) echo \'{line}\' ;;' for name, line in answers.items()
        )
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "terraform"
            stub.write_text(f"""#!/usr/bin/env bash
case "${{1:-}}" in
  init) exit 0 ;;
  console)
    read -r expr
    case "$expr" in
{cases}
        *) echo '{_PLAIN_ANSWER}' ;;
    esac
    exit {console_exit} ;;
  *) echo "unexpected terraform invocation: $*" >&2; exit 1 ;;
esac
""")
            stub.chmod(0o755)
            env = get_isolated_test_env(bin_dir=str(bin_dir))
            return subprocess.run(
                ["bash", str(_SCRIPT)],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(_REPO_ROOT),
            )

    def test_plain_values_pass_and_every_caller_is_swept(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        # The sweep is read out of the source. The GSA guard calls
        # `$(tfvar agent_service_account_id 2>/dev/null)`, so a pattern anchored
        # on a closing paren would skip precisely the variable that regressed;
        # it is also the one string variables.tf declares with `default = null`,
        # so it reaches the sweep by both routes.
        self.assertIn("agent_service_account_id", result.stdout)
        self.assertIn("project_id", result.stdout)

    def test_the_shapes_tfvar_normalises_pass_as_empty(self):
        """`tostring(null)` is the shape #1309 missed and #1350 taught tfvar() to
        read as unset; the check sees the helper's answer, so both pass empty.
        Against the helper as #1309 left it, this case fails with the message
        the test below asserts."""
        for shape in ("tostring(null)", "null"):
            with self.subTest(shape=shape):
                result = self._run(answers={"agent_service_account_id": shape})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertRegex(result.stdout, r"agent_service_account_id\s+-> <empty>")

    def test_the_shapes_tfvar_does_not_normalise_fail_and_name_the_variable(self):
        """A typed null of another type, a typed null list, and an unresolved
        required variable: what the next nullable variable a caller reads could
        come back as."""
        for shape in ("tobool(null)", "tolist(null) /* of string */", "(known after apply)"):
            with self.subTest(shape=shape):
                result = self._run(answers={"namespace": shape})
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"tfvar namespace returned raw console output '{shape}'", result.stderr)

    def test_the_sweep_includes_every_nullable_string_in_variables_tf(self):
        """The second route into the sweep. Its only member today is also a
        literal caller, so the sweep's output cannot show whether the route
        works; the list mode can. The expected set is parsed here with a
        different tool than the script's awk, so a formatting change in
        variables.tf that one of them misses fails rather than agrees."""
        variables_tf = (_REPO_ROOT / "terraform" / "examples" / "full-install" / "variables.tf").read_text()
        blocks = re.findall(r'^variable "([^"]+)" \{(.*?)^\}', variables_tf, re.M | re.S)
        expected = {
            name for name, body in blocks
            if re.search(r"^\s*type\s*=\s*string\s*$", body, re.M)
            and re.search(r"^\s*default\s*=\s*null\s*$", body, re.M)
        }
        self.assertIn("agent_service_account_id", expected)
        result = subprocess.run(
            ["bash", str(_SCRIPT), "--list"],
            capture_output=True, text=True, cwd=str(_REPO_ROOT),
            env=get_isolated_test_env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        listed = set(result.stdout.split())
        self.assertTrue(expected <= listed, f"nullable strings missing from the sweep: {expected - listed}")
        self.assertIn("project_id", listed)

    def test_a_populated_list_shape_fails(self):
        """tfvar keeps the last line of a multi-line console value, so a
        populated list reaches the check as `])`."""
        result = self._run(answers={"namespace": "])"})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("tfvar namespace returned raw console output '])'", result.stderr)

    def test_a_failing_console_fails_the_check_for_that_variable(self):
        result = self._run(console_exit=1)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("terraform console could not evaluate it", result.stderr)

if __name__ == "__main__":
    unittest.main()
