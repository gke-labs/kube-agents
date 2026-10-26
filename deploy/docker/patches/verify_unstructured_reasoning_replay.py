#!/usr/bin/env python3
"""Build gate for the unstructured-reasoning replay patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_unstructured_reasoning_replay.py``. The applier proves its anchors
matched once; this proves the inserted statements are where they have to be
and behave:

- in ``agent/turn_context.py``'s ``build_api_messages``, the marked import and
  its call sit directly in the per-message loop, between the ``api_msg`` clone
  and the reasoning-field copy. That they also follow the ``api_content``
  replay is not checked: v2026.9.14 puts the replay between those two, and a
  bump that moved it below the copy would pass here. Assistant rows carry the
  sidecar only when ``sanitize_context`` changed their text
  (``agent/session_persistence.py``), so the gap is narrow, but a reasoning
  reply that also held a sanitized tag would then go out raw;
- in ``agent/chat_completion_helpers.py``'s ``_iteration_summary_api_messages``,
  they sit directly in its loop between ``substitute_api_content(api_msg)`` and
  the ``append``;
- every name the inserted statements read is bound there (``patchlib.unbound``);
- ``hermes_cli/unstructured_reasoning.py`` is installed, and the inserted
  statements, run against it on stand-in wire messages, turn each reasoning
  shape (``thought``, ``thought:``, ``thought:text``, with and without
  ``tool_calls``) into the placeholder with every other field intact, and leave
  a normal reply, prose that starts with "Thought about", a later "thought", a
  user message and a tool-call reply with no content unchanged.

The rule module is loaded from its file, not imported through the
``hermes_cli`` package, so this runs nothing of Hermes's.

Usage::

    cd /opt/hermes && python3 verify_unstructured_reasoning_replay.py
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import os
import sys
import textwrap
import types
from pathlib import Path

import patchlib

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
MARKER = "kube-agents patch: unstructured_reasoning_replay"
PLACEHOLDER = "[omitted]"
RULE_PACKAGE = "hermes_cli"
RULE_MODULE = "hermes_cli.unstructured_reasoning"
RULE_RELATIVE = "hermes_cli/unstructured_reasoning.py"
FAILURES: list[str] = []

# (file, function, the call the insertion must follow, the call it must precede)
SITES = [
    ("agent/turn_context.py", "build_api_messages", "_clone_message_for_send", "_copy_reasoning_content_for_api"),
    ("agent/chat_completion_helpers.py", "_iteration_summary_api_messages", "substitute_api_content", "append"),
]

TOOL_CALLS = [{"id": "call_1__thought__c2ln", "type": "function",
               "function": {"name": "terminal", "arguments": '{"command": "kubectl version"}'}}]

# (wire message in, whether its content becomes the placeholder, what it is)
CASES = [
    ({"role": "assistant", "content": "thought\nNow that the task is created, I need to check.",
      "tool_calls": TOOL_CALLS}, True, "a reasoning reply with tool_calls"),
    ({"role": "assistant", "content": "Thought\nThe user wants the version."}, True,
     "a reasoning reply without tool_calls"),
    ({"role": "assistant", "content": "thought:\nThe user wants to delete the namespace."}, True,
     "a reasoning reply under thought:"),
    ({"role": "assistant", "content": "thought:The user asked for the version."}, True,
     "a reasoning reply on the thought: line"),
    ({"role": "assistant", "content": "\n  \n  THOUGHT  \r\nthe plan", "finish_reason": "stop"}, True,
     "a reasoning reply under blank lines, upper case and a CRLF"),
    ({"role": "assistant", "content": "thought"}, True, "a reply that is the bare line alone"),
    ({"role": "assistant", "content": "The cluster runs 1.33."}, False, "a normal reply"),
    ({"role": "assistant", "content": "I thought it was 1.32.\nthought\nIt is 1.33."}, False,
     "a reply that says thought later"),
    ({"role": "assistant", "content": "Thought about it: the cluster runs 1.33."}, False,
     "a reply opening Thought about"),
    ({"role": "assistant", "content": "thoughts\nthe plan"}, False, "a reply opening thoughts"),
    ({"role": "user", "content": "thought\nwhat version is it"}, False, "a user message"),
    ({"role": "assistant", "content": None, "tool_calls": TOOL_CALLS}, False, "a tool-call reply with no content"),
]


def fail(msg: str) -> None:
    FAILURES.append(msg)


def _calls(node: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Call) and (
        (isinstance(n.func, ast.Attribute) and n.func.attr == name)
        or (isinstance(n.func, ast.Name) and n.func.id == name)) for n in ast.walk(node))


def inserted_block(relative: str, function: str, after: str, before: str) -> str:
    """The marked import and the statement after it in ``function``'s loop, dedented, or "" after a failure."""
    path = HERMES / relative
    try:
        source = path.read_text()
        tree = ast.parse(source)
    except (OSError, SyntaxError) as exc:
        fail(f"{relative} cannot be read: {exc}")
        return ""
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function]
    if len(funcs) != 1:
        fail(f"{relative}: expected 1 module-level def {function}(), found {len(funcs)}; the message build moved")
        return ""
    lines = source.splitlines(keepends=True)
    for loop in ast.walk(funcs[0]):
        if not isinstance(loop, ast.For):
            continue
        body = loop.body
        after_at = next((i for i, s in enumerate(body) if _calls(s, after)), None)
        before_at = next((i for i, s in enumerate(body) if i != after_at and _calls(s, before)), None)
        if after_at is None or before_at is None:
            continue
        marked = [i for i, s in enumerate(body)
                  if isinstance(s, ast.ImportFrom) and MARKER in "".join(lines[s.lineno - 1: s.end_lineno])]
        if len(marked) != 1:
            fail(f"{relative}: expected 1 marked import in {function}()'s message loop, found {len(marked)}; "
                 "the patch is missing or moved")
            return ""
        at = marked[0]
        if not after_at < at < at + 1 < before_at:
            fail(f"{relative}: the inserted statements are not between {after}() and {before}() in {function}(); "
                 "the patch moved")
            return ""
        nodes = body[at: at + 2]
        if not (isinstance(nodes[1], ast.Expr) and _calls(nodes[1], "_kube_replace_reasoning")):
            fail(f"{relative}: the marked import in {function}() is not followed by its call; the patch is incomplete")
            return ""
        for node in nodes:
            missing = patchlib.unbound(tree, node)
            if missing:
                fail(f"{relative}: an inserted statement reads names nothing binds before it: {missing}")
                return ""
        return textwrap.dedent("".join(lines[nodes[0].lineno - 1: nodes[1].end_lineno]))
    fail(f"{relative}: no loop in {function}() calls {after}() and {before}(); the message build moved")
    return ""


def load_rule() -> bool:
    """Register the installed rule module, loaded from its file, under its import name."""
    path = HERMES / RULE_RELATIVE
    if not path.is_file():
        fail(f"{RULE_RELATIVE} is not installed; the inserted import would fail on every model call")
        return False
    spec = importlib.util.spec_from_file_location(RULE_MODULE, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 -- any failure is the build's to see
        fail(f"{RULE_RELATIVE} does not load: {exc!r}")
        return False
    sys.modules.setdefault(RULE_PACKAGE, types.ModuleType(RULE_PACKAGE))
    sys.modules[RULE_MODULE] = module
    return True


def run_block(block: str, api_msg: dict) -> dict | None:
    try:
        exec(compile(block, "<unstructured-reasoning-replay>", "exec"), {"api_msg": api_msg})
    except Exception as exc:  # noqa: BLE001 -- any failure is the build's to see
        fail(f"the inserted statements raised when run: {exc!r}")
        return None
    return api_msg


def main() -> int:
    blocks = [(site[0], inserted_block(*site)) for site in SITES]
    if all(block for _, block in blocks) and load_rule():
        for relative, block in blocks:
            for message, replaced, what in CASES:
                got = run_block(block, copy.deepcopy(message))
                if got is None:
                    continue
                want = dict(copy.deepcopy(message), content=PLACEHOLDER) if replaced else message
                if got != want:
                    fail(f"{relative}: {what} goes to the model as {got!r}, want {want!r}")
    if FAILURES:
        for f in FAILURES:
            print(f"verify_unstructured_reasoning_replay: {f}", file=sys.stderr)
        return 1
    print("verify_unstructured_reasoning_replay: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
