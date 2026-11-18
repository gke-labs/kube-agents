# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Parse a write-mode ask, and read the VCS broker's write mode from the sandbox.

The write mode is the autonomy dial's level as the VCS broker enforces it:
``none`` is Explain, ``propose`` is Propose fixes. A prompt whose first line is
:data:`DIRECTIVE` asks for a run at a mode: the line carries ``set=<mode>`` and,
optionally, ``from=<mode>`` (set first, so the run starts from a known mode);
the rest of the prompt is the ask. :func:`parse` reads that line.

:func:`read_mode` reads the broker's own answer: ``capabilities`` from the
sandbox, whose ``writeMode`` field is what ``vcs_broker.write_mode()`` returns
for the next request. A read that never ran, or a broker that reports no mode,
is infrastructure (:class:`ModeUnavailable`): an image without the write mode
cannot run such a case at all, which says nothing about the agent.

Setting the mode and running a case at it come in later changes; nothing calls
this module yet. It imports nothing from ``devops_bench``, so the case loader
can import it without the bench's dependencies.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "DIRECTIVE",
    "MODES",
    "ModeUnavailable",
    "WriteMode",
    "is_write_mode_ask",
    "parse",
    "read_command",
    "read_mode",
]

#: The first line that makes a prompt an ask at a set write mode.
DIRECTIVE = "[bench:write-mode]"
SET_FIELD = "set="
FROM_FIELD = "from="
#: The broker's modes (``vcs_broker.WRITE_MODES``).
MODES = ("none", "propose")
#: The script prints this first, so an answer without it is a script that
#: never ran (kubectl exec gave out) rather than one that failed.
SENTINEL = "__BENCH_WRITE_MODE__"
#: The repository ``capabilities`` asks about: the case's GitOps repository.
REPOSITORY_ENV = "BENCH_GITOPS_REPO"
#: ``version-control``'s script in the sandbox, as the platform profile
#: installs it.
VCS_SCRIPT = "/opt/data/profiles/platform/skills/version-control/scripts/vcs.py"
#: How much of an answer that is not JSON an error quotes.
ANSWER_TAIL_CHARS = 200


class ModeUnavailable(RuntimeError):
    """A read never ran to completion in the pod: infrastructure, not a run."""


@dataclass(frozen=True)
class WriteMode:
    """An ask at a set write mode: the ask, the mode, and the mode set first (or ``None``)."""

    prompt: str
    mode: str
    from_mode: str | None = None


def is_write_mode_ask(prompt: str) -> bool:
    """Whether ``prompt``'s first non-blank line opens with the directive. A
    near miss (the directive glued to its field) is one here, so :func:`parse`
    refuses it rather than sending the directive to the agent."""
    return prompt.strip().partition("\n")[0].startswith(DIRECTIVE)


def parse(prompt: str) -> WriteMode | None:
    """The ask ``prompt`` holds, or ``None`` for an ordinary ask.

    Raises:
        ValueError: The directive stands glued to a field, carries a field other
            than one ``set=`` and at most one ``from=`` naming a mode, or no
            ask follows it.
    """
    if not is_write_mode_ask(prompt):
        return None
    first, _, rest = prompt.strip().partition("\n")
    words = first.split()
    if words[0] != DIRECTIVE:
        raise ValueError(f"{DIRECTIVE} must stand alone, then a space before {SET_FIELD}<mode>; got {words[0]}")
    fields: dict[str, str] = {}
    for word in words[1:]:
        name = next((f for f in (SET_FIELD, FROM_FIELD) if word.startswith(f)), None)
        if name is None or name in fields:
            raise ValueError(f"{DIRECTIVE} takes one {SET_FIELD}<mode> and at most one {FROM_FIELD}<mode>, got {words[1:]}")
        value = word[len(name) :]
        if value not in MODES:
            raise ValueError(f"{DIRECTIVE}: {name}{value} is not one of {', '.join(MODES)}")
        fields[name] = value
    if SET_FIELD not in fields:
        raise ValueError(f"{DIRECTIVE} needs {SET_FIELD}<mode>")
    if fields.get(FROM_FIELD) == fields[SET_FIELD]:
        raise ValueError(f"{DIRECTIVE}: {FROM_FIELD} and {SET_FIELD} name the same mode, so nothing changes")
    ask = rest.strip()
    if not ask:
        raise ValueError(f"{DIRECTIVE} needs the ask on the lines after it")
    return WriteMode(prompt=ask, mode=fields[SET_FIELD], from_mode=fields.get(FROM_FIELD))


_READ_SCRIPT = r"""
import json, subprocess, sys
SENTINEL, VCS, REPOSITORY = sys.argv[1:4]
print(SENTINEL)
out = {"mode": None, "error": None}
try:
    proc = subprocess.run([sys.executable, VCS, "capabilities", REPOSITORY], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError("vcs.py exited %d: %s" % (proc.returncode, proc.stderr.strip()[-200:]))
    answer = json.loads(proc.stdout or "{}")
    if not isinstance(answer, dict):
        out["error"] = "capabilities answered %r" % (answer,)
    elif "writeMode" not in answer:
        out["error"] = answer.get("error") or "the broker reports no writeMode (it predates the write mode)"
    else:
        out["mode"] = answer["writeMode"]
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(json.dumps(out))
"""


def read_command(repository: str) -> str:
    """The sandbox command that prints the broker's write mode for ``repository``."""
    args = " ".join(shlex.quote(a) for a in (SENTINEL, VCS_SCRIPT, repository))
    return f"cd /tmp && python3 -c {shlex.quote(_READ_SCRIPT)} {args}"


def _answer(text: str, what: str) -> dict:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or lines[0].strip() != SENTINEL:
        raise ModeUnavailable(f"write mode: the {what} script never ran in the pod (kubectl exec gave out)")
    try:
        payload = json.loads(lines[-1])
    except ValueError as exc:
        raise ModeUnavailable(f"write mode: the {what} script's answer is not JSON: {lines[-1][:ANSWER_TAIL_CHARS]!r}") from exc
    if not isinstance(payload, dict):
        raise ModeUnavailable(f"write mode: the {what} script answered {payload!r}")
    return payload


def read_mode(sandbox_shell: Callable[[str, float], str], timeout: float, repository: str | None = None) -> str:
    """The broker's write mode, as ``capabilities`` reports it.

    Raises :class:`ModeUnavailable` when the read never ran or the broker
    reports no mode.
    """
    repository = repository or os.environ.get(REPOSITORY_ENV, "")
    if not repository:
        raise ModeUnavailable(f"write mode: {REPOSITORY_ENV} is unset, so there is no repository to ask capabilities about")
    payload = _answer(sandbox_shell(read_command(repository), timeout), "read")
    mode = payload.get("mode")
    if mode not in MODES:
        raise ModeUnavailable(f"write mode: could not read the broker's mode: {payload.get('error') or mode!r}")
    return mode
