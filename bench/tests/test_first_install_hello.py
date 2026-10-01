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
@pytest.mark.parametrize(
    "close",
    [" \u2014", " -", " --", ";", ":", "\u2026", " \U0001f64c", " \u2b50", ", and", ", then"],
)
def test_the_invitation_is_a_statement_however_it_ends(case, close):
    reply = _example(case).replace(INVITATION, INVITATION.removesuffix(".") + close)
    assert _failing(case, reply) == []


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
        " Any runbooks I should know about \U0001f642?",
        " Do you have run books I should know about?",
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


@pytest.mark.parametrize(
    "invitation",
    [
        INVITATION,
        " Share your team's runbooks here whenever you like.",
        " Share your team's runbooks in this chat anytime.",
        " Share the runbooks here once you have them.",
        " Share your runbooks here when it suits you.",
        " Drop your runbooks here after the scan.",
        " Runbooks are welcome, post them here anytime.",
    ],
)
def test_the_invitation_does_not_stand_in_for_where_results_appear(invitation):
    case = "first-install-hello-running"
    example = _example(case)
    results = " and I'll post what I find here when it's done"
    assert results in example
    failing = _failing(case, example.replace(results, "").replace(INVITATION, invitation))
    assert "says-results-will-be-posted" in failing
    assert "says-results-come-to-this-chat" in failing


def test_the_invitation_does_not_stand_in_for_the_summary_being_here():
    case = "first-install-hello-done"
    example = _example(case)
    summary = ", and the summary is in this chat"
    assert summary in example
    reply = example.replace(summary, "").replace(
        INVITATION, " Share your team's runbooks in this chat anytime."
    )
    assert "says-the-results-are-in-this-chat" in _failing(case, reply)


@pytest.mark.parametrize(
    "results",
    [
        "I'll share results here when it's done",
        "I'll post my findings here as soon as it's done",
        "I'll post my findings here once the scan finishes",
        "I'll post the findings here",
        "I'll post a summary here when done",
        "I'll share it here when it's done",
        "I'll share them here as soon as it's done",
        "I'll post the summary in this chat",
        "I'll post results in this chat once it's done",
        "I'll post the results to this chat when it's done",
        "I'll post an update here when done",
        "I'll share what I find with you here",
        "I'll post an update here once it's finished",
        "I'll let you know here when it's ready",
        "I'll post the picture in this chat once complete",
        "I'll post the report here once I'm done",
    ],
)
def test_other_ways_of_saying_results_land_here_pass(results):
    case = "first-install-hello-running"
    example = _example(case)
    reply = example.replace("I'll post what I find here when it's done", results)
    assert _failing(case, reply) == []


@pytest.mark.parametrize(
    "summary",
    [
        "the summary is already in this chat",
        "the summary\u2019s in this chat",
        "you'll find the summary in this chat",
        "the summary landed in this chat",
        "the summary is now in this chat",
        "the summary is waiting in this chat",
        "the summary is already here in this chat",
    ],
)
def test_other_ways_of_saying_the_summary_is_here_pass(summary):
    case = "first-install-hello-done"
    reply = _example(case).replace("the summary is in this chat", summary)
    assert _failing(case, reply) == []
