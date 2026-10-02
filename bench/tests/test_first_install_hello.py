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
def test_asking_for_runbooks_is_a_stacked_ask_and_no_invitation(case, ask):
    reply = _example(case).replace(INVITATION, ask)
    failing = _failing(case, reply)
    assert "no-stacked-asks" in failing
    assert "invites-runbooks" in failing


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "invitation",
    [
        " Your team's runbooks are welcome here anytime.",
        " Share your team's run books here anytime.",
        " Share your team's runbooks in this chat anytime.",
        " You can share your team's runbooks with me here anytime.",
        " Drop any runbooks your team keeps here anytime.",
    ],
)
def test_a_reworded_invitation_fails_the_invitation_check(case, invitation):
    # The check is the prompts' sentence, so a paraphrase reds the case.
    assert "invites-runbooks" in _failing(case, _example(case).replace(INVITATION, invitation))


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "invitation",
    [
        " (Share your team's runbooks here anytime.)",
        ' "Share your team\'s runbooks here anytime."',
        " “Share your team's runbooks here anytime.”",
        " Share your team's runbooks here anytime…",
        " Share your team’s runbooks here anytime.",
        " Share  your team's\trunbooks here anytime.",
        " Share your team's runbooks here any time.",
    ],
)
def test_a_closing_bracket_quote_ellipsis_or_spacing_still_ends_the_invitation(case, invitation):
    assert _failing(case, _example(case).replace(INVITATION, invitation)) == []


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
        " Any run-books I should read?",
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


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "joined",
    [
        " Share your team's runbooks here anytime, and is there anything you want me to look at first?",
        " Share your team's runbooks here anytime - is there anything you want me to look at first?",
    ],
)
def test_the_invitation_joined_to_the_closing_question_fails_the_invitation_check(case, joined):
    example = _example(case)
    reply = example[: example.index(INVITATION)] + joined
    assert "invites-runbooks" in _failing(case, reply)


@pytest.mark.parametrize(
    "invitation",
    [
        " Share your team's runbooks here anytime.",
        " Share your team’s runbooks here anytime.",
        " Share your team's runbooks here any time.",
        " Share your team's runbooks here anytime, and I'll be in touch.",
        " Share your team's runbooks here anytime - I'll be in touch.",
    ],
)
def test_the_invitation_alone_does_not_say_where_results_go(invitation):
    reply = (
        "Hi there, I'm kube-agents 👋 I'm taking a first look at your GKE fleet."
        " I'm only reading, so nothing in your clusters changes."
        " Fixes come as pull requests for your team to review."
        + invitation
        + " Is there anything you want me to look at first?"
    )
    # A comma or dash after "anytime" also fails invites-runbooks; the results
    # checks must not see the invitation's "share" and "here" either way.
    results = ["says-results-will-be-posted", "says-results-come-to-this-chat"]
    assert [name for name in _failing("first-install-hello-running", reply) if name in results] == results


@pytest.mark.parametrize(
    "reading",
    [
        (
            " I'm only reading, so nothing in your clusters changes, and I'll post what I find here"
            " when it's done, and share your team's runbooks here anytime."
        ),
        (
            " I'm only reading, so nothing in your clusters changes; I'll post what I find here"
            " when it's done; share your team's runbooks here anytime."
        ),
    ],
)
def test_a_results_clause_in_the_invitations_sentence_still_counts(reading):
    reply = (
        "Hi there, I'm kube-agents 👋 I'm taking a first look at your GKE fleet."
        + reading
        + " Fixes come as pull requests for your team to review."
        " Is there anything you want me to look at first?"
    )
    assert _failing("first-install-hello-running", reply) == []


@pytest.mark.parametrize(
    "results",
    [
        " I'll post what I find here\nwhen it's done.",
        " I'll post what I find\nhere when it's done.",
        " I'll post what I find in this\nchat when it's done.",
    ],
)
def test_a_line_break_inside_the_results_sentence_still_says_where_results_go(results):
    # The forbidden patterns run on the reply line by line, so the break stays
    # a "\n" where main's collapsed reply had a space.
    reply = (
        "Hi there, I'm kube-agents 👋 I'm taking a first look at your GKE fleet."
        " I'm only reading, so nothing in your clusters changes."
        + results
        + " Fixes come as pull requests for your team to review."
        + INVITATION
        + " Is there anything you want me to look at first?"
    )
    assert "says-results-come-to-this-chat" not in _failing("first-install-hello-running", reply)


@pytest.mark.parametrize(
    "opener",
    [
        " Here's the plan: I'm taking a first look at your GKE fleet.",
        " I'm taking a first look at your GKE fleet, and here's what I found so far: nothing yet.",
        " I'm taking a first look at your GKE fleet. Anything you want added here?",
    ],
)
def test_here_without_a_results_sentence_does_not_say_where_results_go(opener):
    # main's list: "here's" and "here?" are not a place the results appear.
    reply = (
        "Hi there, I'm kube-agents 👋"
        + opener
        + " I'm only reading, so nothing changes, and I'll send a summary when it's done."
        " Fixes come as pull requests for your team to review."
        + INVITATION
        + " Is there anything you want me to look at first?"
    )
    assert "says-results-come-to-this-chat" in _failing("first-install-hello-running", reply)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "promise",
    [
        " I'll follow your team's runbooks when I find something.",
        " I'll apply the runbooks you share.",
        " I'll use your runbooks for every fix.",
        " Once you share them, I'll be applying your runbooks.",
        " I applied the runbooks you shared.",
    ],
)
def test_promising_to_follow_runbooks_fails_the_promise_safeguard(case, promise):
    example = _example(case)
    reply = example.replace(INVITATION, INVITATION + promise)
    assert "no-runbook-promise" in _failing(case, reply)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "aside",
    [
        " Useful context like runbooks helps.",
        " The users of your runbooks are welcome too.",
        " Teams often used to keep runbooks in docs.",
    ],
)
def test_a_runbook_aside_that_promises_nothing_passes_the_promise_safeguard(case, aside):
    example = _example(case)
    reply = example.replace(INVITATION, INVITATION + aside)
    assert "no-runbook-promise" not in _failing(case, reply)
