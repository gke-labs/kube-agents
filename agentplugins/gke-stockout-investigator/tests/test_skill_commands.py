"""Tests that the stockout skill teaches commands an unattended worker can run.

    python3 -m unittest discover -s agentplugins/gke-stockout-investigator/tests -v

A worker runs as `hermes chat -q`, where the command scanner refuses a command
whose program is a shell variable (`$G add`) and the refusal is final. The
platform image's skill check does not read this skill, which ships in the
plugin's own image, and its `/opt/vcs/libexec/git` steps sit in inline code,
which that check skips.
"""

import re
import unittest
from pathlib import Path

SKILL_MD = (
    Path(__file__).resolve().parents[1]
    / "files/skills/gke-stockout-investigator/SKILL.md"
)

# A variable run as the program: at the start of a line or a code span, after
# a shell join, as in `cd <workspace> && $G add`, or inside `$(`, and after a
# shell keyword or a wrapper that runs the next word as a program (`xargs $G add`).
PREFIX = (
    r"(^|`|&&?|;|\||\$\()\s*"
    r"((if|then|else|elif|do|while|until|time|xargs|exec|env|nohup|command|!)\s+(-\S+\s+)*)*"
)
VARIABLE_PROGRAM_RE = re.compile(PREFIX + r"\"?\$\{?[A-Za-z_]\w*\}?\"?\s+\S", re.MULTILINE)
# The path put in a variable for later use, which is the variable form's setup.
# Capturing git's output, `SHA=$(/opt/vcs/libexec/git ...)`, is not that.
PROGRAM_ASSIGNMENT_RE = re.compile(
    r"\b[A-Za-z_]\w*=(?!\"?(\$\(|`)/opt/vcs/libexec/git[\s)`])\S*"
    r"(/opt/vcs/libexec/git|submit_suggestion\.py)\b"
)
# A script run by a path that starts with a variable, which the scanner refuses
# the same way (`"$HERMES_HOME"/skills/.../submit_suggestion.py prepare`).
VARIABLE_PATH_PROGRAM_RE = re.compile(
    PREFIX + r"\"?\$\{?[A-Za-z_]\w*\}?\"?/\S*\s+\S", re.MULTILINE
)
PATTERNS = (VARIABLE_PROGRAM_RE, PROGRAM_ASSIGNMENT_RE, VARIABLE_PATH_PROGRAM_RE)
# The caution names the refused form so a reader knows what not to write.
REFUSED_EXAMPLE = "whose program is a variable (`$G add`)"


class SkillCommandsTest(unittest.TestCase):
    def test_no_command_runs_a_variable_as_its_program(self):
        text = SKILL_MD.read_text(encoding="utf-8").replace(REFUSED_EXAMPLE, "")
        for pattern in PATTERNS:
            matches = [m.group(0) for m in pattern.finditer(text)]
            self.assertEqual([], matches, pattern.pattern)

    def test_the_refused_forms_are_caught(self):
        for command in (
            "$G add <path>",
            "cd <workspace> && $G add <path>",
            "cd <workspace> & $G add <path>",
            "SHA=$($G rev-parse HEAD)",
            "find . -name '*.yaml' | xargs $G add",
            "export G=/opt/vcs/libexec/git",
            '"$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py prepare \\',
        ):
            with self.subTest(command):
                self.assertTrue(any(pattern.search(command) for pattern in PATTERNS))

    def test_a_literal_program_is_not_caught(self):
        for command in (
            "./skills/submit-suggestion/scripts/submit_suggestion.py prepare \\",
            'V="$HERMES_HOME"/skills/version-control/scripts/vcs.py',
            'python3 "$V" proposal list --repo <owner>/<repo>',
            "SHA=$(/opt/vcs/libexec/git rev-parse HEAD)",
        ):
            with self.subTest(command):
                self.assertFalse(any(pattern.search(command) for pattern in PATTERNS))


if __name__ == "__main__":
    unittest.main()
