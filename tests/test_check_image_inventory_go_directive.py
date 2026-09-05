"""`check_go_directive` in hack/check-image-inventory.sh fails when the Go builder
pin and k8s-operator/go.mod's `go` directive move apart (#1138).

CI only ever runs the script on a tree where the check passes, so every fail
path -- and the whole patch-tag branch -- would otherwise execute nowhere.
The function is lifted from the script's own text and run under bash against
synthetic go.mod and Dockerfile inputs, so the assertions are against the code
that ships rather than a copy (the approach of tests/test_ci_teardown_sweep.py).
"""

import pathlib
import re
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "hack" / "check-image-inventory.sh"

# The three functions the check needs, each lifted by name. A rename fails
# here loudly instead of silently shrinking what is tested.
_LIFTED_FUNCTIONS = ("fail", "arg_default", "check_go_directive")

# The call sites, asserted present because the lift below supplies its own:
# a function that is defined and never called keeps every gate green.
_CALL_SITES = (
    "check_go_directive deploy/docker/Dockerfile GOLANG_VERSION",
    "check_go_directive k8s-operator/Dockerfile GOLANG_VERSION",
)

# What the check requires of the builder stage besides the ARG default, and
# the FROM line that opens that stage.
_TOOLCHAIN_PIN = "ENV GOTOOLCHAIN=local"
_BUILDER_FROM = "FROM ${GOLANG_IMAGE}:${GOLANG_VERSION} AS builder"

# (builder tag, go directive, expected to pass, fragment the failure names).
# Pass cases: equal major.minor, whatever the patch; a patch or pre-release
# tag at or above the directive. Fail cases: either side ahead on major.minor,
# a patch or pre-release tag below the directive, a tag with no minor.
_CASES = (
    ("1.27-alpine", "1.27.0", True, ""),
    ("1.27-alpine", "1.27.3", True, ""),
    ("1.27-alpine", "1.27", True, ""),
    ("1.27.3-alpine", "1.27.0", True, ""),
    ("1.27.3-alpine", "1.27.3", True, ""),
    ("1.28rc1-alpine", "1.28rc1", True, ""),
    ("1.28-alpine", "1.28rc1", True, ""),
    ("1.27-alpine", "1.28.0", False, "Move both together"),
    ("1.26-alpine", "1.27.0", False, "Move both together"),
    ("1.28.0-alpine", "1.27.0", False, "Move both together"),
    ("1.29.3-bookworm", "1.27.0", False, "Move both together"),
    ("1.27.0-alpine", "1.27.3", False, "below the 'go 1.27.3' floor"),
    ("1.28rc1-alpine", "1.28.0", False, "below the 'go 1.28.0' floor"),
    ("1.28rc1-alpine", "1.28rc2", False, "below the 'go 1.28rc2' floor"),
    ("1-alpine", "1.27.0", False, "does not name a Go major.minor"),
    ("alpine", "1.27.0", False, "does not name a Go major.minor"),
    ("latest", "1.27.0", False, "does not name a Go major.minor"),
)


def _lift(name: str, text: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    if match is None:
        raise AssertionError(f"{_SCRIPT} no longer defines {name}()")
    return match.group(0)


def _dockerfile(tag: str) -> str:
    return f"ARG GOLANG_VERSION={tag}\n{_BUILDER_FROM}\n{_TOOLCHAIN_PIN}\nFROM scratch\n"


def _run_check(go_mod: str, dockerfile: str) -> subprocess.CompletedProcess:
    text = _SCRIPT.read_text()
    functions = "".join(_lift(name, text) for name in _LIFTED_FUNCTIONS)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "go.mod").write_text(go_mod)
        (root / "Dockerfile").write_text(dockerfile)
        script = (
            "set -u\nstatus=0\nGO_MOD=go.mod\nGOLANG_IMAGE_ARG=GOLANG_IMAGE\n"
            f"GOTOOLCHAIN_PIN='{_TOOLCHAIN_PIN}'\n"
            + functions
            + "check_go_directive Dockerfile GOLANG_VERSION\nexit $status\n"
        )
        return subprocess.run(
            ["bash", "-c", script], cwd=root, capture_output=True, text=True, check=False
        )


class CheckGoDirectiveTest(unittest.TestCase):
    def test_builder_tag_against_directive(self):
        for tag, directive, expect_pass, fragment in _CASES:
            with self.subTest(tag=tag, directive=directive):
                result = _run_check(
                    f"module example.com/x\n\ngo {directive}\n",
                    _dockerfile(tag),
                )
                if expect_pass:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                else:
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(fragment, result.stderr)

    def test_directive_is_read_past_toolchain_and_comments(self):
        result = _run_check(
            "module example.com/x\n\ntoolchain go1.28.0\ngodebug x=1\ngo 1.27.0 // floor\n",
            _dockerfile("1.27-alpine"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_directive_fails(self):
        result = _run_check("module example.com/x\n", _dockerfile("1.27-alpine"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("has no 'go' directive", result.stderr)

    def test_unset_arg_fails(self):
        result = _run_check("module example.com/x\n\ngo 1.27.0\n", f"{_BUILDER_FROM}\n{_TOOLCHAIN_PIN}\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("<unset>", result.stderr)

    def test_missing_toolchain_pin_fails(self):
        result = _run_check(
            "module example.com/x\n\ngo 1.27.0\n",
            f"ARG GOLANG_VERSION=1.27-alpine\n{_BUILDER_FROM}\n",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"no line exactly '{_TOOLCHAIN_PIN}'", result.stderr)

    def test_toolchain_pin_in_another_stage_fails(self):
        result = _run_check(
            "module example.com/x\n\ngo 1.27.0\n",
            f"ARG GOLANG_VERSION=1.27-alpine\n{_BUILDER_FROM}\nFROM scratch\n{_TOOLCHAIN_PIN}\n",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"no line exactly '{_TOOLCHAIN_PIN}'", result.stderr)

    def test_toolchain_pin_after_first_run_fails(self):
        result = _run_check(
            "module example.com/x\n\ngo 1.27.0\n",
            f"ARG GOLANG_VERSION=1.27-alpine\n{_BUILDER_FROM}\nRUN go build ./...\n"
            f"{_TOOLCHAIN_PIN}\nFROM scratch\n",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"no line exactly '{_TOOLCHAIN_PIN}'", result.stderr)

    def test_toolchain_pin_after_intermediate_stage_passes(self):
        result = _run_check(
            "module example.com/x\n\ngo 1.27.0\n",
            f"ARG GOLANG_VERSION=1.27-alpine\nFROM alpine AS other\n{_BUILDER_FROM}\n"
            f"WORKDIR /w\n{_TOOLCHAIN_PIN}\nFROM scratch\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_calls_the_check_for_both_dockerfiles(self):
        text = _SCRIPT.read_text()
        for call in _CALL_SITES:
            self.assertIn(call, text)


if __name__ == "__main__":
    unittest.main()
