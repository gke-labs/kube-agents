"""The chat-feedback cases' heading check tells the heading from the offer.

Since docs/designs/chat-feedback-command.md §8 every ``/feedback <text>`` draft
also offers, mid-sentence, to ``add the usage summary``. A heading pattern that
matched the bare word ``summary`` anywhere was therefore satisfied by the offer
on every run, and a reply with no heading passed the objective. Both cases now
grade the heading as a labelled line on the folded surface, where
``fold_decoration`` turns ``- **Summary:** ...`` into ``summary: ...`` at the
start of a line. These fixtures run the whole objective through
``check_passes`` with every other required pattern present, so a reply passes
or fails on the heading alone.
"""

from __future__ import annotations

import pytest
from conftest import check_passes

CASES = (
    ("chat-feedback-composes-and-asks-first", "the-reply-shows-the-submission-and-asks"),
    ("chat-feedback-offers-usage-summary", "the-reply-shows-the-submission-and-offers-the-summary"),
)
# Every other required pattern of both objectives, so the heading is the only
# clause under test: the kind, the sections, the user's words from each
# prompt, the warning, the offer in the persona's words, and the ask.
BODY = (
    "Kind: Bug\n"
    "What happened: the weekly cost report was posted twice in the same thread; "
    "the drift audit update arrives twice a day.\n"
    "This becomes a public issue on gke-labs/kube-agents.\n"
    "Say add the usage summary to attach the install's versions and counters. Shall I file it?\n"
)
# A heading, however the agent lays the line out: bold, a bullet, a table
# row, a plain label, a dash, the label alone above its line, the form's
# other word for it, and the label with a qualifier: the form's own name for
# the field (`One-line summary`, feedback_broker.QUESTION_LABELS["title"]),
# `Issue title`, and a parenthesis after the label.
HEADINGS = (
    "**Summary:** Weekly cost report posted twice in one thread\n",
    "- **Summary**: Weekly cost report posted twice in one thread\n",
    "Summary: Weekly cost report posted twice in one thread\n",
    "| Summary | Weekly cost report posted twice in one thread |\n",
    "Summary - Weekly cost report posted twice in one thread\n",
    "**Summary**\nWeekly cost report posted twice in one thread\n",
    "Title: Weekly cost report posted twice in one thread\n",
    "1. **Title:** Weekly cost report posted twice in one thread\n",
    "**One-line summary:** Weekly cost report posted twice in one thread\n",
    "- **One-line summary**: Weekly cost report posted twice in one thread\n",
    "| One-line summary | Weekly cost report posted twice in one thread |\n",
    "Issue title: Weekly cost report posted twice in one thread\n",
    "**Summary (issue title):** Weekly cost report posted twice in one thread\n",
)
# The word "summary" or "title" without a heading: the offer as the persona
# words it (already in BODY), hyphenated, wrapped across a line, paraphrased
# with the word bare, inside a sentence that opens no line, and a label's
# words opening a sentence that is not a label.
NO_HEADING = (
    "",
    "I can also add the usage-summary (no names in it).\n",
    "I can also add the usage\nsummary before filing, if you like.\n",
    "I can attach a summary of the install's usage if you like.\n",
    "Here is the submission - summary and all - for your yes.\n",
    "Summary and title are the same thing on the form.\n",
    "One-line summary and kind come first on the form.\n",
    "Issue title is taken from the first line.\n",
)


@pytest.mark.parametrize(("case", "check"), CASES, ids=[c for c, _ in CASES])
def test_a_reply_with_a_heading_passes(case: str, check: str):
    for heading in HEADINGS:
        for extra in NO_HEADING:
            report = heading + BODY + extra
            assert check_passes(case, check, report), report


@pytest.mark.parametrize(("case", "check"), CASES, ids=[c for c, _ in CASES])
def test_the_offer_alone_is_not_a_heading(case: str, check: str):
    for extra in NO_HEADING:
        report = BODY + extra
        assert not check_passes(case, check, report), report
