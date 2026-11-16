# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Every `local-exec` heredoc under `bench/tf` is a shell script bash can parse.

`tofu validate` does not look inside a heredoc -- the body is one string to it
-- so a stack whose provisioner is not valid bash passes every check in this
repository and fails at apply, minutes into a run, against a cluster.

The failure this exists for is specific. `<<-EOT` dedents by the *least
indented* line, so a line placed at a smaller indent than the body silently
sets the dedent to zero and leaves every embedded heredoc terminator
(`MANIFEST`, `POLICY`, `PAYLOAD`) indented. An indented terminator does not
terminate: the rest of the provisioner is swallowed as heredoc content. This
became reachable when the drift stacks started sourcing shared scaffolding,
because a `source` or `${file(...)}` line is exactly the kind of line that gets
re-indented by a later edit.

Two properties of bash make that failure easy to miss, and both shape this file:

- an unterminated heredoc is a **warning on stderr with a zero exit**, so
  checking `bash -n`'s return code alone passes it; and
- bash 3.2, which is what macOS ships, does not emit the warning at all, so the
  check is vacuous there. It skips loudly rather than passing.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = REPO_ROOT / "bench" / "tf"

# `command = <<-EOT ... EOT`, the only shape bench/tf uses for an inline script.
HEREDOC = re.compile(r"command\s*=\s*<<-(\w+)\n(.*?)^(\s*)\1\s*$", re.S | re.M)
# A Terraform interpolation, including one nested level such as
# ${local.ci_labels["managed-by"]}. Replaced wholesale: what is under test is
# the script's structure, not any particular value.
INTERPOLATION = re.compile(r"\$\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")
PLACEHOLDER = "TFVAL"
MIN_BASH_MAJOR = 4


def bash_major_version(bash):
    out = subprocess.run([bash, "-c", "echo $BASH_VERSINFO"], capture_output=True, text=True)
    try:
        return int(out.stdout.strip())
    except ValueError:
        return 0


def dedent_heredoc(body):
    """Apply HCL's `<<-` rule: strip the least-indented line's indent from all."""
    lines = body.split("\n")
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    strip = min(indents) if indents else 0
    return "\n".join(line[strip:] if len(line) >= strip else line for line in lines)


def rendered_scripts():
    """Yield (path, index, script) for every local-exec heredoc under bench/tf."""
    for tf in sorted(TF_ROOT.rglob("*.tf")):
        text = tf.read_text(encoding="utf-8")
        for index, match in enumerate(HEREDOC.finditer(text)):
            script = INTERPOLATION.sub(PLACEHOLDER, dedent_heredoc(match.group(2)))
            yield tf.relative_to(REPO_ROOT), index, script


class TerraformHeredocsAreParseableBash(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        if cls.bash is None:
            raise unittest.SkipTest("no bash on PATH")
        cls.major = bash_major_version(cls.bash)

    def test_the_corpus_is_not_empty(self):
        """A regex that matches nothing would make every assertion below vacuous."""
        found = list(rendered_scripts())
        self.assertGreater(
            len(found),
            10,
            f"found only {len(found)} heredocs under {TF_ROOT}; the shape changed "
            "and this file is no longer reading the provisioners it claims to",
        )

    def test_bash_is_new_enough_to_see_an_unterminated_heredoc(self):
        """bash 3.2 (macOS) reports nothing, which would pass everything silently."""
        if self.major < MIN_BASH_MAJOR:
            self.skipTest(
                f"bash {self.major}.x does not warn about an unterminated heredoc, so "
                f"this suite cannot detect one here. CI runs bash {MIN_BASH_MAJOR}+ "
                "on Linux and does enforce it."
            )

    def test_the_check_catches_an_indented_terminator(self):
        """The guard, guarded: prove bash reports the defect before trusting a pass."""
        if self.major < MIN_BASH_MAJOR:
            self.skipTest(f"bash {self.major}.x cannot detect this")
        broken = "cat <<'INNER'\nbody\n INNER\necho after\n"
        result = subprocess.run(
            [self.bash, "-n", "/dev/stdin"], input=broken, capture_output=True, text=True
        )
        self.assertIn(
            "here-document",
            result.stderr,
            "bash did not report an indented heredoc terminator, so a pass below "
            "would mean nothing",
        )

    def test_every_heredoc_parses(self):
        if self.major < MIN_BASH_MAJOR:
            self.skipTest(f"bash {self.major}.x cannot detect an unterminated heredoc")
        for path, index, script in rendered_scripts():
            with self.subTest(stack=str(path), block=index):
                result = subprocess.run(
                    [self.bash, "-n", "/dev/stdin"],
                    input=script,
                    capture_output=True,
                    text=True,
                )
                # stderr, not just the exit code: an unterminated heredoc is a
                # warning and exits 0, which is the whole trap.
                self.assertEqual(
                    (result.returncode, result.stderr.strip()),
                    (0, ""),
                    f"{path} provisioner {index} does not parse as bash once the "
                    f"<<- dedent is applied:\n{result.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
