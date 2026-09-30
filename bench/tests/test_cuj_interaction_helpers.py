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
    block = re.search(
        r"Acknowledge by naming what is being checked.*?```\n(.*?)```", soul, re.DOTALL
    )
    assert block, "agents/chat/SOUL.md no longer shows the delegation template"
    return textwrap.dedent(block.group(1)).strip()


# The receipt the template replaced; an install on an older image still sends it.
OLD_RECEIPT = (
    "> 🔀 Delegated to the **platform** agent\n\n"
    "I've started this as task `t_cbb05c69`. The answer will post into this "
    "thread as soon as it's ready."
)


def test_the_receipt_older_images_send_still_scores_as_no_answer():
    assert substantive_output({"output": OLD_RECEIPT}) == ""
    assert substantive_output({"output": f"{OLD_RECEIPT}\n\n{REPORT}"}) == REPORT


def test_a_capitalised_gerund_sentence_is_an_answer_not_an_ack():
    report = "Checking the logs showed a crash on startup."
    assert substantive_output({"output": report}) == report


def test_a_lowercase_answer_opening_with_an_ing_word_is_kept():
    for report in ("nothing is restarting.", "everything is healthy."):
        assert substantive_output({"output": report}) == report


def test_a_lowercase_answer_opening_with_an_ack_verb_is_kept():
    for report in (
        "looking at the logs, the pod restarted 4 times.",
        "checking the events shows the pod was evicted twice for memory pressure.",
    ):
        assert substantive_output({"output": report}) == report


def test_every_ack_shape_the_template_names_scores_as_no_answer():
    for ack in (
        "checking checkout-gateway.",
        "looking for seeded-z.",
        "reviewing the checkout-gateway rollout.",
        "auditing version skew across the fleet.",
    ):
        assert substantive_output({"output": ack}) == ""


def test_an_ack_that_drifts_from_the_template_still_scores_as_no_answer():
    # The template fixes case and the one line, not the period, the verb,
    # the target's length or whether it is a link; a model drifts on those.
    for ack in (
        "checking checkout-gateway",
        "checking checkout-gateway…",
        "provisioning the staging cluster.",
        "scaling checkout-gateway down to two replicas in prod-a.",
        "reviewing [PR 412](https://github.com/o/r/pull/412).",
        "checking seeded-a and seeded-c.",
        "checking why checkout-gateway is restarting.",
        "checking spot capacity in us-central1.",
        "**checking** `checkout-gateway`.",
        "checking why the rollout failed.",
        "reviewing the failed rollout in prod-a.",
        "checking the seeded fleet.",
        "reviewing failed rollouts in prod-a.",
        "looking for orphaned disks.",
        "checking evicted pods on node-3.",
        "auditing unused node pools.",
        "checking pinned versions across the fleet.",
        "checking the fleet's seed.",
        "checking node-pool-red.",
        "looking at the logs in prod-a.",
        "checking all the nodes in prod-a.",
        "creating a staging cluster named foo.",
        "checking pods labeled app=web.",
        "reviewing the cluster called prod-a.",
    ):
        assert substantive_output({"output": ack}) == "", ack
        assert substantive_output({"output": f"{ack}\n\n{REPORT}"}) == REPORT, ack


def test_a_line_off_the_template_is_kept_even_when_it_is_an_ack():
    # Keeping an ack costs a reviewer one line; stripping an answer costs the
    # answer. A line the template does not describe errs toward the first.
    for line in (
        "Checking checkout-gateway.",
        "Checking spot capacity in us-central1.",
        "checking seeded-a, seeded-b and seeded-c.",
    ):
        assert substantive_output({"output": line}) == line, line


def test_a_short_answer_shaped_like_an_ack_is_kept():
    for report in (
        "looking good.",
        "Looking fine",
        "checking shows nothing wrong.",
        "Checking the logs showed a crash on startup.",
        "Running pods: 12.",
        "Pending pods are stuck on quota.",
        "Missing quota in us-central1.",
        "Everything is healthy.",
        "looking at the logs, the pod restarted 4 times.",
        "checking the rollout, 3 replicas never became ready.",
        "checking checkout-gateway; it restarted 4 times.",
        "**Running pods:** 12.",
        "**Scaling plan:** add two nodes in prod-a.",
        "restarting the pod cleared it.",
        "Restarting the pod fixed it.",
        "Draining the node cleared the pending pods.",
        "provisioning failed.",
        "provisioning failed on quota.",
        "scaling completed.",
        "looking very good.",
        "autoscaling kicked in at 14:02.",
        "looking at the events, nothing stands out.",
        "restarting the pod took 3 minutes.",
        "scaling up brought it back.",
        "restarting won't help.",
        "restarting won\u2019t help.",
        "checking the logs gave nothing.",
        "rolling back will fix it.",
        "staging cluster has 3 nodes.",
        "warning events on checkout-gateway.",
        "running normally.",
        "running smoothly.",
        "Running 3 replicas in prod-a.",
        "provisioning failed again.",
        "scaling completed successfully.",
        "restarting fixed it.",
        "restarting cleared the backlog.",
        "upgrading fixed the skew.",
        "patching resolved them.",
        "upgrading failed silently.",
        "deploying succeeded without errors.",
        "upgrading needs a maintenance window.",
        "restarting requires approval.",
        "draining evicts the pdb-protected pods.",
        "rolling back restores the previous image.",
        "scaling costs about $40 a day.",
        "checking the rollout, replicas never became ready.",
        "looking at the logs, oom kills every minute.",
        "checking the events, pods crashloop on startup.",
        "draining node-3 in prod-a — confirm?",
        "Deleting prod-a?",
        "checking the rollout, it's stuck.",
        "looking at the logs, it's crashlooping.",
        "looking at the logs, four restarts.",
        "checking it's stuck.",
        "upgrading prod-a blocks on quota.",
        "provisioning staging hit quota limits.",
        "deleting prod-a removes all workloads.",
        "provisioning failed when quota ran out.",
        "restarting fixed checkout-gateway.",
        "rolling back fixed checkout-gateway.",
        "checking quota in us-central1 hit limits.",
        "checking pods in prod-a restart constantly.",
    ):
        assert substantive_output({"output": report}) == report, report


def test_a_question_keeps_what_it_asks_about():
    assert (
        substantive_output({"output": "Draining node-3 in prod-a? Confirm."})
        == "Draining node-3 in prod-a? Confirm."
    )
    assert (
        substantive_output({"output": "deleting cluster A. Confirm?"})
        == "deleting cluster A. Confirm?"
    )


def test_known_misreads_are_pinned():
    # A verb outside the lists inside a short run of plain words has the
    # words of "auditing version skew across the fleet." and nothing else to
    # tell it apart. A change that fixes it flips the assertion here.
    for answer in ("scaling staging broke.",):
        assert substantive_output({"output": answer}) == "", answer


def test_a_long_gerund_sentence_is_an_answer():
    report = (
        "restarting the pod cleared the stale mount and the checkout path "
        "recovered within two minutes of the rollout finishing."
    )
    assert substantive_output({"output": report}) == report


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
