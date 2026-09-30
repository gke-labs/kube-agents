"""vcs-review-feedback-read-back's by-path check matches the forms it is meant to.

The worker_commands verifier runs ``re.search`` on each one-line command
summary, so these pin the task file's two patterns against summaries in that
shape: the variable forms the skills used to teach, and the literal-path and
quoted-message forms a passing worker writes.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "vcs-review-feedback-read-back"
    / "task.yaml"
)
CHECK = "the-commit-step-called-its-programs-by-path"


def _patterns():
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    return [re.compile(p) for p in check["check"]["forbidden_patterns"]]


def _flagged(command):
    return any(p.search(command) for p in _patterns())


def test_the_variable_forms_are_flagged():
    for command in (
        "$G add EVAL-NOTES.md",
        '"$S" prepare --repo acme/infra',
        'cd "$WS" && $G add x',
        "SHA=$($G rev-parse HEAD)",
        "$G -C /opt/data/scratch/vcs status",
        "export G=/opt/vcs/libexec/git cd /opt/data/scratch/vcs/acme $G add EVAL-NOTES.md",
        'S="$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py',
        "if $G diff --quiet + 2 commands",
        "if ! $G diff --quiet + 2 commands",
        "time $G add f",
        "find . -name '*.yaml' | xargs $G add",
        "find . -print0 | xargs -0 $G add",
        "cd ws if /opt/vcs/libexec/git diff --quiet then $G commit -m x fi",
        "cd ws time $G add f",
    ):
        assert _flagged(command), command


def test_the_literal_and_quoted_forms_are_not_flagged():
    for command in (
        "/opt/vcs/libexec/git add x",
        "SHA=$(/opt/vcs/libexec/git rev-parse HEAD)",
        'python3 "$HERMES_HOME"/skills/submit-suggestion/scripts/submit_suggestion.py prepare',
        'echo "opened PR (${SHA} commit)"',
        'echo "done; ${SHA} pushed"',
        'cd "$WS" grep foo',
        'for f in $FILES; do echo "$f"; done',
        'if /opt/vcs/libexec/git diff --quiet; then echo "then ${SHA} done"; fi',
    ):
        assert not _flagged(command), command
