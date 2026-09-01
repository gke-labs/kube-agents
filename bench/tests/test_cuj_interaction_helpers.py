"""Offline tests for the pure CUJ interaction helpers.

The live CUJ journeys stay manual, but ``substantive_output`` is a pure
function over one dict, and its judgment — what counts as a delegation
acknowledgment versus an answer — decides what every answer-scored criterion
sees. A regression here would misgrade live runs in ways indistinguishable
from agent behavior, so it gets the millisecond test the live suite cannot
give it.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCH_ROOT = Path(__file__).resolve().parents[1]
if str(BENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCH_ROOT))

from cuj.utils.interaction import substantive_output  # noqa: E402

ACK = (
    "Delegated to the platform agent.\n\n"
    "I've started this design process under task t_cbb05c69. The complete "
    "design report will post directly to this thread as soon as it's ready."
)
REPORT = (
    "## Executive Summary\n\n"
    "Quota is separate from live capacity. The regional limit is 16."
)


def test_delegation_acknowledgment_alone_scores_as_no_answer():
    assert substantive_output({"output": ACK}) == ""


def test_report_following_the_acknowledgment_is_kept_verbatim():
    assert substantive_output({"output": f"{ACK}\n\n{REPORT}"}) == REPORT


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


def test_only_leading_paragraphs_are_treated_as_acknowledgment():
    # A report that *mentions* its task id mid-answer is still the answer.
    tail = f"{REPORT}\n\nEvidence was recorded on task t_cbb05c69 for audit."
    assert substantive_output({"output": f"{ACK}\n\n{tail}"}) == tail


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
