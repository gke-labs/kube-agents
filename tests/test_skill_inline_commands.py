"""Tests that the inline code in the shipped skills teaches commands a worker can run.

    python3 -m unittest tests/test_skill_inline_commands.py

A kanban worker runs as `hermes chat -q`, where the command scanner refuses a
command whose program is a shell variable (`$G add`) and the refusal is final.
The image build's deploy/docker/check_skill_commands.py runs the fenced shell
blocks through that scanner but not inline code, most of which is JSON, report
templates or fragments the scanner cannot rate. This reads the inline code of
the same three skill trees for the variable forms the skills used to teach: a
variable as the program, a script run by a path that starts with one, or the
path of git or `submit_suggestion.py` put in a variable.
"""

import re
import unittest
from pathlib import Path

from markdown_it import MarkdownIt

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_TREES = ("agents/platform/skills", "agents/cluster/skills", "a2a/persona/platform/skills")

# A variable run as the program at the start of a span, after a shell join or
# inside `$(`, and after a shell keyword or a wrapper that runs the next word
# as a program (`if $G diff`, `xargs $G add`). A `!` may sit in that run but
# takes no options.
PREFIX = (
    r"(^|`|&&?|;|\||\$\()\s*"
    r"((if|then|else|elif|do|while|until|time|xargs|exec|env|nohup|command)\s+(-\S+\s+)*|!\s+)*"
)
# A variable followed by `=`, `==`, `!=`, `]` or a comparison option such as
# `-gt` is a test's operand (`[[ -n "$A" && ! "$B" == x ]]`), not a program.
NOT_A_TEST_OPERAND = r"(?![=!]?=|\]|-(eq|ne|gt|ge|lt|le|nt|ot|ef)\b)\S"
# The patterns agentplugins/gke-stockout-investigator/tests/test_skill_commands.py
# applies to that plugin's skill.
VARIABLE_PROGRAM_RE = re.compile(
    PREFIX + r"\"?\$\{?[A-Za-z_]\w*\}?\"?\s+" + NOT_A_TEST_OPERAND, re.MULTILINE
)
# An assignment whose value is git, by name or by any path, or the suggestion
# script by any path: the setup line of the variable form. A command
# substitution (`SHA=$(git rev-parse HEAD)`) runs git rather than naming it.
# bench/tasks/vcs-review-feedback-read-back/task.yaml carries the same pattern.
PROGRAM_ASSIGNMENT_RE = re.compile(
    r"(^|[\s;&|(`])[A-Za-z_]\w*=(?!\"?(\$\(|`))\"?(\S*/)?(git|submit_suggestion\.py)(?![\w.-])"
)
# A script run by a path that starts with a variable, which the skills taught
# for their helper scripts (`"$HERMES_HOME"/skills/.../resolver.py poll`). The
# plugin's test carries it too.
VARIABLE_PATH_PROGRAM_RE = re.compile(
    PREFIX + r"\"?\$\{?[A-Za-z_]\w*\}?\"?/\S*\s+\S", re.MULTILINE
)
# A caution names the refused form so a reader knows what not to write.
REFUSED_EXAMPLE_RE = re.compile(r"whose program is a variable \(`[^`]*`\)")


def inline_code(text):
    for token in MarkdownIt("commonmark").parse(text):
        if token.type == "inline":
            # The parser gives a line only for the paragraph, so count the line
            # breaks in its source up to the span's opening backticks.
            end = 0
            for child in token.children:
                if child.type == "code_inline":
                    start = token.content.index(child.markup, end)
                    end = token.content.index(child.markup, start + len(child.markup))
                    end += len(child.markup)
                    yield token.map[0] + 1 + token.content.count("\n", 0, start), child.content


def refused(span):
    return any(
        pattern.search(span)
        for pattern in (VARIABLE_PROGRAM_RE, PROGRAM_ASSIGNMENT_RE, VARIABLE_PATH_PROGRAM_RE)
    )


class SkillInlineCommandsTest(unittest.TestCase):
    def test_each_tree_holds_a_skill(self):
        for tree in SKILL_TREES:
            with self.subTest(tree):
                self.assertTrue(
                    any((REPO_ROOT / tree).rglob("SKILL.md")),
                    f"{tree} holds no SKILL.md, so its inline code would pass unread",
                )

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

    def test_a_variable_program_is_caught(self):
        for span in (
            "$G add <path>",
            '$G add config/manifest.yaml && $G commit -m "feat: x"',
            "export G=/opt/vcs/libexec/git",
            "G=git",
            "export G=git",
            'G="git"',
            "G=/opt/vcs/bin/git",
            '"$S" prepare --repo <owner>/<repo>',
            '"$HERMES_HOME"/skills/github-issue-resolver/scripts/resolver.py transition',
            'cd "$WS" && "$HERMES_HOME"/skills/github-issue-resolver/scripts/resolver.py poll',
            "if ! $G diff --quiet; then $G commit -m x; fi",
            "test -f x && ! $G diff --quiet",
            "! time $G add <path>",
            "$FIND -newer <ref>",
            "$G -C <dir> status",
            "find . -name '*.yaml' | xargs $G add",
            "SHA=$($G rev-parse HEAD)",
            "SHA=`$G rev-parse HEAD`",
            'cd "$WS" & $G add <path>',
            'cd "$WS"; $G add <path>',
            "find . -print0 | xargs -0 $G add",
            "${G} add <path>",
            'S="$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py',
        ):
            with self.subTest(span):
                self.assertTrue(refused(span))

    def test_a_command_that_runs_no_variable_is_not_caught(self):
        for span in (
            "/opt/vcs/libexec/git add <path>",
            "$HERMES_HOME/skills",
            '"$HERMES_HOME"/skills/pr-conversation/scripts/pr_conversation.py',
            'python3 "$HERMES_HOME"/skills/github-issue-resolver/scripts/resolver.py transition',
            "SHA=$(/opt/vcs/libexec/git rev-parse HEAD)",
            "SHA=$(git rev-parse HEAD)",
            "SHA=`git rev-parse HEAD`",
            "GIT_DIR=.git git status",
            "TOOL=git-lfs",
            "REPO=acme/infra.git",
            'for f in $FILES; do echo "$f"; done',
        ):
            with self.subTest(span):
                self.assertFalse(refused(span))

    def test_a_test_operand_is_not_caught(self):
        for span in (
            '[[ -n "$A" && ! "$N" -gt 0 ]]',
            '[[ -n "$A" && "$B" == x ]]',
            '[[ -n "$A" || "$B" != x ]]',
            '[[ -n "$A" && "$B" = x ]]',
            '[[ -n "$A" && ! "$B" ]]',
            '[[ -n "$A" && ! -f "$X" ]]',
            'test -n "$A" && ! -d "$WS" -o "$X" y',
        ):
            with self.subTest(span):
                self.assertFalse(refused(span))

    def test_a_span_is_reported_at_its_own_line(self):
        text = (
            "Intro.\n\nA paragraph that wraps\nonto `$G add` and `a\nspan`, then\n`$S prepare`.\n"
        )
        self.assertEqual(
            [(4, "$G add"), (4, "a span"), (6, "$S prepare")], list(inline_code(text))
        )


if __name__ == "__main__":
    unittest.main()
