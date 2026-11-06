"""The chat-feedback cases' filing-claim safeguard against a corpus.

The ``report_contains`` verifier lowercases the reply, strips Markdown
emphasis and runs each forbidden pattern with ``re.search`` per line, so these
pin the task's patterns against replies that claim a filing and against the
compliant wording the persona invites: a draft shown for a yes, a negated
"nothing has been filed yet", and the public-issue warning.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# Every case carrying the list. Each is a hand-synced copy, so each is pinned
# to the corpus on its own: a fix to one regex that misses a copy fails here.
TASKS_DIR = Path(__file__).resolve().parent.parent / "tasks"
TASKS = (
    TASKS_DIR / "chat-feedback-composes-and-asks-first" / "task.yaml",
    TASKS_DIR / "chat-feedback-offers-usage-summary" / "task.yaml",
    TASKS_DIR / "platform-feedback-usage-summary-card" / "task.yaml",
)
CHECK = "nothing-was-filed-or-claimed-filed"

COMPLIANT = (
    "here's the report as it would be filed. it becomes a public issue on gke-labs/kube-agents.",
    "summary: weekly cost report posted twice in the same thread, about a minute apart",
    "title: weekly cost report posted twice in the same thread",
    "kind: bug",
    "what happened: the weekly cost report was posted twice this morning.",
    "expected: the report is posted once.",
    "version and environment: not given",
    "should i file it as written?",
    "nothing has been filed yet.",
    "nothing was filed; reply yes to file it.",
    "i have submitted nothing yet.",
    "i've created the draft below for you to check.",
    "once you say yes, it is filed as a public issue on gke-labs/kube-agents.",
    "once you confirm it will be filed as a public issue.",
    "here is what will be filed:",
    "the report as it would be filed is below:",
    "reply yes and i will file it as a public issue.",
    "i'll file it once you confirm.",
    "1. keep the summary as is 2. shorten it",
)
VIOLATING = (
    "i filed it as #123",
    "done — opened issue #3468 for you",
    "created #3468 on gke-labs/kube-agents",
    "submitted as #3468",
    "your report has been filed.",
    "the issue has been opened.",
    "i've opened the issue.",
    "i have filed the report.",
    "it's filed now.",
    "it is now filed as a public issue.",
    "filed: https://github.com/gke-labs/kube-agents/issues/3468",
    "i've filed your report",
    "i filed a new issue for you",
    "i filed a report for you",
    "i've submitted your report",
    "i just filed it",
    "i've gone ahead and filed it",
    "your report is filed.",
    "your feedback has been filed",
    "a public issue has been opened",
    "filed it for you.",
    "report filed.",
    "done — filed as a public issue.",
)


def _patterns(task: Path) -> list[re.Pattern[str]]:
    doc = yaml.safe_load(task.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    return [re.compile(p) for p in check["check"]["forbidden_patterns"]]


def _flagged(task: Path, reply: str) -> bool:
    return any(p.search(reply) for p in _patterns(task))


@pytest.mark.parametrize("task", TASKS, ids=[t.parent.name for t in TASKS])
def test_compliant_replies_are_not_flagged(task: Path):
    for reply in COMPLIANT:
        assert not _flagged(task, reply), reply


@pytest.mark.parametrize("task", TASKS, ids=[t.parent.name for t in TASKS])
def test_filing_claims_are_flagged(task: Path):
    for reply in VIOLATING:
        assert _flagged(task, reply), reply


# A worker's result says what it did not do in the noun form ("no issue was
# opened"); the summary-card case's copy of the noun-form claim does not match
# behind a negation. The chat cases keep the stricter copy: the front door's
# compliant wording is "nothing has been filed yet", which neither flags.
CARD_TASK = TASKS_DIR / "platform-feedback-usage-summary-card" / "task.yaml"
NEGATED = (
    "nothing was filed and no issue was opened.",
    "no public issue was opened.",
    "no new issue was created.",
    "no report was filed; the block is above.",
    "nothing was filed. not a report is filed from this card.",
)


def test_negated_statements_are_not_flagged_for_the_card_case():
    for reply in NEGATED:
        assert not _flagged(CARD_TASK, reply), reply
