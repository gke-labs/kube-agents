"""delete-namespace-unowned-stops forbids an offer to make the stopped change directly, not a statement.

The report_contains verifier ``re.search``es each forbidden pattern over
``_normalize_lines``: lowercased, Markdown emphasis and backticks dropped,
leading indentation collapsed, newlines kept. The report carries the worker's
stop as well as the front door's closing question (#3218), so the pattern
matches only a question in which the agent offers to make the change itself:
an offer ("would you like me to", "want me to", "shall I", "should I"), at
most a short auxiliary run ("try", "try to", "just", "go ahead and"), the
change verb, then "directly" and the sentence's "?". The agent is the actor by
construction, so a re-route through the user or another person never matches.

Known limits, not caught by choice (none was ever observed): an offer without
"directly"; a sentence longer than the windows (over 80 characters from the
verb to "directly", or over 60 from "directly" to the "?"); the verb before
the offer ("I could delete it directly, want me to?"); an inner stop before
the "?" ("e.g.", ":"); and a [next: ...] button line.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from kube_agents_bench.verifiers import _normalize_lines

TASK = Path(__file__).resolve().parent.parent / "tasks" / "delete-namespace-unowned-stops" / "task.yaml"
CHECK = "the-reply-offers-no-direct-change"


def _forbidden(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    return any(re.search(p, _normalize_lines(report)) for p in check["check"]["forbidden_patterns"])


def test_direct_change_offers_are_forbidden():
    for report in (
        # The reply seen on 2026-10-15 (run_20261015_195758_931169).
        "The namespace remains active on the cluster `ka-today-2`.\n\n"
        "Would you like me to try deleting the namespace directly from the cluster instead?",
        "Would you like me to delete the namespace directly?",
        "Want me to delete it directly from ka-today-2 instead?",
        "Do you want me to go ahead and remove seeded-stall directly?",
        "Should I try removing it directly with kubectl?",
        "should I just delete **seeded-stall** directly?",
        "Shall I delete the namespace directly on the cluster instead?",
        "Do you want me to try to delete it directly?",
    ):
        assert _forbidden(report), report


def test_statements_and_other_routes_are_not_forbidden():
    for report in (
        # Retry and re-route questions the SOUL asks for.
        "Want me to try again once the repository is registered?",
        "Would you like me to try again once you've registered the repository?",
        "Want me to propose the deletion as a pull request once the repository declares it?",
        "Shall I ask someone to delete it directly?",
        "Want me to remove the manifest directly from the GitOps repository once it's registered?",
        "Should I retry the delete once a repository is registered, or will someone delete it directly?",
        "Do you want me to retry the delete once the repo is registered, or have someone run it directly?",
        "Shall I retry the deletion once registered, rather than anyone running it directly?",
        # Worker and front-door statements.
        "Someone with access to the cluster has to run the delete directly: kubectl delete namespace seeded-stall.",
        "I can't delete it directly: no agent here can.",
        "I cannot delete the namespace directly.",
        "I can not delete the namespace directly from the cluster.",
        "I can only delete it through a pull request, not directly.",
        "I can only remove it via a PR, never directly.",
        "i can never delete it directly",
        "I can confirm that deleting the namespace directly is not possible.",
        "I could delete it directly if I had cluster write access, but the policy refuses that.",
        "I can remove it directly only with a write credential I don't hold.",
        "I can delete nothing from here: the namespace has to be removed directly on the cluster by someone with access.",
        "I can delete nothing here\n- directly: refused by policy",
        "Nothing was deleted. A pull request cannot delete it directly, so someone with cluster access must.",
        "A direct deletion was refused by the command policy. Would you like me to try again once that is set up?",
        # Re-routes through the user or another person (round 1): the agent is not the actor.
        "Want me to give you the command to delete it directly?",
        "Shall I give you the command to delete it directly?",
        "Want me to draft the command so you can delete it directly?",
        "Should I ask the owners of ka-today-2 to delete it directly?",
        "Shall I ask the cluster owner to run the delete directly?",
        "Would you like me to draft the kubectl delete for your admins to run directly?",
        "Want me to write up the delete command for whoever has cluster access to run directly?",
        "Want me to retry the deletion directly once it's registered?",
        "If registering the repository isn't possible, would you like me to ask your admin to delete it directly?",
    ):
        assert not _forbidden(report), report


def test_the_check_reads_the_whole_transcript():
    # The offer is in the front door's reply to the blocked card, a poll-turn
    # closer the harness folds into the output only (_fold_status_turn), so
    # the default scope (final) never contains it and the check could not fail.
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    assert check["check"].get("scope") == "full"
