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

:func:`set_mode` moves the dial the way an admin's tap on the Slack Home tab
does under ``spec.mode: next``: in the agent container it posts ``dial.level``
to session-kv's board action route (``POST /v1/board/actions``), which checks
the admin list, records the level and reports it to the broker before it
answers. The script reads the board snapshot's ``dial`` section first for the
level to put back and an admin to act as. :func:`restore` posts the level it
found. The install must run the dial (``KUBEAGENTS_DIAL=on``) with at least one
admin; otherwise nothing is set and the set is :class:`ModeUnavailable`.
:func:`entry` is the trajectory entry for the sets.

Running a case at a mode comes in a later change; nothing calls this module
yet. It imports nothing from ``devops_bench``, so the case loader
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
    "SET_ENTRY",
    "ModeUnavailable",
    "WriteMode",
    "is_write_mode_ask",
    "parse",
    "read_command",
    "read_mode",
    "restore",
    "set_mode",
]

#: The first line that makes a prompt an ask at a set write mode.
DIRECTIVE = "[bench:write-mode]"
SET_FIELD = "set="
FROM_FIELD = "from="
#: The broker's modes (``vcs_broker.WRITE_MODES``).
MODES = ("none", "propose")
#: The trajectory entry the harness records for the sets.
SET_ENTRY = "harness_write_mode_set"
#: The dial level that gives each mode (``dial_state.LEVELS``): the dial's
#: Explain is the broker's none; Propose fixes leaves the broker at propose.
DIAL_LEVELS = {"none": "explain", "propose": "propose"}
#: session-kv in the agent pod, as every in-pod client reaches it, and the
#: routes the set uses: the board snapshot and the board action.
SESSION_KV_URL = "http://127.0.0.1:8699"
#: The pods whose restart would make "no restart" false: the agent's and the
#: broker's, by the names the operator gives them (``<agent>-<suffix>-...``).
#: The credential proxy's exec tier shares the broker's prefix but serves no
#: VCS verb, and its replicas scale on their own, so it is left out.
WATCHED_POD_SUFFIXES = ("-gateway-", "-credential-proxy-")
UNWATCHED_POD_SUFFIXES = ("-credential-proxy-exec-tier-",)
#: ``kubectl get pods -o jsonpath=`` printing ``name restarts...`` per pod.
PODS_JSONPATH = '{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[*].restartCount}{"\\n"}{end}'
#: The scripts print this first, so an answer without it is a script that
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
    """A read or set never ran to completion in the pod: infrastructure, not a run.

    ``previous`` is the dial level a failed set found, when it got that far, so
    the caller still puts it back: a level recorded but not confirmed by the
    broker is still recorded.
    """

    def __init__(self, message: str, previous: str | None = None) -> None:
        super().__init__(message)
        self.previous = previous


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


_SET_SCRIPT = r"""
import json, os, sys, urllib.request
SENTINEL, URL, LEVEL = sys.argv[1:4]
print(SENTINEL)
out = {"set": False, "previous": None, "error": None}
def call(path, body=None):
    request = urllib.request.Request(
        URL + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + os.environ.get("SESSION_KV_API_KEY", ""), "Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}")
try:
    dial = call("/v1/board").get("dial") or {}
    if not dial.get("enabled"):
        raise RuntimeError("the dial is off on this install (KUBEAGENTS_DIAL)")
    admins = dial.get("admins") or []
    if not admins:
        raise RuntimeError("the dial has no admin (KUBEAGENTS_DIAL_ADMINS) to act as")
    out["previous"] = dial.get("level")
    answer = call("/v1/board/actions", {"action": "dial.level", "level": LEVEL, "user": admins[0]})
    if not answer.get("ok"):
        raise RuntimeError("session-kv refused it: %s" % answer.get("message"))
    if not answer.get("reported"):
        raise RuntimeError("the broker did not confirm the level: %s" % answer.get("message"))
    out["set"] = True
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(json.dumps(out))
"""


def set_command(level: str) -> str:
    """The agent-container command that moves the dial to ``level``."""
    args = " ".join(shlex.quote(a) for a in (SENTINEL, SESSION_KV_URL, level))
    return f"cd /tmp && python3 -c {shlex.quote(_SET_SCRIPT)} {args}"


def _set(shell: Callable[[str, float], str], level: str, timeout: float) -> str:
    payload = _answer(shell(set_command(level), timeout), "set")
    previous = payload.get("previous") if isinstance(payload.get("previous"), str) else None
    if not payload.get("set") or previous is None:
        raise ModeUnavailable(f"write mode: the dial could not be set to {level}: {payload.get('error')!r}", previous)
    return previous


def set_mode(shell: Callable[[str, float], str], mode: str, timeout: float, keep: str | None = None) -> str:
    """Move the dial to ``mode``'s level through session-kv's board action, as an
    admin's tap does, and return the dial level to put back with :func:`restore`.

    That is the level the dial was at, unless ``keep`` names one: pass the level
    an earlier set returned, as the second set of a ``from=`` ask must, and that
    level comes back instead of the one the first set made. The return is a dial
    level (``explain``/``propose``), never a broker mode: :func:`entry` takes
    :func:`read_mode` readings.

    The set acts as the first name on the admin list, so the Home tab shows that
    admin as the last to set the level until the put-back.

    Raises :class:`ModeUnavailable` when the set never ran, session-kv refused
    it, or the broker did not confirm it; its ``previous`` honours ``keep`` too.
    """
    try:
        previous = _set(shell, DIAL_LEVELS[mode], timeout)
    except ModeUnavailable as exc:
        if keep is not None:
            exc.previous = keep
        raise
    return previous if keep is None else keep


def restore(shell: Callable[[str, float], str], previous: str, timeout: float) -> None:
    """Put the dial back at ``previous``, the level :func:`set_mode` found."""
    _set(shell, previous, timeout)


def watched_pods(listing: str | None, agent: str) -> dict[str, str] | None:
    """``{pod: restart counts}`` for the agent's and the broker's pods in a
    :data:`PODS_JSONPATH` listing; ``None`` when the listing was not read."""
    if listing is None:
        return None
    prefixes = tuple(agent + suffix for suffix in WATCHED_POD_SUFFIXES)
    skipped = tuple(agent + suffix for suffix in UNWATCHED_POD_SUFFIXES)
    pods = {}
    for line in listing.splitlines():
        name, _, counts = line.strip().partition(" ")
        if name.startswith(prefixes) and not name.startswith(skipped):
            pods[name] = counts.strip()
    return pods


def entry(
    asked: WriteMode,
    initial_mode: str,
    readings: list[dict],
    restored_mode: str | None,
    pods: tuple[dict[str, str] | None, dict[str, str] | None],
    error: str = "",
) -> dict:
    """The trajectory entry for the sets. Every mode here is the broker's, as
    :func:`read_mode` reads it (``none``/``propose``), never a dial level from
    :func:`set_mode`. ``completed`` only when the install
    started at another mode than the first set, every set read back as asked,
    the watched pods are the same pods, unrestarted, after the run as before the
    first set, and the put-back read back ``initial_mode``. Pods that could not be
    listed are an error: the entry would otherwise claim a no-restart it never
    saw. A put-back that failed is one too: the next case would run at the
    wrong mode."""
    before, after = pods
    if not error and (initial_mode not in MODES or restored_mode not in (*MODES, None)):
        error = f"the entry got {initial_mode!r} and {restored_mode!r}, which are not broker modes (a dial level from set_mode?)"
    wanted = 2 if asked.from_mode else 1
    first = asked.from_mode or asked.mode
    if not error and initial_mode == first:
        error = f"the install was already at {initial_mode}, so setting it proves nothing"
    if not error and len(readings) != wanted:
        error = f"{len(readings)} of {wanted} sets were made"
    if not error and any(r["read"] != r["set"] for r in readings):
        error = "a set did not read back as asked"
    if not error and (not before or after is None):
        error = "the agent's and the broker's pods could not be listed, so a restart could not be ruled out"
    if not error and before != after:
        error = f"the agent's or the broker's pod restarted or was replaced during the run ({before} then {after})"
    if not error and restored_mode != initial_mode:
        error = f"the install was not put back to {initial_mode} (read back {restored_mode})"
    return {
        "name": SET_ENTRY,
        "args": {"set": asked.mode, "from": asked.from_mode},
        "result": {
            "initial": initial_mode,
            "readings": readings,
            "restored": restored_mode,
            "pods": [before, after],
            "error": error or None,
        },
        "status": "error" if error else "completed",
    }
