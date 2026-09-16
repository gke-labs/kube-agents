"""Offline tests for the pure CUJ interaction helpers.

The live CUJ journeys stay manual, but ``substantive_output`` is a pure
function over one dict, and its judgment — what counts as a delegation
acknowledgment versus an answer — decides what every answer-scored criterion
sees. A regression here would misgrade live runs in ways indistinguishable
from agent behavior, so it gets the millisecond test the live suite cannot
give it.
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

BENCH_ROOT = Path(__file__).resolve().parents[1]
if str(BENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCH_ROOT))

import pytest

from cuj.utils.interaction import delivered_answer, substantive_output  # noqa: E402

REPORT = (
    "## Executive Summary\n\n"
    "Quota is separate from live capacity. The regional limit is 16."
)


@pytest.fixture
def ack() -> str:
    """The hand-off Kage is instructed to send, read from its own SOUL.md.

    Hand-written prose drifts from the instruction it stands in for: an
    earlier version of this fixture had no emphasis markers or backticks, so
    it passed against a matcher that could not read a real reply. Read inside
    a fixture rather than at import so a reworded template fails these tests
    and nothing else in the session.
    """

    soul = (Path(__file__).resolve().parents[2] / "agents/chat/SOUL.md").read_text(
        encoding="utf-8"
    )
    block = re.search(r"```\n(\s*> [^\n]*Delegated to the .*?)```", soul, re.S)
    assert block, "agents/chat/SOUL.md no longer shows the delegation template"
    return (
        textwrap.dedent(block.group(1))
        .strip()
        .replace("<agent-name>", "platform")
        .replace("<task_id>", "t_cbb05c69")
    )


def test_delegation_acknowledgment_alone_scores_as_no_answer(ack):
    assert substantive_output({"output": ack}) == ""


def test_an_acknowledgment_behind_leading_blank_lines_still_scores_as_silence(ack):
    # A leading blank paragraph is not an answer that ends the skipping.
    assert substantive_output({"output": f"\n\n{ack}"}) == ""


def test_report_following_the_acknowledgment_is_kept_verbatim(ack):
    assert substantive_output({"output": f"{ack}\n\n{REPORT}"}) == REPORT


def test_each_leading_acknowledgment_paragraph_is_skipped():
    output = (
        "Delegated to the platform agent\n\n"
        "I have started this as task t_0d0778b9.\n\n"
        "The answer will post into this thread as soon as it's ready.\n\n"
        + REPORT
    )
    assert substantive_output({"output": output}) == REPORT


def test_answers_without_an_acknowledgment_pass_through_unchanged():
    assert substantive_output({"output": REPORT}) == REPORT


def test_only_leading_paragraphs_are_treated_as_acknowledgment(ack):
    # A report that *mentions* its task id mid-answer is still the answer.
    tail = f"{REPORT}\n\nEvidence was recorded on task t_cbb05c69 for audit."
    assert substantive_output({"output": f"{ack}\n\n{tail}"}) == tail


def test_an_answer_sharing_the_acknowledgment_paragraph_survives():
    # A coordinator that answers in the same breath as its hand-off must not
    # be scored as silence.
    output = (
        "Delegated to the platform agent. Quota is separate from live "
        "capacity, and the regional limit is 16."
    )
    assert substantive_output({"output": output}) == (
        "Quota is separate from live capacity, and the regional limit is 16."
    )


def test_a_report_opening_with_its_task_id_is_not_boilerplate():
    # "task t_..." alone is a reference, not a hand-off; only hand-off
    # phrasing around it counts.
    report = "Design report for task t_cbb05c69: the A100 quota is 16."
    assert substantive_output({"output": report}) == report


def test_missing_and_empty_outputs_degrade_to_empty():
    assert substantive_output({}) == ""
    assert substantive_output({"output": None}) == ""


def test_answer_sentences_that_resemble_a_hand_off_are_kept():
    # Only the template's own wording is boilerplate; an answer that says
    # where its results post, or names a task and a start window, stays.
    for output in (
        "Results for the A100 design will post below. Quota is separate.",
        "Assigned under task t_0d0778b9: start 14:00 UTC in us-central1-a. "
        "Quota is separate.",
    ):
        assert substantive_output({"output": output}) == output


def test_delivered_answer_folds_in_the_delegated_task_results(ack):
    # The user reads the coordinator's hand-off and then the specialist's
    # result, posted into the same thread by the gateway.
    interaction = {
        "output": ack,
        "tasks": [
            {"assignee": "platform", "status": "done", "result": REPORT},
            {"assignee": "platform", "status": "done", "result": "   "},
            "not-a-task",
        ],
    }
    assert delivered_answer(interaction) == REPORT
    assert delivered_answer({"output": REPORT}) == REPORT
    assert delivered_answer({"output": ack}) == ""
