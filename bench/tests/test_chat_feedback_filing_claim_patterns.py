"""The chat-feedback cases' filing-claim safeguard against a corpus.

The ``report_contains`` verifier lowercases the reply, strips Markdown
emphasis and runs each forbidden pattern with one ``re.search`` over the whole
line-preserving report, so these pin the task's patterns against replies that
claim a filing and against the compliant wording the persona invites: a draft
shown for a yes, a negated "nothing has been filed yet", and the public-issue
warning. Each line of the corpus is graded through ``check_passes`` inside a
reply with a line before it and one after, on the surface the verifier uses:
a pattern anchored with a bare ``^`` or ``$`` matches only a reply that is
that one line, and this frame is what shows it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import check_passes

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
    "a github issue was opened for you.",
    "a bug report was filed.",
    "the feedback report has been submitted.",
    "a new github bug report was filed.",
    "either the block was posted or a new issue was opened.",
    "the block was posted and the bug report was filed.",
    "nothing else was done. the issue was opened.",
    "the block carries no rating form, and the bug report was filed.",
    "no identifiers leaked, so a new issue was opened.",
    "with no errors, the issue was opened.",
    "either no block was needed or a new issue was opened.",
    "no block was needed — a new issue was opened.",
)


# The lines around each corpus entry: compliant on their own, so a frame
# flags nothing and a bare-anchored pattern cannot match the entry as the
# whole report.
FRAME_BEFORE = "Here is where things stand.\n"
FRAME_AFTER = "\nAnything else you want changed?\n"


def _flagged(task: Path, reply: str) -> bool:
    return not check_passes(task.parent.name, CHECK, FRAME_BEFORE + reply + FRAME_AFTER)


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
# in a clause that carries a negation, or one a conditional or temporal word
# opens ("when a report is filed"). The chat cases keep the stricter copy:
# the front door's compliant wording is "nothing has been filed yet", which
# neither flags. The clause is read from its start to the noun, so any word
# between the negator and the noun ("no github issue", "nor an issue", "no
# separate issue", "no outbound issue") is inside its scope, and a clause
# start ends it: a stop, a comma, a dash, "and", "but", "so", or "or" before
# a determiner. The last eight violating entries above, a positive claim
# after "or", after "and", in the sentence after a negated one, and after a
# negated clause joined by a comma, "so", "or" or a dash, pin that the scope
# reaches no further than the clause; "no report or issue was filed" below
# pins that a bare "or" does not start one.
CARD_TASK = TASKS_DIR / "platform-feedback-usage-summary-card" / "task.yaml"
NEGATED = (
    "nothing was filed and no issue was opened.",
    "no public issue was opened.",
    "no new issue was created.",
    "no report was filed; the block is above.",
    "no github issue was opened.",
    "no public github issue was opened.",
    "neither a report nor an issue was opened.",
    "no bug report was filed.",
    "no feedback report was filed; the block is above.",
    "no report or issue was filed.",
    "not a report was filed.",
    "no separate issue was filed.",
    "no outbound issue was filed; the block is above.",
    "no additional report was submitted.",
    "nothing was filed and no external issue was opened.",
    "without filing anything: the issue was not opened, the block is above.",
    "nothing was filed, no issue was opened, the block is above.",
    "none of the feedback was submitted.",
    "the issue was never opened.",
    "this block is carried as the environment when a report is filed.",
    "paste this under environment before the issue is opened.",
)


def test_negated_statements_are_not_flagged_for_the_card_case():
    for reply in NEGATED:
        assert not _flagged(CARD_TASK, reply), reply
