"""Tests that the stockout skill teaches commands an unattended worker can run.

    python3 -m unittest discover -s agentplugins/gke-stockout-investigator/tests -v

A worker runs as `hermes chat -q`, where the command scanner refuses a command
whose program is a shell variable (`$G add`) and the refusal is final. The
platform image's skill check does not read this skill, which ships in the
plugin's own image, and its `git` steps sit in inline code, which that check
skips. tests/test_skill_inline_commands.py also reads this skill's inline code.
"""

import re
import unittest
from pathlib import Path

from markdown_it import MarkdownIt

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
# An assignment whose value is git, by name or by any path, or the suggestion
# script by any path: the setup line of the variable form. A command
# substitution (`SHA=$(git rev-parse HEAD)`) runs git rather than naming it.
# bench/tasks/vcs-review-feedback-read-back/task.yaml carries the same pattern.
PROGRAM_ASSIGNMENT_RE = re.compile(
    r"(^|[\s;&|(`])[A-Za-z_]\w*=(?!\"?(\$\(|`))\"?(\S*/)?(git|submit_suggestion\.py)(?![\w.-])"
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
            "G=git",
            "export G=git",
            'G="git"',
            "G=/opt/vcs/bin/git",
            'S="$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py',
            '"$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py prepare \\',
        ):
            with self.subTest(command):
                self.assertTrue(any(pattern.search(command) for pattern in PATTERNS))

    def test_a_command_that_runs_no_variable_is_not_caught(self):
        for command in (
            "./skills/submit-suggestion/scripts/submit_suggestion.py prepare \\",
            'python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py prepare \\',
            'V="$HERMES_HOME"/skills/version-control/scripts/vcs.py',
            'python3 "$V" proposal list --repo <owner>/<repo>',
            "SHA=$(/opt/vcs/libexec/git rev-parse HEAD)",
            "SHA=$(git rev-parse HEAD)",
            "SHA=`git rev-parse HEAD`",
            "GIT_DIR=.git git status",
            "TOOL=git-lfs",
            "REPO=acme/infra.git",
        ):
            with self.subTest(command):
                self.assertFalse(any(pattern.search(command) for pattern in PATTERNS))

    def test_a_test_operand_is_not_caught(self):
        for command in (
            '[[ -n "$A" && ! "$N" -gt 0 ]]',
            '[[ -n "$A" && "$B" == x ]]',
            '[[ -n "$A" || "$B" != x ]]',
            '[[ -n "$A" && "$B" = x ]]',
            '[[ -n "$A" && ! "$B" ]]',
            '[[ -n "$A" && ! -f "$X" ]]',
            'test -n "$A" && ! -d "$WS" -o "$X" y',
        ):
            with self.subTest(command):
                self.assertFalse(any(pattern.search(command) for pattern in PATTERNS))

    def test_remediation_pr_terminology_and_audit_gates_preserved(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn(
            'Use `"capacity constraint"` or `"scale-up failure"` in the Pull Request title, description, YAML comments, and chat notifications — do NOT call the diagnosis a `"stockout"`.',
            text,
        )
        self.assertIn("- **Checks Performed**:", text)
        self.assertIn("platform-agent/remediate-stockout-<workload_name>", text)

    def _section_text(self, text: str, level: str, prefix: str) -> str:
        """Return raw markdown lines from a heading matching ``level`` and ``prefix`` to the next sibling heading."""
        tokens = MarkdownIt("commonmark").parse(text)
        lines = text.splitlines(keepends=True)
        level_num = int(level.lstrip("h"))
        start_line = None
        end_line = len(lines)
        for idx, token in enumerate(tokens):
            if token.type != "heading_open":
                continue
            token_level = int(token.tag.lstrip("h"))
            heading_text = tokens[idx + 1].content if idx + 1 < len(tokens) else ""
            line = token.map[0] if token.map else 0
            if start_line is None:
                if token_level == level_num and heading_text.startswith(prefix):
                    start_line = line
            elif token_level <= level_num:
                end_line = line
                break
        if start_line is None:
            self.fail(f"Heading {level} {prefix!r} not found in {SKILL_MD}")
        return "".join(lines[start_line:end_line])

    def test_chat_only_diagnostic_mode_contract(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertIn("MODE=chat-only", text)
        self.assertIn("Chat-Only Diagnostic Mode", text)

        step2a = self._section_text(
            text, "h4", "A. Check Operating Mode & Existing Pull Requests"
        )
        step2b = self._section_text(
            text, "h4", "B. Determine if the Alert is a Real Issue or a False Signal"
        )
        self.assertIn("gitops_preflight.py --list", step2a)
        self.assertIn("/etc/gitops/managed_repos", step2a)
        self.assertIn("context_repos", step2a)
        self.assertNotIn("/etc/gitops/gitops_repo", step2a)
        self.assertNotIn("[ -s /etc/gitops/", step2a)
        self.assertIn("<host>/<path>", step2a)
        self.assertIn("NO_REGISTERED_REPO", step2a)
        self.assertNotIn("platform-agent-gitops-state", step2a)
        self.assertNotIn("GITOPS_STATE_CONFIGMAP", step2a)
        self.assertIn("MODE=chat-only", step2a)
        self.assertIn("strictly read-only", step2a.lower())
        self.assertIn("proceed directly to **Step 2 §B**", step2a)
        self.assertIn("interactive requester explicitly asked to open a PR", step2a)
        self.assertIn('kanban_block(kind="needs_input")', step2a)
        self.assertIn("false signal", step2b)
        self.assertIn("fleet_scope()", step2b)
        self.assertIn("unread", step2b)
        self.assertIn("gcloud projects list", step2b)
        self.assertIn("excluded_clusters", step2b)
        self.assertIn("kubectl config get-contexts -o name", step2b)
        self.assertIn("gcloud container clusters list --project=", step2b)
        self.assertIn('CLUSTER_CONTEXT="<resolved_context>"', step2b)
        self.assertNotIn("| grep -q", step2b)
        self.assertIn('kanban_block(kind="capability")', step2b)
        self.assertIn("--context=\"$CLUSTER_CONTEXT\"", step2b)
        self.assertIn("Do **NOT** call `kanban_block`", step2b)
        self.assertIn("was not found across verified clusters", step2b)
        self.assertIn("omit `(<cluster_name>)` if unresolved", step2b)

        step3_chat = self._section_text(text, "h4", "Option B: Chat-Only Diagnostic Mode")
        for required_cmd in (
            'kubectl --context="$CLUSTER_CONTEXT" get replicaset <replicaset_name> -n <namespace> --ignore-not-found',
            'kubectl --context="$CLUSTER_CONTEXT" get deployment,statefulset,daemonset,job,replicaset -n <namespace> -o yaml',
            'kubectl --context="$CLUSTER_CONTEXT" get computeclasses -o yaml',
            'kubectl --context="$CLUSTER_CONTEXT" get storageclass -o yaml',
            'kubectl --context="$CLUSTER_CONTEXT" get events -n <namespace>',
            'gcloud container node-pools list --cluster="<cluster_name>" --location="<location>" --project="<project_id>"',
        ):
            self.assertIn(required_cmd, step3_chat)
        self.assertIn("Do **NOT** call `submit_suggestion.py`", step3_chat)
        self.assertIn("do **NOT** call `vcs.py`", step3_chat)

        step4 = self._section_text(text, "h3", "4. Diagnose Capacity")
        self.assertIn("MODE=chat-only", step4)
        self.assertIn("In `MODE=gitops`", step4)
        self.assertIn('kubectl --context="$CLUSTER_CONTEXT" get configmap cluster-autoscaler-status', step4)
        self.assertIn('gcloud compute regions describe "<region>" --project="<project_id>"', step4)
        self.assertIn('--region="<region>"', step4)
        self.assertIn("kanban_complete", step4)

        step6_chat = self._section_text(text, "h3", "6-Chat. Chat-Only Diagnostic Report")
        self.assertIn("skip **Steps 6 and 7** completely", step6_chat)
        self.assertIn("send_notification", step6_chat)
        self.assertIn("kanban_complete", step6_chat)
        self.assertIn("Avoid duplicate chat posts", step6_chat)
        self.assertIn("no GitOps repository is configured, so no pull request was opened", step6_chat)
        self.assertIn('"capacity constraint"` or `"scale-up failure"`', step6_chat)
        self.assertIn("## What's wrong", step6_chat)
        self.assertIn("## Why", step6_chat)
        self.assertIn("## What to do", step6_chat)
        self.assertIn(
            "https://console.cloud.google.com/kubernetes/workload/overview?project=<project_id>",
            step6_chat,
        )
        self.assertNotIn("<workload_console_url>", step6_chat)
        self.assertNotIn("<cluster_console_url>", step6_chat)
        self.assertNotIn("Checks Performed:", step6_chat)
        self.assertNotRegex(step6_chat, r"\bRule [A-H]\b")


if __name__ == "__main__":
    unittest.main()
