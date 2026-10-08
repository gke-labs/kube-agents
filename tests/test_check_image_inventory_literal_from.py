"""`check_literal_from` in hack/check-image-inventory.sh fails when an agent
plugin Dockerfile's literal `FROM` pin and the inventory's entry move apart,
and fails closed on a Dockerfile that is anything other than comment lines,
blank lines, `FROM <ref>` and `COPY <src> /`: the one shape both plugin
builders (`docker build` and the crane reader in agentplugins/lib/plugin_image.sh)
read alike. The fence carries no grammar; it compares the stripped body to
those two lines.

The plugin images pin `busybox:musl` by digest in a literal FROM rather than an
ARG pair, so `check_base_image` never reaches them. CI only ever runs the script
on a tree where this check passes, so its fail paths would otherwise execute
nowhere. The function is lifted from the script's own text and run under bash
against a synthetic Dockerfile, as
tests/test_check_image_inventory_go_directive.py does for the Go directive.
"""

import pathlib
import subprocess
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _lift_shell import lift_function  # noqa: E402

_REPO_ROOT = _HERE.parent
_SCRIPT = _REPO_ROOT / "hack" / "check-image-inventory.sh"

# Lifted by name: a rename fails here loudly instead of silently shrinking
# what is tested. repo_of and pin_of read images.json through jq and are
# stubbed below instead, as test_check_image_inventory_operator_pins.py does:
# jq is not on the Python test runner's PATH.
_LIFTED_FUNCTIONS = ("fail", "normalise", "check_literal_from")

# The call sites, asserted present because the lift below supplies its own;
# the third argument is the COPY source each plugin Dockerfile names.
_CALL_SITES = (
    "check_literal_from busybox agentplugins/pubsub-platform/Dockerfile files/platforms/pubsub/",
    "check_literal_from busybox agentplugins/gke-stockout-investigator/Dockerfile files/",
)

# The sweep after the call sites: a plugin Dockerfile the fence never saw is
# a failure, so a third plugin copied from the pair cannot drift unnoticed.
# Its invocation is asserted as a whole line, because the bare name also
# occurs in the function's definition and in a comment.
_SWEEP = "check_plugin_dockerfiles_are_fenced"
_SWEEP_INVOCATION = f"\n{_SWEEP}\n"
_PLUGIN_DIR_DECLARATION = "readonly PLUGIN_DIR=agentplugins"
_UNFENCED = "has no check_literal_from call"

_NAME = "busybox"
_REPOSITORY = "docker.io/library/busybox"
_PIN = "musl@sha256:" + "a" * 64
_OTHER_PIN = "musl@sha256:" + "b" * 64
_SRC = "files/"
_SHAPE = "must be exactly two lines"


def _run_check(dockerfile: str, pin: str = _PIN) -> subprocess.CompletedProcess:
    text = _SCRIPT.read_text()
    functions = "".join(lift_function(name, text, _SCRIPT) for name in _LIFTED_FUNCTIONS)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        script = (
            "set -u\nstatus=0\nINVENTORY=images.json\n"
            f'repo_of() {{ echo "{_REPOSITORY}"; }}\n'
            f'pin_of() {{ echo "{pin}"; }}\n'
            + functions
            + f"check_literal_from {_NAME} Dockerfile {_SRC}\nexit $status\n"
        )
        return subprocess.run(
            ["bash", "-c", script], cwd=root, capture_output=True, text=True, check=False
        )


def _run_sweep(present: tuple, fenced: tuple, body: str = None) -> subprocess.CompletedProcess:
    """Plant `body` (default: the shape, pinned as the inventory) as the Dockerfile
    of every plugin in `present`, fence those in `fenced`, then sweep."""
    if body is None:
        body = f"FROM busybox:{_PIN}\nCOPY {_SRC} /\n"
    text = _SCRIPT.read_text()
    functions = "".join(lift_function(name, text, _SCRIPT) for name in _LIFTED_FUNCTIONS + (_SWEEP,))
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        for plugin in present:
            plugin_dir = root / "agentplugins" / plugin
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "Dockerfile").write_text(body, encoding="utf-8")
        calls = "".join(f"check_literal_from {_NAME} agentplugins/{plugin}/Dockerfile {_SRC}\n" for plugin in fenced)
        script = (
            f"set -u\nstatus=0\nINVENTORY=images.json\n{_PLUGIN_DIR_DECLARATION}\n"
            f'repo_of() {{ echo "{_REPOSITORY}"; }}\n'
            f'pin_of() {{ echo "{_PIN}"; }}\n'
            + functions
            + calls
            + f"{_SWEEP}\nexit $status\n"
        )
        return subprocess.run(
            ["bash", "-c", script], cwd=root, capture_output=True, text=True, check=False
        )


class CheckLiteralFromTest(unittest.TestCase):
    def test_the_one_shape_passes(self):
        # The two instruction lines, with comment and blank lines of any
        # content around them: a commented-out `docker build \` block, an em
        # dash, a `# word=` note below the FROM, which is a comment to Docker
        # and to the fence alike.
        for text in (
            f"FROM busybox:{_PIN}\nCOPY {_SRC} /\n",
            f"FROM {_REPOSITORY}:{_PIN}\nCOPY {_SRC} /\n",
            f"# docker build --platform linux/amd64 \\\n#   -t plugin .\n\nFROM busybox:{_PIN}\n\n  # Pinned by digest \u2014 see images.json \u2026\nCOPY {_SRC} /\n",
            f"FROM busybox:{_PIN}\n# platform=linux/amd64 is the only one the operator schedules\nCOPY {_SRC} /\n",
        ):
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_drifted_digest_fails_naming_the_dockerfile(self):
        result = _run_check(f"FROM busybox:{_OTHER_PIN}\nCOPY {_SRC} /\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Dockerfile: FROM pins", result.stderr)
        self.assertIn(_PIN, result.stderr)

    def test_drifted_inventory_fails(self):
        result = _run_check(f"FROM busybox:{_PIN}\nCOPY {_SRC} /\n", pin=_OTHER_PIN)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(_OTHER_PIN, result.stderr)

    def test_anything_else_fails_closed_with_the_shape(self):
        # Every form the two builders would read differently, and every form
        # that is merely not the shape: refused with the expected lines and
        # the body found, never reported as a pin in step or as drift.
        cases = (
            f"from busybox:{_PIN}\nCOPY {_SRC} /\n",
            f"From busybox:{_PIN}\nCOPY {_SRC} /\n",
            f"  FROM busybox:{_PIN}\nCOPY {_SRC} /\n",
            f"FROM  busybox:{_PIN}\nCOPY {_SRC} /\n",
            f"FROM\tbusybox:{_PIN}\nCOPY {_SRC} /\n",
            f"FROM busybox:{_PIN} AS base\nCOPY {_SRC} /\n",
            f"FROM busybox:{_PIN}\tAS base\nCOPY {_SRC} /\n",
            f"FROM busybox:{_PIN}\x0bAS base\nCOPY {_SRC} /\n",
            f"FROM --platform=linux/amd64 busybox:{_PIN}\nCOPY {_SRC} /\n",
            f"FROM --platform=linux/arm64 busybox:{_PIN}\nCOPY {_SRC} /\n",
            f"FROM busybox:{_PIN}\r\nCOPY {_SRC} /\r\n",
            f"\ufeffFROM busybox:{_PIN}\nCOPY {_SRC} /\n",
            f"FROM busybox:{_PIN}\n FROM busybox:{_OTHER_PIN}\nCOPY {_SRC} /\n",
            f"FROM busybox:{_PIN} \\\n    AS base\nCOPY {_SRC} /\n",
            f"FROM busybox:{_PIN} AS base\nRUN echo \\\\\nFROM busybox:{_OTHER_PIN}\nCOPY {_SRC} /\n",
            f"FROM golang:1.27-alpine AS build\nFROM busybox:{_PIN}\nCOPY {_SRC} /\n",
            f"FROM busybox:{_PIN}\nRUN rm -rf /bin\nCOPY {_SRC} /\n",
            f"FROM busybox:{_PIN}\nCOPY <<EOF /x\nFROM busybox:{_OTHER_PIN}\nEOF\n",
            f"FROM busybox:{_PIN}\nCOPY <<EOF /\n",
            f"FROM busybox:{_PIN}\nCOPY --from=alpine:latest /bin/busybox /bin/busybox\nCOPY {_SRC} /\n",
            f"FROM busybox:{_PIN}\ncopy --chown=1000:1000 {_SRC} /\n",
            f"FROM busybox:{_PIN}\nCOPY {_SRC} /opt/plugin\n",
            f"FROM busybox:{_PIN}\nCOPY {_SRC} /filesystem\n",
            f"FROM busybox:{_PIN}\nCOPY {_SRC} /files/\n",
            f"FROM busybox:{_PIN}\nCOPY other/ /\n",
            f"FROM busybox:{_PIN}\n",
            f"FROM \nCOPY {_SRC} /\n",
            f"FROM docker.io/library/\nCOPY {_SRC} /\n",
            f"COPY {_SRC} /\n",
            "",
        )
        for text in cases:
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(_SHAPE, result.stderr)
                self.assertNotIn("FROM pins", result.stderr)

    def test_invisible_bytes_are_shown_in_the_failure_unfolded(self):
        result = _run_check(f"FROM\tbusybox:{_PIN}\nCOPY {_SRC} /\r\n")
        self.assertIn(f"FROM^Ibusybox:{_PIN}$", result.stderr)
        self.assertIn("COPY files/ /^M$", result.stderr)

    def test_equals_in_the_leading_comments_fails_closed(self):
        # `# syntax=` names a frontend image BuildKit pulls and runs, unpinned,
        # and BuildKit reads the blanks around the key wider than any class
        # worth transcribing (a form feed or a mid-line CR counts), so the
        # fence refuses any `=` in the leading run of comments by name, before
        # comments are dropped. That run is wider than Docker's directive
        # window on purpose (a directive behind a shebang line is live), so a
        # comment with `=` above a plain comment is refused too, and belongs
        # below the FROM, where test_the_one_shape_passes accepts it.
        for text, line in (
            (f"# syntax=docker/dockerfile:1\nFROM busybox:{_PIN}\nCOPY {_SRC} /\n", "# syntax=docker/dockerfile:1"),
            (f"#\x0csyntax=docker/dockerfile:1\nFROM busybox:{_PIN}\nCOPY {_SRC} /\n", "#^Lsyntax=docker/dockerfile:1"),
            (f"#\rsyntax=docker/dockerfile:1\nFROM busybox:{_PIN}\nCOPY {_SRC} /\n", "#^Msyntax=docker/dockerfile:1"),
            (f"#!/usr/bin/env -S docker build -f\n# syntax=docker/dockerfile:1\nFROM busybox:{_PIN}\nCOPY {_SRC} /\n", "# syntax=docker/dockerfile:1"),
            (f"# a note first\n#escape=`\nFROM busybox:{_PIN}\nCOPY {_SRC} /\n", "#escape=`"),
            (f"# digest=ea2b9914 is what the tag resolved to\nFROM busybox:{_PIN}\nCOPY {_SRC} /\n", "# digest=ea2b9914 is what the tag resolved to"),
        ):
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertNotEqual(result.returncode, 0)
                # The offending line is rendered through cat -vt, so a CR or
                # FF in it shows as ^M or ^L instead of moving the cursor.
                self.assertIn(f"line '{line}' holds '=' in the leading run of comments", result.stderr)
                self.assertNotIn("FROM pins", result.stderr)

    def test_script_calls_the_check_for_plugin_dockerfiles(self):
        text = _SCRIPT.read_text()
        for call in _CALL_SITES:
            self.assertIn(call, text)
        self.assertIn(_PLUGIN_DIR_DECLARATION, text)
        self.assertIn(_SWEEP_INVOCATION, text)

    def test_a_plugin_dockerfile_without_a_fence_call_fails_naming_it(self):
        result = _run_sweep(present=("alpha", "beta"), fenced=("alpha",))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"agentplugins/beta/Dockerfile {_UNFENCED}", result.stderr)
        self.assertNotIn("agentplugins/alpha/Dockerfile", result.stderr)

    def test_every_plugin_dockerfile_fenced_passes(self):
        result = _run_sweep(present=("alpha", "beta"), fenced=("alpha", "beta"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_a_fenced_dockerfile_that_fails_the_fence_counts_as_fenced(self):
        # The fence records the file before it judges it, so a file it refuses
        # is reported once, as drift or as the shape, and not a second time as
        # unfenced. Both exits are covered: drift is the function's last
        # statement, the shape fault returns early.
        for body, reported in (
            (f"FROM busybox:{_OTHER_PIN}\nCOPY {_SRC} /\n", "FROM pins"),
            (f"FROM busybox:{_PIN}\nRUN rm -rf /bin\nCOPY {_SRC} /\n", _SHAPE),
        ):
            with self.subTest(body=body):
                result = _run_sweep(present=("alpha",), fenced=("alpha",), body=body)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reported, result.stderr)
                self.assertNotIn(_UNFENCED, result.stderr)


if __name__ == "__main__":
    unittest.main()
