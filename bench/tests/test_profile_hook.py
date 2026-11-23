"""Tests for profile_hook: the [bench:profile ...] directive (#5945)."""

from __future__ import annotations

import pytest

from kube_agents_bench import profile_hook


def test_no_directive_is_none() -> None:
    assert profile_hook.parse("what's failing in kube-system") is None


@pytest.mark.parametrize(
    "prompt",
    [
        "[bench:profile name=cluster project=p1 location=l cluster=c] look at it",
        "[bench:profile name=cluster project=p1 location=l cluster=c]\nlook at it",
        "  [bench:profile name=cluster project=p1 location=l cluster=c]   look at it  ",
    ],
    ids=["same line (a folded block)", "next line (a literal block)", "surrounding space"],
)
def test_the_directive_names_the_profile_and_its_parameters_and_strips_itself(prompt: str) -> None:
    ask = profile_hook.parse(prompt)
    assert ask is not None
    assert ask.name == "cluster"
    assert ask.parameters == {"project": "p1", "location": "l", "cluster": "c"}
    assert ask.prompt == "look at it"


def test_a_profile_with_no_parameters_carries_none() -> None:
    ask = profile_hook.parse("[bench:profile name=cluster] hello")
    assert ask is not None and ask.parameters == {}


@pytest.mark.parametrize(
    ("prompt", "why"),
    [
        ("[bench:profile name=cluster look", "no closing"),
        ("[bench:profile project=p1] ask", "name= is required"),
        ("[bench:profile name=cluster.> ] ask", "not a profile's name"),
        ("[bench:profile name=cluster project] ask", "is not key=value"),
        ("[bench:profile name=cluster project=a project=b] ask", "given twice"),
        ("[bench:profile name=cluster 9x=a] ask", "not a parameter's name"),
        ("[bench:profile name=cluster Project=p1] ask", "not a parameter's name"),
        ("[bench:profile name=cluster " + "k" * 64 + "=a] ask", "not a parameter's name"),
        ("[bench:profile name=cluster]   ", "nothing to ask"),
        ("[bench:profile name=My_Prof] ask", "not a profile's name"),
        ("[bench:profile name=cluster] [bench:stop-in-card-wait] ask", "may follow"),
        ("[bench:profile name=cluster]\n[bench:stop-after-first-card] ask", "may follow"),
        ("[bench:profile name=cluster intent=cancel] ask", "intent=cancel is not one"),
        ("[bench:profile name=cluster intent=Confirm] ask", "intent=Confirm is not one"),
        ("[bench:profile name=cluster intent=] ask", "is not key=value"),
        ("[bench:profile name=cluster intent=confirm intent=confirm] ask", "given twice"),
    ],
)
def test_a_malformed_directive_is_refused(prompt: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        profile_hook.parse(prompt)


def test_is_profile_ask_reads_only_an_opening_directive() -> None:
    assert profile_hook.is_profile_ask("  [bench:profile name=cluster] ask")
    assert not profile_hook.is_profile_ask("[bench:stop-after-first-card]\n[bench:profile name=cluster] ask")


def test_intent_confirm_is_the_asks_intent_and_not_a_parameter() -> None:
    ask = profile_hook.parse("[bench:profile name=cluster intent=confirm project=p1] open the pull request")
    assert ask is not None
    assert ask.intent == "confirm"
    assert ask.parameters == {"project": "p1"}
    assert ask.prompt == "open the pull request"


def test_a_directive_without_intent_sends_a_typed_message() -> None:
    ask = profile_hook.parse("[bench:profile name=cluster project=p1] look at it")
    assert ask is not None and ask.intent == ""


def test_the_directives_intents_are_the_doors() -> None:
    """Restated in profile_hook so the validator can load it alone; held equal here."""
    from kube_agents_bench import inject_transport

    assert profile_hook.INTENTS == inject_transport.INTENTS
