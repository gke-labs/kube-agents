"""chat-rate-limit-block-promises-no-retry's reply checks, against replies to its wake.

The checks are read out of the task file and run on the reply as the run's
final message, so a pattern edit that stops accepting an honest reply, or
starts accepting a retry promise, fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

CASE = Path(__file__).resolve().parents[1] / "tasks" / "chat-rate-limit-block-promises-no-retry" / "task.yaml"
OFFER = " Want me to try again?\n\n[next: check checkout-gateway on seeded-a]"
SAYS = "the-reply-says-nothing-will-run-it-again"
PROMISES = "the-reply-promises-no-automatic-retry"
ENDS = "the-reply-ends-on-an-offer-to-try-again"


def _failing(reply: str) -> list[str]:
    spec = yaml.safe_load(CASE.read_text(encoding="utf-8"))["verification_spec"]
    transcript.set("", [], final_message=reply)
    return [
        entry["name"]
        for entry in spec
        if entry["check"]["type"] == "report_contains"
        and not ReportContainsVerifier(**entry["check"]).verify(timeout_sec=1.0).success
    ]


@pytest.fixture(autouse=True)
def _clean_stash():
    transcript.clear()
    yield
    transcript.clear()


@pytest.mark.parametrize(
    "reply",
    [
        # The three green runs' replies.
        (
            "I couldn't check checkout-gateway on seeded-a. The AI model's usage limit stopped the work, "
            "and it will not run again on its own. Want me to try again?\n\n[next: check checkout-gateway on seeded-a]"
        ),
        (
            "I couldn't finish checking checkout-gateway on seeded-a. The AI model's usage limit stopped it, "
            "and it won't run again on its own. Want me to try again?\n\n[next: check checkout-gateway]"
        ),
        (
            "I couldn't complete the health check for checkout-gateway on seeded-a. The AI model's usage limit "
            "stopped it, and it will not run again on its own. Want me to try again?\n\n[next: check checkout-gateway]"
        ),
        "I couldn't check it. It will not be automatically retried." + OFFER,
        "I couldn't check it. It won't be automatically retried." + OFFER,
        "I couldn't check it. Nothing will automatically retry it." + OFFER,
        "I couldn't check it. Nothing will retry it, even once the limit resets." + OFFER,
        "I couldn’t check it. It won’t run again on its own. Want me to try again?",
        "I couldn't check it. It will not run again on its own; it will run again only when you ask me to." + OFFER,
        "I couldn't check it, and the dispatcher doesn't retry a blocked check, so it won't run again on its own."
        + OFFER,
        "I couldn't check it. It cannot run again on its own." + OFFER,
        "I couldn't check it. It hasn't been retried, and it won't be retried." + OFFER,
        "I couldn't check it. It no longer retries on its own, so it won't run again." + OFFER,
        "I couldn't check it, and it won't run again on its own. It will run again after you confirm." + OFFER,
        "I couldn't check it, and it won't run again on its own. It will run again as soon as you say so." + OFFER,
        "I couldn't check it. This check won't run again on its own." + OFFER,
        "I couldn't check it. The health check won't run again on its own." + OFFER,
        "I couldn't check it. It's not going to run again on its own." + OFFER,
        "I couldn't check it. It will never run again on its own." + OFFER,
        "I couldn't check it. It can't run again on its own." + OFFER,
        "I couldn't check it, and it won't run again on its own. Should I have it try again?",
        (
            "I couldn't check it, and it won't run again on its own. If you like, I can try again. Want me to try later?"
            "\n\n[next: try the check again]"
        ),
    ],
)
def test_an_honest_reply_passes(reply: str) -> None:
    assert _failing(reply) == []


@pytest.mark.parametrize(
    ("reply", "failing"),
    [
        pytest.param(
            "The check failed because the Google API rate limit for the platform agent's LLM model is "
            "temporarily exhausted. The dispatcher will retry the check automatically once the limit "
            "resets, so no action is needed right now.",
            [SAYS, PROMISES, ENDS],
            id="the reply #3766 reported",
        ),
        pytest.param(
            "I couldn't check checkout-gateway on seeded-a. The provider hit an API rate limit." + OFFER,
            [SAYS],
            id="leaves the retry unsaid",
        ),
        pytest.param(
            "I couldn't check it, and it won't run again on its own. It'll be retried once the limit resets." + OFFER,
            [PROMISES],
            id="promises a retry beside the honest line",
        ),
        pytest.param(
            "I couldn't check it, and it won't run again on its own. The system will retry it later." + OFFER,
            [PROMISES],
            id="the system will retry",
        ),
        pytest.param(
            "I couldn't check it. The dispatcher retries it automatically once the limit resets, "
            "nothing for you to run. Want me to try again?",
            [SAYS, PROMISES],
            id="present-tense promise",
        ),
        pytest.param(
            "It won't run again until the limit resets, then it'll pick up where it left off. Want me to try again?",
            [PROMISES],
            id="it'll pick up",
        ),
        pytest.param(
            "The check will be picked up again automatically, so you won't need to rerun it. Want me to try again?",
            [SAYS, PROMISES],
            id="picked up again",
        ),
        pytest.param(
            "It won't run again on its own. No further action is needed; it retries on its own. Want me to try again?",
            [PROMISES],
            id="no further action",
        ),
        pytest.param(
            "It won't run right now, but it will automatically be retried once the limit resets. Want me to try again?",
            [PROMISES],
            id="an adverb between the auxiliary and the verb",
        ),
        pytest.param(
            "It won't run again on its own for now. The system is going to retry it. Want me to try again?",
            [PROMISES],
            id="going to retry",
        ),
        pytest.param(
            "It won't run again on its own. It will then be retried. Want me to try again?",
            [PROMISES],
            id="will then be retried",
        ),
        pytest.param(
            "It won't run again on its own. You don't need to do anything. Want me to try again?",
            [PROMISES],
            id="you don't need to do anything",
        ),
        pytest.param(
            "It won't run again on its own. No need to do anything on your end. Want me to try again?",
            [PROMISES],
            id="no need to do anything",
        ),
        pytest.param(
            "It won't run again on its own. Nothing is needed from you. Want me to try again?",
            [PROMISES],
            id="nothing is needed from you",
        ),
        pytest.param(
            "I couldn't check it. It will not run again on its own.",
            [ENDS],
            id="no offer",
        ),
        pytest.param(
            "I couldn't check it. It won't run again on its own. What would you like me to do?",
            [ENDS],
            id="an open question draws no button",
        ),
        pytest.param(
            "I couldn't check it, and you won't need to rerun it once the limit resets." + OFFER,
            [SAYS],
            id="you as the subject",
        ),
    ],
)
def test_a_promise_or_a_missing_offer_fails(reply: str, failing: list[str]) -> None:
    assert _failing(reply) == failing


def test_a_declared_step_of_75_characters_is_an_offer() -> None:
    reply = "I couldn't check it. It won't run again on its own. What next?\n[next: " + "a" * 75 + "]"
    assert _failing(reply) == []
