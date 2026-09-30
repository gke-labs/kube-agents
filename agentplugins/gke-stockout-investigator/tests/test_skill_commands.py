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
# A `!` may sit in that run but takes no options.
PREFIX = (
    r"(^|`|&&?|;|\||\$\()\s*"
    r"((if|then|else|elif|do|while|until|time|xargs|exec|env|nohup|command)\s+(-\S+\s+)*|!\s+)*"
)
# A variable followed by `=`, `==`, `!=`, `]` or a comparison option such as
# `-gt` is a test's operand (`[[ -n "$A" && ! "$B" == x ]]`), not a program.
NOT_A_TEST_OPERAND = r"(?![=!]?=|\]|-(eq|ne|gt|ge|lt|le|nt|ot|ef)\b)\S"
VARIABLE_PROGRAM_RE = re.compile(
    PREFIX + r"\"?\$\{?[A-Za-z_]\w*\}?\"?\s+" + NOT_A_TEST_OPERAND, re.MULTILINE
)
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
            "cd <workspace>; $G add <path>",
            "SHA=$($G rev-parse HEAD)",
            "SHA=`$G rev-parse HEAD`",
            "if ! $G diff --quiet; then $G commit -m x; fi",
            "test -f x && ! $G diff --quiet",
            "! time $G add <path>",
            "$FIND -newer <ref>",
            "find . -name '*.yaml' | xargs $G add",
            "find . -print0 | xargs -0 $G add",
            "${G} add <path>",
            '"$S" prepare --repo <owner>/<repo>',
            "export G=/opt/vcs/libexec/git",
            'S="$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py',
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

    def test_a_test_operand_is_not_caught(self):
        for command in (
            '[[ -n "$A" && ! "$N" -gt 0 ]]',
            '[[ -n "$A" && "$B" == x ]]',
            '[[ -n "$A" || "$B" != x ]]',
            '[[ -n "$A" && ! "$B" ]]',
            'test -n "$A" && ! -d "$WS" -o "$X" y',
        ):
            with self.subTest(command):
                self.assertFalse(any(pattern.search(command) for pattern in PATTERNS))


if __name__ == "__main__":
    unittest.main()
