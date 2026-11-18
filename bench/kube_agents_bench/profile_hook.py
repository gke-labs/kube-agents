"""A bench case's ask addressed to an AgentProfile as a new top task (#5945).

A prompt that starts with the directive

    [bench:profile name=<profile> <key>=<value> ...]

is sent through the inject door to that profile, with the other fields as the
task's parameters (a target's ``project``, ``location`` and ``cluster``, and
any parameter the profile declares), rather than to the platform agent. The
gateway routes it down the one path every door takes for a profile turn: the
profile is resolved (a generic profile yields to the one pinned to the
target), and its card, tier, parameters and target scope are checked. A
refusal is posted to the conversation like any other.

The directive ends at its first ``]``, so the ask may follow on the same line
(a folded YAML block joins the lines) or on the next. Values take the
harness's ``{{AGENT_CLUSTER_*}}`` and ``{{FLEET_CLUSTER_*}}`` placeholders,
expanded before this runs. Values hold no spaces: a field is ``key=value``,
split on whitespace.

Only the inject transport reads it. The api transport refuses a prompt that
opens with it (it has no way to address a profile), so a case using it is listed
in hack/eval/api-lane-exclusions.txt, which scripts/test_eval_rosters.py checks.
It must open the prompt, and no other ``[bench:...]`` directive may follow it:
the readers that schedule a stopped ask or a replay (stop_hook, hack/ci-eval-pr.sh,
lane.runs_alone) read only a prompt's first line, so a combined case would run in
the wrong phase. Combining one is a later change, made when a case needs it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The directive's opening, and where it ends.
PROFILE_DIRECTIVE = "[bench:profile"
DIRECTIVE_END = "]"
#: The field naming the profile; every other field is a parameter.
NAME_FIELD = "name"
FIELD_SEPARATOR = "="
#: A profile name the gateway accepts: a DNS-1123 label, which is what its
#: ValidSubjectToken holds a profile name to (a2a/lib/envelope.go). The
#: gateway checks it again; this catches a typo in a case before a run.
PROFILE_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
#: The opening every bench directive shares; none may follow this one.
BENCH_DIRECTIVE = "[bench:"
#: A parameter name the gateway accepts: TaskParameterNameRE in
#: a2a/lib/payload.go (lower-case, at most 63 characters).
PARAMETER_KEY = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class ProfileAsk:
    """A prompt's profile directive: the profile, its parameters, and the ask
    with the directive removed."""

    name: str
    prompt: str
    parameters: dict[str, str] = field(default_factory=dict)


def is_profile_ask(prompt: str) -> bool:
    """Whether ``prompt`` opens with the directive: the test the harness's api
    path refuses on, and the roster test reads, so they can't disagree."""
    return prompt.lstrip().startswith(PROFILE_DIRECTIVE)


def parse(prompt: str) -> ProfileAsk | None:
    """The prompt's profile directive, or None when it has none.

    Raises:
        ValueError: The directive is there but malformed: unclosed, no name, a
            field that isn't ``key=value``, a repeated key, a name that can't be
            a profile's, or no ask after it.
    """
    text = prompt.lstrip()
    if not text.startswith(PROFILE_DIRECTIVE):
        return None
    end = text.find(DIRECTIVE_END)
    if end < 0:
        raise ValueError(f"{PROFILE_DIRECTIVE} ... has no closing {DIRECTIVE_END}")
    body, ask = text[len(PROFILE_DIRECTIVE):end], text[end + len(DIRECTIVE_END):].strip()
    fields: dict[str, str] = {}
    for item in body.split():
        key, sep, value = item.partition(FIELD_SEPARATOR)
        if not sep or not key or not value:
            raise ValueError(f"{PROFILE_DIRECTIVE}: {item!r} is not key=value")
        if key in fields:
            raise ValueError(f"{PROFILE_DIRECTIVE}: {key} is given twice")
        fields[key] = value
    name = fields.pop(NAME_FIELD, "")
    if not name:
        raise ValueError(f"{PROFILE_DIRECTIVE}: {NAME_FIELD}= is required")
    if not PROFILE_NAME.match(name):
        raise ValueError(f"{PROFILE_DIRECTIVE}: {name!r} is not a profile's name")
    for key in fields:
        if not PARAMETER_KEY.match(key):
            raise ValueError(f"{PROFILE_DIRECTIVE}: {key!r} is not a parameter's name")
    if not ask:
        raise ValueError(f"{PROFILE_DIRECTIVE}: nothing to ask after the directive")
    if ask.startswith(BENCH_DIRECTIVE):
        raise ValueError(f"no other {BENCH_DIRECTIVE}...] directive may follow {PROFILE_DIRECTIVE}: "
                         "its scheduling readers see only a prompt's first line")
    return ProfileAsk(name=name, prompt=ask, parameters=fields)
