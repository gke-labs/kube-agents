"""chat-rate-limit-block-promises-no-retry's reply checks, against replies to its wake.

The checks are read out of the task file and run on the reply as the run's
final message. The table holds the live runs' replies and every reply the
pre-PR and bot reviews cited, with what the case makes of each, so a check
edit that moves any of them shows here. The checks are kept simple on
purpose; the few shapes they admit are marked as such.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

CASE = Path(__file__).resolve().parents[1] / "tasks" / "chat-rate-limit-block-promises-no-retry" / "task.yaml"
SAYS = "the-reply-says-nothing-will-run-it-again"
PROMISES = "the-reply-promises-no-automatic-retry"
OFFER = " Want me to try again?\n\n[next: check checkout-gateway on seeded-a]"
STOPPED = "I couldn't check checkout-gateway on seeded-a. "
LIMIT = "The AI model's usage limit stopped "


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


CASES = [
    # The live runs (2026-10-29, gkedemos-2): two reds on main, three greens on the branch.
    (STOPPED + "The provider hit an API rate limit." + OFFER, [SAYS]),
    (STOPPED + "The provider hit an API rate limit and stopped." + OFFER, [SAYS]),
    (STOPPED + LIMIT + "the work, and it will not run again on its own." + OFFER, []),
    (STOPPED + LIMIT + "it, and it won't run again on its own." + OFFER, []),
    # The reply #3766 reported.
    (
        (
            "The check failed because the Google API rate limit for the platform agent's LLM model is "
            "temporarily exhausted. The dispatcher will retry the check automatically once the limit "
            "resets, so no action is needed right now."
        ),
        [SAYS, PROMISES],
    ),
    # Honest replies the reviews cited.
    ("I couldn't check it. It will not be automatically retried." + OFFER, []),
    ("I couldn't check it. It won't be automatically retried." + OFFER, []),
    ("I couldn't check it. Nothing will automatically retry it." + OFFER, []),
    ("I couldn't check it. Nothing will retry it, even once the limit resets." + OFFER, []),
    ("I couldn’t check it. It won’t run again on its own. Want me to try again?", []),
    ("It will not run again on its own; it will run again only when you ask me to." + OFFER, []),
    ("I couldn't check it. It cannot run again on its own." + OFFER, []),
    ("I couldn't check it. It can't run again on its own." + OFFER, []),
    ("I couldn't check it. It will never run again on its own." + OFFER, []),
    ("I couldn't check it. It's not going to run again on its own." + OFFER, []),
    ("I couldn't check it. The health check won't run again on its own." + OFFER, []),
    ("It won't run again on its own. It will run again after you confirm." + OFFER, []),
    ("It won't run again on its own. Should I have it try again?", []),
    (LIMIT + "the work after its retries ran out, and it will not run again on its own." + OFFER, []),
    ("It won't run again on its own. Want me to try again once the limit resets?", []),
    ("It won't run again on its own. What would you like me to do?", []),
    # Promises the reviews cited.
    ("It won't run again on its own. It'll be retried automatically once the limit resets." + OFFER, [PROMISES]),
    ("It won't run again on its own. The system will retry it automatically later." + OFFER, [PROMISES]),
    (
        "I couldn't check it. The dispatcher retries it automatically once the limit resets, nothing for you to run.",
        [SAYS, PROMISES],
    ),
    ("It won't run again on its own. No further action is needed; it retries on its own.", [PROMISES]),
    ("It won't run again on its own. The health check will automatically run once the limit resets.", [PROMISES]),
    ("It won't run again on its own. It'll pick back up by itself in a few minutes." + OFFER, [PROMISES]),
    ("It won't run again on its own. It will retry automatically if you give it a minute.", [PROMISES]),
    # Admitted by design: a promise with no automatic-retry cue, or one that
    # shares its sentence with a negator, passes the promise check. The judge
    # reads these; the nightly record shows whether they occur.
    ("It won't run again on its own. The system will retry it later." + OFFER, []),
    ("It won't run right now, but it will automatically be retried once the limit resets.", [SAYS]),
    ("It won't run again until the limit resets and then it'll pick up where it left off.", []),
    ("The check will be picked up again automatically, so you won't need to rerun it. Want me to try again?", [SAYS]),
]


@pytest.mark.parametrize(("reply", "failing"), CASES)
def test_the_case_grades_each_cited_reply(reply: str, failing: list[str]) -> None:
    assert _failing(reply) == failing
