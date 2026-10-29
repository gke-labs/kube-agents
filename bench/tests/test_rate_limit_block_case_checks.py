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
        "I couldn't check checkout-gateway on seeded-a. The AI model's usage limit stopped it, "
        "and it will not run again on its own." + OFFER,
        "I couldn't check checkout-gateway: the model's usage limit ran out, and it won't run again on its own."
        + OFFER,
        "I couldn't check it. It will not be automatically retried." + OFFER,
        "I couldn't check it. It won't be automatically retried." + OFFER,
        "I couldn't check it. Nothing will automatically retry it." + OFFER,
        "I couldn't check it. Nothing will retry it, even once the limit resets." + OFFER,
        "I couldn’t check it. It won’t run again on its own. Want me to try again?",
        "I couldn't check it. It will not run again on its own; it will run again only when you ask me to."
        + OFFER,
        "I couldn't check it, and the dispatcher doesn't retry a blocked check, so it won't run again on its own."
        + OFFER,
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
            [
                "the-reply-says-nothing-will-run-it-again",
                "the-reply-promises-no-automatic-retry",
                "the-reply-ends-on-an-offer-to-try-again",
            ],
            id="the reply #3766 reported",
        ),
        pytest.param(
            "I couldn't check checkout-gateway on seeded-a. The provider hit an API rate limit." + OFFER,
            ["the-reply-says-nothing-will-run-it-again"],
            id="leaves the retry unsaid",
        ),
        pytest.param(
            "I couldn't check it, and it won't run again on its own. It'll be retried once the limit resets."
            + OFFER,
            ["the-reply-promises-no-automatic-retry"],
            id="promises a retry beside the honest line",
        ),
        pytest.param(
            "I couldn't check it, and it won't run again on its own. The system will retry it later." + OFFER,
            ["the-reply-promises-no-automatic-retry"],
            id="the system will retry",
        ),
        pytest.param(
            "I couldn't check it. It will not run again on its own.",
            ["the-reply-ends-on-an-offer-to-try-again"],
            id="no offer",
        ),
        pytest.param(
            "I couldn't check it. The dispatcher retries it automatically once the limit resets, "
            "nothing for you to run. Want me to try again?",
            ["the-reply-says-nothing-will-run-it-again", "the-reply-promises-no-automatic-retry"],
            id="present-tense promise",
        ),
        pytest.param(
            "It won't run again until the limit resets, then it'll pick up where it left off. Want me to try again?",
            ["the-reply-promises-no-automatic-retry"],
            id="it'll pick up",
        ),
        pytest.param(
            "The check will be picked up again automatically, so you won't need to rerun it. Want me to try again?",
            ["the-reply-says-nothing-will-run-it-again", "the-reply-promises-no-automatic-retry"],
            id="picked up again",
        ),
        pytest.param(
            "It won't run again on its own. No further action is needed; it retries on its own. Want me to try again?",
            ["the-reply-promises-no-automatic-retry"],
            id="no further action",
        ),
    ],
)
def test_a_promise_or_a_missing_offer_fails(reply: str, failing: list[str]) -> None:
    assert _failing(reply) == failing
