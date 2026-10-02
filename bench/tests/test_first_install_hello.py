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
INVITATION = " Share your team's runbooks here."
POINT4 = " Fixes come as pull requests for your team to review."
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
        " Share your team's runbooks here...?",
        " Share your team's runbooks here!?",
        " Share your team's runbooks here…?",
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
        " Your team's runbooks are welcome here.",
        " Share your team's run books here.",
        " Share your team's runbooks in this chat.",
        " You can share your team's runbooks with me here.",
        " Drop any runbooks your team keeps here.",
    ],
)
def test_a_reworded_invitation_fails_the_invitation_check(case, invitation):
    # The check is the prompts' sentence, so a paraphrase reds the case.
    assert "invites-runbooks" in _failing(case, _example(case).replace(INVITATION, invitation))


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "invitation",
    [
        " (Share your team's runbooks here.)",
        ' "Share your team\'s runbooks here."',
        " “Share your team's runbooks here.”",
        " Share your team's runbooks here…",
        " Share your team’s runbooks here.",
        " Share  your team's\trunbooks here.",
    ],
)
def test_a_closing_bracket_quote_ellipsis_or_spacing_still_ends_the_invitation(case, invitation):
    assert _failing(case, _example(case).replace(INVITATION, invitation)) == []


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "invitation",
    [
        " You can share your team's runbooks here.",
        " Please share your team's runbooks here.",
        " Feel free to share your team's runbooks here.",
        " And share your team's runbooks here.",
        " Also, share your team's runbooks here.",
        " You can also share your team's runbooks here.",
        " One more thing: share your team's runbooks here.",
        " In the meantime, share your team's runbooks here.",
        " Please do share your team's runbooks here.",
        " 📚 Share your team's runbooks here.",
        " - Share your team's runbooks here.",
        " Please feel free to share your team's runbooks here.",
    ],
)
def test_a_short_lead_in_without_a_negation_still_counts_as_the_invitation(case, invitation):
    assert "invites-runbooks" not in _failing(case, _example(case).replace(INVITATION, invitation))


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("join", [": share", " — share", " - share", " – share"])
def test_a_colon_or_dash_join_still_opens_the_invitation(case, join):
    example = _example(case)
    joined = POINT4.removesuffix(".") + join + INVITATION.removeprefix(" Share")
    assert "invites-runbooks" not in _failing(case, example.replace(POINT4 + INVITATION, joined))


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "invitation",
    [
        " There's no need to share your team's runbooks here.",
        " You don't have to share your team's runbooks here.",
        " I can't read them if you share your team's runbooks here.",
        " Please don't share your team's runbooks here.",
        " Never share your team's runbooks here.",
    ],
)
def test_a_negated_or_conditional_invitation_fails_the_invitation_check(case, invitation):
    # A negation in the words before the invitation can turn it into its opposite.
    assert "invites-runbooks" in _failing(case, _example(case).replace(INVITATION, invitation))


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "invitation", [" Share your team's runbooks here anytime.", " Share your team's runbooks here any time."]
)
def test_an_invitation_promising_any_time_fails_the_invitation_check(case, invitation):
    # The greeting must not promise a runbook path the agent has not been given.
    assert "invites-runbooks" in _failing(case, _example(case).replace(INVITATION, invitation))


# A known limit the cases' comment names: a second runbook ask with no "?" passes no-stacked-asks.
@pytest.mark.parametrize("case", CASES)
def test_an_imperative_runbook_ask_passes_no_stacked_asks(case):
    reply = _example(case).replace(INVITATION, INVITATION + " Send me your on-call runbooks too.")
    assert "no-stacked-asks" not in _failing(case, reply)


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
        " Share your team's runbooks here, and is there anything you want me to look at first?",
        " Share your team's runbooks here - is there anything you want me to look at first?",
    ],
)
def test_the_invitation_joined_to_the_closing_question_fails_the_invitation_check(case, joined):
    example = _example(case)
    reply = example[: example.index(INVITATION)] + joined
    assert "invites-runbooks" in _failing(case, reply)


@pytest.mark.parametrize(
    "invitation",
    [
        " Share your team's runbooks here.",
        " Share your team’s runbooks here.",
        " Share your team's runbooks here, and I'll be in touch.",
        " Share your team's runbooks here - I'll be in touch.",
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
    # A comma or dash after "here" also fails invites-runbooks; the results
    # checks must not see the invitation's "share" and "here" either way.
    results = ["says-results-will-be-posted", "says-results-come-to-this-chat"]
    assert [name for name in _failing("first-install-hello-running", reply) if name in results] == results


@pytest.mark.parametrize(
    "reading",
    [
        (
            " I'm only reading, so nothing in your clusters changes, and I'll post what I find here"
            " when it's done, and share your team's runbooks here."
        ),
        (
            " I'm only reading, so nothing in your clusters changes; I'll post what I find here"
            " when it's done; share your team's runbooks here."
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
        " Happy to follow your runbooks.",
        " I'll use your runbooks.",
        " Many teams keep runbooks, and I'll follow your runbooks.",
        " Many thanks for installing me, happy to follow your runbooks.",
        " With many alerts firing, I plan to apply your runbooks.",
        " Most of all, I want to use your runbooks.",
        " I've got many ideas, and I use runbooks to act on them.",
        " Like many teams, we'll follow your runbooks.",
        " Most clusters look healthy, and happy to follow your runbooks.",
        " Like most teams, happy to follow your runbooks.",
        " I'll help you follow your runbooks.",
        " I'll make sure you get answers that follow your runbooks.",
        " I can walk you through applying your runbooks.",
        " I'll check with you, then follow your runbooks.",
        " Happy to help you follow your runbooks.",
        " Glad to help you apply your runbooks.",
        " I'm happy to help you follow your runbooks.",
        " I'd be glad to walk you through applying your runbooks.",
        " I'll follow all of your team's runbooks.",
        " I'll apply every one of your runbooks.",
        " I'll follow your team's incident and on-call runbooks.",
        " I'll make use of your runbooks.",
        " If your team agrees, fixes will follow your runbooks.",
        " Your team keeps runbooks, and fixes will follow your runbooks.",
    ],
)
def test_promising_to_follow_runbooks_fails_the_promise_safeguard(case, promise):
    example = _example(case)
    reply = example.replace(INVITATION, INVITATION + promise)
    assert "no-runbook-promise" in _failing(case, reply)


# A clearing word inside a prepositional phrase ("for your team", "in your clusters") is not the clause's subject,
# so a promise appended to the example's own sentences still fails.
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "sentence",
    [
        " Fixes come as pull requests for your team to review that follow your runbooks.",
        " Fixes come as pull requests for your team to review and will follow your runbooks.",
        " Pull requests for your team to review follow your runbooks.",
        " Nothing in your clusters changes, and every fix will apply your runbooks.",
        " Any fix for the team will use your runbooks.",
    ],
)
def test_a_promise_after_a_prepositional_team_fails_the_promise_safeguard(case, sentence):
    example = _example(case)
    assert POINT4 in example
    assert "no-runbook-promise" in _failing(case, example.replace(POINT4, sentence))


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "aside",
    [
        " Useful context like runbooks helps.",
        " The users of your runbooks are welcome too.",
        " Teams often used to keep runbooks in docs.",
        " Many teams use runbooks for on-call.",
        " Most people follow their own runbooks.",
        " They apply runbooks during incidents.",
        " If you use runbooks, share them here.",
        " You probably use runbooks already.",
        " Your team uses runbooks.",
        " If your team already follows runbooks, share them here.",
        " SREs use runbooks.",
        " Engineers on your team follow runbooks.",
        " The use of runbooks is up to your team.",
        " I make no use of runbooks on my own.",
        " Use this chat to share your team's runbooks.",
        " I'll use this thread to collect runbooks.",
        " Many on your team use runbooks.",
        " Some of you follow runbooks.",
        " Most of your team follows runbooks.",
        " I'm only reading, so if you follow runbooks, share them here.",
    ],
)
def test_a_runbook_aside_that_promises_nothing_passes_the_promise_safeguard(case, aside):
    example = _example(case)
    reply = example.replace(INVITATION, INVITATION + aside)
    assert "no-runbook-promise" not in _failing(case, reply)


# The known limits the case's comment names: these promise, and pass.
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "promise",
    [
        " I'll stick to your runbooks.",
        " I'll do my best to help your team follow its runbooks.",
        " Let me help you use your runbooks.",
        " Let me help you apply your runbooks.",
        " If you have runbooks, I'll follow them.",
        " I'll help you and your team follow your runbooks.",
        " I could help you follow your runbooks.",
        " Here to help you follow your runbooks.",
        " Happy, as always, to help you follow your runbooks.",
        " I'll follow the long list of incident and on-call runbooks.",
        " Fixes sent to your team follow your runbooks.",
        " You'll get fixes that follow your runbooks.",
        " Your team will get pull requests that follow your runbooks.",
        " I'll" + " x" * 150 + " follow your runbooks.",
        " Also," + " x" * 150 + " follow your runbooks.",
    ],
)
def test_the_promise_safeguards_known_limits_pass_it(case, promise):
    example = _example(case)
    reply = example.replace(INVITATION, INVITATION + promise)
    assert "no-runbook-promise" not in _failing(case, reply)


# Known costs of failing closed, one or more examples per kind the case's comment names: after a first-person opener
# only other people clear the clause, so a "you" that is the subject of an embedded clause fails it too, and so does
# a negation; "happy/glad/ready/keen to" restarts the clause whoever precedes it, so the reader as its subject
# clears nothing; a subject outside the clearing list clears nothing; a gerund is the verb; and a sentence-initial
# imperative to the reader reads as the promise.
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    "aside",
    [
        " I'll see whether you use runbooks.",
        " I'll share what you use runbooks for.",
        " I'll learn how you follow runbooks today.",
        " I can tell you use runbooks.",
        " Happy to hear how you use runbooks.",
        " Glad to learn whether you follow runbooks.",
        " Use this chat for runbooks.",
        " I can read them if you use runbooks.",
        " I'm curious whether you use runbooks.",
        " I'm read-only, so I won't follow runbooks on my own.",
        " When you're ready to use runbooks here, share them.",
        " Anyone who follows runbooks will like this.",
        " Following runbooks is common.",
        " That's a good use of runbooks.",
        " Changes from your team follow runbooks.",
    ],
)
def test_the_promise_safeguards_known_costs_fail_it(case, aside):
    example = _example(case)
    reply = example.replace(INVITATION, INVITATION + aside)
    assert "no-runbook-promise" in _failing(case, reply)


# The length limit is the 300-character bound, not the filler: under it, the same promise fails, after a
# first-person opener and after a sentence start alike.
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("opener", [" I'll", " Also,"])
def test_the_promise_safeguard_holds_a_promise_inside_its_bound(case, opener):
    example = _example(case)
    reply = example.replace(INVITATION, INVITATION + opener + " x" * 140 + " follow your runbooks.")
    assert "no-runbook-promise" in _failing(case, reply)
