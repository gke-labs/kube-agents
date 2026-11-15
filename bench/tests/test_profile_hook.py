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
        ("[bench:profile name=cluster]   ", "nothing to ask"),
    ],
)
def test_a_malformed_directive_is_refused(prompt: str, why: str) -> None:
    with pytest.raises(ValueError, match=why):
        profile_hook.parse(prompt)
