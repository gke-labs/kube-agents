"""The first-install-hello cases' checks, run against the greeting prompts' own examples.

Each onboarding prompt carries one example greeting. The case grading that
variant has to pass it, or the prompt teaches a greeting its eval fails; and the
checks have to tell the examples apart from the near misses below, or they
grade nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

REPO = Path(__file__).resolve().parents[2]
TASKS = REPO / "bench" / "tasks"
ONBOARDING = REPO / "agents" / "chat" / "defaults" / "onboarding"
CASES = {
    "first-install-hello-running": "scan_in_progress.md",
    "first-install-hello-done": "scan_completed.md",
}
# The examples greet a Slack profile name; the API server carries none, so the
# cases require the fallback.
PROFILE_GREETING = "Hi Alex,"
FALLBACK_GREETING = "Hi there,"
INVITATION = " Share your team's runbooks here anytime."
TIMEOUT_SEC = 5.0


@pytest.fixture(autouse=True)
def _clean_stash():
    transcript.clear()
    yield
    transcript.clear()


def _checks(case: str) -> dict[str, ReportContainsVerifier]:
    doc = yaml.safe_load((TASKS / case / "task.yaml").read_text())
    return {
        item["name"]: ReportContainsVerifier(**item["check"])
        for item in doc["verification_spec"]
        if item["check"]["type"] == "report_contains"
    }


def _example(case: str) -> str:
    text = (ONBOARDING / CASES[case]).read_text()
    line = next(x for x in text.splitlines() if x.startswith("> " + PROFILE_GREETING))
    return line.removeprefix("> ").replace(PROFILE_GREETING, FALLBACK_GREETING, 1)


def _failing(case: str, reply: str) -> list[str]:
    transcript.set(reply, [], final_message=reply)
    return [name for name, check in _checks(case).items() if not check.verify(TIMEOUT_SEC).success]


@pytest.mark.parametrize("case", CASES)
def test_the_prompts_example_passes_every_check(case):
    assert _failing(case, _example(case)) == []


@pytest.mark.parametrize("case", CASES)
def test_the_greeting_without_the_invitation_fails_only_on_it(case):
    example = _example(case)
    assert INVITATION in example
    assert _failing(case, example.replace(INVITATION, "")) == ["invites-runbooks"]


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "ask",
    [
        " Do you have runbooks you'd like to share?",
        " Could you share your team's runbooks here?",
        " Any runbooks I should know about?",
        " Do you have runbooks, and where do they live?",
        " Could you share your runbooks - the main ones?",
        " Any runbooks - or conventions - I should know about?",
    ],
)
def test_asking_for_runbooks_is_a_stacked_ask(case, ask):
    reply = _example(case).replace(INVITATION, ask)
    assert "no-stacked-asks" in _failing(case, reply)


@pytest.mark.parametrize("case", CASES)
def test_a_runbook_question_joined_to_the_closing_one_is_a_stacked_ask(case):
    example = _example(case)
    question = example[example.index(INVITATION) + len(INVITATION) :].strip()
    joined = " Could you share your runbooks here, and " + question[0].lower() + question[1:]
    reply = example[: example.index(INVITATION)] + joined
    assert "no-stacked-asks" in _failing(case, reply)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "ask",
    [
        " Do you keep run books I should read first?",
        " Any runbooks, e.g. the on-call ones, I should read first?",
        " Any runbooks, i.e. the on-call ones, I should read first?",
        " Do you keep runbooks in docs/runbooks.md?",
    ],
)
def test_a_runbook_question_after_the_invitation_is_a_stacked_ask(case, ask):
    example = _example(case)
    closing = example[example.index(INVITATION) + len(INVITATION) :]
    reply = example.replace(closing, ask)
    assert "no-stacked-asks" in _failing(case, reply)


def test_the_invitation_alone_does_not_say_where_results_go():
    reply = (
        "Hi there, I'm kube-agents 👋 I'm taking a first look at your GKE fleet."
        " I'm only reading, so nothing in your clusters changes."
        " Fixes come as pull requests for your team to review."
        " Share your team's runbooks here anytime."
        " Is there anything you want me to look at first?"
    )
    assert _failing("first-install-hello-running", reply) == [
        "says-results-will-be-posted",
        "says-results-come-to-this-chat",
    ]
