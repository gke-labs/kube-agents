"""Tests that the inline code in the shipped skills teaches commands a worker can run.

    python3 -m unittest tests/test_skill_inline_commands.py

A kanban worker runs as `hermes chat -q`, where the command scanner refuses a
command whose program is a shell variable (`$G add`) and the refusal is final.
The image build's deploy/docker/check_skill_commands.py runs the fenced shell
blocks through that scanner but not inline code, most of which is JSON, report
templates or fragments the scanner cannot rate. This reads the inline code of
the same three skill trees for the variable forms the skills used to teach: a
variable as the program, or a script run by a path that starts with one.
"""

import re
import unittest
from pathlib import Path

from markdown_it import MarkdownIt

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_TREES = ("agents/platform/skills", "agents/cluster/skills", "a2a/persona/platform/skills")

# A shell keyword or a wrapper that runs the next word as a program
# (`if $G diff`, `xargs $G add`).
PREFIX = (
    r"(^|`|&&|;|\|)\s*"
    r"((if|then|else|elif|do|while|until|time|xargs|exec|env|nohup|command|!)\s+(-\S+\s+)*)*"
)
# The patterns agentplugins/gke-stockout-investigator/tests/test_skill_commands.py
# applies to that plugin's skill.
VARIABLE_PROGRAM_RE = re.compile(PREFIX + r"\"?\$\{?[A-Za-z_]\w*\}?\"?\s+\S", re.MULTILINE)
PROGRAM_ASSIGNMENT_RE = re.compile(
    r"\b[A-Za-z_]\w*=(?!\"?(\$\(|`)/opt/vcs/libexec/git[\s)`])\S*"
    r"(/opt/vcs/libexec/git|submit_suggestion\.py)\b"
)
# A script run by a path that starts with a variable, which the skills taught
# for their helper scripts (`"$HERMES_HOME"/skills/.../resolver.py poll`). The
# plugin's skill never did, so its test has no copy.
VARIABLE_PATH_PROGRAM_RE = re.compile(
    PREFIX + r"\"?\$\{?[A-Za-z_]\w*\}?\"?/\S*\s+\S", re.MULTILINE
)
# A caution names the refused form so a reader knows what not to write.
REFUSED_EXAMPLE_RE = re.compile(r"whose program is a variable \(`[^`]*`\)")


def inline_code(text):
    for token in MarkdownIt("commonmark").parse(text):
        if token.type == "inline":
            for child in token.children:
                if child.type == "code_inline":
                    yield token.map[0] + 1, child.content


def refused(span):
    return any(
        pattern.search(span)
        for pattern in (VARIABLE_PROGRAM_RE, PROGRAM_ASSIGNMENT_RE, VARIABLE_PATH_PROGRAM_RE)
    )


class SkillInlineCommandsTest(unittest.TestCase):
    def test_no_inline_command_runs_a_variable_as_its_program(self):
        found = [
            f"{skill.relative_to(REPO_ROOT)}:{line}: {span}"
            for tree in SKILL_TREES
            for skill in sorted((REPO_ROOT / tree).rglob("SKILL.md"))
            for line, span in inline_code(
                REFUSED_EXAMPLE_RE.sub("", skill.read_text(encoding="utf-8"))
            )
            if refused(span)
        ]
        self.assertEqual([], found)

    def test_the_forms_the_skills_taught_are_caught(self):
        for span in (
            "$G add <path>",
            '$G add config/manifest.yaml && $G commit -m "feat: x"',
            "export G=/opt/vcs/libexec/git",
            '"$S" prepare --repo <owner>/<repo>',
            '"$HERMES_HOME"/skills/github-issue-resolver/scripts/resolver.py transition',
            'cd "$WS" && "$HERMES_HOME"/skills/github-issue-resolver/scripts/resolver.py poll',
            "if ! $G diff --quiet; then $G commit -m x; fi",
            "find . -name '*.yaml' | xargs $G add",
        ):
            with self.subTest(span):
                self.assertTrue(refused(span))

    def test_a_path_or_a_captured_output_is_not_caught(self):
        for span in (
            "/opt/vcs/libexec/git add <path>",
            "$HERMES_HOME/skills",
            '"$HERMES_HOME"/skills/pr-conversation/scripts/pr_conversation.py',
            'python3 "$HERMES_HOME"/skills/github-issue-resolver/scripts/resolver.py transition',
            "SHA=$(/opt/vcs/libexec/git rev-parse HEAD)",
            'for f in $FILES; do echo "$f"; done',
        ):
            with self.subTest(span):
                self.assertFalse(refused(span))


if __name__ == "__main__":
    unittest.main()
