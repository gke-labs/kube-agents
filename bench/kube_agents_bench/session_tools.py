"""Tool names that a session or profile pod's harness spells its own way.

The worker adapter runs session and profile pods under Claude Code or under
core-agent (and Antigravity, whose tools this does not map: its calls are
read as named, as before), and the two name the same tool differently (``Bash`` and
``bash``) and key its arguments differently (``Read``'s ``file_path`` and
``read_file``'s ``path``). Cases name the tools of the harness they were
written against. On an inject record, a router call the worker adapter
published -- one without the hermes bridge's origin mark
(``inject_transport.ENTRY_ORIGIN_FIELD``) and without a worker's ``agent``
tag -- is read under either harness's name, its arguments re-keyed to match,
so a ``tool_called`` leaf naming one harness's tool grades a call made under
the other (:func:`as_named`).

Everything else is read as named. Hermes calls, whether the bridge labelled
them, an api-transport record carried them, or a worker's session store
recorded them, are never re-read. Hermes has its own ``read_file``, keyed
``path`` like core-agent's, and reading one of its calls as Claude Code's
``Read`` would trip a skill-files safeguard when the platform agent reads its
own skills. The hermes bridge's cli executor publishes its parts unlabelled,
so such a call would be re-read too. The api executor is the bridge's default
and labels every part. Re-reading only adds matches. Under the cli executor
that could pass a positive check on a call that was not the session's, or
fail a ``none`` safeguard on one; and such a call counts as a session pod's,
so the scorer would grade a session-only check it would otherwise set aside,
as every check graded before this mapping existed.

An argument with no counterpart is dropped from the re-read, so a pattern
on it rules the call out, the way a call that lacks the argument already
is. Claude Code's ``Grep`` ``glob`` filter has no counterpart in core-agent's
``grep``.
"""

from __future__ import annotations

from typing import Any

# The inject transport's and the verifier's names for what this reads,
# duplicated (``inject_transport`` imports nothing of the bench's, and the
# verifier does not import the transport).
ENTRY_ORIGIN_FIELD = "origin"
INJECT_TASK_ENTRY = "inject.task"
ENTRY_AGENT_FIELD = "agent"
ENTRY_NAME_FIELD = "name"
ENTRY_ARGS_FIELD = "args"

# (Claude Code's name, core-agent's name, Claude Code's argument key ->
# core-agent's). core-agent's names and keys are its builtins at the version
# a2a/go.mod pins and ADK's skill toolset (``load_skill``'s ``name``); Claude
# Code's ``Skill`` has keyed the skill ``skill`` and, in older versions,
# ``command``.
_PAIRS: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("Bash", "bash", {"command": "command"}),
    ("Skill", "load_skill", {"skill": "name", "command": "name"}),
    ("Read", "read_file", {"file_path": "path"}),
    ("Glob", "glob", {"pattern": "pattern", "path": "path"}),
    ("Grep", "grep", {"pattern": "pattern", "path": "path"}),
)

# Each name's counterpart, and how a call under the name re-keys its
# arguments for the counterpart: counterpart key -> this name's keys, the
# first one the call carries supplying the value.
_COUNTERPART: dict[str, tuple[str, dict[str, tuple[str, ...]]]] = {}
for _claude, _core, _keys in _PAIRS:
    _to_core: dict[str, tuple[str, ...]] = {}
    for _claude_key, _core_key in _keys.items():
        _to_core[_core_key] = (*_to_core.get(_core_key, ()), _claude_key)
    _COUNTERPART[_claude] = (_core, _to_core)
    _COUNTERPART[_core] = (_claude, {key: (source,) for key, source in _keys.items()})
del _claude, _core, _keys, _to_core, _claude_key, _core_key

# core-agent's other read tools (``toolgrant.go`` classes them with
# ``read_file``), which Claude Code has no single counterpart for and cases
# name as they are.
CORE_AGENT_ONLY_READ_NAMES = frozenset({"read_many_files", "view_file_outline", "list_dir"})
# Names that a Hermes record never carries: every name above except
# core-agent's ``read_file``, which Hermes has too. A leaf naming only these
# can fire on nothing but a call the worker adapter published.
HERMES_SHARED_NAMES = frozenset({"read_file"})
SESSION_ONLY_TOOL_NAMES = (frozenset(_COUNTERPART) | CORE_AGENT_ONLY_READ_NAMES) - HERMES_SHARED_NAMES


def is_inject_trajectory(trajectory: list[Any]) -> bool:
    """Whether the trajectory is the inject transport's: it carries the task marker."""
    return any(isinstance(e, dict) and e.get(ENTRY_NAME_FIELD) == INJECT_TASK_ENTRY for e in trajectory)


def is_worker_adapter_call(entry: dict[str, Any], inject: bool) -> bool:
    """Whether an entry of an inject record is a router call the worker
    adapter published: no bridge origin and no worker ``agent`` tag."""
    return inject and not entry.get(ENTRY_AGENT_FIELD) and entry.get(ENTRY_ORIGIN_FIELD) is None


def counterpart(name: str) -> str | None:
    """The other harness's name for a tool, or None when it has none here."""
    pair = _COUNTERPART.get(name)
    return None if pair is None else pair[0]


def as_named(entry: dict[str, Any], wanted: set[str], inject: bool) -> dict[str, Any]:
    """The entry as a leaf naming ``wanted`` reads it.

    A worker-adapter call whose own name is not wanted but whose counterpart
    is comes back as a copy under the counterpart, its arguments re-keyed
    (an argument with no counterpart dropped). Anything else comes back
    unchanged, a call already named as wanted included: a leaf that lists
    both spellings reads each call as it was made."""
    name = entry.get(ENTRY_NAME_FIELD)
    if not isinstance(name, str) or name in wanted or not is_worker_adapter_call(entry, inject):
        return entry
    pair = _COUNTERPART.get(name)
    if pair is None or pair[0] not in wanted:
        return entry
    other, keys = pair
    view = dict(entry)
    view[ENTRY_NAME_FIELD] = other
    args = entry.get(ENTRY_ARGS_FIELD)
    if isinstance(args, dict):
        view[ENTRY_ARGS_FIELD] = {
            key: args[present]
            for key, sources in keys.items()
            for present in [next((source for source in sources if source in args), None)]
            if present is not None
        }
    return view
