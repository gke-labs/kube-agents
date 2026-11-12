"""The summary-card case tells the attached block from the hand-paste one.

``platform-feedback-usage-summary-card`` grades the card result for the lines
only ``feedback_summary.render`` prints and forbids the rating form, which
``file_feedback.py summary`` cuts from an attached block. The verifier runs
each pattern with one ``re.search`` over the whole line-preserving report, so
a forbid anchored with a bare ``^`` matched only at the report's first
character and the form passed it on every run. These fixtures run the whole
objective through ``check_passes`` on the block as ``render`` lays it out,
built from the script's own ``FEEDBACK_TEMPLATE`` so a reworded form fails
here rather than slipping past the forbid: form off passes, form on fails,
and a filled-in rating line fails too.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import check_passes

SCRIPTS = Path(__file__).resolve().parents[2] / "agents" / "platform" / "scripts"
CASE = "platform-feedback-usage-summary-card"
CHECK = "the-result-carries-the-audited-block"
# The block as the sandbox render prints it: the data lines the objective
# requires, inside the fences the card result carries, with a line of the
# worker's own before and after.
BLOCK = (
    "Here is the block:\n"
    "```\n"
    "kube-agents usage & feedback summary (2026-11-06)\n"
    "environment:\n"
    "  version: 27725510f0d38bdb352ac7f09b945961828ba231\n"
    "  mode: today\n"
    "usage:\n"
    "  clustersRegistered: 4\n"
    "  lastActive: 2026-11-06T01:35:39Z\n"
    "performance:\n"
    "  tokens: unavailable\n"
    "  spend: unavailable\n"
    "tasks (14 days): unavailable\n"
)
FENCE = "```\n"
TAIL = "Nothing was filed.\n"
FILLED_IN_RATING = "feedback:\n  Rating: 👍\n"


@pytest.fixture(scope="module")
def form() -> str:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import feedback_summary
    finally:
        sys.path.remove(str(SCRIPTS))
    return "".join(line + "\n" for line in feedback_summary.FEEDBACK_TEMPLATE)


def test_the_block_without_the_form_passes():
    assert check_passes(CASE, CHECK, BLOCK + FENCE + TAIL)


def test_the_block_with_the_form_fails(form: str):
    assert "rating" in form.lower(), form
    assert not check_passes(CASE, CHECK, BLOCK + form + FENCE + TAIL)


def test_a_filled_in_rating_line_fails():
    assert not check_passes(CASE, CHECK, BLOCK + FILLED_IN_RATING + FENCE + TAIL)
